from __future__ import annotations

import posixpath
import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ORG = "JuliusBrussee"
_REPO = "caveman"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"caveman_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like caveman_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_ERA_KEY = f"{_ERA_HI}-{_ERA_LO}"
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

_PYTHON_IMAGE = "python:3.13.15-bookworm"
_GO_IMAGE = "golang:1.26.9-bookworm"
_NODE_IMAGE = "node:22.23.4-bookworm"
_GO_VERSION = "go" + _GO_IMAGE.split(":", 1)[1].split("-", 1)[0]
_NODE_VERSION = "v" + _NODE_IMAGE.split(":", 1)[1].split("-", 1)[0]

_GO_TEST_TIMEOUT = "30m"
_NODE_TEST_TIMEOUT_MS = 300000
_NODE_FILE_TIMEOUT_S = 900

_HELPER_DIR = "/home/test-helpers"
_RESULTS = "/tmp/test-results.txt"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_RESULT_RE = re.compile(r"^(PASSED|FAILED|SKIPPED) ((?:js|go|py):\S.*)$")
_BINARY_STUB_RE = re.compile(r"^Binary files (a/\S+|/dev/null) and (b/\S+|/dev/null) differ$", re.M)
_PATCH_FILES_DIR = "/home/patch-files"

_JS_TEST_RE = re.compile(
    r"^(?:(?P<root>.+?)/)?(?P<rel>tests?/(?:(?!fixtures/)[^/]+/)*[^/]+\.(?:test|runtime)\.mjs)$"
)
_JS_STANDALONE_RE = re.compile(r"^(?P<rel>tests/test_[^/]+\.js)$")
_PY_TEST_RE = re.compile(r"^(?:(?P<root>.+?)/)?(?P<rel>tests/test_[^/]+\.py)$")

_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_NODE_REPORT_MJS = r"""import { isAbsolute, relative } from 'node:path';

const ROOT = process.env.TEST_REPO_ROOT || process.cwd();
const MESSAGE_LIMIT = 400;
const seen = new Map();

function failureMessage(error) {
    try {
        const cause = error && error.cause ? error.cause : error;
        const text = String((cause && cause.message) || cause || '');
        return text.slice(0, MESSAGE_LIMIT * 2).replace(/\s+/g, ' ').trim().slice(0, MESSAGE_LIMIT);
    } catch (e) {
        return '<unprintable error>';
    }
}

function fileLabel(file) {
    if (!file) return '<unknown file>';
    const p = isAbsolute(file) ? relative(ROOT, file) : file;
    return p.split('\\').join('/');
}

function unique(line) {
    const n = (seen.get(line) || 0) + 1;
    seen.set(line, n);
    return n === 1 ? line : line + ' #' + n;
}

function flagged(v) {
    return v !== undefined && v !== false;
}

export default async function* leafReporter(source) {
    const open = new Map();
    for await (const event of source) {
        if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
        const data = event.data;
        const file = fileLabel(data.file);
        const nesting = data.nesting || 0;
        let name = String(data.name).replace(/\s+/g, ' ').trim();
        if (data.file && data.name === data.file) name = '<file>';

        let levels = open.get(file);
        if (!levels) {
            levels = [];
            open.set(file, levels);
        }

        const own = flagged(data.skip) || flagged(data.todo)
            ? 'SKIPPED'
            : event.type === 'test:fail' ? 'FAILED' : 'PASSED';
        const children = levels[nesting + 1] || [];
        levels.length = nesting + 1;

        let leaves;
        if (children.length === 0) {
            const message = own === 'FAILED' ? failureMessage(data.details && data.details.error) : '';
            leaves = [{ status: own, path: [name], message }];
        } else {
            leaves = children.map((c) => ({ status: c.status, path: [name, ...c.path], message: c.message }));
            if (own === 'FAILED' && !children.some((c) => c.status === 'FAILED')) {
                leaves.push({ status: 'FAILED', path: [name, '<hook or suite failure>'] });
            }
        }

        if (nesting === 0) {
            for (const leaf of leaves) {
                yield unique(leaf.status + ' js:' + file + ' > ' + leaf.path.join(' > ')) + '\n';
                if (leaf.message) yield '    | ' + leaf.message + '\n';
            }
        } else {
            if (!levels[nesting]) levels[nesting] = [];
            levels[nesting].push(...leaves);
        }
    }
}
"""

