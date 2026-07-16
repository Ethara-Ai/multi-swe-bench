import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# goharbor/harbor spans release lines 1.1 .. 2.14. The module-era code (1.10+)
# lives under src/ with src/go.mod; older lines (<=1.9) are GOPATH-era: no
# go.mod, import path github.com/goharbor/harbor/src/..., deps vendored under
# src/vendor. For the GOPATH eras the checkout MUST live under
# $GOPATH/src/github.com/goharbor/harbor or self-imports resolve to the synthetic
# `_/...` path and every internal package fails to build. So we clone into the
# GOPATH layout unconditionally: module-era builds ignore the location (go.mod
# wins), GOPATH-era builds require it. The scripts pick module vs GOPATH mode
# per-stage from src/go.mod presence.
_GO_IMAGE = "golang:1.24-bookworm"

# GOPATH is /go in the official golang image; harbor's canonical import root.
_REPO_ROOT = "/go/src/github.com/goharbor/harbor"


# ---------------------------------------------------------------------------
# Build-context scripts (COPY'd into the image, run at build/eval time).
# ---------------------------------------------------------------------------

# src/go.mod => module era (1.10+): GO111MODULE=on. No go.mod => GOPATH era
# (<=1.9): GO111MODULE=off so the vendored src/vendor tree + github.com/goharbor
# /harbor/src/... self-imports resolve against the GOPATH checkout.
_MODE_SELECT = (
    'if [ -f go.mod ]; then export GO111MODULE=on; '
    'else export GO111MODULE=off; fi'
)

# Warms the go module/build cache at base.sha so the eval runs start compiled.
# Runs BEFORE the hardening strip; `|| true` keeps a flaky baseline from breaking
# the build.
_INSTALL_SH = """#!/bin/bash
set -e

git config --global --add safe.directory {root} || true
cd {root}/src
{mode_select}
if [ "$GO111MODULE" = "on" ]; then
  timeout --kill-after=30 600 go mod download || true
fi
go build ./... >/dev/null 2>&1 || true
""".format(root=_REPO_ROOT, mode_select=_MODE_SELECT)

# Harbor's unit tests need a live PostgreSQL + Redis: ~54 of 58 test packages
# call InitDatabaseFromEnv()/redis in TestMain and FATAL ("POSTGRESQL_HOST is not
# set" / "connection refused") when they are absent -- identically across the
# run/test/fix stages, which erases every fail->pass transition (the gold f2p
# test never executes because a DB-dependent sibling file aborts the package
# binary first). This snippet provisions both services and exports the same env
# vars harbor's own tests/ci/ut_run.sh + CI.yml UTTEST job use. Run per stage
# (each stage is a fresh container); `|| true` keeps a hiccup from aborting the
# stage under `set -uxo pipefail`.
_SERVICES_UP = r"""
export POSTGRESQL_HOST=localhost
export POSTGRESQL_PORT=5432
export POSTGRESQL_USR=postgres
export POSTGRESQL_PWD=root123
export POSTGRESQL_DATABASE=registry
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_USR=postgres
export POSTGRES_PWD=root123
export REDIS_HOST=localhost
export REDIS_PORT=6379
export POSTGRES_MIGRATION_SCRIPTS_PATH=__ROOT__/make/migrations/postgresql/

_PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | head -1)"
export PGDATA=/var/lib/postgresql/pgdata
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  mkdir -p "$PGDATA"; chown -R postgres:postgres "$PGDATA"
  su postgres -c "$_PG_BIN/initdb -A trust -D $PGDATA" >/dev/null 2>&1 || true
fi
su postgres -c "$_PG_BIN/pg_ctl -D $PGDATA -o '-c listen_addresses=* -c port=5432' -l /tmp/pg.log -w start" >/dev/null 2>&1 || true
for _i in $(seq 1 30); do su postgres -c "psql -h localhost -p 5432 -c 'select 1'" >/dev/null 2>&1 && break; sleep 1; done
su postgres -c "psql -h localhost -p 5432 -c \"ALTER USER postgres PASSWORD 'root123';\"" >/dev/null 2>&1 || true
su postgres -c "psql -h localhost -p 5432 -tc \"SELECT 1 FROM pg_database WHERE datname='registry'\"" 2>/dev/null | grep -q 1 || su postgres -c "psql -h localhost -p 5432 -c 'CREATE DATABASE registry;'" >/dev/null 2>&1 || true
redis-server --daemonize yes >/dev/null 2>&1 || true
""".replace("__ROOT__", _REPO_ROOT)

