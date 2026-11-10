"""Offline-only tests: deterministic public test keys, mocked HTTP, no registration."""

import itertools
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

import bull_gems as bg


def local_temp():
    root = Path(__file__).resolve().parent
    temporary = tempfile.TemporaryDirectory(dir=root, prefix=".test-bull-gems-")
    if Path(temporary.name).resolve().parent != root:
        raise AssertionError("Test temporary path escaped workspace")
    return temporary


def wallet(index=1):
    # Public deterministic test key, never a funded/production wallet.
    account = Account.from_key(index.to_bytes(32, "big"))
    return {"address": account.address, "private_key": "0x" + account.key.hex().removeprefix("0x")}


class Gate:
    def __init__(self):
        self.pauses = []

    def enter(self):
        pass

    def pause(self, seconds):
        self.pauses.append(seconds)


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class ApiTests(unittest.TestCase):
    def api(self, responses, token=""):
        state = {"token": token}
        api = bg.Api(wallet(), state, Mock(), Gate(), bg.REF_CODE)
        api.session.close()
        api.session = Session(responses)
        return api

    def test_personal_sign_and_referral(self):
        message = bg.SIGN_PREFIX + "test-nonce"
        api = self.api([
            Response({"message": message}), Response({"token": "new-token"}),
            Response({"referredBy": "referrer"}),
        ])
        api.login()
        payload = api.session.calls[1][2]["json"]
        self.assertTrue(payload["signature"].startswith("0x"))
        recovered = Account.recover_message(encode_defunct(text=message), signature=payload["signature"])
        self.assertEqual(recovered, wallet()["address"])
        self.assertEqual(payload["refCode"], "0x75527f54")
        self.assertNotIn("private_key", payload)
        self.assertEqual(api.session.calls[2][2]["headers"]["Authorization"], "Bearer new-token")

    def test_401_reauthenticates_and_replays_once(self):
        api = self.api([
            Response(status=401), Response({"message": bg.SIGN_PREFIX + "nonce"}),
            Response({"token": "fresh"}), Response({"referredBy": "yes"}), Response({"ok": True}),
        ], token="expired")
        self.assertEqual(api.request("POST", "/quests/start", {"questId": "daily_miner_signin"}), {"ok": True})
        self.assertEqual(len(api.session.calls), 5)
        self.assertEqual(api.session.calls[0][2]["headers"]["Authorization"], "Bearer expired")
        self.assertEqual(api.session.calls[-1][2]["headers"]["Authorization"], "Bearer fresh")
        for _, _, kwargs in api.session.calls:
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["headers"]["X-Wallet-Address"], wallet()["address"])

    def test_second_401_does_not_loop(self):
        api = self.api([
            Response(status=401), Response({"nonce": "n"}), Response({"token": "fresh"}),
            Response({"referredBy": "yes"}), Response(status=401),
        ], token="old")
        with self.assertRaises(bg.ApiError):
            api.request("GET", "/user/me")
        self.assertEqual(len(api.session.calls), 5)

    def test_referral_missing_does_not_block_mutation_after_reauthentication(self):
        api = self.api([
            Response(status=401), Response({"nonce": "n"}), Response({"token": "fresh"}),
            Response({"referredBy": None}), Response({"ok": True}),
        ], token="old")
        self.assertEqual(api.request("POST", "/mining/positions", {"locationId": "mine"}), {"ok": True})
        self.assertEqual(len(api.session.calls), 5)
        self.assertEqual(api.session.calls[-1][2]["headers"]["Authorization"], "Bearer fresh")

    def test_unconfirmed_referral_keeps_valid_token_for_mining(self):
        api = self.api([
            Response({"nonce": "n"}), Response({"token": "unbound-token"}),
            Response({"referredBy": None}),
        ])
        api.login()
        self.assertEqual(api.state["token"], "unbound-token")
        self.assertEqual(api.state["referral_status"], "empty")
        self.assertEqual(api.session.calls[1][2]["json"]["refCode"], "0x75527f54")

    def test_referral_missing_and_empty_are_distinguished_without_blocking(self):
        api = self.api([], token="valid-token")
        cases = [({}, "missing"), ({"referredBy": None}, "empty"), ({"referredBy": "  "}, "empty"), ({"referredBy": "referrer"}, "present")]
        for me, expected in cases:
            with self.subTest(status=expected):
                self.assertEqual(api.verify_referral(me), expected == "present")
                self.assertEqual(api.state["referral_status"], expected)
                self.assertEqual(api.state["token"], "valid-token")

    def test_referral_warning_is_not_repeated_on_every_poll(self):
        api = self.api([], token="valid-token")
        with self.assertLogs(bg.LOG, level="WARNING") as logs:
            for _ in range(3):
                self.assertFalse(api.verify_referral({"referredBy": None}))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("quests and mining continue", logs.output[0])

    def test_unexpected_message_is_not_signed(self):
        api = self.api([Response({"message": "Send all tokens"})])
        with patch.object(bg.Account, "sign_message") as signer:
            with self.assertRaises(bg.WalletPaused):
                api.login()
            signer.assert_not_called()
        self.assertEqual(len(api.session.calls), 1)

    def test_transport_timeout_does_not_retry_post(self):
        api = self.api([requests.Timeout()], token="token")
        with self.assertRaises(requests.Timeout):
            api.request("POST", "/mining/positions", {"locationId": "mine"})
        self.assertEqual(len(api.session.calls), 1)

    def test_read_timeout_retains_safe_request_context_without_changing_exception_type(self):
        api = self.api([requests.ReadTimeout("SECRET-DO-NOT-LOG")], token="token")
        with self.assertRaises(requests.ReadTimeout) as raised:
            api.request("GET", "/mining/stats")
        self.assertEqual(getattr(raised.exception, "bull_gems_operation", None), "GET /mining/stats")
        self.assertGreaterEqual(raised.exception.bull_gems_elapsed, 0)
        self.assertEqual(len(api.session.calls), 1)

    def test_transport_error_context_omits_url_query(self):
        api = self.api([requests.ReadTimeout("SECRET-DO-NOT-LOG")], token="token")
        with self.assertRaises(requests.ReadTimeout) as raised:
            api.request("GET", "/mining/stats?token=SECRET-QUERY")
        self.assertEqual(raised.exception.bull_gems_operation, "GET /mining/stats")
        self.assertNotIn("SECRET", raised.exception.bull_gems_operation)

    def test_429_pauses_all_clients(self):
        api = self.api([Response(status=429, headers={"Retry-After": "123"})], token="t")
        with self.assertRaises(bg.ApiError) as error:
            api.request("GET", "/user/me")
        self.assertEqual(error.exception.retry_after, 123)
        self.assertEqual(api.gate.pauses, [123])

    def test_redirect_is_not_followed(self):
        api = self.api([Response(status=302, headers={"Location": "https://other.invalid"})], token="t")
        with self.assertRaises(bg.ApiError):
            api.request("GET", "/user/me")
        self.assertFalse(api.session.calls[0][2]["allow_redirects"])


