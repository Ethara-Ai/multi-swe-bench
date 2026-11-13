import re
import shlex
from collections import OrderedDict
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# GitHub now classifies prime-agent as Rust (the repo was later rewritten in
# Rust), but at every base sha in this dataset (PRs #2130-#2299, Sept 2026) it
# is an npm-workspaces TypeScript monorepo: package-lock.json, root
# engines.node ">=22.8.0", CI on Node 22 (actions/setup-node node-version: 22),
# vitest 4.1.10 per package. Node 22.23.4 is the current 22.x LTS image.
_NODE_IMAGE = "node:22.23.4-bookworm"

_ENV = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export DO_NOT_TRACK=1
export PI_NO_LOCAL_LLM=1
export NODE_OPTIONS=--max-old-space-size=4096
unset RLM_DEPTH"""

_SUITE_MARK = "prime-agent-suite"

# A test file under packages/<pkg>/ that vitest picks up.
_TEST_FILE_RE = re.compile(r"^packages/([^/]+)/(.+\.test\.[cm]?[jt]sx?)$")


def _patched_tests(pr: PullRequest) -> "OrderedDict[str, list[str]]":
    """Group the test files that test.patch adds or modifies by workspace package.

    Only these files are run: the full coding-agent suite (400+ files) boots
    Python kernels, real subprocess daemons and wall-clock stress tests that are
    not reproducible in a sandbox, and the repo's own CI shards it. Deleted
    files are skipped. Paths absent at a stage (new files before test.patch)
    simply match nothing (`--passWithNoTests`).
    """
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for path in re.findall(r"^\+\+\+ b/(\S+)", pr.test_patch, flags=re.M):
        found = _TEST_FILE_RE.match(path)
        if not found:
            continue
        pkg, rel = found.groups()
        files = groups.setdefault(pkg, [])
        if rel not in files:
            files.append(rel)
    return groups


def _test_commands(pr: PullRequest) -> str:
    lines = ["rc=0"]
    for pkg, files in _patched_tests(pr).items():
        args = " ".join(shlex.quote(f) for f in files)
        lines.append(f'echo "{_SUITE_MARK} {pkg}"')
        lines.append(
            f"(cd packages/{pkg} && npx --no-install vitest --run "
            f"--reporter=verbose --passWithNoTests {args}) || rc=$?"
        )
    lines.append('exit "$rc"')
    return "\n".join(lines)


def _apply_flags(pr: PullRequest) -> str:
    skipped: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
            head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
            if (
                head
                and re.search(r"^Binary files .* differ$", block, re.M)
                and "GIT binary patch" not in block
            ):
                skipped.update(head.groups())
    flags = ["--whitespace=nowarn"] + [
        f"--exclude={shlex.quote(path)}" for path in sorted(skipped)
    ]
    return " ".join(flags)


class PrimeAgentImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # Same system packages as the repo's CI: cairo/pango/jpeg/gif/rsvg for
        # the `canvas` devDependency (source build fallback), fd + ripgrep for
        # the coding-agent tools.
        return [
            "pkg-config",
            "libcairo2-dev",
            "libpango1.0-dev",
            "libjpeg-dev",
            "libgif-dev",
            "librsvg2-dev",
            "fd-find",
            "ripgrep",
        ]

    def extra_setup(self) -> str:
        return (
            "ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \\\n"
            "    CI=true \\\n"
            "    NO_COLOR=1 \\\n"
            "    DO_NOT_TRACK=1\n"
            "RUN ln -sf \"$(command -v fdfind)\" /usr/local/bin/fd \\\n"
            "    && git config --global --add safe.directory '*' \\\n"
            "    && node --version && npm --version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. This is a per-PR base, so the
    # pin and the history scrub belong here.


class PrimeAgentImageDefault(Image):
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
        return PrimeAgentImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        tests = _test_commands(self.pr)
        apply = _apply_flags(self.pr)
        gate_pkgs = " ".join(sorted(_patched_tests(self.pr)))
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
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

# Section 1: pin
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2: provision
{_ENV}
export HUSKY=0

# Locked install from package-lock.json (as CI: npm ci). node_modules/ and
# dist/ are git-ignored, so a verifier's `git reset --hard && git clean -fd`
# keeps them.
installed=0
for attempt in 1 2 3; do
  if npm ci --no-audit --no-fund; then
    installed=1
    break
  fi
  sleep 20
done
test "$installed" = 1

# Build every workspace package, as CI does before testing.
npm run build

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
for pkg in {gate_pkgs}; do
  (cd "packages/$pkg" && npx --no-install vitest --version)
done
node -e "require.resolve('vitest/package.json')"
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{tests}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply {apply} /home/test.patch
{tests}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply {apply} /home/test.patch /home/fix.patch
{tests}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("PrimeIntellect-ai", "prime-agent")
class PRIME_AGENT(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PrimeAgentImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        suite_re = re.compile(rf"^{re.escape(_SUITE_MARK)} (\S+)\s*$")
        # vitest verbose reporter:
        #   "✓ test/foo.test.ts > suite > case 12ms"
        #   "× test/foo.test.ts > suite > case 30012ms"
        #   "↓ test/foo.test.ts > suite > case"   (skipped / todo)
        result_re = re.compile(
            r"^([✓✔×✗✕↓])\s+(\S+\.test\.[cm]?[jt]sx?\s+>\s+.+?)"
            r"(?:\s+\[[^\]]*\])?(?:\s+\d+(?:\.\d+)?\s*m?s)?(?:\s+\(retry x\d+\))?\s*$"
        )

        pkg = ""
        # vitest can print the same title twice (e.g. an uninterpolated
        # `$var` in it.each). Tests in one file run in declaration order, so
        # the Nth repeat of a name gets a stable " [#N]" suffix in every stage.
        seen: dict[str, int] = {}
        for raw in log.splitlines():
            line = raw.rstrip("\r").strip()
            found = suite_re.match(line)
            if found:
                pkg = found.group(1)
                continue
            found = result_re.match(line)
            if not found:
                continue
            mark, name = found.groups()
            name = re.sub(r"\s+", " ", name).strip()
            test = f"{pkg}:{name}" if pkg else name
            seen[test] = seen.get(test, 0) + 1
            if seen[test] > 1:
                test = f"{test} [#{seen[test]}]"
            if mark in "✓✔":
                passed_tests.add(test)
            elif mark in "×✗✕":
                failed_tests.add(test)
            else:
                skipped_tests.add(test)

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
