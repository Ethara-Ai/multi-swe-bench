from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.rust.tauri_apps.tauri_15207_to_1729 import (
    NODE_LEGACY_VERSION,
    TAURI_GO,
    TAURI_GO_INSTALL,
    TauriIntervalBase,
    TauriIntervalImageDefault,
    TauriIntervalInstance,
    tauri_node_install,
)

# tauri-apps/tauri PRs 25..73 (Go bindings + root jest 24).
# Shared base tag: base-73_to_25 (Go 1.13 + Node 12 + gtk/webkit2gtk-4.0).


class ImageBase73to25(TauriIntervalBase):
    TAG = "base-73_to_25"

    def extra_env(self) -> str:
        return """    GOPATH=/root/go \\
    GOFLAGS= \\
    PATH=/usr/local/go/bin:/usr/local/node/bin:/root/go/bin:${PATH}"""

    def toolchain(self) -> str:
        return f"""RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl xz-utils build-essential pkg-config python3 \\
        libvips-dev libgtk-3-dev libwebkit2gtk-4.0-dev xvfb xauth \\
    && rm -rf /var/lib/apt/lists/*
{tauri_node_install(NODE_LEGACY_VERSION)}
{TAURI_GO_INSTALL}"""


class ImageDefault73to25(TauriIntervalImageDefault):
    BASE_CLASS = ImageBase73to25
    DEFAULT_PLAN = TAURI_GO


@Instance.register("tauri-apps", "tauri_73_to_25")
class Tauri73to25(TauriIntervalInstance):
    IMAGE_DEFAULT = ImageDefault73to25


__all__ = ["ImageBase73to25", "ImageDefault73to25", "Tauri73to25"]
