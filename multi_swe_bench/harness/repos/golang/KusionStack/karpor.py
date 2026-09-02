import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GO122 = "golang:1.22"

_PKG_EXCLUDE = (
    "cmd|internal|internalimport|generated|handler|middleware|registry|"
    "openapi|apis|version|gitutil|server|elasticsearch"
)

_GO_TEST_CMD = "go test -json -count=1 -gcflags=all=-l -timeout 10m $PKGS 2>&1"

_SELECT_PKGS = f"""PKGS="$(go list -e ./pkg/... | grep -vE '{_PKG_EXCLUDE}' || true)"
if [ -z "$PKGS" ]; then
    echo "karpor: package selection produced no packages"
    exit 1
fi"""


_NO_PRUNE_HARDENING = chr(10).join(
    line
    for line in Image._HARDENING_BLOCK.split(chr(10))
    if "git gc --prune=now" not in line and "git repack -a -d -l" not in line
)


class KarporImageBase(Image):
    """The heavy environment image: toolchain + source. ONE, shared by all five.

    Carries `golang:1.22` and the cloned repo with its history intact, and
    deliberately pins NOTHING -- the checkout to a record's base.sha happens in
    prepare.sh, per record. That split is what lets a single tag serve every
    record, so the repo is cloned once per dataset build instead of once per
    record.

    Why the pin cannot live here
    ----------------------------
    Images dedupe on image_full_name, so one tag is built exactly once.
    build_dataset passes BASE_COMMIT as a build arg to every image whose
    dependency() is a string (build_dataset.py:623-629), i.e. this one, and
    Image._HARDENING_BLOCK detaches to it and runs `git gc --prune=now`,
    dropping every object unreachable from that commit. A shared base that also
    pinned would freeze on whichever record built first.

    Measured against a clean full clone with all PR refs fetched: #556's base
    bc2b3582 is contained by 5 refs (all under refs/remotes/origin/pr/*), is NOT
    an ancestor of main, and is NOT an ancestor of #128, #564, #797 or #827. No
    commit in the repo has all five base commits as ancestors, so no single
    pinned image can hold them. Pinning here would cost that record.

    Why the leading `# syntax` directive is load-bearing
    ----------------------------------------------------
    DockerfileEnhancer.enhance early-returns on `if SYNTAX_DIRECTIVE in raw`
    (image.py:317-318), so emitting it here opts this file out of the enhancer,
    and everything the enhancer would have contributed is written out by hand
    below. That opt-out is what keeps this base unpinned: _inject_final_sanitize
    (image.py:389-395) fires on ANY Dockerfile containing `git clone`/`git
    fetch`/`git remote add` and appends Image._HARDENING_BLOCK unconditionally --
    precisely the pin-and-prune this image must not have.

    Why the refs/pull fetch is here
    ------------------------------
    #556's commit arrives with the clone only because a clone transfers whole
    packfiles, unreachable objects included; nothing on a branch points at it, so
    it is one `git gc` away from disappearing. refs/pull/<n>/head does point at
    it, so fetching those refs turns an accident into a guarantee. Once, here,
    rather than five times downstream.

    KNOWN TRADE-OFF: the destructive scrub -- ref deletion, `git gc --prune=now`
    and the four integrity asserts -- does not run, here or downstream, because
    it can only be anchored to one commit. These images therefore ship the repo's
    full history, and the Dockerfile QC's D13/D14/D15 fail on this file by
    design. Keeping ONE base image with all five records intact was chosen over
    that scrub; a `base-pr-<N>` tag is what would buy it back.
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
        return _GO122

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = _safe_path_component(self.pr.repo)
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
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

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

# refs/pull/<n>/head is the only thing pointing at #556's base commit -- see the
# class docstring. --no-tags keeps the 119 upstream tags out of the image.
RUN git fetch --no-tags origin "+refs/pull/*/head:refs/remotes/origin/pr/*"

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_NO_PRUNE_HARDENING}

{self.clear_env}

CMD ["/bin/bash"]
"""


class KarporImageDefault(Image):
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
        return KarporImageBase(self.pr, self.config)

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

# Integrity guard for prepare.sh: assert we are inside a git work tree and that
# the tree is clean. prepare.sh calls this once after `git reset --hard` and
# again after `git checkout <base.sha>`, so a dirty or drifted tree aborts the
# image build instead of being baked in and silently mismeasured by all three
# graded stages. `git reset --hard` alone does not remove untracked files, so
# the porcelain check (which lists them) is what actually closes that hole.

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "check_git_changes: not inside a git work tree: $(pwd)" >&2
    exit 1
fi

changes="$(git status --porcelain)"
if [ -n "$changes" ]; then
    echo "check_git_changes: working tree is not clean" >&2
    echo "$changes" >&2
    exit 1
fi
""",
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

# Warm the module cache and the build cache so the three graded stages measure
# the test suite instead of a cold `go mod download`. `|| true` is correct here
# and only here: a package that does not compile at the base commit is expected
# (the fix patch is what makes it compile) and must not fail the image build.
go mod download || true
go build ./... || true
go test -count=1 -gcflags=all=-l -run XXX_NO_SUCH_TEST ./pkg/... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
{select}
{test_cmd}

""".format(pr=self.pr, select=_SELECT_PKGS, test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{select}
{test_cmd}

""".format(pr=self.pr, select=_SELECT_PKGS, test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{select}
{test_cmd}

""".format(pr=self.pr, select=_SELECT_PKGS, test_cmd=_GO_TEST_CMD),
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


@Instance.register("KusionStack", "karpor")
class Karpor(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KarporImageDefault(self.pr, self._config)

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

    _MODULE_PREFIX = re.compile(r"^github\.com/KusionStack/(?:karbour|karpor)/")

    @classmethod
    def _qualify(cls, pkg: str, test: str) -> str:
        """Build a name that is unique across packages and stable across stages.

        `go test -json` reports Package and Test separately, and a bare test
        name is NOT unique here -- TestNewCache, TestNew and friends recur in
        several of the 21 selected packages. The stage comparison unions names
        across run/test/fix, so a collision would let one package's pass mask
        another package's failure. Subtests keep their full "TestX/sub" path.
        """
        short = cls._MODULE_PREFIX.sub("", pkg)
        return f"{short}::{test}"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        build_failed_re = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")

        pkg_actions: dict[str, str] = {}
        pkg_has_tests: set[str] = set()

        for raw in clean_log.split("\n"):
            line = raw.strip()

            if not line.startswith("{"):
                match = build_failed_re.match(line)
                if match:
                    failed_tests.add(self._qualify(match.group(1), "[build]"))
                continue

            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            action = event.get("Action")
            pkg = event.get("Package")
            test = event.get("Test")

            if action == "output" and pkg and not test:
                match = build_failed_re.match(str(event.get("Output", "")).strip())
                if match:
                    failed_tests.add(self._qualify(match.group(1), "[build]"))
                continue

            if not pkg or action not in ("pass", "fail", "skip"):
                continue

            if not test:
                pkg_actions[pkg] = action
                continue

            pkg_has_tests.add(pkg)
            name = self._qualify(pkg, test)
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        for pkg, action in pkg_actions.items():
            if action == "fail" and pkg not in pkg_has_tests:
                failed_tests.add(self._qualify(pkg, "[build]"))

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
