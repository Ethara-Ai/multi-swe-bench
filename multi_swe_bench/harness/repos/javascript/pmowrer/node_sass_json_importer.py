"""pmowrer/node-sass-json-importer - Node 13 / yarn / Mocha 7.

Every value below came from running the toolchain in Docker at base commit
cfd8f127, not from reading manifests:

  package.json   "test": "mocha --require @babel/register"
                 devDeps: mocha ^7.0.1, chai ^4.2.0, node-sass >=3.5.3
                 no "engines" field, no .nvmrc
  yarn.lock      present, no package-lock.json  -> yarn, not npm
  .travis.yml    node_js: [13]

Three discoveries drive this file, and none is guessable from the manifests:

1. NODE 13 IS MANDATORY, not a stylistic pin. yarn.lock resolves
   node-sass@4.13.1, whose prebuilt bindings stop at Node 13 (ABI 79). On
   Node 14 (ABI 83) the install fails outright:

       Error: Node Sass does not yet support your current environment:
              Linux 64-bit with Unsupported runtime (83)

   and the node-gyp fallback then dies too, because node-gyp 3.8.0 wants
   Python 2 which modern Debian images no longer ship. The tests import
   node-sass and call sass.renderSync, so a broken binding is fatal, not
   cosmetic.

2. THERE IS DELIBERATELY NO apt-get LINE. Debian buster is archived, so
   `apt-get update` inside node:13-buster fails with exit 100 and three
   "does not have a Release file" errors - an apt line here would BREAK the
   build rather than help it. It is also unnecessary: the image already
   ships git 2.20.1, ca-certificates, and both Python 2.7.16 and 3.7.3.

3. ARM64 NEEDS 4.14.1 AND A SOURCE BUILD. node-sass@4.13.1 refuses arm64
   outright - its lib/extensions.js has no mapping for process.arch ===
   "arm64", so it aborts with

       Error: Node Sass does not yet support your current environment:
              Linux Unsupported architecture (arm64)

   before attempting anything. 4.14.1 adds that mapping and is otherwise the
   SAME COMPILER: both embed libsass 3.5.5, confirmed at runtime through
   sass.info rather than from release notes. That equivalence is what makes
   the bump safe, because the suite compares rendered CSS byte-for-byte with
   to.eql against 'body {\\n  color: #c33; }\\n'. Dart Sass - the obvious
   "modern replacement" - was measured here and fails 23 of 39 tests purely
   on formatting: it emits 'color: #c33;\\n}' where libsass emits
   'color: #c33; }'. Any engine change is therefore ruled out, and only a
   wrapper change is admissible.

   No node-sass release publishes an arm64 prebuilt binary. Every asset URL
   404s, and the installer downloads the 9-byte "Not Found" body, reports
   "Download complete", then fails with "file too short" - so the fallback
   must be an explicit source build. node:13-buster carries the full toolchain
   already (python 2.7.16, gcc/g++ 8.3, make), which is the only reason this
   is possible at all given point 2.

   4.14.1 is installed into /opt and reached through a shim, NOT via
   `yarn add`, because package.json and yarn.lock are tracked: editing them
   would dirty the worktree and fail check_git_changes.sh in every graded
   stage. node_modules is gitignored, so the graded tree stays byte-identical
   to the base commit.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# --reporter tap rather than Mocha's default spec reporter. The spec reporter
# prints `✓ <test name>` with the suite name on a SEPARATE line, so a test name
# alone is not unique across suites and the three stages cannot be compared
# reliably. TAP emits `ok N <full title>` - suite path included, plain ASCII,
# no ANSI colour to strip.
MOCHA_TAP = "npx mocha --require @babel/register --reporter tap"

# 4.13.1 (what yarn.lock pins) -> 4.14.1 is a WRAPPER-ONLY bump. Both embed
# libsass 3.5.5 - confirmed at runtime via sass.info, not from changelogs - so
# CSS serialisation is byte-identical and the suite's exact-output assertions
# are untouched. 4.14.1 is simply the first release whose lib/extensions.js maps
# process.arch === "arm64"; 4.13.1 aborts with
#
#     Error: Node Sass does not yet support your current environment:
#            Linux Unsupported architecture (arm64)
#
# before it even tries to build, which is the single reason arm64 was blocked.
NODE_SASS_VERSION = "4.14.1"

# Kept as its own constant, NOT inlined into prepare.sh: that template runs
# through str.format(), where every literal { and } would need doubling. The JS
# below is dense with braces, and escaping it inline would make the one check
# that guards output correctness unreadable.
#
# This is the gate that makes an engine substitution safe. The tests compare
# rendered CSS with to.eql against exact strings, so a compiler whose formatting
# differs does not fail loudly - it silently turns ~23 passes into failures that
# look like real regressions. Dart Sass, for instance, emits
# 'body {\n  color: #c33;\n}' where libsass emits 'body {\n  color: #c33; }\n'.
# Asserting the exact byte sequence here means such a swap breaks the BUILD,
# on whichever arch it happens, instead of quietly corrupting the grade.
SASS_VERIFY = r'''node -e '
const sass = require("node-sass");
const out  = sass.renderSync({data: "body { color: #c33; }"}).css.toString();
const want = "body {\n  color: #c33; }\n";
if (out !== want) {
  console.error("FATAL: CSS serialisation changed -> " + JSON.stringify(out));
  console.error("       expected libsass formatting -> " + JSON.stringify(want));
  process.exit(1);
}
console.log("node-sass OK: " + sass.info.split("\n")[0].trim());
console.log("engine      : " + sass.info.split("\n")[1].trim());
console.log("output format verified byte-for-byte");
'
'''


class NodeSassJsonImporterImageBase(Image):
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
        # Pinned to 13 because node-sass@4.13.1 has no binding for a newer ABI.
        # See the module docstring - this is a hard requirement, not a preference.
        return "node:13-buster"

    def image_tag(self) -> str:
        # Per-PR. DockerfileEnhancer injects a hardening block that detaches at
        # one ${BASE_COMMIT} and prunes every other object, so a shared tag would
        # let whichever PR built first pin the commit for all the others - every
        # other PR would then find its own base commit already gone.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

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

        return f"""\
FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV CI=true

WORKDIR /home/

{code}

{self.clear_env}

"""


class NodeSassJsonImporterImageDefault(Image):
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
        return NodeSassJsonImporterImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
# Assert the reset actually produced a clean tree rather than assuming it did.
# A stray modified file would flow into all three graded stages and corrupt the
# comparison with nothing in the log to explain why.
bash /home/check_git_changes.sh

git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# --frozen-lockfile so yarn.lock decides every version, keeping the dependency
# graph exactly as the base commit resolved it.
#
# --ignore-scripts is what makes arm64 possible at all. node-sass@4.13.1's
# postinstall aborts with "Unsupported architecture (arm64)" and would take the
# whole install down with it. Skipping postinstall is safe here specifically
# because node-sass is the ONLY native dependency in the tree - verified by
# finding exactly one binding.gyp anywhere under node_modules - so no other
# package loses anything.
#
# `timeout 1800` covers a case `|| true` cannot: `|| true` handles a command
# that FAILS, but a command that HANGS never returns and never reaches `||`.
# Docker has no per-step timeout, so a stalled install would block forever.
#
# `|| true` still matters on its own - yarn exits non-zero on optional-dependency
# noise (fsevents is darwin-only and always "fails" on linux) even when every
# real dependency installed correctly.
if timeout 1800 yarn install --frozen-lockfile --ignore-scripts > /tmp/install.log 2>&1; then
  echo "install: OK" > /home/.install_status
else
  echo "install: INCOMPLETE (exit $?)" > /home/.install_status
  tail -20 /tmp/install.log || true
fi
cat /home/.install_status

# Now supply a node-sass that can actually produce a binding on BOTH arches.
#
# It goes in /opt and is reached through a one-line re-export rather than via
# `yarn add`, because package.json and yarn.lock are TRACKED files: editing them
# would dirty the worktree and fail check_git_changes.sh in all three graded
# stages. node_modules is gitignored, so replacing an entry inside it leaves the
# graded tree byte-identical to the base commit - confirmed by git status
# --porcelain staying empty after the swap.
#
# On arm64 the binary must be compiled: NO node-sass release publishes an arm64
# prebuild. Every asset URL 404s, and the installer cheerfully downloads the
# 9-byte "Not Found" body, prints "Download complete", then fails with "file too
# short". --build-from-source skips that misleading round trip. node:13-buster
# already ships the whole toolchain (python 2.7.16, gcc/g++ 8.3, make), which is
# essential because buster's apt is archived - `apt-get update` exits 100, so a
# missing build tool here would be unobtainable.
#
# `timeout 7200` because compiling libsass under QEMU emulation runs roughly an
# order of magnitude slower than native. A native arm64 compile is ~3 minutes,
# so emulated is plausibly 30-60; 7200 leaves real headroom while still bounding
# a genuine hang, which `|| true` could never catch because a hung command never
# returns to be caught.
#
# PYTHON is pinned explicitly rather than left to discovery. node-gyp 3.8.0 runs
# on Python 2 only and probes for a bare `python`; buster does provide
# /usr/bin/python -> 2.7, but that is a distro convention, not a guarantee, and
# if it ever resolved to python3 the build would die with "not supported". Since
# apt is archived here, a wrong interpreter would be unfixable at build time.
# Naming the interpreter costs nothing and removes the dependency on convention.
#
# JOBS caps compiler parallelism. node-gyp otherwise fans out across every
# visible core, and several concurrent g++ processes compiling libsass under
# emulation is exactly the memory spike that killed earlier attempts in this VM.
if [ "$(uname -m)" = "aarch64" ]; then
  SASS_BUILD="--build-from-source"
else
  SASS_BUILD=""
fi
export PYTHON=/usr/bin/python2
export JOBS=2
mkdir -p /opt/node-sass
timeout 7200 npm install --no-save $SASS_BUILD --prefix /opt/node-sass node-sass@{ns}

rm -rf node_modules/node-sass
mkdir -p node_modules/node-sass
printf '{{"name":"node-sass","version":"{ns}","main":"index.js"}}' > node_modules/node-sass/package.json
echo 'module.exports = require("/opt/node-sass/node_modules/node-sass");' > node_modules/node-sass/index.js

# Hard gate, deliberately stricter than "does it load".
#
# An install can "succeed" while leaving node-sass without a usable binding, and
# that surfaces three stages later as an unexplained 0/0/0 rather than as an
# install error. Worse, a binding can load perfectly and still be WRONG: the
# tests compare rendered CSS against exact strings with to.eql, so a compiler
# that formats output differently produces ~23 failures that look like genuine
# regressions. So this asserts the exact byte sequence, not just importability -
# a bad substitution breaks the build instead of corrupting the grade.
{sass_verify}

# node_modules is gitignored, so installing it does not dirty the tree.
git checkout -- . || true
bash /home/check_git_changes.sh
""".format(pr=self.pr, ns=NODE_SASS_VERSION, sass_verify=SASS_VERIFY),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{mocha}
""".format(pr=self.pr, mocha=MOCHA_TAP),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{mocha}
""".format(pr=self.pr, mocha=MOCHA_TAP),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
{mocha}
""".format(pr=self.pr, mocha=MOCHA_TAP),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Generated from files() rather than hard-coded, so a file added there can
        # never be written into the build context yet left uncopied - which would
        # surface at build time as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


def parse_mocha_tap_log(test_log: str) -> TestResult:
    """Parse `mocha --reporter tap` output.

    Captured verbatim from the container at base commit cfd8f127:

        1..45
        ok 1 node-sass-json-importer provides the default export when using node require to import
        ok 3 Import type test (JSON) imports strings
        not ok 15 Import type test (JSON) allows case conversion
        # tests 45
        # pass 45
        # fail 0

    Note Mocha's TAP has NO `-` between the number and the description, unlike
    AVA's `ok 1 - name`. The description is the test's FULL title (suite path
    included), which is what makes names comparable across the three stages.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # `ok 12 some name` / `not ok 12 some name`. An optional ` - ` is tolerated so
    # the same parser survives a Mocha version that emits the AVA-style dash.
    result_re = re.compile(r"^(ok|not ok)\s+(\d+)\s*-?\s+(.*)$")
    # TAP directive form: `ok 3 name # SKIP reason`
    skip_re = re.compile(r"^(ok|not ok)\s+(\d+)\s*-?\s+(.*?)\s+#\s*(SKIP|TODO)\b.*$", re.I)

    ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    for raw_line in test_log.split("\n"):
        line = ansi_escape.sub("", raw_line).strip()
        if not line:
            continue

        # The plan (`1..45`) and the trailing `# tests/pass/fail` summary carry no
        # per-test information; counting them would invent test names.
        if line.startswith("1..") or line.startswith("#"):
            continue

        skip_match = skip_re.match(line)
        if skip_match:
            name = skip_match.group(3).strip()
            if name and name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)
            continue

        match = result_re.match(line)
        if not match:
            continue

        status, _, name = match.groups()
        name = name.strip()
        if not name:
            continue

        if status == "not ok":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        else:
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("pmowrer", "node-sass-json-importer")
class NodeSassJsonImporter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NodeSassJsonImporterImageDefault(self.pr, self._config)

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
        return parse_mocha_tap_log(test_log)