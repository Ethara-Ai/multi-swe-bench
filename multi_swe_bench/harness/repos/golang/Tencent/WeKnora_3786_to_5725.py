import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "Tencent"
_INTERVAL = "WeKnora_3786_to_5725"
_GO_IMAGE = "golang:1.26.3-bookworm"
_BASE_TAG = "base-3786_to_5725"
_NODE_VERSION = "24.21.0"
_MODULE = "github.com/Tencent/WeKnora"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi
echo "check_git_changes: No uncommitted changes"
exit 0
"""

_ENV = """export CI=true
export GOTOOLCHAIN=local
export CGO_ENABLED=1
export NODE_OPTIONS=--max-old-space-size=4096
export MSWB_TAGS=$(grep -qE 'go (test|vet) -tags sqlite_fts5' .github/workflows/app.yml 2>/dev/null && echo sqlite_fts5 || true)"""

_NODE_REPORTER_MJS = r"""import path from "node:path";

const ROOT = process.cwd();
const stack = [];

function rel(file) {
  if (!file) return "unknown";
  const p = file.startsWith("file://") ? new URL(file).pathname : file;
  return path.relative(path.dirname(ROOT), p);
}

function clean(name) {
  return String(name).split(/\s+/).join(" ").replace(/\d{6,}/g, "<N>");
}

export default async function* reporter(source) {
  for await (const event of source) {
    const d = event.data || {};
    if (event.type === "test:start") {
      stack[d.nesting] = clean(d.name);
      stack.length = d.nesting + 1;
    } else if (event.type === "test:pass" || event.type === "test:fail") {
      if (d.details && d.details.type === "suite") continue;
      const parts = stack.slice(0, d.nesting);
      parts.push(clean(d.name));
      let status = event.type === "test:pass" ? "PASS" : "FAIL";
      if (event.type === "test:pass" && (d.skip !== undefined || d.todo !== undefined)) status = "SKIP";
      yield `MSWB_NODE ${status} ${rel(d.file)}::${parts.join(" > ")}\n`;
      if (event.type === "test:fail" && d.details && d.details.error) {
        yield `${String(d.details.error.message || d.details.error).split("\n").slice(0, 5).join("\n")}\n`;
      }
    } else if (event.type === "test:stdout" || event.type === "test:stderr") {
      yield d.message || "";
    }
  }
}
"""

_GO_REPORT_PY = r"""import json
import re
import sys

PKG = sys.argv[1]
WANT = set(sys.argv[2:])
STATUS = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
seen = set()
running = set()

def name(t):
    return re.sub(r"\d{6,}", "<N>", " ".join(str(t).split()))

for raw in sys.stdin:
    try:
        e = json.loads(raw)
    except ValueError:
        sys.stdout.write(raw)
        continue
    action = e.get("Action")
    if action in ("output", "build-output"):
        sys.stdout.write(e.get("Output") or "")
        continue
    if action == "build-fail":
        print("MSWB_GO_BUILD_FAIL " + PKG)
        continue
    test = name(e["Test"]) if e.get("Test") else ""
    if not test:
        if action in STATUS:
            print(f"MSWB_GO_PKG {STATUS[action]} {PKG}")
        continue
    if action == "run":
        running.add(test)
    elif action in STATUS:
        running.discard(test)
        seen.add(test)
        print(f"MSWB_GO {STATUS[action]} {PKG}::{test}")

for t in sorted(running | (WANT - seen)):
    print(f"MSWB_GO FAIL {PKG}::{t}")
