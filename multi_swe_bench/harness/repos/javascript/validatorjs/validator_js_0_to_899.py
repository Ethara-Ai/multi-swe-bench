import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.javascript.validatorjs.validator_js import (
    ValidatorJsImageBase,
)


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
        # Level 2: per-PR image FROM the shared ValidatorJsImageBase toolchain.
        # dependency() is an *Image* (not a string), so the DockerfileEnhancer
        # returns dockerfile() verbatim -- the clone/checkout + verbatim
        # Image._HARDENING_BLOCK below are kept exactly as written (and pinning
        # BASE_COMMIT here is correct: it is per-PR, not the shared base).
        return ValidatorJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        # Two-level per-PR Dockerfile (mirrors FastfetchImageDefault). The shared
        # toolchain base does NOT clone, so this image clones full history then
        # checks out ${BASE_COMMIT} inline. Because dependency() is an Image, the
        # DockerfileEnhancer returns this Dockerfile verbatim -- the clone +
        # hardening below are kept as written. Image._HARDENING_BLOCK is
        # concatenated raw (not via the f-string) so its ${BASE_COMMIT} /
        # %(refname) tokens stay literal. prepare.sh installs node_modules +
        # builds (network is available at build time, before the hardening
        # strip); node_modules is untracked so the history rewrite leaves it in
        # place for the offline eval runs.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/{file.name}\n"

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail

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
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# installs dependencies and builds so the eval runs don't need network.
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git reset --hard || true

npm install --legacy-peer-deps >/dev/null 2>&1 || true

# devDependencies introduced by the fix patch must be available to the test-only
# stage too (the test patch may import a devDep whose package.json entry lives in
# fix.patch). Apply just package.json from the fix, install, then revert the
# source -- node_modules is untracked so it survives the hardening pass.
if [ -f /home/fix.patch ]; then
    git apply --include=package.json --whitespace=nowarn --ignore-whitespace /home/fix.patch 2>/dev/null || true
    npm install --legacy-peer-deps >/dev/null 2>&1 || true
    git checkout -- package.json 2>/dev/null || true
fi

# QC fix: the mocha version pinned in package.json (^3 / ^5) does not run on
# Node 20, so the eval has no working test runner. Install a Node-20-compatible
# mocha without editing package.json (it is reverted above). node_modules is
# untracked, so this survives the hardening history-rewrite and the offline run.
npm install --no-save --legacy-peer-deps mocha@10 >/dev/null 2>&1 || true

# Durability guard: the test runner is baked in here because the offline eval
# gets no second chance to install it. Fail the build LOUDLY if mocha is not
# actually runnable, so a silently-broken image can never reach QC/eval.
./node_modules/.bin/mocha --version >/dev/null 2>&1 || {{ echo "FATAL: mocha not runnable after install" >&2; exit 1; }}

npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "strip_bundle.sh",
                r"""#!/bin/bash
# QC fix: drop diff sections that touch built/bundled artifacts (index.js,
# validator.js, validator.min.js, lib/**, es/**). These are regenerated by
# `npm run build`, so the source-of-truth fix already lives in src/**. Patching
# the single-line minified validator.min.js is fragile and can abort the whole
# `git apply` -- stripping those hunks up front makes the apply robust.
awk '
/^diff --git / {
  p = $4; sub(/^b\//, "", p);
  drop = (p == "index.js" || p == "validator.js" || p == "validator.min.js" || p ~ /^lib\// || p ~ /^es\//) ? 1 : 0;
}
{ if (!drop) print }
' "$1"
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
./node_modules/.bin/mocha --version >/dev/null 2>&1 || npm install --no-save --legacy-peer-deps mocha@10 >/dev/null 2>&1 || true
./node_modules/.bin/mocha --reporter spec

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
bash /home/strip_bundle.sh /home/test.patch > /home/test.stripped.patch
git apply --whitespace=nowarn --ignore-whitespace --exclude=package-lock.json --exclude=yarn.lock /home/test.stripped.patch
npm install --legacy-peer-deps >/dev/null 2>&1 || true
./node_modules/.bin/mocha --version >/dev/null 2>&1 || npm install --no-save --legacy-peer-deps mocha@10 >/dev/null 2>&1 || true
npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
./node_modules/.bin/mocha --reporter spec

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
bash /home/strip_bundle.sh /home/test.patch > /home/test.stripped.patch
bash /home/strip_bundle.sh /home/fix.patch > /home/fix.stripped.patch
git apply --whitespace=nowarn --ignore-whitespace --exclude=package-lock.json --exclude=yarn.lock /home/test.stripped.patch /home/fix.stripped.patch
npm install --legacy-peer-deps >/dev/null 2>&1 || true
./node_modules/.bin/mocha --version >/dev/null 2>&1 || npm install --no-save --legacy-peer-deps mocha@10 >/dev/null 2>&1 || true
npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
./node_modules/.bin/mocha --reporter spec

""".format(pr=self.pr),
            ),
        ]


@Instance.register("validatorjs", "validator_js_0_to_899")
class ValidatorJs0To899(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        lines = test_log.splitlines()
        current_path = []
        indentation_to_level = {}

        for line in lines:
            line = ansi_escape.sub("", line)

            match = re.match(
                r"^(\s*)(?:([✓✔]|[0-9]+\))\s+)?(.*?)(?:\s+\([0-9]+ms\))?$", line
            )

            if not match or not match.group(3).strip():
                continue

            spaces, status, name = match.groups()
            name = name.strip()
            indent = len(spaces)

            if indent not in indentation_to_level:
                if not indentation_to_level:
                    indentation_to_level[indent] = 0
                else:
                    prev_indents = sorted(
                        [i for i in indentation_to_level.keys() if i < indent]
                    )
                    if prev_indents:
                        closest_indent = prev_indents[-1]
                        indentation_to_level[indent] = (
                            indentation_to_level[closest_indent] + 1
                        )
                    else:
                        indentation_to_level[indent] = 0

            level = indentation_to_level[indent]
            current_path = current_path[:level]
            current_path.append(name)

            if status:
                full_path = ":".join(current_path)
                if status in ("✓", "✔"):
                    passed_tests.add(full_path)
                elif status.endswith(")"):
                    failed_tests.add(full_path)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Route bundled PRs that carry a dash-joined number_interval (the list of
# prs_in_bundle, e.g. "616-617-622-627-630") to this 0..899-era config.
# Instance.create() looks up f"{org}/{number_interval}", so each bundle's
# interval string must be registered against this class.
_NUMBER_INTERVALS = [
    "609-884",
    "884-885",
    "616-617-622-627-630",
    "629-664-684-691-695",
    "637-640-646-647-649-660-667-668-670-671",
    "651-698-700-701-702-708-711-714-716-717-720",
    "656-663-672-674-676-677",
    "679-753-755-758-764",
    "734-735-736-737",
    "738-739",
    "740-815-846-891-895-896-898",
    "741-769-785",
    "742-771-842-843",
    "743-746-751",
    "763-768-774-777-779-782",
    "801-845-848-849-853-856-858-861-862-863-864-870-872",
    "804-825-831-836-839",
    "874-878-879-880-881",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("validatorjs", _interval)(ValidatorJs0To899)
