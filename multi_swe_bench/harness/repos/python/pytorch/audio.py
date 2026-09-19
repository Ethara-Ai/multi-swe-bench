import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "pytorch"
REPO = "audio"

# python:3.8-slim: both eras need Python 3.8 for their torch pins. The sox
# toolchain (libsox-dev + libsox-fmt-all) is what the C++ extensions link.
_BASE_IMAGE = "python:3.8-slim"
_BASE_TAG = "base-py38-sox"

_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "wget",
    "ninja-build",
    "pkg-config",
    "cmake",
    "sox",
    "libsox-dev",
    "libsox-fmt-all",
    "libsndfile1",
]


# --------------------------------------------------------------------- scripts

SHEBANG = "#!/bin/bash\nset -eo pipefail"

CHECK_GIT_CHANGES = """#!/bin/bash
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

APPLY_TEST = """if ! git apply --whitespace=nowarn -C1 /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi"""

APPLY_FIX = """if ! git apply --whitespace=nowarn -C1 /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi"""

# Exported by prepare.sh. Free of braces so it can be dropped into the
# f-string templates below.
PREPARE_ENV = """export MAX_JOBS=8
export SETUPTOOLS_USE_DISTUTILS=stdlib
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONDONTWRITEBYTECODE=1"""

# Old torch cpp_extension breaks against setuptools>=60's vendored distutils;
# torchaudio 0.5's setup.py needs the stdlib one.
INSTALL_LATEST_PIP = (
    'python -m pip install --no-cache-dir --upgrade "pip<24.1" "setuptools<60" "wheel" || true'
)

# The "+cpu" local version is published for x86_64 only; aarch64 takes the
# plain version from the community mirror. Contains literal braces only in
# the ${...} shell sense, none in the format() sense.
INSTALL_TORCH = """if [ "$(uname -m)" = "x86_64" ]; then
python -m pip install --no-cache-dir {torch_pin} -f https://download.pytorch.org/whl/torch_stable.html || true
else
python -m pip install --no-cache-dir {torch_pin_aarch64} -f https://torch.kmtea.eu/whl/stable.html || true
fi"""


# ---------------------------------------------------------------- test scoping

# The pytest command must stay character-identical across run.sh, test-run.sh
# and fix-run.sh; only TEST_FILES / extra args differ per era. The existence
# filter keeps the untouched run stage from aborting on a test file the test
# patch has not yet created.
TEST_COMMANDS = """TEST_FILES="{test_files}"
TEST_TARGETS=""
for candidate in $TEST_FILES; do
  if [ -e "$candidate" ]; then
    TEST_TARGETS="$TEST_TARGETS $candidate"
  fi
done
python -m pytest -v -rA -p no:cacheprovider --continue-on-collection-errors $TEST_TARGETS{extra_pytest_args}"""


def _era(number: int) -> dict[str, str]:
    """Era facts for a PR number: torch pins, test files and era deps.

    PRs <= 470 sit on torchaudio 0.5.0a0 (old setup.py, ``_torch_sox``,
    ``six`` + ``backports.tempfile`` in the test utils); PR #790 sits on
    torchaudio 0.7.0a0 (cmake/ninja ``_torchaudio``, ``scipy`` +
    ``parameterized`` in the test utils, and the libritts stub below).
    """
    if number <= 470:
        return {
            "torch_pin": "torch==1.5.1+cpu",
            "torch_pin_aarch64": "torch==1.5.1",
            "test_files": "test/test_functional_filtering.py",
            "extra_pytest_args": "",
            "era_deps": (
                'python -m pip install --no-cache-dir "numpy<1.24" "six" '
                '"backports.tempfile" "pytest==7.4.4" || true'
            ),
            "stub_block": "",
        }
    return {
        "torch_pin": "torch==1.6.0+cpu",
        "torch_pin_aarch64": "torch==1.6.0",
        "test_files": "test/test_datasets.py",
        # Every other class in test_datasets.py downloads multi-gigabyte
        # corpora; -k keeps collection while deselecting them.
        "extra_pytest_args": " -k 'LibriTTS or libritts'",
        "era_deps": (
            'python -m pip install --no-cache-dir "numpy<1.24" "scipy==1.10.1" '
            '"parameterized" "pytest==7.4.4" || true'
        ),
        # Copied into site-packages so the test-stage import of
        # torchaudio.datasets.libritts resolves to the stub (LIBRITTS=None)
        # until the fix patch adds the real module.
        "stub_block": """SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
cp /home/libritts_stub.pth "$SITE/libritts_stub.pth\"""",
    }


