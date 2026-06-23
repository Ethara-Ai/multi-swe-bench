from __future__ import annotations
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class S2nTlsImageBase(Image):
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
        return "gcc:9"

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

        repo = self.pr.repo
        org = self.pr.org

        # SHARED base (tag "base", ONE image reused by EVERY PR — keeps full git
        # history + origin so each PR can `git checkout <base.sha>` in prepare.sh;
        # we do NOT make a base per PR). mam reference format: no proxy/cert,
        # ARGs/ENV/LABEL. The `# syntax` directive makes DockerfileEnhancer.enhance()
        # skip it. Per-PR checkout + hardening live in S2nTlsImageDefault (a shared
        # base cannot pin/strip to one commit).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
        cmake libssl-dev ninja-build pkg-config git ca-certificates && \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class S2nTlsImageDefault(Image):
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
        return S2nTlsImageBase(self.pr, self._config)

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
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Relax specific -Werror flags so older eras compile under gcc 9+.
# Older s2n-tls revisions trigger maybe-uninitialized / cast-align warnings
# that newer GCCs promote to errors. We keep -Werror but allow these specific
# warnings to pass.
if grep -q "^target_compile_options.*-Werror " CMakeLists.txt; then
    sed -i "s/-Werror /-Werror -Wno-error=maybe-uninitialized -Wno-error=uninitialized -Wno-error=cast-align -Wno-error=unused-result -Wno-error=stringop-overflow -Wno-error=array-bounds -Wno-error=stringop-truncation /" CMakeLists.txt
fi

# Commit the build-flag patch so subsequent `git reset --hard HEAD` keeps it.
git config user.email build@local
git config user.name build
git add -A
git commit -m "harness: relax Werror flags" --quiet || true

# Configure + build with PQ crypto disabled (older revisions have ARM ASM
# / static_assert issues; PQ is not exercised by these tests).
# || true so a base-build hiccup doesn't kill the image; test/fix-run.sh rebuild.
rm -rf build
cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DS2N_NO_PQ=ON || true
cmake --build build -j$(nproc) || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if [ ! -d build ] || [ ! -f build/CTestTestfile.cmake ]; then
    rm -rf build
    cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DS2N_NO_PQ=ON
    cmake --build build -j$(nproc)
fi

cd /home/{pr.repo}/build
ctest -V

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard HEAD

# Patch -p1 fallback can exit non-zero on a single rejected hunk even when
# the rest applied; allow it so ctest still runs against what did apply.
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    patch -p1 --no-backup-if-mismatch < /home/test.patch || true
fi

# First try: incremental rebuild with the existing -DS2N_NO_PQ=ON tree.
# Fallbacks (in order):  full PQ-OFF rebuild  ->  full PQ-ON rebuild
# (some PRs add tests that reference PQ symbols, which need PQ-ON to link).
if ! cmake --build build -j$(nproc); then
    rm -rf build
    if ! (cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DS2N_NO_PQ=ON && cmake --build build -j$(nproc)); then
        rm -rf build
        cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
        cmake --build build -j$(nproc)
    fi
fi

cd /home/{pr.repo}/build
ctest -V

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard HEAD

# Patch -p1 fallback can exit non-zero on a single rejected hunk even when
# the rest applied; allow it so ctest still runs against what did apply.
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    patch -p1 --no-backup-if-mismatch < /home/test.patch || true
fi
if ! git apply --whitespace=nowarn /home/fix.patch 2>/dev/null; then
    patch -p1 --no-backup-if-mismatch < /home/fix.patch || true
fi

# First try: incremental rebuild with the existing -DS2N_NO_PQ=ON tree.
# Fallbacks (in order):  full PQ-OFF rebuild  ->  full PQ-ON rebuild
# (some PRs add tests that reference PQ symbols, which need PQ-ON to link).
if ! cmake --build build -j$(nproc); then
    rm -rf build
    if ! (cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DS2N_NO_PQ=ON && cmake --build build -j$(nproc)); then
        rm -rf build
        cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
        cmake --build build -j$(nproc)
    fi
fi

cd /home/{pr.repo}/build
ctest -V

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

        # Per-PR anti-cheat hardening. dependency() returns an Image, so
        # DockerfileEnhancer emits this Dockerfile verbatim (it only auto-injects
        # hardening into str-dependency images) — so we embed it here, after
        # prepare.sh. NOTE: prepare.sh commits a "relax Werror" harness commit ON
        # TOP of base.sha, so we must NOT re-`checkout base.sha` (that would drop
        # the build-flag fix and break the run scripts' `git reset --hard HEAD`).
        # Instead, harden the current detached HEAD in place: drop origin + every
        # ref + reflog, gc-prune unreachable objects (the fix/future commits), then
        # audit (no refs/remotes, base.sha IS an ancestor, rev-list --all == HEAD
        # so nothing beyond base.sha's history + the harness commit is reachable).
        repo = self.pr.repo
        base_sha = self.pr.base.sha
        hardening = (
            "RUN set -eux; \\\n"
            f"    cd /home/{repo}; \\\n"
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all || true; \\\n"
            "    git reflog expire --expire-unreachable=now --all || true; \\\n"
            "    git gc --prune=now --aggressive; \\\n"
            "    git repack -a -d -l --quiet; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\\n'
            '    test -z "$(git remote)"; \\\n'
            f"    git merge-base --is-ancestor {base_sha} HEAD; \\\n"
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'
        )

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{hardening}

{self.clear_env}

"""


@Instance.register("aws", "s2n-tls")
class S2nTls(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return S2nTlsImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes before matching.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Failed"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Exception"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Timeout"),
        ]
        re_skip_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Skipped"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Not Run"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    passed_tests.add(pass_match.group(1))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    skipped_tests.add(skip_match.group(1))

        # Enforce TestResult invariants: a test cannot be in two buckets.
        # Failed wins over passed (retries), skipped is exclusive.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
