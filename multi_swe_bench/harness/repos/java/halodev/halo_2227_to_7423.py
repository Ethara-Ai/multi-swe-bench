from __future__ import annotations

import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ──────────────────────────────────────────────────────────────
# Era: halo_2227_to_7423  —  Halo 2.0 – 2.20
#
#   Toolchain : JDK 17 (Azul Zulu)
#               application/build.gradle pins
#               sourceCompatibility = JavaVersion.VERSION_17
#               through release 2.20.
#   Build     : Gradle wrapper, multi-module project
#   Node.js   : Node 22 + corepack REQUIRED.
#               From the 2.x line onwards the ui/ subproject runs
#               :ui:pnpmSetup, and :ui:nodeSetup is SKIPPED — the
#               node-gradle plugin no longer auto-provisions Node,
#               so a system Node.js must be present. pnpm pulled
#               by corepack needs Node >= 22.13, hence Node 22.
#
# Verified in Docker: PR #2887 (2.0) builds on JDK 17 with no ui/
# subproject; later 2.x PRs (e.g. #6324) fail :ui:pnpmSetup unless
# Node 22 + corepack is available.
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


class Halo2227To7423ImageBase(Image):

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

        repo = self.pr.repo
        org = self.pr.org

        # Per-PR base (tag base-pr-<N>). The clone, the pin to ${BASE_COMMIT} and
        # the history scrub all live HERE, in the base, so the PR layer stays a
        # thin patch/script drop and no responsibility is implemented twice.
        #
        # The proxy ARGs, the ENV passthrough block, the CA symlink farm and the
        # scrub/assert block are taken from harness.image rather than retyped, so
        # this file cannot drift from the canonical, security-reviewed text.
        # `# syntax` keeps DockerfileEnhancer from injecting a second copy.
        build_args = (
            "ARG TARGETARCH\n"
            f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
            "ARG BASE_COMMIT\n"
            f"\n{DockerfileEnhancer._PROXY_ARGS}"
        )

        # Docker layers are additive: a `git gc` in a later layer only writes a
        # whiteout, it cannot unwrite the 91 MB full-history packfile the clone
        # put in an earlier layer — that packfile still ships in the image tar and
        # `git cat-file` on it recovers the merge commit carrying the ground-truth
        # fix. So the clone RUN detaches and scrubs inline, in the same layer, and
        # the fat pack never becomes a shipped layer at all. The standalone D13/D14
        # steps below still run (and still assert) on the already-clean tree; every
        # command in them is idempotent, so running twice is harmless.
        clone_and_prescrub = (
            "# Clone + detach + scrub in ONE layer: Docker layers are additive, so a\n"
            "# later `git gc` cannot unwrite the full-history packfile an earlier layer\n"
            "# already shipped. The D13/D14 steps below re-run idempotently and still\n"
            "# assert, on a tree this layer has already made clean.\n"
            f'RUN git clone "${{REPO_URL}}" /home/{repo} \\\n'
            f"    && cd /home/{repo} \\\n"
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

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
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
RUN apt-get update && apt-get install -y --no-install-recommends zulu17-jdk \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/zulu17
ENV PATH=$JAVA_HOME/bin:$PATH

# Node 22 — required by the ui/ subproject (:ui:pnpmSetup). Installed from the
# official tarball (pinned, reproducible). Arch detected at build time.
RUN NODE_VERSION=22.22.3 \\
    && ARCH="$(dpkg --print-architecture)" \\
    && if [ "$ARCH" = "amd64" ]; then NODE_ARCH=x64; else NODE_ARCH=arm64; fi \\
    && curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$NODE_ARCH.tar.gz" -o /tmp/node.tar.gz \\
    && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \\
    && rm /tmp/node.tar.gz \\
    && corepack enable

{clone_and_prescrub}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""


class Halo2227To7423ImageDefault(Image):

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
        return Halo2227To7423ImageBase(self.pr, self._config)

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
# test/fix patches apply cleanly. Raise the Gradle JVM heap — Halo 2.x
# multi-module builds exhaust the 512m default ('Java heap space'). Persists
# into the PR image layer so run/test/fix all inherit it.
mkdir -p /root/.gradle
echo 'org.gradle.daemon=false' >> /root/.gradle/gradle.properties
echo 'org.gradle.jvmargs=-Xmx2g -XX:MaxMetaspaceSize=512m' >> /root/.gradle/gradle.properties
echo 'org.gradle.parallel=false' >> /root/.gradle/gradle.properties
echo 'org.gradle.workers.max=2' >> /root/.gradle/gradle.properties

chmod +x gradlew

# The wrapper fetches its distribution with a 10s, zero-retry budget
# (networkTimeout=10000, retries=0) and dies if the CDN is slower than that.
# Let it fail once to create the hashed dist dir, then drop the zip in with
# curl; Install.java skips the download when the local zip already exists.
_gw_url=$(sed -n 's/^distributionUrl=//p' gradle/wrapper/gradle-wrapper.properties | sed 's/\\\\//g')
if [ -n "$_gw_url" ]; then
    timeout 180 ./gradlew --version >/dev/null 2>&1 || true
    _gw_dir=$(find /root/.gradle/wrapper/dists -mindepth 2 -maxdepth 2 -type d 2>/dev/null | head -1)
    if [ -n "$_gw_dir" ] && [ ! -f "$_gw_dir/$(basename "$_gw_url")" ]; then
        curl -fL --retry 5 --retry-all-errors --connect-timeout 120 --max-time 1800 \
            -o "$_gw_dir/$(basename "$_gw_url")" "$_gw_url" || true
    fi
fi

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

        # The clone, the ${BASE_COMMIT} pin and the history scrub are owned by the
        # base image (base-pr-<N>) and must not be re-implemented here: repeating
        # them in this layer would both duplicate a base responsibility and add a
        # second fat git layer on top of an already-scrubbed tree.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("halo-dev", "halo_2227_to_7423")
class Halo2227To7423(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Halo2227To7423ImageDefault(self.pr, self._config)

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
_BUNDLE_NIS_Halo2227To7423 = [
    # 2343 targets `next`, not release-1.5 like its numeric neighbours in
    # Halo0To2226; it pins sourceCompatibility 17, so it needs JDK 17 here.
    '2343',
    '2887-2888-2920-2950-2952-2965-2975-2977',
    '3202-3275-3276-3277-3278-3284-3309-3310-3313-3315-3318-3323',
    '3435-3459-3461-3472-3486-3487-3488',
    '3699-3708-3710-3711',
    '3730-3751-3774-3775-3810',
    '3889-3891-3895-3896-3897-3898',
    '3911-3912-3950-4075-4076',
    '4027-4035-4074-4078',
    '4626-4647-4648-4649',
    '4786-4799-4808-4812-4813-4816-4817',
    '4984-5004-5009',
    '5310-5313',
    '5329-5331-5333',
    '5446-5447-5455',
    '5860-5865',
    '5881-5883',
    '6045-6050',
    '6324-6330',
    '6574-6576-6583',
    '6630-6631-6633',
    '6860-6861-6862',
    '6869-6870',
    '6874-6876-6877-6878-6880-6881-6885-6889',
    '6920-6921-6922-6923-6924-6925-6926-6933-6934',
    '7065-7094-7099-7100-7102-7103-7106-7108-7111',
    '7092-7114-7122-7123-7127-7128-7142',
    '7149-7171-7174',
    '3087-3098-3101-3102-3108-3114-3130-3135-3142-3146-3148-3149-3150-3152-3158-3161-3166-3168-3176-3179-3182-3185-3186-3190-3191-3192-3194',
    '3444-4306-4344-4354-4355-4356-4358-4361-4362-4363-4369-4370-4371-4384-4385-4391-4392-4403-4410-4411-4412-4416-4419-4422-4423-4427-4428-4431-4434-4445-4451-4452-4453-4454-4456-4458-4459-4460-4461-4462-4463-4472-4473-4474-4477-4478-4479-4480-4481-4482-4484-4486-4487-4489-4490-4495-4500-4501-4502-4505-4506-4507-4508-4510-4511-4514-4515-4518-4521-4523-4524-4526-4528-4530',
    '4041-4102-4168-4179-4180-4182-4193-4194-4195-4196-4206-4208-4209-4210-4212-4219-4220-4222-4233-4235-4240-4241-4245-4246-4249-4253-4255-4257-4258-4260-4262-4263-4264-4265-4270-4271-4274-4275-4276-4284-4285-4286-4287-4288-4289-4290-4293-4298-4300-4301-4302-4303-4305-4308-4313-4314-4316-4318-4321-4322-4323-4328-4331-4333-4334-4335-4341',
    '6364-6427-6428-6429-6438-6440-6441-6442-6445-6446-6447-6449-6453-6454-6457-6461-6462-6463-6464-6470-6471-6473-6480-6481-6482-6483-6486-6491-6492-6498-6499-6502-6503-6504-6505-6506-6507-6511-6512-6514-6515-6516-6517-6525-6526-6529-6530-6531-6532-6533-6536-6538-6539-6540-6542-6543-6544-6545-6547-6548-6550-6551-6555-6556-6563-6564-6566',
    '6898-6902-6912-6914-6915-6917',
    '6939-6940-6945-6951-6952-6956-6959-6963-6964-6972',
    '7031-7037-7045-7049-7057-7060-7062-7069-7072-7075-7076-7077-7078-7079-7081',
    '7199-7200-7207-7208-7209-7210-7211-7215-7218',
    '7239-7253-7257-7258-7261-7268-7269-7272-7273-7275-7276-7278-7279-7281-7282',
    '7301-7303-7304-7305',
    '7313-7321-7322-7323-7328-7331-7341-7343-7350-7351-7353-7364-7365-7371-7376-7378-7379-7381-7382-7386-7387-7388-7390',
    '7383-7440-7441-7444-7446-7448-7449-7450-7451-7455-7456-7458-7459-7466-7467-7468-7469-7470-7473-7475-7476-7481-7482-7484-7485-7486-7487-7488-7494-7495-7496-7502-7506-7510-7511-7512-7516-7517-7519-7520-7521-7522-7523-7524-7525-7526-7527-7528-7529-7530-7531-7532-7533-7534-7535-7542-7543-7544-7545-7546-7547-7548-7549-7550-7551-7552-7553-7555',
    '7407-7408-7413-7414-7417-7418-7419',
    '7423-7424-7425-7429-7430-7433-7434',
]
for _ni in _BUNDLE_NIS_Halo2227To7423:
    Instance.register('halo-dev', _ni)(Halo2227To7423)
