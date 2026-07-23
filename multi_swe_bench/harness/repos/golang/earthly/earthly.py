import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Era map: lead PR -> base Go toolchain. MEASURED, not guessed: the value is
# max(go directive in go.mod at base.sha, any `go`/`toolchain` bump inside the
# fix patch) -- a fix that raises the floor must build on the raised version
# (learned on prometheus, where mapping base.sha alone broke 6 fix stages).
#
# Consolidation policy (eras only where forced):
#   * go 1.21 base serves every record targeting 1.18-1.21. The 1.21 records
#     are a HARD floor (>=1.21 refuses older toolchains); 1.18-1.20 records
#     build fine under 1.21 (2022-23 deps, language-version compat mode), so
#     they ride the same base instead of 3 extra ones.
#   * go 1.16 and 1.13 stay separate: 2020/21-era pinned deps (x/sys asm) are
#     the classic breakage under newer toolchains, and go 1.13 additionally
#     does not understand `-mod=mod` (added in 1.14).
_GO_MINOR_BY_LEAD: dict[int, str] = {
    # base go 1.13  (9 records)
    90: "1.13",
    140: "1.13",
    207: "1.13",
    261: "1.13",
    346: "1.13",
    493: "1.13",
    518: "1.13",
    647: "1.13",
    673: "1.13",
    # base go 1.16  (26 records)
    713: "1.16",
    729: "1.16",
    730: "1.16",
    743: "1.16",
    754: "1.16",
    792: "1.16",
    860: "1.16",
    935: "1.16",
    1019: "1.16",
    1042: "1.16",
    1085: "1.16",
    1136: "1.16",
    1226: "1.16",
    1454: "1.16",
    1484: "1.16",
    1551: "1.16",
    1598: "1.16",
    1642: "1.16",
    1653: "1.16",
    1682: "1.16",
    1703: "1.16",
    1825: "1.16",
    1938: "1.16",
    2023: "1.16",
    2066: "1.16",
    2113: "1.16",
    # base go 1.21  (31 records)
    1192: "1.19",  # MUST stay on exact era: its ast tests use t.Run-inside-
                   # t.Cleanup, a hard panic from Go 1.20 onward -- the one
                   # measured casualty of the 1.18-1.20 -> 1.21 consolidation
    1229: "1.21",  # target 1.19, consolidated up
    2165: "1.21",  # target 1.19, consolidated up
    2271: "1.21",  # target 1.19, consolidated up
    2285: "1.21",  # target 1.18, consolidated up
    2295: "1.21",  # target 1.18, consolidated up
    2700: "1.21",  # target 1.20, consolidated up
    2732: "1.21",  # target 1.19, consolidated up
    2789: "1.21",  # target 1.20, consolidated up
    2806: "1.21",  # target 1.20, consolidated up
    2910: "1.21",  # target 1.20, consolidated up
    2918: "1.21",  # target 1.20, consolidated up
    2961: "1.21",  # target 1.20, consolidated up
    2985: "1.21",  # target 1.20, consolidated up
    3056: "1.21",  # target 1.20, consolidated up
    3105: "1.21",  # target 1.20, consolidated up
    3156: "1.21",
    3211: "1.21",
    3229: "1.21",
    3315: "1.21",
    3317: "1.21",
    3419: "1.21",
    3580: "1.21",
    3723: "1.21",
    3884: "1.21",
    3935: "1.21",
    4024: "1.21",
    4076: "1.21",
    4117: "1.21",
    4126: "1.21",
    4199: "1.21",
}

# Fallback for any lead not in the table (e.g. dataset refresh). Newest era is
# the safe default: since Go 1.21 the go.mod `go` directive is a hard floor, so
# guessing high fails safe while guessing low fails the build outright.
_GO_MINOR_DEFAULT = "1.21"


def _go_minor(pr: PullRequest) -> str:
    return _GO_MINOR_BY_LEAD.get(pr.number, _GO_MINOR_DEFAULT)


