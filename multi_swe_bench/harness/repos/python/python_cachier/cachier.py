from __future__ import annotations

import re
from typing import Optional, Union

from unidiff import PatchSet

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_DIR = "tests/"

_EXCLUDED_BASENAMES = frozenset({
    "conftest.py",
    "__init__.py",
})

# PR#116: test_patch touches mongo-dependent files + creates standalone_script.py
# (which doesn't exist at base). Only test_memory_core.py and test_pickle_core.py
# contain the hash_params->hash_func rename that produces a clean f2p signal.
_PR116_ALLOWED_FILES = frozenset({
    "tests/test_memory_core.py",
    "tests/test_pickle_core.py",
})


def _test_files_from_patch(patch: str, pr_number: int) -> list[str]:
    seen: set[str] = set()
    for patched_file in PatchSet(patch):
        path = patched_file.target_file
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path == "/dev/null":
            continue
        if path.endswith(".py") and path.startswith(_TEST_DIR):
            basename = path.rsplit("/", 1)[-1]
            if basename not in _EXCLUDED_BASENAMES:
                seen.add(path)

    if pr_number == 116:
        seen = seen & _PR116_ALLOWED_FILES

    return sorted(seen)


class ImageBase(Image):
    """SHARED base image (tag "base"), ONE image reused by EVERY cachier PR.

    It keeps the full git history + origin so each PR image can check out its
    own base.sha on top of it. Building N PRs therefore yields N pr-images plus
    this single base image.
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
        return "python:3.11-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

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

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # The `# syntax` directive makes DockerfileEnhancer.enhance() skip this
        # file: per-PR checkout + history hardening live in ImageDefault, since
        # a shared base cannot be pinned to a single commit.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
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
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

{code}

# History hardening is deferred to prepare.sh, which runs per-PR and ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to a single PR's BASE_COMMIT.

{self.clear_env}

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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = _test_files_from_patch(self.pr.test_patch, self.pr.number)
        test_files_str = " ".join(test_files)

        pytest_opts = (
            "python -m pytest --noconftest --no-header -rN --tb=short -v"
            " -m 'not mongo and not sql and not redis'"
            " -p no:cacheprovider -o 'addopts='"
        )

        # test-run.sh / fix-run.sh apply the test patch first, so every path in
        # test_files exists by the time pytest runs.
        pytest_cmd = f"{pytest_opts} {test_files_str}" if test_files_str else f"{pytest_opts} tests/"

        # run.sh is the baseline: it deliberately does NOT apply the test patch,
        # so any test file the PR *creates* is absent. pytest treats a missing
        # path as a fatal collection error and aborts the whole session, which
        # wipes out the baseline even for files that do exist (PR #322 lost all
        # 213 base tests to one missing tests/test_varargs.py; PR #338 lost
        # tests/test_async_core.py to a missing tests/s3_tests/helpers.py).
        # Keep only the paths present at the base commit, and exit cleanly when
        # none remain so `set -e` does not fail the run.
        if test_files_str:
            baseline_cmd = (
                "BASELINE_FILES=\"\"\n"
                f"for f in {test_files_str}; do\n"
                '    if [ -e "$f" ]; then\n'
                '        BASELINE_FILES="$BASELINE_FILES $f"\n'
                "    else\n"
                '        echo "baseline: skipping $f (does not exist at the base commit)"\n'
                "    fi\n"
                "done\n"
                "\n"
                'if [ -z "$BASELINE_FILES" ]; then\n'
                '    echo "baseline: no test files exist at the base commit; nothing to run"\n'
                "    exit 0\n"
                "fi\n"
                "\n"
                f"{pytest_opts} $BASELINE_FILES"
            )
        else:
            baseline_cmd = f"{pytest_opts} tests/"

        # PRs #36, #116, #121, #133 have no tests/requirements.txt;
        # PR #134+ ships one.  The prepare script handles both eras.
        install_cmd = (
            "pip install --no-cache-dir setuptools wheel\n"
            "if [ -f tests/requirements.txt ]; then\n"
            "    pip install --no-cache-dir --no-build-isolation -e . -r tests/requirements.txt\n"
            "else\n"
            "    pip install --no-cache-dir --no-build-isolation -e . && "
            "pip install --no-cache-dir pytest pytest-cov pymongo pandas\n"
            "fi"
        )

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
                '  echo "check_git_changes: Not inside a git repository"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                "if [[ -n $(git status --porcelain) ]]; then\n"
                '  echo "check_git_changes: Uncommitted changes"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                'echo "check_git_changes: No uncommitted changes"\n'
                "exit 0\n",
            ),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "cd /home/{repo}\n"
                "git reset --hard\n"
                "git checkout {sha}\n"
                "\n"
                "{install}\n".format(
                    repo=self.pr.repo, sha=self.pr.base.sha,
                    install=install_cmd,
                ),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "cd /home/{repo}\n"
                "{baseline_cmd}\n".format(
                    repo=self.pr.repo, baseline_cmd=baseline_cmd
                ),
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "cd /home/{repo}\n"
                "git reset --hard\n"
                "git clean -fd\n"
                "git apply --whitespace=nowarn /home/test.patch\n"
                "{pytest_cmd}\n".format(
                    repo=self.pr.repo, pytest_cmd=pytest_cmd
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "cd /home/{repo}\n"
                "git reset --hard\n"
                "git clean -fd\n"
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                "{pytest_cmd}\n".format(
                    repo=self.pr.repo, pytest_cmd=pytest_cmd
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(
            f"COPY {f.name} /home/\n" for f in self.files()
        )

        # Per-PR anti-cheat hardening: pin to BASE_COMMIT, drop origin + every
        # ref + reflog, gc-prune everything unreachable (the fix commit and all
        # later history), then audit that only BASE_COMMIT's history remains.
        return """FROM {name}:{tag}

ARG BASE_COMMIT="{base_sha}"

{global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

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

{clear_env}

""".format(
            name=name,
            tag=tag,
            base_sha=self.pr.base.sha,
            global_env=self.global_env,
            clear_env=self.clear_env,
            repo=self.pr.repo,
            copy_commands=copy_commands,
        )


@Instance.register("python-cachier", "cachier")
class CACHIER(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        log = ansi_escape.sub("", log)

        # pytest -v format: tests/test_core.py::test_name STATUS  [N%]
        pattern = re.compile(
            r"(tests/[^\s]+::[^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)"
        )

        for line in log.splitlines():
            m = pattern.search(line)
            if m:
                test_name = m.group(1)
                status = m.group(2)
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
