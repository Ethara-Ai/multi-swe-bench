from __future__ import annotations

import json
import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ORG = "pnpm"
_REPO = "pnpm"

_ERA_KEY = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"pnpm_(\d+)_to_(\d+)", _ERA_KEY)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like pnpm_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))


_NODE_IMAGE = "node:22.13.0-bullseye"

_BOOTSTRAP_PNPM = "10.19.0"

_JSON_BEGIN = "===== MSB-JEST-BEGIN"
_JSON_END = "===== MSB-JEST-END"

_REPO_ROOT = f"/home/{_REPO}"

_EXTRA_SETUP: dict[int, str] = {}

_TARGET_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)

_TEST_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _patch_paths(patch: str | None) -> list[str]:
    out = []
    for raw in _TARGET_RE.findall(patch or ""):
        path = raw.strip()
        if not path or path == "/dev/null":
            continue
        out.append(path)
    return out


def _test_files(pr: PullRequest) -> list[str]:
    out = []
    for path in _patch_paths(pr.test_patch):
        probe = "/" + path
        if "/test/" not in probe:
            continue
        if "/test/utils/" in probe:
            continue
        if not path.endswith(_TEST_EXT) or path.endswith(".d.ts"):
            continue
        parts = path.split("/")
        if "fixtures" in parts or "__fixtures__" in parts:
            continue
        out.append(path)
    return sorted(set(out))


def _touched_files(pr: PullRequest) -> list[str]:
    return sorted(set(_patch_paths(pr.fix_patch)) | set(_patch_paths(pr.test_patch)))


def _new_package_dirs(pr: PullRequest) -> list[str]:
    out = []
    for block in (pr.fix_patch or "").split("diff --git ")[1:]:
        if "new file mode" not in block:
            continue
        m = _TARGET_RE.search(block)
        if not m:
            continue
        path = m.group(1).strip()
        if path.endswith("/package.json"):
            out.append(path[: -len("/package.json")])
    return sorted(set(out))


def _sh_list(names: list[str]) -> str:
    return " ".join(f'"{n}"' for n in names)


def _strip_sh_comments(text: str) -> str:
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            continue
        out.append(line)
    return "\n".join(out)


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -euo pipefail

cd [[ROOT]]

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


_COMPILE_FN = r"""msb_compile() {
    local root=[[ROOT]]
    local -a dirs=("pnpm")
    local -a touched=( [[TOUCHED]] )
    local p d found

    for p in "${touched[@]}"; do
        d=$(dirname "$p")
        found=""
        while [ "$d" != "." ] && [ "$d" != "/" ]; do
            if [ -f "$root/$d/package.json" ]; then
                found="$d"
                break
            fi
            d=$(dirname "$d")
        done
        [ -n "$found" ] || continue
        case " ${dirs[*]} " in
            *" $found "*) continue ;;
        esac
        dirs+=("$found")
    done

    for d in "${dirs[@]}"; do
        [ -f "$root/$d/tsconfig.json" ] || continue
        (
            cd "$root/$d"
            echo "compile: $d"
            pnpm exec tsc --build 2>&1 | tail -n 40 \
                || echo "compile: tsc --build reported errors in $d" >&2

            if node -e "process.exit(((require('./package.json').scripts)||{}).bundle?0:1)" 2>/dev/null; then
                rm -rf dist
                pnpm run bundle 2>&1 | tail -n 20 \
                    || echo "compile: bundle failed in $d" >&2
                pnpm exec shx cp -r node-gyp-bin dist/node-gyp-bin > /dev/null 2>&1 || true
                pnpm exec shx cp -r node_modules/@pnpm/tabtab/lib/templates dist/templates > /dev/null 2>&1 || true
                pnpm exec shx cp -r node_modules/@pnpm/tabtab/lib/scripts dist/scripts > /dev/null 2>&1 || true
                pnpm exec shx cp -r node_modules/ps-list/vendor dist/vendor > /dev/null 2>&1 || true
                pnpm exec shx cp pnpmrc dist/pnpmrc > /dev/null 2>&1 || true
            fi
        )
    done

    cd "$root"
}
"""


