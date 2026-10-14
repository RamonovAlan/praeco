import os
from pathlib import Path

import pytest

from praeco.container_entrypoint import (
    ContainerEntrypointError,
    prepare_state_tree,
)


def test_prepare_state_tree_owns_regular_files_without_following_links(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    media = state / "media"
    media.mkdir(parents=True)
    original = media / "image.png"
    original.write_bytes(b"synthetic")

    prepare_state_tree(state, uid=os.getuid(), gid=os.getgid())

    assert original.read_bytes() == b"synthetic"


def test_prepare_state_tree_rejects_symbolic_links(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "unsafe").symlink_to(tmp_path / "outside")

    with pytest.raises(ContainerEntrypointError, match="symbolic links"):
        prepare_state_tree(state, uid=os.getuid(), gid=os.getgid())
