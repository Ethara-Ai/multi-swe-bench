import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _safe_sha(sha: str) -> str:
    """Validate a commit sha before it is interpolated into a RUN line.

    The hardening block is emitted with the LITERAL base.sha (PIPELINE.md S4),
    not a build ARG, so the value reaches a shell inside the image. Anything
    that is not a hex object name is rejected rather than shell-quoted, so a
    malformed record fails the render instead of the build.
    """
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(f"unsafe base.sha for Dockerfile interpolation: {sha!r}")
    return sha


# ---------------------------------------------------------------------------
# Test commands. Shared by the shipped run scripts (baked into the PR image by
# HugoImageDefault.files) and documented here as the single source of truth.
#
# -vet=off: avoids go vet panics on older Hugo code (types.Object nil interface
#   conversion).
# GOPATH fallback: pre-Go-modules Hugo PRs (before go.mod existed, PR <~5097)
#   need GO111MODULE=off.
# ---------------------------------------------------------------------------
_GO_TEST_ALL = (
    "if [ -f go.mod ]; then "
    "go test -vet=off -v -count=1 ./...; "
    "else "
    "export GOPATH=/go && "
    "mkdir -p /go/src/github.com/gohugoio && "
    "ln -sfn /home/hugo /go/src/github.com/gohugoio/hugo && "
    "cd /go/src/github.com/gohugoio/hugo && "
    "GO111MODULE=off go test -vet=off -v -count=1 ./...; "
    "fi"
)

# Extract Go packages touched by both patches and run only those.
# test_patch alone often breaks compilation (references fix_patch APIs).
# Running per-package means: test stage -> [setup failed] (NONE),
# fix stage -> PASS -> valid NONE->PASS transition detected.
_PKGS = (
    "PKGS=$(cat /home/test.patch /home/fix.patch "
    "| grep '^diff --git' | sed 's|diff --git a/.* b/||' "
    "| xargs -I{} dirname {} | sort -u | sed 's|^|./|' | tr '\\n' ' ')"
)

_GO_TEST_PKGS = (
    "if [ -f go.mod ]; then "
    "go test -vet=off -v -count=1 $PKGS; "
    "else "
    "export GOPATH=/go && "
    "mkdir -p /go/src/github.com/gohugoio && "
    "ln -sfn /home/hugo /go/src/github.com/gohugoio/hugo && "
    "cd /go/src/github.com/gohugoio/hugo && "
    "GO111MODULE=off go test -vet=off -v -count=1 $PKGS; "
    "fi"
)


class HugoImageBase(Image):
    """The shared environment image: Go toolchain + a full clone of hugo.

    ONE image (tag `base`) serves every record, so the repo is cloned once per
    dataset build rather than once per record.

    Why the leading `# syntax` directive is load-bearing
    ----------------------------------------------------
    DockerfileEnhancer.enhance early-returns on `if SYNTAX_DIRECTIVE in raw`
    (image.py), so emitting it here opts this base out of the enhancer, and
    everything the enhancer would have contributed -- multi-arch ARG, REPO_URL,
    the MITM proxy/cert scaffolding, the ethara.ai LABEL -- is written out by
    hand below, verbatim against image.py's constants.

    That opt-out is required for correctness, not just for format. Without it
    `_standardize_repo_fetch` / `_inject_final_sanitize` rewrite the clone into
    `git checkout ${BASE_COMMIT}` + Image._HARDENING_BLOCK, whose
    `git gc --prune=now --aggressive` drops every object not reachable from ONE
    commit. Images dedupe on image_full_name, so this single `base` tag is built
    exactly once -- it would freeze on whichever record happened to build first
    and every other record's `git checkout <base.sha>` in prepare.sh would then
    fail against a pruned history.

    So this base keeps full history and applies LIGHT hardening only (drop the
    remote, disable submodule fetch / push default). The STRICT, pinned scrub
    runs one layer up, in HugoImageDefault, anchored to that record's literal
    base.sha -- PIPELINE.md S3/S4.
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
        return "golang:latest"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = _safe_path_component(self.pr.repo)
        org = _safe_path_component(self.pr.org, "org")
        repo_url = f"https://github.com/{org}/{repo}.git"

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
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

ENV LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential curl \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class HugoImageDefault(Image):
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
        return HugoImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go test -v -count=1 ./... || true

""".format(pr=pr),
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -e

