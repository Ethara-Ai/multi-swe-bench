import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks so `git apply` never aborts on a binary hunk
    with no full-index line. Safe: binary hunks touch no Go source and never
    affect test outcomes."""
    import re as _re
    sections = _re.split(r"(?=^diff --git )", patch, flags=_re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )

class EncoreImageBase(Image):
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
        # encore's go.mod `go` directive ranges from 1.18 (PR #236) up to
        # 1.25.0 (PR #2422). Go is backward compatible, so the latest
        # toolchain in the dataset builds every era; GOTOOLCHAIN=auto lets
        # newer go.mod files request a different toolchain if needed.
        return "golang:1.25-bookworm"

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
ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=auto
RUN git config --global --add safe.directory '*'

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && rm -rf /var/lib/apt/lists/*

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


class EncoreImageDefault(Image):
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
        return EncoreImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _strip_binary_diffs(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _strip_binary_diffs(self.pr.test_patch),
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
# checkout (the repo is a Go multi-module workspace: root + runtime(s)/go,
# plus testdata modules we skip). `|| true` keeps missing/optional modules
# from aborting the build.
while IFS= read -r mod; do
  case "$mod" in
    *"/testdata/"*|*"/node_modules/"*) continue ;;
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
# Shared helpers for the encore run/test/fix scripts.
#
# The encore repo is a Go multi-module workspace: a root module (encr.dev)
# plus a separate runtime module (runtime/go.mod in early PRs, runtimes/go/
# in later ones) and a miniredis integration-test module. Running
# `go test ./...` against the root would miss the submodules and waste time
# on the (very large) compiler/parser/cli surface. Instead we scope tests to
# the directories actually touched by the patches and group them by their
# enclosing go.mod so each `go test` invocation stays within one module.
# testdata modules under e2e-tests/testdata/ and cli/daemon/run/testdata/
# are skipped -- they are sample apps the harness can't exercise.

EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf --exclude=docs/*"

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
# directory touched by test.patch + fix.patch. Excludes testdata trees and
# any directories that don't exist on disk for the current checkout.
# Written to be safe under `set -eo pipefail`: a no-match grep / empty awk
# pipeline must not abort the script.
collect_module_packages() {
  local raw
  raw=$(
    {
      git apply --numstat --whitespace=nowarn /home/test.patch 2>/dev/null
      git apply --numstat --whitespace=nowarn /home/fix.patch 2>/dev/null
    } \\
      | awk -F'\\t' '{print $NF}' \\
      | grep -E '\\.go$' \\
      | grep -vE '(^|/)testdata(/|$)' \\
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
# Pin Go toolchain floor to 1.22.12 so mid-era PRs (PR ~679-1805) which transitively
# depend on golang.org/x/tools v0.21.x compile. Go >= 1.23 rejects x/tools v0.21's
# compile-time array-length assertion. `+auto` still lets newer go.mod directives
# (e.g. go 1.25.0 in PR #2422) auto-upgrade the toolchain at runtime.
export GOTOOLCHAIN=go1.22.12+auto

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
# Pin Go toolchain floor to 1.22.12 so mid-era PRs (PR ~679-1805) which transitively
# depend on golang.org/x/tools v0.21.x compile. Go >= 1.23 rejects x/tools v0.21's
# compile-time array-length assertion. `+auto` still lets newer go.mod directives
# (e.g. go 1.25.0 in PR #2422) auto-upgrade the toolchain at runtime.
export GOTOOLCHAIN=go1.22.12+auto

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
# Pin Go toolchain floor to 1.22.12 so mid-era PRs (PR ~679-1805) which transitively
# depend on golang.org/x/tools v0.21.x compile. Go >= 1.23 rejects x/tools v0.21's
# compile-time array-length assertion. `+auto` still lets newer go.mod directives
# (e.g. go 1.25.0 in PR #2422) auto-upgrade the toolchain at runtime.
export GOTOOLCHAIN=go1.22.12+auto

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


@Instance.register("encoredev", "encore")
class Encore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EncoreImageDefault(self.pr, self._config)

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


# ---------------------------------------------------------------------------
# number_interval bundle routing (prs_in_bundle dash-joined)  -- PIPELINE 11b
# ---------------------------------------------------------------------------
# Raw dataset leaves number_interval empty; delivery sets it to
# "-".join(prs_in_bundle). Single-era repo -> every bundle key routes to the
# one Encore class. Original "encoredev/encore" registration above is kept.
_BUNDLE_NIS_ENCORE = [
    "236-248-249",
    "252-253-254-255-256-259-260-263-264-269-276-278-279-281-286-287-297-298-299-300-301-302-303-304-305-306-308",
    "312-342-344-346-349-350-354-356-358",
    "313-314-316-317-318-320-321-323",
    "322-328-332-333-334-335-336-337-338-339-340",
    "360-362-364-365-366-368-370-374-375-376",
    "380-383-385-387-388-389-392-396-399-400-404-409-411-415-418-421-422-423-425-426-428-429-430-431-432-433-434-435-436-437",
    "403-458-459-460-464-466-467-468-469-470-471-472-473-474-475-476-477-479-480-481-482-483-484-485-486-487-488-489-490-491",
    "424-478-493-494-495-502-504-505",
    "439-440-442-445-446-447-448-449-450-451-452-454-455-456",
    "453-506-507-508-509-510-511-512",
    "492-601-602-603-604-606-608-609-610-612",
    "514-515-516-517-518-519-520-523-524-525-526-527-529-530-531-532-534-536-537-538-540-541",
    "528-533-539-543-546-547-549-550-551-552-555-557-559-560-561-562-564",
    "548-566-567-568-569-570-571-572-573-574-575-576-577-579-584",
    "581-583-585-587-588-589-590",
    "591-592-593",
    "611-613-614-615-616-618-619-620-622",
    "624-625-626-627-630-631",
    "629-658-664-667-668-670-674",
    "633-635-636-637-638-639-640-641-642-643-645-646-647-648-649-650-651-653-654-655-656-657",
    "671-672-673-675-676",
    "679-686-687-688-689-690-691-692-693-694",
    "681-682-683-684-685",
    "695-696-698-699-700-701",
    "702-703-705-712-715-716-717-718-720-721-722",
    "726-727-728-729-730-731-732-733-734-735-736-737-738-739",
    "741-742-743-744-745",
    "746-750-751-752-753-755-756-758-759-761-762-763-764",
    "765-766-767-768-769-770",
    "771-772-773-774",
    "775-776-777-778-779",
    "780-781-783-784-785",
    "787-788-789-790-791-792-793-794-795-796-797",
    "799-800",
    "802-803-804-805-806-807-808-809-810-811-812-813-815-816-817-818-819-820-821-822-823",
    "834-838",
    "839-840-841-842-843-844-845-846-847-849-851-852-853-854-855-856",
    "861-865-869-870-871-873-874-875-876-877-878-879-881-882",
    "864-866-867-868-872",
    "888-923-928-931-932-933-934-935-936-937-938-940-941-942-943-944-945-946-949",
    "893-894-896-897-898-899-900-901-902-903-904",
    "906-907-908-909",
    "910-912",
    "997-1015-1018-1019-1021-1023-1024-1025-1026-1028-1029-1030-1032-1036-1037-1038-1039-1040-1041-1042",
    "1001-1003-1005-1006",
    "1011-1012-1013-1014",
    "1031-1035-1044-1045-1046-1048-1049-1050-1054-1055-1056-1057-1058-1062-1063-1064-1065-1068-1069-1070-1071-1072-1073-1074",
    "1075-1076-1077",
    "1079-1080-1081-1082-1083-1084-1086",
    "1123-1124-1125-1126-1127-1128-1130-1132-1133-1134-1135-1136-1137-1138-1139-1140-1141-1142-1144-1145-1146",
    "1147-1148-1149-1150-1151-1152-1153-1154-1155-1156-1157-1158-1159-1160-1161-1162-1163-1164-1165-1166-1167-1168-1169-1170-1171-1172-1173-1174-1176-1177-1179-1182-1183-1184-1185-1186-1187-1188-1189-1190-1192-1193-1194-1195-1196-1197-1199-1200-1202",
    "1178-1236-1237-1238-1239-1240-1241-1242-1243-1244-1245-1246-1247-1248-1249-1250-1251-1252-1253-1254-1255-1256-1257-1258-1259-1261-1262-1263-1264-1265-1266-1267-1268-1269-1270-1271-1274-1276-1277-1278",
    "1203-1204-1205-1207-1208-1209-1210-1212-1213-1214-1215-1216-1217-1218-1219-1220-1221-1222-1224-1227-1228-1230-1231-1232-1233-1234",
    "1275-1280-1281-1282-1283-1284-1285-1286-1287-1288-1289-1291-1292-1293-1297-1299-1300-1301-1302-1303-1304-1305-1306-1310-1311-1312-1313-1315-1318-1319-1321-1322-1323-1324-1325-1328-1329-1330-1331-1332-1334-1335-1336-1337-1338-1339-1341-1342-1344-1345-1346-1347-1348-1349-1350-1351-1352",
    "1296-1449-1452-1454-1456-1457-1459-1460-1461-1463-1465-1468-1469",
    "1390-1396-1410-1411-1413-1415-1416-1417-1418-1419-1420-1422-1423-1424-1425-1427-1428-1429-1430-1431-1432-1433-1434-1435-1437-1438-1439-1440-1441-1442-1443-1444-1446-1447-1448-1451",
    "1426-1470-1473-1474-1476-1479-1480-1481-1482-1483-1484-1485-1486-1487-1488-1489-1491-1492-1493-1495-1496-1498-1500-1502",
    "1504-1518-1519-1522-1523-1524-1525-1526",
    "1505-1506-1507-1508-1509-1510-1511-1513-1514-1515-1516-1517",
    "1550-1559-1561-1562-1563-1564-1565-1566-1567-1568-1569-1570-1571-1573-1575-1576-1577-1578",
    "1580-1581-1582-1583-1584-1586-1588-1592",
    "1587-1589-1591-1594-1596-1597-1598-1599-1600-1601-1602-1604-1605-1606",
    "1614-1616-1619-1621",
    "1620-1631-1642-1649-1650-1652-1653-1654-1655-1660-1662-1663-1664-1665-1666-1685-1686-1687-1688-1690-1691-1694-1695-1697-1698",
    "1661-1684-1693-1706-1708-1709-1710-1712-1713-1714-1715-1716-1720-1721-1726-1727",
    "1699-1736-1737",
    "1718-1794",
    "1805-1818",
    "1847-1860-1862-1864-1865-1870-1872",
    "2016-2022",
    "2027-2032-2034-2036-2037-2039-2040-2041",
    "2042-2044-2045-2048",
    "2060-2061-2062-2065-2066-2067-2068-2070-2071-2072-2074-2076-2077-2079-2080-2082-2084-2085-2087",
    "2083-2088-2089-2090-2091-2092-2094-2096",
    "2093-2097-2123-2142-2149-2162-2163-2164-2165-2167-2169-2171-2172",
    "2109-2128-2201",
    "2173-2174-2175-2176-2182",
    "2180-2184-2185",
    "2209-2210-2220-2223-2232-2234-2236-2237",
    "2238-2241-2242-2243-2244-2246-2247-2248-2249-2251-2253",
    "2250-2254-2255-2256-2257-2259-2262",
    "2258-2285-2317-2321-2322-2324-2326-2327-2328-2329-2330-2331-2332",
    "2263-2267-2268-2271-2275-2276-2278-2279-2286-2290-2291-2292-2294-2295",
    "2284-2300-2302-2303-2305-2306-2308-2309-2310-2314-2315-2316-2318",
    "2334-2336-2338-2339-2340-2342-2343-2344-2346-2347-2349-2350-2352-2355-2356-2357-2358-2359-2360",
    "2362-2393-2397-2399-2401-2402-2403-2404-2405-2407-2409",
    "2408-2416-2420-2423",
    "2422-2438-2439-2441",
]
for _ni in _BUNDLE_NIS_ENCORE:
    Instance.register("encoredev", _ni)(Encore)
