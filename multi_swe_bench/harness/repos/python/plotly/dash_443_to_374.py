import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DashBase_443_374(Image):
    """Shared base image for this era: apt packages, a full clone of the repo, and
    the era's common third-party pip deps -- everything that does NOT depend on a
    particular PR's commit.

    Built ONCE and reused by every PR image in this era: Image equality/dedup is on
    image_full_name(), and build_dataset walks the dependency chain, so all N PR
    images of an era resolve to this single parent. Deliberately does NO checkout
    and NO hardening -- it holds full history on purpose; the per-PR image checks
    out its own sha and strips the history there.
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
        return "python:3.6-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        # One shared tag per era. image_name() is org_m_repo for all four eras, so
        # the tag is what keeps them apart.
        return "base-443-374"

    def workdir(self) -> str:
        return "base-443-374"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "base_install.sh",
                """pip install --upgrade pip setuptools wheel || true
pip install mock six flaky "pytest>=3,<5" pytest-mock "dash-html-components<2" "dash-core-components<2" dash-renderer || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        # Aligned with multi_swe_bench/harness/image.py -- see the notes on the PR
        # image below. Carries the syntax directive, so DockerfileEnhancer.enhance()
        # returns it unchanged; clones via ${REPO_URL} (passed as a build-arg
        # because dependency() is a string) and declares BASE_COMMIT purely to
        # consume the build-arg build_dataset always sends.
        packages = ['ca-certificates', 'curl', 'build-essential', 'git', 'gnupg', 'make', 'sudo', 'wget']
        template = """# syntax=docker/dockerfile:1.6

# plotly/dash shared base -- Python 3.6 era (bundles with max(prs_in_bundle) in 374-443)

FROM python:3.6-slim

ARG TARGETARCH
ARG REPO_URL="__REPO_URL__"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ shared base image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN set -eux; \\
    if ! apt-get update; then \\
        sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list; \\
        sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list; \\
        sed -i '/stretch-updates/d;/buster-updates/d;/jessie-updates/d' /etc/apt/sources.list; \\
        apt-get update; \\
    fi; \\
    apt-get install -y --no-install-recommends \\
__PACKAGES__; \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/__REPO__

COPY base_install.sh /home/base_install.sh
RUN bash /home/base_install.sh || true

CMD ["/bin/bash"]
"""
        return (
            template.replace(
                "__PACKAGES__", " \\\n".join(f"        {p}" for p in packages)
            )
            .replace("__REPO_URL__", f"https://github.com/{self.pr.org}/{self.pr.repo}.git")
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class ImageDefault_443_374(Image):
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
        # An Image (not a string) -> this PR image is built FROM the shared era
        # base, so apt, the clone and the common pip deps are paid once per era
        # instead of once per PR.
        return DashBase_443_374(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                """ls
###ACTION_DELIMITER###
apt-get update && apt-get install -y nodejs npm curl gcc g++ || true
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel || true
###ACTION_DELIMITER###
pip install -r requirements.txt 2>/dev/null || true
###ACTION_DELIMITER###
pip install -e . || true
###ACTION_DELIMITER###
pip install mock six flaky pytest pytest-mock 2>/dev/null || true
###ACTION_DELIMITER###
pip install "pytest>=3,<5" 2>/dev/null || true
###ACTION_DELIMITER###
###ACTION_DELIMITER###
# Generate the bundled component packages dash/html, dash/dcc and dash/dash_table
# (dash 2.0+ monorepo). Without them pytest aborts at collection with
# "cannot import name 'Div' from 'dash.html' (unknown location)".
#
# Guarded on dash/development/update_components.py, the monorepo marker, so this is
# a clean no-op on dash 0.x/1.x (where components are separate pip packages).
#
# Why not just `npm run build`: the dash build orchestrates the three component
# builds through `lerna exec npm run build`, and update_components.py does
# sys.exit(1) if that returns non-zero -- BEFORE copying the (already-generated)
# python packages into dash/. The lerna path returns non-zero here (a `postbuild`
# es-check es5 gate rejects the newer webpack/terser output, plus lerna concurrency
# flakiness), even though each component builds fine on its own. So we build the
# renderer and each component STANDALONE (with the es-check gate stripped -- it is a
# lint check on the minified bundle, irrelevant to the generated python classes) and
# copy the artifacts into dash/ ourselves, exactly mirroring update_components.py's
# copy loop. node 20 (dash 3.x CI's version) is required -- node 24 from `n lts`
# fails the native gyp build during `npm ci`.
if [ -f dash/development/update_components.py ] && command -v n >/dev/null 2>&1; then \
  n 20 >/dev/null 2>&1 || true; hash -r 2>/dev/null || true; \
  pip install coloredlogs 2>/dev/null || true; \
  pip install -r requirements/dev.txt 2>/dev/null || true; \
  (npm ci || npm install) 2>/dev/null || true; \
  (cd dash/dash-renderer && (npm ci || npm install) 2>/dev/null && npm run build 2>/dev/null) || true; \
  for c in dash-core-components dash-html-components dash-table; do \
    [ -d "components/$c" ] || continue; \
    (cd "components/$c" && npm pkg delete scripts.postbuild 2>/dev/null; (npm ci || npm install) 2>/dev/null; npm run build 2>/dev/null) || true; \
    pyp=$(echo "$c" | tr - _); \
    case "$c" in dash-core-components) dst=dcc;; dash-html-components) dst=html;; *) dst=dash_table;; esac; \
    if [ -d "components/$c/$pyp" ]; then rm -rf "dash/$dst"; cp -r "components/$c/$pyp" "dash/$dst"; fi; \
  done; \
  git checkout -- . 2>/dev/null || true; \
  pip install -e . 2>/dev/null || true; \
fi
echo 'prepare done'""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        base = self.dependency()

        # Aligned with multi_swe_bench/harness/image.py:
        #  * dependency() returns an Image, so DockerfileEnhancer.enhance() returns
        #    this file UNCHANGED ("if not isinstance(dep, str): return raw") -- no
        #    proxy / CA-cert / MITM injection, and no rewriting of the fetch.
        #  * build_dataset only passes the BASE_COMMIT build-arg for STRING
        #    dependencies, so this PR image bakes its own sha as the ARG default.
        #  * embeds Image._HARDENING_BLOCK right after the checkout, so the fix
        #    commit and all later history cannot be read back out of the image.
        # The clone, apt and common pip deps already came from the shared base.
        template = """# syntax=docker/dockerfile:1.6

# plotly/dash PR image -- FROM the shared era base, checked out at this PR's sha

FROM __BASE__

ARG BASE_COMMIT="__BASE_COMMIT__"

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

__HARDENING__

__COPY__

RUN bash /home/prepare.sh || true

CMD ["/bin/bash"]
"""
        return (
            template.replace("__BASE__", base.image_full_name())
            .replace("__HARDENING__", Image._HARDENING_BLOCK.strip("\n"))
            .replace("__COPY__", copy_commands.strip("\n"))
            .replace("__BASE_COMMIT__", self.pr.base.sha)
            .replace("__REPO__", self.pr.repo)
        )


@Instance.register("plotly", "dash_443_to_374")
class DASH_443_TO_374(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_443_374(self.pr, self._config)

    _DEP_ENSURE = 'pip uninstall -y pytest-rerunfailures pytest-sugar 2>/dev/null; pip install -r requirements.txt 2>/dev/null; pip install -e . 2>/dev/null; pip install mock six flaky "pytest>=3,<5" pytest-mock "dash-html-components<2" "dash-core-components<2" dash-renderer 2>/dev/null || true'
    _TEST_CMD = "pytest tests/ -vv --ignore=tests/integration --ignore=tests/test_integration.py 2>&1; true"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return f"bash -c 'cd /home/dash && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return f"bash -c 'cd /home/dash && git apply --whitespace=nowarn /home/test.patch && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return f"bash -c 'cd /home/dash && git apply --whitespace=nowarn /home/test.patch /home/fix.patch && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        pattern = r"(tests/[^:]+::[^\s]+)\s+(PASSED|FAILED|ERROR|SKIPPED)|(PASSED|FAILED|ERROR|SKIPPED)\s+(tests/[^:]+::[^\s]+)"
        for line in log.splitlines():
            match = re.search(pattern, line)
            if not match:
                continue
            test = match.group(1) or match.group(4)
            status = match.group(2) or match.group(3)
            if not (test and status):
                continue
            if status == "PASSED":
                passed_tests.add(test)
            elif status in ["FAILED", "ERROR"]:
                failed_tests.add(test)
            elif status == "SKIPPED":
                skipped_tests.add(test)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === number_interval routing: bundles owned by this era config ===============
# Each entry is a dash-joined `prs_in_bundle` from
# Plotly/dataset/plotly__dash_lht_final.jsonl. Instance.create() routes on
# f"{org}/{number_interval}", so a dataset carrying the dash-joined value
# dispatches straight to this config -- no range heuristic, no cross-file table.
#
# Ownership rule: a bundle belongs here when max(prs_in_bundle) falls in
# [374, 443] -- the bounds encoded in this module's name -- resolving the
# overlap with the broader era configs by narrowest containing range. That rule
# reproduces all four module names exactly and agrees with each bundle's base
# version (v0.19.0 .. v0.28.6).
_OWNED_INTERVALS = [
    "163-164-165-166-167-168-169-172-173-174-176-181-184-185-186-190-191-193-196-199-200-203-206-207-212-237-238-251-252-256-273-276-286-294-305-309-314-316-318-320-322-325-333-335-336-338-343-346-351-365-372-374",  # PR #163, v0.19.0..v0.26.5
    "377-379-418",  # PR #377, v0.28.1..v0.28.3
    "432-439-443",  # PR #432, v0.28.5..v0.28.6
]

for _interval in _OWNED_INTERVALS:
    Instance.register("plotly", _interval)(DASH_443_TO_374)
