from typing import Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log

# Two dataset rows cannot be applied with a plain `git apply`: PR 29761 records a
# base.sha at which its test patch is already merged, and PR 30450's fix patch conflicts
# on unrelated package.json churn. Under `set -e` that aborts before jest runs, so the
# instance grades as "unresolved" with zero captured tests rather than failing loudly.
# Injected as a .format() VALUE, so braces here are literal and must stay single.
_APPLY_PATCH_FN = """apply_patch() {
  p="$1"
  git apply --whitespace=nowarn "$p" && return 0
  git apply -R --check --whitespace=nowarn "$p" 2>/dev/null && { echo "apply_patch: already applied: $p"; return 0; }
  git apply --3way --whitespace=nowarn "$p" && { echo "apply_patch: 3-way merged: $p"; return 0; }
  patch --batch --fuzz=5 -p1 -i "$p" && { echo "apply_patch: fuzzy applied: $p"; return 0; }
  echo "apply_patch: FAILED: $p" >&2
  return 1
}"""

# Jest runs all ~2.5k tests by default, so each stage is dominated by suites the PR
# never touches, and the enzyme / rc-* timing tests in that tail flip under load.
# report.py scores a test that passed before the fix patch and failed after it as an
# invalid instance, so an unrelated tooltip test can void a config-provider PR. Scope
# every stage to the gold test files named by the test patch itself, parsed from the
# unified diff at run time so no repo, PR or test specific knowledge is embedded.
_JEST_RUN_FN = r"""jest_targets() {
  awk '
    /^diff --git / { l=$0; sub(/^diff --git /,"",l); i=index(l," b/");
                     if (i>0) { p=substr(l,i+3); gsub(/"/,"",p); print p } next }
    /^\+\+\+ /     { p=$0; sub(/^\+\+\+ /,"",p); sub(/\t.*$/,"",p); gsub(/"/,"",p);
                     sub(/^[ab]\//,"",p);
                     if (p!="" && p!="/dev/null") print p; next }
  ' "$1" \
    | sed -e 's#/__snapshots__/\([^/]*\)\.snap$#/\1#' \
    | grep -E '\.(test|spec)\.(js|jsx|ts|tsx)$' \
    | sort -u
}

run_jest() {
  local want f
  local -a found=()
  want="$(jest_targets /home/test.patch)"
  if [ -z "$want" ]; then
    echo "run_jest: test.patch names no jest test file; refusing to fall back to the full suite" >&2
    exit 1
  fi
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] && found+=("$f")
  done <<< "$want"
  echo "run_jest: gold scope ${#found[@]} of $(printf '%s\n' "$want" | wc -l | tr -d ' '): ${found[*]}"
  if [ "${#found[@]}" -eq 0 ]; then
    echo "run_jest: no gold test file exists at this stage; skipping jest"
    return 0
  fi
  npx jest --config .jest.js --cache=false --verbose \
    --runInBand --testTimeout=30000 --runTestsByPath "${found[@]}" || true
}
"""