_NODE_FACTS_MJS = r"""import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const [cmd, dir = '.'] = process.argv.slice(2);
const pkg = (d) => JSON.parse(readFileSync(join(d, 'package.json'), 'utf8'));

if (cmd === 'node-flags') {
    const script = (pkg(dir).scripts || {}).test || '';
    const re = /--(import|require)(?:=|\s+)(\S+)/g;
    for (const m of script.matchAll(re)) console.log('--' + m[1] + '=' + m[2]);
} else if (cmd === 'engines-ok') {
    const spec = ((pkg('.').engines || {}).node || '').trim();
    const have = process.versions.node.split('.').map(Number);
    for (const part of spec.split(/\s+/).filter(Boolean)) {
        const m = /^>=\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?$/.exec(part);
        if (!m) throw new Error('unsupported engines.node clause: ' + part);
        const want = [Number(m[1]), Number(m[2] || 0), Number(m[3] || 0)];
        const cmp = have[0] - want[0] || have[1] - want[1] || have[2] - want[2];
        if (cmp < 0) throw new Error('node ' + process.version + ' does not satisfy ' + spec);
    }
    console.log('engines.node "' + spec + '" satisfied by ' + process.version);
} else if (cmd === 'package-manager') {
    const pm = pkg('.').packageManager || '';
    const m = /^pnpm@(\d+\.\d+\.\d+)/.exec(pm);
    if (!m) throw new Error('packageManager is not a pinned pnpm: ' + pm);
    console.log(m[1]);
} else {
    throw new Error('unknown command ' + cmd);
}
"""

_REPO_FACTS_PY = r"""import fnmatch
import glob
import os
import re
import shlex
import subprocess
import sys
import tomllib

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name


def norm(path):
    return os.path.normpath(path).replace(os.sep, "/").lstrip("./") or "."


def workflow_lines():
    for wf in sorted(glob.glob(".github/workflows/*.yml")):
        with open(wf, encoding="utf-8") as fh:
            for line in fh:
                yield line


def pip_lines():
    for line in workflow_lines():
        m = re.search(r"\bpip install (.+)$", line.strip())
        if not m:
            continue
        try:
            yield shlex.split(m.group(1))
        except ValueError:
            continue


def pyproject(root):
    path = os.path.join(root, "pyproject.toml")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def local_projects():
    out = {}
    for directory, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
        if "pyproject.toml" not in files:
            continue
        name = ((pyproject(directory) or {}).get("project") or {}).get("name")
        if name:
            out[canonicalize_name(name)] = norm(directory)
    return out


def ws_member(d):
    target = norm(d)
    if not os.path.isfile("pnpm-workspace.yaml"):
        return False
    with open("pnpm-workspace.yaml", encoding="utf-8") as fh:
        globs = re.findall(r"^\s*-\s*[\"']?([^\"'\s#]+)", fh.read(), re.M)
    return any(fnmatch.fnmatch(target, g) for g in globs if not g.startswith("!"))


def pip_args(root):
    target = norm(root)
    for args in pip_lines():
        if any("$" in a for a in args):
            continue
        out, hit = [], False
        for a in args:
            if a == "-e":
                continue
            path = a.split("[", 1)[0]
            if norm(path) == target and ("/" in a or a.startswith(".")):
                out += ["-e", "./" + target + a[len(path):]]
                hit = True
            else:
                out.append(a)
        if hit:
            return out
    return ["-e", "./" + target]


TEMP = r"(?:\$RUNNER_TEMP|\$env:RUNNER_TEMP|\$\{\{\s*runner\.temp\s*\}\})[/\\]"


def ci_go_bins():
    found = []
    for wf in sorted(glob.glob(".github/workflows/*.yml")):
        with open(wf, encoding="utf-8") as fh:
            text = fh.read()
        builds = re.findall(r"go build -o \"?" + TEMP + r"([\w.-]+?)(?:\.exe)?\"? (\./\S+)", text)
        envs = re.findall(r"^\s*([A-Z][A-Z0-9_]*):\s*" + TEMP + r"([\w.-]+?)(?:\.exe)?\s*$", text, re.M)
        for var, name in envs:
            for built, package in builds:
                if built == name and (var, package) not in found:
                    found.append((var, package))
    return found


def local_deps(root, seen=None):
    seen = seen if seen is not None else []
    data = pyproject(root) or {}
    projects = local_projects()
    for spec in (data.get("project") or {}).get("dependencies") or []:
        dep = projects.get(canonicalize_name(Requirement(spec).name))
        if dep and dep != norm(root) and dep not in seen:
            local_deps(dep, seen)
            seen.append(dep)
    return seen


def main():
    cmd, *rest = sys.argv[1:]
    if cmd == "ws-member":
        sys.exit(0 if ws_member(rest[0]) else 1)
    if cmd == "pytest-pin":
        pins = sorted({a for args in pip_lines() for a in args if re.fullmatch(r"pytest==[\w.]+", a)})
        assert len(pins) == 1, "expected one pytest pin in .github/workflows, found %r" % pins
        print(pins[0])
    elif cmd == "pip-args":
        print(shlex.join(pip_args(rest[0])))
    elif cmd == "local-deps":
        for dep in local_deps(rest[0]):
            print(dep)
    elif cmd == "requires-python-ok":
        data = pyproject(rest[0]) or {}
        spec = (data.get("project") or {}).get("requires-python") or ""
        have = "%d.%d.%d" % sys.version_info[:3]
        assert SpecifierSet(spec).contains(have), "python %s does not satisfy %r" % (have, spec)
        print("%s requires-python %r satisfied by %s" % (rest[0], spec, have))
    elif cmd == "go-floor-ok":
        with open("go.mod", encoding="utf-8") as fh:
            want = re.search(r"^go (\d+(?:\.\d+)*)\s*$", fh.read(), re.M).group(1)
        have = subprocess.check_output(["go", "env", "GOVERSION"], text=True).strip()[2:]
        key = lambda v: [int(p) for p in v.split(".")]
        assert key(have) >= key(want), "go %s is older than go.mod's go %s" % (have, want)
        print("go.mod go %s satisfied by go %s" % (want, have))
    elif cmd == "ci-go-bins":
        for var, package in ci_go_bins():
            print(var, package)
    else:
        raise SystemExit("unknown command " + cmd)


main()
"""

