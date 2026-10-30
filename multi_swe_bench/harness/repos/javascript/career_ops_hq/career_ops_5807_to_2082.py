from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:22-bookworm"

BASE_TAG = "base-5807_to_2082"

FILE_BEGIN = "###FILE_BEGIN###"
FILE_END = "###FILE_END###"

CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: uncommitted changes"
  git status --porcelain | head -40
  exit 1
}

echo "check_git_changes: no uncommitted changes"
exit 0
"""

PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

export CI=true
export TZ=UTC
export NO_COLOR=1
export FORCE_COLOR=0
export HUSKY=0
export HUSKY_SKIP_INSTALL=1
export NEXT_TELEMETRY_DISABLED=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export npm_config_audit=false
export npm_config_fund=false
export npm_config_update_notifier=false

install_deps() {
    pkg_dir="$1"
    test -f "$pkg_dir/package.json" || return 0
    cd "$pkg_dir"
    if [ -f package-lock.json ]; then
        if npm ci --no-audit --no-fund --loglevel=error; then
            echo "install_deps: $pkg_dir -> npm ci"
        else
            echo "install_deps: $pkg_dir -> npm ci rejected the lockfile, falling back to npm install"
            npm install --no-audit --no-fund --loglevel=error
        fi
    else
        npm install --no-audit --no-fund --loglevel=error
        echo "install_deps: $pkg_dir -> npm install (no lockfile at this commit)"
    fi
    cd /home/[[REPO]]
}

install_deps /home/[[REPO]]
install_deps /home/[[REPO]]/web

cd /home/[[REPO]]
node --version
npm --version

gate() {
    pkg_dir="$1"
    test -f "$pkg_dir/package.json" || return 0
    cd "$pkg_dir"
    test -d node_modules
    node --input-type=module -e '
import { readFileSync } from "node:fs";
const pkg = JSON.parse(readFileSync("package.json", "utf8"));
const names = Object.keys({ ...(pkg.dependencies || {}), ...(pkg.devDependencies || {}) })
    .filter((name) => !name.startsWith("@types/"));
for (const name of names) import.meta.resolve(name);
console.log("DEPS_OK " + process.cwd() + " " + names.length);
'
    cd /home/[[REPO]]
}

gate /home/[[REPO]]
gate /home/[[REPO]]/web

node --experimental-strip-types -e 'console.log("STRIP_TYPES_OK")'
"""

RUN_SUITE_SH = r"""run_suite() {
    suite_label="$1"
    suite_root="$2"
    shift 2
    test -d "$suite_root/tests" || return 0
    cd "$suite_root"
    suite_files=$(find tests -type f -name '*.test.mjs' | LC_ALL=C sort)
    for suite_file in $suite_files; do
        echo "[[FILE_BEGIN]] $suite_label/$suite_file"
        if timeout -k 10 600 node "$@" --test --test-reporter=tap --test-timeout=120000 "$suite_file"; then
            echo "[[FILE_END]] $suite_label/$suite_file rc=0"
        else
            echo "[[FILE_END]] $suite_label/$suite_file rc=$?"
        fi
    done
    cd /home/[[REPO]]
}

run_suite root /home/[[REPO]]
run_suite web /home/[[REPO]]/web --experimental-strip-types
"""

STAGE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export NODE_ENV=test
export NO_COLOR=1
export FORCE_COLOR=0
export NEXT_TELEMETRY_DISABLED=1
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/[[REPO]]

git checkout -- .
git clean -fdq -e node_modules