"""

_RUN_TESTS_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
[[ENV]]
[[APPLY]]
cat > /tmp/mswb_go_report.py <<'MSWBPY'
[[REPORT]]
MSWBPY
cat > /tmp/mswb_node_reporter.mjs <<'MSWBJS'
[[NODEREPORT]]
MSWBJS
files=$(awk '/^diff --git a\//{sub(/^b\//, "", $4); print $4}' /home/test.patch | sed -n '/_test\.go$/p' | sort -u)
plain='s/^func (Test[A-Za-z0-9_]*)\([A-Za-z_][A-Za-z0-9_]* \*testing\.T\).*/\1/p'
suite=yes
for f in $(git ls-tree --name-only HEAD -- $files); do [ -z "$(git show "HEAD:$f" | sed -nE "$plain")" ] || suite=no; done
: > /tmp/mswb-sel.txt
for d in $(for f in $files; do dirname "$f"; done | sort -u); do
  [ -d "$d" ] || continue
  compiled=" $(go list -tags "$MSWB_TAGS" -e -f '{{join .TestGoFiles " "}} {{join .XTestGoFiles " "}}' "./$d") "
  names=""; snames=""
  for f in $files; do
    [ "$(dirname "$f")" = "$d" ] && [ -f "$f" ] && [[ "$compiled" == *" $(basename "$f") "* ]] || continue
    names="$names $(sed -nE "$plain" "$f" | tr '\n' ' ')"
    if [ "$suite" = yes ]; then snames="$snames $(sed -nE 's/^func \([A-Za-z_][A-Za-z0-9_]* \*?([A-Za-z0-9_]+)\) (Test[A-Za-z0-9_]*)\(\).*/\1:\2/p' "$f" | tr '\n' ' ')"; fi
  done
  names=$(echo $names | tr ' ' '\n' | sort -u | tr '\n' ' ')
  [ -n "${names// /}" ] && echo "$d|$names" >> /tmp/mswb-sel.txt
  for m in $(echo $snames | tr ' ' '\n' | sort -u); do r=$(awk -v t="${m%%:*}" '/^func Test[A-Za-z0-9_]*\(t \*testing\.T\)/{fn=substr($2,1,index($2,"(")-1)} index($0,"suite.Run(t, &" t "{})")||index($0,"suite.Run(t, new(" t "))"){print fn; exit}' "$d"/*_test.go); [ -n "$r" ] && echo "$d|$r/${m#*:}" >> /tmp/mswb-sel.txt; done
done
cat /tmp/mswb-sel.txt
fe=$(awk '/^diff --git a\//{sub(/^b\//, "", $4); print $4}' /home/test.patch | sed -n 's#^frontend/##; /\.test\.ts$/p' | sort -u)
fe_have=""
for f in $fe; do [ -f "frontend/$f" ] && fe_have="$fe_have $f"; done
echo "frontend tests:$fe_have"
test -s /tmp/mswb-sel.txt || test -n "${fe_have// /}"
rc=0
while IFS='|' read -r d names; do
  pat="^($(echo $names | tr ' ' '|'))\$"; [[ "$names" == */* ]] && pat="^${names%%/*}\$/^${names#*/}\$"
  go test -tags "$MSWB_TAGS" -count=1 -timeout=60m -parallel=4 -json -run "$pat" "./$d" 2>&1 < /dev/null \
    | python3 /tmp/mswb_go_report.py "$d" $names || rc=1
done < /tmp/mswb-sel.txt
for f in $fe; do
  if [ -f "frontend/$f" ]; then
    (cd frontend && node --import tsx --test --test-concurrency=1 --test-timeout=600000 --test-reporter=/tmp/mswb_node_reporter.mjs --test-reporter-destination=stdout "$f") < /dev/null || rc=1
  fi
done
exit "$rc"
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
[[ENV]]
retry() { for n in 1 2 3; do "$@" && return 0; echo "prepare: attempt $n failed: $*" >&2; sleep 15; done; return 1; }
git config --global --add safe.directory '*'
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
test "$(git rev-parse HEAD)" = "[[SHA]]"
bash /home/check_git_changes.sh
export MSWB_TAGS=$(grep -qE 'go (test|vet) -tags sqlite_fts5' .github/workflows/app.yml 2>/dev/null && echo sqlite_fts5 || true)

test "$(head -n1 go.mod)" = "module [[MODULE]]"
retry go mod download

go version
test "$(go env GOTOOLCHAIN) $(go env CGO_ENABLED)" = "local 1"
python3 -c 'import json, sys'
go list -tags "$MSWB_TAGS" ./... | grep -v '/docreader/' > /home/go-packages.txt
test -s /home/go-packages.txt
go build -tags "$MSWB_TAGS" $(go list -tags "$MSWB_TAGS" -f '{{if .GoFiles}}{{.ImportPath}}{{end}}' $(cat /home/go-packages.txt))
go test -tags "$MSWB_TAGS" -count=1 -run '^$' -exec /bin/true $(cat /home/go-packages.txt) > /dev/null
test -f frontend/package-lock.json
(cd frontend && retry npm ci --no-audit --no-fund)
git checkout -- .
bash /home/check_git_changes.sh
node --version | grep -qx "v[[NODE]]"
(cd frontend && node --import tsx -e 'require("@vue/compiler-sfc"); require("typescript"); require("vue")')
echo "DEPS_OK $(go version) node $(node --version) tags=${MSWB_TAGS:-none}"
"""


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[REPORT]]", _GO_REPORT_PY.rstrip("\n"))
        .replace("[[NODEREPORT]]", _NODE_REPORTER_MJS.rstrip("\n"))
        .replace("[[NODE]]", _NODE_VERSION)
        .replace("[[ENV]]", _ENV)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[MODULE]]", _MODULE)
    )


