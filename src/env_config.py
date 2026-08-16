"""
Worker configuration, read from a mounted settings file rather than the process
environment.

`scripts/run_container.sh` bind-mounts the host's .env to /app/.env instead of
passing values with `docker run -e`. Two things follow from that:

  * The API key no longer appears in `docker inspect` on the host, nor in the
    container's own environment.
  * Build scripts of the repository under test -- configure, make, setup.py,
    all of which the agent runs as subprocesses -- no longer inherit the key.
    Under `-e` every one of them could read it out of their environment.

The process environment is still consulted as a fallback: the Kubernetes path
(orchestrator.py + job-template.yaml) has no file to mount and supplies values
as pod env vars, and run_container.sh still passes the non-secret run knobs
(RETRIEVAL, MAX_TURNS, JOB_INDEX, ...) that way.

Lookup order for every key: mounted file, then environment, then the default.

Import this module and call get()/get_int()/get_bool() instead of reading
os.environ, so that import order stops mattering -- the file is parsed once,
when this module is first imported, whoever gets there first.
"""

import os

from dotenv import dotenv_values


def _resolve_env_file():
    """
    Locate the settings file: an explicit override, the container mount point,
    then the project root for runs outside a container. Returns None if there is
    no file anywhere, in which case every lookup falls through to the
    environment and behaviour matches the pre-mount setup exactly.
    """
    explicit = os.environ.get("BUILDBENCH_ENV_FILE")
    if explicit:
        return explicit if os.path.isfile(explicit) else None

    project_root_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    for candidate in ("/app/.env", project_root_env):
        if os.path.isfile(candidate):
            return candidate
    return None


ENV_FILE = _resolve_env_file()

# dotenv_values parses into a dict and leaves os.environ alone, which is the
# whole point -- nothing here leaks into the environment of child processes.
# Keys written without a value parse to None; drop them so they fall through to
# the environment rather than shadowing it with nothing.
_FILE_VALUES = {}
if ENV_FILE:
    _FILE_VALUES = {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None and v != ""}


def get(key, default=None):
    """Value for `key` from the settings file, else the environment, else `default`."""
    value = _FILE_VALUES.get(key)
    if value is None:
        value = os.environ.get(key)
    # An empty value means "not configured", matching the `-z` check the shell
    # script makes on API_KEY.
    if value is None or value == "":
        return default
    return value


def get_int(key, default):
    """Like get(), coerced to int. Falls back to `default` if the value is not a number."""
    value = get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"[env_config] {key}={value!r} is not an integer; using {default}")
        return default


def get_bool(key, default=False):
    """
    Like get(), coerced to bool. Accepts true/false in any case, so that a
    lowercase `retrieval=true` in .env does not silently read as False the way a
    bare `== 'True'` comparison would.
    """
    value = get(key)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


def describe_source():
    """One line for worker logs: where configuration came from, without printing secrets."""
    if not ENV_FILE:
        return "config: environment only (no settings file found)"
    return f"config: {ENV_FILE} ({len(_FILE_VALUES)} keys) with environment fallback"
