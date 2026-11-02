import re
import xml.etree.ElementTree as ET
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_RUST_IMAGE = "rust:1.97.1-trixie"
_NODE_IMAGE = "node:22-trixie"
_PROTOC_VERSION = "29.6"
_WASM_BINDGEN_VERSION = "0.2.126"
_INTERVAL = "cua_3958_to_5875"
_SUITE_BEGIN = "cua-suite-begin"
_SUITE_END = "cua-suite-end"
_JUNIT_BEGIN = "cua-junit-begin"
_JUNIT_END = "cua-junit-end"
_SUITE_TIMEOUT = 5400

_RUST_TEST = re.compile(r"^(?P<crate>.+?/crates/[^/]+)/(?P<area>src|tests)/(?P<rest>.+)\.rs$")
_VITEST_TEST = re.compile(r"^(?P<app>.+?)/src/.+\.(?:test|spec)\.[cm]?[jt]sx?$")
_NODE_TEST = re.compile(r"^.+\.test\.[cm]?js$")
_PYTEST_TEST = re.compile(r"^(?:.+/)?(?:test_[^/]+|[^/]+_test)\.py$")

_RUN_ENV = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_HTTP_TIMEOUT=30
export CARGO_HTTP_MULTIPLEXING=false
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_BACKTRACE=1
export PYTHONDONTWRITEBYTECODE=1
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export PUPPETEER_SKIP_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export NODE_OPTIONS=--max-old-space-size=4096"""

_RUN_HELPERS = f"""JUNIT=/tmp/cua-junit.xml

emit_junit() {{
  echo "{_JUNIT_BEGIN}"
  if [ -f "$JUNIT" ]; then
    cat "$JUNIT"
    echo
  fi
  echo "{_JUNIT_END}"
}}

run_rust() {{
  local crate="$1" target="$2" sel
  if [ "$target" != "unit" ]; then
    sel="--test $target"
  elif [ -f "$crate/src/lib.rs" ]; then
    sel="--lib"
  else
    sel="--bins"
  fi
  (cd "$crate" && timeout -k 60 {_SUITE_TIMEOUT} cargo test $sel --no-fail-fast)
}}

run_pytest() {{
  local dir="$1" file="$2" status=0
  rm -f "$JUNIT"
  (cd "$dir" && timeout -k 60 {_SUITE_TIMEOUT} python -m pytest -p no:cacheprovider -v --junitxml="$JUNIT" "$file") || status=$?
  emit_junit
  return "$status"
}}

run_vitest() {{
  local app="$1" file="$2" status=0
  rm -f "$JUNIT"
  (cd "$app" && if [ -f scripts/build-core-wasm.mjs ]; then node scripts/build-core-wasm.mjs; fi && timeout -k 60 {_SUITE_TIMEOUT} node_modules/.bin/vitest run --reporter=default --reporter=junit --outputFile.junit="$JUNIT" "$file") || status=$?
  emit_junit
  return "$status"
}}

run_node() {{
  local file="$1" status=0
  rm -f "$JUNIT"
  timeout -k 60 {_SUITE_TIMEOUT} node --test --test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination="$JUNIT" "$file" || status=$?
  emit_junit
  return "$status"
}}"""

_PREPARE_HELPERS = """host_triple() {
  rustc -vV | sed -n 's/^host: //p'
}

sync_crate_cache() {
  local from="$1" to="$2" part
  for part in cache index; do
    if [ -d "$from/registry/$part" ]; then
      mkdir -p "$to/registry"
      cp -a --update=none "$from/registry/$part" "$to/registry/"
    fi
  done
}

