import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_STEM = Path(__file__).resolve().stem
_STEM_MATCH = re.match(r"^(?P<repo>.+?)_(?P<lo>\d+)_to_(?P<hi>\d+)$", _STEM)
if _STEM_MATCH is None:
    raise ValueError(f"unsupported era config filename: {_STEM}")

ERA_REPO = _STEM_MATCH.group("repo")
ERA_PR_MIN = int(_STEM_MATCH.group("lo"))
ERA_PR_MAX = int(_STEM_MATCH.group("hi"))

ERA_INTERVAL = _STEM

ERA_ORG = "pola-rs"

PY_SUBDIR = "py-polars"

BASE_IMAGE = "python:3.11-slim-bookworm"

TOOLCHAIN_FILE = "rust-toolchain.toml"
TOOLCHAIN_FILE_LEGACY = "rust-toolchain"
TOOLCHAIN_FALLBACK_GLOB = ".github/workflows/*.y*ml"
NIGHTLY_PATTERN = "nightly-[0-9]{4}-[0-9]{2}-[0-9]{2}"

PYTEST_CMD = (
    "pytest tests/unit/ -v -rA --tb=no -p no:cacheprovider"
    " --continue-on-collection-errors"
)

MATURIN_BUILD = "maturin develop"

RUST_TEST_SCOPE_FILE = "/home/rust-test-scope"
CARGO_TEST_CMD = "cargo test --all-features $(cat {scope}) --lib"

TOOLCHAIN_PIN_FILE = "/home/rust-toolchain-pin"

SHELL_ENV = """\
export CI=true
export PY_COLORS=0
export CARGO_TERM_COLOR=never
export RUSTFLAGS="-C debuginfo=0"
export RUST_BACKTRACE=1
export CARGO_HOME=/root/.cargo
export RUSTUP_HOME=/root/.rustup
export VIRTUAL_ENV=/home/{repo}/{py}/venv
export PATH="$VIRTUAL_ENV/bin:$CARGO_HOME/bin:$PATH"
if [ -r {pin} ]; then
  RUSTUP_TOOLCHAIN="$(cat {pin})"
  export RUSTUP_TOOLCHAIN
fi""".format(repo=ERA_REPO, py=PY_SUBDIR, pin=TOOLCHAIN_PIN_FILE)

DROP_REQUIREMENT_SCRIPT = r'''import re
import sys

path, bad = sys.argv[1], sys.argv[2]
norm = re.sub(r"[-_.]+", "-", bad).lower()
kept = []
for line in open(path):
    head = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line.strip())
    if head and re.sub(r"[-_.]+", "-", head.group(1)).lower() == norm:
        continue
    kept.append(line)
open(path, "w").writelines(kept)
'''

RESOLVE_AS_OF_SCRIPT = r'''import json
import re
import sys
import urllib.request

name, cutoff = sys.argv[1], sys.argv[2]
req = urllib.request.Request("https://pypi.org/pypi/" + name + "/json")
req.add_header("User-Agent", "multi-swe-bench polars image build")

best_key = None
best_ver = ""
try:
    data = json.load(urllib.request.urlopen(req, timeout=60))
    for ver, files in data["releases"].items():
        if re.match(r"^[0-9]+\.[0-9]+(\.[0-9]+)?$", ver) is None:
            continue
        dates = sorted(f["upload_time_iso_8601"][:10] for f in files if not f.get("yanked"))
        if not dates or dates[0] > cutoff:
            continue
        key = tuple(int(p) for p in ver.split("."))
        if best_key is None or key > best_key:
            best_key = key
            best_ver = ver
except Exception:
    best_ver = ""

sys.stdout.write(name + "==" + best_ver if best_ver else name)
'''

RUST_SCOPE_SCRIPT = r'''import os
import re
import sys

patch_path, repo, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

touched = set()
try:
    with open(patch_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = re.match(r"^\+\+\+ b/(.+?)\s*$", line)
            if match and match.group(1).endswith(".rs"):
                touched.add(match.group(1))
except OSError:
    touched = set()

def crate_of(rel_path):
    directory = os.path.dirname(os.path.join(repo, rel_path))
    while directory.startswith(repo):
        manifest = os.path.join(directory, "Cargo.toml")
        if os.path.isfile(manifest):
            in_package = False
            with open(manifest, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("["):
                        in_package = stripped == "[package]"
                        continue
                    if in_package:
                        found = re.match(r'^name\s*=\s*"([^"]+)"', stripped)
                        if found:
                            return found.group(1)
        directory = os.path.dirname(directory)
    return None

seen = []
for crate in sorted(filter(None, (crate_of(path) for path in touched))):
    if crate not in seen:
        seen.append(crate)

with open(out_path, "w", encoding="utf-8") as handle:
    handle.write(" ".join("-p " + crate for crate in seen))

sys.stderr.write("rust test scope: %s\n" % (" ".join(seen) or "(none)"))
'''

CHECK_GIT_CHANGES = """\
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
"""

