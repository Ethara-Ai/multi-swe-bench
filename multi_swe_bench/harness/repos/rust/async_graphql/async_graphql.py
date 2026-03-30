import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AsyncGraphqlImageBase(Image):
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
        return "rust:1.85"

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

WORKDIR /home/

{code}

{self.clear_env}

"""


class AsyncGraphqlImageDefault(Image):
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
        return AsyncGraphqlImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _cargo_test_cmd(self) -> str:
        """Return the cargo test command appropriate for this PR's era.

        Era 1 (PR 262): --all-features works (no nightly feature)
        Era 2a (PR 321): has nightly feature, no dataloader yet
        Era 2b (PRs 378, 383, 438): has nightly feature, has dataloader
        Era 3 (PRs 469+): --all-features works (nightly replaced with docsrs cfg)
        """
        pr_num = self.pr.number
        if pr_num == 321:
            return (
                "cargo test -p async-graphql --features "
                "'apollo_tracing,apollo_persisted_queries,multipart,unblock,string_number'"
            )
        if pr_num in (378, 383, 438):
            return (
                "cargo test -p async-graphql --features "
                "'apollo_tracing,apollo_persisted_queries,multipart,unblock,string_number,dataloader'"
            )
        return "cargo test -p async-graphql --all-features"

    def files(self) -> list[File]:
        cargo_test = self._cargo_test_cmd()

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
                "fix-compat.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# 1. Remove rocket integration (ubyte crate is yanked)
sed -i '/rocket/d' Cargo.toml
rm -rf integrations/rocket

# 2. Fix serde::export removal (only affects PR 262, harmless on others)
sed -i 's/use serde::export::PhantomData;/use std::marker::PhantomData;/g' \
    src/guard.rs src/subscription/simple_broker.rs 2>/dev/null || true

# 3. Fix raw ident derive macro panic on Rust 1.85
for DERIVE_DIR in derive/src async-graphql-derive/src; do
    if [ -f "$DERIVE_DIR/utils.rs" ]; then
        sed -i 's|Ident::new(&format!("__{{}}_getter", name), Span::call_site())|Ident::new(\\&format!("__{{}}_getter", name.strip_prefix("r#").unwrap_or(name)), Span::call_site())|' \
            "$DERIVE_DIR/utils.rs"
    fi
    for SRC_FILE in "$DERIVE_DIR/object.rs" "$DERIVE_DIR/subscription.rs"; do
        if [ -f "$SRC_FILE" ]; then
            sed -i 's|get_param_getter_ident(&ident.ident.to_string())|get_param_getter_ident(\\&ident.ident.unraw().to_string())|g' \
                "$SRC_FILE"
        fi
    done
done

# 4. Enable sink feature on futures-channel
sed -i 's/^futures-channel = "0\\.3\\.[0-9]*"$/futures-channel = {{ version = "0.3", features = ["sink"] }}/' \
    Cargo.toml 2>/dev/null || true

# 5. Add visit-mut feature to syn (needed by some fix patches)
for TOML in derive/Cargo.toml async-graphql-derive/Cargo.toml; do
    if [ -f "$TOML" ]; then
        sed -i 's/features = \\["full", "extra-traits"\\]/features = ["full", "extra-traits", "visit-mut"]/' "$TOML"
    fi
done

# 6. Remove nightly feature from Cargo.toml (doc_cfg is unstable on stable Rust)
sed -i '/^nightly = \\[\\]$/d' Cargo.toml 2>/dev/null || true

# 7. Pin time crate to avoid requiring Rust 1.88+
cargo generate-lockfile 2>&1 || true
cargo update time@0.3.47 --precise 0.3.35 2>&1 || true

""".format(pr=self.pr),
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

bash /home/fix-compat.sh

# Pre-build to cache dependencies (allowed to fail)
{cargo_test} 2>&1 || true

""".format(pr=self.pr, cargo_test=cargo_test),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{cargo_test}

""".format(pr=self.pr, cargo_test=cargo_test),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git apply /home/test.patch
bash /home/fix-compat.sh
{cargo_test}

""".format(pr=self.pr, cargo_test=cargo_test),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git apply /home/test.patch /home/fix.patch
bash /home/fix-compat.sh
{cargo_test}

""".format(pr=self.pr, cargo_test=cargo_test),
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


@Instance.register("async-graphql", "async-graphql")
class AsyncGraphql(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AsyncGraphqlImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"test (\S+) \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (\S+) \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) \.\.\. ignored")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
