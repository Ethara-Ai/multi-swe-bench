import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# =============================================================================
# ManifoldScholar/manifold -- interval 1074_to_783 (Node 6.10.3, client jest suite)
#
# Two-stage layout:
#   * MANIFOLD_1074_TO_783_ImageBase   -> mswebench/manifoldscholar_m_manifold:base-1074_to_783
#     ubuntu:22.04 + official Node 6.10.3 tarball (picked by TARGETARCH, so
#     amd64 and arm64 both work) + yarn 1.22, full clone, NO checkout/hardening
#     (shared by every PR in this interval).
#   * MANIFOLD_1074_TO_783_ImageDefault -> mswebench/manifoldscholar_m_manifold:pr-<N>
#     checks out base.sha (detached), installs client deps from yarn.lock
#     (--frozen-lockfile), then applies
#     the anti-reward-hack hardening.
#
# Only the client/ jest suite is run: the dataset's test patches are jest
# specs/snapshots under client/, and the parsers below are jest-only.
# node-sass has no prebuilt binary for arm64/old Node, so deps are installed
# with --ignore-scripts (jest maps style imports, the binding is never loaded).
# =============================================================================

NODE_VERSION = "6.10.3"
YARN_VERSION = "1.22.22"

# Some PRs were branched long before the dataset's base.sha; their test/fix
# patches only apply cleanly at the PR's merge-base with base.sha, so build
# those PRs from the merge-base instead.
BASE_SHA_OVERRIDES = {
    819: "c3669659bc0c6c90aa23e1f6a1b95441fb246515",
}


class MANIFOLD_1074_TO_783_ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-1074_to_783"

    def workdir(self) -> str:
        return "base-1074_to_783"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}
