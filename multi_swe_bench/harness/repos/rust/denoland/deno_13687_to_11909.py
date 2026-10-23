import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.denoland import deno_2952_to_1281 as _deno_ts

_RUST_IMAGE = "rust:1.59.0-bullseye"
_INTERVAL_NAME = "deno_13687_to_11909"
_BASE_TAG = "base-13687_to_11909"
_DEFAULT_TOOLCHAIN = "1.59.0"

_INFRA_BLOCK = """ARG TARGETARCH
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
"""

_LANG_ENV = """ENV CARGO_TERM_COLOR=never \\
    CARGO_BUILD_JOBS=3 \\
    CARGO_INCREMENTAL=0 \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    CARGO_NET_GIT_FETCH_WITH_CLI=true \\
    CARGO_NET_RETRY=10 \\
    RUST_BACKTRACE=1 \\
    DENO_NO_UPDATE_CHECK=1
"""

_TOOL_CHECK = "RUN git --version && rustc --version && cargo --version\n"

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

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "+${{BASE_COMMIT}}:refs/heads/msb-base-commit"
git checkout --detach "${{BASE_COMMIT}}"
bash /home/check_git_changes.sh

TOOLCHAIN={default_toolchain}
if grep -qE 'rust-version: [0-9.]+' .github/workflows/ci.yml; then
  TOOLCHAIN=$(grep -m1 -oE 'rust-version: [0-9.]+' .github/workflows/ci.yml | awk '{{print $2}}')
fi
for attempt in 1 2 3 4 5; do
  if rustup toolchain install "$TOOLCHAIN" --profile minimal --no-self-update; then break; fi
  sleep 20
done
rustup override set "$TOOLCHAIN"
rustc --version
git submodule update --init test_util/std
export CARGO_NET_GIT_FETCH_WITH_CLI=true
git config --global http.version HTTP/1.1
git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 1000
git config --global http.lowSpeedTime 600
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if cargo fetch --locked; then break; fi
  sleep 60
done
cargo fetch --locked --offline
cargo build --locked -p deno --bin deno
cargo build --locked -p test_util --bin test_server
cargo test --locked -p deno --test integration_tests --no-run

