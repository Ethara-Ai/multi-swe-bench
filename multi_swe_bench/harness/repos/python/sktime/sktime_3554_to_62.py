import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v output anchored on the trailing `<STATUS> [ NN%]` so
    parametrized node ids with internal spaces/brackets are captured whole."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_line = re.compile(
        r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
    )

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).strip()
        m = re_line.match(line)
        if not m:
            continue
        nodeid, status = m.group(1).strip(), m.group(2)
        if status in ("PASSED", "XPASS"):
            passed_tests.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(nodeid)
        else:
            skipped_tests.add(nodeid)

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


class SktimePy39ImageBase(Image):
    """sktime era 1 (PRs 62-3554 with requires-python `<3.10`/`<3.11` or
    early/unspecified; releases 0.2->0.16, 2019-2023). Python 3.9 covers
    `<3.10` and `<3.11` constraints and runs early `classifier:3.6/3.7`
    code without forced toolchain. Routing is by python_requires at the
    PR's base SHA, not PR# (sktime maintains parallel release branches
    with backports — PR# is non-monotonic with python_requires)."""

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
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return "base-py39"

    def workdir(self) -> str:
        return "base-py39"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class SktimePy39ImageDefault(Image):
    """Per-PR image: checkout base commit, install sktime + dev extras
    (pytest comes from [dev] in all sktime versions), run targeted pytest."""

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
        return SktimePy39ImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
# Pre-install build deps required by old sktime setup.py before metadata resolution.
# Very old PRs (<=~1600) use setup.py that imports numpy/cython at metadata time.
pip install --no-cache-dir numpy cython 2>&1 | tail -3 || true
# Install with extras priority: [dev] (pytest in all eras) → [tests] → bare.
# Wrap in timeout 600 per [[wrap-install-in-timeout]] — pip resolver hangs are real.
timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -5 \\
    || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -5 \\
    || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -5 || true
# For very old sktime (0.4-0.7, ~2020): setup.py uses .* version specifiers that
# modern pip rejects, so [dev] silently fails leaving pandas/sklearn uninstalled.
# Only install historical pins if pandas is missing — safe for newer era1 PRs.
python -c "import pandas" 2>/dev/null || \\
    pip install --no-cache-dir 'numpy<1.24' 'pandas<2' 'scikit-learn<1.1' \\
        'scipy<1.10' 'statsmodels' 2>&1 | tail -3 || true
# Ensure pytest is present. Use >/dev/null not | head -1: pipe exits 0 even on failure.
python -m pytest --version >/dev/null 2>&1 || pip install --no-cache-dir pytest pytest-xdist || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# Pytest files the PR's test patch touches under sktime/. The grep is anchored
# on `sktime/.+_test\\.py` and `sktime/.+/tests/.+\\.py` to match sktime's
# two test conventions; skip __init__.py.
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.npy' --exclude='*.npz' --exclude='*.parquet' --exclude='*.pkl' \\
    --exclude='*.joblib' --exclude='*.h5' --exclude='*.hdf5' --exclude='*.arff' \\
    --exclude='*.tsv' --exclude='*.tsf' --exclude='*.tar.gz' --exclude='*.xlsx' \\
    --exclude='*.mat' --exclude='*.xls' --exclude='*.nc')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
