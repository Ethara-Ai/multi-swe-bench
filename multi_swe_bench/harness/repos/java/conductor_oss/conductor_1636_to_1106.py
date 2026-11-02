import html
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "conductor-oss"
_REPO = "conductor"
_INTERVAL = "conductor_1636_to_1106"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_ROUTING_FLAG = "_conductor_1636_to_1106_routed"

if not getattr(Instance, _ROUTING_FLAG, False):
    _orig_create = Instance.create.__func__

    def _routed_create(cls, pr, config, *args, **kwargs):
        if (
            not pr.number_interval
            and pr.org.lower() == _ORG
            and pr.repo == _REPO
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return _orig_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_routed_create)
    setattr(Instance, _ROUTING_FLAG, True)

_NODE_VERSION = "22.20.0"
_PNPM_VERSION = "10.34.1"
_UI_MODULE = "ui-next"
_EXCLUDED_MODULES = {"e2e", "test-harness"}
_GRADLEW = "./gradlew --no-daemon --no-build-cache --console=plain"
_RESULTS_MARKER = "##TEST_RESULTS##"

_GIT_CHECK = """#!/bin/bash
set -eo pipefail

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

_PROJECT_LOOP = """for module in {modules}; do
    if grep -Eq "^include ['\\"]${{module//\\//-}}['\\"]" settings.gradle; then
        projects+=(":conductor-${{module//\\//-}}")
    fi
done
"""

_RUN_TESTS = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
status=0
projects=()
{project_loop}tasks=()
for project in "${{projects[@]}}"; do
    tasks+=("$project:cleanTest" "$project:test")
done
if [ "${{#tasks[@]}}" -gt 0 ]; then
    {gradlew} --continue -x jacocoTestReport "${{tasks[@]}}" || status=$?
    while read -r result; do
        echo "{marker} $result"
        sed '/<system-out>/,$d' "$result"
    done < <(find . -path '*/build/test-results/test/TEST-*.xml' | sort)
fi
{ui_tests}exit "$status"
"""

_UI_TESTS = """(cd {ui} && pnpm exec vitest run --reporter=verbose) || status=$?
"""

_PREPARE = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

projects=()
{project_loop}resolve_tasks=()
compile_tasks=()
for project in "${{projects[@]}}"; do
    resolve_tasks+=("$project:test" --tests "__resolve_test_dependencies__")
    compile_tasks+=("$project:testClasses")
done
if [ "${{#compile_tasks[@]}}" -gt 0 ]; then
    {gradlew} "${{compile_tasks[@]}}"
    {gradlew} --continue -x jacocoTestReport "${{resolve_tasks[@]}}" || true
fi
{ui_install}
if [ "${{#compile_tasks[@]}}" -gt 0 ]; then
    {gradlew} --offline "${{compile_tasks[@]}}"
fi
{ui_gate}echo DEPS_OK
"""

_UI_INSTALL = """(cd {ui} && pnpm install --frozen-lockfile)
"""

_UI_GATE = """(cd {ui} && pnpm exec vitest --version)
"""

_STAGE = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{apply}bash /home/run_tests.sh
"""


def _test_modules(pr: PullRequest) -> set[str]:
    paths = re.findall(r"^diff --git a/(\S+) b/", pr.test_patch, re.MULTILINE)
    return {path.split("/src/", 1)[0] for path in paths if "/src/" in path}


def _gradle_modules(pr: PullRequest) -> str:
    return " ".join(sorted(_test_modules(pr) - _EXCLUDED_MODULES - {_UI_MODULE}))


def _has_ui_tests(pr: PullRequest) -> bool:
    return _UI_MODULE in _test_modules(pr)


def _project_loop(pr: PullRequest) -> str:
    modules = _gradle_modules(pr)
    return _PROJECT_LOOP.format(modules=modules) if modules else ""


class Conductor1636To1106ImageBase(Image):
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
        return "eclipse-temurin:21.0.13_9-jdk-noble"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    git -C /home/{repo} rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{repo}.git"
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
    CI=true

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl xz-utils ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    case "$(dpkg --print-architecture)" in \\
        amd64) node_arch=x64 ;; \\
        arm64) node_arch=arm64 ;; \\
        *) exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-$node_arch.tar.xz" \\
        | tar -xJ -C /usr/local --strip-components=1; \\
    npm install -g pnpm@{_PNPM_VERSION}; \\
    node --version; \\
    pnpm --version

