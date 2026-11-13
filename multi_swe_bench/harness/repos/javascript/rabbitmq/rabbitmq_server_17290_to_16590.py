import os
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_BEAM_IMAGE = "elixir:1.19.7-otp-28"
_CT_ARGS = "NON_DETERMINISTIC=1"
_CT_TIMEOUT = "3600"
_CT_LOGS = "/home/ct_logs"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DIFF_PATH = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_SUITE_PATH = re.compile(r"^(deps/[^/]+)/test/([A-Za-z0-9_]+)_SUITE\.erl$")
_BINARY_DIFF = re.compile(r"^Binary files (?:a/(.+?)|/dev/null) and (?:b/(.+?)|/dev/null) differ$", re.M)
_GLOB_SPECIAL = re.compile(r"([*?\[\\])")
_CT_LINE = re.compile(r"^CTRESULT\s+(\S+)\s+(\S+)")
_CT_STATUS = (("ok", "pass"), ("failed", "fail"), ("auto_skipped", "skip"), ("user_skipped", "skip"), ("skipped", "skip"))

_CHECK_GIT_CHANGES = """#!/bin/bash
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
"""

_CT_REPORT = r"""#!/bin/bash
awk -v prefix="$1" '
function path(    i, out) { out = ""; for (i = 1; i <= depth; i++) out = out stack[i] "/"; return out }
/^=case/ { name = $2; sub(/^[^:]*:/, "", name); next }
/^=group_props/ {
    if (name == "init_per_group" && match($0, /\{name,[^,}]*/)) { stack[++depth] = substr($0, RSTART + 6, RLENGTH - 6) }
    next
}
/^=result/ {
    if (name == "end_per_group" && depth > 0) { depth--; name = ""; next }
    if (name == "" || name ~ /^(init|end)_per_/) { name = ""; next }
    result = $2; sub(/,.*/, "", result)
    print "CTRESULT " prefix ":" path() name " " result
    name = ""
}
' "$2"
"""


def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _suites(*patches: str) -> list[tuple[str, str]]:
    found: dict[tuple[str, str], None] = {}
    for patch in patches:
        for path in _DIFF_PATH.findall(patch):
            match = _SUITE_PATH.match(path)
            if match:
                found[match.groups()] = None
    return sorted(found)


def _apps(suites: list[tuple[str, str]]) -> list[str]:
    return sorted({app for app, _ in suites})


def _ct_script(suites: list[tuple[str, str]]) -> str:
    pairs = " ".join(shlex.quote(f"{app}:{suite}") for app, suite in suites)
    return f"""#!/bin/bash
set -o pipefail

status=0
for pair in {pairs}; do
    app="${{pair%%:*}}"
    suite="${{pair#*:}}"
    if [ ! -f "$app/test/${{suite}}_SUITE.erl" ]; then
        continue
    fi
    logs="{_CT_LOGS}/$(basename "$app")/$suite"
    rm -rf "$logs"
    mkdir -p "$logs"
    timeout {_CT_TIMEOUT} make -C "$app" "ct-$suite" {_CT_ARGS} CT_LOGS_DIR="$logs" 2>&1 || status=$?
    for log in $(find "$logs" -name suite.log 2>/dev/null | sort); do
        bash /home/ct_report.sh "$(basename "$app")/${{suite}}_SUITE" "$log"
    done
done
exit $status
"""


def _prepare_script(repo: str, sha: str, apps: list[str]) -> str:
    targets = " ".join(shlex.quote(app) for app in apps)
    return f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
bash /home/check_git_changes.sh

for app in {targets}; do
    if [ -d "$app" ]; then
        make -C "$app" test-build {_CT_ARGS}
    fi
done

bash /home/check_git_changes.sh
"""


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{repo}", apply) if line)
    return f"""#!/bin/bash
set -eo pipefail
export CI=true

{setup}

bash /home/ct_run.sh
"""


def _binary_paths(*patches: str) -> list[str]:
    found: dict[str, None] = {}
    for patch in patches:
        for old, new in _BINARY_DIFF.findall(patch):
            found[new or old] = None
    return list(found)


def _apply_command(excluded: list[str], *patch_files: str) -> str:
    flags = [
        "--exclude=" + shlex.quote(_GLOB_SPECIAL.sub(r"\\\1", path))
        for path in excluded
    ]
    return " ".join(["git apply --whitespace=nowarn", *flags, *patch_files])


def _ct_result(line: str) -> tuple[str, str] | None:
    match = _CT_LINE.match(line)
    if not match:
        return None
    name, raw = match.groups()
    for prefix, result in _CT_STATUS:
        if raw.startswith(prefix):
            return name, result
    return None


class RabbitmqServerEraImageBase(Image):
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
        return _BEAM_IMAGE

    def image_tag(self) -> str:
        hi, lo = re.search(r"_(\d+)_to_(\d+)$", _ERA).groups()
        return f"base-{lo}_to_{hi}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV MIX_ENV=test \\
    ERL_AFLAGS="-kernel shell_history disabled"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential make gawk rsync xz-utils zip unzip p7zip-full \\
    python3 procps net-tools netcat-openbsd openssl \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --system --add safe.directory '*' && \\
    mix local.hex --force && mix local.rebar --force && \\
    erl -noshell -eval 'io:format("~s~n", [erlang:system_info(otp_release)]), halt().' && elixir --version"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class RabbitmqServerEraImageDefault(Image):
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
        return RabbitmqServerEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = _apply_command(
            _binary_paths(self.pr.test_patch),
            "/home/test.patch",
        )
        fix_apply = _apply_command(
            _binary_paths(self.pr.test_patch, self.pr.fix_patch),
            "/home/test.patch",
            "/home/fix.patch",
        )
        suites = _suites(self.pr.test_patch)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "ct_report.sh", _CT_REPORT),
            File(".", "ct_run.sh", _ct_script(suites)),
            File(".", "prepare.sh", _prepare_script(repo, self.pr.base.sha, _apps(suites))),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _ERA)
class RABBITMQ_SERVER_17290_TO_16590(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return RabbitmqServerEraImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}
        for raw in _ANSI.sub("", test_log).splitlines():
            result = _ct_result(raw.strip())
            if result:
                buckets[result[1]].add(result[0])
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
