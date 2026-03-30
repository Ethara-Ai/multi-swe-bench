# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import logging
import platform as _platform
import shlex
import subprocess
from pathlib import Path
from typing import Optional, Union

import docker

docker_client = docker.from_env(timeout=3600)

BUILDX_BUILDER = "pr-eval-multiarch"


def exists(image_name: str) -> bool:
    try:
        docker_client.images.get(image_name)
        return True
    except docker.errors.ImageNotFound:
        return False


def build(
    workdir: Path,
    dockerfile_name: str,
    image_full_name: str,
    logger: logging.Logger,
    buildargs: dict[str, str] | None = None,
    platform: str | None = None,
    output_tar: Path | None = None,
    base_image_context: str | None = None,
):
    workdir = str(workdir)
    logger.info(
        f"Start building image `{image_full_name}`, working directory is `{workdir}`"
    )

    if platform:
        # --- Multi-arch path: use docker buildx ---
        # Resolve output_tar to absolute so buildx (which runs with
        # cwd=workdir) writes to the intended location, not relative
        # to the image workdir.
        abs_output_tar = output_tar.resolve() if output_tar else None
        _build_with_buildx(
            workdir,
            dockerfile_name,
            image_full_name,
            logger,
            buildargs=buildargs,
            platform=platform,
            output_tar=abs_output_tar,
            base_image_context=base_image_context,
            )
    else:
        # --- Legacy single-arch path: use Python Docker SDK (unchanged) ---
        _build_with_sdk(
            workdir,
            dockerfile_name,
            image_full_name,
            logger,
            buildargs=buildargs,
        )


def _build_with_sdk(
    workdir: str,
    dockerfile_name: str,
    image_full_name: str,
    logger: logging.Logger,
    buildargs: dict[str, str] | None = None,
):
    """Original build logic using the Docker Python SDK."""
    try:
        build_logs = docker_client.api.build(
            path=workdir,
            dockerfile=dockerfile_name,
            tag=image_full_name,
            rm=True,
            forcerm=True,
            decode=True,
            encoding="utf-8",
            buildargs=buildargs or {},
        )

        for log in build_logs:
            if "stream" in log:
                logger.info(log["stream"].strip())
            elif "error" in log:
                error_message = log["error"].strip()
                logger.error(f"Docker build error: {error_message}")
                raise RuntimeError(f"Docker build failed: {error_message}")
            elif "status" in log:
                logger.info(log["status"].strip())
            elif "aux" in log:
                logger.info(log["aux"].get("ID", "").strip())

        logger.info(f"image({workdir}) build success: {image_full_name}")
    except docker.errors.BuildError as e:
        logger.error(f"build error: {e}")
        raise e
    except Exception as e:
        logger.error(f"Unknown build error occurred: {e}")
        raise e


def _detect_native_platform() -> str:
    """Detect the native platform string for the host machine.

    Returns 'linux/arm64' on ARM-based hosts (e.g. Apple Silicon),
    'linux/amd64' otherwise.
    """
    machine = _platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    return "linux/amd64"


def _run_buildx(
    cmd: list[str],
    workdir: str,
    logger: logging.Logger,
    label: str = "",
):
    """Execute a docker buildx command, stream output, raise on failure."""
    cmd_str = shlex.join(cmd)
    logger.info(f"Running buildx{f' ({label})' if label else ''}: {cmd_str}")

    try:
        process = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        for line in process.stdout:
            logger.info(line.rstrip())

        returncode = process.wait()
        if returncode != 0:
            raise RuntimeError(
                f"docker buildx build failed with exit code {returncode}"
            )
    except FileNotFoundError:
        raise RuntimeError(
            "docker buildx not found. Install with: docker buildx install"
        )
    except Exception as e:
        logger.error(f"buildx error: {e}")
        raise e


