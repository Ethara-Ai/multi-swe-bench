import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Era-2 (Forge) layout: project lives under autogpts/autogpt/, Poetry env, pytest
# runs from /home/AutoGPT/autogpts/autogpt. The gold test_patch paths are
# repo-root-relative (autogpts/autogpt/tests/...), so strip that prefix to make
# them relative to the pytest CWD.
_ERA2_STRIP = "autogpts/autogpt/"


def _target_test_files(patch: str, strip_prefix: str = "") -> list[str]:
    """Return only the real test files a PR's gold test_patch targets.

    Target side (``+++ b/<path>``) so newly-ADDED test modules are included.
    Keeps genuine test files (``tests.py`` / ``test_*.py`` / ``*_test.py`` /
    ``*_tests.py`` under a ``tests``/``test`` dir); drops fixtures/support
    (conftest.py, __init__.py, utils.py) and same-named source files not under a
    tests dir. strip_prefix rebases repo-root-relative paths onto the pytest CWD.
    """
    out: list[str] = []
    seen: set[str] = set()
    for f in re.findall(r"^\+\+\+ b/(.+?)\s*$", patch, re.M):
        if f == "/dev/null":
            continue
        base = f.rsplit("/", 1)[-1]
        if not base.endswith(".py"):
            continue
        if base in ("conftest.py", "__init__.py", "utils.py"):
            continue
        is_test = (
            base == "tests.py"
            or base.startswith("test_")
            or base.endswith("_test.py")
            or base.endswith("_tests.py")
        )
        under_tests = any(s in ("tests", "test") for s in f.split("/")[:-1])
        if base == "tests.py" or (is_test and under_tests):
            p = f[len(strip_prefix):] if strip_prefix and f.startswith(strip_prefix) else f
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PATH="/root/.local/bin:$PATH"
cd /home/AutoGPT/autogpts/autogpt
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files present at baseline stage"; exit 0; fi
poetry run pytest $RUN -v --tb=short --continue-on-collection-errors \\
    -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""

_APPLY_FN = """apply_patch_lenient() {
    local p="$1"
    if git -C /home/AutoGPT apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
        --exclude='*.ico' --exclude='*.zip' --exclude='*.pdf' --exclude='*.bin' \\
        --exclude='*.wasm' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' \\
        --exclude='*.otf' --exclude='*.eot' --exclude='*.tar.gz' --exclude='*.so' \\
        --exclude='*.dylib' "$p" 2>/dev/null; then
        return 0
    fi
    echo "git apply failed for $p, trying patch -p1 --fuzz=3 fallback..." >&2
    if patch -p1 --forward --fuzz=3 --batch --reject-file=/tmp/$(basename $p).rej < "$p" 2>&1 | tail -50; then
        return 0
    fi
    echo "Warning: $p did not fully apply, continuing anyway" >&2
    return 0
}
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PATH="/root/.local/bin:$PATH"
cd /home/AutoGPT
""" + _APPLY_FN + """apply_patch_lenient /home/test.patch
cd /home/AutoGPT/autogpts/autogpt
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files after test.patch"; exit 0; fi
poetry run pytest $RUN -v --tb=short --continue-on-collection-errors \\
    -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PATH="/root/.local/bin:$PATH"
cd /home/AutoGPT
""" + _APPLY_FN + """apply_patch_lenient /home/test.patch
apply_patch_lenient /home/fix.patch
cd /home/AutoGPT/autogpts/autogpt
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files after patches"; exit 0; fi
poetry run pytest $RUN -v --tb=short --continue-on-collection-errors \\
    -p no:cacheprovider --no-header \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""


# PRs 5241, 5868 (Sept -> Dec 2023, AutoGPT release 0.4.7 -> 0.5.x).
# Forge era: project restructured under autogpts/autogpt/. Uses Poetry.
# Python 3.10 per pyproject.toml (`python = "^3.10"`).
#
# Two-class layout: ImageBase installs Poetry + system deps + clones the repo
# (shared by both PRs). ImageDefault layers patches + scripts. prepare.sh does
# the per-PR checkout, then `poetry install` with a fallback chain for PR 5241
# whose poetry.lock pins yanked agent-protocol* versions.


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
        return "python:3.10-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base = self.dependency()
        if isinstance(base, Image):
            base = base.image_full_name()
        # Minimal base: apt deps + Poetry CLI. Repo clone deferred to
        # prepare.sh so the cross-arch base build stays small. Poetry CLI
        # is fine to install here because it's era-wide, not per-PR.
        #
        # The leading `# syntax=docker/dockerfile:1.6` directive makes
        # DockerfileEnhancer.enhance() emit this Dockerfile verbatim: it injects
        # the ARG/ENV/LABEL + proxy/CA-cert infra ONLY when that directive is
        # absent. This is how the base opts out of proxy and certificate
        # injection without modifying image.py.
        return f"""# syntax=docker/dockerfile:1.6

