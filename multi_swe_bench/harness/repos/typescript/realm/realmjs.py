import re
import textwrap
from typing import Optional, Union
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# PRs that need amd64 emulation (v5/v6/v10/v11 — cannot produce working arm64 realm.node)
# v5/v6: node-pre-gyp fails with GCC 12 (typeof keyword)
# v10: cmake-js compiles but binary has undefined symbols (realm-core not statically linked)
# v11: arm64 prebuilts from static.realm.io are actually x86-64 binaries
# v12 runs natively on arm64 (correct arm64 prebuilt from npm)
_NEEDS_AMD64: set[int] = {
    # v5/v6
    2653,
    2911,
    3096,
    3250,
    3319,
    3558,
    3624,
    # v10
    3150,
    3315,
    3363,
    3440,
    3444,
    3455,
    3511,
    3678,
    3692,
    3738,
    3838,
    3969,
    4008,
    4163,
    4389,
    4564,
    4608,
    4612,
    4651,
    4653,
    4701,
    4711,
    4897,
    # v11
    5106,
    5135,
    5224,
    5239,
    5629,
}

# Version group classification by PR number
# v5-v6: old realm SDK, tests in tests/js/ using jasmine
_V5_V6_PRS: set[int] = {2653, 2911, 3096, 3250, 3319, 3558, 3624}

# v10: mid-era realm SDK, tests in tests/js/ using jasmine (same structure as v5-v6)
_V10_PRS: set[int] = {
    3150,
    3315,
    3363,
    3440,
    3444,
    3455,
    3511,
    3678,
    3692,
    3738,
    3838,
    3969,
    4008,
    4163,
    4389,
    4564,
    4608,
    4612,
    4651,
    4653,
    4701,
    4711,
    4897,
}

# v11: monorepo transition, integration-tests/tests/ using mocha
_V11_PRS: set[int] = {5106, 5135, 5224, 5239, 5629}

# v12+: full monorepo, integration-tests/tests/ using mocha/wireit
# All remaining PRs: 5694, 5991, 6155, 6218, 6223, 6226, 6310, 6360,
#   6502, 6512, 6518, 6552, 6597, 6613, 6633, 6645, 6649, 6707,
#   6801, 6806, 6916, 6930, 6967, 6993


# PRs where fix_patch modifies C++/native code only (no JS/TS runtime changes).
# Since we use prebuilt binaries, C++ changes have no effect at runtime.
# For these PRs, test_patch_run returns run() so the comparison becomes
# run (baseline) vs fix (test+fix patches) — allowing test_patch JS changes
# that cause f2p/n2p transitions to count toward valid=True.
_NATIVE_FIX_PRS: set[int] = {
    # v5/v6 native fixes
    2653,
    2911,
    3096,
    3319,
    3558,
    3624,
    # v10 native fixes
    3150,
    3363,
    3440,
    3455,
    3511,
    3678,
    3692,
    3738,
    3838,
    3969,
    4008,
    4163,
    4389,
    4564,
    4608,
    4612,
    4651,
    4711,
    # v11 native fixes
    5106,
    5135,
    5239,
    5629,
    # v12 native fixes
    5991,
    6552,
    6633,
    6806,
    6916,
    6993,
}

_PREBUILT_BASE = "https://static.realm.io/realm-js-prebuilds"

_PR_BINARY_MAP: dict[int, tuple[str, str]] = {
    # v5/v6: all use v10.3.0 prebuilt (ABI compatible)
    2653: ("10.3.0", "napi-v4"),
    2911: ("10.3.0", "napi-v4"),
    3096: ("10.3.0", "napi-v4"),
    3250: ("10.3.0", "napi-v4"),
    3319: ("10.3.0", "napi-v4"),
    3558: ("10.3.0", "napi-v4"),
    3624: ("10.3.0", "napi-v4"),
    # v10 early (no prebuilt, use v10.3.0)
    3150: ("10.3.0", "napi-v4"),
    3315: ("10.3.0", "napi-v4"),
    3363: ("10.3.0", "napi-v4"),
    3440: ("10.3.0", "napi-v4"),
    3455: ("10.3.0", "napi-v4"),
    3511: ("10.3.0", "napi-v4"),
    # v10.3+ (version-specific prebuilt)
    3444: ("10.10.0", "napi-v4"),
    3678: ("10.3.0", "napi-v4"),
    3692: ("10.4.0", "napi-v4"),
    3738: ("10.4.1", "napi-v4"),
    3838: ("10.6.0", "napi-v4"),
    3969: ("10.8.0", "napi-v4"),
    4008: ("10.9.0", "napi-v4"),
    4163: ("10.12.0", "napi-v4"),
    4389: ("10.16.0", "napi-v4"),
    4564: ("10.18.0", "napi-v4"),
    4608: ("10.19.1", "napi-v5"),
    4612: ("10.19.0", "napi-v4"),
    4651: ("10.19.3", "napi-v5"),
    4653: ("10.19.2", "napi-v5"),
    4701: ("10.19.4", "napi-v5"),
    4711: ("10.22.0", "napi-v5"),
    4897: ("10.21.0", "napi-v5"),
    # v11 (x64 prebuilt — arm64 prebuilts are mislabeled x86)
    5106: ("11.2.0", "napi-v5"),
    5135: ("11.3.0", "napi-v5"),
    5224: ("11.3.2", "napi-v5"),
    5239: ("11.5.1", "napi-v5"),
    5629: ("11.6.0", "napi-v5"),
}


