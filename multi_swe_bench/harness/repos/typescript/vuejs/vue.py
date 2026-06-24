import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# vuejs/vue has two distinct toolchain eras (Docker-verified 2026-05-18 on arm64):
#
#   Era A — Vue 2.1.x .. 2.6.x  (PRs <= 12455, release_line 2.1..2.6)
#     test:unit = `karma start <build|test/unit>/karma.unit.config.js`
#     Karma + Jasmine, runs in a real browser. Built with webpack 1.x..4.x.
#     Run headlessly with system Chromium via a generated karma override that
#     also swaps in a flat, fully-qualified custom reporter.
#
#   Era C — Vue 2.7.x  (PRs > 12455, release_line 2.7)
#     test:unit = `vitest run test/unit`, package manager pnpm (via corepack).
#
# Boundary 12455: no dataset PRs fall in (11906, 12563); kept for continuity.
# The dataset JSONL has no `number_interval`, so the harness routes every
# record by `vuejs/vue`; a single registration with PR-number-conditional
# dependency is therefore required (multi-file/number_interval would leave
# every record unroutable). Each era still has its own ImageBase/ImageDefault
# and a unique image_tag, and parse_log handles both reporters.
#
# Docker-verified end-to-end (run -> test-run -> fix-run, valid f2p):
#   Era A: v2.1.8(790) v2.3.0(892) v2.5.0(1035) v2.5.11(1070) v2.6.13(1292);
#          PR 11906 f2p: "Component slot > v-if inside scoped slot"
#          absent -> TESTFAIL -> TESTPASS.
#   Era C: v2.7.0(1417) v2.7.15(ok); PR 12563 f2p: 2 reactivity specs
#          NONE -> fail -> pass.

# Headless-Chrome karma override placed *beside* the repo's own
# karma.unit.config.js so the base config's relative `./index.js` /
# `./karma.base.config.js` patterns resolve identically. Notes:
#  * custom launcher uses base "Chrome" + explicit --headless so it works with
#    old karma-chrome-launcher 2.0.0 (Vue 2.1) and 3.x (Vue 2.6) alike;
#  * the plugin list is pinned so karma does NOT auto-discover the broken
#    karma-phantomjs-launcher (phantomjs has no arm64 binary) — these package
#    names exist throughout Vue 2.1.x..2.6.x devDependencies;
#  * an inline reporter emits one flat line per spec with a fully-qualified
#    name (suite path + description) and an unambiguous TESTPASS/TESTFAIL/
#    TESTSKIP marker. This avoids karma-mocha-reporter's leaf-only name
#    collisions, indentation scraping and summary-line ambiguity, and keeps
#    names identical across run/test/fix stages. Stable across karma 1.x-6.x.
KARMA_HEADLESS_JS = r"""const base = require("./karma.base.config.js")

function FlatReporter(baseReporterDecorator) {
  baseReporterDecorator(this)
  const self = this
  this.onSpecComplete = function (browser, result) {
    const suite = (result.suite || []).join(" > ")
    const name = suite ? suite + " > " + result.description : result.description
    const tag = result.skipped ? "TESTSKIP" : (result.success ? "TESTPASS" : "TESTFAIL")
    self.write(tag + " " + name + "\n")
  }
}
FlatReporter.$inject = ["baseReporterDecorator"]

module.exports = function (config) {
  config.set(Object.assign({}, base, {
    browsers: ["ChromeHeadlessCI"],
    customLaunchers: {
      ChromeHeadlessCI: {
        base: "Chrome",
        flags: [
          "--headless",
          "--no-sandbox",
          "--disable-gpu",
          "--disable-dev-shm-usage",
          "--remote-debugging-port=9222"
        ]
      }
    },
    reporters: ["flat"],
    singleRun: true,
    plugins: [
      "karma-jasmine",
      "karma-webpack",
      "karma-sourcemap-loader",
      "karma-chrome-launcher",
      { "reporter:flat": ["type", FlatReporter] }
    ]
  }))
}
"""


