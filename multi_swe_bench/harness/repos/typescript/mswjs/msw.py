import re

from multi_swe_bench.harness.image import DockerfileEnhancer, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "mswjs"
REPO = "msw"

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

_FILE = r"\S+\.[cm]?[jt]sx?"
_DUR = r"(?:\s*\(?\s*\d+(?:\.\d+)?\s*(?:ms|s)\s*\)?)?"
_RETRY = re.compile(r"\s*\(retry\s*#\d+\)\s*")

_PASS_MARKS = "✓✔"
_FAIL_MARKS = "✕✗×✘"

_JEST_HEADER = re.compile(r"^(?:PASS|FAIL)\s+(" + _FILE + r")")
_TAP = re.compile(r"^(ok|not ok)\s+\d+\s*-\s*(.+?)(?:\s+#\s*(.*))?$")
_PLAYWRIGHT = re.compile(
    r"^\s*(?P<mark>[-✓✔✕✗×✘])\s+(?:\d+\s+)?"
    r"(?:\[(?P<project>[^\]]+)\]\s*›\s*)?"
    r"(?P<file>" + _FILE + r"):\d+:\d+\s+›\s+(?P<name>.+?)" + _DUR + r"$"
)
_VITEST = re.compile(
    r"^\s*(?P<mark>[✓✔✕✗×✘↓])\s+"
    r"(?P<file>" + _FILE + r")\s+>\s+(?P<name>.+?)" + _DUR + r"$"
)
_MARK_LINE = re.compile(
    r"^(?P<indent>\s+)(?P<mark>[✓✔✕✗×✘○✎↓])\s+(?P<name>.+?)" + _DUR + r"$"
)
_DESCRIBE_LINE = re.compile(r"^(?P<indent>\s+)(?P<name>[^\s●✓✔✕✗×✘○✎↓].*?)\s*$")
_SUMMARY = re.compile(r"^(?:Test Suites:|Tests:|Snapshots:|Time:|Ran all test suites)")
_NOT_DESCRIBE = re.compile(
    r"^(?:at\s|console\.|expect\(|Expected|Received|Difference|\d+\s*\||\^)"
    r"|\([^()]*:\d+:\d+\)$"
)


def _status_for(mark: str) -> str:
    if mark in _PASS_MARKS:
        return "pass"
    if mark in _FAIL_MARKS:
        return "fail"
    return "skip"


def parse_msw_log(log: str) -> TestResult:
    status: dict[str, str] = {}
    describes: list[tuple[int, str]] = []
    current_file = ""
    in_tree = False
    skip_indent = None

    def push(test_id: str, state: str) -> None:
        status[test_id] = state

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw.rstrip())
        stripped = line.strip()

        tap = _TAP.match(stripped)
        if tap:
            state, name, directive = tap.group(1), tap.group(2).strip(), tap.group(3) or ""
            parts = name.split(" > ", 1)
            test_id = parts[0] + "::" + parts[1] if len(parts) == 2 else name
            skipped = directive.upper().startswith(("SKIP", "TODO"))
            push(test_id, "skip" if skipped else ("pass" if state == "ok" else "fail"))
            in_tree = False
            continue

        header = _JEST_HEADER.match(stripped)
        if header:
            current_file = header.group(1)
            describes = []
            in_tree = True
            skip_indent = None
            continue

        match = _PLAYWRIGHT.match(line)
        if match:
            name = _RETRY.sub(" ", match.group("name")).strip().replace("›", ">")
            name = re.sub(r"\s{2,}", " ", name)
            project = match.group("project")
            if project:
                name = project + " > " + name
            push(match.group("file") + "::" + name, _status_for(match.group("mark")))
            in_tree = False
            continue

        match = _VITEST.match(line)
        if match:
            push(
                match.group("file") + "::" + match.group("name").strip(),
                _status_for(match.group("mark")),
            )
            in_tree = False
            continue

        if not in_tree or not stripped:
            continue

        indent = len(line) - len(line.lstrip())

        if skip_indent is not None:
            if indent > skip_indent:
                continue
            skip_indent = None

        if _SUMMARY.match(stripped):
            in_tree = False
            describes = []
            continue

        if stripped.startswith("●"):
            skip_indent = indent
            continue

        match = _MARK_LINE.match(line)
        if match:
            indent = len(match.group("indent"))
            while describes and describes[-1][0] >= indent:
                describes.pop()
            chain = [name for _, name in describes] + [match.group("name").strip()]
            push(current_file + "::" + " > ".join(chain), _status_for(match.group("mark")))
            continue

        match = _DESCRIBE_LINE.match(line)
        if match:
            name = match.group("name").strip()
            if _NOT_DESCRIBE.search(name):
                continue
            while describes and describes[-1][0] >= indent:
                describes.pop()
            describes.append((indent, name))

    passed_tests = {k for k, v in status.items() if v == "pass"}
    failed_tests = {k for k, v in status.items() if v == "fail"}
    skipped_tests = {k for k, v in status.items() if v == "skip"}

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


