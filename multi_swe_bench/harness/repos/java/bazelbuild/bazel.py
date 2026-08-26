import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BazelImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        # Tagged `base-pr-<number>`, not a shared `base`. Because dependency()
        # returns a *string*, DockerfileEnhancer owns this file and rewrites the
        # clone below into clone + `git checkout ${BASE_COMMIT}` + the history
        # scrub, which bakes ONE base commit into the image. A shared `base` tag
        # would therefore stay pinned to whichever PR built it first, and every
        # later PR whose base commit is unreachable from that sha would die in
        # the scrub's `rev-parse HEAD` assertion (R4, R10). It is also what the
        # Dockerfile QC contract requires, so the PR layer can inherit
        # `mswebench/<org>_m_<repo>:base-pr-<N>` (P1). Costs one base image per
        # PR instead of one per repo; deliberate.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def _get_jdk_version(self) -> str:
        ref = self.pr.base.ref
        if ref == "master":
            return "21"

        m = re.match(r"release-(\d+)\.", ref)
        if m:
            major = int(m.group(1))
            if major >= 8:
                return "21"
            elif major >= 7:
                return "17"
            else:
                return "11"

        return "21"

    def _get_bazelisk_setup(self) -> str:
        return textwrap.dedent("""\
            RUN ARCH=$(dpkg --print-architecture) \\
                && curl -fSsL -o /usr/local/bin/bazel https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-${ARCH} \\
                && chmod +x /usr/local/bin/bazel""")

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        jdk_version = self._get_jdk_version()
        bazelisk_setup = self._get_bazelisk_setup()

        # The emitted text below carries NO comments on purpose. The rendered
        # base Dockerfile is a client-facing artifact reviewed against a fixed
        # reference layout, and that reference is bare instructions only, so
        # every explanation lives here in the config instead of being baked into
        # the artifact. Section order matches that reference exactly:
        #
        #   syntax -> FROM -> ARGs -> ENV -> LABEL -> CA farm   (all injected by
        #   DockerfileEnhancer) -> WORKDIR /home/ -> apt -> JDK symlink ->
        #   bazelisk -> user+chown -> USER -> clone -> WORKDIR repo ->
        #   reset/checkout -> history scrub -> submodule scrub -> CMD
        #
        # Only two slots are ours, and each is the per-language slot the QC
        # appendix allows for a Java/bare-OS base:
        #
        # 1. NO ENV instruction at all. The reference layout carries exactly one
        #    ENV block -- the one DockerfileEnhancer injects -- and this file adds
        #    none, so the rendered base matches it instruction for instruction.
        #    JAVA_HOME and LC_ALL are still set, but as exports in prepare.sh and
        #    the three run scripts, which is where they are actually consumed:
        #    nothing in THIS image ever runs Java. The base only apt-installs,
        #    symlinks the JDK path, downloads bazelisk and clones. Every java and
        #    bazel invocation happens later, in the PR layer's prepare.sh and in
        #    the graded stages, and an export in those scripts reaches bazel and
        #    its children exactly as an inherited ENV would. Behaviour identical,
        #    one fewer instruction in the artifact.
        # 2. The apt line. ca-certificates is listed explicitly rather than
        #    relied on as a transitive dependency of curl, because D10 treats
        #    git + ca-certificates as the CRITICAL minimum and a bare ubuntu base
        #    guarantees neither. The rest is the toolchain this repo needs: JDK to
        #    compile, build-essential for Bazel's native bits, zip/unzip for its
        #    embedded tooling, python3 for the build scripts.
        #
        #    `file` is not optional and is easy to miss, because nothing fails to
        #    build without it -- a test fails, several layers down, with output
        #    that looks like a broken assertion rather than a missing package.
        #    Bazel's own test runner shells out to it to fill the mime column of
        #    the undeclared-outputs manifest (tools/test/test-setup.sh:331):
        #
        #        file_type="$(file -L -b --mime-type "$undeclared_output" || ...)"
        #
        #    With `file` absent that column comes out empty, so the manifest reads
        #        deeply/nested/index.html	16
        #    instead of
        #        deeply/nested/index.html	16	text/html
        #    and //src/test/shell/bazel:bazel_test_test fails two cases,
        #    test_undeclared_outputs_are_zipped and _are_not_zipped, in 2 of its 3
        #    shards. That failure reproduces identically at the run and fix
        #    stages, so it never touched the f2p signal -- it just meant the fix
        #    stage could never report a clean pass. Measured: with `file`
        #    installed the same target goes to PASSED in 20.7s.
        # 3. bazeluser. Bazel refuses to run as root, so the build user is
        #    created, handed /home/, and switched to BEFORE the clone. That
        #    ordering is load-bearing:
        #    DockerfileEnhancer._standardize_repo_fetch replaces the clone line
        #    with clone + WORKDIR + reset + checkout ${BASE_COMMIT} + the
        #    history-scrub block + CMD ["/bin/bash"], so anything emitted after
        #    the clone lands after that CMD. The chown and USER switch used to
        #    sit there, leaving the rendered base ending on a stray RUN/USER pair
        #    instead of the CMD the contract requires (D16, D17). Going first
        #    also means clone, checkout and scrub all run as bazeluser inside a
        #    bazeluser-owned tree, so git never raises "detected dubious
        #    ownership" (R13) and no safe.directory workaround is needed.
        #
        # The clone must stay the LAST thing this method emits.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git ca-certificates curl openjdk-{jdk_version}-jdk build-essential zip unzip python3 file \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-{jdk_version}-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-{jdk_version}-openjdk

