import re
from typing import Optional

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_KEY = "battlemath_142_to_142"
_BASE_TAG = "base"
_NODE_IMAGE = "node:22-bookworm"

_PROXY_ARGS = "\n".join(
    [
        'ARG http_proxy=""',
        'ARG https_proxy=""',
        'ARG HTTP_PROXY=""',
        'ARG HTTPS_PROXY=""',
        'ARG no_proxy="localhost,127.0.0.1,::1"',
        'ARG NO_PROXY="localhost,127.0.0.1,::1"',
        'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"',
    ]
)

_ENV_BLOCK = "\n".join(
    [
        "ENV DEBIAN_FRONTEND=noninteractive \\",
        "    LANG=C.UTF-8 \\",
        "    LC_ALL=C.UTF-8 \\",
        "    TZ=UTC \\",
        "    CI=true \\",
        "    HUSKY=0 \\",
        "    CYPRESS_INSTALL_BINARY=0 \\",
        "    NODE_OPTIONS=--max-old-space-size=4096 \\",
        "    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\",
        "    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\",
        "    npm_config_yes=true \\",
        "    npm_config_fund=false \\",
        "    npm_config_audit=false \\",
        "    http_proxy=${http_proxy} \\",
        "    https_proxy=${https_proxy} \\",
        "    HTTP_PROXY=${HTTP_PROXY} \\",
        "    HTTPS_PROXY=${HTTPS_PROXY} \\",
        "    no_proxy=${no_proxy} \\",
        "    NO_PROXY=${NO_PROXY} \\",
        "    SSL_CERT_FILE=${CA_CERT_PATH} \\",
        "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\",
        "    CURL_CA_BUNDLE=${CA_CERT_PATH} \\",
        "    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}",
    ]
)

_CERT_FARM = "\n".join(
    [
        "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt",
    ]
)

_APT_BLOCK = "\n".join(
    [
        "RUN apt-get update && apt-get install -y --no-install-recommends \\",
        "    ca-certificates curl git \\",
        "    && rm -rf /var/lib/apt/lists/*",
    ]
)


def _strip_binary_sections(patch_content: str) -> str:
    if not patch_content:
        return patch_content

    lines = patch_content.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git"):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith("diff --git"):
                if lines[i].startswith("GIT binary patch") or lines[i].startswith(
                    "Binary files"
                ):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1

    cleaned = "\n".join(result)
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned


def _prune_block(repo: str, sha: str) -> str:
    return "\n".join(
        [
            "RUN set -eux; \\",
            "    cd /home/" + repo + "; \\",
            '    test "$(git rev-parse HEAD)" = "' + sha + '"; \\',
            "    git remote remove origin 2>/dev/null || true; \\",
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\",
            "        | xargs -r -n1 git update-ref -d; \\",
            "    git reflog expire --expire=now --all; \\",
            "    git reflog expire --expire-unreachable=now --all; \\",
            "    git gc --prune=now --aggressive; \\",
            "    git repack -a -d -l --quiet; \\",
            "    rm -f .git/objects/info/alternates; \\",
            "    git config --local gc.auto 0; \\",
            "    git config --local fetch.recurseSubmodules false; \\",
            '    git config --local remote.pushDefault ""; \\',
            '    test "$(git rev-parse HEAD)" = "' + sha + '"; \\',
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\',
            '    test -z "$(git remote)"; \\',
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"',
        ]
    )


def _submodule_block(repo: str) -> str:
    return "\n".join(
        [
            "RUN if [ -f /home/" + repo + "/.gitmodules ]; then \\",
            "        cd /home/" + repo + " && git submodule foreach --recursive ' \\",
            "            git checkout --detach HEAD; \\",
            "            git remote remove origin 2>/dev/null || true; \\",
            '            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\',
            "                | xargs -r -n1 git update-ref -d; \\",
            "            git reflog expire --expire=now --all; \\",
            "            git reflog expire --expire-unreachable=now --all; \\",
            "            git gc --prune=now --aggressive; \\",
            "            rm -f .git/objects/info/alternates; \\",
            "        '; \\",
            "    fi",
        ]
    )


