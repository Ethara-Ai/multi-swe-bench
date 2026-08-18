import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INIT_B64 = "ZGVmIE1JUlJPUiA9ICdodHRwczovL21hdmVuLWNlbnRyYWwuc3RvcmFnZS1kb3dubG9hZC5nb29nbGVhcGlzLmNvbS9tYXZlbjIvJwpkZWYgaXNUaHJvdHRsZWQgPSB7IHUgLT4gdSAhPSBudWxsICYmICh1LmNvbnRhaW5zKCdyZXBvLm1hdmVuLmFwYWNoZS5vcmcnKSB8fCB1LmNvbnRhaW5zKCdyZXBvMS5tYXZlbi5vcmcnKSkgfQpkZWYgcmV3cml0ZSA9IHsgcmVwb3MgLT4KICAgIHJlcG9zLmFsbCB7IHIgLT4KICAgICAgICB0cnkgeyBpZiAoci5oYXNQcm9wZXJ0eSgndXJsJykgJiYgaXNUaHJvdHRsZWQoci51cmw/LnRvU3RyaW5nKCkpKSByLnVybCA9IHVyaShNSVJST1IpIH0gY2F0Y2ggKGlnbm9yZWQpIHt9CiAgICB9Cn0KZ3JhZGxlLnNldHRpbmdzRXZhbHVhdGVkIHsgcyAtPgogICAgdHJ5IHsgcy5wbHVnaW5NYW5hZ2VtZW50LnJlcG9zaXRvcmllcyB7IGdyYWRsZVBsdWdpblBvcnRhbCgpOyBtYXZlbiB7IHVybCBNSVJST1IgfSB9IH0gY2F0Y2ggKGlnbm9yZWQpIHt9CiAgICB0cnkgeyByZXdyaXRlKHMucGx1Z2luTWFuYWdlbWVudC5yZXBvc2l0b3JpZXMpIH0gY2F0Y2ggKGlnbm9yZWQpIHt9CiAgICB0cnkgeyByZXdyaXRlKHMuZGVwZW5kZW5jeVJlc29sdXRpb25NYW5hZ2VtZW50LnJlcG9zaXRvcmllcykgfSBjYXRjaCAoaWdub3JlZCkge30KICAgIHRyeSB7IHMuZGVwZW5kZW5jeVJlc29sdXRpb25NYW5hZ2VtZW50LnJlcG9zaXRvcmllcyB7IG1hdmVuIHsgdXJsIE1JUlJPUiB9IH0gfSBjYXRjaCAoaWdub3JlZCkge30KfQpncmFkbGUuYWxscHJvamVjdHMgeyBwIC0+CiAgICByZXdyaXRlKHAucmVwb3NpdG9yaWVzKQogICAgdHJ5IHsgcmV3cml0ZShwLmJ1aWxkc2NyaXB0LnJlcG9zaXRvcmllcykgfSBjYXRjaCAoaWdub3JlZCkge30KfQo="


def _junit_xml_parse(test_log: str) -> TestResult:
    clean = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)
    passed: set = set(); failed: set = set(); skipped: set = set()
    tc = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    nre = re.compile(r'\bname="([^"]*)"'); cre = re.compile(r'\bclassname="([^"]*)"')
    for m in tc.finditer(clean):
        nm = nre.search(m.group(1)); cn = cre.search(m.group(1))
        if not nm or not cn: continue
        tid = cn.group(1) + "." + nm.group(1); inner = m.group(3) or ""
        if m.group(2) == "/>": passed.add(tid)
        elif "<failure" in inner or "<error" in inner: failed.add(tid)
        elif "<skipped" in inner: skipped.add(tid)
        else: passed.add(tid)
    failed -= passed; skipped -= passed; skipped -= failed
    return TestResult(passed_count=len(passed), failed_count=len(failed),
                      skipped_count=len(skipped), passed_tests=passed,
                      failed_tests=failed, skipped_tests=skipped)


