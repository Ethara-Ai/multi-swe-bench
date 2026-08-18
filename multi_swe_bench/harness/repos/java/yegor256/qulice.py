import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _junit_xml_parse(test_log: str) -> TestResult:
    clean = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    name_re = re.compile(r'\bname="([^"]*)"')
    classname_re = re.compile(r'\bclassname="([^"]*)"')
    for m in testcase_re.finditer(clean):
        nm = name_re.search(m.group(1))
        cn = classname_re.search(m.group(1))
        if not nm or not cn:
            continue
        tid = f"{cn.group(1)}.{nm.group(1)}"
        inner = m.group(3) or ""
        if m.group(2) == "/>":
            passed.add(tid)
        elif "<failure" in inner or "<error" in inner:
            failed.add(tid)
        elif "<skipped" in inner:
            skipped.add(tid)
        else:
            passed.add(tid)
    failed -= passed
    skipped -= passed
    skipped -= failed
    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


class QuliceImageBase(Image):
    """Repo-level base: Maven + JDK. qulice is a Maven multi-module project; the graded tests are
    in the `qulice-checkstyle` module (ChecksTest / CheckstyleValidatorTest / RequiredJavaDocTagTest).
    We build the reactor for that module (-am) and skip qulice's own self-lint so a style nit in the
    codebase doesn't mask the JUnit outcome."""

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
        return "maven:3.9-eclipse-temurin-17"

    def image_prefix(self) -> str:
        return "envagent"

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
        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /root/.m2 && echo 'PHNldHRpbmdzPgogIDxtaXJyb3JzPgogICAgPG1pcnJvcj4KICAgICAgPGlkPmdvb2dsZS1jZW50cmFsPC9pZD4KICAgICAgPG5hbWU+R29vZ2xlIE1hdmVuIENlbnRyYWwgbWlycm9yPC9uYW1lPgogICAgICA8dXJsPmh0dHBzOi8vbWF2ZW4tY2VudHJhbC5zdG9yYWdlLWRvd25sb2FkLmdvb2dsZWFwaXMuY29tL21hdmVuMi88L3VybD4KICAgICAgPG1pcnJvck9mPmNlbnRyYWw8L21pcnJvck9mPgogICAgPC9taXJyb3I+CiAgPC9taXJyb3JzPgo8L3NldHRpbmdzPgo=' | base64 -d > /root/.m2/settings.xml

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
# pre-warm: resolve deps + compile the checkstyle module reactor (skip self-lint, tests)
RUN timeout --kill-after=30 1500 mvn -B -pl qulice-checkstyle -am install \\
      -DskipTests -Dqulice.skip=true -Dcheckstyle.skip=true -Dpmd.skip=true -Dspotbugs.skip=true \\
      -Denforcer.skip=true -Dlicense.skip=true || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class QuliceImageDefault(Image):
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
        return QuliceImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Scope surefire to the 3 touched test classes in qulice-checkstyle. Skip qulice's own
        # checks so the JUnit outcome (not a style nit) is what's graded. Surefire writes JUnit
        # XML to target/surefire-reports/TEST-*.xml (same format the parser handles).
        test_cmd = (
            "cd /home/{repo}\n"
            "timeout --kill-after=30 1800 mvn -B -pl qulice-checkstyle test "
            "-Dtest='ChecksTest,CheckstyleValidatorTest,RequiredJavaDocTagTest' "
            "-DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.test.failure.ignore=true "
            "-Dqulice.skip=true -Dcheckstyle.skip=true -Dpmd.skip=true -Dspotbugs.skip=true "
            "-Denforcer.skip=true -Dlicense.skip=true || true\n"
            "echo '===== BEGIN TEST RESULTS ====='\n"
            "find /home/{repo} -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null\n"
            "echo '===== END TEST RESULTS ====='"
        ).format(repo=self.pr.repo)
        apply_test = "git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true; find . -name '*.rej' -delete 2>/dev/null || true"
        apply_fix = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch; git apply --whitespace=nowarn --reject /home/fix.patch; find . -name '*.rej' -delete; }} 2>/dev/null || true"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", """#!/bin/bash
set -e
cd /home/{repo}
git reset --hard >/dev/null 2>&1 || true
git checkout {sha}
""".format(repo=self.pr.repo, sha=self.pr.base.sha)),
            File(".", "run.sh", f"""#!/bin/bash
{test_cmd}
"""),
            File(".", "test-run.sh", f"""#!/bin/bash
cd /home/{self.pr.repo}
{apply_test}
{test_cmd}
"""),
            File(".", "fix-run.sh", f"""#!/bin/bash
cd /home/{self.pr.repo}
{apply_fix}
{test_cmd}
"""),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("yegor256", "qulice")
class QULICE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return QuliceImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _junit_xml_parse(test_log)
