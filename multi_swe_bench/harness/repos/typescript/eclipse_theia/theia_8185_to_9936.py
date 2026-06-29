"""eclipse-theia/theia harness config — Node 12 Yarn era (PRs 8186–9936).

Theia is a TypeScript monorepo using Lerna.  This era uses Yarn Classic as the
package manager (yarn.lock present) and requires Node >= 12.14.1 with an upper
bound of ``< 13``, so ``node:12-buster`` is the appropriate base image.  Tests
run via Mocha through Lerna
(``lerna run --scope "@theia/!(example-)*" test``).
"""

import re

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TheiaNode12ImageDefault(Image):
    """PR-specific Docker layer for the Node 12 Yarn era.

    Pipeline:
        git checkout <base_sha>
        PUPPETEER_SKIP_DOWNLOAD=true yarn install --ignore-engines
        yarn compile
        lerna run --scope "@theia/!(example-)*" test --stream --concurrency=1
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
        return "node:12-buster"

    @staticmethod
    def _is_deprecated_debian(base_img: str) -> bool:
        # node:*-buster (and older) images sit on EOL Debian whose apt repos
        # have moved to archive.debian.org. The base class only recognises
        # bare "debian:buster"-style tags, so flag the node images here too;
        # this makes Image._get_apt_update_command() rewrite sources.list to
        # archive.debian.org before "apt-get update" (otherwise install 404s).
        if any(s in base_img for s in ("buster", "stretch", "jessie")):
            return True
        return Image._is_deprecated_debian(base_img)

    def extra_packages(self) -> list[str]:
        # build-essential (g++/make), git, and python3 are already in the
        # default package set baked into Image.dockerfile(); only Theia's
        # native-module headers (electron/keytar/node-pty backends) are extra.
        return ["libx11-dev", "libxkbfile-dev", "libsecret-1-dev"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the eval scripts + patches into /home/ (outside the git
        # tree, so hardening leaves them untouched) and bakes the heavy
        # yarn/npm install + compile into the image via prepare.sh.
        return (
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh"
        )

    def dockerfile(self) -> str:
        # Explicit canonical Dockerfile (mirrors Image.dockerfile()) so the
        # anti-reward-hacking Image._HARDENING_BLOCK is referenced directly in
        # this file rather than only inherited. The base image is a string dep,
        # so DockerfileEnhancer still injects the REPO_URL/BASE_COMMIT ARGs +
        # infra block; because the clone uses "${REPO_URL}" and the hardening
        # marker is already present, the enhancer makes no further changes.
        base_img = self.dependency()

        default_packages = [
            "ca-certificates", "curl", "build-essential", "git", "gnupg",
            "make", "python3", "sudo", "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        repo = _safe_path_component(self.pr.repo)
        extra_setup = self.extra_setup()

        sections = [f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(apt_command)
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        if extra_setup:
            sections.append(extra_setup)
        sections.append(Image._HARDENING_BLOCK)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"

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
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard

export PUPPETEER_SKIP_DOWNLOAD=true
export TS_NODE_TRANSPILE_ONLY=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=true
yarn install --ignore-engines || true
yarn compile || yarn build || true
""".format(
                    repo=self.pr.repo,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}

export PUPPETEER_SKIP_DOWNLOAD=true
export TS_NODE_TRANSPILE_ONLY=true
./node_modules/.bin/lerna run --scope "@theia/!(example-)*" test --stream --concurrency=1 --no-bail 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
git apply --whitespace=nowarn --3way /home/test.patch

export PUPPETEER_SKIP_DOWNLOAD=true
export TS_NODE_TRANSPILE_ONLY=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=true
yarn compile || yarn build || true
./node_modules/.bin/lerna run --scope "@theia/!(example-)*" test --stream --concurrency=1 --no-bail 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch

