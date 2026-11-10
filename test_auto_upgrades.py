"""Offline automatic mining-power upgrade tests; no production keys or HTTP."""

import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

import requests

import bull_gems as bg
import test_bull_gems as fixtures


def upgrade(identifier="power-upgrade", cost=10, increment=25, **extra):
    return {"id": identifier, "type": "miningPower", "cost": cost,
            "increment": increment, **extra}


class UpgradeApi(fixtures.FakeMiningApi):
    def __init__(self, wallet, state):
        super().__init__(wallet, state)
        self.energy = 100
        self.mining_power = 0
        self.balance = {"gems": 100}
        self.upgrades_payload = [upgrade()]
        self.balance_error = None
        self.upgrades_error = None
        self.upgrade_error = None
        self.upgrade_result = {"ok": True}
        self.upgrade_attempts = 0
        self.apply_upgrade = True
        self.apply_on_error = False
        self.new_level = None

    def records(self):
        value = bg.unwrap(self.upgrades_payload)
        return value["upgrades"] if isinstance(value, dict) else value

    def request(self, method, path, body=None):
        if method == "GET" and path == "/user/me":
            self.calls.append((method, path, body))
            # Real JSON responses are snapshots, not live aliases of server data.
            return dict(self.me)
        if method == "GET" and path in {"/user/balance", "/upgrades"}:
            self.calls.append((method, path, body))
            error = self.balance_error if path == "/user/balance" else self.upgrades_error
            if error is not None:
                raise error
            return self.balance if path == "/user/balance" else self.upgrades_payload
        if method == "POST" and path.startswith("/upgrades/"):
            self.calls.append((method, path, body))
            self.upgrade_attempts += 1
            identifier = unquote(path.removeprefix("/upgrades/"))
            selected = next(item for item in self.records() if item["id"] == identifier)
            if self.apply_upgrade and (self.upgrade_error is None or self.apply_on_error):
                self.mining_power += selected["increment"]
                self.me["miningPower"] = self.mining_power
                self.balance["gems"] -= selected["cost"]
                if self.new_level is not None:
                    self.me["level"] = self.new_level
            if self.upgrade_error is not None:
                raise self.upgrade_error
            return self.upgrade_result
        return super().request(method, path, body)


class AutoUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = fixtures.local_temp()
        self.root = Path(self.temporary.name)
        bg.secure_directory(self.root / "state")
        self.network_guard = patch.object(
            bg.requests.Session, "request", side_effect=AssertionError("No real HTTP in tests"),
        )
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        args = SimpleNamespace(ref_code=bg.REF_CODE, interval=15, skip_quests=True,
                               auto_upgrades=True, skip_upgrades=False, skip_swap=True,
                               location_id=None, max_positions=None)
        self.miner = bg.Miner(fixtures.wallet(), self.root, fixtures.Gate(), args)
        self.miner.api.session.close()
        self.api = self.miner.api = UpgradeApi(self.miner.wallet, self.miner.state)

    def tearDown(self):
        self.api.session.close()
        self.temporary.cleanup()

    def profile(self):
        return {**self.api.me, "miningPower": self.api.mining_power}

    def upgrade_posts(self):
        return [(method, path, body) for method, path, body in self.api.calls
                if method == "POST" and path.startswith("/upgrades/")]

    def reset_upgrade_state(self):
        self.miner.state.clear()
        self.api.calls.clear()
        self.api.balance = {"gems": 100}
        self.api.mining_power = 0
        self.api.me["miningPower"] = 0
        self.api.me["level"] = 1
        self.api.upgrade_attempts = 0
        self.api.balance_error = None
        self.api.upgrades_error = None
        self.api.upgrade_error = None
        self.api.apply_upgrade = True
        self.api.apply_on_error = False
        self.api.new_level = None

    def test_opted_in_cycle_upgrades_once_then_still_mines(self):
        # The compatibility skip flag is optional, but buying requires opt-in.
        del self.miner.args.skip_upgrades
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.upgrade_attempts, 1)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertEqual(self.upgrade_posts(), [("POST", "/upgrades/power-upgrade", None)])
        self.assertNotIn("pending_upgrade", self.miner.state)
        self.assertEqual(self.api.mining_power, 25)
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.upgrade_attempts, 2)

    def test_default_or_missing_opt_in_retains_gems_and_still_mines(self):
        for mode in ("disabled", "missing"):
            with self.subTest(mode=mode):
                self.reset_upgrade_state()
                if mode == "disabled":
                    self.miner.args.auto_upgrades = False
                else:
                    del self.miner.args.auto_upgrades
                before = self.api.open_attempts
                self.assertFalse(self.miner.upgrade_power(self.profile(), force=True))
                self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.api.open_attempts, before + 1)
                self.assertEqual(self.api.upgrade_attempts, 0)
                self.assertEqual(self.api.balance["gems"], 100)
                self.assertFalse(any(path in {"/user/balance", "/upgrades"}
                                     for _, path, _ in self.api.calls))

    def test_skip_flag_disables_upgrade_requests_without_blocking_mining(self):
        self.miner.args.skip_upgrades = True
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertEqual(self.api.upgrade_attempts, 0)
        self.assertFalse(any(path in {"/user/balance", "/upgrades"} for _, path, _ in self.api.calls))

    def test_insufficient_gems_only_defers_upgrading(self):
        self.api.balance["gems"] = 9
        with patch.object(bg.time, "time", return_value=1000):
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.upgrade_attempts, 0)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertGreaterEqual(self.miner.state["upgrade_check_at"], 1060)
        self.assertNotIn("last_error", self.miner.state)
        self.assertNotIn("retry_at", self.miner.state)

    def test_force_can_bypass_regular_check_interval(self):
        self.api.balance["gems"] = 9
        with patch.object(bg.time, "time", return_value=1000):
            self.assertFalse(self.miner.upgrade_power(self.profile()))
            self.api.balance["gems"] = 100
            self.assertTrue(self.miner.upgrade_power(self.profile(), force=True))
        self.assertEqual(self.api.upgrade_attempts, 1)

    def test_claimed_reward_forces_check_before_regular_interval_expires(self):
        self.miner.state["upgrade_check_at"] = time.time() + 3600
        self.api.positions = [{"id": "ready-position", "status": "ready"}]
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.upgrade_attempts, 1)
        paths = [(method, path) for method, path, _ in self.api.calls]
        self.assertLess(paths.index(("POST", "/mining/positions/ready-position/claim")),
                        paths.index(("POST", "/upgrades/power-upgrade")))

    def test_selects_only_exact_mining_power_type_or_stat_by_cost_then_id(self):
        self.api.upgrades_payload = [
            upgrade("energy", cost=1, type="energyCapacity"),
            upgrade("wrong-case", cost=1, type="MiningPower"),
            upgrade("conflicting-type", cost=1, type="energy", stat="miningPower"),
            upgrade("inactive", cost=1, isActive=False),
            upgrade("locked", cost=1, isUnlocked=False),
            upgrade("also-locked", cost=1, unlocked=False),
            upgrade("z-power", cost=10),
            {"id": "a-power", "stat": "miningPower", "cost": 10, "increment": 25},
            upgrade("costlier", cost=20),
        ]
        self.assertTrue(self.miner.upgrade_power(self.profile()))
        self.assertEqual(self.upgrade_posts(), [("POST", "/upgrades/a-power", None)])

    def test_explicit_non_gems_currency_never_purchases(self):
        for field in ("currency", "costCurrency", "paymentCurrency"):
            for value in ("USDC", "ETH", "BASE", "", None, False, 1, {"symbol": "GEMS"}):
                with self.subTest(field=field, value=value):
                    self.reset_upgrade_state()
                    self.api.upgrades_payload = [upgrade(**{field: value})]
                    self.assertFalse(self.miner.upgrade_power(self.profile()))
                    self.assertEqual(self.api.upgrade_attempts, 0)

    def test_explicit_gems_currency_and_missing_currency_can_purchase(self):
        for extra in ({}, {"currency": "GEMS"}, {"costCurrency": " gems "},
                      {"paymentCurrency": "gEmS"},
                      {"currency": "gems", "costCurrency": "GEMS", "paymentCurrency": " GEMS "}):
            with self.subTest(extra=extra):
                self.reset_upgrade_state()
                self.api.upgrades_payload = [upgrade(**extra)]
                self.assertTrue(self.miner.upgrade_power(self.profile()))
                self.assertEqual(self.api.upgrade_attempts, 1)

    def test_conflicting_currency_fields_do_not_allow_a_non_gems_purchase(self):
        self.api.upgrades_payload = [upgrade(currency="GEMS", costCurrency="USDC")]
        self.assertFalse(self.miner.upgrade_power(self.profile()))
        self.assertEqual(self.api.upgrade_attempts, 0)

    def test_upgrade_response_array_and_wrappers_are_supported(self):
        record = upgrade()
        for payload in ([record], {"upgrades": [record]}, {"data": [record]}, {"data": {"upgrades": [record]}}):
            with self.subTest(payload=payload):
                self.reset_upgrade_state()
                self.api.upgrades_payload = payload
                self.assertTrue(self.miner.upgrade_power(self.profile()))
                self.assertEqual(self.api.upgrade_attempts, 1)

    def test_nonpositive_or_invalid_cost_and_increment_never_purchase(self):
        for field in ("cost", "increment"):
            for value in (None, False, 0, -1, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    self.reset_upgrade_state()
                    self.api.upgrades_payload = [upgrade(**{field: value})]
                    self.assertFalse(self.miner.upgrade_power(self.profile()))
                    self.assertEqual(self.api.upgrade_attempts, 0)

    def test_upgrade_success_refreshes_real_level_and_power_before_selecting_mine(self):
        self.api.new_level = 4
        starter = fixtures.LocationSelectionTests.location("starter")
        advanced = fixtures.LocationSelectionTests.location("advanced", level=4, power=25, cost=20)
        self.api.locations = [starter, advanced]
        self.miner.locations = [starter]
        self.miner.locations_profile = (1, 0)
        self.miner.locations_at = time.time() + 1000
        self.assertEqual(self.miner.tick(), 0.5)
        paths = [(method, path) for method, path, _ in self.api.calls]
        purchased = paths.index(("POST", "/upgrades/power-upgrade"))
        refreshed_me = paths.index(("GET", "/user/me"), purchased + 1)
        refreshed_stats = paths.index(("GET", "/mining/stats"), purchased + 1)
        mined = paths.index(("POST", "/mining/positions"))
        self.assertLess(purchased, refreshed_me)
        self.assertLess(purchased, refreshed_stats)
        self.assertLess(refreshed_me, mined)
        self.assertLess(refreshed_stats, mined)
        self.assertIn(("GET", "/mining/locations"), paths)
        self.assertIn(("POST", "/mining/positions", {"locationId": "advanced"}), self.api.calls)
        self.assertEqual(self.api.upgrade_attempts, 1)
        self.assertNotIn("pending_upgrade", self.miner.state)

    def test_power_upgrade_never_invents_a_higher_user_level(self):
        self.api.locations = [fixtures.LocationSelectionTests.location("starter"),
                              fixtures.LocationSelectionTests.location("advanced", level=4, power=25, cost=20)]
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.me["level"], 1)
        self.assertIn(("POST", "/mining/positions", {"locationId": "starter"}), self.api.calls)

    def test_zero_energy_does_not_block_upgrades_or_next_cycle(self):
        self.api.energy = 0
        self.api.locations = []
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.upgrade_attempts, 2)
        self.assertEqual(self.api.open_attempts, 0)
        self.assertNotIn("pending_upgrade", self.miner.state)

    def test_successful_upgrade_with_failed_profile_refresh_still_mines_from_old_snapshot(self):
        for refresh_path in ("/user/me", "/mining/stats"):
            with self.subTest(refresh_path=refresh_path):
                self.reset_upgrade_state()
                original_request = self.api.request

                def request(method, path, body=None):
                    if self.api.upgrade_attempts and method == "GET" and path == refresh_path:
                        self.api.calls.append((method, path, body))
                        raise requests.ReadTimeout("PRIVATE-REFRESH-ERROR")
                    return original_request(method, path, body)

                before = self.api.open_attempts
                with patch.object(self.api, "request", side_effect=request):
                    with self.assertLogs(bg.LOG, level="WARNING") as logs:
                        self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.api.open_attempts, before + 1)
                self.assertIn("pending_upgrade", self.miner.state)
                self.assertIn("pending_upgrade", bg.read_json(self.miner.path))
                self.assertNotIn("PRIVATE-REFRESH-ERROR", "\n".join(logs.output))
                self.assertNotIn("last_error", self.miner.state)

    def test_acknowledged_upgrade_stays_pending_until_power_readback_confirms_it(self):
        self.api.apply_upgrade = False
        self.assertTrue(self.miner.upgrade_power(self.profile()))
        pending = self.miner.state["pending_upgrade"]
        self.assertEqual(pending["upgrade_id"], "power-upgrade")
        self.assertEqual(pending["power_before"], 0)
        self.assertEqual(pending["cost"], 10)
        self.assertEqual(pending["gems_before"], 100)
        self.assertIn("created_at", pending)
        self.assertEqual(bg.read_json(self.miner.path)["pending_upgrade"], pending)
        self.assertFalse(self.miner.reconcile_power_upgrade(self.profile()))
        self.assertIn("pending_upgrade", self.miner.state)
        self.assertTrue(self.miner.reconcile_power_upgrade({**self.profile(), "miningPower": 25}))
        self.assertNotIn("pending_upgrade", self.miner.state)

    def test_stale_acknowledgement_does_not_repeat_purchase_or_stop_mining(self):
        self.api.apply_upgrade = False
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertIn("pending_upgrade", self.miner.state)
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.upgrade_attempts, 1)
        self.assertEqual(self.api.open_attempts, 2)

    def test_uncertain_upgrade_results_never_blindly_repeat_post(self):
        errors = [requests.ReadTimeout("SECRET-DO-NOT-LOG"), bg.ApiError(503),
                  bg.ShapeError("API returned non-JSON success response")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.reset_upgrade_state()
                self.api.upgrade_error = error
                with patch.object(bg.time, "time", return_value=1000):
                    self.assertFalse(self.miner.upgrade_power(self.profile()))
                self.assertIn("pending_upgrade", self.miner.state)
                self.assertIn("pending_upgrade", bg.read_json(self.miner.path))
                with patch.object(bg.time, "time", return_value=2000):
                    for _ in range(3):
                        self.assertFalse(self.miner.upgrade_power(self.profile(), force=True))
                self.assertEqual(self.api.upgrade_attempts, 1)

    def test_upgrade_timeout_keeps_mining_active_and_does_not_log_private_error_text(self):
        self.api.upgrade_error = requests.ReadTimeout("SECRET-DO-NOT-LOG")
        with self.assertLogs(bg.LOG, level="WARNING") as logs:
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertIn("pending_upgrade", self.miner.state)
        self.assertNotIn("SECRET-DO-NOT-LOG", "\n".join(logs.output))
        self.assertNotIn("last_error", self.miner.state)

    def test_power_readback_after_lost_response_clears_pending_without_second_purchase(self):
        self.api.upgrade_error = requests.ReadTimeout()
        self.api.apply_on_error = True
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertIn("pending_upgrade", self.miner.state)
        self.api.upgrade_error = None
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.upgrade_attempts, 1)
        self.assertNotIn("pending_upgrade", self.miner.state)
        self.assertEqual(self.api.open_attempts, 2)

    def test_pending_marker_must_be_saved_before_post(self):
        with patch.object(self.miner, "save", side_effect=OSError("TEST-DISK-FAILURE")):
            self.assertFalse(self.miner.upgrade_power(self.profile()))
        self.assertEqual(self.api.upgrade_attempts, 0)
        self.assertNotIn("pending_upgrade", self.miner.state)

    def test_pending_marker_is_durable_when_upgrade_post_is_sent(self):
        original_request = self.api.request

        def request(method, path, body=None):
            if method == "POST" and path.startswith("/upgrades/"):
                saved = bg.read_json(self.miner.path)["pending_upgrade"]
                self.assertEqual(saved, self.miner.state["pending_upgrade"])
                self.assertEqual(saved["upgrade_id"], "power-upgrade")
            return original_request(method, path, body)

        with patch.object(self.api, "request", side_effect=request):
            self.assertTrue(self.miner.upgrade_power(self.profile()))
        self.assertEqual(self.api.upgrade_attempts, 1)

    def test_failed_confirmation_save_keeps_pending_in_memory_and_on_disk(self):
        self.api.apply_upgrade = False
        self.assertTrue(self.miner.upgrade_power(self.profile()))
        marker = dict(self.miner.state["pending_upgrade"])
        with patch.object(self.miner, "save", side_effect=OSError("TEST-DISK-FAILURE")):
            self.assertFalse(self.miner.upgrade_power({**self.profile(), "miningPower": 25}))
        self.assertEqual(self.miner.state["pending_upgrade"], marker)
        self.assertEqual(bg.read_json(self.miner.path)["pending_upgrade"], marker)
        self.assertFalse(self.miner.upgrade_power(self.profile(), force=True))
        self.assertEqual(self.api.upgrade_attempts, 1)

    def test_definitive_client_error_clears_pending_and_force_cannot_bypass_retry_cooldown(self):
        self.api.upgrade_error = bg.ApiError(400)
        with patch.object(bg.time, "time", return_value=1000):
            self.assertFalse(self.miner.upgrade_power(self.profile()))
            self.assertNotIn("pending_upgrade", self.miner.state)
            self.assertGreaterEqual(self.miner.state["upgrade_retry_at"], 1300)
            self.assertFalse(self.miner.upgrade_power(self.profile(), force=True))
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.upgrade_attempts, 1)
        self.assertEqual(self.api.open_attempts, 1)

    def test_balance_and_upgrade_list_failures_do_not_block_mining(self):
        for field, error in (("balance_error", requests.ReadTimeout()), ("upgrades_error", bg.ApiError(503))):
            with self.subTest(field=field):
                self.reset_upgrade_state()
                self.api.balance_error = None
                self.api.upgrades_error = None
                setattr(self.api, field, error)
                before = self.api.open_attempts
                self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.api.open_attempts, before + 1)
                self.assertEqual(self.api.upgrade_attempts, 0)

    def test_restart_reuses_pending_upgrade_without_resending_purchase(self):
        self.api.upgrade_error = requests.ReadTimeout()
        self.assertFalse(self.miner.upgrade_power(self.profile()))
        self.assertIn("pending_upgrade", bg.read_json(self.miner.path))
        replacement = bg.Miner(self.miner.wallet, self.root, fixtures.Gate(), self.miner.args)
        replacement.api.session.close()
        replacement_api = replacement.api = UpgradeApi(replacement.wallet, replacement.state)
        try:
            self.assertEqual(replacement.tick(), 0.5)
            self.assertEqual(replacement_api.upgrade_attempts, 0)
            self.assertEqual(replacement_api.open_attempts, 1)
            self.assertIn("pending_upgrade", replacement.state)
        finally:
            replacement_api.session.close()


