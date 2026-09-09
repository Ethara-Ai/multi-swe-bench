"""keras-team/keras -- era 22293-to-18649.

Sibling of keras.py, which serves era 20745-to-19955 and is left untouched. A
separate file because the two eras cannot share a base image: the base scrubs git
history down to its anchor commit, so a PR builds only if its base commit is an
ANCESTOR of that anchor, and six PRs here post-date keras.py's 2025-01-08 anchor.

jax pins come from the tree's own requirements.txt where it pins one (21139, 21953)
and otherwise from the newest jax released on or before that PR's base-commit date.
Backends come from a reachability pass over each PR's target *_test.py files: a
module-level import counts, as does one inside a test body, but an import guarded by
`if backend.backend() == ...` or a backend skipif does not.
"""

import copy
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.keras_team.keras import Keras as _LegacyKeras


_ERA_ANCHOR_SHA = "3c4f2cc6c788da9d26b40d11882e36cf87e79734"
_ERA_RANGE = "22293-to-18649"


def _anchor_pr(pr: PullRequest) -> PullRequest:
    """Copy of ``pr`` whose ``base.sha`` is the era anchor (shared ImageBase only)."""
    anchored = copy.deepcopy(pr)
    anchored.base.sha = _ERA_ANCHOR_SHA
    return anchored


class ImageBase(Image):
    """Base image -- python:3.11 plus the cloned repo. Nothing else.

    FULL `python:3.11`, not `-slim`. The base Dockerfile must clone the repo BEFORE
    prepare.sh exists, so git has to be in the image already. Measured:
        python:3.11-slim   git=MISSING  gcc=NO   pkg-config=NO   CA=224449
        python:3.11        git=2.47.3   gcc=yes  pkg-config=yes  CA=224449
    The full variant also supplies gcc and pkg-config, which the old inline apt
    block was installing, so no apt layer is needed and this Dockerfile stays the
    minimal FROM / WORKDIR / clone shape the enhancer expects.
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

    def dependency(self) -> str:
        return "python:3.11"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_ERA_RANGE}"

    def workdir(self) -> str:
        return f"base-{_ERA_RANGE}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Base layer: infra block + a plain clone. No checkout, no history scrub.

        Those steps live in ImageDefault now, pinned per PR. Suppressing them here
        needs a trick, because the enhancer adds them two different ways:
        `_standardize_repo_fetch` rewrites the clone line into
        clone+WORKDIR+reset+checkout+hardening+CMD, and `_inject_final_sanitize`
        puts the hardening block back if it is missing. The one supported opt-out
        is `enhance()`'s first guard: content that already carries the syntax
        directive is returned verbatim. So this method runs the enhancer itself on
        a shim, deletes the checkout/scrub span from the result, and returns the
        finished text -- the pipeline's own enhance() call then no-ops on it.

        Running the real enhancer rather than hand-copying its output keeps the
        ARGs, proxy vars, ENV block, OCI labels and cert symlinks in sync with
        image.py instead of freezing a copy that silently goes stale.
        """
        from multi_swe_bench.harness.image import DockerfileEnhancer

        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        raw = f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""

        class _Shim:
            pr = self.pr

            @staticmethod
            def dependency():
                return image_name

            @staticmethod
            def dockerfile():
                return raw

        enhanced = DockerfileEnhancer.enhance(_Shim())

        scrub_start = enhanced.find("RUN git reset --hard")
        cmd_start = enhanced.find('CMD ["/bin/bash"]')
        if scrub_start != -1 and cmd_start != -1 and scrub_start < cmd_start:
            enhanced = enhanced[:scrub_start] + enhanced[cmd_start:]
        return enhanced


