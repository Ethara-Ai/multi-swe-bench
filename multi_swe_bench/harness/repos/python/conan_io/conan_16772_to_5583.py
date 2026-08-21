import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v output anchored on the trailing `<STATUS> [ NN%]` so
    parametrized node ids with internal spaces are captured whole."""
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


class ConanV1ImageBase(Image):
    """conan era 1 — v1.x (PRs 5583-16772, releases 1.19->1.66, 2019-2023).
    Package dir `conans/`; tests under conans/test/unittests/, integration/,
    functional/. Built with Python 3.9 + pre-installed `Cython<3` to make
    the era's PyYAML 5.x sdist build cleanly (PyYAML 5.x fails under Cython 3),
    plus `markupsafe<2.1`/`jinja2<3.1` so conftest.py (which imports jinja2
    via tools.microsoft) loads without ImportError."""

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
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return "base-v1"

    def workdir(self) -> str:
        return "base-v1"

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

{DockerfileEnhancer._PROXY_ARGS}

{self.global_env}

{DockerfileEnhancer._ENV_BLOCK}

ENV LC_ALL=C.UTF-8 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential curl ca-certificates && rm -rf /var/lib/apt/lists/*

{DockerfileEnhancer._CERT_SYMLINKS}

# Pre-pin Cython<3 + older wheel so PyYAML 5.x (pinned by conan v1) builds
# cleanly. PyYAML 5.x sdist fails against Cython 3 (`cython_sources` attr).
# Pre-pin markupsafe<2.1 + jinja2<3.1: jinja2 < 3.1 (which conan v1 pulls
# transitively via tools.microsoft) imports markupsafe.soft_unicode, which was
# removed in markupsafe 2.1. Without these pins, conftest.py fails to load and
# every test stage reports (0,0,0).
RUN pip install --no-cache-dir "Cython<3" "wheel<0.40" "markupsafe<2.1" "jinja2<3.1" pytest

{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ConanV1ImageDefault(Image):
    """Per-PR image: checkout base commit, install conan in editable mode
    (with test extras), run the targeted pytest unit tests."""

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
        return ConanV1ImageBase(self.pr, self._config)

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
pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# pytest file paths the PR's test patch touches. v1 puts tests under
# conans/test/unittests/, integration/, functional/ — grep matches any path
# under conans/test/ or test/. Exclude __init__/helpers/conftest fixtures.
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' \
    -W ignore::DeprecationWarning 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/(setup\\.py|pyproject\\.toml|requirements)' /home/test.patch 2>/dev/null; then
    pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' \
    -W ignore::DeprecationWarning 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(setup\\.py|pyproject\\.toml|requirements)' /home/test.patch /home/fix.patch 2>/dev/null; then
    pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' \
    -W ignore::DeprecationWarning 2>&1
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""


@Instance.register("conan-io", "5592-5594-5705-6265-6733-6748-6774-7032-7169-7401-7482-7716-7962-7977-8005-8021-8151-8246-8353-8389-8533-8734-8907-8965-9043-9073-9411-9562-9723-9758-9896-10091-10165-10195-10312-10323-11231-11361-11708-11756-11830-11859-11917-12631-12665-15706-16509")
class CONAN_16772_TO_5583(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ConanV1ImageDefault(self.pr, self._config)

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_CONAN_16772_TO_5583 = [
    "9896-9905-9908-9909-9916-9925-9932-9944-9945-9958-9974-9980-9984-9990-9992-10004-10005-10008-10013-10016-10021-10024-10026-10027-10028-10038-10049-10057-10065-10067-10068-10084-10089-10098-10099-10107-10113",
    "8246-8337-8343-8344-8352-8361-8369-8370-8371-8372-8379-8380-8381-8384-8394-8399-8401-8408-8431-8433-8436-8438-8439-8445-8447-8463-8469-8470-8474-8491-8492-8496-8501-8502-8506-8507-8509-8514-8521-8537-8540-8553",
    "8005-8483-8685-8727-8729-8749-8761-8765-8766-8767-8769-8773-8778-8779-8787-8793-8800-8806-8810-8814-8815-8819-8821-8823-8826-8828-8832-8840-8843-8847-8849-8864-8873-8880-8887",
    "7482-7488-7492-7494-7500-7512-7516",
    "6748-6809-7154-7160-7172-7174-7176-7178-7182-7183-7194-7215-7216-7219-7224-7228-7237-7238-7258-7259-7262-7263-7266-7272",
    "6733-6734-6769-6772-6780-6782-6787-6791-6794-6798-6800-6821-6824-6825-6832-6838-6858-6861-6871-6911-6914-6916-6917-6922-6928-6932-6934-6935-6937-6947",
    "5705-5740-7167-7243-7276-7277-7278-7293-7296-7302-7303-7309-7311-7314-7319-7320-7323-7327-7335-7337-7338-7341-7345-7353-7359-7360-7361-7364-7370-7372-7373-7380-7384-7389-7390-7394-7398-7399-7400-7404-7408-7412-7413-7435-7441-7443-7447-7453",
    "5594-6389-6548-6601-6614-6650-6653-6659-6670-6672-6675-6677-6679-6680-6681-6686-6688-6689-6690-6698-6700-6703-6706-6711-6712-6714-6715-6720-6722-6723-6724-6730-6731-6737-6738-6739-6740-6741-6744-6750-6757-6758-6760-6764-6767",
    "5592-5684-6451-6457-6465-6490-6492-6496-6507-6515-6516-6517-6518-6519-6520-6528-6533-6540-6541-6543-6550-6551-6555-6559-6563-6566-6571-6573-6574-6585-6602-6607-6615-6616-6622-6625-6626-6631-6632-6635-6641-6642",
    "16509-16658-16728-16732-16815",
    "15706-15731-15741-15948-16005",
    "11917-12049-12117-12307-12457-12486-12491-12505-12509-12513-12516-12517-12518-12529-12536-12543-12547-12556-12559-12562-12580-12598-12599-12601-12609-12620-12622-12623-12632-12635",
    "11361-11365-11381-11390-11391-11407-11414-11415-11416-11440-11443-11445-11446-11449-11452-11455-11470-11471-11478-11479-11488-11491-11503-11505-11507-11519-11523-11533-11536",
    "10091-10558-10686-10696-10725-10730-10731-10734-10738-10743-10746-10755-10760-10770-10774-10783-10797-10799-10800-10808-10812-10834-10839-10842-10846-10856-10868-10872-10874-10875-10880-10884-10898-10903-10904-10906-10908-10917-10920-10924-10928-10931",
    "8965-10250-10484-10492-10527-10530-10532-10536-10537-10552-10567-10573-10583-10586-10590-10594-10595-10596-10600-10608-10612-10616-10619-10625-10633-10635-10642-10653-10654-10655-10656-10659-10663-10665-10672-10673-10675-10681-10687-10692-10694-10695-10700-10706-10707-10710-10712-10729",
]

for _ni in _BUNDLE_NIS_CONAN_16772_TO_5583:
    Instance.register("conan-io", _ni)(CONAN_16772_TO_5583)
