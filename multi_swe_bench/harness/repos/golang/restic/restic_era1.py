"""Restic harness for Era 1 — pre-cmd layout (src/ directory, GOPATH mode).

Covers number_interval: restic_era1
PRs: 723, 763, 871, 975, 1044, 1075 (base versions v0.3.3–v0.7.1)

Test command: GOPATH=/home/restic/vendor:/home/restic GO111MODULE=off go test -v -count=1 ./src/restic/...
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ResticEra1ImageBase(Image):
    """Toolchain + full-history checkout, shared by every PR in era 1.

    ``image_tag()`` is the constant ``"base-era1"``, so ONE image serves all six
    era-1 bundles while each carries a different ``base.sha``. The ``# syntax``
    directive makes ``DockerfileEnhancer.enhance()`` return this content
    verbatim, which stops the enhancer's ``_standardize_repo_fetch`` from
    rewriting the clone below into ``git clone`` + ``git checkout
    ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK``. That rewrite would prune this
    SHARED tag down to whichever base.sha built first, breaking `git checkout`
    for the other five bundles.

    Full history is therefore kept here; only the network remote is dropped (no
    later layer, and no agent, can re-fetch upstream). Per-PR hardening lives in
    ResticEra1ImageDefault.
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
        # PINNED, not golang:latest. Era-1 code is from 2017 (its .travis.yml
        # targets go 1.6.4/1.7.4) and does an in-place AES-CTR decrypt with
        # overlapping buffers, which modern Go rejects with "crypto/aes:
        # invalid buffer overlap"; go1.26 additionally fails to BUILD the
        # restic/filter and restic/backend/test packages. Measured on the
        # pr-723 base: go1.26.5 -> 13 packages ok / 7 FAIL, go1.9 -> 20 ok /
        # 0 FAIL, and all 6 era-1 base shas build EXIT 0. Available for
        # linux/arm64 as well as amd64.
        return "golang:1.9"

    def image_tag(self) -> str:
        return "base-era1"

    def workdir(self) -> str:
        return "base-era1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Validated before interpolation into the clone URL / WORKDIR paths.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

{fetch}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ResticEra1ImageDefault(Image):
    """Per-PR grading image for era 1 — this tier carries the hardening.

    ``prepare.sh`` checks this PR's ``base.sha`` out of the shared base's full
    history; ``Image._HARDENING_BLOCK`` then detaches at that literal sha and
    strips every other ref, the reflogs and all unreachable objects, so the PR's
    fix commit is not recoverable from git inside the image.
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

    def dependency(self) -> Image | None:
        return ResticEra1ImageBase(self.pr, self.config)

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
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

GOPATH=/home/{pr.repo}/vendor:/home/{pr.repo} GO111MODULE=off go test -v -count=1 ./src/restic/... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
GOPATH=/home/{pr.repo}/vendor:/home/{pr.repo} GO111MODULE=off go test -v -count=1 ./src/restic/...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
GOPATH=/home/{pr.repo}/vendor:/home/{pr.repo} GO111MODULE=off go test -v -count=1 ./src/restic/...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
GOPATH=/home/{pr.repo}/vendor:/home/{pr.repo} GO111MODULE=off go test -v -count=1 ./src/restic/...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # dependency() is an Image, so DockerfileEnhancer returns this content
        # verbatim and injects nothing -- the hardening must be emitted here.
        # ${BASE_COMMIT} is substituted with the literal sha because the pipeline
        # only passes REPO_URL/BASE_COMMIT build args to string-dependency images.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("restic", "restic_era1")
class ResticEra1(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ResticEra1ImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# instance.py routes on f"{org}/{number_interval}" whenever number_interval is
# set, so every dash-joined bundle value a record can carry must resolve to a
# class. These are the era 1 (src/ layout, GOPATH) bundles. Without them a delivered
# jsonl that carries number_interval raises "Instance 'restic/<bundle>' is not
# registered" before a single image is built. The bare "restic/restic_era1" key
# registered above still routes records whose number_interval is empty.
#
# Explicit dash-joined member lists, never ranges -- the bundles are sparse.
_BUNDLE_NIS_RESTIC_ERA1 = [
    "723-727-728-729-731-737-739-740-741-745-746-748-749-750-760-761-762-764",  # pr-723 (18 PRs, v0.3.3..v0.4.0)
    "763-766-768-771-773-775-778-779-782-783-792-793-794-795-798-800-803-806-808-809-814-817-829-835-837-840-844-845-847-850-853-854-855-857-858-860-861-864-865-866-867",  # pr-763 (41 PRs, v0.4.0..v0.5.0)
    "871-872-876-877-878-883-887-896-897-898-902-903-905-908-911-912-913-916-918-919-920-922-927-930-935-936-938-941-943-945-946-947-952-957-960-961-962-964-966-967-968-971",  # pr-871 (42 PRs, v0.5.0..v0.6.0)
    "975-978-990-993-994-997-998-999-1002-1003-1004-1008-1010-1011-1019-1024-1025-1027-1034-1035-1036-1038-1043-1045-1046-1048-1049-1050-1051-1056-1066-1070",  # pr-975 (32 PRs, v0.6.1..v0.7.0)
    "1044-1061-1126-1129-1130-1131-1133-1134-1138-1139-1144-1147-1148-1149-1150-1156-1157-1158-1164-1170-1174-1182-1184-1185-1187-1189-1191-1194-1196-1200-1201-1202-1203-1205-1209-1210-1214-1220-1222-1223-1224-1227-1228-1230-1231-1232-1236",  # pr-1044 (47 PRs, v0.7.1..v0.7.2)
    "1075-1077-1080-1082-1086-1090-1100-1103-1105-1107-1108-1112-1115-1117-1121-1122-1124",  # pr-1075 (17 PRs, v0.7.0..v0.7.1)
]

for _ni in _BUNDLE_NIS_RESTIC_ERA1:
    Instance.register("restic", _ni)(ResticEra1)
