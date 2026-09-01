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


def test_the_electron_path_is_rechecked_after_each_fetch_step(cli):
    """It is resolved before anything is fetched, when it is necessarily
    absent, so reusing that first miss would report a failure for every fresh
    install. Both fetch steps -- the npm package and the binary -- have to be
    followed by a fresh look."""
    body = body_of(cli, "function ensureElectron(", "\n}\n")
    assert body.count("electronExecutable(desktopDir)") >= 3, (
        "resolve again after the install and after the binary download"
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


def test_a_missing_binary_is_downloaded_rather_than_reinstalled(cli):
    """The npm package and its ~100 MB binary are fetched in two separate
    steps, and only the first is `npm install`. An interrupted download leaves
    the package installed but empty, after which npm reports "up to date"
    forever and re-running the install cannot fix it. The package ships its own
    downloader for this case."""
    body = body_of(cli, "function fetchElectronBinary(")
    assert "install.js" in body, "the package's own downloader is what retries the binary"

    setup = body_of(cli, "function ensureElectron(", "\n}\n")
    assert "fetchElectronBinary(" in setup, (
        "a present-but-empty package must trigger the download, not another install"
    )


def test_the_install_is_skipped_when_the_package_is_already_there(cli):
    """Re-running it wastes time on a step npm will decline anyway, and the
    'Installing ... the first time' line is untrue on a second run."""
    setup = body_of(cli, "function ensureElectron(", "\n}\n")
    assert "fs.existsSync(pkgDir)" in setup


def test_the_failure_message_names_the_real_cause(cli):
    """Reporting 'could not install' when npm has just said 'up to date'
    describes the wrong problem and suggests a fix that cannot work."""
    setup = body_of(cli, "function ensureElectron(", "\n}\n")
    assert "two steps" in setup, "the message must explain the split install"
    assert "rmdir /s /q" in setup and "rm -rf" in setup, (
        "and give the removal command that actually clears the state"
    )


def test_npm_runs_without_a_shell(cli):
    """Spawning npm.cmd needs shell:true on Windows, and passing arguments with
    the shell on raises DEP0190 on every launch: a shell concatenates arguments
    rather than escaping them. npm's entry point is a plain Node script, so
    running it through the current node binary needs no shell at all."""
    body = body_of(cli, "function npm(args, opts)")
    assert "process.execPath" in body, "npm-cli.js is run by node directly"

    resolver = body_of(cli, "function npmCli(")
    assert "npm-cli.js" in resolver
    assert "IS_WIN" in resolver, "the layout differs between Windows and the rest"


def test_npm_still_works_when_its_entry_point_is_missing(cli):
    """Distributions repackage npm and may put it somewhere unexpected, in
    which case the shim on PATH is still the right answer."""
    body = body_of(cli, "function npm(args, opts)")
    assert 'run(IS_WIN ? "npm.cmd" : "npm", args, opts)' in body, (
        "fall back rather than failing when npm-cli.js is not found"
    )


def test_no_npm_call_passes_arguments_through_a_shell(cli):
    """This is what DEP0190 warns about. Every npm invocation must go through
    npm(), leaving exactly one mention of the shim: the fallback inside npm()
    itself, for when npm-cli.js cannot be found."""
    assert cli.count('"npm.cmd"') == 1, (
        "npm calls go through npm(); only its own fallback may name the shim"
    )
    fallback = body_of(cli, "function npm(args, opts)")
    assert '"npm.cmd"' in fallback, "and that one mention is the fallback"


def test_shortcut_failure_is_not_reported_as_success(cli):
    """WScript.Shell errors are non-terminating: without ErrorActionPreference
    the COM call prints its error, PowerShell still exits 0, and the installer
    reported a shortcut it never wrote. The exit code alone is not proof, so
    the file is checked afterwards too."""
    body = body_of(cli, "function installWindows(")
    assert "$ErrorActionPreference = 'Stop';" in body, (
        "a COM failure otherwise exits 0"
    )
    assert "exit 1" in body, "and the catch has to turn it into a failing status"
    assert "fs.existsSync(lnk)" in body, (
        "verify the shortcut exists rather than trusting the exit code"
    )


def test_shortcut_reports_which_failure_happened(cli):
    """Three failures need three different fixes: PowerShell missing, the
    script failing, and a success that wrote nothing. One generic message sends
    the user looking in the wrong place."""
    body = body_of(cli, "function installWindows(")
    assert "r.error" in body, "spawnSync sets .error (status stays null) when the shell will not start"
    assert "r.status !== 0" in body, "a non-zero exit is a different failure"


def test_shortcut_falls_back_to_pwsh(cli):
    """powershell.exe is on every normal Windows install, but PowerShell 7-only
    machines have just pwsh. Retry only when the shell fails to start: a script
    that ran and failed must keep its error rather than be run twice."""
    body = body_of(cli, "function installWindows(")
    assert '"powershell", "pwsh"' in body, "try the Windows shell first, then PowerShell 7"
    assert "if (!r.error) break;" in body, (
        "break once a shell starts, so a real script failure is not retried"
    )


def test_installers_warn_about_a_missing_icon(cli):
    """Both platforms accept a path that does not exist and quietly show a
    blank or generic icon - which is how the logo-dark.png typo survived."""
    for func in ("function installLinux(", "function installWindows("):
        body = body_of(cli, func)
        assert "Icon file missing" in body, f"{func} should say so instead of installing a broken icon"
