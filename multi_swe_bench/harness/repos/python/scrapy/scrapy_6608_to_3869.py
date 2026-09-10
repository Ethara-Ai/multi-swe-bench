import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "/home/scrapy"

# Do NOT add `-o addopts=`: scrapy's pytest.ini addopts are load-bearing.
# --continue-on-collection-errors is required or pr-6608 aborts the whole session.
TEST_COMMAND = (
    "python -m pytest -v -p no:cacheprovider "
    "--continue-on-collection-errors tests"
)

# build-essential is required: pr-3988..5190 pin leveldb (C++, no wheels), and a
# failed build there drops every other test dep in the same all-or-nothing pip run.
_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM python:3.9-slim

ARG TARGETARCH
ARG REPO_URL="https://github.com/scrapy/scrapy.git"
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
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="scrapy/scrapy" \\
      org.opencontainers.image.description="scrapy/scrapy Docker image" \\
      org.opencontainers.image.source="https://github.com/scrapy/scrapy" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    libxml2-dev \\
    libxslt1-dev \\
    zlib1g-dev \\
    libffi-dev \\
    libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/scrapy

CMD ["/bin/bash"]
"""

# Twisted 21.7.0 is the ONLY version that works across the whole range. Twisted 22.4.0
# removed twisted.web.client.HTTPClientFactory, which pr-3869..4686 import; pr-6608
# declares Twisted>=21.7.0. Those two bounds meet at exactly 21.7.0.
# pyOpenSSL 22.0.0 likewise: 22.1.0 removed SSL.SSLv3_METHOD (used by pre-2021 scrapy)
# and pr-6608 declares pyOpenSSL>=22.0.0.
# parsel 1.7.0: 1.8.1 removed Selector._default_type, which scrapy<=2.11 calls in
# selector/unified.py -- unpinned it raised AttributeError 466x per run, silently failing
# graded tests. scrapy declares only parsel>=1.5.0 and skips its own jmespath tests on
# parsel<1.8, so 1.7.0 is a configuration scrapy explicitly supports.
# Twisted[http2] (not bare Twisted): scrapy>=2.5 declares Twisted[http2] and gates 153 of its
# HTTP/2 tests on the h2 package. pr-6097's test patch targets tests/test_http2_client_protocol.py,
# so without the extra its 26 graded tests SKIP and the instance is invalid. Verified they pass
# offline (--network=none) and that the extra is a no-op on the pre-2.5 PRs.
# setuptools==70.0.0 (not just <81): 81 removed pkg_resources which the pre-2021 setup.py
# imports, but >=71 also emits a pkg_resources DeprecationWarning that parsel 1.7.0 triggers
# on import. That warning lands in command output and breaks the 6 tests in test_command_check
# / test_spider / test_crawl that assert on exact output. 70.0.0 has pkg_resources, no warning,
# and still satisfies pr-6608's build-backend requirement of setuptools>=61.
# urwid<2.1.2 uses use_2to3, removed in setuptools 58; it arrives via bpython, and a
# `pip install -r` is all-or-nothing, so it would silently drop every other test dep.
# The optional-extras install keeps `|| true` (bpython/mitmproxy/aiohttp may legitimately
# fail); the hard gate below is what makes that safe -- it imports the test-only deps the
# suite needs at collection time (sybil and pyftpdlib are deliberately NOT gated -- they are
# absent from the requirements-py3.txt era and a uniform assert on them would break those
# builds), so a partial install fails the BUILD instead of shipping
# an image whose three stages all fail identically and discard a sound PR.
# PIP_CONSTRAINT is what makes these pins bite -- pip's isolated build env installs its
# OWN setuptools, so without it `pip install -e .` fails on every pre-2021 PR.
_PREPARE_SH = """#!/bin/bash
set -e
cd /home/scrapy

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

printf 'setuptools==70.0.0\nTwisted==21.7.0\npyOpenSSL==22.0.0\ncryptography==38.0.4\nparsel==1.7.0\nurwid>=2.1.2\n' > /opt/constraints.txt
export PIP_CONSTRAINT=/opt/constraints.txt

