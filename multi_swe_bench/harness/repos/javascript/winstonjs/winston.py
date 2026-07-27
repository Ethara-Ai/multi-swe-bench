import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Apt packages installed into the base image before the repo is cloned.
DEFAULT_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]

# npm install for the test/fix phases.
#
# CRITICAL for dataset correctness: `npm install` prints an audit banner ending
# in "Run `npm audit` for details." at column 0. run.sh does NOT install, so
# that banner appeared ONLY in the test/fix logs. parse_log maps indentation to
# nesting depth, so the column-0 banner became the ROOT of the suite tree and
# every real test id gained a "Run `npm audit` for details.:" prefix -- in two
# of the three phases. The same test therefore had a different id at baseline
# than under test/fix, so it read as absent (NONE) then present (PASS), and got
# credited as a NONE->PASS "fix". That single asymmetry manufactured ~1053
# phantom transitions across the corpus.
#
# Sending install output to a file keeps stdout to test output only, so all
# three phases produce identical ids. A failure is still reported and aborts the
# phase (an empty result is honest; a fabricated pass is not).
_NPM_QUIET_INSTALL = """npm install --no-audit --no-fund > /tmp/npm-install.log 2>&1 || {
  echo "ERROR: npm install failed"; tail -40 /tmp/npm-install.log; exit 1;
}"""

# Mocha invocation shared by all three phases -- they MUST be identical, or the
# phases disagree about which tests exist and the diff is scored as a fix.
#
# --ignore: with a .mocharc setting `recursive: true` and no explicit spec,
# mocha loads every .js under test/, including non-test config files. On
# winston 3.18 that meant test/jest.config.integration.js, whose `require`
# of '../jest.config' threw MODULE_NOT_FOUND and aborted the whole run (pr-2467
# recorded 1 test out of 197). Config files are not tests; excluding them keeps
# the suite from dying on load.
#
# `test/helpers/scripts/*.js` MUST be repeated here even though every winston
# .mocharc already lists it under `exclude`. mocha's CLI --ignore is the same
# option as the config's `exclude`, and CLI REPLACES config rather than
# appending -- so passing --ignore silently dropped winston's own exclusion.
# Those helpers are scripts meant to be spawned as subprocesses; loading
# test/helpers/scripts/default-rejections.js as a test throws
# "TypeError: Cannot read properties of undefined (reading 'handle')" during
# file load and mocha collects ZERO tests. That regressed pr-1989/2181/2206/2226
# from ~175 tests to 0 in every phase. Keep this entry first.
_MOCHA_INVOCATION = """if [ -f .mocharc.yml ] || [ -f .mocharc.yaml ] || [ -f .mocharc.json ]; then
  npx mocha --ignore 'test/helpers/scripts/*.js' \\
            --ignore 'test/**/*.config.js' \\
            --ignore 'test/**/jest.config*.js'
else
  npx mocha test/*.test.js test/**/*.test.js --exit
fi
"""

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _safe_sha(sha: str) -> str:
    """Validate a commit SHA before it is interpolated into a Dockerfile RUN."""
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(f"unsafe base commit for Dockerfile interpolation: {sha!r}")
    return sha


