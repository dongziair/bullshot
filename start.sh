#!/usr/bin/env bash
# Linux one-command bootstrap. Usage: bash start.sh [start|stop|status|logs]
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "$(uname -s)" != "Linux" ]]; then
    echo "此启动器仅用于 Linux。" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    if [[ "${EUID}" -eq 0 ]] && command -v apt-get >/dev/null 2>&1; then
        echo "正在安装 Python 和 venv……"
        apt-get update
        apt-get install -y python3 python3-venv
    else
        echo "请先安装 Python 3.10 或以上版本，再执行 bash start.sh。" >&2
        exit 1
    fi
fi

exec python3 - "$SCRIPT_DIR" "$@" <<'PYTHON'
import contextlib
import hashlib
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


class Launcher:
    def __init__(self, root, proc_root=Path("/proc")):
        self.root = Path(root).resolve()
        self.proc_root = Path(proc_root)
        self.script = self.root / "bull_gems.py"
        self.requirements = self.root / "requirements.txt"
        self.python = self.root / ".venv" / "bin" / "python"
        self.pid_file = self.root / "bull_gems.pid"
        self.log_file = self.root / "bull_gems.log"

    def read_pid(self):
        try:
            text = self.pid_file.read_text(encoding="ascii").strip()
            return int(text) if text.isascii() and text.isdigit() and int(text) > 1 else None
        except (OSError, ValueError):
            return None

    def running_pid(self):
        pid = self.read_pid()
        if pid is None:
            return None
        try:
            args = (self.proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
            args = [os.fsdecode(arg) for arg in args if arg]
            if len(args) < 2 or not Path(args[0]).name.startswith("python"):
                return None
            absolute = str(self.script) in args
            relative = "bull_gems.py" in args and (self.proc_root / str(pid) / "cwd").resolve() == self.root
            if not absolute and not relative:
                return None
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def clear_pid(self, expected):
        # Do not remove a PID file written by a concurrent, newer launcher.
        if self.read_pid() == expected:
            with contextlib.suppress(FileNotFoundError):
                self.pid_file.unlink()

    def create_environment(self):
        print("正在创建独立 Python 环境……", flush=True)
        command = [sys.executable, "-m", "venv", str(self.python.parent.parent)]
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError:
            if os.geteuid() != 0 or not shutil.which("apt-get"):
                raise RuntimeError("缺少 venv；Ubuntu/Debian 可安装 python3-venv 后重新运行。") from None
            print("正在补装 python3-venv……", flush=True)
            subprocess.run(["apt-get", "update"], check=True)
            subprocess.run(["apt-get", "install", "-y", "python3-venv"], check=True)
            subprocess.run(command, check=True)

    def ensure_environment(self):
        if not self.script.is_file() or not self.requirements.is_file():
            raise RuntimeError("请先解压完整脚本包，start.sh 必须和 bull_gems.py、requirements.txt 放在一起。")
        if not self.python.is_file():
            self.create_environment()
        elif subprocess.run(
            [str(self.python), "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode:
            # Repair an environment left half-created by an interrupted setup.
            self.create_environment()
        fingerprint = hashlib.sha256(self.requirements.read_bytes()).hexdigest()
        marker = self.python.parent.parent / ".bull_gems_requirements.sha256"
        cached = marker.read_text(encoding="ascii", errors="replace").strip() if marker.is_file() else ""
        probe = subprocess.run(
            [str(self.python), "-c", "import eth_account, requests"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if cached != fingerprint or probe.returncode:
            print("正在安装脚本依赖……", flush=True)
            subprocess.run([
                str(self.python), "-m", "pip", "install", "--disable-pip-version-check",
                "-r", str(self.requirements),
            ], check=True)
            marker.write_text(fingerprint + "\n", encoding="ascii")

    def start(self, extra_args):
        import fcntl

        # Hold a short-lived bootstrap lock, not the long-running miner's lock.
        with (self.root / ".bull_gems_start.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("另一个启动器正在准备环境，请稍后查看状态。")
                return 0
            pid = self.running_pid()
            if pid is not None:
                print(f"已经在运行，PID={pid}，不会重复启动。")
                return 0
            self.ensure_environment()
            command = [
                str(self.python), "-u", str(self.script),
                "--wallets", "100", "--ref-code", "0x75527f54", *extra_args,
            ]
            # Detach without nohup/shell; closing SSH does not terminate this process.
            with self.log_file.open("ab") as log:
                self.log_file.chmod(0o600)
                process = subprocess.Popen(
                    command, cwd=str(self.root), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                    close_fds=True,
                )
            self.pid_file.write_text(str(process.pid) + "\n", encoding="ascii")
            self.pid_file.chmod(0o600)
            time.sleep(2)
            code = process.poll()
            if code is not None:
                self.clear_pid(process.pid)
                print(f"进程已经退出（退出码 {code}）。请执行：bash start.sh logs", file=sys.stderr)
                return code or 1
            print(f"已后台启动，PID={process.pid}；100 个钱包，邀请码 0x75527f54。")
            print(f"钱包和私钥：{self.root / 'bull_gems_data' / 'wallets.json'}（仅本地保存）")
            print(f"运行日志：{self.log_file}")
            print("查看日志：bash start.sh logs")
            print("停止运行：bash start.sh stop")
            return 0

    def stop(self):
        pid = self.running_pid()
        if pid is None:
            print("当前未运行；没有向其他进程发送信号。")
            return 0
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self.clear_pid(pid)
            print("已停止。")
            return 0
        print("正在等待进程保存状态并退出……", flush=True)
        for _ in range(40):
            if self.running_pid() != pid:
                self.clear_pid(pid)
                print("已停止；下次执行 bash start.sh 即可续跑。")
                return 0
            time.sleep(1)
        print("进程仍在退出中，稍后执行 bash start.sh status 查看状态。")
        return 1


def main(argv):
    if platform.system() != "Linux" or sys.version_info < (3, 10):
        print("需要 Linux 和 Python 3.10 或以上版本。", file=sys.stderr)
        return 1
    launcher = Launcher(argv[0])
    args = argv[1:]
    action = args.pop(0) if args and args[0] in {"start", "stop", "status", "logs"} else "start"
    if action != "start" and args:
        print("用法：bash start.sh [start|stop|status|logs]；启动时可附加 --resume 等参数。", file=sys.stderr)
        return 1
    if action == "stop":
        return launcher.stop()
    if action == "status":
        pid = launcher.running_pid()
        print(f"正在运行，PID={pid}" if pid is not None else "当前未运行。")
        return 0 if pid is not None else 1
    if action == "logs":
        if not launcher.log_file.is_file():
            print("还没有日志，请先执行 bash start.sh。", file=sys.stderr)
            return 1
        os.execvp("tail", ["tail", "-n", "50", "-f", str(launcher.log_file)])
    return launcher.start(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        raise SystemExit(1)
PYTHON