export PUPPETEER_SKIP_DOWNLOAD=true
export TS_NODE_TRANSPILE_ONLY=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=true
yarn install --ignore-engines || true
yarn compile || yarn build || true
./node_modules/.bin/lerna run --scope "@theia/!(example-)*" test --stream --concurrency=1 --no-bail 2>&1
""".format(repo=self.pr.repo),
            ),
        ]


@Instance.register("eclipse-theia", "theia_8185_to_9936")
class THEIA_8185_TO_9936(Instance):
    """Harness instance for eclipse-theia/theia — Node 12 Yarn era (PRs 8186–9936)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return TheiaNode12ImageDefault(self.pr, self._config)

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
        """Parse Mocha spec reporter output wrapped by Lerna.

        Lerna prefixes every line with ``@theia/<pkg>: ``.  Mocha uses the
        spec reporter, producing:

            @theia/core: ✓ should do something (123ms)
            @theia/core: - should be skipped
            @theia/core: 42 passing (1m)
            @theia/core: 1 failing
            @theia/core:   1) Suite > test name:
            @theia/core:      Error: expected X to equal Y

        Both ✓ (U+2713) and ✔ (U+2714) may appear depending on the terminal.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        # Also strip Yarn's carriage-return tricks
        yarn_cr = re.compile(r"\x1b\[2K\x1b\[1G")
        clean_log = yarn_cr.sub("", test_log)
        clean_log = ansi_escape.sub("", clean_log)

        # Strip Lerna's @theia/<package>: prefix
        lerna_prefix = re.compile(r"^@theia/[^:]+:\s?", re.MULTILINE)
        clean_log = lerna_prefix.sub("", clean_log)

        # Mocha pass: ✓ or ✔ followed by test name, optional (Nms) duration
        re_pass = re.compile(
            r"^\s*[\u2713\u2714]\s+(.+?)(?:\s+\(\d+m?s\))?\s*$"
        )
        # Mocha numbered failure: N) test name
        re_fail_numbered = re.compile(
            r"^\s+(\d+)\)\s+(.+?)\s*$"
        )
        # Mocha skip/pending: - test name
        re_skip = re.compile(
            r"^\s+-\s+(.+?)(?:\s+\(\d+m?s\))?\s*$"
        )
        # Summary line: N failing
        re_summary_failing = re.compile(r"^\s*(\d+)\s+failing\b")

        in_failure_list = False

        for line in clean_log.splitlines():
            m = re_summary_failing.match(line)
            if m:
                in_failure_list = True
                continue

            m = re_pass.match(line)
            if m:
                test_name = m.group(1).strip()
                passed_tests.add(test_name)
                in_failure_list = False
                continue

            m = re_skip.match(line)
            if m:
                test_name = m.group(1).strip()
                skipped_tests.add(test_name)
                in_failure_list = False
                continue

            m = re_fail_numbered.match(line)
            if m:
                test_name = m.group(2).strip()
                failed_tests.add(test_name)
                continue

        # Deduplicate: failures override pass/skip
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


# Route the dash-joined number_interval (canonical prs_in_bundle from each
# record's resolved_issues + own PR number, sorted, "-"-joined) of the
# SHIPPABLE chunk1+chunk2 dataset to the THEIA_8185_TO_9936 config. Instance.create()
# looks up f"{org}/{number_interval}"; Instance.register returns the class
# unchanged, so it answers to every key (the era key + any prior keys above
# are kept for back-compat).
_BUNDLE_NUMBER_INTERVALS = [
    "2815-5397-5812-6572-6636-7103-7538-7586-7738-7746-7931-7953-8655-8712-8750-8751-8752-8888-8933-8955-8963-8991-8992-8994-9052-9054-9065-9068-9131-9133-9145-9147-9150-9152-9158-9163-9164-9169-9175-9177-9180-9192-9200-9209-9212-9221-9227-9232-9245-9254-9258",
    "2961-4965-6796-8222-8314-8469-8752-8911-8965-8993-9020-9110-9142-9191-9220-9310-9371-9392-9408-9413-9424-9427-9435-9452-9459-9476-9486-9516-9520-9522-9523",
    "5876-6016-6784-7037-7078-7737-8070-8946-8947-8982-9069-9138-9224-9242-9348-9375-9438-9461-9468-9475-9485-9499-9525-9526-9529-9530-9532-9536-9553-9563-9565-9568-9571-9572-9573-9579-9580-9584-9585-9590-9591-9598-9602-9615-9617-9620-9623-9630-9631-9634-9635-9655-9666-9672-9680-9682",
    "202-2819-7797-8769-9269-10023-10077-10200-10201-10267-10598-10684-10689-10710-10819-10830-10891-10931-11015-11034-11036-11051-11059-11072-11089-11096-11099-11102-11104-11110-11111-11115-11116-11130-11142-11150-11154-11158-11160-11175-11176-11178-11182-11184-11189-11190-11191-11195-11196-11201-11203-11207-11208",
    "1327-1902-3053-6439-6904-7431-8795-8903-8970-9391-9405-9432-9436-9543-9663-9674-9727-9778-9779-9800-9806-9807-9815-9817-9819-9820-9826-9831-9832-9833-9841-9851-9852-9856-9860-9870-9871-9876-9882-9883-9900-9901-9905-9910-9914-9919-9927-9931-9935-9937-9938-9939-9951-9954-9956-9958-9960-9962-9968-9970-9973",
    "2961-4965-6796-8222-8314-8469-8752-8965-8993-9020-9110-9142-9191-9220-9310-9371-9392-9408-9413-9424-9427-9435-9452-9459-9476-9486-9516-9520-9522-9523",
    "5876-6016-6784-7037-7078-7737-8070-8946-8982-9069-9138-9224-9242-9348-9375-9438-9461-9468-9475-9485-9499-9525-9526-9529-9530-9532-9536-9553-9563-9565-9568-9571-9572-9573-9579-9580-9584-9585-9590-9591-9598-9602-9615-9617-9620-9623-9630-9631-9634-9635-9655-9666-9672-9680-9682",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("eclipse-theia", _ni)(THEIA_8185_TO_9936)
