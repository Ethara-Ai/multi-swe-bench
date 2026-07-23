import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _target_test_files(patch: str, strip_prefix: str = "") -> list[str]:
    """Return only the real test files a PR's gold test_patch targets.

    Target side (``+++ b/<path>``) so newly-ADDED test modules are included.
    Keeps genuine test files (``tests.py`` / ``test_*.py`` / ``*_test.py`` /
    ``*_tests.py`` under a ``tests``/``test`` dir); drops fixtures/support
    (conftest.py, __init__.py, utils.py) and same-named source files not under a
    tests dir (e.g. autogpt/commands/write_tests.py). Running only these isolates
    the PR's F2P signal from unrelated flaky live/API tests.
    """
    out: list[str] = []
    seen: set[str] = set()
    for f in re.findall(r"^\+\+\+ b/(.+?)\s*$", patch, re.M):
        if f == "/dev/null":
            continue
        base = f.rsplit("/", 1)[-1]
        if not base.endswith(".py"):
            continue
        if base in ("conftest.py", "__init__.py", "utils.py"):
            continue
        is_test = (
            base == "tests.py"
            or base.startswith("test_")
            or base.endswith("_test.py")
            or base.endswith("_tests.py")
        )
        under_tests = any(s in ("tests", "test") for s in f.split("/")[:-1])
        if base == "tests.py" or (is_test and under_tests):
            p = f[len(strip_prefix):] if strip_prefix and f.startswith(strip_prefix) else f
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


# Era-1 layout: `autogpt/` package at repo root (post PR-1380 rename), CWD stays
# /home/AutoGPT so parse_log node names remain "tests/...". Only each PR's own
# gold test files run (see _target_test_files), isolating the F2P signal from
# unrelated flaky live/API tests.
_ERA1_PYTHONPATH = "/home/AutoGPT:/home/AutoGPT/autogpt"

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=""" + _ERA1_PYTHONPATH + """
cd /home/AutoGPT
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files present at baseline stage"; exit 0; fi
python -m pytest $RUN -v --tb=short --continue-on-collection-errors -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""

_APPLY_FN = """apply_patch_lenient() {
    local p="$1"
    if git -C /home/AutoGPT apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
        --exclude='*.ico' --exclude='*.zip' --exclude='*.pdf' --exclude='*.bin' \\
        --exclude='*.wasm' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' \\
        --exclude='*.otf' --exclude='*.eot' --exclude='*.tar.gz' --exclude='*.so' \\
        --exclude='*.dylib' "$p" 2>/dev/null; then
        return 0
    fi
    echo "git apply failed for $p, trying patch -p1 --fuzz=3 fallback..." >&2
    if patch -p1 --forward --fuzz=3 --batch --reject-file=/tmp/$(basename $p).rej < "$p" 2>&1 | tail -50; then
        return 0
    fi
    echo "Warning: $p did not fully apply, continuing anyway" >&2
    return 0
}
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=""" + _ERA1_PYTHONPATH + """
cd /home/AutoGPT
""" + _APPLY_FN + """apply_patch_lenient /home/test.patch
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files after test.patch"; exit 0; fi
python -m pytest $RUN -v --tb=short --continue-on-collection-errors -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=""" + _ERA1_PYTHONPATH + """
cd /home/AutoGPT
""" + _APPLY_FN + """apply_patch_lenient /home/test.patch
apply_patch_lenient /home/fix.patch
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files after patches"; exit 0; fi
python -m pytest $RUN -v --tb=short --continue-on-collection-errors -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""


# PRs 289, 882, 905, 1296, 2665, 2804, 3058, 4987
# Auto-GPT releases 0.2 -> 0.4.6 (April -> July 2023). Repo layout: autogpt/
# package at root, requirements.txt + pyproject.toml. Tests at tests/. Python
# 3.10 matches CI's min-python-version and the pyproject `requires-python`.
#
# Two-class layout: ImageBase pre-installs system deps + clones the repo,
# shared across all 8 PRs in this era. ImageDefault adds patches + scripts;
# prepare.sh does the per-PR checkout + Python install with per-line fallback
# (PR 905 pins `sourcery` which has been yanked from PyPI).


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

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.10-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-era1"

    def workdir(self) -> str:
        return "base-era1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base = self.dependency()
        if isinstance(base, Image):
            base = base.image_full_name()
        # Minimal base: apt deps only. Repo clone deferred to prepare.sh.
        #
        # The leading `# syntax=docker/dockerfile:1.6` directive makes
        # DockerfileEnhancer.enhance() emit this Dockerfile verbatim: it injects
        # the ARG/ENV/LABEL + proxy/CA-cert infra ONLY when that directive is
        # absent. This is how the base opts out of proxy and certificate
        # injection without modifying image.py.
        return f"""# syntax=docker/dockerfile:1.6

