import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .joplin import JoplinImageBase, _CHECK_GIT_CHANGES_SH, _strip_binary_diffs

_NODE_IMAGE = "node:10"
_INTERVAL_NAME = "joplin_1002_to_3978"


class ImageDefaultEra1(Image):

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
        return JoplinImageBase(
            self.pr, self._config, _NODE_IMAGE, _INTERVAL_NAME
        )

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install || true

# Compile TypeScript files (mermaid.ts, sanitize_html.ts etc) -> .js
if [ -f "gulpfile.js" ]; then
    npx gulp build 2>&1 || npx tsc -p tsconfig.json 2>&1 || true
elif [ -f "tsconfig.json" ]; then
    npx tsc -p tsconfig.json 2>&1 || true
fi

cd CliClient
npm install || true

mkdir -p build/locales
if [ -d "../ReactNativeClient/locales" ]; then
    cp -a ../ReactNativeClient/locales/* build/locales/ 2>/dev/null || true
fi
if [ -d "locales" ]; then
    cp -a locales/* build/locales/ 2>/dev/null || true
fi

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}/CliClient
if [ -f "gulpfile.js" ]; then
    npm test 2>&1 || true
elif [ -f "run_test.sh" ]; then
    bash run_test.sh 2>&1 || true
else
    npm test 2>&1 || true
fi

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn --exclude='package-lock.json' --reject /home/test.patch || true
rm -f *.rej **/*.rej 2>/dev/null || true

# Re-install deps in case patches added new packages
npm install || true
cd CliClient && npm install || true
cd /home/{repo}

if [ -f "tsconfig.json" ]; then
    npx tsc -p tsconfig.json 2>&1 || true
fi

cd CliClient
if [ -f "gulpfile.js" ]; then
    npm test 2>&1 || true
elif [ -f "run_test.sh" ]; then
    bash run_test.sh 2>&1 || true
else
    npm test 2>&1 || true
