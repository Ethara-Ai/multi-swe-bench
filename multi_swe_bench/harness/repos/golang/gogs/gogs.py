import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Robustly apply the gold test/fix patches for a *bundled* instance.
#
# A bundle's patches are aggregated from many PRs and can carry defects that make
# a plain `git apply` abort before any test runs — which the harness then records
# as a bogus "no test results"/(0,0,0) fix stage (i.e. an unresolvable instance).
# Two such defects were observed in the gogs bundles:
#   1. Binary files serialized as "Binary files a/x and b/x differ" *stubs* with
#      NO embedded data (the patch was generated without `git diff --binary`).
#      These can never apply, but they are vendored assets (e.g. *.gif) that do
#      not affect compiling or running the Go tests, so we strip those blocks.
#   2. The same new file added by BOTH the test patch and the fix patch (vendored
#      files mis-split into both halves), causing "already exists" on the
#      combined apply. We drop those paths from the fix patch.
# We then apply with `--3way` for resilience. If apply STILL fails we exit
# non-zero so the failure surfaces honestly and is never silently masked.
#
# NOTE: only vendored assets / duplicate-adds are removed; every real source and
# test change is applied unchanged, so the pass/fail verdict stays faithful.
_ROBUST_APPLY_SH = r"""#!/bin/bash
set -uo pipefail

repo="$1"; test_patch="$2"; fix_patch="${3:-}"

_strip_binary() {   # <in> <out> : drop any "diff --git" block containing a binary-stub line
    awk '
        function flush(){ if (block != "" && !isbin) printf "%s", block; block=""; isbin=0 }
        /^diff --git /             { flush() }
        /^Binary files .* differ$/ { isbin=1 }
                                   { block = block $0 ORS }
        END                        { flush() }
    ' "$1" > "$2"
}

_drop_paths() {     # <in> <out> <pathlist> : drop blocks whose new path is listed
    awk -v listf="$3" '
        function flush(){ if (block != "" && !(path in drop)) printf "%s", block; block=""; path="" }
        BEGIN { while ((getline l < listf) > 0) drop[l]=1 }
        /^diff --git / { flush(); path=$0; sub(/^diff --git a\//,"",path); sub(/ b\/.*/,"",path) }
                       { block = block $0 ORS }
        END            { flush() }
    ' "$1" > "$2"
}

cd "$repo"

# Docker image layers reset file mtimes/inodes, so git's stat cache is stale and
# perfectly clean files look "modified". Any index-aware apply mode (--index /
# --3way) then aborts with "<file>: does not match index" WITHOUT applying
# anything. Re-sync the worktree and refresh the index before touching patches.
git reset --hard >/dev/null 2>&1 || true
git update-index --refresh >/dev/null 2>&1 || true

_strip_binary "$test_patch" /tmp/_test.patch

if [ -z "$fix_patch" ]; then
    set -- /tmp/_test.patch
else
    grep '^diff --git ' /tmp/_test.patch | sed -E 's#^diff --git a/(.*) b/.*#\1#' | sort -u > /tmp/_testfiles.txt
    _strip_binary "$fix_patch" /tmp/_fix.b.patch
    _drop_paths   /tmp/_fix.b.patch /tmp/_fix.patch /tmp/_testfiles.txt
    set -- /tmp/_test.patch /tmp/_fix.patch
fi

# Plain apply FIRST — this is the original, index-independent behaviour and is
# what already worked for every healthy bundle. Only if it fails do we fall back
# to --3way (now safe, thanks to the index refresh above). This guarantees we can
# never do worse than the pre-existing apply.
git apply --whitespace=nowarn "$@" || git apply --3way --whitespace=nowarn "$@"
"""


