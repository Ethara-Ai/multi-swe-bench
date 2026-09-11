"""ant-design/ant-design interval config for PRs 33611..39681.

Selected via `number_interval` ("ant_design_39681_to_33611"), not via the plain
`repo` name, so it does not disturb any existing ant-design config.

One shared `node:16` base serves the whole range. The neighbouring era configs
split at PR 35706 (node:16 -> node:18), and this interval straddles that
boundary, but the split was never verified as a hard requirement -- jest
jumps from ^27.0.3 to ^29.0.0 across it, and jest 29's own published
`engines.node` is `^14.15.0 || ^16.10.0 || >=18.0.0`, which already covers
node:16. Confirmed directly (not just from the registry metadata) on
2026-09-10: `npm install --legacy-peer-deps` for PR 39681's package.json (the
newest PR, on the jest ^29 side of the old split) completed cleanly under
node:16.20.2 -- 3930 packages, `jest --version` resolved to 29.7.0. So a
single base is correct here, not a shortcut.

Image layout follows the shared-base / per-PR-checkout pattern (matches
`typescript/denoland/deno_2952_to_1281.py`, the most recent config to use it):

  * base image  - one full-history clone, shared by every PR in the interval.
    Deliberately does NOT check out or prune to a single commit;
    `# syntax=docker/dockerfile:1.6` on line 1 stops the enhancer from doing
    that for it.
  * PR image    - checks out its own BASE_COMMIT and runs the npm/jest setup
    in prepare.sh, then applies `Image._HARDENING_BLOCK` so the shipped image
    carries exactly one commit of history.

The jest command, the check_git_changes.sh contract, and the cheerio/jsdom
pins are reused from the node:16 neighbour (`ant_design_35705_to_30656.py`),
applied uniformly since the whole interval now runs on node:16. Two things
are deliberately NOT carried over from that neighbour: `|| true` on the
graded jest invocation itself (it silently turns a runner that failed to
start into an empty 0/0/0 result), and the missing history-scrub block
(the neighbour doesn't have one).
"""

from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log

_ANSI_RE = re.compile(r"\[[0-9;]*[a-zA-Z]")
_SUITE_PATH_RE = re.compile(r"^\s*(?:PASS|FAIL)\s+(\S+\.(?:tsx|jsx|ts|js))\b", re.MULTILINE)


def _restore_truncated_extensions(test_log: str, result: TestResult) -> TestResult:
    real: dict[str, str] = {}
    for path in _SUITE_PATH_RE.findall(_ANSI_RE.sub("", test_log)):
        if path.endswith((".tsx", ".jsx")):
            real[path[:-1]] = path
    if not real:
        return result

    def restore(ident: str) -> str:
        head, sep, tail = ident.partition("::")
        return f"{real[head]}{sep}{tail}" if head in real else ident

    return TestResult(
        passed_count=result.passed_count,
        failed_count=result.failed_count,
        skipped_count=result.skipped_count,
        passed_tests={restore(t) for t in result.passed_tests},
        failed_tests={restore(t) for t in result.failed_tests},
        skipped_tests={restore(t) for t in result.skipped_tests},
    )


_TAG_SUFFIX = "39681_to_33611"
_NODE_IMAGE = "node:16-bullseye"  # bare "node:16" is Debian buster, EOL and off deb.debian.org

_JEST_PATCH_SCRIPT = """\
node << 'PATCHEOF' || true
const fs = require('fs');
try {
  let c = fs.readFileSync('.jest.js', 'utf8');
  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  let changed = false;
  for (const m of needed) {
    if (!c.includes("'" + m + "'")) {
      c = c.replace('const compileModules = [', "const compileModules = [\\n  '" + m + "',");
      changed = true;
    }
  }
  if (changed) { fs.writeFileSync('.jest.js', c); console.log('Patched .jest.js ESM modules'); }
} catch(e) { console.log('No .jest.js to patch'); }
PATCHEOF
"""