_INSTALL_DEPS_JS = r'''const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const ENV_FILE = '/home/js_env.sh';

function run(command, args, cwd) {
  process.stdout.write('RUN ' + command + ' ' + args.join(' ') + '\n');
  const result = spawnSync(command, args, { cwd, stdio: 'inherit', shell: false });
  return result.status === 0;
}

function has(repoDir, name) {
  return fs.existsSync(path.join(repoDir, name));
}

function readPackage(repoDir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(repoDir, 'package.json'), 'utf8'));
  } catch (error) {
    return {};
  }
}

function detectManager(repoDir, pkg) {
  const declared = typeof pkg.packageManager === 'string' ? pkg.packageManager : '';
  if (declared.startsWith('pnpm')) return 'pnpm';
  if (declared.startsWith('yarn')) return 'yarn';
  if (declared.startsWith('npm')) return 'npm';
  if (has(repoDir, 'pnpm-lock.yaml')) return 'pnpm';
  if (has(repoDir, 'bun.lockb') || has(repoDir, 'bun.lock')) return 'bun';
  if (has(repoDir, 'yarn.lock')) return 'yarn';
  return 'npm';
}

function enableCorepack(repoDir, pkg) {
  if (typeof pkg.packageManager !== 'string' || !pkg.packageManager) return;
  run('corepack', ['enable'], repoDir);
  run('corepack', ['prepare', pkg.packageManager, '--activate'], repoDir);
}

function install(manager, repoDir) {
  const attempts = {
    pnpm: [['install', '--frozen-lockfile'], ['install', '--no-frozen-lockfile'], ['install']],
    yarn: [['install', '--frozen-lockfile'], ['install', '--immutable'], ['install']],
    npm: [['ci'], ['install']],
    bun: [['install', '--frozen-lockfile'], ['install']],
  }[manager];
  for (const args of attempts) {
    if (run(manager, args, repoDir)) return true;
  }
  return false;
}

function testSeparator(manager) {
  return manager === 'npm' ? '--' : '';
}

function testArgs(pkg) {
  const script = (pkg.scripts && pkg.scripts.test) || '';
  if (/vitest/.test(script)) return 'run --reporter=verbose';
  if (/react-scripts\s+test|jest|craco\s+test/.test(script)) {
    return '--watchAll=false --verbose';
  }
  return '';
}

function main() {
  const repoDir = process.argv[2];
  const pkg = readPackage(repoDir);
  enableCorepack(repoDir, pkg);
  const manager = detectManager(repoDir, pkg);
  const ok = install(manager, repoDir);
  const lines = [
    'export PKG_MANAGER=' + manager,
    'export TEST_SEP="' + testSeparator(manager) + '"',
    'export TEST_ARGS="' + testArgs(pkg) + '"',
    'export PATH=' + path.join(repoDir, 'node_modules', '.bin') + ':$PATH',
  ];
  fs.writeFileSync(ENV_FILE, lines.join('\n') + '\n');
  process.stdout.write('JS_ENV\n' + lines.join('\n') + '\n');
  process.stdout.write('INSTALL_OK ' + String(ok) + '\n');
}

main();
'''

_DEPS_GATE_JS = r'''const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

function fail(message) {
  process.stderr.write('deps_gate: ' + message + '\n');
  process.exit(1);
}

function addedBareImports(patchPath) {
  if (!fs.existsSync(patchPath)) return [];
  const text = fs.readFileSync(patchPath, 'utf8');
  const modules = [];
  const patterns = [
    /^\+\s*import\s+[^'"]*from\s+['"]([^'"]+)['"]/,
    /^\+\s*import\s+['"]([^'"]+)['"]/,
    /^\+\s*(?:const|let|var)\s+.*require\(\s*['"]([^'"]+)['"]\s*\)/,
  ];
  for (const line of text.split('\n')) {
    if (line.startsWith('+++')) continue;
    for (const pattern of patterns) {
      const match = line.match(pattern);
      if (!match) continue;
      const name = match[1];
      if (name.startsWith('.') || name.startsWith('/')) break;
      if (!modules.includes(name)) modules.push(name);
      break;
    }
  }
  return modules;
}

function main() {
  const repoDir = process.argv[2];
  const fixPatch = process.argv[3];

  const pkgPath = path.join(repoDir, 'package.json');
  if (!fs.existsSync(pkgPath)) fail('package.json not found');
  const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));

  const modulesDir = path.join(repoDir, 'node_modules');
  if (!fs.existsSync(modulesDir) || fs.readdirSync(modulesDir).length === 0) {
    fail('node_modules is missing or empty');
  }

  const script = (pkg.scripts && pkg.scripts.test) || '';
  if (!script) fail('package.json declares no test script');
  const binary = script.trim().split(/\s+/)[0];
  const binaryPath = path.join(modulesDir, '.bin', binary);
  if (binary !== 'node' && !fs.existsSync(binaryPath)) {
    fail('test runner binary not installed: ' + binary);
  }

  const missing = [];
  for (const name of addedBareImports(fixPatch)) {
    const probe = spawnSync(
      process.execPath,
      ['-e', 'require.resolve(' + JSON.stringify(name) + ')'],
      { cwd: repoDir },
    );
    if (probe.status !== 0) missing.push(name);
  }
  if (missing.length) fail('imports added by the fix patch do not resolve: ' + missing.join(', '));

  process.stdout.write('NODE ' + process.version + '\n');
  process.stdout.write('TEST_SCRIPT ' + script + '\n');
  process.stdout.write('DEPS_OK\n');
}

main();
'''