class ImageBaseKarma(Image):
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
        return "node:12-bullseye"

    def image_tag(self) -> str:
        return "base-karma"

    def workdir(self) -> str:
        return "base-karma"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # SHARED base (tag base-karma), NOT per-PR. # syntax opts OUT of
        # DockerfileEnhancer auto-injection (no per-PR checkout/gc-prune in base).
        # Clone full history once; per-PR layer checks out base.sha (prepare.sh) +
        # full hardening. Light hardening here (remove remote). No proxy/cert/MITM.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org, repo = self.pr.org, self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git jq chromium \\
    && rm -rf /var/lib/apt/lists/*
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class ImageDefaultKarma(Image):
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
        return ImageBaseKarma(self.pr, self._config)

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
                "karma.headless.js",
                KARMA_HEADLESS_JS,
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
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Place the headless-Chrome karma override beside the repo's unit config so
# the base config's relative paths resolve. Vue 2.1-2.3 keep it in build/,
# Vue 2.4+ in test/unit/.
if [ -f test/unit/karma.unit.config.js ]; then
  cp /home/karma.headless.js test/unit/karma.headless.js
else
  cp /home/karma.headless.js build/karma.headless.js
fi

# phantomjs-prebuilt / puppeteer have no arm64 binary and are unused here
# (we drive Chromium directly), so skip lifecycle scripts and tolerate failure.
export PUPPETEER_SKIP_DOWNLOAD=true
yarn install --ignore-scripts || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"

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

        prepare_commands = "RUN bash /home/prepare.sh"
        # Full per-PR hardening — prepare.sh has already checked out base.sha; strip
        # refs/reflog + gc-prune so future/fix commits are unreachable & deleted,
        # then audit (HEAD==base.sha, refs=0, no remote, rev-list all==HEAD).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageBaseVitest(Image):
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
        return "node:16-bookworm"

    def image_tag(self) -> str:
        return "base-vitest"

    def workdir(self) -> str:
        return "base-vitest"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # SHARED base (tag base-vitest), NOT per-PR. The `# syntax` directive opts
        # OUT of DockerfileEnhancer auto-injection (which would otherwise add a
        # per-PR `git checkout ${BASE_COMMIT}` + gc-prune, pinning this shared base
        # to one commit). Clone full history ONCE; the per-PR layer checks out
        # base.sha in prepare.sh and runs the full hardening. Light hardening here
        # (remove remote) so the model cannot fetch the fix. No proxy/cert/MITM.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org, repo = self.pr.org, self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git jq \\
    && rm -rf /var/lib/apt/lists/*
RUN git clone "${{REPO_URL}}" /home/{repo}
RUN corepack enable

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class ImageDefaultVitest(Image):
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
        return ImageBaseVitest(self.pr, self._config)

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
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# corepack resolves the pnpm version from package.json packageManager
# (pnpm@7.1.0 for v2.7.0-2.7.14, pnpm@8.9.2 for v2.7.15+).
corepack enable
# puppeteer chromium has no arm64 binary and is e2e-only; skip + tolerate.
export PUPPETEER_SKIP_DOWNLOAD=true
pnpm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
pnpm exec vitest run test/unit --reporter verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
pnpm exec vitest run test/unit --reporter verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm exec vitest run test/unit --reporter verbose

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

        prepare_commands = "RUN bash /home/prepare.sh"
        # Full per-PR hardening — prepare.sh has already checked out base.sha; strip
        # refs/reflog + gc-prune so future/fix commits are unreachable & deleted,
        # then audit (HEAD==base.sha, refs=0, no remote, rev-list all==HEAD).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("vuejs", "vue")
class Vue(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        # Era A: Vue 2.1-2.6 (Karma/Jasmine, headless Chromium)
        # Era C: Vue 2.7   (Vitest + pnpm)
        if self.pr.number <= 12455:
            return ImageDefaultKarma(self.pr, self._config)

        return ImageDefaultVitest(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI/colour first — karma + webpack emit coloured output.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Era A: custom karma reporter -> "TESTPASS|TESTFAIL|TESTSKIP <name>"
        #   where <name> is the fully-qualified "suite > ... > description".
        re_marker = re.compile(r"^(TESTPASS|TESTFAIL|TESTSKIP) (.+?)\s*$")
        # Era C: vitest --reporter verbose -> "√|✓ <name>" pass, "× <name>"
        #   fail, "↓ <name>" skipped; <name> = "file.spec.ts > suite > test".
        re_pass = re.compile(r"^[√✓] (.+?)\s*$")
        re_fail = re.compile(r"^× (.+?)\s*$")
        re_skip = re.compile(r"^↓ (.+?)\s*$")
        # Guard against any reporter summary line being captured as a name.
        re_summary = re.compile(
            r"^\d+\s+(passed|failed|skipped)|tests?\s+(completed|passed|failed)"
        )

        for line in log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_marker.match(line)
            if m:
                tag, name = m.group(1), m.group(2)
                if tag == "TESTPASS":
                    passed_tests.add(name)
                elif tag == "TESTFAIL":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            m = re_pass.match(line)
            if m and not re_summary.match(m.group(1)):
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m and not re_summary.match(m.group(1)):
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m and not re_summary.match(m.group(1)):
                skipped_tests.add(m.group(1))

        # Enforce TestResult disjoint-set invariants (a test reported both
        # pass and fail — e.g. flaky/retried — counts as failed).
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_Vue = [
    '11906-12002-12104',
    '12563-12564-12601-12605',
    '12754-12757',
    '12979-12985-13053-13067-13107',
    '5267-5284',
    '5539-5540-5549-5564',
    '5589-5593-5597-5607-5610-5625-5627-5633-5640',
    '6213-6220-6223-6274-6325-6344-6346-6363-6369-6374-6414-6441-6450-6467-6473-6477-6511-6517-6530-6535-6547-6549-6570-6571-6573-6576-6577',
    '6794-6795-6796-6802',
    '7002-7023-7024-7026-7046-7059',
    '7080-7085',
    '7098-7113-7126-7135',
    '7154-7156-7192-7200-7224',
    '7258-7271-7274',
    '9128-9191-9193-9275-9296',
    '9297-9331-9341-9365-9408',
    '9423-9424-9425-9431',
    '9489-9526-9537-9538',
    '10777-10969-10971-10981-10984-10985-11050-11064-11072-11077-11078-11080-11082-11085-11157-11179-11180-11185-11196-11200-11262-11267-11279-11280-11286-11365-11420-11425-11433-11434-11435-11481-11542-11554-11566-11570',
    '12646-12648',
    '12661-12725-12727-12732-12735-12737-12744-12747',
    '12677-12684-12687',
    '12759-12779-12792-12813',
    '12789-12834-12839-12840-12842-12863',
    '12851-12862-12872-12873-12878-12879-12894-12905-12911-12912-12927-12949-13010-13068-13070',
    '4626-4639-4642-4646-4653-4662-4672-4682-4684-4700-4723-4725',
    '4740-4747-4753-4756-4760-4770-4797-4807-4829-4844-4845-4848-4857-4859-4860-4863-4866-4890-4892-4899-4910-4925-4926-4928-4932-4945-4950-4954-4964-4995-5008',
    '5014-5024-5026-5039-5044-5047-5050-5054-5056-5059-5065-5072-5073-5082-5088-5091-5092-5093-5094-5101-5102-5103-5104-5106-5125-5127-5130',
    '5052-5216-5224-5229-5233-5243-5253',
    '5123-5143-5144-5147-5161-5164',
    '5132-5204-5322-5324-5383-5402-5411-5412-5426-5448-5460-5462-5481-5488-5508-5514',
    '5977-6322-6391-6618-6620-6621-6624-6627-6681-6704-6715-6716-6718-6729-6730-6731-6735-6736-6745-6760-6763-6769',
    '6819-6833-6837-6839-6860-6886-6905-6910-6927-6932-6939-6944-6949-6958-6992',
    '7121-7272-7286-7287-7291-7302-7308-7311-7314-7322-7350-7364-7369-7375-7391-7406-7452-7460-7491-7524-7530-7545-7549-7584-7596-7597-7600-7610-7641-7663-7665-7666-7671-7676-7687-7689-7704-7712-7733-7737-7738-7739-7755-7762-7781',
    '7127-7215-7926-7941-8047-8179-8217-8297-8395-8450-8638-8652-8666-9017-9199-9324-9343-9346-9371-9386-9400-9405',
    '9139-9154-9165-9170-9171-9172-9184',
    '9241-9484-9785-10088-10119-10167-10396-10529-10845-10958-10990-11052-11065-11107-11112-11121-11152-11189-11191-11326-11392-11487-11522-11576-11609-11616-11620-11651-11653-11692-11704-11705-11706-11723-11784-11785-11791-11795-11825-11843-11850-11865-11866-11883-11914-11943-11946-11947-11948-11962-11963-11964-11987-11996-11997-12004-12014-12015-12021-12047-12054-12064',
    '9508-9541-9550-9563-9572-9585-9588-9595-9598',
    '9525-9619-9621-9622-9624-9647-9653-9660-9667-9668-9673-9675-9677-9680-9684',
    '9649-9700-9709-9728-9733-9738',
    '9804-9827-9828-9834-9845-9846-9848-9858-9860-9884-9907-9909-9912-9914-9917-9922-9933-9934-9970-9974-9995-10031-10064-10101-10113-10130-10136-10158-10181-10232-10233-10242-10257-10319-10327-10353-10355-10358-10380-10467-10474-10531-10534-10556-10585-10587-10589-10598-10599-10607-10630-10631-10632-10633-10634-10635-10636-10638-10750-10779-10799-10800-10821-10841-10895-10896-10901-10914',
]
for _ni in _BUNDLE_NIS_Vue:
    Instance.register('vuejs', _ni)(Vue)
