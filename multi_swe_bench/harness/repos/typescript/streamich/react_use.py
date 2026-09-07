import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ReactUseImageBase(Image):
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
        return "node:18"

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

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # SHARED base (tag "base", reused by all five PRs). The `# syntax` directive
        # makes DockerfileEnhancer.enhance() return this Dockerfile unchanged;
        # otherwise it rewrites the clone to `git checkout ${BASE_COMMIT}` +
        # hardening + gc-prune, pinning the shared base to ONE commit and pruning
        # every object unreachable from it - which breaks the other PRs'
        # `git checkout <base.sha>` in prepare.sh with "reference is not a tree".
        #
        # Opting out costs the enhancer's infrastructure block, so this file has
        # to carry it: build args, proxy/TLS environment, OCI labels, CA-cert
        # symlinks. It is not redundant duplication - nothing else supplies it.
        #
        # Checking out a commit and scrubbing history is the PR layer's job (see
        # ReactUseImageDefault.dockerfile): only that layer knows which single
        # commit it is allowed to keep.
        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
                "# Declared so the harness --build-arg has somewhere to land, and\n"
                "# then deliberately never referenced: this tag is shared by every\n"
                "# PR, so pinning it to one commit would strand the others.\n"
                "ARG BASE_COMMIT"
            ),
            (
                'ARG http_proxy=""\n'
                'ARG https_proxy=""\n'
                'ARG HTTP_PROXY=""\n'
                'ARG HTTPS_PROXY=""\n'
                'ARG no_proxy="localhost,127.0.0.1,::1"\n'
                'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
                'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
            ),
            (
                "ENV DEBIAN_FRONTEND=noninteractive \\\n"
                "    LANG=C.UTF-8 \\\n"
                "    TZ=UTC \\\n"
                "    http_proxy=${http_proxy} \\\n"
                "    https_proxy=${https_proxy} \\\n"
                "    HTTP_PROXY=${HTTP_PROXY} \\\n"
                "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
                "    no_proxy=${no_proxy} \\\n"
                "    NO_PROXY=${NO_PROXY} \\\n"
                "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
                "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
                "    CURL_CA_BUNDLE=${CA_CERT_PATH}"
            ),
            (
                f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
                f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
                f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
                f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
            ),
            # Before the first RUN that touches the network. node:18 is Debian,
            # so only /etc/ssl/certs/ca-certificates.crt exists; tools that look
            # for the RedHat or BSD-style paths (and any MITM proxy CA dropped in
            # later) would otherwise fall back to no trust store at all.
            (
                "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
            ),
            self.global_env,
            (
                "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
                "    git \\\n"
                "    ca-certificates \\\n"
                "    && rm -rf /var/lib/apt/lists/*"
            ),
            # The PR layer runs git as root against a tree cloned by root here.
            # That is fine today, but a single ownership change (bind mount, a
            # non-root USER, a rebuild on a different backend) turns every later
            # git call into "detected dubious ownership" and takes the checkout,
            # the scrub and its assertions down with it.
            "RUN git config --global --add safe.directory '*'",
            "WORKDIR /home/",
            code,
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]

        return "\n\n".join(s for s in sections if s) + "\n"


class ReactUseImageDefault(Image):
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
        return ReactUseImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_files(self) -> str:
        """Empty on purpose: every stage runs the whole suite.

        Was `get_modified_files(self.pr.test_patch)`, which skips ADDED files —
        so the four PRs that add a test file ran everything while pr-948, which
        edits two existing files, was scoped to those two. Do not restore it.
        F2P only comes from test-patch files so a filter gains nothing, while
        P2P can only be collected from the tests a filter would have excluded.
        """
        return ""

    def files(self) -> list[File]:
        test_files = self._test_files()
        test_files = f" {test_files}" if test_files else ""
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Every dependency is installed HERE, at image-build time, so all three graded
# stages execute against one identical node_modules. An installer inside a
# graded script would make the baseline and the patched runs differ by more
# than the patch, which is exactly what the f2p comparison assumes away.
set -e

cd /home/[[REPO]]

# The base image is shared across every PR: it clones the full history and pins
# nothing. This is the layer that puts the tree on THIS PR's commit.
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

yarn install --frozen-lockfile --network-timeout 600000

# Two of these fix patches change the dependency set and carry the matching
# package.json + yarn.lock hunks: pr-878 adds js-cookie, which its test imports,
# and pr-948 swaps tslint for eslint. Stage only those two files, install, then
# put them back. node_modules keeps the added packages, so fix-run.sh finds them
# already present and no graded script ever runs an installer.
#
# The guard greps for the diff header instead of `git apply --check --include`,
# because that check EXITS 0 on a patch touching neither file (verified, git
# 2.54) and would run a second pointless install for the other three PRs.
if grep -qE '^diff --git a/(package\\.json|yarn\\.lock) ' /home/fix.patch; then
    git apply --whitespace=nowarn --include=package.json --include=yarn.lock /home/fix.patch
    yarn install --frozen-lockfile --network-timeout 600000
    git checkout -- package.json yarn.lock
fi

# yarn can exit 0 having left the one binary the graded stages invoke unusable.
# That image builds green, then every stage collects 0 tests - which Report.check()
# rejects as an empty fix stage, indistinguishable from "the fix does not work".
# Fail the build here instead, where the cause is still legible.
DEPS_OK=1
test -d node_modules || DEPS_OK=0
test -x node_modules/.bin/jest || DEPS_OK=0
node_modules/.bin/jest --version > /dev/null 2>&1 || DEPS_OK=0
node -e "require.resolve('typescript')" > /dev/null 2>&1 || DEPS_OK=0
if [ "$DEPS_OK" != "1" ]; then
    echo "prepare: DEPS_OK=0 - install did not produce a usable jest/typescript"
    exit 1
