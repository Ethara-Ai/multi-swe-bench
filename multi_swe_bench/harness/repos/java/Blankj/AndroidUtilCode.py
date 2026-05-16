import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    if not patch_content:
        return patch_content
    lines = patch_content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('diff --git'):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith('diff --git'):
                if lines[i].startswith('GIT binary patch') or lines[i].startswith('Binary files'):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)


class AndroidImageBase(Image):
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
ENV TZ=Etc/UTC
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    git \\
    openjdk-8-jdk \\
    openjdk-11-jdk \\
    wget \\
    unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8
RUN ln -sf /usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-11
ENV JAVA_HOME=/usr/lib/jvm/java-8

RUN mkdir -p ${{ANDROID_HOME}}/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip -O /tmp/cmdline-tools.zip && \\
    unzip -q /tmp/cmdline-tools.zip -d ${{ANDROID_HOME}}/cmdline-tools && \\
    mv ${{ANDROID_HOME}}/cmdline-tools/cmdline-tools ${{ANDROID_HOME}}/cmdline-tools/latest && \\
    rm /tmp/cmdline-tools.zip

ENV PATH=${{ANDROID_HOME}}/cmdline-tools/latest/bin:${{ANDROID_HOME}}/platform-tools:${{PATH}}

{code}

{self.clear_env}

