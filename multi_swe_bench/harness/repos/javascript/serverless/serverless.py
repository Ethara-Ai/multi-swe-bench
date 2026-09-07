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


# =============================================================================
# MITM proxy / cert injection - applied directly in this registry file.
#
# These literals mirror DockerfileEnhancer._PROXY_ARGS / _ENV_BLOCK /
# _CERT_SYMLINKS / _MITM_MOUNT in multi_swe_bench/harness/image.py. They are
# hand-maintained here on instruction; if the image.py constants change this
# copy must be updated to match, or the two will drift.
# =============================================================================

_SLS_PROXY_ARGS = "\n".join(
    [
        'ARG http_proxy=""',
        'ARG https_proxy=""',
        'ARG HTTP_PROXY=""',
        'ARG HTTPS_PROXY=""',
        'ARG no_proxy="localhost,127.0.0.1,::1"',
        'ARG NO_PROXY="localhost,127.0.0.1,::1"',
        'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"',
    ]
)

_SLS_ENV_BLOCK = "\n".join(
    [
        "ENV DEBIAN_FRONTEND=noninteractive \\",
        "    LANG=C.UTF-8 \\",
        "    TZ=UTC \\",
        "    http_proxy=${http_proxy} \\",
        "    https_proxy=${https_proxy} \\",
        "    HTTP_PROXY=${HTTP_PROXY} \\",
        "    HTTPS_PROXY=${HTTPS_PROXY} \\",
        "    no_proxy=${no_proxy} \\",
        "    NO_PROXY=${NO_PROXY} \\",
        "    SSL_CERT_FILE=${CA_CERT_PATH} \\",
        "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\",
        "    CURL_CA_BUNDLE=${CA_CERT_PATH}",
    ]
)

_CA = "/etc/ssl/certs/ca-certificates.crt"

_SLS_CERT_SYMLINKS = "\n".join(
    [
        "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls "
        "/etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\",
        f"    ln -sf {_CA} /etc/pki/tls/certs/ca-bundle.crt && \\",
        f"    ln -sf {_CA} /etc/ssl/cert.pem && \\",
        f"    ln -sf {_CA} /etc/ssl/ca-bundle.pem && \\",
        f"    ln -sf {_CA} /etc/pki/tls/cacert.pem && \\",
        f"    ln -sf {_CA} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\",
        f"    ln -sf {_CA} /etc/ssl/certs/ca-bundle.crt",
    ]
)

# Build-time MITM CA install. Latent in image.py; wired in here so a CA passed
# as `docker build --secret id=mitm_ca,src=<ca.crt>` lands in the trust store.
# required=0 keeps the build working when no secret is supplied.
_SLS_MITM_MOUNT = "\n".join(
    [
        "RUN --mount=type=secret,id=mitm_ca,required=0 \\",
        "    if [ -f /run/secrets/mitm_ca ]; then \\",
        "        cp /run/secrets/mitm_ca "
        "/usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \\",
        "    fi",
    ]
)


def _sls_infrastructure_block(pr, base_img: str) -> str:
    """MITM + build ARGs + LABEL, emitted from this file rather than image.py."""
    repo_url = f"https://github.com/{pr.org}/{pr.repo}.git"
    build_args = "\n".join(
        [
            "ARG TARGETARCH",
            f'ARG REPO_URL="{repo_url}"',
            "ARG BASE_COMMIT",
            "",
            _SLS_PROXY_ARGS,
        ]
    )
    label_block = "\n".join(
        [
            f'LABEL org.opencontainers.image.title="{pr.org}/{pr.repo}" \\',
            f'      org.opencontainers.image.description="{pr.org}/{pr.repo} Docker image" \\',
            f'      org.opencontainers.image.source="https://github.com/{pr.org}/{pr.repo}" \\',
            '      org.opencontainers.image.authors="https://www.ethara.ai/"',
        ]
    )
    return "\n\n".join(
        [build_args, _SLS_ENV_BLOCK, label_block, _SLS_CERT_SYMLINKS, _SLS_MITM_MOUNT]
    )


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
        # §3: ONE shared base image, not one per PR. The base is identical for
        # every bundle (same node:18 + same packages + full clone), so the tag
        # must not vary with pr.number.
        return "base"

    def workdir(self) -> str:
        return "base"

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
            _sls_infrastructure_block(self.pr, base_img),
            "WORKDIR /home/",
            self._get_apt_update_command(packages_str, base_img),
            clone,
            f"WORKDIR /home/{repo}",
            # §3: light hardening only — the base keeps FULL history and is not
            # pinned to any base.sha, which is what lets a single base be shared
            # by every PR. The strict git-strip happens in the PR layer (§4).
            'RUN git remote remove origin 2>/dev/null || true; \\\n'
            "    git config --local fetch.recurseSubmodules false; \\\n"
            '    git config --local remote.pushDefault ""',
            "WORKDIR /home/",
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


