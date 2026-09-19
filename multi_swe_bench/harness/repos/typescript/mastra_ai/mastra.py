from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "mastra-ai"
REPO = "mastra"
REPO_DIR = "/home/mastra"
PNPM_VERSION = "10.18.2"

# node:22.13.0-bookworm, not slim: transitive dependencies build native addons
# through node-gyp on install. CI pins node 22.13.0 at every base sha.
_NODE_IMAGE = "node:22.13.0-bookworm"
_BASE_TAG = "base-node22-pnpm"

_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]

BEGIN_MARKER = "===== BEGIN VITEST JSON ====="
END_MARKER = "===== END VITEST JSON ====="

# Workspace package dirs -> pnpm package names (stable across all base SHAs).
_PKG_NAMES = {
    "packages/core": "@mastra/core",
    "packages/rag": "@mastra/rag",
    "packages/mcp": "@mastra/mcp",
    "voice/google": "@mastra/voice-google",
}


# --------------------------------------------------------------------- scripts

SHEBANG = "#!/bin/bash\nset -eo pipefail"

CHECK_GIT_CHANGES = """#!/bin/bash
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

APPLY_TEST = """if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi"""

APPLY_FIX = """if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi"""

# Exported by prepare.sh and every run script. Free of braces so it can be
# dropped into the templates below.
RUN_ENV = """export CI=true
export HUSKY=0
export NODE_OPTIONS="--max-old-space-size=4096"
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false
export DO_NOT_TRACK=1"""


def _group_test_files(test_patch: str) -> dict[str, list[str]]:
    """Extract .test./.spec. files from the test patch, grouped by workspace package dir."""
    seen: set[str] = set()
    files: list[str] = []
    for m in re.finditer(r"^diff --git a/\S+ b/(\S+)", test_patch, re.MULTILINE):
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)
        if ".test." in path or ".spec." in path:
            files.append(path)

    grouped: dict[str, list[str]] = {}
    for f in files:
        parts = f.split("/")
        if len(parts) >= 3:
            pkg_dir = "/".join(parts[:2])
            rel = "/".join(parts[2:])
        else:
            pkg_dir = "."
            rel = f
        grouped.setdefault(pkg_dir, []).append(rel)
    return grouped


class MastraImageBase(Image):
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
        return "node:22.13.0-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # The leading syntax directive keeps DockerfileEnhancer from rewriting
        # the clone or pinning this shared image to one PR's BASE_COMMIT, so the
        # infrastructure block it would otherwise contribute is written out here.
        # BASE_COMMIT is declared but deliberately unused: build_dataset passes
        # it to every image whose dependency() is a str, and an undeclared build
        # arg is a build warning.
        return f"""# syntax=docker/dockerfile:1.6

FROM {base_img}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"
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
    CI=1 \\
    HUSKY=0 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.description="{ORG}/{REPO} Docker image" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/ca-bundle.crt
{global_env}
WORKDIR /home/

{apt_command}

# corepack's bundled signature keys cannot verify npm's rotated registry
# signatures, so install pnpm directly with a version valid for every era.
RUN npm install -g pnpm@{PNPM_VERSION}

RUN git clone "${{REPO_URL}}" /home/{REPO}
{clear_env}
CMD ["/bin/bash"]
"""


class MastraImageDefault(Image):
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
        return MastraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_cmd(self) -> str:
        grouped = _group_test_files(self.pr.test_patch)
        if not grouped:
            return (
                f"cd {REPO_DIR}\n"
                "vitest_rc=0\n"
                "pnpm exec vitest run --reporter=json "
                f"--outputFile=/home/vitest-results-0.json --no-file-parallelism --no-color "
                "|| vitest_rc=$?\n"
                'echo "VITEST_PACKAGE_EXIT=${vitest_rc}"\n'
                f"echo '{BEGIN_MARKER}'\n"
                "cat /home/vitest-results-0.json 2>/dev/null || true\n"
                "echo ''\n"
                f"echo '{END_MARKER}'"
            )

        blocks: list[str] = []
        for i, (pkg_dir, rel_files) in enumerate(sorted(grouped.items())):
            files_str = " ".join(rel_files)
            blocks.append(
                f"cd {REPO_DIR}/{pkg_dir}\n"
                f"rm -f /home/vitest-results-{i}.json\n"
                "vitest_rc=0\n"
                "pnpm exec vitest run "
                f"{files_str} "
                "--reporter=json "
                f"--outputFile=/home/vitest-results-{i}.json "
                "--no-file-parallelism --no-color "
                "|| vitest_rc=$?\n"
                'echo "VITEST_PACKAGE_EXIT=${vitest_rc}"\n'
                f"echo '{BEGIN_MARKER}'\n"
                f"cat /home/vitest-results-{i}.json 2>/dev/null || true\n"
                "echo ''\n"
                f"echo '{END_MARKER}'"
            )
        return "\n\n".join(blocks)

    def _install_filters(self) -> tuple[str, str]:
        # pnpm '<name>...' = package plus its dependencies; turbo --filter=<name>
        # builds the package after its ^build dependency graph.
        grouped = _group_test_files(self.pr.test_patch)
        names = sorted({_PKG_NAMES[d] for d in grouped if d in _PKG_NAMES})
        if not names:
            names = ["@mastra/core"]
        install_flags = " ".join(f"--filter '{n}...'" for n in names)
        turbo_flags = " ".join(f"--filter={n}" for n in names)
        return install_flags, turbo_flags

    def files(self) -> list[File]:
        test_cmd = self._test_cmd()
        install_flags, turbo_flags = self._install_filters()

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
cd {REPO_DIR}
bash /home/check_git_changes.sh
{RUN_ENV}

# pnpm is preinstalled in the base image; reinstall only if it is missing.
command -v pnpm >/dev/null 2>&1 || npm install -g pnpm@{PNPM_VERSION}

# Install only the workspace packages under test plus their dependencies.
pnpm install {install_flags} --include-workspace-root

# Build the package under test and its workspace dependency graph. Non-fatal:
# core-only PRs run tests from src and do not need dist output.
pnpm exec turbo build {turbo_flags} || echo "WARN: turbo build failed (continuing)"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
cd {REPO_DIR}
{RUN_ENV}

{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd {REPO_DIR}
{APPLY_TEST}
{RUN_ENV}

{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd {REPO_DIR}
{APPLY_FIX}
{RUN_ENV}

{test_cmd}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # Chains to a base Image rather than a str, so DockerfileEnhancer returns
        # this verbatim and injects nothing; the hardening block is applied by
        # hand. It opens with `git checkout --detach "${BASE_COMMIT}"`, so it
        # performs this PR's checkout as well as pruning the full history
        # inherited from the shared base. The repo is cloned once in that base,
        # so this layer never clones and needs no REPO_URL.
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT={self.pr.base.sha}
{global_env}
{copy_commands}
WORKDIR /home/{REPO}

{Image._HARDENING_BLOCK.rstrip()}

RUN bash /home/prepare.sh
{clear_env}"""


