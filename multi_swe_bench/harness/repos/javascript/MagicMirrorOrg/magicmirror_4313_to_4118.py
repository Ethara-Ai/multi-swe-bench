import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "MagicMirrorOrg"
_REPO = "MagicMirror"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"magicmirror_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like magicmirror_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:22.22.2-bookworm"

_ENV_EXPORTS = "export CI=true\nexport NODE_OPTIONS=--max-old-space-size=4096\n"

_TEST_CMD = "bash /home/run_tests.sh\n"

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

_FIX_DEPS_DIR = "/home/mswe_fix_deps"

_DEP_LINE_RE = re.compile(
    r'^\s*"(@?[A-Za-z0-9][\w.-]*(?:/[\w.-]+)?)"\s*:\s*"([\^~><=]*\d[\w.\-+]*)"\s*,?\s*$'
)
_DEP_SECTION_RE = re.compile(
    r'^\s*"(?:dependencies|devDependencies|optionalDependencies)"\s*:\s*\{'
)
_NON_DEP_KEYS = {"version", "node", "npm"}

_COPY_FIX_DEPS = """for p in /home/mswe_fix_deps/node_modules/*/ /home/mswe_fix_deps/node_modules/@*/*/; do
  [ -d "$p" ] || continue
  rel="${p#/home/mswe_fix_deps/node_modules/}"
  rel="${rel%/}"
  case "$rel" in
    @*/*|[!@]*) ;;
    *) continue ;;
  esac
  if [ ! -e "node_modules/$rel" ]; then
    mkdir -p "node_modules/$(dirname "$rel")"
    cp -r "$p" "node_modules/$rel"
  fi
done
"""


_REPLACE_FIX_DEPS = """while read -r n; do
  if [ -n "$n" ] && [ -d "/home/mswe_fix_deps/node_modules/$n" ]; then
    rm -rf "node_modules/$n"
    mkdir -p "node_modules/$(dirname "$n")"
    cp -r "/home/mswe_fix_deps/node_modules/$n" "node_modules/$n"
  fi
done < /home/mswe_fix_deps/replace.txt
"""


def _major(version: str) -> str:
    m = re.search(r"\d+", version)
    return m.group(0) if m else ""


def _major_bumps(patch: str) -> list[str]:
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    in_pkg = False
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            in_pkg = line.endswith(" b/package.json")
            continue
        if not in_pkg or line.startswith(("+++", "---", "@@")):
            continue
        if line[:1] not in ("+", "-"):
            continue
        m = _DEP_LINE_RE.match(line[1:])
        if not m or m.group(1) in _NON_DEP_KEYS:
            continue
        (new if line[0] == "+" else old)[m.group(1)] = m.group(2)
    return [
        f"{n}@{v}"
        for n, v in sorted(new.items())
        if n in old and _major(old[n]) != _major(v)
    ]


def _added_deps(patch: str) -> list[str]:
    added: dict[str, str] = {}
    removed: set[str] = set()
    in_pkg = False
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            in_pkg = line.endswith(" b/package.json")
            continue
        if not in_pkg or line.startswith(("+++", "---", "@@")):
            continue
        if line[:1] not in ("+", "-"):
            continue
        body = line[1:]
        if _DEP_SECTION_RE.match(body):
            continue
        m = _DEP_LINE_RE.match(body)
        if not m or m.group(1) in _NON_DEP_KEYS:
            continue
        if line[0] == "+":
            added[m.group(1)] = m.group(2)
        else:
            removed.add(m.group(1))
    return [f"{n}@{v}" for n, v in sorted(added.items()) if n not in removed]


class MagicMirrorImageBase(Image):
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


