import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "khoj"

# Era: khoj v1.0+ (PR >= 531)
# Django application (`src/khoj/app`), pytest-django. Requires a running
# PostgreSQL server with the pgvector extension. Heavy ML deps
# (torch>=2.0.1, sentence-transformers, llama-cpp-python). The DB connection
# is configured via POSTGRES_* env vars consumed by khoj.app.settings.


class ImageBase(Image):
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
        return "python:3.11-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-v1x"

    def workdir(self) -> str:
        return "base-v1x"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git gnupg make sudo wget build-essential \\
    gcc g++ python3-dev libegl1 cmake \\
    sqlite3 libsqlite3-dev ffmpeg libsm6 libxext6 \\
    postgresql postgresql-contrib postgresql-server-dev-all libpq-dev \\
    && rm -rf /var/lib/apt/lists/*

# Build & install the pgvector extension into the system PostgreSQL
RUN git clone --depth 1 --branch v0.7.4 https://github.com/pgvector/pgvector.git /tmp/pgvector \\
    && cd /tmp/pgvector && make && make install && rm -rf /tmp/pgvector

# Trust local connections so the harness scripts can manage the DB
RUN sed -i 's/peer/trust/g; s/scram-sha-256/trust/g; s/md5/trust/g' /etc/postgresql/*/main/pg_hba.conf

