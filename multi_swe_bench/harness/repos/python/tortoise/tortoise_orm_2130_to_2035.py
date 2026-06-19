import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.tortoise.tortoise_orm import parse_pytest_log


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
        return "python:3.12-slim"

    def image_prefix(self) -> str:
        return "envagent"

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

        repo = self.pr.repo
        org = self.pr.org

        # Base image: reference infra format + anti-cheat hardening, built per PR
        # (checks out and hardens at this PR's base.sha). The syntax directive
        # keeps DockerfileEnhancer.enhance() from rewriting it.
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git bash build-essential && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN pip install --upgrade pip setuptools wheel || true
RUN pip install uv || true
RUN cd /home/{repo} && (uv sync --frozen --all-groups --extra asyncpg --extra aiomysql --extra accel --extra psycopg --extra asyncodbc || pip install -e . || true)

{hardening}

CMD ["/bin/bash"]
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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pip install --upgrade pip setuptools wheel || true
pip install uv || true
uv sync --frozen --all-groups --extra asyncpg --extra aiomysql --extra accel --extra psycopg --extra asyncodbc || pip install -e . || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
PYTHONUNBUFFERED=1 TORTOISE_TEST_DB=sqlite://:memory: timeout --signal=KILL 1800 uv run --with pytest-timeout python - <<'PYEOF'
import os, sys, pytest
rc = pytest.main(["tests/", "-v", "--no-header", "-rA", "--tb=no", "-p", "no:cacheprovider", "--continue-on-collection-errors", "--timeout=180"])
sys.stdout.flush(); sys.stderr.flush()
os._exit(int(rc))
PYEOF

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
PYTHONUNBUFFERED=1 TORTOISE_TEST_DB=sqlite://:memory: timeout --signal=KILL 1800 uv run --with pytest-timeout python - <<'PYEOF'
import os, sys, pytest
rc = pytest.main(["tests/", "-v", "--no-header", "-rA", "--tb=no", "-p", "no:cacheprovider", "--continue-on-collection-errors", "--timeout=180"])
sys.stdout.flush(); sys.stderr.flush()
os._exit(int(rc))
PYEOF

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
PYTHONUNBUFFERED=1 TORTOISE_TEST_DB=sqlite://:memory: timeout --signal=KILL 1800 uv run --with pytest-timeout python - <<'PYEOF'
import os, sys, pytest
rc = pytest.main(["tests/", "-v", "--no-header", "-rA", "--tb=no", "-p", "no:cacheprovider", "--continue-on-collection-errors", "--timeout=180"])
sys.stdout.flush(); sys.stderr.flush()
os._exit(int(rc))
PYEOF

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

        # Per-PR anti-cheat hardening. dependency() returns an Image, so
        # DockerfileEnhancer emits this Dockerfile verbatim (it only auto-injects
        # hardening into str-dependency images). The shared base only clones the
        # repo; the per-PR checkout happens in prepare.sh (git checkout base.sha).
        # Detach at base.sha, drop origin + every ref + reflog, gc-prune the
        # fix/future commits, then 4-way audit so the fix can't be read via
        # git log/show/fetch.
        repo = self.pr.repo
        base_sha = self.pr.base.sha
        hardening = (
            "RUN set -eux; \\\n"
            f"    cd /home/{repo}; \\\n"
            f"    git checkout --detach {base_sha}; \\\n"
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all || true; \\\n"
            "    git reflog expire --expire-unreachable=now --all || true; \\\n"
            "    git gc --prune=now --aggressive; \\\n"
            "    git repack -a -d -l --quiet; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            f'    test "$(git rev-parse HEAD)" = "$(git rev-parse {base_sha})"; \\\n'
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\\n'
            '    test -z "$(git remote)"; \\\n'
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'
        )

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{hardening}

{self.clear_env}

"""


@Instance.register("tortoise", "tortoise_orm_2130_to_2035")
class TORTOISE_ORM_2130_TO_2035(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        return parse_pytest_log(log)
