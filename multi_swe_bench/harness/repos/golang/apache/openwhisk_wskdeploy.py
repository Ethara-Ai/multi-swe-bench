import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OpenwhiskWskdeployImageBase(Image):
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
        # PR 1101 is the commit that introduces `go.mod`; its base still carries
        # `Godeps/Godeps.json` and `.travis.yml` pins `go: "1.9.3"`, while the
        # go.mod the PR adds declares `go 1.14`. One toolchain has to serve both
        # sides of that boundary, and 1.16 is the oldest release that does:
        #
        #   * GO111MODULE=auto (set below) means "module mode iff a go.mod is
        #     reachable from the working directory". At the run and test-patch
        #     stages no go.mod exists, so the build falls back to GOPATH mode and
        #     resolves against the Godeps-pinned checkouts prepare.sh lays down.
        #     At the fix stage the patch creates go.mod + go.sum and the very
        #     same command switches to module mode. Go 1.13 through 1.16 all
        #     honour `auto` inside GOPATH/src; 1.16 is the newest of them and the
        #     closest to the go.mod's own `go 1.14`.
        #   * The 2016-era dependency revisions in Godeps.json (viper, hcl,
        #     afero, go-i18n) are contemporaries of this toolchain.
        #
        # This is why the base is NOT a current Go release: a newer toolchain
        # would still build, but it moves both stages further from the versions
        # the PR was written against for no benefit.
        return "golang:1.16"

    def image_tag(self) -> str:
        # Per-PR, not a shared `base`: DockerfileEnhancer injects
        # `git checkout ${BASE_COMMIT}` plus the history scrub into this image, so a
        # tag shared across PRs would be pinned to whichever PR built it first and
        # would have every other PR's commit pruned out of the object store.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

        # Everything the rendered Dockerfile needs to explain lives here, in
        # Python, rather than as `#` lines inside the f-string below. The
        # generated Dockerfile is a build artefact that reviewers read as one
        # instruction per line; 90% of the Go configs in this tree emit no author
        # comments into it at all.
        #
        # apt: NO archive.debian.org rewrite here, deliberately. golang:1.16 is a
        # Debian BULLSEYE image, not buster -- `docker run --rm golang:1.16 cat
        # /etc/os-release` reports VERSION_ID="11" -- and bullseye is still served
        # from deb.debian.org, so a plain `apt-get update` succeeds. Applying
        # R11's rewrite anyway is what breaks it: `security.debian.org` becomes
        # `archive.debian.org/debian-security`, which publishes no
        # `bullseye-security` Release file, so apt errors and the build dies with
        # exit 100. That was observed on the first build of this image. R11
        # applies to a base that is genuinely EOL; this one is not. Do not
        # "restore" the rewrite.
        #
        # git ships in the golang image already; it is installed explicitly
        # because prepare.sh clones every Godeps-pinned dependency with it, and a
        # base that depends on a tool it never declares is one upstream image
        # rebuild away from breaking.
        #
        # GO111MODULE=auto is the whole mechanism that lets one test command serve
        # all three stages (see dependency() above): no go.mod at the run and
        # test-patch stages means GOPATH mode, and the go.mod the fix patch adds
        # flips the same command to module mode. It is set on the image rather
        # than in the run scripts so prepare.sh's warm-cache step resolves
        # packages exactly the way the graded stages will.
        #
        # CGO_ENABLED is pinned rather than inherited: the golang image defaults
        # to 1, so the value would otherwise depend on whether gcc happens to be
        # present, which differs between the amd64 and arm64 builds of a Debian
        # base. Nothing in this repo or its Godeps-pinned dependencies uses cgo --
        # jibber_jabber, fsnotify and go-isatty are all pure Go over syscall -- so
        # 0 changes no test outcome and makes both architectures build alike.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    make \\
    && rm -rf /var/lib/apt/lists/*

ENV GO111MODULE=auto
ENV GOPATH=/go
ENV CGO_ENABLED=0

{code}

{self.clear_env}

"""


class OpenwhiskWskdeployImageDefault(Image):
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
        return OpenwhiskWskdeployImageBase(self.pr, self.config)

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

# Move the work tree to the import path BEFORE touching git, so every command
# below runs against its final location.
#
# The direction of this link matters and the obvious direction is the wrong one.
# GOPATH mode resolves a package by its import path under $GOPATH/src, so the
# tree has to be reachable at /go/src/github.com/{pr.org}/{pr.repo}. Putting a
# SYMLINK there does not work: when `go test` expands a `...` pattern it walks
# $GOPATH/src and deliberately skips symlinked directories (loop protection),
# so the pattern matches nothing at all:
#
#     warning: ignoring symlink /go/src/github.com/{pr.org}/{pr.repo}
#     go: warning: "github.com/{pr.org}/{pr.repo}/..." matched no packages
#     no packages to test
#
# That was observed, and it produced (0,0,0) in all three stages. So the real
# directory lives at the import path and /home/{pr.repo} is the symlink back --
# the harness, the staged patches and `git apply` all follow it transparently.
mkdir -p /go/src/github.com/{pr.org}
mv /home/{pr.repo} /go/src/github.com/{pr.org}/{pr.repo}
ln -sfn /go/src/github.com/{pr.org}/{pr.repo} /home/{pr.repo}

cd /go/src/github.com/{pr.org}/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Restore the Godeps-pinned dependencies into GOPATH.
#
# This is `godep restore` done by hand, and the revisions are read out of the
# repo's own Godeps/Godeps.json rather than copied into this config, so they are
# by construction the revisions the base commit declares. That matters more here
# than in a normal Go repo: this PR's gold test asserts on the exact wording of
# a gopkg.in/yaml.v2 unmarshal error ("field invalidKey not found in
# struct/type parsers.Project"), and that wording changed between the 2016
# revision Godeps pins and the v2.3.0 the PR's go.mod requires. Pull a newer
# yaml.v2 into the run and test-patch stages and the FAIL -> PASS transition
# this instance exists to measure disappears.
#
# Godeps.json lists sub-packages ("github.com/hashicorp/hcl/hcl/ast"); a clone
# is per repository, so each import path is reduced to its repository root and
# roots already cloned are skipped. The first "ImportPath" in the file is the
# project's own, which has no "Rev" after it -- `tail -n +2` drops it so the
# remaining lines pair up as ImportPath/Rev.
#
# Deliberately NOT `|| true` around the loop as a whole: a dependency that fails
# to appear does not fail loudly at test time, it fails as "cannot find package"
# inside one package, which parse_log reads as that package contributing no
# tests. Individual failures are reported and the loop continues so the build
# log names every one of them; the warm-cache step below is what turns a missing
# dependency into visible output.
grep -E '"(ImportPath|Rev)":' Godeps/Godeps.json \\
  | sed -E 's/^[[:space:]]*"[A-Za-z]+":[[:space:]]*"([^"]*)".*$/\\1/' \\
  | tail -n +2 \\
  | paste - - \\
  | while read -r importpath rev; do
      case "$importpath" in
        github.com/*)
          root=$(echo "$importpath" | cut -d/ -f1-3)
          url="https://$root.git"
          ;;
        golang.org/x/*)
          root=$(echo "$importpath" | cut -d/ -f1-3)
          url="https://github.com/golang/$(echo "$root" | cut -d/ -f3).git"
          ;;
        gopkg.in/yaml.v2)
          root="$importpath"
          url="https://github.com/go-yaml/yaml.git"
          ;;
        *)
          echo "prepare: no clone URL known for $importpath" >&2
          continue
          ;;
      esac

      dest="$GOPATH/src/$root"
      if [ -d "$dest" ]; then
        continue
      fi
      mkdir -p "$(dirname "$dest")"
      if ! git clone --quiet "$url" "$dest"; then
        echo "prepare: clone failed for $url" >&2
        continue
      fi
      if ! git -C "$dest" checkout --quiet "$rev"; then
        echo "prepare: checkout $rev failed in $dest" >&2
      fi
    done

# testify is the one test dependency Godeps.json does not carry: .travis.yml
# installs it with `go get -u github.com/stretchr/testify`, i.e. unpinned at
# whatever HEAD was on the day the build ran. An unpinned dependency makes the
# three stages incomparable across rebuilds, so it is pinned here to v1.6.1 --
# the version this PR's go.mod settles on -- together with the three packages
# testify/assert imports. These are literals because there is nothing in the
# base tree to read them from.
while read -r root url rev; do
  dest="$GOPATH/src/$root"
  if [ -d "$dest" ]; then
    continue
  fi
  mkdir -p "$(dirname "$dest")"
  if ! git clone --quiet "$url" "$dest"; then
    echo "prepare: clone failed for $url" >&2
    continue
  fi
  if ! git -C "$dest" checkout --quiet "$rev"; then
    echo "prepare: checkout $rev failed in $dest" >&2
  fi
done <<'PINNED_TEST_DEPS'
github.com/stretchr/testify https://github.com/stretchr/testify.git f654a9112bbeac49ca2cd45bfbe11533c4666cf8
github.com/davecgh/go-spew https://github.com/davecgh/go-spew.git 8991bc29aa16c548c550c7ff78260e27b9ab7c73
github.com/pmezard/go-difflib https://github.com/pmezard/go-difflib.git 792786c7400a136282c1664665ae0a8db921c6c2
gopkg.in/yaml.v3 https://github.com/go-yaml/yaml.git f6f7691b1fdeb513f56608cd2c32c51f8194bf51
PINNED_TEST_DEPS

# Warm the compile cache and, more importantly, surface a broken GOPATH now --
# at build time, in the build log -- instead of at stage time as a package that
# quietly reports no tests.
#
# `-exec /bin/true` makes `go test` compile each test binary and then "run" it
# via /bin/true, which exits 0 immediately: the cache is warmed and no test
# body, TestMain or package-level init() ever executes. `-run` alone would not
# do that -- it filters which tests execute, but the binary still starts.
# `timeout` bounds the step in case a future toolchain blocks elsewhere.
#
# `-vet=off` for the same reason the graded command carries it, see run.sh.
cd /go/src/github.com/{pr.org}/{pr.repo}
timeout 900 go build -tags=unit github.com/{pr.org}/{pr.repo}/... || true
timeout 900 go test -count=1 -vet=off -tags=unit -run ZZZ_WARM_CACHE_ONLY -exec /bin/true github.com/{pr.org}/{pr.repo}/... || true

# The module cache is deliberately NOT pre-warmed. Warming it would mean writing
# the require list from the PR's go.mod into this image, which is the gold fix
# patch's own content -- an agent solving this instance could read the answer out
# of the container. The fix stage downloads its modules over the network, against
# the go.sum the patch supplies.

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /go/src/github.com/{pr.org}/{pr.repo}
# The raw log goes to a file and the qualified rewrite is what reaches the
# harness. Streaming the raw output as well (`tee`) would put both the bare and
# the qualified spelling of every test in front of parse_log, doubling the
# result set. `status` preserves the exit code `go test` returned.
set +e
# `-vet=off` is load-bearing, not tidy-up. Since Go 1.10 `go test` runs a
# subset of `go vet` first and a diagnostic there is a BUILD failure, not a test
# failure -- the package reports `[build failed]` and contributes no `--- PASS`
# or `--- FAIL` line at all. Go 1.15 added `stringintconv` to that subset, and
# this 2019 tree converts ints to strings directly in six places in
# parsers/manifest_parser_test.go and one in utils/managedannotations.go. On
# golang:1.16 that silently removes BOTH `parsers` and `utils` from every stage,
# and `parsers` is precisely where this PR's gold tests live. Observed: 33
# passing tests with `parsers [build failed]`, instead of the ~150 expected.
# The PR targets go 1.14, which predates the check, so turning it off restores
# the toolchain behaviour the tests were written against. Vet is not the graded
# signal here, and the flag is identical in all three stages.
go test -v -count=1 -p 1 -vet=off -timeout 900s -tags=unit github.com/{pr.org}/{pr.repo}/... > /tmp/go-test.log 2>&1
status=$?
set -e

cat /tmp/go-test.log

# `go test -v` names a test by function alone -- it never says which file
# declared it, because its unit of compilation is the package. parse_log needs
# the file to build the "<file>::<TestName>" id that report.py resolves against
# the patch file list (report.py:385-395), and parse_log only ever receives this
# log. So emit the one fact only this container knows and let parse_log join it.
#
# Appended BELOW the untouched `go test` output, never edited into it: the raw
# runner output stays byte-faithful, and `=== TESTFILE` cannot be mistaken for a
# result because parse_log only ever treats `--- PASS/FAIL/SKIP:` as one.
#
# Generated here rather than in prepare.sh so the map reflects the tree AFTER
# any patch is applied -- a patch that adds a test file is mapped too.
grep -rnoE "^func Test[A-Za-z0-9_]*[(]" --include="*_test.go" . 2>/dev/null \\
  | sed -E "s|^[.]/||; s|^(.+):[0-9]+:func (Test[A-Za-z0-9_]*)[(]|=== TESTFILE \\2 \\1|" \\
  | sort -u

exit "$status"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /go/src/github.com/{pr.org}/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
# The raw log goes to a file and the qualified rewrite is what reaches the
# harness. Streaming the raw output as well (`tee`) would put both the bare and
# the qualified spelling of every test in front of parse_log, doubling the
# result set. `status` preserves the exit code `go test` returned.
set +e
# `-vet=off` is load-bearing, not tidy-up. Since Go 1.10 `go test` runs a
# subset of `go vet` first and a diagnostic there is a BUILD failure, not a test
# failure -- the package reports `[build failed]` and contributes no `--- PASS`
# or `--- FAIL` line at all. Go 1.15 added `stringintconv` to that subset, and
# this 2019 tree converts ints to strings directly in six places in
# parsers/manifest_parser_test.go and one in utils/managedannotations.go. On
# golang:1.16 that silently removes BOTH `parsers` and `utils` from every stage,
# and `parsers` is precisely where this PR's gold tests live. Observed: 33
# passing tests with `parsers [build failed]`, instead of the ~150 expected.
# The PR targets go 1.14, which predates the check, so turning it off restores
# the toolchain behaviour the tests were written against. Vet is not the graded
# signal here, and the flag is identical in all three stages.
go test -v -count=1 -p 1 -vet=off -timeout 900s -tags=unit github.com/{pr.org}/{pr.repo}/... > /tmp/go-test.log 2>&1
status=$?
set -e

cat /tmp/go-test.log

# `go test -v` names a test by function alone -- it never says which file
# declared it, because its unit of compilation is the package. parse_log needs
# the file to build the "<file>::<TestName>" id that report.py resolves against
# the patch file list (report.py:385-395), and parse_log only ever receives this
# log. So emit the one fact only this container knows and let parse_log join it.
#
# Appended BELOW the untouched `go test` output, never edited into it: the raw
# runner output stays byte-faithful, and `=== TESTFILE` cannot be mistaken for a
# result because parse_log only ever treats `--- PASS/FAIL/SKIP:` as one.
#
# Generated here rather than in prepare.sh so the map reflects the tree AFTER
# any patch is applied -- a patch that adds a test file is mapped too.
grep -rnoE "^func Test[A-Za-z0-9_]*[(]" --include="*_test.go" . 2>/dev/null \\
  | sed -E "s|^[.]/||; s|^(.+):[0-9]+:func (Test[A-Za-z0-9_]*)[(]|=== TESTFILE \\2 \\1|" \\
  | sort -u

exit "$status"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /go/src/github.com/{pr.org}/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
# The raw log goes to a file and the qualified rewrite is what reaches the
# harness. Streaming the raw output as well (`tee`) would put both the bare and
# the qualified spelling of every test in front of parse_log, doubling the
# result set. `status` preserves the exit code `go test` returned.
set +e
# `-vet=off` is load-bearing, not tidy-up. Since Go 1.10 `go test` runs a
# subset of `go vet` first and a diagnostic there is a BUILD failure, not a test
# failure -- the package reports `[build failed]` and contributes no `--- PASS`
# or `--- FAIL` line at all. Go 1.15 added `stringintconv` to that subset, and
# this 2019 tree converts ints to strings directly in six places in
# parsers/manifest_parser_test.go and one in utils/managedannotations.go. On
# golang:1.16 that silently removes BOTH `parsers` and `utils` from every stage,
# and `parsers` is precisely where this PR's gold tests live. Observed: 33
# passing tests with `parsers [build failed]`, instead of the ~150 expected.
# The PR targets go 1.14, which predates the check, so turning it off restores
# the toolchain behaviour the tests were written against. Vet is not the graded
# signal here, and the flag is identical in all three stages.
go test -v -count=1 -p 1 -vet=off -timeout 900s -tags=unit github.com/{pr.org}/{pr.repo}/... > /tmp/go-test.log 2>&1
status=$?
set -e

cat /tmp/go-test.log

# `go test -v` names a test by function alone -- it never says which file
# declared it, because its unit of compilation is the package. parse_log needs
# the file to build the "<file>::<TestName>" id that report.py resolves against
# the patch file list (report.py:385-395), and parse_log only ever receives this
# log. So emit the one fact only this container knows and let parse_log join it.
#
# Appended BELOW the untouched `go test` output, never edited into it: the raw
# runner output stays byte-faithful, and `=== TESTFILE` cannot be mistaken for a
# result because parse_log only ever treats `--- PASS/FAIL/SKIP:` as one.
#
# Generated here rather than in prepare.sh so the map reflects the tree AFTER
# any patch is applied -- a patch that adds a test file is mapped too.
grep -rnoE "^func Test[A-Za-z0-9_]*[(]" --include="*_test.go" . 2>/dev/null \\
  | sed -E "s|^[.]/||; s|^(.+):[0-9]+:func (Test[A-Za-z0-9_]*)[(]|=== TESTFILE \\2 \\1|" \\
  | sort -u

exit "$status"

""".format(pr=self.pr),
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


@Instance.register("apache", "openwhisk-wskdeploy")
class OpenwhiskWskdeploy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenwhiskWskdeployImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Only the `--- STATUS:` lines are parsed. The package-level summary
        # (`FAIL\tgithub.com/apache/openwhisk-wskdeploy/parsers\t0.412s`) is
        # deliberately left alone: a pattern loose enough to catch it captures
        # the import path as if it were a test name, and an import path is not
        # stable across the three stages the way a test name is.
        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # `go test -v` prints only the function name, so on its own a result
        # line cannot be tied back to a file. Each run script appends one
        # `=== TESTFILE <name> <path>` line per top-level test, below the
        # untouched runner output; read those first and qualify every name to
        # "<repo-relative _test.go>::<TestName>".
        #
        # That id is what report.py resolves: `_test_name_matches_files`
        # (report.py:385-395) splits on "::" and matches the head against the
        # patch's file list. With a bare `TestFoo` there is no head to match,
        # `_file_matcher_can_hit` disables the file branch, and the
        # `_touched_by_fix_patch` guard -- the check that catches a fix patch
        # authoring its own test -- goes inert for Go, because its fallback
        # compares the name to a file's basename stem and `TestFoo` is never
        # `manifest_parser`.
        #
        # A name with no mapping is left bare rather than guessed. Bare is still
        # identical across all three stages, which is what R3 requires.
        re_testfile = re.compile(r"^=== TESTFILE (\S+) (\S+)$")
        test_files: dict[str, str] = {}
        for line in clean_log.splitlines():
            match = re_testfile.match(line.strip())
            if match:
                test_files[match.group(1)] = match.group(2)

        def qualify(test_name: str) -> str:
            root = test_name.split("/", 1)[0]
            path = test_files.get(root)
            return f"{path}::{test_name}" if path else test_name

        def get_base_name(test_name: str) -> str:
            # Collapse a Go subtest onto its parent: "TestX/case" -> "TestX".
            # Runs BEFORE qualify(), so the name is still bare here and the
            # rfind cannot stray into a file path.
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in clean_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(qualify(get_base_name(match.group(1))))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(qualify(get_base_name(match.group(1))))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(qualify(get_base_name(match.group(1))))

        # Subtests collapse onto their parent, so one parent can be reported
        # both ways. Failure wins, which also keeps the sets disjoint.
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
