import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# telepresenceio/telepresence — a local-development tool for Kubernetes services.
#
# Discovery (dataset analysis):
#  - 72-PR Go range #1864..#4094, almost all on `release/v2` branch
#    (a few on release/v2.x tags). base.sha is the start of a version-range
#    window (e.g. v2.3.7..v2.4.0); each instance bundles 2..48 merged PRs.
#  - Multi-module repo: ./go.mod, ./rpc/go.mod, ./cmd/cobraparser/go.mod,
#    ./cmd/teleroute/go.mod. Per-package `go test` must run from the nearest
#    ancestor go.mod, so the runner walks up from each test pkg path.
#  - Go directive climbs across the range (early v2.3 ~go1.16, latest moved to
#    `go 1.26.0`), so GOTOOLCHAIN=auto is required to honour each base.sha's
#    own go.mod/toolchain directive.
#  - At least one file uses cgo (pkg/client/cli/env/syntax_test.go) — keep
#    CGO_ENABLED=1 + a C toolchain (build-essential + pkg-config).
#  - Test files in cmd/ (49 PRs), pkg/ (55 PRs), integration_test/ (62 PRs).
#    integration_test/ needs a real k8s cluster — those tests fail/skip without
#    one and aren't the recoverable signal. cmd/ and pkg/ unit tests are the
#    recoverable bulk (8 PRs touch ONLY integration_test/ → no unit signal).
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs from each pkg's nearest go.mod ancestor.
#    Runs are fenced with `### TLPKG ###` markers so test ids stay unique
#    across packages.
#
# Conformed to the hardened image.py contract:
#  - dependency() returns a STRING base image so the shared Image.dockerfile()
#    owns the build: it clones "${REPO_URL}", checks out "${BASE_COMMIT}", runs
#    extra_setup(), and appends the _HARDENING_BLOCK that strips every other
#    ref/commit so the fix can't be read out of git history. DockerfileEnhancer
#    then injects the proxy/cert infra + final sanitize pass. None of that fires
#    when dockerfile() is overridden, which is why the old two-stage build (its
#    own base-image class + custom dockerfile) bypassed the anti-cheat hardening.


# Era map: the go.mod `go` directive at each instance's base.sha, read by
# `git show <base.sha>:go.mod` across all 72 records. The toolchain climbs
# 1.15 -> 1.26 over the range. GOTOOLCHAIN=auto can only UPGRADE (and can only
# auto-download >= go1.21), so it cannot rescue old-era code from a too-new
# toolchain: e.g. at v2.3.7 the dep dlib@v1.2.4 re-declares
# `context.WithoutCancel`, which entered the stdlib in go1.21 -> build fails on
# any toolchain >= 1.21. So each instance must build on a base image matching
# its era's go minor. Keyed by PR number (the instance id) rather than a number
# interval because #2548 is a backport: low number, but release_line 2.19 / go
# 1.22 (its base.sha is on the v2.19 line), so number ranges would misroute it.
_GO_MINOR_BY_PR = {
    # go 1.15
    1864: "1.15", 1876: "1.15",
    # go 1.16
    1951: "1.16", 1998: "1.16", 2024: "1.16",
    # go 1.17
    2067: "1.17", 2079: "1.17", 2137: "1.17", 2141: "1.17", 2159: "1.17",
    2186: "1.17", 2370: "1.17", 2424: "1.17", 2432: "1.17", 2483: "1.17",
    2488: "1.17", 2531: "1.17", 2538: "1.17",
    # go 1.18
    2576: "1.18", 2577: "1.18", 2600: "1.18", 2633: "1.18", 2636: "1.18",
    2644: "1.18", 2648: "1.18", 2709: "1.18", 2711: "1.18", 2754: "1.18",
    2767: "1.18", 2806: "1.18", 2807: "1.18",
    # go 1.19
    2862: "1.19", 2877: "1.19", 2897: "1.19", 2912: "1.19", 2991: "1.19",
    3045: "1.19", 3070: "1.19", 3079: "1.19", 3129: "1.19", 3140: "1.19",
    3172: "1.19", 3225: "1.19", 3245: "1.19", 3312: "1.19", 3354: "1.19",
    3359: "1.19",
    # go 1.21 (toolchain go1.21.3)
    3385: "1.21", 3507: "1.21",
    # go 1.22 (toolchain go1.22.2) — note 2548 is a v2.19 backport
    2548: "1.22", 3626: "1.22",
    # go 1.23
    3694: "1.23", 3707: "1.23", 3746: "1.23", 3749: "1.23",
    # go 1.24
    3825: "1.24", 3837: "1.24", 3890: "1.24", 3896: "1.24", 3899: "1.24",
    3904: "1.24", 3909: "1.24", 3953: "1.24", 3961: "1.24",
    # go 1.25
    3991: "1.25", 3994: "1.25", 4016: "1.25",
    # go 1.26
    4038: "1.26", 4069: "1.26", 4083: "1.26", 4091: "1.26", 4094: "1.26",
}