class GogsImageBase(Image):
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
        return "golang:1.9"

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
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # SHARED base image: built ONCE and reused by every gogs PR (image_tag ==
        # "base"). It does NOT pass or check out a per-PR BASE_COMMIT — it clones
        # the repo at HEAD via ${REPO_URL} and installs only the COMMON
        # dependencies (system libpam-dev; the Go deps are vendored in the repo
        # and arrive with the clone). Each per-PR image (GogsImageDefault) builds
        # FROM this base, checks out its own BASE_COMMIT (SHA), and warms the
        # commit-specific build in prepare.sh — reusing everything above from here.
        #
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this Dockerfile unchanged (its first guard is
        # `if SYNTAX_DIRECTIVE in raw: return raw`), so the ARG/ENV/LABEL infra is
        # inlined here and the shared base keeps FULL git history — it is never
        # hardened to a single commit, which would break sibling PRs whose base
        # sha differs. Anti-cheat hardening happens per-PR in GogsImageDefault.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8
LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="ethara.ai"

{self.global_env}

WORKDIR /home/

RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && sed -i '/stretch-updates/d' /etc/apt/sources.list && apt-get update && apt-get install -y --allow-unauthenticated libpam-dev && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /gopath/src/github.com/gogs

{code}

RUN ln -sf /home/{self.pr.repo} /gopath/src/github.com/gogs/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class GogsImageDefault(Image):
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
        return GogsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""".format(),
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

cd /gopath/src/github.com/gogs/{pr.repo}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /gopath/src/github.com/gogs/{pr.repo}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "robust_apply.sh",
                _ROBUST_APPLY_SH,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

bash /home/robust_apply.sh /home/{pr.repo} /home/test.patch
cd /gopath/src/github.com/gogs/{pr.repo}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

bash /home/robust_apply.sh /home/{pr.repo} /home/test.patch /home/fix.patch
cd /gopath/src/github.com/gogs/{pr.repo}
GOPATH=/gopath GO111MODULE=off go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Per-PR anti-cheat hardening. This is the image the model is evaluated
        # in, so after prepare.sh has checked out and warmed BASE_COMMIT we strip
        # every ref/remote and GC unreachable objects: the gold fix/merge commit
        # and the `origin` remote are removed, so a solution cannot recover the
        # fix via `git log`, `git show <future-sha>`, or `git fetch`. BASE_COMMIT
        # is exported as ENV so Image._HARDENING_BLOCK (which references
        # ${BASE_COMMIT}) resolves to THIS PR's base sha.
        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{prepare_commands}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]

"""


@Instance.register("gogs", "gogs")
class Gogs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GogsImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

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


# ---------------------------------------------------------------------------
# number_interval routing
#
# Instance.create() resolves an instance via f"{org}/{number_interval}" when a
# dataset record carries a `number_interval` field. Each bundle's number_interval
# is the dash-joined, SORTED `prs_in_bundle` (e.g. "146-147-150-155-157") — an
# explicit list of the PR numbers actually in the bundle, NOT a contiguous range
# like "146-157". Register every modern-era (github.com/gogs) gogs bundle so these records route to
# Gogs.
# ---------------------------------------------------------------------------
_NUMBER_INTERVALS = [
    "4688-5391-5481-5485-5492-5493-5498-5507-5508-5521-5534-5537",  # base PR 4688 (12 PRs)
    "5584-5623-5628-5629-5638-5641-5650-5678-5689-5724-5750-5760-5766-5773-5774",  # base PR 5584 (15 PRs)
    "5219-5221-5234-5239-5270-5276-5293-5315-5319-5322-5325-5332-5338-5346-5365-5381-5388-5390-5393-5404-5411",  # base PR 5219 (21 PRs)
    "5317-5340-5568",  # base PR 5317 (3 PRs)
    "5786-5788-5793-5795-5803-5804-5806-5812-5820-5827-5835-5836-5837-5840-5843-5850-5851-5856-5857-5865-5883-5888-5890-5895-5900-5901-5903-5907-5910-5911-5913-5920-5927-5928-5931-5932-5937-5940-5942-5943-5951-5952-5953-5954-5955-5957-5958-5959-5960-5963-5964-5965-5970-5973-5974-5975-5976-5978-5980-5981-5982-5983-5986-5987-5988-5989-5991-5996-5997-5998-6001-6002-6003-6004-6005-6006-6008-6011-6013-6014-6015-6016-6017-6019-6020-6021-6022-6024-6025-6026-6027-6030-6031-6032-6033-6035-6036-6037-6039-6041-6042-6043-6044-6045-6046-6047-6048-6049-6051-6055-6056-6058-6059-6060-6061-6062-6066-6068-6069-6070-6071-6072-6073-6075-6076-6077-6079-6080-6081-6083-6084-6085-6086-6087-6088-6090-6092-6094-6095-6097-6099-6100-6101-6102-6103-6105-6106-6107-6112-6116-6119-6120-6122-6123-6128-6135-6140-6142-6143-6144-6147-6153-6156-6158-6166-6170-6171-6174-6181-6183-6188-6191-6194-6199-6200-6201-6202-6203-6207-6216-6237-6246-6251-6253-6254-6255-6256-6257-6260-6261-6263-6264",  # base PR 5786 (192 PRs)
]
for _ni in _NUMBER_INTERVALS:
    Instance.register("gogs", _ni)(Gogs)