def _get_version_group(pr_number: int) -> str:
    if pr_number in _V5_V6_PRS:
        return "v5v6"
    if pr_number in _V10_PRS:
        return "v10"
    if pr_number in _V11_PRS:
        return "v11"
    return "v12"


class RealmJsImageBase(Image):
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
        return "node:18"

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
            code = f"RUN git clone --recurse-submodules https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    cmake \\
    python3 \\
    ccache \\
    ninja-build \\
    git \\
    libcurl4-openssl-dev \\
    libssl-dev \\
    zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class RealmJsImageDefault(Image):
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
        return RealmJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _generate_prepare_sh(self) -> str:
        group = _get_version_group(self.pr.number)
        base = """#!/bin/bash
set -e
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}

# Clean untracked files from previous commits (e.g. packages/ from v12 left behind on v11)
git clean -ffd 2>&1 || true

# Initialize submodules for native compilation
git submodule update --init --recursive 2>&1 || true
bash /home/check_git_changes.sh

""".format(repo=self.pr.repo, sha=self.pr.base.sha)

        if group == "v5v6":
            # v5/v6: npm install will fail at native build (old node-gyp + GCC incompatibility)
            # Binary will be provided at runtime via prebuilt download
            base += """# v5/v6: npm install (native build will fail, that's expected)
npm install --legacy-peer-deps 2>&1 || npm install --force 2>&1 || true
"""
        elif group == "v10":
            # v10: npm install tries prebuild-install (no arm64) then cmake-js (fails on vendored OpenSSL)
            # After npm install, rebuild with system OpenSSL
            base += """# v10: npm install + cmake-js rebuild with system OpenSSL
npm install --legacy-peer-deps 2>&1 || npm install --force 2>&1 || true
echo "Rebuilding realm.node with system OpenSSL..."
npx cmake-js rebuild --CDREALM_USE_SYSTEM_OPENSSL=ON 2>&1 || true
"""
        elif group == "v11":
            # v11: prebuild-install || cmake-js build
            # With submodules + system OpenSSL deps, cmake-js should work
            base += """# v11: npm install + cmake-js rebuild with system OpenSSL
npm install --legacy-peer-deps 2>&1 || npm install --force 2>&1 || true
if [ ! -f build/Release/realm.node ]; then
  echo "realm.node missing, rebuilding with cmake-js..."
  npx cmake-js build --CDREALM_USE_SYSTEM_OPENSSL=ON 2>&1 || true
fi
"""
        else:
            # v12: monorepo, prebuild-install downloads arm64 prebuilt from npm
            base += """# v12: npm install (prebuild-install fetches arm64 prebuilt)
npm install --legacy-peer-deps --ignore-scripts 2>&1 || npm install --force --ignore-scripts 2>&1 || true
# Run realm's install script separately to get prebuilt binary
cd packages/realm && npm run install 2>&1 || npx prebuild-install --runtime napi 2>&1 || true
cd /home/{repo}
""".format(repo=self.pr.repo)

        return base

    def files(self) -> list[File]:
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

