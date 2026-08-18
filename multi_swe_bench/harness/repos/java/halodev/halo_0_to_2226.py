from __future__ import annotations

import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ──────────────────────────────────────────────────────────────
# Era: halo_0_to_2226  —  Halo 1.x (releases 1.4 / 1.5)
#
#   Toolchain : JDK 11 (Azul Zulu)
#   Build     : Gradle wrapper, single-module project
#   Node.js   : not required — the ui/ subproject does not exist
#               in this era, so no com.github.node-gradle plugin
#               and no system Node.js is needed.
#
# Verified in Docker: PR #1144 on JDK 11 compiled and executed
# 122 tests (Halo 1.x targets Java 8 source but builds cleanly on
# JDK 11), so a single JDK 11 image covers the whole 1.x era.
# ──────────────────────────────────────────────────────────────


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Binary diffs (e.g. gradle/wrapper/gradle-wrapper.jar, image assets) cause
    'cannot apply binary patch without full index line' errors with git apply,
    which aborts the whole patch atomically. These binary files are not needed
    to compile or run tests, so the section is dropped.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split("\n")
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git"):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith("diff --git"):
                if lines[i].startswith("GIT binary patch") or lines[i].startswith(
                    "Binary files"
                ):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


class Halo0To2226ImageBase(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Per-PR base (tag base-pr-<N>). The clone, the pin to ${BASE_COMMIT}
        # and the history scrub all live HERE, in the base, so the PR layer
        # stays a thin patch/script drop and no responsibility is duplicated.
        #
        # The proxy ARGs, the ENV passthrough block, the CA symlink farm and
        # the scrub/assert block are taken from harness.image rather than
        # retyped, so this file cannot drift from the canonical, security-
        # reviewed text. `# syntax` stops DockerfileEnhancer double-injecting.
        build_args = (
            "ARG TARGETARCH\n"
            f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
            "ARG BASE_COMMIT\n"
            f"\n{DockerfileEnhancer._PROXY_ARGS}"
        )

        # Docker layers are additive: a `git gc` in a later layer only writes a
        # whiteout, it cannot unwrite the full-history packfile the clone put in
        # an earlier layer — that pack still ships in the image tar and
        # `git cat-file` recovers the commit carrying the ground-truth fix. So
        # the clone RUN detaches and scrubs inline, in the same layer, and the
        # fat pack never becomes a shipped layer. The standalone D13/D14 steps
        # below still run and still assert; every command in them is idempotent.
        clone_and_prescrub = (
            "# Clone + detach + scrub in ONE layer: Docker layers are additive, so a\n"
            "# later `git gc` cannot unwrite the full-history packfile an earlier layer\n"
            "# already shipped. The D13/D14 steps below re-run idempotently and still\n"
            "# assert, on a tree this layer has already made clean.\n"
            f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} \\\n'
            f"    && cd /home/{self.pr.repo} \\\n"
            '    && git checkout --detach "${BASE_COMMIT}" \\\n'
            "    && (git remote remove origin 2>/dev/null || true) \\\n"
            "    && git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d \\\n"
            "    && git reflog expire --expire=now --all \\\n"
            "    && git reflog expire --expire-unreachable=now --all \\\n"
            "    && git gc --prune=now --aggressive \\\n"
            "    && git repack -a -d -l --quiet \\\n"
            "    && rm -f .git/objects/info/alternates"
        )
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

{build_args}

{DockerfileEnhancer._ENV_BLOCK}
ENV LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl unzip git \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://repos.azul.com/azul-repo.key -o /usr/share/keyrings/azul.asc \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.asc] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends zulu11-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu11
ENV PATH=$JAVA_HOME/bin:$PATH

