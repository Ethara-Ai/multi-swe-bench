import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ERA1_MAX_PR = 5615
ERA2_MAX_PR = 14257

ERA_BASE_IMAGES = {
    1: "node:10",
    2: "node:14",
    3: "node:18-bookworm",
}

JSON_BEGIN = "===== JEST JSON BEGIN ====="
JSON_END = "===== JEST JSON END ====="

REPORT_PATH = "/tmp/jest-report.json"

JEST_ENV = "LOG_LEVEL=fatal GIT_ALLOW_PROTOCOL=file"

JEST_CMD = (
    JEST_ENV + " npx jest --coverage=false --colors=false --passWithNoTests "
    "--runInBand --testTimeout=300000 "
    "--json --outputFile=" + REPORT_PATH + " --testPathPattern '[[PATTERN]]'"
)

GATE_CMD = (
    JEST_ENV + " npx jest --listTests --testPathPattern '[[PATTERN]]' > /dev/null"
)

APT_SOURCES_FIX = """
RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\
    sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list && \\
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid
"""

BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM [[BASE_IMAGE]]

ARG TARGETARCH
ARG REPO_URL="https://github.com/[[ORG]]/[[REPO]].git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="[[ORG]]/[[REPO]]" \\
      org.opencontainers.image.description="[[ORG]]/[[REPO]] Docker image" \\
      org.opencontainers.image.source="https://github.com/[[ORG]]/[[REPO]]" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/
[[APT_SOURCES_FIX]]
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

RUN git clone "${REPO_URL}" /home/[[REPO]]

WORKDIR /home/[[REPO]]

CMD ["/bin/bash"]
"""

PR_DOCKERFILE = """FROM [[BASE_FULL_NAME]]

ARG BASE_COMMIT="[[SHA]]"

[[COPY_COMMANDS]]

RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
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
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

yarn install --frozen-lockfile --network-timeout 600000 || \\
    yarn install --network-timeout 600000 || true

if node -e "process.exit(require('./package.json').scripts?.generate ? 0 : 1)"; then
    yarn generate
fi

node -e "require.resolve('jest')"
node -e "require.resolve('ts-jest')"
[[GATE_CMD]]
"""

RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd

[[TEST_BLOCK]]
"""

TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

[[TEST_BLOCK]]
"""

FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch fix.patch failed" >&2
    exit 1
fi

[[TEST_BLOCK]]
"""

TEST_BLOCK = """rm -f [[REPORT]] [[SCRIPT_REPORTS]]
set +e
[[JEST_CMD]]
[[SCRIPT_RUN]]set -e

if [ ! -s [[REPORT]] ]; then
    echo "Error: jest produced no report at [[REPORT]]" >&2
    exit 1
fi

echo "[[JSON_BEGIN]]"
cat [[REPORT]]
echo ""
echo "[[JSON_END]]"
[[SCRIPT_EMIT]]"""

_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_SPEC_SUFFIXES = (".spec.ts", ".spec.js", ".test.ts", ".test.js")
_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_ADDED_SCRIPT_RE = re.compile(r'^\+\s*"([\w:-]+)"\s*:\s*"([^"]*\bjest\b[^"]*)"', re.M)
_PACKAGE_JSON_HUNK_RE = re.compile(
    r"^diff --git a/package\.json b/package\.json$(.*?)(?=^diff --git |\Z)",
    re.M | re.S,
)


def _era_for(number: int) -> int:
    if number <= ERA1_MAX_PR:
        return 1
    if number <= ERA2_MAX_PR:
        return 2
    return 3


def _patch_paths(patch: Optional[str]) -> list[str]:
    if not patch:
        return []

    paths: list[str] = []
    for old, new in _DIFF_PATH_RE.findall(patch):
        paths.extend([old, new])
    return paths


def _spec_paths(pr: PullRequest) -> list[str]:
    specs: set[str] = set()

    for patch in (pr.test_patch, pr.fix_patch):
        for path in _patch_paths(patch):
            if path.endswith(_SPEC_SUFFIXES):
                specs.add(path)

    return sorted(specs)


def _source_dirs(pr: PullRequest) -> list[str]:
    dirs: set[str] = set()

    for path in _patch_paths(pr.fix_patch):
        if path.endswith(_SPEC_SUFFIXES):
            continue
        if not path.endswith(_SOURCE_SUFFIXES):
            continue
        if "/" not in path:
            continue
        dirs.add(path.rsplit("/", 1)[0])

    return sorted(dirs)


def _added_jest_scripts(pr: PullRequest) -> list[str]:
    names: set[str] = set()

    for hunk in _PACKAGE_JSON_HUNK_RE.findall(pr.fix_patch or ""):
        for name, _ in _ADDED_SCRIPT_RE.findall(hunk):
            names.add(name)

    return sorted(names)


def _script_spec_paths(pr: PullRequest) -> set[str]:
    paths: set[str] = set()

    for hunk in _PACKAGE_JSON_HUNK_RE.findall(pr.fix_patch or ""):
        for _, command in _ADDED_SCRIPT_RE.findall(hunk):
            for token in command.split():
                if token.endswith(_SPEC_SUFFIXES):
                    paths.add(token)

    return paths


