"""pola-rs/polars PRs up to #1000 - Rust core graded through the py-polars pytest suite.

Every value below was read off the repo at the five base commits in this range, not inferred.
The range is 2021-02 to 2021-07, and the repo changes underneath it, so the numbers differ
per PR:

  PR   base sha    workflow toolchain     build.requirements.txt
  364  0bcb4548    nightly-2021-02-12     maturin==0.8.1  pytest==5.3.1  black==20.8b1
  478  41c51c98    nightly-2021-03-04     maturin==0.8.1  pytest==5.3.1  black==20.8b1
  504  3fd6b0d8    nightly-2021-03-25     maturin==0.9.4  + pdoc3 numpy ghp-import pandas flake8
  519  5c361738    nightly-2021-03-25     maturin==0.9.4  + pdoc3 numpy ghp-import pandas flake8
  947  90dc0fde    nightly-2021-07-04     maturin==0.11.0 + numpy ghp-import pandas flake8 mypy

Five things are worth knowing before changing anything here.

1. THE TOOLCHAIN IS NOT PINNED IN THIS FILE. `py-polars/rust-toolchain` says the single word
   `nightly` at four of the five commits and does not exist at the fifth, so it cannot be used
   - a 2026 nightly does not compile 2021 polars. The exact date lives in the repo's own
   `.github/workflows/build-test.yaml` as `toolchain: nightly-YYYY-MM-DD`, and prepare.sh
   greps it out of the checked-out tree at build time. That is why four different nightlies
   are served correctly by one file with no per-PR branching, and why a sixth PR added to this
   range needs no edit here.

2. ONE BASE IMAGE SERVES ALL FIVE. That is only possible because the base stops at `git clone`:
   it installs the C toolchain and clones, and nothing else. rustup moved into prepare.sh
   precisely because the nightly differs per PR - had it stayed in the base, four different
   nightlies would have forced four base images. The Python interpreter is the one thing the
   base must fix, and 3.8 is chosen deliberately (see 4).

3. GRADED BY PYTEST, NOT `cargo test`. Every test patch in this range writes a single file
   under `py-polars/tests/`, and those files are Python, so no cargo invocation can observe
   any of them - all three stages would return an identical Rust suite, nothing would
   transition, and Report.check() would reject at rule 3. (py-polars is a member of the root
   cargo workspace at #364 but NOT at the other four commits, which matters for a different
   reason: see 5.)
   py-polars depends on polars-core and polars-io BY PATH, so `maturin develop` compiles each
   fix patch's Rust changes into the extension the Python tests import. `pytest tests` is the
   repo's own command, from `py-polars/tasks.sh`'s `build-run-tests`. Note `tests`, not
   `tests/unit/` - that subdirectory does not exist until long after #947, which is why the
   existing polars_0_to_12255 config cannot grade these five.

4. PYTHON 3.8, NOT THE 3.6 THE WORKFLOW NAMES. Every workflow in this range sets
   python-version 3.6, but the 3.6 images are EOL: their Debian releases are off the mirrors
   and apt fails before the first layer completes. 3.8 is the newest interpreter that predates
   maturin 0.8.1 (PR #364's pin, released before Python 3.9 existed), so it satisfies the
   oldest PR while still sitting on a bullseye base whose apt sources resolve.

5. THE DEPENDENCY GRAPH IS RESOLVED AS OF THE COMMIT DATE, because nothing in the tree pins it.
   There is no root Cargo.lock anywhere in this range - polars added one only in #12256,
   exactly where polars_0_to_12255 takes over - so every caret range resolves against TODAY's
   crates.io and the 2021 toolchain is handed releases written years after it. They fail in
   three different ways and at three different stages: manifests using `dep:` namespaced
   features die in the parser, manifests on edition 2024 die in the parser for a different
   reason, and sources using newer syntax such as `let ... else` compile fine as manifests and
   then fail in rustc. Repairing them crate by crate does not converge - each fix simply
   uncovers whichever crate was behind it.

   prepare.sh therefore generates the lockfile and hands it to PIN_SCRIPT, which walks every
   registry crate back to the newest release that existed on the day the commit was written.
   That is the graph the tree was actually developed against, so the whole class disappears in
   one step instead of one crate at a time. The date is read from the commit at build time and
   no crate version is recorded in this file: a table of pins would be wrong the moment any of
   those crates published again, and would say nothing at all about a sixth PR added here.

   Two flags in SHELL_ENV cover what remains. Editions and `dep:` features were both unstable
   in 2021, so even a correctly dated graph needs `-Z unstable-options` and
   CARGO_UNSTABLE_NAMESPACED_FEATURES for cargo to read it. Both are safe because every commit
   in this range pins a nightly.

   Two things that look like they should work do not, and are worth not retrying. A
   `[patch.crates-io]` entry redirecting a crate to a git tag is silently ignored - cargo
   reports "was not used in the crate graph" and resolves from the registry anyway. And
   `cargo update --precise` cannot repair a resolution failure, because it only rewrites a
   lockfile that resolution has already produced.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The interpreter is the single version this file fixes; everything else per-PR is read from
# the checked-out tree. See point 4 in the module docstring for why it is not the 3.6 the
# workflows name.
PYTHON_IMAGE = "python:3.8-slim-bullseye"

# Where the toolchain date is read from, and the pattern that finds it. Kept next to each
# other so a workflow rename cannot leave a stale regex behind.
TOOLCHAIN_WORKFLOW = ".github/workflows/build-test.yaml"
TOOLCHAIN_PATTERN = "nightly-[0-9]{4}-[0-9]{2}-[0-9]{2}"

# The repo's own dependency file, present at all five commits. Reading it rather than listing
# packages here is what keeps maturin 0.8.1 / 0.9.4 / 0.11.0 correct per PR.
REQUIREMENTS = "py-polars/build.requirements.txt"

# `pytest tests` is `tasks.sh build-run-tests`, the project's own target. `-v -rA` names every
# test including the passing ones (-rA's short summary is the only place a PASS gets its full
# node id). `-p no:cacheprovider` stops .pytest_cache appearing in the tree between stages.
PYTEST_CMD = "pytest tests -v -rA --tb=no -p no:cacheprovider"

# The rebuild every graded stage repeats, because each stage's patch changes Rust source that
# must be recompiled before the Python tests can import it.
MATURIN_BUILD = "maturin develop"

# The floor the repair below installs, and the two probes that decide whether it is needed.
# Held as constants rather than written inline, the way TOOLCHAIN_PATTERN is, so their regex
# escapes are not doubled a second time inside the prepare.sh f-string.
#
# 0.9.4 and not something newer: it is the oldest maturin that reads the field, and it is
# already the version #504 and #519 pin, so the repair never introduces a maturin this range
# does not otherwise use.
MATURIN_FLOOR = "0.9.4"
MATURIN_NAME_PROBE = (
    r"sed -n '/^\[package\.metadata\.maturin\]/,/^\[/p' Cargo.toml"
    r" | grep -qE '^[[:space:]]*name[[:space:]]*='"
)
MATURIN_IS_0_8_PROBE = r"maturin --version 2>/dev/null | grep -qE '(^|[^0-9.])0\.8\.'"

# The lockfile this tree does not carry is built at image-build time and then walked back to
# the day the commit was made. See point 5 in the module docstring for why that is necessary
# and why nothing simpler works.
#
# The cutoff is read from the commit itself rather than written down here, and no crate version
# is recorded here either. Both would rot: a table of pins is wrong the moment one of those
# crates publishes again, and it says nothing about a sixth PR added to this range. A date is
# a fact about the commit and never changes.
# Fed to python3 on stdin from prepare.sh rather than shipped as its own file. Every image
# directory in this harness holds the same eight files - the patches, the four stage scripts,
# check_git_changes.sh and the Dockerfile - and a ninth would make these five images the odd
# ones out for no benefit, since nothing outside prepare.sh ever runs it.

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
    # A clean graph is too strict a test for one swap. When several crates need this treatment
    # at once - #519 needs crossbeam-utils, -epoch, -deque and -channel, every 0.8 release of
    # all four yanked - no single swap can make cargo happy, so demanding one reverts each of
    # them in turn and the sweep converges on nothing having moved. The swap did its job as
    # soon as cargo stops naming THIS crate and starts naming the next one; that is progress,
    # and the next pass deals with the crate it named. Anything else - cargo happy with the
    # crate but unable to resolve at all, or still blaming it - puts the file back.
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
            # A yanked release is invisible to the resolver, so --precise can never reach one.
            # That does not make it unusable: written into the lockfile it resolves like any
            # other version, and for ahash it is the only kind of candidate this range has -
            # every 0.7.x from 2021 has since been yanked, and the two that have not declare
            # `dep:` features the 2021 cargo cannot parse.
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
        # Sweeps repeat because each downgrade re-resolves the graph and can pull in packages
        # that were not in the lockfile when the pass started.
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

        # The sweep is finished and cargo still cannot read the graph, so one crate has no
        # usable release before the cutoff at all - every earlier one yanked, or the crate
        # simply younger than the commit. Only that crate is allowed past the cutoff, and only
        # by as little as possible. Doing this here rather than in the sweep is what keeps the
        # cutoff meaningful for the other two hundred crates.
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

# A template, not a finished string: `{repo}` is filled from self.pr.repo so the venv path
# is never spelled out. Exported by every script rather than declared as ENV in the base image, so the generated
# Dockerfile carries exactly one ENV instruction - the one DockerfileEnhancer injects.
#
#   PATH/VIRTUAL_ENV  maturin refuses to `develop` unless it can see a virtualenv; setting both
#                     is the non-interactive equivalent of sourcing activate.
#   RUSTFLAGS         `-C debuginfo=0` comes from the project's own workflow, where it keeps
#                     the compile inside the runner's memory budget. `-Z unstable-options` is
#                     what lets this toolchain accept edition 2021 dependencies: the edition
#                     was still unstable in early 2021 and stabilised only in 1.56, so without
#                     it any dependency on that edition stops the build with "edition 2021 is
#                     unstable and only available with -Z unstable-options".
#   CARGO_UNSTABLE_   The same story one level up, in the manifest parser. `dep:` namespaced
#   NAMESPACED_       features were also unstable in 2021, and the pinned cargo refuses to
#   FEATURES          read any manifest using them unless this is set. Both flags are safe
#                     here for the same reason: the tree pins a NIGHTLY at every commit in
#                     this range, and neither flag exists on stable.
#   CARGO_TERM_COLOR  parse_log strips ANSI anyway, but turning colour off at the source keeps
#                     the captured log diffable between stages.
SHELL_ENV = """\
export CI=true
export PY_COLORS=0
export CARGO_TERM_COLOR=never
# Cargo's dev profile turns incremental compilation on, and every graded stage recompiles the
# same tree after patching it - which is precisely the input that trips rustc's "found unstable
# fingerprints" ICE. #504 died that way: prepare.sh warmed the incremental cache, fix-run.sh
# patched polars-lazy, and rustc panicked in evaluate_obligation rather than compiling, so
# pytest never ran and the stage reported (0, 0, 0). Nothing here needs incremental rebuilds -
# each stage compiles once - so turning it off costs a little time and removes the whole class.
export CARGO_INCREMENTAL=0
export CARGO_UNSTABLE_NAMESPACED_FEATURES=true
export RUSTFLAGS="-C debuginfo=0 -Z unstable-options"
export VIRTUAL_ENV=/home/{repo}/py-polars/venv
export PATH="$VIRTUAL_ENV/bin:/root/.cargo/bin:$PATH"
# rustup reads `rust-toolchain` from the directory it is invoked in and that file OUTRANKS the
# default toolchain it was installed with. `py-polars/rust-toolchain` holds the bare word
# `nightly` at four of the five commits here, and `maturin develop` runs in py-polars/ - so
# without this, every stage silently fetches TODAY's nightly and compiles 2021 sources against
# a 2026 stdlib. #947 died exactly there: num-bigint 0.4.0 calls `Integer::div_ceil` with a
# reference, and a modern stdlib's inherent `u64::div_ceil` takes the call instead, E0308.
# RUSTUP_TOOLCHAIN outranks the file in turn; prepare.sh writes the date it read out of the
# workflow into this pin, so run/test/fix all compile with the rustc prepare.sh used.
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

