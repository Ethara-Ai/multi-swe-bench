import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class CoderImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    # PR-number boundary between the two gvisor eras. Every record in the
    # dataset with number <= 12150 pins an old `github.com/coder/gvisor` fork
    # (2022-12 or 2023-05) and go.mod `go 1.20/1.21`; every record with
    # number >= 13973 uses the 2024-05 fork and go.mod `go 1.22`..`1.25`.
    # 13000 sits cleanly inside the gap.
    _GVISOR_ERA_BOUNDARY = 13000

    def dependency(self) -> Union[str, "Image"]:
        # The `github.com/coder/gvisor` fork reaches Go-runtime internals via
        # //go:linkname (goready/gopark/semacquire/...). Those symbols only
        # resolve against the exact runtime era the fork was written for:
        #   * 2022/2023 forks compile only on Go 1.20/1.21 -- they already fail
        #     to link on Go 1.22 (verified: "undefined: goready"). go.mod pins
        #     go 1.20/1.21, so golang:1.20 + GOTOOLCHAIN=auto stays on 1.20 for
        #     the go-1.20 records and upgrades to 1.21 for the go-1.21 records.
        #   * The 2024 fork carries the linkname push directives and compiles on
        #     modern Go (proven by the go-1.25 records that depend on it), so
        #     golang:1.25 + GOTOOLCHAIN=auto fetches each record's exact
        #     toolchain (1.22..1.25.x).
        if self.pr.number < self._GVISOR_ERA_BOUNDARY:
            return "golang:1.20-bookworm"
        return "golang:1.25-bookworm"

    def _era_tag(self) -> str:
        # Shared base, one image per gvisor era (NOT per PR). The base only
        # clones the repo at default HEAD with full history; the per-PR checkout
        # and anti-cheat hardening happen in the PR layer (CoderImageDefault),
        # so a single base serves every PR in its era and we clone coder twice
        # instead of 76 times.
        if self.pr.number < self._GVISOR_ERA_BOUNDARY:
            return "base-go120-7134_to_12150"
        return "base-go125-13973_to_25269"

    def image_tag(self) -> str:
        return self._era_tag()

    def workdir(self) -> str:
        return self._era_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        org = self.pr.org

        # Reference-base format, shared per era. Clone only -- full history is
        # kept so any PR's base.sha is reachable; the PR layer does the checkout
        # and history-strip. The `# syntax` directive opts out of
        # DockerfileEnhancer so this hand-written layout is used verbatim (and
        # the enhancer does NOT inject a `checkout ${BASE_COMMIT}` that would
        # pin this shared base to a single commit). BASE_COMMIT is declared for
        # reference-format compliance but intentionally unused at this layer.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=auto

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class CoderImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return CoderImageBase(self.pr, self._config)

    def image_tag(self) -> str:
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

git config --global --add safe.directory '*'
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Pre-fetch module dependencies so the eval run is offline-friendly.
# `|| true` because some early-era PRs reference replace-directives whose
# target dirs are absent from a shallow checkout; the actual test run will
# surface any genuine resolution failure.
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the coder/coder run/test/fix scripts.
#
# coder/coder has 200+ Go packages plus a TypeScript frontend, Helm charts,
# and docs. Running the whole `go test ./...` is wasteful and would pull in
# tests that require postgres/k8s/AWS, so tests are scoped to the Go
# packages touched by the patches (same idea as the eksctl config).
#
# Non-Go directories (site/, helm/, docs/, dogfood/, offlinedocs/, examples/)
# are filtered out since they contain no `go test` targets.

EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --3way $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --reject $EXCLUDES "$f" \\
    || true
}

