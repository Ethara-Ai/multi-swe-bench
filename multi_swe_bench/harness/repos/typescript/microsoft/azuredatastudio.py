from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_LOG_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]")

_STACK_OR_SOURCE = re.compile(r"^at\s|file://|\.js:\d+")


class AzureDataStudioImageBase(Image):

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
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "libx11-dev",
            "libxkbfile-dev",
            "libsecret-1-dev",
            "libkrb5-dev",
            "pkg-config",
            "xvfb",
            "xauth",
            "libxtst6",
            "dbus-x11",
            "libgtk-3-0",
            "libgbm1",
            "libnss3",
            "libxss1",
            "libasound2",
            "libatk-bridge2.0-0",
            "libatk1.0-0",
            "libcups2",
            "libdrm2",
            "libxcomposite1",
            "libxdamage1",
            "libxrandr2",
            "libpango-1.0-0",
            "libcairo2",
            "libx11-xcb1",
            "fonts-liberation",
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        default_packages = [
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
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, image_name)

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/

{apt_command}

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}
"""


class AzureDataStudioImageDefault(Image):

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
        return AzureDataStudioImageBase(self.pr, self._config)

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        sections = [f"FROM {base.image_full_name()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(copy_commands.rstrip("\n"))
        sections.append("RUN bash /home/prepare.sh")
        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        launch_cmd = """\
export CI=true
export ADS_TEST_GREP=@UNSTABLE@
export ADS_TEST_INVERT_GREP=1

export VSCODE_SKIP_PRELAUNCH=1
UDIR=$(mktemp -d)
EDIR=$(mktemp -d)
xvfb-run -a --server-args="-screen 0 1280x1024x24" \\
    ./scripts/code.sh --no-sandbox \\
    --extensionDevelopmentPath="$PWD/extensions/schema-compare" \\
    --extensionTestsPath="$PWD/extensions/schema-compare/out/test" \\
    --user-data-dir="$UDIR" \\
    --extensions-dir="$EDIR" \\
    --disable-telemetry --disable-crash-reporter --disable-updates \\
    --disable-dev-shm-usage --nogpu 2>&1
rm -rf "$UDIR" "$EDIR"
"""

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
                """#!/bin/bash
set -e

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

DID_FETCH=0
if ! git cat-file -e {base_sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {base_sha}
    DID_FETCH=1
fi
git checkout {base_sha}

if [ "$DID_FETCH" = "1" ]; then
    echo "base commit was fetched at PR-build time; restoring scrub invariants"
    git checkout --detach {base_sha}
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d
    rm -f .git/FETCH_HEAD
    git reflog expire --expire=now --all
    git reflog expire --expire-unreachable=now --all
    git gc --prune=now --aggressive
    test "$(git rev-parse HEAD)" = "$(git rev-parse {base_sha})"
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
    test -z "$(git remote)"
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
fi

bash /home/check_git_changes.sh

git config --global url."https://github.com/".insteadOf "git://github.com/"
git config --global --add url."https://github.com/".insteadOf "git@github.com:"
git config --global --add url."https://github.com/".insteadOf "ssh://git@github.com/"

sed -i 's|"gulp-atom-electron": "\\^1\\.22\\.0"|"gulp-atom-electron": "^1.23.0"|' package.json
if ! grep -q '"gulp-atom-electron": "\\^1\\.23\\.0"' package.json; then
    echo "FATAL: gulp-atom-electron pin was not rewritten; the dead"
    echo "       github-releases-ms dependency would 404 during install."
    exit 1
fi

yarn --ignore-scripts --ignore-engines --network-timeout 600000

(cd extensions && yarn --ignore-scripts --ignore-engines --network-timeout 600000)

(cd extensions/schema-compare && yarn --ignore-scripts --ignore-engines --network-timeout 600000)

(cd build && yarn --ignore-scripts --ignore-engines --network-timeout 600000)

(cd build/lib/watch && yarn --ignore-scripts --ignore-engines --network-timeout 600000)

ln -sf /usr/bin/python3 /usr/local/bin/python
python --version

command -v xauth

./node_modules/.bin/electron-rebuild --version 9.4.3 --force --module-dir .

node --max-old-space-size=4096 ./node_modules/gulp/bin/gulp.js compile-client

node build/lib/electron || true

if [ ! -f .build/electron/version ]; then
    echo "gulp-atom-electron path staged nothing; downloading Electron 9.4.3 directly"
    APPNAME=$(node -p "require('./product.json').applicationName")
    rm -rf .build/electron && mkdir -p .build/electron
    case "$(uname -m)" in
        x86_64)  EARCH=linux-x64 ;;
        aarch64|arm64) EARCH=linux-arm64 ;;
        armv7l)  EARCH=linux-armv7l ;;
        *) echo "FATAL: no Electron 9.4.3 build for arch $(uname -m)"; exit 1 ;;
    esac
    echo "staging Electron 9.4.3 for $EARCH"
    curl -fsSL --retry 5 --retry-delay 5 --retry-connrefused \\
        --connect-timeout 30 --max-time 900 -o /tmp/electron.zip \\
        "https://github.com/electron/electron/releases/download/v9.4.3/electron-v9.4.3-$EARCH.zip"
    python3 -m zipfile -e /tmp/electron.zip .build/electron
    (cd .build/electron && mv electron "$APPNAME" && chmod +x "$APPNAME")
    printf '9.4.3' > .build/electron/version
    rm -f /tmp/electron.zip