RUN apt-get update && apt-get install -y --no-install-recommends \
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

        # The `# syntax=` directive on BASE_DOCKERFILE's first line is load-bearing, not
        # decoration. DockerfileEnhancer.enhance() returns content untouched when it is
        # already present (`if cls.SYNTAX_DIRECTIVE in raw: return raw`). Without it the
        # enhancer rewrites the clone line into clone + checkout + the full hardening block,
        # putting hardening back into the base image - exactly what must not happen. Opting
        # out means this file supplies what the enhancer would have added: the ARGs, the
        # proxy/cert env, the OCI labels and the CA symlinks (those are required for the MITM
        # proxy the evaluation harness runs behind).
        #
        # Nothing after the clone. No checkout, no rustup, no hardening - all per-PR, all in
        # the PR image.
        # config.need_clone is what `--need_clone false` sets. With a repo already on disk
        # under --repo_dir, COPY is not just faster - it is the only reliable option for a
        # repo this size: cloning polars inside the build failed with
        # `fatal: early EOF / index-pack failed` when the TLS stream dropped mid-transfer.
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

# No git operations in here. The PR Dockerfile has already done `git reset --hard` and
# `git checkout <base.sha>` before this script runs, and it runs the hardening block after.
# This call only asserts the tree it handed over is clean.
bash /home/check_git_changes.sh

