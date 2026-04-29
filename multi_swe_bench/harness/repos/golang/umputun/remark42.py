import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GO_VERSION_BOUNDARIES = [
    (616, "golang:1.13"),
    (857, "golang:1.14"),
    (1029, "golang:1.15"),
    (1246, "golang:1.16"),
    (1649, "golang:1.17"),
]
_GO_VERSION_DEFAULT = "golang:1.25"

_NODE_VERSION_BOUNDARIES = [
    (760, "10.24.1"),
    (1246, "12.22.12"),
    (1320, "16.20.2"),
]
_NODE_VERSION_DEFAULT = "16.20.2"

_PKG_MANAGER_BOUNDARIES = [
    (1320, "npm"),
]
_PKG_MANAGER_DEFAULT = "pnpm"

_FRONTEND_LAYOUT_BOUNDARIES = [
    (1320, "flat"),
]
_FRONTEND_LAYOUT_DEFAULT = "monorepo"


def _resolve_boundary(pr, boundaries, default):
    for max_pr, value in boundaries:
        if pr.number <= max_pr:
            return value
    return default


def _resolve_go_image(pr):
    return _resolve_boundary(pr, _GO_VERSION_BOUNDARIES, _GO_VERSION_DEFAULT)


def _resolve_node_version(pr):
    return _resolve_boundary(pr, _NODE_VERSION_BOUNDARIES, _NODE_VERSION_DEFAULT)


def _resolve_pkg_manager(pr):
    return _resolve_boundary(pr, _PKG_MANAGER_BOUNDARIES, _PKG_MANAGER_DEFAULT)


def _resolve_frontend_layout(pr):
    return _resolve_boundary(pr, _FRONTEND_LAYOUT_BOUNDARIES, _FRONTEND_LAYOUT_DEFAULT)


def _detect_test_scope(pr):
    combined = (pr.test_patch or "") + (pr.fix_patch or "")
    has_backend = "backend/" in combined
    has_frontend = "frontend/" in combined
    if not has_backend and not has_frontend:
        has_backend = True
    return has_backend, has_frontend


def _extract_go_test_packages(pr):
    """Extract Go packages that contain test files touched by the test_patch.

    Returns a list of package paths relative to backend/app (e.g. ['./rest/api', './notify'])
    or None if we should run all packages (./...).
    """
    test_patch = pr.test_patch or ""
    if not test_patch:
        return None

    # Find all test files in the patch
    files = re.findall(r"diff --git a/(backend/app/.+?_test\.go) b/", test_patch)
    if not files:
        return None

    packages = set()
    for f in files:
        # backend/app/rest/api/rest_public_test.go -> ./rest/api
        rel = f.replace("backend/app/", "")
        parts = rel.split("/")
        if len(parts) == 1:
            packages.add(".")
        else:
            packages.add("./" + "/".join(parts[:-1]))

    return sorted(packages) if packages else None


def _extract_go_test_functions(pr):
    """Extract Go test function names added or modified by the test_patch.

    Parses the unified diff for added lines that define test functions
    (func TestXxx) inside _test.go files.

    Returns a list of test function names, or None if none found.
    """
    test_patch = pr.test_patch or ""
    if not test_patch:
        return None

    funcs = set()
    in_test_file = False

    for line in test_patch.splitlines():
        # Track which file we're in
        if line.startswith("diff --git"):
            in_test_file = "_test.go" in line
            continue
        if not in_test_file:
            continue
        # Match added test function definitions
        if line.startswith("+") and not line.startswith("+++"):
            m = re.match(r"\+func\s+(Test\w+)\s*\(", line)
            if m:
                funcs.add(m.group(1))
        # Also capture test functions from @@ hunk headers (modified existing tests)
        if line.startswith("@@"):
            m = re.search(r"func\s+(Test\w+)\s*\(", line)
            if m:
                funcs.add(m.group(1))

    return sorted(funcs) if funcs else None


