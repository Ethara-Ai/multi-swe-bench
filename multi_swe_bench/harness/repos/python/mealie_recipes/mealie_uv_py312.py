import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# `uv` is a Rust binary that SIGSEGVs under QEMU x86 emulation (multi-arch
# builds on an arm64 host). So: try `uv sync` (works on native arch); if it
# crashes, fall back to a pure-Python pip install of the same dependency set
# (project + pgsql extra + the `dev` dependency-group from pyproject).
_INSTALL = (
    "uv sync --group dev --extra pgsql || ( "
    "pip install --upgrade pip setuptools wheel && "
    "pip install -e \".[pgsql]\" && "
    "python3 -c \"import tomllib; d=tomllib.load(open('pyproject.toml','rb')); "
    "g=(d.get('dependency-groups') or dict()).get('dev') or list(); "
    "print(chr(10).join(x for x in g if isinstance(x,str)))\" > /tmp/devreqs.txt && "
    "pip install -r /tmp/devreqs.txt )"
)
# Use uv's venv when present (native arch), else the system interpreter that
# the pip fallback installed into (emulated arch).
_PYSEL = (
    "if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi"
)
_PYTEST = '"$PY" -m pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors'
_BIN_EXC = (
    "--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' "
    "--exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' "
    "--exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' "
    "--exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' "
    "--exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' "
    "--exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc'"
)


def _apply(repo: str, patch: str) -> str:
    return (
        f"git -C /home/{repo} apply --whitespace=nowarn {_BIN_EXC} /home/{patch} "
        f"|| git -C /home/{repo} apply --whitespace=nowarn --3way {_BIN_EXC} /home/{patch} "
        f"|| ( cd /home/{repo} && patch -p1 --forward --fuzz=3 < /home/{patch} ) || true"
    )


class ImageBase(Image):
    """Shared per-era base: OS + toolchain + a FULL clone of the repo (all
    history, NO checkout, NO hardening). Built ONCE and reused by every PR in
    this era. The leading `# syntax=` directive makes DockerfileEnhancer return
    this Dockerfile verbatim (image.py: `if SYNTAX_DIRECTIVE in raw: return raw`)
    so the enhancer does NOT inject the ${BASE_COMMIT} hardening pass here — the
    base has no BASE_COMMIT and must keep full history so any PR's base.sha stays
    reachable. Per-PR checkout + hardening live in ImageDefault.
    """

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

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-py312-uv"

    def workdir(self) -> str:
        return "base-py312-uv"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """# syntax=docker/dockerfile:1.6
FROM python:3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential patch libsasl2-dev libldap2-dev libssl-dev

RUN pip install --upgrade pip && pip install uv

WORKDIR /home/
RUN git clone https://github.com/mealie-recipes/mealie.git /home/mealie
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
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                "ls -la\n###ACTION_DELIMITER###\n"
                "apt-get update && apt-get install -y libsasl2-dev libldap2-dev libssl-dev\n"
                "###ACTION_DELIMITER###\n"
                "pip install uv\n###ACTION_DELIMITER###\n"
                + _INSTALL
                + "\n###ACTION_DELIMITER###\n"
                + _PYSEL
                + "; "
                + _PYTEST,
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                f"cd /home/{repo}\n"
                + _PYSEL
                + "\n"
                + _PYTEST
                + "\n\n",
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                f"cd /home/{repo}\n"
                + _apply(repo, "test.patch")
                + "\n"
                # Re-sync deps: patches may add dependencies to pyproject/uv.lock
                # that aren't in the base.sha environment.
                + "( " + _INSTALL + " ) || true\n"
                + _PYSEL
                + "\n"
                + _PYTEST
                + "\n\n",
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                f"cd /home/{repo}\n"
                + _apply(repo, "test.patch")
                + "\n"
                + _apply(repo, "fix.patch")
                + "\n"
                # Re-sync deps: the fix patch frequently adds a new dependency
                # to pyproject/uv.lock (e.g. freezegun, httpx-curl-cffi) that is
                # absent from the base.sha environment; without this the fixed
                # code fails to import and no fail->pass is observed.
                + "( " + _INSTALL + " ) || true\n"
                + _PYSEL
                + "\n"
                + _PYTEST
                + "\n\n",
            ),
        ]

    def dockerfile(self) -> str:
        # Two-stage: chain to the shared ImageBase *Image*. Because dependency()
        # returns an Image (not a str), DockerfileEnhancer returns this verbatim
        # and supplies neither ARG BASE_COMMIT nor the hardening pass — so we set
        # BASE_COMMIT and embed Image._HARDENING_BLOCK ourselves. The base holds
        # a full clone; here we check out THIS PR's base.sha, install deps against
        # it, then the hardening block prunes every other ref/commit (reward-hack
        # defense). `hardening` is inserted as a plain value so its ${...}/$(...)
        # tokens stay byte-identical; literal Dockerfile braces are doubled.
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        base_sha = self.pr.base.sha
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{base_sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{repo}
RUN git checkout {base_sha}
RUN {_INSTALL}

{copy_commands}
{hardening}
CMD ["/bin/bash"]
"""


@Instance.register("mealie-recipes", "mealie_uv_py312")
class MEALIE_UV_PY312(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest `-rA` short test summary lines:
        #   PASSED tests/unit_tests/test_config.py::test_name[a b]
        #   FAILED tests/unit_tests/test_config.py::test_name - AssertionError: ...
        #   ERROR  tests/unit_tests/test_x.py::test_y - ...
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(.+?)\s*$", re.MULTILINE
        )
        for status, name in summary_pattern.findall(log):
            if status in ("FAILED", "ERROR"):
                name = re.sub(r"\s+-\s.*$", "", name).strip()
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name.strip())
            # XFAIL / XPASS: expected-fail bookkeeping, not real pass/fail

        # Grouped skip summary: SKIPPED [6] tests/unit_tests/test_x.py:18: reason
        for m in re.finditer(
            r"^SKIPPED\s+\[\d+\]\s+(\S+?):(\d+):", log, re.MULTILINE
        ):
            skipped_tests.add(f"{m.group(1)}:{m.group(2)}")

        # Defensive fallback: verbose per-test lines `nodeid STATUS [ 12%]`
        verbose_pattern = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$",
            re.MULTILINE,
        )
        for name, status in verbose_pattern.findall(log):
            name = name.strip()
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

# Route bundled PRs by their dash-joined `prs_in_bundle` interval to this era.
# Instance.create() looks up f"{org}/{number_interval}", so every bundle whose
# base.sha matches this era (uv era (uv.lock present) — python:3.12-bookworm + uv sync)
# must be registered here. Era was derived from the repo state at each base.sha
# (packaging files), not from PR-number ranges — routing is NOT monotonic in PR
# number (e.g. bundle 5883 is uv-era while the higher 6128/6268 are poetry-era).
# 11 bundle(s); intervals come from the lht dataset's prs_in_bundle.
_NUMBER_INTERVALS = [
    "5883-6110-6149-6169-6235-6342-6443-6640-6648-6649-6653-6655-6656-6657-6659-6660-6661-6662-6664-6665-6666-6667-6670-6671-6672-6675-6677-6678-6680-6683-6684-6685-6686-6687-6688-6689-6691-6693-6694-6696-6697-6698-6699-6701-6704-6705",
    "6405-6523-6577-6588-6602-6663-6781-6782-6797-6798-6806-6809-6815-6816-6825-6827-6830-6832-6833-6835-6839-6840-6843-6846-6849-6851-6852-6858-6859-6861-6862-6863-6864-6866-6867-6870-6872-6878-6879-6881-6885-6886-6888-6891-6895-6898-6901-6903-6905-6908-6909-6910-6911-6912-6913-6919-6922-6923-6924-6925-6926-6927-6929-6930-6932-6933-6934-6935-6936-6937-6938-6941-6942-6944-6945-6946-6948-6949-6952-6953-6954-6955-6956-6957-6958-6959-6960-6961-6962-6963-6964-6967-6969-6970-6972-6974-6976-6977-6978-6979-6980-6981-6982-6983-6984-6987-6988-6989-6990-6991",
    "6496-6505-6512-6569-6731-6737-6741-6744-6745-6748-6750-6752-6753-6754-6755-6756-6757-6758-6759-6760-6763-6765-6766-6767-6768-6771-6772-6773-6774-6776-6777-6778-6779-6783-6786-6787-6788-6789",
    "6513-6517-6536-6542-6547-6548-6552-6553-6554-6557-6558-6559-6561-6563-6565-6568-6573-6576-6581-6582-6589-6590-6591-6594-6595-6601-6603-6604-6605-6606-6607-6608-6610-6611-6612-6613-6614-6615-6616-6617-6618-6619-6620-6622-6624-6625-6626-6627-6628-6629-6631-6632-6637-6638",
    "6634-6700-6702-6706-6707-6708-6709-6710-6711-6712-6713-6716-6719-6722-6723-6724-6725-6726-6729-6733-6734-6736-6740-6742-6743",
    "6764-7121-7203-7205-7207-7208-7209-7211-7213-7214-7218-7220-7221-7223-7224-7225-7228-7230-7231-7232-7234-7235-7236-7237-7239-7240-7241-7242-7245-7246-7250-7252-7253-7254-7255-7256-7257-7258-7260-7262-7265-7266-7267-7268-7269-7270",
    "6803-7217-7271",
    "6857-6896-7015-7017-7086-7274-7276-7277-7278-7279-7280-7281-7282-7289-7292-7293-7294-7297-7298-7299-7304-7309-7310-7314-7315-7317-7318-7319-7321-7323-7325-7326-7327-7330-7332-7333-7334-7335-7336",
    "7010-7012-7013-7014-7016-7019-7020-7021-7022-7023-7024-7026-7028-7029-7030-7032-7033-7034-7039-7040-7041-7042-7043-7044-7047-7048-7049-7052-7053-7054-7055-7056-7057-7058-7059-7060-7061-7062-7063-7065-7066-7067-7070-7073-7075-7077-7080-7082-7083",
    "7076-7338-7340-7344-7345-7346-7349-7351-7356-7357-7359-7360-7362-7364-7365-7367-7369-7370-7371-7372-7373-7374-7375-7379-7380-7384-7386-7388-7389-7391-7392-7393-7394-7397-7398-7399-7400-7406-7407-7408-7410-7411-7412-7413-7414-7415-7418-7419-7421-7422-7425-7426-7427-7428-7430-7431-7432-7436-7438-7439-7440-7443-7444-7447-7448-7450-7452-7453-7454-7455-7459",
    "7088-7092-7093-7096-7098-7100-7101-7102-7104-7105-7106-7107-7109-7110-7111-7112-7113-7116-7119-7122-7126-7127-7128-7130-7131-7132-7134-7135-7136-7137-7138-7139-7140-7144-7145-7146-7148-7149-7150-7151-7152-7153-7155-7157-7160-7164-7165-7166-7168-7169-7170-7173-7174-7178-7180-7181-7182-7183-7186-7188-7189-7190-7191-7192-7193-7194-7195-7196-7198-7202-7204",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("mealie-recipes", _interval)(MEALIE_UV_PY312)