FROM {base}

ARG TARGETARCH
ARG REPO_URL="https://github.com/Significant-Gravitas/AutoGPT.git"
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONUNBUFFERED=1
ENV TZ=UTC
ENV LANG=C.UTF-8
LABEL org.opencontainers.image.title="Significant-Gravitas/AutoGPT" \\
      org.opencontainers.image.description="Significant-Gravitas/AutoGPT base image (era1)" \\
      org.opencontainers.image.source="github.com/Significant-Gravitas/AutoGPT" \\
      org.opencontainers.image.authors="ethara.ai"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git build-essential libxml2-dev libxslt1-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip 'setuptools<81' wheel

# Clone at HEAD (no base commit) and pre-install the COMMON deps so per-PR
# images reuse this layer. The authoritative per-commit checkout + install runs
# later in prepare.sh; the `|| true` keeps the base build green even when HEAD's
# layout/requirements drift from the era's pinned commits.
RUN git clone "${{REPO_URL}}" /home/AutoGPT

WORKDIR /home/AutoGPT

RUN pip install --no-cache-dir -r requirements.txt || true

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
        return f"pr-{self.pr.number}-era1"

    def workdir(self) -> str:
        # Instance-dir name. gen_report.collect_report_tasks parses the PR number
        # via int(dir_name[3:]) expecting "pr-<number>", so NO era suffix here
        # (an "-era1" suffix makes int("<n>-era1") raise and the instance is
        # silently skipped -> 0 reports). PR numbers are unique across eras, so
        # "pr-<number>" cannot collide. The era stays on image_tag() above so the
        # per-era images remain distinct/cached.
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
cd /home
BASE_COMMIT="{pr.base.sha}"
if [ ! -d /home/AutoGPT/.git ]; then
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
fi
cd /home/AutoGPT
git reset --hard 2>/dev/null || true
# A sibling build may have reused + hardened (history-pruned) this checkout to a
# different base commit; if the sha we need is gone, re-clone before checkout.
if ! git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null; then
    cd /home && rm -rf /home/AutoGPT
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
    cd /home/AutoGPT
fi
git checkout "${{BASE_COMMIT}}"
# --- Reward-hacking hardening (mirrors image.Image._HARDENING_BLOCK) ---------
# Detach at the base commit and strip every ref, remote, reflog, tag and
# alternate so the evaluated model cannot recover the fix from git history or
# the PR branch. Assertions abort the build (set -e) if anything leaks through.
git checkout --detach "${{BASE_COMMIT}}"
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD;
        git remote remove origin 2>/dev/null || true;
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d;
        git reflog expire --expire=now --all;
        git reflog expire --expire-unreachable=now --all;
        git gc --prune=now --aggressive;
        rm -f .git/objects/info/alternates;
    '
fi
# --- end hardening -----------------------------------------------------------
# setuptools<81 is REQUIRED: setuptools 81 removed the bundled `pkg_resources`,
# which era-1 deps (spacy via tests.integration.agent_factory) import at
# collection time -> pr-2665 fix stage otherwise died with "No module named
# 'pkg_resources'" and produced zero test results.
pip install --no-cache-dir --upgrade pip 'setuptools<81' wheel || true
# Bulk install first (fast). If it fails (e.g. PR 905 pins `sourcery` which has
# been yanked from PyPI), fall back to per-line installs so one bad pin does
# not skip everything below it.
if ! pip install --no-cache-dir -r requirements.txt; then
    while IFS= read -r line; do
        clean=$(echo "$line" | sed 's/#.*//' | xargs)
        [ -z "$clean" ] && continue
        pip install --no-cache-dir "$clean" || true
    done < requirements.txt
fi
# numpy<2 is REQUIRED: spacy 3.5/thinc 8.1 were built against numpy 1.x ABI;
# pip otherwise resolves unpinned `numpy` in requirements.txt to 2.x and
# pytest fails to collect with "numpy.dtype size changed" on import.
pip install --no-cache-dir 'numpy<2' \\
    pytest pytest-mock pytest-asyncio pytest-cov pytest-integration \\
    pytest-recording pytest-xdist asynctest vcrpy \\
    ftfy distro pypdf 'duckduckgo_search<6' || true
