import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22.23.5-bookworm"
_BASE_TAG = "base-1903_to_75"
_DIGEST_JS = "mswb_digest.js"
_VITE_ENGINE_JS = "mswb_vite_engine.js"
_EMULATED_MACHINE = "x86_64"

_TEST_FILE_RE = re.compile(
    r"^\+\+\+ b/((?:server|client|cli|shared)/\S+\.test\.[cm]?[jt]sx?)$", re.MULTILINE
)


def _test_files(pr: PullRequest) -> list[str]:
    return sorted(set(_TEST_FILE_RE.findall(pr.test_patch or "")))


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

"""

_DIGEST_JS_BODY = r"""const fs = require('fs');
const path = require('path');

const workspace = process.argv[2];
const workspaceDir = process.argv[3];
const reportPath = process.argv[4];
if (!fs.existsSync(reportPath)) {
  console.log('MSWB_SUITE_ERROR ' + workspace + ': vitest wrote no report');
  process.exit(0);
}
const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
const rank = { PASSED: 0, SKIPPED: 1, FAILED: 2 };
const status = new Map();
for (const file of report.testResults || []) {
  const rel = path.relative(workspaceDir, file.name).split(path.sep).join('/');
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title]
      .map((x) => String(x).replace(/\s+/g, ' ').trim())
      .join(' > ');
    const st = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
    const id = 'vitest::' + workspace + '/' + rel + '::' + title;
    if (!status.has(id) || rank[st] > rank[status.get(id)]) {
      status.set(id, st);
    }
  }
  if (results.length === 0 && file.status === 'failed') {
    console.log('MSWB_SUITE_ERROR ' + workspace + '/' + rel + ': ' + String(file.message || '').split('\n')[0]);
  }
}
for (const id of [...status.keys()].sort()) {
  console.log('MSWB_RESULT ' + status.get(id) + ' ' + id);
}
"""

_VITE_ENGINE_JS_BODY = r"""const fs = require('fs');
const path = require('path');

function find(name, from) {
  let dir = path.resolve(from);
  for (;;) {
    const manifest = path.join(dir, 'node_modules', name, 'package.json');
    if (fs.existsSync(manifest)) {
      return manifest;
    }
    const up = path.dirname(dir);
    if (up === dir) {
      throw new Error('mswb_vite_engine: cannot find ' + name + ' from ' + from);
    }
    dir = up;
  }
}

const vitest = fs.realpathSync(find('vitest', process.cwd()));
const vite = fs.realpathSync(find('vite', path.dirname(vitest)));
const dependencies = JSON.parse(fs.readFileSync(vite, 'utf8')).dependencies || {};
if (!dependencies.rolldown) {
  console.log('rollup');
  process.exit(0);
}
const rolldown = fs.realpathSync(find('rolldown', path.dirname(vite)));
find('@rolldown/binding-linux-' + process.arch + '-gnu', path.dirname(rolldown));
console.log('rolldown');
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

mswb_apply_patch() {
  for patch in "$@"; do
      if git apply --whitespace=nowarn "$patch" 2>/dev/null; then
          echo "apply_patch: $patch applied cleanly"
      else
          echo "apply_patch: $patch needs --3way"
          git apply --3way --whitespace=nowarn "$patch"
          echo "apply_patch: $patch applied via --3way"
      fi
  done
  if git grep -qE '^<<<<<<< ' -- ':!node_modules'; then
      echo "apply_patch: CONFLICT MARKERS present after apply" >&2
      exit 1
  fi
}

mswb_present() {
  MSWB_FILES=()
  local f
  for f in "$@"; do
      if [ -f "$f" ]; then
          MSWB_FILES+=("$f")
      else
          echo "mswb: $f is not in the tree yet"
      fi
  done
}

mswb_vitest_bin() {
  if [ -x "/home/__REPO__/$1/node_modules/.bin/vitest" ]; then
      echo "/home/__REPO__/$1/node_modules/.bin/vitest"
  else
      echo "/home/__REPO__/node_modules/.bin/vitest"
  fi
}

