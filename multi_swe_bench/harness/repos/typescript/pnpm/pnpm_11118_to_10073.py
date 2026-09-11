from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


ALIAS_KEY = "pnpm_11118_to_10073"

_PR_NUMBERS = (
    "10073",
    "10103",
    "10168",
    "10303",
    "10359",
    "10375",
    "10478",
    "10652",
    "10708",
    "10846",
    "10879",
    "10965",
    "10975",
    "11003",
    "11038",
    "11061",
    "11067",
    "11071",
    "11079",
    "11118",
)


_NODE_IMAGE = "node:22-bullseye"

_BOOTSTRAP_PNPM = "10.19.0"

_RUNTIME_NODE_VERSIONS = ("20.16.0", "24.6.0")

_JSON_BEGIN = "===== MSB-JEST-BEGIN"
_JSON_END = "===== MSB-JEST-END"

_REPO_ROOT = "/home/pnpm"

_EXTRA_SETUP: dict[int, str] = {}

_TARGET_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)

_TEST_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _patch_paths(patch: Optional[str]) -> list[str]:
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


_COMPILE_SH = r"""#!/bin/bash
set +e

ROOT=[[ROOT]]
cd "$ROOT" || exit 0

DIRS=("pnpm")

TOUCHED=( [[TOUCHED]] )

for p in "${TOUCHED[@]}"; do
    d=$(dirname "$p")
    found=""
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
        if [ -f "$ROOT/$d/package.json" ]; then
            found="$d"
            break
        fi
        d=$(dirname "$d")
    done
    [ -n "$found" ] || continue
    case " ${DIRS[*]} " in
        *" $found "*) continue ;;
    esac
    DIRS+=("$found")
done

for d in "${DIRS[@]}"; do
    [ -f "$ROOT/$d/tsconfig.json" ] || continue
    (
        cd "$ROOT/$d" || exit 0
        echo "compile: $d"
        pnpm exec tsc --build 2>&1 | tail -n 40

        if node -e "process.exit(((require('./package.json').scripts)||{}).bundle?0:1)" 2>/dev/null; then
            rm -rf dist
            pnpm run bundle 2>&1 | tail -n 20
            pnpm exec shx cp -r node-gyp-bin dist/node-gyp-bin > /dev/null 2>&1
            pnpm exec shx cp -r node_modules/@pnpm/tabtab/lib/templates dist/templates > /dev/null 2>&1
            pnpm exec shx cp -r node_modules/@pnpm/tabtab/lib/scripts dist/scripts > /dev/null 2>&1
            pnpm exec shx cp -r node_modules/ps-list/vendor dist/vendor > /dev/null 2>&1
            pnpm exec shx cp pnpmrc dist/pnpmrc > /dev/null 2>&1
        fi
    )
done

exit 0
"""


_RUN_TESTS_SH = r"""#!/bin/bash
set +e

ROOT=[[ROOT]]
cd "$ROOT" || exit 0

export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export npm_config_color=false
export npm_config_progress=false
export NODE_OPTIONS="--dns-result-order=ipv4first --experimental-vm-modules --disable-warning=ExperimentalWarning --disable-warning=DEP0169"

pnpm install --frozen-lockfile --offline > /tmp/msb_install.log 2>&1 \
    || pnpm install --frozen-lockfile > /tmp/msb_install.log 2>&1 \
    || pnpm install --no-frozen-lockfile > /tmp/msb_install.log 2>&1
tail -n 5 /tmp/msb_install.log

[[EXTRA]]

bash /home/compile.sh

TEST_PATHS=( [[TEST_PATHS]] )

declare -A MSB_GROUPS
ORDER=()

for p in "${TEST_PATHS[@]}"; do
    [ -f "$ROOT/$p" ] || continue

    d=$(dirname "$p")
    pkg=""
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
        if [ -f "$ROOT/$d/package.json" ]; then
            pkg="$d"
            break
        fi
        d=$(dirname "$d")
    done
    if [ -z "$pkg" ]; then
        echo "run_tests: no workspace package owns $p" >&2
        continue
    fi

    if [ -z "${MSB_GROUPS[$pkg]+set}" ]; then
        ORDER+=("$pkg")
        MSB_GROUPS[$pkg]=""
    fi
    MSB_GROUPS[$pkg]="${MSB_GROUPS[$pkg]} ${p#$pkg/}"
done

if [ ${#ORDER[@]} -eq 0 ]; then
    echo "run_tests: none of this PR's test files exist at this commit" >&2
    exit 0
fi

for pkg in "${ORDER[@]}"; do
    cd "$ROOT/$pkg" || continue

    export PNPM_SCRIPT_SRC_DIR="$ROOT/$pkg"

    slug=$(printf '%s' "$pkg" | tr '/' '_')
    out="/tmp/msb_jest_${slug}.json"
    log="/tmp/msb_jest_${slug}.log"
    rm -f "$out" "$log"

    pnpm exec jest --ci --coverage=false \
        --globals '{"ts-jest":{"diagnostics":false}}' \
        --json --outputFile="$out" \
        --runTestsByPath ${MSB_GROUPS[$pkg]} > "$log" 2>&1

    tail -n 200 "$log"

    echo "[[BEGIN]] $pkg ====="
    if [ -s "$out" ]; then
        cat "$out"
    else
        echo '{}'
    fi
    echo ""
    echo "[[END]] $pkg ====="

    cd "$ROOT"
done

exit 0
"""


