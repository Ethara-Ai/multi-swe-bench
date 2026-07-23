import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks so `git apply` never aborts on a binary hunk
    with no full-index line. Safe: binary hunks touch no Go source and never
    affect test outcomes."""
    import re as _re
    sections = _re.split(r"(?=^diff --git )", patch, flags=_re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )

class EksctlImageBase(Image):
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
        # eksctl's go.mod `go` directive spans 1.18 (release 0.127) through
        # 1.25.1 (release 0.224). Go is backward compatible, so the newest
        # toolchain in the dataset builds every era -> single base image.
        return "golang:1.25-bookworm"

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

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=auto
RUN git config --global --add safe.directory '*'

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class EksctlImageDefault(Image):
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
        return EksctlImageBase(self.pr, self.config)

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
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Shared helpers for the eksctl run/test/fix scripts.
#
# eksctl has 180+ Go packages; running the whole `go test ./...` per PR is
# wasteful, so tests are scoped to the packages touched by the patches
# (same idea as the weaviate/terraform-provider-azurerm configs).
# `integration/tests/*` need a live AWS account + EKS cluster and cannot run
# inside the harness, so their packages are excluded.

EXCLUDES="--exclude=*.lock --exclude=*.png --exclude=*.ico --exclude=*.mp4 \
--exclude=*.svg --exclude=*.gif --exclude=*.jpg --exclude=*.jpeg \
--exclude=*.webp --exclude=*.pdf --exclude=docs/*"

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --3way $EXCLUDES "$f" \\
    || git apply --whitespace=nowarn --reject $EXCLUDES "$f" \\
    || true
}

# Print the unique Go package directories touched by test.patch + fix.patch
# that exist on disk and are not part of the AWS-dependent integration suite.
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
      | grep -vE '^integration(/|$)' \\
      | sort -u
  ) || true
  for d in $out; do
    if [ -n "$d" ] && [ -d "$d" ]; then
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
  go test -v -count=1 -timeout=1200s $pkgs
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


@Instance.register("eksctl-io", "eksctl")
class Eksctl(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EksctlImageDefault(self.pr, self._config)

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
        # `go test` is not colorized by default, but strip ANSI escapes
        # defensively in case the log was captured through a colorizing tee
        # (eksctl uses Ginkgo, which emits ANSI color codes).
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


# ---------------------------------------------------------------------------
# number_interval bundle routing (prs_in_bundle dash-joined)  -- PIPELINE 11b
# ---------------------------------------------------------------------------
# Raw dataset leaves number_interval empty; delivery sets it to
# "-".join(prs_in_bundle). Single-era repo -> every bundle key routes to the
# one Eksctl class. Original "eksctl-io/eksctl" registration above is kept.
_BUNDLE_NIS_EKSCTL = [
    "5722-6023-6177-6180-6188-6194-6195-6196-6198-6204-6205",
    "5856-6374-6375-6378-6380-6392-6393-6397-6400-6402",
    "6143-6154-6155-6168-6169-6172-6176-6178-6179",
    "6206-6209-6221-6223-6225-6226-6227-6228-6230-6231-6232-6234",
    "6235-6242-6243-6266-6267-6271-6272-6277",
    "6237-6278-6279-6288-6289-6291-6295-6296-6297",
    "6264-6290-6294-6298-6317-6331-6333-6336-6338",
    "6302-6743-7778-7851-7868-7869-7870",
    "6318-6339-6341-6347-6361-6369-6370-6379",
    "6340-6346-6401-6404-6429-6431-6436-6437-6438-6439",
    "6376-6408-6524-6527-6530-6534-6540-6541-6545",
    "6389-6630-6638-6641-6643-6644-6645-6646-6647-6649-6651-6652-6655-6659-6664",
    "6406-6458-6460-6465-6488-6490-6495-6496-6497",
    "6432-6738-6742-6746-6747-6754-6756-6758-6759-6760",
    "6440-6443-6450-6451-6453-6456-6457",
    "6464-6546-6548-6553-6554-6555-6560",
    "6498-6500-6502-6525-6526",
    "6626-6628-6629-6631-6639-6640",
    "6660-6666-6667-6674-6675-6678",
    "6685-6690-6691-6692-6697-6701",
    "6695-6703-6707-6729-6730-6734-6735-6736",
    "6704-6804-6814-6832-6833-6836-6840-6841-6842-6850-6852-6859-6860",
    "6851-6866-6868-6874-6895-6898-6899-6901-6903-6920-6921",
    "6870-7974-7975-7990-7996",
    "6875-6897-6922-6923-6931-6934-6937-6946-6948",
    "6935-7001-7008-7025-7027-7033",
    "6947-6969-6973-6992-6993-6994-6996-6997-6998-7000",
    "6953-6955-6956-6960-6965-6967-6968-6972",
    "7029-7116-7123-7126-7149",
    "7030-7034-7036-7045-7047-7050-7051-7052",
    "7032-7053-7063-7067-7068-7072",
    "7065-7073-7074-7075-7087-7091",
    "7077-7092-7114-7115-7117-7118-7120-7121-7122",
    "7090-7207-7217-7218-7219",
    "7093-7108-7109-7113",
    "7150-7170-7173-7174-7178-7179-7201-7205-7206",
    "7204-7427-7429-7480-7487-7488",
    "7208-7221-7222-7230-7246-7248-7250-7267-7292-7296-7312-7313-7314-7315-7316-7334",
    "7297-7451-7483-7489-7496-7498-7499",
    "7335-7336-7337-7343-7345-7349-7350-7374-7375-7376-7378-7379-7381",
    "7382-7383-7405-7407-7408-7419-7420-7421-7422-7423-7424",
    "7471-7523-7542-7551-7554-7561-7563-7564-7566-7570",
    "7500-7501-7503-7505-7515-7517-7522-7524-7525",
    "7516-7526-7539-7541",
    "7571-7588-7591-7599-7603-7604-7618-7619-7623-7626-7628-7629",
    "7661-7668-7671-7672-7680-7681-7684-7686-7696-7698-7699",
    "7664-7666-7669",
    "7701-7705-7706-7707-7710-7711-7712-7714-7715-7721-7722-7723-7725-7727-7728",
    "7730-7756-7758-7769",
    "7766-7814-7815-7816-7817-7819-7820-7821",
    "7771-7772-7781",
    "7782-7783-7784-7788-7789",
    "7790-7791-7807-7813",
    "7824-7825-7827",
    "7826-7828-7829-7834-7838-7850-7866",
    "7874-7878-7879-7881-7884",
    "7883-7885-7886",
    "7899-7901-7918-8021-8022-8047-8055-8057-8094-8099-8100-8102-8113-8115-8116-8117-8118-8119-8120-8121-8124-8127-8129-8130-8131-8132-8133-8139",
    "7916-7917-7927-7934-7935",
    "7965-7966-7967-7973",
    "7988-8003-8004-8005",
    "7989-8160-8165-8171-8173-8177-8178-8179-8180-8181-8182-8187-8189-8190-8194-8195-8199-8200-8201-8202-8203-8204-8205-8206-8210-8212-8213",
    "8013-8014-8015-8042-8045-8058-8062",
    "8024-8107-8125-8134-8135-8140-8142-8143-8144-8146-8148-8149-8151-8152-8153-8157",
    "8036-8040-8063-8064-8065",
    "8041-8078-8079-8080-8081-8082-8086-8087",
    "8154-8159-8166-8168-8169-8170",
    "8207-8211-8214-8215-8216-8217-8219-8220-8225-8226-8228-8229-8231-8232-8233-8234-8237-8239-8240-8241-8242-8243-8244-8245-8246-8247-8249-8250-8252-8253-8254-8255-8256-8257-8258-8259-8260-8261-8264",
    "8265-8266-8267-8268-8271-8272-8273-8277-8280-8285-8288-8289-8290-8293-8294-8295-8296-8297-8299-8300-8301-8303-8307-8308-8310-8311-8312",
    "8298-8313-8316-8319-8321-8322-8323-8324-8325-8326-8327-8328-8330-8340-8341",
    "8317-8342-8343-8344-8347-8348-8349-8350-8351-8352-8353-8354-8357-8358-8359-8361-8362-8363-8365-8367-8368-8370-8371-8372-8374-8375-8376-8378",
    "8373-8377-8379-8380-8381-8382-8385-8387-8389-8390-8391-8392-8393-8394-8397-8401-8405-8406-8408",
    "8386-8409-8411-8412",
    "8413-8414-8415-8416-8418-8422-8426-8427-8428-8429-8430-8431-8432-8433-8434-8435-8437-8439-8441-8442-8443-8447",
    "8425-8449-8450",
    "8452-8476-8477-8479",
    "8453-8454-8471-8472-8483-8485-8486-8487-8491-8498-8499-8500-8501-8505-8506-8507-8508-8509-8510-8511-8513-8517",
    "8478-8480-8481-8482",
    "8512-8536-8549-8551-8563-8564-8565-8566-8567-8568-8569-8570-8571-8572-8573-8574-8575-8576-8577-8580-8582-8584",
    "8532-8534-8537-8538-8539-8544-8548-8552-8554-8556-8557-8558-8559-8560-8561-8562",
    "8541-8618-8626-8636-8638-8639-8640-8642-8643-8644-8647-8683-8699-8700-8703-8704-8705-8706-8707-8708-8709-8710-8714-8717-8719-8720-8721",
    "8578-8600-8602-8605-8620-8627-8631",
    "8585-8586-8587-8589-8590",
    "8592-8593-8595-8598-8599",
    "8601-8603-8604",
    "8629-8633-8645-8646-8648-8655-8656-8665-8666-8668",
    "8651-8664-8677-8678-8680-8684",
    "8669-8670-8671-8674",
    "8675-8690-8691-8693-8694-8698",
]
for _ni in _BUNDLE_NIS_EKSCTL:
    Instance.register("eksctl-io", _ni)(Eksctl)
