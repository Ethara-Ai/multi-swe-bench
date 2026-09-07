"""Repo config for ``apache/druid`` PRs #890-#2209 (Java / Maven / JUnit+surefire).

Scope
-----
Serves ``Output/apache__druid_raw_dataset.jsonl``: 5 PRs, ``#890`` (base commit
2014-11-19) through ``#2209`` (base commit 2016-01-06).  That window is the
pre-Apache ``io.druid`` era -- Druid ``0.7.0-SNAPSHOT`` to ``0.9.0-SNAPSHOT``.

Registration
------------
Every entry in that JSONL carries ``number_interval == ""`` and ``tag == ""``,
so ``Instance.create()`` (instance.py:40-51) builds the lookup key
``"apache/druid"``.  The first decorator is therefore the one the current
dataset actually resolves through.  The second decorator registers the
interval key that matches this file's stem, so the same config keeps working
unchanged if ``number_interval`` is later stamped onto the dataset -- the
pattern used by ``python/secdev/scapy_3811_to_3810.py`` and
``python/sourmash_bio/sourmash_1186_to_503.py``.

Relationship to the sibling druid configs
-----------------------------------------
``druid_0_to_16976.py`` / ``druid_16977_to_18789.py`` / ``druid_18790_to_99999.py``
split modern Druid by JDK (8 / 11 / 17) and are keyed on ``number_interval``.
This file's PR range is a *subset* of ``0_to_16976``'s interval, so the image
tags below are deliberately prefixed (``jdk8-legacy-``) instead of reusing the
siblings' bare ``base-pr-<n>`` / ``pr-<n>``.  ``Image.__eq__``/``__hash__`` are
``image_full_name()``, so identical tags for the same PR number would make the
build graph silently drop one of the two images (Check 2F).

No symbols are imported from the sibling era files: the helpers there assume the
modern ``extensions-core`` / ``extensions-contrib`` layout, which does not exist
in this era, and their ``lstrip("a/")`` path strip is character-set based rather
than prefix based.

Toolchain evidence (root pom.xml fetched at each of the 5 base SHAs)
--------------------------------------------------------------------
=====  ==========  ==================  =====================  ==========================
PR     base date   project version     parent pom             module layout
=====  ==========  ==================  =====================  ==========================
890    2014-11-19  0.7.0-SNAPSHOT      none (standalone)      flat (``histogram``, ...)
1417   2015-06-03  0.8.0-SNAPSHOT      io.druid:oss-parent:2  ``extensions/<name>``
1730   2015-09-14  0.8.2-SNAPSHOT      io.druid:oss-parent:2  ``extensions/<name>``
2207   2016-01-06  0.9.0-SNAPSHOT      io.druid:oss-parent:2  ``extensions/<name>``
2209   2016-01-06  0.9.0-SNAPSHOT      io.druid:oss-parent:2  ``extensions/<name>``
=====  ==========  ==================  =====================  ==========================

* Java level is ``1.7`` for the whole range -- ``#890`` sets
  ``maven-compiler-plugin:2.5.1`` with ``<source>1.7</source><target>1.7</target>``
  inline; ``#1417``+ inherit ``javac.src.version=1.7`` /
  ``javac.target.version=1.7`` from ``io.druid:oss-parent:2``.  JDK 8 is the
  newest JDK that compiles ``-source 1.7`` while still running Jetty 9.2,
  Jackson 2.4, Guava 16 and cobertura 2.7, so ``openjdk-8-jdk`` it is.
* Surefire is ``2.12.2`` at ``#890`` and ``2.18.1`` (from ``oss-parent:2``) for
  ``#1417``+.  Both emit ``target/surefire-reports/TEST-*.xml`` and both print
  the ``Running <class>`` / ``Tests run:`` console pair that ``parse_log`` falls
  back to.
* Maven: both the root pom and ``oss-parent:2`` declare
  ``<prerequisites><maven>3.0.3</maven></prerequisites>``.  Ubuntu 22.04 ships
  Maven 3.6.3, which is old enough to predate the 3.8.1 plain-HTTP repository
  blocker and new enough for every plugin used here.

Known blocker: PR #890 cannot resolve its dependencies
------------------------------------------------------
``#890``'s root pom pins six artifacts that are **not on Maven Central**, and
its only declared fallback repository is dead::

    io.druid:druid-api:0.3.0            Central has 0.3.3+ only
    com.metamx:java-util:0.26.9         Central has 0.26.12+ only
    com.metamx:emitter:0.2.13           absent
    com.metamx:http-client:0.9.7        absent
    com.metamx:bytebuffer-collections:0.1.1  absent
    com.metamx:server-metrics:0.0.9     absent

    <repository> https://metamx.artifactoryonline.com/metamx/pub-libs-releases-local
        -> invalid TLS certificate, and HTTP 403 from an nginx placeholder.
           JFrog retired the *.artifactoryonline.com hosts.

So ``#890`` is expected to fail dependency resolution, produce no surefire
output, and be rejected by ``Report.check()`` rule 1 (``all_count == 0``).  It
is left routed to this config rather than silently special-cased: a loud empty
report is the correct signal, and nothing in a repo config can conjure deleted
artifacts.  Dropping ``#890`` from the dataset is a dataset decision, not a
config one.

Every other pinned coordinate resolves.  ``dependencyManagement`` was walked in
full against ``repo1.maven.org`` at ``#1417`` (82 entries) and ``#2209`` (81
entries); the only miss in each is ``org.antlr:antlr4-coordinator:4.0``, which
is a version pin that no module ever declares as a dependency (``server`` uses
``org.antlr:antlr4-runtime``, which is present).  ``#1417``+ declare no
``<repositories>`` at all -- everything comes from Central.

Why the build is scoped
-----------------------
See ``_build_pl_flag`` / ``_build_test_flag``.  Compilation stays at full scope
for the selected modules and their ``-am`` upstreams; only test *execution* is
narrowed to the test classes the PR touches.

Test-jar note (matters for #2207)
---------------------------------
``extensions/histogram`` and ``extensions/datasketches`` depend on
``io.druid:druid-processing`` with ``<type>test-jar</type>``, and ``#2207``'s
test patch edits ``processing/src/test/java/.../AggregationTestHelper.java``,
which lives in that test-jar.  ``mvn test`` never reaches the ``package`` phase
that builds a test-jar, but Maven 3's ``ReactorReader`` substitutes
``processing/target/test-classes`` for a reactor test-jar once that module has
run ``test-compile`` in the same session.  ``-pl`` therefore lists ``processing``
alongside the two extensions (it is patch-derived, so this happens
automatically) and the edited helper is what the extensions compile against.

Verification status
-------------------
Static + upstream-artifact checks only.  No Docker build and no test run has
been performed for this config, so the f2p/p2p behaviour of each PR and the
clean application of each patch are unverified.
"""

