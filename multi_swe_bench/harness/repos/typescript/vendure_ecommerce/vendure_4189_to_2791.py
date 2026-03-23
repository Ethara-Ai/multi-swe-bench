import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _clean_test_name(name: str) -> str:
    """Strip variable timing and metadata from test names for stable eval matching."""
    # Strip vitest file-level metadata with optional trailing bare timing:
    #   (2 tests) 75ms, (1 test | 1 failed) 120ms, (2 tests | 2 skipped)
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    # Strip parenthesized timing: (75ms), (150 ms), (8.954 s)
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


class VendureImageBaseVitestNpm(Image):
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-vitest-npm"

    def workdir(self) -> str:
        return "base-vitest-npm"

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

RUN ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV TZ=UTC

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class VendureImageDefaultVitestNpm(Image):
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
        return VendureImageBaseVitestNpm(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
git reset --hard
git checkout {base_sha}
npm install || true
npx lerna run build --ignore @vendure/admin-ui || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="./node_modules/.bin:$PATH"
cd /home/{repo}
npx lerna run test --stream --no-bail --ignore @vendure/admin-ui
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="./node_modules/.bin:$PATH"
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
npx lerna run test --stream --no-bail --ignore @vendure/admin-ui
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="./node_modules/.bin:$PATH"
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npx lerna run test --stream --no-bail --ignore @vendure/admin-ui
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("vendure-ecommerce", "vendure_4189_to_2791")
class VENDURE_4189_TO_2791(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return VendureImageDefaultVitestNpm(self.pr, self._config)

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

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Lerna --stream prefix pattern: @vendure/package-name:
        # e.g. "@vendure/core: ✓ test name"
        lerna_prefix = r"(?:(@vendure/[\w-]+):\s+)?"

        for line in clean_log.splitlines():
            stripped = line.strip()

            # Vitest file-level PASS/FAIL (with optional lerna prefix)
            m = re.match(
                lerna_prefix + r"PASS\s+(.+?)$",
                stripped,
            )
            if m:
                prefix, test = m.group(1), m.group(2).strip()
                name = _clean_test_name(f"{prefix}:{test}" if prefix else test)
                passed_tests.add(name)
                continue

            m = re.match(
                lerna_prefix + r"FAIL\s+(.+?)$",
                stripped,
            )
            if m:
                prefix, test = m.group(1), m.group(2).strip()
                name = _clean_test_name(f"{prefix}:{test}" if prefix else test)
                failed_tests.add(name)
                continue

            # Vitest test-level pass (✓/✔) with optional lerna prefix
            m = re.match(
                lerna_prefix + r"[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$",
                stripped,
            )
            if m:
                prefix, test = m.group(1), m.group(2).strip()
                name = _clean_test_name(f"{prefix}:{test}" if prefix else test)
                passed_tests.add(name)
                continue

            # Vitest test-level fail (×/✕/✗) with optional lerna prefix
            m = re.match(
                lerna_prefix + r"[×✕✗]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$",
                stripped,
            )
            if m:
                prefix, test = m.group(1), m.group(2).strip()
                name = _clean_test_name(f"{prefix}:{test}" if prefix else test)
                failed_tests.add(name)
                continue

            # Vitest skipped (↓/○) with optional lerna prefix
            m = re.match(
                lerna_prefix + r"[↓○]\s+(.+?)(?:\s+\[skipped\])?$",
                stripped,
            )
            if m:
                prefix, test = m.group(1), m.group(2).strip()
                name = _clean_test_name(f"{prefix}:{test}" if prefix else test)
                skipped_tests.add(name)
                continue

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
