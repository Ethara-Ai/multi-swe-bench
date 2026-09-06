import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NUMBER_INTERVAL = "1297-1557-1626-1677-1927-1962-2208-2246-2281-2305-2353-2395-2548-2573-2641-2659-2764-2777-2813-2817-2880-3046-3059-3097-3129-3180-3248-3284-3313-3428-3512-3557-3678-3684-3734-3763-3787-3849-3991-3996-4071-4094-4136-4145-4161-4225-4252-4280-4307-4334-4361-4377-4459-4485-4554-4595-4684"


def _extract_ruby_test_info(test_patch: str) -> dict[str, list[str]]:
    """Extract test methods per Ruby file from a patch.

    Returns dict mapping ruby filename to list of test method names.
    Handles split-file structure (test_core.rb, test_layout.rb, etc.)
    """
    file_methods: dict[str, list[str]] = {}
    current_file = ""
    current_methods: set[str] = set()
    in_ruby_file = False

    for line in test_patch.split("\n"):
        if line.startswith("diff --git"):
            if in_ruby_file and current_methods:
                file_methods[current_file] = sorted(current_methods)
            in_ruby_file = line.endswith(".rb")
            if in_ruby_file:
                parts = line.split(" b/")
                current_file = parts[-1] if len(parts) > 1 else ""
                current_methods = set()
            continue
        if not in_ruby_file:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            m = re.search(r"def (test_\w+)", line)
            if m:
                current_methods.add(m.group(1))
        if line.startswith("@@"):
            m = re.search(r"def (test_\w+)", line)
            if m:
                current_methods.add(m.group(1))

    if in_ruby_file and current_methods:
        file_methods[current_file] = sorted(current_methods)

    return file_methods


class FzfImageBase(Image):
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
        return "golang:1.24-bookworm"

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

        if self.config.need_clone:
            code = f"RUN git clone --no-single-branch https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV GOTOOLCHAIN=local
ENV GOFLAGS="-buildvcs=false -mod=mod"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget patch \\
    ruby ruby-dev tmux locales && \\
    sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen && \\
    rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV TERM=xterm

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class FzfImageDefault(Image):
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
        return FzfImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        ruby_info = _extract_ruby_test_info(self.pr.test_patch)
        ruby_run = ""
        bundle_setup = ""
        if ruby_info:
            has_split = any(
                f != "test/test_go.rb" for f in ruby_info
            )
            if has_split:
                bundle_setup = 'if [ -f Gemfile ]; then gem install bundler && bundle install; fi\n'
            lines = [
                f'cd /home/{self.pr.repo}',
                'mkdir -p bin',
                f'go build -o bin/fzf .',
                'export PATH=$PWD/bin:$PATH',
                'tmux new-session -d -s main 2>/dev/null || true',
            ]
            for ruby_file, methods in ruby_info.items():
                pattern = "|".join(methods)
                class_name = "TestGoFZF"
                if "test_core" in ruby_file:
                    class_name = "TestCore"
                elif "test_layout" in ruby_file:
                    class_name = "TestLayout"
                elif "test_shell_integration" in ruby_file:
                    class_name = "TestBash"
                lines.append(
                    f'ruby {ruby_file} --verbose --name "/^{class_name}#({pattern})$/" || true'
                )
            ruby_run = "\n".join(lines) + "\n"

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

{bundle_setup}go test -v -count=1 ./... || true

""".format(pr=self.pr, bundle_setup=bundle_setup),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 ./...
{ruby_run}
""".format(pr=self.pr, ruby_run=ruby_run),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./... || true
{ruby_run}
""".format(pr=self.pr, ruby_run=ruby_run),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
go test -v -count=1 ./...
{ruby_run}
""".format(pr=self.pr, ruby_run=ruby_run),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


# ---------------------------------------------------------------------------
# number_interval -- bundle routing for junegunn/fzf (PIPELINE.md S11).
#
# Each record is a BUNDLE of PRs, and the required value is the EXACT PRs of
# that bundle joined with '-', never a range:
#
#     prs_in_bundle: [146, 147, 150, 155, 157]
#     number_interval: "146-147-150-155-157"      (NOT "146-157")
#
# A range would claim every PR in between, which is wrong -- these bundles are
# sparse (e.g. 4145-4699-4700-... skips ~550 intervening PRs, and
# 1297-1810-1813 skips ~500).
#
# Bundle membership was derived from git + the GitHub API, not guessed: for
# every record the file set of fix_patch + test_patch reproduces
# `git diff <base.sha>..<end>` EXACTLY, which pins the bundle's end commit;
# prs_in_bundle is then the set of MERGED PRs that landed in that commit range
# (merge commit inside the range, or credited as "(#N)" by a commit in it).
# The bundles cover 664 commits and 171 PRs with zero overlap, and every
# record's lead PR falls inside its own bundle. Two bundles (2208, 2305) were
# dropped at delivery on target-quality grounds, so 17 keys ship here; the
# 19-key list is in fzf.py.bak5.final17.* if they are ever reinstated.
#
# _NUMBER_INTERVAL above is NOT bundle membership -- it is the lead-PR list of
# the original 57-record dataset. It stays registered (harmless) so older
# records keep routing, but the delivered JSONL now carries real bundles.
#
# Bundle-level, NOT pr-level: one key per bundle, so #keys == #instances.
# Keys are data-derived -- REGENERATE this list whenever the bundles change.
# ---------------------------------------------------------------------------
_BUNDLE_NIS_Fzf = [
    "1297-1810-1813-1815-1820-1832-1837-1844-1845-1847-1848-1861-1875-1876-1878-1881-1886-1891-1892-1893-1921",
    "1962-1967-1977-1978-1995-2000-2003-2042-2051-2054-2064-2066-2096-2100-2119",
    "2353-2356-2363-2368-2370-2375-2379-2383",
    "2641-2646-2647",
    "2817-2991-2993-2997-2998-2999-3012-3021",
    "2880-2926-2946-2948-2952-2964-2965-2974-2984-2985",
    "3059-3062-3064-3093-3094-3110-3111-3117",
    "3248-3249-3257",
    "3284-3285-3306",
    "3512-3525-3526-3537-3542-3544",
    "3684-3687-3688-3693-3699-3709",
    "3849-3850-3852-3855-3856-3871-3875-3877-3878-3882-3885-3887-3893-3894-3906-3907-3909-3912-3913",
    "4136-4137-4138-4141-4143-4157-4158-4162-4165-4166-4171-4175-4179-4181-4189",
    "4145-4699-4700-4703-4714-4719-4721-4723-4734-4739-4743-4750-4753",
    "4225-4230-4231-4232-4234-4236",
    "4307-4308",
    "4554-4567-4574-4581-4582-4586-4589-4590",
]


