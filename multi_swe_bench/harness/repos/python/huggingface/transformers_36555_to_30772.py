"""huggingface/transformers harness config — PRs #30772 to #36555.

Registered under the number_interval key ``transformers_36555_to_30772``.
``Instance.register`` writes straight into ``_registry[f"{org}/{repo}"]`` with no
collision check, so registering on the plain ``huggingface/transformers`` key
would silently replace the existing generic config. Note also that the existing
``transformers_44040_to_3323`` interval *contains* this range: routing is by
exact ``number_interval`` match (instance.py:42), so the explicit key below is
what keeps these ten PRs on this file.

Architecture: SHARED base (A-series of the Dockerfile QC).
  base  -> toolchain + full-history clone, pinned to nothing, then CMD.
           `# syntax` on line 1 is the DockerfileEnhancer opt-out (image.py:317);
           without it the enhancer rewrites the clone into
           `git checkout ${BASE_COMMIT}` + scrub, which would pin this one shared
           image to whichever PR the build `set` happened to keep and break every
           other PR in the shard.
           ARG BASE_COMMIT is DECLARED (silences BuildKit's unused-arg warning,
           since the harness passes it regardless) but never REFERENCED — A3.
  PR    -> 7 COPYs, prepare.sh, then the checkout+prune the shared base cannot do.

Layer-leak disclosure (A6): the base clones full history and the PR layer prunes
in a later RUN, so the pre-prune history still exists in the base layer's blob.
This is a DISTRIBUTION concern only — the base tag must never be published; PR
images are what ship. Containers run unprivileged with no socket or volumes
(utils/docker_util.py:314), so an agent inside cannot reach lower layers.

Test scoping is essential here: transformers' full suite is far too large to run,
so each stage runs only the test files the test patch touches.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Drop diff sections for binary files.

    Test patches in this repo carry hunks for images/audio fixtures that lack the
    full index line `git apply` needs for binary blobs, which aborts the whole
    apply and leaves the tree unpatched. These files never affect pytest
    outcomes, so dropping their sections turns the apply back into a no-op for
    them.
    """
    out, skip = [], False
    for line in patch.splitlines(True):
        if line.startswith("diff --git "):
            skip = bool(
                re.search(
                    r"\.(png|jpe?g|gif|bmp|webp|ico|pdf|zip|tar|gz|bz2|xz|wav|mp3|flac|"
                    r"ogg|mp4|webm|npy|npz|pkl|bin|pt|pth|safetensors|h5|parquet|arrow)( |$)",
                    line,
                )
            )
        if not skip:
            out.append(line)
    return "".join(out)


# Shared verbatim by run.sh / test-run.sh / fix-run.sh, so the only difference
# between graded stages is which patch was applied first (QC P7).
#
# Only the test files the test patch touches are run. transformers' full suite is
# tens of thousands of tests and assumes GPUs; running it would time out and
# drown the gating signal in unrelated failures.
# PRs whose graded tests are all @slow-gated. transformers skips @slow unless
# RUN_SLOW is set, so without it every stage returns an identical result and the
# instance is ungradeable. #31499 ("Add FA2 and sdpa support for SigLIP") is the
# case: its sdpa tests are additionally gated on the model class declaring
# _supports_sdpa, which the fix patch is what flips -- so they go SKIP -> PASS
# (s2p) purely because of the fix. Enabled per PR, not globally: un-gating @slow
# everywhere would pull real model weights from the hub for the other instances.
_SLOW_REQUIRED = {31499}

_TEST_BODY = r"""
set +e
TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)(test_[^/]*\.py|[^/]*_test\.py)$' \
    | sort -u)
set -e

TEST_TARGETS=""
for f in $TEST_FILES; do
    [ -f "$f" ] && TEST_TARGETS="$TEST_TARGETS $f"
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    exit 0
fi

echo "running pytest on:$TEST_TARGETS"
python -m pytest -v -rA --no-header --tb=short -p no:cacheprovider -p no:rich \
    -o log_cli=false --continue-on-collection-errors $TEST_TARGETS 2>&1
"""


