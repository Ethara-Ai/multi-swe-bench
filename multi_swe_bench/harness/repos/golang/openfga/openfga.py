import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# openfga/openfga — a fine-grained authorization (Zanzibar-style) server (Go).
#
# Discovery (dataset analysis):
#  - 90-PR Go range #2..#3064, single Go module, base ref `main`.
#  - Test files under cmd/, pkg/, internal/, server/, storage/, tests/.
#    Median 8 test packages per PR, max 34 — moderate scale (not tfaws/mimir).
#  - openfga's storage backends include cgo SQLite, so CGO_ENABLED=1 and a
#    C toolchain are required.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs each. Runs are fenced with `### OFGAPKG ###`
#    markers so test ids stay unique across packages. tests/check and
#    tests/listobjects packages may need running storage backends — they
#    fail/skip without them; the pkg/* unit tests are the resolvable signal.
#
# Registry shape (aligned with harness/image.py — see the two class docstrings):
#  - OpenfgaImageBase    = ONE shared toolchain-only base (no clone). Every PR's
#    per-PR image builds FROM it, so the base is built once and reused.
#  - OpenfgaImageDefault = per-PR image that clones full history, checks out its
#    own base.sha, warms the build cache, then applies the canonical hardening
#    strip. This is the pin-and-strip-safe layout (see OpenfgaImageBase docstring).


# The integration suites under tests/ are table-driven from shared YAML fixtures
# in assets/tests/. They own no *_test.go of their own, so the "package owns a
# changed *_test.go" rule below never selects them -- yet a test patch that only
# extends those YAML files puts the entire fix-gated signal there. Verified on
# pr-637: test.patch adds 36 YAML lines and the fix rewrites ingress computation
# in internal/graph/graph.go; internal/graph's own tests pass either way, but
# tests/listobjects goes 121P/5F without the fix -> 126P/0F with it. 28 of the 90
# instances touch these fixtures, so this was a large blind spot.
_FIXTURE_PREFIX = "assets/tests/"
_FIXTURE_PKGS = (
    "tests/check",
    "tests/oldcheck",
    "tests/listobjects",
    "tests/listusers",
    "tests/writemodel",
    "tests/model",
)

# Those suites boot a server per storage engine. RunDatastoreTestContainer maps
# the engine name as follows (pkg/testfixtures/storage/storage.go):
#   memory   -> in-process, no external anything
#   sqlite   -> local test database file, no external anything
#   postgres -> pulls postgres:14 and creates a container via the Docker API
#   mysql    -> same, via the Docker API
# There is no env hook to point the fixtures at a pre-provisioned database, so
# the postgres/mysql variants cannot run inside an eval container and are fenced
# out. Memory AND SQLite both run clean (verified: TestCheckSQLite ok in 3.7s),
# so both are included. tests/writemodel predates the engine-suffix convention
# but already hardcodes Engine="memory".
_FIXTURE_RUN = "Memory|SQLite|Sqlite|WriteAuthorizationModel"


def _patch_paths(patch: str) -> list[str]:
    """Repo-relative paths touched by a unified diff."""
    paths: list[str] = []
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        paths.append(parts[2][2:] if parts[2].startswith("a/") else parts[2])
    return paths


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for path in _patch_paths(patch):
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


def _fixture_pkgs(patch: str) -> list[str]:
    """Fixture-driven integration suites to add when a patch edits their YAML.

    Returned separately from _test_pkgs() (rather than merged into it) so the
    memory-engine `-run` fence applies ONLY to packages pulled in by this rule.
    A suite explicitly selected because its own *_test.go changed keeps running
    unfenced, exactly as before -- this must not regress already-resolving
    instances such as pr-2875, whose tests/authzen entry points carry no
    "Memory" in their names.
    """
    if not any(p.startswith(_FIXTURE_PREFIX) and p.endswith((".yaml", ".yml"))
               for p in _patch_paths(patch)):
        return []
    owned = set(_test_pkgs(patch))
    return [p for p in _FIXTURE_PKGS if p not in owned]


