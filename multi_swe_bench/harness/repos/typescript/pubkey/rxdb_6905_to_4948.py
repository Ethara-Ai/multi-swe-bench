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
        return "node:20"

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the shared era base to a
        # single PR's base.sha and breaking every other PR in the era with
        # "reference is not a tree". The base keeps full history; the strict
        # anti-reward-hack hardening runs per-PR (see ImageDefault below).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && \\
    rm -rf /var/lib/apt/lists/*
RUN npm install -g cross-env

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self._config)

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
npm install --legacy-peer-deps --ignore-scripts || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
npm run transpile || true
cross-env DEFAULT_STORAGE=lokijs npx mocha --expose-gc --config ./config/.mocharc.cjs ./test_tmp/unit.test.js 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch
npm install --legacy-peer-deps --ignore-scripts || true
npm run transpile || true
cross-env DEFAULT_STORAGE=lokijs npx mocha --expose-gc --config ./config/.mocharc.cjs ./test_tmp/unit.test.js 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --legacy-peer-deps --ignore-scripts || true
npm run transpile || true
cross-env DEFAULT_STORAGE=lokijs npx mocha --expose-gc --config ./config/.mocharc.cjs ./test_tmp/unit.test.js 2>&1 || true

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

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable from git.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("pubkey", "rxdb_6905_to_4948")
class Rxdb6905To4948(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        re_pass = re.compile(r"^\s*[✓✔]\s+(.*)")
        re_fail = re.compile(r"^\s+(\d+)\)\s+(.*)")
        re_skip = re.compile(r"^\s*[-–]\s+(.*)")

        for line in test_log.splitlines():
            clean = ansi_re.sub("", line).rstrip()
            if not clean:
                continue

            fail_match = re_fail.match(clean)
            if fail_match:
                failed_tests.add(fail_match.group(2).strip())
                continue

            pass_match = re_pass.match(clean)
            if pass_match:
                passed_tests.add(pass_match.group(1).strip())
                continue

            skip_match = re_skip.match(clean)
            if skip_match:
                skipped_tests.add(skip_match.group(1).strip())

        # A test that appears in both passed and failed should count as failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval bundle routing (prs_in_bundle dash-joined)  -- PIPELINE 11b
# ---------------------------------------------------------------------------
# The raw dataset leaves `number_interval` empty. At build/delivery the record's
# `number_interval` is set to "-".join(prs_in_bundle); the loader then resolves
# `pubkey/<dash-joined-bundle>`. Register Rxdb6905To4948 (this era, lead PR in range)
# under every bundle key so those records route to the correct era class. The
# original `pubkey/rxdb_6905_to_4948` era key registration above is kept (harmless).
_BUNDLE_NIS_RXDB_ERA2 = [
    "5429-5448-5449-5450-5453-5460-5461-5462-5463",
    "5454-5455-5467-5468-5469-5471-5472-5473-5474-5475-5476-5477-5478-5479-5480-5482-5483-5484-5485-5486-5487-5488-5489-5490-5491-5493-5494-5495-5496-5497-5498-5499-5500-5501-5502-5503-5504",
    "5535-5536-5537-5539-5540",
    "5538-5541-5542",
    "5543-5544-5545-5546-5547-5548-5549-5550-5551-5555-5557",
    "5558-5560-5561-5562-5564-5566-5567-5568-5569-5570-5573-5575-5576-5577",
    "5574-5579-5580-5581-5582-5583-5585-5586-5587-5588-5590-5591-5592-5593-5594-5595-5596-5597-5598-5599-5600",
    "5601-5606-5607-5608-5609-5610",
    "5604-5611-5612-5613-5614-5616-5618-5619-5620-5621-5622",
    "5638-5639-5640-5641-5642-5643-5644-5645-5646-5647-5648-5649-5650-5652-5653-5654-5655-5656-5657-5658-5659-5660",
    "5661-5662",
    "5664-5667-5668-5669-5670-5671-5672-5673-5674-5675-5678-5679-5680-5681-5682-5684",
    "5683-5685-5686-5688-5689-5691-5692-5693-5695-5696-5697-5698-5699-5700-5702-5703-5704-5707-5708-5720-5722-5723-5736-5737-5738-5739-5740-5742-5743-5745-5746-5747-5748-5749-5750-5752-5754-5755",
    "5757-5764-5765",
    "5774-5778-5780",
    "5793-5795-5796-5800-5801-5802-5803-5805-5807-5808-5809-5810-5811-5814",
    "5874-5876-5877-5878-5879-5880-5881-5886-5887-5888-5889-5890-5891-5892-5894-5895-5896-5897-5898-5899-5901-5902-5903-5904-5905-5906-5907-5908-5909-5911-5912-5913",
    "5910-6224-6229-6230-6231-6233-6239-6240-6241-6242-6243-6244-6245-6246-6247-6249-6250-6251-6252-6253-6254-6255-6256-6258-6259-6260",
    "5914-5915-5917-5918-5919",
    "5935-5939-5940-5942-5943",
    "5938-5945",
    "5944-5946-5947-5950-5951-5953-5954-5955-5956-5957-5958-5959-5960-5962-5963-5964-5965-5967-5970-5971-5972-5973-5974-5975-5976-5977-5979",
    "5966-5980-5981-5982-5983-5985-5986-5987-5988-5989-5991-5992-5993-5994-5995-5996-5997-5998-5999-6000-6001-6002-6003-6004-6005-6006-6007-6009-6010-6012-6013-6015-6016-6017-6018-6020-6021",
    "6036-6059-6060-6061-6062-6063-6067-6068-6069-6070-6071-6073-6074-6075-6078-6079-6080-6081-6082-6086",
    "6164-6165-6166-6168-6169-6170-6171-6173-6174-6175-6176-6177-6178",
    "6179-6180-6181-6183-6184-6185-6186-6187-6189",
    "6192-6194-6199-6200-6201-6202-6204-6205-6206-6207-6208-6209-6210-6211-6212-6214-6215-6216-6217-6218-6219-6220-6222-6223-6225-6226",
    "6235-6293-6294-6295-6296-6297-6298-6299-6300-6301-6302-6304-6305-6306-6307-6308-6309-6310-6311-6312-6313-6314-6316-6317-6318",
    "6321-6323-6324-6325-6328-6329-6330-6331-6332-6333-6334-6336-6337",
    "6335-6338-6339",
    "6340-6341-6342-6343",
    "6349-6351-6352-6354-6355-6356-6357-6358-6359-6360-6363-6364-6365-6366-6367-6368-6369-6370-6371-6372-6373-6374-6375-6376-6377-6378-6379-6380-6384",
    "6505-6506-6507-6509-6510-6511-6514-6516-6527-6528-6529-6530-6531-6532-6533-6535-6536-6537-6538-6539-6540-6541-6542-6543-6544-6545-6546-6547-6548",
    "6549-6550-6551-6553-6554-6555-6556-6557-6559-6560-6562-6563-6564-6565-6566-6567-6568-6570-6571-6574-6576-6577-6578",
    "6572-6579",
    "6581-6582-6583-6585-6586-6587-6591-6592-6595",
    "6596-6598-6600-6601-6603-6604-6605-6607-6608-6609-6610-6611-6614-6615-6620-6622",
    "6626-6708-6712-6713-6717-6718-6719-6720-6721-6722-6723-6724-6725-6726-6738-6740-6741-6742-6744-6745-6746-6747-6749-6750-6751-6752-6753-6754-6755",
    "6756-6757-6758-6760-6762-6763-6766-6768-6769",
    "6761-6771-6781-6805-6806-6808-6809-6811-6812-6813-6814-6815-6817-6818-6819-6820-6822",
    "6772-6773",
]
for _ni in _BUNDLE_NIS_RXDB_ERA2:
    Instance.register("pubkey", _ni)(Rxdb6905To4948)
