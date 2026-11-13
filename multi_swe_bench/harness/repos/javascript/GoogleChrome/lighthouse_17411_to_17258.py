import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "GoogleChrome"
_REPO = "lighthouse"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"lighthouse_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like lighthouse_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:22.22.2-bookworm"

_YARN_VERSION = "1.22.22"

_FIX_DEPS_DIR = "/home/mswe_fix_deps"

_ENV_EXPORTS = (
    "export CI=true\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export PUPPETEER_SKIP_DOWNLOAD=1\n"
)

_TEST_CMD = "bash /home/run_tests.sh\n"

_APPLY_EXCLUDES = "--exclude='yarn.lock' --exclude='package-lock.json'"

_TEST_FILE_RE = re.compile(
    r"^(?:build|clients|cli|core|report|shared|third-party|treemap|viewer)/.+-test\.js$"
)
_SNAPSHOT_RE = re.compile(r"^(.+)/__snapshots__/([^/]+-test\.js)\.snap$")
_CORE_SOURCE_RE = re.compile(r"^core/(?!test/)((?:[^/]+/)*)([^/]+)\.js$")
_DIFF_BLOCK_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_PKG_DEP_RE = re.compile(r'^([+-])\s+"([^"]+)":\s*"([^"]+)",?\s*$')

