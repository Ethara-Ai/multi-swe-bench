import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        return "base-agno_99999_to_5000"

    def workdir(self) -> str:
        return "base-agno_99999_to_5000"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # `# syntax` opts this shared era base out of the DockerfileEnhancer,
        # which would otherwise rewrite the `git clone` into clone + `git checkout
        # ${BASE_COMMIT}` + prune HERE, pruning the shared base to one PR's
        # base.sha and breaking every other PR in the era ("reference is not a
        # tree"). The base keeps FULL history; the anti-reward-hack hardening runs
        # per-PR at the literal base.sha (see ImageDefault below).
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
RUN pip install --no-cache-dir --upgrade pip uv
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

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}
cd /home/{self.pr.repo}/libs/agno
EXTRAS_LIST=$(python -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml','rb') as f:
    data = tomllib.load(f)
# Skip known-conflicting extras (lmstudio + brave-search conflict on httpx);
# they're recovered later in the per-extra fallback if needed.
SKIP = {{'lmstudio', 'brave', 'brave-search', 'brave_search', 'infinity', 'infinity_client'}}
extras = [k for k in data.get('project', {{}}).get('optional-dependencies', {{}}).keys() if k not in SKIP]
print(','.join(extras))
print('---')
print('\\n'.join(extras))
" 2>/dev/null)
EXTRAS_CSV=$(echo "$EXTRAS_LIST" | sed -n '1p')
EXTRAS_ITER=$(echo "$EXTRAS_LIST" | sed -n '3,$p')
UV_OPTS="--no-cache --python /usr/local/bin/python --index-strategy unsafe-best-match"
if [ -n "$EXTRAS_CSV" ] && uv pip install $UV_OPTS -e ".[${{EXTRAS_CSV}}]"; then
    echo "--- bulk install succeeded ---"
else
    echo "--- bulk install failed; falling back to per-extra loop ---"
    uv pip install $UV_OPTS -e ".[dev]" \\
        || uv pip install $UV_OPTS -e . \\
        || pip install --no-cache-dir -e ".[dev]" \\
        || true
    if [ -n "$EXTRAS_ITER" ]; then
        for extra in $EXTRAS_ITER; do
            [ "$extra" = "dev" ] && continue
            echo "--- installing extra: $extra ---"
            uv pip install $UV_OPTS -e ".[${{extra}}]" || true
        done
    fi
fi
uv pip install $UV_OPTS sqlalchemy pypdf chromadb lancedb tantivy qdrant-client pgvector pillow || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true; \\
                grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/fix.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
GIT_BIN_EXCL="--exclude=*.pdf --exclude=*.docx --exclude=*.doc --exclude=*.swp \\
              --exclude=*.jpg --exclude=*.jpeg --exclude=*.png --exclude=*.gif --exclude=*.ico --exclude=*.bmp --exclude=*.svg \\
              --exclude=*.zip --exclude=*.tar --exclude=*.gz --exclude=*.bz2 --exclude=*.xz \\
              --exclude=*.bin --exclude=*.so --exclude=*.dll --exclude=*.exe --exclude=*.pyc \\
              --exclude=*.mp3 --exclude=*.mp4 --exclude=*.wav --exclude=*.ogg --exclude=*.webm \\
              --exclude=*.pkl --exclude=*.npz --exclude=*.npy --exclude=*.db --exclude=*.sqlite"
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn $GIT_BIN_EXCL /home/test.patch; then
        echo "Error: git apply test.patch failed (even after excluding binary files)" >&2
        exit 1
    fi
    echo "Note: test.patch applied with binary file exclusions"
fi
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
GIT_BIN_EXCL="--exclude=*.pdf --exclude=*.docx --exclude=*.doc --exclude=*.swp \\
              --exclude=*.jpg --exclude=*.jpeg --exclude=*.png --exclude=*.gif --exclude=*.ico --exclude=*.bmp --exclude=*.svg \\
              --exclude=*.zip --exclude=*.tar --exclude=*.gz --exclude=*.bz2 --exclude=*.xz \\
              --exclude=*.bin --exclude=*.so --exclude=*.dll --exclude=*.exe --exclude=*.pyc \\
              --exclude=*.mp3 --exclude=*.mp4 --exclude=*.wav --exclude=*.ogg --exclude=*.webm \\
              --exclude=*.pkl --exclude=*.npz --exclude=*.npy --exclude=*.db --exclude=*.sqlite"
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn $GIT_BIN_EXCL /home/test.patch /home/fix.patch; then
        echo "Error: git apply test+fix patches failed (even after excluding binary files)" >&2
        exit 1
    fi
    echo "Note: test+fix patches applied with binary file exclusions"
fi
cd /home/{self.pr.repo}/libs/agno
PATCH_TESTS=$( (grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/test.patch 2>/dev/null || true; \\
                grep -oE '^diff --git a/libs/agno/tests/[^ ]+\\.py' /home/fix.patch 2>/dev/null || true) \\
              | sed 's|^diff --git a/libs/agno/||' | sort -u )
EXISTING=""
for p in $PATCH_TESTS; do
    [ -f "$p" ] && EXISTING="$EXISTING $p"
done
PYTHONUNBUFFERED=1 timeout --kill-after=10 300 python -m pytest $EXISTING -v --no-header --tb=short --continue-on-collection-errors || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        # Per-PR anti-cheat hardening at the LITERAL base.sha. The shared base
        # keeps full history so every PR's base.sha is reachable; prepare.sh
        # checks out this PR's base.sha, then this block detaches at that literal
        # sha and strips every other ref/reflog so the fix commit is unreachable
        # from git. Runs in the PR layer because the shared base must NOT prune.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")
        return f"""# syntax=docker/dockerfile:1.6
FROM {dep.image_name()}:{dep.image_tag()}
{self.global_env}
COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}
CMD ["/bin/bash"]
"""


@Instance.register("agno-agi", "agno_99999_to_5000")
class AGNO_99999_TO_5000(Instance):
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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        passed_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+PASSED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        failed_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+FAILED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        skipped_pattern = re.compile(
            r"^(?:\[\s*\d+\]\s+)?(\S+)\s+SKIPPED\s+\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        passed_tests.update(passed_pattern.findall(clean_log))
        failed_tests.update(failed_pattern.findall(clean_log))
        skipped_tests.update(skipped_pattern.findall(clean_log))

        summary_failed = re.compile(r"^FAILED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        summary_passed = re.compile(r"^PASSED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        summary_skipped = re.compile(r"^SKIPPED\s+(\S+?)(?:\s+-.*)?$", re.MULTILINE)
        failed_tests.update(summary_failed.findall(clean_log))
        passed_tests.update(summary_passed.findall(clean_log))
        skipped_tests.update(summary_skipped.findall(clean_log))

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


# --- §11b bundle keys: dash-joined prs_in_bundle for this era -> AGNO_99999_TO_5000. 54 bundles.
_BUNDLE_NIS_AGNO_E2 = [
    "5022-5073-5150-5153-5161-5163",
    "5043-5107-5108-5109-5110-5122",
    "5045-5086-5277-5322-5323-5324-5325-5339",
    "5067-5069-5074",
    "5333-5341-5346-5351-5353-5354-5357-5358",
    "5650-5765-5781-5785-5787-5790",
    "5663-5742-5810-5840",
    "5672-5701-5759-5760-5762-5766-5767-5774-5776",
    "5756-5786-5800-5809-5819-5821-5822-5823-5824",
    "5770-5794-5795",
    "5777-5779",
    "5799-5808",
    "5859-5869-5912-5922-5926-5927-5933-5940-5942-5949",
    "5990-5993",
    "6116-6936-7017-7198-7222-7288-7303-7308-7328-7329-7343-7353-7358-7364-7372-7392-7401-7409-7417-7420-7423-7428-7433",
    "6277-6879-7208-7219-7221-7229",
    "6581-6615-6616-6644-6670-6694-6710-6712-6713-6717-6719",
    "6648-6681-6707-6714-6766-6809-6858-6864",
    "7681-7683-7694-7701",
    "7706-7708-7709-7710-7713",
    "7720-7724",
    "5151-5172-5187-5221-5239-5266-5271-5280-5283-5284-5285-5287-5303-5304-5305-5306-5307",
    "5223-5366-5374-5388-5406-5411-5420",
    "5415-5424",
    "5469-6816-7079-7429-7437-7442-7444-7445-7452-7457-7458-7462-7463",
    "5614-5655-5685-5848-5851-5854-5868-5870-5875-5890-5900-5903-5910-5914-5918",
    "5755-5988-6029-6336-6535-6536-6538-6557-6560-6562-6572-6576",
    "5818-5857-5919-5962-5965-5985-5987-5997-6002-6003-6010-6042-6053-6062-6064-6066-6074-6076",
    "5842-5866-6105-6142-6149-6152-6153-6154-6169-6171-6172-6176",
    "5862-6011-6014-6024-6056-6058-6067-6078-6079-6081-6083-6088-6098-6103-6106-6108-6109",
    "5872-5888-5889-5932-5934",
    "5897-5916-5957-5961-5964-5967-5976-5981-5984-5986",
    "5917-6129-6132-6138-6140-6143-6144-6146-6147-6148",
    "6018-6050-6054-6159-6184-6187-6190-6200-6203",
    "6052-6133-6181-6185",
    "6060-6377-6399-6466-6579-6643-6821-6927-6930-6949-6956-6957-6959-6965-6969-6973-6981-7011-7013-7021-7026-7027",
    "6061-6118-6119-6120-6123-6128-6130",
    "6085-6830-6855-6891-6946-6951-6955-6971-6989-6995-7042-7061-7067-7071-7085-7098-7099-7109-7132-7152-7154-7155-7158-7184",
    "6091-6092-6101-6192-6212-6215-6229-6230-6241-6255-6260-6272-6273-6285-6288-6290-6291-6293",
    "6100-6180-6208-6213-6214-6217-6227-6228",
    "6211-6216-6292-6405-6483-6610-6622-6628-6650-6656-6657-6661-6663-6665-6668-6678-6685-6691-6703-6704",
    "6225-6251-6287-6306-6311-6318-6323-6329-6337-6395-6408-6429-6476-6501-6506-6511-6514-6515-6517-6518-6519-6520-6522-6525-6526-6531",
    "6266-6297-6363-6367-6495-6502-6544-6546-6580-6584-6588-6591-6593-6603-6604-6605-6606-6607-6608-6609-6611-6618-6619",
    "6270-7148-7181-7190-7212-7225-7227-7230-7243-7244-7245-7252-7255-7256-7257-7258",
    "6278-6598-6638-6671-6718-6723-6730-6749-6791-6806-6823-6831-6833-6836-6837",
    "6550-6938-7214-7702-7703-7704-7711-7732-7743-7747-7750-7759-7764-7788-7798-7809-7811",
    "6555-6630-6721-6765-6793-6848-6857-6868-6869-6873-6886-6887",
    "6649-6652-6672-6888-6906-6926-6928-6933-6940-6941",
    "7080-7247-7277-7280-7282-7285-7287-7295-7299-7300-7304",
    "7359-7371-7499-7503-7515-7523-7529-7548-7553-7561-7568-7584-7591-7614-7618-7622-7626-7627-7646-7648-7654",
    "7379-7402-7453-7470-7485-7491-7492-7494-7496-7498-7507-7508-7511",
    "7574-7816-7817-7818-7828-7829-7836-7840-7844-7849-7869-7877-7900-7906-7914-7915",
    "7606-7796-7892-7893-7913-7916-7921-7926-7931-7936",
    "7615-7655-7662-7663-7666-7667",
]
for _ni in _BUNDLE_NIS_AGNO_E2:
    Instance.register("agno-agi", _ni)(AGNO_99999_TO_5000)

