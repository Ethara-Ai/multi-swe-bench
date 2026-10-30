import json
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_RUN_TESTS = """export CI=true
shopt -s nullglob

command -v go >/dev/null
command -v node >/dev/null
command -v python3 >/dev/null

run_suites() {
  if [ -f go.mod ]; then
    echo "@@SUITE go"
    if ! go test -json -count=1 -timeout 60m -skip '^TestUnixServerConcurrentSessionsAndPermissions$' ./... 2>&1; then
      echo "@@SUITE_STATUS go nonzero"
    fi
  fi

  local node_files=(tests/installer/*.test.mjs tests/hooks/*.test.mjs tests/test_*.js)
  local f
  for f in "${node_files[@]}"; do
    echo "@@NODE_FILE $f"
    if ! node --test --test-force-exit --test-reporter=tap "$f" 2>&1; then
      echo "@@SUITE_STATUS node nonzero $f"
    fi
  done

  local py_files=(tests/test_*.py)
  if [ ${#py_files[@]} -gt 0 ]; then
    echo "@@SUITE python"
    if ! python3 -m unittest discover -s tests -v 2>&1; then
      echo "@@SUITE_STATUS python nonzero"
    fi
  fi
  echo "@@SUITE end"
}

run_suites 2>&1 | tee /tmp/suite_output.log

grep -Eq '"Action":"(pass|fail|skip)","Package":"[^"]*","Test":|^ *(not ok|ok) [0-9]+ - | \\.\\.\\. (ok|FAIL|ERROR|skipped|expected failure|unexpected success)' /tmp/suite_output.log
"""

_APPLY_PATCHES = """apply_patches() {
  local excludes=()
  local path
  while IFS= read -r path; do
    excludes+=("--exclude=$path")
  done < <(sed -n -e 's#^Binary files .* and b/\\(.*\\) differ$#\\1#p' -e 's#^Binary files a/\\(.*\\) and /dev/null differ$#\\1#p' "$@" | sort -u)
  git apply --whitespace=nowarn "${excludes[@]}" "$@"
}
"""


class CavemanImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "golang:1.27.1-bookworm"

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

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

ENV CI=true \\
    CGO_ENABLED=1 \\
    GOTOOLCHAIN=auto \\
    GOFLAGS=-buildvcs=false \\
    PATH=/usr/local/node/bin:$PATH

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git python3 xz-utils \\
    && rm -rf /var/lib/apt/lists/*

ARG NODE_VERSION=22.23.4

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
      amd64) node_arch=x64 ;; \\
      arm64) node_arch=arm64 ;; \\
      *) echo "unsupported arch $arch"; exit 1 ;; \\
    esac; \\
    dist="https://nodejs.org/dist/v${{NODE_VERSION}}"; \\
    tarball="node-v${{NODE_VERSION}}-linux-${{node_arch}}.tar.xz"; \\
    cd /tmp; \\
    curl -fsSL "$dist/$tarball" -o "$tarball"; \\
    curl -fsSL "$dist/SHASUMS256.txt" | grep " $tarball\\$" | sha256sum -c -; \\
    mkdir -p /usr/local/node; \\
    tar -xJf "$tarball" -C /usr/local/node --strip-components=1; \\
    rm -f "$tarball"; \\
    node --version; npm --version

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class CavemanImageDefault(Image):
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
        return CavemanImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
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
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "$BASE_COMMIT"
bash /home/check_git_changes.sh

if [ -f go.mod ]; then
  go version
  go mod download
  go test -count=1 -exec /bin/true ./... || true
fi

if [ -f package.json ]; then
  if [ -f package-lock.json ]; then
    npm ci --no-audit --no-fund --ignore-scripts || npm install --no-audit --no-fund --ignore-scripts --no-package-lock || true
  else
    npm install --no-audit --no-fund --ignore-scripts --no-package-lock || true
  fi
fi

node --version
python3 --version
if [ -f go.mod ]; then
  go build ./...
fi
if [ -f package.json ]; then
  node -e "const fs=require('fs');const p=JSON.parse(fs.readFileSync('package.json','utf8'));const missing=Object.keys(p.dependencies||{{}}).filter(d=>!fs.existsSync('node_modules/'+d+'/package.json'));if(missing.length){{console.error('missing deps: '+missing.join(' '));process.exit(1)}}"
fi
echo DEPS_OK

""".format(repo=repo),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{tests}
""".format(repo=repo, tests=_RUN_TESTS),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{apply}
apply_patches /home/test.patch
{tests}
""".format(repo=repo, apply=_APPLY_PATCHES, tests=_RUN_TESTS),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{apply}
apply_patches /home/test.patch /home/fix.patch
{tests}
""".format(repo=repo, apply=_APPLY_PATCHES, tests=_RUN_TESTS),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{repo}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("JuliusBrussee", "caveman")
class Caveman(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return CavemanImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_tap = re.compile(r"^((?:    )*)(not ok|ok) \d+ - (.*?)(?:\s+#\s*(SKIP|TODO)\b.*)?$")
        re_subtest = re.compile(r"^((?:    )*)# Subtest: (.*)$")
        re_py_head = re.compile(r"^(\w+) \(([\w.]+)\)")
        re_py_result = re.compile(
            r"\.\.\. (ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b"
        )

        suite = None
        node_file = None
        subtests: dict[int, str] = {}
        py_pending = None

        def record(name: str, status: str) -> None:
            if status == "pass":
                passed_tests.add(name)
            elif status == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        for raw_line in test_log.splitlines():
            line = raw_line.rstrip("\r")

            if line.startswith("@@SUITE "):
                suite = line[len("@@SUITE ") :].strip()
                node_file = None
                py_pending = None
                continue
            if line.startswith("@@NODE_FILE "):
                suite = "node"
                node_file = line[len("@@NODE_FILE ") :].strip()
                subtests = {}
                continue

            if suite == "go":
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    event = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                test = event.get("Test")
                action = event.get("Action")
                if not test or action not in ("pass", "fail", "skip"):
                    continue
                record(f"{event.get('Package', '')}::{test}", action)

            elif suite == "node" and node_file:
                sub = re_subtest.match(line)
                if sub:
                    level = len(sub.group(1)) // 4
                    subtests[level] = sub.group(2).strip()
                    for deeper in [k for k in subtests if k > level]:
                        del subtests[deeper]
                    continue
                tap = re_tap.match(line)
                if not tap:
                    continue
                level = len(tap.group(1)) // 4
                parts = [subtests[i] for i in range(level) if i in subtests]
                parts.append(tap.group(3).strip())
                name = f"{node_file}::{'/'.join(parts)}"
                if tap.group(4):
                    record(name, "skip")
                elif tap.group(2) == "ok":
                    record(name, "pass")
                else:
                    record(name, "fail")

            elif suite == "python":
                head = re_py_head.match(line)
                if head:
                    py_pending = head.group(2)
                result = re_py_result.search(line)
                if result and py_pending:
                    outcome = result.group(1)
                    name = f"python::{py_pending}"
                    if outcome in ("ok", "expected failure"):
                        record(name, "pass")
                    elif outcome == "skipped":
                        record(name, "skip")
                    else:
                        record(name, "fail")
                    py_pending = None

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
