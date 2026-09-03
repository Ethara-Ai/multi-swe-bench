import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PycyphalImageBase(Image):
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
        return "python:3.11-slim"

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

        if self.config.need_clone:
            code = f'RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}'
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




WORKDIR /home/

RUN DEBIAN_FRONTEND=noninteractive apt-get update && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential libsocketcan-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}



CMD ["/bin/bash"]
"""


class PycyphalImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return PycyphalImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
cd /home/{pr.repo}
echo "== git HEAD =="
git rev-parse HEAD
echo "== git status =="
git status --short
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# libpcap headers for the transport-udp extra; iproute2/kmod so conftest's ip/modprobe calls resolve
apt-get update -qq && apt-get install -y -qq --no-install-recommends libpcap-dev iproute2 kmod

# tests/conftest.py's session fixture runs `sudo modprobe can/vcan` + `sudo ip link add vcan0`,
# which need kernel-level privileges Docker can't grant. Shim `sudo` so those calls no-op
# instead of aborting every test with CalledProcessError. Other sudo invocations still work.
cat > /usr/local/bin/sudo <<'SUDO_EOF'
#!/bin/bash
case "$1" in modprobe|ip) exit 0 ;; esac
exec "$@"
SUDO_EOF
chmod +x /usr/local/bin/sudo

# Public regulated DSDL types live in a submodule; the base image clone leaves it uninitialized
git config --global --add safe.directory /home/{pr.repo}
git submodule update --init --recursive

# Newer pydsdl (installed transitively via nunavut) rejects non-deprecated types that depend
# on deprecated types. A few test-fixture .dsdl files in this repo pre-date that stricter check
# and trigger AggregationError, cascading into ~55 test errors that mask the real fix/no-fix signal.
# Strip @deprecated from test DSDL fixtures so compilation completes and tests actually run.
find tests/dsdl/ -name "*.dsdl" -exec sed -i "/^@deprecated/d" {{}} +

pip install --upgrade pip
pip install -e '.[transport-can-pythoncan,transport-serial,transport-udp]' || pip install -e . || true
pip install 'pytest>=7.0' pytest-timeout pytest-asyncio || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONASYNCIODEBUG=1

cd /home/{pr.repo}
pytest -v --tb=short --continue-on-collection-errors -o addopts= tests/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONASYNCIODEBUG=1

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
pytest -v --tb=short --continue-on-collection-errors -o addopts= tests/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONASYNCIODEBUG=1

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
pip install -e '.[test]' 2>&1 | tail -5 || true
pytest -v --tb=short --continue-on-collection-errors -o addopts= tests/

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening_block = f"""RUN set -eux; \\
    git checkout --detach "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{self.pr.base.sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)" """

        submodule_scrub = """RUN if [ -f .gitmodules ]; then \\
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
    fi"""

        return f"""FROM {name}:{tag}



WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}

{hardening_block}

{submodule_scrub}

RUN bash /home/prepare.sh

"""


@Instance.register("OpenCyphal", "pycyphal")
class Pycyphal(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PycyphalImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"^PASSED\s+(\S+)"),
            re.compile(r"^(\S+)\s+PASSED"),
            re.compile(r"^(\S+::\S+)\s+PASSED"),
        ]
        re_fail_tests = [
            re.compile(r"^FAILED\s+(\S+)"),
            re.compile(r"^(\S+)\s+FAILED"),
            re.compile(r"^ERROR\s+(\S+)"),
            re.compile(r"^(\S+)\s+ERROR"),
        ]
        re_skip_tests = [
            re.compile(r"^SKIPPED\s+(\S+)"),
            re.compile(r"^(\S+)\s+SKIPPED"),
        ]

        def _is_test_id(name: str) -> bool:
            # Reject pytest banners (___ ERROR ___), doctest addresses (mod:file.py:line),
            # bracket fragments, and anything that isn't a proper node ID.
            if not name or name[0].isdigit():
                return False
            if set(name) <= set("_[]"):
                return False
            return "::" in name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                m = re_pass_test.match(line)
                if m:
                    name = m.group(1)
                    if _is_test_id(name):
                        passed_tests.add(name)

            for re_fail_test in re_fail_tests:
                m = re_fail_test.match(line)
                if m:
                    name = m.group(1)
                    if _is_test_id(name):
                        failed_tests.add(name)

            for re_skip_test in re_skip_tests:
                m = re_skip_test.match(line)
                if m:
                    name = m.group(1)
                    if _is_test_id(name):
                        skipped_tests.add(name)

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


# Extra per-PR keys so build_dataset can consume the gen_report-produced
# generated jsonl directly (that file sets number_interval='<N>' on each
# record; Instance.create() then looks up f"{org}/{number_interval}" and
# would fail without these).
for _pr_num in ("235", "280", "322", "362"):
    Instance.register("OpenCyphal", _pr_num)(Pycyphal)
