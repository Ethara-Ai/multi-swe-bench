from __future__ import annotations
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class S2nTlsImageBase(Image):
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
        return "gcc:9"

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

        repo = self.pr.repo
        org = self.pr.org

        # SHARED base (tag "base", ONE image reused by EVERY PR — keeps full git
        # history + origin so each PR can `git checkout <base.sha>` in prepare.sh;
        # we do NOT make a base per PR). mam reference format: no proxy/cert,
        # ARGs/ENV/LABEL. The `# syntax` directive makes DockerfileEnhancer.enhance()
        # skip it. Per-PR checkout + hardening live in S2nTlsImageDefault (a shared
        # base cannot pin/strip to one commit).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
        cmake libssl-dev ninja-build pkg-config git ca-certificates && \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class S2nTlsImageDefault(Image):
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
        return S2nTlsImageBase(self.pr, self._config)

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

# Relax specific -Werror flags so older eras compile under gcc 9+.
# Older s2n-tls revisions trigger maybe-uninitialized / cast-align warnings
# that newer GCCs promote to errors. We keep -Werror but allow these specific
# warnings to pass.
if grep -q "^target_compile_options.*-Werror " CMakeLists.txt; then
    sed -i "s/-Werror /-Werror -Wno-error=maybe-uninitialized -Wno-error=uninitialized -Wno-error=cast-align -Wno-error=unused-result -Wno-error=stringop-overflow -Wno-error=array-bounds -Wno-error=stringop-truncation /" CMakeLists.txt
fi

# Commit the build-flag patch so subsequent `git reset --hard HEAD` keeps it.
git config user.email build@local
git config user.name build
git add -A
git commit -m "harness: relax Werror flags" --quiet || true

# Configure + build with PQ crypto disabled (older revisions have ARM ASM
# / static_assert issues; PQ is not exercised by these tests).
# || true so a base-build hiccup doesn't kill the image; test/fix-run.sh rebuild.
rm -rf build
cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DS2N_NO_PQ=ON || true
cmake --build build -j$(nproc) || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if [ ! -d build ] || [ ! -f build/CTestTestfile.cmake ]; then
    rm -rf build
    cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DS2N_NO_PQ=ON
    cmake --build build -j$(nproc)
fi

cd /home/{pr.repo}/build
ctest -V

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard HEAD

# Patch -p1 fallback can exit non-zero on a single rejected hunk even when
# the rest applied; allow it so ctest still runs against what did apply.
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    patch -p1 --no-backup-if-mismatch < /home/test.patch || true
fi

# First try: incremental rebuild with the existing -DS2N_NO_PQ=ON tree.
# Fallbacks (in order):  full PQ-OFF rebuild  ->  full PQ-ON rebuild
# (some PRs add tests that reference PQ symbols, which need PQ-ON to link).
if ! cmake --build build -j$(nproc); then
    rm -rf build
    if ! (cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DS2N_NO_PQ=ON && cmake --build build -j$(nproc)); then
        rm -rf build
        cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
        cmake --build build -j$(nproc)
    fi
fi

cd /home/{pr.repo}/build
ctest -V

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard HEAD

# Patch -p1 fallback can exit non-zero on a single rejected hunk even when
# the rest applied; allow it so ctest still runs against what did apply.
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    patch -p1 --no-backup-if-mismatch < /home/test.patch || true
fi
if ! git apply --whitespace=nowarn /home/fix.patch 2>/dev/null; then
    patch -p1 --no-backup-if-mismatch < /home/fix.patch || true
fi

# First try: incremental rebuild with the existing -DS2N_NO_PQ=ON tree.
# Fallbacks (in order):  full PQ-OFF rebuild  ->  full PQ-ON rebuild
# (some PRs add tests that reference PQ symbols, which need PQ-ON to link).
if ! cmake --build build -j$(nproc); then
    rm -rf build
    if ! (cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DS2N_NO_PQ=ON && cmake --build build -j$(nproc)); then
        rm -rf build
        cmake . -Bbuild -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
        cmake --build build -j$(nproc)
    fi
fi

