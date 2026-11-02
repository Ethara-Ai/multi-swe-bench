import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        return "node:24-bookworm"

    def image_tag(self) -> str:
        return "base-17293_to_16635"

    def workdir(self) -> str:
        return "base-17293_to_16635"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

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
ARG BUN_VERSION="bun-v1.4.0"

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
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    COREPACK_ENABLE_STRICT=0 \\
    BUN_INSTALL=/usr/local \\
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \\
    FORCE_COLOR=0 \\
    NO_COLOR=1 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    CI=true

{self.global_env}

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
        git ca-certificates curl unzip python3 make g++ pkg-config \\
        fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \\
        libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 \\
        libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 \\
        libxext6 libxfixes3 libxkbcommon0 libxrandr2 libxshmfence1 xvfb \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN curl -fsSL https://bun.sh/install | bash -s -- ${{BUN_VERSION}}

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null

{self.clear_env}

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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def run_env(self) -> str:
        return """export CI=true
export TZ=UTC
export FORCE_COLOR=0
export NO_COLOR=1
export NODE_OPTIONS=--max-old-space-size=4096
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export COREPACK_ENABLE_STRICT=0
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export npm_config_reporter=append-only
export npm_config_update_notifier=false
export KIT_E2E_RETRIES=0
export KIT_E2E_WORKERS=2"""

    def test_command(self) -> str:
        return """for utils in packages/*/test/utils.js; do
  if [ -f "$utils" ]; then
    sed -i -e 's/retries: process[.]env[.]CI /retries: false /' \\
           -e 's/reporter: process[.]env[.]CI$/reporter: false/' \\
           -e "/channel:/ s/'chrome'/'chromium'/" "$utils"
  fi
done

if [ -f pnpm-workspace.yaml ]; then
  if grep -q '^verifyDepsBeforeRun:' pnpm-workspace.yaml; then
    sed -i 's/^verifyDepsBeforeRun:.*/verifyDepsBeforeRun: false/' pnpm-workspace.yaml
  else
    printf '\\nverifyDepsBeforeRun: false\\n' >> pnpm-workspace.yaml
  fi
fi

status=0

pnpm install --no-frozen-lockfile || status=1
pnpm run sync-all || status=1
pnpm exec vitest run --reporter=verbose || status=1
pnpm run --dir packages/kit test:unit --reporter=verbose || status=1
pnpm -r --no-bail --workspace-concurrency=1 --filter='!@sveltejs/kit' run test:unit --reporter=verbose || status=1
pnpm -r --no-bail --workspace-concurrency=1 --filter='!@sveltejs/kit' run test:dev || status=1
pnpm -r --no-bail --workspace-concurrency=1 --filter='!@sveltejs/kit' run test:build || status=1
pnpm -r --no-bail --workspace-concurrency=1 --filter='!@sveltejs/kit' run test || status=1

exit $status"""

    def patch_excludes(self) -> str:
        return (
            "--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' "
            "--exclude='*.gif' --exclude='*.ico' --exclude='*.webp' "
            "--exclude='*.avif' --exclude='*.woff' --exclude='*.woff2' "
            "--exclude='*.ttf' --exclude='*.otf' --exclude='*.eot' "
            "--exclude='*.mp4' --exclude='*.webm' --exclude='*.pdf' "
            "--exclude='*.zip'"
        )

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -euo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
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

cd {repo_dir}

git reset --hard
git clean -fdx -e node_modules
bash /home/check_git_changes.sh

git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

{self.run_env()}

corepack enable pnpm
corepack install

pnpm install --frozen-lockfile

pnpm -F @sveltejs/kit prepublishOnly

unset PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD
pnpm exec playwright install-deps chromium || true
pnpm exec playwright install chromium

pnpm --version
bun --version
pnpm exec playwright --version
(cd packages/kit && pnpm exec vitest --version)
node -e "import('@playwright/test').then(async (pw) => {{ const browser = await pw.chromium.launch(); await browser.close(); console.log('DEPS_OK'); }})"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {repo_dir}

{self.run_env()}

{self.test_command()}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {repo_dir}

{self.run_env()}

git apply --whitespace=nowarn {self.patch_excludes()} /home/test.patch

{self.test_command()}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {repo_dir}

{self.run_env()}

git apply --whitespace=nowarn {self.patch_excludes()} /home/test.patch /home/fix.patch

{self.test_command()}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
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


