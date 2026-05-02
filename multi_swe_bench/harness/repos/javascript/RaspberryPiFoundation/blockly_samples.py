import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _extract_plugin_dir(pr: PullRequest) -> str:
    """Extract the plugin directory name from test_patch or fix_patch.

    Each PR in blockly-samples targets a specific plugin under plugins/<name>/.
    We parse the patch diff headers to find which plugin is being modified.
    """
    for patch in [pr.test_patch, pr.fix_patch]:
        if patch:
            match = re.search(r"plugins/([^/]+)/", patch)
            if match:
                return match.group(1)
    return ""


# ---------------------------------------------------------------------------
# Early Era: PRs 350-775 (2020-2021)
# Node 16, lerna ^3.20.2, npm install --legacy-peer-deps
# ---------------------------------------------------------------------------


class ImageBaseEarlyEra(Image):
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
        return "node:16-bookworm"

    def image_tag(self) -> str:
        return "base-early"

    def workdir(self) -> str:
        return "base-early"

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

RUN npm config set legacy-peer-deps true

{code}

{self.clear_env}

"""


class ImageDefaultEarlyEra(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ImageBaseEarlyEra(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        plugin_dir = _extract_plugin_dir(self.pr)
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

npm install --legacy-peer-deps || true

# Fix for PRs with blockly@git://...#develop dependency:
# The git source lacks dist/ folder that @blockly/dev-scripts webpack config expects.
# npm pack a known-good version and extract into the expected dist/ location.
PLUGIN_DIR=$(find plugins/ -maxdepth 1 -mindepth 1 -type d | head -1)
if [ -n "$PLUGIN_DIR" ] && [ -f "$PLUGIN_DIR/package.json" ]; then
  BLOCKLY_DEP=$(node -e "try {{ var p=require('./' + process.argv[1] + '/package.json'); var d=Object.assign({{}}, p.dependencies, p.devDependencies); console.log(d.blockly || ''); }} catch(e) {{ console.log(''); }}" "$PLUGIN_DIR")
  if echo "$BLOCKLY_DEP" | grep -q "git://"; then
    echo "Detected git:// blockly dependency, injecting packaged blockly into dist/"
    cd /home/{pr.repo}
    npm pack blockly@3.20200924.4 2>/dev/null || true
    if [ -f blockly-3.20200924.4.tgz ]; then
      PLUGIN_DIR_ABS=$(readlink -f "$PLUGIN_DIR")
      mkdir -p "$PLUGIN_DIR_ABS/node_modules/blockly/dist"
      tar xzf blockly-3.20200924.4.tgz -C /tmp/
      cp -r /tmp/package/* "$PLUGIN_DIR_ABS/node_modules/blockly/dist/"
      rm -rf /tmp/package blockly-3.20200924.4.tgz
      echo "Blockly dist/ injected successfully"
    fi
  fi
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}/plugins/{plugin_dir}
npm test
""".format(pr=self.pr, plugin_dir=plugin_dir),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude '*/package-lock.json' --exclude '*.png' --exclude '*.gif' --whitespace=nowarn /home/test.patch
npm install --legacy-peer-deps || true
cd /home/{pr.repo}/plugins/{plugin_dir}
npm test

""".format(pr=self.pr, plugin_dir=plugin_dir),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude '*/package-lock.json' --exclude '*.png' --exclude '*.gif' --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --legacy-peer-deps || true
cd /home/{pr.repo}/plugins/{plugin_dir}
npm test

""".format(pr=self.pr, plugin_dir=plugin_dir),
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


# ---------------------------------------------------------------------------
# Main Era: PRs 1037-2648 (2022-2025)
# Node 20, lerna ^4-8 / npm workspaces, npm install
# ---------------------------------------------------------------------------


class ImageBaseMainEra(Image):
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-main"

    def workdir(self) -> str:
        return "base-main"

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

{code}

{self.clear_env}

"""


class ImageDefaultMainEra(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ImageBaseMainEra(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        plugin_dir = _extract_plugin_dir(self.pr)
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

npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}/plugins/{plugin_dir}
NODE_OPTIONS=--openssl-legacy-provider npm test
""".format(pr=self.pr, plugin_dir=plugin_dir),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude '*/package-lock.json' --exclude '*.png' --exclude '*.gif' --whitespace=nowarn /home/test.patch
npm install || true
cd /home/{pr.repo}/plugins/{plugin_dir}
NODE_OPTIONS=--openssl-legacy-provider npm test

""".format(pr=self.pr, plugin_dir=plugin_dir),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude '*/package-lock.json' --exclude '*.png' --exclude '*.gif' --whitespace=nowarn /home/test.patch /home/fix.patch
npm install || true
cd /home/{pr.repo}/plugins/{plugin_dir}
NODE_OPTIONS=--openssl-legacy-provider npm test

""".format(pr=self.pr, plugin_dir=plugin_dir),
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@Instance.register("RaspberryPiFoundation", "blockly-samples")
class BlocklySamples(Instance):
    # PRs < 1037 use early era (Node 16, legacy-peer-deps)
    # PRs >= 1037 use main era (Node 20)
    MAIN_ERA_THRESHOLD = 1037

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number < self.MAIN_ERA_THRESHOLD:
            return ImageDefaultEarlyEra(self.pr, self._config)

        return ImageDefaultMainEra(self.pr, self._config)

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
        """Parse mocha-style test output.

        Mocha outputs hierarchical test results:
          SuiteName
            ✔ test name
            ✔ test name (123ms)
          N passing (Xms)
          N failing
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        lines = test_log.splitlines()

        # Track hierarchical test nesting via indentation
        current_path = []
        indentation_to_level = {}

        for line in lines:
            # Match test lines - including both group headers and test cases
            # Group 1: indentation spaces
            # Group 2: optional status indicator (✔ or number+))
            # Group 3: test name (without timing info)
            match = re.match(
                r"^(\s*)(?:(✓|✔|[0-9]+\))\s+)?(.*?)(?:\s+\([0-9]+ms\))?$", line
            )

            if not match or not match.group(3).strip():
                continue

            spaces, status, name = match.groups()
            name = name.strip()
            indent = len(spaces)

            # Determine the level in the hierarchy based on indentation
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

            # Update the current path based on level
            current_path = current_path[:level]
            current_path.append(name)

            # Only add to passed/failed sets if this is an actual test (has status indicator)
            if status:
                full_path = " > ".join(current_path)
                if status in ("✓", "✔"):
                    passed_tests.add(full_path)
                elif status.endswith(")"):
                    failed_tests.add(full_path)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
