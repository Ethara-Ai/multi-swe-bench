"""openclaw/openclaw -- era config for PRs 41662 .. 43683.

Ten PRs, base commits all inside 2026-03-09 .. 2026-03-12, every one of them
merged into `main`. A config for this repo already exists for five other eras
(547..1372, 1437..7610, 8388..12091, 27293..29925, 29941..32546, 32706..38292);
all six are a DIFFERENT era and are deliberately left untouched (rule 12,
2026-09-08: never modify an existing repo config, always add a new one for the
dataset in hand).

WHY A NEW ERA AT ALL
--------------------
These ten PRs postdate every existing openclaw config. Routed to any of them
they would be graded against a lane set that predates their test layout, which
is a silent misroute rather than an error: the entries resolve, the stages run,
and the numbers come out wrong. Measured against the 1437..7610 config, counting
graded test files that no lane would execute:

    41662   2 of 2 graded files unreachable   -> every stage reports zero
    42119   2 of 2 graded files unreachable   -> every stage reports zero
    42240   2 of 2 graded files unreachable   -> every stage reports zero
    42554  13 of 17 graded files unreachable
    43683   6 of  9 graded files unreachable

Three PRs reporting a flat zero is exactly the failure rule 11 forbids, and the
two partial losses silently delete most of their f2p signal.

ROUTING
-------
One registration, on the interval key, exactly as openclaw_38292_to_32706.py
does it:

    @Instance.register("openclaw", "openclaw_43683_to_41662")

Instance.create() builds that key from `pr.number_interval` (instance.py:42-43),
so every record this config serves carries

    "number_interval": "openclaw_43683_to_41662"

There is no range check, no dispatch wrapper on the bare "openclaw/openclaw"
key, and no per-number registration. A record without the interval field does
not reach this config at all -- which is the intended behaviour: routing is the
dataset's job, and a missing interval should fail to resolve rather than be
guessed at from a PR number.

TOOLCHAIN, READ AT ALL TEN BASE COMMITS (not sampled from one)
---------------------------------------------------------------
    engines.node     >=22.12.0  at seven commits, >=22.16.0 at three
    packageManager   pnpm@10.23.0                identical at all ten
    devDeps.vitest   ^4.0.18 at seven, ^4.1.0 at three
    scripts.test     node scripts/test-parallel.mjs   identical at all ten

node:22-bookworm satisfies both engine floors, pnpm is pinned by corepack from
each commit's own `packageManager`, and vitest is resolved per PR from that
commit's lockfile by the install in prepare.sh. Nothing in the spread forces a
second base image, so ONE shared base is correct and safe (rule 4, row 2).

LANE MEMBERSHIP IS DERIVED AT RUNTIME, NEVER HARDCODED
-------------------------------------------------------
The six vitest config files were fetched and hashed at all ten base commits and
they are NOT identical: `vitest.channel-paths.mjs` appears partway through the
range, `vitest.unit-paths.mjs` later still, and the channel roots move from
`src/<channel>` into `extensions/<channel>`. So `src/telegram/send.test.ts`
belongs to the channels lane at one commit in this era and to the unit lane at
another.

An earlier revision of this file encoded that as a per-PR-number table mapping
each PR to a rule set. That was wrong on principle: the config behaved
differently for different PRs in one era, driven by a snapshot of facts taken
outside the pipeline, which silently misroutes any PR added later and cannot be
checked against anything.

The repo already answers the question at every commit, so the answer is asked
for rather than remembered. `run-suites.sh` runs

    vitest list --config <lane config> --filesOnly

once per lane that exists in the checked-out tree, and a graded file belongs to
whichever lane enumerates it. Measured cost on this repo: 5-11s per lane, ~44s
for the whole set, against stage runs measured in tens of minutes. vitest's own
include/exclude rules decide -- there is no path table here to drift.

Only the lanes that enumerate a graded file are run. Running every lane for
every PR would be uniform but wasteful: a PR whose two graded files are Telegram
tests has no reason to execute the 900-file unit lane.

THE CORE LANE IS THE RESIDUAL, AND THAT TOO IS DERIVED
-------------------------------------------------------
    gateway      vitest.gateway.config.ts
    channels     vitest.channels.config.ts
    extensions   vitest.extensions.config.ts
    unit         vitest.unit.config.ts
    e2e          vitest.e2e.config.ts
    core         vitest.config.ts <dirs>   <- everything the others leave behind

`core` is not a config file and not an npm script. At every commit in this range
the unit config excludes src/agents, src/commands and src/auto-reply, and
scripts/test-parallel.mjs runs only unit + extensions + gateway, so `pnpm test`
never touches those three trees. The core lane catches exactly the graded files
that NO enumerated lane claims -- it is defined by subtraction, not by a list of
directories, so it keeps working if the repo moves a tree into or out of a lane.
Its scope is the top two path segments of those residual files.

A graded file that the test patch ADDS does not exist in the run stage, so it
cannot be enumerated there. Its lane is then taken from the siblings in its
directory that the lane lists do contain, which keeps the same lanes active
across all three stages.

`e2e` is needed for one file and cannot be reached any other way: every other
config inherits `**/*.e2e.test.ts` in its exclude list from vitest.config.ts.
PR 42554's src/commands/models.list.e2e.test.ts is MODIFIED, not added, so it
exists at the base commit and is f2p-capable -- dropping the lane would not
merely lose an n2p, it would lose a fail-to-pass transition.

The e2e lane runs ONLY the .e2e.test.ts files the PR's own test patch touches.
A full pass over vitest.e2e.config.ts is timing-dependent (gateway sockets,
10s hook timeouts); a file that happens to pass in one stage and time out in
another manufactures a fail-to-pass out of nothing. Narrow beats flaky. That
narrowing is a rule applied identically to every instance, not a per-PR choice.

ONE VITEST INVOCATION PER LANE (rule 11)
-----------------------------------------
Each lane is a separate `vitest run`. A lane whose config or setup file blows
up can then only take itself down. Inside a lane vitest already isolates each
test file in its own worker, so a file that fails to load is reported as a
failed suite while its neighbours still run; the per-lane split guards the layer
above that -- the config, the setup file, the runner process -- which vitest
does not guard.

GRADED-FILE MANIFEST
--------------------
After the lanes finish, run-suites.sh checks that every graded test file appears
somewhere in the lane JSON and prints

    ===== openclaw-missing-graded lane-plan=<lanes> file=<path> =====

for any that does not. The check is skipped in the run stage, where files the
test patch adds legitimately do not exist yet. This turns a misrouted file --
the failure mode this whole config exists to prevent -- from a silent zero into
a line in the log. It never invents a result for the missing file.

THE HONEST BOUNDARY, stated rather than papered over
-----------------------------------------------------
Six PRs add or modify tests that import a symbol the FIX patch introduces. In
the test stage those files cannot be imported, so their tests neither run nor
enumerate; they appear in the fix stage only, which the harness classifies as
N2P. Nothing is invented for them:

    42240  splitTelegramHtmlChunks            src/telegram/format.test.ts
    42370  isValidExecSecretRefId             ref-contract.test.ts + 3 new files
    42501  isGeminiEmbedding2Model et al.     embeddings-gemini/batch-gemini
    42554  hasUsableCustomProviderApiKey,     model-auth.test.ts,
           isKnownEnvApiKeyMarker             model-auth-markers.test.ts
    43683  new module global-singleton.ts,    global-singleton.test.ts,
           __testing export on draft-stream   draft-stream.test.ts

42554 is the one worth naming twice: those two files are MODIFIED, so tests
that pass at baseline drop out of the test stage entirely and land in n2p
rather than p2p. That is the correct reading of what happened, not a defect.

Helpers the TEST patch itself supplies -- src/test-utils/secret-ref-test-vectors.ts
(42370) and test/helpers/import-fresh.ts (43683) -- exist in both the test and
fix stages and are NOT part of this boundary.

TEST IDENTIFIERS
----------------
Identifiers are emitted as `<repo-relative path> > <lane> > <full test name>`.
The path leads deliberately: report.py's `_test_name_matches_files()` matches a
JS/TS id by `test_name.startswith(file + " > ")`, so this shape keeps both the
n2p test-patch matcher and the reward-hacking guard operative. The
`vitest::lane::path::name` shape used by the 32706..38292 config puts the
literal "vitest" in front of the first "::", which makes that matcher unable to
hit any file and silently degrades it to always-true.

NO RE-INSTALL, NO PRUNING
--------------------------
None of the twenty patches touches package.json, pnpm-lock.yaml, any
vitest.*.config.ts or any workflow, and no test patch deletes a test file. The
dependency tree prepare.sh installs is the one all three stages run against,
and there is no retired suite to prune. No PR in this dataset reaches the `ui`
workspace, so no browser is installed.

STRUCTURE -- rule 9 (2026-09-03), which supersedes rules 4/5/8 on placement:

    base Dockerfile   toolchain, infra block, apt, corepack/pnpm, WORKDIR
                      /home/, git clone, CMD. NOTHING after the clone.
    PR Dockerfile     FROM base, COPY lines, ARG BASE_COMMIT, RUN prepare.sh,
                      WORKDIR, then the FULL hardening block with all four
                      asserts.
    prepare.sh        checkout, then the dependency install. No stripping.

Two harness details make that placement work, both checked in
multi_swe_bench/harness/image.py rather than assumed:

  * `DockerfileEnhancer.enhance()` returns the Dockerfile untouched when it
    already carries the BuildKit syntax directive (image.py:311-313). The base
    below emits that directive itself, so the enhancer never appends the
    hardening block to the shared base and never pins it to one PR.
  * The scrub runs in an Image-dependency layer, and those receive NO build
    args (build_dataset.py:623-628 passes REPO_URL/BASE_COMMIT only when
    `dependency()` returns a str). So the PR Dockerfile carries the sha itself
    as `ARG BASE_COMMIT="<sha>"`.

Check before running, inverted from rule 5 exactly as rule 9 requires:
`grep -c 'rev-list --all --count'` must be 0 in the base Dockerfile and 1 in
the PR Dockerfile.

Every generated artifact ships WITHOUT comments (rule 10). The reasoning for
each one sits in a Python comment directly above its string literal.
"""

