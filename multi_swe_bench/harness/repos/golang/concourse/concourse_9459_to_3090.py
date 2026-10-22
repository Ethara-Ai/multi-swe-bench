import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "concourse"

ARCH_RESOLVE = 'a="${TARGETARCH:-}"; if [ -z "$a" ]; then case "$(uname -m)" in x86_64) a=amd64;; aarch64|arm64) a=arm64;; *) a="$(uname -m)";; esac; fi'

TEST_COMMAND = r"""if grep -qE '^\+\+\+ b/.*_test\.go$' /home/test.patch; then
    if grep -qE '^go[[:space:]]+[0-9]' go.mod; then
        export GOROOT=/usr/local/go
        export GOFLAGS=-mod=mod
        export GOPROXY=https://proxy.golang.org,direct
    else
        export GOROOT=/usr/local/go112
        export GOFLAGS=
        export GOPROXY=direct
    fi
    export GO111MODULE=on
    export PATH="${GOROOT}/bin:${PATH}"
    go version
    go list -e -f '{{.ImportPath}} {{.Dir}} {{len .TestGoFiles}} {{len .XTestGoFiles}}' ./... 2>/dev/null | awk '$3+$4 > 0 {print $1" "$2}' | grep -vE '^github.com/concourse/concourse/(testflight|topgun|integration)(/| )' | grep -vE '^github.com/concourse/concourse/worker/(integration|baggageclaim)(/| )' | grep -vE '^github.com/concourse/concourse/worker/runtime/integration(/| )' | grep -vE '^github.com/concourse/concourse/tsa/cmd/tsa(/| )' > /tmp/pkgdirs.txt
    : > /tmp/pkgs_ginkgo.txt
    : > /tmp/pkgs_plain.txt
    while read -r p d; do
        if grep -lqE '"github.com/onsi/ginkgo' "$d"/*_test.go 2>/dev/null; then
            echo "$p" >> /tmp/pkgs_ginkgo.txt
        else
            echo "$p" >> /tmp/pkgs_plain.txt
        fi
    done < /tmp/pkgdirs.txt
    GINKGO_PKGS="$(cat /tmp/pkgs_ginkgo.txt)"
    PLAIN_PKGS="$(cat /tmp/pkgs_plain.txt)"
    rc=0
    if [ -n "${GINKGO_PKGS}" ]; then
        go test -v -count=1 -vet=off -p 1 -timeout 20m ${GINKGO_PKGS} -ginkgo.v -ginkgo.noColor || rc=$?
    fi
    if [ -n "${PLAIN_PKGS}" ]; then
        go test -v -count=1 -vet=off -p 1 -timeout 20m ${PLAIN_PKGS} || rc=$?
    fi
    exit ${rc}
else
    export ELM_HOME=/opt/elm/home
    export PATH="/opt/elm/bin:${PATH}"
    rm -rf web/elm/elm-stuff web/elm/tests/elm-stuff
    cd web/elm
    elm-test --compiler /opt/elm/bin/elm --report json
fi"""

RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/[[REPO]]
"""

PREPARE_SH = """#!/bin/bash
set -e

cd /home/[[REPO]]

git reset --hard

bash /home/check_git_changes.sh

git checkout [[SHA]]

bash /home/check_git_changes.sh

