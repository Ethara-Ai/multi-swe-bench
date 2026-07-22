from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class StarshipLatestImageBase(Image):
    """Shared TOOLCHAIN-ONLY base for the current (rust:latest) era.

    Contains NO ``git clone`` on purpose: DockerfileEnhancer._inject_final_sanitize()
    only injects the history-stripping hardening when the Dockerfile mentions
    git clone/fetch/remote add. With no clone this image is never pinned to a
    BASE_COMMIT and never has its origin removed, so it is safely reusable by
    every PR in the era. The per-PR image does clone + checkout + hardening itself.
    """

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
        return "rust:latest"

    def image_tag(self) -> str:
        return "base-latest"

    def workdir(self) -> str:
        return "base-latest"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """
FROM rust:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git cmake pkg-config libssl-dev

WORKDIR /home/
"""


class StarshipImageDefault(Image):
    """Per-PR image for the current (rust:latest) era of starship.

    Layers on the clone-less StarshipLatestImageBase. Because dependency() is an
    Image, the enhancer emits this Dockerfile verbatim and no REPO_URL/BASE_COMMIT
    build-args are passed, so the clone URL and commit are baked in literally and
    the hardening block is embedded by hand with this PR's sha.
    """

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
        return StarshipLatestImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

cargo test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
cargo test

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_ref = f"{base.image_name()}:{base.image_tag()}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # dependency() is an Image, so DockerfileEnhancer returns this Dockerfile
        # VERBATIM and build_dataset.py passes no REPO_URL/BASE_COMMIT build-args.
        # Clone URL and commit are therefore baked in literally, and the hardening
        # block is embedded by hand with ${BASE_COMMIT} -> this PR's actual sha.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {base_ref}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
{hardening}
"""


@Instance.register("starship", "starship")
class Starship(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StarshipImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"test (\S+) ... ok")]
        re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) ... ignored")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- LHT bundle routing (rust:latest) ---------------------------------------
# Each dataset record's number_interval is the dash-joined prs_in_bundle
# (derived from prs_in_bundle by the from_json shim in __init__.py).
# Instance.create looks up f"starship/{number_interval}", so every bundle
# in this toolchain era is registered here against Starship. (18 bundles)
_Starship_INTERVALS = [
    "1344-4109-4323-4355-4395-4397-4432-4441-4455-4460-4461-4477-4485-4487-4489-4490-4493-4495-4499-4500-4501-4503-4508-4512-4513-4514-4515-4517-4518-4519-4520-4521-4522-4523-4527-4529-4530-4535-4541-4542-4543-4544-4547-4552-4553-4556-4557-4560-4562-4563-4569-4571-4572-4573-4578-4579-4582-4585-4588-4593-4595-4599-4601-4604-4607-4609-4612-4615-4616-4618-4619-4622-4623-4625-4629-4631-4632-4634-4636-4637-4641-4643-4644-4648-4650-4651-4659-4660-4662-4663-4664-4666-4667-4668-4670-4673-4675-4681-4683-4686-4687-4689-4702-4709-4710-4711",
    "1526-3289-3304-3344-3426-3427-3428-3439-3440-3443-3448-3449-3450-3451-3452-3453-3456-3460-3462-3463-3474-3479-3480-3481-3484-3494-3495-3496-3502-3506-3507-3508-3509-3513-3514-3515-3516-3519-3521-3523-3524-3529-3532-3533-3534-3537-3542-3543-3547-3548-3549-3552-3559-3560-3572-3573-3574-3575-3576-3577",
    "2791-3536-3631-3639-3697-3711-3720-3737-3769-3781-3786-3798-3799-3800-3804-3810-3815-3816-3820-3824-3829-3832-3833-3842-3844-3847-3848-3852-3855-3857-3859-3860-3861-3866-3871-3873",
    "3157-3220-3277-3287-3309-3310-3322-3345-3347-3348-3349-3350-3351-3352-3354-3355-3359-3360-3361-3362-3364-3365-3366-3368-3370-3373-3374-3380-3381-3382-3384-3385-3388-3392-3393-3394-3398-3403-3410-3411-3412-3415-3417-3420-3421-3422-3425-3431",
    "3399-3424-3432-3435",
    "3414-3591-3766-3806-3812-3908-3919-3927-3930-3931-3932-3938-3946-3950-3953-3954-3955-3956-3957-3958-3960-3962-3965-3966-3968-3969-3973-3983-3985-3989-3992-3993-3994-4002-4007-4009-4010-4011-4012-4013-4014-4015-4017-4018",
    "3467-4348-4423-4497-4713-4715-4717-4719-4721-4722-4724-4727-4729-4730-4731-4732-4733-4734-4736-4738-4739-4740-4741-4743-4747-4748-4750-4751-4752-4753-4758-4760-4763-4765-4766-4768-4771-4772-4773-4774-4775-4776-4777-4779-4787-4788-4790-4791-4794-4797-4798-4799-4800-4802-4805-4806-4807-4811-4817-4820-4823-4826-4832-4833-4838-4839-4842-4843-4844-4847-4851-4852-4853-4855-4865-4872-4875-4877-4878-4883-4884-4887-4888-4889-4890-4891-4892-4893-4905-4909-4913-4921-4925",
    "3690-3789-3872-3874-3878-3879-3885-3886-3887-3889-3891-3897-3898-3901-3904-3905-3906-3915-3920-3925",
    "3981-4550-4874-5157-5196-5261-5321-5322-5325-5348-5349-5352-5356-5358-5360-5362-5364-5365-5373-5389-5392-5398-5399-5405-5406-5412-5416-5417-5420-5424-5430-5431-5434-5436-5438-5442-5443-5444-5445-5447-5452-5453-5454-5455-5458-5473-5475-5478-5480-5486-5490-5491-5492-5493-5494-5495-5496-5497-5498-5499-5501-5502-5505-5507-5513-5516-5517-5518-5519-5521-5527-5532-5534-5550-5551-5552-5553-5554-5556-5558-5560-5578-5581-5588-5589-5591-5594-5606-5615-5616-5619-5620-5634-5640",
    "4486-5825-6031-6074-6080-6084-6097-6107-6108-6126-6140-6142-6143-6145-6146-6147-6156-6157-6159-6160-6161-6162-6167-6168-6173-6176-6181-6183-6184-6185-6186-6187-6195-6200-6201-6207-6208-6209-6215-6221-6222-6226-6228-6234-6235-6239-6240-6241-6242-6247-6253-6257-6261-6264-6265-6266-6267-6271-6273-6275-6288-6293-6300-6303-6310-6311-6312-6314-6315-6317-6319-6324",
    "4723-4822-4856-4857-4859-4910-4926-4940-4941-4946-4948-4950-4956-4960-4963-4966-4970-4975-4978-4982-4983-4984-4985-4991-4992-4993-4994-4995-4999-5002-5004-5009-5011-5017-5018-5021-5023-5024-5034-5035-5038-5040-5043-5046-5054-5056-5057-5062-5082",
    "4829-4949-5001-5047-5052-5079-5081-5103-5106-5107-5108-5109-5110-5115-5119-5120-5125-5128-5131-5141-5146-5147-5153-5162-5166-5170-5172-5175-5176-5177-5183-5199-5209-5210-5216-5218-5219-5221-5222-5223",
    "4902-4972-5033-5036-5379-5429-5474-5574-5657-5661-5677-5680-5682-5683-5684-5685-5687-5690-5692-5694-5695-5699-5704-5710-5711-5714-5715-5717-5718-5719-5720-5721-5728-5734-5735-5739-5742-5746-5756-5761-5763-5764-5768-5770-5775-5782-5785-5789-5792-5797-5803-5807-5815-5818-5820-5824-5826-5829-5830-5831-5833-5834-5835-5842-5849-5850-5851",
    "5007-5078-5093-5095-5098",
    "5655-6388-7030-7100-7108-7112-7125-7139-7145-7189-7193-7195-7196-7197-7199-7201-7202-7211-7212-7215-7216-7223-7229-7234-7236-7238-7243-7244-7250-7251-7255-7256-7259-7262-7263-7265-7267-7268-7269-7271-7272-7277-7278-7280-7281-7291-7295-7298-7304-7311-7317-7321-7323-7324-7327-7329-7333-7336-7337-7338-7339-7342-7344-7348-7349-7350-7352-7356-7358-7362-7371-7374-7376-7378-7384-7385-7387-7391-7393-7396",
    "5747-5796-6335-6346-6366-6387-6397-6449-6458-6476-6488-6495-6497-6498-6499-6502-6509-6510-6523-6527-6535-6537-6544-6556-6557-6559-6570-6574-6575-6580-6583-6585-6590-6593-6596-6603-6605-6606-6608-6609-6615-6616-6622-6623-6626-6636-6637-6640-6651-6652-6653-6657-6658-6661-6670-6671-6673-6677-6678-6683-6693-6699-6700",
    "6338-6492-6614-6625-6649-6656-6676-6684-6685-6689-6694-6705-6708-6709-6710-6715-6727-6729-6730-6732-6733-6734-6735-6737-6742-6745-6748-6749-6751-6753-6755-6758-6765-6766-6768-6769-6770-6771-6772-6773-6774-6780-6784-6785-6789-6790-6791-6792-6793-6794-6796-6797-6800-6802-6811-6814-6817-6820-6821-6822-6825-6828-6829-6830-6835-6841-6847-6852-6854-6861-6866-6871-6880-6882-6883-6885-6887-6890-6891-6897-6899-6900-6903-6905-6906-6908-6910-6916-6917-6919-6922-6924-6927-6928-6929-6930-6934-6936-6937-6938-6939-6940-6943-6947-6948-6949-6950-6952-6956-6958-6960-6961-6971-6973-6974-6976-6977-6978-6979-6980-6982-6983-6986-6987-6988-6992-6993-6994-6997-6998-7001-7004-7005-7006-7008-7023-7028-7041-7049-7051-7052-7053-7054-7055-7056-7057-7058-7061-7062-7066-7068",
    "7015-7069-7075-7080-7123-7124-7127-7131-7134-7137-7138-7147-7154-7155-7159-7172-7173-7174-7176-7180-7184-7186-7187-7191",
]
for _iv in _Starship_INTERVALS:
    Instance.register("starship", _iv)(Starship)
