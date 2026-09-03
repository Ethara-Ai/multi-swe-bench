# Standalone Karma-era config for a small set of Vue PRs whose default scripts
# in vue.py produce empty reports. Structured after
# multi_swe_bench/harness/repos/java/javaparser/javaparser.py: base image +
# per-PR default image + Instance subclass all defined here, no inheritance
# from vue.py. The plain `("vuejs", "vue")` fallback in vue.py continues to
# serve every other Vue PR; the per-PR number_interval registrations at the
# bottom of this file re-route only the listed PRs.
#
#   pr-3734  Vue 1.x-era. `yarn install --ignore-scripts || true` in the shared
#            prepare.sh silently swallows install failures, so
#            node_modules/.bin/karma is missing and every stage exits (0,0,0).
#            Fix: yarn -> npm fallback + hard sanity check that karma is
#            actually installed before the image is considered built.
#
#   pr-4076  test patch touches test/ssr/ssr-string.spec.js, but Karma is a
#            browser runner and never globs test/ssr/**. All three stages log
#            identical 665 passes, so no test transitions and f2p/p2p/s2p/n2p
#            are empty. Fix: after karma, also run jasmine-node over test/ssr
#            and normalize its output to the same TESTPASS/TESTFAIL/TESTSKIP
#            markers parse_log understands.
#
#   pr-4138  Currently valid=True from style.spec.js, but its ssr-string.spec.js
#            additions are silently missed by Karma. Adding the SSR runner
#            picks those up too.

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.vuejs.vue import Vue


# Headless-Chrome karma override placed beside the repo's own karma.unit.config.js.
# Same content as vue.py's KARMA_HEADLESS_JS but duplicated so this file stays
# self-contained (matches the javaparser.py reference pattern).
KARMA_HEADLESS_JS = r"""const base = require("./karma.base.config.js")

function FlatReporter(baseReporterDecorator) {
  baseReporterDecorator(this)
  const self = this
  this.onSpecComplete = function (browser, result) {
    const suite = (result.suite || []).join(" > ")
    const name = suite ? suite + " > " + result.description : result.description
    const tag = result.skipped ? "TESTSKIP" : (result.success ? "TESTPASS" : "TESTFAIL")
    self.write(tag + " " + name + "\n")
  }
}
FlatReporter.$inject = ["baseReporterDecorator"]

module.exports = function (config) {
  config.set(Object.assign({}, base, {
    browsers: ["ChromeHeadlessCI"],
    customLaunchers: {
      ChromeHeadlessCI: {
        base: "Chrome",
        flags: [
          "--headless",
          "--no-sandbox",
          "--disable-gpu",
          "--disable-dev-shm-usage",
          "--remote-debugging-port=9222"
        ]
      }
    },
    reporters: ["flat"],
    singleRun: true,
    plugins: [
      "karma-jasmine",
      "karma-webpack",
      "karma-sourcemap-loader",
      "karma-chrome-launcher",
      { "reporter:flat": ["type", FlatReporter] }
    ]
  }))
}
"""


# Runtime network blackhole -- rewrites every github.com URL form to a dead
# address so an evaluated model cannot recover the stripped fix by re-cloning.
GIT_BLACKHOLE = r"""RUN BH="https://0.0.0.0:1/"; \
    git config --system url."$BH".insteadOf "https://github.com/"; \
    git config --system url."$BH".insteadOf "http://github.com/"; \
    git config --system url."$BH".insteadOf "git://github.com/"; \
    git config --system url."$BH".insteadOf "ssh://git@github.com/"; \
    git config --system url."$BH".insteadOf "git@github.com:"; \
    git config --system url."$BH".insteadOf "https://codeload.github.com/"; \
    git config --system protocol.allow never; \
    git config --system protocol.file.allow always; \
    git config --system --unset-all credential.helper 2>/dev/null || true"""


# Self-diagnosing patch application shared by test-run.sh / fix-run.sh.
# Progressive fallbacks (plain -> --3way -> patch --fuzz) so a single hiccup
# doesn't drop the whole stage to (0,0,0). Never uses --reject.
APPLY_PATCH_SH = r'''apply_patch() {
  local p="$1"
  [ -s "$p" ] || { echo "apply_patch: $p missing/empty, skipping"; return 0; }
  if git apply --whitespace=nowarn "$p" 2>/dev/null; then echo "apply_patch: applied $p (git apply)"; return 0; fi
  if git apply --whitespace=nowarn --3way "$p" 2>/dev/null; then echo "apply_patch: applied $p (git apply --3way)"; return 0; fi
  if patch -p1 --fuzz=3 --no-backup-if-mismatch -i "$p" >/dev/null 2>&1; then echo "apply_patch: applied $p (patch --fuzz)"; return 0; fi
  echo "apply_patch: FAILED to apply $p -- diagnostics:"; git apply --whitespace=nowarn --3way "$p" 2>&1 | head -20 | sed "s/^/  /"; return 1
}'''


