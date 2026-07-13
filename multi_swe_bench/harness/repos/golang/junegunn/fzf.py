import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NUMBER_INTERVAL = "1297-1557-1626-1677-1927-1962-2208-2246-2281-2305-2353-2395-2548-2573-2641-2659-2764-2777-2813-2817-2880-3046-3059-3097-3129-3180-3248-3284-3313-3428-3512-3557-3678-3684-3734-3763-3787-3849-3991-3996-4071-4094-4136-4145-4161-4225-4252-4280-4307-4334-4361-4377-4459-4485-4554-4595-4684"


def _extract_ruby_test_info(test_patch: str) -> dict[str, list[str]]:
    """Extract test methods per Ruby file from a patch.

    Returns dict mapping ruby filename to list of test method names.
    Handles split-file structure (test_core.rb, test_layout.rb, etc.)
    """
    file_methods: dict[str, list[str]] = {}
    current_file = ""
    current_methods: set[str] = set()
    in_ruby_file = False

    for line in test_patch.split("\n"):
        if line.startswith("diff --git"):
            if in_ruby_file and current_methods:
                file_methods[current_file] = sorted(current_methods)
            in_ruby_file = line.endswith(".rb")
            if in_ruby_file:
                parts = line.split(" b/")
                current_file = parts[-1] if len(parts) > 1 else ""
                current_methods = set()
            continue
        if not in_ruby_file:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            m = re.search(r"def (test_\w+)", line)
            if m:
                current_methods.add(m.group(1))
        if line.startswith("@@"):
            m = re.search(r"def (test_\w+)", line)
            if m:
                current_methods.add(m.group(1))

    if in_ruby_file and current_methods:
        file_methods[current_file] = sorted(current_methods)

    return file_methods


class FzfImageBase(Image):
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
        return "golang:1.24-bookworm"

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

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

ENV GOTOOLCHAIN=local
ENV GOFLAGS="-buildvcs=false -mod=mod"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget patch \\
    ruby ruby-dev tmux locales && \\
    sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen && \\
    rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV TERM=xterm

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class FzfImageDefault(Image):
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
        return FzfImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        ruby_info = _extract_ruby_test_info(self.pr.test_patch)
        ruby_run = ""
        bundle_setup = ""
        if ruby_info:
            has_split = any(
                f != "test/test_go.rb" for f in ruby_info
            )
            if has_split:
                bundle_setup = 'if [ -f Gemfile ]; then gem install bundler && bundle install; fi\n'
            lines = [
                f'cd /home/{self.pr.repo}',
                'mkdir -p bin',
                f'go build -o bin/fzf .',
                'export PATH=$PWD/bin:$PATH',
                'tmux new-session -d -s main 2>/dev/null || true',
            ]
            for ruby_file, methods in ruby_info.items():
                pattern = "|".join(methods)
                class_name = "TestGoFZF"
                if "test_core" in ruby_file:
                    class_name = "TestCore"
                elif "test_layout" in ruby_file:
                    class_name = "TestLayout"
                elif "test_shell_integration" in ruby_file:
                    class_name = "TestBash"
                lines.append(
                    f'ruby {ruby_file} --verbose --name "/^{class_name}#({pattern})$/" || true'
                )
            ruby_run = "\n".join(lines) + "\n"

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

{bundle_setup}go test -v -count=1 ./... || true

""".format(pr=self.pr, bundle_setup=bundle_setup),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 ./...
{ruby_run}
""".format(pr=self.pr, ruby_run=ruby_run),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./... || true
{ruby_run}
""".format(pr=self.pr, ruby_run=ruby_run),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
go test -v -count=1 ./...
{ruby_run}
""".format(pr=self.pr, ruby_run=ruby_run),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("junegunn", _NUMBER_INTERVAL)
class Fzf(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FzfImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        re_ruby_pass = re.compile(r"(\S+#test_\w+) = .* = \.")
        re_ruby_fail = re.compile(r"(\S+#test_\w+) \[.+:\d+\]:")
        re_ruby_error = re.compile(r"^(\S+#test_\w+):$")

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name not in passed_tests and test_name not in failed_tests:
                        skipped_tests.add(get_base_name(test_name))

            m = re_ruby_pass.match(line)
            if m:
                name = m.group(1)
                if name not in failed_tests:
                    passed_tests.add(name)

            m = re_ruby_fail.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                failed_tests.add(name)

            m = re_ruby_error.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                failed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
