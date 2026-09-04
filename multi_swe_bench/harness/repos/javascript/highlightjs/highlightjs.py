import json
import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class highlightjsImageBase(Image):
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
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class highlightjsImageDefault(Image):
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
        return highlightjsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"


    _AUTO_WIRE_NEW_TEST_FILES = """\
for f in $(git ls-files --others --exclude-standard -- 'test/*/*.js' 2>/dev/null); do
  dir=$(dirname "$f")
  base=$(basename "$f" .js)
  idx="$dir/index.js"
  if [ -f "$idx" ] && ! grep -qE "require\\(['\\\"]\\./${base}['\\\"]\\)" "$idx"; then
    printf "\\nrequire('./%s');\\n" "$base" >> "$idx"
  fi
done
"""

    _SPLIT_TEST_RUN = r"""
rm -rf /tmp/mocha_reports /tmp/mocha_shims
mkdir -p /tmp/mocha_reports /tmp/mocha_shims
TARGETS=()
if [ -f test/index.js ]; then
  while IFS= read -r t; do
    [ -n "$t" ] && TARGETS+=("$t")
  done < <(grep -oE "require\(['\"]\./[^'\"]+['\"]\)" test/index.js | sed -E "s/require\(['\"]\.\/([^'\"]+)['\"]\)/\1/")
fi
if [ ${#TARGETS[@]} -eq 0 ]; then
  while IFS= read -r t; do
    [ -n "$t" ] && TARGETS+=("$t")
  done < <(cd test && find . -mindepth 1 -maxdepth 1 \( -name "*.js" -o -type d \) | sed 's#^\./##')
fi

i=0
for t in "${TARGETS[@]}"; do
  i=$((i+1))
  target="test/$t"
  if [ ! -e "$target" ]; then
    target="test/${t}.js"
  fi
  [ -e "$target" ] || continue
  report="/tmp/mocha_reports/${i}.json"
  su nobody -s /bin/bash -c "npx mocha --globals document --no-bail --recursive \"$target\" --reporter json" > "$report" 2>/dev/null || true
  if ! grep -q '"stats"' "$report" 2>/dev/null; then

    recovered=0
    if [ "${ENABLE_FIX_DISCOVERY:-0}" = "1" ] && [ -f /home/fix.patch ] && [ -s /home/fix.patch ]; then

      if git apply --check --whitespace=nowarn /home/fix.patch 2>/dev/null; then
        git apply --whitespace=nowarn /home/fix.patch
        (npm run build || node tools/build.js -t node) >/dev/null 2>&1 || true
        discover_report="/tmp/mocha_reports/${i}_discover.json"
        su nobody -s /bin/bash -c "npx mocha --globals document --no-bail --recursive \"$target\" --reporter json" > "$discover_report" 2>/dev/null || true
        git apply -R --whitespace=nowarn /home/fix.patch
        (npm run build || node tools/build.js -t node) >/dev/null 2>&1 || true
        if grep -q '"stats"' "$discover_report" 2>/dev/null; then
          python3 - "$discover_report" "$report" "$t" <<'PY_EOF'
import json, sys

discover_path, out_path, target_name = sys.argv[1:4]
with open(discover_path) as f:
    data = json.load(f)

def entries(key):
    return data.get(key) or []

reason = (
    f"{target_name}: unavailable in the test.patch-only environment "
    "(requires fix.patch, discovered via post-fix enumeration)"
)
failures = [
    {
        "title": e.get("title"),
        "fullTitle": e.get("fullTitle"),
        "file": e.get("file"),
        "err": {"message": reason},
    }
    for e in entries("passes") + entries("failures")
]
pending = [
    {"title": e.get("title"), "fullTitle": e.get("fullTitle"), "file": e.get("file")}
    for e in entries("pending")
]
out = {
    "stats": {
        "tests": len(failures) + len(pending),
        "passes": 0,
        "failures": len(failures),
        "pending": len(pending),
    },
    "passes": [],
    "failures": failures,
    "pending": pending,
}
with open(out_path, "w") as f:
    json.dump(out, f)
PY_EOF
          recovered=1
        fi
      fi
    fi
    if [ "$recovered" != "1" ]; then

      target_abs="$(pwd)/$target"
      shim="/tmp/mocha_shims/${i}.js"
      cat > "$shim" <<SHIM_EOF
try {
  require("$target_abs");
} catch (e) {
  describe("$t (load error)", function () {
    it("should load without throwing", function () {
      throw e;
    });
  });
}
SHIM_EOF
      su nobody -s /bin/bash -c "npx mocha --globals document --no-bail \"$shim\" --reporter json" > "$report" 2>/dev/null || true
    fi
  fi
  echo "===MOCHA_JSON_START==="
  cat "$report" 2>/dev/null || echo "{}"
  echo "===MOCHA_JSON_END==="
done
"""

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

apt update && apt install -y libxkbfile-dev pkg-config build-essential python3 libkrb5-dev libxss1 xvfb libgtk-3-0 libgbm1

