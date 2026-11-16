import os
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_NODE_IMAGE = "node:22.23.5-bookworm"
_REPORTER = "/home/mswe-vitest-reporter.mjs"
_RESULTS = "/tmp/mswe-vitest.txt"
_BUILD_PACKAGES = ("cli", "eslint-parser")
_INSTALL_ATTEMPTS = 3

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_LINE = re.compile(r"^MSWE_RESULT (PASSED|FAILED|SKIPPED) :: (\S.*)$")

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_VITEST_REPORTER = r"""import fs from 'node:fs';

const ROOT = '/home/__REPO__/';
const OUT = process.env.MSWE_RESULTS;
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
const rel = (f) => (f.startsWith(ROOT) ? f.slice(ROOT.length) : f);

export default class MsweReporter {
	onTestRunEnd(testModules, unhandledErrors, reason) {
		const out = [];
		const counts = {PASSED: 0, FAILED: 0, SKIPPED: 0};
		const seen = new Map();
		const emit = (status, name) => {
			const n = (seen.get(name) || 0) + 1;
			seen.set(name, n);
			counts[status]++;
			out.push('MSWE_RESULT ' + status + ' :: ' + (n > 1 ? name + ' #' + n : name));
		};
		const modules = [...testModules].map((m) => ({
			m,
			prefix: (m.project.name || '<root>') + ' > ' + rel(m.moduleId),
		}));
		modules.sort((a, b) => (a.prefix < b.prefix ? -1 : a.prefix > b.prefix ? 1 : 0));
		for (const {m, prefix} of modules) {
			const tests = [...m.children.allTests()];
			if (m.state() === 'failed' && !tests.some((t) => t.result().state === 'failed')) {
				emit('FAILED', prefix + ' > <suite failed to run>');
				for (const e of m.errors()) out.push('MSWE_LOG ' + clean(e.message).slice(0, 300));
			}
			for (const t of tests) {
				const r = t.result();
				const status = r.state === 'passed' ? 'PASSED' : r.state === 'failed' ? 'FAILED' : 'SKIPPED';
				emit(status, prefix + ' > ' + clean(t.fullName));
				if (status === 'FAILED') {
					for (const e of r.errors || []) out.push('MSWE_LOG ' + clean(e.message).slice(0, 300));
				}
			}
		}
		out.push(
			'MSWE_SUMMARY vitest reason=' + reason + ' modules=' + modules.length +
				' passed=' + counts.PASSED + ' failed=' + counts.FAILED +
				' skipped=' + counts.SKIPPED + ' unhandled=' + unhandledErrors.length,
		);
		fs.writeFileSync(OUT, out.join('\n') + '\n');
	}
}
"""

_ENV = """export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
export npm_config_verify_deps_before_run=false
export pnpm_config_verify_deps_before_run=false"""


def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _build_block() -> str:
    return "\n".join(
        f'(cd packages/{pkg} && pnpm build)' for pkg in _BUILD_PACKAGES
    )


def _install_block() -> str:
    return f"""for attempt in $(seq 1 {_INSTALL_ATTEMPTS}); do
    if pnpm install --prod false --frozen-lockfile; then
        break
    fi
    if [ "$attempt" -eq {_INSTALL_ATTEMPTS} ]; then
        exit 1
    fi
    sleep 15
done"""


def _test_block() -> str:
    return f"""rm -f {_RESULTS}
rc=0
MSWE_RESULTS={_RESULTS} node_modules/.bin/vitest run --maxWorkers=4 --reporter={_REPORTER} || rc=$?
if [ ! -s {_RESULTS} ]; then
    echo "vitest exited with code $rc without writing {_RESULTS}"
    exit "$(( rc == 0 ? 1 : rc ))"
fi
cat {_RESULTS}"""


def _prepare_script(repo: str, sha: str) -> str:
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        _ENV,
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        """PNPM_VERSION=$(node -p "require('./package.json').packageManager.split('@')[1]")
npm install -g "pnpm@$PNPM_VERSION"
node --version
pnpm --version""",
        _install_block(),
        _build_block(),
        f"""test -s {_REPORTER}
test -x node_modules/.bin/vitest
node_modules/.bin/vitest --version
test_files=$(node_modules/.bin/vitest list --filesOnly | wc -l)
echo "prepare: vitest lists $test_files test files"
test "$test_files" -gt 0""",
    )


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{repo}", apply) if line)
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _ENV,
        setup,
        _build_block(),
        _test_block(),
    )


class Ripple1254To1254ImageBase(Image):
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
        hi, lo = re.search(r"_(\d+)_to_(\d+)$", _ERA).groups()
        return f"base-{lo}_to_{hi}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential python3 pkg-config libsecret-1-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --system --add safe.directory '*' && node --version && npm --version"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class Ripple1254To1254ImageDefault(Image):
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
        return Ripple1254To1254ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "mswe-vitest-reporter.mjs", _VITEST_REPORTER.replace("__REPO__", repo)),
            File(".", "prepare.sh", _prepare_script(repo, self.pr.base.sha)),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _ERA)
class RIPPLE_1254_TO_1254(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Ripple1254To1254ImageDefault(self.pr, self._config)

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
        buckets = {"PASSED": passed_tests, "FAILED": failed_tests, "SKIPPED": skipped_tests}
        for raw in _ANSI.sub("", test_log).splitlines():
            match = _RESULT_LINE.match(raw.rstrip())
            if match:
                buckets[match.group(1)].add(match.group(2))
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
