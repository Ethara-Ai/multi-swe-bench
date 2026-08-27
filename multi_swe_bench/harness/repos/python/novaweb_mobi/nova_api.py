import re

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


BASE_IMAGE = "python:3.9-slim"

REPO_DIR = "/home/nova-api"

REPORT_FILE = "/home/pytest-report.xml"
STDOUT_LOG = "/home/pytest-stdout.log"

APT_PACKAGES = [
    "libpq-dev",
]

PINNED_REQUIREMENTS = [
    "pytest==6.2.5",
    "pytest-cov==2.12.1",
    "pytest-mock==3.6.1",
    "pytest-ordering==0.6",
    "mock==4.0.3",
    "mysql-connector==2.2.9",
    "psycopg2==2.9.1",
    "Flask==1.1.4",
    "connexion==2.9.0",
    "python-jose==3.3.0",
    "makefun==1.11.3",
    "Werkzeug==1.0.1",
    "Jinja2==2.11.3",
    "itsdangerous==1.1.0",
    "click==7.1.2",
    "MarkupSafe==1.1.1",
    "jsonschema==3.2.0",
    "PyYAML==5.4.1",
    "clickclick==20.10.2",
    "inflection==0.5.1",
    "openapi-spec-validator==0.3.1",
    "openapi-schema-validator==0.2.3",
    "requests==2.26.0",
    "six==1.16.0",
    "attrs==21.2.0",
    "pyrsistent==0.18.0",
    "urllib3==1.26.7",
    "idna==3.2",
    "certifi==2021.5.30",
    "charset-normalizer==2.0.6",
    "coverage==5.5",
    "toml==0.10.2",
    "iniconfig==1.1.1",
    "packaging==21.0",
    "pluggy==1.0.0",
    "py==1.10.0",
    "pyparsing==2.4.7",
    "ecdsa==0.17.0",
    "rsa==4.7.2",
    "pyasn1==0.4.8",
]

BOOTSTRAP_PINS = ["pip==21.2.4", "setuptools==58.1.0", "wheel==0.37.0"]

PYTEST_CMD = (
    "python -m pytest -rA -v -p no:cacheprovider --continue-on-collection-errors "
    f"--junitxml={REPORT_FILE} tests/unittests"
)

TEST_ENV = f"""export CI=true
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}{REPO_DIR}/nova_api:{REPO_DIR}/tests\""""

TEST_BODY = f"""rm -f {REPORT_FILE} {STDOUT_LOG}
cd {REPO_DIR}
set +e
{PYTEST_CMD} > {STDOUT_LOG} 2>&1
STATUS=$?
set -e
cat {STDOUT_LOG}
echo "pytest exit status: $STATUS"
if [ ! -s {REPORT_FILE} ]; then
    echo "FATAL: pytest wrote no JUnit report; the runner never started." >&2
    exit 1
fi
"""


_STATUSES = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"

_SUMMARY_RE = re.compile(
    rf"^(?P<status>{_STATUSES})\s+(?P<name>\S.*?)(?:\s+-\s.*)?$"
)

_VERBOSE_RE = re.compile(
    rf"^(?P<name>\S.*?)\s+(?P<status>{_STATUSES})(?:\s+\[\s*\d+%\])?\s*$"
)

