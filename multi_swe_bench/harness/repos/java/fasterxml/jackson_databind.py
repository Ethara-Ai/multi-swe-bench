import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class JacksonDatabindImageBase(Image):
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
        return "ubuntu:22.04"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y git openjdk-8-jdk openjdk-17-jdk
RUN apt-get install -y maven

{code}

RUN git clone https://github.com/FasterXML/jackson-bom.git /home/jackson-bom
RUN git clone https://github.com/FasterXML/jackson-parent.git /home/jackson-parent
RUN git clone https://github.com/FasterXML/jackson-core.git /home/jackson-core
RUN git clone https://github.com/FasterXML/jackson-annotations.git /home/jackson-annotations

{self.clear_env}

"""


class JacksonDatabindImageDefault(Image):
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
        return JacksonDatabindImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def old_version(self) -> str:
        old_versions: dict[int, str] = {
            3371: "2.14.0-SNAPSHOT",
            3509: "2.14.0-SNAPSHOT",
            3560: "2.14.0-SNAPSHOT",
            3621: "2.13.5-SNAPSHOT",
            3625: "2.14.0-SNAPSHOT",
            3626: "2.14.0-SNAPSHOT",
            3666: "2.14.1-SNAPSHOT",
            3701: "2.14.2-SNAPSHOT",
            3716: "2.14.2-SNAPSHOT",
            3851: "2.15.0-rc3-SNAPSHOT",
            3860: "2.15.0-rc3-SNAPSHOT",
            4013: "2.16.0-SNAPSHOT",
            4015: "2.16.0-SNAPSHOT",
            4048: "2.16.0-SNAPSHOT",
            4050: "2.16.0-SNAPSHOT",
            4072: "2.16.0-SNAPSHOT",
            4087: "2.16.0-SNAPSHOT",
            4131: "2.16.0-SNAPSHOT",
            4132: "2.16.0-SNAPSHOT",
            4159: "2.16.0-rc1-SNAPSHOT",
            4186: "2.16.0-SNAPSHOT",
            4189: "2.15.4-SNAPSHOT",
            4219: "2.16.1-SNAPSHOT",
            4228: "2.17.0-SNAPSHOT",
            4230: "2.16.1-SNAPSHOT",
            4257: "2.17.0-SNAPSHOT",
            4304: "2.15.4-SNAPSHOT",
            4311: "2.16.2-SNAPSHOT",
            4320: "2.17.0-SNAPSHOT",
            4325: "2.16.2-SNAPSHOT",
            4338: "2.17.0-SNAPSHOT",
            4360: "2.16.2-SNAPSHOT",
            4365: "2.17.0-SNAPSHOT",
            4426: "2.17.0-SNAPSHOT",
            4468: "2.17.1-SNAPSHOT",
            4469: "2.17.1-SNAPSHOT",
            4486: "2.17.1-SNAPSHOT",
            4487: "2.18.0-SNAPSHOT",
            4615: "2.18.0-SNAPSHOT",
            4641: "2.18.0-SNAPSHOT",
        }

        return old_versions.get(self.pr.number, "2.15.0-rc2-SNAPSHOT")

    def new_version(self) -> str:
        new_versions: dict[int, str] = {
            3371: "2.14.4-SNAPSHOT",
            3509: "2.14.4-SNAPSHOT",
            3560: "2.14.4-SNAPSHOT",
            3621: "2.13.6-SNAPSHOT",
            3625: "2.14.4-SNAPSHOT",
            3626: "2.14.4-SNAPSHOT",
            3666: "2.14.4-SNAPSHOT",
            3701: "2.14.4-SNAPSHOT",
            3716: "2.14.4-SNAPSHOT",
            3851: "2.15.5-SNAPSHOT",
            3860: "2.15.5-SNAPSHOT",
            4013: "2.16.3-SNAPSHOT",
            4015: "2.16.3-SNAPSHOT",
            4048: "2.16.3-SNAPSHOT",
            4050: "2.16.3-SNAPSHOT",
            4072: "2.16.3-SNAPSHOT",
            4087: "2.16.3-SNAPSHOT",
            4131: "2.16.3-SNAPSHOT",
            4132: "2.16.3-SNAPSHOT",
            4159: "2.16.3-SNAPSHOT",
            4186: "2.16.3-SNAPSHOT",
            4189: "2.15.5-SNAPSHOT",
            4219: "2.16.3-SNAPSHOT",
            4228: "2.17.4-SNAPSHOT",
            4230: "2.16.3-SNAPSHOT",
            4257: "2.17.4-SNAPSHOT",
            4304: "2.15.5-SNAPSHOT",
            4311: "2.16.3-SNAPSHOT",
            4320: "2.17.4-SNAPSHOT",
            4325: "2.16.3-SNAPSHOT",
            4338: "2.17.4-SNAPSHOT",
            4360: "2.16.3-SNAPSHOT",
            4365: "2.17.4-SNAPSHOT",
            4426: "2.17.4-SNAPSHOT",
            4468: "2.17.4-SNAPSHOT",
            4469: "2.17.4-SNAPSHOT",
            4486: "2.17.4-SNAPSHOT",
            4487: "2.18.5-SNAPSHOT",
            4615: "2.18.5-SNAPSHOT",
            4641: "2.18.5-SNAPSHOT",
        }

        return new_versions.get(self.pr.number, "2.15.5-SNAPSHOT")

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
                "install_parent_poms.sh",
                r"""#!/bin/bash
