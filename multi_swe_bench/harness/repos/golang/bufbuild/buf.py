import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# bufbuild/buf — the Protobuf CLI / linter / breaking-change detector (Go).
#
# Discovery (dataset analysis):
#  - 134-PR range #1..#4492. Single Go module. Test files moved across eras
#    (internal/ -> private/ -> cmd/) but it stays one `go test` build; a recent
#    Go toolchain with GOTOOLCHAIN=auto covers the whole go.mod version span.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs each. Runs are fenced with a `### BUFPKG ###`
#    marker so test ids stay unique across packages. buf's unit tests are
#    self-contained Go tests — no cloud credentials needed.
#
# Build structure (shared base + single-FROM hardened PR layer):
#  - BufImageBase -> tag ":base". Built once and reused by every PR. Installs
#    the common apt packages and does the single `git clone` of the whole repo
#    (full history, so any PR's BASE_COMMIT is reachable). Deliberately NOT
#    hardened — it must retain every commit to serve all PRs. Its Dockerfile
#    carries the BuildKit `# syntax=` directive so DockerfileEnhancer leaves it
#    verbatim (else the enhancer would inject a BASE_COMMIT-dependent hardening
#    pass into the base, which has no BASE_COMMIT and would fail to build).
#  - BufImageDefault -> tag ":pr-<n>". Single-FROM layered build off the shared
#    ":base": pin/checkout BASE_COMMIT -> COPY per-PR patches/scripts -> prep ->
#    Image._HARDENING_BLOCK (prunes every other ref/remote/commit) -> CMD.
#
#  Reward-hacking note: hardening sanitizes the *runtime* filesystem, but with a
#  single FROM the :base layer's full-history packfile sits below the hardening
#  layer, so a layered `docker save` can be raw `tar -x`'d to recover the fix.
#  Distribute FLATTENED images (`docker export`, or build with `--squash`) and
#  never ship the standalone ":base" image to an evaluated model.


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


class BufImageBase(Image):
    """Shared base image (tag ':base'): apt deps + a single full-history clone.

    Built once and reused by every PR. Deliberately un-hardened (it must keep
    all commits so any PR's BASE_COMMIT is reachable). The leading `# syntax=`
    directive makes DockerfileEnhancer return this Dockerfile verbatim, so it
    does not get a BASE_COMMIT-dependent hardening pass injected (which would
    fail to build here — BASE_COMMIT only exists in the PR layer).
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

    def dependency(self) -> Union[str, "Image"]:
        return "golang:1-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM golang:1-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

CMD ["/bin/bash"]
"""


class BufImageDefault(Image):
    """Per-PR image (tag ':pr-<n>'): single-FROM layered build off the shared
    :base. Pins/checks out BASE_COMMIT, copies patches+scripts, preps, then runs
    Image._HARDENING_BLOCK in this layer.

    NOTE: single-FROM means the :base layer's full-history clone sits below the
    hardening layer, so a layered `docker save` can still be `tar -x`'d to
    recover the fix. Distribute FLATTENED images (`docker export` / `--squash`).
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

    def dependency(self) -> Union[str, "Image"]:
        # Chained Image -> the per-PR layer builds FROM the shared ":base".
        # (DockerfileEnhancer returns dockerfile() verbatim for a chained
        # dependency, so the layout below is exactly what ships.)
        return BufImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_files = " ".join(f.name for f in self.files())

        # Industry-standard single-FROM layered pattern. Image._HARDENING_BLOCK
        # is concatenated (not f-stringed) so its ${BASE_COMMIT}/%(refname)
        # braces stay literal and byte-identical to image.py.
        head = f"""FROM {base_name}

# 1. Build-time args first (overridable via --build-arg)
ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

# 2. WORKDIR before any RUN/COPY that depends on it
WORKDIR /home/{repo}

# 3. Git checkout BEFORE copying patches (clean, known state)
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

# 4. COPY scripts/patches late so earlier expensive layers stay cached
COPY {copy_files} /home/

# 5. Install / prep
RUN bash /home/prepare.sh

