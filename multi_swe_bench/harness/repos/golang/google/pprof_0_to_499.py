"""google/pprof harness for the GOPATH era (PR < 500).

Covers number_interval: 109-134-140-150-183-188-213-269-288-304-335-365-383-384-408-418-423-438-446-452-487.

Before PR #506 (Feb 2020) pprof shipped no ``go.mod`` -- it was a
classic GOPATH project that had to live under
``$GOPATH/src/github.com/google/pprof`` and be built with
``GO111MODULE=off``. Its only external dependencies are
``github.com/chzyer/readline`` and
``github.com/ianlancetaylor/demangle``; both are fetched at build time
with ``go get -d -t ./...``.

The base image is ``golang:1.13-buster`` -- the newest Go that still
builds GOPATH-mode code cleanly. Debian Buster is EOL, so the apt
package archives are redirected to archive.debian.org. ``go vet`` is
disabled during tests because this era predates the ``string(int)``
conversion vet error that newer toolchains promote to a hard failure.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.13-buster"
_TAG_SUFFIX = "gopath"
_GOPATH_PKG = "github.com/google/pprof"


class _ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = (
                f"RUN git clone --no-single-branch "
                f"https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/go/src/{_GOPATH_PKG}"
            )
        else:
            code = f"COPY {self.pr.repo} /go/src/{_GOPATH_PKG}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive
ENV GO111MODULE=off
ENV GOPATH=/go
ENV PATH=/go/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' /etc/apt/sources.list && \\
    sed -i 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' /etc/apt/sources.list && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    git make gcc g++ pkg-config ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

CMD ["/bin/bash"]
"""


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        sha = self.pr.base.sha

        prepare = f"""#!/bin/bash
set -e
cd /go/src/{_GOPATH_PKG}
go get -d -t ./... 2>/dev/null || true
"""

        run_tests = f"""#!/bin/bash
set -uo pipefail
cd /go/src/{_GOPATH_PKG}
go get -d -t ./... 2>/dev/null || true
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.\\?$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done 2>/dev/null || true)
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -short -vet=off -v -count=1 -timeout 20m $PKGS
"""

        run_sh = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /go/src/{_GOPATH_PKG}
bash /home/run_tests.sh
"""

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        test_run = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /go/src/{_GOPATH_PKG}
git apply --3way --whitespace=nowarn {excludes} /home/test.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
"""

        fix_run = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /go/src/{_GOPATH_PKG}
git apply --3way --whitespace=nowarn {excludes} /home/test.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
git apply --3way --whitespace=nowarn {excludes} /home/fix.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/fix.patch \\
  || echo "git apply fix.patch failed (continuing)"
bash /home/run_tests.sh
"""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /go/src/{_GOPATH_PKG}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

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

RUN rm -f .git/ORIG_HEAD .git/FETCH_HEAD; \\
    test ! -f .git/ORIG_HEAD

RUN bash /home/prepare.sh
"""


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_result = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")

    for line in test_log.splitlines():
        line = line.rstrip()
        m = re_result.match(line)
        if not m:
            continue
        status, name = m.group(1), m.group(2)
        if status == "PASS":
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)
        elif status == "FAIL":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif status == "SKIP":
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class Pprof_0_to_499(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)


# Register under each bundle's NI (derived from prs_in_bundle)
_ERA0_BUNDLE_NIS = [
    "109-110-111-113-114-115-116-117-118-119-121-122-123-124-125-126-129-131",
    "134-135-138",
    "140-143-145",
    "150-151-154-155-157-160-161-162-163-168-169-171-173-175-176-177-179-180-184",
    "183-189-190-192-194-197-198-199-201-202-204-209-210-212-214-219-220-222-224",
    "188-247-248-249-250-252-254-256-259-263-264-265-266-268",
    "213-221-225-227-228-229-231-232-235-236-237-238-240-241-243",
    "269-270-272-275-278-279-284-286",
    "288-290-291-294-295-296-298-299-301-303-305-306",
    "304-308-312-313-315-316-318-322-326-327-330-332",
    "335-336-337-341-344-345-348-349-350-352-353-354-355-356-357-358-362",
    "365-366-367-368-369-370-371-372-373-374-375-376-381-382",
    "383-390-393-397-399-404-405",
    "384-388-392-394",
    "408-411-412-414-416",
    "418-419-420-421-422-425-426",
    "423-424-428-430-435-436-437",
    "438-442-444-445",
    "446-447",
    "452-456-461-462",
    "487-490-491-492-494-498-499",
]
for _ni in _ERA0_BUNDLE_NIS:
    Instance._registry[f"google/{_ni}"] = Pprof_0_to_499
