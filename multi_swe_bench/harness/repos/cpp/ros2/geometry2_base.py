from __future__ import annotations

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.pull_request import PullRequest


_COMMON_ROS_PACKAGES = [
    "ament-cmake",
    "ament-cmake-auto",
    "ament-cmake-copyright",
    "ament-cmake-cppcheck",
    "ament-cmake-cpplint",
    "ament-cmake-gtest",
    "ament-cmake-google-benchmark",
    "ament-cmake-lint-cmake",
    "ament-cmake-pytest",
    "ament-cmake-python",
    "ament-cmake-ros",
    "ament-cmake-uncrustify",
    "ament-cmake-xmllint",
    "ament-copyright",
    "ament-flake8",
    "ament-pep257",
    "ament-xmllint",
    "action-msgs",
    "builtin-interfaces",
    "console-bridge-vendor",
    "geometry-msgs",
    "message-filters",
    "orocos-kdl-vendor",
    "python-cmake-module",
    "rclcpp",
    "rclcpp-action",
    "rclcpp-components",
    "rcl-interfaces",
    "rclpy",
    "rcpputils",
    "rcutils",
    "rmw-implementation-cmake",
    "rosidl-default-generators",
    "rosidl-default-runtime",
    "rosidl-runtime-cpp",
    "rpyutils",
    "sensor-msgs",
    "sensor-msgs-py",
    "std-msgs",
    "tf2-msgs",
]

_HUMBLE_ONLY_PACKAGES = [
    "python-orocos-kdl-vendor",
]

_SYSTEM_PACKAGES = [
    "graphviz",
    "libbullet-dev",
    "libeigen3-dev",
    "liborocos-kdl-dev",
    "python3-colcon-common-extensions",
    "python3-dev",
    "python3-numpy",
    "python3-pykdl",
    "python3-pytest",
    "python3-yaml",
]


def _ros_packages(distro: str, include_humble_only: bool = False) -> list[str]:
    pkgs = [f"ros-{distro}-{p}" for p in _COMMON_ROS_PACKAGES]
    if include_humble_only:
        pkgs += [f"ros-{distro}-{p}" for p in _HUMBLE_ONLY_PACKAGES]
    return pkgs


def _base_dockerfile(image: Image, base_image: str) -> str:
    org, repo = image.pr.org, image.pr.repo
    repo_url = f"https://github.com/{org}/{repo}.git"

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
    all_packages = default_packages + image.extra_packages()
    packages_str = " \\\n    ".join(all_packages)

    return (
        '# syntax=docker/dockerfile:1.6\n'
        '\n'
        f'FROM {base_image}\n'
        '\n'
        'ARG TARGETARCH\n'
        f'ARG REPO_URL="{repo_url}"\n'
        'ARG BASE_COMMIT\n'
        '\n'
        'ARG http_proxy=""\n'
        'ARG https_proxy=""\n'
        'ARG HTTP_PROXY=""\n'
        'ARG HTTPS_PROXY=""\n'
        'ARG no_proxy="localhost,127.0.0.1,::1"\n'
        'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
        'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"\n'
        '\n'
        'ENV DEBIAN_FRONTEND=noninteractive \\\n'
        '    LANG=C.UTF-8 \\\n'
        '    TZ=UTC \\\n'
        '    http_proxy=${http_proxy} \\\n'
        '    https_proxy=${https_proxy} \\\n'
        '    HTTP_PROXY=${HTTP_PROXY} \\\n'
        '    HTTPS_PROXY=${HTTPS_PROXY} \\\n'
        '    no_proxy=${no_proxy} \\\n'
        '    NO_PROXY=${NO_PROXY} \\\n'
        '    SSL_CERT_FILE=${CA_CERT_PATH} \\\n'
        '    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n'
        '    CURL_CA_BUNDLE=${CA_CERT_PATH}\n'
        '\n'
        f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
        f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
        f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
        f'      org.opencontainers.image.authors="https://www.ethara.ai/"\n'
        '\n'
        'RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n'
        '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n'
        '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n'
        '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n'
        '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n'
        '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n'
        '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt\n'
        '\n'
        'RUN --mount=type=secret,id=mitm_ca,required=0 \\\n'
        '    if [ -f /run/secrets/mitm_ca ]; then \\\n'
        '        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \\\n'
        '    fi\n'
        '\n'
        'WORKDIR /home/\n'
        '\n'
        f'RUN apt-get update && apt-get install -y --no-install-recommends \\\n'
        f'    {packages_str} \\\n'
        '    && rm -rf /var/lib/apt/lists/*\n'
        '\n'
        f'RUN git clone "${{REPO_URL}}" /home/{repo}\n'
        '\n'
        f'WORKDIR /home/{repo}\n'
        '\n'
        'RUN git reset --hard\n'
        'RUN git checkout ${BASE_COMMIT}\n'
        '\n'
        'CMD ["/bin/bash"]\n'
    )


class Geometry2ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "ros:humble-ros-base"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return _ros_packages("humble", include_humble_only=True) + _SYSTEM_PACKAGES

    def dockerfile(self) -> str:
        return _base_dockerfile(self, "ros:humble-ros-base")


class Geometry2IronImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "ros:jazzy-ros-base"

    def image_tag(self) -> str:
        return "jazzy-base"

    def workdir(self) -> str:
        return "jazzy-base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return _ros_packages("jazzy", include_humble_only=False) + _SYSTEM_PACKAGES

    def dockerfile(self) -> str:
        return _base_dockerfile(self, "ros:jazzy-ros-base")
