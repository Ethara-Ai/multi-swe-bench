import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    if not patch:
        return patch

    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    kept: list[str] = []
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        body = "\n".join(section)
        unusable = "Binary files" in body and "GIT binary patch" not in body
        if not unusable:
            kept.extend(section)

    for line in patch.split("\n"):
        if header.match(line):
            flush()
            section = [line]
        elif section:
            section.append(line)
        else:
            kept.append(line)
    flush()

    return "\n".join(kept)


class ChartJsImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "node:6-stretch"

    def image_tag(self) -> str:
        return "base-pr-1653-to-3959"

    def workdir(self) -> str:
        return "base-pr-1653-to-3959"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

# karma needs a real browser; Debian's chromium is the only arm64+amd64 build
# available for these releases. fonts-liberation keeps text metrics stable.
RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' \\
        -e 's|security.debian.org|archive.debian.org|g' \\
        -e '/stretch-updates/d' \\
        -e '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=false \\
        -o Acquire::AllowInsecureRepositories=true update && \\
    apt-get -o APT::Get::AllowUnauthenticated=true install -y \\
        --no-install-recommends --allow-unauthenticated \\
        git chromium fonts-liberation \\
    && rm -rf /var/lib/apt/lists/*

RUN printf '#!/bin/bash\\nexec /usr/bin/chromium --no-sandbox --headless --disable-gpu --disable-dev-shm-usage --disable-software-rasterizer --window-size=1280,1024 --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows --remote-debugging-port=9222 "$@"\\n' \\
        > /usr/local/bin/chromium-no-sandbox && \\
    chmod +x /usr/local/bin/chromium-no-sandbox

# Chart.js 2.0.0-beta declares `"color": "git://github.com/chartjs/color"`. That
# repo was renamed to chartjs/chartjs-color and the old URL now 404s, which
# aborts the entire npm install. Redirect it so npm resolves the dependency the
# repo itself declares; every package this config adds on top comes from the npm
# registry.
RUN git config --global url."https://github.com/".insteadOf "git://github.com/" && \\
    git config --global url."https://github.com/chartjs/chartjs-color".insteadOf "git://github.com/chartjs/color"

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ChartJsImageBaseNode10(ChartJsImageBase):
    def dependency(self) -> str | Image:
        return "node:10-buster"

    def image_tag(self) -> str:
        return "base-pr-4671-to-5841"

    def workdir(self) -> str:
        return "base-pr-4671-to-5841"


class ChartJsImageDefault(Image):
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
        if self.pr.number >= 4000:
            return ChartJsImageBaseNode10(self.pr, self._config)
        return ChartJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        if self.pr.number < 3000:
            # v2.0.0-beta lists `"color": "git://github.com/chartjs/color"` and
            # its gulpfile prepends ./node_modules/color/dist/color.min.js to the
            # karma file list. That repo was renamed to chartjs/chartjs-color, so
            # the original URL now 404s and npm aborts the whole install; the base
            # image's insteadOf rules make it resolve again. The renamed HEAD is
            # chartjs-color 2.4.x, which dropped Color#saturate() and fails eight
            # setHoverStyle specs, so the resolved tree is overwritten with the
            # contemporary color@0.10.1 and the dist bundle -- a build artefact
            # that was never committed to either repo -- is regenerated here.
            extra_install = """\
npm install --no-save karma-spec-reporter@0.0.32 color@0.10.1 browserify@13
if [ ! -d node_modules/karma-spec-reporter ]; then
    echo "ERROR: karma-spec-reporter did not install" >&2
    exit 1
fi

mkdir -p node_modules/color/dist
./node_modules/.bin/browserify node_modules/color/index.js \\
    --standalone Color -o node_modules/color/dist/color.min.js
if [ ! -s node_modules/color/dist/color.min.js ]; then
    echo "ERROR: color.min.js was not generated" >&2
    exit 1
fi"""
        else:
            extra_install = """\
npm install --no-save karma-spec-reporter@0.0.32
if [ ! -d node_modules/karma-spec-reporter ]; then
    echo "ERROR: karma-spec-reporter did not install" >&2
    exit 1
fi"""

        if self.pr.number < 4000:
            # karma 0.12 era: `gulp unittest` runs karma.conf.ci.js, which lists
            # Firefox and only reaches Chrome behind `if (process.env.TRAVIS)`.
            # Point browsers at the Chrome_travis_ci launcher that the file
            # already declares -- CHROME_BIN is the headless wrapper baked into
            # the base image -- and swap the progress reporter for spec, which is
            # the only reporter here that prints one line per test.
            karma_setup = """\
sed -i "s/browsers: \\['Firefox'\\]/browsers: ['Chrome_travis_ci']/" karma.conf.ci.js
sed -i "s/reporters: \\['progress', 'html'\\]/reporters: ['spec']/" karma.conf.ci.js
if ! grep -q "browsers: \\['Chrome_travis_ci'\\]" karma.conf.ci.js; then
    echo "ERROR: browser swap did not apply to karma.conf.ci.js" >&2
    exit 1
fi
if ! grep -q "reporters: \\['spec'\\]" karma.conf.ci.js; then
    echo "ERROR: reporter swap did not apply to karma.conf.ci.js" >&2
    exit 1
fi"""
        elif self.pr.number < 5000:
            # karma 1.7 era: karma.conf.js seeds browsers with Firefox and pushes
            # Chrome in the non-Travis branch. Empty the seed and make the push
            # headless; karma-chrome-launcher 2.1 is the first to ship the
            # ChromeHeadless launcher, so it is available here.
            karma_setup = """\
sed -i "s/browsers: \\['Firefox'\\]/browsers: []/g" karma.conf.js
sed -i "s/config.browsers.push('Chrome');/config.browsers.push('ChromeHeadless');/g" karma.conf.js
sed -i "s/'progress'/'spec'/g" karma.conf.js
if ! grep -q "ChromeHeadless" karma.conf.js; then
    echo "ERROR: browser swap did not apply to karma.conf.js" >&2
    exit 1
fi
if grep -qE "browsers: \\['Firefox'\\]|push\\('Chrome'\\)" karma.conf.js; then
    echo "ERROR: a non-headless browser is still configured in karma.conf.js" >&2
    exit 1
fi
if ! grep -q "'spec'" karma.conf.js; then
    echo "ERROR: reporter swap did not apply to karma.conf.js" >&2
    exit 1
fi"""
        else:
            # karma 3.x/4.x era: karma.conf.js drives two customLaunchers named
            # 'chrome' and 'firefox'. Drop firefox -- the base image ships no
            # Gecko -- and keep the chrome launcher, whose base 'Chrome' resolves
            # through CHROME_BIN to the headless wrapper.
            karma_setup = """\
sed -i "s/browsers: \\['chrome', 'firefox'\\]/browsers: ['chrome']/" karma.conf.js
sed -i "s/reporters: \\['progress', 'kjhtml'\\]/reporters: ['spec']/" karma.conf.js
if ! grep -q "browsers: \\['chrome'\\]" karma.conf.js; then
    echo "ERROR: browser swap did not apply to karma.conf.js" >&2
    exit 1
fi
if ! grep -q "reporters: \\['spec'\\]" karma.conf.js; then
    echo "ERROR: reporter swap did not apply to karma.conf.js" >&2
    exit 1
fi"""

        filtered_fix_patch = _sanitize_patch(self.pr.fix_patch)
        filtered_test_patch = _sanitize_patch(self.pr.test_patch)

        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{pr.repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "check_git_changes: clean tree at $(git rev-parse HEAD)"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install || true

{extra_install}

{karma_setup}
""".format(pr=self.pr, extra_install=extra_install, karma_setup=karma_setup),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox

cd /home/{pr.repo}
./node_modules/.bin/gulp unittest 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch
./node_modules/.bin/gulp unittest 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch /home/fix.patch
./node_modules/.bin/gulp unittest 2>&1
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # dependency() returns an Image, so DockerfileEnhancer.enhance() returns
        # this text verbatim (image.py:314) and nothing is injected. The clone
        # comes from the era base; this layer pins it to THIS PR's commit.
        #
        # No R12 recovery fetch is staged here. All ten base commits -- including
        # 1653/1662, which branch off the since-deleted `v2.0-dev` -- are
        # reachable from the era base's plain clone, so `git checkout --detach`
        # below finds every one of them. Verified: pr-1653 builds green without a
        # fetch step. Adding one would also be self-defeating, since the
        # hardening block immediately removes the remote it would fetch from.
        return f"""FROM {self.dependency().image_full_name()}

{copy_commands}
WORKDIR /home/{repo}

# Git stripping / hardening. Pins the tree to the base commit and reduces the
# repository to exactly that history, then asserts the four invariants:
# HEAD == base commit, no residual refs, no remotes, no unreachable objects.
RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

RUN bash /home/prepare.sh
"""


@Instance.register("chartjs", "Chart_js_5841_to_1653")
class CHART_JS_5841_TO_1653(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ChartJsImageDefault(self.pr, self._config)

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
        # Known, measured limitation: Chart.js declares a handful of duplicate
        # it() titles inside one describe -- two "should return false if arrays
        # are not the same" in helpers.core's arrayEquals block, two "Should
        # display information from user callbacks" in Core.Tooltip -- so jasmine
        # itself gives them one name and no id shape can separate them. Over the
        # 30 stage logs of this dataset that merges 27 of 13336 spec lines, and
        # the count is identical in run/test/fix for every PR, so R3 holds. The
        # merge is conservative: failure wins at the test stage while the fix
        # stage needs BOTH halves green, so a collision can only suppress a
        # transition, never invent one.
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_spec = re.compile(r"^(\s*)(✓|√|✗|×|x|-)\s+(.*\S)\s*$")
        re_suite = re.compile(r"^(\s*)(\S.*\S|\S)\s*$")
        re_noise = re.compile(
            r"^\s*(HeadlessChrome|Chrome|Chromium|Firefox|PhantomJS)\b"
            r"|^\s*(Executed|TOTAL|SUCCESS|FAILED|Finished|START|LOG|WARN|INFO|ERROR)\b"
            r"|^\s*\d+\)"
            r"|^\s*at\s"
            r"|^\s*\d+ (spec|test)s?, "
        )

        stack: list[tuple[int, str]] = []
        last_spec_indent: Optional[int] = None
        for raw in log.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue

            m = re_spec.match(line)
            if m:
                indent, mark, title = len(m.group(1)), m.group(2), m.group(3)
                last_spec_indent = indent
                prefix = "::".join(name for lvl, name in stack if lvl < indent)
                test_id = f"{prefix}::{title}" if prefix else title
                if mark in ("✓", "√"):
                    if test_id not in failed_tests:
                        skipped_tests.discard(test_id)
                        passed_tests.add(test_id)
                elif mark in ("✗", "×", "x"):
                    passed_tests.discard(test_id)
                    skipped_tests.discard(test_id)
                    failed_tests.add(test_id)
                else:
                    if test_id not in passed_tests and test_id not in failed_tests:
                        skipped_tests.add(test_id)
                continue

            if re_noise.match(line):
                continue

            m = re_suite.match(line)
            if m:
                ws, name = m.group(1), m.group(2)
                indent = len(ws)
                if indent == 0 or "\t" in ws:
                    continue
                # karma-spec-reporter prints a nested describe at the SAME indent
                # as its parent's specs, and a failure's message/stack strictly
                # deeper than the spec it belongs to. `>` keeps the nested suite
                # and drops the failure body; `>=` would silently flatten every
                # nested describe out of the test id.
                if last_spec_indent is not None and indent > last_spec_indent:
                    continue
                last_spec_indent = None
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, name))

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )



Instance.register("chartjs", "Chart.js")(CHART_JS_5841_TO_1653)
