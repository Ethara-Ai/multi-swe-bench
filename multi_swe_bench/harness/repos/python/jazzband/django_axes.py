from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Django is pinned rather than left to pip. `setup.py` declares only
# `django>=2.0`, which on a modern index resolves to Django 4.x/5.x -- a decade
# newer than this Oct-2020 codebase and guaranteed to break it. The repo's own
# tox matrix at this commit is django{22,30,31}, so 3.1 is the newest version
# the project actually claims to support, and it is what CI would have run.
DJANGO_PIN = "django>=3.1,<3.2"

# `--no-header` needs pytest >= 6.0; requirements-test.txt pins pytest==6.1.1.
# pytest.ini already supplies `testpaths = axes/tests`, the coverage addopts and
# DJANGO_SETTINGS_MODULE, so no path or settings argument is needed here.
TEST_CMD = "python -m pytest -v -rA --no-header -p no:cacheprovider"


class DjangoAxesImageBase(Image):
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
        # Travis matrix at this commit is 3.6/3.7/3.8 and setup.py declares
        # python_requires="~=3.6". 3.8 is the newest the project tests against;
        # 3.9+ would pair badly with Django 3.1.
        return "python:3.8"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            fetch = 'RUN git clone "${REPO_URL}" /home/' + self.pr.repo
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        org = self.pr.org
        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT={sha}

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    DJANGO_SETTINGS_MODULE=axes.tests.settings
WORKDIR /home/

