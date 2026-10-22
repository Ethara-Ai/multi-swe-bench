import re
from dataclasses import dataclass

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# tsparticles/tsparticles PRs 5226-5226: pnpm 8 workspace, tests in utils/tests against built engine, Node 18.
# Tests are mocha + ts-node run straight from TypeScript sources, reported via
# the TAP reporter. node-canvas is compiled from source (no arm64 prebuilds).


@dataclass(frozen=True)
class _Era:
    lo: int
    hi: int
    node_image: str
    apt_packages: str
    provision: str
    gate: str
    test_dir: str
    pre_test: str

    @property
    def base_tag(self) -> str:
        return f"base-{self.lo}_to_{self.hi}"


# Packages referenced by historical package.json files that are no longer
# published on npm (404). They are dev tooling only (docs theme, bundler
# plugins) and never imported by the test suites, so they are dropped from the
# manifests for the install and the manifests are restored right after.
_STRIP_UNPUBLISHED = """\
node - . <<'JS'
const fs = require("fs"), path = require("path");
const bad = /^(typedoc-plugin-keyword|typedoc-particles-theme|@tsparticles\\/build|webpack-tsparticles-plugin)$/;
function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    if (e.name === "node_modules" || e.name === ".git") continue;
    const p = path.join(d, e.name);
    if (e.isDirectory()) { walk(p); continue; }
    if (e.name !== "package.json") continue;
    const j = JSON.parse(fs.readFileSync(p, "utf8"));
    let changed = false;
    for (const s of ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]) {
      for (const k of Object.keys(j[s] || {})) {
        if (bad.test(k)) { delete j[s][k]; changed = true; }
      }
    }
    if (changed) { fs.writeFileSync(p, JSON.stringify(j, null, 2) + "\\n"); console.log("stripped", p); }
  }
}
walk(process.argv[2] || ".");
JS
"""

_ERA = _Era(
    lo=5226,
    hi=5226,
    node_image="node:18-bookworm",
    apt_packages="build-essential python3 pkg-config libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev",
    provision='cd /home/tsparticles\nnpm install -g pnpm@8.15.9\n@@S@@pnpm install --no-frozen-lockfile --filter "tsparticles-tests..." --filter "{.}"\ngit checkout -- .\n'.replace("@@S@@", _STRIP_UNPUBLISHED),
    gate='(cd /home/tsparticles/engine && pnpm run build > /tmp/engine_build.log 2>&1) || { tail -50 /tmp/engine_build.log; exit 1; }\ncd /home/tsparticles/utils/tests\nexport PATH="$PWD/node_modules/.bin:/home/tsparticles/node_modules/.bin:$PATH"\nnode --version\nnode -e \'require("canvas"); require("ts-node"); require("chai")\'\nmocha --version\n',
    test_dir="utils/tests",
    pre_test='(cd /home/tsparticles/engine && pnpm run build > /tmp/engine_build.log 2>&1) || { tail -50 /tmp/engine_build.log; exit 1; }\n',
)

_REINSTALL = """\
# Re-provision when the applied patches change dependency manifests.
if { git diff --name-only HEAD; git ls-files --others --exclude-standard; } \\
    | grep -Eq '(^|/)(package\\.json|yarn\\.lock|pnpm-lock\\.yaml)$'; then
  find . -name package.json -not -path '*/node_modules/*' -print0 | tar --null -czf /tmp/manifests.tgz -T -
  (
    cd /home/tsparticles
    @@STRIP@@
    pnpm install --no-frozen-lockfile --filter "./utils/tests..." --filter "{.}"
  ) || echo "WARN: dependency re-provisioning failed"
  cd /home/tsparticles
  tar -xzf /tmp/manifests.tgz
fi
""".replace("    @@STRIP@@\n", _STRIP_UNPUBLISHED).replace("      @@STRIP@@\n", _STRIP_UNPUBLISHED)


def _binary_excludes(*patches: str) -> str:
    """git apply --exclude flags for binary diffs (no full index, can't apply)."""
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
    return (
        _ERA.pre_test
        + f"cd /home/tsparticles/{_ERA.test_dir}\n"
        + 'export PATH="$PWD/node_modules/.bin:/home/tsparticles/node_modules/.bin:$PATH"\n'
        + "rc=0\n"
        + "mocha --reporter tap > /tmp/mocha_tap.log 2>&1 || rc=$?\n"
        + "cat /tmp/mocha_tap.log\n"
        # ts-node aborts the whole run when any spec has a type error (e.g. a
        # test patch referencing symbols only the fix adds), leaving no TAP
        # lines. Re-run transpile-only so those specs fail individually.
        + "if ! grep -Eq '^(not )?ok [0-9]+' /tmp/mocha_tap.log; then\n"
        + "  echo '# type-check aborted mocha; re-running with TS_NODE_TRANSPILE_ONLY'\n"
        + "  rc=0\n"
        + "  TS_NODE_TRANSPILE_ONLY=true TS_NODE_COMPILER_OPTIONS='{\"preserveConstEnums\":true}' mocha --reporter tap || rc=$?\n"
        + "fi\n"
        + "exit $rc"
    )


class TsparticlesImageBase_TSPARTICLES_5226_TO_5226(Image):
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
    HUSKY=0 \\
    HUSKY_SKIP_INSTALL=1 \\
    ELECTRON_SKIP_BINARY_DOWNLOAD=1 \\
    PUPPETEER_SKIP_DOWNLOAD=1 \\
    CYPRESS_INSTALL_BINARY=0 \\
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


class TsparticlesImageDefault_TSPARTICLES_5226_TO_5226(Image):
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
        return TsparticlesImageBase_TSPARTICLES_5226_TO_5226(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

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
# Native toolchain + cairo/pango stack for node-canvas source builds.
for i in 1 2 3; do
  apt-get update && apt-get install -y --no-install-recommends --fix-missing APT_PACKAGES && break
  sleep 5
done
rm -rf /var/lib/apt/lists/*
PROVISION
# --- Section 3: hard gate - toolchain and test dependencies resolve ---
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


@Instance.register("tsparticles", "tsparticles_5226_to_5226")
class TSPARTICLES_5226_TO_5226(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TsparticlesImageDefault_TSPARTICLES_5226_TO_5226(self.pr, self._config)

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

        # mocha TAP reporter: "ok N title", "not ok N title", "ok N title # SKIP -"
        re_tap = re.compile(r"^(not ok|ok) \d+ (.+?)\s*$")
        for line in log.splitlines():
            m = re_tap.match(line.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "not ok":
                failed_tests.add(name)
            elif re.search(r"\s#\s*SKIP\b", name):
                skipped_tests.add(re.sub(r"\s#\s*SKIP\b.*$", "", name))
            else:
                passed_tests.add(name)

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
