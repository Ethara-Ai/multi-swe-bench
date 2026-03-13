"""Stub flow-bin on ARM64 at container runtime (no image rebuild needed)."""


def wrap_with_flow_stub(script_path: str) -> str:
    # flow-bin 0.135.0 has no ARM64 binary → "Platform not supported" crash.
    # Overwrites cli.js with process.exit(0) before running the test script.
    # Covers yarn PnP unplugged + classic node_modules. No-op if absent.
    # Outer single-quotes ensure shlex.split → ["bash", "-c", "<body>"].
    stub = (
        'for f in $(find /home -name cli.js -path "*/flow-bin/*" 2>/dev/null); '
        'do printf "process.exit(0);\\n" > "$f" 2>/dev/null; done'
    )
    return f"bash -c '{stub}; bash {script_path}'"
