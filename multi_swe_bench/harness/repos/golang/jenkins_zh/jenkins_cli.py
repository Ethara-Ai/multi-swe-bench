import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The module path declared by go.mod. Used to turn the import paths `go test`
# prints ("github.com/jenkins-zh/jenkins-cli/app/cmd") into the repository
# relative paths the ginkgo JUnit reports are keyed by ("app/cmd"), so both
# halves of a stage's log share one identifier namespace.
MODULE_PATH = "github.com/jenkins-zh/jenkins-cli"

# ---------------------------------------------------------------------------
# What this repository needs before `go test ./...` means anything
#
# 1. app/i18n/bindata.go. i18n.go calls the go-bindata generated Asset(), but
#    .gitignore keeps bindata.go out of the tree at every commit up to this PR
#    -- the project's CI runs `make gen-data-linux` first (.travis.yml,
#    .github/workflows/pull-request.yaml). Without that step app/i18n and
#    app/cmd do not compile, which would hide 170+ tests behind a build error at
#    the base commit. gen-bindata.sh reproduces it; the file stays gitignored,
#    so the working tree is still clean when the patches are applied.
#
# 2. java and vim on PATH. computer_launch.go resolves "java" through
#    centerStartOption.LookPathContext instead of its own option struct, so the
#    test's util.FakeLookPath never takes effect and the real exec.LookPath
#    runs; config_edit_test.go drives survey's editor prompt, which shells out
#    to $VISUAL/$EDITOR and falls back to vim.
#
# 3. A TMPDIR that survives the suite. cwp_test.go and doc_test.go both do
#    `dir := os.TempDir(); defer os.RemoveAll(dir)`, i.e. they delete the whole
#    of /tmp; every later test that wants a temp file then fails. See the
#    TMPDIR note in test-common.sh for how that is defused without touching the
#    repository.
#
# 4. The app/cmd ginkgo suite in its own process. TestDownload caches a
#    cwp-cli.jar in the temp directory and the suite's "cwp command test" spec
#    asserts that `jcli cwp` downloads that jar -- a cached copy short-circuits
#    the download, the mocked round trip is never made and gomock fails the spec
#    in its AfterEach. Splitting the run removes the ordering dependency.
#
# With those four in place the base commit is green (224 specs across six
# ginkgo suites, plus the standalone Test functions), the test patch alone
# leaves app/cmd unable to compile -- it references app/cmd/common and
# app/config, packages only the fix patch creates -- and the fix patch brings
# everything back to green.
# ---------------------------------------------------------------------------

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""

GEN_BINDATA_SH = """#!/bin/bash
set -e

# `make gen-data-linux` from the repository's own Makefile, minus the download
# of the go-bindata binary (the base image already installed the same v3.11.0
# release). app/i18n/i18n.go reads jcli/zh_CN/LC_MESSAGES/jcli.po through the
# generated Asset(), and .gitignore keeps the generated file out of the tree, so
# this has to run before anything compiles.
cd /home/{repo}/app/i18n
go-bindata -o bindata.go -pkg i18n jcli/zh_CN/LC_MESSAGES/

"""