PREPARE_SH = """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}

grep -qxF '{py}/venv/' .git/info/exclude 2>/dev/null || echo '{py}/venv/' >> .git/info/exclude
grep -qxF '{py}/wheels/' .git/info/exclude 2>/dev/null || echo '{py}/wheels/' >> .git/info/exclude

git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

TOOLCHAIN=""
if [ -r {toolchain_file} ]; then
  TOOLCHAIN="$(sed -n 's/^[[:space:]]*channel[[:space:]]*=[[:space:]]*"\\(.*\\)".*/\\1/p' {toolchain_file} | head -1 || true)"
fi
if [ -z "$TOOLCHAIN" ] && [ -r {toolchain_legacy} ]; then
  TOOLCHAIN="$(head -1 {toolchain_legacy} | tr -d '[:space:]"' || true)"
fi
if [ -z "$TOOLCHAIN" ]; then
  TOOLCHAIN="$(grep -hoE '{nightly}' {workflows} 2>/dev/null | head -1 || true)"
fi
if [ -z "$TOOLCHAIN" ]; then
  echo "prepare: no rust toolchain pinned in the tree at {sha}" >&2
  exit 1
fi
echo "prepare: rust toolchain $TOOLCHAIN"
echo "$TOOLCHAIN" > {pin}
export RUSTUP_TOOLCHAIN="$TOOLCHAIN"

rustup toolchain install "$TOOLCHAIN" --profile minimal --no-self-update
rustc --version
cargo --version

cd /home/{repo}/{py}
python -m venv venv

REQ="requirements-dev.txt"
[ -f "$REQ" ] || REQ="build.requirements.txt"
if [ ! -f "$REQ" ]; then
  echo "prepare: no dev requirements file in {py}/ at {sha}" >&2
  ls -1 . >&2
  exit 1
fi
echo "prepare: dev requirements $REQ"

pip install --upgrade pip setuptools wheel

cat > /tmp/drop_requirement.py <<'PYEOF'
{drop_script}
PYEOF
INSTALL_REQ=/tmp/requirements-install.txt
cp "$REQ" "$INSTALL_REQ"
: > /home/.dropped_requirements
deps_ok=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if timeout 1800 pip install -r "$INSTALL_REQ" > /tmp/pip.log 2>&1; then
    deps_ok=1
    break
  fi
  bad="$(sed -n 's/^ERROR: No matching distribution found for \\([A-Za-z0-9._-]*\\).*/\\1/p' /tmp/pip.log | head -1)"
  if [ -z "$bad" ]; then
    bad="$(sed -n 's/^ERROR: Could not find a version that satisfies the requirement \\([A-Za-z0-9._-]*\\).*/\\1/p' /tmp/pip.log | head -1)"
  fi
  if [ -z "$bad" ]; then
    tail -40 /tmp/pip.log || true
    break
  fi
  echo "$bad" >> /home/.dropped_requirements
  echo "prepare: dropping unsatisfiable requirement $bad (attempt $attempt)"
  python /tmp/drop_requirement.py "$INSTALL_REQ" "$bad"
done
echo "warm-up deps: $([ "$deps_ok" = "1" ] && echo OK || echo INCOMPLETE) (dropped: $(tr '\\n' ' ' < /home/.dropped_requirements))" >> /home/.warm_status

if grep -qiE '^[[:space:]]*maturin[[:space:]]*==' "$INSTALL_REQ" 2>/dev/null; then
  echo "prepare: maturin pinned by $REQ"
else
  cat > /tmp/resolve_as_of_commit.py <<'PYEOF'
{resolve_script}
PYEOF
  COMMIT_DATE="$(git -C /home/{repo} show -s --format=%cs HEAD)"
  MATURIN_SPEC="$(python /tmp/resolve_as_of_commit.py maturin "$COMMIT_DATE")"
  echo "prepare: maturin unpinned; resolving as of $COMMIT_DATE -> $MATURIN_SPEC"
  pip install "$MATURIN_SPEC"
fi
command -v maturin > /dev/null 2>&1 || pip install maturin

cat > /tmp/rust_test_scope.py <<'PYEOF'
{scope_script}
PYEOF
python /tmp/rust_test_scope.py /home/test.patch /home/{repo} {scope_file} || : > {scope_file}
[ -f {scope_file} ] || : > {scope_file}
echo "prepare: rust test scope [$(cat {scope_file})]"

cd /home/{repo}/{py}
if timeout 3600 {maturin} > /tmp/warm.log 2>&1; then
  echo "warm-up build: OK" >> /home/.warm_status
else
  echo "warm-up build: INCOMPLETE" >> /home/.warm_status
  tail -40 /tmp/warm.log || true
fi
cat /home/.warm_status

cd /home/{repo}
git reset --hard

cd /home/{repo}/{py}
pytest --version
maturin --version
python -c "import polars; print('polars', polars.__version__)"
echo "prepare: DEPS_OK"
"""

GRADED_BLOCK = """\
rc=0
{pytest} || rc=$?
if [ -s {scope_file} ]; then
  cd /home/{repo}
  {cargo} || rc=$?
fi
exit $rc""".format(
    pytest=PYTEST_CMD,
    scope_file=RUST_TEST_SCOPE_FILE,
    cargo=CARGO_TEST_CMD.format(scope=RUST_TEST_SCOPE_FILE),
    repo=ERA_REPO,
)

