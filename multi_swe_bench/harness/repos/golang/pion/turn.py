import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# pion/turn (module github.com/pion/turn/v4). Structure taken from the QC-passed
# batch-1 Go template; parse_log reports top-level tests only.
#
# go directive of go.mod at each PR's base commit (single module, no toolchain line):
#   #506 (2025-12, base a12c1e6) go 1.21 -> golang:1.21
#   #514 (2026-01, base d74862d) go 1.21 -> golang:1.21
# Single era. Tests are pure Go over loopback sockets (no cgo, no external network).
_GO_VERSION_BY_PR = {
    506: "1.21",
    514: "1.21",
}


def _go_version(pr: PullRequest) -> str:
    return _GO_VERSION_BY_PR.get(pr.number, "1.21")


# Small stdlib-only Go helper, built in prepare.sh to /home/touched_tests (outside
# the repo). It lists the top-level TestXxx functions that the working tree changes
# relative to HEAD (i.e. the tests added/edited by test.patch). It is used only when
# a package does not compile: `go test` then emits no per-test lines at all, and the
# test command reports those touched tests of that package as FAIL instead of letting
# them silently disappear. Packages that compile are reported exactly as go test says.
TOUCHED_TESTS_GO = r"""// touched_tests lists top-level Go test functions (TestXxx) whose declaration
// overlaps lines changed in the working tree relative to HEAD (git diff -U0 HEAD)
// or that live in untracked *_test.go files. Output: "<abs dir>\t<TestName>".
// Used only to report tests of a package that failed to compile as FAIL.
package main

import (
	"bufio"
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

type span struct{ lo, hi int }

func main() {
	root, _ := os.Getwd()
	changed := map[string][]span{}
	all := map[string]bool{}

	out, err := exec.Command("git", "diff", "-U0", "--no-color", "--no-ext-diff", "HEAD", "--", "*_test.go").Output()
	if err == nil {
		hunk := regexp.MustCompile(`^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@`)
		cur := ""
		sc := bufio.NewScanner(bytes.NewReader(out))
		sc.Buffer(make([]byte, 1024*1024), 64*1024*1024)
		for sc.Scan() {
			line := sc.Text()
			if strings.HasPrefix(line, "+++ ") {
				p := strings.TrimPrefix(line, "+++ ")
				if p == "/dev/null" {
					cur = ""
				} else {
					cur = strings.TrimPrefix(p, "b/")
				}
				continue
			}
			if m := hunk.FindStringSubmatch(line); m != nil && cur != "" {
				start, _ := strconv.Atoi(m[1])
				n := 1
				if m[2] != "" {
					n, _ = strconv.Atoi(m[2])
				}
				if n == 0 {
					changed[cur] = append(changed[cur], span{start, start + 1})
				} else {
					changed[cur] = append(changed[cur], span{start, start + n - 1})
				}
			}
		}
	}
	if out, err := exec.Command("git", "ls-files", "--others", "--exclude-standard", "--", "*_test.go").Output(); err == nil {
		for _, f := range strings.Fields(string(out)) {
			all[f] = true
		}
	}

	files := map[string]bool{}
	for f := range changed {
		files[f] = true
	}
	for f := range all {
		files[f] = true
	}
	for f := range files {
		fset := token.NewFileSet()
		af, err := parser.ParseFile(fset, filepath.Join(root, f), nil, 0)
		if err != nil {
			continue
		}
		dir := filepath.Dir(filepath.Join(root, f))
		for _, d := range af.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || fd.Recv != nil || !isTestName(fd.Name.Name) {
				continue
			}
			lo := fset.Position(fd.Pos()).Line
			hi := fset.Position(fd.End()).Line
			hit := all[f]
			for _, s := range changed[f] {
				if s.lo <= hi && s.hi >= lo {
					hit = true
					break
				}
			}
			if hit {
				fmt.Printf("%s\t%s\n", dir, fd.Name.Name)
			}
		}
	}
}

func isTestName(n string) bool {
	if !strings.HasPrefix(n, "Test") || n == "TestMain" {
		return false
	}
	if len(n) == 4 {
		return true
	}
	c := n[4]
	return !(c >= 'a' && c <= 'z')
}
"""

# Known-flaky test excluded in every stage (run/test/fix alike):
#   TestTCPClientMultipleConns (added by #514's test.patch) starts acceptor
#   goroutines that assert on conn.Read/Close after the test has returned. Whether
#   that late assertion fails is a race (observed both with and without the fix),
#   and when it fails Go panics ("Fail in goroutine after ... has completed") and
#   aborts the whole root package, so every later test in that package randomly
#   disappears. It is not a usable target and makes the root package
#   nondeterministic, so it is skipped (go test -skip, Go >= 1.20).
SKIP_FLAKY = "^TestTCPClientMultipleConns$"

TEST_COMMAND = r"""rc=0
touched="$(/home/touched_tests 2>/dev/null || true)"
for mod in $(git ls-files '*go.mod' | xargs -n1 dirname | sort -u); do
  if ! pkgs="$(cd "${mod}" && go list ./...)"; then
    rc=1
    continue
  fi
  if [ -z "${pkgs}" ]; then
    continue
  fi
  log="$(mktemp)"
  if ! (cd "${mod}" && go test -v -short -count=1 -skip '__SKIP_FLAKY__' ./...) > "${log}" 2>&1; then
    rc=1
  fi
  cat "${log}"
  for pkg in $(sed -n 's/^FAIL[[:space:]]\{1,\}\([^[:space:]]\{1,\}\)[[:space:]]\{1,\}\[build failed\].*$/\1/p' "${log}" | sort -u); do
    dir="$(cd "${mod}" && go list -f '{{.Dir}}' "${pkg}" 2>/dev/null)" || continue
    names="$(printf '%s\n' "${touched}" | awk -F'\t' -v d="${dir}" '$1 == d { print $2 }' | sort -u)"
    if [ -z "${names}" ]; then
      continue
    fi
    echo "# ${pkg} did not compile: reporting its changed tests as failed"
    for name in ${names}; do
      echo "=== RUN   ${name}"
      echo "--- FAIL: ${name} (0.00s)"
    done
    printf 'FAIL\t%s [build failed]\n' "${pkg}"
  done
  rm -f "${log}"
done
exit ${rc}""".replace("__SKIP_FLAKY__", SKIP_FLAKY)


