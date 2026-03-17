import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import mapping_to_testresult


class ImageDefault105(Image):
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
        return "python:3.8-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """ls
###ACTION_DELIMITER###
python -m pip install -e .
###ACTION_DELIMITER###
python -m pip install -r test-requirements.txt
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider data_tables/' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh
###ACTION_DELIMITER###
chmod +x test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
pytest --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, trying with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
pytest --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, trying individually with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
pytest --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/alteryx/woodwork.git /home/woodwork

WORKDIR /home/woodwork
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN pip install "setuptools<70" && pip install -e . && pip install -r test-requirements.txt

{copy_commands}
"""


class ImageDefault434(Image):
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """ls -la
###ACTION_DELIMITER###
cat requirements.txt
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
pip install -r test-requirements.txt
###ACTION_DELIMITER###
echo 'pytest -v --ignore=docs --no-header -rA --tb=no -p no:cacheprovider' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install -r dev-requirements.txt
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'pytest -v --no-header -rA --tb=no -p no:cacheprovider woodwork/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
pytest -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, trying with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
pytest -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, trying individually with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
pytest -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git gcc g++ python3-dev

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/alteryx/woodwork.git /home/woodwork

WORKDIR /home/woodwork
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN sed -i 's/codecov==2.1.9/codecov==2.1.13/' dev-requirements.txt 2>/dev/null || true
RUN pip install --upgrade pip "setuptools<70" wheel
RUN pip install -r requirements.txt
RUN pip install -r test-requirements.txt 2>/dev/null || true
RUN pip install -r dev-requirements.txt 2>/dev/null || true
RUN pip install -e . 2>/dev/null || true
RUN pip install "setuptools<70"

{copy_commands}
"""


class ImageDefault806(Image):
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """ls
###ACTION_DELIMITER###
apt-get update && apt-get install -y libopenblas-dev libgfortran5 python3-dev
###ACTION_DELIMITER###
make installdeps
###ACTION_DELIMITER###
apt-get update && apt-get install -y make
###ACTION_DELIMITER###
make installdeps
###ACTION_DELIMITER###
sed -i 's/codecov==2.1.9/codecov==2.1.13/' dev-requirements.txt
###ACTION_DELIMITER###
make installdeps
###ACTION_DELIMITER###
echo 'pytest woodwork/ -v --no-header -rA --tb=no -p no:cacheprovider' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install numpy==1.26.4
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
pytest woodwork/ -v --no-header -rA --tb=no -p no:cacheprovider
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, trying with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
pytest woodwork/ -v --no-header -rA --tb=no -p no:cacheprovider
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, trying individually with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
pytest woodwork/ -v --no-header -rA --tb=no -p no:cacheprovider
""",
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git libopenblas-dev libgfortran5 python3-dev make

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/alteryx/woodwork.git /home/woodwork

WORKDIR /home/woodwork
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN sed -i 's/codecov==2.1.9/codecov==2.1.13/' dev-requirements.txt 2>/dev/null || true
RUN pip install --upgrade pip "setuptools<70" wheel
RUN pip install -e . 2>/dev/null || true
RUN pip install -r test-requirements.txt 2>/dev/null || true
RUN pip install -r dev-requirements.txt 2>/dev/null || true
RUN pip install numpy==1.26.4
RUN pip install "setuptools<70"

{copy_commands}
"""


class ImageDefault1357(Image):
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """ls -l
###ACTION_DELIMITER###
pip install -r requirements.txt
###ACTION_DELIMITER###
pip install -r test-requirements.txt
###ACTION_DELIMITER###
pip install -e ".[test]"
###ACTION_DELIMITER###
echo 'pytest --ignore=docs -v --no-header -rA --tb=no -p no:cacheprovider woodwork/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
pytest --ignore=docs -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, trying with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
pytest --ignore=docs -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, trying individually with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
pytest --ignore=docs -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git make gcc g++ python3-dev

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/alteryx/woodwork.git /home/woodwork

WORKDIR /home/woodwork
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN sed -i 's/codecov==2.1.9/codecov==2.1.13/' dev-requirements.txt 2>/dev/null || true
RUN sed -i 's/codecov>=2.1.9/codecov>=2.1.13/' dev-requirements.txt 2>/dev/null || true
RUN pip install --upgrade pip "setuptools<70" wheel
RUN if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
RUN if [ -f test-requirements.txt ]; then pip install -r test-requirements.txt; fi
RUN if [ -f dev-requirements.txt ]; then pip install -r dev-requirements.txt 2>/dev/null || true; fi
RUN pip install -e ".[dev]" 2>/dev/null || pip install -e ".[test]" 2>/dev/null || pip install -e . 2>/dev/null || true
RUN pip install "pandas<2,>=1.3.0" 2>/dev/null || pip install "pandas<2" 2>/dev/null || true
RUN pip install "numpy<1.24"
RUN pip install "setuptools<70"

{copy_commands}
"""