# Single Go toolchain image shared by every PR. golang:1-bookworm is the latest
# 1.x on Debian bookworm; combined with GOTOOLCHAIN=auto it builds the whole
# #2..#3064 range (older modules build forward-compatibly, newer modules pull
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
# fix/test-patch state) so the three eval runs start compiled and need not fetch
# anything externally. Runs BEFORE the hardening strip; every step is
# best-effort so a flaky baseline never breaks the image build.
_INSTALL_SH = """#!/bin/bash
set -uxo pipefail
git config --global --add safe.directory /home/__REPO__ || true
cd /home/__REPO__

# base.sha is already checked out by the Dockerfile. Warm module + build caches
# (this also triggers GOTOOLCHAIN=auto to download this PR's toolchain).
go mod download 2>/dev/null || true
go build ./... >/dev/null 2>&1 || true
# Compile (but do not run) the test binaries so cgo-SQLite and the test-only
# deps land in the build cache too. Bounded so a slow package cannot hang build.
timeout 1200 go test -run='^$' -count=1 -vet=off ./... >/dev/null 2>&1 || true

# Pre-cache module deps + toolchain introduced by the patches so the fix stage
# need not reach the network. Apply best-effort, warm, then reset.
git apply --3way --whitespace=nowarn /home/fix.patch >/dev/null 2>&1 || true
git apply --3way --whitespace=nowarn /home/test.patch >/dev/null 2>&1 || true
go mod download 2>/dev/null || true
go build ./... >/dev/null 2>&1 || true
git reset --hard >/dev/null 2>&1 || true
git checkout . >/dev/null 2>&1 || true
exit 0
"""

# Shared per-package runner: `go test` each package that owns a changed
# `*_test.go`, fenced with `### OFGAPKG ###` so parse_log keeps ids unique
# across packages. -vet=off keeps vet-only failures from masking the real
# outcome.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### OFGAPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done

# Fixture-driven integration suites (only when the patch edits assets/tests/*.yaml).
# Fenced to the memory-engine entry points so no live Postgres/MySQL is needed.
for pkg in __FIXTPKGS__; do
  [ -d "$pkg" ] || continue
  echo "### OFGAPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m -run '__FIXTRUN__' "./$pkg/" 2>&1 || true
done
"""

# Baseline: clean base.sha, no patches. base.sha stays checkout-able after the
# hardening strip because it is HEAD (reachable, not pruned).
_RUN_SH = """#!/bin/bash
set -uxo pipefail
export CI=true
cd /home/__REPO__
git reset --hard
git checkout __SHA__
bash /home/run_tests.sh
"""

# Test patch only: the new tests exercise behaviour the fix has not introduced
# yet, so they fail (or their package fails to compile) -- genuine f2p / n2p.
_TEST_RUN_SH = """#!/bin/bash
set -uxo pipefail
export CI=true
cd /home/__REPO__
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
cd /home/__REPO__
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

# Binary/asset diffs in the patches would abort `git apply`; skip them so the
# source hunks still apply.
_EXCLUDES = (
    "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
    "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
    "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
)


class OpenfgaImageBase(Image):
    """Level 1: toolchain-only base image, shared by every PR.

    ``dependency()`` returns a *string* (the Go toolchain image), so the
    pipeline's ``DockerfileEnhancer`` engages and prepends the
    ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image must NOT clone
    the repository -- a shared string-dependency image that performs a
    ``git clone`` is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer (``_standardize_repo_fetch`` rewrites the
    clone into clone+checkout+hardening), which breaks ``git checkout`` for
    every other PR sharing the base -- the bug that produces near-zero resolved
    counts. So the clone lives in OpenfgaImageDefault (whose dependency() is an
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

# Auto-fetch whatever toolchain a given PR's go.mod requests across #2..#3064;
# install.sh warms it into the per-PR layer at build time.
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
# openfga's storage backends import cgo SQLite.
ENV CGO_ENABLED=1
# Every openfga PR before ~v1.3.4 imports "go.buf.build/openfga/go/openfga/api".
# Buf shut that hostname down, so a `direct` fetch dies on DNS ("no such host")
# and the package fails to COMPILE -- no tests run at all, every stage reports
# (0,0,0). proxy.golang.org is NOT sufficient on its own: it still *lists* those
# versions in @v/list but returns 404 for the .info/.zip (it never cached the
# content and can no longer re-fetch it from the dead origin). goproxy.cn does
# have the content (verified: v1.1.12 / v1.2.49 / v1.2.50 all HTTP 200), so it
# goes in the chain as the fallback that actually resolves them. Go tries each
# entry in order and advances on 404/410, so live modules still come from
# proxy.golang.org first and "direct" remains the last resort.
ENV GOPROXY=https://proxy.golang.org,https://goproxy.cn,direct
ENV GOSUMDB=off

CMD ["/bin/bash"]
"""


