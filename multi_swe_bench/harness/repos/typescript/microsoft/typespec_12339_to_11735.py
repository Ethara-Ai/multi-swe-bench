from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "typespec"


class typespec_12339_to_11735_ImageBase(Image):
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
        return "node:24"

    def image_tag(self) -> str:
        return "base-node24-dotnet10"

    def workdir(self) -> str:
        return "base-node24-dotnet10"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{REPO_DIR}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {REPO_DIR} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV DOTNET_ROOT=/usr/share/dotnet
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_NOLOGO=1
ENV DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ENV PATH=/usr/share/dotnet:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates libicu-dev default-jdk-headless maven python3 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh && bash /tmp/dotnet-install.sh --version 10.0.400 --install-dir /usr/share/dotnet && rm -f /tmp/dotnet-install.sh && dotnet --version
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh && uv python install 3.12
RUN npm install -g pnpm@11.10.0

WORKDIR /home/

{code}

{self.clear_env}

"""


class typespec_12339_to_11735_ImageDefault(Image):
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
        return typespec_12339_to_11735_ImageBase(self.pr, self._config)

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
                "typespec_tests.sh",
                r"""#!/bin/bash
REPO=/home/[[REPO_DIR]]
CS=packages/http-client-csharp
PY=packages/http-client-python
JV=packages/http-client-java
PYENV=/opt/typespec-py
FILES=$(grep '^diff --git' /home/test.patch | sed 's|diff --git a/||;s| b/.*||')

has() {
    echo "$FILES" | grep -q "$1"
}

cs_projects() {
    local out=""
    has "^$CS/generator/Microsoft.TypeSpec.Generator/test/" && out="$out generator/Microsoft.TypeSpec.Generator/test/Microsoft.TypeSpec.Generator.Tests.csproj"
    has "^$CS/generator/Microsoft.TypeSpec.Generator.ClientModel/test/" && out="$out generator/Microsoft.TypeSpec.Generator.ClientModel/test/Microsoft.TypeSpec.Generator.ClientModel.Tests.csproj"
    has "^$CS/generator/Microsoft.TypeSpec.Generator.Input/test/" && out="$out generator/Microsoft.TypeSpec.Generator.Input/test/Microsoft.TypeSpec.Generator.Input.Tests.csproj"
    has "^$CS/generator/TestProjects/Local" && out="$out generator/TestProjects/Local.Tests/TestProjects.Local.Tests.csproj"
    has "^$CS/generator/TestProjects/Local/Sample-TypeSpec/tests/" && out="$out generator/TestProjects/Local/Sample-TypeSpec/tests/SampleTypeSpec.Tests.csproj"
    has "^$CS/generator/TestProjects/Spector" && out="$out generator/TestProjects/Spector.Tests/TestProjects.Spector.Tests.csproj"
    has "^$CS/generator/TestProjects/Plugin.Tests/" && out="$out generator/TestProjects/Plugin.Tests/TestProjects.Plugin.Tests.csproj"
    echo $out
}

ws_packages() {
    echo "$FILES" | grep '^packages/' | cut -d/ -f2 | sort -u | grep -vxE 'http-client-csharp|http-client-python|http-client-java'
}

setup() {
    set -e
    cd $REPO
    local need_root=0
    [ -n "$(ws_packages)" ] && need_root=1
    has "^$CS/emitter/" && need_root=1
    has "^$PY/emitter/" && need_root=1
    if [ $need_root = 1 ]; then
        pnpm install --frozen-lockfile
    fi
    for p in $(ws_packages); do
        name=$(node -p "require('./packages/$p/package.json').name")
        pnpm --filter "$name^..." run build
    done
    if has "^$CS/"; then
        (cd $CS && npm ci)
        for proj in $(cs_projects); do
            if [ -f "$CS/$proj" ]; then
                (cd $CS && dotnet restore "$proj")
            fi
        done
    fi
    if has "^$PY/"; then
        uv venv --python 3.12 $PYENV
        if [ -f $PY/tests/requirements/unbranded.txt ]; then
            uv pip install --python $PYENV/bin/python -r $PY/tests/requirements/unbranded.txt
        else
            uv pip install --python $PYENV/bin/python -r $PY/tests/requirements/base.txt
        fi
        uv pip install --python $PYENV/bin/python -e $PY/generator
        if has "^$PY/emitter/"; then
            (cd $PY && npm ci --ignore-scripts)
        fi
    fi
    if has "^$JV/"; then
        mvn -B -f $JV/generator/pom.xml -pl http-client-generator-core -am -DskipTests -Dspotless.skip=true -Dspotless.check.skip=true -Dspotless.apply.skip=true install
    fi
}

