"""grafana/grafana registry config for multi-swe-bench.

Grafana is a polyglot monorepo (Go backend + TypeScript/React frontend), but
every record this config serves is frontend-only: all five test patches touch
.ts/.tsx under public/app, so the run scripts drive jest and nothing else.

- Base image: debian:bookworm. Deliberately NOT a language image -- Node is
  installed per record by nvm, driven by each commit's own .nvmrc (see
  GrafanaImageBase.dependency and _FALLBACK_NODE). The base only supplies git,
  curl and a trust store.
- Package manager: yarn, but which yarn changes by era -- classic for the 2020/21
  records, berry (via corepack, `packageManager` field) from #48737 on. prepare.sh
  branches on that field rather than on a PR-number cutoff, so it cannot drift as
  records are added.
- Single config covers all eras: nvm + the packageManager check absorb the
  version drift at build time instead of hard-coding ranges.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



# The harness hardening block, minus its two prune steps. Derived from
# Image._HARDENING_BLOCK rather than copied, so the detach, the ref deletion,
# the reflog expiry, the config locks and all FOUR integrity asserts stay in
# lockstep with upstream.
#
# The prune is dropped because this base image is SHARED by every record (one
# `base` tag, built once). `git gc --prune=now` deletes every object unreachable
# from HEAD, so a shared base that pruned would keep only the history of
# whichever record happened to build it and the others could no longer reach
# their own base.sha. Dropping it costs nothing the asserts check: ref deletion
# still happens, so `git rev-list --all` collapses to HEAD's history and the
# fourth assert holds, while `gc.auto 0` stops any later git command from
# pruning opportunistically. The other records' objects survive unreferenced,
# reachable only by exact sha -- which is what lets each PR layer's prepare.sh
# check out its own base commit.
_NO_PRUNE_HARDENING = chr(10).join(
    line
    for line in Image._HARDENING_BLOCK.split(chr(10))
    if "git gc --prune=now" not in line and "git repack -a -d -l" not in line
)

# Node for the records that predate .nvmrc. Measured at their base commits:
# #28916 declares engines.node ">= 12" and #34774 ">= 14"; neither ships a
# .nvmrc, so `nvm install` has nothing to read and would fall through to the
# current LTS (Node 22+), which cannot build a 2020/2021 Grafana frontend
# (node-sass / webpack 4 of that era). 14 is the newest release that satisfies
# both engines fields. Records from #48737 on DO ship .nvmrc (v16.14.0, then
# v18.12.0) and are driven by it instead.
_FALLBACK_NODE = "14"


# jest is invoked directly rather than through `yarn test:ci`, because that
# script is not a constant across this dataset: #28916 has no test:ci at all
# (its `test` is `grunt test`), and the later records wrap jest in betterer /
# i18n:compile steps that differ by era. A per-record fallback chain would let
# the graded command change between stages, which is exactly what makes an f2p
# comparison meaningless. This one command is identical in run/test-run/fix-run.
#
# --verbose is required, not cosmetic: without it jest prints only `PASS <file>`
# and parse_log can never see individual tests. --runInBand keeps the output
# ordered so the `✓`/`✕` lines stay under the right file header.
_TEST_CMD = "yarn jest --ci --verbose --runInBand --watchAll=false {scope} 2>&1"


def _test_scope(pr: PullRequest) -> str:
    """Jest paths to run: the directories the test patch touches.

    Grafana's full Jest suite is thousands of files and would be run three times
    per record. Every test file in this dataset lives under public/app/..., and
    scoping to its directory keeps the f2p signal intact while still giving real
    p2p coverage from the neighbouring tests. Falls back to the whole suite if
    the patch yields nothing parseable, so the command is never empty.
    """
    dirs = []
    for line in (pr.test_patch or "").split(chr(10)):
        if line.startswith("diff --git a/"):
            path = line.split(" a/", 1)[1].split(" b/", 1)[0]
            d = path.rsplit("/", 1)[0] if "/" in path else ""
            # __snapshots__ lives beside the test file, not on its own
            if d.endswith("/__snapshots__"):
                d = d[: -len("/__snapshots__")]
            if d and d not in dirs:
                dirs.append(d)
    return " ".join(dirs)


class GrafanaImageBase(Image):
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
        # Plain Debian, not a language image: Node never comes from the base.
        # nvm is installed below and prepare.sh drives it from each commit's own
        # .nvmrc (v16.14.0 at #48737, v18.12.0 at #63394/#64237) or the pinned
        # fallback where the commit predates .nvmrc. The base only has to supply
        # git, curl and a trust store.
        #
        # This used to be `golang:1.24-bookworm`, from when the run scripts also
        # ran `go test ./pkg/...`. Those are gone -- every record in this dataset
        # touches only .ts/.tsx under public/app -- so the Go toolchain was ~1 GB
        # of dead weight in every image.
        return "debian:bookworm"

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
        org = self.pr.org
        repo = self.pr.repo

        # The `# syntax` directive opts this file out of DockerfileEnhancer
        # (image.py:317-318), so the proxy ARGs, the TLS-trust ENV and the
        # CA-cert symlink farm are written out here by hand. The farm has to
        # precede the first network RUN -- the nvm installer on the curl line
        # below -- or that download has no trust store to verify against.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        curl git jq ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/HEAD/install.sh | bash

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_NO_PRUNE_HARDENING}

WORKDIR /home/

CMD ["/bin/bash"]
"""



