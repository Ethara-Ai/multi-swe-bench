import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class IgnitusClientImageBase(Image):
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
        return "node:16-bullseye"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV SKIP_PREFLIGHT_CHECK=true
{code}

{self.clear_env}

"""


class IgnitusClientImageDefault(Image):
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
        return IgnitusClientImageBase(self.pr, self._config)

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

# react-alerts-component was unpublished from npm on 2020-04-25.
# Replace with react-notifications-component (the renamed package) for older commits.
if grep -q 'react-alerts-component' package.json 2>/dev/null; then
    sed -i 's/"react-alerts-component"[^,]*/\"react-notifications-component\": \"^3.0.0\"/g' package.json
fi

# node-sass requires native compilation (node-gyp + python2) which fails on Node 16+.
# Replace with sass (dart-sass), a pure JS drop-in replacement.
if grep -q '"node-sass"' package.json 2>/dev/null; then
    sed -i 's/"node-sass"[^,]*/"sass": "^1.32.0"/g' package.json
fi

npm install --force --legacy-peer-deps

# cheerio@1.2.0 (pulled by enzyme) depends on parse5-parser-stream@7.x which uses
# node: protocol imports incompatible with CRA's Jest module resolver on Node 16.
# Downgrade to rc.10 which uses parse5@6 without node: imports.
npm install cheerio@1.0.0-rc.10 --force --legacy-peer-deps 2>/dev/null || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
export SKIP_PREFLIGHT_CHECK=true
npx react-scripts test --verbose --watchAll=false --env=jsdom --maxWorkers=2 --forceExit --testPathIgnorePatterns='src/App.test' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
export SKIP_PREFLIGHT_CHECK=true
python3 -c "
import re
patch = open('/home/test.patch').read()
sections = re.split(r'(?=^diff --git )', patch, flags=re.MULTILINE)
clean = [s for s in sections if s.strip() and 'Binary files' not in s and 'GIT binary patch' not in s]
open('/home/test_clean.patch','w').write(''.join(clean))
" 2>/dev/null || grep -v "^Binary files" /home/test.patch > /home/test_clean.patch || true
if ! git apply --whitespace=nowarn --exclude='**/package-lock.json' /home/test_clean.patch; then
    echo "Warning: git apply had issues, continuing with partial apply" >&2
fi
npm install --force --legacy-peer-deps
npm install cheerio@1.0.0-rc.10 --force --legacy-peer-deps 2>/dev/null || true
npx react-scripts test --verbose --watchAll=false --env=jsdom --maxWorkers=2 --forceExit --testPathIgnorePatterns='src/App.test' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
export SKIP_PREFLIGHT_CHECK=true
python3 -c "
import re
for src,dst in [('/home/test.patch','/home/test_clean.patch'),('/home/fix.patch','/home/fix_clean.patch')]:
    patch = open(src).read()
    sections = re.split(r'(?=^diff --git )', patch, flags=re.MULTILINE)
    clean = [s for s in sections if s.strip() and 'Binary files' not in s and 'GIT binary patch' not in s]
    open(dst,'w').write(''.join(clean))
" 2>/dev/null || {{ grep -v "^Binary files" /home/test.patch > /home/test_clean.patch || true; grep -v "^Binary files" /home/fix.patch > /home/fix_clean.patch || true; }}
git apply --whitespace=nowarn --exclude='**/package-lock.json' /home/test_clean.patch 2>/dev/null || true
git apply --whitespace=nowarn --exclude='**/package-lock.json' /home/fix_clean.patch 2>/dev/null || true
npm install --force --legacy-peer-deps
npm install cheerio@1.0.0-rc.10 --force --legacy-peer-deps 2>/dev/null || true
npx react-scripts test --verbose --watchAll=false --env=jsdom --maxWorkers=2 --forceExit --testPathIgnorePatterns='src/App.test' 2>&1
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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("Ignitus", "Ignitus-client")
class IgnitusClient(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return IgnitusClientImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes for reliable matching
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        lines = log.split("\n")
        hierarchy = []
        base_indent = -1
        current_suite = ""
        for line in lines:
            clean = ansi_re.sub("", line)
            stripped = clean.strip()
            if not stripped:
                continue

            leading_spaces = len(clean) - len(clean.lstrip(" "))

            # Track current test suite file and reset hierarchy
            suite_match = re.match(r"^(PASS|FAIL)\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?\s*$", stripped)
            if suite_match:
                current_suite = suite_match.group(2).strip()
                hierarchy = []
                base_indent = -1
                continue

            if stripped.startswith(("Test Suites:", "Tests:", "Snapshots:", "Time:", "Ran all")):
                continue

            # Skip Jest error detail lines (stack traces, snapshot diffs, etc.)
            if stripped.startswith("●"):
                continue

            if stripped.startswith(("✓", "✕", "○")):
                match = re.match(r"^[✓✕○]\s+(.*?)(?:\s+\(\d+\s*m?s\))?\s*$", stripped)
                if match:
                    test_name_part = match.group(1).strip()
                    # When no describe hierarchy, prefix with suite path to avoid
                    # collisions from bare it() calls across different test files
                    if hierarchy:
                        full_test_name = " > ".join(hierarchy + [test_name_part])
                    else:
                        full_test_name = f"{current_suite} > {test_name_part}" if current_suite else test_name_part
                    if stripped.startswith("✓"):
                        passed_tests.add(full_test_name)
                    elif stripped.startswith("✕"):
                        failed_tests.add(full_test_name)
                    elif stripped.startswith("○"):
                        skipped_tests.add(full_test_name)
            else:
                group_name = stripped
                if base_indent < 0:
                    base_indent = leading_spaces
                current_level = max(0, (leading_spaces - base_indent) // 2)

                if current_level < len(hierarchy):
                    hierarchy = hierarchy[:current_level]
                    hierarchy.append(group_name)
                elif current_level == len(hierarchy):
                    hierarchy.append(group_name)
                else:
                    while len(hierarchy) < current_level:
                        hierarchy.append("")
                    hierarchy.append(group_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
