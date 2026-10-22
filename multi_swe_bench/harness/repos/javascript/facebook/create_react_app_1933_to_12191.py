import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "create_react_app_1933_to_12191"
_INTERVAL_SUFFIX = "1933_to_12191"
_ORG_KEY = "facebook"
_REPO_KEY = "create-react-app"

BASE_TAG = "base-1933_to_12191"
NODE_IMAGE = "node:22-bookworm"
NPM_ERA_FALLBACK = "6.14.18"
JEST_FALLBACK = "jest@26.6.3"
BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="

UNIT_PACKAGES = (
    "react-error-overlay",
    "react-dev-utils",
    "create-react-app",
    "babel-plugin-named-asset-import",
    "babel-preset-react-app",
    "eslint-config-react-app",
    "confusing-browser-globals",
    "stylelint-config-react-app",
)

_EXCLUDE_GLOBS = (
    "package-lock.json",
    "yarn.lock",
    "*/package-lock.json",
    "*/yarn.lock",
    "azure-pipelines*.yml",
    "azure-pipelines*.yaml",
    ".travis.yml",
    "appveyor.yml",
    ".github/*",
    "docs/*",
    "docusaurus/*",
    "CHANGELOG*",
    "*.md",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.pdf",
    "*.jar",
    "*.mp4",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.zip",
)

APPLY_ARGS = "--whitespace=nowarn " + " ".join(
    "--exclude={glob}".format(glob=glob) for glob in _EXCLUDE_GLOBS
)

CHECK_GIT_CHANGES = r"""#!/bin/bash
set -euo pipefail

cd "$1"

if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

EMIT_RESULTS = r"""const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const files = process.argv.slice(3);

let total = 0;

function write(name, status) {
  process.stdout.write("TESTCASE " + name + " " + status + "\n");
  total += 1;
}

for (const resultsFile of files) {
  let report;
  try {
    report = JSON.parse(fs.readFileSync(resultsFile, "utf8"));
  } catch (err) {
    process.stdout.write("MSWEBENCH_EMIT_ERROR " + resultsFile + " " + err.message + "\n");
    continue;
  }

  for (const suite of report.testResults || []) {
    const file = path.relative(root, suite.name).split(path.sep).join("/");
    for (const assertion of suite.assertionResults || []) {
      let status = "SKIPPED";
      if (assertion.status === "passed") {
        status = "PASSED";
      } else if (assertion.status === "failed") {
        status = "FAILED";
      }
      const title = (assertion.fullName || assertion.title || "")
        .replace(/\s+/g, " ")
        .trim();
      if (!title) {
        continue;
      }
      write(file + "::" + title, status);
    }
  }
}

process.stdout.write("MSWEBENCH_TOTAL " + total + "\n");
"""

APPLY_PATCH = r"""_apply_patch() {
    patch_file="$1"
    label="$2"
    if git apply ${APPLY_ARGS} "${patch_file}" 2>/tmp/_apply_err; then
        echo "apply(${label}): clean"
        return 0
    fi
    echo "apply(${label}): direct apply rejected, retrying as a three-way merge"
    tail -5 /tmp/_apply_err || true
    if ! git apply --3way ${APPLY_ARGS} "${patch_file}" 2>/tmp/_apply_err; then
        echo "apply(${label}): FATAL patch does not apply to this tree"
        tail -5 /tmp/_apply_err || true
        return 1
    fi
    if [ -n "$(git diff --name-only --diff-filter=U 2>/dev/null)" ]; then
        echo "apply(${label}): FATAL three-way merge left conflicted paths"
        return 1
    fi
    echo "apply(${label}): clean (three-way)"
    return 0
}
"""

RESOLVE_JEST = r"""resolve_jest() {
    local pkg_dir="$1"
    if [ -x "${pkg_dir}/node_modules/.bin/jest" ]; then
        echo "${pkg_dir}/node_modules/.bin/jest"
        return 0
    fi
    if [ -x "${REPO_DIR}/node_modules/.bin/jest" ]; then
        echo "${REPO_DIR}/node_modules/.bin/jest"
        return 0
    fi
    return 1
}

