import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "ckeditor5_12506_to_8529"
REPO_DIR = "ckeditor5"
BASE_TAG = "base-12506_to_8529"
BASE_IMAGE = "debian:bookworm-slim"

_NODE_VERSIONS = "14.21.3"
_NODE_MAJORS = "14"
_PACKAGE_MANAGER = "yarn"
_PM_VERSION = "1.22.22"
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_DEFAULT_PACKAGES = [
    "build-essential",
    "ca-certificates",
    "chromium",
    "curl",
    "fonts-liberation",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
    "xz-utils",
]

_CHROMIUM_WRAPPER = (
    "RUN { \\\n"
    "        printf '%s\\n' '#!/bin/bash'; \\\n"
    "        printf '%s' 'exec /usr/bin/chromium --no-sandbox --disable-setuid-sandbox'; \\\n"
    "        printf '%s' ' --headless --disable-gpu --disable-dev-shm-usage'; \\\n"
    "        printf '%s' ' --disable-software-rasterizer --no-first-run'; \\\n"
    "        printf '%s' ' --no-default-browser-check --disable-extensions --mute-audio'; \\\n"
    "        printf '%s' ' --disable-background-timer-throttling'; \\\n"
    "        printf '%s' ' --disable-renderer-backgrounding'; \\\n"
    "        printf '%s' ' --disable-backgrounding-occluded-windows'; \\\n"
    "        printf '%s' ' --disable-search-engine-choice-screen'; \\\n"
    "        printf '%s' ' --window-size=1920,1080'; \\\n"
    "        printf '%s\\n' ' --remote-debugging-port=9222 \"$@\"'; \\\n"
    "    } > /usr/local/bin/chromium-no-sandbox && \\\n"
    "    chmod +x /usr/local/bin/chromium-no-sandbox"
)

_NODE_MULTI = (
    "RUN set -eux; \\\n"
    '    case "${TARGETARCH:-amd64}" in \\\n'
    "        amd64) narch=x64 ;; \\\n"
    "        arm64) narch=arm64 ;; \\\n"
    "        *) narch=x64 ;; \\\n"
    "    esac; \\\n"
    f"    for ver in {_NODE_VERSIONS}; do \\\n"
    '        maj="${ver%%.*}"; \\\n'
    '        mkdir -p "/opt/node/${maj}"; \\\n'
    "        for i in 1 2 3 4 5; do \\\n"
    "            if curl -fsSL --retry 3 --connect-timeout 30 \\\n"
    '                "https://nodejs.org/dist/v${ver}/node-v${ver}-linux-${narch}.tar.xz" \\\n'
    '                | tar -xJ -C "/opt/node/${maj}" --strip-components=1; then break; fi; \\\n'
    '            echo "node ${ver} download attempt $i failed, retrying"; sleep 15; \\\n'
    "        done; \\\n"
    '        test -x "/opt/node/${maj}/bin/node"; \\\n'
    "    done"
)

_PACKAGE_MANAGERS = (
    "RUN set -eux; \\\n"
    f"    for maj in {_NODE_MAJORS}; do \\\n"
    '        PATH="/opt/node/${maj}/bin:$PATH" npm install -g --force --no-audit --no-fund \\\n'
    f"            {_PACKAGE_MANAGER}@{_PM_VERSION}; \\\n"
    f'        test -x "/opt/node/${{maj}}/bin/{_PACKAGE_MANAGER}"; \\\n'
    "    done"
)

_CLONE_WITH_RETRY = (
    "RUN set -eux; \\\n"
    "    for i in 1 2 3 4 5; do \\\n"
    f"        rm -rf /home/{REPO_DIR}; \\\n"
    f'        if git -C /home clone "${{REPO_URL}}" {REPO_DIR}; then break; fi; \\\n'
    '        echo "clone attempt $i failed, retrying"; sleep 15; \\\n'
    "    done; \\\n"
    f"    test -d /home/{REPO_DIR}/.git"
)

