import re
from dataclasses import asdict, dataclass
from json import JSONDecoder
from typing import Generator, Optional, Union

from dataclasses_json import dataclass_json

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.mui.material_ui import (
    MaterialUi as _MaterialUiFallback,
)


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

    def dependency(self) -> Union[str, "Image"]:
        return "node:18"

    def image_tag(self) -> str:
        return "base39353to34158"

    def workdir(self) -> str:
        return "base39353to34158"

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

{DockerfileEnhancer._PROXY_ARGS}

{self.global_env}

{DockerfileEnhancer._ENV_BLOCK}
ENV LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/check_git_changes.sh

# --ignore-scripts blocks sharp / cypress / puppeteer / playwright from
# fetching prebuilt binaries via GitHub CDN (blocked in sandboxed envs).
# Safe: MUI test:unit uses only mocha + jsdom; no postinstall needed.
yarn install --network-timeout 600000 --ignore-scripts
[ -x /home/{pr.repo}/node_modules/.bin/mocha ] || {{ echo 'DEPS_FAILED: mocha binary missing after yarn install'; exit 1; }}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn run test:unit --reporter json 

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}

# MSWB probe (safety net): emit marker for every NEW test file added by
# test.patch. Classified as PASS at both stages when stub-gen (below) lets
# the file load; only survives as F2P if stub-gen misses an import shape.
grep -E '^(--- /dev/null|\+\+\+ b/)' /home/test.patch 2>/dev/null | \
    paste -d' ' - - | \
    grep -E '^--- /dev/null \+\+\+ b/.*\.test\.(js|ts|tsx)$' | \
    sed -E 's|^--- /dev/null \+\+\+ b/(.*)$|MSWB_PROBE_NEW_TEST_FILE: /home/{pr.repo}/\1|' \
    || true

git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch

