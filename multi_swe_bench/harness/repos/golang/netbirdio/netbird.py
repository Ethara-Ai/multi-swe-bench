from __future__ import annotations

import json as _json
import os
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_netbird_number_interval_patched", False):
    _netbird_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _netbird_from_json(cls, json_str):
        pr = _netbird_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "netbirdio"
                and raw.get("repo") == "netbird"
                and raw.get("prs_in_bundle")
            ):
                pr._netbird_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_netbird_from_json)
    _pull_request.PullRequest._netbird_number_interval_patched = True

    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_netbird_build_patched", False):
        _netbird_orig_build = _Dataset.build.__func__

        def _netbird_build(cls, pr, report):
            ds = _netbird_orig_build(cls, pr, report)
            ni = getattr(pr, "_netbird_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_netbird_build)
        _Dataset._netbird_build_patched = True


_GO_IMAGE = "golang:1.26-bookworm"

_GO_TAG = "go1.26"

_APT_PACKAGES = [
    "git",
    "ca-certificates",
    "build-essential",
    "pkg-config",
    "libpcap-dev",
    "libx11-dev",
    "libgl1-mesa-dev",
    "libgtk-3-dev",
    "libayatana-appindicator3-dev",
]


def _test_packages(pr: PullRequest) -> str:
    dirs: set[str] = set()
    for patch in (pr.test_patch or "", pr.fix_patch or ""):
        for path in re.findall(r"^diff --git a/(\S+)", patch, re.M):
            if not path.endswith(".go"):
                continue
            directory = os.path.dirname(path)
            dirs.add(directory if directory else ".")

    if not dirs:
        return "./..."

    return " ".join(f"./{d}" for d in sorted(dirs))


def _go_test_cmd(pr: PullRequest) -> str:
    return f"go test -v -count=1 -timeout 1800s {_test_packages(pr)}"


_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
set -e

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
"""

_PREPARE_SH = """\
#!/bin/bash
set -e

# -------- Section 1: PIN --------
# cd explicitly rather than trusting the Dockerfile WORKDIR: prepare.sh is also
# invoked at container start, where that WORKDIR may not be in effect.
cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

# --detach is required, not stylistic. The hardening block in the Dockerfile
# ASSERTS HEAD == BASE_COMMIT and then deletes every ref; if HEAD were left on a
# branch, that branch would either be deleted out from under HEAD or keep the
# fix commit reachable inside the shipped image.
git checkout --detach {sha}
bash /home/check_git_changes.sh

# -------- Section 2: PROVISION --------
go version
go env GOVERSION GOTOOLCHAIN

# Pull the module graph into the image layer so the three graded stages never
# reach proxy.golang.org. `timeout` covers what `|| true` cannot: `|| true`
# handles a command that FAILS, but one that HANGS never returns and never
# reaches `||`, and Docker has no per-step timeout, so a stalled module fetch
# would block the build forever.
#
# RETRIED, and the retry is not defensive padding -- it is the fix for an
# observed failure. proxy.golang.org intermittently resets HTTP/2 streams
# mid-fetch:
#
#   go: github.com/oapi-codegen/runtime@v1.1.2: read
#   "https://proxy.golang.org/...": stream error: stream ID 425;
#   INTERNAL_ERROR; received from peer
#
# netbird pulls a very large module graph, so a single attempt is quite likely
# to lose at least one module -- and a PARTIAL cache is the worst outcome
# available: `go mod download` exits 1, then every scoped package reports
# `[setup failed]`, the baseline yields 0 results, and the gate below fails the
# whole image. Attempt 3 onward drops to HTTP/1.1 via GODEBUG=http2client=0,
# which is the documented workaround for this error class.
#
# This step and the build warm below are the ONLY tolerated ones, and they are
# tolerated solely because the Section 3 gate re-checks what they were supposed
# to accomplish. `if/else` rather than `|| true` so the reported exit status is
# the real one -- after `cmd || true`, `$?` is always 0 and the log would claim
# success for a step that failed.
for attempt in 1 2 3 4 5; do
    if [ "$attempt" -ge 3 ]; then
        export GODEBUG=http2client=0
        echo "go mod download: retrying over HTTP/1.1"
    fi
    if timeout 1800 go mod download; then
        echo "go mod download: OK (attempt $attempt)"
        break
    fi
    echo "go mod download: attempt $attempt INCOMPLETE"
    if [ "$attempt" -lt 5 ]; then
        sleep 20
    fi
done

# Warm the non-test build cache for the scoped packages. This also back-fills
# any module the download loop still missed.
if timeout 1800 go build {packages} > /tmp/build.log 2>&1; then
    echo "build warm: OK"
else
    echo "build warm: INCOMPLETE (exit $?)"
    tail -20 /tmp/build.log || true
fi

# -------- Section 3: GATE --------
# Run the baseline suite once, tolerantly, then assert on its OUTPUT. The run is
# tolerant on purpose: netbird's scoped sets legitimately contain packages that
# cannot compile for linux (client/android, client/ios/NetBirdSDK carry mobile
# build tags), and a non-zero exit from those must not fail the image build.
# Running it here also warms the test cache, so the three graded stages start
# from compiled package objects.
if timeout 5400 {go_test} > /tmp/baseline.log 2>&1; then
    echo "baseline suite: exit 0"
else
    echo "baseline suite: exit $? (expected when a scoped package cannot build)"
fi
echo "baseline package summary:"
grep -E '^(ok|FAIL|[?])' /tmp/baseline.log | head -40 || true
echo "baseline results: $(grep -cE '^[[:space:]]*--- (PASS|FAIL|SKIP): ' /tmp/baseline.log)"

# THE GATE -- last, and not tolerated. `^ok ` appears only when a test binary was
# compiled, linked and executed, so this fails loudly on a broken module graph, a
# missing C header or an unusable toolchain. A missing image is honest; a silently
# hollow one that reports 0/0/0 three stages later is not.
grep -qE '^ok ' /tmp/baseline.log
echo "DEPS_OK"
"""

_RUN_SH = """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{go_test}
"""

_TEST_RUN_SH = """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{go_test}
"""

_FIX_RUN_SH = """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{go_test}
"""


class NetbirdImageBase(Image):

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
        return _GO_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_GO_TAG}"

    def workdir(self) -> str:
        return f"base-{_GO_TAG}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        apt = " \\\n    ".join(_APT_PACKAGES)

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CGO_ENABLED=1 \\
    GOTOOLCHAIN=local \\
    GOFLAGS="-buildvcs=false -mod=mod"

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
    {apt} \\
    && rm -rf /var/lib/apt/lists/* \\
    && git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class NetbirdImageDefault(Image):

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
        return NetbirdImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        go_test = _go_test_cmd(self.pr)
        repo = self.pr.repo

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(
                    repo=repo,
                    sha=self.pr.base.sha,
                    packages=_test_packages(self.pr),
                    go_test=go_test,
                ),
            ),
            File(".", "run.sh", _RUN_SH.format(repo=repo, go_test=go_test)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(repo=repo, go_test=go_test)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(repo=repo, go_test=go_test)),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        header = f"""FROM {dep.image_name()}:{dep.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

"""
        return header + Image._HARDENING_BLOCK


@Instance.register("netbirdio", "netbird")
class Netbird(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NetbirdImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        result_re = re.compile(r"^---\s+(?P<status>PASS|FAIL|SKIP):\s+(?P<name>\S+)")

        for raw_line in test_log.split("\n"):
            line = ansi_escape.sub("", raw_line).strip()
            if not line:
                continue

            match = result_re.match(line)
            if not match:
                continue

            name = match.group("name").strip()
            status = match.group("status")

            if status == "FAIL":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "PASS":
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
            else:
                if name not in failed_tests and name not in passed_tests:
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


_BUNDLE_NIS_NETBIRD = [
    "4083-4145",
]
for _ni in _BUNDLE_NIS_NETBIRD:
    Instance.register("netbirdio", _ni)(Netbird)
