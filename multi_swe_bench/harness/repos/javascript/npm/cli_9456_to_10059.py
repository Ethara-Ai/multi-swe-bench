import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "npm"
REPO = "cli"

_ERA = "cli_9456_to_10059"

_ROOT = f"/home/{REPO}"

_NODE_TAG = "node:24-bookworm"
_BASE_TAG = f"base-{_ERA}"

_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/\S+$", re.M)

_SNAPSHOT = re.compile(
    r"^(?P<prefix>(?:\S+/)??)tap-snapshots/(?P<rel>test/.+)\.test\.cjs$"
)

_TEST_FILE_SUFFIX = (".js", ".cjs", ".mjs")

_WORKSPACE_PREFIXES = (
    "workspaces/arborist",
    "workspaces/config",
    "workspaces/libnpmaccess",
    "workspaces/libnpmdiff",
    "workspaces/libnpmexec",
    "workspaces/libnpmfund",
    "workspaces/libnpmorg",
    "workspaces/libnpmpack",
    "workspaces/libnpmpublish",
    "workspaces/libnpmsearch",
    "workspaces/libnpmteam",
    "workspaces/libnpmversion",
    "docs",
    "smoke-tests",
    "mock-globals",
    "mock-registry",
)

_FIXTURE_SEGMENTS = ("fixtures", "fixture")


def _normalise(path: str) -> Optional[str]:
    snapshot = _SNAPSHOT.match(path)
    if snapshot:
        return snapshot.group("prefix") + snapshot.group("rel")
    if not path.endswith(_TEST_FILE_SUFFIX):
        return None
    segments = path.split("/")
    if "test" not in segments:
        return None
    if any(segment in _FIXTURE_SEGMENTS for segment in segments):
        return None
    return path


def _workspace_of(path: str) -> str:
    for prefix in _WORKSPACE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return prefix
    return ""


def _targets(pr: PullRequest) -> dict[str, list[str]]:
    paths = {
        normalised
        for normalised in (
            _normalise(path) for path in _DIFF_FILE.findall(f"{pr.test_patch}")
        )
        if normalised
    }
    grouped: dict[str, list[str]] = {}
    for path in sorted(paths):
        grouped.setdefault(_workspace_of(path), []).append(path)
    return grouped


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

_STREAM_MARKER = re.compile(r"^##STREAM##\s+(\S+)\s*$")

_SUBTEST = re.compile(r"^\s*# Subtest:\s*(?P<name>.*?)\s*$")

_POINT = re.compile(
    r"^\s*(?P<status>ok|not ok)\s+\d+\s*(?:-\s*(?P<name>.*?))?\s*$"
)

_PLAN = re.compile(r"^\s*\d+\.\.\d+")

_YAML_OPEN = re.compile(r"^\s*---\s*$")
_YAML_CLOSE = re.compile(r"^\s*\.\.\.\s*$")

_DIRECTIVE = re.compile(r"\s+#\s*(?P<directive>[A-Za-z]+)\b.*$")

_TIME_SUFFIX = re.compile(r"\s+#\s*time=[\d.]+m?s\s*$")

_SKIP_DIRECTIVES = frozenset({"skip", "todo"})

_SEVERITY = {"pass": 0, "skip": 1, "fail": 2}

_HARNESS_ERROR = re.compile(r"^(?:[A-Z]+:\s|no tests found\b)")


def _unescape(name: str) -> str:
    return name.replace("\\#", "#").replace("\\\\", "\\")


def _split_directive(name: str) -> tuple[str, str]:
    stripped = _TIME_SUFFIX.sub("", name)
    match = _DIRECTIVE.search(stripped)
    if not match:
        return stripped.strip(), ""
    directive = match.group("directive").lower()
    if directive not in _SKIP_DIRECTIVES:
        return stripped.strip(), ""
    return stripped[: match.start()].strip(), directive


def parse_npm_cli_log(log: str) -> TestResult:
    status: dict[str, str] = {}
    prefix = ""
    stack: list[list] = []
    in_yaml = False
    yaml_indent = 0

    def record(chain: list[str], name: str, state: str) -> None:
        parts = [part for part in chain if part]
        if not parts or not name or _HARNESS_ERROR.match(name):
            return
        if prefix:
            parts[0] = f"{prefix}/{parts[0]}"
        test_id = " > ".join(parts + [name])
        current = status.get(test_id)
        if current is None or _SEVERITY[state] > _SEVERITY[current]:
            status[test_id] = state

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw.rstrip())
        if not line.strip():
            continue

        marker = _STREAM_MARKER.match(line.strip())
        if marker:
            prefix = marker.group(1).strip("/")
            if prefix == ".":
                prefix = ""
            stack = []
            in_yaml = False
            continue

        indent = len(line) - len(line.lstrip())

        if in_yaml:
            if _YAML_CLOSE.match(line) and indent <= yaml_indent:
                in_yaml = False
            continue

        if _YAML_OPEN.match(line):
            in_yaml = True
            yaml_indent = indent
            continue

        if _PLAN.match(line):
            continue

        subtest = _SUBTEST.match(line)
        if subtest:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append([indent, _unescape(subtest.group("name"))])
            continue

        point = _POINT.match(line)
        if not point:
            continue

        name, directive = _split_directive(point.group("name") or "")
        name = _unescape(name)

        if directive:
            state = "skip"
        elif point.group("status") == "ok":
            state = "pass"
        else:
            state = "fail"

        while stack and stack[-1][0] > indent:
            stack.pop()

        if stack and stack[-1][0] == indent and stack[-1][1] == name:
            stack.pop()
            record([entry[1] for entry in stack], name, state)
        elif directive:
            record([entry[1] for entry in stack], name, state)

    passed_tests = {name for name, state in status.items() if state == "pass"}
    failed_tests = {name for name, state in status.items() if state == "fail"}
    skipped_tests = {name for name, state in status.items() if state == "skip"}

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


