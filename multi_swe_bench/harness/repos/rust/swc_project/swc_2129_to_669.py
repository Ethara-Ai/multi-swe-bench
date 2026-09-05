from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ENV_BLOCK = r"""export CI=1
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_TERM_COLOR=never
export RUST_BACKTRACE=1
export RUST_LOG=off
export CARGO_BUILD_JOBS=2
"""

_TOOLCHAIN_BLOCK = r"""resolve_toolchain() {
    pinned=""
    if [ -f rust-toolchain.toml ]; then
        pinned=$(grep -m1 '^ *channel *=' rust-toolchain.toml | sed 's/.*"\(.*\)".*/\1/')
    elif [ -f rust-toolchain ]; then
        pinned=$(tr -d '[:space:]' < rust-toolchain)
    fi
    if [ -z "$pinned" ] || [ "$pinned" = "nightly" ]; then
        pinned="nightly-2020-02-16"
    fi
    echo "$pinned"
}

RUSTUP_TOOLCHAIN=$(resolve_toolchain)
export RUSTUP_TOOLCHAIN
"""

_PACKAGES_BLOCK = r"""pkg_name_of() {
    awk '
        /^\[/ { pkg = ($0 ~ /^\[package\]/); next }
        pkg && /^[ \t]*name[ \t]*=/ {
            line = $0
            sub(/^[^=]*=[ \t]*/, "", line)
            sub(/[ \t]*#.*$/, "", line)
            gsub(/[" \t\r]/, "", line)
            gsub(/\047/, "", line)
            print line
            exit
        }
    ' "$1"
}

test_packages() {
    files=$(grep '^diff --git a/' /home/test.patch 2>/dev/null \
            | awk '{ p = $3; sub(/^a\//, "", p); print p }')
    pkgs=""
    for f in $files; do
        dir=$(dirname "$f")
        while :; do
            if [ -f "$dir/Cargo.toml" ]; then
                name=$(pkg_name_of "$dir/Cargo.toml")
                if [ -n "$name" ]; then
                    pkgs="$pkgs $name"
                fi
                break
            fi
            if [ "$dir" = "." ] || [ "$dir" = "/" ]; then
                break
            fi
            dir=$(dirname "$dir")
        done
    done
    if [ -n "$pkgs" ]; then
        printf '%s\n' $pkgs | sort -u
    fi
}

cargo_test_args() {
    PKGS=$(test_packages)
    if [ -n "$PKGS" ]; then
        ARGS=""
        for p in $PKGS; do
            ARGS="$ARGS -p $p"
        done
    else
        ARGS="--workspace"
    fi
    echo "$ARGS"
}
"""

_CARGO_TEST_BLOCK = r"""list_test_targets() {
    cargo metadata --no-deps --format-version 1 2>/dev/null | node -e "
let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{
  try{
    const m=JSON.parse(s);
    for(const p of (m.packages||[])){
      if(p.name!=='$1') continue;
      for(const t of (p.targets||[])){
        if((t.kind||[]).includes('test')) console.log(t.name);
      }
    }
  }catch(e){}
});"
}

ARGS=$(cargo_test_args)
echo "=== Testing packages:$ARGS ==="

LOG=$(mktemp)
set +e
if [ "$ARGS" = "--workspace" ]; then
    cargo test --workspace --no-fail-fast 2>&1 | tee -a "$LOG"
else
    for PKG in $(printf '%s\n' $ARGS | grep -v '^-p$'); do
        cargo test -p "$PKG" --lib --no-fail-fast 2>&1 | tee -a "$LOG"
        for T in $(list_test_targets "$PKG"); do
            echo "--- $PKG --test $T ---"
            cargo test -p "$PKG" --test "$T" --no-fail-fast 2>&1 | tee -a "$LOG"
        done
    done
fi
set -e
if ! grep -q '^test result:' "$LOG"; then
    echo "=== BUILD FAILURE: no test binary ran ==="
fi
rm -f "$LOG"
exit 0
"""

