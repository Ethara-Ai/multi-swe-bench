import json
import posixpath
import re
import shlex
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "photoprism"
_REPO = "photoprism"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"photoprism_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like photoprism_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"
_NUMBER_INTERVAL = "5563-5656-5716-5783"

_GO_IMAGE = "golang:1.26.6-bookworm"
_TF_VERSION = "2.18.0"
_TESTDATA_SHA1 = "be394d5bee8a5634d415e9e0663eef20b5604510"
_MODULE = "github.com/photoprism/photoprism"

_GO_TEST_FLAGS = "-json -count=1 -p 1 -parallel 1 -cpu 1 -tags=slow,develop -timeout 20m"
_GO_TAGS = "slow,develop"

_ENV_EXPORTS = (
    "export CI=true\n"
    "export GOTOOLCHAIN=local\n"
    "export CGO_ENABLED=1\n"
    "export TF_CPP_MIN_LOG_LEVEL=1\n"
)

_TEST_CMD = "bash /home/run_tests.sh\n"

_LOCKFILE_EXCLUDES = ("frontend/package-lock.json", "package-lock.json")

_CHECK_GIT_CHANGES = """#!/bin/bash
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
"""

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_INDEX_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", re.MULTILINE)


def _patch_blocks(patch: str) -> list[tuple[str, str]]:
    blocks = []
    starts = [m for m in _DIFF_HEADER_RE.finditer(patch)]
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(patch)
        blocks.append((m.group(2), patch[m.start():end]))
    return blocks


def _patch_files(patch: str) -> list[str]:
    return [path for path, _ in _patch_blocks(patch)]


def _test_packages(pr: PullRequest) -> list[str]:
    pkgs = set()
    for path in _patch_files(pr.test_patch):
        if not path.endswith(".go"):
            continue
        parts = path.split("/")
        if "testdata" in parts or parts[0] in ("frontend", "vendor", "node_modules"):
            continue
        pkgs.add(posixpath.dirname(path) or ".")
    return sorted(pkgs)


def _payloadless_binaries(patch: str) -> list[tuple[str, str]]:
    out = []
    for path, block in _patch_blocks(patch):
        if not re.search(r"^Binary files .* differ$", block, re.MULTILINE):
            continue
        m = _INDEX_RE.search(block)
        post = m.group(2) if m else ""
        out.append((path, post))
    return out


def _is_deleted(post: str) -> bool:
    return not post or set(post) == {"0"}


def _touches_go_mod(patch: str) -> bool:
    return any(p in ("go.mod", "go.sum") for p in _patch_files(patch))


_HUNK_HEADER_RE = re.compile(r"^@@ [^@]* @@ ?(.*)$")
_TEST_FUNC_RE = re.compile(r"^func ((?:Test|Fuzz|Example)\w*)\(")


def _func_name(text: str) -> str | None:
    m = _TEST_FUNC_RE.match(text)
    if m and m.group(1) != "TestMain":
        return m.group(1)
    return None


def _target_tests(patch: str) -> dict[str, list[tuple[str, str]]]:
    found: dict[tuple[str, str], None] = {}
    for path, block in _patch_blocks(patch):
        if not path.endswith("_test.go"):
            continue
        current: str | None = None
        in_hunk = False
        for line in block.split("\n"):
            hunk = _HUNK_HEADER_RE.match(line)
            if hunk:
                in_hunk = True
                current = _func_name(hunk.group(1))
                continue
            if not in_hunk or not line or line[0] not in " +-":
                continue
            text = line[1:]
            if text.startswith("func "):
                current = _func_name(text)
            if line[0] != " " and current:
                found[(path, current)] = None
            if text.rstrip() == "}":
                current = None
    targets: dict[str, list[tuple[str, str]]] = {}
    for path, name in found:
        targets.setdefault(posixpath.dirname(path) or ".", []).append((path, name))
    return targets


class PhotoprismEra5783To5563ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

ENV GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    CGO_CFLAGS="-g -O2 -Wno-return-local-addr" \\
    TF_CPP_MIN_LOG_LEVEL=1 \\
    CI=true

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential pkg-config curl wget unzip make \\
    libvips-dev libimage-exiftool-perl ffmpeg imagemagick libheif-examples librsvg2-bin \\
    rawtherapee darktable \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    tf_arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "${{tf_arch}}" in amd64|arm64) ;; *) echo "unsupported architecture: ${{tf_arch}}" >&2; exit 1 ;; esac; \\
    curl -fsSL --retry 3 -o /tmp/libtensorflow.tar.gz "https://dl.photoprism.app/tensorflow/${{tf_arch}}/libtensorflow-${{tf_arch}}-{_TF_VERSION}.tar.gz"; \\
    tar --overwrite --mode=755 -C /usr -xzf /tmp/libtensorflow.tar.gz; \\
    rm -f /tmp/libtensorflow.tar.gz; \\
    ldconfig; \\
    test -f /usr/include/tensorflow/c/c_api.h; \\
    test -e /usr/lib/libtensorflow.so; \\
    test -e /usr/lib/libtensorflow_framework.so.2; \\
    pkg-config --exists vips

