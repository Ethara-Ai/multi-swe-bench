import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


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

    def dependency(self) -> Union[str, "Image"]:
        return "node:12-bullseye"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git curl gnupg tzdata \\
        libssl1.1 libcurl4 \\
    && rm -rf /var/lib/apt/lists/*

# MongoDB 4.4 does not publish arm64 packages for Debian. Copy the mongod/mongo
# binaries from the multi-arch official mongo:4.4 image (Ubuntu focal under the
# hood) instead. Runtime deps satisfied by libssl1.1 + libcurl4 above.
COPY --from=mongo:4.4 /usr/bin/mongod /usr/local/bin/mongod
COPY --from=mongo:4.4 /usr/bin/mongo  /usr/local/bin/mongo

RUN mkdir -p /data/db /var/log/mongodb

WORKDIR /home/

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install --no-audit --no-fund --loglevel=error || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}

mongod --fork --logpath /var/log/mongodb/mongod.log --dbpath /data/db --bind_ip 127.0.0.1 > /dev/null 2>&1 || true
for i in $(seq 1 30); do
  if mongo --quiet --eval 'db.runCommand({{ ping: 1 }}).ok' 127.0.0.1:27017/test 2>/dev/null | grep -q '^1$'; then
    break
  fi
  sleep 1
done

export ATLAS_URI="mongodb://127.0.0.1:27017/test"
export PORT=3001
export CI=true

cd backend
../node_modules/.bin/mocha --reporter json --timeout 15000 --exit 2>&1; echo "TEST_DONE_WITH_EXIT: $?"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{
  echo "Warning: git apply failed cleanly, retrying with --reject"
  git apply --reject --whitespace=nowarn /home/test.patch || true
  find . -name '*.rej' -delete
}}

mongod --fork --logpath /var/log/mongodb/mongod.log --dbpath /data/db --bind_ip 127.0.0.1 > /dev/null 2>&1 || true
for i in $(seq 1 30); do
  if mongo --quiet --eval 'db.runCommand({{ ping: 1 }}).ok' 127.0.0.1:27017/test 2>/dev/null | grep -q '^1$'; then
    break
  fi
  sleep 1
done

export ATLAS_URI="mongodb://127.0.0.1:27017/test"
export PORT=3001
export CI=true

cd backend
../node_modules/.bin/mocha --reporter json --timeout 15000 --exit 2>&1; echo "TEST_DONE_WITH_EXIT: $?"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{
  echo "Warning: combined git apply failed, retrying patches individually with --reject"
  git apply --reject --whitespace=nowarn /home/test.patch || true
  git apply --reject --whitespace=nowarn /home/fix.patch || true
  find . -name '*.rej' -delete
}}

npm install --no-audit --no-fund --loglevel=error || true

mongod --fork --logpath /var/log/mongodb/mongod.log --dbpath /data/db --bind_ip 127.0.0.1 > /dev/null 2>&1 || true
for i in $(seq 1 30); do
  if mongo --quiet --eval 'db.runCommand({{ ping: 1 }}).ok' 127.0.0.1:27017/test 2>/dev/null | grep -q '^1$'; then
    break
  fi
  sleep 1
done

export ATLAS_URI="mongodb://127.0.0.1:27017/test"
export PORT=3001
export CI=true

cd backend
../node_modules/.bin/mocha --reporter json --timeout 15000 --exit 2>&1; echo "TEST_DONE_WITH_EXIT: $?"
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


@Instance.register("csc302-spring-2020", "proj-FakeBirds")
class ProjFakeBirds(Instance):
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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        depth = 0
        start: Optional[int] = None
        json_blocks: list[str] = []
        for i, ch in enumerate(clean_log):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        json_blocks.append(clean_log[start : i + 1])
                        start = None

        for block in json_blocks:
            try:
                data = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue

            for test in data.get("passes", []) or []:
                title = test.get("fullTitle") or test.get("title") or ""
                if title:
                    passed_tests.add(title)

            for test in data.get("failures", []) or []:
                title = test.get("fullTitle") or test.get("title") or ""
                if title:
                    failed_tests.add(title)

            for test in data.get("pending", []) or []:
                title = test.get("fullTitle") or test.get("title") or ""
                if title:
                    skipped_tests.add(title)

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
