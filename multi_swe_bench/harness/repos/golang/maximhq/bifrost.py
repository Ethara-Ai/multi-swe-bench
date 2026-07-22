import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Bundle-NI registration config for the maximhq/bifrost `_lht_final` dataset
# (81 release-delta bundles, #27..#3154, bifrost `transports/v1.1.x..v1.5.x`
# release lines). Each JSONL record has prs_in_bundle populated; number_interval
# is the sorted dash-joined bundle (Rule 5 Pattern 2). Routing:
# Instance._registry["maximhq/<NI>"] = Bifrost for all 81 NIs (_BUNDLE_NIS).
#
# bifrost is a Go MULTI-MODULE monorepo: there is no root go.mod; instead each
# top-level component is its own module (`core/`, `transports/`, `framework/`,
# `cli/`, one per `plugins/<name>/`, one per `tests/<name>/`). The module path
# maps cleanly to the directory: `github.com/maximhq/bifrost/<X>` lives in
# `<X>/`. The module LAYOUT evolves across the #27..#3154 span (Docker-verified
# by `find -name go.mod` at three base commits):
#   * Oldest era (~#27): only THREE modules -- core/, plugins/, transports/
#     (plugins is a single module, not yet split per-plugin).
#   * Modern era (#3154): ~20 modules -- core, framework, cli, transports,
#     plugins/* split into one module each, tests/* split into one module each.
# Because the layout is discovered dynamically at runtime (group_pkgs.sh walks
# up from each test file to its nearest go.mod), the SAME script set is correct
# for every era -- no per-era config files are needed (uniform config, like
# dolthub/dolt and toeverything/AFFiNE).
#
# CROSS-MODULE FIX VISIBILITY (the key wrinkle). A single PR bundle commonly
# changes source in one module (e.g. core/) while its test.patch adds/edits
# `_test.go` files in a DIFFERENT module (e.g. tests/core-providers/,
# plugins/semanticcache/, transports/...). The sibling modules depend on
# PUBLISHED bifrost versions via the module proxy -- e.g. `require
# github.com/maximhq/bifrost/core v1.1.8`, with the local `replace ... =>
# ../../core` directive COMMENTED OUT in the committed go.mod (Docker-verified
# at #157). Left as-is, a fix.patch to local core/ source would be invisible to
# a tests/core-providers test and fix-run would equal test-run. So before each
# test run inject_replace.sh rewrites every `github.com/maximhq/bifrost/*`
# requirement in the target module's go.mod to a local `replace` pointing at
# the in-tree sibling dir, then `go mod tidy` -- making every test build
# against the patched local checkout. Docker-verified: #157 tests/core-providers
# with core replaced => /home/bifrost/core builds + runs.
#
# Many bifrost suites are LIVE-API integration tests (tests/core-providers/*,
# plugins/maxim, core/tests/vertex_test.go, ...) that call OpenAI/Anthropic/
# Bedrock/Vertex/Maxim and need real keys (os.Getenv("OPENAI_API_KEY") ...).
# Without keys they compile and run but FAIL ("no keys found that support
# model: ...") -- this is inherent to the dataset (its f2p/p2p/s2p sets are all
# empty; the harness re-derives them from real run output). The config still
# runs them and parse_log captures the standard go-test FAIL lines; the
# meaningful, key-free unit suites (config-merge, schemas, retries, hashing,
# core/internal/mcptests with mock MCP servers, framework/*, ...) PASS and are
# captured as `--- PASS:`.
#
# No system packages required: every touched module compiled on the bare
# golang:1.25-bookworm image (pure-Go deps incl. aws-sdk-go-v2; no CGO). Multi-
# arch safe (golang official images are multi-arch; verified native linux/arm64,
# amd64 equivalent).
#
# Docker-verified on golang:1.25-bookworm + GOTOOLCHAIN=auto:
#   * #768 base 3d960239 (transports/v1.3.18..19, core/go.mod `go 1.24.0`):
#       test.patch core/bifrost_test.go -> module=core, pkg=. ; replace inject
#       + tidy + `go test .` ran TestExecuteRequestWithRetries_* with full
#       `--- PASS:` incl. subtests.
#   * #3154 base 833f312a (transports/v1.5.1..2, newest): 30 _test.go files
#       across core/internal/mcptests, core/mcp, plugins/logging, framework,
#       transports; group_pkgs.sh fanned them to their modules and emitted
#       standard `--- PASS:` (TestLoadConfig_*, TestSchema*, ...) per module.
#   * #157 base e8af2e36 (tests/core-providers, live-API): replace core =>
#       local built+ran; testify FAILs ("no keys found...") captured in
#       standard `--- FAIL: TestAnthropic/SimpleChat` form (no-key dataset
#       reality).
#   * #29 base f9147ae6 (oldest 3-module era, core/tests/vertex_test.go) and
#       #31 base d1051297 (plugins/maxim, single plugins module): both compiled
#       + ran via the same script set (FAIL on missing keys, not build errors),
#       confirming the dynamic grouping works at the oldest layout too.
# parse_log is the proven hashicorp/Go matcher (see nomad.py / dolt.py /
# gqlgen.py): standard `--- PASS|FAIL|SKIP: <name>` lines, validated against the
# captured output above.
# ---------------------------------------------------------------------------


