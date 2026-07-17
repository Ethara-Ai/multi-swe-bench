from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class McImageBase(Image):
    """Shared base image (built once, reused by every modules-era mc PR).

    Clone-only with full history kept so any PR's base.sha is reachable; the PR
    layer does the checkout + strict history-strip. The `# syntax` directive opts
    out of DockerfileEnhancer so this hand-written layout is used verbatim.
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
        return "golang:1.24-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

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
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GOTOOLCHAIN=auto

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git gcc ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class McImageDefault(Image):
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
        return McImageBase(self.pr, self.config)

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
export GOPROXY=https://proxy.golang.org,direct
export GONOSUMCHECK=*
export GONOSUMDB=*
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

CGO_ENABLED=0 go test -v -vet=off -tags kqueue -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export GOPROXY=https://proxy.golang.org,direct
export GONOSUMCHECK=*
export GONOSUMDB=*
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\.$')
# Filter to only existing directories
EXISTING_PKGS=""
for pkg in $PKGS; do
  dir="${{pkg#./}}"
  if [ -d "$dir" ]; then
    EXISTING_PKGS="$EXISTING_PKGS $pkg"
  fi
done
PKGS="${{EXISTING_PKGS## }}"
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
for pkg in $PKGS; do
  CGO_ENABLED=0 go test -v -vet=off -tags kqueue -count=1 -timeout 15m "$pkg" || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export GOPROXY=https://proxy.golang.org,direct
export GONOSUMCHECK=*
export GONOSUMDB=*
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go mod tidy 2>/dev/null || true
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\.$')
# Filter to only existing directories
EXISTING_PKGS=""
for pkg in $PKGS; do
  dir="${{pkg#./}}"
  if [ -d "$dir" ]; then
    EXISTING_PKGS="$EXISTING_PKGS $pkg"
  fi
done
PKGS="${{EXISTING_PKGS## }}"
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
for pkg in $PKGS; do
  CGO_ENABLED=0 go test -v -vet=off -tags kqueue -count=1 -timeout 15m "$pkg" || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export GOPROXY=https://proxy.golang.org,direct
export GONOSUMCHECK=*
export GONOSUMDB=*
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go mod tidy 2>/dev/null || true
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\.$')
# Filter to only existing directories
EXISTING_PKGS=""
for pkg in $PKGS; do
  dir="${{pkg#./}}"
  if [ -d "$dir" ]; then
    EXISTING_PKGS="$EXISTING_PKGS $pkg"
  fi
done
PKGS="${{EXISTING_PKGS## }}"
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
for pkg in $PKGS; do
  CGO_ENABLED=0 go test -v -vet=off -tags kqueue -count=1 -timeout 15m "$pkg" || true
done

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

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history). prepare.sh checks out this PR's base.sha, then the canonical
        # hardening block detaches at that literal sha and strips every other
        # ref/reflog so later commits (the fix) are unreachable.
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


@Instance.register("minio", "mc")
class Mc(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return McImageDefault(self.pr, self._config)

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
# Delivery scope = RESOLVED (valid) bundles only; keys == #delivered instances (PIPELINE §11c).
# Modules-era bundles (go.mod present at base.sha).
_BUNDLE_NIS_MC = [
    "2723-2727-2730-2732",
    "2755-2757",
    "2774-2840",
    "2777-2782",
    "2794-2851",
    "3031-3032-3033-3034-3035-3040-3041-3042",
    "3043-3161-3162-3174-3183-3188-3189",
    "3083-3095-3100-3111-3114-3115-3117-3118-3119-3121-3125-3126-3132-3133-3135-3137",
    "3097-3159-3182-3195-3202-3204-3213-3217-3218-3220",
    "3138-3139-3141-3142",
    "3156-3163-3168-3169-3171-3172",
    "3272-3304-3306-3308-3309-3310-3311-3312-3314-3315-3320-3321-3326-3327-3329-3330-3334",
    "3318-3418-3422-3424-3427-3429-3433-3434",
    "3343-3347-3394-3397-3401-3402-3404-3406-3408-3410",
    "3395-3430-3439-3440-3443-3444-3445-3446-3448-3449-3454-3455-3457-3458-3460-3462-3463-3464-3465-3468-3469-3470-3471-3476-3481-3483-3487-3488-3490-3491-3493",
    "3461-3492-3510-3511-3512-3513-3514-3515-3517-3518-3522-3525-3526",
    "3618-3642-3648-3650-3661-3662-3663-3664-3670-3676-3680-3681-3683-3684-3688-3695",
    "3629-3754-3759-3760-3768-3771-3772-3778-3779-3780-3782",
    "3666-3766-3777-3783-3788-3792-3795-3797-3798-3799-3800-3802-3804",
    "3732-3808-3831-3832-3835-3837-3838-3839-3841-3851-3855-3857-3858-3859",
    "3769-3917-3920-3921-3927-3931-3932-3935",
    "3908-3914-3915-3929-3934-3941-3942-3943",
    "3936-3940-3945-3946-3947",
    "3964-3966-3969-3971-3972-3977-3981-3982-3983",
    "3993-3996-4000-4003-4004-4006-4007",
    "4109-4156-4161-4164-4169-4170-4171-4173",
    "4160-4163-4187-4189-4190-4191-4193-4194-4195-4198-4203-4204-4205",
    "4185-4186-4310-4333-4336-4337-4338-4339-4341-4344-4346-4347-4348-4350-4351",
    "4218-4226-4227-4228-4229-4232-4233-4234-4235-4236-4237-4238-4239-4240-4241-4243-4244-4246-4247-4248-4249-4250-4255",
    "4245-4254-4257-4259-4261-4263-4264-4266-4268-4270-4271-4275-4276-4277",
    "4262-4318-4357-4358-4360-4361-4368-4369-4370-4371-4373",
    "4269-4366-4372-4375-4376-4378-4381-4382-4384",
    "4428-4474-4479-4488-4490-4494-4495-4497-4499-4503-4504-4505-4506-4508-4510",
    "4473-4514-4531-4532-4533-4534-4536-4538-4542-4543-4544-4545",
    "4498-4507-4526-4535",
    "4608-4613-4623-4624",
    "4645-4653-4654-4659-4661-4662",
    "4698-4745-4770-4771-4774-4777-4779",
    "4750-4754-4755-4757-4758-4759-4763",
    "4760-4795",
    "4825-4834-4835-4838",
    "4859-4861-4864-4866",
    "4867-4877-4885-4888",
    "4882-4890-4892-4894-4898-4899-4904-4905",
    "4920-4921-4922-4924-4926-4927-4928-4929",
    "4951-4962-4963-4964-4965",
    "5060-5062-5064-5065-5066-5068",
    "5158-5160-5166-5167-5169-5173-5175-5176-5177-5178",
]
for _ni in _BUNDLE_NIS_MC:
    Instance.register("minio", _ni)(Mc)


# === remaining bundle keys (full raw dataset coverage, §11b) ===
# The delivered/resolved subset is registered above; these are the rest of
# the 102-record raw dataset so every bundle number_interval routes.
_BUNDLE_NIS_MC_UNRESOLVED = [
    "2592-2662",  # pr-2592 (2 PRs)
    "2614-2683-2697-2699-2701-2706",  # pr-2614 (6 PRs)
    "2671-2687-2700-2705-2709-2717",  # pr-2671 (6 PRs)
    "2672-2745-2748",  # pr-2672 (3 PRs)
    "2692-2712-2716-2718-2720",  # pr-2692 (5 PRs)
    "2703-2708",  # pr-2703 (2 PRs)
    "2768-2841-2847-2848",  # pr-2768 (4 PRs)
    "2784-2788-2790",  # pr-2784 (3 PRs)
    "2817-2819",  # pr-2817 (2 PRs)
    "2890-2892-2900-2901",  # pr-2890 (4 PRs)
    "3021-3026-3030",  # pr-3021 (3 PRs)
    "3243-3244-3246-3247-3249-3250-3251-3257",  # pr-3243 (8 PRs)
    "3287-3392-3407-3411-3412-3415",  # pr-3287 (6 PRs)
    "3313-3350-3354-3364-3365-3367-3373-3378-3380-3381-3382-3383-3384-3385-3386-3388-3390-3391-3393",  # pr-3313 (19 PRs)
    "3339-3342-3349-3351-3352-3355-3356-3358-3359-3360-3361-3366-3369-3371-3372",  # pr-3339 (15 PRs)
    "3417-3531-3540-3545-3547-3552-3554-3556-3557",  # pr-3417 (9 PRs)
    "3685-3692-3708-3709-3710-3714-3716",  # pr-3685 (7 PRs)
    "3790-3796-3805-3806-3810-3811-3812-3813-3814-3816-3818-3819-3820-3823-3824",  # pr-3790 (15 PRs)
    "3827-3866-3871-3873-3875-3876-3878-3881-3884",  # pr-3827 (9 PRs)
    "3887-3910-3911",  # pr-3887 (3 PRs)
    "3907-3909",  # pr-3907 (2 PRs)
    "3928-3988-3994-4001",  # pr-3928 (4 PRs)
    "3998-4012",  # pr-3998 (2 PRs)
    "4015-4016",  # pr-4015 (2 PRs)
    "4062-4065-4068-4069-4070",  # pr-4062 (5 PRs)
    "4127-4149-4153-4155-4158-4159-4162",  # pr-4127 (7 PRs)
    "4323-4328-4330-4331",  # pr-4323 (4 PRs)
    "4383-4416-4420-4427-4429-4435-4436-4439",  # pr-4383 (8 PRs)
    "4386-4387-4388-4389-4390-4392-4393-4394-4395-4396-4398-4399-4400-4401-4402",  # pr-4386 (15 PRs)
    "4568-4591-4621-4626-4629-4633-4634-4636",  # pr-4568 (8 PRs)
    "4609-4610",  # pr-4609 (2 PRs)
    "4614-4617-4618-4619",  # pr-4614 (4 PRs)
    "4669-4710-4713-4714-4716",  # pr-4669 (5 PRs)
    "4681-4682-4684-4685-4688",  # pr-4681 (5 PRs)
    "4687-4715-4718-4719-4720-4721-4723-4725-4726",  # pr-4687 (9 PRs)
    "4700-4701-4702",  # pr-4700 (3 PRs)
    "4875-4881",  # pr-4875 (2 PRs)
    "4907-4914-4917-4918",  # pr-4907 (4 PRs)
    "4923-4931-4935-4939-4940",  # pr-4923 (5 PRs)
    "4944-4947-4948",  # pr-4944 (3 PRs)
    "5001-5002-5005-5006-5008-5010-5011-5013-5016-5018",  # pr-5001 (10 PRs)
    "5038-5058",  # pr-5038 (2 PRs)
    "5079-5080-5082",  # pr-5079 (3 PRs)
    "5127-5129-5134-5135-5137-5138-5141-5142-5143",  # pr-5127 (9 PRs)
]

for _ni in _BUNDLE_NIS_MC_UNRESOLVED:
    Instance.register("minio", _ni)(Mc)
