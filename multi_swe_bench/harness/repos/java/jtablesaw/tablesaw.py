from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JDK_IMAGE = "maven:3.8.8-eclipse-temurin-8-focal"
_MVN = "mvn -B -o --no-transfer-progress -fae"
_SUREFIRE_FLAGS = "-Dmaven.test.failure.ignore=true -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false -Dsurefire.useFile=false"
_DEACTIVATE = "-Djunit.jupiter.conditions.deactivate='org.junit.*DisabledCondition'"
_JSPLOT_TESTS = "-Dtest='tech/tablesaw/plotly/**/*.java,tech/tablesaw/components/**/*.java'"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_MARKER_RE = re.compile(r"^##### FILE: (\S+)[ \t]*$", re.M)
_MODULE_RE = re.compile(r"^(?:\./)?(.*?)/?target/surefire-reports/")
_TESTCASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_NAME_RE = re.compile(r'\bname="([^"]*)"')
_CLASSNAME_RE = re.compile(r'\bclassname="([^"]*)"')


def _test_block(repo: str) -> str:
    return f"""find . -type d -name surefire-reports -prune -exec rm -rf {{}} +
clean=clean
for attempt in 1 2 3 4 5; do
    compile_rc=0
    {_MVN} $clean test-compile > /home/compile.log 2>&1 || compile_rc=$?
    cat /home/compile.log
    [ "$compile_rc" -eq 0 ] && break
    bad=$( {{ grep -oE '^\\[ERROR\\] /home/{repo}/[^ :]*/src/test/java/[^ :]+\\.java:\\[[^]]*\\]' /home/compile.log || true; }} | sed -e 's|^\\[ERROR\\] ||' -e 's|:\\[[^]]*\\]$||' | sort -u)
    [ -n "$bad" ] || break
    printf '%s\\n' "$bad" | while IFS= read -r f; do
        echo "QUARANTINE attempt $attempt: ${{f#/home/{repo}/}}"
        rm -f "$f"
    done
    clean=""
done
rc=0
{_MVN} test {_SUREFIRE_FLAGS} || rc=$?
{_MVN} test {_SUREFIRE_FLAGS} {_DEACTIVATE} {_JSPLOT_TESTS} || rc=$?
echo "===== BEGIN TEST RESULTS ====="
find . -path '*/target/surefire-reports/TEST-*.xml' -print0 | sort -z | while IFS= read -r -d '' f; do
    echo "##### FILE: ${{f#./}}"
    cat "$f"
    echo
done
echo "===== END TEST RESULTS ====="
if [ "$rc" -ne 0 ] && [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)" ]; then
    echo "FATAL: maven exited $rc and produced no surefire reports" >&2
    exit "$rc"
fi
exit 0"""


class TablesawImageBase(Image):
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
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

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
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

ENV MAVEN_OPTS="-Xmx1g"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class TablesawImageDefault(Image):
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
        return TablesawImageBase(self.pr, self.config)

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
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

java -version
mvn -version

mvn -B --no-transfer-progress -fae clean test -Dmaven.test.failure.ignore=true -DfailIfNoTests=false -Dsurefire.useFile=false || {{ echo "prepare.sh: maven could not build the reactor at the base commit"; exit 1; }}
git checkout -- .
git clean -fdq
bash /home/check_git_changes.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
{_test_block(self.pr.repo)}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
{_test_block(self.pr.repo)}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_test_block(self.pr.repo)}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "TablesawImageDefault dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("jtablesaw", "tablesaw")
class TABLESAW(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TablesawImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        chunks = _FILE_MARKER_RE.split(_ANSI_RE.sub("", test_log))[1:]
        for report_path, body in zip(chunks[0::2], chunks[1::2]):
            module_match = _MODULE_RE.match(report_path)
            module = module_match.group(1) if module_match else ""
            for case in _TESTCASE_RE.finditer(body):
                attrs = case.group(1) or ""
                name = _NAME_RE.search(attrs)
                classname = _CLASSNAME_RE.search(attrs)
                if not name or not classname:
                    continue
                rel = classname.group(1).split("$", 1)[0].replace(".", "/") + ".java"
                source = f"{module}/src/test/java/{rel}" if module else f"src/test/java/{rel}"
                test_id = f"{source}::{name.group(1)}"
                inner = case.group(3) or ""
                if case.group(2) == "/>":
                    passed.add(test_id)
                elif "<failure" in inner or "<error" in inner:
                    failed.add(test_id)
                elif "<skipped" in inner:
                    skipped.add(test_id)
                else:
                    passed.add(test_id)

        passed -= failed
        skipped -= failed
        skipped -= passed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )

