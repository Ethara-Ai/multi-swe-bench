"""Restic harness for the module era — cmd/internal layout, Go modules.

Covers the bundles whose base commit carries a go.mod (v0.9.4 onwards).

Test command: go test -v -count=1 ./cmd/... ./internal/...
"""

import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# === number_interval: dash-joined prs_in_bundle ===
#
# FORMAT: the explicit dash-joined member list, NEVER a range. A range like
# "146-157" claims every PR from 146 to 157; the bundles here are sparse, so
# [146, 147, 150, 155, 157] must serialise as "146-147-150-155-157". Restic's
# bundles are extremely sparse (pr-2195 bundles 151 PRs spread over 2255..2930),
# so a range form would be wrong for essentially every record.
#
# WHY A PATCH IS NEEDED: the raw jsonl carries `prs_in_bundle` but NO
# `number_interval`, and PullRequest.from_json goes through dataclass_json, which
# DROPS unknown keys. So the loaded PullRequest has number_interval == "", and
# Dataset.build -- which does `number_interval=pr.number_interval` -- writes ""
# into every row of the resolved jsonl. Deriving it at load time is the only
# place the bundle is still visible.
#
# Unlike the stash-only convention used by MHSanaei/3x-ui, restic DOES set
# pr.number_interval, because for this repo it is also the era routing key:
# instance.py routes on f"{org}/{number_interval}", and the three era classes
# are only reachable that way. Leaving it "" would route all 33 bundles to
# "restic/restic" (the module-era class), which is wrong for the 16 bundles
# whose base commit predates go.mod. It is only set when the resulting key is
# actually registered (every bundle in this dataset is, see the tables at the
# bottom of this file and of restic_era1.py / restic_era2.py); an unregistered
# bundle falls back to "" so Instance.create resolves via "restic/restic"
# instead of raising, and the Dataset.build patch below still stamps the value
# onto the output row.
#
# Two import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to harness
# source), following the aquasecurity/tfsec, elastic/beats and MHSanaei/3x-ui
# convention. Installed here rather than in the era files because __init__.py
# imports this module first and the filter covers all three eras.
import multi_swe_bench.harness.pull_request as _pull_request  # noqa: E402


def _restic_number_interval(raw: dict) -> str:
    """Dash-joined explicit member list of prs_in_bundle ("" if unavailable)."""
    bundle = raw.get("prs_in_bundle")
    if not bundle:
        return ""
    return "-".join(str(p) for p in bundle)


if not getattr(_pull_request.PullRequest, "_restic_number_interval_patched", False):
    _restic_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _restic_from_json(cls, json_str):
        pr = _restic_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if raw.get("org") == "restic" and raw.get("repo") == "restic":
                ni = _restic_number_interval(raw)
                if ni:
                    # Stash unconditionally so the output row can be stamped
                    # even when the bundle is not a registered routing key.
                    pr._restic_ni = ni
                    if f"restic/{ni}" in Instance._registry:
                        pr.number_interval = ni
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_restic_from_json)
    _pull_request.PullRequest._restic_number_interval_patched = True

    # Stamp number_interval onto the resolved-jsonl row. Redundant when the
    # routing key was set above (Dataset.build already copies it), and the
    # actual fix for the unregistered-bundle fallback.
    #
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_restic_build_patched", False):
        _restic_orig_build = _Dataset.build.__func__

        def _restic_build(cls, pr, report):
            ds = _restic_orig_build(cls, pr, report)
            if not ds.number_interval:
                ds.number_interval = getattr(pr, "_restic_ni", "")
            return ds

        _Dataset.build = classmethod(_restic_build)
        _Dataset._restic_build_patched = True
# ---------------------------------------------------------------------------


