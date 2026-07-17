from __future__ import annotations

"""fluxcd/flagger — era 2 registry config (PRs 201.., number_interval='flagger_201_to_99999').

Era 2 is the Go-modules era: every base commit ships a `go.mod`, so no GOPATH
layout is needed (contrast era 1, see flagger_0_to_200.py). The module path
migrates mid-era (github.com/weaveworks/flagger -> github.com/fluxcd/flagger at
PR #765), but module mode resolves that from go.mod itself -- no special
handling required.

TWO TOOLCHAINS, ONE ERA KEY
---------------------------
The dataset assigns all 60 era-2 records the single number_interval
'flagger_201_to_99999', but they do not share a toolchain. Measured on the real
base shas (`go build ./pkg/...`):

    PRs 207..333  (go.mod: 1.12/1.13)  -- FAIL on modern Go:
        github.com/solo-io/go-utils@v0.7.11 requires
        k8s.io/apiextensions-apiserver@v0.0.0-...+incompatible:
        invalid version: +incompatible suffix not allowed
      Modern Go's module resolver rejects this dependency graph outright; the
      failure is in resolution, not compilation, so no build flag avoids it.
      golang:1.14 builds these (8/9 -- see pr-304 below).

    PRs 346..1811 (go.mod: 1.13 -> 1.25.0) -- 51/51 build on golang:1.26.

Rather than split the era key (which would require editing the dataset's
number_interval), this single Instance class emits two shared base images
selected by pr.number. The harness keys bases by image_tag, so each PR layer
depends on the correct one and each base is still built exactly once.

    PRs <= 333  ->  golang:1.14        (tag base-go114), module mode, no GOFLAGS
    PRs >= 346  ->  golang:1.26-bookworm (tag base-go126), GOFLAGS=-mod=mod,
                    GOTOOLCHAIN=auto

`-mod=mod` is deliberately NOT set on the go114 base: Go 1.14 accepts the flag
but the era's oldest commit (#207) still ships a vendor/ tree, and letting Go
apply its own default (vendor when present, mod otherwise) is what was verified
to build.

KNOWN UNBUILDABLE: pr-304
-------------------------
pr-304 fails identically on golang:1.13, 1.14 and 1.15 with
    github.com/apache/thrift@v0.12.0 used for two different module paths
    (git.apache.org/thrift.git and github.com/apache/thrift)
That is a defect in the repository's own go.mod at that commit, not a toolchain
or harness issue. Resolving it would require injecting a `replace` directive,
i.e. altering the record under test, so it is left to fail honestly.

- Tests: go test -v -count=1 ./pkg/...
- Parse: standard Go test output (--- PASS/FAIL/SKIP: TestName)
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Highest PR number that must build on the legacy (golang:1.14) toolchain.
# Measured boundary: pr-333 is the newest failing on modern Go, pr-346 the
# oldest passing.
_LEGACY_MAX_PR = 333


def _is_legacy(pr: PullRequest) -> bool:
    return pr.number <= _LEGACY_MAX_PR


class FlaggerEra2ImageBase(Image):
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
        return "golang:1.14" if _is_legacy(self.pr) else "golang:1.26-bookworm"

    def image_tag(self) -> str:
        return "base-go114" if _is_legacy(self.pr) else "base-go126"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo
        legacy = _is_legacy(self.pr)

        # golang:1.14 is Debian buster, whose apt repos are archived -- an
        # `apt-get update` there fails the build. Everything needed is already
        # in the image (git 2.20.1, gcc, ca-certificates), so the install step
        # is emitted only for the modern bookworm base.
        install = (
            ""
            if legacy
            else """RUN apt-get update && apt-get install -y --no-install-recommends \\
    git gcc ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