_SYNTAX = DockerfileEnhancer.SYNTAX_DIRECTIVE

_LABELS = (
    'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
    '      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
    '      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
    '      org.opencontainers.image.authors="https://www.ethara.ai/"'
)

_BASE_PACKAGES = [
    "git",
    "ca-certificates",
    "build-essential",
    "python3",
]

_BASE_ENV = """ENV CI=true
ENV NODE_ENV=test
ENV NODE_OPTIONS=--max-old-space-size=4096"""


def base_dockerfile(image: Image) -> str:
    base_img = image.dependency()
    packages_str = " \\\n    ".join(_BASE_PACKAGES)

    sections = [
        _SYNTAX,
        f"FROM {base_img}",
        DockerfileEnhancer._TARGETARCH_ARG
        + "\n"
        + f'ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"\n'
        + "ARG BASE_COMMIT\n"
        + "\n"
        + DockerfileEnhancer._PROXY_ARGS,
        DockerfileEnhancer._ENV_BLOCK,
        _LABELS.format(org=ORG, repo=REPO),
        DockerfileEnhancer._CERT_SYMLINKS,
        image._get_apt_update_command(packages_str, base_img),
        _BASE_ENV,
        "RUN git config --global --add safe.directory '*'",
        "WORKDIR /home/",
        f'RUN git clone "${{REPO_URL}}" {_ROOT} && \\\n'
        f"    cd {_ROOT} && git rev-parse HEAD >/dev/null",
        'CMD ["/bin/bash"]',
    ]

    return "\n\n".join(sections) + "\n"


_HARDENING = f"""RUN set -eux; \\
    cd {_ROOT}; \\
    test "$(git rev-parse HEAD)" = "{{sha}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f {_ROOT}/.gitmodules ]; then \\
        cd {_ROOT} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""


def pr_dockerfile(image: Image) -> str:
    base = image.dependency()

    sections = [f"FROM {base.image_full_name()}"]

    if image.global_env:
        sections.append(image.global_env)

    sections.append(
        "".join(f"COPY {file.name} /home/\n" for file in image.files()).rstrip("\n")
    )
    sections.append("RUN bash /home/prepare.sh")
    sections.append(_HARDENING.format(sha=image.pr.base.sha))

    if image.clear_env:
        sections.append(image.clear_env)

    return "\n\n".join(sections) + "\n"


CHECK_GIT_CHANGES = """#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

SHEBANG = "#!/bin/bash\nset -eo pipefail"

APPLY_TEST = """if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi"""

APPLY_FIX = """if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch and fix.patch failed" >&2
    exit 1
fi"""

_RUN_STREAM = f"""TAP_BIN="{_ROOT}/node_modules/.bin/tap"

run_stream() {{
    local tag="$1"; shift
    local log
    log="$(mktemp)"
    echo "##STREAM## ${{tag}}"
    set +e
    "$TAP_BIN" --no-coverage --reporter=tap --jobs=1 --timeout=1800 "$@" >"$log" 2>&1
    set -e
    cat "$log"
    if ! grep -qE '^[[:space:]]*(ok|not ok) [0-9]+' "$log"; then
        echo "Error: stream ${{tag}} produced no TAP results" >&2
        rm -f "$log"
        exit 1
    fi
    rm -f "$log"
}}"""


def _test_commands(pr: PullRequest) -> str:
    parts = [_RUN_STREAM, ""]
    for workspace, paths in sorted(_targets(pr).items()):
        if workspace:
            relative = [path[len(workspace) + 1:] for path in paths]
            parts.append(f"cd {_ROOT}/{workspace}")
            parts.append(
                f"run_stream {workspace} " + " ".join(relative)
            )
        else:
            parts.append(f"cd {_ROOT}")
            parts.append("run_stream . " + " ".join(paths))
    return "\n\n".join(parts)


_DEPS_GATE = f"""cd {_ROOT}
test -x node_modules/.bin/tap
node -e "require('./package.json'); require('tap'); require('nock'); console.log('DEPS_OK')"
node . --version"""


class ImageBase9456To10059(Image):
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
        return _NODE_TAG

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return base_dockerfile(self)


class ImageDefault9456To10059(Image):
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
        return ImageBase9456To10059(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
cd {_ROOT}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {self.pr.base.sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags origin {self.pr.base.sha}
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

node scripts/resetdeps.js

{_DEPS_GATE}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
export CI=true
cd {_ROOT}

{_test_commands(self.pr)}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
export CI=true
cd {_ROOT}
{APPLY_TEST}

{_test_commands(self.pr)}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
export CI=true
cd {_ROOT}
{APPLY_FIX}

{_test_commands(self.pr)}
""",
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self)


@Instance.register(ORG, _ERA)
class CLI_9456_TO_10059(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault9456To10059(self.pr, self._config)

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
        return parse_npm_cli_log(log)


Instance.register(ORG, REPO)(CLI_9456_TO_10059)
