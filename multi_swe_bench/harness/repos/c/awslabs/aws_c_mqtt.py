from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# PR tiers for dependency compatibility:
# Tier 1 (HEAD deps): PR >= 234 and PR != 240
# Tier 2 (Dec 2022 pinned deps): PR in {184, 202, 225, 240}
# Tier 3 (per-PR date-matched deps): PR <= 177
TIER2_PRS = {184, 202, 225, 240}
# Dec 2022 cutoff date for tier 2 deps
TIER2_DATE = "2022-12-01"

# Deps to build (in order) for each tier
# Tier 3 needs fewer: old PRs used system OpenSSL, not aws-lc
DEPS_FULL = [
    ("aws-lc", "aws", "aws-lc"),
    ("s2n-tls", "aws", "s2n-tls"),
    ("aws-c-common", "awslabs", "aws-c-common"),
    ("aws-c-cal", "awslabs", "aws-c-cal"),
    ("aws-c-io", "awslabs", "aws-c-io"),
    ("aws-c-compression", "awslabs", "aws-c-compression"),
    ("aws-c-http", "awslabs", "aws-c-http"),
]

# For tier 3 old PRs: aws-c-common, s2n-tls, aws-c-io, aws-c-compression, aws-c-http
DEPS_OLD = [
    ("aws-c-common", "awslabs", "aws-c-common"),
    ("s2n-tls", "aws", "s2n-tls"),
    ("aws-c-io", "awslabs", "aws-c-io"),
    ("aws-c-compression", "awslabs", "aws-c-compression"),
    ("aws-c-http", "awslabs", "aws-c-http"),
]


def _get_tier(pr_number: int) -> int:
    if pr_number in TIER2_PRS:
        return 2
    if pr_number <= 177:
        return 3
    return 1


def _build_dep_commands_head() -> str:
    """Build all deps at HEAD (Tier 1)."""
    cmds = []
    for name, org, repo in DEPS_FULL:
        extra = " -DBUILD_SHARED_LIBS=OFF -DOPENSSL_NO_ASM=1 -DDISABLE_GO=ON" if name == "aws-lc" else ""
        cmds.append(
            f"# Build {name}\n"
            f"RUN git clone --depth 1 https://github.com/{org}/{repo}.git /home/{repo} && \\\n"
            f"    mkdir -p /home/{repo}/build && cd /home/{repo}/build && \\\n"
            f"    cmake -DBUILD_TESTING=OFF{extra} \\\n"
            f"        -DCMAKE_INSTALL_PREFIX=/home/install \\\n"
            f"        -DCMAKE_PREFIX_PATH=/home/install .. && \\\n"
            f"    make -j$(nproc) && make install && \\\n"
            f"    cd /home && rm -rf /home/{repo}"
        )
    return "\n\n".join(cmds)


def _build_dep_commands_pinned(date: str) -> str:
    """Build all deps pinned to a date (Tier 2)."""
    cmds = []
    for name, org, repo in DEPS_FULL:
        extra = " -DBUILD_SHARED_LIBS=OFF -DOPENSSL_NO_ASM=1 -DDISABLE_GO=ON" if name == "aws-lc" else ""
        cmds.append(
            f"# Build {name} (pinned to {date})\n"
            f"RUN git clone https://github.com/{org}/{repo}.git /home/{repo} && \\\n"
            f"    cd /home/{repo} && \\\n"
            f"    git checkout $(git log --before=\"{date}\" -1 --format=\"%H\") && \\\n"
            f"    find . -name \"*.cmake\" -o -name \"CMakeLists.txt\" | xargs sed -i \"s/-Werror//g\" && \\\n"
            f"    mkdir -p build && cd build && \\\n"
            f"    cmake -DBUILD_TESTING=OFF{extra} \\\n"
            f"        -DCMAKE_INSTALL_PREFIX=/home/install \\\n"
            f"        -DCMAKE_PREFIX_PATH=/home/install .. && \\\n"
            f"    make -j$(nproc) && make install && \\\n"
            f"    find /home/install -name \"*.cmake\" | xargs sed -i \"s/-Werror//g\" && \\\n"
            f"    cd /home && rm -rf /home/{repo}"
        )
    return "\n\n".join(cmds)


