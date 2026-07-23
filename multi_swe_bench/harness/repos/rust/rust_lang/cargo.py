from __future__ import annotations
import json as _cargo_json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Level 1: a SINGLE shared toolchain base for every cargo PR in the dataset.

    dependency() returns the string ``rust:latest`` (which ships rustup), and on
    top of it the old-era compiler ``1.86.0`` is installed alongside the image's
    default stable. Old-era cargo revisions (PR <= 7848) do not build under a
    current rustc, so their ImageDefault selects 1.86.0 with ``rustup override``
    while modern PRs use the default stable -- both from this one base tag.

    This Dockerfile carries the ``# syntax`` directive, so
    DockerfileEnhancer.enhance() returns it verbatim: no proxy args, no
    CA-certificate symlink farm, no MITM secret mount. ``ca-certificates`` is the
    distro trust store plain HTTPS needs for ``git clone``, kept deliberately.

    IMPORTANT: this image must NOT clone the repo. The tag is shared by all 12
    PRs, so a clone here would be force-pinned by the hardening pass to whichever
    PR built the base first and would strip every other commit from the history.
    The clone therefore lives per-PR in ImageDefault.
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

    def dependency(self) -> str:
        return "rust:latest"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM rust:latest

ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV TZ=UTC
ENV RUSTFLAGS="--cap-lints=warn"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# Old-era cargo (PR <= 7848) needs a pre-2025 rustc; install 1.86.0 alongside
# the image's default stable so ImageDefault can pick it per-PR via `rustup
# override`. Modern PRs keep the default stable toolchain.
RUN rustup toolchain install 1.86.0

CMD ["/bin/bash"]
"""


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

    # Cargo revisions up to PR 7848 (cargo 0.36-0.43, ~2019-2020) do not compile
    # under a current rustc: type inference and std changed underneath them.
    # Observed on rust:latest, and NOT curable with lint caps because these are
    # hard errors, not lints:
    #
    #   src/cargo/util/config/de.rs:481  E0283 type annotations needed
    #       `Tuple2Deserializer(1i32, env.as_ref())` -- `String: AsRef<_>` is now
    #       satisfied by multiple impls, so `U` is ambiguous.
    #   src/cargo/util/flock.rs:241      E0308 mismatched types
    #       `f.try_lock_shared()` -- `File::try_lock_shared` was stabilised in
    #       std (returning `Result<(), TryLockError>`) and now shadows the fs2
    #       trait method the code expects (`Result<(), io::Error>`).
    #
    # Pinning to 1.86.0 predates the std `try_lock_shared` stabilisation, so the
    # fs2 trait method resolves again and E0308 disappears; the residual E0283 is
    # handled by the de.rs rewrite in _era_setup(). Same toolchain + fixups the
    # sibling `cargo_7848_to_6772` registry uses, inlined here because routing
    # sends every record in this dataset to `rust-lang/cargo` (number_interval
    # carries the PR bundle, never an era key), so that registry is unreachable.
    _OLD_ERA_MAX_PR = 7848
    _OLD_ERA_IMAGE = "rust:1.86.0"

    @property
    def _is_old_era(self) -> bool:
        return self.pr.number <= self._OLD_ERA_MAX_PR

    def dependency(self) -> Image:
        # Level 2: per-PR image FROM the single shared ImageBase. dependency() is
        # now an *Image* (not a string), so the DockerfileEnhancer returns
        # dockerfile() verbatim and the clone/checkout + hardening below are kept
        # exactly as written. build_dataset only passes REPO_URL/BASE_COMMIT as
        # build args for string-dependency images, so those are defaulted inline
        # in dockerfile() instead.
        return ImageBase(self.pr, self._config)

    def _era_setup(self) -> str:
        """Source fixups required before old-era cargo will compile at all.

        Runs after checkout, before hardening. The `rustup override` selects the
        1.86.0 toolchain (pre-installed in ImageBase) for this repo directory;
        modern PRs return "" and keep the base's default stable.
        """
        if not self._is_old_era:
            return ""
        return (
            "# Select the old-era compiler for this checkout (installed in the base).\n"
            "RUN rustup override set 1.86.0\n"
            "\n"
            "# Old cargo's Cargo.lock resolves two transitive deps to versions that\n"
            "# now demand rustc 1.88 (`home@0.5.12` and `ignore@0.4.31`), so a 1.86.0\n"
            "# build aborts before compiling with 'rustc 1.86.0 is not supported by\n"
            "# the following packages'. Pin BOTH down to the last release that builds\n"
            "# on 1.86.0. Verified: with these two pins cargo 0.36-0.43 compile\n"
            "# cleanly on 1.86.0. Do NOT run a bare `cargo update` afterwards -- it\n"
            "# re-bumps them back to the rustc-1.88 versions.\n"
            "RUN cargo update home --precise 0.5.11 2>/dev/null || true\n"
            "RUN cargo update ignore --precise 0.4.18 2>/dev/null || true\n"
            "\n"
            "# Disambiguate `String: AsRef<_>` for Tuple2Deserializer (E0283).\n"
            'RUN sed -i "s/env\\.as_ref()/env.as_str()/" '
            "src/cargo/util/config/de.rs 2>/dev/null || true\n"
        )

    # Per-PR dependency pins for checkouts whose Cargo.lock resolves a transitive
    # dep to a version that will not compile under the toolchain THIS PR builds
    # with, so cargo aborts before a single test runs. Each entry is verified by
    # actually building that PR's image. (Old-era home/ignore pins live in
    # _era_setup; these are for the modern rust:latest PRs.)
    _PR_PINS = {
        # cargo 0.72 pulls time 0.3.20, which fails E0282 under current rustc;
        # 0.3.36 is the first that builds on modern rustc. Verified: pr-10877
        # compiles cleanly with this pin.
        10877: [("time", "0.3.36")],
    }

    def _pr_pins(self) -> str:
        pins = self._PR_PINS.get(self.pr.number)
        if not pins:
            return ""
        lines = [
            "# Per-PR dep pin: a lockfile version does not compile on this toolchain."
        ]
        for crate, ver in pins:
            lines.append(f"RUN cargo update {crate} --precise {ver} 2>/dev/null || true")
        return "\n".join(lines) + "\n"

    def image_prefix(self) -> str:
        return "envagent"

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
                "apply_patches.sh",
                r"""#!/bin/bash
