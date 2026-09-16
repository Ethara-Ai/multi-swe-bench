from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "Stone_Soup_204_to_204"

BASE_TAG = "base-204-to-204"


PYTEST_START = "-----MSB_PYTEST_START-----"
PYTEST_END = "-----MSB_PYTEST_END-----"


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

git update-index -q --really-refresh || true

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

for p in "$@"; do
    if git apply --whitespace=nowarn "$p"; then
        echo "apply_patch: applied $p"
    else
        echo "apply_patch: plain apply failed for $p, retrying with --3way"
        git apply --3way --whitespace=nowarn "$p"
        echo "apply_patch: applied $p with --3way"
    fi
    git add -A
done

if git grep -qI -e '^<<<<<<< ' -- . 2>/dev/null; then
    echo "apply_patch: conflict markers survived the merge"
    git grep -nI -e '^<<<<<<< ' -- . | head
    exit 1
fi

# A patch may ADD A DEPENDENCY, and site-packages was populated from the
# pre-patch manifests. THIS PR DOES EXACTLY THAT -- its fix patch adds
# `numpy>=1.17` to setup.py's install_requires. Without re-installing, the tests
# run against whatever numpy the base resolved, so the act measures the wrong
# environment. Re-install only when a manifest actually moved.
if git diff --name-only HEAD -- setup.py setup.cfg pyproject.toml requirements*.txt \\
        | grep -q .; then
    echo "apply_patch: dependency manifests changed, re-installing"
    git diff --name-only HEAD -- setup.py setup.cfg pyproject.toml requirements*.txt
    bash /home/pip_install.sh
else
    echo "apply_patch: no dependency manifest touched, install left as built"
fi
"""


_PIP_INSTALL_SH = """#!/bin/bash
# One installer, shared by prepare.sh and apply_patch.sh, so the two can never
# drift into installing differently.
set -eo pipefail
cd /home/{repo}

# PyPI is a third party and it flakes. Retry before believing a failure: a
# single transient 5xx would otherwise turn real evidence into an empty act.
for i in 1 2 3 4; do
    if pip install --no-cache-dir -e '.[dev]'; then break; fi
    echo "pip_install: attempt $i failed (index flake?), retrying"
    sleep $((i * 15))
    [ "$i" = "4" ] && exit 1
done
"""


_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1

cd /home/{repo}

git reset --hard
git clean -fdq

bash /home/check_git_changes.sh

# The image must sit on the dataset's base_commit and nothing else. The PR layer
# checked it out; assert it here so a silent drift fails the BUILD rather than
# surfacing as unexplainable test results three acts later.
test "$(git rev-parse HEAD)" = "{sha}"
echo "prepare: HEAD is {sha}"

bash /home/pip_install.sh

# Gates: FILESYSTEM AND IMPORT CHECKS ONLY -- never run the test suite here.
# A gate that executes the project's own runner can take longer than every act
# combined and hides its failure behind a fallback; whether the tests PASS is
# the acts' question, where a failure is attributable to a PR.
python -c "import stonesoup; print('stonesoup import ok')"
python -m pytest --version
echo "prepare: package imports and pytest is runnable"
"""


_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail
export PIP_DISABLE_PIP_VERSION_CHECK=1
# These suites assert on numeric formatting and on datetime handling; pin the
# clock rather than letting the host decide.
export TZ=utc

cd /home/{repo}

TARGETS=$(cat /home/pytest_targets.txt 2>/dev/null || true)

echo "{py_start}"
if [ -n "$TARGETS" ]; then
    EXISTING=""
    for f in $TARGETS; do
        [ -f "$f" ] && EXISTING="$EXISTING $f"
    done
    if [ -n "$EXISTING" ]; then
        echo "pytest targets:$EXISTING"
        rc=0
        # -p no:cacheprovider so a previous act's cache cannot influence this
        # one; --continue-on-collection-errors so ONE unimportable module does
        # not discard every test already collected (FLOW Issue 28); no
        # --flake8/--cov, which would mix lint findings into test results.
        # The ceiling turns a hung suite into a failed act with a real log
        # rather than an instance that stalls the whole run.
        timeout --signal=KILL 480 \\
            python -m pytest -v --tb=short --no-header -p no:cacheprovider \\
                --continue-on-collection-errors $EXISTING || rc=$?
        echo "pytest exit: $rc"
        [ "$rc" = "137" ] && echo "pytest: KILLED by the 480s ceiling (hung suite)"
    else
        echo "pytest: none of the target files exist at this tree state"
    fi
else
    echo "pytest: this PR's test patch names no runnable test file"
