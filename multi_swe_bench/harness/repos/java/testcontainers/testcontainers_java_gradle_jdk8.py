import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "testcontainers-java"

FILTER_SCRIPT = """\
import sys
import re

patch_file = sys.argv[1]
output_file = sys.argv[2]

with open(patch_file, 'r', errors='replace') as f:
    content = f.read()

starts = [m.start() for m in re.finditer(r'^diff --git ', content, re.MULTILINE)]
parts = []
for i, s in enumerate(starts):
    end = starts[i + 1] if i + 1 < len(starts) else len(content)
    parts.append(content[s:end])

filtered = []
for part in parts:
    if not part.strip():
        continue
    if 'GIT binary patch' in part or 'Binary files' in part:
        continue
    first_line = part.split('\\n')[0]
    if re.search(
        r'\\.(png|jpg|jpeg|gif|ico|bin|woff|woff2|eot|ttf|otf|zip|gz|tar|svg|jar|class|war|ear)$',
        first_line, re.IGNORECASE):
        continue
    filtered.append(part)

with open(output_file, 'w') as f:
    f.write(''.join(filtered))
"""

REMOVE_BINTRAY_SCRIPT = """\
#!/bin/bash
cd /home/testcontainers-java
sed -i '/com.jfrog.bintray/d' build.gradle 2>/dev/null || true
sed -i '/bintray.gradle/d' build.gradle 2>/dev/null || true
sed -i '/bintrayUpload/d' build.gradle 2>/dev/null || true
find . -name "build.gradle" -exec sed -i '/com.jfrog.bintray/d' {} \\; 2>/dev/null || true
find . -name "build.gradle" -exec sed -i '/bintray/d' {} \\; 2>/dev/null || true
"""


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
        return "ubuntu:22.04"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base_testcontainers_java_gradle_jdk8"

    def workdir(self) -> str:
        return "base_testcontainers_java_gradle_jdk8"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        bintray_fix = (
            "RUN grep -rl 'bintray' /home/" + REPO_DIR + " --include='*.gradle' 2>/dev/null"
            " | xargs -r sed -i '/com.jfrog.bintray/d;/bintray.gradle/d;/bintrayUpload/d;/bintray/d'"
            " 2>/dev/null || true"
        )

        dockerfile_content = """\
FROM {image_name}

{global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl python3 ca-certificates docker.io openjdk-8-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-8-openjdk-$TARGETARCH
ENV PATH=$JAVA_HOME/bin:$PATH

{code}

WORKDIR /home/{repo_dir}
{bintray_fix}

{clear_env}

"""
        return dockerfile_content.format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            repo_dir=REPO_DIR,
            bintray_fix=bintray_fix,
            clear_env=self.clear_env,
        )


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "filter_binary_patch.py", FILTER_SCRIPT),
            File(".", "remove_bintray.sh", REMOVE_BINTRAY_SCRIPT),
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
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
bash /home/remove_bintray.sh
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
cd /home/{repo_dir}
bash /home/remove_bintray.sh
./gradlew dependencies --no-daemon 2>/dev/null || true
export TESTCONTAINERS_RYUK_DISABLED=true
export TESTCONTAINERS_CHECKS_DISABLE=true
./gradlew :testcontainers:test --no-daemon -x javadoc 2>&1 || true
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
cd /home/{repo_dir}
bash /home/remove_bintray.sh

python3 /home/filter_binary_patch.py /home/test.patch /tmp/test_filtered.patch

if ! git apply --whitespace=nowarn /tmp/test_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/test_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/test_filtered.patch 2>/dev/null || true
    fi
fi

./gradlew dependencies --no-daemon 2>/dev/null || true
export TESTCONTAINERS_RYUK_DISABLED=true
export TESTCONTAINERS_CHECKS_DISABLE=true
./gradlew :testcontainers:test --no-daemon -x javadoc 2>&1 || true
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
cd /home/{repo_dir}
bash /home/remove_bintray.sh

python3 /home/filter_binary_patch.py /home/test.patch /tmp/test_filtered.patch
python3 /home/filter_binary_patch.py /home/fix.patch /tmp/fix_filtered.patch

if ! git apply --whitespace=nowarn /tmp/test_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/test_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/test_filtered.patch 2>/dev/null || true
    fi
fi

if ! git apply --whitespace=nowarn /tmp/fix_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/fix_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/fix_filtered.patch 2>/dev/null || true
    fi
fi

./gradlew dependencies --no-daemon 2>/dev/null || true
export TESTCONTAINERS_RYUK_DISABLED=true
export TESTCONTAINERS_CHECKS_DISABLE=true
./gradlew :testcontainers:test --no-daemon -x javadoc 2>&1 || true
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

        dockerfile_content = """\
FROM {name}:{tag}

{global_env}

{copy_commands}

{prepare_commands}

{clear_env}

"""
        return dockerfile_content.format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            prepare_commands=prepare_commands,
            clear_env=self.clear_env,
        )


@Instance.register("testcontainers", "testcontainers_java_gradle_jdk8")
class TESTCONTAINERS_JAVA_GRADLE_JDK8(Instance):
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

    def volumes(self):
        return {"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}}

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_res = [
            re.compile(r"^> Task :(\S+)$"),
            re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
            re.compile(r"^(.+ > .+) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^> Task :(\S+) FAILED$"),
            re.compile(r"^(.+ > .+) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^> Task :(\S+) SKIPPED$"),
            re.compile(r"^> Task :(\S+) NO-SOURCE$"),
            re.compile(r"^(.+ > .+) SKIPPED$"),
        ]

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
