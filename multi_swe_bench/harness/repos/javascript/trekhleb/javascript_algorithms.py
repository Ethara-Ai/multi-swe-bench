"""trekhleb/javascript-algorithms harness.

Synced to the updated multi_swe_bench.harness.image contract:
  * the base image is SELF-CONTAINED: its Dockerfile starts with the
    "# syntax=docker/dockerfile:1.6" directive, which makes
    DockerfileEnhancer.enhance() return it verbatim (enhance() short-circuits
    when that directive is already present). The base therefore injects NO
    proxy args, NO CA-cert symlinks and NO MITM cert mount, while still
    hand-embedding the standard git-hardening block (checkout ${BASE_COMMIT},
    drop the origin remote, delete every other ref, prune all objects
    unreachable from the base commit). REPO_URL / BASE_COMMIT / TARGETARCH are
    declared as build ARGs so the pipeline still supplies them;
  * the per-PR image is FROM the base image, so DockerfileEnhancer leaves it
    untouched (it only enhances string-dependency images) -- it never carried
    proxy/cert config either.

node:18 is verified to run every era of this repo (jest 22/23 + babel 6 from
2018 through jest 29 + babel 7 in 2025), so a single base suffices.

Two hardening-compatible fixes over the vanilla template keep the fail->pass
count healthy:
  1. In fix-run, the test patch and fix patch are applied in SEPARATE `git
     apply` invocations. Applying both in one invocation makes git validate the
     combined stream against the original tree and spuriously reject the fix
     patch's package.json / lockfile hunks, so dependencies the fix adds
     (canvas, pngjs, version bumps) never land.
  2. `npm install` is re-run after patching so those fix-patch-added
     dependencies are present before jest runs.

NOTE on binary test fixtures: the two seam-carving PRs (#693, #1006) ship their
`test-image-*.jpg/.png` fixtures as bare "Binary files ... differ" lines with no
payload (the dataset was produced with plain `git diff`, not `--binary`). The
git-hardening block prunes the PR's future objects, so those images cannot be
reconstructed from git history -- and doing so would reach past the anti-leak
boundary the hardening enforces. Those image-loading tests therefore fail to
run; every other test in the repo is unaffected.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Base image: node:18 + native libs for `canvas`, cloned via ${REPO_URL}.

    Returns a string dependency and emits a self-contained Dockerfile carrying
    the "# syntax=docker/dockerfile:1.6" directive, so DockerfileEnhancer leaves
    it verbatim (no proxy / cert / MITM injection). The git-hardening block is
    hand-embedded so the anti-leak guarantee is preserved.
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

    def dependency(self) -> str:
        return "node:18"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def extra_packages(self) -> list[str]:
        # Native build deps for `node-canvas` (seam-carving / image-processing).
        # build-essential, git, python3, curl, make are already in the image.py
        # default package set.
        return [
            "pkg-config",
            "libcairo2-dev",
            "libjpeg-dev",
            "libpango1.0-dev",
            "libgif-dev",
            "librsvg2-dev",
        ]

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # Build ARGs (no proxy args). REPO_URL/BASE_COMMIT are still supplied by
        # the pipeline; TARGETARCH is provided automatically by buildx.
        build_args = (
            "ARG TARGETARCH\n"
            f'ARG REPO_URL="{repo_url}"\n'
            "ARG BASE_COMMIT"
        )

        # ENV without proxy/cert variables.
        env_block = (
            "ENV DEBIAN_FRONTEND=noninteractive \\\n"
            "    LANG=C.UTF-8 \\\n"
            "    TZ=UTC"
        )

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}"'
        )

        # The leading syntax directive makes DockerfileEnhancer.enhance() return
        # this Dockerfile untouched, so NO proxy args, CA-cert symlinks or MITM
        # cert mount are injected. The git-hardening block below is embedded by
        # hand so the anti-leak guarantee (checkout ${BASE_COMMIT}, drop remote,
        # delete refs, prune unreachable objects) is still enforced.
        sections = [DockerfileEnhancer.SYNTAX_DIRECTIVE, f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(build_args)
        sections.append(env_block)
        sections.append(label_block)
        sections.append(apt_command)
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        sections.append(Image._HARDENING_BLOCK)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')
        return "\n\n".join(sections) + "\n"


class ImageDefault(Image):
    """Per-PR image: FROM the hardened base + patches + run scripts."""

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
        return ImageBase(self.pr, self._config)

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
            # The base image is already hardened to ${BASE_COMMIT}. Re-affirm the
            # checkout (no-op when already there) and install base dependencies.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha} 2>/dev/null || true

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# Bounded so a single runaway/infinite-loop test cannot stall report building:
# --testTimeout caps each test; `timeout` is a hard backstop for CPU-bound hangs
# that block jest's own timer (SIGKILL 30s after SIGTERM). Partial output is
# still captured and parsed. Baseline suite runs in ~30s, so 900s never
# false-positives on a healthy run.
timeout -k 30 900 ./node_modules/.bin/jest --verbose --no-cache --runInBand --testTimeout=120000 2>&1 || true

""".format(pr=self.pr),
            ),
            # test-run: apply ONLY the test patch, then re-install (a test patch
            # may add fixtures/deps) before running jest.
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch || true
npm install || true
# Bounded so a single runaway/infinite-loop test cannot stall report building:
# --testTimeout caps each test; `timeout` is a hard backstop for CPU-bound hangs
# that block jest's own timer (SIGKILL 30s after SIGTERM). Partial output is
# still captured and parsed. Baseline suite runs in ~30s, so 900s never
# false-positives on a healthy run.
timeout -k 30 900 ./node_modules/.bin/jest --verbose --no-cache --runInBand --testTimeout=120000 2>&1 || true