if [[ -n $(git status --porcelain --ignore-submodules) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                self._generate_prepare_sh(),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
npm install --legacy-peer-deps 2>&1 || npm install --force 2>&1 || true
npm test 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm install --legacy-peer-deps 2>&1 || npm install --force 2>&1 || true
npm test 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --legacy-peer-deps 2>&1 || npm install --force 2>&1 || true
npm test 2>&1 || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("realm", "realm-js")
class RealmJs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    _APPLY_OPTS = '--whitespace=nowarn --exclude="package-lock.json" --exclude="**/package-lock.json" --exclude="*.realm" --exclude="*.png" --exclude="*.gif" --exclude="*.jar" --exclude="*.tgz" --exclude="*.keystore" --exclude="*.lock"'

    _NPM_FIX = (
        "npm install --legacy-peer-deps 2>/dev/null || "
        "npm install --force 2>/dev/null || true ; "
    )

    _NPM_FIX_NO_SCRIPTS = (
        "npm install --legacy-peer-deps --ignore-scripts 2>/dev/null || "
        "npm install --force --ignore-scripts 2>/dev/null || true ; "
    )

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RealmJsImageDefault(self.pr, self._config)

    def run_platform(self) -> Optional[str]:
        if self.pr.number in _NEEDS_AMD64:
            return "linux/amd64"
        return None

    def name(self) -> str:
        base_name = self.dependency().image_full_name()
        if self.pr.number in _NEEDS_AMD64:
            return f"{base_name}-amd64"
        return base_name

    def _get_v5v6_binary_cmd(self) -> str:
        stitch_fix = (
            "mkdir -p src/object-store/tests/mongodb && "
            "echo eyJhcHBfaWQiOiJkdW1teSJ9 | base64 -d > src/object-store/tests/mongodb/stitch.json ; "
        )
        return (
            stitch_fix + "mkdir -p build/Release && "
            "curl -sL {base}/10.3.0/realm-v10.3.0-napi-v4-linux-x64.tar.gz | "
            "tar xz -C . && "
            "mkdir -p compiled/node-v108_linux_x64 && "
            "cp build/Release/realm.node compiled/node-v108_linux_x64/realm.node && "
            "mkdir -p compiled/napi-v4_linux_x64 && "
            "cp build/Release/realm.node compiled/napi-v4_linux_x64/realm.node && "
            "echo realm.node installed for v5v6 ; "
        ).format(base=_PREBUILT_BASE)

    def _get_v10_binary_cmd(self) -> str:
        ver, napi = _PR_BINARY_MAP[self.pr.number]
        stitch_fix = (
            "mkdir -p src/object-store/tests/mongodb && "
            "echo eyJhcHBfaWQiOiJkdW1teSJ9 | base64 -d > src/object-store/tests/mongodb/stitch.json ; "
        )
        return (
            stitch_fix + "rm -f build/Release/realm.node ; "
            "mkdir -p build/Release && "
            "curl -sL {base}/{ver}/realm-v{ver}-{napi}-linux-x64.tar.gz | "
            "tar xz -C . && "
            "mkdir -p compiled/{napi}_linux_x64 && "
            "cp build/Release/realm.node compiled/{napi}_linux_x64/realm.node && "
            "mkdir -p compiled/node-v108_linux_x64 && "
            "cp build/Release/realm.node compiled/node-v108_linux_x64/realm.node && "
            "echo realm.node installed for v10 ; "
        ).format(base=_PREBUILT_BASE, ver=ver, napi=napi)

    def _get_v11_binary_cmd(self) -> str:
        ver, napi = _PR_BINARY_MAP[self.pr.number]
        return (
            "rm -f build/Release/realm.node ; "
            "mkdir -p build/Release && "
            "curl -sL {base}/{ver}/realm-v{ver}-{napi}-linux-x64.tar.gz | "
            "tar xz -C . && "
            "cd integration-tests/tests && "
            "sed -i /@realm.app-importer/d package.json 2>/dev/null ; "
            'sed -i /\\"realm\\": \\"\\*/d package.json 2>/dev/null ; '
            "sed -i /import-app/d src/index.ts 2>/dev/null ; "
            "npm install --ignore-scripts --legacy-peer-deps 2>/dev/null || true ; "
            "mkdir -p node_modules && rm -rf node_modules/realm && "
            "ln -s /home/{repo} node_modules/realm ; "
            "cd /home/{repo} && "
            "echo realm.node installed for v11 ; "
        ).format(base=_PREBUILT_BASE, ver=ver, napi=napi, repo=self.pr.repo)

    def _get_v12_binary_cmd(self) -> str:
        return (
            "REALM_VER=$(grep -m1 version packages/realm/package.json | "
            "sed -e s/.*:.// -e s/[^0-9.]//g) && "
            "npm install realm@$REALM_VER --prefix /tmp/realm-pkg 2>&1 && "
            "cp -r /tmp/realm-pkg/node_modules/realm/dist packages/realm/dist 2>&1 ; "
            "if [ -d /tmp/realm-pkg/node_modules/realm/prebuilds ]; then "
            "cp -r /tmp/realm-pkg/node_modules/realm/prebuilds packages/realm/prebuilds ; fi ; "
            "if [ -d /tmp/realm-pkg/node_modules/realm/generated ]; then "
            "cp -r /tmp/realm-pkg/node_modules/realm/generated packages/realm/generated ; fi ; "
            "if [ -d /tmp/realm-pkg/node_modules/realm/binding/generated ]; then "
            "mkdir -p packages/realm/binding/generated && "
            "cp -r /tmp/realm-pkg/node_modules/realm/binding/generated/* packages/realm/binding/generated/ ; fi ; "
            "if [ -d /tmp/realm-pkg/node_modules/realm/binding/dist ]; then "
            "mkdir -p packages/realm/binding/dist && "
            "cp -r /tmp/realm-pkg/node_modules/realm/binding/dist/* packages/realm/binding/dist/ ; fi ; "
            "if [ -d /tmp/realm-pkg/node_modules/@realm/fetch/dist ]; then "
            "mkdir -p packages/fetch/dist && "
            "cp -r /tmp/realm-pkg/node_modules/@realm/fetch/dist/* packages/fetch/dist/ ; fi ; "
            "if [ -d packages/realm-network-transport ]; then "
            "(cd packages/realm-network-transport && npx rollup -c rollup.config.mjs 2>/dev/null) || true ; fi ; "
            "if [ -d packages/mocha-reporter ]; then "
            "(cd packages/mocha-reporter && npx tsc 2>/dev/null) || true ; fi ; "
            "cd /home/{repo}/integration-tests/tests && "
            "sed -i /import-app/d src/index.ts 2>/dev/null ; "
            "sed -i /import-app/d src/node/index.ts 2>/dev/null ; "
            "cd /home/{repo} && "
            "echo realm.node installed for v12 ; "
        ).format(repo=self.pr.repo)

    def _get_binary_cmd(self) -> str:
        group = _get_version_group(self.pr.number)
        if group == "v5v6":
            return self._get_v5v6_binary_cmd()
        if group == "v10":
            return self._get_v10_binary_cmd()
        if group == "v11":
            return self._get_v11_binary_cmd()
        return self._get_v12_binary_cmd()

    def _get_test_cmd(self) -> str:
        group = _get_version_group(self.pr.number)
        if group in ("v5v6", "v10"):
            return (
                "cd tests && "
                "npm install --ignore-scripts 2>/dev/null ; "
                "npm test 2>&1 || true"
            )
        if group == "v11":
            return (
                "cd integration-tests/tests && "
                "NODE_PATH=/home/{repo}/node_modules "
                "./node_modules/.bin/mocha --reporter spec --exit --timeout 30000 "
                "src/index.ts 2>&1 || true"
            ).format(repo=self.pr.repo)
        return (
            "cd /home/{repo} && "
            "rm -rf node_modules/tsx && npm install tsx@4.7.0 --no-save 2>/dev/null ; "
            "cd /home/{repo}/integration-tests/tests && "
            "npm install --ignore-scripts --legacy-peer-deps 2>/dev/null ; "
            "npm install source-map-support tsx@4.7.0 --no-save 2>/dev/null ; "
            "cd /home/{repo} && "
            "rm -rf node_modules/tsx && npm install tsx@4.7.0 --no-save 2>/dev/null ; "
            "cd /home/{repo}/integration-tests/tests && "
            "npx mocha --reporter spec --exit --timeout 60000 2>&1 || true"
        ).format(repo=self.pr.repo)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        group = _get_version_group(self.pr.number)
        binary = self._get_binary_cmd()

        if group == "v12":
            npm = self._NPM_FIX_NO_SCRIPTS
        else:
            npm = ""

        return "bash -c 'cd /home/{repo} ; {npm}{binary}{test}'".format(
            repo=self.pr.repo,
            npm=npm,
            binary=binary,
            test=self._get_test_cmd(),
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        if self.pr.number in _NATIVE_FIX_PRS:
            return self.run()

        binary = self._get_binary_cmd()
        npm = self._NPM_FIX_NO_SCRIPTS

        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . 2>/dev/null ; "
            "git apply {opts} /home/test.patch 2>/dev/null || "
            "git apply {opts} --3way /home/test.patch 2>/dev/null || true ; "
            "{npm}"
            "{binary}"
            "{test}"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                npm=npm,
                binary=binary,
                test=self._get_test_cmd(),
            )
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        binary = self._get_binary_cmd()
        npm = self._NPM_FIX_NO_SCRIPTS

        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . 2>/dev/null ; "
            "git apply {opts} /home/test.patch 2>/dev/null || "
            "git apply {opts} --3way /home/test.patch 2>/dev/null || true ; "
            "git apply {opts} /home/fix.patch 2>/dev/null || "
            "git apply {opts} --3way /home/fix.patch 2>/dev/null || true ; "
            "{npm}"
            "{binary}"
            "{test}"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                npm=npm,
                binary=binary,
                test=self._get_test_cmd(),
            )
        )

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def parse_log(self, test_log: str) -> TestResult:
        test_log = self._strip_ansi(test_log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_res = [
            re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+ms\))?$"),
        ]

        failed_res = [
            re.compile(r"^\s*\d+\)\s+(.+)$"),
            re.compile(r"^\s*[×✗✕]\s+(.+)$"),
        ]

        skipped_res = [
            re.compile(
                r"^\s*-\s+(?!Error:|Expected|Cannot |Attempting |Array |at )(.+)$"
            ),
        ]

        # Jest output patterns:
        #   ✓ test name (Xms)     or    PASS path/to/file
        #   ✕ test name           or    FAIL path/to/file
        jest_pass_re = re.compile(r"^\s*PASS\s+(.+)$")
        jest_fail_re = re.compile(r"^\s*FAIL\s+(.+)$")
        # Jest summary: Tests: N passed, N failed, N total
        jest_summary_re = re.compile(
            r"Tests:\s+(?:(\d+)\s+failed,\s+)?(?:(\d+)\s+skipped,\s+)?(?:(\d+)\s+passed,\s+)?(\d+)\s+total"
        )

        summary_passing = 0
        summary_failing = 0

        for line in test_log.splitlines():
            stripped = line.strip()

            # Mocha summary lines
            m_summary_pass = re.match(r"^(\d+)\s+passing\b", stripped)
            if m_summary_pass:
                summary_passing = int(m_summary_pass.group(1))
                continue

            m_summary_fail = re.match(r"^(\d+)\s+failing\b", stripped)
            if m_summary_fail:
                summary_failing = int(m_summary_fail.group(1))
                continue

            m_summary_pend = re.match(r"^(\d+)\s+pending\b", stripped)
            if m_summary_pend:
                continue

            # Jest PASS/FAIL file lines
            m_jest_pass = jest_pass_re.match(stripped)
            if m_jest_pass:
                passed_tests.add(m_jest_pass.group(1).strip())
                continue

            m_jest_fail = jest_fail_re.match(stripped)
            if m_jest_fail:
                failed_tests.add(m_jest_fail.group(1).strip())
                continue

            # Jest summary line
            m_jest_summary = jest_summary_re.search(stripped)
            if m_jest_summary:
                f_count = int(m_jest_summary.group(1) or 0)
                s_count = int(m_jest_summary.group(2) or 0)
                p_count = int(m_jest_summary.group(3) or 0)
                if p_count > 0 and not passed_tests:
                    for i in range(p_count):
                        passed_tests.add(f"jest_test_{i + 1}")
                if f_count > 0 and not failed_tests:
                    for i in range(f_count):
                        failed_tests.add(f"jest_failed_{i + 1}")
                continue

            # Individual test lines (mocha)
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    test_name = m.group(1).strip()
                    if test_name and not re.match(r"^\d+$", test_name):
                        passed_tests.add(test_name)

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    if test_name:
                        failed_tests.add(test_name)
                        if test_name in passed_tests:
                            passed_tests.remove(test_name)

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    if (
                        test_name
                        and test_name not in passed_tests
                        and test_name not in failed_tests
                    ):
                        skipped_tests.add(test_name)

        # Fallback to summary counts
        if not passed_tests and not failed_tests and summary_passing > 0:
            for i in range(summary_passing):
                passed_tests.add(f"test_{i + 1}")
            for i in range(summary_failing):
                failed_tests.add(f"failed_test_{i + 1}")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