class GrafanaImageDefault(Image):
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
        return GrafanaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -eo pipefail

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Node: .nvmrc when the commit ships one, otherwise the pinned fallback. The
# fallback is not a guess -- see _FALLBACK_NODE for the engines fields it was
# derived from. `nvm install` with no argument reads .nvmrc and fails loudly
# when there is none, which is why the branch is explicit rather than a
# fallthrough.
if [ -f .nvmrc ]; then
    nvm install
    nvm use
else
    nvm install {fallback_node}
    nvm use {fallback_node}
fi
node --version

# Yarn: berry (>= 2) is selected by the packageManager field and must come from
# corepack -- a globally installed yarn 1 CLI cannot drive a berry workspace,
# and berry rejects --frozen-lockfile (its spelling is --immutable). Classic
# repos have no packageManager field and take the yarn 1 path.
#
# The fallback needs YARN_CHECKSUM_BEHAVIOR=update, not just a bare retry.
# Grafana's berry-era lockfiles pin three git-hosted deps -- grafana/icons,
# thoward/rst2html and torkelo/drop -- by the checksum of a GitHub-generated
# tarball. Those archives are not byte-for-byte reproducible (gzip stamps the
# mtime and the compressor version), so the recorded checksums no longer match
# what GitHub serves today and berry's default checksumBehavior: throw aborts
# the install with YN0018. A plain `yarn install` retry inherits that same
# default and dies identically, which is what failed pr-48737. Scoped to the
# berry branch only: the yarn 1 path below has no checksumBehavior and is
# unaffected.
if grep -q '"packageManager"' package.json 2>/dev/null; then
    corepack enable
    corepack prepare --activate 2>/dev/null || true

    # cypress and @parcel/watcher are opted out of berry's build step because
    # neither can be built for linux/arm64 under QEMU: cypress 9.x publishes no
    # linux-arm64 binary at all, and @parcel/watcher's node-gyp compile fails in
    # emulation. Left alone they abort the whole install with YN0009, which is
    # what killed pr-64237. Neither is needed by the jest suites these images
    # run -- cypress is e2e-only and jest falls back to polling without the
    # native watcher -- so this mirrors what grafana itself now does upstream
    # (enableScripts: false plus a dependenciesMeta allowlist).
    export CYPRESS_INSTALL_BINARY=0
    node -e '
      const fs = require("fs");
      const p = JSON.parse(fs.readFileSync("package.json", "utf8"));
      const meta = p.dependenciesMeta || Object.create(null);
      const off = Object.create(null);
      off.built = false;
      meta["cypress"] = off;
      meta["@parcel/watcher"] = off;
      p.dependenciesMeta = meta;
      fs.writeFileSync("package.json", JSON.stringify(p, null, 2));
    '

    # --immutable rejects the package.json edit above; the existing fallback
    # then runs a plain install, which accepts it.
    yarn install --immutable || YARN_CHECKSUM_BEHAVIOR=update yarn install
else
    npm list -g yarn >/dev/null 2>&1 || npm install -g yarn@1
    yarn install --frozen-lockfile || yarn install
fi

""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                    fallback_node=_FALLBACK_NODE,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
nvm use >/dev/null 2>&1 || nvm use {fallback_node} >/dev/null 2>&1 || true

{test_cmd}

