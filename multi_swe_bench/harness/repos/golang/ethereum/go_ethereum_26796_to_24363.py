import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
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
        return "golang:1.19"

    def image_tag(self) -> str:
        return "base-go1-19"

    def workdir(self) -> str:
        return "base-go1-19"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("ethereum", "go_ethereum_26796_to_24363")
class GO_ETHEREUM_26796_TO_24363(Instance):
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
# class so Instance.create() resolves f"ethereum/{number_interval}" -> GO_ETHEREUM_26796_TO_24363.
_BUNDLE_NIS_GO_ETHEREUM_26796_TO_24363 = [
    "24363-26414-26813-26894-26973-26999-27008-27049-27083-27084-27112-27114-27136-27137-27147-27155-27159-27160-27161-27162-27168-27169-27171-27176-27178-27183-27184-27186-27193-27194-27196-27202-27206-27213-27214-27217-27219-27221-27222-27223-27226-27228-27229-27230-27233-27236-27238-27239-27240-27241-27246-27252-27255-27256-27257-27264-27268-27272-27273-27277-27279-27281-27287-27288-27292-27294-27295-27296-27304-27318-27320-27329-27330-27333-27334-27335-27336-27350",
    "25942-25963-26181-26359-26681-26940-27000-27072-27135-27189-27209-27218-27249-27263-27270-27285-27299-27303-27309-27310-27323-27325-27327-27328-27331-27332-27339-27347-27349-27356-27369-27376-27382-27383-27387-27392-27393-27396-27397-27400-27404-27405-27406-27428-27429-27430-27432-27438-27447-27449-27450-27452-27457-27463-27464-27470-27471-27472-27475-27476-27477-27478-27479-27481-27484-27485-27486-27487-27488-27489-27490-27491-27492-27493-27494-27496-27500-27501-27503-27505-27506-27510-27512-27518-27521-27522-27523-27525-27527-27530-27532-27538-27543-27544-27549-27550-27559-27561-27613-27615-27618-27620-27621-27635-27643-27660-27662-27663-27664-27665-27679-27687-27691-27695-27698-27701-27703-27704-27705-27706-27708-27712-27713-27716-27717-27721-27722-27723-27724-27729-27736-27737-27741-27742-27743-27744-27752-27753-27754-27755-27756-27762-27763-27764-27767-27787-27789-27790-27791-27793-27796-27797-27803-27814-27815-27816-27821-27822-27825-27828-27835-27842-27844-27845-27853-27857-27858-27861-27873-27874-27882-27887",
    "25977-26544-26648-26719-26770-26828-26834-26838-26841-26844-26846-26850",
    "26377-26633-26667-26685-26697-26698-26713-26721-26753-26756-26771-26773-26776-26777-26778-26779-26790-26793-26795-26799-26801-26802-26803-26804-26817-26822-26824",
    "26665-26676-26696-26718-26722-26723-26729-26731-26732-26747-26748-26757-26758",
    "26699-26763-26840-26852-26856-26862-26863-26865-26870-26871-26873-26882-26883-26895-26898-26908-26911-26912-26913-26914-26918-26930",
    "26796-26843-26848-26880-26896-26907-26917-26920-26922-26932-26934-26935-26936-26938-26946-26950-26951-26955-26960-26963-26965-26968-26969-26970-26975-26976-26992-26993-26995-26998-27001-27006-27007-27011-27012-27013-27014-27023-27025-27027-27029-27030-27031-27032-27038-27039-27041-27045-27046-27048-27051-27063-27068-27070-27071-27078-27087-27089-27099-27113-27116-27121",
]
for _ni in _BUNDLE_NIS_GO_ETHEREUM_26796_TO_24363:
    Instance.register("ethereum", _ni)(GO_ETHEREUM_26796_TO_24363)
