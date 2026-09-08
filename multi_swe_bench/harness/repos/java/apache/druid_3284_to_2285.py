"""Repo config for ``apache/druid`` PRs #2285-#3284 (Java / Maven / JUnit+surefire).

Scope
-----
Serves ``apache__druid_raw_dataset.jsonl``: 10 PRs, ``#2285`` (base commit
2016-01-19) through ``#3284`` (base commit 2016-07-26).  Still the pre-Apache
``io.druid`` era -- Druid ``0.9.0-SNAPSHOT`` to ``0.9.2-SNAPSHOT``.

Registration
------------
The interval key ``apache/druid_3284_to_2285`` is the one this dataset resolves
through once ``number_interval`` is stamped on each row.  The plain
``apache/druid`` key is registered as well so the file also works on rows that
carry no ``number_interval`` -- ``Instance.create`` (instance.py:41-49) builds
``f"{org}/{repo}"`` in that case, and ``validate_dataset``'s
``resolve_number_interval`` checks the same key before the interval index.

That plain key was previously owned by ``druid_890_to_2209.py`` and has been
moved here, because two classes registering one key means the winner is decided
by import order in ``apache/__init__.py``.  ``druid_890_to_2209.py`` keeps its
own interval key; a dataset for that older range must carry
``number_interval: "druid_890_to_2209"``.

Note the hi-then-lo file stem is load-bearing, not cosmetic.  Both the validator
(``build_interval_index``) and ``run_parallel_eval``'s enrich script parse range
keys with ``_(?P<hi>\\d+)_to_(?P<lo>\\d+)`` and index them as ``lo..hi``.  The
three ``lo_to_hi``-named druid siblings (``0_to_16976``, ``16977_to_18789``,
``18790_to_99999``) therefore index as empty intervals and are reachable only by
an exact ``number_interval`` match.  ``3284_to_2285`` indexes as 2285..3284,
which is exactly this dataset's PR span.

Relationship to the sibling druid configs
-----------------------------------------
``druid_890_to_2209.py`` covers the immediately preceding window and shares this
file's toolchain conclusions; the helpers are re-derived here rather than
imported, because the module layout differs (see below) and a shared import
would couple two configs that are meant to age independently.
``druid_0_to_16976.py`` / ``16977_to_18789`` / ``18790_to_99999`` split modern
Druid by JDK (8 / 11 / 17) and assume the ``extensions-core`` /
``extensions-contrib`` layout only.

Toolchain evidence (root pom.xml fetched at all ten base SHAs)
--------------------------------------------------------------
=====  ==========  ==========  ==========================================
PR     base date   base ref    module layout at that commit
=====  ==========  ==========  ==========================================
2285   2016-01-19  master      ``extensions/<name>``            (26 modules)
2325   2016-02-05  master      ``extensions/<name>``            (28 modules)
2519   2016-02-23  master      ``extensions/<name>``            (28 modules)
2529   2016-02-24  **0.9.0**   ``extensions/<name>``            (28 modules)
2753   2016-03-30  master      ``extensions-core|contrib/...``  (31 modules)
2844   2016-04-19  master      ``extensions-core|contrib/...``  (32 modules)
2922   2016-05-04  master      ``extensions-core|contrib/...``  (33 modules)
3033   2016-06-17  master      ``extensions-core|contrib/...``  (33 modules)
3071   2016-09-16  master      ``extensions-core|contrib/...``  (34 modules)
3284   2016-07-26  master      ``extensions-core|contrib/...``  (32 modules)
=====  ==========  ==========  ==========================================

* The ``extensions/`` -> ``extensions-core/`` + ``extensions-contrib/`` split
  lands between ``#2529`` and ``#2753``, so BOTH layouts occur inside this one
  range.  ``_GROUPING_DIRS`` is the union of the three names; a name that does
  not exist at a given commit simply never matches a patch path.
* Every one of the ten commits inherits ``io.druid:oss-parent:2``, which sets
  ``javac.src.version=1.7`` / ``javac.target.version=1.7`` and surefire
  ``2.18.1``.  JDK 8 is the newest JDK that still compiles ``-source 1.7``, so
  ``openjdk-8-jdk`` it is -- the same conclusion the 890-2209 config reached,
  re-checked here rather than assumed.
* None of the ten root poms declares a ``<repositories>`` block, so every
  artifact comes from Maven Central.  This range is therefore free of the dead
  ``metamx.artifactoryonline.com`` fallback that makes ``#890`` unbuildable in
  the older config.
* The root pom's surefire ``<configuration>`` pins
  ``<argLine>-Xmx1024m -Duser.language=en -Duser.country=US
  -Dfile.encoding=UTF-8 ...</argLine>``, so the forked test JVM is sized by the
  repo itself; ``MAVEN_OPTS`` below sizes only the Maven JVM.
* Top-level directories that are NOT reactor modules, read off the actual trees
  at ``#2285`` and ``#3071``: ``docs`` and ``publications``.  Everything else at
  the top level is either a module or a plain file.

``#2529`` is the ``0.9.0``-branch backport of ``#2519`` and carries an identical
fix/test patch.  Its base commit is not on ``master``, so it is reachable only
because the base image does a full ``git clone`` (all refs) and
``refs/heads/0.9.0`` still exists upstream -- verified with ``git ls-remote``.

Image layout
------------
ONE base image for the whole range (``base-3284-to-2285``), shared by all ten
PRs, rather than a per-PR ``base-pr-<N>``.

The layout follows the reference pair in
``output2/workdir/surrealdb/surrealdb/images`` (``base-5831-to-241`` /
``pr-241``) instruction for instruction::

    BASE   syntax directive
           FROM <toolchain image>          # rust:1.68 there, maven:3.8.6-openjdk-8 here
           <infrastructure block>          # ARGs, ENV, LABEL, CA symlinks
           WORKDIR /home/
           RUN git clone "${REPO_URL}" /home/<repo>
           WORKDIR /home/<repo>
           CMD ["/bin/bash"]

    PR     FROM <base>
           ARG BASE_COMMIT="<sha>"
           COPY fix.patch / test.patch / the five scripts
           RUN bash /home/prepare.sh
           RUN git reset --hard
           RUN git checkout ${BASE_COMMIT}
           <Image._HARDENING_BLOCK>        # detach, drop refs, gc, four asserts
                                           # no WORKDIR, no CMD -- inherited

There is **no apt block anywhere**.  The reference base is ``FROM rust:1.68``,
a toolchain image that already ships git and the compiler, so it installs
nothing; ``maven:3.8.6-openjdk-8`` is the Java equivalent and does the same job
here.  That is what settles the earlier question of whether the apt block
belonged in the base or the PR layer: with a toolchain base there is no apt
block to place.  See ``DruidOssParent2ImageBase.dependency`` for the verified
contents of the image and why 3.8.6 rather than 3.6.3.

Keeping the hardening in the PR layer is load-bearing: it detaches at one
``${BASE_COMMIT}`` and runs ``git gc --prune=now``, which on a range-shared base
would pin the tag to whichever PR built it first and prune the other nine PRs'
base commits.  The reference does exactly the same.

Verification status
-------------------
Verified against the real trees (2026-09-07), by extracting the repo at each of
the ten base SHAs from ``codeload.github.com``:

* **Patches apply cleanly, 10/10.**  ``git apply --check --whitespace=nowarn``
  of ``test.patch`` alone, and of ``test.patch`` + ``fix.patch`` together, in
  that order, succeeded at every PR's own base commit.
* **Every ``-pl`` module exists, 10/10** -- each derived module list was checked
  against the ``<modules>`` block of that commit's own root pom.
* **Every ``-Dtest=`` name resolves** to a file that exists after ``test.patch``
  is applied, with its ``@Test`` count recorded; the only zero-``@Test`` entries
  are the known helpers and the ``@Ignore``d benchmark listed in
  ``_build_test_flag``.
* **Toolchain read from the artifact, not inferred**: ``io.druid:oss-parent:2``
  was fetched from Maven Central -- ``javac.src.version``/``javac.target.version``
  ``1.7``, ``maven-surefire-plugin`` ``2.18.1``, ``<prerequisites><maven>3.0.3``.
  Ubuntu 22.04's Maven 3.6.3 clears that floor and predates the 3.8.1 plain-HTTP
  repository blocker.
* **Cross-stage test-name stability** was checked on the one PR where it was at
  risk: ``#2753``'s test patch rewrites the ``@Parameterized`` filter tests, but
  ``@Parameterized.Parameters(name = "{0}")`` and the ``testName`` produced by
  ``makeConstructors`` (``bitmaps[%s], indexMerger[%s], finisher[%s]``) are
  byte-identical before and after the patch, so parameterized names do not drift
  between the three graded stages.
* No patch in the dataset contains a binary hunk, so ``git apply`` needs no
  ``--exclude``.

Still unverified: no Docker image has been built and no test has been executed,
so per-PR f2p/p2p/n2p classification, total stage runtime, and arm64 behaviour
of the native-free dependency set remain open.  A single
``OUT=output_druid ./run_pipeline.sh apache__druid_raw_dataset.jsonl`` settles
all three.
"""