import re
import textwrap

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Top-level directories that exist in this era but are NOT Maven reactor
# modules.  Feeding one to `-pl` aborts the whole build with "Could not find the
# selected project in the reactor".  Taken from the actual root trees:
#   #890  -> docs, install, publications  (every other tree is a <module>)
#   #2209 -> docs, publications           (distribution, integration-tests,
#                                          benchmarks, aws-common, examples are
#                                          all real modules)
_NON_MODULE_DIRS = frozenset({"docs", "install", "publications"})

# Directories that only aggregate child modules.  The root pom declares the
# children by their two-segment path (`<module>extensions/histogram</module>`),
# so `-pl extensions` alone would build nothing but the parent POM and run zero
# tests.  `extensions` is the only such directory in this era -- the modern
# `extensions-core` / `extensions-contrib` / `cloud` split does not appear until
# well after #2209, and #890 predates `extensions` entirely (its histogram,
# hdfs-storage, ... modules sit at the top level and are handled by the
# single-segment branch below).
_GROUPING_DIRS = frozenset({"extensions"})

# Applied identically to the warm-up build and to all three graded runs.
#   -fn                                   --fail-never: a failing test module
#                                         must not abort the reactor before the
#                                         later modules run and before the XML
#                                         reports are dumped.  Maven exits 0 in
#                                         this mode ("Build failures were
#                                         ignored"), so no `|| true` is needed
#                                         on the graded test command.
#   -Dsurefire.useFile=false              per-class results on stdout instead of
#                                         only in *.txt files.
#   -DfailIfNoTests=false                 `-Dtest=` selects classes that exist in
#   -Dsurefire.failIfNoSpecifiedTests     one module and not the others; without
#                                         both, every other module errors out.
_SUREFIRE_FLAGS = (
    "-fn "
    "-Dsurefire.useFile=false "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false"
)

