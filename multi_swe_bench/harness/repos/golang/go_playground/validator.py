import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# PRs whose base commit predates Go modules (no go.mod in the tree). They need
# the module bootstrapped before anything can build, which ValidatorV9ImageDefault
# does. NOTE this is deliberately a set and not a "number <= N" cutoff: the eras
# interleave by PR number -- 539 (v10.0) sits between 496 and 542, which are both
# v9.30 -- because the v10 module rewrite landed on a branch while v9 PRs were
# still merging. Verified against the tree of every base commit in the delivered
# JSONL: exactly these three have no go.mod.
_V9_ERA_PRS = frozenset({484, 496, 542})


class ValidatorImageBase(Image):
    """Toolchain-only base image, SHARED by every go-playground/validator PR.

    Constant tag ``base``, so exactly one of these exists repo-wide and the
    expensive apt layer is built once instead of 34 times. That matters most for
    multi-arch builds, where the linux/amd64 half of every ``apt-get install``
    runs under QEMU emulation on an arm64 host.

    This image MUST NOT ``git clone`` the repository, and must not use
    ``BASE_COMMIT``. Its ``dependency()`` is a *string* (``golang:latest``) and
    this Dockerfile carries no ``# syntax`` directive, so ``DockerfileEnhancer``
    engages and its ``_standardize_repo_fetch`` would rewrite any
    ``RUN git clone ... /home/<repo>`` (or ``COPY <repo> /home/<repo>``) into
    ``git clone "${REPO_URL}"`` + ``git checkout ${BASE_COMMIT}`` + the
    history-stripping hardening block. ``build_dataset`` passes a BASE_COMMIT
    buildarg for string dependencies, so -- because the tag is constant -- that
    would force-pin this ONE shared image to whichever PR built first and
    gc-prune the rest of history, breaking ``git checkout`` for every other PR.
    That was a real bug in the previous ``ValidatorV9ImageBase``.

    So the clone, the per-PR ``${BASE_COMMIT}`` checkout and the hardening block
    all live in the per-PR images, whose ``dependency()`` is an ``Image`` and
    whose Dockerfiles the enhancer therefore leaves verbatim.
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
        return "golang:latest"

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

        # Deliberately no `git clone` / `COPY <repo>` here -- see class docstring.
        # These are the same packages Image.dockerfile() installs by default, so
        # the per-PR images lose nothing by depending on this instead of
        # golang:latest directly.
        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

CMD ["/bin/bash"]
"""