{clone_and_prescrub}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}
CMD ["/bin/bash"]
"""


class Halo0To2226ImageDefault(Image):

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
        return Halo0To2226ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)
        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
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
                "init.gradle",
                '''// Injected via --init-script so build.gradle/settings.gradle stay PRISTINE —
// the dataset's test/fix patches must apply with `git apply`, which breaks if a
// prepare-time `sed` has rewritten those files. Replaces the old sed-based
// JCenter swap and adds per-test logging.
allprojects {
    afterEvaluate { p ->
        // JCenter/Bintray is decommissioned (HTTP 404). Drop it wherever declared
        // and guarantee a live mirror set (Halo 1.x needs image4j off the Aliyun
        // mirror; mavenCentral covers the rest).
        p.repositories.removeIf { r ->
            r.hasProperty('url') && r.url != null && r.url.toString().contains('bintray')
        }
        p.repositories.mavenCentral()
        p.repositories.maven { url 'https://maven.aliyun.com/repository/public' }
    }
    // Gradle prints nothing per-test by default; parse_log then sees only the
    // task-level :test and can never derive f2p/n2p. Emit one line per test.
    tasks.withType(Test).configureEach {
        testLogging {
            events 'passed', 'failed', 'skipped'
            showStandardStreams = false
            exceptionFormat = 'short'
        }
        outputs.upToDateWhen { false }
    }
}
''',
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Dead-JCenter fix + per-test logging are injected via --init-script
# /home/init.gradle, so build.gradle/settings.gradle stay pristine and the
# test/fix patches apply cleanly. Raise Gradle's heap for the warm-up build.
mkdir -p /root/.gradle
echo 'org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=1g' >> /root/.gradle/gradle.properties

chmod +x gradlew
./gradlew build -x test -x check --console=plain --no-daemon --init-script /home/init.gradle || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
./gradlew clean test --continue --console=plain --no-daemon --init-script /home/init.gradle
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./gradlew clean test --continue --console=plain --no-daemon --init-script /home/init.gradle

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew clean test --continue --console=plain --no-daemon --init-script /home/init.gradle

""".format(pr=self.pr),
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

        # The clone, the ${BASE_COMMIT} pin and the history scrub are owned by
        # the base image (base-pr-<N>) and must not be re-implemented here:
        # repeating them would duplicate a base responsibility and stack a second
        # fat git layer on top of an already-scrubbed tree.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("halo-dev", "halo_0_to_2226")
class Halo0To2226(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Halo0To2226ImageDefault(self.pr, self._config)

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

        passed_res = [
            re.compile(r"^> Task :(\S+)$"),
            re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
            re.compile(r"^> Task :(\S+) FROM-CACHE$"),
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

        compile_task_re = re.compile(
            r"^.*:(compile\w*|processResources|processTestResources|classes|testClasses|jar)"
        )

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    task_name = m.group(1)
                    if compile_task_re.match(task_name):
                        continue
                    failed_tests.add(task_name)
                    if task_name in passed_tests:
                        passed_tests.remove(task_name)

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_Halo0To2226 = [
    '1318-1321-1324-1325-1327-1328-1331-1332-1333-1334-1335-1342-1343-1345',
    '1353-1354-1373-1375-1376-1377-1379-1380-1389-1390-1396-1401-1402-1406-1410-1414-1415-1416-1426-1427',
    '1429-1430',
    '1761-1764-1766-1781-1785-1786-1787-1792-1794',
    '1797-1804-1806-1810-1811-1812-1813-1814-1815-1819-1820-1821-1822-1823-1824-1826-1827-1832',
    '1837-1860-1900-1901-1902-1903-1914-1915-1917-1935-2070-2075',
    '2226-2285-2332-2336-2407-2541',
    '1144-1147-1173-1176-1177-1184-1190-1191-1199-1203-1207-1209-1210-1212-1215-1217-1236-1237-1238-1241-1242-1246-1248-1249',
    '1273-1277-1278-1279-1282-1283-1284-1286-1287-1289-1295-1297-1298-1300-1301-1303-1304-1305',
    '1440-1445-1446-1452-1458-1469-1471-1474-1477-1479',
    '1485-1488-1492-1494',
    '2077-2094-2099-2117-2140-2146-2186-2189-2208-2216-2219',
]
for _ni in _BUNDLE_NIS_Halo0To2226:
    Instance.register('halo-dev', _ni)(Halo0To2226)
