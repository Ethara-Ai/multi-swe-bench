import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _parse_tsdb_version(base_label: str) -> tuple[int, int, int]:
    """Parse (major, minor, patch) from base.label like '2.26.1..2.26.2'."""
    first_tag = base_label.split("..")[0]
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", first_tag)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d+)\.(\d+)", first_tag)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)
    raise ValueError(f"Cannot parse timescaledb version from base.label: {base_label}")


def _get_pg_version(version: tuple[int, int, int]) -> int:
    """Return the PostgreSQL major version to use for a given TimescaleDB version."""
    major, minor, _ = version
    if major == 0:
        return 10
    if major == 1:
        return 12
    if major == 2:
        if minor <= 1:
            return 12
        if minor <= 3:
            return 13
        if minor <= 5:
            return 14
        if minor <= 12:
            return 15
        if minor <= 17:
            return 16
        if minor <= 22:
            return 16
        return 17
    return 17


_VERSION_TO_IMAGE = [
    (2, 18, "ubuntu:24.04"),
    (2, 0, "ubuntu:22.04"),
    (1, 0, "ubuntu:22.04"),
    (0, 0, "ubuntu:22.04"),
]
_DEFAULT_IMAGE = "ubuntu:22.04"


def _get_base_image(version: tuple[int, int, int]) -> str:
    major, minor, _ = version
    for min_major, min_minor, image in _VERSION_TO_IMAGE:
        if major > min_major or (major == min_major and minor >= min_minor):
            return image
    return _DEFAULT_IMAGE


class ImageBase(Image):
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
        version = _parse_tsdb_version(self.pr.base.label)
        return _get_base_image(version)

    def image_tag(self) -> str:
        version = _parse_tsdb_version(self.pr.base.label)
        pg_ver = _get_pg_version(version)
        return f"base-pg{pg_ver}"

    def workdir(self) -> str:
        version = _parse_tsdb_version(self.pr.base.label)
        pg_ver = _get_pg_version(version)
        return f"base-pg{pg_ver}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        version = _parse_tsdb_version(self.pr.base.label)
        pg_ver = _get_pg_version(version)
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
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && \\
    apt-get install -y gnupg lsb-release wget curl ca-certificates && \\
    install -d /usr/share/postgresql-common/pgdg && \\
    curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc && \\
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \\
    apt-get update && \\
    apt-get install -y \\
    build-essential cmake git libssl-dev libkrb5-dev \\
    postgresql-{pg_ver} postgresql-server-dev-{pg_ver} libpq-dev \\
    flex bison pkg-config sudo \\
    && (apt-get install -y tzdata-legacy 2>/dev/null || true) \
    && apt-get clean

