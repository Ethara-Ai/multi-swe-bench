import re
from typing import Optional, Union

from unidiff import PatchSet

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# TheAlgorithms/Python ships its tests as doctests embedded in the algorithm
# modules plus a handful of ``test_*.py`` files. Running the whole repo with
# ``pytest --doctest-modules .`` would collect thousands of doctests, many
# needing heavy optional deps, so every run is scoped to the ``.py`` files the
# patch bundle actually touches -- pytest then collects both doctests
# (``--doctest-modules``) and real test functions from them.

# ERA: a SINGLE base (python:3.14) serves all 60 records. Measured requires-python
# across the 60 base.shas: 50 declare none (pure algorithms, any Python 3), 9 need
# >=3.13, 1 (pr-11911) needs >=3.14. python:3.14 satisfies the >=3.14 floor and
# runs the pure-algorithm doctests of the 2017-2024 eras (backward compatible; the
# scoped-file + best-effort-deps + --continue-on-collection-errors design tolerates
# any individual old-module import failure). No per-era bases are needed.
_PYTHON_IMAGE = "python:3.14-bookworm"

_EXCLUDED_BASENAMES = frozenset({
    "conftest.py",
    # Old modules (pre `if __name__` convention) that execute blocking
    # network/socket code at import time. ``--doctest-modules`` imports every
    # module to collect doctests, so collecting these would hang the run.
    "server.py",
    "ftp_send_receive.py",
    "ftp_client_server.py",
})

# ``git apply`` must not abort the whole patch over binary asset diffs (images,
# committed .pyc, .sqlite, notebooks ...) that some bundles carry without full
# index lines. None affect the doctest/pytest run, so they are excluded.
_APPLY_EXCLUDES = " ".join(
    f"--exclude='*.{ext}'"
    for ext in (
        "png", "PNG", "jpg", "JPG", "jpeg", "JPEG", "gif", "GIF", "ico",
        "bmp", "tif", "tiff", "pyc", "pyo", "suo", "sqlite", "sqlite3",
        "db", "zip", "gz", "tar", "so", "pdf", "ipynb", "npy", "npz",
        "pkl", "xlsx", "docx", "class", "jar",
    )
)


def _patch_py_files(patch: str) -> set[str]:
    """``.py`` files referenced by a patch (deleted files skipped)."""
    seen: set[str] = set()
    if not patch:
        return seen
    try:
        patch_set = PatchSet(patch)
    except Exception:
        return seen
    for patched_file in patch_set:
        for path in (patched_file.target_file, patched_file.source_file):
            if not path or path == "/dev/null":
                continue
            if path.startswith(("a/", "b/")):
                path = path[2:]
            if path.endswith(".py") and path.rsplit("/", 1)[-1] not in _EXCLUDED_BASENAMES:
                seen.add(path)
                break
    return seen


def _changed_py_files(test_patch: str, fix_patch: str) -> list[str]:
    """GUARD-SAFE test hosts: ``.py`` files the TEST patch touches, MINUS every
    file the FIX patch also touches.

    report.py's cheating guard (PIPELINE §9) refuses to credit any test whose
    host file the fix patch modifies -- it reads that as the fix doctoring its
    own test. TheAlgorithms embeds its tests (doctests AND inline
    ``def test_*()`` helpers) inside the very implementation modules a fix
    edits, so feeding those modules to pytest poisons the ENTIRE record: 22 of
    60 records were rejected exactly this way in the first full run, including
    ones with hundreds of genuine transitions (pr-2298, pr-2452).

    Restricting the scope to files the fix never touches makes every credited
    test structurally guard-safe, so the guard can no longer fire.
    """
    fix_files = _patch_py_files(fix_patch) | _all_patch_paths(fix_patch)
    return sorted(_patch_py_files(test_patch) - fix_files)


