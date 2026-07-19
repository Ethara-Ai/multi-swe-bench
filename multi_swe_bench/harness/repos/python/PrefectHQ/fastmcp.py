from __future__ import annotations

"""PrefectHQ/fastmcp registry config (single era).

Verified in Docker against the oldest (pr-1, sha 627cf8a8) and newest
(pr-4096, sha ee48a0fd) base commits:
  * package manager : uv (astral) -- `pyproject.toml` + `uv.lock` present at
    every base sha across the whole PR range (1..4096); no `setup.py` era.
  * requires-python : >=3.10 (uniform across the range) -> python:3.11-slim.
  * test framework  : pytest + pytest-asyncio, run as `uv run pytest tests`.
    Smoke: 56 tests collected at pr-1 (2 passed on a live slice), 5697 at
    pr-4096 -- both from a clean `uv sync --frozen`.

Single era: one shared base + one Instance class registered under the bare
`PrefectHQ/fastmcp` key (the dataset's number_interval is empty on all 61
records, so routing falls back to `{org}/{repo}`).
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FastmcpImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "python:3.11-slim"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the shared base to a single
        # PR's base.sha and breaking every other PR with "reference is not a
        # tree". The base keeps full history; the strict anti-reward-hack
        # hardening runs per-PR (see FastmcpImageDefault).
        return f"""# syntax=docker/dockerfile:1.6
FROM python:3.11-slim

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    UV_LINK_MODE=copy

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

# uv pinned (not `latest`): the resolver version decides the dependency set, so
# an unpinned uv makes rebuilds non-reproducible. Verified with uv 0.11.29.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""

# Warm the uv cache at the default branch so each PR layer's `uv sync` only
# resolves the delta. Best-effort: the per-PR sync in prepare.sh is what counts.
RUN uv sync --frozen 2>/dev/null || uv sync 2>/dev/null || true

WORKDIR /home/

