import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FluidSynthImageBase(Image):
    """Level 1: toolchain-only base image (SINGLE, shared across all PRs).

    dependency() returns a *string* (the Debian toolchain) and this Dockerfile
    carries NO ``# syntax`` directive, so the DockerfileEnhancer engages and
    prepends the ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image
    must NOT clone the repository -- a shared string-dependency image that
    clones is force-pinned to a single ``${BASE_COMMIT}`` and history-stripped
    by the enhancer, breaking ``git checkout`` for every other PR sharing the
    base. So the clone lives in FluidSynthImageDefault (an Image dependency,
    left verbatim by the enhancer), done per-PR. This image only provides the
    C/CMake build toolchain and is tagged with a constant ``base`` so exactly
    one base image is built for the whole repo (not one per PR base commit).

    debian:bookworm (GCC 12), NOT debian:latest (GCC 14): this dataset spans a
    wide range of FluidSynth releases (PRs from #168 up through #1756). Legacy C
    in the old releases only WARNS on GCC 12 but is a hard ERROR on GCC 14
    (-Werror=implicit-function-declaration is the default there), so the oldest
    PRs fail to compile at baseline on debian:latest. bookworm builds the range.
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
        return "debian:bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # FluidSynth is a CMake/C project; build-essential, git, make, python3
        # are already in the default package set of dockerfile() below.
        return [
            "cmake",
            "pkg-config",
            "libglib2.0-dev",
            "libsndfile1-dev",
            "libasound2-dev",
            "libpulse-dev",
            "libdbus-1-dev",
            "libsystemd-dev",
            "libreadline-dev",
        ]

    def dockerfile(self) -> str:
        base_img = self.dependency()

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # No `git clone` here on purpose (see docstring); no `# syntax` directive
        # either, so the DockerfileEnhancer injects the ARG/ENV/LABEL infra block
        # (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {base_img}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

{apt_command}

CMD ["/bin/bash"]
"""


class FluidSynthImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return FluidSynthImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

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

    def files(self) -> list[File]:
        # Shared build+test prelude, sourced by run/test/fix scripts.
        #
        # Two era-robustness concerns across the v1.1..v2.5 span this dataset
        # covers:
        #   1. Source-tree location. Ancient releases (v1.1.x, e.g. PR #168)
        #      nest the project under a `fluidsynth/` subdirectory, so there is
        #      NO root CMakeLists.txt; modern releases put it at the repo root.
        #      Detect which and configure against that source dir.
        #   2. Test invocation. The `check` custom target
        #      (ctest --output-on-failure, builds the EXCLUDE_FROM_ALL unit-test
        #      exes) only exists from ~v1.1.7 onward. Prefer it; fall back to a
        #      plain build + bare `ctest` when the target is absent so old eras
        #      degrade gracefully instead of hard-failing on "No rule to make
        #      target 'check'".
        cmake_flags = (
            "-DCMAKE_BUILD_TYPE=DEBUG -Denable-portaudio=OFF -Denable-jack=OFF"
        )
        build_and_test = """
REPO=/home/{pr.repo}
if [ -f "$REPO/CMakeLists.txt" ]; then
  SRC="$REPO"
elif [ -f "$REPO/{pr.repo}/CMakeLists.txt" ]; then
  SRC="$REPO/{pr.repo}"
else
  SRC="$REPO"
fi

rm -rf "$SRC/build"
mkdir -p "$SRC/build"
cd "$SRC/build"
cmake {cmake_flags} "$SRC" || true
# Build the library/tools first (harmless if it fails; test build below retries).
cmake --build . -j$(nproc) 2>&1 || true
# Prefer the `check` target (builds + runs the ctest suite). If it does not
# exist (old era), fall back to building all and running ctest directly.
if cmake --build . --target check -j$(nproc) 2>&1; then
  :
else
  ctest --output-on-failure 2>&1 || true
fi
""".format(pr=self.pr, cmake_flags=cmake_flags)

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
                "install.sh",
                """#!/bin/bash
# Repo is already cloned + checked out at the base commit inline in the
# Dockerfile and hardened there; install.sh only resets the working tree
# (runs before the hardening strip). The out-of-source build dir is created
# lazily by the run/test/fix scripts, which detect the correct source root.
set -e

cd /home/{pr.repo}
git reset --hard || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{build_and_test}""".format(build_and_test=build_and_test),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
{build_and_test}""".format(pr=self.pr, build_and_test=build_and_test),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
{build_and_test}""".format(pr=self.pr, build_and_test=build_and_test),
            ),
        ]


