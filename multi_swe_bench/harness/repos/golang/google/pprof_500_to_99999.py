"""google/pprof harness for the go-modules era (PR >= 500).

Covers number_interval: 506-517-557-559-561-564-577-599-615-631-634-649-660-674-676-682-684-711-716-734-741-742-792-814-818-864-878-887-891-920-934-945-949-972-981.

From PR #506 (Feb 2020) onward pprof carries a ``go.mod``. The declared
``go`` directive grows over time (1.14 -> 1.24.x), so the base image is
``golang:1.24-bookworm`` with ``GOTOOLCHAIN=auto`` -- the toolchain
self-upgrades to whatever a given commit's ``go.mod`` requires. Building
inside the module cache means deps come from ``go mod download``.

``go vet`` is disabled during tests: early module-era commits (go 1.14)
contain ``string(int)`` conversions that modern toolchains reject as a
vet build error even though they compiled fine at the time.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.24-bookworm"
_TAG_SUFFIX = "mod"


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
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
ENV PATH=/go/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN apt-get update && apt-get install -y --no-install-recommends \\
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
        repo = self.pr.repo
        sha = self.pr.base.sha

        prepare = f"""#!/bin/bash
set -e
cd /home/{repo}
go mod download 2>/dev/null || true
"""

        run_tests = f"""#!/bin/bash
set -uo pipefail
cd /home/{repo}
go mod download 2>/dev/null || true
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
cd /home/{repo}
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
cd /home/{repo}
git apply --3way --whitespace=nowarn {excludes} /home/test.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
"""

        fix_run = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
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
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{repo}

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


class Pprof_500_to_99999(Instance):
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
_ERA1_BUNDLE_NIS = [
    "506-508-509-510-513-514",
    "517-518-519-520-522-525-527-528-530-531-535",
    "557-560-563-565",
    "559-584-590-591-595",
    "561-562-570-571-572-574-575",
    "564-579-580-586-587",
    "577-588-593-596-598-601-605",
    "599-608-610-613-614",
    "615-619-620-622-623-627-628",
    "631-641-642-643-644",
    "634-635",
    "649-652-654-657",
    "660-661-664-665-668-672-673",
    "674-675-685-688",
    "676-677",
    "682-699-702-703-704",
    "684-691-692-693-694-695",
    "711-770-772-774-775-776-778-779-780-781-782",
    "716-717-721-722-723",
    "734-735-737-739-740",
    "741-745-749-750-751-756",
    "742-743-746",
    "792-793-795-796-798-799-800-801-802-803-804",
    "814-828-829-831-832-834",
    "818-825-826-827",
    "864-866-867-872-873-874",
    "878-879-880-881",
    "887-888-892-893-897-898",
    "891-894-896-899-900-901-902-903-904-905-907",
    "920-921-923-924-926-927-929",
    "934-935-937-938-939-940-941-942-944",
    "945-946-947-948",
    "949-953-954-956-957-958-959-961-962-964-965",
    "972-973-974-975-976-979",
    "981-982-985-986-988-989",
]
for _ni in _ERA1_BUNDLE_NIS:
    Instance._registry[f"google/{_ni}"] = Pprof_500_to_99999