jest_supports() {
    "$1" --help 2>/dev/null | grep -q -- "--$2"
}

jest_flags() {
    local bin="$1"
    local flags="--runInBand"
    local flag
    for flag in ci forceExit silent; do
        if jest_supports "${bin}" "${flag}"; then
            flags="${flags} --${flag}"
        fi
    done
    echo "${flags}"
}

run_jest() {
    local out_file="$1"
    if jest_supports "${jest_bin}" outputFile; then
        if [ -n "${SELECTOR}" ]; then
            timeout --kill-after=60 3600 "${jest_bin}" ${JEST_FLAGS} \
                "${SELECTOR}" "${PATTERN}" --json --outputFile="${out_file}"
        else
            timeout --kill-after=60 3600 "${jest_bin}" ${JEST_FLAGS} \
                --json --outputFile="${out_file}"
        fi
        return $?
    fi
    if [ -n "${SELECTOR}" ]; then
        timeout --kill-after=60 3600 "${jest_bin}" ${JEST_FLAGS} \
            "${SELECTOR}" "${PATTERN}" --json > "${out_file}"
    else
        timeout --kill-after=60 3600 "${jest_bin}" ${JEST_FLAGS} --json > "${out_file}"
    fi
}

existing_targets() {
    local out=""
    local rel
    for rel in $(targets_for "$1"); do
        if [ -f "${REPO_DIR}/${rel}" ]; then
            out="${out} ${REPO_DIR}/${rel}"
        fi
    done
    echo "${out}"
}

target_pattern() {
    local out=""
    local rel
    local esc
    for rel in $(targets_for "$1"); do
        if [ -f "${REPO_DIR}/${rel}" ]; then
            esc="$(printf '%s' "${rel}" | sed 's/\./\\./g')"
            if [ -z "${out}" ]; then
                out="${esc}"
            else
                out="${out}|${esc}"
            fi
        fi
    done
    echo "${out}"
}
"""

HAS_SUITE = r"""has_suite() {
    local dir="$1"
    local found
    found="$(find "${dir}" -type d -name node_modules -prune -o -type f \
        \( -path '*/__tests__/*.js' -o -name '*.test.js' -o -name 'test.js' \) \
        -print 2>/dev/null | head -1)"
    [ -n "${found}" ]
}
"""

ERA_PROVISION_BODY = r"""export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export CYPRESS_INSTALL_BINARY=0
export HUSKY=0
export npm_config_update_notifier=false

ERA_CUTOFF="$(git show -s --format=%cI HEAD)"
ERA_DATE="${ERA_CUTOFF%%T*}"

NODE_MAJOR=""
for pin_file in .nvmrc .node-version; do
    if [ -f "${pin_file}" ]; then
        pinned="$(tr -d ' \t\r\n' < "${pin_file}" | sed 's/^[vV]//')"
        case "${pinned}" in
            [0-9]*) NODE_MAJOR="${pinned%%.*}" ;;
        esac
        break
    fi
done
if [ -z "${NODE_MAJOR}" ]; then
    case "${ERA_DATE}" in
        201[0-6]-*|2017-0[1-5]-*)    NODE_MAJOR=6 ;;
        2017-*|2018-0[1-3]-*)        NODE_MAJOR=8 ;;
        2018-*|2019-*|2020-0[1-6]-*) NODE_MAJOR=10 ;;
        2020-*|2021-*)               NODE_MAJOR=12 ;;
        2022-*)                      NODE_MAJOR=16 ;;
        *)                           NODE_MAJOR=18 ;;
    esac
fi

if [ "$(node -p 'process.versions.node.split(".")[0]')" != "${NODE_MAJOR}" ]; then
    echo "prepare: switching node to ${NODE_MAJOR}"
    npm install -g n
    n install "${NODE_MAJOR}"
    hash -r
fi
node --version

