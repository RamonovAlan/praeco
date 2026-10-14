"""Prepare a Railway volume as root, then run Praeco as its numeric user."""

import os
import sys
from importlib.util import find_spec
from pathlib import Path

_APPLICATION_UID = 10_001
_APPLICATION_GID = 10_001
_STATE_ROOT = Path("/var/lib/praeco")


class ContainerEntrypointError(RuntimeError):
    """Raised when the container cannot enforce its runtime user boundary."""


def prepare_state_tree(
    root: Path,
    *,
    uid: int,
    gid: int,
) -> None:
    """Own a fixed state tree without following or accepting symbolic links."""

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [root]
    paths.extend(sorted(root.rglob("*")))
    for path in paths:
        if path.is_symlink():
            raise ContainerEntrypointError("state volume must not contain symbolic links")
        os.chown(path, uid, gid, follow_symlinks=False)


def drop_privileges(*, uid: int, gid: int) -> None:
    """Drop supplementary groups, group ID, and user ID permanently."""

    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.geteuid() != uid or os.getegid() != gid:
        raise ContainerEntrypointError("failed to drop container privileges")


def main() -> int:
    """Handle Railway's root-mounted volume and exec the normal application."""

    if os.geteuid() == 0:
        if os.environ.get("RAILWAY_RUN_UID") != "0":
            raise ContainerEntrypointError("unexpected root container execution")
        prepare_state_tree(
            _STATE_ROOT,
            uid=_APPLICATION_UID,
            gid=_APPLICATION_GID,
        )
        drop_privileges(uid=_APPLICATION_UID, gid=_APPLICATION_GID)
    application_module = (
        "praeco.private_ops" if find_spec("praeco.private_ops") is not None else "praeco"
    )
    os.execv(
        sys.executable,
        [sys.executable, "-m", application_module, *sys.argv[1:]],
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