RUN_SH = """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}/{py}
{maturin}
{graded}
"""

TEST_RUN_SH = """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
cd /home/{repo}/{py}
{maturin}
{graded}
"""

FIX_RUN_SH = """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
cd /home/{repo}/{py}
{maturin}
{graded}
"""


class PolarsImageBase(Image):
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-{ERA_PR_MIN}_to_{ERA_PR_MAX}"

    def workdir(self) -> str:
        return f"base-{ERA_PR_MIN}_to_{ERA_PR_MAX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""\
# syntax=docker/dockerfile:1.6

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
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        build-essential ca-certificates cmake curl git pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \\
        | sh -s -- -y --profile minimal --default-toolchain none \\
    && /root/.cargo/bin/rustup --version

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class PolarsImageDefault(Image):
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
        return PolarsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        fmt = dict(
            repo=self.pr.repo,
            py=PY_SUBDIR,
            sha=self.pr.base.sha,
            shell_env=SHELL_ENV,
            maturin=MATURIN_BUILD,
            graded=GRADED_BLOCK,
            toolchain_file=TOOLCHAIN_FILE,
            toolchain_legacy=TOOLCHAIN_FILE_LEGACY,
            workflows=TOOLCHAIN_FALLBACK_GLOB,
            nightly=NIGHTLY_PATTERN,
            pin=TOOLCHAIN_PIN_FILE,
            scope_file=RUST_TEST_SCOPE_FILE,
            drop_script=DROP_REQUIREMENT_SCRIPT.rstrip("\n"),
            resolve_script=RESOLVE_AS_OF_SCRIPT.rstrip("\n"),
            scope_script=RUST_SCOPE_SCRIPT.rstrip("\n"),
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", PREPARE_SH.format(**fmt)),
            File(".", "run.sh", RUN_SH.format(**fmt)),
            File(".", "test-run.sh", TEST_RUN_SH.format(**fmt)),
            File(".", "fix-run.sh", FIX_RUN_SH.format(**fmt)),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        hardening = Image._HARDENING_BLOCK.rstrip().replace(
            "${BASE_COMMIT}", self.pr.base.sha
        )

        sections = [f"FROM {dep.image_name()}:{dep.image_tag()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(copy_commands.rstrip())
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(hardening)
        sections.append("RUN bash /home/prepare.sh")
        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_ID_PREFIX = f"{PY_SUBDIR}/"

_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<name>[^\s]+?)(?:\s+-\s+.*)?$"
)

_PROGRESS_RE = re.compile(
    r"^(?P<name>[^\s]+::[^\s]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
)

_CARGO_RE = re.compile(
    r"^test\s+(?P<name>[A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)*)\s+\.\.\.\s+"
    r"(?P<status>ok|FAILED|ignored)\s*$"
)

_CARGO_BINARY_RE = re.compile(
    r"^(?:Running|Doc-tests)\b.*?deps/(?P<crate>[A-Za-z0-9_]+)-[0-9a-f]{8,}\b"
)
_CARGO_DOCTEST_RE = re.compile(r"^Doc-tests\s+(?P<crate>[A-Za-z0-9_-]+)\s*$")

_CARGO_PREFIX = "cargo::"
_CARGO_STATUS = {"ok": "PASSED", "FAILED": "FAILED", "ignored": "SKIPPED"}

_FAIL_STATUSES = {"FAILED", "ERROR"}
_SKIP_STATUSES = {"SKIPPED", "XFAIL", "XPASS"}


def parse_log_text(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    cargo_crate = ""

    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        binary_match = _CARGO_BINARY_RE.match(line) or _CARGO_DOCTEST_RE.match(line)
        if binary_match:
            cargo_crate = binary_match.group("crate").replace("-", "_") + "::"
            continue

        cargo_match = _CARGO_RE.match(line)
        if cargo_match:
            name = _CARGO_PREFIX + cargo_crate + cargo_match.group("name")
            status = _CARGO_STATUS[cargo_match.group("status")]
        else:
            match = _SUMMARY_RE.match(line) or _PROGRESS_RE.match(line)
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


def _assert_in_era(pr: PullRequest) -> None:
    if pr.org != ERA_ORG or pr.repo != ERA_REPO:
        raise ValueError(
            f"{ERA_INTERVAL} received {pr.org}/{pr.repo}, expected {ERA_ORG}/{ERA_REPO}"
        )
    if not ERA_PR_MIN <= pr.number <= ERA_PR_MAX:
        raise ValueError(
            f"PR #{pr.number} is outside era {ERA_INTERVAL} "
            f"(#{ERA_PR_MIN}-#{ERA_PR_MAX}); route it to the era that covers it"
        )


@Instance.register(ERA_ORG, ERA_INTERVAL)
class POLARS_6841_TO_8249(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        _assert_in_era(pr)
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PolarsImageDefault(self.pr, self._config)

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
        return parse_log_text(test_log)


Instance.register(ERA_ORG, "polars_0_to_12255")(POLARS_6841_TO_8249)
