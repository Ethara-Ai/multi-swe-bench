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
        return "node"

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
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

ENV CI=true

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git jq curl ca-certificates \\
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

    def dependency(self) -> Image | None:
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

npm install -g pnpm@8

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
pnpm install --frozen-lockfile --ignore-scripts || pnpm install --no-frozen-lockfile --ignore-scripts || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
pnpm install --frozen-lockfile --ignore-scripts || pnpm install --no-frozen-lockfile --ignore-scripts || true
pnpm exec tsc --noEmit 2>&1 && echo "TSC_CHECK: passed" || echo "TSC_CHECK: failed"
pnpm exec vite build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --exclude '*.png' --exclude '*.ico' --exclude '*.icns' --exclude '*.gif' --exclude '*.ttf' --whitespace=nowarn /home/test.patch
pnpm install --frozen-lockfile --ignore-scripts || pnpm install --no-frozen-lockfile --ignore-scripts || true
pnpm exec tsc --noEmit 2>&1 && echo "TSC_CHECK: passed" || echo "TSC_CHECK: failed"
pnpm exec vite build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --exclude '*.png' --exclude '*.ico' --exclude '*.icns' --exclude '*.gif' --exclude '*.ttf' --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm install --frozen-lockfile --ignore-scripts || pnpm install --no-frozen-lockfile --ignore-scripts || true
pnpm exec tsc --noEmit 2>&1 && echo "TSC_CHECK: passed" || echo "TSC_CHECK: failed"
pnpm exec vite build

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

        # Canonical hardening from image.py, pinned to this PR's literal base.sha
        # (the PR image has an Image-typed dependency, so the enhancer returns raw).
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


