import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTEST_CMD = 'pytest -n auto --dist loadgroup -m "not release and not benchmark and not docs" -v -rA --tb=no -p no:cacheprovider -W ignore::ResourceWarning -W ignore::pytest.PytestUnraisableExceptionWarning'

MATURIN_BUILD = "maturin develop"

MATURIN_PIN = "maturin==1.5.1"

SHELL_ENV = """\
export CI=true
export PY_COLORS=0
export CARGO_TERM_COLOR=never
export RUSTFLAGS="-C debuginfo=0"
export VIRTUAL_ENV=/home/polars/py-polars/venv
export PATH="$VIRTUAL_ENV/bin:/usr/local/cargo/bin:$PATH"
"""


class PolarsMidImageBase(Image):
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
        return "rust:1.78-bookworm"

    def image_tag(self) -> str:
        return "base-16429_to_14376"

    def workdir(self) -> str:
        return "base-16429_to_14376"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""\
# syntax=docker/dockerfile:1.6

FROM rust:1.78-bookworm

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates cmake git graphviz python3 python3-venv \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class PolarsMidImageDefault(Image):
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
        return PolarsMidImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

{shell_env}

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

TOOLCHAIN=$(sed -n 's/^[[:space:]]*channel[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' rust-toolchain.toml)
rustup toolchain install "$TOOLCHAIN" --profile minimal --no-self-update
rustc --version
cargo --version

USE_UV=0
if grep -q 'pip install uv' .github/workflows/test-python.yml; then
  USE_UV=1
fi

cd py-polars
python3 -m venv venv

warm() {{
  if timeout "$2" bash -c "$3" > /tmp/warm.log 2>&1; then
    echo "warm-up $1: OK" >> /home/.warm_status
  else
    echo "warm-up $1: INCOMPLETE (exit $?)" >> /home/.warm_status
    tail -40 /tmp/warm.log || true
  fi
}}

REQS="-r requirements-dev.txt"
if [ -f requirements-ci.txt ]; then
  REQS="$REQS -r requirements-ci.txt"
fi
if [ "$USE_UV" = "1" ]; then
  warm deps 3600 "pip install uv && uv pip install --compile-bytecode $REQS 'numpy<2' '{maturin_pin}'"
else
  warm deps 3600 "pip install $REQS 'numpy<2' '{maturin_pin}'"
fi

warm build 7200 "maturin develop"

cat /home/.warm_status

pytest --version
maturin --version
python -c "import polars; print('polars', polars.__version__)"
""".format(pr=self.pr, shell_env=SHELL_ENV, maturin_pin=MATURIN_PIN),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}/py-polars
{maturin}
{pytest}
""".format(pr=self.pr, shell_env=SHELL_ENV, maturin=MATURIN_BUILD, pytest=PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cd /home/{pr.repo}/py-polars
{maturin}
{pytest}
""".format(pr=self.pr, shell_env=SHELL_ENV, maturin=MATURIN_BUILD, pytest=PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd /home/{pr.repo}/py-polars
{maturin}
{pytest}
""".format(pr=self.pr, shell_env=SHELL_ENV, maturin=MATURIN_BUILD, pytest=PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

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

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_ID_PREFIX = "py-polars/"

_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<name>[^\s]+?)(?:\s+-\s+.*)?$"
)

_PROGRESS_RE = re.compile(
    r"^(?P<name>[^\s]+::[^\s]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
)

_XDIST_RE = re.compile(
    r"^\[gw\d+\]\s+\[\s*\d+%\]\s+"
    r"(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<name>(?:\S*::.*?|\S+))\s*$"
)

_FAIL_STATUSES = {"FAILED", "ERROR"}
_SKIP_STATUSES = {"SKIPPED", "XFAIL", "XPASS"}


def parse_pytest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = (
            _SUMMARY_RE.match(line)
            or _PROGRESS_RE.match(line)
            or _XDIST_RE.match(line)
        )
        if not match:
            continue

        name = match.group("name")
        if "::" not in name and "/" not in name:
            continue
        name = _ID_PREFIX + name

        status = match.group("status")
        if status in _FAIL_STATUSES:
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif name not in failed_tests:
            if status in _SKIP_STATUSES:
                if name not in passed_tests:
                    skipped_tests.add(name)
            else:
                skipped_tests.discard(name)
                passed_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("pola-rs", "polars_16429_to_14376")
class POLARS_16429_TO_14376(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PolarsMidImageDefault(self.pr, self._config)

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
        return parse_pytest_log(test_log)
