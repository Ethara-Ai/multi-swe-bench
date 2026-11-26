import json
import posixpath
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.15.0-bookworm"
_BASE_TAG = "base-14206_to_13201"

_TEST_FILE_RE = re.compile(
    r"^\+\+\+ b/((?:assets|packages|storybook|tests/js)/\S+?"
    r"(?:/__tests__/\S+|/test/[^/\s]+|/[^/\s]+\.test)\.[jt]sx?)$",
    re.MULTILINE,
)
_SECTION_RE = re.compile(r"^(?=diff --git )", re.MULTILINE)
_HEAD_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_DELETED_RE = re.compile(r"^deleted file mode ", re.MULTILINE)
_BINARY_RE = re.compile(r"^Binary files .* differ$", re.MULTILINE)
_INDEX_RE = re.compile(r"^index [0-9a-f]+\.\.([0-9a-f]+)", re.MULTILINE)


def _test_files(pr: PullRequest) -> list[str]:
    return sorted(set(_TEST_FILE_RE.findall(pr.test_patch)))


def _patch_entries(patch: str) -> list[tuple[str, str, str]]:
    entries = []
    for section in _SECTION_RE.split(patch):
        head = _HEAD_RE.match(section)
        if not head:
            continue
        if _DELETED_RE.search(section):
            entries.append(("DEL", head.group(1), ""))
        if _BINARY_RE.search(section):
            index = _INDEX_RE.search(section)
            entries.append(("BIN", head.group(2), index.group(1) if index else ""))
    return entries


def _blob_entries(pr: PullRequest) -> list[str]:
    blobs = []
    for patch in (pr.test_patch, pr.fix_patch):
        for kind, path, sha in _patch_entries(patch):
            if kind == "BIN" and sha.strip("0"):
                blobs.append(f"{sha}:{path}")
    return blobs


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
    path="${entry#*:}"
    blob="$(git rev-parse --verify --quiet "${sha}^{blob}")"
    mkdir -p "$(dirname "/home/binfiles/$path")"
    git cat-file blob "$blob" > "/home/binfiles/$path"
    echo "prepare: lifted binary $path from blob $blob"
done

npm ci -w assets -w storybook -w tests/js --include-workspace-root --prefer-offline
npm run test:js -- -- --listTests < /dev/null > /dev/null
"""

_APPLY_PATCH_SH = r"""
mswb_apply_patch() {
  local patch="$1" entry kind path refresh
  shift
  local excludes=()
  local lifted=()
  for entry in "$@"; do
      kind="${entry%%:*}"
      path="${entry#*:}"
      case "$kind" in
          BIN)
              excludes+=("--exclude=$path")
              lifted+=("$path")
              ;;
          DEL)
              if [ ! -e "$path" ]; then
                  excludes+=("--exclude=$path")
                  echo "apply_patch: $path is not in the tree, its deletion is skipped"
              fi
              ;;
      esac
  done
  if git apply --whitespace=nowarn "${excludes[@]}" "$patch" 2>/dev/null; then
      echo "apply_patch: $patch applied cleanly"
  else
      echo "apply_patch: $patch needs --3way"
      refresh=0
      git update-index -q --refresh > /dev/null || refresh=$?
      echo "apply_patch: index stat data refreshed (rc $refresh)"
      git apply --3way --whitespace=nowarn "${excludes[@]}" "$patch"
      echo "apply_patch: $patch applied via --3way"
  fi
  for path in "${lifted[@]}"; do
      if [ -f "/home/binfiles/$path" ]; then
          mkdir -p "$(dirname "$path")"
          cp "/home/binfiles/$path" "$path"
          echo "apply_patch: binary $path restored from /home/binfiles"
      else
          rm -f "$path"
          echo "apply_patch: binary $path removed"
      fi
  done
  if git grep -qE '^<<<<<<< ' -- ':!node_modules'; then
      echo "apply_patch: CONFLICT MARKERS present after apply" >&2
      exit 1
  fi
}
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__
__PATCH_STEP__
if ! git diff --quiet HEAD -- package-lock.json; then
    npm ci -w assets -w storybook -w tests/js --include-workspace-root --prefer-offline