# apply_patches.sh <patch>... -- apply patches to the CWD repo, strictly.
#
# Two dataset-side quirks make a bare `git apply` fail on otherwise good
# records, so each is handled explicitly rather than papered over with
# `|| true` (a partial apply would score tests against a tree that never
# received the patch):
#
#   1. Binary files. The patches were generated with plain `git diff` rather
#      than `git diff --binary`, so a binary change is recorded as a
#      contentless placeholder with an abbreviated index line:
#
#          diff --git a/benches/.../global-cache-sample b/...
#          index 1234abc..5678def 100644
#          Binary files a/... and b/... differ
#
#      There is no blob data to apply, so git refuses the WHOLE patch with
#      "cannot apply binary patch ... without full index line". Those sections
#      carry zero information, so they are dropped and everything else applies.
#
#   2. CRLF fixtures. Some test fixtures are stored with CRLF endings (they
#      exist precisely to test CRLF handling), but the patch context lines are
#      LF-only, so context matching fails with "patch does not apply".
#      `--ignore-whitespace` matches context ignoring line-ending differences.
#      Note `--whitespace=nowarn` does NOT help here: it governs whitespace
#      errors on *added* lines, not context matching.
#
# Any other failure is a real failure and must abort.
set -eo pipefail

stripped=()
for p in "$@"; do
  s="${p%.patch}.stripped.patch"
  awk '
    /^diff --git /             { if (n && !skip) printf "%s", buf; buf=""; skip=0; n=1 }
    /^Binary files .* differ$/ { skip=1 }
                               { buf = buf $0 "\n" }
    END                        { if (n && !skip) printf "%s", buf }
  ' "$p" > "$s"

  dropped=$(grep -c '^Binary files .* differ$' "$p" || true)
  if [ "${dropped:-0}" -gt 0 ]; then
    echo "apply_patches: $p: dropped $dropped contentless binary section(s):" >&2
    grep '^Binary files .* differ$' "$p" | sed 's/^/apply_patches:   /' >&2
  fi
  stripped+=("$s")
done

git apply --ignore-whitespace "${stripped[@]}"
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

cargo test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Baseline stage: no patch applied.
#
# --no-fail-fast is required for correct grading, not just for coverage: without
# it cargo stops at the first failing test binary and every later binary reports
# nothing at all. Those tests then parse as absent rather than failing, and
# Report.check()'s gate 2 only rejects PASS->FAIL, never PASS->NONE -- so a
# truncated run silently understates the baseline and inflates the diff the fix
# patch appears to produce.
set -e

cd /home/{pr.repo}
cargo test --no-fail-fast

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# Test-only stage: the test patch MUST apply cleanly (see apply_patches.sh).
set -e

cd /home/{pr.repo}
bash /home/apply_patches.sh /home/test.patch
cargo test --no-fail-fast

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
# Gold stage: test patch + fix patch, applied together in one strict, atomic
# `git apply` so a half-applied tree can never be scored.
set -e