def _all_patch_paths(patch: str) -> set[str]:
    """Every path a patch touches (not just .py) -- the guard compares raw paths."""
    if not patch:
        return set()
    return {
        m.group(2)
        for m in re.finditer(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', patch, re.M)
    }


# Shared pytest invocation. ``-o addopts=''`` neutralises the repo's own
# pyproject/pytest.ini addopts so behaviour is stable across eras;
# ``--doctest-modules`` is added back explicitly. Collection errors (a scoped
# module importing a dependency we did not install) must not abort the run,
# hence ``--continue-on-collection-errors``.
_PYTEST_SNIPPET = """mapfile -t CANDIDATES < /home/test_files.txt
SELECTED=()
for f in "${CANDIDATES[@]}"; do
    [ -n "$f" ] && [ -f "$f" ] && SELECTED+=("$f")
done
if [ ${#SELECTED[@]} -eq 0 ]; then
    echo "No scoped test files present at this revision."
    exit 0
fi
# timeout: wall-clock cap so a blocking import cannot hang the instance.
# --timeout: per-test cap for runaway test bodies.
# --doctest-modules is safe again HERE because _changed_py_files now scopes to
# files the fix patch never touches: any doctest collected from them is
# guard-safe, and doctests are TheAlgorithms' primary test mechanism, so
# excluding them would throw away most of the repo's real signal.
# stdin from /dev/null so a module calling input() at import raises EOFError
# instead of hanging collection (cost a 25-min stall per stage before).
timeout --preserve-status --kill-after=30 600 \\
    python -m pytest --doctest-modules --continue-on-collection-errors \\
    --timeout=120 --timeout-method=signal \\
    -p no:cacheprovider -o addopts='' -rN --tb=short -v "${SELECTED[@]}" < /dev/null || true
"""


class ImageBase(Image):
    """SHARED base, one per repo (single era). Keeps FULL git history so each PR
    layer can `git checkout <its own base.sha>`. Pre-installs pytest + the common
    scientific stack ONCE so the 60 PR images do not each repeat the heavy install."""

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
        return _PYTHON_IMAGE

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

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` directive = the sanctioned enhancer opt-out (PIPELINE §2):
        # without it the enhancer injects `git checkout ${BASE_COMMIT}` + the
        # destructive prune into this SHARED base, pinning it to whichever PR
        # built it first and breaking every other record. The strict per-PR
        # hardening lives in ImageDefault instead.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_ROOT_USER_ACTION=ignore \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

# Shared toolchain + common doctest deps installed ONCE (not per-PR). Best
# effort: whatever fails to resolve on Python 3.14 is retried per-PR by
# prepare.sh, and unmet imports surface as tolerated collection errors.
RUN python -m pip install --no-cache-dir -U pip setuptools wheel \\
    && python -m pip install --no-cache-dir \\
        pytest pytest-cov pytest-timeout \\
        numpy scipy sympy pandas matplotlib scikit-learn \\
        rich pillow requests beautifulsoup4 || true

WORKDIR /home/

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = _changed_py_files(self.pr.test_patch, self.pr.fix_patch)

        # Base already carries pytest + the scientific stack, so prepare.sh only
        # checks out this PR's base.sha and best-effort installs the repo's own
        # era-specific deps (uv 2025+ / requirements.txt 2020-24 / nothing 2017).
        prepare = """#!/bin/bash
set -uo pipefail
cd /home/{repo}
git config --global --add safe.directory /home/{repo} || true
git reset --hard
git checkout {sha}

if [ -f requirements.txt ]; then
    python -m pip install --no-cache-dir -r requirements.txt || true
fi
if [ -f pyproject.toml ] && grep -q 'dependency-groups\\|\\[tool.uv\\]' pyproject.toml; then
    python -m pip install --no-cache-dir uv || true
    uv pip install --system --group test 2>/dev/null || true
fi
""".format(repo=self.pr.repo, sha=self.pr.base.sha)

        run = """#!/bin/bash
cd /home/{repo}
git reset --hard
git clean -fd
{snippet}""".format(repo=self.pr.repo, snippet=_PYTEST_SNIPPET)

        # Resilient apply: clean apply first, then --reject so the .py changes
        # still land even when an unrelated binary/data file would abort the
        # whole patch. Never `exit 1` -- a partial apply still yields signal.
        test_run = """#!/bin/bash
cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn {excl} /home/test.patch \\
  || git apply --whitespace=nowarn {excl} --reject /home/test.patch \\
  || echo "Warning: test.patch did not fully apply"
{snippet}""".format(repo=self.pr.repo, snippet=_PYTEST_SNIPPET, excl=_APPLY_EXCLUDES)

        fix_run = """#!/bin/bash
cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn {excl} /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn {excl} --reject /home/test.patch /home/fix.patch \\
  || echo "Warning: patches did not fully apply"
{snippet}""".format(repo=self.pr.repo, snippet=_PYTEST_SNIPPET, excl=_APPLY_EXCLUDES)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "test_files.txt", "\n".join(test_files) + "\n"),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # Strict per-PR hardening (PIPELINE §2/§4): detach onto the LITERAL
        # base.sha and prune every other ref so the fix cannot be recovered
        # from git history inside the container. (A shared/Image-typed base
        # means the enhancer does NOT auto-inject, so it is applied here.)
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("TheAlgorithms", "Python")
class THEALGORITHMS_PYTHON(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.compile(r"\x1b\[[0-9;]*m").sub("", log)

        # pytest -v line: "<nodeid> <STATUS>  [ NN%]". The nodeid may contain
        # spaces, so the trailing "[ NN%]" is the reliable right anchor.
        line_re = re.compile(
            r"^(.+?)\s+(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED)\s+\[\s*\d+%\]"
        )
        # Modules that fail to import surface only as collection errors.
        collect_re = re.compile(r"ERROR collecting (.+?\.py)\b")

        for line in log.splitlines():
            m = line_re.match(line.rstrip())
            if m:
                node, status = m.group(1).strip(), m.group(2)
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(node)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(node)
                elif status in ("SKIPPED", "XFAIL"):
                    skipped_tests.add(node)
                continue
            cm = collect_re.search(line)
            if cm:
                failed_tests.add(cm.group(1).strip())

        # Disjointness: a node reported both passed and failed counts as failed.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined, PIPELINE §11b) ===
# One key per bundle (60 keys == 60 instances). Data-derived from
# TheAlgorithms__Python_lht_final.jsonl -- regenerate if bundles change.
_BUNDLE_NIS_THEALGORITHMS_PYTHON = [
    "116-123-132-134-135-136-138-139-140-143-146-147-149-150-151-156-157-159-160-161-162-163-164-165-166-168-169-170-173-174-175-176-177-178-179-180-182-183-184-185-186-188-189-190-191-192-193-194-195-196-197-198-199-200-201-203",
    "172-218-220-228-229-230-233-234-235-236-237-238-239",
    "251-252-253-254-255-257-259-260-261-262-263-264-266-267-268-269-270-271-272-273-274-275-276-277-278",
    "296-298-300-301-304-305",
    "322-338-373-386-408-466-602-621-631-632-636-640-642-643-644-645-650-651-658-669-696-699-702-704-706-715-719-720-722-725-726-735",
    "326-396-397-399-425-769-784-797-820-826-830-833-836-841-847-848-849-851-854-855-864-867-872-882-883-889-891-894-895-897-900-902-903",
    "333-346-1200-1202-1203-1205-1206-1207-1214-1235-1244-1248-1250-1254-1257-1258-1259-1260-1261-1263-1266-1267-1270-1277-1278-1279-1281-1287-1291-1292-1293-1294-1298-1300-1302-1303-1304-1308-1309-1310-1313-1316-1324-1330-1331-1332-1333-1338-1341-1350-1351-1352-1354-1355-1358-1359-1360-1363-1364-1367-1374-1378-1384-1386-1387-1389-1391-1392-1393-1394-1396-1399-1401-1403-1404-1405-1406-1412-1414-1415-1416-1419-1420-1421-1422-1424-1429-1430-1432-1448-1453-1455-1457-1461-1467",
    "339-342-347-348-358-359-370-371-376-384-385-388-391-393-395-401-407-412-413-420-431-436-438-439-440-441-446-447-448-451-455-465-468-469-474-475-477-479-482-483-484-487-497-500-506-510-512",
    "364-616-906-1152-1216-1226-1229-1243-1337-1339-1342-1413-1428-1458-1482-1602-1603-1606-1608-1609-1610-1612-1615-1621-1622-1627-1633-1634-1635-1636-1637-1638-1639-1643",
    "435-509-516-517-528-530-535-538-542-544-545-549-552-553-564-565-566-588-591-592-594-597-599-600-601-603-606-607-610-612-615",
    "504-656-724-730-752-763-767-768-774-776-780-787-789-790-791-793-794-796-798-803-804-806-808-809-821-824",
    "508-974-1146-1149-1156-1158-1161-1162-1163-1164-1165-1166-1168-1169-1170-1175-1177-1178-1179-1190-1194-1195",
    "639-818-941-972-1016-1038-1050-1062-1064-1066-1071-1076-1078-1086-1087-1088-1089-1093-1095-1096-1097-1098-1099-1100-1101-1102-1104-1105-1106-1107-1109-1115-1116-1117-1118-1121-1122-1123-1125-1126-1127-1128-1129-1130-1131-1133-1135-1138-1139-1141-1142-1143",
    "673-675-679-680-681-686",
    "844-887-908-909-914-917-918-920-921-922-924-925-926-928-929-930-932-933-934-935-938-939-943-944-947-948-949-953-954-955-960-961-962-964-965-967-968-969-971-975-977-979-980-990-991-993-995-996-997-998-1000-1001-1002-1004-1008-1013-1014-1015-1018-1019-1020-1021-1023-1025-1028-1029-1032-1034-1036-1039-1041-1042-1045-1046-1052-1054-1057-1058-1059-1060",
    "871-876-1382-1426-1440-1445-1466-1475-1476-1477-1478-1488-1491-1493-1499-1500-1501-1503-1506-1507-1509-1513-1517-1518-1519-1523-1524-1525-1526-1534-1535-1536-1537-1542-1543-1544-1545-1548-1549-1550-1551-1553-1556-1557-1558-1559-1560-1563-1564-1568-1569-1570-1571-1572-1573-1574-1575-1576-1577-1578-1581-1584-1588-1589-1590-1592-1593-1594",
    "1353-1642-1648-1650-1652-1653-1654-1657-1659-1660-1663-1664-1666-1667-1670-1674-1675-1676-1679-1683-1684-1685-1687-1690-1692-1693-1698-1701-1704-1708-1710-1711-1713-1717-1718-1719-1725",
    "1715-1754-1822-1829-1841-1853-1855-1856-1857-1858-1861-1866-1867-1869-1870-1872-1873-1875-1876-1877-1880-1885-1886-1887-1896-1897-1902-1905-1906-1908-1911-1914-1916-1920-1921-1923-1924-1925-1929-1930-1931-1934-1935-1936-1939-1943-1945-1950-1957-1958",
    "1721-1722-1723-1726-1733-1734-1739-1740-1742-1744-1745-1746-1749-1751-1752-1756-1757-1759-1763-1764-1775-1779-1781-1782-1783-1784-1785-1786-1787",
    "1812-1889-1913-1959-1960-1961-1962-1966-1972-1974-1975-1976-1982-1984-1985-1986-1991-1996-1997-1999-2001-2002-2007-2008-2010-2012-2013-2015-2017-2018-2020-2022-2024-2025-2026-2032-2033-2035-2037-2041-2047-2048-2051-2054-2061-2064-2065-2066-2072-2073-2081",
    "1888-1990-2062-2188-2190-2197-2199-2209-2211-2218-2221-2223-2229-2232-2233-2234-2237-2238-2241-2242-2243-2244-2245-2246-2248-2249-2256-2259-2261-2262-2271-2280-2281-2284-2287-2293-2301",
    "2057-2067-2075-2076-2079-2080-2082-2084-2087-2090-2091-2093-2094-2096-2097-2098-2099-2100-2104-2106-2107-2110-2111-2113-2114-2116-2119-2120-2122-2123-2124-2125-2126-2130-2132-2135-2138-2140-2141-2142-2145-2146-2148-2150-2151-2152-2154-2155-2156-2158-2159-2160-2161-2164-2166-2167-2168-2170-2175-2178-2179-2181-2182-2183-2184-2185-2192",
    "2174-2207-2219-2291-2302-2305-2307-2309-2310-2317-2318-2319-2321-2323-2325-2327-2329-2330-2331-2334-2339-2340-2342-2343-2344-2345-2346-2347-2348-2349-2350-2351-2352-2354-2356-2357-2362-2366-2367-2371-2372-2375-2378-2389-2393-2396-2399-2400-2404-2410-2413-2414-2415-2416-2420-2421",
    "2298-2335-2337-2386-2417-2418-2419-2422-2427-2431-2433-2435-2439-2440-2442-2443-2445-2447-2448-2449-2450-2451-2453-2455-2463-2464-2467-2468-2469-2470-2471-2472-2473-2474-2475-2476-2477-2478-2481-2483-2487-2492-2493-2494-2496-2498-2501-2503-2505-2506-2507-2509-2511-2512-2515-2516-2522-2523-2524-2532-2626-2632-2659-2678-2702-2742-2756-2765-2768-2785-2792-2874-2875-2882-2885-2887-2891-2896-2898-2900-2901-2903-2917-2924-2931-2934-2940-2945-2949-2957-2958-2962-2973-2976-2978-2981-2982-2985-2992-2998-3001-3018-3020-3023-3046-3047-3050-3062-3065-3073-3076-3087-3094-3123-3124-3147-3159-3173-3177-3182-3185-3188-3210-3211-3215-3227-3228-3233-3235-3238-3241-3253-3255-3262-3266-3270-3273-3280-3281-3283",
    "2359-2436-2486-2557-2627-2682-2684-2880-2948-3041-3070-3113-3132-3149-3350-3420-3534-3692-3811-3860-3862-3874-3877-3880-3884-3889-3893-3902-3903-3904-3906-3908-3911-3915-3917-3922-3925-3926-3934-3949-3961-3964-3970-3972-3976-3977-3979-3987-3988-3992-4016-4017-4024-4025-4031",
    "2452-2454-2461-2561-2563-2571-2598-2628-2743-2779-2916-2944-2946-2954-2979-3016-3029-3035-3072-3075-3078-3101-3109-3110-3115-3122-3125-3129-3141-3144-3206-3212-3219-3242-3256-3259-3264-3284-3285-3286-3297-3300-3306-3318-3319-3343-3344-3378-3380-3405-3408-3410-3437-3447-3449-3454-3468-3469-3501-3513-3518-3522-3528-3554-3599-3616-3619-3620-3625-3681-3683-3690-3691-3698-3700-3704-3706-3710-3730-3754-3756-3768-3799-3813-3817-3829-3835-3848-3855-3863-3864-3866-3870-3879",
    "3013-4033-4035-4037-4038-4050-4051-4053-4055-4056-4065-4066-4074-4108-4113-4121",
    "3805-4068-4080-4118-4119-4205-4214-4216-4220-4221-4224-4232-4233-4236-4243-4244-4247-4261-4268-4271-4273-4275-4276-4277-4278-4283-4285-4286-4289-4290-4292",
    "4267-4350-4453-4464-4474-4483-4485-4486-4487-4488-4499-4506-4507-4512-4521-4527",
    "4280-4501-4510-4524-4528-4531-4544-4550-4552-4553-4556-4557-4558-4568-4572-4575-4576-4579-4581",
    "4293-4295-4296-4297-4298-4304-4305-4306-4307-4308-4309-4314-4315-4317-4319-4320-4326-4333-4334-4336-4357-4359",
    "4382-4530-4605-4620-4665-4709-4718-4748-4749-4752-4760-4763-4779-4807-4808-4844-4857-4868-4869-4881-4927-4928-4972",
    "4631-4747-4782-4791-4793-4806-4814-4824-4853-4855-4856-4867-4897-4949-4950-4988-5019-5022-5038-5044-5091-5113-5156-5165-5166-5171-5173-5182-5183-5199-5220-5223-5224-5225-5230-5240-5241-5246-5251-5258-5274-5289-5290-5311-5326-5331-5334-5337-5357-5362-5363-5373-5378-5379-5385-5388-5409-5419-5429-5430-5433-5439-5443-5447-5466-5474-5475-5477-5480-5489-5490-5491-5493-5496-5503-5512-5516-5517-5518-5519-5530-5532-5533-5543-5544-5551-5555-5556-5558-5560-5565-5566-5569-5570-5571-5572-5573-5575-5576-5577-5579-5583-5584-5585-5587-5589-5590-5591-5592-5593-5597-5598-5600-5604-5607-5608-5613-5614-5615-5618-5621-5626-5629-5633-5634-5635-5638-5640-5641-5649-5652-5658-5677-5696-5698-5701-5703-5704-5705-5708-5710-5725-5730-5731-5734-5736-5738-5739-5742-5744-5745-5746-5747-5749-5750-5751-5753-5754-5757-5759-5760-5761-5763-5765-5768-5770-5772-5773-5775-5781-5782-5789-5792-5794-5795-5798-5799-5808",
    "4849-4878-5257-5333-5453-5464-5552-5803-5817-5882-5949-6005-6040-6097-6112-6113-6122-6126-6127-6141-6153-6154",
    "4992-5548-6044-6194-6228-6230-6233-6236-6240-6245-6246-6250-6259-6263-6265-6267-6269",
    "5786-6321-6356-6677-6846-8004-8053-8604-8605-8610-8693-8753-8775-8787-8843-8851-8899-8907-8919-8930-8932-8936-8942-8949-8957-8958-8959-8960-8961-8962-8963-8964-8967-8968-8970-8985-8987-8988-8998-8999-9005-9006-9007-9009-9013-9023-9027-9042",
    "6017-6061-6165-6169-6183-6190-6201-6219",
    "6025-8626-8738-8752-8761-8773-8786-8801-8802-8803-8808-8813-8817-8825-8827-8828-8832-8833-8836-8837-8838-8842",
    "6255-6273-6385-6400-6441-6442-6452-6467-6503-6504-6569-6583-6591-6606-6607-6616-6625-6627-6628-6632-6642-6682-6731-6735-6742-6743-6745-6771-6782-6788-6807-6830-6840-6864-6871-6877-6879-6909-6912-6918-6940-6965-6983-6995-7001-7003-7019-7034-7037-7040-7044-7054-7056-7057-7060-7062-7063-7064-7065-7066-7080-7081-7085-7086-7099-7105-7106-7107-7116-7128-7132-7133-7141-7143-7152-7162-7167-7171-7183-7189-7191-7196-7197-7198-7205-7212-7222-7234-7235-7262-7271-7277-7317-7319-7338-7339-7340-7347-7349-7354-7355-7357-7368-7387-7390-7394-7398-7403-7405-7406-7409-7417-7429-7438-7446-7449-7451-7455-7486-7488-7499-7504-7507-7509-7522-7526-7533-7534-7538-7547-7550-7556-7558-7564-7566-7575-7585-7586-7587-7588-7589-7593-7595-7596-7604-7607-7608-7610-7614-7620-7666-7672-7673-7674-7683-7694-7696-7706-7710-7729-7733-7737-7744-7745-7748-7749-7757-7759-7761-7765-7778-7790-7794-7819-7821-7840-7843-7844-7845-7848-7850-7866-7867-7869-7881-7896-7898-7901-7905-7906-7913-7914-7920-7921-7932-7934-7936-7939-7945-7947-7948-7949-7952-7953-7959-7966-7975-7976-7977-7978-7979-7980",
    "6275-6279-6294-6298-6300-6303-6319-6323",
    "6282-6872-7967-8045-8154-8158-8160-8163-8165-8166-8168-8175-8177-8178-8179-8184-8294-8546-8551-8570",
    "6602-8603-8714-8903-8906-8938-8996-9001-9020-9046-9055-9056-9057-9062-9067-9068-9069-9076-9078-9083-9097-9108-9148-9161-9162-9165-9170-9177-9178-9180-9182-9187-9203-9208-9228-9229-9237-9272-9278-9288-9323-9324-9325-9351-9358-9363-9374-9386-9426-9431-9442-9446-9469-9471-9475-9477-9480-9482-9505-9513-9516-9525-9543-9576-9580-9581-9650-9651-9652-9654-9656-9666-9667-9668-9695-9707-9712-9717-9727-9748-9753-9760-9765-9769-9775-9782-9783-9794-9799-9800-9823-9824-9825-9839-9851-9856-9861-9864-9866-9871-9872-9874-9875-9886-9942-9944-9945-9969-9973-9977-10011-10012-10016-10027-10030-10043-10051-10081-10084-10111-10114-10120-10135-10140",
    "6708-7811-7937-7995-8024-8036-8073-8102-8137-8606-8687-8690-8699-8703-8704-8716-8730-8732-8740-8746-8747-8748-8749-8759-8760-8763-8766-8767-8768-8784",
    "7070-8065-8100-8162-8167-8182-8183-8541-8566-8567-8569-8590-8593-8595-8599-8600-8601-8602-8607-8611-8615-8617-8621-8624-8634-8664-8665-8666-8667-8680-8685-8689-8691-8700-8701-8702",
    "7974-7982-7983-7984-7985-7986-7988-7993-7994-7997-8001-8003-8005-8006-8007-8008-8017-8026-8028",
    "9163-9231-9250-9265-9300-9534-9625-9628-9687-9777-9814-9841-9849-9881-9913-9975-9976-9978-10015-10033-10038-10040-10045-10058-10073-10076-10094-10095-10121-10141-10142-10143-10144-10152-10156-10159-10161-10169-10187-10188-10191-10197-10202-10209-10210-10220-10221-10229-10237-10242-10244-10251-10253-10259-10269-10273-10281-10282-10335-10341-10342-10344-10361-10369-10388-10394-10396-10397-10403-10415-10418-10422-10424-10427-10432-10437-10438-10439-10442-10445-10456-10457-10464-10467-10469-10479-10482-10491-10503-10508-10516-10518-10526-10533-10546-10552-10562-10565-10571-10572-10573-10584-10587-10591-10599-10613-10618-10623-10627-10628-10629-10633-10637-10651-10656-10659-10663-10664-10674-10684-10687-10688-10692-10695-10702-10708-10714-10716-10717-10723-10724-10732-10737-10740-10741-10742-10743-10745-10746-10749-10751-10756-10776-10791-10798-10822-10823-10824-10828-10833-10834-10836-10838-10840-10855-10856-10859-10861-10864-10872-10882-10884-10891-10893-10899-10902-10903-10911-10918-10920-10926-10927-10928-10944-10949-10957-10961-10967-10969-10973-10974-10977-10978-10980-10984-10987-10988-10996-11001-11004-11008-11011-11020-11023-11025-11028-11036-11054-11056-11059-11060-11064-11068-11082-11083-11105-11106-11108-11114-11134-11135-11136-11141-11143-11146",
    "9256-9295-9317-9630-9935-10024-10092-10193-10540-11553-11605-11749-11833-11989-12345-12372-12399-12766-12771-12782-12814-12843-12855-12861-12874-12877-12879-12880-12891-12900-12922-12924-12927-12930-12944-12946-12952-12961",
    "9949-10265-10421-10929-11393-11640-11779-11810-11906-11994-12073-12148-12193-12327-12353-12354-12368-12383-12387-12388-12390-12394-12414-12423-12428-12434-12437-12438-12439-12445-12448-12449-12454-12462-12463-12464-12466-12467-12469-12470-12471-12477-12480-12481-12482-12483-12484-12485-12491-12507-12515",
    "11224-11306-11318-11319-11321-11322-11325-11326-11327-11328-11329-11330-11331-11332-11334-11336-11337-11339-11341-11343-11344-11345-11346-11350-11352-11355",
    "11349-11360-11364-11370-11374-11375-11376-11377-11378-11380-11382-11383-11387-11388-11391-11394-11395-11402",
    "11445-11500-11501-11507-11515-11522-11527",
    "11510-11797-12070-12396-12493-12506-12516-12517-12521-12524-12530-12536-12542-12554-12567",
    "11532-11535-11554-11557-11563-11568-11579-11587-11588-11590-11594-11619-11621-11639-11669-11685-11687-11690",
    "11911-12774-12995-13703-13821-14074-14081-14137-14154-14184-14196-14200-14204-14205-14209-14215-14225-14251-14288-14306-14324-14325-14347-14362-14373-14445-14470-14509-14513",
    "12377-12678-12746-12759-12760-12769-12772",
    "12560-12623-12631-12644-12646-12647-12651-12653-12654-12655-12657-12658-12661-12662-12671",
    "12649-12663-12664-12666-12669-12673-12676-12677-12680-12683-12692-12705-12708-12717-12721-12722-12725-12728-12730-12731-12733-12736-12744",
    "12710-12963-12969-12975-12984-12988-12991-13006-13286-13335-13336",
    "12781-12815-12821-12833-12837-12841-12846-12864",
    "12992-13024-13143-13303-13346-13427-13473-13476-13480-13486-13515-13590-13860-13863",
]
for _ni in _BUNDLE_NIS_THEALGORITHMS_PYTHON:
    Instance.register("TheAlgorithms", _ni)(THEALGORITHMS_PYTHON)
