import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OutlineImageBase(Image):
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
        return "base-14318_to_12167"

    def workdir(self) -> str:
        return "base-14318_to_12167"

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


class OutlineImageDefault(Image):
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
        return OutlineImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = []
        for path in re.findall(
            r"^diff --git a/(.+?) b/", f"{self.pr.test_patch}", re.MULTILINE
        ):
            if re.search(r"\.test\.(ts|tsx)$", path) and path not in test_files:
                test_files.append(path)

        test_command = """rc=0

service postgresql start >/dev/null
until pg_isready -h 127.0.0.1 -p 5432 -q; do sleep 1; done
NODE_ENV=test yarn sequelize db:migrate >/tmp/db-migrate.log 2>&1 || {{ cat /tmp/db-migrate.log; exit 1; }}

present=()
for f in {test_files}; do
    if [ -f "$f" ]; then
        present+=("$f")
    fi
done

if [ "${{#present[@]}}" -gt 0 ]; then
    yarn vitest run --reporter=verbose --no-file-parallelism --maxWorkers=1 --no-color "${{present[@]}}" || rc=$?
fi

exit $rc""".format(test_files=" ".join(test_files))

        reinstall = r"""
if { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | grep -E '(^|/)package\.json$|^yarn\.lock$' >/dev/null; then
    yarn install
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
export TZ=UTC
export LC_ALL=C.UTF-8
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export NODE_OPTIONS=--max-old-space-size=4096
export NODE_EXTRA_CA_CERTS="${{SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}}"

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

apt-get -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y --no-install-recommends postgresql postgresql-contrib
rm -rf /var/lib/apt/lists/*

service postgresql start
until pg_isready -h 127.0.0.1 -p 5432 -q; do sleep 1; done
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE ROLE \\"user\\" WITH LOGIN SUPERUSER PASSWORD 'pass';"
runuser -u postgres -- createdb -O user outline-test

corepack enable
yarn --version
yarn install --immutable

NODE_ENV=test yarn sequelize db:migrate
service postgresql stop

node -e "require.resolve('vitest/package.json'); require.resolve('sequelize/package.json')"
yarn vitest --version
echo "DEPS_OK"

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export TZ=UTC
export LC_ALL=C.UTF-8
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
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
export HUSKY=0
export TZ=UTC
export LC_ALL=C.UTF-8
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
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
export HUSKY=0
export TZ=UTC
export LC_ALL=C.UTF-8
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
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


@Instance.register("outline", "outline_14318_to_12167")
class OUTLINE_14318_TO_12167(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return OutlineImageDefault(self.pr, self._config)

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

        re_test = re.compile(
            r"^\s*([✓✔×✗↓])\s+(?:\|([^|]+)\|\s+)?(\S+\.(?:ts|tsx)) > (.+?)\s*$"
        )
        re_suite_fail = re.compile(
            r"^\s*FAIL\s+(?:\|([^|]+)\|\s+)?(\S+\.(?:ts|tsx))(?:\s+\[.*\])?\s*$"
        )
        re_timing = re.compile(r"\s+\d+(?:\.\d+)?\s*m?s$")
        re_tag = re.compile(r"\s+\[(?:skipped|todo)\]$")

        for line in clean_log.splitlines():
            m = re_test.match(line)
            if m:
                mark, project, path, title = m.groups()
                title = re_tag.sub("", re_timing.sub("", title)).strip()
                if project:
                    name = f"{path} > [{project.strip()}] > {title}"
                else:
                    name = f"{path} > {title}"
                if mark in "✓✔":
                    passed_tests.add(name)
                elif mark in "×✗":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue
            m = re_suite_fail.match(line)
            if m:
                project, path = m.groups()
                if project:
                    failed_tests.add(f"{path} > [{project.strip()}]")
                else:
                    failed_tests.add(path)

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


Instance.register("outline", "outline")(OUTLINE_14318_TO_12167)