"""
        )

        # Go 1.14 predates GOTOOLCHAIN entirely, and setting -mod=mod there
        # would override the vendor/ tree that pr-207 still ships.
        goenv = (
            ""
            if legacy
            else """ \\
    GOFLAGS=-mod=mod \\
    GOTOOLCHAIN=auto"""
        )

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the shared base to a single
        # PR's base.sha and breaking every other PR sharing it with
        # "reference is not a tree". The base keeps full history; the strict
        # anti-reward-hack hardening runs per-PR (see FlaggerEra2ImageDefault).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC{goenv}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
{install}RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class FlaggerEra2ImageDefault(Image):
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
        return FlaggerEra2ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

git config --global --add safe.directory '*'
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Pre-fetch dependencies so the test stages spend their time running tests.
# `|| true` keeps a transient proxy hiccup from failing the whole image build;
# a genuinely unresolvable graph will surface in the run/test/fix stages.
go mod download || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared patch-apply helper for the flagger era-2 run scripts.
#
# `git apply` is atomic: one unappliable hunk aborts the entire patch and the
# stage silently reports 0 tests. flagger's patches carry binary blobs under
# docs/ (PNG/JPG diagrams and packaged .tgz charts) which cannot affect
# `go test ./pkg/...`, so they are excluded rather than risked.

EXCLUDES="--exclude=docs/* --exclude=*.png --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.gif --exclude=*.svg --exclude=*.ico --exclude=*.tgz \
--exclude=*.pdf --exclude=*.lock"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f"
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
go test -v -count=1 ./pkg/... 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

source /home/common.sh
cd /home/{repo}
apply_patch /home/test.patch
go test -v -count=1 ./pkg/... 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

source /home/common.sh
cd /home/{repo}
apply_patch /home/test.patch
apply_patch /home/fix.patch
go test -v -count=1 ./pkg/... 2>&1
""".format(repo=self.pr.repo),
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

