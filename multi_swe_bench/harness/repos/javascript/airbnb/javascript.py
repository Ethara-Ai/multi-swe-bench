"""airbnb/javascript config.

Routing
-------
Serves the `*_lht_final.jsonl` bundles through both paths Instance.create()
offers: the plain "airbnb/javascript" key for records with no `number_interval`,
and one key per delivered dash-joined bundle for records that carry one
(registered at the bottom of this file). Every key maps to this one class -- the
bundles differ only in which PRs they cover, not in how the repo builds or tests.

Two levels
----------
`<org>_m_<repo>:base` -- built once per repo FROM the public node:18-bullseye.
It carries the toolchain and a full clone of the repository at whatever the
default branch happens to be, plus the `npm install` for that tree. It is NOT
pinned to any commit: there is one base for all 7 bundles, so pinning it to one
PR's sha would be wrong for the other six.

`<org>_m_<repo>:pr-<n>` -- built FROM that base, one per instance. This is where
`${BASE_COMMIT}` lives: it checks out the instance's own sha against the history
the base already cloned, re-runs `npm install` for that tree, and applies the
per-PR scripts and patches. Everything the two trees share -- the apt layer, the
git objects, the bulk of node_modules -- comes from the base layer instead of
being rebuilt 7 times.

Why the base emits its own `# syntax` directive
-----------------------------------------------
DockerfileEnhancer returns a Dockerfile verbatim once it carries the BuildKit
syntax directive (image.py:309). The base needs that opt-out: the enhancer's
`_inject_final_sanitize` appends the `${BASE_COMMIT}`-pinned hardening block to
anything containing a `git clone`, and in an image that has no BASE_COMMIT that
would expand to `git checkout --detach ""` and fail the build. The PR image
needs no directive -- its dependency() is an Image, which the enhancer already
returns verbatim -- so it adds `Image._HARDENING_BLOCK` itself, where pinning is
correct because it is per-instance.

Multi-arch with a chained base works because --output_tar is set: each build
extracts its OCI layout to `<name>.tar.d` (docker_util.py:253), and the PR build
resolves the base through `--build-context <ref>=oci-layout://<dir>`
(build_dataset.py:624-631) rather than trying to pull it from a registry.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The repo is a monorepo and only these two packages carry a `tests-only`
# script. Shared verbatim by the three eval phases, so it lives at module scope
# rather than being repeated in each file body.
_RUN_TESTS = """
for pkg in packages/eslint-config-airbnb-base packages/eslint-config-airbnb; do
    if [ -d "$pkg" ] && [ -f "$pkg/package.json" ]; then
        echo "Running tests in $pkg..."
        cd "$pkg"
        npm run tests-only
        cd {repo_dir}
    fi
done
"""


class ImageBase(Image):
    """Level 1: toolchain + full clone + common node_modules, shared by every PR."""

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
        return "node:18-bullseye"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        # Carries the syntax directive itself so DockerfileEnhancer no-ops --
        # see the module docstring. Nothing here may reference ${BASE_COMMIT}.
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
# build_dataset passes BASE_COMMIT for every string-dependency image
# (build_dataset.py:616). Declared so it is consumed rather than warned about;
# this image is deliberately not pinned to it.
ARG BASE_COMMIT=""

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

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

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN npm install || true

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """Level 2: one image per instance, pinned to that instance's base commit."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def repo_dir(self) -> str:
        return f"/home/{self.pr.repo}"

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_dir = self.repo_dir
        run_tests = _RUN_TESTS.format(repo_dir=repo_dir)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
# Build-time setup for one instance. The repo and the common node_modules come
# from the base image; the Dockerfile has already checked out ${BASE_COMMIT}.
# The hardening block that follows re-checks-out ${BASE_COMMIT} and prunes every
# other object, so nothing here may edit a tracked file -- npm install only
# writes node_modules, which is untracked.
set -eo pipefail

export CI=true

cd """
                + repo_dir
                + """
git reset --hard
bash /home/check_git_changes.sh

npm install || true

