from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "paho_mqtt_golang_625_to_625"

BASE_TAG = "base-625-to-625"


GO_START = "-----MSB_GO_JSON_START-----"
GO_END = "-----MSB_GO_JSON_END-----"


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

git update-index -q --really-refresh || true

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

for p in "$@"; do
    if git apply --whitespace=nowarn "$p"; then
        echo "apply_patch: applied $p"
    else
        echo "apply_patch: plain apply failed for $p, retrying with --3way"
        git apply --3way --whitespace=nowarn "$p"
        echo "apply_patch: applied $p with --3way"
    fi
    git add -A
done

if git grep -qI -e '^<<<<<<< ' -- . 2>/dev/null; then
    echo "apply_patch: conflict markers survived the merge"
    git grep -nI -e '^<<<<<<< ' -- . | head
    exit 1
fi

# A patch can change go.mod/go.sum. Re-resolve when it does, so the build sees
# the dependency set the patch declares rather than the pre-patch one.
if git diff --name-only HEAD -- go.mod go.sum | grep -q .; then
    echo "apply_patch: go.mod/go.sum changed, re-downloading modules"
    go mod download
fi
"""


_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CGO_ENABLED=0
export GOFLAGS=-mod=mod

cd /home/{repo}

git reset --hard
git clean -fdq

bash /home/check_git_changes.sh

# The image must sit on the dataset's base_commit and nothing else. The PR layer
# checked it out; assert it here so a silent drift fails the BUILD rather than
# surfacing as unexplainable test results three acts later.
test "$(git rev-parse HEAD)" = "{sha}"
echo "prepare: HEAD is {sha}"

# Warm the module cache so the acts do not each pay a cold download, and so a
# registry outage fails the BUILD -- where it is attributable -- instead of
# masquerading as a test failure. Retried: proxy.golang.org is a third party.
for i in 1 2 3 4; do
    if go mod download; then break; fi
    echo "prepare: go mod download attempt $i failed, retrying"
    sleep $((i * 10))
    [ "$i" = "4" ] && exit 1
done

# Compile the packages under test WITHOUT running them. This proves the
# toolchain and dependency set are sound at build time; whether the tests PASS
# is the acts' question, not the image's.
go build ./...
go vet ./... 2>/dev/null || true

echo "prepare: modules downloaded and packages build"
"""


_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail
export CGO_ENABLED=0
export GOFLAGS=-mod=mod
# Go test output embeds durations; pin the clock so nothing else varies.
export TZ=utc

cd /home/{repo}

# The fvt_* suites talk to a real MQTT broker on 127.0.0.1:1883. With no broker
# 27 tests fail at the BASE commit for a reason that has nothing to do with this
# PR, which buries the signal the acts exist to measure. Start one per act.
if command -v mosquitto >/dev/null 2>&1; then
    # Started with ONLY our own config, never Debian's /etc/mosquitto/mosquitto.conf.
    # That file sets pid_file to /run/mosquitto/, a directory systemd normally
    # creates and which does not exist in a container, so mosquitto exits with
    # "Unable to write pid file". Adding our own pid_file to conf.d does not
    # help either -- mosquitto rejects a second value outright with
    # "Duplicate pid_file value in configuration" rather than letting the later
    # one win. Loading only msb.conf sidesteps both.
    mosquitto -c /etc/mosquitto/conf.d/msb.conf -d 2>/tmp/mosq_err || true
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null; then
            echo "broker: listening on 1883"
            break
        fi
        sleep 1
        [ "$i" = "10" ] && {{ echo "broker: NOT listening after 10s"; cat /tmp/mosq_err 2>/dev/null; }}
    done
fi

TARGETS=$(cat /home/go_targets.txt 2>/dev/null || true)

echo "{go_start}"
if [ -n "$TARGETS" ]; then
    echo "go test packages:$TARGETS"
    rc=0
    # -json gives one JSON object per event, so each line is short: the report
    # can never be truncated by the 64KB pipe buffer the way a single-line
    # report can. -count=1 disables the test cache, which would otherwise
    # replay a previous act's result instead of running the patched code.
    # The ceiling turns a hung test into a failed act with a real log rather
    # than an instance that stalls the whole run.
    timeout --signal=KILL 480 \\
        go test -json -count=1 -v $TARGETS > /tmp/go_json 2>/tmp/go_err || rc=$?
    echo "go test exit: $rc"
    [ "$rc" = "137" ] && echo "go test: KILLED by the 480s ceiling (hung test)"
    cat /tmp/go_json
else
    echo "go test: this PR's test patch names no package"
fi
echo "{go_end}"

