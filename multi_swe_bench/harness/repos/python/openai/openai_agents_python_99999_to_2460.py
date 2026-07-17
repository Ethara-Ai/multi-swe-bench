import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# openai/openai-agents-python  --  Era 2: PRs #2460 .. #99999
#
# Toolchain (verified in Docker against base commits of PR #2472, #3311):
#   * package manager : uv (astral)            -- unchanged across the era
#   * requires-python : >=3.10                 -- era boundary marker
#   * test framework  : pytest (asyncio_mode=auto, xdist available)
#   * base image      : python:3.11-slim       -- satisfies >=3.10
#
# Era boundary: `requires-python` flips 3.9 -> 3.10 between PR #2456 (3.9) and
# PR #2472 (3.10); the cutoff is fixed at #2460.  Era 1 lives in
# `openai_agents_python_2459_to_1.py`.
#
# Docker-discovered requirements:
#   * apt: git curl wget ca-certificates build-essential linux-libc-dev rclone
#     - linux-libc-dev  -> kernel uapi headers needed to build `evdev`
#                          (pulled transitively via the `pynput` dev dep);
#                          without it `uv sync` aborts the whole environment.
#     - rclone          -> required by tests/sandbox integration tests
#                          (e.g. test_runner_pause_resume).
#   * install: `uv sync --all-extras --all-packages --group dev`
#              (plain `uv sync` omits litellm/voice/sqlalchemy extras and
#               yields ~13 test-collection ImportErrors)
#   * tests need a dummy OPENAI_API_KEY; without it the SDK raises
#     openai.OpenAIError at client construction and most tests error out.
# ---------------------------------------------------------------------------


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
        return "python:3.11-slim"

    def image_tag(self) -> str:
        return "base-99999-to-2460"

    def workdir(self) -> str:
        return "base-99999-to-2460"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + `git gc --prune` HERE, pruning the base to a single PR's
        # base.sha and breaking every other PR in the era with
        # "reference is not a tree". The base keeps full history; the strict
        # anti-reward-hack hardening runs per-PR (see ImageDefault).
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl wget ca-certificates \\
    build-essential gcc g++ python3-dev \\
    linux-libc-dev rclone \\
    && rm -rf /var/lib/apt/lists/*

# uv pinned (not `latest`): the resolver version decides the dependency set, so
# an unpinned uv makes rebuilds non-reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""

# Warm the uv cache at the default branch so each PR layer's `uv sync` only
# resolves the delta. Best-effort: the per-PR sync in prepare.sh is what counts.
RUN uv sync --all-extras --all-packages --group dev || uv sync || true

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
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
git clean -fdx -e .venv
git checkout {self.pr.base.sha}
uv sync --all-extras --all-packages --group dev || uv sync || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
uv run pytest -v
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
uv run pytest -v
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
export OPENAI_API_KEY=sk-fake-key-for-testing
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
uv run pytest -v
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). prepare.sh checks out
        # this PR's base.sha, then the canonical hardening block detaches at that
        # literal sha and strips every other ref/reflog so later commits (the
        # fix) are unreachable.
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


@Instance.register("openai", "openai-agents-python_99999_to_2460")
class OPENAI_AGENTS_PYTHON_99999_TO_2460(Instance):
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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest -v verbose output, e.g.:
        #   tests/test_agent_config.py::test_system_instructions PASSED  [  0%]
        #   tests/test_agent_hooks.py::test_streamed_agent_hooks FAILED  [  2%]
        #   tests/extensions/memory/test_redis_session.py::test_x SKIPPED [ 11%]
        passed_pattern = re.compile(
            r"^(.+?)\s+PASSED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        passed_tests.update(passed_pattern.findall(clean_log))

        skipped_pattern = re.compile(
            r"^(.+?)\s+SKIPPED\s+(?:\[\s*\d+%\s*\]|\[\d+\])", re.MULTILINE
        )
        skipped_tests.update(skipped_pattern.findall(clean_log))

        # Inline verbose failure line: "<nodeid> FAILED [ 2%]"
        failed_inline = re.compile(
            r"^(.+?)\s+FAILED\s+\[\s*\d+%\s*\]", re.MULTILINE
        )
        failed_tests.update(failed_inline.findall(clean_log))

        # Summary section: "FAILED <nodeid> - <reason>" / "ERROR <nodeid>"
        failed_summary = re.compile(
            r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE
        )
        failed_tests.update(failed_summary.findall(clean_log))

        # Dedup: worst result wins
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
# Registered so delivered records (which carry the dash-joined number_interval)
# resolve to this class (PIPELINE §11/§11c). Trimmed to the RESOLVED set
# (delivery-time subset); the era key above still routes the build dataset.
_BUNDLE_NIS_OPENAI_ERA2 = [
    "2472-2507-2508-2509-2510-2512",  # pr-2472 (6 PRs)
    "2481-2482-2483-2484-2485-2486-2496-2497-2498-2499-2500-2501-2502",  # pr-2481 (13 PRs)
    "2521-2522",  # pr-2521 (2 PRs)
    "2526-2527-2529-2530-2532",  # pr-2526 (5 PRs)
    "2538-2539-2547-2548-2549-2552-2553",  # pr-2538 (7 PRs)
    "2555-2556-2559-2560-2561-2563-2564-2565-2566-2567-2568-2569-2570-2571-2572-2575-2576",  # pr-2555 (17 PRs)
    "2578-2579-2581-2582-2584-2585",  # pr-2578 (6 PRs)
    "2587-2589-2591-2592-2593-2595-2596-2597-2598-2600-2605",  # pr-2587 (11 PRs)
    "2594-2606-2608-2609-2610-2611-2612-2613-2614-2615-2616-2619-2620-2621-2623-2626-2627-2632-2634-2635-2639",  # pr-2594 (21 PRs)
    "2622-2629-2640-2641-2642",  # pr-2622 (5 PRs)
    "2651-2653",  # pr-2651 (2 PRs)
    "2654-2850-2883-2885-2899-2900-2901-2902-2903-2904-2910-2918-2920-2925-2930-2931-2935",  # pr-2654 (17 PRs)
    "2655-2656-2660-2662",  # pr-2655 (4 PRs)
    "2663-2665-2666-2667",  # pr-2663 (4 PRs)
    "2668-2674-2681-2682-2684",  # pr-2668 (5 PRs)
    "2670-2700-2708-2710-2718-2719-2721-2724-2725-2726-2728-2730-2731-2737-2738-2743-2751-2757-2758",  # pr-2670 (19 PRs)
    "2676-2691-2694-2696-2697-2703-2704-2705",  # pr-2676 (8 PRs)
    "2687-2688-2747-2877-2891-2892-2893-2894-2895-2896",  # pr-2687 (10 PRs)
    "2706-2744-2759-2761-2762-2763-2765-2768-2769-2770",  # pr-2706 (10 PRs)
    "2709-2711-2713-2714-2716",  # pr-2709 (5 PRs)
    "2715-2771-2772-2773-2774-2781-2782-2786-2787-2791",  # pr-2715 (10 PRs)
    "2792-2793-2795-2799-2800-2801",  # pr-2792 (6 PRs)
    "2813-2814-2815",  # pr-2813 (3 PRs)
    "2818-2819-2820-2821-2822-2827-2828-2843-2844",  # pr-2818 (9 PRs)
    "2845-2847-2851-2853-2854-2860-2861-2864",  # pr-2845 (8 PRs)
    "2948-2950-2953-2956-2963-2965-2974-2975-2978-2979-2980",  # pr-2948 (11 PRs)
    "2972-3026-3027-3028-3031-3038-3039",  # pr-2972 (7 PRs)
    "2976-2981-2982-2984-2985-2986-2987-2988-2989-2996",  # pr-2976 (10 PRs)
    "2998-2999-3000-3005-3006-3007",  # pr-2998 (6 PRs)
    "3013-3014-3015-3016-3021-3022-3023",  # pr-3013 (7 PRs)
    "3019-3094-3132-3147-3150-3151-3152-3154-3157-3160-3161-3162",  # pr-3019 (12 PRs)
    "3047-3048-3049-3050",  # pr-3047 (4 PRs)
    "3053-3057-3059-3060-3061-3062-3063",  # pr-3053 (7 PRs)
    "3058-3064-3071-3072-3073-3075-3078-3080-3082-3083",  # pr-3058 (10 PRs)
    "3076-3077-3081-3084-3085-3088-3090-3092-3095-3097-3098-3099-3100-3101-3102-3107-3111-3114-3118-3127-3128",  # pr-3076 (21 PRs)
    "3117-3148-3153-3163-3164-3165-3166-3167-3172-3173-3175-3176-3179",  # pr-3117 (13 PRs)
    "3129-3131-3134-3135-3136-3140-3141-3149",  # pr-3129 (8 PRs)
    "3177-3185-3190-3191",  # pr-3177 (4 PRs)
    "3187-3188-3193-3194-3199-3201-3202-3205-3207-3210-3211-3213-3214-3215-3216-3217-3223-3224-3227-3229-3230-3234-3237-3243-3245-3247-3248-3249-3253-3254-3261-3272-3276-3278-3279-3281-3283-3285-3287-3289-3290-3292-3293-3294-3295-3296-3297-3298-3299-3303-3305-3307-3309-3314-3316-3318-3320-3326-3328-3331-3339-3340-3341-3342-3343",  # pr-3187 (65 PRs)
    "3311-3312-3350-3351-3352-3355-3360-3362-3366-3368-3370-3371",  # pr-3311 (12 PRs)
]

for _ni in _BUNDLE_NIS_OPENAI_ERA2:
    Instance.register("openai", _ni)(OPENAI_AGENTS_PYTHON_99999_TO_2460)
