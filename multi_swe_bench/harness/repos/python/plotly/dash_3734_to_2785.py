import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DashBase_3734_2785(Image):
    """Shared base image for this era: apt packages, a full clone of the repo, and
    the era's common third-party pip deps -- everything that does NOT depend on a
    particular PR's commit.

    Built ONCE and reused by every PR image in this era: Image equality/dedup is on
    image_full_name(), and build_dataset walks the dependency chain, so all N PR
    images of an era resolve to this single parent. Deliberately does NO checkout
    and NO hardening -- it holds full history on purpose; the per-PR image checks
    out its own sha and strips the history there.
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
        return "python:3.12-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        # One shared tag per era. image_name() is org_m_repo for all four eras, so
        # the tag is what keeps them apart.
        return "base-3734-2785"

    def workdir(self) -> str:
        return "base-3734-2785"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "base_install.sh",
                """pip install --upgrade pip setuptools wheel || true
pip install "setuptools<70" || true
pip install "Werkzeug<3.1" "Flask>=2.3,<3.1" "pytest>=7,<9" "pytest-mock<4" || true
pip install pyyaml mock six flaky flask-talisman numpy dash-dangerously-set-inner-html selenium || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        # Aligned with multi_swe_bench/harness/image.py -- see the notes on the PR
        # image below. Carries the syntax directive, so DockerfileEnhancer.enhance()
        # returns it unchanged; clones via ${REPO_URL} (passed as a build-arg
        # because dependency() is a string) and declares BASE_COMMIT purely to
        # consume the build-arg build_dataset always sends.
        packages = ['ca-certificates', 'curl', 'build-essential', 'git', 'gnupg', 'make', 'sudo', 'wget', 'libxml2-dev', 'libxslt-dev', 'pkg-config', 'zlib1g-dev']
        template = """# syntax=docker/dockerfile:1.6

# plotly/dash shared base -- Python 3.12 era (bundles with max(prs_in_bundle) in 2785-3734)

FROM python:3.12-slim

ARG TARGETARCH
ARG REPO_URL="__REPO_URL__"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ shared base image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN set -eux; \\
    if ! apt-get update; then \\
        sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list; \\
        sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list; \\
        sed -i '/stretch-updates/d;/buster-updates/d;/jessie-updates/d' /etc/apt/sources.list; \\
        apt-get update; \\
    fi; \\
    apt-get install -y --no-install-recommends \\
__PACKAGES__; \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/__REPO__

COPY base_install.sh /home/base_install.sh
RUN bash /home/base_install.sh || true

CMD ["/bin/bash"]
"""
        return (
            template.replace(
                "__PACKAGES__", " \\\n".join(f"        {p}" for p in packages)
            )
            .replace("__REPO_URL__", f"https://github.com/{self.pr.org}/{self.pr.repo}.git")
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class ImageDefault_3734_2785(Image):
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
        # An Image (not a string) -> this PR image is built FROM the shared era
        # base, so apt, the clone and the common pip deps are paid once per era
        # instead of once per PR.
        return DashBase_3734_2785(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                """ls
###ACTION_DELIMITER###
apt-get update && apt-get install -y nodejs npm curl libxml2-dev libxslt-dev gcc g++ pkg-config zlib1g-dev || true
###ACTION_DELIMITER###
npm install -g n && n lts || true
###ACTION_DELIMITER###
hash -r
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel || true
###ACTION_DELIMITER###
pip install "Werkzeug<3.1" "Flask>=2.3,<3.1" || true
###ACTION_DELIMITER###
pip install -e . || true
###ACTION_DELIMITER###
pip install -r requires-ci.txt 2>/dev/null || pip install -r requirements/ci.txt 2>/dev/null || true
###ACTION_DELIMITER###
pip install -r requires-testing.txt 2>/dev/null || pip install -r requirements/testing.txt 2>/dev/null || true
###ACTION_DELIMITER###
pip install mock six flaky flask-talisman numpy redis dash-dangerously-set-inner-html pytest pytest-mock multiprocess psutil 2>/dev/null || true
###ACTION_DELIMITER###
pip install "pytest>=7,<9" "pytest-mock<4" 2>/dev/null || true
###ACTION_DELIMITER###
cd /home/[[REPO_NAME]] && for comp in dash-test-components dash-generator-test-component-nested dash-generator-test-component-standard dash-generator-test-component-typescript; do if [ -d "@plotly/$comp" ]; then cd "@plotly/$comp" && npm ci 2>/dev/null && npm run build 2>/dev/null && pip install -e . 2>/dev/null; cd /home/[[REPO_NAME]]; fi; done || true
###ACTION_DELIMITER###
###ACTION_DELIMITER###
# Generate the bundled component packages dash/html, dash/dcc and dash/dash_table
# (dash 2.0+ monorepo). Without them pytest aborts at collection with
# "cannot import name 'Div' from 'dash.html' (unknown location)".
#
# Guarded on dash/development/update_components.py, the monorepo marker, so this is
# a clean no-op on dash 0.x/1.x (where components are separate pip packages).
#
# Why not just `npm run build`: the dash build orchestrates the three component
# builds through `lerna exec npm run build`, and update_components.py does
# sys.exit(1) if that returns non-zero -- BEFORE copying the (already-generated)
# python packages into dash/. The lerna path returns non-zero here (a `postbuild`
# es-check es5 gate rejects the newer webpack/terser output, plus lerna concurrency
# flakiness), even though each component builds fine on its own. So we build the
# renderer and each component STANDALONE (with the es-check gate stripped -- it is a
# lint check on the minified bundle, irrelevant to the generated python classes) and
# copy the artifacts into dash/ ourselves, exactly mirroring update_components.py's
# copy loop. node 20 (dash 3.x CI's version) is required -- node 24 from `n lts`
# fails the native gyp build during `npm ci`.
if [ -f dash/development/update_components.py ] && command -v n >/dev/null 2>&1; then \
  n 20 >/dev/null 2>&1 || true; hash -r 2>/dev/null || true; \
  pip install coloredlogs 2>/dev/null || true; \
  pip install -r requirements/dev.txt 2>/dev/null || true; \
  (npm ci || npm install) 2>/dev/null || true; \
  (cd dash/dash-renderer && (npm ci || npm install) 2>/dev/null && npm run build 2>/dev/null) || true; \
  for c in dash-core-components dash-html-components dash-table; do \
    [ -d "components/$c" ] || continue; \
    (cd "components/$c" && npm pkg delete scripts.postbuild 2>/dev/null; (npm ci || npm install) 2>/dev/null; npm run build 2>/dev/null) || true; \
    pyp=$(echo "$c" | tr - _); \
    case "$c" in dash-core-components) dst=dcc;; dash-html-components) dst=html;; *) dst=dash_table;; esac; \
    if [ -d "components/$c/$pyp" ]; then rm -rf "dash/$dst"; cp -r "components/$c/$pyp" "dash/$dst"; fi; \
  done; \
  git checkout -- . 2>/dev/null || true; \
  pip install -e . 2>/dev/null || true; \
fi
echo 'prepare done'""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
pytest tests/ -vv --ignore=tests/integration 2>&1; true

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        base = self.dependency()

        # Aligned with multi_swe_bench/harness/image.py:
        #  * dependency() returns an Image, so DockerfileEnhancer.enhance() returns
        #    this file UNCHANGED ("if not isinstance(dep, str): return raw") -- no
        #    proxy / CA-cert / MITM injection, and no rewriting of the fetch.
        #  * build_dataset only passes the BASE_COMMIT build-arg for STRING
        #    dependencies, so this PR image bakes its own sha as the ARG default.
        #  * embeds Image._HARDENING_BLOCK right after the checkout, so the fix
        #    commit and all later history cannot be read back out of the image.
        # The clone, apt and common pip deps already came from the shared base.
        template = """# syntax=docker/dockerfile:1.6

# plotly/dash PR image -- FROM the shared era base, checked out at this PR's sha

FROM __BASE__

ARG BASE_COMMIT="__BASE_COMMIT__"

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

__HARDENING__

__COPY__

RUN bash /home/prepare.sh || true

CMD ["/bin/bash"]
"""
        return (
            template.replace("__BASE__", base.image_full_name())
            .replace("__HARDENING__", Image._HARDENING_BLOCK.strip("\n"))
            .replace("__COPY__", copy_commands.strip("\n"))
            .replace("__BASE_COMMIT__", self.pr.base.sha)
            .replace("__REPO__", self.pr.repo)
        )


@Instance.register("plotly", "dash_3734_to_2785")
class DASH_3734_TO_2785(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_3734_2785(self.pr, self._config)

    _DEP_ENSURE = 'pip uninstall -y pytest-rerunfailures pytest-sugar 2>/dev/null; pip install "setuptools<70" 2>/dev/null; pip install "Werkzeug<3.1" "Flask>=2.3,<3.1" "pytest>=7,<9" "pytest-mock<4" 2>/dev/null; pip install -e ".[dev]" 2>/dev/null; pip install -e . 2>/dev/null; pip install pyyaml mock six flaky flask-talisman numpy "pytest>=7,<9" "pytest-mock<4" dash-dangerously-set-inner-html selenium 2>/dev/null || true'
    _TEST_CMD = "pytest tests/ -vv --ignore=tests/integration --ignore=tests/test_integration.py 2>&1; true"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return f"bash -c 'cd /home/dash && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return f"bash -c 'cd /home/dash && git apply --whitespace=nowarn /home/test.patch && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return f"bash -c 'cd /home/dash && git apply --whitespace=nowarn /home/test.patch /home/fix.patch && {self._DEP_ENSURE} && {self._TEST_CMD}'"

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        pattern = r"(tests/[^:]+::[^\s]+)\s+(PASSED|FAILED|ERROR|SKIPPED)|(PASSED|FAILED|ERROR|SKIPPED)\s+(tests/[^:]+::[^\s]+)"
        for line in log.splitlines():
            match = re.search(pattern, line)
            if not match:
                continue
            test = match.group(1) or match.group(4)
            status = match.group(2) or match.group(3)
            if not (test and status):
                continue
            if status == "PASSED":
                passed_tests.add(test)
            elif status in ["FAILED", "ERROR"]:
                failed_tests.add(test)
            elif status == "SKIPPED":
                skipped_tests.add(test)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval derivation from `prs_in_bundle` (plotly/dash, all eras)
# ---------------------------------------------------------------------------
# Lives here because this is the last plotly module imported by the package
# __init__, so every era config below is already registered. Nothing outside
# this registry directory is edited.
#
# The raw bundle records carry `prs_in_bundle` (e.g. [101, 204, 305, 409]) but
# the emitted dataset needs the EXPLICIT dash-joined member list
# ("101-204-305-409") — NOT a collapsed range like "101-409", which would
# wrongly imply every PR in between belongs to the bundle.
#
# Two problems have to be solved at once:
#   * `PullRequest.from_json` drops unknown fields, so `prs_in_bundle` never
#     reaches the harness and `number_interval` comes out empty.
#   * `Instance.create()` routes on f"{org}/{number_interval}" whenever that
#     field is non-empty, and for plotly/dash that field carries the ERA CONFIG
#     key from the input jsonl ("dash_443_to_374", "dash_1685_to_1137",
#     "dash_2774_to_362", "dash_3734_to_2785"). Writing the bundle list into the
#     PR before routing would break dispatch.
#
# So: stash at parse time, rewrite at serialization time. Routing itself is not
# touched here — each era config owns its bundles explicitly via the
# _OWNED_INTERVALS block at the bottom of its own module.
#
# Sentinels are checked against each class's OWN __dict__ (not getattr, which
# sees inherited attrs — Dataset subclasses PullRequest and would otherwise
# inherit the from_json sentinel and skip its own patch), so a re-import is a
# no-op.
import json as _json  # noqa: E402

from multi_swe_bench.harness.dataset import Dataset as _Dataset  # noqa: E402

_DASH_ORG = "plotly"
_DASH_REPO = "dash"

# A dash-joined bundle: "1415-1416", "101-204-305-409", or a bare "1415".
_DASH_BUNDLE_RE = re.compile(r"^\d+(-\d+)*$")


def dash_interval_from_bundle(bundle) -> str:
    """Dash-join `prs_in_bundle`, de-duplicated, in the original bundle order."""
    seen = set()
    members = []
    for n in bundle or []:
        if n not in seen:
            seen.add(n)
            members.append(str(n))
    return "-".join(members)


# --- 1. stash prs_in_bundle at parse time (routing untouched) ---------------
if "_plotly_dash_from_json_patch" not in PullRequest.__dict__:
    _dash_orig_pr_from_json = PullRequest.from_json.__func__

    @classmethod
    def _dash_pr_from_json_with_bundle(cls, json_str):
        pr = _dash_orig_pr_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == _DASH_ORG
                and raw.get("repo") == _DASH_REPO
                and raw.get("prs_in_bundle")
            ):
                # stash only; number_interval stays as-is so routing is unaffected
                pr._prs_in_bundle = list(raw["prs_in_bundle"])
        except Exception:
            pass
        return pr

    PullRequest.from_json = _dash_pr_from_json_with_bundle
    PullRequest._plotly_dash_from_json_patch = True


# --- 2. write the dash-joined interval into the emitted dataset -------------
if "_plotly_dash_build_patch" not in _Dataset.__dict__:
    _dash_orig_dataset_build = _Dataset.build.__func__

    @classmethod
    def _dash_dataset_build_with_interval(cls, pr, report):
        ds = _dash_orig_dataset_build(cls, pr, report)
        if getattr(pr, "org", "") == _DASH_ORG and getattr(pr, "repo", "") == _DASH_REPO:
            interval = dash_interval_from_bundle(getattr(pr, "_prs_in_bundle", None))
            if not interval and not _DASH_BUNDLE_RE.match(
                getattr(ds, "number_interval", "") or ""
            ):
                # no bundle metadata: fall back to the single PR number rather
                # than leaking the era-config key into the output record
                interval = str(pr.number)
            if interval:
                ds.number_interval = interval
        return ds

    _Dataset.build = _dash_dataset_build_with_interval
    _Dataset._plotly_dash_build_patch = True


# === number_interval routing: bundles owned by this era config ===============
# Each entry is a dash-joined `prs_in_bundle` from
# Plotly/dataset/plotly__dash_lht_final.jsonl. Instance.create() routes on
# f"{org}/{number_interval}", so a dataset carrying the dash-joined value
# dispatches straight to this config -- no range heuristic, no cross-file table.
#
# Ownership rule: a bundle belongs here when max(prs_in_bundle) falls in
# [2785, 3734] -- the bounds encoded in this module's name -- resolving the
# overlap with the broader era configs by narrowest containing range. That rule
# reproduces all four module names exactly and agrees with each bundle's base
# version (v2.16.0 .. v4.2.0rc0).
_OWNED_INTERVALS = [
    "1548-2841-2899-2985-2987-2988-2995-2999-3000",  # PR #1548, v2.18.0..v2.18.1
    "1906-2927-3068-3077-3089-3108-3127-3149-3159-3168-3171-3190-3222-3279-3294-3296-3298-3300-3303-3304-3305-3315-3318-3319-3321-3322-3323-3328-3333-3334-3341-3344-3345-3347-3353-3359-3361-3365-3367-3369-3371-3373-3379-3380-3382-3383-3392-3395-3396-3397-3403-3406-3407-3409-3415-3422-3423-3424-3426-3445-3460-3465-3466-3483-3485",  # PR #1906, v3.0.0rc1..v3.3.0rc2
    "2362-2760-2787-2795-2806-2816-2817-2819-2822-2823-2826-2831-2832-2833-2844-2845",  # PR #2362, v2.16.1..v2.17.0
    "2779-2783-2784-2785",  # PR #2779, v2.16.0..v2.16.1
    "2789-2881-2888-2892-2893-2896-2898-2900-2903-2908-2909-2913-2915-2922-2923-2930-2936-2952-2956-2957-2962-2976-2980-2981",  # PR #2789, v2.17.1..v2.18.0
    "2842-2847-2854-2856-2859-2860-2867-2873-2876-2879-2883-2884",  # PR #2842, v2.17.0..v2.17.1
    "2926-2939-2991-2994-3009-3011-3012-3025-3028-3034-3039-3040-3043-3046-3051-3059-3061",  # PR #2926, v2.18.1..v2.18.2
    "3113-3241-3248-3249-3251-3255-3256",  # PR #3113, v3.0.1..v3.0.2
    "3227-3232-3233-3237-3239-3240-3242-3244",  # PR #3227, v3.0.0..v3.0.1
    "3254-3257-3259-3263-3264-3265-3268-3271-3273-3274-3275",  # PR #3254, v3.0.2..v3.0.3
    "3278-3280-3281-3282-3284-3287-3289-3290-3291",  # PR #3278, v3.0.3..v3.0.4
    "3351-3352-3354-3355",  # PR #3351, v3.1.0..v3.1.1
    "3398-3414-3432-3433-3440-3444-3447-3448-3453-3459-3467-3468-3469-3472-3477-3481-3484-3495-3515-3516-3527-3530-3532-3536-3537-3543-3544-3547-3554-3561-3562",  # PR #3398, v3.3.0rc0..v4.0.0rc6
    "3430-3482-3488-3490-3496-3500-3503-3505-3511-3520-3522-3534-3540-3541-3542-3545-3548-3555-3556-3559-3563-3564-3566-3568-3575-3576-3581-3583-3584-3585-3589-3595-3600-3601-3603-3607-3609",  # PR #3430, v4.0.0rc0..v4.1.0rc0
    "3523-3570-3622-3626-3627-3629-3637-3640-3641-3643-3647-3656-3658-3660-3665-3668-3672-3680-3683-3685-3688-3690-3709-3718-3724-3730-3734",  # PR #3523, v4.1.0rc0..v4.2.0rc0
]

for _interval in _OWNED_INTERVALS:
    Instance.register("plotly", _interval)(DASH_3734_TO_2785)
