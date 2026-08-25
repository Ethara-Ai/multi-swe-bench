import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The PR under test lives entirely in this workspace package: the fix patch touches
# packages/dev-server-core/src/** and the test patch touches packages/dev-server-core/test/**.
# Running the whole monorepo's suite would add thousands of unrelated tests, make every stage
# far slower, and let an unrelated flake in another package masquerade as a regression.
PACKAGE = "packages/dev-server-core"


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

    def dependency(self) -> str:
        # node:14, NOT a current LTS. This repo's CI at the base commit runs a
        # [12.x, 14.x] matrix, and the toolchain is 2020-era: mocha ^8.1.1, ts-node ^8.10.2,
        # typescript ^4.0.0. Modern Node breaks ts-node 8 outright, and the surrounding
        # dependency tree was never resolved against it. 14 is the newest version this
        # commit was actually tested on.
        #
        # Single layer, deliberately: docker_util._get_container_builder() routes any build
        # with a platform set through the docker-container buildx driver, which cannot see
        # images loaded into the local daemon, so a `FROM <our-own-base>` split is
        # unbuildable here. Returning a str also keeps DockerfileEnhancer engaged, which is
        # what performs the BASE_COMMIT checkout and history scrub.
        return "node:14"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # ONE MOCHA PROCESS PER TEST FILE, not one run for the whole suite. This is the
        # single most important decision in this config, and it solves three problems that
        # each independently break the instance:
        #
        # 1. TEST IDS MUST CARRY THE FILE PATH. report.py's `_test_name_matches_files` does
        #    `test_name.split("::", 1)[0]` and compares that head against the test patch's
        #    file list. The 4 tests this PR adds are run=NONE/test=NONE/fix=PASS, and Rule 6
        #    only credits that shape as N2P when the matcher hits; otherwise they are treated
        #    as phantoms and dropped. mocha's xunit reporter emits classname/name but NO file,
        #    so a whole-suite run yields ids like "WebSocketsManager.should ..." which can
        #    never match, and the instance comes back unresolved. Running per file lets the
        #    real repo-relative path be emitted as a marker and prefixed onto every id.
        #
        # 2. A SINGLE UNLOADABLE FILE MUST NOT DESTROY THE STAGE. mocha loads all files
        #    up front, so one TypeScript compile error aborts the entire run and writes no
        #    XML at all. At the test stage the new tests reference `injectWebSocket`, a type
        #    the FIX patch introduces, so the whole suite dies and all 71 pre-existing tests
        #    report NONE. Per file, only the 3 new files fail and the other 71 tests still
        #    report honestly as PASS.
        #
        # 3. DUPLICATE TEST NAMES. Measured at baseline: classname="transformImports()" /
        #    name="does not change import with string concatenation cannot be resolved"
        #    appears twice. Without a file prefix those collapse into one id and one result
        #    masks the other.
        #
        # --reporter-options output=<file> writes XML to disk instead of stdout, which
        # matters because these tests boot real dev servers that log to stdout and would
        # otherwise corrupt a structured report.
        #
        # --exit mirrors the repo's own script: the tests open real HTTP servers and sockets,
        # and mocha would otherwise hang after the last test instead of exiting.
        #
        # --timeout 30000 raises mocha's 2s default. The suite boots real servers and does
        # real socket I/O, markedly slower in a container than on CI metal; without this,
        # slow-but-correct tests fail as timeouts. Applied identically in all three stages,
        # so it cannot manufacture a transition.
        #
        # --retries 3 matches the repo's own CI script. Applied identically across stages, it
        # suppresses flake noise without hiding a genuine failure (a real failure still fails
        # all four attempts).
        # The hoisted binary is invoked by ABSOLUTE PATH, never `npx mocha`. This is a yarn
        # workspace: yarn hoists mocha to <root>/node_modules/.bin, but these scripts cd into
        # packages/dev-server-core, whose own node_modules/.bin has no mocha. npx then finds
        # nothing locally and silently DOWNLOADS a fresh mocha into /root/.npm/_npx/..., and
        # that copy resolves modules relative to itself, so `--require ts-node/register` dies
        # with MODULE_NOT_FOUND and no XML is written at all. Measured: npx path produced zero
        # testcases, absolute path produced 71.
        repo = self.pr.repo
        cmd = (
            "cd /home/" + repo + "/" + PACKAGE + "\n"
            "rm -rf /tmp/xml && mkdir -p /tmp/xml\n"
            "find test \\( -name '*.test.ts' -o -name '*.test.js' "
            "-o -name '*.test.mjs' -o -name '*.test.cjs' \\) | sort | while read -r f; do\n"
            '  out="/tmp/xml/$(echo "$f" | tr "/" "_").xml"\n'
            "  /home/" + repo + '/node_modules/.bin/mocha "$f" '
            "--require ts-node/register --exit --retries 3 --timeout 30000 "
            '--reporter xunit --reporter-options output="$out" > /dev/null 2>&1 || true\n'
            '  echo "===FILE=== ' + PACKAGE + '/$f"\n'
            '  cat "$out" 2>/dev/null || echo "(no xml produced for this file)"\n'
            "done"
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{cmd}
""".format(cmd=cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image}

{self.global_env}

# npm/yarn draw progress bars and colourised output with non-ASCII characters. The harness
# decodes buildx output with the platform default codec (cp1252 on Windows), where those
# bytes are undefined and abort the build with "'charmap' codec can't decode byte ...".
ENV NPM_CONFIG_PROGRESS=false
ENV NPM_CONFIG_COLOR=false
ENV NO_COLOR=1
ENV FORCE_COLOR=0
# Many JS suites change behaviour without this - skipping tests, adding watch mode, or
# prompting. mocha also uses it to disable interactive output.
ENV CI=true

# No apt install. node:14 is built on buildpack-deps:buster and already ships everything
# needed here - git 2.20.1, yarn 1.22.19, and a C toolchain for any native module builds.
#
# Running apt at all would BREAK the build: Debian buster is end-of-life and its suites have
# been moved to archive.debian.org, so `apt-get update` against deb.debian.org now returns
# 404 for buster, buster-updates and buster/updates, and exits 100. Pinning an older Node to
# match this repo's era means inheriting an EOL base, so the rule here is simply not to
# depend on apt. If a package is ever genuinely needed, switch to node:14-bullseye (verified
# available) rather than pointing sources.list at the archive mirror.

WORKDIR /home/

{code}

# DockerfileEnhancer rewrites the clone above and appends its own WORKDIR, reset --hard and
# checkout BASE_COMMIT, then the history-scrub block whose assertions fail the build unless
# HEAD is exactly BASE_COMMIT. Repeating any of that here would be dead code. The WORKDIR is
# kept so the install steps below do not depend silently on the enhancer's line ordering.
WORKDIR /home/{self.pr.repo}

# yarn, not npm: the repo ships yarn.lock and declares yarn workspaces ("packages/*"). npm
# would resolve a different tree, so the three graded stages could each get different
# dependency versions and the f2p diff would measure dependency drift rather than the patch.
# --frozen-lockfile makes a lockfile that no longer matches package.json a hard error rather
# than a silent re-resolve.
#
# --ignore-scripts is deliberate. This tree's postinstall runs patch-package, and other
# workspace packages pull puppeteer, which downloads a browser this image will never use and
# routinely fails or hangs in a container. Neither is needed: the graded tests are
# dev-server-core's Node tests, and the only patch present targets @lion/overlays, a
# docs-site dependency this package never imports.
# --network-timeout 600000 (10 min, up from yarn's 30s default) is required for the arm64
# leg of the multi-arch build. Installing this monorepo under QEMU emulation is slow enough
# that yarn gives up mid-download with "There appears to be trouble with your network
# connection. Retrying..." and eventually exits 1, failing the whole buildx run even though
# amd64 succeeded. It changes only how long yarn waits, never what it resolves, so the two
# architectures still install an identical tree.
#
# --network-concurrency 4 (down from 8) for the same reason: fewer simultaneous sockets is
# markedly more reliable under emulation.
RUN yarn install --frozen-lockfile --ignore-scripts \\
    --network-timeout 600000 --network-concurrency 4

# The FIX patch adds two dependencies to packages/dev-server-core/package.json:
#   +  "@types/ws": "^7.2.6"
#   +  "ws": "^7.3.1"
# yarn install above runs against the BASE manifest, so neither is present when the fix
# stage compiles src/web-sockets/WebSocketsManager.ts, which fails with
# "TS7016: Could not find a declaration file for module 'ws'" and produces zero tests.
#
# They are installed NESTED under the package rather than at the root, which is exactly
# what yarn itself would do: the root already hoists ws@3.3.3 for another dependant, and
# Node resolution walks upward, so a nested copy gives dev-server-core ws@7 while every
# other package keeps ws@3. Overwriting the root copy would break those.
#
# Tarballs are unpacked directly instead of using a package manager. `npm install --no-save`
# was tried and REJECTED: it re-plans the whole tree, and it silently evicted
# @types/is-stream and @types/get-stream, taking the baseline from 71 tests to zero.
# npm pack only downloads, so the existing yarn tree is left untouched.
#
# Nothing here edits package.json. That is deliberate - the fix patch rewrites that file,
# so any build-time edit would make `git apply` conflict and fail the whole stage.
# node_modules/ is gitignored, so the working tree stays clean for the patches.
RUN mkdir -p /tmp/pk && cd /tmp/pk \\
    && npm pack ws@7.3.1 > /dev/null 2>&1 \\
    && npm pack @types/ws@7.2.6 > /dev/null 2>&1 \\
    && mkdir -p /home/{self.pr.repo}/{PACKAGE}/node_modules/ws \\
                /home/{self.pr.repo}/{PACKAGE}/node_modules/@types/ws \\
    && tar xzf /tmp/pk/ws-7.3.1.tgz -C /home/{self.pr.repo}/{PACKAGE}/node_modules/ws --strip-components=1 \\
    && tar xzf /tmp/pk/types-ws-7.2.6.tgz -C /home/{self.pr.repo}/{PACKAGE}/node_modules/@types/ws --strip-components=1 \\
    && rm -rf /tmp/pk

# Refuse to seal an image whose graded stages could not report anything. A missing mocha or
# ts-node yields an empty log, which reads downstream as "these tests do not exist" rather
# than as a broken image, and the harness scores that as a valid n2p-only resolve.
#
# The test directory is asserted too: if the workspace layout ever moves, the mocha glob
# would match nothing and every stage would report zero tests while still "succeeding".
RUN ./node_modules/.bin/mocha --version > /dev/null \\
    && node -e "require.resolve('ts-node')" > /dev/null \\
    && test -d {PACKAGE}/test

WORKDIR /home/

{copy_commands}
{self.clear_env}

"""


@Instance.register("modernweb-dev", "web")
class Web(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        # The run script emits one "===FILE=== <repo-relative path>" marker per test file,
        # followed by that file's xunit XML. Ids are built as "<file>::<describe> > <test>".
        #
        # The "::" separator is REQUIRED, not cosmetic: report.py's _test_name_matches_files
        # does test_name.split("::", 1)[0] and compares that head against the patch's file
        # list, which is what lets a newly added test be credited as N2P instead of silently
        # discarded as a phantom.
        marker_re = re.compile(r"^===FILE===\s+(\S+)\s*$")

        # Attribute ORDER must not matter. mocha's xunit reporter emits classname before
        # name; other runners emit name first. A regex hardcoding one order silently drops
        # every testcase from the other, which reads downstream as "those tests do not exist"
        # rather than as a parse failure.
        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        attr_re = re.compile(r'\b(name|classname)="([^"]*)"')

        def unescape(s: str) -> str:
            # &amp; LAST, or "&amp;lt;" is unescaped twice into "<".
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                         ("&apos;", "'"), ("&amp;", "&")):
                s = s.replace(a, b)
            return s

        # Split the log into (file, chunk) sections on the markers, so every testcase is
        # attributed to the file whose mocha process produced it.
        sections: list[tuple[str, str]] = []
        current_file = ""
        buf: list[str] = []
        for line in log.split("\n"):
            m = marker_re.match(line.strip())
            if m:
                if buf:
                    sections.append((current_file, "\n".join(buf)))
                current_file = m.group(1)
                buf = []
            else:
                buf.append(line)
        if buf:
            sections.append((current_file, "\n".join(buf)))

        for file_path, chunk in sections:
            for m in testcase_re.finditer(chunk):
                attrs = dict(attr_re.findall(m.group(1)))
                name = unescape(attrs.get("name", ""))
                classname = unescape(attrs.get("classname", ""))
                if not name and not classname:
                    continue
                closing, inner = m.group(2), m.group(3) or ""

                # classname is the describe chain; it is empty for a root-level it().
                # Measured at baseline: 65 of 71 cases carry one, 6 do not.
                local = f"{classname} > {name}" if classname else name
                test_id = f"{file_path}::{local}" if file_path else local

                if closing == "/>":
                    passed_tests.add(test_id)
                elif "<failure" in inner or "<error" in inner:
                    failed_tests.add(test_id)
                elif "<skipped" in inner:
                    skipped_tests.add(test_id)
                else:
                    passed_tests.add(test_id)

        # --retries means a flaky test can be reported more than once; enforce one bucket
        # each, or the stage comparison double-counts and invents transitions.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
