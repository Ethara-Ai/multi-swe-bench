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


class Traefik12845To4694ImageBase(Image):
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
        return "base-era-b"

    def workdir(self) -> str:
        return "base-era-b"

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
        # runs per-PR at the literal base.sha (see Traefik12845To4694ImageDefault).
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


class Traefik12845To4694ImageDefault(Image):
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
        return Traefik12845To4694ImageBase(self.pr, self._config)

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


@Instance.register("traefik", "traefik_12845_to_4694")
class Traefik12845To4694(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Traefik12845To4694ImageDefault(self.pr, self._config)

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
# --- routes to Traefik12845To4694. 110 bundles.
_BUNDLE_NIS_B = [
    "10049-10052-10065-10067-10068-10077-10078-10082-10083-10085-10086-10087-10090-10096-10115-10121-10122-10126-10128-10132-10137-10138-10142-10157-10161-10163",
    "10239-10243-10245-10247-10249-10261-10276-10287-10292-10293-10294-10296-10298-10313-10315-10317-10320-10325-10326-10327-10331-10334-10347-10350-10360-10361-10367-10369-10378-10384-10386-10392-10393-10395-10397-10398-10400-10405-10415-10420-10424",
    "10581-11095-11109-11115-11130-11133-11141-11148",
    "10599-10600-10602-10603",
    "10944-10955-10958-10961-10977",
    "11359-11545-11557-11571-11577-11599-11603-11608-11610-11611-11632-11633-11634-11639-11642-11649-11650",
    "11492-11495-11496-11497-11501",
    "11668-11676-11682-11684-11689-11690-11691-11692-11693",
    "11872-11907-11911-11918-11924-11929",
    "11936-11949-11960-11983-11993-11996-12007-12015",
    "12018-12083-12143-12156-12162-12205",
    "12197-12496-12516-12532-12540-12542-12550",
    "12237-12262-12268",
    "12291-12296-12304-12308-12310-12319-12340-12360-12363",
    "12330-12365-12367-12373-12384-12391-12426-12434",
    "12435-12457-12474",
    "12638-12642-12648",
    "12647-12649-12676-12690-12692-12694-12717-12718",
    "12756-12761-12764",
    "12787-12788-12803-12804-12828",
    "5391-5397-5401-5407-5409-5411-5423-5425-5428-5430-5431-5437-5438-5439-5448-5450-5452-5461-5467-5481-5485-5486-5490-5491-5492-5493-5494-5508",
    "5766-5832-5834-5835-5836-5837-5839-5840-5860-5862-5867-5868-5872-5874-5879-5884-5895-5903-5920-5927",
    "5819-5902-5921-5930-5935-5939-5945-5949-5963-5964-5967-5971-5976",
    "6054-6408-6413-6422-6423-6484-6515",
    "6097-6140-6147-6150-6154-6163-6170-6192-6194-6195-6198-6199-6202-6215",
    "6218-6219-6225-6233-6244-6251-6262-6265-6267-6269-6274-6279-6281-6286",
    "6414-6461-6664-6881-6951-7094-7110-7122-7203-7215-7226-7263-7299-7320-7335-7345-7346-7359-7392-7401-7402-7405-7415-7416-7449-7453-7458-7460-7464-7472-7483-7512-7529-7574-7599-7602-7604-7615-7653-7670-7671-7677-7678-7689-7701-7703-7712-7727-7742-7744-7747-7750-7754-7760-7764-7765-7780",
    "6483-6900-7047-7049-7064-7066",
    "6522-6524",
    "6538-6543-6554-6557",
    "7016-7019",
    "7034-7037-7039-7042",
    "7068-7101-7109-7115",
    "7071-7079",
    "7124-7125-7131-7134-7156-7172-7178-7181-7206-7214-7230-7233-7237-7238-7247-7248",
    "7355-7375-7387-7390-7394-7397-7398-7399-7400-7403-7422-7423-7426-7427-7429-7434",
    "7433-7782-7783-7787-7793-7797-7799-7805-7810-7811-7822-7824-7839-7840-7841",
    "7521-7573-7577-7583-7586-7588",
    "7535-7595-7596-7609-7610-7616-7620-7625-7649",
    "7655-7659-7663-7675-7680-7687",
    "7808-7823-7858-7860-7865-7879-7888-7891-7894-7898",
    "7847-7849-7851-7852",
    "7899-7913-7914-7922-7925-7928-7933-7940",
    "7904-7909",
    "8185-8254-8274-8286-8290-8307",
    "8309-8315-8319-8321",
    "8322-8329-8335-8348-8357-8358-8360",
    "8323-8731-8739-8740-8746-8752-8759-8761-8764-8773-8774-8776",
    "8350-8369-8370-8372-8374-8383-8385",
    "8368-8381-8392-8394-8395-8399-8402-8408-8409-8410-8413-8416",
    "8596-8635-8636-8637-8649-8650-8652-8653",
    "8880-8886-8890-8891-8893-8894",
    "9151-9163-9171-9177-9179-9180-9182-9192-9197-9199-9200-9203-9221-9222-9224-9227-9241-9243-9244-9245",
    "9264-9270-9274-9277-9283-9284-9286-9287-9288-9295-9300-9301",
    "9268-9306-9312-9313-9314-9327-9328",
    "9579-9590-9595-9598-9609-9620-9621-9627-9631-9645-9651-9661-9671-9673-9683-9685-9687-9692-9698-9700-9701-9705-9715",
    "9737-9741-9742-9743-9749-9750-9752-9758-9777-9786-9795-9796",
    "10171-10172-10196-10197-10206-10209-10220-10222-10224-10226-10229-10230-10242-10248-10255-10268",
    "10262-10828-10873-10887-10903-10907-10911-10920-10926-10933-10935-10948",
    "10416-10428-10431-10437-10439-10443-10444-10449-10452-10453-10459-10470-10473-10482-10491-10496-10499-10502-10506-10508-10510-10512-10515-10517-10518-10532-10535-10536-10538-10539-10540-10541-10550-10554-10555-10565-10569-10572-10574-10583-10584",
    "10615-10621-10629-10630-10633-10649-10650-10658-10665-10701-10710-10746",
    "10635-10695-10748-10751-10752-10762-10768-10772-10775-10785-10795",
    "10813-10814-10825-10830-10834-10846-10866-10867-10868",
    "10819-10981-10983-10989-11001-11015-11031-11035-11040-11048-11049-11060-11064-11072-11073-11077-11090",
    "11129-11159-11205-11217-11231",
    "11220-11226-11253-11254-11261-11262-11263-11267-11269-11275-11276-11280-11281-11282-11289",
    "11344-11364-11365-11368-11370-11383-11390-11397-11398",
    "11418-11435-11453-11477-11487-11489",
    "11528-11530-11534-11536-11537-11566",
    "11702-11716-11721-11724-11744-11756-11761-11768-11777-11792-11797",
    "11764-11773-11811-11812-11815-11819-11830-11858",
    "12329-12501-12573-12581-12584-12587-12591-12604-12618",
    "12845-12851-12863-12882",
    "4694-4789-4984-5022-5055-5180-5253-5336-5354-5395-5402-5464-5466-5619-5637-5650-5661-5711-5721-5725-5749-5752-5815-5826-5841-5844-5845-5846-5861-5863-5887-5900-5913-5923-5928-5929-5931-5937-5960-5977-5983-5984-5985-5986-5987-6002",
    "5037-5952-6283-6293-6304-6305-6306-6307-6311-6324-6328-6330-6333-6345-6347-6352-6364-6365-6371-6372-6376-6380-6402-6405",
    "5147-5241-5698-5738-5870-5875-5885-5899-5909-5910-5915-5933-5950-5969-5980-5996-6004-6036-6048-6055-6080-6085-6107-6138-6148-6152-6160-6171-6172-6190-6204-6206-6212-6216-6226-6248-6255-6270-6291-6300-6302-6309-6313-6325-6327-6329-6348-6359-6360-6379-6409-6410-6416-6417-6426-6428-6429-6430-6432-6434-6440-6444-6447-6459-6460-6464-6466-6467-6468-6469-6471-6472-6475-6476-6477-6478-6491-6498-6502-6504-6510-6512-6517-6519-6526-6532-6533-6549-6564-6569-6582",
    "5224-5600-5623-5625-5633-5636-5641-5644-5654-5655-5658-5660-5664-5666-5669-5674-5683-5694-5706-5707-5712-5714-5717-5720-5722-5724-5734-5735-5742-5743",
    "5233-5500-5504-5516-5517-5519-5520-5523-5527-5528-5529-5531-5536-5539-5540-5549-5558-5569-5570-5572-5575-5578-5579-5584-5585-5586-5590-5594-5601-5605-5607-5608-5612-5613-5617",
    "5433-5737-5746-5754-5759-5773-5775-5776-5787-5794-5795-5798-5806-5812-5817-5818-5820-5831",
    "6010-6016-6017-6021",
    "6019-6022-6028-6030-6037-6046-6051-6058-6070-6072-6078-6079-6087-6115-6116-6121-6135-6137",
    "6568-6604-6696-6749-6754-6779-6811-6822-6831-6875-6921-6946-6976-7022-7041-7052-7055-7056-7058-7060-7065-7072-7083-7086-7087-7107-7116-7117-7139-7145-7160-7169-7175-7186-7198-7199-7200-7201-7204-7218-7219-7246-7249-7255-7257-7260-7261-7262-7264-7269-7271-7272-7286-7287-7288-7289-7290-7294-7296-7304-7309-7311-7327-7331-7332-7333-7334",
    "6588-6593-6595-6606-6611-6619-6624-6647-6648-6650-6660-6663-6671-6673-6675-6681-6683-6689-6691-6698-6705-6713-6717-6719-6720-6727-6734-6738-6741-6744-6745-6753",
    "6633-8182-8250-8476-8666-8689-8736-8757-8760-8777-8778-8783-8793-8802-8821-8825-8832-8837-8848-8849-8865-8868-8869-8877-8879-8883-8899-8900-8958-8984-9023-9024-9031-9045-9046",
    "6747-6750-6752-6762-6763-6768-6769-6775-6788-6792-6795-6797-6806-6815-6817-6819-6827-6836-6845-6847-6863-6867-6874-6876-6878-6899-6902-6904-6908-6928-6936-6938-6939-6952-6964-6967-6977-6979-6983-6988-6990-7002-7008-7011",
    "6924-6982-7407-7478-7510-7549-7593-7645-7646-7668-7721-7724-7728-7746-7748-7761-7766-7773-7774-7779-7781-7789-7794-7795-7813-7815-7833-7859-7901-7915-7921-7944-7963-7968-7969-7971-8024-8054-8057-8058-8068-8076-8084-8087-8089-8103-8105-8107-8114-8128-8160-8161-8164-8169-8184-8187-8198-8200-8202-8210-8221-8224-8232-8233-8236-8245-8253-8263-8268-8269-8280-8281-8282-8284-8285-8287-8288-8296-8313-8314-8325-8326-8333-8339-8355-8356-8361-8362-8364",
    "7370-7410-7424-7435-7436-7441-7444-7447-7450-7451-7469-7477-7480-7482-7501-7506-7514-7516-7523-7526-7527-7541-7548-7558-7561-7562-7565-7569-7570",
    "7519-7958-8046-8131-8189-8234-8239-8241-8289-8298-8299-8308-8316-8334-8363-8411-8419-8429-8435-8447-8461-8498-8535-8539-8557-8563-8581-8592-8609-8645-8646-8651-8664-8667-8687-8688-8692-8693-8699-8712-8714-8717-8720-8721-8722-8725-8727-8730",
    "7647-7702-7706-7711-7714-7726-7734-7737-7743",
    "7932-7942-7945-7946-7956-7957-7959-7960-7962-7965",
    "7943-7948-7975-7979-7980-7984-7986-7990-7992-7994-8001",
    "7997-8007-8016-8019-8023-8026-8030-8031-8033-8056-8063-8072-8085-8100-8101-8104-8111-8116-8120-8136-8146-8152-8156-8168-8170-8176-8179-8180-8190-8192-8193-8194-8203-8205",
    "8206-8209-8212-8215-8216-8219-8225-8226-8238-8243-8252-8258-8260-8261-8262-8264",
    "8251-8457-8460-8464-8473-8474-8477-8481-8484-8488-8496-8507-8522-8523-8524-8525-8533-8536-8537-8538-8544-8545-8550-8553-8555-8556-8559-8561-8562",
    "8418-8421-8422-8425-8431-8434-8439-8442-8448-8451-8452-8454-8456",
    "8560-8564-8565-8567-8575-8579-8591-8593-8600-8602-8603-8607-8615-8619-8620-8625",
    "8632-9002-9096-9104-9105-9107-9115-9118-9121-9129-9130-9131-9132",
    "8690-8913-8951-9007-9037-9103-9116-9135-9140-9146-9165-9183-9187-9189-9190-9208-9209-9216-9265-9266-9291-9316-9324-9329-9330-9333-9334-9343-9344-9350-9365-9366-9367-9371-9372-9400-9402-9409-9410",
    "8729-8742-8756-8765-8779-8784-8788-8791-8792-8814-8817-8819-8823-8829-8836-8840-8850-8851-8855-8858-8859-8863-8864-8876",
    "8781-8874-8907-8922-8956-8959-8960-8976-8979-9018-9032-9052-9055-9060-9085-9091-9095-9097-9111-9133-9134-9142-9143",
    "8916-8918-8919-8920-8923-8930-8932-8933-8940-8941-8943-8944-8945-8953-8954-8957-8969-8973-8978-8980",
    "9000-9001-9005-9011-9014-9019-9020-9021-9028-9038-9039-9044",
    "9149-9150-9152-9159-9161-9168-9169-9178",
    "9322-9338-9340-9349-9354-9357-9358-9360-9369-9370",
    "9412-9413-9414-9422-9432-9435-9438-9440-9442-9445-9446-9454-9459-9460-9461-9468-9477",
    "9424-9519-9526-9529-9535-9539-9542-9550-9560-9564-9572-9574-9582-9583-9585",
    "9490-9497-9498-9506-9507-9509-9510-9513",
    "9714-9740-9765-9770-9783-9789-9791-9792-9794-9798-9799-9802-9805-9808-9810-9811-9813-9815-9816-9821-9822-9824-9829-9830-9835-9846-9847-9849-9851-9860",
    "9868-9876-9881-9883-9887-9890-9894-9914-9918-9924-9928-9930-9935-9938-9940-9942-9943-9953-9966-9967",
    "9975-9981-10003-10008-10012-10023-10024-10025-10026-10028-10029-10036-10037-10039-10043",
]
for _ni in _BUNDLE_NIS_B:
    Instance.register("traefik", _ni)(Traefik12845To4694)

