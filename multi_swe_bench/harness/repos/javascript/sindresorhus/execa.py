import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Mirrors the package list baked into Image.dockerfile() (image.py) so the
# shared base image installs exactly the canonical toolchain.
_DEFAULT_PACKAGES = [
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


# ---------------------------------------------------------------------------
# execa: sindresorhus/execa
# ---------------------------------------------------------------------------
# Process execution for humans.  Tests use AVA across every version in the
# dataset, run with `npx ava --tap` for machine-parseable TAP output regardless
# of the AVA major version.
#
# Each dataset row is a release-delta BUNDLE (base.label "vA..vB"), so the PR
# numbers squashed into it are carried by `prs_in_bundle` / `number_interval`.
# See the routing block at the bottom of this file.
#
# Image chain:  node:20 -> Base (shared toolchain, no checkout -- one build
#                                serves every execa PR)
#                       -> Per-PR (clone + checkout ${BASE_COMMIT} + npm
#                                  install + patches + hardening block)
#
# Version-specific handling:
#   1. PRs 73-158 (v0.6-v1.0): package.json has "ava": "*", which resolves
#      to AVA 7 (ESM-only, incompatible).  prepare.sh overrides with
#      ava@0.25 (last CJS-compatible 0.x release that works on node:20).
#   2. PRs 312+ (v2.0+): AVA is pinned (^2.1+), resolves correctly.
# ---------------------------------------------------------------------------

# PRs at or below this number have "ava": "*" and need an override.
_AVA_WILDCARD_CUTOFF = 158


# ---------------------------------------------------------------------------
# Base Image
# ---------------------------------------------------------------------------


class ExecaImageBase(Image):
    """Shared base image - node:20 toolchain, one build for every execa PR."""

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
        return "node:20"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Shared toolchain base: ONE build serves every PR of this repo, so it
        # deliberately carries no repo checkout.  Mirrors Image.dockerfile()
        # (image.py) minus the clone/checkout/hardening block, which are per-PR
        # -- the hardening block prunes the repo down to a single commit and
        # would pin this image to one PR.
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""\
FROM {base_img}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

{apt_command}

{self.clear_env}

CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# Instance Image
# ---------------------------------------------------------------------------


class ExecaImageDefault(Image):
    """Per-PR instance image.  Checks out the base commit, installs deps,
    and injects patches + run scripts."""

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
        return ExecaImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _ava_override(self) -> str:
        """Early PRs have ``"ava": "*"`` which resolves to AVA 7 (ESM-only).
        Override with ava@0.25 - last 0.x that works on Node 20."""
        if self.pr.number <= _AVA_WILDCARD_CUTOFF:
            return "npm install ava@0.25 --save-dev"
        return ""

    def _make_run_script(self, patches: str) -> str:
        """Generate a run script with optional patch application."""
        return """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{patches}
npx ava --tap 2>&1
""".format(
            repo=self.pr.repo,
            patches=patches,
        )

    def files(self) -> list[File]:
        ava_override = self._ava_override()
        install_cmd = "npm install || true"
        if ava_override:
            install_cmd = "npm install || true\n{ava}".format(ava=ava_override)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

{install}

""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    install=install_cmd,
                ),
            ),
            # run.sh - baseline: no patches
            File(".", "run.sh", self._make_run_script("")),
            # test-run.sh - test.patch only
            File(
                ".",
                "test-run.sh",
                self._make_run_script(
                    "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.sketch' /home/test.patch || "
                    "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.sketch' --3way /home/test.patch || true"
                ),
            ),
            # fix-run.sh - test.patch + fix.patch
            File(
                ".",
                "fix-run.sh",
                self._make_run_script(
                    "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.sketch' /home/test.patch /home/fix.patch || "
                    "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.sketch' --3way /home/test.patch /home/fix.patch || true"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        # clone -> checkout ${BASE_COMMIT} -> prepare -> harden.
        # REPO_URL/BASE_COMMIT carry defaults because the pipeline only passes
        # those build args to images whose dependency is a string; this one
        # depends on the base Image.
        dep = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""\
FROM {dep.image_full_name()}

{self.global_env}

ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT="{self.pr.base.sha}"

# Fetch ONLY the base commit, with retries.  A full clone of the whole
# history repeatedly timed out under qemu emulation on multi-arch builds
# (`RPC failed; curl 56 Recv failure`), and every commit after BASE_COMMIT
# would be pruned by the hardening block below anyway.
RUN set -eux; \\
    for attempt in 1 2 3; do \\
        rm -rf /home/{self.pr.repo}; \\
        git init -q /home/{self.pr.repo}; \\
        cd /home/{self.pr.repo}; \\
        git remote add origin "${{REPO_URL}}"; \\
        git config http.postBuffer 524288000; \\
        git config http.lowSpeedLimit 1000; \\
        git config http.lowSpeedTime 120; \\
        if git fetch --depth 1 --no-tags origin "${{BASE_COMMIT}}" \\
           || git fetch --no-tags origin "${{BASE_COMMIT}}" \\
           || git fetch --no-tags origin; then \\
            git checkout --detach "${{BASE_COMMIT}}" && break; \\
        fi; \\
        echo "fetch attempt ${{attempt}} failed; retrying"; \\
        sleep $((attempt * 15)); \\
    done; \\
    git rev-parse --verify "${{BASE_COMMIT}}^{{commit}}"

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}

CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@Instance.register("sindresorhus", "execa")
class Execa(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExecaImageDefault(self.pr, self._config)

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
        """Parse AVA TAP output.

        TAP format:
            TAP version 13
            ok 1 - file > test name
            not ok 2 - file > test name
            ...
            1..N
            # tests N
            # pass N
            # fail N

        We extract individual test results from ``ok``/``not ok`` lines.
        The test name is everything after the number and optional dash.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_escape.sub("", test_log)

        # TAP result lines
        re_ok = re.compile(r"^ok\s+\d+\s+-?\s*(.+)$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s+-?\s*(.+)$")
        # TAP skip directive: "ok N - name # skip reason"
        re_skip = re.compile(r"#\s*skip", re.IGNORECASE)

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            m_ok = re_ok.match(stripped)
            if m_ok:
                name = m_ok.group(1).strip()
                if re_skip.search(stripped):
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)
                continue

            m_fail = re_not_ok.match(stripped)
            if m_fail:
                name = m_fail.group(1).strip()
                failed_tests.add(name)
                continue

        # Ensure no overlap
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Every execa dataset row is a release-delta bundle: `number` is the first PR of
# the bundle and `number_interval` is the dash-joined `prs_in_bundle` list
# (e.g. "1163-1166" for the v9.4.1..v9.5.0 delta).  Instance.create() resolves
# f"{org}/{number_interval}" whenever number_interval is non-empty, so every
# delivered interval string must be a registered routing key -> Execa.
# Single-era repo: all keys map to the one class.  Data-derived from the 17
# delivered bundles in dataset/sindresorhus__execa_dataset.jsonl - regenerate
# if the delivered set changes.
_NUMBER_INTERVALS = [
    "1163-1166",
    "1158-1160",
    "1131-1136-1138-1140-1141-1144-1145",
    "1126-1127-1128-1129-1130",
    "1054-1058-1059-1060-1061-1063-1066-1067-1068-1071-1074-1075-1076-1077-1079-1080-1082-1083-1084-1086-1087-1088-1089-1090-1091-1092-1094-1095-1096-1098-1100-1101-1104-1105-1107-1108-1109-1110-1111-1113-1114-1115-1116-1117-1118-1119-1120-1121-1122",
    "575-576-582-583-586-588-589-594-596-600-601-602-604-605-607-609-610-611-613-614-615-618-619-621-622-623-624-625-626-628-629-630-631-632-633-634-635-636-637-638-639-640-641-643-644-645-646-647-648-649-650-651-652-653-654-655-658-659-660-661-662-663-666-668-669-670-671-672-676-678-680-681-682-683-684-685-686-687-688-689-690-691-692-693-696-697-698-699-701-703-704-705-708-709-710-711-712-713-714-715-717-722-723-724-725-727-728-729-731-733-734-735-736-737-738-739-740-741-746-747-748-752-755-756-757-759-768-769-770-771-772-773-775-776-777-778-779-780-783-785-787-788-790-791-793-794-796-800-801-802-803-804-805-806-807-809-811-812-814-818-819-821-826-827-829-831-832-833-834-835-836-837-838-839-840-841-843-846-847-849-850-852-854-855-859-860-861-862-864-865-867-868-869-870-873-875-876-879-880-883-884-885-887-888-890-893-894-896-897-899-903-904-905-906-907-908-910-911-912-914-915-916-917-919-920-921-922-923-924-926-927-928-929-931-933-934-935-937-938-939-940-941-942-943-944-945-948-949-950-951-952-953-954-955-956-957-958-959-960-961-962-963-964-965-966-967-968-969-970-971-972-973-974-975-976-977-978-980-982-983-984-989-990-991-992-993-994-996-998-999-1000-1003-1004-1005-1006-1007-1008-1009-1010-1011-1012-1015-1017-1018-1019-1020-1021-1022-1023-1024-1025-1026-1027-1029",
    "562-563-565",
    "550-551-552-553",
    "510-528-529-530-531-532-533-536-537-538-539-540-541-542-545-546-547",
    "497-500-503-505-506-512-518",
    "490-492",
    "461-462-463-464",
    "435-436-437-439-440",
    "324-325-326-328-329-330-331-332-333-334-337",
    "158-167-171-174-178-180-181-182-184-185-187-188-189-193-194-197-199-200-201-203-208-209-212-213-214-215-216-217-218-219-221-222-223-224-225-226-227-229-230-231-233-234-235-237-238-239-240-241-244-245-246-247-248-249-250-251-256-258-260-261-262-264-266-268-269-270-271-272-273-274-276-277-278-279-280-282-283-284-285-286-287-288-289-290-291-292-293-294-296-297-299-300-301-302-303-305-306-307-308-309-310-311-314",
    "107-114-117-118-120",
    "95-102",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("sindresorhus", _interval)(Execa)
