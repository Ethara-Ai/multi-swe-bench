import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22-bookworm"

_TAG_PREFIX = "yarn-vitest"

_SYNTAX_DIRECTIVE = DockerfileEnhancer.SYNTAX_DIRECTIVE

_PROXY_ARGS = DockerfileEnhancer._PROXY_ARGS

_ENV_BLOCK = DockerfileEnhancer._ENV_BLOCK

_CERT_SYMLINKS = DockerfileEnhancer._CERT_SYMLINKS

_ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

_APT_PACKAGES = "git ca-certificates locales python3 python3-dev python3-pip python3-venv g++ make"

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
for (const file of data.testResults || []) {
  const rel = path.relative(root, file.name);
  for (const t of file.assertionResults || []) {
    const name = [rel].concat(t.ancestorTitles || [], [t.title]).join(' > ');
    console.log('MSWE_TEST ' + t.status + ' ' + name.replace(/\\s+/g, ' '));
  }
}
"""

_REPORT_PY = """import sys
import xml.etree.ElementTree as ET

ORDER = (("failure", "failed"), ("error", "failed"), ("skipped", "skipped"))

for case in ET.parse(sys.argv[1]).getroot().iter("testcase"):
    status = (list(filter(None, ((case.find(tag) is not None) and label for tag, label in ORDER))) + ["passed"])[0]
    name = "client-python::" + "::".join(filter(None, (case.get("classname", ""), case.get("name", ""))))
    print("MSWE_TEST " + status + " " + " ".join(name.split()))
"""

_TEST_CMD_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
ALL=$(sed -nE 's#^\\+\\+\\+ b/([^[:space:]]+).*#\\1#p' /home/test.patch | sort -u | while read -r f; do [ ! -f "$f" ] || echo "$f"; done)
BACK=$(echo "${ALL}" | sed -nE 's#^opencti-platform/opencti-graphql/(tests/01-unit/.*\\.(js|ts))$#\\1#p')
FRONT=$(echo "${ALL}" | sed -nE 's#^opencti-platform/opencti-front/(src/.*\\.test\\.(ts|tsx))$#\\1#p')
PY=$(echo "${ALL}" | sed -nE 's#^client-python/(tests/01-unit/.*\\.py)$#\\1#p')
echo "TEST_FILES: backend=[${BACK}] frontend=[${FRONT}] python=[${PY}]"
test -n "${BACK}${FRONT}${PY}" || { echo "NO_TEST_FILES"; exit 0; }
rm -f /tmp/vitest-back.json /tmp/vitest-front.json /tmp/pytest-report.xml
rc=0
[ -z "${BACK}" ] || ( cd opencti-platform/opencti-graphql && NODE_OPTIONS=--max_old_space_size=8192 yarn vitest run --config vitest.config.ci-unit.ts --reporter=json --outputFile=/tmp/vitest-back.json ${BACK} ) || rc=1
[ -z "${FRONT}" ] || ( cd opencti-platform/opencti-front && yarn relay && NODE_OPTIONS=--max_old_space_size=8192 yarn vitest run --reporter=json --outputFile=/tmp/vitest-front.json ${FRONT} ) || rc=1
[ -z "${PY}" ] || ( cd client-python && /opt/pyclient/bin/python -m pytest -p no:cacheprovider -rA --junitxml=/tmp/pytest-report.xml ${PY} ) || rc=1
for f in /tmp/vitest-back.json /tmp/vitest-front.json; do [ ! -s "$f" ] || node /home/report.js "$f" /home/__REPO__; done
[ ! -s /tmp/pytest-report.xml ] || python3 /home/report.py /tmp/pytest-report.xml
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

corepack enable
yarn --version
yarn install --immutable
( cd opencti-platform/opencti-front && yarn relay )
python3 -m venv /opt/pyclient
/opt/pyclient/bin/pip install --no-cache-dir -r client-python/requirements.txt -r client-python/test-requirements.txt
/opt/pyclient/bin/pip install --no-cache-dir --no-deps -e ./client-python
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


class OpenctiImageBase(Image):
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


class OpenctiImageDefault(Image):
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
        return OpenctiImageBase(self.pr, self._config)

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
            File(".", "report.py", _REPORT_PY),
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

{self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha).replace("git gc --prune=now --aggressive", "git gc --prune=now")}

{self.clear_env}

CMD ["/bin/bash"]

"""


@Instance.register("OpenCTI-Platform", "opencti")
class OPENCTI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenctiImageDefault(self.pr, self._config)

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
