import re
from typing import Optional

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_KEY = "shredder_60_to_45"
_BASE_TAG = "base"
_RUST_IMAGE = "rust:1.98-bookworm"

_PROXY_ARGS = "\n".join(
    [
        'ARG http_proxy=""',
        'ARG https_proxy=""',
        'ARG HTTP_PROXY=""',
        'ARG HTTPS_PROXY=""',
        'ARG no_proxy="localhost,127.0.0.1,::1"',
        'ARG NO_PROXY="localhost,127.0.0.1,::1"',
        'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"',
    ]
)

_ENV_BLOCK = "\n".join(
    [
        "ENV DEBIAN_FRONTEND=noninteractive \\",
        "    LANG=C.UTF-8 \\",
        "    LC_ALL=C.UTF-8 \\",
        "    TZ=UTC \\",
        "    CI=true \\",
        "    CARGO_TERM_COLOR=never \\",
        "    CARGO_NET_RETRY=10 \\",
        "    CARGO_INCREMENTAL=0 \\",
        "    RUST_BACKTRACE=1 \\",
        "    RUSTUP_MAX_RETRIES=10 \\",
        "    http_proxy=${http_proxy} \\",
        "    https_proxy=${https_proxy} \\",
        "    HTTP_PROXY=${HTTP_PROXY} \\",
        "    HTTPS_PROXY=${HTTPS_PROXY} \\",
        "    no_proxy=${no_proxy} \\",
        "    NO_PROXY=${NO_PROXY} \\",
        "    SSL_CERT_FILE=${CA_CERT_PATH} \\",
        "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\",
        "    CURL_CA_BUNDLE=${CA_CERT_PATH}",
    ]
)

_CERT_FARM = "\n".join(
    [
        "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt",
    ]
)

_APT_BLOCK = "\n".join(
    [
        "RUN apt-get update && apt-get install -y --no-install-recommends \\",
        "    ca-certificates curl git pkg-config libssl-dev \\",
        "    && rm -rf /var/lib/apt/lists/*",
    ]
)


def _strip_binary_sections(patch_content: str) -> str:
    if not patch_content:
        return patch_content

    lines = patch_content.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git"):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith("diff --git"):
                if lines[i].startswith("GIT binary patch") or lines[i].startswith(
                    "Binary files"
                ):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1

    cleaned = "\n".join(result)
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned


def _prune_block(repo: str, sha: str) -> str:
    return "\n".join(
        [
            "RUN set -eux; \\",
            "    cd /home/" + repo + "; \\",
            '    test "$(git rev-parse HEAD)" = "' + sha + '"; \\',
            "    git remote remove origin 2>/dev/null || true; \\",
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\",
            "        | xargs -r -n1 git update-ref -d; \\",
            "    git reflog expire --expire=now --all; \\",
            "    git reflog expire --expire-unreachable=now --all; \\",
            "    git gc --prune=now --aggressive; \\",
            "    git repack -a -d -l --quiet; \\",
            "    rm -f .git/objects/info/alternates; \\",
            "    git config --local gc.auto 0; \\",
            "    git config --local fetch.recurseSubmodules false; \\",
            '    git config --local remote.pushDefault ""; \\',
            '    test "$(git rev-parse HEAD)" = "' + sha + '"; \\',
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\',
            '    test -z "$(git remote)"; \\',
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"',
        ]
    )


def _submodule_block(repo: str) -> str:
    return "\n".join(
        [
            "RUN if [ -f /home/" + repo + "/.gitmodules ]; then \\",
            "        cd /home/" + repo + " && git submodule foreach --recursive ' \\",
            "            git checkout --detach HEAD; \\",
            "            git remote remove origin 2>/dev/null || true; \\",
            '            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\',
            "                | xargs -r -n1 git update-ref -d; \\",
            "            git reflog expire --expire=now --all; \\",
            "            git reflog expire --expire-unreachable=now --all; \\",
            "            git gc --prune=now --aggressive; \\",
            "            rm -f .git/objects/info/alternates; \\",
            "        '; \\",
            "    fi",
        ]
    )


class Shredder60To45ImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        source = "https://github.com/" + org + "/" + repo

        if self.config.need_clone:
            acquire = "\n".join(
                [
                    'RUN git clone "${REPO_URL}" /home/' + repo + " && \\",
                    "    cd /home/" + repo + " && git rev-parse HEAD >/dev/null",
                ]
            )
        else:
            acquire = "COPY " + repo + " /home/" + repo

        lines = [
            "# syntax=docker/dockerfile:1.6",
            "",
            "FROM " + _RUST_IMAGE,
            "",
            "ARG TARGETARCH",
            'ARG REPO_URL="' + source + '.git"',
            "ARG BASE_COMMIT",
            "",
            _PROXY_ARGS,
            "",
            _ENV_BLOCK,
            "",
            self.global_env,
            "",
            'LABEL org.opencontainers.image.title="' + org + "/" + repo + '" \\',
            '      org.opencontainers.image.description="'
            + org
            + "/"
            + repo
            + ' Docker image" \\',
            '      org.opencontainers.image.source="' + source + '" \\',
            '      org.opencontainers.image.authors="https://www.ethara.ai/"',
            "",
            _CERT_FARM,
            "",
            _APT_BLOCK,
            "",
            "RUN git config --global --add safe.directory '*'",
            "",
            "WORKDIR /home/",
            "",
            acquire,
            "",
            self.clear_env,
            "",
            'CMD ["/bin/bash"]',
            "",
        ]

        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


