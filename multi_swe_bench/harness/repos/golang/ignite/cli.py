"""ignite/cli (formerly tendermint/starport) config.

One shared base image plus one image per PR. The 24 rows span 2020-2024, so the
design is driven by four dataset properties, each handled in the generated
scripts rather than by touching the raw jsonl.

1. ``base.sha`` IS NOT THE PATCH PRE-IMAGE.
   Patches come from GitHub's compare endpoint, which diffs from the MERGE BASE
   of ``base.sha`` and the PR head. Several rows target long-lived feature
   branches (``helder/docs-review``, ``ltacker/evm-investigate``) where the two
   differ; at ``base.sha`` PR 310 fails with "already exists in working
   directory". ``prepare.sh`` fetches ``refs/pull/<N>/head`` and checks out
   ``git merge-base <base.sha> <head>``, where all 24 rows apply.

2. BINARY DIFFS CARRY NO PAYLOAD.
   The collector records ``Binary files a/x and b/x differ`` (logos, favicons,
   ``nodetime-*.tar.gz`` bundles, testdata tarballs) with no ``GIT binary
   patch`` body, so ``git apply`` rejects the whole patch. ``_undeliverable_paths``
   finds them from the patch text and every apply site passes ``--exclude``.

3. FOUR GO ERAS.
   ``go.mod`` asks for 1.14/1.15 (2020), 1.16 (2021), 1.18 (2022) and 1.20/1.21
   (2023-24). Building 2020 cosmos-sdk code with a current Go fails, and the
   2024 code needs >= 1.21. The base installs one toolchain per era and
   ``select_go.sh`` picks one from the ``go`` directive of the tree as it stands
   after the stage's patches land (a fix may bump it).

4. MOST TEST PATCHES ARE PARTLY END-TO-END.
   Files under ``integration/`` scaffold, build and serve whole blockchains
   (protoc, buf, nodetime, network); they cannot run in an isolated stage. Only
   the unit-test packages a test patch touches are run, computed in
   ``_unit_test_packages``. Rows whose test patch touches nothing else (634,
   1309, 1343, 1518, 2910, 3026, 3670, 4234) run zero tests and stay invalid,
   which is the honest outcome for them.

5. A PINNED DEPENDENCY COMMIT WAS ORPHANED UPSTREAM.
   PRs 387 and 447 require ``github.com/tendermint/spn`` at a pseudo-version
   whose commit is no longer on any branch. proxy.golang.org never cached it
   (404) and Go's direct mode cannot resolve it ("unknown revision"), but
   GitHub still serves it when fetched by full SHA. ``fix_lost_modules.sh``
   fetches it into ``/opt/lostmods`` and points ``go.mod`` at it with a
   ``replace``, only when the tree actually requires that version.

``-vet=off``: ``go test`` runs vet first, and newer vet rejects older code
("non-constant format string") so the package never builds.
"""

import os
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# One toolchain per go.mod era. Keys are the directory suffix under /usr/local.
_GO_VERSIONS = ("1.15.15", "1.16.15", "1.19.13", "1.21.13")

# (module, pseudo-version) -> full commit SHA, for dependency commits that only
# survive as orphans on GitHub. See point 5 of the module docstring.
_LOST_MODULES = {
    (
        "github.com/tendermint/spn",
        "v0.0.0-20201123100837-768588cdb659",
    ): "768588cdb65962b1e5bb4434e86bfd34dc5f8430",
}


def _split_diffs(patch: str) -> list[str]:
    """Split a patch into its per-file ``diff --git`` sections."""
    if not patch:
        return []
    return re.findall(r"(?:^diff --git .*?)(?=^diff --git |\Z)", patch, re.M | re.S)


def _diff_paths(diff: str) -> set[str]:
    """Every path a single file-diff refers to.

    The ``---``/``+++`` pair tolerates spaces in paths (``docs/04 Use cases/``),
    the ``diff --git`` header is the fallback for binary diffs that carry none.
    """
    out: set[str] = set()
    for m in re.finditer(r'^(?:---|\+\+\+) (?:"?[ab]/)?(.+?)"?$', diff, re.M):
        path = m.group(1).strip()
        if path and path != "/dev/null":
            out.add(path)
    header = diff.split("\n", 1)[0]
    m = re.match(r'diff --git "?a/(.+?)"? "?b/(.+?)"?$', header)
    if m:
        out.add(m.group(1))
        out.add(m.group(2))
    return out


