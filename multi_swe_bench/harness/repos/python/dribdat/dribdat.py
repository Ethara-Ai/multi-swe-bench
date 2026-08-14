import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Repo-level base image: OS deps + cloned/checked-out source + baked env.
    Built once as `<repo>:base`; PR images layer only patches + scripts on top."""

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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

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

        # Clone via "${{REPO_URL}}" (the pipeline enhancer leaves this form untouched and
        # injects its git-hardening block just before our trailing CMD); env installs sit
        # after checkout so `pip install -e .` sees the checked-out source.
        return f"""FROM {image_name}
ENV DEBIAN_FRONTEND=noninteractive
# build-essential + libffi-dev needed to build misaka/cffi; libpq for psycopg2 fallbacks.
RUN apt-get update && apt-get install -y git build-essential libffi-dev libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
# Base image `git clone` can land an incomplete packfile; fetch the base commit by URL if missing.
RUN git cat-file -e ${{BASE_COMMIT}} 2>/dev/null || git fetch --no-tags "${{REPO_URL}}" ${{BASE_COMMIT}}
RUN git checkout ${{BASE_COMMIT}}

# --- Environment baked in so human_mode=True works. dribdat's 2020-era poetry.lock is
# unusable with modern OR old Poetry, so install the lock's exact pinned versions
# directly with pip (the dataclasses backport is dropped — it breaks py3.9).
RUN pip install --no-cache-dir "setuptools==57.5.0" wheel
RUN pip install --no-cache-dir --no-build-isolation alembic==1.6.5 aniso8601==7.0.0 atomicwrites==1.4.0 attrs==21.2.0 bcrypt==3.2.0 beautifulsoup4==4.9.3 bleach==3.3.0 blinker==1.4 boto3==1.17.85 botocore==1.20.85 certifi==2021.5.30 cffi==1.14.5 chardet==4.0.0 click==8.0.1 colorama==0.4.4 coverage==5.5 cryptography==3.4.7 cssmin==0.2.0 cssselect==1.1.0 dnspython==1.16.0 email-validator==1.1.2 eventlet==0.30.2 factory-boy==3.2.0 faker==8.4.0 flake8==3.9.2 flake8-blind-except==0.2.0 flake8-debugger==4.0.0 flake8-docstrings==1.6.0 flake8-isort==4.0.0 flake8-polyfill==1.0.2 flake8-quotes==3.2.0 flask==2.0.1 flask-assets==2.0 flask-bcrypt==0.7.1 flask-caching==1.10.1 flask-cors==3.0.10 flask-dance==5.0.0 flask-debugtoolbar==0.11.0 flask-hashing==1.1 flask-login==0.5.0 flask-migrate==3.0.1 flask-misaka==1.0.0 flask-sqlalchemy==2.5.1 flask-talisman==0.7.0 flask-wtf==0.15.1 future==0.18.2 graphene==2.1.8 graphql-core==2.3.2 graphql-relay==2.0.1 greenlet==1.1.0 gunicorn==20.1.0 idna==2.10 iniconfig==1.1.1 isort==5.8.0 itsdangerous==2.0.1 jinja2==3.0.1 jmespath==0.10.0 jsmin==2.2.2 lxml==4.6.3 mako==1.1.4 markupsafe==2.0.1 mccabe==0.6.1 micawber==0.5.3 misaka==2.1.1 oauthlib==3.1.1 packaging==20.9 pep8-naming==0.11.1 pluggy==0.13.1 promise==2.3 psycopg2-binary==2.8.6 py==1.10.0 pycodestyle==2.7.0 pycparser==2.20 pydocstyle==6.1.1 pyflakes==2.3.1 pyopenssl==20.0.1 pyparsing==2.4.7 pyquery==1.4.3 pystache==0.5.4 pytest==6.2.4 python-dateutil==2.8.1 python-dotenv==0.17.1 python-editor==1.0.4 pytz==2021.1 redis==3.5.3 requests==2.25.1 requests-oauthlib==1.3.0 rx==1.6.1 s3transfer==0.4.2 six==1.16.0 snowballstemmer==2.1.0 soupsieve==2.2.1 sqlalchemy==1.4.17 testfixtures==6.17.1 text-unidecode==1.3 toml==0.10.2 typing-extensions==3.10.0.0 urllib3==1.26.5 urlobject==2.4.3 waitress==2.0.0 webassets==2.0 webencodings==0.5.1 webob==1.8.7 webtest==2.0.35 werkzeug==2.0.1 whitenoise==5.2.0 wtforms==2.3.3 zipp==3.4.1 pyyaml==5.4.1

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR-specific image: FROM the repo base, add only patches + run scripts."""

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

    def image_prefix(self) -> str:
        return "envagent"

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
# Re-assert the base commit at PR-build time. Non-destructive on purpose: no `git reset`
# (some bases carry intentional working-tree edits from their env setup) and no test run
# (tests execute at instance time via run/test/fix-run.sh).
cd /home/{pr.repo}
git checkout {pr.base.sha}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest -v -rA tests/
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA tests/
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA tests/
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

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("dribdat", "dribdat")
class Dribdat(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for raw in log.split("\n"):
            line = raw.strip()
            m = re.match(r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(\S+)", line)
            if m:
                status, name = m.group(1), m.group(2)
            else:
                m = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
                if not m:
                    continue
                name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
