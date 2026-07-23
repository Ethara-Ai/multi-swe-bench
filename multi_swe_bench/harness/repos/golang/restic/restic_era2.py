"""Restic harness for Era 2 — cmd/internal layout, GOPATH mode with symlink.

Covers number_interval: restic_era2
PRs: 1040, 1240, 1431, 1483, 1494, 1556, 1719, 1729, 1780, 1802 (base versions v0.7.2–v0.9.5)

Requires symlink: /gopath/src/github.com/restic/restic -> /home/restic
Test command: GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./cmd/... ./internal/...
            run from /gopath/src/github.com/restic/restic (NOT /home/restic)

WHY THE GO COMMANDS RUN FROM THE SYMLINK PATH
---------------------------------------------
Era 1 vendors dependencies gb-style, as vendor/src/<pkg>, so pointing GOPATH at
/home/restic/vendor works there. Era 2 (v0.7.3 onward, dep/Gopkg.toml) switched
to the STANDARD Go vendor layout -- vendor/<pkg>, with no vendor/src -- so that
same GOPATH entry contributes nothing. The toolchain only honours a standard
vendor/ directory when the package being built lives inside GOPATH/src, and
running from /home/restic puts it outside. The result is that every dependency
goes unresolved:

    internal/crypto/kdf.go:10:2: cannot find package "golang.org/x/crypto/scrypt"
        /home/restic/vendor/src/golang.org/x/crypto/scrypt (from $GOPATH)

The base image already symlinks the checkout to /gopath/src/github.com/restic/
restic; cd-ing there with GOPATH=/gopath makes vendor/ resolve and every era-2
base commit compile. Verified: 10/10 era-2 base shas build EXIT 0.

Git operations stay on /home/restic (the real path); only the go commands move.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ResticEra2ImageBase(Image):
    """Toolchain + full-history checkout + /gopath symlink, shared by era 2.

    ``image_tag()`` is the constant ``"base-era2"``, so ONE image serves all ten
    era-2 bundles while each carries a different ``base.sha``. The ``# syntax``
    directive makes ``DockerfileEnhancer.enhance()`` return this content
    verbatim, which stops the enhancer's ``_standardize_repo_fetch`` from
    rewriting the clone below into ``git clone`` + ``git checkout
    ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK``.

    Beyond pinning a shared tag to one PR's commit, that rewrite is doubly wrong
    here: it appends its own ``CMD ["/bin/bash"]`` at the substitution point, so
    the ``ln -sf`` that builds /gopath/src/github.com/restic/restic would land
    AFTER the CMD, and the GOPATH layout every era-2 test command depends on
    would be assembled in a stanza the rewrite reorders.

    Full history is kept; only the network remote is dropped. Per-PR hardening
    lives in ResticEra2ImageDefault.
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
        # PINNED, not golang:latest. Two independent ceilings apply here.
        #
        # 1.11+ is out: era-2 code (2017-2018) does an in-place AES-CTR decrypt
        # with overlapping buffers, and every Go >= 1.11 panics on it with
        # "crypto/cipher: invalid buffer overlap". On go1.26 it is worse --
        # cmd/restic and internal/backend/test fail to BUILD (16 ok / 13 FAIL).
        #
        # 1.10 is also out, more subtly: Go 1.10 started running `go vet`
        # automatically as part of `go test`, and vet rejects a pre-existing bug
        # in restic's own test file at these commits --
        #   internal/restic/node_unix_test.go:104: Skipf format %v reads arg #1,
        #   but call has only 0 args
        # -- so internal/restic reports "[build failed]" and its whole test
        # package is lost from all three run stages. Go 1.9 predates auto-vet.
        #
        # Verified on all 10 era-2 base shas at 1.9: build EXIT 0 and
        # internal/restic compiles for every one. Measured on the pr-1040 base
        # via the real run.sh: 27 ok / 1 FAIL at 1.10 vs a clean compile at 1.9.
        # Available for linux/arm64 as well as amd64.
        return "golang:1.9"

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Validated before interpolation into the clone URL / WORKDIR paths.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

RUN mkdir -p /gopath/src/github.com/{org}

WORKDIR /home/

{fetch}

RUN ln -sf /home/{repo} /gopath/src/github.com/{org}/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ResticEra2ImageDefault(Image):
    """Per-PR grading image for era 2 — this tier carries the hardening.

    ``prepare.sh`` checks this PR's ``base.sha`` out of the shared base's full
    history; ``Image._HARDENING_BLOCK`` then detaches at that literal sha and
    strips every other ref, the reflogs and all unreachable objects, so the PR's
    fix commit is not recoverable from git inside the image.
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

    def dependency(self) -> Image | None:
        return ResticEra2ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def gopath_dir(self) -> str:
        """The GOPATH-internal symlink the base image creates for this repo.

        Every `go` command must run from here rather than /home/<repo>, or the
        standard vendor/ directory is ignored and nothing resolves (see the
        module docstring). Components are validated before interpolation so a
        crafted org/repo cannot inject shell into the generated scripts.
        """
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        return f"/gopath/src/github.com/{org}/{repo}"

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

cd {gopath_dir}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./cmd/... ./internal/... || true

""".format(pr=self.pr, gopath_dir=self.gopath_dir()),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd {gopath_dir}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./cmd/... ./internal/...

