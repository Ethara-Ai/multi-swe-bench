
import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.21-bookworm"

_IMPORT_PATH = "github.com/volatiletech/sqlboiler/v4"
_REPO_DIR = "/home/sqlboiler"

_TEST_PKGS = "./boilingcore ./drivers"

_MAP_BEGIN = "===== BEGIN TEST FILE MAP ====="
_MAP_END = "===== END TEST FILE MAP ====="
_RESULTS_BEGIN = "===== BEGIN TEST RESULTS ====="
_RESULTS_END = "===== END TEST RESULTS ====="

_CHECK_GIT_CHANGES = """#!/bin/bash
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


def _test_cmd() -> str:
    return f"""cd {_REPO_DIR}
echo '{_MAP_BEGIN}'
for f in $(find ./boilingcore ./drivers -maxdepth 1 -name '*_test.go' 2>/dev/null); do
  sed -nE 's/^func (Test[A-Za-z0-9_]+).*/\\1/p' "$f" | while read -r t; do
    printf 'TESTFILE\\t%s\\t%s\\n' "${{f#./}}" "$t"
  done
done
echo '{_MAP_END}'

QDIR=/tmp/go_quarantine
rm -rf "$QDIR"; mkdir -p "$QDIR"
attempt=0
while [ "$attempt" -lt 5 ]; do
  attempt=$((attempt + 1))
  go test -json -count=1 -timeout 15m {_TEST_PKGS} >/tmp/go_out.json 2>/tmp/go_err.txt || true
  # Keep the FIRST attempt's streams: they carry the build-failure attribution that
  # parse_log needs, and they disappear once quarantine makes a later attempt compile.
  if [ "$attempt" = 1 ]; then
    cp /tmp/go_out.json /tmp/go_first_out
    cp /tmp/go_err.txt /tmp/go_first_err
  fi
  # `go test` SPLITS these two streams, and the split is easy to get backwards:
  #   stdout -> "FAIL <pkg> [build failed]"   (the verdict)
  #   stderr -> "<file>:<line>:<col>: <msg>"  (the diagnostics naming the bad files)
  # So the detection must read stdout and the file extraction must read stderr.
  grep -qE '^FAIL[[:space:]]+[^[:space:]]+[[:space:]]+\\[(build|setup) failed\\]' /tmp/go_out.json || break
  BAD=$(sed -nE 's#^([A-Za-z0-9_./-]+_test\\.go):[0-9]+:[0-9]+: .*#\\1#p' /tmp/go_err.txt | sort -u)
  [ -z "$BAD" ] && break
  moved=0
  for f in $BAD; do
    if [ -f "$f" ]; then
      mkdir -p "$QDIR/$(dirname "$f")"
      mv "$f" "$QDIR/$f"
      echo "QUARANTINED_UNCOMPILABLE_TEST_FILE: $f" >&2
      moved=1
    fi
  done
  [ "$moved" = 0 ] && break
done

