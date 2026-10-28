#!/usr/bin/env python3
"""Run one docker/web/compose.yml service's command directly on this host.

CI uses this so the Compose file stays the single definition of every web
check: the command, working directory, and environment come from
`docker compose config`, not from a copy in the workflow. The host must expose
the repository at /workspace (a symlink is enough) because the services use
that path. Container-only settings such as `user`, `read_only`, and
`network_mode` are not applied.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys

COMPOSE_FILE = pathlib.Path(__file__).resolve().parent.parent / "docker" / "web" / "compose.yml"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("service")
    args = parser.parse_args()

    config = json.loads(
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    service = config["services"].get(args.service)
    if service is None or not service.get("command"):
        parser.error("compose service {!r} has no command".format(args.service))

    # `config` keeps Compose's `$$` escapes; a container would see a single `$`.
    command = [argument.replace("$$", "$") for argument in service["command"]]
    environment = dict(os.environ)
    environment.update({key: value for key, value in (service.get("environment") or {}).items() if value is not None})
    os.chdir(service.get("working_dir", "/workspace"))
    sys.stdout.flush()
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