import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "openclaw_43683_to_41662"

_PLUS_LINE_RE = re.compile(r"^\+\+\+ b/(\S+)\s*$", re.MULTILINE)

_TEST_FILE_RE = re.compile(r"\.test\.[cm]?[jt]sx?$")

_E2E_FILE_RE = re.compile(r"\.e2e\.test\.[cm]?[jt]sx?$")

# Slowest and most timing-sensitive lane last, so a lane that hangs cannot
# delay the lanes that carry the bulk of the graded files.
_LANE_ORDER = ("gateway", "channels", "extensions", "core", "unit", "e2e")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_JSON_FENCE_RE = re.compile(
    r"-----BEGIN_VITEST_JSON lane=(\S+)-----\n(.*?)\n-----END_VITEST_JSON-----",
    re.DOTALL,
)


def _test_files(test_patch: str) -> list[str]:
    return sorted(
        {
            path
            for path in _PLUS_LINE_RE.findall(test_patch or "")
            if _TEST_FILE_RE.search(path)
        }
    )


def _graded_files(pr: PullRequest) -> list[str]:
    return _test_files(pr.test_patch)


def _e2e_targets(pr: PullRequest) -> list[str]:
    return [path for path in _graded_files(pr) if _E2E_FILE_RE.search(path)]


