from __future__ import annotations

import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# --------------------------------------------------------------------------- #
# ergochat/ergo - a modern IRC server (ircd) written in Go.
#
# Dataset: ergochat__ergo_raw_dataset.jsonl (1 record, pr-1314, base.sha
# 4336f5620479f5d2282f9b2cc305b0d70068ba3b, merged 2020-10-12).
#
# Three facts about this repo drive every choice below.
#
# 1. THE MODULE IS NAMED `github.com/oragono/oragono`, NOT `ergochat/ergo`.
#    The project was renamed after this base commit, so `go.mod` at the graded
#    commit still declares the old path and `go test -json` will report
#    `"Package": "github.com/oragono/oragono/irc/..."`. That is correct, not a
#    misconfiguration - do not "fix" it. The clone URL stays
#    https://github.com/ergochat/ergo.git (GitHub serves the renamed repo and the
#    history is unbroken), and the module path never has to match it.
#
# 2. DEPENDENCIES ARE VENDORED. `vendor/` + `vendor/modules.txt` are committed and
#    `go.mod` declares `go 1.15`, so Go >= 1.14 selects `-mod=vendor` on its own.
#    The flag is still passed explicitly at every graded stage so the guarantee
#    does not depend on a default: all three stages resolve imports from the
#    checked-out tree and need no network at all. `GO111MODULE=on` is set for the
#    same reason - 1.15 is the last release whose default is `auto`, where module
#    mode is inferred from where the tree happens to sit relative to GOPATH. The
#    checkout at /home/<repo> is outside /go/src so `auto` would resolve to module
#    mode anyway, but pinning it removes a version-dependent default from under
#    the vendor guarantee rather than relying on it. This matters for pr-1314
#    specifically - its fix patch adds `import "golang.org/x/crypto/bcrypt"` to
#    irc/migrations/passwords.go, and that package IS already vendored (listed
#    under `# golang.org/x/crypto` in vendor/modules.txt, alongside blowfish,
#    pbkdf2, sha3 and ssh/terminal). The fix patch does not touch go.mod, go.sum
#    or vendor/, so the fix stage builds offline. `./...` never descends into
#    `vendor/`, so no vendored package is graded as a test.
#
# 3. THE TEST STAGE DOES NOT COMPILE - BY DESIGN. The test patch adds five
#    `TestAnopePassphrase*` functions plus `TestAthemeRawSha1` to
#    irc/migrations/passwords_test.go, and they call `CheckAnopePassphrase`,
#    which the FIX patch introduces. With test.patch applied and fix.patch not,
#    package irc/migrations therefore fails to build:
#
#        # github.com/oragono/oragono/irc/migrations [.....test]
#        irc/migrations/passwords_test.go:113:8: undefined: CheckAnopePassphrase
#        FAIL  github.com/oragono/oragono/irc/migrations [build failed]
#
#    A build failure emits NO per-test json events, so a naive parser would
#    silently credit that stage with zero tests for the package. parse_log()
#    below catches both the json-side and the plain-text-side form of it (see
#    the comments there). The resulting classification is the intended one:
#      * the 6 added tests   -> run=NONE, test=NONE, fix=PASS -> n2p
#        (report.py's `_authored_via_diff` matches each name against the
#        test patch's `+func Test...` lines, so they are real n2p, not phantoms)
#      * TestAthemePassphrases / TestOragonoLegacyPassphrase, which already
#        existed and were hidden by the build failure
#                            -> run=PASS, test=NONE, fix=PASS -> p2p (CBC)
#    The instance qualifies on n2p. Note also that fix_patch_files and
#    test_patch_files are disjoint here (the fix only touches non-test .go files
#    plus two distrib/*.py scripts), so the report.py tamper guard is clean.
#
# Go toolchain: `go.mod` declares `go 1.15` and .travis.yml at this commit pins
# `go: "1.15.x"` on focal, so golang:1.15 is the exact contemporary toolchain -
# no guessing, and no risk of a 2020 dependency tripping over a modern compiler.
# The tag publishes linux/amd64 and linux/arm64v8, so the multi-arch build works.
# golang:1.15 is buster-based and buster's apt repos are archived, but that never
# matters here: the base layer installs nothing. golang:* is buildpack-deps-scm
# derived, so git and ca-certificates are already present, which is all the
# clone, the checkout and the enhancer's cert-symlink block require.
#
# Graded command: `go test -json -count=1 -mod=vendor ./... 2>&1`.
#   * `-json`     - names are package-qualified (`<pkg>::<Test>`). Required:
#                   `./...` spans 8 test packages and Go allows the same test
#                   name in several of them, so bare `--- PASS: TestX` names
#                   from `-v` would collide and desync the 3-stage union.
#   * `-count=1`  - disables the test result cache. Without it the fix stage
#                   could replay cached verdicts for packages the patches did
#                   not touch, and the p2p set would stop being a real measurement.
#   * `2>&1`      - compile errors go to stderr as plain text, not json. They
#                   must reach parse_log for the build-failure guard to fire.
# `make test` is deliberately NOT used: it also runs `go vet`, a gofmt check and
# a `python3 ./gencapdefs.py | diff` gate, none of which are test outcomes, and
# any of which would abort the stage over style rather than correctness.
# --------------------------------------------------------------------------- #