if grep -qE '^\\+\\+\\+ b/.*_test\\.go$' /home/test.patch; then
    { d="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"; for b in "$d"/*; do ln -sf "$b" /usr/local/bin/; done; } || true
    initdb --version
    postgres --version
    if grep -qE '^go[[:space:]]+[0-9]' go.mod; then
        export GOROOT=/usr/local/go
        export GOFLAGS=-mod=mod
        export GOPROXY=https://proxy.golang.org,direct
    else
        { [[ARCH]]; curl -fsSL -o /tmp/go-legacy.tar.gz "https://go.dev/dl/go1.12.17.linux-${a}.tar.gz"; mkdir -p /usr/local/go112; tar -C /usr/local/go112 --strip-components=1 -xzf /tmp/go-legacy.tar.gz; rm -f /tmp/go-legacy.tar.gz; } || true
        /usr/local/go112/bin/go version
        export GOROOT=/usr/local/go112
        export GOFLAGS=
        export GOPROXY=direct
    fi
    export GO111MODULE=on
    export PATH="${GOROOT}/bin:${PATH}"
    GOPROXY=https://proxy.golang.org go mod download || true
    GOPROXY=direct go mod download || true
    go build ./atc/... || true
    go build ./atc/...
else
    export ELM_HOME=/opt/elm/home
    export PATH="/opt/elm/bin:${PATH}"
    { [[ARCH]]; mkdir -p /opt/elm/bin; curl -fsSL -o /tmp/elm.gz "https://github.com/elm/compiler/releases/download/0.19.1/binary-for-linux-64-bit.gz"; gunzip -c /tmp/elm.gz > /opt/elm/bin/elm-x86_64; rm -f /tmp/elm.gz; chmod 0755 /opt/elm/bin/elm-x86_64; if [ "$a" = "amd64" ]; then printf '%s\\n' '#!/bin/sh' 'exec /opt/elm/bin/elm-x86_64 "$@"' > /opt/elm/bin/elm; else printf '%s\\n' '#!/bin/sh' 'exec /usr/bin/qemu-x86_64-static /opt/elm/bin/elm-x86_64 "$@"' > /opt/elm/bin/elm; fi; chmod 0755 /opt/elm/bin/elm; } || true
    { npm install -g --unsafe-perm --omit=optional elm-test@0.19.1-revision7; } || true
    { m="${ELM_HOME}/0.19.1/packages/Skinney/murmur3/2.0.8"; n="${ELM_HOME}/0.19.1/packages/ryannhg/date-format/2.3.0"; mkdir -p "$m" "$n"; rm -rf /tmp/murmur3 /tmp/date-format; git clone --quiet https://github.com/robinheghan/murmur3.git /tmp/murmur3; cp -R /tmp/murmur3/src "$m/"; cp /tmp/murmur3/LICENSE "$m/" 2>/dev/null || true; printf '%s\\n' '{' '    "type": "package",' '    "name": "Skinney/murmur3",' '    "summary": "An implementation of the murmur 3 hash function in elm",' '    "license": "MIT",' '    "version": "2.0.8",' '    "exposed-modules": [ "Murmur3" ],' '    "elm-version": "0.19.0 <= v < 0.20.0",' '    "dependencies": { "elm/core": "1.0.0 <= v < 2.0.0" },' '    "test-dependencies": {}' '}' > "$m/elm.json"; git clone --quiet https://github.com/ryannhg/date-format.git /tmp/date-format; git -C /tmp/date-format checkout --quiet 2.3.0; cp -R /tmp/date-format/src "$n/"; cp /tmp/date-format/LICENSE "$n/" 2>/dev/null || true; cp /tmp/date-format/elm.json "$n/elm.json"; rm -rf /tmp/murmur3 /tmp/date-format; } || true
    elm --version
    elm-test --version
    test -f "${ELM_HOME}/0.19.1/packages/Skinney/murmur3/2.0.8/src/Murmur3.elm"
    test -f "${ELM_HOME}/0.19.1/packages/ryannhg/date-format/2.3.0/src/DateFormat.elm"
    (cd web/elm && elm make src/Main.elm --output=/dev/null) || true
    (cd web/elm && elm-test --compiler /opt/elm/bin/elm --report json) || true
    test -f web/elm/elm.json
fi

git checkout -- .

bash /home/check_git_changes.sh
"""

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM golang:1.25

ARG TARGETARCH
ARG REPO_URL="https://github.com/concourse/concourse.git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="concourse/concourse" \\
      org.opencontainers.image.description="concourse/concourse Docker image" \\
      org.opencontainers.image.source="https://github.com/concourse/concourse" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git gnupg make nodejs npm postgresql postgresql-contrib qemu-user-static sudo wget xz-utils && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/[[REPO]]

WORKDIR /home/[[REPO]]

CMD ["/bin/bash"]
"""

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
SEPARATOR_RE = re.compile(r"^-{30}$")
NOISE_RE = re.compile(r"^\[[a-z]\]\[[a-z_]+\]")
SOURCE_RE = re.compile(r"^\S+\.(go|elm):\d+$")
MARKER_RE = re.compile(r"^(?:\u2022(?=$|\S)|S$|P$)")
SQL_RE = re.compile(
    r"^(?:\(\d+ rows?\)|-{2,}|\|?\s*-{2,}.*|(?:CREATE|DROP|ALTER|INSERT|UPDATE|DELETE|SELECT|TRUNCATE|SET|BEGIN|COMMIT|ROLLBACK|GRANT|REVOKE|COPY|VACUUM|ANALYZE|NOTICE|ERROR|DETAIL|HINT|WARNING|CONTEXT|STATEMENT|LINE|psql)\b.*)$"
)
EACH_RE = re.compile(r"\s*\[(?:Just)?(?:Before|After)Each\]\s*$")
JSONLOG_RE = re.compile(r"\s*\{\"timestamp\".*$")
STEP_RE = re.compile(r"(?:^|\s)STEP:.*$")
PREAMBLE_RE = re.compile(
    r"^(?:=== (?:RUN|PAUSE|CONT|NAME)\b|=+$|Random Seed:|Will run \d+ of \d+ spec|You're using deprecated Ginkgo|You are passing a Done channel|Learn more at:|flag provided but not defined|Usage of |-{1,2}[A-Za-z]|exit status \d+|goroutine \d+|panic:|\[\d+\]\s|Suite Passed|Suite Failed|Total Suite Time|SUCCESS!|FAIL!|Summarizing \d+ Failure)"
)
BRACKET_RE = re.compile(r"\[(?:Fail|Panic!|Timeout\.\.\.)\]")
STACK_RE = re.compile(
    r"(?:\+0x[0-9a-f]+$|/usr/local/go/src/|/usr/local/go112/src/|^panic\(|\(0x[0-9a-f]|\{0x[0-9a-f]|^created by \S|^\S+\([^)]*\)$)"
)
ENDMARK_RE = re.compile(r"\u2022$")
GOTEST_RE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
PKGRES_RE = re.compile(r"^(?:ok|FAIL|\?)\s+(github\.com/concourse/concourse\S*)")
MODPREFIX = "github.com/concourse/concourse"
SUMMARY_RE = re.compile(
    r"^(Ran \d+ of \d+ Specs|SUCCESS!|FAIL!|ok\s|ok$|FAIL\s|---\s|\?\s|PASS$|FAIL$|#\s|Test Suite|Running |Ginkgo ran|Randomizing|\[\d+\] )"
)
WS_RE = re.compile(r"\s+")


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

    def dependency(self) -> str:
        return "golang:1.25"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return BASE_DOCKERFILE.replace("[[ARCH]]", ARCH_RESOLVE).replace(
            "[[REPO]]", REPO_DIR
        )


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

    def dependency(self) -> str | Image:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        header = RUN_HEADER.replace("[[REPO]]", REPO_DIR)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                PREPARE_SH.replace("[[ARCH]]", ARCH_RESOLVE)
                .replace("[[REPO]]", REPO_DIR)
                .replace("[[SHA]]", self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                f"{header}\n{TEST_COMMAND}\n",
            ),
            File(
                ".",
                "test-run.sh",
                f"{header}\ngit apply --whitespace=nowarn /home/test.patch\n\n{TEST_COMMAND}\n",
            ),
            File(
                ".",
                "fix-run.sh",
                f"{header}\ngit apply --whitespace=nowarn /home/test.patch\ngit apply --whitespace=nowarn /home/fix.patch\n\n{TEST_COMMAND}\n",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_full_name()
        return f"""FROM {name}

ARG BASE_COMMIT="{self.pr.base.sha}"

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
"""


@Instance.register("concourse", "concourse")
class Concourse(Instance):
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
        log = ANSI_RE.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        elm_seen = False
        for raw in log.splitlines():
            line = raw.strip()
            if not line.startswith("{") or "testCompleted" not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("event") != "testCompleted":
                continue
            labels = event.get("labels") or []
            name = " > ".join(str(part) for part in labels).strip()
            if not name:
                continue
            elm_seen = True
            status = str(event.get("status") or "").lower()
            if status == "pass":
                passed_tests.add(name)
            elif status == "todo":
                skipped_tests.add(name)
            else:
                failed_tests.add(name)

        if not elm_seen:
            block: list[str] = []
            for raw in log.splitlines():
                stripped = raw.strip()
                if SEPARATOR_RE.match(stripped):
                    self._parse_block(
                        block, passed_tests, failed_tests, skipped_tests
                    )
                    block = []
                    continue
                block.append(raw)
                if MARKER_RE.match(stripped) or ENDMARK_RE.search(stripped):
                    self._parse_block(
                        block, passed_tests, failed_tests, skipped_tests
                    )
                    block = []
            self._parse_block(block, passed_tests, failed_tests, skipped_tests)

            pending: list[tuple[str, str]] = []
            for raw in log.splitlines():
                match = GOTEST_RE.match(raw)
                if match:
                    pending.append((match.group(1), match.group(2)))
                    continue
                pkg_match = PKGRES_RE.match(raw.strip())
                if not pkg_match:
                    continue
                pkg = pkg_match.group(1)
                if pkg == MODPREFIX:
                    pkg = "."
                elif pkg.startswith(MODPREFIX + "/"):
                    pkg = pkg[len(MODPREFIX) + 1 :]
                for status, func in pending:
                    name = f"{pkg}::{func}"
                    if status == "PASS":
                        passed_tests.add(name)
                    elif status == "SKIP":
                        skipped_tests.add(name)
                    else:
                        failed_tests.add(name)
                pending = []

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

    def _parse_block(
        self,
        block: list[str],
        passed_tests: set[str],
        failed_tests: set[str],
        skipped_tests: set[str],
    ) -> None:
        status = ""
        parts: list[str] = []
        run: list[str] = []
        source_seen = False

        for raw in block:
            line = raw.strip()
            if MARKER_RE.match(line):
                status = "pass" if line.startswith("\u2022") else "skip"
                run = []
                continue
            if line.startswith("\u2022 Failure"):
                status = "fail"
                run = []
                continue
            if line.startswith("\u2022"):
                run = []
                continue
            if ENDMARK_RE.search(line):
                status = status or "pass"
                run = []
                continue
            if SOURCE_RE.match(line):
                if run:
                    parts.extend(run)
                    run = []
                source_seen = True
                continue
            if not line:
                run = []
                continue
            if NOISE_RE.match(line):
                continue
            if SQL_RE.match(line):
                run = []
                continue
            if SUMMARY_RE.match(line):
                run = []
                continue
            if PREAMBLE_RE.match(line):
                run = []
                continue
            if BRACKET_RE.search(line):
                run = []
                continue
            if STACK_RE.search(line):
                run = []
                continue
            cleaned = STEP_RE.sub("", JSONLOG_RE.sub("", line)).strip()
            if not cleaned:
                continue
            run.append(EACH_RE.sub("", cleaned))

        if not status or not parts or not source_seen:
            return

        name = WS_RE.sub(" ", " ".join(parts)).strip()
        if not name:
            return

        if status == "pass":
            passed_tests.add(name)
        elif status == "skip":
            skipped_tests.add(name)
        else:
            failed_tests.add(name)