# Runtime deps that the era-1 conftest.py imports transitively but that the
# requirements install can miss: auto_gpt_plugin_template (autogpt.config imports
# it -> pr-2665 failed "No module named 'auto_gpt_plugin_template'") and
# sentry_sdk (agent module imports it in later PRs). Without these the patched
# conftest fails to import and pytest collects 0 tests.
pip install --no-cache-dir \\
    'auto-gpt-plugin-template @ git+https://github.com/Significant-Gravitas/Auto-GPT-Plugin-Template@0.1.0' \\
    sentry-sdk || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch)),
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch)),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch)),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_name()
        base_tag = base.image_tag()
        # COPY prepare.sh and RUN it FIRST so the expensive clone+install layer
        # caches independently of the run/test/fix scripts + patches. Editing those
        # then only invalidates the cheap trailing COPYs, not the pip/poetry install.
        other = [f for f in self.files() if f.name != "prepare.sh"]
        copy_rest = "\n".join(f"COPY {f.name} /home/" for f in other)
        return f"""FROM {base_name}:{base_tag}

COPY prepare.sh /home/

RUN bash /home/prepare.sh

{copy_rest}
"""


@Instance.register("Significant-Gravitas", "AutoGPT_4987_to_289")
class AUTOGPT_4987_TO_289(Instance):
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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        pattern = r"(\btests(?:\.py|/[^\s]+?)::[^\s]+?) (PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b"
        for test_name, status in re.findall(pattern, log):
            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Each dataset record carries number_interval = its prs_in_bundle joined by
# "-" (NOT a low-high range). Instance.create() routes on
# f"{org}/{number_interval}" whenever a record sets number_interval, so every
# bundle interval belonging to this era must be registered to AUTOGPT_4987_TO_289
# (in addition to the "AutoGPT_4987_to_289" name on the class above), else create() raises
# "Instance ... is not registered" before any image is built.
_BUNDLE_NUMBER_INTERVALS = [
    "4987-5035-5036-5041-5044-5045-5047-5048-5050-5051-5056-5063-5065-5075-5076-5078-5079-5092-5094-5112",
    "1296-2635-3250-3774-3969-4098-4471-4789-4803-4810-4828-4839-4840-4855-4858-4863-4870-4875-4876-4882-4884-4888-4889-4893-4899-4902-4903-4904-4905-4906-4907-4912-4914-4933-4937",
    "2665-3683",
    "2804-3666-4799-4981-4994-4996-5005-5008-5020-5021-5022-5026-5028-5032-5033-5034-5039-5042",
    "289-1486-2745-3031-3144-3375-3481-3598-3599-3606-3625-3690-3702-3715-3720-3763-3868-3932-3948-3950-3961-3964-4027-4036-4082-4122-4125-4136-4180-4181-4194-4203-4208-4212-4222-4226-4228-4230-4234-4236-4239-4257-4266-4278-4286-4293-4304-4305-4307-4324-4325-4328-4329-4333-4347-4355-4363-4368-4381-4382-4383-4402-4405-4411-4416-4420-4432-4440-4441-4447-4448-4449-4456-4460-4462-4464-4468-4469-4473-4474-4482-4485-4539-4552-4553-4554-4576",
    "3058-3322-3414-3424-3642-3663-3667-3669-3680-3688-3694-3695-3697-3700-3701-3706-3710-3721-3747-3752-3764-3770-3783-3798-3829-3867-3870-3876-3908-3927-3963-3981-3985-3989-3990-3996-3997-3998-4011-4121-4140-4142-4149-4151-4164-4168-4169-4170-4173-4185-4191-4201",
    "882-1569-2486-2594-2821-3570-3696-4167-4260-4345-4481-4486-4488-4498-4548-4561-4563-4565-4567-4573-4581-4585-4591-4592-4596-4601-4602-4610-4613-4616-4620-4622-4623-4628-4630-4632-4637-4638-4639-4640-4645-4647-4648-4649-4652-4653-4655-4657-4658-4660-4661-4662-4664-4666-4670-4672-4673-4680-4683-4700-4703-4704-4705-4706-4707-4711-4714-4716-4719-4721-4729-4730-4736-4737-4738-4741-4745-4747-4748-4756-4761-4778-4786-4802-4812-4815-4816",
    "905-1091-1130-1192-1240-1371-1473-1474-1477-1555-1679-1680-1723-1743-1815-1836-1859-1866-1875-1916-1925-1942-1977-1983-1987-2001-2003-2007-2009-2012-2019-2020-2022-2024-2032-2040-2041-2050-2056-2061-2062-2063-2083-2089-2093-2096-2105-2108-2129-2132-2137-2153-2168-2172-2176-2183-2192-2193-2195-2198-2203-2217-2227-2231-2318-2321-2324-2327-2339-2351-2355-2359-2369-2373-2375-2408-2415-2429-2441-2448-2494-2495-2542-2562-2573-2576-2599-2624-2625",
]
for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("Significant-Gravitas", _ni)(AUTOGPT_4987_TO_289)
