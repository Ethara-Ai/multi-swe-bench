import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult

_JEST_JSON = "/home/msweb-jest.json"
_JEST_EMIT_JS = "/home/msweb_jest_emit.js"
_STAGE_TIMEOUT = "3600"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


_ERAS: dict[str, dict] = {
    "classic_jest": {
        "shard": (7358, 11434),
        "root_image": "node:12-buster",
        "setup": [
            "RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\\n"
            "    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \\\n"
            "    sed -i '/buster-updates/d' /etc/apt/sources.list && \\\n"
            "    apt-get -o Acquire::Check-Valid-Until=false update && \\\n"
            "    apt-get install -y --no-install-recommends --allow-unauthenticated \\\n"
            "    ca-certificates curl git make python python3 xz-utils && \\\n"
            "    rm -rf /var/lib/apt/lists/*",
            "COPY --from=node:8-slim /usr/local/ /usr/local/\n"
            "COPY --from=node:8-slim /opt/ /opt/",
        ],
        "verify": [
            'test "$(node -p "process.versions.node.split(\'.\')[0]")" = "8"',
            "command -v yarn",
        ],
        "install": """if ! yarn install --ignore-engines --frozen-lockfile; then
    # Some commits in this era pin a lerna fork by URL that no longer resolves.
    sed -i '/@lerna.*collect-updates/d' package.json || true
    sed -i '/nicolo-ribaudo\\/lerna/d' package.json || true
    sed -i '/collect-updates@https/,/^[^[:space:]]/ { /^[^[:space:]]/!d; /collect-updates@https/d }' yarn.lock || true
    yarn install --ignore-engines || true
fi

if [ -f node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
fi""",
        "gate": "test -f node_modules/jest/bin/jest.js\n"
        "node -e \"require.resolve('jest'); require.resolve('babel-jest'); console.log('DEPS_OK')\"",
        "jest_setup": "",
        "jest": "node node_modules/jest/bin/jest.js",
    },
    "berry_jest": {
        "shard": (11435, 13727),
        "root_image": "node:16-slim",
        "setup": [
            "RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\\n"
            "    sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\\n"
            "    sed -i '/bullseye-updates/d' /etc/apt/sources.list && \\\n"
            "    apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    ca-certificates git make python3 && \\\n"
            "    rm -rf /var/lib/apt/lists/*",
        ],
        "verify": [],
        "install": 'YARN_BIN="$(find .yarn/releases -name \'yarn-*.cjs\' | head -1)"\n'
        'test -n "$YARN_BIN"\n'
        'node "$YARN_BIN" install || true',
        "gate": "test -d node_modules\n"
        "node -e \"require.resolve('jest'); console.log('DEPS_OK')\"",
        "jest_setup": 'YARN_BIN="$(find .yarn/releases -name \'yarn-*.cjs\' | head -1)"',
        "jest": 'node "$YARN_BIN" node "$(node "$YARN_BIN" bin jest)"',
    },
}

_NUMBER_ERAS = [
    (4892, 7357, None),
    (7358, 11434, "classic_jest"),
    (11435, 13727, "berry_jest"),
]

_SHA_ERAS: dict[str, str] = {
}


def select_era(number: int, base_sha: Optional[str] = None) -> str:
    if base_sha:
        for prefix, era in _SHA_ERAS.items():
            if base_sha.startswith(prefix):
                return era

    for lo, hi, era in _NUMBER_ERAS:
        if lo <= number <= hi:
            if era is None:
                break
            return era

    known = ", ".join(f"{lo}-{hi}" for lo, hi, e in _NUMBER_ERAS if e)
    raise ValueError(
        f"babel/babel PR {number} falls outside every verified era ({known}). "
        f"Babel's era ranges overlap, so the PR number alone cannot decide the "
        f"toolchain. Read package.json and .yarnrc.yml at this PR's base commit "
        f"(packageManager field, yarnPath, jest major), then add an interval to "
        f"_NUMBER_ERAS -- or, if it inverts an existing interval, pin it in "
        f"_SHA_ERAS."
    )


def _shard_tag(pr: PullRequest) -> str:
    era = select_era(pr.number, pr.base.sha)
    lo, hi = _ERAS[era]["shard"]
    if not lo <= pr.number <= hi:
        raise ValueError(
            f"babel/babel PR {pr.number} routed to era {era!r}, whose shard "
            f"interval base-{hi}_to_{lo} does not contain it. Widen the era's "
            f"'shard' interval, or route the PR to an era that covers it."
        )
    return f"base-{hi}_to_{lo}"


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

_EMIT_SRC = r"""var fs = require('fs');

var PREFIX = '__PREFIX__';
var out = [];

function norm(s) {
  return String(s)
    .replace(/[^\x20-\x7e]/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/^ | $/g, '');
}

var report;
try {
  report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
} catch (e) {
  process.stderr.write('msweb jest emit: ' + e.message + '\n');
  process.exit(0);
}

var suites = report.testResults || [];
for (var i = 0; i < suites.length; i++) {
  var suite = suites[i];
  var full = String(suite.name || '').replace(/\\/g, '/');
  var at = full.indexOf(PREFIX);
  var rel = at !== -1 ? full.slice(at + PREFIX.length) : full.replace(/^\/+/, '');
  var cases = suite.assertionResults || [];
  if (cases.length === 0 && suite.status === 'failed') {
    out.push('TESTCASE FAILED ' + norm(rel + '::<test suite failed to run>'));
    continue;
  }
  for (var k = 0; k < cases.length; k++) {
    var c = cases[k];
    var title = c.fullName || c.title || '';
    if (!title) continue;
    var st = c.status === 'passed' ? 'PASSED'
           : c.status === 'failed' ? 'FAILED' : 'SKIPPED';
    out.push('TESTCASE ' + st + ' ' + norm(rel + '::' + title));
  }
}

process.stdout.write(out.length ? out.join('\n') + '\n' : '');
"""


