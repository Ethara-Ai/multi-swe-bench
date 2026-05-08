from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .joplin import JoplinImageBase, joplin_parse_log, _CHECK_GIT_CHANGES_SH

_NODE_IMAGE = "node:12"
_INTERVAL_NAME = "joplin_4015_to_5864"


class ImageDefaultEra2(Image):

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
        return JoplinImageBase(
            self.pr, self._config, _NODE_IMAGE, _INTERVAL_NAME
        )

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install || true
npx lerna bootstrap --no-ci --ignore-scripts || true

# sqlite3 rebuild from source (arm64 compatible)
for d in packages/*/node_modules/sqlite3; do
  if [ -d "$d" ]; then
    (cd "$d" && node ../node-pre-gyp/bin/node-pre-gyp install --build-from-source 2>&1 || true)
  fi
done

# sharp mock (libvips too old to compile, mock for tests)
for d in packages/*/node_modules/sharp; do
  if [ -d "$d" ]; then
    cat > "$d/index.js" << 'SHARPEOF'
const sharp = (input) => {{
  const inst = {{ resize: () => inst, toBuffer: () => Promise.resolve(Buffer.from('')), toFile: () => Promise.resolve(), metadata: () => Promise.resolve({{width:100,height:100,format:'png'}}), jpeg: () => inst, png: () => inst, rotate: () => inst, flatten: () => inst, trim: () => inst, raw: () => inst, options: {{}} }};
  return inst;
}};
sharp.cache = () => {{}}; sharp.simd = () => {{}}; sharp.concurrency = () => {{}};
module.exports = sharp;
SHARPEOF
  fi
done

# jest global install (for packages without local jest)
JEST_VER=$(node -e "try{{console.log(require('./packages/app-cli/package.json').devDependencies.jest||'27')}}catch(e){{console.log('27')}}")
npm install -g "jest@${{JEST_VER}}" 2>&1 || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
npx lerna run test-ci --stream 2>&1 || true

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git apply --exclude package-lock.json --whitespace=nowarn --reject /home/test.patch || true
rm -f *.rej **/*.rej 2>/dev/null || true
npm install || true
npx lerna bootstrap --no-ci --ignore-scripts || true

for d in packages/*/node_modules/sqlite3; do
  if [ -d "$d" ]; then
    (cd "$d" && node ../node-pre-gyp/bin/node-pre-gyp install --build-from-source 2>&1 || true)
  fi
done

for d in packages/*/node_modules/sharp; do
  if [ -d "$d" ]; then
    cat > "$d/index.js" << 'SHARPEOF'
const sharp = (input) => {{
  const inst = {{ resize: () => inst, toBuffer: () => Promise.resolve(Buffer.from('')), toFile: () => Promise.resolve(), metadata: () => Promise.resolve({{width:100,height:100,format:'png'}}), jpeg: () => inst, png: () => inst, rotate: () => inst, flatten: () => inst, trim: () => inst, raw: () => inst, options: {{}} }};
  return inst;
}};
sharp.cache = () => {{}}; sharp.simd = () => {{}}; sharp.concurrency = () => {{}};
module.exports = sharp;
SHARPEOF
  fi
done

npx lerna run test-ci --stream 2>&1 || true

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git apply --exclude package-lock.json --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
rm -f *.rej **/*.rej 2>/dev/null || true
npm install || true
npx lerna bootstrap --no-ci --ignore-scripts || true

for d in packages/*/node_modules/sqlite3; do
  if [ -d "$d" ]; then
    (cd "$d" && node ../node-pre-gyp/bin/node-pre-gyp install --build-from-source 2>&1 || true)
  fi
done

for d in packages/*/node_modules/sharp; do
  if [ -d "$d" ]; then
    cat > "$d/index.js" << 'SHARPEOF'
const sharp = (input) => {{
  const inst = {{ resize: () => inst, toBuffer: () => Promise.resolve(Buffer.from('')), toFile: () => Promise.resolve(), metadata: () => Promise.resolve({{width:100,height:100,format:'png'}}), jpeg: () => inst, png: () => inst, rotate: () => inst, flatten: () => inst, trim: () => inst, raw: () => inst, options: {{}} }};
  return inst;
}};
sharp.cache = () => {{}}; sharp.simd = () => {{}}; sharp.concurrency = () => {{}};
module.exports = sharp;
SHARPEOF
  fi
done

npx lerna run test-ci --stream 2>&1 || true

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        return """FROM {name}:{tag}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

{clear_env}

""".format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            clear_env=self.clear_env,
        )


@Instance.register("laurent22", _INTERVAL_NAME)
class JoplinEra2(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefaultEra2(self.pr, self._config)

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
        return joplin_parse_log(test_log)
