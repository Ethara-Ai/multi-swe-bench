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


class Glide0To4999ImageBase(Image):
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
        return "base-jdk8"

    def workdir(self) -> str:
        return "base-jdk8"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        # SHARED base (tag "base-jdk8", ONE image reused by every PR in this era):
        # JDK + Android SDK + Gradle cache only, SOURCE-FREE by design.
        #
        # It must not clone the repo. Previously it did, and because this file
        # emitted no CMD, DockerfileEnhancer._inject_final_sanitize appended
        # image.py's hardening block HERE -- pinning a base shared by 13 different
        # base.shas to whichever commit built first, while leaving the per-PR
        # images unpinned. The block prunes everything unreachable from
        # ${BASE_COMMIT}, so it can only ever live in a per-PR layer.
        #
        # Source-free also means this file carries none of the tokens the enhancer
        # keys on ("git clone" / "git fetch" / "git remote add" / "COPY <repo>
        # /home/<repo>"), so _standardize_repo_fetch and _inject_final_sanitize
        # both no-op here. The `# syntax` directive additionally makes enhance()
        # return this text verbatim, which keeps the proxy/CA-certificate
        # scaffolding out; the reference-format markers it would otherwise inject
        # are supplied explicitly below.
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
    openjdk-8-jdk \\
    openjdk-11-jdk \\
    wget \\
    unzip \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8 && \\
    ln -sf /usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-11
ENV JAVA_HOME=/usr/lib/jvm/java-8

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

ENV JAVA_HOME=/usr/lib/jvm/java-11

RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \\
    mkdir -p ${{ANDROID_HOME}}/licenses && \\
    ( echo -e '\\n24333f8a63b6825ea9c5514f83c2829b004d1fee' > ${{ANDROID_HOME}}/licenses/android-sdk-license ) && \\
    ( echo -e '\\n84831b9409646a918e30573bab4c9c91346d8abd' > ${{ANDROID_HOME}}/licenses/android-sdk-preview-license ) && \\
    ( yes | sdkmanager "platforms;android-25" "platforms;android-26" "platforms;android-27" "platforms;android-28" "platforms;android-29" "platforms;android-30" "build-tools;25.0.2" "build-tools;26.0.3" "build-tools;27.0.3" "build-tools;28.0.3" "build-tools;29.0.3" "build-tools;30.0.3" "platform-tools" "extras;android;m2repository" || true )

ENV JAVA_HOME=/usr/lib/jvm/java-8

# On arm64, replace x86_64 build-tools binaries with aarch64 versions
# from github.com/lzhiyong/android-sdk-tools
RUN if [ "$TARGETARCH" = "arm64" ]; then \\
        for BT_PAIR in "25.0.2:25.0.3" "26.0.3:26.1.1" "27.0.3:27.0.11" "28.0.3:28.0.3" "29.0.3:29.0.3" "30.0.3:30.0.3"; do \\
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

RUN mkdir -p /root/.gradle && \\
    printf '%s\\n' \\
        'allprojects {{' \\
        '    buildscript {{' \\
        '        repositories {{' \\
        '            maven {{ url "https://maven.google.com" }}' \\
        '            mavenCentral()' \\
        '            maven {{ url "https://oss.sonatype.org/content/repositories/snapshots" }}' \\
        '        }}' \\
        '    }}' \\
        '    repositories {{' \\
        '        mavenLocal()' \\
        '        maven {{ url "https://maven.google.com" }}' \\
        '        mavenCentral()' \\
        '        maven {{ url "https://oss.sonatype.org/content/repositories/snapshots" }}' \\
        '    }}' \\
        '}}' > /root/.gradle/init.gradle

{self.clear_env}

