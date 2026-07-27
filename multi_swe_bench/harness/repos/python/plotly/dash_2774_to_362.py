import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DashBase_2774_362(Image):
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
        return "base-2774-362"

    def workdir(self) -> str:
        return "base-2774-362"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "base_install.sh",
                """pip install --upgrade pip setuptools wheel || true
pip install "setuptools<70" || true
pip install "werkzeug<2.1" "Flask<2.3" "pytest>=4.6,<5" "pytest-mock<3.12" || true
pip install pyyaml mock six flaky flask-talisman numpy dash-dangerously-set-inner-html dash-renderer "dash-core-components<2" "dash-html-components<2" selenium || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        # Aligned with multi_swe_bench/harness/image.py -- see the notes on the PR
        # image below. Carries the syntax directive, so DockerfileEnhancer.enhance()
        # returns it unchanged; clones via ${REPO_URL} (passed as a build-arg
        # because dependency() is a string) and declares BASE_COMMIT purely to
        # consume the build-arg build_dataset always sends.
        packages = ['ca-certificates', 'curl', 'build-essential', 'git', 'gnupg', 'make', 'sudo', 'wget', 'libxml2-dev', 'libxslt-dev', 'pkg-config', 'zlib1g-dev']
        template = """# syntax=docker/dockerfile:1.6

# plotly/dash shared base -- Python 3.6 era (bundles with max(prs_in_bundle) in 362-2774)

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