_PREPARE_SH = r"""#!/bin/bash
set -e

npm install -g pnpm@[[BOOTSTRAP]] --no-audit --no-fund

git config --global user.name "msb"
git config --global user.email "msb@example.com"
git config --global init.defaultBranch main

cd [[ROOT]]
git reset --hard
bash /home/check_git_changes.sh

git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git rev-parse --verify --quiet "[[SHA]]^{commit}" > /dev/null 2>&1 \
    || git fetch --depth=1 origin [[SHA]] 2>/dev/null \
    || git fetch origin 2>/dev/null || true
git checkout -f [[SHA]]
bash /home/check_git_changes.sh

export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export npm_config_color=false
export npm_config_progress=false
export NODE_OPTIONS=--dns-result-order=ipv4first

PM=$(node -e "process.stdout.write((require('./package.json').packageManager)||'')")
PM_VER=${PM#pnpm@}
if [ -z "$PM_VER" ] || [ "$PM_VER" = "$PM" ]; then
    # Some repos (e.g. pnpm's own monorepo) pin their dev pnpm via the newer
    # devEngines.packageManager field instead of the legacy packageManager
    # field, which the block above does not read.
    PM_VER=$(node -e "var d=((require('./package.json').devEngines)||{}).packageManager||{};process.stdout.write(d.name==='pnpm'?(d.version||''):'')")
fi
if [ -n "$PM_VER" ]; then
    echo "prepare: package.json pins pnpm@$PM_VER"
    npm install -g "pnpm@$PM_VER" --no-audit --no-fund || true
fi
pnpm --version > /dev/null 2>&1 || npm install -g "pnpm@[[BOOTSTRAP]]" --no-audit --no-fund
echo "prepare: pnpm $(pnpm --version), node $(node --version)"

pnpm install --frozen-lockfile --config.engine-strict=false || pnpm install --no-frozen-lockfile --config.engine-strict=false

for v in [[RUNTIME_VERSIONS]]; do
    pnpm env use --global "$v" > /tmp/msb_runtime_warm.log 2>&1 || true
done
pnpm env use --global "$PM_VER" > /dev/null 2>&1 || true

git checkout -- .
bash /home/check_git_changes.sh

if git apply --check --whitespace=nowarn /home/fix.patch > /dev/null 2>&1; then
    git apply --whitespace=nowarn /home/fix.patch
    pnpm install --frozen-lockfile --config.engine-strict=false > /tmp/msb_warm.log 2>&1 \
        || pnpm install --no-frozen-lockfile --config.engine-strict=false > /tmp/msb_warm.log 2>&1 \
        || tail -n 5 /tmp/msb_warm.log

    git checkout -- .
    node -e "var fs=require('fs');var out=[];fs.readFileSync('/home/fix.patch','utf8').split('diff --git ').slice(1).forEach(function(b){if(b.indexOf('new file mode')===-1)return;var m=b.match(/^\+\+\+ b\/(.+)$/m);if(m)out.push(m[1].trim())});out.forEach(function(x){process.stdout.write(x+String.fromCharCode(10))})" > /tmp/msb_fix_new.txt
    echo "prepare: fix patch creates $(grep -c . /tmp/msb_fix_new.txt) file(s); removing them"
    while IFS= read -r f || [ -n "$f" ]; do
        if [ -n "$f" ]; then
            rm -f "[[ROOT]]/$f"
        fi
    done < /tmp/msb_fix_new.txt

    while IFS= read -r f || [ -n "$f" ]; do
        if [ -n "$f" ] && [ -e "[[ROOT]]/$f" ]; then
            echo "prepare: fix-patch file survived the revert: $f" >&2
            exit 1
        fi
    done < /tmp/msb_fix_new.txt

    pnpm install --frozen-lockfile --offline --config.engine-strict=false > /tmp/msb_rebase.log 2>&1 \
        || pnpm install --frozen-lockfile --config.engine-strict=false > /tmp/msb_rebase.log 2>&1
    git checkout -- .
    bash /home/check_git_changes.sh
    echo "prepare: fix-patch dependencies pre-resolved into the store"
else
    echo "prepare: fix patch does not apply at build time; store not pre-warmed" >&2
fi

bash /home/compile.sh

pnpm --dir=__fixtures__ run prepareFixtures 2>/dev/null || true

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
                    pkg=""
                    ;;
            esac
        done
        continue
    fi
    (
        cd "[[ROOT]]/$pkg"
        export PNPM_SCRIPT_SRC_DIR="[[ROOT]]/$pkg"
        pnpm exec jest --version > /dev/null
        node -e "var p=require('./package.json');var d=Object.assign({},p.dependencies,p.devDependencies);if(d[p.name])require.resolve(p.name)"
    )
    CHECKED=$((CHECKED + 1))
done
if [ "$CHECKED" -eq 0 ] && [ "$DEFERRED" -eq 0 ]; then
    echo "prepare: no workspace package owns any graded test path" >&2
    exit 1
fi
echo "prepare: $CHECKED graded package path(s) checked, $DEFERRED deferred to the graded stages"

cd [[ROOT]]
git checkout -- .
bash /home/check_git_changes.sh
"""