# Some fix patches deliver their entire behavioural change as a `dependencies`
# bump in package.json (measured: PR 30381 fixes an input-number bug solely via
# rc-input-number ~7.0.1 -> ~7.1.0). npm install runs at image-build time, so a
# run-time package.json edit is inert unless node_modules is reconciled here.
# `npm install` must NOT be used for that: it reifies the whole ideal tree and
# re-floats cheerio, which reintroduces the parse5 `node:` protocol failure that
# pin_cheerio exists to prevent. Extracting one tarball over one directory is the
# only mechanism that leaves the build-time tree provably untouched.
_SYNC_DEPS_FN = r"""
__fetch_into_node_modules() {
  name="$1"; spec="$2"
  tmp="$(mktemp -d)" || return 1
  if ! npm pack "$name@$spec" --pack-destination "$tmp" >/dev/null 2>&1; then
    echo "sync_runtime_deps: WARNING npm pack failed for $name@$spec; keeping installed copy"
    rm -rf "$tmp"; return 1
  fi
  tgz="$(find "$tmp" -maxdepth 1 -name '*.tgz' -print -quit)"
  if [ -z "$tgz" ]; then
    echo "sync_runtime_deps: WARNING no tarball for $name@$spec"
    rm -rf "$tmp"; return 1
  fi
  mkdir -p "$tmp/x"
  if ! tar -xzf "$tgz" -C "$tmp/x" || [ ! -d "$tmp/x/package" ]; then
    echo "sync_runtime_deps: WARNING bad tarball for $name@$spec"
    rm -rf "$tmp"; return 1
  fi
  rm -rf "node_modules/$name"
  mkdir -p "node_modules/$name"
  cp -a "$tmp/x/package/." "node_modules/$name/"
  got="$(node -p "require(\"./node_modules/$name/package.json\").version" 2>/dev/null || echo unknown)"
  echo "sync_runtime_deps: $name -> $got"
  rm -rf "$tmp"; return 0
}

sync_runtime_deps() {
  plan="$(node -e '
const fs = require("fs");
const path = require("path");
const root = process.cwd();
const nm = path.join(root, "node_modules");
let semver;
try { semver = require(path.join(nm, "semver")); }
catch (e) { console.error("sync_runtime_deps: semver not resolvable; skipping"); process.exit(0); }
let pkg;
try { pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")); }
catch (e) { process.exit(0); }
const deps = pkg.dependencies || {};
const out = [];
for (const name of Object.keys(deps)) {
  const range = deps[name];
  if (typeof range !== "string" || !semver.validRange(range)) continue;
  let cur = null;
  try { cur = JSON.parse(fs.readFileSync(path.join(nm, name, "package.json"), "utf8")).version; }
  catch (e) { cur = null; }
  if (cur === null) { out.push(name + "\t" + range + "\tabsent"); continue; }
  if (!semver.satisfies(cur, range)) out.push(name + "\t" + range + "\t" + cur);
}
process.stdout.write(out.join("\n"));
' 2>/dev/null)" || plan=""

  if [ -z "$plan" ]; then
    echo "sync_runtime_deps: runtime dependencies already satisfy package.json (no-op)"
    return 0
  fi

  refreshed=""
  while IFS="$(printf '\t')" read -r name range installed; do
    [ -z "$name" ] && continue
    echo "sync_runtime_deps: $name installed=$installed declared=$range -> refreshing"
    if __fetch_into_node_modules "$name" "$range"; then
      refreshed="$refreshed $name"
    fi
  done <<< "$plan"

  for name in $refreshed; do
    missing="$(node -e '
const fs = require("fs");
const path = require("path");
const nm = path.join(process.cwd(), "node_modules");
const target = process.argv[1];
let d = {};
try { d = JSON.parse(fs.readFileSync(path.join(nm, target, "package.json"), "utf8")).dependencies || {}; }
catch (e) { process.exit(0); }
const out = [];
for (const k of Object.keys(d)) {
  if (!fs.existsSync(path.join(nm, k, "package.json"))
      && !fs.existsSync(path.join(nm, target, "node_modules", k, "package.json"))) {
    out.push(k + "\t" + d[k]);
  }
}
process.stdout.write(out.join("\n"));
' "$name" 2>/dev/null)" || missing=""
    [ -z "$missing" ] && continue
    while IFS="$(printf '\t')" read -r dep drange; do
      [ -z "$dep" ] && continue
      echo "sync_runtime_deps: $name requires missing $dep@$drange"
      __fetch_into_node_modules "$dep" "$drange" || true
    done <<< "$missing"
  done
  return 0
}
"""


class AntDesignImageBase_ANT_DESIGN_35705_TO_30656(Image):
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
        return "node:16"

    # Deliberately NOT era-scoped. Every ant-design era base renders a byte-identical
    # Dockerfile (node:16 + full-history clone), and the clone is unpinned, so one
    # shared "base" image serves all eras. Re-adding an era suffix here would rebuild
    # and store the same 2 GB image once per era for zero behavioural gain.
    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The syntax directive opts this Dockerfile out of DockerfileEnhancer's
        # rewriting. That is load-bearing: this base image is SHARED by every PR in
        # the era, so _standardize_repo_fetch() would pin it to whichever PR built it
        # first and then `git gc --prune=now` away every other base commit, breaking
        # `git checkout {base_sha}` in prepare.sh for all the remaining PRs.
        # The clone therefore keeps full history; the per-PR layer does the hardening.
        infra = DockerfileEnhancer._infrastructure_block(self, image_name, True)

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{infra}
WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_35705_TO_30656(Image):
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
        return AntDesignImageBase_ANT_DESIGN_35705_TO_30656(self.pr, self.config)

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