CMD ["/bin/bash"]
"""


class FastmcpImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return FastmcpImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
git config --global --add safe.directory '*'
cd /home/{self.pr.repo}
git reset --hard
git clean -fdx -e .venv
git checkout {self.pr.base.sha}
uv sync --frozen 2>/dev/null || uv sync 2>/dev/null || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
uv run --frozen pytest tests -v -p no:cacheprovider 2>&1 \\
  || uv run pytest tests -v -p no:cacheprovider 2>&1
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch || true
uv sync --frozen 2>/dev/null || uv sync 2>/dev/null || true
uv run --frozen pytest tests -v -p no:cacheprovider 2>&1 \\
  || uv run pytest tests -v -p no:cacheprovider 2>&1
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch \\
  || git apply --whitespace=nowarn --3way /home/fix.patch || true
uv sync --frozen 2>/dev/null || uv sync 2>/dev/null || true
uv run --frozen pytest tests -v -p no:cacheprovider 2>&1 \\
  || uv run pytest tests -v -p no:cacheprovider 2>&1
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {dep.image_name()}:{dep.image_tag()}
{self.global_env}
COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("PrefectHQ", "fastmcp")
class Fastmcp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FastmcpImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        # Strip ANSI escapes defensively.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest -v verbose lines, e.g.:
        #   tests/test_x.py::TestC::test_y PASSED   [ 12%]
        #   tests/test_x.py::test_z FAILED          [ 13%]
        #   tests/test_x.py::test_w SKIPPED (reason) [ 14%]
        re_line = re.compile(
            r"^(tests/\S+(?:::\S+)?)\s+(PASSED|FAILED|ERROR|SKIPPED)\b"
        )
        # pytest short-summary lines: "PASSED tests/...", "FAILED tests/... - msg"
        re_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(tests/\S+(?:::\S+)?)"
        )

        for raw in log.splitlines():
            line = raw.strip()
            m = re_line.match(line)
            if m:
                name, status = m.group(1), m.group(2)
            else:
                m = re_summary.match(line)
                if not m:
                    continue
                status, name = m.group(1), m.group(2)

            if status == "PASSED":
                if name not in failed_tests:
                    passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "SKIPPED":
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval bundle routing
# ---------------------------------------------------------------------------
# The raw dataset leaves `number_interval` empty, so at generation time every
# record routes to the bare `PrefectHQ/fastmcp` key above. The delivery step
# stamps `number_interval = "-".join(prs_in_bundle)` on each record, so the
# loader then looks up `PrefectHQ/<dash-joined-bundle>`. Register the same
# Fastmcp class under every bundle key so delivered records resolve. Split
# purely for bookkeeping (resolved vs unresolved) -- functionally identical.
_BUNDLE_NIS_FASTMCP = [
    "77-79-99-100-105-107-108-110-111-112-113-116-117-118",
    "115-136-137-138-140-142-143-144-145-147",
    "149-153-154-155-156",
    "177-200-203-206-214-215-216-217-218",
    "220-222-228-231-232-233",
    "237-260-264-278-279-283-284-285-286-287",
    "242-243-246-248",
    "249-252-253-254-255-256",
    "257-258-261-262-263",
    "290-291-293-294-298-299-300-301-302-303-308-309",
    "306-338-341-342",
    "310-312-314-315-316-317-323-325",
    "327-328-329-336-337",
    "377-379-384-385-387-388-390",
    "394-398-401-402",
    "403-404-405",
    "408-413-424-427-432-434-437-440-447-448-449-450-452-454-455-456",
    "425-458-460-468-471-473-475-476-477-479-483-484-486-489-490-491-492-502-504-509-512-513-514-516",
    "597-605-607-609-610-615-620-623-624-625",
    "731-734-737-739-747-749-751-752-753-754",
    "794-800-802-803-804-806-808-809-810-819-820-821-833-835-836",
    "888-966",
    "927-929-935-938-939-947-949-952-953-954-957",
    "1009-1011",
    "1017-1018-1022-1027-1028-1030-1031-1033-1034-1035-1038-1041-1042-1045",
    "1103-1105-1106-1107-1108-1112-1119-1122-1123-1124-1125-1126",
    "1109-1127-1128-1129-1131-1135-1141-1144-1147-1148-1149-1153-1164-1165-1171-1178-1182-1183-1185-1186-1187-1188",
    "1336-1337-1338-1342-1344-1346-1357-1360",
    "1351-1375-1380-1382-1383-1384",
    "1371-1387-1389-1395-1397-1399-1403-1404-1405-1406-1410-1412-1414-1415-1416-1417-1418-1419-1421-1422-1423-1425-1426-1428-1435-1443-1446-1448-1452",
    "1433-1648-1669-1691-1701-1702-1703-1706-1710-1714-1717-1719-1720-1722-1723-1724-1728-1732-1733-1734-1736-1738",
    "1704-1709-1742-1744-1745-1759-1767-1769-1770-1771-1773-1775-1780-1792-1804-1810",
    "1740-1743",
    "2365-2392-2413-2428-2432-2433-2437-2438-2439-2440-2442-2446-2462-2474-2483-2484-2491-2493-2497-2502-2505-2506-2508-2509",
    "2551-2592-2598-2599-2600-2603-2605-2606-2607-2608-2609-2612-2614-2615-2616-2617-2618-2619-2620-2621",
    "2696-2697-2700-2705-2709-2713-2724-2727-2760-2763-2765-2769-2774-2782-2785-2787",
    "2851-2861-2874-2989",
    "3175-3259-3262-3264-3267-3272",
    "3710-3712-3722-3724-3725-3727-3728-3736-3741-3742-3750-3753-3754-3755-3756-3757-3762-3767-3768-3770-3772-3773-3775-3776-3778-3781-3784-3785-3786-3787-3788-3790-3791",
    "3795-3797",
]

_BUNDLE_NIS_FASTMCP_UNRESOLVED = [
    "1-2-3-4-5-6-7-8-9-11-12-13-14-15-16-17-18-19-20-21",
    "27-28",
    "29-32-34",
    "31-42-43-44-46",
    "47-48-49-51-52-54",
    "56-57-63-67",
    "119-121-122-123-124-125-128-129-161-164-165-168-169-170-171-173-176-181-182-184-185-304-343-347-349-350-351-356-357-358-359-361-364-366-367-369-376-478-517-519-520-521-522-523-526-527-534-537-539-542-550-551-554-555-558-563-564-565-566-567-575-576-578-579-626-635-642-643-645-646-647-649-650-652-653-655-657-660-662-663-664-665-666-667-668-669-670-673-674-676-690-697-700-701-702-703-705-706-708-709-710-711-712-713-714-716-718-719-720-723-725-726-727-729-745-748-750-755-756-757-758-759-760-761-763-764-765-766-767-768-769-770-773-776-777-778-779-781-782-783-784-787-788-789-790-791-792-793-838-842-843-845-848-860-861-869-870-880-881-882-887-889-892-893-894-895-896-897-900-901-902-904-906-907-908-910-912-913-915-916-917-918-919-920-921-922-923-924-967-975-976-977-979-981-982-983-984-985-986-992-995-997-998-999-1000-1001-1005-1008-1099-1132-1138-1145-1160-1190-1194-1198-1199-1208-1209-1210-1214-1216-1217-1222-1224-1226-1227-1229-1230-1234-1235-1236-1238-1239-1242-1245-1246-1248-1254-1255-1257-1259-1260-1267-1268-1269-1270-1278-1279-1281-1282-1283-1287-1289-1290-1293-1294-1295-1296-1297-1302-1303-1306-1317-1321-1322-1326-1327-1328-1329-1330-1331-1332-1333-1334-1335-1434-1436-1444-1453-1454-1456-1460-1470-1471-1472-1473-1475-1481-1482-1483-1484-1486-1487-1488-1496-1497-1498-1499-1504-1509-1510-1511-1512-1513-1515-1516-1517-1520-1522-1523-1532-1533-1534-1535-1536-1537-1538-1545-1549-1550-1556-1557-1559-1561-1567-1568-1578-1581-1582-1586-1588-1591-1592-1593-1594-1595-1596-1604-1605-1607-1611-1613-1614-1615-1616-1617-1620-1622-1623-1630-1631-1632-1633-1634-1635-1636-1642-1660-1661-1662-1667-1671-1672-1673-1674-1675-1676-1679-1680-1682-1684",
    "677-679-680-681-684-686-687",
    "1052-1053-1054-1055-1056-1057-1058-1059-1062-1063-1066-1071-1073-1074-1075-1076-1083-1087-1092-1094-1096",
    "1546-1783-1845-1891-1913-1923-1927-1929-1934-1935-1936-1938-1939-1945-1948-1949-1950-1951-1953-1954-1955-1956-1958-1960-1963-1970-1971-1972-1974-1975-1982-1983-1987-1991-1994-1997-1999-2000-2002-2005-2006-2009-2013-2022-2025-2028-2029-2031-2033-2036-2046-2052-2056-2058-2063-2066-2069-2071-2073-2074-2075-2080-2084-2089-2090-2091-2092-2093-2094-2099-2100-2101-2102-2107-2108-2109-2117-2118-2119-2120-2121-2122-2128-2129-2133-2135-2136-2137-2141-2142-2143-2144-2145-2146-2147-2149-2150-2156-2157-2159-2160-2161-2163-2165-2169-2170-2171-2172-2173-2174-2196-2200-2201-2213-2214-2215-2217-2218-2219-2220-2221-2222-2223-2232-2237-2240-2241-2242-2243-2244-2247-2249-2250-2251-2252",
    "1575-1590-1597",
    "1735-1779-1791-1805-1812-1817-1820-1821-1823-1824-1827-1828-1829-1832-1833-1834-1838-1840-1842-1850-1853-1858-1860-1866-1870-1872-1873-1874-1877-1879-1880-1882-1883-1884-1885-1886-1890-1892-1893-1906-1912-1914-1916-1922-1928",
    "1796-2977-3154-3222-3273-3276-3280-3282-3283-3284-3289-3294-3295-3297-3298-3300-3301-3306-3308-3309-3310-3313-3316-3317-3321-3322-3323-3324-3326-3327-3328-3330-3331-3334-3335-3337-3338-3343-3344-3349-3354-3355-3356-3358-3359-3360-3361-3362-3370-3372-3373-3374-3375-3376-3377-3378-3380-3382-3384-3385-3386-3388-3389-3390-3396-3405-3406-3407-3408-3409-3410-3411-3412-3413-3414-3415-3416-3417-3418-3419-3420-3429-3430-3431-3432-3433-3434-3435-3436-3437-3438-3439-3440-3444-3456-3458-3462-3465-3468-3473-3475-3476-3477-3478-3479-3480-3481-3482-3485-3486-3487-3489-3490-3491-3492-3493-3494-3495-3496-3499-3500-3501-3502-3503-3504-3505-3507-3508-3510-3511-3514-3515-3516-3517-3518-3519-3521-3522-3523-3524-3529-3538-3539-3540-3541-3546-3547-3548-3549-3550-3551-3552-3553-3557-3567-3570-3572-3573-3575-3578-3580-3582-3583-3584-3585-3587-3589-3591-3592-3593-3595-3597-3600-3603-3608-3609-3610-3611-3614-3615-3616-3620-3622-3624-3626-3628-3630-3631-3632-3638-3647-3648-3649-3650-3651-3652-3653-3657-3658-3659-3661-3662-3666-3667-3668-3669-3670-3677-3681-3682-3684-3685-3686-3687-3688-3689-3690-3691-3693-3694-3695-3696-3698-3699-3700-3701-3702-3705-3706-3707-3708-3711-3889-3890-3896-3917-3925-3926-3927-3929-3932-3934-3936-3937-3938-3940-3945-3946-3951-3952-3954-3956-3957-3958-3959-3960-3963-3964-3965-3966-3968-3969-3971-3984-3988-3990-3995-4001-4007-4010-4011-4018-4026-4027-4029-4031-4036-4041-4042-4043-4047-4064-4068-4069-4070-4072-4076-4083-4087-4091-4092-4094-4095-4100-4101-4106-4109-4112-4116-4118-4122-4125",
    "1937-3798-3800-3806-3807-3808-3809-3816-3818-3822-3823-3824-3826-3827-3830-3833-3836-3837-3838-3841-3842-3843-3845-3849-3850-3851-3852-3854-3857-3858-3859-3861-3863-3864-3865-3869-3871-3872-3873-3874-3876-3877-3878-3879-3880-3881-3884-3885-3899-3900-3901-3904-3905-3909-3912-3913-3914-3915-3916-3918",
    "2030-2329-2378-2486-2494-2507-2513-2515-2516-2517-2520-2526-2529-2531-2532-2533-2536-2538-2540-2543-2545-2547-2549-2550-2553-2554-2558-2560-2561-2563-2564-2565-2566-2567-2570-2571-2574-2575-2576-2577-2578-2579-2580-2581-2582-2585-2586-2587-2588-2591-2593-2604-2610-2611-2622-2623-2632-2635-2644-2645-2646-2648-2653-2656-2657-2660-2663-2664-2665-2666-2667-2669-2672-2674-2675-2676-2680-2681-2683-2699-2701-2704-2707-2708-2710-2711-2712-2714-2715-2716-2717-2719-2720-2723-2725-2726-2728-2729-2730-2731-2732-2734-2735-2736-2737-2738-2739-2740-2741-2742-2744-2748-2749-2750-2751-2752-2753-2756-2757-2758-2759-2761-2762-2764-2766-2768-2771-2773-2776-2777-2781-2784-2786-2791-2796-2797-2799-2800-2801-2803-2804-2806-2808-2809-2811-2814-2815-2816-2818-2822-2823-2824-2826-2828-2829-2830-2831-2832-2834-2835-2836-2838-2840-2844-2846-2847-2849-2850-2852-2855-2856-2858-2859-2865-2866-2869-2871-2872-2873-2875-2884-2885-2886-2888-2890-2891-2893-2894-2895-2896-2897-2900-2901-2902-2903-2905-2906-2912-2913-2914-2915-2916-2917-2918-2919-2920-2921-2922-2924-2930-2931-2932-2933-2934-2935-2936-2938-2939-2940-2941-2942-2943-2944-2945-2946-2947-2948-2949-2950-2951-2952-2953-2954-2955-2956-2958-2959-2960-2962-2963-2964-2967-2974-2975-2978-2979-2980-2981-2982-2984-2985-2986-2988-2990-2991-2995-2996-2997-2998-2999-3001-3005-3009-3010-3013-3014-3017-3022-3023-3027-3028-3031-3032-3033-3039-3040-3041-3042-3043-3047-3048-3050-3051-3054-3055-3057-3058-3062-3064-3065-3066-3067-3069-3070-3072-3076-3086-3088-3089-3098-3099-3100-3101-3102-3103-3104-3105-3108-3112-3115-3116-3117-3123-3124-3128-3129-3132-3133-3134-3136-3138-3140-3143-3145-3146-3147-3148-3149-3151-3152-3155-3157-3167-3168-3171-3172-3173-3185-3186-3188-3193-3194-3195-3197-3198-3200-3201-3204-3205-3206-3207-3212-3213-3215-3216-3217-3218-3219-3221",
    "2206-2227-2233-2255-2261-2265-2266-2268-2269-2270-2271-2272-2276-2277-2279-2282-2283-2288-2291-2294-2295-2296-2305-2306-2307-2308-2309-2311-2319-2323-2324-2331-2344-2347-2348-2349-2350-2353-2354-2355-2357-2361-2367-2368-2369-2370-2376-2381-2382-2383-2387-2389-2390-2396-2398-2399-2400-2405-2407-2410-2422-2423-2424-2426",
    "2789-2798-2812-2839-2841-2843-2848",
    "2992-3063",
    "3109-3111-3170",
    "3223-3224-3225-3226-3227-3231-3234-3235-3236-3237-3243-3244-3245-3248-3249-3250-3253-3254-3257-3258",
    "4096-4139-4150",
]

for _ni in _BUNDLE_NIS_FASTMCP:
    Instance.register("PrefectHQ", _ni)(Fastmcp)

for _ni in _BUNDLE_NIS_FASTMCP_UNRESOLVED:
    Instance.register("PrefectHQ", _ni)(Fastmcp)