# Install jackson-bom/jackson-base/jackson-parent POMs into local Maven repo
# so that jackson-databind SNAPSHOT builds can resolve their parent chain.
set -e

REPO_POM="$1"  # path to jackson-databind's pom.xml

if [ ! -f "$REPO_POM" ]; then
    echo "install_parent_poms: pom.xml not found at $REPO_POM"
    exit 1
fi

# Extract parent artifactId and version from jackson-databind pom.xml
PARENT_ARTIFACT=$(grep -A 10 '<parent>' "$REPO_POM" | grep '<artifactId>' | head -1 | sed 's/.*<artifactId>//;s/<.*//')
PARENT_VERSION=$(grep -A 10 '<parent>' "$REPO_POM" | grep '<version>' | head -1 | sed 's/.*<version>//;s/<.*//')

if [ -z "$PARENT_VERSION" ]; then
    echo "install_parent_poms: Could not extract parent version"
    exit 0
fi

echo "install_parent_poms: parent=$PARENT_ARTIFACT version=$PARENT_VERSION"

# Check if it's a SNAPSHOT — if not, Maven Central should have it
if [[ "$PARENT_VERSION" != *-SNAPSHOT ]]; then
    echo "install_parent_poms: Parent is a release version, should resolve from Maven Central"
    exit 0
fi

# Determine the minor version branch (e.g., 2.18.5-SNAPSHOT -> 2.18, 3.1.0-SNAPSHOT -> 3.1)
MINOR_VERSION=$(echo "$PARENT_VERSION" | sed 's/\([0-9]*\.[0-9]*\).*/\1/')
echo "install_parent_poms: minor version branch: $MINOR_VERSION"

# ---- Helper: compute nearest release version for dependency overrides ----
compute_release_version() {
    local VER="$1"
    # Strip -SNAPSHOT suffix
    local BASE_VER=$(echo "$VER" | sed 's/-SNAPSHOT//')

    # Handle 3.0.0-rc*-SNAPSHOT: use previous rc (e.g., rc3 -> rc2, rc2 -> rc1)
    if [[ "$VER" =~ ^3\.0\.0-rc([0-9]+)-SNAPSHOT$ ]]; then
        local RC_NUM="${BASH_REMATCH[1]}"
        if [ "$RC_NUM" -gt 1 ]; then
            echo "3.0.0-rc$((RC_NUM - 1))"
        else
            echo "3.0.0-rc1"
        fi
        return
    fi

    # Handle other rc-SNAPSHOT (e.g., 2.15.0-rc2-SNAPSHOT -> previous minor's latest)
    if [[ "$VER" == *-rc*-SNAPSHOT ]]; then
        local MM=$(echo "$BASE_VER" | sed 's/-rc.*//' | sed 's/\.[0-9]*$//')
        local MINOR_NUM=$(echo "$MM" | awk -F. '{print $2}')
        local MAJOR_NUM=$(echo "$MM" | awk -F. '{print $1}')
        if [ "$MINOR_NUM" -gt 0 ]; then
            echo "${MAJOR_NUM}.$((MINOR_NUM - 1)).3"
        else
            echo "${MM}.0"
        fi
        return
    fi

    # Handle 3.0.0-SNAPSHOT: no released 3.0.0 yet, use 3.0.0-rc1
    if [[ "$VER" == "3.0.0-SNAPSHOT" ]]; then
        echo "3.0.0-rc1"
        return
    fi

    # Handle 3.1.0-SNAPSHOT: use latest 3.x release
    if [[ "$VER" =~ ^3\.[0-9]+\.0-SNAPSHOT$ ]]; then
        echo "3.0.0-rc4"
        return
    fi

    # Regular X.Y.Z-SNAPSHOT
    local MAJOR_MINOR=$(echo "$BASE_VER" | sed 's/\.[0-9]*$//')
    local PATCH=$(echo "$BASE_VER" | grep -oE '[0-9]+$')

    if [ "$PATCH" -gt 0 ] 2>/dev/null; then
        echo "${MAJOR_MINOR}.$((PATCH - 1))"
    else
        # X.Y.0-SNAPSHOT: use previous minor's latest
        local MAJOR_NUM=$(echo "$MAJOR_MINOR" | awk -F. '{print $1}')
        local MINOR_NUM=$(echo "$MAJOR_MINOR" | awk -F. '{print $2}')
        if [ "$MINOR_NUM" -gt 0 ]; then
            echo "${MAJOR_NUM}.$((MINOR_NUM - 1)).0"
        else
            echo "${MAJOR_MINOR}.0"
        fi
    fi
}