RUN mv /usr/lib/postgresql/{pg_ver}/bin/pg_config /usr/lib/postgresql/{pg_ver}/bin/pg_config.real && \\
    echo '#!/bin/bash' > /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo 'if [ "$1" = "--version" ]; then' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo '  /usr/lib/postgresql/{pg_ver}/bin/pg_config.real --version | sed "s/ (.*//"' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo 'else' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo '  /usr/lib/postgresql/{pg_ver}/bin/pg_config.real "$@"' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    echo 'fi' >> /usr/lib/postgresql/{pg_ver}/bin/pg_config && \\
    chmod +x /usr/lib/postgresql/{pg_ver}/bin/pg_config

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _pg_version(self) -> int:
        version = _parse_tsdb_version(self.pr.base.label)
        return _get_pg_version(version)

    def _is_early_era(self) -> bool:
        """v0.x (minor < 8) uses plain Makefile + PGXS, no bootstrap script."""
        version = _parse_tsdb_version(self.pr.base.label)
        major, minor, _ = version
        return major == 0 and minor < 8
    def _is_mid_era(self) -> bool:
        """v0.8-0.12 uses bootstrap+CMake but has installcheck, not regresscheck."""
        version = _parse_tsdb_version(self.pr.base.label)
        major, minor, _ = version
        return major == 0 and minor >= 8

    def _build_commands(self) -> str:
        pg_ver = self._pg_version()
        if self._is_early_era():
            return (
                f"export PG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config\n"
                f"# Patch version check to accept PG10\n"
                f"sed -i 's/MIN_SUPPORTED_VERSION_STR \"9.6\"/MIN_SUPPORTED_VERSION_STR \"9.6\"/' Makefile 2>/dev/null || true\n"
                f"sed -i 's/MIN_SUPPORTED_VERSION 90600/MIN_SUPPORTED_VERSION 90600/' Makefile 2>/dev/null || true\n"
                f"make -j$(nproc) PG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config\n"
                f"make install PG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config"
            )
        # pg10-13 lack pg_isolation_regress; without -DREGRESS_CHECKS=OFF cmake
        # will FATAL_ERROR.  pg14+ ship the binary so we leave checks ON so that
        # the regresscheck make-target is generated (needed by _test_commands).
        regress_off = " -DREGRESS_CHECKS=OFF" if pg_ver <= 13 else ""
        return (
            f"export BUILD_FORCE_REMOVE=true\n"
            f"./bootstrap -DCMAKE_BUILD_TYPE=Release "
            f"-DPG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config{regress_off}\n"
            f"cd build && make -j$(nproc)\n"
            f"make install"
        )

    def _test_commands(self) -> str:
        pg_ver = self._pg_version()
        if self._is_early_era():
            return f'su - postgres -c "cd /home/{self.pr.repo} && make installcheck PG_CONFIG=/usr/lib/postgresql/{pg_ver}/bin/pg_config 2>&1 || true"'
        if self._is_mid_era():
            return f'su - postgres -c "cd /home/{self.pr.repo}/build && make installcheck 2>&1 || true"'
        return f'su - postgres -c "cd /home/{self.pr.repo}/build && make regresscheck 2>&1 || true && make regresscheck-t 2>&1 || true"'

    def files(self) -> list[File]:
        build_cmds = self._build_commands()
        test_cmds = self._test_commands()

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git config --global --add safe.directory /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
chown -R postgres:postgres /home/{repo}

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{build}
chown -R postgres:postgres /home/{repo}
{test}
""".format(repo=self.pr.repo, build=build_cmds, test=test_cmds),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --exclude="*.gz" /home/test.patch
{build}
chown -R postgres:postgres /home/{repo}
{test}

""".format(repo=self.pr.repo, build=build_cmds, test=test_cmds),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --exclude="*.gz" /home/test.patch /home/fix.patch
{build}
chown -R postgres:postgres /home/{repo}
{test}

""".format(repo=self.pr.repo, build=build_cmds, test=test_cmds),
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


@Instance.register("timescale", "timescaledb")
class TimescaleDB(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # pg_regress output format:
        # test <name>-<pgver>  ... ok   <ms>
        # test <name>-<pgver>  ... FAILED   <ms>
        # test <name>          ... ok   <ms>
        re_ok = re.compile(
            r"^test\s+(\S+)\s+\.\.\.\s+ok\s+\d+\s*ms", re.MULTILINE
        )
        re_fail = re.compile(
            r"^test\s+(\S+)\s+\.\.\.\s+FAILED\s+\d+\s*ms", re.MULTILINE
        )
        # TAP output format (v2.14+):
        # ok 1         + <name>   <ms>
        # not ok 102   + <name>   <ms>
        re_tap_ok = re.compile(
            r"^ok\s+\d+\s+[+-]\s+(\S+)\s+\d+\s*ms", re.MULTILINE
        )
        re_tap_fail = re.compile(
            r"^not ok\s+\d+\s+[+-]\s+(\S+)\s+\d+\s*ms", re.MULTILINE
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            ok_match = re_ok.match(line)
            if ok_match:
                test_name = ok_match.group(1)
                passed_tests.add(test_name)
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                test_name = fail_match.group(1)
                failed_tests.add(test_name)
                continue

            tap_ok_match = re_tap_ok.match(line)
            if tap_ok_match:
                test_name = tap_ok_match.group(1)
                passed_tests.add(test_name)
                continue

            tap_fail_match = re_tap_fail.match(line)
            if tap_fail_match:
                test_name = tap_fail_match.group(1)
                failed_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