# 6. Repo cleanup (hardening) — kept as-is, synced with image.py
"""
        return head + self._HARDENING_BLOCK + '\nCMD ["/bin/bash"]\n'

    def files(self) -> list[File]:
        repo = self.pr.repo
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        check_git = """#!/bin/bash
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
"""

        # The repo is already checked out at ${BASE_COMMIT} and hardened by the
        # shared Image.dockerfile(), so prepare.sh no longer performs any git
        # checkout itself — it only warms the module cache. The download is
        # allowed to fail (|| true) because its only purpose here is caching;
        # the real pass/fail signal comes from the run/test-run/fix-run scripts.
        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
go mod download 2>/dev/null || true
# Drop any go.sum churn from the cache warm-up so the tree is clean before the
# hardening pass detaches onto ${BASE_COMMIT}.
git reset --hard 2>/dev/null || true
""".replace("__REPO__", repo)

        # Per-package `go test`. -vet=off avoids vet-only failures masking the
        # real test outcome.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### BUFPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
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

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


@Instance.register("bufbuild", "buf")
class Buf(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BufImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestLint (0.01s)
        #   --- FAIL: TestBreaking (0.02s)
        #   --- SKIP: TestRegistry (0.00s)
        # Fenced by `### BUFPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### BUFPKG:\s+(\S+)\s+###")

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

        # Disjoint sets: failed > skipped > passed.
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


# Route bundled PRs that carry a dash-joined number_interval (the list of
# prs_in_bundle, e.g. "152-153-155-156-157") to this single-era config.
# Instance.create() looks up f"{org}/{number_interval}", so each bundle's
# interval string must be registered against this class.
_NUMBER_INTERVALS = [
    "4480-4498-4499-4500-4502-4503-4505-4511-4512-4513-4514-4515-4516-4517-4518-4519-4520-4521",
    "4468-4469-4471-4472",
    "4437-4473-4476-4477-4478-4479-4481",
    "4381-4382-4383-4384-4385-4386-4387-4388-4394-4395-4396-4397-4398-4401-4403-4406-4409-4410-4413-4414-4419-4420-4421-4423-4424-4426",
    "4351-4402-4407-4408-4416-4427-4428-4435-4436-4438-4439-4442-4443-4444-4445-4446-4447-4448-4449-4450-4451-4452-4453-4460-4461-4462-4463-4464-4466-4467",
    "4344-4355-4356-4357-4358-4359-4360-4364-4365-4366-4369-4378-4379-4380",
    "4317-4318-4321-4322-4323-4324-4325-4326-4327-4333-4334-4335-4336-4337-4338-4339-4341-4342-4343-4345-4346-4347-4350-4353-4354",
    "4294-4296-4297-4298-4299-4300-4303-4304-4305-4306-4307-4308-4309-4310-4311-4314-4315-4316",
    "4262-4263-4264-4265-4266-4267-4268-4269-4270-4275-4277-4278-4279-4280-4282-4283-4284-4285-4286-4292-4293",
    "4247-4249-4250-4253-4254-4255-4256-4257-4258-4259-4260-4261",
    "4244-4245-4246",
    "4191-4194-4195-4196-4201-4202-4203-4204-4208-4212-4214-4215-4216-4219-4222-4224-4226-4227-4228-4229-4230-4231-4232-4239-4240-4241-4242-4243",
    "4152-4169-4171-4172-4178-4179-4180-4185-4186-4188-4189-4190",
    "4026-4043-4045-4054-4057-4058-4059-4060-4062-4063-4064-4065-4066-4067-4068-4069-4070-4071-4073-4074-4075-4076-4077-4078-4079-4080-4081-4082-4083-4084",
    "4018-4020-4021-4024-4025-4029-4036-4039-4041-4042-4044",
    "3963-3972-3983-3986-3991-3992-3994-3995-4002-4010-4011-4012-4013-4014",
    "3785-3843-3844-3848-3850-3854-3855-3858-3859-3860-3861-3862-3863-3864-3865-3866-3876-3877",
    "3780-3794-3796-3871-3885-3886-3887-3889-3895-3896-3898-3900-3901-3902-3906-3909-3910-3911-3915-3916-3923-3924-3933-3934-3936-3938-3942",
    "3769-3782-3783-3784-3788-3789-3790-3791-3792-3793-3803-3805-3809-3810-3813-3818-3819-3820-3821-3822-3823-3825-3826-3827-3829-3831-3837-3838-3839-3840-3841-3842",
    "3758-3804-3806-3912-3939-3943-3945-3946-3947-3948-3954-3955-3956-3958-3959-3960-3962-3965-3967-3968-3969-3979-3980-3981-3982-3984",
    "3740-3754-3755-3756-3757-3759-3760-3762-3768-3770-3771-3772-3774-3775-3776-3779-3781",
    "3714-3716-3717-3724-3726-3727-3728-3729-3732-3733-3734-3735-3736-3737-3739-3747-3748",
    "3624-3652-3665-3670-3674-3678-3679-3680-3681-3682-3683-3684-3685-3686-3687-3688-3689-3690-3694-3695-3696-3698-3707-3708-3709-3710-3711-3713",
    "3591-3597-3602-3603-3608-3611-3613-3618-3619-3623-3625-3629-3631-3632-3638-3639-3647-3648-3649-3653-3660-3664-3671-3672-3673-3675-3676-3677",
    "3576-3577-3578-3579-3582-3589-3592-3594-3595-3596",
    "3558-3560-3561-3562-3563-3564-3565-3568-3569-3570-3571-3574-3575",
    "3431-3433-3435-3436-3439-3441-3443-3446-3447-3449-3450-3451-3452-3454-3463-3466",
    "3404-3411-3468-3473-3474-3475-3479-3480-3482-3483-3486-3487-3494-3495-3496-3497-3498-3499-3507-3508-3509-3512-3513-3521-3523-3524-3525-3526-3528-3530-3531-3534-3535-3536-3538-3540-3545-3547-3548-3549-3550-3553-3556-3557",
    "3370-3392-3393-3394-3395-3396-3397-3403-3406-3408-3409-3410-3415-3416-3417-3418-3419-3420-3421-3422-3424-3427-3428-3430-3432",
    "3366-3367-3372-3373-3374-3375-3376-3377-3378-3379-3385-3386-3387-3388-3390-3391",
    "3317-3349-3355-3356-3358-3363-3364-3365",
    "3316-3331-3332-3333-3334-3340-3342-3344-3345-3346-3347-3348-3350-3352-3354-3357",
    "3313-3314-3315-3318-3319-3320-3324-3325-3326-3328-3329-3330",
    "3297-3299-3300-3302-3303-3305-3307-3309-3311-3312",
    "3293-3294-3295-3296",
    "3173-3260-3262-3264-3266-3267-3268-3269-3270-3271-3272",
    "3130-3132-3167-3169-3170-3181-3187-3188-3196-3198-3200-3201-3202-3204-3216",
    "3127-3139-3206-3229-3233-3235-3238-3245-3246-3247-3250-3251-3255-3258-3259-3261",
    "3095-3179-3182-3184-3186",
    "3079-3081-3088-3090-3092-3093-3096-3097-3098-3102-3103-3104",
    "3010-3123-3214-3217-3220-3221-3222-3223-3225-3231-3232-3234-3237-3239-3241-3244",
    "2999-3025-3029-3030-3031-3032-3036-3037-3038-3039-3042-3043-3044-3046-3050-3051-3053-3054-3055-3058-3061-3062-3068-3070-3072-3073-3074-3075-3077-3078",
    "2992-2993-2994-2997-3006-3007-3009-3013-3015-3017-3018-3021-3022-3027-3028",
    "2985-2989-3001-3003-3004",
    "2982-3071-3105-3107-3108-3109-3110-3111-3113-3114-3116-3124-3128-3131-3133-3134-3136-3143-3149-3150-3151-3153-3156-3162-3164-3172-3178",
    "2633-2732-2741-2742-2744-2745-2747-2748-2750-2752-2754-2755-2757-2758-2766-2769-2770-2781-2785-2786-2787-2789-2792-2794-2795-2797-2800-2801-2803-2804-2806-2807-2808",
    "2559-2563-2566-2567-2568-2569-2570-2571-2572-2573-2579-2581-2582-2583-2584-2586-2587-2590-2591",
    "2503-2504-2508-2509-2510-2511-2518",
    "2473-2515-2517-2519-2520-2521-2522-2523-2524-2525-2526-2527-2528-2530-2531-2532-2533-2534-2535-2536-2538-2544-2545-2546-2547-2548-2550-2552-2553-2554-2555-2558-2560-2561-2564-2565",
    "2470-2585-2588-2592-2593-2594-2595-2596-2597-2598-2599-2600-2602-2603-2604-2605-2612-2614-2615-2616-2618-2619-2620-2621-2624-2625-2626-2630-2634-2635-2647-2648-2650-2652-2654-2656-2657-2659-2663-2671-2672-2673-2690-2693-2694-2697-2706-2710-2711-2712-2717-2718-2719-2721-2724-2726-2728-2729-2731",
    "2452-2453-2454-2455-2461-2478-2479-2486-2487-2494-2495-2496-2497-2501-2502",
    "2354-2357-2363-2367-2368-2369-2371-2372-2374-2375-2379-2380-2381-2382-2383-2384-2388-2389-2391-2392-2394-2395-2396-2401-2402-2404-2406-2409-2410-2411-2413-2419-2420-2421-2423-2424-2426-2428-2429-2430-2431-2433-2434-2435-2436-2438-2439-2441-2442-2443-2444-2445-2446-2447-2456-2457-2458-2464-2465-2471-2475-2476-2477",
    "2330-2336-2339-2340-2341-2342-2356-2358",
    "2291-2293-2294-2296-2299-2300-2301-2305",
    "2256-2278-2302-2306-2307-2308-2309-2310-2312-2313-2315-2316-2317-2319-2320-2322-2323-2324-2325-2326-2327-2328-2329-2331-2332-2334-2335-2337-2338",
    "2243-2244-2245-2246-2248-2249-2250",
    "2151-2217-2222-2223-2224-2225-2227-2228-2229-2230-2232-2233-2234-2235-2236-2237-2239-2240-2242",
    "2150-2161-2162-2163-2164-2168-2169-2170-2171-2173-2174-2176-2178-2180-2181-2182-2183-2184-2185-2186-2188-2189-2190-2192-2193-2194-2196-2197-2198-2199-2201-2202-2203-2210-2212-2214-2215-2218-2220-2221",
    "2122-2136-2137-2139-2141-2143-2144-2145-2146-2147-2148-2152-2157-2158-2159-2160",
    "2118-2241-2251-2253-2254-2257-2258-2259-2260-2261-2262-2264-2265-2266-2273-2274-2275-2276-2277-2279-2281-2282-2283-2284-2287-2288-2289-2290",
    "2073-2081-2101-2102-2103-2105-2109-2111-2113-2114-2115-2116-2117-2119-2121-2128-2131-2132-2133-2134-2135",
    "2052-2053-2055-2056-2057-2058-2059-2060-2061-2063-2066-2072-2074-2075-2076-2077-2079-2082-2083-2085-2086-2089-2093-2094-2095-2096-2097-2098-2099-2100",
    "1977-1979-1980-1981-1987-1988-1990-1991-1992-1993-1995-1996-1998-2001-2002-2004-2007-2008-2010-2011-2012-2013-2014-2015-2016-2019-2020-2022-2023-2026-2027-2028-2029-2031-2034-2036-2042-2046-2047-2048-2050-2051",
    "1954-1956-1958-1959-1961-1962-1963-1965-1966-1967-1968-1970-1971-1972-1973-1974-1976",
    "1867-1868-1869-1870-1871-1874-1876-1877-1878-1879-1880-1882-1885-1891-1893-1894-1895-1898-1899",
    "1835-1892-1897-1900-1901-1902-1910-1912-1913-1914-1916-1919-1921-1922-1923-1925-1926-1927-1928-1929-1931-1933-1934-1935-1937-1938-1939-1940-1942-1943-1944-1947-1950-1951-1952-1953",
    "1785-1815-1816-1817-1818-1819-1820-1822-1824-1827-1833-1834-1836-1837-1838-1840-1842-1843-1844-1846-1847-1849-1851-1852-1853-1854-1855-1856-1858-1860-1861-1862-1863-1864-1866",
    "1729-1730-1758-1762-1767-1770-1771-1777-1778-1779-1781-1783-1787-1791-1792-1793-1796-1797-1798-1800-1801-1804-1807-1808-1809-1810-1811-1812-1813",
    "1677-1687-1688-1689-1690-1691-1692-1694-1695-1696-1697-1698-1702-1703-1704-1705-1706-1710-1711-1712-1713-1714-1722-1726",
    "1625-1643-1644-1652-1656-1657-1658-1659-1660-1661-1663-1665-1674-1675-1678-1682-1684-1686",
    "1619-1622-1724-1727-1728-1731-1733-1734-1737-1738-1739-1740-1741-1743-1744-1747-1751-1752-1753-1754-1755-1756-1757-1759-1763-1764",
    "1471-1494-1503-1513-1514-1516-1517-1518-1519-1520-1521-1522-1524-1530-1531-1532-1533-1534-1535-1537-1538-1539-1540-1541-1545-1549-1551-1552-1553-1554-1556-1559-1560-1561-1562-1563-1564-1567-1569-1571-1573-1574-1575-1577-1578-1579-1580-1581-1582-1586-1588-1591-1592-1594-1595-1596-1597-1598-1599-1600-1602-1603-1604-1607-1608-1611-1615-1616-1617-1626-1627-1628-1630-1633-1634-1635-1636-1638-1647-1650-1654-1655",
    "1368-1378-1379-1386-1387-1403-1408-1414-1416-1417-1418-1419-1422-1423-1425-1426-1427-1428-1430-1433-1438-1445-1447-1450-1455-1456-1457-1459-1460-1461-1463-1464-1465-1466-1467-1468-1472-1474-1476-1483-1484-1486-1487-1488-1489-1490-1495-1500-1501-1506-1507-1510-1511",
    "1198-1277-1288-1303-1304-1305-1306-1307-1308-1313-1316-1317-1318-1319-1323-1324-1325-1326-1327-1328-1329-1330-1331-1332-1333-1337-1341-1342-1343-1347-1348-1349-1353-1354-1355-1357-1359-1360-1362-1366-1367-1369-1371-1372-1373-1374-1375-1376-1377-1381-1385-1390-1394-1395-1397-1398-1399-1401-1402",
    "1124-1137-1161-1162-1163-1165-1166-1167-1168-1170-1172-1173-1175-1178-1181-1182-1185-1188-1189-1190-1192-1193-1194-1201-1203-1205-1213-1221",
    "1026-1029-1033",
    "1021-1034-1040-1041-1042-1043-1045-1046",
    "967-1011-1018-1023",
    "946-949-957-960-961-962-963-964-965-966-968-969-971-974-976-977-978",
    "907-1044-1049-1053-1054-1055-1057-1061-1063-1064-1072-1078-1082",
    "755-1083-1085-1088-1089-1090-1091-1092-1097-1098-1101-1104-1105-1109-1110-1111-1114-1115-1116-1117-1118-1129-1131-1132-1133-1135-1136-1139-1140-1141-1142-1143-1144-1147-1148-1153-1154-1156-1157-1158",
    "518-519-520-521-523-525",
    "477-479-480-500-502-503-505-506-508-509-512-513-514-515-516",
    "451-452-453-455-457-458-459-460-461-462-464-466-467-469-472-473-474-475",
    "437-440-441-447-449-450",
    "429-430-431-436",
    "421-422",
    "411-425",
    "410-414-416",
    "399-400-401-402-405-406",
    "386-388",
    "372-373-374-380-381-382-384",
    "324-326-327-328",
    "311-318-319-322",
    "289-296-298-300",
    "281-286",
    "266-268-270",
    "245-246-247-248-250-251-252-256-257-260",
    "240-243",
    "231-235",
    "216-219-221-222-229-230",
    "214-215",
    "212-213",
    "191-192-194-196-197-198-199",
    "185-187",
    "164-165-167",
    "162-163",
    "158-159-160",
    "152-153-155-156-157",
    "142-145-147",
    "137-139",
    "124-127-131-134-136",
    "119-121-122",
    "111-112-114-115",
    "94-95-97-98-100-101",
    "88-93",
    "83-84",
    "74-75-76-77-78",
    "72-73",
    "64-65-66-67-68-69-70-71",
    "49-53-54-55",
    "46-47",
    "41-43",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("bufbuild", _interval)(Buf)
