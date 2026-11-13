
import html
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "zhukunpenglinyutong"
_INTERVAL = "jetbrains_cc_gui_1636_to_1634"
_BASE_IMAGE = "gradle:8.14.3-jdk17"
_REPO_DIR = "jetbrains-cc-gui"
_NODE_VERSION = "22.12.0"

_REPORTS_BEGIN = "===== TEST REPORTS BEGIN ====="
_REPORTS_END = "===== TEST REPORTS END ====="
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

_GRADLE_ENV = r"""export GRADLE_USER_HOME=/opt/gradle-home
mkdir -p "$GRADLE_USER_HOME/init.d"
cat > "$GRADLE_USER_HOME/gradle.properties" <<'PROPEOF'
org.gradle.daemon=false
org.gradle.console=plain
org.gradle.parallel=false
org.gradle.jvmargs=-Xmx2g -XX:+UseParallelGC -Dfile.encoding=UTF-8
org.gradle.java.installations.auto-download=false
systemProp.org.gradle.internal.http.connectionTimeout=120000
systemProp.org.gradle.internal.http.socketTimeout=120000
systemProp.org.gradle.internal.repository.max.retries=10
systemProp.org.gradle.internal.repository.initial.backoff=2000
PROPEOF
cat > "$GRADLE_USER_HOME/init.d/msb-tests.gradle" <<'INITEOF'
allprojects {
    tasks.withType(Test).configureEach {
        ignoreFailures = true
        filter.failOnNoMatchingTests = false
    }
}
INITEOF
"""

_NODE_ENV = r"""export PATH=/opt/node/bin:$PATH
export npm_config_cache=/opt/npm-cache
"""

_RUN_TESTS = r"""cd /home/[[REPO_DIR]]
[[GRADLE_ENV]]
[[NODE_ENV]]
cat > /tmp/msb-node-reporter.mjs <<'REPEOF'
import path from 'node:path';
const stacks = new Map();
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
export default async function* reporter(source) {
  for await (const ev of source) {
    const d = ev.data || {};
    const file = d.file ? path.relative(process.cwd(), d.file) : 'unknown';
    if (ev.type === 'test:start') {
      const st = stacks.get(file) || [];
      st[d.nesting] = d.name;
      st.length = d.nesting + 1;
      stacks.set(file, st);
      continue;
    }
    if (ev.type !== 'test:pass' && ev.type !== 'test:fail') {
      continue;
    }
    if (d.details && d.details.type === 'suite' && ev.type === 'test:pass') {
      continue;
    }
    const st = stacks.get(file) || [];
    const full = st.slice(0, d.nesting).concat([d.name]).join(' > ');
    let body = '';
    if (ev.type === 'test:fail') {
      body = '<failure/>';
    } else if (d.skip !== undefined || d.todo !== undefined) {
      body = '<skipped/>';
    }
    yield '<testcase classname="' + esc(file) + '" name="' + esc(full) + '">' + body + '</testcase>\n';
  }
}
REPEOF
JS_FILES=()
WV_FILES=()
JAVA_FILTERS=""
for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
  [ -f "$f" ] || continue
  case "$f" in
    ai-bridge/*.test.js|ai-bridge/*.test.mjs) JS_FILES+=("$f") ;;
    webview/src/*.test.ts|webview/src/*.test.tsx) WV_FILES+=("${f#webview/}") ;;
    src/test/java/*.java)
      cls="${f#src/test/java/}"
      cls="${cls%.java}"
      JAVA_FILTERS="$JAVA_FILTERS --tests ${cls//\//.}"
      ;;
  esac
done
echo "MSB_JS_FILES=${JS_FILES[*]}"
echo "MSB_WEBVIEW_FILES=${WV_FILES[*]}"
echo "MSB_JAVA_FILTERS=$JAVA_FILTERS"
rm -f /tmp/msb-js.xml /tmp/msb-webview.xml
find . -path '*/build/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true
rc=0
if [ "${#JS_FILES[@]}" -gt 0 ]; then
  timeout -k 30 1200 node --test --test-concurrency=1 --test-reporter=/tmp/msb-node-reporter.mjs --test-reporter-destination=/tmp/msb-js.xml --test-reporter=spec --test-reporter-destination=stdout "${JS_FILES[@]}" || rc=$?
fi
if [ "${#WV_FILES[@]}" -gt 0 ]; then
  (cd webview && timeout -k 30 1800 npx --no-install vitest run --reporter=default --reporter=junit --outputFile.junit=/tmp/msb-webview.xml "${WV_FILES[@]}") || rc=$?
fi
if [ -n "$JAVA_FILTERS" ]; then
  timeout -k 60 3600 ./gradlew --offline --continue -PskipWebview=true :test $JAVA_FILTERS || rc=$?
fi
echo "[[BEGIN]]"
for report in /tmp/msb-js.xml /tmp/msb-webview.xml $(find . -path '*/build/test-results/*' -name 'TEST-*.xml' | sort); do
  [ -f "$report" ] || continue
  sed -e '/<system-out>/,/<\/system-out>/d' -e '/<system-err>/,/<\/system-err>/d' "$report"
done
echo "[[END]]"
exit "$rc"
"""


