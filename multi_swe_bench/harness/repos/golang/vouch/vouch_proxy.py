import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

GO_IMAGE = "golang:1.14"

_GO_MOD = """module github.com/vouch/vouch-proxy

go 1.14

require (
	github.com/dgrijalva/jwt-go v3.2.0+incompatible
	github.com/gorilla/mux v1.7.4
	github.com/gorilla/sessions v1.2.0
	github.com/karupanerura/go-mock-http-response v0.0.0-20171201120521-7c242a447d45
	github.com/mitchellh/mapstructure v1.2.2
	github.com/patrickmn/go-cache v2.1.0+incompatible
	github.com/spf13/viper v1.6.3
	github.com/stretchr/testify v1.5.1
	github.com/theckman/go-securerandom v0.1.1
	github.com/tsenart/vegeta v0.0.0-20200307100307-e516e0bac62f
	go.uber.org/zap v1.15.0
	golang.org/x/oauth2 v0.0.0-20200107190931-bf48bf16ab8d
)
"""


def _go_env(repo: str) -> str:
    return (
        "export GO111MODULE=on\n"
        "export GOFLAGS=-mod=readonly\n"
        "export GOPROXY=off\n"
        "export CGO_ENABLED=0\n"
        f"export VOUCH_ROOT=/home/{repo}/\n"
        f"export VOUCH_CONFIG=/home/{repo}/config/testing/test_config.yml\n"
    )


_UNGRADED_TESTS = {"handlers::TestValidateRequestHandlerPerf"}

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"

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


def _test_cmd() -> str:
    return (
        "rm -f /tmp/go-test.json /tmp/go-test.stderr\n"
        "rc=0\n"
        "go test -json -count=1 ./... 2>/tmp/go-test.stderr | tee /tmp/go-test.json || rc=$?\n"
        "cat /tmp/go-test.stderr >&2\n"
        "if ! grep -q '\"Action\"' /tmp/go-test.json; then\n"
        '    echo "go test exited with code $rc without emitting any test events" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
    )


class VouchProxyImageBase(Image):
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
        return GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class VouchProxyImageDefault(Image):
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
        return VouchProxyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        return (
            f"{_SCRIPT_HEADER}"
            "# --- 1. pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- 2. provision ---\n"
            "go version\n"
            "export GO111MODULE=on\n"
            "export CGO_ENABLED=0\n"
            "# Pinned module list (untracked; the run scripts' `git reset --hard` keeps it).\n"
            "cat > go.mod <<'__GO_MOD_EOF__'\n"
            f"{_GO_MOD}"
            "__GO_MOD_EOF__\n"
            "rm -f go.sum\n"
            "downloaded=0\n"
            "for attempt in 1 2 3; do\n"
            "    if go mod download; then downloaded=1; break; fi\n"
            '    echo "prepare: go mod download attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            'if [ "$downloaded" != 1 ]; then\n'
            '    echo "prepare: go mod download failed 3 times" >&2\n'
            "    exit 1\n"
            "fi\n"
            "# Record go.sum for every package the test binaries load. go.mod must come out\n"
            "# byte-identical: any requirement Go had to add would be an unpinned resolution.\n"
            "cp go.mod /tmp/go.mod.pinned\n"
            "go list -mod=mod -deps -test ./... > /dev/null\n"
            "if ! cmp -s go.mod /tmp/go.mod.pinned; then\n"
            '    echo "prepare: pinned go.mod is incomplete; go resolved extra requirements:" >&2\n'
            "    diff /tmp/go.mod.pinned go.mod >&2 || true\n"
            "    exit 1\n"
            "fi\n"
            "\n"
            "# --- 3. gate ---\n"
            "# The graded environment (offline, read-only go.mod) must compile every package and\n"
            "# every test binary, and `go test` must see a non-empty test list.\n"
            f"{_go_env(repo)}"
            "test -s go.mod\n"
            "test -s go.sum\n"
            "go build ./...\n"
            "go test -count=1 -run '^$' ./...\n"
            "test_files=$(find . -name '*_test.go' -not -path './.git/*' | wc -l)\n"
            'echo "test files: $test_files"\n'
            'test "$test_files" -gt 0\n'
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        repo = self.pr.repo
        return (
            f"{_SCRIPT_HEADER}"
            f"{_go_env(repo)}\n"
            f"cd /home/{repo}\n"
            f"{_test_cmd()}"
        )

    def _test_run_sh(self) -> str:
        repo = self.pr.repo
        return (
            f"{_SCRIPT_HEADER}"
            f"{_go_env(repo)}\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd()}"
        )

    def _fix_run_sh(self) -> str:
        repo = self.pr.repo
        return (
            f"{_SCRIPT_HEADER}"
            f"{_go_env(repo)}\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_test_cmd()}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("vouch", "vouch-proxy")
class VouchProxy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VouchProxyImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        module = "github.com/vouch/vouch-proxy"

        for line in log.splitlines():
            line = line.strip()
            start = line.find('{"Time"')
            if start == -1:
                continue
            try:
                event = json.loads(line[start:])
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            action = event.get("Action")
            test = event.get("Test")
            if not test or action not in ("pass", "fail", "skip"):
                continue

            pkg = event.get("Package") or ""
            if pkg == module:
                pkg = "."
            elif pkg.startswith(module + "/"):
                pkg = pkg[len(module) + 1 :]
            name = f"{pkg}::{test}"
            if f"{pkg}::{test.split('/', 1)[0]}" in _UNGRADED_TESTS:
                continue

            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
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
