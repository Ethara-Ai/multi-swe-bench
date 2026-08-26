import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Shared per-era base: OS + toolchain + a FULL clone of the repo (all
    history, NO checkout, NO hardening). Built ONCE and reused by every PR in
    this era. The leading `# syntax=` directive makes DockerfileEnhancer return
    this Dockerfile verbatim (image.py: `if SYNTAX_DIRECTIVE in raw: return raw`)
    so the enhancer does NOT inject the ${BASE_COMMIT} hardening pass here — the
    base has no BASE_COMMIT and must keep full history so any PR's base.sha stays
    reachable. Per-PR checkout + hardening live in ImageDefault.
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
        return "python:3.10-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-py310-poetry"

    def workdir(self) -> str:
        return "base-py310-poetry"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """# syntax=docker/dockerfile:1.6
FROM python:3.10-bookworm

ARG TARGETARCH
ARG REPO_URL="https://github.com/mealie-recipes/mealie.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="mealie-recipes/mealie" \\
      org.opencontainers.image.description="mealie-recipes/mealie Docker image" \\
      org.opencontainers.image.source="https://github.com/mealie-recipes/mealie" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential patch libsasl2-dev libldap2-dev libssl-dev ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip && pip install poetry

RUN git clone "${REPO_URL}" /home/mealie

WORKDIR /home/mealie
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

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
                """ls -la
