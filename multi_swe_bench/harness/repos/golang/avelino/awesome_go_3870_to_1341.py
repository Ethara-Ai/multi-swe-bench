import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AwesomeGoLegacyImageBase(Image):
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
        return "golang:1.24-bookworm"

    def image_tag(self) -> str:
        return "base-legacy"

    def workdir(self) -> str:
        return "base-legacy"

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

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # Base image hardening (LIGHT): drops the remote and every ref so the
        # image carries no branch/tag/PR provenance, but deliberately does NOT
        # run gc/repack/prune and does NOT check out ${BASE_COMMIT}. This image
        # is SHARED by every PR in the era, so pruning it to one PR's base
        # commit would break `git checkout <sha>` for all the others. All
        # commit objects stay reachable-by-sha; the destructive prune + HEAD
        # audit live in the per-PR image below.
        base_hardening = (
            "RUN set -eux; \\\n"
            "    git checkout --detach HEAD; \\\n"
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all; \\\n"
            "    git reflog expire --expire-unreachable=now --all; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            '    git config --local remote.pushDefault ""; \\\n'
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\\n'
            '    test -z "$(git remote)"'
        )

        base_hardening_submodules = (
            "RUN if [ -f .gitmodules ]; then \\\n"
            "        git submodule foreach --recursive ' \\\n"
            "            git checkout --detach HEAD; \\\n"
            "            git remote remove origin 2>/dev/null || true; \\\n"
            '            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\\n'
            "                | xargs -r -n1 git update-ref -d; \\\n"
            "            git reflog expire --expire=now --all; \\\n"
            "            git reflog expire --expire-unreachable=now --all; \\\n"
            "            rm -f .git/objects/info/alternates; \\\n"
            "        '; \\\n"
            "    fi"
        )

        # Leading `# syntax=` directive makes DockerfileEnhancer.enhance() return
        # this file verbatim, so its greedy `_standardize_repo_fetch` /
        # `_inject_final_sanitize` rewrites cannot inject the destructive
        # per-PR hardening block into this shared base.
        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT"
            ),
            "ENV DEBIAN_FRONTEND=noninteractive \\\n    LANG=C.UTF-8 \\\n    TZ=UTC",
            label,
            self.global_env,
            "ENV GOTOOLCHAIN=auto",
            "WORKDIR /home/",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            base_hardening,
            base_hardening_submodules,
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


class AwesomeGoLegacyImageDefault(Image):
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
        return AwesomeGoLegacyImageBase(self.pr, self.config)

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

export GOTOOLCHAIN=auto

# Pre-go.mod era: synthesize a module to fetch deps via the proxy
if [ ! -f go.mod ]; then
    go mod init github.com/{pr.org}/{pr.repo} 2>&1 || true
    go mod tidy 2>&1 || true
fi

go test -v -count=1 -skip 'TestStaleRepository|TestMaturity' ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto
export CI=true
if [ ! -f go.mod ]; then
    go mod init github.com/{pr.org}/{pr.repo} 2>&1 || true
    go mod tidy 2>&1 || true
fi
go test -v -count=1 -skip 'TestStaleRepository|TestMaturity' ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto
export CI=true
# Era 1: prepare.sh synthesized go.mod/go.sum. Remove before patching so a
# fix.patch that adds go.mod won't conflict; re-synthesize if patch didn't add one.
rm -f go.mod go.sum
git apply --whitespace=nowarn --exclude='*.png' /home/test.patch
if [ ! -f go.mod ]; then
    go mod init github.com/{pr.org}/{pr.repo} 2>&1 || true
    go mod tidy 2>&1 || true
fi
go test -v -count=1 -skip 'TestStaleRepository|TestMaturity' ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto
export CI=true
rm -f go.mod go.sum
git apply --whitespace=nowarn --exclude='*.png' /home/test.patch /home/fix.patch
if [ ! -f go.mod ]; then
    go mod init github.com/{pr.org}/{pr.repo} 2>&1 || true
    go mod tidy 2>&1 || true