RUN set -eux; \\
    mkdir -p /tmp/photoprism; \\
    for f in tensorflow/nasnet.zip tensorflow/facenet.zip tensorflow/nsfw.zip qa/testdata.zip; do \\
        curl -fsSL --retry 3 -o "/tmp/photoprism/$(basename "$f")" "https://dl.photoprism.app/$f"; \\
    done; \\
    echo "{_TESTDATA_SHA1}  /tmp/photoprism/testdata.zip" | sha1sum -c -

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class PhotoprismEra5783To5563ImageDefault(Image):
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
        return PhotoprismEra5783To5563ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_binaries(self) -> list[tuple[str, str]]:
        return _payloadless_binaries(self.pr.test_patch)

    def _fix_binaries(self) -> list[tuple[str, str]]:
        return _payloadless_binaries(self.pr.fix_patch)

    def _apply_excludes(self) -> str:
        paths = list(_LOCKFILE_EXCLUDES)
        for path, _ in self._test_binaries() + self._fix_binaries():
            if path not in paths:
                paths.append(path)
        return " ".join(f"--exclude={shlex.quote(p)}" for p in paths)

    def _extract_binaries_sh(self) -> str:
        lines = []
        for stage, items in (("test", self._test_binaries()), ("fix", self._fix_binaries())):
            for path, post in items:
                if _is_deleted(post):
                    continue
                dest = f"/home/mswe_bin_{stage}/{path}"
                lines.append(f"mkdir -p {shlex.quote(posixpath.dirname(dest))}")
                lines.append(f'mswe_blob="$(git rev-parse --verify --quiet "{post}^{{blob}}")"')
                lines.append(f'git cat-file blob "$mswe_blob" > {shlex.quote(dest)}')
                lines.append(f'test "$(git hash-object {shlex.quote(dest)})" = "$mswe_blob"')
        return "\n".join(lines) + ("\n" if lines else "")

    def _restore_binaries_sh(self, stages: tuple[str, ...]) -> str:
        lines = []
        for stage in stages:
            items = self._test_binaries() if stage == "test" else self._fix_binaries()
            if not items:
                continue
            for path, post in items:
                if _is_deleted(post):
                    lines.append(f"rm -f {shlex.quote(path)}")
            if any(not _is_deleted(post) for _, post in items):
                lines.append(f"cp -R /home/mswe_bin_{stage}/. /home/{self.pr.repo}/")
        return "\n".join(lines) + ("\n" if lines else "")

    def _warm_fix_modules_sh(self) -> str:
        patches = [p for p, text in (("/home/test.patch", self.pr.test_patch), ("/home/fix.patch", self.pr.fix_patch))
                   if _touches_go_mod(text)]
        if not patches:
            return ""
        return (
            'mswe_mod_dir="$(mktemp -d)"\n'
            'cp go.mod go.sum "$mswe_mod_dir"/\n'
            f'(cd "$mswe_mod_dir" && git apply --whitespace=nowarn --include=go.mod --include=go.sum {" ".join(patches)} '
            "&& timeout 1800 go mod download) || true\n"
            'rm -rf "$mswe_mod_dir"\n'
        )

    def _gate_sh(self) -> str:
        pkgs = " ".join(_test_packages(self.pr))
        return f"""mswe_gate_pkgs=()
mkdir -p /tmp/mswe_gate
for pkg in {pkgs}; do
  if compgen -G "$pkg/*.go" > /dev/null; then
    mswe_gate_pkgs+=("./$pkg")
    go test -c -tags={_GO_TAGS} -o "/tmp/mswe_gate/$(echo "$pkg" | tr '/.' '__').test" "./$pkg"
  fi
done
rm -rf /tmp/mswe_gate
"""

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""#!/bin/bash
set -e

{_ENV_EXPORTS}
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

{self._extract_binaries_sh()}
go version
go env GOVERSION GOTOOLCHAIN CGO_ENABLED
timeout 1800 go mod download || true
{self._warm_fix_modules_sh()}timeout 1800 make dep-tensorflow || true
git diff --quiet -- go.mod go.sum || git checkout -- go.mod go.sum

cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

{self._gate_sh()}test -s /home/run_tests.sh
test -f /tmp/photoprism/testdata.zip
go list -deps -test -tags={_GO_TAGS} "${{mswe_gate_pkgs[@]}}" > /dev/null && echo DEPS_OK
"""

    def _run_tests_sh(self) -> str:
        repo = self.pr.repo
        pkgs = " ".join(_test_packages(self.pr))
        return f"""#!/bin/bash
set -uo pipefail

cd /home/{repo}

present=()
for pkg in {pkgs}; do
  if ! compgen -G "$pkg/*.go" > /dev/null; then
    echo "MSWE_SKIP_PKG $pkg (package not found)"
    continue
  fi
  echo "MSWE_PKG $pkg"
  mkdir -p "$pkg/../../storage"
  present+=("$pkg")
  for f in "$pkg"/*_test.go; do
    [ -f "$f" ] || continue
    sed -nE 's/^func[[:space:]]+((Test|Example|Fuzz)[[:alnum:]_]*)[[:space:]]*\\(.*/\\1/p' "$f" | while read -r name; do
      echo "MSWE_TESTFILE $pkg $name $f"
    done
  done
done

if [ "${{#present[@]}}" -eq 0 ]; then
  echo "MSWE_NO_PACKAGES"
  exit 1
fi

args=()
for pkg in "${{present[@]}}"; do
  args+=("./$pkg")
done

go test {_GO_TEST_FLAGS} "${{args[@]}}" 2>&1
rc=$?
echo "MSWE_GO_TEST_EXIT $rc"
exit $rc
"""

    def _run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
{_TEST_CMD}"""

    def _test_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply --whitespace=nowarn {self._apply_excludes()} /home/test.patch
{self._restore_binaries_sh(("test",))}{_TEST_CMD}"""

    def _fix_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
git apply --whitespace=nowarn {self._apply_excludes()} /home/test.patch /home/fix.patch
{self._restore_binaries_sh(("test", "fix"))}{_TEST_CMD}"""

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_TESTFILE_RE = re.compile(r"^MSWE_TESTFILE (\S+) (\S+) (\S+)$")
_PLAIN_PKG_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+\[(?:build failed|setup failed)\]")


def _pkg_dir(import_path: str) -> str:
    path = import_path.split(" ", 1)[0]
    if path == _MODULE:
        return "."
    if path.startswith(_MODULE + "/"):
        return path[len(_MODULE) + 1:]
    return path


@Instance.register(_ORG, _NUMBER_INTERVAL)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class PHOTOPRISM_5783_TO_5563(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config
        self._targets = _target_tests(pr.test_patch)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PhotoprismEra5783To5563ImageDefault(self.pr, self._config)

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

        clean = _ANSI_RE.sub("", test_log).replace("\r", "")

        test_files: dict[tuple[str, str], str] = {}
        pkg_failed: set[str] = set()
        pkg_test_failed: set[str] = set()
        pkg_tests_seen: set[str] = set()

        for raw in clean.split("\n"):
            line = raw.strip()
            if not line:
                continue

            m = _TESTFILE_RE.match(line)
            if m:
                test_files[(m.group(1), m.group(2))] = m.group(3)
                continue

            if line.startswith("{"):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                action = event.get("Action")
                pkg_path = event.get("Package") or event.get("ImportPath") or ""
                if not pkg_path:
                    continue
                pkg = _pkg_dir(pkg_path)
                test = event.get("Test")
                if test:
                    pkg_tests_seen.add(pkg)
                    if action not in ("pass", "fail", "skip"):
                        continue
                    top = test.split("/", 1)[0]
                    prefix = test_files.get((pkg, top), pkg)
                    name = f"{prefix} > {test}"
                    if action == "pass":
                        passed_tests.add(name)
                    elif action == "fail":
                        failed_tests.add(name)
                        pkg_test_failed.add(pkg)
                    else:
                        skipped_tests.add(name)
                elif action in ("fail", "build-fail"):
                    pkg_failed.add(pkg)
                continue

            m = _PLAIN_PKG_FAIL_RE.match(line)
            if m:
                pkg_failed.add(_pkg_dir(m.group(1)))

        for pkg in pkg_failed - pkg_test_failed:
            failed_tests.add(f"{pkg} > (package)")
            if pkg not in pkg_tests_seen:
                for path, func in self._targets.get(pkg, []):
                    failed_tests.add(f"{path} > {func}")

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
