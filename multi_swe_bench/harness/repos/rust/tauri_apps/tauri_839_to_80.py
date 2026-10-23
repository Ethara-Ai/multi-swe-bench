from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.rust.tauri_apps.tauri_15207_to_1729 import (
    TauriIntervalBase,
    TauriIntervalImageDefault,
    TauriIntervalInstance,
    tauri_jest,
    tauri_node_install,
)

# tauri-apps/tauri PRs 80..839 (cli/tauri.js jest 24-26, sharp 0.23-0.25).
# Shared base tag: base-839_to_80 (Node 12 + gtk/webkit2gtk-4.0 + libvips).

# Node 12.16.3 (2020 era): later 12.x enforces package `exports` strictly and
# breaks @babel/compat-data 7.8.x pinned by these yarn.locks (PR 530).
NODE_VERSION = "12.16.3"


class ImageBase839to80(TauriIntervalBase):
    TAG = "base-839_to_80"

    def extra_env(self) -> str:
        return """    PATH=/usr/local/node/bin:${PATH}"""

    def toolchain(self) -> str:
        return f"""RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl xz-utils build-essential pkg-config python3 \\
        libvips-dev libgtk-3-dev libwebkit2gtk-4.0-dev xvfb xauth \\
    && rm -rf /var/lib/apt/lists/*
{tauri_node_install(NODE_VERSION)}"""


class ImageDefault839to80(TauriIntervalImageDefault):
    BASE_CLASS = ImageBase839to80
    DEFAULT_PLAN = tauri_jest(["cli/tauri.js"], build=True)


@Instance.register("tauri-apps", "tauri_839_to_80")
class Tauri839to80(TauriIntervalInstance):
    IMAGE_DEFAULT = ImageDefault839to80


__all__ = ["ImageBase839to80", "ImageDefault839to80", "Tauri839to80"]