import re
import textwrap

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Top-level directories that exist in this era but are NOT Maven reactor
# modules.  Feeding one to `-pl` aborts the whole build with "Could not find the
# selected project in the reactor".  Read off the actual top-level trees at
# #2285 (2016-01) and #3071 (2016-08): `docs` and `publications` are the only
# two directories at the root that the pom never declares as a module -- every
# other root entry is either a module or a file.  `docs` is the one that matters
# in practice: five of the ten fix patches touch `docs/content/...`.
_NON_MODULE_DIRS = frozenset({"docs", "publications"})

# Directories that only aggregate child modules.  The root pom declares the
# children by their two-segment path (`<module>extensions-core/histogram</module>`),
# so `-pl extensions-core` alone would build nothing but the parent POM and run
# zero tests.
#
# All three names are needed because the rename happens INSIDE this range:
# #2285..#2529 still use `extensions/<name>`, #2753..#3284 use
# `extensions-core/<name>` and `extensions-contrib/<name>`.  A name that does
# not exist at a given base commit simply never matches a patch path, so the
# union is safe.  `cloud/` is deliberately absent -- it does not appear until
# long after this range.
_GROUPING_DIRS = frozenset({"extensions", "extensions-core", "extensions-contrib"})

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
    "-Dsurefire.failIfNoSpecifiedTests=false "
    # Wagon-level retry for a dropped or throttled transfer. Cheap insurance on
    # top of the mirror in _MAVEN_MIRROR_SETUP; identical in the warm-up and in
    # all three graded commands, so the stages stay comparable.
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120"
)