# The one value that cannot be pinned in the config: `py-polars/rust-toolchain` says the bare
# word `nightly` at four of the five commits in this range and is absent at the fifth, so it
# is useless - a current nightly does not compile 2021 polars. The project's own workflow
# names the exact date, and it is read from the tree that was just checked out, which is what
# lets one file serve four different nightlies.
TOOLCHAIN=$(grep -oE '{TOOLCHAIN_PATTERN}' {TOOLCHAIN_WORKFLOW} | head -1)
if [ -z "$TOOLCHAIN" ]; then
  echo "prepare: no pinned nightly found in {TOOLCHAIN_WORKFLOW}" >&2
  exit 1
fi
echo "prepare: rust toolchain $TOOLCHAIN (read from {TOOLCHAIN_WORKFLOW})"

# Pin it before rustup runs, so nothing downstream can be diverted by py-polars/rust-toolchain.
# Written to a file as well as exported because the graded stages are separate processes; the
# SHELL_ENV block every one of them starts with reads it back.
echo "$TOOLCHAIN" > /home/rust-toolchain-pin
export RUSTUP_TOOLCHAIN="$TOOLCHAIN"

# `curl | sh` runs under /bin/sh, which has no pipefail, so the reported status is sh's and
# not curl's: a truncated download would exit 0 and leave a Rust-less image that only breaks
# later inside maturin. The `&&` chain on the version probes is the only guard at this layer.
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \\
    | sh -s -- -y --profile minimal --default-toolchain "$TOOLCHAIN" \\
  && /root/.cargo/bin/rustc --version \\
  && /root/.cargo/bin/cargo --version

