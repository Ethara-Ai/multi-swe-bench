import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:18-bookworm"

_INTERVAL = "babel_15948_to_15037"

_DEFAULT_BRANCH = "main"


_BABEL8_TEST_GATE_RE = re.compile(
    r"^\+.*\b(?:BABEL_8_BREAKING|BABEL_TYPES_8_BREAKING)\b.*\b(?:it|test|describe)\.skip\b"
    r"|^\+.*\b(?:itBabel8|testBabel8|describeBabel8)\b",
    re.MULTILINE,
)


def _is_babel8(pr: PullRequest) -> bool:
    return bool(_BABEL8_TEST_GATE_RE.search(pr.test_patch or ""))


def _lane_env(babel8: bool) -> str:
    lines = [
        "",
        "export BROWSERSLIST_IGNORE_OLD_DATA=1",
    ]
    if babel8:
        lines += [
            "export BABEL_8_BREAKING=true",
            "export BABEL_TYPES_8_BREAKING=true",
        ]
    return "\n".join(lines) + "\n"


_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_LINE = re.compile(r"^(PASS|FAIL)\s+(\S+)")
_TEST_LINE = re.compile(r"^(\s+)([✓✔✕✗×○◌✎])\s+(.*\S)\s*$")
_HEADING_LINE = re.compile(r"^(\s+)(\S.*\S|\S)\s*$")
_TIMING_SUFFIX = re.compile(r"\s+\(\d+(?:\.\d+)?\s*m?s\)$")
_SKIP_PREFIX = re.compile(r"^(?:skipped|todo)\s+")
_NOT_A_HEADING = re.compile(r"^(?:[●>+|-]|at\s|console\.|Warning:|\d+\s*\||expect\()")


def parse_babel_jest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    current_file: str | None = None
    describes: dict[int, str] = {}

    for raw_line in _ANSI.sub("", test_log).split("\n"):
        line = raw_line.rstrip()

        match = _FILE_LINE.match(line)
        if match:
            current_file = match.group(2)
            describes = {}
            if match.group(1) == "FAIL":
                failed_tests.add(current_file)
            else:
                passed_tests.add(current_file)
            continue

        match = _TEST_LINE.match(line)
        if match:
            indent, symbol = len(match.group(1)), match.group(2)
            name = _TIMING_SUFFIX.sub("", match.group(3)).strip()
            if symbol in "○◌✎":
                name = _SKIP_PREFIX.sub("", name).strip()
            path = [describes[key] for key in sorted(describes) if key < indent]
            test_id = " > ".join([current_file or "<unknown>"] + path + [name])
            if symbol in "✓✔":
                passed_tests.add(test_id)
            elif symbol in "○◌✎":
                skipped_tests.add(test_id)
            else:
                failed_tests.add(test_id)
            continue

        match = _HEADING_LINE.match(line)
        if match and current_file:
            text = match.group(2)
            if _NOT_A_HEADING.match(text) or " › " in text:
                continue
            indent = len(match.group(1))
            describes = {k: v for k, v in describes.items() if k < indent}
            describes[indent] = text

    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


_YARN_STATE_PATHS = ".yarn/install-state.gz .yarn/build-state.yml .yarn/unplugged .pnp.cjs .pnp.loader.mjs"

_BUILD_TIME_INSTALL = """
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export COREPACK_ENABLE_STRICT=0
corepack enable

snapshot_deps() {{
  stage="$1"
  shift
  if git apply --whitespace=nowarn "$@" 2>/dev/null; then
    if [ -n "$(git status --porcelain -- yarn.lock '*package.json')" ]; then
      echo "prepare: $stage patches change dependencies; saving /home/deps-$stage.tar.gz"
      YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install
      node -e "require('./package.json'); require.resolve('jest'); console.log('DEPS_OK')"
      find . -path ./.git -prune -o -type d -name node_modules -prune -print > "/tmp/deps-$stage.list"
      for path in {state_paths}; do
        if [ -e "$path" ]; then echo "$path" >> "/tmp/deps-$stage.list"; fi
      done
      tar -czf "/home/deps-$stage.tar.gz" -T "/tmp/deps-$stage.list"
    fi
  else
    echo "prepare: $stage patches do not apply at prepare time; no dependency snapshot"
  fi
  git reset --hard
  git clean -fd
}}

snapshot_deps test /home/test.patch
snapshot_deps fix /home/test.patch /home/fix.patch

YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install

node -e "require('./package.json'); require.resolve('jest'); console.log('DEPS_OK')"
""".format(state_paths=_YARN_STATE_PATHS)

_RESTORE_DEPS = """
if [ -f /home/deps-{stage}.tar.gz ]; then
  find . -path ./.git -prune -o -type d -name node_modules -prune -exec rm -rf {{}} +
  rm -rf {state_paths}
  tar -xzf /home/deps-{stage}.tar.gz
  node -e "require('./package.json'); require.resolve('jest'); console.log('DEPS_OK')"
fi
"""

_BUILD_AND_TEST = """
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
{restore_deps}
make build || true
BABEL_ENV=test yarn jest --verbose --ci || true
"""


def _build_time_install(babel8: bool) -> str:
    return _lane_env(babel8) + _BUILD_TIME_INSTALL


def _build_and_test(babel8: bool, stage: str = "") -> str:
    restore_deps = (
        _RESTORE_DEPS.format(stage=stage, state_paths=_YARN_STATE_PATHS) if stage else ""
    )
    return _lane_env(babel8) + _BUILD_AND_TEST.format(restore_deps=restore_deps)


class Babel15948To15037ImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL.removeprefix('babel_')}-fullhist"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

# History hardening is deferred to the per-PR image, which ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to a single PR's BASE_COMMIT.

{self.clear_env}

CMD ["/bin/bash"]
"""


class Babel15948To15037ImageDefault(Image):
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
        return Babel15948To15037ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha)
                + _build_time_install(_is_babel8(self.pr)),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
""".format(repo=self.pr.repo)
                + _build_and_test(_is_babel8(self.pr)),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
""".format(repo=self.pr.repo)
                + _build_and_test(_is_babel8(self.pr), "test"),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
""".format(repo=self.pr.repo)
                + _build_and_test(_is_babel8(self.pr), "fix"),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        fetch_base = (
            'RUN git fetch --no-tags origin "${BASE_COMMIT}"\n'
            if self.pr.base.ref != _DEFAULT_BRANCH
            else ""
        )

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
{fetch_base}RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


@Instance.register("babel", _INTERVAL)
class BABEL_15948_TO_15037(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Babel15948To15037ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_babel_jest_log(test_log)


babel_15948_to_15037 = BABEL_15948_TO_15037