run_tests() {
    cd $REPO
    for proj in $(cs_projects); do
        if [ -f "$CS/$proj" ]; then
            echo "typespec_tests: dotnet test $proj"
            (cd $CS && dotnet test "$proj" --logger "console;verbosity=normal" -- NUnit.DisplayName=FullName) 2>&1
        fi
    done
    if has "^$CS/emitter/"; then
        echo "typespec_tests: vitest $CS/emitter"
        (cd $CS && npx vitest run -c ./emitter/vitest.config.ts --reporter=verbose) 2>&1
    fi
    if has "^$PY/tests/unit/"; then
        echo "typespec_tests: pytest $PY/tests/unit"
        (cd $PY/tests/unit && $PYENV/bin/python -m pytest . -v -rA -p no:cacheprovider --confcutdir=.) 2>&1
    fi
    if has "^$PY/emitter/"; then
        echo "typespec_tests: vitest $PY/emitter"
        (cd $PY && npx vitest run -c ./emitter/vitest.config.ts --reporter=verbose) 2>&1
    fi
    for p in $(ws_packages); do
        echo "typespec_tests: vitest packages/$p"
        (cd packages/$p && pnpm exec vitest run --reporter=verbose) 2>&1
    done
    if has "^$JV/"; then
        echo "typespec_tests: mvn test $JV/generator/http-client-generator-core"
        mvn -B -f $JV/generator/pom.xml -pl http-client-generator-core -Dspotless.skip=true -Dspotless.check.skip=true -Dspotless.apply.skip=true -Dsurefire.failIfNoSpecifiedTests=false test 2>&1
        python3 - "$JV/generator/http-client-generator-core/target/surefire-reports" <<'EOF'
import glob
import sys
import xml.etree.ElementTree as ET

for path in sorted(glob.glob(sys.argv[1] + "/TEST-*.xml")):
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.get("classname", "") + "#" + case.get("name", "")
        if case.find("failure") is not None or case.find("error") is not None:
            status = "FAILED"
        elif case.find("skipped") is not None:
            status = "SKIPPED"
        else:
            status = "PASSED"
        print(f"JUNIT {status} {name}")
EOF
    fi
}

case "$1" in
    setup) setup ;;
    run) run_tests ;;
esac
""".replace("[[REPO_DIR]]", REPO_DIR),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

bash /home/typespec_tests.sh setup

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
bash /home/typespec_tests.sh run

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/typespec_tests.sh run

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/typespec_tests.sh run

""".format(repo_dir=REPO_DIR),
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


@Instance.register("microsoft", "typespec_12339_to_11735")
class typespec_12339_to_11735(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return typespec_12339_to_11735_ImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        re_dotnet = re.compile(
            r"^\s*(Passed|Failed|Skipped)\s+(.+?)\s+\[(?:<\s*)?\d+(?:[.,]\d+)?\s*(?:ms|s|m|h)(?:\s+\d+(?:[.,]\d+)?\s*(?:ms|s|m))*\]\s*$"
        )
        re_vitest = re.compile(
            r"^\s*(✓|√|×|✗|↓)\s+(.+? > .+?)(?:\s+\d+(?:\.\d+)?\s*m?s)?\s*$"
        )
        re_vitest_fail = re.compile(r"^\s*FAIL\s+(.+? > .+?)\s*$")
        re_pytest = re.compile(
            r"^(\S+\.py::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$"
        )
        re_pytest_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(\S+\.py::.+?)(?:\s+-\s+.*)?$"
        )
        re_junit = re.compile(r"^JUNIT\s+(PASSED|FAILED|SKIPPED)\s+(\S+)\s*$")

        status_map = {
            "Passed": passed_tests,
            "Failed": failed_tests,
            "Skipped": skipped_tests,
            "PASSED": passed_tests,
            "XPASS": passed_tests,
            "FAILED": failed_tests,
            "ERROR": failed_tests,
            "SKIPPED": skipped_tests,
            "XFAIL": skipped_tests,
            "✓": passed_tests,
            "√": passed_tests,
            "×": failed_tests,
            "✗": failed_tests,
            "↓": skipped_tests,
        }

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).rstrip()
            if not line:
                continue

            m = re_dotnet.match(line)
            if m:
                status_map[m.group(1)].add(m.group(2).strip())
                continue

            m = re_junit.match(line)
            if m:
                status_map[m.group(1)].add(m.group(2).strip())
                continue

            m = re_pytest.match(line)
            if m:
                status_map[m.group(2)].add(m.group(1).strip())
                continue

            m = re_pytest_summary.match(line)
            if m:
                status_map[m.group(1)].add(m.group(2).strip())
                continue

            m = re_vitest_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_vitest.match(line)
            if m:
                status_map[m.group(1)].add(m.group(2).strip())
                continue

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("microsoft", "typespec")(typespec_12339_to_11735)
