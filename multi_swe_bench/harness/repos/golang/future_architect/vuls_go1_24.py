import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Shared per-era base image (built once, reused by every go1_24 PR).

    Clone-only with full history kept so any PR's base.sha is reachable; the PR
    layer does the checkout + strict history-strip. The `# syntax` directive opts
    out of DockerfileEnhancer so this hand-written layout is used verbatim.
    """

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
        return "golang:1.24-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-go1_24"

    def workdir(self) -> str:
        return "base-go1_24"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GOTOOLCHAIN=auto

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git gcc ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
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

    def dependency(self) -> "Image":
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

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
                "check_git_changes.sh",
                """#!/bin/bash
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Recent trivy deps import encoding/json/v2 (Go 1.25+, gated behind GOEXPERIMENT=jsonv2).
# Enable it only when the (possibly patched) go.mod resolves to Go >= 1.25; setting it on
# older toolchains errors, so guard on the highest go/toolchain directive minor version.
JSONV2_MINOR=$(grep -hoE '^(go|toolchain) +1[.][0-9]+' go.mod 2>/dev/null | grep -oE '1[.][0-9]+' | cut -d. -f2 | sort -n | tail -1)
if [ -n "$JSONV2_MINOR" ] && [ "$JSONV2_MINOR" -ge 25 ]; then export GOEXPERIMENT=jsonv2; fi

go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
JSONV2_MINOR=$(grep -hoE '^(go|toolchain) +1[.][0-9]+' go.mod 2>/dev/null | grep -oE '1[.][0-9]+' | cut -d. -f2 | sort -n | tail -1)
if [ -n "$JSONV2_MINOR" ] && [ "$JSONV2_MINOR" -ge 25 ]; then export GOEXPERIMENT=jsonv2; fi
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
JSONV2_MINOR=$(grep -hoE '^(go|toolchain) +1[.][0-9]+' go.mod 2>/dev/null | grep -oE '1[.][0-9]+' | cut -d. -f2 | sort -n | tail -1)
if [ -n "$JSONV2_MINOR" ] && [ "$JSONV2_MINOR" -ge 25 ]; then export GOEXPERIMENT=jsonv2; fi
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
JSONV2_MINOR=$(grep -hoE '^(go|toolchain) +1[.][0-9]+' go.mod 2>/dev/null | grep -oE '1[.][0-9]+' | cut -d. -f2 | sort -n | tail -1)
if [ -n "$JSONV2_MINOR" ] && [ "$JSONV2_MINOR" -ge 25 ]; then export GOEXPERIMENT=jsonv2; fi
go test -v -count=1 ./...

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

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history). prepare.sh checks out this PR's base.sha, then the canonical
        # hardening block detaches at that literal sha and strips every other
        # ref/reflog so later commits (the fix) are unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("future-architect", "vuls_go1_24")
