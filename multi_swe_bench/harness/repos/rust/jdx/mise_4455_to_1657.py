import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
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
        return "rust:1.82"

    def image_tag(self) -> str:
        return "base-mise_4455_to_1657"

    def workdir(self) -> str:
        return "base-mise_4455_to_1657"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Shared base for every old-era mise PR (built once, tag "base-mise_4455_to_1657").
        # The `# syntax` directive opts out of DockerfileEnhancer so this hand-written
        # layout is used verbatim: clone FULL history + light harden only. The strict
        # anti-reward-hack strip runs in the PR layer at each PR's literal base.sha.
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}
ENV LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates curl build-essential git \\
        libssl-dev pkg-config cmake \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
{fetch}

WORKDIR /home/{repo}
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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self.config)

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

cargo test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply /home/test.patch
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
cargo test

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

        # Strict anti-reward-hack hardening in the PR layer: prepare.sh checked out
        # this PR's base.sha; the canonical block then detaches at that literal sha
        # and strips every other ref/reflog so the fix commit is unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("jdx", "mise_4455_to_1657")
class MISE_4455_TO_1657(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped
        import re
        import json

        for line in log.splitlines():
            if line.startswith("test "):
                # ok
                match = re.search(r"test (.*) ... ok", line)
                if match:
                    passed_tests.add(match.group(1).strip())
                # ignored
                match = re.search(r"test (.*) ... ignored", line)
                if match:
                    skipped_tests.add(match.group(1).strip())
                # failed
                match = re.search(r"test (.*) ... FAILED", line)
                if match:
                    failed_tests.add(match.group(1).strip())
        if "failures:" in log:
            match = re.search(r"failures:\n([\s\S]*?)\n\n", log)
            if match:
                for test in match.group(1).splitlines():
                    # Make sure there are no blank lines
                    if test.strip() and not test.strip().startswith("----"):
                        failed_tests.add(test.strip())
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_MISE_4455_TO_1657 = [
    '1657-3193-3194-3195-3197-3199-3201-3202-3204-3205-3213-3214-3215',
    '1697-2994-3251-3252-3253-3254-3255-3257-3258-3259-3261-3264-3265-3266-3268-3269-3270-3271-3272',
    '1712-1763-1796-1801-1803-1804-1805',
    '1809-1813-1814-1815-1816-1817-1818-1819-1820-1821-1822-1824-1825-1826',
    '1827-1828-1829',
    '1877-1878',
    '2019-2040',
    '2295-3235-3236-3237-3238-3239-3240-3241-3242-3243-3244-3245-3246-3247-3248-3249',
    '2889-3286-3290-3292-3296-3297-3298-3299-3300',
    '3256-3273-3274-3275-3277-3278',
    '3302-3305-3307-3309-3316-3318-3334-3335-3336-3341-3349-3350-3352-3353',
    '3355-3357-3358-3359-3360-3363-3366-3367-3370-3371-3373-3374-3375-3378-3380-3381-3384',
    '3397-3400-3404-3406-3407-3409',
    '3439-3440-3441-3448-3449-3450-3452-3453-3454-3455',
    '3456-3459-3460-3461-3464-3465-3466-3467-3468-3469-3472-3473',
    '3474-3475-3476-3478-3479-3480-3481-3482-3483-3484-3485-3486-3488-3489-3490-3492-3495-3497',
    '3501-3510-3511-3514-3516-3519-3520-3521-3522-3526-3527-3528',
    '3506-3612-3613-3615-3616-3617-3618-3619-3620-3622-3623-3625-3626-3628-3633-3637-3639-3640-3647-3649-3650-3651',
    '3529-3530-3532-3533-3534-3535-3536-3556-3558-3561-3562-3563-3564-3565',
    '3566-3568-3570-3571-3572-3573-3575-3576-3577-3583-3584-3586-3587-3590-3591-3592-3593-3594-3595',
    '3596-3598-3599-3600-3601-3603-3604-3607-3608-3610-3611',
    '3655-3656-3659-3664-3666-3667-3670-3675-3676-3677-3679-3682-3683-3685',
    '3718-3719-3720-3723-3729-3732-3735-3737-3739-3740-3741-3742-3743-3744',
    '3753-3765-3769-3770-3771-3772-3786-3787-3788-3789-3791-3792',
    '3762-4029-4030-4034-4036-4037-4038-4045-4046-4047-4049',
    '3818-3836-3839-3845-3850',
    '3823-3894-3902-3903-3904-3906-3907-3910',
    '3851-3852-3853-3855-3862',
    '3857-4159-4233-4299-4312-4317-4318-4319-4321-4324-4326-4329-4330-4334-4340-4341-4342',
    '3864-3867-3873-3876-3880-3881-3884',
    '3913-3914-3918-3919-3923-3926-3927-3931-3937-3938-3939-3942-3943-3957',
    '3920-4684-4686-4691-4693',
    '3955-3962-3969-3978-3982-3987-3988-3991-3992-3993-3994-3995-3996-3997-3998-3999-4004',
    '4006-4007-4008-4010-4019-4024-4025-4026-4027',
    '4048-4251-4260-4261-4269-4270-4272-4277',
    '4050-4052-4055-4056-4058-4059-4061-4062',
    '4063-4065-4067-4072',
    '4073-4075-4087-4088-4100-4101-4104-4105-4106-4107-4108',
    '4130-4131-4133-4134-4147-4148-4149',
    '4144-4160-4208-4216-4219-4220-4223-4224-4225-4226-4227-4228',
    '4181-4424-4427-4429-4435-4443-4446-4448-4449-4451-4453-4459-4463',
    '4197-4198-4199-4200-4204',
    '4230-4231-4232-4235-4248-4249-4252-4253-4256',
    '4279-4280-4282-4283-4285-4287-4288-4289-4290',
    '4296-4409-4456-4457-4460-4464-4466-4476-4483-4493-4494-4497-4498-4501-4507-4512',
    '4328-4530-4542-4543-4544-4545-4546-4547-4548-4549-4550-4557',
    '4333-4338-4351-4354-4356-4357-4358-4363-4382-4387-4388-4390-4391-4392-4396-4401',
    '4349-4553-4605-4648-4658-4661-4663-4666-4668-4669-4670-4672-4674',
    '4350-4743-4745-4746-4747-4750-4751-4754-4759-4764',
    '4355-4414-4415-4418-4419-4421-4422',
    '4402-4410-4412-4413',
    '4452-4873-4903-4906-4907-4908-4909-4910-4911-4912',
    '4455-4513-4519-4524-4532-4534-4538',
]
for _ni in _BUNDLE_NIS_MISE_4455_TO_1657:
    Instance.register('jdx', _ni)(MISE_4455_TO_1657)
