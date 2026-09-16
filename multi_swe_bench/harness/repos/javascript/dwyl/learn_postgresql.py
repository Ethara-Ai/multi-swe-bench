import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_BODY = """
PG_VERSION="$(ls /etc/postgresql | sort -V | tail -1)"
PG_HBA="/etc/postgresql/${PG_VERSION}/main/pg_hba.conf"

sed -i -E 's/^((local|host)[[:space:]]+.*[[:space:]]+)(peer|ident|md5|scram-sha-256)[[:space:]]*$/\\1trust/' "$PG_HBA"

pg_ctlcluster "${PG_VERSION}" main start

for _ in $(seq 1 60); do
    if pg_isready -q -h 127.0.0.1 -p 5432; then break; fi
    sleep 1
done

psql -h 127.0.0.1 -U postgres -q -c 'DROP DATABASE IF EXISTS codeface;'
psql -h 127.0.0.1 -U postgres -q -c 'CREATE DATABASE codeface;'

if [ -f schema.sql ]; then
    psql -h 127.0.0.1 -U postgres -d codeface -q -f schema.sql
fi

set +e
timeout --kill-after=30 600 ./node_modules/.bin/tap \\
    --reporter=tap --no-coverage -j1 -t60 \\
    ./test/db.test.js ./test/server.test.js ./test/utils.test.js \\
    > /tmp/tap.out 2>&1
TAP_RC=$?
set -e
if [ "$TAP_RC" -ne 0 ]; then
    echo "NOTE: tap exited ${TAP_RC}"
fi

cat /tmp/tap.out

grep -qE '^1\\.\\.[0-9]+' /tmp/tap.out
"""


class LearnPostgresqlImageBase(Image):
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
        return "node:12.22.12-bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    postgresql postgresql-contrib \\
    ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class LearnPostgresqlImageDefault(Image):
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
        return LearnPostgresqlImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

mkdir -p /home/deps
cd /home/deps
npm init -y > /dev/null

npm install --no-audit --no-fund --loglevel=error \\
    tap@12.6.1 pg@7.18.2 supertest@4.0.2 || true

ln -sfn /home/deps/node_modules /home/{pr.repo}/node_modules

test -x /home/{pr.repo}/node_modules/.bin/tap

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=TEST
export DATABASE_URL=postgres://postgres:@localhost/codeface

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=TEST
export DATABASE_URL=postgres://postgres:@localhost/codeface

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=TEST
export DATABASE_URL=postgres://postgres:@localhost/codeface

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_SUBTEST_HEADER = re.compile(r"^(\s*)#\s*Subtest:\s*(.+?)\s*$")
_RESULT_LINE = re.compile(r"^(\s*)(not ok|ok)\b\s*\d*\s*-?\s*(.*?)\s*$")

_TIME_SUFFIX = re.compile(r"\s*#\s*time=[0-9.]+\s*m?s\s*$")
_DIRECTIVE = re.compile(r"\s*#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

_YAML_OPEN = re.compile(r"^\s*---\s*$")
_YAML_CLOSE = re.compile(r"^\s*\.\.\.\s*$")


def parse_tap_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    stack: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    in_yaml = False

    for raw in clean.splitlines():
        line = raw.rstrip("\r")

        if in_yaml:
            if _YAML_CLOSE.match(line):
                in_yaml = False
            continue
        if _YAML_OPEN.match(line):
            in_yaml = True
            continue

        header = _SUBTEST_HEADER.match(line)
        if header:
            indent = len(header.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, header.group(2)))
            continue

        result = _RESULT_LINE.match(line)
        if not result:
            continue

        indent = len(result.group(1))
        status = result.group(2)
        name = result.group(3)

        skipped = bool(_DIRECTIVE.search(name))
        name = _DIRECTIVE.sub("", name)
        name = _TIME_SUFFIX.sub("", name).strip()

        if not stack or stack[-1][0] != indent or stack[-1][1] != name:
            continue

        path = " > ".join(entry for _, entry in stack)
        stack.pop()

        count = seen.get(path, 0) + 1
        seen[path] = count
        if count > 1:
            path = f"{path} (#{count})"

        if skipped:
            skipped_tests.add(path)
        elif status == "ok":
            passed_tests.add(path)
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


@Instance.register("dwyl", "learn-postgresql")
class LearnPostgresql(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return LearnPostgresqlImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_tap_log(log)