FROM {base}

ARG TARGETARCH
ARG REPO_URL="https://github.com/Significant-Gravitas/AutoGPT.git"
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/root/.local/bin:$PATH"
ENV TZ=UTC
ENV LANG=C.UTF-8
LABEL org.opencontainers.image.title="Significant-Gravitas/AutoGPT" \\
      org.opencontainers.image.description="Significant-Gravitas/AutoGPT base image (era2)" \\
      org.opencontainers.image.source="github.com/Significant-Gravitas/AutoGPT" \\
      org.opencontainers.image.authors="ethara.ai"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl build-essential libxml2-dev libxslt1-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 -

# Clone at HEAD (no base commit) and pre-install the COMMON deps so per-PR
# images reuse this layer. The authoritative per-commit checkout + install runs
# later in prepare.sh; the `|| true` keeps the base build green even when HEAD's
# layout drifts (era-2 poetry project lives under autogpts/autogpt).
RUN git clone "${{REPO_URL}}" /home/AutoGPT

WORKDIR /home/AutoGPT

RUN if [ -f autogpts/autogpt/pyproject.toml ]; then cd autogpts/autogpt && poetry install --no-root; \\
    elif [ -f pyproject.toml ]; then poetry install --no-root; fi || true

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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-era2"

    def workdir(self) -> str:
        # Instance-dir name. gen_report.collect_report_tasks parses the PR number
        # via int(dir_name[3:]) expecting "pr-<number>", so NO era suffix here
        # (an "-era2" suffix makes int("<n>-era2") raise and the instance is
        # silently skipped -> 0 reports). PR numbers are unique across eras, so
        # "pr-<number>" cannot collide. The era stays on image_tag() above so the
        # per-era images remain distinct/cached.
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
                "prepare.sh",
                """#!/bin/bash
set -e
export PATH="/root/.local/bin:$PATH"
cd /home
BASE_COMMIT="{pr.base.sha}"
if [ ! -d /home/AutoGPT/.git ]; then
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
fi
cd /home/AutoGPT
git reset --hard 2>/dev/null || true
# A sibling build may have reused + hardened (history-pruned) this checkout to a
# different base commit; if the sha we need is gone, re-clone before checkout.
if ! git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null; then
    cd /home && rm -rf /home/AutoGPT
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
    cd /home/AutoGPT
fi
git checkout "${{BASE_COMMIT}}"
# --- Reward-hacking hardening (mirrors image.Image._HARDENING_BLOCK) ---------
# Detach at the base commit and strip every ref, remote, reflog, tag and
# alternate so the evaluated model cannot recover the fix from git history or
# the PR branch. Assertions abort the build (set -e) if anything leaks through.
# Run while still at the repo root (/home/AutoGPT), before descending into the
# autogpts/autogpt subtree for the Poetry install.
git checkout --detach "${{BASE_COMMIT}}"
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD;
        git remote remove origin 2>/dev/null || true;
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d;
        git reflog expire --expire=now --all;
        git reflog expire --expire-unreachable=now --all;
        git gc --prune=now --aggressive;
        rm -f .git/objects/info/alternates;
    '
fi
# --- end hardening -----------------------------------------------------------
cd /home/AutoGPT/autogpts/autogpt
# Try a clean install; if locked agent-protocol* (PR 5241) is yanked, fall back
# to pip-installing AutoGPT's runtime deps directly with version pins that
# keep pydantic v1 working.
poetry install --no-interaction 2>&1 | tee /tmp/poetry.log
poetry_status=${{PIPESTATUS[0]}}
if [ "$poetry_status" -ne 0 ]; then
    poetry lock --no-cache --regenerate || true
    poetry install --no-interaction --no-root --without dev || true
    poetry run pip install --no-cache-dir \\
        'pydantic<2' 'spacy>=3.5,<3.7' 'openapi-python-client<0.14' \\
        'numpy<2' \\
        PyPDF2 jsonschema pyyaml openai colorama distro inflection \\
        beautifulsoup4 requests tiktoken orjson python-dotenv click \\
        gitpython ftfy charset-normalizer numpy selenium \\
        webdriver-manager docker duckduckgo-search prompt_toolkit \\
        Pillow markdown pylatexenc python-docx readability-lxml \\
        gTTS 'playsound==1.2.2' pinecone-client redis fastapi \\
        hypercorn uvicorn psycopg2-binary boto3 \\
        google-api-python-client google-cloud-logging google-cloud-storage \\
        'agent-protocol>=1' 'agent-protocol-client>=1' \\
        'auto-gpt-plugin-template @ git+https://github.com/Significant-Gravitas/Auto-GPT-Plugin-Template@0.1.0' \\
        sentry-sdk pypdf \\
        || true
fi
poetry run pip install --no-cache-dir pytest pytest-mock pytest-asyncio \\
    pytest-cov pytest-recording pytest-xdist asynctest vcrpy \\
    sentry-sdk || true
# sentry_sdk (added unconditionally above): pr-5868 takes the poetry-SUCCESS path,
# where poetry's default groups can omit it, so the conftest import chain
# autogpt.agents.agent -> sentry_sdk fails and pytest collects 0 tests.
# forge: a sibling local package (autogpts/forge) imported as `forge.sdk...`.
# poetry's path-dep install misses it when the lock falls back (pr-5241 ->
# "No module named 'forge'"), so install it editable; fall back to --no-deps if
# forge's own pins hit the yanked agent-protocol, then top up forge.sdk.db's deps.
if [ -d /home/AutoGPT/autogpts/forge ]; then
    poetry run pip install --no-cache-dir -e /home/AutoGPT/autogpts/forge \\
        || poetry run pip install --no-cache-dir --no-deps -e /home/AutoGPT/autogpts/forge || true
    poetry run pip install --no-cache-dir sqlalchemy python-multipart || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch, _ERA2_STRIP)),
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch, _ERA2_STRIP)),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch, _ERA2_STRIP)),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_name()
        base_tag = base.image_tag()
        # COPY prepare.sh and RUN it FIRST so the expensive clone+install layer
        # caches independently of the run/test/fix scripts + patches. Editing those
        # then only invalidates the cheap trailing COPYs, not the pip/poetry install.
        other = [f for f in self.files() if f.name != "prepare.sh"]
        copy_rest = "\n".join(f"COPY {f.name} /home/" for f in other)
        return f"""FROM {base_name}:{base_tag}

