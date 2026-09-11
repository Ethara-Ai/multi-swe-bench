"""payloadcms/payload — era 1: PRs #0–#3409.

Single-package repo (`src/`), yarn 1 + `yarn.lock`, Jest for integration tests
(`jest.config.js` sets `verbose: true`) and Playwright for `test/**/e2e.spec.ts`.
`engines` at the SHAs in this range: node >=14, yarn >=1.22 <2.

Era 2 (#3424+) is the pnpm monorepo; see payload_3424_to_99999.py.
"""

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .payload import (
    CHECK_GIT_CHANGES_SH,
    START_MONGO_SH,
    STRIP_BINARY_DIFFS_PY,
    base_dockerfile,
    fix_run_sh,
    payload_parse_log,
    pr_dockerfile,
    recover_base_commit,
    render,
    run_e2e_sh,
    run_sh,
    select_e2e_sh,
    select_targets_sh,
    test_run_sh,
)

_INTERVAL_NAME = "payload_0_to_3409"
_PACKAGE_MANAGER = "yarn"

# 6.0 is the MongoDB release contemporary with this era's mongoose version.
#
# The apt source is the *Ubuntu jammy* one even though node:18 is Debian bookworm.
# This is deliberate: MongoDB's Debian bookworm repo publishes only mongosh /
# database-tools / atlas-cli — it carries no `mongodb-org` or `mongodb-org-server`
# for either architecture, so pointing at it fails with "Unable to locate package
# mongodb-org". The Ubuntu jammy repo publishes the full server for amd64 and
# arm64 alike, and jammy binaries run on bookworm's newer glibc. Verified against
# the published Packages indexes and by building the image.
_MONGO_SERIES = "6.0"

# Playwright's Chromium runtime dependencies. Needed from PR #1168 on, where the
# e2e suite lands; harmless before that.
_CHROMIUM_LIBS = (
    "fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 "
    "libcairo2 libcups2 libcurl4 libdbus-1-3 libdrm2 libexpat1 libgbm1 "
    "libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libudev1 "
    "libwayland-client0 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 "
    "libxext6 libxfixes3 libxkbcommon0 libxrandr2 libxshmfence1"
)

_BASE_SETUP = f"""# node:18 already ships yarn 1.22.22, which satisfies engines.yarn ">=1.22 <2";
# the pin only covers a base image that ever drops it.
RUN yarn --version || npm install -g yarn@1.22.22
RUN npm install -g cross-env

# MongoDB plus Chromium's runtime libraries: payload's integration tests need a
# real mongod on 27017, and the e2e suite drives Chromium through Playwright.
#
# libvips-dev + pkg-config are what keep sharp buildable. sharp otherwise fetches
# a prebuilt libvips from GitHub's release CDN, which the build sandbox refuses
# (ECONNREFUSED to 185.199.x.x) even though github.com itself is reachable — so
# `sharp-linux-arm64v8.node` never lands and every suite touching src/uploads
# dies on the import. With a global libvips present, sharp compiles against it
# and makes no network call at all. Debian bookworm ships 8.14.1, which clears
# the minimum for both sharp 0.29 (>=8.11.3) and sharp 0.31 (>=8.13.3).
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl gnupg wget libvips-dev pkg-config {_CHROMIUM_LIBS} && \\
    wget -qO - https://pgp.mongodb.com/server-{_MONGO_SERIES}.asc \\
        | gpg --dearmor -o /usr/share/keyrings/mongodb-server-{_MONGO_SERIES}.gpg && \\
    echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-{_MONGO_SERIES}.gpg] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/{_MONGO_SERIES} multiverse" \\
        > /etc/apt/sources.list.d/mongodb-org-{_MONGO_SERIES}.list && \\
    apt-get update && \\
    apt-get install -y --no-install-recommends mongodb-org && \\
    mkdir -p /data/db && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*"""

# `pretest` is a pre-hook of `test`, not of `test:int`, so nothing the repo runs
# builds for us — the build below is deliberate and is needed by the e2e suite.
_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/@@REPO@@

git reset --hard

# -x so ignored files go too. Nothing should be untracked at this point — the
# base image's clone is pristine — but a leftover from a cached layer would
# otherwise survive into the graded tree (QC P5).
git clean -fdxq

# Assert the inherited tree is clean BEFORE moving it, so a dirty base is caught
# here rather than silently carried into the checkout (QC P5).
bash /home/check_git_changes.sh

# Recover this PR's base commit if the shared base image's clone does not carry
# it, then detach onto it. The Dockerfile's hardening block runs after this and
# asserts HEAD is exactly this commit.
@@RECOVER@@
bash /home/check_git_changes.sh

yarn install || true

# sharp fetches a prebuilt libvips for the target architecture from GitHub
# releases. That fetch is the single most contended step in a parallel build and
# a refused connection leaves `sharp-linux-arm64v8.node` missing, which kills
# every suite that reaches src/uploads — so it is retried rather than accepted.
sharp_ok=0
for attempt in 1 2 3; do
  if npm rebuild sharp; then sharp_ok=1; break; fi
  echo "prepare: sharp rebuild attempt $attempt failed; retrying"
  sleep 15
done
if [ "$sharp_ok" != 1 ]; then
  echo "prepare: WARNING sharp is still unbuilt; uploads-dependent suites will fail"
fi

# The e2e suite compiles the admin bundle at run time and its SCSS imports
# `../dist/admin/scss/vars`, so `dist/` has to exist before Playwright starts.
# The integration suites do not need it (jest transforms `src/` directly), but
# building once here is far cheaper than the webpack failure it prevents.
yarn build || true

if [ -x node_modules/.bin/playwright ]; then
  # install-deps knows the exact apt set this Playwright build needs; the static
  # list in the base image covers the common case, this covers the rest.
  node_modules/.bin/playwright install-deps chromium || true
  node_modules/.bin/playwright install chromium || true
fi

# Hard gate (QC P14). No `|| true` on this one, and it comes last: a swallowed
# install failure otherwise ships an image that looks healthy and produces an
# empty test report, which the harness reads as "0 failures" rather than
# "broken image". Asserting on jest as well as the package itself matters — a
# bare package.json parse passes while every test errors on a missing runner.
node -e "require('./package.json'); require.resolve('jest'); console.log('DEPS_OK')"
"""


class PayloadV1ImageBase(Image):
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
        return "node:18"

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return base_dockerfile(self, self.dependency(), _BASE_SETUP)


class PayloadV1ImageDefault(Image):
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
        return PayloadV1ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "strip_binary_diffs.py", STRIP_BINARY_DIFFS_PY),
            File(".", "start-mongo.sh", START_MONGO_SH),
            File(".", "select-targets.sh", select_targets_sh(repo)),
            File(".", "select-e2e.sh", select_e2e_sh(repo)),
            File(".", "run-e2e.sh", run_e2e_sh(repo)),
            File(
                ".",
                "prepare.sh",
                render(
                    _PREPARE_SH,
                    repo=repo,
                    recover=recover_base_commit(self.pr),
                ),
            ),
            File(".", "run.sh", run_sh(repo, _PACKAGE_MANAGER)),
            File(".", "test-run.sh", test_run_sh(repo, _PACKAGE_MANAGER)),
            File(".", "fix-run.sh", fix_run_sh(repo, _PACKAGE_MANAGER)),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self)


@Instance.register("payloadcms", _INTERVAL_NAME)
class PAYLOAD_0_TO_3409(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PayloadV1ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return payload_parse_log(test_log)