class ImageDefault1439(Image):
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """ls .github/workflows
###ACTION_DELIMITER###
make package_woodwork
###ACTION_DELIMITER###
apt-get update && apt-get install -y make
###ACTION_DELIMITER###
make package_woodwork
###ACTION_DELIMITER###
make package_woodwork VERSION=0.16.3
###ACTION_DELIMITER###
pip install pep517
###ACTION_DELIMITER###
make package_woodwork
###ACTION_DELIMITER###
pip install -e unpacked_sdist/
###ACTION_DELIMITER###
pip install unpacked_sdist/[test]
###ACTION_DELIMITER###
echo 'pytest -v woodwork/' > test_commands.sh
###ACTION_DELIMITER###
chmod +x test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install numpy==1.23.5
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'pytest -v woodwork/ -k "not test_load_retail and not test_to_csv_S3 and not test_serialize_s3_pickle and not test_serialize_s3_parquet and not test_s3_test_profile"' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
pytest -v woodwork/ -k "not test_load_retail and not test_to_csv_S3 and not test_serialize_s3_pickle and not test_serialize_s3_parquet and not test_s3_test_profile"
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, trying with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
pytest -v woodwork/ -k "not test_load_retail and not test_to_csv_S3 and not test_serialize_s3_pickle and not test_serialize_s3_parquet and not test_s3_test_profile"
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, trying individually with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
pytest -v woodwork/ -k "not test_load_retail and not test_to_csv_S3 and not test_serialize_s3_pickle and not test_serialize_s3_parquet and not test_s3_test_profile"
""",
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git make

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/alteryx/woodwork.git /home/woodwork

WORKDIR /home/woodwork
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN pip install pep517 "setuptools<70"
RUN make package_woodwork || make package_woodwork VERSION=0.0.0
RUN pip install -e unpacked_sdist/ && pip install "unpacked_sdist/[test]"
RUN pip install numpy==1.23.5
RUN pip install "setuptools<70"

