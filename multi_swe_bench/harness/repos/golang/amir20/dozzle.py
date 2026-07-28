import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DozzleImageBase(Image):
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
        # dozzle's go.mod `go` directive ranges from 1.21 (v5.x era, PR #2478)
        # up to 1.26.3 (v10.x era, PR #4670). Go is backward compatible, so the
        # newest toolchain in the dataset builds every era -> single base image.
        return "golang:1.26-bookworm"

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

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise rewrite the clone into checkout `${{BASE_COMMIT}}` +
        # prune HERE, pruning the shared base to one PR's base.sha and breaking
        # every other PR ("reference is not a tree"). Base keeps FULL history;
        # per-PR literal-sha hardening runs in DozzleImageDefault.
        return f"""# syntax=docker/dockerfile:1.6
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

RUN apt-get update && apt-get install -y --no-install-recommends git openssl ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# -mod=mod lets older eras (go 1.21) self-heal their go.sum under the 1.26 toolchain.
# GOTOOLCHAIN=auto downloads the exact toolchain pinned by each PR's go.mod when needed.
ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=auto
RUN git config --global --add safe.directory '*'

WORKDIR /home/
{code}
WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class DozzleImageDefault(Image):
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
        return DozzleImageBase(self.pr, self.config)

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

git config --global --add safe.directory '*'
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Makefile `fake_assets` target: tests embed dist/ via go:embed, so an empty
# dist/index.html is required for the binary to compile.
mkdir -p dist
echo "assets build was skipped" > dist/index.html

# Makefile `shared_key.pem` / `shared_cert.pem` targets: internal/agent tests
# load these certs at init() time and panic if missing.
openssl genpkey -algorithm Ed25519 -out shared_key.pem
openssl req -new -key shared_key.pem -out shared_request.csr \\
    -subj "/C=US/ST=California/L=San Francisco/O=Dozzle"
openssl x509 -req -in shared_request.csr -signkey shared_key.pem \\
    -out shared_cert.pem -days 1825
rm shared_request.csr

# Pre-fetch module dependencies so the eval run is offline-friendly.
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the dozzle run/test/fix scripts.
#
# dozzle's Go tests are fast (~5s for the whole repo) and self-contained, so
# we run `go test ./...` across all packages rather than scoping to touched
# packages. The TypeScript/Vue test suite and the docker-compose-driven
# Playwright e2e tests are out of scope (they need a live cluster).

EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf --exclude=docs/*"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --3way $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --reject $EXCLUDES "$f" \\
    || true
}

run_go_tests() {
  echo "=== Running go test on all packages ==="
  go test -v -count=1 -timeout=600s ./...
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch
run_go_tests

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

        # Per-PR anti-cheat hardening at the LITERAL base.sha. prepare.sh checks
        # out this PR's base.sha, then this block detaches at that literal sha and
        # strips every other ref/reflog so the fix commit is unreachable from git.
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


@Instance.register("amir20", "dozzle")
class Dozzle(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DozzleImageDefault(self.pr, self._config)

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
        # `go test` is not colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        # A package summary line ("ok   <import-path>", "FAIL <import-path>",
        # "?    <import-path>") closes the block of tests printed above it.
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        # Buffer tests per-package so the import path can be prepended -- this
        # keeps names globally unique across packages.
        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            pass_match = re_pass.match(line)
            if pass_match:
                pending_pass.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                pending_fail.add(fail_match.group(1))
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                pending_skip.add(skip_match.group(1))
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        # Flush tests not followed by a summary line (e.g. truncated/timed-out
        # log) so they are still counted.
        flush("unknown")

        # Enforce TestResult disjointness invariants: a test reported as both
        # passed and failed (e.g. flaky retry) counts as failed.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- §11b bundle keys: dash-joined prs_in_bundle -> Dozzle. 83 bundles (single-era).
_BUNDLE_NIS_DOZZLE = [
    "2478-2479-2480-2483-2485-2487",
    "2490-2491-2492-2493-2497-2498-2499-2501",
    "2502-2505-2507-2511-2512-2514-2515-2516",
    "2628-2632-2633-2634-2635-2637-2638-2641-2642-2645",
    "2659-2660-2661-2662-2663",
    "2668-2669-2670-2671",
    "2717-2719-2720-2722-2723-2724-2728-2729-2730-2731",
    "2754-2758-2760-2762-2763",
    "2789-2790-2791-2794-2795-2796-2797",
    "2826-2827-2830-2831-2832-2835-2836-2837",
    "2840-2841-2843-2845-2846-2847-2848-2849-2850-2852-2853-2854-2855",
    "2857-2858-2859-2860-2863-2864-2868-2869-2871-2872-2874-2875-2876-2878-2880-2881",
    "2903-2905-2906-2908-2910-2911-2912-2914-2916-2917-2918",
    "2952-2957-2958-2959-2965-2966-2968-2971-2972-2973-2974",
    "2961-2975-2976-2977-2978-2979-2980",
    "2981-2982-2983-2984-2985-2986-2987-2988-2990",
    "2992-2994-2995-2996-2997-2998-2999-3000",
    "3001-3002-3004-3005-3007-3008-3009",
    "3012-3013-3014",
    "3033-3034-3037-3039-3040-3041-3042-3043-3045-3046-3047-3049-3051-3053",
    "3145-3150-3151-3152",
    "3201-3203-3205-3206-3208",
    "3244-3245-3246-3247",
    "3272-3281",
    "3276-3283",
    "3284-3286-3289-3290-3291-3292",
    "3294-3295",
    "3599-3615-3616-3617-3618-3619-3620-3622-3623-3624",
    "3656-3658-3661",
    "3693-3694-3695-3696-3697",
    "3951-3952-3956-3957-3958-3959-3960-3961-3965-3966-3967-3968-3969-3972-3973-3974",
    "4233-4234-4236-4240-4242",
    "4463-4466-4468",
    "4502-4508-4509-4510-4511-4512-4513-4516-4517-4518-4520",
    "4529-4530-4532-4533-4534-4535-4536-4537-4538-4542-4543-4544-4545",
    "4546-4549-4550-4552-4553-4554-4555-4556-4558-4559-4560",
    "4605-4607-4608-4609-4610-4612-4613",
    "4634-4635-4637-4638",
    "4639-4640-4641-4642-4645-4646-4647-4651-4652-4653-4656-4657",
    "4644-4675-4676-4677-4681-4683-4684-4686-4687-4688-4690-4691",
    "4670-4671-4672-4673-4674",
    "2559-2560-2562-2564-2565-2566-2567-2569-2570-2572-2573-2574-2577-2578-2580-2581-2582-2583-2584",
    "2705-2707-2708-2709-2711-2712-2713",
    "2732-2733-2734-2735-2737-2740-2741-2742-2745-2746-2747-2748-2750-2751",
    "2892-2893-2895-2896",
    "2919-2920-2924-2926-2928-2929-2930-2932-2933-2936-2937-2939-2942",
    "3016-3018-3020-3022-3023-3024-3025-3029-3030-3031-3032",
    "3054-3055-3056-3058-3059-3060-3062-3064-3065",
    "3068-3070-3071-3073-3076",
    "3132-3136-3137-3142-3143-3144-3148",
    "3165-3167-3168-3169-3170",
    "3209-3210-3211-3213-3214-3215-3216-3218-3219-3220-3224-3227-3230-3231-3232-3233-3235-3236-3237-3238-3239-3242",
    "3296-3299-3300",
    "3303-3304-3305-3306-3307-3310-3311-3312-3313-3314-3315",
    "3317-3318-3319-3320-3321-3322-3324-3327",
    "3330-3332-3333-3335-3336-3338",
    "3357-3359-3360-3361-3362-3363-3366-3368-3369",
    "3394-3398-3399-3400",
    "3402-3403-3404-3406-3407-3408-3409",
    "3442-3443-3445-3446-3450-3452-3453-3454-3455-3456-3457",
    "3460-3461-3462-3463-3464-3465-3466-3468-3469-3470-3474-3475-3476-3480-3481-3482-3484-3486-3487-3488-3490-3491",
    "3492-3493-3494-3495-3496-3498-3499-3500-3501-3503-3504-3505-3506-3507-3508-3509-3510-3511",
    "3519-3520-3521",
    "3572-3573-3576-3577-3578-3579-3583-3584-3588-3591-3592-3593-3594-3596-3598-3603-3605-3607-3611-3612",
    "3645-3647-3651-3653-3655",
    "3757-3759-3763-3765-3766-3771-3772-3773-3776",
    "3782-3785-3786",
    "3787-3789-3791-3792-3793-3794",
    "3902-3907-3908-3910",
    "3940-3941-3944-3946-3947-3949",
    "3975-3977-3979-3982-3985",
    "4106-4119-4120-4121-4124-4125-4126-4127-4128-4132-4133-4134-4138-4139-4140",
    "4147-4150-4151-4152-4153-4155-4156-4158",
    "4198-4199-4200-4201-4203-4207-4210-4211-4213-4214",
    "4217-4218-4219-4223-4224-4227-4228-4229",
    "4295-4296-4297-4298-4299-4303-4304-4306-4307-4313-4314-4315-4316-4318-4319-4320-4321-4322-4323-4324-4325",
    "4349-4359-4361-4362-4366-4367-4368-4369-4371-4372-4373-4374-4378-4379-4381-4382-4384-4385-4388-4389-4391-4392-4393-4394-4398-4399-4400-4401-4402-4403-4404-4406-4408-4409-4410-4411-4413-4414-4415-4416-4418-4419-4423",
    "4417-4424-4427-4428-4429",
    "4439-4440-4441-4443-4446-4449-4451-4454-4457-4458-4459-4460-4462",
    "4450-4567-4568-4570-4571-4573-4574-4578-4579-4580-4582-4583-4584-4586-4587-4588-4590-4591-4592-4593-4595-4596-4598-4599-4600",
    "4521-4522-4525-4527",
    "4563-4564-4566",
    "4660-4662-4664-4665-4667",
]
for _ni in _BUNDLE_NIS_DOZZLE:
    Instance.register("amir20", _ni)(Dozzle)

