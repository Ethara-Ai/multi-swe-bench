import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "whyour"
_REPO = "qinglong"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"qinglong_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like qinglong_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:22.23.2-bookworm"
PNPM_VERSION = "8.3.1"

_ENV_EXPORTS = "export CI=true\nexport NODE_OPTIONS=--max-old-space-size=4096\n"

_TEST_CMD = "bash /home/run_tests.sh\n"

_TEST_FILE_RE = re.compile(r"^(?:test|cli/test)/.+\.test\.cjs$")

_APPLY = "git apply --whitespace=nowarn --exclude='*pnpm-lock.yaml' --exclude='*package-lock.json'"

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


def _test_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/(\S+) b/", pr.test_patch, re.MULTILINE)
    return sorted({f for f in files if _TEST_FILE_RE.match(f)})


class QinglongImageBase(Image):
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
    git ca-certificates curl jq perl procps unzip zip \\
    build-essential python3 python3-venv \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-audit --no-fund pnpm@{PNPM_VERSION} && \\
    test "$(pnpm --version)" = "{PNPM_VERSION}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class QinglongImageDefault(Image):
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
        return QinglongImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

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
pnpm --version
timeout --kill-after=30 1800 pnpm install --frozen-lockfile || true
timeout --kill-after=30 900 npm ci --prefix cli --no-audit --no-fund || true
git diff --name-only -- 'pnpm-lock.yaml' 'cli/package-lock.json' | xargs -r git checkout --
cp .env.example .env

cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

test -s /home/run_tests.sh
python3 -c "import ensurepip, venv"
node -e "require('./package.json'); require('typescript'); require.resolve('ts-node/register/transpile-only'); require('reflect-metadata'); require('express'); require('celebrate'); require('typedi'); require('./cli/node_modules/typescript/package.json'); require('./cli/node_modules/commander/package.json'); require('./cli/node_modules/esbuild').transformSync('let a = 1'); console.log('DEPS_OK')"
"""

    def _run_tests_sh(self) -> str:
        repo = self.pr.repo
        files = _test_files(self.pr)
        build = ""
        if any(f.startswith("cli/") for f in files):
            build = """echo "━━━ Building cli ━━━"
if ! timeout --kill-after=30 600 node cli/scripts/build.cjs 2>&1; then
  echo "MSWE_CLI_BUILD FAIL"
  status=1
fi
"""
        return f"""#!/bin/bash
set -uo pipefail

cd /home/{repo}
export CI=true
export TS_NODE_PROJECT=back/tsconfig.json

status=0
{build}
for f in {" ".join(files)}; do
  if [ ! -f "$f" ]; then
    echo "━━━ Skipping $f (file not found) ━━━"
    continue
  fi
  echo "━━━ Running $f ━━━"
  case "$f" in
    cli/*)
      timeout --kill-after=30 900 node --test --test-reporter=tap "$f" 2>&1
      rc=$?
      ;;
    *)
      timeout --kill-after=30 900 node -r ts-node/register/transpile-only --test --test-reporter=tap "$f" 2>&1
      rc=$?
      ;;
  esac
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
{_APPLY} /home/test.patch
{_TEST_CMD}"""

    def _fix_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
{_APPLY} /home/test.patch /home/fix.patch
{_TEST_CMD}"""

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
_SKIP_HEADER_RE = re.compile(r"^━━━ (?:Skipping|Building) ")
_FILE_RESULT_RE = re.compile(r"^MSWE_FILE (PASS|FAIL) (\S+)$")
_TAP_RE = re.compile(r"^( *)(not ok|ok) \d+(?: - (.*))?$")
_DIRECTIVE_RE = re.compile(r"(?<!\\)\s#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class QINGLONG_3098_TO_3083(Instance):
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
        return QinglongImageDefault(self.pr, self._config)

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

        clean = _ANSI_RE.sub("", test_log).replace("\r", "").replace("️", "")

        buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}
        current: str | None = None
        pending: dict[int, list[tuple[list[str], str]]] = {}
        for raw in clean.split("\n"):
            line = raw.rstrip()

            m = _HEADER_RE.match(line)
            if m:
                current = m.group(1)
                pending = {}
                continue

            if _SKIP_HEADER_RE.match(line):
                current = None
                pending = {}
                continue

            m = _FILE_RESULT_RE.match(line)
            if m:
                name = f"{m.group(2)} > (file exits 0)"
                buckets["pass" if m.group(1) == "PASS" else "fail"].add(name)
                current = None
                pending = {}
                continue

            if current is None:
                continue

            m = _TAP_RE.match(line)
            if not m:
                continue

            level = len(m.group(1)) // 4
            status = "fail" if m.group(2) == "not ok" else "pass"
            title = m.group(3) or ""
            directive = _DIRECTIVE_RE.search(title)
            if directive:
                title = title[: directive.start()]
                status = "skip"
            title = title.strip()
            if not title:
                continue

            kids = pending.pop(level + 1, [])
            for deeper in [k for k in pending if k > level]:
                pending.pop(deeper)
            entries = [([title] + parts, st) for parts, st in kids]
            entries.append(([title], status))
            if level == 0:
                for parts, st in entries:
                    buckets[st].add(f"{current} > " + " > ".join(parts))
            else:
                pending.setdefault(level, []).extend(entries)

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
