from __future__ import annotations
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_START_DBS_SH = r"""#!/bin/bash
# Start MariaDB on port 9910 (gorm default)
mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld 2>/dev/null
if command -v mariadbd &>/dev/null; then
  mariadbd --user=mysql --skip-grant-tables --port=9910 --socket=/run/mysqld/mysqld.sock &
  sleep 3
  mariadb --socket=/run/mysqld/mysqld.sock -e \
    "FLUSH PRIVILEGES; CREATE DATABASE IF NOT EXISTS gorm; \
     DROP USER IF EXISTS 'gorm'@'localhost'; \
     CREATE USER 'gorm'@'localhost' IDENTIFIED BY 'gorm'; \
     GRANT ALL ON gorm.* TO 'gorm'@'localhost'; FLUSH PRIVILEGES;" 2>/dev/null
fi

# Start Postgres on port 9920 (gorm default)
if command -v initdb &>/dev/null || ls /usr/lib/postgresql/*/bin/initdb 2>/dev/null; then
  PG_VER=$(ls /etc/postgresql/ 2>/dev/null | head -1)
  if [ -n "$PG_VER" ]; then
    mkdir -p /run/postgresql && chown postgres:postgres /run/postgresql 2>/dev/null
    rm -rf /tmp/pgdata
    su postgres -c "/usr/lib/postgresql/$PG_VER/bin/initdb -D /tmp/pgdata" >/dev/null 2>&1
    su postgres -c "/usr/lib/postgresql/$PG_VER/bin/pg_ctl -D /tmp/pgdata start -l /tmp/pg.log \
      -o '-c listen_addresses=localhost -c port=9920'" >/dev/null 2>&1
    sleep 2
    su postgres -c "/usr/lib/postgresql/$PG_VER/bin/psql -p 9920 \
      -c \"CREATE USER gorm WITH PASSWORD 'gorm' CREATEDB;\"" >/dev/null 2>&1
    su postgres -c "/usr/lib/postgresql/$PG_VER/bin/psql -p 9920 \
      -c \"CREATE DATABASE gorm OWNER gorm;\"" >/dev/null 2>&1
  fi
fi
"""


class GormImageBase(Image):
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
        return "golang:latest"

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

        org, repo = self.pr.org, self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} base Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

{code}

{self.clear_env}

"""


class GormImageDefault(Image):
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
        return GormImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "start_dbs.sh",
                _START_DBS_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eux

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sqlite3 mariadb-server postgresql >/dev/null 2>&1
rm -rf /var/lib/apt/lists/*

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "$(git rev-parse "{pr.base.sha}")"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD
        git remote remove origin 2>/dev/null || true
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d
        git reflog expire --expire=now --all
        git reflog expire --expire-unreachable=now --all
        git gc --prune=now --aggressive
        rm -f .git/objects/info/alternates
    '
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

bash /home/start_dbs.sh

cd /home/{pr.repo}
go test -v -count=1 ./...

if [ -d tests ]; then
  cd tests
  GORM_DIALECT=sqlite go test -v -count=1 ./... 2>&1 | sed 's/--- \\(PASS\\|FAIL\\|SKIP\\): /--- \\1: sqlite\\//'
  GORM_DIALECT=mysql GORM_DSN="gorm:gorm@unix(/run/mysqld/mysqld.sock)/gorm?charset=utf8&parseTime=True" go test -v -count=1 ./... 2>&1 | sed 's/--- \\(PASS\\|FAIL\\|SKIP\\): /--- \\1: mysql\\//' || true
  cd ..
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

bash /home/start_dbs.sh

cd /home/{pr.repo}
git apply /home/test.patch
go test -v -count=1 ./...

if [ -d tests ]; then
  cd tests
  go get -t ./... || true
  go mod tidy || true
  GORM_DIALECT=sqlite go test -v -count=1 ./... 2>&1 | sed 's/--- \\(PASS\\|FAIL\\|SKIP\\): /--- \\1: sqlite\\//'
  GORM_DIALECT=mysql GORM_DSN="gorm:gorm@unix(/run/mysqld/mysqld.sock)/gorm?charset=utf8&parseTime=True" go test -v -count=1 ./... 2>&1 | sed 's/--- \\(PASS\\|FAIL\\|SKIP\\): /--- \\1: mysql\\//' || true
  cd ..
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

bash /home/start_dbs.sh

cd /home/{pr.repo}
git apply /home/fix.patch
go test -v -count=1 ./...

if [ -d tests ]; then
  cd tests
  go get -t ./... || true
  go mod tidy || true
  GORM_DIALECT=sqlite go test -v -count=1 ./... 2>&1 | sed 's/--- \\(PASS\\|FAIL\\|SKIP\\): /--- \\1: sqlite\\//'
  GORM_DIALECT=mysql GORM_DSN="gorm:gorm@unix(/run/mysqld/mysqld.sock)/gorm?charset=utf8&parseTime=True" go test -v -count=1 ./... 2>&1 | sed 's/--- \\(PASS\\|FAIL\\|SKIP\\): /--- \\1: mysql\\//' || true
  cd ..
fi

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("go-gorm", "gorm")
class Gorm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GormImageDefault(self.pr, self._config)

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
        def get_base_name(test_name: str) -> str:
            for dialect in ("sqlite/", "mysql/"):
                if test_name.startswith(dialect):
                    test_name = test_name[len(dialect):]
                    break
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    base_name = get_base_name(pass_match.group(1))
                    if base_name in failed_tests:
                        continue
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    passed_tests.add(base_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    base_name = get_base_name(fail_match.group(1))
                    if base_name in passed_tests:
                        passed_tests.remove(base_name)
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    failed_tests.add(base_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    base_name = get_base_name(skip_match.group(1))
                    if base_name in passed_tests:
                        continue
                    if base_name not in failed_tests:
                        continue
                    skipped_tests.add(base_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
