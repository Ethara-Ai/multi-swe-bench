import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _touched_packages(pr: PullRequest) -> list[str]:
    packages = set()
    patches = f"{pr.test_patch}\n{pr.fix_patch}"
    for path in re.findall(r"^diff --git a/(.+?) b/", patches, re.M):
        match = re.match(r"^(packages|codemods|eslint)/([^/]+)/", path)
        if match:
            packages.add(f"{match.group(1)}/{match.group(2)}")
    return sorted(packages)


def _needs_babel8(pr: PullRequest) -> bool:
    def added(patch: str) -> list[str]:
        return [
            line
            for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]

    return any(
        re.search(r"itBabel8|describeBabel8", line) for line in added(pr.test_patch)
    ) and any("process.env.BABEL_8_BREAKING" in line for line in added(pr.fix_patch))


def _needs_esm_mode(pr: PullRequest) -> bool:
    return _needs_babel8(pr) or bool(
        re.search(r"describeESM|itESM|itGteESM|esm-cjs-integration", pr.test_patch)
    )


class ImageBase(Image):
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
        return "node:22-bookworm-slim"

    def image_tag(self) -> str:
        return "base-16820-to-17576"

    def workdir(self) -> str:
        return "base-16820-to-17576"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    g++ \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{self.pr.repo}

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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        build_env = "BABEL_8_BREAKING=true " if _needs_babel8(self.pr) else ""

        esm_switch = (
            "USE_ESM=true make use-esm >/dev/null 2>&1 || true\n"
            if _needs_esm_mode(self.pr)
            else ""
        )

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
                "run-tests.sh",
                """#!/bin/bash
set -uo pipefail

cd /home/{pr.repo}
export CI=true
export BABEL_ENV=test

# Turn require(esm) back off for the graded runs only. Node 22 enables it by
# default, which makes babel-core's "'.mjs files' called synchronously" tests
# resolve where they assert a rejection -- a PASS->FAIL that invalidates PR
# 16820, whose subject is exactly that feature. The flag cannot live in the
# image ENV: with it set, gulp's jest-worker threads die during `make build`
# ("Call retries were exceeded") for every PR from 17443 on.
export NODE_OPTIONS="--no-experimental-require-module --max-old-space-size=4096"
{babel8_env}{esm_setup}
JEST_BIN="$(yarn bin jest)"

for pkg in {packages}; do
  [ -d "$pkg/test" ] || continue
  timeout 1800 yarn node "$JEST_BIN" \\
    --runInBand --forceExit --verbose --ci \\
    --rootDir /home/{pr.repo} "$pkg/test" 2>&1
done

exit 0

""".format(
                    pr=self.pr,
                    packages=" ".join(_touched_packages(self.pr)),
                    babel8_env=(
                        "\n# This PR's new tests are itBabel8, inert unless the flag is set.\n"
                        "export BABEL_8_BREAKING=true\n"
                        if _needs_babel8(self.pr)
                        else ""
                    ),
                    esm_setup=(
                        "\n# This PR's tests are behind describeESM/itESM, which are no-ops\n"
                        "# unless .module-type says \"module\". Switching here rather than in\n"
                        "# prepare.sh keeps all three stages identical.\n"
                        "USE_ESM=true make use-esm >/dev/null 2>&1 || true\n"
                        if _needs_esm_mode(self.pr)
                        else ""
                    ),
                ),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

corepack enable
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install
{esm_switch}{build_env}make build

# Hard gate: assert the tree loads and the runner the stages depend on resolves.
node -e "require('./package.json'); require.resolve('jest'); require.resolve('jest-light-runner'); console.log('DEPS_OK')"

""".format(pr=self.pr, build_env=build_env, esm_switch=esm_switch),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{esm_switch}{build_env}make build || true
bash /home/run-tests.sh

""".format(pr=self.pr, build_env=build_env, esm_switch=esm_switch),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{esm_switch}{build_env}make build || true
bash /home/run-tests.sh

""".format(pr=self.pr, build_env=build_env, esm_switch=esm_switch),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
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

RUN bash /home/prepare.sh
"""


@Instance.register("babel", "babel_17576_to_16820")
class BABEL_17576_TO_16820(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        suite_re = re.compile(
            r"^(?:PASS|FAIL)\s+(\S+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"
        )
        test_re = re.compile(
            r"^(\s+)(?:(✓|✔)|(✕|✗|×)|(○))\s+"
            r"(?:skipped\s+)?(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
        )
        header_re = re.compile(r"^(\s+)(\S.*?)\s*$")

        suite = ""
        stack: list[tuple[int, str]] = []

        for line in clean_log.splitlines():
            line = line.rstrip()
            if not line.strip():
                continue

            match = suite_re.match(line)
            if match:
                suite = match.group(1)
                stack = []
                continue

            match = test_re.match(line)
            if match:
                indent = len(match.group(1))
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                parts = ([suite] if suite else []) + [t for _, t in stack]
                parts.append(match.group(5).strip())
                name = " > ".join(parts)

                if match.group(2):
                    passed_tests.add(name)
                elif match.group(3):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            match = header_re.match(line)
            if match and suite:
                title = match.group(2).strip()
                if title.startswith(("●", "at ")):
                    continue
                indent = len(match.group(1))
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, title))

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


Instance.register("babel", "babel")(BABEL_17576_TO_16820)
