"""Repo config for spring-projects/spring-framework (Java / Gradle / JUnit 5).

Runner
------
Spring Framework is a ~25-module Gradle build (``settings.gradle`` at
``2f32806bcb6c`` lists ``spring-aop`` ... ``integration-tests``). Tests are JUnit
5 executed by Gradle's own ``Test`` task.

Why the graded command is scoped
--------------------------------
``./gradlew test`` on the root project builds and runs *every* module. For a PR
whose patches touch one module that is an hour-plus of unrelated work per stage,
executed three times, and every unrelated flake it picks up is a candidate
``PASS -> FAIL`` that ``Report.check()`` rule 2 rejects.

``_gradle_test_tasks`` therefore derives the task list from the patches
themselves: every ``diff --git a/<segment>/...`` whose leading segment is a real
Gradle subproject becomes ``:<segment>:test``. PR 27735 patches only
``spring-tx/src/main/...`` and ``spring-tx/src/test/...``, so both graded stages
run ``:spring-tx:test``. Gradle resolves the inter-project dependencies
(``spring-tx`` needs ``spring-core``, ``spring-beans``, ``spring-aop``,
``spring-context``) on its own, so scoping the *task* does not under-build the
*classpath*. When no segment looks like a subproject the list falls back to the
root ``test`` task, so an unusual PR degrades to the old behaviour rather than
running nothing.

The derivation reads the same two patch strings in all three scripts, so the
command is identical across ``run`` / ``test`` / ``fix`` by construction --
the only thing that varies between the stages is which patch was applied.

Test identity comes from an init script, not from Gradle's console
------------------------------------------------------------------
This is the part that matters most. Gradle's default console reports **only
failures** at test granularity; a passing test prints nothing at all. Parsing
the console therefore cannot see a pass, and the previous revision of this file
worked around that by keying ``parse_log`` on ``> Task :<name>`` lines -- i.e.
the "tests" in the dataset were Gradle *task* names such as
``spring-tx:compileJava``. The gold test would show up as ``FAILED`` in the test
stage and then vanish (``NONE``) in the fix stage, because a *passing* test is
never printed. The f2p set was task names, not tests.

``msb-test-logging.gradle`` is passed with ``-I`` and registers an ``afterTest``
listener on every ``Test`` task, emitting one line per test result:

    MSB-TEST|spring-tx/src/test/java/org/springframework/dao/support/DataAccessUtilsTests.java|org.springframework.dao.support.DataAccessUtilsTests|withEmptyCollection()|SUCCESS

``parse_log`` keys on ``<source path> > <class> > <method>``.

The leading field is the test's own source file, resolved from the project's
test source set, and it is load-bearing rather than decorative.
``Report._test_name_matches_files`` resolves a name to a patched file only when
the name *starts with* that path, and ``Report.check`` rule 5 names
"Gradle-style IDs that never map to a file path" as precisely the case that
disables its tamper guard. Keying on a bare project path or a bare FQCN leaves
``_test_patch_matcher_ok`` false, which makes ``_touched_by_test_patch`` return
``True`` for every test and switches the guard off for every instance of this
repo. The path also makes the name unique across a ~25-module build, which a
bare FQCN is not guaranteed to be.

An init script is used rather than editing ``build.gradle`` because the tree
must stay clean for ``check_git_changes.sh`` and for the ``git apply`` in the
run scripts.

The same init script sets two things the graded stages depend on:

* ``outputs.upToDateWhen { false }`` -- a ``Test`` task replayed ``UP-TO-DATE``
  or ``FROM-CACHE`` does not fire ``afterTest``, and would report zero tests.
  ``org.gradle.caching=true`` is set in the repo's own ``gradle.properties``, so
  this is a live hazard, not a theoretical one. ``--no-build-cache`` on the
  command line closes the other half.
* ``ignoreFailures = true`` -- a failing test is the *expected* outcome of the
  test stage. Without this Gradle exits non-zero, and under ``set -eo pipefail``
  the stage would die before printing anything.

Compile failure in the test stage is expected and is not an error
-----------------------------------------------------------------
The gold test calls ``DataAccessUtils.singleResult`` / ``optionalResult``, which
the fix patch introduces. In the test stage ``compileTestJava`` therefore fails
and ``:spring-tx:test`` never runs -- every test in the module reports ``NONE``
for that stage. That is a valid report: rule 1 holds (the fix stage produces
results), rule 3 holds (``NONE -> PASS`` is a ``!PASS -> PASS`` transition), and
rules 2 and 4 cannot trigger because nothing fails in the fix stage. It does
widen f2p to the module's whole suite, which is inherent to a statically
compiled language whose gold test references a not-yet-existing API.

What it must *not* do is report ``0/0/0`` for a stage because Gradle never
started. The runner-start guarantee below separates the two: Gradle always
prints a build-result line once it is running, whatever the outcome, so its
absence means the wrapper, the JDK, or the plugin stripping is broken and the
stage fails loudly.

Gradle bootstrap hazards handled in prepare.sh
----------------------------------------------
* ``settings.gradle`` at this commit declares ``com.gradle.enterprise 3.7.2``
  and ``io.spring.ge.conventions 0.0.7``, resolved from
  ``https://repo.spring.io/plugins-release``, which now answers 401. Both plugin
  lines and the trailing ``settings.gradle.projectsLoaded { gradleEnterprise
  {...} }`` block are stripped -- they only publish build scans and have no
  effect on compilation or test execution. The trailing block has to go with the
  plugin lines: leaving it behind fails evaluation with "Could not find method
  gradleEnterprise()".
* The wrapper is ``gradle-7.3`` here, so the 4.x/5.x/6.x upgrade rules are
  inert for this PR. They are kept for the older eras this config also serves.
* The ``sed`` edits deliberately run *after* the clean-tree assertions, because
  they must persist into the graded stages. They leave ``settings.gradle`` and
  ``build.gradle`` modified; no Spring gold patch touches those files, so the
  ``git apply`` in the run scripts is unaffected.
* ``-Dorg.gradle.java.installations.auto-download=false`` stops Gradle from
  trying to fetch a toolchain JDK mid-stage. ``gradle/toolchains.gradle``
  defaults to language level 17 and the base image installs Zulu 17, so nothing
  legitimate needs downloading.

JDK selection
-------------
``_select_jdk`` routes on the base branch first and the PR number second. PR
27735 has ``base.ref == "main"`` (Spring Framework 6.0.0-SNAPSHOT,
``gradle/toolchains.gradle`` -> ``JavaLanguageVersion.of(17)``), so it takes the
JDK 17 base.

Base images are tagged ``base-pr-<N>`` rather than a shared ``base``: a shared
tag is rewritten by every other instance of this repo, silently changing the
foundation an already-verified instance was built against.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Leading path segments that appear in a Spring Framework diff but are not
# Gradle subprojects. Everything else that looks like a directory is treated as
# one; `settings.gradle` cannot be read at config time.
_NON_PROJECT_DIRS = {
    "gradle",
    "buildSrc",
    "ci",
    "src",
    "framework-docs",
    "framework-platform",
    ".github",
    ".idea",
}

_DIFF_PATH = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def _gradle_test_tasks(pr: PullRequest) -> list[str]:
    """Derive the graded Gradle test tasks from the patched paths.

    Identical inputs in all three stages -- both patch strings come straight off
    the PullRequest -- so the command cannot differ between run/test/fix.
    """
    projects: set[str] = set()
    for patch in (pr.fix_patch or "", pr.test_patch or ""):
        for old, new in _DIFF_PATH.findall(patch):
            for path in (old, new):
                head = path.split("/", 1)[0]
                if "/" not in path or head in _NON_PROJECT_DIRS or "." in head:
                    continue
                projects.add(head)

    if not projects:
        # Unrecognised layout: run the whole build rather than nothing.
        return ["test"]
    return [f":{name}:test" for name in sorted(projects)]


# Registered on every `Test` task through `-I`. Kept out of the repo tree on
# purpose: editing build.gradle would dirty the working tree that
# check_git_changes.sh asserts on and that `git apply` writes into.
#
# Contains no backslashes and no Python format placeholders, so it is written
# to disk byte-for-byte as authored.
_TEST_LOGGING_INIT = """\
// Injected by the multi-swe-bench harness. Not part of the repository.
//
// Gradle's console prints a line for a FAILED test and nothing at all for a
// passing one, so console scraping cannot produce a pass/fail set. This
// listener emits one machine-readable line per test result instead:
//
//   MSB-TEST|<test source path, relative to the repo root>|<class>|<method>|<result>
//
// The leading field is the test's own source file, resolved from the project's
// test source set. Report._test_name_matches_files keys the test-patch and
// fix-patch matchers on a name that starts with that path, and
// Report.check rule 5 calls out "Gradle-style IDs that never map to a file
// path" as exactly the blind spot that disables its tamper guard. A bare
// project path would leave that guard silently switched off for every instance
// of this repo.
// Test sources the stage script has proved cannot compile against this stage's
// tree (see the retry loop in run.sh/test-run.sh/fix-run.sh). Absolute paths,
// comma separated. Excluding them lets the rest of the module still compile and
// run; the excluded classes simply report NONE for that stage, which is the
// honest answer -- they genuinely do not exist in a buildable form yet.
def msbExcluded = []
if (gradle.startParameter.projectProperties.containsKey("msbExcludeTests")) {
    msbExcluded = gradle.startParameter.projectProperties.get("msbExcludeTests").split(",").findAll { it }
}

