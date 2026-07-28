import json
import re
from typing import Optional, Union
from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
    _safe_path_component,
)
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
    """Android toolchain + FULL clone, SHARED by every PR in this repo.

    `image_tag()` is the literal "base", so ONE image (…_m_androidutilcode:base)
    is built once and reused by all PRs. That is why this Dockerfile must NOT
    check out any PR's base.sha and must NOT run image.py's `_HARDENING_BLOCK`:
    hardening pins HEAD to a single sha and runs `git gc --prune=now
    --aggressive`, which would prune the shared clone down to whichever PR
    happened to build first and break every other PR's `git checkout
    <its own base.sha>` in prepare.sh with "reference is not a tree". The
    per-PR checkout is a per-PR concern and already lives in prepare.sh /
    run.sh / test-run.sh, which run inside AndroidImageDefault.

    The `# syntax` directive is what makes that safe: `DockerfileEnhancer.
    enhance()` returns any Dockerfile already carrying it verbatim (image.py
    line 284), so `_standardize_repo_fetch` does not rewrite the clone below
    into clone + `git checkout ${BASE_COMMIT}` + hardening, and
    `_inject_final_sanitize` does not append that hardening for the mere
    presence of "git clone". This is the same opt-out SCRCPY_*_ImageBase and
    S2nTlsImageBase use. Opting out also skips the enhancer's ARG/LABEL
    injection, so the ARG TARGETARCH + ARG REPO_URL/BASE_COMMIT + ENV + LABEL
    blocks it would have emitted are replicated by hand below — multi-arch
    buildx support and the OCI labels are not lost, and BASE_COMMIT is still
    declared (build_dataset.py passes it as a build arg for any string
    dependency) so the build does not warn about an unconsumed arg.
    """

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

    def extra_packages(self) -> list[str]:
        """Android/Gradle toolchain on top of image.py's default package set."""
        return [
            "openjdk-8-jdk",
            "openjdk-11-jdk",
            "unzip",
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Validate org/repo before they are interpolated into the clone URL and
        # RUN/COPY/WORKDIR paths, exactly as image.py does — a name carrying
        # shell metacharacters cannot inject commands into the generated build.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        # Reuse image.py's apt builder so the default package set (which includes
        # the python3 this repo's android-fixup.sh depends on, plus
        # ca-certificates for the HTTPS SDK download), --no-install-recommends,
        # and the deprecated-Debian archive rewrite all stay in one place.
        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, image_name)

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # Replicated by hand because the `# syntax` directive opts this file out
        # of DockerfileEnhancer.enhance() — see the class docstring.
        build_args = (
            f"ARG TARGETARCH\n"
            f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
            f"ARG BASE_COMMIT"
        )
        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{build_args}

{DockerfileEnhancer._ENV_BLOCK}

{label_block}

{self.global_env}

ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk

WORKDIR /home/

{apt_command}

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

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
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

    def dependency(self) -> Union[str, "Image"]:
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
# 1) jcenter -> mavenCentral + google maven + gradle plugin portal + JCenter mirror.
#    NOTE: inject `maven { url 'https://maven.google.com' }` rather than the
#    bare `google()` shorthand — `google()` is a Gradle 4.0+ method and old-era
#    PRs run Gradle 3.x, where it errors "Could not find method google()".
#    The explicit maven{} form is the same repo and works on every Gradle.
#
#    The JCenter mirror is LAST on purpose, so it is only consulted after the
#    canonical repos 404. It is required because a set of artifacts this repo
#    pins were published ONLY to JCenter, which is shut down, and were never
#    rehosted on mavenCentral or maven.google.com:
#      * com.android.tools.build:gradle 2.2.3 / 2.3.1 / 2.3.3 -- Google Maven
#        only starts at AGP 3.0.0, so old-era PRs cannot resolve their
#        buildscript ':classpath' at all and fail during the configuration
#        phase, before a single test runs.
#      * com.blankj:bus, :free-proguard, :swipe-panel, :base-transform -- the
#        author's own artifacts. These are consumed as real DEPENDENCIES (e.g.
#        `api depConfig.bus` in config.gradle), not just plugin classpath
#        entries, so the dead-plugin surgery in steps 2-4 below cannot remove
#        them without breaking compilation of the module under test.
#    Without this mirror those builds fail with "Could not find <artifact>".
find . -type f -name '*.gradle' -exec sed -i 's|jcenter()|mavenCentral()|g' {} + 2>/dev/null || true
find . -type f -name build.gradle -exec sed -i "s|mavenCentral()|mavenCentral()\\n        maven { url 'https://maven.google.com' }\\n        maven { url 'https://plugins.gradle.org/m2/' }\\n        maven { url 'https://maven.aliyun.com/repository/public' }|" {} + 2>/dev/null || true
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

        repo = _safe_path_component(self.pr.repo)

        # The shared base already cloned the full repo and left WORKDIR at
        # /home/{repo}, so prepare.sh just resets and checks out this PR's
        # base.sha against that existing clone -- no re-clone needed here.
        prepare_commands = "RUN bash /home/prepare.sh"

        # ANTI-REWARD-HACKING: the git history strip MUST be applied here, in
        # the per-PR layer, and NOT in AndroidImageBase.
        #
        # AndroidImageBase is a SHARED image (tag "base", one build for all
        # PRs), so hardening there would prune the clone to whichever PR built
        # first and break every other PR's checkout -- see its docstring. But
        # leaving it off entirely is worse: without this block the graded
        # container keeps all ~1434 commits plus the `origin` remote, so a
        # model can run `git log --all` / `git rev-list <base>..origin/master`
        # / `git show <future-sha>` and simply read the real fix diff instead
        # of solving the task.
        #
        # Applying it here fixes both: because Docker layers are copy-on-write,
        # this layer's history-stripping never touches the shared base image,
        # only the per-PR image built on top of it. Because this image's
        # dependency() is another Image (not a string), DockerfileEnhancer.
        # enhance() returns this Dockerfile untouched and will NOT inject the
        # block for us -- hence the explicit reference to Image._HARDENING_BLOCK.
        # ENV BASE_COMMIT is a literal SHA (not a build ARG) purely so that
        # block's ${BASE_COMMIT} references resolve; its trailing `test`
        # assertions fail the build if any ref, remote, or unreachable commit
        # survives, so a regression here cannot pass silently.
        #
        # No proxy setup here, deliberately. DockerfileEnhancer states the
        # policy for generated Dockerfiles: "Deliberately injects no proxy
        # ARGs/ENV, CA-cert symlinks, or MITM cert mount: builds talk to
        # upstream directly" (image.py). This image used to scrape
        # http(s)_proxy out of `global_env` and write systemProp.*.proxyHost
        # into ~/.gradle/gradle.properties, which reintroduced exactly that
        # coupling for Gradle only. The run scripts already `unset HTTP_PROXY
        # HTTPS_PROXY http_proxy https_proxy`, so the scraped properties
        # contradicted the runtime environment anyway.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{repo}