# Exported by every script rather than added as ENV layers, so the rendered base
# Dockerfile keeps exactly one ENV instruction (the infrastructure block).
#
#   JAVA_HOME   the `maven` package pulls default-jdk-headless (JDK 11) in as a
#               dependency, so without this `mvn` would run on 11 and reject the
#               `-source 1.7` that this era's poms require.
#   MAVEN_OPTS  sizes the Maven JVM itself; the `-am` closure of `processing`
#               holds ~2500 main plus ~1200 test sources. The forked surefire
#               JVM is sized separately by the repo's own <argLine>.
#   LC_ALL      the JVM needs it for locale-stable test output.
_TOOLCHAIN_ENV = (
    "export JAVA_HOME=/usr/lib/jvm/java-8-openjdk\n"
    'export MAVEN_OPTS="-Xmx2g"\n'
    "export LC_ALL=C.UTF-8"
)


def _patch_paths(patch_text: str) -> list[str]:
    """Repo-relative path of every file touched by a unified diff.

    Reads the ``a/`` side of each ``diff --git`` header.  Splitting on
    whitespace is safe here because no path in this repo contains a space --
    checked across all ten patches in the dataset.
    """
    paths: list[str] = []
    for line in patch_text.split("\n"):
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2]
        path = path.removeprefix("a/")
        paths.append(path)
    return paths


def _modules_from_patch(patch_text: str) -> set[str]:
    """Maven reactor module paths touched by a unified diff.

    Two-segment (``extensions/histogram``) for children of a grouping
    directory, single-segment (``processing``) otherwise, matching how the root
    pom declares them.
    """
    modules: set[str] = set()
    for path in _patch_paths(patch_text):
        segments = path.split("/")
        # A root-level file (pom.xml, README.md, .travis.yml) is not a module.
        if len(segments) < 2:
            continue
        top = segments[0]
        if top.startswith(".") or top in _NON_MODULE_DIRS:
            continue
        if top in _GROUPING_DIRS:
            # `extensions/pom.xml` has only two segments -- a file in the
            # aggregator itself, not a buildable child.
            if len(segments) >= 3:
                modules.add(f"{top}/{segments[1]}")
            continue
        modules.add(top)
    return modules


def _build_pl_flag(pr: PullRequest) -> str:
    """``-pl <modules> -am`` for the modules this PR touches.

    Both patches are read, so a fix that lands in ``processing`` and a test that
    lands in ``extensions/datasketches`` both end up in the reactor.  ``-am``
    pulls in their upstream modules, so the selected modules still compile
    against freshly built sources rather than stale jars.
    """
    modules = _modules_from_patch(pr.fix_patch) | _modules_from_patch(pr.test_patch)
    if not modules:
        return ""
    return "-pl " + ",".join(sorted(modules)) + " -am"


def _test_classes_from_patch(patch_text: str) -> set[str]:
    """Bare JUnit class names (for ``-Dtest=``) under any ``src/test/`` tree."""
    classes: set[str] = set()
    for path in _patch_paths(patch_text):
        if "/src/test/" not in path or not path.endswith(".java"):
            continue
        classes.add(path.rsplit("/", 1)[-1][: -len(".java")])
    return classes


