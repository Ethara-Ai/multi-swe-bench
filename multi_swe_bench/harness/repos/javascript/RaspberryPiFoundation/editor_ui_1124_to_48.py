from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


NUMBER_INTERVAL = "editor_ui_1124_to_48"
BASE_TAG = "base"
APT_PACKAGES = ["git", "ca-certificates", "coreutils"]
INSTALL_ATTEMPTS = 4
INSTALL_BACKOFF_SECONDS = 15
NETWORK_TIMEOUT_MS = 600000
JEST_WORKERS = 2
TARGETED_TIMEOUT_SECONDS = 480
FULL_SUITE_TIMEOUT_SECONDS = 540
ENV_TEMPLATES = [".env", ".env.webcomponent"]
JEST_START = "-----MSB_JEST_JSON_START-----"
JEST_END = "-----MSB_JEST_JSON_END-----"
JEST_JSON = "/home/jest-report.json"
TEST_FILE_RE = r"\.(test|spec)\.(js|jsx|ts|tsx)$"


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/__REPO__

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository"
    exit 1
fi

git update-index -q --really-refresh || true

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


_PRIVATE_DEPS_JS = r"""
var fs = require("fs");
var url = require("url");
var publicHosts = process.argv.slice(2).map(function (u) { return url.parse(u).host; });
var pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));
var deps = Object.assign({}, pkg.dependencies || {}, pkg.devDependencies || {});
var routed = {};
if (fs.existsSync(".npmrc")) {
    fs.readFileSync(".npmrc", "utf8").split(/\r?\n/).forEach(function (line) {
        var m = line.match(/^\s*(@[^:\s]+):registry\s*=\s*(\S+)/);
        if (m && publicHosts.indexOf(url.parse(m[2]).host) === -1) { routed[m[1]] = true; }
    });
}
var locked = {};
if (fs.existsSync("yarn.lock")) {
    var name = null;
    fs.readFileSync("yarn.lock", "utf8").split(/\r?\n/).forEach(function (line) {
        var h = line.match(/^"?((?:@[^/@"]+\/)?[^@"\s]+)@/);
        if (h) { name = h[1]; return; }
        var r = line.match(/^\s+resolved\s+"?([^"\s#]+)/);
        if (r && name && publicHosts.indexOf(url.parse(r[1]).host) === -1) { locked[name] = true; }
    });
}
Object.keys(deps).forEach(function (n) {
    var scope = n.charAt(0) === "@" ? n.split("/")[0] : "";
    var spec = String(deps[n]);
    if (/^(npm|file|link|git|https?):/.test(spec)) { return; }
    if (routed[scope] || locked[n]) {
        process.stdout.write(n + "\t" + spec + "\t" + n.toLowerCase() + "\n");
    }
});
"""


_PICK_VERSION_JS = r"""
var semver = require(process.argv[2]);
var range = process.argv[3];
var all = JSON.parse(process.argv[4] || "[]");
if (!Array.isArray(all)) { all = [all]; }
all = all.filter(function (v) { return semver.valid(v); }).sort(semver.compare);
var ok = all.filter(function (v) { return semver.satisfies(v, range, { includePrerelease: true }); });
process.stdout.write((ok[0] || all[0] || "") + "\n");
"""


_ALIAS_JS = r"""
var fs = require("fs");
var p = JSON.parse(fs.readFileSync("package.json", "utf8"));
var name = process.argv[2], target = process.argv[3];
["dependencies", "devDependencies"].forEach(function (k) {
    if (p[k] && p[k][name] !== undefined) { p[k][name] = target; }
});
fs.writeFileSync("package.json", JSON.stringify(p, null, 2) + "\n");
"""


_STRIP_LOCK_JS = r"""
var fs = require("fs");
var name = process.argv[2];
var out = [], skip = false;
fs.readFileSync("yarn.lock", "utf8").split("\n").forEach(function (line) {
    if (/^\S/.test(line)) {
        var keys = line.replace(/:$/, "").split(/,\s*/).map(function (k) { return k.replace(/^"|"$/g, ""); });
        skip = keys.some(function (k) { return k.indexOf(name + "@") === 0; });
    }
    if (!skip) { out.push(line); }
});
fs.writeFileSync("yarn.lock", out.join("\n"));
"""