class Remark42ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config, go_image: str):
        self._pr = pr
        self._config = config
        self._go_image = go_image

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._go_image

    def image_tag(self) -> str:
        version_slug = self._go_image.replace("golang:", "go").replace(".", "_")
        node_major = _resolve_node_version(self.pr).split(".")[0]
        return f"base-{version_slug}-node{node_major}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def _is_buster_based(self) -> bool:
        """golang:1.13, 1.14, 1.15 use Debian Buster (EOL, repos return 404)."""
        go_image = self._go_image
        for ver in ("1.13", "1.14", "1.15"):
            if f"golang:{ver}" == go_image:
                return True
        return False

    def dockerfile(self) -> str:
        pr = self.pr
        node_version = _resolve_node_version(pr)
        pkg_manager = _resolve_pkg_manager(pr)

        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            clone_cmd = f"RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}"
        else:
            clone_cmd = f"COPY {pr.repo} /home/{pr.repo}"

        # Fix for Debian Buster EOL - redirect to archive.debian.org
        if self._is_buster_based():
            apt_fix = (
                "RUN sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' /etc/apt/sources.list && \\\n"
                "    sed -i 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' /etc/apt/sources.list && \\\n"
                "    sed -i '/buster-updates/d' /etc/apt/sources.list\n\n"
            )
        else:
            apt_fix = ""

        node_install = (
            f"RUN apt-get update && apt-get install -y curl xz-utils && \\\n"
            f"    ARCH=$(dpkg --print-architecture) && \\\n"
            f"    if [ \"$ARCH\" = \"arm64\" ]; then ARCH=\"arm64\"; else ARCH=\"x64\"; fi && \\\n"
            f"    curl -fsSL https://nodejs.org/dist/v{node_version}/node-v{node_version}-linux-$ARCH.tar.xz | \\\n"
            f"    tar -xJ -C /usr/local --strip-components=1 && \\\n"
            f"    apt-get clean && rm -rf /var/lib/apt/lists/*"
        )

        # Always install pnpm - base image is shared across PRs and some need pnpm
        # Pin pnpm@7 for Node 16 (pnpm 8+ requires Node >= 18)
        node_major = int(node_version.split(".")[0])
        if node_major < 18:
            pnpm_install = "\nRUN npm install -g pnpm@7"
        else:
            pnpm_install = "\nRUN npm install -g pnpm"

        return f"""FROM {image_name}

{apt_fix}RUN apt-get update && apt-get install -y git gcc libc-dev make python3 && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

{node_install}
{pnpm_install}

{self.global_env}

ENV GOFLAGS="-mod=vendor"
ENV GONOSUMCHECK=*
ENV GONOSUMDB=*
ENV CGO_ENABLED=1

{clone_cmd}

{self.clear_env}

WORKDIR /home/{pr.repo}

"""