RUN git config --global --add safe.directory '*'

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Conductor1636To1106ImageDefault(Image):
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
        return Conductor1636To1106ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        has_ui = _has_ui_tests(self.pr)
        apply_cmd = "git apply --whitespace=nowarn --exclude='*.png'"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _GIT_CHECK),
            File(
                ".",
                "run_tests.sh",
                _RUN_TESTS.format(
                    repo=repo,
                    project_loop=_project_loop(self.pr),
                    gradlew=_GRADLEW,
                    marker=_RESULTS_MARKER,
                    ui_tests=_UI_TESTS.format(ui=_UI_MODULE) if has_ui else "",
                ),
            ),
            File(
                ".",
                "prepare.sh",
                _PREPARE.format(
                    repo=repo,
                    sha=self.pr.base.sha,
                    project_loop=_project_loop(self.pr),
                    gradlew=_GRADLEW,
                    ui_install=_UI_INSTALL.format(ui=_UI_MODULE) if has_ui else "",
                    ui_gate=_UI_GATE.format(ui=_UI_MODULE) if has_ui else "",
                ),
            ),
            File(".", "run.sh", _STAGE.format(repo=repo, apply="")),
            File(
                ".",
                "test-run.sh",
                _STAGE.format(repo=repo, apply=f"{apply_cmd} /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                _STAGE.format(
                    repo=repo,
                    apply=f"{apply_cmd} /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        proxy_setup = ""
        proxy_cleanup = ""
        proxy = re.search(
            r"^ENV\s*https?_proxy=\"?https?://([^:/\s\"]+):(\d+)",
            self.global_env,
            re.MULTILINE,
        )
        if proxy:
            host, port = proxy.groups()
            proxy_setup = (
                "RUN mkdir -p /root/.gradle && printf '%s\\n' \\\n"
                f"    'systemProp.http.proxyHost={host}' \\\n"
                f"    'systemProp.http.proxyPort={port}' \\\n"
                f"    'systemProp.https.proxyHost={host}' \\\n"
                f"    'systemProp.https.proxyPort={port}' \\\n"
                "    >> /root/.gradle/gradle.properties"
            )
            proxy_cleanup = "RUN rm -f /root/.gradle/gradle.properties"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register(_ORG, _INTERVAL)
class CONDUCTOR_1636_TO_1106(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Conductor1636To1106ImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_testcase = re.compile(
            r'<testcase\b((?:[^>"]|"[^"]*")*?)(/>|>(.*?)</testcase>)', re.DOTALL
        )
        re_name = re.compile(r'\bname="([^"]*)"')
        re_classname = re.compile(r'\bclassname="([^"]*)"')
        re_vitest = re.compile(
            r"^\s*([✓√×✗↓])\s+(\S.*?\s>\s.*?)(?:\s+\[skipped\])?(?:\s+\(retry x\d+\))?(?:\s+\d+(?:\.\d+)?\s*m?s)?\s*$"
        )

        chunks = re.split(
            rf"^{re.escape(_RESULTS_MARKER)} (\S+)\s*$", test_log, flags=re.MULTILINE
        )
        for path, content in zip(chunks[1::2], chunks[2::2]):
            module = path.removeprefix("./").split("/build/test-results/", 1)[0]
            for match in re_testcase.finditer(content):
                attrs, body = match.group(1), match.group(3) or ""
                method = re_name.search(attrs)
                class_name = re_classname.search(attrs)
                if not method or not class_name:
                    continue
                name = html.unescape(f"{module}:{class_name.group(1)}.{method.group(1)}")
                if "<failure" in body or "<error" in body:
                    failed_tests.add(name)
                elif "<skipped" in body:
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

        for line in test_log.splitlines():
            match = re_vitest.match(line)
            if match:
                status, test = match.groups()
                name = f"{_UI_MODULE}:{test}"
                if status in ("×", "✗"):
                    failed_tests.add(name)
                elif status == "↓":
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

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