class TransformersEraImageBase(Image):
    """python:3.10-slim + toolchain, then a full-history clone. Nothing after it."""

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
        return "python:3.10-slim"

    def image_tag(self) -> str:
        return "base-36555_to_30772"

    def workdir(self) -> str:
        return "base-36555_to_30772"

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
    PYTHONDONTWRITEBYTECODE=1 \\
    TRANSFORMERS_IS_CI=1 \\
    HF_HUB_DISABLE_TELEMETRY=1 \\
    TOKENIZERS_PARALLELISM=false

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
    ca-certificates \\
    curl \\
    build-essential \\
    cmake \\
    pkg-config \\
    ffmpeg \\
    libsndfile1 \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class TransformersEraImageDefault(Image):
    """PR layer: FROM base, exactly 7 COPYs, prepare.sh, then checkout+prune."""

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
        return TransformersEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        slow_env = "\nexport RUN_SLOW=1" if self.pr.number in _SLOW_REQUIRED else ""
        org = self.pr.org
        sha = self.pr.base.sha

        prepare = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

# --detach, not a branch: the prune below asserts HEAD == BASE_SHA rather than
# re-establishing it, and then deletes every ref. An attached HEAD would either
# lose its branch out from under it or keep extra history reachable.
git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git fetch --no-tags --depth=1 origin [[SHA]] 2>/dev/null || git fetch --no-tags origin 2>/dev/null || true
git checkout --detach [[SHA]]
test "$(git rev-parse HEAD)" = "$(git rev-parse [[SHA]])"
bash /home/check_git_changes.sh

# ---------------------------------------------------------------------------
# Dependencies, pinned to what THIS commit declares. The pins are read out of
# the checkout instead of hand-mapped, so each PR in this interval gets its own
# era (the span here is transformers 4.42 -> 4.56). transformers reaches into
# huggingface_hub private modules (utils._deprecation, file_download.http_get),
# so hub is pinned to the minor series its authors developed against rather
# than the table's loose <1.0 upper bound. pip writes egg-info into the tree,
# so NO clean-tree assert runs after this point.
# ---------------------------------------------------------------------------
python - <<'PYEOF'
import pathlib, re

deps = {k.lower(): v for k, v in re.findall(
    r'"([^"]+)":\s*"([^"]+)"',
    pathlib.Path("src/transformers/dependency_versions_table.py").read_text())}
minor = int(re.search(
    r'__version__ = "\d+\.(\d+)',
    pathlib.Path("src/transformers/__init__.py").read_text()).group(1))

def series(name):
    # `huggingface-hub>=0.23.2,<1.0` -> `huggingface-hub==0.23.*`
    spec = deps.get(name)
    if not spec:
        return None
    m = re.search(r">=\s*(\d+)\.(\d+)", spec)
    return f"{name}=={m.group(1)}.{m.group(2)}.*" if m else spec

pins = [series("huggingface-hub"), series("safetensors"), series("accelerate")]
pins += [deps.get(k) for k in ("tokenizers", "pyyaml", "regex", "requests",
                               "tqdm", "filelock", "packaging", "protobuf",
                               "sentencepiece", "pillow")]
pins.append("numpy<2" if minor <= 50 else "numpy<3")
pathlib.Path("/home/era_pins.txt").write_text(
    "\n".join(p for p in pins if p) + "\n")

torch, vision, datasets = (("2.4.1", "0.19.1", "datasets<3") if minor <= 46 else
                           ("2.6.0", "0.21.0", "datasets<4") if minor <= 50 else
                           ("2.8.0", "0.23.0", "datasets<5"))
pathlib.Path("/home/torch_pin.txt").write_text(f"{torch} {vision} {datasets}\n")
PYEOF

read TORCH_V VISION_V DATASETS_SPEC < /home/torch_pin.txt
echo "=== era pins (torch==$TORCH_V torchvision==$VISION_V $DATASETS_SPEC) ==="
cat /home/era_pins.txt

# torch FIRST, and only from the CPU index. accelerate declares a torch
# dependency, so installing it earlier makes pip resolve torch off the default
# index -- that is the CUDA build, ~554 MB plus ~1.6 GB of nvidia_* wheels that
# nothing in a CPU container can use.
pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    "torch==$TORCH_V" "torchvision==$VISION_V"