echo '{_RESULTS_BEGIN}'
cat /tmp/go_first_err
cmp -s /tmp/go_first_err /tmp/go_err.txt || cat /tmp/go_err.txt
cat /tmp/go_first_out
cmp -s /tmp/go_first_out /tmp/go_out.json || cat /tmp/go_out.json
echo '{_RESULTS_END}'
"""


class _ImageBase(Image):

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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {_GO_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    GOTOOLCHAIN=local \\
    GOFLAGS=-buildvcs=false \\
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

{self.global_env}

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class _ImageDefault(Image):

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
        return _ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = _test_cmd()

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
# The shared base kept full history and is pinned to nothing, so this layer owns the
# pin. --detach is required, not stylistic: the prune block in the PR Dockerfile
# ASSERTS HEAD == BASE_COMMIT rather than re-establishing it, and then deletes every
# ref. An attached HEAD would have its branch deleted out from under it, or would
# keep post-fix history reachable inside the shipped image.
cd {_REPO_DIR}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION the era's dependencies, at BUILD time ----------
# Module mode, resolved against the go.sum that shipped with THIS commit, so the
# graph is reproducible and the graded stages need no network. This is the whole
# reason the era split exists: the pre-#687 tree has no go.mod and cannot do this.
#
# SCOPED to the graded packages on purpose. A bare `go mod download` walks the
# ENTIRE module graph, which here includes modernc.org/{{ccorpus,sqlite,tcl}} --
# roughly a gigabyte of SQLite test corpus reached only through
# drivers/sqlboiler-sqlite3, a package nothing graded here touches. Those three zips
# are also the ones that fall over with "unexpected EOF" against proxy.golang.org.
# `go build <pkgs>` downloads exactly the modules those packages need, still pinned
# by go.sum, so the provisioning stays era-correct and reproducible while dropping
# the dead weight.
go build {_TEST_PKGS}

# ---------- Section 3: HARD GATE — last, and not tolerant of failure ----------
# `go test -run XXX_NO_MATCH` compiles each package AND its test binary without
# running anything, which is strictly deeper than `go build`: it catches a dependency
# that only the test binary reaches -- exactly the class of miss that does not fail
# loudly at run time, but surfaces as "[setup failed]" and a stage that collected
# zero tests, which parse_log reports as 0/0/0 and Report.check() rule 1
# (report.py:204) rejects only after all three stages have run.
go test -count=1 -run XXX_NO_MATCH {_TEST_PKGS}
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

# Baseline stage: NO patch applied. Load-bearing, not informational --
# Report.check() step 6 (report.py:269-313) falls back to `test.run` to decide
# whether a credited test is F2P or P2P, so a baseline that fails to run silently
# corrupts the classification.
{test_cmd}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

# test.patch ONLY. No `|| true` on the apply: a run script has no hard gate to redeem
# a swallowed failure, so an unapplied test.patch would make this stage identical to
# the baseline, no test would transition !PASS -> PASS, and Report.check() rule 3
# (report.py:217) would discard the instance with nothing explaining why.
cd {_REPO_DIR}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

# test.patch FIRST, then fix.patch -- the fix must land on a tree that already
# carries the new tests.
cd {_REPO_DIR}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd {_REPO_DIR}; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f {_REPO_DIR}/.gitmodules ]; then \\
        cd {_REPO_DIR} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


def _tests_added_by_patch(test_patch: str) -> set[str]:
    added: set[str] = set()
    for line in test_patch.split("\n"):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = re.match(r"\+\s*func\s+(Test[A-Za-z0-9_]+)\s*\(", line)
        if m:
            added.add(m.group(1))
    return added


def _parse_go_test_json(test_log: str, test_patch: str = "") -> TestResult:
    file_of: dict[str, str] = {}
    map_start = test_log.find(_MAP_BEGIN)
    map_end = test_log.find(_MAP_END)
    if map_start != -1 and map_end > map_start:
        for line in test_log[map_start:map_end].splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] == "TESTFILE":
                file_of[parts[2].strip()] = parts[1].strip()

    body = test_log
    res_start = test_log.find(_RESULTS_BEGIN)
    res_end = test_log.find(_RESULTS_END)
    if res_start != -1 and res_end > res_start:
        body = test_log[res_start + len(_RESULTS_BEGIN) : res_end]

    verdicts: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = ev.get("Action")
        test = ev.get("Test")
        if not test or action not in ("pass", "fail", "skip"):
            continue

        root = test.split("/", 1)[0]
        path = file_of.get(root)
        if not path:
            pkg = ev.get("Package") or ""
            if pkg.startswith(_IMPORT_PATH + "/"):
                pkg = pkg[len(_IMPORT_PATH) + 1 :]
            elif pkg == _IMPORT_PATH:
                pkg = "."
            path = pkg

        verdicts[f"{path}::{test}" if path else test] = action

    if test_patch:
        added = _tests_added_by_patch(test_patch)
        if added:
            for m in re.finditer(
                r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]\s*$", body, re.M
            ):
                pkg = m.group(1)
                if pkg.startswith(_IMPORT_PATH + "/"):
                    rel = pkg[len(_IMPORT_PATH) + 1 :]
                elif pkg == _IMPORT_PATH:
                    rel = "."
                else:
                    continue
                for test, path in file_of.items():
                    if test not in added:
                        continue
                    owner = path.rsplit("/", 1)[0] if "/" in path else "."
                    if owner == rel:
                        verdicts.setdefault(f"{path}::{test}", "fail")

    passed = {k for k, v in verdicts.items() if v == "pass"}
    failed = {k for k, v in verdicts.items() if v == "fail"}
    skipped = {k for k, v in verdicts.items() if v == "skip"}

    passed -= failed
    skipped -= failed
    skipped -= passed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("aarondl", "sqlboiler")
class Sqlboiler(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_json(test_log, self.pr.test_patch)
