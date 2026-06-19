import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "pre-commit"

# PRs: [468..886] | Python 3.5 (stretch, EOL)
#
# Conformed to the hardened image.py contract:
#   * dependency() returns a *string* base image, so the shared
#     Image.dockerfile() owns the build: it clones "${REPO_URL}", checks out
#     "${BASE_COMMIT}", and appends the _HARDENING_BLOCK that detaches HEAD and
#     deletes every other ref / remote / unreachable commit. That closes the
#     reward-hacking hole where the fix could be recovered from git history
#     (git log / show / diff origin/...). DockerfileEnhancer then injects the
#     proxy / cert / REPO_URL + BASE_COMMIT infra. None of that fired while the
#     old dockerfile() override hand-rolled its own FROM + clone + checkout.
#   * apt packages -> extra_packages(); pip / gem / git-config -> install.sh,
#     warmed in extra_setup() and re-run by the run scripts. The eval scripts and
#     both patches are COPYed into /home/ (outside /home/pre-commit) so the
#     history scrub -- which only rewrites the git tree -- leaves them intact.


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

    def dependency(self) -> str:
        # String dependency -> Image.dockerfile() clones, checks out
        # ${BASE_COMMIT}, and appends the hardening block. See module docstring.
        return "python:3.5-slim-stretch"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @staticmethod
    def _is_deprecated_debian(base_img: str) -> bool:
        # This era pins an EOL Debian suite (buster/stretch) whose image name is
        # "python:*-slim-<suite>", which Image.DEPRECATED_DEBIAN_IMAGES does NOT
        # match (it only knows the bare "debian:<suite>" / "gcc:*" tags). Force
        # True so the inherited _get_apt_update_command rewrites sources.list to
        # archive.debian.org before apt-get update -- otherwise the base build
        # 404s on the dead deb.debian.org mirror. Registry-only; image.py is left
        # untouched.
        return True

    def extra_packages(self) -> list[str]:
        # pre-commit's suite exercises hooks written in several languages, so the
        # toolchains must be present. make / git / build-essential / curl / ...
        # already come from Image.dockerfile()'s default package set.
        #
        # NOTE: no "npm" here. This era pins python:3.5-slim-stretch (Debian 9),
        # whose main repo has no standalone `npm` package -- apt aborts the whole
        # (atomic) install with "Unable to locate package npm". `nodejs` is
        # present and stays; the buster/bookworm eras keep npm.
        return ["golang", "ruby", "rustc", "nodejs", "patch"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening pass.
        # Bake the eval scripts + both patches in (build_dataset.run_instance does
        # NOT mount fix.patch at runtime, so the fix stage reads the baked copy),
        # then warm the install into an image layer.
        return (
            "ENV PIP_ROOT_USER_ACTION=ignore\n"
            "ENV PIP_DISABLE_PIP_VERSION_CHECK=1\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY install.sh /home/install.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "RUN bash /home/install.sh || true\n"
        )

    def files(self) -> list[File]:
        install = """#!/bin/bash
# Warm the environment at the (already checked-out, history-scrubbed) base
# commit and re-run at test time so a patch that adds a dependency still
# installs. Everything is guarded with || true: one failing optional dep must
# not abort the build or the test run.
set -uo pipefail
cd /home/__REPO_DIR__
git config --global user.email "you@example.com" || true
git config --global user.name "Your Name" || true
git config --global --add safe.directory '*' || true
gem install bundler || true
__PIP__"""

        run_sh = """#!/bin/bash
set -uo pipefail
export CI=true
bash /home/install.sh || true
cd /home/__REPO_DIR__
pytest --no-header -rA --tb=no -p no:cacheprovider -v --continue-on-collection-errors || true
"""

        test_run = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO_DIR__
git apply --whitespace=nowarn /home/test.patch || patch --batch --fuzz=5 -p1 -i /home/test.patch || echo "git apply test.patch failed (continuing)"
bash /home/install.sh || true
pytest --no-header -rA --tb=no -p no:cacheprovider -v --continue-on-collection-errors || true
"""

        fix_run = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO_DIR__
git apply --whitespace=nowarn /home/test.patch || patch --batch --fuzz=5 -p1 -i /home/test.patch || echo "git apply test.patch failed (continuing)"
git apply --whitespace=nowarn /home/fix.patch || patch --batch --fuzz=5 -p1 -i /home/fix.patch || echo "git apply fix.patch failed (continuing)"
bash /home/install.sh || true
pytest --no-header -rA --tb=no -p no:cacheprovider -v --continue-on-collection-errors || true
"""

        install = install.replace("__REPO_DIR__", REPO_DIR).replace("__PIP__", 'pip install -e . || pip install . || true\npip install -r requirements-dev.txt || pip install pytest pytest-env coverage || true\npip install pytest || true\n')
        run_sh = run_sh.replace("__REPO_DIR__", REPO_DIR)
        test_run = test_run.replace("__REPO_DIR__", REPO_DIR)
        fix_run = fix_run.replace("__REPO_DIR__", REPO_DIR)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", install),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


@Instance.register("pre-commit", "pre-commit_886_to_468")
class PreCommit886To468(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Verbose pytest output: tests/foo/bar.py::test_name PASSED/FAILED/SKIPPED
        verbose_re = re.compile(
            r"^(tests/.*?::\S+)\s+(PASSED|FAILED|SKIPPED)", re.MULTILINE
        )
        for match in verbose_re.finditer(log):
            test_name, status = match.groups()
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Short test summary (-rA shows PASSED/FAILED/SKIPPED/ERROR)
        summary_re = re.compile(
            r"^=+\s+short test summary info\s+=+((?:.|\n)*?)^=+.+=$", re.MULTILINE
        )
        summary_match = summary_re.search(log)
        if summary_match:
            summary_content = summary_match.group(1)
            failed_re = re.compile(r"^(?:FAILED|ERROR) (.*?)(?:\ - .*)?$", re.MULTILINE)
            for match in failed_re.finditer(summary_content):
                failed_tests.add(match.group(1).strip())
            passed_re = re.compile(r"^PASSED (.*?)$", re.MULTILINE)
            for match in passed_re.finditer(summary_content):
                passed_tests.add(match.group(1).strip())
            skipped_re = re.compile(r"^SKIPPED (.*?)(?:\ - .*)?$", re.MULTILINE)
            for match in skipped_re.finditer(summary_content):
                skipped_tests.add(match.group(1).strip())

        # Cleanup: failed tests should not be in passed/skipped
        passed_tests.difference_update(failed_tests)
        skipped_tests.difference_update(failed_tests)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
