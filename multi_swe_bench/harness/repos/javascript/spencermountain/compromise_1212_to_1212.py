import os
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_NODE_IMAGE = "node:26.13.1-bookworm"
_FIX_DEPS_PATCH = "fix-deps.patch"
_FIX_DEPS_DIR = "/tmp/mswe-fix-deps"
_FIX_DEPS_TAR = "/home/mswe-fix-node-modules.tar.gz"
_FIX_DEPS_LIST = "/home/mswe-fix-node-modules.list"
_RUNNER = "/home/mswe-tape.sh"
_DEV_ROOTS = (
    ("tests", "*.test.js", ""),
    ("scripts/test", "smoke.test.js", ""),
    ("plugins", "*.test.js", "*/tests/*"),
)
_PROD_ROOTS = (("tests", "*.test.js", ""),)
_SUITES = (("dev", "", _DEV_ROOTS), ("prod", "prod", _PROD_ROOTS))
_PLUGIN_GATE = "./plugins/dates/src/plugin.js"
_DEPENDENCY_FILES = re.compile(r"(^|/)(package\.json|package-lock\.json)$")
_INSTALL_ATTEMPTS = 3
_FILE_TIMEOUT = 600

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_LINE = re.compile(r"^MSWE_FILE (\S+)$")
_EXIT_LINE = re.compile(r"MSWE_EXIT (\S+) (\d+)$")
_HEADER_LINE = re.compile(r"^# (?!tests \d|pass \d|fail \d|ok$)(.+)$")
_TAP_LINE = re.compile(r"^(?:(not ok)|ok)\s+\d+\s*(.*?)(?:\s+# (SKIP|TODO)(?:\s.*)?)?$")

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: Not inside a git repository"
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)$", patch, re.M)


def _dependency_patch(pr: PullRequest) -> str:
    sections = re.split(r"(?=^diff --git )", pr.fix_patch, flags=re.M)
    return "".join(
        section
        for section in sections
        if (match := re.match(r"diff --git a/\S+ b/(\S+)", section))
        and _DEPENDENCY_FILES.search(match.group(1))
    )


def _changes_dependencies(pr: PullRequest) -> bool:
    return bool(_dependency_patch(pr))


def _find_tests(roots: tuple) -> str:
    clauses = []
    for root, name, path in roots:
        path_filter = f" -path '{path}'" if path else ""
        clauses.append(
            f"find {root} -name node_modules -prune -o -type f{path_filter} -name '{name}' -print"
        )
    return "{ " + "; ".join(clauses) + "; } | LC_ALL=C sort"


def _suite_block(label: str, testenv: str, roots: tuple) -> str:
    env = f"env TESTENV={testenv}" if testenv else "env -u TESTENV"
    return f"""files=$({_find_tests(roots)})
if [ -z "$files" ]; then
    echo "mswe-tape: no {label} test files found"
    exit 1
fi
for f in $files; do
    echo "MSWE_FILE {label}:$f"
    rc=0
    {env} timeout {_FILE_TIMEOUT} node_modules/.bin/tape "$f" 2>&1 || rc=$?
    echo "MSWE_EXIT {label}:$f $rc"
done"""


def _runner_script(repo: str) -> str:
    return _sections(
        "#!/bin/bash\nset -o pipefail",
        f"cd /home/{repo}",
        *(_suite_block(label, testenv, roots) for label, testenv, roots in _SUITES),
    )


def _retry_block() -> str:
    return f"""retry() {{
    for attempt in $(seq 1 {_INSTALL_ATTEMPTS}); do
        if "$@"; then
            return 0
        fi
        if [ "$attempt" -eq {_INSTALL_ATTEMPTS} ]; then
            return 1
        fi
        sleep 15
    done
}}"""