fi

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn --exclude='package-lock.json' --reject /home/test.patch /home/fix.patch || true
rm -f *.rej **/*.rej 2>/dev/null || true

# Re-install deps in case patches added new packages
npm install || true
cd CliClient && npm install || true
cd /home/{repo}

if [ -f "tsconfig.json" ]; then
    npx tsc -p tsconfig.json 2>&1 || true
fi

cd CliClient
if [ -f "gulpfile.js" ]; then
    npm test 2>&1 || true
elif [ -f "run_test.sh" ]; then
    bash run_test.sh 2>&1 || true
else
    npm test 2>&1 || true
fi

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        # Anti-reward-hack hardening runs in the PR layer (shared base keeps full
        # history). prepare.sh checks out this PR's base.sha; the canonical block then
        # detaches at that literal sha and strips every other ref/reflog so the fix
        # commit is unreachable from git history.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return """# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{repo}

{hardening}

{clear_env}

""".format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            repo=self.pr.repo,
            hardening=hardening,
            clear_env=self.clear_env,
        )


@Instance.register("laurent22", _INTERVAL_NAME)
class JoplinEra1(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefaultEra1(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        # Jasmine 3.x default reporter: "N) suite_name test_name" under "Failures:"
        # Summary: "N specs, M failures, K pending"
        re_jasmine_failure = re.compile(r"^\d+\)\s+(.+)$")
        re_jasmine_summary = re.compile(
            r"^(\d+)\s+specs?,\s+(\d+)\s+failures?(?:,\s+(\d+)\s+pending)?$"
        )

        # Jest PASS/FAIL for gulp-era PRs that internally use jest
        re_jest_pass = re.compile(
            r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
        )
        re_jest_fail = re.compile(
            r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
        )

        # run_test.sh runs: `npm test tests-build/XYZ.js` — track which file
        re_npm_test_file = re.compile(r"^>\s+.*jasmine.*?(tests-build/\S+\.js)", re.IGNORECASE)

        in_failures_section = False
        total_specs = 0
        total_failures = 0
        total_pending = 0
        current_test_file = ""

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            # Track which test file is being run (from npm output line)
            m = re_npm_test_file.match(line)
            if m:
                current_test_file = m.group(1)
                continue

            if line == "Failures:":
                in_failures_section = True
                continue

            m = re_jasmine_summary.match(line)
            if m:
                specs = int(m.group(1))
                failures = int(m.group(2))
                pending = int(m.group(3)) if m.group(3) else 0
                total_specs += specs
                total_failures += failures
                total_pending += pending
                in_failures_section = False

                if failures == 0 and current_test_file:
                    passed_tests.add(current_test_file)
                elif failures > 0 and current_test_file:
                    failed_tests.add(current_test_file)
                    passed_tests.discard(current_test_file)
                current_test_file = ""
                continue

            if in_failures_section:
                m = re_jasmine_failure.match(line)
                if m:
                    name = m.group(1).strip()
                    failed_tests.add(name)
                continue

            # Jest PASS/FAIL (gulp-era PRs)
            m = re_jest_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_jest_fail.match(line)
            if m:
                name = m.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

        # If we have jasmine summaries but no file-level pass tracking,
        # generate synthetic pass names from the summary counts
        if total_specs > 0 and not passed_tests:
            jasmine_passed = total_specs - total_failures - total_pending
            for i in range(jasmine_passed):
                passed_tests.add("spec_{i}".format(i=i + 1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=total_pending if total_specs > 0 else len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- number_interval bundle routing (prs_in_bundle dash-joined) -- PIPELINE 11b
# Delivery stamps number_interval = "-".join(prs_in_bundle); register JoplinEra1
# under every bundle key so delivered records resolve. Era-key registration
# above (_INTERVAL_NAME) is kept for the build-time era routing.
_BUNDLE_NIS_JOPLIN_E1 = [
    "1002-1054-1061-1076-1079-1098-1101-1102-1109-1110-1113-1114-1116",
    "1119-1121-1124-1125",
    "1148-1152-1173-1195-1206-1216-1218-1219-1226-1227-1229-1230-1231-1234-1235-1252-1253",
    "1149-1155-1157-1175-1185",
    "1179-1181-1199-1204-1211",
    "1290-1294-1303-1304-1310-1315",
    "1333-1337-1362-1363-1368",
    "1387-1391-1408-1420",
    "1415-1472-1487-1495",
    "1434-1439-1446-1448-1463-1464-1471-1475",
    "1466-1507-1508",
    "1504-1524-1527-1532-1533-1534-1537-1539-1541-1545",
    "1589-1596-1597-1601-1603-1612-1613-1616-1625-1633",
    "1634-1638-1640-1641-1648-1655",
    "1647-1653-1708-1733-1738-1741-1742-1744-1749-1759-1765-1769-1775-1776-1777",
    "1659-1660-1665-1680-1681-1683-1688-1692-1705-1709-1710",
    "1791-1800-1802-1804-1806-1809-1810-1811-1813-1821-1822-1823-1828-1836-1852-1853-1856-1858-1859-1860",
    "1795-1797-1866-1868-1871-1875-1887",
    "1884-1899",
    "1888-1901-1902-1911-1913-1915-1922-1924-1925-1926-1928-1929-1930-1933-1934-1935-1937-1939-1940-1941-1947-1949-1952-1954-1955-1964-1965-1966-1967-1969-1976",
    "1981-1982-1984-1987-1989-1991-1997-1999-2000-2003-2004-2005-2006-2007-2009-2014-2026-2028-2029-2033-2035-2037-2044-2051-2052-2054-2056-2060-2061-2062-2063-2064",
    "2075-2080-2083",
    "2084-2086-2092-2100-2102-2106-2108-2109-2115-2116-2123-2125-2135-2147-2150-2152-2154",
    "2161-2214-2217-2245-2248-2250-2251-2255-2262-2285-2288-2290-2292-2295-2302-2314",
    "2177-2179-2194-2198-2199-2201-2206-2210-2211-2215-2227-2231",
    "2189-2247-2272-2368-2398-2463-2465-2466-2468-2478-2479-2480-2495-2497-2498-2500-2508-2512-2522-2525-2526-2534-2537-2538",
    "2224-2346-2686-2719-2782-2828-2839-2845-2846-2868-2869-2876-2880-2881-2897-2898-2909-2914-2918",
    "2311-2318-2329-2361-2366-2367-2372-2376-2386-2387-2393-2403-2408-2410-2414-2421-2428-2432-2434-2436-2443-2446-2447",
    "2323-2333-2340-2347-2353-2355-2358",
    "2404-2431-2453-2456-2457-2462",
    "2444-2472-2474-2503-2514-2530-2531-2541-2542-2543-2546-2549-2551-2554-2557-2562-2564-2569-2571-2577-2582-2585-2612-2619",
    "2520-2556-2620-2649-2661-2679-2704-2711-2713-2720-2724-2744",
    "2563-2594-2642-2653-2672-2675-2708-2723-2730-2757-2776-2791-2806-2809-2825",
    "2566-2749-2851-2910-2913-2940-2951-2971-2973-2978-2983-2986-2989-2997",
    "2572-3448-3468-3470",
    "2623-2626",
    "2630-2673-2777-3018-3061-3065-3081-3086-3089-3096-3100-3104-3105-3122-3123-3128",
    "2650-2657",
    "2772-2870-3180-3181-3183-3195-3207-3235-3246-3262-3268-3271-3277-3284-3288-3295-3305-3309-3311-3314-3315-3316-3321-3326-3327-3328-3332",
    "2796-2819-2895-3012-3037-3187-3208-3216",
    "2805-2877-2919-2926-2945-2955-2957-2988-3006-3063-3069-3075-3084-3111-3113-3136-3188",
    "2815-3360-3401-3431-3489-3490-3498-3505-3512-3515-3517-3518-3522-3523-3526-3542-3544",
    "2905-3055-3062",
    "3034-3362-3363-3365-3374-3375-3378-3380",
    "3159-3172",
    "3202-3347-3358",
    "3213-3252-3275-3418-3433-3524-3540-3545-3561-3565-3570-3571-3580-3581-3582-3589-3590-3593-3599-3606-3629",
    "3373-3466",
    "3388-3391-3394-3408-3409-3414-3415-3417-3430-3432",
    "3454-3646-3674",
    "3525-3632-3655-3672-3673-3702-3703-3712",
    "3586-3717-3721-3745-3761-3770-3771-3776-3786-3787-3795",
    "3713-3718-3728-3735",
    "3778-3794-3798-3812-3823",
    "3875-3921-3924-3929-3936-3940-3945-3946",
    "3877-3947-3967",
    "3978-3994-3995",
]
for _ni in _BUNDLE_NIS_JOPLIN_E1:
    Instance.register("laurent22", _ni)(JoplinEra1)
