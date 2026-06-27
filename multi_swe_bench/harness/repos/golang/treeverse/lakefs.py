from __future__ import annotations

import os
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Go toolchain image used across every lakeFS era (1.25 is forward-compatible
# with the older go.mod directives via GOTOOLCHAIN=auto).
_GO_IMAGE = "golang:1.25"


_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".bmp", ".webp",
    ".pdf", ".zip", ".jar", ".class", ".tar", ".gz", ".tgz", ".bz2", ".7z",
    ".parquet", ".avro", ".orc",
    ".bin", ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".wav", ".ogg",
}


def _is_binary_path(path: str) -> bool:
    _, ext = os.path.splitext(path.lower())
    if ext in _BINARY_EXTENSIONS:
        return True
    # lakeFS test fixtures with no extension but binary content
    if "nessie/files/ro_" in path or "/test_files/" in path:
        return True
    return False


def _strip_binary_hunks(patch_text: str) -> str:
    """Remove binary-file hunks from a unified diff so `git apply` won't reject
    the whole patch when only PNG/JPG/JAR-style hunks are problematic.
    Binary content is irrelevant to Go test outcomes in this repo."""
    if not patch_text:
        return patch_text
    out = []
    blocks = re.split(r"(?m)^(?=diff --git )", patch_text)
    for block in blocks:
        if not block.strip():
            continue
        first_line = block.splitlines()[0]
        m = re.match(r"diff --git a/(.*) b/(.*)$", first_line)
        if m and (_is_binary_path(m.group(1)) or _is_binary_path(m.group(2))):
            continue
        if "GIT binary patch" in block or re.search(
            r"^Binary files .* differ", block, re.MULTILINE
        ):
            continue
        out.append(block)
    return "".join(out)


# ---------------------------------------------------------------------------
# Build-context scripts (COPY'd into the per-PR image, run at build/eval time).
# ---------------------------------------------------------------------------

# Shared code-generation step. lakeFS embeds generated Go (mockgen, oapi-codegen,
# statik UI/SQL) that must match whatever source state is in the tree, so this
# runs AFTER every checkout/patch and BEFORE `go test`. All generators are
# pre-installed in the image (go install in the Dockerfile) and live on
# /go/bin. Everything is best-effort (set +e) -- a missing generator in one era
# must not abort the run.
_GEN_SH = """#!/bin/bash
set +e
export PATH=/go/bin:$PATH

if [ -d tools/wrapgen ]; then
  go install ./tools/wrapgen 2>/dev/null
fi

# Earlier era (<= v0.x): statik embeds SQL migrations into pkg/ddl.
if [ -d pkg/ddl ] && ls pkg/ddl/*.sql >/dev/null 2>&1; then
  statik -ns ddl -m -f -p ddl \\
    -c "auto-generated SQL files for data migrations" \\
    -dest pkg -src pkg/ddl -include '*.sql' 2>/dev/null
fi

# Earlier era: oapi-codegen for pkg/api via //go:generate in pkg/api/serve.go.
if [ -f pkg/api/serve.go ] && grep -q "^//go:generate" pkg/api/serve.go; then
  go generate ./pkg/api 2>/dev/null
fi

# Later era: apigen package with its own //go:generate directive.
if [ -f pkg/api/apigen/doc.go ] && grep -q "^//go:generate" pkg/api/apigen/doc.go; then
  go generate ./pkg/api/apigen 2>/dev/null
fi
for p in pkg/auth pkg/authentication; do
  if [ -f "$p/service.go" ] && grep -q "^//go:generate.*oapi-codegen" "$p/service.go"; then
    go generate ./$p 2>/dev/null
  fi
done

# Stub the embedded web UI so the package compiles without npm (era-agnostic).
#   - Newer era: a Go file carries `//go:embed dist` (webui/ at repo root in the
#     v0.6x-v0.10x line, or pkg/webui/). Create the sibling dist/ it embeds.
#   - Older era: pkg/webui uses statik (no //go:embed) -- generate a stub
#     statik.go from a throwaway dist/.
_embed_stubbed=0
for _gf in $(grep -rl --include='*.go' 'go:embed dist' . 2>/dev/null); do
  _dist="$(dirname "$_gf")/dist"
  if [ ! -d "$_dist" ] || [ -z "$(ls -A "$_dist" 2>/dev/null)" ]; then
    mkdir -p "$_dist"
    echo '<!doctype html><html></html>' > "$_dist/index.html"
  fi
  _embed_stubbed=1
done
if [ "$_embed_stubbed" = 0 ] && grep -rq 'treeverse/lakefs/pkg/webui' pkg/ cmd/ 2>/dev/null; then
  if [ ! -f pkg/webui/statik.go ] && [ ! -f pkg/webui/embedded.go ]; then
    mkdir -p pkg/webui/dist
    echo '<!doctype html><html></html>' > pkg/webui/dist/index.html
    statik -src=pkg/webui/dist -dest=pkg -p=webui -ns=webui -f 2>/dev/null
  fi
fi

# mockgen directives across many packages.
for tgt in pkg/graveler pkg/graveler/committed pkg/graveler/sstable pkg/graveler/staging \\
           pkg/pyramid pkg/actions pkg/kv pkg/onboard; do
  [ -d "$tgt" ] && go generate ./$tgt 2>/dev/null
done

# Ensure the rakyll/statik runtime module is required when any generated
# statik.go imports it (older ddl/webui era). Some base.sha go.mod files lack
# the require even though the statik binary is installed, which makes the whole
# module fail to build. Add it from the module cache (offline-safe: the image
# `go install`ed statik into /go/pkg/mod).
if grep -rqs 'rakyll/statik/fs' --include='*.go' . 2>/dev/null; then
  if ! grep -q 'rakyll/statik' go.mod 2>/dev/null; then
    GOFLAGS=-mod=mod go get github.com/rakyll/statik@v0.1.8 2>/dev/null \\
      || GOFLAGS=-mod=mod GOPROXY=off go get github.com/rakyll/statik@v0.1.8 2>/dev/null \\
      || true
  fi
fi

exit 0
"""