def _fix_deps_block(pr: PullRequest) -> str:
    if not _changes_dependencies(pr):
        return ""
    return f"""rm -rf {_FIX_DEPS_DIR}
mkdir -p {_FIX_DEPS_DIR}
git ls-files -z -- '*package.json' '*package-lock.json' | xargs -0 cp --parents -t {_FIX_DEPS_DIR}
(cd {_FIX_DEPS_DIR} && git apply --whitespace=nowarn /home/{_FIX_DEPS_PATCH})
(cd {_FIX_DEPS_DIR} && find . -name node_modules -prune -o -name package-lock.json -print | xargs -n1 dirname | LC_ALL=C sort > {_FIX_DEPS_LIST})
test -s {_FIX_DEPS_LIST}
while read -r dir; do
    (cd "{_FIX_DEPS_DIR}/$dir" && retry npm ci --ignore-scripts)
done < {_FIX_DEPS_LIST}
test -x {_FIX_DEPS_DIR}/node_modules/.bin/tape
(cd {_FIX_DEPS_DIR} && sed 's#$#/node_modules#' {_FIX_DEPS_LIST} | tar -czf {_FIX_DEPS_TAR} -T -)
test -s {_FIX_DEPS_TAR}
rm -rf {_FIX_DEPS_DIR}
npm cache clean --force"""


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        "export CI=true",
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        "node --version\nnpm --version",
        _retry_block(),
        "retry npm ci\nretry npm run plugins:ci",
        _fix_deps_block(pr),
        f"""test -x node_modules/.bin/tape
test -s {_RUNNER}
test_files=$({_find_tests(_DEV_ROOTS)} | wc -l)
echo "prepare: $test_files tape test files"
test "$test_files" -gt 0
node --input-type=module -e "const {{default: nlp}} = await import('./src/three.js'); const {{default: plugin}} = await import('{_PLUGIN_GATE}'); nlp.plugin(plugin); if (!nlp('prepare check').found) process.exit(1); console.log('prepare: compromise loads ' + nlp.version)\"""",
    )


def _run_script(repo: str, apply: str = "", restore_deps: bool = False) -> str:
    restore = (
        f"""while read -r dir; do
    rm -rf "$dir/node_modules"
done < {_FIX_DEPS_LIST}
tar -xzf {_FIX_DEPS_TAR}"""
        if restore_deps
        else ""
    )
    setup = "\n".join(line for line in (f"cd /home/{repo}", apply) if line)
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        "export CI=true",
        setup,
        restore,
        f"bash {_RUNNER}",
    )


class Compromise1212To1212ImageBase(Image):
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
        return _NODE_IMAGE

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

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
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --system --add safe.directory '*' && node --version && npm --version"""
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


class Compromise1212To1212ImageDefault(Image):
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
        return Compromise1212To1212ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        dependency_patch = _dependency_patch(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            *([File(".", _FIX_DEPS_PATCH, dependency_patch)] if dependency_patch else []),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", os.path.basename(_RUNNER), _runner_script(repo)),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(
                ".",
                "fix-run.sh",
                _run_script(repo, fix_apply, restore_deps=_changes_dependencies(self.pr)),
            ),
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
class COMPROMISE_1212_TO_1212(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Compromise1212To1212ImageDefault(self.pr, self._config)

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
        seen: dict[str, int] = {}
        current_file = ""
        header = ""
        file_failed = False

        def record(name: str, status: str) -> None:
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [{seen[name]}]"
            buckets[status].add(name)

        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.rstrip()
            file_match = _FILE_LINE.match(line)
            if file_match:
                current_file, header, file_failed = file_match.group(1), "", False
                continue
            exit_match = _EXIT_LINE.search(line)
            if exit_match:
                if exit_match.group(2) != "0" and not file_failed:
                    record(f"{exit_match.group(1)} > <file failed to run>", "fail")
                current_file, header = "", ""
                continue
            if not current_file:
                continue
            header_match = _HEADER_LINE.match(line)
            if header_match:
                header = header_match.group(1).strip()
                continue
            tap_match = _TAP_LINE.match(line)
            if not tap_match:
                continue
            not_ok, message, directive = tap_match.groups()
            name = " > ".join(part for part in (current_file, header, message.strip()) if part)
            if directive:
                record(name, "skip")
            elif not_ok:
                file_failed = True
                record(name, "fail")
            else:
                record(name, "pass")

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