prefetch_crates() {
  python - "$1" "$2" <<'PY'
import concurrent.futures, glob, hashlib, os, sys, tomllib, urllib.request
home, crate = sys.argv[1], os.path.abspath(sys.argv[2])
mirror = os.environ.get("CUA_CRATE_MIRROR", "https://rsproxy.cn/api/v1/crates")
try:
    root = crate
    while root != "/" and not os.path.exists(os.path.join(root, "Cargo.lock")):
        root = os.path.dirname(root)
    with open(os.path.join(root, "Cargo.lock"), "rb") as fh:
        lock = tomllib.load(fh)
    known = glob.glob(os.path.join(home, "registry", "index", "index.crates.io-*"))
    cache = os.path.join(home, "registry", "cache", os.path.basename(known[0]) if known else "index.crates.io-1949cf8c6b5b557f")
    os.makedirs(cache, exist_ok=True)
    todo = [
        p for p in lock.get("package", [])
        if (p.get("source") or "") in ("registry+https://github.com/rust-lang/crates.io-index", "sparse+https://index.crates.io/")
        and p.get("checksum")
        and not os.path.exists(os.path.join(cache, f"{p['name']}-{p['version']}.crate"))
    ]

    def get(p):
        dest = os.path.join(cache, f"{p['name']}-{p['version']}.crate")
        for _ in range(3):
            try:
                req = urllib.request.Request(f"{mirror}/{p['name']}/{p['version']}/download", headers={"User-Agent": "cua-prefetch"})
                data = urllib.request.urlopen(req, timeout=60).read()
                if hashlib.sha256(data).hexdigest() == p["checksum"]:
                    with open(dest + ".part", "wb") as out:
                        out.write(data)
                    os.replace(dest + ".part", dest)
                    return 1
            except Exception:
                pass
        return 0

    if todo and not get(todo[0]):
        print("crate prefetch skipped: mirror unavailable")
    else:
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            done = 1 + sum(pool.map(get, todo[1:])) if todo else 0
        print(f"crate prefetch: {done}/{len(todo)} verified from mirror")
except Exception as exc:
    print(f"crate prefetch skipped: {exc}")
PY
}

fetch_rust() {
  local crate="$1" host home shared="/var/cache/cua-cargo"
  host=$(host_triple)
  home="${CARGO_HOME:-$HOME/.cargo}"
  if [ -d "$shared" ]; then
    sync_crate_cache "$shared" "$home"
    if (cd "$crate" && cargo fetch --locked --offline --target "$host"); then
      return 0
    fi
  fi
  prefetch_crates "$home" "$crate"
  for attempt in 1 2 3 4 5; do
    if (cd "$crate" && cargo fetch --locked --target "$host"); then
      if [ -d "$shared" ]; then
        sync_crate_cache "$home" "$shared"
      fi
      return 0
    fi
    sleep 30
  done
  return 1
}

gate_rust() {
  local crate="$1" host out
  host=$(host_triple)
  out="/tmp/cua-deps-$(echo "$crate" | tr '/' '_').json"
  (cd "$crate" && cargo metadata --locked --offline --filter-platform "$host" --format-version 1 > "$out")
}

verify_crate_cache() {
  python - "${CARGO_HOME:-$HOME/.cargo}" /tmp/cua-deps-*.json <<'PY'
import glob, hashlib, json, os, sys, tomllib
home, files = sys.argv[1], sys.argv[2:]
missing, checked, keep = [], 0, set()
for path in files:
    meta = json.load(open(path))
    with open(os.path.join(meta["workspace_root"], "Cargo.lock"), "rb") as fh:
        lock = tomllib.load(fh)
    sums = {(p["name"], p["version"]): p.get("checksum") for p in lock.get("package", [])}
    needed = {node["id"] for node in (meta.get("resolve") or {}).get("nodes", [])}
    for pkg in meta["packages"]:
        source = pkg.get("source") or ""
        if pkg["id"] not in needed or not source.startswith(("registry+", "sparse+")):
            continue
        checked += 1
        name, version = pkg["name"], pkg["version"]
        keep.add(f"{name}-{version}.crate")
        hits = glob.glob(os.path.join(home, "registry", "cache", "*", f"{name}-{version}.crate"))
        want = sums.get((name, version))
        if not hits:
            missing.append(f"{name}-{version} (absent)")
        elif want and hashlib.sha256(open(hits[0], "rb").read()).hexdigest() != want:
            missing.append(f"{name}-{version} (checksum)")
for extra in glob.glob(os.path.join(home, "registry", "cache", "index.crates.io-*", "*.crate")):
    if os.path.basename(extra) not in keep:
        os.remove(extra)
if missing or not checked:
    print(f"crate cache incomplete: checked={checked} missing={missing[:20]}")
    sys.exit(1)
print(f"crate cache verified: {checked} packages")
PY
}

