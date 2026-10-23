import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .mermaid import _CHECK_GIT_CHANGES_SH

_NODE_IMAGE = "node:22-bookworm"
_INTERVAL_NAME = "mermaid_233_to_233"

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

_LANG_ENV = """ENV CI=true \\
    NODE_PATH=/opt/mswb/node_modules \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false
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

mkdir -p /opt/mswb
cd /opt/mswb
npm init -y > /dev/null
npm install --no-package-lock mocha@10.8.2 chai@4.5.0 jsdom@24.1.3

cd /home/{repo}
node -e "require('mocha'); require('chai'); require('jsdom'); console.log('DEPS_OK')"
"""

_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
found=0
while read -r spec; do
  [ -f "$spec" ] || continue
  found=1
  node /home/mswb_load.js "$spec"
done < <(grep -oP '^\\+\\+\\+ b/\\K.*\\.spec\\.js$' /home/test.patch | sort -u)
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

_MSWB_LOAD_JS = """const fs = require('fs');
const path = require('path');
const Mocha = require('mocha');
const chai = require('chai');
const { JSDOM } = require('jsdom');

const root = '/home/{repo}';
const spec = path.resolve(root, process.argv[2]);
const rel = path.relative(root, spec);
const vendor = path.join(root, 'live_editor/bower_components');
const libs = [
  'angular/angular.js',
  'angular-mocks/angular-mocks.js',
  path.relative(vendor, path.join(path.dirname(spec), '..', path.basename(path.dirname(path.dirname(spec))) + '.js')),
];

const mocha = new Mocha({
  reporter: path.join(__dirname, 'mswb_reporter.js'),
  reporterOptions: { spec: rel },
});
const ctx = {};
mocha.suite.emit('pre-require', ctx, spec, mocha);

const dom = new JSDOM('<!DOCTYPE html><html><head></head><body></body></html>', {
  runScripts: 'outside-only',
  url: 'http://localhost/',
});
const w = dom.window;
w.mocha = true;
w.beforeEach = ctx.beforeEach;
w.afterEach = ctx.afterEach;
for (const lib of libs) {
  const p = path.join(vendor, lib);
  if (fs.existsSync(p)) {
    w.eval(fs.readFileSync(p, 'utf8'));
  } else {
    console.log('MSWB_MISSING_LIB ' + path.relative(root, p));
  }
}

const names = ['describe', 'context', 'it', 'specify', 'before', 'after', 'beforeEach', 'afterEach', 'xdescribe', 'xit'];
const body = new Function(...names, 'module', 'inject', 'expect', 'window', 'angular', fs.readFileSync(spec, 'utf8'));
body(...names.map((n) => ctx[n]), w.module, w.inject, chai.expect, w, w.angular);
mocha.suite.emit('post-require', ctx, spec, mocha);
mocha.run(() => process.exit(0));
"""

_MSWB_REPORTER_JS = """const Base = require('mocha').reporters.Base;

class MswbReporter extends Base {
  constructor(runner, options) {
    super(runner, options);
    const spec = ((options && options.reporterOptions) || {}).spec;
    runner.on('fail', (t, err) => {
      console.log('MSWB_ERROR ' + t.fullTitle() + ': ' + (err && err.message ? err.message.split('\\n')[0] : ''));
    });
    runner.once('end', () => {
      const walk = (suite) => {
        for (const t of suite.tests) {
          const name = t.titlePath().map((x) => x.trim()).join(' > ');
          const status = t.state === 'passed' ? 'PASSED' : t.isPending() ? 'SKIPPED' : 'FAILED';
          console.log('MSWB_RESULT ' + status + ' mocha::' + spec + '::' + name);
        }
        suite.suites.forEach(walk);
      };
      walk(runner.suite);
    });
  }
}

module.exports = MswbReporter;
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = _INFRA_BLOCK.format(org=self.pr.org, repo=self.pr.repo)
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {_NODE_IMAGE}\n\n"
            f"{infra}\n"
            f"{_LANG_ENV}\n"
            "RUN git --version && node --version && npm --version\n\n"
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
            File(".", "prepare.sh", _PREPARE_SH.format(repo=repo)),
            File(".", "run.sh", _RUN_SH.format(repo=repo)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(repo=repo)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(repo=repo)),
            File(".", "run_tests.sh", _RUN_TESTS_SH.format(repo=repo)),
            File(".", "mswb_load.js", _MSWB_LOAD_JS.replace("{repo}", repo)),
            File(".", "mswb_reporter.js", _MSWB_REPORTER_JS),
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


@Instance.register("mermaid-js", _INTERVAL_NAME)
class MERMAID_233_TO_233(Instance):
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


_ROUTE_RE = re.compile(r"^mermaid-js/mermaid_(?P<hi>\d+)_to_(?P<lo>\d+)$")


@Instance.register("mermaid-js", "mermaid")
class MERMAID_ROUTER(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        owners = []
        for name, impl in Instance._registry.items():
            match = _ROUTE_RE.match(name)
            if match and int(match["lo"]) <= pr.number <= int(match["hi"]):
                owners.append((int(match["hi"]) - int(match["lo"]), impl))
        if not owners:
            raise ValueError(f"mermaid-js/mermaid: no config covers pr {pr.number}")
        owners.sort(key=lambda item: item[0])
        return owners[0][1](pr, config, *args, **kwargs)