_RUN_SH = """#!/bin/bash
set -e

bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -e

cd [[ROOT]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -e

cd [[ROOT]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh
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

    def dependency(self) -> Union[str, "Image"]:
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

WORKDIR /home/

{code}

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

    def dependency(self) -> Optional[Image]: # type: ignore
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
            return (
                text.replace("[[EXTRA]]", extra)
                .replace("[[TEST_PATHS]]", test_paths)
                .replace("[[TOUCHED]]", touched)
                .replace("[[NEW_PKG_DIRS]]", new_pkg_dirs)
                .replace("[[RUNTIME_VERSIONS]]", " ".join(_RUNTIME_NODE_VERSIONS))
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
            File(".", "compile.sh", _fill(_COMPILE_SH)),
            File(".", "run_tests.sh", _fill(_RUN_TESTS_SH)),
            File(".", "prepare.sh", _fill(_PREPARE_SH)),
            File(".", "run.sh", _fill(_RUN_SH)),
            File(".", "test-run.sh", _fill(_TEST_RUN_SH)),
            File(".", "fix-run.sh", _fill(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name() # type: ignore
        tag = image.image_tag() # type: ignore
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
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

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("pnpm", ALIAS_KEY)
class Pnpm11118To10073(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]: # type: ignore
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

                for a in assertions:
                    name = a.get("fullName") or a.get("title") or ""
                    if not name:
                        continue
                    test_id = f"jest::{path}::{name}" if path else f"jest::{name}"
                    status = (a.get("status") or "").lower()

                    if status == "passed":
                        passed_tests.add(test_id)
                    elif status == "failed":
                        failed_tests.add(test_id)
                    else:
                        skipped_tests.add(test_id)

        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


for _pr_number in _PR_NUMBERS:
    Instance.register("pnpm", _pr_number)(Pnpm11118To10073)
