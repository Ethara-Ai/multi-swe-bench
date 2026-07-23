import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Single-era config covering PRs #1967 -> #3402.
# go.mod ranges from `go 1.13` (v3 module path) to `go 1.24.0` (v4 module path).
# Single config suffices because:
#   - GOTOOLCHAIN=auto on the golang:1.22 host downloads the toolchain version
#     declared in go.mod at runtime (verified for go 1.13 through go 1.24.0).
#   - The repo is single-module across the full PR range (one go.mod at root).
#   - Test framework is Go built-in `testing`; --- PASS / --- FAIL / --- SKIP
#     output is stable across the entire history.
#   - //go:embed directives only exist in examples/ (no test files), so no
#     `go generate` step is required before `go test`.


# =============================================================================
# TWO-LEVEL IMAGE LAYOUT: shared toolchain base + per-PR image.
#
# ImageBase (tag `base`) is built ONCE per architecture and every per-PR image
# FROMs it, so `apt-get update && apt-get install` runs twice (once per arch)
# instead of 146 times.
#
# The base deliberately carries NO `git clone`. The repo checkout lives in the
# per-PR image, for two independent reasons:
#
#  1. Docker layers are additive. _HARDENING_BLOCK (delete every ref, `git gc
#     --prune=now`) only writes *whiteouts* in the layer that runs it — the
#     objects it "deletes" are still present in the lower base layer and can be
#     recovered by unpacking the image. A cloned base would therefore ship the
#     full upstream history, including the commits that fix each PR, defeating
#     the hardening entirely.
#  2. A base can only be hardened against ONE ${BASE_COMMIT}, and it is shared
#     by 73 PRs with 73 different SHAs. Whichever PR built it would prune away
#     every other PR's commit.
#
# Sharing a *clone* across PR images is thus not achievable without breaking the
# anti-cheat guarantee; sharing the toolchain layer is, and that is what this
# does. Clone + checkout + harden all happen per-PR, against that PR's own SHA.
# =============================================================================


class ImageBase(Image):
    """Shared toolchain base: golang:1.22 + apt packages. No repo, no patches.

    `dependency()` returns a str, so DockerfileEnhancer.enhance() runs on this
    Dockerfile and adds the syntax directive, ARGs, ENV block and LABELs. Since
    the body contains no `git clone` / `git fetch` / `git remote add` token,
    the enhancer's _standardize_repo_fetch and _inject_final_sanitize are both
    no-ops here — the clone and the hardening stay in the per-PR image where
    ${BASE_COMMIT} is unambiguous.
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
        return "golang:1.22"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Mirrors the apt section of Image.dockerfile() (same default package
        # list, same _get_apt_update_command helper so the deprecated-Debian
        # rewrite stays consistent), minus the clone/checkout/harden sections.
        # DEBIAN_FRONTEND/LANG are supplied by the enhancer's ENV block.
        packages = [
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
        apt_command = self._get_apt_update_command(
            " \\\n    ".join(packages), self.dependency()
        )

        return f"""FROM {self.dependency()}

WORKDIR /home/

{apt_command}

ENV GOTOOLCHAIN=auto

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """Per-PR image: FROM the shared base, then clone + checkout + harden.

    Because `dependency()` returns an Image, harness/image.py hands this class
    the whole job and steps back:
      * DockerfileEnhancer.enhance() returns the Dockerfile untouched (it only
        rewrites string-dependency images), so the syntax directive, ARGs,
        LABELs, hardening and CMD must be emitted here;
      * build_dataset/run_evaluation pass NO build args, so REPO_URL and
        BASE_COMMIT are baked as ARG defaults instead.

    Both are done by REUSING image.py's own definitions rather than restating
    them, so the output cannot drift from what a string-dependency image gets.
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        base = self.dependency()
        base_ref = base.image_full_name()

        # Same ARG/ENV/LABEL preamble the enhancer injects into every
        # string-dependency image. Taken from the enhancer itself so a change
        # to image.py's infrastructure block is picked up here automatically.
        infra = DockerfileEnhancer._infrastructure_block(self, base_ref).rstrip("\n")

        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        # _infrastructure_block emits a bare `ARG BASE_COMMIT` (it expects the
        # value as a --build-arg). Image-dependency images get no build args, so
        # redeclare it with this PR's SHA as the default; the later declaration
        # is what ${BASE_COMMIT} resolves to below, including inside the
        # hardening block's assertions.
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_ref}

{infra}

ARG BASE_COMMIT="{self.pr.base.sha}"

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

{self._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""

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
                "run_tests.sh",
                """#!/bin/bash
# Single-module Go repo. GOTOOLCHAIN=auto lets the host golang:1.22 download
# the toolchain version declared by go.mod (pion/webrtc spans go 1.13 -> 1.24+).
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto
cd /home/{pr.repo}
# `go test` always writes per-package result lines (`--- PASS:` / `--- FAIL:` /
# `--- SKIP:`) before exiting, so the script's exit status does not affect what
# parse_log sees. Capture status without `|| true`, then exit 0 so the harness
# always reaches parse_log even when some packages fail.
status=0
# `-p=1` runs packages serially; `-parallel=1` runs subtests within a package
# serially. pion/webrtc tests exercise ICE/DTLS/SCTP transports — most use the
# in-process vnet, but several bind real loopback ports (TestICETCP, mux
# listeners). Serializing prevents port-conflict flakes that show up as Rule 2
# (PASS->FAIL between test/fix stages) in the harness.
go test -v -count=1 -p=1 -parallel=1 -timeout=20m ./... || status=$?
echo "run_tests.sh: go test exited with status=$status"
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Warm the module cache only. The reset/checkout that used to live here is now
# emitted by Image.dockerfile() (`git reset --hard` + `git checkout
# ${{BASE_COMMIT}}`), and _HARDENING_BLOCK asserts afterwards that HEAD really
# is BASE_COMMIT and the tree carries no other refs — so repeating either here
# would be redundant, and pinning the sha in a script would drift from the
# BASE_COMMIT build arg the harness passes.
set -e