_DEP_LINE = re.compile(r'^\+\s*"(@?[\w.\-]+(?:/[\w.\-]+)?)"\s*:\s*"([\^~>=<]?\d[^"]*)"')


def extra_dep_specs(pr: PullRequest) -> list[str]:
    patch = pr.fix_patch or ""
    specs = []
    inside = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            inside = line.rstrip().endswith("/package.json")
            continue
        if not inside:
            continue
        match = _DEP_LINE.match(line)
        if match:
            specs.append(match.group(1) + "@" + match.group(2))
    return specs


_SYNTAX = DockerfileEnhancer.SYNTAX_DIRECTIVE

_LABELS = (
    'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
    '      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
    '      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
    '      org.opencontainers.image.authors="https://www.ethara.ai/"'
)

_DEFAULT_PACKAGES = [
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


def base_dockerfile(image: Image, extra_packages: list[str], extra_env: str = "") -> str:
    base_img = image.dependency()
    packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + extra_packages)
    apt_command = image._get_apt_update_command(packages_str, base_img)

    sections = [
        _SYNTAX,
        f"FROM {base_img}",
        DockerfileEnhancer._TARGETARCH_ARG
        + "\n"
        + f'ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"\n'
        + "\n"
        + DockerfileEnhancer._PROXY_ARGS,
        DockerfileEnhancer._ENV_BLOCK,
        "ENV CI=1",
        _LABELS.format(org=ORG, repo=REPO),
        DockerfileEnhancer._CERT_SYMLINKS,
        "WORKDIR /home/",
        apt_command,
    ]

    if extra_env:
        sections.append(extra_env)

    sections.append(f'RUN git clone "${{REPO_URL}}" /home/{REPO}')
    sections.append('CMD ["/bin/bash"]')

    return "\n\n".join(sections) + "\n"


def pr_dockerfile(image: Image) -> str:
    base = image.dependency()

    sections = [f"FROM {base.image_full_name()}"]

    if image.global_env:
        sections.append(image.global_env)

    sections.append(f"ARG BASE_COMMIT={image.pr.base.sha}")

    copy_commands = "".join(f"COPY {file.name} /home/\n" for file in image.files())
    if copy_commands:
        sections.append(copy_commands.rstrip("\n"))

    sections.append(f"WORKDIR /home/{REPO}")
    sections.append(Image._HARDENING_BLOCK.rstrip("\n"))
    sections.append("RUN bash /home/prepare.sh")

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

_ERAS = [
    (2000, "msw_2206_to_2000"),
    (1257, "msw_1369_to_1257"),
    (0, "msw_607_to_607"),
]


def resolve_era(pr: PullRequest) -> str:
    number = getattr(pr, "number", 0) or 0
    for lower_bound, era_key in _ERAS:
        if number >= lower_bound:
            return era_key
    return ""


if not getattr(Instance, "_msw_route_hook", False):
    _stock_create = Instance.create.__func__

    def _msw_create(cls, pr, config, *args, **kwargs):
        if getattr(pr, "org", "") == ORG and getattr(pr, "repo", "") == REPO:
            era_key = resolve_era(pr)
            if era_key and f"{pr.org}/{era_key}" in cls._registry:
                return cls._registry[f"{pr.org}/{era_key}"](pr, config, *args, **kwargs)
        return _stock_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_msw_create)
    Instance._msw_route_hook = True