class Vuls_go1_24(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Delivery scope = RESOLVED (valid) bundles only; keys == #delivered instances (PIPELINE §11c).
# Bucketed by the authoritative go.mod-at-base.sha era key (NOT a lead-PR proxy).
_BUNDLE_NIS_Vuls_go1_24 = [
    "801-925-927-931-932-936-939-942-947-948",
    "974-981",
    "1008-1012-1014",
    "1029-1031",
    "1051-1052-1056-1059-1060",
    "1076-1078",
    "1089-1090-1092",
    "1091-1094-1095-1096-1097",
    "1098-1105-1106-1107-1109-1110-1114-1115-1117-1118-1119-1120-1122-1124",
    "1099-1100-1102-1103-1104",
    "1116-1143-1145-1147-1148-1149",
    "1132-1133-1134-1136-1137-1139-1140-1141-1142",
    "1154-1155-1158-1160-1161-1162-1163-1165-1166-1167",
    "1157-1168",
    "1169-1170-1173-1174-1176-1177-1178",
    "1179-1180-1182-1183-1185-1186-1187-1188-1189-1190-1191-1193-1194-1195-1196-1197-1201-1203-1204",
    "1207-1212-1216-1218-1221-1222-1223-1224-1227-1228-1229-1232-1234-1235-1236-1242-1244-1246-1247-1248-1249",
    "1220-1878-1885-1888-1890-1891-1893-1894-1896-1898-1899-1902-1903-1907-1908-1909-1910",
    "1261-1274-1275-1277-1278-1279-1282-1283-1286-1287-1288-1290-1291-1292-1293-1294-1296",
    "1280-1298-1300-1301",
    "1308-1309-1310-1311-1313-1314-1316-1317-1318-1320",
    "1321-1323-1324-1325-1326-1331-1335",
    "1334-1338-1339-1343-1347-1348-1351-1352",
    "1344-1406-1415-1442-1443-1456-1460-1461-1465-1466-1469-1475-1479-1481-1487",
    "1359-1360-1364-1365-1366-1367-1372-1377-1379-1385",
    "1380-1397-1401-1405-1407-1414-1417-1425-1426-1428",
    "1381-1912-1914-1918-1921",
    "1382-1384-1386-1387-1388-1393-1395",
    "1431-1433-1436-1438-1444-1451-1452",
    "1490-1494-1495-1498-1499-1504-1507-1510-1511",
    "1523-1581-1606-1625-1626-1627-1632",
    "1538-1543-1548-1552-1553",
    "1578-1585-1586-1591-1592-1593-1597-1598-1603-1610-1615",
    "1580-1588-1647-1656-1657-1658-1659-1660-1662-1663-1665-1666-1667-1669-1671-1672-1673-1674",
    "1599-1628-1635-1639-1642-1646-1649-1650-1652",
    "1616-1621",
    "1636-1687-1692-1699-1703-1706-1707-1714-1721-1726-1730-1731-1739",
    "1654-1675-1676-1677-1678-1681-1682-1688-1689-1696",
    "1661-1922-1927-1928-1929-1930-1934-1935-1936-1938-1941-1942-1943-1944-1945-1946-1947-1948-1949-1950-1951-1952-1953-1954-1955-1957-1958-1959-1960-1961-1962-1964-1965-1966-1967-1969-1970-1971-1972",
    "1708-1733-1750-1754-1769-1770",
    "1712-1736-1741",
    "1743-1745-1748-1749-1756-1757-1761-1762",
    "1751-1771-1773-1774-1775-1776-1777-1780-1781-1782-1785-1786-1788-1789-1790-1791-1794",
    "1763-1767",
    "1797-1798-1799-1803-1805-1812",
    "1806-1849-1854-1856-1858-1859-1861-1862-1864-1865-1866",
    "1818-1819-1824-1826-1829-1831-1833-1836-1837-1838-1842-1843-1844",
    "1868-1871-1872-1873-1874-1875-1877-1880-1881",
    "1973-1974-1976-1978-1979-1980-1981-1982-1983-1984-1985-1986-1987-1988-1990-1991-1992-1993-1994-1995-1997-1999-2000-2001-2002-2003-2004-2005-2006-2008-2012-2013-2014-2015-2020-2021-2022-2023-2025-2026-2027-2028-2029-2030-2032-2037-2038-2039-2040-2041-2042-2043-2044-2045",
    "2046-2047-2049-2051-2052-2054-2055-2057-2058-2059-2060-2061-2063-2064-2065-2067-2068-2069-2072-2073-2074",
    "2078-2080-2081-2085-2088-2089-2090-2092-2093",
    "2100-2101-2102-2103-2104-2106-2108-2109-2113-2115-2117-2119-2120-2121",
    "2151-2152-2154-2156-2159-2160-2162-2163-2166-2167-2169-2170-2171-2174-2175-2176-2177-2179-2180-2181-2182-2183-2184-2185-2186-2201",
    "2157-2194-2200-2206-2210-2211-2212-2214-2215-2216-2218-2221-2224-2227-2228-2229-2230-2232-2233-2234-2235-2236-2237-2239-2240-2241-2243-2244-2245-2247-2248-2249",
    "2197-2203-2204-2205",
    "2255-2259-2260-2261-2262-2263-2265-2269-2271-2273-2277-2280-2282-2285-2286-2287-2288-2290-2291-2292-2293",
    "2284-2440",
    "2294-2302-2310-2311-2314-2315-2316-2317-2319-2321-2322-2323-2324-2326-2327",
    "2297-2298-2300-2304-2306-2307-2309",
    "2328-2329-2330",
    "2334-2350-2351-2352-2354-2356-2357-2358-2359-2360-2362-2363",
    "2364-2366-2367-2370-2371-2372-2373",
    "2375-2377-2379-2380-2383-2384-2385-2387-2388-2389-2390-2393-2394-2396-2399-2400-2401-2402-2403-2404-2405-2406-2408-2409-2410-2411-2412-2413-2414",
    "2415-2416-2417-2418-2419-2420-2421-2425-2426",
    "2431-2432-2433-2434-2436-2437",
    "2441-2443-2450",
    "2448-2454-2458-2460-2469-2471-2474-2476-2477-2478-2482-2483-2491-2495-2497-2498-2502-2503-2505-2508-2509-2511-2512-2513-2517-2518-2520-2521-2523-2524-2525-2526-2527-2531-2532-2533-2540-2543-2546-2547-2548-2549",
]
for _ni in _BUNDLE_NIS_Vuls_go1_24:
    Instance.register("future-architect", _ni)(Vuls_go1_24)
