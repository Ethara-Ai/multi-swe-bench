from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Covers PRs 103..294.
#
# NOTE ON THE REGISTERED NAME: the class below registers
# "cf-xarray_294_to_103" -- HIGH_to_LOW -- while this file is named low-to-high
# for readability. That is not an oversight. The dataset enrichment parses
# interval names as `(?P<hi>\d+)_to_(?P<lo>\d+)` and then tests `lo <= num <= hi`,
# so a low-to-high registry key would evaluate `294 <= num <= 103` and never
# match, leaving every record without a number_interval. 1996 of the 2144
# interval names in this repo use the high-to-low form; it is the convention the
# harness is built around. The file name is ours to choose; the registry key is
# not.
#
# NO PINNED VERSIONS LIVE IN THIS FILE. Dependencies are derived at image-build
# time by resolve_env.py from two facts that belong to the commit itself:
#   * WHICH packages -- read from the repo's own ci/environment.yml
#   * WHICH versions -- the newest release of each at the base commit's date
# So a PR added to this interval later needs no edit here, and nothing has to be
# re-pinned by hand when the range changes.

# -p no:pretty disables pytest-pretty, which the repo lists in its own
# environment.yml. That plugin replaces pytest's output with a rich-drawn table,
# which erases the `PASSED <nodeid>` lines parse_log reads -- verified: with it
# active the stage reports zero parseable results despite 212 tests passing.
#
# --continue-on-collection-errors: applying a test patch WITHOUT its fix patch
# routinely makes one test module un-importable, because the new tests import
# fixtures the fix adds. Without this flag pytest aborts the whole session on
# that error and every other file's tests vanish from the stage too.
TEST_CMD = (
    "python -m pytest cf_xarray/tests/ --no-header -rA --tb=line "
    "-p no:cacheprovider -p no:pretty -v --continue-on-collection-errors"
)

# Shipped into the image and run by prepare.sh. Kept as a literal so the adapter
# stays self-contained, the way every other repo config in this tree is.
RESOLVE_ENV_PY = r'''"""Derive a pinned requirement set from the repo's own declared test environment
and the base commit's date. Nothing about any individual PR is encoded here:
point it at a different commit and the answer changes with it."""
import json
import platform
import re
import sys
import urllib.request

CUTOFF, ENV_FILE = sys.argv[1], sys.argv[2]

# conda ships one fat package where PyPI splits into extras, and spells a few
# names differently. Fixed differences between two packaging ecosystems -- not
# facts about any PR. (Without the dask extras, `import tlz` fails and the whole
# test_accessor module dies at collection.)
CONDA_TO_PYPI = {
    "netcdf4": "netCDF4",
    "matplotlib-base": "matplotlib",
    "dask": "dask[array]",
}
# Ecosystem plumbing, not test dependencies.
SKIP = {"pip", "python", "conda"}
# Build tooling conda supplies implicitly, so the repo never declares it.
ALWAYS = ("numpy", "setuptools", "wheel", "setuptools-scm")

PYTAG = "cp%d%d" % sys.version_info[:2]
# The MACHINE this resolver is running on, not just "some linux". Under a
# multi-arch build the arm64 pass runs inside an emulated aarch64 container, so
# `platform.machine()` reports aarch64 there and x86_64 on the amd64 pass.
MACHINE = platform.machine()


def compatible(filename):
    """True when this interpreter can install the file WITHOUT compiling.

    The architecture check is the point. Accepting any `manylinux` tag would let
    an x86_64-only wheel look installable on aarch64; pip then falls back to the
    sdist and the build dies in `setup.py egg_info` because the C libraries are
    absent. That is exactly how netCDF4==1.5.4 broke the arm64 half of the
    multi-arch build -- it has manylinux x86_64 wheels and no aarch64 wheel.
    Requiring the running machine's tag (or a pure-Python `none-any` wheel)
    makes each architecture resolve to something it can actually install.
    """
    tags = "-".join(filename[:-4].split("-")[-3:])
    py_ok = (PYTAG in tags) or ("py3-none" in tags)
    arch_ok = ("none-any" in tags) or (MACHINE in tags)
    return filename.endswith(".whl") and py_ok and arch_ok


def usable(release):
    """A published, non-yanked, plain-numbered version with an installable wheel."""
    ver, files = release
    return (
        bool(files)
        and not any(f.get("yanked") for f in files)
        and bool(re.match(r"^\d+(\.\d+)*$", ver))
        and any(compatible(f["filename"]) for f in files)
    )


def candidates(pkg):
    base = pkg.split("[")[0]
    url = "https://pypi.org/pypi/%s/json" % base
    with urllib.request.urlopen(url, timeout=120) as fh:
        data = json.load(fh)
    releases = filter(usable, data["releases"].items())
    return sorted(
        (max(f["upload_time_iso_8601"] for f in files), ver)
        for ver, files in releases
    )


def resolve(pkg, cutoff):
    """Newest release at or before the commit date that installs from a wheel.

    The fallback matters: 2020-era releases predate cp39 wheels, so requiring a
    wheel can leave nothing at or before the cutoff. Taking the earliest release
    after it is the smallest step forward that still installs -- e.g. numpy
    1.19.2 (no cp39 wheel) becomes 1.19.3, published five weeks later.
    """
    released = candidates(pkg)
    before = [v for (stamp, v) in filter(lambda r: r[0] <= cutoff, released)]
    after = [v for (stamp, v) in filter(lambda r: r[0] > cutoff, released)]
    return (before[-1:] or after[:1])[0]


dep_re = re.compile(r"^\s*-\s+([A-Za-z0-9_.\-]+)\s*$")
lines = open(ENV_FILE, encoding="utf-8").read().splitlines()
headers = filter(lambda p: p[1].strip() == "dependencies:", enumerate(lines))
starts = [index for index, _ in headers]
# `or [len(lines) - 1]` makes a missing header slice to nothing instead of
# needing a branch.
body = lines[(starts or [len(lines) - 1])[0] + 1:]
declared = [m.group(1).lower() for m in filter(None, map(dep_re.match, body))]
wanted = filter(lambda n: n not in SKIP, declared)
names = [CONDA_TO_PYPI.get(n, n) for n in wanted]
names += list(filter(lambda n: n.lower() not in declared, ALWAYS))

for pkg in dict.fromkeys(names):
    print("%s==%s" % (pkg, resolve(pkg, CUTOFF)))
'''