class Shredder60To45ImageDefault(Image):
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
        return Shredder60To45ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _repo_dir(self) -> str:
        return "/home/" + _safe_path_component(self.pr.repo)

    def _test_command(self) -> list[str]:
        repo_dir = self._repo_dir()
        return [
            "export CI=true",
            "export CARGO_TERM_COLOR=never",
            "export RUST_BACKTRACE=1",
            "cd " + repo_dir,
            "cargo test --lib --no-fail-fast || true",
            "cargo test --bins --no-fail-fast || true",
            "for target in tests/*.rs; do",
            '    [ -e "$target" ] || continue',
            '    cargo test --test "$(basename "$target" .rs)" --no-fail-fast || true',
            "done",
            "cargo test --doc --no-fail-fast || true",
            "",
        ]

    def files(self) -> list[File]:
        repo_dir = self._repo_dir()
        sha = self.pr.base.sha

        check_git_changes = "\n".join(
            [
                "#!/bin/bash",
                "set -e",
                "",
                "cd " + repo_dir,
                "",
                "if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then",
                '    echo "check_git_changes: not a git repository" >&2',
                "    exit 1",
                "fi",
                "",
                'if [ -n "$(git status --porcelain)" ]; then',
                '    echo "check_git_changes: working tree is dirty" >&2',
                "    git status --porcelain >&2",
                "    exit 1",
                "fi",
                "",
                'echo "check_git_changes: clean"',
                "",
            ]
        )

        prepare = "\n".join(
            [
                "#!/bin/bash",
                "set -e",
                "",
                "cd " + repo_dir,
                "git reset --hard",
                "git clean -fdx",
                "bash /home/check_git_changes.sh",
                'if ! git cat-file -e "' + sha + '^{commit}" 2>/dev/null; then',
                '    git fetch --no-tags origin "' + sha + '"',
                "fi",
                'git checkout --detach "' + sha + '"',
                "bash /home/check_git_changes.sh",
                "",
                "export CI=true",
                "export CARGO_TERM_COLOR=never",
                "",
                "rustc --version",
                "cargo --version",
                "",
                "test -f Cargo.lock || cargo generate-lockfile || true",
                "cargo fetch || true",
                "cargo test --all --no-run --no-fail-fast || true",
                "",
                "cargo test --all --no-run --no-fail-fast",
                'echo "DEPS_OK"',
                "",
            ]
        )

        run_sh = "\n".join(
            ["#!/bin/bash", "set -eo pipefail", ""] + self._test_command()
        )

        test_run_sh = "\n".join(
            [
                "#!/bin/bash",
                "set -eo pipefail",
                "",
                "cd " + repo_dir,
                "git apply --whitespace=nowarn /home/test.patch",
                "",
            ]
            + self._test_command()
        )

        fix_run_sh = "\n".join(
            [
                "#!/bin/bash",
                "set -eo pipefail",
                "",
                "cd " + repo_dir,
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                "",
            ]
            + self._test_command()
        )

        return [
            File(".", "fix.patch", _strip_binary_sections(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_sections(self.pr.test_patch)),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)
        sha = self.pr.base.sha

        copy_commands = "\n".join(
            "COPY " + file.name + " /home/" for file in self.files()
        )

        lines = [
            "FROM " + name + ":" + tag,
            "",
            self.global_env,
            "",
            copy_commands,
            "",
            "WORKDIR /home/" + repo,
            "",
            "RUN git reset --hard",
            "RUN git checkout " + sha,
            "",
            "RUN bash /home/prepare.sh",
            "",
            _prune_block(repo, sha),
            "",
            _submodule_block(repo),
            "",
            self.clear_env,
            "",
        ]

        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


_TARGET_LINE = re.compile(r"^\s*Running\s+(unittests\s+)?(\S+)\s+\(")
_DOCTEST_LINE = re.compile(r"^\s*Doc-tests\s+(\S+)\s*$")
_TEST_LINE = re.compile(r"^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)\b")
_DURATION = re.compile(r"\s*<\s*[\d.]+s\s*>\s*$")


def _parse_cargo_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log or "")

    current_target = ""

    for raw_line in clean_log.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        target = _TARGET_LINE.match(line)
        if target:
            path = target.group(2)
            current_target = ("unittests::" + path) if target.group(1) else path
            continue

        doctest = _DOCTEST_LINE.match(line)
        if doctest:
            current_target = "doc-tests/" + doctest.group(1)
            continue

        entry = _TEST_LINE.match(line.strip())
        if not entry:
            continue

        title = _DURATION.sub("", entry.group(1)).strip()
        if not title:
            continue
        name = (current_target + "::" + title) if current_target else title
        status = entry.group(2)
        if status == "ok":
            passed_tests.add(name)
        elif status == "FAILED":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    passed_tests -= skipped_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class SHREDDER_60_TO_45(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Shredder60To45ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return _parse_cargo_log(log)


for _org, _name in (("Others", _ERA_KEY), ("Others", "shredder")):
    Instance.register(_org, _name)(SHREDDER_60_TO_45)
