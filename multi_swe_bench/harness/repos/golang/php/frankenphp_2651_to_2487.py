import json
import posixpath
import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "php"
_REPO = "frankenphp"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"frankenphp_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like frankenphp_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

PHP_IMAGE = "php:8.5.12-zts-trixie"
GO_VERSION = "1.27.4"
GO_SHA256_AMD64 = "7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840"
GO_SHA256_ARM64 = "3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92"
GO_BUILD_TAGS = "nobadger,nomysql,nopgx,nowatcher"

_NESTED_MODULES = ("caddy",)
_SOURCE_EXTS = (".go", ".c", ".h")
_SHELL_TEST_RE = re.compile(r"(?:^|/)[^/]+_test\.sh$")

_APT_PACKAGES = (
    "git ca-certificates curl build-essential pkg-config \\\n"
    "    libargon2-dev libbrotli-dev libcurl4-openssl-dev libonig-dev libreadline-dev \\\n"
    "    libsodium-dev libsqlite3-dev libssl-dev libxml2-dev zlib1g-dev"
)

_ENV_EXPORTS = """export CI=true
export CGO_ENABLED=1
export CGO_CFLAGS="-DFRANKENPHP_TEST $(php-config --includes)"
export CGO_LDFLAGS="-L/usr/local/lib $(php-config --ldflags) $(php-config --libs)"
"""

_TEST_CMD = "bash /home/run_tests.sh\n"

_APPLY_EXCLUDES = "--whitespace=nowarn --exclude='go.sum' --exclude='*/go.sum'"

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

_FILE_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ [^@]* @@ ?(.*)$")
_TEST_FUNC_RE = re.compile(r"^func ((?:Test|Fuzz|Example)\w*)\(")


def _patch_blocks(patch: str) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    for line in patch.split("\n"):
        header = _FILE_HEADER_RE.match(line)
        if header:
            blocks.append((header.group(2), []))
        elif blocks:
            blocks[-1][1].append(line)
    return blocks


def _func_name(text: str) -> str | None:
    match = _TEST_FUNC_RE.match(text)
    if match and match.group(1) != "TestMain":
        return match.group(1)
    return None


def _target_tests(patch: str) -> dict[str, list[tuple[str, str]]]:
    found: dict[tuple[str, str], None] = {}
    for path, body in _patch_blocks(patch):
        if not path.endswith("_test.go"):
            continue
        current: str | None = None
        in_hunk = False
        for line in body:
            hunk = _HUNK_HEADER_RE.match(line)
            if hunk:
                in_hunk = True
                current = _func_name(hunk.group(1))
                continue
            if not in_hunk or not line or line[0] not in " +-":
                continue
            text = line[1:]
            if text.startswith("func "):
                current = _func_name(text)
            if line[0] != " " and current:
                found[(path, current)] = None
            if text.rstrip() == "}":
                current = None
    targets: dict[str, list[tuple[str, str]]] = {}
    for path, name in found:
        targets.setdefault(posixpath.dirname(path) or ".", []).append((path, name))
    return targets


def _go_packages(patch: str) -> list[str]:
    dirs: dict[str, None] = {}
    for path, _ in _patch_blocks(patch):
        if not path.endswith(_SOURCE_EXTS):
            continue
        parts = path.split("/")[:-1]
        if any(p in ("testdata", "vendor", ".github") for p in parts):
            continue
        dirs["/".join(parts) or "."] = None
    return sorted(dirs)


def _shell_tests(patch: str) -> list[tuple[str, str]]:
    tests: dict[str, str] = {}
    for path, body in _patch_blocks(patch):
        if not _SHELL_TEST_RE.search(path):
            continue
        renamed = [line[len("rename from ") :] for line in body if line.startswith("rename from ")]
        if any(line.startswith(("@@", "Binary files")) for line in body):
            tests[path] = path
        elif renamed:
            tests[path] = renamed[0]
    return sorted(tests.items())


def _module_of(directory: str) -> tuple[str, str]:
    for module in _NESTED_MODULES:
        if directory == module:
            return module, "."
        if directory.startswith(module + "/"):
            return module, "./" + directory[len(module) + 1 :]
    if directory == ".":
        return ".", "."
    return ".", "./" + directory


class FrankenphpImageBase(Image):
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
        return PHP_IMAGE

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