class ValidatorImageDefault(Image):
    """v10-era per-PR image (every PR whose base commit already has a go.mod)."""

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
        # An Image (not a string): the shared toolchain base, so the apt layer is
        # built once for all 34 PRs. Because this is an Image dependency the
        # DockerfileEnhancer returns dockerfile() verbatim -- it does NOT inject
        # the clone/checkout or append _HARDENING_BLOCK -- so dockerfile() below
        # writes all of that out explicitly. Pinning ${BASE_COMMIT} here is
        # correct because this image is per-PR; doing it in the shared base is
        # what caused the old pinning bug (see ValidatorImageBase docstring).
        return ValidatorImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        # Staged into /home/ after the checkout and before the hardening block.
        # These files live outside /home/<repo>, so the hardening pass (which
        # only operates inside the git tree) leaves them untouched.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
                "prepare.sh",
                """#!/bin/bash
# Warm the Go module + build caches at image-build time so the eval runs don't
# need network. The repo is already cloned and checked out at the base commit by
# dockerfile(), so this script performs no git checkout itself. `go test` may
# fail (|| true) because its only purpose here is to populate caches; the real
# pass/fail signal comes from the run/test-run/fix-run scripts.
set -e

cd /home/{pr.repo}

go mod download || true
go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # ${BASE_COMMIT} must be defined before the checkout and the hardening
        # block reference it. build_dataset only injects a BASE_COMMIT buildarg
        # for *string* dependencies, so bake the PR's base.sha in as the ARG
        # default (still overridable at build time).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{self.extra_setup()}

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via an f-string) so its ${BASE_COMMIT} and
        # %(refname) tokens stay literal. Runs AFTER prepare.sh so the cache
        # pre-warm still sees full history, and before CMD so it is not dead
        # code. Verified to defeat git log/show/reflog/fsck recovery of the
        # upstream fix commit.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("go-playground", "validator")
class Validator(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        # Era dispatch. The delivered JSONL carries no number_interval and no
        # tag, so Instance.create() routes every PR -- both eras -- to this one
        # class via "go-playground/validator"; the split therefore has to happen
        # here rather than through the registry key.
        if self.pr.number in _V9_ERA_PRS:
            # Imported inside the method, not at module scope: validator_542_to_484
            # imports ValidatorImageBase from this module, so a module-level
            # import here would be a circular import.
            from multi_swe_bench.harness.repos.golang.go_playground.validator_542_to_484 import (
                ValidatorV9ImageDefault,
            )

            return ValidatorV9ImageDefault(self.pr, self._config)
        return ValidatorImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Instance.create() routes on f"{org}/{number_interval}" whenever a delivered PR
# carries one, and falls back to f"{org}/{repo}" otherwise. The delivered
# go-playground__validator_lht_final.jsonl carries no number_interval, so the
# "go-playground/validator" registration above is what actually routes all 34
# PRs today -- but a re-delivery that populates the field from prs_in_bundle
# would otherwise raise "Instance ... is not registered" before any image is
# built. Registering both shapes keeps the registry robust either way.
# The v9-era intervals (484-489, 496-529-530-535, 542-543) are registered in
# validator_542_to_484.py and are intentionally NOT repeated here.
_NUMBER_INTERVALS = [
    "539-541-547-548-550-558-560",
    "563-569-571-572-574-575",
    "577-578-579-582-583-588-598-600-607-609-612",
    "613-615-620-630-635-641-642-644-657-658-664-666-667",
    "671-673-679-680-706-710-712-716-732-733-746",
    "694-820-824-825-847-856-858-875-884-890-891-898-912-914-917-924-930-934",
    "752-754-758-759-767",
    "765-774-783-798-799-801",
    "771-788",
    "786-792-793",
    "804-809-811-814-815-816",
    "826-831-833-867-873",
    "939-976-1086-1088-1090-1097-1100",
    "948-982",
    "967-981-983-997-1005-1009-1011-1012-1013-1014-1022-1023-1024-1041-1045-1047-1048-1062-1064-1066-1071-1076-1078-1080-1081",
    "975-1170",
    "1015-1026-1057-1058-1061",
    "1110-1114",
    "1121-1122-1125-1133-1134-1135",
    "1154-1202-1214-1322-1336-1338-1366-1373-1376-1378-1380-1381-1382-1383-1384-1391-1393-1395-1400-1405",
    "1166-1171-1184-1187-1189",
    "1196-1217-1242-1250-1261-1262-1270",
    "1200-1349-1358-1363-1394-1406-1412-1418-1419-1422-1423-1425-1431-1433-1436-1440-1442-1444-1445-1447",
    "1252-1253-1258",
    "1275-1277",
    "1284-1343-1417-1435-1456-1459-1461-1463-1464-1465-1467-1468",
    "1289-1346-1359-1362-1375",
    "1294-1302-1321-1326",
    "1307-1472-1473-1474-1476-1478-1479-1484-1485-1487-1490-1492-1495-1497-1498-1501-1503",
    "1482-1514-1516",
    "1504-1505-1507-1508-1509-1510",
]
# Legacy alias: this file previously registered only "go-playground/1110", the
# bare lead PR number rather than its bundle interval. Kept so an older JSONL
# delivered with number_interval="1110" still routes instead of hard-failing.
_NUMBER_INTERVALS.append("1110")

for _interval in _NUMBER_INTERVALS:
    Instance.register("go-playground", _interval)(Validator)
