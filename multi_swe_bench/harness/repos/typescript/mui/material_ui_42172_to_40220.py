import re
from dataclasses import asdict, dataclass
from json import JSONDecoder
from typing import Generator, Optional, Union

from dataclasses_json import dataclass_json

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        return "node:20"

    def image_tag(self) -> str:
        return "base42172to40220"

    def workdir(self) -> str:
        return "base42172to40220"

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

RUN apt update && apt install -y git ca-certificates
RUN npm install -g pnpm@8
RUN apt install -y jq

RUN git config --global --add safe.directory '*'

# Light hardening only: keep FULL history (gc off) so every PR's base.sha can be
# checked out; the PR layer does the strict per-sha strip.
WORKDIR /home/{self.pr.repo}
RUN git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

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
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

sed -i 's/packageManager": ".*"/packageManager": "pnpm@^8"/' package.json
jq '.packageManager = "pnpm@^8" | del(.engines)' package.json > temp.json && mv temp.json package.json
pnpm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
NODE_ENV=test pnpm exec mocha 'packages/**/*.test.{{js,ts,tsx}}' 'docs/**/*.test.{{js,ts,tsx}}' --reporter json

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch
NODE_ENV=test pnpm exec mocha 'packages/**/*.test.{{js,ts,tsx}}' 'docs/**/*.test.{{js,ts,tsx}}' --reporter json
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch /home/fix.patch
NODE_ENV=test pnpm exec mocha 'packages/**/*.test.{{js,ts,tsx}}' 'docs/**/*.test.{{js,ts,tsx}}' --reporter json
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

        # Strict anti-reward-hack hardening at the PR layer with this PR's LITERAL
        # base.sha (shared base keeps full history; each PR strips its own image).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}
{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("mui", "material-ui_42172_to_40220")
class MaterialUi42172to40220(Instance):
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
        return _parse_mocha_log(test_log)


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

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# --- Bundle-level number_interval routing keys (all -> MaterialUi42172to40220) ---
# Each bundle's dash-joined number_interval registered so Instance.create()
# resolves f"mui/{number_interval}" to this era class. Fixes routing: records with
# empty/era-name number_interval otherwise fall through to the mui/material-ui fallback.
_BUNDLE_NIS_MATERIALUI42172TO40220 = [
    "40150-40423-40754-40772-40866-40869-40947-40968-40971-40972-40981-40999-41003-41004",
    "40302-40461-40853-40882-40884-40913-40943-40949-40957-40970-40988-40992-41000-41001-41009-41032-41036-41065",
    "40324-40332-40453-40457-40459-40481-40503-40524-40541-40552-40567-40587-40617-40623-40632-40643-40645-40646-40647-40652-40655-40657-40661-40670-40673-40674-40679-40683-40689-40691-40697-40699-40701-40704-40711-40724-40732",
    "40330-40559-40944-41040-41201-41221-41223-41276-41316-41405-41451-41461-41472-41473-41481-41484-41487-41494-41498-41499-41500-41502-41508-41516-41517-41518-41519-41520-41521-41522-41523-41524-41525-41526-41527-41532-41535-41540-41542-41543-41544",
    "40967-41084-41105-41196-41241-41248-41249-41287-41300-41306-41312-41313-41320-41323-41324-41329-41355-41358-41367-41369-41370-41372-41379-41381-41383-41384-41385-41387-41389-41390-41392-41395-41404-41407-41408-41409-41411-41413-41419-41423-41424-41425-41427-41428-41429-41430-41431-41433-41434-41436-41437-41438-41439-41442-41445-41447-41450-41453-41462-41463-41465-41467-41468-41475-41476-41477",
    "41188-41474-41742-41786-41815-41820-41822-41842-41864-41905-41914-41924-41929-41996-42009-42014-42030-42034-42054-42057-42059-42074-42075-42078-42084",
    "41551-41557-41604-41605-41606-41641-41642-41677-41678-41679-41680-41681-41747-41752",
    "41803-43605-44415-44537-44571-44627-44643-44650-44651-44654-44660-44663-44672-44676-44681-44683-44687-44688-44689-44690-44691-44692-44693-44695-44696-44697-44704-44707-44709-44710",
    "41956-43243-43406-43407-43426-43427-43428-43429-43430-43431-43432-43448-43464-43465-43467-43469-43472-43474-43483-43488-43489-43490-43493-43494-43498-43500-43510-43515",
    "42176-42226-42233-42250-42255",
    "42241-42265-42267-42306-42312-42315-42318-42327-42330-42334-42357-42366-42373-42410-42419-42420-42421-42426-42441-42443",
]
for _ni in _BUNDLE_NIS_MATERIALUI42172TO40220:
    Instance.register("mui", _ni)(MaterialUi42172to40220)
