from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:14.21.3-bullseye"
_BASE_TAG = "base-1403_to_1204"
_REPO_DIR = "hoppscotch"

_BINARY_HUNK_RE = re.compile(r"^Binary files (\S+) and (\S+) differ$", re.M)


def binary_excludes(pr: PullRequest) -> str:
    paths: list[str] = []
    for patch in (pr.test_patch, pr.fix_patch):
        for pair in _BINARY_HUNK_RE.findall(patch or ""):
            for side in pair:
                if side == "/dev/null":
                    continue
                path = side[2:] if side[:2] in ("a/", "b/") else side
                if path not in paths:
                    paths.append(path)
    return "".join(f" --exclude={path}" for path in paths)


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

_GATE_JS = r"""const fs = require("fs");
const path = require("path");
const cp = require("child_process");

const root = process.argv[2];
const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const jestConfig = pkg.jest || {};
const problems = [];

const resolves = (id) => {
  try {
    require.resolve(id, { paths: [root] });
    return true;
  } catch (e) {
    return false;
  }
};

const modules = ["jest"]
  .concat(Object.values(jestConfig.transform || {}).map((v) => (Array.isArray(v) ? v[0] : v)))
  .concat(jestConfig.snapshotSerializers || [])
  .concat(jestConfig.testEnvironment ? [jestConfig.testEnvironment] : []);

modules.forEach((id) => resolves(id) || problems.push("cannot resolve " + id));

(jestConfig.setupFiles || [])
  .concat(jestConfig.setupFilesAfterEnv || [])
  .map((f) => f.replace("<rootDir>", root))
  .forEach((f) => fs.existsSync(f) || problems.push("missing setup file " + f));

const listed = cp.spawnSync(path.join(root, "node_modules", ".bin", "jest"), ["--ci", "--listTests"], {
  cwd: root,
  encoding: "utf8",
});
const files = String(listed.stdout || "").split("\n").filter((line) => line.trim());
listed.status === 0 ||
  problems.push("jest --listTests exited " + listed.status + ": " + String(listed.stderr || "").split("\n").slice(0, 3).join(" | "));
files.length || problems.push("jest --listTests found no test files");

problems.length &&
  (problems.forEach((p) => console.error("gate: " + p)), process.exit(1));

console.log("gate: " + modules.length + " jest modules resolve, " + files.length + " test files listed");
"""

_RUN_TESTS_JS = r"""const fs = require("fs");
const cp = require("child_process");

const root = process.argv[2];
const OUT = "/tmp/jest-results.json";
const ATTEMPTS = 3;
const STATUS = {
  passed: "PASSED",
  failed: "FAILED",
  pending: "SKIPPED",
  skipped: "SKIPPED",
  todo: "SKIPPED",
  disabled: "SKIPPED",
};
const RANK = { PASSED: 3, FAILED: 2, SKIPPED: 1 };
const results = new Map();

const relative = (name) => String(name || "").split(root + "/").slice(-1)[0];
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const record = (report) => {
  (report.testResults || []).forEach((suite) => {
    const file = relative(suite.name);
    const assertions = suite.assertionResults || [];
    const brokenKey = file + " > <suite failed to run>";
    const entries = assertions.length
      ? assertions.map((a) => [[file].concat(a.ancestorTitles || [], [a.title]).join(" > "), STATUS[a.status] || "SKIPPED"])
      : [[brokenKey, "FAILED"]];
    assertions.length && results.delete(brokenKey);
    entries.forEach(([name, status]) => {
      const previous = results.get(name);
      (!previous || RANK[status] > RANK[previous]) && results.set(name, status);
    });
  });
};

const runOnce = (files) => {
  fs.existsSync(OUT) && fs.unlinkSync(OUT);
  const r = cp.spawnSync(
    root + "/node_modules/.bin/jest",
    ["--ci", "--silent", "--coverage=false", "--json", "--outputFile=" + OUT].concat(files),
    { cwd: root, stdio: ["ignore", "inherit", "inherit"], env: Object.assign({}, process.env, { CI: "true", TZ: "UTC" }) }
  );
  console.log("run-tests: jest exited " + r.status + (files.length ? " (retry of " + files.length + " file(s))" : ""));
  return fs.existsSync(OUT) ? JSON.parse(fs.readFileSync(OUT, "utf8")) : null;
};

let report = runOnce([]);
report && record(report);

for (let attempt = 2; report && attempt <= ATTEMPTS; attempt++) {
  const failing = (report.testResults || []).filter((s) => s.status === "failed").map((s) => "^" + escape(s.name) + "$");
  if (!failing.length) break;
  report = runOnce(failing);
  report && record(report);
}

const counts = { PASSED: 0, FAILED: 0, SKIPPED: 0 };
results.forEach((status, name) => {
  counts[status]++;
  console.log(status + " " + name);
});
console.log("run-tests: tests=" + results.size + " passed=" + counts.PASSED + " failed=" + counts.FAILED + " skipped=" + counts.SKIPPED);

results.size || (console.log("run-tests: jest produced no results"), process.exit(1));
process.exit(counts.FAILED ? 1 : 0);
"""