# Exported by every script rather than added as ENV layers, so the rendered base
# Dockerfile keeps exactly one ENV instruction (the infrastructure block).
#
#   MAVEN_OPTS  sizes the Maven JVM itself; the `-am` closure of `server` or
#               `indexing-service` is the whole of `processing` plus `common`
#               plus `api`. The forked surefire JVM is sized separately by the
#               root pom's own <argLine> (-Xmx1024m).
#   LC_ALL      the JVM needs it for locale-stable test output. The enhancer's
#               ENV block sets LANG but not LC_ALL.
#
# JAVA_HOME is deliberately NOT exported: the `maven:3.8.6-openjdk-8` base image
# already sets it to /usr/local/openjdk-8, and that value is inherited by every
# RUN and exec. Hard-coding a path here would silently rot if the image moved
# its JDK. Verified on the image: JAVA_HOME=/usr/local/openjdk-8,
# `java -version` -> 1.8.0_342.
_TOOLCHAIN_ENV = 'export MAVEN_OPTS="-Xmx2g"\nexport LC_ALL=C.UTF-8'


# Maven Central rate-limits this host. On 2026-09-07 both repo.maven.apache.org
# and repo1.maven.org answered "HTTP 429 Too Many Requests" (Cloudflare, no
# Retry-After), which killed the warm-up at the very first artifact --
#   [FATAL] Non-resolvable parent POM for io.druid:druid:0.9.0-rc2-SNAPSHOT:
#           Could not transfer io.druid:oss-parent:pom:2 ... status: 429
# -- and prepare.sh's `|| true` then hid it, leaving an empty local repository
# and a PR image that looked built but could not compile anything.
#
# Google's read-only GCS mirror of Central returned 200 for that exact URL at
# the same moment, so `central` is mirrored there. <mirrorOf>central</mirrorOf>
# redirects ONLY the central repository id; any other repository a pom declares
# still resolves normally (none do in this range -- checked all ten root poms
# and oss-parent:2).
#
# Written into ~/.m2 during the image build, so all three graded stages inherit
# it from the image layer without repeating this block.
#
# An existing settings.xml is PRESERVED: the PR Dockerfile's proxy block writes
# one when a proxy is configured, so the mirrors element is spliced in before
# </settings> instead of overwriting the file.
_MAVEN_MIRROR_SETUP = """mkdir -p ~/.m2
_MIRROR='<mirrors><mirror><id>gcs-central</id><name>GCS mirror of Maven Central</name><url>https://maven-central.storage-download.googleapis.com/maven2</url><mirrorOf>central</mirrorOf></mirror></mirrors>'
if [ -f ~/.m2/settings.xml ]; then
    sed -i "s#</settings>#${_MIRROR}</settings>#" ~/.m2/settings.xml
else
    {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">'
        echo "${_MIRROR}"
        echo '</settings>'
    } > ~/.m2/settings.xml
fi"""


def _retry_maven(cmd: str) -> str:
    """Five attempts with a 60s pause, then give up quietly.

    The mirror above removes the usual cause of a 429, but a mirror can throttle
    too and a transient network blip must not cost a whole image build. `-fn`
    means Maven exits 0 on ordinary TEST failures, so a non-zero exit here is a
    genuine infrastructure failure (unresolvable artifact, dead network) and is
    exactly what deserves a retry.

    The trailing `|| true` keeps prepare.sh's contract: the warm-up must never
    fail the image build (QC check 3A).
    """
    lines = [
        "for attempt in 1 2 3 4 5; do",
        f"    if {cmd}; then",
        "        break",
        "    fi",
        '    echo "maven warm-up attempt $attempt failed; sleeping 60s before retry"',
        "    sleep 60",
        "done || true",
    ]
    return "\n".join(lines)


