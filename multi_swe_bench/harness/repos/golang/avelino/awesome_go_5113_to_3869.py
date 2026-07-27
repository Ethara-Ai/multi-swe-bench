import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AwesomeGoModImageBase(Image):
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
        return "base-gomod"

    def workdir(self) -> str:
        return "base-gomod"

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


class AwesomeGoModImageDefault(Image):
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
        return AwesomeGoModImageBase(self.pr, self.config)

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
go mod download 2>&1 || true
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
git apply --whitespace=nowarn --exclude='*.png' /home/test.patch
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
git apply --whitespace=nowarn --exclude='*.png' /home/test.patch /home/fix.patch
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


@Instance.register("avelino", "awesome-go_5113_to_3869")
class AWESOME_GO_5113_TO_3869(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AwesomeGoModImageDefault(self.pr, self._config)

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
# instance.py routes it to AWESOME_GO_5113_TO_3869 instead of raising "not registered".
# Explicit member lists, never ranges -- bundles are sparse. Each bundle maps
# to its OWN era (go.mod cutover is non-monotonic: pr-3870 base has no go.mod
# -> legacy; pr-3869 base has go.mod -> gomod).
_AVELINO_BUNDLE_NIS = [
    "3869-3905-3914-3916-3985-3987-3989-3990-3992-3995-3996-3997-4000-4003-4004-4005-4006-4010-4011-4013-4014-4015-4017-4019-4020-4021-4022-4023-4024-4025-4027-4028-4029-4031-4034-4037-4038-4039-4040-4048-4049-4052",
    "3896-3899-3918-3927-3936-3937-3941-3945-3946-3947-3948-3949-3955-3958-3959-3960-3962-3964-3967-3970-3972-3975-3977-3978-3979-3980-3984",
    "4172-4228-4231-4233-4235-4236-4238-4239-4240-4241-4242-4243-4248-4254-4256-4260-4261-4262-4263-4265-4266-4267-4275-4277-4280-4281-4284-4296-4303",
    "4347-4406-4437-4446-4447-4449-4453-4458-4459-4464-4466-4475-4495-4500-4505-4507-4512-4516-4518-4528-4530-4538-4539-4543-4544-4545-4546-4551-4552-4553-4554-4555-4558-4567-4571-4575-4578-4580-4581-4582-4584-4585-4586-4587-4588",
    "4354-4358-4366-4386-4389-4402-4405-4408-4409-4412-4413-4415-4420-4421-4422-4423-4426-4427-4429-4430-4431-4432-4433-4434-4435-4438-4439-4442-4444-4455-4473-4474",
    "4472-4491-4510-4569-4572-4590-4613-4625-4648-4673-4674-4718-4722-4774-4825-4898-4958-5056-5226-5240-5249-5260-5278-5309-5347-5348-5354-5356-5357-5360-5361-5362-5364-5366-5367-5369-5373-5375-5377-5378-5381-5382",
    "4492-4660-4749-5072-5103-5215-5338-5350-5395-5403-5408-5410-5412-5417-5419-5422-5428-5430-5431-5432-5438-5439-5442-5444-5446-5448",
    "4531-5932-5958-5959-5976-5991-5997-6000-6016-6023",
    "4573-4792-4808-4811-4812-4813-4814-4817-4818-4820-4822-4829-4843-4846-4847-4848-4849-4850-4852-4856",
    "4678-4726-4876-4902-4905-4906-4907-4908-4926",
    "4745-5082-5263-5268-5276-5277-5279-5285",
    "5113-5258-5319-5404-5443-5476-5492-5493-5508-5514-5579-5581-5585-5587-5624-5640-5642-5644-5646-5668-5672-5677-5678-5680-5686-5695-5741-5748-5768-5782-5783-5795-5853-5857-5858-5859-5860-5862-5863-5866-5874-5875-5876-5878-5879-5881-5883-5884-5887-5891-5909-5913-5914-5915-5919",
]

for _ni in _AVELINO_BUNDLE_NIS:
    Instance.register("avelino", _ni)(AWESOME_GO_5113_TO_3869)
