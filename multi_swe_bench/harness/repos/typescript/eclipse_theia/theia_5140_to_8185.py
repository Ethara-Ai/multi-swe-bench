"""eclipse-theia/theia harness config — Node 10 Yarn era (PRs 5451–7908).

Theia is a TypeScript monorepo using Lerna.  This era uses Yarn Classic as the
package manager (yarn.lock present) and requires Node >= 10.2.0.  The repo pins
``node >= 10.2.0`` to ``>= 10.11.0`` with an upper bound of ``< 12`` over this
range, so ``node:10-buster`` is the appropriate base image.  Tests run via Mocha
through Lerna (``lerna run --scope "@theia/!(example-)*" test``).
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TheiaNode10ImageBase(Image):
    """Level 1: toolchain-only base image (shared per era).

    dependency() returns a *string* (the Node toolchain) and this Dockerfile
    carries NO ``# syntax`` directive, so the DockerfileEnhancer engages and
    prepends the ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image
    must NOT clone the repository -- a shared string-dependency image that
    clones is force-pinned to a single ``${BASE_COMMIT}`` and history-stripped
    by the enhancer, breaking ``git checkout`` for every other PR sharing the
    base. So the clone lives per-PR in TheiaNode10ImageDefault. This image only provides the
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
        return "node:10-buster"

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


class TheiaNode10ImageDefault(Image):
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
        return TheiaNode10ImageBase(self.pr, self._config)

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


@Instance.register("eclipse-theia", "theia_5140_to_8185")
class THEIA_5140_TO_8185(Instance):
    """Harness instance for eclipse-theia/theia — Node 10 Yarn era (PRs 5451–7908)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return TheiaNode10ImageDefault(self.pr, self._config)

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


# Route the bundled PR set (base PR 5451) that carries a dash-joined
# number_interval (the list of prs_in_bundle) to this era's config.
# Instance.create() looks up f"{org}/{number_interval}", so the interval
# string must be registered against this class.
Instance.register("eclipse-theia", "5451-5976-6247-6252-6266-6268-6270-6271-6277-6279-6280-6281-6283-6285-6290-6291-6293-6301-6302-6304-6307-6310-6312-6313-6318-6321-6323-6326-6328-6331-6334-6335-6341-6342-6345-6351-6352-6354-6356-6364-6365-6369-6378-6382-6388-6397-6398-6403-6405-6411-6413-6419-6422-6426-6436-6437-6443-6450-6469-6471-6476-6477")(THEIA_5140_TO_8185)


# Route the dash-joined number_interval (canonical prs_in_bundle from each
# record's resolved_issues + own PR number, sorted, "-"-joined) of the
# SHIPPABLE chunk1+chunk2 dataset to the THEIA_5140_TO_8185 config. Instance.create()
# looks up f"{org}/{number_interval}"; Instance.register returns the class
# unchanged, so it answers to every key (the era key + any prior keys above
# are kept for back-compat).
_BUNDLE_NUMBER_INTERVALS = [
    "904-3409-4048-4285-4436-4972-5013-5229-5451-5578-5836-6134-6204-6212-6228-6245-6270-6271-6277-6279-6280-6283-6285-6288-6290-6293-6295-6301-6305-6307-6310-6312-6318-6319-6325-6334-6335-6341-6342-6345-6351-6354-6355-6364-6369-6377-6380-6384-6388-6393-6396-6397-6399-6410-6413-6426-6432-6443-6449-6468-6471-6476-6477",
    "4488-5471-6108-6350-6550-6678-6691-6803-6847-6855-6869-6905-6910-6919-6933-6968-6980-6996-7010-7022-7026-7028-7031-7032-7034-7035-7042-7051-7056-7064-7077-7084-7091-7093-7096-7101-7102-7109-7111-7113-7118-7122-7128-7129-7130-7134-7146-7155-7163-7167-7173-7179-7180-7184-7197-7199-7200-7204-7205-7208-7209-7216-7228-7230-7232-7236-7239",
    "904-3409-4048-4285-4436-4972-5013-5229-5578-5836-6134-6204-6212-6228-6245-6270-6271-6277-6279-6280-6283-6285-6288-6290-6293-6295-6301-6305-6307-6310-6312-6318-6319-6325-6334-6335-6341-6342-6345-6351-6354-6355-6364-6369-6377-6380-6384-6388-6393-6396-6397-6399-6410-6413-6426-6432-6443-6449-6468-6471-6476-6477",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("eclipse-theia", _ni)(THEIA_5140_TO_8185)