fi
go test -v -count=1 -skip 'TestStaleRepository|TestMaturity' ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        # dependency() returns an Image, so DockerfileEnhancer.enhance() emits
        # this file verbatim -- the anti-reward-hacking block must be embedded
        # here by hand. It runs LAST (after prepare.sh has checked out the base
        # commit and warmed the module cache) so it prunes every object that is
        # not an ancestor of BASE_COMMIT: no future commits, no PR/merge refs,
        # no origin to re-fetch them from.
        sections = [
            f"FROM {name}:{tag}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\nENV BASE_COMMIT=${{BASE_COMMIT}}',
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard && git checkout ${BASE_COMMIT}",
            copy_commands.rstrip("\n"),
            "RUN bash /home/prepare.sh",
            Image._HARDENING_BLOCK.rstrip("\n"),
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


@Instance.register("avelino", "awesome-go_3870_to_1341")
class AWESOME_GO_3870_TO_1341(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AwesomeGoLegacyImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        # get_base_name() rolls subtests up to their parent; if one subtest passes
        # and a sibling fails, the parent name lands in both sets. Enforce
        # TestResult invariants by giving failures priority, then skips.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === number_interval: dash-joined prs_in_bundle on the resolved jsonl ===
#
# FORMAT: explicit dash-joined member list, NEVER a range. Bundles are sparse --
# pr-1522's bundle is [1522, 1525, 1533, ...] -> "1522-1525-1533-...". A range
# like "1522-1578" would falsely claim 1523/1524/1526..., not in the bundle.
#
# The raw jsonl carries `prs_in_bundle` but NO `number_interval`, and the
# dataclass_json loader drops unknown keys, so Dataset.build (which copies
# `number_interval=pr.number_interval`) would write "" into every resolved row.
# Setting pr.number_interval at load time would ALSO change the routing key
# (instance.py routes on f"{org}/{number_interval}"), so the value is stashed in
# a NON-field attr and stamped onto the OUTPUT row only, leaving routing on the
# era tags. Idempotent + scoped to this registry (no harness-source edits),
# mirroring MHSanaei/xui. Guard flag makes the twin import in the other era file
# a no-op.
import json as _avelino_json
import multi_swe_bench.harness.pull_request as _avelino_pr

if not getattr(_avelino_pr.PullRequest, "_avelino_number_interval_patched", False):
    _avelino_orig_from_json = _avelino_pr.PullRequest.from_json.__func__

    def _avelino_from_json(cls, json_str):
        pr = _avelino_orig_from_json(cls, json_str)
        try:
            raw = _avelino_json.loads(json_str)
            if (
                raw.get("org") == "avelino"
                and raw.get("repo") == "awesome-go"
                and raw.get("prs_in_bundle")
            ):
                pr._avelino_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _avelino_pr.PullRequest.from_json = classmethod(_avelino_from_json)
    _avelino_pr.PullRequest._avelino_number_interval_patched = True

    from multi_swe_bench.harness.dataset import Dataset as _AvelinoDataset

    # Dataset subclasses PullRequest and INHERITS the flag above; use a distinct
    # flag checked on the class's OWN __dict__ so this patch is not skipped.
    if not _AvelinoDataset.__dict__.get("_avelino_build_patched", False):
        _avelino_orig_build = _AvelinoDataset.build.__func__

        def _avelino_build(cls, pr, report):
            ds = _avelino_orig_build(cls, pr, report)
            ni = getattr(pr, "_avelino_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _AvelinoDataset.build = classmethod(_avelino_build)
        _AvelinoDataset._avelino_build_patched = True


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Defensive: covers a regenerated jsonl that DOES carry number_interval, so
# instance.py routes it to AWESOME_GO_3870_TO_1341 instead of raising "not registered".
# Explicit member lists, never ranges -- bundles are sparse. Each bundle maps
# to its OWN era (go.mod cutover is non-monotonic: pr-3870 base has no go.mod
# -> legacy; pr-3869 base has go.mod -> gomod).
_AVELINO_BUNDLE_NIS = [
    "1341-1342-1354-1355-1356-1361-1362-1363-1364-1366-1367-1369-1370-1372-1376-1377-1378-1379-1380-1381-1383-1384-1388-1389-1392-1394-1395-1396-1397-1398-1399-1400-1402-1404-1405",
    "1522-1525-1533-1534-1535-1536-1537-1538-1539-1540-1542-1543-1544-1545-1546-1547-1552-1553-1554-1555-1556-1557-1558-1560-1562-1564-1565-1567-1568-1570-1574-1577-1578",
    "1561-1569-1571-1579-1582-1584-1585-1586-1591-1593-1594-1595-1596-1597-1598-1603-1608-1613-1614-1617-1618-1621",
    "2210-2214-2231-2234-2235-2236-2237-2238-2239-2241-2242-2243-2244-2246-2247-2248-2249-2250-2251-2252-2256-2257-2258-2259-2260-2261-2262-2263-2264-2265-2266-2267-2268-2269-2270-2271-2272-2273-2278-2279-2281-2283-2285-2286-2288-2289-2290-2291",
    "2343-2357-2379-2384-2386-2388-2389-2390-2392-2394-2395-2396-2397-2398-2399-2406-2407-2410-2411-2412-2419-2420-2421-2422-2423-2426-2427-2428-2429-2430-2432-2434-2436-2439-2441-2442-2443-2444",
    "2365-2372-2385-2391-2393-2402-2403-2405-2415-2425-2446-2447-2449-2450-2451-2452-2454-2455-2456-2457-2458-2460-2461-2462-2464-2466-2467-2468-2469-2471-2472-2474-2476-2477-2481-2483-2484-2485-2490",
    "3210-3231-3252-3254-3255-3258-3259-3262-3267-3269-3270-3273-3276-3277-3278-3279-3282-3284-3285-3286-3287-3288-3289-3290-3293-3294-3297-3298-3299-3300-3301-3302-3307-3309",
    "3388-3439-3460-3463-3465-3466-3467-3468-3472-3474-3476-3478-3480-3484-3485-3487-3490-3493-3494-3495-3496-3500-3506-3515-3516-3520",
    "3401-3406-3413-3414-3416-3417-3418-3419-3420-3423-3424-3427-3428-3430-3431-3432-3433-3435-3436-3440-3441-3444-3446-3447-3448-3449-3450-3451-3452-3454-3455-3457-3461-3462",
    "3870-3893-3897-3906-3910-3912-3922-3925-3926-3930-3932-3933-3940",
]

for _ni in _AVELINO_BUNDLE_NIS:
    Instance.register("avelino", _ni)(AWESOME_GO_3870_TO_1341)