CMD ["/bin/bash"]
"""


@Instance.register("fluxcd", "flagger_201_to_99999")
class Flagger201To99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FlaggerEra2ImageDefault(self.pr, self._config)

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
# resolve to this class (PIPELINE §11/§11b). The era key "flagger_201_to_99999" above
# still routes the build-time dataset, whose number_interval is the era tag.
_BUNDLE_NIS_FLAGGER_ERA2 = [
    "207-210-211-212-216-217-218-219-220-221-222-224",  # pr-207 (12 PRs)
    "227-228-229-230-231-232-234-235-236-238",  # pr-227 (10 PRs)
    "240-248-251-253-254-255-257",  # pr-240 (7 PRs)
    "271-272-275-276-278-280-281-282",  # pr-271 (8 PRs)
    "286-289-293-295-296-297",  # pr-286 (6 PRs)
    "304-305-306-307-310-314-315-316",  # pr-304 (8 PRs)
    "311-324-325-326-327-331",  # pr-311 (6 PRs)
    "317-318-319-320-322-323",  # pr-317 (6 PRs)
    "333-334-336-338-340-342-343-344",  # pr-333 (8 PRs)
    "346-350-353-354",  # pr-346 (4 PRs)
    "356-358-359-363-364",  # pr-356 (5 PRs)
    "372-373-378-380-383-384-389",  # pr-372 (7 PRs)
    "386-390-391-394-397-399-400",  # pr-386 (7 PRs)
    "401-406-407-408-409-411-412",  # pr-401 (7 PRs)
    "419-423-424-425-429-430-433-436-438-440-441-442-446-447-448-449-450-454-455-457-460-461-462-463-464-467-469-471-472-474-475-476-479-480-481-483-484-485-486-489-490-492-493-494-495-500-502-504-506-507-509-511-512-514-516-519-520-521-524-526-528-529-530-531-534-535-536-537-538-539-540-541-543-544-546-547-548-549-557-559-560-561-565-571-575-576-579-581-584-585-586-587-588-589-592-593-594-596-598-601-604-605-607-608-609-611-612-615-617-621-623-624",  # pr-419 (112 PRs)
    "652-654-661-663-667-668-670-671-672-674",  # pr-652 (10 PRs)
    "679-681-684-685-691-692-695-702",  # pr-679 (8 PRs)
    "704-709-714-715-718-721-725-726-729-731-733-734",  # pr-704 (12 PRs)
    "735-736-740-741",  # pr-735 (4 PRs)
    "749-754-755-756-762-763-764-766",  # pr-749 (8 PRs)
    "765-770-771-772-774",  # pr-765 (5 PRs)
    "777-778-781-782",  # pr-777 (4 PRs)
    "783-785-788-794",  # pr-783 (4 PRs)
    "792-796-798-799-800-805-806-812-813",  # pr-792 (9 PRs)
    "867-872-876-877-878-879-881-884-887-895-896-897",  # pr-867 (12 PRs)
    "894-898-900-902-907-908-909",  # pr-894 (7 PRs)
    "912-914-915-916-919",  # pr-912 (5 PRs)
    "917-920-921-922-924",  # pr-917 (5 PRs)
    "925-932-934-935-936-937",  # pr-925 (6 PRs)
    "939-940-941",  # pr-939 (3 PRs)
    "943-952-953-955-958-959-960-964-966-975-977-978-979-980-982-983-984-985",  # pr-943 (18 PRs)
    "986-987-990-991-1012-1013-1015-1016-1018-1019",  # pr-986 (10 PRs)
    "1001-1020-1022-1023-1025-1034-1036-1038-1043",  # pr-1001 (9 PRs)
    "1041-1085-1091-1093-1094",  # pr-1041 (5 PRs)
    "1044-1045-1047-1048-1049-1052-1057-1058",  # pr-1044 (8 PRs)
    "1092-1095-1100-1102-1103-1105-1106-1107",  # pr-1092 (8 PRs)
    "1108-1110-1116-1117-1119-1125-1128-1130-1131-1138",  # pr-1108 (10 PRs)
    "1139-1142-1143-1144-1145-1146-1148-1156-1162-1164-1171-1172",  # pr-1139 (12 PRs)
    "1150-1181-1183-1187-1188-1189",  # pr-1150 (6 PRs)
    "1185-1191-1204-1205-1208-1210-1211-1212-1215-1216-1219-1220-1221-1222-1223-1224-1228",  # pr-1185 (17 PRs)
    "1233-1239-1241-1242-1243-1244",  # pr-1233 (6 PRs)
    "1264-1265-1267-1270-1275-1276-1279-1282-1283-1284",  # pr-1264 (10 PRs)
    "1280-1302-1306-1313",  # pr-1280 (4 PRs)
    "1281-1324-1326-1328-1331",  # pr-1281 (5 PRs)
    "1316-1319-1321-1322-1323",  # pr-1316 (5 PRs)
    "1332-1338-1340-1343-1352-1354",  # pr-1332 (6 PRs)
    "1346-1355-1356-1359-1361-1362-1364-1366-1370-1371-1372-1373-1374-1375",  # pr-1346 (14 PRs)
    "1385-1392-1393-1394-1398-1402-1405-1406-1408-1411-1412-1413",  # pr-1385 (12 PRs)
    "1442-1443-1446-1451-1452-1456-1461-1466-1470-1476-1477-1483-1485-1489-1490-1491-1492-1493-1494-1495-1496-1497-1498-1499",  # pr-1442 (24 PRs)
    "1502-1505-1506-1507-1512-1513-1516-1517-1518-1521-1522-1524-1525-1528",  # pr-1502 (14 PRs)
    "1511-1555-1564-1570-1571-1572-1574-1576-1582-1589-1590-1593-1594-1595-1596",  # pr-1511 (15 PRs)
    "1529-1537-1538-1540-1541-1545-1549-1552-1557-1558-1559-1560",  # pr-1529 (12 PRs)
    "1597-1598-1599-1603-1606-1607-1608",  # pr-1597 (7 PRs)
    "1602-1610-1611-1614-1617-1620-1621-1622-1623-1624",  # pr-1602 (10 PRs)
    "1628-1630-1634-1637-1638-1648-1649-1653-1656-1657-1666-1675-1676-1683-1686-1690-1691",  # pr-1628 (17 PRs)
    "1677-1682-1707-1751-1755-1756-1757-1763-1771-1776-1783-1784-1785-1786-1787-1788",  # pr-1677 (16 PRs)
    "1702-1709-1711-1713-1721-1723-1724-1725-1726-1727-1728-1730",  # pr-1702 (12 PRs)
    "1731-1733-1735-1744-1745-1746-1747-1749",  # pr-1731 (8 PRs)
    "1739-1791-1792-1797-1803-1812-1823-1826-1828-1831-1832-1835-1836-1842-1843-1844-1845-1846-1847",  # pr-1739 (19 PRs)
    "1811-1851-1858-1861-1862-1863-1868-1870-1874-1875-1878-1880-1885-1887-1894-1895-1897-1900-1901-1902-1903-1904-1906-1908",  # pr-1811 (24 PRs)
]

for _ni in _BUNDLE_NIS_FLAGGER_ERA2:
    Instance.register("fluxcd", _ni)(Flagger201To99999)