cd /home/{pr.repo}/build
ctest -V

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

        # Per-PR anti-cheat hardening. dependency() returns an Image, so
        # DockerfileEnhancer emits this Dockerfile verbatim (it only auto-injects
        # hardening into str-dependency images) — so we embed it here, after
        # prepare.sh. NOTE: prepare.sh commits a "relax Werror" harness commit ON
        # TOP of base.sha, so we must NOT re-`checkout base.sha` (that would drop
        # the build-flag fix and break the run scripts' `git reset --hard HEAD`).
        # Instead, harden the current detached HEAD in place: drop origin + every
        # ref + reflog, gc-prune unreachable objects (the fix/future commits), then
        # audit (no refs/remotes, base.sha IS an ancestor, rev-list --all == HEAD
        # so nothing beyond base.sha's history + the harness commit is reachable).
        repo = self.pr.repo
        base_sha = self.pr.base.sha
        hardening = (
            "RUN set -eux; \\\n"
            f"    cd /home/{repo}; \\\n"
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all || true; \\\n"
            "    git reflog expire --expire-unreachable=now --all || true; \\\n"
            "    git gc --prune=now --aggressive; \\\n"
            "    git repack -a -d -l --quiet; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\\n'
            '    test -z "$(git remote)"; \\\n'
            f"    git merge-base --is-ancestor {base_sha} HEAD; \\\n"
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'
        )

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{hardening}

{self.clear_env}

