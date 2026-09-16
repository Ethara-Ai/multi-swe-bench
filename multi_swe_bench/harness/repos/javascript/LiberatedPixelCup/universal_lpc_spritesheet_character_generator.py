import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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


_EXTRA_PACKAGES = [
    "chromium",
    "firefox-esr",
    "xvfb",
    "dbus",
    "dbus-x11",
    "fonts-liberation",
    "fonts-dejavu-core",
]

_VENDORED_ASSETS = [
    ("https://cdnjs.cloudflare.com/ajax/libs/mocha/11.7.2/mocha.css", "mocha.css"),
    (
        "https://cdnjs.cloudflare.com/ajax/libs/mocha/11.7.2/mocha.min.js",
        "mocha.min.js",
    ),
    ("https://cdn.jsdelivr.net/npm/bulma@0.9.4/css/bulma.min.css", "bulma.min.css"),
    ("https://unpkg.com/mithril@2.2.2/mithril.min.js", "mithril.min.js"),
    ("https://unpkg.com/classnames@^2.5.1", "classnames.js"),
    ("https://cdn.jsdelivr.net/npm/chai@6.2.2+esm", "chai.js"),
    ("https://cdn.jsdelivr.net/npm/sinon@21.0.1/+esm", "sinon.js"),
]


def _apply(patches: str) -> str:
    return f"""if ! git apply --whitespace=nowarn {patches} \\
   && ! git apply --whitespace=nowarn --3way {patches}; then
    echo "patch application failed: {patches}" >&2
    exit 1
fi"""


def _vendor_download() -> str:
    lines = ["mkdir -p vendor"]
    for url, name in _VENDORED_ASSETS:
        lines.append(f'curl -fsSL --retry 3 -o "vendor/{name}" "{url}"')
    return "\n".join(lines)


def _vendor_rewrite() -> str:
    seds = " \\\n".join(
        f"    -e 's#{url}#/vendor/{name}#g'" for url, name in _VENDORED_ASSETS
    )
    return f"""sed -i \\
{seds} \\
    tests_run.html

if grep -qE 'https?://' tests_run.html; then
    echo "vendor-rewrite: unvendored remote asset still referenced:" >&2
    grep -nE 'https?://' tests_run.html >&2
    exit 1
fi"""


class LPCImageBase(Image):

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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return _EXTRA_PACKAGES

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""\
FROM {base_img}

{self.global_env}


WORKDIR /home/

ENV LC_ALL=C.UTF-8

{apt_command}

RUN ln -sf /usr/bin/chromium /usr/local/bin/google-chrome \\
    && ln -sf /usr/bin/firefox-esr /usr/local/bin/firefox

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}

CMD ["/bin/bash"]
"""


class LPCImageDefault(Image):

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
        return LPCImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _make_run_script(self, patches: str) -> str:
        return """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{patches}
bash /home/vendor-rewrite.sh

export CI=true
export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1920x1080x24 &
for _ in $(seq 1 30); do
    if [ -e /tmp/.X11-unix/X99 ]; then
        break
    fi
    sleep 1
done

export XDG_RUNTIME_DIR=/tmp/xdg-runtime
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

timeout -k 60 1800 dbus-run-session -- npx testem ci --reporter tap 2>&1
""".format(
            repo=self.pr.repo,
            patches=patches,
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

""",
            ),
            File(
                ".",
                "vendor-rewrite.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{rewrite}
""".format(
                    repo=self.pr.repo,
                    rewrite=_vendor_rewrite(),
                ),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

npm install || true

{vendor}
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    vendor=_vendor_download(),
                ),
            ),
            File(".", "run.sh", self._make_run_script("")),
            File(".", "test-run.sh", self._make_run_script(_apply("/home/test.patch"))),
            File(
                ".",
                "fix-run.sh",
                self._make_run_script(_apply("/home/test.patch /home/fix.patch")),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""\
FROM {dep.image_full_name()}

{self.global_env}

WORKDIR /home/{self.pr.repo}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


_TAP_LINE = re.compile(r"^(not ok|ok)\s+\d+\s*-?\s*(.*)$", re.MULTILINE)
_BROWSER_VERSION = re.compile(
    r"^(Chrome|Firefox|Chromium|Safari|Headless \w+)\s+[\d.]+\s+-\s+"
    r"(?:\[[^\]]*\]\s+-\s+)?"
)
_DIRECTIVE = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

_TESTEM_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1):\d+(?:/\d+)?/")


@Instance.register("LiberatedPixelCup", "Universal-LPC-Spritesheet-Character-Generator")
class UniversalLPCSpritesheetCharacterGenerator(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LPCImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for status, raw_name in _TAP_LINE.findall(clean_log):
            name = raw_name.strip()
            if not name:
                continue

            directive = _DIRECTIVE.search(name)
            name = _DIRECTIVE.sub("", name).strip()
            name = _BROWSER_VERSION.sub(lambda m: f"{m.group(1)} - ", name).strip()
            name = _TESTEM_URL.sub("/", name).strip()
            if not name:
                continue

            if directive:
                skipped_tests.add(name)
            elif status == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

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