install_wasm_bindgen() {
  local version="$1" triple name url sum
  case "$(uname -m)" in
    x86_64) triple=x86_64-unknown-linux-musl ;;
    aarch64) triple=aarch64-unknown-linux-musl ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; return 1 ;;
  esac
  name="wasm-bindgen-$version-$triple"
  url="https://github.com/wasm-bindgen/wasm-bindgen/releases/download/$version/$name.tar.gz"
  curl -fsSL --retry 5 -o "/tmp/$name.tar.gz" "$url"
  sum=$(curl -fsSL --retry 5 "$url.sha256sum" | cut -d ' ' -f 1)
  echo "$sum  /tmp/$name.tar.gz" | sha256sum -c -
  tar -xzf "/tmp/$name.tar.gz" -C /tmp
  install -m 0755 "/tmp/$name/wasm-bindgen" /usr/local/cargo/bin/wasm-bindgen
  rm -rf "/tmp/$name" "/tmp/$name.tar.gz"
  wasm-bindgen --version | grep -qF "$version"
}

pnpm_spec() {
  node -e 'const fs=require("fs");const path=require("path");for(const d of [process.argv[1],"."]){const f=path.join(d,"package.json");if(!fs.existsSync(f))continue;const pm=JSON.parse(fs.readFileSync(f,"utf8")).packageManager;if(pm&&pm.startsWith("pnpm@")){console.log(pm.split("+")[0]);process.exit(0)}}process.exit(1)' "$1"
}

