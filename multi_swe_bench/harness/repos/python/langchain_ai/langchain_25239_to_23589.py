"""langchain-ai/langchain harness config — the poetry / libs-community era.

Covers PRs #23589, #25003, #25139, #25239 (Jun–Sep 2024). Every one of them
touches `libs/community`, which in that window is a **poetry** project
(`[tool.poetry]`, `poetry.lock`, `python = ">=3.8.1,<4.0"`), so a single
python:3.11-slim + poetry base serves all four.

This is the sibling of langchain_34235_to_32578.py, which owns the 2025 PRs.
The split is by BUILD TOOL, not by PR number: poetry here, uv there. Both files
are new; neither modifies any pre-existing langchain config.

Artifact split (rule 9 — base stops at the clone):
  base Dockerfile -> toolchain, proxy/CA, ENV, LABELs, clone, CMD. Nothing else.
  PR Dockerfile   -> FROM base, 7 COPY lines, RUN bash /home/prepare.sh,
                     then the FULL git strip / hardening block with 4 asserts.
  prepare.sh      -> deps, cd repo, fetch + checkout <sha>, poetry install.

Test selection: only the test files this PR's own test patch touches. The four
PRs all put their tests under `libs/community/tests/integration_tests/`, a tree
of ~700 files that mostly needs live API keys and paid credentials; running it
whole would drown the report in errors that have nothing to do with the PR.
The per-PR extra pip installs below are exactly what those chosen files import.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Python packages each PR's chosen test files import but that
# `poetry install --with test` does not bring in, because they live in
# libs/community's OPTIONAL extras rather than its test group.
#
#   25003  sqlite-vec       tests/integration_tests/vectorstores/test_sqlitevec.py
#          + vcrpy           imports langchain_community.vectorstores.SQLiteVec,
#                           whose module does `import sqlite_vec`. The PR's own
#                           test patch adds this pin to extended_testing_deps.txt.
#                           vcrpy is separate and non-obvious: the moment that
#                           test file exists, pytest loads
#                           tests/integration_tests/vectorstores/conftest.py,
#                           which does `from vcr.request import Request` at module
#                           scope. A conftest ImportError is FATAL -- pytest exits
#                           before collection and --continue-on-collection-errors
#                           cannot save it -- so the whole stage returned (0,0,0)
#                           until vcrpy was installed. Measured 2026-09-08.
#   25139  qianfan          QianfanLLMEndpoint.validate_environment builds a
#                           qianfan.Completion client at construction time.
#   25239  websocket-client SparkLLM.validate_environment builds a
#                           _SparkLLMClient, whose __init__ does `import websocket`.
#   23589  (none)           ChatBaichuan needs only requests, already a main dep.
_EXTRA_PIP: dict[int, str] = {
    23589: "",
    25003: "sqlite-vec vcrpy",
    25139: "qianfan",
    25239: "websocket-client",
}

# Every PR in this era lives in the same poetry project.
_PKG = "libs/community"


def _test_paths(test_patch: str) -> list[str]:
    """Repo-relative test files this PR's test patch writes to.

    Reads the `+++ b/` lines, never the `diff --git` header: the header still
    names a file the patch deletes, which would put a nonexistent path on the
    pytest command line.
    """
    paths = set()
    for m in re.finditer(r"^\+\+\+ b/(.+?)\s*$", test_patch, re.M):
        p = m.group(1)
        if p.endswith(".py") and "/tests/" in p:
            paths.add(p)
    return sorted(paths)


def _pkg_relative(paths: list[str]) -> list[str]:
    """Same paths, relative to _PKG, because pytest runs from inside it."""
    prefix = _PKG + "/"
    return [p[len(prefix) :] for p in paths if p.startswith(prefix)]


class LangchainPoetryEraImageBase(Image):
    """Shared base for the four community PRs: toolchain + clone, then CMD.

    dependency() returns a STRING, so DockerfileEnhancer would normally own this
    file. Emitting the `# syntax=` directive ourselves makes image.py return it
    verbatim instead, which is what keeps the clone unpinned and the hardening
    out of the base. Everything the enhancer would have contributed (ARGs,
    proxy, CA farm, OCI labels) is therefore written out by hand below.
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
        # Distinct from langchain_19331_to_260.py's "base-poetry" and from the
        # uv era's "base-uv-py312". All three configs share one image NAME
        # (org/repo are identical), so the tag is the only thing keeping their
        # contents apart.
        return "base-community-poetry"

    def workdir(self) -> str:
        return "base-community-poetry"

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

