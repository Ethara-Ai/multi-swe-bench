import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:22-bookworm"
BASE_TAG = "base-2183_to_2827"

_ENV = """\
export CI=true
export TERM=dumb
export NO_COLOR=1
export HUSKY=0
export DO_NOT_TRACK=1
export PI_NO_LOCAL_LLM=1
export NODE_OPTIONS="--max-old-space-size=4096"
export CARGO_TERM_COLOR=never
export RUSTUP_HOME=/usr/local/rustup
export CARGO_HOME=/usr/local/cargo
export PATH="/usr/local/cargo/bin:$PATH"
export CARGO_BUILD_JOBS=4
"""

_TARGETS = """\
CARGO_TARGETS=()
JS_FILES=()
for p in $(grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u); do
  if [[ "$p" =~ ^(.+)/tests/([^/]+)\\.rs$ ]] && [ -f "${BASH_REMATCH[1]}/Cargo.toml" ] && [ -f "$p" ]; then
    CARGO_TARGETS+=("${BASH_REMATCH[1]}|--test|${BASH_REMATCH[2]}")
  elif [[ "$p" =~ ^(.+)/src/.+\\.rs$ ]] && [ -f "${BASH_REMATCH[1]}/Cargo.toml" ] && [ -f "$p" ]; then
    CARGO_TARGETS+=("${BASH_REMATCH[1]}|--lib|")
  elif [[ "$p" =~ ^(packages/[^/]+)/test/.+\\.test\\.ts$ ]] && [ -f "${BASH_REMATCH[1]}/package.json" ] && [ -f "$p" ]; then
    JS_FILES+=("$p")
  fi
done
mapfile -t CARGO_TARGETS < <(printf '%s\\n' "${CARGO_TARGETS[@]}" | sed '/^$/d' | sort -u)
mapfile -t JS_FILES < <(printf '%s\\n' "${JS_FILES[@]}" | sed '/^$/d' | sort -u)
mapfile -t JS_PKGS < <(for f in "${JS_FILES[@]}"; do echo "${f%%/test/*}"; done | sed '/^$/d' | sort -u)
test $(( ${#CARGO_TARGETS[@]} + ${#JS_FILES[@]} )) -gt 0"""

_RUN = """\
rm -rf /tmp/msb && mkdir -p /tmp/msb
if [ -f package-lock.json ] && ! git diff --quiet HEAD -- package-lock.json; then
  npm ci --no-audit --no-fund --offline 2>&1 || RC=$?
fi
for t in "${CARGO_TARGETS[@]}"; do
  IFS='|' read -r PKG KIND NAME <<< "$t"
  echo "MSB_SUITE|cargo|$PKG|$KIND|$NAME"
  cargo test --locked --manifest-path "$PKG/Cargo.toml" --no-fail-fast $KIND $NAME 2>&1 | tee /tmp/msb/cargo-out.log || RC=$?
  python3 /home/msb_parse.py cargo "$PKG" "$KIND" "$NAME" /tmp/msb/cargo-out.log
done
i=0
for pkg in "${JS_PKGS[@]}"; do
  files=()
  for f in "${JS_FILES[@]}"; do
    if [[ "$f" == "$pkg/"* ]]; then files+=("${f#"$pkg"/}"); fi
  done
  if node -e 'process.exit(/vitest/.test(require(process.argv[1]).scripts?.test || "") ? 0 : 1)' "$PWD/$pkg/package.json"; then
    i=$((i+1))
    echo "MSB_SUITE|vitest|$pkg"
    (cd "$pkg" && npx --no-install vitest --run --reporter=default --reporter=json --outputFile.json="/tmp/msb/vitest-$i.json" "${files[@]}") 2>&1 || RC=$?
  else
    for f in "${files[@]}"; do
      i=$((i+1))
      echo "MSB_SUITE|node-test|$pkg/$f"
      echo "$pkg/$f" > "/tmp/msb/node-$i.file"
      (cd "$pkg" && node --test --import tsx --test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination="/tmp/msb/node-$i.xml" "$f") 2>&1 || RC=$?
    done
  fi
done
python3 /home/msb_parse.py js /tmp/msb "" "" "" """

