"""ant-design/ant-design interval config for PRs 15868..18715.

Selected via `number_interval` ("ant_design_18715_to_15868"), not via the plain
`repo` name, so it does not disturb the existing ant-design configs.

Image layout follows the shared-base / per-PR-checkout pattern:

  * base image  - one full-history clone shared by every PR in the interval.
    It deliberately does NOT check out or prune to a single commit, so the
    marker string that `DockerfileEnhancer._inject_final_sanitize` looks for is
    kept in a comment; that stops the enhancer from pinning this shared base to
    whichever PR happened to win the base-image dedup in `run_mode_image`.
  * PR image    - checks out its own BASE_COMMIT (baked in as an ARG default,
    because build_dataset only passes REPO_URL/BASE_COMMIT build args to base
    images), runs prepare.sh, then applies the history hardening at the end so
    the shipped image still carries exactly one commit of history.

Toolchain and prepare/run scripts are the era-correct ones from
`ant_design_17846_to_10891` (node:11 with pinned jsdom/enzyme/cheerio and
ts-jest diagnostics disabled), which covers both halves of this interval.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_LINE = re.compile(r"^(PASS|FAIL)\s+(\S+)")
_TEST_LINE = re.compile(r"^(\s+)([✓✔✕✗×○◌])\s+(.*\S)\s*$")
_HEADING_LINE = re.compile(r"^(\s+)(\S.*\S|\S)\s*$")
_TIMING_SUFFIX = re.compile(r"\s+\(\d+(?:\.\d+)?\s*m?s\)$")
# Failure detail, code frames, stack lines and console output are indented exactly
# like a describe heading; none of them may enter the describe path.
_NOT_A_HEADING = re.compile(r"^(?:[●>+|-]|at\s|console\.|Warning:|\d+\s*\||expect\()")


def parse_jest_log_stable(test_log: str) -> TestResult:
    """Parse Jest verbose output into *stable, unique* test identifiers.

    The shared `parse_jest_log` keys tests on Jest's leaf title alone, which breaks
    this repo two ways:

    * ant-design reuses leaf titles across suites - `normal`, `prefixCls` and
      `configProvider` each occur dozens of times in
      components/config-provider/__tests__/components.test.js alone - so distinct
      tests collapse onto one key.
    * Its timing-stripping branch is guarded by `name not in failed_tests`. Once any
      suite has a failing `prefixCls`, that guard sends every *passing* `prefixCls`
      down the fallback branch, which stores the raw `'prefixCls (6ms)'`. Timings
      differ between the run/test/fix stages, so the same test is a different key in
      each stage: it reads as absent at run and test and brand-new at fix, and its
      FAIL -> PASS transition is never credited (f2p/s2p/n2p all come out empty).

    Keying on `<file> > <describe...> > <test>` with the timing always stripped makes
    identity stable across stages. Measured on pr-16532: every one of the 1437 passing
    ids at the run stage is present at the test stage, and no id retains a timing
    suffix. File-level PASS/FAIL entries are kept, as the shared parser does, so a
    whole suite that flips is still credited.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    current_file: str | None = None
    describes: dict[int, str] = {}

    for raw_line in _ANSI.sub("", test_log).split("\n"):
        line = raw_line.rstrip()

        match = _FILE_LINE.match(line)
        if match:
            current_file = match.group(2)
            describes = {}
            if match.group(1) == "FAIL":
                failed_tests.add(current_file)
            else:
                passed_tests.add(current_file)
            continue

        match = _TEST_LINE.match(line)
        if match:
            indent, symbol = len(match.group(1)), match.group(2)
            name = _TIMING_SUFFIX.sub("", match.group(3)).strip()
            path = [describes[key] for key in sorted(describes) if key < indent]
            test_id = " > ".join([current_file or "<unknown>"] + path + [name])
            if symbol in "✓✔":
                passed_tests.add(test_id)
            elif symbol in "○◌":
                skipped_tests.add(test_id)
            else:
                failed_tests.add(test_id)
            continue

        match = _HEADING_LINE.match(line)
        if match and current_file:
            text = match.group(2)
            if _NOT_A_HEADING.match(text) or " › " in text:
                continue
            indent = len(match.group(1))
            describes = {k: v for k, v in describes.items() if k < indent}
            describes[indent] = text

    # Worst status wins - the sets must stay mutually exclusive.
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


# The shell blocks below are kept out of the `.format()` templates because the embedded
# shell and JS are full of braces.