def _build_deps_script_old() -> str:
    """Shell script to build date-matched deps (Tier 3). Runs in prepare.sh.
    Uses DEPS_FULL so HEAD rebuilds (for test/fix patches) have all deps including aws-lc/aws-c-cal.
    For old dates, aws-lc/aws-c-cal may not exist yet — script skips them gracefully."""
    lines = [
        '#!/bin/bash',
        'PREFIX=/home/install',
        'TARGET_DATE="$1"',
        'echo "Building date-matched deps for $TARGET_DATE"',
        '',
        'rm -rf $PREFIX',
        'mkdir -p $PREFIX',
        '',
    ]
    for name, org, repo in DEPS_FULL:
        extra = " -DBUILD_SHARED_LIBS=OFF -DOPENSSL_NO_ASM=1 -DDISABLE_GO=ON" if name == "aws-lc" else ""
        lines.extend([
            f'echo "Building {name}..."',
            f'cd /home/deps/{repo}',
            f'git reset --hard',
            f'COMMIT=$(git log origin/HEAD --before="$TARGET_DATE" -1 --format="%H" 2>/dev/null || true)',
            f'if [ -z "$COMMIT" ]; then',
            f'    echo "Skipping {name} — no commit before $TARGET_DATE"',
            f'else',
            f'    git checkout $COMMIT',
            f'    find . -name "*.cmake" -o -name "CMakeLists.txt" | xargs sed -i "s/-Werror//g" 2>/dev/null || true',
            f'    rm -rf build && mkdir -p build && cd build',
            f'    if cmake -DBUILD_TESTING=OFF{extra} -DCMAKE_INSTALL_PREFIX=$PREFIX -DCMAKE_PREFIX_PATH=$PREFIX .. 2>&1 && make -j$(nproc) 2>&1 && make install 2>&1; then',
            f'        find $PREFIX -name "*.cmake" | xargs sed -i "s/-Werror//g" 2>/dev/null || true',
            f'        echo "{name} done"',
            f'    else',
            f'        echo "WARN: {name} build failed at this date — skipping"',
            f'    fi',
            f'fi',
            '',
        ])
    lines.append('echo "All deps built successfully"')
    return '\n'.join(lines)


