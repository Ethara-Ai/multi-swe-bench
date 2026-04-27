"""fzf harness for glide GOPATH era (PRs 1015-1413).

Uses golang:1.11 with glide v0.13.3 for dependency management.
Code lives in src/ subdirectory; needs GOPATH symlink setup.
Requires -vet=off to suppress Go vet false positives in test files.
Supports Ruby integration tests (test_go.rb) and Vim vader tests.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.11"
_TAG_SUFFIX = "gopath_glide"


def _extract_ruby_test_methods(test_patch: str) -> list[str]:
    """Extract test method names from Ruby test file changes in a patch."""
    methods: set[str] = set()
    in_ruby_file = False
    for line in test_patch.split("\n"):
        if line.startswith("diff --git"):
            in_ruby_file = line.endswith(".rb")
        if not in_ruby_file:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            m = re.search(r"def (test_\w+)", line)
            if m:
                methods.add(m.group(1))
        if line.startswith("@@"):
            m = re.search(r"def (test_\w+)", line)
            if m:
                methods.add(m.group(1))
    return sorted(methods)


def _has_vader_tests(test_patch: str) -> bool:
    for line in test_patch.split("\n"):
        if line.startswith("diff --git") and ".vader" in line:
            return True
    return False


class _ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        gopath_pkg = f"github.com/{self.pr.org}/{self.pr.repo}"

        if self.config.need_clone:
            code = (
                f"RUN mkdir -p /go/src/{gopath_pkg} && "
                f"git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/go/src/{gopath_pkg}"
            )
        else:
            code = f"COPY {self.pr.repo} /go/src/{gopath_pkg}"

        return f"""FROM {image_name}

ENV DEBIAN_FRONTEND=noninteractive
ENV GOPATH=/go
ENV PATH=$GOPATH/bin:$PATH

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install -y --no-install-recommends --allow-unauthenticated git ruby tmux locales && \
    sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen

ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV TERM=xterm

RUN ARCH=$(dpkg --print-architecture 2>/dev/null || echo amd64) && \
    curl -fsSL https://github.com/Masterminds/glide/releases/download/v0.13.3/glide-v0.13.3-linux-${{ARCH}}.tar.gz | \
    tar -xz -C /usr/local/bin --strip-components=1 linux-${{ARCH}}/glide

{self.global_env}

{code}

WORKDIR /go/src/{gopath_pkg}

RUN ln -sf /go/src/{gopath_pkg}/src $GOPATH/src/{gopath_pkg}/src/vendor/github.com || true && \
    mkdir -p $GOPATH/src/{gopath_pkg}/src/vendor/github.com 2>/dev/null || true

RUN cd /go/src/{gopath_pkg} && \
    SRC_LINK=$GOPATH/src/{gopath_pkg}/src && \
    VENDOR_LINK=$GOPATH/src/{gopath_pkg}/vendor && \
    if [ -f glide.yaml ]; then glide install; fi && \
    if [ -d vendor ] && [ ! -L "$VENDOR_LINK" ]; then \
        ln -sf /go/src/{gopath_pkg}/vendor $VENDOR_LINK 2>/dev/null || true; \
    fi

{self.clear_env}

"""


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        gopath_pkg = f"github.com/{self.pr.org}/{self.pr.repo}"
        ruby_methods = _extract_ruby_test_methods(self.pr.test_patch)
        vader = _has_vader_tests(self.pr.test_patch)
        ruby_run = ""
        ruby_test_run = ""
        ruby_fix_run = ""
        if ruby_methods:
            pattern = "|".join(ruby_methods)
            ruby_base = (
                'cd /go/src/{gopath_pkg}\n'
                'mkdir -p bin\n'
                'go build -o bin/fzf .\n'
                'export PATH=$PWD/bin:$PATH\n'
                'tmux new-session -d -s main 2>/dev/null || true\n'
                'ruby test/test_go.rb --verbose --name "/^TestGoFZF#({pattern})$/" || true\n'
            ).format(gopath_pkg=gopath_pkg, pattern=pattern)
            ruby_run = ruby_base
            ruby_test_run = ruby_base
            ruby_fix_run = ruby_base
        elif vader:
            vader_base = (
                'cd /go/src/{gopath_pkg}\n'
                'mkdir -p bin\n'
                'go build -o bin/fzf .\n'
                'if command -v vim >/dev/null 2>&1 && [ -d ~/.vim/plugged/vader.vim ]; then\n'
                '  vim -N -u test/vimrc "+Vader! test/fzf.vader" 2>&1 || true\n'
                'fi\n'
            ).format(gopath_pkg=gopath_pkg)
            ruby_run = vader_base
            ruby_test_run = vader_base
            ruby_fix_run = vader_base

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

if [ -f glide.yaml ]; then
  glide install
fi

cd /go/src/{gopath_pkg}/src
go test -v -count=1 -vet=off ./... || true

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}/src
go test -v -count=1 -vet=off ./...
{ruby_run}
""".format(pr=self.pr, gopath_pkg=gopath_pkg, ruby_run=ruby_run),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
git apply /home/test.patch
cd /go/src/{gopath_pkg}/src
go test -v -count=1 -vet=off ./...
{ruby_test_run}
""".format(pr=self.pr, gopath_pkg=gopath_pkg, ruby_test_run=ruby_test_run),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
git apply /home/test.patch /home/fix.patch
cd /go/src/{gopath_pkg}/src
go test -v -count=1 -vet=off ./...
{ruby_fix_run}
""".format(pr=self.pr, gopath_pkg=gopath_pkg, ruby_fix_run=ruby_fix_run),
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"--- PASS: (\S+)")
    re_fail = [
        re.compile(r"--- FAIL: (\S+)"),
        re.compile(r"FAIL:?\s?(.+?)\s"),
    ]
    re_skip = re.compile(r"--- SKIP: (\S+)")

    # Ruby minitest verbose format: "TestGoFZF#test_name = 0.15 s = ."
    re_ruby_pass = re.compile(r"(\S+#test_\w+) = .* = \.")
    re_ruby_fail = re.compile(r"(\S+#test_\w+) \[.+:\d+\]:")
    re_ruby_error = re.compile(r"^(\S+#test_\w+):$")

    for line in test_log.splitlines():
        line = line.strip()

        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)

        for rp in re_fail:
            m = rp.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)

        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)

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


@Instance.register("junegunn", "fzf_gopath_glide")
class FzfGopathGlide(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)
