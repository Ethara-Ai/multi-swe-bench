from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .joplin import JoplinImageBase, joplin_parse_log, _CHECK_GIT_CHANGES_SH, _strip_binary_diffs

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
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
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
# Era2 fix: compile TS -> JS before tests (Era1 does `tsc`, Era3 does `yarn run tsc`; Era2 was
# missing it, so compiled modules like packages/lib/testing/test-utils.js never existed and jest
# reported "Test suite failed to run"). --no-bail so an empty package (e.g. @joplin/renderer
# "No tests found") cannot abort the whole suite before the real tests run.
npx lerna run tsc --stream --no-bail 2>&1 || true
npx lerna run test-ci --stream --no-bail 2>&1 || true

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

# Era2 fix: compile TS -> JS before tests (Era1 does `tsc`, Era3 does `yarn run tsc`; Era2 was
# missing it, so compiled modules like packages/lib/testing/test-utils.js never existed and jest
# reported "Test suite failed to run"). --no-bail so an empty package (e.g. @joplin/renderer
# "No tests found") cannot abort the whole suite before the real tests run.
npx lerna run tsc --stream --no-bail 2>&1 || true
npx lerna run test-ci --stream --no-bail 2>&1 || true

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

# Era2 fix: compile TS -> JS before tests (Era1 does `tsc`, Era3 does `yarn run tsc`; Era2 was
# missing it, so compiled modules like packages/lib/testing/test-utils.js never existed and jest
# reported "Test suite failed to run"). --no-bail so an empty package (e.g. @joplin/renderer
# "No tests found") cannot abort the whole suite before the real tests run.
npx lerna run tsc --stream --no-bail 2>&1 || true
npx lerna run test-ci --stream --no-bail 2>&1 || true

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

        # Anti-reward-hack hardening runs in the PR layer (shared base keeps full
        # history). prepare.sh checks out this PR's base.sha; the canonical block then
        # detaches at that literal sha and strips every other ref/reflog so the fix
        # commit is unreachable from git history.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return """# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{repo}

{hardening}

{clear_env}

""".format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            repo=self.pr.repo,
            hardening=hardening,
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


# --- number_interval bundle routing (prs_in_bundle dash-joined) -- PIPELINE 11b
# Delivery stamps number_interval = "-".join(prs_in_bundle); register JoplinEra2
# under every bundle key so delivered records resolve. Era-key registration
# above (_INTERVAL_NAME) is kept for the build-time era routing.
_BUNDLE_NIS_JOPLIN_E2 = [
    "4015-4046-4082-4093-4094-4102-4109-4110",
    "4022-4023-4031-4074",
    "4034-4116-4117-4121",
    "4037-4156-4168-4169",
    "4063-4068",
    "4120-4259-4260-4273-4274-4275-4277-4278",
    "4136-4209-4212-4215",
    "4152-4192-4193-4195-4200-4205-4206",
    "4240-4245",
    "4272-4279-4307-4315-4318-4319-4321",
    "4303-4309-4322-4324",
    "4390-4492-4718-4739-4742-4749-4751-4755-4765-4771-4773-4777-4780-4781-4782-4783-4791-4794-4799-4804-4806-4811-4812-4815-4819-4826-4834-4844-4855-4868-4870-4873-4875-4876-4878",
    "4405-4406-4409-4419-4420-4421-4423-4429-4436-4437-4438-4440-4442-4443-4446-4449-4455-4467-4477-4483-4496-4509-4534-4537-4538-4541-4548-4550-4557-4566-4571-4574-4580-4583-4584-4586-4589-4590-4593-4599-4604-4605-4607-4609-4619-4622-4624-4625-4627-4629-4631-4632-4636-4642-4648-4651-4657-4660-4668-4670-4673-4675-4678-4681-4689-4703-4707-4708-4711-4717-4720-4724-4725-4729-4737-4745-4748-4752",
    "4822-4832-4852-4865-4887-4898-4902-4909",
    "4914-4998-5012-5016-5017-5018-5024-5043-5052-5053-5057-5061",
    "4933-4960-4961-4966-4969-4976-4981-4984-4985-4993",
    "4957-4991-5003-5011-5027-5029-5039",
    "5049-5058-5066",
    "5079-5092-5096",
    "5101-5106-5108",
    "5202-5290-5366-5370-5371-5372",
    "5212-5246-5270-5275",
    "5276-5291-5296",
    "5278-5294-5301-5309-5314",
    "5298-5315-5317-5322-5323-5331-5332-5333-5337-5340-5347-5359",
    "5312-5587-5743-5748-5749-5759-5791-5793-5795-5797-5798-5804-5807-5809-5829",
    "5344-5360-5445-5448-5449-5452-5464-5467-5476-5478",
    "5425-5432",
    "5437-5529-5548-5569-5602-5606-5680-5682-5684-5688-5695-5698-5711-5729-5730-5733-5735-5736-5737",
    "5438-5481-5523-5539",
    "5484-5488",
    "5588-5609-5616-5625-5627-5629-5638-5640",
    "5597-5644",
    "5732-5824-5833-5883-5894-5895-5903-5912-5919-5920",
]
for _ni in _BUNDLE_NIS_JOPLIN_E2:
    Instance.register("laurent22", _ni)(JoplinEra2)
