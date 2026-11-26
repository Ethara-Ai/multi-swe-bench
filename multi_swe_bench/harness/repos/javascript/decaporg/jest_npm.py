import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24-bookworm"

_TAG_PREFIX = "jest"

_SYNTAX_DIRECTIVE = DockerfileEnhancer.SYNTAX_DIRECTIVE

_PROXY_ARGS = DockerfileEnhancer._PROXY_ARGS

_ENV_BLOCK = DockerfileEnhancer._ENV_BLOCK

_CERT_SYMLINKS = DockerfileEnhancer._CERT_SYMLINKS

_ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

_APT_PACKAGES = "git ca-certificates locales"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_RESULT_RE = re.compile(r"^MSWE_TEST (passed|failed|skipped|pending|todo) (.+?)\s*$", re.MULTILINE)

_SECTION_RE = re.compile(r"(?m)(?=^diff --git )")

_BINARY_RE = re.compile(r"(?m)^(Binary files .* differ|GIT binary patch)$")

_STATUS = {"passed": "passed", "failed": "failed", "skipped": "skipped", "pending": "skipped", "todo": "skipped"}

_RANK = {"passed": 0, "skipped": 1, "failed": 2}

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || { echo "check_git_changes: Not inside a git repository"; exit 1; }
test -z "$(git status --porcelain)" || { echo "check_git_changes: Uncommitted changes"; git status --porcelain | head -20; exit 1; }
echo "check_git_changes: No uncommitted changes"
exit 0
"""

_REPORT_JS = """const fs = require('fs');
const path = require('path');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const root = process.argv[3];
const say = (status, name) => console.log('MSWE_TEST ' + status + ' ' + name.replace(/\\s+/g, ' '));
for (const file of data.testResults || []) {
  const rel = path.relative(root, file.name);
  for (const t of file.assertionResults || []) {
    say(t.status, [rel].concat(t.ancestorTitles || [], [t.title]).join(' > '));
  }
  ((file.assertionResults || []).length === 0 && file.status === 'failed' ? [rel] : []).forEach(r => say('failed', r + ' > <file load>'));
}
"""

_TEST_CMD_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
FILES=$(sed -nE 's#^\\+\\+\\+ b/([^[:space:]]+).*#\\1#p' /home/test.patch | sed -nE '/^packages\\/.*\\.(spec|test)\\.[jt]sx?$/p' | sort -u | while read -r f; do [ ! -f "$f" ] || echo "$f"; done)
echo "TEST_FILES: ${FILES:-none}"
test -n "${FILES}" || { echo "NO_TEST_FILES"; exit 0; }
rm -f /tmp/jest-report.json
rc=0
npx cross-env NODE_ENV=test jest --no-cache --ci --json --outputFile=/tmp/jest-report.json ${FILES} || rc=1
test -s /tmp/jest-report.json && node /home/report.js /tmp/jest-report.json /home/__REPO__
exit ${rc}
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __BASE_SHA__^{commit} 2>/dev/null || git fetch --quiet https://github.com/__ORG__/__REPO__.git __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__BASE_SHA__"

npm --version
npm ci --no-audit --no-fund
git reset --hard
echo DEPS_OK
"""

_STAGE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git reset --hard
git clean -qfd
__APPLY__
bash /home/test-cmd.sh
"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__REPO__", pr.repo)
        .replace("__ORG__", pr.org)
        .replace("__BASE_SHA__", pr.base.sha)
    )


def _sanitize_patch(patch: str) -> str:
    return "".join(filter(lambda section: not _BINARY_RE.search(section), _SECTION_RE.split(patch or "")))


class DecapJestNpmImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"{_TAG_PREFIX}-base"

    def workdir(self) -> str:
        return f"{_TAG_PREFIX}-base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        fetch = {
            True: f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}',
            False: f"COPY {self.pr.repo} /home/{self.pr.repo}",
        }[bool(self.config.need_clone)]

        return f"""{_SYNTAX_DIRECTIVE}
FROM {_NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{_PROXY_ARGS}

{_ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_CERT_SYMLINKS}

{self.global_env}

ENV CI=true \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    CYPRESS_INSTALL_BINARY=0 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    LC_ALL=C.UTF-8

RUN /bin/bash -o pipefail -c "( apt-get update && apt-get install -y --no-install-recommends {_APT_PACKAGES} && rm -rf /var/lib/apt/lists/* ) 2>&1 | {_ASCII_FILTER}"

RUN git config --system --add safe.directory '*'

WORKDIR /home/

{fetch}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class DecapJestNpmImageDefault(Image):
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
        return DecapJestNpmImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        stage = _render(_STAGE_SH, self.pr)
        return [
            File(".", "fix.patch", _sanitize_patch(self.pr.fix_patch)),
            File(".", "test.patch", _sanitize_patch(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "report.js", _REPORT_JS),
            File(".", "test-cmd.sh", _render(_TEST_CMD_SH, self.pr)),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run.sh", stage.replace("__APPLY__", "")),
            File(
                ".",
                "test-run.sh",
                stage.replace("__APPLY__", "git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                stage.replace(
                    "__APPLY__",
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}

RUN /bin/bash -o pipefail -c "bash /home/prepare.sh 2>&1 | {_ASCII_FILTER}"

WORKDIR /home/{self.pr.repo}

{self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)}

{self.clear_env}

CMD ["/bin/bash"]

"""


@Instance.register("decaporg", "jest_npm")
class DECAP_CMS_JEST_NPM(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DecapJestNpmImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = _ANSI_RE.sub("", test_log).replace("\r", "")
        results: dict[str, str] = {}
        for raw, name in _RESULT_RE.findall(log):
            status = _STATUS[raw]
            results[name] = max(results.get(name, status), status, key=_RANK.get)

        buckets: dict[str, set[str]] = {"passed": set(), "failed": set(), "skipped": set()}
        for name, status in results.items():
            buckets[status].add(name)

        passed_tests = buckets["passed"] - buckets["failed"] - buckets["skipped"]
        skipped_tests = buckets["skipped"] - buckets["failed"]
        failed_tests = buckets["failed"]

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
