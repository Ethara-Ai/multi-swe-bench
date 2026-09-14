import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_PY_IMAGE = "python:3.8-slim-bookworm"
_BASE_APT = "git ca-certificates build-essential libxml2-dev libxslt1-dev zlib1g-dev libffi-dev"
_BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
_END_MARKER = "===== END TEST DETAIL ====="
_DESELECT = "not smoke and not e2e"
_TEST_CMD = (
    "python -m pytest tests/ -v --tb=short --override-ini=addopts="
    " -p no:cacheprovider --continue-on-collection-errors"
    f' -k "{_DESELECT}" --junitxml=/home/results.xml'
)

_PREPARE_TEMPLATE = """set -e
STATUS_LOG=/home/__REPO__/prepare-status.log
: > "$STATUS_LOG"
cd /home/__REPO__
echo "PREPARE_HEAD=$(git rev-parse HEAD)" >> "$STATUS_LOG"
echo "PREPARE_TREE_DIRTY_LINES=$(git status --porcelain | wc -l)" >> "$STATUS_LOG"

cat > /home/constraints-hydra.txt <<'CON_EOF'
torch==1.8.1
pytorch-lightning==1.4.9
torchmetrics==0.5.1
transformers==4.11.3
tokenizers==0.10.2
datasets==1.12.1
hydra-core==1.1.1
omegaconf==2.1.1
fairscale==0.4.0
sparseml==0.9.0
jupyter==1.0.0
notebook==6.4.12
ipywidgets==7.6.5
jupyter-client==7.4.9
jupyter-core==4.12.0
nbconvert==6.5.4
lxml==5.3.0
nbclient==0.7.4
qtconsole==5.4.4
jupyter-console==6.6.3
tornado==6.2
onnx==1.10.1
scikit-image==0.19.3
rouge-score==0.0.4
sentencepiece==0.1.96
lightning-bolts==0.4.0
wandb==0.12.2
numpy==1.20.3
Pillow==8.4.0
matplotlib==3.5.3
packaging==20.9
protobuf==3.20.3
pyarrow==5.0.0
pytest==6.2.5
CON_EOF

cat > /home/extras-hydra.txt <<'EXT_EOF'
EXT_EOF

cat > /home/constraints-cli.txt <<'CON_EOF'
torch==1.12.1
torchvision==0.13.1
pytorch-lightning==1.7.7
torchmetrics==0.9.3
transformers==4.22.1
tokenizers==0.12.1
datasets==2.5.1
sentencepiece==0.1.97
Pillow==9.2.0
jsonargparse==4.14.1
numpy==1.23.3
protobuf==3.20.3
pyarrow==9.0.0
pytest==6.0.0
wandb==0.10.22
CON_EOF

cat > /home/extras-cli.txt <<'EXT_EOF'
jsonargparse[signatures]
torchvision
EXT_EOF

cat > /home/parse_junit.py <<'PJ_EOF'
import os
import xml.etree.ElementTree as ET

PATH = "/home/results.xml"

if os.path.exists(PATH):
    try:
        root = ET.parse(PATH).getroot()
    except ET.ParseError:
        root = None

    if root is not None:
        for tc in root.iter("testcase"):
            path = tc.get("file")
            if not path:
                classname = tc.get("classname") or ""
                path = classname.replace(".", "/") + ".py"
            name = tc.get("name") or ""
            name = name.replace("\\r", " ").replace("\\n", " ")

            status = "PASSED"
            for child in tc:
                if child.tag in ("failure", "error"):
                    status = "FAILED"
                    break
                if child.tag == "skipped":
                    status = "SKIPPED"
                    break

            print("TESTCASE " + path + "::" + name + " " + status)
PJ_EOF

cat > /home/run_tests.sh <<'RT_EOF'
set -eo pipefail
export CI=true
export TOKENIZERS_PARALLELISM=false
export HF_HUB_DISABLE_TELEMETRY=1
cd /home/__REPO__
rm -f /home/results.xml
set +e
__TEST_CMD__
RC=$?
set -e
echo "TEST_EXIT_CODE=$RC"
echo "__BEGIN_MARKER__"
python /home/parse_junit.py
echo "__END_MARKER__"
RT_EOF
chmod +x /home/run_tests.sh

if grep -qi "hydra-core" requirements.txt; then
  CONSTRAINTS=/home/constraints-hydra.txt
  EXTRAS=/home/extras-hydra.txt
else
  CONSTRAINTS=/home/constraints-cli.txt
  EXTRAS=/home/extras-cli.txt
fi
echo "PREPARE_CONSTRAINTS=$CONSTRAINTS" >> "$STATUS_LOG"

python -m pip install --no-cache-dir "pip<24.1" setuptools wheel

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

set +e
python -m pip install --no-cache-dir --retries 10 -c "$CONSTRAINTS" -r requirements.txt
PIP_REQ=$?
grep -vEi "^[[:space:]]*(codecov|check-manifest|twine|pre-commit|mypy|isort|yapf|flake8)([[:space:]]|[<>=!]|$)" requirements/test.txt > /home/test-requirements.txt
python -m pip install --no-cache-dir --retries 10 -c "$CONSTRAINTS" -r /home/test-requirements.txt
PIP_TEST=$?
python -m pip install --no-cache-dir --retries 10 -c "$CONSTRAINTS" -r "$EXTRAS"
PIP_EXTRA=$?
set -e

git reset --hard
git clean -fd

python -m pip install --no-cache-dir --no-deps -e .

echo "PREPARE_PIP_REQ=$PIP_REQ" >> "$STATUS_LOG"
echo "PREPARE_PIP_TEST=$PIP_TEST" >> "$STATUS_LOG"
echo "PREPARE_PIP_EXTRA=$PIP_EXTRA" >> "$STATUS_LOG"
test "$PIP_REQ" = "0"
test "$PIP_TEST" = "0"
test "$PIP_EXTRA" = "0"

if grep -qi qemu /proc/self/maps 2>/dev/null; then
  STRICT=0
else
  STRICT=1
fi
echo "PREPARE_STRICT=$STRICT" >> "$STATUS_LOG"
set +e
python -c "import torch, pytorch_lightning, transformers, datasets, lightning_transformers; print('imports ok')"
IMPORTS_EXIT=$?
python -c "import transformers.pipelines; print('pipelines ok')"
PIPELINES_EXIT=$?
python -c "import tests.conftest" 2>&1 | tail -n 30
set -e
echo "PREPARE_PIPELINES_EXIT=$PIPELINES_EXIT" >> "$STATUS_LOG"
set +e
python -m pytest tests/ --collect-only -q --override-ini=addopts= -p no:cacheprovider --continue-on-collection-errors > /home/collect.log 2>&1
COLLECT_EXIT=$?
set -e
tail -n 40 /home/collect.log
echo "PREPARE_COLLECT_EXIT=$COLLECT_EXIT" >> "$STATUS_LOG"
COLLECTED=$(grep -cE "::" /home/collect.log 2>/dev/null || true)
COLLECTED=${COLLECTED:-0}
echo "PREPARE_IMPORTS_EXIT=$IMPORTS_EXIT" >> "$STATUS_LOG"
echo "PREPARE_COLLECTED=$COLLECTED" >> "$STATUS_LOG"
if [ "$IMPORTS_EXIT" -ge 128 ] || [ "$COLLECT_EXIT" -ge 128 ]; then
  echo "PREPARE_SIGNAL_DEATH=1" >> "$STATUS_LOG"
else
  test "$IMPORTS_EXIT" = "0"
  test "$COLLECTED" -gt 0
fi
echo "DEPS_OK"
"""

