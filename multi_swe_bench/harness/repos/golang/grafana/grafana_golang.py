from __future__ import annotations

"""grafana/grafana Go-only registry config for multi-swe-bench.

Grafana is a polyglot monorepo: Go backend + TypeScript/React frontend.
This config handles ONLY Go PRs (40 PRs, number_interval='grafana_golang').
- Base image: golang:latest (Debian Bookworm)
- Tests: go test -v -count=1 ./pkg/...
- Parse: standard Go test output (--- PASS/FAIL/SKIP: TestName)
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GrafanaGoImageBase(Image):
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
        return "base-go"

    def workdir(self) -> str:
        return "base-go"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class GrafanaGoImageDefault(Image):
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
        return GrafanaGoImageBase(self.pr, self._config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Pre-download Go modules
go mod download || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
go test -v -count=1 ./pkg/... 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./pkg/... 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./pkg/... 2>&1

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("grafana", "grafana_golang")
class GrafanaGolang(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrafanaGoImageDefault(self.pr, self._config)

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
        """Parse Go test output.

        Go test output format:
            --- PASS: TestName (0.00s)
            --- FAIL: TestName (0.01s)
            --- SKIP: TestName (0.00s)
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")

        def get_base_name(name: str) -> str:
            """Strip subtest suffix (TestFoo/SubTest -> TestFoo)."""
            idx = name.rfind("/")
            return name[:idx] if idx != -1 else name

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_fail.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_skip.match(line)
            if m:
                test_name = get_base_name(m.group(1))
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# The 40 Go records of grafana/grafana route here; the raw dataset also
# routes via the "grafana_golang" era key registered above.
_BUNDLE_NIS_GRAFANA_GO = [
    "258-45481-45483-45494-45506-45519-45521-45527-45529-45530-45540-45542-45544-45551-45554-45557-45561-45565-45573-45580-45586",  # pr-258 (21 PRs)
    "24851-24926-25094",  # pr-24851 (3 PRs)
    "29363-29364-29368-29415-29488-29504-29526-29527-29532-29606-29682-29685-29687-29690-29698-29705-29707-29708-29709-29710-29711-29723-29726",  # pr-29363 (23 PRs)
    "29761-30055-30179-30270",  # pr-29761 (4 PRs)
    "29915-32122",  # pr-29915 (2 PRs)
    "32476-32487-32490-32515-32530-32535-32558-32560-32567-32587-32592-32598-32689-32710-32714-32726-32739-32741-32745",  # pr-32476 (19 PRs)
    "32752-32758-32804-32844-32863-32945-32947-32952-32967-32968-32971",  # pr-32752 (11 PRs)
    "33659-36813-36836-37878-38894-40002",  # pr-33659 (6 PRs)
    "35454-35499-35521-35522-35527-35530-35545-35550-35551-35560-35562-35573-35579-35582-35584-35586-35587-35605-35614",  # pr-35454 (19 PRs)
    "35619-35626-35627-35636-35639-35644-35653-35661-35664-35668-35671-35672-35673-35679-35693-35696-35719-35744-35746-35748-35753-35754-35755-35764-35771-35773-35775-35776-35777-35778-35781-35782-35786-35788-35795-35803-35814-35819-35825-35826-35855-35856-35859-35861-35862-35867-35868-35872-35877-35881-35888-35889-35892-35894-35898-35902-35903-35905-35909-35921-35922-35924-35929",  # pr-35619 (63 PRs)
    "36077-36087-36749-36764-36775-36792",  # pr-36077 (6 PRs)
    "38084-38091-38101-38108-38116-38438-38450-38456-38466-38476-38477-38480-38486-38491-38510-38524-38525-38531-38539-38557-38563-38572-38574-38580-38597-38602-38624-38628-38630-38639-38641-38647-38652-38658-38662-38684-38686-38691-38702-38710-38737-38738-38757-38793-38809-38815-38821-38828-38829-38830-38837-38863-38892-38896-38916-38919-38926-38930-38953-38958-38960-38965-38967",  # pr-38084 (63 PRs)
    "38969-38980-38999-39018-39034-39053-39093-39120-39124-39138-39141-39149-39160-39167-39208-39220-39233-39243-39246-39252-39253-39265-39273",  # pr-38969 (23 PRs)
    "45596-45603-45606-45609-45615-45622-45655-45658-45667-45669-45671-45689-45692-45693-45712-45716-45731-45733-45753-45767-45768-45777-45778-45780-45783-45788-45790-45799",  # pr-45596 (28 PRs)
    "59252-59289-59943-60168-60192-60313-60317-60324-60330-60332-60349-60355-60406-60424-60429-60430-60439-60444-60509-60516-60517-60520-60534-60574-60600-60604-60625-60631-60637-60640-60641-60642-60643-60663-60686-60690-60693-60699-60704-60705-60722-60732-60737-60738-60742-60743-60745-60749-60822-60825-60835-60846-60887-60902-60907-60925-60927-60947-60976-60982-61013-61097-61132-61162-61181-61208-61236-61254-61268-61286-61289-61296-61307-61309-61327-61357-61408-61420-61435-61452-61481-61489-61502-61508-61543-61558-61565-61599-61601-61604-61606-61624-61657-61682-61692-61717-61739-61755-61756-61788-61792-61793-61796-61804-61819-61823-61829-61836-61843-61861-61865-61876-61889-61933-61937-61960",  # pr-59252 (116 PRs)
    "60878-61067-61996-62011-62020-62036-62063-62068-62074-62099-62141-62157-62176-62177-62180",  # pr-60878 (15 PRs)
    "69097-70745-70778-70948-70954-70991-70994-71002-71003-71039-71046-71078-71093-71097-71099-71100-71115-71127-71132-71139-71140-71165-71169-71192-71197-71207-71209-71213-71218-71229-71234-71267-71270-71290-71293-71295-71312-71320-71322-71333-71339-71344-71352-71359-71360-71369-71389-71418-71443-71445-71447-71452-71479-71481-71483-71503-71506-71521-71592-71615-71636-71647-71668-71669-71672-71706-71708-71723-71737-71742-71751-71759-71761-71763-71782-71798-71812-71817-71833-71845-71849-71854-71858-71861-71862-71873-71889-71894-71899-71908-71910-71918-71931-71937-71939-71955-71969-71972",  # pr-69097 (98 PRs)
    "79706-79713-79741-79788-80042-80393-80449-80606-80681-80740-80916-81072",  # pr-79706 (12 PRs)
    "79818-79877-79879-79940-80027-80044-80157-80221-80241-80360-80368-80386-80395-80451-80485-80550-80603-80636-80647-80657-80662-80686-80811-80816-80864-80918-81015-81074-81086-81092-81107-81172-81179-81181-81288-81410-81417-81420",  # pr-79818 (38 PRs)
    "83983-83997-84002-84004-84020-84063-84067-84082-84086-84091-84105-84149-84166-84195-84207-84217-84218-84258-84388-84450-84464-84504-84730-84756-84769-85014",  # pr-83983 (26 PRs)
    "85949-87181-87665-87728-87732-87736-87745-87749-87752-87763-87774-87822-87828-87832-87837-87861-87863-87927-87968-88091-88114-88149-88177-88192-88194-88197-88198-88199-88226-88237-88251-88253-88379-88406-88647-88701-88709-88764-88935-88998-89066-89241-89423-89435-89497",  # pr-85949 (45 PRs)
    "86924-88066-89177-89178-89242-89313-89424-89436-89498-89500",  # pr-86924 (10 PRs)
    "89578-89590-89610-89731-89735-89741-89748-89750-89786-90204-90266-90290-90302-90400-90618-90695-90701-90750",  # pr-89578 (18 PRs)
    "89591-89601-89611-89631-89683-89684-89736-89747-89749-89787-89972-90096-90097-90108-90209-90229-90268-90287-90291-90303-90331-90372-90389-90392-90397-90403-90440-90443-90501-90543-90557-90601-90619-90620-90663-90689-90693-90702-90752-90765",  # pr-89591 (40 PRs)
    "89592-89612-89636-89677-89690-89703-89737-89788-89837-89904-89942-89969-89979-89986-90072-90085-90089-90111-90165-90205-90262-90292-90296-90298-90304-90388-90393-90398-90444-90476-90502-90510-90515-90545-90558-90598-90602-90621-90623-90664-90690-90692-90703-90749-90766-90842",  # pr-89592 (46 PRs)
    "92345-92446-92459-92481-92502-92522-92529-92538-92588-92645-92662-92677-92682-92708-92710-92756-92769-92773-92777-92781-92822-92910-92914-92992",  # pr-92345 (24 PRs)
    "96558-96616-96635-96695-96704-96773-96795-96799-96873-97109-97130-97230-97261-97271-97292-97296-97307",  # pr-96558 (17 PRs)
    "97410-97429-97521-97546-97595-97667-97681-97695-97704-97855-97874-97903-98216-98538-98582-98595-98763-99126-99187-99207-99433",  # pr-97410 (21 PRs)
    "97500-97510-97513-97522-97526-97583-97594-97624-97668-97671-97678-97686-97707-97749-97751-97798-97810-97830-97835-97861-97877-97894-97906-97911-97923-97954-97971-98016-98043-98066-98071-98078-98119-98183-98219-98264-98269-98292-98307-98381-98384-98400-98405-98422-98425-98537-98629-98636-98649-98734-98747-98748-98766-98773-98786-98817-98861-98906-98954-98986-99070-99082-99089-99119-99123-99197-99210-99212-99303-99323-99360-99373-99406-99410-99429",  # pr-97500 (75 PRs)
    "98659-108538-108638-109141-109303-109316-109468",  # pr-98659 (7 PRs)
    "104334-108481-109612-109667-109673-109759-109795-109980-110000-110168-110204-110286-110346-110419-110504-110523-110528-110643-110945-111130-111156-111180-111243-111344-111348-111377-111393-111409-111418-111495",  # pr-104334 (30 PRs)
    "108037-108570-109625-109668-109674-109685-109688-109692-109700-109710-109760-109761-109796-109981-109996-109999-110025-110041-110046-110057-110144-110167-110187-110192-110195-110202-110243-110281-110287-110312-110420-110502-110529-110553-110620-110645-110910-110944-110964-110973-111079-111097-111111-111114-111119-111181-111351-111378-111394-111411-111419-111498",  # pr-108037 (52 PRs)
    "108540-108547-108553-108601-108602-108608-108627-108630-108636-108702-108705-108707-108716-108719-108752-108767-108799-108800-108815-108830-108877-108878-108909-108939-108989-109071-109079-109090-109193-109199-109300-109318-109329-109357-109364-109366-109403-109435-109505",  # pr-108540 (39 PRs)
    "108549-108635-108714-108798-108938-108988-109192-109198-109265-109317-109331-109354-109363",  # pr-108549 (13 PRs)
    "109577-109671-109794-109833-109978-110001-110169-110206-110285-110345-110505-110646-111098-111179-111319-111342-111347-111376-111392-111410-111417",  # pr-109577 (21 PRs)
    "111509-111684-111801-111846-111983-112045-112094-112162-112164-112283-112365-112370-112415-112654",  # pr-111509 (14 PRs)
    "112736-113201",  # pr-112736 (2 PRs)
    "118029-118045-118099-118181-118207-118221-118225-118249-118292-118303-118305-118309-118327-118329-118346-118352-118374-118417-118454-118493-118523-118603-118617-118655-118679-118740-118785-118799",  # pr-118029 (28 PRs)
    "118834-118887-118910-118934-119022-119078-119129-119161-119173-119320-119323-119326-119331-119336-119397-119432-119536-119644-119694-119797",  # pr-118834 (20 PRs)
    "119819-119921-120465-120783-120802-120899-120929-120964",  # pr-119819 (8 PRs)
]

for _ni in _BUNDLE_NIS_GRAFANA_GO:
    Instance.register("grafana", _ni)(GrafanaGolang)