"""


@Instance.register("aws", "s2n-tls")
class S2nTls(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return S2nTlsImageDefault(self.pr, self._config)

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

        # Strip ANSI escape codes before matching.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Failed"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Exception"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Timeout"),
        ]
        re_skip_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Skipped"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+\s*Not Run"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    passed_tests.add(pass_match.group(1))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    skipped_tests.add(skip_match.group(1))

        # Enforce TestResult invariants: a test cannot be in two buckets.
        # Failed wins over passed (retries), skipped is exclusive.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# JSONL + registry ship together; Instance.create() resolves
# aws/<number_interval>, so every delivered dash-joined bundle value must be a
# registered routing key -> S2nTls. Single-era repo: all keys map to the one
# class. Bundle-level (one key per delivered bundle). Data-derived from the
# 149 delivered bundles -- regenerate if the delivered set changes.
_BUNDLE_NIS = [
    "1627-1813-1827-1830-1832-1836-1840-1843-1845-1849-1850-1851-1852-1853-1858-1859-1862-1865-1866-1870-1871-1872-1874-1878-1883-1884-1885-1886-1887-1890-1892-1894",
    "1842-1864-1888-1889-1891-1896-1900-1901-1902-1906-1912-1917",
    "1903-2037-2053-2054-2058-2059-2064-2066-2068-2104-2120-2122-2138-2143-2145-2146-2149-2150-2152-2156-2160",
    "1905-1916-1924-1954-1962-1963-1969-1979-1983-1984-1986-1987-1988-1991-1993",
    "1918-1920-1922-1923-1942-1944-1945-1946-1947-1948-1956-1957-1959-1960-1961-1968",
    "1919-1928-1930-1932-1933-1934-1935-1936-1937-1938-1943-1950",
    "1921-2026-2071-2118-2123-2139-2161-2162-2163-2165-2166-2167-2170-2172-2174-2175-2176-2177-2178-2179-2180-2182",
    "1925-1926-1927-1964-1967-1978-1982-1996-1997-1998-2001-2002-2003-2004-2006-2007-2014-2015-2016-2017-2018-2019-2020-2022-2024-2035-2057",
    "2025-2033-2052-2061-2063-2073-2075",
    "2031-2242-2318-2333-2335-2337-2340-2343-2345-2346-2347-2348-2351-2352",
    "2036-2038-2041-2042-2044-2045-2046-2048-2055-2056-2060-2065-2067-2069-2072-2074-2076-2077-2080-2082-2084-2089-2093-2094-2095-2097-2098-2100-2101-2103-2106-2107-2108-2110-2111-2113-2115-2117-2129-2131-2134",
    "2169-2181-2184-2187-2189-2195-2198-2204-2206-2207-2209-2210-2211-2213-2216-2218-2221-2225-2228",
    "2190-2193-2215-2238-2246-2248-2249-2254-2258-2259",
    "2192-2271-2283-2288-2297",
    "2202-2222-2237",
    "2219-2229-2230-2241-2244-2245-2253-2255-2260-2263-2265-2267-2269-2272-2273-2275-2282",
    "2224-2445-2457-2460-2465-2466-2475-2478-2481-2493-2494-2500-2507",
    "2264-2320-2330-2331-2350-2354-2356-2357-2359-2361",
    "2268-2451-2694-2701-2719-2743-2746-2748-2749-2751-2755-2758-2761-2762-2763-2765-2767-2768-2769-2770-2771-2772-2773-2775-2778-2779-2781-2782-2783-2784-2785-2788-2789-2792-2795-2798-2799-2800-2804-2805-2806-2807-2809-2810-2813-2816-2822-2824-2825-2828-2829",
    "2274-2284-2291-2294-2295-2296-2299-2301-2303-2306-2309-2312-2313-2316-2317-2322-2323-2325-2328",
    "2276-2286-2287",
    "2293-2304-2338-2344-2353-2360-2362-2364-2365-2367-2369-2370-2371-2373-2374-2381-2382-2383-2386-2388-2395-2400-2407-2408-2409-2410-2412-2426-2427-2428-2429-2430-2432-2434-2437-2438-2439-2440-2442-2444-2449-2450-2459",
    "2339-2423-2441-2578-2594-2596-2597-2598-2604-2605-2606-2607-2608-2609-2610-2611-2613-2621-2628-2630-2631-2632-2634-2636-2638",
    "2433-2443-2448-2452-2454-2468-2470",
    "2486-2489-2499-2506-2508-2510-2511-2517",
    "2491-2512-2514-2516-2518-2519-2523-2524-2525-2527-2528-2530-2531-2532-2537-2540-2543-2545-2551",
    "2533-2534-2535-2538-2546-2549-2550-2554-2555-2556-2557-2558-2559-2560-2579-2580-2581-2582-2584-2588-2589-2590-2595-2601",
    "2586-2674-2680-2681-2683-2684-2686-2688-2689-2690-2691-2696-2698-2699-2700",
    "2612-2639-2644-2645-2647-2649-2657-2658-2662-2665-2666-2667-2668-2675-2677-2678-2679",
    "2669-3088-3097-3099-3101-3106-3113",
    "2682-2702-2709-2710-2711-2713-2715-2716-2717-2718-2720-2723-2725-2726-2728-2729-2730-2732-2734-2739-2744-2747",
    "2697-2703-2704-2706-2707",
    "2714-2793-2821-2849-2852-2853-2857-2858-2860-2865-2866-2867-2868-2869-2870-2872-2873-2875-2876-2877-2878-2879-2882-2889",
    "2753-2790-2791-2814-2820-2826-2832-2837-2841-2843-2844-2848",
    "2754-2863-2864-2885-2886-2888-2890-2894-2895-2896-2897-2898-2900-2903-2904-2906-2909-2916",
    "2842-2908-2913-2917-2918-2925-2927-2928-2931-2933",
    "2847-2936-2958-2960-2961-2963-2964-2968-2970-2971-2972-2973-2974-2977",
    "2915-2939-2940-2941-2943-2945",
    "2920-2946-2981-2982-2986-2991-2996-3002-3003-3005-3006-3007-3008-3010-3011-3012-3015-3017-3018-3019-3022",
    "2975-2976-2978-2979-2980",
    "2987-2992-2993-2995-2997",
    "3004-3021-3030-3039-3042-3048-3050-3051-3053-3054-3055-3056-3060",
    "3009-3014-3023-3024-3026-3027-3028-3029-3035",
    "3016-3067-3084-3102-3103-3110-3117-3119-3120",
    "3038-3041-3044",
    "3043-3071-3073-3079-3085-3087-3090-3091-3093-3094",
    "3061-3176-3191-3194-3204-3208-3216-3221-3224-3229-3230-3232-3233-3235-3237-3239-3241-3242-3243-3245-3255",
    "3121-3126-3128-3129-3131-3133",
    "3122-3124",
    "3142-3153-3154-3156-3157-3158",
    "3155-3164-3165-3171-3172-3174-3179-3180-3182-3184-3185-3187",
    "3181-3188",
    "3183-3186-3189-3193-3195-3196-3197",
    "3198-3205-3209-3210-3212-3213-3215-3217",
    "3207-3238-3252-3254-3256-3259-3260-3264-3265-3266-3267-3268",
    "3219-3220-3226-3227-3231",
    "3222-3500-3503-3506-3508-3512-3517-3518-3521-3522-3527-3529-3531-3535",
    "3223-3497-3507-3513",
    "3225-3272-3321-3325-3328-3330-3332-3333-3338-3341-3343-3345-3346",
    "3244-3253-3261-3270-3271-3275-3276-3278-3280-3282-3289",
    "3269-3329-3335-3336-3337-3342-3355-3356-3358-3360-3361-3366-3367-3368-3372-3374-3375-3376-3378-3383",
    "3277-3279-3284-3290-3294-3295-3296-3297-3299-3300-3304-3305-3306-3309-3310-3311-3312",
    "3362-3363-3382-3384-3385-3386-3387-3388-3391-3397-3398-3401",
    "3392-3395-3402-3403-3406-3410-3412-3413-3414",
    "3405-3407-3409-3411-3415-3422-3424-3425-3426-3430",
    "3418-3423-3431-3450-3457-3458-3462-3464-3466-3467-3468-3469-3470-3472-3474-3478-3479-3481-3483",
    "3421-3434-3437-3439-3441-3443-3444-3448-3449-3451-3452-3454-3456",
    "3480-3482-3484-3485-3486-3487-3490-3493-3494-3496-3498",
    "3515-3580-3587-3591-3594-3596-3597-3599-3600-3605-3609-3610-3611",
    "3520-3526-3533-3536-3539-3540-3541-3544-3548-3549-3550-3552-3553-3555-3556-3560-3561-3562",
    "3523-3537-3613-3615-3616-3617-3618-3620-3622-3625-3629-3632-3633-3635-3637-3638-3642-3645-3651-3653-3659",
    "3534-3670-3704-3728-3735-3739-3741-3744-3746-3752-3753-3754-3761",
    "3543-3601-3623-3628-3631-3634-3641-3647-3650-3661-3662-3663-3664-3665-3666-3669-3671-3672-3673-3684-3685-3687-3688-3691-3692-3693",
    "3545-3546-3565-3570-3571-3572-3574-3575-3577-3578-3581-3582-3583-3585-3588-3589-3590-3592",
    "3554-3558-3563-3564-3567-3573",
    "3604-3627-3640-3677-3678-3703-3705-3708-3709-3712-3714-3716-3718-3721-3722-3723-3726-3727-3731-3733-3736-3740-3742",
    "3675-3676-3679-3680-3681-3682-3683-3686-3690-3694-3700",
    "3715-3747-3750-3762-3764-3765-3767-3768-3772-3783",
    "3719-3757-3790-3791-3794-3796-3797-3802-3804-3806-3816",
    "3759-3769-3780-3785-3787-3789-3793",
    "3770-3776-3779-3824-3825-3826-3827-3828-3829-3830-3834-3836-3837-3838-3840-3842-3844-3845-3846-3847-3849-3850-3855-3857-3858",
    "3771-3774-3811-3814-3815-3817-3818-3821-3823-3833",
    "3798-3921-4009-4016-4019-4020-4021-4024-4028-4029-4032-4033-4035-4037-4038-4043-4044-4045-4046-4053-4055-4057-4060-4063-4064-4068",
    "3800-3839-3859-3860-3863-3866-3870-3873",
    "3831-3832-3853-3864-3869-3871-3877-3878-3882-3883-3885-3886-3887-3891-3895",
    "3897-3904-3907-3908-3909-3910-3912",
    "3905-3906-3913-3914-3915-3917-3918-3919-3920-3923-3924-3925-3927-3929-3930-3932-3938-3939-3940-3942-3943-3946-3951",
    "3945-3950-3952-3956-3957-3961-3962-3966-3967-3968-3969-3971-3974-3977-3980",
    "3947-3964-3970-3972-3978-3984-3985-3986-3989-3992-3993-3997-3998-4003-4004-4006-4007-4008-4010-4011-4013-4014-4015-4022-4023-4025-4027",
    "4034-4075-4089-4109-4113-4115-4116-4120-4122-4125-4126-4127-4128-4129-4130-4135",
    "4047-4050-4059-4061-4066-4069-4072-4074-4076-4077-4079-4084-4085-4087-4091-4093",
    "4048-4267-4315-4327-4335-4336-4338-4341-4343-4345-4346-4347-4350-4355-4356-4358-4359-4364-4370",
    "4071-4080-4081-4083-4096-4103-4104-4107-4110",
    "4101-4250-4255-4261-4271-4272-4273-4275-4276-4278-4281-4283-4285-4287-4288-4289-4290-4292-4293-4295-4298-4301-4303",
    "4114-4134-4136-4139-4144-4145-4146-4147-4148-4149-4150-4152-4154-4156-4161-4166",
    "4160-4172-4173-4174-4175-4178-4179-4180-4183-4185-4188",
    "4177-4181-4186-4189-4190-4192-4194-4196-4197-4198-4199-4201-4202-4203-4205-4206-4209-4212-4214-4216-4217",
    "4210-4300-4304-4309-4310-4311-4313-4314-4317-4318-4319-4322-4323-4326-4328-4330-4331-4332-4334",
    "4213-4227-4229-4230",
    "4218-4235-4237-4238-4239-4243-4247",
    "4223-4225-4232",
    "4228-4236-4248-4249-4251-4254-4258-4259-4260-4266-4268",
    "4302-4352-4374-4377-4379-4380-4383-4388-4389-4392-4393-4411-4412",
    "4351-4353-4361-4365-4368-4372-4376-4378-4381-4384",
    "4369-4385-4390-4402-4462-4463-4467-4470-4474-4475-4481-4483",
    "4396-4415-4416-4417-4418-4420-4421-4425-4427",
    "4398-4399-4405-4407-4424-4426-4428-4429-4430-4433-4434-4437-4446",
    "4404-4436-4440-4447-4450-4451-4452-4456-4458",
    "4422-4460-4484-4495-4503-4504-4505-4506-4507-4511-4512-4514-4519-4520-4522-4530",
    "4449-4453",
    "4454-4523-4545-4548-4554-4558-4559-4562-4563-4566-4569-4572-4574-4580",
    "4465-4852-4863-4864-4867-4869-4874-4876-4880-4882-4885",
    "4468-4482-4485-4486-4490",
    "4469-4489-4492-4494-4496-4497",
    "4477-4526-4531-4576-4579-4582-4586-4587-4588-4589-4596-4597-4599-4601-4602-4603-4604-4605-4606-4611-4613-4621-4624",
    "4493-4498-4499-4501",
    "4509-4513-4524-4527-4534-4536-4544",
    "4532-4533-4539-4540-4551-4552-4561",
    "4565-4592-4669-4676-4677-4679-4696-4698-4701-4702-4707",
    "4567-4617-4644-4648-4649-4650-4653-4654-4658-4659-4661-4662-4663-4664-4665-4666-4667-4668-4670-4671-4672-4678-4683-4685-4686-4688-4690-4693-4694-4697",
    "4571-4578-4609-4612-4622-4623-4625-4627-4628-4630-4635-4636-4638-4639-4642-4643-4645-4646-4652-4656-4657",
    "4584-4817-4819-4821",
    "4695-4703-4706-4708-4709-4712-4714-4716-4718-4719-4720-4721-4722-4726-4727-4728-4729-4732-4733-4737-4739-4740-4744-4746",
    "4731-4742-4743-4749-4754-4755-4758-4760-4762-4768-4774-4783",
    "4735-5326-5329-5578-5586-5591-5592-5594-5595-5596-5599-5600-5603-5604-5605-5608-5610-5611-5612-5613-5614-5617-5619-5621-5623",
    "4750-4756-4763-4764-4770-4771-4773-4776-4779-4780-4785-4787-4790-4791-4794-4797-4798-4801-4802-4809-4810-4811-4812",
    "4795-4808-4815-4816-4818-4823-4824-4828-4829-4830-4832-4833-4834-4836-4839-4840-4841-4848-4849-4850",
    "4835-4844-4853-4854-4857-4858-4859-4860",
    "4838-4851-4862-4866-4868-4872-4878-4884-4888-4889-4890-4892-4894-4895-4897-4898-4903-4904-4905-4906-4907-4908-4912-4913-4914-4917-4919-4921-4924-4926-4927-4928-4930-4933-4934-4935-4937-4938-4939-4940-4941-4942-4943-4945-4946-4948-4949-4950-4951-4953-4954-4959-4961-4964-4969-4973",
    "4987-5001-5012-5019-5028-5034-5037-5038-5041-5046-5048-5052-5053-5054-5056-5057-5058-5060-5064-5065-5067-5069-5070-5072-5074-5076-5080-5082-5084",
    "5061-5071-5081-5083-5093-5094-5096-5098-5103-5104-5106-5107-5108-5110-5111-5114-5116-5117-5118-5120-5121-5123-5124-5125-5126-5127-5128-5136-5137",
    "5087-5099-5100-5109-5129-5132-5138-5139-5141-5144-5146-5148-5151-5160",
    "5131-5140-5150-5153-5156-5158-5159-5161-5164-5166-5167-5168-5169-5170-5173-5174-5175-5177-5178-5181-5183-5184-5186-5187-5191-5192-5195",
    "5155-5182-5194-5202-5204-5206-5207-5209-5210-5211-5212-5214-5215-5217-5218-5220-5221-5224-5225-5238",
    "5205-5251-5252-5253-5258-5259-5260-5261-5263-5267-5269",
    "5208-5226-5227-5229-5231-5232-5235-5241-5242-5243-5245-5255",
    "5228-5453-5484-5495-5497-5501-5504-5508-5509-5512-5516-5517-5520",
    "5268-5272-5273-5274-5278-5279-5280-5282-5284-5285-5286-5287-5288-5290-5292-5294-5295-5296-5297-5298-5299-5300-5302-5303-5305-5307-5309-5316",
    "5277-5451-5455-5459-5460-5464-5465-5466-5470",
    "5314-5315-5318-5319-5321-5331-5332-5336-5338",
    "5317-5461-5467-5468-5471-5472-5473-5474-5475-5476-5478-5479-5481-5485-5486-5487-5488-5492-5493-5494-5500-5506",
    "5333-5352-5367-5370-5383-5393-5395-5397-5398-5400-5402-5408-5409-5421-5424-5426-5427-5433-5435",
    "5357-5396-5502-5519-5521-5523-5526-5528-5530-5534-5535-5537-5540-5542-5544-5545-5548-5549-5550-5552-5553-5554-5555-5556-5558-5559-5560-5570-5580-5581-5583-5585",
    "5491-5539-5572-5635-5651-5653-5656-5657-5659-5660-5661-5662-5664-5667-5673-5675-5677-5679-5682-5684-5685-5686",
    "5579-5757-5760-5765-5767-5768-5769-5771-5775-5776-5777-5780-5782-5784-5787-5788-5789-5790-5792-5797-5803-5804-5805-5810",
    "5615-5620-5622-5630-5633-5634-5636-5637-5638-5640-5644-5645-5646-5648-5649",
    "5616-5705-5708-5709-5712-5713-5715-5716-5718-5719-5722-5724-5726-5728-5729-5730-5734-5735-5736-5737-5742-5743-5744-5745-5746-5747-5748-5749-5750-5751-5753-5755-5756-5758-5759-5761-5763-5764-5766",
    "5641-5652",
    "5772-5786-5791-5808-5809-5811-5812-5814-5816-5817-5818-5820-5822-5823-5824-5825-5827-5828-5830-5833-5835-5837-5838-5839-5840-5841-5843-5853-5857-5859-5862",
]
for _ni in _BUNDLE_NIS:
    Instance.register("aws", _ni)(S2nTls)