"""


class AndroidImageDefault(Image):
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
        return AndroidImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _filter_binary_patches(f"{self.pr.fix_patch}"),
            ),
            File(
                ".",
                "test.patch",
                _filter_binary_patches(f"{self.pr.test_patch}"),
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

if [[ -n $(git status --porcelain --diff-filter=ACDMRTUX) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""",
            ),
            File(
                ".",
                "android-fixup.sh",
                """#!/bin/bash
# Centralised gradle surgery for Blankj/AndroidUtilCode.
# Run after every git apply that may have re-introduced jcenter()/blankj refs.
# 1) jcenter -> mavenCentral + google maven + gradle plugin portal.
#    NOTE: inject `maven { url 'https://maven.google.com' }` rather than the
#    bare `google()` shorthand — `google()` is a Gradle 4.0+ method and old-era
#    PRs run Gradle 3.x, where it errors "Could not find method google()".
#    The explicit maven{} form is the same repo and works on every Gradle.
find . -type f -name '*.gradle' -exec sed -i 's|jcenter()|mavenCentral()|g' {} + 2>/dev/null || true
find . -type f -name build.gradle -exec sed -i "s|mavenCentral()|mavenCentral()\\n        maven { url 'https://maven.google.com' }\\n        maven { url 'https://plugins.gradle.org/m2/' }|" {} + 2>/dev/null || true
# 2) Drop bintray classpath / plugin application lines (the dead artifact).
#    Catch every variant: classpath '...', apply plugin: '...', apply plugin: "...", id "...", id '...'
find . -type f -name '*.gradle' -exec sed -i '/com\\.jfrog\\.bintray/d' {} + 2>/dev/null || true
# 3) Drop Blankj's custom plugin refs (bus, free-proguard, api-gradle-plugin, base-transform, adapt-screen)
#    These were only on JCenter; nothing to replace them with, so kill any line mentioning them
#    in a classpath/plugin context (covers single and double quotes).
find . -type f -name '*.gradle' -exec sed -i '/classpath .*com\\.blankj/d' {} + 2>/dev/null || true
find . -type f -name '*.gradle' -exec sed -i '/apply plugin.*com\\.blankj/d' {} + 2>/dev/null || true
# Delete the plugins-DSL apply form `id 'com.blankj.x'` but NOT the gradlePlugin descriptor
# form `id = 'com.blankj.x'` (local plugin modules define their own id this way).
find . -type f -name '*.gradle' -exec sed -i '/id .*com\\.blankj/{/=/!d}' {} + 2>/dev/null || true
# 3a) apply-block form: `apply { plugin "com.blankj.bus" }` — the dead plugin id
#     sits on its own `plugin "..."` line inside an apply{} block, so the
#     `apply plugin:`/`id` seds above miss it. Drop those lines too.
find . -type f -name '*.gradle' -exec sed -i '/^[[:space:]]*plugin[[:space:]].*com\\.blankj/d' {} + 2>/dev/null || true
# 3b) config.gradle era: dead plugins are referenced *indirectly* via a config
#     map/list, which the literal-string seds above cannot see. Two syntaxes:
#       (i)  map entry  `bus_gradle_plugin : "com.blankj:bus-gradle-plugin:1.4"`
#            consumed as `classpath dep.bus_gradle_plugin`  -> drop classpath line
#       (ii) list element `"com.blankj:bus-gradle-plugin:$bus.version",`
#            consumed as `classpath plugin`                 -> drop the element
#     Dead = a Blankj gradle-plugin/base-transform/adapt-screen, OR the bintray
#     plugin (whose `http-builder` transitive is JCenter-only and unresolvable).
python3 - <<'PYEOF'
import re, glob
dead_plugin = re.compile(
    r'com\\.blankj:[a-z0-9-]*(?:gradle-plugin|base-transform|adapt-screen)'
    r'|gradle-bintray-plugin'
    r'|com\\.jfrog\\.bintray'
)
# any double-quoted map entry  key : "value"
entry = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\\s*:\\s*"([^"]*)"')
dead_keys = set()
gradles = [g for g in glob.glob('**/*.gradle', recursive=True) if '/build/' not in g]
for gf in gradles:
    try:
        txt = open(gf).read()
    except Exception:
        continue
    for m in entry.finditer(txt):
        if dead_plugin.search(m.group(2)):
            dead_keys.add(m.group(1))
for gf in gradles:
    try:
        lines = open(gf).read().splitlines(keepends=True)
    except Exception:
        continue
    out = []
    for ln in lines:
        # (i) classpath line referencing a dead-plugin map key
        if 'classpath' in ln and any(k in ln for k in dead_keys):
            continue
        # (ii) a bare dead-plugin string literal — a config list element
        if ln.lstrip().startswith('"') and dead_plugin.search(ln):
            continue
        out.append(ln)
    if len(out) != len(lines):
        open(gf, 'w').writelines(out)
        print('dropped dead-plugin ref in ' + gf)
PYEOF
# 4) buildSrc-era surgery: Config.groovy and DepConfig.groovy hold plugin classpath via Groovy class.
#    Set isApply: false on every DepConfig whose pluginPath references a dead Blankj plugin or bintray.
python3 - <<'PYEOF'
import re, glob, os
# Dead artifacts: only ever on JCenter (or pull JCenter-only transitives onto the classpath).
# DepConfig has multiple constructor shapes; the surgery picks the right one per line:
#   1. named-args:        new DepConfig(pluginPath: ...)             -> inject isApply: false at front
#   2. positional bool:   new DepConfig(true, ...)                   -> first arg IS isApply -> flip to false
#   3. single string:     new DepConfig("com.blankj:swipe-panel:1.2")-> convert to (false, "...") using 2-arg ctor
#   4. explicit named:    new DepConfig(isApply: true, ...)          -> flip true -> false
dead_pat = re.compile(
    r"com\\.blankj:[a-z0-9-]*(?:gradle-plugin|free-proguard|base-transform|adapt-screen|api-gradle-plugin|bus-gradle-plugin|swipe-panel)"
    r"|com\\.jfrog\\.bintray"
    r"|com\\.blankj:base-transform"
)
for p in glob.glob('buildSrc/src/main/**/*.groovy', recursive=True):
    try:
        c = open(p).read()
    except Exception:
        continue
    old = c
    lines = c.splitlines(keepends=True)
    out = []
    for ln in lines:
        if 'new DepConfig' in ln and dead_pat.search(ln):
            # Idempotency: android-fixup.sh runs twice per stage. The check below distinguishes
            # "already-patched" (isApply present, in any value) from "needs-patching" (no isApply).
            if 'isApply' in ln:
                # Already has isApply somewhere — only flip true to false. false stays false.
                ln = re.sub(r'isApply\\s*:\\s*true', 'isApply: false', ln)
            elif re.search(r'new\\s+DepConfig\\s*\\(\\s*(?:true|false)\\b', ln):
                # shape 2 — first positional bool is isApply -> force false
                ln = re.sub(r'(new\\s+DepConfig\\s*\\(\\s*)(?:true|false)\\b', r'\\1false', ln, count=1)
            elif re.search(r'new\\s+DepConfig\\s*\\(\\s*"', ln):
                # shape 3 — single string ctor; rewrite as (isApply=false, path)
                ln = re.sub(r'new\\s+DepConfig\\s*\\(\\s*("[^"]+")', r'new DepConfig(false, \\1', ln, count=1)
            elif re.search(r'new\\s+DepConfig\\s*\\(\\s*[a-zA-Z_]', ln):
                # shape 1 — named-args, no isApply yet -> inject as first key
                ln = re.sub(r'new\\s+DepConfig\\s*\\(\\s*', 'new DepConfig(isApply: false, ', ln, count=1)
            # else: unknown shape, leave alone (safer to fail loud than corrupt the file)
        elif (re.search(r'new\\s+(?:Plugin|Module)Config\\s*\\(', ln)
              and dead_pat.search(ln)
              and re.search(r'isApply\\s*:\\s*true', ln)
              and 'useLocal: true' not in ln):
            # Newer buildSrc era (e.g. PR#1385 refactor): plugins/modules are
            # declared as PluginConfig/ModuleConfig named-arg ctors. Disable any
            # entry whose dead com.blankj artifact is fetched remotely
            # (useLocal: false) by flipping isApply true -> false. useLocal: true
            # entries build from a local path so their dead remotePath is unused.
            ln = re.sub(r'isApply\\s*:\\s*true', 'isApply: false', ln)
        out.append(ln)
    new = ''.join(out)
    if new != old:
        open(p, 'w').write(new)
        print(f'patched: {p}')
PYEOF
# 5) Bintray upload glue files / lines that older PRs had
sed -i '/bintrayUpload/d' utilcode/build.gradle subutil/build.gradle 2>/dev/null || true
rm -f bintrayUpload.gradle 2>/dev/null || true
# 5b) publish.gradle still references bintray-only tasks after we strip the plugin.
#     We KEEP the `apply from:` lines (so the stub loads), and REPLACE publish.gradle's
#     contents with a no-op PublishExtension that swallows any property/method.
#     Use python (not heredoc) to avoid `find -exec` substituting {} inside file contents.
python3 - <<'PYEOF'
import os
STUB = '''// publish.gradle stubbed by registry: no-op PublishExtension so module
// build.gradle files that call `publish [block]` evaluate cleanly.
class PublishExtension {
    def methodMissing(String name, args) { null }
    def propertyMissing(String name) { null }
    def propertyMissing(String name, value) { null }
}
extensions.create("publish", PublishExtension)
'''
for root, dirs, files in os.walk('.'):
    for fn in files:
        full = os.path.join(root, fn)
        if fn == 'publish.gradle' and '/gradle/' in full.replace(os.sep, '/'):
            open(full, 'w').write(STUB)
            print(f'stubbed: {full}')
        elif fn == 'bintrayUpload.gradle':
            open(full, 'w').write('// stubbed by registry\\n')
            print(f'stubbed: {full}')
PYEOF
# Nuke leftover bintray-related dot-references (bintrayUpload.doFirst, etc.)
find . -type f -name '*.gradle' -exec sed -i '/bintrayUpload/d; /bintrayKey/d' {} + 2>/dev/null || true
# 6) AGP-3.5-era project files sometimes reference verifyReleaseResources, a task that doesn't
#    exist until AGP 3.6. Comment out any line that mentions it so configure-phase doesn't fail.
find . -type f -name '*.gradle' -exec sed -i 's|^\\(.*verifyReleaseResources.*\\)$|// stubbed (AGP 3.5 has no verifyReleaseResources) \\1|' {} + 2>/dev/null || true
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

export CI=true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
chmod +x gradlew
bash /home/android-fixup.sh
# Best-effort first build to warm the Gradle cache. Test failures are non-fatal here.
GRADLE_EXCLUDES=""
# AAPT2 (x86_64-only binary) fails under QEMU arm64 in the :app module's
# *Resources tasks. Exclude them ONLY if an :app project actually exists in
# this base SHA (modern config.json-driven layouts have no :app), otherwise
# gradle errors "Project 'app' not found". --continue handles the rest:
# library test tasks don't depend on :app so they still run.
if ./gradlew -q projects 2>/dev/null | grep -q "Project ':app'"; then
  GRADLE_EXCLUDES="-x :app:mergeDebugResources -x :app:mergeReleaseResources -x :app:processDebugResources -x :app:processReleaseResources"
fi
# Clean + no-cache build so the freshly patched sources are always recompiled
# (a stale base-SHA build-cache entry must never shadow a fix-patch class).
./gradlew clean >/dev/null 2>&1 || true
./gradlew test --continue $GRADLE_EXCLUDES --no-build-cache || true
# Gradle does not print per-test results to the console, so dump the JUnit XML
# reports to stdout for parse_log. Markers delimit the block; ::XMLFILE:: tags
# each file. No curly braces here — this script is .format()-substituted.
echo "===== JUNIT XML RESULTS START ====="
for xml in $(find . -path '*/test-results/*' -name 'TEST-*.xml' 2>/dev/null); do
  echo "::XMLFILE:: $xml"
  cat "$xml" 2>/dev/null || true
  echo ""
done
echo "===== JUNIT XML RESULTS END ====="
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
yes | JAVA_HOME=/usr/lib/jvm/java-11 $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager --licenses 2>&1 || true
JAVA_HOME=/usr/lib/jvm/java-11 $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager "platforms;android-24" "platforms;android-25" "platforms;android-26" "platforms;android-27" "platforms;android-29" "build-tools;24.0.3" "build-tools;25.0.2" "build-tools;25.0.3" "build-tools;26.0.0" "build-tools;27.0.2" "build-tools;29.0.3" "platform-tools" 2>&1 || true

cd /home/{pr.repo}
bash /home/android-fixup.sh
GRADLE_EXCLUDES=""
# AAPT2 (x86_64-only binary) fails under QEMU arm64 in the :app module's
# *Resources tasks. Exclude them ONLY if an :app project actually exists in
# this base SHA (modern config.json-driven layouts have no :app), otherwise
# gradle errors "Project 'app' not found". --continue handles the rest:
# library test tasks don't depend on :app so they still run.
if ./gradlew -q projects 2>/dev/null | grep -q "Project ':app'"; then
  GRADLE_EXCLUDES="-x :app:mergeDebugResources -x :app:mergeReleaseResources -x :app:processDebugResources -x :app:processReleaseResources"
fi
# Clean + no-cache build so the freshly patched sources are always recompiled
# (a stale base-SHA build-cache entry must never shadow a fix-patch class).
./gradlew clean >/dev/null 2>&1 || true
./gradlew test --continue $GRADLE_EXCLUDES --no-build-cache || true
# Gradle does not print per-test results to the console, so dump the JUnit XML
# reports to stdout for parse_log. Markers delimit the block; ::XMLFILE:: tags
# each file. No curly braces here — this script is .format()-substituted.
echo "===== JUNIT XML RESULTS START ====="
for xml in $(find . -path '*/test-results/*' -name 'TEST-*.xml' 2>/dev/null); do
  echo "::XMLFILE:: $xml"
  cat "$xml" 2>/dev/null || true
  echo ""
done
echo "===== JUNIT XML RESULTS END ====="

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
yes | JAVA_HOME=/usr/lib/jvm/java-11 $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager --licenses 2>&1 || true
JAVA_HOME=/usr/lib/jvm/java-11 $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager "platforms;android-24" "platforms;android-25" "platforms;android-26" "platforms;android-27" "platforms;android-29" "build-tools;24.0.3" "build-tools;25.0.2" "build-tools;25.0.3" "build-tools;26.0.0" "build-tools;27.0.2" "build-tools;29.0.3" "platform-tools" 2>&1 || true

cd /home/{pr.repo}
# Restore the pristine base-SHA tree first. prepare.sh baked an android-fixup'd
# tree into the image; applying patches onto that mutated tree makes hunks whose
# context android-fixup touched (e.g. buildSrc/Config.groovy) reject — leaving
# an inconsistent tree (deleted DepConfig.groovy but kept its references).
# Patches are diffs against base SHA, so they must apply to a clean base SHA.
git reset --hard
# git reset --hard restores gradlew to its recorded git mode, which on some
# base SHAs is non-executable — re-assert the +x bit prepare.sh set.
chmod +x gradlew
# test.patch is already binary-stripped by _filter_binary_patches().
git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
bash /home/android-fixup.sh
GRADLE_EXCLUDES=""
# AAPT2 (x86_64-only binary) fails under QEMU arm64 in the :app module's
# *Resources tasks. Exclude them ONLY if an :app project actually exists in
# this base SHA (modern config.json-driven layouts have no :app), otherwise
# gradle errors "Project 'app' not found". --continue handles the rest:
# library test tasks don't depend on :app so they still run.
if ./gradlew -q projects 2>/dev/null | grep -q "Project ':app'"; then
  GRADLE_EXCLUDES="-x :app:mergeDebugResources -x :app:mergeReleaseResources -x :app:processDebugResources -x :app:processReleaseResources"
fi
# Clean + no-cache build so the freshly patched sources are always recompiled
# (a stale base-SHA build-cache entry must never shadow a fix-patch class).
./gradlew clean >/dev/null 2>&1 || true
./gradlew test --continue $GRADLE_EXCLUDES --no-build-cache || true
# Gradle does not print per-test results to the console, so dump the JUnit XML
# reports to stdout for parse_log. Markers delimit the block; ::XMLFILE:: tags
# each file. No curly braces here — this script is .format()-substituted.
echo "===== JUNIT XML RESULTS START ====="
for xml in $(find . -path '*/test-results/*' -name 'TEST-*.xml' 2>/dev/null); do
  echo "::XMLFILE:: $xml"
  cat "$xml" 2>/dev/null || true
  echo ""
done
echo "===== JUNIT XML RESULTS END ====="

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
yes | JAVA_HOME=/usr/lib/jvm/java-11 $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager --licenses 2>&1 || true
JAVA_HOME=/usr/lib/jvm/java-11 $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager "platforms;android-24" "platforms;android-25" "platforms;android-26" "platforms;android-27" "platforms;android-29" "build-tools;24.0.3" "build-tools;25.0.2" "build-tools;25.0.3" "build-tools;26.0.0" "build-tools;27.0.2" "build-tools;29.0.3" "platform-tools" 2>&1 || true

cd /home/{pr.repo}
# Restore the pristine base-SHA tree before patching. prepare.sh baked an
# android-fixup'd tree into the image; patches are diffs against base SHA, so
# applying them onto the mutated tree makes android-fixup-touched hunks (e.g.
# buildSrc/Config.groovy) reject while sibling hunks apply — an inconsistent
# tree (e.g. DepConfig.groovy deleted but Config.groovy still references it).
git reset --hard
# git reset --hard restores gradlew to its recorded git mode, which on some
# base SHAs is non-executable — re-assert the +x bit prepare.sh set.
chmod +x gradlew
# NOTE: patches are already binary-stripped by _filter_binary_patches(). They
# are applied in SEPARATE git apply invocations on purpose: a non-applying hunk
# in test.patch (e.g. a stale file removal) must not abort the stream before
# fix.patch — that would leave fix-patch source classes missing and make the
# test compile fail spuriously. android-fixup runs AFTER patches so it never
# perturbs patch context.
git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
bash /home/android-fixup.sh
GRADLE_EXCLUDES=""
# AAPT2 (x86_64-only binary) fails under QEMU arm64 in the :app module's
# *Resources tasks. Exclude them ONLY if an :app project actually exists in
# this base SHA (modern config.json-driven layouts have no :app), otherwise
# gradle errors "Project 'app' not found". --continue handles the rest:
# library test tasks don't depend on :app so they still run.
if ./gradlew -q projects 2>/dev/null | grep -q "Project ':app'"; then
  GRADLE_EXCLUDES="-x :app:mergeDebugResources -x :app:mergeReleaseResources -x :app:processDebugResources -x :app:processReleaseResources"
fi
# Clean + no-cache build so the freshly patched sources are always recompiled
# (a stale base-SHA build-cache entry must never shadow a fix-patch class).
./gradlew clean >/dev/null 2>&1 || true
./gradlew test --continue $GRADLE_EXCLUDES --no-build-cache || true
# Gradle does not print per-test results to the console, so dump the JUnit XML
# reports to stdout for parse_log. Markers delimit the block; ::XMLFILE:: tags
# each file. No curly braces here — this script is .format()-substituted.
echo "===== JUNIT XML RESULTS START ====="
for xml in $(find . -path '*/test-results/*' -name 'TEST-*.xml' 2>/dev/null); do
  echo "::XMLFILE:: $xml"
  cat "$xml" 2>/dev/null || true
  echo ""
done
echo "===== JUNIT XML RESULTS END ====="

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
                    RUN mkdir -p ~/.gradle && \\
                        if [ ! -f "$HOME/.gradle/gradle.properties" ]; then \\
                            touch "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        if ! grep -q "systemProp.http.proxyHost" "$HOME/.gradle/gradle.properties"; then \\
                            echo 'systemProp.http.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.http.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        echo 'export GRADLE_USER_HOME=/root/.gradle' >> ~/.bashrc && \\
                        /bin/bash -c "source ~/.bashrc"
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f ~/.gradle/gradle.properties
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


@Instance.register("Blankj", "AndroidUtilCode")
class AndroidUtilCode(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AndroidImageDefault(self.pr, self._config)

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
        import xml.etree.ElementTree as ET

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # --- Primary: JUnit XML reports dumped between markers by run/test/fix .sh ---
        # Gradle writes per-test results only to build/test-results/*/TEST-*.xml,
        # never to the console, so the shell scripts cat those files into the log.
        block = ""
        if (
            "JUNIT XML RESULTS START" in clean_log
            and "JUNIT XML RESULTS END" in clean_log
        ):
            block = clean_log.split("JUNIT XML RESULTS START", 1)[1].split(
                "JUNIT XML RESULTS END", 1
            )[0]

        for chunk in block.split("::XMLFILE::"):
            chunk = chunk.strip()
            lt = chunk.find("<")
            if lt == -1:
                continue
            xml = chunk[lt:]
            try:
                root = ET.fromstring(xml)
            except Exception:
                continue
            suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
            for suite in suites:
                for tc in suite.iter("testcase"):
                    cls = tc.get("classname") or tc.get("class") or ""
                    nm = tc.get("name") or ""
                    tid = f"{cls}.{nm}" if cls else nm
                    if not tid.strip("."):
                        continue
                    tags = {c.tag for c in list(tc)}
                    if tags & {"failure", "error"}:
                        failed_tests.add(tid)
                    elif "skipped" in tags:
                        skipped_tests.add(tid)
                    else:
                        passed_tests.add(tid)

        # --- Fallback: console "Class > method PASSED" lines (if testLogging on) ---
        if not (passed_tests or failed_tests or skipped_tests):
            test_passed_re = re.compile(r"^(\S.+\s+>\s+.+?)\s+PASSED$")
            test_failed_re = re.compile(r"^(\S.+\s+>\s+.+?)\s+FAILED$")
            test_skipped_re = re.compile(r"^(\S.+\s+>\s+.+?)\s+SKIPPED$")
            for line in clean_log.splitlines():
                m = test_passed_re.match(line)
                if m:
                    passed_tests.add(m.group(1))
                    continue
                m = test_failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    continue
                m = test_skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

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
