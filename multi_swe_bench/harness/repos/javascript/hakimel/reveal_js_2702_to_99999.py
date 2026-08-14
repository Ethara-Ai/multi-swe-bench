"""reveal.js harness -- Gulp era, routing key `reveal_js_2702_to_99999`.

Superseded for NEW data by reveal_js_2746_to_99999 (the delivered bundles put
the Grunt/Gulp boundary at #2745/#2746, matching reveal.js's v4.0 switch), but
the key stays live for any existing jsonl that references it.

Hardening split (see image.py) -- identical contract to reveal_js_2746_to_99999:
ImageBase is SHARED across every PR on this key, so it opts out of
DockerfileEnhancer via `# syntax=docker/dockerfile:1.6` (image.py:281) and
keeps full git history with light hardening only. Letting the enhancer inject
Image._HARDENING_BLOCK here would pin the shared tag to whichever PR built it
first and break `git checkout` for all the others. ImageDefault is PER-PR, so
it emits the canonical block by hand after pinning ${BASE_COMMIT}, and declares
`ARG BASE_COMMIT="<sha>"` with a default because per-PR images receive no build
args (build_dataset.py:614-620).
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# GitHub's exact casing, and the path the clone + WORKDIR create.
REPO_DIR = "reveal.js"

# Mirrors the default set in Image.dockerfile(), which the hand-written base
# Dockerfile below bypasses. fonts-liberation because without a font package
# headless chromium renders blank glyphs and reveal.js layout assertions fail.
_APT_COMMAND = (
    "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    git \\\n"
    "    chromium \\\n"
    "    fonts-liberation \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    wget \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)

# Configure git globally BEFORE cloning; the arm64 half of a multi-arch build
# runs under QEMU where a stalled transfer easily trips libcurl's low-speed
# timeout and dies with `gnutls_handshake() failed`.
_GIT_RESILIENCY = (
    "RUN git config --global http.version HTTP/1.1 \\\n"
    "    && git config --global http.postBuffer 1048576000 \\\n"
    "    && git config --global http.lowSpeedLimit 0 \\\n"
    "    && git config --global http.lowSpeedTime 999999 \\\n"
    "    && git config --global core.compression 0 \\\n"
    "    && git config --global submodule.fetchJobs 1"
)

# LIGHT hardening only -- drop the origin remote so the image carries no
# upstream to re-fetch from. The canonical Image._HARDENING_BLOCK deliberately
# does NOT run here; see module docstring.
_LIGHT_HARDENING = (
    "RUN git remote remove origin 2>/dev/null || true; \\\n"
    "    git config --local fetch.recurseSubmodules false; \\\n"
    '    git config --local remote.pushDefault ""; \\\n'
    "    git config --local gc.auto 0"
)


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

    def dependency(self) -> Union[str, "Image"]:
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return "base-gulp"

    def workdir(self) -> str:
        return "base-gulp"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        if isinstance(base_img, Image):
            base_img = base_img.image_full_name()
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        if self.config.need_clone:
            # Retry x3 so one flaky handshake does not discard the build. The
            # bare `git clone "${REPO_URL}" /home/<repo>` form is preserved
            # inside the loop so the reference-format marker still matches.
            fetch = (
                "RUN for i in 1 2 3; do \\\n"
                f'        git clone "${{REPO_URL}}" /home/{REPO_DIR} && break; \\\n'
                '        echo "clone attempt $i failed; retrying"; \\\n'
                f"        rm -rf /home/{REPO_DIR}; \\\n"
                "        sleep 10; \\\n"
                "    done; \\\n"
                f"    test -d /home/{REPO_DIR}/.git"
            )
        else:
            fetch = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        # Hand-written infra block: the `# syntax` directive opts this
        # Dockerfile out of DockerfileEnhancer entirely (see module docstring),
        # so nothing below is injected for us and it must stay in sync with the
        # enhancer's reference format.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            "ARG TARGETARCH\n" f'ARG REPO_URL="{repo_url}"\n' "ARG BASE_COMMIT",
            "ENV DEBIAN_FRONTEND=noninteractive \\\n"
            "    LANG=C.UTF-8 \\\n"
            "    LC_ALL=C.UTF-8 \\\n"
            "    TZ=UTC",
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"',
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(_APT_COMMAND)
        sections.append(
            "ENV PUPPETEER_SKIP_DOWNLOAD=true\n"
            "ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium\n"
            "ENV CHROME_BIN=/usr/bin/chromium"
        )
        sections.append(_GIT_RESILIENCY)
        sections.append(fetch)
        sections.append(f"WORKDIR /home/{REPO_DIR}")
        sections.append(_LIGHT_HARDENING)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


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

    def dependency(self) -> Image | None:
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

cd /home/{repo_dir}
git reset --hard
git checkout ${{BASE_COMMIT}}
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"

export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)

npm install || true

# Ensure --no-sandbox is set for running as root in Docker
if grep -q "puppeteerArgs" gulpfile.js 2>/dev/null; then
    if ! grep -q "no-sandbox" gulpfile.js 2>/dev/null; then
        sed -i "s/puppeteerArgs: \\[/puppeteerArgs: ['--no-sandbox', '--disable-setuid-sandbox', /" gulpfile.js
    fi
fi

# Upgrade node-qunit-puppeteer if bundled version < 2.1 (crashes with modern Chromium)
NQP_VER=$(node -e "try{{console.log(require('./node_modules/node-qunit-puppeteer/package.json').version)}}catch(e){{console.log('0.0.0')}}" 2>/dev/null)
NQP_MINOR=$(echo "$NQP_VER" | cut -d. -f2)
if [ "$(echo "$NQP_VER" | cut -d. -f1)" = "2" ] && [ "$NQP_MINOR" -lt 1 ] 2>/dev/null; then
    npm install node-qunit-puppeteer@2.2.1 --save-dev 2>/dev/null || true
fi

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
npx gulp test 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
git apply --reject --whitespace=nowarn /home/test.patch || true
npm install || true
if grep -q "puppeteerArgs" gulpfile.js 2>/dev/null; then
    if ! grep -q "no-sandbox" gulpfile.js 2>/dev/null; then
        sed -i "s/puppeteerArgs:\\s*\\[/puppeteerArgs: ['--no-sandbox', /" gulpfile.js || true
    fi
fi
npx gulp test 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
git apply --reject --whitespace=nowarn /home/test.patch /home/fix.patch || true
npm install || true
if grep -q "puppeteerArgs" gulpfile.js 2>/dev/null; then
    if ! grep -q "no-sandbox" gulpfile.js 2>/dev/null; then
        sed -i "s/puppeteerArgs:\\s*\\[/puppeteerArgs: ['--no-sandbox', /" gulpfile.js || true
    fi
fi
npx gulp test 2>&1 || true

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

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{REPO_DIR}

{copy_commands}RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete every ref, expire the
        # reflog, gc/repack, drop alternates, then assert; plus the submodule
        # strip). Concatenated raw rather than interpolated through the f-string
        # above so its ${BASE_COMMIT} and %(refname) tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("hakimel", "reveal_js_2702_to_99999")
class REVEAL_JS_2702_TO_99999(Instance):

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
        """Parse node-qunit-puppeteer test output from gulp test.

        Captured output format (verified in Docker):
          ✔ test/test-state.html [6/6] in 32ms
          ✔ test/test.html [158/158] in 450ms
          ! test/test-foo.html [5/8] in 120ms
          ✔ Passed 293 tests
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

        # ✔ test/test-state.html [6/6] in 32ms
        re_file_pass = re.compile(
            r"^[✔✓]\s+(.+?)\s+\[(\d+)/(\d+)\]\s+in\s+\d+ms$"
        )
        # ! test/test-foo.html [5/8] in 120ms
        re_file_fail = re.compile(
            r"^[!✗✕]\s+(.+?)\s+\[(\d+)/(\d+)\]\s+in\s+\d+ms$"
        )

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            m = re_file_pass.match(line)
            if m:
                # Trust the RATIO, not the glyph. Crediting on the check mark
                # alone banks a partial failure ("✔ test/test.html [292/293]")
                # as a full pass, which is a free F2P the moment the assertion
                # count moves.
                if m.group(2) == m.group(3):
                    passed_tests.add(m.group(1))
                else:
                    failed_tests.add(m.group(1))
                continue

            m = re_file_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

        for test in failed_tests:
            passed_tests.discard(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
