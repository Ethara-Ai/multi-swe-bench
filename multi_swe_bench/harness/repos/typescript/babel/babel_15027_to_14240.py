"""babel/babel -- bundle 15027_to_14240 (10 PRs, TypeScript/JavaScript).

WHY THIS FILE EXISTS
--------------------
`babel_dispatcher._ERAS` only covers two verified ranges (4892-7357 and
7358-10852), so every PR in this bundle hit its guard:

    ValueError: babel/babel PR 14240 falls outside every verified era ...
    Babel's era ranges overlap ... Read package.json and .yarnrc.yml at this
    PR's base commit, confirm which era it matches, then add an interval.

That guard is right to refuse: babel's eras genuinely overlap by PR number
(era 4 is 10853-13727, era 5 is 11554-16101), so the number alone cannot decide
the toolchain -- the same number can be yarn-classic on one branch and
yarn-berry on another.

SO THE ERA WAS RESOLVED EMPIRICALLY, not guessed. package.json and .yarnrc.yml
were fetched at each of the ten base commits:

    PR        packageManager   nodeLinker
    14240     yarn@3.1.1       node-modules
    14295     yarn@3.6.2       node-modules
    14311     yarn@3.1.1       node-modules
    14341     yarn@3.1.1       node-modules
    14371     yarn@3.1.1       node-modules
    14456     yarn@3.1.1       node-modules
    14757     yarn@3.1.1       node-modules
    14886     yarn@3.1.1       node-modules
    14985     yarn@3.1.1       node-modules
    15027     yarn@3.1.1       node-modules

All ten are yarn 3.x with the node-modules linker -> Era 5, which is exactly
what `babel_yarn3_jest` (yarn@3.x + corepack + jest on node:18-bookworm-slim)
implements. No ambiguity for this bundle.

The subclass is therefore a pure re-registration: identical images, identical
tags, identical run scripts and parser. Only the registry key and
`number_interval` change. The dispatcher's `_ERAS` is deliberately NOT edited --
this bundle routes by its own interval, so the dispatcher's honest "I cannot
tell" guard stays intact for every other babel PR.
"""

from __future__ import annotations

import logging as _logging

from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.babel.babel_yarn3_jest import (
    BabelYarn3ImageBase,
    BabelYarn3ImageDefault,
    babel_yarn3_jest,
)

_log = _logging.getLogger(__name__)

# The ten PRs of this bundle, ascending. Read from the raw dataset, not typed by hand.
BUNDLE_PR_NUMBERS = [
    14240, 14295, 14311, 14341, 14371,
    14456, 14757, 14886, 14985, 15027,
]

# number_interval is the EXACT dash-joined PR list, never a range.
NUMBER_INTERVAL = "-".join(str(n) for n in BUNDLE_PR_NUMBERS)

# Human-facing bundle name: <hi>_to_<lo>.
ALIAS_KEY = "babel_15027_to_14240"

_BUNDLE_SET = set(BUNDLE_PR_NUMBERS)


# ---------------------------------------------------------------------------
# SHARED BASE (one image for all ten PRs)
#
# The stock yarn3 era builds a base PER PR -- its tag embeds the commit
# (`base-yarn3-jest-<sha8>`) and the base itself runs
# `git checkout ${BASE_COMMIT}` + the hardening scrub. For a ten-PR bundle that
# is ten clones and ten toolchain installs of a ~140-package monorepo.
#
# This bundle uses ONE shared base instead:
#   * base  -> toolchain + FULL-HISTORY clone, pinned to nothing
#   * PR    -> owns the checkout AND the prune
#
# Three things make that safe, and all three are load-bearing:
#   1. `# syntax=docker/dockerfile:1.6` stays on line 1. It is the
#      DockerfileEnhancer opt-out (image.py:317). Without it the enhancer
#      rewrites the clone into `git checkout ${BASE_COMMIT}` + scrub and silently
#      re-pins the shared image to whichever PR built it first.
#   2. `ARG BASE_COMMIT` is DECLARED but never referenced in the base -- it only
#      silences a BuildKit unused-arg warning. Consuming it is what would pin.
#   3. The clone is full history (no --depth, no --branch), so every PR in the
#      bundle can still reach its own base commit.
#
# The PR layer already carries Image._HARDENING_BLOCK, whose first command is
# `git checkout --detach "${BASE_COMMIT}"` and which then prunes and asserts the
# four invariants. So the pin and the scrub simply move there -- nothing is lost.
#
# ORDERING FIX: prepare.sh runs BEFORE the hardening block, so on an unpinned
# shared base `yarn install` would resolve against the default-branch tip -- the
# wrong dependency tree, and code from AFTER the fix. prepare.sh below therefore
# checks out the base commit itself, first thing. The hardening block's own
# `checkout --detach` then becomes a same-commit no-op that preserves the tree.
# ---------------------------------------------------------------------------

_SHARED_BASE_TAG = "base"