###ACTION_DELIMITER###
apt-get update && apt-get install -y libsasl2-dev libldap2-dev libssl-dev
###ACTION_DELIMITER###
pip install poetry
###ACTION_DELIMITER###
poetry install || (poetry lock && poetry install)
###ACTION_DELIMITER###
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/test.patch ) || true
( poetry install || (poetry lock && poetry install) ) || true
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/test.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/test.patch ) || true
git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/fix.patch || git -C /home/{pr.repo} apply --whitespace=nowarn --3way --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' --exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' --exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc' /home/fix.patch || ( cd /home/{pr.repo} && patch -p1 --forward --fuzz=3 < /home/fix.patch ) || true
( poetry install || (poetry lock && poetry install) ) || true
poetry run pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # Two-stage: chain to the shared ImageBase *Image*. Because dependency()
        # returns an Image (not a str), DockerfileEnhancer returns this verbatim
        # and supplies neither ARG BASE_COMMIT nor the hardening pass — so we set
        # BASE_COMMIT and embed Image._HARDENING_BLOCK ourselves. The base holds
        # a full clone; here we check out THIS PR's base.sha, install deps against
        # it, then the hardening block prunes every other ref/commit (reward-hack
        # defense). `hardening` is inserted as a plain value so its ${...}/$(...)
        # tokens stay byte-identical; literal Dockerfile braces are doubled.
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        base_sha = self.pr.base.sha
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{base_sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{repo}
RUN git checkout {base_sha}
RUN poetry install || (poetry lock && poetry install)

{copy_commands}
{hardening}
CMD ["/bin/bash"]
"""


@Instance.register("mealie-recipes", "mealie_v1_poetry_py310")
class MEALIE_V1_POETRY_PY310(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest `-rA` short test summary lines:
        #   PASSED tests/unit_tests/test_config.py::test_name[a b]
        #   FAILED tests/unit_tests/test_config.py::test_name - AssertionError: ...
        #   ERROR  tests/unit_tests/test_x.py::test_y - ...
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(.+?)\s*$", re.MULTILINE
        )
        for status, name in summary_pattern.findall(log):
            if status in ("FAILED", "ERROR"):
                name = re.sub(r"\s+-\s.*$", "", name).strip()
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name.strip())
            # XFAIL / XPASS: expected-fail bookkeeping, not real pass/fail

        # Grouped skip summary: SKIPPED [6] tests/unit_tests/test_x.py:18: reason
        for m in re.finditer(
            r"^SKIPPED\s+\[\d+\]\s+(\S+?):(\d+):", log, re.MULTILINE
        ):
            skipped_tests.add(f"{m.group(1)}:{m.group(2)}")

        # Defensive fallback: verbose per-test lines `nodeid STATUS [ 12%]`
        verbose_pattern = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$",
            re.MULTILINE,
        )
        for name, status in verbose_pattern.findall(log):
            name = name.strip()
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

# Route bundled PRs by their dash-joined `prs_in_bundle` interval to this era.
# Instance.create() looks up f"{org}/{number_interval}", so every bundle whose
# base.sha matches this era (poetry era, pyproject python ^3.10 — python:3.10-bookworm)
# must be registered here. Era was derived from the repo state at each base.sha
# (packaging files), not from PR-number ranges — routing is NOT monotonic in PR
# number (e.g. bundle 5883 is uv-era while the higher 6128/6268 are poetry-era).
# 18 bundle(s); intervals come from the lht dataset's prs_in_bundle.
_NUMBER_INTERVALS = [
    "2717-3014-3019-3025-3026-3027-3030-3031-3032-3033-3034-3035-3036-3038-3039-3041-3043-3044-3045-3046-3047-3048-3052-3053",
    "2772-2810-2902-3056-3066-3103-3105-3106-3107-3110-3111-3115-3116-3120-3122-3123-3124-3130-3131-3132-3135-3137-3138",
    "2914-3060-3100-3102-3118-3133-3134-3136-3139-3140-3142-3143-3145-3146-3147-3148-3149-3150-3152-3153-3155-3156-3157-3160-3161-3162-3163-3164-3166-3167-3169-3170-3172-3173-3174-3175-3176-3178-3179-3182-3184-3187-3189-3193-3194-3195-3196-3197-3202-3206-3207-3208-3210-3211-3212-3213-3215-3216-3217-3220-3221-3222-3223-3226-3228-3230-3231-3232-3233-3234-3235-3236-3237-3238-3239-3240-3243-3245-3246-3247-3248-3249-3251-3255-3256-3257-3258",
    "3057-3058-3059-3062-3063-3064-3065-3069-3070-3071-3072-3075-3076-3078-3079-3084-3085-3086-3088-3089-3090-3094-3096-3097",
    "3104-3345-3395-3409-3411-3415-3419-3420-3421-3422-3424-3425-3427-3429-3431-3435-3437-3439-3441-3443-3444-3447-3451-3452-3456-3458-3462-3464-3465-3467-3468",
    "3204-3261-3279-3280-3281-3283-3284-3285-3286-3290-3299-3300-3303-3305-3306-3307-3311-3312-3313-3314-3316-3319-3321-3322-3323-3328-3332-3333-3339-3340-3341-3342-3346-3347-3351-3352-3353-3354-3355-3358-3361-3362-3366-3368-3369-3370-3373-3376-3377-3378-3379-3380-3381-3383-3386-3390-3394-3397-3400-3402-3405-3406-3407-3408",
    "3389-3453-3636-3637-3638-3640-3641-3642-3645-3646-3650-3651-3653-3655-3656-3657-3658-3659-3660-3661-3662-3663-3665-3666-3667-3668-3669-3670-3672-3673-3674-3675-3678-3680-3683-3684-3685-3687-3688-3689-3691-3693-3694-3695-3696",
    "3448-3457-3459-3471-3482-3484-3485-3487-3493-3494-3495-3496-3497-3498-3500-3501-3507-3510-3513-3514-3515-3516-3517-3518-3520-3521-3522-3523-3527-3530-3531-3532-3533-3534-3535-3538-3539-3540-3541-3542-3544-3548-3549-3550-3552-3553-3555-3560-3563-3564-3565-3568-3569-3570",
    "3571-3575-3576-3577-3579-3580-3581-3583-3585-3586-3587-3588-3589-3590-3592-3595-3596-3598-3603-3604-3605-3611-3614-3616-3618-3620-3621-3622-3623-3625-3629-3630-3631-3632-3633-3635",
    "3648-3690-3698-3699-3700-3701-3705-3706-3707-3709-3711-3712-3713-3715-3716-3720-3721-3722-3723-3725-3726-3728-3729-3730-3733-3734-3736-3737-3740-3741-3742-3744",
    "3732-3738-3747-3749-3750-3752-3753-3756-3758-3759-3760-3761-3762-3763-3764-3765-3767-3768-3769-3770-3771-3774-3775-3776-3777-3778-3780-3782-3784-3785-3786-3789-3791-3795-3796-3797-3798-3800-3801-3802-3804-3806-3807-3808-3810-3812-3813-3814-3817-3820-3821-3822-3823-3824-3826-3827-3828-3829-3831-3832",
    "3799-3933-3958-3965-3967-3969-3971-3973-3974-3975-3976-3977-3978-3979-3980-3981-3985-3987-3988-3989-3990-3993-3994-3995-3996-3997-4001-4002-4004-4005-4007-4008-4009-4011-4012-4015-4016-4019-4020-4023-4025-4030-4034-4039-4041-4042-4043-4047-4049-4052-4053-4054-4056-4057-4058-4060",
    "3818-3825-3837-3840-3843-3844-3847-3851-3854-3855-3856-3857-3860-3862-3864-3866-3869-3872-3873-3875-3877-3878-3882-3883-3884-3886-3887-3888-3889-3890-3891-3893-3894-3895-3896-3897-3901-3905-3906-3908-3909-3910-3911-3912-3913-3914-3915-3916-3917-3919-3921-3922-3923-3925-3927-3928-3929-3930-3932-3938-3939-3940-3942-3943-3944-3949-3950-3954-3955-3957-3959-3960-3961-3963",
    "3970-4064-4065-4068-4072-4074-4075-4076-4078-4085-4087-4088-4089-4090-4092-4093-4095-4096-4098-4101-4102-4103-4104-4105-4111-4112-4113-4115-4116-4117-4120-4121-4122-4124-4127-4130-4131-4132-4133-4138-4141-4142-4143-4145-4147-4149-4150-4153-4154-4156-4158-4159-4160-4161-4162-4163-4164-4165-4167-4168-4169-4170-4171-4174-4176-4179-4180-4181-4183-4185-4186-4188-4189-4190-4191-4192-4194-4195-4196-4199-4200-4201-4203-4204-4205-4206-4207-4213-4215-4218-4220-4221-4225-4226-4227-4228-4229-4230-4231-4233-4234-4235-4236-4237-4238-4240-4241-4243-4245-4247-4248-4249-4253-4254-4255-4256-4257-4258-4259-4261-4264-4265-4266-4267-4268-4269-4270-4271-4272-4273-4274-4277-4278-4280-4281-4283-4284-4285-4287-4289-4292-4293-4298-4299-4300-4301-4302-4303-4305-4306-4308-4309-4310-4314-4315-4316-4317-4318-4319-4321-4324-4325-4326-4328-4330-4331-4332-4333-4337-4338-4339-4341-4342-4343-4344-4345-4346-4347-4351-4352-4355-4356-4359-4360-4362-4364-4365-4366-4367-4369-4370-4371-4375-4376-4379-4381-4382-4383-4384-4385-4387-4388-4389-4390-4391-4393-4394-4395-4397-4398-4402-4403-4405-4406",
    "4252-4464-4489-4517-4534-4535-4536-4538-4539-4544-4546-4548-4549-4550-4552-4554-4555-4556-4557-4562-4567-4568-4572-4580-4583-4585-4586-4587-4590-4602-4603-4605-4606-4607-4609-4614",
    "4329-4542-4588-4601-4615-4617-4618-4620-4621-4622-4623-4624-4625-4631-4638-4639-4652-4653-4654-4655-4656-4657-4661-4662-4666-4672-4673-4674-4675-4677-4679-4680-4683-4685-4688-4690-4696-4697-4700-4703-4704-4705-4712-4713-4716-4718-4721",
    "4378-4408-4409-4410-4412-4413-4414-4416-4417-4418-4419-4422-4428-4429-4431-4432-4434-4435-4436-4437-4438-4439-4440-4441-4442-4444-4446-4447-4450-4451-4453-4460-4461-4466-4468",
    "4400-4421-4452-4456-4459-4469-4470-4471-4475-4479-4481-4486-4487-4488-4491-4493-4495-4500-4504-4506-4510-4512-4513-4515-4516-4518-4519-4520-4522-4524-4530",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("mealie-recipes", _interval)(MEALIE_V1_POETRY_PY310)