def _patch_paths(patch_text: str) -> list[str]:
    """Repo-relative path of every file touched by a unified diff.

    Reads the ``a/`` side of each ``diff --git`` header.  Splitting on
    whitespace is safe here because no path in this repo contains a space --
    checked across all twenty patches in the dataset.

    ``removeprefix`` rather than ``lstrip("a/")``: ``lstrip`` is character-set
    based, so it would eat the leading ``a`` of a real path such as
    ``aws-common/...`` (a module in every one of the ten trees) and produce
    ``ws-common``, which is not in the reactor.
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

    Two-segment (``extensions-core/kafka-indexing-service``) for children of a
    grouping directory, single-segment (``processing``) otherwise, matching how
    the root pom declares them.
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
            # `extensions-core/pom.xml` has only two segments -- a file in the
            # aggregator itself, not a buildable child.
            if len(segments) >= 3:
                modules.add(f"{top}/{segments[1]}")
            continue
        modules.add(top)
    return modules


def _build_pl_flag(pr: PullRequest) -> str:
    """``-pl <modules> -am`` for the modules this PR touches.

    Both patches are read, so a fix that lands in ``server`` and a test that
    lands in ``extensions-core/kafka-indexing-service`` both end up in the
    reactor.  ``-am`` pulls in their upstream modules, so the selected modules
    still compile against freshly built sources rather than stale jars.
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


def _companion_test_classes(patch_text: str) -> set[str]:
    """``<MainClass>Test`` for every ``src/main`` file a patch touches.

    Druid follows the ``Foo`` -> ``FooTest`` convention throughout this era, so a
    fix that edits ``StringFormatExtractionFn.java`` has its behaviour guarded by
    ``StringFormatExtractionFnTest``.  Selecting those alongside the patch's own
    test files is what gives the graded stages a real baseline.

    Why this is needed (measured on the 2026-09-07 run, before this existed):

    * ``#2285`` and ``#3033`` selected ONLY a test class that ``test.patch``
      creates.  At the base commit that class does not exist, so ``run.sh``
      matched nothing and the report came back ``run_result 0/0/0`` with
      ``p2p_tests`` EMPTY -- no evidence the fix leaves existing behaviour
      intact.  ``#2285``'s fix touches 13 classes in
      ``io.druid.query.extraction``, seven of which already had tests at its
      base commit (``RegexDimExtractionFnTest``, ``JavaScriptExtractionFnTest``,
      ``TimeDimExtractionFnTest``, ``MatchingDimExtractionFnTest``,
      ``SearchQuerySpecDimExtractionFnTest``, ``TimeFormatExtractionFnTest``,
      ``CascadeExtractionFnTest`` -- each verified present upstream).  None were
      being run.
    * ``test_patch_result`` was ``0/0/0`` for seven of the ten PRs, because the
      single module holding the new test fails ``test-compile`` when the test
      references API that only ``fix.patch`` introduces.  Maven's ``-fn`` keeps
      the reactor going, so tests in the OTHER modules a fix touches still run
      -- but only if something selects them.  For ``#2325`` that is ``common``
      and ``server``; ``#2519``/``#2529`` ``indexing-hadoop``; ``#2844``
      ``indexing-service``; ``#2922`` and ``#3284``
      ``extensions-core/kafka-indexing-service``.

    A name that has no corresponding test file simply matches nothing, which is
    harmless because ``-DfailIfNoTests=false`` and
    ``-Dsurefire.failIfNoSpecifiedTests=false`` are always set.

    Not a fix for every case: where the fix and the test live in the SAME single
    module (``#2285``, ``#3033``, ``#2753``), that module still fails to compile
    under ``test.patch`` alone, so ``test_patch_result`` stays ``0/0/0``.  That
    is a property of the PR -- a test that cannot compile without the fix is
    still a valid fail-to-pass signal, and report.py:288-303 classifies it as
    n2p by design.  What this function fixes there is ``run_result``.
    """
    classes: set[str] = set()
    for path in _patch_paths(patch_text):
        if "/src/main/" not in path or not path.endswith(".java"):
            continue
        classes.add(path.rsplit("/", 1)[-1][: -len(".java")] + "Test")
    return classes