npm_ver="$(npm --version 2>/dev/null || echo 0)"
npm_major="${npm_ver%%.*}"
npm_rest="${npm_ver#*.}"
npm_minor="${npm_rest%%.*}"
npm_before=0
case "${npm_major}" in
    ''|*[!0-9]*) ;;
    *) if [ "${npm_major}" -gt 6 ]; then npm_before=1; fi ;;
esac
if [ "${npm_major}" = "6" ]; then
    case "${npm_minor}" in
        ''|*[!0-9]*) ;;
        *) if [ "${npm_minor}" -ge 9 ]; then npm_before=1; fi ;;
    esac
fi
if [ "${npm_before}" != "1" ]; then
    echo "prepare: npm ${npm_ver} has no --before, installing npm@${NPM_ERA_FALLBACK}"
    npm install -g "npm@${NPM_ERA_FALLBACK}" || true
    hash -r
fi
npm --version

echo "prepare: ERA_CUTOFF=${ERA_CUTOFF} NODE_MAJOR=${NODE_MAJOR} PACKAGES=${INSTALL_PACKAGES}"

npm install --before="${ERA_CUTOFF}" --legacy-peer-deps --no-audit --no-fund --no-package-lock \
    || npm install --before="${ERA_CUTOFF}" --legacy-peer-deps --no-audit --no-fund --no-package-lock --ignore-scripts \
    || true

