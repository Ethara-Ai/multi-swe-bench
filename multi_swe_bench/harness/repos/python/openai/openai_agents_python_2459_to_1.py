import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

class ImageBase(Image):
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
        return "base-2459-to-1"

    def workdir(self) -> str:
        return "base-2459-to-1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the base to a single PR's
        # base.sha and breaking every other PR in the era with
        # "reference is not a tree". The base keeps full history; the strict
        # anti-reward-hack hardening runs per-PR (see ImageDefault).
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl wget ca-certificates \\
    build-essential gcc g++ python3-dev \\
    linux-libc-dev rclone \\
    && rm -rf /var/lib/apt/lists/*

# uv pinned (not `latest`): the resolver version decides the dependency set, so
# an unpinned uv makes rebuilds non-reproducible.
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
RUN uv sync --all-extras --all-packages --group dev || uv sync || true

WORKDIR /home/

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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
git clean -fdx -e .venv
git checkout {self.pr.base.sha}
uv sync --all-extras --all-packages --group dev || uv sync || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
uv run pytest -v
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
uv run pytest -v
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
uv run pytest -v
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


@Instance.register("openai", "openai-agents-python_2459_to_1")
class OPENAI_AGENTS_PYTHON_2459_TO_1(Instance):
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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest -v verbose output, e.g.:
        #   tests/test_agent_config.py::test_system_instructions PASSED  [  0%]
        #   tests/test_agent_hooks.py::test_streamed_agent_hooks FAILED  [  2%]
        #   tests/extensions/memory/test_redis_session.py::test_x SKIPPED [ 11%]
        passed_pattern = re.compile(
            r"^(.+?)\s+PASSED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        passed_tests.update(passed_pattern.findall(clean_log))

        skipped_pattern = re.compile(
            r"^(.+?)\s+SKIPPED\s+(?:\[\s*\d+%\s*\]|\[\d+\])", re.MULTILINE
        )
        skipped_tests.update(skipped_pattern.findall(clean_log))

        # Inline verbose failure line: "<nodeid> FAILED [ 2%]"
        failed_inline = re.compile(
            r"^(.+?)\s+FAILED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        failed_tests.update(failed_inline.findall(clean_log))

        # Summary section: "FAILED <nodeid> - <reason>" / "ERROR <nodeid>"
        failed_summary = re.compile(
            r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE
        )
        failed_tests.update(failed_summary.findall(clean_log))

        # Dedup: worst result wins
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Registered so delivered records (which carry the dash-joined number_interval)
# resolve to this class (PIPELINE §11/§11c). Trimmed to the RESOLVED set
# (delivery-time subset); the era key above still routes the build dataset.
_BUNDLE_NIS_OPENAI_ERA1 = [
    "5-13-29-33-34-35-39-45-52-55-56-57-58-80-83-84-89-93-99-103-104-105-108-110-112-113-114-140",  # pr-5 (28 PRs)
    "242-249-255-264-265-266-267",  # pr-242 (7 PRs)
    "262-639-763-861-871-897-903-909-920-923-925-928-930-935-936-937-938-950-951-952-958-960-963",  # pr-262 (23 PRs)
    "439-452-457-460-463-465-475-483-484-486-496-500-503-504-505-506-507-508-509-513-514",  # pr-439 (21 PRs)
    "550-573-579-580-582-589-590-593",  # pr-550 (8 PRs)
    "577-592-595-626-635",  # pr-577 (5 PRs)
    "598-1162-1169-1170",  # pr-598 (4 PRs)
    "638-643-677-685-701",  # pr-638 (5 PRs)
    "665-735-736-737-743-746-757-774-775-780-785-789-792-799-801-803-804-808-809",  # pr-665 (19 PRs)
    "752-922-977-990-1000-1009-1010-1015-1033-1034-1038-1043-1053-1055-1067-1068-1069-1070-1071-1072-1073-1074-1076-1079-1080-1081-1082-1084-1086-1104-1105-1106-1107-1110-1111-1112-1113-1117-1118-1119-1120-1122-1124",  # pr-752 (43 PRs)
    "766-811-814-815-817-818-842-872-874-876-878",  # pr-766 (11 PRs)
    "971-1052-1063-1381-1386-1392-1413-1415-1423",  # pr-971 (9 PRs)
    "974-1206-1250-1278-1292-1296-1302-1307-1308-1309-1310-1313-1319-1321-1322-1326-1329-1330-1332-1336-1339-1341-1355-1356-1360-1366-1368-1369-1370-1388-1398",  # pr-974 (31 PRs)
    "998-1354-1382-1399-1426-1439-1440-1458-1461-1462-1469-1470-1471-1472-1473-1480",  # pr-998 (16 PRs)
    "999-1101-1134-1135-1139-1141-1142-1149-1150-1151-1153-1157",  # pr-999 (12 PRs)
    "1048-1098-1212-1214-1215-1216-1217-1218-1221-1224-1231-1232-1233-1235-1240-1241-1242-1243-1246-1251-1252-1259-1260-1261-1266-1267-1268-1270-1272-1273-1280-1284-1286-1287-1289-1291-1301",  # pr-1048 (37 PRs)
    "1192-1300-1535-1548-1549-1558-1561-1562-1563-1576-1577-1582-1586-1587-1589-1590-1599-1600-1601-1602-1607-1610",  # pr-1192 (22 PRs)
    "1298-1537-1550-1646-1657-1665-1667-1669-1682-1683-1684-1685-1687-1688-1689-1691-1693-1695-1696-1700-1710-1717",  # pr-1298 (22 PRs)
    "1475-1476-1478-1479-1482-1483-1484-1490-1495-1500-1501",  # pr-1475 (11 PRs)
    "1619-1624-1626-1627-1628-1633-1636-1637-1641-1642-1643-1647-1648-1649-1650-1654-1655",  # pr-1619 (17 PRs)
    "1662-1785-1792-1798-1809-1810-1811-1812-1813-1816-1818-1819-1820-1821-1825-1826-1835-1836-1837-1838",  # pr-1662 (20 PRs)
    "1674-1703-1706-1718-1720-1721-1730-1734-1739-1740-1743-1744-1745-1747-1749-1751-1753-1757-1758-1759-1768-1773",  # pr-1674 (22 PRs)
    "1752-1765-1774-1777-1779-1782-1784-1787-1793",  # pr-1752 (9 PRs)
    "1791-1795-1827-1833-1839-1842-1843-1855-1856-1858-1861-1868-1869-1872-1873-1874-1878-1883-1884-1891-1893-1896-1898-1905-1910-1913-1917-1918-1919",  # pr-1791 (29 PRs)
    "1804-1986-1996-2014-2033-2082-2091-2092-2093-2095",  # pr-1804 (10 PRs)
    "1852-2084-2104-2105-2106",  # pr-1852 (5 PRs)
    "1894-1921-1922-1931-1932-1933-1934-1936-1947-1952-1955-1956-1960-1962-1963-1965-1967-1968-1971",  # pr-1894 (19 PRs)
    "1937-1961-1993-1995-2000-2002-2006-2013-2015-2028-2034-2037-2039",  # pr-1937 (13 PRs)
    "1972-1979-1981-1982-1984-1988",  # pr-1972 (6 PRs)
    "2019-2026-2044-2047-2077-2079-2080",  # pr-2019 (7 PRs)
    "2059-2224-2267-2270-2271-2284-2286-2287-2288-2289-2290-2292-2295-2298-2300-2302-2303-2304-2306-2309-2310-2311",  # pr-2059 (22 PRs)
    "2108-2112-2116-2117-2126-2128-2131-2139-2141-2142-2144-2147-2152-2153",  # pr-2108 (14 PRs)
    "2134-2162-2166-2177-2178",  # pr-2134 (5 PRs)
    "2158-2170-2207-2209-2210-2212-2213-2214-2215-2219-2225-2226-2227-2229-2235-2238-2243-2260-2262-2263",  # pr-2158 (20 PRs)
    "2169-2174-2179-2182-2184-2188-2189-2191-2192-2193-2194-2197-2198",  # pr-2169 (13 PRs)
    "2196-2230-2249-2273-2347-2355-2356-2357-2359-2360-2362-2363-2368-2369-2374-2375-2377-2378-2380-2381-2382-2383-2385-2387-2389-2390-2391-2394-2395-2398-2399-2400-2402-2404-2408-2409-2410-2411-2413-2414-2415-2416-2418-2419",  # pr-2196 (44 PRs)
    "2264-2272-2282-2327-2338-2339-2340-2341-2342-2344-2345-2350",  # pr-2264 (12 PRs)
    "2268-2325-2326-2328-2329-2331-2332",  # pr-2268 (7 PRs)
    "2299-2307-2312-2316-2318-2319-2320-2322-2323",  # pr-2299 (9 PRs)
    "2334-2336-2337",  # pr-2334 (3 PRs)
    "2405-2420-2423-2424-2425-2431-2433",  # pr-2405 (7 PRs)
    "2432-2434-2435-2438-2443-2446-2447-2448",  # pr-2432 (8 PRs)
    "2436-2450-2451",  # pr-2436 (3 PRs)
    "2440-2452-2453-2454-2455-2458-2460-2463-2464-2465-2466-2469",  # pr-2440 (12 PRs)
    "2456-2468-2473-2474-2475-2478-2479-2480",  # pr-2456 (8 PRs)
]

for _ni in _BUNDLE_NIS_OPENAI_ERA1:
    Instance.register("openai", _ni)(OPENAI_AGENTS_PYTHON_2459_TO_1)