[[PATCH_STEP]]
[[RUN_SUITE]]"""

PRUNE = r"""RUN set -eux; \
    cd /home/[[REPO]]; \
    test "$(git rev-parse HEAD)" = "[[SHA]]"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test -z "$(git remote)"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/[[REPO]]/.gitmodules ]; then \
        cd /home/[[REPO]] && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi"""


class CareerOpsImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

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

ENV npm_config_audit=false \\
    npm_config_fund=false \\
    npm_config_update_notifier=false \\
    HUSKY=0 \\
    HUSKY_SKIP_INSTALL=1 \\
    NEXT_TELEMETRY_DISABLED=1 \\
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \\
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates coreutils \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class CareerOpsImageDefault(Image):
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
        return CareerOpsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[FILE_BEGIN]]", FILE_BEGIN)
            .replace("[[FILE_END]]", FILE_END)
        )

    def _stage(self, patch_step: str) -> str:
        return self._expand(
            STAGE_SH.replace("[[RUN_SUITE]]", RUN_SUITE_SH).replace(
                "[[PATCH_STEP]]", patch_step
            )
        )

    def install_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ]

    def grading_files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "run.sh", self._stage("")),
            File(
                ".",
                "test-run.sh",
                self._stage("git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                self._stage(
                    "git apply --whitespace=nowarn /home/test.patch\n"
                    "git apply --whitespace=nowarn /home/fix.patch\n"
                ),
            ),
        ]

    def files(self) -> list[File]:
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{self.global_env}

{copies}
RUN bash /home/prepare.sh

{self._expand(PRUNE)}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_BEGIN_RE = re.compile(r"^###FILE_BEGIN###\s+(\S+)\s*$")
_FILE_END_RE = re.compile(r"^###FILE_END###\s+(\S+)\s+rc=(-?\d+)\s*$")
_SUBTEST_RE = re.compile(r"^(\s*)#\s*Subtest:\s*(.*)$")
_RESULT_RE = re.compile(r"^(\s*)(not ok|ok)\s+\d+\s*(?:-\s*)?(.*)$")
_DIRECTIVE_RE = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)


def _parse_node_test_tap(log: str) -> tuple[set[str], set[str], set[str]]:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    current_file: Optional[str] = None
    subtests: dict[int, str] = {}
    has_children: dict[int, bool] = {}
    in_yaml = False

    for raw_line in log.split("\n"):
        line = raw_line.rstrip("\r")

        match = _FILE_BEGIN_RE.match(line)
        if match:
            current_file = match.group(1)
            subtests = {}
            has_children = {}
            in_yaml = False
            continue

        if _FILE_END_RE.match(line):
            current_file = None
            subtests = {}
            has_children = {}
            in_yaml = False
            continue

        if current_file is None:
            continue

        stripped = line.strip()
        if in_yaml:
            if stripped == "...":
                in_yaml = False
            continue
        if stripped == "---":
            in_yaml = True
            continue

        match = _SUBTEST_RE.match(line)
        if match:
            indent = len(match.group(1))
            subtests = {k: v for k, v in subtests.items() if k < indent}
            has_children = {k: True for k in has_children if k < indent}
            subtests[indent] = match.group(2).strip()
            has_children[indent] = False
            continue

        match = _RESULT_RE.match(line)
        if not match:
            continue

        indent = len(match.group(1))
        if has_children.get(indent):
            continue

        status = match.group(2)
        description = match.group(3).strip()

        directive = _DIRECTIVE_RE.search(description)
        if directive:
            description = _DIRECTIVE_RE.sub("", description).strip()

        own = subtests.get(indent, description)
        if not own:
            continue

        ancestors = [subtests[k] for k in sorted(subtests) if k < indent]
        name = f"{current_file}::" + " > ".join(ancestors + [own])

        if directive:
            skipped.add(name)
        elif status == "ok":
            passed.add(name)
        else:
            failed.add(name)

    passed -= failed
    passed -= skipped
    skipped -= failed

    return passed, failed, skipped


@Instance.register("career-ops-hq", "career_ops_5807_to_2082")
@Instance.register("career-ops-hq", "career-ops")
class CAREER_OPS_5807_TO_2082(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CareerOpsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = _ANSI_RE.sub("", test_log)
        passed, failed, skipped = _parse_node_test_tap(log)

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
