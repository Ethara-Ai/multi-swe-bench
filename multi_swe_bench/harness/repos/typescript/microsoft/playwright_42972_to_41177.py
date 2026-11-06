import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PlaywrightImageBase(Image):
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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base-42972_to_41177"

    def workdir(self) -> str:
        return "base-42972_to_41177"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM node:22-bookworm

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

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class PlaywrightImageDefault(Image):
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
        return PlaywrightImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        library_files = []
        runner_files = []
        for path in re.findall(
            r"^diff --git a/(.+?) b/", f"{self.pr.test_patch}", re.MULTILINE
        ):
            if not re.search(r"\.spec\.(ts|tsx)$", path):
                continue
            if path.startswith("tests/playwright-test/"):
                if path not in runner_files:
                    runner_files.append(path)
            elif re.match(r"tests/(library|page)/", path):
                if path not in library_files:
                    library_files.append(path)

        test_command = """rc=0

npm run build >/tmp/playwright-build.log 2>&1 || {{ tail -n 80 /tmp/playwright-build.log; exit 1; }}

library=()
for f in {library_files}; do
    if [ -f "$f" ]; then
        library+=("$f")
    fi
done

runner=()
for f in {runner_files}; do
    if [ -f "$f" ]; then
        runner+=("$f")
    fi
done

if [ "${{#library[@]}}" -gt 0 ]; then
    echo "##SUITE## tests"
    xvfb-run --auto-servernum --server-args="-screen 0 1280x960x24" -- npx playwright test --config=tests/library/playwright.config.ts --project=chromium-library --project=chromium-page --reporter=list --workers=1 --retries=0 "${{library[@]}}" || rc=$?
fi

if [ "${{#runner[@]}}" -gt 0 ]; then
    echo "##SUITE## tests/playwright-test"
    xvfb-run --auto-servernum --server-args="-screen 0 1280x960x24" -- node ./tests/playwright-test/stable-test-runner/node_modules/@playwright/test/cli test --config=tests/playwright-test/playwright.config.ts --project=playwright-test --reporter=list --workers=1 --retries=0 "${{runner[@]}}" || rc=$?
fi

exit $rc""".format(
            library_files=" ".join(library_files),
            runner_files=" ".join(runner_files),
        )

        reinstall = r"""
if { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | grep -qE '(^|/)package\.json$|^package-lock\.json$'; then
    npm install --no-audit --no-fund
fi
"""

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
                """#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_OPTIONS=--max-old-space-size=4096

apt-get -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates curl git xauth xvfb
rm -rf /var/lib/apt/lists/*

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "{sha}^{{commit}}" 2>/dev/null; then
    git fetch origin "{sha}"
fi
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

npm ci --no-audit --no-fund
npm run build
npx playwright install --with-deps chromium
git status --porcelain

test -f tests/playwright-test/stable-test-runner/node_modules/@playwright/test/cli.js
node -e "require.resolve('playwright-core/package.json')"
npx playwright --version
echo "DEPS_OK"

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
cd /home/{repo}

{test_command}

""".format(repo=self.pr.repo, test_command=test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{reinstall}
{test_command}

""".format(repo=self.pr.repo, reinstall=reinstall, test_command=test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{reinstall}
{test_command}

""".format(repo=self.pr.repo, reinstall=reinstall, test_command=test_command),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
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
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("microsoft", "playwright_42972_to_41177")
class PLAYWRIGHT_42972_TO_41177(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PlaywrightImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        re_suite = re.compile(r"^##SUITE## (\S+)\s*$")
        re_test = re.compile(
            r"^\s*(✓|✔|ok|✘|×|x|-|°|±)\s+\d+\s+(?:\[[^\]]+\]\s+›\s+)?(\S+?\.(?:ts|tsx|js|mjs)):\d+:\d+\s+›\s+(.+?)\s*$"
        )
        re_timing = re.compile(r"\s+\((?:\d+(?:\.\d+)?(?:ms|s|m|h)\s*)+\)$")
        re_retry = re.compile(r"\s+\(retry #\d+\)$")

        prefix = ""
        for line in clean_log.splitlines():
            m = re_suite.match(line)
            if m:
                prefix = m.group(1)
                continue
            m = re_test.match(line)
            if not m or not prefix:
                continue
            mark, path, title = m.groups()
            title = re_retry.sub("", re_timing.sub("", title)).strip()
            title = " > ".join(part.strip() for part in title.split(" › "))
            name = f"{prefix}/{path} > {title}"
            if mark in ("✓", "✔", "ok", "±"):
                passed_tests.add(name)
            elif mark in ("✘", "×", "x"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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


Instance.register("microsoft", "playwright")(PLAYWRIGHT_42972_TO_41177)
