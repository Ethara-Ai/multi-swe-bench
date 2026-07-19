import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks from a unified diff string.

    Binary hunks (e.g. .snap snapshot files) cannot be applied with
    ``git apply`` when the patch was generated without ``--full-index``.
    Stripping them is safe because snapshot tests regenerate these files.
    """
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )


class NapiRsImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        # Pinned (not `rust:latest`, which drifts between rebuilds). 1.90: the
        # example crates' transitive deps (e.g. toml_datetime 1.1) now require
        # Rust `edition2024`, stabilised in 1.85 -- an older toolchain (1.83)
        # fails `yarn build:test` with "feature edition2024 is required", so no
        # native .node addon builds and every test errors out. Verified end to
        # end: 1.90 builds the CLI + native addons and the ava suite passes.
        # Bookworm base so the nodesource setup_20.x script and corepack work.
        return "rust:1.90-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the shared base to a single
        # PR's base.sha and breaking every other PR with "reference is not a
        # tree". The base keeps full history; the strict anti-reward-hack
        # hardening runs per-PR (see NapiRsImageDefault).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    curl gnupg git ca-certificates && \\
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\
    apt-get install -y --no-install-recommends nodejs && \\
    corepack enable && \\
    rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
{fetch}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class NapiRsImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return NapiRsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _strip_binary_diffs(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _strip_binary_diffs(self.pr.test_patch),
            ),
            File(
                ".",
                "strip_binary_diffs.py",
                '''#!/usr/bin/env python3
"""Strip binary diffs from a patch file so git apply doesn't choke on them."""
import re
import sys

def strip_binary_diffs(patch_path):
    with open(patch_path, 'r', errors='replace') as f:
        content = f.read()
    diffs = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
    text_diffs = [d for d in diffs if d.strip() and 'Binary files' not in d and 'GIT binary patch' not in d]
    with open(patch_path, 'w') as f:
        f.write(''.join(text_diffs))