def _build_with_buildx(
    workdir: str,
    dockerfile_name: str,
    image_full_name: str,
    logger: logging.Logger,
    buildargs: dict[str, str] | None = None,
    platform: str = "linux/amd64",
    output_tar: Path | None = None,
    base_image_context: str | None = None,
):
    """Multi-arch build using docker buildx subprocess.

    When output_tar is provided, the image is exported as an OCI archive AND
    also loaded into the local Docker daemon:
      - Single platform: both --output and --load in one buildx invocation.
      - Multi platform: first invocation exports the OCI tar, second invocation
        loads just the native platform from the buildx cache into the daemon.
    """
    platforms = [p.strip() for p in platform.split(",")]
    is_multi_platform = len(platforms) > 1

    cmd = [
        "docker",
        "buildx",
        "build",
        "--platform",
        platform,
        "-f",
        dockerfile_name,
        "-t",
        image_full_name,
        "--provenance=false",
        "--sbom=false",
    ]

    cmd.extend(["--builder", BUILDX_BUILDER])

    # Add build arguments
    for key, value in (buildargs or {}).items():
        cmd.extend(["--build-arg", f"{key}={value}"])

    # Add base image context for resolving locally-built base images via OCI layout
    if base_image_context:
        cmd.extend(["--build-context", base_image_context])

    # Output strategy:
    #   - output_tar + single platform: OCI tar AND --load in one command
    #   - output_tar + multi platform:  OCI tar only (--load in second pass below)
    #   - no output_tar:                --load only
    if output_tar:
        cmd.extend(["--output", f"type=oci,dest={output_tar}"])
        if not is_multi_platform:
            cmd.append("--load")
    else:
        cmd.append("--load")

    cmd.append(".")  # build context

    _run_buildx(cmd, workdir, logger)
    logger.info(f"image({workdir}) buildx success: {image_full_name}")

    # Extract OCI tar to a directory so downstream builds can reference it
    # via --build-context with oci-layout:// protocol.
    if output_tar:
        oci_dir = Path(str(output_tar) + ".d")
        oci_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["tar", "-xf", str(output_tar), "-C", str(oci_dir)],
            check=True,
        )
        logger.info(f"Extracted OCI tar to {oci_dir}")

    # --- Second pass for multi-platform + output_tar ---
    # The first build exported a multi-arch OCI tar but could NOT use --load
    # (incompatible with multi-platform).  Re-run buildx targeting only the
    # native platform with --load.  All layers are already in the buildx
    # cache from the first build, so this completes near-instantly.
    if output_tar and is_multi_platform:
        native = _detect_native_platform()
        logger.info(
            f"Loading native platform ({native}) into daemon from buildx cache..."
        )

        load_cmd = [
            "docker",
            "buildx",
            "build",
            "--platform",
            native,
            "-f",
            dockerfile_name,
            "-t",
            image_full_name,
            "--provenance=false",
            "--sbom=false",
        ]
        load_cmd.extend(["--builder", BUILDX_BUILDER])
        for key, value in (buildargs or {}).items():
            load_cmd.extend(["--build-arg", f"{key}={value}"])
        if base_image_context:
            load_cmd.extend(["--build-context", base_image_context])
        load_cmd.append("--load")
        load_cmd.append(".")

        import time
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                _run_buildx(load_cmd, workdir, logger, label="load native")
                logger.info(f"Native platform image loaded into daemon: {image_full_name}")
                break
            except RuntimeError as e:
                logger.warning(f"Load attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    daemon_check = subprocess.run(
                        ["docker", "info"],
                        capture_output=True,
                    )
                    if daemon_check.returncode != 0:
                        logger.info("Docker daemon is down. Restarting...")
                        subprocess.run(["systemctl", "restart", "docker"], check=True)
                        time.sleep(5)
                    else:
                        logger.info("Docker daemon is alive. Retrying after short sleep...")
                        time.sleep(3)
                else:
                    logger.error("All load attempts failed.")
                    raise


def run(
    image_full_name: str,
    run_command: str,
    output_path: Optional[Path] = None,
    global_env: Optional[list[str]] = None,
    volumes: Optional[Union[dict[str, str], list[str]]] = None,
) -> str:
    container = None
    try:
        container = docker_client.containers.run(
            image=image_full_name,
            command=run_command,
            remove=False,
            detach=True,
            stdout=True,
            stderr=True,
            environment=global_env,
            volumes=volumes,
        )

        output = ""
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                for line in container.logs(stream=True, follow=True):
                    line_decoded = line.decode("utf-8")
                    f.write(line_decoded)
                    output += line_decoded
        else:
            container.wait()
            output = container.logs().decode("utf-8")

        return output
    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception as e:
                print(f"Warning: Failed to remove container: {e}")
