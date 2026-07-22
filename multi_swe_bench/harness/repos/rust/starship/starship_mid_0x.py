from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class StarshipMid0xImageBase(Image):
    """Shared TOOLCHAIN-ONLY base for the mid-0.x era (rust:1.56.0).

    Contains NO ``git clone`` on purpose: DockerfileEnhancer._inject_final_sanitize()
    only injects the history-stripping hardening when the Dockerfile mentions
    git clone/fetch/remote add. With no clone this image is never pinned to a
    BASE_COMMIT and never has its origin removed, so it is safely reusable by
    every PR in the era. The per-PR image does clone + checkout + hardening itself.
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
        return "rust:1.56.0"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-mid0x"

    def workdir(self) -> str:
        return "base-mid0x"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """
FROM rust:1.56.0

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for starship
RUN apt-get update && apt-get install -y git cmake pkg-config libssl-dev

WORKDIR /home/
"""


class StarshipMid0xImageDefault(Image):
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
        return StarshipMid0xImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard

cargo test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
cargo test

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_ref = f"{base.image_name()}:{base.image_tag()}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # dependency() is an Image, so DockerfileEnhancer returns this Dockerfile
        # VERBATIM and build_dataset.py passes no REPO_URL/BASE_COMMIT build-args.
        # Clone URL and commit are therefore baked in literally, and the hardening
        # block is embedded by hand with ${BASE_COMMIT} -> this PR's actual sha.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {base_ref}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
{hardening}
"""


@Instance.register("starship", "starship_mid_0x")
class STARSHIP_MID_0X(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StarshipMid0xImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"test (\S+) ... ok")]
        re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) ... ignored")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- LHT bundle routing (rust1.56) ---------------------------------------
# Each dataset record's number_interval is the dash-joined prs_in_bundle
# (derived from prs_in_bundle by the from_json shim in __init__.py).
# Instance.create looks up f"starship/{number_interval}", so every bundle
# in this toolchain era is registered here against STARSHIP_MID_0X. (9 bundles)
_STARSHIP_MID_0X_INTERVALS = [
    "966-1681-1710-1744-1788-1847-1886-1890-1897-1900-1903-1910-1913-1915-1919-1922-1926-1938-1942-1944-1947-1950-1952-1955-1962-1964-1965-1966-1968-1972-1974-1983-1992-1993-1995-1997-2000-2001-2002-2008-2013-2014-2015-2016-2017-2019-2023-2026-2033-2034-2040-2048",
    "1019-1261-1298-1360-1366-1374-1382-1385-1392-1411-1448-1449-1450-1455-1456-1457-1459-1463-1464-1472-1473-1474-1479-1482-1485-1488-1490-1491-1492-1493-1494-1495-1496-1498-1499-1500-1504-1506-1507-1509-1511-1516-1517-1518-1525-1527-1531-1532-1534-1539-1541-1544-1546-1547-1552-1553-1558-1566-1569-1570-1571-1572-1575-1576-1581-1584-1585-1586-1590-1592-1593-1595-1596-1598-1599-1606-1612-1613-1614-1615-1618-1621-1624-1629-1645-1647-1648-1651-1661-1662-1665-1667-1668-1672-1677-1682-1683-1684-1685-1686-1687",
    "1594-1649-1946-2104-2219-2228-2257-2260-2264-2266-2267-2275-2278-2280-2283-2286-2288-2291-2292-2294-2295-2297-2299-2300-2303-2304-2305-2307-2308-2310-2311-2312-2314-2315-2317-2318-2320-2322-2324-2325-2326-2327-2329-2339-2340-2341-2346-2347-2348-2349-2350-2351-2352-2353-2354-2355-2356-2357-2358-2359-2362-2365-2366-2367-2368-2371-2372-2374-2375-2379-2382-2383-2387-2391-2392-2393-2397-2404-2408-2409-2410-2411-2412-2416-2417-2428-2429-2430-2431-2434-2442-2443-2451-2453-2455-2456-2458-2460-2471-2486",
    "1643-1751-1904-1917-1941-1948-1989-2020-2053-2054-2062-2067-2068-2081-2090-2091-2096-2100-2101-2106-2107-2108-2116-2117-2118-2119-2120-2121-2122-2124-2125-2126-2129-2133-2135-2137-2139-2147-2150-2151-2153-2155-2158-2159-2160-2163-2165-2166-2167-2168-2171-2172-2173-2174-2177-2179-2180-2185-2186-2187-2188-2189-2190-2191-2192-2193-2198-2201-2202-2203-2205-2207-2208-2209-2211-2213-2217-2218",
    "2010-2981-3042-3055-3057-3066-3067-3076-3085-3087-3088-3090-3102-3107-3108-3109-3112-3113-3115-3117-3118-3124-3129-3131-3132-3144-3146-3147-3148-3152-3153-3155-3158-3160-3165-3169-3170-3171-3172-3173-3175-3178-3181-3184-3190-3193-3200-3201-3204-3205-3206-3211-3212-3213",
    "2248-2418-2444-2465-2469-2475-2483-2489-2490-2491-2493-2494-2495-2497-2499-2503-2504-2507-2513-2516-2517-2518-2520-2521-2522-2526-2527-2528-2529-2531-2535-2536-2538-2539-2547-2548-2551-2552-2553-2554-2556-2558-2559-2560-2561-2565-2566-2571-2573-2574-2575-2578-2583-2589-2595-2597-2599-2604-2607-2608-2609-2613-2614-2615-2623-2626",
    "2481-2807-2854-2870-2876-2877-2880-2881-2883-2884-2885-2890-2893-2897-2898-2900-2903-2904-2908-2909-2911-2916-2920-2930-2931-2936-2939-2940-2941-2943-2945-2946-2947-2948-2949-2959-2963-2973-2974-2976-2982-2983-2984-2987-2989-2990-2991-2993-2997-2998-2999-3002-3003-3006",
    "2738-2752-2775-2782-2795-2797-2813-2822-2827-2831-2832-2834-2836-2837-2838-2839-2843-2844-2845-2847-2848-2853-2856-2873-2874-2878-2879",
    "2855-2887-2932-2985-2994-3008-3009-3012-3017-3019-3021-3022-3025-3026-3027-3028-3029-3032-3045-3047-3049-3054-3058-3063-3065-3069-3075-3077-3078-3081",
]
for _iv in _STARSHIP_MID_0X_INTERVALS:
    Instance.register("starship", _iv)(STARSHIP_MID_0X)