_YARN_INSTALL_SH = """#!/bin/bash
set -eo pipefail
cd /home/__REPO__

MODE="${1:-strict}"
YARN_VERSION="$(yarn --version)"
YARN_MAJOR="${YARN_VERSION%%.*}"
echo "yarn_install: yarn ${YARN_VERSION}, mode ${MODE}"

attempt() {
    for i in $(seq 1 __INSTALL_ATTEMPTS__); do
        if "$@"; then return 0; fi
        echo "yarn_install: attempt $i of '$*' failed, retrying"
        sleep $((i * __INSTALL_BACKOFF__))
    done
    return 1
}

PUBLIC_NPM="$(cd /tmp && npm config get registry)"
PUBLIC_YARN="$(cd /tmp && yarn config get registry 2>/dev/null || true)"
SEMVER_JS="$(npm root -g)/npm/node_modules/semver"
PRIVATE="$(node /home/private_deps.js "$PUBLIC_NPM" "$PUBLIC_YARN")"
RESTORE=""
restore() {
    if [ -n "$RESTORE" ]; then
        cp /tmp/package.json.orig package.json
        if [ -f /tmp/yarn.lock.orig ]; then cp /tmp/yarn.lock.orig yarn.lock; fi
    fi
}
trap restore EXIT
if [ -n "$PRIVATE" ]; then
    cp package.json /tmp/package.json.orig
    [ -f yarn.lock ] && cp yarn.lock /tmp/yarn.lock.orig
    RESTORE=yes
    while IFS=$'\\t' read -r name spec public; do
        [ -n "$name" ] || continue
        versions="$(cd /tmp && npm view "$public" versions --json --registry "$PUBLIC_NPM" 2>/dev/null || true)"
        version="$(node /home/pick_version.js "$SEMVER_JS" "$spec" "$versions")"
        if [ -z "$version" ]; then
            echo "yarn_install: $name ($spec) is on a private registry and $public has no public release"
            continue
        fi
        echo "yarn_install: $name ($spec) is on a private registry, installing public $public@$version under its name"
        node /home/alias_dep.js "$name" "npm:$public@$version"
        [ -f yarn.lock ] && node /home/strip_lock.js "$name"
    done <<EOF
$PRIVATE
EOF
fi

if [ "$YARN_MAJOR" = "1" ]; then
    if [ "$MODE" = "strict" ] && [ -z "$RESTORE" ]; then
        if ! attempt yarn install --frozen-lockfile --network-timeout __NETWORK_TIMEOUT__; then
            echo "yarn_install: frozen lockfile install failed, installing unfrozen"
            attempt yarn install --network-timeout __NETWORK_TIMEOUT__
        fi
    else
        attempt yarn install --network-timeout __NETWORK_TIMEOUT__
    fi
else
    if [ "$MODE" = "strict" ] && [ -z "$RESTORE" ]; then
        attempt yarn install --immutable
    else
        YARN_ENABLE_IMMUTABLE_INSTALLS=false attempt yarn install
    fi
fi

test -d node_modules
test -f node_modules/jest/package.json || test -f node_modules/react-scripts/package.json
echo "yarn_install: done"
"""


_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail

cd /home/__REPO__

binary_hunks() {
    awk '/^diff --git a\\// {p=$4; sub(/^b\\//,"",p)}
         /^Binary files / {d=($0 ~ / and \\/dev\\/null differ$/) ? "D" : "A"; print d, p}' "$1"
}

materialise_binaries() {
    while read -r kind path; do
        [ -n "$path" ] || continue
        if [ "$kind" = "D" ]; then
            rm -f "$path"
            echo "apply_patch: binary deletion $path"
        else
            mkdir -p "$(dirname "$path")"
            : > "$path"
            echo "apply_patch: binary addition $path (no payload in the dataset, created empty)"
        fi
    done <<EOF
$(binary_hunks "$1")
EOF
}