def _sanitize_patch(patch: str) -> str:
    """Drop diff sections ``git apply`` cannot take cleanly.

    Both failure modes abort the WHOLE apply under ``set -e`` so the real code
    changes never land (proven on prometheus):

    * binary hunks (images/fonts) emitted without a full index line;
    * ``go.sum`` / ``go.work.sum`` lock-file hunks, which depend on the exact
      module graph and routinely conflict. The scripts run with
      ``GOFLAGS=-mod=mod`` (go >= 1.14 eras) or go 1.13's default auto-update
      mode, so stripped go.sum entries regenerate on demand.
    """
    if not patch:
        return patch
    kept = []
    for sec in re.split(r"(?m)(?=^diff --git )", patch):
        if not sec:
            continue
        if "Binary files " in sec or "GIT binary patch" in sec:
            continue
        m = re.match(r"diff --git a/\S+ b/(\S+)", sec)
        if m and m.group(1).rsplit("/", 1)[-1] in ("go.sum", "go.work.sum"):
            continue
        kept.append(sec)
    return "".join(kept)


class EarthlyImageBase(Image):
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
        return f"golang:{_go_minor(self.pr)}"

    def image_tag(self) -> str:
        # One SHARED base per Go era.
        return f"base-go{_go_minor(self.pr)}"

    def workdir(self) -> str:
        # MUST track image_tag(): the build-context folder derives from
        # workdir(), so a constant would make the eras overwrite each other's
        # Dockerfile (prometheus collision lesson).
        return f"base-go{_go_minor(self.pr)}"

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

        # GOFLAGS=-mod=mod lets `go test` fetch modules and WRITE missing
        # go.sum entries on demand (fix patches add imports; their go.sum
        # hunks are stripped by _sanitize_patch). The flag value only exists
        # from go 1.14 -- setting it on the go 1.13 era would break every
        # command, and 1.13's default mode already auto-updates.
        goflags_env = ""
        if _go_minor(self.pr) != "1.13":
            goflags_env = "ENV GOFLAGS=-mod=mod\n"

        # SHARED base, one per era: keeps FULL git history (no checkout, no
        # prune) so each PR layer can check out its own base.sha; dropping
        # origin unreferences upstream branches only. Strict per-PR hardening
        # happens in EarthlyImageDefault.
        #
        # `# syntax` directive = the sanctioned enhancer opt-out (PIPELINE §2).
        # Without it the enhancer would inject `git checkout ${BASE_COMMIT}` +
        # the destructive prune into this SHARED base, pinning it to whichever
        # PR built it first and breaking every other era member.
        #
        # NO apt-get: golang:1.13/1.16 are Debian buster whose apt archives
        # are gone (apt-get update fails); every golang image already ships
        # git + ca-certificates, which is all the base needs.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GOTOOLCHAIN=auto
{goflags_env}
LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

RUN git config --global --add safe.directory '*'

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


class EarthlyImageDefault(Image):
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
        return EarthlyImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _sanitize_patch(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _sanitize_patch(self.pr.test_patch),
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

# Pre-fetch module dependencies so instance runs start warm.
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the earthly run/test/fix scripts.
#
# earthly is primarily built via its own `Earthfile` (needs a running earthly
# daemon + Docker-in-Docker), but the repo also has a standard Go unit-test
# suite. The harness exercises the Go suite, scoped to packages touched by the
# patches -- Earthfile-driven integration trees (tests/, scripts/tests/,
# examples/, ...) need a live buildkit and cannot run in the eval container.

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn "$f" \\
    || git apply --whitespace=nowarn --3way "$f" \\
    || git apply --whitespace=nowarn --reject "$f" \\
    || true
}

