from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log


class AntDesignImageBase_ANT_DESIGN_4756_TO_563(Image):
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
        return "node:6"

    def image_tag(self) -> str:
        return "base-4756_to_563"

    def workdir(self) -> str:
        return "base-4756_to_563"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive

{code}

{self.clear_env}

"""


class AntDesignImageDefault_ANT_DESIGN_4756_TO_563(Image):
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
        return AntDesignImageBase_ANT_DESIGN_4756_TO_563(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install --legacy-peer-deps || true

# Ensure jest is available (some ancient PRs don't have it in devDependencies)
if [ ! -f ./node_modules/.bin/jest ]; then
  echo "jest not found, force-installing jest@18 + babel-jest@18 for compatibility with node:6"
  npm install --no-save jest@18 babel-jest@18 react-test-renderer@15 2>/dev/null || true
fi

# Install typescript-babel-jest if the jest config references it (needed for some PRs)
if grep -q "typescript-babel-jest" package.json 2>/dev/null; then
  echo "Found typescript-babel-jest reference, installing..."
  npm install --no-save typescript-babel-jest 2>/dev/null || true
fi

# Create stub tests/setup.js if it doesn't exist (some ancient PRs lack it)
if [ ! -f tests/setup.js ]; then
  echo "Creating stub tests/setup.js"
  mkdir -p tests
  echo "// stub setup" > tests/setup.js
fi

# Only install jsdom@11 if tests/setup.js does NOT use the old jsdom v9 API (import {{ jsdom }} from 'jsdom')
if grep -q "from 'jsdom'" tests/setup.js 2>/dev/null || grep -q 'from "jsdom"' tests/setup.js 2>/dev/null; then
  echo "Detected old jsdom v9 API in tests/setup.js, skipping jsdom@11 pin"
  npm install --no-save nwsapi@2.2.2 2>/dev/null || true
else
  npm install --no-save --legacy-peer-deps jsdom@11.12.0 jest-environment-jsdom@24.9.0 nwsapi@2.2.2 2>/dev/null || true
fi

# Force cheerio@0.22.0 LAST — npm install above re-resolves enzyme's cheerio@^1.0.0-rc.2 to 1.x
find node_modules -path "*/node_modules/cheerio" -type d -exec rm -rf {{}} + 2>/dev/null
rm -rf node_modules/cheerio node_modules/.package-lock.json 2>/dev/null
npm install --no-save --legacy-peer-deps cheerio@0.22.0 2>/dev/null || true
for nested in $(find node_modules -path "*/node_modules/cheerio/dist" -type d 2>/dev/null); do
  nested_dir=$(dirname "$nested")
  rm -rf "$nested_dir"
  cp -r node_modules/cheerio "$nested_dir"
done
find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {{}} + 2>/dev/null
CHEERIO_VER=$(node -e "try{{console.log(require('cheerio/package.json').version)}}catch(e){{console.log('none')}}" 2>/dev/null)
echo "cheerio version after pin: $CHEERIO_VER"

npm run version || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
./node_modules/.bin/jest --verbose || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
./node_modules/.bin/jest --verbose || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./node_modules/.bin/jest --verbose || true
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("AntDesignImageDefault_ANT_DESIGN_4756_TO_563 dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("ant-design", "ant_design_4756_to_563")
class ANT_DESIGN_4756_TO_563(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_4756_TO_563(self.pr, self._config)

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
        return parse_jest_log(test_log)