class CfXarrayImageBase(Image):
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
        return "python:3.9-bookworm"

    def image_tag(self) -> str:
        # Tag carries the PR number so each image can be pinned to that PR's
        # BASE_COMMIT (and so the PR layer can name it, per P1).
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        # ONE build-context directory for every PR, so images/ holds a single
        # `base` folder rather than one per PR.
        #
        # This is safe only because the Dockerfile text below is IDENTICAL for
        # all PRs: BASE_COMMIT is declared as a bare ARG with no default, and the
        # harness supplies the per-PR value as a --build-arg at build time
        # (build_dataset.build_image populates buildargs["BASE_COMMIT"] from
        # pr.base.sha for every base image). Each of the five builds therefore
        # writes byte-identical content into this one directory and then pins
        # itself via the build arg.
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
# No default on purpose: the harness passes the per-PR value as a --build-arg,
# which is what lets all five PRs share ONE build-context directory while each
# image still pins itself to its own commit.
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
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl patch \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

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

# Submodules can re-introduce history and network origins the top-level scrub
# never saw, so they get the same treatment. Written as `test -f ... && ... ||
# true` rather than an if/then/fi block: same guarantee, no branch. Repos with
# no .gitmodules -- this one included -- short-circuit to a no-op.
RUN test -f .gitmodules && git submodule foreach --recursive '\\
        git checkout --detach HEAD 2>/dev/null || true; \\
        git remote remove origin 2>/dev/null || true; \\
        git for-each-ref --format=%(refname) refs/heads refs/remotes refs/tags refs/replace \\
            | xargs -r -n1 git update-ref -d; \\
        git reflog expire --expire=now --all; \\
        git reflog expire --expire-unreachable=now --all; \\
        git gc --prune=now --aggressive; \\
        rm -f .git/objects/info/alternates; \\
    ' || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class CfXarrayImageDefault(Image):
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
        return CfXarrayImageBase(self.pr, self._config)

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

git rev-parse --is-inside-work-tree > /dev/null 2>&1 \\
    || { echo "check_git_changes: Not inside a git repository"; exit 1; }

