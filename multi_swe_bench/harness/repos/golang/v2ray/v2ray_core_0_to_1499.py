import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class V2rayCore0To1499ImageBase(Image):
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
        # Pre-go-modules v2ray-core (PRs 0–1499). Go 1.22 removed `go get` in
        # GOPATH mode, so we pin to 1.21 which still supports the workflow our
        # prepare.sh needs (module mode + writable module cache + curl).
        return "golang:1.21-bookworm"

    def image_tag(self) -> str:
        return "base-legacy"

    def workdir(self) -> str:
        return "base-legacy"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class V2rayCore0To1499ImageDefault(Image):
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
        return V2rayCore0To1499ImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "pinned_go.mod",
                """module v2ray.com/core

go 1.13

require (
\tgithub.com/golang/glog v0.0.0-20160126235308-23def4e6c14b
\tgithub.com/golang/protobuf v1.2.0
\tgithub.com/gorilla/websocket v1.2.0
\tgithub.com/miekg/dns v1.0.8
\tgolang.org/x/crypto v0.0.0-20190208162236-193df9c0f06f
\tgolang.org/x/net v0.0.0-20190206173232-65e2d4e15006
\tgolang.org/x/sync v0.0.0-20180314180146-1d60e4601c6f
\tgolang.org/x/sys v0.0.0-20180830151530-49385e6e1522
\tgolang.org/x/text v0.3.0
\tgoogle.golang.org/genproto v0.0.0-20180817151627-c66870c02cf8
\tgoogle.golang.org/grpc v1.18.0
\tv2ray.com/ext v0.0.0-20171226163434-694045b342ba
\th12.me/socks v0.0.0-20180505162055-cd352f5a4693
)

replace v2ray.com/ext => /home/v2ray-ext-stub
replace h12.me/socks => github.com/h12w/socks v0.0.0-20180505162055-cd352f5a4693
""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

# Pre-go-modules PRs lack a working dep manifest. Pin 2018-era versions and
# use replace directives for import paths whose hosting no longer resolves
# (v2ray.com/ext, h12.me/socks). GOTOOLCHAIN=local prevents Go from
# auto-upgrading when transitive deps request a newer toolchain.
export GOTOOLCHAIN=local

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

# Some base SHAs are not on default branches; fetch by full hash if needed.
if ! git cat-file -e {pr.base.sha} 2>/dev/null; then
    git fetch origin {pr.base.sha} || true
fi
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Install the pinned manifest. Strip any partial vendor/ tree (none of the
# 0_to_1499 PRs ship a complete vendor, so vendor mode is unusable here).
rm -rf vendor go.mod go.sum
cp /home/pinned_go.mod ./go.mod

# Build a local v2ray.com/ext stub: clone the Dec-2017 snapshot and remove
# the tools/conf/*.go siblings that import v2ray.com/core packages absent
# from older bases (e.g., app/policy and common/log didn't exist in 2017).
# A local-directory `replace` bypasses module-cache hashing, so the strip works.
if [ ! -d /home/v2ray-ext-stub ]; then
    git clone https://github.com/v2ray/ext /home/v2ray-ext-stub
    (cd /home/v2ray-ext-stub && git checkout 694045b342ba0aee86b6601649c51e1fc51914b1)
    rm -f /home/v2ray-ext-stub/tools/conf/*.go
    # serial/loader.go imports tools/conf (parent) and uses conf.Config.Build().
    # Write a minimal stub satisfying that interface so the package compiles
    # without pulling in the original siblings that reference unavailable
    # v2ray.com/core subpackages.
    cat > /home/v2ray-ext-stub/tools/conf/stub.go <<'EOF'
package conf

import "v2ray.com/core"

type Config struct{{}}

func (c *Config) Build() (*core.Config, error) {{
    return &core.Config{{}}, nil
}}
EOF
    # The cloned tree predates go modules — add a minimal go.mod for the replace target.
    cat > /home/v2ray-ext-stub/go.mod <<'EOF'
module v2ray.com/ext

go 1.13
EOF
fi

# Some v2ray-core tests load geoip.dat / geosite.dat via a GOPATH-style path
# (/go/src/v2ray.com/core/release/config/*.dat) regardless of module mode.
# We only need the GOPATH symlink here — the .dat downloads happen at test
# time (run.sh / test-run.sh / fix-run.sh) AFTER patch application, so that
# patches which themselves add geoip.dat don't conflict with a pre-staged file.
mkdir -p /go/src/v2ray.com
ln -sfn /home/{pr.repo} /go/src/v2ray.com/core

# tidy populates go.sum + adds missing transitive deps; -e keeps going on errors.
# Skip the warm-up `go test ./...` — it compiles every package in parallel and
# can OOM-kill the build container; the actual run.sh / test-run.sh / fix-run.sh
# do the test compilation at execution time instead.
go mod tidy -e -compat=1.13 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

export GOTOOLCHAIN=local

cd /home/{pr.repo}

# Download geoip.dat/geosite.dat only if missing (so a patch that adds
# these files takes precedence over our network fetch).
mkdir -p release/config
for asset in geoip.dat geosite.dat; do
    if [ ! -f release/config/$asset ]; then
        curl -fsSL -o release/config/$asset \
            https://github.com/v2fly/geoip/releases/latest/download/$asset 2>/dev/null || \
        curl -fsSL -o release/config/$asset \
            https://github.com/v2fly/domain-list-community/releases/latest/download/$asset 2>/dev/null || true
    fi
done

go test -v -count=1 -vet=off -tags json ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

export GOTOOLCHAIN=local

cd /home/{pr.repo}
git apply /home/test.patch || git apply --reject --allow-empty /home/test.patch || true
find . -name '*.rej' -delete 2>/dev/null || true

# Re-tidy after applying patches: test.patch / fix.patch may add code that
# imports packages not in the unpatched source's import graph (e.g. grpc).
# The unpatched prepare.sh tidy can't anticipate these, so retidy here with
# the pinned go.mod as the baseline. -compat=1.13 keeps pin discipline.
cp /home/pinned_go.mod ./go.mod
rm -f go.sum
go mod tidy -e -compat=1.13 || true

mkdir -p release/config
for asset in geoip.dat geosite.dat; do
    if [ ! -f release/config/$asset ]; then
        curl -fsSL -o release/config/$asset \
            https://github.com/v2fly/geoip/releases/latest/download/$asset 2>/dev/null || \
        curl -fsSL -o release/config/$asset \
            https://github.com/v2fly/domain-list-community/releases/latest/download/$asset 2>/dev/null || true
    fi
done

go test -v -count=1 -vet=off -tags json ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

export GOTOOLCHAIN=local

cd /home/{pr.repo}
git apply /home/test.patch || git apply --reject --allow-empty /home/test.patch || true
find . -name '*.rej' -delete 2>/dev/null || true
git apply /home/fix.patch || git apply --reject --allow-empty /home/fix.patch || true
find . -name '*.rej' -delete 2>/dev/null || true

# Re-tidy after applying patches (see test-run.sh comment).
cp /home/pinned_go.mod ./go.mod
rm -f go.sum
go mod tidy -e -compat=1.13 || true

mkdir -p release/config
for asset in geoip.dat geosite.dat; do
    if [ ! -f release/config/$asset ]; then
        curl -fsSL -o release/config/$asset \
            https://github.com/v2fly/geoip/releases/latest/download/$asset 2>/dev/null || \
        curl -fsSL -o release/config/$asset \
            https://github.com/v2fly/domain-list-community/releases/latest/download/$asset 2>/dev/null || true
    fi
done

go test -v -count=1 -vet=off -tags json ./...

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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("v2ray", "v2ray-core_0_to_1499")
class V2RAY_CORE_0_TO_1499(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return V2rayCore0To1499ImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