_RUN_TESTS_BODY = r"""set +e
set -uo pipefail

export CI=true
DEFAULT_BACKEND="${KERAS_BACKEND:-numpy}"
TEST_TIMEOUT="${KR_TEST_TIMEOUT:-600}"
MIN_RESULTS="${KR_MIN_RESULTS:-0}"

TARGETS=$(python3 - /home/test.patch /home/fix.patch <<'PY_EXTRACT_TARGETS'
import re, sys

test_files = set()
for patch_path in sys.argv[1:]:
    try:
        content = open(patch_path).read()
    except (IOError, OSError):
        continue
    for m in re.finditer(r'^diff --git a/(\S+) b/(\S+)', content, re.M):
        path = m.group(2)
        if path.endswith('_test.py') or '/test_' in path or '/tests/' in path:
            test_files.add(path)

print(' '.join(sorted(test_files)))
PY_EXTRACT_TARGETS
)
TEST_FILES=$(echo "$TARGETS" | sed -n '1p')

EXISTING=""
for f in $TEST_FILES; do
  [ -f "$f" ] && EXISTING="$EXISTING $f"
done
EXISTING=$(echo "$EXISTING" | xargs)

echo "KR RUNNER: default backend = $DEFAULT_BACKEND"
echo "KR RUNNER: target files   = [$TEST_FILES]"
echo "KR RUNNER: existing here  = [$EXISTING]"

if [ -z "$EXISTING" ]; then
  echo "KR RUNNER: no target test file exists in this tree; nothing to measure"
  exit 0
fi

backend_for() {
  case "$1" in
    integration_tests/*)
      if grep -qE '^(import|from) tensorflow' "$1"; then echo tensorflow
      elif grep -qE '^(import|from) torch' "$1"; then echo torch
      elif grep -qE '^(import|from) jax' "$1"; then echo jax
      else echo "$DEFAULT_BACKEND"; fi ;;
    *) echo "$DEFAULT_BACKEND" ;;
  esac
}

G_numpy=""; G_tensorflow=""; G_torch=""; G_jax=""
for f in $EXISTING; do
  case "$(backend_for "$f")" in
    tensorflow) G_tensorflow="$G_tensorflow $f" ;;
    torch)      G_torch="$G_torch $f" ;;
    jax)        G_jax="$G_jax $f" ;;
    *)          G_numpy="$G_numpy $f" ;;
  esac
done

: > /tmp/kr-test.log
worst_rc=0
for be in numpy tensorflow torch jax; do
  eval "group=\$G_$be"
  group=$(echo "$group" | xargs)
  [ -z "$group" ] && continue
  echo "KR RUNNER: --- KERAS_BACKEND=$be :: [$group] ---" | tee -a /tmp/kr-test.log
  KERAS_BACKEND="$be" pytest --no-header -rA --tb=short -p no:cacheprovider -v \
    --timeout="$TEST_TIMEOUT" $group >> /tmp/kr-test.log 2>&1
  rc=$?
  echo "KR RUNNER: backend $be exited $rc"
  if [ "$rc" -ge 2 ] || { [ "$rc" -eq 1 ] && [ "$worst_rc" -eq 0 ]; }; then
    if [ "$rc" -gt "$worst_rc" ]; then worst_rc=$rc; fi
  fi
done
cat /tmp/kr-test.log
rc=$worst_rc

results=$(grep -cE "::.*(PASSED|FAILED|SKIPPED|ERROR)|^(PASSED|FAILED|SKIPPED|ERROR) " /tmp/kr-test.log)
echo "KR RUNNER: worst pytest rc=$rc, $results result lines collected"

if [ "$rc" -ge 2 ]; then
  echo "KR RUNNER: INFRASTRUCTURE FAILURE: pytest exited $rc, which is not a test failure"
  echo "KR RUNNER: the results above are not trustworthy"
  exit "$rc"
fi

if [ "$results" -lt "$MIN_RESULTS" ]; then
  echo "KR RUNNER: INFRASTRUCTURE FAILURE: collected $results result lines, expected at least $MIN_RESULTS"
  exit 1
fi

exit 0
"""