RELEASE_VERSION=$(compute_release_version "$PARENT_VERSION")
echo "install_parent_poms: nearest release for deps: $RELEASE_VERSION"

# ---- Handle jackson-parent (old 2.8/2.9 PRs use jackson-parent directly) ----
if [[ "$PARENT_ARTIFACT" == "jackson-parent" ]]; then
    echo "install_parent_poms: Parent is jackson-parent (not jackson-base)"
    PARENT_DIR="/home/jackson-parent"
    if [ ! -d "$PARENT_DIR" ]; then
        echo "install_parent_poms: jackson-parent not found at $PARENT_DIR, skipping"
        exit 0
    fi
    cd "$PARENT_DIR"
    git reset --hard 2>/dev/null || true
    if ! git checkout "$MINOR_VERSION" 2>/dev/null; then
        git checkout "2.x" 2>/dev/null || { echo "install_parent_poms: No branch for $MINOR_VERSION"; exit 1; }
    fi
    # Rewrite version to match what jackson-databind expects
    CURRENT_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
    echo "install_parent_poms: jackson-parent branch version: $CURRENT_VERSION -> rewriting to $PARENT_VERSION"
    sed -i "s|<version>${CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" pom.xml
    mvn install -N -q -DskipTests -Denforcer.skip=true 2>&1 || echo "install_parent_poms: WARNING - jackson-parent install failed"
    git checkout -- . 2>/dev/null || true
    echo "install_parent_poms: jackson-parent Done"
    exit 0
fi

# ---- Standard path: jackson-base -> jackson-bom chain ----
BOM_DIR="/home/jackson-bom"
if [ ! -d "$BOM_DIR" ]; then
    echo "install_parent_poms: jackson-bom not found at $BOM_DIR"
    exit 1
fi

cd "$BOM_DIR"
git reset --hard 2>/dev/null || true

# Checkout the right branch
if ! git checkout "$MINOR_VERSION" 2>/dev/null; then
    if [[ "$MINOR_VERSION" == 3.* ]]; then
        git checkout "3.x" 2>/dev/null || { echo "install_parent_poms: No branch for $MINOR_VERSION"; exit 1; }
    else
        git checkout "2.x" 2>/dev/null || { echo "install_parent_poms: No branch for $MINOR_VERSION"; exit 1; }
    fi
fi

# Get the current version on this branch
BOM_CURRENT_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
echo "install_parent_poms: Branch tip version: $BOM_CURRENT_VERSION -> rewriting to $PARENT_VERSION"

# Rewrite version in jackson-bom pom.xml
sed -i "s|<version>${BOM_CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" pom.xml
sed -i "s|<jackson.version>${BOM_CURRENT_VERSION}</jackson.version>|<jackson.version>${PARENT_VERSION}</jackson.version>|g" pom.xml

# Rewrite version in jackson-base pom.xml
if [ -f base/pom.xml ]; then
    sed -i "s|<version>${BOM_CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" base/pom.xml
fi