class StorageTests(unittest.TestCase):
    def test_create_and_reuse_without_regeneration(self):
        with local_temp() as directory:
            folder = Path(directory)
            accounts = [Account.from_key(i.to_bytes(32, "big")) for i in range(1, 101)]
            with patch.object(bg.Account, "create", side_effect=accounts) as create:
                result = bg.ensure_wallets(folder, 100)
                self.assertEqual(create.call_count, 100)
            self.assertEqual(len({w["address"] for w in result}), 100)
            original = (folder / "wallets.json").read_bytes()
            with patch.object(bg.Account, "create", side_effect=AssertionError("must reuse")):
                self.assertEqual(bg.ensure_wallets(folder, 100), result)
            self.assertEqual((folder / "wallets.json").read_bytes(), original)

    def test_corrupt_vault_is_never_replaced(self):
        with local_temp() as directory:
            folder = Path(directory)
            path = folder / "wallets.json"
            bg.write_json(path, {"version": 1, "wallets": [{**wallet(), "address": wallet(2)["address"]}]})
            before = path.read_bytes()
            with self.assertRaises(bg.ShapeError):
                bg.ensure_wallets(folder, 100)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_vault_with_existing_state_fails_closed(self):
        with local_temp() as directory:
            folder = Path(directory)
            bg.secure_directory(folder / "state")
            bg.write_json(folder / "state" / "existing.json", {})
            with self.assertRaises(bg.WalletPaused):
                bg.ensure_wallets(folder, 100)

    def test_init_only_does_not_use_network(self):
        with local_temp() as directory:
            account = Account.from_key((1).to_bytes(32, "big"))
            with patch.object(bg.Account, "create", return_value=account), patch.object(bg.requests, "Session") as session, patch.object(bg.signal, "signal"):
                result = bg.main(["--wallets", "1", "--wallet-file", str(Path(directory) / "wallet.json"), "--data-dir", directory, "--init-only"])
            self.assertEqual(result, 0)
            session.assert_not_called()

    def test_generation_uses_wallet_json_and_only_appends_to_target_total(self):
        with local_temp() as directory:
            root = Path(directory)
            folder = root / "data"
            bg.secure_directory(folder)
            path = root / "wallet.json"
            accounts = [Account.from_key(i.to_bytes(32, "big")) for i in range(1, 4)]
            with patch.object(bg.Account, "create", side_effect=accounts[:2]):
                first = bg.ensure_wallets(folder, 2, path)
            with patch.object(bg.Account, "create", return_value=accounts[2]) as create:
                larger = bg.ensure_wallets(folder, 3, path)
            self.assertEqual(larger[:2], first)
            self.assertEqual(len(larger), 3)
            create.assert_called_once_with()
            before = path.read_bytes()
            with self.assertRaises(bg.WalletPaused):
                bg.ensure_wallets(folder, 1, path)
            self.assertEqual(path.read_bytes(), before)

    def test_generation_migrates_legacy_keys_without_replacing_them(self):
        with local_temp() as directory:
            root = Path(directory)
            folder = root / "bull_gems_data"
            bg.secure_directory(folder)
            legacy = folder / "wallets.json"
            bg.write_json(legacy, {"version": 1, "wallets": [wallet(1), wallet(2)]})
            before = legacy.read_bytes()
            with patch.object(bg.Account, "create", side_effect=AssertionError("must reuse legacy keys")):
                result = bg.ensure_wallets(folder, 2, root / "wallet.json")
            self.assertEqual(result, [wallet(1), wallet(2)])
            self.assertEqual(bg.load_wallets(root / "wallet.json"), result)
            self.assertEqual(legacy.read_bytes(), before)

    def test_loading_wallets_is_read_only_and_never_generates(self):
        with local_temp() as directory:
            path = Path(directory) / "wallet.json"
            bg.write_json(path, {"version": 1, "wallets": [wallet(1), wallet(2)]})
            before = path.read_bytes()
            with patch.object(bg.Account, "create", side_effect=AssertionError("runtime must not generate")):
                self.assertEqual(bg.load_wallets(path), [wallet(1), wallet(2)])
            self.assertEqual(path.read_bytes(), before)

    def test_missing_wallet_file_is_not_created_on_read(self):
        with local_temp() as directory:
            path = Path(directory) / "wallet.json"
            with patch.object(bg.Account, "create") as create:
                with self.assertRaises(bg.WalletPaused):
                    bg.load_wallets(path)
            create.assert_not_called()
            self.assertFalse(path.exists())

    def test_same_directory_cannot_be_locked_twice(self):
        with local_temp() as directory:
            with bg.directory_lock(Path(directory)):
                with self.assertRaises(bg.WalletPaused):
                    with bg.directory_lock(Path(directory)):
                        pass


