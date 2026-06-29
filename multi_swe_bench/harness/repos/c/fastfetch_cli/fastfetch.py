import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FastfetchImageBase(Image):
    """Level 1: toolchain-only base image (shared per era).

    dependency() returns a *string* (the Debian toolchain) and this Dockerfile
    carries NO ``# syntax`` directive, so the DockerfileEnhancer engages and
    prepends the ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image
    must NOT clone the repository -- a shared string-dependency image that
    clones is force-pinned to a single ``${BASE_COMMIT}`` and history-stripped
    by the enhancer, breaking ``git checkout`` for every other PR sharing the
    base. So the clone lives in FastfetchImageDefault (an Image dependency, left
    verbatim by the enhancer), done per-PR. This image only provides the C/CMake
    build toolchain.

    debian:bookworm (GCC 12), NOT debian:latest (GCC 14): this dataset spans
    fastfetch releases 1.5 .. 2.52. Legacy C in the old releases (e.g.
    tests/strbuf.c calling exit() without <stdlib.h>) only WARNS on GCC 12 but
    is a hard ERROR on GCC 14 (-Werror=implicit-function-declaration is the
    default there), so the oldest PRs fail to compile at baseline on
    debian:latest. bookworm builds every release in range.

    Upper-bound check (QC fastfetch-1785, base 2.46.0 era): the newest releases
    in range also build cleanly on bookworm's GCC 12 / libdrm -- the 2.46 GPU
    work that moves `FFGpuDriverPciBusId` from gpu_driver_specific.h into gpu.h
    compiles here, and its test-stage compile cascade (the test patch removes
    the struct definition while callers in gpu_linux.c/gpu_nvidia.c still
    reference it) is the intended f2p signal, resolved by the fix patch. So
    debian:bookworm is the single compatible base pin for the whole 1.5..2.52
    span; do NOT bump it to debian:latest.
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
        # fastfetch is a CMake/C project; build-essential and git are in the
        # default package set below.
        return ["cmake", "pkg-config"]

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


class FastfetchImageDefault(Image):

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
        return FastfetchImageBase(self.pr, self._config)

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
        cmake_configure = "cmake -DBUILD_TESTS=ON -DCMAKE_BUILD_TYPE=Debug .."

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
# Dockerfile and hardened there; install.sh only resets the working tree and
# creates the out-of-source CMake build dir (runs before the hardening strip).
set -e

cd /home/{pr.repo}
git reset --hard || true

# QC fix (fastfetch-361 and other pre-1.8.0 bundles): src/util/textModifier.h
# was introduced in release 1.8.0. The test patch for older bundles adds
# `#include "util/textModifier.h"` to tests/list.c and tests/strbuf.c, and the
# fix patch creates the header as a NEW file -- which a partial `git apply
# --reject` of the large release-bundled patch can silently drop. Without the
# header the test/fix stage fails at compile time on a missing constants header
# (a spurious cascade) instead of on the genuine API change (test-strbuf still
# fails to compile at base on the missing ffStrbufEqualS/ffStrbufContainC).
# Create it as an untracked file when absent so the header is never the cause
# of a build break; releases that already ship it skip this (no-op). Untracked
# files survive the hardening block (it detaches/gc's but never `git clean`s).
if [ ! -f src/util/textModifier.h ]; then
  mkdir -p src/util
  cat > src/util/textModifier.h <<'FF_TEXTMOD_EOF'
#pragma once

#ifndef FASTFETCH_INCLUDED_TEXT_MODIFIER
#define FASTFETCH_INCLUDED_TEXT_MODIFIER

#define FASTFETCH_TEXT_MODIFIER_BOLT  "\\033[1m"
#define FASTFETCH_TEXT_MODIFIER_ERROR "\\033[1;31m"
#define FASTFETCH_TEXT_MODIFIER_RESET "\\033[0m"

#endif
FF_TEXTMOD_EOF
fi

mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}

mkdir -p build
cd build
{cmake_configure}
cmake --build . -j$(nproc)
ctest --output-on-failure || true
""".format(pr=self.pr, cmake_configure=cmake_configure),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

mkdir -p build
cd build
{cmake_configure} || true
cmake --build . -j$(nproc) -- -k 2>&1 || true
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr, cmake_configure=cmake_configure),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

mkdir -p build
cd build
{cmake_configure} || true
cmake --build . -j$(nproc) -- -k 2>&1 || true
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr, cmake_configure=cmake_configure),
            ),
        ]


@Instance.register("fastfetch-cli", "fastfetch")
class Fastfetch(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FastfetchImageDefault(self.pr, self._config)

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

        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test = pass_match.group(1).strip()
                    passed_tests.add(test)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test = fail_match.group(1).strip()
                    failed_tests.add(test)

        # Deduplicate: if a test appears in both passed and failed, treat as failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# number_interval values are the hyphen-joined prs_in_bundle for each selected
# PR (from fastfetch-cli__fastfetch_lht_final.jsonl). Instance.create() routes on
# f"{org}/{number_interval}", so every interval that a PR can carry must be
# registered to the same Fastfetch config (in addition to "fastfetch-cli/fastfetch").
_NUMBER_INTERVALS = [
    # QC bundle fastfetch-cli__fastfetch_361-1785.jsonl carries these two
    # intervals (different bundling than the lht_final set below). They MUST be
    # registered or Instance.create() raises "Instance ... is not registered"
    # before any image is built.
    "361-362-363",
    "1785-1789-1796-1802-1804-1806-1811-1813",
    "2218-2219-2222-2228-2237-2238-2245",
    "2105-2115-2119-2121-2123-2124-2127-2129-2135-2136-2138",
    "1459-1874-1950-1960-1967-1969-1970-1973-1974",
    "1939-1941-1947-1951",
    "1895-1908-1915-1919-1924-1926-1927-1928-1929-1935",
    "1875-1881-1882-1883-1887-1888-1890-1891-1894",
    "1855-1861-1862-1864-1865-1868-1869-1871",
    "1782-1828-1830-1832-1837-1843-1845-1847",
    "1773-1785-1787-1789-1790-1796-1801-1802-1804-1805-1806-1807-1811",
    "1754-1759-1760-1761-1764-1765",
    "1694-1695-1697-1700-1701-1709-1716-1717-1718-1720-1721",
    "1527-1533-1535-1538-1539-1540-1541-1543-1547-1550",
    "771-1413-1465-1466-1467-1468-1472-1479-1481-1483-1484-1485-1486-1487",
    "1393-1405-1406-1408-1409-1410-1413-1415-1418-1420-1421-1422-1424",
    "830-1373-1376-1377-1381-1382-1383-1385-1386-1387-1390-1395-1398-1400",
    "1085-1267-1276-1283-1295-1301",
    "1217-1219-1222-1228-1229-1233-1236-1237",
    "1066-1110-1135-1138-1140-1142-1144-1149-1150-1152-1154",
    "1090-1095-1097-1098-1101-1102-1107-1108",
    "679-682-684",
    "618-620-623-624-625-626-631-632-633-637-639-641-642-644",
    "195-371-373-374-376-378-381-389-398",
    "336-359-361-363",
    "234-235-236-237-239-242-244-245-246-247-249-251-253-254-255-257-258-259-260-263-266-269-270-271-273-274",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("fastfetch-cli", _interval)(Fastfetch)
