from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "openclaw_47719_to_45689"

BASE_TAG = "base-45689_to_47719"

NODE_IMAGE = "node:22-bookworm"

VITEST_START = "-----MSB_VITEST_JSON_START-----"
VITEST_END = "-----MSB_VITEST_JSON_END-----"
VITEST_JSON = "/home/vitest-report.json"


TEST_SUFFIX = ".test.ts"


def _target_tests(pr: PullRequest) -> list[str]:
    seen: list[str] = []
    for line in (pr.test_patch or "").splitlines():
        if not line.startswith("diff --git "):
            continue
        path = line.split(" b/", 1)[-1].strip()
        if not path.endswith(TEST_SUFFIX):
            continue
        if "/fixtures/" in path or "/__fixtures__/" in path:
            continue
        if path not in seen:
            seen.append(path)
    return seen


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository"
    exit 1
fi

git update-index -q --really-refresh || true

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
exit 0
"""


_PREPARE_SH = """#!/bin/bash
set -euo pipefail

export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export CI=true
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1

cd /home/{repo}

git reset --hard
git clean -fdx -e node_modules -e '**/node_modules'
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

corepack prepare --activate

for i in 1 2 3 4; do
    if pnpm install --frozen-lockfile --reporter=append-only; then
        break
    fi
    echo "prepare: pnpm install attempt $i failed, retrying"
    sleep $((i * 15))
    [ "$i" = "4" ] && exit 1
done

test -d /home/{repo}/node_modules
node -e "require('fs').accessSync('vitest.config.ts')"
./node_modules/.bin/vitest --version
echo "prepare: DEPS_OK"
"""


_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export FORCE_COLOR=0
export NO_COLOR=1

cd /home/{repo}

TARGETS=$(cat /home/vitest_targets.txt 2>/dev/null || true)

rm -f {vitest_json}

echo "{vitest_start}"
if [ -n "$TARGETS" ]; then
    rc=0
    timeout --signal=KILL 900 \\
        ./node_modules/.bin/vitest run --config vitest.config.ts \\
            --reporter=json --outputFile={vitest_json} --passWithNoTests \\
            $TARGETS > /tmp/vitest_stdout 2>&1 || rc=$?
    echo "vitest exit: $rc"
    [ "$rc" = "137" ] && echo "vitest: KILLED by the 900s ceiling"
    if [ -s {vitest_json} ]; then
        node -e '
const fs = require("fs");
const r = JSON.parse(fs.readFileSync("{vitest_json}", "utf8"));
const out = [];
for (const f of r.testResults || []) {{
    for (const a of f.assertionResults || []) {{
        out.push({{
            n: f.name,
            a: a.ancestorTitles || [],
            t: a.title,
            s: a.status,
        }});
    }}
}}
process.stdout.write(JSON.stringify({{ numTotalTests: out.length, tests: out }}, null, 1));
'
    else
        echo "vitest: no JSON report produced"
    fi
else
    echo "vitest: this PR names no test file"
fi
echo "{vitest_end}"

echo "----- vitest stdout (human, not parsed) -----"
tail -60 /tmp/vitest_stdout 2>/dev/null || true
"""


_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

for p in "$@"; do
    if git apply --whitespace=nowarn "$p"; then
        echo "apply_patch: applied $p"
    elif git apply --whitespace=nowarn --exclude=CHANGELOG.md "$p"; then
        echo "apply_patch: applied $p without CHANGELOG.md"
    else
        echo "apply_patch: retrying $p with --3way"
        git apply --3way --whitespace=nowarn "$p"
        echo "apply_patch: applied $p with --3way"
    fi
done

if git grep -qI -e '^<<<<<<< ' -- . 2>/dev/null; then
    echo "apply_patch: conflict markers survived"
    git grep -nI -e '^<<<<<<< ' -- . | head
    exit 1
fi

if ! git diff --quiet -- package.json pnpm-lock.yaml; then
    echo "apply_patch: manifest changed, re-installing"
    pnpm install --frozen-lockfile --reporter=append-only \\
        || pnpm install --no-frozen-lockfile --reporter=append-only
fi
"""


class OpenclawImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    CI=true \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    REPO_URL=${{REPO_URL}} \\
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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates coreutils \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN corepack enable

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git -C /home clone "${{REPO_URL}}" {repo}; then break; fi; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    test -d /home/{repo}/.git

CMD ["/bin/bash"]
"""


class OpenclawImageDefault(Image):
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
        return OpenclawImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        repo_url = f"https://github.com/{self.pr.org}/{repo}.git"
        targets = _target_tests(self.pr)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "vitest_targets.txt",
                ("\n".join(targets) + "\n") if targets else "",
            ),
            File(
                ".",
                "check_git_changes.sh",
                _CHECK_GIT_CHANGES_SH.format(repo=repo),
            ),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(repo=repo, repo_url=repo_url, sha=sha),
            ),
            File(
                ".",
                "apply_patch.sh",
                _APPLY_PATCH_SH.format(repo=repo),
            ),
            File(
                ".",
                "run_tests.sh",
                _RUN_TESTS_SH.format(
                    repo=repo,
                    vitest_json=VITEST_JSON,
                    vitest_start=VITEST_START,
                    vitest_end=VITEST_END,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{copies}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN if [ -f .gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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
"""


@Instance.register("openclaw", NUMBER_INTERVAL)
class Openclaw47719To45689(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        start = test_log.find(VITEST_START)
        end = test_log.find(VITEST_END)
        if start == -1 or end == -1 or end <= start:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed,
                failed_tests=failed,
                skipped_tests=skipped,
            )

        blob = test_log[start + len(VITEST_START) : end]
        blob = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", blob)

        first = blob.find("{")
        last = blob.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed,
                failed_tests=failed,
                skipped_tests=skipped,
            )

        try:
            records = json.loads(blob[first : last + 1]).get("tests") or []
        except (json.JSONDecodeError, AttributeError):
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed,
                failed_tests=failed,
                skipped_tests=skipped,
            )

        counter: dict[str, int] = {}
        for rec in records:
            if not isinstance(rec, dict):
                continue
            fname = str(rec.get("n") or "")
            rel = re.sub(r"^.*?/home/[^/]+/", "", fname)
            parts = [str(x) for x in (rec.get("a") or [])]
            title = str(rec.get("t") or "")
            name = f"{rel}:{' > '.join(parts + [title])}" if parts else f"{rel}:{title}"

            counter[name] = counter.get(name, 0) + 1
            if counter[name] > 1:
                name = f"{name} #{counter[name]}"

            status = str(rec.get("s") or "")
            if status == "passed":
                passed.add(name)
            elif status == "failed":
                failed.add(name)
            else:
                skipped.add(name)

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