{bazelisk_setup}

RUN groupadd -r bazeluser && useradd -r -g bazeluser -m -d /home/bazeluser bazeluser \\
    && chown -R bazeluser:bazeluser /home/

USER bazeluser

{code}

{self.clear_env}

"""


class BazelImageDefault(Image):
    # No per-PR tables live here. Everything below is derived from the pull
    # request itself — the patch contents, the base ref, and the checked-out
    # tree — so a new PR needs no entry anywhere to be supported.

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
        return BazelImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _get_bazel_version_for_ref(self) -> str:
        ref = self.pr.base.ref
        m = re.match(r"release-(\d+)\.(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

        m = re.match(r"release-(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.0"

        # Non-standard branches: try to extract version from branch name
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

        return "last_green"

    @staticmethod
    def _find_build_dirs(*patches: str) -> set[str]:
        dirs: set[str] = set()
        for patch in patches:
            for m in re.finditer(r"diff --git a/(.+?) b/(.+)", patch):
                path = m.group(2)
                basename = path.rsplit("/", 1)[-1] if "/" in path else path
                if basename in ("BUILD", "BUILD.bazel"):
                    pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
                    dirs.add(pkg_dir)
        return dirs

    @staticmethod
    def _likely_subdir(pkg_dir: str, parent_dir: str, build_dirs: set[str]) -> bool:
        if not parent_dir:
            return False
        if "/test/py/" in pkg_dir:
            return True
        if "/testdata/" in pkg_dir:
            return True
        if pkg_dir.endswith("/testdata"):
            return True
        if pkg_dir.endswith("/bin"):
            return True
        if parent_dir in build_dirs:
            return True
        return False

    def _extract_test_targets(self) -> str:
        all_build_dirs = self._find_build_dirs(self.pr.test_patch, self.pr.fix_patch)

        test_files: list[str] = []
        for m in re.finditer(r"diff --git a/(.+?) b/(.+)", self.pr.test_patch):
            path = m.group(2)
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if "/test/" not in path and "/javatests/" not in path:
                continue
            # Production sources also live under directories literally named
            # "test" (e.g. src/main/java/.../analysis/test/TestStrategy.java).
            # Those hold no test rules, so deriving a target from them only
            # costs a build and an empty-pattern warning.
            if path.startswith("src/main/"):
                continue
            if basename in ("BUILD", "BUILD.bazel"):
                continue
            test_files.append(path)

        if not test_files:
            return "//src/test/..."

        targets: set[str] = set()

        for path in test_files:
            pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
            basename = path.rsplit("/", 1)[-1]
            stem = basename.rsplit(".", 1)[0] if "." in basename else basename

            if pkg_dir in all_build_dirs:
                targets.add(f"//{pkg_dir}/...")
                continue

            parent = pkg_dir
            found_parent_pkg = False
            while "/" in parent:
                parent = parent.rsplit("/", 1)[0]
                if parent in all_build_dirs:
                    targets.add(f"//{parent}:{stem}")
                    found_parent_pkg = True
                    break

            if found_parent_pkg:
                continue

            parent_dir = pkg_dir.rsplit("/", 1)[0] if "/" in pkg_dir else ""
            if self._likely_subdir(pkg_dir, parent_dir, all_build_dirs):
                targets.add(f"//{parent_dir}:{stem}")
            elif basename.endswith(".sh"):
                # Shell tests are the one case where the package wildcard is
                # never affordable: a single sh_test package here holds ~112
                # targets, each bootstrapping a full Bazel from source, so
                # expanding one touched script into `//pkg/...` costs hours and
                # drowns the run in unrelated integration failures. sh_test
                # targets are conventionally named for their script stem, so
                # address the touched script directly instead.
                targets.add(f"//{pkg_dir}:{stem}")
            else:
                targets.add(f"//{pkg_dir}/...")

        if not targets:
            return "//src/test/..."

        return " ".join(sorted(targets))

    def files(self) -> list[File]:
        jdk_version = BazelImageBase(self.pr, self._config)._get_jdk_version()
        test_targets = self._extract_test_targets()
        bazel_version_pin = self._get_bazel_version_for_ref()

        # The graded command, built ONCE and interpolated into all three run
        # scripts, so they cannot drift apart (R3, P7).
        #
        # --build_event_json_file is the load-bearing addition. Bazel's terminal
        # summary is TRUNCATED: TerminalTestResultNotifier.java hardcodes
        #
        #     @VisibleForTesting public static final int NUM_FAILED_TO_BUILD = 5;
        #
        # and prints "(Skipping other failed to build tests)" past it. No flag
        # raises that cap. On this PR the test stage had SEVEN targets fail to
        # build and the console named only five, so the two it dropped read as
        # absent (NONE) instead of failed -- and one of them,
        # StdoutInfoItemHandlerTest, is a genuine FAIL->PASS that was therefore
        # mis-bucketed as p2p, under-reporting f2p by one. The Build Event
        # Protocol is Bazel's machine-readable stream and is never truncated;
        # measured on the same stage it reports all 7 targets.
        #
        # Emitting a compact digest rather than cat-ing the file: the raw BEP for
        # this one command is ~568 KB of mostly progress events, which would
        # bloat every stage log for no gain.
        #
        # targetCompleted carries build success and is what covers a target that
        # never ran; testSummary carries the run status and is authoritative when
        # present. setdefault on the former plus direct assignment on the latter
        # gives testSummary precedence regardless of the order events arrive in.
        #
        # This also structurally kills the nested-Bazel phantom: the inner Bazel
        # that //src/test/shell/bazel:bazel_test_test spawns writes no BEP of
        # ours, so its scratch targets (//dir:test) can no longer leak into the
        # results the way they did through the shared console stream.
        bep_digest = """python3 - <<'PYEOF'