RUN pip install --no-cache-dir "poetry==1.8.5"

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class LangchainPoetryEraImageDefault(Image):
    """PR layer: FROM base, exactly 7 COPY lines, prepare.sh, then hardening.

    dependency() returns an Image, so DockerfileEnhancer returns this file
    verbatim and injects no ARG/ENV/WORKDIR/CMD. That is also why the sha is
    inlined below rather than read from ${BASE_COMMIT}: an Image-dependency
    layer receives no build args at all.
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
        return LangchainPoetryEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        extra = _EXTRA_PIP.get(self.pr.number, "")
        rel_paths = _pkg_relative(_test_paths(self.pr.test_patch))

        if extra:
            extra_cmd = f'poetry run pip install --no-cache-dir {extra} || true'
        else:
            extra_cmd = 'true  # no extra runtime package needed for this PR'

        # One quoted path per line, fed to the shell as a here-doc-free list.
        test_paths_sh = " ".join(f'"{p}"' for p in rel_paths)

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

# The base image keeps full history and no pin (rule 9), so the sha is already
# present. The remote re-attach and fetch are kept as a cheap safety net for a
# base commit that sits on a branch the default clone did not bring down.
git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git rev-parse --verify --quiet "[[SHA]]^{commit}" >/dev/null 2>&1 \
    || git fetch --depth=1 origin [[SHA]] 2>/dev/null \
    || git fetch origin 2>/dev/null || true
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
# Runs only the test files this PR's test patch touches. A path that does not
# exist yet is dropped rather than passed to pytest, because pytest treats a
# missing argument as a usage error and prints no result lines at all -- which
# is exactly what the baseline `run.sh` stage hits for a brand-new test file.
set +e
cd /home/[[REPO]]/[[PKG]] || exit 0

PATHS=()
for p in [[TEST_PATHS]]; do
    [ -e "$p" ] && PATHS+=("$p")
done

if [ ${#PATHS[@]} -eq 0 ]; then
    echo "run_tests: none of this PR's test files are present yet" >&2
    exit 0
fi

# -o addopts= drops the repo's own "--strict-markers --strict-config -vv
# --snapshot-warn-unused", which needs the syrupy plugin and turns any config
# warning into an error. asyncio_mode=auto lives in ini_options, not addopts,
# so the async tests still collect.
poetry run pytest -o addopts= --no-header -rA --tb=no -p no:cacheprovider \
    --continue-on-collection-errors "${PATHS[@]}" 2>&1 \
    | sed -E "s#^(PASSED|FAILED|ERROR|XFAIL|XPASS)[[:space:]]+#\1 pytest::[[PKG]]/#; s#^(SKIPPED \[[0-9]+\])[[:space:]]+#\1 pytest::[[PKG]]/#"
exit 0
RUNTESTS_EOF

chmod +x /home/strip_binaries.sh /home/run_tests.sh

cd /home/[[REPO]]/[[PKG]]
# libs/community's test group carries langchain-core, langchain and
# langchain-standard-tests as develop path deps on ../core, ../langchain and
# ../standard-tests, so this one install wires up the whole monorepo slice.
poetry install --with test --no-interaction || {
    poetry lock --no-update || poetry lock
    poetry install --with test --no-interaction
}
[[EXTRA]]

# poetry rewrites poetry.lock during install. Restoring tracked files returns
# the tree to exactly the base sha -- the state every `git apply` in the three
# run scripts expects -- while leaving the gitignored .venv in place.
cd /home/[[REPO]]
git checkout -- .
bash /home/check_git_changes.sh
""".replace("[[TEST_PATHS]]", test_paths_sh)
                .replace("[[EXTRA]]", extra_cmd)
                .replace("[[PKG]]", _PKG)
                .replace("[[REPO]]", repo)
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

        # Rule 9: the strip lives here, never in the base and never in
        # prepare.sh. It runs AFTER prepare.sh because prepare.sh needs the
        # remote and the network for `poetry install`.
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


@Instance.register("langchain-ai", "langchain_25239_to_23589")
class Langchain25239To23589(Instance):
    """Harness instance for langchain-ai/langchain — the poetry community era."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LangchainPoetryEraImageDefault(self.pr, self._config)

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
        """Parse pytest -rA output. Ids are `pytest::<repo path>::<name>`.

        The tool name comes FIRST on purpose: an id that starts with a path the
        fix patch creates makes report.py flag the instance as fix-authored.
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
