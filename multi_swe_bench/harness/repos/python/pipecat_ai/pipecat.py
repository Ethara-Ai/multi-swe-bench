import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# pipecat-ai/pipecat — a Python framework for voice/multimodal AI agents.
#
# Discovery (verified in Docker, python:3.12-bookworm, native arm64):
#  - 60-PR range #60..#4378. The repo was renamed (early "dailyai" ->
#    "pipecat-ai") and migrated setuptools -> uv, requires-python 3.7 -> 3.11.
#  - Tests are pytest (pytest-asyncio); the CI uses `uv sync` then `uv run
#    pytest`. The modern suite is large (~1066 tests collected).
#  - One config, no era split: install is a fallback chain -- `uv sync
#    --all-extras` for the modern era, degrading to `uv pip install -e .` for
#    the old "dailyai" era; pytest is the runner throughout, so one parse_log
#    serves every era.
#  - System dep portaudio19-dev is required (pipecat imports pyaudio).
#  - pipecat has many optional service extras (anthropic/aws/google/livekit/
#    ...); --all-extras pulls them so test files importing them collect.


_PY = "3.12"


class PipecatImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone --no-single-branch https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label_block = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

{label_block}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_ROOT_USER_ACTION=ignore
ENV UV_LINK_MODE=copy
WORKDIR /home/