TEST_COMMON_SH = """#!/bin/bash
# Shared test plumbing, sourced by prepare.sh, run.sh, test-run.sh and
# fix-run.sh so every stage executes exactly the same commands.

REPO_DIR="/home/{repo}"

setup_test_env() {{
    export CI=true

    # Pin module resolution. At the test-patch stage app/cmd legitimately does
    # not compile (it imports app/cmd/common, which only the fix patch adds);
    # -mod=mod would turn that into a network walk for a module named
    # "github.com/jenkins-zh/jenkins-cli/app/cmd/common" and could rewrite
    # go.mod. readonly fails fast, offline, and leaves the tree alone.
    export GOFLAGS="-mod=readonly"

    # Keep the go tool's scratch space out of TMPDIR. The tests below delete
    # their temp directory; the build work dir must not go with it.
    export GOTMPDIR="/go/tmp"

    # "/tmp/." rather than "/tmp". cwp_test.go's TestDownload and doc_test.go's
    # "doc command" spec both run
    #     dir := os.TempDir(); defer os.RemoveAll(dir)
    # which on Linux deletes /tmp outright, taking down every later test that
    # writes a temp file (open_test.go, job_artifact_download_test.go,
    # root_test.go, ...). os.RemoveAll rejects a path ending in "/." with EINVAL
    # before it touches the filesystem, so those two deletes become no-ops.
    # Every other consumer reaches the directory through path.Join /
    # filepath.Join, which cleans the "/." away -- the tests still read and
    # write exactly /tmp/<name>, and nothing in the repository is modified.
    export TMPDIR="/tmp/."

    mkdir -p "$GOTMPDIR"
    reset_tmp
}}

reset_tmp() {{
    rm -rf /tmp
    mkdir -p /tmp
    chmod 1777 /tmp
}}

clear_reports() {{
    # Every ginkgo suite here writes a JUnit file through
    # reporters.NewJUnitReporter. They are gitignored (*.xml), so a report left
    # behind by the image build -- or by a package that compiled in an earlier
    # stage but not in this one -- would otherwise be scored as this stage's
    # result.
    find "$REPO_DIR" -type f -name '*.xml' -delete 2>/dev/null || true
}}

run_all_tests() {{
    cd "$REPO_DIR"
    clear_reports

    # -p 1 keeps each package's block of `go test` output contiguous, so
    # parse_log can attribute every "--- PASS:" line to the package summary line
    # that closes the block. -ginkgo.seed pins the spec order ginkgo otherwise
    # seeds from the clock, so a stage is reproducible run to run.
    reset_tmp
    go test -v -count=1 -p 1 $(go list ./... | grep -v '/app/cmd$') -args -ginkgo.seed=1 || true

    # app/cmd runs in two passes: the ginkgo suite alone, on a pristine temp
    # directory, then everything else. TestDownload leaves a cwp-cli.jar behind
    # and the suite's "cwp command test" spec asserts the download happens, so
    # in a single process the outcome depends on which of the two ran first.
    reset_tmp
    go test -v -count=1 -p 1 ./app/cmd -run '^TestCmd$' -args -ginkgo.seed=1 || true

    reset_tmp
    local others
    others="$(go test -list '.*' ./app/cmd 2>/dev/null | grep -E '^(Test|Benchmark|Example|Fuzz)[A-Z_]' | grep -vx 'TestCmd' | paste -sd '|' -)"
    if [ -n "$others" ]; then
        go test -v -count=1 -p 1 ./app/cmd -run "^(${{others}})$" -args -ginkgo.seed=1 || true
    else
        # -list produced nothing: the package does not compile in this stage.
        # Run it anyway so the compiler diagnostics and the
        # "FAIL <pkg> [build failed]" line still reach the log.
        go test -v -count=1 -p 1 ./app/cmd -args -ginkgo.seed=1 || true
    fi
}}

print_test_results() {{
    cd "$REPO_DIR"

    # A ginkgo suite surfaces in `go test` output as a single Test function no
    # matter how many specs it holds; its JUnit report holds them individually.
    # Emitting both gives parse_log per-spec granularity on top of the
    # per-function and per-package results.
    echo "===== BEGIN TEST REPORTS ====="
    find . -type f -name '*.xml' -print | sort | while read -r f; do
        grep -q '<testsuite' "$f" 2>/dev/null || continue
        rel="${{f#./}}"
        echo "===== TEST REPORT: ${{rel%/*}} ====="
        cat "$f"
        echo ""
    done
    echo "===== END TEST REPORTS ====="
}}

"""

PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Reproduce `make gen-data-linux`; see gen-bindata.sh.
bash /home/gen-bindata.sh

source /home/test-common.sh
setup_test_env

# Warm the module and build caches for the base commit. `-run '^$'` compiles
# every test binary and runs no test, so nothing a test writes (a deleted /tmp,
# a cached cwp-cli.jar, a JUnit report) can leak into the image.
go build ./... || true
go test -count=1 -run '^$' ./... || true

