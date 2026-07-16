import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FliptImageBase(Image):
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
        # flipt's go.mod `go` directive ranges from 1.13 (PR #194, when the
        # module was still `github.com/markphelps/flipt`) up to 1.26.0 (PR
        # #5404, after the move to `go.flipt.io/flipt`). Go is backward
        # compatible, so the newest toolchain in the dataset builds every era;
        # GOTOOLCHAIN=auto lets newer go.mod files request a different
        # toolchain if needed.
        return "golang:1.26-bookworm"

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

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GOFLAGS=-mod=mod \\
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
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class FliptImageDefault(Image):
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
        return FliptImageBase(self.pr, self._config)

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

git config --global --add safe.directory '*'
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Pre-fetch module dependencies for every go.mod that actually ships in the
# checkout. flipt is a Go multi-module monorepo: the root module plus
# sibling modules under errors/, core/, build/, rpc/flipt/, rpc/v2/*,
# sdk/go/, sdk/go/v2/, and internal/cmd/protoc-gen-*. Early-era PRs (pre
# v1.10) only ship the root module, so this loop adapts to whichever
# go.mod files exist at the checked-out commit. `|| true` keeps a missing
# or transient module from aborting the whole image build.
while IFS= read -r mod; do
  case "$mod" in
    *"/testdata/"*|*"/node_modules/"*|*"/ui/"*|*"/_tools/"*|*"/examples/"*) continue ;;
  esac
  dir="$(dirname "$mod")"
  echo "=== go mod download in $dir ==="
  ( cd "$dir" && go mod download ) || true
done < <(find . -name go.mod -not -path '*/node_modules/*')

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the flipt run/test/fix scripts.
#
# flipt-io/flipt is a Go multi-module monorepo. Module layout changes over
# the dataset's PR range (#194 -> #5404, spanning v0.11 -> v2.7):
#
#   * PR #194 era: a single root module at github.com/markphelps/flipt
#   * Modern era: root go.flipt.io/flipt plus sibling modules under
#     errors/, core/, build/, rpc/flipt/, rpc/v2/{environments,evaluation}/,
#     sdk/go/, sdk/go/v2/, internal/cmd/protoc-gen-{flipt-openapi,go-flipt-sdk}/
#
# Running `go test ./...` from the repo root would silently skip sibling
# modules in the modern era. Instead we collect the directories touched by
# the patches, walk each one up to its nearest go.mod, and run one
# `go test` per (module, package-list) group so package paths stay relative
# to that module's root. Same shape as the encore config.
#
# Non-Go trees (ui/, docs/, examples/, _tools/, testdata/) are filtered
# out -- they contain no `go test` targets exercisable in this harness.

EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf --exclude=docs/* --exclude=ui/*"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --3way $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --reject $EXCLUDES "$f" \\
    || true
}

# Walk up from $1 (a directory relative to the repo root) until a go.mod is
# found. Echoes the relative path of that module directory, or empty if
# none exists (file outside any module, e.g. docs/).
_module_dir_for() {
  local d="$1"
  while [ -n "$d" ] && [ "$d" != "." ]; do
    if [ -f "$d/go.mod" ]; then
      echo "$d"
      return 0
    fi
    d="$(dirname "$d")"
  done
  if [ -f "go.mod" ]; then
    echo "."
  fi
}

# Print "<module_dir>\\t<package_rel_to_module>" for every unique Go test
# directory touched by test.patch + fix.patch. Excludes testdata/ui/docs
# trees and any directories that don't exist on disk for the current
# checkout. Written to be safe under `set -eo pipefail`: a no-match grep /
# empty awk pipeline must not abort the script.
collect_module_packages() {
  local raw
  raw=$(
    {
      git apply --numstat --whitespace=nowarn /home/test.patch 2>/dev/null
      git apply --numstat --whitespace=nowarn /home/fix.patch 2>/dev/null
    } \\
      | awk -F'\\t' '{print $NF}' \\
      | grep -E '\\.go$' \\
      | grep -vE '(^|/)(testdata|ui|docs|examples|_tools)(/|$)' \\
      | sed -E 's#/[^/]+$##' \\
      | sort -u
  ) || true

  local d mod rel
  for d in $raw; do
    [ -n "$d" ] || continue
    [ -d "$d" ] || continue
    mod=$(_module_dir_for "$d")
    [ -n "$mod" ] || continue
    if [ "$mod" = "." ]; then
      rel="./$d"
    else
      rel="./${d#$mod/}"
    fi
    printf '%s\\t%s\\n' "$mod" "$rel"
  done | sort -u
}

run_go_tests() {
  local pairs current_mod="" pkgs=""

  # Modern era has go.work => workspace mode, which rejects -mod=mod
  # (set globally via ENV GOFLAGS in the dockerfile). Clear GOFLAGS so
  # `go test` runs with the workspace's default (-mod=readonly).
  if [ -f go.work ]; then
    export GOFLAGS=""
  fi

  pairs=$(collect_module_packages)
  if [ -z "$pairs" ]; then
    echo "No Go test packages touched by the patches; nothing to run."
    return 0
  fi

  echo "=== Touched (module, package) pairs ==="
  printf '%s\\n' "$pairs"
  echo "======================================="

  # Group consecutive lines by module (input is already sorted) and run one
  # `go test` per module so package paths stay relative to that go.mod.
  local rc=0
  while IFS=$'\\t' read -r mod rel; do
    if [ "$mod" != "$current_mod" ]; then
      if [ -n "$current_mod" ] && [ -n "$pkgs" ]; then
        echo "=== go test in $current_mod ==="
        ( cd "$current_mod" && go test -v -count=1 -timeout=1200s $pkgs ) || rc=$?
      fi
      current_mod="$mod"
      pkgs=""
    fi
    pkgs="$pkgs $rel"
  done <<< "$pairs"

  if [ -n "$current_mod" ] && [ -n "$pkgs" ]; then
    echo "=== go test in $current_mod ==="
    ( cd "$current_mod" && go test -v -count=1 -timeout=1200s $pkgs ) || rc=$?
  fi

  return $rc
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto

cd /home/{pr.repo}
source /home/common.sh

run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch
run_go_tests

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
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable.
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


@Instance.register("flipt-io", "flipt")
class Flipt(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FliptImageDefault(self.pr, self._config)

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
        # `go test` is not colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        # A package summary line ("ok   <import-path>", "FAIL <import-path>",
        # "?    <import-path>") closes the block of tests printed above it.
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        # Tests are buffered per package so the package import path can be
        # prepended -- this keeps names globally unique when several packages
        # are tested in one `go test` invocation.
        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            pass_match = re_pass.match(line)
            if pass_match:
                pending_pass.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                pending_fail.add(fail_match.group(1))
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                pending_skip.add(skip_match.group(1))
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        # Flush tests not followed by a summary line (e.g. truncated/timed-out
        # log) so they are still counted.
        flush("unknown")

        # Enforce TestResult disjointness invariants: a test reported as both
        # passed and failed (e.g. flaky retry) counts as failed.
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

# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Registered so delivered records (which carry the dash-joined number_interval)
# resolve to this class (PIPELINE §11/§11c). The bare `flipt` key above still
# routes the build-time dataset (whose number_interval is empty).
# Single-era: golang:1.26-bookworm + GOTOOLCHAIN=auto spans go.mod 1.13 -> 1.26.
_BUNDLE_NIS_FLIPT = [
    "194-195-196",
    "199-200-201-202-203-204-205-206-207-208-209-210-211-212-213-214",
    "217-218-219-220-221-222-226-227-228-230-231-233",
    "234-235-236-237-238",
    "239-240-241-242-244-246-247-248-249-250-251-252-253-254-255-256-260-261",
    "264-265-266-267-268-269-272-273-275-276-277-279-280-281-282-283-284-285-286-287",
    "288-290-291-292",
    "294-295-296-297-298",
    "299-300-301-302-303-305-306-307-308-309-310",
    "321-322-323-324-325-326",
    "357-358-359-360-361-362-363-364-365-366-369-370-371-372-376",
    "373-374-377-378-379-381-382-383",
    "384-388-389-390-392-393-397-398-404-407-408-409-410-411-412-413-414-415-416-417-419-420-422-423-424-425-427-428-429-430-431-432-433-434-436-437-438-439-440-441-442-443-444-445-447-448-449-451-452-453-454-456-457-460-463-464-465-468-469-470-471-472-473-474-475-481",
    "476-477-480-482-483-484-485-487-488-489-492-493-494-497-498-499-500-504-505-507-508-510-511-514-516-517-518-519-520",
    "496-501-512-521-522-523-524-525-526-527-529-531-532-533-534-535-537-538-539-540-541-542-544-545-546-549-552-554-558-559-560-565-566-567-569-572-574-575-578-581-582-585-587-588-590-591-592-593-594-596-597-598-599-600-601-602-604-605-607-610-611-612-613-614-617-618-619-620-621-622-625-626-627-629-630-631-632-634-635-636-639-641-643-645-646",
    "701-702-703-707-711",
    "709-722-723-724-725-726-728-729-730-731-734-735-736-737-739-740-742-746-748-750-752-753-754-755-756-757-759-764",
    "712-713-714-715-717-718-720-721",
    "761-762-763-765-768-769-770-771-772-773-774-775-776-777-778-780-781-782-783-784-785-786-787-790-791",
    "819-820-824-825",
    "854-855-856-865-866-870-877-879-880-882-884-885-886-887-888-889-890-891-896-898-899-900-901-905-906-907-908-911-912-913-915-916-918-919-920-924-925-926-927-928",
    "958-962-967-968-970-972-979-981-982-983-986-987-988-990-994-997-998-1000-1003-1004-1005-1006-1007-1008-1009-1010-1011-1012-1013-1014-1015-1016-1017-1019-1020-1026",
    "973-1018-1021-1022-1023-1024-1025-1027-1028-1029-1030-1031-1032-1033-1034-1036-1037-1038-1039-1040",
    "1041-1048-1052-1053-1055-1056-1057-1058-1059-1063-1064-1066-1067-1070-1071-1072-1073-1074-1076-1077",
    "1042-1043-1044-1045-1047",
    "1138-1139-1170-1172-1187-1188-1189-1193-1194-1196-1197-1198-1199-1200-1201-1202-1203-1204-1213-1216-1218-1219-1220-1221-1222-1224-1225-1226-1227-1228-1232-1237-1238-1240-1241-1242-1243-1244-1245-1248-1250-1251-1253-1254-1255-1257-1261-1264-1265-1266-1267",
    "1272-1273-1274-1275-1277-1278-1279-1280-1281-1282-1287",
    "1283-1286-1288-1289-1290-1291-1292-1293-1294-1295-1296-1297-1298-1299-1301-1303-1304-1305-1306-1307",
    "1367-1373-1374-1375-1376-1379-1384-1385-1387-1389-1391-1393-1394-1395-1397-1399-1400-1401-1402",
    "1736-1737-1738-1740-1741-1744-1745-1746-1747-1748-1751-1752-1753-1755-1756-1757-1759",
    "1742-1743-1749-1754-1758-1760-1761-1762-1763-1764-1765-1766-1767-1768-1769-1770-1771-1772-1773-1774-1775-1777-1778-1779-1780-1781-1782-1784-1785-1786-1787-1788-1789-1790-1791-1792-1793-1794-1795-1797-1799-1803-1804-1807-1809-1810-1811-1812-1813-1814-1815-1816-1817-1818-1819-1820",
    "1796-1821-1822-1823-1825-1828-1831-1832",
    "1824-1833-1837-1840-1841-1842-1843-1844-1847-1848-1851-1861-1864-1867-1868-1869-1874-1875-1876-1877-1878-1879-1880-1892-1893-1894-1895-1896-1897-1898-1899-1900-1901-1903-1905-1906-1907-1908-1909-1910-1912-1913-1917-1918-1919-1920-1921-1923-1924-1925-1926-1927-1928-1929-1930-1931-1932",
    "1915-1934-1935-1936-1937-1938-1939-1941-1945-1946-1947-1948-1953-1954-1955-1956-1957-1958-1959-1960-1961-1962-1963-1964-1965-1966-1970-1973-1974-1975-1976-1977-1978-1979-1980-1983-1984-1985-1986-1987-1988-1989-1990-1991-1992-1993-1995-1996-1997-1998-1999-2000-2001-2002-2003-2005",
    "2007-2008-2009-2010-2011-2013-2014-2015-2016-2018-2019-2020-2021-2026-2027-2028-2029-2030-2031-2032-2033-2034-2035-2036-2037-2038-2039-2040-2041-2042-2043-2044-2045-2046-2048-2049-2050-2051-2052-2054-2056",
    "2055-2057-2058",
    "2118-2119-2120-2121-2128-2129-2130-2131-2132-2134-2136-2137-2138-2139-2140-2141-2142-2143-2144",
    "2133-2135-2145-2146-2147-2148-2150-2151-2152-2154-2155-2156-2157-2158-2159-2160-2161-2162-2163-2164-2165-2166-2167-2168-2169-2170-2171-2172-2173-2174-2176-2177-2180-2182-2183-2184-2185-2186-2187-2188-2189-2190",
    "2262-2265-2266-2267-2268-2269-2272-2273-2278-2279-2280-2281-2282-2283-2285-2286-2290-2293-2295-2298-2299-2300-2301-2303-2304-2305-2306-2307-2308-2309-2310-2311-2312-2313-2314-2315-2316-2318",
    "2297-2319-2320-2321-2325-2328-2331-2332-2333-2334-2336-2338-2339-2340-2341-2342-2343-2344-2345-2346-2347-2352-2355-2359-2360-2363-2365-2366-2368-2369-2370-2371-2372-2373-2374-2375-2376-2377-2378-2379-2380-2381-2382-2383-2384-2387-2388-2391-2393-2394-2395-2396-2397-2398",
    "2401-2405",
    "2406-2408-2410-2411-2412-2413-2414-2415-2417-2422-2424-2426-2430-2431-2432-2438-2439-2440-2441-2443-2445-2446-2449-2450-2451-2452-2453-2454-2455-2456-2459-2460-2461-2462-2463-2464-2465-2466-2467-2468-2469",
    "2470-2472-2473-2474-2476-2478-2480-2481-2482-2484-2485-2486-2487-2488-2489-2490-2491-2492-2493-2494-2495-2496-2502-2503-2505-2506-2508-2509-2512-2513-2515-2516-2517-2518-2519-2520-2521-2524-2525-2527",
    "2894-2908",
    "2962-2994-2998-3036-3039-3040-3044-3046-3047-3048-3049-3050-3051-3052-3053-3054-3055-3057-3059-3063-3064-3065-3066-3067-3068-3069-3070-3071-3073-3074-3075-3077-3078-3079-3081-3082-3083-3084",
    "3008-3088-3090-3091-3102-3103-3107-3109-3110-3111-3112-3114-3117-3118-3119-3121-3129-3130-3133-3136-3137-3138-3139",
    "3033-3034-3035",
    "3085-3086-3092-3093-3094-3095-3096-3097-3100",
    "3089-3113-3123-3124-3128-3135-3140-3141-3143-3148-3149-3150-3151-3152-3153-3154-3155-3156-3157-3159-3160-3161-3162-3163-3164-3165-3166-3167-3169-3170-3171-3173",
    "3326-3330-3331-3334",
    "3442-3443-3444-3452-3453-3454-3456-3459-3460-3461-3462-3463-3464-3465-3466-3468-3469-3470-3472",
    "3467-3473-3474-3475-3479-3480-3481-3482-3483-3484-3485-3486-3487-3488-3492-3493-3494-3495-3497-3499-3500-3501-3502-3503-3504-3505-3506-3507-3508-3509-3511-3512",
    "3568-3569-3570-3571-3573-3575-3576-3578-3579-3580-3581-3582-3583-3584-3585-3586-3587-3588-3589-3591-3592-3595-3596-3597-3598",
    "3689-3691-3693-3694-3695-3696-3697-3698-3699-3700-3701-3702-3703-3704-3705-3706-3707-3708-3709",
    "3781-3792-3793-3794-3795-3796-3797-3798-3799-3800-3801-3802-3803-3804-3805-3808-3809-3810",
    "3813-3814-3815-3816-3817-3818-3819-3820-3821-3822-3823-3824-3827-3830-3833-3837-3838-3839-3840-3841-3842-3843-3845-3846-3847-3848-3849-3853-3856-3857-3860-3861-3864-3865-3866-3867-3868-3869-3870",
    "3844-3884-3890-3891-3892-3893-3894-3895-3896-3897-3899-3900-3901-3929-3930-3931-3932-3934-3935-3936-3937-3938-3944-3945-3946-3947-3948-3950-3951-3952-3953-3954-3955-3956-3957-3958-3959-3960-3962-3963-3964-3965-3966-3967-3968-3969-3970-3971-3973-3975-3976-3977",
    "4063-4067-4070-4071-4072-4073-4074-4075-4076-4077-4078-4079-4080-4081-4083-4087-4088-4102-4103-4104-4105-4106-4107-4108-4109-4110-4111-4112-4113-4114-4115-4116-4117-4124-4127-4128-4129-4130-4131-4132-4133-4134-4135-4136-4137-4140-4142-4151-4158-4159-4160-4161-4162-4163-4164-4165-4166-4167-4168-4169-4178-4184-4185",
    "4186-4189-4190-4191-4192-4193-4194-4195-4196-4197-4198-4199-4214-4215-4216-4217-4218-4219-4220-4221-4222-4223-4224-4229-4231-4233",
    "4234-4242-4243-4244-4245-4246-4247-4249-4250-4251-4253",
    "4254-4255-4261-4275-4276-4277-4278-4279-4280-4281-4282-4283-4284-4285-4286-4288-4290-4292-4294",
    "4295-4297-4298-4299-4300-4301-4302-4303-4304-4305-4306-4307-4343",
    "4369-4386-4388-4389-4391-4392-4393-4394-4395-4397-4398-4399-4400-4401-4402-4403-4404-4405-4412-4416-4417-4418-4419-4420-4421-4422-4423-4425-4430-4431-4433-4434-4436-4437-4438-4440-4441-4442-4443-4445-4446-4447-4458-4460-4499-4500-4501",
    "4502-4503-4543-4586-4592-4595-4597-4600-4601-4607-4610-4612",
    "4526-4565-4570-4572-4573-4575-4576-4577-4578-4579-4580-4581-4582-4594-4602-4603-4604",
    "4547-4548-4549-4553-4555-4557-4558-4559-4561-4562-4564",
    "4608-4609-4611-4613-4614-4615-4616-4617-4618-4624-4625-4626-4627-4628-4629-4630-4631-4632-4633-4634-4635-4636-4637-4638-4640-4641-4642-4643-4644-4647-4648-4649-4650-4651-4652-4654-4655-4656-4657-4658-4659-4668-4670-4671-4673-4675-4685-4686-4687-4688-4689-4690-4691-4692-4693-4694-4695-4696-4697-4699-4700-4701-4702-4703-4704-4705-4706-4708-4709-4711-4712-4714-4718-4719",
    "4683-4715-4721-4730-4731-4733-4736-4737-4739-4741-4742-4744-4745-4746",
    "5144-5257-5258-5259-5260-5261-5262-5263-5264-5265-5272-5286-5288-5289-5290-5291-5292-5293-5294-5295-5298-5300-5301-5302-5313-5315-5316-5317-5318-5319-5320-5327-5331-5335-5339-5340-5341-5342-5343-5344-5345-5347-5348-5362-5364",
    "5404-5407-5408-5411-5412-5414-5415-5416-5417-5418-5419-5420-5421-5422-5423-5424-5425-5427-5428-5430-5431-5434-5435-5436-5437-5438-5461-5462-5463-5464-5465-5466-5467-5469-5470-5471-5478-5479-5481-5499-5501",
]
for _ni in _BUNDLE_NIS_FLIPT:
    Instance.register("flipt-io", _ni)(Flipt)
