from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_UBUNTU_IMAGE = "ubuntu:22.04"
_BASE_TAG = "base-26751_to_25490"
_JDK_PACKAGE = "openjdk-21-jdk-headless"
_BAZELISK_VERSION = "v1.25.0"
_BAZELISK_SHA256_AMD64 = "fd8fdff418a1758887520fa42da7e6ae39aefc788cf5e7f7bb8db6934d279fc4"
_BAZELISK_SHA256_ARM64 = "4c8d966e40ac2c4efcc7f1a5a5cceef2c0a2f16b957e791fa7a867cce31e8fcb"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TESTS_BLOCK_RE = re.compile(r"^===TESTS BEGIN===$(.*?)^===TESTS END===$", re.MULTILINE | re.DOTALL)
_TESTS_LINE_RE = re.compile(r"^(PASSED|FAILED|SKIPPED)\t(\S.*)$")

_BAZEL_FLAGS = (
    "--build_tests_only --test_tag_filters=-manual --jobs=6 --local_test_jobs=2"
    " --test_output=errors --noshow_progress"
)

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""

_TEST_DIGEST = r"""python3 - <<'PYEOF'
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

java_class = re.compile(r'^[a-z][\w]*(\.[a-z][\w]*)*\.[A-Z]\w*(\$\w+)*$')


def test_id(label, classname, name):
    name = ' '.join(name.split())
    if label.startswith('//src/test/java/') and java_class.match(classname):
        top, _, inner = classname.partition('$')
        prefix = inner.replace('$', '.') + '.' if inner else ''
        return 'src/test/java/' + top.replace('.', '/') + '.java::' + prefix + name
    return label + '::' + (classname + '.' if classname else '') + name


summaries = {}
xmls = {}
with open('/tmp/bep.json') as fh:
    for line in fh:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        ident = ev.get('id') or {}
        ts = ident.get('testSummary')
        if ts and ts.get('label'):
            summaries[ts['label']] = (ev.get('testSummary') or {}).get('overallStatus', 'NO_STATUS')
        tr = ident.get('testResult')
        if tr and tr.get('label'):
            key = (tr['label'], tr.get('run', 1), tr.get('shard', 1))
            attempt = tr.get('attempt', 1)
            for out in (ev.get('testResult') or {}).get('testActionOutput') or []:
                if out.get('name') == 'test.xml' and out.get('uri', '').startswith('file://'):
                    if key not in xmls or xmls[key][0] <= attempt:
                        xmls[key] = (attempt, urllib.parse.unquote(out['uri'][len('file://'):]))

status = {}
failing_labels = set()
for (label, _, _), (_, path) in sorted(xmls.items()):
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        continue
    for case in root.iter('testcase'):
        tid = test_id(label, case.get('classname') or '', case.get('name') or '')
        if case.find('failure') is not None or case.find('error') is not None:
            result = 'FAILED'
            failing_labels.add(label)
        elif case.find('skipped') is not None:
            result = 'SKIPPED'
        else:
            result = 'PASSED'
        if status.get(tid) != 'FAILED':
            status[tid] = result

for label, overall in summaries.items():
    if overall in ('FAILED', 'TIMEOUT', 'INCOMPLETE', 'REMOTE_FAILURE') and label not in failing_labels:
        status[label] = 'FAILED'

print('===TESTS BEGIN===')
for tid in sorted(status):
    print(status[tid] + '\t' + tid)
print('===TESTS END===')
PYEOF"""


def _test_targets(test_patch: str) -> str:
    targets: set[str] = set()
    for path in re.findall(r"^diff --git a/\S+ b/(\S+)$", test_patch, re.MULTILINE):
        directory, _, name = path.rpartition("/")
        stem, _, ext = name.rpartition(".")
        if path.startswith("src/test/java/") and ext == "java":
            targets.add(f"//{directory}/...")
        elif path.startswith("src/test/py/bazel/") and ext == "py" and stem.endswith("_test"):
            targets.add(f"//src/test/py/bazel:{stem}")
        elif path.startswith("src/test/shell/") and ext == "sh" and stem.endswith("_test"):
            targets.add(f"//{directory}:{stem}")
    if not targets:
        raise ValueError("bazel_26751_to_25490: test patch touches no runnable test target")
    return " ".join(sorted(targets))


class Bazel26751To25490ImageBase(Image):
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
        return _UBUNTU_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV LC_ALL=C.UTF-8 \\
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk \\
    BAZELISK_HOME=/opt/bazelisk

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential python3 zip unzip file {_JDK_PACKAGE} \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) sum={_BAZELISK_SHA256_AMD64} ;; \\
        arm64) sum={_BAZELISK_SHA256_ARM64} ;; \\
        *) echo "unsupported architecture: $arch"; exit 1 ;; \\
    esac; \\
    ln -sfn "/usr/lib/jvm/java-21-openjdk-$arch" "$JAVA_HOME"; \\
    "$JAVA_HOME/bin/java" -version; \\
    curl -fsSL -o /usr/local/bin/bazel \\
        "https://github.com/bazelbuild/bazelisk/releases/download/{_BAZELISK_VERSION}/bazelisk-linux-$arch"; \\
    echo "$sum  /usr/local/bin/bazel" | sha256sum -c -; \\
    chmod +x /usr/local/bin/bazel

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Bazel26751To25490ImageDefault(Image):
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
        return Bazel26751To25490ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        targets = _test_targets(self.pr.test_patch)
        test_cmds = f"""rm -f /tmp/bep.json
rc=0
bazel test {_BAZEL_FLAGS} --keep_going --build_event_json_file=/tmp/bep.json {targets} 2>&1 || rc=$?
echo "bazel exit code: $rc"
{_TEST_DIGEST}
case "$rc" in
  0|1|3|4) ;;
  *) echo "bazel test exited with status $rc" >&2; exit "$rc" ;;
esac"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

export CI=true

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

bazel_version="$(cat .bazelversion)"
bazel --version | grep -qx "bazel $bazel_version" || {{ echo "prepare.sh: bazelisk does not run bazel $bazel_version from .bazelversion"; exit 1; }}

bazel build {_BAZEL_FLAGS} {targets}

bazel query "tests({' + '.join(targets.split())})" --noshow_progress > /tmp/test_targets.txt
grep -q '^//' /tmp/test_targets.txt || {{ echo "prepare.sh: no test targets match {targets}"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
{test_cmds}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{test_cmds}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{test_cmds}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Bazel26751To25490ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("bazelbuild", "bazel_26751_to_25490")
class BAZEL_26751_TO_25490(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Bazel26751To25490ImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        block = _TESTS_BLOCK_RE.search(_ANSI_RE.sub("", test_log))
        lines = block.group(1).splitlines() if block else []
        for raw in lines:
            m = _TESTS_LINE_RE.match(raw.rstrip("\r"))
            if not m:
                continue
            status, test_id = m.group(1), m.group(2)
            if status == "PASSED":
                passed.add(test_id)
            elif status == "FAILED":
                failed.add(test_id)
            else:
                skipped.add(test_id)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