# Print the unique Go package directories touched by test.patch + fix.patch
# that exist on disk and are not part of the frontend/docs/helm trees.
# Written to be safe under `set -eo pipefail` (a no-match grep must not abort).
collect_pkgs() {
  local out d
  out=$(
    {
      git apply --numstat --whitespace=nowarn /home/test.patch 2>/dev/null
      git apply --numstat --whitespace=nowarn /home/fix.patch 2>/dev/null
    } \\
      | awk -F'\\t' '{print $NF}' \\
      | grep -E '\\.go$' \\
      | sed -E 's#/[^/]+$##' \\
      | grep -vE '^(site|helm|docs|offlinedocs|dogfood|examples|scripts)(/|$)' \\
      | sort -u
  ) || true
  for d in $out; do
    if [ -n "$d" ] && [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then
      echo "./$d"
    fi
  done
}

run_go_tests() {
  local pkgs
  pkgs=$(collect_pkgs)
  if [ -z "$pkgs" ]; then
    echo "No Go test packages touched by the patches; nothing to run."
    return 0
  fi
  echo "=== Running go test on touched packages ==="
  echo "$pkgs"
  echo "==========================================="
  go test -v -count=1 -short -timeout=1200s $pkgs
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch
run_go_tests

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

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history). prepare.sh has already checked out this PR's base.sha, so we
        # bake the canonical hardening block with the literal sha: detach at
        # base.sha, strip all other refs/reflog, gc --prune, and assert
        # rev-list --all == HEAD so future commits are unreachable (git
        # log/show of later commits fails -> no patch leakage). The `# syntax`
        # directive keeps DockerfileEnhancer from rewriting this layout; it is a
        # no-op for the PR image anyway (its dependency is an Image, not a str).
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register("coder", "coder")
class Coder(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CoderImageDefault(self.pr, self._config)

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
        # `go test` output is not colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        # A package summary line ("ok   <import-path>", "FAIL <import-path>",
        # "?    <import-path>") closes the block of tests printed above it.
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        # Tests are buffered per package so the package import path can be
        # prepended -- this keeps names globally unique when several packages
        # are tested in one `go test` invocation.
        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            pass_match = re_pass.match(line)
            if pass_match:
                pending_pass.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                pending_fail.add(fail_match.group(1))
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                pending_skip.add(skip_match.group(1))
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        # Flush tests not followed by a summary line (e.g. truncated/timed-out
        # log) so they are still counted.
        flush("unknown")

        # Enforce TestResult disjointness invariants: a test reported as both
        # passed and failed (e.g. flaky retry) counts as failed.
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# JSONL + registry ship together to the trajectory team; Instance.create()
# resolves name = "coder/{number_interval}", so every dash-joined bundle value
# must be a registered routing key -> Coder. Bundle-level (one key per bundle,
# #keys == #instances). The era (golang:1.20 vs 1.25) is chosen inside
# CoderImageBase by the lead PR number, so all keys route to the single Coder
# class. Keys are data-derived from the dataset -- regenerate if bundles change.
_BUNDLE_NIS_CODER = [
    "11638-11665-11681-11723-11724-11726-11728-11733-11744-11747",
    "13973-13994",
    "15098-15219-15223",
    "16790-16832",
    "16793-16927-16945",
    "17950-17952",
    "19756-19765-20094",
    "20153-20325",
    "22192-22209-22211-22217-22255-22266",
    "22193-22207-22212-22218-22256",
    "22842-22917-22919",
    "23017-23228-23623-23634",
    "23801-23841-23858",
    "23860-24116-24215-24221-24245-24249",
    "23941-24110-24129",
    "24109-24133",
    "24329-24469-24560-24575-24768",
    "24386-24415-24468-24576-24579-24733-24734-24767",
    "24470-24577-25231-25232-25233-25234-25235-25237-25257-25261-25281-25305",
    "24769-24796",
    "24806-25224-25225-25227-25228-25238-25240-25249-25277-25303",
    "24807-24899-24902",
    "24808-24900-24901-24905",
    "25212-25213-25214-25216-25218-25220-25236-25239-25260-25278-25304",
    "25229-25230-25247-25270-25276-25302",
    "25245-25251-25252-25253-25254-25256-25258-25262-25263-25264-25265-25266-25279-25307",
    "25269-25280-25309",
    "10169-10184-10190-10193-10196-10200-10208-10217-10222-10223-10224-10225-10226-10228-10229-10230-10232-10233-10235-10237-10238-10239-10243-10247-10248-10249-10250-10252-10253-10256-10258-10259-10260-10261-10262-10263-10276-10279-10283-10285-10286-10287-10288-10290-10291-10294-10295-10296-10297-10298-10303-10312-10313-10316-10317-10320-10321",
    "10199-10559-10573-10590-10603-10619-10686-10687-10695-10702-10720-10745-10752-10764-10768-10769-10771-10773-10774-10775-10776-10779-10780-10783-10787-10788-10789-10790-10792-10793-10797-10798-10800-10801-10803-10804-10805-10806-10807-10808-10809-10812-10813-10814-10815-10817-10821-10822-10823-10825-10826-10827-10828-10830-10833-10834-10835-10837-10838-10839-10840-10842-10843-10844-10845-10846-10848-10850-10852-10855-10856-10857-10858-10859-10860-10861-10862-10863-10868-10869-10870-10875-10877-10878-10879-10880-10881-10882-10890-10891-10892-10893-10894-10898-10903-10907-10912-10913-10914-10915-10916-10917-10918-10923-10924-10926-10927-10929-10934-10936-10937-10938-10939-10940-10941-10942-10943-10944-10945-10948-10949-10950-10951-10954-10958-10959-10960-10963-10964-10965-10966-10967-10968-10970-10973-10974-10975-10976-10980-10982-10983-10984-10985-10986-10988-10992-10993-10994-10995-10996-10997-10998-10999-11000-11001-11007-11009-11010-11013-11014-11018-11020-11022-11023-11024-11025-11026-11028-11030-11032-11033-11034-11037-11040-11041-11042-11043-11044-11045-11046-11048-11049-11050-11051-11052-11053-11057-11058-11059-11060-11061-11062-11063-11065-11066-11068-11069-11070-11071-11072-11074-11076-11077-11079-11080-11082-11085-11088-11090-11092-11095-11096-11099-11100-11101-11102-11107-11108-11109-11110-11112-11113-11114-11117-11118-11119-11120-11123-11125-11126-11130-11135-11139-11143-11144-11145-11147-11148-11150-11153-11154-11155-11157",
    "10234-10242-10277-10282-10284-10304-10315-10319-10322-10325-10327-10328-10332-10333-10334-10337-10338-10347-10349-10351-10353-10354-10355-10363-10365-10366",
    "10275-10306-10324-10356-10369-10371-10377-10379-10380-10381-10383-10384-10385-10386-10387-10388-10390-10397-10398-10399-10402-10403-10404-10406-10407-10409-10414-10415-10418-10420-10423-10429-10430-10432-10434-10435",
    "10331-10346-10350-10362-10375-10417-10419-10421-10422-10424-10426-10427-10431-10438-10439-10441-10442-10444-10445-10447-10448-10449-10453-10456-10457-10458-10459-10460-10462-10463-10464-10465-10466-10467-10468-10469-10470-10471-10474-10485-10486-10490-10492-10493-10495-10496-10497-10500-10501-10502-10505-10507-10508-10510-10511-10513-10514-10517-10518-10519-10520-10521-10522-10523-10535-10536-10537-10538-10540-10541-10542-10543-10544-10546-10547-10548-10551-10552-10553-10554-10555-10556-10557-10558-10560-10561-10563-10565-10567-10569-10572-10574-10579-10580-10583-10584-10586-10588-10591-10592-10593-10595-10596-10598-10604-10605-10606-10608-10613-10614-10616-10617-10618-10623-10630-10631-10638-10644-10646-10647-10648-10649-10650-10654-10655-10657-10659-10662-10667-10668-10669-10671-10672-10673-10674-10677-10683-10685-10688-10693-10694-10697-10698-10699-10700-10701-10703-10704-10706-10707-10711-10713-10714-10719-10721-10722-10729-10730-10731-10732-10739-10740-10742-10744-10748-10749-10750-10751-10756-10757-10758-10763-10765-10770",
    "11093-11129-11132-11140-11152-11156-11159-11161-11163-11164-11165-11167-11168-11169-11170-11172-11173-11174-11177-11178-11180-11182-11183-11188-11193-11194-11195-11196-11200-11204-11205-11206-11209-11210-11212-11213-11215-11216-11220",
    "12150-12151",
    "15095-15220-15249",
    "15574-15852",
    "15796-15883-15885",
    "16190-16208-16239-16246",
    "16265-16313",
    "17267-17337-17338-17411-17414-17433-17444-17486-17494-17495-17497-17536",
    "17415-17420-17423-17487-17493-17498-17538-17602",
    "17416-17422-17429-17537",
    "17513-17540-17556-17557",
    "18737-18858-18868-18885-18886-18890",
    "18901-19175-19177-19193-19219-19223-19227-19231-19233",
    "19314-19483-19520-19669-19685-19698",
    "19315-19482-19521-19668-19684-19697",
    "20093-20095",
    "20306-20324",
    "20911-20944-21041",
    "20912-20945-21042",
    "21122-21561-21575-21611",
    "21559-21573",
    "21560-21574",
    "21957-21958-21959",
    "22342-22468-22473",
    "22343-22465-22467",
    "22930-22992-23019",
    "22936-22993-23018",
    "23447-23621-23636",
    "23620-23635",
    "7134-7812-7860-7863-7898-7924-7934-7937-7938-7943-7944-7945-7952-7954-7967-7969-7972-7974-7975-7979-7981-7983-7984-7985-7986-7987-7988-7990-7991-7993-7994-7996-7997-7998-7999-8001-8002-8004-8006-8007-8008-8013-8014-8015-8018-8019-8023",
    "7488-7556-7560-7583-7584-7585-7634-7646-7647-7656-7660-7663-7670-7674-7677-7681-7682-7684-7686-7687-7688-7689-7693-7694-7695-7696-7701-7703-7705-7707-7708-7709-7712-7713-7715-7719-7720-7721-7722-7723-7727-7730-7731-7732-7735-7736-7738-7739-7742-7743-7744-7746-7747-7751-7753-7754-7755-7761-7762-7763-7764-7765-7769-7772-7773-7774-7775-7778-7779-7780-7781-7782-7784-7785-7786-7787-7789-7792-7798-7799-7800-7801-7802-7803-7804-7805-7807-7808-7810-7811-7814-7815-7817-7818-7819-7820-7821-7822-7823-7824-7825-7828-7829-7830-7831-7832-7835-7837-7838-7840-7841-7843-7844-7845-7846-7847-7850-7853-7854-7857-7858-7859-7864-7865-7866-7870-7871-7873-7874-7875-7876-7877-7878-7879-7880-7881-7882-7883-7885-7886-7888-7892-7893-7894-7896-7897-7899-7900-7903-7904-7906-7907-7909-7910-7911-7915-7917-7918-7919-7920-7925-7933-7935-7941",
    "7790-8115-8176-8258-8333-8415-8418-8425-8435-8445-8454-8475-8476-8477-8478-8479-8480-8481-8482-8483-8484-8486-8488-8489-8490-8494-8495-8496-8497-8502-8503-8506-8511-8512-8513-8515-8516-8520-8521-8522-8524-8527-8528-8529-8530-8533-8534-8535-8541-8544-8548-8553-8554-8555-8558-8559-8561-8562-8563-8564-8565-8567-8568-8569-8570-8571-8572-8576-8578-8581-8584-8585-8586-8587-8590-8591-8594-8596-8597-8598-8599-8600-8601-8603-8604-8606-8608-8609-8612-8613-8614-8616-8617-8618-8619-8623-8624-8627-8628-8637-8641-8643-8645",
    "7851-7927-7936-7950-7976-7982-7989-8005-8009-8017-8020-8028-8029-8030-8031-8035-8036-8037-8040-8042-8044-8045-8046-8047-8052-8056-8057-8059-8060-8062-8066-8073-8074-8075-8076-8077-8078-8079-8080-8081-8082-8083-8084-8087-8088-8093-8095-8096-8097-8100-8102-8103-8104-8105-8106-8108-8111-8112-8113-8114-8116-8118-8121-8125-8129-8130-8131-8133-8135-8136-8137-8138-8139-8140-8142-8143-8144-8146-8148-8151-8154-8158-8159-8160-8162-8164-8166-8167-8168-8170-8178-8184-8185-8186-8190-8191-8195-8196-8197-8198-8201-8203-8204-8205-8206-8208-8209-8212-8215-8222-8223-8224-8225-8226-8227-8228-8229-8230-8232-8233-8234-8239-8240-8244-8246-8249-8251-8252-8255-8260-8264-8270-8271-8272-8273-8274-8275-8276-8277-8278-8284-8285-8286-8287-8288-8289-8291-8299-8300-8301-8303-8304-8310-8312-8319-8320-8321-8322-8328",
    "7942-8231-8256-8336-8366-8374-8380-8384-8397-8398-8400-8402-8403-8406-8408-8409-8410-8411-8412-8419-8420-8421-8422-8423-8424-8428-8429-8431-8432-8433-8436-8437-8438-8440-8442-8446-8450-8452-8453-8455-8457-8459-8460-8461-8464-8465-8466-8469-8473",
    "8194-8280-8309-8317-8318-8329-8330-8331-8332-8334-8335-8337-8339-8340-8347-8349-8350-8352-8353-8354-8355-8357-8358-8359-8360-8367-8368-8369-8372-8373-8377-8381-8382-8383-8389-8390-8392-8393",
    "8519-8588-8648-8652-8660",
    "8640-8860-8906-8921-8924-8964-8979-8993-9000-9001-9017-9021-9022-9026-9027-9028-9029-9030-9038-9039-9046-9047-9048-9049-9050-9051-9052-9054-9059-9063-9064-9068-9069-9070-9071-9073-9079-9080-9083-9084-9086-9088-9089-9091-9092-9093-9094-9096-9097-9098-9101-9103-9104-9111-9112-9113-9114-9115-9117-9125-9126-9127-9128-9129-9134-9137-9140-9143-9146-9150-9152-9153-9154-9156",
    "8936-9226-9259-9264-9286-9290-9304-9308-9314-9317-9319-9320-9321-9322-9325-9327-9330-9342-9344-9346-9347-9355-9356-9357-9358-9359-9360-9362-9365-9370-9372-9376-9382-9386-9387-9390-9392-9393-9395-9398",
    "9040-9164-9176-9198-9216-9218-9245-9247-9248-9251-9252-9253-9258-9262-9263-9266-9273-9275-9277-9288",
    "9100-9295-9313-9338-9349-9363-9366-9367-9369-9375-9377-9378-9385-9388-9389-9397-9401-9402-9405-9407-9408-9410-9411-9412-9413-9414-9417-9427-9429-9430-9436-9437-9438-9440-9441-9442-9443-9447-9448-9449-9450-9452-9453-9454-9455-9456-9458-9459-9460-9463-9464-9465-9468-9469-9471-9472-9476-9479-9481-9482-9483-9484-9486-9487-9488-9490-9494-9496-9500-9503-9506-9507-9508-9509-9510-9511-9512-9513-9514-9515-9516-9517-9520-9521-9528-9529-9530-9534-9535-9538",
    "9108-9351-9461-9475-9522-9523-9524-9539-9540-9543-9545-9548-9549-9552-9554-9555-9557-9559-9562-9563-9564-9566-9567-9569-9570-9578-9582-9583-9584-9585-9586-9587-9588-9589-9591-9593-9594-9595-9596-9601-9603-9605-9606-9607-9608-9613-9616-9618-9620-9621-9622-9625-9626-9627-9628-9629-9630-9631-9632-9633-9636-9638-9639-9640-9641-9642-9644-9645-9646-9650-9652-9653-9654-9655-9656-9657-9658-9659-9660-9662-9663-9666-9667-9668-9669-9672-9674-9675-9676-9677-9680-9681-9683-9684-9686-9689-9693-9694-9696-9697-9698-9699-9700-9701-9702-9703-9704-9705-9707-9708-9709-9710-9711-9714-9715-9717-9718-9720-9722-9723-9725-9726-9727-9728-9729-9730-9731-9732-9734-9735-9736-9738-9739-9740-9742-9743-9746-9751-9755-9756-9757-9758-9759-9763-9765-9768-9770-9771-9774-9776-9777-9778-9781-9783-9784-9786-9788-9789-9790-9792-9794-9797-9798-9801-9802-9804-9805-9807-9808-9809-9810-9811-9812-9813-9814-9817-9818-9824-9826-9827-9828-9829-9830-9831-9832-9834-9843-9844-9846-9847-9848-9849-9850-9851-9852-9853-9854-9859-9860-9861-9862-9864-9865-9866-9868-9869-9870-9871-9872-9874-9875-9876-9878-9882-9885-9886-9887-9891-9892",
    "9238-9250-9270-9272-9279-9292-9293-9297-9298-9299-9300-9302-9303-9305-9311",
    "9842-10811-10920-11124-11171-11189-11197-11199-11207-11208-11211-11214-11218-11222-11223-11224-11225-11227-11228-11231-11233-11234-11240-11242-11243-11245-11246-11248-11250-11251-11253-11254-11256-11258-11259-11260-11266-11267-11268-11270-11271-11273-11274-11277-11279-11281-11283-11285-11286-11288-11291-11293-11296-11305-11309-11313-11314-11318-11320",
    "9998-10011-10022-10024-10026-10029-10030-10050-10052-10057-10058-10059-10060-10061-10062-10063-10065-10066-10068-10069-10070-10071-10072-10073-10075-10076-10079-10080-10083-10084-10085-10087-10089-10090-10091-10092-10093-10094-10095-10096-10097-10099-10101-10107-10108-10110-10111-10112-10114-10115-10116-10117-10118-10119-10125-10128-10129-10130-10131-10132-10133-10134-10135-10136-10137-10138-10139-10140-10141-10142-10144-10145-10146-10147-10149-10150-10152-10153-10155-10156-10157-10158-10160-10162-10163-10164-10168-10170-10171-10172-10173-10175-10177-10178-10179-10181-10182-10185-10186-10187-10188-10191-10197-10198-10201-10203-10206-10207-10210-10211-10212-10214-10215-10220-10221",
]
for _ni in _BUNDLE_NIS_CODER:
    Instance.register("coder", _ni)(Coder)