# Executable .pth line: site.py only executes lines starting with "import",
# and every lambda closes over no free variables (names bound by the .pth
# exec are invisible to them), hence the __import__ calls. The finder is
# APPENDED to sys.meta_path so the real module always wins once it exists.
LIBRITTS_STUB_PTH = (
    "import sys; sys.meta_path.append(type('_LibriTTSStubFinder', (), "
    "{'find_spec': staticmethod(lambda name, path=None, target=None: "
    "__import__('importlib.util').util.spec_from_loader(name, "
    "type('_LibriTTSStubLoader', (), {'create_module': staticmethod(lambda spec: "
    "__import__('types').ModuleType(name)), 'exec_module': staticmethod(lambda m: "
    "setattr(m, 'LIBRITTS', None))})()) "
    "if name == 'torchaudio.datasets.libritts' else None)}))\n"
)


# ---------------------------------------------------------------- log parsing

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# pytest -v verbose lines:
#   test/test_functional_filtering.py::TestClass::test_case PASSED [ 12%]
# pytest -rA short-summary lines:
#   PASSED test/test_functional_filtering.py::TestClass::test_case
RE_PASSES = [
    re.compile(r"^(\S+::\S+)\s+PASSED\b"),
    re.compile(r"^PASSED\s+(\S+::\S+)"),
]
RE_FAILS = [
    re.compile(r"^(\S+::\S+)\s+(?:FAILED|ERROR)\b"),
    re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+)"),
]
RE_SKIPS = [
    re.compile(r"^(\S+::\S+)\s+(?:SKIPPED|XFAIL|XPASS)\b"),
    re.compile(r"^(?:SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"),
]


# --------------------------------------------------------------------- images

class ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # The leading syntax directive keeps DockerfileEnhancer from rewriting
        # the clone or pinning this shared image to one PR's BASE_COMMIT, so
        # the infrastructure block it would otherwise contribute is written
        # out here. BASE_COMMIT is declared but deliberately unused:
        # build_dataset passes it to every image whose dependency() is a str,
        # and an undeclared build arg is a build warning.
        return f"""# syntax=docker/dockerfile:1.6

FROM {base_img}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.description="{ORG}/{REPO} Docker image" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/ca-bundle.crt
{global_env}
WORKDIR /home/

{apt_command}

RUN git clone "${{REPO_URL}}" /home/{REPO}
{clear_env}
CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        era = _era(self.pr.number)
        test_commands = TEST_COMMANDS.format(
            test_files=era["test_files"],
            extra_pytest_args=era["extra_pytest_args"],
        )

        run_files = [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
cd /home/{REPO}
bash /home/check_git_changes.sh
{PREPARE_ENV}
{INSTALL_LATEST_PIP}
{INSTALL_TORCH.format(
    torch_pin=era["torch_pin"],
    torch_pin_aarch64=era["torch_pin_aarch64"],
)}
{era["era_deps"]}
{era["stub_block"]}
python -m pip install --no-cache-dir --no-build-isolation -e .
bash /home/check_git_changes.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{test_commands}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_TEST}
{test_commands}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_FIX}
{test_commands}
""",
            ),
        ]

        if era["stub_block"]:
            run_files.append(File(".", "libritts_stub.pth", LIBRITTS_STUB_PTH))

        return run_files

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # Chains to a base Image rather than a str, so DockerfileEnhancer
        # returns this verbatim and injects nothing; the hardening block is
        # applied by hand. It opens with `git checkout --detach
        # "${BASE_COMMIT}"`, so it performs this PR's checkout as well as
        # pruning the full history inherited from the shared base. The repo
        # is cloned once in that base, so this layer never clones and needs
        # no REPO_URL.
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT={self.pr.base.sha}
{global_env}
{copy_commands}
WORKDIR /home/{REPO}

{Image._HARDENING_BLOCK.rstrip()}

RUN bash /home/prepare.sh
{clear_env}"""


# ------------------------------------------------------------------- instance

@Instance.register(ORG, REPO)
class Audio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

        clean_log = ANSI_ESCAPE.sub("", test_log)

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass in RE_PASSES:
                pass_match = re_pass.match(line)
                if pass_match:
                    passed_tests.add(pass_match.group(1).strip())

            for re_fail in RE_FAILS:
                fail_match = re_fail.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1).strip())

            for re_skip in RE_SKIPS:
                skip_match = re_skip.match(line)
                if skip_match:
                    skipped_tests.add(skip_match.group(1).strip())

        # Remove any overlap (a test name should only appear once); worst
        # wins, matching TestResult's overlap rejection.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