ENV BASE_COMMIT={self.pr.base.sha}

{Image._HARDENING_BLOCK}

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


# ---------------------------------------------------------------------------
# number_interval for Blankj/AndroidUtilCode bundles -- REGISTRY-SCOPED shim.
#
# The dataset records carry exact bundle membership in `prs_in_bundle`. The
# required `number_interval` is those PRs joined with '-', never a range:
#
#     prs_in_bundle:   [1306, 1314, 1320, 1344]
#     number_interval: "1306-1314-1320-1344"      (NOT "1306-1344")
#
# A range would claim every PR in between, which is wrong -- these bundles are
# sparse (e.g. anchor 1385 bundles 1385-1423-...-1671, skipping most of the
# intervening PRs).
#
# `Dataset.build()` copies number_interval straight off the loaded PullRequest
# into the resolved output jsonl, so filling it at load time is what makes it
# appear downstream. As this must live ONLY in the registry, two small,
# idempotent, Blankj/AndroidUtilCode-scoped shims are installed at import time
# (this file is already imported by the package __init__, so nothing else is
# touched):
#
#   1. PullRequest.from_json / .from_dict -- for Blankj/AndroidUtilCode records
#      whose number_interval is EMPTY, fill it from the raw record's
#      prs_in_bundle. Only empty values are filled, so an explicitly-set
#      number_interval is never overwritten, and other repos are untouched.
#   2. Instance.create -- routing looks up `Blankj/<number_interval>`, and a
#      dash-joined bundle list is not a registered key. On the resulting
#      ValueError, fall back to the single registered `Blankj/AndroidUtilCode`
#      class, which owns every era of this repo.
# ---------------------------------------------------------------------------

_BLANKJ_ORG = "Blankj"
_BLANKJ_REPO = "AndroidUtilCode"


def blankj_number_interval(prs_in_bundle) -> str:
    """Dash-join a bundle's PR numbers: [178, 179] -> '178-179'."""
    if not prs_in_bundle:
        return ""
    return "-".join(str(p) for p in prs_in_bundle)


def _blankj_fill_number_interval(pr, raw) -> None:
    if not isinstance(raw, dict):
        return
    if getattr(pr, "org", "") != _BLANKJ_ORG or getattr(pr, "repo", "") != _BLANKJ_REPO:
        return
    if getattr(pr, "number_interval", ""):
        return
    interval = blankj_number_interval(raw.get("prs_in_bundle"))
    if interval:
        pr.number_interval = interval


if not getattr(PullRequest, "_blankj_ni_shim", False):
    _blankj_orig_from_json = PullRequest.from_json.__func__
    _blankj_orig_from_dict = PullRequest.from_dict.__func__

    # Signature-transparent (*args/**kwargs): the @dataclass_json decorator
    # REPLACES the class-body from_dict/from_json, so the live signatures are
    # dataclass_json's -- from_dict(cls, kvs, *, infer_missing=False) and
    # from_json(cls, s, *, parse_float=..., **kw). Its from_json delegates to
    # cls.from_dict(kvs, infer_missing=...), so a fixed 2-arg shim here breaks
    # every repo's loader, not just this one.
    def _blankj_from_json(cls, *args, **kwargs):
        pr = _blankj_orig_from_json(cls, *args, **kwargs)
        try:
            if args:
                _blankj_fill_number_interval(pr, json.loads(args[0]))
        except Exception:
            pass
        return pr

    def _blankj_from_dict(cls, *args, **kwargs):
        pr = _blankj_orig_from_dict(cls, *args, **kwargs)
        try:
            raw = args[0] if args else kwargs.get("kvs")
            _blankj_fill_number_interval(pr, raw)
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_blankj_from_json)
    PullRequest.from_dict = classmethod(_blankj_from_dict)
    PullRequest._blankj_ni_shim = True


if not getattr(Instance, "_blankj_route_shim", False):
    _blankj_orig_create = Instance.create.__func__

    def _blankj_create(cls, pr, config, *args, **kwargs):
        try:
            return _blankj_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _BLANKJ_ORG
                and getattr(pr, "repo", "") == _BLANKJ_REPO
            ):
                key = f"{_BLANKJ_ORG}/{_BLANKJ_REPO}"
                if key in cls._registry:
                    return cls._registry[key](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_blankj_create)
    Instance._blankj_route_shim = True