if __name__ == '__main__':
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
''',
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

""".format(),
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

yarn install || true
yarn build || true
yarn build:test || true
yarn test || true
cargo test -p napi-examples || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn install
yarn build
yarn build:test
yarn test
cargo test -p napi-examples

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
python3 /home/strip_binary_diffs.py /home/test.patch
git apply --whitespace=nowarn /home/test.patch
yarn install
yarn build
yarn build:test
yarn test
cargo test -p napi-examples

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
yarn install
yarn build
yarn build:test
yarn test
cargo test -p napi-examples

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

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("napi-rs", "napi-rs")
class NapiRs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NapiRsImageDefault(self.pr, self._config)

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

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        # ava: "  ✔ <test>" / "  ✔ <test> (123ms)"
        re_ava_pass = re.compile(r"^\s*\u2714\s+(.+?)(?:\s+\(\d+.*?\))?\s*$")
        # ava: "  ✘ <test>"
        re_ava_fail = re.compile(r"^\s*\u2718\s+(.+?)(?:\s+\(\d+.*?\))?\s*$")
        # ava skipped: "  - <test>" (must contain › to avoid matching non-ava lines)
        re_ava_skip = re.compile(r"^\s*-\s+(.+\u203a.+?)(?:\s+\(\d+.*?\))?\s*$")

        # rust: "test <name> ... ok/FAILED/ignored"
        re_rust_pass = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+ok$")
        re_rust_fail = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED$")
        re_rust_skip = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+ignored$")

        # TAP: "ok 1 - <name>" / "not ok 1 - <name>"
        re_tap_pass = re.compile(r"^ok\s+\d+\s+-\s+(.+)$")
        re_tap_fail = re.compile(r"^not ok\s+\d+\s+-\s+(.+)$")

        pass_regexes = [re_ava_pass, re_rust_pass, re_tap_pass]
        fail_regexes = [re_ava_fail, re_rust_fail, re_tap_fail]
        skip_regexes = [re_ava_skip, re_rust_skip]

        for line in test_log.splitlines():
            stripped = line.strip()
            matched = False

            for regex in pass_regexes:
                match = regex.match(stripped)
                if match:
                    passed_tests.add(match.group(1).strip())
                    matched = True
                    break

            if not matched:
                for regex in fail_regexes:
                    match = regex.match(stripped)
                    if match:
                        failed_tests.add(match.group(1).strip())
                        matched = True
                        break

            if not matched:
                for regex in skip_regexes:
                    match = regex.match(stripped)
                    if match:
                        skipped_tests.add(match.group(1).strip())
                        break

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval bundle routing
# ---------------------------------------------------------------------------
# The raw dataset leaves `number_interval` empty, so at generation time every
# record routes to the bare `napi-rs/napi-rs` key above. The delivery step
# stamps `number_interval = "-".join(prs_in_bundle)` on each record, so the
# loader then looks up `napi-rs/<dash-joined-bundle>`. Register the same NapiRs
# class under every bundle key so delivered records resolve. Split purely for
# bookkeeping (resolved vs unresolved) -- functionally identical.
_BUNDLE_NIS_NAPI = [
    "144-145-146-147-149-150-153-155-157-158-160-161-163-171-240-290-292-293-294-295-296-298-301-305-306-307-308-309-313-314-315-316-317-318-319-320-321-322-323-324-326-327-329-330-331-332-333-334-335-336-337-338-339-340-342-343-344-345-346-347-349-350-351-352-353-355-357-358-359-360-361-362-363-364-365-366-368-370-373-374-375-377-378-379-380-381-383-384-385-387-388-389-391-392-393-394-395-399-400-401-402-403-404-405-409-412-413",
    "456-457-458-460-462-464-465-466-467-468-469-472-473-474-475-476-477-478-479-481-482-483-484-485-486-487-488-489-492-494-496-498-499-500-501-502-503-504-505-508-509-510-519-520-521-522-524-525-532-533-535",
    "730-731-732-733-734-736-737-738-740-741-742-743-744-745",
    "902-904-906-909-910-911-912-913-914-915-916-917-918-919-921-922-923-924-925-926-927-928-929-1647-1770-1826",
    "938-939-940-941-942-946-947-950-951-953-954-955-956-957-958-959-960",
    "961-963-964-966-967-969-971-974-975-976-977-978-979-983-987-989-990-991-992-993-994-995-996-997-998-999-1000-1001-1002-1003-1004-1006-1009-1010-1011-1012-1015-1020-1023-1024-1025-1026-1027-1029-1031-1034-1035-1036-1038-1039-1041-1043-1048-1050-1052-1056-1058-1063-1064-1065-1066-1067-1069-1071-1072-1075-1079-1080-1081-1084-1088-1089-1090-1091-1095-1107-1111-1113-1115-1130-1131-1132-1133-1136-1137-1140-1141-1143-1144-1147-1148-1149-1150-1151-1152-1153-1155-1159-1161-1162-1166-1167-1169-1172-1176-1177-1178-1179-1181-1182-1190-1191-1192-1193-1195-1197-1198-1201-1202-1207-1209",
    "1106-1117-1118-1123-1125-1475-1492-1663-1672-1916-1921-1922-2020",
    "1238-1242-1243-1248-1251-1253-1254-1255-1256-1257-1258",
    "1259-1536-1546-1553-1554-1555-1556-1557-1561-1562-1563-1568-1569-1571-1573-1577-1578-1579-1580-1584-1587-1588-1590-1592-1593-1596-1598-1599-1601-1603-1604-1605-1606-1607-1610-1612-1614-1616-1617-1622-1623-1624-1625-1626-1628-1631-1633-1636-1637-1648-1651-1653-1654-1657-1659-1660-1664-1669-1670-1671-1675-1677-1679-1680-1682-1687-1691-1693-1695-1697-1699-1701-1702-1708-1709-1711-1712-1713-1716-1717-1718-1721-1722-1723-1724-1725-1726-1727-1730-1731-1734-1742-1743-1747-1750-1753-1754-1755-1758-1763-1769-1771-1772-1774-1776-1778-1779-1780-1781-1783-1784-1785-1786-1788-1789-1790-1792-1795-1796-1797-1798-1801-1803-1804-1805-1810-1812-1813-1814-1815-1817-1819-1820-1821-1822-1823-1827-1828-1829-1830-1831-1832-1833-1834-1835-1837-1838-1839-1841-1842-1843-1845-1846-1847-1848-1849-1850-1851-1853-1854-1856-1859-1860-1861-1863-1864-1867-1869-1870-1872-1875-1876-1878-1879-1880-1881-1882-1883-1884-1887-1888-1889-1891-1892-1894-1895-1899-1900-1901-1904-1905-1908-1909-1910-1911-1912-1913-1914-1915-1917-1923-1925-1926-1927-1931-1933-1934-1935-1937-1938-1939-1941-1942-1943-1946-1947-1949-1950-1951-1953-1954-1955-1956-1957-1958-1960-1961-1962-1963-1965-1966-1967-1968-1969-1970-1971-1974-1975-1976-1977-1978-1979-1980-1982-1984-1987-1989-1993-1995-1997-1998-1999-2002-2004-2006-2007-2008-2010-2012-2013-2014-2015-2017-2018-2019-2023-2024-2025-2026-2028-2029-2030-2031-2032-2033-2034-2035-2037-2038-2039-2040-2043-2044-2045-2049-2050-2051-2054-2056-2057-2058-2059-2062-2064-2066-2067-2074-2077-2078-2079-2082-2083-2090-2091-2092-2094-2095-2096-2097-2100-2101-2103-2107-2108-2112-2114-2115-2117-2118-2119-2122-2123-2125-2126-2129-2131-2132-2134-2135-2136-2137-2138-2139-2140-2142-2143-2144-2149-2150-2153-2155-2156-2159-2160-2161-2162-2163-2164-2165-2166-2167-2168-2169-2171-2172-2173-2175-2176-2177-2179-2182-2183-2184-2185-2186-2187-2188-2189-2190-2191-2192-2194-2195-2196-2197-2202-2204-2205-2208-2209-2210-2212-2213-2214-2216-2218-2220-2221-2222-2224-2226-2227-2228-2229-2230-2231-2232-2233-2234-2235-2239-2241-2242-2243-2247-2248-2250-2251-2252-2253-2254-2255-2256-2257-2258-2260-2262-2263-2264-2265-2266-2267-2268-2269-2270-2271-2272-2273-2275-2280-2284-2286-2287-2288-2291-2292-2296-2297-2298-2301-2303-2304-2307-2308-2309-2310-2311-2312-2314-2317-2318-2319-2321-2323-2324-2329-2330-2331-2332-2334-2336-2338-2339-2341-2342-2344-2346-2347-2348-2351-2358-2359-2360-2361-2362-2364-2366-2367-2371-2372-2374-2376-2377-2378-2380-2381-2382-2384-2385-2388-2394-2396-2398-2399-2400-2401-2402-2404-2407-2409-2410-2413-2416-2417-2418-2420-2421-2422-2424-2425-2426-2427-2428-2429-2431-2432-2433-2434-2435-2437-2438-2439-2441-2442-2443-2445-2446-2448-2449-2451-2454-2459-2461-2462-2463-2469-2470-2471-2472-2474-2477-2478-2480-2482-2483-2486-2488-2489-2490-2493-2494-2495-2497-2498-2499-2501-2503-2506-2507-2508-2509-2510-2512-2514-2515-2516-2518-2519-2523-2526-2528-2530-2531-2536-2538-2541-2542-2543-2545-2549-2550-2551-2552-2554-2556-2560-2562-2564-2565-2566-2568-2576-2577-2579-2586-2587-2588-2589-2590-2591-2592-2595-2598-2599-2600-2601-2603-2605-2606-2607-2609-2610-2611-2612-2613-2614-2619-2620-2622-2623-2624-2625-2626-2627-2628-2629-2631-2632-2633-2637-2638-2639-2640-2643-2644-2645-2646-2647-2648-2649-2650-2652-2653-2654-2655-2656-2657-2659-2660-2661-2662-2663-2667-2668-2669-2671-2672-2673-2675-2678-2681-2682-2683-2684-2686-2689-2690-2691-2693-2694-2695-2697-2698-2699-2700-2701-2702-2705-2707-2710-2711-2712-2713-2714-2715-2718-2723-2724-2725-2729-2730-2731-2732-2733-2735-2736-2740-2741-2742-2743-2744-2745-2747-2748-2749-2750-2752-2753-2754-2755-2756-2757-2759-2762-2763-2764-2765-2767-2768-2769-2771-2772-2773-2774-2775-2776-2777-2779",
    "1263-1265-1266",
    "1270-1272-1273-1274-1275-1278-1280-1281-1284-1285-1286-1287-1290-1291-1293-1294-1300-1302-1303-1306-1313-1314-1315-1317-1320-1330-1331-1332",
    "1427-1432-1433-1434-1436",
    "1439-1440-1442-1443-1445",
    "1447-1448-1449-1450-1451-1452-1453-1455-1457-1458",
    "1466-1467-1471",
    "1472-1473-1477-1481",
    "1478-1486-1487-1489",
    "1497-1499-1505-1506-1511-1512-1515-1516-1518-1525-1526-1527-1529-1530",
    "1531-1532",
    "1533-1538-1542-1547-1548-1549-1550-1551-1552",
    "1658-1698",
    "2782-2783-2786-2787-2788-2789",
    "2784-2805-2810-2811-2813-2814-2817-2818-2819-2820-2822-2824-2825-2827-2828-2829-2831-2834-2835-2837-2838-2839-2841",
    "2791-2792-2793-2794-2795-2797-2798-2799-2800-2801-2803",
    "2843-2845-2846-2849-2850",
    "2854-3024-3038-3047-3048-3050-3051-3053-3054-3055-3057-3058-3059-3060-3062-3063-3065-3066-3068-3069-3072-3076-3077-3078-3079-3080",
    "2855-2857-2858-2859-2860-2861-2862-2863-2864",
    "2865-2868-2870",
    "2866-2872-2873-2874-2875-2878-2879-2881-2883-2885",
    "2882-2887-2889-2890-2892-2893-2895-2898-2900-2901-2903-2904-2906-2907-2908-2909-2911-2912-2914-2916-2917-2919",
    "2913-2975-2976",
    "2920-2921-2922-2923-2927-2928-2929-2930-2931-2932-2933-2935-2936-2937-2941-2942-2943-2944-2945-2946-2947",
    "2949-2951-2954",
    "2977-2978-2979-2981-2982-2983-2984-2989-2990-2992-2993-2994-2995-2996-2998-3000-3001-3002-3004-3005-3006-3007-3008-3009-3010-3011-3012-3013-3014-3015-3017-3019-3020-3021-3023-3025-3026-3030-3031-3032-3033-3035-3039-3040-3041-3042-3043-3044-3045-3046",
    "3073-3081-3082-3084-3087-3088-3089-3090-3091-3092-3094-3095-3097-3098-3102-3103-3105-3106-3107-3108-3109-3110-3111-3112-3113-3114-3115-3117-3118-3120-3121-3123-3125-3126-3127-3129-3131-3132-3134-3135-3136-3137-3138-3139-3140-3141-3143-3144-3145-3146-3147-3148-3151-3152-3153-3154-3155-3156-3158-3159-3163-3164-3165-3166-3167-3169-3170",
]

_BUNDLE_NIS_NAPI_UNRESOLVED = [
    "5-6-7-9-11-12-13",
    "14-15-16-17-18-19-20-21-23-24-25-26-27-28-29-30-31-32-33-34-35",
    "36-38-39-41-42-43-44-45-46-47-48-49-50-51-52-53-54-56-58-59-60-63-64-65-66-67",
    "69-70-72-73-75-76-77-79-80-81-84-86-87-88-93-94-95-96-97-99-100-101-102-103-104-105-106-109-110-112-113-114-115-116-117-119-121-122-123-124-126-129-130-131-133-134-135-136",
    "137-138-139-140-143",
    "175-176-177-178-179-181-183-184-185-186-190-191-192-193-194-195-197-198-199-200-201-202-203-204-205-206-207",
    "208-209-210-211-212-213-214-215-216-218-220-221-222-223-224-225-226-227-228-229-230-231-232-233-234-237-238-241-244-245-247-248-249-250-251-252-255-256-257-258-259-260-261-262-263-264-265-266-267-268-269-270-274-278-280-281-282-283-285-287-288",
    "426-428-429-430-431-432-433-439-441-442-443-444-448-449-450-451-455",
    "526-528-529-530-531-536-537-538-539-540-541-542-543-544-545-546-549-550-551-552-553-554-555-556-557-558-560-561-562-563-564-565-568-569-570-571-572-573-574-575-576-577-578-579-582-584-586-587-588-589-590-591-592-593-594-595-597-601-602-603-604-605-606-607-608",
    "598-609-611-614-615-616-617-618-619-620-621-622-623-624-625-626-627-628-631-632-633-634-635-637-638-639-640-641-642-643-644-645-647-648-649-650-651-652-653-655-656-657-658-659-660-661-662-663-664-665-666-667-668-669-670-672-673-674-675-676-677-678-679-680-681-682-683-685",
    "690-691-692-693-694-695-697-700-701-702-703-704-705-707-708-709-710-711-712-713-714-715-716-717-718-719-720-721-723-725",
    "696-750-751-752-753-754-755-760-761-762-763-764-765-766-767-768-769-770-771-772-773-774-775-776-777-778-781-782-783-784-786-787-788-789-790-791-792-793-794-797-802-803-804-805-806-807-808-809-810-811-812-813-814-815-817-818-820-821-822-824-825-826-827-828-829-837-838-840-841-842-843-844-845-846-847-848-849-851-852-853-855-856-857-858-859-860-861-863-865-867-868-871-874-876-877-878-879-880-881-882-883-884-885-887-888-889-890-891-892-893-894-897-899-901",
    "1200-1213-1219-1221-1223-1224-1225",
    "1227-1228-1230-1234-1235-1241-1247",
    "1339-1369-1371-1376-1382-1383-1384",
    "1348-1349-1350-1354-1355-1360-1364",
    "1351-1352-1363-1367-1368",
    "1393-1395-1396-1397-1399-1400",
    "1403-1410-1413-1414-1416",
    "1418-1420-1423-1424-1426",
    "2065-2088-2093-2356-2453-2524",
    "2956-2957-2959-2960-2961-2963-2966-2967-2971-2972-2973-2974",
]

for _ni in _BUNDLE_NIS_NAPI:
    Instance.register("napi-rs", _ni)(NapiRs)

for _ni in _BUNDLE_NIS_NAPI_UNRESOLVED:
    Instance.register("napi-rs", _ni)(NapiRs)
