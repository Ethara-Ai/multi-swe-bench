import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# inngest/inngest — a durable functions / event-driven workflow platform (Go).
#
# Discovery (dataset analysis):
#  - 113-PR Go range #423..#4065 spanning releases v0.13 .. v1.19, single Go
#    module, base ref `main`. This is a release-bundled dataset: each record's
#    base.label is a release-tag range (e.g. "v0.24.3..v0.25.0"), fix/test
#    patches are the diff across that range, and base.sha is the lower tag.
#  - Because the go directive moves across the range (go 1.18 .. 1.25), the
#    single shared toolchain base uses GOTOOLCHAIN=auto so each PR's go.mod
#    pulls (and the build warms) whatever toolchain it needs.
#  - Test files live under `pkg/...`, `tests/`, and a top-level `inngest/`.
#  - inngest's dev server uses cgo (mattn/go-sqlite3) for embedded state, so
#    CGO_ENABLED=1 is required and a C toolchain must be present.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs each. Runs are fenced with `### IGPKG ###`
#    markers so test ids stay unique across packages.
#
# Registry shape (aligned with harness/image.py — see the two class docstrings):
#  - InngestImageBase  = ONE shared toolchain-only base (no clone). Every PR's
#    per-PR image builds FROM it, so the base is built once and reused.
#  - InngestImageDefault = per-PR image that clones full history, checks out its
#    own base.sha, warms the build cache, then applies the canonical hardening
#    strip. This is the pin-and-strip-safe layout (see InngestImageBase docstring).


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


# Single Go toolchain image shared by every PR. golang:1-bookworm is the latest
# 1.x on Debian bookworm; combined with GOTOOLCHAIN=auto it builds the whole
# v0.13..v1.19 range (older modules build forward-compatibly, newer modules pull
# their exact toolchain, which install.sh warms into the image at build time).
_GO_IMAGE = "golang:1-bookworm"

