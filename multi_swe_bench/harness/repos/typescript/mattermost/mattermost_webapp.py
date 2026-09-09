"""mattermost/mattermost — webapp (frontend) era.

`mattermost/mattermost` is a monorepo: Go server under `server/`, React/TypeScript
client under `webapp/`. The existing golang eras
(`mattermost_go_22000_to_99999`, `mattermost_0_to_21999`) run
`cd server && go test ./...`, which can only grade Go patches. A PR whose patches
touch only `webapp/**` produces byte-identical run/test/fix results under those
eras -- no fail->pass transition is possible -- so every such instance is
rejected as invalid.

This module supplies the missing half: an npm/jest era for webapp PRs. Routing
lives in `repos/golang/mattermost/mattermost.py`, which dispatches on PATCH
CONTENT (Go files vs `webapp/**`) rather than PR number.

Structure and the jest-JSON reporting contract are modelled on
`typescript/mattermost_community/focalboard.py` -- same org family, same stack.
"""

from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "mattermost"
REPO = "mattermost"
ERA = "mattermost_webapp"

# Markers around the machine-readable jest report. parse_log reads ONLY what sits
# between them, never the human-readable tick output whose leaf titles collide.
JSON_START = "-----MSB_JEST_JSON_START-----"
JSON_END = "-----MSB_JEST_JSON_END-----"

# webapp/package.json declares npm workspaces (channels, platform/*) and its
# postinstall builds the platform packages that `channels` imports. Scripts are
# therefore NOT skipped on install -- with --ignore-scripts the platform
# workspaces stay unbuilt and every channels suite dies on unresolved
# @mattermost/{types,client,components} imports.
#
# webapp/channels/package.json runs `cross-env TZ=Etc/UTC jest`; TZ is pinned
# here for the same reason (date-formatting snapshots are timezone sensitive).
JEST_RUN = """cd /home/{repo}/webapp/channels

export CI=true
export TZ=Etc/UTC

rm -f /home/jest_results.json

# A jest worker deadlock hung one run for 49 minutes at ~0% CPU; --forceExit
# only fires after a run completes, so it cannot break that. Bound the stage
# with an external timeout and a per-test timeout instead.
set +e
timeout -k 60 1200 ../node_modules/.bin/jest \\
    --ci \\
    --coverage=false \\
    --maxWorkers=1 \\
    --json \\
    --outputFile=/home/jest_results.json \\
    --testTimeout=60000 \\
    --forceExit
set -e

echo "{start}"
if [ -f /home/jest_results.json ]; then
    cat /home/jest_results.json
fi
echo ""
echo "{end}"
"""


def jest_run(repo: str) -> str:
    return JEST_RUN.format(repo=repo, start=JSON_START, end=JSON_END)



# webapp/package.json `engines.node` changes inside this repo's history:
#     pr-23977                     node ^16.10.0   npm ^7.24.0
#     pr-26698/26967/27915/27918   node >=18.10.0  npm ^9 || ^10
# mattermost enforces engines (npm fails with `notsup`, not a warning), so a
# single base cannot serve both. Pick the interpreter per PR and give each era
# its own base tag.
def _node_major(pr: PullRequest) -> int:
    return 16 if (getattr(pr, "number", 0) or 0) < 25000 else 20


class MattermostWebappImageBase(Image):
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
        return f"node:{_node_major(self.pr)}-bullseye"

    def image_tag(self) -> str:
        return f"base-webapp-node{_node_major(self.pr)}"

    def workdir(self) -> str:
        return f"base-webapp-node{_node_major(self.pr)}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""FROM {self.dependency()}

ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=Etc/UTC \\
    CI=true \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NODE_OPTIONS=--max-old-space-size=4096

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.description="{ORG}/{REPO} webapp Docker image" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}"

# Debian bullseye's security suite Release file is EXPIRED, so `apt-get update`
# fails hard ("Release file ... is expired"). Drop the -security lines; the
# remaining deb.debian.org suites carry everything this image installs. Same
# guard the handsontable base config uses, for the same reason.
RUN set -eux; \\
    sed -i '/-security/d' /etc/apt/sources.list; \\
    ! grep -q '\\-security' /etc/apt/sources.list; \\
    grep -q 'deb.debian.org/debian ' /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git make python3 build-essential \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

