from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log


class AntDesignImageBase_ANT_DESIGN_10890_TO_4765(Image):
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
        return "node:8"

    def image_tag(self) -> str:
        return "base-10890_to_4765"

    def workdir(self) -> str:
        return "base-10890_to_4765"

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


class AntDesignImageDefault_ANT_DESIGN_10890_TO_4765(Image):
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
        return AntDesignImageBase_ANT_DESIGN_10890_TO_4765(self.pr, self.config)

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

# Detect jest version capability: jest >=22 supports JS config, <22 only JSON
JEST_MAJOR=$(node -e "try{{var v=require('./node_modules/jest/package.json').version;console.log(v.split('.')[0])}}catch(e){{console.log('0')}}" 2>/dev/null)

if [ -f .jest.js ]; then
  # .jest.js already exists natively — patch it
  node << 'PATCHEOF'
const fs = require('fs');
try {{
  let c = fs.readFileSync('.jest.js', 'utf8');
  let changed = false;

  if (!c.includes('testURL')) {{
    c = c.replace(
      /module\\.exports\\s*=\\s*\\{{/,
      "module.exports = {{\\n  testURL: 'http://localhost',"
    );
    changed = true;
  }}

  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  for (const m of needed) {{
    if (c.includes('compileModules') && !c.includes("'" + m + "'")) {{
      c = c.replace('const compileModules = [', "const compileModules = [\\n  '" + m + "',");
      changed = true;
    }}
  }}

  if (changed) {{ fs.writeFileSync('.jest.js', c); console.log('Patched .jest.js'); }}
}} catch(e) {{ console.log('Error patching .jest.js: ' + e.message); }}
PATCHEOF
elif [ "$JEST_MAJOR" -ge 22 ] 2>/dev/null; then
  # jest >=22 supports JS config — create .jest.js
  echo "No .jest.js found, creating minimal jest config for jest >=$JEST_MAJOR"
  cat > .jest.js << 'JESTEOF'
const libDir = process.env.LIB_DIR || 'components';
module.exports = {{
  testURL: 'http://localhost',
  setupFiles: ['./tests/setup.js'],
  testPathIgnorePatterns: ['/node_modules/', 'dekko', 'node_modules'],
  transform: {{ '.*': './node_modules/babel-jest' }},
  collectCoverageFrom: [
    'components/**/*.{{js,jsx}}',
  ],
}};
JESTEOF
else
  # jest <22 can only read JSON config — inject testURL into package.json jest block
  echo "Old jest (v$JEST_MAJOR), injecting testURL into package.json"
  node << 'PKGEOF'
const fs = require('fs');
try {{
  const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
  if (pkg.jest && !pkg.jest.testURL) {{
    pkg.jest.testURL = 'http://localhost';
    fs.writeFileSync('package.json', JSON.stringify(pkg, null, 2));
    console.log('Injected testURL into package.json jest config');
  }}
}} catch(e) {{ console.log('Error patching package.json: ' + e.message); }}
PKGEOF
fi
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
if [ -f .jest.js ]; then
  npx jest --config .jest.js --verbose || true
else
  npx jest --verbose || true
fi
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
if [ -f .jest.js ]; then
  npx jest --config .jest.js --verbose || true
else
  npx jest --verbose || true
fi
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
if [ -f .jest.js ]; then
  npx jest --config .jest.js --verbose || true
else
  npx jest --verbose || true
fi
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("AntDesignImageDefault_ANT_DESIGN_10890_TO_4765 dependency must be an Image")
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


@Instance.register("ant-design", "ant_design_10890_to_4765")
class ANT_DESIGN_10890_TO_4765(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_10890_TO_4765(self.pr, self._config)

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
