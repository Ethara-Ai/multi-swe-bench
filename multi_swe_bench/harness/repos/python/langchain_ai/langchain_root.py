import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

class LangchainRootImageBase(Image):
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
        return "python:3.11-slim"

    def image_tag(self) -> str:
        return "base-root"

    def workdir(self) -> str:
        return "base-root"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_INPUT=1
ENV POETRY_VIRTUALENVS_IN_PROJECT=true
ENV POETRY_NO_INTERACTION=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    curl \\
    ca-certificates \\
    pkg-config \\
    libffi-dev \\
    libssl-dev \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry<1.5"

WORKDIR /home/

{code}

{self.clear_env}

"""

class LangchainRootImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return LangchainRootImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        # gen_report.py:357 only collects workdirs whose name starts with
        # "pr-", so we prefix with pr- and suffix -root for era disambiguation.
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
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
                "strip_binaries.sh",
                r"""#!/bin/bash
# Drop diff sections for binary files. The dataset's test_patch/fix_patch
# include hunks for PDFs, images, .xlsx, .zip, .db, etc. that lack the
# "full index line" git apply needs for binary blobs, which aborts the
# whole `git apply` and leaves the working tree unpatched. These binary
# files never affect pytest outcomes, so dropping their diff sections is
# safe and turns the apply back into a no-op for them.
awk '
BEGIN { skip = 0 }
/^diff --git / {
  skip = 0
  if ($0 ~ /\.(ico|icns|png|jpe?g|gif|bmp|webp|woff2?|ttf|eot|otf|pdf|zip|tar|tgz|tbz2?|txz|bz2|xz|gz|class|jar|war|ear|enc|gpg|asc|p7s|der|crt|key|pem|sig|odt|ods|odp|docx|xlsx|pptx|msg|vsdx|db|sqlite3?|bin|dat|so|dll|dylib|a|o|obj|exe|wasm|mp[34]|wav|ogg|flac|webm|mov|avi|mkv|ipynb|faiss|pkl|npy|npz|joblib|model|onnx|pt|pth|safetensors|h5|parquet|arrow|feather|index)( |$)/) skip = 1
}
{ if (!skip) print }
' "$1"
""",
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
# Discover EVERY tests/unit_tests/ directory in the repo after patches and
# run pytest against each from a venv that owns the working langchain
# install. The "set up experimental" PR's fix_patch restructures the repo
# (moves langchain/ -> libs/langchain/ + adds libs/experimental/) so we
# can't bake one TEST_ROOT into the script.
#
# Strategy:
#   1. Pick the venv directory whose poetry env has langchain importable.
#      That's always /home/{pr.repo}/.venv on the pre-restructure base,
#      and after fix_patch it may need re-creation in libs/langchain/.
#   2. From that venv, run pytest against every tests/unit_tests/ dir.
#   3. Prefix output lines with [pkg] so parse_log keeps IDs unique.
set +e

# Find the venv that has langchain installed. Default to /home/{pr.repo}.
VENV_ROOT=/home/{pr.repo}
for candidate in /home/{pr.repo} /home/{pr.repo}/libs/langchain; do
    [ -d "$candidate/.venv" ] || continue
    if "$candidate/.venv/bin/python" -c "import langchain" >/dev/null 2>&1; then
        VENV_ROOT="$candidate"
        break
    fi
done

# If no working venv exists yet (fix_patch restructure left libs/langchain
# without one), bootstrap it now.
if [ -f /home/{pr.repo}/libs/langchain/pyproject.toml ] && \\
   ! "$VENV_ROOT/.venv/bin/python" -c "import langchain" >/dev/null 2>&1; then
    (
        cd /home/{pr.repo}/libs/langchain
        poetry install --with test --no-interaction >/dev/null 2>&1 || true
        poetry run pip install -e . --no-deps >/dev/null 2>&1 || true
        if ! poetry run python -c "import pytest_socket, pytest_asyncio, pytest_mock" 2>/dev/null; then
            poetry run pip install "pytest<8" "pytest-asyncio<0.24" \\
                pytest-socket pytest-mock pytest-cov pytest-dotenv \\
                freezegun responses syrupy >/dev/null 2>&1 || true
        fi
        poetry run pip install -e . >/dev/null 2>&1 || true
    )
    VENV_ROOT=/home/{pr.repo}/libs/langchain
fi

PYTEST="$VENV_ROOT/.venv/bin/pytest"
PYBIN="$VENV_ROOT/.venv/bin/python"
[ ! -x "$PYTEST" ] && PYTEST=$(command -v pytest)

# pytest-socket support is per-venv.
SOCKET_FLAGS=""
"$PYBIN" -c "import pytest_socket" >/dev/null 2>&1 && \\
    SOCKET_FLAGS="--disable-socket --allow-unix-socket"

ran_any=0
while IFS= read -r tdir; do
    # tdir is .../X/tests/unit_tests — we want X as the parent and the
    # relative tests/unit_tests/ as the pytest target.
    parent=$(dirname "$(dirname "$tdir")")
    pkg=$(echo "$parent" | sed "s#^/home/{pr.repo}/##; s#^/home/{pr.repo}#root#")
    [ -z "$pkg" ] && pkg=root
    echo "================= PYTEST PKG: $pkg ================="
    (
        cd "$parent"
        "$PYTEST" -o addopts= --no-header -rA --tb=no -p no:cacheprovider \\
            --continue-on-collection-errors $SOCKET_FLAGS tests/unit_tests/ 2>&1 \\
            | sed -E "s#^(PASSED|FAILED|ERROR|XFAIL|XPASS)[[:space:]]+#\\1 [$pkg] #; s#^(SKIPPED \\[[0-9]+\\])[[:space:]]+#\\1 [$pkg] #"
    )
    ran_any=1
done < <(find /home/{pr.repo} -maxdepth 5 -type d -name unit_tests -path '*/tests/unit_tests' -not -path '*/.venv/*' 2>/dev/null | sort)

[ "$ran_any" = "0" ] && echo "run_tests: no tests/unit_tests directory found" >&2
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

cd /home/{pr.repo}
poetry install --with test --no-interaction || true
# Fallback: a single dep build failure (e.g. duckdb-engine on ARM) cuts
# the install short and leaves langchain itself uninstalled. Re-run via
# pip with --no-deps for the project, then runtime deps individually so
# unrelated build errors don't hide langchainplus_sdk / langsmith / etc.
poetry run pip install -e . --no-deps 2>/dev/null || true
# Only install pytest plugins if a plugin is genuinely missing — pinning
# pytest<8 so the resolver doesn't drag in pytest 9 (whose deprecations
# break the era's PytestRemovedIn9Warning-as-error suite).
if ! poetry run python -c "import pytest_socket, pytest_asyncio, pytest_mock" 2>/dev/null; then
    # Pin pytest-asyncio<0.24 (older release that still works with pytest<8);
    # later pytest-asyncio versions need pytest>=8.2 and pull pytest 9 back in.
    poetry run pip install "pytest<8" "pytest-asyncio<0.24" \\
        pytest-socket pytest-mock pytest-cov pytest-dotenv freezegun \\
        responses syrupy 2>/dev/null || true
fi
poetry run pip install -e . 2>/dev/null || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /tmp/test.filtered.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
bash /home/strip_binaries.sh /home/fix.patch  > /tmp/fix.filtered.patch
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /tmp/test.filtered.patch /tmp/fix.filtered.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Per-PR anti-cheat hardening. dependency() returns an Image, so
        # DockerfileEnhancer emits this Dockerfile verbatim (it only auto-injects
        # the hardening into str-dependency/base images), hence we embed
        # Image._HARDENING_BLOCK ourselves. ENV BASE_COMMIT resolves the block's
        # ${BASE_COMMIT}; WORKDIR pins the repo dir so the hardening RUN (detach
        # onto BASE_COMMIT -> drop every ref/remote -> GC unreachable objects ->
        # self-audit) operates on the checkout prepare.sh produced.
        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{copy_commands}
{prepare_commands}

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""

@Instance.register("langchain-ai", "langchain_root")
class LangchainRoot(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LangchainRootImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Pytest with -rA emits parametrized test ids that may contain spaces
        # (e.g. test_foo[foo bar baz]), so capture everything up to the
        # optional " - <reason>" trailer rather than the next whitespace.
        re_pass = re.compile(r"^PASSED\s+(.+?)\s*$")
        re_fail = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_error = re.compile(r"^ERROR\s+(.+?)(?:\s+-\s.*)?\s*$")
        # SKIPPED format: "SKIPPED [N] file:line: reason" — keep file:line as
        # the unique identifier so per-line skips don't collapse to one entry.
        re_skip = re.compile(r"^SKIPPED\s+\[\d+\]\s+(\S+?:\d+)(?::\s.*)?\s*$")
        re_xfail = re.compile(r"^XFAIL\s+(.+?)\s*$")
        re_xpass = re.compile(r"^XPASS\s+(.+?)\s*$")

        for raw in test_log.splitlines():
            line = raw.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_error.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

            m = re_xfail.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

            m = re_xpass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

        common_pf = passed_tests & failed_tests
        passed_tests -= common_pf
        common_ps = passed_tests & skipped_tests
        skipped_tests -= common_ps
        common_fs = failed_tests & skipped_tests
        failed_tests -= common_fs

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_LangchainRoot = [
    '4047-5172-5326-5541-5580-5613-5625-5650-5661-5703-5728-5731-5742-5743-5745-5749-5751-5752-5753-5757-5758-5761-5766',
    '5138-5165-5259-5290-5292-5305-5309-5311-5314-5320-5323-5331-5351-5364',
    '5206-5495-5515-5563-5569-5571-5573-5574-5578-5581-5602-5616-5620-5621',
    '5543-6186-6275-6299-6381-6386-6396-6398-6400-6409-6421-6423-6425-6429-6430-6433-6442-6446-6447-6448-6449-6450-6453-6456-6457-6463-6464-6465',
    '5588-6775-6790-6862-6865-6867-6881-6887-6891-6892-6897-6899',
    '5666-5695-5750-5772-5782-5783-5784-5789-5792-5794-5796-5800-5802-5805-5806-5808-5811-5812',
    '5716-5780-5920-6060-6063-6223-6229-6247-6261-6262',
    '5804-6719',
    '5825-5836-5856-5858-5862-5874-5877-5891-5894-5896-5908-5912-5914-5917-5919-5947-5949',
    '5898-6103-6112-6113-6115-6130',
    '5922-6054-6133-6136-6148-6155-6163-6185-6195-6233',
    '6154-6382-6480-6487-6488',
    '6165-6328-6350-6402-6408-6410',
    '6222-6249-6301-6315-6424-6437-6483-6559-6571-6656',
    '7595-7947-7948',
    '3350-5129-5321-5377-5394-5397-5413-5427-5430-5432-5439-5440-5443-5446-5449-5450-5461-5464-5466-5468-5471-5480-5503-5504',
    '3817-7789-7868-7877-7890-7901-7908-7910-7927-7929-7930-7931-7941-7951-7952-7955-7956-7962-7970-7972-7973-7976-7978-7983-7984-7988-7993-8000-8007-8008-8012',
    '4521-5166-5470-5478-5485-5500-5501-5507-5512-5518-5523-5524-5525-5526-5527-5528-5529-5533-5538-5559-5567-5568',
    '4693-5219-5282-5293-5306-5325-5327-5330-5343-5344-5359-5371-5373-5380-5382-5395-5402-5403-5404-5407',
    '4767-5050-5171-5338-5381-5405-5408-5417-5425-5438-5442',
    '4979-5116-5179-5315-5332-5339-5383-5385-5401-5479-5497-5517-5540-5547-5566-5584-5589-5590-5595-5609-5610-5617-5629-5632-5636-5637-5639-5646-5655-5657-5659-5664-5665-5671-5673-5676-5680-5681-5704',
    '5089-6595-6601-6603-6604-6607-6609-6611-6616-6622-6626-6634-6644-6645-6664-6667-6669-6670-6673-6676-6684-6688',
    '5196-7505-7647-7734-7755-7759-7767-7774-7779-7793-7794-7796-7805-7807-7808-7812-7814-7836-7838',
    '5572-5575-5619-5641-5693-5715-5781-5793-5801-5810-5814-5815-5818-5832-5837-5841-5844-5847-5854-5855-5864-5865-5866',
    '5733-5878-5929-5966-6006-6008-6010-6011-6012-6015-6017-6018-6020-6022-6023-6026',
    '5798-5956-5988',
    '5823-5902-5925-5939-5950-5955-5958-5967-5979-5985-5989-5990-5991-5992-5993-6001-6007',
    '5848-6927-7015-7022-7051-7087-7101-7104-7123-7151-7253-7281-7301-7344-7348-7355-7357-7359-7360-7362-7363-7370-7372-7377-7378-7381-7382-7383-7390-7392-7393',
    '5860-6042-6044-6048-6049-6056-6062-6066-6067-6069-6076-6077-6078-6099-6100-6102',
    '5879-7681-7694-7704-7705-7708-7710-7712-7714-7718-7728-7730-7740-7743-7744-7750-7751-7754',
    '5903-5938-6199-6313-7477-7562-7568-7636-7651-7667-7668-7672-7675-7676-7677-7682-7686-7687-7690-7697-7699-7701-7707',
    '5954-6119-6536-6537-6541-6544-6552-6554-6555-6562-6563-6565-6568-6569-6572-6578-6580-6584-6587-6593',
    '5962-6560-6728-6802-6944-6972-6975-6977-6978-6979-6985-6986-6988-6996-7013-7014-7017-7023-7025-7028-7030-7031-7045-7047',
    '5997-6031-6035-6040-6043-6086-6107-6110-6169-6189-6221-6263-6283-6300-6302-6303-6304-6305-6309-6314-6316-6317-6318-6319-6320-6327-6332-6339-6340-6341-6344-6349-6359-6372',
    '6089-6123-6132-6141-6153-6170-6181-6187-6211-6219-6226-6235-6239-6246-6248-6256-6258-6276-6277-6281-6287-6297-6323-6375-6376-6383-6385-6387-6388-6389-6390-6391-6392',
    '6122-6216-6218-6242-6326-6354-6399-6426-6454-6468-6474-6479-6492-6495-6496-6498-6501-6507-6509-6510-6518-6540',
    '6124-6232-6331-6342-6419-6476-6524-6573-6625-6769-6785-6806-6834-6835-6845-7379-7388-7521-7611-7612-7614-7621-7622-7627-7629-7637-7639-7643-7653-7659',
    '6255-6528-6570-6937-7152-7229-7386-7453-7473-7486-7507-7544-7545',
    '6274-6401-6420-6486-6780-6781-6788-6791-6792-6798-6804-6812-6816-6821-6823-6832-6833-6839-6840-6842-6849-6857',
    '6306-6311-6321-6357-6360-6432-6473-6591-6594-6600-6663-6692-6697-6702-6705-6708-6710-6727-6736-6746-6765-6770',
    '6351-6366-6525-6864-6942-6964-6990-7033-7114-7122-7143-7173-7178-7195-7196-7218-7219-7230-7240-7245-7247-7248-7250-7256-7257-7258-7261-7263-7264-7266-7267-7270-7271-7274-7284-7292-7293-7296-7297-7300-7306-7312-7316-7318-7324-7326-7330-7335',
    '6440-6506-6847-6876-6890-6913-6967-6989-6995-7005-7006-7012-7050-7055-7086',
    '6515-6519-6882-6893-6976-6987-6992-7000-7046-7068-7069-7070-7074-7081-7083-7096-7120-7133-7134-7159-7161-7165-7169-7170-7174-7176-7180-7185-7186-7194-7202-7204-7205-7206-7207-7209-7213-7214-7216-7222-7227-7228-7235-7236-7237-7238-7241-7244',
    '6538-6615-6629-6668-6686-6703-6734-6755-6782-6784-6787-6796-6799-6807-6808-6831',
    '6558-6827-6994-7073-7085-7090-7092-7093-7095-7097-7099-7102-7105-7107-7109-7110-7128-7132-7155',
    '6698-7113-7460-7520-7749-7752-7753-7757-7764-7768-7769-7771-7773-7781-7783-7809-7810-7843-7844-7847-7848-7850-7852-7856-7857-7858-7859-7860-7861-7862-7863-7866-7870-7871-7874-7884-7891-7892-7893-7898-7904-7907-7911-7913-7918-7937-7938',
    '6729-6771-6801-6824-6830-6855-6858-6871-6874-6888-6894-6895-6901-6902-6904-6908-6945-6948-6949-6960-6962',
    '6783-6859-7310-7319-7356-7367-7397-7399-7409-7416-7417-7425-7437-7442-7444-7447-7461-7464-7465-7467',
    '7026-7276-7358-7394-7398-7404-7478-7487-7504-7510-7511-7514-7530-7534-7540-7550-7555-7556-7560-7561-7566-7570-7580-7582-7584-7587-7592-7593',
    '7940-7945-7961-7966-8001-8010-8014-8022-8025-8027-8030-8033-8035-8036-8041-8044-8045-8046-8047-8048-8049-8077',
]
for _ni in _BUNDLE_NIS_LangchainRoot:
    Instance.register('langchain-ai', _ni)(LangchainRoot)