_GO_REPORT_PY = r"""import json
import sys

module, path = sys.argv[1], sys.argv[2]
STATUS = {"pass": "PASSED", "fail": "FAILED", "skip": "SKIPPED"}


def label(package):
    if package == module:
        return "."
    if package.startswith(module + "/"):
        return package[len(module) + 1:]
    return None


with open(path, encoding="utf-8", errors="replace") as fh:
    for raw in fh:
        line = raw.strip()
        try:
            event = json.loads(line) if line.startswith("{") else None
        except ValueError:
            event = None
        if not isinstance(event, dict):
            sys.stderr.write(raw)
            continue
        if event.get("Action") == "output" and not event.get("Test"):
            sys.stderr.write(event.get("Output") or "")
        status = STATUS.get(event.get("Action"))
        package = label(event.get("Package") or "")
        if status is None or package is None:
            continue
        test = event.get("Test")
        if test:
            print(status + " go:" + package + "::" + test)
        elif status == "FAILED" and event.get("FailedBuild"):
            print("FAILED go:" + package + "::[build failed]")
"""

_PYTEST_REPORT_PY = r"""import os

_RESULTS = os.environ["TEST_RESULTS_FILE"]
_PREFIX = os.environ.get("TEST_PY_PREFIX", "")


def _emit(status, nodeid):
    with open(_RESULTS, "a", encoding="utf-8") as fh:
        fh.write(status + " py:" + _PREFIX + " ".join(nodeid.split()) + "\n")


def pytest_runtest_logreport(report):
    if report.when == "call":
        status = "PASSED" if report.passed else "SKIPPED" if report.skipped else "FAILED"
    elif report.failed:
        status = "FAILED"
    elif report.when == "setup" and report.skipped:
        status = "SKIPPED"
    else:
        return
    _emit(status, report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _emit("FAILED", (report.nodeid or "<session>") + " > <collection error>")
"""

_HELPERS = (
    ("node-report.mjs", _NODE_REPORT_MJS, "__NODE_REPORT_EOF__"),
    ("node-facts.mjs", _NODE_FACTS_MJS, "__NODE_FACTS_EOF__"),
    ("repo_facts.py", _REPO_FACTS_PY, "__REPO_FACTS_EOF__"),
    ("go_report.py", _GO_REPORT_PY, "__GO_REPORT_EOF__"),
    ("test_report_plugin.py", _PYTEST_REPORT_PY, "__PYTEST_REPORT_EOF__"),
)