# Build-time warm-up. Runs at base.sha (already checked out in the Dockerfile)
# BEFORE the hardening strip, so it may still see the full clone. Generates code
# and pre-compiles the test binaries (without running them) to seed the build
# cache; everything is || true so a flaky baseline never breaks the image build.
_INSTALL_SH = """#!/bin/bash
set -e
export PATH=/go/bin:$PATH
cd /home/lakeFS

go mod download || true
bash /home/gen.sh

# `go test -c`-style warm: compile each package's test binary but do not run it.
go test -count=1 -run='^$' ./... > /dev/null 2>&1 || true
"""

# Baseline: clean base.sha, no patches. Generated code is regenerated so the
# module compiles, then the full suite runs (matches the golden p2p/s2p set).
_RUN_SH = """#!/bin/bash
set -uxo pipefail
export PATH=/go/bin:$PATH
cd /home/lakeFS

git reset --hard
git checkout {pr.base.sha}
git clean -fdq

bash /home/gen.sh
go test -v -count=1 ./...
"""

# Test patch only: new tests exercise behaviour the fix has not introduced yet,
# so they fail (or their package fails to compile) -- genuine f2p / n2p.
# `git clean -fdq` removes the untracked generated files left by the build-time
# warm-up so a test.patch that ADDS generated/mock files applies cleanly.
_TEST_RUN_SH = """#!/bin/bash
set -uxo pipefail
export PATH=/go/bin:$PATH
cd /home/lakeFS

git reset --hard
git checkout {pr.base.sha}
git clean -fdq

git apply --whitespace=nowarn /home/test.patch
bash /home/gen.sh
go test -v -count=1 ./...
"""

# Test + fix patches: production fix present, the suite passes.
_FIX_RUN_SH = """#!/bin/bash
set -uxo pipefail
export PATH=/go/bin:$PATH
cd /home/lakeFS

git reset --hard
git checkout {pr.base.sha}
git clean -fdq

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/gen.sh
go test -v -count=1 ./...
"""


# apt + the four Go code-generators lakeFS needs across eras. oapi-codegen
# generates pkg/api/apigen and *.gen.go; statik embeds SQL migrations / UI;
# mockgen generates pkg/*/mock; goimports backs some wrapgen directives.
_APT_AND_TOOLS = """RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

ENV GOTOOLCHAIN=auto
ENV PATH=/go/bin:$PATH

RUN go install github.com/deepmap/oapi-codegen/cmd/oapi-codegen@v1.5.6 \\
 && go install github.com/golang/mock/mockgen@v1.6.0 \\
 && go install github.com/rakyll/statik@latest \\
 && go install golang.org/x/tools/cmd/goimports@latest"""