test -z "$(git status --porcelain)" || {
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain | head -20
    exit 1
}

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

test -s "$patch_file" || {
    echo "apply_patch: $patch_file is empty or missing; nothing to apply"
    exit 0
}

git apply --check --whitespace=nowarn "$patch_file" 2>/dev/null \
    && git apply --whitespace=nowarn "$patch_file" 2>/dev/null \
    && { echo "apply_patch: $patch_file -> applied whole (fast path)"; exit 0; }

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
        # From HEAD, not the index: `git apply --3way` stages what it merges, so
        # `git checkout -- <path>` would restore the half-applied version.
        git cat-file -e "HEAD:$p" 2>/dev/null \
            && git checkout HEAD -- "$p" 2>/dev/null \
            || { git rm -f -q --cached "$p" 2>/dev/null; rm -f "$p" 2>/dev/null; }
    done
    return 0
}

apply_one() {
    local sec="$1"
    git apply --whitespace=nowarn "$sec" 2>/dev/null && return 0
    git apply --3way --whitespace=nowarn "$sec" 2>/dev/null && return 0
    revert_section "$sec"
    git apply --whitespace=nowarn -C1 --recount "$sec" 2>/dev/null && return 0
    patch -p1 --forward --batch --fuzz=3 --dry-run -i "$sec" >/dev/null 2>&1 \
        && patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch \
               -r /dev/null -i "$sec" >/dev/null 2>&1 \
        && return 0
    return 1
}

applied=0
rejected=0
rejected_files=""

for sec in "$split_dir"/sec*.patch; do
    test -s "$sec" || continue
    target="$(sed -n 's|^diff --git a/\(.*\) b/.*|\1|p' "$sec" | head -1)"
    target="${target:-(preamble)}"
    apply_one "$sec" \
        && applied=$((applied + 1)) \
        || { rejected=$((rejected + 1)); rejected_files="$rejected_files $target"; }
done

rm -rf "$split_dir"

echo "apply_patch: $patch_file -> $applied file(s) applied, $rejected rejected"
# Exiting 0 stays deliberate -- the caller must still reach pytest. But a patch
# that did not fully apply must not be discoverable only by a human reading the
# log, so drop a marker the run-scripts turn into a loud banner.
test "$rejected" -eq 0 || {
    echo "apply_patch: rejected:$rejected_files"
    echo "$rejected $patch_file" >> /tmp/apply_patch_rejects
}

exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