cd /home/{pr.repo}
{_GO_TEST_ALL}

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
{_PKGS}
{_GO_TEST_PKGS}

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
{_PKGS}
{_GO_TEST_PKGS}

""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)
        base_sha = _safe_sha(self.pr.base.sha)

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # dependency() is an Image, so DockerfileEnhancer.enhance returns this
        # content raw -- nothing is auto-injected here. The anti-reward-hacking
        # scrub therefore has to be emitted by hand, pinned to the LITERAL
        # base.sha (PIPELINE.md S2/S4). The base image deliberately keeps full
        # history; this layer is where it is destroyed.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", base_sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{repo}

{hardening}
{self.clear_env}

CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# number_interval -- bundle routing for gohugoio/hugo (PIPELINE.md S11).
#
# Each record is a BUNDLE of PRs, and the required value is the EXACT PRs of
# that bundle joined with '-', never a range:
#
#     prs_in_bundle: [13108, 13112, 13114]
#     number_interval: "13108-13112-13114"      (NOT "13108-13114")
#
# A range would claim every PR in between, which is wrong -- these bundles are
# sparse (e.g. 11784-12106-12179 skips ~300 intervening PRs).
#
# Bundle membership was derived from git, not guessed: for every record the
# file set of fix_patch + test_patch reproduces `git diff <base.sha>..<end>`
# EXACTLY, and prs_in_bundle is the set of merged PRs whose commits fall in
# that range. The 23 bundles partition 278 PRs with zero overlap, and every
# record's lead PR falls inside its own bundle.
#
# Bundle-level, NOT pr-level: one key per bundle, so #keys == #instances.
# Keys are data-derived -- REGENERATE this list whenever the bundles change.
# ---------------------------------------------------------------------------
_BUNDLE_NIS_Hugo = [
    "9294-9379-9387-9391-9395-9398-9399-9428-9430",
    "9589-9601-9606-9607-9623-9624-9626-9628-9630-9634-9637-9640",
    "9643-9653-9672-9674-9679",
    "9843-9912-9914-9920-9923-9926-9928-9938-9941-9942-9943-9945-9948-9952-9955",
    "10038-10054-10077-10080-10087-10111-10119-10139-10146-10155-10182-10189-10192-10193-10195-10205-10206-10211-10212-10213-10217",
    "10284-10295-10300-10308-10310-10312-10316-10317-10319",
    "10471-10476-10479-10481-10484-10486-10488-10490-10493-10496-10499-10500-10502-10506",
    "10731-10784-10786-10787-10788-10790-10793",
    "10847-11148-11152-11156-11158-11166-11170-11173-11174-11175-11177-11178-11181-11182-11185",
    "11220-11252-11261-11269-11270",
    "11308-11316-11317-11318-11320-11322-11323-11324-11326-11329-11330-11331-11332-11333-11334-11337-11341-11344",
    "11636-11741-11795-11866-11880-11885-11910",
    "11784-12106-12179-12204-12209-12211",
    "12069-12199-12222-12227-12229-12233-12234-12235-12238-12239-12240-12246-12247-12248-12253-12255-12257-12259-12260-12262-12264-12265-12267-12268",
    "12098-12099-12102-12109-12116-12117",
    "12135-12139-12145-12149-12150-12158-12161",
    "12242-12286-12292-12298-12299-12300-12301-12302-12311-12312-12313-12322-12323-12327-12328-12329-12331-12332-12335-12336-12337-12338-12341-12343-12345-12346-12349-12352-12353-12354-12355-12361-12363-12364-12367-12370-12371-12372-12373-12374-12379",
    "12505-12759-12784-12798-12800-12804-12807-12809-12810-12812-12813-12815-12819",
    "12512-12515-12524-12528-12529-12551-12553",
    "12832-12870-12871-12877",
    "12887-12889-12891-12893-12894-12895-12901-12905-12907-12908-12921-12924-12927-12928-12930-12931-12933-12934-12936-12938",
    "12966-12968-12987-12989-12995-12996-13000-13005-13006-13008",
    "13108-13112-13114",
]