class ImagesToPdfImageBase(Image):
    """Repo-level base: JDK8 + Android SDK (platform-28, build-tools 28.0.3) for this Android app
    (Gradle 5.4.1). The graded test is the JVM unit test app/src/test/.../FileUtilsTest.java, run
    via :app:testDebugUnitTest (no device/emulator needed)."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr; self._config = config
    @property
    def pr(self): return self._pr
    @property
    def config(self): return self._config
    def dependency(self): return "eclipse-temurin:8-jdk"
    def image_prefix(self): return "envagent"
    def image_tag(self): return "base"
    def workdir(self): return "base"
    def files(self): return []
    def dockerfile(self):
        image_name = self.dependency()
        if isinstance(image_name, Image): image_name = image_name.image_full_name()
        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV GRADLE_OPTS="-Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"
ENV CI=true
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates unzip wget && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /root/.gradle && echo '{_INIT_B64}' | base64 -d > /root/.gradle/init.gradle

# Android SDK (cmdline-tools + platform 28 + build-tools 28.0.3)
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk
RUN mkdir -p $ANDROID_SDK_ROOT/cmdline-tools && cd $ANDROID_SDK_ROOT/cmdline-tools \
    && wget -q https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip -O t.zip \
    && unzip -q t.zip && mv cmdline-tools latest && rm t.zip
ENV PATH="$PATH:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools"
RUN yes | sdkmanager --licenses >/dev/null 2>&1 || true
RUN sdkmanager "platform-tools" "platforms;android-28" "build-tools;28.0.3" >/dev/null 2>&1 || true

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
RUN chmod +x ./gradlew || true
RUN timeout --kill-after=30 1500 ./gradlew :app:compileDebugUnitTestSources --no-daemon \
      -Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImagesToPdfImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr; self._config = config
    @property
    def pr(self): return self._pr
    @property
    def config(self): return self._config
    def dependency(self): return ImagesToPdfImageBase(self.pr, self._config)
    def image_prefix(self): return "envagent"
    def image_tag(self): return f"pr-{self.pr.number}"
    def workdir(self): return f"pr-{self.pr.number}"
    def files(self):
        test_cmd = (
            "cd /home/{repo}\n"
            "timeout --kill-after=30 1500 ./gradlew :app:testDebugUnitTest "
            "--tests '*FileUtilsTest' --no-daemon --continue "
            "-Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false || true\n"
            "echo '===== BEGIN TEST RESULTS ====='\n"
            "find /home/{repo} -path '*/build/test-results/*/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null\n"
            "echo '===== END TEST RESULTS ====='"
        ).format(repo=self.pr.repo)
        at = "git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true; find . -name '*.rej' -delete 2>/dev/null || true"
        af = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch; git apply --whitespace=nowarn --reject /home/fix.patch; find . -name '*.rej' -delete; }} 2>/dev/null || true"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", "#!/bin/bash\nset -e\ncd /home/{repo}\ngit reset --hard >/dev/null 2>&1 || true\ngit checkout {sha}\n".format(repo=self.pr.repo, sha=self.pr.base.sha)),
            File(".", "run.sh", f"#!/bin/bash\n{test_cmd}\n"),
            File(".", "test-run.sh", f"#!/bin/bash\ncd /home/{self.pr.repo}\n{at}\n{test_cmd}\n"),
            File(".", "fix-run.sh", f"#!/bin/bash\ncd /home/{self.pr.repo}\n{af}\n{test_cmd}\n"),
        ]
    def dockerfile(self):
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("Swati4star", "Images-to-PDF")
class IMAGES_TO_PDF(Instance):
    def __init__(self, pr, config, *args, **kwargs):
        super().__init__(); self._pr = pr; self._config = config
    @property
    def pr(self): return self._pr
    def dependency(self): return ImagesToPdfImageDefault(self.pr, self._config)
    def run(self, run_cmd=""): return run_cmd or "bash /home/run.sh"
    def test_patch_run(self, c=""): return c or "bash /home/test-run.sh"
    def fix_patch_run(self, c=""): return c or "bash /home/fix-run.sh"
    def parse_log(self, test_log): return _junit_xml_parse(test_log)