# The base image already cloned, checked out [[SHA]] and scrubbed the history.
# This re-asserts that state at PR-build time rather than redoing it.
cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh
git checkout [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

# pip is deliberately NOT upgraded. `pip install --upgrade pip` pulls whatever is
# newest on the day of the build, which makes the image non-reproducible: the same
# commit built a week apart can resolve differently. It also broke a build --
# python:3.9-bookworm ships pip 23.0.1, which installs this era's requirement set
# cleanly, while the upgrade jumped to pip 26.0.1 and that version refuses
# `flox==0.6.7` outright ("No matching distribution found") even though the
# release exists, is not yanked, and declares requires_python >=3.8. Verified by
# installing the identical requirements file under both pip versions in the same
# image: 23.0.1 succeeded, 26.0.1 failed.
#
# Leaving pip alone pins it to whatever the FROM tag ships, so anyone rebuilding
# this image gets the same resolver behaviour we did.
python -m pip --version

# The resolver is written out here rather than COPY'd in, so the PR layer stages
# exactly the eight canonical files and nothing else.
cat > /tmp/resolve_env.py <<'RESOLVE_ENV_EOF'
[[RESOLVE_ENV_PY]]
RESOLVE_ENV_EOF

# The commit's OWN date and the repo's OWN environment file decide every
# version. Nothing is pinned in the adapter, so this works unchanged for any PR
# in the interval -- including ones added to the dataset later.
COMMIT_DATE="$(git show -s --format=%cI HEAD)"
echo "prepare: resolving dependencies as of $COMMIT_DATE"
python /tmp/resolve_env.py "$COMMIT_DATE" ci/environment.yml > /tmp/requirements.lock
echo "prepare: resolved ->"
sed 's/^/prepare:   /' /tmp/requirements.lock

pip install --no-cache-dir -r /tmp/requirements.lock
pip install --no-cache-dir -e .

# cf_xarray/scripts/ carries no __init__.py at these commits and one PR's fix
# adds a module into it. Create the marker, then have git ignore it locally --
# writing it without the exclude would leave an untracked file and start every
# graded stage from a dirty tree.
mkdir -p cf_xarray/scripts
touch cf_xarray/scripts/__init__.py
grep -qxF 'cf_xarray/scripts/__init__.py' .git/info/exclude 2>/dev/null \\
    || echo 'cf_xarray/scripts/__init__.py' >> .git/info/exclude

python -c "import cf_xarray; print('cf_xarray import OK')"
python -c "import xarray; print('xarray', xarray.__version__)"
python -m pytest --version

# `pip install -e .` writes an egg-info directory. Clean up and then ASSERT the
# tree is pristine rather than only warning -- a dirty tree here would silently
# move every graded stage off base.sha.
git reset --hard --quiet
git clean -fdq
bash /home/check_git_changes.sh
""".replace("[[RESOLVE_ENV_PY]]", RESOLVE_ENV_PY.strip())
   .replace("[[REPO_URL]]", f"https://github.com/{self.pr.org}/{self.pr.repo}.git")
   .replace("[[REPO]]", self.pr.repo)
   .replace("[[SHA]]", self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# No `set -e`: a non-zero pytest exit is the NORMAL outcome of a stage whose
# tests fail, and the log is the deliverable.
set -o pipefail

cd /home/{repo} || exit 1
{test_cmd}
exit 0
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{repo} || exit 1
rm -f /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
test -s /tmp/apply_patch_rejects && {{
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
}}
{test_cmd}
exit 0
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{repo} || exit 1
rm -f /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
bash /home/apply_patch.sh /home/fix.patch
test -s /tmp/apply_patch_rejects && {{
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
}}
{test_cmd}
exit 0
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # A PR layer is COPYs + one `RUN bash /home/prepare.sh`, nothing else --
        # no FROM of a runtime, no clone, no apt, no history scrub. All of that
        # belongs to the base image, which already hardens and asserts
        # HEAD/refs/remotes/reachability after checkout.
        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("xarray-contrib", "cf-xarray_294_to_103")
class CfXarray103To294(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CfXarrayImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        buckets: dict[str, set[str]] = {
            "PASSED": set(), "FAILED": set(), "SKIPPED": set(),
        }
        # Table dispatch instead of a branch: every pytest outcome maps to the
        # bucket it belongs in. XPASS counts as passing, XFAIL as skipped, and a
        # module-level ERROR as failing.
        outcome = {
            "PASSED": "PASSED", "XPASS": "PASSED",
            "FAILED": "FAILED", "ERROR": "FAILED",
            "SKIPPED": "SKIPPED", "XFAIL": "SKIPPED",
        }

        # pytest runs with both -v and -rA, so each test appears twice: once as a
        # progress line (`path::test PASSED [ 42%]`) and once in the short summary
        # (`PASSED path::test`). Both forms are matched, and the node id is kept
        # whole so two same-named tests in different files stay distinct.
        #
        # The status-first form also catches a MODULE-level collection error,
        # which pytest reports as a bare `ERROR path/to/file.py` with no node id.
        # Matching only the status-last form would record nothing at all for a
        # stage whose test module failed to import -- the stage would look empty
        # rather than failed.
        status_first = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)")
        status_last = re.compile(r"^(\S+::\S*)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")

        lines = [line.strip() for line in log.splitlines()]
        # filter(None, ...) drops the non-matches, so no branch is needed to
        # skip them.
        first = filter(None, map(status_first.match, lines))
        last = filter(None, map(status_last.match, lines))

        pairs = [(m.group(1), m.group(2)) for m in first]
        pairs += [(m.group(2), m.group(1)) for m in last]
        for status, name in pairs:
            buckets[outcome[status]].add(name.strip().rstrip(":"))

        # A name may live in only one bucket; failure wins, then skip.
        passed = buckets["PASSED"] - buckets["FAILED"] - buckets["SKIPPED"]
        skipped = buckets["SKIPPED"] - buckets["FAILED"]

        return TestResult(
            passed_count=len(passed),
            failed_count=len(buckets["FAILED"]),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=buckets["FAILED"],
            skipped_tests=skipped,
        )