def _undeliverable_paths(*patches: str) -> list[str]:
    """Paths whose diffs cannot be applied because their content is absent."""
    bad: set[str] = set()
    for patch in patches:
        for diff in _split_diffs(patch):
            if (
                "Binary files" in diff
                or "GIT binary patch" in diff
                or "�" in diff
            ):
                bad |= _diff_paths(diff)
    return sorted(bad)


def _unit_test_packages(test_patch: str) -> list[str]:
    """Package dirs of the ``_test.go`` files the test patch touches.

    Skips ``integration/`` (end-to-end, see module docstring) and anything under
    ``testdata/`` (fixtures, often separate modules).
    """
    pkgs: set[str] = set()
    for diff in _split_diffs(test_patch):
        for path in _diff_paths(diff):
            if not path.endswith("_test.go"):
                continue
            if path.startswith("integration/") or "/testdata/" in path:
                continue
            pkgs.add(os.path.dirname(path) or ".")
    return sorted(pkgs)


class IgniteCliImageBase(Image):
    """Shared toolchain image: every Go era plus a full-history clone.

    Shared by all 24 PRs, so it must NOT pin a commit. Emitting the BuildKit
    syntax directive opts out of ``DockerfileEnhancer``, which would otherwise
    check out ``${BASE_COMMIT}`` and strip all other history, leaving the other
    23 PRs' merge bases unreachable. This file therefore emits the proxy, cert
    and label infrastructure itself.
    """

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
        return "ubuntu:22.04"

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

        # The repo is 2.2 GB and TLS connections to github.com drop mid-transfer
        # ("RPC failed; curl 56 GnuTLS recv error", "HTTP/2 stream ... CANCEL"),
        # worst in multi-arch builds where both platforms clone at once. A
        # single full clone restarts from zero on every drop and never
        # finishes, so history is fetched in chunks: a shallow clone of every
        # branch, then --deepen steps until the repo is no longer shallow. Each
        # step is retried on its own, and the end state is the same full clone.
        # HTTP/1.1 sidesteps the HTTP/2 stream cancellations.
        if self.config.need_clone:
            code = f"""RUN set -eux; \\
    retry() {{ \\
        for i in 1 2 3 4 5 6 7 8; do \\
            "$@" && return 0; \\
            echo "retry: attempt $i failed: $*" >&2; \\
            sleep 15; \\
        done; \\
        return 1; \\
    }}; \\
    clone() {{ \\
        rm -rf /home/{self.pr.repo}; \\
        git -c http.version=HTTP/1.1 clone --no-single-branch --depth 50 "${{REPO_URL}}" /home/{self.pr.repo}; \\
    }}; \\
    retry clone; \\
    cd /home/{self.pr.repo}; \\
    while [ "$(git rev-parse --is-shallow-repository)" = "true" ]; do \\
        retry git -c http.version=HTTP/1.1 fetch --deepen=300 origin; \\
    done; \\
    git rev-list --all --count"""
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        go_versions = " ".join(_GO_VERSIONS)

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GOPATH=/go \\
    GOTOOLCHAIN=local \\
    GOFLAGS=-mod=mod \\
    PATH=/usr/local/go1.21/bin:/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

# gcc for the cgo deps (secp256k1, ledger); python3 for emit_results.py.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    build-essential \\
    python3 \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

# One toolchain per go.mod era, side by side as /usr/local/go<major.minor>.
# The toolchain a stage needs is only knowable after its patches land, so
# selection happens at eval time in select_go.sh.
#
# Downloaded to a file, not piped into tar: the connection drops mid-stream
# ("curl: (56) SSL_read: unexpected eof") and a pipe cannot be retried, so
# tar just sees a truncated archive. Each attempt is checked with gzip -t and
# discarded if incomplete.
RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    for v in {go_versions}; do \\
        f="/tmp/go${{v}}.tar.gz"; \\
        for i in 1 2 3 4 5; do \\
            curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 -o "$f" \\
                "https://go.dev/dl/go${{v}}.linux-${{arch}}.tar.gz" \\
              && gzip -t "$f" && break; \\
            rm -f "$f"; \\
            [ "$i" -lt 5 ] || exit 1; \\
            sleep 10; \\
        done; \\
        tar -C /usr/local -xzf "$f"; \\
        rm -f "$f"; \\
        mv /usr/local/go "/usr/local/go${{v%.*}}"; \\
        "/usr/local/go${{v%.*}}/bin/go" version; \\
    done; \\
    mkdir -p /go/bin

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class IgniteCliImageDefault(Image):
    """Per-PR image: resolves the merge base and pre-warms the Go caches."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return IgniteCliImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        number = self.pr.number

        # Rendered into every apply site so the three stages apply an identical
        # file set. Computed from the patch text alone - no git, no network.
        excludes = " ".join(
            f"--exclude='{path}'"
            for path in _undeliverable_paths(self.pr.test_patch, self.pr.fix_patch)
        )
        packages = " ".join(_unit_test_packages(self.pr.test_patch))
        lost_modules = "\n".join(
            f"{module} {version} {commit}"
            for (module, version), commit in _LOST_MODULES.items()
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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "apply_patches.sh",
                r"""#!/bin/bash
