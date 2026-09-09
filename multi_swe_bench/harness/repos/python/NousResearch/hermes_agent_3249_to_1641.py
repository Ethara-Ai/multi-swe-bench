"""NousResearch/hermes-agent harness config — PRs #1641 to #3249.

Registered under the number_interval key ``hermes_agent_3249_to_1641``, NOT
under ``NousResearch/hermes-agent``. ``Instance.register`` writes straight into
``_registry[f"{org}/{repo}"]`` with no collision check, so a second class on the
plain key would silently replace ``hermes_agent.py`` and break every PR already
routed through it. Routing to this file is by ``number_interval`` on the dataset
records (``Instance.create``, instance.py:42).

Environment is inherited from the proven generic config: python:3.11,
``pip install -e ".[dev]"``, pytest over ``tests/``.

Differences from hermes_agent.py:
  * exactly 7 COPY files (the contract), so ``wire_test_worktree.py`` is not
    copied. It is a PR-specific device that rewrites a test file's standalone
    reimplementations into adapters; it applies to one file in one PR and is
    not carried into a 10-PR range.
  * base stops at ``git clone`` then CMD; the git strip lives in the PR layer.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class HermesAgentEraImageBase(Image):
    """python:3.11 + toolchain, then the clone. Nothing after it.

    Emitting the syntax directive keeps DockerfileEnhancer from rewriting the
    clone into clone+checkout+hardening (image.py:316) -- the hardening belongs
    to the PR layer.
    """

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
        return "python:3.11"

    def image_tag(self) -> str:
        return "base-pip"

    def workdir(self) -> str:
        return "base-pip"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONDONTWRITEBYTECODE=1

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    build-essential \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class HermesAgentEraImageDefault(Image):
    """PR layer: FROM base, exactly 7 COPYs, prepare.sh, then the git strip."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return HermesAgentEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        # Identical in all three stages (QC P7): the only difference between
        # them is which patch was applied first.
        test_cmd = """python -m pytest tests \\
    -p no:cacheprovider -n 0 \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1
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
set -e

cd /home/[[REPO]]
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git fetch --depth=1 origin [[SHA]] 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout -f [[SHA]]
test "$(git rev-parse HEAD)" = "$(git rev-parse [[SHA]])"
bash /home/check_git_changes.sh

python -V
python -m pip install --no-cache-dir --upgrade pip setuptools wheel
# ".[all]", not ".[dev]": tests/acp imports `agent-client-protocol`, which lives
# in the [acp] extra, so a dev-only install fails collection with
# "ModuleNotFoundError: No module named 'acp'" across tests/acp/*. [all] bundles
# the 15 light extras (acp, mcp, cli, pty, voice, ...) and deliberately does NOT
# include [rl], whose deps are git checkouts of atroposlib/tinker plus wandb.
# Degrade rather than fail outright if one optional extra cannot build; the
# collect-only check below is the real gate.
python -m pip install --no-cache-dir -e ".[all]" \
    || python -m pip install --no-cache-dir -e ".[dev,acp]" \
    || python -m pip install --no-cache-dir -e ".[dev]"
python -m pip install --no-cache-dir pytest-xdist

# The [acp] extra pins agent-client-protocol>=0.8.1,<1.0 and pip takes the
# newest match (0.12.1), but acp_adapter/server.py imports AuthMethod from
# acp.schema and that name was dropped in 0.9.0 -- verified across 0.8.1 /
# 0.9.0 / 0.10.0 / 0.11.0 / 0.12.1, where only 0.8.1 still exports it. Without
# this pin, tests/acp/test_server.py fails collection and takes the whole
# collect-only gate down with it. Re-pin AFTER the extras install so nothing
# resolves it forward again.
python -m pip install --no-cache-dir "agent-client-protocol==0.8.1" 2>/dev/null || true

# Collect once at the base commit: a collection error here is an environment
# problem and should fail the build loudly, rather than surfacing later as a
# stage that silently reports 0/0/0.
python -m pytest tests --collect-only -q -p no:cacheprovider -n 0

# `pip install -e .` writes an egg-info dir into the tree; restoring tracked
# files leaves the install intact while returning the worktree to exactly
# BASE_COMMIT -- the state every `git apply` in the run scripts expects.
git checkout -- .
git clean -fdq -e '*.egg-info' -e '*.egg-link'
bash /home/check_git_changes.sh
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true

cd /home/[[REPO]]
""" + test_cmd).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi
""" + test_cmd).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "=== PATCH FAILURE: test.patch + fix.patch did not apply ==="
    exit 1
fi
""" + test_cmd).replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Strip AFTER prepare.sh: prepare.sh fetches this PR's sha, and
        # stripping first would prune the objects it needs. The sha is inlined
        # so this layer declares no ARG/ENV of its own.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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
"""


@Instance.register("NousResearch", "hermes_agent_3249_to_1641")
class HermesAgent3249To1641(Instance):
    """Harness instance for NousResearch/hermes-agent — PRs #1641 to #3249."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HermesAgentEraImageDefault(self.pr, self._config)

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
        """Parse pytest -rA output into file-qualified test ids.

        Ids keep the FILE PATH ("tests/x.py::test_y"), never the bare name after
        "::". Two same-named tests in different files would otherwise collapse
        into one id and land in two status sets, which TestResult.__post_init__
        rejects outright -- that crash took out an earlier Python dataset.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Two formats, because the run scripts pass BOTH -v and -rA:
        #   -v  prints  "tests/x.py::test_y PASSED [ 12%]"   (id first, streamed)
        #   -rA prints  "PASSED tests/x.py::test_y"          (status first, at the end)
        # The verbose form is the load-bearing one: it appears as each test
        # finishes, so a run that is cut short still yields results, whereas the
        # -rA summary only exists if pytest reaches the end.
        inline_re = re.compile(
            r"^(?P<id>\S+\.py::\S+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
            re.MULTILINE,
        )
        summary_re = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
            r"(?P<id>\S+\.py::\S+?)(?:\s+-\s.*)?\s*$",
            re.MULTILINE,
        )
        for m in list(inline_re.finditer(log)) + list(summary_re.finditer(log)):
            status, tid = m.group("status"), m.group("id")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(tid)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        # A suite that fails to import prints "ERROR tests/x.py" with no "::".
        # Record the FILE so the stage still carries that signal instead of
        # going silently empty.
        for m in re.finditer(r"^ERROR\s+(\S+\.py)\s*(?:-.*)?$", log, re.MULTILINE):
            failed_tests.add(m.group(1))

        # TestResult.__post_init__ enforces disjoint sets. Failure wins over a
        # retry that later passed, then skip.
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
