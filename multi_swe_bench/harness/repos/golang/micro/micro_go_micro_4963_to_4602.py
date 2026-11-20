import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# micro/go-micro PRs #4602 - #4963, base branch `master`. Every base commit of
# this range has the same layout:
#   go.mod   module go-micro.dev/v6, `go 1.25.0`
#   no go.work; nested modules only under examples/ and internal/website
#            (their own go.mod, so `./...` from the root never enters them)
# Go is backward compatible with older `go` directives, so one Go 1.26 base
# (GOTOOLCHAIN=local, no toolchain download; CI itself uses Go 1.26.10)
# serves the whole range.
#
# Test surface mirrors the CI job `unittests` (.github/workflows/tests.yaml):
# `go test ./...` over the root module with IN_TRAVIS_CI=yes. No external
# services: NATS tests start an embedded nats-server, model/ai provider tests
# use httptest, and the etcd / external-NATS tests skip without ETCD_ADDRESS /
# NATS_URL (the separate etcd-integration job is not reproduced).
# TRAVIS=true is also set for the stages: it skips the mDNS registry tests,
# whose multicast results leak between containers on a shared Docker network.
_INTERVAL_NAME = "micro_go_micro_4963_to_4602"
_LO, _HI = 4602, 4963

_GO_IMAGE = "golang:1.26.11-bookworm"
_MODULE = "go-micro.dev/v6"
_GIT_APPLY = "git apply --whitespace=nowarn"

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