# jasmine-node prints "PASSED / FAILED / SKIPPED" for each spec; the awk block
# rewrites those into the flat TESTPASS/TESTFAIL/TESTSKIP markers that
# parse_log below already handles. Wrapped in `|| true` so a jasmine failure
# does not kill the whole script under `set -o pipefail` -- individual TESTFAIL
# lines still reach the log and drive f2p classification.
SSR_RUN_BLOCK = r"""
# Karma is browser-only and does not glob test/ssr/**. Old Vue also ships a
# jasmine-node SSR suite -- run it separately and normalize the output so
# parse_log picks it up. Only run if the directory exists (Vue 1.x has none).
if [ -d test/ssr ]; then
  if [ ! -x node_modules/.bin/jasmine-node ]; then
    npm install --no-save --no-audit --no-fund jasmine-node@1 >/dev/null 2>&1 || true
  fi
  if [ -x node_modules/.bin/jasmine-node ]; then
    node_modules/.bin/jasmine-node --verbose test/ssr 2>&1 | awk '
      /- it / {name=$0; sub(/^[[:space:]]*- it /,"",name); next}
      /passed/ && name!="" {print "TESTPASS ssr > " name; name=""; next}
      /failed/ && name!="" {print "TESTFAIL ssr > " name; name=""; next}
      /skipped/ && name!="" {print "TESTSKIP ssr > " name; name=""; next}
      {print}
    ' || true
  else
    echo "ssr: jasmine-node unavailable, skipping test/ssr"
  fi
fi
"""


