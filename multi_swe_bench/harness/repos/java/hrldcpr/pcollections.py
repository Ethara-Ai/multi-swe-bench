import re
import xml.etree.ElementTree as ET
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MAVEN_IMAGE = "maven:3.9-eclipse-temurin-8"
_BASE_TAG = "base"

_SUITE = re.compile(r"<testsuite\b.*?</testsuite>", re.S)
_SYNTHETIC = re.compile(r"^SYNTHETIC_FAIL (\S+)$", re.M)

_RUN_TESTS_BODY = r"""rm -rf target/surefire-reports
mvn -o -B -DskipTests test-compile > /tmp/compile.log 2>&1
echo "COMPILE_EXIT=$?"
tail -20 /tmp/compile.log
CLASSES=$(find src/test -name '*Test.java' | sed 's#^src/test/java/##; s#/#.#g; s#\.java$##' | sort -u)
for cls in $CLASSES; do
  mvn -o -B surefire:test -Dtest="$cls" -DfailIfNoTests=false > "/tmp/mvn-$cls.log" 2>&1
  xml="target/surefire-reports/TEST-$cls.xml"
  if [ -f "$xml" ]; then
    echo "=== SUREFIRE XML $cls"
    cat "$xml"
    echo
  else
    echo "=== SUREFIRE MISSING $cls"
    src="src/test/java/$(echo "$cls" | tr . /).java"
    grep -oE 'public void test[A-Za-z0-9_]*' "$src" 2>/dev/null | awk '{print "SYNTHETIC_FAIL " $3}'
    tail -15 "/tmp/mvn-$cls.log"
  fi
done
"""


def _run_tests_sh(repo: str) -> str:
    return "#!/bin/bash\n" f"cd /home/{repo}\n" + _RUN_TESTS_BODY


class PcollectionsImageBase(Image):
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
        return _MAVEN_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        infra = DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n")
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class PcollectionsImageDefault(Image):
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
        return PcollectionsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "git rev-parse --is-inside-work-tree > /dev/null\n"
            "git status --porcelain\n"
            'test -z "$(git status --porcelain)"\n'
            'echo "check_git_changes: No uncommitted changes"\n'
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            'git cat-file -e "${BASE_COMMIT}" 2>/dev/null || git fetch --no-tags origin "${BASE_COMMIT}"\n'
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "mvn -B test\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "bash /home/run_tests.sh\n"
        )

        test_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            "bash /home/run_tests.sh\n"
        )

        fix_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            "bash /home/run_tests.sh\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
            File(".", "run_tests.sh", _run_tests_sh(repo)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {image.image_full_name()}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("hrldcpr", "pcollections")
class Pcollections(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PcollectionsImageDefault(self.pr, self._config)

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

        for block in _SUITE.findall(test_log):
            try:
                root = ET.fromstring(block)
            except ET.ParseError:
                continue
            suite = root.get("name") or ""
            for case in root.iter("testcase"):
                name = case.get("name")
                if not name:
                    continue
                ident = f"mvn::{suite}::{name}"
                if case.find("failure") is not None or case.find("error") is not None:
                    failed_tests.add(ident)
                elif case.find("skipped") is not None:
                    skipped_tests.add(ident)
                else:
                    passed_tests.add(ident)

        for section in test_log.split("=== SUREFIRE MISSING ")[1:]:
            cls = section.split("\n", 1)[0].strip()
            body = section.split("=== SUREFIRE", 1)[0]
            for method in _SYNTHETIC.findall(body):
                failed_tests.add(f"mvn::{cls}::{method}")

        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