# Every version pin this interval needs, in the order they must be applied. Applied
# once, by prepare.sh at build time. Nothing may run `npm install` after this point:
# npm 6 recomputes the whole tree on every install and would drag these back to their
# drifted versions, which is why patch dependencies are unpacked rather than installed.
_PIN_DEPS = """
# Pin transitive deps to prevent version drift (no lockfile in repo)
npm install --no-save --legacy-peer-deps jsdom@11.12.0 jest-environment-jsdom@24.9.0 nwsapi@2.2.2 2>/dev/null || true

# Pin the babel-jest chain to the jest-24 era.
#
# babel-plugin-jest-hoist >= 25 rewrites a hoisted `jest.mock(...)` into a
# `_getJestObj()` helper that does `require('@jest/globals')`. That package does not
# exist in jest 23/24, so every suite that calls jest.mock() dies at load time with
#   Cannot find module '@jest/globals' from 'components.test.js'
# and the whole file's tests are never captured at all (7+ suites, incl. menu,
# config-provider, modal and the demo suites). The repo declares jest ^24.8.0 but has
# no lockfile, so @ant-design/tools' codePreprocessor pulls a modern babel-jest.
# Pinning the three packages to 24.9.0 restores the plain hoist that needs no
# @jest/globals.
npm install --no-save --legacy-peer-deps babel-jest@24.9.0 babel-preset-jest@24.9.0 babel-plugin-jest-hoist@24.9.0 2>/dev/null || true

# Pin cheerio to the era-correct 1.0.0-rc.2.
#
# enzyme's `render()` returns a cheerio wrapper, and these suites snapshot it
# (`expect(render(...)).toMatchSnapshot()`). The committed .snap files were generated
# with the cheerio 1.0.0-rc.x that enzyme resolved in 2019, which parses through parse5
# and preserves SVG attribute case. cheerio 0.22.0 parses through htmlparser2, which
# lowercases every attribute name - `viewBox` becomes `viewbox` - and that single
# systemic diff failed ~750 snapshot assertions at the baseline stage, before any patch.
# A plain `^1.0.0-rc.2` is not usable either: npm resolves it to cheerio 1.1.x, whose
# `catch {}` / `node:stream` syntax needs Node 16. The exact rc.2 keeps parse5
# semantics and stays ES5-safe on Node 11. Must run AFTER all other npm installs.
# `|| true` on every line: prepare.sh is `set -e`, and find exits non-zero when a
# directory it is walking disappears under it.
find node_modules -path "*/node_modules/cheerio" -type d -exec rm -rf {} + 2>/dev/null || true
rm -rf node_modules/cheerio node_modules/.package-lock.json 2>/dev/null || true
npm install --no-save --legacy-peer-deps cheerio@1.0.0-rc.2 2>/dev/null || true
# Any nested copy npm re-created must be rc.2 as well, or enzyme picks up 1.1.x again.
for nested in $(find node_modules -mindepth 3 -path "*/node_modules/cheerio" -type d 2>/dev/null || true); do
  rm -rf "$nested"
  cp -r node_modules/cheerio "$nested"
done
"""

_INSTALL_PATCH_DEPS = """
# A gold patch may introduce a runtime dependency: PR #16532 adds `rc-mentions` to
# package.json alongside components/mentions/index.tsx. npm install ran in prepare.sh
# BEFORE any patch was applied, so the package is absent at test/fix time and the new
# suite dies at load time with "Cannot find module 'rc-mentions' from 'index.tsx'".
# The suite never executes, so no test in it can go FAIL -> PASS and the PR yields no
# f2p at all.
#
# The package is unpacked straight into node_modules instead of being installed with
# npm. npm 6 recomputes the whole tree on EVERY install, including a single-package
# one, and the two needs are mutually destructive: `npm install rc-mentions@~0.3.1`
# reported "removed 18 packages, updated 5 packages" and restored cheerio 1.1.x, whose
# `node:stream` import does not exist on Node 11 (304 suites dead, fix stage 0 passed /
# 152 failed); re-pinning afterwards with `npm install jsdom@11.12.0 ...` then reported
# "removed 24 packages" and took rc-mentions back out again. `npm pack` only downloads
# a tarball - it resolves nothing and touches no other package - so the pins that
# prepare.sh baked into the image survive untouched.
#
# Verified inside the pr-16532 image: every dependency of rc-mentions@0.3.1
# (@ant-design/create-react-context 0.2.6, babel-runtime 6.26.0, classnames 2.2.6,
# rc-menu 7.4.32, rc-trigger 2.6.5, rc-util 4.21.1, react-lifecycles-compat 3.0.4) is
# already present at a satisfying version, and cheerio is 1.0.0-rc.2.
node -e '
  var fs = require("fs");
  var after = JSON.parse(fs.readFileSync("package.json", "utf8"));
  var before = JSON.parse(fs.readFileSync("/home/package.before.json", "utf8"));
  var had = Object.assign({}, before.dependencies, before.devDependencies);
  var has = Object.assign({}, after.dependencies, after.devDependencies);
  var added = Object.keys(has).filter(function (name) { return !(name in had); });
  if (added.length) {
    process.stdout.write(added.map(function (n) { return n + " " + n + "@" + has[n]; }).join("\\n") + "\\n");
  }
' > /home/new_deps.txt 2>/dev/null || true
if [ -s /home/new_deps.txt ]; then
  # Set by the caller; fall back to the checkout we are already standing in, so an
  # unset value can never send `mkdir -p`/`tar -C` to /node_modules at the root.
  REPO_DIR="${REPO_DIR:-$(pwd)}"
  echo "dependencies added by the patch:"
  cat /home/new_deps.txt
  mkdir -p /home/patch_deps
  while read -r dep_name dep_spec; do
    [ -n "$dep_name" ] || continue
    if [ -d "$REPO_DIR/node_modules/$dep_name" ]; then
      echo "  $dep_name already resolved in the tree, leaving it alone"
      continue
    fi
    dep_tgz=$(cd /home/patch_deps && npm pack "$dep_spec" 2>/dev/null | tail -1)
    if [ -n "$dep_tgz" ] && [ -f "/home/patch_deps/$dep_tgz" ]; then
      mkdir -p "$REPO_DIR/node_modules/$dep_name"
      tar -xzf "/home/patch_deps/$dep_tgz" --strip-components=1 -C "$REPO_DIR/node_modules/$dep_name" || true
      echo "  unpacked $dep_spec from $dep_tgz"
    else
      echo "  WARNING: could not fetch $dep_spec; its suites will fail to load"
    fi
  done < /home/new_deps.txt
fi
"""