class UpgradeCliTests(unittest.TestCase):
    def test_automatic_upgrades_default_off_and_require_explicit_opt_in(self):
        cases = (([], (False, False)), (["--auto-upgrades"], (True, False)),
                 (["--skip-upgrades"], (False, True)))
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                captured = []

                def run_client(args, stop):
                    captured.append((args.auto_upgrades, args.skip_upgrades))
                    stop.set()
                    return 0

                with patch.object(bg.signal, "signal"), patch.object(bg, "run_client", side_effect=run_client):
                    self.assertEqual(bg.main(arguments), 0)
                self.assertEqual(captured, [expected])

    def test_upgrade_opt_in_and_legacy_skip_flags_are_mutually_exclusive(self):
        with patch.object(bg, "run_client") as run, patch("sys.stderr"):
            with self.assertRaises(SystemExit) as result:
                bg.main(["--auto-upgrades", "--skip-upgrades"])
        self.assertEqual(result.exception.code, 2)
        run.assert_not_called()

    def test_clear_pending_upgrade_only_removes_upgrade_markers_and_never_loads_wallets(self):
        with fixtures.local_temp() as directory:
            root = Path(directory)
            bg.secure_directory(root / "state")
            address = fixtures.wallet()["address"]
            state_path = root / "state" / (address.lower() + ".json")
            original = {"pending_upgrade": {"upgrade_id": "uncertain", "power_before": 0},
                        "pending_open": {"before_ids": [], "location_id": "starter"},
                        "upgrade_check_at": 999, "upgrade_retry_at": 999, "token": "preserved"}
            bg.write_json(state_path, original)
            missing_wallet = root / "must-not-be-created.json"
            with patch.object(bg.signal, "signal"), \
                    patch.object(bg, "load_wallets", side_effect=AssertionError("No vault needed")), \
                    patch.object(bg.Account, "create") as create, \
                    patch.object(bg.requests.Session, "request", side_effect=AssertionError("No HTTP")):
                self.assertEqual(bg.main(["--data-dir", str(root), "--wallet-file", str(missing_wallet),
                                          "--clear-pending-upgrade", address]), 0)
            self.assertEqual(bg.read_json(state_path), {"pending_open": original["pending_open"],
                                                        "token": "preserved"})
            self.assertFalse(missing_wallet.exists())
            create.assert_not_called()

    def test_clearing_deployment_and_upgrade_markers_are_mutually_exclusive(self):
        address = fixtures.wallet()["address"]
        with patch.object(bg, "run_client") as run, patch("sys.stderr"):
            with self.assertRaises(SystemExit) as result:
                bg.main(["--clear-pending", address, "--clear-pending-upgrade", address])
        self.assertEqual(result.exception.code, 2)
        run.assert_not_called()

    def test_invalid_maintenance_address_is_rejected_before_runtime_starts(self):
        for flag in ("--clear-pending", "--clear-pending-upgrade"):
            for address in ("", "not-an-address", "0x1234", "../state", " " + fixtures.wallet()["address"]):
                with self.subTest(flag=flag, address=address):
                    with patch.object(bg, "run_client") as run, patch("sys.stderr"):
                        with self.assertRaises(SystemExit) as result:
                            bg.main([flag, address])
                    self.assertEqual(result.exception.code, 2)
                    run.assert_not_called()

    def test_run_client_direct_invalid_maintenance_input_never_loads_or_generates_wallets(self):
        with fixtures.local_temp() as directory:
            root = Path(directory)
            for field in ("clear_pending", "clear_pending_upgrade"):
                for address in ("", "invalid"):
                    with self.subTest(field=field, address=address):
                        args = SimpleNamespace(data_dir=root, clear_pending=None,
                                               clear_pending_upgrade=None)
                        setattr(args, field, address)
                        with patch.object(bg, "load_wallets") as load, \
                                patch.object(bg, "ensure_wallets") as ensure, \
                                patch.object(bg.requests.Session, "request", side_effect=AssertionError("No HTTP")):
                            with self.assertRaisesRegex(bg.WalletPaused, "Invalid wallet address"):
                                bg.run_client(args, threading.Event())
                        load.assert_not_called()
                        ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