gradle.allprojects { proj ->
    if (msbExcluded) {
        proj.tasks.matching { it.name.startsWith("compileTest") }.all { ct ->
            ct.exclude { fte -> msbExcluded.contains(fte.file.absolutePath) }
        }
    }

    proj.tasks.withType(org.gradle.api.tasks.testing.Test) { t ->
        // A failing test is the expected outcome of the test stage. Letting
        // Gradle exit non-zero would kill the stage script before it prints.
        t.ignoreFailures = true

        // afterTest does not fire for a task replayed UP-TO-DATE or FROM-CACHE,
        // which would silently report zero tests. The repo sets
        // org.gradle.caching=true, so force execution in every stage.
        t.outputs.upToDateWhen { false }

        // One filesystem probe per class, not per test.
        def pathCache = [:]

        t.afterTest { desc, result ->
            def cls = desc.className ?: "unknown"

            def path = pathCache.get(cls)
            if (path == null) {
                path = ""
                // Nested classes live in the outer class's file.
                def top = cls.indexOf((int) 36) >= 0 ? cls.substring(0, cls.indexOf((int) 36)) : cls
                def rel = top.replace(".", "/")
                // sourceSets.test.allSource does not reliably carry the Kotlin
                // srcDirs in this build, so the conventional roots are appended
                // explicitly. Without them every Kotlin test falls back to the
                // project path and stops matching a patched file.
                def roots = []
                try { roots.addAll(proj.sourceSets.test.allSource.srcDirs) } catch (Throwable ignored) { }
                ["java", "kotlin", "groovy"].each { d ->
                    roots.add(new File(proj.projectDir, "src/test/" + d))
                }
                try {
                    roots.each { dir ->
                        if (path) return
                        ["java", "kotlin", "groovy"].each { ext ->
                            if (path) return
                            def candidate = new File(dir, rel + "." + ext)
                            if (candidate.isFile()) {
                                path = proj.rootDir.toPath()
                                        .relativize(candidate.toPath())
                                        .toString()
                                        .replace(File.separator, "/")
                            }
                        }
                    }
                } catch (Throwable ignored) {
                    // No java plugin, an exotic source set, or a class with no
                    // source file. Fall through to the project path: the name
                    // stays unique and stable, it just cannot be matched to a
                    // patched file.
                }
                if (!path) {
                    path = proj.path
                }
                pathCache.put(cls, path)
            }

            println("MSB-TEST|" + path + "|" + cls + "|" + desc.name + "|" + result.resultType)
        }
    }
}
"""


# Emits one FAILURE marker per test in a source file the stage could not
# compile. Plain constant -- no `.format()` -- so the awk/sed braces below need
# no escaping.
#
# A test whose source cannot compile has not "disappeared"; it has failed. That
# is what pytest reports natively for a Python module with an ImportError, and
# it is what makes the fail -> pass transition visible for a compiled language.
# Leaving the class out entirely made the whole module read as
# `run=PASS, test=NONE, fix=PASS`, which Report rule 6 files as Classic CBC
# (p2p) -- so a genuine f2p instance looked like it fixed nothing.
#
# Names come from the baseline JUnit XML that prepare.sh snapshots at the base
# commit, not from parsing Java, so they are Gradle's own strings and match the
# run/fix stages exactly. A test method that exists only in the test patch has
# no baseline entry and is simply absent (run=NONE, test=NONE, fix=PASS -> n2p),
# which is also the correct classification for it.
_REPORT_EXCLUDED = r"""#!/bin/bash
# usage: msb-report-excluded.sh <repo root> <excludes list> <baseline xml dir>
set -eo pipefail