fi
if [ ! -f .build/electron/version ]; then
    echo "FATAL: no Electron staged in .build/electron"
    exit 1
fi

ELECTRON_BIN=".build/electron/$(node -p "require('./product.json').applicationName")"
ELF_MACHINE=$(od -An -tx1 -j18 -N2 "$ELECTRON_BIN" | tr -d ' \\n')
case "$(uname -m)" in
    x86_64)        WANT=3e00 ;;
    aarch64|arm64) WANT=b700 ;;
    armv7l)        WANT=2800 ;;
    *)             WANT="$ELF_MACHINE" ;;
esac
if [ "$ELF_MACHINE" != "$WANT" ]; then
    echo "FATAL: staged Electron is the wrong architecture."
    echo "       host $(uname -m) expects ELF machine $WANT, binary has $ELF_MACHINE"
    exit 1
fi
echo "Electron binary arch OK for $(uname -m) (ELF machine $ELF_MACHINE)"

node build/lib/builtInExtensions.js || echo "WARN: built-in extension sync failed; not required for these tests"

node --max-old-space-size=4096 ./node_modules/gulp/bin/gulp.js compile-extension:schema-compare
""".format(
                    org=self.pr.org,
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh
""".format(repo=self.pr.repo)
                + launch_cmd,
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
if ! git apply --whitespace=nowarn --3way /home/test.patch; then
    git reset --hard
    git apply --whitespace=nowarn --reject /home/test.patch
fi

node --max-old-space-size=4096 ./node_modules/gulp/bin/gulp.js compile-extension:schema-compare || true

if [ ! -f extensions/schema-compare/out/test/testSchemaCompareDialog.js ]; then
    echo "gulp pipeline aborted on the expected type errors; emitting with tsc"
    ./node_modules/.bin/tsc -p extensions/schema-compare/tsconfig.json || true
fi

if [ ! -f extensions/schema-compare/out/test/testSchemaCompareDialog.js ]; then
    echo "FATAL: test.patch applied but its new test file was never emitted to"
    echo "       out/test, by gulp or by tsc. Refusing to report a misleading"
    echo "       zero-new-test run."
    exit 1
fi
""".format(repo=self.pr.repo)
                + launch_cmd,
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
if git apply --whitespace=nowarn --check /home/test.patch /home/fix.patch 2>/dev/null; then
    git apply --whitespace=nowarn /home/test.patch /home/fix.patch
else
    if ! git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch; then
        git reset --hard
        git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch
    fi
fi

node --max-old-space-size=4096 ./node_modules/gulp/bin/gulp.js compile-extension:schema-compare
""".format(repo=self.pr.repo)
                + launch_cmd,
            ),
        ]


@Instance.register("microsoft", "azuredatastudio")
class AzureDataStudio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AzureDataStudioImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

        summary_re = re.compile(r"^\d+\s+(passing|failing|pending)\b")
        tag_re = re.compile(r"\s*@[\w.-]+@")
        duration = r"(?:\s+\([\d.]+\s*\w+\))?"

        re_pass = re.compile(r"^[✔✓]\s+(.*?)" + duration + r"$")
        re_hook_fail = re.compile(r'^\d+\)\s+".*?"\s+hook(?:\s+for\s+"(.*?)")?\s*:?$')
        re_fail = re.compile(r"^\d+\)\s+(.*?)" + duration + r"$")
        re_skip = re.compile(r"^-\s+(\S.*?)" + duration + r"$")

        summary_seen = False
        suite_stack: list[tuple[int, str]] = []

        def qualify(leaf: str) -> str:
            return " > ".join([name for _, name in suite_stack] + [leaf])

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if summary_re.match(stripped):
                summary_seen = True
                continue
            if summary_seen:
                continue

            indent = len(line) - len(line.lstrip())

            m_pass = re_pass.match(stripped) if indent >= 2 else None
            m_hook = re_hook_fail.match(stripped) if indent >= 2 else None
            m_fail = re_fail.match(stripped) if (indent >= 2 and not m_hook) else None
            m_skip = re_skip.match(stripped) if indent >= 2 else None

            if m_pass or m_hook or m_fail or m_skip:
                while suite_stack and suite_stack[-1][0] >= indent:
                    suite_stack.pop()

            if m_pass:
                name = qualify(m_pass.group(1).strip())
                if name not in failed_tests and name not in skipped_tests:
                    passed_tests.add(name)
                continue

            if m_hook:
                inner = (m_hook.group(1) or "").strip()
                name = qualify(inner) if inner else stripped
                failed_tests.add(name)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                continue

            if m_fail:
                name = qualify(m_fail.group(1).strip())
                failed_tests.add(name)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                continue

            if m_skip:
                name = qualify(m_skip.group(1).strip())
                if name not in failed_tests:
                    skipped_tests.add(name)
                    passed_tests.discard(name)
                continue

            if (
                indent >= 2
                and indent % 2 == 0
                and not stripped.startswith("[")
                and not _LOG_TIMESTAMP.match(stripped)
                and not _STACK_OR_SOURCE.search(stripped)
            ):
                while suite_stack and suite_stack[-1][0] >= indent:
                    suite_stack.pop()
                suite_stack.append((indent, tag_re.sub("", stripped).strip()))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
