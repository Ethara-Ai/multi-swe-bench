import json
import posixpath
import re
import shlex
import xml.etree.ElementTree as ET

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BUN_IMAGE = "oven/bun:1.4.0"
_BASE_TAG = "base-3121_to_3121"

_TEST_FILE_RE = re.compile(
    r"^\+\+\+ b/(\S+\.(?:test|spec)\.[cm]?[jt]sx?)$", re.MULTILINE
)
_PACKAGE_RE = re.compile(r"^(packages/[^/]+)/(.+)$")


def _test_targets(pr: PullRequest) -> list[tuple[str, str]]:
    targets = []
    for path in sorted(set(_TEST_FILE_RE.findall(pr.test_patch))):
        owner = _PACKAGE_RE.match(path)
        targets.append((owner.group(1), owner.group(2)) if owner else (".", path))
    return targets


_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__

git reset --hard
git clean -fdx
test -z "$(git status --porcelain)"
git checkout --detach __SHA__
test -z "$(git status --porcelain)"

bun install --frozen-lockfile

mswb_gate() {
    local dir="$1" file
    shift
    local present=()
    for file in "$@"; do
        if [ -f "$dir/$file" ]; then
            present+=("./$file")
        fi
    done
    if [ "${#present[@]}" -gt 0 ]; then
        cd "/home/__REPO__/$dir"
        bun test --pass-with-no-tests -t '^mswb_no_match$' "${present[@]}" < /dev/null
        cd /home/__REPO__
    fi
}

__GATES__
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export ARCHON_TELEMETRY_DISABLED=1

cd /home/__REPO__
__PATCH_STEP__

mswb_bun_test() {
    local dir="$1" file="$2" report="/tmp/mswb-junit-$3.xml" rc=0 pid
    cd "/home/__REPO__/$dir"
    bun test --timeout 20000 --retry 2 --reporter=junit --reporter-outfile="$report" "./$file" < /dev/null &
    pid=$!
    wait "$pid" || rc=$?
    cd /home/__REPO__
    python3 - "$dir/$file" "$rc" "$report" "$HOSTNAME" "$pid" <<'EOF'
import json, os, sys
os.set_blocking(1, True)
path, rc, report, host, pid = sys.argv[1:]
print(f"bun test {path} exit {rc}")
if os.path.exists(report):
    print(f"MSWB_JUNIT_BEGIN {path} {host} {pid}")
    print(json.dumps(open(report, encoding="utf-8").read()))
    print("MSWB_JUNIT_END")
EOF
}

__TEST_STEP__
"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class _ArchonImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class Archon3121To3121ImageBase(_ArchonImage):
    def dependency(self) -> str | Image:
        return _BUN_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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

RUN apt-get update && \\
    apt-get install -y --no-install-recommends ca-certificates git python3 && \\
    rm -rf /var/lib/apt/lists/*

{self.global_env}

RUN git config --global user.email "mswb@example.invalid" && \\
    git config --global user.name "mswb"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Archon3121To3121ImageDefault(_ArchonImage):
    def dependency(self) -> Image:
        return Archon3121To3121ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare(self) -> str:
        groups: dict[str, list[str]] = {}
        for owner, path in _test_targets(self.pr):
            groups.setdefault(owner, []).append(path)
        gates = [
            "mswb_gate " + " ".join(shlex.quote(a) for a in [owner, *paths])
            for owner, paths in groups.items()
        ]
        return _render(
            _PREPARE_SH,
            {
                "__REPO__": self.pr.repo,
                "__SHA__": self.pr.base.sha,
                "__GATES__": "\n".join(gates),
            },
        )

    def _act(self, name: str, patch_step: str) -> File:
        tests = [
            f"mswb_bun_test {shlex.quote(owner)} {shlex.quote(path)} {index}"
            for index, (owner, path) in enumerate(_test_targets(self.pr))
        ]
        values = {
            "__REPO__": self.pr.repo,
            "__PATCH_STEP__": patch_step,
            "__TEST_STEP__": "\n".join(tests),
        }
        return File(".", name, _render(_RUN_SH, values))

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", self._prepare()),
            self._act("run.sh", ""),
            self._act(
                "test-run.sh", "git apply --whitespace=nowarn /home/test.patch"
            ),
            self._act(
                "fix-run.sh",
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        base_commit_arg = f'ARG BASE_COMMIT="{self.pr.base.sha}"'
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{base_commit_arg}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_JUNIT_RE = re.compile(
    r"^MSWB_JUNIT_BEGIN (\S+) (\S+) (\d+)\n(.*?)\nMSWB_JUNIT_END$",
    re.MULTILINE | re.DOTALL,
)
_SPACE_RE = re.compile(r"\s+")


def _stable(text: str, runtime: dict[str, str]) -> str:
    for value, label in runtime.items():
        text = re.sub(rf"(?<![\w.]){re.escape(value)}(?![\w.])", label, text)
    return _SPACE_RE.sub(" ", text).strip()


def _collect(
    node: ET.Element,
    path: str,
    titles: list[str],
    runtime: dict[str, str],
    seen: dict[str, int],
    found: dict[str, set[str]],
):
    for child in node:
        if child.tag == "testsuite":
            _collect(child, path, [*titles, child.get("name")], runtime, seen, found)
        elif child.tag == "testcase":
            parts = [*titles, child.get("name")]
            name = f"bun::{path}::" + " > ".join(_stable(p, runtime) for p in parts)
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [{seen[name]}]"
            if child.find("failure") is not None or child.find("error") is not None:
                status = "FAILED"
            elif child.find("skipped") is not None:
                status = "SKIPPED"
            else:
                status = "PASSED"
            found[status].add(name)


@Instance.register("coleam00", "archon_3121_to_3121")
class ARCHON_3121_TO_3121(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Archon3121To3121ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        text = _ANSI_RE.sub("", test_log).replace("\r", "")
        for path, host, pid, block in _JUNIT_RE.findall(text):
            try:
                report = ET.fromstring(json.loads(block))
            except (ValueError, ET.ParseError):
                continue
            runtime = {host: "<host>"}
            if len(pid) >= 3:
                runtime[pid] = "<pid>"
            seen: dict[str, int] = {}
            for suite in report:
                _collect(suite, posixpath.normpath(path), [], runtime, seen, found)

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