# For 3.x SNAPSHOT: build jackson-core from source instead of using release override
# For 2.x: override to nearest release (as before)
if [[ "$PARENT_VERSION" == 3.*-SNAPSHOT ]]; then
    echo "install_parent_poms: 3.x SNAPSHOT detected — will build jackson-core from source"
    # Don't override jackson.version.core for 3.x — we'll install SNAPSHOT jar from source
    # Still override annotations since jackson-annotations 3.x uses release versions
    sed -i "s|<jackson.version.annotations>\${jackson.version}</jackson.version.annotations>|<jackson.version.annotations>${RELEASE_VERSION}</jackson.version.annotations>|" pom.xml
else
    # 2.x: override both to nearest release
    sed -i "s|<jackson.version.annotations>\${jackson.version}</jackson.version.annotations>|<jackson.version.annotations>${RELEASE_VERSION}</jackson.version.annotations>|" pom.xml
    sed -i "s|<jackson.version.core>\${jackson.version}</jackson.version.core>|<jackson.version.core>${RELEASE_VERSION}</jackson.version.core>|" pom.xml
fi

# Install jackson-bom POM to local repo
mvn install -N -q -DskipTests -Denforcer.skip=true 2>&1 || echo "install_parent_poms: WARNING - jackson-bom install failed"

# Install jackson-base POM to local repo
if [ -f base/pom.xml ]; then
    cd base
    mvn install -N -q -DskipTests -Denforcer.skip=true 2>&1 || echo "install_parent_poms: WARNING - jackson-base install failed"
    cd ..
fi

# Reset to clean state
git checkout -- . 2>/dev/null || true

# ---- Build jackson-core from source for 3.x SNAPSHOT ----
if [[ "$PARENT_VERSION" == 3.*-SNAPSHOT ]]; then
    CORE_DIR="/home/jackson-core"
    if [ -d "$CORE_DIR" ]; then
        echo "install_parent_poms: Building jackson-core from source for $PARENT_VERSION"
        cd "$CORE_DIR"
        git reset --hard 2>/dev/null || true

        # Checkout the matching branch (3.0 for 3.0.x, 3.1 for 3.1.x, etc.)
        if ! git checkout "$MINOR_VERSION" 2>/dev/null; then
            git checkout "3.x" 2>/dev/null || { echo "install_parent_poms: No jackson-core branch for $MINOR_VERSION"; }
        fi

        # Get jackson-core's current version on this branch
        CORE_CURRENT_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
        echo "install_parent_poms: jackson-core branch version: $CORE_CURRENT_VERSION -> rewriting to $PARENT_VERSION"

        # Rewrite jackson-core version to match what jackson-databind expects
        sed -i "s|<version>${CORE_CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" pom.xml

        # Also rewrite parent version reference in jackson-core pom
        CORE_PARENT_VERSION=$(grep -A 10 '<parent>' pom.xml | grep '<version>' | head -1 | sed 's/.*<version>//;s/<.*//')
        if [ -n "$CORE_PARENT_VERSION" ] && [ "$CORE_PARENT_VERSION" != "$PARENT_VERSION" ]; then
            sed -i "0,/<version>${CORE_PARENT_VERSION}<\/version>/s|<version>${CORE_PARENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|" pom.xml
        fi

        # Use Java 17 for 3.x builds
        export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
        export PATH="$JAVA_HOME/bin:$PATH"

        # Build and install jackson-core (skip tests for speed)
        mvn install -DskipTests -Denforcer.skip=true -q 2>&1 || echo "install_parent_poms: WARNING - jackson-core build failed"

        # Reset
        git checkout -- . 2>/dev/null || true
        echo "install_parent_poms: jackson-core build complete"
    else
        echo "install_parent_poms: jackson-core not found at $CORE_DIR, falling back to release version"
    fi

    # Also build jackson-annotations from source if needed
    ANNOT_DIR="/home/jackson-annotations"
    if [ -d "$ANNOT_DIR" ]; then
        echo "install_parent_poms: Building jackson-annotations from source for $PARENT_VERSION"
        cd "$ANNOT_DIR"
        git reset --hard 2>/dev/null || true

        if ! git checkout "$MINOR_VERSION" 2>/dev/null; then
            git checkout "3.x" 2>/dev/null || { echo "install_parent_poms: No jackson-annotations branch for $MINOR_VERSION"; }
        fi

        ANNOT_CURRENT_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
        echo "install_parent_poms: jackson-annotations branch version: $ANNOT_CURRENT_VERSION"

        # Only build if it's a SNAPSHOT (some branches use release versions like 3.0-NEVER-SNAPSHOT)
        if [[ "$ANNOT_CURRENT_VERSION" == *SNAPSHOT* ]] && [[ "$ANNOT_CURRENT_VERSION" != *NEVER* ]]; then
            sed -i "s|<version>${ANNOT_CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" pom.xml
            ANNOT_PARENT_VERSION=$(grep -A 10 '<parent>' pom.xml | grep '<version>' | head -1 | sed 's/.*<version>//;s/<.*//')
            if [ -n "$ANNOT_PARENT_VERSION" ] && [ "$ANNOT_PARENT_VERSION" != "$PARENT_VERSION" ]; then
                sed -i "0,/<version>${ANNOT_PARENT_VERSION}<\/version>/s|<version>${ANNOT_PARENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|" pom.xml
            fi
            mvn install -DskipTests -Denforcer.skip=true -q 2>&1 || echo "install_parent_poms: WARNING - jackson-annotations build failed"
        else
            echo "install_parent_poms: jackson-annotations uses release version ($ANNOT_CURRENT_VERSION), skipping source build"
        fi

        git checkout -- . 2>/dev/null || true
    fi