pip install --no-cache-dir -r /home/era_pins.txt
pip install --no-cache-dir "$DATASETS_SPEC"
pip install --no-cache-dir [[PYTEST_PIN]] pytest-xdist timeout-decorator psutil parameterized
pip install --no-cache-dir boto3 importlib_metadata sacremoses
pip install --no-deps --no-cache-dir evaluate
pip install --no-deps --no-cache-dir -e .

# ---------------------------------------------------------------------------
# Hard gate (QC P14). NOT tolerant, and it asserts the real entry points plus
# the test-only dependencies -- a bare `import transformers` passes while every
# test errors on a missing parameterized/datasets. Importing transformers also
# runs the repo's own dependency_versions_check against the table above, so
# this line doubles as proof the pins really are the declared ones. That check
# is deliberately left enabled rather than patched out.
# ---------------------------------------------------------------------------
python -c "import transformers, torch, pytest, parameterized, datasets, PIL; print('DEPS_OK', transformers.__version__, torch.__version__)"
"""

        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
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
                prepare.replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha)
                .replace("[[PYTEST_PIN]]", "'pytest<8.0'")
                
            ),
            File(
                ".",
                "run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true[[SLOW_ENV]]

cd /home/[[REPO]]
""" + _TEST_BODY).replace("[[REPO]]", repo).replace("[[SLOW_ENV]]", slow_env),
            ),
            File(
                ".",
                "test-run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true[[SLOW_ENV]]

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi
""" + _TEST_BODY).replace("[[REPO]]", repo).replace("[[SLOW_ENV]]", slow_env),
            ),
            File(
                ".",
                "fix-run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true[[SLOW_ENV]]

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "=== PATCH FAILURE: test.patch + fix.patch did not apply ==="
    exit 1
fi
""" + _TEST_BODY).replace("[[REPO]]", repo).replace("[[SLOW_ENV]]", slow_env),
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

        # Prune runs AFTER prepare.sh: prepare.sh fetches and checks out this PR's
        # sha, so pruning first would delete the objects it needs. The opening
        # commit-scoped `checkout --detach` is a same-tree no-op that preserves
        # prepare.sh's tracked-file edits while guaranteeing HEAD is detached
        # before every ref is deleted (QC P12). No reset/clean/path-scoped
        # checkout appears here -- those would discard that work.
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


@Instance.register("huggingface", "transformers_36555_to_30772")
class Transformers36555To30772(Instance):
    """Harness instance for huggingface/transformers — PRs #30772 to #36555."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TransformersEraImageDefault(self.pr, self._config)

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
        """Parse pytest output into file-qualified ids.

        Both formats are matched because the run scripts pass -v AND -rA:
          -v   "tests/x.py::test_y PASSED"   streamed as each test finishes
          -rA  "PASSED tests/x.py::test_y"   only printed if the run completes
        The inline form is the load-bearing one -- a run cut short still yields
        results. Matching only the summary form silently reports zero.

        Ids keep the FILE PATH. Bare names after "::" collapse same-named tests
        from different model dirs into one id that then lands in two status sets,
        which TestResult.__post_init__ rejects outright.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        inline_re = re.compile(
            r"^(?P<id>\S+\.py::\S+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
            re.MULTILINE,
        )
        summary_re = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
            r"(?P<id>\S+\.py::\S+?)(?:\s+-\s.*)?\s*$",
            re.MULTILINE,
        )
        for m in list(inline_re.finditer(log)) + list(summary_re.finditer(log)):
            status, tid = m.group("status"), m.group("id")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(tid)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        # A module that fails to import prints "ERROR tests/x.py" with no "::".
        # Record the FILE so the stage still carries that signal instead of
        # going silently empty.
        for m in re.finditer(r"^ERROR\s+(\S+\.py)\s*(?:-.*)?$", log, re.MULTILINE):
            failed_tests.add(m.group(1))

        # TestResult.__post_init__ enforces disjoint sets. Failure wins over a
        # retry that later passed, then skip.
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