fi
echo "prepare: DEPS_OK=1"
""".replace("[[REPO]]", self.pr.repo).replace("[[SHA]]", self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
npx jest --ci --runInBand --forceExit --verbose --no-coverage{test_files}
""".format(repo=self.pr.repo, test_files=test_files),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch
npx jest --ci --runInBand --forceExit --verbose --no-coverage{test_files}
""".format(repo=self.pr.repo, test_files=test_files),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --exclude yarn.lock --whitespace=nowarn /home/test.patch /home/fix.patch
npx jest --ci --runInBand --forceExit --verbose --no-coverage{test_files}
""".format(repo=self.pr.repo, test_files=test_files),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files()).rstrip(
            "\n"
        )

        # The base tag is shared, so it still holds the full upstream history -
        # including the commit that fixes this very PR, one `git log origin/master`
        # away from any model running inside the container. Scrubbing it is this
        # layer's responsibility because this is the first layer that knows which
        # commit is the only one allowed to survive.
        #
        # After prepare.sh, not before: prepare.sh needs origin and the ref graph
        # to check out and verify the commit. The SHA is inlined rather than
        # read from ${BASE_COMMIT} because build_dataset only passes that build
        # arg to images whose dependency() is a string, i.e. base images only.
        #
        # Both reflog expirations are required. --expire=now drops reachable
        # entries; unreachable ones survive it and keep the fix commit alive
        # through gc, so dropping either variant leaves the answer in the image.
        history_scrub = f"""RUN set -eux; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

        submodule_scrub = """RUN test -f .gitmodules && git submodule foreach --recursive '\\
        git checkout --detach HEAD 2>/dev/null || true; \\
        git remote remove origin 2>/dev/null || true; \\
        git for-each-ref --format=%(refname) refs/heads refs/remotes refs/tags refs/replace \\
            | xargs -r -n1 git update-ref -d; \\
        git reflog expire --expire=now --all; \\
        git reflog expire --expire-unreachable=now --all; \\
        git gc --prune=now --aggressive; \\
        rm -f .git/objects/info/alternates; \\
    ' || true"""

        sections = [
            f"FROM {name}:{tag}",
            self.global_env,
            copy_commands,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            history_scrub,
            submodule_scrub,
            self.clear_env,
        ]

        return "\n\n".join(s for s in sections if s) + "\n"


@Instance.register("streamich", "react-use")
class ReactUse(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ReactUseImageDefault(self.pr, self._config)

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
        """Record Jest output at BOTH granularities: the suite path, and each
        assertion as `<path>::<name>`.

        Both are required. A suite whose new test file cannot compile yet prints
        no ✓/✕ lines at all — pr-878's `tests/useCookie.test.tsx` dies on
        `TS2305: Module '"../src"' has no exported member 'useCookie'` — so its
        assertions read NONE→NONE→PASS and report.py buckets them as n2p. Only
        the suite entry carries NONE→FAIL→PASS, which is the f2p signal. Drop
        suite entries and all four valid PRs silently downgrade to n2p.

        Assertions are prefixed with their suite and numbered on repeat because
        bare Jest names collide inside a single file here (`useDefault.test.ts`
        reuses three names four times each, `useAsync.test.tsx` reuses three
        twice); without both, 23 of 462 assertions vanish and a name that passes
        under one `describe` while failing under another would put the same key
        in two sets, tripping TestResult's disjointness check.

        Timings are stripped: they differ run to run, and a name carrying one is
        not comparable across the three stages.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        suite_passed_re = re.compile(r"^PASS\s+(\S+)(?:\s+\([\d.]+\s*m?s\))?$")
        suite_failed_re = re.compile(r"^FAIL\s+(\S+)(?:\s+\([\d.]+\s*m?s\))?$")
        test_passed_re = re.compile(r"^\s+[✓✔]\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$")
        test_failed_re = re.compile(r"^\s+[✕✗×]\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$")
        test_skipped_re = re.compile(
            r"^\s+○\s+skipped\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$"
        )

        current_suite = ""
        occurrences: dict[str, int] = {}

        def assertion_key(suite: str, name: str) -> str:
            base = f"{suite}::{name}"
            occurrences[base] = occurrences.get(base, 0) + 1
            nth = occurrences[base]
            return base if nth == 1 else f"{base} #{nth}"

        for line in clean_log.splitlines():
            line = line.rstrip()

            m = suite_passed_re.match(line)
            if m:
                current_suite = m.group(1)
                passed_tests.add(current_suite)
                continue

            m = suite_failed_re.match(line)
            if m:
                current_suite = m.group(1)
                failed_tests.add(current_suite)
                continue

            # Assertions are indented under the suite header that owns them, and
            # --runInBand keeps those blocks strictly sequential, so the last
            # header seen is the owning file. Leading whitespace is the only
            # thing separating an assertion from a header: do not strip it.
            if not current_suite:
                continue

            m = test_passed_re.match(line)
            if m:
                passed_tests.add(assertion_key(current_suite, m.group(1)))
                continue

            m = test_failed_re.match(line)
            if m:
                failed_tests.add(assertion_key(current_suite, m.group(1)))
                continue

            m = test_skipped_re.match(line)
            if m:
                skipped_tests.add(assertion_key(current_suite, m.group(1)))

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
