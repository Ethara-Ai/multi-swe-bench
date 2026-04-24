import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


CIRQ_5650_TO_4103_PR_TESTS = {
    4103: [
        "cirq-core/cirq/ops/linear_combinations_test.py",
    ],
    4746: [
        "cirq-core/cirq/optimizers/transformer_primitives_test.py",
        "cirq-core/cirq/transformers/transformer_primitives_test.py",
    ],
    4762: [
        "cirq-core/cirq/transformers/analytical_decompositions/two_qubit_state_preparation_test.py",
        "cirq-core/cirq/optimizers/two_qubit_state_preparation_test.py",
    ],
    4785: [
        "cirq-core/cirq/optimizers/clifford_decomposition_test.py",
        "cirq-core/cirq/optimizers/controlled_gate_decomposition_test.py",
        "cirq-core/cirq/optimizers/cphase_to_fsim_test.py",
        "cirq-core/cirq/transformers/analytical_decompositions/clifford_decomposition_test.py",
        "cirq-core/cirq/transformers/analytical_decompositions/controlled_gate_decomposition_test.py",
        "cirq-core/cirq/transformers/analytical_decompositions/cphase_to_fsim_test.py",
    ],
    4799: [
        "cirq-core/cirq/optimizers/decompositions_test.py",
        "cirq-core/cirq/transformers/analytical_decompositions/single_qubit_decompositions_test.py",
    ],
    4809: [
        "cirq-core/cirq/optimizers/three_qubit_decomposition_test.py",
        "cirq-core/cirq/optimizers/two_qubit_decompositions_test.py",
        "cirq-core/cirq/optimizers/two_qubit_to_fsim_test.py",
        "cirq-core/cirq/optimizers/two_qubit_to_sqrt_iswap_test.py",
        "cirq-core/cirq/transformers/analytical_decompositions/three_qubit_decomposition_test.py",
        "cirq-core/cirq/transformers/analytical_decompositions/two_qubit_to_cz_test.py",
        "cirq-core/cirq/transformers/analytical_decompositions/two_qubit_to_fsim_test.py",
        "cirq-core/cirq/transformers/analytical_decompositions/two_qubit_to_sqrt_iswap_test.py",
    ],
    4891: [
        "cirq-core/cirq/optimizers/align_left_test.py",
        "cirq-core/cirq/optimizers/align_right_test.py",
        "cirq-core/cirq/transformers/align_test.py",
    ],
    4915: [
        "cirq-core/cirq/contrib/quimb/density_matrix_test.py",
        "cirq-core/cirq/ops/pauli_string_phasor_test.py",
        "cirq-core/cirq/optimizers/drop_empty_moments_test.py",
        "cirq-core/cirq/optimizers/drop_negligible_test.py",
        "cirq-core/cirq/optimizers/eject_z_test.py",
        "cirq-core/cirq/optimizers/expand_composite_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_to_sqrt_iswap_test.py",
        "cirq-core/cirq/optimizers/merge_single_qubit_gates_test.py",
        "cirq-core/cirq/transformers/drop_empty_moments_test.py",
        "cirq-core/cirq/transformers/drop_negligible_operations_test.py",
    ],
    4944: [
        "cirq-core/cirq/optimizers/stratify_test.py",
        "cirq-core/cirq/transformers/stratify_test.py",
    ],
    4946: [
        "cirq-core/cirq/contrib/acquaintance/bipartite_test.py",
        "cirq-core/cirq/contrib/acquaintance/gates_test.py",
        "cirq-core/cirq/contrib/acquaintance/mutation_utils_test.py",
        "cirq-core/cirq/contrib/acquaintance/optimizers_test.py",
        "cirq-core/cirq/contrib/acquaintance/permutation_test.py",
        "cirq-core/cirq/contrib/acquaintance/shift_test.py",
        "cirq-core/cirq/ops/pauli_string_phasor_test.py",
        "cirq-core/cirq/optimizers/expand_composite_test.py",
        "cirq-core/cirq/transformers/expand_composite_test.py",
    ],
    4955: [
        "cirq-core/cirq/optimizers/eject_z_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_to_sqrt_iswap_test.py",
        "cirq-core/cirq/transformers/eject_z_test.py",
    ],
    4958: [
        "cirq-core/cirq/optimizers/eject_phased_paulis_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_to_sqrt_iswap_test.py",
        "cirq-core/cirq/transformers/eject_phased_paulis_test.py",
    ],
    4974: [
        "cirq-core/cirq/transformers/transformer_primitives_test.py",
    ],
    4986: [
        "cirq-core/cirq/devices/noise_model_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_to_sqrt_iswap_test.py",
        "cirq-core/cirq/optimizers/merge_single_qubit_gates_test.py",
        "cirq-core/cirq/transformers/merge_k_qubit_gates_test.py",
        "cirq-core/cirq/transformers/merge_single_qubit_gates_test.py",
    ],
    5002: [
        "cirq-core/cirq/transformers/analytical_decompositions/two_qubit_to_sqrt_iswap_test.py",
    ],
    5005: [
        "cirq-core/cirq/protocols/decompose_protocol_test.py",
        "cirq-core/cirq/transformers/optimize_for_target_gateset_test.py",
        "cirq-core/cirq/transformers/target_gatesets/compilation_target_gateset_test.py",
    ],
    5007: [
        "cirq-core/cirq/contrib/paulistring/convert_gate_set_test.py",
        "cirq-core/cirq/optimizers/convert_to_cz_and_single_gates_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_test.py",
        "cirq-core/cirq/transformers/target_gatesets/compilation_target_gateset_test.py",
        "cirq-core/cirq/transformers/target_gatesets/cz_gateset_test.py",
    ],
    5025: [
        "cirq-core/cirq/transformers/analytical_decompositions/two_qubit_to_sqrt_iswap_test.py",
        "cirq-core/cirq/transformers/target_gatesets/sqrt_iswap_gateset_test.py",
    ],
    5040: [
        "cirq-core/cirq/optimizers/merge_interactions_test.py",
        "cirq-core/cirq/optimizers/merge_interactions_to_sqrt_iswap_test.py",
        "cirq-google/cirq_google/optimizers/convert_to_sqrt_iswap_test.py",
    ],
    5044: [
        "cirq-google/cirq_google/optimizers/convert_to_sycamore_gates_test.py",
        "cirq-google/cirq_google/transformers/analytical_decompositions/two_qubit_to_sycamore_test.py",
    ],
    5054: [
        "cirq-google/cirq_google/optimizers/convert_to_sycamore_gates_test.py",
        "cirq-google/cirq_google/transformers/target_gatesets/sycamore_gateset_test.py",
    ],
    5095: [
        "cirq-google/cirq_google/calibration/engine_simulator_test.py",
    ],
    5096: [
        "cirq-google/cirq_google/devices/xmon_device_test.py",
        "cirq-google/cirq_google/optimizers/convert_to_xmon_gates_test.py",
    ],
    5650: [
        "cirq-core/cirq/contrib/paulistring/clifford_optimize_test.py",
        "cirq-core/cirq/contrib/paulistring/clifford_target_gateset_test.py",
        "cirq-core/cirq/contrib/paulistring/convert_gate_set_test.py",
        "cirq-core/cirq/contrib/paulistring/convert_to_clifford_gates_test.py",
        "cirq-core/cirq/contrib/paulistring/convert_to_pauli_string_phasors_test.py",
        "cirq-core/cirq/contrib/paulistring/pauli_string_optimize_test.py",
    ],
}


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "python:3.10-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _needs_cirq_google(self) -> bool:
        test_files = CIRQ_5650_TO_4103_PR_TESTS.get(self.pr.number, [])
        return any("cirq-google" in f for f in test_files)

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        test_files = CIRQ_5650_TO_4103_PR_TESTS.get(self.pr.number, [])
        test_files_str = " ".join(test_files)
        needs_google = self._needs_cirq_google()

        install_google = 'pip install -e "./cirq-google"' if needs_google else ""

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
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/[[REPO_NAME]]
pip install --upgrade pip "setuptools<70" wheel
###ACTION_DELIMITER###
pip install -e "./cirq-core[contrib]"
###ACTION_DELIMITER###
[[INSTALL_GOOGLE]]
###ACTION_DELIMITER###
pip install pytest
""".replace("[[REPO_NAME]]", repo_name).replace("[[INSTALL_GOOGLE]]", install_google),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
# Filter to only test files that exist at this base commit
EXISTING_TESTS=""
for f in [[TEST_FILES]]; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Filter to only test files that exist after applying test patch
EXISTING_TESTS=""
for f in [[TEST_FILES]]; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Filter to only test files that exist after applying patches
EXISTING_TESTS=""
for f in [[TEST_FILES]]; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""".replace("[[REPO_NAME]]", repo_name).replace("[[TEST_FILES]]", test_files_str),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""
FROM python:3.10-slim

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content


@Instance.register("quantumlib", "Cirq_5650_to_4103")
class CIRQ_5650_TO_4103(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in log.split("\n"):
            line = line.strip()
            if line.startswith("PASSED "):
                test_name = line[len("PASSED "):].strip()
                passed_tests.add(test_name)
            elif line.startswith("FAILED "):
                test_name = line[len("FAILED "):].strip()
                if " - " in test_name:
                    test_name = test_name.split(" - ")[0]
                failed_tests.add(test_name)
            elif line.startswith("SKIPPED "):
                test_name = line[len("SKIPPED "):].strip()
                skipped_tests.add(test_name)
            else:
                # pytest -rA summary: "test_path::test_name PASSED"
                match = re.match(r"^(.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL)\s*(\[.*\])?$", line)
                if match:
                    test_name = match.group(1)
                    status = match.group(2)
                    if status == "PASSED":
                        passed_tests.add(test_name)
                    elif status in ("FAILED", "ERROR"):
                        failed_tests.add(test_name)
                    elif status == "SKIPPED":
                        skipped_tests.add(test_name)
                    elif status == "XFAIL":
                        passed_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
