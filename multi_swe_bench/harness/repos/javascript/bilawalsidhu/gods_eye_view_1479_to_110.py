import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.22.3-bookworm"
_BASE_TAG = "base-gods_eye_view_1479_to_110"
_TEST_TIMEOUT_MS = "300000"
_CA = "/etc/ssl/certs/ca-certificates.crt"
_CERT_FARM = "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem" \
    " /etc/ssl/certs && \\\n    " + " && \\\n    ".join(
        f"ln -sf {_CA} {t}" for t in (
            "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem",
            "/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cacert.pem",
            "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
            "/etc/ssl/certs/ca-bundle.crt"))
_ALLOC = ("src/data/focusAllocations.test.mjs", "src/overlays/worldOverlayAllocation.test.mjs")

_SECTION_RE = re.compile(r"^(?=diff --git )", re.MULTILINE)
_HEAD_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_BINARY_RE = re.compile(r"^Binary files .* differ$", re.MULTILINE)
_INDEX_RE = re.compile(r"^index [0-9a-f]*\.\.([0-9a-f]+)", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_CASE_RE = re.compile(r"^<testcase\s+(.*?)/?>$")
_ATTR_RE = re.compile(r'([A-Za-z_][\w.-]*)="([^"]*)"')
_ENTITY_RE = re.compile(r"&(#\d+|quot|apos|lt|gt|amp);")
_NAMED = {"quot": '"', "apos": "'", "lt": "<", "gt": ">", "amp": "&"}

def _unescape(text: str) -> str:
    def char(hit: re.Match) -> str:
        token = hit.group(1)
        if token in _NAMED:
            return _NAMED[token]
        glyph = chr(int(token[1:]))
        return glyph if glyph.isprintable() else " "

    return _ENTITY_RE.sub(char, text)

def _binaries(patch: str) -> list[tuple[str, str]]:
    found = []
    for section in _SECTION_RE.split(patch):
        head = _HEAD_RE.match(section)
        if head and _BINARY_RE.search(section):
            index = _INDEX_RE.search(section)
            if index and index.group(1).strip("0"):
                found.append((index.group(1), head.group(2)))
    return found

def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template

_DETECT_SH = r"""ROOTS=src
if [ -d server ]; then ROOTS="src server"; fi
FLAGS=""
if [ -f build/registerVue.js ]; then FLAGS="--import=./build/registerVue.js"; fi
"""

_SUM_PASSED = (
    """awk 'match($0,/<!-- pass [0-9]+ -->/)"""
    """{n=substr($0,RSTART,RLENGTH);gsub(/[^0-9]/,"",n);s+=n} END{print s+0}'"""
)

_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__

git reset --hard
git clean -fdx
test -z "$(git status --porcelain)"
git checkout --detach __SHA__
test -z "$(git status --porcelain)"

for entry in __BLOBS__; do
    sha="${entry%%:*}"
    git cat-file blob "$(git rev-parse --verify --quiet "${sha}^{blob}")" > "/home/$sha.bin"
    echo "prepare: lifted binary ${entry#*:} from blob $sha"
done

npm ci

__DETECT__
gate="$(find $ROOTS -name '*.test.mjs' | sort | sed -n 1p)"
node $FLAGS --test --test-timeout=__TIMEOUT__ --test-reporter=junit "$gate" > /home/gate.xml 2>&1 || true
test "$(__SUM_PASSED__ /home/gate.xml)" -gt 0
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdq
__PATCH_STEP__
__DETECT__
: > /home/results.xml
rc=0
find $ROOTS -name '*.test.mjs' | sed 's|^\./||' | sort | grep -vxF __ALLOC_ARGS__ \
    | xargs -n 50 node $FLAGS --test --test-timeout=__TIMEOUT__ --test-reporter=junit >> /home/results.xml 2>&1 || rc=$?
echo "node --test exit $rc"
for spec in __ALLOC__; do
    if [ -f "$spec" ]; then
        rc=0
        node $FLAGS --expose-gc --test --test-timeout=__TIMEOUT__ --test-reporter=junit --test-concurrency=1 "$spec" \
            >> /home/results.xml 2>&1 || rc=$?
        echo "node --test exit $rc ($spec)"
    fi
done
for spec in $(awk '/<testcase /&&/ failure="/{if(match($0,/ file="[^"]*"/))print substr($0,RSTART+7,RLENGTH-8)}' /home/results.xml | sort -u); do
    if [ -f "$spec" ]; then
        rc=0
        node $FLAGS --test --test-timeout=__TIMEOUT__ --test-reporter=junit --test-concurrency=1 "$spec" >> /home/results.xml 2>&1 || rc=$?
        echo "node --test exit $rc (retry $spec)"
    fi
done
python3 -c 'import sys; sys.stdout.buffer.write(open(sys.argv[1],"rb").read().decode("utf-8","replace").encode("ascii","xmlcharrefreplace"))' /home/results.xml
cases="$(awk '/<testcase /{n++} END{print n+0}' /home/results.xml)"
passed="$(__SUM_PASSED__ /home/results.xml)"
echo "MSWB_TESTCASES $cases"
echo "MSWB_PASSED $passed"
test "$cases" -gt 0
test "$passed" -gt 0
"""

class _GodsEyeViewImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

class GodsEyeView1479To110ImageBase(_GodsEyeViewImage):
    def dependency(self) -> str | Image:
        return _NODE_IMAGE
    def image_tag(self) -> str:
        return _BASE_TAG
    def workdir(self) -> str:
        return _BASE_TAG
    def files(self) -> list[File]:
        return []
    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    CI=true \\
    PUPPETEER_SKIP_DOWNLOAD=1 \\
    GEV_REQUIRE_ALLOCATION_GATE=1 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_FETCH_RETRIES=5 \\
    NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_CERT_FARM}

{self.global_env}

RUN git config --global user.email "mswb@example.invalid" && \\
    git config --global user.name "mswb"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""

class GodsEyeView1479To110ImageDefault(_GodsEyeViewImage):
    def dependency(self) -> Image:
        return GodsEyeView1479To110ImageBase(self.pr, self._config)
    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"
    def workdir(self) -> str:
        return f"pr-{self.pr.number}"
    def _common_values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__TIMEOUT__": _TEST_TIMEOUT_MS,
            "__DETECT__": _DETECT_SH,
            "__SUM_PASSED__": _SUM_PASSED,
            "__ALLOC_ARGS__": " ".join(f"-e {s}" for s in _ALLOC),
            "__ALLOC__": " ".join(_ALLOC),
        }

    def _prepare(self) -> str:
        values = self._common_values()
        values["__SHA__"] = self.pr.base.sha
        values["__BLOBS__"] = " ".join(
            shlex.quote(f"{sha}:{path}")
            for patch in (self.pr.test_patch, self.pr.fix_patch)
            for sha, path in _binaries(patch)
        )
        return _render(_PREPARE_SH, values)
    def _act(self, name: str, *patches: tuple[str, str]) -> File:
        values = self._common_values()
        steps, restores = [], []
        for patch_name, patch in patches:
            binaries = _binaries(patch)
            excludes = "".join(f" --exclude={path}" for _, path in binaries)
            steps.append(f"git apply --whitespace=nowarn{excludes} /home/{patch_name}")
            restores += [f"cp /home/{sha}.bin {path}" for sha, path in binaries]
        if steps:
            steps += restores
        values["__PATCH_STEP__"] = "\n".join(steps)
        return File(".", name, _render(_RUN_SH, values))
    def files(self) -> list[File]:
        test = ("test.patch", self.pr.test_patch)
        fix = ("fix.patch", self.pr.fix_patch)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", self._prepare()),
            self._act("run.sh"),
            self._act("test-run.sh", test),
            self._act("fix-run.sh", test, fix),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}

ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""

@Instance.register("bilawalsidhu", "gods-eye-view")
class GODS_EYE_VIEW_1479_TO_110(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr
    def dependency(self) -> Image:
        return GodsEyeView1479To110ImageDefault(self.pr, self._config)
    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"
    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"
    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"
    def parse_log(self, test_log: str) -> TestResult:
        root = f"/home/{self.pr.repo}/"
        text = _ANSI_RE.sub("", test_log).replace("\r", "")
        seen: dict[str, str] = {}
        pending = None
        for line in text.splitlines():
            case = _CASE_RE.match(line.strip())
            if case:
                pending = None
                attrs = dict(_ATTR_RE.findall(case.group(1)))
                path, title = attrs.get("file", ""), _unescape(attrs.get("name", ""))
                if not path or not title:
                    continue
                index = path.find(root)
                rel = path[index + len(root):] if index >= 0 else path.lstrip("/")
                if title.endswith(".test.mjs"):
                    continue
                group = _unescape(attrs.get("classname", "test"))
                if group != "test":
                    title = f"{group.replace('.', '/')}/{title}"
                ident = f"{rel}::{title}"
                if "failure" in attrs:
                    seen[ident] = "FAILED"
                else:
                    seen[ident] = "PASSED"
                    pending = None if line.rstrip().endswith("/>") else ident
            elif pending and line.lstrip().startswith("<skipped"):
                seen[pending] = "SKIPPED"
                pending = None
        passed, failed, skipped = (
            {ident for ident, status in seen.items() if status == want}
            for want in ("PASSED", "FAILED", "SKIPPED")
        )
        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
