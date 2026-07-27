import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIX = "github.com/rook/rook/"


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Each test emits a JSON event with
    `Action` (run/pass/fail/skip), `Package`, and `Test`. Names are kept
    package-qualified (`pkg/path::TestName`) since Go test function names
    recur across packages; subtests appear as `TestName/sub`."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw in log.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = _json.loads(raw)
        except Exception:
            continue
        test = ev.get("Test")
        action = ev.get("Action")
        pkg = ev.get("Package", "") or ""
        if not test or action not in ("pass", "fail", "skip"):
            continue
        if pkg.startswith(_REPO_PREFIX):
            pkg = pkg[len(_REPO_PREFIX):]
        name = f"{pkg}::{test}"
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    # Enforce TestResult disjoint-set invariant.
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


class RookEra3ImageBase(Image):
    """rook era 3 (PRs 14029-17289, v1.14->1.19): go.mod `go 1.21`-`1.25`.
    Pure-Go Kubernetes/Ceph operator — CGO disabled, `go test` unit suite
    under `pkg/` + `cmd/`. Built with Go 1.25 (>= every go.mod in this era); GOTOOLCHAIN=auto as safety net."""

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
        return "golang:1.25"

    def image_tag(self) -> str:
        return "base-go125"

    def workdir(self) -> str:
        return "base-go125"

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

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=auto
ENV GOFLAGS="-buildvcs=false -mod=mod"
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
    git jq curl ca-certificates && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class RookEra3ImageDefault(Image):
    """Per-PR image: checkout base commit, prefetch modules, run the targeted
    Go unit tests."""

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
        return RookEra3ImageBase(self.pr, self._config)

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
git reset --hard
git checkout {pr.base.sha}
timeout 600 go mod download || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export ROOK_UNIT_JQ_PATH="$(which jq)"
# Go package dirs the PR's test patch touches (pkg/ + cmd/ unit tests only;
# the tests/ tree holds integration tests that need a real cluster).
TEST_DIRS=$({{ grep -E '^diff --git a/(pkg|cmd)/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
MAIN=""
APIS=""
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then
        case "$d" in
            pkg/apis/*)
                if [ -f pkg/apis/go.mod ]; then APIS="$APIS ./${{d#pkg/apis/}}/"; else MAIN="$MAIN ./$d/"; fi ;;
            *) MAIN="$MAIN ./$d/" ;;
        esac
    fi
done
if [ -z "$MAIN" ] && [ -z "$APIS" ]; then echo "NO_BASELINE_TEST_DIRS"; exit 0; fi
RC=0
if [ -n "$MAIN" ]; then go test -json -count=1 $MAIN 2>&1 || RC=$?; fi
if [ -n "$APIS" ]; then (cd pkg/apis && go test -json -count=1 $APIS 2>&1) || RC=$?; fi
exit $RC
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export ROOK_UNIT_JQ_PATH="$(which jq)"
EXCLUDES=(--exclude='Documentation/*' --exclude='design/*' --exclude='*.png' \
    --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/(pkg|cmd)/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
MAIN=""
APIS=""
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then
        case "$d" in
            pkg/apis/*)
                if [ -f pkg/apis/go.mod ]; then APIS="$APIS ./${{d#pkg/apis/}}/"; else MAIN="$MAIN ./$d/"; fi ;;
            *) MAIN="$MAIN ./$d/" ;;
        esac
    fi
done
if [ -z "$MAIN" ] && [ -z "$APIS" ]; then echo "NO_TEST_DIRS"; exit 0; fi
RC=0
if [ -n "$MAIN" ]; then go test -json -count=1 $MAIN 2>&1 || RC=$?; fi
if [ -n "$APIS" ]; then (cd pkg/apis && go test -json -count=1 $APIS 2>&1) || RC=$?; fi
exit $RC
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export ROOK_UNIT_JQ_PATH="$(which jq)"
EXCLUDES=(--exclude='Documentation/*' --exclude='design/*' --exclude='*.png' \
    --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/(pkg|cmd)/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
MAIN=""
APIS=""
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then
        case "$d" in
            pkg/apis/*)
                if [ -f pkg/apis/go.mod ]; then APIS="$APIS ./${{d#pkg/apis/}}/"; else MAIN="$MAIN ./$d/"; fi ;;
            *) MAIN="$MAIN ./$d/" ;;
        esac
    fi
done
if [ -z "$MAIN" ] && [ -z "$APIS" ]; then echo "NO_TEST_DIRS"; exit 0; fi
RC=0
if [ -n "$MAIN" ]; then go test -json -count=1 $MAIN 2>&1 || RC=$?; fi
if [ -n "$APIS" ]; then (cd pkg/apis && go test -json -count=1 $APIS 2>&1) || RC=$?; fi
exit $RC
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

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{self.pr.repo}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=$BASE_COMMIT

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
"""


class ROOK_17289_TO_14029(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RookEra3ImageDefault(self.pr, self._config)

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
        return parse_go_test_log(log)


_BUNDLE_NIS_ERA3 = [
    "14029-14031-14032-14047-14053-14055-14061-14066-14068-14078-14081-14084",
    "14090-14091-14095-14097",
    "14105-14112-14113-14121-14122-14127-14130-14132-14146-14148-14149-14150-14153-14156-14157",
    "14164-14168-14170-14172-14176-14179-14195-14205-14207-14218-14224-14225-14226-14228",
    "14237-14239-14242-14252-14261-14268-14274-14277-14280-14284-14285",
    "14288-14301-14306-14310-14315-14321-14327-14329-14333-14335-14336",
    "14340-14348-14359-14360-14369",
    "14448-14450-14452-14463-14490-14492-14493-14494-14498",
    "14504-14571-14581-14609-14614",
    "14619-14628-14633-14635-14639-14640-14657-14658-14661-14664-14669-14682-14687",
    "14632-14634-14638-14668-14739-14741-14743",
    "14683-14691-14692-14696-14706-14707-14711-14712-14721-14734-14735-14740-14742-14744",
    "14745-14749-14759-14762-14768-14785-14787-14788-14794-14796-14799-14802-14803",
    "14806-14839-14868-14907-14911-14973-14975",
    "14807-14814-14828-14840-14851-14856-14857-14863-14866-14867",
    "14871-14873-14876-14879-14885-14887-14892-14904-14905-14908-14912-14924-14927-14929-14931-14940-14946-14948-14950-14953-14960-14961-14963-14967-14968-14974",
    "15042-15046-15059-15069-15089-15092-15095-15117-15144-15171-15181-15193",
    "15198-15202-15212-15213-15216-15229-15230",
    "15237-15247-15249-15252-15267-15268-15278-15280-15285-15287-15290",
    "15312-15314-15318-15320-15321-15343-15345-15365-15366",
    "15396-15417-15438-15463-15465-15468-15487-15491-15507",
    "15397-15413-15414-15416-15418-15419-15434-15436-15437-15439",
    "15444-15452-15453-15462-15464-15466-15467-15475-15476-15478-15488-15489-15492-15494-15504-15506-15508",
    "15516-15533-15581-15582-15584-15586-15587-15589-15590-15595",
    "15626-15654-15663-15667-15673-15679-15684-15691-15707-15720-15722-15726-15728-15730-15735-15737",
    "15748-15757-15760-15765-15770-15771-15772-15773",
    "15756-15774-15786-15802-15810-15826-15939-15943-15990-15993",
    "15787-15799-15800-15803-15806-15807-15811-15812-15816-15827-15828-15831-15832-15833-15834-15836",
    "15843-15860-15864-15865-15874-15877-15880-15881-15885-15900-15901-15910-15911-15919-15920",
    "15921-15927-15932-15940-15941-15944-15959",
    "15964-15965-15968-15975-15976-15977-15982-15991-15998-16001-16006-16012-16018-16019-16021-16022-16028-16034-16036",
    "16046-16051-16053-16065-16066-16084-16100-16107-16116-16117-16118-16120",
    "16054-16083-16121-16122-16276-16323-16324-16325-16327",
    "16136-16137-16138-16139-16145-16147-16148-16160-16165-16180-16187-16191-16193-16204-16207-16212",
    "16213-16221-16234-16240-16262-16264-16266-16314-16316-16333-16357-16359",
    "16380-16394-16397-16404-16407-16410-16411-16412-16413",
    "16398-16422-16423-16424-16429-16444-16445-16450-16455-16459-16460-16461-16463-16464",
    "16458-16502-16534-16633-16634-16686-16703-16716",
    "16465-16475-16476-16477-16487-16490-16491-16495-16498-16500-16503-16509-16510-16519-16525-16527-16529-16533-16539-16544-16545-16546-16547-16562-16564-16565-16566",
    "16579-16586-16587-16590-16604-16605-16606-16608-16611-16612-16621-16630-16631-16632-16635",
    "16666-16671-16676-16682-16683-16684-16687-16701-16704-16709-16717",
    "16708-16714-16732-16733-16738-16740-16749-16752-16785",
    "16796-16800-16814-16820-16832-16856-16859-16863-16893-16902-16906-16909-16915",
    "16917-16980-16986-16999-17007-17011-17027-17035-17053-17063-17124-17180-17182-17241",
    "16942-16945-16955-16970-16971-16981-16987-17000-17003-17008-17012-17021-17026-17028-17031-17032-17033-17036",
    "17040-17042-17054-17055-17061-17062-17066-17069-17086-17088-17100-17106-17107-17109",
    "17115-17118-17125-17126-17127-17128-17138-17139-17142-17156-17158-17159-17162-17163-17178-17179-17181-17186-17203-17211-17219-17242-17243",
    "17247-17251-17252-17277-17291-17292-17297-17299-17325-17326-17327-17329-17330-17353-17355",
    "17289-17360-17367-17368-17375-17378-17390-17392-17396-17424-17436-17449",
]

for _ni in _BUNDLE_NIS_ERA3:
    Instance._registry[f"rook/{_ni}"] = ROOK_17289_TO_14029