def _per_pr_dockerfile(image: Image) -> str:
    base = image.dependency()
    copy_lines = "\n".join(f"COPY {f.name} /home/" for f in image.files())
    # §4: prepare.sh checks out this PR's base.sha, then the canonical hardening
    # block strips git history. The block is emitted with the LITERAL base.sha
    # (not ${BASE_COMMIT}) because the PR layer declares no such build ARG.
    hardening = Image._HARDENING_BLOCK.replace(
        "${BASE_COMMIT}", image.pr.base.sha
    ).rstrip("\n")
    return f"""FROM {base.image_full_name()}

{copy_lines}

RUN bash /home/prepare.sh

WORKDIR /home/{image.pr.repo}

{hardening}
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# One key per bundle (bundle-level, NOT pr-level); every dash-joined
# number_interval in serverless__serverless_usable114.jsonl must resolve via
# Instance.create() -> f"{org}/{number_interval}". Data-derived: regenerate
# when the bundles change. Single-era repo -> all keys point to Serverless,
# which selects mocha/jest internally by pr.number.
_BUNDLE_NIS_Serverless = [
    "10231-10576-10636-10639-10652-10653-10654-10655-10657-10662-10663-10664-10667-10668-10678",
    "10259-10416-10424-10427-10428-10430-10435-10447-10452-10461-10466-10478-10479-10480",
    "10263-10285-10288-10289-10290-10296-10297-10299-10302",
    "10273-10291-10304-10306-10309-10313-10315-10317-10320-10328-10344",
    "10359-10371-10380-10382-10383-10384-10388-10389-10392-10393-10395-10396-10406-10409-10413-10415-10417-10420-10422",
    "10830-10837-10839-10842-10846-10850",
    "10845-10885-10895-10898",
    "10933-10936-10937",
    "10950-10956-10973-10974",
    "11084-11135-11137-11138-11152-11154-11155-11164-11168-11178-11189-11194-11196-11197-11200-11203-11209-11212-11216-11217-11219-11220-11221",
    "11100-11105-11106-11107",
    "11201-11223-11261-11265-11270-11271-11272-11277-11279-11282-11283-11284-11292-11293-11294-11299-11307-11312-11315-11324-11340-11341",
    "11227-11228-11235-11236-11237",
    "11434-11453-11459-11461-11470-11483-11485-11487-11489-11490-11494-11496",
    "11555-11558-11576-11581-11582-11586-11587-11589-11593-11600-11608-11615-11617-11622-11629-11630",
    "11633-11636-11639-11644-11645-11650-11656-11658-11659-11660-11671-11678-11683-11685-11690-11693-11695-11700-11705-11706-11709",
    "11712-11714-11715-11720-11747-11760-11766-11767",
    "11768-11769-11771",
    "11772-11774-11777-11778-11779-11782-11785-11788-11789-11790-11791-11793-11794-11797-11798-11799-11801-11803-11804-11805-11809-11810-11815-11816-11817-11818-11819-11821-11822-11823-11827-11832-11833-11834-11835-11838-11844-11845-11847-11848-11849-11851-11853-11854-11855",
    "11863-11875-11885-11896-11898-11899",
    "11888-11907-11960-11974-11976-11980-11985-11987-11994-11995-11999",
    "11996-12001",
    "12055-12057-12058",
    "12063-12125-12129-12138-12147-12148-12149-12151-12153-12154-12155-12157-12158-12159",
    "12274-12287-12386",
    "3344-3412-3443-3446-3454-3457-3466-3468-3469-3470-3471-3476-3477-3478-3479-3480-3483-3494-3496-3503-3505-3517-3524",
    "3433-3440",
    "3445-3507-3532-3533-3541-3543-3548-3549-3550-3551-3553-3554-3562-3563-3572-3575-3583-3584-3585",
    "3534-3600-3612-3614-3617-3620-3624",
    "3537-3558-3571-3592-3609-3618-3622-3632-3633-3634-3636-3642-3644-3645-3660-3664-3666-3670-3673-3674",
    "3564-3647-3654-3657-3668-3672-3675-3678-3680-3681-3682-3687-3692-3699-3700-3701-3702-3703-3704-3705-3706-3709-3712-3717-3727-3732-3743-3749-3753-3754",
    "3722-3808-3842-3856-3876-3888-3889-3902-3903-3904-3907-3908-3909-3910-3911-3912-3921-3931-3933-3935-3939-3940-3950-3963",
    "3798-3810-3825-3829-3835-3837-3843-3844-3846-3848-3849-3850-3854-3855",
    "3830-3838-3839-3857-3859-3860-3863-3870-3872-3873-3877-3879-3880-3884-3890-3897-3898",
    "3832-3864-3866-3919-3924-3980-4026-4036-4047-4050-4056-4058-4059-4097-4114-4115-4122-4126-4127-4129-4130-4132-4134-4137-4140-4142-4145-4152-4153-4154-4155-4157-4160-4162-4164-4167-4175-4177",
    "3959-3960-3962-3971-3973-3975-3982-3996-4004",
    "3970-4011-4019-4033-4034-4038-4044-4051-4054-4060-4077-4081-4082-4087-4092-4095-4101",
    "4062-4120-4139-4174-4178-4181-4184-4186-4189-4192-4193-4204-4205-4208-4210-4211-4212",
    "4118-4144-4293-4429-4433-4451-4460-4465-4468-4470-4474-4475-4478-4480-4486-4487-4492-4497-4499-4503-4505-4510-4515-4518-4521-4525-4529-4531-4532-4533-4534-4537-4541-4547-4552-4559-4560-4561-4573-4574-4575-4579-4581-4594",
    "4912-5086-5108-5109-5112-5114-5126-5127-5129-5131-5132-5134-5141-5163",
    "5079-5222-5229-5236-5243-5258-5266-5268-5276-5287",
    "5265-5290",
    "5274-5297-5351-5443-5509-5743-5840-5849-5850-5854-5859-5860-5861-5862-5863-5865-5867-5872-5880-5883-5884-5885-5889-5894-5899-5909-5911-5912-5924",
    "5312-5378-5411-5437-5554-5601-5604-5607-5613-5618-5620-5623-5625-5627-5628-5631-5635-5638-5639-5649-5650-5651-5653-5659-5665-5666-5670",
    "5335-5640-5898-5926-5936-5937-5941-5943-5944-5948-5949-5957-5963-5968",
    "5531-5662-5715-5723-5726-5727-5733-5736-5738-5741",
    "5552-5559-5560-5562-5565-5566-5571-5574-5575-5579-5585-5587-5592-5594",
    "5692-5954-5964-5970-5971-5973-5977-5988-5989-5991-5992-5994-5997-5998-6010-6011-6013-6018-6023-6025",
    "6051-6705-6924-6926-6930-6934-6935-6936-6937-6938-6940-6941-6945-6948-6954-6955-6960-6962-6963-6966-6967-6971-6972-6975-6978-6982-6983-6984-6985",
    "6113-6137-6138-6142-6143-6147-6158",
    "6200-6225-6228-6231-6244-6253-6254-6255-6256-6258-6261-6265-6268-6272-6275-6280-6285-6286-6288-6292-6293-6305",
    "6238-6284-6290-6294-6299-6300-6301-6306-6308-6309-6310-6317-6318-6319-6321-6322-6323-6324-6325-6327-6330-6332-6343-6354",
    "6344-6345-6350-6362-6365-6366-6367-6372-6373-6377-6378-6379-6385-6386-6387-6388-6395-6398-6404",
    "6383-6869-6879-6912-6916-6919-6920-6922-6928",
    "6417-6449-6467-6471-6473-6477-6478-6479-6483-6484-6488-6489-6491-6496-6500-6501-6502-6506-6511-6513-6517-6518-6519-6520-6526-6531-6534-6535-6537-6540",
    "6419-6422-6427-6428",
    "6512-6566-6601-6604-6606-6608-6611-6615-6616-6618-6622-6624-6625-6631-6635-6642-6645-6646-6652-6653-6654-6655-6663-6665",
    "6697-6747-6823-6870-6877-6878-6880-6883-6886-6889-6896-6898-6907",
    "7102-7120-7122-7126-7127",
    "7105-7109-7158-7164-7170-7175-7177-7179-7180-7182-7187-7191-7193-7195-7196-7197-7200-7205",
    "7108-7113-7116-7118-7119",
    "7156-7262-7270-7272-7273-7274-7288-7293",
    "7218-7222",
    "7233-7381-7445-7465-7467-7475-7476-7484-7488-7490",
    "7261-7386-7412-7415-7417-7418-7419-7420-7421-7426-7430-7431-7432-7439",
    "7372-7434-7452-7526-7532-7538-7585-7588-7604",
    "7482-7552-7562-7564-7587-7607-7612-7613-7617-7620-7623-7625",
    "7540-7729-7738-7741-7748-7754-7755-7757-7759-7760-7761-7763-7764-7765-7766-7772-7775-7778-7782-7784-7786-7787-7789-7798-7799-7804",
    "7576-7632-7638-7639-7644-7648-7661-7663-7665-7666-7667-7668-7672-7673-7674-7682-7684-7688-7692",
    "7622-7680-7715-7739-7742-7743-7750",
    "7677-7717-7719-7720-7721-7722-7725-7728-7730-7731-7735",
    "7694-7698-7699-7701-7703-7707-7708-7709-7712",
    "7851-7881-7882-7893-7897-7901-7902-7903-7905-7914-7915-7918-7919-7924-7934-7940-7941-7942",
    "7930-7945-7947-7955-7956-7957-7958-7961-7963-7964-7965-7968-7969-7972",
    "8044-8045",
    "8219-8297-8299-8308-8309-8310-8314-8319-8320-8323-8326-8329-8331-8332-8337-8338-8341-8343-8349-8353-8354",
    "8289-8290-8291-8295-8298-8307",
    "8315-8710-8719-8726-8728-8732-8735-8736-8741-8743-8744-8745-8746-8747-8749-8751-8754-8755-8757-8759-8760-8762-8765",
    "8335-8342-8351-8372-8380-8384-8385-8388-8389",
    "8437-8913-8975-8997-9000-9003",
    "8466-8470-8471-8474-8476-8482",
    "8472-8495-8496-8501-8505-8507-8508-8510-8520-8522-8527-8530",
    "8484-8486",
    "8581-9166-9171-9175-9177-9181-9182-9191-9192-9193-9195-9200-9201-9202-9203",
    "8698-8725-8748-8753-8767-8768-8770-8774-8775-8776-8777-8778-8780-8785-8786-8792-8793-8796-8797",
    "8721-8829-8844-8845-8846-8847-8849-8853-8858-8863-8864-8865-8866",
    "8840-9603-9847-9884-9892-9895-9903-9905-9906-9908-9910-9914-9915-9918-9923-9926-9928-9930-9933-9934-9935-9936-9938-9946",
    "8850-9066-9078-9079-9094-9097-9102-9103-9106-9110-9111-9115",
    "8961-8962-8964-8966",
    "8984-8992-8998-9002-9008-9009-9011",
    "9074-9076",
    "9133-9135-9140-9142-9147-9148-9151-9152-9159-9160-9161-9164-9168-9169",
    "9198-9216-9235-9248-9254-9256",
    "9222-9238-9266-9267-9270-9280-9281-9282-9284-9287-9292-9298-9300-9301-9304-9307-9308-9310-9318-9320-9323-9324-9327-9329",
    "9313-9319-9356-9358",
    "9404-9411-9416-9446-9447-9448-9449-9454-9455-9459",
    "9410-9445-9464-9472-9474",
    "9503-9504-9505-9506",
    "9507-9510-9513-9514-9518-9519-9520",
    "9551-9592-9600-9601-9602-9617-9618-9622",
    "9582-9587-9588-9591-9593",
    "9615-9728-9734-9735-9736-9741-9743-9744-9747-9748-9749",
    "9623-9628-9629-9632",
    "9644-9666-9675-9678-9679-9682-9684-9687-9690-9692-9693-9695-9698-9699-9701-9702",
    "9680-9683-9770-9780-9786-9790-9793-9798-9804",
    "9706-11893-11905-11922-11935-11938-11940-11941-11967-11972",
    "9712-9714-9716-9720-9721-9722",
    "9758-9839-9859-9863-9865-9866-9869-9872-9874-9877-9878-9879-9881",
    "9760-9778-9811-9816-9824-9825-9826-9827-9833-9834",
    "9835-9838-9845-9849-9850-9851-9854-9857",
    "9870-9882-9887-9888-9889-9890-9891-9894-9896-9901-9902",
    "9912-10060-10070-10071-10076-10077-10078-10079-10080-10083-10086-10087-10093-10096-10100-10103",
    "9919-9945-10097-10107-10111-10112-10114-10115-10120-10121-10122",
    "9942-10007-10014-10015-10016-10019-10023-10024",
]

for _ni in _BUNDLE_NIS_Serverless:
    Instance.register("serverless", _ni)(Serverless)

import json as _sls_json  # noqa: E402
import logging as _sls_logging  # noqa: E402
import re as _sls_re  # noqa: E402

_sls_log = _sls_logging.getLogger(__name__)

# number_interval must be the EXACT dash-joined PR list, never a range.
#   prs_in_bundle [146, 147, 150, 155, 157]  ->  "146-147-150-155-157"
# A range ("146-157") is wrong: it implies 146..157, PRs that may not be in the
# bundle. Bundles are frequently non-contiguous, so only the explicit list is valid.
_SLS_NI_RE = _sls_re.compile(r"^\d+(?:-\d+)*$")


def _sls_number_interval_from_bundle(bundle) -> str:
    """Dash-join prs_in_bundle, preserving source order, dropping duplicates."""
    seen = set()
    members = []
    for n in bundle:
        if n not in seen:
            seen.add(n)
            members.append(str(n))
    return "-".join(members)


def _sls_is_valid_number_interval(ni: str) -> bool:
    """Well-formed dash-joined list of distinct PR numbers."""
    if not ni or not _SLS_NI_RE.match(ni):
        return False
    parts = ni.split("-")
    return len(set(parts)) == len(parts)


if not getattr(PullRequest, "_serverless_ni_shim", False):
    _sls_orig_from_json = PullRequest.from_json.__func__

    def _sls_from_json(cls, json_str):
        pr = _sls_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "serverless"
                and getattr(pr, "repo", "") == "serverless"
            ):
                raw = _sls_json.loads(json_str) or {}
                prs = raw.get("prs_in_bundle") or []
                current = getattr(pr, "number_interval", "") or ""
                if prs:
                    # prs_in_bundle is authoritative. Always rebuild from it so a
                    # range-style or stale value cannot survive into the output.
                    derived = _sls_number_interval_from_bundle(prs)
                    if current and current != derived:
                        _sls_log.warning(
                            "serverless pr-%s: number_interval %r does not match "
                            "prs_in_bundle; rewriting to %r",
                            getattr(pr, "number", "?"), current, derived,
                        )
                    pr.number_interval = derived
                elif not current:
                    # No bundle info at all. Deliberately NOT falling back to the
                    # bare lead PR: that would silently emit an incomplete bundle.
                    _sls_log.warning(
                        "serverless pr-%s: no number_interval and no prs_in_bundle; "
                        "backfill from the source _lht_final.jsonl by PR number",
                        getattr(pr, "number", "?"),
                    )
                elif not _sls_is_valid_number_interval(current):
                    _sls_log.warning(
                        "serverless pr-%s: malformed number_interval %r "
                        "(expected dash-joined PR numbers)",
                        getattr(pr, "number", "?"), current,
                    )
        except Exception:
            _sls_log.debug("number_interval normalisation failed", exc_info=True)
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