class ImageDefault_2774_362(Image):
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
        return DashBase_2774_362(self.pr, self._config)

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
apt-get update && apt-get install -y nodejs npm curl libxml2-dev libxslt-dev gcc g++ pkg-config zlib1g-dev || true
###ACTION_DELIMITER###
npm install -g n && n lts || true
###ACTION_DELIMITER###
hash -r
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel || true
###ACTION_DELIMITER###
pip install "werkzeug<2.1" "Flask<2.3" || true
###ACTION_DELIMITER###
pip install -e . || true
###ACTION_DELIMITER###
pip install -r requires-ci.txt 2>/dev/null || pip install -r requirements/ci.txt 2>/dev/null || true
###ACTION_DELIMITER###
pip install -r requires-testing.txt 2>/dev/null || pip install -r requirements/testing.txt 2>/dev/null || true
###ACTION_DELIMITER###
pip install mock six flaky flask-talisman numpy redis dash-dangerously-set-inner-html pytest pytest-mock multiprocess psutil 2>/dev/null || true
###ACTION_DELIMITER###
pip install "pytest>=4.6,<5" "pytest-mock<3.12" 2>/dev/null || true
###ACTION_DELIMITER###
cd /home/[[REPO_NAME]] && for comp in dash-test-components dash-generator-test-component-nested dash-generator-test-component-standard dash-generator-test-component-typescript; do if [ -d "@plotly/$comp" ]; then cd "@plotly/$comp" && npm ci 2>/dev/null && npm run build 2>/dev/null && pip install -e . 2>/dev/null; cd /home/[[REPO_NAME]]; fi; done || true
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
echo 'prepare done'""".replace("[[REPO_NAME]]", repo_name),
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


@Instance.register("plotly", "dash_2774_to_362")
class DASH_2774_TO_362(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_2774_362(self.pr, self._config)

    _DEP_ENSURE = 'pip uninstall -y pytest-rerunfailures pytest-sugar 2>/dev/null; pip install "setuptools<70" 2>/dev/null; pip install "werkzeug<2.1" "Flask<2.3" "pytest>=4.6,<5" "pytest-mock<3.12" 2>/dev/null; pip install -e . 2>/dev/null; pip install pyyaml mock six flaky flask-talisman numpy "pytest>=4.6,<5" "pytest-mock<3.12" dash-dangerously-set-inner-html dash-renderer "dash-core-components<2" "dash-html-components<2" selenium 2>/dev/null || true'
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
# [362, 2774] -- the bounds encoded in this module's name -- resolving the
# overlap with the broader era configs by narrowest containing range. That rule
# reproduces all four module names exactly and agrees with each bundle's base
# version (v0.28.6 .. v2.9.3).
_OWNED_INTERVALS = [
    "352-362",  # PR #352, v0.29.0..v0.30.0
    "367-436-622-623-627",  # PR #367, v0.38.0..v0.39.0
    "444-450",  # PR #444, v0.28.6..v0.29.0
    "451-461-473-477-478",  # PR #451, v0.30.0..v0.32.0
    "483-509-511-513-515-518",  # PR #483, v0.34.0..v0.35.1
    "492-597-601-603-605-607-608-610-611",  # PR #492, v0.37.0..v0.38.0
    "524-547-548",  # PR #524, v0.35.2..v0.35.3
    "545-558-561-563-565-566-569-573-574-575",  # PR #545, v0.36.0..v0.37.0
    "620-636-646",  # PR #620, v0.39.0..v0.40.0
    "638-669-670-672-675-676-679-680",  # PR #638, v0.40.0..v0.41.0
    "685-687-690-692-717-718",  # PR #685, v0.41.0..v0.43.0
    "714-721-722-724-725-726-737-739-740-744-750-753-761-764-767-768-770-773-774-780-785",  # PR #714, v0.43.0..v1.0.0
    "772-782-783-786-796-801-805-808-812-814-815",  # PR #772, v1.0.0..v1.0.1
    "778-818-819-821-822",  # PR #778, v1.0.1..v1.0.2
    "817-825-827-835-836-845-848-849",  # PR #817, v1.0.2..v1.1.0
    "910-915",  # PR #910, v1.2.0..v1.3.0
    "986-987",  # PR #986, v1.5.0..v1.5.1
    "1000-1001-1006",  # PR #1000, v1.6.0..v1.6.1
    "1743-1745-1753-1763-1768-1778-1779-1788-1789-1792-1798-1801-1804-1815-1822-1825-1836-1857-1858-1866-1869-1872-1873-1876-1879-1883-1886-1887-1894-1895-1901-1902-1903",  # PR #1743, v2.0.0..v2.1.0
    "1751-1762-1839-1939-1952-1956-1967-1968-1970-1976-2006-2009-2013-2015-2016-2024-2027-2029-2032-2034-2035-2036-2041-2042",  # PR #1751, v2.3.1..v2.4.0
    "1891-1911-1923-1926-1929-1930-1931-1932-1935-1936-1938",  # PR #1891, v2.1.0..v2.2.0
    "1915-1937-1942-1945-1948-1949-1953-1954",  # PR #1915, v2.2.0..v2.3.0
    "1963-1969-1995",  # PR #1963, v2.3.0..v2.3.1
    "2039-2097-2098-2100-2102-2104-2109-2110-2113-2114-2116-2120-2126-2131-2134-2136-2137-2138",  # PR #2039, v2.5.1..v2.6.0
    "2068-2260-2349-2392-2393-2414-2415-2417-2424-2425-2426-2429-2435-2441-2446-2450-2453-2458-2459",  # PR #2068, v2.8.1..v2.9.0
    "2084-2087-2089-2090-2092-2093-2094",  # PR #2084, v2.5.0..v2.5.1
    "2105-2257-2261-2265-2277-2282-2287-2289-2291-2292-2293-2298-2299",  # PR #2105, v2.6.2..v2.7.0
    "2146-2148-2159-2167-2168-2175-2178-2179",  # PR #2146, v2.6.0..v2.6.1
    "2152-2182-2187-2188-2194-2202-2206-2217-2218-2223-2226-2237-2238-2243-2247",  # PR #2152, v2.6.1..v2.6.2
    "2207-2468-2472-2474-2500-2508-2513-2520-2531-2533-2538-2540-2543-2544",  # PR #2207, v2.9.3..v2.10.0
    "2303-2321-2332-2336-2344-2361-2363-2364-2365",  # PR #2303, v2.7.0..v2.7.1
    "2327-2366-2367-2388-2389-2394-2395-2396",  # PR #2327, v2.7.1..v2.8.0
    "2461-2464-2466",  # PR #2461, v2.9.0..v2.9.1
    "2471-2473-2476-2479-2481-2482-2483",  # PR #2471, v2.9.1..v2.9.2
    "2489-2491-2498-2501-2506-2507",  # PR #2489, v2.9.2..v2.9.3
    "2530-2555-2565-2572-2574-2575-2576-2577",  # PR #2530, v2.10.2..v2.11.0
    "2573-2578-2579-2581-2582",  # PR #2573, v2.11.0..v2.11.1
    "2589-2593-2596-2599-2603-2604-2605-2616-2617-2619-2621-2622-2623",  # PR #2589, v2.11.1..v2.12.0
    "2610-2630-2632-2633",  # PR #2610, v2.12.1..v2.13.0
    "2625-2626",  # PR #2625, v2.12.0..v2.12.1
    "2634-2635-2647-2649-2655-2659-2661-2662",  # PR #2634, v2.13.0..v2.14.0
    "2652-2695-2721-2723-2732-2737-2739",  # PR #2652, v2.14.2..v2.15.0
    "2700-2703",  # PR #2700, v2.14.1..v2.14.2
    "2730-2734-2735-2747-2748-2752-2753-2755-2756-2758-2762-2770-2771-2773-2774",  # PR #2730, v2.15.0..v2.16.0
]

for _interval in _OWNED_INTERVALS:
    Instance.register("plotly", _interval)(DASH_2774_TO_362)