# `go test -json` reporter, used in two modes:
#   mswb_go_report.py --plan PASS1        prints "<import path>\t<-run regex>"
#       for every package whose tests ran and had failing top-level tests;
#   mswb_go_report.py PASS1 [PASS2]       prints one stable line per result:
#   MSWB_GO <PASS|FAIL|SKIP> <pkg>::<Test>/<subtest>
#   MSWB_GO_PKG <PASS|FAIL|SKIP> <pkg>    (package aggregate, not graded)
#   MSWB_GO_BUILD_FAIL <pkg>              (diagnostic, not graded)
#   MSWB_GO_RETRY <status> <name>         (diagnostic, not graded)
# PASS2 holds the single retry of PASS1's failing top-level tests; for a
# retried test (and its subtests) the retry result is final, as with the
# whole-step retry CI applies to these tests (nick-fields/retry,
# max_attempts: 2). Genuine failures fail again and stay FAIL.
# <pkg> is the import path without the module prefix ("." for the root
# package). A test that started but never reported (panic, timeout) is FAIL.
# A package that fails without running any test (its test binary did not
# build, or vet failed) reports every top-level Test function declared in its
# directory's *_test.go files as FAIL: those tests exist and cannot pass. Such
# packages are not retried. No durations, so names are identical across
# stages.
_GO_REPORT_PY = r'''import json
import os
import re
import sys

MODULE_PREFIX = "@@MODULE@@"
MAX_LINES_PER_TEST = 40
STATUS = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
TEST_FUNC = re.compile(r"^func\s+(Test\w*)\s*\(\s*\w*\s*\*testing\.T\s*\)", re.M)
dump_budget = [20000]


def pkg_name(p):
    if not p:
        return "<unknown>"
    p = p.split(" ", 1)[0]
    if p == MODULE_PREFIX:
        return "."
    if p.startswith(MODULE_PREFIX + "/"):
        return p[len(MODULE_PREFIX) + 1:]
    return p


def import_path(pkg):
    return MODULE_PREFIX if pkg == "." else MODULE_PREFIX + "/" + pkg


def declared_tests(pkg):
    names = set()
    try:
        entries = sorted(os.listdir(pkg))
    except OSError:
        return names
    for f in entries:
        if not f.endswith("_test.go"):
            continue
        try:
            with open(os.path.join(pkg, f), encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError:
            continue
        for name in TEST_FUNC.findall(src):
            if name == "Test" or not name[4].islower():
                names.add(name)
    return names


def clean(s):
    return " ".join(str(s).split())


def remember(store, key, line):
    lines = store.setdefault(key, [])
    if len(lines) < MAX_LINES_PER_TEST:
        lines.append(line)


class Pass:
    def __init__(self, path, quiet):
        self.results = {}
        self.pkgs = {}
        self.ran = set()
        self.diag = []
        self.quiet = quiet
        self.parse(path)

    def note(self, line):
        if not self.quiet:
            self.diag.append(line)

    def dump(self, lines):
        for line in lines or []:
            if dump_budget[0] <= 0:
                return
            dump_budget[0] -= 1
            self.note("    | " + str(line).rstrip("\n"))

    def set(self, name, status):
        self.results[name] = status
        if status == "FAIL":
            self.note(f"MSWB_GO_FAILED {name}")

    def parse(self, path):
        open_tests, output, build_output = {}, {}, {}
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return
        with fh:
            for raw in fh:
                try:
                    e = json.loads(raw)
                except ValueError:
                    if raw.strip():
                        self.note("MSWB_GO_RAW " + raw.rstrip("\n"))
                    continue
                if not isinstance(e, dict):
                    continue
                action = e.get("Action")
                if action == "build-output":
                    remember(build_output, e.get("ImportPath"), e.get("Output") or "")
                    continue
                if action == "build-fail":
                    self.note("MSWB_GO_BUILD_FAIL " + pkg_name(e.get("ImportPath")))
                    self.dump(build_output.get(e.get("ImportPath")))
                    continue
                pkg = pkg_name(e.get("Package"))
                test = clean(e["Test"]) if e.get("Test") else ""
                if action == "run" and test:
                    self.ran.add(pkg)
                    open_tests.setdefault(pkg, set()).add(test)
                    continue
                if action == "output":
                    remember(output, (pkg, test), e.get("Output") or "")
                    continue
                status = STATUS.get(action)
                if not status:
                    continue
                if test:
                    self.ran.add(pkg)
                    open_tests.get(pkg, set()).discard(test)
                    self.set(f"{pkg}::{test}", status)
                    if status == "FAIL":
                        self.dump(output.get((pkg, test)))
                    output.pop((pkg, test), None)
                    continue
                for t in sorted(open_tests.pop(pkg, set())):
                    self.set(f"{pkg}::{t}", "FAIL")
                if status == "FAIL" and pkg not in self.ran:
                    for t in sorted(declared_tests(pkg)):
                        if f"{pkg}::{t}" not in self.results:
                            self.set(f"{pkg}::{t}", "FAIL")
                for key in [k for k in output if k[0] == pkg and k[1]]:
                    del output[key]
                self.pkgs[pkg] = status
                self.note(f"MSWB_GO_PKG {status} {pkg}")
                if status == "FAIL":
                    self.dump(output.get((pkg, "")))
                output.pop((pkg, ""), None)
        for pkg in sorted(open_tests):
            for t in sorted(open_tests[pkg]):
                self.set(f"{pkg}::{t}", "FAIL")


def failing_top_level(p):
    plan = {}
    for name, status in p.results.items():
        pkg, test = name.split("::", 1)
        if status == "FAIL" and pkg in p.ran:
            plan.setdefault(pkg, set()).add(test.split("/", 1)[0])
    return plan


def main(argv):
    if argv and argv[0] == "--plan":
        p = Pass(argv[1], quiet=True)
        for pkg, tests in sorted(failing_top_level(p).items()):
            print(import_path(pkg) + "\t^(" + "|".join(sorted(tests)) + ")$")
        return 0
    first = Pass(argv[0], quiet=False)
    final = dict(first.results)
    if len(argv) > 1:
        retry = Pass(argv[1], quiet=True)
        for pkg, tests in failing_top_level(first).items():
            for t in tests:
                prefix = f"{pkg}::{t}"
                for name, status in retry.results.items():
                    if name == prefix or name.startswith(prefix + "/"):
                        sys.stdout.write(f"MSWB_GO_RETRY {status} {name}\n")
                        final[name] = status
    for line in first.diag:
        sys.stdout.write(line + "\n")
    any_fail = False
    for name, status in final.items():
        any_fail = any_fail or status == "FAIL"
        sys.stdout.write(f"MSWB_GO {status} {name}\n")
    sys.stdout.flush()
    return 1 if any_fail else 0


sys.exit(main(sys.argv[1:]))
'''.replace("@@MODULE@@", _MODULE)