# Archive-resilient apt: try the live mirror first, fall back to
# archive.debian.org (dropping -updates) if bookworm is ever retired. Mirrors
# the deprecated-Debian handling image.py applies, keyed off reachability.
_APT_INSTALL = (
    "RUN { apt-get update 2>/dev/null || "
    "{ sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list* && "
    "sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list* && "
    "sed -i '/-updates/d' /etc/apt/sources.list*; apt-get update; }; } && \\\n"
    "    apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    wget \\\n"
    "    git \\\n"
    "    build-essential \\\n"
    "    pkg-config \\\n"
    "    gnupg \\\n"
    "    make \\\n"
    "    sudo \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)


# ---------------------------------------------------------------------------
# Build-context scripts (COPY'd into the per-PR image, run at build/eval time).
# ---------------------------------------------------------------------------

# Warms the Go module + build cache at base.sha (and, best-effort, at the
# fix/test-patch state) so the three eval runs start compiled and offline-safe.
# Runs BEFORE the hardening strip; every step is best-effort so a flaky baseline
# never breaks the image build.
_INSTALL_SH = """#!/bin/bash
set -uxo pipefail
git config --global --add safe.directory /home/inngest || true
cd /home/inngest

# base.sha is already checked out by the Dockerfile. Warm module + build caches
# (this also triggers GOTOOLCHAIN=auto to download this PR's toolchain).
go mod download 2>/dev/null || true
go build ./... >/dev/null 2>&1 || true

# Pre-cache module deps + toolchain introduced by the release-range patches so
# the fix stage need not reach the network. Apply best-effort, warm, then reset.
git apply --3way --whitespace=nowarn /home/fix.patch >/dev/null 2>&1 || true
git apply --3way --whitespace=nowarn /home/test.patch >/dev/null 2>&1 || true
go mod download 2>/dev/null || true
go build ./... >/dev/null 2>&1 || true
git reset --hard >/dev/null 2>&1 || true
git checkout . >/dev/null 2>&1 || true
exit 0
"""

# Shared per-package runner: `go test` each package that owns a changed
# `*_test.go`, fenced with `### IGPKG ###` so parse_log keeps ids unique across
# packages. -vet=off keeps vet-only failures from masking the real outcome.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/inngest
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### IGPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done
"""

# Baseline: clean base.sha, no patches. base.sha stays checkout-able after the
# hardening strip because it is HEAD (reachable, not pruned).
_RUN_SH = """#!/bin/bash
set -uxo pipefail
export CI=true
cd /home/inngest
git reset --hard
git checkout __SHA__
bash /home/run_tests.sh
"""

# Test patch only: the new tests exercise behaviour the fix has not introduced
# yet, so they fail (or their package fails to compile) -- genuine f2p / n2p.
_TEST_RUN_SH = """#!/bin/bash
set -uxo pipefail
export CI=true
cd /home/inngest
git reset --hard
git checkout __SHA__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "WARN: test.patch apply failed (continuing)"
bash /home/run_tests.sh
"""

# Test + fix patches: production fix present, the new suite should pass. Fix and
# test patches are applied separately so an overlap in one does not block the
# other.
_FIX_RUN_SH = """#!/bin/bash
set -uxo pipefail
export CI=true
cd /home/inngest
git reset --hard
git checkout __SHA__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/fix.patch \\
  || echo "WARN: fix.patch apply failed (continuing)"
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "WARN: test.patch apply failed (continuing)"
bash /home/run_tests.sh
"""

# Binary/asset diffs in the release-range patches would abort `git apply`; skip
# them so the source hunks still apply.
_EXCLUDES = (
    "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
    "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
    "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
)


class InngestImageBase(Image):
    """Level 1: toolchain-only base image, shared by every PR.

    ``dependency()`` returns a *string* (the Go toolchain image), so the
    pipeline's ``DockerfileEnhancer`` engages and prepends the
    ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image must NOT clone
    the repository -- a shared string-dependency image that performs a
    ``git clone`` is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer, which breaks ``git checkout`` for every
    other PR sharing the base (the old bug that produced near-zero resolved
    counts). So the clone lives in InngestImageDefault (whose dependency() is an
    Image, left verbatim by the enhancer) and is done per-PR. This image only
    provides the Go toolchain, apt deps, and the Go build env.
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

    def dependency(self) -> Union[str, "Image"]:
        return _GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # No `git clone` here on purpose -- see the class docstring. The string
        # dependency means DockerfileEnhancer injects the ARG/ENV/LABEL infra
        # block (DEBIAN_FRONTEND/LANG/TZ included, so they are NOT re-declared
        # below), but no clone/hardening since this Dockerfile has no clone.
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/

{_APT_INSTALL}

# Auto-fetch whatever toolchain a given PR's go.mod requests (v0.13..v1.19 span
# go 1.18..1.25); install.sh warms it into the per-PR layer at build time.
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
# inngest's dev server imports cgo SQLite for embedded state.
ENV CGO_ENABLED=1

CMD ["/bin/bash"]
"""


class InngestImageDefault(Image):
    """Level 2: per-PR image, built on the single shared toolchain base.

    ``dependency()`` returns InngestImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim -- no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ``${BASE_COMMIT}`` inline, COPYs the
    scripts, warms the build cache (install.sh), then the verbatim
    ``Image._HARDENING_BLOCK`` strips origin/refs/future history (with the four
    post-condition asserts + submodule pass) while keeping base.sha reachable.
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

    def dependency(self) -> Union[str, "Image"]:
        return InngestImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        run_tests = _RUN_TESTS_SH.replace("__PKGS__", pkg_list)
        run_sh = _RUN_SH.replace("__SHA__", sha)
        test_run = _TEST_RUN_SH.replace("__SHA__", sha).replace("__EXCLUDES__", _EXCLUDES)
        fix_run = _FIX_RUN_SH.replace("__SHA__", sha).replace("__EXCLUDES__", _EXCLUDES)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", _INSTALL_SH),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("inngest", "inngest")
class Inngest(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return InngestImageDefault(self.pr, self._config)

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
        #   --- PASS: TestExecute (0.01s)
        #   --- FAIL: TestQueue (0.02s)
        #   --- SKIP: TestS3 (0.00s)
        # Fenced by `### IGPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### IGPKG:\s+(\S+)\s+###")

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
# number_interval routing.
#
# Each dataset record carries a `number_interval` = the dash-joined, ascending
# `prs_in_bundle` for that release bundle (e.g. "2451-2454"). When
# number_interval is non-empty, Instance.create() routes ONLY on
# f"{org}/{number_interval}" -- there is NO fallback to the repo key (see
# harness/instance.py). So every interval a dataset row can carry MUST be
# registered here, all mapped to the same Inngest config. The
# @Instance.register("inngest", "inngest") above is kept as a back-compat route
# for any record that omits number_interval.
#
# _NUMBER_INTERVALS is generated from prs_in_bundle across the 113 records.
# Regenerate after any dataset bundle change with:
#   python - <<'PY'
#   import json
#   for l in open("Prem_LHT/Dataset/inngest__inngest_lht_final.jsonl"):
#       d = json.loads(l)
#       print("-".join(str(x) for x in sorted(d["prs_in_bundle"])))
#   PY
# ---------------------------------------------------------------------------
_NUMBER_INTERVALS = [
    "1014-1022-1043-1045-1047-1048-1049-1050-1051-1052-1053-1054-1055-1057-1058-1060",
    "1165-1167-1169-1172-1175-1176-1177",
    "1170-1171-1174-1179-1180-1181-1182-1184-1185-1186",
    "1173-1183-1187-1188-1189-1190-1191-1192-1193-1194-1195-1196-1198-1199-1200-1201-1202-1203-1204-1205-1206-1207-1209-1211-1212-1214-1215",
    "1197-1272-1274-1280-1281-1282-1283-1284-1285-1287-1288-1289-1292-1293-1294-1295-1296-1297-1299-1300-1301-1302-1303-1304-1305-1306-1307-1308-1309-1310-1311-1312-1313-1314-1316-1317-1318",
    "1218-1230-1248-1252-1253-1254-1259-1261-1262-1263-1264-1265-1266-1267-1268-1269-1270-1277",
    "1319-1320-1321-1322-1324-1326-1327-1328-1329-1330-1331-1332-1333-1334-1335-1337-1338-1343-1345-1346-1348-1349-1351-1352-1353-1354-1356-1357-1358-1359-1360-1364-1365-1366-1368-1369-1370-1372-1373",
    "1325-1344-1355-1363-1367-1371-1374-1375-1376-1377-1378-1379-1380-1381-1382-1383-1384-1385-1386-1388-1389-1390-1391-1392-1394-1395-1397-1398-1399-1400-1401-1402-1403-1405-1406-1409-1410-1411-1412-1413-1415-1416-1417-1419-1422-1423-1424-1425-1426-1427-1429-1430-1431-1432",
    "1393-1396-1408-1414-1418-1421-1435-1437-1438-1439-1440-1441-1442-1443-1444-1445-1446-1447-1448-1449-1451-1452-1453-1454-1455-1456-1457-1458-1461-1462-1465-1467-1468-1469-1470-1471-1472-1473-1474-1475-1476-1477-1479-1480-1482-1483-1485-1487-1488-1489-1490-1491-1492-1493-1494-1495-1496-1498-1499-1500-1501-1502-1503-1504-1505-1506-1507-1508-1509-1510-1511-1512-1513-1514-1515-1516-1519-1520-1521-1522-1523-1525-1526-1527-1529-1531-1532-1533-1534-1535-1537-1539-1543-1545-1546-1547",
    "1466-1528-1536-1538-1540-1541-1542-1544-1550-1554-1555-1556-1557-1558-1559-1560-1561-1562-1563-1564-1565-1566-1567-1569-1570-1571-1572-1574-1575-1576-1577-1578-1579-1580-1581-1582-1583-1584-1586-1587-1588-1589-1590-1591-1592-1593-1594-1595-1596-1597-1598-1599-1600-1601-1602-1605-1606-1607-1609-1610-1612-1613-1615-1616-1619-1620-1621-1622-1623-1624-1626-1627-1628-1629-1630-1631-1632-1635-1636-1637-1638-1640-1641-1643-1644-1645-1646-1647-1648-1649-1650-1651-1653-1654-1655-1656-1658-1659-1660-1661-1662-1663-1664",
    "1617-1625-1642-1665-1666-1668-1669-1670-1671-1672-1673-1677-1678-1679-1681-1682-1683-1684",
    "1674-1676-1688-1689-1691-1697-1702-1703-1707-1708-1710-1711-1713-1714-1716-1721-1723-1724-1726-1727-1728",
    "1675-2150-2151-2153-2156-2157-2158-2159-2160-2161-2162-2163-2164-2165-2166-2167-2168-2169-2172-2174-2176-2177-2178-2179-2180-2181-2185-2186-2187-2188-2189-2190-2191-2192-2193-2194-2195-2196-2197-2199-2201-2202",
    "1699-1751-1767-1775-1782-1783-1785-1786-1787",
    "1705-1706-1709-1717-1718-1725-1729-1733-1734-1735-1736-1737-1738-1739-1740-1741-1743-1745-1749-1750",
    "1763-1778-1779-1780-1781",
    "1791-1794-1795-1796-1797-1798-1799-1800-1801-1802-1803",
    "1899-2018-2028-2034-2035-2036-2037-2038-2039-2040-2041-2042-2046-2049-2050-2051-2052-2053-2054-2056-2057-2058-2061-2062-2063-2064-2065-2066-2067-2068-2069-2071-2072-2073-2074-2075-2076-2077-2078-2080-2081-2082-2083-2084-2085-2086-2087-2089-2090",
    "1940-2055-2070-2102-2114-2116-2118-2121-2123-2124-2125-2126-2127-2128-2129-2130-2131-2132-2133-2134-2135-2137-2140-2141-2142-2143-2144-2146-2148-2149-2152-2155",
    "1962-1974-1975-1976-1979-1980-1981-1985-1986",
    "1978-1983-1984-1987-1988-1989-1991-1992-1993-1994-1996",
    "2005-2008-2009-2011-2012-2014-2015-2017-2020-2021-2022-2023-2026-2029-2031",
    "2059-2254-2283-2295-2296-2298-2299-2300-2301-2302-2303-2304-2305-2308-2309-2310-2311-2312-2313-2314-2316-2317-2319-2320-2321-2322-2323-2324-2326-2328-2329-2331",
    "2091-2198-2200-2203-2204-2210-2211-2212-2214-2215-2218-2219-2220-2221-2222-2223-2224-2225-2227-2228",
    "2205-2207-2208-2209",
    "2217-2233-2234-2237",
    "2288-2327-2330-2332-2333-2334-2335-2336-2337-2338-2339-2341-2343-2344-2345-2346-2347-2348-2349-2350-2352-2353-2355-2358",
    "2318-2394-2396-2401-2402-2403-2404-2405-2407-2408-2409-2410-2413",
    "2325-2340-2361-2365-2369-2370-2372-2375-2376-2377-2378-2380-2381-2382-2383-2385-2386-2387-2388-2389-2390-2392-2393-2395-2397-2398",
    "2356-2359-2364-2366-2367-2368-2371",
    "2391-2399-2517-2526-2527-2528-2532-2533-2534-2535-2536-2538-2539-2540-2541-2542-2545-2546-2547-2549-2553-2555-2556-2557-2560-2562",
    "2411-2412-2417-2419-2424-2428-2430-2431-2432-2433-2434-2435-2436-2438-2439-2440-2441-2442-2443",
    "2421-2616-2619-2630-2638-2640-2641-2642-2643-2644-2645-2646-2647-2648-2650-2651-2652-2653-2654-2655-2656-2657-2658-2659-2660-2661-2662-2663-2664-2666-2669-2670-2671-2672-2674-2675-2676-2677-2683-2684-2685-2686-2687-2688-2689-2691-2693-2695-2696-2697-2698-2702-2704-2705-2706-2707-2708-2709-2710-2711-2712-2713-2714-2715-2719",
    "2444-2445-2446-2447-2448-2449-2450",
    "2451-2454",
    "2452-2453-2456-2457-2458-2459-2461-2462-2464-2466-2467-2468-2470-2475-2476-2477-2479-2480-2481-2482-2483-2484",
    "2473-2507-2592-2593-2598-2603-2610-2612-2613-2614-2615-2618-2620-2623-2624-2625-2626-2628-2629-2632-2633-2636-2639",
    "2474-2530-2550-2558-2559-2561-2564-2565-2567-2569-2570-2571-2572-2573-2574-2575-2577-2578-2581-2582",
    "2518-2773-2780-2781-2782-2789-2799",
    "2552-2576-2586-2599-2600-2601-2602-2607-2611",
    "2554-2563-2580-2583-2585-2587-2588-2589-2590-2591-2594-2595-2596",
    "2667-2681-2718-2731-2732-2733-2734-2736-2737-2738-2739-2740-2741-2742-2743-2744-2745-2747-2750-2751",
    "2690-2694-2701",
    "2699-2716-2717-2720-2722-2723-2724-2725-2726-2730",
    "2735-2754-2757-2759-2760-2762-2763-2766-2767-2771-2772-2775",
    "2749-2761-2769-2770-2776-2778-2783-2785-2786-2788-2792-2793-2794-2795-2797-2798",
    "2752-2796-2804-2806-2807-2812-2815-2816-2817-2818-2821-2822-2824-2826-2828-2829-2831-2834-2835-2836-2838-2840-2841-2842-2843-2845-2848-2849-2851-2852-2855-2856-2857-2860",
    "2764-2801-2802-2803-2805-2808-2810-2814",
    "2811-2839-2854-2886-2887-2889-2891-2893-2894-2895-2896-2897-2898-2900-2904-2905",
    "2823-2825-2846-2853-2859-2861-2864-2865-2866-2868-2869-2870-2873-2876-2877-2878-2879-2880-2881-2882-2883-2884-2885-2888-2890",
    "2874-2901-2902-2909-2910-2913-2914-2915-2918-2920-2922-2925",
    "2912-4045-4156-4190-4191-4194-4195-4196-4198-4200-4202",
    "2954-2976-2986-2992-2995-3002-3016-3017-3021-3022-3023-3025-3026-3027-3029-3030-3031-3032-3033-3034-3035-3036-3037-3038-3040-3042-3043-3044-3045-3047-3048-3051-3052-3054-3055-3056-3057-3059-3060-3061-3064-3065-3066-3067-3068-3070-3071-3072-3073-3074-3075-3076-3077-3080-3082-3083",
    "3018-3019",
    "3096-3196-3251-3255-3256-3258-3259-3260-3261-3262-3263-3266-3267-3268-3269-3275-3276-3277",
    "3142-3162-3225-3226-3232-3234-3235-3236",
    "3209-3213-3228-3240-3241-3242-3243-3245-3247-3248-3249-3250-3252-3253-3254",
    "3265-3270-3271-3272-3273-3274-3281-3284-3288",
    "3278-3285-3290-3291-3292-3293-3299",
    "3303-3312-3382-3399-3410-3411-3412-3415-3417-3418-3419-3420-3421-3422-3423-3424-3425-3426-3428-3431-3433-3435-3436-3438",
    "3304-3327-3330-3331-3334-3339-3340-3341-3343-3344-3345-3346-3347-3348-3350-3351-3352-3353-3354-3356-3357-3359-3360-3361-3363-3364-3366-3367-3368-3369-3370-3371-3372-3375-3376-3377-3378",
    "3310-3365-3388-3395-3432-3440-3478-3483-3484-3485-3486-3487-3488-3490-3491-3493-3494-3496-3497-3498-3500-3501-3502-3503-3505-3506-3507-3509-3511-3512-3513-3514",
    "3313-3429-3430-3434-3437-3441-3442-3443-3444-3445-3446-3447-3448-3449-3451-3453-3454-3456-3459-3460-3461-3463-3464-3465",
    "3337-3373-3374-3381-3384-3385-3386-3387-3389-3390-3392-3393-3394-3396-3397-3400-3401-3403-3404-3405-3406-3407-3408",
    "3358-3492-3535-3539-3562-3565-3572-3573-3576-3577-3579-3580-3581-3582-3583-3584-3585-3586-3587-3588-3590-3591-3594-3595-3596-3597-3598-3599-3603-3605-3606-3607-3609-3610-3613-3614-3615-3616-3618-3619-3620-3621-3622-3623-3624-3625-3626-3631",
    "3409-3473-3480-3504-3508-3515-3516-3517-3518-3520-3521-3522-3523-3524-3525-3526-3527-3528-3530-3531-3532-3533-3534-3536-3537-3540-3541-3543-3544-3545-3546-3547-3548-3550-3553-3558-3559-3560-3561-3564-3568-3570-3571",
    "3467-3468-3469-3470-3471-3476-3477-3479",
    "3538-3671-3673-3675-3676-3677-3678-3679-3680-3681-3682-3684-3685-3686-3687-3688-3689-3690-3691-3692-3694-3696-3700-3702-3704",
    "3575-3593-3611-3636-3638-3639-3640-3641-3670",
    "3604-3634-3635",
    "3629-3697-3698-3699-3705-3706-3708-3709-3710-3711-3713-3714-3717-3719-3720-3721-3722-3723-3724-3726-3727-3728-3729-3730-3732-3733-3734-3735-3736-3739-3740-3742-3743-3745-3746-3747-3748-3749-3752-3753-3755-3756-3758-3762-3763-3769-3770-3771-3772-3773-3774-3775-3776-3777-3778-3784-3785-3787-3789-3790-3793-3795-3797-3799-3800",
    "3716-3741-3788-3804-3806-3810-3818-3823-3825-3826-3828-3829-3830-3831-3832-3833-3834-3836-3837-3841-3842-3843-3848-3854-3855-3856-3857-3858-3860-3861-3862-3863-3864-3865-3868-3869-3870-3872-3873-3874-3878-3879-3880-3883-3884-3886-3887-3889-3890-3893-3896-3899-3901-3902-3903-3906",
    "3731-3801-3803-3807-3808-3811-3812-3813-3814-3815-3816-3819",
    "3751-3822",
    "3765-3921-3922-3923-3926-3927",
    "3820-3821",
    "3840-3897-3904-3908-3910-3913-3914-3915-3916",
    "3924-3928-3930-3931-3934",
    "3948-3984-4119-4127-4130-4134-4138-4141-4142-4146-4147-4148-4149-4152-4154-4155-4159-4162-4163-4168-4169-4173-4175-4180-4183-4186-4187-4188",
    "4022-4023-4025-4033-4039-4040-4043-4047-4048-4050-4052-4055-4056-4061-4064-4069-4070-4071-4075-4076",
    "4065-4066-4067-4072-4079-4084-4085",
    "423-426-427-428-429-430-431-432-435-436-437-438-441-442-443-444",
    "434-454-462",
    "450-466-467",
    "455-539-543",
    "460-561-566-568-569-570-571-575",
    "465-475-477-478-479-480-481-482-483-485-486-487-489-490-491-492-494-495-496-497-498-499-500-501-503",
    "470-472-474",
    "504-511-513",
    "514-522-525-530-531-532-533-536-537-538",
    "541-545-546-547-549-551-554-555-559-560",
    "542-563-564-565",
    "574-580-581-582-585-587-588-589-590-591-592-593-594-595-596-599-600-602-603-604",
    "583-584-598-601-605-606-607-608-609-610-611-614-617-619-620-621-622-623-624-625-626-627-630-631-633-634-635-636-638-642-645",
    "618-679-683-688-689-690-691-692-693-694-695-697-698-699-700-701-702-703-704-705-706-707-708-709-710-711-712-713-714-715-716-717-718-719-720-722-723-724-725-726-727-728-729-730-731-732-733-734-735-736-737-738-739-740-741-742-743-744-746-747-748-749-750-751-752-753-755-756-757-758-759-760-761-763-764-765-766-768-769-770-771-772-773-778-779-780-784-785-786-787-788-789-794-795-796-797-798-802-803-804",
    "641-643-644-646-647-648-649-650-655",
    "676-677-678-681",
    "767-782-843-847-852-876-877-878-879-881-882-883-884-885-886-887-888-889-890-891-892-893-894-895-896-897-898-899-900-901-902-903-904-905-906-908-909-910-911-912-913-914-916-918-919-922-923",
    "781-799-800-801-805-806-807-812-813-814-815-816-817-818-820-822-823-824-827-829-830-831-832-833-836",
    "819-835-837-838-839-840-841-844-848-849",
    "845-846-850-851-853-854-856-857-858-859-860-861-862-863-865-866-867-868-869-870-871-873",
    "872-927-967-1020-1036-1071-1074-1075-1076-1077-1078-1079-1080-1081-1082-1083-1084-1085-1086-1087-1088-1089-1090-1091-1093-1094-1095-1096-1097-1098-1100-1101-1102-1103-1104-1105-1106-1107-1108-1110-1111-1112-1114-1115-1116-1117-1118-1119-1120-1121-1123-1124-1126-1127-1128-1129-1130-1131-1132-1139-1140-1141-1142-1143-1144-1146-1147-1148-1149-1150-1151-1152-1153-1154-1155-1156-1157-1159-1161-1162-1163-1166-1168",
    "907-917-920-921-924-928-931-932-933-934-939",
    "915-926-935-936-937-938-940-942-943-944-945-946-947-948-950-951-952-953-954-955-956-957-958-959-960-961-964-965-966-968-969-970-971-972-973-974-975-976-977-978-979-980-981-983-987-988-991-992-993-994-995-996-997-998-999-1000-1002-1003-1004-1005-1007-1008-1009-1010-1011-1012-1013-1015-1016-1017-1018-1019-1025-1027-1028-1029-1030",
    "990-1035-1037-1038-1039-1040-1041-1042",
    "1481-1518-1608-1746-1804-1817-1818-1819-1820-1821-1822-1823-1824-1825-1826-1828-1829-1830-1831-1832-1834-1835-1836-1837-1838-1839-1840-1841-1842-1843-1844-1845-1846-1847-1848-1849-1850-1851-1852-1853-1854-1855-1856-1857-1858-1859-1860-1861-1862-1863-1864-1866-1867-1868-1869-1870-1872-1873-1874-1875-1876-1877-1878-1879-1880-1881-1883-1884-1885-1887-1888-1889-1890-1891-1892-1893-1894-1895-1897-1900-1901-1902-1903-1904-1905-1906-1907-1908-1909-1910-1911-1912-1913-1914-1915-1917-1918-1919-1921-1922-1923-1925-1927-1928-1930-1933-1935-1937-1938-1939-1941-1943-1944-1946-1947-1948-1949-1950-1951-1952-1954-1955-1956-1957-1958-1959-1960-1961-1963-1964-1965-1966-1968-1969-1970-1971-1972-1973-1977",
    "2463-2471-2485-2486-2487-2488-2489-2490-2492-2494-2496-2497-2498-2499-2500-2501-2502-2504-2505-2506-2508-2509-2510-2511-2512-2513-2516-2519-2520-2521-2524-2525",
    "2837-2850-2858-2862-2867-2903-2906-2907-2908-2917-2919-2921-2923-2926-2928-2929-2930-2931-2932-2933-2934-2935-2936-2937-2938-2939-2940-2941-2943-2944-2945-2946-2948-2949-2950-2951-2952-2953-2956-2957-2959-2960-2961-2962-2965-2966-2967-2968-2969-2972-2973-2974-2975-2977-2978-2979-2980-2981-2982-2983-2984-2985-2987-2988-2989-2990-2991-2993-2994-2996-2997-2998-2999-3000-3003-3005-3006-3008-3009-3010-3013-3014",
    "2847-3007-3046-3079-3086-3089-3090-3091-3092-3093-3097-3098-3101-3102-3103-3104-3105-3106-3108-3109-3111-3114-3115-3116-3117-3118-3119-3120-3121-3122-3123-3124-3126-3128-3130-3131-3132-3133-3134-3135-3136-3137-3139-3140-3141-3143-3144-3145-3146-3147-3148-3150-3152-3153-3157-3158-3163-3166-3167-3169-3170-3171-3173-3174-3176-3177-3178-3179-3180-3181-3182-3183-3187-3189-3192-3195-3198-3200-3201-3202-3203-3204-3205-3207-3208-3210-3212-3215-3217-3218-3221-3222-3223-3224-3227",
    "3786-3835-3875-3881-3882-3894-3900-3909-3917-3918-3925-3935-3936-3937-3939-3941-3945-3946-3947-3949-3952-3956-3958-3960-3961-3962-3963-3964-3966-3967-3969-3971-3975-3976-3980-3981-3982-3989-3992-3993-3994-3995-3999-4001-4002-4003-4008-4009-4014-4015-4019-4020-4024-4026-4030-4032-4034-4035-4036-4038",
    "3968-3991-4010-4018-4027-4037-4077-4080-4081-4082-4087-4088-4096-4099-4100-4104-4106-4107-4109-4111-4112-4113-4116-4120-4122-4129-4131-4133-4135-4137",
    "612-632-651-652-653-656-657-658-659-660-661-663-664-665-666-667-668-669-670-671-672-673-674-675",
    "880-2088-2256-2258-2259-2260-2261-2262-2264-2265-2266-2267-2268-2269-2270-2271-2272-2273-2274-2276-2277-2278-2279-2280-2281-2284-2286-2287-2289-2290-2292",
]

for _iv in _NUMBER_INTERVALS:
    Instance.register("inngest", _iv)(Inngest)
