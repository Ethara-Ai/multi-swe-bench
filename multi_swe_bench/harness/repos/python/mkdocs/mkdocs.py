import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Era boundary (PIPELINE.md §1, multi-era repo).
#
# mkdocs 0.17 raises StopIteration inside a generator (mkdocs/nav.py:337). PEP 479
# turned that into a RuntimeError from Python 3.7 on, so the 0.17 line is simply
# not runnable on python:3.8 -- measured: 49 of its 232 tests error out, versus 1
# error on python:3.6. Every later line (1.0 - 1.6) is healthy on 3.8 (0-3
# pre-existing failures) and 1.5+ needs a modern pip for its hatchling
# pyproject.toml, which python:3.6 does not have. Hence one base per era.
#
# The boundary is keyed on PR number, not on the `release_line` string, because
# two records carry release_line "unknown" and both belong to the 3.8 era. The
# 0.17 bundle is PRs 1318-1333; the next era's lowest PR is 1434.
_PY36_MAX_PR = 1333


class ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def _py_tag(self) -> str:
        return "py36" if self.pr.number <= _PY36_MAX_PR else "py38"

    def dependency(self) -> str:
        return "python:3.6" if self._py_tag == "py36" else "python:3.8"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        # One SHARED base per era. Distinct tags so the two eras cannot collide.
        return f"base-{self._py_tag}"

    def workdir(self) -> str:
        # Must track image_tag(): the build context dir is derived from workdir(),
        # so a constant here would make both eras share one folder and overwrite
        # each other's Dockerfile.
        return f"base-{self._py_tag}"

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Third-party pins only. `pip install -e .` deliberately does NOT run here:
        # this base is shared by every PR, so it is not sitting on any particular
        # commit, and mkdocs' setup.py requirements differ across release lines
        # 0.17 - 1.6. The editable install therefore happens per-PR in prepare.sh,
        # after that PR's base.sha is checked out.
        return (
            "RUN pip install --no-cache-dir "
            "'jinja2<3.1' 'markupsafe<2.1' 'Markdown<3.4' 'click<8' mock"
        )

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # SHARED base (tag "base", ONE image reused by every PR). It keeps the full
        # git history -- no checkout of a specific commit, no gc/prune -- so each
        # PR's prepare.sh can still `git checkout <its own base.sha>`. Dropping
        # origin unreferences the upstream branches but leaves their objects
        # intact; the strict per-PR hardening in ImageDefault only prunes AFTER
        # detaching onto that PR's base.sha. A shared base cannot pin to one
        # commit, so the strict pass cannot live here.
        #
        # The `# syntax` directive makes DockerfileEnhancer.enhance() skip this
        # file. That is the sanctioned opt-out (PIPELINE.md §2) and it does two
        # jobs: it stops the enhancer injecting the proxy ARGs / SSL_CERT_FILE /
        # CA-cert symlink block that HARD RULE 4 forbids, AND it stops the
        # enhancer auto-injecting a ${BASE_COMMIT} checkout + prune, which would
        # silently turn this shared base back into a per-PR one.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}
WORKDIR /home/
# Same package set Image.dockerfile() installs by default. Kept identical on
# purpose: opting out of the enhancer is meant to drop ONLY the proxy/cert
# injection, not silently change the toolchain available to 20 release lines
# (0.17 - 1.6) whose setup.py requirements differ.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.extra_setup()}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

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

if [[ -n $(git status --porcelain --ignore-submodules=all) ]]; then
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

# The base image is SHARED (one for all PRs) and sits on the clone's default
# branch with full history, so this is where this PR's own commit is selected.
# It is also the script build_dataset/run_evaluation replay in non-human
# (envagent) mode -- session_util opens this path unconditionally, so the file
# must exist.
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}

