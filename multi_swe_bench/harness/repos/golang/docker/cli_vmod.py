"""docker/cli harness for the vendor.mod era (Go modules + vendored deps).

Routes by number_interval == "cli_vmod" in the JSONL. Covers PRs whose
base.sha contains a vendor.mod file (no go.mod). Uses golang:1.26-bookworm
with GOTOOLCHAIN=auto so vendor.mod's pinned Go directive is honored.
The Makefile relies on gotestsum; we run `go test` directly instead, which
is what `parse_log` is written against.

Base = single shared reference-format image (# syntax opt-out, ARG REPO_URL,
ethara.ai LABEL, TZ=UTC, light hardening). PR layer appends the canonical
_HARDENING_BLOCK (literal base.sha) so the fix cannot be read from git
history (anti reward-hacking).
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



class CliVmodImageBase(Image):
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
        return "golang:1.26-bookworm"

    def image_tag(self) -> str:
        return "base-vmod-1581_to_6826"

    def workdir(self) -> str:
        return "base-vmod-1581_to_6826"

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

ENV DEBIAN_FRONTEND=noninteractive \\
    GOTOOLCHAIN=auto \\
    CGO_ENABLED=0 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class CliVmodImageDefault(Image):
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
        return CliVmodImageBase(self.pr, self.config)

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# vendor.mod era: there is no go.mod in the repo. Symlink vendor.mod
# (and vendor.sum) into the expected go.mod/go.sum locations so the
# standard go toolchain treats this as a module rooted here. Mirrors
# what scripts/with-go-mod.sh does upstream, but persistent.
if [ -f vendor.mod ] && [ ! -e go.mod ]; then
  ln -sf vendor.mod go.mod
  ln -sf vendor.sum go.sum
fi

export GOFLAGS="-mod=vendor"
go test -v -count=1 $(go list ./... | grep -vE '/vendor/|/e2e/') || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOFLAGS="-mod=vendor"
go test -v -count=1 $(go list ./... | grep -vE '/vendor/|/e2e/')

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.binpb' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.mp4' --exclude='*.webp' /home/test.patch
export GOFLAGS="-mod=vendor"
go test -v -count=1 $(go list ./... | grep -vE '/vendor/|/e2e/')

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.binpb' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.mp4' --exclude='*.webp' /home/test.patch /home/fix.patch
export GOFLAGS="-mod=vendor"
go test -v -count=1 $(go list ./... | grep -vE '/vendor/|/e2e/')

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
        # Per-PR FULL hardening (prepare.sh has checked out base.sha): strip refs +
        # gc-prune so future/fix commits are unreachable & deleted, then audit.
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


@Instance.register("docker", "cli_vmod")
class CLI_VMOD(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CliVmodImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(test_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(test_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name in failed_tests:
                        continue
                    skipped_tests.add(test_name)

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
_BUNDLE_NIS_CLI_VMOD = [
    '4141-4154-4157-4164-4172-4175-4176-4177-4178-4182-4195-4196-4202',
    '4988-5106-5276-5277-5278-5279-5292',
    '5083-5095-5102-5103-5104-5105-5108-5116-5118',
    '5107-5283-5288-5404',
    '5348-5349-5351-5353-5354-5358-5362-5365-5366-5368-5371-5372-5374',
    '5701-5706-5707-5712-5720-5721-5723-5726-5732-5735-5736',
    '6173-6176-6177-6178-6179-6180-6181-6182-6183-6185',
    '6425-6428-6469-6470-6471-6472-6473-6474-6476-6477-6478-6480-6481-6482-6483-6489-6491-6493-6495-6497-6501-6509-6510-6514-6518-6519-6522-6529-6531',
    '6544-6560-6607-6608-6609-6610-6613-6621',
    '6687-6688',
    '6768-6770-6771-6772-6773',
    '1581-4966-5282-5995-6019-6022-6024-6026-6028-6029-6030-6031-6032-6033-6034-6036-6037-6040-6041-6042-6045-6050-6051-6052-6053-6054-6058-6060-6061-6063-6066-6069-6070-6071-6072-6073-6074-6075-6078-6079-6080-6081-6083-6084-6085-6086-6087-6091-6092-6093-6094-6095-6096-6097-6098-6099-6100-6101-6108-6109-6110-6111-6112-6113-6114',
    '2783-2943-3728-4188-4290-4391-4535-6485-6737-6754-6840-6846-6913-6947-6950-6951-6952-6953-6954-6964-6965-6966-6967-6968-6969-6971-6972-6974-6975-6977-6980-6982-6984-6985-6986-6987-6988',
    '4035-4039-4047-4061-4063-4065-4076-4077-4083-4086-4088-4092-4106-4107-4124-4126',
    '4217-4229-4230-4231-4232-4233',
    '4270-4274-4278-4285-4311-4329-4364-4409-4431-4432-4433-4434-4446-4451-4472-4477-4509-4513-4518',
    '4320-4326-4328-4339-4351-4368-4369-4371-4373-4375-4381-4393-4395',
    '4407-4423-4424-4425-4426-4428-4430-4438-4443-4445-4450',
    '4460-4471-4476-4491-4500-4508-4512-4517-4520-4528-4537-4538-4542-4544',
    '4639-4680-4682-4695-4706-4710-4750-4768-4824-4826-4827-4829',
    '4823-4825',
    '4837-4841-4856-4857',
    '4858-4883-4885-4908-4909-4910-4911-4912-4913-4914-4915-4919-4920-4921-4924',
    '4903-5829-5876-5926-5936-5939-5941-5942-5944-5945-5957-5960',
    '5062-5065-5066',
    '5190-5192-5197-5198-5199-5201-5202',
    '5208-5219-5227-5237-5248-5250-5260-5261-5263-5265-5266-5269-5270-5271-5274',
    '5267-5293-5296-5302-5325-5326-5333-5334-5335-5337-5339-5343',
    '5336-5341-5397-5406-5511-5514-5566',
    '5380-5385-5391-5392-5394-5395-5399-5400-5402-5408-5409-5411-5414',
    '5419-5423-5426-5429-5430-5431-5432-5439-5440-5442-5443-5447-5448-5449-5452',
    '5470-5472-5497-5498-5499-5503-5509-5512-5518-5519-5530-5531-5540-5543-5545-5548-5552-5563-5564-5569-5588-5602-5603-5610-5611-5612-5613-5614-5616-5618-5619-5622-5623-5631-5635-5640-5649-5652-5654-5658-5661-5669',
    '5742-5848-5850-5854-5859-5864-5865-5867-5870',
    '5784-5842-5851-5863-5869-5872-5875-5877-5878-5879-5880-5881-5882-5885-5887-5888-5889-5890-5892-5893-5894-5901-5902-5903-5904-5906-5907-5908-5909-5910-5911-5912-5913-5915-5916-5917-5919-5920-5929-5932-5938',
    '5858-5868-5914-5918-5921-5924-5934-5946-5947-5950-5952-5953-5958-5959-5961-5966-5968-5969-5970-5973-5974-5975-5976-5977-5978-5979-5980-5981-5982-5983-5984-5985-5986-5988-5989-5990-5991-5992-5994-5996-5997-5998-5999-6000-6001-6002-6006-6009-6010-6011-6012-6013-6015-6016-6017-6018',
    '6008-6117-6119-6120-6123-6124-6127-6129-6130-6131-6132-6135-6137-6138-6140-6141-6144-6146-6148',
    '6089-6295-6367-6602-6710-6711-6713-6714-6715-6716-6717-6718-6720-6721-6722-6723-6724-6725-6735-6736-6738-6739-6740-6741-6742-6744-6745-6746-6747-6750-6753-6758-6759-6760-6761-6762-6763-6764',
    '6147-6149-6154-6157-6158-6159-6160-6162-6163-6165-6167-6170',
    '6218-6221-6256-6257-6258-6259-6262-6264-6265-6266-6269-6272-6275-6276-6277-6278-6280-6289-6291-6292-6294-6298-6307-6308-6309-6310-6311-6312-6313-6315-6331-6332-6333-6340-6341-6343-6348-6354-6357-6358-6369-6379-6380-6381-6384-6386-6390-6391-6392-6394-6399-6403-6405-6409-6411-6413-6416-6420-6422-6423-6424',
    '6579-6671-6680-6681-6682',
    '6648-6654-6656-6657-6658-6659',
    '6667-6668',
    '6685-6690-6692-6697-6700-6701-6702-6704-6705',
    '6775-6776-6780-6781-6784-6785-6787-6788-6789-6790-6791-6792-6793-6794-6796-6797-6798-6801-6803-6807-6809-6813-6815-6822-6823-6824-6827-6828-6829-6832-6833-6836-6842-6843-6844',
    '6826-6915-6920-6923-6927-6929-6930-6931-6935-6936-6941-6942-6943-6944-6945-6946-6948-6949',
]
for _ni in _BUNDLE_NIS_CLI_VMOD:
    Instance.register("docker", _ni)(CLI_VMOD)
