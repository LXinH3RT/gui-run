#!/usr/bin/env python3
"""gui-run — run a command in the macOS GUI (Aqua) session without opening Terminal windows.

Why this exists
---------------
An SSH session on macOS is a non-interactive session outside the GUI (Aqua) audit
session, so it cannot use private keys in the login keychain that are protected by
an ACL. ``codesign`` then fails with ``errSecInternalComponent`` while still
exiting 0.

The usual workaround hands the command to Terminal.app::

    osascript -e 'tell application "Terminal" to do script "..."'

Every call opens a new window or tab. This script instead submits the command as a
launchd job via ``launchctl bootstrap gui/<uid>``. The job gets the same GUI
session context and ``Terminal.app`` is never touched.

Usage
-----
::

    gui-run.py '<command>'

Exit status
-----------
The command's own exit code, or one of:

======  ==================================================================
  124   timed out (``GUIRUN_TIMEOUT``, default 300 seconds)
   64   bad usage
   70   could not create the temporary working directory
   71   ``launchctl bootstrap`` failed
======  ==================================================================

Pairs with the zsh implementation of the same name; both accept the same
arguments and honour the same environment variable.
"""

from __future__ import annotations

import os
import plistlib
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EX_USAGE = 64
EX_CANTCREAT = 70
EX_UNAVAILABLE = 71
EX_TEMPFAIL = 124

DEFAULT_TIMEOUT = 300.0
POLL_INTERVAL = 0.1

LAUNCHCTL = "/bin/launchctl"
SHELL = "/bin/zsh"

PROG = "gui-run.py"

USAGE = f"""\
usage: {PROG} <command ...>

Run a command in the macOS GUI (Aqua) session without opening Terminal windows.

The arguments are joined into a single string and handed to {SHELL}, so several
steps can be chained with ';' or '&&'.

environment:
  GUIRUN_TIMEOUT   seconds to wait before giving up (default {DEFAULT_TIMEOUT:g})

exit status:
  the command's own exit code
  124  timed out
   64  bad usage
   70  could not create the temporary working directory
   71  'launchctl bootstrap' failed
"""


def gui_domain() -> str:
    """launchd domain for the current user's logged-in GUI session."""
    return f"gui/{os.getuid()}"


def read_timeout() -> float:
    """Timeout in seconds from ``GUIRUN_TIMEOUT``, defaulting to 300."""
    raw = os.environ.get("GUIRUN_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"GUIRUN_TIMEOUT is not a number: {raw!r}") from None
    if value <= 0:
        raise ValueError(f"GUIRUN_TIMEOUT must be greater than zero: {raw!r}")
    return value


def build_wrapper(command: str, startdir: str) -> str:
    """Assemble the script launchd runs: restore a login environment, then the command.

    The command sits inside a subshell so that an ``exit`` in it cannot skip the
    bookkeeping that follows. The exit code and a ``done`` marker go to files,
    because a launchd job is a separate process whose status the caller cannot
    observe directly.
    """
    return "\n".join(
        [
            f"#!{SHELL}",
            "# launchd does not read ~/.zprofile, so Homebrew has to be loaded by hand.",
            "# Both prefixes are probed to cover Apple Silicon and Intel installs.",
            "for _brew in /opt/homebrew/bin/brew /usr/local/bin/brew; do",
            '  [[ -x $_brew ]] && eval "$($_brew shellenv)" && break',
            "done",
            'export PATH="$HOME/bin:$HOME/development/flutter/bin:$HOME/.local/bin:$PATH"',
            f"cd {shlex.quote(startdir)} 2>/dev/null",
            "(",
            command,
            ") 2>&1",
            'print -r -- "$?" > "$1"',
            'print -r -- done > "$2"',
            "",
        ]
    )


def build_plist(
    label: str, run: Path, rc_file: Path, done_file: Path, log_file: Path
) -> bytes:
    """Serialise the launchd job definition.

    ``plistlib`` escapes values properly, so a path containing ``&`` or ``<`` still
    produces a valid plist.
    """
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [SHELL, str(run), str(rc_file), str(done_file)],
            "RunAtLoad": True,
            "StandardOutPath": str(log_file),
            "StandardErrorPath": str(log_file),
        }
    )


def wait_for(path: Path, timeout: float) -> bool:
    """Poll until ``path`` exists, returning False once ``timeout`` expires."""
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def emit_log(path: Path) -> None:
    """Copy the job's output to stdout unchanged."""
    try:
        data = path.read_bytes()
    except OSError:
        return
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def read_rc(path: Path) -> int:
    """Read the exit code the command wrote, falling back to 0."""
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return value if 0 <= value <= 255 else 0


def bootout(label: str) -> None:
    """Remove the job, ignoring the failure that means it is already gone."""
    subprocess.run(
        [LAUNCHCTL, "bootout", f"{gui_domain()}/{label}"],
        capture_output=True,
    )


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    if not argv:
        sys.stderr.write(USAGE)
        return EX_USAGE

    command = " ".join(argv)

    try:
        timeout = read_timeout()
    except ValueError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EX_USAGE

    startdir = os.getcwd()
    tmproot = os.environ.get("TMPDIR") or "/tmp"
    try:
        work = Path(tempfile.mkdtemp(prefix="guirun.", dir=tmproot))
    except OSError as exc:
        print(f"{PROG}: cannot create a working directory: {exc}", file=sys.stderr)
        return EX_CANTCREAT

    label = f"local.guirun.{os.getpid()}.{random.randint(0, 32767)}"
    run = work / "run.sh"
    log = work / "out.log"
    done = work / "done"
    plist = work / "job.plist"
    rc_file = work / "rc"

    try:
        run.write_text(build_wrapper(command, startdir), encoding="utf-8")
        run.chmod(0o755)
        plist.write_bytes(build_plist(label, run, rc_file, done, log))

        bootstrap = subprocess.run(
            [LAUNCHCTL, "bootstrap", gui_domain(), str(plist)],
            capture_output=True,
            text=True,
        )
        if bootstrap.returncode != 0:
            detail = (bootstrap.stderr or bootstrap.stdout).strip()
            print(
                f"{PROG}: 'launchctl bootstrap' failed: {detail or bootstrap.returncode}",
                file=sys.stderr,
            )
            bootout(label)
            return EX_UNAVAILABLE

        finished = wait_for(done, timeout)
        bootout(label)
        emit_log(log)

        if not finished:
            print(
                f"{PROG}: still running after {timeout:g}s "
                "(the command may have been backgrounded, or is waiting for a "
                "GUI authorisation prompt)",
                file=sys.stderr,
            )
            return EX_TEMPFAIL

        return read_rc(rc_file)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
