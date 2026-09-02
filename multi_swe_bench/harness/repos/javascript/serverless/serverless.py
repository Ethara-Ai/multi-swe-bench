import re
from typing import Optional, Union
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

MOCHA_ERA_MAX_NUMBER = 13185

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DURATION_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s|m)\)\s*$")
_SUMMARY_RE = re.compile(r"^[ \t]*\d+ (?:passing|failing|pending)\b")
_PASS_RE = re.compile(r"^[✓✔]\s+(.+)$")
_PENDING_RE = re.compile(r"^-\s+(.+)$")
_INLINE_FAIL_RE = re.compile(r"^\d+\)\s+(.+)$")
_VOLATILE_RE = re.compile(r"\d{8,}")
_EMBEDDED_PASS_RE = re.compile(r"^.*?\S {2,}([✓✔]\s+.+)$")


class ServerlessImageBase(Image):
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
        return "node:18"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["jq", "ruby"]

    def dockerfile(self) -> str:
        from multi_swe_bench.harness.image import DockerfileEnhancer

        base_img = self.dependency()
        repo = self.pr.repo
        packages_str = " \\\n    ".join(
            [
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
            + self.extra_packages()
        )
        if self.config.need_clone:
            clone = 'RUN git clone "${REPO_URL}" /home/' + repo
        else:
            clone = f"COPY {repo} /home/{repo}"

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n"),
            "WORKDIR /home/",
            self._get_apt_update_command(packages_str, base_img),
            clone,
            f"WORKDIR /home/{repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
            Image._HARDENING_BLOCK.rstrip("\n"),
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


def _per_pr_dockerfile(image: Image) -> str:
    base = image.dependency()
    copy_lines = "\n".join(f"COPY {f.name} /home/" for f in image.files())
    return f"""FROM {base.image_full_name()}

{copy_lines}

RUN bash /home/prepare.sh
"""


class ServerlessMochaImageDefault(Image):
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
        return ServerlessImageBase(self.pr, self._config)

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
""",
            ),
            File(
                ".",
                "fix-deps.sh",
                """#!/bin/bash
if [ -f package.json ] && command -v python3 &>/dev/null; then
    python3 -c "
import json, sys, re
with open('package.json') as f:
    d = json.load(f)
changed = False
dead = ['phantomjs', 'phantomjs-prebuilt']
for section in ['dependencies', 'devDependencies']:
    if section not in d:
        continue
    for dep in dead:
        if dep in d[section]:
            del d[section][dep]
            changed = True
    for k, v in list(d[section].items()):
        if isinstance(v, str) and 'github.com' in v and not v.startswith('git'):
            del d[section][k]
            changed = True
if changed:
    with open('package.json', 'w') as f:
        json.dump(d, f, indent=2)
" 2>/dev/null
    rm -rf node_modules/phantomjs node_modules/phantomjs-prebuilt 2>/dev/null
    rm -f package-lock.json npm-shrinkwrap.json 2>/dev/null
fi
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

bash /home/fix-deps.sh
npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true

md5sum package.json > /home/.pkg-manifest.md5 2>/dev/null || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}

export CI=true SLS_IGNORE_WARNING='*'

if ! md5sum -c --status /home/.pkg-manifest.md5 2>/dev/null; then
    bash /home/fix-deps.sh
    npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
fi
bash /home/run-tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --exclude='*package-lock.json' --exclude='*npm-shrinkwrap.json' --exclude='*yarn.lock' --exclude='*pnpm-lock.yaml' --whitespace=nowarn /home/test.patch

export CI=true SLS_IGNORE_WARNING='*'

if ! md5sum -c --status /home/.pkg-manifest.md5 2>/dev/null; then
    bash /home/fix-deps.sh
    npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
fi
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --exclude='*package-lock.json' --exclude='*npm-shrinkwrap.json' --exclude='*yarn.lock' --exclude='*pnpm-lock.yaml' --whitespace=nowarn /home/test.patch /home/fix.patch

export CI=true SLS_IGNORE_WARNING='*'

if ! md5sum -c --status /home/.pkg-manifest.md5 2>/dev/null; then
    bash /home/fix-deps.sh
    npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
fi
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                "#!/bin/bash\n"
                "set -o pipefail\n"
                f"cd /home/{self.pr.repo}\n"
                "\n"
                "npm test 2>&1 | tee /home/mocha-output.log\n"
                "\n"
                "if ! grep -qE '^[[:space:]]*[0-9]+ (passing|failing|pending)' "
                "/home/mocha-output.log; then\n"
                '    echo "FATAL: mocha emitted no summary line -- the test runner '
                'failed to start."\n'
                "    exit 1\n"
                "fi\n"
                "exit 0\n",
            ),
        ]

    def dockerfile(self) -> str:
        return _per_pr_dockerfile(self)


