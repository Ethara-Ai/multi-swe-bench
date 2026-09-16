import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

UBUNTU_IMAGE = "ubuntu:20.04"
_BASE_APT = (
    "build-essential ca-certificates curl dssp git "
    "python3 python3-dev python3-pip zlib1g-dev"
)

_PIP_PINS = "pip==23.3.2 setuptools==68.0.0 wheel==0.42.0"
_BUILD_PINS = "pbr==6.0.0"
_ERA_PINS = " ".join(
    [
        "numpy==1.24.4",
        "scipy==1.10.1",
        "networkx==3.1",
        "attrs==23.1.0",
        "pytest==7.4.3",
        "pytest-cov==4.1.0",
        "coverage==7.3.2",
        "hypothesis==6.92.1",
        "hypothesis-networkx==0.3.0",
        "astunparse==1.6.3",
        "pyparsing==3.1.1",
    ]
)
_MDTRAJ_PIN = "mdtraj==1.9.9"

_PYTEST_CMD = "python3 -m pytest vermouth -vv -rA --color=no -p no:cacheprovider"


class VermouthMartinizeImageBase(Image):
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
        return UBUNTU_IMAGE

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
    TZ=UTC \\
    VERMOUTH_TEST_DSSP=mkdssp \\
    SKIP_GENERATE_AUTHORS=1 \\
    SKIP_WRITE_GIT_CHANGELOG=1 \\
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

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_BASE_APT} \\
    && rm -rf /var/lib/apt/lists/*

RUN if [ -x /usr/bin/mkdssp ] && [ ! -e /usr/bin/dssp ]; then ln -sf mkdssp /usr/bin/dssp; fi && \\
    if [ -x /usr/bin/dssp ] && [ ! -e /usr/bin/mkdssp ]; then ln -sf dssp /usr/bin/mkdssp; fi && \\
    command -v mkdssp && command -v dssp

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class VermouthMartinizeImageDefault(Image):
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
        return VermouthMartinizeImageBase(self.pr, self._config)

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
            "set -euo pipefail\n"
            f'BASE_COMMIT="${{BASE_COMMIT:-{sha}}}"\n'
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            f"python3 -m pip install --upgrade {_PIP_PINS}\n"
            f"python3 -m pip install {_BUILD_PINS}\n"
            f"python3 -m pip install {_ERA_PINS}\n"
            f"python3 -m pip install {_MDTRAJ_PIN}\n"
            "python3 -m pip install -e . --no-deps --no-build-isolation\n"
            "command -v mkdssp\n"
            "command -v martinize2\n"
            "python3 - <<'DEPS_GATE'\n"
            "import attrs, coverage, hypothesis, hypothesis_networkx\n"
            "import networkx, numpy, pytest, scipy\n"
            "import mdtraj\n"
            "import vermouth\n"
            "from vermouth.dssp import dssp\n"
            "from vermouth.forcefield import get_native_force_field\n"
            "mdtraj.compute_dssp\n"
            "print('DEPS_OK')\n"
            "DEPS_GATE\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            f"{_PYTEST_CMD}\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_PYTEST_CMD}\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_PYTEST_CMD}\n"
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

{copy_commands}
RUN bash /home/prepare.sh

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
"""


@Instance.register("marrink-lab", "vermouth-martinize")
class VermouthMartinize(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return VermouthMartinizeImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_verbose = re.compile(
            r"^(\S+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        re_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(\S+?::.+?)(?:\s+-\s.*)?$"
        )

        def record(name: str, status: str) -> None:
            if status in ("FAILED", "ERROR"):
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status in ("PASSED", "XPASS"):
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            else:
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        for line in test_log.splitlines():
            line = line.strip()
            m = re_verbose.match(line)
            if m:
                record(m.group(1), m.group(2))
            m = re_summary.match(line)
            if m:
                record(m.group(2), m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
