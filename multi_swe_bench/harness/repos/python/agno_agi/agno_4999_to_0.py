import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
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
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        return "base-agno_4999_to_0"

    def workdir(self) -> str:
        return "base-agno_4999_to_0"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # `# syntax` opts this shared era base out of the DockerfileEnhancer,
        # which would otherwise rewrite the `git clone` into clone + `git checkout
        # ${BASE_COMMIT}` + prune HERE, pruning the shared base to one PR's
        # base.sha and breaking every other PR in the era ("reference is not a
        # tree"). The base keeps FULL history; the anti-reward-hack hardening runs
        # per-PR at the literal base.sha (see ImageDefault below).
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
RUN pip install --no-cache-dir --upgrade pip uv
{self.clear_env}
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
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}
cd /home/{self.pr.repo}/libs/agno
EXTRAS_LIST=$(python -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml','rb') as f:
    data = tomllib.load(f)
SKIP = {{'lmstudio', 'brave', 'brave-search', 'brave_search', 'infinity', 'infinity_client'}}
extras = [k for k in data.get('project', {{}}).get('optional-dependencies', {{}}).keys() if k not in SKIP]
print(','.join(extras))
print('---')
print('\\n'.join(extras))
" 2>/dev/null)
EXTRAS_CSV=$(echo "$EXTRAS_LIST" | sed -n '1p')
EXTRAS_ITER=$(echo "$EXTRAS_LIST" | sed -n '3,$p')
UV_OPTS="--no-cache --python /usr/local/bin/python --index-strategy unsafe-best-match"
if [ -n "$EXTRAS_CSV" ] && uv pip install $UV_OPTS -e ".[${{EXTRAS_CSV}}]"; then
    echo "--- bulk install succeeded ---"
else
    echo "--- bulk install failed; falling back to per-extra loop ---"
    uv pip install $UV_OPTS -e ".[dev]" \\
        || uv pip install $UV_OPTS -e . \\
        || pip install --no-cache-dir -e ".[dev]" \\
        || true
    if [ -n "$EXTRAS_ITER" ]; then
        for extra in $EXTRAS_ITER; do
            [ "$extra" = "dev" ] && continue
            echo "--- installing extra: $extra ---"
            uv pip install $UV_OPTS -e ".[${{extra}}]" || true
        done
    fi
fi
uv pip install $UV_OPTS sqlalchemy pypdf chromadb lancedb tantivy qdrant-client pgvector pillow || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true; \\
                grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/fix.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
GIT_BIN_EXCL="--exclude=*.pdf --exclude=*.docx --exclude=*.doc --exclude=*.swp \\
              --exclude=*.jpg --exclude=*.jpeg --exclude=*.png --exclude=*.gif --exclude=*.ico --exclude=*.bmp --exclude=*.svg \\
              --exclude=*.zip --exclude=*.tar --exclude=*.gz --exclude=*.bz2 --exclude=*.xz \\
              --exclude=*.bin --exclude=*.so --exclude=*.dll --exclude=*.exe --exclude=*.pyc \\
              --exclude=*.mp3 --exclude=*.mp4 --exclude=*.wav --exclude=*.ogg --exclude=*.webm \\
              --exclude=*.pkl --exclude=*.npz --exclude=*.npy --exclude=*.db --exclude=*.sqlite"
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn $GIT_BIN_EXCL /home/test.patch; then
        echo "Error: git apply test.patch failed (even after excluding binary files)" >&2
        exit 1
    fi
    echo "Note: test.patch applied with binary file exclusions"
fi
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
GIT_BIN_EXCL="--exclude=*.pdf --exclude=*.docx --exclude=*.doc --exclude=*.swp \\
              --exclude=*.jpg --exclude=*.jpeg --exclude=*.png --exclude=*.gif --exclude=*.ico --exclude=*.bmp --exclude=*.svg \\
              --exclude=*.zip --exclude=*.tar --exclude=*.gz --exclude=*.bz2 --exclude=*.xz \\
              --exclude=*.bin --exclude=*.so --exclude=*.dll --exclude=*.exe --exclude=*.pyc \\
              --exclude=*.mp3 --exclude=*.mp4 --exclude=*.wav --exclude=*.ogg --exclude=*.webm \\
              --exclude=*.pkl --exclude=*.npz --exclude=*.npy --exclude=*.db --exclude=*.sqlite"
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn $GIT_BIN_EXCL /home/test.patch /home/fix.patch; then
        echo "Error: git apply test+fix patches failed (even after excluding binary files)" >&2
        exit 1
    fi
    echo "Note: test+fix patches applied with binary file exclusions"
fi
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true; \\
                grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/fix.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        # Per-PR anti-cheat hardening at the LITERAL base.sha. The shared base
        # keeps full history so every PR's base.sha is reachable; prepare.sh
        # checks out this PR's base.sha, then this block detaches at that literal
        # sha and strips every other ref/reflog so the fix commit is unreachable
        # from git. Runs in the PR layer because the shared base must NOT prune.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")
        return f"""# syntax=docker/dockerfile:1.6
FROM {dep.image_name()}:{dep.image_tag()}
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


@Instance.register("agno-agi", "agno_4999_to_0")
class AGNO_4999_TO_0(Instance):
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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        passed_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+PASSED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        failed_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+FAILED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        skipped_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+SKIPPED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        passed_tests.update(passed_pattern.findall(clean_log))
        failed_tests.update(failed_pattern.findall(clean_log))
        skipped_tests.update(skipped_pattern.findall(clean_log))

        summary_failed = re.compile(r"^FAILED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        summary_passed = re.compile(r"^PASSED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        summary_skipped = re.compile(r"^SKIPPED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        failed_tests.update(summary_failed.findall(clean_log))
        passed_tests.update(summary_passed.findall(clean_log))
        skipped_tests.update(summary_skipped.findall(clean_log))

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


# Defensive default alias: register the early-era class under the bare repo key
# so PRs without a number_interval don't crash Instance.create lookups.
Instance.register("agno-agi", "agno")(AGNO_4999_TO_0)


# --- §11b bundle keys: dash-joined prs_in_bundle for this era -> AGNO_4999_TO_0. 92 bundles.
_BUNDLE_NIS_AGNO_E1 = [
    "1861-2305-2307-2315-2316-2325-2333-2342-2348-2351-2353-2360-2364-2365-2369-2375",
    "1888-1956-1961-1971-1984-1989-1991",
    "1951-1969-1973-1979",
    "1962-1986-1996-2006-2010",
    "1994-1995-2002-2004-2013-2016",
    "2058-2448-2450-2468-2485-2486",
    "2066-2094-2102-2108-2117-2118-2125-2132-2138-2140-2141",
    "2086-2089-2090-2091-2096-2097-2098-2101-2103-2104-2106-2110-2111-2123-2124",
    "2105-2195-2197-2283-2284-2287-2289-2290-2292-2311-2312",
    "2120-2157-2214-2226-2227-2229",
    "2150-2294-2372-2378-2380-2387",
    "2220-2221",
    "2246-2280-2355-2356-2377-2379-2396-2398-2399-2406-2407-2408-2411-2416-2421-2423-2425-2432-2438-2439-2445-2449-2453-2455-2456-2461-2466",
    "2368-2381-2389-2393-2394",
    "2457-2656-2804-2939-2942-2950-2951",
    "2490-2526-2555-2580-2584-2588-2590-2591-2593-2598-2603-2609-2614-2615-2617-2618-2619-2621-2626-2643-2645-2646",
    "2518-2648-2662-2664-2667-2672-2682-2696-2711-2716-2718",
    "2519-2522-2523-2524-2525",
    "2520-2521",
    "2566-3012-3058-3064-3070-3072-3075-3077-3084-3085-3086",
    "2568-2765-2779-2814-2815-2823-2831-2840-2841-2842-2843-2851-2858-2862-2863-2866-2867-2868",
    "2655-2660-2683-2684",
    "2658-2659-2676-2680",
    "2687-2748-2751-2757-2773-2775",
    "2693-2694",
    "2736-2853-2890-2915-2917-2918-2922-2923-2930-2932-2935-2936-2941-3180",
    "2807-2811",
    "3375-3467-3519-3521-3524-3530",
    "4390-5115-5124-5130-5132",
    "4575-5021-5053-5057-5061-5063-5064",
    "4793-4796-4798",
    "1598-1906-2076-2109-2121-2135-2154-2167-2168-2169-2170-2178-2180-2181-2182-2187-2189-2191-2192-2196-2199-2202-2203-2205-2212-2215-2216",
    "1882-2077-2409-2420-2673-2880-3024-3227-3315-3346-3368-3372-3378-3380-3388-3405-3420-3436-3441-3444-3453-3456-3460-3468-3471-3473-3474-3475-3477-3478-3479-3482-3483-3485",
    "1975-3517-3529-3531-3532-3533-3536-3543-3546-3547-3549-3550-3557-3558",
    "1976-2018-2025-2061-2062-2067-2070-2074-2078-2084-2085-2087-2088",
    "2023-2092-2144-2146-2152-2155-2156-2160",
    "2095-2306-2395-2413-2629-2704-3754-3760-3817-3887-3890-3898-3914-3917-3920-3921-3927-3928-3940-3941-3950-3952-3953-3959",
    "2179-2830-3013-3017-3141-3160-3181-3193-3200-3204-3210-3216-3217-3226-3230-3234-3244-3245-3247-3251-3252-3253-3256-3257",
    "2222-2321-3192-3311-3481-3497-3580-3598-3604-3605-3612-3613-3614-3621-3629",
    "2223-2228-2235-2244-2252-2257-2263-2270-2271-2273-2275-2277-2278",
    "2327-2481-2484-2487-2488-2492-2502-2512-2513-2517-2631-2719-2785-2794-2796",
    "2397-3688-3718-3848-4238-4357-4359-4360-4369-4380-4386-4400-4404-4413-4457-4475",
    "2412-2892-2920-2947-2952-2954-2956-2957-2961-2964-2965-2970-2974",
    "2414-2458-2539-2545-2546-2558-2560-2561-2563-2564-2565-2567-2573-2574-2575-2576-2577-2578",
    "2426-3981-4063-4065-4066-4067-4069-4071-4074-4077-4089-4092-4093-4094-4105-4124",
    "2473-2480-2633-2674-2686-2708-2717-2720-2721-2723-2724-2727-2728-2729-2730-2731",
    "2536-2916-2996-3032-3034-3049-3133-3136-3138-3140-3146-3147-3159-3164-3171-3174-3175",
    "2582-2948-2979-2981-2989-2993-3007-3008-3009-3025-3036",
    "2634-2725-2764-2766-2798-2808-2835-2852-2856-2869-2872-2873-2876-2877-2884-2885",
    "2685-2760-2888-2893-2901-2902-2903-2904-2905-2906-2913-2914",
    "2726-3088-3209-3279-3285-3287-3289-3295-3299-3301-3302-3316-3317-3324-3332-3334-3337-3339-3344-3349-3350-3354-3355-3356-3357-3359-3360-3363-3364-3367",
    "2973-2975-2980-2985-2999-3023-3026-3035-3037-3038-3040-3042-3045-3052-3053-3056-3057-3060-3061-3062",
    "3005-3080-3098-3100-3102-3104-3115-3118-3119-3123-3127-3128-3131-3142-3150-3152-3154-3155",
    "3019-3028-3205-3243-3258-3260-3262-3272-3278-3281-3286-3294-3297-3300-3305-3308-3322-3323-3325-3329-3330",
    "3166-3169-3182-3184-3185-3189-3195-3199-3208-3219-3222",
    "3186-3673-4115-4141-4143-4147-4150-4153-4163-4188-4189-4194-4199-4200-4202-4205-4208-4216-4217",
    "3235-3834-3863-3876-3877-3878-3885",
    "3280-3399-3400-3417-3421-3424-3426-3427-3428-3442-3450",
    "3327-3369-3377-3379-3385-3395-3396-3397-3398-3401-3404-3407-3408-3409-3410-3411-3412-3414-3415",
    "3353-3599-3675-3679-3689-3693-3697-3702-3703-3716-3717-3725-3730-3732-3734-3735-3736-3740",
    "3365-3548-3560-3567-3572-3576-3582",
    "3439-3445-3487-3488-3490-3491-3492-3494-3495-3498",
    "3447-3857",
    "3619-3631-3632-3634-3637-3640-3644-3647-3664-3667-3671-3674",
    "3627-3831-4647-4654-4664-4682-4687-4689-4695-4696-4697-4698-4700-4701-4702-4703-4704-4706-4708-4709-4710-4711-4727-4730-4732-4733-4735-4738-4739-4743-4744",
    "3639-3804-3807-3810-3813-3814-3815-3824-3827-3830-3835-3846",
    "3728-3745-3746-3757-3758-3759-3762-3769-3771-3773-3779-3780-3782-3785-3797-3805",
    "3774-4109-4126-4130-4133-4139-4144-4145",
    "3775-4449-4526-4534-4535-4539-4542-4543-4544-4545-4546-4548-4549-4550-4551-4554-4556-4557-4558-4559-4565-4569-4570-4572-4576",
    "3784-4106-4166-4167-4172-4173-4182-4186-4187",
    "3889-4284-4308-4314-4318-4325-4326-4330-4336-4340-4342-4346-4354",
    "3906-3913-3955-3974-3982-3984-3985-3992-3993-4001-4021-4023-4025-4035-4036-4056",
    "3960-4757-4847-4885-4886-4888-4891-4897-4899-4908-4912-4918-4920-4924-4927-4929-4930-4931-4932-4933-4938",
    "4010-4468-4734-4751-4752-4753-4754-4756-4760-4761-4763-4764-4768-4770-4777-4778-4780-4782-4783",
    "4081-4112-4220-4223-4240-4252-4260-4265-4270-4274-4280",
    "4243-4269-4281-4287-4292-4294-4311-4316-4317",
    "4307-4470-4482-4503-4504-4506-4509-4513-4515-4516-4517-4518-4520-4521-4523-4524-4529-4530-4532",
    "4338-4895-4936-4971-4972-4989-4990-4997-5006-5007-5009-5019-5023-5025-5027-5028-5029-5031-5035-5038-5041-5042-5044-5046-5049-5051-5052",
    "4361-4879-5155-5227-5229-5282-5302-5316-5338-5343-5347-5381-5386-5387-5391-5399-5402-5403-5407-5408-5413-5418-5419-5421-5422-5425-5434-5435-5437-5439-5445-5449-5454-5458-5459-5461-5463-5464-5465-5467-5468-5472-5475-5476-5478-5479-5480-5482-5485-5487-5489-5494-5496-5497-5501-5503-5506-5507-5509-5512-5513-5522-5524-5526-5528-5533-5539-5540-5541-5542-5544-5545-5546-5548-5550-5551-5558-5561-5563-5567-5569-5570-5575-5576-5577-5578-5579-5583-5584-5589-5591-5593-5595-5596-5597-5600-5601-5602-5603-5604-5606-5607-5618-5619-5621-5624-5625-5626-5627-5629-5631-5633-5635-5636-5638-5639-5641-5642-5645-5646-5649-5651-5652-5653-5666-5668-5669-5670-5671-5677-5678-5679-5680-5682-5683-5687-5689-5692-5693-5694-5695-5697-5698-5700-5702-5704-5705-5706-5707-5711-5715-5719-5721-5722-5728-5730-5731-5732-5733-5734-5739-5745-5746-5747-5748-5750-5751",
    "4415-4775-4975-5040-5113-5200-5253-5255-5256-5257-5258-5260-5262-5268",
    "4488-4626-4649-4717-4762-4790-4824-4827-4832-4833-4834-4837-4840-4842-4843-4850-4853-4855-4856-4859-4862-4863-4865",
    "4495-4519-4525-4555-4574-4588-4614-4625-4644-4645-4648-4650-4652-4653-4656-4659-4661-4662-4665",
    "4547-4581-4594-4599-4602-4605-4607-4609-4615-4616-4618-4621-4623-4624-4627-4628-4629-4631-4633-4635-4636-4637-4639",
    "4597-4655-4657-4666-4673-4675-4676-4679-4680-4683-4684-4686",
    "4658-4771-4785-4907-4926-4950-4951-4954-4959-4960-4966-4969-4980-4981-4982-4986",
    "4765-4802-4804-4807-4811-4814-4821",
    "4836-4881-4906-4943-4947-4949-4953",
    "4880-5128-5134-5345-5352-5360-5361-5369-5373-5375-5376-5384-5389-5390-5394-5395-5398",
    "4904-4910-4991-5012-5068-5070-5080-5087-5088-5090",
    "4963-5084-5120-5168-5222-5234-5235-5237-5238-5243-5245",
    "4983-5119-5138-5152-5156-5158-5159-5174-5176-5178-5180-5181-5183-5193-5194-5199-5202-5203-5205-5206-5207-5209-5211-5212-5213-5218-5219-5225-5226",
    "4992-4998-5085-5092-5100-5104-5127-5129-5133-5140-5146",
]
for _ni in _BUNDLE_NIS_AGNO_E1:
    Instance.register("agno-agi", _ni)(AGNO_4999_TO_0)