""".format(pr=self.pr),
            ),
            # fix-run: apply the test patch and the fix patch in SEPARATE git
            # apply invocations (applying both together makes git validate the
            # combined stream against the original tree and spuriously reject the
            # fix patch's package.json / lockfile hunks). Re-install so any
            # dependency the fix adds (canvas, pngjs, version bumps) is present.
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch || true
git apply --reject --whitespace=nowarn /home/fix.patch || true
npm install || true
# Bounded so a single runaway/infinite-loop test cannot stall report building:
# --testTimeout caps each test; `timeout` is a hard backstop for CPU-bound hangs
# that block jest's own timer (SIGKILL 30s after SIGTERM). Partial output is
# still captured and parsed. Baseline suite runs in ~30s, so 900s never
# false-positives on a healthy run.
timeout -k 30 900 ./node_modules/.bin/jest --verbose --no-cache --runInBand --testTimeout=120000 2>&1 || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name() if isinstance(base, Image) else base

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return (
            f"FROM {base_name}\n"
            f"\n"
            f"{self.global_env}\n"
            f"\n"
            f"WORKDIR /home/{self.pr.repo}\n"
            f"\n"
            f"{copy_commands}"
            f"RUN bash /home/prepare.sh; exit 0\n"
            f"\n"
            f"{self.clear_env}\n"
            f"\n"
            f'CMD ["/bin/bash"]\n'
        )


@Instance.register("trekhleb", "javascript-algorithms")
class JavascriptAlgorithms(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_re.sub("", test_log)

        current_file = ""
        describe_stack = []
        in_error_section = False

        for line in clean_log.splitlines():
            stripped = line.strip()

            if not stripped:
                continue

            file_match = re.match(r"^\s*(PASS|FAIL)\s+(\S+)", line)
            if file_match:
                current_file = file_match.group(2).strip()
                describe_stack = []
                in_error_section = False
                continue

            # jest error detail section: ● describe › test name
            if stripped.startswith("●"):
                in_error_section = True
                continue

            if in_error_section:
                continue

            if re.match(
                r"^(Test Suites:|Tests:|Snapshots:|Time:|Ran all test suites)",
                stripped,
            ):
                continue

            if not current_file:
                continue

            indent = len(line) - len(line.lstrip())

            # jest verbose: ✓ passed, ✕ failed, ○ skipped/todo
            pass_match = re.match(
                r"^\s+[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if pass_match:
                test_name = pass_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                passed_tests.add(full_name)
                continue

            fail_match = re.match(
                r"^\s+[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if fail_match:
                test_name = fail_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                failed_tests.add(full_name)
                continue

            skip_match = re.match(
                r"^\s+○\s+(?:skipped|todo)\s+(.+?)\s*$", line
            )
            if skip_match:
                test_name = skip_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                skipped_tests.add(full_name)
                continue

            if indent > 0:
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                describe_stack.append((indent, stripped))

        # dedup: worst result wins (failed > skipped > passed)
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
# number_interval routing (canonical dash-joined prs_in_bundle format).
# Each resolved record carries number_interval == its sorted prs_in_bundle
# joined by "-" (NOT a low-high range). Instance.create() looks up
# f"{org}/{number_interval}"; registering the same class under each key makes
# it answer to every bundle. The plain "trekhleb/javascript-algorithms"
# registration above stays for records with an empty number_interval.
# ---------------------------------------------------------------------------
_BUNDLE_NUMBER_INTERVALS = [
    "3-4-5-8-10-14-16-17-18-20-23-27-28-36-39-42-46-47-50-55-62-65-66",
    "80-81-85-86-94-101-106",
    "118-120-124-137-138-170-180-183",
    "140-141-152-154",
    "146-185",
    "151-283-286-291-345-350-371-385-386-396-409-423-424-432-439-447-449-459-466-516-532-533-540-542-547-575-581-584-587-592-595-603",
    "175-177",
    "203-208-211-214-218-221-222",
    "224-226-227-228-233",
    "235-250-257-260-262-263-266-267-273-275-276-277-278-279-282",
    "293-296-301-309-313-316-317-318-319-320-321-322-324-326-330-331-334",
    "332-333-335-337-340",
    "469-486-487-489-497-502-517-520-524-530",
    "600-602-607-612-613",
    "615-617-622",
    "628-632-634-637-639-644-651-652-663-664-665-666-667-668-670-708-710-712-717-723-724-726-735-739-740-742-752-767-768-771-773-774-775-777-780-785-787-789-796-797-804-805-806-808-809-810-815-816-817-820-828-829-833-836-842-843",
    "693-694",
    "790-792",
    "975-980-989",
    "1006-1029",
    "1071-1079-1086-1088-1093-1117",
    "1077-1202-2030",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("trekhleb", _ni)(JavascriptAlgorithms)