class VueSsrImageBase(Image):
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
        return "node:12-bullseye"

    def image_tag(self) -> str:
        return "base-karma-ssr"

    def workdir(self) -> str:
        return "base-karma-ssr"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org, repo = self.pr.org, self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

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
      org.opencontainers.image.description="{org}/{repo} Docker image (SSR-aware)" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN set -eux; \\
    mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs; \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem; \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem; \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem; \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates jq chromium \\
    && rm -rf /var/lib/apt/lists/*
RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class VueSsrImageDefault(Image):
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
        return VueSsrImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-ssr"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                "prepare.sh",
                # Checkout lives here; hardening (blackhole/scrub/asserts) stays
                # in the PR Dockerfile and runs AFTER this script.
                """#!/bin/bash
set -e
export CI=true

cd /home/{pr.repo}

# --- Git checkout (reference-style, idempotent across rebuilds) ---
git reset --hard
bash /home/check_git_changes.sh
git remote get-url origin >/dev/null 2>&1 \\
  || git remote add origin https://github.com/{pr.org}/{pr.repo}.git
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null \\
  || git fetch --depth=1 origin {pr.base.sha}
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# --- Karma config placement (JS is inlined below; no separate file is shipped) ---
if [ -f test/unit/karma.unit.config.js ]; then
  KCFG=test/unit/karma.headless.js
else
  KCFG=build/karma.headless.js
fi
mkdir -p "$(dirname "$KCFG")"
cat > "$KCFG" << 'KARMA_EOF'
__KARMA_HEADLESS_JS__
KARMA_EOF

# --- Deps install (must complete before Dockerfile-level blackhole activates) ---
export PUPPETEER_SKIP_DOWNLOAD=true
if ! yarn install --ignore-scripts; then
  echo "prepare.sh: yarn install failed, falling back to npm"
  npm install --ignore-scripts --no-audit --no-fund
fi

# Hard sanity check -- missing karma is the pr-3734 root cause (all stages
# exited (0,0,0) silently under the original `|| true`).
test -x node_modules/.bin/karma || {{ echo "prepare.sh: karma not installed after dep install"; exit 1; }}

""".format(pr=self.pr).replace("__KARMA_HEADLESS_JS__", KARMA_HEADLESS_JS.rstrip("\n")),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"
{ssr}
""".format(pr=self.pr, ssr=SSR_RUN_BLOCK),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
git checkout -f -- . 2>/dev/null || git reset --hard 2>/dev/null || true
{apply}
apply_patch /home/test.patch
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"
{ssr}
""".format(pr=self.pr, apply=APPLY_PATCH_SH, ssr=SSR_RUN_BLOCK),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
git checkout -f -- . 2>/dev/null || git reset --hard 2>/dev/null || true
{apply}
apply_patch /home/test.patch
apply_patch /home/fix.patch
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"
{ssr}
""".format(pr=self.pr, apply=APPLY_PATCH_SH, ssr=SSR_RUN_BLOCK),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for f in self.files():
            copy_commands += f"COPY {f.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{repo}
RUN bash /home/prepare.sh

RUN set -eux; \\
    BH="https://0.0.0.0:1/"; \\
    git config --system url."$BH".insteadOf "https://github.com/"; \\
    git config --system url."$BH".insteadOf "http://github.com/"; \\
    git config --system url."$BH".insteadOf "git://github.com/"; \\
    git config --system url."$BH".insteadOf "ssh://git@github.com/"; \\
    git config --system url."$BH".insteadOf "git@github.com:"; \\
    git config --system url."$BH".insteadOf "https://codeload.github.com/"; \\
    git config --system protocol.allow never; \\
    git config --system protocol.file.allow always; \\
    git config --system --unset-all credential.helper 2>/dev/null || true

RUN set -eux; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
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


class VueSsr(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._vue_fallback: Optional[Vue] = None

    def _vue(self) -> Vue:
        # Non-SSR PRs that still land on the "vuejs/vue" key (this file's
        # Instance.register overwrites vue.py's by import order) are delegated
        # straight back to vue.py's Vue router. Keeps the 5-PR SSR override
        # safe against future JSONL additions arriving with an empty
        # number_interval.
        if self._vue_fallback is None:
            self._vue_fallback = Vue(self._pr, self._config)
        return self._vue_fallback

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number in _SSR_KARMA_PRS:
            return VueSsrImageDefault(self.pr, self._config)
        return self._vue().dependency()

    def run(self, run_cmd: str = "") -> str:
        if self.pr.number in _SSR_KARMA_PRS:
            return run_cmd or "bash /home/run.sh"
        return self._vue().run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if self.pr.number in _SSR_KARMA_PRS:
            return test_patch_run_cmd or "bash /home/test-run.sh"
        return self._vue().test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if self.pr.number in _SSR_KARMA_PRS:
            return fix_patch_run_cmd or "bash /home/fix-run.sh"
        return self._vue().fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        if self.pr.number not in _SSR_KARMA_PRS:
            return self._vue().parse_log(test_log)

        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        # Strip ANSI/colour first -- karma + webpack emit coloured output.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Karma FlatReporter + jasmine-node awk-wrapper both emit lines shaped
        # "TESTPASS|TESTFAIL|TESTSKIP <fully-qualified name>". This is the only
        # marker format these PRs produce (all are Karma-era; no vitest).
        re_marker = re.compile(r"^(TESTPASS|TESTFAIL|TESTSKIP) (.+?)\s*$")
        for line in log.splitlines():
            m = re_marker.match(line)
            if not m:
                continue
            tag, name = m.group(1), m.group(2)
            if tag == "TESTPASS":
                passed_tests.add(name)
            elif tag == "TESTFAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # Enforce TestResult disjoint-set invariants (a flaky test reported
        # both pass and fail counts as failed).
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


# Route via the plain "vuejs/vue" key. Instance.create() only consults per-PR
# number_interval keys when pr.number_interval is set on the PullRequest; the
# raw jsonl for these PRs has no number_interval field, so number-based
# registrations never fire. This file is imported last in
# typescript/vuejs/__init__.py (line 6, after vue.py at line 5), so this
# registration wins the collision with vue.py's Vue class. Every VueSsr method
# gates on _SSR_KARMA_PRS: the 5 dataset PRs get the SSR-aware images/scripts
# below; every other PR is delegated back to vue.py's Vue router so a future
# JSONL that adds a non-SSR record under this key does not break.
_SSR_KARMA_PRS = {3734, 3988, 4022, 4076, 4138}
Instance.register("vuejs", "vue")(VueSsr)

# Extra per-PR keys so build_dataset can consume the gen_report-produced
# generated jsonl directly (that file sets number_interval='<N>' on each
# record; Instance.create() then looks up f"{org}/{number_interval}" and
# would fail without these). Same VueSsr class is reused for every key —
# the actual routing decision still happens inside VueSsr via _SSR_KARMA_PRS.
for _pr_num in ("3734", "3988", "4022", "4076", "4138"):
    Instance.register("vuejs", _pr_num)(VueSsr)
