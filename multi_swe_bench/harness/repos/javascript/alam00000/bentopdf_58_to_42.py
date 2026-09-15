import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PRS = [42, 58]
BASE_TAG = "base-58_to_42"
NODE_IMAGE = "node:20-bookworm"

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

EMIT_RESULTS = r"""const fs = require('fs');
const path = require('path');

const repoDir = process.argv[2];
const jsonPath = process.argv[3];

const STATUS = {
    passed: 'PASS',
    failed: 'FAIL',
    pending: 'SKIP',
    skipped: 'SKIP',
    todo: 'SKIP',
};

function rel(p) {
    if (!p) return '';
    let r = path.isAbsolute(p) ? path.relative(repoDir, p) : p;
    return r.split(path.sep).join('/');
}

let raw;
try {
    raw = fs.readFileSync(jsonPath, 'utf8');
} catch (e) {
    console.log('MSWEBENCH_EMIT_ERROR no results file: ' + jsonPath);
    process.exit(0);
}

let data;
try {
    data = JSON.parse(raw);
} catch (e) {
    console.log('MSWEBENCH_EMIT_ERROR unparsable results: ' + e.message);
    process.exit(0);
}

let n = 0;
for (const suite of data.testResults || []) {
    const file = rel(suite.name || suite.testFilePath);
    for (const t of suite.assertionResults || []) {
        const status = STATUS[t.status] || 'SKIP';
        const title = t.fullName || [].concat(t.ancestorTitles || [], t.title || []).join(' > ');
        if (!file || !title) continue;
        console.log('MSWEBENCH_TEST ' + status + ' ' + file + '::' + title);
        n += 1;
    }
}
console.log('MSWEBENCH_TOTAL ' + n);
"""

PREPARE_BODY = r"""set -euo pipefail

export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export NPM_CONFIG_UPDATE_NOTIFIER=false
export CI=true

cd "$REPO_DIR"

bash /home/check_git_changes.sh "$REPO_DIR"

npm ci

test -d node_modules
test -x node_modules/.bin/vitest
node_modules/.bin/vitest --version

bash /home/check_git_changes.sh "$REPO_DIR"
"""

TEST_BODY = r"""export CI=true
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false

cd "$REPO_DIR"

rm -f /home/vitest-results.json /home/vitest.log

node_modules/.bin/vitest run --reporter=json --outputFile=/home/vitest-results.json \
    > /home/vitest.log 2>&1 || true

cat /home/vitest.log

node /home/emit_results.js "$REPO_DIR" /home/vitest-results.json
"""


class BentoPdfImageBase(Image):
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


class BentoPdfImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return BentoPdfImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"

        prepare = "#!/bin/bash\n"
        prepare += f'REPO_DIR="{repo_dir}"\n'
        prepare += "\n"
        prepare += "cat > /home/emit_results.js <<'MSWEBENCH_JS_EOF'\n"
        prepare += EMIT_RESULTS
        prepare += "MSWEBENCH_JS_EOF\n"
        prepare += "\n"
        prepare += PREPARE_BODY

        run = "#!/bin/bash\n"
        run += "set -uo pipefail\n"
        run += f'REPO_DIR="{repo_dir}"\n'
        run += "\n"
        run += TEST_BODY

        test_run = "#!/bin/bash\n"
        test_run += "set -uo pipefail\n"
        test_run += f'REPO_DIR="{repo_dir}"\n'
        test_run += "\n"
        test_run += f'cd "{repo_dir}"\n'
        test_run += "git apply --whitespace=nowarn /home/test.patch\n"
        test_run += "\n"
        test_run += TEST_BODY

        fix_run = "#!/bin/bash\n"
        fix_run += "set -uo pipefail\n"
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

RUN git cat-file -e {sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags --depth=2147483647 origin {sha} \\
    || git fetch --no-tags origin "+refs/pull/{self.pr.number}/head:refs/remotes/origin/pr-{self.pr.number}"

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

RUN bash /home/prepare.sh

{self.clear_env}
"""


class BentoPdfInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return BentoPdfImageDefault(self.pr, self._config)

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

        line_re = re.compile(
            r"^MSWEBENCH_TEST (PASS|FAIL|SKIP) (.+?)\s*$", re.MULTILINE
        )

        for match in line_re.finditer(test_log):
            status = match.group(1)
            name = match.group(2).strip()
            if not name:
                continue
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


@Instance.register("alam00000", "bentopdf_58_to_42")
class BENTOPDF_58_TO_42(BentoPdfInstance):
    pass