class Weknora3786To5725ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _GO_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo} && git -C /home/{repo} rev-parse HEAD > /dev/null'
        else:
            code = f"COPY {repo} /home/{repo}"
        global_block = f"\n{self.global_env}\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""
        return f"""# syntax=docker/dockerfile:1.6

FROM {_GO_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{repo}.git"
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
    LC_ALL=C.UTF-8 \\
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
    GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{global_block}
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl python3 build-essential xz-utils libsqlite3-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN set -e && case "$(dpkg --print-architecture)" in amd64) narch=x64 ;; arm64) narch=arm64 ;; *) exit 1 ;; esac && \\
    curl -fsSL --retry 5 --retry-delay 10 -o /tmp/node.tar.xz https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-${{narch}}.tar.xz && \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --exclude CHANGELOG.md --exclude LICENSE --exclude README.md && \\
    rm -f /tmp/node.tar.xz && \\
    node --version | grep -qx "v{_NODE_VERSION}" && npm --version

RUN git config --global --add safe.directory '*' && \\
    git config --global user.name "CI Bot" && \\
    git config --global user.email "ci@weknora.test" && \\
    go version && go env GOTOOLCHAIN CGO_ENABLED

{code}
{clear_block}
CMD ["/bin/bash"]
"""


class Weknora3786To5725ImageDefault(Image):
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
        return Weknora3786To5725ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        apply = "git apply --whitespace=nowarn {patches}"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run.sh", _render(_RUN_TESTS_SH, self.pr)),
            File(".", "test-run.sh", _render(_RUN_TESTS_SH, self.pr, apply.format(patches="/home/test.patch"))),
            File(".", "fix-run.sh", _render(_RUN_TESTS_SH, self.pr, apply.format(patches="/home/test.patch /home/fix.patch"))),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        global_block = f"\n{self.global_env}\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""
        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}
{global_block}
WORKDIR /home/{repo}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
{clear_block}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"^MSWB_(?:GO|NODE) (PASS|FAIL|SKIP) (\S.*?)\s*$")


@Instance.register(_ORG, _INTERVAL)
class WEKNORA_3786_TO_5725(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Weknora3786To5725ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}
        for line in _ANSI_RE.sub("", test_log or "").replace("\r", "").splitlines():
            m = _RESULT_RE.match(line.strip())
            if m:
                buckets[m.group(1)].add(m.group(2))
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
