import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v output anchored on the trailing `<STATUS> [ NN%]` so
    parametrized node ids with internal spaces are captured whole."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

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
        else:
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


class ConanV2ImageBase(Image):
    """conan era 2 — v2.x (PRs 13141-19706, releases 2.0->2.28, 2023+).
    Package dir `conan/` + `conans/` shim; tests live under top-level
    `test/{integration,functional,unittests}/` plus legacy `conans/test/...`.
    The grep matches any path under `test/` or `conans/test/`. Built with
    Python 3.10."""

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
        return "python:3.10-slim"

    def image_tag(self) -> str:
        return "base-v2"

    def workdir(self) -> str:
        return "base-v2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{DockerfileEnhancer._PROXY_ARGS}

{self.global_env}

{DockerfileEnhancer._ENV_BLOCK}

ENV LC_ALL=C.UTF-8 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential curl ca-certificates && rm -rf /var/lib/apt/lists/*

{DockerfileEnhancer._CERT_SYMLINKS}

# Ensure pytest is always present even if `pip install -e .[test]` falls back
# to `-e .` (which lacks test extras in some PR commits).
RUN pip install --no-cache-dir pytest PyJWT

{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ConanV2ImageDefault(Image):
    """Per-PR image: checkout base commit, install conan in editable mode
    (with test extras), run the targeted pytest unit tests."""

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
        return ConanV2ImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e
cd /home/{pr.repo}
pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# pytest file paths the PR's test patch touches. v2 layout uses
# test/integration/, test/functional/, test/unittests/ plus legacy
# conans/test/... — grep matches any path under test/ or conans/test/.
# Exclude __init__/helpers/conftest — they only get touched as fixtures.
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
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
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/(setup\\.py|pyproject\\.toml|requirements)' /home/test.patch 2>/dev/null; then
    pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
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
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(setup\\.py|pyproject\\.toml|requirements)' /home/test.patch /home/fix.patch 2>/dev/null; then
    pip install --no-cache-dir -e .[test] || pip install --no-cache-dir -e . || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/(conans/test|test)/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py|/conftest\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""


@Instance.register("conan-io", "13141-13173-13186-13390-13421-13529-13919-13930-13985-14054-14133-14289-14323-14674-14752-14780-15047-15262-15382-15413-15588-15682-16322-16415-16417-16443-16594-17250-17374-17398-17405-17647-17656-17714-17793-17800-17848-17898-18266-18316-18396-18496-19286-19345-19706")
class CONAN_19706_TO_13141(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ConanV2ImageDefault(self.pr, self._config)

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_CONAN_19706_TO_13141 = [
    "16415-16420-16596-16597-16602-16605-16607-16608-16610-16611-16613-16615-16616-16617-16619-16620-16633-16637-16642-16647-16648-16657-16680-16683-16684-16685-16700-16709-16715-16720-16723-16724-16743-16752-16753",
    "15588-16224-16228-16235-16243-16244-16246-16252",
    "15262-15287-15320-15323-15356-15357-15373-15409-15410-15418-15432-15436",
    "14752-14759-14760-14781-14782-14787-14795-14797-14800-14804-14813-14819-14825",
    "14674-14694-14871-15109-15116-15121-15126-15127-15135-15136-15137-15140-15149-15150-15151-15153-15170-15172-15174-15177-15180-15185-15186-15188-15192-15194-15196-15204-15207-15211-15212-15214-15215-15221-15222-15233-15234-15244-15250-15251-15253-15255-15257-15258-15260-15263-15266-15272-15274-15281-15282-15284-15285-15288-15289-15294-15296-15297-15299-15310",
    "14323-14363-14532-14533-14578-14605-14608-14616-14621-14622-14627-14633-14642-14643-14644-14645-14650-14654-14655-14660-14662-14664-14667-14668-14673-14675-14676-14679-14682-14692-14709-14711-14712-14713-14716-14721-14728-14731-14735-14737-14740-14743-14756",
    "14289-14295-14296-14302-14306-14309-14310-14320-14322-14325-14330-14331",
    "13985-14113-14140-14152-14157-14161-14164-14167-14168-14169-14176-14177-14185-14187-14190-14194-14195-14199-14215-14223-14227-14231-14233-14238-14250-14252-14253-14254-14255-14261-14264-14267-14272",
    "13930-15630-15653-15658-15683-15695-15697-15699-15704-15705-15706-15707-15710-15713-15717-15724-15727-15731-15736-15737-15743-15747-15748-15756-15762-15763-15767-15771-15775-15779-15781-15788-15789-15790-15794-15802-15804-15806-15807-15808-15812-15813-15815-15823-15825-15829-15830-15832-15834-15835-15836-15838-15842-15845-15846-15853-15859-15863-15869-15870-15876-15877-15881-15885-15888-15890-15896-15903",
    "13919-13923-13926-13929-13934-13937-13940-13943-13944-13953-13964-13966-13967",
    "13421-13450-13458-13459-13461-13463-13467-13468-13470-13477-13487-13488-13496-13502-13505-13516-13522-13526-13531-13542-13544-13557-13561-13564-13571-13574-13581-13596",
    "13390-13391-13456-13509-13585-13601-13605-13607-13608-13610-13612-13618-13622-13626-13631-13657-13661-13662-13667-13668-13669-13671",
    "13186-13191-13207-13211-13214-13226-13248-13249-13263-13264-13266-13267-13269-13270-13277-13278-13282-13284-13285-13287-13288-13292-13293-13297-13298-13299-13301",
    "19345-19438-19474-19522-19530-19535-19538-19539-19544-19554-19560-19561-19571-19574-19577-19578-19582-19584-19587-19591-19592-19594-19599-19602-19604-19610-19611-19612-19623-19625-19628-19629-19635-19642-19643-19645-19646-19647-19657-19660-19662-19666-19669-19670",
    "18496-18534-18566-18571-18572-18575-18576-18578-18587-18588-18595-18600-18603-18611-18612-18616-18621-18628-18631-18642-18644-18646-18649-18655-18661-18662-18663-18671-18676-18679-18680",
    "18396-18792-18972-19171-19182-19185-19187-19192-19193-19194-19197-19198-19200-19204-19206-19222-19223-19226-19229-19230-19232-19234-19239-19240-19242-19244-19245-19250-19257-19260-19262-19264-19265-19268-19269-19289-19291-19294-19295-19296-19301-19307",
    "18316-18819-18830-18947-18995-19012-19013-19014-19021-19023-19024-19030-19031-19034-19035-19038-19040-19041-19043-19047-19051-19052-19068-19074-19075-19077-19078-19080-19091-19094-19096-19099-19103-19105-19106-19111-19115-19126-19128-19130-19132-19133-19134-19137-19148",
    "17898-18247-18350-18377-18383-18385-18390-18394-18399-18402-18407-18410-18411-18418-18419-18422-18423-18424-18427-18428-18429-18430-18432-18435-18437-18439-18440-18442-18444-18447-18449-18452-18456-18461-18463-18464-18471-18472-18474-18477-18485-18489-18493-18495-18498-18503-18515-18518-18520-18533-18538-18541-18544-18548-18552-18557-18558-18559-18561-18562",
    "17800-17925-17927-17943-17945-17954-17957-17961-17964-17967-17968-17970-17975-17984-17985-17986-17998-18009-18012-18014-18015-18018-18028-18037-18038-18040",
    "17793-18897-19319-19324-19327-19328-19330-19331-19338-19339-19343-19344-19348-19349-19351-19356-19357-19360-19367-19370-19375-19376-19382",
    "17714-19337-19368-19383-19388-19392-19393-19394-19396-19398-19402-19403-19406-19408-19410-19415-19417-19421-19428-19429-19435-19437-19441-19442-19444-19446-19451-19457-19468-19470-19471-19475-19478-19479-19483-19485-19487-19494-19496-19497-19498-19499-19501-19506-19510-19515-19519-19520",
    "17647-17649",
    "17405-17408-17411-17416-17421-17430-17432-17442-17444-17445-17450-17453-17470-17473-17479-17482-17487-17494-17498-17500-17503",
    "17398-17459-17481-17542-17577-17642-17643-17654-17655-17658-17659-17668-17670-17675-17684-17688-17692-17695-17696-17701-17704-17708-17715-17722-17724-17727-17735-17742-17745-17751-17758-17765-17770-17771-17781-17783-17794-17796-17801-17803-17809-17812-17819-17821-17824-17829-17831-17834-17838-17839-17843-17845",
    "15047-19251-19639-19691-19693-19694-19703-19704-19709-19720-19724-19725-19727-19728-19731-19735-19737-19739-19740-19744-19746-19750-19755-19758-19760-19766-19774-19778-19779-19780-19781-19785",
]

for _ni in _BUNDLE_NIS_CONAN_19706_TO_13141:
    Instance.register("conan-io", _ni)(CONAN_19706_TO_13141)


# ---------------------------------------------------------------------------
# conan-io/conan number_interval routing -- REGISTRY-SCOPED shim.
#
# Placed at the end of this era file (imported last by the package __init__.py)
# so it can install a patched Instance.create classmethod that resolves targets
# via `cls._registry` at CALL time. By then every conan era class is registered,
# so the shim sees the full registry regardless of where it sits.
#
# Why it's needed: conan is registered ONLY under number_interval keys
# (e.g. `conan-io/13141-...-19706` and the per-bundle dash-lists). There is NO
# plain `conan-io/conan` registration. In `gen_report --mode dataset`, report
# tasks are collected BEFORE the raw dataset is loaded, so each task's
# number_interval is empty and Instance.create is called with the plain
# `conan-io/conan` key, which is unregistered -> every report fails.
#
# The fallback dispatches on the PR NUMBER: it finds the registered conan era
# class whose bundle interval has this PR as its primary (first) element -- the
# exact class that owns the instance -- and routes to it. Other repos are
# unaffected: the fallback re-raises for any non-conan org/repo.
# ---------------------------------------------------------------------------
from multi_swe_bench.harness.instance import Instance as _ConanInstance  # noqa: E402

_CONAN_ORG = "conan-io"
_CONAN_REPO = "conan"


def _conan_build_number_routing(registry: dict):
    """Map PR number -> era class from the live registry.

    ``primary`` maps a bundle's first (target) PR to its class -- the precise,
    conflict-free routing. ``contains`` maps every PR listed in any bundle as a
    fallback for non-primary numbers; on the rare cross-era duplicate the first
    registration wins (primary routing already covers all real instances).
    """
    primary: dict[int, object] = {}
    contains: dict[int, object] = {}
    for key, cls in registry.items():
        if not key.startswith(f"{_CONAN_ORG}/"):
            continue
        interval = key.split("/", 1)[1]
        parts = [p for p in interval.split("-") if p.isdigit()]
        if not parts:
            continue
        primary.setdefault(int(parts[0]), cls)
        for p in parts:
            contains.setdefault(int(p), cls)
    return primary, contains


if not getattr(_ConanInstance, "_conan_route_shim", False):
    _conan_orig_create = _ConanInstance.create.__func__

    def _conan_create(cls, pr, config, *args, **kwargs):
        try:
            return _conan_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _CONAN_ORG
                and getattr(pr, "repo", "") == _CONAN_REPO
            ):
                primary, contains = _conan_build_number_routing(cls._registry)
                target = primary.get(pr.number) or contains.get(pr.number)
                if target is not None:
                    return target(pr, config, *args, **kwargs)
            raise

    _ConanInstance.create = classmethod(_conan_create)
    _ConanInstance._conan_route_shim = True
