import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_SERVER_DIR = "server"

_GO_TEST_CMD = "go test -v -count=1 -timeout 20m ./..."

_ENV_EXPORTS = "export CGO_ENABLED=1\nexport CI=true"


class OffenImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "golang:1.16"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def _clone_and_scrub(self) -> str:
        repo = self.pr.repo
        return (
            "# Clone + detach + scrub in ONE layer -- see _clone_and_scrub().\n"
            f'RUN git clone "${{REPO_URL}}" /home/{repo} \\\n'
            f"    && cd /home/{repo} \\\n"
            '    && git checkout --detach "${BASE_COMMIT}" \\\n'
            "    && (git remote remove origin 2>/dev/null || true) \\\n"
            "    && git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d \\\n"
            "    && git reflog expire --expire=now --all \\\n"
            "    && git reflog expire --expire-unreachable=now --all \\\n"
            "    && git gc --prune=now --aggressive \\\n"
            "    && git repack -a -d -l --quiet \\\n"
            "    && rm -f .git/objects/info/alternates"
        )

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = self._clone_and_scrub()
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


class OffenImageDefault(Image):
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
        return OffenImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

{env}

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the module and build caches at IMAGE BUILD time, while the network is
# still reachable, so the three evaluation stages need no downloads. This is
# safe for the fix stage too: the gold patch only DELETES from server/go.sum
# (24 removals, 0 additions - it drops github.com/rakyll/statik together with
# its transitive-only entries), so applying it never pulls a new module.
cd /home/{pr.repo}/{server}
go mod download || true
# `|| true` above is the harness convention (a transient fetch hiccup should not
# abort the build), but unlike an optional native addon a Go image with no module
# cache is useless: every later stage would fail to compile with an unhelpful
# error. So resolve the full dependency graph explicitly — this is a metadata
# walk, not a compile, and `set -e` makes an unresolved module fail the build
# here, where the cause is obvious.
go list -deps ./... > /dev/null
{go} || true

# Go 1.16's `go mod download` may append missing checksums to go.sum. That would
# leave the tree dirty and make `git apply` of the gold go.sum hunk fail at
# evaluation time, so the image ships a pristine checkout and fails the build
# loudly if anything else was left behind.
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

""".format(pr=self.pr, server=_SERVER_DIR, go=_GO_TEST_CMD, env=_ENV_EXPORTS),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}/{server}
{go}

""".format(pr=self.pr, server=_SERVER_DIR, go=_GO_TEST_CMD, env=_ENV_EXPORTS),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

# Patch paths are repo-root relative (`server/...`), so apply from the repo root
# and only then descend into the Go module. This two-step cd is identical in all
# three stage scripts, so the test command always runs from the same place.
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cd /home/{pr.repo}/{server}
{go}

""".format(pr=self.pr, server=_SERVER_DIR, go=_GO_TEST_CMD, env=_ENV_EXPORTS),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd /home/{pr.repo}/{server}
{go}

""".format(pr=self.pr, server=_SERVER_DIR, go=_GO_TEST_CMD, env=_ENV_EXPORTS),
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


@Instance.register("offen", "offen")
class Offen(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OffenImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_verdict = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+)")

        re_pkg_end = re.compile(r"^(ok|FAIL|\?)\s+(\S+)")

        def record(name: str, status: str) -> None:
            if status == "FAIL":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "PASS":
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            else:
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        pending: list[tuple[str, str]] = []

        for line in clean.splitlines():
            line = line.strip()

            verdict = re_verdict.match(line)
            if verdict:
                pending.append((verdict.group(2), verdict.group(1)))
                continue

            pkg_end = re_pkg_end.match(line)
            if pkg_end and ("/" in pkg_end.group(2) or "." in pkg_end.group(2)):
                marker, pkg = pkg_end.group(1), pkg_end.group(2)
                for name, status in pending:
                    record(f"{pkg}::{name}", status)
                pending.clear()

                if marker == "FAIL":
                    record(pkg, "FAIL")
                elif marker == "ok":
                    record(pkg, "PASS")

        for name, status in pending:
            record(name, status)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
