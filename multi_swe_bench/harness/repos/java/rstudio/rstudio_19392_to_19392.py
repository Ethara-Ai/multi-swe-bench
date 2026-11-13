import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "rstudio"
_INTERVAL = "rstudio_19392_to_19392"
_BASE_IMAGE = "node:24.22.1-bookworm"
_REPO_DIR = "rstudio"

_REPORTS_BEGIN = "===== TEST REPORTS BEGIN ====="
_REPORTS_END = "===== TEST REPORTS END ====="

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

_NODE_ENV = r"""export DESKTOP_DIR=/home/[[REPO_DIR]]/src/node/desktop
export ELECTRON_CACHE=/opt/electron-cache
export electron_config_cache=/opt/electron-cache
export npm_config_cache=/opt/npm-cache
export CI=1
"""

_RUN_TESTS = r"""cd /home/[[REPO_DIR]]
[[NODE_ENV]]
SPECS=()
for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
  case "$f" in
    src/node/desktop/test/unit/*.test.ts) ;;
    *) continue ;;
  esac
  [ -f "$f" ] && SPECS+=("${f#src/node/desktop/}")
done
echo "MSB_TEST_SPECS=${SPECS[*]}"
cd "$DESKTOP_DIR"
rm -f /tmp/msb-mocha.json
rc=0
if [ "${#SPECS[@]}" -gt 0 ]; then
  timeout -k 60 1800 xvfb-run -a -s "-screen 0 1280x1024x24" npx --no-install electron-mocha --no-sandbox --full-trace --extension ts --require ts-node/register --slow 750 --reporter json --reporter-option output=/tmp/msb-mocha.json "${SPECS[@]}" < /dev/null || rc=$?
fi
echo "[[BEGIN]]"
[ -f /tmp/msb-mocha.json ] && cat /tmp/msb-mocha.json
echo
echo "[[END]]"
exit "$rc"
"""


def _render(template: str) -> str:
    return (
        template.replace("[[NODE_ENV]]", _NODE_ENV)
        .replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Rstudio19392To19392ImageBase(Image):
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


class Rstudio19392To19392ImageDefault(Image):
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
        return Rstudio19392To19392ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare = _render(
            """#!/bin/bash
set -e

cd /home/[[REPO_DIR]]
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout [[SHA]]
bash /home/check_git_changes.sh

export DEBIAN_FRONTEND=noninteractive
ok=0
for i in 1 2 3; do
  if apt-get update && apt-get install -y --no-install-recommends xvfb xauth libgtk-3-0 libnss3 libxss1 libasound2 libgbm1 libxshmfence1 libdrm2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libpango-1.0-0 libcairo2 libatspi2.0-0 libdbus-1-3 libexpat1 libx11-xcb1; then
    ok=1
    break
  fi
  sleep 20
done
[ "$ok" = 1 ] || { echo "prepare: apt install failed" >&2; exit 1; }
rm -rf /var/lib/apt/lists/*

[[NODE_ENV]]
mkdir -p "$ELECTRON_CACHE" "$npm_config_cache"
cd "$DESKTOP_DIR"
ok=0
for i in 1 2 3 4 5; do
  if npm ci --no-audit --no-fund; then
    ok=1
    break
  fi
  rm -rf node_modules
  sleep 30
done
[ "$ok" = 1 ] || { echo "prepare: npm ci failed" >&2; exit 1; }
timeout -k 60 1800 xvfb-run -a -s "-screen 0 1280x1024x24" npx --no-install electron-mocha --no-sandbox --extension ts --require ts-node/register --reporter json --reporter-option output=/tmp/msb-warmup.json test/unit/main/context-menu.test.ts < /dev/null || true
python3 -c 'import json; d = json.load(open("/tmp/msb-warmup.json")); s = d["stats"]; print("warm-up stats", s); assert s["tests"] > 0' || { echo "prepare: electron-mocha warm-up produced no results" >&2; exit 1; }
rm -f /tmp/msb-warmup.json
cd /home/[[REPO_DIR]]
git status --porcelain
bash /home/check_git_changes.sh

"""
        ).replace("[[SHA]]", self.pr.base.sha).replace("[[REPO_URL]]", f"https://github.com/{self.pr.org}/{self.pr.repo}.git")

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
            File(".", "run.sh", "#!/bin/bash\n\n" + _render(_RUN_TESTS)),
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
class Rstudio19392To19392(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Rstudio19392To19392ImageDefault(self.pr, self._config)

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

        start = test_log.find(_REPORTS_BEGIN)
        end = test_log.rfind(_REPORTS_END)
        data = {}
        if start != -1 and end != -1 and end > start:
            body = test_log[start + len(_REPORTS_BEGIN) : end].strip()
            try:
                data = json.loads(body) if body else {}
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}

        def _name(test: dict) -> str:
            path = test.get("file", "") or ""
            idx = path.find("test/unit/")
            path = path[idx:] if idx != -1 else path
            return f"{path}::{test.get('fullTitle', '')}"

        for test in data.get("passes", []) or []:
            passed_tests.add(_name(test))
        for test in data.get("failures", []) or []:
            failed_tests.add(_name(test))
        for test in data.get("pending", []) or []:
            skipped_tests.add(_name(test))

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