class BifrostImageBase(Image):
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
        # Newest stable Go major. With GOTOOLCHAIN=auto this builds every
        # go.mod `go` directive in the dataset (1.24.x .. 1.25) directly and
        # auto-fetches the exact toolchain for any newer pin a later v1.5.x
        # bumps to.
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

        if self.config.need_clone:
            code = f"RUN git clone --no-single-branch https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label_block = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{label_block}

{self.global_env}

ENV GOTOOLCHAIN=auto
# Module graph is rewritten at run time (inject_replace.sh adds local
# `replace` directives + `go mod tidy`), so the build must be allowed to
# update go.mod/go.sum.
ENV GOFLAGS=-mod=mod

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class BifrostImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    def _get_test_files(self) -> str:
        """Repo-root-relative `_test.go` paths added/modified by this PR's
        test.patch, as a single space-separated string.

        The bash scripts iterate this list and group entries by their nearest
        ancestor go.mod (group_pkgs.sh) so each touched test package is run
        from the root of the module that actually owns it -- which is what
        bifrost's multi-module, layout-varies-by-era monorepo requires.

        Skips non-`_test.go` files (templates, .go support files, .json, ...)
        and `+++ /dev/null` deletions. Returns "" when no _test.go is touched;
        the run scripts then no-op (6/81 bundles only edit test *helper* .go
        files and have nothing test-gated to run -- consistent with the
        dataset's empty f2p sets).
        """
        test_files = set()
        for match in re.finditer(r"^\+\+\+ b/(.+)$", self.pr.test_patch, re.M):
            fpath = match.group(1).strip()
            # strip a trailing tab+timestamp some diff tools append
            fpath = fpath.split("\t")[0].strip()
            if not fpath.endswith("_test.go"):
                continue
            test_files.add(fpath)
        return " ".join(sorted(test_files))

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return BifrostImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        # MUST be `pr-<number>` with the number immediately after `pr-`:
        # gen_report.collect_report_tasks parses it as
        # int(dir.name[3:].split('-')[0]). PR numbers are globally unique.
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = self._get_test_files()
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
            # Group the PR's test files by the directory of their nearest
            # ancestor go.mod. Each output line:  "<mod_root> <pkg>"
            #   mod_root = absolute path of the owning module (so we can `cd`)
            #   pkg      = "." (test file sits in the module root dir) or
            #              "./<rel>" (test file in a subpackage of the module)
            # Deduplicated so each (mod_root, pkg) pair appears at most once.
            # Files with no ancestor go.mod are skipped (shouldn't happen --
            # every bifrost source dir lives under some module).
            #
            # NOTE the `dir == found` branch: unlike gqlgen (whose touched test
            # files always sit in SUBdirectories of the module root), bifrost
            # routinely has `_test.go` directly in a module root
            # (core/bifrost_test.go, plugins/maxim/plugin_test.go, ...). Without
            # this branch the rel-strip leaves rel="core" and `go test ./core`
            # from inside the core module fails "[setup failed]" -- the bug this
            # branch fixes (Docker-verified at #768).
            File(
                ".",
                "group_pkgs.sh",
                """#!/bin/bash
set -eo pipefail

# Args: $1 = absolute repo root, $2... = repo-root-relative test files
ROOT="$1"
shift

cd "$ROOT"
declare -A SEEN

for f in "$@"; do
  [ -f "$f" ] || continue
  dir="$(dirname "$f")"
  cur="$dir"
  found=""
  while :; do
    if [ -f "$cur/go.mod" ]; then
      found="$cur"
      break
    fi
    [ "$cur" = "." ] && break
    [ "$cur" = "/" ] && break
    cur="$(dirname "$cur")"
  done
  [ -z "$found" ] && continue
  if [ "$found" = "." ]; then
    mod_root="$ROOT"
    rel="$dir"
  elif [ "$dir" = "$found" ]; then
    mod_root="$ROOT/$found"
    rel="."
  else
    mod_root="$ROOT/$found"
    rel="${dir#$found/}"
  fi
  [ -z "$rel" ] && rel="."
  if [ "$rel" = "." ]; then pkg="."; else pkg="./$rel"; fi
  key="$mod_root|$pkg"
  if [ -z "${SEEN[$key]:-}" ]; then
    SEEN[$key]=1
    echo "$mod_root $pkg"
  fi
done

""",
            ),
            # Make the patched local checkout the source of truth for the
            # module in the CURRENT directory: for every bifrost sibling module
            # required by this go.mod, add a `replace` pointing at the in-tree
            # dir, then `go mod tidy`. Without this the module would build
            # against PUBLISHED bifrost versions and a fix.patch to local
            # sibling source would be invisible (test-run == fix-run). Skips the
            # module's own path (a self-replace is meaningless) and any sibling
            # that isn't actually present as a directory+go.mod in the tree.
            File(
                ".",
                "inject_replace.sh",
                """#!/bin/bash
set -eo pipefail

# Arg: $1 = absolute repo root. cwd must already be the target module dir.
ROOT="$1"

self="$(awk '/^module /{print $2; exit}' go.mod)"

for full in $(grep -oE 'github\\.com/maximhq/bifrost/[a-zA-Z0-9/_-]+' go.mod | sort -u); do
  [ "$full" = "$self" ] && continue
  sub="${full#github.com/maximhq/bifrost/}"
  if [ -d "$ROOT/$sub" ] && [ -f "$ROOT/$sub/go.mod" ]; then
    go mod edit -replace "$full=$ROOT/$sub"
  fi
done

go mod tidy

""",
            ),
            # Strip plain `Binary files ... differ` diff blocks (git diff
            # without --binary -> no appliable content) so the remaining text
            # hunks apply atomically. Prints the cleaned patch to stdout.
            # (Same mechanism as dolthub/dolt and 99designs/gqlgen.)
            File(
                ".",
                "strip_binary.sh",
                """#!/bin/bash
set -eo pipefail
awk '
/^diff --git /{ if (blk!="") { if (!drop) printf "%s",blk } blk=""; drop=0 }
/^Binary files .* differ$/{ drop=1 }
{ blk=blk $0 ORS }
END{ if (blk!="" && !drop) printf "%s",blk }
' "$1"

""",
            ),
            # Warm-up: pin the base commit, then for each owning module pre-fetch
            # deps (with local replaces) + pre-build the target package so the
            # toolchain, module cache and build cache live in the image layer.
            # `|| true` everywhere: a target dir may not exist yet at base (added
            # by the patch) and that is fine for baseline. The replace edits +
            # tidy dirty the worktree, so the tree is reset --hard back to a
            # clean base commit at the end -- the warmed /go/pkg caches persist
            # in the image regardless.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