_CHECK_GIT_CHANGES = """set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi
echo "check_git_changes: No uncommitted changes"
exit 0
"""

_RUN_TEMPLATE = """set -eo pipefail
cd /home/__REPO__
git reset --hard
git clean -qfd
__APPLY__bash /home/run_tests.sh
"""


def _render(template: str, repo: str) -> str:
    return (
        template.replace("__REPO__", repo)
        .replace("__TEST_CMD__", _TEST_CMD)
        .replace("__BEGIN_MARKER__", _BEGIN_MARKER)
        .replace("__END_MARKER__", _END_MARKER)
    )


def _run_script(repo: str, apply_cmd: str) -> str:
    return _render(_RUN_TEMPLATE, repo).replace("__APPLY__", apply_cmd)


class LT197To288ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return _PY_IMAGE

    def image_tag(self) -> str:
        return "base-197_to_288"

    def workdir(self) -> str:
        return "base-197_to_288"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image}

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DEFAULT_TIMEOUT=120 \\
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
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN set -eux; \\
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/pki/ca-trust/extracted/pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/cert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cacert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends {_BASE_APT} && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class LT197To288ImageDefault(Image):
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
        return LT197To288ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _render(_PREPARE_TEMPLATE, repo)),
            File(".", "run.sh", _run_script(repo, "")),
            File(
                ".",
                "test-run.sh",
                _run_script(repo, "git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_script(
                    repo,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).rstrip("\n")
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {name}:{tag}\n\n"
            f"WORKDIR /home/{repo}\n\n"
            "RUN git reset --hard\n\n"
            f"RUN git checkout {sha}\n\n"
            f"{hardening}\n\n"
            "WORKDIR /home/\n\n"
            f"{copy_commands}\n\n"
            "RUN bash /home/prepare.sh\n"
        )


@Instance.register("Lightning-Universe", "lightning_transformers_197_to_288")
class LIGHTNING_TRANSFORMERS_197_TO_288(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LT197To288ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        log_clean = re.sub(r"\x1B\[[0-?9;]*[mK]", "", log)
        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in log_clean.splitlines():
            stripped = line.strip()

            if stripped.startswith(_BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(_END_MARKER):
                in_detail = False
                continue
            if not in_detail:
                continue

            m = case_re.match(stripped)
            if not m:
                continue

            name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