import json
status = {}
try:
    fh = open('/tmp/bep.json')
except OSError:
    fh = []
for line in fh:
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    ident = ev.get('id') or {}
    tc = ident.get('targetCompleted')
    if tc and 'completed' in ev:
        ok = (ev.get('completed') or {}).get('success', False)
        status.setdefault(tc.get('label', ''), 'BUILT' if ok else 'FAILED TO BUILD')
    ts = ident.get('testSummary')
    if ts:
        status[ts.get('label', '')] = (ev.get('testSummary') or {}).get('overallStatus', 'NO_STATUS')
print('===BEP BEGIN===')
for label in sorted(status):
    if label:
        print(status[label], label)
print('===BEP END===')
PYEOF"""

        test_cmd = (
            "rm -f /tmp/bep.json\n"
            f"bazel test {test_targets}"
            " --build_tests_only --test_output=errors --test_tag_filters=-manual"
            " --test_timeout=600 --keep_going --jobs=6"
            " --build_event_json_file=/tmp/bep.json 2>&1\n"
            f"{bep_digest}"
        )

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

# Set here rather than as ENV in the base image, so the rendered base Dockerfile
# carries no ENV instruction of its own and matches the reference layout exactly.
# This is the first place either variable is actually needed -- the base image
# never runs java or bazel. An export reaches bazel and every child process it
# spawns exactly as an inherited ENV would.
export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk
export LC_ALL=C.UTF-8

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

if [ ! -f .bazelversion ]; then
  echo "{bazel_version}" > .bazelversion
fi

# Warm the Bazel cache into the image so each of the three run stages does not
# recompile the tree from scratch. Never fatal: a cold cache only costs time.
bazel version || true
bazel build {test_targets} --noshow_progress 2>&1 || true
""".format(pr=self.pr, bazel_version=bazel_version_pin, test_targets=test_targets, jdk_version=jdk_version),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -o pipefail