@Instance.register("mastra-ai", "mastra")
class Mastra(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return MastraImageDefault(self.pr, self._config)

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

        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        blobs = self._extract_json_blobs(clean)
        if blobs:
            for payload in blobs:
                self._collect_from_json(
                    payload, passed_tests, failed_tests, skipped_tests
                )
        else:
            self._collect_from_console(
                clean, passed_tests, failed_tests, skipped_tests
            )

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

    @staticmethod
    def _extract_json_blobs(clean_log: str) -> list[dict]:
        payloads: list[dict] = []
        start = 0
        while True:
            begin = clean_log.find(BEGIN_MARKER, start)
            if begin == -1:
                break
            begin += len(BEGIN_MARKER)
            end = clean_log.find(END_MARKER, begin)
            blob = clean_log[begin:end] if end != -1 else clean_log[begin:]
            start = (end + len(END_MARKER)) if end != -1 else len(clean_log)
            blob = blob.strip()
            if not blob:
                continue
            first = blob.find("{")
            last = blob.rfind("}")
            if first == -1 or last == -1 or last <= first:
                continue
            try:
                parsed = json.loads(blob[first : last + 1])
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                payloads.append(parsed)
        return payloads

    @staticmethod
    def _relative_path(name: str) -> str:
        p = (name or "").replace("\\", "/")
        prefix = REPO_DIR + "/"
        if p.startswith(prefix):
            p = p[len(prefix) :]
        return p

    @classmethod
    def _collect_from_json(
        cls,
        payload: dict,
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        for suite in payload.get("testResults") or []:
            if not isinstance(suite, dict):
                continue
            rel = cls._relative_path(suite.get("name") or "")
            assertions = suite.get("assertionResults") or []

            if not assertions:
                if (suite.get("status") or "").lower() == "failed" and rel:
                    failed.add(rel)
                continue

            for a in assertions:
                if not isinstance(a, dict):
                    continue
                title = (a.get("title") or "").strip()
                ancestors = [
                    str(x).strip()
                    for x in (a.get("ancestorTitles") or [])
                    if str(x).strip()
                ]
                if not title and not ancestors:
                    continue
                parts = ([rel] if rel else []) + ancestors + ([title] if title else [])
                test_id = " > ".join(parts)
                status = (a.get("status") or "").lower()
                if status in ("failed", "error"):
                    failed.add(test_id)
                elif status in ("pending", "skipped", "todo", "disabled"):
                    skipped.add(test_id)
                elif status == "passed":
                    passed.add(test_id)

    @staticmethod
    def _collect_from_console(
        clean_log: str,
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        line_re = re.compile(
            r"^\s*(?P<mark>[✓√✔×✗❌↓○⊘])\s+"
            r"(?P<name>\S+\.(?:test|spec)\.[cm]?[jt]sx?\s*>\s*.+?)"
            r"(?:\s+\d+(?:\.\d+)?\s*m?s)?\s*$"
        )
        for line in clean_log.splitlines():
            m = line_re.match(line)
            if not m:
                continue
            mark = m.group("mark")
            name = re.sub(r"\s*>\s*", " > ", m.group("name").strip())
            if mark in "✓√✔":
                passed.add(name)
            elif mark in "×✗❌":
                failed.add(name)
            else:
                skipped.add(name)