class Battlemath142To142ImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        source = "https://github.com/" + org + "/" + repo

        if self.config.need_clone:
            acquire = "\n".join(
                [
                    'RUN git clone "${REPO_URL}" /home/' + repo + " && \\",
                    "    cd /home/" + repo + " && git rev-parse HEAD >/dev/null",
                ]
            )
        else:
            acquire = "COPY " + repo + " /home/" + repo

        lines = [
            "# syntax=docker/dockerfile:1.6",
            "",
            "FROM " + _NODE_IMAGE,
            "",
            "ARG TARGETARCH",
            'ARG REPO_URL="' + source + '.git"',
            "ARG BASE_COMMIT",
            "",
            _PROXY_ARGS,
            "",
            _ENV_BLOCK,
            "",
            self.global_env,
            "",
            'LABEL org.opencontainers.image.title="' + org + "/" + repo + '" \\',
            '      org.opencontainers.image.description="'
            + org
            + "/"
            + repo
            + ' Docker image" \\',
            '      org.opencontainers.image.source="' + source + '" \\',
            '      org.opencontainers.image.authors="https://www.ethara.ai/"',
            "",
            _CERT_FARM,
            "",
            _APT_BLOCK,
            "",
            "RUN git config --global --add safe.directory '*'",
            "",
            "WORKDIR /home/",
            "",
            acquire,
            "",
            self.clear_env,
            "",
            'CMD ["/bin/bash"]',
            "",
        ]

        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


class Battlemath142To142ImageDefault(Image):
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
        return Battlemath142To142ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _repo_dir(self) -> str:
        return "/home/" + _safe_path_component(self.pr.repo)

    def _test_command(self) -> list[str]:
        repo_dir = self._repo_dir()
        return [
            "export CI=true",
            "export HUSKY=0",
            ". /home/js_env.sh",
            "cd " + repo_dir,
            '"$PKG_MANAGER" run test $TEST_SEP $TEST_ARGS',
            "",
        ]

    def files(self) -> list[File]:
        repo_dir = self._repo_dir()
        sha = self.pr.base.sha

        check_git_changes = "\n".join(
            [
                "#!/bin/bash",
                "set -e",
                "",
                "cd " + repo_dir,
                "",
                "if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then",
                '    echo "check_git_changes: not a git repository" >&2',
                "    exit 1",
                "fi",
                "",
                'if [ -n "$(git status --porcelain)" ]; then',
                '    echo "check_git_changes: working tree is dirty" >&2',
                "    git status --porcelain >&2",
                "    exit 1",
                "fi",
                "",
                'echo "check_git_changes: clean"',
                "",
            ]
        )

        prepare = "\n".join(
            [
                "#!/bin/bash",
                "set -e",
                "",
                "cd " + repo_dir,
                "git reset --hard",
                "git clean -fdx",
                "bash /home/check_git_changes.sh",
                'if ! git cat-file -e "' + sha + '^{commit}" 2>/dev/null; then',
                '    git fetch --no-tags origin "' + sha + '"',
                "fi",
                'git checkout --detach "' + sha + '"',
                "bash /home/check_git_changes.sh",
                "",
                "export CI=true",
                "export HUSKY=0",
                "export CYPRESS_INSTALL_BINARY=0",
                "",
                "node /home/install_deps.js " + repo_dir + " || true",
                "",
                ". /home/js_env.sh",
                'cd ' + repo_dir,
                '"$PKG_MANAGER" run test $TEST_SEP $TEST_ARGS || true',
                "",
                "node /home/deps_gate.js " + repo_dir + " /home/fix.patch",
                "",
            ]
        )

        run_sh = "\n".join(
            ["#!/bin/bash", "set -eo pipefail", ""] + self._test_command()
        )

        test_run_sh = "\n".join(
            [
                "#!/bin/bash",
                "set -eo pipefail",
                "",
                "cd " + repo_dir,
                "git apply --whitespace=nowarn /home/test.patch",
                "",
            ]
            + self._test_command()
        )

        fix_run_sh = "\n".join(
            [
                "#!/bin/bash",
                "set -eo pipefail",
                "",
                "cd " + repo_dir,
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                "",
            ]
            + self._test_command()
        )

        js_env = "\n".join(
            [
                "export PKG_MANAGER=yarn",
                'export TEST_SEP=""',
                'export TEST_ARGS=""',
                "export PATH=" + repo_dir + "/node_modules/.bin:$PATH",
                "",
            ]
        )

        return [
            File(".", "fix.patch", _strip_binary_sections(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_sections(self.pr.test_patch)),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "js_env.sh", js_env),
            File(".", "install_deps.js", _INSTALL_DEPS_JS),
            File(".", "deps_gate.js", _DEPS_GATE_JS),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)
        sha = self.pr.base.sha

        copy_commands = "\n".join(
            "COPY " + file.name + " /home/" for file in self.files()
        )

        lines = [
            "FROM " + name + ":" + tag,
            "",
            self.global_env,
            "",
            copy_commands,
            "",
            "WORKDIR /home/" + repo,
            "",
            "RUN git reset --hard",
            "RUN git checkout " + sha,
            "",
            "RUN bash /home/prepare.sh",
            "",
            _prune_block(repo, sha),
            "",
            _submodule_block(repo),
            "",
            self.clear_env,
            "",
        ]

        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