""".format(
                    repo=self.pr.repo,
                    fallback_node=_FALLBACK_NODE,
                    test_cmd=_TEST_CMD.format(scope=_test_scope(self.pr)),
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
# Test scaffolding under __mocks__/ has to come across with the tests, even when
# the upstream patch split filed it under fix.patch. #64237 is the case that
# forced this: both files test.patch touches import
# public/app/features/explore/__mocks__/makeLogs.ts, but that file is created by
# fix.patch, so at this stage the module does not resolve, jest fails BOTH suites
# at import time, and 32 tests vanish from the run instead of reporting. The two
# new tests then never emit a name to fail on, so they score n2p instead of f2p
# and the fail-to-pass signal is lost.
#
# --include is scoped to __mocks__/ on purpose: that is jest's convention for
# fixtures and fakes, never for the code under test. makeLogs.ts only fabricates
# LogRowModel[] via sortLogRows, which already exists at the base commit. Verified
# on pr-64237 that the scoped apply brings in the mock and NOTHING else -- none of
# LiveLogs.tsx, LogsContainer.tsx, query.ts, state/utils.ts, useLiveTailControls.ts
# or types/explore.ts leak in, so the fix itself stays out of this stage.
#
# `|| true` because most records have no __mocks__ hunk at all and git apply exits
# non-zero when the include filter matches nothing.
git apply --whitespace=nowarn --include='**/__mocks__/**' /home/fix.patch 2>/dev/null || true
nvm use >/dev/null 2>&1 || nvm use {fallback_node} >/dev/null 2>&1 || true

{test_cmd}

""".format(
                    repo=self.pr.repo,
                    fallback_node=_FALLBACK_NODE,
                    test_cmd=_TEST_CMD.format(scope=_test_scope(self.pr)),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
nvm use >/dev/null 2>&1 || nvm use {fallback_node} >/dev/null 2>&1 || true

{test_cmd}

""".format(
                    repo=self.pr.repo,
                    fallback_node=_FALLBACK_NODE,
                    test_cmd=_TEST_CMD.format(scope=_test_scope(self.pr)),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Deliberately thin: FROM, the patches and run-scripts, and prepare.sh.
        # Nothing else.
        #
        # No checkout here -- prepare.sh already does it: cd /home/<repo> ->
        # git reset --hard -> check_git_changes -> git checkout <base.sha> ->
        # check_git_changes. Repeating it as a Dockerfile RUN would duplicate work.
        #
        # No history scrub here either, and that is a trade-off rather than an
        # oversight. Image._HARDENING_BLOCK prunes everything unreachable from a
        # single commit; running it in the shared base would freeze that base on
        # whichever record built it first, and running it here is what made this
        # layer thick. Keeping ONE base image was chosen over scrubbing, so these
        # images ship the repo's full history. The base still drops the remote and
        # disables auto-gc.
        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("grafana", "grafana")
class Grafana(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrafanaImageDefault(self.pr, self._config)

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
        """Parse test output from both Go tests and Jest (TypeScript) tests.

        Go test output format:
            --- PASS: TestName (0.00s)
            --- FAIL: TestName (0.01s)
            --- SKIP: TestName (0.00s)

        Jest output format (verbose):
            PASS packages/grafana-data/src/dataframe/ArrayDataFrame.test.ts
              Suite Name
                ✓ test description (2 ms)
                ✕ failing test (1 ms)
                ○ skipped test
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # Go test patterns
        re_go_pass = re.compile(r"^--- PASS: (\S+)")
        re_go_fail = re.compile(r"^--- FAIL: (\S+)")
        re_go_skip = re.compile(r"^--- SKIP: (\S+)")

        # Jest suite-level patterns (PASS/FAIL <file>)
        re_jest_suite = re.compile(r"^(PASS|FAIL)\s+(\S+)")

        # Jest individual test patterns (✓ / ✕ / ○)
        re_jest_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_fail = re.compile(r"^\s*[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_skip = re.compile(r"^\s*[○⊘]\s+(.+)")

        current_suite = ""
        has_individual_tests = False

        def get_base_name(name: str) -> str:
            """Strip subtest suffix for Go tests (TestFoo/SubTest -> TestFoo)."""
            idx = name.rfind("/")
            return name[:idx] if idx != -1 else name

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            # --- Go test output ---
            m = re_go_pass.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_go_fail.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_go_skip.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

            # --- Jest suite-level (PASS/FAIL <file>) ---
            m = re_jest_suite.match(line)
            if m:
                current_suite = m.group(2)
                # Only add suite-level if no individual tests found yet
                if not has_individual_tests:
                    if m.group(1) == "PASS":
                        failed_tests.discard(current_suite)
                        skipped_tests.discard(current_suite)
                        passed_tests.add(current_suite)
                    else:
                        failed_tests.discard(current_suite)
                        passed_tests.discard(current_suite)
                        failed_tests.add(current_suite)
                continue

            # --- Jest individual test lines ---
            m = re_jest_pass.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                # Remove suite-level entry now that we have individual tests
                passed_tests.discard(current_suite)
                failed_tests.discard(current_suite)
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_jest_fail.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                passed_tests.discard(current_suite)
                failed_tests.discard(current_suite)
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_jest_skip.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