def _fallback_test_dirs(patch_text: str) -> set[str]:
    """``src/test/java`` package directories matching the fix's main sources.

    Last-resort scope for a PR where NOTHING the companion rule names actually
    exists.  ``#3033`` is the case: its fix touches ``BucketExtractionFn.java``
    (created by the fix), ``ExtractionCacheHelper.java`` and the ``ExtractionFn``
    interface, and at its base commit none of the three has a test file -- so
    the selector matched nothing, ``run_result`` came back ``0/0/0`` and the
    instance had no p2p baseline at all.

    The same package, ``io/druid/query/extraction``, holds 14 test classes at
    that commit (``RegexDimExtractionFnTest``, ``JavaScriptExtractionFnTest``,
    ``CascadeExtractionFnTest`` ...), every one of them an ``ExtractionFn``
    implementation -- exactly the code a change to the ``ExtractionFn``
    interface could break.  So the fallback is targeted, not filler.

    Returned as SLASH paths because prepare.sh feeds them to ``find``.
    """
    dirs: set[str] = set()
    for path in _patch_paths(patch_text):
        if "/src/main/java/" not in path or not path.endswith(".java"):
            continue
        dirs.add(path.split("/src/main/java/", 1)[1].rsplit("/", 1)[0])
    return dirs


def _selector_resolution_block(pr: PullRequest) -> str:
    """Shell that resolves ``-Dtest=`` ONCE, at the base commit, in prepare.sh.

    Why resolved at runtime rather than baked in as a literal: the config has no
    access to the repo, so it cannot know that ``BucketExtractionFnTest`` does
    not exist while ``RegexDimExtractionFnTest`` does.  prepare.sh runs inside
    the container at ``base.sha``, where the tree IS present, so it can check.

    Why resolved ONCE and shared: the answer is written to
    ``/home/mvn_test_selector`` and sourced by run.sh, test-run.sh and
    fix-run.sh.  Recomputing per stage would be wrong -- test.patch adds test
    files, so stage 2 and 3 would resolve a LARGER set than stage 1 and the
    three stages would no longer be comparable.  Computing at the base commit,
    before any patch, is the only point where all three agree.

    Behaviour is unchanged for the other nine PRs: if any named class exists,
    only the existing names are selected.  Dropping names that match nothing
    changes no test outcome (``-DfailIfNoTests=false`` already made them
    harmless) -- it only avoids the fallback firing when the primary rule
    genuinely found something.

    The fallback emits REAL class names discovered by ``find``, not a surefire
    package pattern.  Package-qualified ``-Dtest=`` syntax differs between
    surefire 2.x (this era) and 3.x; plain class names work in both, so nothing
    here depends on the surefire version.
    """
    always = sorted(_test_classes_from_patch(pr.test_patch))
    candidates = sorted(_companion_test_classes(pr.fix_patch) - set(always))
    dirs = sorted(_fallback_test_dirs(pr.fix_patch))
    if not always and not candidates and not dirs:
        return """echo "MVN_TEST_SELECTOR=" > /home/mvn_test_selector
. /home/mvn_test_selector"""

    return f"""_ALWAYS="{' '.join(always)}"
_CANDIDATES="{' '.join(candidates)}"
_FALLBACK_DIRS="{' '.join(dirs)}"
_sel=""
_present=0
# test.patch's own classes are ALWAYS selected. They are usually absent at the
# base commit -- that is the whole point, test.patch creates them -- so an
# existence check must never drop them or the f2p/n2p signal disappears.
for _c in $_ALWAYS; do
    _sel="$_sel,$_c*"
    if [ -n "$(find . -path "*/src/test/java/*/$_c.java" -print -quit 2>/dev/null)" ]; then
        _present=$((_present+1))
    fi
done
# companion classes are only useful if they actually exist at the base commit.
for _c in $_CANDIDATES; do
    if [ -n "$(find . -path "*/src/test/java/*/$_c.java" -print -quit 2>/dev/null)" ]; then
        _sel="$_sel,$_c*"
        _present=$((_present+1))
    fi
done
# Nothing selected exists yet => run.sh would score 0 and the instance would have
# no p2p baseline. Widen to the package(s) the fix touches.
if [ "$_present" -eq 0 ]; then
    echo "selector: no selected test class exists at this commit; falling back to package scope"
    for _d in $_FALLBACK_DIRS; do
        for _f in $(find . -path "*/src/test/java/$_d/*Test.java" 2>/dev/null); do
            _sel="$_sel,$(basename "$_f" .java)*"
        done
    done
fi
_sel="${{_sel#,}}"
if [ -n "$_sel" ]; then
    echo "MVN_TEST_SELECTOR=-Dtest=$_sel" > /home/mvn_test_selector
else
    echo "MVN_TEST_SELECTOR=" > /home/mvn_test_selector
fi
cat /home/mvn_test_selector
. /home/mvn_test_selector"""