provision_app() {
  local app="$1" pm want
  local ws=()
  pm=$(pnpm_spec "$app")
  if [ ! -f "$app/pnpm-workspace.yaml" ]; then
    ws=(--ignore-workspace)
  fi
  for attempt in 1 2 3 4 5; do
    if (cd "$app" && npx --yes "$pm" install --frozen-lockfile "${ws[@]}"); then
      break
    fi
    sleep 30
  done
  test -x "$app/node_modules/.bin/vitest"
  if [ -f "$app/scripts/build-core-wasm.mjs" ]; then
    want=$(sed -nE 's/.*WASM_BINDGEN = "([0-9.]+)".*/\\1/p' "$app/scripts/build-core-wasm.mjs" | head -n 1)
    if [ -n "$want" ] && ! wasm-bindgen --version | grep -qF "$want"; then
      install_wasm_bindgen "$want"
    fi
  fi
}"""


def _suites(test_patch: str) -> list[tuple[str, str, str]]:
    suites: set[tuple[str, str, str]] = set()
    for path in re.findall(r"^diff --git a/(\S+) b/", test_patch, re.M):
        found = _RUST_TEST.match(path)
        if found:
            crate, area, rest = found.group("crate", "area", "rest")
            if area == "src":
                suites.add(("rust", crate, "unit"))
            elif "/" not in rest:
                suites.add(("rust", crate, rest))
            elif rest.endswith("/main") and rest.count("/") == 1:
                suites.add(("rust", crate, rest.split("/")[0]))
            continue
        found = _VITEST_TEST.match(path)
        if found:
            app = found.group("app")
            suites.add(("vitest", app, path[len(app) + 1 :]))
            continue
        if _NODE_TEST.match(path):
            suites.add(("node", ".", path))
            continue
        if _PYTEST_TEST.match(path):
            parent = path.rsplit("/", 1)[0] if "/" in path else "."
            if parent == "tests":
                root = "."
            elif parent.endswith("/tests"):
                root = parent[: -len("/tests")]
            else:
                root = parent
            relative = path if root == "." else path[len(root) + 1 :]
            suites.add(("pytest", root, relative))
    return sorted(suites)


def _suite_id(kind: str, location: str, target: str) -> str:
    if kind == "rust":
        return f"rust:{location}:{target}"
    if kind == "node":
        return f"node:{target}"
    if location == ".":
        return f"{kind}:{target}"
    return f"{kind}:{location}/{target}"


def _test_commands(suites: list[tuple[str, str, str]]) -> str:
    lines = ["rc=0"]
    for kind, location, target in suites:
        lines.append(f'echo "{_SUITE_BEGIN} {_suite_id(kind, location, target)}"')
        if kind == "rust":
            lines.append(f"run_rust {location} {target} || rc=$?")
        elif kind == "pytest":
            lines.append(f"run_pytest {location} {target} || rc=$?")
        elif kind == "vitest":
            lines.append(f"run_vitest {location} {target} || rc=$?")
        else:
            lines.append(f"run_node {target} || rc=$?")
        lines.append(f'echo "{_SUITE_END}"')
    lines.append('exit "$rc"')
    return "\n".join(lines)


def _provision(suites: list[tuple[str, str, str]]) -> str:
    crates = sorted({location for kind, location, _ in suites if kind == "rust"})
    apps = sorted({location for kind, location, _ in suites if kind == "vitest"})
    lines = []
    for crate in crates:
        lines.append(f"fetch_rust {crate}")
    for app in apps:
        lines.append(f"provision_app {app}")
    return "\n".join(lines)


def _gate(suites: list[tuple[str, str, str]]) -> str:
    kinds = {kind for kind, _, _ in suites}
    crates = sorted({location for kind, location, _ in suites if kind == "rust"})
    apps = sorted({location for kind, location, _ in suites if kind == "vitest"})
    lines = [
        "test -n \"$(git rev-parse --verify HEAD)\"",
        "cargo --version",
        "node --version",
        'python -c "import pytest, yaml, jsonschema, toml, cryptography"',
    ]
    for crate in crates:
        lines.append(f"gate_rust {crate}")
    if crates:
        lines.append('rm -rf "${CARGO_HOME:-$HOME/.cargo}/registry/src"')
        lines.append("verify_crate_cache")
    for app in apps:
        lines.append(f"(cd {app} && node_modules/.bin/vitest --version)")
    if "vitest" in kinds:
        lines.append("wasm-bindgen --version")
    return "\n".join(lines)


def _junit_cases(xml_text: str) -> list[tuple[str, str]]:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []
    cases: list[tuple[str, str]] = []

    def walk(node: ET.Element, trail: list[str]) -> None:
        for child in node:
            if child.tag == "testsuite":
                walk(child, trail + [child.get("name", "")])
            elif child.tag == "testcase":
                parts: list[str] = []
                for part in trail + [child.get("classname", ""), child.get("name", "")]:
                    part = re.sub(r"\s+", " ", part or "").strip()
                    if part and (not parts or parts[-1] != part):
                        parts.append(part)
                name = " > ".join(parts)
                tags = {grand.tag for grand in child}
                if tags & {"failure", "error"}:
                    status = "failed"
                elif "skipped" in tags:
                    status = "skipped"
                else:
                    status = "passed"
                cases.append((name, status))

    if root.tag == "testsuite":
        holder = ET.Element("testsuites")
        holder.append(root)
        walk(holder, [])
    else:
        walk(root, [])
    return cases


class Cua3958To5875ImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_RUST_IMAGE}

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CARGO_HTTP_CAINFO=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    PIP_CERT=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PROTOC=/usr/local/bin/protoc

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl unzip xz-utils build-essential pkg-config cmake clang libclang-dev perl \\
    python3 python3-venv python3-dev libssl-dev \\
    libx11-dev libx11-xcb-dev libxcb1-dev libxext-dev libxi-dev libxtst-dev libxrandr-dev \\
    libxfixes-dev libxdamage-dev libxkbcommon-dev libxkbcommon-x11-dev libwayland-dev \\
    libdbus-1-dev libudev-dev libpipewire-0.3-dev libspa-0.2-dev libei-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN arch="${{TARGETARCH:-$(dpkg --print-architecture)}}" && \\
    case "$arch" in \\
        amd64) asset=linux-x86_64 ;; \\
        arm64) asset=linux-aarch_64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac && \\
    curl -fsSL --retry 5 -o /tmp/protoc.zip \\
        "https://github.com/protocolbuffers/protobuf/releases/download/v{_PROTOC_VERSION}/protoc-{_PROTOC_VERSION}-$asset.zip" && \\
    unzip -q -o /tmp/protoc.zip -d /usr/local bin/protoc 'include/*' && \\
    rm -f /tmp/protoc.zip && \\
    protoc --version

COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    node --version && npm --version && npx --version

RUN python3 -m venv /opt/cua-python && \\
    /opt/cua-python/bin/pip install --no-cache-dir \\
        pytest==8.4.2 PyYAML==6.0.3 jsonschema==4.25.1 cryptography==46.0.3 toml==0.10.2 && \\
    /opt/cua-python/bin/python -c "import pytest, yaml, jsonschema, toml, cryptography"

ENV PATH=/opt/cua-python/bin:${{PATH}}

RUN rustup component add rustfmt clippy && \\
    rustup target add wasm32-unknown-unknown && \\
    rustc --version && cargo --version

RUN arch="${{TARGETARCH:-$(dpkg --print-architecture)}}" && \\
    case "$arch" in \\
        amd64) triple=x86_64-unknown-linux-musl ;; \\
        arm64) triple=aarch64-unknown-linux-musl ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac && \\
    name="wasm-bindgen-{_WASM_BINDGEN_VERSION}-$triple" && \\
    url="https://github.com/wasm-bindgen/wasm-bindgen/releases/download/{_WASM_BINDGEN_VERSION}/$name.tar.gz" && \\
    curl -fsSL --retry 5 -o "/tmp/$name.tar.gz" "$url" && \\
    echo "$(curl -fsSL --retry 5 "$url.sha256sum" | cut -d ' ' -f 1)  /tmp/$name.tar.gz" | sha256sum -c - && \\
    tar -xzf "/tmp/$name.tar.gz" -C /tmp && \\
    install -m 0755 "/tmp/$name/wasm-bindgen" /usr/local/cargo/bin/wasm-bindgen && \\
    rm -rf "/tmp/$name" "/tmp/$name.tar.gz" && \\
    wasm-bindgen --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Cua3958To5875ImageDefault(Image):
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
        return Cua3958To5875ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        suites = _suites(self.pr.test_patch)
        test_commands = _test_commands(suites)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{_RUN_ENV}

{_PREPARE_HELPERS}

{_provision(suites)}

{_gate(suites)}
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_RUN_ENV}

{_RUN_HELPERS}

cd /home/{repo}
{test_commands}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_RUN_ENV}

{_RUN_HELPERS}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_commands}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_RUN_ENV}

{_RUN_HELPERS}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_commands}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN --mount=type=cache,id=cua-cargo-crates,target=/var/cache/cua-cargo,sharing=locked bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("trycua", _INTERVAL)
class CUA_3958_TO_5875(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Cua3958To5875ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        begin = re.compile(rf"^{re.escape(_SUITE_BEGIN)} (\S+)\s*$")
        rust_result = re.compile(r"^test (\S.*?) \.\.\. (ok|FAILED|ignored)\b")

        suite = ""
        junit: Optional[list[str]] = None
        for raw in log.splitlines():
            line = raw.rstrip("\r")
            if junit is not None:
                if line.strip() == _JUNIT_END:
                    for name, status in _junit_cases("\n".join(junit)):
                        test = f"{suite}::{name}"
                        if status == "passed":
                            passed_tests.add(test)
                        elif status == "failed":
                            failed_tests.add(test)
                        else:
                            skipped_tests.add(test)
                    junit = None
                else:
                    junit.append(line)
                continue
            stripped = line.strip()
            found = begin.match(stripped)
            if found:
                suite = found.group(1)
                continue
            if stripped == _SUITE_END:
                suite = ""
                continue
            if stripped == _JUNIT_BEGIN and suite:
                junit = []
                continue
            if not suite.startswith("rust:"):
                continue
            found = rust_result.match(line.rstrip())
            if not found:
                continue
            test = f"{suite}::{found.group(1)}"
            status = found.group(2)
            if status == "ok":
                passed_tests.add(test)
            elif status == "FAILED":
                failed_tests.add(test)
            else:
                skipped_tests.add(test)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
