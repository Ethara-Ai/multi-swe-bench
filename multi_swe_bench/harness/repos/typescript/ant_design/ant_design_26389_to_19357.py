"""ant-design/ant-design PRs 19357-26389."""

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import (
    parse_jest_log,
)
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design_dispatcher import (
    AntDesignDispatcher,
)

_PR_LOW, _PR_HIGH = 19357, 26389

_NODE_IMAGE = "node:12"

TEST_COMMAND = "npx jest --config .jest.js --no-cache --verbose"

_PINS_JS = """\
const p = require(process.cwd() + "/package.json");
const all = Object.assign({}, p.dependencies || {}, p.devDependencies || {});
const out = [];
for (const k of Object.keys(all)) {
  const v = all[k];
  if (typeof v === "string" && /^[\\^~]0\\.0\\.0-/.test(v)) {
    out.push(k + "@" + v.replace(/^[\\^~]/, ""));
  }
}
process.stdout.write(out.join(" "));
"""

_VERIFY_JS = """\
const p = require(process.cwd() + "/package.json");
const all = Object.assign({}, p.dependencies || {}, p.devDependencies || {});
const bad = [];
for (const k of Object.keys(all)) {
  const v = all[k];
  if (typeof v === "string" && /^[\\^~]0\\.0\\.0-/.test(v)) {
    const want = v.replace(/^[\\^~]/, "");
    let got = null;
    try {
      got = require(process.cwd() + "/node_modules/" + k + "/package.json").version;
    } catch (e) {}
    if (got !== want) bad.push(k + " want=" + want + " got=" + got);
  }
}
if (bad.length) {
  console.error("PIN VERIFICATION FAILED: " + bad.join("; "));
  process.exit(1);
}
console.log("pins verified: " + Object.keys(all).length + " deps scanned");
"""




class AntDesignImageBase_ANT_DESIGN_26389_TO_19357(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base-26389_to_19357"

    def workdir(self) -> str:
        return "base-26389_to_19357"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        infra = DockerfileEnhancer._infrastructure_block(self, image_name, True)

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{infra}
WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_26389_TO_19357(Image):
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
        return AntDesignImageBase_ANT_DESIGN_26389_TO_19357(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        pins_js = _PINS_JS
        verify_js = _VERIFY_JS
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
                f"""\
#!/bin/bash
set -e

export CI=true

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

npm install --legacy-peer-deps --no-audit --no-fund || true

cat > /tmp/pins.js <<'PINSJS'
{pins_js}
PINSJS
cat > /tmp/verify_pins.js <<'VERIFYJS'
{verify_js}
VERIFYJS

PINS=$(node /tmp/pins.js) || true
npm install --no-save --legacy-peer-deps $PINS cheerio@1.0.0-rc.10 || true
find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {{}} + 2>/dev/null || true
find node_modules -path "*/node_modules/cheerio" -type d 2>/dev/null | while read -r nested; do
  [ "$nested" = "node_modules/cheerio" ] || {{ rm -rf "$nested"; cp -r node_modules/cheerio "$nested"; }}
done || true

node -v
npm -v
test -d node_modules
npx jest --version
node /tmp/verify_pins.js
node -e "require('enzyme')"
node -e "require('cheerio')"
test "$(npx jest --config .jest.js --listTests 2>/dev/null | wc -l)" -gt 0

bash /home/check_git_changes.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
{TEST_COMMAND}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{TEST_COMMAND}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{TEST_COMMAND}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "AntDesignImageDefault_ANT_DESIGN_26389_TO_19357 dependency "
                "must be an Image"
            )

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}"""


@Instance.register("ant-design", "ant-design")
@Instance.register("ant-design", "ant_design_26389_to_19357")
class ANT_DESIGN_26389_TO_19357(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if not (_PR_LOW <= pr.number <= _PR_HIGH):
            return AntDesignDispatcher(pr, config, *args, **kwargs)
        return super().__new__(cls)

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_26389_TO_19357(self.pr, self._config)

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