def _build_test_flag(pr: PullRequest) -> str:
    """Scope surefire to the test classes this PR actually touches.

    ``-am`` drags in modules with hundreds of test classes each (``processing``
    alone), and Druid's query and realtime-indexing tests are minutes apiece;
    running all of them four times (warm-up + three graded stages) on two
    architectures is days of build time.  Compilation is left at full scope on
    purpose -- every selected module and its upstreams still compile, so a patch
    that breaks compilation elsewhere is still caught -- and only test EXECUTION
    is narrowed.

    Stated plainly: this shrinks the p2p baseline to the touched test classes.
    f2p/n2p are unaffected, because they come from exactly these classes.

    Each name gets a trailing ``*`` so surefire also matches NESTED static test
    classes: a JUnit container whose ``@Test`` methods all live in inner classes
    compiles to ``Outer$Nested.class``, and a bare ``-Dtest=Outer`` matches only
    the empty outer class and contributes zero tests.

    Several patches here also touch shared test *helpers* that hold no ``@Test``
    of their own -- ``AggregationTestHelper`` and ``TestIndex`` (#2519/#2529),
    ``IndexBuilder`` and the abstract ``BaseFilterTest`` (#2753), ``TestBroker``
    (#2844).  They are included in the selector because they live under
    ``src/test/``; surefire matches them, finds nothing runnable and moves on.
    Harmless, and it keeps the rule "every touched test file is selected" free
    of exceptions.  ``BaseFilterTest`` (#2753) is ``abstract`` -- surefire skips
    it -- and ``OnheapIncrementalIndexBenchmark`` (#2519/#2529) carries a single
    ``@Ignore @Test``, so it is reported skipped in all three stages and costs
    no runtime.  Verified by reading the files at their own base commits.

    Pairs with ``-DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false``
    so modules holding none of the named classes do not fail the build.
    """
    classes = _test_classes_from_patch(pr.test_patch) | _companion_test_classes(
        pr.fix_patch
    )
    if not classes:
        return ""
    return "-Dtest=" + ",".join(f"{c}*" for c in sorted(classes))


def _mvn_scope(pr: PullRequest) -> str:
    """``-pl`` is static; the ``-Dtest=`` selector is expanded from the shell.

    ``${MVN_TEST_SELECTOR}`` is set by ``_selector_resolution_block`` in
    prepare.sh and sourced by the graded scripts, so all four invocations use
    one identical, base-commit-resolved value.  It is left unquoted because it
    is a single whitespace-free token (or empty, in which case it vanishes) --
    the same shape the literal flag had before.
    """
    return " ".join(x for x in (_build_pl_flag(pr), "${MVN_TEST_SELECTOR}") if x)


def _mvn_prepare_cmd(pr: PullRequest) -> str:
    """Warm-up build, run once while the PR image is being built.

    ``clean test`` so the local repository, the reactor's ``target/classes`` and
    ``target/test-classes`` are all fully populated inside the image.  That is
    what keeps the three graded runs down to an incremental recompile.
    """
    return " ".join(
        x for x in (f"mvn clean test {_SUREFIRE_FLAGS}", _mvn_scope(pr)) if x
    )


def _mvn_graded_cmd(pr: PullRequest) -> str:
    """Graded command -- identical string in run.sh, test-run.sh and fix-run.sh.

    No ``clean``: the warm-up already compiled everything, so each stage only
    recompiles what the applied patch changed.

    Deliberately NOT ``-o`` (offline), even though the warm-up populates the
    local repository and no PR here touches a ``pom.xml``.  Surefire resolves
    its *provider* jar at runtime rather than declaring it, and it only does so
    once it has tests to run.  A ``-Dtest=`` selector that matches nothing at
    the base commit -- which is exactly what a PR adding a brand-new test class
    produces, e.g. ``#3033``'s ``BucketExtractionFnTest`` -- means the warm-up
    ran surefire, selected zero tests, and never fetched
    ``org.apache.maven.surefire:surefire-junit4:2.18.1``.  Under ``-o`` the fix
    stage then dies with "Unable to generate classpath ... 1 required artifact
    is missing", scoring 0/0/0 and failing ``Report.check()`` rule 1.  Observed
    on the sibling 890-2209 config's 2026-09-04 run
    (``instances/pr-2209/fix-patch-run.log``); the same selector shape recurs in
    this range, so the lesson is carried over rather than re-learned.
    """
    return " ".join(x for x in (f"mvn test {_SUREFIRE_FLAGS}", _mvn_scope(pr)) if x)


