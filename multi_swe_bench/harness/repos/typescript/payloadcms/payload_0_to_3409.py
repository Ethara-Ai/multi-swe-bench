import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .payload import (
    payload_parse_log,
    _CHECK_GIT_CHANGES_SH,
    _STRIP_BINARY_DIFFS_PY,
    _START_MONGO_SH,
)

_INTERVAL_NAME = "payload_0_to_3409"


class PayloadV1ImageBase(Image):

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
        return "node:18"

    def image_tag(self) -> str:
        return "base-{name}".format(name=_INTERVAL_NAME)

    def workdir(self) -> str:
        return "base-{name}".format(name=_INTERVAL_NAME)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo}".format(
                org=self.pr.org, repo=self.pr.repo
            )
        else:
            code = "COPY {repo} /home/{repo}".format(repo=self.pr.repo)

        return """FROM {image_name}

{global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN yarn --version || npm install -g yarn
RUN npm install -g cross-env
RUN apt-get update && \\
    apt-get install -y gnupg curl wget ca-certificates lsb-release && \\
    wget -qO - https://pgp.mongodb.com/server-6.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-6.0.gpg && \\
    echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-6.0.gpg] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/6.0 multiverse" \\
    > /etc/apt/sources.list.d/mongodb-org-6.0.list && \\
    apt-get update && \\
    apt-get install -y mongodb-org && \\
    mkdir -p /data/db && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


class PayloadV1ImageDefault(Image):

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
        return PayloadV1ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "strip_binary_diffs.py", _STRIP_BINARY_DIFFS_PY),
            File(".", "start-mongo.sh", _START_MONGO_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

yarn install || true

yarn build || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export MONGOMS_SYSTEM_BINARY=/usr/bin/mongod
export MONGOMS_STORAGE_ENGINE=wiredTiger

bash /home/start-mongo.sh || true

cd /home/{repo}

export PAYLOAD_CONFIG_PATH=demo/payload.config.ts
export NODE_ENV=test
export DISABLE_LOGGING=true

yarn test:int || true

echo "=== Test run complete ==="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export MONGOMS_SYSTEM_BINARY=/usr/bin/mongod
export MONGOMS_STORAGE_ENGINE=wiredTiger

bash /home/start-mongo.sh || true

cd /home/{repo}

python3 /home/strip_binary_diffs.py /home/test.patch
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true

yarn install || true
yarn build || true

export PAYLOAD_CONFIG_PATH=demo/payload.config.ts
export NODE_ENV=test
export DISABLE_LOGGING=true

yarn test:int || true

echo "=== Test run complete ==="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export MONGOMS_SYSTEM_BINARY=/usr/bin/mongod
export MONGOMS_STORAGE_ENGINE=wiredTiger

bash /home/start-mongo.sh || true

cd /home/{repo}

python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}

yarn install || true
yarn build || true

export PAYLOAD_CONFIG_PATH=demo/payload.config.ts
export NODE_ENV=test
export DISABLE_LOGGING=true

yarn test:int || true

echo "=== Test run complete ==="
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        return """FROM {name}:{tag}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

{clear_env}

""".format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            clear_env=self.clear_env,
        )


@Instance.register("payloadcms", _INTERVAL_NAME)
class PAYLOAD_0_TO_3409(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PayloadV1ImageDefault(self.pr, self._config)

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
        return payload_parse_log(test_log)
