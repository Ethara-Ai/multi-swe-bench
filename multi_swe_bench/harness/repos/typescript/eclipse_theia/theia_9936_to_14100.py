"""eclipse-theia/theia harness config — Node 16 Yarn era (PRs 10049–13306).

Theia is a TypeScript monorepo using Lerna.  This era uses Yarn Classic as the
package manager (yarn.lock present) and requires Node >= 12.14.1 (no upper
bound from PR 10049 onward).  CI uses Node 16 during this period, so
``node:16-bullseye`` is the appropriate base image.  Tests run via Mocha through
Lerna (``lerna run --scope "@theia/!(example-)*" test``).
"""

import re

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TheiaNode16ImageDefault(Image):
    """PR-specific Docker layer for the Node 16 Yarn era.

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
        return "node:16-bullseye"

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
export ELECTRON_SKIP_BINARY_DOWNLOAD=true
yarn install --ignore-engines || true
yarn compile || yarn build || true
./node_modules/.bin/lerna run --scope "@theia/!(example-)*" test --stream --concurrency=1 --no-bail 2>&1
""".format(repo=self.pr.repo),
            ),
        ]


@Instance.register("eclipse-theia", "theia_9936_to_14100")
class THEIA_9936_TO_14100(Instance):
    """Harness instance for eclipse-theia/theia — Node 16 Yarn era (PRs 10049–13306)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return TheiaNode16ImageDefault(self.pr, self._config)

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
# SHIPPABLE chunk1+chunk2 dataset to the THEIA_9936_TO_14100 config. Instance.create()
# looks up f"{org}/{number_interval}"; Instance.register returns the class
# unchanged, so it answers to every key (the era key + any prior keys above
# are kept for back-compat).
_BUNDLE_NUMBER_INTERVALS = [
    "1733-6580-7632-9260-10338-11020-11878-11911-11979-12015-12063-12101-12111-12146-12173-12174-12178-12188-12217-12218-12222-12224-12238-12242-12244-12248-12250-12253-12254-12255-12256-12260-12261-12265-12276-12283-12285-12291-12293-12296-12304-12305-12311-12314-12316-12322-12330-12336-12345-12357-12359-12361-12362-12363",
    "199-9514-10194-10443-10463-11314-11509-11512-11517-11519-11606-11616-11730-11732-11786-11851-11868-11921-11926-11931-11932-11945-11950-11956-11989-12003-12011-12013",
    "2018-4199-5667-10404-11837-11873-11893-12017-12019-12063-12171-12172-12185-12231-12240-12328-12334-12337-12364-12372-12374-12381-12384-12385-12389-12401-12404-12406-12410-12424-12427-12434-12436-12461-12467-12469-12471",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("eclipse-theia", _ni)(THEIA_9936_TO_14100)