def clone_and_harden(repo: str, url: str, sha: str) -> str:
    """Clone, pin to the base commit, and destroy all post-base-commit history
    -- in a SINGLE Docker layer.

    Why one layer: Docker layers are append-only. If the clone lands in one RUN
    and the prune in a later RUN, the pre-prune packfile -- which still contains
    every future commit, including the fix -- remains recoverable from the lower
    layer, and the hardening is cosmetic. Doing all of it in one RUN means no
    layer ever holds unpruned history.

    Why in the PR image and not the base: this pins the repo to ONE commit, so
    an image containing it can serve exactly one base SHA. Keeping it here lets
    every PR share a single toolchain-only base image (see ImageBase).

    What it removes: the remote, all refs (heads/remotes/tags/replace), both
    reflogs, and -- via `gc --prune=now` -- the unreachable objects themselves.
    A solver cannot recover the fix through `git log --all`, `git show <sha>`,
    `git cat-file`, `git fsck --lost-found`, the reflog, tags, or packed-refs;
    those objects are gone from the object store, not merely unreferenced.

    The four `test` assertions fail the build if any of that did not hold, so a
    silent hardening regression cannot ship as a usable image.

    Scope: this does NOT stop re-downloading the repo at eval time. Blocking
    `git remote add` + `git fetch` requires network egress control in the runner.
    """
    sha = _safe_sha(sha)
    return f"""RUN set -eux; \\
    git clone "{url}" /home/{repo}; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"; \\
    if [ -f .gitmodules ]; then \\
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

    def dependency(self) -> Union[str, "Image"]:
        return "node:20-bookworm"

    # A single shared base image: toolchain only, NO repo checkout. Every PR
    # image inherits it, so the expensive apt layer is built once. The repo is
    # cloned and hardened per-PR in ImageDefault (see clone_and_harden) because
    # hardening pins the repo to one commit and could not be shared.
    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        if isinstance(base_img, Image):
            base_img = base_img.image_full_name()

        packages_str = " \\\n    ".join(DEFAULT_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(apt_command)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

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

""",
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

# Fail LOUDLY on a broken install. `npm install || true` previously swallowed
# real dependency failures (e.g. a missing @colors/colors/safe), which then
# surfaced as a mid-run mocha crash: the test phase recorded almost no tests,
# every test looked NONE->PASS in the fix phase, and the instance auto-resolved
# on evidence that never existed. A broken image is better than a fake pass.
# --no-audit/--no-fund keep npm's banner off stdout (see NPM_QUIET note below).
npm install --no-audit --no-fund

