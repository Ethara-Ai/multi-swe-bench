from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.rust.tauri_apps.tauri_15207_to_1729 import (
    NODE_MODERN_VERSION,
    TAURI_RUST_ENV,
    TauriIntervalBase,
    TauriIntervalImageDefault,
    TauriIntervalInstance,
    tauri_jest,
    tauri_node_install,
    tauri_rust_install,
)

# tauri-apps/tauri PR 1442 (cli/tauri.js jest 26 template spec builds a Rust app).
# Shared base tag: base-1442_to_1442 (rustup + Node 14 + gtk/webkit2gtk-4.0).


class ImageBase1442to1442(TauriIntervalBase):
    TAG = "base-1442_to_1442"

    def extra_env(self) -> str:
        return TAURI_RUST_ENV

    def toolchain(self) -> str:
        return f"""RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl xz-utils file build-essential pkg-config python3 \\
        libssl-dev libgtk-3-dev libwebkit2gtk-4.0-dev libayatana-appindicator3-dev \\
        librsvg2-dev patchelf libvips-dev xvfb xauth \\
    && rm -rf /var/lib/apt/lists/*
{tauri_node_install(NODE_MODERN_VERSION)}
{tauri_rust_install()}"""


class ImageDefault1442to1442(TauriIntervalImageDefault):
    BASE_CLASS = ImageBase1442to1442
    DEFAULT_PLAN = tauri_jest(["cli/tauri.js"], build=True)


@Instance.register("tauri-apps", "tauri_1442_to_1442")
class Tauri1442to1442(TauriIntervalInstance):
    IMAGE_DEFAULT = ImageDefault1442to1442


__all__ = ["ImageBase1442to1442", "ImageDefault1442to1442", "Tauri1442to1442"]
