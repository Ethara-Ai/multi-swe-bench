import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "Amanieu"
_ERA = "parking_lot_533_to_533"
_RUST_IMAGE = "rust:1.99.0-bookworm"
_BASE_TAG = "base-rust-bookworm"
_TEST_ARGS = "--test-threads=1"
_APT_PACKAGES = "git ca-certificates curl build-essential pkg-config"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RESULT = re.compile(r"(?:^|\s)test (\S+) \.\.\. (ok|FAILED|ignored)\b")
_MARK = re.compile(r"^MSWE_TARGET_(BEGIN|END) (\S+)(?: (\d+))?$")
_UNBUILT = re.compile(r"^MSWE_UNBUILT (\w+)$")

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: Not inside a git repository"
    exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain | head -20
    exit 1
fi
echo "check_git_changes: No uncommitted changes"\n"""

_COLLECT = """spec_of() {
    local f="$1" d rel name
    d=$(dirname "$f")
    while ! { [ -f "$d/Cargo.toml" ] && grep -q '^\\[package\\]' "$d/Cargo.toml"; }; do
        if [ "$d" = "." ]; then return 0; fi
        d=$(dirname "$d")
    done
    rel="$f"
    if [ "$d" != "." ]; then rel="${f#"$d"/}"; fi
    case "$rel" in
        tests/*/*.rs)
            name="${rel#tests/}"
            if [ -f "$d/tests/${name%%/*}/main.rs" ]; then echo "$d|test|${name%%/*}"; fi ;;
        tests/*.rs)
            if [ -f "$f" ]; then echo "$d|test|$(basename "$rel" .rs)"; fi ;;
        src/*)
            if [ -f "$d/src/lib.rs" ]; then echo "$d|lib|"; elif [ -f "$d/src/main.rs" ]; then echo "$d|bins|"; fi ;;
    esac
    return 0
}

add_specs() {
    local f s
    while IFS= read -r f; do
        s=$(spec_of "$f")
        if [ -n "$s" ]; then specs+=("$s"); fi
    done < <(sed -n 's#^diff --git a/.* b/\\(.*\\.rs\\)$#\\1#p' "$1" | sort -u)
    return 0
}

specs=()
add_specs /home/test.patch
if ! grep -Eq '^diff --git a/.* b/.*\\.rs$' /home/test.patch; then add_specs /home/fix.patch; fi
if [ "${#specs[@]}" -gt 0 ]; then mapfile -t specs < <(printf '%s\\n' "${specs[@]}" | sort -u); fi

target_args() {
    IFS='|' read -r dir kind name <<< "$1"
    label="$dir:$kind"
    args=(--manifest-path "$dir/Cargo.toml" --no-fail-fast)
    case "$kind" in
        test) label="$label:$name"; args+=(--test "$name") ;;
        lib) args+=(--lib) ;;
        bins) args+=(--bins) ;;
    esac
}"""

_RUN_LOOP = """if [ "${#specs[@]}" -eq 0 ]; then
    echo "no cargo test targets exist for the patches in this tree"
    exit 1
fi
rc=0
out=$(mktemp)
for spec in "${specs[@]}"; do
    target_args "$spec"
    echo "MSWE_TARGET_BEGIN $label"
    code=0
    cargo test "${args[@]}" -- @TESTARGS@ 2>&1 | tee "$out" || code=$?
    if [ "$code" -ne 0 ] && [ "$kind" = test ] && ! grep -Eq '^running [0-9]+ tests?$' "$out"; then
        src="$dir/tests/$name.rs"
        if [ ! -f "$src" ]; then src="$dir/tests/$name/main.rs"; fi
        [ -f "$src" ] && awk '/^#\\[([A-Za-z_]+::)*test(\\]|\\()/ {p = 1; next} p && /^(#\\[|\\/\\/)/ {next} p {if (match($0, /fn +[A-Za-z0-9_]+/)) {s = substr($0, RSTART, RLENGTH); sub(/^fn +/, "", s); print "MSWE_UNBUILT " s} p = 0}' "$src"
    fi
    echo "MSWE_TARGET_END $label $code"
    if [ "$code" -ne 0 ]; then rc=$code; fi
done
exit $rc"""

_WARM_LOOP = """for spec in "${specs[@]}"; do
    target_args "$spec"
    cargo test "${args[@]}" --no-run || true
done"""

_TOOLCHAIN = """channel=$(cat rust-toolchain.toml rust-toolchain 2>/dev/null | sed -n 's/^[[:space:]]*channel[[:space:]]*=[[:space:]]*"\\([^"]*\\)".*/\\1/p;/^[0-9][0-9.]*$/p' | head -n 1)
if ! printf '%s' "$channel" | grep -Eq '^[0-9]+\\.[0-9]+(\\.[0-9]+)?$'; then
    channel="$RUST_VERSION"
fi
rustup toolchain install "$channel" --profile minimal -c rustfmt
echo "export RUSTUP_TOOLCHAIN=$channel" > /home/toolchain.env"""

_ENV = """source /home/toolchain.env
export CI=true
export CARGO_TERM_COLOR=never
rustc --version"""

def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _prepare_script(pr: PullRequest) -> str:
    return _sections(
        "#!/bin/bash\nset -e",
        f"cd /home/{pr.repo}\ngit reset --hard\ngit clean -fdx\nbash /home/check_git_changes.sh\ngit checkout --detach {pr.base.sha}\nbash /home/check_git_changes.sh",
        _TOOLCHAIN,
        _ENV,
        "cargo fetch || true",
        _COLLECT,
        _WARM_LOOP,
        "git checkout -- .\ncargo metadata --offline --format-version 1 > /dev/null\necho DEPS_OK",
    )


def _run_script(repo: str, apply: str = "") -> str:
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        f"cd /home/{repo}",
        _ENV,
        apply,
        _COLLECT,
        _RUN_LOOP.replace("@TESTARGS@", _TEST_ARGS),
    )


class ParkingLotImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class ParkingLotBaseImage(ParkingLotImage):
    def dependency(self) -> str:
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = "\n\n".join(
            [
                f"FROM {_RUST_IMAGE}",
                f'{DockerfileEnhancer._TARGETARCH_ARG}\nARG REPO_URL="https://github.com/{org}/{repo}.git"\nARG BASE_COMMIT',
                DockerfileEnhancer._PROXY_ARGS,
                DockerfileEnhancer._ENV_BLOCK + " \\\n    LC_ALL=C.UTF-8 \\\n    CARGO_RESOLVER_INCOMPATIBLE_RUST_VERSIONS=fallback \\\n    CARGO_INCREMENTAL=0",
                f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
                f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
                f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
                f'      org.opencontainers.image.authors="https://www.ethara.ai/"',
                DockerfileEnhancer._CERT_SYMLINKS,
            ]
        )
        return _sections(
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            header,
            f"RUN apt-get update && apt-get install -y --no-install-recommends {_APT_PACKAGES} \\\n    && rm -rf /var/lib/apt/lists/*",
            "RUN git config --system --add safe.directory '*' && rustc --version && cargo --version",
            self.global_env,
            f'WORKDIR /home/\n\nRUN git clone "${{REPO_URL}}" /home/{repo}',
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class ParkingLotPrImage(ParkingLotImage):
    def dependency(self) -> Image:
        return ParkingLotBaseImage(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _ERA)
class PARKING_LOT_533_TO_533(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ParkingLotPrImage(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {"ok": passed_tests, "FAILED": failed_tests, "ignored": skipped_tests}
        label = ""
        for raw in test_log.splitlines():
            text = _ANSI.sub("", raw).strip()
            mark = _MARK.match(text)
            if mark:
                label = mark.group(2) if mark.group(1) == "BEGIN" else ""
                continue
            unbuilt = _UNBUILT.match(text)
            if unbuilt and label:
                failed_tests.add(f"{label}::{unbuilt.group(1)}")
                continue
            result = _RESULT.search(text)
            if result:
                name = f"{label}::{result.group(1)}" if label else result.group(1)
                buckets[result.group(2)].add(name)
        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