# npm install creates/updates package-lock.json. The fix patch ships its own
# lockfile, so the tree must be left clean or `git apply` dies with
# "error: package-lock.json: already exists in working directory" and the fix
# phase captures zero tests. Restore it when tracked; delete it when npm
# generated it.
git checkout -- package-lock.json 2>/dev/null || rm -f package-lock.json

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{mocha}
""".format(pr=self.pr, mocha=_MOCHA_INVOCATION),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{npm}
{mocha}
""".format(pr=self.pr, npm=_NPM_QUIET_INSTALL, mocha=_MOCHA_INVOCATION),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{npm}
{mocha}
""".format(pr=self.pr, npm=_NPM_QUIET_INSTALL, mocha=_MOCHA_INVOCATION),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Validated before interpolation so an org/repo carrying shell
        # metacharacters cannot inject commands into the generated build.
        repo = _safe_path_component(self.pr.repo)
        org = _safe_path_component(self.pr.org, "org")
        url = f"https://github.com/{org}/{repo}.git"

        sections = [f"FROM {name}:{tag}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(copy_commands.rstrip("\n"))
        sections.append(clone_and_harden(repo, url, self.pr.base.sha))
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN bash /home/prepare.sh")

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("winstonjs", "winston")
class Winston(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        lines = test_log.splitlines()
        current_path = []
        indentation_to_level = {}
        summary_re = re.compile(r"^\s*\d+\s+(passing|failing|pending)")

        for line in lines:
            line = ansi_escape.sub("", line)

            if summary_re.match(line):
                break

            # Match passing (✓/✔), failing (N)), pending (-), or describe headings
            match = re.match(
                r"^(\s*)(?:([✓✔]|-|[0-9]+\))\s+)?(.*?)(?:\s+\([0-9]+ms\))?$", line
            )

            if not match or not match.group(3).strip():
                continue

            spaces, status, name = match.groups()
            name = name.strip()
            indent = len(spaces)

            # Defence in depth against non-test stdout polluting test ids.
            # Mocha's spec reporter indents root suites by 2 and test lines
            # further still; nothing belonging to the suite tree ever sits at
            # column 0. Anything that does is foreign output (npm banners,
            # `> winston@3 test` lifecycle echoes, winston's own JSON log lines
            # emitted by the tests themselves). Before this guard such a line
            # became the tree ROOT and prefixed every id beneath it, so the same
            # test carried different ids in different phases and was scored as a
            # NONE->PASS fix. _NPM_QUIET_INSTALL removes the main source; this
            # ensures any other stray output cannot corrupt ids either.
            if indent < 2:
                continue

            if indent not in indentation_to_level:
                if not indentation_to_level:
                    indentation_to_level[indent] = 0
                else:
                    prev_indents = sorted(
                        [i for i in indentation_to_level.keys() if i < indent]
                    )
                    if prev_indents:
                        closest_indent = prev_indents[-1]
                        indentation_to_level[indent] = (
                            indentation_to_level[closest_indent] + 1
                        )
                    else:
                        indentation_to_level[indent] = 0

            level = indentation_to_level[indent]
            current_path = current_path[:level]
            current_path.append(name)

            if status:
                full_path = ":".join(current_path)
                if status in ("✓", "✔"):
                    passed_tests.add(full_path)
                elif status == "-":
                    skipped_tests.add(full_path)
                elif status.endswith(")"):
                    failed_tests.add(full_path)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# The JSONL and this registry ship together. Instance.create() routes on
# f"{org}/{number_interval}" whenever number_interval is non-empty
# (harness/instance.py), so every dash-joined bundle value delivered in
# winstonjs__winston_lht_final.jsonl MUST be a registered key or the run dies
# with "Instance 'winstonjs/<interval>' is not registered" before any image is
# built.
#
# These are the winston 3.x bundles (mocha + assume). The 2.4 bundle uses vows
# and is registered to Winston2x in winston_1086_to_1086.py -- keep the two
# lists disjoint.
#
# Data-derived from the 16 delivered 3.x bundles; regenerate if that set changes.
_BUNDLE_NIS = [
    "1355-1410-1418-1434-1462-1463-1467-1470-1471-1474-1480-1483-1485-1488-1499-1503-1509-1512-1513-1516-1521-1526-1531-1533-1534-1540-1546-1548-1552-1554-1555-1556-1557-1559-1560-1562-1576",
    "1539-1579-1583-1586",
    "1593-1600-1603-1605-1610-1622-1623-1625-1647-1650-1651-1652-1654-1656-1661-1662-1672-1677-1683-1684-1686-1691-1697-1700-1705-1714-1723-1729-1733-1736-1737-1743-1750-1754-1768-1772-1777-1778-1779-1780-1785-1793",
    "1712-1824-1830-1853-1861-1878-1881-1947-1952-1961-1964-1974-1975-1976-1977-1978-1979-1980-1981-1982-1986-1987-1990-1991-1992-1997-2008-2012",
    "1740-1803-1807-1810",
    "1989-2073-2074-2075-2079-2081-2082-2083-2084-2086-2093-2098-2099-2101",
    "2020-2500-2506-2507-2511-2512-2513",
    "2049-2051-2057-2058-2059-2062-2064-2067-2068-2069-2071",
    "2181-2309-2313-2314-2315-2317-2320-2321",
    "2206-2208-2209-2215-2216-2217-2218-2230-2234-2235-2236-2240-2244-2252-2256-2258-2259-2260-2264-2271-2272-2275-2292-2301",
    "2226-2329-2334-2336-2346-2353-2357",
    "2337-2339-2350-2361-2362-2375-2378-2382-2384-2386-2390-2391-2397-2405-2406-2411-2413-2416-2417-2418-2421-2422",
    "2412-2431-2434",
    "2448-2453-2454-2455-2456-2466-2472-2475-2483-2484",
    "2467-2517-2567-2589-2591-2593-2594",
    "2514-2532-2549-2550",
]
for _ni in _BUNDLE_NIS:
    Instance.register("winstonjs", _ni)(Winston)
