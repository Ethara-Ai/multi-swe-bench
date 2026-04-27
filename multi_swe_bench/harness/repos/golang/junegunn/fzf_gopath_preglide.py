"""fzf harness for pre-glide GOPATH era (PRs 787-991).

Uses golang:1.9 with manually pinned dependencies and libncurses5-dev
for ncurses CGO compilation. Code lives in src/ subdirectory; needs
GOPATH symlink setup.

Supports both Go unit tests and Ruby integration tests (test_go.rb).
Ruby tests run via minitest with tmux inside Docker when test_patch
modifies Ruby test files.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.9"
_TAG_SUFFIX = "gopath_preglide"


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

        # Pin dependencies at old commits compatible with pre-2017 fzf.
        # go get fails on latest versions of these packages because APIs changed.
        return f"""FROM {image_name}

ENV DEBIAN_FRONTEND=noninteractive
ENV GOPATH=/go
ENV PATH=$GOPATH/bin:$PATH

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install -y --no-install-recommends --allow-unauthenticated git libncurses5-dev ruby tmux locales && \
    sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen

ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV TERM=xterm

{self.global_env}

{code}

WORKDIR /go/src/{gopath_pkg}

RUN mkdir -p /go/src/{gopath_pkg}/src

RUN git clone https://github.com/junegunn/go-runewidth.git /go/src/github.com/junegunn/go-runewidth && \
    cd /go/src/github.com/junegunn/go-runewidth && git checkout ed005bd && \
    git clone https://github.com/junegunn/go-shellwords.git /go/src/github.com/junegunn/go-shellwords && \
    git clone https://github.com/junegunn/go-isatty.git /go/src/github.com/junegunn/go-isatty && \
    git clone https://github.com/junegunn/tcell.git /go/src/github.com/junegunn/tcell && \
    git clone https://github.com/gdamore/tcell.git /go/src/github.com/gdamore/tcell && \
    cd /go/src/github.com/gdamore/tcell && git checkout v1.0.0 && \
    git clone https://github.com/gdamore/encoding.git /go/src/github.com/gdamore/encoding && \
    cd /go/src/github.com/gdamore/encoding && git checkout b23993c && \
    git clone https://github.com/lucasb-eyer/go-colorful.git /go/src/github.com/lucasb-eyer/go-colorful && \
    cd /go/src/github.com/lucasb-eyer/go-colorful && git checkout c900de9 && \
    git clone https://github.com/mattn/go-runewidth.git /go/src/github.com/mattn/go-runewidth && \
    cd /go/src/github.com/mattn/go-runewidth && git checkout 97311d9 && \
    git clone https://github.com/mattn/go-isatty.git /go/src/github.com/mattn/go-isatty && \
    cd /go/src/github.com/mattn/go-isatty && git checkout 66b8e73 && \
    git clone https://github.com/mattn/go-shellwords.git /go/src/github.com/mattn/go-shellwords && \
    cd /go/src/github.com/mattn/go-shellwords && git checkout 02e3cf0 && \
    mkdir -p /go/src/golang.org/x && \
    git clone https://github.com/golang/text.git /go/src/golang.org/x/text && \
    cd /go/src/golang.org/x/text && git checkout 836efe4 && \
    git clone https://github.com/golang/sys.git /go/src/golang.org/x/sys && \
    cd /go/src/golang.org/x/sys && git checkout d8f5ea2 && \
    git clone https://github.com/golang/crypto.git /go/src/golang.org/x/crypto && \
    cd /go/src/golang.org/x/crypto && git checkout 94eea52

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

cd /go/src/{gopath_pkg}/src
go test -v -count=1 ./... || true

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}/src
go test -v -count=1 ./...
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
go test -v -count=1 ./...
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
go test -v -count=1 ./...
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


@Instance.register("junegunn", "fzf_gopath_preglide")
class FzfGopathPreglide(Instance):
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