_SELECT_NODE_SH = f"""select_node() {{
    local majors="{_NODE_MAJORS}"
    local min=""
    local want=""
    if [ -f pnpm-workspace.yaml ] || [ -f pnpm-lock.yaml ]; then
        min=24
    elif [ -f package.json ]; then
        if python3 -c "import json,re;d=json.load(open('package.json'));e=(d.get('engines') or {{}}).get('node') or '';m=re.search(r'([0-9]+)',e);print(m.group(1) if m else '')" > /tmp/msb_engines_min 2>/dev/null; then
            min=$(cat /tmp/msb_engines_min)
        fi
    fi
    min=${{min:-0}}
    for maj in $majors; do
        if [ "$maj" -ge "$min" ] 2>/dev/null; then
            want=$maj
            break
        fi
    done
    if [ -z "$want" ]; then
        want=$(printf '%s' "$majors" | awk '{{print $NF}}')
    fi
    export PATH="/opt/node/${{want}}/bin:$PATH"
    export NODE_OPTIONS="--max-old-space-size=4096"
    echo "select_node: engines_min=${{min}} selected=${{want}} node=$(node --version)"
}}
"""

_INSTALL_DEPS_SH = """install_deps() {
    export HUSKY=0
    export HUSKY_SKIP_INSTALL=1
    export CYPRESS_INSTALL_BINARY=0
    export PUPPETEER_SKIP_DOWNLOAD=true
    export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
    if [ -f pnpm-lock.yaml ] || [ -f pnpm-workspace.yaml ]; then
        pnpm config set node-linker hoisted
        for attempt in 1 2 3; do
            if pnpm install --frozen-lockfile; then return 0; fi
            echo "install_deps: pnpm attempt ${attempt} failed, retrying"
            sleep 20
        done
        if pnpm install --no-frozen-lockfile; then return 0; fi
        echo "install_deps: pnpm falling back to --ignore-scripts"
        pnpm install --no-frozen-lockfile --ignore-scripts
    else
        for attempt in 1 2 3; do
            if yarn install --ignore-engines --network-concurrency 4 \
                --network-timeout 600000; then return 0; fi
            echo "install_deps: yarn attempt ${attempt} failed, retrying"
            sleep 20
        done
        echo "install_deps: yarn falling back to --ignore-scripts"
        yarn install --ignore-engines --network-concurrency 4 \
            --network-timeout 600000 --ignore-scripts
    fi
}
"""

_RESOLVE_RUNNER_SH = """resolve_runner() {
    local cand
    for cand in \\
        node_modules/@ckeditor/ckeditor5-dev-tests/bin/testautomated.js \\
        node_modules/@ckeditor/ckeditor5-dev-tests/bin/test.js; do
        if [ -f "$cand" ]; then
            printf '%s' "node $cand"
            return 0
        fi
    done
    for cand in ckeditor5-dev-tests-run-automated ckeditor5-dev-tests; do
        if [ -x "node_modules/.bin/$cand" ]; then
            printf '%s' "node_modules/.bin/$cand"
            return 0
        fi
    done
    echo "resolve_runner: ckeditor5-dev-tests runner not installed" >&2
    return 1
}
"""

_PIN_KARMA_SH = """pin_karma() {
    local major
    local pinned
    test -f package.json || return 0
    major=$(python3 -c "
import json, re
try:
    d = json.load(open('package.json'))
except Exception:
    print('')
    raise SystemExit
deps = dict(d.get('dependencies') or {}, **(d.get('devDependencies') or {}))
spec = deps.get('@ckeditor/ckeditor5-dev-tests') or ''
m = re.search(r'([0-9]+)', spec)
print(m.group(1) if m else '')
" 2>/dev/null)
    test -n "$major" || return 0
    if [ "$major" -ge 24 ] && [ "$major" -lt 30 ] 2>/dev/null; then
        python3 -c "
import json
d = json.load(open('package.json'))
r = d.setdefault('resolutions', {})
r['karma'] = '6.4.4'
json.dump(d, open('package.json', 'w'), indent=2)
"
        pinned=$(python3 -c "
import json
print((json.load(open('package.json')).get('resolutions') or {}).get('karma', ''))
" 2>/dev/null)
        echo "pin_karma: dev_tests_major=${major} karma=${pinned}"
    else
        echo "pin_karma: dev_tests_major=${major} karma=unchanged"
    fi
}
"""