class PionTurnImageBase(Image):
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
        return f"golang:{_go_version(self.pr)}"

    def image_tag(self) -> str:
        return f"base-go{_go_version(self.pr)}"

    def workdir(self) -> str:
        return f"base-go{_go_version(self.pr)}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"""RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"""
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    GO111MODULE=on \\
    CGO_ENABLED=0 \\
    CI=true

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

# The official golang images already ship git, ca-certificates, gcc and make.
# Older tags (e.g. golang:1.13 on Debian buster) point at retired apt mirrors, so
# only fall back to apt when something is actually missing.
RUN if ! command -v git >/dev/null 2>&1 || ! command -v gcc >/dev/null 2>&1; then \\
        apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential && rm -rf /var/lib/apt/lists/*; \\
    fi

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class PionTurnImageDefault(Image):
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
        return PionTurnImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def test_command(self) -> str:
        return TEST_COMMAND

    def files(self) -> list[File]:
        repo = self.pr.repo
        prepare = (
            r"""#!/bin/bash
set -euo pipefail

cd /home && go build -o /home/touched_tests /home/touched_tests.go

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
bash /home/check_git_changes.sh

for mod in $(git ls-files '*go.mod' | xargs -n1 dirname | sort -u); do
  (cd "${mod}" && go mod download)
done

for mod in $(git ls-files '*go.mod' | xargs -n1 dirname | sort -u); do
  pkgs="$(cd "${mod}" && go list ./...)"
  if [ -n "${pkgs}" ]; then
    (cd "${mod}" && go build ./... && go test -count=1 -run '^$' ./... > /dev/null)
  fi
done

# Must leave tracked files untouched (the grader resets the tree before each stage).
git checkout -- .
# Final hard gate (last step): the restored tree still builds.
go build ./...
echo DEPS_OK
"""
            .replace("__REPO__", repo)
            .replace("__SHA__", self.pr.base.sha)
        )

        def stage(apply: str) -> str:
            return (
                "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                f"cd /home/{repo}\n{apply}{TEST_COMMAND}\n"
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "touched_tests.go", TOUCHED_TESTS_GO),
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

""",
            ),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", stage("")),
            File(
                ".",
                "test-run.sh",
                stage("git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                stage(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
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

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
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

{self.clear_env}

"""


@Instance.register("pion", "turn")
class PionTurn(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PionTurnImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_run = re.compile(r"^=== RUN\s+(\S+)")
        # search(), not match(): a test that prints without a trailing newline puts its
        # "--- PASS:" marker mid-line (seen in mmcdole/gofeed); match() would miss it and
        # the stale "=== RUN" would turn a passing test into FAIL.
        re_result = re.compile(r"--- (PASS|FAIL|SKIP): (\S+)")
        re_package = re.compile(r"^(ok|FAIL)\s+(\S+)(\s|$)")
        # Go's testing package panics with this message when a goroutine started
        # by test X calls t.Error/t.Fail after X has returned. The failure belongs
        # to X (named in the message), not to whichever test happened to be running
        # when the panic aborted the package binary. pion/turn's TCP tests start
        # such goroutines (e.g. TestTCPClientMultipleConns in #514).
        re_late_fail = re.compile(r"^panic: Fail in goroutine after (\S+) has completed")

        pending_run = set()
        pending_result = {}

        for line in log.splitlines():
            run_match = re_run.match(line)
            if run_match:
                pending_run.add(run_match.group(1))
                continue

            result_match = re_result.search(line)
            if result_match:
                pending_result[result_match.group(2)] = result_match.group(1)
                continue

            late_match = re_late_fail.match(line)
            if late_match:
                pending_result[late_match.group(1).split("/")[0]] = "FAIL"
                # Tests still in flight were interrupted by the panic, not failed
                # by it: leave them unreported instead of blaming them.
                pending_run = set(pending_result)
                continue

            package_match = re_package.match(line)
            if not package_match:
                continue

            package = package_match.group(2)
            for name in pending_run - set(pending_result):
                pending_result[name] = "FAIL"
            for name, status in pending_result.items():
                # Top-level tests only. Subtests (t.Run) often get runtime-built names
                # (fmt.Sprintf, table fields, #00) that never appear literally in either
                # patch, so they cannot be graded targets (Argos-QC C03). The parent test
                # fails whenever one of its subtests fails, so nothing is lost.
                if "/" in name:
                    continue
                # Example/Fuzz/Benchmark functions are not graded targets (Argos-QC
                # golden_qc only recognises func Test*).
                if not name.startswith("Test"):
                    continue
                full_name = f"{package}/{name}"
                if status == "PASS":
                    passed_tests.add(full_name)
                elif status == "FAIL":
                    failed_tests.add(full_name)
                else:
                    skipped_tests.add(full_name)
            pending_run = set()
            pending_result = {}

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