# The suite runs against sqlite in-memory (axes/tests/settings.py), so no database
# service is needed. `patch` backs the apply_patch.sh fallback when `git apply`
# rejects a hunk; python:3.8 is buildpack-deps based and already ships the C
# toolchain, so build-essential is not required.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl patch \\
    && rm -rf /var/lib/apt/lists/*

{fetch}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

{self.clear_env}

CMD ["/bin/bash"]
"""


class DjangoAxesImageDefault(Image):
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
        return DjangoAxesImageBase(self.pr, self._config)

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
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does NOT remove stray untracked ones, and the Dockerfile's HEAD/refs asserts
# only prove WHICH commit is checked out -- a dirty tree satisfies all of them.
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
""",
            ),
            File(
                ".",
                "apply_patch.sh",
                r"""#!/bin/bash
# Apply one patch as completely as possible, then ALWAYS exit 0. The caller must
# reach pytest no matter how patching went: a stage that dies while patching
# reports zero tests, which the harness cannot tell apart from "the fix does not
# work". Whole-patch fast path first; per-file cascade only when something
# rejects, so one unappliable file cannot take the gold tests down with it.

patch_file="$1"

if [ ! -s "$patch_file" ]; then
    echo "apply_patch: $patch_file is empty or missing; nothing to apply"
    exit 0
fi

if git apply --check --whitespace=nowarn "$patch_file" 2>/dev/null; then
    if git apply --whitespace=nowarn "$patch_file" 2>/dev/null; then
        echo "apply_patch: $patch_file -> applied whole (fast path)"
        exit 0
    fi
fi

split_dir="$(mktemp -d)"
csplit -z -s -f "$split_dir/sec" -b '%05d.patch' "$patch_file" '/^diff --git /' '{*}' \
    2>/dev/null || cp "$patch_file" "$split_dir/sec00000.patch"

section_paths() {
    sed -n -e 's|^--- a/||p' -e 's|^+++ b/||p' "$1" \
        | grep -v '^/dev/null$' | sort -u
}

revert_section() {
    local p
    for p in $(section_paths "$1"); do
        if git cat-file -e "HEAD:$p" 2>/dev/null; then
            # From HEAD, not the index: `git apply --3way` stages what it merges,
            # so `git checkout -- <path>` would restore the half-applied version.
            git checkout HEAD -- "$p" 2>/dev/null || true
        else
            git rm -f -q --cached "$p" 2>/dev/null || true
            rm -f "$p" 2>/dev/null || true
        fi
    done
}

apply_one() {
    local sec="$1"
    git apply --whitespace=nowarn "$sec" 2>/dev/null && return 0
    if git apply --3way --whitespace=nowarn "$sec" 2>/dev/null; then return 0; fi
    revert_section "$sec"
    git apply --whitespace=nowarn -C1 --recount "$sec" 2>/dev/null && return 0
    if patch -p1 --forward --batch --fuzz=3 --dry-run -i "$sec" >/dev/null 2>&1; then
        patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch \
            -r /dev/null -i "$sec" >/dev/null 2>&1 && return 0
    fi
    return 1
}

applied=0
rejected=0
rejected_files=""

for sec in "$split_dir"/sec*.patch; do
    [ -s "$sec" ] || continue
    target="$(sed -n 's|^diff --git a/\(.*\) b/.*|\1|p' "$sec" | head -1)"
    [ -n "$target" ] || target="(preamble)"
    if apply_one "$sec"; then
        applied=$((applied + 1))
    else
        rejected=$((rejected + 1))
        rejected_files="$rejected_files $target"
    fi
done

rm -rf "$split_dir"

echo "apply_patch: $patch_file -> $applied file(s) applied, $rejected rejected"
if [ "$rejected" -gt 0 ]; then
    echo "apply_patch: rejected:"
    for f in $rejected_files; do echo "apply_patch:   $f"; done
    # Exiting 0 stays deliberate -- the caller must still reach pytest. But a
    # patch that did not fully apply must not be discoverable only by a human
    # reading the log. Drop a marker the run-scripts turn into a loud banner.
    echo "$rejected $patch_file" >> /tmp/apply_patch_rejects
fi

exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Django first and pinned, so the pinned version wins before `-e .` can pull an
# unbounded `django>=2.0` from setup.py. requirements-test.txt then supplies the
# rest at the versions this commit was tested with (pytest 6.1.1,
# pytest-django 3.10.0, pytest-cov 2.10.1, pytest-subtests 0.3.2, coverage 5.3)
# and installs the package itself in editable mode via its own `-e .` line.
#
# `six` is an undeclared dependency of pytest-django 3.10.0: its
# live_server_helper.py does `import six` without listing it in install_requires.
# In 2020 it came along transitively; on a clean index against Django 3.1 nothing
# pulls it in, and pytest dies at COLLECTION time -- which would look like every
# test failing rather than a missing package. Pinned to the last 1.x, the series
# contemporary with this commit.
pip install --no-cache-dir "{django_pin}" "six==1.15.0"
pip install --no-cache-dir -r requirements-test.txt

python -c "import django; print('django', django.get_version())"
python -c "import axes; print('axes import OK')"
python -m pytest --version

# `-e .` writes axes.egg-info. That path is gitignored, so it does not dirty the
# tree, but clean up anyway and then ASSERT pristine rather than only warning --
# a dirty tree here would silently move every graded stage off base.sha.
git reset --hard --quiet
git clean -fdq
bash /home/check_git_changes.sh
""".format(pr=self.pr, django_pin=DJANGO_PIN),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# No `set -e`: a non-zero pytest exit is the NORMAL outcome of a stage whose
# tests fail, and the log is the deliverable.
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
rm -f /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
rm -f /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
bash /home/apply_patch.sh /home/fix.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # A PR layer is COPYs + one `RUN bash /home/prepare.sh`, nothing else --
        # no FROM of a runtime, no clone, no apt, no history scrub. All of that
        # belongs to the base image, which already hardens and asserts
        # HEAD/refs/remotes/reachability after checkout.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("jazzband", "django-axes")
class DjangoAxes(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DjangoAxesImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `-rA` prints a short-summary line per outcome. The node id is captured
        # whole (path::Class::test), not just the path, so two tests in one file
        # stay distinct -- collapsing them to the file would merge a pass and a
        # fail. django-axes puts every test in a TestCase class, so the ids carry
        # a class segment.
        result_re = re.compile(
            r"^(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED)(?:\s+\[\s*\d+\s*\])?\s+(\S+)"
        )

        for raw in log.splitlines():
            m = result_re.match(raw.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2).strip().rstrip(":")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A name may live in only one bucket.
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