_LINK_WORKSPACE_SH = """link_workspace() {
    local dir
    local name
    local target
    local linked=0
    test -d packages || return 0
    for dir in packages/*/; do
        test -f "${dir}package.json" || continue
        name=$(node -e "try{process.stdout.write(require(process.cwd()+'/'+process.argv[1]+'package.json').name||'')}catch(e){}" "$dir")
        test -n "$name" || continue
        target="node_modules/${name}"
        if [ ! -e "$target" ]; then
            mkdir -p "$(dirname "$target")"
            ln -s "${PWD}/${dir%/}" "$target"
            linked=$((linked+1))
        fi
    done
    echo "link_workspace: linked=${linked}"
}
"""

_GATE_SH = """verify_provisioned() {
    local cmd
    local dir
    local name
    local pinned
    local installed
    local missing=0
    test -d node_modules
    cmd="$(resolve_runner)"
    test -n "$cmd"
    node -e "require.resolve('karma'); require.resolve('karma-mocha-reporter'); require.resolve('mocha')"
    if [ -d packages ]; then
        for dir in packages/*/; do
            test -f "${dir}package.json" || continue
            name=$(node -e "try{process.stdout.write(require(process.cwd()+'/'+process.argv[1]+'package.json').name||'')}catch(e){}" "$dir")
            test -n "$name" || continue
            if [ ! -e "node_modules/${name}" ]; then
                echo "verify_provisioned: workspace package not linked: ${name}"
                missing=$((missing+1))
            fi
        done
    fi
    test "$missing" -eq 0
    node -e "require.resolve('@ckeditor/ckeditor5-utils/package.json', {paths:[process.cwd()]})"
    pinned=$(python3 -c "
import json
print((json.load(open('package.json')).get('resolutions') or {}).get('karma', ''))
" 2>/dev/null)
    if [ -n "$pinned" ]; then
        installed=$(node -e "console.log(require('karma/package.json').version)")
        case "$installed" in
            6.*) ;;
            *) echo "verify_provisioned: karma pinned to ${pinned} but installed ${installed}"; return 1 ;;
        esac
        echo "verify_provisioned: karma=${installed} pinned=${pinned}"
    fi
    echo "verify_provisioned: runner=${cmd} workspace_links=ok"
}
"""

_APPLY_PATCH_SH = """apply_patch() {
    local patch="$1"
    git apply --whitespace=nowarn --binary "$patch" \\
        || git apply --whitespace=nowarn --binary -3 "$patch"
}
"""

_MSB_REPORTER_JS = """'use strict';

function MsbReporter(baseReporterDecorator) {
    baseReporterDecorator(this);
    var self = this;

    self.onSpecComplete = function (browser, result) {
        var names = [].concat(result.suite || [], result.description || []);
        var id = names.join('::');
        var status = 'PASS';
        if (result.skipped) {
            status = 'SKIP';
        } else if (!result.success) {
            status = 'FAIL';
        }
        self.write('MSB_TEST ' + status + ' ' + id + '\\n');
    };

    self.onRunComplete = function () {
        self.write('MSB_RUN_COMPLETE\\n');
    };
}

MsbReporter.$inject = ['baseReporterDecorator'];

module.exports = {
    'reporter:msb': ['type', MsbReporter]
};
"""

_KARMA_OVERRIDES_JS = """'use strict';

var path = require('path');

module.exports = function (karmaConfig) {
    karmaConfig.plugins = (karmaConfig.plugins || []).concat([
        require(path.resolve(__dirname, 'msb_reporter.js'))
    ]);
    karmaConfig.reporters = ['msb'];
};
"""

_RUN_TESTS_SH = """run_tests() {
    export CHROME_BIN=/usr/local/bin/chromium-no-sandbox
    export CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox
    local cmd
    local pkg
    local st
    cmd="$(resolve_runner)"
    for pkg in $(printf '%s' "${TEST_PACKAGES}" | tr ',' ' '); do
        echo "MSB_PACKAGE: ${pkg}"
        rm -rf build/.automated-tests
        set +e
        timeout -k 60 3600 $cmd \\
            --reporter=mocha \\
            --browsers=Chrome \\
            --no-source-map \\
            --files="${pkg}" \\
            --karma-config-overrides=/home/karma_overrides.js 2>&1
        st=$?
        set -e
        echo "MSB_PACKAGE_EXIT: ${pkg} ${st}"
    done
}
"""