class AwsCMqttImageBase(Image):
    """Tier 1: HEAD deps for modern PRs (>= 234, != 240)."""

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

        dep_commands = _build_dep_commands_head()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y \\
    git \\
    build-essential \\
    cmake \\
    libssl-dev \\
    libcurl4-openssl-dev \\
    pkg-config \\
    python3 \\
    golang \\
    && rm -rf /var/lib/apt/lists/*

{dep_commands}

{code}

{self.clear_env}

"""


class AwsCMqttImageBaseMid(Image):
    """Tier 2: Dec 2022 pinned deps for PRs 184, 202, 225, 240."""

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
        return "base-mid"

    def workdir(self) -> str:
        return "base-mid"

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

        dep_commands = _build_dep_commands_pinned(TIER2_DATE)

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y \\
    git \\
    build-essential \\
    cmake \\
    libssl-dev \\
    libcurl4-openssl-dev \\
    pkg-config \\
    python3 \\
    golang \\
    && rm -rf /var/lib/apt/lists/*

{dep_commands}

{code}

{self.clear_env}

"""


class AwsCMqttImageBaseOld(Image):
    """Tier 3: Base image for old PRs (<= 177). Only clones dep repos, no building."""

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
        return "base-old"

    def workdir(self) -> str:
        return "base-old"

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

        clone_cmds = []
        for name, org, repo in DEPS_FULL:
            clone_cmds.append(
                f"RUN git clone https://github.com/{org}/{repo}.git /home/deps/{repo}"
            )
        clone_block = "\n".join(clone_cmds)

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y \\
    git \\
    build-essential \\
    cmake \\
    libssl-dev \\
    libcurl4-openssl-dev \\
    pkg-config \\
    python3 \\
    golang \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/deps

{clone_block}

{code}

{self.clear_env}

"""


class AwsCMqttImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        tier = _get_tier(self.pr.number)
        if tier == 2:
            return AwsCMqttImageBaseMid(self.pr, self._config)
        elif tier == 3:
            return AwsCMqttImageBaseOld(self.pr, self._config)
        else:
            return AwsCMqttImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        tier = _get_tier(self.pr.number)

        base_files = [
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
        ]

        if tier == 3:
            # Old PRs: build deps in prepare.sh using date-matched checkouts
            base_files.append(
                File(
                    ".",
                    "build_deps.sh",
                    _build_deps_script_old(),
                )
            )
            base_files.append(
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

# Get the date of the base commit to match deps
TARGET_DATE=$(git log -1 --format="%ci" {pr.base.sha})
echo "Base commit date: $TARGET_DATE"

# Build date-matched deps
bash /home/build_deps.sh "$TARGET_DATE"

# Strip -Werror from installed cmake modules and from mqtt source
find /home/install -name "*.cmake" | xargs sed -i "s/-Werror//g" 2>/dev/null || true
find /home/{pr.repo} -name "*.cmake" -o -name "CMakeLists.txt" | xargs sed -i "s/-Werror//g" 2>/dev/null || true

mkdir -p build

""".format(pr=self.pr),
                )
            )
        else:
            # Tier 1 and 2: deps already built in base image
            base_files.append(
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
mkdir -p build

""".format(pr=self.pr),
                )
            )

        # Build/test scripts are the same for all tiers
        base_files.extend([
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
find . -name "*.cmake" -o -name "CMakeLists.txt" | xargs sed -i "s/-Werror//g" 2>/dev/null || true
cd build
cmake -DBUILD_TESTING=ON -DCMAKE_PREFIX_PATH=/home/install -DCMAKE_MODULE_PATH=/home/install/lib/cmake/aws-c-common/modules ..
make -j$(nproc)
ctest --output-on-failure --timeout 120 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                self._test_run_script(),
            ),
            File(
                ".",
                "fix-run.sh",
                self._fix_run_script(),
            ),
        ])

        return base_files

    def _rebuild_deps_block(self) -> str:
        tier = _get_tier(self.pr.number)
        if tier != 3:
            return ""
        upper_tag = self.pr.base.label.split("..")[-1].strip() if ".." in self.pr.base.label else ""
        if upper_tag:
            return (
                f'MERGE_DATE=$(cd /home/{self.pr.repo} && git log -1 --format="%ci" {upper_tag} 2>/dev/null || echo "2099-01-01")\n'
                f'bash /home/build_deps.sh "$MERGE_DATE"\n'
            )
        return 'bash /home/build_deps.sh "2099-01-01"\n'

    def _test_run_script(self) -> str:
        rebuild = self._rebuild_deps_block()
        return """#!/bin/bash
set -e

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
    git apply --whitespace=nowarn /home/test.patch || echo "WARNING: test patch failed to apply completely" >&2
fi
{rebuild}find . -name "*.cmake" -o -name "CMakeLists.txt" | xargs sed -i "s/-Werror//g" 2>/dev/null || true
find /home/install -name "*.cmake" | xargs sed -i "s/-Werror//g" 2>/dev/null || true
rm -rf build && mkdir -p build && cd build
cmake -DBUILD_TESTING=ON -DCMAKE_PREFIX_PATH=/home/install -DCMAKE_MODULE_PATH=/home/install/lib/cmake/aws-c-common/modules ..
make -j$(nproc)
ctest --output-on-failure --timeout 120 || true

""".format(pr=self.pr, rebuild=rebuild)

    def _fix_run_script(self) -> str:
        rebuild = self._rebuild_deps_block()
        return """#!/bin/bash
set -e

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
    git apply --whitespace=nowarn /home/test.patch || echo "WARNING: test patch failed to apply completely" >&2
fi
if [ -s /home/fix.patch ]; then
    git apply --whitespace=nowarn /home/fix.patch || echo "WARNING: fix patch failed to apply completely" >&2
fi
{rebuild}find . -name "*.cmake" -o -name "CMakeLists.txt" | xargs sed -i "s/-Werror//g" 2>/dev/null || true
find /home/install -name "*.cmake" | xargs sed -i "s/-Werror//g" 2>/dev/null || true
rm -rf build && mkdir -p build && cd build
cmake -DBUILD_TESTING=ON -DCMAKE_PREFIX_PATH=/home/install -DCMAKE_MODULE_PATH=/home/install/lib/cmake/aws-c-common/modules ..
make -j$(nproc)
ctest --output-on-failure --timeout 120 || true

""".format(pr=self.pr, rebuild=rebuild)

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


@Instance.register("awslabs", "aws-c-mqtt")
class AwsCMqtt(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AwsCMqttImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed")]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Failed")
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test = pass_match.group(1)
                    passed_tests.add(test)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test = fail_match.group(1)
                    failed_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
