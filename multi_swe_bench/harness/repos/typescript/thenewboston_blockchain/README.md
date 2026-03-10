# thenewboston-blockchain Registries

This directory contains the auto-generated registries for the `thenewboston-blockchain/Website` repository. These registries are used by the `multi-swe-bench` harness to evaluate Pull Requests (PRs) against resolved issues.

## Package Structure

- `website_[PR]_to_[ISSUE].py`: Individual registry files for each PR.
- `__init__.py`: Package entry point that registers all instances with the harness.

## Technical Configuration

- **Language**: TypeScript / JavaScript
- **Base Image**: `node:18-bullseye`
- **Dependency Manager**: `yarn`
- **Test Framework**: Jest
- **Environment**: All PRs currently use Node 18 as the default environment.

## Syntax & Patterns

These registries follow the standard `multi-swe-bench` patterns:
- **ImageDefault Class**: Handles Docker environment setup, repository cloning, and patch application.
- **Instance Class**: Handles test execution and log parsing.
- **Registration**: Uses `@Instance.register("thenewboston-blockchain", "[id]")`.

## Usage Commands

To use these registries, ensure you are in the root of the `multi-swe-bench` repository and using **Python 3.11**.

### 1. List Registered Instances
To verify that the instances are correctly loaded:
```bash
export PYTHONPATH=$PYTHONPATH:$(pwd)
python3.11 -c "from multi_swe_bench.harness.instance import Instance; print('\n'.join([k for k in Instance._registry if 'thenewboston-blockchain' in k]))"
```

### 2. Run Evaluation for a Specific PR
To run the evaluation for a single instance (e.g., PR 1383):
```bash
python3.11 multi_swe_bench/harness/run_evaluation.py \
    --dataset_path thenewboston_Website.jsonl \
    --instance_id thenewboston-blockchain/website_1383_to_1373 \
    --output_dir ./evaluation_results
```

### 3. Run Evaluation for All PRs
To run evaluations for all PRs in the dataset:
```bash
python3.11 multi_swe_bench/harness/run_evaluation.py \
    --dataset_path thenewboston_Website.jsonl \
    --repo thenewboston-blockchain \
    --output_dir ./evaluation_results
```

### 4. View Results
Results will be stored in the specified `--output_dir` as JSON files containing the test execution logs and parsed pass/fail counts.
