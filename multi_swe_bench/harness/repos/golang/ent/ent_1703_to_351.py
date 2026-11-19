from __future__ import annotations

import os
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "ent"
_INTERVAL_NAME = "ent_1703_to_351"
_BASE_IMAGE = "golang:1.17-bullseye"
_GOFLAGS = "-mod=mod"

_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".bmp", ".webp",
    ".pdf", ".zip", ".jar", ".class", ".tar", ".gz", ".tgz", ".bz2", ".7z",
    ".bin", ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".wav", ".ogg",
    ".db", ".sqlite", ".rdb",
    ".nupkg", ".whl", ".egg", ".gem",
    ".rpm", ".deb", ".apk",
}


def _is_binary_path(path: str) -> bool:
    _, ext = os.path.splitext(path.lower())
    return ext in _BINARY_EXTENSIONS


def _strip_binary_hunks(patch_text: str) -> str:
    if not patch_text:
        return patch_text
    out = []
    for block in re.split(r"(?m)^(?=diff --git )", patch_text):
        if not block.strip():
            continue
        m = re.match(r"diff --git a/(.*) b/(.*)$", block.splitlines()[0])
        if m and (_is_binary_path(m.group(1)) or _is_binary_path(m.group(2))):
            continue
        if "GIT binary patch" in block or re.search(
            r"^Binary files .* differ", block, re.MULTILINE
        ):
            continue
        out.append(block)
    return "".join(out)


class Ent1703To351ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}"'
        )

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {_BASE_IMAGE}",
            (
                f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT"
            ),
            DockerfileEnhancer._PROXY_ARGS,
            DockerfileEnhancer._ENV_BLOCK,
            label,
            DockerfileEnhancer._CERT_SYMLINKS,
            self.global_env,
            (
                "ENV GOTOOLCHAIN=local \\\n"
                f"    GOFLAGS={_GOFLAGS} \\\n"
                "    GOPROXY=https://proxy.golang.org,direct \\\n"
                "    CGO_ENABLED=1 \\\n"
                "    CI=true"
            ),
            (
                "RUN sed -i '/debian-security/d' /etc/apt/sources.list 2>/dev/null; (apt-get update || (sed -i -e 's|deb.debian.org|archive.debian.org|g' -e 's|security.debian.org|archive.debian.org|g' -e '/-updates/d' /etc/apt/sources.list && apt-get -o Acquire::Check-Valid-Until=false update)) \\\n"
                "    && apt-get install -y --no-install-recommends \\\n"
                "    git ca-certificates build-essential pkg-config zip unzip file \\\n"
                "    && rm -rf /var/lib/apt/lists/*"
            ),
            "WORKDIR /home/",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


class Ent1703To351ImageDefault(Image):
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
        return Ent1703To351ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        env = f"""export GOTOOLCHAIN=local
export GOFLAGS={_GOFLAGS}
export CI=true
cd /home/{self.pr.repo}"""
        return [
            File(".", "fix.patch", _strip_binary_hunks(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_hunks(self.pr.test_patch)),
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
                f"""#!/bin/bash
set -eo pipefail

{env}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

go mod download || true
go build ./... || true
go list -deps -test ./... > /dev/null
git checkout -- go.mod go.sum

""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
go test -v -count=1 ./... 2>&1

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./... 2>&1

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./... 2>&1

""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_commands}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    re_pass = re.compile(r"^--- PASS: (\S+)")
    re_fail = re.compile(r"^--- FAIL: (\S+)")
    re_skip = re.compile(r"^--- SKIP: (\S+)")
    re_pkg_fail = re.compile(r"^FAIL\s+(\S+)\s+\[(build failed|setup failed)\]")

    for raw in test_log.splitlines():
        line = raw.strip()
        m = re_fail.match(line)
        if m:
            name = m.group(1)
            failed_tests.add(name)
            passed_tests.discard(name)
            skipped_tests.discard(name)
            continue
        m = re_pkg_fail.match(line)
        if m:
            failed_tests.add(m.group(1))
            continue
        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests:
                passed_tests.add(name)
                skipped_tests.discard(name)
            continue
        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests and name not in passed_tests:
                skipped_tests.add(name)

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


@Instance.register(_ORG, _INTERVAL_NAME)
class ENT_1703_TO_351(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Ent1703To351ImageDefault(self.pr, self._config)

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
        return _parse_go_test_log(test_log)