# Vendor-aware module resolution, shared by every run script in this era.
#
# The early module-era bundles (v0.9.4-v0.9.6) still ship a vendor/ tree
# ALONGSIDE go.mod. Their go.sum pulls contrib.go.opencensus.io/exporter/
# ocagent@v0.4.3, whose transitive requirement carries a pseudo-version that
# modern Go rejects outright:
#
#   go: contrib.go.opencensus.io/exporter/ocagent@v0.4.3 requires
#       github.com/census-instrumentation/opencensus-proto@v0.1.0-0.20181214143942-...:
#       invalid pseudo-version: version before v0.1.0 would have negative patch number
#
# That aborts `go test` before a single test runs. It is not a base-state
# problem -- pr-1944's `run` stage was healthy (810 passing) and only its FIX
# stage broke, because the fix patch rewrites go.mod/go.sum and pulls the bad
# requirement in. The whole instance was then scored unresolved on
# fix_patch_result.all_count == 0.
#
# -mod=vendor makes the toolchain read the vendored copies and never resolve or
# download modules, so the malformed pseudo-version is never parsed. Verified on
# pr-1944: -mod=mod reproduces the error, -mod=vendor tests ok.
#
# Gated on vendor/modules.txt because the later bundles (v0.11.0 onward) dropped
# vendor/ entirely and must keep resolving normally -- an unconditional
# -mod=vendor would break all 14 of them.
#
# Applied identically in prepare/run/test/fix so all three graded stages resolve
# dependencies the same way; a stage-dependent flag would skew the reward
# buckets.
#
# ---------------------------------------------------------------------------
# The suite runs as an UNPRIVILEGED user, never as root.
#
# The build host has SELinux enabled, so every file created inside the container
# is auto-labelled with a security.selinux xattr. Running as root, restic's
# restore path then tries to strip that label and gets EPERM:
#
#   ignoring error for /restic.src: xattr.LRemove
#       /tmp/restic-test-.../restore/restic.src security.selinux: permission denied
#   cmd_restore_integration_test.go:31: unexpected error: There were 4 errors
#
# That is what flipped pr-4939's TestRestoreWithPermissionFailure from PASS at
# the test stage to FAIL at the fix stage, tripping the "no new failures" rule
# and failing the whole instance. The same test passes unprivileged.
#
# This is NOT a single-instance workaround. Measured on pr-5491, which already
# resolved: the base-state suite reports 27 failing tests as root and 6 as
# non-root. Those 21 root-only failures are permanently-failing tests that can
# never be credited as F2P, so running as root was silently shrinking the reward
# signal on every module-era instance. Upstream restic CI also runs unprivileged.
#
# Ownership is re-applied before each graded run because the preceding
# `git apply` runs as root and leaves root-owned files behind. GOCACHE/GOMODCACHE
# live under /gocache, owned by the same user, so caches warmed by prepare.sh
# are reusable by the graded runs. HOME must be passed explicitly -- `runuser`
# without `-l` would otherwise leave HOME=/root, which the user cannot write.
TEST_USER = "restictest"

GO_CACHE_ENV = "GOCACHE=/gocache/build GOMODCACHE=/gocache/mod HOME=/home/" + TEST_USER


def go_test_cmd(repo: str) -> str:
    """Emit the vendor-aware, unprivileged `go test` invocation for this era."""
    return f"""if [ -f vendor/modules.txt ]; then FLAGS="-mod=vendor"; else FLAGS=""; fi
chown -R {TEST_USER}:{TEST_USER} /home/{repo} /gocache
runuser -u {TEST_USER} -- env GOFLAGS="$FLAGS" {GO_CACHE_ENV} \\
    go test -v -count=1 ./cmd/... ./internal/..."""


class ResticImageBase(Image):
    """Toolchain + full-history checkout, shared by every PR in this era.

    ``image_tag()`` is the constant ``"base"``, so ONE image serves every PR
    here while the records carry different ``base.sha`` values. That is why this
    Dockerfile declares its own ``# syntax`` directive: it makes
    ``DockerfileEnhancer.enhance()`` return the content verbatim, which is the
    only way to keep the enhancer's ``_standardize_repo_fetch`` from rewriting
    the clone below into ``git clone`` + ``git checkout ${BASE_COMMIT}`` +
    ``Image._HARDENING_BLOCK``. That rewrite deletes every ref and
    ``gc --prune``s the repository down to a single commit, so the first PR to
    build would pin this shared tag to its own base.sha and every other PR in
    the era would then fail ``git checkout <their sha>`` with "reference is not
    a tree".

    Opting out means the ARG/ENV/LABEL block the enhancer would have injected is
    no longer free, so the parts still wanted are spelled out inline below.

    The base therefore keeps FULL history (every era member's base.sha stays
    reachable) and only takes the hardening that is safe to share: the network
    remote is dropped so no later layer — and no agent — can re-fetch upstream
    history. The strict per-PR anti-reward-hacking hardening runs one tier up,
    in ResticImageDefault, where pinning to a single base.sha is correct.
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
        return "golang:latest"

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

        # Validated before interpolation into the clone URL / WORKDIR paths.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

{fetch}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

# Unprivileged account the graded test runs execute under (see go_test_cmd).
# The checkout gets chowned to it at run time, which makes root's later git
# commands -- the hardening block in the PR image -- see "dubious ownership";
# safe.directory keeps them working.
RUN useradd -m -u 1000 {TEST_USER} && \\
    mkdir -p /gocache/build /gocache/mod && \\
    chown -R {TEST_USER}:{TEST_USER} /gocache && \\
    git config --global --add safe.directory '*'

{self.clear_env}

CMD ["/bin/bash"]
"""


class ResticImageDefault(Image):
    """Per-PR grading image — this is the tier that carries the hardening.

    ``prepare.sh`` checks out this PR's ``base.sha`` out of the shared base's
    full history; the canonical ``Image._HARDENING_BLOCK`` then detaches at that
    literal sha and strips every other ref, the reflogs and all unreachable
    objects, so the PR's own fix commit — and everything merged after it — is no
    longer readable out of git inside the image.
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

    def dependency(self) -> Image | None:
        return ResticImageBase(self.pr, self.config)

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

{go_test} || true

""".format(pr=self.pr, go_test=go_test_cmd(self.pr.repo)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{go_test}

""".format(pr=self.pr, go_test=go_test_cmd(self.pr.repo)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{go_test}

""".format(pr=self.pr, go_test=go_test_cmd(self.pr.repo)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{go_test}

""".format(pr=self.pr, go_test=go_test_cmd(self.pr.repo)),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # This image's dependency() is an Image, so DockerfileEnhancer returns
        # the content verbatim and injects nothing -- the hardening has to be
        # emitted here explicitly. ${BASE_COMMIT} is substituted with the literal
        # sha because the pipeline only passes REPO_URL/BASE_COMMIT build args to
        # string-dependency (base) images. Concatenating the block through
        # .replace rather than an f-string keeps its %(refname) tokens literal.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("restic", "restic")
class Restic(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ResticImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# instance.py routes on f"{org}/{number_interval}" whenever number_interval is
# set, so every dash-joined bundle value a record can carry must resolve to a
# class. These are the the module era (go.mod present) bundles. Without them a delivered
# jsonl that carries number_interval raises "Instance 'restic/<bundle>' is not
# registered" before a single image is built. The bare "restic/restic" key
# registered above still routes records whose number_interval is empty.
#
# Explicit dash-joined member lists, never ranges -- the bundles are sparse.
_BUNDLE_NIS_RESTIC = [
    "1944-2032-2087-2124-2138-2139-2142-2147-2151-2153-2154-2156-2167-2171-2185-2187-2189-2193-2197-2205-2208-2209-2210-2217-2218-2220-2221-2228-2230-2231-2232-2243-2247",  # pr-1944 (33 PRs, v0.9.4..v0.9.5)
    "2195-2255-2271-2285-2287-2294-2318-2328-2337-2342-2354-2358-2373-2391-2423-2439-2461-2465-2486-2487-2488-2489-2509-2510-2524-2530-2540-2545-2546-2552-2560-2568-2570-2573-2574-2575-2576-2577-2579-2581-2582-2583-2584-2587-2589-2592-2598-2599-2600-2602-2603-2605-2606-2607-2608-2610-2612-2616-2621-2622-2623-2628-2630-2633-2635-2637-2638-2640-2644-2648-2652-2655-2660-2668-2669-2674-2677-2681-2682-2684-2689-2692-2695-2702-2704-2709-2711-2713-2716-2717-2719-2730-2732-2733-2741-2749-2755-2760-2769-2773-2776-2778-2779-2781-2784-2786-2787-2789-2790-2802-2805-2809-2812-2813-2815-2818-2821-2827-2832-2835-2840-2841-2845-2847-2852-2854-2855-2857-2859-2861-2863-2864-2865-2868-2869-2874-2879-2884-2885-2893-2896-2897-2898-2899-2904-2905-2914-2927-2928-2929-2930",  # pr-2195 (151 PRs, v0.9.6..v0.10.0)
    "2206-2212-2251-2252-2253-2257-2261-2266-2304-2307-2310-2321-2322-2324-2333-2368-2394-2425-2442-2444-2450-2456-2463-2471-2472-2476-2478-2479-2480-2483-2484",  # pr-2206 (31 PRs, v0.9.5..v0.9.6)
    "2274-2614-2658-2844-2849-2906-2910-2931-2932-2933-2934-2935-2936-2937-2939-2940-2945-2955-2957-2959-2963-2964-2965-2966-2967-2970-2972-2973-2974-2975-2977-2978-2980-2981-2982-2983-2984-2986-2987-2988-2989-2990-2991-2992-2996-2997-2998-3002-3011-3015-3016-3018-3019-3022-3025-3026-3031-3035-3039-3042-3045-3050-3051-3052-3053-3055",  # pr-2274 (66 PRs, v0.10.0..v0.11.0)
    "2311-2594-2657-2816-2838-2856-2880-3163-3246-3264-3300-3429-3436-3467-3474-3479-3480-3482-3485-3487-3488-3496-3499-3501-3502-3507-3509-3510-3512-3514-3519-3520-3522-3523-3524-3526-3528-3532-3534-3535-3537-3539-3544-3548-3562-3565-3566-3568-3571-3573-3574-3575-3578-3589-3590-3591-3592-3593-3602-3605-3607-3615-3618-3619-3623-3626-3628-3638-3642-3644-3645-3653-3654-3656-3663-3664-3665-3668-3673-3677-3678",  # pr-2311 (81 PRs, v0.12.1..v0.13.0)
    "2398-2661-2731-2750-2875-3132-3521-3569-3780-3854-3875-3877-3878-3879-3880-3882-3885-3886-3893-3894-3895-3898-3899-3900-3904-3905-3912-3913-3915-3921-3923-3924-3925-3927-3928-3931-3935-3938-3940-3942-3943-3944-3947-3948-3949-3950-3951-3952-3953-3955-3956-3957-3958-3959-3960-3961-3966-3967-3968-3969-3970-3971-3972-3973-3974-3977-3978-3979-3980-3981-3982-3983-3985-3986-3988-3989-3990-3992-3993-3996-3997-4000-4001-4007-4008-4010-4011-4014-4017-4019-4020-4021-4022-4024-4025-4028-4036-4037-4038-4039-4040-4041-4042-4044-4047-4049-4050-4051-4053-4056-4059-4063-4064-4065-4066-4067-4068-4069-4070-4074-4075-4077-4079-4082-4083-4084-4086-4088-4089-4090-4094-4100-4104-4108-4109-4110-4117-4134-4135-4136-4138-4139-4140-4141-4142",  # pr-2398 (145 PRs, v0.14.0..v0.15.0)
    "2475-2505-2535-2536-2690-2718-2793-2823-2833-2842-2850-2941-3006-3008-3014-3017-3030-3034-3038-3048-3058-3063-3065-3068-3069-3075-3076-3077-3081-3082-3085-3086-3090-3093-3094-3099-3101-3102-3103-3105-3106-3107-3109-3112-3113-3115-3119-3120-3125-3128-3130-3134-3135-3136-3138-3139-3141-3148-3149-3150-3152-3158-3164-3165-3170-3173-3174-3175-3176-3177-3179-3181-3188-3189-3192-3197-3199-3204-3205-3207-3208-3211-3217-3228-3236-3242-3243-3244-3245-3248-3249-3250-3251-3253-3254-3255-3256-3270-3282",  # pr-2475 (99 PRs, v0.11.0..v0.12.0)
    "2740-2876-3225-3261-3563-3802-3939-3991-4029-4081-4107-4166-4176-4177-4180-4182-4192-4194-4195-4196-4198-4201-4205-4210-4212-4213-4219-4220-4224-4226-4232-4234-4235-4236-4240-4242-4243-4244-4246-4247-4249-4255-4259-4264-4265-4266-4270-4271-4272-4273-4282-4283-4285-4286-4288-4291-4293-4296-4298-4299-4300-4301-4302-4304-4305-4306-4308-4309-4310-4311-4312-4314-4315-4316-4317-4318-4328-4331-4333-4334-4339-4340-4342-4343-4345-4346-4347-4348-4350-4351-4352-4353-4355-4356-4360-4361-4362-4364-4365-4366-4373-4374-4378-4379-4383-4384-4387-4389-4390-4391-4392-4395-4400-4401-4402-4403-4407-4409-4417-4422-4423-4424-4426-4427",  # pr-2740 (124 PRs, v0.15.2..v0.16.0)
    "2999-3167-3257-3283-3286-3287-3294-3298-3305-3308-3309-3310-3312-3319-3320-3321-3323-3325-3327-3331-3332-3335-3343-3345-3347-3354-3356-3362-3363-3371-3373-3376-3386-3393-3394-3399-3401-3402-3403-3409-3416-3420-3421-3426-3427-3437-3438-3442-3449-3452-3453-3454-3457-3468",  # pr-2999 (54 PRs, v0.12.0..v0.12.1)
    "3067-4006-4354-4410-4474-4499-4503-4520-4526-4527-4538-4539-4541-4542-4543-4545-4550-4553-4554-4555-4557-4559-4563-4570-4571-4572-4573-4576-4577-4579-4580-4582-4584-4586-4590-4593-4596-4598-4600-4605-4606-4607-4608-4609-4610-4611-4615-4616-4618-4620-4621-4622-4623-4624-4625-4626-4639-4641-4643-4644-4645-4647-4648-4649-4650-4654-4655-4657-4661-4662-4663-4664-4665-4666-4668-4669-4670-4671-4674-4675-4679-4681-4682-4684-4685-4687-4692-4694-4696-4697-4700-4701-4703-4705-4708-4709-4713-4714-4715-4716-4717-4718-4719-4722-4724-4725-4726-4727-4731-4734-4737-4739-4740-4741-4742-4743-4745-4748-4750-4751-4753-4756-4761-4763-4764-4766-4769-4770-4772-4776-4782-4784-4786-4787-4789-4790-4792-4794-4796-4799-4800-4802-4803-4805-4807-4808-4809-4810-4811-4812-4814-4815-4816-4818-4819-4820-4821-4822-4823-4824-4825-4826-4829-4835-4837-4838-4839-4840-4842-4843-4844-4845-4847-4849-4851-4852-4853-4856-4861-4863-4864-4866-4881-4882-4883-4884-4885-4886-4887-4888-4889-4890-4891-4893-4894-4896-4904-4905-4906-4907-4908-4909-4911-4912-4913-4914-4916-4917-4918-4920-4921-4925-4926-4930-4931-4932-4933-4936",  # pr-3067 (218 PRs, v0.16.5..v0.17.0)
    "4111-4116-4129-4145-4146-4149-4150-4151-4152-4153-4154-4163-4167-4169-4170-4171-4175-4178",  # pr-4111 (18 PRs, v0.15.0..v0.15.1)
    "4394-4419-4428-4429-4434-4436-4442-4446-4450-4451-4452-4453-4454-4457-4458-4459-4462-4464-4471-4480-4485-4486-4487-4489-4490-4491-4492-4493-4495-4496-4498-4500-4502-4505-4511-4514-4518-4519-4524-4528-4530-4531-4532-4533-4534-4535",  # pr-4394 (46 PRs, v0.16.0..v0.16.1)
    "4938-4952-4959-4981-4989-4990-4993-4995-4996-4997-4998-4999-5000-5006-5007-5008-5009-5010-5011-5012-5013-5014-5015-5016-5017-5018-5019-5020-5022-5023-5024-5026-5028-5032-5033-5034-5035-5037-5038-5039-5040-5042-5043-5045-5046-5047-5048-5051-5053-5054-5056-5057-5058-5060-5061-5074-5075-5076-5077-5078-5079-5080-5083-5084-5093-5094-5095-5096-5097-5100-5101-5105-5112-5113-5114-5115-5116-5117-5119-5120-5121-5122-5123-5129-5134-5138-5141-5142-5143-5144-5145-5146-5153-5158-5161-5162-5163-5165-5166-5167-5168-5170-5173-5179-5180-5182-5183-5184-5185-5194-5196-5200-5202-5207-5211-5212-5219-5222-5223-5225-5226-5227-5228-5232-5235-5240-5241-5242-5249-5251-5255-5256-5262-5263-5264-5265-5267-5268-5270-5274-5292-5295-5296-5297-5298-5299-5300-5302-5304-5305-5306-5307-5308-5315-5316",  # pr-4938 (155 PRs, v0.17.3..v0.18.0)
    "4939-4946-4951-4954-4956-4958-4960-4967-4973-4974-4976-4977-4978-4979-4980-4994-5029-5030",  # pr-4939 (18 PRs, v0.17.0..v0.17.1)
    "5098-5099-5102",  # pr-5098 (3 PRs, v0.17.1..v0.17.2)
    "5110-5125-5126",  # pr-5110 (3 PRs, v0.17.2..v0.17.3)
    "5491-5508-5509-5513",  # pr-5491 (4 PRs, v0.18.0..v0.18.1)
]

for _ni in _BUNDLE_NIS_RESTIC:
    Instance.register("restic", _ni)(Restic)