export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk
export LC_ALL=C.UTF-8

cd /home/{pr.repo}
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=test_cmd, jdk_version=jdk_version),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail

export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk
export LC_ALL=C.UTF-8

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=test_cmd, jdk_version=jdk_version),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail

export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk
export LC_ALL=C.UTF-8

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=test_cmd, jdk_version=jdk_version),
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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                RUN mkdir -p /home/{self.pr.repo} && \\
                    cat > /home/{self.pr.repo}/.bazelrc.user <<'BAZELRC'
startup --host_jvm_args=-Dhttp.proxyHost={proxy_host} --host_jvm_args=-Dhttp.proxyPort={proxy_port}
startup --host_jvm_args=-Dhttps.proxyHost={proxy_host} --host_jvm_args=-Dhttps.proxyPort={proxy_port}
BAZELRC
                """
                )

                proxy_cleanup = textwrap.dedent(
                    f"""
                    RUN rm -f /home/{self.pr.repo}/.bazelrc.user
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("bazelbuild", "bazel")
class Bazel(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BazelImageDefault(self.pr, self._config)

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
        target_status: dict[str, str] = {}

        # Preferred source: the Build Event Protocol digest the run scripts emit
        # between ===BEP BEGIN===/===BEP END===. Bazel's terminal summary caps the
        # failed-to-build list at NUM_FAILED_TO_BUILD = 5 and prints "(Skipping
        # other failed to build tests)", so on a stage where more than five
        # targets fail to compile the console is provably incomplete -- the
        # dropped names appear NOWHERE in the log and cannot be recovered from it.
        # The BEP stream is not truncated.
        #
        # The console block below is kept as a fallback for any log captured
        # before the scripts emitted BEP, so old logs still parse.
        bep_block = re.search(
            r"===BEP BEGIN===\n(.*?)===BEP END===", test_log, re.DOTALL
        )
        if bep_block:
            re_bep = re.compile(
                r"^(FAILED TO BUILD|NO_STATUS|NO STATUS|PASSED|FAILED|TIMEOUT|FLAKY|"
                r"INCOMPLETE|BUILT)\s+(//\S+)$"
            )
            for line in bep_block.group(1).splitlines():
                m = re_bep.match(line.strip())
                if not m:
                    continue
                status, target = m.group(1), m.group(2)
                if status == "BUILT":
                    # Built but no testSummary: nothing was executed for it, so
                    # it contributes no result rather than a false pass.
                    continue
                target_status[target] = status.replace("NO_STATUS", "NO STATUS")

        # "FAILED TO BUILD" is how Bazel reports a test target whose sources do
        # not compile — the normal shape of the test stage, where the gold tests
        # reference an API the fix patch has not introduced yet. It carries no
        # "in <n>s" duration, so the duration suffix has to stay optional or the
        # target reads as absent (NONE) instead of failing, and a genuine f2p
        # gets misclassified as p2p. Longest alternatives first.
        # `... in <n> out of <m>` is the retry/flaky summary Bazel prints when a
        # target ran more than once (`--flaky_test_attempts`, `--runs_per_test`):
        #
        #     //src/test/shell/bazel:bazel_test_test FAILED in 2 out of 3 in 107.8s
        #
        # Without this alternative the line does not match at all, so a target
        # that genuinely FAILED is recorded as absent (NONE) rather than failed --
        # a silently swallowed failure, which is what makes a real transition
        # invisible (R3, R8). Optional and placed before the duration, because the
        # plain single-run form has no such clause.
        re_test_result = re.compile(
            r"^(//\S+)\s+"
            r"(?:\(cached\)\s+)?"
            r"(FAILED TO BUILD|NO STATUS|PASSED|FAILED|TIMEOUT|FLAKY|INCOMPLETE)"
            r"(?:,\s+passed\s+\d+/\d+)?"
            r"(?:\s+in\s+\d+\s+out\s+of\s+\d+)?"
            r"(?:\s+in\s+[\d.]+s)?\s*$",
            re.MULTILINE,
        )

        # Fallback only. Skipped entirely when the BEP digest was present, since
        # the console stream is both truncated (NUM_FAILED_TO_BUILD) and polluted
        # by nested Bazel servers, and mixing the two sources would let the weaker
        # one overwrite the authoritative one.
        for match in (
            re_test_result.finditer(test_log) if not target_status else ()
        ):
            target = match.group(1)

            # Only this repo's own targets. Bazel's shell integration tests run a
            # nested Bazel inside a scratch workspace, and that inner server
            # prints its own result lines into our log:
            #
            #     //dir:test PASSED in 2.1s
            #     //dir:test FAILED in 1 out of 2 in 0.0s
            #
            # `//dir:test` is not a target of this build -- it belongs to a
            # throwaway workspace created by //src/test/shell/bazel:bazel_test_test.
            # Counting it is wrong twice over: it invents a test id that does not
            # exist in the repo, and it is nondeterministic, appearing 4 times in
            # the run stage, 0 times in the test stage (the suite failed to build,
            # so the shell test never ran) and 5 times with mixed statuses in the
            # fix stage. That NONE at the test stage flanked by run=PASS and a
            # possible fix=FAILED is precisely check()'s Rule 4 anomalous pattern,
            # which rejects the whole report -- so leaving the phantom in makes a
            # valid instance depend on which status the inner Bazel happened to
            # print last. Every real target of this repo is rooted at //src/.
            if not target.startswith("//src/"):
                continue

            status = match.group(2)

            # Failure wins when one target reports more than once, instead of
            # last-line-wins. Keeps the mapping order-independent, so the three
            # stages cannot disagree merely because Bazel emitted the lines in a
            # different order (R2, R3).
            if target_status.get(target, "PASSED") != "PASSED":
                continue
            target_status[target] = status

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for target, status in target_status.items():
            if status in ("PASSED", "FLAKY"):
                passed_tests.add(target)
            elif status in ("FAILED", "TIMEOUT", "INCOMPLETE", "FAILED TO BUILD"):
                failed_tests.add(target)
            elif status == "NO STATUS":
                skipped_tests.add(target)

        if not passed_tests and not failed_tests:
            re_junit = re.compile(
                r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
                r"\s*Skipped:\s*(\d+),\s*Time elapsed:\s*[\d.]+\s*s(?:ec)?"
                r".*?(?:in\s+(\S+)|$)",
                re.MULTILINE,
            )
            for match in re_junit.finditer(test_log):
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5) if match.group(5) else f"test_suite_{match.start()}"

                if tests_run > 0 and failures == 0 and errors == 0 and skipped != tests_run:
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