# Dumping surefire's own XML is what gives METHOD-level granularity.  Console
# output is per-CLASS only, which cannot represent a PR that adds new @Test
# methods to a test class that already exists -- the dominant shape in this
# range (#2285, #2325, #2519/#2529, #2753, #2844, #2922, #3071, #3284 all extend
# existing test classes): the class passes both before and after the fix, so it
# lands in p2p while f2p/n2p come out empty.
#
# The XML is also the only complete record here: every root pom in this range
# sets surefire's <redirectTestOutputToFile>true</redirectTestOutputToFile>
# ("our tests are very verbose, let's keep the volume down"), which sends each
# fork's stdout/stderr to target/surefire-reports/<class>-output.txt rather than
# to the console.  The console fallback in parse_log is therefore a genuine
# last resort for this repo, not an equal second path.
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


class DruidOssParent2ImageBase(Image):
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
        # A toolchain image, not a bare OS -- the same shape as the reference
        # base `FROM rust:1.68` (surrealdb_5831_to_241). It already carries
        # everything this era needs, so the base Dockerfile has NO apt block at
        # all and the apt-in-base-vs-PR question does not arise.
        #
        # Verified by running the image:
        #   JAVA_HOME=/usr/local/openjdk-8
        #   openjdk version "1.8.0_342"     -> compiles the -source 1.7 that
        #                                      io.druid:oss-parent:2 requires
        #   Apache Maven 3.8.6              -> clears the poms' <maven>3.0.3
        #                                      prerequisite
        #   git version 2.30.2              -> the clone below needs it
        #
        # 3.8.6 rather than 3.6.3: `maven:3.6.3-jdk-8` publishes linux/amd64
        # ONLY, while `maven:3.8.6-openjdk-8` publishes amd64 + arm64
        # (`docker manifest inspect`), and this pipeline builds multi-arch.
        #
        # Maven 3.8.1+ blocks plain-HTTP repositories via the `maven-default-http-blocker`
        # mirror. Checked and safe: none of the ten root poms declares a
        # <repositories> block, and oss-parent:2 declares neither <repositories>
        # nor <pluginRepositories> -- every artifact resolves from Central over
        # HTTPS.
        return "maven:3.8.6-openjdk-8"

    def image_tag(self) -> str:
        # NOT per-PR. The base stops at `git clone`, so its content is identical
        # for every PR in the range: same base OS, same JDK/Maven packages, same
        # full-history clone. The per-PR work -- checkout of BASE_COMMIT and the
        # history hardening -- lives in the PR image, so nothing here is pinned
        # to one commit and all ten PRs share one build.
        #
        # The range in the tag keeps this distinct from `base-jdk8-legacy`
        # (druid_890_to_2209.py) and from `base-pr-<n>` (druid_0_to_16976.py).
        # Image identity is image_full_name(), so a tag shared with a sibling
        # config would make the build graph silently drop one of the images.
        return "base-3284-to-2285"

    def workdir(self) -> str:
        return "base-3284-to-2285"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Base Dockerfile: infrastructure, clone, CMD. No apt block.

        Byte-for-byte the same shape as the reference base
        ``output2/workdir/surrealdb/surrealdb/images/base-5831-to-241/Dockerfile``:
        syntax directive, ``FROM`` a toolchain image, infrastructure block,
        ``WORKDIR /home/``, ``git clone``, ``WORKDIR /home/<repo>``, ``CMD``.

        No packages are installed because none are missing --
        ``maven:3.8.6-openjdk-8`` already carries JDK 8, Maven and git, exactly
        as ``rust:1.68`` does for the reference. See ``dependency()``.

        This returns a COMPLETE Dockerfile including the BuildKit syntax
        directive, which makes ``DockerfileEnhancer.enhance()`` return it
        verbatim (image.py:317-318). That is load-bearing, not cosmetic: without
        it ``_standardize_repo_fetch()`` would rewrite the clone into
        clone + checkout + hardening and ``_inject_final_sanitize()`` would
        append the hardening block before ``CMD``. Both would pin this
        RANGE-SHARED base to whichever PR built it first, and the hardening's
        ``git gc --prune=now`` would prune the other nine PRs' base commits.
        The reference base likewise carries the directive and a bare clone.

        The clone is written already-parameterised as
        ``RUN git clone "${REPO_URL}" /home/<repo>`` -- the exact form
        ``_standardize_repo_fetch``'s negative lookahead (image.py:379-384)
        skips -- so the file stays enhancer-proof either way. It is a FULL
        clone on purpose: ``#2529``'s base commit lives on
        ``refs/heads/0.9.0``, not on master.

        The infrastructure block is not hand-copied -- it is generated by
        ``DockerfileEnhancer._infrastructure_block()``, so the ARGs, ENV block,
        OCI labels and CA-cert symlinks stay byte-identical to what the enhancer
        emits for every other image in the pipeline.

        ``ARG REPO_URL`` and ``ARG BASE_COMMIT`` are declared (by that block)
        but not consumed here: build_dataset passes BASE_COMMIT as a build arg
        to any image whose ``dependency()`` is a string
        (build_dataset.py:625-629), and an undeclared build arg makes Docker
        warn.  No extra commit ARG is introduced.

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

        body = f"""{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

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


class DruidOssParent2ImageDefault(Image):
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
        return DruidOssParent2ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        # MUST be exactly "pr-<number>", NOT prefixed like the base tag.
        # build_dataset names the exported OCI archive after image_full_name()
        # (build_dataset.py:606), and scripts/batch_ecr_push.py::find_tar()
        # looks the tar up as "mswebench_<org>_m_<repo>_pr-<number>.tar". A
        # prefixed tag yields a file that tool cannot find, and the push fails
        # with "tar not found" for every instance.
        #
        # Safe despite the sibling configs using the same "pr-<n>" form: a PR
        # resolves to exactly one registration key, so two configs are never
        # both instantiated for the same PR number in one run. The base images
        # differ ("base-3284-to-2285" vs "base-jdk8-legacy" vs "base-pr-<n>"),
        # so the shared parent never collides either.
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        # MUST be exactly "pr-<number>", unlike image_tag(). run_instance()
        # names the instance directory after this (build_dataset.py:731-738),
        # and gen_report.collect_report_tasks() only picks up directories that
        # start with "pr-" and whose remainder parses as an int
        # (gen_report.py:357-359). A prefixed workdir builds and runs fine but
        # collects 0 report tasks, so final_report.json comes out all zeros --
        # observed on the sibling config's 2026-09-04 run with workdir
        # "jdk8-legacy-pr-<n>".
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
export CI=true
{_TOOLCHAIN_ENV}

{_MAVEN_MIRROR_SETUP}

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

{_selector_resolution_block(self.pr)}

{_retry_maven(prepare_cmd)}
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
. /home/mvn_test_selector
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
. /home/mvn_test_selector
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
. /home/mvn_test_selector
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
        in the base image because they are per-PR: the block detaches at *this*
        PR's ``base.sha`` and deletes every other ref, so the agent cannot read
        the future of the branch. The block is reused verbatim from the harness
        so it stays in lockstep with the canonical hardening instead of drifting
        from a copy -- including its ``git gc --prune=now``, which is safe here
        precisely because it runs on a per-PR layer rather than on the shared
        base.

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
                # variable as `ENV key="value"` (image.py:115), so the older
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

        # Instruction order mirrors the reference PR Dockerfile
        # (output2/workdir/surrealdb/surrealdb/images/pr-241/Dockerfile):
        #
        #   FROM base -> ARG BASE_COMMIT -> COPY x7 -> prepare.sh
        #   -> reset/checkout -> hardening
        #
        # No WORKDIR: the base image ends on `WORKDIR /home/<repo>`, so every
        # RUN here already starts in the repo root. No trailing CMD either --
        # the base's `CMD ["/bin/bash"]` is inherited. Both match the reference.
        #
        # prepare.sh runs BEFORE the reset/checkout/hardening, as in the
        # reference. That is safe because prepare.sh performs its own
        # `git reset --hard` + `git checkout <base.sha>` before building, so the
        # Maven warm-up happens on the right tree; the Dockerfile's own checkout
        # then resolves to the same commit and the hardening prunes from there.
        # The warm-up's `target/` output is gitignored, so `git reset --hard`
        # afterwards does not discard the populated build cache.
        blocks = [
            f"FROM {name}:{tag}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            proxy_setup.strip(),
            copy_commands.strip(),
            "RUN bash /home/prepare.sh",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
            Image._HARDENING_BLOCK.rstrip("\n"),
            proxy_cleanup.strip(),
            self.clear_env,
        ]
        return "\n\n".join(b for b in blocks if b) + "\n"


@Instance.register("apache", "druid")
@Instance.register("apache", "druid_3284_to_2285")
class DRUID_3284_TO_2285(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DruidOssParent2ImageDefault(self.pr, self._config)

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
        # before the outcome elements are looked for.  Druid's realtime and
        # kafka-indexing tests are heavy log emitters, so this is not
        # hypothetical here.
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
            # test's own file. `#` is also Maven's own selector form
            # (-Dtest=Class#method).
            #
            # It also keeps the two same-named IncrementalIndexTest classes in
            # #2325 (io.druid.segment.data and io.druid.segment.incremental)
            # distinguishable, since the fully qualified classname is preserved
            # on the left of the separator.
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
            # Only 2.18.1 (from oss-parent:2) appears in this era, so the class
            # name is taken from the preceding "Running <class>" line.
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