_PREPARE_BODY = r"""for RUSTUP_TRY in 1 2 3 4 5; do
    if rustup toolchain install "$RUSTUP_TOOLCHAIN" --profile minimal 2>&1; then
        break
    fi
    echo "=== rustup install attempt $RUSTUP_TRY failed; retrying ==="
    sleep $((RUSTUP_TRY * 5))
done
rustup show

git submodule update --init --recursive 2>&1 || true

install_js_deps() {
    if [ -f package.json ]; then
        if [ -f yarn.lock ]; then
            npx --yes yarn install --frozen-lockfile 2>&1 \
                || npx --yes yarn install 2>&1 \
                || true
        else
            npm install 2>&1 || true
        fi
    fi
}

install_js_deps

if [ ! -f Cargo.lock ]; then
    BASE_DATE=$(git show -s --format=%cI HEAD)
    echo "=== reconstructing Cargo.lock as of $BASE_DATE ==="

    if ! cargo generate-lockfile >/dev/null 2>&1; then
        echo "=== $RUSTUP_TOOLCHAIN cannot resolve this manifest; trying a newer cargo ==="
        for FALLBACK in nightly-2022-02-01 nightly-2022-08-01; do
            for FB_TRY in 1 2 3; do
                rustup toolchain install "$FALLBACK" --profile minimal >/dev/null 2>&1 && break
                sleep $((FB_TRY * 5))
            done
            if RUSTUP_TOOLCHAIN=$FALLBACK cargo generate-lockfile >/dev/null 2>&1; then
                export RUSTUP_TOOLCHAIN=$FALLBACK
                echo "=== resolution toolchain switched to $FALLBACK ==="
                break
            fi
        done
    fi
    rm -f Cargo.lock

    pick_version() {
        for ATTEMPT in 1 2 3 4; do
            RESULT=$(pick_version_once "$1" "$2" "$3")
            if [ -n "$RESULT" ]; then echo "$RESULT"; return 0; fi
            sleep $((ATTEMPT * 2))
        done
        echo ""
    }

    CRATE_CACHE=/tmp/crates_cache
    mkdir -p "$CRATE_CACHE"
    fetch_versions() {
        _cf="$CRATE_CACHE/$1.json"
        if [ ! -s "$_cf" ]; then
            curl -sS --max-time 20 -H "User-Agent: multi-swe-bench" \
                "https://crates.io/api/v1/crates/$1/versions" 2>/dev/null > "$_cf.tmp"
            if node -e "
try{const d=JSON.parse(require('fs').readFileSync('$_cf.tmp','utf8'));
process.exit(Array.isArray(d.versions)&&d.versions.length?0:1);}catch(e){process.exit(1);}" 2>/dev/null; then
                mv "$_cf.tmp" "$_cf"
            else
                rm -f "$_cf.tmp"
                return 1
            fi
        fi
        cat "$_cf"
    }

    pick_version_once() {
        fetch_versions "$1" | node -e "
let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{
  try{
    const d=JSON.parse(s), cut='$2', locked='$3';
    const lp=locked.split('.').map(x=>parseInt(x,10));
    const compat=(v)=>{const p=v.split('-')[0].split('.').map(x=>parseInt(x,10));
      if(p.some(isNaN))return false;
      if(lp[0]>0)return p[0]===lp[0];
      if(lp[1]>0)return p[0]===0&&p[1]===lp[1];
      return p[0]===0&&p[1]===0;};
    const cmp=(a,b)=>{const A=a.split('-')[0].split('.').map(Number),B=b.split('-')[0].split('.').map(Number);
      for(let i=0;i<3;i++){if((A[i]||0)!==(B[i]||0))return (A[i]||0)-(B[i]||0);}return 0;};
    let best=null;
    for(const v of (d.versions||[])){
      if(v.yanked||v.num.includes('-'))continue;
      if(v.created_at>cut)continue;
      if(!compat(v.num))continue;
      if(!best||cmp(v.num,best)>0)best=v.num;
    }
    console.log(best||'');
  }catch(e){console.log('');}
});"
    }

    unwind_blockers() {
        _err="$1"; _depth="$2"
        [ "$_depth" -gt 4 ] && return 1
        _blk=$(printf '%s' "$_err" | grep -oE 'required by package `[^`]+`' \
               | head -1 | sed 's/required by package `//; s/`$//')
        _bn=$(printf '%s' "$_blk" | awk '{print $1}')
        _bv=$(printf '%s' "$_blk" | awk '{print $2}' | sed 's/^v//')
        [ -z "$_bn" ] || [ -z "$_bv" ] && return 1
        _bnew=$(pick_version "$_bn" "$BASE_DATE" "$_bv")
        [ -z "$_bnew" ] || [ "$_bnew" = "$_bv" ] && return 1
        if _e2=$(cargo update -p "$_bn:$_bv" --precise "$_bnew" 2>&1); then
            CHANGED=$((CHANGED+1))
            echo "===   [d$_depth] lowered blocker $_bn $_bv -> $_bnew ==="
            return 0
        fi
        unwind_blockers "$_e2" $((_depth+1))
    }

    cargo -Z minimal-versions generate-lockfile 2>&1 | tail -2

    for PASS in 1 2 3 4 5; do
        node -e "
const fs=require('fs');const t=fs.readFileSync('Cargo.lock','utf8');const out=[];
for(const blk of t.split('[[package]]')){
  const n=(blk.match(/^\s*name = \"(.*)\"/m)||[])[1];
  const v=(blk.match(/^\s*version = \"(.*)\"/m)||[])[1];
  if(n&&v&&/^\s*source = /m.test(blk)) out.push(n+' '+v);
}
console.log(out.join('\n'));
" > /tmp/pkgs.txt
        CHANGED=0
        while read -r NAME VER; do
            [ -z "$NAME" ] && continue
            NEW=$(pick_version "$NAME" "$BASE_DATE" "$VER")
            if [ -n "$NEW" ] && [ "$NEW" != "$VER" ]; then
                if ERR=$(cargo update -p "$NAME:$VER" --precise "$NEW" 2>&1); then
                    CHANGED=$((CHANGED+1))
                else
                    if unwind_blockers "$ERR" 1; then :; fi
                    cargo update -p "$NAME:$VER" --precise "$NEW" >/dev/null 2>&1 \
                        && CHANGED=$((CHANGED+1))
                fi
            fi
        done < /tmp/pkgs.txt
        echo "=== lockfile rewind pass $PASS changed $CHANGED crates ==="
        [ "$CHANGED" -eq 0 ] && break
    done

    parents_of() {
        node -e "
const fs=require('fs');const t=fs.readFileSync('Cargo.lock','utf8');
for(const blk of t.split('[[package]]')){
  const n=(blk.match(/^\s*name = \"(.*)\"/m)||[])[1];
  const v=(blk.match(/^\s*version = \"(.*)\"/m)||[])[1];
  if(!n||!v||n==='$1') continue;
  const dep=blk.match(/dependencies = \[([^\]]*)\]/s);
  if(dep && new RegExp('\"'+'$1'+'( |\")').test(dep[1])) console.log(n+' '+v);
}"
    }

    mkdir -p /tmp/hpdr
    has_pre_date_release() {
        HPDR_C="/tmp/hpdr/$1"
        if [ -f "$HPDR_C" ]; then
            [ "$(cat "$HPDR_C")" = yes ] && echo yes
            return
        fi
        for HPDR_TRY in 1 2 3; do
            HPDR=$(fetch_versions "$1" | node -e "
let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{
  try{
    const d=JSON.parse(s);
    if(!Array.isArray(d.versions)){ console.log('unknown'); return; }
    for(const v of d.versions){ if(!v.yanked && v.created_at<='$2'){ console.log('yes'); return; } }
    console.log('no');
  }catch(e){ console.log('unknown'); }
});")
            case "$HPDR" in
                yes) echo yes > "$HPDR_C"; echo yes; return ;;
                no)  echo no  > "$HPDR_C"; echo ''; return ;;
            esac
            sleep $((HPDR_TRY * 3))
        done
        echo yes
    }

    cp Cargo.lock /tmp/Cargo.lock.preprune
    set +e

    for PRUNE in 1 2 3; do
        PRUNED=0
        node -e "
const fs=require('fs');const t=fs.readFileSync('Cargo.lock','utf8');const out=[];
for(const blk of t.split('[[package]]')){
  const n=(blk.match(/^\s*name = \"(.*)\"/m)||[])[1];
  const v=(blk.match(/^\s*version = \"(.*)\"/m)||[])[1];
  if(n&&v&&/^\s*source = /m.test(blk)) out.push(n+' '+v);
}
console.log(out.join('\n'));
" > /tmp/pkgs.txt
        while read -r NAME VER; do
            [ -z "$NAME" ] && continue
            [ -n "$(has_pre_date_release "$NAME" "$BASE_DATE")" ] && continue
            echo "===   $NAME $VER never existed at $BASE_DATE; lowering its parents ==="
            parents_of "$NAME" | while read -r PN PV; do
                [ -z "$PN" ] && continue
                PNEW=$(pick_version "$PN" "$BASE_DATE" "$PV")
                if [ -n "$PNEW" ] && [ "$PNEW" != "$PV" ]; then
                    if PE=$(cargo update -p "$PN:$PV" --precise "$PNEW" 2>&1); then
                        echo "===     pruned via $PN $PV -> $PNEW ==="
                    elif unwind_blockers "$PE" 1; then
                        cargo update -p "$PN:$PV" --precise "$PNEW" >/dev/null 2>&1 \
                            && echo "===     pruned via $PN $PV -> $PNEW after unwinding ==="
                    fi
                fi
            done
            PRUNED=$((PRUNED+1))
        done < /tmp/pkgs.txt
        echo "=== prune round $PRUNE handled $PRUNED impossible crates ==="
        [ "$PRUNED" -eq 0 ] && break
    done

    if cargo metadata --format-version 1 >/dev/null 2>&1; then
        echo "=== prune phase kept: lockfile still resolves ==="
    else
        echo "=== prune phase discarded: it broke resolution; restoring pre-prune lockfile ==="
        cp /tmp/Cargo.lock.preprune Cargo.lock
    fi
    set -e

    STALE=""
    while read -r NAME VER; do
        [ -z "$NAME" ] && continue
        PUB=$(curl -sS -H "User-Agent: multi-swe-bench" \
            "https://crates.io/api/v1/crates/$NAME/$VER" 2>/dev/null | node -e "
let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{
  try{ console.log(JSON.parse(s).version.created_at||''); }catch(e){ console.log(''); }
});")
        if [ -n "$PUB" ] && [ "$PUB" \> "$BASE_DATE" ]; then
            STALE="$STALE $NAME@$VER"
        fi
    done < /tmp/pkgs.txt
    if [ -n "$STALE" ]; then
        echo "=== WARNING: crates postdating $BASE_DATE remain:$STALE ==="
    else
        echo "=== lockfile verified: no crate postdates $BASE_DATE ==="
    fi
fi

warm() {
    cargo test $(cargo_test_args) --no-run --no-fail-fast 2>&1 || true
}

warm

if [ -z "$(ls -A target/debug/deps 2>/dev/null)" ]; then
    echo "=== BUILD FAILURE: warm pass produced no artifacts; image would be empty ==="
    exit 1
fi
echo "=== warm pass produced artifacts ==="

git apply --whitespace=nowarn /home/test.patch 2>&1 || true
git apply --whitespace=nowarn /home/fix.patch 2>&1 || true

install_js_deps
warm

git reset --hard
git clean -fd -e node_modules -e Cargo.lock
"""