# Unique Go package dirs touched by test.patch + fix.patch that exist on disk
# and contain Go files. Integration-test / non-Go trees are filtered out.
# Safe under `set -eo pipefail` (a no-match grep must not abort).
collect_pkgs() {
  local out d
  out=$(
    {
      git apply --numstat --whitespace=nowarn /home/test.patch 2>/dev/null
      git apply --numstat --whitespace=nowarn /home/fix.patch 2>/dev/null
    } \\
      | awk -F'\\t' '{print $NF}' \\
      | grep -E '\\.go$' \\
      | sed -E 's#/[^/]+$##' \\
      | grep -vE '^(tests|scripts|examples|contrib|docs|release|buildkitd/buildkitd-bootstrap)(/|$)' \\
      | sort -u
  ) || true
  for d in $out; do
    if [ -n "$d" ] && [ -d "$d" ]; then
      if ls "$d"/*.go >/dev/null 2>&1; then
        echo "./$d"
      fi
    fi
  done
}

# Several earthly test files are gated by `//go:build hasgitdirectory` (hash
# the working git repo) or `//go:build chaos`. Without these tags those tests
# silently vanish from the build and the harness sees `[no test files]`.
# windows/!windows tags are left alone (platform gated).
GO_BUILD_TAGS="hasgitdirectory chaos"

# earthly is a MULTI-MODULE repo in later eras (ast/go.mod,
# util/deltautil/go.mod, ...). `go test ./ast/...` from the repo root fails
# with "main module does not contain package" and kills the WHOLE invocation
# (15 records lost in the first full run). Group each touched package under
# its owning module (nearest go.mod walking up) and run `go test` inside
# that module.
run_go_tests() {
  local pkgs
  pkgs=$(collect_pkgs)
  if [ -z "$pkgs" ]; then
    echo "No Go test packages touched by the patches; nothing to run."
    return 0
  fi
  echo "=== Running go test on touched packages ==="
  echo "$pkgs"
  echo "==========================================="
  local p d mod rel rc=0
  declare -A _mod_pkgs
  for p in $pkgs; do
    d="${p#./}"
    mod="$d"
    while [ "$mod" != "." ] && [ ! -f "$mod/go.mod" ]; do
      mod=$(dirname "$mod")
    done
    if [ "$mod" = "." ]; then
      _mod_pkgs["."]="${_mod_pkgs["."]:-} ./$d"
    else
      rel="${d#$mod}"; rel="${rel#/}"; [ -z "$rel" ] && rel="."
      _mod_pkgs["$mod"]="${_mod_pkgs["$mod"]:-} ./$rel"
    fi
  done
  for mod in "${!_mod_pkgs[@]}"; do
    echo "=== module: $mod -> ${_mod_pkgs[$mod]} ==="
    ( cd "$mod" && go test -v -count=1 -timeout=1200s -tags="$GO_BUILD_TAGS" ${_mod_pkgs[$mod]} ) || rc=1
  done
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

        # Strict per-PR hardening (PIPELINE §2/§4): detach onto the literal
        # base.sha and prune every other ref so the fix cannot be recovered
        # from git history inside the container.
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("earthly", "earthly")
class Earthly(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EarthlyImageDefault(self.pr, self._config)

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
        # `go test` output isn't colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")

        # BARE Go test names, deliberately NOT package-prefixed. report.py's
        # cheating-guard matcher treats the "::"-head of a test name as a file
        # path (`file_head.endswith("/" + f)`), and this repo contains a root
        # script literally named `earthly` -- so prefixed names like
        # "github.com/earthly/earthly/cmd/earthly::TestX" false-positived the
        # guard whenever the fix patch touched that script (smoke pr-1136).
        # Bare names are the convention report.py's matchers are designed for
        # (same as the prometheus/ndarray registries).
        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

        # A name can surface in several packages (or reruns) with different
        # outcomes; collapse fail-closed with priority FAILED > ignored > ok.
        # Buckets must end pairwise-disjoint or TestResult.__post_init__ raises.
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


# === bundle number_interval routing (prs_in_bundle dash-joined, PIPELINE §11b) ===
# One key per bundle (66 keys == 66 instances). Data-derived from
# earthly__earthly_lht_final.jsonl -- regenerate if bundles change.
_BUNDLE_NIS_EARTHLY = [
    "90-91-92-93-94-95-97-98-100-101-102-105-106-108-109-110-112-113-118-119-120-121-123-125-126-127-129-130",
    "140-142-143-144-145-146-147-149-150-151-153-154-155-156-157-158-159-160-161-162-163-164-165-168-170-171-172-173-176-177-180-181-182-183-184-185-186-187-188-192-193-194-195-196-198-200-201",
    "207-208-209-210-211-212-213-214-215-216-217-220-221-224-225-226-227-228-230-235-236",
    "261-270-271-272-273-274-275-276-277",
    "346-382-393-406-407-409-410-411-412-413-414-415-417-418-420-421-423-424-425-426-427-428-429-432-433-434-435-436-437-438-441-442-443-447-448",
    "493-494-497-499-503-504-505-506-508",
    "518-552-560-565-566-567-568-569-570-571-572-573-591-592-594-595-596-597-599-600-601-602-603-606-608-609-610-611-613-616-617-618-620-621-622-624-625-626-627-630-631-632-633-635-636-637-639-642-643-644-645",
    "647-654-658-659-660-661-662-663-664-665-666-667-668-669-670-672",
    "673-674",
    "713-714-715-716-717-718-720-721-723-725-726-727-731-732-733-734-735-736-737-738-739-742-745-746",
    "729-741-747-751-756-757-758-759-761-763-767-768-769-773",
    "730-748-749-750-752-753-755",
    "743-973-975-976-978-981",
    "754-774-775-776-777-778-780-783-785-790",
    "792-793-794-795-797-800-802-803-804-805",
    "860-861-862-863-864-867-868-869-871-872-873-874-875-878-880-881-885-886",
    "935-937-939-940-943-944-945-947-948-949-950",
    "1019-1032-1033-1034-1035-1036-1037-1038-1039-1040-1041-1044-1045-1046-1047-1050-1051-1053-1054-1055-1056-1057-1058-1060",
    "1042-1158-1166-1181-1182-1183-1185-1186-1191-1193-1194-1196-1199-1204-1212-1213-1214-1216-1217-1218-1219-1224-1227-1231-1232",
    "1085-1087-1088-1090-1093-1096-1098-1100-1101-1104-1105-1106-1111",
    "1136-1138-1147-1157-1161-1162-1163-1164-1170-1171-1172-1173-1174-1176-1179-1180",
    "1192-2492-2559-2575-2587-2591-2596-2604-2613-2623-2631-2633-2638-2650-2654-2671-2679-2680-2681-2684-2693-2702-2703-2705-2708-2713-2715-2716-2718-2719-2723-2728-2730-2733",
    "1226-1234-1322-1328-1330-1341-1346-1348-1353-1354-1355-1356-1360-1361-1362-1367-1368-1369-1370-1371-1372-1373-1376-1377-1378-1381-1382-1383-1385-1387-1388-1389-1394-1400-1404-1409-1418-1423-1432-1433",
    "1229-1233-1235-1236-1237-1238-1239-1241-1244-1248-1250-1251-1252-1253-1255-1256-1259-1260-1261-1263-1265-1266-1270-1271-1275-1277-1281-1284-1287-1289-1290-1293-1296-1297-1299-1300-1301-1303-1304-1306-1307-1310-1311-1312-1313-1315-1316-1318-1319-1320-1323-1324-1331",
    "1454-1457-1459-1467-1468-1471-1472-1473-1474-1478-1479-1481-1482-1486-1487-1488-1490-1494-1495-1496-1497-1498-1504-1507-1509-1510-1512-1513-1514-1519-1521-1522-1523-1528-1530-1531-1532-1534-1535-1536-1537-1539-1541-1542-1544-1545-1548-1553-1555-1557-1559-1560",
    "1484-1493-1563-1564-1566-1567-1568-1569-1570-1577-1583",
    "1551-1561-1576-1586-1588-1591-1593-1596-1597-1599-1600-1601-1608",
    "1598-1612-1615-1617-1618-1623-1624-1626-1629-1630-1631-1633-1636-1637-1639-1640-1641-1643-1645-1649-1650-1651",
    "1642-1675-1676-1677-1678-1683-1685-1686-1688-1689-1691-1692-1694-1697-1701-1705-1706-1708-1709",
    "1653-1654-1655-1656-1657-1658-1659-1661-1662-1667-1668-1669-1671-1673-1674",
    "1682-1699-1707-1737-1750-1753-1754-1755-1756-1759-1760-1761-1762-1763-1770",
    "1703-1717-1718-1719-1725-1730-1731-1733-1734-1740-1741-1742-1746-1747-1749",
    "1825-1834-1838-1840-1841-1843-1844-1853-1854-1856-1858-1861-1871-1876-1880-1884-1885-1887-1888-1896-1900-1902-1905",
    "1938-1971-1973-1976-1977-1982-1983-1984-1985-1987-1988-1989-1990-1992-1994-1996-1998-2000-2001-2002-2003-2004-2007-2008-2009-2010-2012-2013-2016-2017-2018-2020-2021-2022-2024-2025",
    "2023-2029-2032-2033-2034-2037-2038-2040-2043-2045-2046-2048-2050-2056-2058-2060-2061-2065-2067-2068-2074-2075-2076",
    "2066-2204-2219-2222-2225-2226-2230-2231-2235-2238-2240-2242-2244-2245-2249-2251-2252-2253-2256-2258-2259-2260-2264-2266-2270-2275",
    "2113-2122-2124-2125-2127-2128-2130-2131-2133-2135-2136-2137-2138-2139-2141-2143-2152-2153-2154-2155-2156-2159-2160-2162-2163-2169",
    "2165-2166-2170-2171-2172-2173-2174-2175-2176-2177-2179-2180-2181-2182-2184-2185-2186-2187-2190-2191-2196-2197-2198-2199-2200-2201-2205-2206-2207-2211",
    "2271-2416-2428-2430-2435-2436-2437-2438-2439-2440-2441-2443-2444-2445-2448-2455-2457-2458-2459-2462-2465-2466-2467-2473-2474-2475-2479-2481-2483-2485-2486-2490-2497-2499-2500-2501-2504-2505-2506-2507-2508-2509-2511-2517-2526-2528-2529-2530-2532-2533-2534-2535-2536-2537-2538-2542-2543-2545-2546-2549-2550-2558-2561-2562-2563-2568-2570-2571-2573-2574-2576-2579-2580-2581-2582-2584-2585-2586-2590-2597-2598-2600-2601-2605-2607-2608-2609-2610-2615-2616-2617-2619-2620-2622-2627-2629-2635-2636-2639-2642-2643-2644-2645-2646-2653-2655-2658-2666-2667-2668-2670-2672-2674-2675-2676-2677-2683-2686-2687-2689",
    "2285-2289-2300-2320-2321-2323-2325-2326-2328-2331-2332-2333-2334-2335-2341-2342-2343-2344-2346-2347-2349-2352-2354-2356-2358-2360-2361-2364-2367-2370",
    "2295-2345-2359-2365-2369-2372-2373-2374-2375-2378-2379-2380-2381-2384-2392-2393-2395-2396-2397-2398-2400-2401-2402-2403-2404-2405-2407-2408-2409-2410-2411-2412-2413-2414-2415-2418-2419-2420-2421-2422-2423-2424-2425-2426-2427-2433-2434",
    "2700-2706-2709-2724-2731-2749-2750-2770-2775-2778-2788-2790-2791-2792-2793-2794-2797-2798-2801-2802-2803-2804-2807-2809-2810-2811-2813-2816-2819-2820-2822-2828-2829-2830-2835-2839-2844-2845-2846-2847-2850-2853-2854-2855-2856-2863-2864-2865",
    "2732-2734-2755-2757-2758-2760-2767-2768-2769-2772-2773-2782-2784-2785",
    "2789-2805-2814-2825-2840-2849-2893-2900-2903-2909-2912-2925-2936-2938-2951-2963-2964-2967-2974-2981-2988-2990-2994-2999",
    "2806-2836-2866-2869-2870-2874-2877-2880-2882-2888-2892-2894-2895-2897-2901-2904-2911-2919-2922-2924-2927-2928-2931-2932-2935",
    "2910-2937-2939-2944-2946-2948-2949-2950-2952",
    "2918-3023-3024-3029-3033-3036-3037-3054-3057-3059-3060-3061-3064-3070",
    "2961-2965-3001-3003",
    "2985-2995-2998-3007-3008-3011-3012-3015-3017-3018-3022-3026-3030-3031-3032-3034-3041-3047-3048-3049",
    "3056-3083-3084-3097-3101-3102-3104-3109-3110-3111-3112-3113-3115-3117-3118-3120-3122-3123-3124-3136-3140-3143-3144-3146",
    "3105-3152-3155-3161-3163",
    "3156-3162-3166-3169-3170-3172-3173-3175-3176-3177-3178-3180-3181-3182-3185-3186-3189-3193-3194-3196-3197-3202-3203-3204-3209-3210",
    "3211-3217-3218-3219-3220-3221-3224-3227-3231-3232-3236-3237-3238-3239-3240-3241-3243-3244-3245-3248-3249-3250-3251-3252-3253-3254-3257-3258-3259-3260-3261-3263-3264-3269-3270",
    "3229-3242-3266-3267-3272-3273-3276-3277-3279-3280-3283-3285-3287-3290-3293-3295-3296-3297-3299-3304-3305-3306-3307-3310-3311-3312-3313-3320-3323-3324-3325-3328-3330-3334",
    "3315-3316-3391-3426-3432-3443-3463-3464-3466-3474-3477-3480-3483-3491-3515-3520-3525-3540-3543-3544-3545-3547-3548-3551-3553-3554-3556-3558-3559-3560-3561-3562-3564-3565-3566-3568-3571-3572-3573-3574-3576-3577-3578-3579-3583-3584-3585-3586-3588-3589-3591-3593-3594-3595-3597-3598-3600-3602-3603-3604-3605-3606-3607-3608-3610-3611-3616",
    "3317-3331-3332-3335-3336-3340-3341-3342-3343-3345-3347-3348-3349-3350-3351-3353-3355-3357-3358-3359-3365-3367-3368-3369-3370-3371-3372-3373-3376-3377-3379-3380-3388-3389-3390-3392-3396-3397-3398-3404-3405-3406-3410-3414-3415-3418",
    "3419-3420-3421-3422-3424-3427-3428-3429-3430-3431-3436-3437-3444-3445-3446-3447-3448-3451-3453-3455-3457-3459-3461-3465-3467-3469-3471-3472-3478-3482-3494-3496-3497-3498-3500-3501-3502-3504-3507-3508-3510-3512-3513-3514-3516-3518-3519-3521-3522-3523-3524-3528-3529-3530-3532-3533-3537-3542",
    "3580-3613-3620-3621-3622-3625-3626-3629-3634-3637-3642-3643-3647-3648-3649-3650-3651-3652-3653-3654-3655-3656-3657-3660-3661-3662-3664-3670-3671-3672-3677-3679-3680-3681-3682-3683-3684-3685-3686-3687-3688-3690-3693-3695-3698-3699-3700-3701-3702-3704-3705-3707-3710-3711-3712-3716-3717-3719-3721-3724-3726",
    "3723-3732-3739-3741-3742-3745-3750-3751-3753-3756",
    "3884-3886-3896-3900-3906-3921-3922-3923-3927-3936-3939-3940-3941-3942-3943-3944-3945-3949-3950-3951-3952-3953-3954-3955-3956-3957-3960-3961-3962-3964-3965-3967-3969-3970-3973-3974-3975-3977-3978",
    "3935-3968-3972-3982-3984-3994-4005-4031-4033-4037-4053-4059-4060-4113-4114-4124-4125-4129-4137-4140-4149-4151-4152-4153-4154-4155-4157-4158-4165-4167-4168-4169-4171",
    "4024-4038-4039-4040-4042-4047-4064-4067-4068-4072-4073-4078-4080-4081-4082-4084-4087-4093-4094-4096-4097-4098-4104-4106-4107-4108-4111",
    "4076-4145-4170-4172-4176-4178-4179-4180-4182-4184-4185-4189-4192-4193",
    "4117-4118-4119-4132-4133-4142-4144-4148",
    "4126-4201-4202-4203-4204-4205-4206-4211-4227-4231-4236-4248-4253-4254-4258-4259-4268-4272-4283-4287-4323-4325-4326",
    "4199-4216-4222-4225-4233-4242-4246",
]
for _ni in _BUNDLE_NIS_EARTHLY:
    Instance.register("earthly", _ni)(Earthly)
