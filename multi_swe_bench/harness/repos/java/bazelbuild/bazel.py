import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BazelImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def _get_jdk_version(self) -> str:
        ref = self.pr.base.ref
        if ref == "master":
            return "21"

        m = re.match(r"release-(\d+)\.", ref)
        if m:
            major = int(m.group(1))
            if major >= 8:
                return "21"
            elif major >= 7:
                return "17"
            else:
                return "11"

        return "21"

    def _get_bazelisk_setup(self) -> str:
        return textwrap.dedent("""\
            RUN ARCH=$(dpkg --print-architecture) \\
                && curl -fSsL -o /usr/local/bin/bazel https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-${ARCH} \\
                && chmod +x /usr/local/bin/bazel""")

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        jdk_version = self._get_jdk_version()
        bazelisk_setup = self._get_bazelisk_setup()

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git curl openjdk-{jdk_version}-jdk build-essential zip unzip python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-{jdk_version}-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-{jdk_version}-openjdk
ENV JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk

{bazelisk_setup}

RUN groupadd -r bazeluser && useradd -r -g bazeluser -m -d /home/bazeluser bazeluser

{code}

RUN chown -R bazeluser:bazeluser /home/

USER bazeluser

{self.clear_env}

"""


class BazelImageDefault(Image):
    _FIX_PATCH_TRANSFORMS = {
        23403: lambda patch: patch.replace(
            "+        case SYMLINK_ACTION -> {\n"
            "+          // Symlink actions are not represented in the expanded format.\n"
            "+        }",
            "+        case SYMLINK_ACTION:\n"
            "+          // Symlink actions are not represented in the expanded format.\n"
            "+          break;",
        ),
    }

    _TEST_TARGET_OVERRIDES = {
        12547: "//src/test/java/com/google/devtools/build/lib/query2/cquery/...",
    }

    _BAZEL_VERSION_OVERRIDES = {
        12547: "3.7.2",
    }

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
        return BazelImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _get_fix_patch(self) -> str:
        transform = self._FIX_PATCH_TRANSFORMS.get(self.pr.number)
        if transform:
            return transform(self.pr.fix_patch)
        return self.pr.fix_patch

    def _get_bazel_version_for_ref(self) -> str:
        override = self._BAZEL_VERSION_OVERRIDES.get(self.pr.number)
        if override:
            return override

        ref = self.pr.base.ref
        m = re.match(r"release-(\d+)\.(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

        m = re.match(r"release-(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.0"

        # Non-standard branches: try to extract version from branch name
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

        return "last_green"

    @staticmethod
    def _find_build_dirs(*patches: str) -> set[str]:
        dirs: set[str] = set()
        for patch in patches:
            for m in re.finditer(r"diff --git a/(.+?) b/(.+)", patch):
                path = m.group(2)
                basename = path.rsplit("/", 1)[-1] if "/" in path else path
                if basename in ("BUILD", "BUILD.bazel"):
                    pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
                    dirs.add(pkg_dir)
        return dirs

    @staticmethod
    def _likely_subdir(pkg_dir: str, parent_dir: str, build_dirs: set[str]) -> bool:
        if not parent_dir:
            return False
        if "/test/py/" in pkg_dir:
            return True
        if "/testdata/" in pkg_dir:
            return True
        if pkg_dir.endswith("/testdata"):
            return True
        if pkg_dir.endswith("/bin"):
            return True
        if parent_dir in build_dirs:
            return True
        return False

    def _extract_test_targets(self) -> str:
        all_build_dirs = self._find_build_dirs(self.pr.test_patch, self.pr.fix_patch)

        test_files: list[str] = []
        for m in re.finditer(r"diff --git a/(.+?) b/(.+)", self.pr.test_patch):
            path = m.group(2)
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if "/test/" not in path and "/javatests/" not in path:
                continue
            if basename in ("BUILD", "BUILD.bazel"):
                continue
            test_files.append(path)

        if not test_files:
            return "//src/test/..."

        targets: set[str] = set()

        for path in test_files:
            pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
            basename = path.rsplit("/", 1)[-1]
            stem = basename.rsplit(".", 1)[0] if "." in basename else basename

            if pkg_dir in all_build_dirs:
                targets.add(f"//{pkg_dir}/...")
                continue

            parent = pkg_dir
            found_parent_pkg = False
            while "/" in parent:
                parent = parent.rsplit("/", 1)[0]
                if parent in all_build_dirs:
                    targets.add(f"//{parent}:{stem}")
                    found_parent_pkg = True
                    break

            if found_parent_pkg:
                continue

            parent_dir = pkg_dir.rsplit("/", 1)[0] if "/" in pkg_dir else ""
            if self._likely_subdir(pkg_dir, parent_dir, all_build_dirs):
                targets.add(f"//{parent_dir}:{stem}")
            else:
                targets.add(f"//{pkg_dir}/...")

        if not targets:
            return "//src/test/..."

        return " ".join(sorted(targets))

    def files(self) -> list[File]:
        test_targets = self._TEST_TARGET_OVERRIDES.get(
            self.pr.number, self._extract_test_targets()
        )
        bazel_version_pin = self._get_bazel_version_for_ref()
        fix_patch = self._get_fix_patch()

        return [
            File(
                ".",
                "fix.patch",
                f"{fix_patch}",
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

if [ ! -f .bazelversion ]; then
  echo "{bazel_version}" > .bazelversion
fi
""".format(pr=self.pr, bazel_version=bazel_version_pin),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{pr.repo}
bazel test {test_targets} --build_tests_only --test_output=errors --test_tag_filters=-manual --test_timeout=600 --keep_going --jobs=6 2>&1; exit 0
""".format(pr=self.pr, test_targets=test_targets),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bazel test {test_targets} --build_tests_only --test_output=errors --test_tag_filters=-manual --test_timeout=600 --keep_going --jobs=6 2>&1; exit 0
""".format(pr=self.pr, test_targets=test_targets),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bazel test {test_targets} --build_tests_only --test_output=errors --test_tag_filters=-manual --test_timeout=600 --keep_going --jobs=6 2>&1; exit 0
""".format(pr=self.pr, test_targets=test_targets),
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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                RUN mkdir -p /home/{self.pr.repo} && \\
                    cat > /home/{self.pr.repo}/.bazelrc.user <<'BAZELRC'
startup --host_jvm_args=-Dhttp.proxyHost={proxy_host} --host_jvm_args=-Dhttp.proxyPort={proxy_port}
startup --host_jvm_args=-Dhttps.proxyHost={proxy_host} --host_jvm_args=-Dhttps.proxyPort={proxy_port}
BAZELRC
                """
                )

                proxy_cleanup = textwrap.dedent(
                    f"""
                    RUN rm -f /home/{self.pr.repo}/.bazelrc.user
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("bazelbuild", "bazel")
class Bazel(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BazelImageDefault(self.pr, self._config)

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
        target_status: dict[str, str] = {}

        re_test_result = re.compile(
            r"^(//\S+)\s+"
            r"(?:\(cached\)\s+)?"
            r"(PASSED|FAILED|TIMEOUT|NO STATUS|FLAKY|INCOMPLETE)"
            r"(?:,\s+passed\s+\d+/\d+)?"
            r"\s+in\s+[\d.]+s",
            re.MULTILINE,
        )

        for match in re_test_result.finditer(test_log):
            target = match.group(1)
            status = match.group(2)
            target_status[target] = status

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for target, status in target_status.items():
            if status in ("PASSED", "FLAKY"):
                passed_tests.add(target)
            elif status in ("FAILED", "TIMEOUT", "INCOMPLETE"):
                failed_tests.add(target)
            elif status == "NO STATUS":
                skipped_tests.add(target)

        if not passed_tests and not failed_tests:
            re_junit = re.compile(
                r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
                r"\s*Skipped:\s*(\d+),\s*Time elapsed:\s*[\d.]+\s*s(?:ec)?"
                r".*?(?:in\s+(\S+)|$)",
                re.MULTILINE,
            )
            for match in re_junit.finditer(test_log):
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5) if match.group(5) else f"test_suite_{match.start()}"

                if tests_run > 0 and failures == 0 and errors == 0 and skipped != tests_run:
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