# Editable install AFTER the checkout: mkdocs' setup.py requirements differ
# across release lines 0.17 - 1.6, so the install has to match this commit
# rather than whatever the shared base happened to clone. Leaves only
# mkdocs.egg-info/, which the repo gitignores, so the tree stays clean.
pip install --no-cache-dir -e .

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
python -m unittest discover -s mkdocs -p '*tests.py' -v 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "strip_binary.sh",
                r"""#!/bin/bash
# Strip binary diff hunks from a patch file, outputting text-only diffs.
# A text diff always has @@ hunk markers. Binary diffs don't.
awk '
/^diff --git / {
    if (buf != "" && has_hunk) { printf "%s", buf }
    buf = $0 "\n"; has_hunk = 0; next
}
/^@@/ { has_hunk = 1 }
{ buf = buf $0 "\n" }
END { if (buf != "" && has_hunk) printf "%s", buf }
' "$@"
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /tmp/test_text.patch
if [ -s /tmp/test_text.patch ]; then
    if ! git -C /home/{repo} apply --whitespace=nowarn /tmp/test_text.patch; then
        echo "Error: git apply failed for test patch" >&2
        exit 1
    fi
    # Re-install if setup.py or pyproject.toml changed
    if grep -q "setup.py\|setup.cfg\|pyproject.toml" /tmp/test_text.patch; then
        pip install --no-cache-dir -e . > /dev/null 2>&1
    fi
fi
python -m unittest discover -s mkdocs -p '*tests.py' -v 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /tmp/test_text.patch
bash /home/strip_binary.sh /home/fix.patch > /tmp/fix_text.patch
if [ -s /tmp/test_text.patch ]; then
    if ! git -C /home/{repo} apply --whitespace=nowarn /tmp/test_text.patch; then
        echo "Error: git apply failed for test patch" >&2
        exit 1
    fi
fi
if [ -s /tmp/fix_text.patch ]; then
    if ! git -C /home/{repo} apply --whitespace=nowarn /tmp/fix_text.patch; then
        echo "Error: git apply failed for fix patch" >&2
        exit 1
    fi
fi
# Re-install if any patch touched setup.py/pyproject.toml (new deps)
if grep -q "setup.py\|setup.cfg\|pyproject.toml" /tmp/test_text.patch /tmp/fix_text.patch 2>/dev/null; then
    pip install --no-cache-dir -e . > /dev/null 2>&1
fi
python -m unittest discover -s mkdocs -p '*tests.py' -v 2>&1

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # PIPELINE.md §4. The base is shared and keeps full history, so ALL of the
        # per-PR work happens here: prepare.sh checks out this PR's base.sha and
        # installs it, then the canonical hardening block from image.py runs with
        # ${BASE_COMMIT} bound to the literal sha -- dropping origin, every ref and
        # the reflog, then pruning, so no commit after base.sha survives in the
        # image. Referenced from image.py rather than copied so it cannot drift
        # from the source of truth. Order matters: hardening must follow
        # prepare.sh, since it asserts HEAD == base.sha.
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("mkdocs", "mkdocs")
class Mkdocs(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # unittest verbose output format:
        # test_name (module.Class.test_name) ... ok
        # test_name (module.Class.test_name) ... FAIL
        # test_name (module.Class.test_name) ... ERROR
        # test_name (module.Class.test_name) ... skipped 'reason'
        # NOT anchored to start-of-line: when a subTest writes into the stream the
        # next test's verbose entry gets concatenated onto the same line, e.g.
        #   test_theme (...ConfigTests) ... test_deploy (...TestGitHubDeploy) ... ok
        # An anchored pattern misses `test_deploy` entirely and records it as NONE.
        # `(?<![\w.])` keeps the test name from matching a partial identifier, and
        # requiring a dotted class plus a literal " ... <status>" keeps traceback
        # lines (`File "...", line 218, in test_theme`) from matching.
        pattern = re.compile(
            r"(?<![\w.])(\w+)\s+\(([\w.]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped\b)",
        )

        for match in pattern.finditer(log):
            test_method = match.group(1)
            test_class = match.group(2)
            test_id = f"{test_class}.{test_method}"
            status = match.group(3)

            if status == "ok":
                passed_tests.add(test_id)
            elif status in ("FAIL", "ERROR"):
                failed_tests.add(test_id)
            elif status.startswith("skipped"):
                skipped_tests.add(test_id)

        # A test using subTest() that has a failing sub-case prints its verbose
        # line with NO status token after the "...", so the pattern above cannot
        # classify it and the test silently becomes NONE. The real per-sub-case
        # results only appear in the summary block:
        #
        #   FAIL: test_theme (tests.config.config_tests.ConfigTests) [{'theme': 'x'}]
        #   ERROR: test_x (tests.foo.Bar)
        #
        # Left unparsed this produced a bogus PASS -> NONE -> PASS transition
        # (mkdocs pr-3026: test_theme / test_deploy), which reads as "the test
        # vanished" when it actually failed under the test patch and passes after
        # the fix. Scan the summary block too; a name found here is authoritative,
        # because a subTest that reports any FAIL/ERROR did not pass as a whole.
        summary = re.compile(
            r"^(FAIL|ERROR):\s+(\S+)\s+\(([^)\s]+)\)",
            re.MULTILINE,
        )
        for match in summary.finditer(log):
            test_id = f"{match.group(3)}.{match.group(2)}"
            failed_tests.add(test_id)

        passed_tests -= failed_tests | skipped_tests
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
_BUNDLE_NIS_MKDOCS = [
    "1599-1601",
    "1616-1617-1623-1625",
    "2111-2112",
    "2810-2825-2844-2852-2860-2863-2893-2894-2895",
    "2987-2988-2990-2991-2992-2996-2997-2998-3001-3004",
    "3026-3034-3037-3046-3094-3107-3117-3124-3125-3129-3139-3154-3156-3192-3193-3195-3199-3207-3208",
    "3320-3324-3325-3326-3330",
    "3334-3340-3351-3367-3368-3370-3381-3383-3390-3392-3400",
    "1318-1322-1326-1328-1329-1330-1333",
    "1434-1576-1582-1586-1589-1590-1594-1597-1607-1630-1631-1634-1638-1642-1645-1653-1654-1656-1657-1669-1672-1673-1684-1685-1689-1695-1703-1707-1713-1714-1718-1719-1739-1740-1748-1749-1759-1767-1774-1781-1782-1792-1797-1811-1816-1841-1842-1843-1857-1860-1863-1864-1867-1869-1878-1880-1882-1902-1921-1922-1925-1929-1930-1935-1936-1938-1939-1940-1945-1950-1953-1967-1969-1970-1971-1982-1984-1985-1986-1991-1992-1994-1995-1996-1998-1999-2000",
    "1603-1606-1610-1611-1613-1615",
    "1978-2179-2407-2464-2477-2478-2481-2489-2490-2496-2497-2501-2502-2506-2507-2510",
    "2001-2007-2018-2020-2021-2022-2029-2030-2035-2048-2060-2069-2070-2071-2073-2081-2093-2094-2095-2100-2103-2106-2130-2131-2146-2152-2165-2167-2173-2178-2193-2196-2203-2209-2214-2223-2224-2229-2230-2253-2254-2257-2258-2259-2260-2261-2263-2264-2265-2267-2271-2283-2296-2297-2299-2300-2301-2303-2304-2305-2309-2313-2334-2339-2344-2353-2354-2355-2356-2358-2359-2360-2361-2364-2366-2367-2372-2376-2382-2383-2385-2388-2396-2397-2402-2403-2405-2413-2421-2422-2424-2427-2430-2436-2438-2440",
    "2290-2387-2684-2777-2807-2824-2897-2907-2912-2913-2914-2915-2916-2917-2918-2921-2927-2928-2929-2930-2931-2933-2934-2937-2938-2940-2941-2942-2943-2944-2946-2947-2948-2949-2959-2962-2963-2970-2972-2973-2976-2978-2979-2980-2981-2982-2983-2984-2986",
    "2439-2585-2594-2612-2613-2617-2622-2626-2633-2636-2642-2652-2653-2654-2661-2663-2667-2673-2680-2698-2699-2705-2708-2710-2712-2713-2714-2733-2735-2740-2742-2751-2755-2756-2763-2778-2781-2785-2787-2791-2800-2801-2802-2804-2806",
    "2443-2444-2449-2454",
    "2515-2525-2535-2541-2545-2548-2549-2550-2551-2552-2563-2565-2567-2587-2591-2602-2603-2604-2606-2607-2608-2609-2610-2611-2614-2616-2618-2620-2621",
    "3016-3019-3020-3022-3024-3027-3032",
    "3391-3395-3425-3429-3430-3435-3437-3443-3444-3445-3448-3449-3451-3456-3460-3463-3464-3465-3466-3476-3477-3485-3493-3500-3501-3502-3503-3505-3511-3518-3520-3522-3525-3561-3564-3568-3578-3609-3613-3625-3631-3634-3647-3649-3651-3657",
    "3629-3682-3683-3684-3694-3697-3700-3730-3743-3762-3764-3774-3784-3787-3795-3798-3804-3808-3809-3817-3819",
]
for _ni in _BUNDLE_NIS_MKDOCS:
    Instance.register("mkdocs", _ni)(Mkdocs)