class ImageBase(Image):
    """Repo-level base image. Carries the stock harness shape and nothing more:
    FROM, apt, clone, checkout, history scrub, CMD. The whole environment build
    lives in prepare.sh on the PR layer instead.

    Tagged per-PR (`base-pr-<N>`) rather than with a shared `base`: a repo-wide
    tag collapses every PR of the repo onto one image, and because the enhancer
    scrubs git history down to a single commit lineage, a second PR's
    prepare.sh could then no longer check out its own base commit. This dataset
    holds one PR today, but retrofitting the tag later means a full rebuild."""

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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return APT_PACKAGES

    _DEFAULT_APT_PACKAGES = [
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

    def dockerfile(self) -> str:
        base_img = self.dependency()
        repo = _safe_path_component(self.pr.repo)
        packages_str = " \\\n    ".join(
            self._DEFAULT_APT_PACKAGES + self.extra_packages()
        )

        sections = [f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(self._get_apt_update_command(packages_str, base_img))
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        extra_setup = self.extra_setup()
        if extra_setup:
            sections.append(extra_setup)

        sections.append(self._HARDENING_BLOCK)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class ImageDefault(Image):
    """PR-specific image: FROM the repo base, add the patches and the stage
    scripts, then run prepare.sh -- which is where the environment build
    happens."""

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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        """The full environment build, executed once by the PR image's
        `RUN bash /home/prepare.sh`.

        Every step is idempotent, so the script is still correct on the
        envagent replay path (where the harness re-feeds it before each stage).
        """
        requirements = " \\\n    ".join(f'"{r}"' for r in PINNED_REQUIREMENTS)
        bootstrap = " ".join(f'"{p}"' for p in BOOTSTRAP_PINS)
        return """#!/bin/bash
set -e
cd [[REPO_DIR]]

# --- Pristine tree at the PR's base commit -------------------------------
git reset --hard
bash /home/check_git_changes.sh
git checkout [[BASE_SHA]]
bash /home/check_git_changes.sh

# --- Python stack --------------------------------------------------------
pip install --no-cache-dir -U [[BOOTSTRAP]]
# `|| true` per the runbook: a native build (psycopg2 is the only one here)
# can fail in ways that are non-fatal for the suite, and the asserts below are
# what actually decide whether this image is usable. A hard failure still
# fails the build loudly -- one line down, with a message that names the
# missing import instead of a wall of pip output.
pip install --no-cache-dir \\
    [[REQUIREMENTS]] || true

# --- Fail-fast asserts ---------------------------------------------------
# Cheap, and they convert the two silent failure modes (a package that
# resolved but cannot import; psycopg2 failing to link against libpq) into a
# loud build failure at image-build time rather than a blank test stage.
pip check
python -c "import pytest, flask, connexion, jose, psycopg2, mysql.connector, \\
    makefun, mock, pytest_mock; print('deps ok:', pytest.__version__)"
python -c "import nova_api, nova_api.entity, nova_api.dao.generic_sql_dao; \\
    print('nova_api importable')"

# The trailing `set +e` matters on the envagent replay path: the body is fed
# into the same persistent bash session that later runs the tests, and a leaked
# `-e` would tear that session down the moment pytest exits non-zero --
# truncating the log parse_log is meant to read.
set +e
""".replace("[[REPO_DIR]]", REPO_DIR) \
   .replace("[[BASE_SHA]]", self.pr.base.sha) \
   .replace("[[BOOTSTRAP]]", bootstrap) \
   .replace("[[REQUIREMENTS]]", requirements)

    def files(self) -> list[File]:
        def stage(patch_line: str) -> str:
            return """#!/bin/bash
set -eo pipefail
[[TEST_ENV]]
[[PATCH_LINE]][[TEST_BODY]]""".replace("[[TEST_ENV]]", TEST_ENV) \
                              .replace("[[PATCH_LINE]]", patch_line) \
                              .replace("[[TEST_BODY]]", TEST_BODY)

        apply_test = (
            f"git -C {REPO_DIR} apply --whitespace=nowarn /home/test.patch"
            ' || { echo "Error: git apply failed" >&2; exit 1; }\n'
        )
        apply_both = (
            f"git -C {REPO_DIR} apply --whitespace=nowarn"
            " /home/test.patch /home/fix.patch"
            ' || { echo "Error: git apply failed" >&2; exit 1; }\n'
        )

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
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", stage("")),
            File(".", "test-run.sh", stage(apply_test)),
            File(".", "fix-run.sh", stage(apply_both)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("novaweb-mobi", "nova-api")
class NovaApi(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        for raw in clean_log.split("\n"):
            line = raw.rstrip()

            match = _SUMMARY_RE.match(line)
            if match:
                status, name = match.group("status"), match.group("name")
            else:
                match = _VERBOSE_RE.match(line)
                if not match:
                    continue
                name, status = match.group("name"), match.group("status")

            name = name.strip()

            if "::" not in name:
                continue

            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

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
