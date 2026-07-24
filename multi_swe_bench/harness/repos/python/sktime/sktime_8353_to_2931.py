import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v output anchored on the trailing `<STATUS> [ NN%]` so
    parametrized node ids with internal spaces/brackets are captured whole."""
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


class SktimePy312ImageBase(Image):
    """sktime era 3 (PRs with requires-python `<3.13` / `<3.14`; releases
    0.24->0.40, 2024-2026). Python 3.12 covers `>=3.8,<3.13`, `>=3.9,<3.13`,
    `>=3.9,<3.14`, `>=3.10,<3.14`. Routing is by python_requires at base
    SHA (sktime's parallel release branches make PR# unreliable as era
    discriminator)."""

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
        return "python:3.12-slim"

    def image_tag(self) -> str:
        return "base-py312"

    def workdir(self) -> str:
        return "base-py312"

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
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential && rm -rf /var/lib/apt/lists/*

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


class SktimePy312ImageDefault(Image):
    """Per-PR image: checkout base commit, install sktime + dev extras
    (pytest comes from [dev] in all sktime versions), run targeted pytest."""

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
        return SktimePy312ImageBase(self.pr, self._config)

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
timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -5 \\
    || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -5 \\
    || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -5 || true
python -m pytest --version 2>&1 | head -1 || pip install --no-cache-dir pytest pytest-xdist || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.npy' --exclude='*.npz' --exclude='*.parquet' --exclude='*.pkl' \\
    --exclude='*.joblib' --exclude='*.h5' --exclude='*.hdf5' --exclude='*.arff' \\
    --exclude='*.tsv' --exclude='*.tsf' --exclude='*.tar.gz' --exclude='*.xlsx' \\
    --exclude='*.mat' --exclude='*.xls' --exclude='*.nc')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/(setup\\.py|pyproject\\.toml|setup\\.cfg|requirements)' /home/test.patch 2>/dev/null; then
    timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -3 || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \\
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.bin' \\
    --exclude='*.npy' --exclude='*.npz' --exclude='*.parquet' --exclude='*.pkl' \\
    --exclude='*.joblib' --exclude='*.h5' --exclude='*.hdf5' --exclude='*.arff' \\
    --exclude='*.tsv' --exclude='*.tsf' --exclude='*.tar.gz' --exclude='*.xlsx' \\
    --exclude='*.mat' --exclude='*.xls' --exclude='*.nc')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(setup\\.py|pyproject\\.toml|setup\\.cfg|requirements)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 pip install --no-cache-dir -e ".[dev]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e ".[tests]" 2>&1 | tail -3 \\
        || timeout 600 pip install --no-cache-dir -e . 2>&1 | tail -3 || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/sktime/' /home/test.patch \\
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \\
    | grep -E '(_test\\.py$|/tests/.+\\.py$)' \\
    | grep -v '__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
python -m pytest $EXIST -v --no-header --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
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


class SKTIME_8353_TO_2931(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SktimePy312ImageDefault(self.pr, self._config)

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


_BUNDLE_NIS_ERA3 = [
    "2931-4891-4931-5304-5313-5314-5329-5338-5375-5389-5395-5398-5409-5412-5421-5422-5424-5425-5427-5428-5430-5434-5437-5439-5443-5453-5455-5457-5459-5461-5464-5466-5469-5470-5471-5472-5473-5474-5478-5482-5483-5486-5487-5490-5492-5494-5497-5499-5500-5508-5509-5511-5513-5514-5521-5522-5524-5526-5528-5529-5531-5533",
    "3629-6424-6628-6666-6948-6952-7001-7012-7042-7151-7161-7168-7171-7172-7175-7178-7179-7180-7181-7183-7185-7193-7195-7199-7203-7204-7206-7208-7212-7221-7223-7225-7227-7230-7235-7241-7244-7245-7251-7260-7261-7262-7264-7265-7267-7272-7278-7281-7283-7284-7290-7292-7293-7296",
    "3916-3978-4596-5426-5465-5504-5516-5536-5585-5590-5633-5638-5639-5652-5654-5656-5657-5664-5670-5672-5673-5676-5678-5680-5681-5685-5686-5687-5688-5689-5690-5694-5695-5697-5698-5699-5700-5705-5707-5708-5709-5710-5711-5713-5714-5716-5717-5721-5724-5725-5726-5730-5733-5734-5737-5741-5742-5744-5747-5748-5749-5750-5751-5752-5753-5754-5756-5759-5760-5762-5764-5769-5770-5771-5772-5777-5779-5780-5782-5784-5786-5787-5792-5793-5795-5798-5799-5800-5801-5802-5803-5807-5808-5811-5812-5813-5815-5816-5818-5824-5825-5836",
    "4016-7087-8062-8373-8493-8494-8496-8497-8500-8504-8507-8510-8511-8513-8514-8516-8520-8527-8529-8534-8536-8539-8541-8546-8548-8550-8552-8554-8556-8561-8564-8571-8572",
    "4806-6958-7231-7268-7269-7288-7314-7320-7322-7324-7327-7335-7338-7339-7341-7342-7344-7348-7353-7358-7361-7366-7369-7376-7379-7384-7387-7389-7393-7394-7395-7400-7401-7403-7404-7417-7420-7422-7424-7425-7429-7430-7431-7432-7433-7434-7435-7436-7439-7440-7442-7443-7444-7445-7446-7448-7452-7455-7456-7457-7458-7459-7460-7461-7463-7464-7465-7466-7468-7470-7473-7474-7475-7476-7478",
    "5055-5854-6423-6774-6885-7556-7575-7588-7602-7603-7622-7640-7648-7663-7684-7689-7702-7718-7729-7730-7737-7738-7740-7754-7762-7764-7766-7770-7772-7777-7782-7783-7785-7789-7791-7795-7799-7800-7813-7814-7815-7819-7821-7823-7826-7832-7835-7836-7842-7845-7847-7850-7851-7857-7860-7865-7866-7869-7872-7878-7882-7884-7888-7898-7899-7903-7904-7910-7911-7912-7914-7915-7919-7922-7923-7925-7927-7932-7943-7952-7954-7955-7956-7959-7960-7971-7972-7977-7978-7980-7981-7983-7984-7986-7987-7989-7990-7991-7992-7996-7998-8004-8013-8020-8022-8023-8024-8025-8028-8029-8030-8031-8037-8044-8045-8047-8050-8053-8054-8057-8065-8066-8067-8069-8070-8071-8073-8074-8076-8078-8079-8080-8081-8083-8085-8086-8088-8091-8092-8094-8099-8101-8104-8108-8109-8110-8116-8119-8121-8122-8126-8130-8131-8136-8137-8139-8141-8143-8145",
    "5175-5886-5948-6104-6202-6454-6490-6524-6551-6650-6668-6699-6704-6712-6723-6726-6731-6732-6743-6749-6752-6754-6756-6757-6758-6759-6760-6761-6762-6764-6765-6768-6769-6770-6771-6776-6777-6780-6789-6791-6792-6797-6798-6799-6803-6805-6807-6816-6818-6819-6820-6821-6823-6824-6825-6827-6830-6834-6835-6837-6838-6840-6841-6843-6845-6846-6847-6849-6850-6853-6855-6856-6857-6861-6862-6863-6864-6865-6873-6876-6877-6878-6879-6881-6882-6890-6893-6898-6906-6907-6908-6909-6910-6912-6913-6918-6919-6920-6921-6923-6925-6926-6927-6929-6931-6933-6934-6935-6937-6939",
    "5339-5727-5757-5785-5834-5837-5840-5842-5845-5847-5848-5851-5852-5856-5857-5858-5861-5862-5869-5871-5875-5882-5883-5884-5888-5889-5890-5896-5899-5900-5901-5906-5908-5909-5910-5911-5912-5916-5920-5921-5924-5925-5926-5928-5929-5930-5931-5937-5939-5942-5945-5946-5947-5949-5950-5952-5957-5958-5962-5965-5968-5969-5970-5971-5972-5973-5976-5977-5983-5991-5992-5994-5996-6000-6001",
    "5351-5408-5612-5613-5651-5661-5662-5663-5667-5668-5669",
    "5796-5923-5953-5982-6052-6055-6066-6128-6144-6179-6187-6193-6199-6200-6209-6210-6211-6217-6229-6231-6232-6235-6236-6237-6246-6248-6250-6251-6253-6256-6258-6259-6262-6264-6267-6269-6270-6273-6277-6278-6281-6282-6285-6289-6292-6293-6294-6296-6300-6302-6307-6308-6309-6312-6316-6317-6321-6322-6324-6325-6326-6328-6333-6338-6341-6345-6346",
    "5841-6114-6198-6213-6215-6221-6222-6223-6226",
    "5887-5913-5954-5980-5984-6002-6005-6007-6019-6022-6023-6028-6031-6034-6038-6039-6041-6043-6045-6046-6047-6049-6050-6051-6053-6057-6058-6059-6060-6064-6067-6069-6072-6074-6075-6076-6078-6079-6081-6082-6084-6087-6088-6090-6093-6095-6097-6098-6101-6102-6103-6105-6110-6111-6116-6117-6121-6123-6125-6133-6135-6136-6139-6140-6141-6143-6146-6154-6155-6160-6163-6164-6165-6172-6173-6183-6184-6185-6186-6189-6191-6195-6196-6197-6203-6208-6212",
    "6118-6147-6149-6228-6339-6349-6355-6363-6449-6457-6496-6504-6530-6531-6533-6534-6536-6541-6543-6550-6556-6562-6563-6564-6565-6566-6567-6568-6573-6574-6578-6585-6586-6587-6588-6590-6593-6612-6613-6614-6615-6616-6617-6619-6620-6621-6626-6630-6631-6632-6634-6638-6643-6647-6655-6662-6664-6689-6691-6692-6693-6695-6702-6705-6706-6707-6711-6713-6717",
    "6188-6265-6329-6354-6367-6384-6443-6462-6468-6478-6488-6509-6510-6511-6512-6514-6516-6517-6518-6519-6520-6521-6522-6523-6525-6526-6528",
    "6233-6239-6334-6336-6351-6353-6360-6364-6372-6373-6374-6375-6377-6383-6386-6394-6395-6398-6400-6401-6402-6414-6416-6418-6419-6422-6426-6428-6429-6430-6432-6433-6434-6437-6439-6441-6442-6444-6447-6448-6450-6451-6452-6453-6455-6456-6458-6460-6464-6471-6473-6474-6476-6477-6479-6482-6486-6489-6491-6492-6493-6494-6495-6497-6500-6501-6502-6503-6506-6508",
    "6238-6347-6348-6350-6357-6358-6361-6365-6366-6368-6369-6371",
    "6330-6331-6624-6663-6676-6715-6716-6719-6721-6728-6729-6733",
    "6485-6552-6571-6725-6871-6883-6924-6930-6953-6959-6961-6962-6963-6965-6966-6967-6971-6973-6974-6978-6986-6988-6990-6991-6992-6999-7000-7002-7005-7006-7011-7013-7015-7018-7019-7026-7028-7029-7030-7033-7036-7037-7039",
    "6529-6532-6535-6539-6540",
    "6570-6888-7509-7562-7660-7697-7902-7993-8000-8064-8114-8124-8168-8170-8178-8179-8189-8192-8193-8194-8197-8200-8202-8204-8207-8213-8214-8220-8226-8230-8231-8232-8236-8237-8241-8242-8247-8249-8255-8257-8263-8266-8267-8269-8270-8274-8275-8279-8285-8293-8301-8303-8306-8313-8317-8318-8319-8320-8324-8326-8332-8337-8338-8339-8341-8344-8345-8347-8350-8357-8358-8359-8369-8372-8376-8385-8389-8395-8399-8400-8401-8407-8408-8410-8412-8415-8416-8419-8420-8421-8422-8423-8425-8427-8430",
    "6697-6746-6782-7055-7058-7066-7074-7089-7099-7102-7104-7109-7114-7115-7124-7127-7129-7133-7140-7141-7143-7147-7150-7153-7154-7158-7159-7163-7164-7165-7166-7167",
    "6740-7103-7198-7213-7238-7294-7298-7299-7301-7302-7306-7307-7308-7311-7312",
    "6787-7095-7715-7844-8017-8132-8162-8286-8304-8328-8329-8335-8386-8418-8470-8484-8485-8502-8505-8509-8517-8528-8549-8557-8559-8573-8577-8579-8582-8583-8584-8585-8598-8599-8603-8604-8606-8608-8609-8610-8611-8622-8623-8624-8626-8627-8628-8631-8632-8633-8636-8637-8638-8640-8642-8644-8647-8651-8657-8660-8666-8668-8669-8670-8671-8674-8675-8676-8679-8680-8682-8687-8689-8692-8694-8695-8701-8702-8709-8718-8720-8721-8723-8724-8726-8737-8738-8739-8741-8743-8744",
    "6951-6954-6955-6957",
    "6969-6998-7009-7024-7034-7041-7047-7053-7057-7059-7065-7068-7071-7072-7073-7075-7081-7082",
    "7004-7007-7010-7035-7060-7061-7062-7067-7083-7084-7085-7091-7092-7096-7097-7098",
    "7040-7043-7044-7046",
    "7233-7280-7330-7334-7362-7381-7396-7398-7423-7469-7472-7480-7481-7487-7494-7499-7501-7502-7504-7508-7510-7513-7514-7515-7516-7518-7519-7520-7522-7524-7525-7527-7529-7532-7533-7535-7539-7540-7541-7542-7544-7545-7546-7550-7553-7554-7555-7558-7563-7573-7574-7576-7577-7582-7584-7585-7589-7592-7593-7597-7606-7608-7612-7615-7620-7624-7625-7628-7631-7633-7637-7638-7639-7641-7646-7647-7651-7654-7658-7659-7665-7666-7670-7673-7676-7677-7679-7682-7692-7693-7699-7704-7705-7706-7709-7711-7720-7722-7726-7733-7734-7736-7739-7741",
    "7392-7406-7482-7483-7485-7488-7491-7495-7498-7500",
    "7486-7995-8250-8777-8797-8838-8842-8849-8857-8859-8860-8861-8865-8873-8874-8876-8877-8880-8883-8887-8888-8891-8893-8894-8897-8899-8901-8908-8911-8913-8914-8915-8916-8918-8919-8920-8921-8922-8924-8925-8926-8927-8930-8931-8934-8935-8937-8939-8940-8941-8943-8944-8947-8948-8951-8956-8957-8958-8959-8960-8961-8962-8963-8965-8967-8968-8970-8972-8974-8977-8979-8980-8981-8982-8983-8985-8986-8989-8990-8991-8992-8995-8996-8997-8998-9000-9001-9002-9003-9008-9012-9015-9016-9018-9019-9024-9029-9032-9035-9043-9046-9048-9051-9052-9053-9054-9057-9062-9063-9064-9065-9066-9067-9072-9075-9079-9080-9084-9086-9090-9095-9096-9105",
    "7675-7742-7743-7747-7748-7750-7751-7755-7760",
    "7816-7917-8061-8133-8147-8148-8152-8153-8155-8159-8160-8171-8174-8180-8182-8187-8188",
    "8349-8402-8414-8424-8429-8431-8432-8435-8437-8439-8440-8441-8442-8447-8449-8451",
    "8353-8683-8705-8708-8711-8733-8745-8746-8751-8756-8757-8758-8761-8763-8764-8765-8772-8773-8778-8782-8783-8786-8788-8790-8792-8794-8795-8798-8799-8800-8801-8802-8803-8804-8805-8806-8812-8815-8816-8817-8818-8819-8821-8822-8823-8824-8825-8829-8831-8833-8834-8835-8839-8845-8846-8848-8850-8851-8852-8853-8854-8856",
]
for _ni in _BUNDLE_NIS_ERA3:
    Instance._registry[f"sktime/{_ni}"] = SKTIME_8353_TO_2931
