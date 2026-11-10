import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# colinhacks/zod PRs #6571 - #6587: the Nub era, after "Migrate development
# and CI to Nub" (#6562, 2026-09-07). Every base commit in this range has the
# same toolchain:
#   package.json  packageManager nub@0.8.3, type module, workspaces packages/*
#   pnpm-lock.yaml (read by `nub install`), no pnpm-workspace.yaml
#   .nvmrc 24; vitest ^4.1.5, typescript ~5.5.4, rollup ^4.60.2
# nub is a single self-contained binary that runs on the system node, so the
# base is Node 24 plus the nub 0.8.3 release archive (the version CI's
# nubjs/setup-nub and the devcontainer pin), checked by SHA-256.
#
# Test surface mirrors .github/workflows/test.yml: `nub run build` then
# `nub run test` (= `nub exec --node vitest run` over the packages/* projects
# plus the compile-mode project, with typecheck enabled). The build has to run in every stage, after the patches: the
# packages/treeshake tests bundle the built zod and refuse a build older than
# packages/zod/src.
_INTERVAL_NAME = "zod_6587_to_6571"
_LO, _HI = 6571, 6587

_NODE_IMAGE = "node:24.20.0-bookworm"
_PACKAGE_MANAGER = "nub@0.8.3"
_NUB_VERSION = "0.8.3"
_NUB_SHA256 = {
    "amd64": ("linux-x64", "249b064916db290c53de5bc1721a1dd78cf82e0f951f320782598b477cad7af9"),
    "arm64": ("linux-arm64", "060a31aeab3024f6db54fee74db2a1552759a8fee98e1b3dd6924977f9732bca"),
}

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

# vitest reporter, printed once at the end of the run, one sorted line per
# result:
#   MSWB_VITEST <PASS|FAIL|SKIP> [<project>] <file>[ (typecheck)] > <full name>
# No durations or counts, so names are identical across stages. Typecheck runs
# of a file are kept apart from its runtime runs. A file that fails to load,
# and a suite whose hook failed, are reported under their own names.
_VITEST_REPORTER_MJS = r"""import path from "node:path";

const STATUS = { passed: "PASS", failed: "FAIL", skipped: "SKIP", pending: "SKIP" };
const clean = s => String(s).replace(/\s+/g, " ").trim();

export default class MswbVitestReporter {
    onInit(vitest) {
        this.root = process.env.MSWB_REPO_ROOT || vitest.config.root;
    }

    onTestRunEnd(testModules, unhandledErrors) {
        const lines = [];
        for (const mod of testModules) {
            const file = path.relative(this.root, mod.moduleId).split(path.sep).join("/");
            const project = mod.project.name ? `[${clean(mod.project.name)}] ` : "";
            const kind = mod.task.meta && mod.task.meta.typecheck ? " (typecheck)" : "";
            const prefix = `${project}${file}${kind}`;
            const seen = new Map();
            let count = 0;
            for (const test of mod.children.allTests()) {
                count++;
                const base = `${prefix} > ${clean(test.fullName)}`;
                const n = (seen.get(base) || 0) + 1;
                seen.set(base, n);
                const status = STATUS[test.result().state] || "FAIL";
                lines.push(`MSWB_VITEST ${status} ${n === 1 ? base : `${base} #${n}`}`);
            }
            for (const suite of mod.children.allSuites()) {
                if (suite.errors().length) lines.push(`MSWB_VITEST FAIL ${prefix} > ${clean(suite.fullName)} <suite>`);
            }
            if (mod.errors().length) {
                lines.push(`MSWB_VITEST FAIL ${prefix}`);
            }
            else if (count === 0) {
                lines.push(`MSWB_VITEST ${mod.state() === "failed" ? "FAIL" : "PASS"} ${prefix}`);
            }
        }
        lines.sort();
        process.stdout.write(lines.join("\n") + "\n");
        process.stdout.write(`MSWB_VITEST_UNHANDLED ${(unhandledErrors || []).length}\n`);
    }
}
"""


def _run_tests_sh(repo: str) -> str:
    # Identical for run.sh / test-run.sh / fix-run.sh. The build is a setup
    # step: if it fails, vitest still runs (only the tests that need the build
    # fail) and the failure is returned at the end. MSWB_SETUP lines are for
    # diagnosis only; parse_log does not grade them.
    return f"""#!/bin/bash
set -eo pipefail

REPO=/home/{repo}
cd "$REPO"
export CI=true
export MSWB_REPO_ROOT="$REPO"
status=0

echo "=== MSWB build"
if nub run build; then
    echo "MSWB_SETUP PASS build"
else
    echo "MSWB_SETUP FAIL build"
    status=1
fi

echo "=== MSWB vitest"
# The treeshake tests bundle zod with rollup inside the default 5s timeout,
# which they miss intermittently under load; tests with their own timeout
# keep it.
if nub exec --node vitest run --testTimeout=60000 --hookTimeout=60000 \\
    --reporter=default --reporter=/home/mswb_vitest_reporter.mjs; then
    :
else
    status=1
fi

echo "=== MSWB done status=$status"
exit "$status"
"""