class LakeFSImageBase(Image):
    """Level 1 -- shared toolchain image (built once, reused by every PR).

    ``dependency()`` returns a *string* (the Go toolchain), so the pipeline's
    DockerfileEnhancer engages and prepends the ``# syntax`` / ARG / ENV / LABEL
    infra. It installs apt prerequisites and the four lakeFS code-generators
    (oapi-codegen, mockgen, statik, goimports) and nothing else. Crucially it
    does **no** ``git clone`` / ``git checkout`` -- so the enhancer's final
    sanitize pass finds no git op and injects **no** hardening / base-commit pin.
    The image therefore stays generic and is identical across all PRs, so the
    harness builds it exactly once.
    """

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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

{_APT_AND_TOOLS}

{self.clear_env}

CMD ["/bin/bash"]
"""


class LakeFSImageRepo(Image):
    """Level 2 -- shared repo image (built once, reused by every PR).

    ``dependency()`` returns the Level-1 ``LakeFSImageBase`` *Image* (not a
    string), so ``DockerfileEnhancer.enhance`` returns the Dockerfile verbatim --
    it does NOT force a ``checkout ${BASE_COMMIT}`` + history strip. That is the
    whole point: this image clones the **full history** of lakeFS once and warms
    the Go module cache via ``go mod download`` (the "install common deps from
    the package file" step), WITHOUT pinning to any single PR's base.sha. Because
    no build-args reach an Image-dependency build, the clone URL is baked in as a
    literal. Identical across all PRs, so the harness builds it exactly once and
    every per-PR image is ``FROM`` it -- the common dependencies are reused, not
    re-installed per PR.

    It is deliberately NOT hardened: hardening pins to one base.sha and would
    defeat the reuse. The anti-cheat strip lives in the per-PR image (Level 3),
    which is the only image evaluation ever runs in.
    """

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
        return LakeFSImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "repo"

    def workdir(self) -> str:
        return "repo"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base = self.dependency()
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        return f"""FROM {base.image_full_name()}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN git clone "{repo_url}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN go mod download || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class LakeFSImageDefault(Image):
    """Level 3 -- per-PR image (one per PR, layered on the shared Level-2 repo).

    ``dependency()`` returns the Level-2 ``LakeFSImageRepo`` *Image*, so the
    enhancer leaves this Dockerfile verbatim and the already-cloned repo from the
    shared base is reused -- no re-clone, common deps already present. This image
    only does the PR-specific work: checkout the PR's ``base.sha`` (baked in as a
    literal, since Image-dependency builds receive no build-args), COPY the
    patches + eval scripts, a build-time warm-up (install.sh: PR-specific
    ``go mod download`` + codegen + test-binary compile), and -- because the
    enhancer no longer injects it for Image deps -- the verbatim
    ``Image._HARDENING_BLOCK`` strip (origin/refs/reflog/gc + post-condition
    asserts + submodule pass) so the eval image cannot see post-base.sha history.
    """

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
        return LakeFSImageRepo(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", _strip_binary_hunks(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_hunks(self.pr.test_patch)),
            File(".", "gen.sh", _GEN_SH),
            File(".", "install.sh", _INSTALL_SH),
            File(".", "run.sh", _RUN_SH.format(pr=self.pr)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(pr=self.pr)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(pr=self.pr)),
        ]

    def dockerfile(self) -> str:
        repo = self.dependency()
        base_sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/{file.name}\n"

        return f"""FROM {repo.image_full_name()}

ARG BASE_COMMIT="{base_sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/install.sh || true

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("treeverse", "lakeFS")
class LakeFS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LakeFSImageDefault(self.pr, self._config)

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
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

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
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        # Dedup: when a parent test's parametrized subtests have mixed
        # PASS/FAIL results, the parent name (base_name) can end up in both
        # passed and failed sets. Likewise for skipped. TestResult rejects
        # overlapping sets in __post_init__, so resolve conflicts here:
        #   any FAIL on a name wins over PASS/SKIP for that name.
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


# Route the dash-joined number_interval (canonical prs_in_bundle format) of the
# release-bundled finalLakefs_shippable dataset to the single LakeFS config.
# Each record carries a unique number_interval == sorted PR/issue numbers from
# its resolved_issues joined by "-"; Instance.create() looks up
# f"{org}/{number_interval}", so every bundle key must resolve to LakeFS.
# Instance.register returns the class unchanged, so it answers to every key
# (the original "lakeFS" era key above is kept for back-compat).
_BUNDLE_NUMBER_INTERVALS = [
    "2579-2596-2599-2603-2604-2605-2606-2607-2608-2610-2611-2612-2616-2620",
    "2966-2975-2978-2980-2984",
    "2667-4041-4068-4377-4378-4391-4398-4415-4422-4423-4425-4440-4441-4445-4452-4453-4455-4456-4459-4461-4467-4471-4472-4481-4485-4492-4494-4499-4524-4533-4536-4538-4555-4564-4568",
    "1933-4382-4450-4473-4488-4547-4557-4561-4565-4571-4573-4575-4584-4589-4597-4603",
    "4668-4963-4965-5003-5053-5085-5093-5157-5184-5186-5190-5192-5194-5196-5202-5209",
    "4968-5292-5377-5396-5397-5399-5402-5410-5412-5417",
    "7042-7186-7233-7234-7239",
    "5678-7249-7445-7475-7618-7620-7627-7628-7631-7636-7637-7640-7641",
    "7529-7536-7539-7547-7548-7550-7553",
    "7549-7560-7561-7563-7564-7565",
    "7569-7571-7576-7577-7580-7583-7587-7588-7589-7591-7593",
    "3082-5505-6357-7466-7528-7557-7643-7701-7730-7789-7817-7819-7821-7823-7825-7833-7837-7843-7845-7846-7850-7851-7857-7858-7859-7860-7862-7864-7869-7873-7879",
    "7687-7712-7752-7776-7777-7780-7781-7783-7785-7788-7792-7797-7799-7800-7803-7804-7805-7807-7810-7816-7818",
    "7846-7849-7864-7874-7883-7890-7893-7894-7895-7897-7898-7899-7901-7902-7906",
    "5326-7791-7876-7888-7918-7922-7923-7927-7929-7930-7933-7936-7937-7941-7942-7944-7947-7948-7949-7950-7951-7954-7956",
    "7382-8032-8434-8448-8451-8453-8456-8458-8460-8462-8466",
    "7732-8063-8064-8068-8071-8072-8075-8076-8079-8081-8083-8097",
    "8070-8089-8090-8096-8102-8107-8110-8116-8120-8125-8131",
    "8114-8250-8251-8252-8254-8255-8258",
    "7974-8088-8293-8314-8341-8343-8350-8351-8356-8357-8358-8366-8368-8371-8373-8377",
    "8158-8355-8365-8386-8387-8389-8393-8395-8396-8406-8409-8411",
    "7596-8394-8399-8419-8429-8430-8431-8432-8440-8445",
    "1034-9934-10116-10132-10134-10137-10139-10140-10141-10142-10145-10147-10149-10152-10155-10158-10160-10163",
    "536-538-764-1636-2286-2328-9889-10112-10153-10154-10159-10165-10167-10176-10183-10187-10188-10189-10194-10195-10199-10210-10213",
    "294-483-484-560-569-571-572-574-575-604-1076-1474-1479-8720-9076-10004-10036-10038-10099-10136-10173-10205-10207-10208-10215-10216-10219-10221-10223-10224-10232-10234-10236-10245-10246-10247-10248-10250-10251-10253-10255-10256-10257-10258-10264-10265-10266-10267-10268-10269-10272-10274-10277-10281-10290-10295-10296-10299-10300-10301-10302-10303-10306-10309-10310",
    "5675-5858-5876-5987-5989-5990-5992-5995",
    "5363-5580-5599-5617-5688-5691-5692-5696-5702-5703-5704-5705-5706-5710-5713-5717-5718-5721-5726-5734-5735-5736",
    "4502-4676-4757-5218-5381-5390-5611-5612-5628-5638-5642-5644-5645-5649-5650-5656-5657-5666-5667-5672-5673-5674-5682-5685-5686-5689-5694-5699-5701",
    "2883-2886-2889-3978-5115-5436-5604-5837-5875-5895-5896-5928-5931-5934-5935-5936-5938-5939-5941-5955-5958-5960-5961-5963-5964-5966-5968-5973-5975-5977-5979-5988",
    "4964-4966-4967-5001-5012-5054-5089-5103-5178-5197-5198-5199-5205-5207-5210-5222-5226-5233-5235-5237-5238-5241-5245-5248-5255-5257-5259-5260-5261-5263-5266-5268-5270-5271-5272-5282-5284-5287-5291-5298-5303-5304-5305-5306-5307-5309-5311-5318-5320-5323",
    "4070-4566-4587-4594-4598-4599-4608-4617-4618-4621-4622-4624-4630-4632-4638-4642-4650",
    "654-3451-3696-4181-4214-4233-4246-4247-4272-4273-4275-4290-4294-4304-4306-4319-4322-4326-4328-4329-4334-4335-4337-4339-4345-4346-4351-4353-4354-4358-4361-4365",
    "2344-4196-4210-4212-4223-4238-4244-4249-4251-4255-4259-4263-4265-4275-4282-4284-4286-4287-4293-4296-4302-4305",
    "2649-4121-4191-4341-4490-4493-4572-4583-4602-4604-4605-4628-4631-4635-4636-4641-4648-4652-4654-4655-4656-4657-4659-4664-4673-4677-4679-4681-4683-4688-4689-4692-4693-4698-4701-4704-4705-4707-4713-4718-4723-4724-4725-4727-4733-4735-4737-4738-4745-4752-4758-4759-4760-4762-4766-4768-4771-4779-4785-4789-4791-4800-4804-4807",
    "3437-3575-3628-3633-3686-3692-3713-3727-3741-3785-3815-3828-3840-3842-3844-3845-3846-3851-3854-3855-3858-3865-3866-3868-3870-3873-3875-3877-3879-3882-3886-3943-3944-3945",
    "2732-3269-3344-3475-3493-3508-3512-3513-3516-3518-3535",
    "2839-3174-3266-3373-3411-3428-3452-3453-3482-3505-3520-3530-3531-3536-3537-3540-3542-3543-3548-3554-3561-3562-3563-3564-3568-3576-3578-3579-3584-3601-3610-3611-3619-3625-3641-3644-3953",
    "2894-3026-3243-3310-3330-3339-3348-3365-3375-3377-3378-3379-3382-3383-3384-3385-3395-3396-3398-3400-3403-3406-3407-3409-3413-3414-3417-3420-3423-3424",
    "2031-2042-2418-2419-2461-2499-2509-2510-2883-2996-3098-3099-3137-3150-3154-3161-3203-3226-3242-3257-3265-3290-3293-3295-3296-3298-3301-3302-3304-3307-3308-3309-3317-3318-3319-3320-3325-3333-3334-3335-3337-3338-3343-3360-3361-3371",
    "3134-3190-3192-3219-3220-3222-3224-3228-3230-3232-3234-3237-3239-3255-3256-3260-3261-3264-3267-3268-3270-3272-3273-3275-3276-3277-3279-3282-3284-3287-3288-3289-3292",
    "2967-3100-3111-3113-3149-3152-3166-3173-3175-3177-3178-3184-3185-3186-3187-3188-3189-3194-3195-3197-3209-3214-3221-3223-3227",
    "2226-2409-2902-2907-2940-2944-2947-2952-2956-2959-2961-2962-2965-2972-2979",
    "2509-2659-2758-2763-2778-2784-2810-2821-2831-2833-2837-2840-2843-2844-2846-2847-2850-2854-2856-2865-2866-2869-2871-2876-2878-2879-2885-2888-2890",
    "2715-2737-2752-2768-2769-2776-2780-2782-2783-2806",
    "1637-1749-2437-2561-2562-2576-2647-2668-2669-2671-2672-2674-2677-2678-2680-2681-2682-2684-2686-2687-2688-2691-2695-2697-2699-2707-2709-2712-2714",
    "1749-2162-2251-2506-2524-2568-2602-2617-2618-2619-2621-2623-2626-2629-2630-2634-2635-2636-2637-2638-2640-2641-2652-2653-2656-2660-2661-2662-2664-2665-2666",
    "995-1370-1927-2058-2345-2429-2454-2485-2496-2514-2520-2523-2529-2547-2549-2551-2552-2563-2566-2567-2574-2577-2578-2581-2583-2584-2586-2589-2590-2593-2595-2600",
    "5438-5827-5862-5951-5974-6000-6035-6051-6052-6061-6063-6064-6066-6068-6069-6071-6079-6087-6093-6095-6097-6100-6107-6108-6111-6113",
    "6333-6334-6527-6913-6914-6930-6938-6950-6960-6963-6973-6990-6991-6993-6994-6995-6996-6997-6998-6999-7001-7002-7007-7008-7011-7013-7016-7019-7021-7023-7030-7032-7033-7034-7037-7043-7046-7049-7052-7053-7054-7059-7060-7063-7067-7070-7088-7089",
    "2684-6197-6415-6420-6421-6422-6427-6428-6432-6436-6441-6445-6446",
    "6424-6425-6537-6554-6562-6575-6581-6666-6667-6668-6673-6674-6678-6681-6687-6689-6692-6699-6707-6709-6710-6712-6722-6723-6725-6728",
    "6686-6721-6782-6789-6790-6791-6792-6795-6798-6803-6804-6805-6810-6817",
    "6954-7025-7040-7065-7066-7075-7077-7082-7085-7086-7092-7094-7096-7098-7099-7101-7103-7105-7106-7109-7111-7113-7115-7117-7118-7122-7124-7127-7128-7131-7133-7134-7136-7138",
    "6694-7203-7214-7232-7237-7254-7255-7257-7259-7261-7264-7269-7272-7274-7276-7280-7282-7285-7287",
    "2092-2891-3090-3098-3100-3116-5967-7302-7438-7452-7499-7500-7506-7509-7511-7512-7513-7514-7517-7518-7522-7523-7530-7531-7541-7542-7546",
    "7465-7470-7478-7479-7481-7483-7484-7487-7489",
    "7441-7856-8160-8194-8261-8262-8267-8270-8272-8276-8281-8289-8290",
    "4231-4233-4244-4261-4284-4313-4316-4370-4372-7866-7940-8062-8138-8161-8181-8182-8192-8210-8211-8217-8220-8221-8222-8227-8230-8233-8234-8243-8248",
    "8019-8022-8383-8484-8511-8541-8542-8558-8570-8571-8579-8582-8583-8586-8587-8595-8598-8603-8605-8611-8618-8620-8621-8624-8626-8627-8630-8633-8636-8641-8643-8645-8647-8648-8650-8656-8658-8661-8663-8665-8667-8669-8674-8675-8686-8688-8691",
    "5437-8094-8136-8219-8257-8259-8265-8266",
    "8299-8300-8301-8303-8313-8325-8327-8328-8331",
    "3165-8364-8444-8476-8479-8486-8488-8504",
    "8512-8564-9332-9382-9403-9415-9418-9422-9426-9432-9433-9435-9436-9437-9439-9441-9443-9448-9451-9455-9456-9457",
    "8615-8672-8738-8749-8757-8760-8762-8764-8765-8766-8770-8773-8774-8776-8780-8782-8783-8784-8785-8787-8789-8791-8792-8794-8795-8796-8797-8799-8800-8801-8805-8808-8809-8812-8815-8818-8824-8825-8827",
    "8696-8772-8842-8853-8868-8878-8881-8882-8883-8888-8890-8894-8895-8900-8902",
    "7939-8972-8973-8986-8987-8988-8991-8995-8999-9000-9003-9004-9005-9006-9007-9008-9011-9012-9017-9018-9019-9026-9028",
    "1076-8690-9294-9544-9588-9592-9615-9624-9626-9630-9631-9635-9637-9638-9641-9642-9648-9652-9663",
    "7505-9224-9251-9309-9335-9365-9369-9370-9372-9374-9378-9380-9385-9386-9388-9392-9393-9394",
    "848-8200-9207-9334-9375-9424-9458-9459-9465-9468-9469-9472-9473-9477-9478-9479-9483-9484-9486-9487",
    "4917-7220-9644-9649-9653-9655-9660-9664-9665-9666-9670-9671-9674-9676-9677-9678-9679-9680-9681-9682-9685-9686-9689-9691-9694-9695-9698-9701-9703-9705-9706-9713-9715-9719",
    "2465-3393-5117-5243-5251-6457-7350-7424-7482-7715-7755-8425-8681-8959-9595-9599-9614-9640-9650-9658-9662-9708-9711-9720-9722-9723-9731-9734-9736-9740-9741-9742-9743-9746-9747-9748-9753-9754-9755-9757-9765-9766-9767-9769-9771-9772-9774-9776-9779-9780-9785-9788-9793-9800-9803-9812-9815-9819-9820-9824-9828-9834-9843-9845-9848-9850-9852-9855-9858-9859-9860-9861-9865",
    "6347-9296-9460-9721-9728-9729-9770-9781-9831-9838-9849-9857-9871-9872-9893-9894-9895-9907-9914-9915-9917-9922-9924-9930-9935-9939-9942-9944-9949-9950-9951-9954-9968-9971-9972-9973-9974-9975-9981-9982",
    "579-1947-1948-1951-1952-2097-8898-9727-9809-10275-10311-10312-10316-10318-10320-10321-10325-10326-10328-10332-10334-10335-10336-10338-10347-10349-10350-10352-10354-10355-10356-10357-10358-10360-10362-10364-10365-10367-10374-10380-10384-10392-10393-10394-10398",
    "6347-7575-9940-9941-9983-9984-9990-9994-9995-9996-10003-10012",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("treeverse", _ni)(LakeFS)