@Instance.register("junegunn", _NUMBER_INTERVAL)
class Fzf(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FzfImageDefault(self.pr, self._config)

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

        re_ruby_pass = re.compile(r"(\S+#test_\w+) = .* = \.")
        re_ruby_fail = re.compile(r"(\S+#test_\w+) \[.+:\d+\]:")
        re_ruby_error = re.compile(r"^(\S+#test_\w+):$")

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
                    if test_name not in passed_tests and test_name not in failed_tests:
                        skipped_tests.add(get_base_name(test_name))

            m = re_ruby_pass.match(line)
            if m:
                name = m.group(1)
                if name not in failed_tests:
                    passed_tests.add(name)

            m = re_ruby_fail.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                failed_tests.add(name)

            m = re_ruby_error.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                failed_tests.add(name)

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
#
# No blanket routing fallback is installed here on purpose: junegunn/fzf is a
# MULTI-era repo (fzf_gopath_preglide / fzf_gopath_glide / fzf_go1_13 /
# fzf_go1_17 / this modern era), so silently defaulting an unknown bundle key
# to Fzf could build a record with the wrong Go toolchain. An unregistered key
# must fail loudly; regenerate _BUNDLE_NIS_Fzf when the bundles change.
for _ni in _BUNDLE_NIS_Fzf:
    Instance.register("junegunn", _ni)(Fzf)


# --- number_interval must reach the resolved JSONL -----------------------
# Dataset.build() copies number_interval straight off the loaded PullRequest
# into the resolved/classified output (dataset.py), so a record that arrives
# with an EMPTY number_interval but a populated prs_in_bundle would silently
# emit an empty interval downstream. This junegunn/fzf-scoped shim fills it at
# load time. Only empty values are filled, so an explicitly-set
# number_interval is never overwritten and no other repo is affected.
#
# Signature-transparent (*args/**kwargs): the @dataclass_json decorator
# REPLACES the class-body from_dict/from_json, so the live signatures are
# dataclass_json's -- from_dict(cls, kvs, *, infer_missing=False) and
# from_json(cls, s, **kw), whose from_json delegates to cls.from_dict. A
# fixed-arity shim here would break every repo's loader, not just this one.
_FZF_ORG = "junegunn"
_FZF_REPO = "fzf"


def fzf_number_interval(prs_in_bundle) -> str:
    """Dash-join a bundle's PR numbers: [146, 147, 150] -> '146-147-150'."""
    if not prs_in_bundle:
        return ""
    return "-".join(str(p) for p in prs_in_bundle)


def _fzf_fill_number_interval(pr, raw) -> None:
    if not isinstance(raw, dict):
        return
    if getattr(pr, "org", "") != _FZF_ORG or getattr(pr, "repo", "") != _FZF_REPO:
        return
    if getattr(pr, "number_interval", ""):
        return
    interval = fzf_number_interval(raw.get("prs_in_bundle"))
    if interval:
        pr.number_interval = interval


if not getattr(PullRequest, "_junegunn_ni_shim", False):
    _fzf_orig_from_json = PullRequest.from_json.__func__
    _fzf_orig_from_dict = PullRequest.from_dict.__func__

    def _fzf_from_json(cls, *args, **kwargs):
        pr = _fzf_orig_from_json(cls, *args, **kwargs)
        try:
            if args:
                _fzf_fill_number_interval(pr, json.loads(args[0]))
        except Exception:
            pass
        return pr

    def _fzf_from_dict(cls, *args, **kwargs):
        pr = _fzf_orig_from_dict(cls, *args, **kwargs)
        try:
            raw = args[0] if args else kwargs.get("kvs")
            _fzf_fill_number_interval(pr, raw)
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_fzf_from_json)
    PullRequest.from_dict = classmethod(_fzf_from_dict)
    PullRequest._junegunn_ni_shim = True