class LocationSelectionTests(unittest.TestCase):
    @staticmethod
    def location(identifier, level=1, power=0, cost=5, duration=60, **extra):
        return {"id": identifier, "levelRequired": level, "miningPowerRequired": power,
                "energyCost": cost, "durationSeconds": duration, **extra}

    def test_prefers_highest_eligible_level_not_id_or_cheapest_mine(self):
        starter = self.location("a-starter")
        middle = self.location("b-middle", level=4, power=25, cost=20, duration=1800)
        advanced = self.location("z-advanced", level=8, power=60, cost=35, duration=5400)
        selected = bg.choose_location([starter, advanced, middle], {"level": 8, "miningPower": 60}, None, 100)
        self.assertIs(selected, advanced)

    def test_level_and_power_must_both_meet_requirements(self):
        starter = self.location("starter")
        too_high_level = self.location("level-locked", level=9, power=0)
        too_high_power = self.location("power-locked", level=4, power=61)
        self.assertIs(bg.choose_location([too_high_level, too_high_power, starter], {"level": 8, "miningPower": 60}, None, 100), starter)

    def test_expensive_high_level_falls_back_to_affordable_eligible_mine(self):
        starter = self.location("starter")
        advanced = self.location("advanced", level=8, power=60, cost=35)
        self.assertIs(bg.choose_location([advanced, starter], {"level": 8, "miningPower": 60}, None, 10), starter)
        self.assertIs(bg.choose_location([advanced, starter], {"level": 8, "miningPower": 60}, None, 35), advanced)

    def test_locked_or_inactive_locations_are_excluded(self):
        starter = self.location("starter")
        for flag in ("isUnlocked", "unlocked", "isActive"):
            with self.subTest(flag=flag):
                blocked = self.location("blocked", level=4, **{flag: False})
                self.assertIs(bg.choose_location([blocked, starter], {"level": 8, "miningPower": 60}, None, 100), starter)

    def test_requested_location_does_not_bypass_eligibility_or_fall_back(self):
        starter = self.location("starter")
        requested = self.location("advanced", level=8, power=60, cost=35)
        cases = [({"level": 7, "miningPower": 60}, 100),
                 ({"level": 8, "miningPower": 59}, 100),
                 ({"level": 8, "miningPower": 60}, 34)]
        for me, energy in cases:
            with self.subTest(me=me, energy=energy):
                self.assertIsNone(bg.choose_location([starter, requested], me, "advanced", energy))
        self.assertIs(bg.choose_location([starter, requested], {"level": 8, "miningPower": 60}, "starter", 100), starter)
        self.assertIsNone(bg.choose_location([starter], {"level": 8, "miningPower": 60}, "missing", 100))

    def test_tie_breaks_by_power_then_cost_duration_and_id(self):
        me = {"level": 8, "miningPower": 60}
        cases = [
            (self.location("winner", power=25, cost=20), self.location("other", power=0, cost=5)),
            (self.location("winner", power=25, cost=10), self.location("other", power=25, cost=20)),
            (self.location("winner", power=25, duration=60), self.location("other", power=25, duration=600)),
            (self.location("a-winner", power=25), self.location("z-other", power=25)),
        ]
        for winner, other in cases:
            with self.subTest(winner=winner, other=other):
                self.assertIs(bg.choose_location([other, winner], me, None, 100), winner)

    def test_numeric_strings_and_duration_minutes_are_supported(self):
        mine = {"id": "minutes", "levelRequired": "4", "miningPowerRequired": "25", "energyCost": "20", "durationMinutes": "30"}
        self.assertIs(bg.choose_location([mine], {"level": "4", "miningPower": "25"}, None, 20), mine)

    def test_invalid_cost_or_duration_never_selects_a_location(self):
        for field in ("energyCost", "durationSeconds"):
            for value in (None, False, -1, 0, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    mine = self.location("invalid", **{field: value})
                    with self.assertRaises(bg.ShapeError):
                        bg.choose_location([mine], {"level": 8, "miningPower": 60}, None, 100)

    def test_empty_or_unaffordable_locations_return_none(self):
        me = {"level": 8, "miningPower": 60}
        self.assertIsNone(bg.choose_location([], me, None, 100))
        self.assertIsNone(bg.choose_location([self.location("starter")], me, None, 4))

    def test_unaffordable_location_missing_duration_does_not_block_affordable_mine(self):
        expensive = {"id": "expensive", "levelRequired": 8, "energyCost": 100}
        starter = self.location("starter")
        self.assertIs(bg.choose_location([expensive, starter], {"level": 8, "miningPower": 60}, None, 10), starter)


class FakeMiningApi:
    verify_referral = bg.Api.verify_referral

    def __init__(self, wallet, state):
        self.wallet = wallet
        self.state = state
        self.calls = []
        self.energy = 10
        self.mining_power = None
        self.me = {"referredBy": "yes", "level": 1, "energy": 10, "miningPower": 0}
        self.positions = []
        self.locations = [{"id": "starter", "energyCost": 5, "durationSeconds": 60}]
        self.quests = {"daily": [], "weekly": []}
        self.timeout_open = False
        self.open_attempts = 0
        self.session = Mock()

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            stats = {"energy": self.energy}
            if self.mining_power is not None:
                stats["miningPower"] = self.mining_power
            return {
                "/user/me": self.me,
                "/mining/positions": list(self.positions),
                "/mining/stats": stats,
                "/mining/locations": self.locations,
                "/quests": self.quests,
            }[path]
        if path == "/mining/positions":
            self.open_attempts += 1
            if self.timeout_open:
                raise requests.Timeout()
            self.positions.append({"id": f"new-{self.open_attempts}", "status": "active"})
        elif path.startswith("/mining/positions/") and path.endswith("/claim"):
            position_id = path.split("/")[-2]
            self.positions = [p for p in self.positions if p["id"] != position_id]
        return {"delaySeconds": 10}


class MinerTests(unittest.TestCase):
    def setUp(self):
        self.temp = local_temp()
        folder = Path(self.temp.name)
        bg.secure_directory(folder / "state")
        args = SimpleNamespace(ref_code=bg.REF_CODE, interval=15, skip_quests=True, skip_upgrades=True, location_id=None)
        self.miner = bg.Miner(wallet(), folder, Gate(), args)
        self.miner.api.session.close()
        self.api = self.miner.api = FakeMiningApi(self.miner.wallet, self.miner.state)

    def tearDown(self):
        self.temp.cleanup()

    def test_claims_ready_before_opening(self):
        self.api.positions = [{"id": "ready1", "status": "ready"}, {"id": "active1", "status": "active"}]
        self.miner.tick()
        paths = [(m, p) for m, p, _ in self.api.calls]
        claim = paths.index(("POST", "/mining/positions/ready1/claim"))
        deploy = paths.index(("POST", "/mining/positions"))
        self.assertLess(claim, deploy)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertNotIn("pending_open", self.miner.state)

    def test_default_has_no_five_position_limit(self):
        for occupied in (5, 10):
            with self.subTest(occupied=occupied):
                self.api.positions = [{"id": str(i), "status": "active"} for i in range(occupied)]
                before = self.api.open_attempts
                self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.api.open_attempts, before + 1)

    def test_explicit_position_limit_blocks_new_opening(self):
        self.miner.args.max_positions = 5
        self.api.positions = [{"id": str(i), "status": "active"} for i in range(5)]
        with self.assertLogs(bg.LOG, level="INFO") as logs:
            self.assertEqual(self.miner.tick(), 15)
        self.assertEqual(self.api.open_attempts, 0)
        snapshot = next(line for line in logs.output if "mining check:" in line)
        self.assertIn("limit=5", snapshot)
        self.assertIn("positions_full", snapshot)

    def test_explicit_position_limit_still_claims_ready_positions_first(self):
        self.miner.args.max_positions = 5
        self.api.positions = [{"id": str(i), "status": "active"} for i in range(5)] + [{"id": "ready1", "status": "ready"}]
        self.assertEqual(self.miner.tick(), 15)
        self.assertIn(("POST", "/mining/positions/ready1/claim", None), self.api.calls)
        self.assertEqual(self.api.open_attempts, 0)

    def test_live_stats_energy_takes_precedence(self):
        self.api.me["energy"] = 100
        self.api.energy = 4
        self.assertEqual(self.miner.tick(), 15)
        self.assertEqual(self.api.open_attempts, 0)

    def test_eligible_mine_can_cost_more_and_take_longer_than_original_entry_mine(self):
        self.api.locations = [{"id": "quarry", "energyCost": 10, "durationSeconds": 600}]
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertIn(("POST", "/mining/positions", {"locationId": "quarry"}), self.api.calls)

    def test_selects_highest_eligible_level_using_live_stats_power(self):
        self.api.me.update({"level": 8, "miningPower": 0})
        self.api.mining_power = 60
        self.api.energy = 100
        self.api.locations = [LocationSelectionTests.location("starter"), LocationSelectionTests.location("advanced", level=8, power=60, cost=35, duration=5400)]
        with self.assertLogs(bg.LOG, level="INFO") as logs:
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertIn(("POST", "/mining/positions", {"locationId": "advanced"}), self.api.calls)
        deployed = next(line for line in logs.output if "miner deployed" in line)
        for expected in ("location=advanced", "level_required=8", "power_required=60", "cost=35", "duration=5400", "energy_before=100"):
            self.assertIn(expected, deployed)

    def test_live_stats_zero_power_does_not_fall_back_to_higher_profile_power(self):
        self.api.me.update({"level": 8, "miningPower": 60})
        self.api.mining_power = 0
        self.api.energy = 100
        self.api.locations = [LocationSelectionTests.location("advanced", level=8, power=60, cost=35)]
        self.assertEqual(self.miner.tick(), 15)
        self.assertEqual(self.api.open_attempts, 0)

    def test_level_change_refreshes_cached_locations_and_selects_newly_unlocked_mine(self):
        self.api.energy = 100
        self.api.me.update({"level": 1, "miningPower": 60})
        self.api.locations = [LocationSelectionTests.location("starter"), LocationSelectionTests.location("advanced", level=8, power=60, cost=35, isUnlocked=False)]
        self.assertEqual(self.miner.tick(), 0.5)
        self.api.me["level"] = 8
        self.api.locations = [LocationSelectionTests.location("starter"), LocationSelectionTests.location("advanced", level=8, power=60, cost=35, isUnlocked=True)]
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(sum(path == "/mining/locations" for method, path, _ in self.api.calls if method == "GET"), 2)
        self.assertEqual([body["locationId"] for method, path, body in self.api.calls if method == "POST" and path == "/mining/positions"], ["starter", "advanced"])

    def test_power_change_refreshes_cached_locations_and_selects_newly_eligible_mine(self):
        self.api.energy = 100
        self.api.me["level"] = 8
        self.api.mining_power = 0
        self.api.locations = [LocationSelectionTests.location("starter"), LocationSelectionTests.location("advanced", level=8, power=60, cost=35, isUnlocked=False)]
        self.assertEqual(self.miner.tick(), 0.5)
        self.api.mining_power = 60
        self.api.locations = [LocationSelectionTests.location("starter"), LocationSelectionTests.location("advanced", level=8, power=60, cost=35, isUnlocked=True)]
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(sum(path == "/mining/locations" for method, path, _ in self.api.calls if method == "GET"), 2)
        self.assertEqual([body["locationId"] for method, path, body in self.api.calls if method == "POST" and path == "/mining/positions"], ["starter", "advanced"])

    def test_unchanged_profile_reuses_location_cache_until_five_minute_expiry(self):
        with patch.object(bg.time, "time", return_value=1000):
            self.assertEqual(self.miner.tick(), 0.5)
        with patch.object(bg.time, "time", return_value=1010):
            self.assertEqual(self.miner.tick(), 0.5)
        calls_before_expiry = sum(path == "/mining/locations" for method, path, _ in self.api.calls if method == "GET")
        self.assertEqual(calls_before_expiry, 1)
        with patch.object(bg.time, "time", return_value=1301):
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(sum(path == "/mining/locations" for method, path, _ in self.api.calls if method == "GET"), 2)

    def test_requested_unaffordable_mine_does_not_fall_back_to_cheaper_mine(self):
        self.api.me.update({"level": 8, "miningPower": 60})
        self.api.energy = 10
        self.api.locations = [LocationSelectionTests.location("starter"), LocationSelectionTests.location("advanced", level=8, power=60, cost=35)]
        self.miner.args.location_id = "advanced"
        self.assertEqual(self.miner.tick(), 15)
        self.assertEqual(self.api.open_attempts, 0)

    def test_unrecognized_status_prevents_mutation(self):
        self.api.positions = [{"id": "p", "status": "unexpected"}]
        self.assertGreaterEqual(self.miner.tick(), 300)
        self.assertIn("last_error", self.miner.state)
        self.assertFalse(any(m == "POST" for m, _, _ in self.api.calls))

    def test_empty_referral_does_not_block_mining(self):
        self.api.me["referredBy"] = None
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertEqual(self.miner.state["referral_status"], "empty")
        self.assertNotIn("retry_at", self.miner.state)

    def test_read_timeout_log_identifies_endpoint_without_exposing_exception_body(self):
        self.miner.state["token"] = "TEST-TOKEN-DO-NOT-LOG"
        api = bg.Api(self.miner.wallet, self.miner.state, self.miner.save, Gate(), bg.REF_CODE)
        api.session.close()
        api.session = Session([requests.ReadTimeout("SECRET-DO-NOT-LOG")])
        self.miner.api = api
        with self.assertLogs(bg.LOG, level="WARNING") as logs:
            delay = self.miner.tick()
        output = "\n".join(logs.output)
        self.assertGreaterEqual(delay, 15)
        self.assertIn("ReadTimeout", output)
        self.assertIn("GET /user/me", output)
        self.assertIn("elapsed=", output)
        self.assertNotIn("SECRET-DO-NOT-LOG", output)
        self.assertNotIn("TEST-TOKEN-DO-NOT-LOG", output)
        self.assertEqual(len(api.session.calls), 1)

    def test_idle_mining_status_is_printed_periodically_without_extra_requests(self):
        self.api.energy = 0
        with patch.object(bg.time, "monotonic", side_effect=[100, 101, 401]), self.assertLogs(bg.LOG, level="INFO") as logs:
            for _ in range(3):
                self.assertEqual(self.miner.tick(), 15)
        snapshots = [line for line in logs.output if "mining check:" in line]
        self.assertEqual(len(snapshots), 2)
        self.assertIn("energy=0", snapshots[0])
        self.assertIn("occupied=0", snapshots[0])
        self.assertIn("limit=none", snapshots[0])
        self.assertNotIn("occupied=0/5", snapshots[0])
        self.assertIn("waiting_energy", snapshots[0])
        self.assertEqual(len(self.api.calls), 9)

    def test_successful_cycle_resets_timeout_backoff(self):
        with patch.object(self.miner, "cycle", side_effect=[requests.ReadTimeout(), 15, requests.ReadTimeout()]), patch.object(bg.random, "uniform", return_value=0):
            self.assertEqual(self.miner.tick(), 15)
            self.assertEqual(self.miner.tick(), 15)
            self.assertEqual(self.miner.tick(), 15)
        self.assertEqual(self.miner.failures, 1)

    def test_omitted_referral_does_not_block_quests_or_mining(self):
        self.api.me.pop("referredBy")
        self.miner.args.skip_quests = False
        self.api.quests = {"daily": [{"id": "daily_miner_signin", "status": "ready"}]}
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertIn(("POST", "/quests/claim", {"questId": "daily_miner_signin", "tweetUrl": ""}), self.api.calls)
        self.assertEqual(self.miner.state["referral_status"], "missing")
        self.assertNotIn("retry_at", self.miner.state)

    def test_old_referral_pause_automatically_resumes_mining(self):
        self.api.me["referredBy"] = None
        self.miner.state.update({"token": "cached-token", "paused_reason": "referredBy is empty", "last_error": "referredBy is empty", "retry_at": 9999999999})
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertEqual(self.miner.state["token"], "cached-token")
        self.assertNotIn("paused_reason", self.miner.state)
        self.assertNotIn("last_error", self.miner.state)
        self.assertNotIn("retry_at", self.miner.state)

    def test_unexpected_exception_is_retried_without_logging_secrets(self):
        with patch.object(self.miner, "cycle", side_effect=[ValueError("SECRET-DO-NOT-LOG"), 15]), self.assertLogs(bg.LOG, level="WARNING") as logs:
            self.assertGreaterEqual(self.miner.tick(), 15)
            self.assertEqual(self.miner.tick(), 15)
        self.assertNotIn("SECRET-DO-NOT-LOG", "\n".join(logs.output))
        self.assertNotIn("last_error", self.miner.state)
        self.assertEqual(self.miner.failures, 0)

    def test_state_save_failure_does_not_escape_tick(self):
        with patch.object(self.miner, "cycle", side_effect=RuntimeError()), patch.object(self.miner, "save", side_effect=OSError()):
            self.assertGreaterEqual(self.miner.tick(), 15)

    def test_failed_predeployment_save_never_sends_post_or_leaves_false_pending(self):
        with patch.object(self.miner, "save", side_effect=[OSError(), None]):
            self.assertGreaterEqual(self.miner.tick(), 15)
        self.assertEqual(self.api.open_attempts, 0)
        self.assertNotIn("pending_open", self.miner.state)
        self.miner.tick()
        self.assertEqual(self.api.open_attempts, 1)

    def test_http_403_cools_down_instead_of_permanent_pause(self):
        with patch.object(self.miner, "cycle", side_effect=[bg.ApiError(403), 15]):
            self.assertGreaterEqual(self.miner.tick(), 300)
            self.assertEqual(self.miner.tick(), 15)
        self.assertNotIn("paused_reason", self.miner.state)

    def test_old_paused_state_is_retried_automatically(self):
        self.miner.state["paused_reason"] = "old version pause"
        with patch.object(self.miner, "cycle", return_value=15) as cycle:
            self.assertEqual(self.miner.tick(), 15)
        cycle.assert_called_once_with()
        self.assertNotIn("paused_reason", self.miner.state)

    def test_corrupt_state_is_preserved_and_reloaded_after_repair(self):
        self.miner.path.write_text("{broken", encoding="utf-8")
        replacement = bg.Miner(wallet(), self.miner.path.parent.parent, Gate(), self.miner.args)
        try:
            self.assertFalse(replacement.state_loaded)
            self.assertGreaterEqual(replacement.tick(), 15)
            self.assertEqual(self.miner.path.read_text(encoding="utf-8"), "{broken")
            bg.write_json(self.miner.path, {"token": "repaired"})
            with patch.object(replacement, "cycle", return_value=15):
                self.assertEqual(replacement.tick(), 15)
            self.assertTrue(replacement.state_loaded)
            self.assertEqual(replacement.api.state["token"], "repaired")
        finally:
            replacement.api.session.close()

    def test_uncertain_deployment_is_not_blindly_repeated(self):
        self.api.timeout_open = True
        self.miner.tick()
        self.assertIn("pending_open", self.miner.state)
        self.miner.tick()
        self.assertEqual(self.api.open_attempts, 1)
        # Later a server read proves the original deployment was accepted.
        self.api.positions = [{"id": "arrived-later", "status": "active"}]
        self.api.energy = 0
        self.miner.tick()
        self.assertNotIn("pending_open", self.miner.state)
        self.assertEqual(self.api.open_attempts, 1)

    def test_quest_timer_and_social_skip(self):
        self.miner.args.skip_quests = False
        self.api.quests = {
            "daily": [{"id": "daily_miner_signin", "status": "available"}, {"id": "twitter_task", "status": "available"}],
            "weekly": [{"id": "weekly_miner_checkin", "status": "available"}],
        }
        with patch.object(bg.time, "time", return_value=1000):
            self.assertFalse(self.miner.quests())
        posts = [(p, b) for m, p, b in self.api.calls if m == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(p == "/quests/start" for p, _ in posts))
        with patch.object(bg.time, "time", return_value=1009):
            self.assertFalse(self.miner.quests())
        self.assertEqual(len(self.api.calls), 3)
        for group in ("daily", "weekly"):
            for quest in self.api.quests[group]:
                quest["status"] = "ready"
        with patch.object(bg.time, "time", return_value=1011):
            self.assertTrue(self.miner.quests())
        claims = [b for m, p, b in self.api.calls if m == "POST" and p == "/quests/claim"]
        self.assertEqual({b["questId"] for b in claims}, bg.QUEST_IDS)
        self.assertTrue(all(b["tweetUrl"] == "" for b in claims))

    def test_whitelisted_quest_with_twitter_platform_is_skipped(self):
        self.miner.args.skip_quests = False
        self.api.quests = {"daily": [{"id": "daily_miner_signin", "status": "ready", "platform": "twitter"}]}
        self.miner.quests()
        self.assertFalse(any(m == "POST" for m, _, _ in self.api.calls))

    def test_skip_quests_does_not_busy_poll_old_timer(self):
        self.miner.state["quest_check_at"] = 1
        self.api.energy = 0
        self.assertEqual(self.miner.tick(), 15)


