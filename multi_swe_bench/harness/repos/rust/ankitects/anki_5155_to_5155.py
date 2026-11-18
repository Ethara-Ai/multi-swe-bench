import html
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "ankitects"
_INTERVAL = "anki_5155_to_5155"
_BASE_IMAGE = "rust:1.98.1-bookworm"
_REPO_DIR = "anki"

_BEGIN = "===== MSB TEST REPORTS BEGIN ====="
_END = "===== MSB TEST REPORTS END ====="

_CASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""

_ENV = """export CI=true
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export PATH=/opt/msb-node/bin:$PATH
"""

_TARGETS_FN = r"""msb_targets() {
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
    case "$f" in
      ts/tests/e2e/*) continue ;;
      ts/*.test.ts|ts/*.test.js|ts/*.spec.ts|ts/*.spec.js) ;;
      *) continue ;;
    esac
    [ -f "$f" ] || continue
    echo "${f#ts/}"
  done | sort -u
}
"""

_RETRY_FN = r"""msb_net_retry() {
  local n=0 out rc
  while :; do
    out=$("$@" 2>&1) && rc=0 || rc=$?
    printf '%s\n' "$out"
    [ "$rc" -eq 0 ] && return 0
    n=$((n + 1))
    if [ "$n" -ge 5 ] || ! printf '%s' "$out" | grep -qiE 'could not resolve|timed out|timeout|connection (reset|refused|closed)|network|temporary failure|econnreset|etimedout|eai_again|enotfound|socket hang up|ssl|tls|early eof|unexpected disconnect|50[234]|failed to download|failed to fetch|curl'; then
      return "$rc"
    fi
    sleep $((n * 15))
  done
}
"""

_RUN_SH = r"""#!/bin/bash
set -o pipefail

cd /home/[[REPO_DIR]]
[[ENV]][[TARGETS_FN]]
mapfile -t TARGETS < <(msb_targets)
rm -f /tmp/msb-vitest.xml
rc=0
if [ "${#TARGETS[@]}" -gt 0 ]; then
  (cd ts && ../node_modules/.bin/vitest run --reporter=default --reporter=junit --outputFile.junit=/tmp/msb-vitest.xml "${TARGETS[@]}") 2>&1 || rc=$?
else
  echo "no vitest files from test.patch present"
fi
echo "[[BEGIN]]"
[ -f /tmp/msb-vitest.xml ] && cat /tmp/msb-vitest.xml
echo "[[END]]"
exit "$rc"
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO_DIR]]
[[ENV]][[RETRY_FN]]
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  msb_net_retry git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout [[SHA]]
bash /home/check_git_changes.sh

NODE_VER=""
for f in .nvmrc .node-version; do
  if [ -z "$NODE_VER" ] && [ -f "$f" ]; then NODE_VER=$(tr -d ' v\n' < "$f"); fi
done
if [ -z "$NODE_VER" ] && [ -f build/ninja_gen/src/node.rs ]; then
  NODE_VER=$(grep -oE 'nodejs\.org/dist/v[0-9]+\.[0-9]+\.[0-9]+' build/ninja_gen/src/node.rs | head -1 | sed 's#.*/v##')
fi
[ -n "$NODE_VER" ] || { echo "prepare: cannot determine node version" >&2; exit 1; }
case "$(dpkg --print-architecture)" in
  amd64) NODE_ARCH=x64 ;;
  arm64) NODE_ARCH=arm64 ;;
  *) echo "prepare: unsupported arch" >&2; exit 1 ;;
esac
if [ "$(/opt/msb-node/bin/node --version 2>/dev/null)" != "v$NODE_VER" ]; then
  rm -rf /opt/msb-node /tmp/node.tar.xz
  msb_net_retry curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v$NODE_VER/node-v$NODE_VER-linux-$NODE_ARCH.tar.xz"
  mkdir -p /opt/msb-node
  tar -xJf /tmp/node.tar.xz -C /opt/msb-node --strip-components=1
  rm -f /tmp/node.tar.xz
fi
node --version
corepack enable
msb_net_retry yarn --version
msb_net_retry yarn install --immutable
mkdir -p out

[[TARGETS_FN]]
git apply --whitespace=nowarn /home/test.patch
mapfile -t TARGETS < <(msb_targets)
if [ "${#TARGETS[@]}" -gt 0 ]; then
  (cd ts && ../node_modules/.bin/vitest run --reporter=dot "${TARGETS[@]}") || echo "prepare: vitest warm-up finished with failures (expected before fix)"
fi
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

"""


def _render(template: str) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[TARGETS_FN]]", _TARGETS_FN)
        .replace("[[RETRY_FN]]", _RETRY_FN)
        .replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[BEGIN]]", _BEGIN)
        .replace("[[END]]", _END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Anki5155To5155ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _base_tag(self.pr)

    def workdir(self) -> str:
        return _base_tag(self.pr)

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


class Anki5155To5155ImageDefault(Image):
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
        return Anki5155To5155ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare = (
            _render(_PREPARE_SH)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace(
                "[[REPO_URL]]", f"https://github.com/{self.pr.org}/{self.pr.repo}.git"
            )
        )

        apply_and_run = """#!/bin/bash

cd /home/{repo}
if ! git apply --whitespace=nowarn {patches}; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run.sh

"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", _render(_RUN_SH)),
            File(
                ".",
                "test-run.sh",
                apply_and_run.format(repo=_REPO_DIR, patches="/home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                apply_and_run.format(
                    repo=_REPO_DIR, patches="/home/test.patch /home/fix.patch"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
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


@Instance.register(_ORG, _INTERVAL)
class Anki5155To5155(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Anki5155To5155ImageDefault(self.pr, self._config)

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

        start = test_log.find(_BEGIN)
        end = test_log.rfind(_END)
        if start == -1 or end == -1 or end < start:
            reports = ""
        else:
            reports = test_log[start + len(_BEGIN) : end]

        for m in _CASE_RE.finditer(reports):
            attrs = dict(_ATTR_RE.findall(m.group(1)))
            name = f"{html.unescape(attrs.get('classname', ''))} > {html.unescape(attrs.get('name', ''))}"
            body = m.group(3) or ""
            if "<failure" in body or "<error" in body:
                failed_tests.add(name)
            elif "<skipped" in body:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
