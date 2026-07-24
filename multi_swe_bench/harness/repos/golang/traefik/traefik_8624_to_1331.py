import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Drop binary file sections from a unified diff.

    ``git apply`` is atomic: a single binary hunk lacking a full index line
    (``Binary files a/x and b/x differ`` or a ``GIT binary patch`` block)
    aborts the WHOLE apply, so with ``set -e`` in *-run.sh the fix stage
    yields zero results and the record is misclassified invalid. Splitting on
    the ``diff --git`` boundary and dropping only the binary sections lets the
    text hunks (the Go test/source changes that carry the f2p/n2p signal)
    apply cleanly.
    """
    if not patch:
        return patch
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    kept = [
        s for s in sections
        if "Binary files " not in s and "GIT binary patch" not in s
    ]
    return "".join(kept)


class Traefik8624To1331ImageBase(Image):
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
        return "golang:1.13"

    def image_tag(self) -> str:
        return "base-era-a"

    def workdir(self) -> str:
        return "base-era-a"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` opts this shared era base out of the DockerfileEnhancer,
        # which would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + prune HERE, pruning the shared base to a single PR's
        # base.sha and breaking every other PR in the era with "reference is not
        # a tree". The base keeps full history; the anti-reward-hack hardening
        # runs per-PR at the literal base.sha (see Traefik8624To1331ImageDefault).
        return f'''# syntax=docker/dockerfile:1.6
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
RUN git config --global --add safe.directory '*'
{code}

RUN sed -i 's/deb.debian.org/archive.debian.org/g; s/security.debian.org/archive.debian.org/g; /buster-updates/d' /etc/apt/sources.list || true
ENV GO111MODULE=off
ENV GOPATH=/go
RUN mkdir -p /go/src/github.com/containous && \
    ln -sf /home/traefik /go/src/github.com/containous/traefik
RUN go get -u github.com/jteeuwen/go-bindata/... || true

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
'''


class Traefik8624To1331ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return Traefik8624To1331ImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set +e

mkdir -p /go/src/github.com/containous
[ -e /go/src/github.com/containous/{pr.repo} ] || ln -sf /home/{pr.repo} /go/src/github.com/containous/{pr.repo}
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
cd /go/src/github.com/containous/{pr.repo}
go generate ./... 2>&1 || true
go build ./... 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /go/src/github.com/containous
[ -e /go/src/github.com/containous/{pr.repo} ] || ln -sf /home/{pr.repo} /go/src/github.com/containous/{pr.repo}
cd /go/src/github.com/containous/{pr.repo}
go test -v -count=1 -vet=off ./...
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /go/src/github.com/containous
[ -e /go/src/github.com/containous/{pr.repo} ] || ln -sf /home/{pr.repo} /go/src/github.com/containous/{pr.repo}
cd /go/src/github.com/containous/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 -vet=off ./...
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /go/src/github.com/containous
[ -e /go/src/github.com/containous/{pr.repo} ] || ln -sf /home/{pr.repo} /go/src/github.com/containous/{pr.repo}
cd /go/src/github.com/containous/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 -vet=off ./...
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

        # Per-PR anti-cheat hardening at the LITERAL base.sha (the shared base
        # keeps full history so every PR's base.sha is reachable). prepare.sh
        # checks out this PR's base.sha; the hardening block then detaches at
        # that literal sha and strips every other ref/reflog so the fix commit
        # is unreachable from git.
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


@Instance.register("traefik", "traefik_8624_to_1331")
class Traefik8624To1331(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Traefik8624To1331ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return (
            "bash -c '" + """cd /go/src/github.com/containous/traefik && rm -rf integration && go test -v -count=1 -vet=off ./...""" + "'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '" + """cd /go/src/github.com/containous/traefik && git apply --whitespace=nowarn /home/test.patch && rm -rf integration && go test -v -count=1 -vet=off ./...""" + "'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '" + """cd /go/src/github.com/containous/traefik && git apply --whitespace=nowarn /home/test.patch /home/fix.patch && rm -rf integration && go test -v -count=1 -vet=off ./...""" + "'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

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

        # Go subtests share a base name (get_base_name strips "/sub"); if any
        # subtest failed, the base must not also remain in passed/skipped, or the
        # harness rejects the report ("passed and failed should not overlap").
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


# --- §11b bundle keys: every dash-joined prs_in_bundle for this era
# --- routes to Traefik8624To1331. 51 bundles.
_BUNDLE_NIS_A = [
    "1331-1347-1349",
    "1401-1417-1420",
    "1695-1700-1714-1749-1757-1760",
    "1772-1782-1791-1796-1798-1800-1805-1806",
    "1859-1863-1896-1899",
    "1905-1907",
    "2079-2085",
    "2270-2292-2295-2296-2301-2302-2309-2310-2313",
    "2354-2363-2372-2374-2380-2385-2389-2391-2400-2403-2404",
    "2382-2405-2420-2423-2429-2434-2436-2456",
    "2461-2477-2478-2483-2528",
    "2748-2750-2751-2756-2757-2766-2767-2768-2777-2778",
    "3252-3261-3267-3279-3290-3291-3294-3299-3314-3317-3323",
    "3287-3368-3389-3390-3392-3394-3398-3405-3411-3412-3418-3421-3425-3430-3431-3432-3434-3437",
    "3438-3450-3454-3469-3474-3477-3479-3484-3485-3488-3498-3501",
    "3923-3934-3946-3948-3952",
    "3938-3942-3954-3956-3958-3962-3963-3964-3966-3971-3976-3979",
    "4272-4294",
    "4378-4438-4441-4442-4458-4459-4468-4471-4478-4479-4480-4482",
    "5194-5201-5204",
    "5362-5399-5404-5412-5459-5469",
    "5555-5574-5662-5663-5682-5739",
    "6531-6537-6553-6556",
    "7090-7108-7114",
    "7155-7174-7301-7315-7325-7328-7667-7718-7751-7753-7755",
    "8482-8486-8489",
    "8624-8626",
    "1895-1944-1971-2020-2030-2033-2034-2050-2056-2071-2074-2077-2084-2092-2098-2117-2127-2133-2140-2154-2161-2176-2202-2220-2233-2242-2243-2265-2271-2274-2276-2283-2287-2289-2291-2303-2304-2306-2312-2318-2320-2324-2325-2334-2335-2344-2347-2356-2358-2378-2379-2383-2386-2387-2388-2406-2407-2411-2413-2414-2415-2419-2421-2424-2425-2428-2432-2440-2445-2446-2447-2449-2451-2452-2455-2457-2460-2463-2464-2465-2469-2470-2471-2473-2476-2479-2480-2485-2486-2487-2488-2490-2491-2492-2496-2498-2499-2501-2505-2506-2509-2520-2523-2529-2530-2532-2533-2538-2544-2558-2559-2560-2562-2564-2570-2573-2574-2577-2580-2585-2588-2589-2590-2591-2592-2594-2598-2599-2603-2606-2609-2611-2621-2624-2626-2627-2634-2635-2642-2644-2650-2651-2654-2656-2657-2669-2675-2676-2677-2679-2681-2685-2686-2689-2692-2695-2701-2702-2706-2707-2711-2719-2725-2727-2731-2737-2738-2739-2741-2742-2744-2745-2747",
    "2111-2217-2222-2226-2398-2439-2443-2482-2513-2514-2516-2518-2519-2522-2534-2536-2553-2567-2575-2583-2584-2587-2600-2601-2602-2604-2605-2607-2608-2610-2612-2614-2616-2622-2639-2643-2646-2652-2655-2659-2661-2662-2663-2665-2668-2680-2682-2687-2697-2708-2713-2717-2720-2726-2733-2740-2743-2754-2758-2759-2772-2774-2781-2790-2799-2801-2803-2807-2817-2823-2827-2839-2843-2844-2845-2846-2847-2848-2850-2860-2865-2867-2872-2880-2882-2883-2886-2889-2890-2898-2900-2906-2907-2908-2911-2914-2915-2933-2943-2946-2951-2958-2961-2970-2971-2972-2973-2982-2987-2988-2991-2992-3006-3007-3009-3024-3028-3033-3042-3050-3055-3061-3062-3063-3064-3065-3067-3070-3073-3078-3079-3080-3082-3084-3086-3087-3091-3093-3096-3099-3101-3102-3106-3108-3109-3110-3116-3120-3126-3127-3130-3132-3136-3137-3138-3141-3143-3150-3154-3156-3158-3159-3167-3171-3175-3179-3183-3184-3185-3187-3188-3189-3190-3191-3192-3195-3197-3199-3201-3207-3211-3213-3215-3217-3219-3221-3223-3227-3241-3242-3243-3245-3250-3251",
    "2308-2314-2317-2330-2331-2333-2337-2338-2340-2343-2345-2350-2352-2353",
    "2631-2638-2640-2641",
    "2779-2780-2787-2794-2795-2798-2800-2802-2811-2813-2814-2818-2821-2822-2824-2825-2834-2838-2841",
    "2852-2862-2863-2870-2871-2878-2887-2894-2901-2904-2909-2913-2921-2929-2934-2935-2938-2941",
    "2948-2950-2955-2959-2960-2962-2975-2977-2980-2981-2983-2984-2990-2996-3000-3004-3012-3013-3015-3016-3022",
    "3047-3100-3107-3112-3121-3129-3152-3180-3202-3203-3204-3205-3209-3225-3231-3234-3238-3246-3253-3276-3278-3285-3286-3312-3315-3319-3324-3326-3327-3340-3342-3350-3352-3362-3364-3367-3371-3373-3379-3383-3386-3387-3391-3393-3403-3404-3424-3439-3441-3444-3452-3460-3461-3463-3470-3471-3480-3481-3491-3492-3499-3502-3505-3510-3512-3516-3517-3521-3523-3533-3534-3537-3540-3541-3542-3547-3553-3554-3556-3559-3563-3564-3571-3577-3578-3580-3582-3583-3584-3592-3593-3595-3601-3604-3605-3606-3607-3610-3611-3615-3618-3619-3628-3629-3631-3632-3636-3638-3639-3648-3655-3659-3664-3665-3674-3675-3679-3682-3689-3690-3699-3700-3701-3705-3706-3708-3709-3711-3717-3719-3720-3724-3726-3727-3729-3730-3733-3742-3743-3747-3753-3756-3779-3795-3796-3797-3798-3799-3800-3802-3804-3805-3807-3811-3815-3816-3817-3824-3825-3826-3835-3844-3848-3850-3851-3856-3863-3864-3878-3880-3885-3888-3889-3893-3894-3895-3898-3900-3902-3907-3908-3913-3915-3920",
    "3274-3329-3331-3332-3333-3335-3337-3339-3344-3346-3347-3358-3361-3363-3366",
    "3506-3511-3513-3532-3546-3558-3560-3567-3579-3587",
    "3608-3609-3616-3736-3740-3751-3777-3788-3790-3793-3794",
    "3862-3891-3896-3973-3988-3991-3995-3996-3998-4009-4015-4018-4021-4028-4031-4033-4042-4045",
    "3931-4050-4053-4061-4062-4063-4065-4075-4083-4086-4090-4091-4093-4094-4095-4096-4097-4101-4106-4111-4112-4113-4118-4122-4123-4124-4130-4133-4136",
    "4022-4177-4359-4360-4367-4370-4374-4376-4384-4390-4393-4394-4395-4397-4398-4420-4428-4436",
    "4036-4105-4132-4135-4138-4156-4159-4166-4169-4170-4171-4175-4185-4188-4189-4194-4201-4208-4212-4225-4230-4244-4255-4258-4263-4264",
    "4116-4277-4289-4298-4299-4302-4307-4310-4317-4320-4326-4327-4340-4347-4351-4355-4358-4361",
    "4313-4486-4499-4508-4516-4538-4557-4564-4577-4605-4606-4610-4624-4627-4643-4670-4679-4683-4684-4686",
    "4515-4682-4690-4696-4697-4720-4722-4743-4747-4765-4800-4819-4822",
    "4537-4909-4927-4929-4953-4954-4963-4999-5002-5014-5021-5051-5067-5085-5095-5109-5131-5150-5160-5166-5186",
    "4716-5230-5235-5238-5269-5285-5319-5320-5353-5356-5367",
    "4751-4821-4877-4878-4890-4900-4901-4910-4918-4919",
    "5873-6005-6071-6162-6240-6273-6277-6337-6346",
    "6357-6375-6386-6394-6446-6456-6457-6513",
    "6552-6571-6631-6901-7054",
]
for _ni in _BUNDLE_NIS_A:
    Instance.register("traefik", _ni)(Traefik8624To1331)

