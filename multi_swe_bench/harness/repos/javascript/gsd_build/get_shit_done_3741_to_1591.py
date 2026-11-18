import json
import posixpath
import re
import xml.etree.ElementTree as ET

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22.23.5-bookworm"
_BASE_TAG = "base-3741_to_1591"

_ROOT_TEST_RE = re.compile(r"^\+\+\+ b/(tests/[^/\s]+\.test\.cjs)$", re.MULTILINE)
_SDK_TEST_RE = re.compile(r"^\+\+\+ b/sdk/(src/\S+\.test\.ts)$", re.MULTILINE)


def _root_tests(pr: PullRequest) -> list[str]:
    return sorted(set(_ROOT_TEST_RE.findall(pr.test_patch)))


def _sdk_tests(pr: PullRequest) -> list[str]:
    return sorted(set(_SDK_TEST_RE.findall(pr.test_patch)))


_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__

git reset --hard
git clean -fdx
test -z "$(git status --porcelain)"
git checkout --detach __SHA__
test -z "$(git status --porcelain)"

npm ci --prefer-offline
cd sdk
npm ci --prefer-offline
npm run build
GOGC=off node_modules/.bin/vitest list < /dev/null > /dev/null
"""

_APPLY_PATCH_SH = r"""
mswb_apply_patch() {
  local patch="$1" path refresh
  local skips=()
  if git apply --whitespace=nowarn "$patch" 2>/dev/null; then
      echo "apply_patch: $patch applied cleanly"
      return 0
  fi
  while IFS=$'\t' read -r _ _ path; do
      if git apply -R --check --include="$path" "$patch" 2>/dev/null; then
          skips+=("--exclude=$path")
          echo "apply_patch: $path already holds the patched content, skipped"
      fi
  done < <(git apply --numstat "$patch")
  if git apply --whitespace=nowarn "${skips[@]}" "$patch" 2>/dev/null; then
      echo "apply_patch: $patch applied without the skipped paths"
  else
      echo "apply_patch: $patch needs --3way"
      refresh=0
      git update-index -q --refresh > /dev/null || refresh=$?
      echo "apply_patch: index stat data refreshed (rc $refresh)"
      git apply --3way --whitespace=nowarn "${skips[@]}" "$patch"
      echo "apply_patch: $patch applied via --3way"
  fi
}
"""

_NODE_STEP = r"""
for f in __ROOT_TESTS__; do
    if [ ! -f "$f" ]; then
        continue
    fi
    rm -f /tmp/mswb-node.xml
    rc=0
    node --test --test-reporter=junit --test-reporter-destination=/tmp/mswb-node.xml "$f" < /dev/null || rc=$?
    echo "node --test $f exit $rc"
    if [ -f /tmp/mswb-node.xml ]; then
        echo "MSWB_NODE_BEGIN $f"
        python3 -c 'import json; print(json.dumps(open("/tmp/mswb-node.xml", encoding="utf-8").read()))'
        echo "MSWB_NODE_END"
    fi
done
"""

_SDK_STEP = r"""
cd /home/__REPO__/sdk
rc=0
node_modules/.bin/vitest run --pool=forks --no-file-parallelism --retry 2 \
    --reporter=json --outputFile=/tmp/mswb-vitest.json __SDK_TESTS__ < /dev/null || rc=$?
echo "vitest exit $rc"
if [ -f /tmp/mswb-vitest.json ]; then
    echo "MSWB_JSON_BEGIN"
    python3 -c 'import json; print(json.dumps(json.load(open("/tmp/mswb-vitest.json", encoding="utf-8"))))'
    echo "MSWB_JSON_END"
fi
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__
__PATCH_STEP__
if ! git diff --quiet HEAD -- package.json package-lock.json; then
    npm ci --prefer-offline
fi
cd sdk
if ! git diff --quiet HEAD -- package.json package-lock.json; then
    npm ci --prefer-offline
fi
npm run build
cd /home/__REPO__
__TEST_STEP__"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class _GetShitDoneImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class GetShitDone3741To1591ImageBase(_GetShitDoneImage):
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


class GetShitDone3741To1591ImageDefault(_GetShitDoneImage):
    def dependency(self) -> Image:
        return GetShitDone3741To1591ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare(self) -> str:
        return _render(_PREPARE_SH, {"__REPO__": self.pr.repo, "__SHA__": self.pr.base.sha})

    def _test_step(self) -> str:
        steps = []
        root_tests = _root_tests(self.pr)
        sdk_tests = _sdk_tests(self.pr)
        if root_tests:
            steps.append(_NODE_STEP.replace("__ROOT_TESTS__", " ".join(root_tests)))
        if sdk_tests:
            steps.append(_SDK_STEP.replace("__SDK_TESTS__", " ".join(sdk_tests)))
        return "".join(steps)

    def _act(self, name: str, *patches: str) -> File:
        steps = [f"mswb_apply_patch /home/{patch}" for patch in patches]
        values = {
            "__PATCH_STEP__": _APPLY_PATCH_SH + "\n".join(steps) if steps else "",
            "__TEST_STEP__": self._test_step(),
            "__REPO__": self.pr.repo,
        }
        return File(".", name, _render(_RUN_SH, values))

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", self._prepare()),
            self._act("run.sh"),
            self._act("test-run.sh", "test.patch"),
            self._act("fix-run.sh", "test.patch", "fix.patch"),
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
_NODE_RE = re.compile(r"^MSWB_NODE_BEGIN (\S+)\n(.*?)\nMSWB_NODE_END$", re.MULTILINE | re.DOTALL)
_SPACE_RE = re.compile(r"\s+")
_STATUS = {"passed": "PASSED", "failed": "FAILED"}


def _title(parts: list[str]) -> str:
    return " > ".join(_SPACE_RE.sub(" ", p).strip() for p in parts)


def _junit_cases(node: ET.Element, parents: list[str]):
    for child in node:
        if child.tag == "testsuite":
            yield from _junit_cases(child, [*parents, child.get("name")])
        elif child.tag == "testcase":
            if child.find("failure") is not None:
                status = "FAILED"
            elif child.find("skipped") is not None:
                status = "SKIPPED"
            else:
                status = "PASSED"
            yield _title([*parents, child.get("name")]), status


@Instance.register("gsd-build", "get_shit_done_3741_to_1591")
class GET_SHIT_DONE_3741_TO_1591(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return GetShitDone3741To1591ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        root = f"/home/{self.pr.repo}"
        text = _ANSI_RE.sub("", test_log).replace("\r", "")
        for path, block in _NODE_RE.findall(text):
            try:
                tree = ET.fromstring(json.loads(block))
            except (ValueError, ET.ParseError):
                continue
            for title, status in _junit_cases(tree, []):
                found[status].add(f"node::{path}::{title}")
        for block in _JSON_RE.findall(text):
            try:
                report = json.loads(block)
            except ValueError:
                continue
            for result in report["testResults"]:
                rel = posixpath.relpath(result["name"], root)
                for t in result["assertionResults"]:
                    title = _title([*t["ancestorTitles"], t["title"]])
                    status = _STATUS.get(t["status"], "SKIPPED")
                    found[status].add(f"vitest::{rel}::{title}")

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
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
