import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Binary diffs (e.g., for .png, .gif files) cause 'cannot apply binary patch
    without full index line' errors with git apply. These are typically
    documentation assets not needed for compilation or testing.
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


class Glide5000To99999ImageBase(Image):
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

    def image_tag(self) -> str:
        return "base-jdk17"

    def workdir(self) -> str:
        return "base-jdk17"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        # SHARED base (tag "base-jdk17", ONE image reused by every PR in this era):
        # JDK + Android SDK only, SOURCE-FREE by design. Same rationale as the
        # era-1 registry: a base shared by several base.shas cannot host the
        # hardening block, and cloning here made
        # DockerfileEnhancer._inject_final_sanitize append it to the SHARED image.
        # The `# syntax` directive keeps the proxy/CA scaffolding out; the
        # reference-format markers it would otherwise inject are supplied below.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
{self.global_env}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git \\
    openjdk-11-jdk \\
    openjdk-17-jdk \\
    wget \\
    unzip \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-17
ENV JAVA_HOME=/usr/lib/jvm/java-17

RUN mkdir -p ${{ANDROID_HOME}}/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip -O /tmp/cmdline-tools.zip && \\
    unzip -q /tmp/cmdline-tools.zip -d ${{ANDROID_HOME}}/cmdline-tools && \\
    mv ${{ANDROID_HOME}}/cmdline-tools/cmdline-tools ${{ANDROID_HOME}}/cmdline-tools/latest && \\
    rm /tmp/cmdline-tools.zip

ENV PATH=${{ANDROID_HOME}}/cmdline-tools/latest/bin:${{ANDROID_HOME}}/platform-tools:${{PATH}}

# On arm64, create a fake emulator package so sdkmanager does not fail
# trying to resolve the x86-only emulator dependency
RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        mkdir -p ${{ANDROID_HOME}}/emulator && \\
        echo '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' > ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02" xmlns:ns3="http://schemas.android.com/repository/android/common/01">' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <localPackage path="emulator"><type-details xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="ns2:genericDetailsType"/>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <revision><major>31</major><minor>3</minor><micro>14</micro></revision>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '  <display-name>Android Emulator (fake for arm64)</display-name></localPackage>' >> ${{ANDROID_HOME}}/emulator/package.xml && \\
        echo '</ns2:repository>' >> ${{ANDROID_HOME}}/emulator/package.xml; \\
    fi

RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \\
    mkdir -p ${{ANDROID_HOME}}/licenses && \\
    ( echo -e '\\n24333f8a63b6825ea9c5514f83c2829b004d1fee' > ${{ANDROID_HOME}}/licenses/android-sdk-license ) && \\
    ( echo -e '\\n84831b9409646a918e30573bab4c9c91346d8abd' > ${{ANDROID_HOME}}/licenses/android-sdk-preview-license ) && \\
    ( yes | sdkmanager "platforms;android-34" "platforms;android-36" "build-tools;34.0.0" "build-tools;36.0.0" "platform-tools" || true )

# On arm64, replace x86_64 build-tools binaries with aarch64 versions
# from github.com/lzhiyong/android-sdk-tools
RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        for BT_PAIR in "34.0.0:34.0.3" "36.0.0:36.0.0"; do \\
            SDK_VER="${{BT_PAIR%%:*}}" && \\
            LZHIYONG_VER="${{BT_PAIR##*:}}" && \\
            ( curl -fsSL "https://github.com/lzhiyong/android-sdk-tools/releases/download/${{LZHIYONG_VER}}/android-sdk-tools-static-aarch64.zip" -o /tmp/arm64-build-tools.zip || true ) && \\
            ( unzip -q /tmp/arm64-build-tools.zip -d /tmp/arm64-bt || true ) && \\
            for BIN in aapt aapt2 zipalign dexdump split-select; do \\
                if [ -f "/tmp/arm64-bt/$BIN" ]; then \\
                    cp -f "/tmp/arm64-bt/$BIN" "${{ANDROID_HOME}}/build-tools/$SDK_VER/$BIN" && \\
                    chmod +x "${{ANDROID_HOME}}/build-tools/$SDK_VER/$BIN"; \\
                fi; \\
            done && \\
            rm -rf /tmp/arm64-build-tools.zip /tmp/arm64-bt; \\
        done; \\
    fi

