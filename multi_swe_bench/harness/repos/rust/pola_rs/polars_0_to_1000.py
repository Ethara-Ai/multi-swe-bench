
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_IMAGE = "python:3.8-slim-bullseye"

TOOLCHAIN_WORKFLOW = ".github/workflows/build-test.yaml"
TOOLCHAIN_PATTERN = "nightly-[0-9]{4}-[0-9]{2}-[0-9]{2}"

REQUIREMENTS = "py-polars/build.requirements.txt"

PYTEST_CMD = "pytest tests -v -rA --tb=no -p no:cacheprovider"

MATURIN_BUILD = "maturin develop"

MATURIN_FLOOR = "0.9.4"
MATURIN_NAME_PROBE = (
    r"sed -n '/^\[package\.metadata\.maturin\]/,/^\[/p' Cargo.toml"
    r" | grep -qE '^[[:space:]]*name[[:space:]]*='"
)
MATURIN_IS_0_8_PROBE = r"maturin --version 2>/dev/null | grep -qE '(^|[^0-9.])0\.8\.'"


PIN_SCRIPT = r'''#!/usr/bin/env python3
"""Walk Cargo.lock back to the dependency versions that existed on the commit date.

There is no lockfile at any commit in this range, so cargo resolves every caret range against
TODAY's crates.io and picks releases written years after the toolchain the tree pins. Those
releases fail in three different ways - manifests using `dep:` namespaced features, manifests
on edition 2024, and sources using syntax like `let ... else` - and the failures surface at
three different stages, so repairing them crate by crate does not converge.

Resolving as of the commit date removes the whole class at once: every crate is taken at the
newest release that existed when the commit was written, which is the graph the tree was
actually developed against. crates.io's API carries the publication dates; the sparse index
does not, which is why this talks to the API.
"""
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

CUTOFF, REPO = sys.argv[1], sys.argv[2]
LOCK = os.path.join(REPO, "Cargo.lock")
UA = "multi-swe-bench polars image build (pin_to_commit_date)"
ENV = dict(os.environ)


def sh(args):
    return subprocess.run(args, cwd=REPO, env=ENV, capture_output=True, text=True)


_cache = {}


def released(name):
    """[(version, YYYY-MM-DD, yanked)] newest first. YANKED RELEASES ARE KEPT.

    Yanking is a fact about today, not about the commit date. Every ahash 0.7.x that existed
    while this range was written - 0.7.0 through 0.7.6 - has since been yanked, and the only
    0.7 releases still standing are 0.7.7 and 0.7.8, both published years later and both
    declaring `dep:` namespaced features that a 2021 cargo cannot parse. Drop the yanked ones
    and `ahash = "^0.7"` has no answer this toolchain can read, which is exactly the failure
    this whole script exists to prevent. So they stay in the list, and walk_back() deals with
    the fact that they cannot be installed the ordinary way.

    A failed lookup is never cached. Caching one would make the crate invisible to every later
    sweep and let the script report itself finished with the crate still on a 2026 release."""
    if name in _cache:
        return _cache[name]
    url = "https://crates.io/api/v1/crates/%s/versions" % name
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as h:
                data = json.load(h)
            _cache[name] = [(v["num"], v["created_at"][:10], bool(v["yanked"]))
                            for v in data["versions"]]
            return _cache[name]
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    print("pin: WARNING could not read release dates for %s" % name, flush=True)
    return []


_cksum = {}


def checksums(name):
    """{version: sha256} straight from the sparse index, which lists yanked releases too.

    crates.io's API gives dates but not checksums, and a [[package]] block without a correct
    checksum is rejected by cargo before it ever looks at the version. The index is the only
    place both halves are available for a release that has been yanked."""
    if name in _cksum:
        return _cksum[name]
    n = name.lower()
    if len(n) == 1:
        path = "1/" + n
    elif len(n) == 2:
        path = "2/" + n
    elif len(n) == 3:
        path = "3/%s/%s" % (n[0], n)
    else:
        path = "%s/%s/%s" % (n[:2], n[2:4], n)
    for attempt in range(5):
        try:
            req = urllib.request.Request("https://index.crates.io/" + path,
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as h:
                raw = h.read().decode()
            out = {}
            for line in raw.splitlines():
                if line.strip():
                    d = json.loads(line)
                    out[d["vers"]] = d["cksum"]
            _cksum[name] = out
            return out
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    print("pin: WARNING could not read index checksums for %s" % name, flush=True)
    return {}


def lock_swap(name, ver, cand):
    """Write a yanked release into Cargo.lock by hand. True if cargo then accepts the graph.

    `cargo update --precise` cannot reach a yanked release: the resolver filters yanked
    versions out of every registry query unless they are already present in the lockfile.
    That exception is the way in. Editing the [[package]] block directly puts the version on
    cargo's yanked whitelist, and from then on it resolves like any other.

    The block's `dependencies` list is deliberately left untouched. It describes the version
    being replaced, not the one going in, and cargo rewrites it from the real manifest on the
    next resolve. `cargo metadata` is the referee: if it cannot read the result, the original
    file goes back and the caller moves on to the next candidate."""
    cks = checksums(name).get(cand)
    if not cks:
        return False
    with open(LOCK) as fh:
        before = fh.read()
    head, sep, body = before.partition("[[package]]")
    if not sep:
        return False
    blocks = body.split("[[package]]")
    hit = -1
    for i, b in enumerate(blocks):
        if (re.search(r'^name = "%s"$' % re.escape(name), b, re.M)
                and re.search(r'^version = "%s"$' % re.escape(ver), b, re.M)):
            hit = i
            break
    if hit < 0:
        return False
    b = re.sub(r'^version = "%s"$' % re.escape(ver), 'version = "%s"' % cand,
               blocks[hit], count=1, flags=re.M)
    b, n = re.subn(r'^checksum = ".*"$', 'checksum = "%s"' % cks, b, count=1, flags=re.M)
    if not n:
        return False
    blocks[hit] = b
    with open(LOCK, "w") as fh:
        fh.write(head + sep + "[[package]]".join(blocks))
    r = sh(["cargo", "metadata", "--format-version", "1"])
    if r.returncode == 0:
        return True
    blame = blamed_crate(r.stderr)
    if blame is not None and blame[0] != name:
        return True
    with open(LOCK, "w") as fh:
        fh.write(before)
    return False


def locked():
    """Registry packages in the lockfile. Path and git entries have no crates.io release."""
    out = []
    with open(os.path.join(REPO, "Cargo.lock")) as fh:
        blocks = fh.read().split("[[package]]")[1:]
    for b in blocks:
        n = re.search(r'name = "(.*?)"', b)
        v = re.search(r'version = "(.*?)"', b)
        s = re.search(r'source = "(.*?)"', b)
        if n and v and s and s.group(1).startswith("registry+"):
            out.append((n.group(1), v.group(1)))
    return out


def walk_back(name, ver, rel, allow_after=False, limit=8):
    """Move one crate to the newest release that predates the cutoff. Returns it, or None.

    Several candidates are tried rather than only the newest, because the newest is often
    refused - some other crate in the graph requires a floor above it - and stopping at the
    first refusal leaves the crate on a 2026 release while the sweep reports itself finished.

    `allow_after` opens up the releases on the far side of the cutoff, oldest first. It is off
    during the sweep and on ONLY for a crate that cargo has just named as unreadable, and that
    distinction is the whole reason this converges. Applied to every crate it does not: a crate
    whose usable releases all postdate the cutoff gets pulled down every sweep and pushed back
    up by the next re-resolution, so ahash and crossbeam oscillate between two versions forever
    and the graph that comes out is not the one the cutoff describes.

    A candidate that has since been yanked is applied through lock_swap() instead, because
    `cargo update --precise` cannot select one."""
    before = [(v, w, y) for v, w, y in rel if w <= CUTOFF and v != ver]    # newest first
    groups = [before[:limit]]
    if allow_after:
        groups.append(sorted([(v, w, y) for v, w, y in rel if w > CUTOFF and v != ver],
                             key=lambda p: p[1])[:limit])                 # oldest first
    for group in groups:
        for cand, when, yanked in group:
            how = ""
            ok = sh(["cargo", "update", "-p", "%s:%s" % (name, ver),
                     "--precise", cand]).returncode == 0
            if not ok and yanked:
                ok = lock_swap(name, ver, cand)
                how = "  [yanked - written into Cargo.lock]"
            if ok:
                note = "" if when <= CUTOFF else "  [after cutoff - nothing usable before it]"
                print("pin:   %-26s %-14s -> %-14s (%s)%s"
                      % (name, ver, cand, when, note + how), flush=True)
                return cand
    return None


def sweep():
    """One pass over the lockfile, cutoff-respecting only. Returns how many crates moved."""
    pkgs = locked()
    with cf.ThreadPoolExecutor(8) as ex:
        list(ex.map(released, sorted({n for n, _ in pkgs})))
    changed = 0
    for name, ver in pkgs:
        rel = released(name)
        if not rel:
            continue
        dates = {v: w for v, w, _ in rel}
        if ver not in dates or dates[ver] <= CUTOFF:
            continue
        if walk_back(name, ver, rel):
            changed += 1
    return changed


BLAMED = re.compile(r"registry/src/[^/]+/([A-Za-z0-9_.-]+?)-(\d+\.\d+\.\d+[^/]*)/Cargo\.toml")


def blamed_crate(stderr):
    """The crate cargo just refused to read, as (name, version), or None.

    cargo names it in the manifest path it was parsing when it gave up, which is the only place
    the exact version appears."""
    m = BLAMED.search(stderr)
    return (m.group(1), m.group(2)) if m else None


def main():
    print("pin: resolving dependencies as of %s" % CUTOFF, flush=True)
    moved = 0
    for attempt in range(1, 7):
        for _ in range(8):
            changed = sweep()
            moved += changed
            print("pin: sweep moved %d crate(s)" % changed, flush=True)
            if not changed:
                break

        r = sh(["cargo", "metadata", "--format-version", "1"])
        if r.returncode == 0:
            print("pin: done, %d crate(s) moved, cargo metadata is clean" % moved, flush=True)
            return 0

        hit = blamed_crate(r.stderr)
        if not hit:
            break
        name, ver = hit
        print("pin: %s %s cannot be read and has nothing usable before the cutoff" % hit,
              flush=True)
        rel = released(name)
        if not rel or not walk_back(name, ver, rel, allow_after=True):
            break
        moved += 1

    print("pin: FAILED - cargo metadata still cannot read the graph", flush=True)
    sys.stderr.write(r.stderr[-3000:])
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''

SHELL_ENV = """\
export CI=true
export PY_COLORS=0
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_UNSTABLE_NAMESPACED_FEATURES=true
export RUSTFLAGS="-C debuginfo=0 -Z unstable-options"
export VIRTUAL_ENV=/home/{repo}/py-polars/venv
export PATH="$VIRTUAL_ENV/bin:/root/.cargo/bin:$PATH"
if [ -r /home/rust-toolchain-pin ]; then
  RUSTUP_TOOLCHAIN="$(cat /home/rust-toolchain-pin)"
  export RUSTUP_TOOLCHAIN
fi"""


BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM {image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    http_proxy=${{http_proxy}} \
    https_proxy=${{https_proxy}} \
    HTTP_PROXY=${{HTTP_PROXY}} \
    HTTPS_PROXY=${{HTTPS_PROXY}} \
    no_proxy=${{no_proxy}} \
    NO_PROXY=${{NO_PROXY}} \
    SSL_CERT_FILE=${{CA_CERT_PATH}} \
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \
      org.opencontainers.image.description="{org}/{repo} Docker image" \
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /home/

RUN printf '%s\n' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20250801T000000Z bullseye main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20250801T000000Z bullseye-security main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20250801T000000Z bullseye-updates main' \
        > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake curl git pkg-config \
    && rm -rf /var/lib/apt/lists/*

{fetch}

CMD ["/bin/bash"]
"""


class PolarsEarly2021ImageBase(Image):
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
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return BASE_DOCKERFILE.format(
            image=image_name,
            org=self.pr.org,
            repo=self.pr.repo,
            fetch=fetch,
        )


class PolarsEarly2021ImageDefault(Image):
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
        return PolarsEarly2021ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        env = SHELL_ENV.format(repo=self.pr.repo)
        repo = self.pr.repo

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\\
#!/bin/bash
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
                "prepare.sh",
                f"""\\
#!/bin/bash
set -e

{env}

cd /home/{repo}

bash /home/check_git_changes.sh

TOOLCHAIN=$(grep -oE '{TOOLCHAIN_PATTERN}' {TOOLCHAIN_WORKFLOW} | head -1)
if [ -z "$TOOLCHAIN" ]; then
  echo "prepare: no pinned nightly found in {TOOLCHAIN_WORKFLOW}" >&2
  exit 1
fi
echo "prepare: rust toolchain $TOOLCHAIN (read from {TOOLCHAIN_WORKFLOW})"

echo "$TOOLCHAIN" > /home/rust-toolchain-pin
export RUSTUP_TOOLCHAIN="$TOOLCHAIN"

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \\
    | sh -s -- -y --profile minimal --default-toolchain "$TOOLCHAIN" \\
  && /root/.cargo/bin/rustc --version \\
  && /root/.cargo/bin/cargo --version

cd /home/{repo}

JSONPATH_PIN=bf901857f3da5c1117b233e0d4bda7d8771294f5
_files=$(grep -rlE 'branch[[:space:]]*=[[:space:]]*"improve_compiled"' --include=Cargo.toml . 2>/dev/null || true)
_a2=$(grep -rlE 'ritchie46/arrow2' --include=Cargo.toml . 2>/dev/null || true)
if [ -n "$_files" ] || [ -n "$_a2" ]; then
  _BASE_DATE=$(git show -s --format=%cI HEAD)
  [ -n "$_files" ] && echo "$_files" | xargs sed -i -E "s|branch[[:space:]]*=[[:space:]]*\\"improve_compiled\\"|rev = \\"$JSONPATH_PIN\\"|g"
  [ -n "$_a2" ] && echo "$_a2" | xargs sed -i -E 's|(ritchie46/arrow2"[^}}]*)branch[[:space:]]*=[[:space:]]*"dev"|\\1rev = "4bb32375520fbd974b98028cec37ada969a1996c"|g'
  GIT_COMMITTER_DATE="$_BASE_DATE" \
    git -c user.email=harness@local -c user.name=harness commit --date="$_BASE_DATE" -aq -m "harness: pin git deps"
  [ -n "$_files" ] && echo "jsonpath pinned in: $_files (committer date=$_BASE_DATE)"
  [ -n "$_a2" ] && echo "arrow2 pinned in: $_a2"
fi

cargo generate-lockfile
python3 - "$(git show -s --format=%cI HEAD | cut -c1-10)" /home/{repo} <<'PIN_TO_COMMIT_DATE_EOF'
{PIN_SCRIPT}
PIN_TO_COMMIT_DATE_EOF

cd /home/{repo}/py-polars
python -m venv venv
python -m pip install --upgrade 'pip<23' || true

python -m pip install -r /home/{repo}/{REQUIREMENTS} || true

REQUIRES_DIST=$(sed -n '/^\[package\.metadata\.maturin\]/,/^\[/p' /home/{repo}/py-polars/Cargo.toml | grep '^requires-dist' | cut -d'[' -f2 | tr -d '"]' | tr ',' ' ')
if [ -n "$REQUIRES_DIST" ]; then
  python -m pip install $REQUIRES_DIST || true
else
  python -m pip install numpy "pyarrow<5" || true
fi
python -m pip install pandas || true

if {MATURIN_NAME_PROBE} && {MATURIN_IS_0_8_PROBE}; then
  echo "prepare: manifest declares package.metadata.maturin.name, which maturin 0.8.x cannot read - upgrading to {MATURIN_FLOOR}"
  python -m pip install "maturin=={MATURIN_FLOOR}"
fi

{MATURIN_BUILD}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\\
#!/bin/bash
set -euo pipefail

{env}

cd /home/{repo}
_files=$(grep -rlE 'branch[[:space:]]*=[[:space:]]*"improve_compiled"' --include=Cargo.toml . 2>/dev/null || true)
[ -n "$_files" ] && echo "$_files" | xargs sed -i -E 's|branch[[:space:]]*=[[:space:]]*"improve_compiled"|rev = "bf901857f3da5c1117b233e0d4bda7d8771294f5"|g'
_a2=$(grep -rlE 'ritchie46/arrow2' --include=Cargo.toml . 2>/dev/null || true)
[ -n "$_a2" ] && echo "$_a2" | xargs sed -i -E 's|(ritchie46/arrow2"[^}}]*)branch[[:space:]]*=[[:space:]]*"dev"|\1rev = "4bb32375520fbd974b98028cec37ada969a1996c"|g'

cd /home/{repo}/py-polars
{MATURIN_BUILD}
{PYTEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\\
#!/bin/bash
set -euo pipefail

{env}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
_files=$(grep -rlE 'branch[[:space:]]*=[[:space:]]*"improve_compiled"' --include=Cargo.toml . 2>/dev/null || true)
[ -n "$_files" ] && echo "$_files" | xargs sed -i -E 's|branch[[:space:]]*=[[:space:]]*"improve_compiled"|rev = "bf901857f3da5c1117b233e0d4bda7d8771294f5"|g'
_a2=$(grep -rlE 'ritchie46/arrow2' --include=Cargo.toml . 2>/dev/null || true)
[ -n "$_a2" ] && echo "$_a2" | xargs sed -i -E 's|(ritchie46/arrow2"[^}}]*)branch[[:space:]]*=[[:space:]]*"dev"|\1rev = "4bb32375520fbd974b98028cec37ada969a1996c"|g'

cd /home/{repo}/py-polars
{MATURIN_BUILD}
{PYTEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\\
#!/bin/bash
set -euo pipefail

{env}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
_files=$(grep -rlE 'branch[[:space:]]*=[[:space:]]*"improve_compiled"' --include=Cargo.toml . 2>/dev/null || true)
[ -n "$_files" ] && echo "$_files" | xargs sed -i -E 's|branch[[:space:]]*=[[:space:]]*"improve_compiled"|rev = "bf901857f3da5c1117b233e0d4bda7d8771294f5"|g'
_a2=$(grep -rlE 'ritchie46/arrow2' --include=Cargo.toml . 2>/dev/null || true)
[ -n "$_a2" ] && echo "$_a2" | xargs sed -i -E 's|(ritchie46/arrow2"[^}}]*)branch[[:space:]]*=[[:space:]]*"dev"|\1rev = "4bb32375520fbd974b98028cec37ada969a1996c"|g'

cd /home/{repo}/py-polars
{MATURIN_BUILD}
{PYTEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_ID_PREFIX = "py-polars/"

_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<name>[^\s]+?)(?:\s+-\s+.*)?$"
)

_PROGRESS_RE = re.compile(
    r"^(?P<name>[^\s]+::[^\s]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
)

_FAIL_STATUSES = {"FAILED", "ERROR"}
_SKIP_STATUSES = {"SKIPPED", "XFAIL", "XPASS"}


def parse_pytest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _SUMMARY_RE.match(line) or _PROGRESS_RE.match(line)
        if not match:
            continue

        name = match.group("name")
        if "::" not in name and "/" not in name:
            continue
        name = _ID_PREFIX + name

        status = match.group("status")
        if status in _FAIL_STATUSES:
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif name not in failed_tests:
            if status in _SKIP_STATUSES:
                if name not in passed_tests:
                    skipped_tests.add(name)
            else:
                skipped_tests.discard(name)
                passed_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("pola-rs", "polars_0_to_1000")
class POLARS_0_TO_1000(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PolarsEarly2021ImageDefault(self.pr, self._config)

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
        return parse_pytest_log(test_log)


Instance.register("pola-rs", "polars")(POLARS_0_TO_1000)