def _prepare_sh(repo: str, sha: str, era: dict) -> str:
    return f"""#!/bin/bash
set -e

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

# The shared base clones branches and tags only -- never refs/pull/*. A PR whose
# base commit is no longer reachable from any surviving ref is therefore absent
# from the clone, and the checkout below would die with "reference is not a
# tree". Fetch it on demand; this is a no-op for the usual case where the commit
# is already present. origin still exists here -- the prune removes it later.
git cat-file -e {sha}^{{commit}} 2>/dev/null || git fetch --no-tags origin {sha}

git checkout --detach {sha}
bash /home/check_git_changes.sh

{era["install"]}

make build || true

# Hard gate. The install above tolerates failures, so assert here that the tree
# actually loads: a swallowed install failure otherwise produces an empty test
# report, which the harness reads as "0 failures" rather than "broken image".
test -f Makefile
{era["gate"]}
"""


def _graded_sh(repo: str, era: dict, apply_line: str = "") -> str:
    apply = f"{apply_line}\n" if apply_line else ""
    setup = era["jest_setup"]
    jest_setup = f"{setup}\n" if setup.strip() else ""
    return f"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test
export FORCE_COLOR=0

cd /home/{repo}
{apply}
make build || true

jest_status=0
rm -f {_JEST_JSON}
# --forceExit: babel's suite leaves handles open, and without this jest sits after
# the last test instead of exiting. timeout is the backstop if it hangs anyway --
# a stage must fail loudly rather than block the batch forever.
{jest_setup}timeout {_STAGE_TIMEOUT} {era["jest"]} --maxWorkers=2 --ci --forceExit \
    --json --outputFile={_JEST_JSON} || jest_status=$?
echo "##### MSWEB-JEST-EXIT: $jest_status"

# jest writes the report only on a clean finish, so a missing file means the run
# died or was killed -- NOT that nothing failed. Emitting zero results here would
# read downstream as "0 failures", i.e. a broken run masquerading as a clean one.
if [ ! -f {_JEST_JSON} ]; then
    echo "##### MSWEB-JEST-NO-REPORT: jest produced no report (exit $jest_status)"
    exit 1
fi

node {_JEST_EMIT_JS} {_JEST_JSON}
"""


_PRUNE_BLOCK = """WORKDIR /home/{repo}

RUN set -eux; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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


class BabelShardBase(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
        self._era = _ERAS[select_era(pr.number, pr.base.sha)]

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._era["root_image"]

    def image_tag(self) -> str:
        return _shard_tag(self.pr)

    def workdir(self) -> str:
        return _shard_tag(self.pr)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {self._era['root_image']}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{repo}.git"\n'
                "ARG BASE_COMMIT\n"
                f"\n{DockerfileEnhancer._PROXY_ARGS}"
            ),
            DockerfileEnhancer._ENV_BLOCK,
        ]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )
        sections.append(DockerfileEnhancer._CERT_SYMLINKS)
        sections.append("WORKDIR /home/")
        sections.extend(s for s in self._era["setup"] if s.strip())
        sections.append("RUN git config --global --add safe.directory '*'")
        sections.append(
            f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
            f"    cd /home/{repo} && git rev-parse HEAD >/dev/null"
        )
        checks = list(self._era["verify"]) + [
            f"test -d /home/{repo}/.git",
            f"test -f /home/{repo}/package.json",
        ]
        sections.append("RUN set -eux; \\\n    " + "; \\\n    ".join(checks))
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')
        return "\n\n".join(sections) + "\n"


class BabelPRImage(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
        self._era = _ERAS[select_era(pr.number, pr.base.sha)]

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return BabelShardBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        era = self._era
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "msweb_jest_emit.js",
                _EMIT_SRC.replace("__PREFIX__", f"/home/{repo}/"),
            ),
            File(".", "prepare.sh", _prepare_sh(repo, self.pr.base.sha, era)),
            File(".", "run.sh", _graded_sh(repo, era)),
            File(
                ".",
                "test-run.sh",
                _graded_sh(
                    repo, era, "git apply --whitespace=nowarn /home/test.patch"
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _graded_sh(
                    repo,
                    era,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        sections = [f"FROM {base.image_name()}:{base.image_tag()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append("\n".join(f"COPY {f.name} /home/" for f in self.files()))
        sections.append("RUN bash /home/prepare.sh")
        sections.append(
            _PRUNE_BLOCK.format(repo=self.pr.repo, sha=self.pr.base.sha)
        )
        if self.clear_env:
            sections.append(self.clear_env)
        return "\n\n".join(sections) + "\n"


@Instance.register("babel", "babel")
class BABEL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        select_era(pr.number, pr.base.sha)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BabelPRImage(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        text = _ANSI_ESCAPE.sub("", test_log or "").replace("\r", "")

        for line in text.split("\n"):
            case = _TESTCASE_RE.match(line)
            if not case:
                continue
            status = case.group(1)
            name = re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e]", " ", case.group(2)))
            name = name.strip()
            if not name:
                continue
            if status == "FAILED":
                failed.add(name)
            elif status == "SKIPPED":
                skipped.add(name)
            else:
                passed.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