class Openclaw43683To41662ImageBase(Image):

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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base-43683_to_41662"

    def workdir(self) -> str:
        return "base-43683_to_41662"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        # The syntax directive on line 1 is the DockerfileEnhancer opt-out
        # (image.py:311). Without it the enhancer rewrites the clone below into
        # a per-PR checkout plus scrub, which would pin this shared base to one
        # PR and break the other nine. BASE_COMMIT is declared and never
        # referenced: declaring it silences BuildKit's unused-arg warning,
        # consuming it would pin the base. The shallow window starts well
        # before the oldest base commit in this era (2026-03-09); prepare.sh
        # still carries a per-sha fetch fallback for anything outside it.
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
    DO_NOT_TRACK=1 \\
    OPENCLAW_TELEMETRY_DISABLED=1 \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    NODE_OPTIONS=--max-old-space-size=4096

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
        git \\
        python3 \\
        make \\
        g++ \\
        ca-certificates \\
    && apt-get clean \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g corepack@0.36.0 && corepack enable
{global_block}
WORKDIR /home/

RUN git config --global http.version HTTP/1.1 \\
    && git config --global http.postBuffer 524288000 \\
    && for attempt in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git clone --shallow-since=2026-02-20 "${{REPO_URL}}" /home/{repo}; then break; fi; \\
        echo "clone attempt ${{attempt}} failed; retrying in 15s" >&2; \\
        sleep 15; \\
    done \\
    && test -d /home/{repo}/.git
{clear_block}
CMD ["/bin/bash"]
"""


class Openclaw43683To41662ImageDefault(Image):

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
        return Openclaw43683To41662ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        e2e_targets = " ".join(_e2e_targets(self.pr))
        graded = " ".join(_graded_files(self.pr))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
""",
            ),
            # The checkout is verified from both sides: the tree is clean
            # before it and clean after it. corepack takes pnpm from the
            # commit's own `packageManager` field, so the pinned 10.23.0 is
            # used rather than whatever the image happens to ship. The install
            # is retried once because a single transient registry failure
            # should not cost the whole image, and then once more WITHOUT
            # --frozen-lockfile, because one base commit in this era cannot
            # satisfy a frozen install at all:
            #
            #     43520  base 0e397e62 = "chore: bump version to 2026.3.10"
            #            ERR_PNPM_OUTDATED_LOCKFILE ... <ROOT>/extensions
            #            openclaw (lockfile: workspace:*, manifest: >=2026.3.7)
            #
            # The base commit IS the version-bump commit, and the bump landed
            # without regenerating pnpm-lock.yaml. The repo's own CI exposes
            # exactly this switch -- .github/actions/setup-node-env takes a
            # `frozen-lockfile` input and drops the flag when it is "false" --
            # so the fallback mirrors upstream rather than inventing an escape
            # hatch. Era-pinning is preserved everywhere it can be: pnpm still
            # resolves every satisfied specifier from the checked-in lockfile
            # and only re-resolves the one that disagrees, which here is the
            # workspace's own self-reference. The gate below still has to pass,
            # so a genuinely broken install cannot slip through. `canvas:a2ui:bundle` runs
            # because CI runs it before its test lanes, but it is best-effort:
            # the canvas-host suites write their own stub bundle when the real
            # one is missing -- and the gate below re-checks everything that
            # matters, so the tolerance cannot hide a broken provision.
            #
            # Section order is deliberate. Both clean-tree assertions sit in
            # section 1, before the install; none appears after it, because
            # section 2 is allowed to leave the tree dirty. The gate is section
            # 3: it asserts the vitest config file for every lane this PR will
            # run actually exists at this commit, that node_modules exists, and
            # that the runner resolves and executes. None of it is tolerated --
            # a failure here fails the image build, loudly, instead of
            # surfacing later as an empty lane that reads like a clean pass.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git remote add origin https://github.com/{org}/{repo}.git 2>/dev/null || true
