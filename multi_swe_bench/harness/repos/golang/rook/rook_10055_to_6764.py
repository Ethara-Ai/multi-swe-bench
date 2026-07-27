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


class RookEra1ImageBase(Image):
    """rook era 1 (PRs 6764-9976, v1.5->1.8 + early 1.9; effective range after PR 10055
    moved to era 2 — its fix-side deps need go>=1.17 build tags): go.mod `go 1.13`-`1.16`.
    Pure-Go Kubernetes/Ceph operator — CGO disabled, `go test` unit suite
    under `pkg/` + `cmd/`. Built with Go 1.16 (>= every go.mod in this era).
    `-buildvcs` is intentionally NOT set: it predates Go 1.18."""

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
        return "golang:1.16"

    def image_tag(self) -> str:
        return "base-go116"

    def workdir(self) -> str:
        return "base-go116"

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
ENV GOFLAGS="-mod=mod"
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


class RookEra1ImageDefault(Image):
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
        return RookEra1ImageBase(self.pr, self._config)

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


class ROOK_10055_TO_6764(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RookEra1ImageDefault(self.pr, self._config)

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


_BUNDLE_NIS_ERA1 = [
    "6764-6766-6770-6771-6777-6778-6780",
    "6774-6786-6787-6794-6802-6810-6820-6821-6828-6829-6842-6843-6844-6852-6858-6859",
    "6855-6901-6902-6904-6905-6909-6914-6919-6921-6923-6927-6929-6931-6941-6942-6944-6953-6954-6956-6960-6962-6965",
    "6969-6970-6979-6986-6987-7000-7009-7012-7014-7039-7043-7045-7053-7057-7061-7067-7072-7078-7080-7085-7088-7090-7093-7095-7096-7097",
    "7098-7104-7114-7115-7117-7118-7127-7130-7131-7136-7137-7143-7145-7148-7149-7155-7159-7161-7163-7166-7167-7181-7184-7186-7187-7195-7197-7201-7204-7205-7208",
    "7215-7216-7225-7231-7232-7237-7243-7245-7246-7250-7251-7253-7254-7255-7260-7263-7269-7272-7274-7281-7291-7293-7300-7301-7309-7310-7313-7316",
    "7285-7456-7470-7477-7490-7492-7508-7509-7514-7538-7541-7561-7612-7616-7625-7641-7643-7649-7651-7656",
    "7308-7314-7324-7326-7329-7339-7341-7343-7345-7348-7350-7380-7383-7384-7408-7410-7420-7421-7422-7437-7438",
    "7665-7666-7668-7679-7684-7689-7693-7697-7698-7699-7702-7705-7714-7721-7725-7729-7730-7738-7740-7741-7743",
    "7737-7739-7766-7791-7800-7810-7822",
    "7748-7749-7755-7758-7763-7765-7767-7771-7772-7774-7775-7780-7781-7784-7785-7786-7792-7799-7805-7812-7821-7825-7828-7832-7833-7834-7836-7840-7841-7846-7848-7852-7853",
    "7802-7868-7895-7897-7900-8049",
    "7864-7865-7869-7879-7880-7882-7888-7892-7893-7896-7901-7902-7921-7929-7931-7932-7939-7944-7959-7960-7961",
    "7978-7979-7980-7981-7990-7992-8003-8004-8009-8010-8014-8018-8024-8027-8030-8035-8036-8044-8048-8051-8053",
    "8054-8064-8091-8094-8095-8096-8101-8104-8105-8108-8110-8111",
    "8075-8079-8088-8099-8124-8131-8133-8134-8150-8152-8158-8159-8161-8162-8167-8171-8174-8177-8178-8186-8193-8194",
    "8197-8201-8212-8218-8221-8223-8225-8228-8229-8240-8241-8246-8247-8248-8249",
    "8257-8258-8259-8260-8263-8270-8281-8283-8295-8300-8305-8312-8313-8327-8334-8341-8342-8346-8354-8357-8365-8366-8367-8368",
    "8387-8416-8505-8523-8525-8542-8544-8554-8564-8572",
    "8454-8477-8485-8488-8500-8503-8507-8508-8509-8518-8522-8524-8533",
    "8543-8545-8553-8555-8556-8563-8569-8570-8581-8586-8589-8591-8592-8595-8599-8602-8604",
    "8585-8594-8601-8605-8627-8630-8703-8719-8811",
    "8606-8614-8625-8626-8628-8631-8632-8639-8642-8644-8645-8656-8658-8660-8661-8662-8664-8672-8679-8680-8681-8682-8684-8688-8689",
    "8697-8702-8704-8705-8706-8710-8717-8720-8747-8748-8760-8764-8775-8780-8786-8789-8797-8801-8803-8809-8810-8812-8814-8817-8824",
    "8815-8903-8904-9138-9207-9210",
    "8827-8834-8835-8839-8842-8844-8847-8849-8855-8856-8857-8864-8871-8872-8873-8880-8887-8888-8890-8902-8906-8910-8915-8921-8922-8929-8930-8936",
    "8934-8937-8941-8945-8950-8956-8957-8960-8961-8964-8968-8969-8970-8972-8973-8977-8983-8986-8991-8992-8997-8998-9000-9013",
    "9014-9018-9019-9026-9038-9046-9050-9053-9057-9060-9067-9069-9071-9072-9080-9085-9093-9095-9101-9102-9105-9108-9109-9113",
    "9143-9222-9229-9237-9239-9240-9245-9247-9249-9255-9269-9271-9274-9289-9293-9303",
    "9308-9329-9339-9353-9416-9448-9450",
    "9389-9403-9404-9405-9406-9407-9408-9409-9412-9420-9421-9425-9426-9428-9435-9437-9447-9449-9451",
    "9456-9461-9463-9472-9475-9476-9477-9479-9480-9481-9490-9517-9518-9520-9521-9523-9531-9537-9538",
    "9460-9478-9527-9592-9599-9634",
    "9540-9541-9548-9561-9562-9572-9573-9574-9587-9593-9595-9597-9598-9600-9601-9606-9607-9610-9611-9617-9621-9628-9629-9630-9633-9635",
    "9631-9641-9644-9650-9656-9657-9662-9664-9666-9668-9674-9676-9677-9683-9699-9700",
    "9704-9707-9708-9717-9722-9723-9725-9726",
    "9730-9737-9738-9739-9744-9745-9752-9753-9756-9759-9761-9763-9775-9779-9780-9783-9785-9793-9795-9796-9798-9801-9803-9805",
    "9811-9812-9813-9815-9818-9830-9833-9836-9848-9849-9850-9861-9869-9874-9875-9876-9877-9886-9889-9892",
    "9895-9896-9900-9907-9914-9932-9935-9949-9954-9955-9959-9965-9966",
    "9976-9984-9991-10011-10019-10028-10037-10097-10129",
]

for _ni in _BUNDLE_NIS_ERA1:
    Instance._registry[f"rook/{_ni}"] = ROOK_10055_TO_6764