# Build the lockfile the tree does not carry, then walk it back to the commit date. Nothing in
# this tree pins its dependencies - see point 5 in the module docstring - so without this every
# caret range resolves against today's crates.io and the 2021 toolchain is handed releases it
# cannot parse, cannot compile, or both.
#
# Ordering is the whole point. generate-lockfile has to run after rustup installs cargo, the
# walk-back has to run after generate-lockfile because `cargo update --precise` edits a
# lockfile that already exists, and both have to finish before the warm-up build below - that
# build is the step that would otherwise unpack a 2026 manifest and die.
#
# The lockfile is a working-tree change the repo does not carry at these commits, and nothing
# downstream objects to that. check_git_changes.sh has already run and passed further up, and
# the hardening block asserts HEAD, refs and remotes - never working-tree cleanliness - so it
# survives the `git checkout --detach` exactly the way the venv does.
cd /home/{repo}
cargo generate-lockfile
python3 - "$(git show -s --format=%cI HEAD | cut -c1-10)" /home/{repo} <<'PIN_TO_COMMIT_DATE_EOF'
{PIN_SCRIPT}
PIN_TO_COMMIT_DATE_EOF

# The venv sits where SHELL_ENV's VIRTUAL_ENV points. It is gitignored, so it survives the
# hardening block's `git checkout --detach` and is still there for every graded stage.
cd /home/{repo}/py-polars
python -m venv venv
python -m pip install --upgrade pip || true

