"""Helpers for skylot/jadx config variants.

The upstream lht release-line patches often reference binary files via the
short-form `Binary files a/X and b/Y differ` header (no embedded blob). `git
apply` cannot apply such hunks, so we strip them from the patch text and
recover the final file state directly from the repo's end-of-range git tag
(derived from `base.label` of the form `<start>..<end>`).
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Keeps text-only diff sections intact so `git apply` succeeds. Binary
    content is restored separately in fix-run.sh/test-run.sh by checking
    out the file at the end-of-range git tag.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('diff --git'):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith('diff --git'):
                if lines[i].startswith('GIT binary patch') or lines[i].startswith('Binary files'):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    out = '\n'.join(result)
    if out and not out.endswith('\n'):
        out += '\n'
    return out


def _extract_binary_ops(patch_content: str) -> list:
    """Return list of (action, path) for binary diff sections in a patch.

    action is one of 'delete', 'add', 'modify'. path is the b/ path (new
    path) except for 'delete' where it's the a/ path.
    """
    if not patch_content:
        return []

    lines = patch_content.split('\n')
    ops = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].startswith('diff --git'):
            header = lines[i]
            section_start = i
            i += 1
            is_binary = False
            new_file = False
            deleted_file = False
            while i < n and not lines[i].startswith('diff --git'):
                ln = lines[i]
                if ln.startswith('GIT binary patch') or ln.startswith('Binary files'):
                    is_binary = True
                elif ln.startswith('new file mode'):
                    new_file = True
                elif ln.startswith('deleted file mode'):
                    deleted_file = True
                i += 1
            if is_binary:
                # Parse `diff --git a/foo b/bar` -> extract paths
                # Format: diff --git a/<path> b/<path>
                parts = header.split(' ')
                a_path = None
                b_path = None
                for p in parts:
                    if p.startswith('a/'):
                        a_path = p[2:]
                    elif p.startswith('b/'):
                        b_path = p[2:]
                if deleted_file:
                    if a_path:
                        ops.append(('delete', a_path))
                elif new_file:
                    if b_path:
                        ops.append(('add', b_path))
                else:
                    if b_path:
                        ops.append(('modify', b_path))
        else:
            i += 1
    return ops


def _extract_end_tag(base_label: str) -> str:
    """Given base.label `<start>..<end>`, return `<end>`. Empty string if not parseable."""
    if not base_label or '..' not in base_label:
        return ''
    return base_label.split('..', 1)[1]


def _binary_extract_shell(fix_patch: str, test_patch: str, end_tag: str) -> str:
    """Build-time snippet: copy every binary blob referenced by either patch from
    end_tag into /home/.bin_fixtures, BEFORE the hardening pass runs.

    The shared Image.dockerfile() hardening block deletes all tags, removes the
    origin remote and prunes unreachable objects, so the end-of-range tag (and
    its blobs) are gone by eval time. This snippet runs inside prepare.sh (part
    of extra_setup(), which executes before hardening) while the tag still
    exists, stashing the blobs outside the git tree so they survive. Runs from
    the repo root; emits a bash snippet with no leading shebang.
    """
    if not end_tag:
        return '# no end_tag; skipping binary pre-extract\n'
    merged = {}
    for action, path in _extract_binary_ops(test_patch) + _extract_binary_ops(fix_patch):
        merged[path] = action
    # Only adds/modifies need a blob; deletes are handled at restore time.
    adds = [p for p, a in merged.items() if a != 'delete']
    if not adds:
        return '# no binary blobs to pre-extract\n'

    lines = [
        '# --- Pre-extract binary fixtures from end-of-range tag (pre-hardening) ---',
        f'_END_REF="{end_tag}"',
        'if git rev-parse --verify -q "$_END_REF" >/dev/null 2>&1; then',
        '    _HAVE_END=1',
        'else',
        '    git fetch --tags --quiet origin >/dev/null 2>&1 || true',
        '    if git rev-parse --verify -q "$_END_REF" >/dev/null 2>&1; then _HAVE_END=1; else _HAVE_END=0; fi',
        'fi',
        'if [ "$_HAVE_END" = "1" ]; then',
    ]
    for path in adds:
        q = path.replace("'", "'\\''")
        lines.append(f"    mkdir -p \"/home/.bin_fixtures/$(dirname '{q}')\" 2>/dev/null || true")
        lines.append(f"    git show \"$_END_REF:{q}\" > '/home/.bin_fixtures/{q}' 2>/dev/null || true")
    lines.append('fi')
    lines.append('# --- end pre-extract ---')
    return '\n'.join(lines) + '\n'


def _binary_restore_shell(fix_patch: str, test_patch: str, end_tag: str) -> str:
    """Eval-time snippet: restore binary files from the fixtures pre-extracted to
    /home/.bin_fixtures by _binary_extract_shell.

    The fixtures were stashed at build time (before the hardening pass removed
    git history), so this no longer touches git at all: adds/modifies are copied
    in from /home/.bin_fixtures, deletes are `rm`-ed. Combines both patches
    (pass fix_patch='' for the test-only variant). Returns a bash snippet with
    no leading shebang; runs from the repo root.
    """
    if not end_tag:
        return '# no end_tag; skipping binary restore\n'
    merged = {}
    for action, path in _extract_binary_ops(test_patch) + _extract_binary_ops(fix_patch):
        merged[path] = action
    if not merged:
        return '# no binary ops\n'

    lines = ['# --- Restore binary files from pre-extracted fixtures ---']
    for path, action in merged.items():
        # Escape for shell single-quoting
        q = path.replace("'", "'\\''")
        if action == 'delete':
            lines.append(f"rm -f '{q}' || true")
        else:
            lines.append(
                f"if [ -f '/home/.bin_fixtures/{q}' ]; then "
                f"mkdir -p \"$(dirname '{q}')\" 2>/dev/null || true; "
                f"cp '/home/.bin_fixtures/{q}' '{q}'; fi"
            )
    lines.append('# --- end binary restore ---')
    return '\n'.join(lines) + '\n'


# Standard apt packages for the shared base (same set the harness default image
# installs, minus what the JDK image already provides). git + ca-certificates are
# required for the HTTPS clone; the rest mirror the proven single-tier env.
_BASE_APT = "ca-certificates curl build-essential git gnupg make python3 sudo wget"


def parse_gradle_log(test_log: str) -> "TestResult":
    """Parse Gradle test output into a TestResult. Shared by every era's
    Instance (the format is identical across JDK 8/11/21)."""
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    passed_res = [re.compile(r"^(.+ > .+) PASSED$")]
    failed_res = [
        re.compile(r"^> Task :(\S+) FAILED$"),
        re.compile(r"^(.+ > .+) FAILED$"),
    ]
    skipped_res = [
        re.compile(r"^> Task :(\S+) SKIPPED$"),
        re.compile(r"^(.+ > .+) SKIPPED$"),
    ]

    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    for line in clean_log.splitlines():
        for r in passed_res:
            m = r.match(line)
            if m and m.group(1) not in failed_tests:
                passed_tests.add(m.group(1))
        for r in failed_res:
            m = r.match(line)
            if m:
                failed_tests.add(m.group(1))
                passed_tests.discard(m.group(1))
        for r in skipped_res:
            m = r.match(line)
            if m:
                skipped_tests.add(m.group(1))

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


# gradle.properties baked into the image (per RERUN_28_DETAILED_REPORT.md fix for
# halo-7239 / jadx-1464: "configure gradle in image -- daemon disabled, JVM limits
# set"). Lives at /root/.gradle so it applies to every gradle invocation; raises
# the heap/metaspace limits over the GRADLE_OPTS default and keeps the daemon off.
_GRADLE_PROPERTIES = """\
org.gradle.daemon=false
org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=1024m -Dfile.encoding=UTF-8
org.gradle.workers.max=2
org.gradle.parallel=false
org.gradle.configureondemand=false
"""


class JadxBaseImage(Image):
    """SINGLE SHARED base per JDK era (tag ``base-<jdk>``), built ONCE and reused
    as the FROM parent of every per-PR image in that era.

    It clones the FULL repo history and KEEPS ``origin`` (so each per-PR image
    can ``git checkout`` its own base commit and pre-extract binary fixtures from
    the end-of-range tag), and it warms the Gradle dependency cache so the common
    dependency download is NOT repeated for every PR. It carries NO per-PR base
    commit -- there is only the ``REPO_URL`` arg; it clones and installs deps.

    The leading ``# syntax`` directive makes ``DockerfileEnhancer.enhance()``
    return this Dockerfile VERBATIM (it early-returns when the directive is
    present). That is deliberate and does two things: (1) it stops the enhancer
    rewriting the clone into a ``git checkout ${BASE_COMMIT}`` + history-strip,
    which would pin this shared base to whichever PR built it first; (2) it omits
    the enhancer's proxy/cert/MITM infra block, so the base carries NO proxy or
    CA-cert config. Git-history hardening is applied PER-PR in ``JadxPRImage``,
    AFTER that PR's base commit is checked out -- keeping this ONE base reusable.

    Subclasses set ``JDK_IMAGE`` (e.g. ``eclipse-temurin:11``) and ``INIT_GRADLE``.
    """

    JDK_IMAGE = "eclipse-temurin:11"
    INIT_GRADLE = ""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self):
        return self.JDK_IMAGE

    def image_tag(self) -> str:
        # era-specific, NO pr number -> one shared base per JDK era
        return "base-" + self.JDK_IMAGE.replace(":", "-")

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        # init.gradle + gradle.properties live in the shared base; per-PR images
        # inherit them (gradle.properties: daemon off, raised JVM limits).
        return [
            File(".", "init.gradle", self.INIT_GRADLE),
            File(".", "gradle.properties", _GRADLE_PROPERTIES),
        ]

    def dockerfile(self) -> str:
        jdk = self.JDK_IMAGE
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {jdk}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8" \\
    GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.jvmargs=-Xmx2g"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_BASE_APT} && rm -rf /var/lib/apt/lists/*

COPY init.gradle /root/.gradle/init.gradle
COPY gradle.properties /root/.gradle/gradle.properties

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
# Warm the SHARED Gradle dependency cache from the repo's current default branch
# so the common deps live in this single base layer instead of being downloaded
# by every PR. `|| true`: the latest commit may need a newer JDK than this era's
# image (e.g. a JDK 8 base vs a master that needs JDK 11); the per-PR assemble
# re-resolves the exact deps at the checked-out base commit.
RUN ./gradlew --no-daemon assemble || true

CMD ["/bin/bash"]
"""


