"""aarondl/sqlboiler harness — pre-modules (GOPATH) era.

Covers PR #686 ("Support outer joins", base sha 58d296ce, 2020-02-09).

Three facts drive this file, each verified against the base commit rather than
assumed:

1. **There is no ``go.mod``** — no ``vendor/``, ``Gopkg.toml`` or ``glide.yaml``
   either. The tree predates modules, so it only builds in GOPATH mode.

2. **The canonical import path is ``github.com/volatiletech/sqlboiler``**, not
   ``github.com/aarondl/sqlboiler``. The project changed owner after this commit;
   at base sha all 133 self-imports still say ``volatiletech``. In GOPATH mode
   the on-disk directory *is* the import path.

   ``DockerfileEnhancer`` always clones to ``/home/<repo>``, so the base image
   symlinks that checkout into ``/go/src/github.com/volatiletech/sqlboiler``.
   Verified working: ``go test`` resolves and runs through the symlink.

3. **Unpinned ``go get`` cannot build this tree.** It fetches default-branch
   HEAD, and ``volatiletech/null`` is now v9, which imports Go 1.21's ``slices``
   stdlib package and pulls in ``aarondl/null``. On golang:1.13 that fails with
   ``unrecognized import path "slices"`` before anything compiles.
"""

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Contemporary with the 2020-02-09 base commit, and the newest line that still
# supports this workflow: Go 1.22 removed GOPATH-mode `go get` and stopped
# honouring GO111MODULE=off, so a modern image cannot build a tree with no go.mod.
_GO_IMAGE = "golang:1.13"

_IMPORT_PATH = "github.com/volatiletech/sqlboiler"
_GOPATH_DIR = f"/go/src/{_IMPORT_PATH}"

# Packages the two patches touch. Scoped deliberately: the repo's other trees
# (drivers/, boil_*) need live databases and would drag unrelated failures into p2p.
_TEST_PKGS = "./queries/..."

# Explicit pins, contemporary with the base commit. This list is the primary
# mechanism because it is auditable and reproducible; the auto-resolve pass below
# is the safety net for anything it misses.
_DEPS = [
    ("github.com/DATA-DOG/go-sqlmock", "v1.4.1"),
    ("github.com/davecgh/go-spew", "v1.1.1"),
    ("github.com/friendsofgo/errors", "v0.9.2"),
    # Reached only through the TEST binary:
    #   queries (test) -> volatiletech/null -> sqlboiler/randomize -> gofrs/uuid
    # Missing it does not break `go build`; it breaks `go test` with
    # "[setup failed]", which collects zero tests. Pinned to the v3 line because
    # this import path carries no version suffix — v4+ moved to `/v4`.
    ("github.com/gofrs/uuid", "v3.2.0"),
    ("github.com/spf13/cast", "v1.3.1"),
    ("github.com/volatiletech/inflect", "v0.0.1"),
    ("github.com/volatiletech/null", "v8.0.0"),
]

# Markers fencing the two payloads the stage log carries.
_MAP_BEGIN = "===== BEGIN TEST FILE MAP ====="
_MAP_END = "===== END TEST FILE MAP ====="
_RESULTS_BEGIN = "===== BEGIN TEST RESULTS ====="
_RESULTS_END = "===== END TEST RESULTS ====="


