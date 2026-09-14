import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:12-bullseye"
BASE_TAG = "base"
RESULTS_MARKER = "----- per-test results -----"

_SPEC_FILES = re.compile(r"^diff --git a/(\S+\.spec\.js) b/", re.M)
_PASSED = re.compile(r"^===TEST=== PASS (.+)$", re.M)
_FAILED = re.compile(r"^===TEST=== FAIL (.+)$", re.M)
_SKIPPED = re.compile(r"^===TEST=== SKIP (.+)$", re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _targets(pr: PullRequest) -> str:
    dirs = {f.rsplit("/", 1)[0] for f in _SPEC_FILES.findall(pr.test_patch)}
    return " ".join(sorted(dirs))


def _run_tests_sh(pr: PullRequest) -> str:
    return (
        "#!/bin/bash\n"
        f"cd /home/{pr.repo}\n"
        "export NODE_ENV=test\n"
        "RESULTS=/tmp/msb-results.txt\n"
        ': > "$RESULTS"\n'
        f'TARGETS="{_targets(pr)}"\n'
        "SUITES=$(find $TARGETS -type f -name '*.spec.js' 2>/dev/null | sort -u)\n"
        "for suite in $SUITES; do\n"
        '  out="/tmp/msb-$(echo "$suite" | tr / _).json"\n'
        '  rm -f "$out"\n'
        '  npx mocha --reporter json "$suite" > "$out" 2>/dev/null < /dev/null\n'
        '  SUITE="$suite" OUT="$out" node >> "$RESULTS" <<\'JS\'\n'
        "const fs = require('fs');\n"
        "const file = process.env.SUITE;\n"
        "const clean = (s) => String(s).replace(/\\s+/g, ' ').trim();\n"
        "let report = {};\n"
        "try { report = JSON.parse(fs.readFileSync(process.env.OUT, 'utf8')); } catch (e) {}\n"
        "const lines = [];\n"
        "const add = (list, status) =>\n"
        "  (list || []).forEach((t) =>\n"
        "    lines.push('===TEST=== ' + status + ' ' + file + ' > ' + clean(t.fullTitle)),\n"
        "  );\n"
        "add(report.passes, 'PASS');\n"
        "add(report.failures, 'FAIL');\n"
        "add(report.pending, 'SKIP');\n"
        "const missing = ['===TEST=== FAIL ' + file + ' > SUITE_ERROR'].slice(\n"
        "  Math.min(1, lines.length),\n"
        ");\n"
        "console.log([...lines, ...missing].join('\\n'));\n"
        "JS\n"
        "done\n"
        f'echo "{RESULTS_MARKER}"\n'
        'cat "$RESULTS"\n'
    )


class IpfsDesktopImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        infra = DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n")
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

ENV NPM_CONFIG_PROGRESS=false
ENV NPM_CONFIG_COLOR=false
ENV NO_COLOR=1
ENV FORCE_COLOR=0
ENV CI=true
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class IpfsDesktopImageDefault(Image):
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
        return IpfsDesktopImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "git rev-parse --is-inside-work-tree > /dev/null\n"
            "git status --porcelain\n"
            'test -z "$(git status --porcelain)"\n'
            'echo "check_git_changes: No uncommitted changes"\n'
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "npm config set maxsockets 5\n"
            "npm ci --ignore-scripts --no-audit --no-fund"
            " || npm ci --ignore-scripts --no-audit --no-fund\n"
            "test -x node_modules/.bin/mocha\n"
            "node -e \"['mocha', 'chai', 'dirty-chai', 'sinon', 'proxyquire']"
            ".forEach((m) => require.resolve(m)); console.log('DEPS_OK')\"\n"
            "bash /home/check_git_changes.sh\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "bash /home/run_tests.sh\n"
        )

        test_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            "bash /home/run_tests.sh\n"
        )

        fix_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            "bash /home/run_tests.sh\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
            File(".", "run_tests.sh", _run_tests_sh(self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {image.image_full_name()}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("ipfs", "ipfs-desktop")
class IpfsDesktop(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return IpfsDesktopImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        section = _ANSI.sub("", test_log).rsplit(RESULTS_MARKER, 1)[-1]
        failed_tests = set(_FAILED.findall(section))
        passed_tests = set(_PASSED.findall(section)) - failed_tests
        skipped_tests = set(_SKIPPED.findall(section)) - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