class Zod6587To6571ImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_LO}_to_{_HI}"

    def workdir(self) -> str:
        return f"base-{_LO}_to_{_HI}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # SHARED base for the whole era: keeps full history and never pins or
        # scrubs. The leading syntax directive opts out of DockerfileEnhancer,
        # so all proxy / CA / label infrastructure is written out by hand.
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

# FORCE_COLOR=0: packages/resolution/attw.test.ts snapshots attw's plain
# output; forced colour breaks the snapshot (CI sets no FORCE_COLOR).
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    PATH=/root/.nub/bin:${{PATH}} \\
    FORCE_COLOR=0 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    CI=true

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
{global_block}
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*

# nub {_NUB_VERSION}, pinned by package.json `packageManager` across the era.
# Same layout the upstream installer produces (~/.nub/bin with the nubx
# alias); the first run unpacks nub's embedded runtime into ~/.cache/nub.
RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) target="{_NUB_SHA256["amd64"][0]}"; sha="{_NUB_SHA256["amd64"][1]}" ;; \\
        arm64) target="{_NUB_SHA256["arm64"][0]}"; sha="{_NUB_SHA256["arm64"][1]}" ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/nub.tgz "https://github.com/nubjs/nub/releases/download/v{_NUB_VERSION}/nub-$target.tar.gz"; \\
    echo "$sha  /tmp/nub.tgz" | sha256sum -c -; \\
    mkdir -p /root/.nub; \\
    tar -xzf /tmp/nub.tgz -C /root/.nub; \\
    ln -sf nub /root/.nub/bin/nubx; \\
    rm /tmp/nub.tgz; \\
    nub --version

RUN git config --global --add safe.directory '*'

{code}
{clear_block}
CMD ["/bin/bash"]
"""


class Zod6587To6571ImageDefault(Image):
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
        return Zod6587To6571ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        prepare = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

# Section 1: PIN
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

# Section 2: PROVISION
export CI=true
# Registry downloads fail transiently; retry an install up to three times.
retry() {{
    local n
    for n in 1 2 3; do
        "$@" && return 0
        echo "prepare: attempt $n failed: $*" >&2
        sleep 15
    done
    return 1
}}
test "$(node -p "require('./package.json').packageManager")" = "{package_manager}"
retry nub install --frozen-lockfile
# Same build every stage runs before vitest; done once here so a broken
# toolchain fails the image build instead of the stages.
nub run build
# postbuild runs `biome format --write .`; restore any tracked file it touched
# so the stage patches apply to the pristine base tree. Build output is kept.
git checkout -- .

# Section 3: GATE
test -x node_modules/.bin/vitest
nub exec --node vitest --version
test -f packages/zod/index.js
nub exec --node vitest list --filesOnly > /home/vitest-files.txt
test -s /home/vitest-files.txt
echo "DEPS_OK $(node --version) nub $(nub --version)"
""".format(repo=repo, sha=sha, package_manager=_PACKAGE_MANAGER)

        run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
bash /home/mswb_run_tests.sh
""".format(repo=repo)

        test_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
bash /home/mswb_run_tests.sh
""".format(repo=repo)

        fix_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/mswb_run_tests.sh
""".format(repo=repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_vitest_reporter.mjs", _VITEST_REPORTER_MJS),
            File(".", "mswb_run_tests.sh", _run_tests_sh(repo)),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        # dependency() returns an Image, so DockerfileEnhancer never touches
        # this file: the pin and the history scrub are written out here.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "\n".join(
            f"COPY {file.name} /home/" for file in self.files()
        )

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}
{global_block}
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach {sha}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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
{clear_block}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# MSWB_SETUP / MSWB_VITEST_UNHANDLED lines are diagnostics, not tests, and are
# deliberately not matched.
_RESULT_RE = re.compile(r"^MSWB_VITEST (PASS|FAIL|SKIP) (\S.*?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}
    for line in _ANSI_RE.sub("", test_log or "").replace("\r", "").splitlines():
        m = _RESULT_RE.match(line.strip())
        if not m:
            continue
        status, name = m.groups()
        buckets[status].add(name)

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    passed_tests -= skipped_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("colinhacks", _INTERVAL_NAME)
class ZOD_6587_TO_6571(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Zod6587To6571ImageDefault(self.pr, self._config)

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
        return _parse_mswb_log(test_log)


# ---------------------------------------------------------------------------
# Routing
#
# The JSONL carries no number_interval, so Instance.create() looks up the bare
# "colinhacks/zod" key. Every zod era module takes that key over and sends its
# own range to itself; any other PR goes to whatever held the key before it
# was imported. Importing zod.py first guarantees the legacy config is at the
# end of that chain, so the routing is the same whatever order
# colinhacks/__init__.py imports the era modules in, and no era module imports
# another.
# ---------------------------------------------------------------------------

import multi_swe_bench.harness.repos.typescript.colinhacks.zod  # noqa: E402,F401

_FALLBACK = Instance._registry["colinhacks/zod"]


@Instance.register("colinhacks", "zod")
class ZOD_6587_TO_6571_DISPATCH(Instance):
    """Router only: __new__ returns the era-appropriate instance."""

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return ZOD_6587_TO_6571(pr, config, *args, **kwargs)
        return _FALLBACK(pr, config, *args, **kwargs)
