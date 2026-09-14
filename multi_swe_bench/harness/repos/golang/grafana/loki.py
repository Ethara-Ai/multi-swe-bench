import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# grafana/loki — log aggregation system (Go monorepo).
#
# Discovery (dataset analysis, guard-safe subset of Dataset/loki_real.jsonl):
#  - Single era. go.mod `go` directive spans 1.17 .. 1.25.7 across base commits;
#    Go is backward compatible, so one golang:1.25 base (+ GOTOOLCHAIN=auto for
#    any `toolchain` line newer than the image) builds every base.
#  - The root module ships a vendor/ tree at every base commit, so tests run in
#    the default -mod=vendor mode (no GOFLAGS=-mod=mod, no network needed for
#    root-module packages).
#  - Nested modules exist (operator/, operator/apis/loki, tools/lambda-promtail,
#    pkg/push ...). A package inside one cannot be tested from the repo root, so
#    each package is tested from its nearest go.mod directory; nested modules
#    are not vendored, so prepare.sh warms their module cache at build time.
#  - `go test ./...` over the whole monorepo takes hours; tests are scoped to the
#    packages that own the `*_test.go` files in the test_patch (median 3 pkgs).
#  - Only top-level tests are recorded. Table-driven subtests carry generated,
#    order-assigned names (`TestX/#00`) that are not stable identifiers.


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        for raw in (parts[2], parts[3]):
            path = raw[2:] if raw[:2] in ("a/", "b/") else raw
            if not path.endswith("_test.go"):
                continue
            segs = path.split("/")
            if "vendor" in segs or "testdata" in segs:
                continue
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks so `git apply` never aborts on a binary hunk
    with no full-index line. Binary hunks touch no Go source."""
    sections = re.split(r"(?=^diff --git )", patch or "", flags=re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )


class LokiImageBase(Image):
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
        return "golang:1.25-bookworm"

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

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` opts this shared base out of the DockerfileEnhancer, which
        # would otherwise inject checkout + gc-prune into the shared base and
        # break every other PR's base.sha. Full history is kept here; per-PR
        # literal-sha hardening runs in LokiImageDefault.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

{DockerfileEnhancer._PROXY_ARGS}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
# Newer `toolchain` lines in a base commit's go.mod are fetched on demand.
ENV GOTOOLCHAIN=auto
# Older bases vendor go4.org/unsafe/assume-no-moving-gc, whose init() panics on
# any runtime it was not updated for unless this equals the running go1.X.
# Go 1.25's GC is non-moving, so asserting it is accurate.
ENV ASSUME_NO_MOVING_GC_UNSAFE_RISK_IT_WITH=go1.25

{DockerfileEnhancer._ENV_BLOCK}

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class LokiImageDefault(Image):
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
        return LokiImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        check_git = """#!/bin/bash
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

        # Resolves a package directory to its nearest enclosing module root.
        common = """#!/bin/bash
# Nearest directory (walking up from $1) that holds a go.mod; "." = repo root.
mod_root() {
  local d="$1"
  while [ "$d" != "." ] && [ ! -f "$d/go.mod" ]; do
    d=$(dirname "$d")
  done
  echo "$d"
}
"""

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh
source /home/common.sh
go version

# The root module is vendored. Nested modules are not, so warm their module
# cache here (build time has network) for every module owning a test package.
for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  root=$(mod_root "$pkg")
  [ "$root" = "." ] && continue
  (cd "$root" && go mod download) || true