""".format(gopath_dir=self.gopath_dir()),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cd {gopath_dir}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./cmd/... ./internal/...

""".format(pr=self.pr, gopath_dir=self.gopath_dir()),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd {gopath_dir}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./cmd/... ./internal/...

""".format(pr=self.pr, gopath_dir=self.gopath_dir()),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # dependency() is an Image, so DockerfileEnhancer returns this content
        # verbatim and injects nothing -- the hardening must be emitted here.
        # ${BASE_COMMIT} is substituted with the literal sha because the pipeline
        # only passes REPO_URL/BASE_COMMIT build args to string-dependency images.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("restic", "restic_era2")
class ResticEra2(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ResticEra2ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

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
# instance.py routes on f"{org}/{number_interval}" whenever number_interval is
# set, so every dash-joined bundle value a record can carry must resolve to a
# class. These are the era 2 (cmd/internal, GOPATH + symlink) bundles. Without them a delivered
# jsonl that carries number_interval raises "Instance 'restic/<bundle>' is not
# registered" before a single image is built. The bare "restic/restic_era2" key
# registered above still routes records whose number_interval is empty.
#
# Explicit dash-joined member lists, never ranges -- the bundles are sparse.
_BUNDLE_NIS_RESTIC_ERA2 = [
    "1040-1249-1257-1258-1259-1260-1262-1263-1265-1266-1267-1269-1270-1272-1275-1276-1277-1279-1281-1282-1285-1287-1295-1298-1299-1300-1301-1304-1311-1312-1314-1315-1316-1317-1319-1320-1321-1324-1325-1326-1327-1333-1334-1336-1337-1339-1340-1343-1345-1346-1352-1353-1358-1360-1362-1365-1368-1373-1374-1381-1384-1387-1389-1390-1391-1393-1394-1395-1397-1398-1399-1400-1406-1410-1414-1415-1423-1426-1428-1437-1443-1445-1446",  # pr-1040 (83 PRs, v0.7.3..v0.8.0)
    "1240-1241-1243-1244-1245-1247-1248-1250-1254",  # pr-1240 (9 PRs, v0.7.2..v0.7.3)
    "1431-1436-1439-1447-1452-1454-1459-1461-1462-1464-1465-1468-1469-1471-1475-1476-1481-1482-1488-1491-1493-1499-1501-1503-1504",  # pr-1431 (25 PRs, v0.8.0..v0.8.1)
    "1483-1507-1511-1518-1524-1529-1530-1534-1535-1536-1538-1548-1549-1554-1564-1568-1569-1571-1573-1574-1575-1577-1579-1580-1582-1583-1584-1588-1589-1592-1594-1595-1598-1603-1613-1615-1616",  # pr-1483 (37 PRs, v0.8.1..v0.8.2)
    "1494-1552-1639-1647-1648-1649-1650-1651-1653-1657-1660-1661-1662-1667-1668-1669-1673-1675-1676-1679-1684-1686-1692-1693-1695-1696-1698-1699-1702-1703-1705-1709-1712-1715-1718-1720-1731-1735-1741-1742-1744-1746-1748-1749-1750-1751-1754-1757-1764-1765-1767-1769-1770-1773-1774-1776-1778-1779-1781-1782-1784-1787-1791-1794",  # pr-1494 (64 PRs, v0.8.3..v0.9.0)
    "1556-1560-1611-1623-1624-1625-1634-1635-1636-1638-1640-1643",  # pr-1556 (12 PRs, v0.8.2..v0.8.3)
    "1719-1962-2017-2039-2042-2043-2044-2050-2053-2054-2055-2056-2057-2066-2068-2070-2081-2082-2086-2088-2090-2094-2095-2098-2099-2100-2103-2108-2111-2120-2130-2137",  # pr-1719 (32 PRs, v0.9.3..v0.9.4)
    "1729-1772-1841-1844-1845-1846-1848-1851-1853-1855-1856-1858-1861-1882-1885-1887-1888-1889-1894-1899-1900-1901-1902-1913-1914-1915-1919-1921-1922-1924-1927",  # pr-1729 (31 PRs, v0.9.1..v0.9.2)
    "1780-1876-1892-1912-1920-1941-1942-1946-1948-1949-1950-1953-1955-1961-1970-1971-1973-1975-1980-1982-1983-1991-1992-1993-2002-2005-2006-2009-2018-2019-2020-2022-2025-2026-2027-2029-2031-2033-2036",  # pr-1780 (39 PRs, v0.9.2..v0.9.3)
    "1802-1806-1815-1820-1821-1824-1827-1828-1835-1836-1837-1839",  # pr-1802 (12 PRs, v0.9.0..v0.9.1)
]

for _ni in _BUNDLE_NIS_RESTIC_ERA2:
    Instance.register("restic", _ni)(ResticEra2)