class SwcLegacyImageBase(Image):
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
        return "rust:1.83-bookworm"

    def image_tag(self) -> str:
        return "base-669-2129"

    def workdir(self) -> str:
        return "base-669-2129"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

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

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image (PR 669..2129)" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    cmake \\
    libssl-dev \\
    nodejs \\
    npm \\
    pkg-config \\
    && rm -rf /var/lib/apt/lists/*

{global_block}
WORKDIR /home/

{code}

WORKDIR /home/{repo}
{clear_block}
CMD ["/bin/bash"]
"""


class SwcLegacyImageDefault(Image):
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
        return SwcLegacyImageBase(self.pr, self.config)

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
                "prepare.sh",
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{env}
{toolchain}
{packages}
{body}
""".format(
                    pr=self.pr,
                    env=_ENV_BLOCK,
                    toolchain=_TOOLCHAIN_BLOCK,
                    packages=_PACKAGES_BLOCK,
                    body=_PREPARE_BODY,
                ),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

{env}
{toolchain}
{packages}
{block}
""".format(
                    pr=self.pr,
                    env=_ENV_BLOCK,
                    toolchain=_TOOLCHAIN_BLOCK,
                    packages=_PACKAGES_BLOCK,
                    block=_CARGO_TEST_BLOCK,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi

{env}
{toolchain}
{packages}
{block}
""".format(
                    pr=self.pr,
                    env=_ENV_BLOCK,
                    toolchain=_TOOLCHAIN_BLOCK,
                    packages=_PACKAGES_BLOCK,
                    block=_CARGO_TEST_BLOCK,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "=== PATCH FAILURE: fix.patch did not apply ==="
    exit 1
fi

{env}
{toolchain}
{packages}
{block}
""".format(
                    pr=self.pr,
                    env=_ENV_BLOCK,
                    toolchain=_TOOLCHAIN_BLOCK,
                    packages=_PACKAGES_BLOCK,
                    block=_CARGO_TEST_BLOCK,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        hardening = Image._HARDENING_BLOCK.replace(
            '"${BASE_COMMIT}"', self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}
{global_block}
{copy_commands}
WORKDIR /home/{self.pr.repo}

{hardening}

RUN bash /home/prepare.sh
{clear_block}"""


@Instance.register("swc-project", "swc_2129_to_669")
class SWC_2129_TO_669(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SwcLegacyImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_running_paren = re.compile(r"^Running\s+.*\(([^()]+)\)$")
        re_running_plain = re.compile(r"^Running\s+(\S+)$")
        re_doc_tests = re.compile(r"^Doc-tests\s+(\S+)$")
        re_hash = re.compile(r"-[0-9a-f]{7,}$")

        re_pass_tests = [re.compile(r"test (\S+) \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (\S+) \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) \.\.\. ignored")]

        target = ""
        for line in log.splitlines():
            line = line.strip()

            match = re_running_paren.match(line) or re_running_plain.match(line)
            if match:
                target = re_hash.sub("", match.group(1).rsplit("/", 1)[-1])
                continue

            match = re_doc_tests.match(line)
            if match:
                target = f"doc::{match.group(1)}"
                continue

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    name = match.group(1)
                    passed_tests.add(f"{target}::{name}" if target else name)

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    name = match.group(1)
                    failed_tests.add(f"{target}::{name}" if target else name)

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    name = match.group(1)
                    skipped_tests.add(f"{target}::{name}" if target else name)

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
