import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.25.14"
_BASE_TAG = "base-72249-to-64799"
_TIKV_VERSION = "v9.0.0-beta.2.pre-nightly"
_TEST_TAGS = "intest,deadlock"
_REALTIKV_PREFIX = "tests/realtikvtest/"

_PLUS_PATH_RE = re.compile(r"^\+\+\+ b/(\S+)$", re.MULTILINE)
_NEW_FILE_RE = re.compile(r"^--- /dev/null\n\+\+\+ b/(\S+)$", re.MULTILINE)
_ADDED_TEST_RE = re.compile(r"^\+func (Test\w+)\(", re.MULTILINE)
_HUNK_TEST_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ func (Test\w+)\(", re.MULTILINE
)
_IT_CASE_RE = re.compile(r"^tests/integrationtest/[tr]/(.+)\.(?:test|result)$")


def _plus_paths(patch: str) -> list[str]:
    return _PLUS_PATH_RE.findall(patch or "")


def _file_sections(patch: str):
    for chunk in re.split(r"^diff --git ", patch or "", flags=re.MULTILINE):
        m = _PLUS_PATH_RE.search(chunk)
        if m:
            yield m.group(1), chunk


def _go_test_files(pr: PullRequest) -> list[str]:
    files = [p for p in _plus_paths(pr.test_patch) if p.endswith("_test.go")]
    if files and all(p.startswith(_REALTIKV_PREFIX) for p in files):
        return files
    return [p for p in files if not p.startswith(_REALTIKV_PREFIX)]


def _needs_tikv(pr: PullRequest) -> bool:
    files = _go_test_files(pr)
    return bool(files) and all(p.startswith(_REALTIKV_PREFIX) for p in files)


def _scan_mode(pr: PullRequest) -> str:
    if _needs_tikv(pr):
        return "0"
    files = set(_go_test_files(pr))
    if files and files <= set(_NEW_FILE_RE.findall(pr.test_patch or "")):
        return "2"
    return "1"


def _go_packages(pr: PullRequest) -> list[str]:
    return sorted(
        {p.rsplit("/", 1)[0] if "/" in p else "." for p in _go_test_files(pr)}
    )


def _go_names(pr: PullRequest) -> list[str]:
    wanted = set(_go_test_files(pr))
    names: set[str] = set()
    for path, chunk in _file_sections(pr.test_patch):
        if path not in wanted:
            continue
        names |= set(_ADDED_TEST_RE.findall(chunk))
        if not _needs_tikv(pr):
            names |= set(_HUNK_TEST_RE.findall(chunk))
    return sorted(names)


def _it_cases(pr: PullRequest) -> list[str]:
    cases: set[str] = set()
    for path in _plus_paths(pr.test_patch):
        m = _IT_CASE_RE.match(path)
        if m:
            cases.add(m.group(1))
    return sorted(cases)


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
  if grep -rqE '^<<<<<<< ' --include='*.go' .; then
      echo "apply_patch: CONFLICT MARKERS present after apply" >&2
      exit 1
  fi
}

mswb_failpoint() {
  find "$PWD"/ -type d | grep -vE "(\.git|\.idea|tools)" | xargs /usr/local/bin/failpoint-ctl "$1"
}