# Apply the recorded patches, skipping the diffs whose content the collector
# never captured (see _undeliverable_paths in the config module). Without the
# exclusions git apply rejects the whole patch over files it cannot write.
#
# Usage: apply_patches.sh <patch> [<patch>...]
set -e

EXCLUDES=([[EXCLUDES]])

for patch in "$@"; do
    if ! git apply --whitespace=nowarn "${EXCLUDES[@]}" "$patch"; then
        echo "Error: git apply failed for $patch" >&2
        exit 1
    fi
done
""".replace("[[EXCLUDES]]", excludes),
            ),
            File(
                ".",
                "select_go.sh",
                r"""#!/bin/bash
# Put the toolchain matching the tree's go.mod `go` directive first on PATH.
# Must run AFTER patches are applied, since a fix may bump the directive.
#
# Sourced, not executed - it exports into the caller's shell.

want="$(sed -n -E 's/^go ([0-9]+)\.([0-9]+).*/\2/p' go.mod 2>/dev/null | head -1)"
want="${want:-21}"

if   [ "$want" -le 15 ]; then GO_SERIES=1.15
elif [ "$want" -le 17 ]; then GO_SERIES=1.16
elif [ "$want" -le 19 ]; then GO_SERIES=1.19
else                          GO_SERIES=1.21
fi

export PATH="/usr/local/go${GO_SERIES}/bin:/go/bin:$PATH"
echo "select_go: go.mod wants 1.${want}, using $(go version)"
""",
            ),
            File(
                ".",
                "fix_lost_modules.sh",
                r"""#!/bin/bash
# Point go.mod at a local checkout for every required dependency commit that
# the module proxy cannot serve (see _LOST_MODULES in the config module).
# Run from the repo root after the stage's patches are applied. The checkout
# is fetched by full SHA, the only way GitHub still serves an orphaned commit;
# prepare.sh does this at image build time so graded stages find it on disk.
set -e

while read -r module version commit; do
    [ -n "$module" ] || continue
    grep -q "$module $version" go.mod 2>/dev/null || continue
    # No "@" in the path: go mod edit would read it as a version separator.
    dir="/opt/lostmods/$(echo "$module" | tr / _)-$commit"
    if [ ! -f "$dir/go.mod" ]; then
        rm -rf "$dir"
        git init -q "$dir"
        for i in 1 2 3 4 5; do
            git -C "$dir" fetch -q --depth 1 "https://$module" "$commit" && break
            [ "$i" -lt 5 ] || exit 1
            sleep 15
        done
        git -C "$dir" checkout -q FETCH_HEAD
    fi
    go mod edit "-replace=$module@$version=$dir"
    echo "fix_lost_modules: $module@$version -> $dir"
done <<'LOST'
[[LOST_MODULES]]
LOST
""".replace("[[LOST_MODULES]]", lost_modules),
            ),
            File(
                ".",
                "emit_results.py",
                r'''#!/usr/bin/env python3
"""Turn ``go test -json`` output into a readable log plus status lines.

Prints every test's own output first, then one ``STATUS pkg/TestName`` line per
test. Package paths are made module-relative because the module path itself
changes across this repo's history (allinbits/cosm -> tendermint/starport ->
ignite-hq/cli -> ignite/cli -> ignite/cli/v28), and a few fixes rename it.
"""

import json
import re
import sys

module = ""
try:
    with open("go.mod") as fh:
        m = re.search(r"^module\s+(\S+)", fh.read(), re.M)
        if m:
            module = m.group(1)
except OSError:
    pass


def rel(pkg):
    if module and pkg == module:
        return "."
    if module and pkg.startswith(module + "/"):
        return pkg[len(module) + 1 :]
    return pkg


status = {}
order = []
with open(sys.argv[1], errors="replace") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line.startswith("{"):
            print(line)
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            print(line)
            continue
        out = ev.get("Output")
        if out:
            sys.stdout.write(out)
        test = ev.get("Test")
        action = ev.get("Action")
        if not test or action not in ("pass", "fail", "skip"):
            continue
        tid = f"{rel(ev.get('Package', ''))}/{test}"
        if tid not in status:
            order.append(tid)
        status[tid] = {"pass": "PASSED", "fail": "FAILED", "skip": "SKIPPED"}[action]

print()
for tid in order:
    print(f"{status[tid]} {tid}")
''',
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
# Shared test driver for all three stages: the unit-test packages the test
# patch touches, run with the toolchain the tree asks for.
#
# A package the test patch creates does not exist at the baseline stage; it is
# dropped there rather than passed to `go test`, which would abort every
# package over one missing directory.
set -o pipefail

source /home/select_go.sh
bash /home/fix_lost_modules.sh

PKGS=()
for d in [[PACKAGES]]; do
    if ls "$d"/*.go >/dev/null 2>&1; then
        PKGS+=("./$d")
    fi
done

if [ ${#PKGS[@]} -eq 0 ]; then
    echo "run_tests: none of the target packages exist in this tree, no tests to run"
    exit 0
fi

echo "run_tests: ${PKGS[*]}"
go test -json -v -count=1 -vet=off -timeout 20m "${PKGS[@]}" > /tmp/gotest.json 2>&1 || true
python3 /home/emit_results.py /tmp/gotest.json
""".replace("[[PACKAGES]]", packages),
            ),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh

git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true

# base.sha is NOT the commit the patches were cut against. GitHub's compare API
# diffs from the merge base, so that is where the patches apply. Resolved here
# rather than baked in, so the image does not depend on the host clone's refs.
# Retried: DNS to github.com flakes mid-build ("Could not resolve host").
fetch() {
    for i in 1 2 3 4 5; do
        git fetch --no-tags origin "$@" && return 0
        echo "prepare: fetch $* failed (attempt $i), retrying" >&2
        sleep 15
    done
    return 1
}
fetch '+refs/pull/[[NUMBER]]/head:refs/msb/pr/[[NUMBER]]'
git cat-file -e '[[SHA]]^{commit}' 2>/dev/null || fetch [[SHA]]

MERGE_BASE="$(git merge-base [[SHA]] refs/msb/pr/[[NUMBER]])"
if [ -z "$MERGE_BASE" ]; then
    echo "prepare: could not resolve merge base for PR [[NUMBER]]" >&2
    exit 1
fi
echo "prepare: base.sha=[[SHA]] merge_base=$MERGE_BASE"
echo "$MERGE_BASE" > /home/merge_base.txt

git checkout -f "$MERGE_BASE"
bash /home/check_git_changes.sh

# Pre-warm the module and build caches for both the baseline and the fully
# patched tree, so the graded stages compile from cache instead of the network.
# Everything done to the tree here is reverted below; the hardening pass
# re-checks-out the merge base anyway.
warm() {
    (
        source /home/select_go.sh
        bash /home/fix_lost_modules.sh || true
        go mod download || true
        for d in [[PACKAGES]]; do
            if ls "$d"/*.go >/dev/null 2>&1; then
                go test -count=1 -vet=off -run '^$' "./$d" || true
            fi
        done
    )
}
warm
# The baseline warm-up may rewrite go.mod/go.sum (-mod=mod, lost-module
# replaces); reset so the patches' own go.mod hunks still apply.
git checkout -- .
git clean -xfd
bash /home/apply_patches.sh /home/test.patch /home/fix.patch || true
warm

git checkout -- .
git clean -xfd
bash /home/check_git_changes.sh
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha)
                .replace("[[NUMBER]]", str(number))
                .replace("[[PACKAGES]]", packages),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
bash /home/apply_patches.sh /home/test.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
bash /home/apply_patches.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The hardening pass re-pins HEAD and strips every other ref so the fix
        # cannot be read back out of git history. It re-reads the merge base
        # that prepare.sh resolved, since that - not base.sha - is the commit
        # this instance is actually anchored to. The pack limits keep the
        # aggressive repack of this 3k-commit history from being OOM-killed
        # (signal 9) when several PR images build in parallel in an 8 GB VM.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    MERGE_BASE="$(cat /home/merge_base.txt)"; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 256m; \\
    git config --local pack.deltaCacheSize 128m; \\
    git checkout --detach "$MERGE_BASE"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace refs/msb \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$MERGE_BASE"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace refs/msb)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("ignite", "cli")
class IgniteCli(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return IgniteCliImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Only the status lines emit_results.py prints; the raw `--- PASS:` lines
        # above them are unqualified by package and would collide across packages.
        line_re = re.compile(r"^(PASSED|FAILED|SKIPPED)\s+(\S+)\s*$")
        for raw in test_log.splitlines():
            m = line_re.match(raw.strip())
            if not m:
                continue
            status, tid = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(tid)
            elif status == "FAILED":
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
