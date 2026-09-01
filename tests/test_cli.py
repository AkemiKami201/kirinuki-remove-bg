"""Static guards for bin/cli.js.

These read the launcher as text rather than running it: the failures they cover
are platform-specific (Windows batch shims, an editor's inherited environment)
and cannot be reproduced on the machine running the suite. Catching a
regression in the source is worth more here than not catching it at all.
"""

from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parent.parent / "bin" / "cli.js"


@pytest.fixture(scope="module")
def cli():
    return CLI.read_text(encoding="utf-8")


def body_of(src, marker, end="\n}"):
    start = src.index(marker)
    return src[start : src.index(end, start)]


def test_electron_is_launched_through_the_real_executable(cli):
    """`node_modules/.bin/electron.cmd` is a batch file, and Node has refused to
    spawn one without a shell since the CVE-2024-27980 fix: it fails with
    EINVAL. Turning the shell on would only trade that for a quoting problem,
    since the path runs through the user's home directory."""
    assert "electron.cmd" not in cli, (
        "spawning the .bin shim fails with EINVAL on Windows"
    )
    body = body_of(cli, "function electronExecutable(")
    assert "path.txt" in body, (
        "the electron package records its real binary name there"
    )
    assert "electron.exe" in body, "and a fallback is needed if that file is missing"


def test_the_electron_path_is_rechecked_after_installing(cli):
    """It is resolved before the install, when it is necessarily absent, so
    reusing that first result would report a failure for every fresh install."""
    body = body_of(cli, "function ensureElectron(")
    assert body.count("electronExecutable(desktopDir)") == 2, (
        "resolve again after npm install rather than trusting the pre-install miss"
    )


def test_the_desktop_window_does_not_inherit_run_as_node(cli):
    """A terminal inside VS Code (or Cursor) exports ELECTRON_RUN_AS_NODE=1.
    Inherited, it tells Electron to behave as a plain Node runtime, so
    `require("electron")` yields no app object and main.js dies on its first
    API call."""
    body = body_of(cli, "function cmdDesktop(", "\n}\n")
    assert "delete childEnv.ELECTRON_RUN_AS_NODE" in body
    assert "delete childEnv.ELECTRON_NO_ATTACH_CONSOLE" in body


def test_the_desktop_window_can_resolve_the_electron_module(cli):
    """main.js ships in the package directory but electron is installed into
    the state directory, so the usual upward search never reaches it."""
    body = body_of(cli, "function cmdDesktop(", "\n}\n")
    assert "NODE_PATH" in body, "the module directory must be passed to the child"


def test_batch_shims_still_run_through_a_shell(cli):
    """npm.cmd is spawned by name rather than by path, so it does need the
    shell that electron.cmd was avoiding."""
    body = body_of(cli, "function run(cmd, args, opts)")
    assert "shell: needsShell" in body
    assert r"/\.(cmd|bat)$/i" in body