BASE_IMAGE = "golang:1.15"
GO_TEST_CMD = "go test -json -count=1 -mod=vendor ./... 2>&1"

# Exported by prepare.sh and by all three graded scripts rather than set as
# ENV in the base Dockerfile: the reference base Dockerfile carries only the
# enhancer's own ENV block, and keeping these in one shared string means the
# cache-warming build and the three graded stages cannot drift apart.
#   CI            - conventional test-suite signal.
#   CGO_ENABLED=0 - pure-Go build; removes the cgo/arch surface on arm64.
#   GO111MODULE   - 1.15 is the last release defaulting to `auto`, where
#                   module mode is inferred from position relative to GOPATH.
#   GOTOOLCHAIN   - no-op on 1.15; pins behaviour if the base is ever bumped.
GO_ENV = """export CI=true
export CGO_ENABLED=0
export GO111MODULE=on
export GOTOOLCHAIN=local"""


class ErgoImageBase(Image):
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        # Per-PR, not one shared "base" tag: DockerfileEnhancer rewrites the
        # clone below into clone + `git checkout ${BASE_COMMIT}`, so this layer
        # is specific to one commit. Image dedup is keyed on image_full_name(),
        # so a shared tag would build it once and then silently reuse the wrong
        # commit for every other PR added to this dataset later.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # No `# syntax=` directive, proxy ARGs, cert symlinks or OCI labels are
        # written here on purpose: DockerfileEnhancer.enhance() injects all of
        # them, and it bails out entirely if a syntax directive is already
        # present. The clone/COPY must stay the LAST instruction - the enhancer
        # expands exactly that line into clone + WORKDIR + reset + checkout +
        # the history-scrub/integrity-assert block + CMD.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{self.clear_env}

{code}

"""


class ErgoImageDefault(Image):
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
        return ErgoImageBase(self.pr, self._config)

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
                "test.patch",
                f"{self.pr.test_patch}",
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

{go_env}

cd /home/{pr.repo}

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the build and test caches while the network is still up during the image
# build, so the three graded stages are fast. Nothing is downloaded: the tree is
# vendored, so `-mod=vendor` resolves everything locally and `go mod download`
# would be a no-op - it is deliberately omitted.
#
# `|| true` is required: this is a cache-warming step, not a gate. A transient
# arm64 emulation hiccup here must not abort the whole image, and the three
# graded stages re-run the real command from scratch regardless of what
# happens now.
#
# None of these commands write into the working tree (Go's caches live under
# GOCACHE), so the checkout stays clean for the patch-apply stages.
go build -mod=vendor ./... || true
go test -count=1 -mod=vendor ./... || true

""".format(pr=self.pr, go_env=GO_ENV),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{go_env}

cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=GO_TEST_CMD, go_env=GO_ENV),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{go_env}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=GO_TEST_CMD, go_env=GO_ENV),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{go_env}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=GO_TEST_CMD, go_env=GO_ENV),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("ergochat", "ergo")
class Ergo(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ErgoImageDefault(self.pr, self._config)

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

        # Strip ANSI escapes first: colored output would otherwise break both the
        # `line.startswith("{")` json guard and the anchored build-failure regex.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        def _rel(pkg: str) -> str:
            """Go import path -> repo-relative directory.

            A module path starts with a domain-like first segment
            (`github.com`, `gopkg.in`, `golang.org`), so the module prefix is
            exactly the first three segments -- host/org/repo. Dropping them
            turns `github.com/oragono/oragono/irc/migrations` into
            `irc/migrations`. The module root itself becomes ".".

            Written generically rather than against the literal module string
            because this repo's module path changed with the ergo rename; a
            hardcoded prefix would silently stop matching on a later base commit
            and leak full import paths back into the ids.
            """
            parts = pkg.split("/")
            if len(parts) >= 3 and "." in parts[0]:
                rest = parts[3:]
                return "/".join(rest) if rest else "."
            return pkg

        # `go test -json` emits one JSON object per line. Test-level events carry
        # BOTH `Package` and `Test`, which is the whole reason -json is used here:
        # the name is joined as "<pkg-path>::<Test>" so a test name repeated in two
        # packages stays two distinct entries, and subtests keep their full
        # "TestX/sub" path rather than collapsing every table-driven case into the
        # parent. Package-level events have no `Test` field.
        #
        # `Package` is the IMPORT path (`github.com/oragono/oragono/irc/migrations`).
        # _rel() below trims the module prefix off it so the reported id is the
        # repo-relative directory instead -- `irc/migrations::TestAthemeRawSha1` --
        # matching the `<path>::<test>` node-id shape the delivery format uses for
        # every other language (pytest `tests/test_x.py::test_y`, and so on).
        pkg_actions: dict[str, str] = {}
        pkg_has_tests: set[str] = set()

        # A package that fails to COMPILE produces no json test event at all - go
        # writes the compile errors to stderr as plain text and closes with
        # "FAIL <pkg> [build failed]". Every test in that package would otherwise
        # vanish from the stage with no signal whatsoever. That is not a corner
        # case for this instance: it is exactly what the test stage does (see the
        # header note - the test patch calls CheckAnopePassphrase before the fix
        # patch defines it). Both forms of the signal are caught: the plain-text
        # line here, and the json package-level `fail`-with-no-tests below.
        build_failed_re = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")

        for raw in log.split("\n"):
            line = raw.strip()

            if not line.startswith("{"):
                m = build_failed_re.match(line)
                if m:
                    failed_tests.add(f"{_rel(m.group(1))}::[build]")
                continue

            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue

            action = ev.get("Action")
            pkg = ev.get("Package")
            test = ev.get("Test")

            # "run"/"output"/"pause"/"cont" events carry no verdict.
            if not pkg or action not in ("pass", "fail", "skip"):
                continue

            if not test:
                pkg_actions[pkg] = action
                continue

            pkg_has_tests.add(pkg)
            name = f"{_rel(pkg)}::{test}"

            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A package that failed while producing no test event did not build, or
        # died in TestMain. Surface it so the stage is not silently credited with
        # zero tests for that package.
        for pkg, action in pkg_actions.items():
            if action == "fail" and pkg not in pkg_has_tests:
                failed_tests.add(f"{_rel(pkg)}::[build]")

        # TestResult.__post_init__ requires the three sets to be pairwise
        # disjoint; a re-reported test can otherwise land in two of them.
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
