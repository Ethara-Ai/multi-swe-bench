import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks from a unified diff so `git apply` never aborts
    on a binary hunk with no full-index line (e.g. `docs/.DS_Store`, images).
    Safe: binary hunks touch no Python source and never affect test outcomes."""
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )



def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v -rA output. Verbose result lines look like:

        tests/test_var.py::test_fstring_roundtrip PASSED [ 12%]

    Test names are kept as full pytest node ids (`path::test`)."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Anchor on the trailing "<STATUS> [ NN%]" so node ids containing spaces
    # (parametrized tests, e.g. `test_x[append then pop]`) are captured whole.
    re_line = re.compile(
        r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
    )

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).strip()
        m = re_line.match(line)
        if not m:
            continue
        nodeid, status = m.group(1).strip(), m.group(2)
        if status in ("PASSED", "XPASS"):
            passed_tests.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(nodeid)
        else:  # SKIPPED, XFAIL
            skipped_tests.add(nodeid)

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


class ReflexEra1ImageBase(Image):
    """reflex era 1 — the Pynecone era (PRs 16-1221, v0.1->0.2): package dir
    `pynecone/`, deps via poetry, pytest unit suite under `tests/`. Python 3.8
    (satisfies the era's `^3.7` constraint; 3.7 images are EOL)."""

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
        return "python:3.8-slim"

    def image_tag(self) -> str:
        return "base-era1"

    def workdir(self) -> str:
        return "base-era1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV http_proxy=${{http_proxy}}
ENV https_proxy=${{https_proxy}}
ENV HTTP_PROXY=${{HTTP_PROXY}}
ENV HTTPS_PROXY=${{HTTPS_PROXY}}
ENV no_proxy=${{no_proxy}}
ENV NO_PROXY=${{NO_PROXY}}
ENV SSL_CERT_FILE=${{CA_CERT_PATH}}
ENV REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}}
ENV CURL_CA_BUNDLE=${{CA_CERT_PATH}}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential curl ca-certificates && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ReflexEra1ImageDefault(Image):
    """Per-PR image: checkout base commit, `poetry install`, run the targeted
    pytest unit tests."""

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
        return ReflexEra1ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
pip install --no-cache-dir "poetry<2" || true
poetry config virtualenvs.create false || true
poetry install --no-interaction || pip install --no-cache-dir -e . || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# pytest file paths the PR's test patch touches (tests/ unit suite only;
# top-level integration/ harness tests need a browser and are skipped).
TEST_FILES=$({{ grep -E '^diff --git a/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff*' --exclude='*.svg' \
    --exclude='*.lockb' --exclude='*.webp' --exclude='*.mp3')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/(pyproject\\.toml|poetry\\.lock)' /home/test.patch 2>/dev/null; then
    poetry install --no-interaction || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff*' --exclude='*.svg' \
    --exclude='*.lockb' --exclude='*.webp' --exclude='*.mp3')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(pyproject\\.toml|poetry\\.lock)' /home/test.patch /home/fix.patch 2>/dev/null; then
    poetry install --no-interaction || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
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

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("reflex-dev", "reflex_1221_to_16")
class REFLEX_1221_TO_16(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ReflexEra1ImageDefault(self.pr, self._config)

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
        return parse_pytest_log(log)


# ---------------------------------------------------------------------------
# number_interval bundle routing (prs_in_bundle dash-joined)  -- PIPELINE 11b
# ---------------------------------------------------------------------------
# Raw dataset leaves number_interval empty; delivery sets it to
# "-".join(prs_in_bundle). Register REFLEX_1221_TO_16 (this era) under every bundle key so
# delivered records resolve to pubkey/<bundle>. Original era-key registration
# above is kept.
_BUNDLE_NIS_REFLEX_ERA1 = [
    "16-17-18-22-25-29-30-31-32-34-35-36-38-40-41",
    "45-47-50-52-59-69-71-72-75-76-80-84-85-87-91-92-96-98-99-106-107-108-109-117-118",
    "119-143-145-148-150-152-153-155-159-163-164-171-172-173-174-179-182-184-185",
    "192-193-203-205-206-207-211-214-222-223-228-229-231-241-243-244-245-246-248-249",
    "250-252-260-261-265-269-272-274-275-286-289-290",
    "305-309-311-312-313-316-318-323-324-329-332-335-337-338-339-340-343-348-352-363-366-368-370-375-377-380-381-382-384-386-387-388-389-390-391-392-393-396-399-404-407",
    "359-402-413-417-434-437-440-449-450-451-452-453-455-465-469-473-474-479-482-484-494",
    "485-493-498-499-500-501-502-503-504-513-516-517-519-520-521-523-524-525-527-530-536",
    "538-543-544-545-547-550-552-553-562-565-577-580",
    "570-571-583-588-589-591-593-594-596-601-614-615-616-617-618-622-624-625-626-629-630",
    "576-636-638-639-641-642-643-650-653-654-655-657-659-664-668",
    "627-763-766-768-769-770-771-773-775-788-790",
    "666-670-677-685-691-702-703-712-732-735-738-742-744-745-750-755-758-761-762",
    "700-792-800-808-810-811-816-824-828-832-840-843-846-848-849-850-851-854-856-859-860-861-864-866-871-872-873",
    "718-1092-1093-1095-1096-1098-1102-1104-1107-1109-1111-1112-1123-1124-1126-1128-1130-1131-1136-1138-1142-1145-1146-1148-1153-1156-1157-1158-1161-1163-1166-1168-1171-1172-1173-1175-1176-1177-1178",
    "835-898-899-919-928-930",
    "852-902-915-929-953-963-979-983-984-986-990-991-993-1000-1001-1002-1004-1005-1007-1010-1011-1013-1019-1022-1031-1033-1036-1038",
    "880-881-885-886-887-888-889-890-891-894-895",
    "917-926-935-938-941-949-950-951-952-957-959-960-961-965-970-972",
    "1029-1030-1043-1046-1051-1052-1053-1054-1058-1063-1068-1070-1073-1075-1078-1079-1080-1081-1082-1085-1089",
    "1032-1041-1042",
    "1116-1132-1155-1189-1193-1198-1199-1200-1203-1206-1207-1215-1216-1217-1219",
    "1221-1222-1223-1225-1227-1230-1232-1234-1236-1246-1249-1250-1252-1258-1262-1265-1268-1270-1271-1272-1278-1281-1284-1286-1289-1292-1299-1301-1305-1308",
]
for _ni in _BUNDLE_NIS_REFLEX_ERA1:
    Instance.register("reflex-dev", _ni)(REFLEX_1221_TO_16)
