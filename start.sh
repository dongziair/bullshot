#!/usr/bin/env bash
# Step 2: read wallet.json and run. Step 1 is generate_wallets.sh COUNT.
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
import argparse
import contextlib
import hashlib
import json
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
        self.wallet_file = self.root / "wallet.json"

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
        if any(arg.split("=", 1)[0] in {"--init-only", "--wallets"} for arg in extra_args):
            raise RuntimeError("运行脚本不生成钱包；请先执行 bash generate_wallets.sh 数量。")
        options = argparse.ArgumentParser(add_help=False)
        options.add_argument("--wallet-file", type=Path, default=self.wallet_file)
        selected, extra_args = options.parse_known_args(extra_args)
        wallet_file = selected.wallet_file.expanduser()
        wallet_file = (self.root / wallet_file).resolve()
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
            if not wallet_file.is_file():
                raise RuntimeError("缺少 wallet.json；请先执行 bash generate_wallets.sh 100（数量可修改）。")
            vault = json.loads(wallet_file.read_text(encoding="utf-8"))
            if not isinstance(vault, dict) or vault.get("version") != 1 or not isinstance(vault.get("wallets"), list) or not vault["wallets"]:
                raise RuntimeError("钱包文件格式错误或没有钱包；不会自动创建或覆盖。")
            count = len(vault["wallets"])
            self.ensure_environment()
            command = [
                str(self.python), "-u", str(self.script),
                "--wallet-file", str(wallet_file), "--ref-code", "0x75527f54", *extra_args,
            ]
            # Detach from the terminal's process group, but keep console output.
            # The client discards output after SSH closes; no log file is opened.
            process = subprocess.Popen(
                command, cwd=str(self.root), stdin=subprocess.DEVNULL,
                stdout=None, stderr=None, start_new_session=True,
                close_fds=True,
            )
            self.pid_file.write_text(str(process.pid) + "\n", encoding="ascii")
            self.pid_file.chmod(0o600)
            time.sleep(2)
            code = process.poll()
            if code is not None:
                self.clear_pid(process.pid)
                print(f"进程已经退出（退出码 {code}），请查看当前窗口的错误信息。", file=sys.stderr)
                return code or 1
            print(f"已后台启动，PID={process.pid}；读取 {count} 个钱包，邀请码 0x75527f54。")
            print(f"钱包文件：{wallet_file}（只读使用，不自动生成或补齐）")
            print("日志只在当前启动窗口打印，不写日志文件；关闭窗口后不保留历史日志。")
            print("停止运行：bash start.sh stop")
            return 0

    def generate(self, args):
        options = argparse.ArgumentParser(prog="bash generate_wallets.sh", description="只生成钱包到 wallet.json，不注册、不挖矿；数量是目标总数。")
        options.add_argument("count", type=int, help="钱包目标总数，范围 1..10000")
        options.add_argument("--wallet-file", type=Path, default=self.wallet_file)
        selected = options.parse_args(args)
        if not 1 <= selected.count <= 10000:
            options.error("数量必须在 1..10000 之间")
        wallet_file = (self.root / selected.wallet_file.expanduser()).resolve()
        import fcntl

        with (self.root / ".bull_gems_start.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("另一个启动器正在准备环境，请稍后重试。")
                return 0
            if self.running_pid() is not None:
                raise RuntimeError("请先 bash start.sh stop，再调整钱包数量。")
            self.ensure_environment()
            subprocess.run([
                str(self.python), str(self.script), "--init-only",
                "--wallets", str(selected.count), "--wallet-file", str(wallet_file),
            ], cwd=str(self.root), check=True)
            print(f"钱包文件已准备好：{wallet_file}；目标总数 {selected.count}。")
            print("现在执行 bash start.sh 读取钱包并循环运行。")
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
    action = args.pop(0) if args and args[0] in {"start", "stop", "status", "logs", "generate"} else "start"
    if action not in {"start", "generate"} and args:
        print("用法：bash start.sh [start|stop|status|logs]；启动时可附加 --resume 等参数。", file=sys.stderr)
        return 1
    if action == "stop":
        return launcher.stop()
    if action == "status":
        pid = launcher.running_pid()
        print(f"正在运行，PID={pid}" if pid is not None else "当前未运行。")
        return 0 if pid is not None else 1
    if action == "logs":
        print("日志仅在启动时的窗口打印，不写入文件；没有可追溯或重新附加的历史日志。")
        return 0
    if action == "generate":
        return launcher.generate(args)
    return launcher.start(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        label = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"启动失败：{label}", file=sys.stderr)
        raise SystemExit(1)
PYTHON
