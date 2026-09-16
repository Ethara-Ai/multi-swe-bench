import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LangchainEraImageBase(Image):

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
        return "python:3.11-slim"

    def image_tag(self) -> str:
        return "base-poetry"

    def workdir(self) -> str:
        return "base-poetry"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_INPUT=1 \\
    POETRY_VIRTUALENVS_IN_PROJECT=true \\
    POETRY_NO_INTERACTION=1

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    curl \\
    ca-certificates \\
    pkg-config \\
    libffi-dev \\
    libssl-dev \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry<1.5"

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class LangchainEraImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return LangchainEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

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
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh

git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git fetch --depth=1 origin [[SHA]] 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout -f [[SHA]]
bash /home/check_git_changes.sh

cat > /home/strip_binaries.sh <<'STRIP_EOF'
#!/bin/bash
awk '
BEGIN { skip = 0 }
/^diff --git / {
  skip = 0
  if ($0 ~ /\.(ico|icns|png|jpe?g|gif|bmp|webp|woff2?|ttf|eot|otf|pdf|zip|tar|tgz|tbz2?|txz|bz2|xz|gz|class|jar|war|ear|enc|gpg|asc|p7s|der|crt|key|pem|sig|odt|ods|odp|docx|xlsx|pptx|msg|vsdx|db|sqlite3?|bin|dat|so|dll|dylib|a|o|obj|exe|wasm|mp[34]|wav|ogg|flac|webm|mov|avi|mkv|ipynb|faiss|pkl|npy|npz|joblib|model|onnx|pt|pth|safetensors|h5|parquet|arrow|feather|index)( |$)/) skip = 1
}
{ if (!skip) print }
' "$1"
STRIP_EOF

cat > /home/run_tests.sh <<'RUNTESTS_EOF'
#!/bin/bash
set +e
cd /home/[[REPO]]
ran_any=0
while IFS= read -r tdir; do
    pkg=$(dirname "$(dirname "$tdir")")
    [ -f "$pkg/pyproject.toml" ] || continue
    rel=$(realpath --relative-to=/home/[[REPO]] "$pkg")
    echo "================= PYTEST PKG: $rel ================="
    ran_any=1
    cd "$pkg" || continue
    SOCKET_FLAGS=""
    if poetry run python -c "import pytest_socket" >/dev/null 2>&1; then
        SOCKET_FLAGS="--disable-socket --allow-unix-socket"
    fi
    poetry run pytest -o addopts= --no-header -rA --tb=no -p no:cacheprovider \
        --continue-on-collection-errors $SOCKET_FLAGS tests/unit_tests/ 2>&1 \
        | sed -E "s#^(PASSED|FAILED|ERROR|XFAIL|XPASS)[[:space:]]+#\1 [$rel] #; s#^(SKIPPED \[[0-9]+\])[[:space:]]+#\1 [$rel] #"
    cd /home/[[REPO]]
done < <(find /home/[[REPO]] -maxdepth 5 -type d -path '*/tests/unit_tests' -not -path '*/.venv/*' 2>/dev/null | sort)
[ "$ran_any" = "0" ] && echo "run_tests: no tests/unit_tests directory found" >&2
exit 0
RUNTESTS_EOF

chmod +x /home/strip_binaries.sh /home/run_tests.sh

while IFS= read -r tdir; do
    pkg=$(dirname "$(dirname "$tdir")")
    [ -f "$pkg/pyproject.toml" ] || continue
    (
        cd "$pkg"
        poetry install --with test --no-interaction || true
        poetry run pip install -e . --no-deps 2>/dev/null || true
        if ! poetry run python -c "import pytest_socket, pytest_asyncio, pytest_mock" 2>/dev/null; then
            poetry run pip install "pytest<8" "pytest-asyncio<0.24" \
                pytest-socket pytest-mock pytest-cov pytest-dotenv freezegun \
                responses syrupy 2>/dev/null || true
        fi
        poetry run pip install -e . 2>/dev/null || true
    )
done < <(find /home/[[REPO]] -maxdepth 5 -type d -path '*/tests/unit_tests' -not -path '*/.venv/*' 2>/dev/null | sort)

cd /home/[[REPO]]
git checkout -- .
bash /home/check_git_changes.sh
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO]]
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
if ! git -C /home/[[REPO]] apply --whitespace=nowarn /tmp/test.filtered.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO]]
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
bash /home/strip_binaries.sh /home/fix.patch  > /tmp/fix.filtered.patch
if ! git -C /home/[[REPO]] apply --whitespace=nowarn /tmp/test.filtered.patch /tmp/fix.filtered.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
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


@Instance.register("langchain-ai", "langchain_19331_to_260")
class Langchain19331To260(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LangchainEraImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"^PASSED\s+(.+?)\s*$")
        re_fail = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_error = re.compile(r"^ERROR\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_skip = re.compile(
            r"^SKIPPED\s+\[\d+\]\s+((?:\[\S+?\]\s+)?\S+?:\d+)(?::\s.*)?\s*$"
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
                    bucket.add(m.group(1))
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
