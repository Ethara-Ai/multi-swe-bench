import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.26-trixie"

_GO_FLOOR = "1.21.0"

_TAG_PREFIX = "gomod"

_SYNTAX_DIRECTIVE = DockerfileEnhancer.SYNTAX_DIRECTIVE

_PROXY_ARGS = DockerfileEnhancer._PROXY_ARGS

_ENV_BLOCK = DockerfileEnhancer._ENV_BLOCK

_CERT_SYMLINKS = DockerfileEnhancer._CERT_SYMLINKS

_ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_EVENT_RE = re.compile(r"^\{.*\}$")

_SECTION_RE = re.compile(r"(?m)(?=^diff --git )")

_DROP_RE = re.compile(
    r"(?m)^(Binary files .* differ|GIT binary patch)$|\Adiff --git a/\S+ b/(\S+/)?go(\.work)?\.sum\n"
)

_STATUS = {"pass": "passed", "fail": "failed", "skip": "skipped"}

_RANK = {"passed": 0, "skipped": 1, "failed": 2}

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || { echo "check_git_changes: Not inside a git repository"; exit 1; }
test -z "$(git status --porcelain)" || { echo "check_git_changes: Uncommitted changes"; git status --porcelain | head -20; exit 1; }
echo "check_git_changes: No uncommitted changes"
exit 0
"""

_TOOLCHAIN_SH = """#!/bin/bash
set -eo pipefail
cd /home/__REPO__
DECLARED=$(sed -n 's/^go \\([0-9.]*\\).*/\\1/p' go.mod | head -1 | awk -F. 'NF==2 {print $0".0"; next} {print}')
WANTED=$(printf '%s\\n%s\\n' "__FLOOR__" "${DECLARED}" | sed '/^$/d' | sort -V | tail -1)
MODFLAG=$( (test -d vendor && echo vendor) || echo mod )
echo "TOOLCHAIN: declared=${DECLARED:-none} selected=go${WANTED} modflag=${MODFLAG}"
printf 'export GOTOOLCHAIN=go%s+auto\\nexport GOFLAGS=-mod=%s\\nexport GOWORK=off\\nexport CI=true\\n' "${WANTED}" "${MODFLAG}" > /home/go-env.sh
. /home/go-env.sh
go version
"""

_PACKAGES_SH = """#!/bin/bash
set -eo pipefail
cd /home/__REPO__
dirs_of() { (grep -hoE "$1" "${@:2}" || true) | sed -E 's#^\\+\\+\\+ b/##' | xargs -r -n1 dirname | sort -u | while read -r d; do test -d "$d" && printf './%s ' "$d"; done || true; }
FROM_TESTS=$(dirs_of '^\\+\\+\\+ b/\\S+_test\\.go$' /home/test.patch)
FROM_GO=$(dirs_of '^\\+\\+\\+ b/\\S+\\.go$' /home/test.patch /home/fix.patch)
printf '%s' "${FROM_TESTS:-${FROM_GO:-./...}}"
"""

_TEST_CMD_SH = """#!/bin/bash
set -eo pipefail
cd /home/__REPO__
. /home/go-env.sh
PKGS=$(bash /home/packages.sh)
echo "TEST_PACKAGES: ${PKGS}"
go test -json -count=1 -timeout 90m "$@" ${PKGS}
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __BASE_SHA__^{commit} 2>/dev/null || git fetch --quiet https://github.com/__ORG__/__REPO__.git __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__BASE_SHA__"

bash /home/toolchain.sh
. /home/go-env.sh

go mod download
bash /home/test-cmd.sh -run '^$'
git reset --hard
echo DEPS_OK
"""

_STAGE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git reset --hard
git clean -qfd
__APPLY__
bash /home/test-cmd.sh
"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__REPO__", pr.repo)
        .replace("__ORG__", pr.org)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__FLOOR__", _GO_FLOOR)
    )


def _sanitize_patch(patch: str) -> str:
    return "".join(
        section
        for section in _SECTION_RE.split(patch or "")
        if section and not _DROP_RE.search(section)
    )


def _decode(line: str) -> dict:
    try:
        return json.loads(line)
    except ValueError:
        return {}


class GomodToolchainImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"{_TAG_PREFIX}-base"

    def workdir(self) -> str:
        return f"{_TAG_PREFIX}-base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        fetch = {
            True: f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}',
            False: f"COPY {self.pr.repo} /home/{self.pr.repo}",
        }[bool(self.config.need_clone)]

        return f"""{_SYNTAX_DIRECTIVE}
FROM {_GO_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{_PROXY_ARGS}

{_ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_CERT_SYMLINKS}

{self.global_env}

ENV CGO_ENABLED=1 \\
    LC_ALL=C.UTF-8 \\
    CI=true

WORKDIR /home/

{fetch}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class GomodToolchainImageDefault(Image):
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
        return GomodToolchainImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"{_TAG_PREFIX}-pr-{self.pr.number}"

    def workdir(self) -> str:
        # gen_report only collects instance dirs named "pr-<N>"
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        stage = _render(_STAGE_SH, self.pr)
        return [
            File(".", "fix.patch", _sanitize_patch(self.pr.fix_patch)),
            File(".", "test.patch", _sanitize_patch(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "toolchain.sh", _render(_TOOLCHAIN_SH, self.pr)),
            File(".", "packages.sh", _render(_PACKAGES_SH, self.pr)),
            File(".", "test-cmd.sh", _render(_TEST_CMD_SH, self.pr)),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run.sh", stage.replace("__APPLY__", "")),
            File(
                ".",
                "test-run.sh",
                stage.replace("__APPLY__", "git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                stage.replace(
                    "__APPLY__",
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}

RUN /bin/bash -o pipefail -c "bash /home/prepare.sh 2>&1 | {_ASCII_FILTER}"

WORKDIR /home/{self.pr.repo}

{self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)}

{self.clear_env}

CMD ["/bin/bash"]

"""


@Instance.register("prometheus", "gomod_toolchain")
class GOMOD_TOOLCHAIN(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GomodToolchainImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = _ANSI_RE.sub("", test_log).replace("\r", "")
        stripped = [line.strip() for line in log.splitlines()]
        events = [_decode(line) for line in stripped if _EVENT_RE.match(line)]
        records = [
            (f"{e.get('Package', '')}::{e['Test']}", _STATUS[e["Action"]])
            for e in events
            if e.get("Test") and e.get("Action") in _STATUS
        ]

        results: dict[str, str] = {}
        for name, status in records:
            results[name] = max(results.get(name, status), status, key=_RANK.get)

        buckets: dict[str, set[str]] = {"passed": set(), "failed": set(), "skipped": set()}
        for name, status in results.items():
            buckets[status].add(name)

        return TestResult(
            passed_count=len(buckets["passed"]),
            failed_count=len(buckets["failed"]),
            skipped_count=len(buckets["skipped"]),
            passed_tests=buckets["passed"],
            failed_tests=buckets["failed"],
            skipped_tests=buckets["skipped"],
        )
