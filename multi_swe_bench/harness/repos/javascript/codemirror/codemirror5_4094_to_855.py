import re
from dataclasses import dataclass

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# codemirror/codemirror5 PRs 855-4094: lib/codemirror.js is committed, so no build step and no npm deps are needed.
# The suite is the in-browser harness at test/index.html. It is driven headless
# by a pinned puppeteer-core against Debian's chromium, with "#verbose" so every
# test result is rendered, and each result is printed as "TEST <status> <name>".


@dataclass(frozen=True)
class _Era:
    lo: int
    hi: int
    node_image: str
    apt_packages: str
    provision: str
    gate: str
    pre_test: str

    @property
    def base_tag(self) -> str:
        return f"base-{self.lo}_to_{self.hi}"


# Headless runner kept outside the repo so the working tree stays clean.
_RUNNER_JS = r"""
const http = require("http");
const fs = require("fs");
const path = require("path");
const puppeteer = require("/opt/cmrunner/node_modules/puppeteer-core");

const root = process.argv[2] || "/home/codemirror5";
const smoke = process.argv.includes("--smoke");
const types = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".png": "image/png", ".gif": "image/gif",
  ".svg": "image/svg+xml", ".txt": "text/plain",
};

const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split("?")[0].split("#")[0]);
  const file = path.join(root, path.normalize(rel));
  if (!file.startsWith(root)) { res.writeHead(403); return res.end(); }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream" });
    res.end(data);
  });
});

server.listen(0, "127.0.0.1", async () => {
  const port = server.address().port;
  let code = 0;
  const browser = await puppeteer.launch({
    executablePath: "/usr/bin/chromium",
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  try {
    const page = await browser.newPage();
    if (smoke) {
      await page.goto("about:blank");
      console.log("runner smoke ok:", await browser.version());
      return;
    }
    // Upstream CI for these bases ran test/index.html under PhantomJS
    // (test/run.js -> phantom_driver.js); tests and lib/codemirror.js gate
    // engine-specific behaviour on /PhantomJS/.test(userAgent), so mirror it.
    await page.setUserAgent((await browser.userAgent()) + " PhantomJS/2.1.1");
    await page.evaluateOnNewDocument(() => { window.automatedTests = true; });
    await page.goto(`http://127.0.0.1:${port}/test/index.html#verbose`);
    await page.waitForFunction(() => {
      if (window.done) return true;
      const st = document.getElementById("status");
      if (!st) return false;
      const holder = st.parentNode && st.parentNode.className || "";
      return /\bdone\b/.test(holder) || /^(\d+ failures?|All passed)/i.test(st.textContent.trim());
    }, { timeout: 900000, polling: 250 });
    const results = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#output dt")).map((dt) => [
        (dt.className || "").trim(), dt.textContent.trim(),
      ]));
    for (const [cls, name] of results) console.log(`TEST ${cls || "unknown"} ${name}`);
    const status = await page.evaluate(() => (document.getElementById("status") || {}).textContent || "");
    console.log(`STATUS ${status.trim()}`);
    if (results.some(([cls]) => cls === "fail" || cls === "error")) code = 1;
  } catch (e) {
    console.error(`runner error: ${e && e.stack || e}`);
    code = 2;
  } finally {
    await browser.close();
    server.close();
    process.exit(code);
  }
});
"""

_RUNNER_PROVISION = (
    "mkdir -p /opt/cmrunner\n"
    "cd /opt/cmrunner\n"
    "[ -f package.json ] || npm init -y > /dev/null\n"
    "npm install --no-audit --no-fund puppeteer-core@21.11.0\n"
    "cat > /opt/cmrunner/run.js <<'JS'\n" + _RUNNER_JS.lstrip("\n") + "JS\n"
)

_ERA = _Era(
    lo=855,
    hi=4094,
    node_image="node:18-bookworm",
    apt_packages="chromium fonts-liberation",
    provision=_RUNNER_PROVISION + "",
    gate=(
        "chromium --version\n"
        "node --version\n"
        "node /opt/cmrunner/run.js /home/codemirror5 --smoke\n"
        + "test -f /home/codemirror5/lib/codemirror.js\n"
    ),
    pre_test="",
)

# lib/codemirror.js is committed; nothing to rebuild after patching.
_REINSTALL = ""