# Test command: mirror harbor's coverage4gotest.sh, which runs
# `go list ./... | grep -v -E 'tests|testing'` -- excluding the tests/ and
# testing/ trees keeps the harness off packages harbor never unit-tests (the
# `tests/apitests/apilib` -> github.com/dghubble/sling and `testing/job` ->
# undefined job.Replication build failures live there). Fall back to ./... if
# `go list` yields nothing.
#
# `-p 1` is REQUIRED: every DB-dependent package bootstraps golang-migrate
# against the single shared `registry` database in its TestMain. Go's default
# parallel package execution makes several connections race on
# `CREATE TABLE schema_migrations`, which trips Postgres' catalog unique
# constraint (pg_type_typname_nsp_index); the migration then aborts and the real
# tables (project, ...) are never created, so every DB test FATALs with
# `relation "project" does not exist`. Serializing package execution (as harbor's
# own coverage4gotest.sh does with a per-package for-loop) lets the first package
# apply migrations cleanly; the rest see schema_migrations already current.
_TEST_CMD = (
    '_PKGS=$(go list ./... 2>/dev/null | grep -vE \'/tests(/|$)|/testing(/|$)\'); '
    '[ -z "$_PKGS" ] && _PKGS=./...; '
    'timeout --kill-after=60 2400 go test -p 1 -v -count=1 $_PKGS'
)

# Baseline: clean base.sha, no patches. base.sha is still checkout-able after the
# hardening strip because it is HEAD (reachable, not pruned).
_RUN_SH = """#!/bin/bash
set -uxo pipefail
export CI=true
{services}
git config --global --add safe.directory {root} || true
cd {root}
git reset --hard
git checkout {{pr.base.sha}}

cd {root}/src
{mode_select}
{test_cmd}
""".format(root=_REPO_ROOT, mode_select=_MODE_SELECT, services=_SERVICES_UP, test_cmd=_TEST_CMD)

# Test patch only: the new tests exercise behaviour the fix has not introduced
# yet, so they fail (or their package fails to compile) -- genuine f2p / n2p.
_TEST_RUN_SH = """#!/bin/bash
set -uxo pipefail
export CI=true
{services}
git config --global --add safe.directory {root} || true
cd {root}
git reset --hard
git checkout {{pr.base.sha}}
git apply --whitespace=nowarn /home/test.patch

cd {root}/src
{mode_select}
{test_cmd}
""".format(root=_REPO_ROOT, mode_select=_MODE_SELECT, services=_SERVICES_UP, test_cmd=_TEST_CMD)

# Test + fix patches: production fix present, the suite passes.
_FIX_RUN_SH = """#!/bin/bash
set -uxo pipefail
export CI=true
{services}
git config --global --add safe.directory {root} || true
cd {root}
git reset --hard
git checkout {{pr.base.sha}}
git apply --whitespace=nowarn /home/fix.patch
git apply --whitespace=nowarn /home/test.patch

cd {root}/src
{mode_select}
{test_cmd}
""".format(root=_REPO_ROOT, mode_select=_MODE_SELECT, services=_SERVICES_UP, test_cmd=_TEST_CMD)