def _run_tests_sh(repo: str) -> str:
    # Identical for run.sh / test-run.sh / fix-run.sh. The package list is
    # computed from the tree being tested, so packages a patch adds are run.
    # MSWB_SETUP lines are for diagnosis only; parse_log does not grade them.
    return f"""#!/bin/bash
set -eo pipefail

REPO=/home/{repo}
cd "$REPO"
export CI=true
export GOTOOLCHAIN=local
export IN_TRAVIS_CI=yes
# TRAVIS=true is the repo's own switch for environments where multicast is not
# private to the test (registry/mdns_test.go skips its mDNS tests). Containers
# on a shared Docker bridge see each other's mDNS announcements, so TestMDNS's
# "deregistered service is gone" check depends on what else is running.
export TRAVIS=true
status=0
P1=/tmp/mswb_pass1.jsonl
P2=/tmp/mswb_pass2.jsonl
PLAN=/tmp/mswb_retry_plan.txt
rm -f "$P1" "$P2" "$PLAN"
touch "$P2"

echo "=== MSWB packages"
pkgs=()
if listing=$(go list -e -f '{{{{if .Module}}}}{{{{.Module.Path}}}} {{{{.ImportPath}}}}{{{{end}}}}' ./...); then
    echo "MSWB_SETUP PASS go-list"
else
    echo "MSWB_SETUP FAIL go-list"
    status=1
fi
while read -r mod p; do
    if [ "$mod" = "{_MODULE}" ] && [ -n "$p" ]; then
        pkgs+=("$p")
    fi
done <<< "$listing"
echo "MSWB_SETUP INFO packages=${{#pkgs[@]}}"
if [ "${{#pkgs[@]}}" -eq 0 ]; then
    echo "MSWB_SETUP FAIL no-packages"
    exit 1
fi

echo "=== MSWB suite: unit"
if go test -count=1 -p 4 -timeout=20m -json "${{pkgs[@]}}" > "$P1" 2>&1; then
    echo "MSWB_SETUP INFO pass1=ok"
else
    echo "MSWB_SETUP INFO pass1=failures"
fi

echo "=== MSWB retry of failing top-level tests (once)"
python3 /home/mswb_go_report.py --plan "$P1" > "$PLAN"
while IFS=$'\\t' read -r pkg regex; do
    [ -n "$pkg" ] || continue
    echo "MSWB_SETUP INFO retry $pkg $regex"
    if go test -count=1 -p 1 -timeout=5m -json -run "$regex" "$pkg" >> "$P2" 2>&1; then
        echo "MSWB_SETUP INFO retry-ok $pkg"
    else
        echo "MSWB_SETUP INFO retry-failures $pkg"
    fi
done < "$PLAN"

echo "=== MSWB results"
if python3 /home/mswb_go_report.py "$P1" "$P2"; then
    :
else
    status=1
fi

echo "=== MSWB done status=$status"
exit "$status"
"""


class MicroGoMicro4963To4602ImageBase(Image):
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
        return f"base-{_LO}_to_{_HI}"

    def workdir(self) -> str:
        return f"base-{_LO}_to_{_HI}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # SHARED base for the whole interval: keeps full history and never pins
        # or scrubs. The leading syntax directive opts out of
        # DockerfileEnhancer, so all proxy / CA / label infrastructure is
        # written out by hand.
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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

# GOTOOLCHAIN=local: the image's Go 1.26 builds the `go 1.25.0` directive of
# every base commit in the range; never download another toolchain.
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
    IN_TRAVIS_CI=yes \\
    CI=true

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
{global_block}
WORKDIR /home/

# python3 runs the go test -json reporter.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN go version && go env GOTOOLCHAIN

RUN git config --global --add safe.directory '*'

# A container has no machine id; OpenTelemetry host.id detection (exercised by
# the metrics/resource tests) reads /etc/machine-id like any systemd host.
RUN test -s /etc/machine-id || \\
    (cat /proc/sys/kernel/random/uuid | tr -d '-' > /etc/machine-id)