{code}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        env_exports = "\n".join(
            [
                'export POSTGRES_HOST="localhost"',
                'export POSTGRES_PORT="5432"',
                'export POSTGRES_USER="postgres"',
                'export POSTGRES_PASSWORD="postgres"',
                'export POSTGRES_DB="khoj"',
                "export CI=true",
                # Use the model pre-cached at build time; disable the xet
                # transfer path whose token endpoint is what gets the host
                # IP rate-limited (429). Not fully offline so an
                # un-cached model can still fall back to the CDN.
                'export HF_HUB_DISABLE_XET="1"',
                'export TOKENIZERS_PARALLELISM="false"',
                'export TRANSFORMERS_OFFLINE="0"',
            ]
        )
        # Authoritative, idempotent DB bring-up at runtime (build-time setup is
        # best-effort). pgvector extension is already installed in the image.
        db_setup = (
            "service postgresql start || true\n"
            "for i in $(seq 1 30); do pg_isready -q && break; sleep 1; done\n"
            "su - postgres -c \"psql -c \\\"ALTER USER postgres WITH PASSWORD "
            "'postgres';\\\"\" || true\n"
            "su - postgres -c 'createdb khoj' 2>/dev/null || true\n"
            "su - postgres -c \"psql -d khoj -c 'CREATE EXTENSION IF NOT "
            "EXISTS vector;'\" || true"
        )
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
set -e
cd /home/{REPO_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
# DB setup is best-effort at build time (postgres may not start under
# emulated multi-arch buildx); run scripts set it up authoritatively at
# runtime. `|| true` everywhere so the image build never fails on this.
service postgresql start || true
su - postgres -c "psql -c \\"ALTER USER postgres WITH PASSWORD 'postgres';\\"" || true
su - postgres -c "createdb khoj" || true
su - postgres -c "psql -d khoj -c 'CREATE EXTENSION IF NOT EXISTS vector;'" || true
pip install --upgrade pip
# The `dev` extra pulls `pgserver` (via the `local` extra) which has no
# arm64 wheel, so `.[dev]` aborts on arm64. For some base commits even
# `pip install .` fails (heavy/optional deps fail to build under py3.11),
# which left the `khoj` distribution unregistered (tests use
# importlib.metadata.version("khoj") -> PackageNotFoundError) and the
# tests/conftest.py import chain broken. Always fall back to a --no-deps
# install so the metadata is registered, then add deps explicitly.
pip install ".[dev]" || pip install ".[test]" || pip install . \
    || pip install -e . --no-deps || pip install . --no-deps || true
# khoj pins pytest-django==4.5.2 (needs pytest<8). Install a compatible
# test toolchain + the prod deps imported by tests/conftest.py.
pip install "pytest<8" "pytest-django==4.5.2" factory-boy freezegun \
    "pytest-xdist[psutil]" pytest-asyncio trio gitpython || true
pip install stripe twilio boto3 gunicorn || true
# Runtime deps that `pip install .` failed to provide for older base
# commits; without these tests/conftest.py fails to import (fastapi /
# openai / langchain_community / django_apscheduler / magika / pyjson5 /
# google.genai ModuleNotFound / ImportError).
pip install fastapi "uvicorn[standard]" "openai>=1.40" \
    langchain langchain-community django-apscheduler django-unfold \
    magika pyjson5 resend tiktoken \
    google-genai google-generativeai || true
# The ML version trio must agree. khoj pins sentence-transformers==2.2.2,
# which imports `cached_download` (present only in huggingface_hub<0.26),
# but its unbounded `transformers>=4.28` resolves to >=4.45 which requires
# huggingface_hub>=0.34 -> irreconcilable. Pin transformers + tokenizers
# back to a release that works with huggingface_hub<0.26 and install the
# four together as one resolve so s-t 2.2.2 / transformers / tokenizers /
# hub stay mutually consistent (also pins s-t explicitly in case the
# `pip install . --no-deps` fallback above skipped it).
pip install "sentence-transformers==2.2.2" "transformers>=4.28,<4.40" \
    "tokenizers<0.19" "huggingface_hub<0.26" safetensors || true
# Pre-cache the default embedding model into the image so tests are a
# cache hit at runtime instead of calling huggingface.co live (live calls
# get the host IP rate-limited -> 429 "Too Many Requests" errors).
export HF_HUB_DISABLE_XET=1
python - <<'PYEOF' || true
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer("thenlper/gte-small")
    print("precached thenlper/gte-small")
except Exception as exc:
    print("model precache skipped:", exc)
PYEOF
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
{db_setup}
{env_exports}
# Several test modules hard-code `SKIP_TESTS = True` (the skip reason says
# "Disable in CI" but it is NOT env-gated), so their tests never run and a
# PR whose target fail->pass tests live there can never validate. Flip the
# constant in every tests/ file that sets it, at each stage so the
# run/test/fix comparison stays apples-to-apples.
grep -rl '^SKIP_TESTS = True' tests/ 2>/dev/null | xargs -r sed -i 's/^SKIP_TESTS = True/SKIP_TESTS = False/' || true
python -m pytest -v -rA -p no:cacheprovider --continue-on-collection-errors
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
{db_setup}
{env_exports}
# GitHub-format patches reference binary blobs with an abbreviated index
# line and no blob data, so `git apply` (even --reject) aborts the whole
# patch -> zero tests collected. Strip the binary file-diffs out, then
# apply the remaining text hunks with --reject so a context-mismatch in
# one hunk can't discard the rest.
for p in /home/test.patch; do
  python - "$p" "$p.nobin" "$p.bins" <<'PYEOF' || (cp "$p" "$p.nobin"; : > "$p.bins")
import sys
src, dst, binsf = sys.argv[1], sys.argv[2], sys.argv[3]
data = open(src, errors="replace").read().split("\n")
out = []
bins = []
block = []
path = None
is_bin = False
for ln in data:
    if ln.startswith("diff --git "):
        if block and is_bin and path:
            bins.append(path)
        elif block and not is_bin:
            out.extend(block)
        block = [ln]
        is_bin = False
        seg = ln.split(" b/")
        path = seg[1].strip() if len(seg) > 1 else None
    else:
        block.append(ln)
        if ln.startswith("GIT binary patch") or ln.startswith("Binary files "):
            is_bin = True
if block and is_bin and path:
    bins.append(path)
elif block and not is_bin:
    out.extend(block)
open(dst, "w").write("\n".join(out))
open(binsf, "w").write("\n".join(bins))
PYEOF
  git apply --whitespace=nowarn "$p.nobin" \
    || git apply --whitespace=nowarn --reject "$p.nobin" \
    || patch -p1 -f --no-backup-if-mismatch < "$p.nobin" \
    || true
done
# The stripped binary diffs are often real test fixtures (e.g. a .docx /
# .png the tests load). The patch only had `Binary files differ` w/o the
# blob, so restore the actual files from this PR's head on GitHub.
# Best-effort: needs network, and the path must exist at the PR head.
BINS=$(cat /home/test.patch.bins /home/fix.patch.bins 2>/dev/null | sort -u)
if [ -n "$BINS" ]; then
  git fetch -q --depth 1 origin pull/{self.pr.number}/head 2>/dev/null \
    && for bp in $BINS; do git checkout -q FETCH_HEAD -- "$bp" 2>/dev/null || true; done \
    || true
fi
# Unskip hard-coded `SKIP_TESTS = True` modules AFTER patching so tests the
# patch adds/edits there are unskipped too (not env-gated despite the
# "Disable in CI" reason text).
grep -rl '^SKIP_TESTS = True' tests/ 2>/dev/null | xargs -r sed -i 's/^SKIP_TESTS = True/SKIP_TESTS = False/' || true
python -m pytest -v -rA -p no:cacheprovider --continue-on-collection-errors
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
{db_setup}
{env_exports}
# Apply test then fix patch separately (not atomically). Strip binary
# file-diffs first (unappliable without the blob), then --reject so a
# context mismatch in one hunk can't discard the rest.
for p in /home/test.patch /home/fix.patch; do
  python - "$p" "$p.nobin" "$p.bins" <<'PYEOF' || (cp "$p" "$p.nobin"; : > "$p.bins")
import sys
src, dst, binsf = sys.argv[1], sys.argv[2], sys.argv[3]
data = open(src, errors="replace").read().split("\n")
out = []
bins = []
block = []
path = None
is_bin = False
for ln in data:
    if ln.startswith("diff --git "):
        if block and is_bin and path:
            bins.append(path)
        elif block and not is_bin:
            out.extend(block)
        block = [ln]
        is_bin = False
        seg = ln.split(" b/")
        path = seg[1].strip() if len(seg) > 1 else None
    else:
        block.append(ln)
        if ln.startswith("GIT binary patch") or ln.startswith("Binary files "):
            is_bin = True
if block and is_bin and path:
    bins.append(path)
elif block and not is_bin:
    out.extend(block)
open(dst, "w").write("\n".join(out))
open(binsf, "w").write("\n".join(bins))
PYEOF
  git apply --whitespace=nowarn "$p.nobin" \
    || git apply --whitespace=nowarn --reject "$p.nobin" \
    || patch -p1 -f --no-backup-if-mismatch < "$p.nobin" \
    || true
done
# The stripped binary diffs are often real test fixtures (e.g. a .docx /
# .png the tests load). The patch only had `Binary files differ` w/o the
# blob, so restore the actual files from this PR's head on GitHub.
# Best-effort: needs network, and the path must exist at the PR head.
BINS=$(cat /home/test.patch.bins /home/fix.patch.bins 2>/dev/null | sort -u)
if [ -n "$BINS" ]; then
  git fetch -q --depth 1 origin pull/{self.pr.number}/head 2>/dev/null \
    && for bp in $BINS; do git checkout -q FETCH_HEAD -- "$bp" 2>/dev/null || true; done \
    || true
fi
# Unskip hard-coded `SKIP_TESTS = True` modules AFTER patching so tests the
# patch adds/edits there are unskipped too (not env-gated despite the
# "Disable in CI" reason text).
grep -rl '^SKIP_TESTS = True' tests/ 2>/dev/null | xargs -r sed -i 's/^SKIP_TESTS = True/SKIP_TESTS = False/' || true
python -m pytest -v -rA -p no:cacheprovider --continue-on-collection-errors
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("khoj-ai", "khoj_531_to_99999")
class KHOJ_531_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # Strip ANSI escape codes first
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        verbose_re = re.compile(
            r"^(?P<name>\S+::\S+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        summary_re = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR)\s+(?P<name>\S+::\S+)"
        )

        for raw in clean_log.splitlines():
            line = raw.strip()
            m = verbose_re.match(line)
            if m:
                name, status = m.group("name"), m.group("status")
            else:
                m = summary_re.match(line)
                if not m:
                    continue
                name, status = m.group("name"), m.group("status")

            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR", "XFAIL"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