def _dep_setup() -> str:
    """Shell that places the checkout in GOPATH and resolves dependencies.

    Runs from the PR layer's prepare.sh rather than the base image: the
    enhancer's clone replacement terminates the base Dockerfile with ``CMD``, so
    the base cannot carry any step after the checkout.

    Two mechanisms, in order:

    * **Explicit pins** (``_DEPS``) — auditable and reproducible, and the reason
      the build is deterministic rather than "whatever HEAD is today".
    * **A bounded auto-resolve pass** — fetches anything the explicit list missed
      and pins it by DATE to the base commit. This exists because a hand-written
      list is exactly how ``gofrs/uuid`` (reachable only through the test binary)
      was missed once already, and a missing GOPATH package does not fail loudly:
      it surfaces as ``[setup failed]`` and a stage that collected zero tests.

    The final compile check is fail-closed: if ``./queries/...`` still does not
    build, prepare.sh exits non-zero and the image build fails, instead of
    shipping an image whose every stage would silently report no tests.
    """
    explicit = "\n".join(
        f'go get -d -v {path}/... >/dev/null 2>&1 || true\n'
        f'test -d /go/src/{path} || {{ echo "FATAL: {path} not fetched" >&2; exit 1; }}\n'
        f'git -C /go/src/{path} checkout --quiet {tag} '
        f'|| {{ echo "FATAL: cannot pin {path} -> {tag}" >&2; exit 1; }}\n'
        f'echo "pinned {path} -> {tag}"'
        for path, tag in _DEPS
    )

    return f"""# Place the checkout at its canonical import path: GOPATH mode resolves packages
# by directory, and this tree imports itself as {_IMPORT_PATH}.
mkdir -p /go/src/github.com/volatiletech
[ -e {_GOPATH_DIR} ] || ln -s /home/sqlboiler {_GOPATH_DIR}
cd {_GOPATH_DIR}

{explicit}

BASE_DATE=$(git show -s --format=%cI HEAD)
pin_by_date() {{
  for d in $(find /go/src -maxdepth 3 -mindepth 3 -type d | grep -v '/sqlboiler$'); do
    [ -d "$d/.git" ] || continue
    c=$(git -C "$d" rev-list -1 --before="$BASE_DATE" HEAD 2>/dev/null || true)
    if [ -n "$c" ] && [ "$(git -C "$d" rev-parse HEAD)" != "$c" ]; then
      git -C "$d" checkout --quiet "$c" && echo "date-pinned ${{d#/go/src/}} -> $c"
    fi
  done
}}

# Safety net: up to three fetch/pin rounds for packages the explicit list missed.
for _ in 1 2 3; do
  missing=$(go test -count=1 -run XXX_NO_MATCH {_TEST_PKGS} 2>&1 \
    | grep -oE 'cannot find package "[^"]+"' | sed 's/cannot find package //; s/"//g' | sort -u)
  [ -z "$missing" ] && break
  echo "auto-resolving missing packages: $missing"
  for m in $missing; do go get -d "$m" >/dev/null 2>&1 || true; done
  pin_by_date
done

go test -count=1 -run XXX_NO_MATCH {_TEST_PKGS} >/dev/null 2>&1 || {{
  echo "FATAL: {_TEST_PKGS} does not compile after dependency resolution" >&2
  go test -count=1 -run XXX_NO_MATCH {_TEST_PKGS} 2>&1 | tail -20 >&2
  exit 1
}}
echo "dependency resolution OK: {_TEST_PKGS} compiles"
"""


def _test_cmd() -> str:
    """Byte-identical across the three stages.

    Emits two fenced payloads:

    * a TestName -> source-file map, harvested from the `func TestXxx` decls, so
      ids can be reported as `path/to/file_test.go::TestName` (the pytest-style
      node id used across this dataset). `go test -json` reports the *package*,
      never the file, so the map is the only way to get there.
    * the raw `go test -json` stream. JSON rather than `-v` text because each
      event carries Package and Test explicitly and cannot be corrupted by output
      a test writes to stdout.

    `|| true`: the test stage is EXPECTED to fail (that is the signal), and the
    verdict is read from the JSON, not the exit code.
    """
    return f"""export GO111MODULE=off
cd {_GOPATH_DIR}
echo '{_MAP_BEGIN}'
grep -rhoE '^func (Test[A-Za-z0-9_]+)' --include='*_test.go' {_TEST_PKGS.replace("/...", "")} 2>/dev/null >/dev/null || true
for f in $(find {_TEST_PKGS.replace("/...", "")} -name '*_test.go' 2>/dev/null); do
  sed -nE 's/^func (Test[A-Za-z0-9_]+).*/\\1/p' "$f" | while read -r t; do
    printf 'TESTFILE\\t%s\\t%s\\n' "${{f#./}}" "$t"
  done
done
echo '{_MAP_END}'
echo '{_RESULTS_BEGIN}'
go test -json -count=1 -timeout 15m {_TEST_PKGS} 2>&1 || true
echo '{_RESULTS_END}'
"""