for p in "$@"; do
    if git apply --whitespace=nowarn "$p"; then
        echo "apply_patch: applied $p"
        git add -A
        continue
    fi

    EXCL=""
    while read -r kind path; do
        [ -n "$path" ] || continue
        EXCL="$EXCL --exclude=$path"
    done <<EOF
$(binary_hunks "$p")
EOF

    if [ -n "$EXCL" ] && git apply --whitespace=nowarn $EXCL "$p"; then
        materialise_binaries "$p"
        echo "apply_patch: applied $p with its payload-less binary hunks handled"
        git add -A
        continue
    fi

    SKIP=""
    while read -r f post; do
        [ -n "$f" ] || continue
        cur=$(git rev-parse "HEAD:$f" 2>/dev/null | cut -c1-${#post} || true)
        if [ "$cur" = "$post" ]; then
            echo "apply_patch: $f is already at the patch's post-image, skipping"
            SKIP="$SKIP --exclude=$f"
        fi
    done <<EOF
$(awk '/^diff --git a\\// {f=$3; sub(/^a\\//,"",f)} /^index / {i=index($2,".."); if (f != "" && i > 0) print f, substr($2, i+2); f=""}' "$p")
EOF
    if [ -n "$SKIP" ] && git apply --whitespace=nowarn $SKIP $EXCL "$p"; then
        materialise_binaries "$p"
        echo "apply_patch: applied $p minus the already-applied files"
    else
        echo "apply_patch: retrying $p with --3way"
        git apply --3way --whitespace=nowarn "$p"
        echo "apply_patch: applied $p with --3way"
    fi
    git add -A
done

if git grep -qI -e '^<<<<<<< ' -- . 2>/dev/null; then
    echo "apply_patch: conflict markers survived the merge"
    git grep -nI -e '^<<<<<<< ' -- . | head
    exit 1
fi

if git diff --name-only HEAD -- package.json yarn.lock .yarnrc.yml .yarn/releases .npmrc | grep -q .; then
    echo "apply_patch: dependency manifests changed, re-installing"
    git diff --name-only HEAD -- package.json yarn.lock .yarnrc.yml .yarn/releases .npmrc
    bash /home/yarn_install.sh loose
else
    echo "apply_patch: no dependency manifest touched, install left as built"
fi
"""


_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

_t0=$(date +%s)
step() { echo "prepare[+$(( $(date +%s) - _t0 ))s] $*"; }

cd /home/__REPO__

git reset --hard
git clean -fdq -e node_modules

step "checking work tree"
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "__SHA__"
echo "prepare: HEAD is __SHA__"

for f in __ENV_TEMPLATES__; do
    if [ -f "$f.example" ] && [ ! -f "$f" ]; then cp "$f.example" "$f"; fi
done

step "yarn install"
bash /home/yarn_install.sh strict

test -d /home/__REPO__/node_modules
test -s /home/__REPO__/node_modules/jest/package.json || test -s /home/__REPO__/node_modules/react-scripts/package.json
test -s /home/__REPO__/package.json
node --version
yarn --version

git reset --hard
bash /home/check_git_changes.sh
step "gates passed"
"""


_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export TZ=UTC

cd /home/__REPO__

TARGETS=$(cat /home/jest_targets.txt 2>/dev/null || true)

echo "__JEST_START__"
rm -f __JEST_JSON__
RAN=no
rc=0
if [ -n "$TARGETS" ]; then
    EXISTING=""
    for f in $TARGETS; do
        [ -f "$f" ] && EXISTING="$EXISTING $f"
    done
    if [ -n "$EXISTING" ]; then
        echo "jest targets:$EXISTING"
        RAN=yes
        timeout --signal=KILL __TARGETED_TIMEOUT__ \\
            yarn test --watchAll=false --ci --maxWorkers=__JEST_WORKERS__ \\
            --json --outputFile=__JEST_JSON__ --runTestsByPath $EXISTING \\
            > /tmp/jest_stdout 2>&1 || rc=$?
    else
        echo "jest: none of the target files exist at this tree state"
    fi
else
    echo "jest: the test patch adds or edits no jest suite; running the whole suite"
    RAN=yes
    timeout --signal=KILL __FULL_TIMEOUT__ \\
        yarn test --watchAll=false --ci --maxWorkers=__JEST_WORKERS__ \\
        --json --outputFile=__JEST_JSON__ \\
        > /tmp/jest_stdout 2>&1 || rc=$?
fi
[ "$rc" = "137" ] && echo "jest: KILLED by the wall-clock ceiling (hung suite)"

if [ -f __JEST_JSON__ ]; then
    node -e '
        var r = require("__JEST_JSON__");
        var n = 0;
        var out = {testResults: (r.testResults || []).map(function (s) {
            var a = (s.assertionResults || []).map(function (t) {
                n += 1;
                return {ancestorTitles: t.ancestorTitles, title: t.title, status: t.status};
            });
            return {name: s.name, status: s.status,
                    suiteError: (a.length === 0 && s.message) ? String(s.message).split("\\n").slice(0, 6).join(" | ") : "",
                    assertionResults: a};
        })};
        out.emitted = n;
        process.stdout.write(JSON.stringify(out, null, 1));
    ' || echo "FATAL: could not re-emit the jest report"
fi
echo "__JEST_END__"
echo "jest exit: $rc"

echo "----- jest stdout (human, not parsed) -----"
tail -80 /tmp/jest_stdout 2>/dev/null || true
if [ "$RAN" = "yes" ] && [ ! -f __JEST_JSON__ ]; then
    echo "FATAL: jest produced no JSON report -- the runner failed to start."
fi
"""


def _render(text: str, **kw: str) -> str:
    for k, v in kw.items():
        text = text.replace(f"__{k}__", v)
    return text


class EditorUi1124To48ImageBase(Image):
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
        return "node:16.20.2-bookworm"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        repo = self.pr.repo
        infra = DockerfileEnhancer._infrastructure_block(self, image_name).strip()
        packages = " ".join(APT_PACKAGES)
        clone = (
            "RUN set -eux; \\\n"
            "    for i in 1 2 3 4 5; do \\\n"
            f"        rm -rf /home/{repo}; \\\n"
            f"        if git -C /home clone \"${{REPO_URL}}\" {repo}; then break; fi; \\\n"
            "        echo \"clone attempt $i failed, retrying\"; sleep 15; \\\n"
            "    done; \\\n"
            f"    test -d /home/{repo}/.git"
        )
        parts = [DockerfileEnhancer.SYNTAX_DIRECTIVE, f"FROM {image_name}", infra]
        if self.global_env:
            parts.append(self.global_env)
        parts += [
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"    {packages} \\\n"
            "    && rm -rf /var/lib/apt/lists/*",
            "RUN git config --global --add safe.directory '*'",
            clone,
        ]
        if self.clear_env:
            parts.append(self.clear_env)
        parts.append('CMD ["/bin/bash"]')
        return "\n\n".join(parts) + "\n"


class EditorUi1124To48ImageDefault(Image):
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
        return EditorUi1124To48ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_patch_files(self) -> list[str]:
        return [ln[6:].strip() for ln in self.pr.test_patch.split("\n")
                if ln.startswith("+++ b/")]

    def jest_targets(self) -> list[str]:
        out = set()
        for f in self._test_patch_files():
            if not f.startswith("src/"):
                continue
            if f.endswith(".snap"):
                mapped = f.replace("__snapshots__/", "")[: -len(".snap")]
                if re.search(TEST_FILE_RE, mapped):
                    out.add(mapped)
                continue
            if re.search(TEST_FILE_RE, f) or "/__tests__/" in f:
                out.add(f)
        return sorted(out)

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        targets = self.jest_targets()
        install = _render(
            _YARN_INSTALL_SH,
            REPO=repo,
            INSTALL_ATTEMPTS=str(INSTALL_ATTEMPTS),
            INSTALL_BACKOFF=str(INSTALL_BACKOFF_SECONDS),
            NETWORK_TIMEOUT=str(NETWORK_TIMEOUT_MS),
        )
        run_tests = _render(
            _RUN_TESTS_SH,
            REPO=repo,
            JEST_START=JEST_START,
            JEST_END=JEST_END,
            JEST_JSON=JEST_JSON,
            JEST_WORKERS=str(JEST_WORKERS),
            TARGETED_TIMEOUT=str(TARGETED_TIMEOUT_SECONDS),
            FULL_TIMEOUT=str(FULL_SUITE_TIMEOUT_SECONDS),
        )
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "jest_targets.txt", "\n".join(targets) + ("\n" if targets else "")),
            File(".", "check_git_changes.sh", _render(_CHECK_GIT_CHANGES_SH, REPO=repo)),
            File(".", "private_deps.js", _PRIVATE_DEPS_JS.lstrip()),
            File(".", "pick_version.js", _PICK_VERSION_JS.lstrip()),
            File(".", "alias_dep.js", _ALIAS_JS.lstrip()),
            File(".", "strip_lock.js", _STRIP_LOCK_JS.lstrip()),
            File(".", "yarn_install.sh", install),
            File(".", "apply_patch.sh", _render(_APPLY_PATCH_SH, REPO=repo)),
            File(".", "prepare.sh", _render(_PREPARE_SH, REPO=repo, SHA=sha,
                                            ENV_TEMPLATES=" ".join(ENV_TEMPLATES))),
            File(".", "run_tests.sh", run_tests),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency().image_full_name()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        sha = self.pr.base.sha
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()
        fetch = (
            "RUN set -eux; \\\n"
            f"    if ! git cat-file -e \"{sha}^{{commit}}\" 2>/dev/null; then \\\n"
            "        for i in 1 2 3 4 5; do \\\n"
            f"            if git fetch --depth 1 --no-tags -q origin \"{sha}\"; then break; fi; \\\n"
            f"            echo \"fetch {sha} attempt $i failed, retrying\"; sleep 10; \\\n"
            "        done; \\\n"
            "    fi; \\\n"
            f"    git cat-file -e \"{sha}^{{commit}}\""
        )
        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

{fetch}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


@Instance.register("RaspberryPiFoundation", NUMBER_INTERVAL)
class EditorUi1124To48(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EditorUi1124To48ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    @staticmethod
    def _between(text: str, start: str, end: str) -> str:
        i = text.find(start)
        if i == -1:
            return ""
        i += len(start)
        j = text.find(end, i)
        return text[i:j] if j != -1 else text[i:]

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        seen: dict[str, int] = {}

        def uniq(name: str) -> str:
            n = seen[name] = seen.get(name, 0) + 1
            return name if n == 1 else f"{name} #{n}"

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        blob = self._between(text, JEST_START, JEST_END)

        data = None
        anchor = blob.find('"testResults"')
        starts = [m.start() for m in re.finditer(r"\{", blob)]
        if anchor != -1:
            starts.sort(key=lambda i: (i > anchor, abs(anchor - i)))
        for start in starts:
            for end in range(len(blob), start, -1):
                chunk = blob[start:end]
                if not chunk.rstrip().endswith("}"):
                    continue
                try:
                    parsed = json.loads(chunk)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and "testResults" in parsed:
                    data = parsed
                break
            if data is not None:
                break

        if data is not None:
            for suite in data.get("testResults", []) or []:
                fname = re.sub(r"^.*?/home/[^/]+/", "", suite.get("name") or "")
                for t in suite.get("assertionResults", []) or []:
                    full = " > ".join((t.get("ancestorTitles") or []) + [t.get("title") or ""])
                    name = uniq(f"jest::{fname}::{full}".strip())
                    st = (t.get("status") or "").lower()
                    if st == "passed":
                        passed.add(name)
                    elif st in ("failed", "error"):
                        failed.add(name)
                    else:
                        skipped.add(name)

        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