if [ "$(dpkg --print-architecture)" = "amd64" ]; then
    wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list
    apt-get update
    apt-get install -y google-chrome-stable fonts-ipafont-gothic fonts-wqy-zenhei fonts-thai-tlwg fonts-khmeros fonts-kacst fonts-freefont-ttf libxss1 dbus dbus-x11 --no-install-recommends
    rm -rf /var/lib/apt/lists/*
else
    apt-get update
    apt-get install -y chromium fonts-ipafont-gothic fonts-wqy-zenhei fonts-thai-tlwg fonts-khmeros fonts-kacst fonts-freefont-ttf libxss1 dbus dbus-x11 --no-install-recommends
    rm -rf /var/lib/apt/lists/*
fi

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

nvm install || true
nvm use || true
npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd /home/{pr.repo}

nvm use || true
npm install || true
npm run build || node tools/build.js -t node || true
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
{split_test_run}
""".format(pr=self.pr, split_test_run=self._SPLIT_TEST_RUN),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_wiring_fixup}
nvm use || true
npm install || true
npm run build || node tools/build.js -t node || true
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
export ENABLE_FIX_DISCOVERY=1
{split_test_run}

""".format(
                    pr=self.pr,
                    test_wiring_fixup=self._AUTO_WIRE_NEW_TEST_FILES,
                    split_test_run=self._SPLIT_TEST_RUN,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_wiring_fixup}
nvm use || true
npm install || true
npm run build || node tools/build.js -t node || true
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
{split_test_run}

""".format(
                    pr=self.pr,
                    test_wiring_fixup=self._AUTO_WIRE_NEW_TEST_FILES,
                    split_test_run=self._SPLIT_TEST_RUN,
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

        prepare_commands = "RUN bash /home/prepare.sh"
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{proxy_setup}

{copy_commands}

WORKDIR /home/{self.pr.repo}

{prepare_commands}

{Image._HARDENING_BLOCK}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("highlightjs", "highlight.js")
class highlightjs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]: # type: ignore
        return highlightjsImageDefault(self.pr, self._config)

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
        ignore_tests = ["ast-utils", "bin/highlightjs.js"]

        json_blocks = re.findall(
            r"===MOCHA_JSON_START===(.*?)===MOCHA_JSON_END===", test_log, re.DOTALL
        )
        merged: dict[str, list] = {"passes": [], "failures": [], "pending": []}
        found_any = False
        for raw in json_blocks:
            raw = raw.strip()
            anchor = re.search(r'\{\s*"stats"', raw)
            if not anchor:
                continue
            try:
                data, _ = json.JSONDecoder().raw_decode(raw[anchor.start():])
            except json.JSONDecodeError:
                continue
            if data.get("passes") or data.get("failures") or data.get("pending"):
                found_any = True
                merged["passes"].extend(data.get("passes") or [])
                merged["failures"].extend(data.get("failures") or [])
                merged["pending"].extend(data.get("pending") or [])

        if found_any:
            return self._result_from_mocha_json(merged, ignore_tests)

        return self._result_from_mocha_text(test_log, ignore_tests)

    @staticmethod
    def _result_from_mocha_json(data: dict, ignore_tests: list[str]) -> TestResult:
        def key(entry: dict) -> str:
            file = entry.get("file") or ""
            full_title = entry.get("fullTitle") or entry.get("title") or ""
            return f"{file}::{full_title}" if file else full_title

        passed_tests = {key(e) for e in data.get("passes", []) or []}
        failed_tests = {key(e) for e in data.get("failures", []) or []}
        skipped_tests = {key(e) for e in data.get("pending", []) or []}

        for test in list(failed_tests):
            if test in ignore_tests:
                failed_tests.remove(test)

        if failed_tests:
            failed_tests.add("ToTal_Test")
        elif passed_tests or skipped_tests:
            passed_tests.add("ToTal_Test")
        else:
            failed_tests.add("ToTal_Test")
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    @staticmethod
    def _result_from_mocha_text(test_log: str, ignore_tests: list[str]) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        flat_passed_re = re.compile(r"PASS:?\s+([^\(]+)")
        flat_failed_re = re.compile(r"FAIL:?\s+([^\(]+)")
        flat_skipped_re = re.compile(r"SKIP:?\s+([^\(]+)")
        mocha_pass_re = re.compile(r"^[✔✓]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$")
        mocha_fail_x_re = re.compile(r"^[×✗]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$")
        mocha_fail_numbered_re = re.compile(r"^(?!\(node:)\d+\)\s+(.*)$")
        mocha_pending_re = re.compile(r"^-\s+(.*)$")
        mocha_summary_re = re.compile(r"^\d+\s+(?:passing|failing|pending)(?:\s*\(.*\))?$")

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        stack: list[tuple[int, str]] = []

        def qualified_name(name: str) -> str:
            if not stack:
                return name
            return "/".join(entry[1] for entry in stack) + "/" + name

        in_detailed_summary = False
        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line)
            stripped = line.strip()
            if not stripped:
                continue

            for m, target in (
                (flat_passed_re.search(stripped), passed_tests),
                (flat_failed_re.search(stripped), failed_tests),
                (flat_skipped_re.search(stripped), skipped_tests),
            ):
                if m:
                    target.add(m.group(1))

            if in_detailed_summary:
                continue

            if mocha_summary_re.match(stripped):
                if re.match(r"^\d+\s+failing", stripped):
                    in_detailed_summary = True
                continue

            m = mocha_pass_re.match(stripped)
            if m:
                name = qualified_name(m.group(1))
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

            m = mocha_fail_x_re.match(stripped)
            if m:
                name = qualified_name(m.group(1))
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            m = mocha_fail_numbered_re.match(stripped)
            if m:
                name = qualified_name(m.group(1))
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            m = mocha_pending_re.match(stripped)
            if m:
                skipped_tests.add(qualified_name(m.group(1)))
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent == 0:
                continue
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, stripped))

        for test in failed_tests:
            if test in ignore_tests:
                failed_tests.remove(test)
        if failed_tests:
            failed_tests.add("ToTal_Test")
        elif passed_tests or skipped_tests:
            passed_tests.add("ToTal_Test")
        else:
            failed_tests.add("ToTal_Test")
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