ROOT="$1"
EXCLUDES="$2"
BASELINE="$3"

[ -s "$EXCLUDES" ] || exit 0

if [ ! -d "$BASELINE" ] || [ -z "$(ls -A "$BASELINE" 2>/dev/null)" ]; then
    echo "NOTE: no baseline test inventory; uncompilable tests report NONE, not FAILURE" >&2
    exit 0
fi

while read -r src; do
    [ -n "$src" ] || continue
    rel="${src#$ROOT/}"
    fqcn=$(printf '%s' "$rel" \
        | sed -E 's#^[^/]+/src/test/(java|kotlin|groovy)/##; s#\.(java|kt|groovy)$##; s#/#.#g')

    grep -ho '<testcase name="[^"]*" classname="[^"]*"' "$BASELINE"/*.xml 2>/dev/null \
        | sed -E 's#<testcase name="([^"]*)" classname="([^"]*)"#\2\t\1#' \
        | awk -F'\t' -v c="$fqcn" -v p="$rel" '
            BEGIN { n = 0 }
            ($1 == c) || (substr($1, 1, length(c) + 1) == c "$") {
                print "MSB-TEST|" p "|" $1 "|" $2 "|FAILURE"
                n++
            }
            END {
                if (n == 0) {
                    print "NOTE: no baseline tests recorded for " c > "/dev/stderr"
                }
            }'
done < "$EXCLUDES"
"""


# Shared by run.sh / test-run.sh / fix-run.sh.
#
# Identical in all three by construction: the only difference between the graded
# stages is which patch was applied before this block runs. Anything that varied
# the command itself would make a FAIL -> PASS transition attributable to the
# command rather than to the fix.
_TEST_BODY = """\
# `clean` is deliberately absent. prepare.sh already compiled this module and
# its inter-project dependencies at the base commit; cleaning would throw that
# away and rebuild spring-core/beans/aop/context from scratch in every stage.
# Gradle's input hashing recompiles exactly what `git apply` changed.
#
# ---------------------------------------------------------------------------
# Why this is a loop rather than one invocation.
#
# javac compiles a source set as a unit. One test file that does not compile
# takes the whole `compileTestJava` task down, `:<module>:test` never runs, and
# the stage reports 0/0/0 -- a blind stage that tells you nothing about the
# hundreds of tests that were perfectly fine.
#
# That is not hypothetical here: a gold test patch that exercises API the fix
# patch introduces cannot compile in the test stage by construction. Measured
# 2026-08-26 on PR 27735, the test stage reported 0 tests for exactly this
# reason while run and fix each reported 335.
#
# `options.failOnError = false` does NOT solve it -- measured the same day,
# javac abandons code generation on error and Gradle's incremental compiler had
# already deleted the previous outputs, leaving 1 class file out of ~300.
#
# So: run, read back which *test* source files javac rejected, exclude exactly
# those, and run again. Excluding cascades -- other tests import helper classes
# nested inside the rejected file -- so it iterates. On PR 27735 it converged in
# three rounds (DataAccessUtilsTests, then the two files importing its nested
# MapPersistenceExceptionTranslator, then the two importing *those*) and the
# stage reported 307 tests instead of 0.
#
# This does not weaken the signal, and it is not a per-stage command change in
# any sense that matters:
#   * The pattern only ever matches paths under a `/src/test/` directory, so a
#     broken *main* source can never be excluded -- that still fails the stage.
#   * Exclusion only ever *removes* a test (it reports NONE). It cannot make a
#     test pass, so no FAIL -> PASS transition can be manufactured by it.
#   * run and fix compile cleanly, so the loop exits after one iteration and
#     their command is byte-identical to the single-shot form.
#   * Every exclusion is printed. Nothing is dropped silently.
#
# The exit code is captured rather than allowed to propagate: a failing test is
# the expected outcome of the test stage, and `ignoreFailures` in the init
# script covers only test failures, not a compile failure. The harness grades
# from the log text, not from this status.
# ---------------------------------------------------------------------------
: > /tmp/msb-excludes.txt

for msb_attempt in 1 2 3 4 5; do
    rm -f /tmp/gradle-stage.log

    MSB_EXARGS=()
    if [ -s /tmp/msb-excludes.txt ]; then
        MSB_EXARGS=(-PmsbExcludeTests="$(paste -sd, /tmp/msb-excludes.txt)")
    fi

    set +e
    ./gradlew {tasks} \
        --no-daemon \
        --console=plain \
        --continue \
        --no-build-cache \
        --max-workers 4 \
        -I /home/msb-test-logging.gradle \
        "${{MSB_EXARGS[@]}}" \
        -Dorg.gradle.jvmargs="-Xmx3g -XX:MaxMetaspaceSize=768m" \
        -Dorg.gradle.java.installations.auto-download=false \
        > /tmp/gradle-stage.log 2>&1
    GRADLE_RC=$?
    set -e

    # Only ever /src/test/ paths: a broken main source must still fail loudly.
    grep -oE '/home/{repo}/[^:]*/src/test/[^:]*\\.(java|kt|groovy):[0-9]+: error' \
        /tmp/gradle-stage.log 2>/dev/null \
        | sed 's/:[0-9]*: error$//' | sort -u > /tmp/msb-broken.txt || true

    MSB_NEW=$(comm -13 /tmp/msb-excludes.txt /tmp/msb-broken.txt)
    if [ -z "$MSB_NEW" ]; then
        break
    fi

    printf '%s\\n' "$MSB_NEW" | while read -r msb_f; do
        [ -n "$msb_f" ] && echo "NOTE: test source will not compile against this stage's tree, excluding: $msb_f"
    done
    printf '%s\\n' "$MSB_NEW" >> /tmp/msb-excludes.txt
    sort -u -o /tmp/msb-excludes.txt /tmp/msb-excludes.txt
