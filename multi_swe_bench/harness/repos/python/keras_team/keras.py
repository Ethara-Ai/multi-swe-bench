import copy
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------- shared base
# One base image for the whole dataset, pinned to the NEWEST base commit among its
# PRs (PR 20745, 2025-01-08) rather than to whichever PR builds first.
#
# The enhancer appends `git checkout ${BASE_COMMIT}` and then scrubs history down
# to it, asserting
#     test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# so the image holds ONLY commits reachable from BASE_COMMIT, and a later PR can
# check out its own commit only if that commit is an ANCESTOR of the pinned one.
# build_dataset.py:629 takes BASE_COMMIT from `image.pr.base.sha`, so letting the
# shared base inherit the triggering PR would make correctness depend on build
# order.
#
# Verified with `git merge-base --is-ancestor` against a local clone: 19955, 20023,
# 20380 and 20447 are all ancestors of 20745.
#
# CONSTRAINT: the anchor must stay the newest commit in the range this base serves.
# A PR with a newer base commit will not exist in the scrubbed image -- that fails
# loudly in prepare.sh's `git checkout`, never silently on the wrong tree; move the
# anchor (and _ERA_RANGE) forward when adding one.
_ERA_ANCHOR_SHA = "97c1c00af87aaae69dd56aa2480ff304d40cf516"  # PR 20745
_ERA_RANGE = "20745-to-19955"


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
        # Range-named shared base, the established form in this tree (70 configs
        # use `base-<hi>-to-<lo>`). The HIGH end is the anchor commit, so the name
        # states its own validity: every PR it claims to serve is <= the anchor and
        # therefore an ancestor whose commit survives the scrub.
        return f"base-{_ERA_RANGE}"

    def workdir(self) -> str:
        return f"base-{_ERA_RANGE}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


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


# No `|| true` on either apply, deliberately. A patch that fails to apply must
# abort the stage: otherwise the stage silently measures the UNPATCHED tree,
# stages 2 and 3 both report the base state, f2p collapses to zero, and
# Report.check() sees nothing wrong with the result.
_APPLY_TEST = r"""filter_patch /home/test.patch > /tmp/filtered_test.patch
git apply --whitespace=nowarn /tmp/filtered_test.patch
"""


# test.patch FIRST, then fix.patch -- order matters and both must apply.
_APPLY_FIX = r"""filter_patch /home/fix.patch > /tmp/filtered_fix.patch
git apply --whitespace=nowarn /tmp/filtered_fix.patch
"""


# Per-PR build environment, resolved once instead of probed at build time.
#
# jax: requirements.txt leaves `jax[cpu]` unbounded, so a plain install resolves to
# today's jax (0.10.2) against a 2024 tree. That release dropped
# jax.experimental.enable_x64, which variables_test.py and numpy_test.py import
# heavily -- 1326 of PR 19955's ids and 3270 of PR 20745's failed on that one
# ImportError, in every stage, while the instances still reported valid. Each pin
# below is the newest jax released on or before that PR's base-commit date.
#
# backends: the heavy frameworks a PR's target tests actually import on a reachable
# path. Verified against checked-out trees, not guessed from patch text: PR 20023's
# torch import sits on an untouched context line, and the tensorflow imports in
# numpy_test.py / trainer_test.py are unreachable on the numpy backend
# (`if backend.backend() == "tensorflow":`, `@skipif(...)`), so they are excluded.
#
# tf: the pin from that commit's own requirements.txt.
_PR_ENV = {
    19955: ("0.4.30", ["torch"], ""),
    20023: ("0.4.30", ["torch"], ""),
    20380: ("0.4.34", ["tensorflow", "torch"], "2.17.0"),
    20447: ("0.4.35", [], ""),
    20745: ("0.4.38", [], ""),
}