class _Plan:

    def __init__(self, pr: PullRequest):
        paths = sorted(set(_DIFF_HEADER_RE.findall(pr.test_patch or "")))

        self.js: dict[str, list[str]] = {}
        self.go_pkgs: list[str] = []
        self.py: dict[str, list[str]] = {}

        for path in paths:
            js = _JS_TEST_RE.match(path) or _JS_STANDALONE_RE.match(path)
            py = _PY_TEST_RE.match(path)
            if js:
                root = js.groupdict().get("root") or "."
                self.js.setdefault(root, []).append(js.group("rel"))
            elif py:
                self.py.setdefault(py.group("root") or ".", []).append(py.group("rel"))
            elif path.endswith("_test.go"):
                directory = posixpath.dirname(path)
                package = f"./{directory}" if directory else "."
                if package not in self.go_pkgs:
                    self.go_pkgs.append(package)
        self.go_pkgs.sort()

        if not (self.js or self.go_pkgs or self.py):
            raise ValueError(
                f"{pr.org}/{pr.repo} PR #{pr.number}: test patch touches no runnable test file"
            )

    @property
    def js_package_dirs(self) -> list[str]:
        return [d for d in self.js if d != "."]

    @property
    def py_package_roots(self) -> list[str]:
        return [r for r in self.py if r != "."]


def _binary_stubs(patch: str) -> list[tuple[str, bool]]:
    stubs: list[tuple[str, bool]] = []
    for old, new in _BINARY_STUB_RE.findall(patch or ""):
        deleted = new == "/dev/null"
        path = (old if deleted else new)[2:]
        if (path, deleted) not in stubs:
            stubs.append((path, deleted))
    return stubs


def _restore_stubs(kind: str, stubs: list[tuple[str, bool]]) -> str:
    lines = ""
    for path, deleted in stubs:
        if deleted:
            lines += f"rm -f {path}\n"
        else:
            lines += f'mkdir -p "$(dirname {path})"\ncp {_PATCH_FILES_DIR}/{kind}/{path} {path}\n'
    return lines


def _apply_sh(pr: PullRequest, with_fix: bool) -> str:
    kinds = [("test", pr.test_patch)] + ([("fix", pr.fix_patch)] if with_fix else [])
    excludes = "".join(
        f" --exclude={path}" for _, patch in kinds for path, _ in _binary_stubs(patch)
    )
    patches = " ".join(f"/home/{kind}.patch" for kind, _ in kinds)
    lines = f"git apply --whitespace=nowarn{excludes} {patches}"
    restores = "".join(_restore_stubs(kind, _binary_stubs(patch)) for kind, patch in kinds)
    if restores:
        lines += "\n# content-less binary diffs, restored from the PR head (see prepare.sh)\n"
        lines += restores.rstrip("\n")
    return lines


def _stub_capture_sh(pr: PullRequest) -> str:
    kinds = [(k, _binary_stubs(p)) for k, p in (("test", pr.test_patch), ("fix", pr.fix_patch))]
    if not any(stubs for _, stubs in kinds):
        return ""
    out = (
        "\n"
        "# The patches carry some files only as \"Binary files ... differ\" (no content), which\n"
        "# git apply rejects. Their post-patch content is the PR head's: the second parent of\n"
        "# the PR's merge commit, which is in this clone's history.\n"
        f'merge="$(git log --all --merges -n 1 --format=%H --grep="^Merge pull request #{pr.number} from ")"\n'
        'test -n "$merge"\n'
        'head="$(git rev-parse "$merge^2")"\n'
        'echo "PR head for binary diffs: $head (merge $merge)"\n'
    )
    for kind, stubs in kinds:
        for path, deleted in stubs:
            if deleted:
                continue
            target = f"{_PATCH_FILES_DIR}/{kind}/{path}"
            out += f'mkdir -p "$(dirname {target})"\n' f'git show "$head:{path}" > {target}\n'
    return out


def _stub_gate_sh(pr: PullRequest) -> str:
    out = ""
    for kind, patch in (("test", pr.test_patch), ("fix", pr.fix_patch)):
        for path, deleted in _binary_stubs(patch):
            if not deleted:
                out += f"test -s {_PATCH_FILES_DIR}/{kind}/{path}\n"
    return out


def _helper(name: str) -> str:
    return f"{_HELPER_DIR}/{name}"