class SchedulerTests(unittest.TestCase):
    def test_stop_interrupts_a_long_global_rate_limit_wait(self):
        stop = threading.Event()
        request_stop = threading.Event()
        entered = threading.Event()
        gate = bg.RequestGate(0, request_stop)
        gate.pause(3600)

        def tick():
            entered.set()
            try:
                gate.enter()
            except bg.Stopped:
                return None
            raise AssertionError("Rate limit should not have expired")

        miner = SimpleNamespace(tick=tick, api=SimpleNamespace(session=Mock()))
        runner = threading.Thread(target=bg.run_miners, args=([miner], 1, stop, request_stop))
        runner.start()
        try:
            self.assertTrue(entered.wait(2))
        finally:
            stop.set()
            runner.join(3)
        self.assertFalse(runner.is_alive())
        self.assertTrue(request_stop.is_set())

    def test_escaped_worker_exception_is_rescheduled_without_global_shutdown(self):
        stop = threading.Event()
        request_stop = threading.Event()
        miner = SimpleNamespace(tick=Mock(side_effect=[RuntimeError("hidden"), None]), api=SimpleNamespace(session=Mock()))
        with patch.object(bg.time, "monotonic", side_effect=itertools.count(0, 31)):
            bg.run_miners([miner], 1, stop, request_stop)
        self.assertEqual(miner.tick.call_count, 2)
        self.assertFalse(stop.is_set())
        self.assertTrue(request_stop.is_set())

    def test_wallets_are_not_ticked_concurrently(self):
        stop = threading.Event()

        class Miner:
            def __init__(self):
                self.count = 0
                self.active = False
                self.api = SimpleNamespace(session=Mock())

            def tick(self):
                if self.active:
                    raise AssertionError("same wallet running concurrently")
                self.active = True
                self.count += 1
                self.active = False
                return 0 if self.count < 3 else None

        miners = [Miner() for _ in range(5)]
        bg.run_miners(miners, 2, stop)
        self.assertEqual([m.count for m in miners], [3] * 5)
        self.assertTrue(all(m.api.session.close.called for m in miners))


