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
        return "golang:1.23"

    def image_tag(self) -> str:
        return "base-go1-23"

    def workdir(self) -> str:
        return "base-go1-23"

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


@Instance.register("ethereum", "go_ethereum_31966_to_17439")
class GO_ETHEREUM_31966_TO_17439(Instance):
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
# class so Instance.create() resolves f"ethereum/{number_interval}" -> GO_ETHEREUM_31966_TO_17439.
_BUNDLE_NIS_GO_ETHEREUM_31966_TO_17439 = [
    "17439-30265-31075-31267-31306-31331-31361-31373-31414-31433-31468-31469-31486-31501-31511-31519-31522-31526-31530-31534-31538-31539-31540-31543-31544-31548-31551-31552-31566-31567-31577-31595",
    "27508-31065-31079-31080-31081-31117-31216-31242-31243-31258-31295-31304-31307-31314-31316-31332-31334-31341-31342-31348-31352-31353-31355-31356-31357-31360-31362-31365-31368-31369-31374-31379-31381-31383-31384-31387-31393-31400-31403-31406-31411-31419-31424-31429-31430-31434-31439-31445-31450-31455-31456-31463-31470-31473-31479",
    "29158-30464-30661-30971-31006-31014-31156-31161-31182-31288-31427-31541-31557-31598-31604-31606-31624-31630-31652-31658-31703-31725-31753-31765-31768-31769-31771-31774-31775-31776-31779-31781-31782-31783-31784-31785-31786-31790-31791-31800-31804-31806-31809-31818-31821-31823-31836-31837-31838-31839-31843-31845-31846-31852-31854-31855-31856-31859-31860-31861-31867-31870-31874-31875-31878-31879-31880-31885-31886-31887-31890-31891-31896-31898-31909-31911-31915-31918-31919-31920-31921-31922-31923-31924-31925-31927-31928-31940-31941-31944-31946-31947-31949-31951-31952-31953-31955-31961-31962-31970-31978-31982-31988-31992-31993-31998-32000-32004-32012-32015-32017-32024-32027-32029-32034-32047-32051-32053-32055-32057-32062-32065-32066-32067-32070-32071-32075-32080-32081-32086-32087-32090-32091-32097-32099",
    "29450-30984-31033-31049-31061-31084-31122-31159-31164-31170-31172-31174-31176-31179-31206-31209-31211-31217-31218-31224-31225-31233-31240-31241-31247-31251",
    "30017-30558-31189-31202-31219-31234-31246-31249-31265-31266-31270-31282-31283-31290",
    "30932-31175-31228-31293-31301-31336-31394-31475-31492-31493-31495-31496-31497-31500-31525-31531",
    "31148-31585-31824-31877-31902-31913-31948-31965-31989-31990-31991-32046-32060-32068-32092-32121-32128-32135-32136-32141-32142-32143-32145-32149-32150-32166-32172-32177-32179-32183-32188-32193-32194-32197-32198-32199-32206-32209-32210-32212-32213-32214-32215-32217-32219-32220-32222-32225-32226-32230-32231-32237-32241-32246-32248-32250-32251-32253-32255-32258-32260-32269-32274-32280-32286-32288-32291-32293-32298-32301-32303-32304-32324-32336-32343",
    "31198-31441-31705-31706-31708",
    "31340-31407-31476-31535-31590-31610-31618-31621-31629-31636-31637-31638-31639-31641-31642-31646-31656-31657-31663-31668-31671-31674-31680",
    "31378-31480-31504-31506-31574-31667-31710-31711-31715-31716-31733-31734-31735-31739-31742-31743-31746-31750-31752-31758-31760-31761-31763",
    "31634-31666-31714-31795-31882-31912-32127-32134-32186-32190-32239-32279-32300-32306-32309-32315-32316-32317-32319-32320-32321-32322-32323-32344-32345-32347-32348-32349-32352-32354-32356-32357-32360-32361-32363-32365-32366-32369-32378-32380-32382-32384-32388-32389-32391-32393-32397-32398-32401-32402-32404-32405-32411-32412-32413-32414-32417-32421-32424-32425-32427-32428-32430-32431-32432-32433-32434-32435-32443-32444-32454-32455-32461-32466-32472-32477-32480-32481-32488-32491-32494-32495-32497-32498-32499-32501-32502-32503-32506-32507-32509-32510-32513-32516-32521",
    "31659-31692-31871-31876-31934-32021-32073-32089-32093-32104-32107-32110-32118-32123-32125-32129-32130",
    "31966-32216-32327-32362-32418-32447-32517-32518-32520-32523-32524-32525-32526-32527-32528-32529-32531-32533-32534-32535-32536-32538-32542-32543-32544-32551-32553-32554-32555-32557-32559-32563-32564-32568-32576-32577-32578-32579-32584-32587-32589-32590-32592-32593-32597-32598-32599-32602-32604-32609-32610-32613-32614-32615-32616-32618-32619-32622-32623-32624-32627-32633-32636-32638-32639-32640-32641-32643-32645-32648-32649-32650-32651-32656-32657-32658-32659-32660-32662-32663-32664-32676-32677-32678-32681-32688-32694-32698-32699-32714-32716-32718-32719-32726-32732-32734-32735-32736-32737-32738-32740-32742",
]
for _ni in _BUNDLE_NIS_GO_ETHEREUM_31966_TO_17439:
    Instance.register("ethereum", _ni)(GO_ETHEREUM_31966_TO_17439)