# Dependencies come from the repo's own file, so maturin 0.8.1 / 0.9.4 / 0.11.0 each arrive
# with the commit that pins them without this config naming any of the three.
#
# `|| true` on this line only - deliberately NOT on rustup above, and NOT on the maturin
# build below. This step compiles native code, and a 2021 numpy or the jemalloc shim can fail
# to build a wheel on arm64 while the graded stages still run fine on amd64, so a failure here
# must not sink the image. rustup and maturin are the opposite: if either one fails, nothing
# downstream can work at all, so both stay hard gates.
python -m pip install -r /home/{repo}/{REQUIREMENTS} || true

# The test suite imports numpy, pandas and pyarrow, and no commit in this range lists all three
# anywhere pip can see. build.requirements.txt names none of them at #364 and #478, and the
# maturin `requires-dist` that does name numpy and pyarrow is metadata for building a wheel -
# maturin 0.8.1's `develop` never installs it. Left alone, every graded stage dies in pytest
# collection with ModuleNotFoundError and reports an empty suite, which Report.check() rejects
# without ever naming the cause.
#
# requires-dist is still read rather than hardcoded, because it carries the pyarrow==3.0 pin
# that four of these five commits depend on. #947 is the one that declares no requires-dist at
# all, so it falls back to the 4.x line, which is what was current in July 2021 when it landed.
REQUIRES_DIST=$(sed -n '/^\[package\.metadata\.maturin\]/,/^\[/p' /home/{repo}/py-polars/Cargo.toml | grep '^requires-dist' | cut -d'[' -f2 | tr -d '"]' | tr ',' ' ')
if [ -n "$REQUIRES_DIST" ]; then
  python -m pip install $REQUIRES_DIST || true
else
  python -m pip install numpy "pyarrow<5" || true
fi
python -m pip install pandas || true

# One commit's pin is wrong, and this repairs it. maturin learned `name` under
# [package.metadata.maturin] in 0.9.0, but #478's py-polars/Cargo.toml uses the field while its
# build.requirements.txt still pins 0.8.1 - so the pinned maturin cannot parse the manifest it
# is handed, and `maturin develop` dies before it compiles a line. #364 pins 0.8.1 as well but
# declares no `name`, and #504, #519 and #947 pin 0.9.4 or 0.11.0, so #478 is the only commit
# in this range where the manifest and the pin disagree.
#
# Detected from the manifest instead of matched on a PR number, so the repair covers any other
# commit carrying the same mismatch and stays inert everywhere else - a sixth PR added to this
# range still needs no edit here. Both probes must agree before anything is installed: the
# field has to be present AND the installed maturin has to be a 0.8.x that cannot read it.
if {MATURIN_NAME_PROBE} && {MATURIN_IS_0_8_PROBE}; then
  echo "prepare: manifest declares package.metadata.maturin.name, which maturin 0.8.x cannot read - upgrading to {MATURIN_FLOOR}"
  python -m pip install "maturin=={MATURIN_FLOOR}"
fi

# Warm ~/.cargo and target/ so the rebuild in each graded stage is incremental rather than a
# cold compile of the whole workspace three more times.
#
# A hard gate, despite being nominally a warm-up. Every graded stage runs this same command,
# so whatever breaks it here breaks all three identically and the instance reports (0,0,0),
# (0,0,0), (0,0,0) - which Report.check() rejects while saying nothing about the cause. This
# was not hypothetical: a `|| true` here let a completely non-functional image build green and
# hid a dead `cargo metadata` all the way to gen_report. Fail at build time instead, where the
# compiler error is still on screen.
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

cd /home/{repo}/py-polars

# Rebuilt in every stage, not just prepare.sh: each stage's patch changes Rust source that the
# Python tests import through the compiled extension.
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