ENV GO_VERSION={GO_VERSION} \\
    GOPATH=/root/go \\
    PATH=/usr/local/go/bin:/root/go/bin:$PATH \\
    GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    GOFLAGS="-buildvcs=false -tags={GO_BUILD_TAGS}"

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/* \\
    && ldconfig

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) go_sha256="{GO_SHA256_AMD64}" ;; \\
        arm64) go_sha256="{GO_SHA256_ARM64}" ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/go.tar.gz "https://go.dev/dl/go${{GO_VERSION}}.linux-${{arch}}.tar.gz"; \\
    echo "${{go_sha256}}  /tmp/go.tar.gz" | sha256sum -c -; \\
    rm -rf /usr/local/go; \\
    tar -C /usr/local -xzf /tmp/go.tar.gz; \\
    rm -f /tmp/go.tar.gz; \\
    go version; \\
    php -r 'exit(PHP_ZTS ? 0 : 1);'; \\
    php-config --version; \\
    test -e /usr/local/lib/libphp.so

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class FrankenphpImageDefault(Image):
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
        return FrankenphpImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _gate_lines(self) -> str:
        packages = _go_packages(self.pr.test_patch)
        shells = _shell_tests(self.pr.test_patch)
        if not packages and not shells:
            packages = ["."]
        lines = []
        for directory in packages:
            module, pkg = _module_of(directory)
            if module == ".":
                lines.append(f"go test -count=1 -run '^$' {pkg}")
            else:
                lines.append(f"(cd {module} && go test -count=1 -run '^$' {pkg})")
        if shells:
            lines.append("(cd caddy/frankenphp && go build -o frankenphp .)")
            lines.append("./caddy/frankenphp/frankenphp version")
        return "\n".join(lines) + "\n"

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

go version
php-config --version
timeout --kill-after=30 1800 go mod download || true
(cd caddy && timeout --kill-after=30 1800 go mod download) || true
git diff --quiet -- go.sum caddy/go.sum || git checkout -- go.sum caddy/go.sum

cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

test -s /home/run_tests.sh
{self._gate_lines()}echo DEPS_OK
"""

    def _run_tests_sh(self) -> str:
        repo = self.pr.repo
        parts = [
            "#!/bin/bash",
            "set -uo pipefail",
            "",
            _ENV_EXPORTS.rstrip("\n"),
            "",
            f"cd /home/{repo}",
            "",
            "status=0",
        ]
        for directory in _go_packages(self.pr.test_patch):
            module, pkg = _module_of(directory)
            glob = "*_test.go" if directory == "." else f"{directory}/*_test.go"
            parts += [
                "",
                f'if [ -d "{directory}" ] && ls {directory}/*.go >/dev/null 2>&1; then',
                f'  echo "MSWE_PKG {directory}"',
                f"  for f in {glob}; do",
                '    [ -f "$f" ] || continue',
                "    sed -n -E 's/^func ((Test|Fuzz|Example)[A-Za-z0-9_]*)\\(.*/\\1/p' \"$f\" | while read -r fn; do",
                '      echo "MSWE_FUNC $f $fn"',
                "    done",
                "  done",
                f'  (cd {module} && go test -json -count=1 -p 1 -timeout 30m {pkg}) 2>&1 || status=1',
                f'  echo "MSWE_PKG_END {directory}"',
                "else",
                f'  echo "### MSWE skip package {directory} (not present at this commit)"',
                "fi",
            ]
        shells = _shell_tests(self.pr.test_patch)
        if shells:
            listed = " ".join(f"{name}:{source}" for name, source in shells)
            parts += [
                "",
                "present=0",
                f"for entry in {listed}; do",
                '  [ -f "${entry%%:*}" ] || [ -f "${entry#*:}" ] && present=1',
                "done",
                "built=0",
                'if [ "$present" = 1 ] && (cd caddy/frankenphp && go build -o frankenphp .) 2>&1; then',
                "  built=1",
                "fi",
                "served=0",
                'if [ "$built" = 1 ] && (cd testdata && ../caddy/frankenphp/frankenphp start) 2>&1; then',
                "  served=1",
                "fi",
                f"for entry in {listed}; do",
                '  name="${entry%%:*}"',
                '  f="$name"',
                '  [ -f "$f" ] || f="${entry#*:}"',
                '  if [ ! -f "$f" ]; then',
                '    echo "### MSWE skip $name (not present at this commit)"',
                "    continue",
                "  fi",
                '  echo "### MSWE run $name ($f)"',
                '  if [ "$built" = 1 ] && timeout --kill-after=30 900 bash "$f" 2>&1; then',
                '    echo "MSWE_FILE PASS $name"',
                "  else",
                '    echo "MSWE_FILE FAIL $name"',
                "    status=1",
                "  fi",
                "done",
                'if [ "$served" = 1 ]; then',
                "  (cd testdata && ../caddy/frankenphp/frankenphp stop) 2>&1 || true",
                "fi",
            ]
        parts += ["", "exit $status", ""]
        return "\n".join(parts)

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
git apply {_APPLY_EXCLUDES} /home/test.patch
{_TEST_CMD}"""

    def _fix_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply {_APPLY_EXCLUDES} /home/test.patch /home/fix.patch
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
_PKG_RE = re.compile(r"^MSWE_PKG (\S+)$")
_PKG_END_RE = re.compile(r"^MSWE_PKG_END (\S+)$")
_FUNC_RE = re.compile(r"^MSWE_FUNC (\S+) (\S+)$")
_FILE_RESULT_RE = re.compile(r"^MSWE_FILE (PASS|FAIL) (\S+)$")
_TEXT_BUILD_FAIL_RE = re.compile(r"^FAIL\s+\S+\s+\[(?:build|setup) failed\]")
_VOLATILE_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{6,}")


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class FRANKENPHP_2651_TO_2487(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config
        self._targets = _target_tests(pr.test_patch)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return FrankenphpImageDefault(self.pr, self._config)

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

        block: dict | None = None

        def name_of(state: dict, test: str, package: str) -> str:
            test = _VOLATILE_ADDR_RE.sub("0x<addr>", test)
            path = state["funcs"].get(test.split("/", 1)[0])
            if path:
                return f"{path} > {test}"
            return f"{package or state['dir']} > {test}"

        def finish(state: dict | None) -> None:
            if state is None:
                return
            targets = {func for _, func in self._targets.get(state["dir"], [])}
            for test, package in state["running"].items():
                top = test.split("/", 1)[0]
                crashed = any(p == test or p.startswith(test + "/") for p in state["panicked"])
                if top in targets or crashed:
                    failed_tests.add(name_of(state, test, package))
            if state["build_failed"] or (state["pkg_failed"] and not state["tests_seen"]):
                for path, func in self._targets.get(state["dir"], []):
                    failed_tests.add(f"{path} > {func}")

        for raw in clean.split("\n"):
            line = raw.strip()
            if not line:
                continue

            m = _PKG_RE.match(line)
            if m:
                finish(block)
                block = {
                    "dir": m.group(1),
                    "funcs": {},
                    "running": {},
                    "panicked": set(),
                    "tests_seen": False,
                    "pkg_failed": False,
                    "build_failed": False,
                }
                continue

            m = _PKG_END_RE.match(line)
            if m:
                finish(block)
                block = None
                continue

            m = _FILE_RESULT_RE.match(line)
            if m:
                name = f"{m.group(2)} > (script exits 0)"
                (passed_tests if m.group(1) == "PASS" else failed_tests).add(name)
                continue

            if block is None:
                continue

            m = _FUNC_RE.match(line)
            if m:
                path = m.group(1)
                if path.startswith("./"):
                    path = path[2:]
                block["funcs"].setdefault(m.group(2), path)
                continue

            if _TEXT_BUILD_FAIL_RE.match(line):
                block["build_failed"] = True
                block["pkg_failed"] = True
                continue

            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            action = event.get("Action")
            test = event.get("Test")
            package = event.get("Package") or ""
            if action == "build-fail":
                block["build_failed"] = True
                block["pkg_failed"] = True
                continue
            if not test:
                if action == "fail":
                    block["pkg_failed"] = True
                    if event.get("FailedBuild"):
                        block["build_failed"] = True
                continue
            if action == "output":
                if str(event.get("Output", "")).startswith(("panic:", "fatal error:")):
                    block["panicked"].add(test)
                continue
            if action == "run":
                block["running"][test] = package
                block["tests_seen"] = True
                continue
            if action not in ("pass", "fail", "skip"):
                continue
            block["running"].pop(test, None)
            block["tests_seen"] = True
            name = name_of(block, test, package)
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        finish(block)

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
