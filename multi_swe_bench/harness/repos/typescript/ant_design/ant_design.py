"""ant-design/ant-design base config — fallback for PRs without number_interval."""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ANSI = re.compile(r"\[[0-9;]*[a-zA-Z]")
_FILE = re.compile(r"^\s*(?:PASS|FAIL)\s+(\S+\.(?:js|jsx|ts|tsx))")
_TEST = re.compile(r"^(\s*)([✓✔√]|[✕✗×])\s+(.+?)$")
_SKIP_N = re.compile(r"^\s*[○◌]\s+skipped\s+(\d+)\s+tests?")
_SKIP_1 = re.compile(r"^(\s*)[○◌]\s+(?:skipped\s+)?(.+?)$")
_DESC = re.compile(r"^(\s{2,})([^\s✓✔√✕✗×○◌].*?)\s*$")
_TIME = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")
_PASS_MARKS = "✓✔√"


def parse_jest_log(test_log: str) -> TestResult:
    """Parse Jest --verbose output into file+describe qualified identities.

    Jest prints one `PASS|FAIL <path>` header per suite, then that suite's
    describe blocks and test lines, nested by indentation. Leaf names repeat
    both across files and across describe blocks within one file (ant-design
    has 12 such collisions in a single run), so an identity is only stable when
    it carries the file AND the describe path -- otherwise distinct tests merge
    into one set entry and appear to vanish between stages.

    Skipped tests are reported only as an aggregate (`○ skipped N tests`) and
    are never named, so they are counted via synthesised placeholders; they
    carry no cross-stage meaning but keep the totals honest.
    """
    passed, failed, skipped = set(), set(), set()
    seen: dict[str, int] = {}
    clean = _ANSI.sub("", test_log)

    cur_file = ""
    stack: list[tuple[int, str]] = []  # (indent, describe title)

    for line in clean.splitlines():
        m = _FILE.match(line)
        if m:
            cur_file = m.group(1)
            stack = []
            continue

        m = _SKIP_N.match(line)
        if m:
            for i in range(int(m.group(1))):
                skipped.add(f"{cur_file}::<skipped {len(skipped) + 1}>")
            continue

        m = _TEST.match(line)
        if m:
            indent, mark, raw = len(m.group(1)), m.group(2), m.group(3)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            path = " > ".join(t for _, t in stack)
            name = _TIME.sub("", raw).strip()
            ident = f"{cur_file}::{path} > {name}" if path else f"{cur_file}::{name}"
            # A suite can legitimately run the same describe+name twice; a bare
            # set would silently merge them and undercount against jest.
            seen[ident] = seen.get(ident, 0) + 1
            if seen[ident] > 1:
                ident = f"{ident} #{seen[ident]}"
            (passed if mark in _PASS_MARKS else failed).add(ident)
            continue

        m = _SKIP_1.match(line)
        if m:
            indent, raw = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            path = " > ".join(t for _, t in stack)
            name = _TIME.sub("", raw).strip()
            skipped.add(f"{cur_file}::{path} > {name}" if path else f"{cur_file}::{name}")
            continue

        m = _DESC.match(line)
        if m and cur_file:
            indent, title = len(m.group(1)), m.group(2).strip()
            if title and not title.startswith(("console.", "at ", "●", "✕", "✓")):
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, title))

    passed -= failed
    skipped -= failed
    skipped -= passed

    # Jest never names tests skipped via a skipped suite -- only the trailing
    # summary knows how many there were. Trust it and top up with placeholders
    # so totals reconcile; skips carry no fail->pass signal either way.
    m = re.search(r"^Tests:.*?(\d+) skipped", clean, re.M)
    if m:
        want = int(m.group(1))
        while len(skipped) < want:
            skipped.add(f"<skipped {len(skipped) + 1}>")

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


class AntDesignImageBase(Image):
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
        return "node:18"

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


class AntDesignImageDefault(Image):
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
        return AntDesignImageBase(self.pr, self.config)

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

npm install || true

# Pin transitive deps to prevent version drift (no lockfile in repo)
npm install --no-save cheerio@1.0.0-rc.10 2>/dev/null || true
npm run version || true

# Patch .jest.js to add missing ESM module transforms
node -e "
const fs = require('fs');
try {{
  let c = fs.readFileSync('.jest.js', 'utf8');
  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  let changed = false;
  for (const m of needed) {{
    if (!c.includes(\"'\" + m + \"'\")) {{
      c = c.replace('const compileModules = [', \"const compileModules = [\\n  '\" + m + \"',\");
      changed = true;
    }}
  }}
  if (changed) {{ fs.writeFileSync('.jest.js', c); console.log('Patched .jest.js ESM modules'); }}
}} catch(e) {{ console.log('No .jest.js to patch'); }}
" || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
npx jest --config .jest.js --no-cache --verbose || true
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
npx jest --config .jest.js --no-cache --verbose || true
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
npx jest --config .jest.js --no-cache --verbose || true
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("AntDesignImageDefault dependency must be an Image")
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


@Instance.register("ant-design", "ant-design")
class AntDesign(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AntDesignImageDefault(self.pr, self._config)

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
