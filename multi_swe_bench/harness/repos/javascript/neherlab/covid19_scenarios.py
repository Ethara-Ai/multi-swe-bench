import re
from typing import Optional, Union
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def jest_override_config(repo: str) -> str:
    """Jest config that wraps the repo's own; the repo is never modified.

    Identical for every PR, so it is baked into the shared base image rather
    than copied into each PR build context.
    """
    return """// Wraps the repo's own jest config; the repo is never modified.
//
// jest.tests.config.js sets `diagnostics.warnOnly: true`, which demotes every
// TypeScript error to a warning.  That hides a real failure: a test importing a
// module that does not exist yet still reports as a passing suite.  Here TS2307
// ("Cannot find module") alone is promoted back to fatal.  Every other code stays
// ignored, because this repo carries pre-existing type errors (implicit-any,
// missing @types, undeclared custom matchers) at every commit in range -- making
// those fatal would fail base, test and fix runs alike and signal nothing.
const base = require('/home/{repo}/config/jest/jest.config.js')

const FATAL = new Set([2307])
const ignoreCodes = []
for (let code = 1000; code <= 19999; code++) {{
  if (!FATAL.has(code)) ignoreCodes.push(code)
}}

function patch(project) {{
  const globals = project.globals || {{}}
  const tsJest = {{ ...(globals['ts-jest'] || {{}}) }}
  tsJest.diagnostics = {{
    ...(tsJest.diagnostics || {{}}),
    warnOnly: false,
    ignoreCodes,
  }}
  return {{ ...project, globals: {{ ...globals, 'ts-jest': tsJest }} }}
}}

module.exports =
  base.projects && base.projects.length
    ? {{ ...base, projects: base.projects.map(patch) }}
    : patch(base)
""".format(repo=repo)


class Covid19ScenariosImageBase(Image):

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
        return "node:12"

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

        # The leading syntax directive makes DockerfileEnhancer return this
        # content verbatim, so the per-PR pinning (git checkout ${BASE_COMMIT}
        # + the hardening block) is NOT injected here.  One base serves every
        # PR: it clones the repo and stops.  Each PR image pins its own sha.
        # The infrastructure the enhancer would otherwise add (proxy/CA args,
        # env, labels, cert symlinks) is reproduced below.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

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

{self.global_env}

WORKDIR /home/
ENV TZ=Etc/UTC
RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\
    sed -i '/security.debian.org/d' /etc/apt/sources.list && \\
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Covid19ScenariosImageDefault(Image):

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
        return Covid19ScenariosImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

# Written here rather than shipped as a build-context file or a Dockerfile
# heredoc: see jest_override_config() for what it does and why.
cat > /home/jest.msb.config.js <<'JEST_CONFIG'
{jest_config}
JEST_CONFIG

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

cp .env.example .env
yarn install --frozen-lockfile || yarn install || true
yarn schema:totypes 2>&1 || true
""".format(pr=self.pr, jest_config=jest_override_config(self.pr.repo)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}

npx jest --config=/home/jest.msb.config.js --verbose --no-watchAll --no-coverage 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude yarn.lock /home/test.patch

# The patch may add dependencies to package.json; prepare.sh installed only
# what the base commit needed.  yarn.lock is excluded above, so resolve fresh.
yarn install 2>&1 || true

yarn schema:totypes 2>&1 || true
npx jest --config=/home/jest.msb.config.js --verbose --no-watchAll --no-coverage 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude yarn.lock /home/test.patch /home/fix.patch

# The patch may add dependencies to package.json; prepare.sh installed only
# what the base commit needed.  yarn.lock is excluded above, so resolve fresh.
yarn install 2>&1 || true

yarn schema:totypes 2>&1 || true
npx jest --config=/home/jest.msb.config.js --verbose --no-watchAll --no-coverage 2>&1 || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Pin + harden here rather than in the base: the base is shared by every
        # PR, so it can only clone.  BASE_COMMIT is baked in per PR image.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

{prepare_commands}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

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

{self.clear_env}

"""


@Instance.register("neherlab", "covid19_scenarios")
class Covid19Scenarios(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Covid19ScenariosImageDefault(self.pr, self._config)

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

        current_suite = None

        # Handles Jest displayName prefix: "PASS test src/..." (not just "PASS src/...")
        re_pass_suite = re.compile(
            r"^PASS\s+(?:[a-zA-Z][-\w]*\s+)?(\S+\.\S+?)(?:\s+\(.+\))?$"
        )
        re_fail_suite = re.compile(
            r"^FAIL\s+(?:[a-zA-Z][-\w]*\s+)?(\S+\.\S+?)(?:\s+\(.+\))?$"
        )

        re_pass_test = re.compile(
            r"^\s*[✔✓]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        )
        re_fail_test = re.compile(
            r"^\s*[×✕✗✘✖]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        )

        re_skipped_test = re.compile(
            r"^\s*○\s+(?:skipped\s+)?(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        )

        current_describe = None

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)
            stripped = line.strip()
            if not stripped:
                continue

            pass_suite = re_pass_suite.match(stripped)
            if pass_suite:
                current_suite = pass_suite.group(1)
                passed_tests.add(current_suite)
                current_describe = None
                continue

            fail_suite = re_fail_suite.match(stripped)
            if fail_suite:
                current_suite = fail_suite.group(1)
                failed_tests.add(current_suite)
                current_describe = None
                continue

            if current_suite and re.match(r"^  \S", line) and not re.match(r"^  [✓✔✕×✗✘✖○]", line):
                current_describe = stripped
                continue

            pass_test = re_pass_test.match(stripped)
            if pass_test:
                test_name = pass_test.group(1).strip()
                if current_describe:
                    test_name = f"{current_describe}:{test_name}"
                if current_suite:
                    test_name = f"{current_suite}:{test_name}"
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            fail_test = re_fail_test.match(stripped)
            if fail_test:
                test_name = fail_test.group(1).strip()
                if current_describe:
                    test_name = f"{current_describe}:{test_name}"
                if current_suite:
                    test_name = f"{current_suite}:{test_name}"
                failed_tests.add(test_name)
                if test_name in passed_tests:
                    passed_tests.remove(test_name)
                continue

            skipped_test = re_skipped_test.match(stripped)
            if skipped_test:
                test_name = skipped_test.group(1).strip()
                if current_describe:
                    test_name = f"{current_describe}:{test_name}"
                if current_suite:
                    test_name = f"{current_suite}:{test_name}"
                skipped_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
