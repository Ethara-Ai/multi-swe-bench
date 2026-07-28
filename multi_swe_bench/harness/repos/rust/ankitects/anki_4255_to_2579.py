import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "rust:1.85"

    def image_prefix(self) -> str:
        return "mswebench"

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

apt-get update && apt-get install -y protobuf-compiler

cd /home/anki
git reset --hard
git checkout {pr.base.sha}
git submodule update --init --recursive

export PROTOC=/usr/local/bin/protoc
cargo test --workspace || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/anki
export PROTOC=/usr/local/bin/protoc
cargo test --workspace

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/anki
if ! git -C /home/anki apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PROTOC=/usr/local/bin/protoc
cargo test --workspace

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/anki
if ! git -C /home/anki apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export PROTOC=/usr/local/bin/protoc
cargo test --workspace

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM rust:1.85

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git protobuf-compiler
# apt protobuf-compiler (3.12) is too old for anki's proto3 `optional` fields
# (rslib/build/protobuf.rs panics). Install a modern protoc (25.x), arch-aware
# so multiarch amd64+arm64 both work; the run scripts point PROTOC at it.
RUN set -e; PV=25.1; case "$(uname -m)" in x86_64) PA=x86_64;; aarch64) PA=aarch_64;; *) PA=x86_64;; esac; \
    apt-get install -y unzip curl >/dev/null 2>&1 || true; \
    curl -fsSL -o /tmp/protoc.zip https://github.com/protocolbuffers/protobuf/releases/download/v${{PV}}/protoc-${{PV}}-linux-${{PA}}.zip; \
    unzip -oq /tmp/protoc.zip -d /usr/local; chmod +x /usr/local/bin/protoc; rm -f /tmp/protoc.zip

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/ankitects/anki.git /home/anki

