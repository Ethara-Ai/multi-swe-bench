from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        section
        for section in sections
        if "GIT binary patch" not in section
        and not re.search(r"^Binary files .* differ$", section, re.MULTILINE)
    )


_ORG = "earendil-works"
_REPO = "pi"
_NODE_IMAGE = "node:22-bookworm"

_MITM_PROXY_ARGS = (
    'ARG http_proxy=""\n'
    'ARG https_proxy=""\n'
    'ARG HTTP_PROXY=""\n'
    'ARG HTTPS_PROXY=""\n'
    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
)

_BASE_ENV_BLOCK = (
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
    "    CURL_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \\\n"
    "    NODE_OPTIONS=--max-old-space-size=4096 \\\n"
    "    CI=true \\\n"
    "    HUSKY=0 \\\n"
    "    NODE_ENV=test \\\n"
    "    NO_UPDATE_NOTIFIER=1 \\\n"
    "    NPM_CONFIG_UPDATE_NOTIFIER=false \\\n"
    "    NPM_CONFIG_FUND=false \\\n"
    "    NPM_CONFIG_AUDIT=false"
)

_MITM_CERT_SYMLINKS = (
    "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
)

_HARDENING_BLOCK = """RUN set -eux; \\
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
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

_SUBMODULE_SCRUB_BLOCK = """RUN if [ -f .gitmodules ]; then \\
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
    fi"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

export CI=true
export HUSKY=0

# This repo is fetched over a link that resets long-running connections, and three image
# builds install concurrently, so make every npm fetch in this script retry rather than
# fail the whole image on one dropped socket. Timeouts only; no TLS or integrity checks
# are relaxed.
export npm_config_fetch_retries=5
export npm_config_fetch_retry_mintimeout=20000
export npm_config_fetch_retry_maxtimeout=120000
export npm_config_fetch_timeout=600000

npm ci --no-audit --no-fund

# packages/ai/src/providers/data is gitignored, so the generated model data is absent at
# every commit and has to be reconstructed before anything can build. Two sources exist,
# tried in this order:
#
#   1. A published @earendil-works/pi-ai artifact, which ships dist/providers/data. This is
#      reproducible and era-correct, so it is preferred. package.json's version is bumped at
#      release time, so a base commit taken mid-window can need a neighbouring release's data
#      rather than the one it names: at 1355cd36e0b1 (which declares 0.84.2) src/providers/
#      xai.ts is Provider<"openai-responses">, and only 0.84.3's data satisfies it, because
#      the generated *.models.ts infer their literal types from data/*.json. Candidates are
#      therefore the declared version followed by its nearest neighbours.
#   2. `npm run hydrate:model-data`, the repo's own generator. This is what check-model-data
#      tells you to run and what upstream CI does, but it queries live provider APIs, so it
#      is the fallback rather than the default.
#
# The arbiter in both cases is the repo's own root `build:offline` -- the only thing that
# actually proves the data type-checks against this commit's tracked sources. It reaches
# packages/ai within a couple of seconds, so probing with it is cheap.
pi_ai_version="$(node -p "require('./packages/ai/package.json').version")"
candidates="$(node -e '
const { execSync } = require("node:child_process");
const want = process.argv[1];
let all = [];
try {
    all = JSON.parse(execSync("npm view @earendil-works/pi-ai versions --json", { encoding: "utf8" }));
} catch (error) {
    all = [];
}
if (!Array.isArray(all)) all = [];
let at = all.indexOf(want);
if (at < 0) {
    // Declared version was never published: start from the newest release instead.
    at = all.length - 1;
}
const order = [];
if (all.indexOf(want) >= 0) order.push(want);
for (let step = 1; step <= 5; step += 1) {
    if (at + step < all.length) order.push(all[at + step]);
    if (at - step >= 0) order.push(all[at - step]);
}
console.log([...new Set(order.filter(Boolean))].join(" "));
' "$pi_ai_version")"

