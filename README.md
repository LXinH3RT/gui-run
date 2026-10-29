# gui-run — 在 macOS GUI 会话中执行命令且不弹终端窗口

本仓库收录一个小工具与其说明，用于解决「通过 SSH 在 macOS 上执行需要 GUI 会话权限的命令时，
每次都弹出新的终端窗口」这一问题。

## 1. 目标

macOS 的 SSH 会话属于非交互会话，不在 GUI（Aqua）audit session 内，因此无法访问
login keychain 中被 ACL 保护的私钥。典型表现为 `codesign` 失败：

```
$ ssh <host> 'codesign --force --sign <identity> a.txt'
a.txt: errSecInternalComponent
$ echo $?
0
```

注意退出码仍为 0，必须用 `codesign -dvv` 查看 `Authority=` 与 `Signature size=` 才能确认是否真正签名。

为绕过该限制，常见做法是把命令交给 `Terminal.app` 在 GUI 会话中执行：

```bash
osascript -e 'tell application "Terminal" to do script "/tmp/build.sh"'
```

`do script` 每调用一次即新建一个终端窗口或标签页，批量远程调试时窗口会迅速堆积。

`gui-run` 的目标是保留「进入 GUI 会话」的能力，同时完全不触碰 `Terminal.app`。

## 2. 实现原理

`launchctl bootstrap gui/<uid>` 可以把一个 `launchd` 作业提交到用户登录的 GUI 域中运行，
作业进程因此具备 GUI 会话上下文，可以读取 login keychain 中相应私钥的 ACL 授权。

`gui-run` 的流程：

1. 在 `$TMPDIR` 下建立临时工作目录，写入一个包装脚本与一个 `launchd` plist。
2. 包装脚本先恢复 login 环境（`brew shellenv`、Flutter 等工具链路径），再在子 shell 中执行用户命令，
   把退出码写入文件、把 `done` 标记写到另一个文件。使用子 shell 是为了让命令内部的 `exit`
   不会跳过后面的收尾动作。
3. `launchctl bootstrap gui/$(id -u) <plist>` 提交作业，标准输出与标准错误重定向到日志文件。
4. 轮询等待 `done` 标记，超时上限由 `GUIRUN_TIMEOUT` 控制，默认 300 秒。
5. `launchctl bootout` 回收作业，输出日志内容，并以命令自身的退出码退出。

不使用 `launchctl asuser <uid> <cmd>`：SSH 会话无权切换到 GUI audit session，该命令会报
`Could not switch to audit session ...: Operation not permitted`。

## 3. 安装

把 `gui-run` 复制到目标 macOS 主机的 `~/bin/` 并赋予执行权限：

```bash
install -m 755 gui-run ~/bin/gui-run
```

`~/bin` 通常不在非交互 SSH 会话的 `PATH` 中，调用时使用完整路径 `~/bin/gui-run`。

## 4. 用法

```bash
~/bin/gui-run '<命令>'
```

示例：

```bash
# 签名
~/bin/gui-run "codesign --force --sign <identity> /path/to/Framework.framework"

# 查看设备
~/bin/gui-run "xcrun devicectl list devices"

# 构建（输出较多时重定向到文件查看）
~/bin/gui-run "flutter build ios --release" > /tmp/build.log 2>&1

# 调整超时
GUIRUN_TIMEOUT=1200 ~/bin/gui-run "flutter build ios --release"
```

参数以字符串形式传入，由 `/bin/zsh` 解释；需要多步操作时在字符串内用 `;` 或 `&&` 连接。

## 5. 行为与限制

| 项目 | 行为 |
|-|-|
| 终端窗口 | 全程不创建，不访问 `Terminal.app` |
| 退出码 | 原样透传命令自身退出码 |
| 超时 | 默认 300 秒，`GUIRUN_TIMEOUT=<秒>` 覆盖，超时返回 124 |
| 无参数 | 打印用法，返回 64 |
| 提交失败 | `launchctl bootstrap` 失败时返回 71 并输出原因 |
| 环境变量 | **不继承** SSH 会话中的环境变量，每次都是干净的 login 环境；需要的变量在命令内 `export` |
| 工作目录 | 沿用调用时所在的目录 |
| SSH 断开 | 作业已在 `launchd` 中运行，SSH 断开不影响其继续执行 |
| 并发 | 每次调用使用独立的临时目录与作业标签，可并发 |

## 6. 验证方法

判断是否真的进入了 GUI 会话，用 `codesign` 做对照：

```bash
# SSH 直连：签名失败但仍返回 0
codesign --force --sign <identity> a.txt
codesign -dvv a.txt          # -> a.txt: no signature

# 经 gui-run：签名成功
~/bin/gui-run "codesign --force --sign <identity> b.txt"
codesign -dvv b.txt          # -> Signature size=... / Authority=...
```

判断是否开窗，不要用 `osascript -e 'tell application "Terminal" to count windows'`——
该命令本身会拉起 `Terminal.app`。应使用 `pgrep -l Terminal`，无输出即表示未运行。

## 7. 已知问题与注意事项

- `codesign` 在 SSH 直连下失败时退出码为 0，容易误判为成功；必须检查 `codesign -dvv` 输出。
- 若私钥 ACL 未授权给相关程序，`gui-run` 内仍会失败，可能需要 `security set-key-partition-list`
  调整 ACL，或确认 GUI 会话已解锁。
- `launchd` 作业不读 `~/.zprofile`，`gui-run` 已自动探测 Homebrew 并追加常用工具链路径
  （`~/bin`、`~/development/flutter/bin`、`~/.local/bin`）；其他工具链需在命令内显式 `export PATH=...`。
- macOS 默认没有 `timeout` 命令，需要限时用 `GUIRUN_TIMEOUT`。

## 8. 适用场景

- 通过 SSH 在 macOS 上执行 `codesign`、`xcrun devicectl`、`flutter build ios` 等需要 GUI 会话或
  完整工具链环境的命令。
- 任何需要 GUI 会话权限但不希望弹出终端窗口的自动化流程。

不适用于：需要读取终端标准输入进行交互的命令（`gui-run` 不支持交互式输入）。

## 9. 许可证

[MIT](LICENSE)。