{copy_commands}
"""


class ImageDefault1557(Image):
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """cat Makefile
###ACTION_DELIMITER###
apt-get update && apt-get install -y openjdk-17-jre-headless && export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
###ACTION_DELIMITER###
make installdeps
###ACTION_DELIMITER###
apt-get update && apt-get install -y make
###ACTION_DELIMITER###
make installdeps
###ACTION_DELIMITER###
apt-get update && apt-get install -y openjdk-11-jre-headless && export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64
###ACTION_DELIMITER###
echo -e 'export PYARROW_IGNORE_TIMEZONE=1\nexport JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64\nexport SPARK_DRIVER_EXTRA_JAVA_OPTIONS="--add-exports=java.base/sun.nio.ch=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED"\nexport SPARK_EXECUTOR_EXTRA_JAVA_OPTIONS="--add-exports=java.base/sun.nio.ch=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED"\nexport SPARK_MASTER=local[1]\npytest -v -s woodwork/ --log-level=DEBUG --setup-show' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
export PYARROW_IGNORE_TIMEZONE=1
export JAVA_HOME=${{JAVA_HOME:-/usr/lib/jvm/default-java}}
export PATH=$JAVA_HOME/bin:$PATH
export SPARK_DRIVER_EXTRA_JAVA_OPTIONS="--add-exports=java.base/sun.nio.ch=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED"
export SPARK_EXECUTOR_EXTRA_JAVA_OPTIONS="--add-exports=java.base/sun.nio.ch=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED"
export SPARK_MASTER=local[1]
pytest -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, trying with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
export PYARROW_IGNORE_TIMEZONE=1
export JAVA_HOME=${{JAVA_HOME:-/usr/lib/jvm/default-java}}
export PATH=$JAVA_HOME/bin:$PATH
export SPARK_DRIVER_EXTRA_JAVA_OPTIONS="--add-exports=java.base/sun.nio.ch=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED"
export SPARK_EXECUTOR_EXTRA_JAVA_OPTIONS="--add-exports=java.base/sun.nio.ch=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED"
export SPARK_MASTER=local[1]
pytest -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, trying individually with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
export PYARROW_IGNORE_TIMEZONE=1
export JAVA_HOME=${{JAVA_HOME:-/usr/lib/jvm/default-java}}
export PATH=$JAVA_HOME/bin:$PATH
export SPARK_DRIVER_EXTRA_JAVA_OPTIONS="--add-exports=java.base/sun.nio.ch=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED"
export SPARK_EXECUTOR_EXTRA_JAVA_OPTIONS="--add-exports=java.base/sun.nio.ch=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED"
export SPARK_MASTER=local[1]
pytest -v --no-header -rA --tb=no -p no:cacheprovider woodwork/
""",
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git make default-jre-headless

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/alteryx/woodwork.git /home/woodwork

WORKDIR /home/woodwork
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN pip install "setuptools<70"
RUN make installdeps
RUN pip install "pandas<2" "numpy<2"
RUN pip install "dask[dataframe]<2024" 2>/dev/null || true
RUN pip install "pyspark>=3.5,<4" 2>/dev/null || true
RUN pip install "setuptools<70"

ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PYARROW_IGNORE_TIMEZONE=1
ENV SPARK_MASTER=local[1]

{copy_commands}
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

    def dependency(self) -> str:
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """ls -la
###ACTION_DELIMITER###
make installdeps-test
###ACTION_DELIMITER###
apt-get update && apt-get install -y make
###ACTION_DELIMITER###
make installdeps-test
###ACTION_DELIMITER###
pytest -v woodwork/
###ACTION_DELIMITER###
echo 'pytest -v woodwork/' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
pytest -v woodwork/
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply failed, trying with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
pytest -v woodwork/
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Warning: git apply failed, trying individually with --reject" >&2
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
pytest -v woodwork/
""",
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git make default-jre-headless

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/alteryx/woodwork.git /home/woodwork

WORKDIR /home/woodwork
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN if make installdeps-test 2>/dev/null; then true; else pip install -e ".[test]"; fi

RUN pip install "dask[dataframe]" 2>/dev/null || true
RUN pip install pyspark 2>/dev/null || true

ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PYARROW_IGNORE_TIMEZONE=1
ENV SPARK_MASTER=local[1]

# Pin pandas<2 if datetime_freq.py has the broken .astype("datetime64[ns]") pattern
RUN if grep -q 'astype.*datetime64' woodwork/tests/fixtures/datetime_freq.py 2>/dev/null; then pip install "pandas<2" "numpy<1.24"; else python -c "import pandas; exit(0 if int(pandas.__version__.split('.')[0]) >= 2 else 1)" 2>/dev/null || pip install "numpy<1.24"; fi

{copy_commands}
"""


@Instance.register("alteryx", "woodwork")
class WOODWORK(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        if self.pr.number <= 105:
            return ImageDefault105(self.pr, self._config)
        if self.pr.number <= 434:
            return ImageDefault434(self.pr, self._config)
        if self.pr.number <= 806:
            return ImageDefault806(self.pr, self._config)
        if self.pr.number <= 1357:
            return ImageDefault1357(self.pr, self._config)
        if self.pr.number <= 1439:
            return ImageDefault1439(self.pr, self._config)
        if self.pr.number <= 1557:
            return ImageDefault1557(self.pr, self._config)
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
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        pattern = re.compile(
            r"^(.*?)\s+(PASSED|FAILED|SKIPPED|XFAIL|ERROR)\s*(?:\[.*?\])?$"
            r"|^(PASSED|FAILED|SKIPPED|XFAIL|ERROR)\s+(.+)$"
        )
        test_status_map = {}
        for line in test_log.split("\n"):
            line = ansi_escape.sub("", line).strip()
            m = pattern.match(line)
            if not m:
                continue
            if m.group(2):
                name, status = m.group(1).strip(), m.group(2)
            else:
                status, name = m.group(3), m.group(4).strip()
            if name:
                test_status_map[name] = status
        return mapping_to_testresult(test_status_map)