# Era 2 (PRs 5000+) uses Gradle 8.x with settings-level repo management.
# The project build.gradle already has google(), mavenCentral(), gradlePluginPortal().
# An init.gradle with allprojects {{ repositories }} conflicts with PREFER_SETTINGS mode,
# so we intentionally leave init.gradle empty for this era.
RUN mkdir -p /root/.gradle && rm -f /root/.gradle/init.gradle

{self.clear_env}

CMD ["/bin/bash"]
"""


class Glide5000To99999ImageDefault(Image):
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
        return Glide5000To99999ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # repo/sha land in `cd` and `git checkout` lines that run as root in the
        # build and in every evaluation container -- validate before interpolating.
        repo = _safe_path_component(self.pr.repo)
        sha = _safe_path_component(self.pr.base.sha, "base commit")

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
./gradlew test testDebugUnitTest --continue || true
""".format(repo=repo, sha=sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
./gradlew test testDebugUnitTest --continue

""".format(repo=repo, sha=sha),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
./gradlew test testDebugUnitTest --continue

""".format(repo=repo, sha=sha),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew test testDebugUnitTest --continue

""".format(repo=repo, sha=sha),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        sha = _safe_path_component(self.pr.base.sha, "base commit")

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Clone + checkout + harden inside a discarded `fetch` STAGE. Layers are
        # additive, so a `git clone` layer keeps the full-history packfile no
        # matter what a later RUN prunes -- `git gc` in a subsequent layer only
        # stacks a whiteout over it, and `docker save` still yields every
        # post-base-commit upstream fix. Copying only the pruned tree forward is
        # what makes the guarantee hold on the shipped artifact.
        #
        # dependency() returns an Image, so the enhancer emits this verbatim and
        # build_dataset passes no REPO_URL / BASE_COMMIT build args; we declare
        # them here with this PR's values, named exactly as the block expects.
        # The block is emitted verbatim from image.py, never rewritten.
        #
        # No Gradle proxy scaffolding is written.
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag} AS fetch

ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT="{sha}"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}
RUN git submodule update --init --recursive || true

{hardening}


FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{self.global_env}

COPY --from=fetch /home/{repo} /home/{repo}

WORKDIR /home/{repo}

# Re-assert the invariants on what actually shipped.
RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("bumptech", "glide_5000_to_99999")
class Glide5000To99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Glide5000To99999ImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

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
# Era 5000+: every bundle whose lead PR falls in this era routes to Glide5000To99999.
#
# The key is the bundle PR numbers joined with "-", NOT a low-high range:
# these bundles are non-contiguous, so a range would silently claim PRs the
# bundle never contained (e.g. 5572-5672 would imply 101 PRs for a 14-PR
# bundle) and could collide with a neighbouring bundle.
#
# Instance.create() looks up f"{org}/{pr.number_interval}" whenever
# number_interval is non-empty. Without these registrations every bundled
# record raises "Instance ... is not registered" -- the bare key
# "bumptech/glide" is registered by neither era module.
#
# Data-derived from bumptech__glide_lht_final.jsonl -- regenerate if the
# bundles change.
_BUNDLE_NIS_GLIDE_5000_TO_99999 = [
    "5572-5610-5613-5618-5621-5622-5627-5628-5636-5637-5639-5668-5669-5672",
    "5598-5600-5606-5607",
]
for _ni in _BUNDLE_NIS_GLIDE_5000_TO_99999:
    Instance.register("bumptech", _ni)(Glide5000To99999)
