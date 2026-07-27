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


class RookEra2ImageBase(Image):
    """rook era 2 (PRs 10055-14267, v1.9->1.14; includes PR 10055 whose base go.mod is 1.16
    but whose fix pulls deps with go1.17+ `//go:build`-only tags): go.mod `go 1.16`-`1.20`.
    Pure-Go Kubernetes/Ceph operator — CGO disabled, `go test` unit suite
    under `pkg/` + `cmd/`. Built with Go 1.20 (>= every go.mod in this era)."""

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
        return "golang:1.20"

    def image_tag(self) -> str:
        return "base-go120"

    def workdir(self) -> str:
        return "base-go120"

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


class RookEra2ImageDefault(Image):
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
        return RookEra2ImageBase(self.pr, self._config)

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


class ROOK_14267_TO_10128(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RookEra2ImageDefault(self.pr, self._config)

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


_BUNDLE_NIS_ERA2 = [
    "10055-10063-10069-10077-10079-10080-10081-10087-10092-10098-10101-10102-10103-10104-10106-10112-10113-10114-10117-10119-10120-10122-10130",
    "10128-10138-10143-10144-10146-10151-10155-10159-10168-10170-10175-10176-10179-10184-10185-10186",
    "10197-10200-10201-10205-10206-10218-10221-10229-10236-10241-10248-10255-10257-10258-10260-10261-10262-10263",
    "10357-10359-10367-10368-10369-10378-10379-10384-10387-10388-10392-10393-10401-10407-10409-10417-10418-10421-10422-10426-10430",
    "10436-10439-10453-10457-10461-10462-10469-10471-10475-10479-10488-10497-10498-10500",
    "10512-10517-10524-10527-10549-10550-10553-10554-10564-10565",
    "10560-10571-10584-10586-10587-10588-10592-10595-10599-10601-10606-10608-10611-10613-10616-10632-10633-10634-10637",
    "10643-10644-10661-10662-10663-10666-10667-10669-10670-10690-10704-10705-10707",
    "10680-10711-10727-10740-10754-10756-10760-10785-10801-10818-10823-10825",
    "10793-10831-10838-10845-10864-10866-10876-10879-10883-10885-10916-10918-10920-10922-10940-10959-10976-10978-10981-10996-10998-11000-11002-11019-11061-11065",
    "10862-10863-10865-10867-10868-10870-10873-10878-10880-10884-10886-10897-10901-10902-10913-10915-10917-10919-10921-10923-10938-10945-10946",
    "10949-10952-10955-10957-10960-10961-10970-10973-10974-10977-10979-10980-10982-10985-10987-10988-10997-10999-11001-11003-11010-11020-11021-11023-11033-11041-11057-11062-11063-11066",
    "11072-11073-11075-11077-11086-11087-11088-11089-11094-11098-11102-11105-11108-11113-11116-11119",
    "11106-11107-11112-11118",
    "11117-11134-11139-11140-11146-11160-11164-11167-11168-11181-11185-11189-11190-11191-11192-11193-11194",
    "11133-11239-11249-11266",
    "11144-11199-11207-11214-11218-11234-11240-11250-11257-11260-11261-11264-11267",
    "11270-11271-11279-11285-11287-11288-11292-11294-11295-11300-11310-11311-11319-11325-11327-11328",
    "11343-11348-11359-11362-11363-11371-11375-11392-11396",
    "11410-11412-11420-11425-11426-11441-11443-11444-11445-11446-11447-11449-11457-11458-11459-11462-11469-11471-11472",
    "11494-11495-11498-11508-11513-11514-11520-11533-11534-11535",
    "11548-11550-11551-11557-11558-11559",
    "11561-11580-11581-11582-11604-11605-11608-11615-11622-11633-11635-11636-11648-11651-11652-11655",
    "11664-11669-11683-11684-11708-11732-11751-11773-11780",
    "11782-11793-11809-11811-11815-11837-11892-11920-11999-12032",
    "11794-11807-11810-11812-11813-11816-11824-11830-11838-11841-11843-11847-11849-11850",
    "11856-11857-11868-11876-11877-11879-11893-11897-11909-11912-11913-11921-11922-11923-11939-11944-11952-11953-11955",
    "11959-11969-11971-12000-12001-12009-12013-12014-12015-12016-12026-12029-12031-12039-12040",
    "12041-12043-12044-12047-12058-12060-12067-12071-12074-12080-12081-12084-12085-12088",
    "12093-12119-12128-12134-12136-12141-12150-12158-12167-12168-12169-12171-12177",
    "12135-12178-12183-12184-12188-12189-12191-12196-12197-12198-12199-12209-12210-12221-12222-12226-12239-12240-12241",
    "12245-12246-12248-12258-12269-12281-12283-12298-12301-12303-12304-12307-12308-12313-12315-12317",
    "12319-12338-12339-12345-12346-12348-12355-12377-12382-12385-12386-12387-12391",
    "12390-12407-12408-12410-12411-12414-12444-12447-12452-12453-12454",
    "12553-12581-12668-12684-12701-12826-12891-12907",
    "12554-12582-12584-12586-12587-12588-12589-12590-12594-12601-12602-12604-12606-12619-12620-12626-12628-12629-12630-12632-12635-12641-12642",
    "12648-12649-12657-12658-12662-12667-12669-12685-12686-12690-12692-12696-12699-12705-12707-12727-12728",
    "12729-12737-12738-12739-12740-12742-12749-12750-12752-12753-12755-12762-12769-12777-12779-12792-12793-12794-12795-12796-12805-12806-12807-12810-12823-12827-12828",
    "12830-12831-12835-12836-12851-12853-12854-12863-12864-12866-12873-12892-12893-12908",
    "12906-12910-12911-12912-12927-12928-12936-12939-12942-12960-12961-12964-12965-12966-12975-12980-12982-12990-12994-12995-12999-13000-13003-13004-13005-13006-13013-13014",
    "13023-13030-13034-13035-13038-13042-13043-13044-13051-13054-13062-13064-13067",
    "13081-13086-13090-13093-13095-13098-13103-13104-13106-13107-13108-13111-13112",
    "13116-13124-13131-13132-13134-13147-13153-13164-13181-13183-13186-13188-13189-13197-13198-13208-13223",
    "13224-13232-13242-13254-13260-13275-13276-13279-13299-13300-13301",
    "13322-13333-13378-13391-13392",
    "13395-13404-13407-13413-13426-13427-13431-13432-13444-13445-13446-13447-13448-13455-13456",
    "13412-13514-13518-13606-13627",
    "13473-13503-13505-13515-13517-13533-13538-13543-13562-13563",
    "13576-13583-13587-13596-13607-13608-13612-13624-13628",
    "13629-13640-13647-13649-13650-13660-13661-13668-13669-13671-13682-13688-13689-13691-13696-13706-13709-13710-13713-13721-13729-13731-13732",
    "13733-13742-13762-13764-13767-13771-13783-13791-13798-13806-13813",
    "13797-13822-13834-13841-13847-13862-13866-13868-13879-13887-13894-13895-13897",
    "13920-13924-13935-13937",
    "14028-14065-14104-14163-14177-14194-14206-14217-14227",
    "14267-14377-14390-14410-14416",
]

for _ni in _BUNDLE_NIS_ERA2:
    Instance._registry[f"rook/{_ni}"] = ROOK_14267_TO_10128
