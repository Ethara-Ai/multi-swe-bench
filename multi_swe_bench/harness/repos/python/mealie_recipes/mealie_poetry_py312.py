import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Shared per-era base: OS + toolchain + a FULL clone of the repo (all
    history, NO checkout, NO hardening). Built ONCE and reused by every PR in
    this era. The leading `# syntax=` directive makes DockerfileEnhancer return
    this Dockerfile verbatim (image.py: `if SYNTAX_DIRECTIVE in raw: return raw`)
    so the enhancer does NOT inject the ${BASE_COMMIT} hardening pass here — the
    base has no BASE_COMMIT and must keep full history so any PR's base.sha stays
    reachable. Per-PR checkout + hardening live in ImageDefault.
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
        return "python:3.12-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-py312-poetry"

    def workdir(self) -> str:
        return "base-py312-poetry"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """# syntax=docker/dockerfile:1.6
FROM python:3.12-bookworm

ARG TARGETARCH
ARG REPO_URL="https://github.com/mealie-recipes/mealie.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="mealie-recipes/mealie" \\
      org.opencontainers.image.description="mealie-recipes/mealie Docker image" \\
      org.opencontainers.image.source="https://github.com/mealie-recipes/mealie" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential patch libsasl2-dev libldap2-dev libssl-dev ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip && pip install poetry

RUN git clone "${REPO_URL}" /home/mealie

WORKDIR /home/mealie
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

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

    def image_prefix(self) -> str:
        return "envagent"

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
                """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y libsasl2-dev libldap2-dev libssl-dev
