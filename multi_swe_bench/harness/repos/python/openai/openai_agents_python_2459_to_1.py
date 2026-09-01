import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

class ImageBase(Image):
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
        return "python:3.11-slim"

    def image_tag(self) -> str:
        return "base-2459-to-1"

    def workdir(self) -> str:
        return "base-2459-to-1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the base to a single PR's
        # base.sha and breaking every other PR in the era with
        # "reference is not a tree". The base keeps full history; the strict
        # anti-reward-hack hardening runs per-PR (see ImageDefault).
        # PIPELINE.md 2a/8.1: this repo is `# syntax`-opt-out (the enhancer would
        # prune the shared base to one PR's sha), so the canonical MITM block is
        # referenced from image.py by hand. Referencing - not copying - keeps
        # image.py the single source of truth (8.2) and guarantees the generated
        # Dockerfile matches its constants verbatim.
        mitm_args = DockerfileEnhancer._PROXY_ARGS
        mitm_env = DockerfileEnhancer._ENV_BLOCK
        mitm_certs = DockerfileEnhancer._CERT_SYMLINKS

        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

{mitm_args}

{mitm_env}

ENV LC_ALL=C.UTF-8 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl wget ca-certificates \\
    build-essential gcc g++ python3-dev \\
    linux-libc-dev rclone \\
    && rm -rf /var/lib/apt/lists/*

{mitm_certs}

# uv pinned (not `latest`): the resolver version decides the dependency set, so
# an unpinned uv makes rebuilds non-reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""

# Warm the uv cache at the default branch so each PR layer's `uv sync` only
# resolves the delta. Best-effort: the per-PR sync in prepare.sh is what counts.
RUN uv sync --all-extras --all-packages --group dev || uv sync || true

WORKDIR /home/

CMD ["/bin/bash"]
"""


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
        return ImageBase(self.pr, self.config)

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
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
git clean -fdx -e .venv
git checkout {self.pr.base.sha}
uv sync --all-extras --all-packages --group dev || uv sync || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
uv run pytest -v
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
uv run pytest -v
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
uv run pytest -v
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        # PIPELINE.md 2a/8.1: this repo is `# syntax`-opt-out (the enhancer would
        # prune the shared base to one PR's sha), so the canonical MITM block is
        # referenced from image.py by hand. Referencing - not copying - keeps
        # image.py the single source of truth (8.2) and guarantees the generated
        # Dockerfile matches its constants verbatim.
        mitm_args = DockerfileEnhancer._PROXY_ARGS
        mitm_env = DockerfileEnhancer._ENV_BLOCK
        mitm_certs = DockerfileEnhancer._CERT_SYMLINKS

        return f"""# syntax=docker/dockerfile:1.6
FROM {dep.image_name()}:{dep.image_tag()}

{mitm_args}

{mitm_env}
{self.global_env}
COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("openai", "openai-agents-python_2459_to_1")
class OPENAI_AGENTS_PYTHON_2459_TO_1(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest -v verbose output, e.g.:
        #   tests/test_agent_config.py::test_system_instructions PASSED  [  0%]
        #   tests/test_agent_hooks.py::test_streamed_agent_hooks FAILED  [  2%]
        #   tests/extensions/memory/test_redis_session.py::test_x SKIPPED [ 11%]
        passed_pattern = re.compile(
            r"^(.+?)\s+PASSED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        passed_tests.update(passed_pattern.findall(clean_log))

        skipped_pattern = re.compile(
            r"^(.+?)\s+SKIPPED\s+(?:\[\s*\d+%\s*\]|\[\d+\])", re.MULTILINE
        )
        skipped_tests.update(skipped_pattern.findall(clean_log))

        # Inline verbose failure line: "<nodeid> FAILED [ 2%]"
        failed_inline = re.compile(
            r"^(.+?)\s+FAILED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        failed_tests.update(failed_inline.findall(clean_log))

        # Summary section: "FAILED <nodeid> - <reason>" / "ERROR <nodeid>"
        failed_summary = re.compile(
            r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE
        )
        failed_tests.update(failed_summary.findall(clean_log))

        # Dedup: worst result wins
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Registered so delivered records (which carry the dash-joined number_interval)
# resolve to this class (PIPELINE §11/§11c). Trimmed to the RESOLVED set
# (delivery-time subset); the era key above still routes the build dataset.
_BUNDLE_NIS_OPENAI_ERA1 = [
    "242-249-255-264-265-266-267",
    "262-639-763-861-871-897-903-909-920-923-925-928-930-935-936-937-938-950-951-952-958-960-963",
    "439-452-457-460-463-465-475-483-484-486-496-500-503-504-505-506-507-508-509-513-514",
    "550-573-579-580-582-589-590-593",
    "598-1162-1169-1170",
    "766-811-814-815-817-818-842-872-874-876-878",
    "974-1206-1250-1278-1292-1296-1302-1307-1308-1309-1310-1313-1319-1321-1322-1326-1329-1330-1332-1336-1339-1341-1355-1356-1360-1366-1368-1369-1370-1388-1398",
    "998-1354-1382-1399-1426-1439-1440-1458-1461-1462-1469-1470-1471-1472-1473-1480",
    "999-1101-1134-1135-1139-1141-1142-1149-1150-1151-1153-1157",
    "1192-1300-1535-1548-1549-1558-1561-1562-1563-1576-1577-1582-1586-1587-1589-1590-1599-1600-1601-1602-1607-1610",
    "1298-1537-1550-1646-1657-1665-1667-1669-1682-1683-1684-1685-1687-1688-1689-1691-1693-1695-1696-1700-1710-1717",
    "1475-1476-1478-1479-1482-1483-1484-1490-1495-1500-1501",
    "1619-1624-1626-1627-1628-1633-1636-1637-1641-1642-1643-1647-1648-1649-1650-1654-1655",
    "1662-1785-1792-1798-1809-1810-1811-1812-1813-1816-1818-1819-1820-1821-1825-1826-1835-1836-1837-1838",
    "1804-1986-1996-2014-2033-2082-2091-2092-2093-2095",
    "1894-1921-1922-1931-1932-1933-1934-1936-1947-1952-1955-1956-1960-1962-1963-1965-1967-1968-1971",
    "1972-1979-1981-1982-1984-1988",
    "2019-2026-2044-2047-2077-2079-2080",
    "2108-2112-2116-2117-2126-2128-2131-2139-2141-2142-2144-2147-2152-2153",
    "2158-2170-2207-2209-2210-2212-2213-2214-2215-2219-2225-2226-2227-2229-2235-2238-2243-2260-2262-2263",
    "2169-2174-2179-2182-2184-2188-2189-2191-2192-2193-2194-2197-2198",
    "2264-2272-2282-2327-2338-2339-2340-2341-2342-2344-2345-2350",
    "2299-2307-2312-2316-2318-2319-2320-2322-2323",
    "2334-2336-2337",
    "2405-2420-2423-2424-2425-2431-2433",
    "2432-2434-2435-2438-2443-2446-2447-2448",
    "2440-2452-2453-2454-2455-2458-2460-2463-2464-2465-2466-2469",
    "2456-2468-2473-2474-2475-2478-2479-2480",
]

for _ni in _BUNDLE_NIS_OPENAI_ERA1:
    Instance.register("openai", _ni)(OPENAI_AGENTS_PYTHON_2459_TO_1)