echo "----- go stderr (human, not parsed) -----"
tail -40 /tmp/go_err 2>/dev/null || true
"""


class PahoMqttGolang625ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    # go.mod at the base commit declares `go 1.14`. golang:1.19 still accepts a
    # 1.14 module (the toolchain only refuses a directive NEWER than itself) and
    # is a real multi-arch tag, which the delivery requires.
    def dependency(self) -> Union[str, "Image"]:
        return "golang:1.19-bullseye"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        # The `-C <dir>` form throughout: core's DockerfileEnhancer scans this
        # text -- comments included -- for a bare git verb and, on a base whose
        # dependency() is a string, injects a commit checkout plus the history
        # scrub. Under this layout that block belongs in the PR layer.
        #
        # The base fetches only this bundle's base commits, at depth 1. The PR
        # layer's hardening block ends in an aggressive gc, and that repack is
        # charged against whatever history the base carries.
        return """# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{{org}}/{{repo}}.git"
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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{{org}}/{{repo}}" \\
      org.opencontainers.image.description="{{org}}/{{repo}} Docker image" \\
      org.opencontainers.image.source="https://github.com/{{org}}/{{repo}}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

# bullseye is EOL. Its security pool is gone from deb.debian.org (every .deb
# 404s) and archive.debian.org serves no Release file for bullseye-security
# either, so the line cannot be rewritten -- only dropped. Every package this
# image needs, mosquitto 2.0.11 included, still resolves from bullseye main.
# Without this, `apt-get install mosquitto` dies with exit 100 because apt
# prefers the security-pool build that no longer exists.
RUN sed -i '/-security/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates mosquitto \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    mkdir -p /etc/mosquitto/conf.d; \\
    echo "listener 1883 127.0.0.1" >  /etc/mosquitto/conf.d/msb.conf; \\
    echo "allow_anonymous true"    >> /etc/mosquitto/conf.d/msb.conf; \\
    echo "persistence false"       >> /etc/mosquitto/conf.d/msb.conf; \\
    echo "pid_file /tmp/mosquitto.pid" >> /etc/mosquitto/conf.d/msb.conf

RUN git config --global --add safe.directory '*'

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git -C /home clone "${{REPO_URL}}" {repo}; then break; fi; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    test -d /home/{repo}/.git

{clear_env}

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            org=org,
            global_env=self.global_env,
            clear_env=self.clear_env,
            repo=repo,
        )


class PahoMqttGolang625ImageDefault(Image):
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
        return PahoMqttGolang625ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_patch_files(self) -> list[str]:
        out = []
        for line in self.pr.test_patch.split("\n"):
            if line.startswith("+++ b/"):
                out.append(line[6:].strip())
        return out

    def go_targets(self) -> list[str]:
        """The PACKAGES holding this PR's tests, as ./dir paths.

        `go test` takes packages, not files: naming a single _test.go file
        compiles that file ALONE, without its package's other sources, and the
        build fails on every identifier defined next door. Map each touched
        test file to its directory instead, deduplicated.
        """
        pkgs = set()
        for f in self._test_patch_files():
            if not f.endswith("_test.go"):
                continue
            d = f.rsplit("/", 1)[0] if "/" in f else "."
            pkgs.add("./" + d if d != "." else ".")
        return sorted(pkgs)

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "go_targets.txt", "\n".join(self.go_targets()) + "\n"),
            File(".", "check_git_changes.sh",
                 _CHECK_GIT_CHANGES_SH.format(repo=repo)),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH.format(repo=repo)),
            File(".", "prepare.sh", _PREPARE_SH.format(repo=repo, sha=sha)),
            File(".", "run_tests.sh",
                 _RUN_TESTS_SH.format(repo=repo, go_start=GO_START, go_end=GO_END)),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # The sha is written LITERALLY: build_dataset.py supplies REPO_URL and
        # BASE_COMMIT only when dependency() returns a str -- the base alone --
        # so a PR layer writing ${BASE_COMMIT} would expand it to the empty
        # string and land on the default branch.
        sha = self.pr.base.sha

        # DERIVED from the harness's own definition, never retyped.
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


@Instance.register("eclipse-paho", NUMBER_INTERVAL)
class PahoMqttGolang625To625(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PahoMqttGolang625ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    @staticmethod
    def _between(text: str, start: str, end: str) -> str:
        i = text.find(start)
        if i == -1:
            return ""
        i += len(start)
        j = text.find(end, i)
        return text[i:j] if j != -1 else text[i:]

    def parse_log(self, test_log: str) -> TestResult:
        """Read `go test -json`, one event per line.

        Only Action in {pass, fail, skip} WITH a Test field is a test result:
        the same actions also appear for the PACKAGE (no Test field), and
        counting those inflates the totals with things that are not tests --
        the Go equivalent of counting a build task as a test.
        """
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        seen: dict[str, int] = {}

        def uniq(name: str) -> str:
            n = seen[name] = seen.get(name, 0) + 1
            return name if n == 1 else f"{name} #{n}"

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        blob = self._between(text, GO_START, GO_END)

        for line in blob.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            action = ev.get("Action")
            test = ev.get("Test")
            if not test or action not in ("pass", "fail", "skip"):
                continue
            # The tool name comes FIRST: report.py's cheating guard splits a
            # test name on "::" and treats the head as a file path, so a head
            # of "go" matches no file and a PR that CREATES a test file cannot
            # trip guard_fix_patch_touched_tests.
            name = uniq(f"go::{ev.get('Package', '')}::{test}")
            if action == "pass":
                passed.add(name)
            elif action == "fail":
                failed.add(name)
            else:
                skipped.add(name)

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
