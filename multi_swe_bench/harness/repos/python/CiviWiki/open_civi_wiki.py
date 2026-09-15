import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_IMAGE = "python:3.10"


class OpenCiviWikiImageBase(Image):
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
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image}

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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN set -eux; \\
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/pki/ca-trust/extracted/pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/cert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cacert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN git -C /home clone "${{REPO_URL}}" {repo}

CMD ["/bin/bash"]
"""


class OpenCiviWikiImageDefault(Image):
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
        return OpenCiviWikiImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        sha = self.pr.base.sha

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  git status --porcelain\n"
            "  exit 1\n"
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f'BASE_COMMIT="${{BASE_COMMIT:-{sha}}}"\n'
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "python -m pip install --upgrade pip setuptools wheel || true\n"
            "if [ -f requirements.txt ]; then pip install -r requirements.txt || true; fi\n"
            "if [ -f requirements/base.txt ]; then pip install -r requirements/base.txt || true; fi\n"
            "if [ -f requirements/dev.txt ]; then pip install -r requirements/dev.txt || true; fi\n"
            "if [ -f requirements/test.txt ]; then pip install -r requirements/test.txt || true; fi\n"
            "git checkout -- .\n"
            "bash /home/check_git_changes.sh\n"
        )

        manage_resolver = (
            "if [ -f manage.py ]; then\n"
            "  MANAGE_DIR=.\n"
            "elif [ -f project/manage.py ]; then\n"
            "  MANAGE_DIR=project\n"
            "else\n"
            '  echo "manage.py not found" >&2; exit 1\n'
            "fi\n"
        )

        django_env = (
            "export CI=true\n"
            "export DJANGO_SETTINGS_MODULE=project.core.settings\n"
            "export SECRET_KEY=ci-dummy-secret-key\n"
            f"export PYTHONPATH=/home/{repo}\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"{django_env}"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfdx -e '__pycache__'\n"
            f"{manage_resolver}"
            'cd "$MANAGE_DIR"\n'
            "python manage.py test --verbosity=2 --noinput\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"{django_env}"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfdx -e '__pycache__'\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{manage_resolver}"
            'cd "$MANAGE_DIR"\n'
            "python manage.py test --verbosity=2 --noinput\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"{django_env}"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfdx -e '__pycache__'\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{manage_resolver}"
            'cd "$MANAGE_DIR"\n'
            "python manage.py test --verbosity=2 --noinput\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

{copy_commands}
RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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

RUN bash /home/prepare.sh
"""


@Instance.register("CiviWiki", "OpenCiviWiki")
class OpenCiviWiki(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenCiviWikiImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        line_re = re.compile(
            r"^(?P<name>\S+\s*\([^)]+\))\s*\.\.\.\s*(?P<status>ok|OK|FAIL|ERROR|skipped.*|expected failure|unexpected success)\s*$"
        )
        summary_re = re.compile(
            r"^(?P<status>FAIL|ERROR):\s+(?P<name>\S+\s*\([^)]+\))"
        )

        def norm(name: str) -> str:
            return re.sub(r"\s+", " ", name).strip()

        for raw in test_log.splitlines():
            line = raw.rstrip()
            m = line_re.match(line.strip())
            if m:
                name = norm(m.group("name"))
                status = m.group("status").lower()
                if status in ("ok",):
                    if name in failed_tests:
                        continue
                    skipped_tests.discard(name)
                    passed_tests.add(name)
                elif status in ("fail", "error"):
                    passed_tests.discard(name)
                    skipped_tests.discard(name)
                    failed_tests.add(name)
                elif status.startswith("skipped") or status == "expected failure":
                    if name in passed_tests or name in failed_tests:
                        continue
                    skipped_tests.add(name)
                elif status == "unexpected success":
                    passed_tests.discard(name)
                    skipped_tests.discard(name)
                    failed_tests.add(name)
                continue

            m2 = summary_re.match(line.strip())
            if m2:
                name = norm(m2.group("name"))
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