# golang base images that ship on Debian buster/bullseye (<= go1.20). Their apt
# mirrors have moved to archive.debian.org, so the default apt-get update 404s.
_OLD_DEBIAN_GO = ("1.15", "1.16", "1.17", "1.18", "1.19", "1.20")

# number_interval per instance = the dash-joined EXACT prs_in_bundle list (NOT a
# range): e.g. bundle [146,147,150,155,157] -> "146-147-150-155-157". The raw
# jsonl carries this in its `number_interval` field, and gen_report copies it
# verbatim into the resolved dataset jsonl. Because Instance.create() routes by
# `{org}/{number_interval}` whenever number_interval is non-empty, we must
# register the Telepresence instance under every interval key (done at the bottom
# of this file) — all of them map to the same class; era routing stays keyed on
# pr.number via _GO_MINOR_BY_PR, independent of the registration key.
_NUMBER_INTERVAL_BY_PR = {
    1864: "1864-1910-1911-1914-1920-1921-1922-1927-1931-1942-1946-1949-1950-1954-1956-1958-1964",
    1876: "1876-1877-1881-1882-1888-1892-1898-1899-1903-1904-1905-1908-1909-1913",
    1951: "1951-1952-1957-1965-1969-1972-1974-1975-1982-1984-1985-1986-1991-1992-1994-1999-2000-2001-2002-2005-2006-2008-2009-2014-2015-2016-2017-2022",
    1998: "1998-2035-2036-2037-2038-2040-2041-2042-2043-2044-2047-2051-2052-2053-2056-2058-2060-2063-2064-2065-2066-2068-2070-2074-2078-2080-2082",
    2024: "2024-2028-2029-2030-2031-2032",
    2067: "2067-2077-2088-2090-2093-2100-2102-2107-2108-2111-2112-2113-2118",
    2079: "2079-2094-2116-2117-2119-2123-2125-2128-2131-2138-2148-2152-2153-2155-2157",
    2137: "2137-2183-2192-2195-2197-2198-2200-2202-2204-2209-2210-2212-2214-2219-2221-2222-2223-2224-2226-2229-2231-2232",
    2141: "2141-2220-2227-2233-2235-2236-2237-2238-2239-2240-2241-2243",
    2159: "2159-2160-2164-2166-2168-2171-2172-2175-2176-2179-2182-2184-2189",
    2186: "2186-2259-2265-2268-2269-2273-2275-2279-2281-2285-2286-2298-2301-2302",
    2370: "2370-2371-2372-2373",
    2424: "2424-2440-2441-2442-2443-2445-2446-2448-2449-2452-2453-2454-2455-2458-2460-2461-2463-2464-2469-2470-2471-2472-2473-2474-2475-2476-2477-2478-2481-2491-2493",
    2432: "2432-2437-2438",
    2483: "2483-2547-2553-2556-2558-2559-2560-2561-2562-2563-2567-2569-2571-2572-2574",
    2488: "2488-2494-2497-2500-2503-2505-2506-2508",
    2531: "2531-2546-2552",
    2538: "2538-2541-2542-2543-2545",
    2548: "2548-3282-3618-3620-3621-3622-3623-3624-3625-3628-3629-3632-3633-3634-3636-3637-3638-3641-3643-3645",
    2576: "2576-2589-2590-2593-2594-2597-2601-2603",
    2577: "2577-2578-2579-2581-2583",
    2600: "2600-2608-2614-2617-2618-2620-2621-2622-2626-2627-2629-2631-2632",
    2633: "2633-2635-2638-2639",
    2636: "2636-2642-2646-2647-2650-2655-2659",
    2644: "2644-2664-2665-2666-2671-2674-2677-2679-2684-2685-2690-2692-2693-2699-2700-2705-2707-2708",
    2648: "2648-2652-2653-2660-2662-2663",
    2709: "2709-2720-2722-2723-2726-2728-2731-2734-2737-2741-2743-2744-2749-2751",
    2711: "2711-2719-2721",
    2754: "2754-2785-2787-2788-2790-2794-2795-2797-2802-2805-2808-2812",
    2767: "2767-2773-2774-2776-2779",
    2806: "2806-2815-2816-2817-2822-2823-2825-2828-2831-2832-2836-2841",
    2807: "2807-2810-2833-2838-2843-2844-2846-2850-2852-2855-2857-2858-2859-2863-2867-2871-2872-2873-2875",
    2862: "2862-2983-2984-2985-2986-2987-2988-2990-2992-2994-2995-2998-2999-3001-3003-3007-3010-3013-3016-3017-3018-3019-3020-3021-3023-3024-3027-3029-3030-3031-3034-3038",
    2877: "2877-2887-2889-2890-2891-2892-2894-2895-2896-2898-2899",
    2897: "2897-2901-2902-2904-2905-2906-2907-2908-2909-2910-2914",
    2912: "2912-2927-2932-2934-2935-2936-2937-2941-2943-2944-2945-2947-2952-2955-2956-2959-2962-2964",
    2991: "2991-3083-3091-3097-3098-3100-3101-3103-3104-3105-3108-3110-3111-3112-3116-3118-3122-3125-3126-3128-3130-3131-3132-3134",
    3045: "3045-3048-3049-3050-3052-3054-3055-3056-3059-3061-3062-3066-3067-3068-3071-3072-3073-3074-3080-3081-3082",
    3070: "3070-3084-3085-3086-3089",
    3079: "3079-3136-3161-3174-3175-3181-3183-3184-3185-3188-3194-3198-3204",
    3129: "3129-3145-3147-3148-3153-3154-3156-3157-3158-3160-3165-3167-3168-3177",
    3140: "3140-3248-3265-3273-3280-3285-3286-3288-3289-3290-3291-3292-3293-3294-3295-3296-3298-3299-3300-3301-3302-3303-3304-3305-3306-3308-3311-3315-3316-3320-3322-3324-3325-3326-3327",
    3172: "3172-3203-3205-3206-3207-3212-3216-3217-3218-3219-3220-3222-3223-3224-3227-3228-3229-3230-3231-3233-3234-3235",
    3225: "3225-3236-3239-3241-3244-3251-3252-3253-3254-3255-3257-3258-3260-3262",
    3245: "3245-3263-3266-3269-3272-3281-3314",
    3312: "3312-3323-3328-3329-3330-3331-3332-3334-3336-3337-3343-3345-3348-3349-3351-3352-3355-3358-3362-3363",
    3354: "3354-3368-3386-3388-3389-3391-3399-3402-3403-3408-3409-3410-3411-3412-3416-3417-3420-3421-3422-3427",
    3359: "3359-3364-3365-3367-3371-3372-3374-3377-3378-3379-3381",
    3385: "3385-3424-3428-3430-3431-3432-3433-3434-3435-3436-3438-3446-3447-3448-3450-3451-3457-3458-3459-3461-3462-3463-3465-3466-3469-3472-3474-3475-3476-3479-3480-3481-3486-3489-3490-3493-3494-3495-3496-3497-3500-3501-3502-3503-3505-3506-3508",
    3507: "3507-3511-3516-3517-3519-3520-3521-3522-3523-3524-3525-3526-3528-3530-3532-3533-3536-3537-3540-3541-3554-3559-3560-3561-3563-3564-3565-3566-3571-3572-3574-3580-3581-3583-3585-3588-3589-3590-3592-3594-3596-3605-3606-3608-3612-3614-3616-3617",
    3626: "3626-3648-3651-3655-3658-3659-3662-3663-3664-3665-3666-3667-3668-3669-3670-3671-3673-3674-3675-3677-3679-3681-3682-3683-3684-3688-3693",
    3694: "3694-3695-3699-3700-3701-3702-3705-3710-3712-3717-3718-3719-3729-3736-3737-3738-3739-3740",
    3707: "3707-3724-3725",
    3746: "3746-3748",
    3749: "3749-3752-3754-3756-3759-3761-3762-3763-3764-3765-3788-3789-3791-3796-3798-3799-3800-3802-3803-3805-3808-3813-3814-3817-3818-3820",
    3825: "3825-3848-3849-3854-3859-3865-3872-3874-3877-3878-3884",
    3837: "3837-3838-3839-3840-3841",
    3890: "3890-3891-3892-3893-3894-3895",
    3896: "3896-3897-3898",
    3899: "3899-3903-3912-3914-3916-3933-3935-3936-3937-3940-3943-3945-3947",
    3904: "3904-3905-3906",
    3909: "3909-3915",
    3953: "3953-3954-3958-3959",
    3961: "3961-3963-3965-3967-3968-3970-3971-3972-3974-3975-3976-3977-3979-3980-3981-3985",
    3991: "3991-3999",
    3994: "3994-3995-3996-4000-4014-4021-4022-4023-4024-4025-4026-4028-4029-4030-4031-4033-4034-4035-4036-4037",
    4016: "4016-4017-4018-4019",
    4038: "4038-4039-4040-4041-4042-4046-4047-4050-4051-4057-4059-4060-4062-4063-4066",
    4069: "4069-4071",
    4083: "4083-4087-4089",
    4091: "4091-4093",
    4094: "4094-4097-4098-4101-4103-4106-4109-4112-4113-4115-4116-4120",
}


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


