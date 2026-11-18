import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "cachix"
_ERA = "devenv_3275_to_3275"
_NIX_IMAGE = "nixos/nix:2.35.4"
_BASE_TAG = "base-nix"
_TEST_ARGS = ""
_DEVENV_FLAKE = "github:cachix/devenv/v2.4.0"
_NIX_CONF = (
    "experimental-features = nix-command flakes",
    "sandbox = false",
    "filter-syscalls = false",
    "accept-flake-config = true",
    "max-jobs = auto",
    "extra-substituters = https://devenv.cachix.org https://cachix.cachix.org",
    "extra-trusted-public-keys = devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw= "
    "cachix.cachix.org-1:eWNHQldwUO7G2VkjpnjDbWwy4KQ/HNxht7H4SSoMckM=",
)

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

_DEV_ENV = """export DEVENV_TUI=false
export CI=true
devenv version
devenv print-dev-env > /home/devenv-env.sh
git reset --hard
git clean -fd"""

_ENV = """repo_dir=$(pwd)
set +e
source /home/devenv-env.sh
set -e
cd "$repo_dir"
export CI=true
export DEVENV_TUI=false
export CARGO_TERM_COLOR=never
mkdir -p "${TMPDIR:-/tmp}"
rustc --version
cargo --version"""

def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _prepare_script(pr: PullRequest) -> str:
    return _sections(
        "#!/bin/bash\nset -e",
        f"cd /home/{pr.repo}\ngit reset --hard\ngit clean -fdx\nbash /home/check_git_changes.sh\ngit checkout --detach {pr.base.sha}\nbash /home/check_git_changes.sh",
        _DEV_ENV,
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


class DevenvImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class DevenvBaseImage(DevenvImage):
    def dependency(self) -> str:
        return _NIX_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        nix_conf = "\\n".join(_NIX_CONF)
        header = "\n\n".join(
            [
                f"FROM {_NIX_IMAGE}",
                f'{DockerfileEnhancer._TARGETARCH_ARG}\nARG REPO_URL="https://github.com/{org}/{repo}.git"\nARG BASE_COMMIT',
                DockerfileEnhancer._PROXY_ARGS,
                DockerfileEnhancer._ENV_BLOCK + " \\\n    LC_ALL=C.UTF-8 \\\n    PATH=/opt/devenv/bin:/usr/local/bin:${PATH}",
                f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
                f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
                f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
                f'      org.opencontainers.image.authors="https://www.ethara.ai/"',
            ]
        )
        return _sections(
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            header,
            "RUN mkdir -p /etc/ssl/certs /usr/local/bin && \\\n"
            "    for c in /etc/ssl/certs/ca-bundle.crt /nix/var/nix/profiles/default/etc/ssl/certs/ca-bundle.crt /root/.nix-profile/etc/ssl/certs/ca-bundle.crt; do \\\n"
            "        if [ -e \"$c\" ]; then ln -sf \"$(readlink -f \"$c\")\" /etc/ssl/certs/ca-certificates.crt && break; fi; \\\n"
            "    done && \\\n"
            "    test -s /etc/ssl/certs/ca-certificates.crt && \\\n"
            "    ln -sf \"$(readlink -f \"$(command -v bash)\")\" /bin/bash",
            DockerfileEnhancer._CERT_SYMLINKS,
            f"RUN printf '%b\\n' '{nix_conf}' >> /etc/nix/nix.conf && nix --version",
            f"RUN nix build --out-link /opt/devenv {_DEVENV_FLAKE}#devenv && \\\n"
            f"    for p in $(nix build --no-link --print-out-paths --inputs-from {_DEVENV_FLAKE} nixpkgs#gnused^out nixpkgs#gawk^out); do ln -sf \"$p\"/bin/* /usr/local/bin/; done && \\\n"
            "    devenv version && sed --version && awk --version",
            "RUN git config --global --add safe.directory '*'",
            self.global_env,
            f'WORKDIR /home/\n\nRUN git clone "${{REPO_URL}}" /home/{repo}',
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class DevenvPrImage(DevenvImage):
    def dependency(self) -> Image:
        return DevenvBaseImage(self.pr, self._config)

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
class DEVENV_3275_TO_3275(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DevenvPrImage(self.pr, self._config)

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
