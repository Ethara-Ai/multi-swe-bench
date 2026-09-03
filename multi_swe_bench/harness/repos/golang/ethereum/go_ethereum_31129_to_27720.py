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
        return "golang:1.22"

    def image_tag(self) -> str:
        return "base-go1-22"

    def workdir(self) -> str:
        return "base-go1-22"

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


@Instance.register("ethereum", "go_ethereum_31129_to_27720")
class GO_ETHEREUM_31129_TO_27720(Instance):
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
# class so Instance.create() resolves f"ethereum/{number_interval}" -> GO_ETHEREUM_31129_TO_27720.
_BUNDLE_NIS_GO_ETHEREUM_31129_TO_27720 = [
    "27720-28880-29179-29431-29465-29761-30040-30125-30129-30137-30175-30231-30242-30247-30264-30269-30283-30286-30288-30289-30290-30291-30292-30297-30298-30299-30302-30305-30306-30315-30318-30319-30320-30321-30322-30323-30325-30326-30327-30331-30332-30335-30336-30342-30343-30344-30346-30349-30351-30353-30354-30355-30357-30361-30364-30369-30372-30381-30385-30388-30391-30393-30394-30398-30401-30404-30409-30414-30415-30421-30430-30431-30433-30437-30443-30444-30449-30455-30456",
    "27838-29338-29347-29485-29519-29530-29572-29639-29655-29707-29711-29714-29730-29731-29738-29746-29748-29749-29762-29763-29767-29768-29769-29776-29777-29784-29795-29799-29801-29809-29811-29821-29823-29824-29827-29828-29831-29832-29836-29839-29841-29842-29843-29844-29852-29853-29864-29867-29872-29873-29874-29875-29876-29879-29883-29887-29888-29889-29890-29892-29893-29895-29899-29903-29919-29924",
    "29135-29281-29355-29362-29482-29514-29520-29579-29590-29598-29614-29616-29620-29623-29626-29627-29636-29637-29641-29643-29644-29647-29648-29649-29651-29661-29665-29672-29681-29683-29684-29686-29690-29692-29697-29699-29701-29703-29708-29720-29723-29725-29733-29734",
    "29719-29753-29807-29861-29869-29894-29901-29907-29911-29921-29926-29930-29941-29943-29946-29948-29952-29954-29957-29960-29963-29964-29970-29972-29974-29985-29986-29988-29989-29995-30001-30010-30011-30014-30019-30020-30023-30024-30028-30029-30037-30038-30039-30044-30047-30048-30050-30052-30058-30062-30065-30071",
    "29721-29760-30080-30091-30094-30105-30123-30127-30130-30135-30138-30148-30150-30157-30158-30167-30171-30172-30181-30182-30184-30185-30189-30191-30193-30195-30200-30203-30208-30211-30219-30228-30232-30234-30239-30241-30248-30249-30250-30252-30253-30257-30258-30259-30261-30263-30267-30268-30272-30273-30276-30277-30280-30281",
    "29891-29913-29932-29936-29938-29942-29944",
    "30069-30367-30454-30457-30458-30459-30460-30466-30474-30479-30488-30490-30491-30493-30495-30496-30499-30504-30506-30518-30521-30522",
    "30386-30465-30473-30512-30527-30530-30535",
    "31073-31119-31137-31139-31153-31155-31157-31158-31165-31171-31173",
    "31129-31185-31191-31192",
]
for _ni in _BUNDLE_NIS_GO_ETHEREUM_31129_TO_27720:
    Instance.register("ethereum", _ni)(GO_ETHEREUM_31129_TO_27720)
