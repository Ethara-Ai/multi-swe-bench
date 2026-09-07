"""langchain-ai/langchain harness config — PRs #260 to #19331.

Spans two repo layouts with one shared base image:

  * flat era   (#260 Dec-2022, #7089 Jul-2023): package + tests/ at the repo root
  * monorepo   (#17386, #18960, #19331 2024):   libs/<pkg>/tests/unit_tests

Both eras build with poetry on python:3.11-slim, so a single base serves all
five PRs; prepare.sh moves the checkout to each PR's own base sha. Test
discovery is layout-agnostic (find every tests/unit_tests dir), which is what
lets one config cover both eras.

Artifact split:
  base Dockerfile -> toolchain, proxy/CA, clone, reset, checkout ${BASE_COMMIT},
                     hardening block + 4 asserts, submodule scrub
  PR Dockerfile   -> FROM base, 7 COPY lines, RUN bash /home/prepare.sh
  prepare.sh      -> deps, cd repo, reset, checkout <sha>, check_git_changes asserts
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LangchainEraImageBase(Image):
    """Shared base: toolchain + clone. One image for every PR in the range.

    dependency() returns a STRING, which is what makes DockerfileEnhancer own
    this file (image.py:314 returns raw untouched when dependency() is an
    Image). The enhancer rewrites the `git clone` line below into
    clone -> WORKDIR -> git reset --hard -> git checkout ${BASE_COMMIT} ->
    _HARDENING_BLOCK (all 4 asserts) -> submodule scrub, so none of that is
    written by hand here.
    """

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
        # base-<name> per the artifact contract. QC P1 asks for base-pr-<N>,
        # but a per-PR base contradicts "one base image for every PR"; the
        # shared-base form is the deliberate choice, as in the reference's
        # base-jest. Named for the build tool both eras share.
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

        # Emitting the syntax directive ourselves makes DockerfileEnhancer
        # return this file verbatim (image.py:316). That is deliberate: the
        # enhancer's _standardize_repo_fetch would otherwise rewrite the clone
        # into clone+checkout+hardening, and the hardening must live in the PR
        # layer, not here. Everything the enhancer would have contributed
        # (ARGs, proxy, CA farm, OCI labels) is therefore written out below.
        # Content stops at the clone, then CMD.
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
    """PR layer: FROM base, exactly 7 COPY lines, RUN bash /home/prepare.sh.

    dependency() returns an Image, so DockerfileEnhancer returns this file
    verbatim (image.py:314). Nothing is injected — no ARG, no ENV, no WORKDIR,
    no git, no CMD — which is why the hardening lives wholly in the base.

    Only 7 files are copied. strip_binaries.sh and run_tests.sh are *generated*
    by prepare.sh at build time rather than copied, to keep the COPY list at
    the 7 the artifact contract allows.
    """

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

# The base image is hardened: its remote is removed and history pruned to a
# single commit's ancestry, so this PR's sha is usually absent. Re-attach the
# remote and fetch the exact object before checkout -- that is what lets one
# base image serve every PR in the range.
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
# Layout-agnostic: discovers every tests/unit_tests dir, so the same script
# serves the flat era (tests/unit_tests at the repo root) and the monorepo era
# (libs/<pkg>/tests/unit_tests) without branching on repo shape.
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
    # -o addopts= drops per-package inifile addopts (coverage/snapshot/socket),
    # which otherwise error out when their plugin failed to install.
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
        # pytest<8 and pytest-asyncio<0.24 are the last pair that work together
        # here; newer pytest-asyncio needs pytest>=8.2, which drags in pytest 9
        # whose deprecations are errors under this era's filterwarnings.
        if ! poetry run python -c "import pytest_socket, pytest_asyncio, pytest_mock" 2>/dev/null; then
            poetry run pip install "pytest<8" "pytest-asyncio<0.24" \
                pytest-socket pytest-mock pytest-cov pytest-dotenv freezegun \
                responses syrupy 2>/dev/null || true
        fi
        poetry run pip install -e . 2>/dev/null || true
    )
done < <(find /home/[[REPO]] -maxdepth 5 -type d -path '*/tests/unit_tests' -not -path '*/.venv/*' 2>/dev/null | sort)

# poetry rewrites poetry.lock during install. Restoring tracked files returns
# the tree to exactly the base sha -- the state every `git apply` in the three
# run scripts expects -- while leaving the gitignored .venv dirs in place.
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

        # The history strip / hardening lives here, not in the base and not in
        # prepare.sh. It runs AFTER prepare.sh, because prepare.sh is what moves
        # the checkout to this PR's sha -- stripping before that would delete the
        # very objects prepare.sh needs to fetch.
        #
        # The sha is inlined rather than taken from an ARG so this layer needs no
        # ARG/ENV of its own; the base's ${BASE_COMMIT} is not this PR's commit.
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
    """Harness instance for langchain-ai/langchain — PRs #260 to #19331."""

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
        """Parse pytest -rA output, ids prefixed with their package.

        run_tests.sh tags every status line with "[<pkg>] " so that same-named
        test files in different libs/* packages stay distinct ids.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Parametrized ids can contain spaces (test_foo[a b c]), so capture to
        # end of line minus the optional " - <reason>" trailer rather than to
        # the next whitespace.
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

        # TestResult.__post_init__ rejects overlapping sets. Failure wins over a
        # later pass, then skip.
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