mswb_vitest() {
  mswb_present "$@"
  if [ ${#MSWB_FILES[@]} -eq 0 ]; then
      echo "mswb: none of the test files exist yet"
      return 0
  fi
  local ws f rc out
  for ws in $(printf '%s\n' "${MSWB_FILES[@]}" | cut -d/ -f1 | sort -u); do
      local rel=()
      for f in "${MSWB_FILES[@]}"; do
          case "$f" in
              "$ws"/*) rel+=("${f#"$ws"/}") ;;
          esac
      done
      out="/tmp/mswb-vitest-$ws.json"
      rm -f "$out"
      echo "mswb: vitest in $ws on ${#rel[@]} file(s)"
      rc=0
      (cd "/home/__REPO__/$ws" && "$(mswb_vitest_bin "$ws")" run --pool=forks --no-file-parallelism \
          --reporter=json --outputFile="$out" "${rel[@]}") < /dev/null || rc=$?
      echo "mswb: vitest in $ws exit $rc"
      node /home/__DIGEST_JS__ "$ws" "/home/__REPO__/$ws" "$out"
  done
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__SHA__"

for attempt in 1 2 3; do
    rc=0
    npm ci --no-audit --no-fund || rc=$?
    if [ "$rc" -eq 0 ]; then
        break
    fi
    echo "prepare: npm ci failed with $rc (attempt $attempt)"
    test "$attempt" -lt 3
    sleep 30
done
test "$rc" -eq 0
npm cache clean --force

(cd server && node -e "const Database = require('better-sqlite3'); new Database(':memory:').prepare('select 1 as one').get(); console.log('gate: better-sqlite3 loads')")
if grep -q '"sharp"' server/package.json; then
    (cd server && node -e "require('sharp'); console.log('gate: sharp loads')")
fi
for ws in $(printf '%s\n' __TEST_FILES__ | cut -d/ -f1 | sort -u); do
    test -x "$(mswb_vitest_bin "$ws")"
    (cd "$ws" && node -e "console.log('gate: $ws resolves vitest ' + require('vitest/package.json').version)")
    mswb_present __TEST_FILES__
    rel=()
    for f in "${MSWB_FILES[@]}"; do
        case "$f" in
            "$ws"/*) rel+=("${f#"$ws"/}") ;;
        esac
    done
    engine="$(cd "$ws" && node /home/__VITE_ENGINE_JS__)"
    echo "gate: $ws vitest runs on vite with $engine"
    if [ "$engine" = "rolldown" ] && [ "$(uname -m)" = "__EMULATED_MACHINE__" ]; then
        echo "gate: $ws vitest list skipped, rolldown cannot execute under emulated __EMULATED_MACHINE__"
    elif [ ${#rel[@]} -gt 0 ]; then
        (cd "$ws" && "$(mswb_vitest_bin "$ws")" list "${rel[@]}" > "/tmp/mswb-list-$ws.log")
        echo "gate: $ws lists $(wc -l < "/tmp/mswb-list-$ws.log") test(s)"
    fi
done
echo "gate: dependencies and the PR test files load"
"""

_ACT_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test

cd /home/__REPO__

. /home/prepare.sh

__PATCH_STEP__
mswb_vitest __TEST_FILES__
"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class _FreellmapiImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class Freellmapi1903To75ImageBase(_FreellmapiImage):
    def dependency(self) -> str | Image:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_FETCH_RETRIES=5 \\
    NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

RUN node --version && npm --version && git --version && python3 --version && make --version && g++ --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Freellmapi1903To75ImageDefault(_FreellmapiImage):
    def dependency(self) -> Image:
        return Freellmapi1903To75ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _common_values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__DIGEST_JS__": _DIGEST_JS,
            "__TEST_FILES__": " ".join(_test_files(self.pr)),
        }

    def _prepare(self) -> str:
        values = self._common_values()
        values["__SHA__"] = self.pr.base.sha
        values["__VITE_ENGINE_JS__"] = _VITE_ENGINE_JS
        values["__EMULATED_MACHINE__"] = _EMULATED_MACHINE
        return _render(_PREPARE_SH, values)

    def _act(self, name: str, *patches: str) -> File:
        values = self._common_values()
        values["__PATCH_STEP__"] = (
            f"mswb_apply_patch {' '.join(patches)}" if patches else ""
        )
        return File(".", name, _render(_ACT_SH, values))

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", _DIGEST_JS, _DIGEST_JS_BODY),
            File(".", _VITE_ENGINE_JS, _VITE_ENGINE_JS_BODY),
            File(".", "prepare.sh", self._prepare()),
            self._act("run.sh"),
            self._act("test-run.sh", "/home/test.patch"),
            self._act("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        copy_commands = copy_commands.rstrip("\n")

        base_commit_arg = f'ARG BASE_COMMIT="{self.pr.base.sha}"'
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{base_commit_arg}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(r"^MSWB_RESULT (?P<status>PASSED|FAILED|SKIPPED) (?P<name>vitest::\S.*?)$")


@Instance.register("tashfeenahmed", "freellmapi_1903_to_75")
class FREELLMAPI_1903_TO_75(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Freellmapi1903To75ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            m = _LINE_RE.match(line.rstrip())
            if m:
                found[m.group("status")].add(m.group("name").strip())

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
