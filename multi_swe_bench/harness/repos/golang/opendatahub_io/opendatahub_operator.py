import re
import xml.etree.ElementTree as ET
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OpendatahubOperatorImageBase(Image):
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
        return "golang:1.18"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV CGO_ENABLED=0
ENV GOPROXY=https://proxy.golang.org,direct
ENV CI=true

WORKDIR /home/

{code}

{self.clear_env}

"""


class OpendatahubOperatorImageDefault(Image):
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
        return OpendatahubOperatorImageBase(self.pr, self.config)

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

ARCH=$(dpkg --print-architecture) && curl -fsSL -o /usr/local/bin/setup-envtest https://github.com/kubernetes-sigs/controller-runtime/releases/download/v0.19.4/setup-envtest-linux-$ARCH && chmod +x /usr/local/bin/setup-envtest
mkdir -p /home/{pr.repo}/testbin
export KUBEBUILDER_ASSETS="$(setup-envtest use 1.24.2 --bin-dir=/home/{pr.repo}/testbin -p path)"

KUBEBUILDER_ASSETS="$KUBEBUILDER_ASSETS" go test -v -count=1 ./... || true
GINKGO_VERSION=$(go list -m -f '{{{{.Version}}}}' github.com/onsi/ginkgo/v2 2>/dev/null)
go run github.com/onsi/ginkgo/v2/ginkgo@${{GINKGO_VERSION:-latest}} --help > /dev/null 2>&1 || true

curl -fsSL -o /usr/local/bin/operator-sdk https://github.com/operator-framework/operator-sdk/releases/download/v1.34.1/operator-sdk_linux_amd64
chmod +x /usr/local/bin/operator-sdk

""".format(pr=self.pr),
            ),
            File(
                ".",
                "scorecard-run.sh",
                """#!/bin/bash
set -uo pipefail

cd /home/{pr.repo}

if [ ! -f bundle/tests/scorecard/config.yaml ]; then
    exit 0
fi

TEST_NAMES=$(grep -E '^\\s*test:' bundle/tests/scorecard/config.yaml | awk '{{print $2}}' | sort -u)

VALIDATE_LOG=$(operator-sdk bundle validate ./bundle --select-optional suite=operatorframework 2>&1)
VALIDATE_EXIT=$?

CSV_FILE=$(ls bundle/manifests/*clusterserviceversion.yaml 2>/dev/null | head -1)
ALM_EXAMPLES_STATE="missing"
if [ -n "$CSV_FILE" ]; then
    if grep -qE "^\\s*alm-examples:\\s*'?\\[\\]'?\\s*$" "$CSV_FILE"; then
        ALM_EXAMPLES_STATE="empty"
    elif grep -qE "^\\s*alm-examples:" "$CSV_FILE"; then
        ALM_EXAMPLES_STATE="populated"
    fi
fi

echo "---SCORECARD-BUNDLE-VALIDATE-START---"
echo "$VALIDATE_LOG"
echo "---SCORECARD-BUNDLE-VALIDATE-EXIT---"
echo "$VALIDATE_EXIT"
echo "---SCORECARD-BUNDLE-VALIDATE-TESTS---"
echo "$TEST_NAMES"
echo "---SCORECARD-ALM-EXAMPLES---"
echo "$ALM_EXAMPLES_STATE"
echo "---SCORECARD-BUNDLE-VALIDATE-END---"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export KUBEBUILDER_ASSETS="$(setup-envtest use 1.24.2 --bin-dir=/home/{pr.repo}/testbin -p path)"

rm -rf /tmp/ginkgo-reports
mkdir -p /tmp/ginkgo-reports

GINKGO_VERSION=$(go list -m -f '{{{{.Version}}}}' github.com/onsi/ginkgo/v2)

set +e
go run github.com/onsi/ginkgo/v2/ginkgo@${{GINKGO_VERSION}} -r --junit-report=report.xml --output-dir=/tmp/ginkgo-reports --no-color ./...
GINKGO_EXIT=$?
set -e

echo "---GINKGO-JUNIT-REPORT-START---"
cat /tmp/ginkgo-reports/*.xml
echo "---GINKGO-JUNIT-REPORT-END---"

bash /home/scorecard-run.sh || true

exit $GINKGO_EXIT

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export KUBEBUILDER_ASSETS="$(setup-envtest use 1.24.2 --bin-dir=/home/{pr.repo}/testbin -p path)"
git apply --whitespace=nowarn /home/test.patch

rm -rf /tmp/ginkgo-reports
mkdir -p /tmp/ginkgo-reports

GINKGO_VERSION=$(go list -m -f '{{{{.Version}}}}' github.com/onsi/ginkgo/v2)

set +e
go run github.com/onsi/ginkgo/v2/ginkgo@${{GINKGO_VERSION}} -r --junit-report=report.xml --output-dir=/tmp/ginkgo-reports --no-color ./...
GINKGO_EXIT=$?
set -e

echo "---GINKGO-JUNIT-REPORT-START---"
cat /tmp/ginkgo-reports/*.xml
echo "---GINKGO-JUNIT-REPORT-END---"

bash /home/scorecard-run.sh || true

exit $GINKGO_EXIT

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export KUBEBUILDER_ASSETS="$(setup-envtest use 1.24.2 --bin-dir=/home/{pr.repo}/testbin -p path)"
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

rm -rf /tmp/ginkgo-reports
mkdir -p /tmp/ginkgo-reports

GINKGO_VERSION=$(go list -m -f '{{{{.Version}}}}' github.com/onsi/ginkgo/v2)

set +e
go run github.com/onsi/ginkgo/v2/ginkgo@${{GINKGO_VERSION}} -r --junit-report=report.xml --output-dir=/tmp/ginkgo-reports --no-color ./...
GINKGO_EXIT=$?
set -e

echo "---GINKGO-JUNIT-REPORT-START---"
cat /tmp/ginkgo-reports/*.xml
echo "---GINKGO-JUNIT-REPORT-END---"

bash /home/scorecard-run.sh || true

exit $GINKGO_EXIT

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("opendatahub-io", "opendatahub-operator")
class OpendatahubOperator(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OpendatahubOperatorImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        xml_matches = re.findall(r"<testsuites.*?</testsuites>", clean_log, re.DOTALL)
        for xml_text in xml_matches:
            try:
                root = ET.fromstring(xml_text)
            except ET.ParseError:
                continue

            for testcase in root.iter("testcase"):
                classname = testcase.get("classname", "").strip()
                name = testcase.get("name", "").strip()
                full_name = f"{classname}/{name}" if classname else name

                if testcase.find("failure") is not None or testcase.find("error") is not None:
                    failed_tests.add(full_name)
                elif testcase.find("skipped") is not None:
                    skipped_tests.add(full_name)
                else:
                    passed_tests.add(full_name)

        bundle_validate_matches = re.findall(
            r"---SCORECARD-BUNDLE-VALIDATE-START---\n(.*?)\n"
            r"---SCORECARD-BUNDLE-VALIDATE-EXIT---\n(.*?)\n"
            r"---SCORECARD-BUNDLE-VALIDATE-TESTS---\n(.*?)\n"
            r"---SCORECARD-ALM-EXAMPLES---\n(.*?)\n"
            r"---SCORECARD-BUNDLE-VALIDATE-END---",
            clean_log,
            re.DOTALL,
        )
        keyword_hints = {
            "olm-crds-have-resources-test": [
                "not found in the resources",
                "resources for owned crd",
                "not listed in the customresourcedefinitions",
            ],
            "olm-crds-have-validation-test": [
                "openapiv3schema",
                "structural schema",
                "crd has no validation",
            ],
            "olm-spec-descriptors-test": ["specdescriptors"],
            "olm-status-descriptors-test": ["statusdescriptors"],
        }

        for (
            validate_log,
            exit_code_text,
            test_names_text,
            alm_examples_state,
        ) in bundle_validate_matches:
            test_names = [t.strip() for t in test_names_text.splitlines() if t.strip()]
            validate_log_lower = validate_log.lower()
            alm_examples_state = alm_examples_state.strip()
            try:
                exit_code = int(exit_code_text.strip())
            except ValueError:
                exit_code = 1
            has_errors = (
                exit_code != 0
                or "level=error" in validate_log_lower
                or "error:" in validate_log_lower
            )

            for name in test_names:
                full_name = f"scorecard/{name}"

                if name == "basic-check-spec-test":
                    if alm_examples_state == "populated":
                        passed_tests.add(full_name)
                    else:
                        failed_tests.add(full_name)
                    continue

                if not has_errors:
                    passed_tests.add(full_name)
                    continue

                keywords = keyword_hints.get(name)
                if keywords is None:
                    failed_tests.add(full_name)
                elif any(keyword in validate_log_lower for keyword in keywords):
                    failed_tests.add(full_name)
                else:
                    skipped_tests.add(full_name)

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
