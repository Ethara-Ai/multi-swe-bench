import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.92-trixie"

_RUST_FLOOR = "1.70.0"

_TEST_USER = "uutester"

_TAG_PREFIX = "cargo"

_SYNTAX_DIRECTIVE = DockerfileEnhancer.SYNTAX_DIRECTIVE

_PROXY_ARGS = DockerfileEnhancer._PROXY_ARGS

_ENV_BLOCK = DockerfileEnhancer._ENV_BLOCK

_CERT_SYMLINKS = DockerfileEnhancer._CERT_SYMLINKS

_ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

_APT_PACKAGES = "git ca-certificates pkg-config clang libclang-dev libacl1-dev libattr1-dev libselinux1-dev attr acl locales"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_RESULT_RE = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b.*$", re.MULTILINE)

_SECTION_RE = re.compile(r"(?m)(?=^diff --git )")

_BINARY_RE = re.compile(r"(?m)^(Binary files .* differ|GIT binary patch)$")

_STATUS = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}

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
DECLARED=$(sed -n 's/^rust-version *= *"\\([0-9.]*\\)".*/\\1/p' Cargo.toml | head -1)
WANTED=$(printf '%s\\n%s\\n' "__FLOOR__" "${DECLARED}" | sed '/^$/d' | sort -V | tail -1)
echo "TOOLCHAIN: declared=${DECLARED:-none} selected=${WANTED}"
rustup toolchain install --profile minimal "${WANTED}"
rustup default "${WANTED}"
(test -f rust-toolchain.toml || test -f rust-toolchain) && rustup toolchain install
rustc --version
"""

_TEST_CMD_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
FILTERS=$( (grep -oE '^\\+\\+\\+ b/tests/by-util/test_[A-Za-z0-9_]+\\.rs' /home/test.patch || true) | sed -E 's#.*/(test_[A-Za-z0-9_]+)\\.rs#\\1::#' | sort -u | tr '\\n' ' ')
echo "TEST_FILTERS: ${FILTERS}"
find /home/__REPO__ -path /home/__REPO__/target -prune -o -print0 | xargs -0 chown __USER__:__USER__
runuser -u __USER__ -- env HOME=/home/__USER__ CI=true cargo test --features unix --no-fail-fast --test tests "$@" -- ${FILTERS}
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

cargo fetch
chown -R __USER__:__USER__ /home/__REPO__ "$CARGO_HOME"
bash /home/test-cmd.sh --no-run
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
        .replace("__USER__", _TEST_USER)
        .replace("__FLOOR__", _RUST_FLOOR)
    )


def _sanitize_patch(patch: str) -> str:
    return "".join(
        section
        for section in _SECTION_RE.split(patch or "")
        if section and not _BINARY_RE.search(section)
    )


class UutilsCargoImageBase(Image):
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
        return _RUST_IMAGE

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
FROM {_RUST_IMAGE}

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

ENV CARGO_TERM_COLOR=never \\
    CARGO_TERM_PROGRESS_WHEN=never \\
    RUST_BACKTRACE=1 \\
    NO_COLOR=1 \\
    LC_ALL=C.UTF-8 \\
    CI=true

RUN /bin/bash -o pipefail -c "( apt-get update && apt-get install -y --no-install-recommends {_APT_PACKAGES} && rm -rf /var/lib/apt/lists/* ) 2>&1 | {_ASCII_FILTER}"

RUN useradd --create-home --shell /bin/bash {_TEST_USER} && git config --system --add safe.directory '*'

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


class UutilsCargoImageDefault(Image):
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
        return UutilsCargoImageBase(self.pr, self._config)

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


@Instance.register("uutils", "uutils_cargo")
class UUTILS_CARGO(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return UutilsCargoImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = _ANSI_RE.sub("", test_log).replace("\r", "")
        results: dict[str, str] = {}
        for name, raw in _RESULT_RE.findall(log):
            status = _STATUS[raw]
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