_CHECK_GIT_CHANGES = """#!/bin/bash
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


def _patch_blocks(patch: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    matches = list(_DIFF_BLOCK_RE.finditer(patch))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(patch)
        blocks[m.group(2)] = patch[m.start():end]
    return blocks


def _test_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.MULTILINE)
    selected: set[str] = set()
    for f in files:
        if _TEST_FILE_RE.match(f):
            selected.add(f)
            continue
        m = _SNAPSHOT_RE.match(f)
        if m and _TEST_FILE_RE.match(f"{m.group(1)}/{m.group(2)}"):
            selected.add(f"{m.group(1)}/{m.group(2)}")
    if selected:
        return sorted(selected)
    fix_files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.fix_patch, re.MULTILINE)
    for f in fix_files:
        m = _CORE_SOURCE_RE.match(f)
        if m:
            selected.add(f"core/test/{m.group(1)}{m.group(2)}-test.js")
    return sorted(selected)


def _fix_dep_bumps(pr: PullRequest) -> list[tuple[str, str, str]]:
    blocks = _patch_blocks(pr.fix_patch)
    pkg = blocks.get("package.json", "")
    lock = blocks.get("yarn.lock", "")
    if not pkg or not lock:
        return []

    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    for line in pkg.split("\n"):
        m = _PKG_DEP_RE.match(line)
        if not m:
            continue
        (removed if m.group(1) == "-" else added)[m.group(2)] = m.group(3)
    bumps = {
        name: spec
        for name, spec in added.items()
        if name in removed and removed[name] != spec
    }
    if not bumps:
        return []

    entries: list[tuple[list[str], str, str]] = []
    header: list[str] = []
    version = ""
    for line in lock.split("\n"):
        if not line or line[0] not in "+ ":
            continue
        if line.startswith("+++"):
            continue
        body = line[1:]
        if body and not body.startswith(" ") and body.rstrip().endswith(":"):
            header = [s.strip().strip('"') for s in body.rstrip()[:-1].split(",")]
            version = ""
        elif body.startswith("  version ") and line[0] == "+":
            version = body.split('"')[1] if '"' in body else body.split()[-1]
        elif body.startswith("  integrity ") and line[0] == "+" and header and version:
            entries.append((header, version, body.split()[1]))

    result: list[tuple[str, str, str]] = []
    for name, spec in sorted(bumps.items()):
        for specs, version, integrity in entries:
            if f"{name}@{spec}" in specs:
                result.append((name, version, integrity))
                break
    return result


class LighthouseImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class LighthouseImageDefault(Image):
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
        return LighthouseImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _fetch_fix_deps_sh(self) -> str:
        bumps = _fix_dep_bumps(self.pr)
        if not bumps:
            return ""
        lines = [f"rm -rf {_FIX_DEPS_DIR}"]
        for i, (name, version, _) in enumerate(bumps):
            lines.append(f"mkdir -p {_FIX_DEPS_DIR}/{i}")
            lines.append(
                f'timeout --kill-after=30 600 npm pack "{name}@{version}" '
                f"--pack-destination {_FIX_DEPS_DIR}/{i} --silent || true"
            )
            lines.append(f"mv {_FIX_DEPS_DIR}/{i}/*.tgz {_FIX_DEPS_DIR}/{i}.tgz || true")
        return "\n".join(lines) + "\n"

    def _fix_deps_gate_sh(self) -> str:
        lines = []
        for i, (_, _, integrity) in enumerate(_fix_dep_bumps(self.pr)):
            lines.append(
                "test \"$(node -e 'process.stdout.write(\"sha512-\" + "
                "require(\"crypto\").createHash(\"sha512\").update("
                "require(\"fs\").readFileSync(process.argv[1])).digest(\"base64\"))' "
                f'{_FIX_DEPS_DIR}/{i}.tgz)" = "{integrity}"'
            )
        return "\n".join(lines) + ("\n" if lines else "")

    def _apply_fix_deps_sh(self) -> str:
        lines = []
        for i, (name, _, _) in enumerate(_fix_dep_bumps(self.pr)):
            target = f"node_modules/{name}"
            lines.append(f"rm -rf {target}")
            lines.append(f"mkdir -p {target}")
            lines.append(f"tar -xzf {_FIX_DEPS_DIR}/{i}.tgz -C {target} --strip-components=1")
        return "\n".join(lines) + ("\n" if lines else "")

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""#!/bin/bash
set -e

{_ENV_EXPORTS}
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

node --version
yarn --version
timeout --kill-after=30 2400 yarn install --frozen-lockfile --non-interactive --network-timeout 1000000 || true
git diff --quiet -- yarn.lock || git checkout -- yarn.lock
timeout --kill-after=30 900 yarn build-report || true
timeout --kill-after=30 300 yarn reset-link || true
{self._fetch_fix_deps_sh()}
cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

test "$(yarn --version)" = "{_YARN_VERSION}"
node --input-type=module -e "await import('mocha'); await import('expect'); await import('jest-snapshot'); await import('@paulirish/trace_engine'); console.log('DEPS_OK')"
node core/test/scripts/run-mocha-tests.js --help >/dev/null
{self._fix_deps_gate_sh()}test -s /home/run_tests.sh
"""

    def _run_tests_sh(self) -> str:
        repo = self.pr.repo
        files = " ".join(_test_files(self.pr))
        return f"""#!/bin/bash
set -uo pipefail

cd /home/{repo}

status=0
for f in {files}; do
  if [ ! -f "$f" ]; then
    echo "━━━ Skipping $f (file not found) ━━━"
    continue
  fi
  echo "━━━ Running $f ━━━"
  timeout --kill-after=30 1800 node core/test/scripts/run-mocha-tests.js --testMatch "$f" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "MSWE_FILE PASS $f"
  else
    echo "MSWE_FILE FAIL $f"
    status=1
  fi
done
exit $status
"""

    def _run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
{_TEST_CMD}"""

    def _test_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply --whitespace=nowarn {_APPLY_EXCLUDES} /home/test.patch
{_TEST_CMD}"""

    def _fix_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply --whitespace=nowarn {_APPLY_EXCLUDES} /home/test.patch /home/fix.patch
{self._apply_fix_deps_sh()}{_TEST_CMD}"""

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_HEADER_RE = re.compile(r"^━━━ Running (\S+) ━━━$")
_SKIP_HEADER_RE = re.compile(r"^━━━ Skipping (\S+) \(file not found\) ━━━$")
_FILE_RESULT_RE = re.compile(r"^MSWE_FILE (PASS|FAIL) (\S+)$")
_ARM_RE = re.compile(r"^running \d+ test files?$")
_EPILOGUE_RE = re.compile(r"^\s*\d+ passing \(\d+(?:\.\d+)?(?:ms|s|m|h|d)\)$")
_PASS_RE = re.compile("^( *)[✔✓√]️? (.+)$")
_FAIL_RE = re.compile("^( *)(?:\\d+\\)|[✖✗×❌]️?) (.+)$")
_PENDING_RE = re.compile(r"^( *)- (.+)$")
_DURATION_RE = re.compile(r"\s+\(\d+(?:\.\d+)?m?s\)$")


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class LIGHTHOUSE_17411_TO_17258(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return LighthouseImageDefault(self.pr, self._config)

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

        clean = _ANSI_RE.sub("", test_log).replace("\r", "")

        current: str | None = None
        armed = False
        epilogue = False
        stack: list[str] = []
        seen: dict[str, int] = {}

        for raw in clean.split("\n"):
            line = raw.rstrip()

            m = _HEADER_RE.match(line)
            if m:
                current = m.group(1)
                armed = False
                epilogue = False
                stack = []
                seen = {}
                continue

            if _SKIP_HEADER_RE.match(line):
                current = None
                continue

            m = _FILE_RESULT_RE.match(line)
            if m:
                name = f"{m.group(2)} > (file exits 0)"
                (passed_tests if m.group(1) == "PASS" else failed_tests).add(name)
                current = None
                continue

            if current is None or epilogue:
                continue

            if not armed:
                if _ARM_RE.match(line.strip()):
                    armed = True
                continue

            if not line.strip():
                continue

            if _EPILOGUE_RE.match(line):
                epilogue = True
                continue

            status = None
            m = _PASS_RE.match(line)
            if m:
                status = "pass"
            else:
                m = _FAIL_RE.match(line)
                if m:
                    status = "fail"
                else:
                    m = _PENDING_RE.match(line)
                    if m:
                        status = "skip"

            if status is None:
                indent = len(line) - len(line.lstrip(" "))
                if indent >= 2 and indent % 2 == 0:
                    level = indent // 2 - 1
                    if level <= len(stack):
                        stack = stack[:level] + [line.strip()]
                continue

            indent = len(m.group(1))
            depth = max((indent - 2) // 2, 0)
            title = m.group(2).strip()
            if status == "pass":
                title = _DURATION_RE.sub("", title).strip()
            base = " > ".join([current] + stack[:depth] + [title])
            count = seen.get(base, 0) + 1
            seen[base] = count
            name = base if count == 1 else f"{base} #{count}"
            if status == "pass":
                passed_tests.add(name)
            elif status == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