test -x target/debug/deno && test -x target/debug/test_server && test -f test_util/std/testing/asserts.ts && target/debug/deno --version && echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
export NO_COLOR=1
cargo build --locked -p deno --bin deno > /tmp/mswb_build.log 2>&1 || tail -n 50 /tmp/mswb_build.log
cargo build --locked -p test_util --bin test_server > /tmp/mswb_build_ts.log 2>&1 || tail -n 50 /tmp/mswb_build_ts.log
found=0
while read -r spec; do
  [ -f "$spec" ] || continue
  case "$spec" in
    cli/tests/unit/*_test.ts)
      found=1
      out=/tmp/mswb_unit_$(echo "$spec" | tr '/.' '__').log
      target/debug/test_server > /tmp/mswb_test_server.log 2>&1 &
      server=$!
      sleep 5
      target/debug/deno test --unstable --location=http://js-unit-tests/foo/bar -A "$spec" > "$out" 2>&1
      kill "$server" 2>/dev/null
      wait "$server" 2>/dev/null
      target/debug/deno run --allow-read /home/list_deno_tests.js deno "$spec" "$out"
      ;;
    cli/tests/integration/*_tests.rs)
      found=1
      mod=$(basename "$spec" _tests.rs)
      out=/tmp/mswb_itest_${{mod}}.log
      cargo test --locked -p deno --test integration_tests "integration::${{mod}}::" > "$out" 2>&1
      target/debug/deno run --allow-read /home/list_deno_tests.js cargo "$spec" "$out"
      ;;
  esac
done < <(grep -E '^\\+\\+\\+ b/' /home/test.patch | sed 's|^+++ b/||' | sort -u)
if [ "$found" = 0 ]; then
  echo "MSWB_NO_SPECS"
fi
exit 0
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_LIST_DENO_TESTS_JS = r"""const [kind, spec, logPath] = Deno.args;
const strip = (s) => s.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, "");
const source = Deno.readTextFileSync(spec);
let log = "";
try {
  log = strip(Deno.readTextFileSync(logPath));
} catch (_e) {
  log = "";
}

function expectedDeno(text) {
  const names = [];
  const re = /(?:Deno\.test|unitTest)\s*\(/g;
  const starts = [];
  let m;
  while ((m = re.exec(text)) !== null) {
    starts.push(m.index + m[0].length);
  }
  for (let i = 0; i < starts.length; i++) {
    const end = i + 1 < starts.length ? starts[i + 1] : text.length;
    const chunk = text.slice(starts[i], Math.min(end, starts[i] + 2000));
    const lit = chunk.match(/^\s*(["'`])((?:(?!\1).)+)\1/);
    if (lit) {
      names.push(lit[2]);
      continue;
    }
    const nameProp = chunk.match(/\bname\s*:\s*(["'`])((?:(?!\1).)+)\1/);
    const fn = chunk.match(/function\s*\*?\s*([A-Za-z0-9_$]+)\s*\(/);
    if (nameProp && (!fn || nameProp.index < fn.index)) {
      names.push(nameProp[2]);
    } else if (fn) {
      names.push(fn[1]);
    }
  }
  return names;
}

function expectedCargo(text) {
  const names = [];
  const re = /#\[(?:test|tokio::test|flaky_test(?:::flaky_test)?)\]\s*(?:#\[[^\]]*\]\s*)*(?:async\s+)?fn\s+([A-Za-z0-9_]+)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    names.push(m[1]);
  }
  const it = /\bitest(?:_flaky)?!\s*\(\s*([A-Za-z0-9_]+)/g;
  while ((m = it.exec(text)) !== null) {
    names.push(m[1]);
  }
  return names;
}

const results = new Map();
let prefix;
let expected;
if (kind === "cargo") {
  const mod = spec.split("/").pop().replace(/_tests\.rs$/, "");
  prefix = "cargo::integration_tests::integration::" + mod + "::";
  expected = expectedCargo(source);
  const re = new RegExp("^test integration::" + mod + "::([A-Za-z0-9_]+) \\.\\.\\. (ok|FAILED|ignored)", "gm");
  let m;
  while ((m = re.exec(log)) !== null) {
    results.set(m[1], m[2]);
  }
} else {
  prefix = "deno::" + spec + "::";
  expected = expectedDeno(source);
  const re = /^test (.+?) \.\.\. (ok|FAILED|ignored)\b/gm;
  let m;
  while ((m = re.exec(log)) !== null) {
    results.set(m[1].trim(), m[2]);
  }
}

const seen = new Set();
for (const [name, status] of results) {
  const label = status === "ok" ? "PASSED" : status === "FAILED" ? "FAILED" : "SKIPPED";
  console.log("MSWB_RESULT " + label + " " + prefix + name);
  seen.add(name);
}
for (const name of expected) {
  if (!seen.has(name)) {
    console.log("MSWB_RESULT FAILED " + prefix + name);
    seen.add(name);
  }
}
"""

_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()
    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    for line in test_log.splitlines():
        match = _RESULT_RE.match(line.strip())
        if not match:
            continue
        status, name = match.group(1), match.group(2)
        if status == "PASSED":
            passed_tests.add(name)
        elif status == "FAILED":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)
    passed_tests -= failed_tests
    skipped_tests -= failed_tests | passed_tests
    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class ImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = _INFRA_BLOCK.format(org=self.pr.org, repo=self.pr.repo)
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {_RUST_IMAGE}\n\n"
            f"{infra}\n"
            f"{_LANG_ENV}\n"
            f"{_TOOL_CHECK}\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}\n\n'
            'CMD ["/bin/bash"]\n'
        )


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(repo=repo, default_toolchain=_DEFAULT_TOOLCHAIN),
            ),
            File(".", "run.sh", _RUN_SH.format(repo=repo)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(repo=repo)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(repo=repo)),
            File(".", "run_tests.sh", _RUN_TESTS_SH.format(repo=repo)),
            File(".", "list_deno_tests.js", _LIST_DENO_TESTS_JS),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        hardening = Image._HARDENING_BLOCK.rstrip("\n")
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copy_commands}\n"
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\n\n'
            "RUN bash /home/prepare.sh\n\n"
            f"WORKDIR /home/{self.pr.repo}\n\n"
            f"{hardening}\n\n"
        )


@Instance.register("denoland", _INTERVAL_NAME)
class DENO_13687_TO_11909(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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


_INTERVAL_RE = re.compile(r"^deno_(?P<hi>\d+)_to_(?P<lo>\d+)$")
_INTERVAL_MATCH = _INTERVAL_RE.match(_INTERVAL_NAME)
_INTERVAL_LO = int(_INTERVAL_MATCH.group("lo"))
_INTERVAL_HI = int(_INTERVAL_MATCH.group("hi"))
_FALLBACK = _deno_ts.DenoDefault


@Instance.register("denoland", "deno")
class DENO_ROUTER(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _INTERVAL_LO <= pr.number <= _INTERVAL_HI:
            return DENO_13687_TO_11909(pr, config, *args, **kwargs)
        return _FALLBACK(pr, config, *args, **kwargs)
