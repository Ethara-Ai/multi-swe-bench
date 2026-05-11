import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "kestra"

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
        return "ubuntu:24.04"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base_kestra_java25"

    def workdir(self) -> str:
        return "base_kestra_java25"

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

        dockerfile_content = """\
FROM {image_name}

{global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl python3 ca-certificates openjdk-25-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-25-openjdk-$TARGETARCH
ENV PATH=$JAVA_HOME/bin:$PATH

{code}

WORKDIR /home/{repo_dir}

{clear_env}

"""
        return dockerfile_content.format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            repo_dir=REPO_DIR,
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
./gradlew classes testClasses --no-daemon --console=plain || true
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}
./gradlew test --no-daemon --console=plain --continue
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}

python3 /home/filter_binary_patch.py /home/test.patch /tmp/test_filtered.patch

if ! git apply --whitespace=nowarn /tmp/test_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/test_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/test_filtered.patch 2>/dev/null || true
    fi
fi

./gradlew test --no-daemon --console=plain --continue
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}

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

./gradlew test --no-daemon --console=plain --continue
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

        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p ~/.gradle && \\
                        if [ ! -f "$HOME/.gradle/gradle.properties" ]; then \\
                            touch "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        if ! grep -q "systemProp.http.proxyHost" "$HOME/.gradle/gradle.properties"; then \\
                            echo 'systemProp.http.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.http.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties"; \\
                        fi
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f ~/.gradle/gradle.properties
                """
                )

        dockerfile_content = """\
FROM {name}:{tag}

{global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{clear_env}

"""
        return dockerfile_content.format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            proxy_setup=proxy_setup,
            copy_commands=copy_commands,
            prepare_commands=prepare_commands,
            proxy_cleanup=proxy_cleanup,
            clear_env=self.clear_env,
        )


@Instance.register("kestra-io", "kestra_14975_to_99999")
class KESTRA_14975_TO_99999(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_res = [
            re.compile(r"^> Task :(\S+)$"),
            re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
            re.compile(r"^> Task :(\S+) FROM-CACHE$"),
            re.compile(r"^(.+ > .+) PASSED$"),
            re.compile(r"^\s+Test (.+?) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^> Task :(\S+) FAILED$"),
            re.compile(r"^(.+ > .+) FAILED$"),
            re.compile(r"^\s+Test (.+?) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^> Task :(\S+) SKIPPED$"),
            re.compile(r"^> Task :(\S+) NO-SOURCE$"),
            re.compile(r"^(.+ > .+) SKIPPED$"),
            re.compile(r"^\s+Test (.+?) SKIPPED$"),
        ]

        for line in clean_log.splitlines():
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

        # Deduplicate: remove overlaps
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
