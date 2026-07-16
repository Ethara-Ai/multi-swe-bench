from __future__ import annotations

"""grafana/grafana TypeScript-only registry config for multi-swe-bench.

Grafana is a polyglot monorepo: Go backend + TypeScript/React frontend.
This config handles ONLY TypeScript PRs (99 PRs, number_interval='grafana_typescript').
- Base image: golang:latest (Debian Bookworm — provides git, curl; Node via NVM)
- Node managed via NVM — reads .nvmrc at each commit
- Package manager: yarn (yarn.lock present throughout)
- Tests: CI=true yarn test:ci (Jest)
- Parse: Jest output (PASS/FAIL suite lines, individual check/cross/circle markers)
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GrafanaTsImageBase(Image):
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
        return "golang:latest"

    def image_tag(self) -> str:
        return "base-ts"

    def workdir(self) -> str:
        return "base-ts"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # `# syntax` opts out of DockerfileEnhancer so this hand-written base is used
        # verbatim: clone FULL history (all release branches) + light harden only, with
        # gc disabled so no commit is pruned. The strict anti-reward-hack strip runs in
        # the PR layer at each PR's LITERAL base.sha. This fixes the shared-base bug: a
        # single base pruned to one commit could not hold base.shas from the 30 divergent
        # release lines (6.3 -> 12.4); a full-history base makes every checkout succeed.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
# Install system dependencies and NVM (no version pins)
RUN apt-get update && apt-get install -y --no-install-recommends \\
        curl git jq ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/HEAD/install.sh | bash

RUN git config --global --add safe.directory '*'
{fetch}

WORKDIR /home/{repo}
# Light hardening only: keep FULL history (all branches reachable, gc off) so every
# PR's base.sha can be checked out; the PR layer does the strict per-sha strip.
RUN git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class GrafanaTsImageDefault(Image):
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
        return GrafanaTsImageBase(self.pr, self._config)

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
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

export NVM_DIR="$HOME/.nvm"
# nvm.sh sourcing can return non-zero under set -e
set +e
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install Node version specified by .nvmrc (or latest LTS as fallback)
set +e
nvm install 2>/dev/null || nvm install --lts || true
nvm use 2>/dev/null || true
set -e

# Install yarn globally if not present
npm list -g yarn 2>/dev/null | grep -q yarn || npm install -g yarn || true

# Install JS/TS dependencies
yarn install --frozen-lockfile 2>/dev/null || yarn install || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export NVM_DIR="$HOME/.nvm"
set +e; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; set -eo pipefail

cd /home/{repo}
set +e; nvm use 2>/dev/null; set -eo pipefail

CI=true yarn test:ci 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export NVM_DIR="$HOME/.nvm"
set +e; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
set +e; nvm use 2>/dev/null; set -eo pipefail

yarn install --frozen-lockfile 2>/dev/null || yarn install || true

CI=true yarn test:ci 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export NVM_DIR="$HOME/.nvm"
set +e; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
set +e; nvm use 2>/dev/null; set -eo pipefail

yarn install --frozen-lockfile 2>/dev/null || yarn install || true

CI=true yarn test:ci 2>&1

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Strict anti-reward-hack hardening at the PR layer with this PR's LITERAL
        # base.sha (the shared base keeps full history; each PR image strips itself to
        # base.sha). prepare.sh checks out base.sha first; the canonical block then
        # detaches, removes all refs/remotes, gc-prunes, and asserts HEAD == base.sha.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}
{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("grafana", "grafana_typescript")
class GrafanaTypescript(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrafanaTsImageDefault(self.pr, self._config)

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
        """Parse Jest test output.

        Jest output format (verbose):
            PASS packages/grafana-data/src/dataframe/ArrayDataFrame.test.ts
              Suite Name
                ✓ test description (2 ms)
                ✕ failing test (1 ms)
                ○ skipped test
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # Jest suite-level patterns (PASS/FAIL <file>)
        re_jest_suite = re.compile(r"^(PASS|FAIL)\s+(\S+)")

        # Jest individual test patterns
        re_jest_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_fail = re.compile(r"^\s*[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_skip = re.compile(r"^\s*[○⊘]\s+(.+)")

        current_suite = ""
        has_individual_tests = False

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            # Suite-level (PASS/FAIL <file>)
            m = re_jest_suite.match(line)
            if m:
                current_suite = m.group(2)
                if not has_individual_tests:
                    if m.group(1) == "PASS":
                        failed_tests.discard(current_suite)
                        skipped_tests.discard(current_suite)
                        passed_tests.add(current_suite)
                    else:
                        passed_tests.discard(current_suite)
                        failed_tests.add(current_suite)
                continue

            # Individual test lines
            m = re_jest_pass.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                passed_tests.discard(current_suite)
                failed_tests.discard(current_suite)
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_jest_fail.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                passed_tests.discard(current_suite)
                failed_tests.discard(current_suite)
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_jest_skip.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- Bundle-level number_interval routing keys (all -> GrafanaTypescript) ---
# Each TS bundle's dash-joined number_interval registered so Instance.create()
# resolves f"grafana/{number_interval}" to the TS-only GrafanaTypescript class
# (fixes routing: empty number_interval otherwise falls through to grafana/grafana).
_BUNDLE_NIS_GRAFANA_TS = [
    "102610-102641-102776-102824",
    "102636-102716-102727-102818-102830-102837",
    "109572-109670-109976-110170-110207-110503-110644-111099-111178-111343-111349",
    "112735-113202",
    "115376-115427-115450-115560",
    "115438-115446",
    "116948-117181-117373-117501-117712-117777-117971",
    "117892-118003-118190-118289-118658",
    "117934-117989-118186-118290-118308-118321-118657",
    "117939-117981-118182-118291-118311-118324-118616-118656",
    "118801-118938-119074-119702",
    "118806-118845-118885-118912-118937-119021-119076-119419-119701",
    "119813-119924-120462-120785-120803-120898-120932-120946",
    "28403-28407-28424",
    "28637-28651-28655-28662-28668-28676-28687-28689-28694",
    "28691-28704-28722-28726-28727-28741-28755-28774-28775-28800-28801-28828-28832-28851-28855-28856-28859-28873-28890-28891-28925-28927-28934-28944-28951-28959-28963-28964-28983-28984-28985-28987-29007-29015-29020-29024-29025",
    "29162-29177-29180-29181-29184-29205-29261-29278-29285-29309-29323-29328-29333-29335-29336-29337-29343",
    "30912-30915-30924-30934-30976-30983-30986-30991-31001-31005-31007-31014-31029-31032-31037-31046-31050-31053-31076-31084-31090-31094-31100-31105-31117-31120-31127-31128",
    "31329-31342-31343-31344-31348-31349-31385-31386-31395-31441-31442",
    "32348-32361-32362",
    "34428-34447-34474-34603-34702-34827-34994-35025-35138-35210-35321-35353-35365-35412-35443-35519-35585-35650-35701-35747-35799-35804",
    "35822-35880-36056",
    "37598-37603-37605-37606-37620-37625-37641-37646-37647-37656-37680-37691",
    "39289-39304",
    "40140-40146-40151-40161-40166-40177-40178-40200-40202-40207-40209-40219-40220-40221-40236-40258-40259-40262",
    "41872-41888-41895-41915-42002-42009-42013-42061-42142-42181-42207-42427-42454-42455-42510-42528-42529-42589-42626-42646-42653",
    "42536-49223-49224",
    "44066-44149",
    "49812-55176-55688-55734",
    "57898-57913-57914-57919-57923-57925-57935-57937-57938-57945-57948-57949-57957-57958-57969-57983-57993-58037-58040-58042-58044-58049-58068-58102-58104-58123-58125-58128-58145-58147-58151-58175-58194-58222-58225-58243-58272-58273-58274-58298-58308-58314-58323-58339-58344-58346-58349-58354-58356-58366",
    "59309-59653-59679-59680-59683-59693-59697-59707-59708-59709-59716-59717-59744-59746-59756-59806-59825-59833-59840-59841-59856-59869-59876-59879-59933-59934-59937-59941-59948-59951-59955-59971-60034-60035-60042-60047-60048-60051-60054-60057-60070-60071-60074-60078-60082-60085-60086-60090-60092-60100-60110-60111-60122-60124-60149-60150-60162-60170-60174-60184-60216-60224-60237-60239-60246-60250-60274-60280-60291-60294",
    "60348-60443-60662-60748-60821-60826-60834-60888-61012-61133-61148-61180-61267-61285-61419-61462-61480-61501-61564-61681-61691-61791-61818-61822-61828-61888-61940",
    "83995-84012-84014-84069-84446-84634-84764",
    "83996-84007-84009-84068-84447-84507-84765",
    "84016-84017-84070-84445-84762",
    "85168-85170-85789-86783-87418-87476",
    "89571-89723-89729-89739-89743-89784-90208-90288-90300-90449-90615-90696-90699-90756",
    "90909-90954-90964-90976-90983-91020-91094-91129-91157",
    "96561-96633-96675-96699-96717-96793-96805-96870-97128-97132-97263-97269-97289-97299-97305",
    "108541-108637-108868-109196-109302-109315-109333-109465",
    "108545-108634-108728-108810-108817-109197-109301-109314-109332-109352-109362-109400",
    "111512-111604-111682-111766-111847-111995-112096-112161-112284-112364-112369-112655",
    "111514-111531-111605-111607-111632-111683-111692-111764-111767-111781-111848-111857-111939-111945-111997-112067-112097-112131-112159-112285-112362-112367-112656",
    "112693-112722-112731-112868-112891-112933-113057-113097-113190-113235-113280-113863-113900-113919",
    "112694-112725-112732-112869-112892-112934-113100-113234-113505-113538-113858-113901-113920",
    "114159-114218-114249-114300-114396-114757-115060-115071-115388-115409",
    "114162-114242-114256-114262-114268-114327-114391-114629-114702-114706-114753-114834-114840-114875-114906-115053-115063-115075-115138-115180-115185-115225-115279-115282-115391-115412",
    "114164-114252-114266-114299-114394-114704-114756-114873-114904-115051-115061-115072-115343-115389-115410",
    "114171-114254-114261-114267-114298-114393-114705-114755-114874-114905-115052-115062-115073-115141-115344-115390-115411-115425",
    "118614-119805-119848-119861-119899-119981-120151-120152-120170-120179-120206-120209-120273-120275-120287-120330-120388-120397-120400-120436-120457-120500-120518-120519-120523-120525-120568-120628-120690-120718-120779-120799-120847-120925-120935-120955-120998-121047",
    "118811-118848-118886-118911-118935-119010-119025-119077-119398-119696-119719",
    "119131-120837-121092-121191-121337-121414-121417-121481-121537-121545-121584-121992-122006-122095-122125-122131-122147-122172-122209-122223-122349-122372-122419-122424-122442-122443-122470-122510",
    "119804-119856-119993-120153-120157-120174-120210-120258-120399-120405-120459-120780-120800-120897-120927-120950",
    "119845-119920-120404-120468-120781-120801-120900-120928-120948",
    "19394-20598",
    "21965-22293",
    "22904-22924",
    "23327-23808",
    "267-44979-45091-45129-45155-45168-45172-45266-45268-45318-45329-45330-45393-45445-45526-45539-45670-45711-45782-45983-45991-46000",
    "29018-29054-29068-29086-29087-29088-29095-29111-29119-29126-29128-29132-29146-29147-29151-29155",
    "31162-31170-31176-31181-31201-31209-31213-31214-31221-31224-31228-31232-31238-31239-31244-31245-31246-31248-31262-31266-31269-31272-31275",
    "32394-32395-32397-32410-32424-32431-32433-32442-32486-32488-32502",
    "33481-33490-33492-33495-33497-33536-33586-33588-33612-33625-33681-33703-33799-33842-33851-33872-33876-33878-33881-33901-33909-33914-33922",
    "33935-33936-33975-34018-34025-34138-34226-34243",
    "35355-35359-35361-35370-35375-35377-35383-35384-35387-35389-35395-35399-35402-35409-35419-35423-35430-35432-35434-35439-35440-35444-35457-35477-35480-35481-35482-35483-35485-35492-35501",
    "35738-35937-35938-35946-35973-35978-35997-36006-36013-36014-36034-36058-36066-36076-36078-36081-36088-36089-36090-36105-36115-36123-36129-36135-36137-36158-36162-36173-36182-36183-36217-36224-36227-36232-36234-36251-36255-36264-36272-36275-36287-36288-36296-36299-36305-36315-36321-36327-36333-36343",
    "36348-36354-36356-36372-36375-36382-36386-36388-36391-36392-36394-36395-36397-36399-36421-36423-36431-36434-36438-36461-36462-36464-36478-36487-36495-36496-36505-36510-36515-36516-36546-36550-36553",
    "36557-36566-36577-36605-36613-36615-36617-36618-36623-36625-36638-36639-36649-36651-36662-36663-36665-36672-36676-36688-36690-36692-36715-36723-36726-36741-36746-36748-36750-36765-36774",
    "37690-37695-37700-37705-37710-37712-37714-37720-37722-37723-37725-37731-37735-37747-37749-37753-37801-37825-37827-37831-37832-37840-37851-37864-37877-37880-37881-37884-37886-37889-37894-37896-37902-37908-37912-37914-37915-37925-37928-37931-37936-37950-37953-37954-37964-37975-37982-37988-37993-37996-37997-38009-38022-38024-38027-38028-38035-38054-38061-38066-38072-38074-38076-38083-38085",
    "40292-40297-40317-40321-40322-40323-40329-40340-40342-40350-40351-40356-40363-40368-40374-40390-40412-40427-40432-40436-40448-40450-40457-40478-40487-40500-40501-40502-40529-40535-40538-40539-40556-40560-40561-40571-40582-40597-40598-40608-40621-40623-40628-40630-40637-40652-40655-40664-40670-40678-40689-40691-40697-40704-40705-40712-40741-40745-40750",
    "58733-58850-58857-58862-58905-58978-58982-58987-58997-59025-59064-59066-59072",
    "59136-59189-59196-59244-59245-59261-59277-59285-59296-59318-59375-59397-59404-59430-59454-59465-59486",
    "78092-78119-78128-78135-78216-78217-78232-78260-78302-78306-78308-78337-78338-78343-78349",
    "79648-79656-79708-79712-79742-79789-80043-80394-80450-80604-80635-80646-80661-80678-80917-81073-81085-81112-81398-81409",
    "79710-79714-79787-80295-80607-80682-80751-80915",
    "80032-85096-85099-85103-85148-85176-85177-85274-85343-85361-85379-85535-85688-85790-85970-85974-85983-85987-85991-86013-86015-86102-86170-86175-86218-86224-86228-86235-86245-86251-86508-86515-86530-86720-86723-86727-86733-86781-87266-87351-87423-87444-87475-87583",
    "81481-83984-83994-83998-84000-84021-84066-84087-84092-84104-84196-84208-84215-84219-84259-84324-84451-84471-84505-84684-84707-84757-84770-84852",
    "84607-87664-87729-87733-87737-87746-87751-87754-87764-87773-87775-87805-87821-87823-87829-87834-87862-87864-87969-88023-88067-88068-88069-88073-88076-88094-88115-88141-88151-88168-88174-88185-88189-88191-88195-88224-88228-88229-88241-88244-88254-88408-88501-88648-88650-88702-88711-88749-88765-88786-88866-88907-88934-88999-89061-89067-89104-89129",
    "84853-84862-84878-84918-84924-84926-84943-84945-85016-85072-85087-85098-85101-85105-85120-85144-85150-85154-85174-85192-85208-85237-85246-85276-85279-85301-85345-85349-85363-85394-85413-85511-85537-85608-85610-85631-85685-85699-85713-85745-85749-85751-85760-85762-85792-85802-85817-85871",
    "84931-85015-85097-85100-85104-85149-85153-85207-85250-85275-85344-85362-85374-85536-85686-85791-85971-85975-85984-85988-85992-86014-86017-86103-86148-86171-86176-86219-86225-86229-86236-86239-86246-86509-86512-86531-86664-86698-86713-86722-86728-86734-86766-86784-86820-87254-87267-87352-87424-87451-87474-87584",
    "85160-85162-85787-86782-87031-87425-87478-87504",
    "85225-86490-87274-87620-87627-87673-87698-87720-87730-87734-87738-87753-87755-87756-87757-87760-87770-87776-87791-87792-87800-87806-87817-87824-87830-87838-87845-87865-87875-87887-87896-87916-87918-87928-87936-87940-87963-87970-87983-87992-87993-87999-88021-88024-88053-88054-88055-88056-88060-88063-88090-88100-88116-88140-88148-88167-88175-88190-88246-88252-88265-88285-88333-88401-88403-88407-88434-88473-88477-88493-88502-88510-88583-88598-88649-88651-88669-88700-88703-88706-88738-88766-88787-88846-88848-88852-88862-88894-88903-88917-88937-88982-88985-89000-89015-89063-89068-89070-89081-89092-89130-89134-89240-89246-89300-89327-89374-89416-89425-89478-89493-89501-89513",
    "85921-85945-85957-85959-85972-85976-85985-85989-85993-85998-85999-86016-86019-86104-86150-86162-86172-86177-86220-86226-86230-86237-86240-86247-86281-86467-86510-86513-86532-86609-86620-86633-86665-86667-86672-86690-86697-86700-86714-86721-86729-86735-86770-86785-86821-86866-86907-86937-87084-87164-87176-87231-87255-87268-87299-87348-87353-87373-87408-87420-87467-87473-87557-87585-87592-87647",
    "85948-87666-87711-87727-87731-87735-87744-87748-87750-87762-87827-87850-87854-87860-87866-87926-88113-88124-88646-88712-88763-88936-88997-89238-89422",
    "87876-87880-88707-88760-89286",
    "89576-89589-89726-89734-89740-89785-90130-90131-90203-90265-90289-90301-90446-90617-90694-90700-90753",
    "90953-90963-90975-91087-91093-91128-91209-91460-91917-92116-92177-92370",
    "92341-92358-92398-92440-92447-92452-92456-92460-92489-92500-92523-92530-92539-92541-92646-92663-92671-92678-92683-92720-92726-92757-92770-92774-92778-92782-92823-92841-92856-92892-92898-92911-92915-93010-93034",
    "92344-92445-92477-92521-92528-92537-92591-92676-92681-92709-92711-92755-92768-92772-92776-92780-92998",
    "92468-92544-93012",
    "94399-94742-94992-95122-95153-95164-95230-95245-95262-95284-95297-95312-95411-95488-95593-95907-95933-95977-95992-96123-96132-96317-96468-96479",
    "94400-94741-95151-95152-95158-95229-95244-95261-95283-95296-95350-95410-95487-95906-95932-95986-95991-96131-96316-96323-96467-96478",
    "94788-95281-96533-96560-96571-96618-96637-96707-96714-96726-96775-96797-96801-96808-96831-96843-96844-96864-96874-97002-97047-97050-97060-97086-97089-97111-97114-97121-97146-97156-97207-97232-97238-97262-97273-97286-97297-97309-97356",
    "95421-97411-97415-97433-97519-97524-97545-97565-97591-97664-97669-97683-97705-97808-97856-97875-97904-98045-98217-98290-98385-98402-98535-98583-98596-98764-98859-99125-99188-99208-99431",
    "95707-96432-97379-97389-97414-97416-97437-97444-97467-97520-97525-97550-97576-97593-97633-97666-97670-97685-97706-97750-97809-97829-97834-97860-97876-97905-97910-97970-98044-98118-98218-98267-98268-98291-98383-98401-98421-98424-98526-98585-98733-98765-98785-98860-98907-99083-99088-99124-99189-99209-99299-99430",
    "96559-96617-96636-96706-96710-96774-96796-96800-96869-96872-97110-97129-97231-97237-97264-97272-97285-97298-97308",
    "96562-96634-96693-96702-96776-96794-96806-96871-97108-97131-97213-97229-97265-97291-97300-97306",
    "97407-97420-97518-97590-97679-97692-97700-97702-97853-97901-98214-98481-98540-98586-98761-99128-99185-99435",
    "97409-97427-97523-97548-97592-97665-97680-97693-97703-97854-97902-98215-98539-98584-98594-98762-99127-99186-99206-99434",
]
for _ni in _BUNDLE_NIS_GRAFANA_TS:
    Instance.register("grafana", _ni)(GrafanaTypescript)