TEST_FILES="{test_files}"
if [ -n "$TEST_FILES" ]; then
  bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
    ( cd "$mod_root" \\
        && bash /home/inject_replace.sh "/home/{repo}" \\
        && go test -vet=off -count=1 "$pkg" ) || true
  done
fi

cd /home/{repo}
git reset --hard {base_sha}
git clean -fdq
bash /home/check_git_changes.sh

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha, test_files=test_files),
            ),
            # Baseline (no patch). Skip a package whose dir is absent at base --
            # a brand-new package added by the patch is not present here, so
            # `go test` on a missing path would abort that module's run; its
            # tests are correctly NONE at base.
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "bifrost-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  pkg_dir="${{pkg#./}}"
  if [ "$pkg_dir" != "." ] && [ ! -d "$mod_root/$pkg_dir" ]; then
    continue
  fi
  ( cd "$mod_root" \\
      && bash /home/inject_replace.sh "/home/{repo}" \\
      && go test -vet=off -count=1 -v -timeout 30m "$pkg" )
done

""".format(repo=self.pr.repo, test_files=test_files),
            ),
            # test.patch only. Apply from the repo root (patch paths are
            # repo-root-relative) after stripping unappliable binary blocks,
            # then run each owning module's target package with local replaces.
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /home/test.clean.patch
git apply --whitespace=nowarn /home/test.clean.patch
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "bifrost-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  ( cd "$mod_root" \\
      && bash /home/inject_replace.sh "/home/{repo}" \\
      && go test -vet=off -count=1 -v -timeout 30m "$pkg" )
done

""".format(repo=self.pr.repo, test_files=test_files),
            ),
            # test.patch then fix.patch applied as two separate git apply calls.
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /home/test.clean.patch
bash /home/strip_binary.sh /home/fix.patch > /home/fix.clean.patch
git apply --whitespace=nowarn /home/test.clean.patch
git apply --whitespace=nowarn /home/fix.clean.patch
TEST_FILES="{test_files}"
if [ -z "$TEST_FILES" ]; then
  echo "bifrost-runner: no _test.go files in test.patch"
  exit 0
fi

bash /home/group_pkgs.sh "/home/{repo}" $TEST_FILES | while read mod_root pkg; do
  ( cd "$mod_root" \\
      && bash /home/inject_replace.sh "/home/{repo}" \\
      && go test -vet=off -count=1 -v -timeout 30m "$pkg" )
done

""".format(repo=self.pr.repo, test_files=test_files),
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

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Bifrost(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BifrostImageDefault(self.pr, self._config)

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

        # Go test does not colorize when stdout is not a TTY (the harness
        # captures to a pipe), but strip ANSI defensively so the anchored
        # regexes below can never be defeated by escape sequences.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

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
                    base_name = get_base_name(test_name)
                    if base_name in failed_tests:
                        continue
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    passed_tests.add(base_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        passed_tests.remove(base_name)
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    failed_tests.add(base_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        continue
                    if base_name in failed_tests:
                        continue
                    skipped_tests.add(base_name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# Bundle-NI registrations (Rule 5, Pattern 2).
# Each entry is the sorted dash-joined prs_in_bundle for one JSONL record.
# Harness routes "maximhq/<number_interval>" → Bifrost via Instance._registry.
# ---------------------------------------------------------------------------
_BUNDLE_NIS = [
    "1006-1128-1131-1134-1135-1137-1139-1140",
    "1032-1035",
    "1039-1040-1048-1054-1055-1057-1059-1060-1061",
    "1044-1119-1120-1121-1169-1170-1210-1331-1357-1360-1369-1370-1371-1378-1380-1382-1384-1385-1386-1391-1392-1394-1396-1401-1402-1404-1405-1407-1408-1409-1410-1411-1413-1414-1415-1416-1418-1419-1423-1424-1425-1426-1427-1428-1431-1433-1434-1435-1436-1440-1442-1443-1444-1445-1446-1447-1448-1449-1450-1451-1454-1455-1456-1459-1460-1463-1467-1468-1469-1470-1471-1472-1473-1477-1478-1481-1482-1483-1484-1485-1486-1487-1488-1491-1492-1494-1495-1496-1497-1498-1499-1500-1502-1504-1505-1506-1507-1508-1509-1510-1511-1512-1513-1514-1515-1517-1518-1519-1520-1521-1522-1523-1525-1526-1528-1529-1530-1531-1532-1535-1536-1537-1538-1539-1541-1542-1543-1544-1545-1547-1549-1550-1553-1554-1555-1557-1558-1559-1562-1564-1565-1568-1569-1572-1573-1574-1575-1576-1578-1579-1580-1581-1584-1585-1586-1588-1589-1590-1593-1594-1595-1597-1598-1599-1600-1601-1602-1603-1604-1605-1606-1607-1608-1611-1612-1614-1615-1616-1617-1618-1619-1620-1621-1623-1624-1625-1626-1627-1628-1629-1630-1631-1632-1633-1635-1636-1637-1639-1640-1641-1642-1645-1646-1647-1648-1649-1651-1653-1654-1655-1658-1662-1663-1669-1676-1677-1680-1681-1682-1683-1686-1687-1688-1689-1690-1691-1692-1693-1694-1698-1701-1702-1704-1705-1706-1707-1709-1710-1714-1715-1718-1719-1728-1729-1730-1735-1753-1765-1766-1775-1777-1778-1779-1780-1781-1782-1783-1784-1785-1786-1787-1788-1789-1790-1791-1792-1796-1797-1798-1801-1805-1806-1808-1810-1811-1812-1815-1817-1818-1819-1820-1822-1823-1827-1830-1833-1834-1836-1837-1838-1840-1842-1843-1844-1845-1846-1849-1863-1868-1869-1870-1871-1872-1874-1876-1878-1882-1883-1887-1888-1889-1890-1891-1892-1896-1897-1901",
    "1062-1064-1065-1067-1068-1069-1070-1071-1072",
    "1095-1172-1175-1176-1178-1181-1183",
    "1108-1191-1222-1224-1225-1227-1228-1231-1236-1237-1238-1240-1243-1244-1245-1246",
    "1133-1174-1215-1216-1217-1218-1219-1221",
    "1138-1142-1143-1144-1151-1155-1156-1158",
    "1159-1162-1166-1167",
    "1165-1180-1205-1206-1207",
    "1184-1185-1186-1193-1196-1198-1199-1200-1201-1202",
    "1255-1265",
    "1268-1274-1276-1278",
    "1277-1368-1372-1373-1374-1375-1376",
    "157-160-162-166-167",
    "158-159-172-177-179-180-181-182-183-184-185-187-188-189",
    "169-170-171",
    "174-200-202-205-207-208-209-212-213",
    "1748-1768-1769-1770-1771-1772-1773-1774-1800-1839-1858-1867-1899-1934-1941-1946-1952-1956-1959-1962-1963-1964-1966-1967-1968-1969-1970-1971-1973-1975-1980-1981-1982-1985-1990-1991-1995-1996-2000-2001-2002-2003-2004-2005-2007-2009-2012-2013-2015-2018-2020-2021-2022-2023-2025-2027-2032-2033-2035-2036-2038-2040-2041-2042-2043-2044-2045-2047-2051-2052-2053-2055-2056-2057-2058-2059-2060-2061-2063-2064-2065-2066-2067-2068-2069-2071-2072-2075-2077-2082-2083-2084-2085-2086-2087-2088-2089-2090",
    "1864-1879-1884-1903-1904-1906-1907-1908-1909-1910-1913-1914-1917-1918-1919-1921-1923-1926-1928-1930-1931-1937-1939-1943-1944-1945-1947-1948-1949-1950-1951-1953",
    "1880-1986-2070-2091-2094-2097-2098-2099-2100-2101-2105-2106-2107-2108-2109-2112-2114-2115-2116-2117-2118-2119-2123-2137-2139-2140-2141-2143",
    "194-196-201",
    "1972-2429-2658-2673-2681-2691-2692-2693-2694-2697-2698-2702-2703-2706-2725-2727-2734-2736-2737-2738-2739-2746-2748-2749-2754-2770-2773-2776-2778-2780-2781-2782-2783-2784-2787-2788-2792-2809-2810-2811-2812-2813-2814-2815-2816-2817-2818-2819-2820",
    "2076-2110-2226-2232-2249-2250-2252-2259-2261-2262-2264-2265-2267-2270-2271-2273-2274-2275-2277-2284-2287-2289-2290-2291-2295-2296-2299",
    "2111-2120-2122-2133-2138-2142-2156-2165-2167-2169-2171-2174-2175-2176-2179-2182-2183-2184-2185-2186-2187-2188-2190-2193-2197-2203-2204-2205-2207-2209-2211-2212-2213-2214-2216-2218-2219-2220-2221-2223-2228-2231-2233-2235-2236-2238-2239",
    "2132-2266-2297-2319-2337-2338-2339-2340-2341-2342-2343-2355-2363-2375-2391-2393-2406-2407-2418-2422-2427-2454-2456-2458-2459-2460-2461-2462-2463-2466-2467-2468-2470-2473-2474-2475-2476-2481-2485-2486-2487-2488-2489-2493-2495-2497-2498-2499-2500-2501-2513-2518-2519-2520-2522-2523-2524-2525-2526-2527-2529-2530-2531-2532-2534-2536-2540-2550-2552-2555-2556-2560-2562-2563-2564-2568-2569-2570-2571",
    "214-217-218-220",
    "2145-2146-2147-2152-2153-2157-2163-2164",
    "2177-2253-2300-2302-2304-2308-2314-2315-2320-2322-2325-2326-2332-2334-2335-2336-2345-2357-2358-2359-2360-2362-2368-2369-2371-2374",
    "2227-2240-2242-2243-2244-2247-2248",
    "2245-2769-2785-2795-2796-2830-2831-2832-2835-2836-2838-2839-2841-2842-2844-2845-2846-2847-2848-2849-2850-2851-2854-2856-2859-2860-2861-2862-2863-2864-2870-2872-2874-2877-2878-2879",
    "2310-2353-2372-2376-2377-2379-2380-2381-2382-2385-2392-2396-2400-2401-2403-2410-2411-2413-2415-2417-2419-2421-2425-2426-2430-2431-2432",
    "238-239-263-267-268-270-272-274-278-279-281-282-283",
    "2433-2434-2435-2436-2439-2444-2445",
    "2535-2539-2553-2558-2561-2566-2576-2577-2580-2581-2582-2583-2587-2588-2591-2594-2597-2598-2604-2605-2608-2624-2636",
    "259-293-298-299-300-301-302-303-305-306-307-308-309-310-311-312-313-314-315-316-317-318-319-320-321-322-323-324-325-326-327-328-329-330-331-332-333-334-335-340-342-345-346-347-348-349-350-351-352-353-355-356-357-358-359-360-361-364-365-366-369-370-371-373-374-375-377-378-379",
    "261-262-265",
    "2640-2641-2647",
    "27-28",
    "2716-2852-2858-2898-2899-2908-2913-2918-2919-2920-2921-2922-2923-2924-2925-2926-2927-2928-2929-2932-2933-2934-2935-2936-2937-2938-2940-2941-2942-2943-2944-2945-2946-2948-2950-2956-2957-2958-2960-2961-2962-2966-2969-2980-2982-2983-2989-2991-3004-3005-3008-3009-3010-3011-3015-3018-3023-3026-3027-3028-3029",
    "2892-2953-2979-3024-3033-3036-3044-3046-3050-3051-3053-3054-3055-3056-3058-3061-3062-3063-3065-3066-3067-3068-3069-3070-3071-3072-3074-3075-3077-3079-3080-3081-3082-3083-3084-3085-3086-3087-3088-3089-3090-3094-3095-3096-3097-3098-3099-3102-3103-3104-3105-3107-3108-3109-3110-3111-3112-3113-3114-3115-3117-3118-3119-3120-3123-3124-3128-3130-3135-3136-3144-3145-3146-3147-3148",
    "29-30-35",
    "2988-3129-3133-3142-3155-3156-3157-3158-3160-3162-3163-3165-3166-3168-3173-3176-3179",
    "3076-3167-3184-3186-3194-3195-3196-3197-3204-3205-3206-3208-3209-3211-3212-3214-3215-3219-3225-3226-3228-3230-3231-3235-3236-3237-3239-3241-3242-3247-3249-3250-3251-3252-3256-3257-3260-3262-3263-3264-3265-3266-3267-3269-3270-3271-3272-3273-3275-3276-3277-3279",
    "31-32-33-34-36-37-38-39-40-41-43-44-45-46",
    "3150-3258-3280-3281-3283-3284-3285-3288-3293-3294-3297-3301-3302-3304-3306-3308-3312-3313-3315-3317-3318-3320-3321-3324-3326-3327-3328-3329-3332-3346-3348-3349-3350-3351-3352-3353-3354-3355-3356-3359-3360-3361-3366-3368-3369-3372-3373-3375-3376-3379-3380-3381-3385-3386-3387-3391-3392-3397-3398-3399",
    "3154-3200-3202-3203-3221-3338-3401-3402-3413-3418-3419-3420-3432-3433-3437-3439-3442-3446",
    "338-368-372-376-386-387-388-389-390-391-392-394-395-396-397-399-401-403-404-406-408-409-410-411-412-413-414-415-416-419-420-421-422-424-427-433-436-437-443-447-448",
    "423-450-465-466-468-469-473-475-476",
    "449-507-555-560-561-562-563-564-566-568-569-570-571-573-574-576-578-579-580-581-588-591-592-593-595-596-598-599-600-601-602-604-608-610-612-613-618-620-627",
    "462-464",
    "471-477-478-480-481",
    "495-496-499-505-506",
    "51-52-54-55-56-57-58-59-62-63-64-65-67",
    "513-514-515-525-528",
    "521-679-681-682-683-687-691-692-694-695-696-699-701-703-704-705-706-707",
    "606-623-631-633-634-635-636-637-638-640-642-643-644-646-647-648-650-651-653-654-656-657-659-661-663-665-666-668-674",
    "639-700-702-716-720-723-724-725-727-731-732-733-737-739-740-741",
    "645-676",
    "671-841-916-932-934-938-939-940-942-943-947-948-951-952-953-954-956-957-958-959-960-961-962",
    "69-74-75-76-77-78-79-80",
    "70-219-232-235-236-237-242-248-250-251-252-253-254-255-256-260",
    "709-710-711-712-713-714-717-718-719",
    "734-756-757-758-759-760-764-765-766-767",
    "742-743-744-746-747-748-749-750-751",
    "745-789-815-821-823-826-827-830-833-834-835",
    "768-770-771-774-779-780-781-782",
    "777-783",
    "794-811-813-816-817-818-820",
    "81-82-83-84-85-88-89-90-92-94-95-97-98-99-100-101-102-103-104-105-106-107-108-109-110-113-115-116-117-129-130-131-132-133-135-137-138-139-141-142-143-144-145-146-147-148-149-150-151-152-153-154-155-156",
    "824-837-839-840-842-843-844-845-846",
    "856-858-859",
    "881-890-895",
    "884-885-889",
    "886-896-897-899-900-905-906-907-908-909-910",
    "893-915-921-923-924-926-927",
    "955-992-1007-1042-1050-1053-1080-1087-1090-1094-1096-1097-1098-1099-1100-1101-1102",
    "963-964-965-966-967-968-971-972-973-975",
    "977-978-999-1000-1010-1018-1066-1077-1078",
    "984-1013-1014-1017-1021-1022-1023-1024-1025-1027-1028-1029",
]

for _ni in _BUNDLE_NIS:
    Instance._registry[f"maximhq/{_ni}"] = Bifrost