done
""".replace("__REPO__", repo).replace("__SHA__", sha).replace("__PKGS__", pkg_list)

        # Per-package `go test` from the package's module root. -vet=off keeps
        # vet-only failures under the newer toolchain from masking test results.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
source /home/common.sh

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  ls "$pkg"/*.go >/dev/null 2>&1 || continue
  root=$(mod_root "$pkg")
  if [ "$root" = "." ]; then
    rel="./$pkg/"
  elif [ "$root" = "$pkg" ]; then
    rel="./"
  else
    rel="./${pkg#"$root"/}/"
  fi
  echo "### LOKIPKG: $pkg ###"
  (cd "$root" && go test -v -count=1 -vet=off -timeout=20m "$rel" 2>&1) || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(".", "check_git_changes.sh", check_git),
            File(".", "common.sh", common),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Per-PR anti-cheat hardening at the LITERAL base.sha: strips every
        # other ref/reflog so the fix commit is unreachable from git history.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("grafana", "loki")
class Loki(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LokiImageDefault(self.pr, self._config)

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
        clean = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` result lines, fenced per package by `### LOKIPKG: <pkg> ###`
        # so ids stay unique across packages. Subtest lines (name contains "/")
        # are skipped: their names are generated and order-assigned; the parent
        # test's own line already reflects any subtest failure.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### LOKIPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if "/" in name:
                continue
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        # Disjoint sets: failed > skipped > passed.
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
# number_interval bundle routing (prs_in_bundle dash-joined) -- PIPELINE §11b
# ---------------------------------------------------------------------------
# Single-era repo -> every bundle key routes to the one Loki class. The original
# "grafana/loki" registration above is kept.
_BUNDLE_NIS_LOKI = [
    "10526-10545-10556-10558-10567-10587-10590-10592-10597",
    "10531-10896-10926-10931-10955",
    "11131-11188-11246-11267-11276-11280-11285-11287-11291",
    "11602-11655-11658-11707-11715-11743-11761",
    "11928-12097-12129-12130-12134-12135-12139-12143-12146-12148-12150-12151-12152",
    "19579-19633-19640-19646-19648-19657-19659-19661-19663-19667-19669-19670",
    "20120-20213-20216-20238-20250-20257-20262-20266-20273-20284-20296-20311-20317-20318-20328-20368-20398-20421-20431-20435-20439-20443-20445-20463-20514",
    "20754-20855-21083-21123-21132",
    "20768-20775-20779-20870-20919",
    "20848-20857-20859-20866-20868-20871-20873-20920",
    "4673-4676-4678-4689-4690-4691-4692-4698",
    "5920-6483-7010-7116-7122-7145-7162-7172-7174-7183-7187-7190-7191-7201",
    "8292-8314-8341-8348",
    "10411-10733-10812-10849-10857",
    "10449-10454-10457-10463-10534-10536-10539-10546-10550-10552-10559-10564-10566-10572-10573",
    "10519-10599-10709-10717-10762-10784",
    "10527-15543-15790-15800-15804-15805-15807-15808-15814-15815-15823-15824-15828-15831",
    "10605-10858",
    "10620-10654-10665-10666-10667-10672-10675-10676-10677-10684-10685-10686-10687-10689-10694-10698-10700-10704-10710-10713-10722",
    "10622-10623-10624-10625-10734-10754-10768-10780-10795-10804-10805-10807-10808-10815",
    "10752-10755-10782-10794-10886-10894-10906-10907-10910-10911-10914",
    "11241-11511-11592-11649-11666-11670-11677-11682-11685",
    "11456-11594-11603-11627-11644-11656-11659-11664-11667-11674-11676-11714-11745-11758-11760",
    "14088-14194-14195-14200-14205-14219-14220-14224-14229-14232-14236-14251-14265-14315-14335-14363-14380-14386-14422-14429-14450-14454-14459-14468-14471-14474-14479-14490-14493-14502-14518",
    "14417-15829-16024-16035-16054-16056-16075-16091-16103-16104-16105-16107-16108-16109-16110-16111-16112-16114-16115-16117-16123-16124-16125-16126-16130-16133-16139-16140-16143-16144-16146-16147-16149-16152-16155-16156-16157-16158-16159-16165-16166-16167-16168-16171-16172-16173-16177-16180-16184-16189-16195-16198-16204-16207-16210-16211",
    "17957-19328-19534-19572-19590-19609-19656-19660-19672-19674-19675-19676-19679-19681-19688-19690-19691-19693-19695",
    "18698-19095-19097-19136-19205-19252-19258-19301-19319-19324-19325-19329-19330-19331-19335-19336-19337-19338-19339-19340-19342-19343-19345-19348-19351-19352-19353-19355-19359-19360-19362-19363-19364-19366-19367-19368-19369-19370-19371-19372-19373-19374-19376-19378",
    "20369-20529-20538-20555-20576-20586-20590-20613-20649-20667-20679-20684-20717",
    "7749-7867-7929-7947-8101-8114-8141-8262-8273-8280-8282-8286-8287",
    "8818-9390-9402-9403-9453-9605-9631-9637-9695-9744-9944-9946-9965",
]
for _ni in _BUNDLE_NIS_LOKI:
    Instance.register("grafana", _ni)(Loki)
