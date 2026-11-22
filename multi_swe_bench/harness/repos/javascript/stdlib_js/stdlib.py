"""stdlib-js/stdlib config.

One shared base image plus one image per PR. Every row is on ``develop`` and
every patch applies at ``base.sha``. The design is driven by three properties
of the repo.

1. THE HISTORY IS ~2.2 GB.
   A full clone in every per-PR, per-arch image is impractical, and it would
   carry the fix commit in its history. The base holds only the toolchain; each
   PR image fetches its own ``base.sha`` at depth 1, so the repo has exactly one
   commit and the hardening pass only has to drop the remote and refs.

2. THE TESTS NEED ONE EXTERNAL PACKAGE.
   Every ``@stdlib/*`` require resolves to the monorepo's own
   ``lib/node_modules/@stdlib`` through normal Node module resolution, so the
   root ``package.json`` (60 published ``@stdlib`` deps, 138 devDependencies,
   several of them native or git-hosted) is never installed. Unit tests only
   need ``tape`` (the ``kgryte/tape#fix/globby`` fork the repo pins), plus
   ``proxyquire`` for the few that stub modules. Both live in
   ``/home/node_modules``, outside the repo, so the tree stays clean.

3. TEST SELECTION AND NAMING.
   Only the ``test/test*.js`` files the test patch touches are run, each as a
   plain ``node`` script emitting TAP. Native-addon, ESM-module and CLI tests
   need a node-gyp build or a spawned process and are skipped. A file the test
   patch creates is dropped at the baseline stage. Results are reported per
   ``tape`` test (``[file] > test name``), not per assertion: assertion numbers
   shift whenever the test patch adds an assertion earlier in the file, which
   would rename every later assertion between stages.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22-bookworm"

# ``tape`` as pinned by the root package.json (git+https fork, branch
# fix/globby), fetched as a tarball at a fixed commit so npm never rewrites it
# to an SSH URL.
_TAPE = "https://codeload.github.com/kgryte/tape/tar.gz/360c2fda548ed7a0120491a90196518a4b1dbaa7"
_PROXYQUIRE = "proxyquire@2.1.3"

_TEST_FILE_RE = re.compile(
    r"^lib/node_modules/@stdlib/.+/test/test[^/]*\.js$"
)
_SKIP_TEST_RE = re.compile(r"/test/test\.(native|module|cli)\.js$")


def _test_files(test_patch: str) -> list[str]:
    """The runnable ``test/test*.js`` files the test patch touches."""
    files: set[str] = set()
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", test_patch or "", re.M):
        for path in m.groups():
            if _TEST_FILE_RE.match(path) and not _SKIP_TEST_RE.search(path):
                files.add(path)
    return sorted(files)


class StdlibImageBase(Image):
    """Shared toolchain image: Node 22 plus the test runner.

    Holds no repository, so it is valid for every PR. Emitting the BuildKit
    syntax directive opts out of ``DockerfileEnhancer``, which would otherwise
    add a full clone pinned to one PR's ``BASE_COMMIT``; this file therefore
    emits the proxy, cert and label infrastructure itself.
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
        return _NODE_IMAGE

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

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

# Test runner outside the repo; see point 2 of the config module docstring.
WORKDIR /home/
RUN set -eux; \\
    echo '{{"name":"stdlib-test-runner","private":true}}' > /home/package.json; \\
    for i in 1 2 3 4 5; do \\
        npm install --no-save --ignore-scripts {_TAPE} {_PROXYQUIRE} && break; \\
        [ "$i" -lt 5 ] || exit 1; sleep 15; \\
    done; \\
    rm -f /home/package.json /home/package-lock.json; \\
    node -e "require('tape'); require('proxyquire')"

CMD ["/bin/bash"]
"""


class StdlibImageDefault(Image):
    """Per-PR image: fetches ``base.sha`` at depth 1."""

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
        return StdlibImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        tests = " ".join(_test_files(self.pr.test_patch))

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
                "run_tests.sh",
                r"""#!/bin/bash
# Shared test driver for all three stages: the test files the test patch
# touches, minus any that do not exist in this tree yet. Each file runs as its
# own process so one crash cannot hide the others.
FILES=()
for f in [[TESTS]]; do
    if [ -f "$f" ]; then
        FILES+=("$f")
    fi
done

if [ ${#FILES[@]} -eq 0 ]; then
    echo "run_tests: none of the target test files exist in this tree, no tests to run"
    exit 0
fi

for f in "${FILES[@]}"; do
    echo "### FILE: $f"
    node "$f" 2>&1 || true
done
""".replace("[[TESTS]]", tests),
            ),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

# Depth-1 fetch of the one commit this instance is anchored to; see point 1
# of the config module docstring. Retried: DNS to github.com flakes mid-build.
rm -rf /home/[[REPO]]
git init -q /home/[[REPO]]
cd /home/[[REPO]]
git remote add origin https://github.com/[[ORG]]/[[REPO]].git
for i in 1 2 3 4 5; do
    git fetch --no-tags --depth 1 origin [[SHA]] && break
    echo "prepare: fetch failed (attempt $i), retrying" >&2
    [ "$i" -lt 5 ] || exit 1
    sleep 15
done
git checkout -q --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "[[SHA]]"
bash /home/check_git_changes.sh
git apply --check /home/test.patch /home/fix.patch
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO]]
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
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

        # The repo already holds a single commit (depth-1 fetch); this pass
        # removes the remote and every ref so nothing can be fetched or read
        # beyond base.sha, and asserts that state. No --aggressive: the tree is
        # large and a depth-1 pack has nothing to gain from a full recompute.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    rm -f .git/shallow.lock .git/FETCH_HEAD; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "1"

CMD ["/bin/bash"]
"""


@Instance.register("stdlib-js", "stdlib")
class Stdlib(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StdlibImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # tape TAP: "# <test name>" opens a test, then "ok N msg" / "not ok N
        # msg" assertions (optionally "# SKIP"). A test is reported once per
        # file under its name; see point 3 of the config module docstring.
        assert_re = re.compile(r"^(ok|not ok) \d+\b(.*)$")
        current_file = ""
        current: Optional[str] = None
        seen: dict[str, int] = {}
        outcome: dict[str, set[str]] = {}

        for raw in test_log.splitlines():
            line = raw.strip()
            if line.startswith("### FILE: "):
                current_file = line[len("### FILE: "):].strip()
                current = None
                seen = {}
                continue
            if line.startswith("1.."):
                # Plan line: the trailing "# tests/# pass/# fail" are summaries.
                current = None
                continue
            if line.startswith("# "):
                name = line[2:].strip()
                if not current_file or not name:
                    continue
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name = f"{name} #{seen[name]}"
                current = f"[{current_file}] > {name}"
                continue
            m = assert_re.match(line)
            if m and current:
                status, rest = m.groups()
                if re.search(r"#\s*SKIP\b", rest, re.I):
                    state = "skip"
                elif status == "ok":
                    state = "pass"
                else:
                    state = "fail"
                outcome.setdefault(current, set()).add(state)

        for name, states in outcome.items():
            if "fail" in states:
                failed_tests.add(name)
            elif "pass" in states:
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