class TelepresenceImageBase(Image):
    """Shared, ERA-keyed base image (two-tier strategy).

    Built ONCE per go-minor era and reused by every PR of that era: the tag is
    `base-<minor>`, so all PRs in an era resolve to the same image_full_name and
    the harness dependency graph dedups them to a single build. The base:
      * clones the repo once (NO BASE_COMMIT — only the REPO_URL arg),
      * installs the common system dependencies (apt) once,
      * warms the Go module cache once (best-effort).
    Each per-PR image then only checks out its BASE_COMMIT, fetches any remaining
    era-specific deps, and runs the hardening pass — so the expensive clone +
    common-dependency install is NOT repeated per PR.

    Begins with the BuildKit syntax directive so DockerfileEnhancer.enhance()
    returns it verbatim (no proxy/cert/MITM injection). The base deliberately
    does NOT run the hardening block (it has no BASE_COMMIT and keeps full
    history); the anti-cheat git-history strip runs in the PR layer, and the
    pruned .git is what the eval container sees at runtime.
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
        # Era-matched external golang image (see _GO_MINOR_BY_PR). One base per
        # era is mandatory: the repo's go directive climbs 1.15 -> 1.26 and old
        # code won't compile on a new toolchain, so a single base cannot span
        # all 72 PRs.
        minor = _GO_MINOR_BY_PR.get(self.pr.number, "1.24")
        return f"golang:{minor}"

    def image_tag(self) -> str:
        # ERA-keyed (NOT pr-keyed) so the base is shared by all PRs of the era.
        minor = _GO_MINOR_BY_PR.get(self.pr.number, "1.24")
        return f"base-{minor}"

    def workdir(self) -> str:
        minor = _GO_MINOR_BY_PR.get(self.pr.number, "1.24")
        return f"base-{minor}"

    def extra_packages(self) -> list[str]:
        return ["pkg-config"]

    def files(self) -> list[File]:
        return []

    def _get_apt_update_command(self, packages_str: str, base_img: str) -> str:
        # golang:1.15..1.20 are built on Debian buster/bullseye, whose apt repos
        # have moved to archive.debian.org -> force the archive-rewrite branch
        # and make it non-fatal (the golang image already ships git/gcc/curl).
        dep = self.dependency()
        if any(dep.startswith(f"golang:{v}") for v in _OLD_DEBIAN_GO):
            cmd = super()._get_apt_update_command(packages_str, "debian:buster")
            return cmd + " || true"
        return super()._get_apt_update_command(packages_str, base_img)

    def dockerfile(self) -> str:
        base_img = self.dependency()
        default_packages = [
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
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        repo = self.pr.repo
        repo_url = f"https://github.com/{self.pr.org}/{repo}.git"

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {base_img}",
            ('ARG TARGETARCH\n' f'ARG REPO_URL="{repo_url}"'),
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8",
            "ENV GOTOOLCHAIN=auto\nENV CGO_ENABLED=1\nENV GOWORK=off",
            apt_command,
            f'RUN git clone "${{REPO_URL}}" /home/{repo}',
            f"WORKDIR /home/{repo}",
            # Warm the common Go module cache once (best-effort: the default-branch
            # go.mod may need a newer toolchain than this era's image, in which
            # case this is a no-op and the per-PR `go mod download` fills the gap).
            "RUN go mod download 2>&1 || true",
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class TelepresenceImageDefault(Image):
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
        # Two-tier: depend on the shared per-era base image. Returning an Image
        # (not a string) makes DockerfileEnhancer.enhance() return our Dockerfile
        # verbatim (no proxy/cert/MITM injection) AND tells the harness to build
        # the base first and reuse it across every PR of the era.
        return TelepresenceImageBase(self.pr, self._config)

    def dockerfile(self) -> str:
        # PR layer on top of the shared base: checkout this PR's base commit,
        # (re)download era-specific deps + warm build cache (extra_setup ->
        # prepare.sh), then run the _HARDENING_BLOCK (the anti-cheat git-history
        # strip — what the eval container sees at runtime). The common clone +
        # apt deps + module cache are inherited from the base, not repeated here.
        #
        # IMPORTANT: build_dataset only injects the BASE_COMMIT/REPO_URL build
        # args when dependency() is a STRING. Our dependency() is the base Image,
        # so ${BASE_COMMIT} would be UNSET at build time. We therefore bake the
        # literal base.sha into the checkout + the hardening block instead of
        # relying on the build arg.
        base = self.dependency().image_full_name()
        repo = self.pr.repo
        sha = self.pr.base.sha
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {base}",
            f"WORKDIR /home/{repo}",
            f"RUN git reset --hard\nRUN git checkout {sha}",
        ]
        extra = self.extra_setup()
        if extra:
            sections.append(extra)
        sections.append(hardening)
        sections.append('CMD ["/bin/bash"]')
        return "\n\n".join(sections) + "\n"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # build-essential is already in the default package set; pkg-config is
        # needed by the cgo test package (pkg/client/cli/env/syntax_test.go).
        return ["pkg-config"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Sets persistent Go env, stages the runtime helper scripts +
        # patches into /home/, and warms the module cache via prepare.sh. The
        # copied files live outside /home/{repo}, so the hardening pass (which
        # only operates inside the git tree) leaves them untouched.
        #   - GOTOOLCHAIN=auto: honour each base.sha's go/toolchain directive.
        #   - CGO_ENABLED=1: the syntax_test.go cgo package.
        #   - GOWORK=off: keep each sub-module's own go.mod authoritative
        #     (avoids -mod conflicts if a future release adds go.work).
        return (
            "ENV GOTOOLCHAIN=auto\n"
            "ENV CGO_ENABLED=1\n"
            "ENV GOWORK=off\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run_tests.sh /home/run_tests.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

    def files(self) -> list[File]:
        repo = self.pr.repo
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        prepare = """#!/bin/bash
# Repo is already cloned + checked out at ${BASE_COMMIT} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# only warms the Go module/build caches so the eval runs don't need network.
set -e
cd /home/__REPO__
git reset --hard || true