fi
flags=""
for attempt in 1 2 3; do
    rc=0
    npm run test:js -- -- --ci --json --outputFile=/tmp/mswb-jest.json $flags \
        --runTestsByPath __TEST_FILES__ < /dev/null || rc=$?
    echo "jest attempt $attempt exit $rc"
    if [ -f /tmp/mswb-jest.json ]; then
        echo "MSWB_JSON_BEGIN"
        python3 -c 'import json; print(json.dumps(json.load(open("/tmp/mswb-jest.json", encoding="utf-8"))))'
        echo "MSWB_JSON_END"
    fi
    [ "$rc" -ne 0 ] || break
    flags="--onlyFailures"
done
"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class _SiteKitWpImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class SiteKitWp14206To13201ImageBase(_SiteKitWpImage):
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

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

RUN git config --global user.email "mswb@example.invalid" && \\
    git config --global user.name "mswb"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class SiteKitWp14206To13201ImageDefault(_SiteKitWpImage):
    def dependency(self) -> Image:
        return SiteKitWp14206To13201ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _common_values(self) -> dict[str, str]:
        root = f"/home/{self.pr.repo}"
        return {
            "__REPO__": self.pr.repo,
            "__TEST_FILES__": " ".join(f"{root}/{f}" for f in _test_files(self.pr)),
        }

    def _prepare(self) -> str:
        values = self._common_values()
        values["__SHA__"] = self.pr.base.sha
        values["__BLOBS__"] = " ".join(shlex.quote(b) for b in _blob_entries(self.pr))
        return _render(_PREPARE_SH, values)

    def _act(self, name: str, *patches: tuple[str, str]) -> File:
        values = self._common_values()
        steps = []
        for patch_name, patch in patches:
            args = [f"/home/{patch_name}"]
            args += [f"{kind}:{path}" for kind, path, _ in _patch_entries(patch)]
            steps.append("mswb_apply_patch " + " ".join(shlex.quote(a) for a in args))
        values["__PATCH_STEP__"] = _APPLY_PATCH_SH + "\n".join(steps) if steps else ""
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
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        base_commit_arg = f'ARG BASE_COMMIT="{self.pr.base.sha}"'
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{base_commit_arg}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_JSON_RE = re.compile(r"^MSWB_JSON_BEGIN\n(.*?)\nMSWB_JSON_END$", re.MULTILINE | re.DOTALL)
_SPACE_RE = re.compile(r"\s+")
_STATUS = {"passed": "PASSED", "failed": "FAILED"}


@Instance.register("google", "site_kit_wp_14206_to_13201")
class SITE_KIT_WP_14206_TO_13201(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SiteKitWp14206To13201ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        latest: dict[str, str] = {}
        root = f"/home/{self.pr.repo}"
        text = _ANSI_RE.sub("", test_log).replace("\r", "")
        for block in _JSON_RE.findall(text):
            try:
                report = json.loads(block)
            except ValueError:
                continue
            found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
            for result in report["testResults"]:
                rel = posixpath.relpath(result["name"], root)
                for t in result["assertionResults"]:
                    parts = [*t["ancestorTitles"], t["title"]]
                    title = " > ".join(_SPACE_RE.sub(" ", p).strip() for p in parts)
                    status = _STATUS.get(t["status"], "SKIPPED")
                    found[status].add(f"jest::{rel}::{title}")
            found["PASSED"] -= found["FAILED"]
            found["SKIPPED"] -= found["FAILED"]
            found["PASSED"] -= found["SKIPPED"]
            for status, names in found.items():
                for name in names:
                    latest[name] = status

        passed = {n for n, s in latest.items() if s == "PASSED"}
        failed = {n for n, s in latest.items() if s == "FAILED"}
        skipped = {n for n, s in latest.items() if s == "SKIPPED"}

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
