import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v output anchored on the trailing `<STATUS> [ NN%]` so
    parametrized node ids with internal spaces/brackets are captured whole."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_line = re.compile(
        r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
    )

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).strip()
        m = re_line.match(line)
        if not m:
            continue
        nodeid, status = m.group(1).strip(), m.group(2)
        if status in ("PASSED", "XPASS"):
            passed_tests.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(nodeid)
        else:
            skipped_tests.add(nodeid)

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


class SktimePy311ImageBase(Image):
    """sktime era 2 (PRs with requires-python `<3.12`; releases 0.15->0.24,
    2022-2024). Python 3.11 covers `>=3.7,<3.12` and `>=3.8,<3.12`
    constraints. Routing is by python_requires at base SHA (sktime's
    parallel release branches make PR# unreliable as era discriminator)."""

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
        return "python:3.11-slim"

    def image_tag(self) -> str:
        return "base-py311"

    def workdir(self) -> str:
        return "base-py311"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class SktimePy311ImageDefault(Image):
    """Per-PR image: checkout base commit, install sktime + dev extras
    (pytest comes from [dev] in all sktime versions), run targeted pytest."""

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
        return SktimePy311ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -5 \\
    || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -5 \\
    || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -5 || true
python -m pytest --version 2>&1 | head -1 || pip install --no-cache-dir pytest pytest-xdist || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.npy' --exclude='*.npz' --exclude='*.parquet' --exclude='*.pkl' \\
    --exclude='*.joblib' --exclude='*.h5' --exclude='*.hdf5' --exclude='*.arff' \\
    --exclude='*.tsv' --exclude='*.tsf' --exclude='*.tar.gz' --exclude='*.xlsx' \\
    --exclude='*.mat' --exclude='*.xls' --exclude='*.nc')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/(setup\\.py|pyproject\\.toml|setup\\.cfg|requirements)' /home/test.patch 2>/dev/null; then
    timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -3 || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.npy' --exclude='*.npz' --exclude='*.parquet' --exclude='*.pkl' \\
    --exclude='*.joblib' --exclude='*.h5' --exclude='*.hdf5' --exclude='*.arff' \\
    --exclude='*.tsv' --exclude='*.tsf' --exclude='*.tar.gz' --exclude='*.xlsx' \\
    --exclude='*.mat' --exclude='*.xls' --exclude='*.nc')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(setup\\.py|pyproject\\.toml|setup\\.cfg|requirements)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -3 || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
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

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{self.pr.repo}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=$BASE_COMMIT

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
"""


class SKTIME_5277_TO_3003(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SktimePy311ImageDefault(self.pr, self._config)

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
        return parse_pytest_log(log)


_BUNDLE_NIS_ERA2 = [
    "3003-3667-4012-4381-4416-4439-4444-4452-4455-4456-4461-4464-4465-4466-4469-4470-4472-4474-4476-4477-4478-4479-4480-4481-4483-4484-4486-4487-4488-4490-4492-4493-4497-4501-4503-4505-4506",
    "3151-3777-3843-4508-4510-4512-4513-4514",
    "3630-3822-4005-4160-4190-4214-4215-4221-4223-4225-4228-4231-4232-4238-4241-4243-4244-4245-4246-4247-4248-4250-4252-4253-4255-4261-4267-4269-4270-4271-4272-4274-4275-4276-4279-4284-4285-4287-4288-4289-4290-4291-4294-4297-4302-4305-4306-4308-4309-4310-4311-4312-4316-4317-4318-4319-4320-4321-4322-4323-4324-4328-4329-4331-4334-4337-4339-4340-4342-4346-4347-4353-4355-4356-4358-4360-4361-4364-4366-4367-4368-4371-4376-4382-4388-4389-4390-4391-4392-4393-4394-4395-4397-4398-4399-4402-4406-4411-4412-4414-4415-4417-4421-4423-4424-4425-4428",
    "4112-4216-4463-4580-4637-4644-4681-4724-4729-4736-4738-4757-4758-4759-4760-4761-4763-4764-4768-4770-4771-4772-4774-4775-4779-4780-4781-4782-4784-4788-4789-4793-4795-4800-4810-4811-4812-4813-4815-4816-4819-4821-4823-4824-4825-4826-4828-4831-4832-4833-4836-4851-4852-4854-4855-4856-4859-4860-4861-4862-4867-4870-4876-4879-4890",
    "4185-4496-4498-4499-4522-4523-4525-4526-4527-4529-4530-4531-4532-4533-4538-4539-4542-4545-4546-4548-4551-4552-4554-4556-4559-4560-4561-4563-4564-4567-4568-4571-4572-4573-4575-4577-4583-4586-4588-4589-4590-4593-4594-4597-4599-4600-4601-4603-4604-4605-4606-4607-4609-4612-4613-4614-4616-4618-4619-4620-4621-4625-4627-4628-4629-4630-4631-4633-4634-4636-4640-4641-4647",
    "4427-4432-4433-4435-4436-4437-4438-4442-4443-4448-4450",
    "4429-4622-4646-4654-4657",
    "4638-4648-4649-4662-4663-4667-4672-4673-4675-4679-4680-4682-4684-4686-4689-4693-4694-4699-4705-4707-4711-4714-4715-4716-4719-4722-4732-4737",
    "4717-4720-4725-4726-4731-4733-4734-4735-4741",
    "4778-4790-4880-4894-4913-4914-4915",
    "4822-5106-5120-5121",
    "5224-5226",
    "5277-5299-5319-5342-5345-5396-5403-5404-5415-5419",
]
for _ni in _BUNDLE_NIS_ERA2:
    Instance._registry[f"sktime/{_ni}"] = SKTIME_5277_TO_3003