WORKDIR /home/
ENV LC_ALL=C.UTF-8 \\
    CI=true \\
    NODE_OPTIONS=--max-old-space-size=4096

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    python3 \\
    xz-utils \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) node_arch=x64 ;; \\
        arm64) node_arch=arm64 ;; \\
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.xz" \\
        | tar -xJ -C /usr/local --strip-components=1 --no-same-owner; \\
    mkdir -p /opt/yarn; \\
    curl -fsSL "https://github.com/yarnpkg/yarn/releases/download/v{YARN_VERSION}/yarn-v{YARN_VERSION}.tar.gz" \\
        | tar -xz -C /opt/yarn --strip-components=1 --no-same-owner; \\
    ln -sf /opt/yarn/bin/yarn /usr/local/bin/yarn; \\
    ln -sf /opt/yarn/bin/yarnpkg /usr/local/bin/yarnpkg; \\
    node --version; yarn --version

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class MANIFOLD_1074_TO_783_ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def base_sha(self) -> str:
        return BASE_SHA_OVERRIDES.get(self.pr.number, self.pr.base.sha)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return MANIFOLD_1074_TO_783_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_cmd = (
            "cd /home/{repo}/client\n"
            "export CI=true\n"
            "export YARN_IGNORE_ENGINES=true\n"
            "# fix patches may add client deps; install only when package.json/yarn.lock changed\n"
            "if ! git diff --quiet HEAD -- package.json yarn.lock; then\n"
            "  yarn install --ignore-scripts --ignore-engines --network-timeout 600000 --non-interactive\n"
            "fi\n"
            "yarn test --maxWorkers=4\n"
        ).format(repo=repo)
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
                f"""#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.base_sha}
bash /home/check_git_changes.sh

cd /home/{repo}/client
yarn install --frozen-lockfile --ignore-scripts --ignore-engines --network-timeout 600000 --non-interactive

# hard gate: jest must resolve from the installed client deps
node -e "require.resolve('jest/package.json')"
./node_modules/.bin/jest --version
""",
            ),
            File(
                ".",
                "test.sh",
                f"""#!/bin/bash
set -eo pipefail

{test_cmd}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{repo}
bash /home/test.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{repo}
apply_patch() {{
    # Only client/ (jest) is exercised; skip Ruby api/ hunks and binary hunks
    # that were exported without a full index line (git cannot apply them).
    local excludes=(--exclude='api/*' --exclude='Gemfile' --exclude='Gemfile.lock')
    while IFS= read -r path; do
        excludes+=("--exclude=$path")
    done < <(sed -n -E 's#^Binary files (a/(.*)|/dev/null) and (b/(.*)|/dev/null) differ$#\\2\\4#p' "$1")
    git apply --whitespace=nowarn "${{excludes[@]}}" "$1"
}}
apply_patch /home/test.patch
bash /home/test.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{repo}
apply_patch() {{
    # Only client/ (jest) is exercised; skip Ruby api/ hunks and binary hunks
    # that were exported without a full index line (git cannot apply them).
    local excludes=(--exclude='api/*' --exclude='Gemfile' --exclude='Gemfile.lock')
    while IFS= read -r path; do
        excludes+=("--exclude=$path")
    done < <(sed -n -E 's#^Binary files (a/(.*)|/dev/null) and (b/(.*)|/dev/null) differ$#\\2\\4#p' "$1")
    git apply --whitespace=nowarn "${{excludes[@]}}" "$1"
}}
apply_patch /home/test.patch
apply_patch /home/fix.patch
bash /home/test.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        name = dep.image_name()
        tag = dep.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.base_sha}"
ENV BASE_COMMIT={self.base_sha}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("ManifoldScholar", "manifold_1074_to_783")
class MANIFOLD_1074_TO_783(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MANIFOLD_1074_TO_783_ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        # Jest --verbose (CI=true, non-interactive) output, e.g.
        #   PASS src/store/reducers/__tests__/notifications-test.js
        #     store/reducers/notifications          <- describe (indent 2, nested +2)
        #       \u2713 should return the initial state (2ms)
        #       \u2715 renders correctly (16ms)
        #       \u25cb skipped test
        #   FAIL src/.../x-test.js  +  "\u25cf Test suite failed to run"
        # Test id = "<suite file> > <describe chain> > <test title>" so ids are
        # unique per file and identical across run / test-run / fix-run.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        suite_re = re.compile(r"^\s*(PASS|FAIL)\s+(\S+\.(?:js|jsx|ts|tsx))(?:\s+\(.*\))?\s*$")
        test_re = re.compile(r"^(\s+)(\u2713|\u2714|\u2715|\u2716|\u00d7|\u25cb)\s+(.+?)\s*$")
        describe_re = re.compile(r"^( {2,})(\S.*?)\s*$")
        duration_re = re.compile(r"\s+\(\d+(?:\.\d+)?\s*m?s\)$")
        noise_re = re.compile(
            r"^(\u25cf|console\.|at |Test Suites:|Tests:|Snapshots:|Time:|Ran all|expect|Expected|Received|[-+@<>{}\[\]])"
        )

        suite = None
        stack: list[tuple[int, str]] = []
        load_failed = False
        # the repo has a few identical describe+title pairs in one file; jest
        # runs them in source order, so an occurrence suffix is stable across stages
        seen: dict[str, int] = {}

        for raw in log.splitlines():
            line = raw.rstrip()
            m = suite_re.match(line)
            if m:
                suite = m.group(2)
                stack = []
                load_failed = False
                seen = {}
                continue
            if suite is None:
                continue
            if line.strip().startswith("Test Suites:"):
                suite = None
                continue
            if "\u25cf Test suite failed to run" in line:
                load_failed = True
                failed_tests.add(suite)
                continue
            m = test_re.match(line)
            if m:
                indent = len(m.group(1))
                mark = m.group(2)
                title = duration_re.sub("", m.group(3))
                if mark == "\u25cb" and re.match(r"^skipped \d+ tests?$", title):
                    continue
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                name = " > ".join([suite] + [s for _, s in stack] + [title])
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name = f"{name} #{seen[name]}"
                if mark in ("\u2713", "\u2714"):
                    passed_tests.add(name)
                elif mark == "\u25cb":
                    skipped_tests.add(name)
                else:
                    failed_tests.add(name)
                continue
            m = describe_re.match(line)
            if m and not load_failed and not noise_re.match(m.group(2)):
                indent = len(m.group(1))
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, m.group(2)))

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