class _ImageBase(Image):
    """Toolchain + checkout, in the canonical base-image shape.

    ``dockerfile()`` emits only ``FROM`` / ``WORKDIR`` / the clone, because
    ``DockerfileEnhancer._standardize_repo_fetch`` rewrites that clone line into
    clone + ``WORKDIR`` + ``git reset --hard`` + ``git checkout ${BASE_COMMIT}``
    + the history-hardening block + ``CMD``. Its replacement *ends* with ``CMD``,
    so the clone must be the last instruction here — anything after it would land
    below the CMD and never run. That is precisely why dependency setup lives in
    the PR layer's prepare.sh instead of in this image.

    Nothing here re-emits ENV. The enhancer contributes the single
    DEBIAN_FRONTEND/LANG/TZ/proxy/cert stanza; the default ``Image.dockerfile()``
    is deliberately NOT used because it adds its own ``ENV DEBIAN_FRONTEND`` and
    ``ENV LANG``, which is what produced a duplicated ENV block before.

    No apt layer either: golang:1.13 is buildpack-deps derived and already ships
    git, curl and a C toolchain, while its Debian buster mirrors are retired — an
    ``apt-get update`` here would be both unnecessary and fragile.
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        # Per-PR, not a shared tag: the enhancer bakes one BASE_COMMIT in AND
        # scrubs every other ref, so a shared base would pin the whole repo to
        # whichever PR built it first. Also what the Dockerfile QC contract
        # requires the PR layer to inherit.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/

# git and a C toolchain already ship in golang:1.13 (buildpack-deps derived), so
# no package installation step is needed here.
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
"""


class _ImageDefault(Image):
    """Thin per-PR layer: copy the harness scripts in and prepare."""

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
        return _ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = _test_cmd()

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
                f"""#!/bin/bash
set -e
export GO111MODULE=off

{_dep_setup()}

# Re-assert the pinned baseline in THIS layer. The base image already checked out
# ${{BASE_COMMIT}} and its hardening block asserts HEAD == BASE_COMMIT, so this is a
# second, independent guard rather than a correction -- but without it nothing in
# the PR layer names the SHA, and a base built from a different commit would go
# unnoticed here. Also leaves the worktree exactly as committed, so `git apply` in
# the graded stages cannot fail on a tree that setup dirtied.
cd {_GOPATH_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
""",
            ),
            File(".", "run.sh", f"#!/bin/bash\nset -o pipefail\n\n{test_cmd}"),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {_GOPATH_DIR}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {_GOPATH_DIR}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        # One COPY per file, matching the reference PR-layer layout. The base
        # already owns the clone, checkout and history hardening, so this layer
        # carries nothing else.
        copy_commands = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}
"""


def _parse_go_test_json(test_log: str) -> TestResult:
    """Build `path/to/file_test.go::TestName` ids from the fenced payloads.

    Falls back to the package path when a test has no mapped file (for example a
    generated test, or a stage that died before the map was emitted) so a name is
    never dropped silently.
    """
    file_of: dict[str, str] = {}
    map_start = test_log.find(_MAP_BEGIN)
    map_end = test_log.find(_MAP_END)
    if map_start != -1 and map_end > map_start:
        for line in test_log[map_start:map_end].splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] == "TESTFILE":
                file_of[parts[2].strip()] = parts[1].strip()

    body = test_log
    res_start = test_log.find(_RESULTS_BEGIN)
    res_end = test_log.find(_RESULTS_END)
    if res_start != -1 and res_end > res_start:
        body = test_log[res_start + len(_RESULTS_BEGIN) : res_end]

    verdicts: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = ev.get("Action")
        test = ev.get("Test")
        # Package-level pass/fail events carry no Test key. Recording them would
        # invent "tests" named after packages and inflate every bucket.
        if not test or action not in ("pass", "fail", "skip"):
            continue

        # Subtests arrive as TestX/sub; the file is keyed on the parent decl.
        root = test.split("/", 1)[0]
        path = file_of.get(root)
        if not path:
            pkg = ev.get("Package") or ""
            if pkg.startswith(_IMPORT_PATH + "/"):
                pkg = pkg[len(_IMPORT_PATH) + 1 :]
            elif pkg == _IMPORT_PATH:
                pkg = "."
            path = pkg

        verdicts[f"{path}::{test}" if path else test] = action

    passed = {k for k, v in verdicts.items() if v == "pass"}
    failed = {k for k, v in verdicts.items() if v == "fail"}
    skipped = {k for k, v in verdicts.items() if v == "skip"}

    # The three sets MUST be disjoint or TestResult raises.
    passed -= failed
    skipped -= failed
    skipped -= passed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("aarondl", "sqlboiler")
class Sqlboiler(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_json(test_log)
