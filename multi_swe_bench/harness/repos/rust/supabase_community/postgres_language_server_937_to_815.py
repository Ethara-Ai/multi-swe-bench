import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "supabase-community"
_ERA = "postgres_language_server_937_to_815"
_RUST_IMAGE = "rust:1.99.0-bookworm"
_BASE_TAG = "base-pg15-bookworm"
_TEST_ARGS = ""
_PG_MAJOR = "15"
_PG_CONFIG = f"/usr/lib/postgresql/{_PG_MAJOR}/bin/pg_config"
_TREE_SITTER = "0.25.9"
_PLPGSQL_CHECK = "v2.7.11"
_PGRX = "0.18.0"
_PGLINTER_URL = "https://github.com/pmpetit/pglinter.git"
_PGLINTER_SHA = "8e8778017c85521b82f44bcc04e21f7662f4868f"
_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
_APT_PACKAGES = f"git ca-certificates curl build-essential pkg-config cmake clang libclang-dev libssl-dev libicu-dev nodejs postgresql-{_PG_MAJOR} postgresql-server-dev-{_PG_MAJOR}"

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

_ENV = f"""source /home/toolchain.env
export CI=true
export CARGO_TERM_COLOR=never
export DATABASE_URL={_DATABASE_URL}
pg_ctlcluster {_PG_MAJOR} main status > /dev/null 2>&1 || pg_ctlcluster {_PG_MAJOR} main start
pg_isready -h 127.0.0.1 -p 5432 -t 60
rustc --version"""

_PLPGSQL_CHECK_RUN = f"""RUN git clone --branch {_PLPGSQL_CHECK} --depth 1 https://github.com/okbob/plpgsql_check.git /tmp/plpgsql_check && \\
    make -C /tmp/plpgsql_check USE_PGXS=1 PG_CONFIG={_PG_CONFIG} && make -C /tmp/plpgsql_check USE_PGXS=1 PG_CONFIG={_PG_CONFIG} install && \\
    rm -rf /tmp/plpgsql_check"""

_PGLINTER_RUN = f"""RUN cargo install cargo-pgrx --version {_PGRX} --locked && \\
    cargo pgrx init --pg{_PG_MAJOR} {_PG_CONFIG} && \\
    git clone {_PGLINTER_URL} /tmp/pglinter && \\
    git -C /tmp/pglinter checkout {_PGLINTER_SHA} && \\
    cd /tmp/pglinter && CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 cargo pgrx install --pg-config {_PG_CONFIG} --release && \\
    cd / && rm -rf /tmp/pglinter /root/.pgrx"""

_DB_INIT_RUN = f"""RUN pg_ctlcluster {_PG_MAJOR} main start && \\
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres -c "ALTER USER postgres PASSWORD 'postgres';" && \\
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres -c "CREATE SCHEMA IF NOT EXISTS extensions;" \\
        -c "CREATE EXTENSION IF NOT EXISTS plpgsql_check SCHEMA extensions;" -c "CREATE EXTENSION IF NOT EXISTS pglinter SCHEMA extensions;" && \\
    pg_ctlcluster {_PG_MAJOR} main stop"""


_BINARIES = """awk '/^diff --git /{f=$4; sub(/^b\\//,"",f)} /^index /{split($2,h,"[.][.]"); n=h[2]} /^Binary files /{print f" "n}'"""

_BLOBS = f"""rm -rf /home/patch_blobs
for patch in /home/test.patch /home/fix.patch; do
    {_BINARIES} "$patch" | while read -r p n; do
        mkdir -p "/home/patch_blobs/$(dirname "$p")"
        git cat-file blob "$n" > "/home/patch_blobs/$p" || rm -f "/home/patch_blobs/$p"
    done
done"""

_APPLY_FN = f"""apply_patches() {{
    local patch p n excludes=()
    for patch in "$@"; do
        while read -r p n; do excludes+=("--exclude=$p"); done < <({_BINARIES} "$patch")
    done
    git apply --whitespace=nowarn "${{excludes[@]}}" "$@"
    for patch in "$@"; do
        while read -r p n; do
            if [ -f "/home/patch_blobs/$p" ]; then mkdir -p "$(dirname "$p")" && cp "/home/patch_blobs/$p" "$p"; else rm -f "$p"; fi
        done < <({_BINARIES} "$patch")
    done
}}"""


def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _prepare_script(pr: PullRequest) -> str:
    return _sections(
        "#!/bin/bash\nset -e",
        f"cd /home/{pr.repo}\ngit reset --hard\ngit clean -fdx\nbash /home/check_git_changes.sh\ngit checkout --detach {pr.base.sha}\ngit submodule update --init --recursive\nbash /home/check_git_changes.sh",
        _TOOLCHAIN,
        _ENV,
        _BLOBS,
        "cargo fetch || true",
        _COLLECT,
        _WARM_LOOP,
        f"pg_ctlcluster {_PG_MAJOR} main stop\ngit checkout -- .\ncargo metadata --offline --format-version 1 > /dev/null\necho DEPS_OK",
    )


def _run_script(repo: str, apply: str = "") -> str:
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        f"cd /home/{repo}",
        _ENV,
        _APPLY_FN,
        apply,
        _COLLECT,
        _RUN_LOOP.replace("@TESTARGS@", _TEST_ARGS),
    )


class PglsImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class PglsBaseImage(PglsImage):
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
                DockerfileEnhancer._ENV_BLOCK + " \\\n    LC_ALL=C.UTF-8 \\\n    CARGO_INCREMENTAL=0",
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
            f"RUN cargo install tree-sitter-cli --version {_TREE_SITTER} --locked && tree-sitter --version",
            _PLPGSQL_CHECK_RUN,
            _PGLINTER_RUN,
            _DB_INIT_RUN,
            self.global_env,
            f'WORKDIR /home/\n\nRUN git clone "${{REPO_URL}}" /home/{repo}',
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class PglsPrImage(PglsImage):
    def dependency(self) -> Image:
        return PglsBaseImage(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = "apply_patches /home/test.patch"
        fix_apply = "apply_patches /home/test.patch /home/fix.patch"
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
class POSTGRES_LANGUAGE_SERVER_937_TO_815(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PglsPrImage(self.pr, self._config)

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