class OpenfgaImageDefault(Image):
    """Level 2: per-PR image, built on the single shared toolchain base.

    ``dependency()`` returns OpenfgaImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim -- no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ``${BASE_COMMIT}`` inline, COPYs the
    scripts, warms the build cache (install.sh), then the verbatim
    ``Image._HARDENING_BLOCK`` strips origin/refs/future history (with the four
    post-condition asserts + submodule pass) while keeping base.sha reachable.
    Embedding the block here is required: because the enhancer skips this
    Dockerfile, escaping it without hardening would leave `git log`/`git show`
    able to reveal the fix.
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
        return OpenfgaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."
        fixt_list = " ".join(_fixture_pkgs(self.pr.test_patch))

        install = _INSTALL_SH.replace("__REPO__", repo)
        run_tests = (
            _RUN_TESTS_SH.replace("__REPO__", repo)
            .replace("__PKGS__", pkg_list)
            .replace("__FIXTPKGS__", fixt_list)
            .replace("__FIXTRUN__", _FIXTURE_RUN)
        )
        run_sh = _RUN_SH.replace("__REPO__", repo).replace("__SHA__", sha)
        test_run = (
            _TEST_RUN_SH.replace("__REPO__", repo)
            .replace("__SHA__", sha)
            .replace("__EXCLUDES__", _EXCLUDES)
        )
        fix_run = (
            _FIX_RUN_SH.replace("__REPO__", repo)
            .replace("__SHA__", sha)
            .replace("__EXCLUDES__", _EXCLUDES)
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", install),
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
RUN git config --global --add safe.directory /home/{self.pr.repo} \\
    && git reset --hard && git checkout ${{BASE_COMMIT}}

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


# Every dataset record carries `number_interval` = its prs_in_bundle joined by
# "-" (e.g. "1200-1217-1218-1219"). When that field is non-empty Instance.create
# routes ONLY on f"{org}/{number_interval}" -- there is NO repo-key fallback
# (instance.py:41-42) -- so every interval in the dataset must be registered
# below or every instance dies with "Instance 'openfga/...' is not registered".
# Generated programmatically from the dataset; regenerate if the bundles change.
_NUMBER_INTERVALS = [
    "1200-1217-1218-1219",
    "1208-1209-1210-1212-1213-1214",
    "144-146-167-169-171-172-173-174-177-178-180-181-183-185-186-187-188-193-195-196-198-200-201-202",
    "1816-1837-1841",
    "2-4-14-15-16-18-19-20-22-23-26-28-29-31-32-33-34-35-36-39-42-43-45-46-47-48-52",
    "249-268-269-270-271-272-273-275-278-281-285-286-287-288-289-290-293-294-295-296-298-299-302-303-305-306-310",
    "2524-2539-2542-2543-2544-2545-2546-2547-2548-2549-2550-2551-2552-2553-2555-2556-2557-2561-2562-2565-2566-2567-2568-2569-2572-2574-2576-2577-2580-2582",
    "2638-2701-2703-2709-2710-2716-2719-2721-2724",
    "2664-2977-2978-2981-2984-2988-2992",
    "2668-2693-2717-2725-2727-2731-2736-2737-2740",
    "27-41-53-54-58-59-61",
    "2780-2781-2786-2787",
    "2789-2790",
    "2875-2968-2972-2980-2993-2994-2997",
    "2914-2922-2924",
    "3064-3068",
    "693-697-698",
    "797-829-830-831-832-834-835",
    "1015-1071-1081-1086-1087-1089-1091-1092-1093-1094-1095-1103-1106-1110-1111-1112-1119-1121-1122-1123-1124-1126",
    "1017-1027-1028-1029-1032-1036-1037-1043",
    "1023-1030-1046-1052-1064-1067-1069-1070-1073-1075-1077-1079-1083-1084",
    "1034-1041-1042-1049-1050-1053-1054-1056-1057-1059-1060-1061-1062-1063-1065",
    "1155-1267-1313-1321-1324-1325-1330-1331-1332-1333-1334-1336-1337-1340-1342-1343-1345-1346-1347-1348-1349-1350-1351-1352-1356-1361-1364-1367-1368-1369-1370-1371-1372-1373-1374-1375-1377-1379-1380-1381-1383-1384-1387-1388-1389-1390-1391-1392-1393-1397-1403-1406-1411",
    "1173-1179-1193-1226-1227-1228-1229-1230-1231-1233-1234-1235-1236-1237-1239-1240-1241-1242-1243-1244-1245-1247-1248-1250-1251-1253-1254-1257-1261-1262-1263-1264-1266-1269-1272-1273-1274-1276-1277-1278-1284-1288",
    "1220-1221-1222",
    "1292-1293-1294-1295-1297-1300-1303-1304",
    "131-135-137-138-140-141-142-143-145-147-148-153-154-155-156-158-159-160-161-162-164-165-166-168",
    "1354-1378-1404-1407-1408-1412-1413-1415-1416-1417-1418-1420-1421-1422-1423-1424-1425-1427-1430-1432-1435-1436-1437-1440-1442-1443-1444-1446-1447-1450-1453-1455-1459-1461-1462-1465-1466-1469",
    "1360-1624-1631-1639-1647-1653-1660-1666-1669-1670-1674-1678-1680-1681-1685-1687-1693",
    "1433-1521-1535-1543-1546-1547-1550-1552-1553-1554-1557-1558-1559-1560-1562-1563-1565-1566-1568-1569-1571-1576-1578-1579-1580-1585-1586-1588-1589-1592-1593-1594-1598-1600-1602-1608-1609-1610-1611-1612-1616-1617-1618-1623-1630-1633-1641-1644-1646-1648",
    "1513-1517-1520-1522-1523-1529-1531-1536-1537-1540-1541",
    "1601-1636-1654-1667-1684-1694-1696-1697-1698-1700-1705-1714-1717-1730-1731-1733-1734-1735-1737-1742-1744-1751-1754-1755-1756-1760-1761-1763-1767-1768-1774",
    "1615-1863-1888-1896-1897-1899-1900-1904-1905-1906-1907-1908-1911-1912-1915-1917-1918-1919-1925-1926",
    "1626-1762-1764-1765-1770-1773-1775-1776-1778-1780-1781-1786-1788-1789-1790-1792-1793-1795-1796",
    "1658-1784-1785-1798-1802-1804-1805-1808-1809-1811-1814-1815-1817-1818-1820-1821-1822",
    "1662-2460-2675-2702-2707-2718-2744-2825-2837-2839-2841-2842-2845-2846-2847-2848-2849-2850-2851-2853-2854-2855-2856-2857-2858-2861-2862-2863-2864-2865-2867-2868-2869-2870-2872-2873-2876-2877-2878-2882-2884-2885-2886-2887-2898-2899",
    "1688-1807-1825-1830-1831-1833-1835-1839-1840-1842-1843-1844-1845-1846-1847-1848-1849-1850-1851-1852-1853-1855-1856-1858-1859-1860-1861-1862-1865-1866-1868-1869-1872-1877-1878-1879-1880-1883-1884-1887-1889-1890-1891-1893-1894-1895-1898",
    "1838-2020-2022-2039-2045-2060-2061-2063-2064-2067-2068-2069-2071-2074-2077-2078-2079-2080-2081-2085",
    "1857-1922-1928-1929-1934-1936-1938-1939-1941-1942-1943-1946-1947-1948-1951-1953-1954-1958-1959-1960-1962-1964-1968-1972-1973-1974-1975-1976-1977-1978-1980-1981-1984-1985-1986-1987-1988",
    "1913-1923-1924-1963-1982-1983-1992-1994-1996-1997-1998-2000-2001-2002-2003-2004-2006-2008-2010-2012-2013-2017-2018-2019-2021-2023-2024-2025-2026-2027-2028-2029-2030-2031-2032-2033-2034-2035-2036-2037-2038-2040-2041-2042-2044-2046-2049-2050-2051-2052-2056-2057",
    "1927-1993-1999-2075-2076-2082-2083-2084-2086-2088-2089-2090-2091-2092-2095-2096-2102-2104-2106-2107-2110-2113-2114-2115-2116-2117-2127-2128-2129-2130-2132-2139-2143",
    "197-297-331-335-339-340-341-342-344-346-347-348-350-353-355-356-357-358-359-363-364-366-367-368-369-370-371-372-374-375-376-378-379-382-383-384-385-386-387-389-390-391-394-395-396-398-399-401-402",
    "206-210-223-225-232-240-241-242-246-247-251-252-253-255-256-257-259-261-262-263-264-266-267",
    "2100-2150-2180-2182-2190-2193-2199-2200-2210-2211-2212-2216-2217-2218-2219-2220-2221-2222-2225-2226-2228-2229-2231-2232-2234-2235-2236-2238-2239-2241-2242-2244-2245-2246-2248-2252-2255-2259-2260-2261-2262-2263-2264-2266-2267-2271-2272-2273-2274-2283-2284",
    "2103-2136-2149-2160-2167-2170-2171-2172-2173-2175-2176-2177-2178-2181-2183-2185-2186-2187",
    "2124-2126-2135-2140-2145-2146-2147-2152-2155-2157-2158-2159-2161-2162-2163-2164-2165-2166",
    "2184-2192-2194-2195-2197-2198-2201-2203-2206-2207-2209",
    "2230-2308-2311-2314-2317-2318-2319-2320-2321-2322-2323-2324-2325-2327-2328-2331-2332-2333",
    "2270-2281-2294-2295-2296-2297-2299-2300-2301-2302-2303-2304-2305",
    "2292-2463-2464-2471-2476-2477-2483-2484-2490-2491-2494-2497",
    "2329-2334-2336-2337-2338-2339-2340-2341-2343-2344-2345-2346-2347-2348-2349",
    "2350-2351-2352-2353-2354-2355-2356-2357-2358-2359-2360-2362-2364-2366-2367-2368-2369-2371-2374-2376-2378-2381-2382-2384-2386-2387-2388-2390-2391-2393-2394-2397-2398-2399-2401-2402",
    "2379-2385-2405-2409-2410-2411-2412-2420-2421-2425-2428-2429-2436-2437-2438",
    "2380-2400-2404",
    "2407-2414-2432-2433-2434-2435-2441-2442-2444-2447-2450-2451-2452-2453-2455-2456-2457-2458-2459-2467",
    "2474-2479-2492-2498-2499",
    "2478-2501-2505-2508",
    "2515-2581-2583-2586-2587-2588-2589-2590-2591-2592-2594-2595-2598-2601-2603-2604-2605-2607-2608-2609-2610-2611-2612-2613-2622-2623-2625-2627-2630",
    "2584-2624-2640-2642-2645-2646-2649-2652-2653-2654-2656-2657-2658-2661-2663-2670-2679-2680-2681-2682-2683-2684-2687-2688-2689-2691-2692-2694-2695-2699-2700",
    "2632-2633-2636-2637",
    "265-468-476-507-508-510-512-513-514-515-516-517-519-520-521-522-523-525-526-527-528-529-532-535-537-539-541-543-544-546-547-549-550-552-553",
    "2708-2734-2735-2738-2739-2741-2742-2746-2747-2748-2749-2750-2751-2752-2753-2755-2757-2758-2759-2760-2761-2762-2763-2764-2765-2766-2767-2770-2771-2773-2775-2777-2778",
    "2714-2921-2925-2926-2927-2928-2929-2934-2935-2936-2939",
    "276-279-307-309-311-312-315-316-317-319-320-321-322-323-324-325-326-328-329-330-332-333-334-336",
    "2779-2791-2793-2794-2795-2797-2798-2802-2805-2806-2808-2809-2813-2814-2815-2817-2819-2822-2823",
    "2811-2821-2826-2830-2832-2833-2835-2836-2838",
    "2816-2938-2942-2945-2946-2947-2949-2950-2951-2952-2953-2956-2957-2959-2960-2961-2963-2971-2974-2975",
    "2891-2892-2900-2901-2904-2905-2907-2909-2910-2912-2915-2918-2919",
    "2937-3018-3033-3062-3065-3069-3070-3073-3075-3077-3081-3084-3085-3086-3087-3090",
    "2976-3006-3016-3035-3043-3045-3046-3047-3050-3051-3053-3054-3056-3057-3058-3060",
    "2990-2998-2999-3010-3014-3015-3017-3025-3028-3030-3031-3032-3038-3040",
    "3-49-60-62-65-66-67-68-69-70-71",
    "3042-3091-3092-3093-3095-3096-3097-3102-3104-3105-3106-3111-3112",
    "351-381-405-409-412-416-418-419-420-422-423-425-426-427-428",
    "360-392-410-414-429-430-431-432-433-434-437-438-439-443-444-446-450-451-452-454-457-459-460-461",
    "417-477-488-495-498-500-501-503-504-506",
    "453-458-462-465-466-467-471-472-474-475-478-481-487-489-490-491-492-493",
    "530-551-561-564-565-567",
    "545-554-558-559-560-568-569-570-572-574-577-578-579-580-582-584-585-589-590-591-592-593-594-595-596-599-601-603-604-605-608-610-611-613-614-617-620-623-624-625-626-627-628-629-631-632-633-634",
    "587-636-641-649-652-653-655-658-659-662-663-664-665-666-667-669-671-672-673-674-675-677-679-680-683-684-687-688-689",
    "63-84-86-87-88-90-92-93-95-96-97-98-99-100-103-104-106-107-108-109-111-112-113-115-116-117-118-119-122-125-127-128",
    "637-638",
    "650-651-694-695-699-701-704-705-711-713-716-720-721-723-725-726-729-730-731-732-733-734-735-738-739-741-742-744-745-747-748-750-751-752-753-757-758-759-760-761-762-763",
    "755-766-769-770-772-776-779-781-782-784-785-786-787-788-789-790-791-792-793-794-795-796-798-799-802-807-813-815-816-817-819-820-821-823-824-826",
    "780-1066-1290-1452-1454-1456-1458-1468-1474-1476-1478-1479-1480-1483-1489-1490-1493-1499-1506-1507-1510",
    "822-833-837-838-839-840-841-842-843-844-845-846-847-849-850-851-853-854-858-859-860-862-864-865-867-872-873-881-882-884-887-888-895-898-912",
    "83-114-191-192-194-199-203-204-205-209-211-212-213-214-215-216-218-219-221-222-224-227-228-229-230-231-233-234-236-238-239",
    "880-891-900-901-902-903-904-906-907-908-909-910-911-916-917-919-921-923-924-926-927-929-931-932-940-941-943-950-952-953-958-966",
    "885-934-959-963-967-968-969-970-973-975-982-984-987-988-992-993-995-998-1000-1003-1005-1006-1008-1009-1010-1011-1016-1019-1020-1021",
    "997-1038-1055-1088-1113-1133-1134-1136-1139-1142-1143-1144-1146-1149-1150-1152-1153-1158-1159-1160-1166-1170-1171-1175-1180-1186-1187-1196-1202-1203-1204-1205",
]


@Instance.register("openfga", "openfga")
class Openfga(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenfgaImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestCheck (0.01s)
        #   --- FAIL: TestListObjects (0.02s)
        #   --- SKIP: TestPostgresIntegration (0.00s)
        # Fenced by `### OFGAPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### OFGAPKG:\s+(\S+)\s+###")

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


# Register the per-bundle interval keys too (the decorator above keeps the plain
# "openfga/openfga" key working for records that predate the number_interval
# column). Same class, so routing is identical either way.
for _iv in _NUMBER_INTERVALS:
    Instance.register("openfga", _iv)(Openfga)