@Instance.register("clash-verge-rev", "clash-verge-rev")
class ClashVergeRev(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        re_tsc_error = re.compile(r"^(\S+\.tsx?)(?:\(\d+,\d+\))?:\s*error\s+TS\d+:")
        re_vite_built = re.compile(r"✓\s+built in\b", re.IGNORECASE)
        re_vite_error = re.compile(r"^(?:\[vite\]|error|Error:)\s+(.+)$")
        re_build_failed = re.compile(r"\b(?:ELIFECYCLE|build failed|Build failed)\b", re.IGNORECASE)
        re_tsc_check = re.compile(r"^TSC_CHECK:\s*(passed|failed)", re.IGNORECASE)

        vite_built = False
        vite_error = False
        tsc_check_passed = None

        for raw_line in test_log.splitlines():
            clean = ansi_re.sub("", raw_line).strip()
            if not clean:
                continue

            tsc_match = re_tsc_error.match(clean)
            if tsc_match:
                failed_tests.add(tsc_match.group(1))
                continue

            if re_vite_built.search(clean):
                vite_built = True
                continue

            if re_build_failed.search(clean):
                vite_error = True
                continue

            vite_err = re_vite_error.match(clean)
            if vite_err and "error" in clean.lower():
                # "error during build:" is vite's banner line, not a test name.
                detail = vite_err.group(1).strip()
                if detail and detail.lower() != "during build:":
                    failed_tests.add(detail[:200])
                vite_error = True
                continue

            tsc_check = re_tsc_check.match(clean)
            if tsc_check:
                tsc_check_passed = tsc_check.group(1).lower() == "passed"

        # These two names are synthetic, so they must not collide with any string
        # the repo itself contains: report.py's cheating guard rejects a record
        # when a credited test name appears in the fix patch, and "web:build" is
        # a real npm script in this repo's package.json. The harness: prefix keeps
        # them out of the source text.
        #
        # web:build must always be emitted with a definite status. Reporting it
        # only when nothing else failed leaves it absent (status NONE) at the run
        # and test stages, so a genuine FAIL->PASS build fix lands in neither f2p
        # nor n2p and the record classifies valid with empty transition sets.
        if vite_built and not vite_error:
            passed_tests.add("harness:web-build")
        elif vite_error:
            failed_tests.add("harness:web-build")

        if tsc_check_passed is True:
            passed_tests.add("harness:tsc-check")
        elif tsc_check_passed is False:
            failed_tests.add("harness:tsc-check")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Data-derived from Dataset/clash-verge-rev__clash-verge-rev_lht_final.jsonl.
# Regenerate when bundles change.
_BUNDLE_NIS_ClashVergeRev = [
    "1-2-3-21-24",
    "106-110-112",
    "1127-1137-1138-1141-1157",
    "1326-1353-1390-1391-1396",
    "1430-1457-1484",
    "1739-1939-1945-1961-1995-2033-2045",
    "2176-2198",
    "246-250-255-256",
    "2818-2820-2825-2827-2830-2831-2835-2841-2842-2851-2855-2856-2857-2869-2880-2886-2896-2900-2906-2909-2913-2917-2925-2956-2967-2978-2981",
    "450-508-521",
    "524-526-528",
    "545-558",
    "622-632",
    "1114-1117",
    "27-29-33-34-38-40-41-44-47",
    "273-284",
    "2973-3345-3346-3357-3365-3390-3391-3404-3457-3498-3502-3505-3507-3510-3513-3521-3530-3532-3534-3587-3660-3674-3682-3684-3708-3714-3715-3717-3719-3728-3740",
    "3794-3831-3841-3854-3860-3862-3867-3871-3872-3875-3882-3886-3896-3906-3908-3909-3910-3914-3918-3926-3927-3932-3933-3943-3944-3951-3960-3963-3969-3975-3979-3986-3987-3989-3990-3995-3997-3999-4011-4014-4023-4026-4029-4072-4073-4074-4082-4086-4128",
    "4098-4133-4164-4179-4183-4213-4214-4215-4228-4229-4231-4243-4254-4271-4275-4277-4279-4286-4294-4297-4324-4329-4342-4347-4348-4359-4360-4367-4380-4400-4401-4404-4408-4409-4428-4430-4443-4446-4451-4454-4461-4471",
    "4467-4468-4491-4502-4523-4542-4553-4554-4564-4568",
    "4624-4674-4681-4682-4686-4687-4698-4702-4731-4753-4765-4783-4788-4794-4795-4796-4803-4805-4815-4817-4818-4824-4833-4834-4841-4842-4843-4844-4857-4888-4889-4890-4899-4919-4922-4924-4926-4939-4940-4941-4942-4951-4952-4954-4955-4956-4958-4959-4960-4962-4964-4965-4968-4972-4974-4975-4979-4980-4984-4985-4986-4987-4988-4993-4995-4996-4997-5000-5003-5004-5007-5015-5017-5021-5023-5024-5033-5037-5038-5040-5044-5045-5046-5048-5051-5052-5054-5055-5058-5059-5060-5063-5064-5065-5068-5070-5073-5093-5103-5104-5108-5111-5112-5113-5115-5119-5120-5138-5139-5141-5142-5145-5147-5149-5150-5154-5156-5158-5159-5163-5165-5166-5167-5168-5169-5170-5171-5175-5176-5178-5180-5185-5189-5197-5198-5205-5210-5211-5212-5215-5216-5217-5218-5221-5224-5225-5231-5234-5245-5246-5251-5258-5261-5264-5265-5268-5277-5278-5280-5281-5284-5303-5311-5317-5319-5321-5324-5325-5331-5338-5342-5346-5349",
    "5244-5249-5263-5276-5287-5374-5375-5387-5394-5400-5409-5412-5416-5417-5420-5424-5431-5441-5452-5453-5455-5462-5463-5465-5468-5469-5470-5479-5486-5489-5500-5501-5502-5503-5510-5511-5522-5527-5528-5533-5534-5535-5540-5541-5544-5547-5548-5554-5555-5557-5558-5561-5565-5576-5577-5583-5587-5588-5593-5595-5600-5613-5625-5631-5637-5646-5647-5648-5660-5661-5663-5670-5671-5675-5682-5690-5691-5692-5693-5698-5699-5701-5706-5711-5719-5737-5739-5741-5754-5758-5768-5769-5780-5786-5787-5790-5791-5792-5799-5800-5801-5802-5808-5812-5827-5846-5866",
    "5621-5669-5724-5728-5815-5819-5886-5905-5912-5918-5941-5942-5946-5959-5961-5965-5969-5990-5994-5996-5997-6005-6007-6010-6014-6015-6042-6051-6053-6063-6064-6073-6087-6088-6114-6118-6119-6142-6143-6152",
    "6312-6318-6323-6340-6347-6348-6351-6356-6358-6361-6362-6369-6373-6377-6379-6382-6384-6396-6397-6403-6419-6420-6421-6429-6432-6438-6440-6442-6446-6447-6463-6464-6465-6467-6474-6475-6476-6490-6492-6494-6496-6500-6502-6508-6509-6511-6515-6522-6523-6524-6525-6528-6536-6541-6543-6547-6549-6551-6555-6558-6571",
    "724-788-799-804-815-816-821-822-840-857-887-889-895-900-901-904-908-911",
]

for _ni in _BUNDLE_NIS_ClashVergeRev:
    Instance.register("clash-verge-rev", _ni)(ClashVergeRev)