def _prepare_sh(pr: PullRequest) -> str:
    """Build prepare.sh for one PR: checkout, deps, assert, clean-tree check."""
    if pr.number not in _PR_ENV:
        raise KeyError(
            f"PR {pr.number} has no _PR_ENV entry; add its jax pin and backends "
            f"rather than letting it build an unpinned environment"
        )
    jax, backends, tf = _PR_ENV[pr.number]

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

    # tensorflow-cpu publishes no linux aarch64 wheel; only `tensorflow` does, and
    # on aarch64 that package IS the CPU build.
    tf_arg = ""
    if tf:
        out += [
            f'TF_PKG="tensorflow-cpu~={tf}"',
            f'[ "$(uname -m)" = x86_64 ] || TF_PKG="tensorflow~={tf}"',
        ]
        tf_arg = " $TF_PKG"

    # torch comes from the pytorch CPU index, which carries both arches and avoids
    # the ~900MB CUDA wheel plain PyPI hands amd64. Installed separately because it
    # shares no pins with the group below.
    if "torch" in backends:
        out.append(
            "pip install --no-cache-dir --index-url "
            "https://download.pytorch.org/whl/cpu torch==2.5.1 || true"
        )

    # One resolver pass for everything that shares transitive pins. jax[cpu] alone
    # pulls ml_dtypes>=0.5, which TensorFlow 2.17 caps at <0.5; installed separately
    # they fight and jax dies. Resolved together, pip just picks a compatible set.
    out += [
        f'pip install --no-cache-dir pytest pytest-timeout "jax[cpu]=={jax}" scipy{tf_arg} || true',
        "pip uninstall -y keras keras-nightly || true",
        "pip install --no-cache-dir . || true",
        "",
    ]

    # A bare `import keras` defaults to the tensorflow backend, so it is not a valid
    # check. Assert each backend the stage scripts will actually use.
    for b in ["numpy", "jax"] + backends:
        out.append(f'KERAS_BACKEND={b} python3 -c "import keras"')

    # Last, with no `exit 0` after it: this script's exit status IS the clean-tree check.
    out += ["", "bash /home/check_git_changes.sh", ""]
    return "\n".join(out)


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
        # Anchored: every PR shares one base pinned to the range's newest commit.
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
                # Baseline: no patch applied.
                (_STAGE_HEADER + _RUN_TESTS_BODY).replace("__REPO__", self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                (
                    _STAGE_HEADER + _FILTER_PATCH_FN + _APPLY_TEST + _RUN_TESTS_BODY
                ).replace("__REPO__", self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                (
                    _STAGE_HEADER
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
        """
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

{self.clear_env}

"""


@Instance.register("keras-team", "keras")
class Keras(Instance):
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

        # Strip the full CSI set, not just colour (`m`) sequences -- pytest also
        # emits erase-line codes, and a stray one glued to a test id would make
        # the same test read as two different names across stages.
        log_clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", log)

        # Anchored at line start, and the id is matched with `.*?` rather than a
        # character class. keras uses absl parameterized tests, whose node ids
        # carry the parameter tuple verbatim:
        #
        #   variables_test.py::VariableOpsDTypeTest::test_add_('bfloat16', 'bool')
        #
        # Parentheses, quotes, commas and spaces are not in `[\w\[\]._-]`, so the
        # previous class-based pattern died at `test_add_` and discarded the whole
        # line. That silently dropped 1326 of PR 19955's 1434 ids and 3270 of PR
        # 20745's 5798 -- the instances still validated, on a fraction of their
        # real test population.
        #
        # The path itself never contains whitespace (`\S+\.py::`), so only the part
        # AFTER `::` needs to tolerate spaces.
        #
        # `\.py::` is required on both branches. That deliberately still excludes
        # collection-error lines like `ERROR integration_tests/x.py - ImportError`,
        # which name a file and no test: PR 20023 depends on those staying absent
        # so its added tests read as NONE in the test stage and classify as n2p.
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

        # Keep the buckets disjoint. `-rA` prints every test twice -- once on the
        # verbose line as `id STATUS` and once in the summary as `STATUS id` --
        # and the regex above matches both orders, so a test reported
        # inconsistently between the two lands in two sets at once. TestResult
        # raises ValueError on any overlap, which would abort the whole instance.
        # Failure wins: crediting a test that was ever seen failing as passed is
        # the unsafe direction.
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
