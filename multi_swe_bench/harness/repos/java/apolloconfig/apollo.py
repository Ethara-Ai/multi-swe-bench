import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Older Apollo PRs (v0.5–v0.6 era) target Java 8 and use ServiceLoader patterns
# that hit JPMS module-access restrictions on Java 11. Pin those to Java 8.
JAVA8_PR_NUMBERS = {542, 547, 589, 612, 642}


class ApolloImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def _use_java8(self) -> bool:
        return self._pr.number in JAVA8_PR_NUMBERS

    def dependency(self) -> Union[str, "Image"]:
        return "eclipse-temurin:8" if self._use_java8() else "eclipse-temurin:11"

    def image_tag(self) -> str:
        suffix = "java8" if self._use_java8() else "java11"
        return f"base-{suffix}"

    def workdir(self) -> str:
        suffix = "java8" if self._use_java8() else "java11"
        return f"base-{suffix}"

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

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        maven_mirror = (
            "RUN mkdir -p ~/.m2 && cat > ~/.m2/settings.xml <<'MAVENEOF'\n"
            '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"\n'
            '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">\n'
            "    <mirrors>\n"
            "        <mirror>\n"
            "            <id>aliyunmaven</id>\n"
            "            <mirrorOf>central</mirrorOf>\n"
            "            <name>Aliyun Maven Mirror</name>\n"
            "            <url>https://maven.aliyun.com/repository/public</url>\n"
            "        </mirror>\n"
            "    </mirrors>\n"
            "</settings>\n"
            "MAVENEOF"
        )

        # Hardening for the shared base image anchors at HEAD (not a PR-specific
        # BASE_COMMIT). This preserves the full git history so every PR image can
        # git-checkout its own base SHA in prepare.sh, regardless of era.
        base_hardening = (
            "RUN set -eux; \\\n"
            "    git checkout --detach HEAD; \\\n"
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all; \\\n"
            "    git reflog expire --expire-unreachable=now --all; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            "    git config --local remote.pushDefault \"\"; \\\n"
            "    test -z \"$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)\"; \\\n"
            "    test -z \"$(git remote)\""
        )

        base_hardening_submodules = (
            "RUN if [ -f .gitmodules ]; then \\\n"
            "        git submodule foreach --recursive ' \\\n"
            "            git checkout --detach HEAD; \\\n"
            "            git remote remove origin 2>/dev/null || true; \\\n"
            "            git for-each-ref --format=\"%(refname)\" refs/heads refs/remotes refs/tags refs/replace \\\n"
            "                | xargs -r -n1 git update-ref -d; \\\n"
            "            git reflog expire --expire=now --all; \\\n"
            "            git reflog expire --expire-unreachable=now --all; \\\n"
            "            git gc --prune=now; \\\n"
            "            rm -f .git/objects/info/alternates; \\\n"
            "        '; \\\n"
            "    fi"
        )

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT"
            ),
            "ENV DEBIAN_FRONTEND=noninteractive \\\n    LANG=C.UTF-8 \\\n    TZ=UTC",
            label,
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends git maven && rm -rf /var/lib/apt/lists/*",
            code,
            maven_mirror,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN mvn install -DskipTests || true",
            base_hardening,
            base_hardening_submodules,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class ApolloImageDefault(Image):
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
        return ApolloImageBase(self.pr, self._config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout {pr.base.sha}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if [ ! -f ~/.m2/settings.xml ]; then
    mkdir -p ~/.m2 && cat <<EOF > ~/.m2/settings.xml
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">

    <mirrors>
        <mirror>
            <id>aliyunmaven</id>
            <mirrorOf>central</mirrorOf>
            <name>Aliyun Maven Mirror</name>
            <url>https://maven.aliyun.com/repository/public</url>
        </mirror>
    </mirrors>

</settings>
EOF
else
  grep -q "<mirror>" ~/.m2/settings.xml || sed -i '/<\\/settings>/i \\
  <mirrors> \\
      <mirror> \\
          <id>aliyunmaven</id> \\
          <mirrorOf>central</mirrorOf> \\
          <name>Aliyun Maven Mirror</name> \\
          <url>https://maven.aliyun.com/repository/public</url> \\
      </mirror> \\
  </mirrors>' ~/.m2/settings.xml
fi
mvn clean test -Dstyle.color=never || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
mvn clean test -Dstyle.color=never
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
mvn clean test -Dstyle.color=never

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn \
  --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' \
  --exclude='*.gif' --exclude='*.svg' --exclude='*.ico' \
  /home/test.patch /home/fix.patch
mvn clean test -Dstyle.color=never

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        file_names = " ".join(file.name for file in self.files())
        copy_command = f"COPY {file_names} /home/"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_command}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

CMD ["/bin/bash"]
"""


@Instance.register("apolloconfig", "5333-5566")
class APOLLO_2PR(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ApolloImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        re_result = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: .+ - in (.+)"
        )

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.split("\n"):
            result_match = re_result.search(line)
            if result_match:
                failures = int(result_match.group(2))
                errors = int(result_match.group(3))
                test_name = result_match.group(5)

                if failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                else:
                    passed_tests.add(test_name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
