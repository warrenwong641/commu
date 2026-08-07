"""Hermetic tests for the runner's narrow sudo ownership repair."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import traffic_experiment.traffic_measure.runner as runner
from traffic_experiment.traffic_measure.runner import _ensure_capture_writable


def test_chowns_only_output_and_recorded_created_ancestors(tmp_path):
    parent = tmp_path / "created"
    inner = parent / "output"
    inner.mkdir(parents=True)
    effective_uid = inner.stat().st_uid
    target_uid = effective_uid + 10000
    with (
        mock.patch.dict(
            os.environ,
            {"SUDO_UID": str(target_uid), "SUDO_GID": "5678"},
        ),
        mock.patch.object(runner.os, "name", "posix"),
        mock.patch.object(
            runner.os,
            "geteuid",
            return_value=effective_uid,
            create=True,
        ),
        mock.patch.object(runner.os, "chown", create=True) as chown,
    ):
        _ensure_capture_writable(inner, [inner, parent])

    assert chown.call_args_list == [
        mock.call(inner.absolute(), target_uid, 5678),
        mock.call(parent.absolute(), target_uid, 5678),
    ]
    assert all(call.args[0] != tmp_path.absolute() for call in chown.call_args_list)


def test_unanchored_preexisting_output_is_never_chowned(tmp_path):
    inner = tmp_path / "output"
    inner.mkdir()
    effective_uid = inner.stat().st_uid
    target_uid = effective_uid + 10000
    with (
        mock.patch.dict(
            os.environ,
            {"SUDO_UID": str(target_uid), "SUDO_GID": "5678"},
        ),
        mock.patch.object(runner.os, "name", "posix"),
        mock.patch.object(
            runner.os,
            "geteuid",
            return_value=effective_uid,
            create=True,
        ),
        mock.patch.object(runner.os, "chown", create=True) as chown,
    ):
        _ensure_capture_writable(inner)

    chown.assert_not_called()


def test_preexisting_output_under_user_owned_parent_can_be_chowned(tmp_path):
    inner = tmp_path / "output"
    inner.mkdir()
    inner_resolved = inner.resolve()
    parent_resolved = tmp_path.resolve()
    real_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        if path == inner_resolved:
            return SimpleNamespace(st_uid=0)
        if path == parent_resolved:
            return SimpleNamespace(st_uid=1234)
        return real_stat(path, *args, **kwargs)

    with (
        mock.patch.dict(os.environ, {"SUDO_UID": "1234", "SUDO_GID": "5678"}),
        mock.patch.object(runner.os, "name", "posix"),
        mock.patch.object(runner.os, "geteuid", return_value=0, create=True),
        mock.patch.object(Path, "stat", fake_stat),
        mock.patch.object(runner.os, "chown", create=True) as chown,
    ):
        _ensure_capture_writable(inner)

    chown.assert_called_once_with(inner_resolved, 1234, 5678)


def test_filesystem_root_can_never_be_chowned():
    root = Path(Path.cwd().anchor)
    with (
        mock.patch.dict(os.environ, {"SUDO_UID": "1234", "SUDO_GID": "5678"}),
        mock.patch.object(runner.os, "name", "posix"),
        mock.patch.object(runner.os, "geteuid", return_value=0, create=True),
        mock.patch.object(runner.os, "chown", create=True) as chown,
    ):
        _ensure_capture_writable(root)

    chown.assert_not_called()


def test_noop_without_sudo_identity(tmp_path):
    inner = tmp_path / "output"
    inner.mkdir()
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(runner.os, "name", "posix"),
        mock.patch.object(runner.os, "chown", create=True) as chown,
    ):
        _ensure_capture_writable(inner, [inner])

    chown.assert_not_called()


def test_noop_on_non_posix(tmp_path):
    inner = tmp_path / "output"
    inner.mkdir()
    with (
        mock.patch.dict(os.environ, {"SUDO_UID": "1234", "SUDO_GID": "5678"}),
        mock.patch.object(runner.os, "name", "nt"),
        mock.patch.object(runner.os, "chown", create=True) as chown,
    ):
        _ensure_capture_writable(inner, [inner])

    chown.assert_not_called()