class Remark42ImageDefault(Image):
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
        go_image = _resolve_go_image(self.pr)
        return Remark42ImageBase(self.pr, self.config, go_image)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        has_backend, has_frontend = _detect_test_scope(pr)
        pkg_manager = _resolve_pkg_manager(pr)
        frontend_layout = _resolve_frontend_layout(pr)

        if frontend_layout == "monorepo":
            fe_test_dir = f"/home/{pr.repo}/frontend/apps/remark42"
            fe_install_cmd = f"cd /home/{pr.repo}/frontend && pnpm install --no-frozen-lockfile || pnpm install || true"
        else:
            fe_test_dir = f"/home/{pr.repo}/frontend"
            if pkg_manager == "npm":
                fe_install_cmd = f"cd {fe_test_dir} && (npm install --legacy-peer-deps || npm install || true) && (npm install @swc/core --legacy-peer-deps 2>/dev/null || true)"
            else:
                fe_install_cmd = f"cd {fe_test_dir} && pnpm install --no-frozen-lockfile || pnpm install || true"

        go_packages = _extract_go_test_packages(pr)
        go_pkg_target = " ".join(go_packages) if go_packages else "./..."

        go_test_funcs = _extract_go_test_functions(pr)
        run_filter = f" -run '^({'|'.join(go_test_funcs)})$'" if go_test_funcs else ""

        go_test_cmd = f"cd /home/{pr.repo}/backend/app && go test -race -v -count=1 -timeout=300s{run_filter} {go_pkg_target} || true"
        if pr.number <= 616:
            go_test_cmd = f"cd /home/{pr.repo}/backend/app && go test -v -count=1 -timeout=300s{run_filter} {go_pkg_target} || true"

        fe_test_cmd = f"cd {fe_test_dir} && npx jest --verbose --no-watchAll 2>&1 || true"

        lockfile_excludes = "--exclude '*/package-lock.json' --exclude '*/pnpm-lock.yaml' --exclude '*/yarn.lock'"

        be_run_cmd = go_test_cmd.replace(" || true", "")
        fe_run_cmd = fe_test_cmd.replace(" || true", "")

        run_parts = []
        if has_backend:
            run_parts.append(be_run_cmd)
        if has_frontend:
            run_parts.append(fe_run_cmd)

        return [
            File(
                ".",
                "fix.patch",
                f"{pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{pr.test_patch}",
            ),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh
{fe_install}
{go_test}

""".format(
                    repo=pr.repo,
                    base_sha=pr.base.sha,
                    fe_install=fe_install_cmd if has_frontend else "",
                    go_test=go_test_cmd if has_backend else "",
                ),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\nset -e\n\n" + "\n".join(run_parts) + "\n\n",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{go_checkout}
git apply --whitespace=nowarn {lockfile_excludes} /home/test.patch || git apply --whitespace=nowarn {lockfile_excludes} --reject /home/test.patch || true
{fe_install}
{run_cmds}

""".format(
                    repo=pr.repo,
                    go_checkout="git checkout -- backend/go.mod backend/go.sum 2>/dev/null || true" if has_backend else "",
                    lockfile_excludes=lockfile_excludes,
                    fe_install=fe_install_cmd if has_frontend else "",
                    run_cmds="\n".join(run_parts),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{go_checkout}
(git apply --whitespace=nowarn {lockfile_excludes} /home/test.patch || git apply --whitespace=nowarn {lockfile_excludes} --reject /home/test.patch || true) && \
(git apply --whitespace=nowarn {lockfile_excludes} /home/fix.patch || git apply --whitespace=nowarn {lockfile_excludes} --reject /home/fix.patch || true)
{fe_install}
{run_cmds}

""".format(
                    repo=pr.repo,
                    go_checkout="git checkout -- backend/go.mod backend/go.sum 2>/dev/null || true" if has_backend else "",
                    lockfile_excludes=lockfile_excludes,
                    fe_install=fe_install_cmd if has_frontend else "",
                    run_cmds="\n".join(run_parts),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

_RE_GO_PASS = re.compile(r"--- PASS: (\S+)")
_RE_GO_FAIL = re.compile(r"--- FAIL: (\S+)")
_RE_GO_SKIP = re.compile(r"--- SKIP: (\S+)")
_RE_GO_FAIL_PKG = re.compile(r"FAIL\s+(\S+)\s")

_RE_JEST_PASS_SUITE = re.compile(r"^PASS\s+(\S+)(\s+\(.+\))?$")
_RE_JEST_FAIL_SUITE = re.compile(r"^FAIL\s+(\S+)(\s+\(.+\))?$")
_RE_JEST_PASS_TEST = re.compile(r"^\s*[✔✓]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$")
_RE_JEST_FAIL_TEST = re.compile(r"^\s*[×✕✗✘✖]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$")
_RE_JEST_SKIP_TEST = re.compile(r"^\s*○\s+(?:skipped\s+)?(.*?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$")


def _get_go_base_name(test_name):
    idx = test_name.rfind("/")
    if idx != -1:
        return test_name[:idx]
    return test_name


def _parse_go_log(log):
    passed = set()
    failed = set()
    skipped = set()

    for line in log.splitlines():
        line = line.strip()

        m = _RE_GO_PASS.match(line)
        if m:
            name = _get_go_base_name(m.group(1))
            if name in failed:
                continue
            if name in skipped:
                skipped.remove(name)
            passed.add(name)
            continue

        m = _RE_GO_FAIL.match(line)
        if m:
            name = _get_go_base_name(m.group(1))
            if name in passed:
                passed.remove(name)
            if name in skipped:
                skipped.remove(name)
            failed.add(name)
            continue

        m = _RE_GO_FAIL_PKG.match(line)
        if m:
            name = m.group(1)
            if name in passed:
                passed.remove(name)
            if name in skipped:
                skipped.remove(name)
            failed.add(name)
            continue

        m = _RE_GO_SKIP.match(line)
        if m:
            name = _get_go_base_name(m.group(1))
            if name in passed:
                continue
            if name in failed:
                continue
            skipped.add(name)

    return passed, failed, skipped


def _parse_jest_log(log):
    passed = set()
    failed = set()
    skipped = set()
    current_suite = ""
    current_describe = ""
    suite_had_tests = {}

    for line in log.splitlines():
        clean = _ANSI_ESCAPE.sub("", line)
        stripped = clean.strip()
        if not stripped:
            continue

        m = _RE_JEST_PASS_SUITE.match(stripped)
        if m:
            current_suite = m.group(1)
            current_describe = ""
            passed.add(current_suite)
            continue

        m = _RE_JEST_FAIL_SUITE.match(stripped)
        if m:
            current_suite = m.group(1)
            current_describe = ""
            failed.add(current_suite)
            passed.discard(current_suite)
            continue

        m = _RE_JEST_PASS_TEST.match(stripped)
        if m:
            test_name = m.group(1).strip()
            if current_describe:
                full_name = f"{current_suite}:{current_describe}:{test_name}"
            else:
                full_name = f"{current_suite}:{test_name}"
            if full_name not in failed:
                passed.add(full_name)
            continue

        m = _RE_JEST_FAIL_TEST.match(stripped)
        if m:
            test_name = m.group(1).strip()
            if current_describe:
                full_name = f"{current_suite}:{current_describe}:{test_name}"
            else:
                full_name = f"{current_suite}:{test_name}"
            failed.add(full_name)
            passed.discard(full_name)
            continue

        m = _RE_JEST_SKIP_TEST.match(stripped)
        if m:
            test_name = m.group(1).strip()
            if current_describe:
                full_name = f"{current_suite}:{current_describe}:{test_name}"
            else:
                full_name = f"{current_suite}:{test_name}"
            if full_name not in failed and full_name not in passed:
                skipped.add(full_name)
            continue

        # Describe block header detection (2-space indent, no test marker)
        # Uses `clean` (not stripped) to preserve indentation for detection
        if current_suite and re.match(r"^  \S", clean) and not re.match(
            r"^  [✓✔✕×✗✘✖○]", clean
        ):
            current_describe = stripped

    return passed, failed, skipped


@Instance.register("umputun", "remark42")
class Remark42Instance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Remark42ImageDefault(self.pr, self._config)

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
        has_backend, has_frontend = _detect_test_scope(self._pr)

        all_passed = set()
        all_failed = set()
        all_skipped = set()

        if has_backend:
            go_passed, go_failed, go_skipped = _parse_go_log(test_log)
            all_passed.update(go_passed)
            all_failed.update(go_failed)
            all_skipped.update(go_skipped)

        if has_frontend:
            jest_passed, jest_failed, jest_skipped = _parse_jest_log(test_log)
            all_passed.update(jest_passed)
            all_failed.update(jest_failed)
            all_skipped.update(jest_skipped)

        return TestResult(
            passed_count=len(all_passed),
            failed_count=len(all_failed),
            skipped_count=len(all_skipped),
            passed_tests=all_passed,
            failed_tests=all_failed,
            skipped_tests=all_skipped,
        )
