import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPORTER = """const path = require('path');

function MochaIds(runner) {
    const root = process.cwd();
    const clean = (s) => String(s).replace(/\\s+/g, ' ').trim();
    const id = (t) => {
        const file = t.file ? path.relative(root, t.file) : 'unknown';
        return [file].concat(t.titlePath().map(clean)).join(' > ');
    };
    runner.on('pass', (t) => console.log('MOCHA_RESULT PASS ' + id(t)));
    runner.on('fail', (t) => console.log('MOCHA_RESULT FAIL ' + id(t)));
    runner.on('pending', (t) => console.log('MOCHA_RESULT SKIP ' + id(t)));
}

module.exports = MochaIds;
"""


def _patch_paths(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)", patch, re.MULTILINE)


def _binary_excludes(*patches: str) -> str:
    paths = set()
    for patch in patches:
        for section in re.split(r"^(?=diff --git )", patch, flags=re.MULTILINE):
            match = re.match(r"diff --git a/\S+ b/(\S+)", section)
            if match and re.search(r"^Binary files .* differ$", section, re.MULTILINE):
                paths.add(match.group(1))
    return "".join(f" --exclude={path}" for path in sorted(paths))


def _spec_files(pr: PullRequest) -> list[str]:
    return sorted(
        path
        for path in _patch_paths(pr.test_patch)
        if re.match(r"test/.+\.test\.m?js$", path)
    )


class ENGINE_9598_TO_9376_ImageBase(Image):
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
        return "node:24-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n    cd /home/{repo} && git rev-parse HEAD >/dev/null'
        else:
            clone = f"COPY {repo} /home/{repo}"

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"{self.clear_env}\n\n" if self.clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

{env_block}ARG TARGETARCH
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates python3 build-essential pkg-config \\
    libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN node --version && npm --version

RUN git config --global --add safe.directory '*'

{clone}

{clear_block}CMD ["/bin/bash"]
"""


class ENGINE_9598_TO_9376_ImageDefault(Image):
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
        return ENGINE_9598_TO_9376_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        excludes = _binary_excludes(self.pr.test_patch, self.pr.fix_patch)
        specs = " ".join(_spec_files(self.pr))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "mocha_ids_reporter.cjs", REPORTER),
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
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

for attempt in 1 2 3 4 5; do
    if npm ci --no-audit --no-fund --progress=false; then break; fi
    if [ "$attempt" -eq 5 ]; then break; fi
    sleep 15
done || true

npx --no-install mocha --version > /dev/null && node -e "require('canvas')" && echo DEPS_OK
""".format(repo=repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{repo}

for spec in {specs}; do
    if [ -f "$spec" ] && ! npx --no-install mocha --dry-run --require test/fixtures.mjs "$spec" > /dev/null 2>&1; then
        echo "EXCLUDED_UNCOMPILABLE_TEST $spec"
        rm -f "$spec"
    fi
done

status=0
npm test -- --reporter /home/mocha_ids_reporter.cjs || status=$?
exit $status
""".format(repo=repo, specs=specs),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
bash /home/run_tests.sh
""".format(repo=repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn{excludes} /home/test.patch
bash /home/run_tests.sh
""".format(repo=repo, excludes=excludes),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn{excludes} /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".format(repo=repo, excludes=excludes),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {name}:{tag}

{env_block}ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK.rstrip()}
{clear_block}"""


@Instance.register("playcanvas", "engine_9598_to_9376")
class ENGINE_9598_TO_9376(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ENGINE_9598_TO_9376_ImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_result = re.compile(r"^MOCHA_RESULT (PASS|FAIL|SKIP) (.+?)\s*$")

        status_map = {
            "PASS": passed_tests,
            "FAIL": failed_tests,
            "SKIP": skipped_tests,
        }

        for line in test_log.splitlines():
            match = re_result.match(line.strip())
            if match:
                status_map[match.group(1)].add(match.group(2))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("playcanvas", "engine")(ENGINE_9598_TO_9376)