def _words(items: list[str]) -> str:
    return " ".join(items)


def _existing(var: str, files: list[str]) -> str:
    return (
        f"{var}=()\n"
        f"for f in {_words(files)}; do\n"
        f'  if [ -f "$f" ]; then {var}+=("$f"); fi\n'
        "done\n"
    )


def _indent(text: str) -> str:
    return "".join(f"  {line}\n" if line else "\n" for line in text.splitlines())


def _ws_select(plan: _Plan) -> str:
    return (
        "ws_filters=()\n"
        "npm_dirs=()\n"
        f"for d in {_words(plan.js_package_dirs)}; do\n"
        '  if [ ! -f "$d/package.json" ]; then continue; fi\n'
        f'  if python3 {_helper("repo_facts.py")} ws-member "$d"; then\n'
        '    ws_filters+=(--filter "{./$d}...")\n'
        '  elif [ -f "$d/package-lock.json" ]; then\n'
        '    npm_dirs+=("$d")\n'
        "  fi\n"
        "done\n"
    )


def _ws_build(rc_var: str) -> str:
    return (
        'if [ "${#ws_filters[@]}" -gt 0 ]; then\n'
        f'  pnpm --no-bail "${{ws_filters[@]}}" run build || {rc_var}=$?\n'
        "fi\n"
    )


def _select_go(plan: _Plan) -> str:
    return (
        "pkgs=()\n"
        f"for p in {_words(plan.go_pkgs)}; do\n"
        '  if compgen -G "$p/*.go" > /dev/null; then pkgs+=("$p"); fi\n'
        "done\n"
    )


def _node_suite(directory: str, files: list[str], tag: str) -> str:
    body = (
        f"cd {directory}\n"
        f"{_existing('files', files)}"
        'if [ "${#files[@]}" -eq 0 ]; then exit 0; fi\n'
        f"mapfile -t flags < <(node {_helper('node-facts.mjs')} node-flags .)\n"
        "suite_rc=0\n"
        'for f in "${files[@]}"; do\n'
        "  file_rc=0\n"
        f"  rm -f /tmp/test-{tag}-file.txt\n"
        f"  timeout -k 30 {_NODE_FILE_TIMEOUT_S} node --test --test-force-exit "
        f'--test-timeout={_NODE_TEST_TIMEOUT_MS} "${{flags[@]}}" \\\n'
        f"    --test-reporter={_helper('node-report.mjs')} "
        f'--test-reporter-destination=/tmp/test-{tag}-file.txt "$f" || file_rc=$?\n'
        '  if [ "$file_rc" -eq 124 ] || [ "$file_rc" -eq 137 ]; then\n'
        f'    echo "WARNING: $f hit the {_NODE_FILE_TIMEOUT_S}s limit; its remaining tests have no result" >&2\n'
        "  fi\n"
        f"  if [ -f /tmp/test-{tag}-file.txt ]; then cat /tmp/test-{tag}-file.txt >> /tmp/test-{tag}.txt; fi\n"
        '  if [ "$file_rc" -ne 0 ]; then suite_rc=$file_rc; fi\n'
        "done\n"
        'exit "$suite_rc"\n'
    )
    return (
        f"# --- js: {directory} ---\n"
        "(\n"
        f"{_indent(body)}"
        ") || rc=$?\n"
        f"if [ -f /tmp/test-{tag}.txt ]; then\n"
        f"  cat /tmp/test-{tag}.txt\n"
        f"  awk '/^(PASSED|FAILED|SKIPPED) /' /tmp/test-{tag}.txt >> {_RESULTS}\n"
        "fi\n"
    )


def _go_suite(plan: _Plan) -> str:
    return (
        "# --- go ---\n"
        f"{_select_go(plan)}"
        'if [ "${#pkgs[@]}" -gt 0 ]; then\n'
        f'  go test -json -count=1 -vet=off -timeout={_GO_TEST_TIMEOUT} "${{pkgs[@]}}" '
        "> /tmp/test-go.json 2>&1 || rc=$?\n"
        f'  python3 {_helper("go_report.py")} "$(go list -m)" /tmp/test-go.json >> {_RESULTS}\n'
        "fi\n"
    )


