import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_BASE = "node:16-bullseye"

_TARGETS = (
    "TARGETS=$(grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null "
    "| sed 's#^+++ b/##' "
    "| grep -E '\\.(test|spec)\\.[jt]sx?$' "
    "| sed 's#^template/##' | sort -u | tr '\\n' ' ')\n"
    'echo "SCOPED CRA TARGETS: $TARGETS"\n'
)

_RESET = "git reset --hard\ngit clean -fdq -- template/src\n"

_SETUP = (
    "node /home/gen_package.js /home/{repo}\n"
    "[ -L /home/{repo}/test ] || ln -s template /home/{repo}/test\n"
    "cd /home/{repo}/template\n"
    "export CI=true SKIP_PREFLIGHT_CHECK=true\n"
    "yarn install --ignore-engines --network-timeout 600000 || true\n"
)

_TEST = (
    "rm -f /home/jest_out.json\n"
    "CI=true SKIP_PREFLIGHT_CHECK=true yarn react-scripts test $TARGETS "
    "--watchAll=false --json --outputFile=/home/jest_out.json >/dev/null 2>&1 || true\n"
    "cat /home/jest_out.json 2>/dev/null || true\n"
)

_GEN_PACKAGE_JS = """\
const fs = require('fs');
const repo = process.argv[2];
const t = require(repo + '/template.json').package || {};
const jest = Object.assign({}, t.jest || {});
delete jest.testMatch;
const dev = {};
for (const [k, v] of Object.entries(t.devDependencies || {})) {
  if (!k.startsWith('@storybook/')) dev[k] = v;
}
const pkg = {
  name: 'kingdom-app', version: '0.1.0', private: true,
  dependencies: Object.assign(
    { react: '^17.0.2', 'react-dom': '^17.0.2', 'react-scripts': '4.0.3' },
    t.dependencies || {}
  ),
  devDependencies: dev,
  scripts: { test: 'react-scripts test' },
  browserslist: {
    production: ['>0.2%', 'not dead', 'not op_mini all'],
    development: ['last 1 chrome version'],
  },
  eslintConfig: { extends: ['react-app'] },
  jest: jest,
};
fs.writeFileSync(repo + '/template/package.json', JSON.stringify(pkg, null, 2));
fs.writeFileSync(repo + '/template/.env', 'SKIP_PREFLIGHT_CHECK=true\\n');
"""


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
        return _NODE_BASE

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
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}
WORKDIR /home/{repo}

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
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "gen_package.js", _GEN_PACKAGE_JS),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo}\n"
                f"git cat-file -e {self.pr.base.sha}^{{commit}} 2>/dev/null \\\n"
                f"    || git fetch --no-tags --depth=2147483647 origin {self.pr.base.sha} \\\n"
                f'    || git fetch --no-tags origin "+refs/pull/{self.pr.number}/head:refs/remotes/origin/pr-{self.pr.number}"\n'
                "git reset --hard\n"
                "git clean -fdq\n"
                f"git checkout --detach {self.pr.base.sha}\n"
                f'test "$(git rev-parse HEAD)" = "$(git rev-parse {self.pr.base.sha})"\n'
                "git clean -fdq\n"
                + _SETUP.format(repo=repo),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo}\n"
                + _RESET
                + _SETUP.format(repo=repo)
                + _TARGETS
                + _TEST,
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo}\n"
                + _RESET
                + "git apply --whitespace=nowarn /home/test.patch\n"
                + _SETUP.format(repo=repo)
                + _TARGETS
                + _TEST,
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo}\n"
                + _RESET
                + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                + _SETUP.format(repo=repo)
                + _TARGETS
                + _TEST,
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency().image_full_name()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("t10d", "cra-template-kingdom")
class CraTemplateKingdom(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_cra_log(test_log)


def _parse_cra_log(test_log: str) -> TestResult:
    import json

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    start = test_log.find("{")
    while start != -1:
        try:
            data = json.loads(test_log[start:])
        except Exception:
            start = test_log.find("{", start + 1)
            continue
        for tr in data.get("testResults", []) or []:
            for a in tr.get("assertionResults", []) or []:
                name = a.get("fullName") or a.get("title")
                if not name:
                    continue
                st = a.get("status")
                if st == "passed":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif st == "failed":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                elif st in ("skipped", "pending", "todo", "disabled"):
                    skipped_tests.add(name)
        break

    if not (passed_tests or failed_tests):
        pass_re = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        fail_re = re.compile(r"^\s*[×✗✕]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        for line in test_log.splitlines():
            m = pass_re.match(line)
            if m and m.group(1) not in failed_tests:
                passed_tests.add(m.group(1))
            m = fail_re.match(line)
            if m:
                failed_tests.add(m.group(1))
                passed_tests.discard(m.group(1))

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )
