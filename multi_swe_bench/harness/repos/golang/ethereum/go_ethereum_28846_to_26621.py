import re
from typing import Optional, Union

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
        return "golang:1.21"

    def image_tag(self) -> str:
        return "base-go1-21"

    def workdir(self) -> str:
        return "base-go1-21"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git build-essential

{self.global_env}

WORKDIR /home/

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}

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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self.config)

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
                """#!/bin/bash
# Skip entirely on non-native arch (QEMU crashes on go operations)
if [ "$(uname -m)" != "aarch64" ]; then exit 0; fi
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

go mod download
go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn --allow-binary-replacement /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn --allow-binary-replacement /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
go test -v -count=1 ./...

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
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("ethereum", "go_ethereum_28846_to_26621")
class GO_ETHEREUM_28846_TO_26621(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set(re.findall(r"--- PASS: ([\S]+)", log))
        failed_tests = set(re.findall(r"--- FAIL: ([\S]+)", log))
        skipped_tests = set(re.findall(r"--- SKIP: ([\S]+)", log))

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Registers each delivered bundle's dash-joined number_interval to this era's
# class so Instance.create() resolves f"ethereum/{number_interval}" -> GO_ETHEREUM_28846_TO_26621.
_BUNDLE_NIS_GO_ETHEREUM_28846_TO_26621 = [
    "26621-28581-28839-28845-28865-28876-28879-28881-28882-28889-28893-28900-28903-28908-28911-28914-28916-28917-28922-28923-28932-28933-28944-28948-28952-28954-28956-28958-28959-28961",
    "27766-28187-28256-28270-28328-28435-28443-28446-28459-28460-28461-28463-28467-28468-28473-28479-28488-28504-28506-28507-28511-28520-28521-28524-28526-28527-28529-28530-28532-28536-28538-28542-28544-28546-28549-28557-28560-28562-28564-28566-28569-28584-28585-28586-28588-28590-28595-28597-28600-28602-28605-28609-28612-28614-28618-28621-28622-28627-28628-28630-28634-28635-28637-28648-28649-28650-28652-28653-28654-28657-28662-28669-28675-28677-28682-28686-28691-28692-28696-28699",
    "27801-28070-28098-28183-28195-28201-28205-28207-28209-28212-28218-28221-28224-28226-28227-28228-28233-28238-28239-28243-28245-28249-28254-28255-28258-28261-28271-28280-28286-28287-28291-28300-28302-28304-28313-28322",
    "27834-28525-28702-28704-28705-28706-28707",
    "28084-28148-28198-28250-28252-28295-28327-28348-28349-28350-28352-28358-28359-28361-28362-28364-28368-28371-28373-28376-28379-28381-28382-28383-28386-28387-28389-28393-28397-28398-28400-28407-28412-28416-28417-28421-28426-28428-28444-28453-28456-28462-28470-28474-28475-28482-28483-28484-28491-28494-28501-28505",
    "28087-28095-28097-28107-28109-28127-28139-28140-28145-28146-28147-28150-28155-28159-28160-28163-28165-28171-28177-28178-28179-28180-28184-28190-28191-28192-28193-28196-28199-28208-28213",
    "28202-28719-28725-28728-28730-28733-28734-28735-28743-28747-28748-28755-28760-28762-28764-28769-28772-28774-28780",
    "28220-28306-28311-28323-28324-28329-28332-28333-28334-28335-28336-28337-28342-28356-28357",
    "28230-28246-28283-28598-28703-28744-28775-28778-28782-28784-28785-28786-28787-28794-28796-28798-28799-28800-28801-28804-28814-28815-28818-28825-28827-28830-28832-28834-28836-28837-28849-28856-28857-28858-28859-28860-28864-28868",
    "28340-28709-28710-28712-28718-28724-28727",
    "28606-28824-28910-28915-28929-28962-28966-28970-28974-28979-28981-28985-28989-28993-28994-28995-28996-29001-29003-29005-29008-29020-29022-29023-29024-29026-29031-29036-29037-29042",
    "28846-29049-29051-29068-29073-29074-29076-29077-29081-29083-29085-29090-29095",
]
for _ni in _BUNDLE_NIS_GO_ETHEREUM_28846_TO_26621:
    Instance.register("ethereum", _ni)(GO_ETHEREUM_28846_TO_26621)
