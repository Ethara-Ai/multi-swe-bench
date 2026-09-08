import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:12-bullseye"

_PR_NUMBERS: set = set()


def _target_pkg(test_patch: str):
    """The package directory whose tests this PR touches, read off the test patch.
    A test file lives at packages/<pkg>/test/...; everything before '/test/' is the
    package root. Derived from the patch, never written down."""
    for path in re.findall(r"diff --git a/(\S+) b/\S+", test_patch or ""):
        if "/test/" in path:
            return path.split("/test/")[0]
    return ""


class IstanbuljsImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        nums = _PR_NUMBERS or {self.pr.number}
        return f"base-{min(nums)}-{max(nums)}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image}

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    NODE_ENV=test \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}
CMD ["/bin/bash"]
"""


class IstanbuljsImageDefault(Image):
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
        return IstanbuljsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkg = _target_pkg(self.pr.test_patch)
        # Install the target package's own deps (mocha/chai/nyc + runtime deps such as
        # istanbul-lib-coverage/source-map) so its tests can run standalone -- the whole
        # lerna monorepo is not bootstrapped, only the package under test.
        pkg_install = (
            f'if [ -n "{pkg}" ] && [ -d "{pkg}" ]; then (cd "{pkg}" && '
            'npm install --no-audit --no-fund || npm install --no-audit --no-fund --legacy-peer-deps); fi'
        )
        # Run mocha in the package dir; --reporter json gives a machine-readable verdict.
        test_cmd = (
            f'if [ -n "{pkg}" ] && [ -d "{pkg}" ]; then cd "{pkg}"; fi\n'
            'npx --no-install mocha --reporter json --timeout 30000 '
            '"test/**/*.js" "test/*.js" 2>/dev/null || '
            'npx mocha --reporter json --timeout 30000 "test/**/*.js" "test/*.js"'
        )

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"; exit 1\n'
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"; git status --porcelain; exit 1\n'
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f'BASE_COMMIT="${{BASE_COMMIT:-{sha}}}"\n'
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            f"{pkg_install}\n"
            "git checkout -- .\n"
            "bash /home/check_git_changes.sh\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -o pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            f"{test_cmd}\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -o pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{test_cmd}\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -o pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{test_cmd}\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
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


@Instance.register("istanbuljs", "istanbuljs")
class Istanbuljs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        _PR_NUMBERS.add(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return IstanbuljsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        # mocha --reporter json prints one JSON object per invocation. Find each
        # top-level object and read its passes/failures/pending arrays.
        for m in re.finditer(r"\{[\s\S]*?\"stats\"[\s\S]*?\}\s*(?=\n\{|\Z)", log):
            blob = m.group(0)
            try:
                data = json.loads(blob)
            except Exception:
                # Fall back: locate the outermost object bounds around the match.
                try:
                    start = log.rindex("{", 0, m.start() + 1)
                    data = json.loads(log[start : m.end()])
                except Exception:
                    continue
            for t in data.get("passes", []) or []:
                name = t.get("fullTitle") or t.get("title")
                if name:
                    passed_tests.add(name)
            for t in data.get("failures", []) or []:
                name = t.get("fullTitle") or t.get("title")
                if name:
                    failed_tests.add(name)
            for t in data.get("pending", []) or []:
                name = t.get("fullTitle") or t.get("title")
                if name:
                    skipped_tests.add(name)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
