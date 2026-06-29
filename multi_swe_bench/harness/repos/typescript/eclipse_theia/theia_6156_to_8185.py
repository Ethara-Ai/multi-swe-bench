"""eclipse-theia/theia harness config — Node 12 Yarn era (PRs 6156–8185).

Theia is a TypeScript monorepo using Lerna.  This era uses Yarn Classic as the
package manager (yarn.lock present) and requires Node >= 12.14.1 with an upper
bound of ``< 13``, so ``node:12-buster`` is the appropriate base image.  Tests
run via Mocha through Lerna
(``lerna run --scope "@theia/!(example-)*" test``).
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TheiaNode12_6156ImageBase(Image):
    """Level 1: toolchain-only base image (shared per era).

    dependency() returns a *string* (the Node toolchain) and this Dockerfile
    carries NO ``# syntax`` directive, so the DockerfileEnhancer engages and
    prepends the ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image
    must NOT clone the repository -- a shared string-dependency image that
    clones is force-pinned to a single ``${BASE_COMMIT}`` and history-stripped
    by the enhancer, breaking ``git checkout`` for every other PR sharing the
    base. So the clone lives per-PR in TheiaNode12_6156ImageDefault. This image only provides the
    Node toolchain + apt deps for Theia's native-module headers.
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
        # bare "debian:buster"-style tags, so flag the node images here too.
        if any(s in base_img for s in ("buster", "stretch", "jessie")):
            return True
        return Image._is_deprecated_debian(base_img)

    def _era(self) -> str:
        # Per-era slug derived from the Node toolchain, e.g. node:10-buster ->
        # "node10". Distinct eras (node10 / node12) MUST get distinct base tags
        # so the shared base image name is not pinned to two Node versions.
        name, ver = self.dependency().split(":")
        return name + ver.split("-")[0].replace(".", "")

    def image_tag(self) -> str:
        return f"base-{self._era()}"

    def workdir(self) -> str:
        return f"base-{self._era()}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # Theia's native-module headers (electron/keytar/node-pty backends);
        # build-essential/git/python3 are in the default set below.
        return ["libx11-dev", "libxkbfile-dev", "libsecret-1-dev"]

    def dockerfile(self) -> str:
        base_img = self.dependency()

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # No `git clone` here on purpose (see docstring); no `# syntax` directive
        # either, so the DockerfileEnhancer injects the ARG/ENV/LABEL infra block
        # (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {base_img}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

{apt_command}

CMD ["/bin/bash"]
"""


class TheiaNode12_6156ImageDefault(Image):
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
        return TheiaNode12_6156ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail

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
                "install.sh",
                """\
#!/bin/bash
# Repo is already cloned + checked out at the base commit inline in the
# Dockerfile; install.sh warms the yarn install + compile cache (runs before
# the hardening strip, while the full clone is present).
set -e

cd /home/{repo}
git reset --hard

export PUPPETEER_SKIP_DOWNLOAD=true
export TS_NODE_TRANSPILE_ONLY=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=true
yarn install --frozen-lockfile --ignore-engines || true
yarn compile || yarn build || true

