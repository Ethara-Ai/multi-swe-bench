import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# rqlite/rqlite — a lightweight distributed relational database built on SQLite.
#
# Discovery (dataset analysis):
#  - 119-PR Go range #1459..#2658, single Go module, all base ref `master`.
#  - Test files live directly under top-level packages: auth/, cluster/, db/,
#    http/, store/, auto/backup, snapshot/, ... Standard `go test` per package.
#  - rqlite uses SQLite through cgo (mattn/go-sqlite3) — CGO_ENABLED=1 is
#    required and a C toolchain must be present; build-essential covers it.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs each. Runs are fenced with `### RQPKG ###`
#    markers so test ids stay unique across packages.


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks so `git apply` never aborts on a binary hunk
    with no full-index line (e.g. images, *.db fixtures). Safe: binary hunks
    touch no Go source and never affect test outcomes."""
    import re as _re
    sections = _re.split(r"(?=^diff --git )", patch, flags=_re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )


class RqliteImageBase(Image):
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
        return "golang:1-bookworm"

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

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
# Let Go auto-fetch whatever toolchain a given PR's go.mod requests.
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
# rqlite imports cgo SQLite (mattn/go-sqlite3).
ENV CGO_ENABLED=1
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class RqliteImageDefault(Image):
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
        return RqliteImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

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

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the Go module cache at the base commit (cached into the image layer).
go mod download 2>/dev/null || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        # Per-package `go test`. -vet=off avoids vet-only failures masking the
        # real test outcome.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### RQPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(".", "check_git_changes.sh", check_git),
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

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("rqlite", "rqlite")
class Rqlite(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RqliteImageDefault(self.pr, self._config)

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

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestQuery (0.01s)
        #   --- FAIL: TestStoreRestart (0.02s)
        #   --- SKIP: TestS3Backup (0.00s)
        # Fenced by `### RQPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### RQPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

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


# ---------------------------------------------------------------------------
# number_interval bundle routing (prs_in_bundle dash-joined)  -- PIPELINE 11b
# ---------------------------------------------------------------------------
# Raw dataset leaves number_interval empty; delivery sets it to
# "-".join(prs_in_bundle). Single-era repo -> every bundle key routes to the
# one Rqlite class. Original "rqlite/rqlite" registration above is kept.
_BUNDLE_NIS_RQLITE = [
    "1459-1460-1462-1463-1464-1465",
    "1466-1467-1468-1469",
    "1471-1472-1473-1475-1478-1479-1480",
    "1484-1485",
    "1492-1493-1494",
    "1503-1505-1509",
    "1510-1511",
    "1515-1516-1518-1519-1520",
    "1522-1523-1524-1525-1526-1527",
    "1528-1529-1530-1531-1533-1535-1536-1538-1539-1540-1541-1542-1543-1544",
    "1547-1548-1550-1555-1556-1557-1564",
    "1563-1566-1567-1570-1571-1572-1573",
    "1574-1575-1576-1578-1579",
    "1582-1583",
    "1584-1585-1586-1587-1588-1589-1590",
    "1592-1593-1596-1597-1598-1599-1600-1601-1602",
    "1603-1604-1605-1606",
    "1607-1608-1610-1611-1612",
    "1615-1616-1617-1620-1621",
    "1619-1622-1623-1625-1626",
    "1629-1631-1632",
    "1633-1634",
    "1635-1636-1637",
    "1638-1639-1640-1641",
    "1646-1647-1649-1651-1652-1653-1654",
    "1656-1659-1660-1661",
    "1665-1666-1667-1669",
    "1670-1671-1674-1675-1677-1681",
    "1682-1683-1732-1742-1743-1745",
    "1685-1686-1688",
    "1689-1692",
    "1693-1694",
    "1702-1703-1704",
    "1708-1709-1710-1713-1714-1715",
    "1716-1718-1719-1720-1721-1722-1723-1724-1725-1727",
    "1747-1748-1749-1750-1753",
    "1754-1755-1757-1758-1759",
    "1760-1761-1762",
    "1765-1766-1768-1769-1772-1773",
    "1776-1777",
    "1783-1786-1787",
    "1791-1792",
    "1793-1795-1796",
    "1800-1801-1805",
    "1814-1815-1816-1824-1825",
    "1826-1827-1828",
    "1832-1834",
    "1836-1837-1838",
    "1841-1842-1843",
    "1844-1845-1847-1848-1849-1852",
    "1854-1855",
    "1859-1860-1861-1862-1863-1864-1865-1867-1868",
    "1872-1873",
    "1876-1878",
    "1877-1880-1881-1882-1883",
    "1884-1887-1888",
    "1889-1890-1891-1892-1893",
    "1894-1895-1896-1897-1899-1900-1901",
    "1903-1905",
    "1906-1908-1910",
    "1911-1912-1913",
    "1915-1916-1917-1918-1920-1921",
    "1923-1924-1925-1926-1927-1928-1931-1932",
    "1930-1933-1934-1935-1936-1937-1938-1940-1941-1942",
    "1947-1949-1950",
    "1955-1956-1957",
    "1958-1959-1962-1963-1964",
    "1965-1966-1967-1969-1972",
    "1975-1976",
    "1988-1989-1991",
    "1992-1993",
    "2000-2002-2003-2004-2005-2006",
    "2012-2013-2014-2015-2016",
    "2020-2021-2022-2024-2025-2026",
    "2027-2029-2030-2031-2032-2033-2034",
    "2037-2038",
    "2044-2047-2048",
    "2051-2052",
    "2054-2055-2056",
    "2057-2058-2059",
    "2061-2062-2066-2067",
    "2064-2068-2069-2070-2071-2072-2073-2074-2076-2077-2080-2081-2082-2083-2084",
    "2099-2101-2102-2105-2108-2110-2112-2113-2114-2115",
    "2106-2125-2126-2128-2129-2131-2132-2133-2134-2135-2137-2138-2139",
    "2119-2120-2121-2124",
    "2144-2145-2146",
    "2147-2148",
    "2149-2150-2152-2154-2156-2159-2161-2170",
    "2172-2173-2174-2176-2177-2179",
    "2180-2182-2183-2184",
    "2186-2187-2189-2190-2191",
    "2193-2195",
    "2197-2198-2200-2201-2202-2203",
    "2204-2205-2206-2208-2211-2214-2216-2217-2218",
    "2220-2221-2223-2225-2226-2227-2228-2231-2233-2235-2236-2238-2240-2242-2243-2248-2250-2252-2253-2255-2256-2257-2258-2259-2261-2262-2265-2266",
    "2263-2267-2268-2270-2274-2275-2276-2278-2280-2285-2286-2287-2288-2292-2296-2297-2298-2299-2300-2301-2303-2305-2306-2307-2308-2309-2310-2311-2312-2313-2314-2315-2316-2317-2318-2319-2321-2323-2324-2325-2327",
    "2328-2329",
    "2334-2337-2340",
    "2343-2344-2349-2350",
    "2352-2355-2356-2357-2358-2359-2360-2362-2365-2367-2369",
    "2371-2373-2374-2375-2376",
    "2377-2378-2380",
    "2382-2383",
    "2384-2385-2386-2387-2388",
    "2396-2397",
    "2404-2408-2412-2413",
    "2419-2420-2421-2422",
    "2423-2424-2425",
    "2427-2428-2429-2430-2432",
    "2436-2437",
    "2451-2452-2453-2454-2456-2457-2459-2460",
    "2461-2462-2463",
    "2465-2467",
    "2468-2472-2473-2474",
    "2469-2470",
    "2640-2641-2642-2644-2648",
    "2649-2651",
    "2653-2654-2655-2656",
    "2658-2659-2660",
]
for _ni in _BUNDLE_NIS_RQLITE:
    Instance.register("rqlite", _ni)(Rqlite)