git rev-parse --verify --quiet "{sha}^{{commit}}" >/dev/null 2>&1 \\
    || git fetch --depth=1 origin {sha} 2>/dev/null \\
    || git fetch origin 2>/dev/null || true
git checkout --detach --force {sha}
bash /home/check_git_changes.sh

corepack enable
node --version
corepack pnpm --version

export CI=true

PNPM_ARGS="--ignore-scripts=false --config.engine-strict=false --config.enable-pre-post-scripts=true"

corepack pnpm install --frozen-lockfile $PNPM_ARGS \\
    || corepack pnpm install --frozen-lockfile $PNPM_ARGS \\
    || {{ echo "prepare: lockfile is stale at this commit; retrying unfrozen" >&2; \\
          corepack pnpm install --no-frozen-lockfile $PNPM_ARGS; }}

corepack pnpm canvas:a2ui:bundle || \\
    echo "prepare: a2ui bundle failed; the suites stub it themselves" >&2

git checkout -- .
git status --porcelain

test -f vitest.config.ts

test -d node_modules
VITEST_VERSION="$(corepack pnpm exec vitest --version)"
test -n "$VITEST_VERSION"
echo "prepare: vitest ${{VITEST_VERSION}}"
echo "DEPS_OK"
""".format(repo=repo, org=org, sha=sha),
            ),
            # One `vitest run` per lane, each writing its own JSON report, so a
            # lane that dies from a config or setup error cannot take the other
            # lanes with it. The verbose reporter stays on for a human-readable
            # log; the JSON reporter is what parse_log consumes. The fence is
            # printed even when the output file is missing so a dead lane is
            # visible in the log rather than merely absent.
            #
            # The lane's exit status is INSPECTED, not discarded. A non-zero
            # exit is the normal, expected outcome of the test stage -- the
            # bug-catching tests are supposed to fail there -- so `set -e`
            # cannot govern this loop without breaking the three-stage
            # contract. What must not happen is the two failure shapes reading
            # alike, so they are separated: `openclaw-lane-failed rc=<n>` means
            # the suite ran and something failed, `openclaw-lane-no-output`
            # means the runner never produced a report at all, which is a
            # broken lane rather than a failing test.
            #
            # The graded-file check runs in the test and fix stages only. In
            # the run stage the test patch has not been applied, so files it
            # adds are legitimately absent and a missing-file line would be
            # noise. A line here means a graded file was executed by no lane --
            # the misroute this config exists to prevent -- and it is reported,
            # never substituted with a result.
            File(
                ".",
                "run-suites.sh",
                """#!/bin/bash
