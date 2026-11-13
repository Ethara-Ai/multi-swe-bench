import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "zotero"
_REPO = "zotero"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"zotero_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like zotero_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:18.20.8-bookworm"

_ENV_EXPORTS = (
    "export CI=true\n"
    "export NODE_OPTIONS=--openssl-legacy-provider\n"
)

_TEST_CMD = "bash /home/run_tests.sh\n"

_TEST_FILE_RE = re.compile(r"^test/tests/([A-Za-z0-9_]+)Test\.js$")

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


class ZoteroImageBase(Image):
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
    git git-lfs ca-certificates xvfb xauth dbus-x11 zstd xxd zip unzip rsync python3 \\
    libgtk-3-0 libx11-xcb1 libdbus-glib-1-2 libxt6 libasound2 \\
    && rm -rf /var/lib/apt/lists/*

RUN git lfs install --system --skip-repo

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class ZoteroImageDefault(Image):
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
        return ZoteroImageBase(self.pr, self._config)

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
git submodule sync --recursive
git submodule update --init --recursive --force
git lfs pull --include="app/linux/updater.tar.xz"
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

ARCH=$(uname -m)
[ "$ARCH" = "aarch64" ] && ARCH=arm64 || ARCH=x86_64

node --version
npm --version
timeout --kill-after=30 1200 app/scripts/fetch_xulrunner -p l -a "$ARCH" || true
timeout --kill-after=30 900 npm ci --no-audit --no-fund || true
timeout --kill-after=30 1200 node js-build/build.js || true

cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

node -e "require('./package.json'); console.log('DEPS_OK')"
test -x "app/xulrunner/firefox-linux-$ARCH/firefox"
! head -c 200 app/linux/updater.tar.xz | grep -q "git-lfs.github.com/spec"
test -d build
test -s /home/run_tests.sh
"""

    def _run_tests_sh(self) -> str:
        repo = self.pr.repo
        files = " ".join(_test_files(self.pr))
        return f"""#!/bin/bash
set -uo pipefail

cd /home/{repo}

timeout --kill-after=30 1200 node js-build/build.js > /tmp/zotero-js-build.log 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  tail -50 /tmp/zotero-js-build.log
  echo "MSWE_BUILD FAIL"
  exit 1
fi

status=0
for f in {files}; do
  if [ ! -f "$f" ]; then
    echo "━━━ Skipping $f (file not found) ━━━"
    continue
  fi
  name=$(basename "$f" Test.js)
  echo "━━━ Running $f ━━━"
  timeout --kill-after=30 1800 xvfb-run -a test/runtests.sh -r 2 "$name" 2>&1
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
git apply --whitespace=nowarn /home/test.patch
{_TEST_CMD}"""

    def _fix_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
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
_FILE_RESULT_RE = re.compile(r"^MSWE_FILE (PASS|FAIL) (\S+)$")
_PASS_RE = re.compile(r"^(\s*)(?:✅|✓|✔)️?\s+(.+?)\s*$")
_FAIL_RE = re.compile(r"^(\s*)(?:❌|✖|×|✗)️?\s+\[FAIL\]\s+(.+?)\s*$")
_RETRY_RE = re.compile(r"^(\s*)(?:❌|✖|×|✗)️?\s+\[FAIL -- retrying\]\s+")
_PENDING_RE = re.compile(r"^(\s*)pending  -(.+?)\s*$")
_SUITE_RE = re.compile(r"^((?:  )+)(\S.*?)\s*$")
_SUFFIX_RE = re.compile(r"(?:\s+\(\d+ ms\))?(?:\s+\(passed on retry \d+\))?$")


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class ZOTERO_5983_TO_5823(Instance):
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
        return ZoteroImageDefault(self.pr, self._config)

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

        clean = _ANSI_RE.sub("", test_log).replace("\r", "").replace("\x07", "")

        current: str | None = None
        stack: list[str] = []
        in_error = False

        def full(indent: str, title: str) -> str:
            depth = len(indent) // 2
            parents = stack[: max(depth, 0)]
            return " > ".join([current, *parents, _SUFFIX_RE.sub("", title).strip()])

        for raw in clean.splitlines():
            line = raw.rstrip()

            m = _HEADER_RE.match(line)
            if m:
                current, stack, in_error = m.group(1), [], False
                continue

            m = _FILE_RESULT_RE.match(line)
            if m:
                name = f"{m.group(2)} > (file exits 0)"
                (passed_tests if m.group(1) == "PASS" else failed_tests).add(name)
                current, stack, in_error = None, [], False
                continue

            if current is None:
                continue

            if in_error:
                if not line.strip():
                    in_error = False
                continue

            m = _FAIL_RE.match(line)
            if m:
                failed_tests.add(full(m.group(1), m.group(2)))
                in_error = True
                continue

            if _RETRY_RE.match(line):
                in_error = True
                continue

            m = _PASS_RE.match(line)
            if m:
                passed_tests.add(full(m.group(1), m.group(2)))
                continue

            m = _PENDING_RE.match(line)
            if m:
                skipped_tests.add(full(m.group(1), m.group(2)))
                continue

            m = _SUITE_RE.match(line)
            if m:
                depth = len(m.group(1)) // 2
                if 1 <= depth <= len(stack) + 1:
                    stack = stack[: depth - 1] + [m.group(2)]

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