# The same for the dependency set the fix patch introduces -- it adds
# ghodss/yaml, pkg/errors, gopkg.in/src-d/go-git.v4 and x/crypto to go.mod.
# Done in a throwaway copy, so the graded tree is never patched, only the shared
# module and build caches are populated, and fix-run.sh is not the first thing
# in the pipeline that needs the module proxy.
rm -rf /home/_warmup
cp -a /home/{repo} /home/_warmup
(
    cd /home/_warmup
    rm -f app/i18n/bindata.go
    git apply --whitespace=nowarn /home/test.patch /home/fix.patch
    go build ./...
    go test -count=1 -run '^$' ./...
) || true
rm -rf /home/_warmup

clear_reports
bash /home/check_git_changes.sh

"""

RUN_SH = """#!/bin/bash
set -o pipefail

# Deliberately no `set -e`: a stage has to run all three `go test` passes and
# print the JUnit reports even when a package fails, otherwise one failure would
# truncate the log and every result it swallowed would read as "this test never
# existed" instead of "this test failed".

source /home/test-common.sh
setup_test_env

run_all_tests
print_test_results

"""

TEST_RUN_SH = """#!/bin/bash
set -o pipefail

source /home/test-common.sh
setup_test_env

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}

run_all_tests
print_test_results

"""

FIX_RUN_SH = """#!/bin/bash
set -o pipefail

source /home/test-common.sh
setup_test_env

cd /home/{repo}
# The fix patch adds app/i18n/bindata.go as a new file, and prepare.sh already
# generated that same file (nothing compiles without it, and .gitignore hides it
# from git). It has to go first or `git apply` rejects the whole patch with
# "already exists".
rm -f app/i18n/bindata.go
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true; git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}

run_all_tests
print_test_results

"""


class JenkinsCliImageBase(Image):
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
        # go.mod declares `go 1.12` and the CI of the day built on 1.12/1.13,
        # but nothing in the tree needs an old toolchain: every package compiles
        # and every test passes on 1.23, and the module graph -- including the
        # go-git / x-crypto pair the fix patch adds -- resolves unchanged. The
        # bookworm suffix is the reason for the explicit tag: golang:1.16
        # through golang:1.19 sit on Debian bullseye, whose apt repositories are
        # at the end of their life, and this image needs a working apt for the
        # JRE and the editor below.
        return "golang:1.23-bookworm"

    # Scoped to the PR rather than a single shared ":base". The generated base
    # image clones and then prunes history down to one ${BASE_COMMIT}, so a
    # shared tag would be built from whichever PR happened to run first and a
    # later PR would silently inherit someone else's commit. Tagging per PR
    # makes the pair provable and lets prepare.sh check out its own base commit
    # with no re-fetch fallback.
    def image_tag(self) -> str:
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

        # Everything this image adds has to precede the clone line:
        # DockerfileEnhancer replaces that single line with the clone, the
        # checkout of ${BASE_COMMIT}, the history-hardening block and the
        # closing CMD, so anything written after it would land past the CMD.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    default-jre-headless \\
    vim-tiny \\
    && ln -sf /usr/bin/vim.tiny /usr/local/bin/vim \\
    && ln -sf /usr/bin/vim.tiny /usr/local/bin/vi \\
    && rm -rf /var/lib/apt/lists/*

RUN GOBIN=/usr/local/bin go install github.com/kevinburke/go-bindata/go-bindata@v3.11.0 \\
    && go clean -cache -modcache \\
    && go-bindata -version

{code}

{self.clear_env}

"""


