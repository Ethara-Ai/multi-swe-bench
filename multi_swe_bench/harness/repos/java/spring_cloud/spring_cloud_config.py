import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MVN_BASE = "maven:3.9-eclipse-temurin-17"

# Derive the target module (first path segment) and test classes (from *Tests.java
# / *Test.java in the test.patch) and run scoped `mvn test`. -am builds upstream
# modules the target depends on. failIfNoTests=false so a stage with no matching
# test (new file absent at base) doesn't hard-error.
_TARGETS = (
    "MODULE=$(grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null | sed 's#^+++ b/##' "
    "| grep -E '\\.java$' | head -1 | cut -d/ -f1)\n"
    '[ -z "$MODULE" ] && MODULE=.\n'
    "CLASSES=$(grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null | sed 's#^+++ b/##' "
    "| grep -E '(Test|Tests|IT)\\.java$' | sed -E 's#.*/##; s#\\.java$##' | sort -u | paste -sd, -)\n"
    '[ -z "$CLASSES" ] && CLASSES="*Tests"\n'
    'echo "SCOPED MODULE=$MODULE CLASSES=$CLASSES"\n'
)

_RESET = "git reset --hard\ngit clean -fdq\n"

# emit per-testcase PASS/FAIL/SKIP from surefire XML so parse_log gets method names
_EMIT = r"""
python3 - <<'PYEOF'
import glob, xml.etree.ElementTree as ET
for f in glob.glob("**/surefire-reports/TEST-*.xml", recursive=True):
    try: root = ET.parse(f).getroot()
    except Exception: continue
    for tc in root.iter("testcase"):
        name = tc.get("classname","") + "#" + tc.get("name","")
        child = [c.tag for c in tc]
        if "failure" in child or "error" in child: print("SUREFIRE FAILED " + name)
        elif "skipped" in child: print("SUREFIRE SKIPPED " + name)
        else: print("SUREFIRE PASSED " + name)
PYEOF
"""

_MVN = (
    "find . -path '*/surefire-reports/TEST-*.xml' -delete 2>/dev/null || true\n"
    "mvn -q -B -ntp -pl $MODULE -am -Dtest=\"$CLASSES\" -DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false -Dmaven.test.failure.ignore=true "
    "-Ddisable.checks=true -Dcheckstyle.skip=true -Dspring-javaformat.skip=true -Denforcer.skip=true test 2>&1 || true\n"
    + _EMIT
)


def _apply_one(patch: str) -> str:
    return (
        f"git apply --whitespace=nowarn {patch} 2>/dev/null "
        f"|| git apply --3way --whitespace=nowarn {patch} 2>/dev/null "
        f"|| git apply --whitespace=nowarn --include='*.java' {patch} 2>/dev/null || true\n"
    )


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
        return _MVN_BASE

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
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=5 update && apt-get install -y --no-install-recommends git python3 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}
WORKDIR /home/{repo}

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

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo}\n"
                f"git cat-file -e {self.pr.base.sha}^{{commit}} 2>/dev/null \\\n"
                f"    || git fetch --no-tags --depth=2147483647 origin {self.pr.base.sha} \\\n"
                f'    || git fetch --no-tags origin "+refs/pull/{self.pr.number}/head:refs/remotes/origin/pr-{self.pr.number}"\n'
                "git reset --hard\n"
                "git clean -fdq\n"
                f"git checkout --detach {self.pr.base.sha}\n"
                "git clean -fdq\n"
                # warm the local maven cache (deps) so graded stages are offline-ish
                "MODULE=$(grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null | sed 's#^+++ b/##' | grep -E '\\.java$' | head -1 | cut -d/ -f1)\n"
                '[ -z "$MODULE" ] && MODULE=.\n'
                "mvn -q -B -ntp -pl $MODULE -am -DskipTests -Ddisable.checks=true -Dcheckstyle.skip=true -Dspring-javaformat.skip=true -Denforcer.skip=true install 2>&1 | tail -20 || true\n",
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\nset -uo pipefail\n" f"cd /home/{repo}\n" + _RESET + _TARGETS + _MVN,
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\nset -uo pipefail\n" f"cd /home/{repo}\n" + _RESET
                + _apply_one("/home/test.patch") + _TARGETS + _MVN,
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\nset -uo pipefail\n" f"cd /home/{repo}\n" + _RESET
                + _apply_one("/home/test.patch") + _apply_one("/home/fix.patch") + _TARGETS + _MVN,
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency().image_full_name()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("spring-cloud", "spring-cloud-config")
class SpringCloudConfig(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        res = re.compile(r"^SUREFIRE (PASSED|FAILED|SKIPPED) (\S+)")
        for line in test_log.splitlines():
            m = res.match(line.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASSED":
                if name not in failed_tests:
                    passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
                passed_tests.discard(name)
            else:
                skipped_tests.add(name)
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