_PARSER = """\
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

mode, a, b, c, path = sys.argv[1:6]


def emit(status, name):
    print(f"MSB_TEST|{status}|{name}", flush=True)


if mode == "cargo":
    text = open(path, encoding="utf-8", errors="replace").read()
    text = re.sub(r"\\x1b\\[[0-?]*[ -/]*[@-~]", "", text)
    target = f"{a}::{c}" if b == "--test" else f"{a}::lib"
    for line in text.splitlines():
        m = re.match(r"^test (.+?) \\.\\.\\. (ok|FAILED|ignored)\\b", line)
        if m:
            emit({"ok": "PASS", "FAILED": "FAIL", "ignored": "SKIP"}[m.group(2)], f"{target}::{m.group(1)}")
else:
    status_map = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "pending": "SKIP", "todo": "SKIP", "disabled": "SKIP"}
    for jf in sorted(glob.glob(os.path.join(a, "vitest-*.json"))):
        try:
            data = json.load(open(jf, encoding="utf-8"))
        except Exception:
            continue
        for f in data.get("testResults", []):
            rel = os.path.relpath(f.get("name", ""), os.getcwd()).replace(os.sep, "/")
            tests = f.get("assertionResults") or []
            if not tests:
                if f.get("status") == "failed":
                    emit("FAIL", rel)
                continue
            for t in tests:
                name = " > ".join(list(t.get("ancestorTitles") or []) + [t.get("title", "")])
                emit(status_map.get(t.get("status"), "FAIL"), f"{rel}::{name}")
    for xf in sorted(glob.glob(os.path.join(a, "node-*.xml"))):
        label_path = xf[:-4] + ".file"
        label = open(label_path).read().strip() if os.path.exists(label_path) else os.path.basename(xf)
        try:
            root = ET.parse(xf).getroot()
        except Exception:
            emit("FAIL", label)
            continue

        def walk(node, prefix):
            for child in node:
                if child.tag == "testsuite":
                    walk(child, prefix + [child.get("name", "")])
                elif child.tag == "testcase":
                    tags = {x.tag for x in child}
                    if tags & {"failure", "error"}:
                        status = "FAIL"
                    elif "skipped" in tags:
                        status = "SKIP"
                    else:
                        status = "PASS"
                    emit(status, f"{label}::" + " > ".join(prefix + [child.get("name", "")]))

        walk(root, [])
"""

_PREPARE_CMD = """\
node --version
rustc --version
cargo --version
if [ -f package-lock.json ]; then
  npm ci --no-audit --no-fund
fi
if [ -f Cargo.toml ]; then
  cargo fetch --locked
  for d in $(grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sed -n 's#^\\(.*\\)/\\(tests\\|src\\)/.*\\.rs$#\\1#p' | sort -u); do
    if [ -f "$d/Cargo.toml" ]; then cargo test --locked --manifest-path "$d/Cargo.toml" --no-run; fi
  done
fi
git checkout -- .
bash /home/check_git_changes.sh
echo DEPS_OK"""

_TEST_CMD = f"""\
RC=0
{_TARGETS}
{_RUN}
exit $RC"""


_CHECK_GIT_CHANGES = """\
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
"""


class PrimeAgent2183To2827ImageBase(Image):
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

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GIT_SSL_CAINFO=${{CA_CERT_PATH}} \\
    RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:$PATH \\
    TERM=dumb \\
    CI=true

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl jq build-essential pkg-config python3 \\
        libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev fd-find ripgrep \\
    && ln -sf "$(command -v fdfind)" /usr/local/bin/fd \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.98.1 && \\
    rustc --version && cargo --version

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class PrimeAgent2183To2827ImageDefault(Image):
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
        return PrimeAgent2183To2827ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        prepare_sh = """\
#!/bin/bash
set -euo pipefail

cd /home/REPO
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK

PREPARE_CMD
"""

        run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
TEST_CMD
"""

        test_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("ENV_BLOCK\n", _ENV)
                .replace("TEST_CMD", _TEST_CMD)
                .replace("PREPARE_CMD", _PREPARE_CMD)
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("/home/REPO", f"/home/{repo}")
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "msb_parse.py", _PARSER),
            File(".", "prepare.sh", render(prepare_sh)),
            File(".", "run.sh", render(run_sh)),
            File(".", "test-run.sh", render(test_run_sh)),
            File(".", "fix-run.sh", render(fix_run_sh)),
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

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || \\
    git fetch --no-tags origin "${{BASE_COMMIT}}"
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=2 -c pack.windowMemory=512m gc --prune=now --aggressive; \\
    git -c pack.threads=2 -c pack.windowMemory=512m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
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

{self.clear_env}
"""


@Instance.register("PrimeIntellect-ai", "prime_agent_2183_to_2827")
class PRIME_AGENT_2183_TO_2827(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PrimeAgent2183To2827ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {
            "PASS": passed_tests,
            "FAIL": failed_tests,
            "SKIP": skipped_tests,
        }

        re_test = re.compile(r"^MSB_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")

        for line in log.splitlines():
            m = re_test.match(line.strip())
            if m:
                buckets[m.group(1)].add(m.group(2))

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