# git for checkout; portaudio19-dev for pyaudio; build tools for native wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential portaudio19-dev \\
    && rm -rf /var/lib/apt/lists/*

# uv for arm64 (fast); pip3 fallback for amd64 where uv segfaults under qemu.
RUN pip install --no-cache-dir uv

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class PipecatImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return PipecatImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        check_git = """#!/bin/bash
set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi
echo "check_git_changes: No uncommitted changes"
exit 0
"""

        # Shared install: resolve + install pipecat and its dependencies for
        # the checked-out (possibly patched) tree. Fallback chain spans eras.
        install = """#!/bin/bash
set -u
cd /home/__REPO__
uv venv --python __PY__ .venv >/dev/null 2>&1 || python3 -m venv .venv || true
export VIRTUAL_ENV="$PWD/.venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"

# Modern era: uv sync with every extra + the dev group. Degrades gracefully
# to a plain editable install for the old "dailyai" era.
# pip3 final fallback handles amd64 where uv segfaults under qemu emulation.
uv sync --all-extras --group dev >/dev/null 2>&1 \\
  || uv sync --all-extras >/dev/null 2>&1 \\
  || uv sync >/dev/null 2>&1 \\
  || pip3 install -e ".[all]" >/dev/null 2>&1 \\
  || pip3 install -e . >/dev/null 2>&1 || true

# Guarantee the pytest runner is present regardless of which branch ran.
uv pip install pytest pytest-asyncio pytest-aiohttp >/dev/null 2>&1 \\
  || pip3 install pytest pytest-asyncio pytest-aiohttp >/dev/null 2>&1 || true
"""

        install = install.replace("__REPO__", repo).replace("__PY__", _PY)

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the install at the base commit (deps cached into the image layer).
bash /home/install.sh || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        run_tests = """#!/bin/bash
# Re-run install (a patch may add a dependency), then run pytest. `-v` gives
# one `path::test STATUS` line per test for parse_log; the collection-error
# flag keeps a single bad import from aborting the whole run.
set -uo pipefail
cd /home/__REPO__
bash /home/install.sh || true
.venv/bin/python -m pytest -v -rA --continue-on-collection-errors \\
    -p no:cacheprovider tests/ || true
""".replace("__REPO__", repo)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
git apply --3way --whitespace=nowarn /home/fix.patch \\
  || git apply --whitespace=nowarn --reject /home/fix.patch \\
  || echo "git apply fix.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "install.sh", install),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        label_block = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{label_block}

{self.global_env}

{copy_commands}

WORKDIR /home/{self.pr.repo}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{prepare_commands}

{self.clear_env}

"""


_BUNDLE_NIS = [
    "1008-1064-1080-1083-1099-1100-1103-1104-1105-1106-1107-1110-1111-1115-1116-1118-1121-1122-1125-1126-1131-1133-1139",
    "1025-1672-1729-1746-1755-1774-1775-1779-1780-1784-1785-1790-1793-1796-1799-1800-1801-1805-1806-1810-1811-1814-1815-1816-1819-1820-1824-1825-1828-1829-1830-1834-1835-1839-1840-1841-1844-1847-1848-1850-1854-1856-1857-1859-1860-1862-1864-1866-1867-1868-1869-1870-1871-1872-1873-1878-1879-1880-1881-1884-1885-1886-1890-1895-1897-1898-1899-1901-1903-1906-1907",
    "110-111-112-113-114-115",
    "1112-1199-1228-1233-1238-1240-1241-1242-1243-1245-1248-1249-1250-1251-1252-1253-1257-1259-1260-1261-1263-1264-1265-1266-1269-1272-1274-1278-1279-1280-1281-1282-1283-1285-1286-1287-1288-1289-1291-1292-1293-1294-1295-1296-1297-1299-1300",
    "1123-1128-1129-1159-1163-1166-1173-1174-1175-1176-1177-1179-1181-1182-1183-1184-1185-1186-1190-1191-1193-1194-1196-1197-1198-1202-1203-1204-1208-1209-1212-1213-1214-1215-1216-1218-1219-1220-1222-1223-1224-1225-1226-1227",
    "1130-1270-1290-1383-1388-1441-1459-1460-1463-1465-1468-1469-1471-1472-1473-1474-1475-1476-1477-1478-1479-1486-1487-1488-1489-1490-1492-1494-1496-1497-1499-1500-1501-1502-1503-1504-1506-1507",
    "1211-1301-1302-1303-1304-1308-1310-1311-1312-1313-1317-1318-1320-1321-1324-1326-1327-1329-1330-1331-1332-1334-1336-1340-1341-1342-1343-1353-1354-1356-1358-1359-1360-1366-1367-1368-1370-1371-1372-1374-1376-1377-1378-1379-1381-1385-1387-1390-1392-1394-1395-1396-1397-1404-1405-1406-1408-1409-1411-1413-1414-1416",
    "122-123-124-125-126-128-129-130-132-133",
    "1348-1418-1498-1508-1509-1512-1513-1514-1515-1517-1518-1519-1521-1523-1524-1531-1532-1534-1537-1539-1542-1545-1550-1551-1552-1553-1555-1560-1561-1565-1566-1569",
    "1389-1420-1423-1424-1425-1429-1434-1436-1439-1442-1447-1448-1449-1451-1452-1453-1455-1457",
    "1443-1670-1671-1704-1724-1732-1737-1739-1742-1745-1748-1751-1752-1753-1757-1759-1760-1761-1762-1766-1767-1768-1770-1773",
    "1525-1533-1544-1563-1577-1579-1580-1583-1585-1586-1588-1592-1595-1596-1597-1598-1600-1601-1603-1607-1609-1610-1611-1612-1614-1615-1616-1619-1620-1625-1628-1631-1634-1636-1638",
    "1581-1587-1593-1613-1627-1640-1642-1644-1647-1648-1649-1650-1657-1658-1660-1664-1666-1667-1668-1669-1678-1679-1680-1681-1688-1689-1690-1693-1696-1697-1699-1701-1702-1705-1706-1707-1708-1709-1710-1711-1714-1717-1720-1721-1722-1723-1725-1726",
    "1623-1786-2036-2051-2077-2079-2084-2085-2086-2087-2088-2089-2090-2091-2095-2098-2101-2103-2104-2105-2107-2110-2113-2114-2115-2116-2118-2119-2120-2122-2123-2125-2129-2130-2131-2133-2134-2135-2137-2138-2139",
    "1683-1750-1804-1822-1876-1889-1904-1908-1909-1914-1915-1917-1920-1921-1922-1923-1924-1926-1927-1931-1934-1935-1936-1937-1938-1939-1941-1943-1944-1945-1946-1947",
    "175-189-190-191-193-194-195-198-199-200",
    "1874-2266-2610-2627-2644-2646-2648-2651-2652-2653-2654-2655-2660-2662-2663-2664-2666-2667-2668-2669-2671-2672-2673-2675-2676-2679-2680-2682-2683-2684-2686-2687-2689-2692-2694-2696-2697-2698-2699-2705-2706-2707-2708-2712-2713-2714-2715-2716-2717-2718-2719-2720-2721-2722-2724-2726-2727-2728-2729-2731-2732-2733-2734",
    "1887-1925-1950-1952-1998-2001-2002-2004-2006-2008-2011-2012-2014-2015-2016-2017-2020-2021-2022-2023-2024-2027-2032-2034-2035-2037-2038-2039-2044-2048-2049-2052-2053-2054-2055-2056-2058-2059-2060-2061-2063-2065-2066-2067-2068-2069-2071-2072-2073-2074",
    "1910-1932-2150-2171-2195-2196-2198-2199-2201-2204-2208-2210-2214-2223-2224-2225-2226-2227-2230-2232-2233-2234-2235-2236-2237-2238-2239-2240-2242-2243-2244-2246-2248-2252-2254-2255-2256-2259-2260-2261-2265-2268-2269-2270-2273-2274-2279-2284-2286-2287-2288-2289-2290-2291-2292-2293-2296-2298-2300-2301-2303-2305-2308-2309-2311-2312-2314-2316-2317",
    "1911-1949-1954-1956-1958-1960-1961-1962-1967-1972-1973-1975-1978-1979-1981-1982-1983-1984-1986-1988-1991",
    "201-204-205-206-208-211-212-213",
    "2096-2612-2735-2738-2742-2743-2744-2745-2747-2748-2751-2753-2756-2757-2758-2759-2761-2765-2766-2771-2772-2774-2775",
    "2166-2173-2176-2177-2181-2182-2183-2184-2185-2186-2189-2190-2193",
    "217-218-219-220",
    "2333-2391-2395-2397-2398-2399-2400-2403-2404-2405-2407-2409-2410-2411-2413-2415-2416-2417-2419-2420-2422-2424-2425-2427-2430-2433-2435",
    "2356-2440-2501-2504-2510-2511-2512-2514-2515-2516-2517-2520-2521-2523-2524-2525-2526-2527-2531",
    "2380-2392-2393-2394",
    "2387-2402-2408-2437-2438-2442-2444-2445-2446-2447-2450-2451-2452-2455-2464-2465-2468-2469-2470-2471-2474-2476-2478-2479-2480-2487-2489-2490-2491-2492-2493-2495-2496-2497-2502",
    "2456-2462-2473-2545-2547-2596-2598-2604-2606-2607-2608-2613-2614-2619-2621-2622-2623-2628-2633-2635-2636-2637-2638-2639-2640",
    "2513-2529-2532-2534-2536-2537-2538-2540-2541-2542-2543-2546-2552-2555-2560-2562-2563-2565-2571-2572-2575-2576-2577-2578-2579-2580",
    "2518-2701-2777-2778-2781-2783-2786-2790-2792-2793-2794-2796-2802-2803-2805-2806-2807",
    "267-268-269-270-271-274-275-276-278-279-280",
    "2750-2819-2835-2838-2840-2842-2844-2853-2854-2855-2856-2857-2858-2859-2861-2862-2863-2864-2865-2866-2867-2868-2870-2872-2875-2877-2879-2882-2885-2888-2891-2892-2894-2895-2896-2897-2898-2901-2903",
    "2773-2776-2779-2804-2808-2810-2814-2816-2817-2820-2822-2823-2824-2825-2826-2827-2828-2829-2830-2831-2832-2833-2834",
    "2798-2809",
    "2821-2927-2956-2959-2961-2964-2967-2969-2970-2972-2973-2974-2976-2977-2979-2980-2982-2983-2984-2985-2986-2987-2990-2991-2992-2993-2994-2995-2996-2997-2998-3000-3001-3002-3003-3004-3005-3006-3007",
    "284-286-291-297-304-306-307-310-312",
    "2873-2889-2900-2904-2907-2908-2911-2914-2915-2921-2923-2924-2929-2930-2931-2932-2933-2934-2935-2936-2937-2940-2942-2943-2944-2946-2947-2949-2952-2953-2954",
    "2881-3014-3025-3028-3029-3033-3038-3039-3040-3041-3042-3044-3050-3053-3058-3059-3060-3061-3069-3070-3071-3073-3074-3078-3080-3081-3082-3090",
    "3010-3015-3016-3017-3020-3021-3022",
    "3011-3084-3091-3096-3097-3101-3102-3106-3107-3108-3113-3115-3117-3118-3119-3125-3136-3143-3144-3145-3146-3147-3148",
    "3045-3205-3216-3225-3233-3255-3257-3261-3262-3263-3267-3268-3271-3276-3277-3279-3283-3284-3285-3286-3288-3289-3291-3292-3296-3297-3298-3300-3305-3307-3308-3310-3313-3316-3317-3318-3319-3320-3322-3323-3324-3325-3328-3329-3330-3334-3335-3338-3343-3345-3346-3351-3354-3356-3357-3360-3363-3364-3366-3367-3369-3370-3371-3372-3373-3374-3376-3377-3379-3380-3385-3386-3390-3391-3392-3394-3396-3397-3398-3399-3400-3403-3404-3410-3412-3413-3414-3418-3420-3422-3424-3425-3426-3427-3428-3430-3431-3433-3434-3435-3436-3437-3438-3439-3440",
    "3052-3344-3406-3408-3429-3495-3498-3500-3510-3516-3518-3519-3520-3521-3523-3525-3526-3527-3529-3531-3533-3534-3536-3537-3541-3546-3550-3554-3555-3559-3560-3562-3565-3567-3568-3571-3574-3575-3580-3581-3582-3583-3585-3587-3590-3592-3594-3595-3599-3600-3601-3604-3605-3606",
    "3072-3079-3120-3130-3132-3151-3152-3153-3155-3156-3162-3166-3168-3171-3176-3177-3180-3181-3183-3184-3185-3186-3187-3188-3190-3192-3195-3196-3198-3200-3201",
    "3085-3175-3189-3197-3202-3206-3207-3208-3209-3210-3212-3214-3215-3219-3224-3226-3227-3228-3230-3231-3232-3234-3235-3236-3239-3240-3241-3245-3246-3247-3248-3249-3250-3251-3252-3253",
    "3134-3355-3542-3584-3593-3598-3608-3610-3611-3612-3614-3616-3617-3619-3621-3623-3628-3629-3630-3635-3636-3637-3640-3644-3649-3650-3652-3653-3654-3656-3657-3658-3659-3660-3663-3664-3666-3667-3668-3671-3672-3675-3676-3679-3682-3683-3687-3689-3690-3691-3692-3693-3694-3695-3697-3700-3701-3702-3704-3707",
    "3169-3287-3349-3444-3446-3447-3453-3454-3455-3460-3461-3462-3466-3468-3470-3471-3472-3474-3476-3479-3480-3482-3483-3484-3486-3488-3489-3490-3491-3492-3493-3494-3499-3501-3503-3504-3508-3509-3511-3512",
    "3449-4029-4073-4074-4075-4082-4083-4085-4087-4088-4090-4091-4093-4097-4104-4109-4116",
    "3457-3991-3996-3997-3999-4000-4001-4002-4003-4004-4005-4006-4007-4009-4010-4012-4023-4024-4025-4026-4035-4037-4042-4044-4046-4047-4048-4050-4056-4057-4058-4062-4063-4064-4066-4071-4079-4080",
    "3615-3625-3642-3684-3705-3706-3708-3709-3713-3717-3718-3719-3720-3721-3728-3729-3730-3732-3733-3735-3736-3738-3739-3741-3744-3747-3748-3759-3761-3763-3765-3768-3772-3773-3774-3776-3779-3782-3784-3785-3787-3788-3789-3792-3793",
    "3696-3714-3764-3786-3794-3795-3803-3805-3806-3807-3809-3811-3812-3813-3814-3815-3816-3817-3819-3820-3821-3822-3825-3827-3828-3830-3833-3834-3836-3837-3838-3841-3842-3845-3850-3852-3855-3856-3857-3863-3864-3865-3866-3868-3871-3873-3874-3875-3878-3879-3881-3883-3885-3886-3888-3891-3893-3895-3896-3898-3900-3902-3905-3906",
    "3797-3984-4034-4067-4140-4141-4162-4187-4189-4191-4192-4194-4198-4201-4202-4203-4204-4205-4206-4207-4208-4209-4211-4213-4214-4215-4216-4217-4218-4219-4220-4224-4225-4228-4229-4230-4231-4232-4233-4234-4235-4236-4237-4238-4239-4240-4241-4242-4244-4245-4247-4248-4249-4251-4255-4265-4267-4268-4271-4272-4273-4278-4280-4282-4283-4293-4294-4295-4297",
    "3804-3831-3848-3870-3877-3889-3899-3907-3908-3909-3914-3915-3916-3917-3918-3919-3920-3926-3927-3928-3930-3931-3932-3933-3936-3937-3938-3939-3940-3941-3942-3943-3944-3946-3947-3951-3952-3953-3954-3956-3957-3958-3959-3960-3961-3962-3963-3964-3965-3966-3967-3968-3970-3973-3974-3976-3978-3979-3980-3982-3983",
    "391-421-422-425-433-435-436-437-444-445-448-449-454-464-465-466-467-469-470-474-475-478-479-480-484-485-486-490-491-492-494-495-496-497-499-500-503-504-505-506-509-510-514-515-516-517-520-522-524-526-527-528-529-530-531-534-537-538-539-540",
    "3981-4013-4022-4028-4031-4060-4078-4084-4089-4092-4113-4115-4117-4119-4120-4122-4125-4126-4127-4128-4130-4135-4136-4137-4142-4143-4145-4146-4147-4148-4149-4150-4151-4152-4153-4154-4156-4158-4161-4164-4166-4167-4171-4172-4173-4174-4175-4176-4177-4179-4183-4184-4185-4186",
    "4015-4252-4253-4269-4270-4301-4304-4311-4312-4313-4314-4315-4319-4320-4321-4322-4323-4324-4325-4326-4327-4329-4330-4334-4335-4336-4337-4338-4340-4341-4342-4344-4346-4347-4348-4349-4350-4352-4354-4355-4358-4359-4360-4361-4362-4368-4370-4371-4372-4374-4376-4377-4379-4381",
    "4378-4382-4384-4385-4386-4390-4393-4395-4396-4397-4400-4401-4404-4405-4407-4414-4415-4416-4417-4419-4422-4424-4426-4428-4429-4430-4431-4433-4434-4435-4439-4440-4441-4443-4446-4447-4448-4449-4450-4452-4460-4461-4462-4464-4465-4467-4469-4470-4471-4472-4473-4474-4475-4477-4482-4495-4497-4498",
    "60-61-62-64-65-66-67-68-69-70-73-78-79-80-81-82-84-85-86-88-89-90-91-92-93-94-95-97-98-101-102-103",
    "742-848-921-924-926-927-929-931-934-935-938-939-943-947-949-952-954-955-956-959-960-961-962-963-965-966-967-968-969-970-971-972-973-974-982-987-988-990-991-992-994-995-996-997-999-1002-1004-1005-1009-1010-1011-1012-1016-1017-1018-1019-1020-1021-1022-1023-1024-1029-1032-1033-1034-1035-1037-1039",
    "975-998-1030-1041-1044-1046-1048-1050-1051-1057-1058-1060-1061-1065-1066-1068-1071-1075-1077-1078-1079-1081-1082-1085-1086-1090-1093-1094-1095-1097",
]

class Pipecat(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PipecatImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest -v / -rA output, one test per line:
        #   tests/test_foo.py::test_bar PASSED                    [ 12%]
        #   tests/test_foo.py::TestX::test_y FAILED               [ 30%]
        #   tests/test_foo.py::test_z SKIPPED (reason)            [ 45%]
        # The -rA summary repeats these as "PASSED tests/..::.." — both forms
        # are matched; set semantics dedupe.
        line_re = re.compile(
            r"^(?:(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"|(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+))\b"
        )

        for line in clean.splitlines():
            line = line.strip()
            m = line_re.match(line)
            if not m:
                continue
            name = m.group(1) or m.group(4)
            status = m.group(2) or m.group(3)
            if not name:
                continue
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # Disjoint sets: failed > skipped > passed.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


for _ni in _BUNDLE_NIS:
    Instance._registry[f"pipecat-ai/{_ni}"] = Pipecat
