import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ERA_RANGE = "1677-to-1120"

_PYTHON_IMAGE = "python:3.11-slim-bookworm"
_RUST_IMAGE = "rust:1.93-slim-bookworm"
_UV_PIN = "0.8.22"
_VENV = "/home/venv"
_TEST_TOOLS = "pytest pytest-asyncio pytest-timeout pydantic 'pip>=25.1'"

_STATUS = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"


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

    def dependency(self) -> str:
        return _PYTHON_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_ERA_RANGE}"

    def workdir(self) -> str:
        return f"base-{_ERA_RANGE}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        from multi_swe_bench.harness.image import DockerfileEnhancer

        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/"
                f"{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        raw = f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential cmake curl pkg-config libssl-dev clang libclang-dev \\
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:$PATH
COPY --from={_RUST_IMAGE} /usr/local/rustup /usr/local/rustup
COPY --from={_RUST_IMAGE} /usr/local/cargo /usr/local/cargo

{code}

{self.clear_env}

"""

        class _Shim:
            pr = self.pr

            @staticmethod
            def dependency():
                return image_name

            @staticmethod
            def dockerfile():
                return raw

        enhanced = DockerfileEnhancer.enhance(_Shim())

        scrub_start = enhanced.find("RUN git reset --hard")
        cmd_start = enhanced.find('CMD ["/bin/bash"]')
        if scrub_start != -1 and cmd_start != -1 and scrub_start < cmd_start:
            enhanced = enhanced[:scrub_start] + enhanced[cmd_start:]
        return enhanced


_STAGE_HEADER = r"""set -eo pipefail
export CI=true
export RUST_BACKTRACE=1
unset COCOINDEX_DATABASE_URL

cd /home/__REPO__
. __VENV__/bin/activate

"""


_APPLY_TEST = r"""git apply --whitespace=nowarn /home/test.patch
"""


_APPLY_FIX = r"""git apply --whitespace=nowarn /home/fix.patch
"""


_RUN_TESTS = r"""TEST_DIR=python/tests
[ -d "$TEST_DIR" ] || TEST_DIR=python/cocoindex/tests

python -m pytest "$TEST_DIR" -v --no-header -rA --tb=short --color=no -p no:cacheprovider --continue-on-collection-errors
"""


_CHECK_GIT_CHANGES = """set -e

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


_ERA_REQS = r"""python - <<'PY'
import tomllib
p = tomllib.load(open('pyproject.toml', 'rb'))
proj = p.get('project', {})
groups = p.get('dependency-groups', {})
def expand(name):
    out = []
    for item in groups.get(name, []):
        if isinstance(item, dict):
            out.extend(expand(item['include-group']))
        else:
            out.append(item)
    return out
reqs = list(p['build-system']['requires'])
reqs += list(proj.get('dependencies', []))
reqs += list(proj.get('optional-dependencies', {}).get('lancedb', []))
reqs += expand('ci')
reqs += expand('dev')
open('/tmp/era-reqs.txt', 'w').write('\n'.join(reqs) + '\n')
PY"""


_GATE = r"""python - <<'PY'
import importlib.metadata as md
from pip._vendor.packaging.requirements import Requirement
import cocoindex, pytest, pytest_asyncio, pytest_timeout, pydantic
missing = []
for line in open('/tmp/era-reqs.txt'):
    line = line.strip()
    if not line:
        continue
    req = Requirement(line)
    if req.marker is not None and not req.marker.evaluate():
        continue
    try:
        md.distribution(req.name)
    except md.PackageNotFoundError:
        missing.append(req.name)
if missing:
    raise SystemExit('missing: ' + ' '.join(missing))
print('gate ok')
PY"""


def _prepare_sh(pr: PullRequest) -> str:
    return "\n".join(
        [
            "set -e",
            "",
            "export CI=true",
            "export RUST_BACKTRACE=1",
            "unset COCOINDEX_DATABASE_URL",
            "",
            f"cd /home/{pr.repo}",
            "git reset --hard",
            "git clean -fdx",
            "bash /home/check_git_changes.sh",
            f"git checkout --detach {pr.base.sha}",
            "bash /home/check_git_changes.sh",
            "",
            "BEFORE=$(TZ=UTC git log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd HEAD)",
            f"python -m venv {_VENV}",
            f". {_VENV}/bin/activate",
            "for attempt in 1 2 3 4 5; do"
            f" pip install --no-cache-dir --retries 10 --timeout 60 \"uv=={_UV_PIN}\" && break;"
            " [ \"$attempt\" = 5 ] && exit 1;"
            " sleep 30;"
            " done",
            _ERA_REQS,
            "for attempt in 1 2 3 4 5; do"
            f" uv pip install --exclude-newer \"$BEFORE\" -r /tmp/era-reqs.txt {_TEST_TOOLS} && break;"
            " [ \"$attempt\" = 5 ] && exit 1;"
            " uv cache clean || true;"
            " sleep 30;"
            " done",
            "",
            "maturin develop --locked",
            "",
            _GATE,
            "python -m pytest --version",
            "",
        ]
    )


_PR_SCRUB = r"""RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""


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

    def dependency(self) -> "ImageBase":
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        stage = _STAGE_HEADER.replace("__REPO__", self.pr.repo).replace("__VENV__", _VENV)

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
                _CHECK_GIT_CHANGES,
            ),
            File(
                ".",
                "prepare.sh",
                _prepare_sh(self.pr),
            ),
            File(
                ".",
                "run.sh",
                stage + _RUN_TESTS,
            ),
            File(
                ".",
                "test-run.sh",
                stage + _APPLY_TEST + _RUN_TESTS,
            ),
            File(
                ".",
                "fix-run.sh",
                stage + _APPLY_TEST + _APPLY_FIX + _RUN_TESTS,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{_PR_SCRUB}
{self.clear_env}

"""


@Instance.register("cocoindex-io", "cocoindex_1677_to_1120")
class COCOINDEX_1677_TO_1120(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", log)

        verbose_re = re.compile(
            rf"^(?P<name>\S+\.py::\S.*?)\s+(?P<status>{_STATUS})"
            r"(?:\s+\(.*?\))?(?:\s+\[\s*\d+%\])?\s*$"
        )
        summary_re = re.compile(
            rf"^(?P<status>{_STATUS})\s+(?P<name>\S+\.py(?:::\S.*?)?)"
            r"(?:\s+-\s.*)?\s*$"
        )

        for raw in clean.splitlines():
            line = raw.strip()
            if not line:
                continue

            match = summary_re.match(line) or verbose_re.match(line)
            if not match:
                continue

            name = match.group("name").strip()
            status = match.group("status")

            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
