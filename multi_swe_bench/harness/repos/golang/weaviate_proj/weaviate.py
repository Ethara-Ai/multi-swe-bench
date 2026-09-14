import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# === MITM proxy / CA-cert scaffolding — DEFINED HERE, NOT imported from image.py ===
# Manager instruction (supersedes PIPELINE.md 8.2 for this repo): weaviate carries its own
# MITM block instead of relying on DockerfileEnhancer injection. The `# syntax` directive
# on the base Dockerfile opts this repo out of that auto-injection, so the constants below
# are the SINGLE source of MITM for weaviate images — no duplicate ARG/ENV is emitted.
# Text is kept byte-identical to image.py's canonical constants so the 8 audit still
# matches verbatim; the git-strip hardening still comes from the canonical
# Image._HARDENING_BLOCK (inherited) so it cannot drift.
_SYNTAX_DIRECTIVE = "# syntax=docker/dockerfile:1.6"

_MITM_PROXY_ARGS = """ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt\""""

_MITM_ENV_BLOCK = """ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}"""

_MITM_CERT_SYMLINKS = """RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"""

# Build-time MITM CA install. Latent in image.py (2a: "defined but NOT injected"); wired
# here so the MITM CA is actually trusted at build. required=0 -> builds without the secret
# still succeed. Supply with: docker build --secret id=mitm_ca,src=<ca.crt>
_MITM_CA_MOUNT = """RUN --mount=type=secret,id=mitm_ca,required=0 \\
    if [ -f /run/secrets/mitm_ca ]; then \\
        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \\
    fi"""


# Base keeps FULL git history (PIPELINE.md 3: "Light hardening only ... the PR layer does
# the strict hardening"). The strict git-strip MUST NOT run here: the base image is a single
# shared tag, so pruning to one BASE_COMMIT deletes every other PR's base commit and their
# prepare.sh dies with "fatal: unable to read tree".
_BASE_LIGHT_HARDENING = """RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault \"\""""


class WeaviateImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

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

        return f"""{_SYNTAX_DIRECTIVE}

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{_MITM_PROXY_ARGS}

{_MITM_ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_MITM_CERT_SYMLINKS}

{_MITM_CA_MOUNT}

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

{_BASE_LIGHT_HARDENING}

WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class WeaviateImageDefault(Image):
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
        return WeaviateImageBase(self.pr, self.config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm GOCACHE by COMPILING every package. Deliberately NOT `go test ./...`:
# executing the full weaviate suite here cost 45-65 min per image per arch at build
# time. The suite is meant to run in the instance phase (run.sh / test-run.sh /
# fix-run.sh), which still benefits from the cache this populates.
go build ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
go test -v -count=1 ./...

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

        # 4: strict git-strip runs in the PR layer, pinned to the LITERAL base.sha
        # (not ${BASE_COMMIT}) so each PR hardens against its own commit.
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("weaviate", "weaviate")
class Weaviate(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WeaviateImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Data-derived from the delivered dataset (weaviate__weaviate_78_shippable.jsonl).
# Instance.create() routes on f"{org}/{number_interval}", so every bundle value the
# JSONL carries must be a registered key. Single-era repo -> all keys map to Weaviate.
# Regenerate whenever the bundles change (PIPELINE.md 11b).
_BUNDLE_NIS_Weaviate = [
    "6997-7313-7322-7334-7341-7342-7343-7345-7349-7352-7353-7354-7358-7361-7362-7367-7375-7376-7377-7378-7379-7380-7382-7387-7388-7389-7390-7396",
    "7057-7285-7293-7296-7300-7305-7306-7307-7311-7314-7315-7319-7321-7323-7325-7326-7330-7332-7333",
    "7240-7259-7262-7265-7266-7267-7268-7270-7271-7275-7276-7277-7279-7283",
    "7503-7506-7508-7509-7515-7519-7520-7521-7522",
    "7545-7546-7552-7562-7564-7565-7566-7568-7569-7576-7588-7591-7601-7616-7618-7619-7622-7629-7630-7632-7640-7646-7649-7652-7658-7659-7663-7668-7680-7683-7686-7687-7688-7689-7690-7692-7693-7694-7701-7702-7727-7730-7735-7768-7770-7772-7775",
    "7656-7671-7675-7695-7704-7719-7729-7732-7738-7759-7776",
    "7751-7778-7801-7809-7815-7817-7831-7838-7847",
    "7781-7783-7791-7793-7797-7798-7803-7804-7805-7806-7808-7813-7814-7818-7824-7825-7828-7830-7837-7839-7841-7843-7844-7845-7850-7857-7858-7861-7863-7869-7891-7894-7901-7903-7905-7907",
    "7826-7833-7848-7851-7856-7866-7872-7880-7886-7896-7904-7914-7921-7930-7951-7958",
    "7836-7853-7864-7888-7895-7912-7926-7950-7955-8001-8030-8031",
    "8075-8080-8081-8085-8095-8096-8098-8100-8101-8102-8103-8104-8105",
    "8077-8082-8099-8108",
    "8086-9050-9062-9067-9086-9087-9103-9108-9109-9119-9122-9136-9141-9150",
    "8109-8126-8130-8133-8154-8170-8192-8200-8211-8214",
    "8193-8430-8434-8443-8450-8452-8453-8458-8469-8478-8484-8488-8490-8500",
    "8194-8234-8243",
    "8217-8221-8231-8235-8238-8248-8250-8279-8291-8294-8298-8313-8419-8420-8461-8471-8475-8477-8502-8506-8507-8542-8543-8566",
    "8252-8565-8570-8573-8579-8587-8589-8600-8610-8617-8627-8628-8640-8643-8653-8666-8672",
    "8345-8551-8552-8569-8571-8578-8584-8590-8594-8597-8598-8599-8614-8630-8638-8639-8647-8652",
    "8358-8499-8520-8531-8548-8554-8561-8563-8564",
    "8392-8410-8424-8427-8428-8429-8433-8436",
    "8438-8445-8466-8480",
    "8491-8505-8508-8511-8512-8513-8514-8525-8538-8544-8545",
    "8616-8832-8930-8945-8949-8956-8960-8976-8990-8993-9005-9006-9014-9017-9021-9022-9023-9025-9030-9036",
    "8658-8659-8660-8669-8675-8689-8698-8700-8703-8706-8707-8708-8709-8722-8723",
    "8799-8842-8861",
    "8834-8835-8849-8854-8876-8883",
    "8938-8948-8953-8962-8970-8971-8977-8979-8989-9004-9020-9024-9028-9033",
    "9078-9095-9097-9110",
    "9104-9401-9443-9477-9494-9498-9505-9511-9514-9516-9518-9522-9523-9535-9537-9539-9546-9556",
    "9111-9113-9128-9137-9142-9145-9149-9153-9165-9168-9170-9174",
    "9116-9117-9126-9151-9152-9158-9160-9169-9171-9172-9176-9177-9184-9186-9188-9191",
    "9146-9175-9180-9197-9201-9210-9211-9214",
    "9163-9194-9195-9198-9204-9206-9208",
    "9179-9207-9548-9623-9628-9632-9633-9635-9643-9650-9657-9673-9676-9685-9689-9700-9721-9722-9725-9727-9729-9731-9734-9740",
    "9192-9196-9215-9220-9221-9225-9245-9246-9248-9259-9261-9277-9280-9288-9299",
    "9240-9297-9356-9358-9362-9365-9367-9370-9373-9376-9380-9382-9390-9397-9407-9414-9417-9426-9430-9441-9453-9454-9465-9469",
    "9257-9301-9317-9323-9374-9375-9392-9394-9395-9396-9410-9419",
    "9319-9357-9361-9363-9364-9383-9398-9406-9416-9427-9433-9435-9446-9452-9459-9474-9479",
    "9399-9502-9506-9531-9533-9543",
    "9423-9448-9458-9468-9471-9476-9481",
    "9472-9487-9489-9495-9601",
    "9551-9557-9563-9565-9570-9571-9574",
    "9552-9558-9568-9585",
    "9581-9584-9591-9596-9598-9603-9610",
    "9625-9631-9639-9645-9662-9679-9681-9703-9717-9724-9744-9757",
    "9694-9839-9844-9865-9867-9868",
    "9807-9815-9820",
    "9809-9825-9838-9847-9848",
    "9811-9816-9821-9823-9828-9830-9842-9851",
    "9849-9857-9866-9876",
    "9850-9897-9910-9917-9928-9956-9973",
    "9854-9870-9878",
    "9874-9880-9881-9900-9923-9936-9947-9959-9964-9975-9984",
    "9877-9888-9890",
    "9879-9895",
    "9883-9884-9896",
    "9886-9894-9907-9912-9921-9926-9927-9933-9946-9954-9968-9969-9971",
    "9974-9979-9997-10002-10014-10024-10043-10046-10055-10067-10086",
    "9987-9998-10006-10016-10022-10034-10044-10063-10069",
    "10008-10009-10012-10035-10045-10053-10056-10082-10085",
    "10178-10187-10192-10200-10206-10221",
    "10199-10356-10389-10390-10396-10403-10405-10437-10443-10455-10457-10467-10493-10495-10497-10498-10511-10515-10521-10522-10525-10532-10546",
    "10213-10220-10226-10229-10230-10238-10240-10241-10242-10243-10266",
    "10224-10228",
    "10308-10315-10327-10329-10335",
    "10318-10332-10337",
    "10540-10673-10678-10686-10698-10704-10706-10713-10715-10716-10719-10725-10733-10736-10741-10755-10757-10759-10766-10788-10791-10800-10806-10808-10818-10827-10833-10844-10845-10846",
    "10633-10677-10687-10708-10721-10729-10742-10751-10758-10764-10765-10772-10797-10803-10807-10819",
    "10696-10813-10885-10899-10919-10924-10930",
    "10784-10828-10831-10855-10856",
    "10804-10820-10829-10830-10843-10852-10865-10866-10869",
    "10851-10857-10858-10868-10876-10878-10882-10886-10893-10897-10898-10900-10903-10907-10909-10916-10918-10922-10923-10950-10951-10977-10978-10982-10988-10990-10992-10997-11001-11003-11005-11025-11033-11035-11044-11045-11047",
    "10859-10877-10883",
    "11008-11028-11036-11049",
    "11037-11054-11058-11068-11070",
    "11041-11042-11121-11127-11142-11153-11155-11159-11162-11172-11175-11193-11197-11199-11204-11208-11218-11222-11232-11234-11242-11254-11255",
    "11059-11077-11082-11093-11101-11105-11107-11111-11114-11116-11118-11147-11150-11186-11188",
]

for _ni in _BUNDLE_NIS_Weaviate:
    Instance.register("weaviate", _ni)(Weaviate)