WORKDIR /home/anki
RUN git reset --hard
RUN git checkout {pr.base.sha}
RUN git submodule update --init --recursive
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("ankitects", "anki_4255_to_2579")
class ANKI_4255_TO_2579(Instance):
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
        # Parse cargo test output.
        # Example output lines:
        #   test cloze::test::cloze_only ... ok
        #   test some::module::test_name ... FAILED
        #   test some::module::test_name ... ignored
        #   test result: ok. 306 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in test_log.splitlines():
            if line.startswith("test "):
                match = re.search(r"test (.*) \.\.\. ok", line)
                if match:
                    passed_tests.add(match.group(1).strip())
                match = re.search(r"test (.*) \.\.\. ignored", line)
                if match:
                    skipped_tests.add(match.group(1).strip())
                match = re.search(r"test (.*) \.\.\. FAILED", line)
                if match:
                    failed_tests.add(match.group(1).strip())
        if "failures:" in test_log:
            match = re.search(r"failures:\n([\s\S]*?)\n\n", test_log)
            if match:
                for test in match.group(1).splitlines():
                    if test.strip() and not test.strip().startswith("----"):
                        failed_tests.add(test.strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# PIPELINE.md §11b: every dash-joined bundle value must be a registered key
# in addition to the era key, else Instance.create() raises "not registered".
_BUNDLE_NIS_ANKI_4255_TO_2579 = [
    "2579-3222-3223-3224-3225-3226-3228-3230-3232-3241-3255-3257-3264-3272-3273-3275-3277-3281-3283-3284-3285-3286-3288-3290-3292-3293-3294-3296-3298-3299-3304-3306-3307-3308-3311-3312-3313-3314-3315-3320-3322-3323-3324-3325-3326-3327-3328-3329-3330-3331-3345-3346-3347-3351-3356-3360-3361-3362-3363-3364-3365-3366-3367-3368-3369-3372-3375-3376-3377-3378-3380-3381-3382-3385-3386-3387-3398-3399-3400-3403-3404-3405-3406-3407-3408-3409-3410-3411-3412-3413-3415-3418-3419-3420-3421-3422-3425-3431-3432-3433-3434-3437-3441-3442-3443-3447-3448-3450-3454-3457-3459-3464-3465-3467-3468-3471-3474-3475-3478-3482-3483-3485-3486-3488-3489-3490-3495-3496-3500-3501-3502-3503-3504-3505-3507-3508-3510-3511-3513-3516-3518-3519-3520-3522-3523-3524-3525-3526-3527-3530-3533-3534-3535-3536-3537-3538-3539-3540-3541-3542-3544",
    "2598-2612-2614-2615-2618-2623-2626-2630-2632-2633-2634-2636-2639-2640-2643-2646-2647-2649-2653-2654-2655-2657-2658-2659-2660-2663-2669-2670-2671-2673-2675-2682-2683-2684-2685-2686-2687-2688-2689-2703-2704-2705-2707-2709-2710-2711-2712-2716-2718-2720-2721-2722-2723-2726-2727-2728-2729-2730-2732-2734-2735-2739-2740-2741-2742-2744-2746-2747-2748-2750-2751-2756-2757-2758-2760-2761-2762-2763-2764-2766-2767-2773-2775-2782-2784-2785-2788-2790-2792-2805-2806-2815-2816-2817",
    "2765-2787-2804-2809-2810-2811-2820-2824-2825-2827-2828-2829-2832-2833-2835-2836-2840-2841-2845-2847-2849-2850-2851",
    "2854-2855-2856-2857-2859-2860-2861-2862-2863-2864-2865-2866-2869-2870-2873-2874-2878-2879-2884-2885-2886-2887-2888-2889-2894-2896-2898-2899-2900-2901-2903-2907-2909-2910-2911-2912-2916-2918-2919-2920-2922-2924-2928-2929-2930-2931-2933-2935-2936-2940-2941-2943-2945-2947-2949-2950-2953-2957-2958-2960-2963-2966-2967-2969-2971-2977-2981-2984-2985-2987-2988-2989-2992-2993-2994-2995-2996-2997-3002-3003-3006-3007-3008-3009-3010-3012-3013-3014-3018-3019-3021-3024-3027-3029-3030-3031-3036-3038-3040-3045-3049-3050-3051-3052-3053-3054-3055-3056-3058-3059-3060-3061-3065-3066-3067-3071-3072-3075-3080-3082-3083-3087-3088-3089-3092-3093-3097",
    "2986-3135-3136-3141-3142-3143-3144-3148-3150-3153-3155-3160-3162-3165-3166-3167-3170-3171-3174-3181-3182-3184-3186-3194-3195-3196-3197-3198-3199-3200-3202-3203-3206-3208-3209-3210-3213-3218-3219-3221",
    "3231-3233-3236-3237-3243-3245-3246-3253-3256-3258-3259-3265",
    "3379-3553-3554-3555-3556-3557-3558-3559-3560-3562-3564-3565-3566-3567-3568-3569-3570-3573-3574-3576-3577-3578-3579-3582-3587-3590-3591-3594-3598",
    "3571-3602-3603-3604-3605-3606-3609-3610-3611-3612-3613-3618-3620-3621-3622-3623-3627-3628-3629-3630-3631-3633-3639-3640-3641-3642-3643-3644-3645-3646-3648-3651-3653-3655-3658-3660-3661-3662-3665-3666-3667-3668-3670-3671-3672-3673-3674-3675-3676-3677-3678-3679-3681-3685-3686-3687-3689-3690-3691-3692-3693-3705-3706-3707-3709-3710-3711-3714-3716-3717-3718-3719-3721-3722-3723-3724-3725-3727-3728-3729-3730-3732-3733-3735-3736-3737-3738-3742-3743-3744-3745-3747-3748-3752-3754-3756-3759-3760-3763-3768-3771-3772",
    "3572-3795-3798-3801-3804-3805-3806-3811-3814-3815-3817-3820-3821-3822-3823-3825-3826-3827-3828-3829-3831-3832-3834-3837-3838-3839-3840-3844-3846-3847-3849-3852-3855-3856-3857-3858-3859-3860-3862-3863-3864-3865-3866-3867-3869-3870-3872-3873-3874-3877-3878-3879-3880-3882-3886-3887-3888-3890-3891-3894-3900-3901-3902-3903-3904-3905-3910-3912-3913-3914-3915-3916-3917-3919-3920-3922-3923-3925-3927-3928-3929-3933-3938-3939-3940-3941-3942-3943-3944-3945-3946-3947-3948-3949-3951-3953-3954-3956",
    "3952-3957-3958-3959-3960-3962-3963-3964-3966-3969-3970-3971-3972-3973-3975-3976-3977-3979-3980-3981-3982-3985-3986-3987-3989-3990-3991-3992-3993-3994-3995-3996-3997-3998-3999-4001-4003-4004-4005-4008-4009-4011-4012-4016-4018-4019-4021-4024-4025-4026-4027-4030-4034-4035-4038-4039-4040-4041-4042-4043-4046-4047-4048-4049-4050-4052-4054-4055-4056-4057-4059-4060-4063-4066-4067-4068-4069-4074-4075-4076-4077-4078-4080-4083-4084-4086-4095-4096-4101-4102-4103-4105-4106-4108-4111-4113-4114-4115-4116-4117-4119-4120-4122-4123-4125-4127-4128-4131-4132-4133-4136",
    "4255-4258-4259-4260-4262-4264-4265-4266-4267-4269-4274-4275-4280-4281-4282-4283-4284-4286-4290-4291-4292-4293-4296-4297-4298-4310-4312-4316-4317",
]
for _ni in _BUNDLE_NIS_ANKI_4255_TO_2579:
    Instance.register("ankitects", _ni)(ANKI_4255_TO_2579)