_STAGE_HEADER = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git checkout -- . 2>/dev/null || true
"""


_FILTER_PATCH_FN = r"""filter_patch() {
  python3 - "$1" <<'PY_FILTER_PATCH'
import re, sys

if len(sys.argv) < 2:
    sys.exit(1)

try:
    content = open(sys.argv[1]).read()
except (IOError, OSError):
    sys.exit(1)

if not content.strip():
    sys.exit(1)

parts = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
filtered = [p for p in parts if p.strip() and 'Binary files' not in p]
result = ''.join(filtered)

if result.strip():
    sys.stdout.write(result)
else:
    sys.exit(1)
PY_FILTER_PATCH
}
"""


_APPLY_TEST = r"""filter_patch /home/test.patch > /tmp/filtered_test.patch
git apply --whitespace=nowarn /tmp/filtered_test.patch
"""


_APPLY_FIX = r"""filter_patch /home/fix.patch > /tmp/filtered_fix.patch
git apply --whitespace=nowarn /tmp/filtered_fix.patch
"""


_DEFAULT_TORCH = "2.5.1"

_PR_ENV = {
    18649: ("0.4.19", ["tensorflow"], "2.15.0"),
    19459: ("0.4.26", [], ""),
    19903: ("0.4.30", ["tensorflow"], "2.16.1"),
    19937: ("0.4.30", [], ""),
    21139: ("0.5.0", ["tensorflow"], "2.18.0"),
    21953: ("0.8.1", ["tensorflow"], "2.20.0"),
    22106: ("0.9.0.1", ["tensorflow", "torch"], "2.20.0", "2.10.0"),
    22112: ("0.8.3", ["tensorflow", "torch"], "2.20.0", "2.10.0"),
    22257: ("0.9.0.1", ["tensorflow"], "2.20.0"),
    22293: ("0.9.1", ["torch"], "", "2.10.0"),
}


_TEST_BACKEND = {
    21139: "tensorflow",
    22106: "torch",
    22293: "torch",
}


def _backend_env(pr: PullRequest) -> str:
    """`export KERAS_BACKEND=...` for PRs whose tests only fail on one backend.

    The runner defaults to numpy (`DEFAULT_BACKEND="${KERAS_BACKEND:-numpy}"`),
    which is right for most PRs but silently wrong for a backend-specific fix: the
    added tests either skip ("Trainer not implemented for NumPy and OpenVINO
    backend") or pass untouched, stage 2 reports zero failures, and Report.check()
    rejects the instance for having no !PASS->PASS transition. Measured on the
    built images: 21139 goes 0 -> 4 failures on tensorflow, 22106 0 -> 1 on torch,
    22293 0 -> 1 on torch, and each returns to zero once fix.patch is applied.

    22293 is titled an OpenVINO bug and its test patch un-excludes an OpenVINO
    test, but it passes on openvino and fails on torch with "value cannot be
    converted to type int32 without overflow" -- its fix touches
    backend/torch/{core,random}.py, which is the real signal.
    """
    backend = _TEST_BACKEND.get(pr.number)
    return f"export KERAS_BACKEND={backend}\n\n" if backend else ""


def _prepare_sh(pr: PullRequest) -> str:
    """Build prepare.sh for one PR: checkout, deps, assert, clean-tree check."""
    if pr.number not in _PR_ENV:
        raise KeyError(
            f"PR {pr.number} has no _PR_ENV entry; add its jax pin and backends "
            f"rather than letting it build an unpinned environment"
        )
    _entry = _PR_ENV[pr.number]
    jax, backends, tf = _entry[0], _entry[1], _entry[2]
    torch_pin = _entry[3] if len(_entry) > 3 else _DEFAULT_TORCH

    out = [
        "#!/bin/bash",
        "set -e",
        "",
        f"cd /home/{pr.repo}",
        "git reset --hard",
        "bash /home/check_git_changes.sh",
        f"git checkout {pr.base.sha}",
        "bash /home/check_git_changes.sh",
        "",
    ]

    tf_arg = ""
    if tf:
        out += [
            f'TF_PKG="tensorflow-cpu~={tf}"',
            f'[ "$(uname -m)" = x86_64 ] || TF_PKG="tensorflow~={tf}"',
        ]
        tf_arg = " $TF_PKG"

    if "torch" in backends:
        out.append(
            "pip install --no-cache-dir --index-url "
            f"https://download.pytorch.org/whl/cpu torch=={torch_pin} || true"
        )

    out += [
        f'pip install --no-cache-dir pytest pytest-timeout "jax[cpu]=={jax}" scipy{tf_arg} || true',
        "pip uninstall -y keras keras-nightly || true",
        "pip install --no-cache-dir . || true",
        "",
    ]

    for b in ["numpy", "jax"] + backends:
        out.append(f'KERAS_BACKEND={b} python3 -c "import keras"')

    out += ["", "bash /home/check_git_changes.sh", ""]
    return "\n".join(out)


_PR_SCRUB = r"""RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""

class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "ImageBase":
        return ImageBase(_anchor_pr(self.pr), self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
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
                _prepare_sh(self.pr),
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "run.sh",
                (_STAGE_HEADER + _backend_env(self.pr) + _RUN_TESTS_BODY).replace(
                    "__REPO__", self.pr.repo
                ),
            ),
            File(
                ".",
                "test-run.sh",
                (
                    _STAGE_HEADER
                    + _backend_env(self.pr)
                    + _FILTER_PATCH_FN
                    + _APPLY_TEST
                    + _RUN_TESTS_BODY
                ).replace("__REPO__", self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                (
                    _STAGE_HEADER
                    + _backend_env(self.pr)
                    + _FILTER_PATCH_FN
                    + _APPLY_TEST
                    + _APPLY_FIX
                    + _RUN_TESTS_BODY
                ).replace("__REPO__", self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        """Thin PR layer: inherit the base, stage the files, run prepare.sh once.

        Everything that used to live here inline -- the apt block, the pip
        installs, `git reset`/`git checkout`, and `pip install .` -- moved into
        prepare.sh so this file keeps the minimal shape the enhancer expects.

        That also removed two real defects. The old version hard-coded the base
        SHA as a literal (`RUN git checkout ca9519bf...`) instead of using
        ${BASE_COMMIT}; and because the enhancer appends its hardening block and
        CMD to whatever the config emits, those inline steps landed *after*
        `CMD ["/bin/bash"]` and after the history scrub.

        The scrub is repeated here, per PR, and that is not redundant. The base is
        anchored at the era's NEWEST commit, so its history legitimately contains
        every later commit -- including, for an older PR, the very commit that
        fixes it. Re-scrubbing to this PR's own base leaves the image holding only
        commits reachable from it.

        `${BASE_COMMIT}` is declared as an ARG defaulted to this PR's sha because
        build_dataset passes that build arg only for base images (it sets buildargs
        when `dependency()` returns a str); an undeclared ${BASE_COMMIT} would
        expand to empty here. It runs after prepare.sh, which does the checkout and
        the pip installs -- scrubbing first would be undone by them.
        """
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{_PR_SCRUB}
{self.clear_env}

"""


@Instance.register("keras-team", "keras_22293_to_18649")
@Instance.register("keras-team", "keras")
class KERAS_22293_TO_18649(Instance):
    """keras-team/keras, PRs 22293 down to 18649.

    Registered under BOTH keys on purpose. `Instance.create` routes on
    `pr.number_interval` when the dataset sets one and falls back to `org/repo`
    otherwise; this era's raw dataset leaves it empty, so the plain `keras` key is
    the only one it can reach. Claiming that key would shadow keras.py, whose era
    (20745-to-19955) has its own anchor and _PR_ENV, so any PR this table does not
    own is handed straight back to that class -- keras.py is not modified, and its
    PRs keep building exactly as before.

    The `import ... as _LegacyKeras` above runs keras.py's registration first, so
    the decorator here always wins the shared key regardless of __init__ order.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if pr.number not in _PR_ENV:
            return _LegacyKeras(pr, config, *args, **kwargs)
        return super().__new__(cls)

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log_clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", log)

        pattern = re.compile(
            r"^(?P<fname>\S+\.py::\S.*?)\s+(?P<fstatus>PASSED|FAILED|SKIPPED|ERROR)(?:\s|$)"
            r"|"
            r"^(?P<rstatus>PASSED|FAILED|SKIPPED|ERROR)\s+(?P<rname>\S+\.py::\S.*?)"
            r"(?:\s+-\s.*)?$",
            re.M,
        )

        for match in pattern.finditer(log_clean):
            if match.group("fname"):
                test_name = match.group("fname").strip()
                status = match.group("fstatus")
            elif match.group("rname"):
                test_name = match.group("rname").strip()
                status = match.group("rstatus")
            else:
                continue

            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
