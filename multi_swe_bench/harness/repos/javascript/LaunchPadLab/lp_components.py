import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

try:
    from multi_swe_bench.utils import git_util as _lp_git_util
    from multi_swe_bench.utils.safe_subprocess import safe_run as _lp_safe_run
    from git import Repo as _LpRepo

    _lp_orig_get_all = _lp_git_util.get_all_commit_hashes

    def _lp_patched_get_all(repo_path, logger):
        if "LaunchPadLab" in str(repo_path) and "lp-components" in str(repo_path):
            try:
                _lp_safe_run(
                    ["git", "-C", str(repo_path), "fetch", "--no-tags", "--quiet",
                     "origin", "+refs/pull/*/head:refs/remotes/origin/pr/*"],
                    check=False,
                )
                return {c.hexsha for c in _LpRepo(repo_path).iter_commits("--all")}
            except Exception as exc:
                logger.error(f"lp-components PR-ref fetch failed: {exc}")
        return _lp_orig_get_all(repo_path, logger)

    _lp_git_util.get_all_commit_hashes = _lp_patched_get_all
except Exception:
    pass


_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
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


def _base_dockerfile(from_image: str, org: str, repo: str, apt_extra_sed: str) -> str:
    return f"""\
# syntax=docker/dockerfile:1.6

FROM {from_image}

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

WORKDIR /home/

RUN {apt_extra_sed}apt-get update && \\
    apt-get install -y --no-install-recommends ca-certificates git && \\
    rm -rf /var/lib/apt/lists/*

RUN git -C /home clone "${{REPO_URL}}" {repo}

RUN git -C /home/{repo} fetch --no-tags origin "+refs/pull/*/head:refs/remotes/origin/pr/*"

CMD ["/bin/bash"]
"""


def _pr_dockerfile(name: str, tag: str, sha: str, repo: str, copy_commands: str) -> str:
    return f"""\
FROM {name}:{tag}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout {sha}

{copy_commands}
RUN set -eux; \\
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

RUN bash /home/prepare.sh
"""


_APT_SED_STRETCH = (
    "sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\\n"
    "    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \\\n"
    "    sed -i '/stretch-updates/d' /etc/apt/sources.list && \\\n    "
)

_APT_SED_BUSTER = (
    "sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\\n"
    "    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \\\n"
    "    sed -i '/buster-updates/d' /etc/apt/sources.list && \\\n    "
)


def _prepare_sh(yarn_prefix: str, sha: str, repo: str) -> str:
    return f"""\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{yarn_prefix}yarn install --frozen-lockfile || true
node -e "require.resolve('jest'); require('react'); console.log('DEPS_OK')"
"""


def _run_sh(repo: str) -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
npx jest --verbose
"""


def _test_run_sh(repo: str, yarn_prefix: str = "") -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --exclude yarn.lock --exclude '*/yarn.lock' --whitespace=nowarn /home/test.patch
{yarn_prefix}yarn install
npx jest --verbose
"""