done

if [ -s /tmp/msb-excludes.txt ]; then
    echo "NOTE: $(wc -l < /tmp/msb-excludes.txt) test source file(s) did not compile this stage."
fi

# A test whose source will not compile has failed, not vanished. Append a
# FAILURE marker for each one so the stage accounts for the whole suite instead
# of silently shrinking, and so the fail -> pass transition is visible.
bash /home/msb-report-excluded.sh \
    /home/{repo} /tmp/msb-excludes.txt /home/msb-baseline-results \
    >> /tmp/gradle-stage.log

# parse_log reads stdout, so the captured build output has to land there.
cat /tmp/gradle-stage.log

# Runner-start guarantee. Gradle prints a build-result line once it is running,
# whatever the outcome -- BUILD SUCCESSFUL, BUILD FAILED, or a FAILURE banner.
# Its absence means the wrapper never downloaded, the JDK is wrong, or settings
# evaluation died, and the stage must fail loudly rather than hand parse_log an
# empty log that becomes a silent 0/0/0.
if ! grep -qE '^(BUILD SUCCESSFUL|BUILD FAILED|FAILURE: Build)' /tmp/gradle-stage.log; then
    echo "Error: gradle produced no build result (exit ${{GRADLE_RC}})" >&2
    exit 1
fi
"""


class SpringFrameworkImageBase(Image):
    """``base-pr-<N>`` image for Spring Framework PRs on JDK 17 (default).

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance`` rewrites
    the ``git clone`` below into the standard clone + ``checkout
    ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` + ``CMD`` sequence and supplies
    ``REPO_URL`` / ``BASE_COMMIT`` as build args, plus the proxy/CA/label
    infrastructure. None of that is written here.
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

    def dependency(self) -> str | Image:
        return "ubuntu:22.04"

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

        # `ubuntu:22.04` carries no JDK, so the toolchain is entirely on this
        # apt line. Zulu 17 is the one the build's toolchain default asks for;
        # 21/24/25 are kept for the later eras this config also serves, whose
        # `javaNNTest` tasks request them by language version.
        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"
ENV LC_ALL=C.UTF-8
ENV CI=true

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gnupg ca-certificates git curl \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends \\
    zulu17-jdk zulu21-jdk zulu24-jdk zulu25-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu17
ENV PATH=/usr/lib/jvm/zulu17/bin:$PATH

{code}

{self.clear_env}

"""


class SpringFrameworkImageBaseJDK11(Image):
    """``base-pr-<N>`` image for Spring Framework PRs on JDK 11 (5.2.x era)."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "ubuntu:22.04"

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

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"
ENV LC_ALL=C.UTF-8
ENV CI=true

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gnupg ca-certificates git curl \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends \\
    zulu11-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu11
ENV PATH=/usr/lib/jvm/zulu11/bin:$PATH

{code}

{self.clear_env}

"""


class SpringFrameworkImageBaseJDK8(Image):
    """``base-pr-<N>`` image for Spring Framework PRs on JDK 8 (3.2.x/4.2.x era)."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "ubuntu:22.04"

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

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"
ENV LC_ALL=C.UTF-8
ENV CI=true

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gnupg ca-certificates git curl \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends \\
    zulu8-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu8
ENV PATH=/usr/lib/jvm/zulu8/bin:$PATH

{code}

{self.clear_env}

"""


class SpringFrameworkImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT, repairs the Gradle bootstrap, warms caches.

    JDK selection:

    * JDK 8  -- ``3.2.x`` / ``4.2.x`` branches, and master PRs below 2000.
    * JDK 11 -- ``5.2.x`` branch, and master PRs below 25000.
    * JDK 17 -- ``main`` (6.0+) and everything else.
    """

    JDK_8_BRANCHES = {"3.2.x", "4.2.x"}
    JDK_11_BRANCHES = {"5.2.x"}

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def _select_jdk(self) -> str:
        ref = self.pr.base.ref
        if ref in self.JDK_8_BRANCHES:
            return "8"
        if ref in self.JDK_11_BRANCHES:
            return "11"
        if ref == "master" and self.pr.number < 2000:
            return "8"
        if ref == "master" and self.pr.number < 25000:
            return "11"
        # Late master PRs overlap with early `main`, and `main` is 6.0+.
        return "17"

    def dependency(self) -> Image | None:
        jdk = self._select_jdk()
        if jdk == "8":
            return SpringFrameworkImageBaseJDK8(self.pr, self._config)
        if jdk == "11":
            return SpringFrameworkImageBaseJDK11(self.pr, self._config)
        return SpringFrameworkImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        tasks = " ".join(_gradle_test_tasks(self.pr))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "msb-test-logging.gradle", _TEST_LOGGING_INIT),
            File(".", "msb-report-excluded.sh", _REPORT_EXCLUDED),
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
                """#!/bin/bash
set -e
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# ---------------------------------------------------------------------------
# Gradle bootstrap repair. Everything below this point deliberately modifies
# the tree and therefore runs AFTER the clean-tree assertions: the edits must
# persist into the graded stages. No Spring gold patch touches settings.gradle
# or build.gradle, so the `git apply` in the run scripts is unaffected.
# ---------------------------------------------------------------------------

# The build-scan plugins resolve from https://repo.spring.io/plugins-release,
# which now answers 401. They publish CI metadata only -- no effect on
# compilation or test execution -- so they are stripped rather than fixed.
sed -i "/io.spring.gradle-enterprise-conventions/d" settings.gradle 2>/dev/null || true
sed -i "/io.spring.gradle-enterprise-conventions/d" build.gradle 2>/dev/null || true
sed -i "/com.gradle.build-scan/d" build.gradle 2>/dev/null || true
sed -i '/io.spring.ge.conventions/d' settings.gradle 2>/dev/null || true
sed -i '/com.gradle.enterprise/d' settings.gradle 2>/dev/null || true

# The configuration blocks have to go with the plugin lines. Left behind, they
# fail evaluation with "Could not find method gradleEnterprise()". At
# 2f32806bcb6c that block is the trailing settings.gradle.projectsLoaded /
# gradleEnterprise / buildScan nest and is the last thing in the file.
sed -i '/settings.gradle.projectsLoaded/,$d' settings.gradle 2>/dev/null || true
sed -i '/^gradleEnterprise[[:space:]]*{{/,$d' settings.gradle 2>/dev/null || true
sed -i '/^develocity[[:space:]]*{{/,$d' settings.gradle 2>/dev/null || true

# Old wrappers cannot read the class files a newer JDK emits. Conservative for
# the JDK 8 era, 7.6.4 for JDK 11/17. Inert for this PR: the wrapper at the base
# commit is already gradle-7.3.
if grep -q "gradle-4" gradle/wrapper/gradle-wrapper.properties 2>/dev/null; then
    if [ "$JAVA_HOME" = "/usr/lib/jvm/zulu8" ]; then
        sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-4.10.3-bin.zip|' gradle/wrapper/gradle-wrapper.properties
    else
        sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-7.6.4-bin.zip|' gradle/wrapper/gradle-wrapper.properties
    fi
elif grep -q -e "gradle-5" -e "gradle-6" gradle/wrapper/gradle-wrapper.properties 2>/dev/null; then
    sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-7.6.4-bin.zip|' gradle/wrapper/gradle-wrapper.properties
fi

chmod +x gradlew

# ---------------------------------------------------------------------------
# Warm-up: the graded command itself, at the base commit.
#
# `|| true` is required -- a test that fails here is data, not a broken image,
# and on arm64 a native-dependency failure must not abort the build. Running the
# real `test` task rather than `testClasses` is deliberate: it resolves the test
# *runtime* classpath as well as the compile classpath, so the graded stages
# need no network even after the proxy configuration is torn down.
#
# The assertion is on the build-result line, not on the exit status: Gradle
# prints one once it is running whatever the outcome, so its absence means the
# wrapper, the JDK, or settings evaluation is broken. Failing here is far
# cheaper than discovering a silent 0/0/0 three stages later.
# ---------------------------------------------------------------------------
if [ -n "$MSB_TARGETARCH" ] && [ -n "$MSB_BUILDARCH" ] && [ "$MSB_TARGETARCH" != "$MSB_BUILDARCH" ]; then
    echo "NOTE: cross-building $MSB_TARGETARCH on $MSB_BUILDARCH -- skipping the Gradle warm-up."
    echo "NOTE: this image ships a cold Gradle cache and no baseline test inventory."
else
    ./gradlew {tasks} \\
        --no-daemon \\
        --console=plain \\
        --continue \\
        --no-build-cache \\
        --max-workers 4 \\
        -I /home/msb-test-logging.gradle \\
        -Dorg.gradle.jvmargs="-Xmx3g -XX:MaxMetaspaceSize=768m" \\
        -Dorg.gradle.java.installations.auto-download=false \\
        > /tmp/gradle-warmup.log 2>&1 || true

    tail -n 30 /tmp/gradle-warmup.log
    grep -qE '^(BUILD SUCCESSFUL|BUILD FAILED|FAILURE: Build)' /tmp/gradle-warmup.log

    # Snapshot the baseline test inventory. Gradle wipes build/test-results at the
    # start of every Test task, so a stage whose test source set will not compile
    # has no other way to learn the names of the tests it just lost. These are
    # Gradle's own strings, so the FAILURE markers synthesised from them match the
    # run/fix stages exactly.
    rm -rf /home/msb-baseline-results
    mkdir -p /home/msb-baseline-results
    find . -path '*/build/test-results/test/TEST-*.xml' -print0 \
        | xargs -0 -r cp -t /home/msb-baseline-results
    echo "baseline test inventory: $(ls -1 /home/msb-baseline-results | wc -l) class result file(s)"

fi
""".format(pr=self.pr, tasks=tasks),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY.format(tasks=tasks, repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
# Fatal on failure. A test patch that does not apply means the stage would run
# unpatched code and report the baseline as though it were the test stage --
# silent corruption of the f2p signal rather than a visible failure.
if ! git -C /home/{pr.repo} apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' \\
        --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' \\
        /home/test.patch; then
    echo "Error: git apply failed for test.patch" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY.format(tasks=tasks, repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
# test.patch first, then fix.patch, in a single apply.
if ! git -C /home/{pr.repo} apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' \\
        --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' \\
        /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed for test.patch/fix.patch" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY.format(tasks=tasks, repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # BuildKit supplies TARGETARCH/BUILDARCH; prepare.sh compares them to tell
        # a native build from an emulated cross-build. Declared here rather than
        # left to DockerfileEnhancer: the enhancer only rewrites images whose
        # dependency() returns a string, and this one returns an Image, so it
        # never runs over this file. Both are empty on the classic builder,
        # where prepare.sh correctly falls back to running the warm-up.
        arch_args = (
            "ARG TARGETARCH\n"
            "ARG BUILDARCH\n"
            "ENV MSB_TARGETARCH=${TARGETARCH}\n"
            "ENV MSB_BUILDARCH=${BUILDARCH}"
        )

        prepare_commands = "RUN bash /home/prepare.sh"

        # Gradle does not read the http_proxy env vars; it needs systemProp.*
        # entries in ~/.gradle/gradle.properties. Written only for the build
        # (prepare.sh downloads the wrapper, the plugins and the whole
        # dependency graph through it) and removed afterwards, so the graded
        # stages run against the warmed cache with no proxy credentials baked
        # into the shipped image.
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
                proxy_setup = f"""RUN mkdir -p /root/.gradle && \\
    printf '%s\\n' \\
        'systemProp.http.proxyHost={proxy_host}' \\
        'systemProp.http.proxyPort={proxy_port}' \\
        'systemProp.https.proxyHost={proxy_host}' \\
        'systemProp.https.proxyPort={proxy_port}' \\
        >> /root/.gradle/gradle.properties"""

                proxy_cleanup = "RUN rm -f /root/.gradle/gradle.properties"

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{arch_args}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


# `--console=plain` should keep the log clean, but Gradle still colours some
# output when a plugin writes it directly, and an escape inside a name would
# make the same test a different string in a different stage.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Emitted by msb-test-logging.gradle, one line per test result:
#   MSB-TEST|<test source path>|<class>|<method>|<SUCCESS|FAILURE|SKIPPED>
# Anchored at line start so a test whose own output quotes the marker cannot
# inject a result. The name carries no timing, no counts and no ordinal, so it
# is byte-identical across the run/test/fix stages.
_RESULT_LINE = re.compile(
    r"^MSB-TEST\|([^|]*)\|([^|]*)\|(.*)\|(SUCCESS|FAILURE|SKIPPED)\s*$"
)


def parse_gradle_marker_log(log: str) -> TestResult:
    """Build a TestResult from the init script's per-test marker lines."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    for line in clean.splitlines():
        m = _RESULT_LINE.match(line.rstrip())
        if not m:
            continue

        path, cls, method, result = m.groups()
        # Leading with the source path is what lets Report's test-patch and
        # fix-patch file matchers resolve this name (`<file> > ...`).
        name = f"{path} > {cls} > {method}"

        if result == "SUCCESS":
            passed_tests.add(name)
        elif result == "FAILURE":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    # TestResult.__post_init__ rejects overlapping sets. A test retried after a
    # failure can be reported both ways; failure is the honest verdict.
    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("spring-projects", "spring-framework")
class SpringFramework(Instance):
    """Instance handler for spring-projects/spring-framework.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves
    on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SpringFrameworkImageDefault(self.pr, self._config)

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
        return parse_gradle_marker_log(log)
