import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Earliest pre-modules era: vuls used `glide` (glide.yaml / glide.lock) and a
# GOPATH layout. The repo must live at $GOPATH/src/github.com/future-architect/vuls
# and deps are materialised into vendor/ from the pinned glide.lock via `glide install`.
GOPATH_DIR = "/go/src/github.com/future-architect/vuls"


class ImageBase(Image):
    """Shared per-era base image (built once, reused by every glide PR).

    Installs the pinned glide binary, clones the repo into /home/vuls with the
    GOPATH symlink, and keeps full history; the PR layer checks out base.sha and
    does the strict history-strip.
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
        return "golang:1.10"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-glide"

    def workdir(self) -> str:
        return "base-glide"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GO111MODULE=off \\
    GOPATH=/go

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

# glide is the pinned dependency manager for this era. TARGETARCH is only set by
# buildx --platform; fall back to dpkg for native (classification) builds.
RUN ARCH="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    curl -sSL -o /tmp/glide.tar.gz https://github.com/Masterminds/glide/releases/download/v0.13.3/glide-v0.13.3-linux-${{ARCH}}.tar.gz \\
    && tar -xzf /tmp/glide.tar.gz -C /tmp \\
    && cp /tmp/linux-${{ARCH}}/glide /usr/local/bin/glide \\
    && chmod +x /usr/local/bin/glide \\
    && rm -rf /tmp/glide.tar.gz /tmp/linux-${{ARCH}}

WORKDIR /home/
RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}
# GOPATH layout: symlink $GOPATH/src/github.com/future-architect/vuls -> /home/{repo}.
RUN mkdir -p /go/src/github.com/future-architect && ln -sfn /home/{repo} {GOPATH_DIR}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "Image":
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

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

cd {gopath_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Materialise vendor/ from the pinned glide.lock (vendor is gitignored, so the
# working tree stays clean for the assertions above).
glide install

go test -v -count=1 ./... || true

""".format(pr=self.pr, gopath_dir=GOPATH_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd {gopath_dir}
go test -v -count=1 ./...

""".format(gopath_dir=GOPATH_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd {gopath_dir}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go test -v -count=1 ./...

""".format(gopath_dir=GOPATH_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd {gopath_dir}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go test -v -count=1 ./...

""".format(gopath_dir=GOPATH_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("future-architect", "vuls_glide")
class Vuls_glide(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Delivery scope = RESOLVED (valid) bundles only; keys == #delivered instances (PIPELINE §11c).
# Bucketed by the authoritative go.mod-at-base.sha era key (NOT a lead-PR proxy).
_BUNDLE_NIS_Vuls_glide = [
    "288-289-291-292-293-296-297-298-299-300-301-302-303-304-305-306-308-309-314-317-319-322-324-326-328-329-330-331-332-337-338-339-340-343-344-345-346-347-348-350-351-354-356-358-363-364-369-370-372-374-375-377-378-379-381-382-383-384-386-387-388-392-394-400",
]
for _ni in _BUNDLE_NIS_Vuls_glide:
    Instance.register("future-architect", _ni)(Vuls_glide)
