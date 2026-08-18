import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ArchUnitImageBase(Image):
    """Repo-level base: JDK + git + clone. ArchUnit is a multi-module Gradle build (Gradle 7.5,
    JUnit 5). The graded tests live in the `:archunit` module; we scope the test task to the 3
    touched classes to keep the run fast."""

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
        return "eclipse-temurin:17-jdk"

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
ENV GRADLE_OPTS="-Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"
ENV CI=true
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /root/.gradle && echo 'ZGVmIE1JUlJPUiA9ICdodHRwczovL21hdmVuLWNlbnRyYWwuc3RvcmFnZS1kb3dubG9hZC5nb29nbGVhcGlzLmNvbS9tYXZlbjIvJwpkZWYgaXNUaHJvdHRsZWQgPSB7IHUgLT4gdSAhPSBudWxsICYmICh1LmNvbnRhaW5zKCdyZXBvLm1hdmVuLmFwYWNoZS5vcmcnKSB8fCB1LmNvbnRhaW5zKCdyZXBvMS5tYXZlbi5vcmcnKSkgfQpkZWYgcmV3cml0ZSA9IHsgcmVwb3MgLT4KICAgIHJlcG9zLmFsbCB7IHIgLT4KICAgICAgICB0cnkgeyBpZiAoci5oYXNQcm9wZXJ0eSgndXJsJykgJiYgaXNUaHJvdHRsZWQoci51cmw/LnRvU3RyaW5nKCkpKSByLnVybCA9IHVyaShNSVJST1IpIH0gY2F0Y2ggKGlnbm9yZWQpIHt9CiAgICB9Cn0KZ3JhZGxlLnNldHRpbmdzRXZhbHVhdGVkIHsgcyAtPgogICAgdHJ5IHsgcy5wbHVnaW5NYW5hZ2VtZW50LnJlcG9zaXRvcmllcyB7IGdyYWRsZVBsdWdpblBvcnRhbCgpOyBtYXZlbiB7IHVybCBNSVJST1IgfSB9IH0gY2F0Y2ggKGlnbm9yZWQpIHt9CiAgICB0cnkgeyByZXdyaXRlKHMucGx1Z2luTWFuYWdlbWVudC5yZXBvc2l0b3JpZXMpIH0gY2F0Y2ggKGlnbm9yZWQpIHt9CiAgICB0cnkgeyByZXdyaXRlKHMuZGVwZW5kZW5jeVJlc29sdXRpb25NYW5hZ2VtZW50LnJlcG9zaXRvcmllcykgfSBjYXRjaCAoaWdub3JlZCkge30KICAgIHRyeSB7IHMuZGVwZW5kZW5jeVJlc29sdXRpb25NYW5hZ2VtZW50LnJlcG9zaXRvcmllcyB7IG1hdmVuIHsgdXJsIE1JUlJPUiB9IH0gfSBjYXRjaCAoaWdub3JlZCkge30KfQpncmFkbGUuYWxscHJvamVjdHMgeyBwIC0+CiAgICByZXdyaXRlKHAucmVwb3NpdG9yaWVzKQogICAgdHJ5IHsgcmV3cml0ZShwLmJ1aWxkc2NyaXB0LnJlcG9zaXRvcmllcykgfSBjYXRjaCAoaWdub3JlZCkge30KfQo=' | base64 -d > /root/.gradle/init.gradle

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
RUN chmod +x ./gradlew || true
# pre-warm the Gradle distribution + dependency cache (hard cap so a dead repo can't hang)
RUN timeout --kill-after=30 900 ./gradlew :archunit:compileTestJava --no-daemon --configure-on-demand \\
      -Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class ArchUnitImageDefault(Image):
    """PR-specific image: FROM the repo base, add only patches + run scripts."""

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
        return ArchUnitImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Scope to :archunit + the 3 touched test classes, then dump the JUnit XML so parse_log
        # sees per-test outcomes. --continue so a failing class doesn't abort the others.
        test_cmd = (
            "cd /home/{repo}\n"
            "timeout --kill-after=30 1200 ./gradlew :archunit:test "
            "--tests 'com.tngtech.archunit.lang.EvaluationResultTest' "
            "--tests 'com.tngtech.archunit.lang.ArchRuleTest' "
            "--tests 'com.tngtech.archunit.library.freeze.FreezingArchRuleTest' "
            "--no-daemon --continue --configure-on-demand "
            "-Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false || true\n"
            "echo '===== BEGIN TEST RESULTS ====='\n"
            "find /home/{repo} -path '*/build/test-results/test/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null\n"
            "echo '===== END TEST RESULTS ====='"
        ).format(repo=self.pr.repo)
        apply_test = "git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true; find . -name '*.rej' -delete 2>/dev/null || true"
        apply_fix = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch; git apply --whitespace=nowarn --reject /home/fix.patch; find . -name '*.rej' -delete; }} 2>/dev/null || true"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
git reset --hard >/dev/null 2>&1 || true
git checkout {sha}
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{self.pr.repo}
{apply_test}
{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{self.pr.repo}
{apply_fix}
{test_cmd}
""",
            ),
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


def _junit_xml_parse(test_log: str) -> TestResult:
    """Shared JUnit-XML parser (Gradle build/test-results + Maven surefire-reports)."""
    clean = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    name_re = re.compile(r'\bname="([^"]*)"')
    classname_re = re.compile(r'\bclassname="([^"]*)"')
    for m in testcase_re.finditer(clean):
        attrs = m.group(1)
        nm = name_re.search(attrs)
        cn = classname_re.search(attrs)
        if not nm or not cn:
            continue
        test_id = f"{cn.group(1)}.{nm.group(1)}"
        closing = m.group(2)
        inner = m.group(3) or ""
        if closing == "/>":
            passed.add(test_id)
        elif "<failure" in inner or "<error" in inner:
            failed.add(test_id)
        elif "<skipped" in inner:
            skipped.add(test_id)
        else:
            passed.add(test_id)
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


@Instance.register("TNG", "ArchUnit")
class ARCHUNIT(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ArchUnitImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _junit_xml_parse(test_log)
