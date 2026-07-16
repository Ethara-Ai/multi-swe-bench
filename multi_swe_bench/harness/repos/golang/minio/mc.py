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