_CHECK_GIT_CHANGES_SH = f"""#!/bin/bash
set -e

cd /home/{REPO_DIR}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository"
    exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


def _patch_targets(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)$", patch or "", re.MULTILINE)


def _append_package(names: list[str], directory: str) -> None:
    name = re.sub(r"^ckeditor5?-", "", directory)
    if not _PACKAGE_NAME_RE.match(name):
        return
    if name not in names:
        names.append(name)


def _test_packages(pr: PullRequest) -> str:
    names: list[str] = []

    for path in _patch_targets(pr.test_patch):
        parts = path.split("/")
        if len(parts) > 3 and parts[0] == "packages" and parts[2] == "tests":
            _append_package(names, parts[1])

    if not names:
        for path in _patch_targets(pr.fix_patch):
            parts = path.split("/")
            if len(parts) > 2 and parts[0] == "packages":
                _append_package(names, parts[1])

    if not names:
        return "*"

    return ",".join(names)


def _script_header(packages: str) -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "export CI=true\n"
        "export TRAVIS=true\n"
        f'export TEST_PACKAGES="{packages}"\n'
        "\n"
        f"cd /home/{REPO_DIR}\n"
        "\n"
    )


def _prepare_sh(packages: str) -> str:
    return (
        _script_header(packages)
        + "git reset --hard\n"
        + "git clean -fdq\n"
        + "bash /home/check_git_changes.sh\n"
        + "git checkout --detach ${BASE_COMMIT}\n"
        + "bash /home/check_git_changes.sh\n"
        + 'test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"\n'
        + "\n"
        + _SELECT_NODE_SH
        + _INSTALL_DEPS_SH
        + _RESOLVE_RUNNER_SH
        + _PIN_KARMA_SH
        + _LINK_WORKSPACE_SH
        + _GATE_SH
        + "\nselect_node\n"
        + "pin_karma\n"
        + "install_deps\n"
        + "link_workspace\n"
        + "verify_provisioned\n"
    )


def _run_sh(packages: str) -> str:
    return (
        _script_header(packages)
        + _SELECT_NODE_SH
        + _RESOLVE_RUNNER_SH
        + _RUN_TESTS_SH
        + "\nselect_node\n"
        + "run_tests\n"
    )


def _test_run_sh(packages: str) -> str:
    return (
        _script_header(packages)
        + _SELECT_NODE_SH
        + _RESOLVE_RUNNER_SH
        + _APPLY_PATCH_SH
        + _RUN_TESTS_SH
        + "\nselect_node\n"
        + "apply_patch /home/test.patch\n"
        + "run_tests\n"
    )


def _fix_run_sh(packages: str) -> str:
    return (
        _script_header(packages)
        + _SELECT_NODE_SH
        + _RESOLVE_RUNNER_SH
        + _APPLY_PATCH_SH
        + _RUN_TESTS_SH
        + "\nselect_node\n"
        + "apply_patch /home/test.patch\n"
        + "apply_patch /home/fix.patch\n"
        + "run_tests\n"
    )


def _sanitize_patch(patch: str) -> str:
    if not patch:
        return patch
    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    kept: list[str] = []
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        body = "\n".join(section)
        unusable = "Binary files" in body and "GIT binary patch" not in body
        if not unusable:
            kept.extend(section)

    for line in patch.split("\n"):
        if header.match(line):
            flush()
            section = [line]
        elif section:
            section.append(line)
        else:
            kept.append(line)
    flush()
    return "\n".join(kept)


def _apt_block() -> str:
    packages = " ".join(_DEFAULT_PACKAGES)
    return (
        "RUN set -eux; \\\n"
        "    ok=0; \\\n"
        "    for i in 1 2 3 4 5; do \\\n"
        "        (apt-get update || apt-get -o Acquire::Check-Valid-Until=false update) || true; \\\n"
        "        if apt-get install -o Acquire::Retries=20 -o Acquire::http::Timeout=180 \\\n"
        "            -y --no-install-recommends --fix-missing \\\n"
        f"            {packages}; then ok=1; break; fi; \\\n"
        '        echo "apt attempt $i incomplete, retrying"; sleep 15; \\\n'
        "    done; \\\n"
        '    test "$ok" = "1"; \\\n'
        "    rm -rf /var/lib/apt/lists/*"
    )


class CKEditor5ImageBase(Image):
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            DockerfileEnhancer._infrastructure_block(self, base_img).strip(),
            "ENV LC_ALL=C.UTF-8",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(_apt_block())
        sections.append(DockerfileEnhancer._MITM_MOUNT)
        sections.append(_NODE_MULTI)
        sections.append(_PACKAGE_MANAGERS)
        sections.append(_CHROMIUM_WRAPPER)
        sections.append("RUN git config --global --add safe.directory '*'")
        sections.append(_CLONE_WITH_RETRY)
        sections.append(f"WORKDIR /home/{REPO_DIR}")
        sections.append(
            "RUN git config --local fetch.recurseSubmodules false; \\\n"
            '    git config --local remote.pushDefault ""; \\\n'
            "    git config --local gc.auto 0; \\\n"
            "    git config --local pack.threads 2; \\\n"
            "    git config --local pack.windowMemory 128m; \\\n"
            "    git config --local pack.deltaCacheSize 32m; \\\n"
            "    git config --local pack.packSizeLimit 512m; \\\n"
            "    git config --local core.packedGitWindowSize 32m; \\\n"
            "    git config --local core.packedGitLimit 128m"
        )

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class CKEditor5ImageDefault(Image):
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
        return CKEditor5ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        packages = _test_packages(self.pr)
        return [
            File(".", "fix.patch", _sanitize_patch(self.pr.fix_patch)),
            File(".", "test.patch", _sanitize_patch(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "msb_reporter.js", _MSB_REPORTER_JS),
            File(".", "karma_overrides.js", _KARMA_OVERRIDES_JS),
            File(".", "prepare.sh", _prepare_sh(packages)),
            File(".", "run.sh", _run_sh(packages)),
            File(".", "test-run.sh", _test_run_sh(packages)),
            File(".", "fix-run.sh", _fix_run_sh(packages)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name() if isinstance(base, Image) else base
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        sha = self.pr.base.sha
        scrub = (
            Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)
            .replace("git gc --prune=now --aggressive;", "git gc --prune=now;")
            .replace(
                "git repack -a -d -l --quiet;",
                "git repack -a -d -l -f --window=10 --depth=50 --quiet;",
            )
            .strip()
        )

        header = (
            f"FROM {base_name}\n\n"
            f'ARG BASE_COMMIT="{sha}"\n'
            "ENV BASE_COMMIT=${BASE_COMMIT}\n\n"
            f"{self.global_env}\n\n"
            f"WORKDIR /home/{REPO_DIR}\n\n"
            f"{copy_commands}\n"
            f"RUN git reset --hard && git checkout --detach {sha}\n\n"
            "RUN bash /home/prepare.sh\n\n"
        )
        tail = f"\n\n{self.clear_env}\n" if self.clear_env else "\n"
        return header + scrub + tail


@Instance.register("ckeditor", NUMBER_INTERVAL)
class CKEDITOR5_12506_TO_8529(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return CKEditor5ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_package = re.compile(r"^MSB_PACKAGE:\s+(\S+)\s*$")
        re_test = re.compile(r"^MSB_TEST\s+(PASS|FAIL|SKIP)\s+(.*\S)\s*$")

        package = ""

        for raw in log.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue

            match = re_package.match(line)
            if match:
                package = match.group(1)
                continue

            match = re_test.match(line)
            if match:
                status, name = match.group(1), match.group(2)
                names = [package] if package else []
                names.append(name)
                test_id = "::".join(names)
                if status == "FAIL":
                    passed_tests.discard(test_id)
                    skipped_tests.discard(test_id)
                    failed_tests.add(test_id)
                elif status == "SKIP":
                    if test_id not in passed_tests and test_id not in failed_tests:
                        skipped_tests.add(test_id)
                else:
                    if test_id not in failed_tests:
                        skipped_tests.discard(test_id)
                        passed_tests.add(test_id)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