_RUN_TESTS_FN = r"""msb_run_tests() {
    local root=[[ROOT]]
    local -a test_paths=( [[TEST_PATHS]] )
    local -A groups=()
    local -a order=()
    local p d pkg slug out log rc missing=0

    cd "$root"

    export CI=true
    export NO_COLOR=1
    export FORCE_COLOR=0
    export npm_config_color=false
    export npm_config_progress=false
    export NODE_OPTIONS="--dns-result-order=ipv4first --max-old-space-size=4096 --experimental-vm-modules --disable-warning=ExperimentalWarning --disable-warning=DEP0169"

    if ! pnpm install --frozen-lockfile --offline --config.engine-strict=false > /tmp/msb_install.log 2>&1; then
        if ! pnpm install --frozen-lockfile --config.engine-strict=false > /tmp/msb_install.log 2>&1; then
            tail -n 40 /tmp/msb_install.log
            echo "run_tests: pnpm install failed" >&2
            return 1
        fi
    fi
    tail -n 5 /tmp/msb_install.log

    [[EXTRA]]

    msb_compile

    for p in "${test_paths[@]}"; do
        [ -f "$root/$p" ] || continue

        d=$(dirname "$p")
        pkg=""
        while [ "$d" != "." ] && [ "$d" != "/" ]; do
            if [ -f "$root/$d/package.json" ]; then
                pkg="$d"
                break
            fi
            d=$(dirname "$d")
        done
        if [ -z "$pkg" ]; then
            echo "run_tests: no workspace package owns $p" >&2
            continue
        fi

        if [ -z "${groups[$pkg]+set}" ]; then
            order+=("$pkg")
            groups[$pkg]=""
        fi
        groups[$pkg]="${groups[$pkg]} ${p#$pkg/}"
    done

    if [ ${#order[@]} -eq 0 ]; then
        echo "run_tests: none of this PR's test files exist at this commit" >&2
        return 0
    fi

    for pkg in "${order[@]}"; do
        cd "$root/$pkg"

        export PNPM_SCRIPT_SRC_DIR="$root/$pkg"

        slug=$(printf '%s' "$pkg" | tr '/' '_')
        out="/tmp/msb_jest_${slug}.json"
        log="/tmp/msb_jest_${slug}.log"
        rm -f "$out" "$log"

        rc=0
        pnpm exec jest --ci --coverage=false \
            --globals '{"ts-jest":{"diagnostics":false}}' \
            --json --outputFile="$out" \
            --runTestsByPath ${groups[$pkg]} > "$log" 2>&1 || rc=$?

        tail -n 200 "$log"

        echo "[[BEGIN]] $pkg ====="
        if [ -s "$out" ]; then
            cat "$out"
        else
            echo '{}'
            echo "run_tests: jest wrote no results for $pkg (exit $rc)" >&2
            missing=1
        fi
        echo ""
        echo "[[END]] $pkg ====="

        cd "$root"
    done

    return "$missing"
}
"""


_PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export npm_config_color=false
export npm_config_progress=false
export NODE_OPTIONS="--dns-result-order=ipv4first --max-old-space-size=4096"

[[COMPILE_FN]]

cd [[ROOT]]

git config --global user.name "msb"
git config --global user.email "msb@example.com"
git config --global init.defaultBranch main

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
    git remote get-url origin > /dev/null 2>&1 \
        || git remote add origin https://github.com/[[ORG]]/[[REPO]].git
    git fetch --no-tags origin [[SHA]]
fi
git checkout --detach [[SHA]]
test "$(git rev-parse HEAD)" = "[[SHA]]"
bash /home/check_git_changes.sh

