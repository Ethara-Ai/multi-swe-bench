"""lobehub/lobehub config for PRs 71-6452 (single-app era, vitest)."""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Newline used inside f-strings, which cannot contain a backslash escape.
_NL = "\n"

# Copied into every per-PR image of both eras.
_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


# vitest release contemporary with this era, used only to replace a floating
# spec ("latest" / "*") that would otherwise resolve to a modern major.
_ERA_VITEST_PIN = "0.34.6"

# Appended verbatim to run.sh / test-run.sh / fix-run.sh. Expects PKG_MANAGER to
# be set and, in the patched stages, to run after `set +e`. Contains literal
# braces, so it is passed as a .format() ARGUMENT and never as part of a
# template.
_VITEST_RUNNER = """run_vitest() {
    if [ "$PKG_MANAGER" = "pnpm" ]; then
        pnpm vitest run --reporter=verbose 2>&1
    else
        npx vitest run --reporter=verbose 2>&1
    fi
}

install_missing() {
    if [ "$PKG_MANAGER" = "pnpm" ]; then
        pnpm add -D "$1" >/dev/null 2>&1 || true
    else
        npm install --no-save --legacy-peer-deps "$1" >/dev/null 2>&1 || true
    fi
}

VITEST_LOG=/tmp/vitest-run.log
run_vitest > "$VITEST_LOG" 2>&1

# A module that only the PATCHED tree imports -- typically a setup-file
# dependency the fix patch adds to package.json -- is absent from node_modules,
# so vitest aborts collection for EVERY suite and reports no tests at all.
# Install any such module once and re-run, so the emitted log is the repaired
# run. The name is matched against the npm package grammar, which rejects
# relative paths and "@/" import aliases. When nothing is missing, which is the
# case for every PR whose environment is already complete, the loop and the
# re-run are both skipped and the log is exactly what the single run produced.
MISSING=$(sed -n -e 's/.*Failed to resolve import "\\([^"]*\\)".*/\\1/p' \\
                 -e "s/.*Cannot find package '\\([^']*\\)'.*/\\1/p" "$VITEST_LOG" \\
          | grep -E '^(@[a-zA-Z0-9._-]+/)?[a-zA-Z0-9][a-zA-Z0-9._-]*$' | sort -u)

if [ -n "$MISSING" ]; then
    for pkg in $MISSING; do
        echo "vitest: installing missing module $pkg"
        install_missing "$pkg"
    done
    run_vitest > "$VITEST_LOG" 2>&1
fi

cat "$VITEST_LOG"
"""


def _clean_test_name(name: str) -> str:
    """Strip variable timing and metadata from test names for stable eval matching."""
    # Strip vitest file-level metadata: (2 tests) 75ms, (1 test | 1 failed) 120ms
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    # Strip parenthesized timing: (75ms), (150 ms), (8.954 s)
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


class LobeHubImageBase(Image):
    """The single shared base image for every lobehub PR, both eras.

    Both eras need the same toolchain (node 20 plus git and libvips) and the
    same full-history clone, so one ``base`` tag is built once for the repo and
    reused by every per-PR image rather than one base per era.
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
        return "node:20-bookworm"

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

        # Only emit the env sections when the run config defines them, so an
        # empty config does not leave stray blank lines in the Dockerfile.
        global_env = f"{_NL}{self.global_env}{_NL}" if self.global_env else ""
        clear_env = f"{_NL}{self.clear_env}{_NL}" if self.clear_env else ""

        # One shared base for the whole repo: clones the repo ONCE so the 1.2 GB
        # clone layer is not duplicated across every per-PR image. It stops at
        # ``git clone`` followed by ``CMD``: no checkout, no history hardening.
        # A single ``base`` tag is shared by every PR while each PR has its own
        # ``base.sha``, so checking out or pruning here would strip every other
        # PR's commit out of history. The per-PR checkout and the git-history
        # hardening both live in the per-PR Dockerfile, never in this base and
        # never in ``prepare.sh``.
        #
        # The leading ``# syntax`` directive makes DockerfileEnhancer.enhance()
        # emit this file verbatim, so the infrastructure block below is spelled
        # out in full here and neither ``_standardize_repo_fetch`` nor
        # ``_inject_final_sanitize`` can rewrite the clone or append a
        # BASE_COMMIT-pinned hardening block to the shared base.
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

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends git libvips-dev && rm -rf /var/lib/apt/lists/*
{global_env}
WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
{clear_env}
CMD ["/bin/bash"]
"""