set -uo pipefail

export CI=true
cd /home/{repo}

STAGE="${{1:-run}}"
GRADED="{graded}"
E2E_TARGETS="{e2e_targets}"
LANE_ORDER="gateway channels extensions unit e2e"

lane_config() {{
    case "$1" in
        gateway)    echo vitest.gateway.config.ts ;;
        channels)   echo vitest.channels.config.ts ;;
        extensions) echo vitest.extensions.config.ts ;;
        unit)       echo vitest.unit.config.ts ;;
        e2e)        echo vitest.e2e.config.ts ;;
    esac
}}

rm -f /home/vitest-*.json /home/lane-*.files

for lane in $LANE_ORDER; do
    cfg="$(lane_config "$lane")"
    [ -f "$cfg" ] || continue
    if corepack pnpm exec vitest list --config "$cfg" --filesOnly > "/home/lane-${{lane}}.files" 2>/dev/null; then
        echo "===== openclaw-lane-enumerated: ${{lane}} $(grep -c . "/home/lane-${{lane}}.files") files ====="
    else
        echo "===== openclaw-lane-enumeration-failed: ${{lane}} ====="
        rm -f "/home/lane-${{lane}}.files"
    fi
done

ACTIVE=""
CORE_FILES=""
for file in $GRADED; do
    hit=0
    for lane in $LANE_ORDER; do
        list="/home/lane-${{lane}}.files"
        [ -f "$list" ] || continue
        if grep -qxF -- "$file" "$list"; then
            ACTIVE="$ACTIVE $lane"
            hit=1
        fi
    done

    if [ "$hit" -eq 0 ]; then
        case "$file" in
            *.e2e.test.ts) fallback_lanes="e2e" ;;
            *)             fallback_lanes="gateway channels extensions unit" ;;
        esac
        dir="$(dirname "$file")"
        for lane in $fallback_lanes; do
            list="/home/lane-${{lane}}.files"
            [ -f "$list" ] || continue
            if grep -q -- "^${{dir}}/" "$list"; then
                ACTIVE="$ACTIVE $lane"
                hit=1
            fi
        done
    fi

    [ "$hit" -eq 0 ] && CORE_FILES="$CORE_FILES $file"
done

CORE_SCOPE=""
for file in $CORE_FILES; do
    seg="$(echo "$file" | cut -d/ -f1-2)"
    case " $CORE_SCOPE " in *" $seg "*) ;; *) CORE_SCOPE="$CORE_SCOPE $seg" ;; esac
done
[ -n "$CORE_SCOPE" ] && ACTIVE="$ACTIVE core"

LANES=""
for lane in gateway channels extensions core unit e2e; do
    case " $ACTIVE " in *" $lane "*) LANES="$LANES $lane" ;; esac
done
LANES="$(echo $LANES)"

echo "===== openclaw-lane-plan: ${{LANES:-<none>}} core-scope:${{CORE_SCOPE:-<none>}} ====="