# Don't bake a transient node-`temp` log-config into the image: a partial
# /tmp/f-* JSON would be read by @theia/core's logger on later test runs.
rm -rf /tmp/f-* /tmp/d-* 2>/dev/null || true
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
# Delete-on-boot: clear stale node-`temp` artifacts in /tmp before tests run.
# @theia/core's logger-cli test writes a deliberately malformed JSON log-config
# via the `temp` pkg (/tmp/f-*); a leftover/partial one from a prior run makes
# the logger fail with "Error reading log config file ...: Unexpected token {{ in
# JSON" and the suite exits 1. Removing them lets each run recreate fresh.
rm -rf /tmp/f-* /tmp/d-* 2>/dev/null || true
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
# Delete-on-boot: clear stale node-`temp` artifacts in /tmp before tests run.
# @theia/core's logger-cli test writes a deliberately malformed JSON log-config
# via the `temp` pkg (/tmp/f-*); a leftover/partial one from a prior run makes
# the logger fail with "Error reading log config file ...: Unexpected token {{ in
# JSON" and the suite exits 1. Removing them lets each run recreate fresh.
rm -rf /tmp/f-* /tmp/d-* 2>/dev/null || true
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
yarn install --frozen-lockfile --ignore-engines || true
yarn compile || yarn build || true
# Delete-on-boot: clear stale node-`temp` artifacts in /tmp before tests run.
# @theia/core's logger-cli test writes a deliberately malformed JSON log-config
# via the `temp` pkg (/tmp/f-*); a leftover/partial one from a prior run makes
# the logger fail with "Error reading log config file ...: Unexpected token {{ in
# JSON" and the suite exits 1. Removing them lets each run recreate fresh.
rm -rf /tmp/f-* /tmp/d-* 2>/dev/null || true
./node_modules/.bin/lerna run --scope "@theia/!(example-)*" test --stream --concurrency=1 --no-bail 2>&1
""".format(repo=self.pr.repo),
            ),
        ]


@Instance.register("eclipse-theia", "theia_6156_to_8185")
class THEIA_6156_TO_8185(Instance):
    """Harness instance for eclipse-theia/theia — Node 12 Yarn era (PRs 6156–8185)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return TheiaNode12_6156ImageDefault(self.pr, self._config)

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
        spec reporter, producing::

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


# Route the bundled PR set (base PR 8185) that carries a dash-joined
# number_interval (the list of prs_in_bundle) to this era's config.
# Instance.create() looks up f"{org}/{number_interval}", so the interval
# string must be registered against this class.
Instance.register("eclipse-theia", "8185-8540-8579-8593-8645-8646-8674-8677-8680-8686-8688-8695-8698-8699-8700-8704-8710-8711-8715-8719-8720-8721-8725-8729-8732-8733-8734-8736-8741-8744-8756-8770-8784-8785")(THEIA_6156_TO_8185)


# Route the dash-joined number_interval (canonical prs_in_bundle from each
# record's resolved_issues + own PR number, sorted, "-"-joined) of the
# SHIPPABLE chunk1+chunk2 dataset to the THEIA_6156_TO_8185 config. Instance.create()
# looks up f"{org}/{number_interval}"; Instance.register returns the class
# unchanged, so it answers to every key (the era key + any prior keys above
# are kept for back-compat).
_BUNDLE_NUMBER_INTERVALS = [
    "938-4367-5673-6965-7273-7577-7745-8122-8209-8211-8354-8363-8378-8398-8413-8424-8426-8439-8451-8454-8465-8471-8475-8487-8496-8497-8500-8502-8505-8518-8558-8559",
    "5199-6100-6217-7464-7629-7989-8383-8536-8541-8689-8724-8739-8890-8921-8980-8989-9006-9012-9014-9015-9018-9021-9023-9033-9042-9047-9048-9057-9059-9060-9062-9073-9087-9089-9090-9113-9114-9119-9123-9127-9128-9130-16502",
    "2547-6846-7742-7775-10090-10164-10353-10394-10412-10416-10428-10439-10440-10459-10462-10464-10470-10478-10480-10484-10489-10490-10492-10493-10498-10500-10509-10515-10521-10530-10537-10543-10545-10547-10553-10554-10555-10557",
    "5609-5692-6867-7444-7608-7752-7899-8185-8186-8380-8620-8635-8639-8676-8681-8685-8687-8694-8696-8699-8702-8703-8714-8719-8720-8721-8725-8728-8733-8734-8741-8744-8755-8770-8784-8785",
    "938-4367-5673-7273-7577-7745-8122-8209-8211-8354-8363-8378-8398-8413-8424-8426-8439-8451-8454-8465-8471-8475-8487-8496-8497-8500-8502-8505-8518-8558-8559",
    "2547-6846-7742-10090-10164-10353-10394-10412-10416-10428-10439-10440-10459-10462-10464-10470-10478-10480-10484-10489-10490-10492-10493-10498-10500-10509-10515-10521-10530-10537-10543-10545-10547-10553-10554-10555-10557",
    "5609-5692-6867-7444-7608-7752-7899-8186-8380-8620-8635-8639-8676-8681-8685-8687-8694-8696-8699-8702-8703-8714-8719-8720-8721-8725-8728-8733-8734-8741-8744-8755-8770-8784-8785",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("eclipse-theia", _ni)(THEIA_6156_TO_8185)
