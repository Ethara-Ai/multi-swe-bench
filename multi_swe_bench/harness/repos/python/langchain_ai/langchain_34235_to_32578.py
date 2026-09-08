"""langchain-ai/langchain harness config — the uv era.

Covers PRs #32578, #32622, #32727, #33480, #33943, #34235 (Aug–Dec 2025). By
this point every `libs/*` directory is an independent **uv** project: its own
`pyproject.toml` with `[project]` + `[dependency-groups]`, its own `uv.lock`,
and `[tool.uv.sources]` path entries pointing at its siblings. There is no
`[tool.uv.workspace]` at the repo root, so `cd libs/<pkg> && uv sync` is
self-contained — which is what lets one base image serve six PRs across four
different packages:

    32578  libs/core                 0.3.76, pdm.backend, requires-python >=3.9
    32622  libs/core                 0.3.x,  pdm.backend
    32727  libs/partners/deepseek    1.0.0,  hatchling, >=3.10
    33480  libs/partners/perplexity  1.0.0,  hatchling, >=3.10
    33943  libs/partners/huggingface 1.1.0,  hatchling, >=3.10
    34235  libs/core                 1.1.1,  hatchling, >=3.10

python:3.12 satisfies every one of those requires-python floors, and the build
backend difference is uv's problem, not the image's — the base carries only the
toolchain, so nothing era-specific has to be baked in.

This is the sibling of langchain_25239_to_23589.py, which owns the 2024 poetry
PRs. Both files are new; neither modifies any pre-existing langchain config.

Artifact split (rule 9 — base stops at the clone):
  base Dockerfile -> toolchain, proxy/CA, ENV, LABELs, clone, CMD. Nothing else.
  PR Dockerfile   -> FROM base, 7 COPY lines, RUN bash /home/prepare.sh,
                     then the FULL git strip / hardening block with 4 asserts.
  prepare.sh      -> cd repo, fetch + checkout <sha>, uv sync the one package.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Packages a PR's test needs that `uv sync --group test` does not install at the
# BASE commit, because the PR's own fix patch is what adds them.
#
#   33943  The fix adds `langchain` to the test group and a
#          `langchain = { path = "../../langchain_v1", editable = true }` uv
#          source, because the new test does
#          `from langchain.chat_models.base import init_chat_model`. Installing
#          it here means the pre-fix run fails on the real defect rather than on
#          a bare ImportError. `uv sync` is never re-run by the test scripts, so
#          this stays put for all three stages.
_EXTRA_UV: dict[int, str] = {
    33943: "uv pip install --python .venv/bin/python -e ../../langchain_v1 || true",
}


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


def _package_dir(paths: list[str]) -> str:
    """The uv project a set of test paths belongs to.

    `libs/core/tests/unit_tests/test_tools.py`               -> libs/core
    `libs/partners/deepseek/tests/unit_tests/test_x.py`      -> libs/partners/deepseek

    Every PR in this era touches exactly one package; if that ever stops being
    true the shortest prefix wins, which is the package whose venv holds the
    others as editable path deps anyway.
    """
    pkgs = sorted({p.split("/tests/", 1)[0] for p in paths})
    return pkgs[0] if pkgs else "libs/core"


class LangchainUvEraImageBase(Image):
    """Shared base for the six uv PRs: toolchain + clone, then CMD.

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
        return "python:3.12-slim"

    def image_tag(self) -> str:
        # Distinct from the poetry era's "base-community-poetry" and from
        # langchain_19331_to_260.py's "base-poetry". All three configs share one
        # image NAME (org/repo are identical), so the tag is the only thing
        # keeping their contents apart.
        return "base-uv-py312"

    def workdir(self) -> str:
        return "base-uv-py312"

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
    UV_HTTP_TIMEOUT=180 \\
    UV_LINK_MODE=copy \\
    UV_NO_PROGRESS=1

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

# uv is deliberately unpinned. Every lock in this era was written by a uv from
# late 2025, and uv reads older lock revisions forward but not newer ones, so a
# pin here would be the thing most likely to break, not the thing that saves us.
RUN pip install --no-cache-dir uv

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class LangchainUvEraImageDefault(Image):
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
        return LangchainUvEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        abs_paths = _test_paths(self.pr.test_patch)
        pkg = _package_dir(abs_paths)
        rel_paths = [p[len(pkg) + 1 :] for p in abs_paths if p.startswith(pkg + "/")]

        extra_cmd = _EXTRA_UV.get(
            self.pr.number, "true  # no extra package needed for this PR"
        )
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

# .venv/bin/python instead of `uv run`: uv run re-syncs from the lock, and the
# fix patch for #33943 edits both pyproject.toml and uv.lock, so a re-sync would
# make the fix stage install a different dependency set from the test stage.
# Pinning the interpreter keeps all three stages on one environment.
#
# -o addopts= drops the repo's own "--snapshot-warn-unused --strict-markers
# --strict-config --durations=5", which needs the syrupy plugin and turns any
# config warning into an error. asyncio_mode=auto lives in ini_options, not
# addopts, so the async tests still collect.
.venv/bin/python -m pytest -o addopts= --no-header -rA --tb=no -p no:cacheprovider \
    --continue-on-collection-errors "${PATHS[@]}" 2>&1 \
    | sed -E "s#^(PASSED|FAILED|ERROR|XFAIL|XPASS)[[:space:]]+#\1 pytest::[[PKG]]/#; s#^(SKIPPED \[[0-9]+\])[[:space:]]+#\1 pytest::[[PKG]]/#"
exit 0
RUNTESTS_EOF

chmod +x /home/strip_binaries.sh /home/run_tests.sh

cd /home/[[REPO]]/[[PKG]]
# No [tool.uv.workspace] at the repo root, so this package is a standalone uv
# project; its [tool.uv.sources] pull the sibling libs in as editable path deps.
uv venv --python 3.12 .venv
uv sync --group test --frozen || uv sync --group test
[[EXTRA]]

# uv rewrites uv.lock when the lock and the manifest disagree. Restoring tracked
# files returns the tree to exactly the base sha -- the state every `git apply`
# in the three run scripts expects -- while leaving the gitignored .venv alone.
cd /home/[[REPO]]
git checkout -- .
bash /home/check_git_changes.sh
""".replace("[[TEST_PATHS]]", test_paths_sh)
                .replace("[[EXTRA]]", extra_cmd)
                .replace("[[PKG]]", pkg)
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
        # remote and the network for `uv sync`.
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


@Instance.register("langchain-ai", "langchain_34235_to_32578")
class Langchain34235To32578(Instance):
    """Harness instance for langchain-ai/langchain — the uv era."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LangchainUvEraImageDefault(self.pr, self._config)

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