cd /home/{repo}/py-polars
{MATURIN_BUILD}
{PYTEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # COPY lines are generated from files(), never hand-listed, so a file added there can
        # not be left uncopied - which would surface at build time as
        # `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # The hardening block, with the sha inlined rather than passed as ${BASE_COMMIT}, so
        # the generated Dockerfile is self-describing and cannot drift from the sha checked
        # out three lines above it.
        #
        # Order matters: checkout -> prepare.sh -> hardening. prepare.sh builds against the
        # base commit, and the hardening then re-detaches at that same sha, deletes every
        # other ref, expires the reflog and asserts the result - so nothing later than the
        # base commit is reachable from inside the image and a graded stage cannot read the
        # real fix out of git history.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# PYTEST_CMD runs from py-polars/, so pytest reports node ids relative to that directory
# (`tests/test_series.py::test_x`). test_patch_files holds repo-relative paths
# (`py-polars/tests/test_series.py`), and report.py's _test_name_matches_files compares the
# two by path. Prefixing here is what lets an n2p test be attributed to the patch that
# introduced it.
_ID_PREFIX = "py-polars/"

# pytest's `-rA` short-summary block - the authoritative source, and the only place a PASSING
# test is named with its full node id.
#
#   PASSED tests/test_series.py::test_from_pandas
#   FAILED tests/test_series.py::test_from_pandas - AssertionError
#
# The trailing ` - <reason>` is stripped: the reason text differs between stages (different
# assertion values), and a name carrying it would make one test read as two and manufacture
# transitions that never happened.
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<name>[^\s]+?)(?:\s+-\s+.*)?$"
)

# pytest's `-v` progress line, the fallback when a stage dies before the summary block.
#
#   tests/test_series.py::test_from_pandas PASSED [ 42%]
#
# The percentage is outside the capture on purpose - it shifts with the size of the suite,
# which is exactly what changes between the run and fix stages.
_PROGRESS_RE = re.compile(
    r"^(?P<name>[^\s]+::[^\s]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
)

_FAIL_STATUSES = {"FAILED", "ERROR"}
_SKIP_STATUSES = {"SKIPPED", "XFAIL", "XPASS"}


def parse_pytest_log(test_log: str) -> TestResult:
    """Read a py-polars pytest run into the three buckets report.py compares between stages.

    Both the `-rA` summary and the `-v` progress lines are read and unioned. They name a test
    identically, so a test present in both contributes one entry; reading both means a stage
    killed before it could print the summary still reports what it got through.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Strip ANSI FIRST. pytest colourises whenever it believes it is attached to a terminal,
    # and a single escape sequence in front of PASSED defeats every pattern below - the stage
    # then reports 0/0/0 and Report.check() rejects it at rule 1 with no clue why.
    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _SUMMARY_RE.match(line) or _PROGRESS_RE.match(line)
        if not match:
            continue

        name = match.group("name")
        # A summary line for a collection error names a FILE, not a node id. Keeping it is
        # correct - a file that fails to import is a real regression - but a bare word from
        # some other log line is not, so require something that looks like a path or node id.
        if "::" not in name and "/" not in name:
            continue
        name = _ID_PREFIX + name

        status = match.group("status")
        # Worst result wins, applied as we go rather than as a post-pass, so a test reported
        # twice can never end up in two buckets at once. TestResult.__post_init__ raises
        # ValueError on any overlap, which would abort the whole instance rather than
        # mis-grade it.
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


# Alias registration under the bare `pola-rs/polars` key, in addition to the interval key
# above.
#
# gen_report.run_dataset() calls collect_report_tasks() BEFORE the raw dataset is loaded
# (gen_report.py:585-586), so the number_interval lookup at gen_report.py:373 finds nothing
# and the key it builds is `pola-rs/polars`, not `pola-rs/polars_0_to_1000`. The build stage
# reads the interval correctly; only the report stage does not. run_evaluation() already
# guards against exactly this with a `_ = self.dataset` pre-load (gen_report.py:571) - the
# same line is simply missing from run_dataset().
#
# Registering the bare key makes the report stage resolve without touching the harness.
#
# The cost, stated plainly: this key now catches every polars PR that arrives without a
# number_interval, including ones above #1000 that belong to polars_0_to_12255 or
# polars_12256_to_99999. Today such a row fails loudly as "not registered", which is a
# clearer signal than being graded by the wrong era's config. Datasets for the later eras
# must therefore carry their number_interval - which they already do, since those eras have
# no bare-key registration of their own to fall back on.
Instance.register("pola-rs", "polars")(POLARS_0_TO_1000)