python -m pip install --no-cache-dir --upgrade pip wheel || true
python -m pip install --no-cache-dir --upgrade "setuptools==70.0.0" || true

python -m pip install --no-cache-dir "Twisted[http2]==21.7.0" "pyOpenSSL==22.0.0" "cryptography==38.0.4" "parsel==1.7.0" || true

if [ -f tests/requirements-py3.txt ]; then
  python -m pip install --no-cache-dir -r tests/requirements-py3.txt || true
elif [ -f tests/requirements.txt ]; then
  python -m pip install --no-cache-dir -r tests/requirements.txt || true
else
  python -m pip install --no-cache-dir \\
    attrs pytest pytest-cov pytest-xdist sybil testfixtures pyftpdlib pexpect pygments || true
fi

python -m pip install --no-cache-dir -e .

python -c "import scrapy, scrapy.cmdline, pytest, parsel, xdist, pexpect, pygments, lxml, w3lib, queuelib, testfixtures, twisted, OpenSSL, cryptography; assert twisted.__version__ == '21.7.0', twisted.__version__; assert OpenSSL.__version__ == '22.0.0', OpenSSL.__version__; assert parsel.__version__ == '1.7.0', parsel.__version__; print('DEPS_OK', scrapy.__version__, pytest.__version__)"
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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


class ScrapyImageBase(Image):
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-6608_to_3869"

    def workdir(self) -> str:
        return "base-6608_to_3869"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return []

    def extra_setup(self) -> str:
        return ""

    def dockerfile(self) -> str:
        return _BASE_DOCKERFILE


class ScrapyImageDefault(Image):
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
        return ScrapyImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.replace("__BASE_SHA__", self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "export CI=true\n"
                f"cd {REPO_DIR}\n"
                "\n"
                f"{TEST_COMMAND}\n",
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "export CI=true\n"
                f"cd {REPO_DIR}\n"
                "\n"
                "git apply --whitespace=nowarn /home/test.patch\n"
                "\n"
                f"{TEST_COMMAND}\n",
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "export CI=true\n"
                f"cd {REPO_DIR}\n"
                "\n"
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                "\n"
                f"{TEST_COMMAND}\n",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n"
            "\n"
            f"{copy_commands}"
            "\n"
            "RUN bash /home/prepare.sh\n"
            "\n"
            "RUN set -eux; \\\n"
            f"    cd {REPO_DIR}; \\\n"
            f'    test "$(git rev-parse HEAD)" = "{sha}"; \\\n'
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all; \\\n"
            "    git reflog expire --expire-unreachable=now --all; \\\n"
            "    git gc --prune=now --aggressive; \\\n"
            "    git repack -a -d -l --quiet; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            '    git config --local remote.pushDefault ""; \\\n'
            '    test -z "$(git remote)"; \\\n'
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\\n'
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"\n'
            "\n"
            f"RUN if [ -f {REPO_DIR}/.gitmodules ]; then \\\n"
            f"        cd {REPO_DIR} && git submodule foreach --recursive ' \\\n"
            "            git checkout --detach HEAD; \\\n"
            "            git remote remove origin 2>/dev/null || true; \\\n"
            '            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\\n'
            "                | xargs -r -n1 git update-ref -d; \\\n"
            "            git reflog expire --expire=now --all; \\\n"
            "            git reflog expire --expire-unreachable=now --all; \\\n"
            "            git gc --prune=now --aggressive; \\\n"
            "            rm -f .git/objects/info/alternates; \\\n"
            "        '; \\\n"
            "    fi\n"
        )


@Instance.register("scrapy", "scrapy_6608_to_3869")
class SCRAPY_6608_TO_3869(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ScrapyImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        line_re = re.compile(
            r"^(?P<name>\S.*?::.+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b.*$"
        )

        for raw_line in log.split("\n"):
            match = line_re.match(raw_line.rstrip())
            if not match:
                continue
            name = match.group("name").strip()
            status = match.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

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