class Babel15027To14240ImageBase(BabelYarn3ImageBase):
    """One base for the whole bundle: toolchain + full-history clone, unpinned."""

    def image_tag(self) -> str:
        return _SHARED_BASE_TAG

    def workdir(self) -> str:
        return _SHARED_BASE_TAG

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org, repo = self.pr.org, self.pr.repo

        sections = [
            # MUST stay line 1 -- DockerfileEnhancer opt-out (image.py:317). Without
            # it the enhancer rewrites the clone below into a pinned checkout + scrub
            # and silently re-pins this SHARED base to one PR's commit.
            "# syntax=docker/dockerfile:1.6",
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
                "# Declared so BuildKit does not warn about an unused build-arg (the\n"
                "# harness passes it for every image). Deliberately NEVER referenced\n"
                "# below: consuming it is exactly what would pin this shared base.\n"
                "ARG BASE_COMMIT"
            ),
            (
                'ARG http_proxy=""\n'
                'ARG https_proxy=""\n'
                'ARG HTTP_PROXY=""\n'
                'ARG HTTPS_PROXY=""\n'
                'ARG no_proxy="localhost,127.0.0.1,::1"\n'
                'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
                'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
            ),
            (
                "ENV DEBIAN_FRONTEND=noninteractive \\\n"
                "    LANG=C.UTF-8 \\\n"
                "    LC_ALL=C.UTF-8 \\\n"
                "    TZ=UTC \\\n"
                "    CI=true \\\n"
                "    http_proxy=${http_proxy} \\\n"
                "    https_proxy=${https_proxy} \\\n"
                "    HTTP_PROXY=${HTTP_PROXY} \\\n"
                "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
                "    no_proxy=${no_proxy} \\\n"
                "    NO_PROXY=${NO_PROXY} \\\n"
                "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
                "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
                "    CURL_CA_BUNDLE=${CA_CERT_PATH}"
            ),
            (
                f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
                f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
                f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
                f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
            ),
            # CA-cert farm BEFORE any network RUN, so the very first HTTPS call
            # (apt, then git clone) already trusts an injected MITM CA.
            (
                "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
            ),
            "WORKDIR /home/",
            # Retries added after 8 of 10 builds died on a transient
            # "Could not resolve 'deb.debian.org'".
            (
                "RUN apt-get -o Acquire::Retries=5 update && \\\n"
                "    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\\n"
                "    ca-certificates git make python3 && \\\n"
                "    rm -rf /var/lib/apt/lists/*"
            ),
            "RUN git config --global --add safe.directory '*'",
            # Full history, pinned to NOTHING: each PR checks out its own commit in
            # its own layer, so every commit in the bundle must remain reachable.
            (
                "RUN set -eux; \\\n"
                "    git config --global core.compression 0; \\\n"
                "    git config --global http.postBuffer 1048576000; \\\n"
                "    for i in 1 2 3; do \\\n"
                f'        git clone "${{REPO_URL}}" /home/{repo} && break; \\\n'
                f"        rm -rf /home/{repo}; \\\n"
                "    done; \\\n"
                f"    git -C /home/{repo} rev-parse --verify HEAD; \\\n"
                f'    test "$(git -C /home/{repo} rev-list --all --count)" -gt 10000'
            ),
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class Babel15027To14240ImageDefault(BabelYarn3ImageDefault):
    """PR layer on the shared base. Owns the checkout AND the prune.

    The Dockerfile deliberately ENDS at `RUN bash /home/prepare.sh` -- no
    hardening RUN, no trailing WORKDIR, no CMD. Everything the PR layer must do
    therefore lives inside prepare.sh, in this order:

        reset/clean -> assert clean -> checkout base commit -> assert
          -> yarn install -> prune history + assert the four invariants

    Order matters twice over:
      * the checkout must precede `yarn install`, or dependencies resolve
        against the shared base's default-branch tip -- the wrong tree, and code
        from AFTER the fix;
      * the prune must FOLLOW the install, and must not use `git reset`,
        `git clean` or a path-scoped checkout, or it would silently undo work
        the install did. Only a commit-scoped `checkout --detach` is used, which
        repositions HEAD while preserving the tree.
    """

    def dependency(self) -> Optional[Image]:
        return Babel15027To14240ImageBase(self.pr, self._config)

    # The graded command. IDENTICAL in all three run scripts -- only the patch
    # application differs between them, which is what keeps f2p meaningful.
    #
    # --forceExit is the load-bearing addition. Measured on this bundle: the suite
    # (15,752 tests) finishes in 1-4 minutes, but jest then refuses to exit because
    # some test file leaves an open handle, so the Node event loop never drains.
    # Stages that hit it sat at 0% CPU for ~21 further minutes until killed:
    #
    #     pr-14295  1m + 1m + 1m   = 3 min   (jest happened to exit)
    #     pr-14240  25m + 24m + 25m = 74 min (jest hung on all three)
    #
    # ~90% of elapsed time was a timer running down, not computation. --forceExit
    # makes jest exit once testing completes. `timeout` is a second line of defence
    # so a genuine hang still fails loudly instead of blocking the queue forever.
    _TEST_CMD = (
        "timeout -k 60 1800 env BABEL_ENV=test yarn jest --verbose --ci --forceExit || true"
    )

    def files(self) -> list[File]:
        sha = self.pr.base.sha
        files = [
            f
            for f in super().files()
            if f.name not in ("prepare.sh", "run.sh", "test-run.sh", "fix-run.sh")
        ]
        repo = self.pr.repo
        body = (
            "corepack enable || true\n"
            "YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install || true\n"
            "make build || true\n"
            f"{self._TEST_CMD}\n"
        )
        files += [
            File(".", "run.sh", f"#!/bin/bash\nset -e\ncd /home/{repo}\n{body}"),
            File(
                ".",
                "test-run.sh",
                f"#!/bin/bash\nset -e\ncd /home/{repo}\n"
                f"git apply --whitespace=nowarn /home/test.patch\n{body}",
            ),
            File(
                ".",
                "fix-run.sh",
                f"#!/bin/bash\nset -e\ncd /home/{repo}\n"
                f"git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n{body}",
            ),
        ]
        files.append(
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail
cd /home/{self.pr.repo}

# ---- pin the tree ---------------------------------------------------------
# The shared base is unpinned (full history, no checkout), so THIS is where the
# code is fixed to this PR's base commit -- and it must happen before install.
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"

# ---- provision ------------------------------------------------------------
corepack enable || true
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install || true

# ---- hard gate ------------------------------------------------------------
# `yarn install || true` above tolerates a flaky network, but a SILENT install
# failure is far worse than a loud one: the image would build fine and then every
# graded stage would emit an empty jest report, which the harness reads as
# "0 failures" rather than "broken image" -- i.e. a bad instance that looks valid.
# So assert the install actually produced something usable, with no `|| true`.
test -d node_modules || {{ echo "FATAL: yarn install produced no node_modules" >&2; exit 1; }}
node -e "require('./package.json'); console.log('DEPS_OK')"

# ---- prune history --------------------------------------------------------
# Same guarantees as the harness hardening block, run here because the
# Dockerfile ends at this script. No reset/clean/path-checkout below: the tree
# is intentionally dirty after the install and must stay that way.
git checkout --detach "{sha}"
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""

# the four canonical invariants
test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD;
        git remote remove origin 2>/dev/null || true;
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
            | xargs -r -n1 git update-ref -d;
        git reflog expire --expire=now --all;
        git reflog expire --expire-unreachable=now --all;
        git gc --prune=now --aggressive;
        rm -f .git/objects/info/alternates;
    '
fi
echo "PREPARE_OK"
""",
            )
        )
        return files

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        sections = [
            f"FROM {image.image_name()}:{image.image_tag()}",
            (
                f'ARG BASE_COMMIT={self.pr.base.sha}\n'
                "ENV BASE_COMMIT=${BASE_COMMIT}"
            ),
            copy_commands,
            # Ends here on purpose: prepare.sh owns pin, install and prune.
            "RUN bash /home/prepare.sh",
        ]
        return "\n\n".join(sections) + "\n"


class Babel15027To14240(babel_yarn3_jest):
    """Bundle 15027_to_14240 on a SHARED base image."""

    def dependency(self) -> Optional[Image]:
        return Babel15027To14240ImageDefault(self.pr, self._config)


Instance.register("babel", NUMBER_INTERVAL)(Babel15027To14240)
Instance.register("babel", ALIAS_KEY)(Babel15027To14240)


# ---------------------------------------------------------------------------
# number_interval backfill by PR number.
#
# The raw dataset ships these ten PRs with no number_interval and no
# prs_in_bundle, so Instance.create() would fall through to "babel/babel" (the
# dispatcher) and hit the era guard. Filling the interval in routes them to the
# era that was empirically confirmed above.
#
# Deliberately narrow: org/repo must match, the number must be one of the ten,
# and an existing value is never overwritten.
# ---------------------------------------------------------------------------
if not getattr(PullRequest, "_babel_15027_to_14240_ni_shim", False):
    _prev_from_json = PullRequest.from_json.__func__

    def _from_json(cls, json_str):
        pr = _prev_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "babel"
                and getattr(pr, "repo", "") == "babel"
                and getattr(pr, "number", None) in _BUNDLE_SET
                and not (getattr(pr, "number_interval", "") or "")
            ):
                pr.number_interval = NUMBER_INTERVAL
                _log.debug(
                    "babel pr-%s: backfilled number_interval for bundle %s",
                    pr.number, ALIAS_KEY,
                )
        except Exception:
            _log.debug("number_interval backfill failed", exc_info=True)
        return pr

    PullRequest.from_json = classmethod(_from_json)
    PullRequest._babel_15027_to_14240_ni_shim = True