@Instance.register("gohugoio", "hugo")
class Hugo(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HugoImageDefault(self.pr, self._config)

    # The three graded stages run the scripts baked into the image
    # (PIPELINE.md S4) rather than an inline `bash -c` string, so what ships in
    # the image is exactly what is measured, and so the command survives the
    # f-string interpolation on the envagent path in build_dataset.py.
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
                    if test_name not in failed_tests:
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


# --- bundle routing keys -------------------------------------------------
# Instance.create() looks up f"{org}/{number_interval}", so every dash-joined
# bundle value the JSONL carries must be a registered key or it raises
# ValueError: not registered. The JSONL and this registry ship together.
for _ni in _BUNDLE_NIS_Hugo:
    Instance.register("gohugoio", _ni)(Hugo)


# --- number_interval must reach the resolved JSONL -----------------------
# Dataset.build() copies number_interval straight off the loaded PullRequest
# into the resolved/classified output (dataset.py), so a record that arrives
# with an EMPTY number_interval but a populated prs_in_bundle would silently
# emit an empty interval downstream. This gohugoio/hugo-scoped shim fills it
# at load time. Only empty values are filled, so an explicitly-set
# number_interval is never overwritten and no other repo is affected.
#
# Signature-transparent (*args/**kwargs): the @dataclass_json decorator
# REPLACES the class-body from_dict/from_json, so the live signatures are
# dataclass_json's -- from_dict(cls, kvs, *, infer_missing=False) and
# from_json(cls, s, **kw), whose from_json delegates to cls.from_dict. A
# fixed-arity shim here would break every repo's loader, not just this one.
_HUGO_ORG = "gohugoio"
_HUGO_REPO = "hugo"


def hugo_number_interval(prs_in_bundle) -> str:
    """Dash-join a bundle's PR numbers: [146, 147, 150] -> '146-147-150'."""
    if not prs_in_bundle:
        return ""
    return "-".join(str(p) for p in prs_in_bundle)


def _hugo_fill_number_interval(pr, raw) -> None:
    if not isinstance(raw, dict):
        return
    if getattr(pr, "org", "") != _HUGO_ORG or getattr(pr, "repo", "") != _HUGO_REPO:
        return
    if getattr(pr, "number_interval", ""):
        return
    interval = hugo_number_interval(raw.get("prs_in_bundle"))
    if interval:
        pr.number_interval = interval


if not getattr(PullRequest, "_gohugoio_ni_shim", False):
    _hugo_orig_from_json = PullRequest.from_json.__func__
    _hugo_orig_from_dict = PullRequest.from_dict.__func__

    def _hugo_from_json(cls, *args, **kwargs):
        pr = _hugo_orig_from_json(cls, *args, **kwargs)
        try:
            if args:
                _hugo_fill_number_interval(pr, json.loads(args[0]))
        except Exception:
            pass
        return pr

    def _hugo_from_dict(cls, *args, **kwargs):
        pr = _hugo_orig_from_dict(cls, *args, **kwargs)
        try:
            raw = args[0] if args else kwargs.get("kvs")
            _hugo_fill_number_interval(pr, raw)
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_hugo_from_json)
    PullRequest.from_dict = classmethod(_hugo_from_dict)
    PullRequest._gohugoio_ni_shim = True


# --- routing fallback for regenerated bundles ---------------------------
# hugo is a SINGLE-era repo: exactly one class serves every bundle. If the
# dataset is regenerated with different bundles before _BUNDLE_NIS_Hugo is
# refreshed, routing would hard-fail on an unregistered key. Fall back to the
# repo key rather than losing the record; other orgs/repos are untouched.
if not getattr(Instance, "_gohugoio_route_shim", False):
    _hugo_orig_create = Instance.create.__func__

    def _hugo_create(cls, pr, config, *args, **kwargs):
        try:
            return _hugo_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if getattr(pr, "org", "") != _HUGO_ORG or getattr(pr, "repo", "") != _HUGO_REPO:
                raise
            return cls._registry[f"{_HUGO_ORG}/{_HUGO_REPO}"](pr, config, *args, **kwargs)

    Instance.create = classmethod(_hugo_create)
    Instance._gohugoio_route_shim = True