def _py_suite(root: str, files: list[str]) -> str:
    prefix = "" if root == "." else root + "/"
    body = (
        f"cd {root}\n"
        f"{_existing('files', files)}"
        'if [ "${#files[@]}" -eq 0 ]; then exit 0; fi\n'
        f'TEST_PY_PREFIX="{prefix}" TEST_RESULTS_FILE={_RESULTS} '
        f"PYTHONPATH={_HELPER_DIR} \\\n"
        "  python3 -m pytest -p test_report_plugin -p no:cacheprovider --rootdir=. \\\n"
        '  --continue-on-collection-errors -q "${files[@]}"\n'
    )
    return f"# --- py: {root} ---\n(\n{_indent(body)}) || rc=$?\n"


def _prepare_sh(pr: PullRequest) -> str:
    plan = _Plan(pr)
    out: list[str] = [
        "#!/bin/bash\n"
        "set -e\n"
        "\n"
        "export CI=true\n"
        "export HUSKY=0\n"
        "export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1\n"
        "export PUPPETEER_SKIP_DOWNLOAD=1\n"
        "\n"
        "retry() {\n"
        "  local attempt\n"
        "  for attempt in 1 2 3 4 5; do\n"
        '    if "$@"; then return 0; fi\n'
        '    echo "prepare: \'$*\' attempt $attempt failed" >&2\n'
        "    sleep $((attempt * 10))\n"
        "  done\n"
        '  echo "prepare: \'$*\' failed after 5 attempts" >&2\n'
        "  return 1\n"
        "}\n"
        "\n"
        "# --- 1. pin ---\n"
        f"cd /home/{pr.repo}\n"
        "git reset --hard\n"
        "git clean -fdx\n"
        "bash /home/check_git_changes.sh\n"
        f"git checkout --detach {pr.base.sha}\n"
        f'test "$(git rev-parse HEAD)" = "{pr.base.sha}"\n'
        "bash /home/check_git_changes.sh\n"
        "\n"
        "# --- 2. provision ---\n"
        "# Result reporters and repo-fact readers the scripts use; written outside the repo.\n"
        f"mkdir -p {_HELPER_DIR}\n"
    ]
    for name, content, eof in _HELPERS:
        out.append(f"cat > {_helper(name)} <<'{eof}'\n{content}{eof}\n")

    out.append(_stub_capture_sh(pr))

    out.append(
        "\n"
        f"node {_helper('node-facts.mjs')} engines-ok\n"
        "# Installs below keep going on failure (a native module that cannot build on one\n"
        "# arch must not abort the image); the gate in section 3 fails the build instead.\n"
    )

    if plan.js_package_dirs:
        out.append(
            "\n"
            "# pnpm is the version package.json's packageManager names (corepack reads it).\n"
            f"{_ws_select(plan)}"
            "build_rc=0\n"
            'if [ "${#ws_filters[@]}" -gt 0 ]; then\n'
            "  retry pnpm install --frozen-lockfile || true\n"
            "fi\n"
            "# Workspace packages under test are built with their workspace deps here and\n"
            "# rebuilt by the run scripts after the patches apply; dist/ is gitignored.\n"
            f"{_ws_build('build_rc')}"
            "# Directories outside the workspace install from their own package-lock.json.\n"
            'for d in "${npm_dirs[@]}"; do\n'
            '  retry npm --prefix "$d" ci --no-audit --no-fund || true\n'
            "done\n"
        )

    if plan.go_pkgs or plan.js_package_dirs:
        out.append(
            "\n"
            "# Go modules: for the Go suites, and for the Go binaries CI hands the JS package\n"
            "# tests through env vars (the run scripts build those offline).\n"
            "retry go mod download || true\n"
            "go build ./...\n"
        )

    if plan.py:
        out.append(
            "\n"
            "# Python deps are unlocked, so resolve them as of the base commit's date: the same\n"
            "# versions on every rebuild. pytest is the version the CI workflows pin.\n"
            'cutoff="$(git log -1 --format=%cI HEAD)"\n'
            f'pytest_pin="$(python3 {_helper("repo_facts.py")} pytest-pin)"\n'
            'retry python3 -m pip install --uploaded-prior-to "$cutoff" "$pytest_pin" || true\n'
        )
        if plan.py_package_roots:
            out.append(
                "# Each Python package under test, after the repo-local packages it depends on,\n"
                "# installed editable with the args its CI workflow uses.\n"
                "py_roots=()\n"
                f"for r in {_words(plan.py_package_roots)}; do\n"
                f'  mapfile -t deps < <(python3 {_helper("repo_facts.py")} local-deps "$r")\n'
                '  for d in "${deps[@]}" "$r"; do\n'
                '    if [ -z "$d" ]; then continue; fi\n'
                '    case " ${py_roots[*]} " in *" $d "*) ;; *) py_roots+=("$d") ;; esac\n'
                "  done\n"
                "done\n"
                'for r in "${py_roots[@]}"; do\n'
                f'  eval "args=($(python3 {_helper("repo_facts.py")} pip-args "$r"))"\n'
                '  retry python3 -m pip install --uploaded-prior-to "$cutoff" "${args[@]}" || true\n'
                "done\n"
            )

    out.append(
        "\n"
        "# --- 3. gate ---\n"
    )
    for name, _, _ in _HELPERS:
        out.append(f"test -s {_helper(name)}\n")
    out.append(
        f"node --check {_helper('node-report.mjs')}\n"
        f"node --check {_helper('node-facts.mjs')}\n"
        f"python3 -m py_compile {_helper('repo_facts.py')} {_helper('go_report.py')} "
        f"{_helper('test_report_plugin.py')}\n"
        f"{_stub_gate_sh(pr)}"
    )
    if plan.js_package_dirs:
        out.append(
            'if [ "${#ws_filters[@]}" -gt 0 ]; then\n'
            f'  test "$(pnpm --version)" = "$(node {_helper("node-facts.mjs")} package-manager)"\n'
            "  test -d node_modules/.pnpm\n"
            "fi\n"
            'if [ "$build_rc" -ne 0 ]; then\n'
            '  echo "prepare: workspace build failed at the base commit (rc=$build_rc)" >&2\n'
            '  exit "$build_rc"\n'
            "fi\n"
            f"for d in {_words(plan.js_package_dirs)}; do\n"
            '  if [ -f "$d/package.json" ]; then test -d "$d/node_modules"; fi\n'
            "done\n"
        )
    if plan.go_pkgs or plan.js_package_dirs:
        out.append(f"python3 {_helper('repo_facts.py')} go-floor-ok\n")
    if plan.js_package_dirs:
        out.append(
            "# The CI binaries the run scripts build must compile offline at the base commit.\n"
            "while read -r var pkg; do\n"
            '  echo "ci binary: $var <- $pkg"\n'
            '  GOPROXY=off go build -o /dev/null "$pkg"\n'
            f"done < <(python3 {_helper('repo_facts.py')} ci-go-bins)\n"
        )
    if plan.go_pkgs:
        out.append(
            f"{_select_go(plan)}"
            'if [ "${#pkgs[@]}" -gt 0 ]; then\n'
            "  GOPROXY=off go test -count=1 -vet=off -run '^$' -exec /bin/true \"${pkgs[@]}\"\n"
            "fi\n"
        )
    if plan.py:
        out.append(
            'test "pytest==$(python3 -c \'import pytest; print(pytest.__version__)\')" = "$pytest_pin"\n'
        )
        if plan.py_package_roots:
            out.append(
                'for r in "${py_roots[@]}"; do\n'
                f'  python3 {_helper("repo_facts.py")} requires-python-ok "$r"\n'
                "done\n"
                f"for r in {_words(plan.py_package_roots)}; do\n"
                '  n=$(cd "$r" && python3 -m pytest --collect-only -q -p no:cacheprovider tests '
                "| python3 -c 'import sys; print(sum(\"::\" in l for l in sys.stdin))')\n"
                '  echo "pytest $r: $n tests collected"\n'
                '  test "$n" -gt 0\n'
                "done\n"
            )
    out.append('echo "DEPS_OK"\n')
    return "".join(out)