# MSWB stub-gen: for each newly-added test file, extract ./X and ../X
# relative imports. If the target does not yet exist on disk (source module
# lives in fix.patch), create a .ts Proxy stub so mocha loads the test file
# and its reporter initializes. Test bodies then fail per-case at call time
# with a real error, giving real per-test F2P at fix-stage (fresh container,
# real source from fix.patch replaces stub). See MSWB_STUB_CREATED log lines.
grep -E '^(--- /dev/null|\+\+\+ b/)' /home/test.patch 2>/dev/null | \
    paste -d' ' - - | \
    grep -E '^--- /dev/null \+\+\+ b/.*\.test\.(js|ts|tsx)$' | \
    sed -E 's|^--- /dev/null \+\+\+ b/(.*)$|\1|' | \
    while IFS= read -r tf; do
        tf_abs="/home/{pr.repo}/$tf"
        tf_dir=$(dirname "$tf_abs")
        [ -f "$tf_abs" ] || continue
        grep -oE "(from ['\"]|require\(['\"])[.][^'\"]*['\"]" "$tf_abs" 2>/dev/null | \
            sed -E "s#(from ['\"]|require\(['\"])##" | \
            sed -E "s#['\"]\$##" | \
            sort -u | \
            while IFS= read -r imp; do
                case "$imp" in
                    ./*|../*) ;;
                    *) continue ;;
                esac
                target_base="$tf_dir/$imp"
                exists=""
                for cand in "$target_base.ts" "$target_base.tsx" "$target_base.js" "$target_base.jsx" "$target_base/index.ts" "$target_base/index.tsx" "$target_base/index.js" "$target_base/index.jsx" "$target_base"; do
                    if [ -e "$cand" ]; then
                        exists=1
                        break
                    fi
                done
                if [ -z "$exists" ]; then
                    stub_path="$target_base.ts"
                    mkdir -p "$(dirname "$stub_path")"
                    cat > "$stub_path" <<'MSWB_STUB_EOF'
// MSWB_STUB: created at test-stage; real source module lives in fix.patch.
const _mswb_stub: any = new Proxy(function () {{
  throw new Error('MSWB_STUB: source module unavailable at test-stage');
}}, {{
  get: function () {{ return _mswb_stub; }},
}});
export default _mswb_stub;
module.exports = _mswb_stub;
module.exports.default = _mswb_stub;
MSWB_STUB_EOF
                    echo "MSWB_STUB_CREATED: $stub_path"
                fi
            done
    done

yarn run test:unit --reporter json || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}

# MSWB probe: emit the SAME marker as test-run.sh for every NEW test file
# added by test.patch. At fix-stage the source module exists too, so the
# file loads and its tests appear in mocha JSON → probe classified as PASS
# by the parser. Do NOT add "|| true" to the mocha line here: a fix-stage
# crash means the fix did not actually fix compilation and must hard-fail.
grep -E '^(--- /dev/null|\+\+\+ b/)' /home/test.patch 2>/dev/null | \
    paste -d' ' - - | \
    grep -E '^--- /dev/null \+\+\+ b/.*\.test\.(js|ts|tsx)$' | \
    sed -E 's|^--- /dev/null \+\+\+ b/(.*)$|MSWB_PROBE_NEW_TEST_FILE: /home/{pr.repo}/\1|' \
    || true

git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch /home/fix.patch
yarn run test:unit --reporter json 

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

        # Strict anti-reward-hack hardening at the PR layer with this PR's LITERAL
        # base.sha (shared base keeps full history; each PR strips its own image).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
{hardening}

RUN bash /home/prepare.sh

{self.clear_env}
"""


# Override of "material-ui" is last-writer-wins in Instance._registry and
# depends on mui/__init__.py importing this file AFTER material_ui.py and
# after any other module that registers "material-ui". Preserve that order.
@Instance.register("mui", "material-ui_39353_to_34158")
@Instance.register("mui", "material-ui")
class MaterialUi39353to34158(Instance):
    _RANGE_MIN = 34158
    _RANGE_MAX = 39353

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._args = args
        self._kwargs = kwargs
        self._fallback_cached: Optional[_MaterialUiFallback] = None

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def _in_range(self) -> bool:
        return self._RANGE_MIN <= self.pr.number <= self._RANGE_MAX

    @property
    def _fallback(self) -> _MaterialUiFallback:
        if self._fallback_cached is None:
            self._fallback_cached = _MaterialUiFallback(
                self._pr, self._config, *self._args, **self._kwargs
            )
        return self._fallback_cached

    def dependency(self) -> Optional[Image]:
        if self._in_range:
            return ImageDefault(self.pr, self._config)
        return self._fallback.dependency()

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        if self._in_range:
            return "bash /home/run.sh"
        return self._fallback.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        if self._in_range:
            return "bash /home/test-run.sh"
        return self._fallback.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        if self._in_range:
            return "bash /home/fix-run.sh"
        return self._fallback.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        if self._in_range:
            return _parse_mocha_log(test_log)
        return self._fallback.parse_log(test_log)


def _parse_mocha_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    @dataclass_json
    @dataclass
    class MaterialUiStats:
        suites: int
        tests: int
        passes: int
        pending: int
        failures: int
        start: str
        end: str
        duration: int

        @classmethod
        def from_dict(cls, d: dict) -> "TestResult":
            return cls(**d)

        @classmethod
        def from_json(cls, json_str: str) -> "TestResult":
            return cls.from_dict(cls.schema().loads(json_str))

        def dict(self) -> dict:
            return asdict(self)

        def json(self) -> str:
            return self.to_json(ensure_ascii=False)

    @dataclass_json
    @dataclass
    class MaterialUiTest:
        title: str
        fullTitle: str
        currentRetry: int
        err: dict
        file: Optional[str] = None
        duration: Optional[int] = None
        speed: Optional[str] = None

        @classmethod
        def from_dict(cls, d: dict) -> "TestResult":
            return cls(**d)

        @classmethod
        def from_json(cls, json_str: str) -> "TestResult":
            return cls.from_dict(cls.schema().loads(json_str))

        def dict(self) -> dict:
            return asdict(self)

        def json(self) -> str:
            return self.to_json(ensure_ascii=False)

    @dataclass_json
    @dataclass
    class MaterialUiInfo:
        stats: MaterialUiStats
        tests: list[MaterialUiTest]
        pending: list[MaterialUiTest]
        failures: list[MaterialUiTest]
        passes: list[MaterialUiTest]

        @classmethod
        def from_dict(cls, d: dict) -> "MaterialUiInfo":
            return cls(**d)

        @classmethod
        def from_json(cls, json_str: str) -> "MaterialUiInfo":
            return cls.from_dict(cls.schema().loads(json_str))

        def dict(self) -> dict:
            return asdict(self)

        def json(self) -> str:
            return self.to_json(ensure_ascii=False)

    def extract_json_objects(
        text: str, decoder=JSONDecoder()
    ) -> Generator[dict, None, None]:
        pos = 0
        while True:
            match = text.find("{", pos)
            if match == -1:
                break
            try:
                result, index = decoder.raw_decode(text[match:])
                yield result
                pos = match + index
            except ValueError:
                pos = match + 1

    if "Building new" in test_log:
        test_log = test_log[test_log.find("Building new", 0) :]

    re_removes = [
        re.compile(r"error Command failed with exit code \d+\.", re.DOTALL),
    ]
    for re_remove in re_removes:
        test_log = re_remove.sub("", test_log)

    original_log = test_log
    test_log = test_log.replace("\r\n", "")
    test_log = test_log.replace("\n", "")

    for obj in extract_json_objects(test_log):
        try:
            info = MaterialUiInfo.from_dict(obj)
        except (KeyError, TypeError):
            continue
        for test in info.passes:
            test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle

            passed_tests.add(test_id)
        for test in info.failures:
            test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle

            failed_tests.add(test_id)
        for test in info.pending:
            test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle

            skipped_tests.add(test_id)

    for test in failed_tests:
        if test in passed_tests:
            passed_tests.remove(test)
        if test in skipped_tests:
            skipped_tests.remove(test)

    for test in skipped_tests:
        if test in passed_tests:
            passed_tests.remove(test)

    if not passed_tests and not failed_tests and not skipped_tests:
        clean_log = re.sub(r'\x1b\[[0-9;]*m', '', original_log)

        vitest_match = re.search(
            r"Tests\s+(\d+)\s+failed\s*\|\s*(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
            clean_log,
        )
        if not vitest_match:
            vitest_match = re.search(
                r"Tests\s+(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
                clean_log,
            )
            if vitest_match:
                vp = int(vitest_match.group(1) or 0)
                vs = int(vitest_match.group(2) or 0)
                for i in range(vp):
                    passed_tests.add(f"vitest_pass_{i}")
                for i in range(vs):
                    skipped_tests.add(f"vitest_skip_{i}")
        else:
            vf = int(vitest_match.group(1) or 0)
            vp = int(vitest_match.group(2) or 0)
            vs = int(vitest_match.group(3) or 0)
            if vp > 0 or vf > 0:
                for i in range(vp):
                    passed_tests.add(f"vitest_pass_{i}")
                for i in range(vf):
                    failed_tests.add(f"vitest_fail_{i}")
                for i in range(vs):
                    skipped_tests.add(f"vitest_skip_{i}")

        if not passed_tests and not failed_tests:
            dot_pass = re.search(r"(\d+)\s+passing", clean_log)
            dot_fail = re.search(r"(\d+)\s+failing", clean_log)
            dot_pend = re.search(r"(\d+)\s+pending", clean_log)
            dp = int(dot_pass.group(1)) if dot_pass else 0
            df = int(dot_fail.group(1)) if dot_fail else 0
            ds = int(dot_pend.group(1)) if dot_pend else 0
            if dp > 0 or df > 0:
                for i in range(dp):
                    passed_tests.add(f"dot_pass_{i}")
                for i in range(df):
                    failed_tests.add(f"dot_fail_{i}")
                for i in range(ds):
                    skipped_tests.add(f"dot_skip_{i}")

    # Classify MSWB_PROBE_NEW_TEST_FILE markers (from test-run.sh/fix-run.sh)
    # as PASS if the file's tests loaded, FAIL otherwise. Deterministic id →
    # only "fail at test + pass at fix" promotes to F2P (pr-37244 signal-loss).
    probe_files: set[str] = set()
    for line in original_log.splitlines():
        m = re.match(r"^MSWB_PROBE_NEW_TEST_FILE:\s*(.+)$", line.strip())
        if m:
            probe_files.add(m.group(1).strip())

    all_seen = passed_tests | failed_tests | skipped_tests
    for probe_file in probe_files:
        probe_id = f"{probe_file}:__mswb_probe_file_loaded__"
        if any(t.startswith(probe_file + ":") for t in all_seen):
            passed_tests.add(probe_id)
        else:
            failed_tests.add(probe_id)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )
