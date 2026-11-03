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

import multi_swe_bench.harness.pull_request as _pull_request


def _restic_number_interval(raw: dict) -> str:
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
                    pr._restic_ni = ni
                    if f"restic/{ni}" in Instance._registry:
                        pr.number_interval = ni
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_restic_from_json)
    _pull_request.PullRequest._restic_number_interval_patched = True

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


TEST_USER = "restictest"

GO_CACHE_ENV = (
    "GOTOOLCHAIN=local GOCACHE=/gocache/build GOMODCACHE=/gocache/mod "
    "HOME=/home/" + TEST_USER
)


def go_test_cmd(repo: str) -> str:
    return f"""if [ -f vendor/modules.txt ]; then FLAGS="-mod=vendor"; else FLAGS=""; fi
chown -R {TEST_USER}:{TEST_USER} /home/{repo} /gocache
runuser -u {TEST_USER} -- env GOFLAGS="$FLAGS" {GO_CACHE_ENV} \\
    go test -v -count=1 ./cmd/... ./internal/..."""


def go_gate_cmd(repo: str) -> str:
    return f"""if [ -f vendor/modules.txt ]; then FLAGS="-mod=vendor"; else FLAGS=""; fi
chown -R {TEST_USER}:{TEST_USER} /home/{repo} /gocache
runuser -u {TEST_USER} -- env GOFLAGS="$FLAGS" {GO_CACHE_ENV} \\
    go build ./...
echo DEPS_OK"""


class ResticImageBase(Image):

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

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}


WORKDIR /home/

{fetch}

WORKDIR /home/{repo}

{self.clear_env}
CMD ["/bin/bash"]
"""


class ResticImageDefault(Image):

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

# Unprivileged account the graded test runs execute under (see go_test_cmd),
# and the caches it owns. Done here rather than in the base image so the base
# Dockerfile stays exactly the standard template -- toolchain image, clone,
# nothing else. safe.directory is required because this script chowns the
# checkout to {test_user}, which makes root's later git commands (the reset,
# the pin and the prune block in the PR image) see "dubious ownership".
id -u {test_user} >/dev/null 2>&1 || useradd -m -u 1000 {test_user}
mkdir -p /gocache/build /gocache/mod
chown -R {test_user}:{test_user} /gocache
git config --global --add safe.directory '*'

cd /home/{pr.repo}
git remote remove origin 2>/dev/null || true
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

{go_test} || true

{go_gate}

""".format(
                    pr=self.pr,
                    test_user=TEST_USER,
                    go_test=go_test_cmd(self.pr.repo),
                    go_gate=go_gate_cmd(self.pr.repo),
                ),
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

        hardening = """RUN set -eux; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}

{self.clear_env}
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

        re_random_subtest = re.compile(r"/max-\d+")

        for line in clean_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(re_random_subtest.sub("/max-N", m.group(1)))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(re_random_subtest.sub("/max-N", m.group(1)))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(re_random_subtest.sub("/max-N", m.group(1)))
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


_BUNDLE_NIS_RESTIC = [
    "1944-2032-2087-2124-2138-2139-2142-2147-2151-2153-2154-2156-2167-2171-2185-2187-2189-2193-2197-2205-2208-2209-2210-2217-2218-2220-2221-2228-2230-2231-2232-2243-2247",
    "2195-2255-2271-2285-2287-2294-2318-2328-2337-2342-2354-2358-2373-2391-2423-2439-2461-2465-2486-2487-2488-2489-2509-2510-2524-2530-2540-2545-2546-2552-2560-2568-2570-2573-2574-2575-2576-2577-2579-2581-2582-2583-2584-2587-2589-2592-2598-2599-2600-2602-2603-2605-2606-2607-2608-2610-2612-2616-2621-2622-2623-2628-2630-2633-2635-2637-2638-2640-2644-2648-2652-2655-2660-2668-2669-2674-2677-2681-2682-2684-2689-2692-2695-2702-2704-2709-2711-2713-2716-2717-2719-2730-2732-2733-2741-2749-2755-2760-2769-2773-2776-2778-2779-2781-2784-2786-2787-2789-2790-2802-2805-2809-2812-2813-2815-2818-2821-2827-2832-2835-2840-2841-2845-2847-2852-2854-2855-2857-2859-2861-2863-2864-2865-2868-2869-2874-2879-2884-2885-2893-2896-2897-2898-2899-2904-2905-2914-2927-2928-2929-2930",
    "2206-2212-2251-2252-2253-2257-2261-2266-2304-2307-2310-2321-2322-2324-2333-2368-2394-2425-2442-2444-2450-2456-2463-2471-2472-2476-2478-2479-2480-2483-2484",
    "2274-2614-2658-2844-2849-2906-2910-2931-2932-2933-2934-2935-2936-2937-2939-2940-2945-2955-2957-2959-2963-2964-2965-2966-2967-2970-2972-2973-2974-2975-2977-2978-2980-2981-2982-2983-2984-2986-2987-2988-2989-2990-2991-2992-2996-2997-2998-3002-3011-3015-3016-3018-3019-3022-3025-3026-3031-3035-3039-3042-3045-3050-3051-3052-3053-3055",
    "2311-2594-2657-2816-2838-2856-2880-3163-3246-3264-3300-3429-3436-3467-3474-3479-3480-3482-3485-3487-3488-3496-3499-3501-3502-3507-3509-3510-3512-3514-3519-3520-3522-3523-3524-3526-3528-3532-3534-3535-3537-3539-3544-3548-3562-3565-3566-3568-3571-3573-3574-3575-3578-3589-3590-3591-3592-3593-3602-3605-3607-3615-3618-3619-3623-3626-3628-3638-3642-3644-3645-3653-3654-3656-3663-3664-3665-3668-3673-3677-3678",
    "2398-2661-2731-2750-2875-3132-3521-3569-3780-3854-3875-3877-3878-3879-3880-3882-3885-3886-3893-3894-3895-3898-3899-3900-3904-3905-3912-3913-3915-3921-3923-3924-3925-3927-3928-3931-3935-3938-3940-3942-3943-3944-3947-3948-3949-3950-3951-3952-3953-3955-3956-3957-3958-3959-3960-3961-3966-3967-3968-3969-3970-3971-3972-3973-3974-3977-3978-3979-3980-3981-3982-3983-3985-3986-3988-3989-3990-3992-3993-3996-3997-4000-4001-4007-4008-4010-4011-4014-4017-4019-4020-4021-4022-4024-4025-4028-4036-4037-4038-4039-4040-4041-4042-4044-4047-4049-4050-4051-4053-4056-4059-4063-4064-4065-4066-4067-4068-4069-4070-4074-4075-4077-4079-4082-4083-4084-4086-4088-4089-4090-4094-4100-4104-4108-4109-4110-4117-4134-4135-4136-4138-4139-4140-4141-4142",
    "2475-2505-2535-2536-2690-2718-2793-2823-2833-2842-2850-2941-3006-3008-3014-3017-3030-3034-3038-3048-3058-3063-3065-3068-3069-3075-3076-3077-3081-3082-3085-3086-3090-3093-3094-3099-3101-3102-3103-3105-3106-3107-3109-3112-3113-3115-3119-3120-3125-3128-3130-3134-3135-3136-3138-3139-3141-3148-3149-3150-3152-3158-3164-3165-3170-3173-3174-3175-3176-3177-3179-3181-3188-3189-3192-3197-3199-3204-3205-3207-3208-3211-3217-3228-3236-3242-3243-3244-3245-3248-3249-3250-3251-3253-3254-3255-3256-3270-3282",
    "2740-2876-3225-3261-3563-3802-3939-3991-4029-4081-4107-4166-4176-4177-4180-4182-4192-4194-4195-4196-4198-4201-4205-4210-4212-4213-4219-4220-4224-4226-4232-4234-4235-4236-4240-4242-4243-4244-4246-4247-4249-4255-4259-4264-4265-4266-4270-4271-4272-4273-4282-4283-4285-4286-4288-4291-4293-4296-4298-4299-4300-4301-4302-4304-4305-4306-4308-4309-4310-4311-4312-4314-4315-4316-4317-4318-4328-4331-4333-4334-4339-4340-4342-4343-4345-4346-4347-4348-4350-4351-4352-4353-4355-4356-4360-4361-4362-4364-4365-4366-4373-4374-4378-4379-4383-4384-4387-4389-4390-4391-4392-4395-4400-4401-4402-4403-4407-4409-4417-4422-4423-4424-4426-4427",
    "2999-3167-3257-3283-3286-3287-3294-3298-3305-3308-3309-3310-3312-3319-3320-3321-3323-3325-3327-3331-3332-3335-3343-3345-3347-3354-3356-3362-3363-3371-3373-3376-3386-3393-3394-3399-3401-3402-3403-3409-3416-3420-3421-3426-3427-3437-3438-3442-3449-3452-3453-3454-3457-3468",
    "3067-4006-4354-4410-4474-4499-4503-4520-4526-4527-4538-4539-4541-4542-4543-4545-4550-4553-4554-4555-4557-4559-4563-4570-4571-4572-4573-4576-4577-4579-4580-4582-4584-4586-4590-4593-4596-4598-4600-4605-4606-4607-4608-4609-4610-4611-4615-4616-4618-4620-4621-4622-4623-4624-4625-4626-4639-4641-4643-4644-4645-4647-4648-4649-4650-4654-4655-4657-4661-4662-4663-4664-4665-4666-4668-4669-4670-4671-4674-4675-4679-4681-4682-4684-4685-4687-4692-4694-4696-4697-4700-4701-4703-4705-4708-4709-4713-4714-4715-4716-4717-4718-4719-4722-4724-4725-4726-4727-4731-4734-4737-4739-4740-4741-4742-4743-4745-4748-4750-4751-4753-4756-4761-4763-4764-4766-4769-4770-4772-4776-4782-4784-4786-4787-4789-4790-4792-4794-4796-4799-4800-4802-4803-4805-4807-4808-4809-4810-4811-4812-4814-4815-4816-4818-4819-4820-4821-4822-4823-4824-4825-4826-4829-4835-4837-4838-4839-4840-4842-4843-4844-4845-4847-4849-4851-4852-4853-4856-4861-4863-4864-4866-4881-4882-4883-4884-4885-4886-4887-4888-4889-4890-4891-4893-4894-4896-4904-4905-4906-4907-4908-4909-4911-4912-4913-4914-4916-4917-4918-4920-4921-4925-4926-4930-4931-4932-4933-4936",
    "4111-4116-4129-4145-4146-4149-4150-4151-4152-4153-4154-4163-4167-4169-4170-4171-4175-4178",
    "4394-4419-4428-4429-4434-4436-4442-4446-4450-4451-4452-4453-4454-4457-4458-4459-4462-4464-4471-4480-4485-4486-4487-4489-4490-4491-4492-4493-4495-4496-4498-4500-4502-4505-4511-4514-4518-4519-4524-4528-4530-4531-4532-4533-4534-4535",
    "4938-4952-4959-4981-4989-4990-4993-4995-4996-4997-4998-4999-5000-5006-5007-5008-5009-5010-5011-5012-5013-5014-5015-5016-5017-5018-5019-5020-5022-5023-5024-5026-5028-5032-5033-5034-5035-5037-5038-5039-5040-5042-5043-5045-5046-5047-5048-5051-5053-5054-5056-5057-5058-5060-5061-5074-5075-5076-5077-5078-5079-5080-5083-5084-5093-5094-5095-5096-5097-5100-5101-5105-5112-5113-5114-5115-5116-5117-5119-5120-5121-5122-5123-5129-5134-5138-5141-5142-5143-5144-5145-5146-5153-5158-5161-5162-5163-5165-5166-5167-5168-5170-5173-5179-5180-5182-5183-5184-5185-5194-5196-5200-5202-5207-5211-5212-5219-5222-5223-5225-5226-5227-5228-5232-5235-5240-5241-5242-5249-5251-5255-5256-5262-5263-5264-5265-5267-5268-5270-5274-5292-5295-5296-5297-5298-5299-5300-5302-5304-5305-5306-5307-5308-5315-5316",
    "4939-4946-4951-4954-4956-4958-4960-4967-4973-4974-4976-4977-4978-4979-4980-4994-5029-5030",
    "5098-5099-5102",
    "5110-5125-5126",
    "5491-5508-5509-5513",
]

for _ni in _BUNDLE_NIS_RESTIC:
    Instance.register("restic", _ni)(Restic)