# compression 0 + a large postBuffer: a plain clone of this monorepo is large
# enough that the default settings stall and die in index-pack.
RUN set -eux; \\
    git config --global core.compression 0; \\
    git config --global http.postBuffer 1048576000; \\
    git config --global http.lowSpeedLimit 1000; \\
    git config --global http.lowSpeedTime 120; \\
    for i in 1 2 3; do \\
        git clone "${{REPO_URL}}" /home/{REPO} && break; \\
        echo "clone attempt $i failed; retrying" >&2; \\
        rm -rf /home/{REPO}; \\
    done; \\
    git -C /home/{REPO} rev-parse --verify HEAD

WORKDIR /home/{REPO}

CMD ["/bin/bash"]
"""


class MattermostWebappImageDefault(Image):
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
        return MattermostWebappImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git reset --hard
# Some base commits are unreachable from any ref upstream (the base branch was
# force-pushed after collection), so the image's `git clone` does not carry them
# and `git checkout` dies with "reference is not a tree". GitHub still serves
# such objects by explicit SHA. Guarded: a no-op when the commit is present.
# DockerfileEnhancer (harness core) appends its hardening block to the BASE
# image, which does `git remote remove origin`. A PR layer therefore has no
# remote to fetch from, and unreachable base commits fail with
#     fatal: Could not read from remote repository
# Re-add origin only for the duration of the fetch and drop it immediately.
# The enhancer's scrub still runs after this and re-asserts a clean tree, so the
# anti-reward-hacking guarantee is unchanged.
if ! git cat-file -e {sha}^{{commit}} 2>/dev/null; then
    git remote add origin https://github.com/{org}/{repo}.git 2>/dev/null || true
    git fetch --no-tags origin {sha}
    git remote remove origin 2>/dev/null || true
fi
git cat-file -e {sha}^{{commit}}
git checkout --detach {sha}

cd /home/{repo}/webapp
# Scripts intentionally NOT ignored: postinstall builds the platform workspaces
# that webapp/channels imports.
npm ci --no-audit --no-fund

test -x /home/{repo}/webapp/node_modules/.bin/jest
""".format(repo=REPO, org=ORG, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
{jest}""".format(jest=jest_run(REPO)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{jest}""".format(repo=REPO, jest=jest_run(REPO)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{jest}""".format(repo=REPO, jest=jest_run(REPO)),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copies.rstrip()}

RUN bash /home/prepare.sh
"""


@Instance.register(ORG, ERA)
class MattermostWebapp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MattermostWebappImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    @staticmethod
    def _extract_report(test_log: str) -> Optional[dict]:
        import json

        start = test_log.rfind(JSON_START)
        if start == -1:
            return None
        end = test_log.find(JSON_END, start)
        if end == -1:
            return None
        blob = test_log[start + len(JSON_START):end].strip()
        if not blob:
            return None
        try:
            return json.loads(blob)
        except ValueError:
            return None

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        report = self._extract_report(test_log)
        if report is None:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )

        prefix = f"/home/{REPO}/"

        for suite in report.get("testResults") or []:
            path = suite.get("name") or ""
            rel_path = path[len(prefix):] if path.startswith(prefix) else path.lstrip("/")

            assertions = suite.get("assertionResults") or []

            # A suite that produced no assertion (type error, import failure,
            # OOM) must still count as a failure, or the file silently vanishes
            # from both f2p and p2p.
            if not assertions:
                if (suite.get("status") or "").lower() == "failed":
                    failed_tests.add(f"jest::{rel_path}::<suite failed to run>")
                continue

            for assertion in assertions:
                name = assertion.get("fullName") or assertion.get("title") or ""
                if not name:
                    continue
                # id shape <tool>::<path>::<name> -- tool first, or report.py
                # rejects the instance when the fix patch creates that file.
                test_id = f"jest::{rel_path}::{name}"
                status = (assertion.get("status") or "").lower()
                if status == "passed":
                    passed_tests.add(test_id)
                elif status == "failed":
                    failed_tests.add(test_id)
                else:
                    skipped_tests.add(test_id)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