class MagicMirrorImageDefault(Image):
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
        return MagicMirrorImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _fix_deps(self) -> list[str]:
        return _added_deps(self.pr.fix_patch)

    def _fix_bumps(self) -> list[str]:
        return _major_bumps(self.pr.fix_patch)

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        deps = self._fix_deps()
        bumps = self._fix_bumps()
        fetch = ""
        gate = ""
        if deps or bumps:
            pick = ""
            args = " ".join(deps)
            if bumps:
                pick = (
                    "BUMPS=$(node -e \"const d=require('./package.json').dependencies||{};"
                    "console.log(process.argv.slice(1).filter(s=>d[s.slice(0,s.lastIndexOf('@'))]).join(' '))\" "
                    f"{' '.join(bumps)})\n"
                    f"printf '%s\\n' $BUMPS | sed 's/@[^@]*$//' | grep -v '^$' > {_FIX_DEPS_DIR}/replace.txt || true\n"
                )
                args = f"{args} $BUMPS".strip()
            fetch = (
                f"rm -rf {_FIX_DEPS_DIR}\n"
                f"mkdir -p {_FIX_DEPS_DIR}\n"
                f"{pick}"
                f"timeout --kill-after=30 900 npm install --prefix {_FIX_DEPS_DIR} "
                f"--no-audit --no-fund --no-package-lock {args} || true\n"
            )
            gate = "".join(
                f"test -d {_FIX_DEPS_DIR}/node_modules/{d.rsplit('@', 1)[0]}\n" for d in deps
            )
            if bumps:
                gate += (
                    f'while read -r n; do test -d "{_FIX_DEPS_DIR}/node_modules/$n"; '
                    f"done < {_FIX_DEPS_DIR}/replace.txt\n"
                )
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
npm --version
timeout --kill-after=30 900 npm ci --no-audit --no-fund || true
timeout --kill-after=30 900 npx --no-install playwright install --with-deps chromium || true
rm -rf /var/lib/apt/lists/*
{fetch}
mkdir -p config
touch config/custom.css

cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

node -e "require('./package.json'); require('vitest/package.json'); require('jsdom'); console.log('DEPS_OK')"
test -x "$(node -e "process.stdout.write(require('playwright').chromium.executablePath())")"
test -s /home/run_tests.sh
{gate}"""

    def _run_tests_sh(self) -> str:
        return f"""#!/bin/bash
set -uo pipefail

cd /home/{self.pr.repo}
mkdir -p config
touch config/custom.css

timeout --kill-after=30 2400 npx --no-install vitest run --project unit --project e2e --bail 0 --reporter verbose 2>&1
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
git apply --whitespace=nowarn /home/test.patch
{_TEST_CMD}"""

    def _fix_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_COPY_FIX_DEPS if (self._fix_deps() or self._fix_bumps()) else ""}{_REPLACE_FIX_DEPS if self._fix_bumps() else ""}{_TEST_CMD}"""

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
_INLINE_RE = re.compile(r"^\s*(✓|×|↓)\s+(unit|e2e|electron)\s+(\S+(?:\s+>\s+.*)?)$")
_SUMMARY_FAIL_RE = re.compile(r"^\s*FAIL\s+(unit|e2e|electron)\s+(\S+(?:\s+>\s+.*)?)$")
_DURATION_RE = re.compile(r"\s+\d+(?:\.\d+)?m?s$")


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class MAGICMIRROR_4313_TO_4118(Instance):
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
        return MagicMirrorImageDefault(self.pr, self._config)

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

        seen: dict[str, int] = {}
        summary_fails: set[str] = set()
        for raw in clean.splitlines():
            line = raw.rstrip()

            m = _INLINE_RE.match(line)
            if m:
                base = _DURATION_RE.sub("", m.group(3)).strip()
                count = seen.get(base, 0) + 1
                seen[base] = count
                name = base if count == 1 else f"{base} #{count}"
                if m.group(1) == "✓":
                    passed_tests.add(name)
                elif m.group(1) == "×":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            m = _SUMMARY_FAIL_RE.match(line)
            if m:
                summary_fails.add(_DURATION_RE.sub("", m.group(2)).strip())

        for name in summary_fails:
            if name not in seen:
                failed_tests.add(name)

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