def _run_sh(pr: PullRequest, apply_line: str) -> str:
    plan = _Plan(pr)
    out: list[str] = [
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        "export NODE_ENV=test\n"
        'export NODE_OPTIONS="--max-old-space-size=4096"\n'
        "export GOPROXY=off\n"
        f"export TEST_REPO_ROOT=/home/{pr.repo}\n"
        "\n"
        f"cd /home/{pr.repo}\n"
    ]
    if apply_line:
        out.append("git reset --hard\n" f"{apply_line}\n")
    out.append("\n" f"rm -f {_RESULTS} /tmp/test-*.txt /tmp/test-go.json\n" "rc=0\n")

    if plan.js_package_dirs:
        out.append(
            "\n"
            "# --- rebuild the workspace packages under test against this tree ---\n"
            f"{_ws_select(plan)}"
            "build_rc=0\n"
            f"{_ws_build('build_rc')}"
            'if [ "$build_rc" -ne 0 ]; then\n'
            '  echo "WARNING: workspace build exited $build_rc" >&2\n'
            "fi\n"
            "# Go binaries CI builds and passes to these tests through env vars.\n"
            "mkdir -p /tmp/test-bin\n"
            "while read -r var pkg; do\n"
            '  if go build -o "/tmp/test-bin/$var" "$pkg"; then\n'
            '    export "$var=/tmp/test-bin/$var"\n'
            "  else\n"
            '    echo "WARNING: go build $pkg for $var failed" >&2\n'
            "  fi\n"
            f"done < <(python3 {_helper('repo_facts.py')} ci-go-bins)\n"
        )

    for i, (directory, files) in enumerate(plan.js.items()):
        out.append("\n" + _node_suite(directory, files, f"js{i}"))
    if plan.go_pkgs:
        out.append("\n" + _go_suite(plan))
    for root, files in plan.py.items():
        out.append("\n" + _py_suite(root, files))

    out.append(
        "\n"
        'echo "===== test results ====="\n'
        f"if [ -f {_RESULTS} ]; then cat {_RESULTS}; fi\n"
        f"if ! grep -qE '^(PASSED|FAILED|SKIPPED) ' {_RESULTS} 2>/dev/null; then\n"
        '  echo "no test results were recorded (rc=$rc)" >&2\n'
        '  exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
        'exit "$rc"\n'
    )
    return "".join(out)