def _render(template: str) -> str:
    return (
        template.replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[GRADLE_ENV]]", _GRADLE_ENV)
        .replace("[[NODE_ENV]]", _NODE_ENV)
        .replace("[[NODE_VERSION]]", _NODE_VERSION)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class JetbrainsCcGui1636To1634ImageBase(Image):
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


class JetbrainsCcGui1636To1634ImageDefault(Image):
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
        return JetbrainsCcGui1636To1634ImageBase(self.pr, self._config)

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

[[GRADLE_ENV]]
DIST_URL=$(sed -n 's/^distributionUrl=//p' gradle/wrapper/gradle-wrapper.properties | tr -d '\\\\')
DIST_ZIP=$(basename "$DIST_URL")
DIST_HASH=$(python3 -c '
import hashlib, sys
n = int(hashlib.md5(sys.argv[1].encode()).hexdigest(), 16)
d = "0123456789abcdefghijklmnopqrstuvwxyz"
s = ""
while n:
    n, r = divmod(n, 36)
    s = d[r] + s
print(s)
' "$DIST_URL")
DIST_DIR="$GRADLE_USER_HOME/wrapper/dists/${DIST_ZIP%.zip}/$DIST_HASH"
if [ ! -f "$DIST_DIR/$DIST_ZIP.ok" ]; then
  mkdir -p "$DIST_DIR"
  curl -4 --http1.1 -fL --retry 10 --retry-all-errors --connect-timeout 30 --speed-limit 10000 --speed-time 60 -o "$DIST_DIR/$DIST_ZIP" "$DIST_URL"
  (cd "$DIST_DIR" && unzip -q "$DIST_ZIP" && touch "$DIST_ZIP.ok")
fi
if ! command -v zip >/dev/null 2>&1; then
  ok=0
  for i in 1 2 3; do
    if apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends zip; then
      ok=1
      break
    fi
    sleep 20
  done
  [ "$ok" = 1 ] || { echo "prepare: apt install zip failed" >&2; exit 1; }
  rm -rf /var/lib/apt/lists/*
fi
[[NODE_ENV]]
if [ ! -x /opt/node/bin/node ]; then
  case "$(uname -m)" in
    aarch64|arm64) NODE_ARCH=arm64 ;;
    *) NODE_ARCH=x64 ;;
  esac
  NODE_TGZ=/tmp/node.tar.gz
  curl -4 --http1.1 -fL --retry 10 --retry-all-errors --connect-timeout 30 --speed-limit 10000 --speed-time 60 -o "$NODE_TGZ" "https://nodejs.org/dist/v[[NODE_VERSION]]/node-v[[NODE_VERSION]]-linux-$NODE_ARCH.tar.gz"
  mkdir -p /opt/node
  tar -xzf "$NODE_TGZ" -C /opt/node --strip-components=1
  rm -f "$NODE_TGZ"
fi
node --version
for d in ai-bridge webview; do
  ok=0
  for i in 1 2 3; do
    if (cd "$d" && (npm ci --no-audit --no-fund || npm install --no-audit --no-fund)); then
      ok=1
      break
    fi
    sleep 20
  done
  [ "$ok" = 1 ] || { echo "prepare: npm install failed in $d" >&2; exit 1; }
done
git checkout -- ai-bridge/package-lock.json webview/package-lock.json
(cd webview && npm run prebuild)
ok=0
for i in 1 2 3 4 5; do
  if ./gradlew --continue -PskipWebview=true :test --tests MsbWarmupNoSuchTest; then
    ok=1
    break
  fi
  sleep 60
done
[ "$ok" = 1 ] || { echo "prepare: gradle warm-up failed" >&2; exit 1; }
if git apply --whitespace=nowarn --include='build.gradle' --include='settings.gradle' --include='gradle.properties' --include='gradle/*' /home/fix.patch && [ -n "$(git status --porcelain)" ]; then
  ok=0
  for i in 1 2 3 4 5; do
    if ./gradlew --continue -PskipWebview=true :test --tests MsbWarmupNoSuchTest; then
      ok=1
      break
    fi
    sleep 60
  done
  git checkout -- .
  [ "$ok" = 1 ] || { echo "prepare: gradle warm-up with fix build files failed" >&2; exit 1; }
fi
git checkout -- .
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
class JetbrainsCcGui1636To1634(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JetbrainsCcGui1636To1634ImageDefault(self.pr, self._config)

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
        if start == -1 or end == -1 or end < start:
            reports = ""
        else:
            reports = test_log[start + len(_REPORTS_BEGIN) : end]

        for m in _CASE_RE.finditer(reports):
            attrs = dict(_ATTR_RE.findall(m.group(1)))
            name = html.unescape(f"{attrs.get('classname', '')}#{attrs.get('name', '')}")
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