# Reinstall if patch touches deps — 80/81 sktime PRs do this.
if grep -qE '^diff --git a/(setup\\.py|pyproject\\.toml|setup\\.cfg|requirements)' /home/test.patch 2>/dev/null; then
    timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -3 || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.npy' --exclude='*.npz' --exclude='*.parquet' --exclude='*.pkl' \\
    --exclude='*.joblib' --exclude='*.h5' --exclude='*.hdf5' --exclude='*.arff' \\
    --exclude='*.tsv' --exclude='*.tsf' --exclude='*.tar.gz' --exclude='*.xlsx' \\
    --exclude='*.mat' --exclude='*.xls' --exclude='*.nc')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(setup\\.py|pyproject\\.toml|setup\\.cfg|requirements)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -3 || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{self.pr.repo}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=$BASE_COMMIT

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
"""


class SKTIME_3554_TO_62(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SktimePy39ImageDefault(self.pr, self._config)

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
        return parse_pytest_log(log)


_BUNDLE_NIS_ERA1 = [
    "62-71-72-73-76-85-89-90-94-95-99-100-106-108-110-111-112-114-115-116-117-119",
    "125-175-181-183-185-187",
    "190-192-206-213-215-236-267-278-281-282",
    "285-286-293-295-301-330",
    "324-364-369-371-374-379-380-382-383-385-387-389-391-393-394-395-399-400-404",
    "333-420-437-438-439-442-444-445-446-453-454-457-458-463-467-468-469-471-472-473-475-482-485-486-487-489-495-496-497-500-502-505-506-527-533-536-537-538-547-548",
    "392-398-405-408-410-414-416-418-422-428-430",
    "492-546-553-595-614-615-627-634-635-636-637-638-642-643-658",
    "509-515-542-554-555-556-559-581",
    "579-582-593-599-606-610-613",
    "657-659-660-667-676-678-685-686-688-689-690-693-695-697-698-701-705-708-715-734-735-737-739-749-751-756-757-762-766-771-772-774-777-793-794-808-810-811-812",
    "714-730-752-819-827-850-858-861-864-872-873-875-885-887-889-892-900-902-911-912-914-915-923-941-942-953-967-970-972-977-989-998-999-1003-1004-1005-1013-1015-1016-1017-1019-1021-1026-1029-1031-1034-1035-1037-1042-1049-1051-1053-1067-1068-1069-1071-1075-1077-1088-1089-1094-1100-1108-1109-1112-1118-1124-1128",
    "733-845-945-975-980-1024-1044-1061-1078-1082-1084-1091-1103-1127-1130-1134-1135-1137-1138-1139-1140-1145-1149-1151-1155-1156-1164-1165-1166-1169-1170-1172-1173-1179-1180-1187-1190-1191-1193-1195-1196-1197-1200-1201-1205-1209-1213-1220-1221-1225-1226-1232-1236-1239-1242-1243-1248-1253-1255-1256-1258-1259-1260-1262-1266-1269-1277-1278-1282-1283-1285-1286-1295-1297-1301-1305-1306-1308-1309-1310-1311-1312-1314-1315-1320-1322-1324-1326-1328-1330-1333-1335-1336-1337-1339-1340-1343-1347-1349-1357-1360-1361-1368-1378-1382-1391-1392-1394-1396-1398-1400-1401-1406-1410-1416-1428-1431",
    "769-779-788-791-796-801-815-825-828-829-830-831-834-835-848-851-870",
    "800-1264-1365-1470-1527-1535-1545-1548-1559-1561-1562-1566-1567-1571-1572-1573-1574-1582-1583-1584-1588-1595-1599-1600-1602-1604-1610-1611-1615-1619-1623-1633-1636-1637-1638-1640-1641-1644-1648-1650-1653-1656-1661-1663-1664-1665-1666-1669-1670-1671-1680-1681-1682-1691-1692-1695-1698-1699-1704-1706-1711",
    "890-1110-1229-1329-1352-1356-1358-1359-1370-1376-1379-1395-1403-1407-1409-1421-1429-1436-1437-1442-1444-1445-1449-1450-1453-1455-1456-1457-1461-1463-1464-1465-1472-1473-1475-1477-1479-1487-1489-1490-1491-1493-1498-1504-1506-1511-1517-1525-1531-1532-1541-1544-1552-1553-1554-1557",
    "1284-1822-2112-2268-2389-2392-2394-2410-2411-2414-2422-2429-2439-2440-2449-2450-2454-2456-2457-2458-2462-2463-2466-2468-2470-2474-2476-2479-2487-2489-2491-2492-2494-2496-2497-2503-2505-2506-2508-2512-2513-2516-2517-2519-2520-2521-2522-2523-2525-2529-2531-2532-2533-2534-2536-2538-2539-2540-2541-2543-2548-2549-2557-2561-2563-2567-2579-2580-2583",
    "1300-3021-3095-3279-3333-3336-3378-3380-3386-3399-3403-3411-3425-3436-3442-3445-3451-3460-3475-3478-3481-3484-3485-3486-3495-3503-3504-3505-3506-3511-3513-3514-3515-3518-3519-3523-3527-3532-3535-3541-3542-3544-3546-3548-3549-3552-3553-3555-3556-3557-3561-3562-3563-3564-3566-3569-3575-3576-3577-3578-3579-3581-3585-3587-3589-3590-3591-3593-3594-3595-3598-3599-3602-3603-3607-3610-3618-3622-3623-3624-3627-3628-3632-3635-3636-3637-3639-3642-3643-3645-3652-3654-3673-3676-3677-3679-3684-3690-3699-3706-3709-3710-3713-3718",
    "1579-1594-1620-1630-1660-1672-1677-1689-1702-1703-1707-1709-1721-1724-1726-1729-1730-1732-1734-1743-1745-1748-1752-1754-1758-1761-1764-1768-1770-1774-1775-1777-1780-1784-1785-1786-1789-1790-1792-1793-1795-1796-1799-1800-1804-1805-1806-1807-1811-1813-1816-1819-1820-1823-1829-1830-1833-1836-1838-1839-1840-1841-1842-1844-1846-1847-1848-1849-1851-1852-1853-1855-1858-1859-1863-1864-1869-1872-1874-1879-1885-1892-1895-1897-1901-1903-1907-1910-1911-1913-1920-1921-1922-1926-1927-1932-1944-1953-1958-1959-1961-1964-1965-1966-1969-1970",
    "1705-3382-3410-3431-3606-3653-3689-3708-3723-3724-3727-3728-3733-3735-3736-3737-3739-3740-3741-3742-3744-3746-3747-3750-3751-3754-3756-3759-3760-3761-3762-3767-3768-3770-3771-3775-3781-3785-3786-3792-3796-3797-3799-3805-3808-3809-3810-3812-3813-3817-3820-3821-3837-3839-3845-3851-3855-3858-3859",
    "1771-1772-1810-1818-1854-1865-1929-1931-1936-1962-1968-1993-2041-2048-2051-2060-2065-2069-2090-2092-2093-2094-2100-2104-2107-2108-2110-2114-2115-2119-2121-2122-2124-2130-2131-2135-2139-2142-2144-2146-2147-2154-2156-2160-2161-2162-2164-2165-2166-2167-2168-2170-2180-2182-2186-2187-2188-2189-2190-2191-2193-2196-2197-2199-2205-2208-2209-2210-2216-2217-2219-2220-2222-2225-2226-2227-2229-2230-2231-2232-2236-2239-2241-2244-2246-2250-2251-2257-2260-2262-2271-2272-2273-2276-2277-2279-2281-2284-2285-2286-2288-2293-2296-2299-2306",
    "1834-1902-1924-1934-1963-1972-1973-1974-1977-1978-1980-1981-1995-1996-1997-2000-2004-2014-2020-2025-2027-2029-2034-2035-2042-2045-2047-2050-2063-2076-2083-2086-2096-2097-2098-2099",
    "1940-2744-3158-3216-3232-3233-3242-3255-3302-3305-3307-3308-3312-3327-3331-3334-3338-3339-3340-3341-3342-3343-3346-3349-3350-3352-3354-3355-3356-3357-3358-3360-3362-3366-3367-3373-3374-3376-3377-3379-3381-3383-3391-3393-3395-3396-3400-3401-3405-3407-3408-3409-3414-3415-3416-3418-3428-3435-3440-3449-3455-3456-3457-3458-3463-3466-3467-3482",
    "1998-2379-2486-2544-2601-2833-2855-2896-2902-2907-2922-2925-2928-2951-2954-2955-2969-2971-2975-2976-2988-2990-2991-2995-2996-3001-3002-3007-3008-3010-3015-3016-3017-3019-3029-3030-3031-3036-3038-3039-3040-3041-3042-3043-3048-3049-3054-3055-3059-3060-3066-3067-3068-3070-3074-3075-3076-3077-3081-3085-3086-3087-3089-3091-3092-3093-3094-3098-3102-3104-3105-3106-3107-3109-3111-3112-3116-3121-3122-3123-3126-3129-3130-3133-3134-3135-3136-3137-3139-3140-3141-3143-3145-3146-3147-3149-3152-3157-3160-3167-3168-3170-3173-3174-3178-3187-3195-3196-3198-3200-3203-3204-3207-3208-3215-3222-3223-3225-3227-3228-3229-3236-3239-3240-3241",
    "2103-2223-2234-2252-2253-2259-2287-2292-2295-2298-2303-2304-2305-2310-2311-2314-2316-2318-2320-2322-2324-2325-2326-2328-2330-2333-2335-2339-2342-2343-2348-2353-2355-2356-2358-2359-2362-2363-2364-2365-2366-2367-2369-2372-2373-2378-2380-2384-2390-2393-2395-2396-2398-2400-2401-2404-2405-2406-2408-2418-2420-2423-2425-2426-2428",
    "2235-2412-2551-2553-2558-2572-2577-2582-2592-2593-2595-2596-2597-2605-2606-2612-2613-2616-2619-2627-2630-2632-2633-2636",
    "2248-2375-2581-2660-2661-2752-2763-2783-2786-2794-2800-2803-2809-2810-2815-2818-2824-2829-2830-2831-2832-2835-2840-2842-2843-2844-2845-2847-2850-2852-2857-2858-2859-2861-2863-2864-2865-2866-2867-2870-2873-2874-2876-2878-2882-2883-2887-2892-2895-2899-2900-2906-2908-2909-2911-2912-2915-2917-2920-2927-2932-2936-2937-2939-2940-2945-2958-2959-2960-2965-2970-2972-2973-2974-2979-2980-2984-2985-2992-2994-2999-3000-3004-3005-3006",
    "2370-2964-3114-3132-3155-3209-3217-3226-3231-3243-3245-3248-3249-3250-3251-3252-3254-3256-3257-3260-3261-3262-3263-3264-3266-3268-3270-3271-3272-3273-3274-3276-3287-3290-3297-3301-3303-3304-3309-3310-3317-3321-3322-3323-3324-3325-3326-3330",
    "2382-2383-2447-2500-2502-2518-2535-2542-2545-2546-2562-2565-2575-2589-2599-2607-2609-2611-2620-2623-2638-2639-2641-2642-2643-2644-2647-2648-2664-2666-2667-2671-2673-2674-2675-2676-2677-2679-2682-2683-2684-2686-2687-2688-2690-2691-2693-2699-2701-2703-2706-2707-2708-2709-2718-2719-2720-2721-2722-2723-2725-2726-2727-2731-2732-2734-2736-2737-2738-2740-2743-2747-2749-2755-2756-2760-2761-2769-2771-2773-2776-2778-2779-2780-2781-2782-2784-2790-2793-2795-2797-2798-2801-2802-2808",
    "2926-3480-3492",
    "2977-3631-3960-4000-4028-4150-4156-4157-4161-4171-4176-4177-4178-4180-4183-4192-4193-4194-4195-4196-4198-4199-4200-4202-4205-4206-4207-4210-4222-4226-4227-4230",
    "3171-3430-3714-3729-3745-3825-3844-3846-3857-3860-3862-3863-3864-3866-3867-3871-3872-3878-3881-3887-3908-3912-3914-3915-3917-3918-3919-3921-3922-3923-3924-3926-3927-3931-3936-3938-3945-3951-3952-3953-3964-3968-3974-3975-3977-3979",
    "3516-3678-3807-3905-4008-4036-4079-4095-4099-4100-4102-4105-4108-4109-4110-4111-4113-4115-4116-4120-4121-4122-4124-4125-4126-4127-4130-4132-4133-4137-4138-4141-4164-4166-4167-4168",
    "3554-3674-3688-3707-3840-3852-3883-3892-3925-3933-3935-3942-3948-3949-3950-3955-3956-3957-3958-3967-3969-3970-3971-3972-3980-3985-3987-3991-3995-3996-3997-3998-4001-4002-4003-4004-4006-4007-4010-4011-4014-4015-4018-4019-4020-4021-4022-4025-4027-4029-4030-4031-4032-4034-4035-4037-4038-4040-4041-4043-4044-4047-4052-4053-4057-4061-4063-4074-4075-4083-4085-4087-4093-4096",
]
for _ni in _BUNDLE_NIS_ERA1:
    Instance._registry[f"sktime/{_ni}"] = SKTIME_3554_TO_62
