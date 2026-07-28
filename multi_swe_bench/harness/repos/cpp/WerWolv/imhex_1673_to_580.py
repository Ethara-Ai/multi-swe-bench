"""WerWolv/ImHex -- era 2 (gcc-12 / C++23, bundles anchored at PR 580..1673).

Same two-tier hardening split as era 1 (see imhex_570_to_16.py for the full
rationale), with its own base tag so the two toolchains never share an image:

  * ``ImHexEra2ImageBase`` (``:base-cxx23``) is shared by all 18 era-2 bundles
    and therefore keeps FULL history -- clone only, no checkout, no hardening.
    Hardening it would prune to one bundle's ``${BASE_COMMIT}`` and break
    ``git checkout <sha>`` in the other 17 bundles' prepare.sh.
  * ``ImHexEra2ImageDefault`` (``:pr-<n>``) is per bundle and runs the canonical
    ``Image._HARDENING_BLOCK`` after prepare.sh has checked out this bundle's
    base.sha and populated submodules.

Both Dockerfiles carry ``# syntax=docker/dockerfile:1.6`` so
``DockerfileEnhancer.enhance()`` returns them verbatim (image.py:317); the infra
it would otherwise inject (OCI labels, the ``${REPO_URL}`` clone) is spelled out
explicitly.  No proxy ARG/ENV, CA-bundle symlinks or MITM CA secret mount are
emitted -- matching the enhancer after that logic was removed from image.py.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Loud binary-tolerant patch applier, defined once in the era-1 module and reused
# here so both eras stay byte-identical. See imhex_570_to_16.py for the full
# rationale (dataset patches lack `git diff --binary`, so binary sections are
# marker-only; this strips them, logs every drop, and fails hard on text rejects
# instead of the old silent `git apply --reject ... || true`). Importing era 1
# runs its @Instance.register side effects too, which is harmless and idempotent.
from multi_swe_bench.harness.repos.cpp.WerWolv.imhex_570_to_16 import _APPLY_PATCH_FN

# One entry per era-2 bundle: prs_in_bundle joined with "-" exactly as the
# dataset stores it (an explicit anchor-first list, NOT a first-to-last range).
# Instance.create() looks up f"{org}/{pr.number_interval}", so these strings are
# the routing keys. Generated from WerWolv__ImHex_lht_final.jsonl.
_BUNDLE_NIS = [
    "580-582-586-589-591",
    "593-608-613-615-617-620-630-633-637-642-643-644-645",
    "654-661-663-669-670-672",
    "676-677-679",
    "684-689-691-694-696-708-716-718-719",
    "722-727-732-733-734-737-739",
    "744-748-759-761-762-764-765-766-767",
    "781-784-787-789-795-797-798-800-801-803-805-807-808-810-811",
    "819-820-821-826-838-839-840-847-851-852",
    "856-859-860-861-869-870-873-878-879-889-890-892-915-916-920",
    "928-933-945-963-966-970-972-991-992-993-995-997-1001-1003-1004",
    "1036-1042-1047-1049-1050-1052-1053-1059-1061-1065-1079-1082-1086-1087-1088-1093",
    "1084-1085-1091-1094-1095-1097-1098-1099-1101-1102-1103-1104-1105-1108-1115-1116-1117-1122-1125-1129-1133-1134-1135-1149-1150",
    "1170-1171-1172-1173-1174-1183-1185-1193-1195-1197-1199-1200-1212-1214-1217-1230-1247-1248-1249-1250-1251-1253-1257-1259-1264-1267-1268-1272-1275-1276-1277-1280-1282-1286-1300-1301-1302-1307-1308",
    "1309-1322-1323-1328-1331-1332-1333-1337-1341-1342-1343-1344-1346-1354-1355-1357-1358-1359-1360-1363-1365-1366-1368-1369-1371-1377-1378-1379-1382-1388-1389-1390-1393-1395-1396-1398-1399-1400-1401-1403-1404-1406-1417-1418-1419-1420-1422-1425-1427-1429-1430-1431-1433-1436-1437-1439-1441-1442-1446-1447-1448-1451-1456-1457-1458-1459-1468-1469-1470",
    "1480-1481",
    "1486-1490-1501-1506-1508-1509-1510-1511-1512-1513-1516-1517-1518-1519-1520-1522-1523-1525-1533-1537-1539-1541-1542-1544-1556-1570",
    "1673-1698-1735-1747-1755-1769-1774",
]


class ImHexEra2ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-cxx23"

    def workdir(self) -> str:
        return "base-cxx23"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        # Unconditional clone (no `COPY {repo}` branch): build_dataset skips
        # copy_source_code when dependency() is a string, so a COPY would point at
        # a path never staged into the build context. REPO_URL arrives as a build
        # arg (build_dataset.py:616); the ARG default covers a direct docker build.
        #
        # Deliberately NO `git checkout` and NO hardening here -- see the module
        # docstring. Pruning happens per bundle in ImHexEra2ImageDefault.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        build-essential gcc-12 g++-12 lld pkg-config cmake ccache ninja-build make \\
        git ca-certificates python3 python3-dev perl autoconf automake libtool \\
        libglfw3-dev libglm-dev libmagic-dev libmbedtls-dev libfreetype-dev \\
        libdbus-1-dev libcurl4-gnutls-dev libgtk-3-dev libssl-dev libcrypto++-dev \\
        nlohmann-json3-dev libcapstone-dev libyara-dev libcli11-dev \\
        zlib1g-dev libbz2-dev liblzma-dev libzstd-dev libpsl-dev libfmt-dev \\
        libssh2-1-dev libidn2-dev libnghttp2-dev librtmp-dev libkrb5-dev libldap2-dev \\
        libarchive-dev liblz4-dev libmd4c-dev libmd4c-html0-dev libfontconfig-dev llvm-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 100 && \\
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 100

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

"""