fi

echo "install_parent_poms: Done"
""",
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

file="/home/{pr.repo}/pom.xml"
old_version="{old_version}"
new_version="{new_version}"
sed -i "s/$old_version/$new_version/g" "$file"

# Install parent POMs (jackson-bom, jackson-base, jackson-parent) into local Maven repo
bash /home/install_parent_poms.sh "$file"

# Select Java version based on jackson major version
JACKSON_VERSION=$(grep '<version>' "$file" | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
if [[ "$JACKSON_VERSION" == 3.* ]]; then
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
    export PATH="$JAVA_HOME/bin:$PATH"
    echo "prepare: Using Java 17 for jackson 3.x"
fi

mvn clean test -Dmaven.test.skip=false -DfailIfNoTests=false -Denforcer.skip=true || true
""".format(
                    pr=self.pr,
                    old_version=self.old_version(),
                    new_version=self.new_version(),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Select Java version based on jackson major version
JACKSON_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
if [[ "$JACKSON_VERSION" == 3.* ]]; then
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
    export PATH="$JAVA_HOME/bin:$PATH"
fi

mvn clean test -Dmaven.test.skip=false -DfailIfNoTests=false -Denforcer.skip=true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

# Select Java version based on jackson major version
JACKSON_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
if [[ "$JACKSON_VERSION" == 3.* ]]; then
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
    export PATH="$JAVA_HOME/bin:$PATH"
fi

mvn clean test -Dmaven.test.skip=false -DfailIfNoTests=false -Denforcer.skip=true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Select Java version based on jackson major version
JACKSON_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
if [[ "$JACKSON_VERSION" == 3.* ]]; then
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
    export PATH="$JAVA_HOME/bin:$PATH"
fi

mvn clean test -Dmaven.test.skip=false -DfailIfNoTests=false -Denforcer.skip=true

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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            # Extract proxy host and port
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


class JacksonDatabindImage3851(Image):
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
        return JacksonDatabindImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def old_version(self) -> str:
        return "2.15.0-rc3-SNAPSHOT"

    def new_version(self) -> str:
        return "2.15.5-SNAPSHOT"

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
                "install_parent_poms.sh",
                r"""#!/bin/bash
# Install jackson-bom/jackson-base/jackson-parent POMs into local Maven repo
# so that jackson-databind SNAPSHOT builds can resolve their parent chain.
set -e

REPO_POM="$1"  # path to jackson-databind's pom.xml

if [ ! -f "$REPO_POM" ]; then
    echo "install_parent_poms: pom.xml not found at $REPO_POM"
    exit 1
fi

# Extract parent artifactId and version from jackson-databind pom.xml
PARENT_ARTIFACT=$(grep -A 10 '<parent>' "$REPO_POM" | grep '<artifactId>' | head -1 | sed 's/.*<artifactId>//;s/<.*//')
PARENT_VERSION=$(grep -A 10 '<parent>' "$REPO_POM" | grep '<version>' | head -1 | sed 's/.*<version>//;s/<.*//')

if [ -z "$PARENT_VERSION" ]; then
    echo "install_parent_poms: Could not extract parent version"
    exit 0
fi

echo "install_parent_poms: parent=$PARENT_ARTIFACT version=$PARENT_VERSION"

# Check if it's a SNAPSHOT — if not, Maven Central should have it
if [[ "$PARENT_VERSION" != *-SNAPSHOT ]]; then
    echo "install_parent_poms: Parent is a release version, should resolve from Maven Central"
    exit 0
fi

# Determine the minor version branch (e.g., 2.18.5-SNAPSHOT -> 2.18, 3.1.0-SNAPSHOT -> 3.1)
MINOR_VERSION=$(echo "$PARENT_VERSION" | sed 's/\([0-9]*\.[0-9]*\).*/\1/')
echo "install_parent_poms: minor version branch: $MINOR_VERSION"

# ---- Helper: compute nearest release version for dependency overrides ----
compute_release_version() {
    local VER="$1"
    local BASE_VER=$(echo "$VER" | sed 's/-SNAPSHOT//')

    if [[ "$VER" =~ ^3\.0\.0-rc([0-9]+)-SNAPSHOT$ ]]; then
        local RC_NUM="${BASH_REMATCH[1]}"
        if [ "$RC_NUM" -gt 1 ]; then
            echo "3.0.0-rc$((RC_NUM - 1))"
        else
            echo "3.0.0-rc1"
        fi
        return
    fi

    if [[ "$VER" == *-rc*-SNAPSHOT ]]; then
        local MM=$(echo "$BASE_VER" | sed 's/-rc.*//' | sed 's/\.[0-9]*$//')
        local MINOR_NUM=$(echo "$MM" | awk -F. '{print $2}')
        local MAJOR_NUM=$(echo "$MM" | awk -F. '{print $1}')
        if [ "$MINOR_NUM" -gt 0 ]; then
            echo "${MAJOR_NUM}.$((MINOR_NUM - 1)).3"
        else
            echo "${MM}.0"
        fi
        return
    fi

    if [[ "$VER" == "3.0.0-SNAPSHOT" ]]; then
        echo "3.0.0-rc1"
        return
    fi

    if [[ "$VER" =~ ^3\.[0-9]+\.0-SNAPSHOT$ ]]; then
        echo "3.0.0-rc4"
        return
    fi

    local MAJOR_MINOR=$(echo "$BASE_VER" | sed 's/\.[0-9]*$//')
    local PATCH=$(echo "$BASE_VER" | grep -oE '[0-9]+$')

    if [ "$PATCH" -gt 0 ] 2>/dev/null; then
        echo "${MAJOR_MINOR}.$((PATCH - 1))"
    else
        local MAJOR_NUM=$(echo "$MAJOR_MINOR" | awk -F. '{print $1}')
        local MINOR_NUM=$(echo "$MAJOR_MINOR" | awk -F. '{print $2}')
        if [ "$MINOR_NUM" -gt 0 ]; then
            echo "${MAJOR_NUM}.$((MINOR_NUM - 1)).0"
        else
            echo "${MAJOR_MINOR}.0"
        fi
    fi
}

RELEASE_VERSION=$(compute_release_version "$PARENT_VERSION")
echo "install_parent_poms: nearest release for deps: $RELEASE_VERSION"

# ---- Handle jackson-parent (old 2.8/2.9 PRs use jackson-parent directly) ----
if [[ "$PARENT_ARTIFACT" == "jackson-parent" ]]; then
    echo "install_parent_poms: Parent is jackson-parent (not jackson-base)"
    PARENT_DIR="/home/jackson-parent"
    if [ ! -d "$PARENT_DIR" ]; then
        echo "install_parent_poms: jackson-parent not found at $PARENT_DIR, skipping"
        exit 0
    fi
    cd "$PARENT_DIR"
    git reset --hard 2>/dev/null || true
    if ! git checkout "$MINOR_VERSION" 2>/dev/null; then
        git checkout "2.x" 2>/dev/null || { echo "install_parent_poms: No branch for $MINOR_VERSION"; exit 1; }
    fi
    CURRENT_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
    echo "install_parent_poms: jackson-parent branch version: $CURRENT_VERSION -> rewriting to $PARENT_VERSION"
    sed -i "s|<version>${CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" pom.xml
    mvn install -N -q -DskipTests -Denforcer.skip=true 2>&1 || echo "install_parent_poms: WARNING - jackson-parent install failed"
    git checkout -- . 2>/dev/null || true
    echo "install_parent_poms: jackson-parent Done"
    exit 0
fi

# ---- Standard path: jackson-base -> jackson-bom chain ----
BOM_DIR="/home/jackson-bom"
if [ ! -d "$BOM_DIR" ]; then
    echo "install_parent_poms: jackson-bom not found at $BOM_DIR"
    exit 1
fi

cd "$BOM_DIR"
git reset --hard 2>/dev/null || true

if ! git checkout "$MINOR_VERSION" 2>/dev/null; then
    if [[ "$MINOR_VERSION" == 3.* ]]; then
        git checkout "3.x" 2>/dev/null || { echo "install_parent_poms: No branch for $MINOR_VERSION"; exit 1; }
    else
        git checkout "2.x" 2>/dev/null || { echo "install_parent_poms: No branch for $MINOR_VERSION"; exit 1; }
    fi
fi

BOM_CURRENT_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
echo "install_parent_poms: Branch tip version: $BOM_CURRENT_VERSION -> rewriting to $PARENT_VERSION"

sed -i "s|<version>${BOM_CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" pom.xml
sed -i "s|<jackson.version>${BOM_CURRENT_VERSION}</jackson.version>|<jackson.version>${PARENT_VERSION}</jackson.version>|g" pom.xml

if [ -f base/pom.xml ]; then
    sed -i "s|<version>${BOM_CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" base/pom.xml
fi

if [[ "$PARENT_VERSION" == 3.*-SNAPSHOT ]]; then
    echo "install_parent_poms: 3.x SNAPSHOT detected — will build jackson-core from source"
    sed -i "s|<jackson.version.annotations>\${jackson.version}</jackson.version.annotations>|<jackson.version.annotations>${RELEASE_VERSION}</jackson.version.annotations>|" pom.xml
else
    sed -i "s|<jackson.version.annotations>\${jackson.version}</jackson.version.annotations>|<jackson.version.annotations>${RELEASE_VERSION}</jackson.version.annotations>|" pom.xml
    sed -i "s|<jackson.version.core>\${jackson.version}</jackson.version.core>|<jackson.version.core>${RELEASE_VERSION}</jackson.version.core>|" pom.xml
fi

mvn install -N -q -DskipTests -Denforcer.skip=true 2>&1 || echo "install_parent_poms: WARNING - jackson-bom install failed"

if [ -f base/pom.xml ]; then
    cd base
    mvn install -N -q -DskipTests -Denforcer.skip=true 2>&1 || echo "install_parent_poms: WARNING - jackson-base install failed"
    cd ..
fi

git checkout -- . 2>/dev/null || true

if [[ "$PARENT_VERSION" == 3.*-SNAPSHOT ]]; then
    CORE_DIR="/home/jackson-core"
    if [ -d "$CORE_DIR" ]; then
        echo "install_parent_poms: Building jackson-core from source for $PARENT_VERSION"
        cd "$CORE_DIR"
        git reset --hard 2>/dev/null || true

        if ! git checkout "$MINOR_VERSION" 2>/dev/null; then
            git checkout "3.x" 2>/dev/null || { echo "install_parent_poms: No jackson-core branch for $MINOR_VERSION"; }
        fi

        CORE_CURRENT_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
        echo "install_parent_poms: jackson-core branch version: $CORE_CURRENT_VERSION -> rewriting to $PARENT_VERSION"

        sed -i "s|<version>${CORE_CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" pom.xml

        CORE_PARENT_VERSION=$(grep -A 10 '<parent>' pom.xml | grep '<version>' | head -1 | sed 's/.*<version>//;s/<.*//')
        if [ -n "$CORE_PARENT_VERSION" ] && [ "$CORE_PARENT_VERSION" != "$PARENT_VERSION" ]; then
            sed -i "0,/<version>${CORE_PARENT_VERSION}<\/version>/s|<version>${CORE_PARENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|" pom.xml
        fi

        export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
        export PATH="$JAVA_HOME/bin:$PATH"

        mvn install -DskipTests -Denforcer.skip=true -q 2>&1 || echo "install_parent_poms: WARNING - jackson-core build failed"

        git checkout -- . 2>/dev/null || true
        echo "install_parent_poms: jackson-core build complete"
    fi

    ANNOT_DIR="/home/jackson-annotations"
    if [ -d "$ANNOT_DIR" ]; then
        echo "install_parent_poms: Building jackson-annotations from source for $PARENT_VERSION"
        cd "$ANNOT_DIR"
        git reset --hard 2>/dev/null || true

        if ! git checkout "$MINOR_VERSION" 2>/dev/null; then
            git checkout "3.x" 2>/dev/null || { echo "install_parent_poms: No jackson-annotations branch for $MINOR_VERSION"; }
        fi

        ANNOT_CURRENT_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
        echo "install_parent_poms: jackson-annotations branch version: $ANNOT_CURRENT_VERSION"

        if [[ "$ANNOT_CURRENT_VERSION" == *SNAPSHOT* ]] && [[ "$ANNOT_CURRENT_VERSION" != *NEVER* ]]; then
            sed -i "s|<version>${ANNOT_CURRENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|g" pom.xml
            ANNOT_PARENT_VERSION=$(grep -A 10 '<parent>' pom.xml | grep '<version>' | head -1 | sed 's/.*<version>//;s/<.*//')
            if [ -n "$ANNOT_PARENT_VERSION" ] && [ "$ANNOT_PARENT_VERSION" != "$PARENT_VERSION" ]; then
                sed -i "0,/<version>${ANNOT_PARENT_VERSION}<\/version>/s|<version>${ANNOT_PARENT_VERSION}</version>|<version>${PARENT_VERSION}</version>|" pom.xml
            fi
            mvn install -DskipTests -Denforcer.skip=true -q 2>&1 || echo "install_parent_poms: WARNING - jackson-annotations build failed"
        else
            echo "install_parent_poms: jackson-annotations uses release version ($ANNOT_CURRENT_VERSION), skipping source build"
        fi

        git checkout -- . 2>/dev/null || true
    fi
fi

echo "install_parent_poms: Done"
""",
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

file="/home/{pr.repo}/pom.xml"
old_version="{old_version}"
new_version="{new_version}"
sed -i "s/$old_version/$new_version/g" "$file"

# Install parent POMs (jackson-bom, jackson-base, jackson-parent) into local Maven repo
bash /home/install_parent_poms.sh "$file"

# Select Java version based on jackson major version
JACKSON_VERSION=$(grep '<version>' "$file" | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
if [[ "$JACKSON_VERSION" == 3.* ]]; then
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
    export PATH="$JAVA_HOME/bin:$PATH"
    echo "prepare: Using Java 17 for jackson 3.x"
fi

mvn clean test -Dmaven.test.skip=false -DfailIfNoTests=false -Denforcer.skip=true || true
""".format(
                    pr=self.pr,
                    old_version=self.old_version(),
                    new_version=self.new_version(),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Select Java version based on jackson major version
JACKSON_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
if [[ "$JACKSON_VERSION" == 3.* ]]; then
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
    export PATH="$JAVA_HOME/bin:$PATH"
fi

mvn clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -Dtest=com.fasterxml.jackson.databind.deser.creators.JsonCreatorModeForEnum3566 -DfailIfNoTests=false -Denforcer.skip=true -am
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

# Select Java version based on jackson major version
JACKSON_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
if [[ "$JACKSON_VERSION" == 3.* ]]; then
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
    export PATH="$JAVA_HOME/bin:$PATH"
fi

mvn clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -Dtest=com.fasterxml.jackson.databind.deser.creators.JsonCreatorModeForEnum3566 -DfailIfNoTests=false -Denforcer.skip=true -am

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Select Java version based on jackson major version
JACKSON_VERSION=$(grep '<version>' pom.xml | head -2 | tail -1 | sed 's/.*<version>//;s/<.*//')
if [[ "$JACKSON_VERSION" == 3.* ]]; then
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
    export PATH="$JAVA_HOME/bin:$PATH"
fi

mvn clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -Dtest=com.fasterxml.jackson.databind.deser.creators.JsonCreatorModeForEnum3566 -DfailIfNoTests=false -Denforcer.skip=true -am

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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            # Extract proxy host and port
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("fasterxml", "jackson-databind")
class JacksonDatabind(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number == 3851:
            return JacksonDatabindImage3851(self.pr, self._config)

        return JacksonDatabindImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.]+ .+? in (.+)"
        )

        for line in test_log.splitlines():
            match = pattern.search(line)
            if match:
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5)

                if (
                    tests_run > 0
                    and failures == 0
                    and errors == 0
                    and skipped != tests_run
                ):
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
