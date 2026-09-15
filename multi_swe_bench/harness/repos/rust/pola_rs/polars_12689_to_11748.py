from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

RUST_IMAGE = "rust:1.91-bookworm"

PY_PACKAGE_DIR = "py-polars"

CARGO_BUILD_JOBS = "3"

_PLUS_FILE_RE = re.compile(r"^\+\+\+ b/(?P<path>\S+)", re.M)


def python_test_paths(patch: str) -> list[str]:
    prefix = f"{PY_PACKAGE_DIR}/"
    paths = set()
    for path in _PLUS_FILE_RE.findall(patch or ""):
        if not path.startswith(prefix + "tests/") or not path.endswith(".py"):
            continue
        if not path.rsplit("/", 1)[-1].startswith("test_"):
            continue
        paths.add(path[len(prefix):])
    return sorted(paths)


CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: clean"
exit 0
"""

PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

cat > /home/build_polars.sh <<'BUILD_EOF'
#!/bin/bash
set -e
export RUSTFLAGS="-C debuginfo=0"
export CARGO_TERM_COLOR=never
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-[[JOBS]]}"
export VIRTUAL_ENV=/home/venv
export PATH="/home/venv/bin:$PATH"
cd /home/[[REPO]]
CHANNEL=$(sed -n 's/^channel *= *"\(.*\)"/\1/p' rust-toolchain.toml)
rustup toolchain list | grep -q "^$CHANNEL" || rustup toolchain install "$CHANNEL" --profile minimal
cd /home/[[REPO]]/[[PKG]]
rustc --version
maturin develop
BUILD_EOF

cat > /home/run_tests.sh <<'RUNTESTS_EOF'
#!/bin/bash
set +e
export CI=true
export TZ=UTC
export PATH="/home/venv/bin:$PATH"
cd /home/[[REPO]]/[[PKG]] || exit 0

PATHS=()
for p in [[TEST_PATHS]]; do
    [ -e "$p" ] && PATHS+=("$p")
done

if [ ${#PATHS[@]} -eq 0 ]; then
    echo "run_tests: none of this PR's test files are present yet" >&2
    exit 0
fi

/home/venv/bin/python -m pytest --no-header -rA --tb=no -p no:cacheprovider \
    -n 4 --dist loadgroup \
    --continue-on-collection-errors "${PATHS[@]}" 2>&1 \
    | sed -E "s#^(PASSED|FAILED|ERROR|XFAIL|XPASS)[[:space:]]+#\1 pytest::[[PKG]]/#; s#^(SKIPPED \[[0-9]+\])[[:space:]]+#\1 pytest::[[PKG]]/#"
exit 0
RUNTESTS_EOF

chmod +x /home/build_polars.sh /home/run_tests.sh

python3 -m venv /home/venv
/home/venv/bin/python -m pip install --upgrade pip uv

CUTOFF=$(TZ=UTC git log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd HEAD)
echo "prepare: installing python packages released before $CUTOFF"

grep -v '^[[:space:]]*--' [[PKG]]/requirements-dev.txt > /tmp/requirements-dev.filtered.txt

if ! /home/venv/bin/uv pip install --python /home/venv/bin/python \
        --exclude-newer "$CUTOFF" -r /tmp/requirements-dev.filtered.txt; then
    echo "prepare: full requirements install failed, installing one by one"
    sed -e 's/[[:space:]]#.*$//' -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
        /tmp/requirements-dev.filtered.txt | while IFS= read -r req || [ -n "$req" ]; do
        /home/venv/bin/uv pip install --python /home/venv/bin/python \
            --exclude-newer "$CUTOFF" "$req" \
            || echo "prepare: SKIPPED requirement: $req"
    done
fi

/home/venv/bin/uv pip install --python /home/venv/bin/python \
    --exclude-newer "$CUTOFF" maturin pytest pytest-xdist hypothesis numpy

bash /home/build_polars.sh

/home/venv/bin/python -c "import polars; print('prepare: polars', polars.__version__)"

cd /home/[[REPO]]
git checkout -- .
bash /home/check_git_changes.sh
"""

STAGE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
[[PATCH_STEP]]
if ! bash /home/build_polars.sh; then
    echo "Error: polars build failed" >&2
    exit 1
fi
bash /home/run_tests.sh
"""

APPLY_TEST = r"""if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
"""

APPLY_TEST_AND_FIX = r"""if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
"""


class PolarsPyTestImageBase(Image):
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
        return RUST_IMAGE

    def image_tag(self) -> str:
        return "base-12689_to_11748"

    def workdir(self) -> str:
        return "base-12689_to_11748"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

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

ENV CARGO_TERM_COLOR=never \\
    CARGO_NET_RETRY=10 \\
    RUST_BACKTRACE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential curl cmake clang pkg-config libssl-dev \\
    python3 python3-dev python3-venv python3-pip patchelf \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global http.version HTTP/1.1 && \\
    git config --global http.postBuffer 524288000 && \\
    git config --global http.lowSpeedLimit 1000 && \\
    git config --global http.lowSpeedTime 600 && \\
    for attempt in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        git clone "${{REPO_URL}}" /home/{repo} && break; \\
        echo "clone attempt $attempt failed, retrying"; \\
        sleep 20; \\
    done && \\
    test -d /home/{repo}/.git

CMD ["/bin/bash"]
"""


class PolarsPyTestImageDefault(Image):
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
        return PolarsPyTestImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        test_paths = " ".join(
            f"'{p}'" for p in python_test_paths(self.pr.test_patch)
        )
        return (
            template.replace("[[TEST_PATHS]]", test_paths)
            .replace("[[PKG]]", PY_PACKAGE_DIR)
            .replace("[[JOBS]]", CARGO_BUILD_JOBS)
            .replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
            File(".", "run.sh", self._expand(STAGE_SH.replace("[[PATCH_STEP]]", ""))),
            File(
                ".",
                "test-run.sh",
                self._expand(STAGE_SH.replace("[[PATCH_STEP]]", APPLY_TEST)),
            ),
            File(
                ".",
                "fix-run.sh",
                self._expand(STAGE_SH.replace("[[PATCH_STEP]]", APPLY_TEST_AND_FIX)),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{copy_commands}
ARG BASE_COMMIT="{sha}"

RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git status --porcelain)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("pola-rs", "polars_12689_to_11748")
class Polars12689To11748(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PolarsPyTestImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        node_id = re.compile(r"^pytest::[^\s:]+\.py(::.*)?$")
        re_pass = re.compile(r"^PASSED\s+(.+?)\s*$")
        re_fail = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_error = re.compile(r"^ERROR\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_skip = re.compile(
            r"^SKIPPED\s+\[\d+\]\s+(pytest::\S+?\.py):\d+(?::\s.*)?\s*$"
        )
        re_xfail = re.compile(r"^XFAIL\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_xpass = re.compile(r"^XPASS\s+(.+?)(?:\s+-\s.*)?\s*$")

        for raw in test_log.splitlines():
            line = raw.strip()
            if not line:
                continue
            for rx, bucket in (
                (re_pass, passed_tests),
                (re_fail, failed_tests),
                (re_error, failed_tests),
                (re_skip, skipped_tests),
                (re_xfail, skipped_tests),
                (re_xpass, passed_tests),
            ):
                m = rx.match(line)
                if m:
                    name = m.group(1)
                    if node_id.match(name):
                        bucket.add(name)
                    break

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