def _build_test_flag(pr: PullRequest) -> str:
    """Scope surefire to the test classes this PR actually touches.

    ``-am`` drags in modules with hundreds of test classes each (``processing``
    alone), and Druid's query tests are minutes apiece; running all of them four
    times (warm-up + three graded stages) on two architectures is days of build
    time.  Compilation is left at full scope on purpose -- every selected module
    and its upstreams still compile, so a patch that breaks compilation
    elsewhere is still caught -- and only test EXECUTION is narrowed.

    Stated plainly: this shrinks the p2p baseline to the touched test classes.
    f2p/n2p are unaffected, because they come from exactly these classes.

    Each name gets a trailing ``*`` so surefire also matches NESTED static test
    classes: a JUnit container whose ``@Test`` methods all live in inner classes
    compiles to ``Outer$Nested.class``, and a bare ``-Dtest=Outer`` matches only
    the empty outer class and contributes zero tests.

    Pairs with ``-DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false``
    so modules holding none of the named classes do not fail the build.
    """
    classes = _test_classes_from_patch(pr.test_patch)
    if not classes:
        return ""
    return "-Dtest=" + ",".join(f"{c}*" for c in sorted(classes))


def _mvn_scope(pr: PullRequest) -> str:
    return " ".join(x for x in (_build_pl_flag(pr), _build_test_flag(pr)) if x)


def _mvn_prepare_cmd(pr: PullRequest) -> str:
    """Warm-up build, run once while the PR image is being built.

    ``clean test`` so the local repository, the reactor's ``target/classes`` and
    ``target/test-classes`` are all fully populated inside the image.  That is
    what lets the three graded runs use ``-o``.
    """
    return " ".join(
        x for x in (f"mvn clean test {_SUREFIRE_FLAGS}", _mvn_scope(pr)) if x
    )


def _mvn_graded_cmd(pr: PullRequest) -> str:
    """Graded command -- identical string in run.sh, test-run.sh and fix-run.sh.

    No ``clean``: the warm-up already compiled everything, so each stage only
    recompiles what the applied patch changed.

    Deliberately NOT ``-o`` (offline), even though the warm-up populates the
    local repository and no PR here touches a ``pom.xml``. Surefire resolves its
    *provider* jar at runtime rather than declaring it, and it only does so once
    it has tests to run. PR #2209's ``-Dtest=CascadeExtractionFnTest*`` matches
    nothing at the base commit -- test.patch is what creates that class -- so the
    warm-up ran surefire, selected zero tests, and never fetched
    ``org.apache.maven.surefire:surefire-junit4:2.18.1``. The fix stage then died
    with "Unable to generate classpath ... 1 required artifact is missing",
    scoring 0/0/0 and failing Report.check() rule 1. Observed, not theorised:
    ``instances/pr-2209/fix-patch-run.log`` from the 2026-09-04 run.
    """
    return " ".join(x for x in (f"mvn test {_SUREFIRE_FLAGS}", _mvn_scope(pr)) if x)


# Dumping surefire's own XML is what gives METHOD-level granularity.  Console
# output is per-CLASS only, which cannot represent a PR that adds new @Test
# methods to a test class that already exists: the class passes both before and
# after the fix, so it lands in p2p while f2p/n2p come out empty.
#
# The purge runs first because the graded stages deliberately do NOT `clean`.
# Without it, a class that ran during the image warm-up but fails to compile
# after test.patch would leave its stale PASSING report behind, and `cat` would
# report it as passing in a stage where it never ran.
#
# `|| true` here is on housekeeping, not on the test command: find returns
# non-zero merely for a directory that vanished mid-walk, and `set -e` would
# turn that into a lost stage.
_PURGE_REPORTS = (
    "find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true"
)
_DUMP_REPORTS = (
    "find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} \\; "
    "2>/dev/null || true"
)


