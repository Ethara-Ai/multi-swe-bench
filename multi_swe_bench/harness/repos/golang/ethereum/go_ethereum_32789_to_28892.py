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
        return "golang:1.24"

    def image_tag(self) -> str:
        return "base-go1-24"

    def workdir(self) -> str:
        return "base-go1-24"

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


@Instance.register("ethereum", "go_ethereum_32789_to_28892")
class GO_ETHEREUM_32789_TO_28892(Instance):
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
# class so Instance.create() resolves f"ethereum/{number_interval}" -> GO_ETHEREUM_32789_TO_28892.
_BUNDLE_NIS_GO_ETHEREUM_32789_TO_28892 = [
    "28892-31547-32591-33511-33539-33576-33589-33607-33628-33656-33741-33742-33743-33767-33774-33790-33800-33849-33864-33865-33866-33874-33875-33887-33890-33893-33896-33898-33899-33900-33901-33908-33920-33921-33922-33923-33928-33940",
    "30747-31613-32132-32270-32374-32569-32572-32596-32668-32687-32697-32720-32728-32731-32739-32746-32748-32749-32750-32751-32755-32756-32760-32766-32768-32771-32772-32776-32780-32783-32787-32794-32796-32800-32804-32807-32820-32823-32829-32831-32834-32845-32847-32849-32850-32869-32876-32881-32882-32887-32888-32889-32894-32896-32899-32900-32901-32906-32912",
    "31696-32632-32689-32816-32830-32844-32856-32907-32911-32914-32916-32917-32921-32929-32930-32934-32936-32937-32946-32947-32964-32965-32969-32971-32972-32980-32989-32993-32996-32997-33001-33002-33005-33012-33015-33018-33020-33024-33032-33041-33047-33050-33063-33064-33087",
    "32789-33150-33198-33523-33593-33640-33645-33648-33657-33708-33763-33773-33816-33823-33829-33832-33836-33869-33894-33919-33927-33931-33932-33934-33943-33945-33946-33947-33950-33951-33952-33955-33961-33963-33971-33975-33976-33978-33984-33989-33990-33997-34000-34003-34005-34006-34008-34011-34016-34021-34022-34025-34031-34032-34036-34039-34044-34048-34051-34052-34056-34059-34062-34067-34074-34079-34085-34092-34094-34100-34101-34106-34115-34616-34617-34618",
]
for _ni in _BUNDLE_NIS_GO_ETHEREUM_32789_TO_28892:
    Instance.register("ethereum", _ni)(GO_ETHEREUM_32789_TO_28892)