export GOWORK=off
go mod download 2>&1 || true
go build ./... 2>&1 || true
""".replace("__REPO__", repo)

        # Multi-module aware runner: walk up from each test pkg dir to find its
        # nearest go.mod ancestor; run `go test` from there with the relative
        # path. GOWORK=off keeps each sub-module's own go.mod authoritative.
        # GOEXPERIMENT=jsonv2 is enabled per-modroot ONLY when the resolved
        # toolchain accepts it — newest PRs (go 1.26+) import encoding/json/v2,
        # but an older toolchain (pinned by a `toolchain` directive under
        # GOTOOLCHAIN=auto) would hard-error on an unknown experiment name.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__

export GOWORK=off
unset GOFLAGS 2>/dev/null || true
export GOTOOLCHAIN=auto
export CGO_ENABLED=1

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  # Find nearest ancestor with go.mod
  d="$pkg"
  while [ -n "$d" ] && [ "$d" != "." ] && [ ! -f "$d/go.mod" ]; do
    parent=$(dirname "$d")
    [ "$parent" = "$d" ] && break
    d="$parent"
  done
  if [ -f "$d/go.mod" ]; then
    modroot="$d"
  else
    modroot="."
  fi
  # Compute relative path inside the module's root.
  if [ "$modroot" = "$pkg" ]; then
    rel="."
  elif [ "$modroot" = "." ]; then
    rel="$pkg"
  else
    rel="${pkg#$modroot/}"
  fi
  echo "### TLPKG: $pkg ###"
  (
    cd "$modroot"
    EXP=""
    # `go env` validates GOEXPERIMENT, so this enables jsonv2 only on a
    # toolchain that actually recognises it (go1.25+) and silently skips it on
    # older eras (where the name is unknown and would hard-error every command).
    if GOEXPERIMENT=jsonv2 go env GOVERSION >/dev/null 2>&1; then
      EXP="jsonv2"
    fi
    GOEXPERIMENT="$EXP" go mod download 2>/dev/null || true
    GOEXPERIMENT="$EXP" go test -v -count=1 -vet=off -timeout=20m "./$rel/" 2>&1
  ) || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        # Per-file split-apply: `git apply --3way` is all-or-nothing across the
        # whole patch — if any single hunk fails the 3way merge, none of the
        # patch's NEW files get created either. Split by `diff --git` boundaries
        # and apply each file's hunks independently. Bundle patches are large
        # (some fix.patch > 400 KB) so this materially improves apply coverage.
        apply_split = """split_apply() {
  local pf="$1"
  local td
  td=$(mktemp -d)
  awk 'BEGIN{i=0} /^diff --git /{i++; f=sprintf("%s/p%04d.patch","'"$td"'",i)} f{print > f}' "$pf"
  local applied=0 failed=0
  for f in "$td"/p*.patch; do
    [ -s "$f" ] || continue
    if git apply --3way --whitespace=nowarn __EXCLUDES__ "$f" >/dev/null 2>&1; then
      applied=$((applied+1))
    elif git apply --whitespace=nowarn __EXCLUDES__ "$f" >/dev/null 2>&1; then
      applied=$((applied+1))
    else
      failed=$((failed+1))
    fi
  done
  rm -rf "$td"
  echo "split_apply $pf: applied=$applied failed=$failed"
}
""".replace("__EXCLUDES__", excludes)

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
__APPLY_SPLIT__
split_apply /home/test.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__APPLY_SPLIT__", apply_split)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
__APPLY_SPLIT__
split_apply /home/test.patch
split_apply /home/fix.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__APPLY_SPLIT__", apply_split)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


@Instance.register("telepresenceio", "telepresence")
class Telepresence(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TelepresenceImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestParse (0.01s)
        #   --- FAIL: TestConnect (0.02s)
        #   --- SKIP: TestIntegration (0.00s)
        # Fenced by `### TLPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### TLPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

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


# Instance.create() routes by `{org}/{number_interval}` when number_interval is
# set on the PR (the raw jsonl now sets it = dash-joined prs_in_bundle). The
# @Instance.register decorator above only registers the bare `telepresenceio/
# telepresence` key (used for records WITHOUT a number_interval), so we also
# register the Telepresence class under every interval key here. All of them map
# to the same class; the per-era golang base + per-PR behaviour is keyed on
# pr.number (see _GO_MINOR_BY_PR), independent of the registration key.
for _pr_num, _interval in _NUMBER_INTERVAL_BY_PR.items():
    Instance._registry[f"telepresenceio/{_interval}"] = Telepresence