# Pin React to the API era these configs were written against.
#
# packages/eslint-config-airbnb declares `"react": ">= 0.13.0"` -- an unbounded
# range -- so npm resolves today's latest (React 19). eslint-plugin-react's
# `react/no-deprecated` rule judges the source against whatever React version it
# detects, so against React 19 it flags lifecycle methods such as
# componentWillMount that were entirely valid when these tests were written.
# That turns the *pre-patch* baseline red, which disqualifies the instance
# before any patch is applied.
#
# 16.8.6 rather than a date-derived version on purpose: resolving with
# `npm install --before=<commit date>` lands on 16.9.0 for the 2019 bundles, and
# 16.9.0 is precisely the release that deprecated those methods, so the baseline
# stays red. 16.8.6 is the last release before that deprecation. Verified
# against all 7 bundles: it makes pr-1255 a valid instance and leaves the
# already-valid pr-2249 / pr-1538 unchanged.
#
# --no-save keeps package.json untouched, so the working tree stays clean for
# check_git_changes.sh and for the hardening block that follows. Only packages
# that already resolved a React are touched.
for _pkg in packages/eslint-config-airbnb packages/eslint-config-airbnb-base; do
    if [ -d "$_pkg/node_modules/react" ]; then
        (cd "$_pkg" && npm install --no-save --no-audit --no-fund \\
            react@16.8.6 react-dom@16.8.6) || true
    fi
done
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd """
                + repo_dir
                + "\n"
                + run_tests,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd """
                + repo_dir
                + """
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
"""
                + run_tests,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd """
                + repo_dir
                + """
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
"""
                + run_tests,
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # dependency() is an Image, so DockerfileEnhancer returns this verbatim:
        # no REPO_URL/BASE_COMMIT build args arrive and no infra block is
        # injected, so BASE_COMMIT is baked in as an ARG default here. The clone
        # (and its full history) already exists in the base layer, so this only
        # needs to check the instance's own sha out of it.
        header = f"""FROM {base.image_name()}:{base.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
{Image._CHECKOUT_BASE_COMMIT}

{copy_commands}
RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening: detach at ${BASE_COMMIT}, drop origin,
        # delete every ref, expire reflogs, gc/repack, then assert the tree holds
        # nothing but this commit's ancestry. Concatenated raw rather than
        # interpolated so its ${BASE_COMMIT} / %(refname) tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("airbnb", "javascript")
class AirbnbJavaScript(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

        # Strip ANSI escape sequences
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_escape.sub("", test_log)

        for line in clean_log.splitlines():
            line = line.strip()

            ok_match = re.match(r"^ok\s+\d+\s*(?:[-–]\s*)?(.+)$", line)
            if ok_match:
                test_name = ok_match.group(1).strip()
                if test_name:
                    passed_tests.add(test_name)
                continue

            not_ok_match = re.match(r"^not\s+ok\s+\d+\s*(?:[-–]\s*)?(.+)$", line)
            if not_ok_match:
                test_name = not_ok_match.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                continue

            bare_ok = re.match(r"^ok\s+\d+$", line)
            if bare_ok:
                passed_tests.add(f"test_{bare_ok.group(0)}")
                continue

            bare_not_ok = re.match(r"^not\s+ok\s+\d+$", line)
            if bare_not_ok:
                failed_tests.add(f"test_{bare_not_ok.group(0)}")
                continue

        # Ensure no test is counted as both passing and failing
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# One key per delivered bundle, for records that carry a `number_interval`.
# Instance.create() routes on f"{org}/{number_interval}" when the field is set
# and only falls back to f"{org}/{repo}" when it is empty (instance.py:42-49),
# so both spellings have to resolve to this class. Generated from
# dataset_4/airbnb__javascript_lht_final.jsonl as the dash-join of each record's
# prs_in_bundle -- the same derivation the producer uses
# (build_lht_dataset.py:537-538).
_NUMBER_INTERVALS = [
    "1255-1583-1863-1864-1962-1966-2022-2042-2043-2052-2056-2060-2062-2064",
    "1403-1477-1514-1533-1543",
    "1538-1547-1578-1587-1595-1600-1602-1605-1606-1607-1608-1610-1611-1612-1613-1616-1618-1620-1621-1622-1625-1628-1629-1635-1648-1652-1661-1662-1668-1669-1685-1691-1693-1694-1698-1699-1704-1708-1710-1712-1714-1721-1722-1727-1729-1732-1736-1737-1740-1742-1743-1746-1749-1751-1756-1760-1761-1768-1770-1772-1774-1778-1779-1780-1781-1782-1784-1787-1790-1793-1794-1798-1799-1801-1802-1809-1818-1820-1822-1823-1828-1829-1831",
    "2074-2085-2090-2101-2108-2109-2110-2112-2113-2121-2130-2132-2138-2157-2184-2186-2192",
    "2168-2193-2194-2197-2198-2204-2207-2230-2235-2237-2238-2240",
    "2249-2250-2269-2283-2287-2303-2304-2306-2310-2315-2318-2320-2322",
    "2329-2333-2341-2356-2362-2366-2404-2406-2407-2408-2413-2419-2420-2422-2423-2438-2471-2474-2482-2483-2489-2491-2495",
]

for _interval in _NUMBER_INTERVALS:
    Instance.register("airbnb", _interval)(AirbnbJavaScript)
