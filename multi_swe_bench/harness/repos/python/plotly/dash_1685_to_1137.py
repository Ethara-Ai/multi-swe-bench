import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DashBase_1685_1137(Image):
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
        return "python:2.7-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        # One shared tag per era. image_name() is org_m_repo for all four eras, so
        # the tag is what keeps them apart.
        return "base-1685-1137"

    def workdir(self) -> str:
        return "base-1685-1137"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "base_install.sh",
                """pip install --upgrade "pip<21" setuptools wheel || true
pip install "pytest<5" "pytest-mock<2" mock requests percy selenium "dash-core-components<2" "dash-html-components<2" dash-renderer || true
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

# plotly/dash shared base -- Python 2.7 era (bundles with max(prs_in_bundle) in 1137-1685)

FROM python:2.7-slim

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
        # An Image (not a string) -> this PR image is built FROM the shared era
        # base, so apt, the clone and the common pip deps are paid once per era
        # instead of once per PR.
        return DashBase_1685_1137(self.pr, self._config)

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
pip install --upgrade "pip<21" || true
###ACTION_DELIMITER###
pip install -e . || true
###ACTION_DELIMITER###
pip install "pytest<5" mock requests || true
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
echo 'pytest tests/ -vv --ignore=tests/unit/development/test_generate_class.py' > test_commands.sh
###ACTION_DELIMITER###
chmod +x test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
pytest tests/ -vv --ignore=tests/unit/development/test_generate_class.py --ignore=tests/unit/test_browser.py --ignore=tests/integration 2>&1; true

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
pytest tests/ -vv --ignore=tests/unit/development/test_generate_class.py --ignore=tests/unit/test_browser.py --ignore=tests/integration 2>&1; true

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
pytest tests/ -vv --ignore=tests/unit/development/test_generate_class.py --ignore=tests/unit/test_browser.py --ignore=tests/integration 2>&1; true

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


@Instance.register("plotly", "dash_1685_to_1137")
class DASH_1685_TO_1137(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    _DEP_ENSURE = 'pip uninstall -y pytest-rerunfailures pytest-sugar 2>/dev/null; pip install --upgrade "pip<21" 2>/dev/null; pip install -e . 2>/dev/null; pip install "pytest<5" "pytest-mock<2" mock requests percy selenium "dash-core-components<2" "dash-html-components<2" dash-renderer 2>/dev/null || true'
    _TEST_CMD = "pytest tests/ -vv --ignore=tests/unit/development/test_generate_class.py --ignore=tests/unit/test_browser.py --ignore=tests/integration --ignore=tests/test_integration.py 2>&1; true"

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
# [1137, 1685] -- the bounds encoded in this module's name -- resolving the
# overlap with the broader era configs by narrowest containing range. That rule
# reproduces all four module names exactly and agrees with each bundle's base
# version (v-test .. v1.9.1).
_OWNED_INTERVALS = [
    "859-860-862-863-864-867-872-874-881-890-892-894-896-899-901-903-918-923-926-927-935-936-937-939-940-942-944-947-948-949-950-952-953-955-957-964-967-969-973-974-979-981-983-996-1015-1018-1020-1026-1027-1032-1035-1037-1048-1066-1073-1074-1078-1080-1082-1086-1094-1103-1106-1109-1124-1126-1134-1138-1142-1145-1151-1156-1172-1174-1182-1186-1199-1201-1203-1212-1219-1220-1224-1228",  # PR #859, v-test..v1.12
    "1127-1130-1133-1135-1136-1137",  # PR #1127, v1.9.0..v1.9.1
    "1179-1371-1380-1384-1385-1389-1391",  # PR #1179, v1.15.0..v1.16.0
    "1180-1338-1341-1342-1349-1351-1352-1353-1355-1362-1368-1375-1377-1378",  # PR #1180, v1.14.0..v1.15.0
    "1185-1234-1237-1238-1239-1240-1248-1249-1254-1255-1276-1280-1288-1289-1290",  # PR #1185, v1.12..v1.13
    "1376-1399-1408-1409",  # PR #1376, v1.16.0..v1.16.1
    "1386-1397-1453-1454-1457-1483-1484-1486-1487-1488-1491-1493-1495-1496-1497-1498-1506-1507-1508-1525-1528-1530-1531-1534-1535-1536-1546-1550-1551-1553-1556-1562-1563-1567-1568-1569-1570-1576-1582-1583-1584-1585-1586-1588-1589-1611-1612-1614-1615-1616-1626-1628-1629-1630-1631-1632-1633-1634-1635-1636-1640-1643-1651-1652-1653-1655-1664-1675-1677-1680-1685",  # PR #1386, v1.20.0..v1.21.0
    "1415-1416",  # PR #1415, v1.16.1..v1.16.2
]

for _interval in _OWNED_INTERVALS:
    Instance.register("plotly", _interval)(DASH_1685_TO_1137)
