from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# GOPATH layout: $GOPATH/src/github.com/minio/mc -> /home/mc (symlink), so the
# hardening block (which runs in /home/mc) and the GOPATH build agree.
GOPATH_DIR = "/go/src/github.com/minio/mc"


class ImageBase(Image):
    """Shared pre-modules base image (built once, reused by every premod PR).

    Clone-only with full history kept so any PR's base.sha is reachable; the PR
    layer does the checkout + strict history-strip. The `# syntax` directive opts
    out of DockerfileEnhancer so this hand-written layout is used verbatim.
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
        return "golang:1.10"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-premod"

    def workdir(self) -> str:
        return "base-premod"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    GOPATH=/go

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}
# GOPATH layout: symlink $GOPATH/src/github.com/{org}/{repo} -> /home/{repo}.
RUN mkdir -p /go/src/github.com/{org} && ln -sfn /home/{repo} {GOPATH_DIR}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

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

    def dependency(self) -> "Image":
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

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

cd {gopath_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go test -v -vet=off -count=1 ./... || true

""".format(pr=self.pr, gopath_dir=GOPATH_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd {gopath_dir}
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
# Filter to only existing directories
EXISTING_PKGS=""
for pkg in $PKGS; do
  dir="${{pkg#./}}"
  if [ -d "$dir" ]; then
    EXISTING_PKGS="$EXISTING_PKGS $pkg"
  fi
done
PKGS="${{EXISTING_PKGS## }}"
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
for pkg in $PKGS; do
  go test -v -vet=off -count=1 -timeout 15m "$pkg" || true
done

""".format(gopath_dir=GOPATH_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd {gopath_dir}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
# Filter to only existing directories
EXISTING_PKGS=""
for pkg in $PKGS; do
  dir="${{pkg#./}}"
  if [ -d "$dir" ]; then
    EXISTING_PKGS="$EXISTING_PKGS $pkg"
  fi
done
PKGS="${{EXISTING_PKGS## }}"
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
for pkg in $PKGS; do
  go test -v -vet=off -count=1 -timeout 15m "$pkg" || true
done

""".format(gopath_dir=GOPATH_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd {gopath_dir}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
# Extract packages affected by patches
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.$')
# Filter to only existing directories
EXISTING_PKGS=""
for pkg in $PKGS; do
  dir="${{pkg#./}}"
  if [ -d "$dir" ]; then
    EXISTING_PKGS="$EXISTING_PKGS $pkg"
  fi
done
PKGS="${{EXISTING_PKGS## }}"
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
for pkg in $PKGS; do
  go test -v -vet=off -count=1 -timeout 15m "$pkg" || true
done

""".format(gopath_dir=GOPATH_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history). prepare.sh checks out this PR's base.sha, then the canonical
        # hardening block detaches at that literal sha and strips every other
        # ref/reflog so later commits (the fix) are unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("minio", "mc_premod")
class Mc_premod(Instance):
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
            return test_name

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
# Delivery scope = RESOLVED (valid) bundles only; keys == #delivered instances (PIPELINE §11c).
# Pre-modules-era bundles (GOPATH, golang:1.10).
_BUNDLE_NIS_MC_PREMOD = [
    "1937-1938-1939-1940-1942-1943-1945-1946-1947-1950-1951-1953-1955-1958-1959-1966-1967-1968-1969-1973-1975-1976-1977-1978-1979-1980-1982-1985-1986-1987-1988-1990-1992-1994",
    "2005-2006-2008-2009-2010-2013-2014-2015-2019-2020-2021-2023-2024-2026-2030-2031-2033-2036-2037-2040-2041-2043-2045-2049-2051-2052-2054-2059-2062-2063-2065-2066-2067-2068-2069-2070-2075-2076-2078-2080-2081-2082-2084-2087-2091-2095-2096-2097-2098-2100-2102-2103-2104",
    "2101-2106-2109-2111-2113-2114-2116-2118-2119-2120-2121-2122-2126-2127-2131-2133-2135-2138-2139-2141-2143-2146-2149-2150-2153-2155-2160-2165-2167-2171-2173-2177-2178-2179-2180-2181-2182",
    "2142-2293-2329-2350-2360-2361-2362-2363-2365-2370-2383",
    "2265-2275-2276-2282-2285-2287-2289-2290-2291-2292-2294-2299-2300-2302-2303-2306-2310-2313-2318-2320-2322-2323-2325-2326-2331",
    "2511-2518-2520-2521-2523-2525-2529",
    "2649-2659",
]
for _ni in _BUNDLE_NIS_MC_PREMOD:
    Instance.register("minio", _ni)(Mc_premod)