_PRE_CLEAN_SCRIPT = """\
find node_modules -path "*/node_modules/cheerio" -type d -exec rm -rf {} + 2>/dev/null
rm -rf node_modules/cheerio node_modules/.package-lock.json 2>/dev/null
"""

# Every pin goes in ONE npm install. Any subsequent `npm install` re-resolves the
# whole tree from package.json and silently undoes earlier --no-save pins: that is
# how cheerio came back as 1.2.0, which depends on undici, which needs a
# TextDecoder global that jest's jsdom environment does not provide -- killing all
# 278 suites of every pre-36296 PR with "ReferenceError: TextDecoder is not defined".
# jsdom is deliberately NOT pinned: package.json resolves ^19.0.0 -> 19.0.0 for the
# jest 27 era and ^20.0.0 -> 20.0.3 for the jest 28/29 era, which are the versions
# each era actually needs.
_PIN_SCRIPT = """\
npm install --no-save --legacy-peer-deps cheerio@1.0.0-rc.10 @ant-design/icons@4.7.0 @ant-design/icons-svg@4.2.1 2>/dev/null || true
"""

_POST_PIN_SCRIPT = """\
for nested in $(find node_modules -path "*/node_modules/cheerio/dist" -type d 2>/dev/null); do
  nested_dir=$(dirname "$nested")
  rm -rf "$nested_dir"
  cp -r node_modules/cheerio "$nested_dir"
done
find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {} + 2>/dev/null
find node_modules -path "*/node_modules/@ant-design/icons-svg" -type d \\
  -not -path "node_modules/@ant-design/icons-svg" -exec rm -rf {} + 2>/dev/null
npm run version || true
"""


class AntDesignImageBase_ANT_DESIGN_39681_TO_33611(Image):
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
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_39681_TO_33611(Image):
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
        return AntDesignImageBase_ANT_DESIGN_39681_TO_33611(self.pr, self.config)

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
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

npm install --legacy-peer-deps || true

# Pin transitive deps to prevent version drift (no lockfile in repo).
# icons-svg 4.3.0+ (Aug 2023) renders fill-rule on every icon path, which no
# snapshot committed in this interval contains; the repo's ^4.7.0 range on the
# parent resolves forward to it, so 4.7.0 is pinned alongside -- it is the only
# parent whose range (^4.2.1) admits icons-svg 4.2.1.
{_PRE_CLEAN_SCRIPT}{_PIN_SCRIPT}{_POST_PIN_SCRIPT}
{_JEST_PATCH_SCRIPT}
test -x node_modules/.bin/jest || {{ echo "prepare.sh: jest was not installed"; exit 1; }}
test -f components/version/version.tsx -o -f components/version/version.ts || {{ echo "prepare.sh: npm run version generated neither components/version/version.tsx nor version.ts"; exit 1; }}
node -e "const v=require('@ant-design/icons-svg/package.json').version; if(!v.startsWith('4.2.')){{console.error('prepare.sh: icons-svg pin failed, got '+v);process.exit(1);}}console.log('icons-svg '+v);"
node -e "try{{const u=require('undici/package.json').version;console.error('prepare.sh: undici '+u+' present - cheerio pin failed, jsdom suites will die on TextDecoder');process.exit(1);}}catch(e){{if(e.code!=='MODULE_NOT_FOUND'){{throw e;}}}}console.log('undici absent');"
node -e "require('./package.json'); console.log('DEPS_OK')"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
npx jest --config .jest.js --no-cache --verbose --maxWorkers=4
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --3way --whitespace=nowarn /home/test.patch
npx jest --config .jest.js --no-cache --verbose --maxWorkers=4
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch
npx jest --config .jest.js --no-cache --verbose --maxWorkers=4
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "AntDesignImageDefault_ANT_DESIGN_39681_TO_33611 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("ant-design", "ant_design_39681_to_33611")
class ANT_DESIGN_39681_TO_33611(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_39681_TO_33611(self.pr, self._config)

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
        return _restore_truncated_extensions(test_log, parse_jest_log(test_log))
