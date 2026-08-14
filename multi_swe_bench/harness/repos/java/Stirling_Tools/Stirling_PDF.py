import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class StirlingPdfImageBase(Image):
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
        # Stirling-PDF is a Spring Boot / Gradle multi-module project targeting
        # Java 17 (sourceCompatibility=17). A JDK 21 toolchain compiles source 17
        # fine, and Gradle's foojay toolchain resolver (bundled by the project)
        # auto-provisions an exact JDK if a PR's build declares one.
        return "eclipse-temurin:21-jdk"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV GRADLE_OPTS="-Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl gnupg \\
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class StirlingPdfImageDefault(Image):
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
        return StirlingPdfImageBase(self.pr, self._config)

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
                "print_test_results.sh",
                """#!/bin/bash
# Concatenate every JUnit XML report so parse_log can extract test outcomes.
# Gradle's `test` task writes reports to <module>/build/test-results/test/TEST-*.xml.
REPO_DIR="/home/{repo}"
echo "===== BEGIN TEST RESULTS ====="
find "$REPO_DIR" \\( -path '*/build/test-results/test/TEST-*.xml' -o -path '*/frontend/vitest-junit.xml' \\) -exec cat {{}} \\; 2>/dev/null
echo ""
echo "===== END TEST RESULTS ====="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
# The shared base image is pruned to a single commit's history, so a PR whose
# base commit differs from the one that seeded the base image will be missing it
# ("fatal: unable to read tree"). Re-fetch that exact commit if it isn't present
# so the checkout works regardless of which PR built the base image.
if ! git cat-file -e {sha}^{{commit}} 2>/dev/null; then
    git remote add origin https://github.com/{org}/{repo}.git 2>/dev/null || true
    git fetch --depth 1 origin {sha}
fi
git checkout {sha}
bash /home/check_git_changes.sh

chmod +x ./gradlew || true

# Pre-warm the Gradle distribution/dependencies; hard cap so a dead plugin repo
# can't hang the build. `|| true` lets prepare succeed regardless.
export CI=true
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false"
timeout --kill-after=30 600 ./gradlew help --no-daemon || true

# Frontend (vitest) deps — only when this PR's base actually ships a frontend.
# Older PRs have no frontend/ dir, so this is skipped and the config stays a
# pure Java/Gradle build for them (their results are unchanged).
if [ -f frontend/package.json ]; then
    ( cd frontend && ( timeout 600 npm ci --no-audit --no-fund || timeout 600 npm install --no-audit --no-fund ) ) || true
fi
""".format(repo=self.pr.repo, sha=self.pr.base.sha, org=self.pr.org),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
timeout --kill-after=30 600 ./gradlew test --no-daemon --continue || true
# Frontend vitest → JUnit XML (identical format, parsed by the same parse_log).
# Only runs when the PR's base ships a frontend; a no-op otherwise.
if [ -f frontend/package.json ]; then
    ( cd frontend && timeout 900 npx vitest run --reporter=junit --outputFile=vitest-junit.xml ) || true
fi
bash /home/print_test_results.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
timeout --kill-after=30 600 ./gradlew test --no-daemon --continue || true
# Frontend vitest → JUnit XML (identical format, parsed by the same parse_log).
# Only runs when the PR's base ships a frontend; a no-op otherwise.
if [ -f frontend/package.json ]; then
    ( cd frontend && timeout 900 npx vitest run --reporter=junit --outputFile=vitest-junit.xml ) || true
fi
bash /home/print_test_results.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true; git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
timeout --kill-after=30 600 ./gradlew test --no-daemon --continue || true
# Frontend vitest → JUnit XML (identical format, parsed by the same parse_log).
# Only runs when the PR's base ships a frontend; a no-op otherwise.
if [ -f frontend/package.json ]; then
    ( cd frontend && timeout 900 npx vitest run --reporter=junit --outputFile=vitest-junit.xml ) || true
fi
bash /home/print_test_results.sh
""".format(repo=self.pr.repo),
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


@Instance.register("Stirling-Tools", "Stirling-PDF")
class StirlingPDF(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StirlingPdfImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text: str) -> str:
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # JUnit XML format produced by Gradle's `test` task under
        # <module>/build/test-results/test/TEST-*.xml:
        #   <testcase name="X" classname="Y" time=".."/>          -> pass
        #   <testcase ...><failure .../></testcase>               -> fail
        #   <testcase ...><error .../></testcase>                 -> fail
        #   <testcase ...><skipped/></testcase>                   -> skip
        # Attribute order differs by producer: Gradle/JUnit emits
        # `<testcase name=".." classname="..">` (name first) while vitest's junit
        # reporter emits `<testcase classname=".." name="..">` (classname first).
        # Match the whole tag and pull the attributes out order-independently so
        # both are captured. (A name-first-only regex silently drops every vitest
        # test.) Java results are unchanged — the same tags still match.
        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        name_re = re.compile(r'\bname="([^"]*)"')
        classname_re = re.compile(r'\bclassname="([^"]*)"')

        # Vitest (frontend) reports an import/collection failure at the FILE level
        # (name == the .test.ts path) but reports passes at the METHOD level, so a
        # test that fails-without-fix and passes-with-fix would otherwise get two
        # different ids and no transition. Collapse vitest entries to the file so
        # the file's fail->pass registers as one transition. Java/JUnit (classname
        # is a fully-qualified package.Class, never a *.test.* path) is unaffected,
        # so the Gradle-resolved instances keep identical results.
        _vitest_suffixes = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx",
                            ".test.js", ".test.jsx", ".spec.js")

        for m in testcase_re.finditer(test_log):
            attrs = m.group(1)
            nm = name_re.search(attrs)
            cn = classname_re.search(attrs)
            if not nm or not cn:
                continue
            name = nm.group(1)
            classname = cn.group(1)
            closing = m.group(2)
            inner = m.group(3) or ""
            if classname.endswith(_vitest_suffixes):
                test_id = classname
            else:
                test_id = f"{classname}.{name}"

            if closing == "/>":
                passed_tests.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed_tests.add(test_id)
            elif "<skipped" in inner:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        # A test id can appear more than once across retries/reports; resolve
        # precedence deterministically so the buckets never overlap.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
