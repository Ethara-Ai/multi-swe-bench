import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class VerdaccioImageBase(Image):
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
        return "node:14"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}\n'
                f"\n"
                f"WORKDIR /home/{self.pr.repo}\n"
                f"\n"
                f'RUN git fetch origin "+refs/pull/{self.pr.number}/head:refs/heads/pr-{self.pr.number}" || true\n'
                f"RUN git reset --hard\n"
                f"RUN git checkout ${{BASE_COMMIT}}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN npm install -g pnpm@5.5.12

ENV NPM_CONFIG_REGISTRY=https://registry.npmjs.org/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class VerdaccioImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return VerdaccioImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

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
                "install-mockdate.sh",
                """#!/bin/bash
set -e

cd /tmp
npm pack mockdate@3.0.2 --registry=https://registry.npmjs.org/ > /dev/null
mkdir -p /home/{pr.repo}/node_modules/mockdate
tar -xzf /tmp/mockdate-3.0.2.tgz -C /home/{pr.repo}/node_modules/mockdate --strip-components=1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pnpm recursive install --registry=https://registry.npmjs.org/ || true

pnpm --filter verdaccio-htpasswd... run build || true

bash /home/install-mockdate.sh || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/{pr.repo}
pnpm recursive install --registry=https://registry.npmjs.org/ || true
bash /home/install-mockdate.sh || true

cd /home/{pr.repo}/packages/core/htpasswd
export PATH="/home/{pr.repo}/node_modules/.bin:$PATH"
jest --verbose --runInBand --ci

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
git checkout HEAD -- packages/core/htpasswd/tests/utils.test.ts
git checkout HEAD -- packages/core/htpasswd/tests/__snapshots__/utils.test.ts.snap
pnpm recursive install --registry=https://registry.npmjs.org/ || true
bash /home/install-mockdate.sh || true

cd /home/{pr.repo}/packages/core/htpasswd
export PATH="/home/{pr.repo}/node_modules/.bin:$PATH"
jest --verbose --runInBand --ci

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/{pr.repo}
git checkout -- pnpm-lock.yaml
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm recursive install --registry=https://registry.npmjs.org/ || true
bash /home/install-mockdate.sh || true

cd /home/{pr.repo}/packages/core/htpasswd
export PATH="/home/{pr.repo}/node_modules/.bin:$PATH"
jest --verbose --runInBand --ci

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("verdaccio", "verdaccio")
class Verdaccio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VerdaccioImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        re_suite = re.compile(r"^(PASS|FAIL)\s+(\S+)")

        re_case = re.compile(
            r"^\s*(?:(?P<pass>[✓✔])|(?P<fail>[✕✗×])|(?P<skip>[○◯])|(?P<todo>✎))\s+"
            r"(?:skipped\s+|todo\s+)?"
            r"(?P<name>.*?)"
            r"(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        )

        current_suite = ""
        cases_in_suite = 0
        pending_suite = None

        def flush_suite():
            if pending_suite and cases_in_suite == 0:
                status, name = pending_suite
                if status == "FAIL":
                    failed_tests.add(name)
                else:
                    passed_tests.add(name)

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line)

            m = re_suite.match(line)
            if m:
                flush_suite()
                current_suite = m.group(2)
                cases_in_suite = 0
                pending_suite = (m.group(1), current_suite)
                continue

            m = re_case.match(line)
            if not m:
                continue

            name = m.group("name").strip()
            if not name:
                continue
            cases_in_suite += 1
            full_name = f"{current_suite}::{name}" if current_suite else name

            if m.group("fail"):
                failed_tests.add(full_name)
            elif m.group("skip") or m.group("todo"):
                skipped_tests.add(full_name)
            else:
                passed_tests.add(full_name)

        flush_suite()

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
