import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DjangoFlatJsonWidgetImageBase(Image):
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
        return "python:3.11-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            fetch = (
                f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" '
                f"/home/{self.pr.repo}"
            )
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential curl wget bzip2 xz-utils \\
    firefox-esr fonts-liberation \\
    libgtk-3-0 libdbus-glib-1-2 libasound2 libx11-xcb1 libxtst6 \\
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \\
    arch="$(dpkg --print-architecture)"; \\
    case "$arch" in \\
      amd64) gecko_arch="linux64" ;; \\
      arm64) gecko_arch="linux-aarch64" ;; \\
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/geckodriver.tar.gz \\
      "https://github.com/mozilla/geckodriver/releases/download/v0.36.0/geckodriver-v0.36.0-${{gecko_arch}}.tar.gz"; \\
    tar -xzf /tmp/geckodriver.tar.gz -C /usr/local/bin; \\
    chmod +x /usr/local/bin/geckodriver; \\
    rm -f /tmp/geckodriver.tar.gz; \\
    geckodriver --version; \\
    firefox-esr --version
RUN git config --global --add safe.directory '*'
RUN pip install --no-cache-dir --upgrade pip wheel setuptools && mkdir -p /opt/pip-cache

{self.clear_env}

{fetch}
"""


class DjangoFlatJsonWidgetImageDefault(Image):
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
        return DjangoFlatJsonWidgetImageBase(self.pr, self.config)

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
export PIP_ROOT_USER_ACTION=ignore

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pip install --disable-pip-version-check --find-links /opt/pip-cache -e '.[test]' || true

# Warm the local wheel cache with the browser driver bindings that the fix
# patch pulls in via the "selenium" extra of openwisp-utils. They are not part
# of the base commit's dependency set, so without this the fix stage would be
# the only stage that needs the network.
pip download --disable-pip-version-check --dest /opt/pip-cache 'selenium>=4.10,<4.36' || true

python -c "import django, openwisp_utils; print(django.get_version())"
geckodriver --version
firefox-esr --version
python runtests.py -v 2 > /tmp/baseline.txt 2>&1
grep -q "Ran 2 tests" /tmp/baseline.txt

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export SELENIUM_HEADLESS=1
export GECKO_BIN=/usr/bin/firefox-esr
export SE_GECKODRIVER=/usr/local/bin/geckodriver
export PIP_ROOT_USER_ACTION=ignore

cd /home/{pr.repo}
pip install --disable-pip-version-check --find-links /opt/pip-cache -e '.[test]' || true
python runtests.py -v 2

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export SELENIUM_HEADLESS=1
export GECKO_BIN=/usr/bin/firefox-esr
export SE_GECKODRIVER=/usr/local/bin/geckodriver
export PIP_ROOT_USER_ACTION=ignore

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
pip install --disable-pip-version-check --find-links /opt/pip-cache -e '.[test]' || true
python runtests.py -v 2

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export SELENIUM_HEADLESS=1
export GECKO_BIN=/usr/bin/firefox-esr
export SE_GECKODRIVER=/usr/local/bin/geckodriver
export PIP_ROOT_USER_ACTION=ignore

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pip install --disable-pip-version-check --find-links /opt/pip-cache -e '.[test]' || true
python runtests.py -v 2

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        rendered = f"""FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}
{prepare_commands}
{self.clear_env}"""

        return rendered.rstrip() + "\n"


@Instance.register("openwisp", "django-flat-json-widget")
class DjangoFlatJsonWidget(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DjangoFlatJsonWidgetImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Django's test runner prints "<method> (<dotted.path>) ... <status>".
        # Tests carrying a docstring split that over two lines, with the status
        # trailing the docstring instead of the name.
        name_re = r"(?P<method>[\w.\-]+) \((?P<path>[\w.\-]+)\)"
        status_re = (
            r"(?P<status>ok|OK|FAIL|ERROR|skipped.*|"
            r"expected failure|unexpected success)"
        )
        header_pattern = re.compile(rf"^{name_re}$")
        inline_pattern = re.compile(rf"{name_re}\s+\.\.\.\s+{status_re}\s*$")
        summary_pattern = re.compile(rf"^(?P<status>FAIL|ERROR):\s+{name_re}\s*$")
        continuation_pattern = re.compile(rf"\.\.\.\s+{status_re}\s*$")
        # "Applying <migration>..." is emitted without a trailing newline, so the
        # first test line of a run is glued onto the end of it.
        migration_pattern = re.compile(r"^\s*Applying\s+\S+?\.\.\.")

        def canonical_name(method: str, path: str) -> str:
            if path == method or path.endswith(f".{method}"):
                return path
            return f"{path}.{method}"

        def record(name: str, status: str) -> None:
            status = status.strip().lower()
            if status.startswith("skipped") or status == "expected failure":
                skipped_tests.add(name)
            elif status in ("ok",):
                passed_tests.add(name)
            else:
                failed_tests.add(name)

        pending: Optional[str] = None
        for raw_line in clean_log.split("\n"):
            line = migration_pattern.sub("", raw_line).strip()

            match = summary_pattern.match(line)
            if match:
                record(
                    canonical_name(match.group("method"), match.group("path")),
                    match.group("status"),
                )
                pending = None
                continue

            match = inline_pattern.search(line)
            if match:
                record(
                    canonical_name(match.group("method"), match.group("path")),
                    match.group("status"),
                )
                pending = None
                continue

            match = header_pattern.match(line)
            if match:
                pending = canonical_name(match.group("method"), match.group("path"))
                continue

            if pending:
                match = continuation_pattern.search(line)
                if match:
                    record(pending, match.group("status"))
                pending = None

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