if [ -f lerna.json ] && [ -x ./node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap --concurrency=1 || true
fi

for pkg in ${INSTALL_PACKAGES}; do
    pkg_dir="${REPO_DIR}/packages/${pkg}"
    [ -f "${pkg_dir}/package.json" ] || continue
    ( cd "${pkg_dir}" && npm install --before="${ERA_CUTOFF}" --legacy-peer-deps --no-audit --no-fund --no-package-lock ) \
        || ( cd "${pkg_dir}" && npm install --before="${ERA_CUTOFF}" --legacy-peer-deps --no-audit --no-fund --no-package-lock --ignore-scripts ) \
        || true
done

for pkg in ${INSTALL_PACKAGES}; do
    pkg_dir="${REPO_DIR}/packages/${pkg}"
    [ -f "${pkg_dir}/package.json" ] || continue
    if ! resolve_jest "${pkg_dir}" > /dev/null; then
        ( cd "${pkg_dir}" && npm install --no-save --before="${ERA_CUTOFF}" --legacy-peer-deps --no-audit --no-fund --no-package-lock jest ) || true
    fi
done

if [ ! -x "${REPO_DIR}/node_modules/.bin/jest" ]; then
    echo "prepare: no era runner at repo root, installing pinned ${JEST_FALLBACK}"
    npm install --no-save --legacy-peer-deps --no-audit --no-fund --no-package-lock "${JEST_FALLBACK}" || true
fi
"""

GATE_BODY = r"""test -d "${REPO_DIR}/node_modules" || test -n "$(ls -d ${REPO_DIR}/packages/*/node_modules 2>/dev/null)"

RUNNABLE=0
for pkg in ${TARGET_PACKAGES}; do
    pkg_dir="${REPO_DIR}/packages/${pkg}"
    if [ ! -f "${pkg_dir}/package.json" ]; then
        echo "gate: ${pkg} -> absent at base commit (created by the test patch)"
        continue
    fi
    if jest_bin="$(resolve_jest "${pkg_dir}")"; then
        "${jest_bin}" --version > /dev/null
        echo "gate: ${pkg} -> ${jest_bin}"
        RUNNABLE=$((RUNNABLE + 1))
    else
        echo "gate: ${pkg} -> no jest runner resolved"
    fi
done

if [ "${RUNNABLE}" = "0" ] && [ ! -x "${REPO_DIR}/node_modules/.bin/jest" ]; then
    echo "gate: FATAL no target package has a usable jest runner"
    exit 1
fi
"""

FALLBACK_GATE_BODY = r"""test -d "${REPO_DIR}/node_modules" || test -n "$(ls -d ${REPO_DIR}/packages/*/node_modules 2>/dev/null)"

RUNNABLE=0
for pkg in ${UNIT_PACKAGES}; do
    pkg_dir="${REPO_DIR}/packages/${pkg}"
    [ -f "${pkg_dir}/package.json" ] || continue
    if has_suite "${pkg_dir}"; then
        if jest_bin="$(resolve_jest "${pkg_dir}")"; then
            "${jest_bin}" --version > /dev/null
            echo "gate: ${pkg} -> ${jest_bin}"
            RUNNABLE=$((RUNNABLE + 1))
        else
            echo "gate: ${pkg} -> suite present, no jest runner resolved"
        fi
    fi
done

if [ "${RUNNABLE}" = "0" ]; then
    echo "gate: FATAL no package has both a jest suite and a usable runner"
    exit 1
fi
"""

GATE_TAIL = r"""node --check /home/emit_results.js
node --version > /dev/null
echo "DEPS_OK"
"""

TEST_ENV = r"""export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export SKIP_PREFLIGHT_CHECK=true
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export FORCE_COLOR=0
export NO_COLOR=1
export npm_config_update_notifier=false

cd "${REPO_DIR}"

rm -f /home/results.*.json

ATTEMPTED=0
RESULT_FILES=""
"""

TEST_EMIT = r"""echo "${BEGIN_MARKER}"
if [ -n "${RESULT_FILES}" ]; then
    node /home/emit_results.js "${REPO_DIR}" ${RESULT_FILES}
else
    echo "MSWEBENCH_NO_RUNNABLE_PACKAGES"
    echo "MSWEBENCH_TOTAL 0"
fi
echo "${END_MARKER}"

if [ -n "${RESULT_FILES}" ]; then
    exit 0
fi
if [ "${ATTEMPTED}" = "0" ]; then
    echo "run: no package had a runnable suite at stage ${STAGE}"
    exit 0
fi
echo "run: FATAL ${ATTEMPTED} package(s) executed but produced no report at stage ${STAGE}"
exit 1
"""

TEST_BODY = (
    TEST_ENV
    + r"""for pkg in ${TARGET_PACKAGES}; do
    pkg_dir="${REPO_DIR}/packages/${pkg}"
    [ -f "${pkg_dir}/package.json" ] || continue

    TARGET_FILES="$(existing_targets "${pkg}")"
    if [ -z "${TARGET_FILES}" ]; then
        echo "MSWEBENCH_NO_TARGETS ${pkg}"
        continue
    fi

    jest_bin="$(resolve_jest "${pkg_dir}")" || { echo "MSWEBENCH_NO_JEST ${pkg}"; continue; }

    JEST_FLAGS="$(jest_flags "${jest_bin}")"
    PATTERN="$(target_pattern "${pkg}")"
    SELECTOR=""
    if jest_supports "${jest_bin}" testPathPattern; then
        SELECTOR="--testPathPattern"
    fi
    echo "MSWEBENCH_RUN ${pkg} ${jest_bin} ${JEST_FLAGS} ${SELECTOR} ${PATTERN}"

    out="/home/results.${pkg}.json"
    ATTEMPTED=$((ATTEMPTED + 1))
    set +e
    ( cd "${pkg_dir}" && run_jest "${out}" )
    rc=$?
    set -e
    if [ "${rc}" = "124" ] || [ "${rc}" = "137" ]; then
        echo "run: FATAL ${pkg} exceeded the time budget at stage ${STAGE}"
        exit 1
    fi

    if [ -s "${out}" ]; then
        RESULT_FILES="${RESULT_FILES} ${out}"
    else
        echo "MSWEBENCH_NO_REPORT ${pkg}"
    fi
done

"""
    + TEST_EMIT
)

FALLBACK_TEST_BODY = (
    TEST_ENV
    + r"""for pkg in ${UNIT_PACKAGES}; do
    pkg_dir="${REPO_DIR}/packages/${pkg}"
    [ -f "${pkg_dir}/package.json" ] || continue

    if ! has_suite "${pkg_dir}"; then
        echo "MSWEBENCH_NO_SUITE ${pkg}"
        continue
    fi

    jest_bin="$(resolve_jest "${pkg_dir}")" || { echo "MSWEBENCH_NO_JEST ${pkg}"; continue; }

    JEST_FLAGS="$(jest_flags "${jest_bin}")"
    SELECTOR=""
    PATTERN=""
    echo "MSWEBENCH_RUN ${pkg} ${jest_bin} ${JEST_FLAGS}"

    out="/home/results.${pkg}.json"
    ATTEMPTED=$((ATTEMPTED + 1))
    set +e
    ( cd "${pkg_dir}" && run_jest "${out}" )
    rc=$?
    set -e
    if [ "${rc}" = "124" ] || [ "${rc}" = "137" ]; then
        echo "run: FATAL ${pkg} exceeded the time budget at stage ${STAGE}"
        exit 1
    fi

    if [ -s "${out}" ]; then
        RESULT_FILES="${RESULT_FILES} ${out}"
    else
        echo "MSWEBENCH_NO_REPORT ${pkg}"
    fi
done

"""
    + TEST_EMIT
)


_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")

_JS_EXT = re.compile(r"\.[cm]?[jt]sx?$")
_TEST_BASENAME = re.compile(r"^(?:.*\.)?(?:test|spec)\.[cm]?[jt]sx?$")
_NON_SUITE_SEGMENTS = {"node_modules", "fixtures", "template", "__snapshots__"}
_NON_SUITE_STEMS = {
    "setupjest",
    "setuptests",
    "test-setup",
    "jest.config",
    "jest.setup",
}


def _jest_targets(test_patch: str) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    if not test_patch:
        return targets

    paths: list[str] = []
    pending: str | None = None
    for line in test_patch.replace("\r\n", "\n").split("\n"):
        match = _DIFF_HEADER.match(line)
        if match:
            if pending is not None:
                paths.append(pending)
            pending = match.group(2)
            continue
        if pending is not None and line.startswith("deleted file mode"):
            pending = None
    if pending is not None:
        paths.append(pending)

    for path in paths:
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "packages":
            continue
        package = parts[1]
        if package not in UNIT_PACKAGES:
            continue
        if any(segment in _NON_SUITE_SEGMENTS for segment in parts[2:]):
            continue
        basename = parts[-1]
        stem = basename.rsplit(".", 1)[0].lower()
        if stem in _NON_SUITE_STEMS:
            continue
        in_tests_dir = "__tests__" in parts[2:-1]
        is_suite = (in_tests_dir and bool(_JS_EXT.search(basename))) or bool(
            _TEST_BASENAME.match(basename)
        )
        if not is_suite:
            continue
        bucket = targets.setdefault(package, [])
        if path not in bucket:
            bucket.append(path)

    return {package: sorted(files) for package, files in sorted(targets.items())}


def _targets_for_function(targets: dict[str, list[str]]) -> str:
    lines = ["targets_for() {", '    case "$1" in']
    for package, files in targets.items():
        lines.append("        {package})".format(package=package))
        lines.append('            echo "{files}"'.format(files=" ".join(files)))
        lines.append("            ;;")
    lines.append("        *)")
    lines.append('            echo ""')
    lines.append("            ;;")
    lines.append("    esac")
    lines.append("}")
    return "\n".join(lines)


_PATCH_PACKAGE_TOOLCHAIN: dict[str, tuple[str, ...]] = {
    "stylelint-config-react-app": (
        "stylelint@8.2.0",
        "stylelint-order@0.7.0",
        "stylelint-scss@2.1.0",
        "stylelint-no-unsupported-browser-features@1.0.1",
        "stylelint-declaration-block-no-ignored-properties@1.0.1",
    ),
}


def _patch_package_toolchain(targets: dict[str, list[str]]) -> str:
    blocks: list[str] = []
    for package in targets:
        pins = _PATCH_PACKAGE_TOOLCHAIN.get(package)
        if not pins:
            continue
        specs = " ".join(pins)
        deps = " ".join(pin.split("@")[0] for pin in pins)
        blocks.append(
            'echo "prepare: provisioning patch-created package {package}"\n'
            'pkg_dir="${{REPO_DIR}}/packages/{package}"\n'
            'if [ ! -f "${{pkg_dir}}/package.json" ]; then\n'
            '    mkdir -p "${{pkg_dir}}"\n'
            '    printf \'{{"name":"mswb-provision","version":"0.0.0","private":true}}\\n\''
            ' > "${{pkg_dir}}/package.json"\n'
            '    ( cd "${{pkg_dir}}" && npm install --no-save --legacy-peer-deps'
            " --no-audit --no-fund --no-package-lock {specs} ) || true\n"
            '    rm -f "${{pkg_dir}}/package.json"\n'
            "    for dep in {deps}; do\n"
            '        if [ -d "${{pkg_dir}}/node_modules/${{dep}}" ]; then\n'
            '            echo "prepare: {package} <- ${{dep}}"\n'
            "        else\n"
            '            echo "prepare: {package} MISSING ${{dep}}"\n'
            "        fi\n"
            "    done\n"
            "fi".format(package=package, specs=specs, deps=deps)
        )
    return "\n\n".join(blocks)


def _script_header(repo_dir: str, targets: dict[str, list[str]], stage: str) -> str:
    common = (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        'REPO_DIR="{repo_dir}"\n'
        'STAGE="{stage}"\n'
        'NPM_ERA_FALLBACK="{npm_fallback}"\n'
        'JEST_FALLBACK="{jest_fallback}"\n'
        'BEGIN_MARKER="{begin}"\n'
        'END_MARKER="{end}"\n'
        'APPLY_ARGS="{apply_args}"\n'
        'UNIT_PACKAGES="{unit_packages}"\n'
    ).format(
        repo_dir=repo_dir,
        stage=stage,
        npm_fallback=NPM_ERA_FALLBACK,
        jest_fallback=JEST_FALLBACK,
        begin=BEGIN_MARKER,
        end=END_MARKER,
        apply_args=APPLY_ARGS,
        unit_packages=" ".join(UNIT_PACKAGES),
    )
    if not targets:
        return (
            common
            + 'TARGET_PACKAGES=""\n'
            + 'INSTALL_PACKAGES="${UNIT_PACKAGES}"\n'
            + "\n"
            + RESOLVE_JEST
            + "\n"
            + HAS_SUITE
            + "\n"
        )
    return (
        common
        + 'TARGET_PACKAGES="{packages}"\n'.format(packages=" ".join(targets.keys()))
        + 'INSTALL_PACKAGES="${TARGET_PACKAGES}"\n'
        + "\n"
        + _targets_for_function(targets)
        + "\n\n"
        + RESOLVE_JEST
        + "\n"
    )


_PROXY_ARGS = """ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt\""""

_ENV_BLOCK = """ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}"""

_CERT_SYMLINKS = """RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"""

_HARDENING_BLOCK = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

_SUBMODULE_SCRUB = """RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""


class ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "str | Image":
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = 'RUN git clone "${{REPO_URL}}" /home/{repo}'.format(repo=repo)
        else:
            code = "COPY {repo} /home/{repo}".format(repo=repo)

        sections = [
            "# syntax=docker/dockerfile:1.6",
            "FROM {image_name}".format(image_name=image_name),
            "ARG TARGETARCH\n"
            'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
            "ARG BASE_COMMIT".format(org=org, repo=repo),
            _PROXY_ARGS,
            _ENV_BLOCK,
            'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            '      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            '      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            '      org.opencontainers.image.authors="https://www.ethara.ai/"'.format(
                org=org, repo=repo
            ),
            _CERT_SYMLINKS,
        ]

        if self.global_env.strip():
            sections.append(self.global_env.strip())

        sections.append("WORKDIR /home/")
        sections.append(code)
        sections.append("WORKDIR /home/{repo}".format(repo=repo))

        if self.clear_env.strip():
            sections.append(self.clear_env.strip())

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "str | Image":
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return "pr-{number}-{suffix}".format(
            number=self.pr.number, suffix=_INTERVAL_SUFFIX
        )

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        repo_dir = "/home/{repo}".format(repo=self.pr.repo)
        base_sha = self.pr.base.sha
        targets = _jest_targets(self.pr.test_patch)

        prepare = _script_header(repo_dir, targets, "prepare")
        prepare += "cd {repo_dir}\n".format(repo_dir=repo_dir)
        prepare += "\n"
        prepare += "git config --global --add safe.directory '*'\n"
        prepare += "git reset --hard\n"
        prepare += "git clean -fdx\n"
        prepare += "bash /home/check_git_changes.sh {repo_dir}\n".format(
            repo_dir=repo_dir
        )
        prepare += 'git checkout --detach "{sha}"\n'.format(sha=base_sha)
        prepare += "bash /home/check_git_changes.sh {repo_dir}\n".format(
            repo_dir=repo_dir
        )
        prepare += 'test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"\n'.format(
            sha=base_sha
        )
        prepare += 'echo "prepare: pinned to {sha}"\n'.format(sha=base_sha)
        prepare += "\n"
        prepare += "cat > /home/emit_results.js <<'MSWEBENCH_JS_EOF'\n"
        prepare += EMIT_RESULTS
        prepare += "MSWEBENCH_JS_EOF\n"
        prepare += "\n"
        prepare += ERA_PROVISION_BODY
        prepare += "\n"
        if targets:
            toolchain = _patch_package_toolchain(targets)
            if toolchain:
                prepare += toolchain + "\n\n"
            prepare += GATE_BODY
            body = TEST_BODY
        else:
            prepare += FALLBACK_GATE_BODY
            body = FALLBACK_TEST_BODY
        prepare += "\n"
        prepare += GATE_TAIL

        run = _script_header(repo_dir, targets, "baseline") + body

        test_run = _script_header(repo_dir, targets, "test")
        test_run += APPLY_PATCH
        test_run += "\n"
        test_run += 'cd "{repo_dir}"\n'.format(repo_dir=repo_dir)
        test_run += "_apply_patch /home/test.patch test\n"
        test_run += "\n"
        test_run += body

        fix_run = _script_header(repo_dir, targets, "fix")
        fix_run += APPLY_PATCH
        fix_run += "\n"
        fix_run += 'cd "{repo_dir}"\n'.format(repo_dir=repo_dir)
        fix_run += "_apply_patch /home/test.patch test\n"
        fix_run += "_apply_patch /home/fix.patch fix\n"
        fix_run += "\n"
        fix_run += body

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(
            "COPY {name} /home/".format(name=file.name) for file in self.files()
        )

        sections = [
            "FROM {name}:{tag}".format(name=name, tag=tag),
            'ARG BASE_COMMIT="{sha}"'.format(sha=self.pr.base.sha),
        ]

        if self.global_env.strip():
            sections.append(self.global_env.strip())

        sections.append(copy_commands)
        sections.append("RUN bash /home/prepare.sh")
        sections.append(
            "RUN git reset --hard\n" 'RUN git checkout --detach "${BASE_COMMIT}"'
        )
        sections.append(_HARDENING_BLOCK)
        sections.append(_SUBMODULE_SCRUB)

        if self.clear_env.strip():
            sections.append(self.clear_env.strip())

        return "\n\n".join(sections) + "\n"


@Instance.register(_ORG_KEY, _REPO_KEY)
class CREATE_REACT_APP(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

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

        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in clean.splitlines():
            stripped = line.strip()
            if stripped.startswith(BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(END_MARKER):
                in_detail = False
                continue
            if not in_detail:
                continue

            match = case_re.match(stripped)
            if not match:
                continue
            name = match.group(1).strip()
            if not name:
                continue
            status = match.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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


Instance.register(_ORG_KEY, _INTERVAL_NAME)(CREATE_REACT_APP)
