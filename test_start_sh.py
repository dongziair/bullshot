"""Offline tests for the Python launcher embedded in the Linux start.sh."""

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parent / "start.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")
PYTHON_SOURCE = SOURCE.split("<<'PYTHON'\n", 1)[1].rsplit("\nPYTHON", 1)[0]
NAMESPACE = {"__name__": "bull_gems_launcher_test"}
exec(compile(PYTHON_SOURCE, str(SCRIPT), "exec"), NAMESPACE)
Launcher = NAMESPACE["Launcher"]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        workspace = SCRIPT.parent.resolve()
        self.temporary = tempfile.TemporaryDirectory(dir=workspace, prefix=".test-bull-gems-")
        self.root = Path(self.temporary.name).resolve()
        if self.root.parent != workspace:
            raise AssertionError("Temporary directory escaped workspace")
        self.launcher = Launcher(self.root, proc_root=self.root / "test-proc")
        self.launcher.script.write_text("# Mock miner; never executed.\n", encoding="utf-8")
        self.launcher.requirements.write_text("requests>=2.32,<3\n", encoding="ascii")
        self.launcher.wallet_file.write_text(json.dumps({"version": 1, "wallets": [{"address": "TEST-1", "private_key": "TEST-ONLY"}, {"address": "TEST-2", "private_key": "TEST-ONLY"}]}), encoding="utf-8")
        self.fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=Mock())

    def tearDown(self):
        self.temporary.cleanup()

    def fake_environment(self, cached=False):
        self.launcher.python.parent.mkdir(parents=True)
        self.launcher.python.touch()
        if cached:
            fingerprint = hashlib.sha256(self.launcher.requirements.read_bytes()).hexdigest()
            marker = self.launcher.python.parent.parent / ".bull_gems_requirements.sha256"
            marker.write_text(fingerprint + "\n", encoding="ascii")

    def test_launches_background_reading_wallet_json_without_generation_flags(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.dict("sys.modules", {"fcntl": self.fcntl}), patch.object(self.launcher, "running_pid", return_value=None), patch.object(self.launcher, "ensure_environment") as setup, patch.object(subprocess, "Popen", return_value=process) as popen, patch.object(NAMESPACE["time"], "sleep"):
            result = self.launcher.start(["--resume"])
        self.assertEqual(result, 0)
        setup.assert_called_once_with()
        command = popen.call_args.args[0]
        self.assertEqual(command, [str(self.launcher.python), "-u", str(self.launcher.script), "--wallet-file", str(self.launcher.wallet_file), "--ref-code", "0x75527f54", "--resume"])
        self.assertNotIn("--wallets", command)
        self.assertNotIn("--init-only", command)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIsNone(popen.call_args.kwargs["stdout"])
        self.assertIsNone(popen.call_args.kwargs["stderr"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.root))
        self.assertEqual(self.launcher.read_pid(), 12345)
        self.assertEqual(list(self.root.glob("*.log")), [])

    def test_generation_runs_once_with_selected_count_and_no_background_process(self):
        with patch.dict("sys.modules", {"fcntl": self.fcntl}), patch.object(self.launcher, "running_pid", return_value=None), patch.object(self.launcher, "ensure_environment"), patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, patch.object(subprocess, "Popen") as popen:
            self.assertEqual(self.launcher.generate(["37"]), 0)
        self.assertEqual(run.call_args.args[0], [str(self.launcher.python), str(self.launcher.script), "--init-only", "--wallets", "37", "--wallet-file", str(self.launcher.wallet_file)])
        popen.assert_not_called()
        self.assertFalse(self.launcher.pid_file.exists())

    def test_start_rejects_generation_flags(self):
        with patch.object(subprocess, "Popen") as popen:
            for args in (["--wallets", "50"], ["--wallets=50"], ["--init-only"]):
                with self.assertRaises(RuntimeError):
                    self.launcher.start(args)
        popen.assert_not_called()

    def test_start_without_wallet_file_does_not_generate_or_spawn(self):
        self.launcher.wallet_file.unlink()
        with patch.dict("sys.modules", {"fcntl": self.fcntl}), patch.object(self.launcher, "running_pid", return_value=None), patch.object(self.launcher, "ensure_environment") as setup, patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                self.launcher.start([])
        setup.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.launcher.wallet_file.exists())

    def test_generation_refuses_to_change_wallet_count_while_running(self):
        with patch.dict("sys.modules", {"fcntl": self.fcntl}), patch.object(self.launcher, "running_pid", return_value=12345), patch.object(subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                self.launcher.generate(["5"])
        run.assert_not_called()

    def test_generation_requires_positive_bounded_count(self):
        with patch.object(subprocess, "run") as run:
            for args in ([], ["0"], ["-1"], ["10001"]):
                with self.assertRaises(SystemExit):
                    self.launcher.generate(args)
        run.assert_not_called()

    def test_logs_command_does_not_open_or_replay_any_file(self):
        with patch.object(NAMESPACE["platform"], "system", return_value="Linux"), patch.object(os, "execvp") as execute:
            self.assertEqual(NAMESPACE["main"]([str(self.root), "logs"]), 0)
        execute.assert_not_called()
        self.assertEqual(list(self.root.glob("*.log")), [])

    def test_already_running_does_not_install_or_spawn(self):
        with patch.dict("sys.modules", {"fcntl": self.fcntl}), patch.object(self.launcher, "running_pid", return_value=12345), patch.object(self.launcher, "ensure_environment") as setup, patch.object(subprocess, "Popen") as popen:
            self.assertEqual(self.launcher.start([]), 0)
        setup.assert_not_called()
        popen.assert_not_called()

    def test_bootstrap_lock_prevents_concurrent_installation(self):
        self.fcntl.flock.side_effect = BlockingIOError()
        with patch.dict("sys.modules", {"fcntl": self.fcntl}), patch.object(self.launcher, "ensure_environment") as setup, patch.object(subprocess, "Popen") as popen:
            self.assertEqual(self.launcher.start([]), 0)
        setup.assert_not_called()
        popen.assert_not_called()

    def test_cached_healthy_environment_does_not_run_pip(self):
        self.fake_environment(cached=True)
        with patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.launcher.ensure_environment()
        self.assertEqual(run.call_count, 2)
        self.assertFalse(any("install" in call.args[0] for call in run.call_args_list))

    def test_uncached_environment_installs_requirements(self):
        self.fake_environment()
        with patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.launcher.ensure_environment()
        install = [call.args[0] for call in run.call_args_list if "install" in call.args[0]]
        self.assertEqual(len(install), 1)
        self.assertIn(str(self.launcher.requirements), install[0])
        self.assertTrue((self.launcher.python.parent.parent / ".bull_gems_requirements.sha256").is_file())

    def test_half_created_environment_is_repaired(self):
        self.fake_environment()
        with patch.object(subprocess, "run", side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)]), patch.object(self.launcher, "create_environment") as repair:
            self.launcher.ensure_environment()
        repair.assert_called_once_with()

    def test_root_apt_fallback_repairs_missing_venv(self):
        failure = subprocess.CalledProcessError(1, ["python3", "-m", "venv"])
        with patch.object(os, "geteuid", return_value=0, create=True), patch.object(NAMESPACE["shutil"], "which", return_value="/usr/bin/apt-get"), patch.object(subprocess, "run", side_effect=[failure, Mock(), Mock(), Mock()]) as run:
            self.launcher.create_environment()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["apt-get", "install", "-y", "python3-venv"], commands)
        self.assertEqual(commands[0], commands[-1])

    def test_startup_failure_is_not_reported_as_running(self):
        process = Mock(pid=12345)
        process.poll.return_value = 2
        with patch.dict("sys.modules", {"fcntl": self.fcntl}), patch.object(self.launcher, "running_pid", return_value=None), patch.object(self.launcher, "ensure_environment"), patch.object(subprocess, "Popen", return_value=process), patch.object(NAMESPACE["time"], "sleep"):
            self.assertEqual(self.launcher.start([]), 2)
        self.assertFalse(self.launcher.pid_file.exists())

    def test_stale_pid_does_not_signal_unrelated_process(self):
        self.launcher.pid_file.write_text("12345\n", encoding="ascii")
        proc = self.launcher.proc_root / "12345"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(b"/usr/bin/bash\0other-task.sh\0")
        with patch.object(os, "kill") as kill:
            self.assertEqual(self.launcher.stop(), 0)
        kill.assert_not_called()

    def test_running_pid_requires_matching_script(self):
        self.launcher.pid_file.write_text("12345\n", encoding="ascii")
        proc = self.launcher.proc_root / "12345"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(os.fsencode(str(self.launcher.python)) + b"\0-u\0" + os.fsencode(str(self.launcher.script)) + b"\0")
        with patch.object(os, "kill") as kill:
            self.assertEqual(self.launcher.running_pid(), 12345)
        kill.assert_called_once_with(12345, 0)

    def test_stop_sends_sigterm_and_removes_only_old_pid(self):
        self.launcher.pid_file.write_text("12345\n", encoding="ascii")
        with patch.object(self.launcher, "running_pid", side_effect=[12345, None]), patch.object(os, "kill") as kill:
            self.assertEqual(self.launcher.stop(), 0)
        kill.assert_called_once_with(12345, signal.SIGTERM)
        self.assertFalse(self.launcher.pid_file.exists())
        self.launcher.pid_file.write_text("99999\n", encoding="ascii")
        self.launcher.clear_pid(12345)
        self.assertEqual(self.launcher.read_pid(), 99999)

    def test_shell_file_uses_unix_line_endings(self):
        self.assertNotIn(b"\r", SCRIPT.read_bytes())
        self.assertNotIn(b"\r", (SCRIPT.parent / "generate_wallets.sh").read_bytes())


if __name__ == "__main__":
    unittest.main()
