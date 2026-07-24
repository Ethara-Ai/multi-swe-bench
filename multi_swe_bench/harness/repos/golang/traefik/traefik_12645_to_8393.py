import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Drop binary file sections from a unified diff.

    ``git apply`` is atomic: a single binary hunk lacking a full index line
    (``Binary files a/x and b/x differ`` or a ``GIT binary patch`` block)
    aborts the WHOLE apply, so with ``set -e`` in *-run.sh the fix stage
    yields zero results and the record is misclassified invalid. Splitting on
    the ``diff --git`` boundary and dropping only the binary sections lets the
    text hunks (the Go test/source changes that carry the f2p/n2p signal)
    apply cleanly.
    """
    if not patch:
        return patch
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    kept = [
        s for s in sections
        if "Binary files " not in s and "GIT binary patch" not in s
    ]
    return "".join(kept)


class Traefik12645To8393ImageBase(Image):
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
        return "base-era-c"

    def workdir(self) -> str:
        return "base-era-c"

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

        # `# syntax` opts this shared era base out of the DockerfileEnhancer,
        # which would otherwise inject `git checkout --detach ${BASE_COMMIT}` +
        # ref-strip + prune HERE, pruning the shared base to a single PR's
        # base.sha and breaking every other PR in the era with "reference is not
        # a tree". The base keeps full history; the anti-reward-hack hardening
        # runs per-PR at the literal base.sha (see Traefik12645To8393ImageDefault).
        return f'''# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
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
'''


class Traefik12645To8393ImageDefault(Image):
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
        return Traefik12645To8393ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _strip_binary_diffs(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _strip_binary_diffs(self.pr.test_patch),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
go build ./... 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
go test -v -count=1 -vet=off ./...
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 -vet=off ./...
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 -vet=off ./...
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

        # Per-PR anti-cheat hardening at the LITERAL base.sha (the shared base
        # keeps full history so every PR's base.sha is reachable). prepare.sh
        # checks out this PR's base.sha; the hardening block then detaches at
        # that literal sha and strips every other ref/reflog so the fix commit
        # is unreachable from git.
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

CMD ["/bin/bash"]
"""


@Instance.register("traefik", "traefik_12645_to_8393")
class Traefik12645To8393(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Traefik12645To8393ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return (
            "bash -c '" + """cd /home/traefik && rm -rf integration && go test -v -count=1 -vet=off ./...""" + "'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '" + """cd /home/traefik && git apply --whitespace=nowarn /home/test.patch && rm -rf integration && go test -v -count=1 -vet=off ./...""" + "'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '" + """cd /home/traefik && git apply --whitespace=nowarn /home/test.patch /home/fix.patch && rm -rf integration && go test -v -count=1 -vet=off ./...""" + "'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        # Go subtests share a base name (get_base_name strips "/sub"); if any
        # subtest failed, the base must not also remain in passed/skipped, or the
        # harness rejects the report ("passed and failed should not overlap").
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- §11b bundle keys: every dash-joined prs_in_bundle for this era
# --- routes to Traefik12645To8393. 44 bundles.
_BUNDLE_NIS_C = [
    "10827-10831-10841-10851-10869-10870",
    "10838-10905-10925-10931-10936-10938-10940-10950-10951",
    "10946-10947-10954-10956-10966-10967-10972-10973-10974-10978-10979",
    "11053-11207-11229-11246-11268-11270-11287-11288-11290-11291",
    "11082-11102-11107-11108",
    "11111-11132-11142-11149-11152",
    "11185-11188-11198-11218-11232-11233",
    "11330-11583-11586-11594-11606-11628-11641-11644-11651-11652",
    "11343-11352-11354-11357-11366-11385-11386-11388-11392-11400-11401",
    "11615-11663-11665-11671-11685-11694-11696-11703-11704",
    "11715-11734",
    "11876-11956-12025-12028-12030-12032-12033-12038-12039-12040-12041-12049",
    "11914-11919-11926-11930-11931",
    "12202-12208-12216-12218-12227-12239",
    "12370-12385-12387-12388-12398-12424-12428",
    "12431-12432-12436-12438-12440-12442-12443-12444-12445-12448-12454-12475-12476",
    "12545-12632-12658-12677-12679-12682-12699-12702-12719-12720",
    "12645-12759-12860-12886-12889-12904-12926-12937-12941",
    "10211-11022-11100-11311-11350-11374-11394-11420-11443-11455-11473-11475-11504-11520-11541-11547-11556-11595-11597-11609-11619-11653-11654-11670-11687-11701-11705-11707-11736-11738",
    "10591-10728-10756-10758-10763-10777-10780-10781-10796-10797",
    "10668-10673-10675-10679-10680-10689-10704-10716-10719-10723-10724-10725-10729-10740-10747-10749",
    "10982-10985-11008-11010-11020-11044-11052-11058-11065-11067-11068-11088-11092-11093",
    "11084-11151-11168-11174-11179-11180",
    "11213-11247-11297-11300-11305-11314-11321-11329",
    "11417-11419-11428-11433-11442-11445-11446-11447",
    "11450-11458-11476-11491-11498-11499-11502-11503",
    "11479-11514-11515-11518-11522-11523-11524-11526-11531-11553-11564-11567-11570",
    "11623-11714-11785-11798-11803-11804-11808-11810-11828-11831-11835-11856-11859-11860",
    "11673-11838-11867-11882-11904-11925-11937-11942-11945-11952-11953-11958-11966-11973-11989-11991-11994-11995-12005-12006-12010-12012-12013-12016-12017-12019-12020",
    "11698-11742-11751-11753-11757-11759-11762-11771-11775-11782-11783-11789-11790-11796-11799-11800",
    "11741-11760-11845-11847-11881-11885-11887-11896-11897",
    "11978-12022-12035-12044-12056-12057-12060-12064-12067-12069-12077-12080-12089-12097",
    "12037-12406-12460-12464-12466-12467-12473-12488-12497-12503-12507-12508-12509-12510-12514-12515-12520-12521-12528-12529-12531-12533-12552-12554",
    "12062-12063-12084-12094-12096-12099-12103-12105-12108-12111-12112-12115-12118-12119-12121-12122-12123-12124-12129-12131-12135-12141-12142-12149-12151-12152-12153-12158-12163-12164-12165-12166-12168-12177-12185-12186-12187-12189-12198-12200-12201-12206-12207",
    "12219-12231-12254-12256-12258-12266-12267-12269-12271",
    "12238-12403-12405-12429-12430-12479-12482-12505-12541-12553-12556-12570-12571-12574-12575-12594-12600-12601-12603-12605-12611-12612-12616-12617-12636-12639-12643-12644-12652-12653-12655",
    "12283-12288-12289-12297-12298-12309-12311-12315-12318-12324-12328-12331-12333-12335-12341-12351-12352-12361-12364-12366",
    "12376-12818-12840-12847-12856-12857-12872-12873-12876-12880-12883-12885",
    "12567-12651-12671-12683-12684-12711-12713-12729-12731-12740-12744-12753-12754-12760-12763-12765-12770-12771",
    "12599-12779-12784-12799-12801-12806-12807-12808-12817-12822-12825-12830-12833",
    "8393-10610-11203-11589-11637-11643-11674-11708-11711-11719-11731-11752-11758-11806-11813-11817-11844-11855-11857-11861-11863-11865-11870-11874-11893-11898-11899-11906-11908-11920-11927-11932-11933-11934-11935",
    "9747-10776-10816-10833-10906-10917-10921-10943-10952-10970-10975-10980-10987-10995-10997-11009-11019-11032-11042-11047-11051-11055-11056-11066-11070-11075-11110-11122-11124-11131-11134-11147-11153-11154-11164-11167-11169-11170-11176-11181-11182-11186-11189-11191-11193-11199-11209-11212-11219-11222-11235-11236",
    "9807-9871-9897-9933-10040-10131-10278-10362-10399-10418-10467-10479-10567-10571-10655-10660-10664-10667-10682-10709-10714-10717-10750-10761-10766-10771-10778-10784-10789-10800-10802-10811-10815-10829-10832-10835-10840-10844-10848-10849-10850-10853-10856-10857-10860-10862-10871-10872-10876-10881-10893-10897-10901-10902-10904",
    "9946-10645-11238-11351-11448-11900-11939-11940-11976-11977-12002-12021-12029-12050-12051-12061-12065-12085-12095-12120-12130-12136-12140-12145-12160-12167-12179-12188-12191-12193-12210-12211-12212-12215-12235-12242-12243",
]
for _ni in _BUNDLE_NIS_C:
    Instance.register("traefik", _ni)(Traefik12645To8393)