class JadxPRImage(Image):
    """Per-PR image: FROM the shared era base, check out THIS PR's base commit,
    pre-extract binary fixtures + warm per-SHA deps, then STRIP git history so the
    evaluated agent cannot recover the fix from git log/show. Only the PR-specific
    delta is applied on top of the reused shared base.

    Subclasses set ``BASE_CLASS`` to the matching ``JadxBaseImage`` subclass.
    """

    BASE_CLASS = JadxBaseImage

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
        # Returns an Image (the shared base) -> DockerfileEnhancer.enhance()
        # early-returns (dep is not a str) and leaves dockerfile() verbatim, so
        # the hardening below is applied by hand, anchored on HEAD. This is what
        # lets the base stay shared (and keeps proxy/cert infra out of the PR).
        return self.BASE_CLASS(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)
        end_tag = _extract_end_tag(getattr(self.pr.base, "label", "") or "")
        bin_extract = _binary_extract_shell(
            self.pr.fix_patch, self.pr.test_patch, end_tag
        )
        bin_restore_fix = _binary_restore_shell(
            self.pr.fix_patch, self.pr.test_patch, end_tag
        )
        bin_restore_test = _binary_restore_shell("", self.pr.test_patch, end_tag)
        repo = self.pr.repo
        sha = self.pr.base.sha

        prepare_sh = (
            "#!/bin/bash\n"
            "# FROM the shared era base (repo already cloned + deps warmed). Check\n"
            "# out THIS PR's base commit, pre-extract binary fixtures from the\n"
            "# end-of-range tag (origin/tags still present from the base), and warm\n"
            "# the per-SHA deps. The Dockerfile hardening pass strips history after.\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git reset --hard || true\n"
            f"git checkout {sha}\n"
            "\n"
            + bin_extract
            + "\n"
            "./gradlew --no-daemon assemble || true\n"
        )
        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "./gradlew --no-daemon test --continue\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            + bin_restore_test
            + "./gradlew --no-daemon test --continue\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            + bin_restore_fix
            + "./gradlew --no-daemon test --continue\n"
        )

        return [
            File(".", "fix.patch", f"{filtered_fix_patch}"),
            File(".", "test.patch", f"{filtered_test_patch}"),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Git-history hardening for the per-PR (agent) image, applied AFTER
        # prepare.sh has checked out this PR's base commit -- so the commit to
        # KEEP is the current HEAD (== this PR's base sha). The shared base
        # deliberately keeps full history + origin (see JadxBaseImage); this
        # strips the remote and every ref/commit not reachable from HEAD, so the
        # evaluated agent cannot recover the fix from git log/show/history.
        # Mirrors the harness _HARDENING_BLOCK, anchored on HEAD, and asserts
        # HEAD really is this PR's base commit.
        harden = f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
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
    fi"""

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

{harden}

CMD ["/bin/bash"]
"""
