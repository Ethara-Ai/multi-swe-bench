import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TenantsImageBase(Image):
    """Repo-level base (`images/base/`, tag `:base`).

    Deliberately does NOT override `dockerfile()`: the default in
    `Image.dockerfile()` (harness/image.py:200) emits the canonical
    FROM + apt + `git clone ${REPO_URL}` + `git checkout ${BASE_COMMIT}` +
    hardening sequence, and `DockerfileEnhancer` injects TARGETARCH /
    proxy args / cert symlinks / multi-arch labels on top. Overriding
    here bypasses that and breaks multi-arch buildx / OCI export.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        # Pipfile at base_commit 41f49a0e pins `python_version = "3.7"`.
        # Bullseye (Debian 11) is the newest suite that still carries
        # python 3.7-slim on Docker Hub AND is still on deb.debian.org
        # (buster went EOL 2024-06 and its `buster-updates` Release
        # file was moved to archive.debian.org, breaking `apt-get
        # update` in the base image). Bullseye ships Node 12 by
        # default which satisfies package.json's `engines.node >=8.4.0`.
        return "python:3.7-slim-bullseye"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def extra_packages(self) -> list:
        # nodejs+npm for jest; python2.7+build-essential+g++ for node-gyp
        # fallback if any transitive dep needs a native compile (node-sass
        # 4.9.3 in particular). libpq-dev is a no-op for this PR but keeps
        # the base reusable if a sibling backend PR reuses this image.
        return [
            "nodejs",
            "npm",
            "python2.7",
            "build-essential",
            "gcc",
            "g++",
            "libpq-dev",
        ]

    def files(self) -> list:
        return []


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

    def dependency(self) -> Image:
        return TenantsImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/tenants2

# PR #64 touches only frontend/**/*.tsx + frontend/**/*.ts + one .scss;
# no Python source or Django test is exercised. Skipping `pipenv install`
# saves ~90 s of image build time and removes a whole class of failure
# modes (mypy/factory-boy Py3.7 compat with modern pip resolvers).

# npm's `postinstall` in package.json chains `npm run sass && npm run build`
# which compiles SCSS + runs full webpack. Both are expensive AND node-sass
# 4.9.3 has no prebuilt arm64 binary, so it falls back to source compile
# through node-gyp — brittle under qemu emulation. `--ignore-scripts`
# sidesteps this. jest.config.js has no scss transform and
# frontend/lib/tests/setup.ts does not import any stylesheet, so the fix
# patch's frontend/sass/styles.scss delta is never consumed at test time.
npm install --ignore-scripts --no-audit --no-fund --loglevel=error

cat > /home/tenants2/test_commands.sh <<'RUNNER'
#!/bin/bash
cd /home/tenants2
# Bypass `npm test` because it is defined as `jest && npm run lint`; a
# pre-existing tsc error in an unrelated file would then mask the jest
# signal we care about. Invoke jest directly with a deterministic reporter.
node_modules/.bin/jest --verbose --colors=false 2>&1
RUNNER
chmod +x /home/tenants2/test_commands.sh
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/tenants2
bash /home/tenants2/test_commands.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/tenants2
if ! git -C /home/tenants2 apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/tenants2/test_commands.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/tenants2
if ! git -C /home/tenants2 apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/tenants2/test_commands.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("JustFixNYC", "tenants2_178_to_23")
class TENANTS2_178_TO_23(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: Optional[str] = None) -> str:
        if run_cmd:
            return run_cmd

        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: Optional[str] = None) -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: Optional[str] = None) -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        # jest with --verbose prints one summary line per test FILE:
        #   PASS  frontend/lib/tests/app.test.tsx (1.234 s)
        #   FAIL  frontend/lib/tests/aria.test.tsx
        # Individual assertions get ✓/✗ symbols on indented lines below,
        # which we intentionally do NOT count — parsing at file granularity
        # is the invariant this repo relies on and the sibling
        # tenants2_531_to_341 config uses the same pattern.
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        pattern = re.compile(r"^(PASS|FAIL|SKIPPED)\s+(.*)$", re.MULTILINE)
        for match in pattern.finditer(log):
            status = match.group(1)
            test_name = match.group(2).strip()
            # jest emits durations like ` (1.234 s)` after the path — strip
            # them so f2p diffing against a subsequent run doesn't churn on
            # wall-clock jitter.
            test_name = re.sub(r"\s*\(\d[\d.]*\s*(?:m?s|min)\)\s*$", "", test_name)
            if status == "PASS":
                passed_tests.add(test_name)
            elif status == "FAIL":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
