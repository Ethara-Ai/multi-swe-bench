import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


NODE_IMAGE_8 = "node:18-bullseye"
NODE_IMAGE_9 = "node:20-bookworm"
ERA_BOUNDARY = 11804

CHECK_GIT_CHANGES = r"""#!/bin/bash
set -euo pipefail

cd "$1"

if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


def _base_tag(pr: PullRequest) -> str:
    return "base-8" if pr.number <= ERA_BOUNDARY else "base-9"


def _node_image(pr: PullRequest) -> str:
    return NODE_IMAGE_8 if pr.number <= ERA_BOUNDARY else NODE_IMAGE_9


class SvelteImageBase(Image):
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
        return _node_image(self.pr)

    def image_tag(self) -> str:
        return _base_tag(self.pr)

    def workdir(self) -> str:
        return _base_tag(self.pr)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

{self.global_env}

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

RUN corepack enable || npm install -g pnpm@8 yarn || true

WORKDIR /home/

{clone}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


def _test_body(repo: str) -> str:
    repo_dir = f"/home/{repo}"
    return f"""export CI=true
export npm_config_update_notifier=false

cd "{repo_dir}"

if [ -f pnpm-lock.yaml ]; then
  pnpm test || pnpm test -- --reporter verbose || true
elif [ -f yarn.lock ]; then
  yarn test || true
else
  npm test || npm test -- --verbose || true
fi
"""


class SvelteImageDefault(Image):
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
        return SvelteImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"

        prepare = "#!/bin/bash\n"
        prepare += "set -euo pipefail\n"
        prepare += "\n"
        prepare += f'REPO_DIR="{repo_dir}"\n'
        prepare += "\n"
        prepare += f"cd {repo_dir}\n"
        prepare += "\n"
        prepare += "git reset --hard\n"
        prepare += "git clean -fdx\n"
        prepare += f"bash /home/check_git_changes.sh {repo_dir}\n"
        prepare += "\n"
        prepare += f"git checkout --detach {self.pr.base.sha}\n"
        prepare += f"bash /home/check_git_changes.sh {repo_dir}\n"
        prepare += "\n"
        prepare += "if [ -f pnpm-lock.yaml ]; then\n"
        prepare += "  corepack enable || npm install -g pnpm@8 || true\n"
        prepare += "  pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile || true\n"
        prepare += "elif [ -f yarn.lock ]; then\n"
        prepare += "  corepack enable || npm install -g yarn || true\n"
        prepare += "  yarn install --frozen-lockfile || yarn install || true\n"
        prepare += "elif [ -f package-lock.json ]; then\n"
        prepare += "  npm ci || npm install || true\n"
        prepare += "else\n"
        prepare += "  npm install || true\n"
        prepare += "fi\n"
        prepare += "\n"
        prepare += "node -e \"require('fs').accessSync('package.json'); console.log('DEPS_OK')\"\n"

        run = "#!/bin/bash\n"
        run += "set -eo pipefail\n"
        run += "\n"
        run += _test_body(self.pr.repo)

        test_run = "#!/bin/bash\n"
        test_run += "set -eo pipefail\n"
        test_run += "\n"
        test_run += f'cd "{repo_dir}"\n'
        test_run += "git apply --whitespace=nowarn /home/test.patch\n"
        test_run += "\n"
        test_run += _test_body(self.pr.repo)

        fix_run = "#!/bin/bash\n"
        fix_run += "set -eo pipefail\n"
        fix_run += "\n"
        fix_run += f'cd "{repo_dir}"\n'
        fix_run += "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
        fix_run += "\n"
        fix_run += _test_body(self.pr.repo)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
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

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

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

{self.clear_env}
"""


@Instance.register("sveltejs", "svelte")
class Svelte(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SvelteImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_vitest_pass = re.compile(r"^✓ (.+?)(?:\s*\d*\.?\d+\s*(?:ms|s|m))?$")
        re_vitest_fail = re.compile(r"^× (.+?)(?:\s*\d*\.?\d+\s*(?:ms|s|m))?$")

        re_mocha_summary = re.compile(r"^\d+ (?:passing|pending|failing)")
        re_mocha_inline_fail = re.compile(r"^\d+\)\s+(.+)")
        re_error_number = re.compile(r"^\d+\)\s+(.+)")

        lines = clean_log.splitlines()
        is_mocha = any(re_mocha_summary.match(l.strip()) for l in lines)

        if not is_mocha:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("✓"):
                    match = re_vitest_pass.match(line)
                    if match:
                        passed_tests.add(match.group(1))
                    else:
                        passed_tests.add(line.replace("✓", "").strip())
                    continue
                fail_match = re_vitest_fail.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
        else:
            current_suite = ""
            in_summary = False

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue

                if re_mocha_summary.match(stripped):
                    in_summary = True
                    continue

                if in_summary:
                    error_match = re_error_number.match(stripped)
                    if error_match:
                        current_suite = error_match.group(1)
                        continue
                    if stripped.endswith(":") and current_suite:
                        test_name = stripped.rstrip(":")
                        failed_tests.add(f"{current_suite} > {test_name}")
                        continue
                    continue

                raw_indent = len(line) - len(line.lstrip())

                if stripped.startswith("✓"):
                    match = re_vitest_pass.match(stripped)
                    name = match.group(1) if match else stripped.replace("✓", "").strip()
                    qualified = f"{current_suite} > {name}" if current_suite else name
                    passed_tests.add(qualified)
                    continue

                if stripped.startswith("- "):
                    name = stripped[2:].strip()
                    qualified = f"{current_suite} > {name}" if current_suite else name
                    skipped_tests.add(qualified)
                    continue

                if re_mocha_inline_fail.match(stripped):
                    continue

                if raw_indent <= 4 and not stripped.startswith("(") and not stripped.startswith("Error"):
                    if not any(c in stripped for c in [":", "/", "→", "!", ">"]):
                        current_suite = stripped

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            passed_tests=passed_tests,
            failed_count=len(failed_tests),
            failed_tests=failed_tests,
            skipped_count=len(skipped_tests),
            skipped_tests=skipped_tests,
        )
