from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class NotcursesImageBaseDoctest(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-349_to_99999"

    def workdir(self) -> str:
        return "base-349_to_99999"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TERM=xterm \\
    COLORTERM=truecolor \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    cmake \\
    git \\
    pkg-config \\
    ca-certificates \\
    doctest-dev \\
    libncurses-dev \\
    libunistring-dev \\
    libdeflate-dev \\
    libreadline-dev \\
    libavformat-dev \\
    libavutil-dev \\
    libavcodec-dev \\
    libswscale-dev \\
    libavdevice-dev \\
    libqrcodegen-dev \\
    libopenimageio-dev \\
    python3-dev \\
    python3-setuptools \\
    python3-cffi \\
    util-linux \\
    zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class NotcursesImageDefaultDoctest(Image):
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
        return NotcursesImageBaseDoctest(self.pr, self._config)

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
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
rm -rf build
mkdir -p build && cd build
cmake .. -DUSE_DOCTEST=ON -DUSE_PANDOC=OFF -DCMAKE_BUILD_TYPE=Release 2>&1 || true
cmake --build . -j$(nproc) 2>&1 || true
ctest --output-on-failure 2>&1 || true
if [ -x ./notcurses-tester ]; then
  unset TERM
  echo "===== NOTCURSES TESTER XML ====="
  ./notcurses-tester --reporters=xml 2>&1 || true
  echo "===== END XML ====="
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git reset --hard {pr.base.sha}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
rm -rf build
mkdir -p build && cd build
cmake .. -DUSE_DOCTEST=ON -DUSE_PANDOC=OFF -DCMAKE_BUILD_TYPE=Release 2>&1 || true
cmake --build . -j$(nproc) 2>&1 || true
ctest --output-on-failure 2>&1 || true
if [ -x ./notcurses-tester ]; then
  unset TERM
  echo "===== NOTCURSES TESTER XML ====="
  ./notcurses-tester --reporters=xml 2>&1 || true
  echo "===== END XML ====="
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git reset --hard {pr.base.sha}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
rm -rf build
mkdir -p build && cd build
cmake .. -DUSE_DOCTEST=ON -DUSE_PANDOC=OFF -DCMAKE_BUILD_TYPE=Release 2>&1 || true
cmake --build . -j$(nproc) 2>&1 || true
ctest --output-on-failure 2>&1 || true
if [ -x ./notcurses-tester ]; then
  unset TERM
  echo "===== NOTCURSES TESTER XML ====="
  ./notcurses-tester --reporters=xml 2>&1 || true
  echo "===== END XML ====="
fi

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"
        # Per-PR FULL hardening (prepare.sh has checked out base.sha): strip refs +
        # gc-prune so future/fix commits are unreachable & deleted, then audit.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("dankamongmen", "notcurses_349_to_99999")
class NOTCURSES_349_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NotcursesImageDefaultDoctest(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean = re.sub(r"\x1b\[[?0-9;]*[a-zA-Z]", "", test_log)

        re_xml_testcase = re.compile(
            r'<TestCase\s+name="([^"]+)"[^>]*>(.*?)</TestCase>',
            re.DOTALL,
        )
        re_xml_failures = re.compile(
            r'<OverallResultsAsserts[^>]*failures="(\d+)"'
        )
        for m in re_xml_testcase.finditer(clean):
            name = m.group(1)
            body = m.group(2)
            fa = re_xml_failures.search(body)
            if fa and int(fa.group(1)) > 0:
                failed_tests.add(name)
            else:
                passed_tests.add(name)

        if not passed_tests and not failed_tests:
            re_doctest_failure_name = re.compile(r"TEST CASE:\s+(\S.*?)\s*$", re.MULTILINE)
            failing_named = re_doctest_failure_name.findall(clean)
            re_doctest_summary = re.compile(
                r"\[doctest\]\s*test cases:\s*\d+\s*\|\s*(\d+)\s*passed\s*\|\s*(\d+)\s*failed\s*\|\s*(\d+)\s*skipped"
            )
            sm = re_doctest_summary.search(clean)
            if sm:
                n_pass, n_fail, n_skip = (int(sm.group(i)) for i in (1, 2, 3))
                for nm in failing_named[:n_fail]:
                    failed_tests.add(nm.strip())
                while len(failed_tests) < n_fail:
                    failed_tests.add(f"doctest_failed_{len(failed_tests)}")
                for i in range(n_pass):
                    passed_tests.add(f"doctest_passed_{i}")
                for i in range(n_skip):
                    skipped_tests.add(f"doctest_skipped_{i}")

        re_ctest_pass = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"
        )
        re_ctest_fail_variants = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]
        re_ctest_skip = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Skipped\s+.*$"
        )
        for line in clean.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re_ctest_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
            for r in re_ctest_fail_variants:
                m = r.match(line)
                if m:
                    failed_tests.add(m.group(1).strip())
            m = re_ctest_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_NOTCURSES_349_TO_99999 = [
    '1060-1062-1070',
    '1075-1076',
    '1205-1206',
    '1208-1212-1217-1219',
    '1232-1239-1244-1253',
    '1281-1293',
    '1343-1347-1349-1351',
    '1514-1516-1520-1521',
    '1626-1632',
    '1825-1838-1842-1843-1846',
    '1859-1862',
    '1923-1926-1927',
    '1935-1937-1939-1940-1946-1952',
    '2011-2013',
    '2091-2122-2126',
    '2137-2139-2148',
    '2164-2166-2173-2176-2177-2179-2180',
    '2197-2198-2205-2206-2207',
    '2280-2292-2294-2296',
    '2303-2305-2312',
    '2520-2523-2528-2534-2536',
    '2543-2545-2549-2567-2569',
    '2618-2619-2623',
    '2625-2662-2663-2666-2668-2681-2687-2690',
    '2629-2633-2640-2648',
    '2886-2891-2901-2902-2903',
    '385-396',
    '710-713-715',
    '832-837-841-844-845-848',
    '900-902-907-908-922-931-932-937-938-939-944',
    '973-975-977-978',
    '991-996-999-1001-1005',
    '1008-1016-1018-1019',
    '1027-1029-1032-1035',
    '1037-1040-1041-1042-1048-1050',
    '1083-1085-1087-1102',
    '1118-1128-1129',
    '1132-1133-1134-1136-1137-1141-1142',
    '1145-1146-1147-1149-1152-1157-1158-1166-1167',
    '1169-1170-1173-1178-1181',
    '1270-1279',
    '1317-1321',
    '1355-1358-1361-1365-1372-1377-1379-1383-1384-1385',
    '1394-1398-1403-1404-1407-1409-1410-1414-1415-1418-1427-1428-1429-1431-1442-1458-1463-1466-1467-1471-1473-1475-1477-1480',
    '1485-1491-1494-1499-1502',
    '1498-1640-1647-1651-1660-1662-1663-1667-1669-1670',
    '1535-1536-1541-1544-1549-1551',
    '1567-1573-1586-1589-1591-1597-1604-1606-1609-1610-1621-1623',
    '1652-1672-1674-1683-1685-1691-1693-1697-1706-1709-1713',
    '1717-1725-1727-1729-1730-1731-1736-1737-1742-1748-1749-1755',
    '1760-1764-1765-1776-1779-1785-1786-1787-1792-1794-1799-1802-1803-1809-1813-1815',
    '1876-1885-1889-1897-1911-1919-1920',
    '1953-1957-1965-1968-1971-1979-1988-1995',
    '1978-2041-2048-2053-2056-2061',
    '2236-2239-2242-2243',
    '2246-2250-2251-2253-2259-2263-2274',
    '2427-2436-2437-2441-2444-2447-2449-2460-2461-2462-2463',
    '2467-2468-2469-2470-2477-2478-2481-2485-2487-2488-2490-2491-2492-2493',
    '2483-2497-2500-2501-2506-2514',
    '2570-2576-2580-2586-2588-2589-2598-2599-2600-2602-2609-2610-2612-2613-2614-2615-2616-2617',
    '2696-2698-2700-2706-2707-2712-2713-2716-2724-2737-2764-2769-2781-2782-2783-2788',
    '2743-2765-2768-2806-2808-2820-2823-2824-2825',
    '2845-2846-2851-2864-2871',
    '349-353-354-358',
    '369-372-384',
    '399-400-404-405-406-411-412-415-420',
    '416-423-424-426-433-434-435-437-440',
    '442-444-452-454-456',
    '471-472-477-479-480-481-490-495-500-501-502',
    '524-531-533-534-542',
    '555-567',
    '583-587-588-592',
    '597-600-602-608',
    '626-632-633-640-644-647-653-658-663-665-677',
    '684-690',
    '723-724',
    '730-731',
    '747-755-758-760',
    '776-783',
    '791-794-795',
    '808-816-820-821',
    '852-855-860-863-864-867-868-870-873-875-880',
    '885-887-893-895',
    '950-954-955-956-957',
    '966-968',
]
for _ni in _BUNDLE_NIS_NOTCURSES_349_TO_99999:
    Instance.register('dankamongmen', _ni)(NOTCURSES_349_TO_99999)
