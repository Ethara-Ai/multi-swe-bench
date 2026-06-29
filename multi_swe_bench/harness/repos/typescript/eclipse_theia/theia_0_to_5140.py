"""eclipse-theia/theia harness config — Node 8 Yarn era (PRs 331–4602).

Theia is a TypeScript monorepo using Lerna.  This era uses Yarn Classic as the
package manager (yarn.lock present) and requires Node >= 7.  The repo pins
``node >= 7.9.0`` to ``>= 8.9.3`` over this range, so ``node:8-buster`` is the
appropriate base image.  Tests run via Mocha through Lerna
(``lerna run --scope "@theia/!(example-)*" test``).
"""

import re

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TheiaNode8ImageDefault(Image):
    """PR-specific Docker layer for the Node 8 Yarn era.

    Pipeline:
        git checkout <base_sha>
        PUPPETEER_SKIP_DOWNLOAD=true yarn install --ignore-engines
        yarn compile
        lerna run --scope "@theia/!(example-)*" test --stream --concurrency=1
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
        return "node:8-buster"

    @staticmethod
    def _is_deprecated_debian(base_img: str) -> bool:
        # node:*-buster (and older) images sit on EOL Debian whose apt repos
        # have moved to archive.debian.org. The base class only recognises
        # bare "debian:buster"-style tags, so flag the node images here too;
        # this makes Image._get_apt_update_command() rewrite sources.list to
        # archive.debian.org before "apt-get update" (otherwise install 404s).
        if any(s in base_img for s in ("buster", "stretch", "jessie")):
            return True
        return Image._is_deprecated_debian(base_img)

    def extra_packages(self) -> list[str]:
        # build-essential (g++/make), git, and python3 are already in the
        # default package set baked into Image.dockerfile(); only Theia's
        # native-module headers (electron/keytar/node-pty backends) are extra.
        return ["libx11-dev", "libxkbfile-dev", "libsecret-1-dev"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the eval scripts + patches into /home/ (outside the git
        # tree, so hardening leaves them untouched) and bakes the heavy
        # yarn/npm install + compile into the image via prepare.sh.
        return (
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh"
        )

    def dockerfile(self) -> str:
        # Explicit canonical Dockerfile (mirrors Image.dockerfile()) so the
        # anti-reward-hacking Image._HARDENING_BLOCK is referenced directly in
        # this file rather than only inherited. The base image is a string dep,
        # so DockerfileEnhancer still injects the REPO_URL/BASE_COMMIT ARGs +
        # infra block; because the clone uses "${REPO_URL}" and the hardening
        # marker is already present, the enhancer makes no further changes.
        base_img = self.dependency()

        default_packages = [
            "ca-certificates", "curl", "build-essential", "git", "gnupg",
            "make", "python3", "sudo", "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        repo = _safe_path_component(self.pr.repo)
        extra_setup = self.extra_setup()

        sections = [f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(apt_command)
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        if extra_setup:
            sections.append(extra_setup)
        sections.append(Image._HARDENING_BLOCK)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"

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
                """\
#!/bin/bash
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


@Instance.register("eclipse-theia", "theia_0_to_5140")
class THEIA_0_TO_5140(Instance):
    """Harness instance for eclipse-theia/theia — Node 8 Yarn era (PRs 331–4602)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return TheiaNode8ImageDefault(self.pr, self._config)

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


# Route the dash-joined number_interval (canonical prs_in_bundle from each
# record's resolved_issues + own PR number, sorted, "-"-joined) of the
# SHIPPABLE chunk1+chunk2 dataset to the THEIA_0_TO_5140 config. Instance.create()
# looks up f"{org}/{number_interval}"; Instance.register returns the class
# unchanged, so it answers to every key (the era key + any prior keys above
# are kept for back-compat).
_BUNDLE_NUMBER_INTERVALS = [
    "228-584-815-821-841-871-874-882-887-888-904",
    "21-639-1213-1307-1535-1660-1680-1744-1769-1841-1845-1864-1866-1875-1983-2054-2055-2153-2154-2173-2178-2183-2208-2211-2258-2280-2337-2348-2350-2351-2377-2378-2391-2400-2408-2421-2434-2464-2467-2475-2478-2481-2484-2485-2488-2489-2491-2496-2499-2501-2503-2508-2510-2511-2512-2516-2517-2520-2523-2524-2525-2527-2528-2540-2546-2548-2552-2554-2555-2561-2567-2571-2572-2574-2575-2579-2583-2585-2591-2595-2599-2603-2606-2608-2616-2617-2625-2626-2628-2641-2654-2655-2656-2657-2663-2666-2669-2671-2672-2673-2688-2691-2700-2711-2716-8383-10380",
    "656-1489-1692-1751-2069-2076-2092-2165-2187-2190-2198-2199-2201-2213-2220-2237-2242-2247-2253-2256-2261-2264-2270-2273-2275-2276-2278-2281-2282-2284-2286-2291-2301-2310-2312-2313-2315-2317-2320-2322-2327-2330-2331-2338-2354-2357-2358-2360-2361-2366-2373-2376-2386-2396-2402-2403-2404-2405-2406-2409-2425-2426-2430-2437-2446",
    "200-934-1137-1331-1344-1352-1364-1791-1919-1946-2184-2349-2381-2397-2624-2767-2782-2829-2854-2892-2910-2944-2945-2951-2963-2968-2977-2978-2992-3001-3002-3005-3006-3009-3010-3011-3018-3019-3020-3031-3032-3036-3040-3041-3046-3049-3051-3054-3055-3060-3061-3062-3064-3065-3066-3067-3068-3069-3070-3071-3072-3073-3078-3081-3082-3083-3084-3085-3086-3087-3088-3092-3098-3101-3105-3106-3109-3110-3112-3113-3114-3115-3117-3118-3123-3124-3125-3128-3129-3131-3132-3137-3139-3141-3142-3144-3152-3154-3161-3164-3165-3166-3168-3170-3171-3173-3178-3179-3183-3185-3189-3190-3193-3197-3204-3206-3211-3212-3216-3219-3221-3230-3233-3235-3237-3238-3239-3240-3242-3248-3255-3258-3259-3267-3277-3280-9990-10574-11235",
    "1244-1591-1888-2200-2503-2802-2894-2956-2970-3229-3236-3250-3283-3302-3377-3397-3404-3465-3469-3484-3571-3573-3620-3626-3635-3639-3641-3646-3647-3660-3664-3665-3676-3679-3681-3682-3687-3695-3696-3697-3698-3700-3709-3711-3716-3719-3721-3723-3725-3726-3727-3728-3731-3733-3739-3741-3742-3743-3745-3753-3754-3757-3759-3769-3775-3776-3786-3792-3797-3799-3805-3806-3810-3811-3816-3817-3818-3820-3821-3827-3830-3834-3845-3850-3853-3856-3868-3870-3873",
    "971-2195-2395-2547-2667-2784-3077-3079-3312-3338-3353-3599-3658-3732-3766-3778-3801-3804-3812-3826-3829-3849-3861-3862-3863-3875-3877-3878-3883-3888-3889-3893-3895-3900-3904-3905-3909-3912-3914-3919-3922-3926-3930-3936-3938-3939-3941-3943-3946-3951-3953-3954-3957-3958-3965-3969-3973-3976-3978-3991-3997-4000-4006-4009-4010-4013-4014-4015-4018-4019-4026-4028-4037-4047-4056-4058-4064-4069-4085-4092-4106-4111-4125-4133-4134-4135-4137-4139",
    "228-584-815-821-871-874-882-887-888-904",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("eclipse-theia", _ni)(THEIA_0_TO_5140)