class DruidLegacyJdk8ImageBase(Image):
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
        # NOT per-PR. The base stops at `git clone`, so its content is identical
        # for every PR in the range: same base OS, same JDK/Maven packages, same
        # full-history clone. The per-PR work -- checkout of BASE_COMMIT and the
        # history hardening -- lives in the PR image, so nothing here is pinned
        # to one commit and all five PRs share one build.
        #
        # `jdk8-legacy` prefix: this file's PR range (890-2209) sits inside the
        # interval owned by druid_0_to_16976.py, which uses the bare
        # `base-pr-<n>` tag. Image identity is image_full_name(), so an
        # unprefixed tag would collide for any PR number both configs can see
        # and one of the two images would be silently skipped.
        return "base-jdk8-legacy"

    def workdir(self) -> str:
        return "base-jdk8-legacy"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Full base Dockerfile: infrastructure, toolchain, clone, CMD. Nothing else.

        This returns a COMPLETE Dockerfile including the BuildKit syntax
        directive, which makes ``DockerfileEnhancer.enhance()`` return it
        verbatim (image.py:317-318). That is deliberate and is the only way to
        keep the required layout without touching shared harness code:
        ``_standardize_repo_fetch()`` would otherwise rewrite a bare clone line
        into clone + checkout + history-hardening, and ``_inject_final_sanitize()``
        would append the hardening block before ``CMD``. Both belong in the PR
        image, not here.

        The infrastructure block is not hand-copied -- it is generated by
        ``DockerfileEnhancer._infrastructure_block()``, so the ARGs, ENV block,
        OCI labels and CA-cert symlinks stay byte-identical to what the enhancer
        emits for every other image in the pipeline.

        ``ARG BASE_COMMIT`` is declared but not consumed here: build_dataset
        passes it as a build arg to any image whose ``dependency()`` is a string
        (build_dataset.py:625-629), and an undeclared build arg makes Docker warn.

        The infrastructure ENV block is the ONLY ``ENV`` instruction in the
        rendered file. JAVA_HOME, MAVEN_OPTS and LC_ALL are exported by the
        shell scripts instead (``_TOOLCHAIN_ENV``) rather than added as extra
        ``ENV`` layers here.
        """
        base_img = self.dependency()
        if isinstance(base_img, Image):
            base_img = base_img.image_full_name()

        repo = self.pr.repo
        infra = DockerfileEnhancer._infrastructure_block(self, base_img)

        # The apt step is retried as a unit and asserted afterwards.
        # `apt-get update` exits 0 even when it fails to fetch an index -- it
        # only prints "W: Failed to fetch" -- so a partial update silently drops
        # `universe`, where `maven` lives, and the failure surfaces later as
        # "E: Unable to locate package maven". That is exactly what killed the
        # first multi-arch attempt: the amd64 apt and the arm64 400MB git clone
        # ran concurrently and archive.ubuntu.com timed out at 621s. Keeping
        # update and install inside one `if` means a dropped index retries the
        # whole pair, and `command -v mvn` turns any residual failure into a
        # loud one instead of an image silently missing its build tool.
        body = f"""{self.global_env}

WORKDIR /home/