cd /home/{pr.repo}
export GOTOOLCHAIN=auto
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
        ]


@Instance.register("pion", "webrtc")
class Webrtc(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Every row in dataset/pion__webrtc_lht_final.jsonl is a release-delta BUNDLE and
# carries `number_interval` = its `prs_in_bundle` joined by "-" (e.g.
# "1967-2170-2465-2513-2523-2528-2529"). Instance.create() routes on
# f"{org}/{number_interval}" whenever number_interval is non-empty, so every
# interval below must be registered against Webrtc — the single-era config above
# covers the whole #1967 -> #3402 range, so they all map to the same class.
# Instance.register returns the class unchanged, so it answers to every key.
_NUMBER_INTERVALS = [
    "1967-2170-2465-2513-2523-2528-2529",
    "1971-1973-1978-1979-1980",
    "1994-1995-2002-2003-2005",
    "2007-2015",
    "2024-2025-2026-2029",
    "2064-2067",
    "2071-2082-2086",
    "2076-2091",
    "2105-2106-2108",
    "2119-2120-2129-2131",
    "2130-2132-2134-2135-2136-2137-2138-2139-2142-2143-2144-2145-2146",
    "2160-2163-2166-2167-2169-2172-2173-2174-2176",
    "2177-2178-2179",
    "2183-2184",
    "2192-2194-2196-2198-2199-2200-2201-2202-2204",
    "2208-2209-2210-2212",
    "2215-2216-2219",
    "2220-2222",
    "2227-2229-2230-2231-2232-2234",
    "2236-2239-2240-2242-2245-2266",
    "2255-2287-2305-2332-2339-2340-2344-2346-2348-2352",
    "2303-2460",
    "2307-2396-2397-2401-2402-2408-2410",
    "2317-2319-2320-2329",
    "2336-2949-2966-2968-2969-2972-2974",
    "2376-2429",
    "2392-2398",
    "2395-2454",
    "2406-2412",
    "2423-2433-2434-2435",
    "2436-2438-2440-2442-2443",
    "2445-2450-2451-2452-2455",
    "2456-2467-2468-2471-2475",
    "2506-2508-2509-2510",
    "2530-2535",
    "2538-2542-2546-2549-2551-2552",
    "2563-2572",
    "2769-2773-2783",
    "2842-2854",
    "2930-2938-3021-3034-3038",
    "2931-2939-2940-2945-2947-2950",
    "2975-2976-2977",
    "3018-3100-3101-3103-3106-3107-3108",
    "3039-3040-3043",
    "3059-3060-3066-3067",
    "3116-3118-3124",
    "3123-3158-3159-3160-3162-3163-3166-3167-3168-3170-3171",
    "3238-3240-3241-3243",
    "3291-3293-3295-3296",
    "3377-3381-3384-3386-3388-3390-3393-3394-3397-3398-3399-3401",
    "3378-3380",
    "3382-3383",
    "3402-3406-3407-3409-3411-3412-3418",
    "2069-2074-2077-2081",
    "2102-2116-2117-2118",
    "2109-2110",
    "2112-2531-2537",
    "2149-2150-2151-2153",
    "2191-2301",
    "2378-2379-2380-2382-2386-2390-2391-2393",
    "2480-2481-2482",
    "2777-3097-3115-3144-3244-3246-3247-3249-3251-3252-3253-3254-3255-3256-3257-3258-3259-3260-3262-3265-3266-3267-3268-3269-3270-3271-3273-3274-3275-3277-3278-3279-3280-3281-3283-3284-3285-3287-3288-3289",
    "2978-2979-2980-2982-2983-2984-2987-2988-2990-2991-2997-2999-3000-3004-3008-3009-3010",
    "2985-3017-3020-3024-3025-3028-3030-3031-3033",
    "3016-3041-3044-3045-3046-3048-3049-3050",
    "3061-3062-3070-3071-3072-3073-3075-3078-3080-3082-3083-3084-3088-3091-3092-3093-3096",
    "3081-3165-3177-3178-3180-3182-3190-3191-3193-3195-3196-3197",
    "3085-3110-3111-3112-3113-3114",
    "3119-3297-3298-3299-3300-3301-3302-3303-3304-3305-3306-3308-3309-3312-3313-3314-3315-3316-3317-3318-3319",
    "3125-3126-3129-3130-3131-3132-3133-3139-3146-3147-3150-3151-3152-3153-3154",
    "3188-3198-3199-3200-3201-3202-3203-3204-3205-3206-3208-3210-3212-3214-3215-3216-3217-3218-3219-3220-3221-3222-3223-3224-3225-3227-3228-3229-3230-3231-3232-3233-3234-3235-3236",
    "3324-3325-3329-3332-3337-3338-3339-3340-3341-3342-3344-3346-3347-3348-3349-3350-3353-3354",
    "3326-3355-3358-3359-3360-3363-3364-3366-3367-3368-3372-3374",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("pion", _interval)(Webrtc)