PM_VER=$(node -e "
var p = require('./package.json');
var v = '';
if (typeof p.packageManager === 'string' && p.packageManager.indexOf('pnpm@') === 0) {
  v = p.packageManager.slice(5).split('+')[0];
} else {
  var d = (p.devEngines || {}).packageManager || {};
  if (d.name === 'pnpm') v = d.version || '';
}
process.stdout.write(v);
")
if [ -z "$PM_VER" ]; then
    echo "prepare: package.json pins no pnpm version; using pnpm@[[BOOTSTRAP]]"
    PM_VER=[[BOOTSTRAP]]
fi
echo "prepare: installing pnpm@$PM_VER"
npm install -g "pnpm@$PM_VER" --no-audit --no-fund
if printf '%s' "$PM_VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'; then
    test "$(pnpm --version)" = "$PM_VER"
fi
echo "prepare: pnpm $(pnpm --version), node $(node --version)"

INSTALLED=0
for i in 1 2 3; do
    if pnpm install --frozen-lockfile --config.engine-strict=false; then
        INSTALLED=1
        break
    fi
    echo "prepare: pnpm install attempt $i failed" >&2
    sleep 15
done
if [ "$INSTALLED" -ne 1 ]; then
    echo "prepare: pnpm install failed after 3 attempts" >&2
    exit 1
fi

node -e "
var fs = require('fs');
var seen = {};
var inLock = false;
fs.readFileSync('/home/fix.patch', 'utf8').split('\n').forEach(function (line) {
  if (line.indexOf('diff --git ') === 0) {
    inLock = line === 'diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml';
    return;
  }
  if (!inLock) return;
  var m = line.match(/^\+  '?((?:@[^@\/\s']+\/)?[^@\s'(]+)@(\d[^\s'():]*)(?:\([^\n]*\))?'?:\s*$/);
  if (m) seen[m[1] + '@' + m[2]] = true;
});
process.stdout.write(Object.keys(seen).join('\n'));
" > /tmp/msb_fix_pkgs.txt
if [ -s /tmp/msb_fix_pkgs.txt ]; then
    echo "prepare: fix patch adds $(grep -c . /tmp/msb_fix_pkgs.txt) lockfile package(s); adding them to the store"
    if xargs pnpm store add < /tmp/msb_fix_pkgs.txt > /tmp/msb_store_add.log 2>&1; then
        echo "prepare: fix-patch packages added to the store"
    else
        tail -n 20 /tmp/msb_store_add.log
        echo "prepare: pnpm store add failed; graded stages will install online" >&2
    fi
else
    echo "prepare: fix patch adds no lockfile packages"
fi

msb_compile

pnpm --dir=__fixtures__ run prepareFixtures || echo "prepare: no prepareFixtures step here"

git checkout -- .

NEW_PKG_DIRS=( [[NEW_PKG_DIRS]] )
CHECKED=0
DEFERRED=0
for p in [[TEST_PATHS]]; do
    d=$(dirname "$p")
    pkg=""
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
        if [ -f "[[ROOT]]/$d/package.json" ]; then
            pkg="$d"
            break
        fi
        d=$(dirname "$d")
    done
    if [ -z "$pkg" ]; then
        for newdir in "${NEW_PKG_DIRS[@]}"; do
            case "/$p" in
                "/$newdir/"*)
                    echo "prepare: $p belongs to $newdir, created by the fix patch; deferring its check"
                    DEFERRED=$((DEFERRED + 1))
                    ;;
            esac
        done
        continue
    fi
    (
        cd "[[ROOT]]/$pkg"
        export PNPM_SCRIPT_SRC_DIR="[[ROOT]]/$pkg"
        pnpm exec jest --version > /dev/null
        LISTED=$(pnpm exec jest --listTests | wc -l)
        if [ "$LISTED" -eq 0 ]; then
            echo "prepare: jest lists no tests in $pkg" >&2
            exit 1
        fi
        node -e "
var fs = require('fs');
var p = require('./package.json');
if (p.main && !fs.existsSync(p.main)) {
  console.error('prepare: ' + p.name + ' entry ' + p.main + ' was not built');
  process.exit(1);
}
var d = Object.assign({}, p.dependencies, p.devDependencies);
if (d[p.name]) require.resolve(p.name);
"
    )
    CHECKED=$((CHECKED + 1))
done
if [ "$CHECKED" -eq 0 ] && [ "$DEFERRED" -eq 0 ]; then
    echo "prepare: no workspace package owns any graded test path" >&2
    exit 1
fi
echo "prepare: DEPS_OK ($CHECKED graded package path(s) checked, $DEFERRED deferred to the graded stages)"
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

[[COMPILE_FN]]

[[RUN_TESTS_FN]]

cd [[ROOT]]
msb_run_tests
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

[[COMPILE_FN]]

[[RUN_TESTS_FN]]

