from __future__ import annotations

import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ──────────────────────────────────────────────────────────────
# Era: halo_7424_to_99999  —  Halo 2.21 and newer
#
#   Toolchain : JDK 21 (Azul Zulu)
#               From release 2.21 application/build.gradle pins
#               a Gradle toolchain of
#               languageVersion = JavaLanguageVersion.of(21),
#               so JDK 21 is mandatory (JDK 17 cannot satisfy it).
#   Build     : Gradle wrapper, multi-module project
#   Node.js   : Node 22 + corepack REQUIRED.
#               The ui/ subproject runs :ui:pnpmSetup while
#               :ui:nodeSetup is SKIPPED, so a system Node.js must
#               be present; the corepack-managed pnpm needs
#               Node >= 22.13.
#
# Verified in Docker: PR #7489 (2.21) and #9921 (2.24) build with
# JDK 21 + Node 22 + corepack (:ui:pnpmSetup succeeds).
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


class Halo7424To99999ImageBase(Image):

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

<<<<<<< Updated upstream
{DockerfileEnhancer._ENV_BLOCK}
ENV LC_ALL=C.UTF-8
=======
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}
>>>>>>> Stashed changes

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl unzip git \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN curl -fsSL https://repos.azul.com/azul-repo.key -o /usr/share/keyrings/azul.asc \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.asc] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends zulu21-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu21
ENV PATH=$JAVA_HOME/bin:$PATH

# Node 22 — required by the ui/ subproject (:ui:pnpmSetup). Installed from the
# official tarball (pinned, reproducible; avoids piping a remote script to a
# shell). Arch detected at build time, so it works on amd64 and arm64.
RUN NODE_VERSION=22.22.3 \\
    && ARCH="$(dpkg --print-architecture)" \\
    && if [ "$ARCH" = "amd64" ]; then NODE_ARCH=x64; else NODE_ARCH=arm64; fi \\
    && curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$NODE_ARCH.tar.gz" -o /tmp/node.tar.gz \\
    && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \\
    && rm /tmp/node.tar.gz \\
    && corepack enable

{clone_and_prescrub}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}
CMD ["/bin/bash"]
"""


class Halo7424To99999ImageDefault(Image):

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
        return Halo7424To99999ImageBase(self.pr, self._config)

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
// JCenter removal and adds per-test logging.
allprojects {
    afterEvaluate { p ->
        // JCenter/Bintray is decommissioned (HTTP 404). Drop it wherever declared
        // and guarantee a live mirror set for every project.
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
# test/fix patches apply cleanly. Raise the Gradle JVM heap high — Halo 2.20+
# reactive integration tests OOM'd at 4g ('Java heap space'). Persists into the
# PR image layer so run/test/fix all inherit it.
mkdir -p /root/.gradle
echo 'org.gradle.jvmargs=-Xmx8g -XX:MaxMetaspaceSize=1g' >> /root/.gradle/gradle.properties

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


@Instance.register("halo-dev", "halo_7424_to_99999")
class Halo7424To99999(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Halo7424To99999ImageDefault(self.pr, self._config)

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
_BUNDLE_NIS_Halo7424To99999 = [
    '7781-7917-7918-7919-7920-7921',
    '8411-8418-8419-8423',
    '8502-8503-9860-9870-9876-9877-9879',
    '7489-7586-7587-7588-7589-7591-7592-7594',
    '7558-7559-7563-7564-7565-7568-7572-7573',
    '7582-7595-7596-7597-7598-7599-7600-7601-7602-7604-7606-7608-7613-7614',
    '7616-7617-7619-7626-7628-7630-7631-7632-7634-7635-7640-7642-7643-7646-7647',
    '7657-7658-7665-7667-7668-7670-7673',
    '7674-7677-7678-7679-7681-7682-7683-7684-7685-7687-7688-7689-7695-7700-7703-7704-7705',
    '7711-7715-7725-7726-7738-7743-7744-7745-7746',
    '8095-8097-8098-8109-8110-8111-8113',
    '8104-8146-8179-8181-8182-8187-8189-8190-8191-8193-8198-8199-8201-8202-8203-8204',
    '8126-8128-8129',
    '8138-8142-8143-8145-8153-8154',
    '8155-8158-8159-8160-8163-8165-8166-8168-8169',
    '8215-8216-8225-8226-8227-8228-8229-8230-8233-8236-8237',
    '8240-8242-8244-8246-8249-8250-8251-8253-8255-8256',
    '8413-8431-8434-8436-8485-8487-8488-8490-8492-8495-8497-8498',
    '9888-9895-9897-9899-9904-9914-9915-9918-9919-9920',
    '9921-9924-9928-9930-9931-9932',
]
for _ni in _BUNDLE_NIS_Halo7424To99999:
    Instance.register('halo-dev', _ni)(Halo7424To99999)