class ServerlessJestImageDefault(Image):
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
        return ServerlessImageBase(self.pr, self._config)

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
""",
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

rm -f package-lock.json
npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}

npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
bash /home/run-tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --exclude='*package-lock.json' --exclude='*npm-shrinkwrap.json' --exclude='*yarn.lock' --exclude='*pnpm-lock.yaml' --whitespace=nowarn /home/test.patch

npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --exclude='*package-lock.json' --exclude='*npm-shrinkwrap.json' --exclude='*yarn.lock' --exclude='*pnpm-lock.yaml' --whitespace=nowarn /home/test.patch /home/fix.patch

npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
cd /home/{pr.repo}

for pkg_dir in packages/sf-core packages/serverless packages/engine; do
    if [ -d "$pkg_dir" ] && [ -f "$pkg_dir/package.json" ]; then
        pkg_name=$(python3 -c "import json; print(json.load(open('$pkg_dir/package.json')).get('name',''))" 2>/dev/null)
        if [ -n "$pkg_name" ]; then
            NODE_OPTIONS="--experimental-vm-modules" NODE_NO_WARNINGS=1 npm run test --workspace="$pkg_name" -- --testPathIgnorePatterns=integration || true
        fi
    fi
done
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        return _per_pr_dockerfile(self)


@Instance.register("serverless", "serverless")
class Serverless(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def _is_mocha_era(self) -> bool:
        return self.pr.number <= MOCHA_ERA_MAX_NUMBER

    def dependency(self) -> Optional[Image]:
        if self._is_mocha_era:
            return ServerlessMochaImageDefault(self.pr, self._config)
        return ServerlessJestImageDefault(self.pr, self._config)

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
        if self._is_mocha_era:
            return self._parse_log_mocha(test_log)
        return self._parse_log_jest(test_log)

    def _parse_log_mocha(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log)

        suite_stack: list[tuple[int, str]] = []
        occurrences: dict[str, int] = {}

        def qualify(title: str) -> str:
            title = _DURATION_RE.sub("", title).strip()
            name = " > ".join([t for _, t in suite_stack] + [title])
            name = _VOLATILE_RE.sub("<n>", name)
            seen = occurrences[name] = occurrences.get(name, 0) + 1
            return name if seen == 1 else f"{name} #{seen}"

        for raw_line in clean_log.split("\n"):
            line = raw_line.rstrip()
            if not line.strip():
                continue

            if _SUMMARY_RE.match(line):
                break

            indent = len(line) - len(line.lstrip(" "))
            text = line.strip()

            if indent < 2:
                m = _EMBEDDED_PASS_RE.match(line)
                if not m:
                    continue
                text = m.group(1)
                indent = 2

            m = _PASS_RE.match(text)
            if m:
                passed_tests.add(qualify(m.group(1)))
                continue

            m = _INLINE_FAIL_RE.match(text)
            if m:
                failed_tests.add(qualify(m.group(1)))
                continue

            m = _PENDING_RE.match(text)
            if m:
                skipped_tests.add(qualify(m.group(1)))
                continue

            while suite_stack and suite_stack[-1][0] >= indent:
                suite_stack.pop()
            expected_indent = suite_stack[-1][0] + 2 if suite_stack else 2
            if indent <= expected_indent:
                suite_stack.append((indent, text))

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    def _parse_log_jest(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        clean_log = _ANSI_RE.sub("", test_log)

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re.match(r"[✓✔]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$", line)
            if m:
                test_name = m.group(1).strip()
                if test_name and test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            m = re.match(r"[✕✗×]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$", line)
            if m:
                test_name = m.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                continue

            m = re.match(r"FAIL\s+(\S+)", line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re.match(r"○\s+(.*)", line)
            if m:
                test_name = m.group(1).strip()
                if test_name:
                    skipped_tests.add(test_name)

        test_summary = re.search(r"Tests:\s+(.+)", clean_log)
        if test_summary:
            summary_text = test_summary.group(1)
            passed_match = re.search(r"(\d+)\s+passed", summary_text)
            failed_match = re.search(r"(\d+)\s+failed", summary_text)
            skipped_match = re.search(r"(\d+)\s+skipped", summary_text)

            if passed_match and int(passed_match.group(1)) > 0 and not passed_tests:
                passed_tests.add("ToTal_Test")
            if failed_match and int(failed_match.group(1)) > 0 and not failed_tests:
                failed_tests.add("ToTal_Test")
            if skipped_match and int(skipped_match.group(1)) > 0 and not skipped_tests:
                skipped_tests.add("ToTal_Pending")

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


import json as _sls_json  # noqa: E402


def _sls_number_interval_from_bundle(bundle) -> str:
    seen = set()
    members = []
    for n in bundle:
        if n not in seen:
            seen.add(n)
            members.append(str(n))
    return "-".join(members)


if not getattr(PullRequest, "_serverless_ni_shim", False):
    _sls_orig_from_json = PullRequest.from_json.__func__

    def _sls_from_json(cls, json_str):
        pr = _sls_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "serverless"
                and getattr(pr, "repo", "") == "serverless"
                and not getattr(pr, "number_interval", "")
            ):
                prs = (_sls_json.loads(json_str) or {}).get("prs_in_bundle") or []
                if prs:
                    pr.number_interval = _sls_number_interval_from_bundle(prs)
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_sls_from_json)
    PullRequest._serverless_ni_shim = True


if not getattr(Instance, "_serverless_route_shim", False):
    _sls_orig_create = Instance.create.__func__

    def _sls_create(cls, pr, config, *args, **kwargs):
        try:
            return _sls_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == "serverless"
                and getattr(pr, "repo", "") == "serverless"
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_sls_create)
    Instance._serverless_route_shim = True