cd [[ROOT]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
msb_run_tests
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

[[COMPILE_FN]]

[[RUN_TESTS_FN]]

cd [[ROOT]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
msb_run_tests
"""


class Pnpm11118To10073ImageBase(Image):
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
        return _NODE_IMAGE

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

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_OPTIONS=--dns-result-order=ipv4first \\
    CI=true \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    NPM_CONFIG_COLOR=false \\
    NPM_CONFIG_PROGRESS=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt



RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class Pnpm11118To10073ImageDefault(Image):
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
        return Pnpm11118To10073ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        test_paths = _sh_list(_test_files(self.pr))
        touched = _sh_list(_touched_files(self.pr))
        new_pkg_dirs = _sh_list(_new_package_dirs(self.pr))

        extra = _EXTRA_SETUP.get(int(self.pr.number), "true")

        def _fill(text: str) -> str:
            return _strip_sh_comments(
                text.replace("[[RUN_TESTS_FN]]", _RUN_TESTS_FN.rstrip("\n"))
                .replace("[[COMPILE_FN]]", _COMPILE_FN.rstrip("\n"))
                .replace("[[EXTRA]]", extra)
                .replace("[[TEST_PATHS]]", test_paths)
                .replace("[[TOUCHED]]", touched)
                .replace("[[NEW_PKG_DIRS]]", new_pkg_dirs)
                .replace("[[BOOTSTRAP]]", _BOOTSTRAP_PNPM)
                .replace("[[BEGIN]]", _JSON_BEGIN)
                .replace("[[END]]", _JSON_END)
                .replace("[[ROOT]]", _REPO_ROOT)
                .replace("[[ORG]]", org)
                .replace("[[REPO]]", repo)
                .replace("[[SHA]]", sha)
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _fill(_CHECK_GIT_CHANGES_SH)),
            File(".", "prepare.sh", _fill(_PREPARE_SH)),
            File(".", "run.sh", _fill(_RUN_SH)),
            File(".", "test-run.sh", _fill(_TEST_RUN_SH)),
            File(".", "fix-run.sh", _fill(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"



{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register(_ORG, _ERA_KEY)
class Pnpm11118To10073(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Pnpm11118To10073ImageDefault(self.pr, self._config)

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

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        prefix = _REPO_ROOT + "/"

        blob_re = re.compile(
            re.escape(_JSON_BEGIN)
            + r"\s+(?P<pkg>\S+)\s+=====\s*(?P<body>.*?)"
            + re.escape(_JSON_END),
            re.S,
        )

        for match in blob_re.finditer(text):
            body = match.group("body")
            start = body.find("{")
            if start == -1:
                continue

            try:
                data = json.loads(body[start:])
            except Exception:
                end = body.rfind("}")
                if end <= start:
                    continue
                try:
                    data = json.loads(body[start : end + 1])
                except Exception:
                    continue

            for suite in data.get("testResults") or []:
                path = suite.get("name") or ""
                if prefix in path:
                    path = path.split(prefix, 1)[1]
                path = path.replace("\\", "/")

                assertions = suite.get("assertionResults") or []

                if not assertions:
                    if suite.get("status") == "failed" or suite.get("message"):
                        failed_tests.add(f"jest::{path}::<suite failed to load>")
                    continue

                seen: dict[str, int] = {}
                for a in assertions:
                    name = a.get("fullName") or a.get("title") or ""
                    if not name:
                        continue
                    test_id = f"jest::{path}::{name}" if path else f"jest::{name}"
                    seen[test_id] = seen.get(test_id, 0) + 1
                    if seen[test_id] > 1:
                        test_id = f"{test_id} #{seen[test_id]}"
                    status = (a.get("status") or "").lower()

                    if status == "passed":
                        passed_tests.add(test_id)
                    elif status == "failed":
                        failed_tests.add(test_id)
                    else:
                        skipped_tests.add(test_id)

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


def _owns(pr: PullRequest) -> bool:
    if getattr(pr, "org", "") != _ORG or getattr(pr, "repo", "") != _REPO:
        return False
    try:
        number = int(getattr(pr, "number", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not _ERA_LO <= number <= _ERA_HI:
        return False
    interval = str(getattr(pr, "number_interval", "") or "")
    return interval in ("", str(number))


_HOOK_FLAG = f"_{_ERA_KEY}_route_hook"

if not getattr(Instance, _HOOK_FLAG, False):
    _previous_create = Instance.create.__func__

    def _era_create(cls, pr, config, *args, **kwargs):
        if _owns(pr):
            return Pnpm11118To10073(pr, config, *args, **kwargs)
        return _previous_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_era_create)
    setattr(Instance, _HOOK_FLAG, True)
