from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MilvusGoImageBase(Image):
    """Base image for milvus-io/milvus Go era (v2.0+, PRs 5141-99999).

    Go + C++ hybrid project. Go tests use `go test -v`, but many packages
    depend on CGO bindings to the C++ core (Knowhere, segcore). For pure-Go
    packages the standard `go test` works; CGO-dependent packages need the
    built C++ libraries on LD_LIBRARY_PATH.
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

    def dependency(self) -> str | Image:
        return "golang:1.25"

    def image_tag(self) -> str:
        return "base-go"

    def workdir(self) -> str:
        return "base-go"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    CGO_ENABLED=1 \\
    GOPROXY=https://proxy.golang.org,direct \\
    GOTOOLCHAIN=auto

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    cmake \\
    g++ \\
    gcc \\
    make \\
    wget \\
    curl \\
    pkg-config \\
    libssl-dev \\
    protobuf-compiler \\
    unzip \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class MilvusGoImageDefault(Image):
    """Per-PR image for milvus-io/milvus Go era.

    Runs Go tests with `-v -count=1 -tags dynamic` on packages that have
    test files in the test_patch. Falls back to `./...` for broad coverage.
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
        return MilvusGoImageBase(self.pr, self.config)

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

""",
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

go mod tidy || true
go mod download || true

# Generate proto files if the Makefile target exists
if grep -q "generated-proto-without-cpp" Makefile 2>/dev/null; then
  mkdir -p cmake_build/bin
  ln -sf "$(which protoc)" cmake_build/bin/protoc
  make generated-proto-without-cpp 2>&1 || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
go mod tidy || true
go test -v -count=1 -timeout 600s ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch; true
go mod tidy || true
go test -v -count=1 -timeout 600s ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch; true
go mod tidy || true
go test -v -count=1 -timeout 600s ./...

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

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("milvus-io", "milvus_5141_to_99999")
class Milvus_5141_to_99999(Instance):
    """Instance for milvus-io/milvus Go era (v2.0+).

    Build system: Go modules + CMake for C++ core
    Test framework: Go test (standard `go test -v` output)
    PRs 5141-99999: Go + C++ hybrid project.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MilvusGoImageDefault(self.pr, self._config)

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
        """Parse Go test output.

        Format:
          --- PASS: TestName (0.00s)
          --- FAIL: TestName (0.00s)
          --- SKIP: TestName (0.00s)
          === RUN   TestName
        """
        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            stripped = line.strip()

            pass_match = re_pass.search(stripped)
            if pass_match:
                test_name = pass_match.group(1)
                base_name = get_base_name(test_name)
                if base_name not in failed_tests:
                    passed_tests.add(base_name)
                continue

            fail_match = re_fail.search(stripped)
            if fail_match:
                test_name = fail_match.group(1)
                base_name = get_base_name(test_name)
                passed_tests.discard(base_name)
                failed_tests.add(base_name)
                continue

            skip_match = re_skip.search(stripped)
            if skip_match:
                test_name = skip_match.group(1)
                base_name = get_base_name(test_name)
                if base_name not in failed_tests and base_name not in passed_tests:
                    skipped_tests.add(base_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Registered so delivered records (which carry the dash-joined number_interval)
# resolve to this era class (PIPELINE §11/§11c). The era-tag key above still
# routes the build-time dataset.
# Go era (PRs 5141+): golang:1.25, CGO + C++ core.
_BUNDLE_NIS_MILVUS_GO = [
    "11393-14207-14418-15119-15134-15138-15163-15171-15172-15173-15174-15182-15251-15273-15358-15377-15381-15384-15387-15389-15408-15410-15411-15414-15417-15419-15422-15424-15425-15426-15427-15431-15434-15436-15447-15448-15453-15455-15461-15463-15467-15482-15485-15487-15491-15493-15495-15497-15498-15500-15501-15502-15506-15507-15511-15512-15515-15525-15528-15530-15531-15537-15539-15542-15543-15545-15547-15550-15551-15553-15563-15569-15571-15572-15580-15581-15586-15588-15590-15591-15595-15598-15601-15602-15603-15618-15626-15631-15636-15638-15639-15643-15663-15664-15673-15675-15678-15686-15687-15690",
    "15541-15577-15582-15606-15614-15640-15647-15649-15650-15680-15684-15693-15698-15700-15701-15702-15706-15707-15709-15712-15715-15725-15726-15727-15732-15733-15737-15738-15740-15743-15748-15749-15752-15753-15759-15760-15761-15770-15774-15776-15787-15790-15795-15796-15798-15801-15803-15804-15809-15813-15814-15821-15827-15838-15839-15845-15853-15870-15932-15935-15956-16035-16058-16063-16066-16070-16072-16178-16243-16244-16245-16252-16253-16259-16327-16331-16338",
    "17899-18570-18584-18627-18632-18658-18678-18679-18683-18693-18701-18708-18714-18727-18732-18733-18740-18745-18753-18783-18784-18790-18795-18796-18797-18844-18850-18858-18881-18884-18886-18889-18895-18906-18919-18934-18937-18947-18953-18990-18991-18996-19002-19010-19021-19028-19045-19060-19076-19080-19091-19111-19112-19131-19132-19135-19136-19173",
    "18394-18410-18423-18427-18432-18467-18513-18542-18568-18569",
    "19309-19326-19353-19371-19391-19402-19406-19421-19426-19436-19465-19476-19486-19487",
    "20499-20631-20690-20696-20699-20722-20728-20737-20739-20742-20750-20754-20759-20762-20770-20778-20782-20785-20788-20814-20826-20827-20834-20840-20844-20847-20872-20881-20883-20887-20890-20899-20900-20901-20902-20903-20910-20923-20930-20931-20939-20940-20941-20942-20943-20950-20971-20974-20976-20984-21010-21011-21012-21016-21019-21024-21028-21029-21030-21040-21048-21054-21058-21066-21067-21073-21079-21083-21105-21114-21119-21121-21130-21132-21133-21135-21136-21137-21139-21145-21146-21150-21154-21155-21163-21164-21174-21178-21183-21214-21224-21226-21227-21232-21233-21241-21243-21244-21246-21255",
    "21314-21320-21321-21329-21333-21334",
    "21658-22084-22111-22124-22136-22145-22154-22176-22188-22197-22208-22209-22215-22225-22227-22233-22238-22239-22241-22251-22252-22255-22257-22258-22267-22269-22274-22285-22287-22291-22296-22306-22311-22313-22317-22322-22326-22329-22331-22339-22340-22341-22353-22357-22368-22369-22370-22371-22375-22377-22378-22386-22395-22400-22402-22414-22423-22433-22437-22440-22441-22442-22444-22446-22449-22452-22454-22464-22470-22472-22474-22476-22486-22487-22493-22505-22509-22514-22517-22518-22523-22526-22529-22543-22544-22548-22551-22560-22584-22589-22596-22598-22601-22611-22614-22618-22622-22632-22634-22635-22653-22659-22660-22667-22668-22673-22675-22686-22691-22696-22721-22723-22725-22729-22731-22734-22739-22741-22746-22752-22756-22771-22802-22807-22818",
    "23814-23835-23838",
]
for _ni in _BUNDLE_NIS_MILVUS_GO:
    Instance.register("milvus-io", _ni)(Milvus_5141_to_99999)