_PASS_SYMBOLS = "✓√"
_FAIL_SYMBOLS = "✕✗×"
_SKIP_SYMBOLS = "○✎↓"

_SUITE_HEADER = re.compile(r"^(PASS|FAIL)\s+(\S+\.[cm]?[jt]sx?)(?:\s|$)")
_STATUS_LINE = re.compile(
    "^(\\s*)([" + _PASS_SYMBOLS + _FAIL_SYMBOLS + _SKIP_SYMBOLS + "])\\s+(.*\\S)\\s*$"
)
_TITLE_LINE = re.compile(r"^(\s+)(\S.*?)\s*$")
_DURATION = re.compile(r"\s*\(\s*\d+(?:\.\d+)?\s*m?s\s*\)\s*$")


def _parse_jest_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log or "")

    current_suite: Optional[str] = None
    stack: list[tuple[int, str]] = []

    def record(bucket: str, name: str) -> None:
        if not name:
            return
        if bucket == "failed":
            failed_tests.add(name)
        elif bucket == "skipped":
            skipped_tests.add(name)
        else:
            passed_tests.add(name)

    for raw_line in clean_log.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        header = _SUITE_HEADER.match(line.strip())
        if header:
            current_suite = header.group(2)
            stack = []
            record("passed" if header.group(1) == "PASS" else "failed", current_suite)
            continue

        if current_suite is None:
            continue

        if not line.startswith(" "):
            current_suite = None
            stack = []
            continue

        status = _STATUS_LINE.match(line)
        if status:
            indent = len(status.group(1))
            symbol = status.group(2)
            title = _DURATION.sub("", status.group(3)).strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parts = [entry[1] for entry in stack] + [title]
            name = current_suite + "::" + " > ".join(parts)
            if symbol in _PASS_SYMBOLS:
                record("passed", name)
            elif symbol in _FAIL_SYMBOLS:
                record("failed", name)
            else:
                record("skipped", name)
            continue

        title_match = _TITLE_LINE.match(line)
        if not title_match:
            current_suite = None
            stack = []
            continue

        indent = len(title_match.group(1))
        title = title_match.group(2).strip()
        if title.startswith("●") or title.startswith("console."):
            current_suite = None
            stack = []
            continue

        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, title))

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


class BATTLEMATH_142_TO_142(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Battlemath142To142ImageDefault(self.pr, self._config)

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
        return _parse_jest_log(log)


for _org, _name in (("JesseRWeigel", _ERA_KEY), ("JesseRWeigel", "battlemath")):
    Instance.register(_org, _name)(BATTLEMATH_142_TO_142)
