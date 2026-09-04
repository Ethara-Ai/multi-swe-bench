"""pretalx/pretalx -- era 3 of 3, PR interval 2289 -> 2228 (Django 6.0 / Python 3.12).

Era boundary. At PR 2228's base (344bb22f14a3) pyproject.toml declares
`requires-python >= 3.12` and pins `Django[argon2]~=6.0.0` with
`django-filter==25.2`; Django 6.0 publishes no distribution for an interpreter
below 3.12, so `pip install --dry-run` at this commit fails under python:3.10
and python:3.11 with "No matching distribution found for
Django[argon2]~=6.0.0". Two further things move with it:

  * a `devdocs` extra appears (`dev, devdocs, postgres, redis`; the `mysql`
    extra of the earlier eras is gone), and
  * src/pretalx/settings.py gains a guarded `INSTALLED_APPS.append(
    "drf_spectacular")` -- the module is declared only in `devdocs`, so
    installing `[dev]` alone leaves Django's app registry pointing at an absent
    module and django.setup() raises ModuleNotFoundError during pytest's
    initial conftest load. That collects zero tests and reads as a parse_log
    bug rather than a missing dependency, which is why this era installs
    `.[dev,devdocs]`. Observed: 76-line stage log and 0 tests with `[dev]`,
    1868 tests collected with both extras.

    pretalx_1264_to_1264.py             1 PR   python:3.8-bookworm
    pretalx_1576_to_1543.py             2 PRs  python:3.10-bookworm
    pretalx_2289_to_2228.py  this file  2 PRs  python:3.12-bookworm

Registration. The dataset rows carry no `number_interval`, so
Instance.create() (instance.py:41-49) can only ever compute the plain
`pretalx/pretalx` key and the three range keys are unreachable from this
dataset. Per §17.4 option 2 this file therefore also registers the plain key,
and its ImageDefault.dependency() branches to the sibling eras' base
images by PR number so that every row still lands on a base its own metadata
accepts. The range key `pretalx_2289_to_2228` stays available for a dataset
that does carry `number_interval`.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Toolchain base for this era. Shared by every PR in the interval.

    R10: this image must NOT clone the repo. Because dependency() returns a
    string, DockerfileEnhancer processes this Dockerfile and
    _standardize_repo_fetch would rewrite any clone/COPY line into
    `clone + checkout ${BASE_COMMIT} + hardening`, pinning the shared base to
    whichever single PR happened to build it first and pruning every other
    commit in the era. The clone lives in ImageDefault.
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
        return "python:3.12-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-2289-to-2228"

    def workdir(self) -> str:
        return "base-2289-to-2228"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # python:3.12-bookworm is a current Debian release, so the default
        # deb.debian.org sources resolve and no archive.debian.org rewrite is
        # needed (R11). gettext supplies msgfmt, which pretalx's package data
        # step invokes when compiling translation catalogues. This era's
        # metadata is modern, so pip is simply kept current.
        # The full file is emitted here, syntax directive included, because
        # DockerfileEnhancer.enhance() (image.py:317) returns a Dockerfile
        # verbatim once it already carries the directive. That keeps this base
        # byte-identical to the reference layout: neither _standardize_repo_fetch
        # nor _inject_final_sanitize rewrites the clone or appends a hardening
        # block, which belongs in the per-PR image rather than in a base shared
        # across an era (R10).
        org = self.pr.org
        repo = self.pr.repo

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gettext \\
    gnupg \\
    make \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ImageBaseDjango32(ImageBase):
    """Era 1 base (PRs <= 1264), reachable through the plain-key registration.

    R25 keeps range files self-contained, so this is a copy of
    pretalx_1264_to_1264.ImageBase rather than an import of it. It
    renders the identical Dockerfile and the identical image tag, so whichever
    file builds it first produces the same layer.
    """

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-bookworm"

    def image_tag(self) -> str:
        return "base-1264-to-1264"

    def workdir(self) -> str:
        return "base-1264-to-1264"

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The full file is emitted here, syntax directive included, because
        # DockerfileEnhancer.enhance() (image.py:317) returns a Dockerfile
        # verbatim once it already carries the directive. That keeps this base
        # byte-identical to the reference layout: neither _standardize_repo_fetch
        # nor _inject_final_sanitize rewrites the clone or appends a hardening
        # block, which belongs in the per-PR image rather than in a base shared
        # across an era (R10).
        org = self.pr.org
        repo = self.pr.repo

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gettext \\
    gnupg \\
    make \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install "pip<24.1"


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ImageBaseDjango42(ImageBase):
    """Era 2 base (PRs 1373-1576), reachable through the plain-key registration.

    R25 keeps range files self-contained, so this is a copy of
    pretalx_1576_to_1543.ImageBase rather than an import of it. It
    renders the identical Dockerfile and the identical image tag, so whichever
    file builds it first produces the same layer.
    """

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.10-bookworm"

    def image_tag(self) -> str:
        return "base-1576-to-1543"

    def workdir(self) -> str:
        return "base-1576-to-1543"

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The full file is emitted here, syntax directive included, because
        # DockerfileEnhancer.enhance() (image.py:317) returns a Dockerfile
        # verbatim once it already carries the directive. That keeps this base
        # byte-identical to the reference layout: neither _standardize_repo_fetch
        # nor _inject_final_sanitize rewrites the clone or appends a hardening
        # block, which belongs in the per-PR image rather than in a base shared
        # across an era (R10).
        org = self.pr.org
        repo = self.pr.repo

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gettext \\
    gnupg \\
    make \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install "pip>=23,<24.1"


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """Per-PR image: clones, pins BASE_COMMIT, stages the patches and scripts."""

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
        # This class is reached through both the range key (era 3 only) and the
        # plain `pretalx/pretalx` key (every row, because the dataset carries
        # no number_interval). The branch keeps an older PR on the base image
        # its own metadata accepts: PR 1264 pins celery~=4.4.0, which has no
        # Python 3.12 release, and PRs 1543/1576 pin Django~=4.2.0.
        if self.pr.number >= 2224:
            return ImageBase(self.pr, self._config)
        if self.pr.number >= 1373:
            return ImageBaseDjango42(self.pr, self._config)
        return ImageBaseDjango32(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Three install shapes, one per era. From PR 2224 on the `devdocs`
        # extra carries drf-spectacular, which settings.py appends to
        # INSTALLED_APPS whenever it imports -- omitting it makes django.setup()
        # raise ModuleNotFoundError and the stage collect zero tests. PRs
        # 1576..2223 are packaged from the repo root without that extra, and
        # PRs <= 1543 are packaged from src/ and run pytest from inside it.
        if self.pr.number >= 2224:
            install_target = '".[dev,devdocs]"'
            test_dir = f"/home/{self.pr.repo}"
            pytest_target = "src/tests"
            config_file = "src/tests/ci_sqlite.cfg"
        elif self.pr.number >= 1576:
            install_target = '".[dev]"'
            test_dir = f"/home/{self.pr.repo}"
            pytest_target = "src/tests"
            config_file = "src/tests/ci_sqlite.cfg"
        else:
            install_target = '"src[dev]"'
            test_dir = f"/home/{self.pr.repo}/src"
            pytest_target = "tests"
            config_file = "tests/ci_sqlite.cfg"

        # R3: byte-identical across run.sh, test-run.sh and fix-run.sh, so a
        # test reports the same name in all three stages. -p no:randomly keeps
        # ordering deterministic; --timeout guards a hung test from stalling a
        # stage, which docker_util.run would never interrupt on its own.
        test_cmd = (
            "python -m pytest --no-header -rA --tb=no -p no:cacheprovider"
            f" -p no:randomly --timeout=300 {pytest_target}"
        )

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/check_git_changes.sh

# Recover the base commit when upstream deleted the branch it lived on. GitHub
# still serves such a commit by SHA and via refs/pull/<N>/head. The cat-file
# guard makes this a no-op when the clone already contains it -- which is the
# normal case here, since the base image keeps a full clone with its remote.
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags --depth=2147483647 origin {pr.base.sha} \\
    || git fetch --no-tags origin "+refs/pull/{pr.number}/head:refs/remotes/origin/pr-{pr.number}"

# Every install ends in `|| true`: a native wheel that fails to build on one
# architecture must not abort the image build. The test stage is what decides
# whether the environment is usable.
pip install -Ue {install_target} || true
pip install pytest-timeout || true

""".format(pr=self.pr, install_target=install_target),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PRETALX_CONFIG_FILE={config_file}
cd {test_dir}
{test_cmd}