RUN set -eux; \\
    ok=0; \\
    for i in 1 2 3 4 5; do \\
        if apt-get -o Acquire::Retries=5 update \\
           && apt-get install -y ca-certificates git openjdk-8-jdk maven; then \\
            ok=1; break; \\
        fi; \\
        echo "apt attempt $i failed; retrying in 15s"; \\
        sleep 15; \\
    done; \\
    test "$ok" = 1; \\
    command -v mvn; \\
    rm -rf /var/lib/apt/lists/*; \\
    ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""

        return "\n".join(
            [
                DockerfileEnhancer.SYNTAX_DIRECTIVE,
                "",
                f"FROM {base_img}",
                "",
                infra,
                body,
            ]
        )


class DruidLegacyJdk8ImageDefault(Image):
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
        return DruidLegacyJdk8ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        # MUST be exactly "pr-<number>", NOT prefixed like the base tag.
        # build_dataset names the exported OCI archive after image_full_name()
        # (build_dataset.py:606), and scripts/batch_ecr_push.py::find_tar()
        # looks the tar up as "mswebench_<org>_m_<repo>_pr-<number>.tar". A
        # prefixed tag yields a file that tool cannot find, and the push fails
        # with "tar not found" for every instance.
        #
        # Safe despite druid_0_to_16976.py using the same "pr-<n>" form: a PR
        # resolves to exactly one registration key, so the two configs are never
        # both instantiated for the same PR number in one run. The base images
        # do differ ("base-jdk8-legacy" vs "base-pr-<n>"), so the shared parent
        # never collides either.
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        # MUST be exactly "pr-<number>", unlike image_tag(). run_instance()
        # names the instance directory after this (build_dataset.py:731-738),
        # and gen_report.collect_report_tasks() only picks up directories that
        # start with "pr-" and whose remainder parses as an int
        # (gen_report.py:357-359). A prefixed workdir builds and runs fine but
        # collects 0 report tasks, so final_report.json comes out all zeros --
        # observed on the 2026-09-04 run with workdir "jdk8-legacy-pr-<n>".
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_sha = self.pr.base.sha
        prepare_cmd = _mvn_prepare_cmd(self.pr)
        graded_cmd = _mvn_graded_cmd(self.pr)

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
{_TOOLCHAIN_ENV}

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

{prepare_cmd} || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{_TOOLCHAIN_ENV}

cd /home/{repo}
{_PURGE_REPORTS}
{graded_cmd}
{_DUMP_REPORTS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{_TOOLCHAIN_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{_PURGE_REPORTS}
{graded_cmd}
{_DUMP_REPORTS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{_TOOLCHAIN_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_PURGE_REPORTS}
{graded_cmd}
{_DUMP_REPORTS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        """PR image: pin to this PR's base commit, strip the history, then build.

        ``DockerfileEnhancer.enhance()`` returns this verbatim (image.py:315-316
        bails out when ``dependency()`` is an Image), so everything the pipeline
        would otherwise inject has to be written here explicitly.

        The checkout and ``Image._HARDENING_BLOCK`` live in this file rather than
        in the base image or in prepare.sh because they are per-PR: the block
        detaches at *this* PR's ``base.sha`` and deletes every other ref, so the
        agent cannot read the future of the branch. The block is reused verbatim
        from the harness so it stays in lockstep with the canonical hardening
        instead of drifting from a copy.

        ``BASE_COMMIT`` is an ARG with a default because build_dataset only
        passes build args to images whose ``dependency()`` is a string
        (build_dataset.py:625-629).

        Hardening runs before prepare.sh: prepare.sh's own
        ``git checkout <base.sha>`` then resolves against the already-detached
        HEAD, and the Maven build happens on the stripped tree.

        The Maven proxy plumbing has to live here too -- prepare.sh runs ``mvn``
        during THIS build, and the JVM ignores the ``http_proxy`` env vars the
        infrastructure block sets.
        """
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                # The `"?` is load-bearing: Image.global_env renders every
                # variable as `ENV key="value"` (image.py:115), so the sibling
                # configs' `=http`-anchored pattern never matches and their
                # settings.xml is never written. Bare form still accepted.
                match = re.match(
                    r'^ENV\s*(http[s]?_proxy)="?http[s]?://([^:"]+):(\d+)', line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )

        blocks = [
            f"FROM {name}:{tag}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            proxy_setup.strip(),
            copy_commands.strip(),
            f"WORKDIR /home/{repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
            Image._HARDENING_BLOCK.rstrip("\n"),
            "RUN bash /home/prepare.sh",
            proxy_cleanup.strip(),
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(b for b in blocks if b) + "\n"


@Instance.register("apache", "druid")
@Instance.register("apache", "druid_890_to_2209")
class DRUID_890_TO_2209(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DruidLegacyJdk8ImageDefault(self.pr, self._config)

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

        # Strip ANSI first.  Maven suppresses colour when stdout is not a TTY,
        # which is the normal case here, but a plugin that writes escapes
        # unconditionally would otherwise land them inside a captured test name
        # and desynchronise the three stages.
        clean_log = re.sub(r"\x1B\[[0-9;?]*[a-zA-Z]", "", test_log)

        # --- Preferred path: surefire XML, METHOD granularity ---------------
        # The graded scripts cat target/surefire-reports/TEST-*.xml to stdout.
        # See _DUMP_REPORTS for why class-level console parsing is not enough.
        # Falls back to the console parser below only when no XML is present at
        # all (e.g. the build died before surefire ran).
        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        re_attr = re.compile(r'(\w+)="([^"]*)"')
        # A test that prints the literal text "<failure" would otherwise be
        # misread as failing, so captured stdout/stderr is removed from the body
        # before the outcome elements are looked for.
        re_captured_output = re.compile(
            r"<system-(out|err)\b.*?</system-\1>|<system-(out|err)\b[^>]*/>", re.DOTALL
        )
        saw_xml = False
        for m in re_case.finditer(clean_log):
            attrs = dict(re_attr.findall(m.group(1)))
            cls = attrs.get("classname", "")
            meth = attrs.get("name", "")
            if not meth:
                continue
            saw_xml = True
            # `#` between class and method, NOT `.`. classname is fully
            # qualified, so either separator is unique across modules and
            # carries no timing or ordinal metadata. But `#` is the JVM test-id
            # shape the harness parses: report.py::_file_hosts_test (362-382)
            # derives the class as `split(" > "|"#")[0].rsplit(".", 1)[-1]`, so
            # with a dotted separator the LAST segment is the method name, the
            # class never matches the file stem, and the fix-patch cheating
            # guard (report.py:178-180) silently cannot tie a match to the
            # test's own file. It also breaks the gold-deletion detection for
            # renamed tests: PR #2207 renames testSimpleDataIngestAndQuery and
            # PR #1730 deletes three testUnionTimeLineLookup* methods, and with
            # dotted names both are misreported as lost baseline coverage.
            # `#` is also Maven's own selector form (-Dtest=Class#method).
            name = f"{cls}#{meth}" if cls else meth
            body = re_captured_output.sub("", m.group(3) or "")
            if "<failure" in body or "<error" in body:
                failed_tests.add(name)
            elif "<skipped" in body:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        if not saw_xml:
            # --- Fallback: surefire console summary, CLASS granularity ------
            # Surefire 3.x: "[INFO] Tests run: 5, ... -- in com.foo.BarTest"
            # Surefire 2.x: "Tests run: 5, ... Time elapsed: 0.203 sec"
            # Only 2.12.2 / 2.18.1 appear in this era, so the class name is
            # taken from the preceding "Running <class>" line.
            #
            # The skip-zone between "Running X" and its "Tests run:" line must
            # stop at the next "Running Y" AND at Maven's "Results:" aggregate
            # header.  Otherwise a class whose fork dies (OOM, JVM crash) before
            # printing its own "Tests run:" line gets paired with a later,
            # unrelated one -- the next class's, or the build-wide aggregate --
            # and a crashed class is reported as passed.
            re_pass_tests = [
                re.compile(
                    r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
                )
            ]
            re_fail_tests = [
                re.compile(
                    r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+).*<<<\s*FAILURE!"
                )
            ]

            for re_fail_test in re_fail_tests:
                for m in re_fail_test.finditer(clean_log):
                    failed_tests.add(m.group(1))

            for re_pass_test in re_pass_tests:
                for m in re_pass_test.finditer(clean_log):
                    test_name = m.group(1)
                    if test_name in failed_tests:
                        continue
                    tests_run = int(m.group(2))
                    failures = int(m.group(3))
                    errors = int(m.group(4))
                    skipped = int(m.group(5))
                    if failures > 0 or errors > 0:
                        failed_tests.add(test_name)
                    elif tests_run > 0 and skipped == tests_run:
                        skipped_tests.add(test_name)
                    elif tests_run > 0:
                        passed_tests.add(test_name)

        # TestResult.__post_init__ (test_result.py:56-101) rejects any overlap
        # between the three sets.  Ordering matters: failures win over passes
        # and skips, skips win over passes, and the last subtraction cannot
        # reintroduce a failed test.  A JUnit class re-listed across modules can
        # legitimately land in two buckets, so this is load-bearing, not
        # defensive padding.
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