COPY prepare.sh /home/

RUN bash /home/prepare.sh

{copy_rest}
"""


@Instance.register("Significant-Gravitas", "AutoGPT_5868_to_5241")
class AUTOGPT_5868_TO_5241(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        pattern = r"(\btests(?:\.py|/[^\s]+?)::[^\s]+?) (PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b"
        for test_name, status in re.findall(pattern, log):
            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Each dataset record carries number_interval = its prs_in_bundle joined by
# "-" (NOT a low-high range). Instance.create() routes on
# f"{org}/{number_interval}" whenever a record sets number_interval, so every
# bundle interval belonging to this era must be registered to AUTOGPT_5868_TO_5241
# (in addition to the "AutoGPT_5868_to_5241" name on the class above), else create() raises
# "Instance ... is not registered" before any image is built.
_BUNDLE_NUMBER_INTERVALS = [
    "5241-5246-5247-5248-5252-5254-5255-5258-5259-5264-5266-5267-5268-5269-5270-5271-5272-5275-5279-5282-5285-5286-5287-5288-5290-5291-5294-5295-5296-5297-5298-5300-5303-5304-5306-5315-5316-5318-5321-5322-5323-5329-5330-5332-5333-5334-5335-5336-5338-5340-5341-5342-5345-5347-5348-5351-5356-5358-5360-5361-5362-5363-5364-5366-5368-5372-5373-5374-5376-5377-5379-5383-5385-5387-5390-5391-5393-5396-5399-5401-5403-5404-5407-5410-5411-5412-5413-5415-5421-5423-5425-5427-5428-5431-5432-5433-5435-5436-5437-5439-5441-5442-5443-5444-5446-5448-5449-5450-5454-5455-5456-5457-5458-5460-5461-5462-5464-5465-5467-5469-5470-5471-5474-5475-5476-5477-5478-5479-5481-5482-5483-5485-5491-5492-5497-5498-5499-5500-5501-5503-5504-5505-5506-5507-5508-5509-5510-5512-5513-5514-5515-5516-5520-5521-5522-5528-5531-5533-5534-5535-5538-5539-5540-5541-5543-5545-5547-5549-5550-5551-5552-5553-5554-5555-5556-5560-5561-5563-5564-5566-5567-5568-5569-5570-5572-5573-5576-5577-5578-5579-5580-5581-5582-5584-5585-5586-5587-5588-5589-5597-5599-5600-5601-5603-5607-5608-5610-5611-5612-5613-5614-5615-5619-5620-5621-5627-5629-5630-5632-5633-5634-5638-5639-5641-5645-5647-5648-5649-5651-5652-5653-5654-5655-5656-5664-5665-5668-5671-5672-5674-5675-5679-5680-5683-5688-5689-5690-5692-5693-5696-5697-5698-5700-5704-5706-5707-5711-5713-5714-5715-5717-5719-5720-5721-5725-5730-5731-5732-5733-5735-5736-5737-5739-5743-5744-5746-5747-5749-5750-5752-5754-5755-5757-5758-5759-5764-5765-5766-5767-5768-5769-5770-5771-5772-5776-5777-5779-5780-5781-5783-5784-5789-5793-5797-5798-5799-5801-5804-5805-5806-5808-5813-5814-5815-5816-5821-5824-5847-5848-5851-5871-5989-5990-5992-5993-5994-5995-5996-6003-6005-6007-6012-6013-6015-6016-6021-6023-6027-6028-6029-6030-6031-6035-6037-6038-6039-6041-6042-6043-6044-6053-6055-6056-6057-6058-6061-6063-6064-6065-6066-6068-6069-6072-6077-6080-6082-6084-6087-6088-6091-6092-6094-6096-6099-6102-6104-6105-6107-6108-6118-6119-6121-6124-6125-6127-6147-6203-6236-6259-6274-6284-6313-6324-6335-6379-6485-6497-6510-6512-6558",
    "5868-6378-6569-6571-6643-6650-6653-6691-6777-6778-6822-6888-6900-6903-6927-6931-6937-6938-6946-6952-6990-6992-6995-6996-6997-7005-7010-7014-7016-7025-7026-7029-7035-7040-7041-7045-7046-7082",
]
for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("Significant-Gravitas", _ni)(AUTOGPT_5868_TO_5241)