# The .jest.js rewrite, kept out of `.format()` for the same reason.
_PATCH_JEST_CONFIG = """
npm run version || true

node << 'PATCHEOF' || true
const fs = require('fs');
try {
  let c = fs.readFileSync('.jest.js', 'utf8');
  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  let changed = false;
  for (const m of needed) {
    if (!c.includes("'" + m + "'")) {
      c = c.replace('const compileModules = [', "const compileModules = [\\n  '" + m + "',");
      changed = true;
    }
  }
  // Turn OFF ts-jest type-checking.
  //
  // The base commits at the young end of this interval declare jest ^23 and transitively
  // resolve ts-jest 23.10.5, which type-checks every suite and FAILS the suite on any
  // TypeScript error. The commits at the old end declare jest ^24, resolve no ts-jest at
  // all, and therefore never type-check. So type-checking is already inconsistent across
  // the interval, and it is applied to exactly the half that then cannot produce results.
  //
  // Disabling diagnostics makes the whole interval behave the way its jest-24 half already
  // does. The graded artifact is the jest result, not the type check.
  if (!/diagnostics/.test(c)) {
    if (/globals\\s*:/.test(c)) {
      c = c.replace(/'ts-jest'\\s*:\\s*{/, "'ts-jest': { diagnostics: false,");
    } else {
      c = c.replace(/module\\.exports\\s*=\\s*{/,
                    "module.exports = {\\n  globals: { 'ts-jest': { diagnostics: false } },");
    }
    changed = true;
  }
  if (changed) { fs.writeFileSync('.jest.js', c); console.log('Patched .jest.js ESM modules'); }
} catch(e) { console.log('No .jest.js to patch'); }
PATCHEOF
"""


class AntDesignImageBase_ANT_DESIGN_18715_TO_15868(Image):
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
        return "node:11"

    def image_tag(self) -> str:
        return "base-18715_to_15868_fullhist"

    def workdir(self) -> str:
        return "base-18715_to_15868_fullhist"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

# History hardening is deferred to the per-PR image, which ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to a single PR's BASE_COMMIT.

{self.clear_env}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_18715_TO_15868(Image):
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
        return AntDesignImageBase_ANT_DESIGN_18715_TO_15868(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Baseline manifest, read back by test-run.sh / fix-run.sh to find the dependencies a
# patch adds. Must be taken before npm install touches anything.
cp package.json /home/package.before.json

npm install --legacy-peer-deps || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha)
                + _PIN_DEPS
                + _PATCH_JEST_CONFIG,
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
npx jest --config .jest.js --no-cache --verbose || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

REPO_DIR=/home/{repo}
cd "$REPO_DIR"
git apply --whitespace=nowarn /home/test.patch
""".format(repo=self.pr.repo)
                + _INSTALL_PATCH_DEPS
                + """
npx jest --config .jest.js --no-cache --verbose || true
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

REPO_DIR=/home/{repo}
cd "$REPO_DIR"
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
""".format(repo=self.pr.repo)
                + _INSTALL_PATCH_DEPS
                + """
npx jest --config .jest.js --no-cache --verbose || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "AntDesignImageDefault_ANT_DESIGN_18715_TO_15868 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


@Instance.register("ant-design", "ant-design")
class ANT_DESIGN_18715_TO_15868(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AntDesignImageDefault_ANT_DESIGN_18715_TO_15868(self.pr, self._config)

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
        return parse_jest_log_stable(test_log)