cd /home/{pr.repo}
bash /home/apply_patches.sh /home/test.patch /home/fix.patch
cargo test --no-fail-fast

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this file verbatim (image.py: `if cls.SYNTAX_DIRECTIVE in raw`),
        # so _infrastructure_block never runs against it. That is deliberate:
        # it suppresses the proxy build args (http_proxy/https_proxy/no_proxy),
        # the proxy + SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE entries of
        # the shared ENV block, the CA-certificate symlink farm, and the MITM
        # certificate secret mount. No proxy or certificate configuration is
        # injected into this image.
        #
        # Opting out also skips the enhancer's other passes, so everything still
        # required is declared inline below:
        #
        #   * TARGETARCH / REPO_URL / BASE_COMMIT args. build_dataset passes
        #     REPO_URL and BASE_COMMIT as --build-arg for string-dependency
        #     images (dependency() returns "rust:latest"), so they must be
        #     declared here to be consumed.
        #   * The non-proxy ENV settings (DEBIAN_FRONTEND/LANG/TZ) and the OCI
        #     labels, which the infrastructure block would otherwise have
        #     supplied.
        #   * _standardize_repo_fetch / _inject_final_sanitize no longer run, so
        #     the canonical fetch form is written out directly: clone from
        #     "${REPO_URL}", check out ${BASE_COMMIT}, then _HARDENING_BLOCK as
        #     the last git operation before CMD. The only COPYs after it write
        #     to /home/ (never the repo tree), so the block's assertions still
        #     describe the shipped image.
        #
        # `ca-certificates` is kept in the apt install: that is the distro trust
        # store plain HTTPS needs for `git clone`, not proxy/MITM interception
        # config. Removing the symlink farm does not remove the need for it.
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        org, repo = self.pr.org, self.pr.repo
        image = self.dependency()
        base_ref = f"{image.image_name()}:{image.image_tag()}"
        era_setup = self._era_setup()
        pr_pins = self._pr_pins()
        # BASE_COMMIT is defaulted to this PR's sha: dependency() is now an Image,
        # so build_dataset does NOT pass REPO_URL/BASE_COMMIT as build args (those
        # are only injected for string-dependency images). REPO_URL keeps a default
        # too. The apt install, RUSTFLAGS, DEBIAN_FRONTEND/LANG/TZ and labels are
        # inherited from ImageBase and are not repeated here.
        return f"""# syntax=docker/dockerfile:1.6
FROM {base_ref}

ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT="{self.pr.base.sha}"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{era_setup}
{pr_pins}
{Image._HARDENING_BLOCK}
{copy_commands}
CMD ["/bin/bash"]
"""


@Instance.register("rust-lang", "cargo")
class Cargo(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"test (\S+) ... ok")]
        re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) ... ignored")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval auto-population -- REGISTRY-SCOPED shim (no other file edited).
#
# The output dataset jsonl's `number_interval` is written from the loaded
# PullRequest (Dataset.build -> number_interval=pr.number_interval), but the
# bundle's PR list (`prs_in_bundle`) is dropped when the raw record is parsed
# into a PullRequest, and the harness never derives it. The rust-lang/cargo
# dataset ships `prs_in_bundle` but leaves `number_interval` empty on every
# record, so without this it would stay "" in the resolved jsonl.
#
# The interval is the EXACT PRs in the bundle joined with "-", NOT a first-last
# range: prs_in_bundle [146, 147, 150, 155, 157] -> "146-147-150-155-157".
# A "146-157" range would wrongly imply every PR in between is included; the
# cargo bundles are sparse (e.g. [7848, 8004], [16020, 16032, 16050, 16057]),
# so a range would over-claim by hundreds of PRs.
#
# As this must live ONLY in the registry, we install two small, idempotent,
# rust-lang/cargo-scoped shims at import time (this file is the only one
# changed):
#
#   1. PullRequest.from_json -- for rust-lang/cargo records whose
#      number_interval is empty, fill it from the raw line's prs_in_bundle.
#      That value then flows straight into the output dataset record.
#   2. Instance.create -- a non-empty number_interval makes routing look up
#      `rust-lang/<that-list>`, which is not a registered key; fall back to
#      `rust-lang/cargo` so the build still routes. Other repos are unaffected:
#      shim 1 only fills rust-lang/cargo, and era-keyed datasets keep their
#      pre-set number_interval (only EMPTY values are filled) whose
#      `org/<era>` key is registered (so the fallback never triggers for them).
#      Shims installed by other registries chain safely: each re-raises when the
#      org/repo is not its own.
# ---------------------------------------------------------------------------

_CARGO_ORG = "rust-lang"
_CARGO_REPO = "cargo"


def _cargo_interval_from_raw(json_str: str) -> str:
    """Return the dash-joined prs_in_bundle for a raw record, or "" if absent.

    Bundle order is preserved as delivered (the dataset ships them ascending);
    values are emitted verbatim so the string round-trips the source list.
    """
    try:
        prs = (_cargo_json.loads(json_str) or {}).get("prs_in_bundle") or []
    except Exception:
        return ""
    return "-".join(str(p) for p in prs)


if not getattr(PullRequest, "_cargo_ni_shim", False):
    _cargo_orig_from_json = PullRequest.from_json.__func__

    def _cargo_from_json(cls, json_str):
        pr = _cargo_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == _CARGO_ORG
                and getattr(pr, "repo", "") == _CARGO_REPO
                and not getattr(pr, "number_interval", "")
            ):
                interval = _cargo_interval_from_raw(json_str)
                if interval:
                    pr.number_interval = interval
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_cargo_from_json)
    PullRequest._cargo_ni_shim = True

if not getattr(Instance, "_cargo_route_shim", False):
    _cargo_orig_create = Instance.create.__func__

    def _cargo_create(cls, pr, config, *args, **kwargs):
        try:
            return _cargo_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _CARGO_ORG
                and getattr(pr, "repo", "") == _CARGO_REPO
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_cargo_create)
    Instance._cargo_route_shim = True