{code}
{clear_block}
CMD ["/bin/bash"]
"""


class MicroGoMicro4963To4602ImageDefault(Image):
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
        return MicroGoMicro4963To4602ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        prepare = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

# Section 1: PIN
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

# Section 2: PROVISION
export CI=true
export GOTOOLCHAIN=local
export IN_TRAVIS_CI=yes
# Module downloads fail transiently; retry up to three times.
retry() {{
    local n
    for n in 1 2 3; do
        "$@" && return 0
        echo "prepare: attempt $n failed: $*" >&2
        sleep 15
    done
    return 1
}}
test "$(head -n1 go.mod)" = "module {module}"
retry go mod download
# Warm the build cache the stages use: every package and its test binary.
go build ./...
go test -count=1 -run '^$' ./... > /dev/null
# Restore every tracked file so the stage patches apply to the pristine base
# tree. The module and build caches are kept.
git checkout -- .

# Section 3: GATE
go version
test "$(go env GOTOOLCHAIN)" = "local"
python3 -c 'import json, sys'
go list -f '{{{{.Module.Path}}}} {{{{.ImportPath}}}}' ./... | awk '$1 == "{module}"' > /home/go-packages.txt
test -s /home/go-packages.txt
go vet . > /dev/null
echo "DEPS_OK $(go version)"
""".format(repo=repo, sha=sha, module=_MODULE)

        run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
bash /home/mswb_run_tests.sh
""".format(repo=repo)

        test_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
{git_apply} /home/test.patch
bash /home/mswb_run_tests.sh
""".format(repo=repo, git_apply=_GIT_APPLY)

        fix_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
{git_apply} /home/test.patch /home/fix.patch
bash /home/mswb_run_tests.sh
""".format(repo=repo, git_apply=_GIT_APPLY)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_go_report.py", _GO_REPORT_PY),
            File(".", "mswb_run_tests.sh", _run_tests_sh(repo)),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        # dependency() returns an Image, so DockerfileEnhancer never touches
        # this file: the pin and the history scrub are written out here.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "\n".join(
            f"COPY {file.name} /home/" for file in self.files()
        )

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}
{global_block}
WORKDIR /home/{repo}

RUN git reset --hard
# A few base commits are no longer reachable from the default branch the
# shared base cloned (they sat on history later rewritten); fetch exactly this
# PR's base commit by SHA when it is absent. FETCH_HEAD is dropped right away.
RUN git cat-file -e "{sha}^{{commit}}" 2>/dev/null || \\
    (git fetch --no-tags origin {sha} && rm -f .git/FETCH_HEAD)
RUN git checkout --detach {sha}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
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
{clear_block}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# MSWB_SETUP / MSWB_GO_PKG / MSWB_GO_BUILD_FAIL / MSWB_GO_RAW lines are
# diagnostics, not tests, and are deliberately not matched.
_RESULT_RE = re.compile(r"^MSWB_GO (PASS|FAIL|SKIP) (\S.*?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}
    for line in _ANSI_RE.sub("", test_log or "").replace("\r", "").splitlines():
        m = _RESULT_RE.match(line.strip())
        if not m:
            continue
        status, name = m.groups()
        buckets[status].add(name)

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


@Instance.register("micro", _INTERVAL_NAME)
class MICRO_GO_MICRO_4963_TO_4602(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MicroGoMicro4963To4602ImageDefault(self.pr, self._config)

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
        return _parse_mswb_log(test_log)


# ---------------------------------------------------------------------------
# Routing
#
# The JSONL carries no number_interval, so Instance.create() looks up the bare
# "micro/go-micro" key. No other go-micro config exists, so this module owns that
# key: PRs of this interval go to the interval instance, anything else is
# refused rather than built with the wrong toolchain.
# ---------------------------------------------------------------------------


@Instance.register("micro", "go-micro")
class MICRO_GO_MICRO_4963_TO_4602_DISPATCH(Instance):
    """Router only: __new__ returns the interval-appropriate instance."""

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return MICRO_GO_MICRO_4963_TO_4602(pr, config, *args, **kwargs)
        raise ValueError(
            f"micro/go-micro PR #{pr.number} is outside every configured "
            f"interval (only #{_LO}-#{_HI})"
        )