for lane in $LANES; do
    case "$lane" in
        gateway)    set -- --config vitest.gateway.config.ts --pool=forks ;;
        channels)   set -- --config vitest.channels.config.ts ;;
        extensions) set -- --config vitest.extensions.config.ts ;;
        core)       set -- --config vitest.config.ts $CORE_SCOPE ;;
        unit)       set -- --config vitest.unit.config.ts ;;
        e2e)        [ -n "$E2E_TARGETS" ] || {{ echo "===== openclaw-lane-skipped: e2e (no graded e2e file) ====="; continue; }}
                    set -- --config vitest.e2e.config.ts $E2E_TARGETS ;;
        *)          echo "run-suites: unknown lane '$lane'" >&2; continue ;;
    esac

    out="/home/vitest-${{lane}}.json"

    echo "===== openclaw-lane: ${{lane}} ====="
    if corepack pnpm exec vitest run "$@" --reporter=verbose --reporter=json --outputFile="$out" --silent=passed-only
    then
        echo "===== openclaw-lane-ok: ${{lane}} ====="
    else
        echo "===== openclaw-lane-failed: ${{lane}} rc=$? ====="
    fi

    if [ ! -f "$out" ]; then
        echo "===== openclaw-lane-no-output: ${{lane}} ====="
    fi

    echo "-----BEGIN_VITEST_JSON lane=${{lane}}-----"
    if [ -f "$out" ]; then cat "$out"; fi
    echo
    echo "-----END_VITEST_JSON-----"
done

if [ "$STAGE" != "run" ]; then
    for file in $GRADED; do
        if ! grep -qF -- "$file" /home/vitest-*.json 2>/dev/null; then
            echo "===== openclaw-missing-graded lane-plan=${{LANES// /,}} file=${{file}} ====="
        fi
    done
fi

exit 0
""".format(
                    repo=repo,
                    graded=graded,
                    e2e_targets=e2e_targets,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

bash /home/run-suites.sh run
""".format(repo=repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

bash /home/run-suites.sh test
""".format(repo=repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi

bash /home/run-suites.sh fix
""".format(repo=repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "Openclaw43683To41662ImageDefault needs an Image dependency"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # BASE_COMMIT is carried as a literal ARG here rather than passed in:
        # build_dataset.py:623-628 supplies REPO_URL/BASE_COMMIT only to images
        # whose dependency() is a str, and this one depends on an Image.
        return f"""FROM {name}:{tag}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("openclaw", _INTERVAL_NAME)
class OPENCLAW_43683_TO_41662(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Openclaw43683To41662ImageDefault(self.pr, self._config)

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
        # The JSON reporter writes to a file which run-suites.sh cats back out,
        # so the fenced payload is normally clean -- but the verbose reporter on
        # the same stream is coloured, and a single escape byte landing inside a
        # fence would make json.loads() drop that lane's entire report in
        # silence. Stripping first costs nothing and removes the failure mode.
        test_log = _ANSI_RE.sub("", test_log or "")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        repo_prefix = f"/home/{self.pr.repo}/"

        for match in _JSON_FENCE_RE.finditer(test_log):
            lane = match.group(1)
            try:
                report = json.loads(match.group(2))
            except ValueError:
                continue

            for suite in report.get("testResults") or []:
                path = suite.get("name") or ""
                if repo_prefix in path:
                    path = path.split(repo_prefix, 1)[1]

                assertions = suite.get("assertionResults") or []

                # A suite that produced no assertions never got as far as
                # running a test: a collection error, a setup throw, an import
                # of a symbol the fix patch has not introduced yet. It is
                # recorded under its own identifier so the stage shows the file
                # was reached and failed, rather than the file vanishing.
                if not assertions:
                    ident = f"{path} > {lane} > <suite failed to load>"
                    if suite.get("status") == "passed":
                        passed_tests.add(ident)
                    else:
                        failed_tests.add(ident)
                    continue

                # vitest reports repeated names (`test.each`, a title reused
                # across two describes) verbatim. Numbering the repeats keeps
                # them distinct instead of collapsing them into one set member.
                seen: dict[str, int] = {}
                for assertion in assertions:
                    name = assertion.get("fullName") or assertion.get("title") or ""
                    name = " ".join(name.split())
                    seen[name] = seen.get(name, 0) + 1
                    suffix = "" if seen[name] == 1 else f"#{seen[name]}"
                    ident = f"{path} > {lane} > {name}{suffix}"

                    status = assertion.get("status")
                    if status == "passed":
                        passed_tests.add(ident)
                    elif status == "failed":
                        failed_tests.add(ident)
                    else:
                        skipped_tests.add(ident)

        # A name that failed anywhere is failed, and a name that ran anywhere is
        # not skipped. Only reachable when an unknown-shape PR runs a file in
        # two lanes, but the precedence is fixed here rather than left to set
        # ordering.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