npm install --legacy-peer-deps || true

# Pin transitive deps to prevent version drift (no lockfile in repo)
npm install --no-save --legacy-peer-deps jsdom@20.0.3 nwsapi@2.2.16 2>/dev/null || true

# @ant-design/tools' jest codePreprocessor calls babel-jest with the signature of whichever
# jest major it was released against. package.json floats the tools range (no lockfile), so a
# jest-27-only tools release lands in a jest-26 tree and every suite dies with
# "Cannot read properties of undefined (reading 'cwd')". Resolve the newest release inside the
# declared range whose babel-jest major matches the installed jest major.
JEST_MAJOR=$(node -p "require('jest/package.json').version.split('.')[0]" 2>/dev/null || echo "")
TOOLS_RANGE=$(node -p "require('./package.json').devDependencies['@ant-design/tools']" 2>/dev/null || echo "")
TOOLS_PICK=""
if [ -n "$JEST_MAJOR" ] && [ -n "$TOOLS_RANGE" ]; then
  # npm's output is staged through a FILE, never a pipe into `node -e`. Under QEMU
  # emulation (cross-arch image builds) reading fd 0 intermittently returns
  # "EAGAIN: resource temporarily unavailable", which yields an empty pick, skips the
  # pin, and resurrects the babel-jest mismatch several build steps later.
  for _try in 1 2 3 4 5; do
    rm -f /tmp/tools_view.json
    npm view "@ant-design/tools@$TOOLS_RANGE" version dependencies.babel-jest --json > /tmp/tools_view.json 2>/dev/null || true
    if [ -s /tmp/tools_view.json ]; then
      TOOLS_PICK=$(node -e '
const raw = require("fs").readFileSync("/tmp/tools_view.json", "utf8").trim();
if (!raw) {{ process.exit(0); }}
const parsed = JSON.parse(raw);
const entries = Array.isArray(parsed) ? parsed : [parsed];
const want = process.argv[1];
let pick = "";
for (const e of entries) {{
  const bj = e["dependencies.babel-jest"] || (e.dependencies && e.dependencies["babel-jest"]);
  if (!bj) continue;
  if (String(bj).replace(/[^0-9.]/g, "").split(".")[0] === want) pick = e.version;
}}
process.stdout.write(pick);
' "$JEST_MAJOR" 2>/dev/null || echo "")
    fi
    if [ -n "$TOOLS_PICK" ]; then break; fi
    sleep 3
  done
fi
echo "toolchain: jest major=$JEST_MAJOR tools range=$TOOLS_RANGE pick=$TOOLS_PICK"

# Force cheerio@1.0.0-rc.10 everywhere — enzyme resolves cheerio to 1.2.0 which needs parse5-parser-stream (node: protocol)
# cheerio and tools are installed in ONE npm transaction: this function deletes
# node_modules/.package-lock.json, so any separate install would re-resolve the tree from
# package.json and undo the other pin. package.json itself must stay pristine — 14 of the 20
# fix_patches modify it, so rewriting it would break git apply.
pin_cheerio() {{
  find node_modules -path "*/node_modules/cheerio" -type d -exec rm -rf {{}} + 2>/dev/null || true
  rm -rf node_modules/cheerio node_modules/.package-lock.json 2>/dev/null || true
  if [ -n "$TOOLS_PICK" ]; then
    npm install --no-save --legacy-peer-deps cheerio@1.0.0-rc.10 "@ant-design/tools@$TOOLS_PICK" 2>/dev/null || true
  else
    npm install --no-save --legacy-peer-deps cheerio@1.0.0-rc.10 2>/dev/null || true
  fi
  # Copy top-level cheerio into every nested location that enzyme recreated
  for nested in $(find node_modules -path "*/node_modules/cheerio/dist" -type d 2>/dev/null); do
    nested_dir=$(dirname "$nested")
    rm -rf "$nested_dir"
    cp -r node_modules/cheerio "$nested_dir"
  done
  find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {{}} + 2>/dev/null || true
}}

pin_cheerio

# pin_cheerio removes node_modules/.package-lock.json, so its npm install re-resolves the
# tree from package.json alone and can prune transitive deps that nothing references
# directly (seen on PR 30202: resolve-from vanished, breaking jest-cli's import-local
# chain). Heal the tree and re-pin until jest actually runs. No-op when jest is healthy.
for _attempt in 1 2 3; do
  if npx jest --version >/dev/null 2>&1; then break; fi
  npm install --legacy-peer-deps || true
  pin_cheerio
done
npm run version || true

node << 'PATCHEOF' || true
const fs = require('fs');
try {{
  let c = fs.readFileSync('.jest.js', 'utf8');
  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  let changed = false;
  for (const m of needed) {{
    if (!c.includes("'" + m + "'")) {{
      c = c.replace('const compileModules = [', "const compileModules = [\\n  '" + m + "',");
      changed = true;
    }}
  }}
  if (changed) {{ fs.writeFileSync('.jest.js', c); console.log('Patched .jest.js ESM modules'); }}
}} catch(e) {{ console.log('No .jest.js to patch'); }}
PATCHEOF

test -d node_modules
npx jest --version
node -e "require('enzyme')"
test "$(npx jest --config .jest.js --listTests 2>/dev/null | wc -l)" -gt 0

# --listTests only resolves paths; it never loads the transform or tests/setup.js. Execute one
# real suite so a broken toolchain fails the build here instead of silently producing an image
# whose every run reports "Tests: 0 total" and grades as unresolved.
SMOKE_FILE=$(npx jest --config .jest.js --listTests 2>/dev/null | head -1)
npx jest --config .jest.js --cache=false "$SMOKE_FILE" > /tmp/smoke.log 2>&1 || true
SMOKE_TOTAL=$(grep -a "Tests:" /tmp/smoke.log | tail -1 | grep -aoE "[0-9]+ total" | grep -aoE "^[0-9]+" | head -1)
if [ "${{SMOKE_TOTAL:-0}}" -lt 1 ]; then tail -60 /tmp/smoke.log; exit 1; fi
echo "smoke: $SMOKE_FILE ran $SMOKE_TOTAL tests"

# The Dockerfile stage after this one is Image._HARDENING_BLOCK, whose
# `git gc --prune=now --aggressive` repacks ~20k commits with window=250. Unbounded,
# several concurrent image builds OOM the Docker VM and pack-objects dies of signal 9
# (exit 128, "fatal: failed to run repack"). These local settings cap that repack.
git config --local pack.threads 1
git config --local pack.windowMemory 64m
git config --local pack.deltaCacheSize 64m
git config --local core.bigFileThreshold 16m
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export TZ=UTC
{sync_deps_fn}
{jest_run_fn}
sync_runtime_deps || true
run_jest
""".format(
                    repo=self.pr.repo,
                    sync_deps_fn=_SYNC_DEPS_FN,
                    jest_run_fn=_JEST_RUN_FN,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export TZ=UTC
{apply_patch_fn}
{sync_deps_fn}
{jest_run_fn}
apply_patch /home/test.patch
sync_runtime_deps || true
run_jest
""".format(
                    repo=self.pr.repo,
                    apply_patch_fn=_APPLY_PATCH_FN,
                    sync_deps_fn=_SYNC_DEPS_FN,
                    jest_run_fn=_JEST_RUN_FN,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export TZ=UTC
{apply_patch_fn}
{sync_deps_fn}
{jest_run_fn}
apply_patch /home/test.patch
apply_patch /home/fix.patch
sync_runtime_deps || true
run_jest
""".format(
                    repo=self.pr.repo,
                    apply_patch_fn=_APPLY_PATCH_FN,
                    sync_deps_fn=_SYNC_DEPS_FN,
                    jest_run_fn=_JEST_RUN_FN,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("AntDesignImageDefault_ANT_DESIGN_35705_TO_30656 dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        # The runner scripts are not inputs to prepare.sh (npm install) or to the
        # hardening repack, so COPYing them last keeps those two very expensive
        # layers cached when only the jest invocation is re-tuned.
        copy_commands = ""
        runner_copy_commands = ""
        for file in self.files():
            line = f"COPY {file.name} /home/\n"
            if file.name.endswith("run.sh"):
                runner_copy_commands += line
            else:
                copy_commands += line

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{runner_copy_commands}

{self.clear_env}

"""


@Instance.register("ant-design", "ant_design_35705_to_30656")
class ANT_DESIGN_35705_TO_30656(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_35705_TO_30656(self.pr, self._config)

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
        return parse_jest_log(test_log)