""".format(test_dir=test_dir, config_file=config_file, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PRETALX_CONFIG_FILE={config_file}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cd {test_dir}
{test_cmd}

""".format(
                    pr=self.pr,
                    test_dir=test_dir,
                    config_file=config_file,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PRETALX_CONFIG_FILE={config_file}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd {test_dir}
{test_cmd}

""".format(
                    pr=self.pr,
                    test_dir=test_dir,
                    config_file=config_file,
                    test_cmd=test_cmd,
                ),
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

        # The shared base already cloned the repo into /home/<repo>, so this
        # image must NOT clone again -- a second `git clone` into the same path
        # aborts the build with "destination path already exists and is not an
        # empty directory". This image chains to an Image object, so
        # DockerfileEnhancer returns the text verbatim (R9) and injects nothing:
        # the recovery fetch, the hardening block and prepare.sh below are the
        # whole recipe, in that order.
        global_env = f"\n{self.global_env}\n" if self.global_env else ""

        return f"""FROM {name}:{tag}
{global_env}
{copy_commands}
WORKDIR /home/{self.pr.repo}

# Git stripping / hardening. Pins the tree to the base commit and reduces the
# repository to exactly that history, then asserts the four invariants:
# HEAD == base commit, no residual refs, no remotes, no unreachable objects.
RUN set -eux; \\
    git checkout --detach {self.pr.base.sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {self.pr.base.sha})"; \\
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

{prepare_commands}
{self.clear_env}

"""


@Instance.register("pretalx", "pretalx_2289_to_2228")
class PRETALX_2289_TO_2228(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest's -rA short test summary emits one line per test:
        #   PASSED tests/orga/views/test_orga_views_cfp.py::test_track_position
        #   FAILED tests/cfp/views/test_cfp_user.py::test_edit - AssertionError
        #   SKIPPED [1] tests/api/test_api_schedule.py:42: needs network
        # The trailing " - <reason>" carries assertion text that differs
        # between stages, so only the leading non-space token is captured:
        # keeping the reason would name the same test differently in the test
        # and fix stages and make the FAIL->PASS transition invisible (R3).
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"^(?:PASSED|XPASS)\s+(\S+)")
        re_fail = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")
        re_skip = re.compile(r"^(?:SKIPPED|XFAIL)(?:\s+\[\d+\])?\s+(\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            match = re_pass.match(line)
            if match:
                passed_tests.add(match.group(1))
                continue

            match = re_fail.match(line)
            if match:
                failed_tests.add(match.group(1))
                continue

            match = re_skip.match(line)
            if match:
                skipped_tests.add(match.group(1))

        # R2 -- the three sets MUST be disjoint or TestResult.__post_init__
        # raises and the whole run dies. Failure wins: a test reported both
        # PASSED and FAILED (a rerun, or the same name under two targets) is a
        # failure.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# §17.4 option 2: the dataset rows carry no `number_interval`, so
# Instance.create() computes the plain `pretalx/pretalx` key for every row and
# the three range keys above are unreachable from this dataset. Registering the
# plain key here keeps all five rows routable; ImageDefault.dependency()
# and .files() branch by PR number so each row still gets its own era's base
# image and install command. Remove this line once the dataset carries
# `number_interval`.
Instance.register("pretalx", "pretalx")(PRETALX_2289_TO_2228)
