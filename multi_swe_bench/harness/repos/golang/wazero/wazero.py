import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# wazero/wazero (formerly tetratelabs/wazero; module path at these commits is
# github.com/tetratelabs/wazero). go directive at each PR's base commit and the
# Go release current when the PR was merged (what CI used):
#   #242  (2022-02) go 1.16 -> golang:1.17
#   #466  (2022-04) go 1.17 -> golang:1.18
#   #935  (2022-12) go 1.17 -> golang:1.19
#   #1558 (2023-07) go 1.18 -> golang:1.20
# Only the root module is tested; the nested go.mod files (vs/*, fuzz, example
# stars, version testdata) need cgo, external runtimes or tinygo-built inputs.
_GO_VERSION_BY_PR = {
    242: "1.17",
    466: "1.18",
    935: "1.19",
    1558: "1.20",
}


def _go_version(pr: PullRequest) -> str:
    if pr.number in _GO_VERSION_BY_PR:
        return _GO_VERSION_BY_PR[pr.number]
    if pr.number < 400:
        return "1.17"
    if pr.number < 800:
        return "1.18"
    if pr.number < 1200:
        return "1.19"
    return "1.20"


# Each package of the root module is tested on its own: some test.patches
# (e.g. #466) change a test file's package clause so the package cannot even be
# loaded, and a single `go test ./...` would then abort before running anything.
# A package that produces no result line of its own (load error) is reported
# as `FAIL <pkg> [setup failed]`.
TEST_COMMAND = r"""rc=0
for pkg in $(go list -e ./...); do
  log="$(mktemp)"
  if ! go test -v -count=1 "${pkg}" > "${log}" 2>&1; then
    rc=1
  fi
  cat "${log}"
  if ! grep -Eq "^(ok|FAIL|\?)[[:space:]]+${pkg}([[:space:]]|$)" "${log}"; then
    printf 'FAIL\t%s [setup failed]\n' "${pkg}"
  fi
  rm -f "${log}"
done
exit ${rc}"""


_POINTER_SUBTEST = re.compile(r"^(0x)?[0-9a-f]{8,16}$")


class WazeroImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return f"golang:{_go_version(self.pr)}"

    def image_tag(self) -> str:
        return f"base-go{_go_version(self.pr)}"

    def workdir(self) -> str:
        return f"base-go{_go_version(self.pr)}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"""RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"""
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    GOTOOLCHAIN=local \\
    GO111MODULE=on \\
    CGO_ENABLED=0 \\
    CI=true

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

# The official golang images already ship git, ca-certificates, gcc and make.
# Older tags (e.g. golang:1.13 on Debian buster) point at retired apt mirrors, so
# only fall back to apt when something is actually missing.
RUN if ! command -v git >/dev/null 2>&1 || ! command -v gcc >/dev/null 2>&1; then \\
        apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential && rm -rf /var/lib/apt/lists/*; \\
    fi

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class WazeroImageDefault(Image):
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
        return WazeroImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def test_command(self) -> str:
        return TEST_COMMAND

    def files(self) -> list[File]:
        repo = self.pr.repo
        prepare = (
            r"""#!/bin/bash
set -euo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
bash /home/check_git_changes.sh

# Root module only (see the note at the top of this file).
go mod download
go build ./...
go test -count=1 -run '^$' ./... > /dev/null__PRECOMPILE_TAIL__

# Must leave tracked files untouched (the grader resets the tree before each stage).
git checkout -- .
# Final hard gate (last step): the restored tree still builds.
go build ./...
echo DEPS_OK
"""
            .replace("__REPO__", repo)
            .replace("__SHA__", self.pr.base.sha)
            # Before ~#400 tests/bench and tests/codec import wasmer-go/wasmtime-go on amd64,
            # which need cgo (CGO_ENABLED=0 here); they never hold graded tests, so warming
            # the test cache must not abort the build on amd64.
            .replace("__PRECOMPILE_TAIL__", " || true" if self.pr.number < 400 else "")
        )

        def stage(apply: str) -> str:
            return (
                "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                f"cd /home/{repo}\n{apply}{TEST_COMMAND}\n"
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", stage("")),
            File(
                ".",
                "test-run.sh",
                stage("git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                stage(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

{self.clear_env}

"""


@Instance.register("wazero", "wazero")
class Wazero(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WazeroImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_run = re.compile(r"^=== RUN\s+(\S+)")
        re_result = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        re_package = re.compile(r"^(ok|FAIL)\s+(\S+)(\s|$)")

        pending_run = set()
        pending_result = {}

        for line in log.splitlines():
            run_match = re_run.match(line)
            if run_match:
                pending_run.add(run_match.group(1))
                continue

            result_match = re_result.match(line)
            if result_match:
                pending_result[result_match.group(2)] = result_match.group(1)
                continue

            package_match = re_package.match(line)
            if not package_match:
                continue

            package = package_match.group(2)
            for name in pending_run - set(pending_result):
                pending_result[name] = "FAIL"
            for name, status in pending_result.items():
                # api.TestEncodeDecodeExternRef names its subtests after a heap
                # pointer (e.g. ".../40000ab520"), which differs on every run;
                # such subtests are dropped (the parent test is still recorded).
                if "/" in name and _POINTER_SUBTEST.match(name.rsplit("/", 1)[1]):
                    continue
                full_name = f"{package}/{name}"
                if status == "PASS":
                    passed_tests.add(full_name)
                elif status == "FAIL":
                    failed_tests.add(full_name)
                else:
                    skipped_tests.add(full_name)
            pending_run = set()
            pending_result = {}

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