CMD ["/bin/bash"]
"""


class Glide0To4999ImageDefault(Image):
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
        return Glide0To4999ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # repo/sha land in `cd` and `git checkout` lines that run as root in the
        # build and in every evaluation container -- same gate image.py applies
        # to the Dockerfiles it generates itself.
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

        # Validated before interpolation into RUN/WORKDIR paths and into the
        # hardening block's ${BASE_COMMIT}. Base.__post_init__ only type-checks
        # `sha`, so this is the only charset gate standing between a crafted
        # dataset record and a RUN line in the generated build.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        sha = _safe_path_component(self.pr.base.sha, "base commit")

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The base is source-free, so the fetch lives here, in a discarded
        # `fetch` STAGE.
        #
        # Why a stage rather than consecutive RUNs: layers are additive, so the
        # `git clone` layer keeps the full-history packfile no matter what a
        # LATER RUN prunes -- `git gc` in a subsequent layer only stacks a
        # whiteout over it. The running container reads the union and looks
        # clean, but `docker save` of the built image still yields every
        # post-base-commit upstream fix. Cloning and hardening inside a stage
        # whose layers are never exported, then COPYing only the pruned tree
        # forward, makes the guarantee hold on the shipped artifact.
        #
        # dependency() returns an Image, so DockerfileEnhancer emits this text
        # verbatim AND build_dataset passes no REPO_URL / BASE_COMMIT build args
        # (both gated on a str dependency). We therefore declare those ARGs here,
        # defaulted to this PR's own values and named exactly as the block
        # expects -- Docker exposes build args to RUN as environment variables,
        # so ${BASE_COMMIT} resolves to this sha. The block itself is emitted
        # verbatim from image.py: referenced, never rewritten, so it cannot drift
        # from the harness.
        #
        # No Gradle proxy scaffolding is written: the previous version injected
        # systemProp.http.proxyHost/.proxyPort into ~/.gradle/gradle.properties.
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

# Re-assert the invariants on what actually shipped, so a COPY that dropped or
# altered the pruned tree fails the build here rather than surfacing later.
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


@Instance.register("bumptech", "glide_0_to_4999")
class Glide0To4999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Glide0To4999ImageDefault(self.pr, self._config)

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
# Era 0..4999: every bundle whose lead PR falls in this era routes to Glide0To4999.
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
_BUNDLE_NIS_GLIDE_0_TO_4999 = [
    "2138-2203-2233-2261-2286-2293",
    "2334-2349-2351-2353-2359-2405-2411-2425-2439-2447",
    "2426-2489-2508-2509",
    "2550-2556-2558-2640-2641-2666-2668-2674",
    "2670-2671-2681-2685-2712-2713-2720-2721-2722-2729-2746-2747-2748-2749-2750-2755-2762-2771-2778-2779",
    "2789-2790-2797",
    "2873-2889-2935-2936-2970-2996-2999-3002",
    "3136-3139-3160",
    "3305-3308-3352-3365-3375-3438-3444-3446-3508",
    "3525-3537-3583-3669-3670-3671-3672-3677-3678-3682-3683-3684-3687-3688-3689-3694-3695-3698-3699-3701-3702-3703-3704-3705-3707-3710-3711-3712-3715-3721-3723-3724-3727-3729-3730-3747-3763-3764-3765-3766-3776-3777-3784-3786-3790-3791-3792-3796-3797-3799-3801-3804-3805-3810-3813-3817-3824-3825-3831-3832-3833-3834-3843-3854-3862-3864-3880-3887-3888-3892",
    "3889-3890-3891-3903-3907-3908-3909-3911-3914-3933-3934-3940-3947-3948-3950-3952-3956-3957-3959-3960-3962-3963-3964-3965-3967-3968-3977-3982-3983-3994-4002-4033-4040",
    "4041-4044-4047-4048-4050-4054-4068-4070-4072-4075-4076-4077-4080-4086-4089-4091-4117-4130-4141-4146-4147-4148-4156-4157-4159-4163-4167-4184-4185-4193-4214-4217-4218-4225-4233-4235-4240-4241-4243-4261-4262-4273-4276-4277-4295-4303-4310-4329-4331-4343-4352-4354-4375-4380-4381-4382-4389-4418-4428-4433-4446-4449-4465-4486",
    "4484-4493-4504-4514-4518-4520-4525-4526-4527-4529-4536-4546-4547-4551-4634-4653-4719-4738",
]
for _ni in _BUNDLE_NIS_GLIDE_0_TO_4999:
    Instance.register("bumptech", _ni)(Glide0To4999)