fi
echo "{py_end}"
"""


class StoneSoup204ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    # The base commit is from 2020-04 and CircleCI tested python 3.5 - 3.8
    # there. 3.8 is the newest the project verified at this commit, so it is the
    # one least likely to surprise the numeric code, and python:3.8-bullseye is
    # a real multi-arch tag, which the delivery requires.
    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-bullseye"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        # The `-C <dir>` form throughout, comments included: core's
        # DockerfileEnhancer scans this TEXT for a bare git verb and, on a base
        # whose dependency() is a string, injects a commit checkout plus the
        # history scrub. Under this layout that block belongs in the PR layer,
        # and a shared base pinned to one PR's commit prunes away every other
        # PR's history -- silently and order-dependently.
        #
        # The fetch is depth 1 over exactly this bundle's base commits: the PR
        # layer's hardening block ends in an aggressive gc, and that repack is
        # charged against whatever history the base carries.
        return """# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{{org}}/{{repo}}.git"
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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{{org}}/{{repo}}" \\
      org.opencontainers.image.description="{{org}}/{{repo}} Docker image" \\
      org.opencontainers.image.source="https://github.com/{{org}}/{{repo}}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN sed -i '/-security/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git -C /home clone "${{REPO_URL}}" {repo}; then break; fi; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    test -d /home/{repo}/.git

{clear_env}

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            org=org,
            global_env=self.global_env,
            clear_env=self.clear_env,
            repo=repo,
        )


class StoneSoup204ImageDefault(Image):
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
        return StoneSoup204ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_patch_files(self) -> list[str]:
        out = []
        for line in self.pr.test_patch.split("\n"):
            if line.startswith("+++ b/"):
                out.append(line[6:].strip())
        return out

    def pytest_targets(self) -> list[str]:
        """The test files this PR's own test patch touches.

        Running the whole suite would drown the graded transition in unrelated
        noise; the evidence f2p/n2p needs lives in the files the test patch
        changes.
        """
        return sorted({f for f in self._test_patch_files()
                       if f.endswith(".py")
                       and ("/tests/" in f or "/test_" in f
                            or f.rsplit("/", 1)[-1].startswith("test_"))})

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "pytest_targets.txt",
                 "\n".join(self.pytest_targets()) + "\n"),
            File(".", "check_git_changes.sh",
                 _CHECK_GIT_CHANGES_SH.format(repo=repo)),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH.format(repo=repo)),
            File(".", "pip_install.sh", _PIP_INSTALL_SH.format(repo=repo)),
            File(".", "prepare.sh", _PREPARE_SH.format(repo=repo, sha=sha)),
            File(".", "run_tests.sh",
                 _RUN_TESTS_SH.format(repo=repo, py_start=PYTEST_START,
                                      py_end=PYTEST_END)),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # The sha is written LITERALLY: build_dataset.py supplies REPO_URL and
        # BASE_COMMIT only when dependency() returns a str -- the base alone --
        # so a PR layer writing ${BASE_COMMIT} would expand it to the empty
        # string and the unquoted checkout would land on the default branch.
        sha = self.pr.base.sha

        # DERIVED from the harness's own definition, never retyped.
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


@Instance.register("dstl", NUMBER_INTERVAL)
class StoneSoup204To204(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StoneSoup204ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    @staticmethod
    def _between(text: str, start: str, end: str) -> str:
        i = text.find(start)
        if i == -1:
            return ""
        i += len(start)
        j = text.find(end, i)
        return text[i:j] if j != -1 else text[i:]

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        # Occurrence counter: a suite that builds tests in a loop can give
        # several tests an identical id; without this they collapse into ONE
        # set entry and the count silently drops.
        seen: dict[str, int] = {}

        def uniq(name: str) -> str:
            n = seen[name] = seen.get(name, 0) + 1
            return name if n == 1 else f"{name} #{n}"

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        body = self._between(text, PYTEST_START, PYTEST_END)

        # `\S+` for the id is WRONG: pytest writes a parametrised id verbatim
        # and a parameter may contain SPACES, e.g.
        #     tests/test_x.py::test_case[a b c] PASSED   [ 12%]
        # `\S+` stops at the space inside the bracket and the line is dropped
        # SILENTLY. Match the id lazily up to the status word instead, and
        # require the status to be followed by end-of-line or pytest's
        # `[ NN%]` progress column so a status word inside a test name cannot
        # terminate the id early.
        line_re = re.compile(
            r"^(?P<name>\S[^\s:]*::.*?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?=\s*(?:\[\s*\d+%\])?\s*$)")

        for line in body.splitlines():
            m = line_re.match(line.strip())
            if not m:
                continue
            # The tool name comes FIRST: report.py's cheating guard splits a
            # test name on "::" and treats the head as a file path, so a head
            # of "pytest" matches no file and a PR that CREATES a test file
            # cannot trip guard_fix_patch_touched_tests.
            name = uniq(f"pytest::{m.group('name')}")
            st = m.group("status")
            if st in ("PASSED", "XPASS"):
                passed.add(name)
            elif st in ("FAILED", "ERROR"):
                failed.add(name)
            else:
                skipped.add(name)

        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