class LobeHubImageDefaultEarly(Image):
    """PR-specific image for lobehub early era."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return LobeHubImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

# A few early commits float the vitest spec ("latest" / "*"). That resolves to a
# modern vitest which declares vite as a PEER dependency, and --legacy-peer-deps
# skips peers, so vitest cannot boot ("Cannot find package 'vite'") and no test
# is ever captured. Pin a floating spec to the release this era actually used,
# install against it, then restore package.json so the working tree stays
# pristine at the base commit and the patches still apply cleanly.
# Commits that pin a real range (every other PR of this era) skip this entirely.
PINNED=""
if node -e "const d=require('./package.json').devDependencies||{{}};const v=d.vitest||'';process.exit((v==='latest'||v==='*')?0:1)" 2>/dev/null; then
    node -e "const fs=require('fs');const p=JSON.parse(fs.readFileSync('package.json','utf8'));p.devDependencies.vitest='{vitest_pin}';fs.writeFileSync('package.json',JSON.stringify(p,null,2));"
    PINNED=1
    echo "prepare: pinned floating vitest spec to {vitest_pin}"
fi

PKG_MANAGER=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log('pnpm'); else console.log('npm'); }} catch(e) {{ console.log('npm'); }}")

if [ "$PKG_MANAGER" = "pnpm" ]; then
    PNPM_VERSION=$(node -e "try {{ const pm = require('./package.json').packageManager; console.log(pm.split('@')[1]); }} catch(e) {{ console.log('latest'); }}")
    npm install -g "pnpm@${{PNPM_VERSION}}"
    pnpm install --no-frozen-lockfile || true
else
    npm install --legacy-peer-deps || true
fi

if [ -n "$PINNED" ]; then
    git checkout -- package.json
fi
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha, vitest_pin=_ERA_VITEST_PIN),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}

PKG_MANAGER=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log('pnpm'); else console.log('npm'); }} catch(e) {{ console.log('npm'); }}")

{runner}""".format(repo=self.pr.repo, runner=_VITEST_RUNNER),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

PKG_MANAGER=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log('pnpm'); else console.log('npm'); }} catch(e) {{ console.log('npm'); }}")

set +e
{runner}""".format(repo=self.pr.repo, runner=_VITEST_RUNNER),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

PKG_MANAGER=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log('pnpm'); else console.log('npm'); }} catch(e) {{ console.log('npm'); }}")

set +e
{runner}""".format(repo=self.pr.repo, runner=_VITEST_RUNNER),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        global_env = f"{_NL}{self.global_env}{_NL}" if self.global_env else ""
        clear_env = f"{_NL}{self.clear_env}{_NL}" if self.clear_env else ""

        # The repo is cloned once in the shared base image, so this layer does
        # NOT clone (and needs no ``REPO_URL``): it only checks out this PR's
        # commit in the inherited working tree.
        #
        # This per-PR image chains to a base *Image* (not a string), so
        # DockerfileEnhancer returns this dockerfile verbatim and does NOT
        # auto-inject git-history hardening. We therefore check out
        # ``${BASE_COMMIT}`` and apply ``Image._HARDENING_BLOCK`` manually so
        # the fix / future commits cannot be read out of git history (reward
        # hacking). ``BASE_COMMIT`` is pinned to *this* PR's ``base.sha``, which
        # also prunes the full history inherited from the shared base.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
{global_env}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}{clear_env}"""


@Instance.register("lobehub", "lobehub_6452_to_71")
class LOBEHUB_6452_TO_71(Instance):
    """Instance for lobehub PRs 71-6452 (single-app era, vitest)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return LobeHubImageDefaultEarly(self.pr, self._config)

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

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # Vitest test-level pass: ✓ or ✔
            m = re.match(r"[✓✔]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                passed_tests.add(name)
                continue

            # Vitest test-level fail: × or ✕ or ✗
            m = re.match(r"[×✕✗]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                failed_tests.add(name)
                continue

            # Vitest file-level FAIL
            m = re.match(r"FAIL\s+(.+?)$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                failed_tests.add(name)
                continue

            # Vitest skipped: ↓ or ○
            m = re.match(r"[↓○]\s+(.+?)(?:\s+\[skipped\])?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                skipped_tests.add(name)
                continue

        # Dedup: worst wins
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


# ---------------------------------------------------------------------------
# Bundle routing by number_interval
#
# The *generated* dataset (``<org>__<repo>_dataset.jsonl``) carries a
# ``number_interval`` per row, so ``Instance.create()`` resolves the registry
# name to ``f"{org}/{number_interval}"`` rather than ``f"{org}/{repo}"``. A
# single-PR bundle stores that interval as the bare PR number, e.g. "567", so
# feeding the generated dataset back in looks up "lobehub/567". Without these
# entries every row is dropped with "Instance 'lobehub/567' is not registered."
#
# Registering each PR number of this era against the era class makes the raw
# dataset and the generated dataset interchangeable as ``--raw_dataset_files``.
# Multi-PR bundles dash-join their members instead; the late era registers its
# own such strings in ``lobehub_13716_to_6474._NUMBER_INTERVALS``.
#
# Registration only adds lookup names. It cannot change how an already-routed
# instance builds or runs.
# ---------------------------------------------------------------------------
for _number in range(71, 6453):
    Instance.register("lobehub", str(_number))(LOBEHUB_6452_TO_71)