_PREPARE_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo_dir}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

fetched=0
for attempt in 1 2 3; do
  if git fetch --no-tags origin {sha}; then
    fetched=1
    break
  fi
  echo "prepare.sh: fetch of {sha} attempt $attempt failed; retrying in 15s" >&2
  sleep 15
done
test "$fetched" -eq 1

git checkout --detach {sha}
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"

installed=0
for attempt in 1 2 3; do
  if npm ci --ignore-scripts --no-audit --no-fund; then
    installed=1
    break
  fi
  echo "prepare.sh: npm ci attempt $attempt failed; retrying in 15s" >&2
  sleep 15
done
test "$installed" -eq 1

node /home/gate.js /home/{repo_dir} && echo DEPS_OK
"""

_TEST_CMD = f"node /home/run-tests.js /home/{_REPO_DIR}"

_STAGE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo_dir}
{apply}{test_cmd}
"""


class Hoppscotch1403To1204ImageBase(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_IMAGE}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV CI=true \\
    npm_config_fund=false \\
    npm_config_audit=false \\
    NODE_OPTIONS=--max-old-space-size=4096

{self.global_env}

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

WORKDIR /home/

RUN git --version && \\
    node --version && \\
    npm --version && \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{_REPO_DIR}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Hoppscotch1403To1204ImageDefault(Image):

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
        return Hoppscotch1403To1204ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        excludes = binary_excludes(self.pr)
        stages = {
            "run.sh": "",
            "test-run.sh": f"git apply --whitespace=nowarn{excludes} /home/test.patch\n",
            "fix-run.sh": f"git apply --whitespace=nowarn{excludes} /home/test.patch /home/fix.patch\n",
        }
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "gate.js", _GATE_JS),
            File(".", "run-tests.js", _RUN_TESTS_JS),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(repo_dir=_REPO_DIR, sha=self.pr.base.sha),
            ),
        ] + [
            File(
                ".",
                name,
                _STAGE_SH.format(repo_dir=_REPO_DIR, apply=apply, test_cmd=_TEST_CMD),
            )
            for name, apply in stages.items()
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copies}
RUN bash /home/prepare.sh

WORKDIR /home/{_REPO_DIR}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
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
"""


_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_RESULT = re.compile(r"^(PASSED|FAILED|SKIPPED)\s+(\S.*)$")


def parse_run_tests_log(log: str) -> TestResult:
    log = _RE_ANSI.sub("", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    buckets = {"PASSED": passed_tests, "FAILED": failed_tests, "SKIPPED": skipped_tests}

    for line in log.splitlines():
        match = _RE_RESULT.match(line.strip())
        if match:
            buckets[match.group(1)].add(match.group(2).strip())

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    skipped_tests -= passed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("hoppscotch", "hoppscotch_1403_to_1204")
class HOPPSCOTCH_1403_TO_1204(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Hoppscotch1403To1204ImageDefault(self.pr, self._config)

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
        return parse_run_tests_log(test_log)
