import json as _ghost_json
import re
from typing import Optional, Union

from multi_swe_bench.harness.dataset import Dataset as _GhostDataset
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GhostImageBase(Image):
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
        return "node:16"

    def image_tag(self) -> str:
        return "base-15578-to-15536"

    def workdir(self) -> str:
        return "base-15578-to-15536"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        apt = """RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' \\
        -e 's|security.debian.org|archive.debian.org|g' \\
        -e '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=false update && \\
    apt-get install -y --no-install-recommends \\
    git ca-certificates python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*"""

        org = self.pr.org
        repo = self.pr.repo

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

{apt}


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class GhostImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return GhostImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# The Dockerfile layer above already detached HEAD onto {pr.base.sha} and
# scrubbed the history (no remotes, no refs), so there is NO checkout and no
# fetch here -- a fetch would fail against the removed origin, and a checkout
# would be a no-op at best. `git reset --hard` only clears any working-tree
# residue, then the tree is asserted pristine.
git reset --hard
bash /home/check_git_changes.sh

# Build time is where the network lives (R16).
#
# NOT --frozen-lockfile: #15578's fix patch edits yarn.lock together with
# ghost/oembed-service/package.json, so the tree is legitimately allowed to
# move the lockfile at the fix stage; a frozen install would be a false
# constraint. --ignore-engines because several transitive deps declare engines
# ranges that exclude 16 while working fine on it, and yarn 1 treats an engines
# miss as fatal. `|| true` per R3/§4.1 -- native optional deps (sqlite3) may
# fail to build on arm64 and that must not abort the image build.
export CI=true
yarn install --ignore-engines --network-timeout 600000 || yarn install --ignore-engines || true

# Cache-warming run: also proves at build time that mocha resolves and the
# suite executes. Never gates the build.
bash /home/run-tests.sh > /dev/null 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
# The single shared test command (R3): all three stages source this one file,
# so the command is character-identical by construction and cannot drift.
#
# No `set -e`: a non-zero mocha exit (the expected state at the test stage) must
# still let the JSON reporter's output reach stdout for parse_log. The pass/fail
# signal is the JSON body, not this script's exit status -- so it ends `exit 0`
# and the three callers keep their own `set -eo pipefail` meaningful.
set -o pipefail

cd /home/{pr.repo}

# Re-install AFTER the patches are applied, because a patch may introduce a new
# runtime dependency. #15578's fix patch adds `charset` and `iconv-lite` to
# ghost/oembed-service/package.json (and the matching yarn.lock entries); the
# build-time install in prepare.sh predates that, so without this step the fix
# stage resolves the patched oembed-service and dies with
#   Cannot find module 'charset'
# which 500s the whole admin API and cascaded 111 failures / 530 lost passes.
#
# --prefer-offline keeps the no-op case cheap: for the run and test stages, and
# for any PR whose patches do not touch a manifest, every package is already in
# the yarn cache from the build, so this resolves from disk and adds seconds
# rather than minutes. `|| true` because a registry hiccup must not abort the
# stage -- mocha then reports the missing module as ordinary test failures
# rather than the stage producing no parseable output at all.
yarn install --ignore-engines --network-timeout 600000 --prefer-offline > /dev/null 2>&1 || true

cd /home/{pr.repo}/ghost/core

# ghost/core's own harness (see its package.json scripts): overrides.js sets
# NODE_ENV=testing and WEBHOOK_SECRET and installs the @tryghost/express-test
# snapshot mochaHooks. --exit because Ghost leaves handles open. mocha runs
# single-process -- no worker pool to deadlock under emulation (R14).
#
# Scope: exactly the two directories this era's test patches touch. The package
# default (`test` -> `test:unit`) covers neither. Timeout matches the repo's own
# regression/e2e settings (60000/15000); the larger is used for both so one
# command serves both PRs.
export CI=true
export NODE_ENV=testing

./node_modules/.bin/mocha \\
    --require=./test/utils/overrides.js \\
    --exit \\
    --recursive \\
    --extension=test.js \\
    --reporter json \\
    --timeout 60000 \\
    './test/regression' './test/e2e-api' 2>&1

exit 0

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh

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

        sha = self.pr.base.sha
        hardening = f"""RUN set -eux; \\
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
    fi"""

        return f"""FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{self.pr.repo}

{hardening}

RUN bash /home/prepare.sh
"""


@Instance.register("TryGhost", "Ghost_15578_to_15536")
class GHOST_15578_TO_15536(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GhostImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        def rel_path(path: str) -> str:
            if not path:
                return ""
            marker = "/home/Ghost/"
            idx = path.find(marker)
            if idx != -1:
                return path[idx + len(marker):]
            return path.lstrip("/")

        def add(bucket: set, entry: dict) -> None:
            title = entry.get("fullTitle") or entry.get("title") or ""
            if not title:
                return
            path = rel_path(entry.get("file") or "")
            bucket.add(f"{path} > {title}" if path else title)

        decoder = _ghost_json.JSONDecoder()
        pos = 0
        while True:
            start = clean_log.find("{", pos)
            if start == -1:
                break
            try:
                obj, end = decoder.raw_decode(clean_log[start:])
            except ValueError:
                pos = start + 1
                continue
            pos = start + end
            if not isinstance(obj, dict) or "stats" not in obj:
                continue
            for entry in obj.get("passes", []):
                if isinstance(entry, dict):
                    add(passed_tests, entry)
            for entry in obj.get("failures", []):
                if isinstance(entry, dict):
                    add(failed_tests, entry)
            for entry in obj.get("pending", []):
                if isinstance(entry, dict):
                    add(skipped_tests, entry)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("TryGhost", "Ghost")(GHOST_15578_TO_15536)


if not getattr(_GhostDataset, "_ghost_dataset_fields_shim", False):
    _ghost_orig_build = _GhostDataset.build.__func__

    def _ghost_build(cls, pr, report):
        data = _ghost_orig_build(cls, pr, report)
        if getattr(pr, "org", "") != "TryGhost" or getattr(pr, "repo", "") != "Ghost":
            return data

        if not getattr(data, "lang", ""):
            data.lang = "javascript"

        instance_id = f"{pr.org}__{pr.repo}-{pr.number}"
        _orig_json = data.json

        def _json_with_instance_id(*args, **kwargs):
            raw = _orig_json(*args, **kwargs)
            try:
                payload = _ghost_json.loads(raw)
            except ValueError:
                return raw
            payload.setdefault("instance_id", instance_id)
            return _ghost_json.dumps(payload)

        data.json = _json_with_instance_id
        return data

    _GhostDataset.build = classmethod(_ghost_build)
    _GhostDataset._ghost_dataset_fields_shim = True