class CavemanImageBase(Image):
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
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        dockerfile = f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {_PYTHON_IMAGE}

{DockerfileEnhancer._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

ENV LC_ALL=C.UTF-8 \\
    GOTOOLCHAIN=local \\
    GOPATH=/root/go \\
    CGO_ENABLED=0 \\
    PATH=/usr/local/go/bin:/root/go/bin:$PATH \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

COPY --from={_GO_IMAGE} /usr/local/go /usr/local/go
COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    ln -sf ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack && \\
    corepack enable pnpm

RUN git --version && \\
    test "$(go env GOVERSION)" = "{_GO_VERSION}" && \\
    test "$(node --version)" = "{_NODE_VERSION}" && \\
    npm --version && \\
    python3 --version && \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""
        return _BLANK_RUN_RE.sub("\n\n", dockerfile)


class CavemanImageDefault(Image):
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
        return CavemanImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _prepare_sh(self.pr)),
            File(".", "run.sh", _run_sh(self.pr, "")),
            File(".", "test-run.sh", _run_sh(self.pr, _apply_sh(self.pr, with_fix=False))),
            File(".", "fix-run.sh", _run_sh(self.pr, _apply_sh(self.pr, with_fix=True))),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile = f"""FROM {image.image_full_name()}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK.rstrip(chr(10))}

{self.clear_env}
"""
        return _BLANK_RUN_RE.sub("\n\n", dockerfile)


@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_LO}-{_ERA_HI}")
@Instance.register(_ORG, _ERA_KEY)
class CAVEMAN_1199_TO_784(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return CavemanImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"PASSED": passed, "FAILED": failed, "SKIPPED": skipped}

        for line in _ANSI_RE.sub("", test_log).splitlines():
            m = _RESULT_RE.match(line.rstrip())
            if m:
                buckets[m.group(1)].add(m.group(2))

        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


def _owns(pr: PullRequest) -> bool:
    if getattr(pr, "org", "") != _ORG or getattr(pr, "repo", "") != _REPO:
        return False
    try:
        number = int(getattr(pr, "number", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not _ERA_LO <= number <= _ERA_HI:
        return False
    interval = str(getattr(pr, "number_interval", "") or "")
    return interval in ("", _ERA_FILE, _ERA_KEY, f"{_ERA_LO}-{_ERA_HI}")


_HOOK_FLAG = f"_{_ERA_FILE}_route_hook"

if not getattr(Instance, _HOOK_FLAG, False):
    _previous_create = Instance.create.__func__

    def _era_create(cls, pr, config, *args, **kwargs):
        if _owns(pr):
            return CAVEMAN_1199_TO_784(pr, config, *args, **kwargs)
        return _previous_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_era_create)
    setattr(Instance, _HOOK_FLAG, True)