@Instance.register("sveltejs", "sveltejs_17293_to_16635")
class SVELTEJS_17293_TO_16635(Instance):
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
        repeat_passed: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        clean_log = clean_log.replace("\r\n", "\n").replace("\r", "\n")
        repo_prefix = f"/home/{self.pr.repo}/"

        pnpm_prefix_re = re.compile(
            r"^(?P<pkg>[@A-Za-z0-9][A-Za-z0-9@/._-]*) "
            r"(?P<script>(?:pre|post)?test(?::[A-Za-z0-9._-]+)*): "
            r"(?P<rest>.*)$"
        )
        playwright_re = re.compile(
            r"^\s*(?P<marker>[✓✔✘✗×✖−-])\s+\d+\s+(?P<body>\S.*)$"
        )
        vitest_re = re.compile(
            r"^\s*(?P<marker>[✓✔√×✗✘↓❯])\s+(?P<body>\S.*)$"
        )
        bun_re = re.compile(
            r"^\s*\((?P<status>pass|fail|skip|todo)\)\s+(?P<body>\S.*)$"
        )
        fail_line_re = re.compile(r"^\s*FAIL\s+(?P<body>\S.*)$")

        location_re = re.compile(r":\d+:\d+(?= > |$)")
        counts_re = re.compile(r"\s*\((?:\d+\s+[A-Za-z]+(?:\s*\|\s*)?)+\)\s*$")
        retry_re = re.compile(
            r"\s*\((?:retry\s*#?x?\d+|flaky|repeat\s*#?\d+)\)\s*$", re.IGNORECASE
        )
        bracket_duration_re = re.compile(
            r"\s*\[\d+(?:[.,]\d+)?\s*(?:ms|s|m|us|µs|ns)\]\s*$"
        )
        duration_re = re.compile(r"\s*\(?\d+(?:[.,]\d+)?\s*(?:ms|s|m)\)?\s*$")
        file_re = re.compile(r"\.(?:ts|tsx|js|jsx|mjs|cjs|mts|cts|svelte)$")

        def relative(text: str) -> str:
            return text.replace(repo_prefix, "")

        def normalize(body: str) -> str:
            body = relative(body)
            body = body.replace(" › ", " > ").replace("›", ">")
            body = location_re.sub("", body)
            for _ in range(3):
                body = counts_re.sub("", body)
                body = retry_re.sub("", body)
                body = bracket_duration_re.sub("", body)
                body = duration_re.sub("", body)
            body = re.sub(r"[\s─│·.]+$", "", body)
            return re.sub(r"\s+", " ", body).strip()

        def scope(pkg: str, name: str) -> str:
            if not name:
                return ""
            if pkg:
                return f"{pkg} > {name}"
            return name

        current_file: dict[str, str] = {}

        for raw_line in clean_log.splitlines():
            pkg = ""
            line = raw_line
            prefix_match = pnpm_prefix_re.match(raw_line)
            if prefix_match:
                pkg = prefix_match.group("pkg")
                line = prefix_match.group("rest")

            bun_match = bun_re.match(line)
            if bun_match:
                name = scope(pkg, normalize(bun_match.group("body")))
                if not name:
                    continue
                status = bun_match.group("status")
                if status == "pass":
                    passed_tests.add(name)
                elif status == "fail":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            playwright_match = playwright_re.match(line)
            if playwright_match:
                name = scope(pkg, normalize(playwright_match.group("body")))
                if not name:
                    continue
                marker = playwright_match.group("marker")
                if marker in ("✓", "✔"):
                    passed_tests.add(name)
                    repeat_passed.add(name)
                elif marker in ("✘", "✗", "×", "✖"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            vitest_match = vitest_re.match(line)
            if vitest_match:
                marker = vitest_match.group("marker")
                body = normalize(vitest_match.group("body"))
                if not body:
                    continue

                head = body.split(" > ")[0]
                if file_re.search(head) and " > " not in body:
                    current_file[pkg] = head
                    continue

                if file_re.search(head):
                    name = scope(pkg, body)
                elif current_file.get(pkg):
                    name = scope(pkg, f"{current_file[pkg]} > {body}")
                else:
                    name = scope(pkg, body)

                if marker in ("✓", "✔", "√"):
                    passed_tests.add(name)
                elif marker in ("×", "✗", "✘"):
                    failed_tests.add(name)
                elif marker == "↓":
                    skipped_tests.add(name)
                continue

            fail_match = fail_line_re.match(line)
            if fail_match:
                body = normalize(fail_match.group("body"))
                if " > " in body and file_re.search(body.split(" > ")[0]):
                    failed_tests.add(scope(pkg, body))

        failed_tests -= repeat_passed
        skipped_tests -= repeat_passed

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


Instance.register("sveltejs", "kit")(SVELTEJS_17293_TO_16635)