def _binary_excludes(*patches: str) -> str:
    paths: set[str] = set()
    for patch in patches:
        for section in re.split(r"^(?=diff --git )", patch or "", flags=re.M):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", section)
            if not m:
                continue
            if re.search(r"^(Binary files .* differ|GIT binary patch)$", section, re.M):
                paths.add(m.group(1))
                paths.add(m.group(2))
    return "".join(f" --exclude='{p}'" for p in sorted(paths))


def _test_cmd() -> str:
    return _ERA.pre_test + "node /opt/cmrunner/run.js /home/codemirror5"


class CodemirrorImageBase_CODEMIRROR5_4094_TO_855(Image):
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
        return _ERA.node_image

    def image_tag(self) -> str:
        return _ERA.base_tag

    def workdir(self) -> str:
        return _ERA.base_tag

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Shared, PR-independent base image.

        Starts with the `# syntax=docker/dockerfile:1.6` directive so the
        framework's enhancer leaves it untouched (image.py:317); all ARG/ENV,
        proxy and CA plumbing is therefore written by hand. The CA symlink farm
        runs before any network call. BASE_COMMIT is declared but unused: the
        commit is pinned in the PR layer.
        """
        image_name = self.dependency()
        repo = self.pr.repo
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{repo}.git"
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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GIT_SSL_CAINFO=${{CA_CERT_PATH}}

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    NPM_CONFIG_CAFILE=${{CA_CERT_PATH}} \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    PUPPETEER_SKIP_DOWNLOAD=1 \\
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1 \\
    PHANTOMJS_CDNURL="" \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{repo}" \\
      org.opencontainers.image.description="Base image for {self.pr.org}/{repo} PRs {_ERA.lo}-{_ERA.hi}" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class CodemirrorImageDefault_CODEMIRROR5_4094_TO_855(Image):
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
        return CodemirrorImageBase_CODEMIRROR5_4094_TO_855(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-codemirror5_4094_to_855"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = _test_cmd()
        test_excludes = _binary_excludes(self.pr.test_patch)
        fix_excludes = _binary_excludes(self.pr.test_patch, self.pr.fix_patch)
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -euo pipefail

# --- Section 1: pin the base commit on a clean tree ---
cd /home/REPO
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

# --- Section 2: provision dependencies for this era ---
# Headless chromium for the browser test harness.
for i in 1 2 3; do
  apt-get update && apt-get install -y --no-install-recommends --fix-missing APT_PACKAGES && break
  sleep 5
done
rm -rf /var/lib/apt/lists/*
PROVISION
# --- Section 3: hard gate - browser runner and test dependencies work ---
GATE
echo DEPS_OK
""".replace("APT_PACKAGES", _ERA.apt_packages)
                .replace("PROVISION\n", _ERA.provision)
                .replace("GATE\n", _ERA.gate)
                .replace("REPO", repo)
                .replace("BASE_SHA", sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/REPO
TEST_CMD
""".replace("REPO", repo).replace("TEST_CMD", test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/REPO
git apply --whitespace=nowarnTEST_EXCLUDES /home/test.patch
REINSTALL
TEST_CMD
""".replace("REPO", repo)
                .replace("TEST_EXCLUDES", test_excludes)
                .replace("REINSTALL\n", _REINSTALL)
                .replace("TEST_CMD", test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/REPO
git apply --whitespace=nowarnFIX_EXCLUDES /home/test.patch /home/fix.patch
REINSTALL
TEST_CMD
""".replace("REPO", repo)
                .replace("FIX_EXCLUDES", fix_excludes)
                .replace("REINSTALL\n", _REINSTALL)
                .replace("TEST_CMD", test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        if isinstance(dep, str) or dep is None:
            raise ValueError("ImageDefault dependency must be the shared base Image")
        name = dep.image_name()
        tag = dep.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{self.global_env}

WORKDIR /home/{repo}

RUN git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null \\
    || git fetch --no-tags origin "${{BASE_COMMIT}}"

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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

{self.clear_env}
"""


@Instance.register("codemirror", "codemirror5_4094_to_855")
class CODEMIRROR5_4094_TO_855(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return CodemirrorImageDefault_CODEMIRROR5_4094_TO_855(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Runner output: "TEST <ok|expected|skipped|fail|error> <test name>"
        re_test = re.compile(r"^TEST (\S+) (.+?)\s*$")
        for line in log.splitlines():
            m = re_test.match(line.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status in ("ok", "expected"):
                passed_tests.add(name)
            elif status == "skipped":
                skipped_tests.add(name)
            elif status in ("fail", "error"):
                failed_tests.add(name)

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
