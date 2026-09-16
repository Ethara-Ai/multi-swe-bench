import re
import textwrap
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class HospitalRunFrontendImageBase(Image):

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
        return "node:16-bookworm"

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
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class HospitalRunFrontendImageDefault(Image):

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
        return HospitalRunFrontendImageBase(self.pr, self.config)

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
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

npm pkg set overrides.cheerio=1.0.0-rc.12 2>/dev/null || \\
  node -e "var p=require('./package.json');p.overrides=p.overrides||{{}};p.overrides.cheerio='1.0.0-rc.12';require('fs').writeFileSync('./package.json',JSON.stringify(p,null,2)+'\\n')"

npm install --legacy-peer-deps --ignore-scripts
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

npx react-scripts test --verbose --watchAll=false --maxWorkers=2 2>&1 || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch

npm install --legacy-peer-deps --ignore-scripts || true

npx react-scripts test --verbose --watchAll=false --maxWorkers=2 2>&1 || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

npm install --legacy-peer-deps --ignore-scripts || true

npx react-scripts test --verbose --watchAll=false --maxWorkers=2 2>&1 || true
""".format(repo=self.pr.repo),
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


@Instance.register("HospitalRun", "hospitalrun_frontend_2349_to_1736")
class HospitalRunFrontend2349To1736(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HospitalRunFrontendImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        suite_re = re.compile(r"^(PASS|FAIL):?\s+(\S+\.[jt]sx?)\b")
        case_re = re.compile(
            r"^\s+(?P<mark>[✓✔✕×✗○])\s+(?:skipped\s+)?(?P<name>.+?)"
            r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
        )

        current_file = ""
        for line in test_log.splitlines():
            sm = suite_re.match(line)
            if sm:
                current_file = sm.group(2)
                if sm.group(1) == "FAIL":
                    failed_tests.add(current_file)
                continue

            cm = case_re.match(line)
            if not cm:
                continue
            name = cm.group("name").strip()
            tid = f"{current_file} > {name}" if current_file else name
            mark = cm.group("mark")
            if mark in "✓✔":
                passed_tests.add(tid)
            elif mark in "✕×✗":
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

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
