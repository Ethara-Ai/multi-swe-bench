"""
This script is used to parse the output of the `env` command and generate a Dockerfile.

Usage:
    python -m multi_swe_bench.utils.env_to_dockerfile
"""

import re
from typing import List, Tuple

# A POSIX environment-variable name. Generated ENV instructions interpolate the
# name verbatim, so only validated identifiers are emitted — a crafted name
# (e.g. one containing a newline) can never break out of the ENV line and inject
# a RUN/FROM directive. Every real env var name is already a valid identifier,
# so nothing legitimate is dropped.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_valid_env_name(name: str) -> bool:
    return bool(name and _ENV_NAME_RE.match(name))


def escape_env_value(value: str) -> str:
    """Make ``value`` safe inside a double-quoted ENV instruction.

    Escapes backslashes and quotes and neutralises CR/LF so a crafted value
    cannot terminate the quoted string and inject further Dockerfile directives.
    Values without these characters are returned unchanged (no behaviour change).
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def parse_env_output(env_output: str) -> List[Tuple[str, str]]:
    """
    Parse env command output, extract env var name and value
    Support multi-line env vars (continuation lines, quoted values, etc.)
    """
    env_vars = []
    lines = env_output.strip().split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Check if it's an env var definition (contains '=')
        if "=" in line:
            # Split var name and value
            var_name, var_value = line.split("=", 1)
            # Filter out empty env vars
            if not var_name or not var_name.strip():
                i += 1
                continue

            # Check if value starts with quotes
            if var_value.startswith('"') or var_value.startswith("'"):
                quote_char = var_value[0]
                # Find matching closing quote
                full_value = var_value
                i += 1

                while i < len(lines):
                    next_line = lines[i].strip()
                    full_value += "\n" + next_line

                    # Check if closing quote is found
                    if next_line.endswith(quote_char):
                        # Remove start and end quotes
                        full_value = full_value[1:-1]
                        break
                    i += 1

                env_vars.append((var_name, full_value))
            else:
                # Check if there's a continuation line
                if line.endswith("\\"):
                    # Collect continuation lines
                    full_value = var_value.rstrip("\\")
                    i += 1

                    while i < len(lines):
                        next_line = lines[i].strip()
                        if not next_line.endswith("\\"):
                            full_value += next_line
                            break
                        else:
                            full_value += next_line.rstrip("\\")
                        i += 1

                    env_vars.append((var_name, full_value))
                else:
                    # Single line env var
                    env_vars.append((var_name, var_value))
        i += 1

    return env_vars


def generate_dockerfile(
    env_vars: List[Tuple[str, str]], base_image: str = "ubuntu:24.04"
) -> str:
    """
    Generate Dockerfile content
    """
    dockerfile_lines = [
        f"FROM {base_image}",
        "",
    ]

    for var_name, var_value in env_vars:
        # Only emit validated identifiers; escape the value so it cannot break out.
        if is_valid_env_name(var_name):
            dockerfile_lines.append(f'ENV {var_name}="{escape_env_value(var_value)}"')

    return "\n".join(dockerfile_lines)


def generate_dockerfile_from_env_vars(
    delete_env_vars: List[Tuple[str, str]],
    add_and_change_env_vars: List[Tuple[str, str]],
    base_image: str = "ubuntu:24.04",
) -> str:
    dockerfile_lines = [
        f"FROM {base_image}",
        "",
    ]

    # Delete env vars
    for var_name, _ in delete_env_vars:
        if is_valid_env_name(var_name):
            dockerfile_lines.append(f'ENV {var_name}=""')

    if delete_env_vars:
        dockerfile_lines.append("")

    # Add and change env vars
    for var_name, var_value in add_and_change_env_vars:
        if is_valid_env_name(var_name):
            dockerfile_lines.append(f'ENV {var_name}="{escape_env_value(var_value)}"')

    return "\n".join(dockerfile_lines)


def diff_env_vars(pre_env_output: str, post_env_output: str, image_name: str):
    # Parse env vars
    pre_env_vars = parse_env_output(pre_env_output)
    post_env_vars = parse_env_output(post_env_output)

    # Get delete and add/change env vars
    delete_env_vars = []
    add_and_change_env_vars = []

    # Convert post_env_vars to dict for lookup
    post_env_dict = dict(post_env_vars)

    for var_name, var_value in pre_env_vars:
        if var_name not in post_env_dict:
            delete_env_vars.append((var_name, var_value))
        elif var_value != post_env_dict[var_name]:
            add_and_change_env_vars.append((var_name, post_env_dict[var_name]))

    for var_name, var_value in post_env_vars:
        if var_name not in [name for name, _ in pre_env_vars]:
            add_and_change_env_vars.append((var_name, var_value))

    return generate_dockerfile_from_env_vars(
        delete_env_vars, add_and_change_env_vars, image_name
    )


def validate_dockerfile(df_content: str) -> bool:
    """
    Validate Dockerfile content using dockerfile library
    """
    try:
        import dockerfile

        result = dockerfile.parse_string(df_content)
        has_from = any(cmd.cmd.lower() == "from" for cmd in result)
        if not has_from:
            raise ValueError("Dockerfile must contain at least one FROM instruction")
        return True
    except ImportError:
        raise RuntimeError(
            "dockerfile library is not installed. Please install it with: pip install dockerfile"
        )
    except dockerfile.GoParseError as e:
        raise RuntimeError(f"Dockerfile syntax error: {e}")
    except Exception as e:
        raise RuntimeError(f"Invalid Dockerfile: {e}")


if __name__ == "__main__":
    pre_env_output = """HOSTNAME=494e602cc801
PWD=/home/bytes
HOME=/root
LS_COLORS=
LESSCLOSE=/usr/bin/lesspipe %s %s
LESSOPEN=| /usr/bin/lesspipe %s
SHLVL=1
LC_CTYPE=C.UTF-8
PS2=
PS0=
PS1=
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
DEBIAN_FRONTEND=noninteractive
_=/usr/bin/env"""

    post_env_output = """HOSTNAME=494e602cc801
PWD=/home/bytes
HOME=/root
LS_COLORS=
LESSCLOSE=/usr/bin/lesspipe %s %s
LESSOPEN=| /usr/bin/lesspipe %s
SHLVL=1
LC_CTYPE=C.UTF-8
PS2=
PS0=
PS1=
PATH=/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
DEBIAN_FRONTEND=noninteractive
_=/usr/bin/env"""
    df_content = diff_env_vars(
        pre_env_output, post_env_output, "ubuntu:latest\nexport xxx"
    )
    print(df_content)
    is_valid = validate_dockerfile(df_content)
    print(is_valid)