class HarborImageBase(Image):
    """Level 1: toolchain-only base image (shared by all PRs).

    ``dependency()`` returns a *string* (the Go toolchain image), so the
    pipeline's ``DockerfileEnhancer`` engages and prepends the
    ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image must NOT clone
    the repository -- a shared string-dependency image that performs a
    ``git clone`` is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer, which would break ``git checkout`` for
    every other PR sharing the base. So the clone lives in HarborImageDefault
    (whose dependency() is an Image, left verbatim by the enhancer), done per-PR.
    This image only provides the Go toolchain, apt deps, and Go build env.
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

    def dependency(self) -> Union[str, "Image"]:
        return _GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # No `git clone` here on purpose -- see the class docstring. The string
        # dependency means DockerfileEnhancer injects the ARG/ENV/LABEL infra
        # block (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    pkg-config \\
    libldap2-dev \\
    python3 \\
    sudo \\
    wget \\
    postgresql \\
    postgresql-contrib \\
    redis-server \\
    && rm -rf /var/lib/apt/lists/*

ENV GOFLAGS=-buildvcs=false
ENV CGO_ENABLED=1
ENV GOTOOLCHAIN=local

CMD ["/bin/bash"]
"""


class HarborImageDefault(Image):
    """Level 2: per-PR image (built on the shared toolchain base).

    ``dependency()`` returns HarborImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim -- no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ``${BASE_COMMIT}`` inline, COPYs the
    scripts, warms the build cache (install.sh), then the verbatim
    ``Image._HARDENING_BLOCK`` strips origin/refs/future history (with the four
    post-condition asserts + submodule pass) while keeping base.sha reachable.
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

    def dependency(self) -> Union[str, "Image"]:
        return HarborImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", _INSTALL_SH),
            File(".", "run.sh", _RUN_SH.format(pr=self.pr)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(pr=self.pr)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(pr=self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git {_REPO_ROOT}

WORKDIR {_REPO_ROOT}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("goharbor", "harbor")
class Harbor(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HarborImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        def base_name(name: str) -> str:
            idx = name.rfind("/")
            return name if idx == -1 else name[:idx]

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(base_name(m.group(1)))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(base_name(m.group(1)))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(base_name(m.group(1)))

        # Disjoint sets: passed > failed > skipped
        failed_tests -= passed_tests
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


# Each release-bundled record carries a dash-joined number_interval (the bundled
# PR numbers). Instance.create() routes ONLY on f"{org}/{number_interval}" when a
# record sets number_interval (no fallback to the repo key) -- so every interval
# in the dataset MUST be registered here or the record raises
# ValueError("Instance 'goharbor/<interval>' is not registered.").
_NUMBER_INTERVALS = [
    "10258-10270-10306-10358-10359-10364-10393-10439-10453-10461-10510-10511-10515-10527-10530-10640-10644-10649-10651-10654-10671",
    "10517-10901-10925-11066-11149-11161-11174-11345-11405",
    "10754-12588-12655-12686-12829-12887-12955",
    "12034-12045-12672-12681",
    "12397-12438-12477-12505-12509-12519-12521-12529-12530-12558-12587-12597-12603-12606",
    "13575-13584-13636-13657-13677-13701",
    "13603-13871-13891-13939-14009-14344-14374-14398-14735-14744-14847",
    "13725-13816-13828-13873-13893-13903-13908-13938-13943",
    "13727-13874-13904-14008-14076-14115-14131",
    "14010-14088-14100-14123-14291-14294-14295-14296-14342-14364-14365-14396-14399-14423",
    "14417-14597-14601-14627-14630-14643-14646-14648-14652-14660-14663-14699-14726",
    "14740-15192-15193-15194-15199-15205",
    "15204-15748-15749-15776-15785-15802-15859-15892",
    "15312-15337-15373-15374-15378-15398-15405-15443-15446-15447",
    "15434-15559-15586-15588-15593-15627-15631-15658-15753-15754",
    "15473-15487-15494-15509-15544-15594-15608-15622-15626-15638-15639-15655-15657",
    "15734-15773-15922-15947-15979-15981",
    "16015-16059-16061-16066-16088-16105",
    "16192-16193-16203",
    "16717-16720-16750-16773-16774-16789",
    "16881-16994-17026-17034-17036-17042-17057-17058-17060-17065-17067-17072-17073-17080-17088-17092-17094",
    "17253-17275-17279-17309-17360-17369-17403-17416-17446-17464-17470",
    "17466-17509-17576-17590-17592-17593-17598-17601-17604-17635",
    "17547-17551-17599",
    "17665-17675-17685-17686-17687-17690-17692-17693-17697-17720-17721-17727-17728-17729-17732-17743-17745-17750-17762-17770",
    "17666-17825-17849-17972-17979-17989-18005-18041-18056-18069-18071-18095-18097",
    "17822-17823",
    "18147-18152",
    "18167-18190-18208-18211-18222-18240-18248-18259",
    "18302-18577-18641-18710",
    "18683-18691-18696-18712-18741-18749-18750-18768-18769-18776-18782",
    "18785-18803-18903-18907-18915-18961-18979-18990-18994-19007",
    "19206-20698-20900-20905-20907-20916",
    "19604-19607-19608-19613-19620-19644",
    "1983-2018-2020-2026-2031-2032-2041-2097-2105-2115-2116-2119-2125-2129-2143-2159-2172-2174-2175-2176-2179-2182-2184-2198-2201-2202-2204-2240-2241-2300",
    "20126-20170-20196-20211-20221-20236-20242-20262-20263-20268-20275",
    "20127-20168-20212-20222-20235-20243-20266-20267-20271",
    "20169-20194-20195-20210-20220-20237-20241",
    "20442-20551-20664-20665",
    "20581-20633-20634-20661-20678-20694-20699-20814-20817-20824-20826-20827-20834-20838-20839-20854",
    "20886-21000-21166-21167-21168-21171-21177-21178-21180-21181-21184-21217",
    "21374-21393-21412-21417-21426",
    "21908-22019-22022-22024",
    "22186-22189-22200-22209-22211-22215-22216",
    "22253-22475-22476-22477-22478-22485-22486-22500-22502-22513-22547-22549-22551-22559-22560-22561-22562-22575",
    "22603-22646-22658-22689-22691-22705-22712-22727",
    "22657-22693-22711-22728",
    "22918-22924-22930-22939-22943-22945-22950-22951-22959-22960-22963-22968-22970",
    "22919-22923-22931-22940-22944-22946-22949-22952-22955-22958-22964-22969-22971",
    "5376-5787-5807-5811-5874",
    "5923-6191-6257-6300",
    "6569-6575-6611-6623-6633-6646-6650-6686-6692-6695",
    "7134-7168-7174-7181-7246-7260",
    "7502-8959-9017-9064",
    "9285-9287",
    "9539-9556-9586-9590-9636-9656-9672-9689-9695",
    "9698-9817-9856-9868-9879-9890",
    "9910-9914-9932-9965-9968-9992-10024-10037-10045-10049-10050-10073-10075-10090-10133-10150-10157-10215-10257-10315-10351-10360-10363",
    "11567-11804-11851-12016-12040-12042-12059-12068-12069-12070-12081-12098-12104-12113",
    "12242-12424-12447",
    "12830-12834-12836-12859-12872-12874-12878-12892-12893-12901-12902-12950-12952-12967-13007-13009-13033-13058-13060-13062",
    "13121-13122-13133-13173-13250-13256-13274-13298-13299-13300-13301-13310-13311-13322-13325-13343",
    "13168-13407-13411-13516-13520-13526-13528",
    "13251-13273-13278-13346-13430-13450-13456",
    "13336-13377-13381-13394-13429-13451-13571-13572-13574-13601-13635-13638-13651-13658-13663-13665-13668-13676-13681-13686-13711-13713",
    "14227-14273-14322-14343-14357-14362-14363-14394-14424-14428-14434-14451-14452-14456-14471-14478-14508",
    "14570-14595-14602-14626-14629-14632-14639-14644-14647-14670-14680-14695-14700-14724-14755-14761-14842-14852-14867-14868-14883-14884-14885",
    "14781-14825-14869-14871-14886-15017-15019-15026-15032-15036-15176-15183-15240-15249",
    "14828-14976-14977-15016-15018-15025-15085-15089-15126-15224-15243-15248-15258",
    "15222-15225-15244-15281-15319-15324-15329-15330-15335",
    "15933-15948-16016-16063-16091-16106-16109-16118-16119-16127",
    "16212-16258-16262-16268-16281-16299-16305-16344-16358-16383-16395-16407-16417-16447-16484-16500-16512-16520",
    "16573-16590-16603-16835-17236-17238-17239-17240-17241-17243-17260-17281-17284",
    "16691-16738-16787-16791-16814-16818-16819-16822-16830-16847-16855-16857-16858-16867-16869-16893-16905-16913",
    "18049-18054-18109-18149-18166-18176-18177-18206-18213-18220-18227-18229-18235-18238-18241-18244-18246",
    "18108-18160-18174-18178-18207-18212-18215-18221-18228-18230-18232-18239-18245-18247",
    "18348-18421-18437-18464-18467-18494-18496-18544-18547-18548-18550-18556-18559-18582-18585-18586",
    "18546-18557-18579-18581-18584-18587-18622-18639-18648-18655-18657-18658",
    "18632-18650-18660-18684-18711-18742-18751-18784-18802-18980-18995-19056-19082-19089-19093-19119-19121-19148-19162-19189-19220-19306-19314-19324-19329",
    "19058-19081-19088-19092-19117-19122-19138-19147-19170-19184",
    "19219-19305-19337-19562-19670-20024-20040-20041-20046-20071",
    "19307-19336-19351-19377-19396-19431-19432-19447-19449-19451-19455-19460-19463-19471-19475-19476-19483-19488-19499-19503-19506-19511-19513-19514",
    "19530-19550-19563-19606-19626-19628-19637-19737-19740-19815-19837-19847-19848-19851-19857-19861-19891-19893-19900-19901-19906-19913",
    "19736-19739-19800-19816-19818-19823-19838-19846-19862-19894-19904-19939-19947-19950-19960-19998-20020-20022-20121-20122",
    "19930-19940-19941-19943-19951-19958-19997-20005-20021-20023-20047-20070",
    "20443-20552-20657-20662-20677",
    "21114-21135-21186-21216-21272-21278-21289-21295-21305-21308-21313-21314-21316-21322-21327-21337-21338",
    "21604-21607-21897-21899-21901-21917-21934-21948-21967",
    "21875-21907-21918-21927-21932-21936-21965-21975-21976-21978-21983-21984-21997-22002-22013-22023",
    "22394-22409-22410-22469-22489-22496-22499-22501-22506-22512-22515-22518-22541-22550-22552-22564-22576",
    "2330-2431-2454-2479",
    "4906-4911-4913-4915-4980-5004",
    "6924-6961-6962-6970-6985-6991-7007-7019-7033-7049",
    "7880-7971-8006-8013-8015-8040-8048-8057",
    "7923-8761-8780-8782-8795-8796-8822-8922-8949-8950-8963-9025-9097-9111",
    "8141-8201-8212-8214-8226-8291-8292-8315-8327-8329-8342-8447-8504-8531-8560",
    "9162-9163-9196-9199-9201-9215-9228-9229-9235-9241-9244-9246-9248-9254-9268-9269-9272-9281-9286-9371",
    "9237-9697-9724-9746-9751-9806-9834-9843-9858-9871-9872-9878-9893",
    "9416-9419-9442-9456-9458-9459-9462-9479-9481-9488-9497-9498-9523-9527-9533-9589-9620-9621-9661-9662-9678-9679-9682-9684-9688",
]


for _interval in _NUMBER_INTERVALS:
    Instance.register("goharbor", _interval)(Harbor)