mswb_module_root() {
  local d="$1"
  while [ "$d" != "." ] && [ ! -f "$d/go.mod" ]; do
      case "$d" in
          */*) d="${d%/*}" ;;
          *) d="." ;;
      esac
  done
  echo "$d"
}

mswb_module_roots() {
  local p
  for p in "$@"; do mswb_module_root "$p"; done | sort -u
}

mswb_rel_pkgs() {
  local root="$1" p rel=""
  shift
  for p in "$@"; do
      [ -d "$p" ] || continue
      [ "$(mswb_module_root "$p")" = "$root" ] || continue
      if [ "$root" = "." ]; then
          rel="$rel ./$p"
      elif [ "$p" = "$root" ]; then
          rel="$rel ."
      else
          rel="$rel ./${p#$root/}"
      fi
  done
  echo "$rel"
}

mswb_module_download() {
  local root
  for root in $(mswb_module_roots $1); do
      [ "$root" = "." ] && continue
      [ -d "$root" ] || continue
      (cd "$root" && go mod download)
  done
}

mswb_gate_tests() {
  local root rel
  for root in $(mswb_module_roots $1); do
      rel=$(mswb_rel_pkgs "$root" $1)
      [ -n "$rel" ] || continue
      (cd "$root" && go list -tags __TAGS__ -test -deps $rel > /dev/null)
      echo "gate: test packages resolve in module $root ->$rel"
  done
}

mswb_go_tests() {
  local pkgs="$1" files="$2" names="$3" scan="$4" all f root rel list p g sib
  : > /tmp/mswb_go.json
  : > /tmp/mswb_go.err
  [ -n "$pkgs" ] || return 0
  all="$names"
  if [ "$scan" = "1" ] || [ "$scan" = "2" ]; then
      for f in $files; do
          [ -f "$f" ] || continue
          all="$all $(grep -hoE '^func Test[A-Za-z0-9_]*\(' "$f" | sed -E 's/^func (Test[A-Za-z0-9_]*)\(/\1/' | tr '\n' ' ')"
      done
  fi
  if [ "$scan" = "2" ]; then
      for p in $pkgs; do
          [ -d "$p" ] || continue
          sib=""
          for g in $(ls "$p"/*_test.go 2>/dev/null | sort); do
              case " $files " in *" $g "*) continue;; esac
              sib="$sib $(grep -hoE '^func Test[A-Za-z0-9_]*\(' "$g" | sed -E 's/^func (Test[A-Za-z0-9_]*)\(/\1/' | tr '\n' ' ')"
          done
          sib=$(printf '%s\n' $sib | grep -E '^Test' | grep -vx 'TestMain' | tr '\n' ' ')
          echo "go_tests: existing tests added for $p: $sib"
          all="$all $sib"
      done
  fi
  list=$(printf '%s\n' $all | grep -E '^Test' | sort -u | paste -sd'|' -)
  if [ -z "$list" ]; then
      echo "go_tests: no test names selected"
      return 0
  fi
  echo "go_tests: -run ^($list)\$"
  for root in $(mswb_module_roots $pkgs); do
      rel=$(mswb_rel_pkgs "$root" $pkgs)
      [ -n "$rel" ] || continue
      echo "go_tests: module $root ->$rel"
      (cd "$root" && go test -tags __TAGS__ -count=1 -p 2 -timeout 90m -json -run "^($list)\$" $rel >> /tmp/mswb_go.json 2>> /tmp/mswb_go.err)
  done
  tail -n 30 /tmp/mswb_go.err
}

mswb_it_cases() {
  local c rc server=/tmp/mswb-tidb-server
  [ -n "$1" ] || return 0
  rm -f "$server"
  make server SERVER_OUT="$server" > /tmp/mswb_it_build.log 2>&1 || tail -n 20 /tmp/mswb_it_build.log
  for c in $1; do
      if [ ! -f "tests/integrationtest/t/$c.test" ]; then
          echo "it_cases: $c not present at this stage"
          continue
      fi
      rc=0
      if [ -x "$server" ]; then
          (cd tests/integrationtest && ./run-tests.sh -s "$server" -t "$c") > /tmp/mswb_it.log 2>&1 || rc=$?
      else
          echo "it_cases: tidb-server build failed" > /tmp/mswb_it.log
          rc=1
      fi
      tail -n 40 /tmp/mswb_it.log
      if [ "$rc" -eq 0 ]; then
          echo "mysqltest::tests/integrationtest::$c PASSED"
      else
          echo "mysqltest::tests/integrationtest::$c FAILED"
      fi
  done
}

mswb_start_cluster() {
  local i
  ulimit -n 1000000 2>/dev/null || ulimit -n 82920 2>/dev/null || true
  rm -rf /tmp/mswb-pd /tmp/mswb-tikv
  nohup pd-server --name=pd --data-dir=/tmp/mswb-pd \
      --client-urls=http://127.0.0.1:2379 --peer-urls=http://127.0.0.1:2380 \
      --config=/opt/mswb/pd.toml --log-file=/tmp/mswb-pd.log > /tmp/mswb-pd.out 2>&1 &
  for i in $(seq 1 60); do
      curl -sf http://127.0.0.1:2379/pd/api/v1/health > /dev/null 2>&1 && break
      sleep 1
  done
  nohup tikv-server --addr=127.0.0.1:20160 --advertise-addr=127.0.0.1:20160 \
      --status-addr=127.0.0.1:20180 --pd-endpoints=127.0.0.1:2379 \
      --data-dir=/tmp/mswb-tikv --config=/opt/mswb/tikv.toml \
      --log-file=/tmp/mswb-tikv.log > /tmp/mswb-tikv.out 2>&1 &
  for i in $(seq 1 90); do
      if curl -s http://127.0.0.1:2379/pd/api/v1/stores 2>/dev/null | grep -q '"state_name": "Up"'; then
          sleep 5
          echo "cluster: PD + TiKV up after ${i} poll(s)"
          return 0
      fi
      sleep 2
  done
  echo "cluster: PD + TiKV did not come up" >&2
  tail -n 20 /tmp/mswb-pd.out /tmp/mswb-tikv.out /tmp/mswb-tikv.log >&2 || true
  return 1
}

mswb_stop_cluster() {
  pkill -f tikv-server || true
  pkill -f pd-server || true
  sleep 3
  rm -rf /tmp/mswb-pd /tmp/mswb-tikv /tmp/mswb-pd.log /tmp/mswb-tikv.log /tmp/mswb-pd.out /tmp/mswb-tikv.out
}

mswb_install_tikv() {
  local v="$1" arch c n url sum entry
  case "$(uname -m)" in
      x86_64) arch=amd64 ;;
      aarch64|arm64) arch=arm64 ;;
      *) echo "tikv_install: unsupported arch $(uname -m)" >&2; return 1 ;;
  esac
  mkdir -p /opt/mswb /tmp/mswb-tiup
  for c in pd tikv; do
      for n in 1 2 3 4 5; do
          if python3 - "$c" "$v" "linux/$arch" > "/tmp/mswb-tiup/$c.ref" <<'MSWB_TIUP'
import json
import sys
import urllib.request

mirror = "https://tiup-mirrors.pingcap.com"
comp, ver, plat = sys.argv[1:4]
snap = json.load(urllib.request.urlopen(mirror + "/snapshot.json", timeout=60))
num = snap["signed"]["meta"]["/%s.json" % comp]["version"]
man = json.load(urllib.request.urlopen("%s/%d.%s.json" % (mirror, num, comp), timeout=60))
item = man["signed"]["platforms"][plat][ver]
print(mirror + item["url"], item["hashes"]["sha256"], item["entry"])
MSWB_TIUP
          then
              read -r url sum entry < "/tmp/mswb-tiup/$c.ref"
              if curl -fsSL --retry 3 -o "/tmp/mswb-tiup/$c.tar.gz" "$url" \
                  && echo "$sum  /tmp/mswb-tiup/$c.tar.gz" | sha256sum -c - > /dev/null; then
                  tar -xzf "/tmp/mswb-tiup/$c.tar.gz" -C /tmp/mswb-tiup "$entry"
                  install -m 0755 "/tmp/mswb-tiup/$entry" "/usr/local/bin/$entry"
                  echo "tikv_install: $entry $v linux/$arch sha256 $sum"
                  break
              fi
          fi
          echo "tikv_install: $c attempt $n failed"
          sleep 20
      done
  done
  rm -rf /tmp/mswb-tiup
  test -x /usr/local/bin/pd-server
  test -x /usr/local/bin/tikv-server
  printf '%s\n' '[replication]' 'max-replicas = 1' > /opt/mswb/pd.toml
  printf '%s\n' \
      'memory-usage-limit = "3GB"' \
      '[storage]' 'reserve-space = "0MB"' \
      '[storage.block-cache]' 'capacity = "1GB"' \
      '[rocksdb]' 'max-open-files = 4096' \
      '[raftdb]' 'max-open-files = 4096' > /opt/mswb/tikv.toml
}

mswb_emit() {
python3 - <<'MSWB_EMIT'
import json
import re

prefix = ""
try:
    m = re.search(r"^module\s+(\S+)", open("go.mod").read(), re.MULTILINE)
    if m:
        prefix = m.group(1) + "/"
except OSError:
    pass
status = {}
build_failed = set()
try:
    lines = open("/tmp/mswb_go.json", errors="replace").read().splitlines()
except OSError:
    lines = []
for line in lines:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    action = ev.get("Action")
    pkg = ev.get("Package") or ev.get("ImportPath") or ""
    if pkg.startswith(prefix):
        pkg = pkg[len(prefix):]
    pkg = pkg.split(" ", 1)[0]
    if action == "build-fail":
        build_failed.add(pkg)
        continue
    test = ev.get("Test")
    if not test or "/" in test:
        continue
    if action in ("pass", "fail", "skip"):
        status[(pkg, test)] = {"pass": "PASSED", "fail": "FAILED", "skip": "SKIPPED"}[action]
for pkg in sorted(build_failed):
    print("go_tests: build failed for package %s" % pkg)
for (pkg, test), st in sorted(status.items()):
    print("go::%s::%s %s" % (pkg, test, st))
MSWB_EMIT
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

export GOTOOLCHAIN=local

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__SHA__"

go mod download
mswb_module_download "__GO_PKGS__"
go build -o /usr/local/bin/failpoint-ctl github.com/pingcap/failpoint/failpoint-ctl
__TIKV_INSTALL__
__IT_PREP__
test -x /usr/local/bin/failpoint-ctl
__TIKV_GATE__
__IT_GATE__
mswb_gate_tests "__GO_PKGS__"
"""

_IT_PREP_SH = r"""apt-get update
apt-get install -y --no-install-recommends unzip
rm -rf /var/lib/apt/lists/*
_mt_spec=$(grep -oE 'github\.com/pingcap/mysql-tester/src@[0-9A-Za-z.+-]+' tests/integrationtest/run-tests.sh | head -n 1)
test -n "$_mt_spec"
GOBIN=/tmp/mswb-mt go install "$_mt_spec"
test -x /tmp/mswb-mt/src
rm -rf /tmp/mswb-mt
echo "it_prep: $_mt_spec is in the module cache"
"""

_IT_GATE_SH = r"""for _tool in $(grep -ohwE 'unzip|lsof|make|curl' tests/integrationtest/run-tests.sh | sort -u); do
    command -v "$_tool" > /dev/null
    echo "gate: $_tool needed by run-tests.sh is installed"
done
go list -tags __TAGS__ -deps ./cmd/tidb-server > /dev/null
echo "gate: cmd/tidb-server dependencies resolve"
"""

_ACT_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export GOTOOLCHAIN=local
export TZ=Asia/Shanghai
export log_level=fatal

cd /home/__REPO__

. /home/prepare.sh

__PATCH_STEP__
set +e
mswb_it_cases "__IT_CASES__"
mswb_failpoint enable > /tmp/mswb_fp.log 2>&1
__CLUSTER_STEP__
mswb_go_tests "__GO_PKGS__" "__GO_FILES__" "__GO_NAMES__" "__SCAN__"
mswb_emit
"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class _Img(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class Tidb72249To64799ImageBase(_Img):
    def dependency(self) -> str | Image:
        return _GO_IMAGE

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
    GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl make gcc libc6-dev python3 lsof procps tzdata \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Tidb72249To64799ImageDefault(_Img):
    def dependency(self) -> Image:
        return Tidb72249To64799ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _common_values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__TAGS__": _TEST_TAGS,
            "__GO_PKGS__": " ".join(_go_packages(self.pr)),
            "__GO_FILES__": " ".join(_go_test_files(self.pr)),
            "__GO_NAMES__": " ".join(_go_names(self.pr)),
            "__SCAN__": _scan_mode(self.pr),
            "__IT_CASES__": " ".join(_it_cases(self.pr)),
        }

    def _prepare(self) -> str:
        values = self._common_values()
        cases = values["__IT_CASES__"]
        tikv = _needs_tikv(self.pr)
        values.update(
            {
                "__SHA__": self.pr.base.sha,
                "__TIKV_INSTALL__": f'mswb_install_tikv "{_TIKV_VERSION}"' if tikv else "",
                "__IT_PREP__": _IT_PREP_SH if cases else "",
                "__IT_GATE__": _render(_IT_GATE_SH, {"__TAGS__": _TEST_TAGS}) if cases else "",
                "__TIKV_GATE__": "pd-server -V\ntikv-server -V" if tikv else "",
            }
        )
        return _render(_PREPARE_SH, values)

    def _act(self, name: str, *patches: str) -> File:
        values = self._common_values()
        values.update(
            {
                "__PATCH_STEP__": f"mswb_apply_patch {' '.join(patches)}" if patches else "",
                "__CLUSTER_STEP__": "mswb_start_cluster" if _needs_tikv(self.pr) else "",
            }
        )
        return File(".", name, _render(_ACT_SH, values))

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
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
_LINE_RE = re.compile(
    r"^(?P<name>(?:go|mysqltest)::\S+) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("pingcap", "tidb_72249_to_64799")
@Instance.register("pingcap", "72249_to_64799")
class TIDB_72249_TO_64799(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Tidb72249To64799ImageDefault(self.pr, self._config)

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