def _test_pattern(pr: PullRequest) -> str:
    owned_by_script = _script_spec_paths(pr)

    alternatives = [
        re.escape(path)
        for path in _spec_paths(pr)
        if path not in owned_by_script
    ]
    alternatives.extend(
        re.escape(directory) + r"/[^/]*\.spec\.[tj]s"
        for directory in _source_dirs(pr)
    )

    if not alternatives:
        return r"\.spec\.ts$"

    return "(" + "|".join(alternatives) + ")$"


def _script_report_path(name: str) -> str:
    return "/tmp/jest-script-{name}.json".format(name=name)


def _script_run_block(pr: PullRequest) -> str:
    lines = []
    for name in _added_jest_scripts(pr):
        lines.append(
            "{env} yarn --silent {name} --colors=false --runInBand "
            "--testTimeout=300000 --json --outputFile={path}".format(
                env=JEST_ENV, name=name, path=_script_report_path(name)
            )
        )

    return "".join(line + "\n" for line in lines)


def _script_emit_block(pr: PullRequest) -> str:
    blocks = []
    for name in _added_jest_scripts(pr):
        path = _script_report_path(name)
        blocks.append(
            "\nif [ -s {path} ]; then\n"
            '    echo "{begin}"\n'
            "    cat {path}\n"
            '    echo ""\n'
            '    echo "{end}"\n'
            "fi\n".format(path=path, begin=JSON_BEGIN, end=JSON_END)
        )

    return "".join(blocks)


def render_script(template: str, pr: PullRequest) -> str:
    pattern = _test_pattern(pr)
    script_reports = " ".join(
        _script_report_path(name) for name in _added_jest_scripts(pr)
    )

    test_block = (
        TEST_BLOCK.replace("[[SCRIPT_REPORTS]]", script_reports)
        .replace("[[SCRIPT_RUN]]", _script_run_block(pr))
        .replace("[[SCRIPT_EMIT]]", _script_emit_block(pr))
    )

    return (
        template.replace("[[TEST_BLOCK]]", test_block)
        .replace("[[JEST_CMD]]", JEST_CMD.replace("[[PATTERN]]", pattern))
        .replace("[[GATE_CMD]]", GATE_CMD.replace("[[PATTERN]]", pattern))
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPORT]]", REPORT_PATH)
        .replace("[[JSON_BEGIN]]", JSON_BEGIN)
        .replace("[[JSON_END]]", JSON_END)
    )


class RenovateImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
        self._era = _era_for(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return ERA_BASE_IMAGES[self._era]

    def image_tag(self) -> str:
        return "base-era{era}".format(era=self._era)

    def workdir(self) -> str:
        return "base-era{era}".format(era=self._era)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return (
            BASE_DOCKERFILE.replace("[[BASE_IMAGE]]", ERA_BASE_IMAGES[self._era])
            .replace(
                "[[APT_SOURCES_FIX]]",
                APT_SOURCES_FIX if self._era in (1, 2) else "",
            )
            .replace("[[ORG]]", self.pr.org)
            .replace("[[REPO]]", self.pr.repo)
        )


class RenovateImageDefault(Image):
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
        return RenovateImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", render_script(PREPARE_SH, self.pr)),
            File(".", "run.sh", render_script(RUN_SH, self.pr)),
            File(".", "test-run.sh", render_script(TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", render_script(FIX_RUN_SH, self.pr)),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        return (
            PR_DOCKERFILE.replace(
                "[[BASE_FULL_NAME]]", self.dependency().image_full_name()
            )
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[COPY_COMMANDS]]", copy_commands.strip())
        )


@Instance.register("renovatebot", "renovate")
class Renovate(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RenovateImageDefault(self.pr, self._config)

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

        blob_re = re.compile(
            re.escape(JSON_BEGIN) + r"\s*(?P<body>.*?)" + re.escape(JSON_END),
            re.S,
        )

        for match in blob_re.finditer(clean_log):
            body = match.group("body")
            start = body.find("{")
            if start == -1:
                continue

            try:
                data = json.loads(body[start:])
            except ValueError:
                end = body.rfind("}")
                if end <= start:
                    continue
                try:
                    data = json.loads(body[start : end + 1])
                except ValueError:
                    continue

            for suite in data.get("testResults") or []:
                path = suite.get("name") or ""
                path = path.replace("\\", "/")
                marker = "/home/renovate/"
                if marker in path:
                    path = path.split(marker, 1)[1]

                assertions = suite.get("assertionResults") or []

                if not assertions:
                    if suite.get("status") == "failed" or suite.get("message"):
                        failed_tests.add(
                            "{path}::<suite failed to load>".format(path=path)
                        )
                    continue

                for assertion in assertions:
                    name = assertion.get("fullName") or assertion.get("title") or ""
                    if not name:
                        continue

                    test_id = (
                        "{path}::{name}".format(path=path, name=name)
                        if path
                        else name
                    )
                    status = (assertion.get("status") or "").lower()

                    if status == "passed":
                        passed_tests.add(test_id)
                    elif status == "failed":
                        failed_tests.add(test_id)
                    else:
                        skipped_tests.add(test_id)

        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