@Instance.register("FluidSynth", "fluidsynth")
class FluidSynth(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FluidSynthImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # CTest output format:
        #  N/M Test  #N: test_name ...   Passed    X.XX sec
        #  N/M Test  #N: test_name ...***Failed    X.XX sec
        #  N/M Test  #N: test_name ...***Not Run   X.XX sec
        re_pass = re.compile(
            r"^\s*\d+/\d+\s+Test\s+#?\d+:\s+(\S+)\s+[.\s]+Passed"
        )
        re_fail = re.compile(
            r"^\s*\d+/\d+\s+Test\s+#?\d+:\s+(\S+)\s+[.\s]*\*\*\*Failed"
        )
        re_skip = re.compile(
            r"^\s*\d+/\d+\s+Test\s+#?\d+:\s+(\S+)\s+[.\s]*\*\*\*Not Run"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

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

        # Deduplication: worst-result-wins
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


# number_interval values are the hyphen-joined prs_in_bundle for each selected
# PR (from FluidSynth__fluidsynth_lht_final.jsonl). Instance.create() routes on
# f"{org}/{number_interval}" whenever number_interval is set, so every interval a
# PR can carry must be registered to the same FluidSynth config (in addition to
# "FluidSynth/fluidsynth"), else create() raises "Instance ... is not registered"
# before any image is built.
_NUMBER_INTERVALS = [
    "1038-1055-1056-1067",
    "1230-1231-1236-1239-1243-1245-1251-1253",
    "1545-1546-1548-1551-1552-1554-1558-1559-1561-1565-1566",
    "513-515",
    "596-598-599-600-606-607-610-611-612-614-616-617-619",
    "734-735-736-738",
    "1159-1162-1163-1164-1166-1167-1171-1174-1181-1183-1187-1191-1194-1195",
    "1199-1287-1314-1316-1317-1318-1319-1332-1345-1346-1364-1377-1381-1385-1392-1400-1409-1414",
    "1209-1211-1213-1219-1220-1223-1225-1226-1228",
    "1416-1421-1423-1429-1430-1431-1432-1434-1435-1438",
    "1444-1447-1449-1454-1458-1459",
    "1460-1462-1468-1469-1471-1474-1476-1477",
    "1485-1486-1487-1491-1494-1499-1500-1501",
    "1514-1515-1516-1518-1520-1521-1522-1528-1533-1534-1535-1539-1540-1542",
    "1519-1553-1564-1567-1568-1570-1575-1583-1586-1587-1594-1595-1596-1597-1599-1613-1619-1620-1621-1626-1631-1635-1640-1647-1649-1652-1653-1654-1655-1656-1659-1662-1663-1664-1666-1667",
    "1673-1674-1677-1685-1687-1688-1689-1690-1691-1692-1694-1698-1699-1703-1704-1705",
    "168-169-170-173-174-176-177-178-180-183-184-187-190-193-194-196-198-199-200",
    "1709-1720-1729-1730-1731-1732-1733-1735",
    "1736-1739-1740-1742-1743-1745-1752-1756-1759",
    "461-469-471-473-480-482-484-485-486-487-488",
    "630-631-636-638-639-641-642-644-647",
    "818-823-825-826-828-834-837-839-841-844-845-848-849-854-855-858-859-860-861-868-871-873-875-876-877-878",
    "883-884-885-886-887-888-889-890-891-892-893-895-896-902-903-905-906-908-909-910-911-913-916-919-923-927-929-932-933-934-937-939",
    "901-969-970-973-977-978-982-1015-1016-1023-1032-1040-1081-1135-1136-1142-1146-1148-1152-1153-1154-1156-1158",
    "940-942-943-947-949-950-953-954-963-967-975",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("FluidSynth", _interval)(FluidSynth)
