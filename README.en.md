# gui-run — Run macOS GUI-session commands without opening Terminal windows

**Language:** [中文](README.md) | English

This repository contains a small tool and its documentation, addressing the problem of a new
terminal window opening every time a command that requires GUI session privileges is executed
on macOS over SSH.

## 1. Background

An SSH session on macOS is a non-interactive session. It is not part of the GUI (Aqua) audit
session and therefore cannot access private keys in the login keychain that are protected by an
ACL. The typical symptom is a `codesign` failure:

```
$ ssh <host> 'codesign --force --sign <identity> a.txt'
a.txt: errSecInternalComponent
$ echo $?
0
```

Note that the exit code is still 0. Only `codesign -dvv` can confirm whether signing actually
happened, by showing `Authority=` and `Signature size=`.

A common workaround is to hand the command to `Terminal.app` so that it runs inside the GUI
session:

```bash
osascript -e 'tell application "Terminal" to do script "/tmp/build.sh"'
```

Each call to `do script` creates a new terminal window or tab. During batch remote debugging,
windows pile up quickly.

`gui-run` keeps the ability to enter the GUI session while never touching `Terminal.app`.

## 2. How it works

`launchctl bootstrap gui/<uid>` submits a `launchd` job to the user's logged-in GUI domain.
The job process therefore runs with GUI session context and can use the ACL grants on the
relevant private keys in the login keychain.

The `gui-run` flow:

1. Create a temporary working directory under `$TMPDIR` and write a wrapper script and a
   `launchd` plist into it.
2. The wrapper script restores the login environment (Homebrew `shellenv`, Flutter and other
   toolchain paths), then runs the user command inside a subshell. It writes the exit code to
   one file and a `done` marker to another. The subshell matters: an `exit` inside the command
   would otherwise terminate the wrapper before the bookkeeping steps run.
3. `launchctl bootstrap gui/$(id -u) <plist>` submits the job; stdout and stderr are redirected
   to a log file.
4. Poll for the `done` marker. The timeout is controlled by `GUIRUN_TIMEOUT` and defaults to
   300 seconds.
5. `launchctl bootout` removes the job, the log is printed, and the script exits with the
   command's own exit code.

`launchctl asuser <uid> <cmd>` is not used: an SSH session has no permission to switch into
the GUI audit session, and that command fails with
`Could not switch to audit session ...: Operation not permitted`.

## 3. Installation

Copy `gui-run` to `~/bin/` on the target macOS host and make it executable:

```bash
install -m 755 gui-run ~/bin/gui-run
```

`~/bin` is usually not in the `PATH` of a non-interactive SSH session, so invoke the tool by
its full path, `~/bin/gui-run`.

## 4. Usage

```bash
~/bin/gui-run '<command>'
```

Examples:

```bash
# Signing
~/bin/gui-run "codesign --force --sign <identity> /path/to/Framework.framework"

# List devices
~/bin/gui-run "xcrun devicectl list devices"

# Build (redirect to a file when the output is large)
~/bin/gui-run "flutter build ios --release" > /tmp/build.log 2>&1

# Adjust the timeout
GUIRUN_TIMEOUT=1200 ~/bin/gui-run "flutter build ios --release"
```

The argument is a single string interpreted by `/bin/zsh`. Chain multiple steps inside the
string with `;` or `&&`.

## 5. Behaviour and limitations

| Item | Behaviour |
|-|-|
| Terminal windows | None are created; `Terminal.app` is never invoked |
| Exit code | Passed through unchanged from the command |
| Timeout | Defaults to 300 seconds, overridable with `GUIRUN_TIMEOUT=<seconds>`; returns 124 on timeout |
| No argument | Prints usage and returns 64 |
| Submission failure | Returns 71 with the reason when `launchctl bootstrap` fails |
| Environment variables | **Not inherited** from the SSH session; every run starts from a clean login environment. Export what you need inside the command |
| Working directory | The directory the tool was invoked from |
| SSH disconnect | The job already runs under `launchd`, so a dropped SSH connection does not stop it |
| Concurrency | Each invocation uses its own temporary directory and job label, so calls can run concurrently |

## 6. Verifying it works

Use `codesign` as a control to confirm the command really entered the GUI session:

```bash
# Direct over SSH: signing fails but still exits 0
codesign --force --sign <identity> a.txt
codesign -dvv a.txt          # -> a.txt: no signature

# Through gui-run: signing succeeds
~/bin/gui-run "codesign --force --sign <identity> b.txt"
codesign -dvv b.txt          # -> Signature size=... / Authority=...
```

To check whether a window was opened, do not use
`osascript -e 'tell application "Terminal" to count windows'` — that command itself launches
`Terminal.app`. Use `pgrep -l Terminal` instead; no output means it is not running.

## 7. Known issues and caveats

- When `codesign` fails over a direct SSH session it still exits 0, which is easy to mistake for
  success. Always inspect the output of `codesign -dvv`.
- If the private key ACL does not grant access to the relevant program, `gui-run` will still
  fail. `security set-key-partition-list` may be needed to adjust the ACL, and the GUI session
  must be unlocked.
- A `launchd` job does not read `~/.zprofile`. `gui-run` therefore probes for Homebrew
  automatically and appends common toolchain paths (`~/bin`, `~/development/flutter/bin`,
  `~/.local/bin`). Other toolchains need an explicit `export PATH=...` inside the command.
- macOS has no `timeout` command by default; use `GUIRUN_TIMEOUT` to bound a run.

## 8. When to use it

- Running `codesign`, `xcrun devicectl`, `flutter build ios` and similar commands on macOS over
  SSH, where a GUI session or a complete toolchain environment is required.
- Any automation that needs GUI session privileges without popping up terminal windows.

Not suitable for commands that read from the terminal's standard input; `gui-run` does not
support interactive input.

## 9. License

[MIT](LICENSE).