###ACTION_DELIMITER###
pip install poetry
###ACTION_DELIMITER###
poetry install || (poetry lock && poetry install)
###ACTION_DELIMITER###
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/test.patch ) || true
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/test.patch ) || true
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/fix.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/fix.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/fix.patch ) || true
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # Two-stage: chain to the shared ImageBase *Image*. Because dependency()
        # returns an Image (not a str), DockerfileEnhancer returns this verbatim
        # and supplies neither ARG BASE_COMMIT nor the hardening pass — so we set
        # BASE_COMMIT and embed Image._HARDENING_BLOCK ourselves. The base holds
        # a full clone; here we check out THIS PR's base.sha, install deps against
        # it, then the hardening block prunes every other ref/commit (reward-hack
        # defense). `hardening` is inserted as a plain value so its ${...}/$(...)
        # tokens stay byte-identical; literal Dockerfile braces are doubled.
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        base_sha = self.pr.base.sha
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{base_sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{repo}
RUN git checkout {base_sha}
RUN poetry install || (poetry lock && poetry install)

{copy_commands}
{hardening}
CMD ["/bin/bash"]
"""


@Instance.register("mealie-recipes", "mealie_poetry_py312")
class MEALIE_POETRY_PY312(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest `-rA` short test summary lines:
        #   PASSED tests/unit_tests/test_config.py::test_name[a b]
        #   FAILED tests/unit_tests/test_config.py::test_name - AssertionError: ...
        #   ERROR  tests/unit_tests/test_x.py::test_y - ...
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(.+?)\s*$", re.MULTILINE
        )
        for status, name in summary_pattern.findall(log):
            if status in ("FAILED", "ERROR"):
                name = re.sub(r"\s+-\s.*$", "", name).strip()
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name.strip())
            # XFAIL / XPASS: expected-fail bookkeeping, not real pass/fail

        # Grouped skip summary: SKIPPED [6] tests/unit_tests/test_x.py:18: reason
        for m in re.finditer(
            r"^SKIPPED\s+\[\d+\]\s+(\S+?):(\d+):", log, re.MULTILINE
        ):
            skipped_tests.add(f"{m.group(1)}:{m.group(2)}")

        # Defensive fallback: verbose per-test lines `nodeid STATUS [ 12%]`
        verbose_pattern = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$",
            re.MULTILINE,
        )
        for name, status in verbose_pattern.findall(log):
            name = name.strip()
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

# Route bundled PRs by their dash-joined `prs_in_bundle` interval to this era.
# Instance.create() looks up f"{org}/{number_interval}", so every bundle whose
# base.sha matches this era (poetry era, pyproject python 3.12, no uv.lock — python:3.12-bookworm)
# must be registered here. Era was derived from the repo state at each base.sha
# (packaging files), not from PR-number ranges — routing is NOT monotonic in PR
# number (e.g. bundle 5883 is uv-era while the higher 6128/6268 are poetry-era).
# 13 bundle(s); intervals come from the lht dataset's prs_in_bundle.
_NUMBER_INTERVALS = [
    "4415-4812-4933-4934-4935-4938-4939-4941-4942-4948-4952-4953-4965-4967-4968-4970-4971-4972-4975-4980-4985-4988-4989-4990-4991-4992-4995-4996-5008-5011-5016-5017-5018-5020-5021",
    "4551-4608-4843-4940-4958-5009-5022-5026-5029-5030-5031-5032-5038-5039-5042-5045-5046-5047-5048-5049-5051-5054-5065-5067-5069-5070-5073-5078-5079-5080-5086-5087-5089-5090-5091-5092-5093-5098-5099-5100-5101-5102-5103-5105-5106-5107",
    "4577-4658-4746-4747-4750-4751-4752-4753-4754-4759-4760-4764-4769-4772-4774-4781-4784-4787-4791-4792-4797-4801-4813-4815-4816-4821-4823-4826-4827-4831-4835-4837-4838-4840",
    "4616-4726-4803-4805-4810-4814-4842-4845-4847-4849-4852-4853-4854-4855-4857-4862-4863-4864-4865-4867-4869-4870-4871-4872-4873-4874-4875-4877-4882-4886-4887-4888-4889-4890-4891-4896-4897-4898-4899-4900-4902-4905-4906-4908-4909-4910-4911-4915-4917-4919-4920-4922-4926-4928-4929-4932",
    "4800-6170-6211-6248-6259-6358-6438-6469-6470-6471-6478-6480-6481-6485-6486-6487-6490-6492-6493-6494-6495-6498-6500-6502-6503-6506-6507-6508-6518-6524-6525-6528-6539",
    "4943-5050-5154-5178-5184-5197-5204-5219-5227-5235-5236-5238-5243-5244-5245-5246-5247-5248-5250-5251-5252-5253-5254-5258-5259-5260-5262-5263-5268-5269-5270-5274-5275-5278-5280-5281-5283-5284-5285-5289-5290-5293-5294-5295-5297-5300-5301-5304-5308-5310-5311-5312-5313-5314-5317-5318-5320-5322-5325-5327-5328-5333-5335-5337-5340-5342-5343-5344-5345-5346-5347-5352-5357-5359-5360-5361-5365-5366-5368-5370-5372-5373-5374-5378-5379-5381-5388-5390-5394-5396-5398-5403-5404-5405-5407-5410-5412-5416-5418-5424-5428-5429-5434-5435-5438-5441-5442-5445-5446-5447-5450-5455-5457-5458-5461-5462-5463-5464-5466-5467-5468-5469-5470-5471-5472-5473-5474-5484-5485-5486-5487-5488-5491-5495-5497-5498-5499-5500-5501-5502-5503-5505-5506-5507-5510-5512-5515-5518-5519-5520-5522-5523-5527-5533-5534-5535-5536-5537-5538-5542-5545-5546-5547-5552-5553-5555-5557-5558-5559-5560-5561-5564-5565-5567-5568-5570-5571-5577-5579-5581-5584-5585-5587-5588-5589-5590-5591-5592-5595-5598-5603-5605-5608-5609-5610-5611-5612-5613-5615-5618-5619-5624-5625-5627-5629-5630-5631-5632-5633-5635-5636-5637-5639-5640-5641-5642-5643-5644-5652-5653-5655-5656-5657-5659-5660-5662-5663-5664-5665-5666-5667-5668-5669-5672-5673-5674",
    "5061-5127-5129-5130-5131-5134-5135-5137-5138-5139-5141-5142-5145-5147-5149-5150-5158-5159-5161-5162-5163-5165-5166-5167-5174-5175-5176-5179-5180-5182-5183-5185-5187-5188-5189-5194-5198-5199-5200-5201-5208-5209-5220-5228-5229-5233",
    "5085-5125-5709-6310-6330-6336-6341-6344-6345-6346-6347-6351-6352-6353-6354-6356-6357-6359-6361-6362-6364-6366-6367-6370-6371-6372-6376-6377-6381-6383-6384-6385-6388-6389-6391-6392-6394-6395-6396-6397-6398-6400-6407-6408-6409-6412-6418-6422-6424-6425-6426-6429-6430-6431-6432-6434-6435-6436-6439-6440-6441-6442-6444-6445-6446-6448-6450-6451-6455-6456-6457-6462-6464-6465",
    "5622-5654-5661-5683-5702-5708-5710-5713-5714-5715-5717-5718-5722-5725-5726-5727-5728-5730-5736-5743-5744-5748-5749-5750-5754-5755-5756-5757-5758-5759-5762-5764-5766",
    "5647-5684-5687-5716-5737-5739-5746-5765-5767-5768-5769-5770-5771-5775-5780-5781-5783-5785-5787-5792-5794-5795-5796-5798-5800-5802-5804-5805-5808-5809-5811-5813-5814-5815-5816-5817-5821-5825-5826-5827-5828-5829-5830-5831-5835-5836-5837-5838-5839-5840-5843-5845-5847-5849-5852-5853-5854-5855-5856-5860-5861-5862-5866-5867-5869-5870-5877-5878-5879-5881-5882-5884-5887-5889-5890-5892-5894-5895-5896-5897-5899-5900-5901-5903-5904-5908-5912-5913-5914-5915-5917-5918-5919-5920-5922-5923-5924-5925-5926-5928-5929-5932-5933-5934-5935-5936-5938-5939-5941-5942-5943-5946-5949-5952-5953-5955-5956-5958-5962-5963-5964-5965-5967-5969-5979-5981-5982-5984-5985-5986-5989",
    "5864-5911-5975-6011-6018-6021-6026-6032-6034-6035-6037-6041-6042-6043-6044-6048-6049-6050-6054-6056-6058-6062-6063-6066-6067-6069-6070-6071-6073-6075-6076-6077-6079-6080-6082-6083-6086-6088-6089-6090-6091-6092-6093-6094-6095-6096-6097-6099-6100-6101-6102-6103-6105-6106-6107-6108-6113-6116-6117-6118-6121-6122-6123-6125-6127-6129-6130-6132-6133-6134-6135-6136-6137-6139-6142-6143-6150-6156-6158-6160",
    "6128-6138-6141-6145-6146-6147-6148-6151-6159-6161-6162-6172-6174-6176-6178-6179-6181-6182-6185-6191-6193-6194-6196-6198-6200-6205-6206-6213-6215-6216-6218-6219-6220-6221-6222-6223-6224-6225-6227-6228-6230-6231-6233-6234-6236-6237-6239-6240-6241-6242-6243-6245-6246-6250-6253-6254-6256-6257-6262-6264-6266",
    "6268-6273-6295-6296-6299-6300-6301-6302-6308-6309-6313-6317-6318-6320-6321-6322-6324-6327-6328-6332-6334-6335",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("mealie-recipes", _interval)(MEALIE_POETRY_PY312)