class RuntimeTests(unittest.TestCase):
    def test_cli_position_limit_defaults_to_none_and_accepts_positive_integer(self):
        for arguments, expected in (([], None), (["--max-positions", "17"], 17)):
            with self.subTest(arguments=arguments):
                captured = []

                def run_client(args, stop):
                    captured.append(args.max_positions)
                    stop.set()
                    return 0

                with patch.object(bg.signal, "signal"), patch.object(bg, "run_client", side_effect=run_client):
                    self.assertEqual(bg.main(arguments), 0)
                self.assertEqual(captured, [expected])

    def test_cli_rejects_nonpositive_or_noninteger_position_limits(self):
        with patch.object(bg, "run_client") as run:
            for value in ("0", "-1", "1.5", "abc"):
                with self.subTest(value=value), self.assertRaises(SystemExit) as raised:
                    bg.main(["--max-positions", value])
                self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_terminal_signals_stop_runtime_without_retry_or_generation(self):
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            if not hasattr(bg.signal, name):
                continue
            with self.subTest(signal=name):
                handlers = {}
                stop = threading.Event()

                def register(signum, handler):
                    handlers[signum] = handler

                def receive_signal(args, event):
                    self.assertFalse(event.is_set())
                    handlers[getattr(bg.signal, name)](getattr(bg.signal, name), None)
                    self.assertTrue(event.is_set())
                    return 0

                with patch.object(bg.threading, "Event", return_value=stop), patch.object(bg.signal, "signal", side_effect=register), patch.object(bg, "run_client", side_effect=receive_signal) as run, patch.object(stop, "wait") as wait, patch.object(bg.Account, "create") as create:
                    self.assertEqual(bg.main([]), 0)
                run.assert_called_once()
                wait.assert_not_called()
                create.assert_not_called()

    def test_runtime_loads_exact_file_count_without_modifying_or_generating(self):
        with local_temp() as directory:
            root = Path(directory)
            path = root / "wallet.json"
            bg.write_json(path, {"version": 1, "wallets": [wallet(1), wallet(2)]})
            before = path.read_bytes()
            args = SimpleNamespace(data_dir=root / "data", clear_pending=None, init_only=False, wallet_file=path, request_interval=0.25, workers=3)
            with patch.object(bg.Account, "create") as create, patch.object(bg, "ensure_wallets", side_effect=AssertionError("runtime must never enter generation")), patch.object(bg.requests, "Session") as session, patch.object(bg, "Miner", side_effect=lambda wallet, *args: SimpleNamespace(wallet=wallet)), patch.object(bg, "run_miners") as run:
                self.assertEqual(bg.run_client(args, threading.Event()), 0)
            self.assertEqual([miner.wallet for miner in run.call_args.args[0]], [wallet(1), wallet(2)])
            create.assert_not_called()
            session.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_runtime_missing_file_never_falls_back_to_generation(self):
        with local_temp() as directory:
            root = Path(directory)
            path = root / "wallet.json"
            bg.secure_directory(root / "data")
            bg.write_json(root / "data" / "wallets.json", {"version": 1, "wallets": [wallet(1)]})
            args = SimpleNamespace(data_dir=root / "data", clear_pending=None, init_only=False, wallet_file=path)
            with patch.object(bg.Account, "create") as create, patch.object(bg.requests, "Session") as session:
                with self.assertRaises(bg.WalletPaused):
                    bg.run_client(args, threading.Event())
            create.assert_not_called()
            session.assert_not_called()
            self.assertFalse(path.exists())

    def test_closed_window_logging_is_discarded_without_a_file(self):
        stream = Mock()
        stream.write.side_effect = BrokenPipeError()
        handler = bg.WindowLogHandler(stream)
        record = logging.LogRecord("test", logging.INFO, "", 1, "hello", (), None)
        handler.emit(record)
        handler.emit(record)
        self.assertTrue(handler.window_closed)
        self.assertEqual(stream.write.call_count, 1)
        self.assertNotIsInstance(handler, logging.FileHandler)

    def test_main_retries_bootstrap_error_until_explicit_shutdown(self):
        stop = threading.Event()
        attempts = []

        def run_client(args, event):
            attempts.append(args)
            if len(attempts) == 1:
                raise RuntimeError("SECRET-DO-NOT-LOG")
            event.set()
            return 0

        with patch.object(bg.threading, "Event", return_value=stop), patch.object(stop, "wait", return_value=False), patch.object(bg.signal, "signal"), patch.object(bg, "run_client", side_effect=run_client), self.assertLogs(bg.LOG, level="ERROR") as logs:
            self.assertEqual(bg.main([]), 0)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("SECRET-DO-NOT-LOG", "\n".join(logs.output))

    def test_retrying_corrupt_vault_never_regenerates_keys(self):
        with local_temp() as directory:
            path = Path(directory) / "wallet.json"
            path.write_text("{broken", encoding="utf-8")
            stop = threading.Event()

            def stop_waiting(seconds):
                stop.set()
                return True

            with patch.object(bg.threading, "Event", return_value=stop), patch.object(stop, "wait", side_effect=stop_waiting), patch.object(bg.signal, "signal"), patch.object(bg.Account, "create") as create, patch.object(bg.requests, "Session") as session:
                self.assertEqual(bg.main(["--data-dir", directory, "--wallet-file", str(path)]), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
            create.assert_not_called()
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