class JenkinsCliImageDefault(Image):
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
        return JenkinsCliImageBase(self.pr, self._config)

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
                CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "gen-bindata.sh",
                GEN_BINDATA_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-common.sh",
                TEST_COMMON_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                PREPARE_SH.format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                RUN_SH,
            ),
            File(
                ".",
                "test-run.sh",
                TEST_RUN_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                FIX_RUN_SH.format(repo=self.pr.repo),
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


@Instance.register("jenkins-zh", "jenkins-cli")
class JenkinsCli(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JenkinsCliImageDefault(self.pr, self._config)

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
        # A stage log has two halves, split by the marker print_test_results
        # writes: the `go test` console output, then the ginkgo JUnit reports.
        # Both are parsed, because neither is sufficient alone -- the console
        # output is the only place a package that failed to compile is reported,
        # and the reports are the only place the individual specs of the six
        # ginkgo suites appear.
        report_marker = "===== BEGIN TEST REPORTS ====="
        marker_at = test_log.find(report_marker)
        if marker_at >= 0:
            console_log = test_log[:marker_at]
            reports_log = test_log[marker_at:]
        else:
            console_log = test_log
            reports_log = ""

        # FAIL outranks SKIP outranks PASS. One identifier can be reported by
        # more than one command -- app/cmd's package line is printed by both of
        # its passes -- and a failure anywhere is the honest verdict.
        rank = {"PASS": 0, "SKIP": 1, "FAIL": 2}
        status: dict[str, str] = {}

        def record(name: str, outcome: str) -> None:
            name = name.strip()
            if not name:
                return
            previous = status.get(name)
            if previous is None or rank[outcome] > rank[previous]:
                status[name] = outcome

        def package_id(import_path: str) -> str:
            if import_path == MODULE_PATH:
                return "."
            if import_path.startswith(MODULE_PATH + "/"):
                return import_path[len(MODULE_PATH) + 1 :]
            return import_path

        ansi_re = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")

        # "--- PASS: TestCmd (0.08s)", and the indented form subtests use.
        test_line_re = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+)")
        # The line that closes a package's block: "ok <pkg> 0.11s",
        # "FAIL <pkg> [build failed]", "? <pkg> [no test files]". The bare
        # trailing "FAIL" go prints, and ginkgo's "FAIL!" banner, carry no
        # package and are excluded by the \\s+(\\S+) requirement.
        pkg_pass_re = re.compile(r"^ok\s+(\S+)")
        pkg_fail_re = re.compile(r"^FAIL\s+(\S+)")
        pkg_none_re = re.compile(r"^\?\s+(\S+)")

        # Test functions are buffered until the package summary line names the
        # package they belong to: `go test` never prints the package on the
        # "--- PASS:" line, and two packages here both define TestApp.
        pending: list[tuple[str, str]] = []

        def flush(import_path: str) -> None:
            prefix = package_id(import_path)
            for name, outcome in pending:
                record(f"{prefix}::{name}", outcome)
            pending.clear()

        for raw_line in console_log.splitlines():
            line = ansi_re.sub("", raw_line).strip()

            match = test_line_re.match(line)
            if match:
                pending.append((match.group(2), match.group(1)))
                continue

            match = pkg_pass_re.match(line)
            if match:
                flush(match.group(1))
                record(package_id(match.group(1)), "PASS")
                continue

            match = pkg_fail_re.match(line)
            if match:
                flush(match.group(1))
                record(package_id(match.group(1)), "FAIL")
                continue

            if pkg_none_re.match(line):
                # "? <pkg> [no test files]": nothing compiled, nothing ran and
                # nothing to record. `pending` is deliberately left alone -- it
                # belongs to whichever package has not printed its summary line
                # yet, and dropping it would silently lose that package's
                # results if go ever interleaved the two blocks.
                continue

        # The ginkgo JUnit reports, one section per suite, keyed by the
        # repository relative directory print_test_results announced -- the
        # directory rather than the report's own classname attribute, because
        # the app and app/i18n suites are both registered under the name "app".
        section_re = re.compile(r"===== TEST REPORT: (.+?) =====")
        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        name_re = re.compile(r'\bname="([^"]*)"')

        sections = section_re.split(reports_log)
        for index in range(1, len(sections) - 1, 2):
            directory = sections[index].strip()
            body = sections[index + 1]
            for match in testcase_re.finditer(body):
                name_match = name_re.search(match.group(1))
                if not name_match:
                    continue
                spec = f"{directory}::{name_match.group(1)}"
                closing = match.group(2)
                inner = match.group(3) or ""
                if closing == "/>":
                    record(spec, "PASS")
                elif "<failure" in inner or "<error" in inner:
                    record(spec, "FAIL")
                elif "<skipped" in inner:
                    record(spec, "SKIP")
                else:
                    record(spec, "PASS")

        passed_tests = {name for name, s in status.items() if s == "PASS"}
        failed_tests = {name for name, s in status.items() if s == "FAIL"}
        skipped_tests = {name for name, s in status.items() if s == "SKIP"}

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