model_data_source=""
for candidate in $candidates; do
    rm -rf /tmp/pi-ai-data
    mkdir -p /tmp/pi-ai-data
    if ! (cd /tmp/pi-ai-data && npm pack "@earendil-works/pi-ai@${candidate}" --silent >/dev/null 2>&1 && tar xzf ./*.tgz); then
        echo "prepare.sh: candidate ${candidate} could not be fetched"
        continue
    fi
    if [ ! -d /tmp/pi-ai-data/package/dist/providers/data ]; then
        echo "prepare.sh: candidate ${candidate} ships no provider data"
        continue
    fi
    rm -rf packages/ai/src/providers/data
    cp -r /tmp/pi-ai-data/package/dist/providers/data packages/ai/src/providers/data
    if ! npm --prefix packages/ai run check:model-data >"/tmp/pi-check-${candidate}.log" 2>&1; then
        echo "prepare.sh: candidate ${candidate} rejected by check:model-data"
        continue
    fi
    if npm run build:offline >"/tmp/pi-build-${candidate}.log" 2>&1; then
        model_data_source="@earendil-works/pi-ai@${candidate}"
        break
    fi
    echo "prepare.sh: candidate ${candidate} failed build:offline:"
    grep -m 3 -E "error TS|npm error code" "/tmp/pi-build-${candidate}.log" || true
done
rm -rf /tmp/pi-ai-data

if [ -z "${model_data_source}" ]; then
    echo "prepare.sh: no published artifact worked; hydrating model data from live provider APIs"
    rm -rf packages/ai/src/providers/data
    if npm run hydrate:model-data >/tmp/pi-hydrate.log 2>&1 \
        && npm run build:offline >/tmp/pi-build-hydrate.log 2>&1; then
        model_data_source="live hydrate:model-data"
    else
        echo "prepare.sh: model data could not be reconstructed for ${pi_ai_version}" >&2
        echo "--- hydrate:model-data ---" >&2
        tail -n 25 /tmp/pi-hydrate.log >&2 || true
        echo "--- build:offline ---" >&2
        tail -n 25 /tmp/pi-build-hydrate.log >&2 || true
        exit 1
    fi
fi
echo "prepare.sh: model data from ${model_data_source}"

test -x node_modules/.bin/vitest
test -f packages/ai/dist/index.js
node --input-type=module -e "await import('@earendil-works/pi-ai'); console.log('DEPS_OK')"
"""


class PiImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base-node22"

    def workdir(self) -> str:
        return "base-node22"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{_MITM_PROXY_ARGS}

{_BASE_ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_MITM_CERT_SYMLINKS}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class PiImageDefault(Image):
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
        return PiImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        targets: dict[str, list[str]] = {}
        for path in re.findall(
            r"^diff --git a/(.+?) b/", f"{self.pr.test_patch}", re.MULTILINE
        ):
            if "/test/" not in path:
                continue
            if not re.search(r"\.(test|spec)\.(ts|mts|js|mjs)$", path):
                continue
            pkg, rel = path.split("/test/", 1)
            targets.setdefault(pkg, [])
            if f"test/{rel}" not in targets[pkg]:
                targets[pkg].append(f"test/{rel}")

        test_calls = "\n".join(
            f"run_package {pkg} " + " ".join(files)
            for pkg, files in sorted(targets.items())
        )

        test_command = """rc=0

run_package() {{
    local pkg="$1"
    shift
    local present=()
    local f
    for f in "$@"; do
        if [ -f "/home/{repo}/$pkg/$f" ]; then
            present+=("$f")
        fi
    done
    if [ "${{#present[@]}}" -eq 0 ]; then
        return 0
    fi
    cd "/home/{repo}/$pkg"
    local runner
    runner="$(node -p "((require('./package.json').scripts || {{}}).test) || ''")"
    if [[ "$runner" == *vitest* ]]; then
        echo "##PKG## $pkg"
        /home/{repo}/node_modules/.bin/vitest run --reporter=verbose --no-file-parallelism "${{present[@]}}" 2>&1 || rc=$?
    else
        for f in "${{present[@]}}"; do
            echo "##FILE## $pkg/$f"
            node --test --test-reporter=tap "$f" 2>&1 || rc=$?
        done
    fi
    cd /home/{repo}
}}

{test_calls}

exit $rc""".format(repo=self.pr.repo, test_calls=test_calls)

        return [
            File(".", "fix.patch", _filter_binary_patches(f"{self.pr.fix_patch}")),
            File(".", "test.patch", _filter_binary_patches(f"{self.pr.test_patch}")),
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
                _PREPARE_SH.replace("@@REPO@@", self.pr.repo).replace(
                    "@@SHA@@", self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}

{test_command}

""".format(repo=self.pr.repo, test_command=test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

{test_command}

""".format(repo=self.pr.repo, test_command=test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_command}

""".format(repo=self.pr.repo, test_command=test_command),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        sections = [
            f"FROM {dep.image_name()}:{dep.image_tag()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "".join(f"COPY {file.name} /home/\n" for file in self.files()).rstrip("\n")
        )

        sections.append("RUN bash /home/prepare.sh")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        sections.append(_HARDENING_BLOCK)
        sections.append(_SUBMODULE_SCRUB_BLOCK)

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("earendil-works", "pi_11347_to_8361")
class PI_11347_TO_8361(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PiImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        re_pkg = re.compile(r"^##PKG## (\S+)\s*$")
        re_file = re.compile(r"^##FILE## (\S+)\s*$")
        re_vitest = re.compile(r"^\s*([✓✔×✗↓])\s+(\S+\.(?:ts|mts|js|mjs) > .+?)\s*$")
        re_suite_fail = re.compile(r"^\s*FAIL\s+(\S+\.(?:ts|mts|js|mjs))\s+\[ .+ \]\s*$")
        re_timing = re.compile(r"\s+\d+(?:\.\d+)?(?:ms|s)$")
        re_vitest_tag = re.compile(r"\s+\[(?:skipped|todo)\]$")
        re_subtest = re.compile(r"^(\s*)# Subtest: (.*?)\s*$")
        re_point = re.compile(r"^(\s*)(ok|not ok) \d+(?: - (.*?))?\s*$")
        re_directive = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)
        re_yaml_open = re.compile(r"^(\s*)---\s*$")
        re_yaml_close = re.compile(r"^\s*\.\.\.\s*$")

        mode = ""
        prefix = ""
        stack: list[list] = []
        in_yaml = False
        yaml_indent = 0

        for raw in clean_log.splitlines():
            line = raw.rstrip()

            m = re_pkg.match(line)
            if m:
                mode, prefix, stack, in_yaml = "vitest", m.group(1), [], False
                continue
            m = re_file.match(line)
            if m:
                mode, prefix, stack, in_yaml = "tap", m.group(1), [], False
                continue

            if mode == "vitest":
                m = re_vitest.match(line)
                if m:
                    mark, name = m.groups()
                    name = re_vitest_tag.sub("", re_timing.sub("", name))
                    name = f"{prefix}/{name}"
                    if mark in "✓✔":
                        passed_tests.add(name)
                    elif mark in "×✗":
                        failed_tests.add(name)
                    else:
                        skipped_tests.add(name)
                    continue
                m = re_suite_fail.match(line)
                if m:
                    failed_tests.add(f"{prefix}/{m.group(1)}")
                continue

            if mode != "tap":
                continue

            if in_yaml:
                if re_yaml_close.match(line) and len(line) - len(line.lstrip()) <= yaml_indent:
                    in_yaml = False
                continue
            m = re_yaml_open.match(line)
            if m:
                in_yaml = True
                yaml_indent = len(m.group(1))
                continue

            m = re_subtest.match(line)
            if m:
                indent = len(m.group(1))
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                if stack:
                    stack[-1][2] = True
                stack.append([indent, m.group(2).replace("\\#", "#"), False])
                continue

            m = re_point.match(line)
            if not m:
                continue
            indent = len(m.group(1))
            status = m.group(2)
            title = m.group(3) or ""
            skip = re_directive.search(title)
            if skip:
                title = title[: skip.start()]
            title = title.strip().replace("\\#", "#")

            while stack and stack[-1][0] > indent:
                stack.pop()
            has_children = False
            if stack and stack[-1][0] == indent:
                has_children = stack[-1][2]
                stack.pop()
            if has_children and status == "ok":
                continue

            name = " > ".join([prefix] + [entry[1] for entry in stack] + [title])
            if skip:
                skipped_tests.add(name)
            elif status == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

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


Instance.register("earendil-works", "pi")(PI_11347_TO_8361)
