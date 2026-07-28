import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LefthookImageBase(Image):
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
        # lefthook's go.mod `go` directive spans no-directive (Arkweid era,
        # PR 88..280) through 1.22/1.24 (mid era) to 1.26 (latest /v2 era).
        # Go is backward compatible, so the newest toolchain in the dataset
        # builds every era -> single base image.
        return "golang:1.26-bookworm"

    # Shared base: built ONCE per architecture, then every per-PR image FROMs
    # it. dockerfile() below emits the `# syntax=` directive, which opts this
    # file out of DockerfileEnhancer rewriting -- so the enhancer does NOT turn
    # the clone into `git checkout ${BASE_COMMIT}` + Image._HARDENING_BLOCK.
    # The base therefore stays a plain FULL-HISTORY clone, so every PR's base.sha
    # (Arkweid -> evilmartians -> /v2 eras, spread across branches) remains
    # reachable from the one shared image. The per-PR checkout + anti-cheat
    # hardening is done in LefthookImageDefault, where the literal base.sha is
    # known. One shared base + N eval images, not a base per PR.
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

        # GOFLAGS=-mod=mod and `git config safe.directory '*'` are lefthook
        # specific: the multi-era builds and root-owned git ops rely on them.
        #
        # The `# syntax` directive makes DockerfileEnhancer.enhance() return this
        # file verbatim (it early-returns when the directive is already present),
        # so the enhancer does NOT rewrite the clone into a BASE_COMMIT checkout
        # + hardening. Full history is kept on purpose -- see image_tag() above.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    GOTOOLCHAIN=auto \\
    GOFLAGS=-mod=mod

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates make gcc libc6-dev \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class LefthookImageDefault(Image):
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
        return LefthookImageBase(self.pr, self.config)

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

git config --global --add safe.directory '*'
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Pre-fetch module dependencies so the eval run is offline-friendly.
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the lefthook run/test/fix scripts.
#
# lefthook spans three eras (Arkweid/lefthook -> evilmartians/lefthook ->
# evilmartians/lefthook/v2) with a small but evolving package layout.
# Tests are scoped to the Go packages touched by the patches (matches the
# weaviate config). The repo's own CI excludes the `gen/` package, so we do
# the same. Integration/e2e suites under `tests/` need a configured git
# environment and lefthook binary on PATH, so those packages are skipped.

EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf --exclude=docs/*"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --3way $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --reject $EXCLUDES "$f" \\
    || true
}

# Print the unique Go package directories touched by test.patch + fix.patch
# that exist on disk and are not part of the infra-dependent suites.
# Written to be safe under `set -eo pipefail` (a no-match grep must not abort).
collect_pkgs() {
  local out d
  out=$(
    {
      git apply --numstat --whitespace=nowarn /home/test.patch 2>/dev/null
      git apply --numstat --whitespace=nowarn /home/fix.patch 2>/dev/null
    } \\
      | awk -F'\\t' '{print $NF}' \\
      | grep -E '\\.go$' \\
      | sed -E 's#/[^/]+$##' \\
      | grep -vE '^(gen|tests)(/|$)' \\
      | sort -u
  ) || true
  for d in $out; do
    if [ -n "$d" ] && [ -d "$d" ]; then
      echo "./$d"
    fi
  done
}

run_go_tests() {
  local pkgs
  pkgs=$(collect_pkgs)
  if [ -z "$pkgs" ]; then
    echo "No Go test packages touched by the patches; nothing to run."
    return 0
  fi
  echo "=== Running go test on touched packages ==="
  echo "$pkgs"
  echo "==========================================="
  go test -v -count=1 -timeout=900s $pkgs
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch
run_go_tests

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

        # Anti-cheat hardening runs HERE, in the per-PR layer -- not in the
        # shared base, which keeps full history. dependency() returns an Image,
        # so DockerfileEnhancer.enhance() returns this file raw; the hardening
        # block is therefore emitted explicitly, with the literal base.sha
        # substituted for ${BASE_COMMIT} (no BASE_COMMIT build arg is passed for
        # Image-dependency builds). prepare.sh has already checked out base.sha;
        # this detaches at it, then strips every ref/reflog/remote so the fix
        # commit and all later history are unreachable inside the container.
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

"""


@Instance.register("evilmartians", "lefthook")
class Lefthook(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LefthookImageDefault(self.pr, self._config)

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
        # `go test` is not colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        # A package summary line ("ok   <import-path>", "FAIL <import-path>",
        # "?    <import-path>") closes the block of tests printed above it.
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            pass_match = re_pass.match(line)
            if pass_match:
                pending_pass.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                pending_fail.add(fail_match.group(1))
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                pending_skip.add(skip_match.group(1))
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        flush("unknown")

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
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
# number_interval -- bundle member list, NOT a first-last range.
#
# A lefthook record's `prs_in_bundle` is the explicit set of PRs squashed into
# one instance, and those sets are sparse: PR #130 bundles
# [130, 142, 146, 148, 150, 154, 167, 168].  Writing that as the range
# "130-168" would claim ~50 PRs that are not in the bundle, so the interval is
# always the sorted members joined by "-":
#
#     prs_in_bundle [146, 147, 150, 155, 157]  ->  "146-147-150-155-157"
#
# Two registry-scoped pieces are needed, because nothing outside the registry
# derives this field:
#
#   1. `PullRequest.from_json` drops `prs_in_bundle` when parsing a raw record
#      and the harness never rebuilds it, so `number_interval` stays "" and the
#      resolved jsonl (written from `pr.number_interval` in dataset.py) would
#      carry an empty field.  The shim below fills it for evilmartians/lefthook
#      records whose value is empty; it then flows straight into the output.
#   2. `Instance.create()` routes on f"{org}/{number_interval}" whenever that
#      field is non-empty and raises if the key is absent -- so every delivered
#      interval is registered against `Lefthook` below, and a scoped fallback
#      catches bundles from a regenerated dataset that are not in this list.
#
# Both shims are idempotent, guarded by a sentinel attribute, and match only
# org == "evilmartians" / repo == "lefthook"; every other repo is untouched.
# ---------------------------------------------------------------------------
_LEFTHOOK_BUNDLE_NIS = [
    "88-89",
    "116-123-124-126",
    "130-142-146-148-150-154-167-168",
    "169-171-172",
    "173-186-187-188-189-193-194-196-201-205-209-218-223-224-231-235-236-242-256-260-263-265-273",
    "175-177-179-181-182-184",
    "244-245-264-275",
    "280-301-304-305-306-307-308-309-310-311-312-314-316",
    "318-324-330",
    "333-334-335-337-338",
    "343-351",
    "363-368-370-371",
    "373-375-376-377",
    "393-395-396",
    "397-398",
    "402-429",
    "448-449",
    "450-455-457",
    "461-462",
    "474-475",
    "481-482-483",
    "484-485-487",
    "489-490-491-492-493-499",
    "519-520-521-523-524",
    "525-526-527-529",
    "531-532-533",
    "534-536-537",
    "541-543",
    "545-546-547-548",
    "549-550",
    "553-556-561",
    "572-575",
    "577-601-604-606",
    "589-590",
    "596-609",
    "602-607-851-853-854",
    "616-630-638",
    "634-637-653-668-670-672-673-674",
    "678-684-687",
    "689-690-692-694-695",
    "701-711-716",
    "735-737-738-739-740-742",
    "748-754",
    "813-881-883-884-886",
    "847-848-849-850",
    "857-858",
    "861-896-897-898-899",
    "875-879",
    "917-918",
    "924-925-926",
    "930-931",
    "936-937",
    "964-969",
    "974-976-977-978",
    "1015-1017",
    "1027-1031-1034-1040-1044",
    "1064-1071",
    "1067-1069",
    "1072-1074-1075",
    "1094-1101-1102",
    "1095-1103-1104-1107-1108-1115-1116",
    "1117-1118-1119-1129-1130-1131",
    "1132-1133-1135",
    "1138-1139",
    "1140-1141",
    "1143-1145-1146-1147-1148-1150-1151-1152",
    "1160-1161",
    "1181-1243-1255-1263-1278-1285-1287-1288-1297",
    "1189-1190-1200-1206",
    "1209-1246-1261-1274-1275",
    "1210-1230-1234",
    "1219-1220-1221-1222-1223-1224-1225-1229",
    "1227-1235-1236-1242",
    "1244-1245-1250-1259",
    "1251-1318-1319-1323-1324-1326-1327",
    "1291-1381-1382-1383-1391-1393",
    "1292-1301",
    "1308-1339-1340-1343-1346-1347-1348",
    "1349-1362-1368-1370-1371-1372-1373-1375",
]

for _ni in _LEFTHOOK_BUNDLE_NIS:
    Instance.register("evilmartians", _ni)(Lefthook)


import json as _lht_json  # noqa: E402
from multi_swe_bench.harness.pull_request import PullRequest as _LhtPullRequest  # noqa: E402

if not getattr(_LhtPullRequest, "_evilmartians_ni_shim", False):
    _lht_orig_from_json = _LhtPullRequest.from_json.__func__

    def _lht_from_json(cls, json_str):
        pr = _lht_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "evilmartians"
                and getattr(pr, "repo", "") == "lefthook"
                and not getattr(pr, "number_interval", "")
            ):
                prs = (_lht_json.loads(json_str) or {}).get("prs_in_bundle") or []
                if prs:
                    # Sorted members joined by "-" -- never a first-last range.
                    pr.number_interval = "-".join(str(p) for p in sorted(prs))
        except Exception:
            pass
        return pr

    _LhtPullRequest.from_json = classmethod(_lht_from_json)
    _LhtPullRequest._evilmartians_ni_shim = True

if not getattr(Instance, "_evilmartians_route_shim", False):
    _lht_orig_create = Instance.create.__func__

    def _lht_create(cls, pr, config, *args, **kwargs):
        try:
            return _lht_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            # A regenerated dataset can bundle PRs differently, producing an
            # interval absent from _LEFTHOOK_BUNDLE_NIS. lefthook is a single
            # era (one base image spans every go.mod), so the bare key is the
            # correct target for any such bundle.
            if (
                getattr(pr, "org", "") == "evilmartians"
                and getattr(pr, "repo", "") == "lefthook"
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_lht_create)
    Instance._evilmartians_route_shim = True
