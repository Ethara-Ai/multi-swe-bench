import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_TAG = "base-174_to_11"
NODE_IMAGE = "node:20-bookworm"
PNPM_VERSION = "8.15.8"
BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="

CHECK_GIT_CHANGES = r"""#!/bin/bash
set -euo pipefail

cd "$1"

if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

EMIT_RESULTS = r"""const fs = require("fs");
const path = require("path");

const resultsFile = process.argv[2];
const root = process.argv[3];

let report;
try {
  report = JSON.parse(fs.readFileSync(resultsFile, "utf8"));
} catch (err) {
  process.stdout.write("MSWEBENCH_EMIT_ERROR " + err.message + "\n");
  process.exit(0);
}

let total = 0;
for (const suite of report.testResults || []) {
  const file = path.relative(root, suite.name).split(path.sep).join("/");
  const assertions = suite.assertionResults || [];
  if (assertions.length === 0 && suite.status === "failed") {
    process.stdout.write("TESTCASE " + file + "::Test suite failed to run FAILED\n");
    total += 1;
    continue;
  }
  for (const assertion of assertions) {
    let status = "SKIPPED";
    if (assertion.status === "passed") {
      status = "PASSED";
    } else if (assertion.status === "failed") {
      status = "FAILED";
    }
    const title = (assertion.fullName || assertion.title || "").replace(/\s+/g, " ").trim();
    process.stdout.write("TESTCASE " + file + "::" + title + " " + status + "\n");
    total += 1;
  }
}

process.stdout.write("MSWEBENCH_TOTAL " + total + "\n");
"""

PROVISION_BODY = r"""export DEBIAN_FRONTEND=noninteractive
export npm_config_update_notifier=false

npm install -g "pnpm@${PNPM_VERSION}"

cd "$REPO_DIR/apps/api"
pnpm install --frozen-lockfile
"""

GATE_BODY = r"""test -x node_modules/.bin/jest
node -e "require('jest/package.json'); require('ts-jest/package.json'); require('typescript'); require.resolve('jest-fetch-mock'); require('supertest'); console.log('DEPS_OK')"
node_modules/.bin/jest --listTests --testPathIgnorePatterns=src/__tests__/e2e_noAuth/ --testPathIgnorePatterns=src/__tests__/e2e_withAuth/
"""

TEST_BODY = r"""export CI=true
export npm_config_update_notifier=false

cd "$REPO_DIR/apps/api"

pnpm install --frozen-lockfile --prefer-offline

rm -f /home/results.json

set +e
timeout --kill-after=60 3600 node_modules/.bin/jest --ci --forceExit --watchAll=false --passWithNoTests --testPathIgnorePatterns=src/__tests__/e2e_noAuth/ --testPathIgnorePatterns=src/__tests__/e2e_withAuth/ --json --outputFile=/home/results.json
set -e

test -s /home/results.json

echo "===== BEGIN TEST DETAIL ====="
node /home/emit_results.js /home/results.json "$REPO_DIR"
echo "===== END TEST DETAIL ====="
"""


class Firecrawl174To11ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "str | Image":
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

{self.global_env}

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

{clone}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Firecrawl174To11ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "str | Image":
        return Firecrawl174To11ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"

        prepare = "#!/bin/bash\n"
        prepare += "set -euo pipefail\n"
        prepare += "\n"
        prepare += f'REPO_DIR="{repo_dir}"\n'
        prepare += f'PNPM_VERSION="{PNPM_VERSION}"\n'
        prepare += "\n"
        prepare += f"cd {repo_dir}\n"
        prepare += "\n"
        prepare += "git reset --hard\n"
        prepare += "git clean -fdx\n"
        prepare += f"bash /home/check_git_changes.sh {repo_dir}\n"
        prepare += "\n"
        prepare += f"git checkout --detach {self.pr.base.sha}\n"
        prepare += f"bash /home/check_git_changes.sh {repo_dir}\n"
        prepare += "\n"
        prepare += "cat > /home/emit_results.js <<'MSWEBENCH_JS_EOF'\n"
        prepare += EMIT_RESULTS
        prepare += "MSWEBENCH_JS_EOF\n"
        prepare += "\n"
        prepare += PROVISION_BODY
        prepare += "\n"
        prepare += GATE_BODY

        run = "#!/bin/bash\n"
        run += "set -eo pipefail\n"
        run += f'REPO_DIR="{repo_dir}"\n'
        run += "\n"
        run += TEST_BODY

        test_run = "#!/bin/bash\n"
        test_run += "set -eo pipefail\n"
        test_run += f'REPO_DIR="{repo_dir}"\n'
        test_run += "\n"
        test_run += f'cd "{repo_dir}"\n'
        test_run += "git apply --whitespace=nowarn /home/test.patch\n"
        test_run += "\n"
        test_run += TEST_BODY

        fix_run = "#!/bin/bash\n"
        fix_run += "set -eo pipefail\n"
        fix_run += f'REPO_DIR="{repo_dir}"\n'
        fix_run += "\n"
        fix_run += f'cd "{repo_dir}"\n'
        fix_run += "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
        fix_run += "\n"
        fix_run += TEST_BODY

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

RUN set -eux; \\
    git checkout --detach {sha}; \\
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

{self.clear_env}
"""


class Firecrawl174To11Instance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Firecrawl174To11ImageDefault(self.pr, self._config)

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

        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in clean.splitlines():
            stripped = line.strip()
            if stripped.startswith(BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(END_MARKER):
                in_detail = False
                continue
            if not in_detail:
                continue

            match = case_re.match(stripped)
            if not match:
                continue
            name = match.group(1).strip()
            status = match.group(2)
            if not name:
                continue
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
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


@Instance.register("firecrawl", "firecrawl_174_to_11")
class FIRECRAWL_174_TO_11(Firecrawl174To11Instance):
    pass
