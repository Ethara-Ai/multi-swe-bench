"""MetaMask/metamask-extension config for PRs #40459-#46716 (Feb-Sep 2026).

Routed through ``number_interval`` so the original ``metamaskextension.py``
(Node 18, full unit + integration + e2e suites) keeps serving older rows. Every
row here is on ``main`` and every patch applies at ``base.sha``. The design is
driven by three properties of the repo.

1. THE HISTORY IS ~1.8 GB, A CHECKOUT IS ~150 MB.
   The base holds only the toolchain; each PR image fetches its own
   ``base.sha`` at depth 1, so the repo has exactly one commit and the
   hardening pass only has to drop the remote and refs.

2. TOOLCHAIN.
   ``.nvmrc`` is v24.13 and ``packageManager`` is Yarn 4 (4.12-4.14, via
   corepack) across the whole range. ``yarn install --immutable`` runs once at
   the baseline with ``CI=true``; no fix or test patch in the range touches
   ``package.json`` or ``yarn.lock``. The Yarn cache and Electron download
   cache are dropped afterwards (``nodeLinker: node-modules``, nothing reads
   them again).

3. TEST SELECTION AND REPORTING.
   Only the jest unit tests the test patch touches (``*.test.{js,ts,tsx}``)
   are run, through the repo's own ``jest.config.js`` but with
   ``--reporters=default``: the console-baseline reporter would otherwise
   compare against a JSON baseline the test patch also edits. Playwright
   ``*.spec.ts`` files and integration tests (which need a webpack build) are
   skipped. Results come from jest's ``--json`` output, printed one line per
   test as ``<file> > <describe> > <test>``, plus one line per file so a suite
   that fails to load (e.g. a missing module before the fix) still counts as a
   failure.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.13-bookworm"

_TEST_FILE_RE = re.compile(r"\.test\.(js|ts|tsx)$")
_SKIP_PREFIXES = ("test/integration/", "development/webpack/", "test/e2e/tests/")


def _test_files(test_patch: str) -> list[str]:
    """The jest unit test files the test patch touches."""
    files: set[str] = set()
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", test_patch or "", re.M):
        for path in m.groups():
            if _TEST_FILE_RE.search(path) and not path.startswith(_SKIP_PREFIXES):
                files.add(path)
    return sorted(files)


class MetamaskExtensionImageBase(Image):
    """Shared toolchain image: Node 24.13 with corepack enabled.

    Holds no repository, so it is valid for every PR. Emitting the BuildKit
    syntax directive opts out of ``DockerfileEnhancer``, which would otherwise
    add a full clone pinned to one PR's ``BASE_COMMIT``; this file therefore
    emits the proxy, cert and label infrastructure itself.
    """

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
        return _NODE_IMAGE

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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    NPM_CONFIG_UPDATE_NOTIFIER=false

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN corepack enable && node -v

WORKDIR /home/

CMD ["/bin/bash"]
"""


class MetamaskExtensionImageDefault(Image):
    """Per-PR image: fetches ``base.sha`` at depth 1 and installs dependencies."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return MetamaskExtensionImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        tests = " ".join(_test_files(self.pr.test_patch))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
            File(
                ".",
                "jest_report.js",
                r"""// Prints jest --json results as one "MSB <STATUS> <name>" line per test
// and per file; see point 3 of the config module docstring.
const fs = require('fs');

const [, , jsonPath, root] = process.argv;
if (!fs.existsSync(jsonPath)) {
  console.log('jest_report: no jest results (jest crashed before writing them)');
  process.exit(0);
}
const results = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
const prefix = root.endsWith('/') ? root : `${root}/`;
const STATUS = { passed: 'PASS', failed: 'FAIL' };

for (const file of results.testResults || []) {
  const rel = file.name.startsWith(prefix) ? file.name.slice(prefix.length) : file.name;
  const seen = new Map();
  let fileFailed = file.status === 'failed';
  for (const t of file.assertionResults || []) {
    let name = [rel, ...t.ancestorTitles, t.title].join(' > ');
    const n = (seen.get(name) || 0) + 1;
    seen.set(name, n);
    if (n > 1) {
      name = `${name} #${n}`;
    }
    const status = STATUS[t.status] || 'SKIP';
    if (status === 'FAIL') {
      fileFailed = true;
    }
    console.log(`MSB ${status} ${name}`);
  }
  console.log(`MSB ${fileFailed ? 'FAIL' : 'PASS'} ${rel}`);
}
""",
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
# Shared test driver for all three stages: the jest unit test files the test
# patch touches, minus any that do not exist in this tree yet.
FILES=()
for f in [[TESTS]]; do
    if [ -f "$f" ]; then
        FILES+=("$f")
    fi
done

if [ ${#FILES[@]} -eq 0 ]; then
    echo "run_tests: none of the target test files exist in this tree, no tests to run"
    exit 0
fi

echo "run_tests: ${FILES[*]}"
rm -f /tmp/jest-results.json
CI=true NODE_OPTIONS=--max-old-space-size=4096 timeout 3600 \
    node --expose-gc ./node_modules/jest/bin/jest.js \
    --ci --reporters=default --maxWorkers=2 --forceExit --passWithNoTests \
    --json --outputFile=/tmp/jest-results.json \
    --runTestsByPath "${FILES[@]}" 2>&1 || true
node /home/jest_report.js /tmp/jest-results.json "$(pwd)"
""".replace("[[TESTS]]", tests),
            ),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

# Depth-1 fetch of the one commit this instance is anchored to; see point 1
# of the config module docstring. Retried: DNS to github.com flakes mid-build.
rm -rf /home/[[REPO]]
git init -q /home/[[REPO]]
cd /home/[[REPO]]
git remote add origin https://github.com/[[ORG]]/[[REPO]].git
for i in 1 2 3 4 5; do
    git fetch --no-tags --depth 1 origin [[SHA]] && break
    echo "prepare: fetch failed (attempt $i), retrying" >&2
    [ "$i" -lt 5 ] || exit 1
    sleep 15
done
git checkout -q --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "[[SHA]]"
bash /home/check_git_changes.sh

# See point 2 of the config module docstring.
export CI=true
for i in 1 2 3 4 5; do
    yarn install --immutable && break
    echo "prepare: yarn install failed (attempt $i), retrying" >&2
    [ "$i" -lt 5 ] || exit 1
    sleep 30
done
rm -rf .yarn/cache /root/.cache/electron
test -x node_modules/jest/bin/jest.js

bash /home/check_git_changes.sh
git apply --check /home/test.patch /home/fix.patch
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO]]
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The repo already holds a single commit (depth-1 fetch); this pass
        # removes the remote and every ref so nothing can be fetched or read
        # beyond base.sha, and asserts that state.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    rm -f .git/shallow.lock .git/FETCH_HEAD; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "1"

CMD ["/bin/bash"]
"""


@Instance.register("MetaMask", "metamask_extension_46716_to_40459")
class MetamaskExtension46716To40459(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MetamaskExtensionImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Lines emitted by jest_report.js; jest's own console output never
        # starts with the "MSB " marker.
        line_re = re.compile(r"^MSB (PASS|FAIL|SKIP) (.+)$")
        for raw in test_log.splitlines():
            m = line_re.match(raw.rstrip())
            if not m:
                continue
            status, name = m.groups()
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