def _fix_run_sh(repo: str, yarn_prefix: str = "") -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --exclude yarn.lock --exclude '*/yarn.lock' --whitespace=nowarn /home/test.patch /home/fix.patch
{yarn_prefix}yarn install
npx jest --verbose
"""


def _pr_files(pr: PullRequest, yarn_prefix: str = "") -> list[File]:
    return [
        File(".", "fix.patch", f"{pr.fix_patch}"),
        File(".", "test.patch", f"{pr.test_patch}"),
        File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
        File(".", "prepare.sh", _prepare_sh(yarn_prefix, pr.base.sha, pr.repo)),
        File(".", "run.sh", _run_sh(pr.repo)),
        File(".", "test-run.sh", _test_run_sh(pr.repo, yarn_prefix)),
        File(".", "fix-run.sh", _fix_run_sh(pr.repo, yarn_prefix)),
    ]


def _pr_copy_commands(files: list[File]) -> str:
    return "\n".join(f"COPY {f.name} /home/" for f in files) + "\n"


class ImageBase12(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "node:12"

    def image_tag(self) -> str:
        return "base-node12"

    def workdir(self) -> str:
        return "base_node12"

    def files(self) -> list[File]:
        return []

    def repo_dir(self) -> str:
        return self.pr.repo

    def dockerfile(self) -> str:
        return _base_dockerfile(self.dependency(), self.pr.org, self.pr.repo, _APT_SED_STRETCH)


class ImageDefault12(Image):
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
        return ImageBase12(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def repo_dir(self) -> str:
        return self.pr.repo

    def files(self) -> list[File]:
        return _pr_files(self.pr)

    def dockerfile(self) -> str:
        image = self.dependency()
        return _pr_dockerfile(
            image.image_name(),
            image.image_tag(),
            self.pr.base.sha,
            self.pr.repo,
            _pr_copy_commands(self.files()),
        )


class ImageBase16(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "node:16"

    def image_tag(self) -> str:
        return "base-node16"

    def workdir(self) -> str:
        return "base_node16"

    def files(self) -> list[File]:
        return []

    def repo_dir(self) -> str:
        return self.pr.repo

    def dockerfile(self) -> str:
        return _base_dockerfile(self.dependency(), self.pr.org, self.pr.repo, _APT_SED_BUSTER)


class ImageDefault16(Image):
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
        return ImageBase16(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def repo_dir(self) -> str:
        return self.pr.repo

    def files(self) -> list[File]:
        return _pr_files(self.pr)

    def dockerfile(self) -> str:
        image = self.dependency()
        return _pr_dockerfile(
            image.image_name(),
            image.image_tag(),
            self.pr.base.sha,
            self.pr.repo,
            _pr_copy_commands(self.files()),
        )


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

    def dependency(self) -> str:
        return "node:20"

    def image_tag(self) -> str:
        return "base-node20"

    def workdir(self) -> str:
        return "base_node20"

    def files(self) -> list[File]:
        return []

    def repo_dir(self) -> str:
        return self.pr.repo

    def dockerfile(self) -> str:
        return _base_dockerfile(self.dependency(), self.pr.org, self.pr.repo, "")


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def repo_dir(self) -> str:
        return self.pr.repo

    def files(self) -> list[File]:
        return _pr_files(self.pr, yarn_prefix="HUSKY=0 ")

    def dockerfile(self) -> str:
        image = self.dependency()
        return _pr_dockerfile(
            image.image_name(),
            image.image_tag(),
            self.pr.base.sha,
            self.pr.repo,
            _pr_copy_commands(self.files()),
        )


@Instance.register("LaunchPadLab", "lp-components")
class LpComponents(Instance):
    _PRS_NODE12: frozenset[int] = frozenset({521})
    _MAX_PR_NODE16: int = 562

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number in self._PRS_NODE12:
            return ImageDefault12(self.pr, self._config)
        if self.pr.number <= self._MAX_PR_NODE16:
            return ImageDefault16(self.pr, self._config)
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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        current_suite = None

        re_pass_suite = re.compile(r"^PASS (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")
        re_pass_test = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d*\.?\d+\s*\w+\))?$")

        re_fail_suite = re.compile(r"^FAIL (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")
        re_fail_test = re.compile(r"^\s*[✕×]\s+(.+?)(?:\s+\(\d*\.?\d+\s*\w+\))?$")

        re_skip_test = re.compile(r"^\s*○\s+(?:skipped\s+|todo\s+)?(.+?)(?:\s+\(\d*\.?\d+\s*\w+\))?$", re.IGNORECASE)

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_suite.match(line)
            if pass_match:
                current_suite = pass_match.group(1)
                passed_tests.add(current_suite)
                continue

            fail_match = re_fail_suite.match(line)
            if fail_match:
                current_suite = fail_match.group(1)
                failed_tests.add(current_suite)
                continue

            pass_test_match = re_pass_test.match(line)
            if pass_test_match:
                if current_suite is None:
                    continue

                test = f"{current_suite}:{pass_test_match.group(1)}"
                passed_tests.add(test)
                continue

            fail_test_match = re_fail_test.match(line)
            if fail_test_match:
                if current_suite is None:
                    continue

                test = f"{current_suite}:{fail_test_match.group(1)}"
                failed_tests.add(test)
                continue

            skip_test_match = re_skip_test.match(line)
            if skip_test_match:
                if current_suite is None:
                    continue

                test = f"{current_suite}:{skip_test_match.group(1)}"
                skipped_tests.add(test)
                continue

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


class _LpRegistry(dict):
    def __missing__(self, key):
        if (isinstance(key, str)
                and key.startswith("LaunchPadLab/")
                and key.rsplit("/", 1)[1].isdigit()):
            return self["LaunchPadLab/lp-components"]
        raise KeyError(key)


if not isinstance(Instance._registry, _LpRegistry):
    Instance._registry = _LpRegistry(Instance._registry)