class ImHexEra2ImageDefault(Image):
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
        return ImHexEra2ImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "init_submodules.sh",
                """#!/bin/bash
# Robust submodule init.
# Standard `git submodule update --init` silently swallows failures (network
# under QEMU, deleted submodule refs, etc.). When a submodule dir stays empty
# (or worse, has only a .git gitfile pointer and no real content), downstream
# cmake fails with "External dependency ... is empty".
#
# Strategy:
#   1) git submodule sync (refresh URL state)
#   2) git submodule update --init --recursive --force --depth=1 (twice, swallow errors)
#   3) For every .gitmodules entry, check if the path is empty *of real content*
#      (any non-hidden file/dir). If empty, rm -rf and explicit clone from URL,
#      then recursively init the clone's own submodules.
set +e

# Single pass that processes a .gitmodules-bearing directory.
init_one_repo() {
  local here="$1"
  ( cd "$here" 2>/dev/null || return 0
    [ ! -f .gitmodules ] && return 0
    git submodule sync --recursive 2>&1
    git submodule update --init --recursive --force --depth=1 2>&1
    git submodule update --init --recursive --force --depth=1 2>&1
    git config --file .gitmodules --get-regexp '^submodule\\..*\\.path$' 2>/dev/null | while read key path; do
      name=$(echo "$key" | sed 's/^submodule\\.//; s/\\.path$//')
      url=$(git config --file .gitmodules --get "submodule.${name}.url" 2>/dev/null)
      # "Empty of real content" = no non-hidden entries (ls without -A ignores dotfiles).
      # A populated submodule will have CMakeLists.txt or src/ etc.; a stale .git pointer alone
      # leaves ls "" so the fallback fires.
      visible_count=$(ls -1 "$path" 2>/dev/null | wc -l)
      if [ -n "$url" ] && { [ ! -d "$path" ] || [ "$visible_count" = "0" ]; }; then
        echo "init_submodules: fallback clone $url -> $here/$path"
        rm -rf "$path"
        git clone --depth=1 --recursive "$url" "$path" 2>&1 \\
          || git clone --recursive "$url" "$path" 2>&1 \\
          || true
        # Recurse into the newly cloned dir to init ITS nested submodules.
        init_one_repo "$path"
      fi
    done
  )
}

init_one_repo "$(pwd)"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
git clean -fdx -e build
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/init_submodules.sh
bash /home/check_git_changes.sh
mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-12 \\
  -DCMAKE_CXX_COMPILER=g++-12 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  ..
cmake --build . -j $(nproc) --target unit_tests
ctest --output-on-failure
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

{apply_fn}

apply_patch_tolerant /home/test.patch test.patch || exit 1
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-12 \\
  -DCMAKE_CXX_COMPILER=g++-12 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  .. || true
cmake --build . -j $(nproc) --target unit_tests -- -k 0 || true
ctest --output-on-failure

""".format(pr=self.pr, apply_fn=_APPLY_PATCH_FN),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

{apply_fn}

apply_patch_tolerant /home/test.patch test.patch || exit 1
apply_patch_tolerant /home/fix.patch  fix.patch  || exit 1
bash /home/init_submodules.sh
cd build
cmake -GNinja \\
  -DCMAKE_BUILD_TYPE=Release \\
  -DCMAKE_C_COMPILER=gcc-12 \\
  -DCMAKE_CXX_COMPILER=g++-12 \\
  -DIMHEX_ENABLE_UNIT_TESTS=ON \\
  -DIMHEX_OFFLINE_BUILD=ON \\
  -DIMHEX_STRICT_WARNINGS=OFF \\
  -DIMHEX_IGNORE_BAD_COMPILER=ON \\
  -DIMHEX_BUNDLE_DOTNET=OFF \\
  -DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON \\
  .. || true
cmake --build . -j $(nproc) --target unit_tests -- -k 0 || true
ctest --output-on-failure

""".format(pr=self.pr, apply_fn=_APPLY_PATCH_FN),
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

        # Harden AFTER prepare.sh: the shared base carries full history, prepare.sh
        # pins this bundle's base.sha and populates submodules, and only then is the
        # prune correct and the submodule pass meaningful. BASE_COMMIT is an ARG
        # defaulted to the sha because build_dataset passes build args only when
        # dependency() is a string and ours is an Image (build_dataset.py:616);
        # the default lets Image._HARDENING_BLOCK be used verbatim.
        #
        # The syntax directive makes DockerfileEnhancer.enhance() a no-op here
        # (image.py:317), which is why CMD is emitted explicitly.
        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("WerWolv", "imhex_1673_to_580")
class IMHEX_1673_TO_580(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImHexEra2ImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]
        re_skip_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Skipped\s*.*$"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    passed_tests.add(pass_match.group(1).strip())

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1).strip())

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    skipped_tests.add(skip_match.group(1).strip())

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


# The dataset's records carry number_interval, not tag, so Instance.create()
# looks up f"{org}/{number_interval}". The "imhex_1673_to_580" key above only
# matches a record with tag="1673_to_580", which this dataset does not set --
# register every era-2 bundle key against the same class so the records route.
for _ni in _BUNDLE_NIS:
    Instance.register("WerWolv", _ni)(IMHEX_1673_TO_580)
