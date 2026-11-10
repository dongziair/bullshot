"""Offline resource-to-GEMS swap tests; deterministic public fixtures only."""

import unittest
import time
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import bull_gems as bg
import test_auto_upgrades as upgrade_fixtures
import test_bull_gems as fixtures


class SwapApi(upgrade_fixtures.UpgradeApi):
    def __init__(self, wallet, state):
        super().__init__(wallet, state)
        self.resource_id = sorted(bg.SWAP_RESOURCE_IDS)[0]
        self.rates = {"assetValues": {self.resource_id: 2}}
        self.inventory = [{"assetId": self.resource_id, "quantity": 3.5, "name": "Test ore"}]
        self.rates_error = None
        self.inventory_error = None
        self.swap_error = None
        self.swap_result = {"received": 6, "toAsset": "GEMS"}
        self.swap_attempts = 0
        self.apply_swap = True
        self.apply_swap_on_error = False
        self.claim_resource_reward = 0

    def inventory_records(self):
        value = bg.unwrap(self.inventory)
        return value["inventory"] if isinstance(value, dict) else value

    def request(self, method, path, body=None):
        if method == "GET" and path in {"/trade/rates", "/user/inventory"}:
            self.calls.append((method, path, body))
            error = self.rates_error if path == "/trade/rates" else self.inventory_error
            if error is not None:
                raise error
            if path == "/trade/rates":
                return self.rates
            value = self.inventory
            if isinstance(value, list):
                return [dict(item) for item in value]
            return value
        if method == "POST" and path == "/trade/swap":
            self.calls.append((method, path, body))
            self.swap_attempts += 1
            if self.apply_swap and (self.swap_error is None or self.apply_swap_on_error):
                record = next(item for item in self.inventory_records()
                              if (item.get("assetId") or item.get("asset_id")) == body["fromAssetId"])
                record["quantity"] = str(Decimal(str(record["quantity"])) - body["amount"])
                rate = Decimal(str(bg.unwrap(self.rates)["assetValues"][body["fromAssetId"]]))
                self.balance["gems"] += float(body["amount"] * rate)
            if self.swap_error is not None:
                raise self.swap_error
            return self.swap_result
        if method == "POST" and path.startswith("/mining/positions/") and path.endswith("/claim"):
            result = super().request(method, path, body)
            if self.claim_resource_reward:
                record = next(item for item in self.inventory_records()
                              if (item.get("assetId") or item.get("asset_id")) == self.resource_id)
                record["quantity"] = str(Decimal(str(record["quantity"])) + self.claim_resource_reward)
            return result
        return super().request(method, path, body)


class AutoSwapTests(unittest.TestCase):
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
                               auto_upgrades=False, skip_upgrades=False, skip_swap=False,
                               swap_gems=False, location_id=None, max_positions=None)
        self.miner = bg.Miner(fixtures.wallet(), self.root, fixtures.Gate(), args)
        self.miner.api.session.close()
        self.api = self.miner.api = SwapApi(self.miner.wallet, self.miner.state)
        self.resource_id = self.api.resource_id

    def tearDown(self):
        self.api.session.close()
        self.temporary.cleanup()

    def swap_posts(self):
        return [(method, path, body) for method, path, body in self.api.calls
                if method == "POST" and path == "/trade/swap"]

    def reset_swap_state(self):
        self.miner.state.clear()
        self.api.calls.clear()
        self.api.swap_attempts = 0
        self.api.rates_error = None
        self.api.inventory_error = None
        self.api.swap_error = None
        self.api.apply_swap = True
        self.api.apply_swap_on_error = False
        self.api.claim_resource_reward = 0

    def test_default_cycle_swaps_resources_to_gems_without_buying_upgrades(self):
        del self.miner.args.skip_swap
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.swap_posts(), [("POST", "/trade/swap",
                                             {"fromAssetId": self.resource_id, "toAssetId": "GEMS", "amount": 3})])
        self.assertEqual(self.api.balance["gems"], 106)
        self.assertEqual(self.api.upgrade_attempts, 0)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertFalse(any(path == "/upgrades" or path.startswith("/upgrades/")
                             for _, path, _ in self.api.calls))

    def test_skip_swap_avoids_trade_requests_without_blocking_mining(self):
        self.miner.args.skip_swap = True
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.swap_attempts, 0)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertFalse(any(path in {"/trade/rates", "/user/inventory", "/trade/swap"}
                             for _, path, _ in self.api.calls))

    def test_asset_id_alias_and_primary_field_are_supported(self):
        for record in ({"asset_id": self.resource_id, "quantity": 3.5},
                       {"assetId": self.resource_id, "asset_id": "not-the-primary-id", "quantity": 3.5}):
            with self.subTest(record=record):
                self.reset_swap_state()
                self.api.inventory = [record.copy()]
                self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.swap_posts(), [("POST", "/trade/swap",
                                                     {"fromAssetId": self.resource_id, "toAssetId": "GEMS", "amount": 3})])

    def test_gems_or_inventory_without_a_rate_is_not_swapped(self):
        self.api.inventory = [{"assetId": "GEMS", "quantity": 100},
                              {"assetId": "unknown-resource", "quantity": 5}]
        self.api.rates["assetValues"]["GEMS"] = 1
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.swap_attempts, 0)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertEqual(self.api.balance["gems"], 100)

    def test_all_ten_supported_resources_are_swappable_by_default(self):
        expected = {"iron-ore", "coal", "copper-ore", "sulfur", "cryo-shard", "void-dust",
                    "steel-ingot", "bronze-wire", "thermal-core", "void-reactor"}
        self.assertEqual(bg.SWAP_RESOURCE_IDS, expected)
        for asset_id in sorted(expected):
            with self.subTest(asset_id=asset_id):
                self.reset_swap_state()
                self.api.inventory = [{"assetId": asset_id, "quantity": 1}]
                self.api.rates = {"assetValues": {asset_id: 1}}
                self.assertTrue(self.miner.auto_swap())
                self.assertEqual(self.swap_posts()[0][2],
                                 {"fromAssetId": asset_id, "toAssetId": "GEMS", "amount": 1})

    def test_six_gem_assets_require_explicit_opt_in(self):
        expected = {"emerald", "sapphire", "ruby", "diamond", "obsidian", "void-gem"}
        self.assertEqual(bg.SWAP_GEM_IDS, expected)
        for asset_id in sorted(expected):
            with self.subTest(asset_id=asset_id):
                self.reset_swap_state()
                self.miner.args.swap_gems = False
                self.api.inventory = [{"assetId": asset_id, "quantity": 1}]
                self.api.rates = {"assetValues": {asset_id: 10}}
                self.assertFalse(self.miner.auto_swap())
                self.assertEqual(self.api.swap_attempts, 0)
                self.miner.args.swap_gems = True
                self.assertTrue(self.miner.auto_swap(force=True))
                self.assertEqual(self.swap_posts()[0][2],
                                 {"fromAssetId": asset_id, "toAssetId": "GEMS", "amount": 1})

    def test_gems_and_unknown_assets_are_kept_even_if_priced_and_gem_opt_in_is_enabled(self):
        self.miner.args.swap_gems = True
        self.api.inventory = [{"assetId": "GEMS", "quantity": 100},
                              {"assetId": "unknown-resource", "quantity": 5}]
        self.api.rates = {"assetValues": {"GEMS": 1, "unknown-resource": 9999}}
        self.assertFalse(self.miner.auto_swap())
        self.assertEqual(self.api.swap_attempts, 0)

    def test_missing_api_quote_never_uses_a_hardcoded_price(self):
        self.api.rates = {"assetValues": {}}
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.swap_attempts, 0)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertEqual(self.api.inventory[0]["quantity"], 3.5)

    def test_nonpositive_or_invalid_quantity_never_swaps_and_still_mines(self):
        for quantity in (None, False, 0, 0.99, -1, float("nan"), float("inf"), "invalid"):
            with self.subTest(quantity=quantity):
                self.reset_swap_state()
                self.api.inventory = [{"assetId": self.resource_id, "quantity": quantity}]
                before = self.api.open_attempts
                self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.api.swap_attempts, 0)
                self.assertEqual(self.api.open_attempts, before + 1)

    def test_nonpositive_or_invalid_rate_never_swaps_and_still_mines(self):
        for rate in (None, False, 0, -1, float("nan"), float("inf"), "invalid"):
            with self.subTest(rate=rate):
                self.reset_swap_state()
                self.api.rates = {"assetValues": {self.resource_id: rate}}
                before = self.api.open_attempts
                self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.api.swap_attempts, 0)
                self.assertEqual(self.api.open_attempts, before + 1)

    def test_rates_and_inventory_errors_only_defer_swap_not_mining(self):
        for field, error in (("rates_error", requests.ReadTimeout()),
                             ("inventory_error", bg.ApiError(503))):
            with self.subTest(field=field):
                self.reset_swap_state()
                setattr(self.api, field, error)
                before = self.api.open_attempts
                self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.api.swap_attempts, 0)
                self.assertEqual(self.api.open_attempts, before + 1)
                self.assertNotIn("last_error", self.miner.state)
                self.assertNotIn("retry_at", self.miner.state)

    def test_uncertain_swap_result_never_blindly_reposts(self):
        errors = (requests.ReadTimeout("PRIVATE-SWAP-DETAIL"), bg.ApiError(503),
                  bg.ShapeError("API returned non-JSON success response"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.reset_swap_state()
                self.api.inventory = [{"assetId": self.resource_id, "quantity": 3.5}]
                self.api.apply_swap = False
                self.api.swap_error = error
                before = self.api.open_attempts
                with patch.object(bg.time, "time", return_value=1000):
                    with self.assertLogs(bg.LOG, level="WARNING") as logs:
                        self.assertEqual(self.miner.tick(), 0.5)
                self.assertIn("pending_swap", self.miner.state)
                self.assertIn("pending_swap", bg.read_json(self.miner.path))
                self.assertGreaterEqual(self.miner.state["swap_retry_at"], 1300)
                self.assertEqual(self.miner.state["swap_check_at"], 0)
                self.api.swap_error = None
                with patch.object(bg.time, "time", return_value=2000):
                    for _ in range(3):
                        self.assertEqual(self.miner.tick(), 0.5)
                self.assertEqual(self.api.swap_attempts, 1)
                self.assertEqual(self.api.open_attempts, before + 4)
                self.assertNotIn("PRIVATE-SWAP-DETAIL", "\n".join(logs.output))

    def test_post_lost_response_is_reconciled_from_inventory_without_repeat_swap(self):
        self.api.swap_error = requests.ReadTimeout()
        self.api.apply_swap_on_error = True
        with patch.object(bg.time, "time", return_value=1000):
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertIn("pending_swap", self.miner.state)
        self.api.swap_error = None
        with patch.object(bg.time, "time", return_value=2000):
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.swap_attempts, 1)
        self.assertEqual(self.api.open_attempts, 2)
        self.assertNotIn("pending_swap", self.miner.state)
        self.assertEqual(self.api.balance["gems"], 106)

    def assert_pending_readback_precedes_new_resource_claim(self, lose_response):
        self.api.inventory[0]["quantity"] = 10
        self.api.swap_result = {"received": 20, "toAsset": "GEMS"}
        if lose_response:
            self.api.swap_error = requests.ReadTimeout()
            self.api.apply_swap_on_error = True
        with patch.object(bg.time, "time", return_value=1000):
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.swap_attempts, 1)
        self.assertEqual(Decimal(self.api.inventory[0]["quantity"]), 0)
        self.assertIn("pending_swap", self.miner.state)
        self.assertEqual(Decimal(self.miner.state["pending_swap"]["quantity_before"]), 10)
        self.api.swap_error = None
        self.api.claim_resource_reward = 1
        self.api.positions = [{"id": "new-resource-reward", "status": "ready"}]
        self.api.calls.clear()
        with patch.object(bg.time, "time", return_value=1001):
            self.assertEqual(self.miner.tick(), 0.5)
        paths = [(method, path) for method, path, _ in self.api.calls]
        readback = paths.index(("GET", "/user/inventory"))
        claim = paths.index(("POST", "/mining/positions/new-resource-reward/claim"))
        self.assertLess(readback, claim)
        new_swaps = self.swap_posts()
        self.assertEqual(len(new_swaps), 1)
        self.assertEqual(new_swaps[0][2],
                         {"fromAssetId": self.resource_id, "toAssetId": "GEMS", "amount": 1})
        self.assertGreater(paths.index(("POST", "/trade/swap")), claim)
        self.assertEqual(self.api.swap_attempts, 2)
        self.assertEqual(self.miner.state["pending_swap"]["amount"], 1)
        self.assertEqual(Decimal(self.miner.state["pending_swap"]["quantity_before"]), 1)
        self.assertEqual(self.api.open_attempts, 2)

    def test_acknowledged_swap_is_confirmed_before_claim_adds_same_resource(self):
        self.assert_pending_readback_precedes_new_resource_claim(lose_response=False)

    def test_lost_swap_response_is_read_back_before_claim_adds_same_resource(self):
        self.assert_pending_readback_precedes_new_resource_claim(lose_response=True)

    def test_pending_swap_is_also_read_back_before_quest_rewards(self):
        self.api.inventory[0]["quantity"] = 10
        with patch.object(bg.time, "time", return_value=1000):
            self.assertEqual(self.miner.tick(), 0.5)
        self.api.calls.clear()

        def quests():
            self.api.calls.append(("POST", "/quests/claim", {"questId": "test-reward"}))
            self.api.inventory[0]["quantity"] = "1"
            return True

        with patch.object(self.miner, "quests", side_effect=quests), \
                patch.object(bg.time, "time", return_value=1001):
            self.assertEqual(self.miner.tick(), 0.5)
        paths = [(method, path) for method, path, _ in self.api.calls]
        self.assertLess(paths.index(("GET", "/user/inventory")), paths.index(("POST", "/quests/claim")))
        self.assertEqual(self.miner.state["pending_swap"]["amount"], 1)
        self.assertEqual(Decimal(self.miner.state["pending_swap"]["quantity_before"]), 1)

    def test_uncertain_post_allows_early_read_but_failed_readback_cools_down(self):
        self.api.swap_error = requests.ReadTimeout()
        self.api.apply_swap = False
        with patch.object(bg.time, "time", return_value=1000):
            self.assertFalse(self.miner.auto_swap())
        self.assertGreaterEqual(self.miner.state["swap_retry_at"], 1300)
        self.assertEqual(self.miner.state["swap_check_at"], 0)
        self.api.swap_error = None
        self.api.inventory_error = requests.ReadTimeout()
        before = sum(path == "/user/inventory" for _, path, _ in self.api.calls)
        with patch.object(bg.time, "time", return_value=1001):
            self.assertFalse(self.miner.auto_swap())
        self.assertEqual(sum(path == "/user/inventory" for _, path, _ in self.api.calls), before + 1)
        self.assertGreaterEqual(self.miner.state["swap_retry_at"], 1301)
        self.assertEqual(self.miner.state["swap_check_at"], self.miner.state["swap_retry_at"])
        self.assertIn("pending_swap", self.miner.state)
        self.api.inventory_error = None
        self.api.positions = [{"id": "cooldown-reward", "status": "ready"}]
        with patch.object(bg.time, "time", return_value=1002):
            self.assertFalse(self.miner.auto_swap(force=True))
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(sum(path == "/user/inventory" for _, path, _ in self.api.calls), before + 1)
        self.assertEqual(self.api.swap_attempts, 1)
        self.assertEqual(self.api.open_attempts, 1)
        self.assertIn(("POST", "/mining/positions/cooldown-reward/claim", None), self.api.calls)
        with patch.object(bg.time, "time", return_value=1301):
            self.assertFalse(self.miner.auto_swap())
        self.assertEqual(sum(path == "/user/inventory" for _, path, _ in self.api.calls), before + 2)
        self.assertEqual(self.miner.state["swap_check_at"], 1316)

    def test_unconfirmed_early_read_is_not_repeated_by_same_cycle_claim_force(self):
        self.api.apply_swap = False
        with patch.object(bg.time, "time", return_value=1000):
            self.assertEqual(self.miner.tick(), 0.5)
        self.api.claim_resource_reward = 1
        self.api.positions = [{"id": "still-pending-reward", "status": "ready"}]
        self.api.calls.clear()
        with patch.object(bg.time, "time", return_value=1001):
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(sum(path == "/user/inventory" for _, path, _ in self.api.calls), 1)
        self.assertEqual(self.miner.state["swap_check_at"], 1016)
        self.assertEqual(self.api.swap_attempts, 1)
        self.assertEqual(self.swap_posts(), [])
        self.assertEqual(Decimal(self.api.inventory[0]["quantity"]), Decimal("4.5"))
        self.assertEqual(Decimal(self.miner.state["pending_swap"]["quantity_before"]), Decimal("3.5"))
        self.assertEqual(self.api.open_attempts, 2)

    def test_pending_swap_is_saved_before_post_is_sent(self):
        original_request = self.api.request

        def request(method, path, body=None):
            if method == "POST" and path == "/trade/swap":
                saved = bg.read_json(self.miner.path)["pending_swap"]
                self.assertEqual(saved, self.miner.state["pending_swap"])
            return original_request(method, path, body)

        with patch.object(self.api, "request", side_effect=request):
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.swap_attempts, 1)

    def test_only_one_asset_is_swapped_per_cycle_in_stable_id_order(self):
        first, second = sorted(bg.SWAP_RESOURCE_IDS)[:2]
        self.api.inventory = [{"assetId": second, "quantity": 2},
                              {"assetId": first, "quantity": 3}]
        self.api.rates = {"assetValues": {first: 2, second: 2}}
        self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.swap_posts(), [("POST", "/trade/swap",
                                             {"fromAssetId": first, "toAssetId": "GEMS", "amount": 3})])

    def test_fractional_inventory_is_floored_without_losing_decimal_precision(self):
        self.api.inventory[0]["quantity"] = "2.9999999999999999999999999"
        self.api.rates = {"assetValues": {self.resource_id: "0.015"}}
        self.api.swap_result = {"received": "0.03", "toAsset": "GEMS"}
        self.assertTrue(self.miner.auto_swap())
        payload = self.swap_posts()[0][2]
        self.assertIs(type(payload["amount"]), int)
        self.assertEqual(payload["amount"], 2)
        self.assertEqual(Decimal(self.miner.state["pending_swap"]["estimated_gems"]), Decimal("0.03"))
        self.assertEqual(Decimal(self.miner.state["pending_swap"]["quantity_before"]),
                         Decimal("2.9999999999999999999999999"))

    def test_quote_is_rounded_down_and_too_small_swaps_are_preserved(self):
        for rate, expected_posts, expected_estimate in (("0.00999999999999999999999", 0, None),
                                                       ("0.01", 1, Decimal("0.01")),
                                                       ("0.01999999999999999999999", 1, Decimal("0.01"))):
            with self.subTest(rate=rate):
                self.reset_swap_state()
                self.api.inventory = [{"assetId": self.resource_id, "quantity": 1}]
                self.api.rates = {"assetValues": {self.resource_id: rate}}
                self.assertEqual(self.miner.auto_swap(), bool(expected_posts))
                self.assertEqual(self.api.swap_attempts, expected_posts)
                if expected_estimate is not None:
                    self.assertEqual(Decimal(self.miner.state["pending_swap"]["estimated_gems"]), expected_estimate)

    def test_post_amount_never_exceeds_safe_integer_limit(self):
        self.api.inventory[0]["quantity"] = "9007199254740993"
        self.assertFalse(self.miner.auto_swap())
        self.assertEqual(self.api.swap_attempts, 0)

    def test_maximum_safe_integer_quantity_is_sent_as_an_integer(self):
        self.api.inventory[0]["quantity"] = str(2**53 - 1)
        self.assertTrue(self.miner.auto_swap())
        amount = self.swap_posts()[0][2]["amount"]
        self.assertIs(type(amount), int)
        self.assertEqual(amount, 2**53 - 1)

    def test_acknowledgement_remains_pending_until_full_inventory_decrease(self):
        self.api.apply_swap = False
        self.assertTrue(self.miner.auto_swap())
        pending = self.miner.state["pending_swap"]
        self.assertEqual(pending["asset_id"], self.resource_id)
        self.assertEqual(pending["amount"], 3)
        self.assertIs(type(pending["amount"]), int)
        self.assertIsInstance(pending["quantity_before"], str)
        self.assertEqual(Decimal(pending["quantity_before"]), Decimal("3.5"))
        self.assertEqual(Decimal(pending["estimated_gems"]), Decimal("6"))
        self.assertIn("created_at", pending)
        self.assertEqual(bg.read_json(self.miner.path)["pending_swap"], pending)
        self.api.balance["gems"] = 999999
        self.assertFalse(self.miner.reconcile_swap({self.resource_id: Decimal("0.6")}))
        self.assertIn("pending_swap", self.miner.state)
        self.assertTrue(self.miner.reconcile_swap({self.resource_id: Decimal("0.5")}))
        self.assertNotIn("pending_swap", self.miner.state)

    def test_stale_acknowledgement_only_pauses_swaps_not_mining(self):
        self.api.apply_swap = False
        with patch.object(bg.time, "time", return_value=1000):
            self.assertEqual(self.miner.tick(), 0.5)
        with patch.object(bg.time, "time", return_value=1020):
            self.assertEqual(self.miner.tick(), 0.5)
        with patch.object(bg.time, "time", return_value=1040):
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.swap_attempts, 1)
        self.assertEqual(self.api.open_attempts, 3)
        self.assertIn("pending_swap", self.miner.state)

    def test_pending_readback_respects_fifteen_second_interval_even_when_forced(self):
        self.api.apply_swap = False
        with patch.object(bg.time, "time", return_value=1000):
            self.assertTrue(self.miner.auto_swap())
        with patch.object(bg.time, "time", return_value=1001):
            self.assertFalse(self.miner.auto_swap())
        inventory_reads = sum(path == "/user/inventory" for _, path, _ in self.api.calls)
        with patch.object(bg.time, "time", return_value=1015):
            self.assertFalse(self.miner.auto_swap(force=True))
        self.assertEqual(sum(path == "/user/inventory" for _, path, _ in self.api.calls), inventory_reads)
        self.api.inventory[0]["quantity"] = "0.5"
        with patch.object(bg.time, "time", return_value=1016):
            self.assertTrue(self.miner.auto_swap())
        self.assertEqual(self.api.swap_attempts, 1)
        self.assertNotIn("pending_swap", self.miner.state)

    def test_success_without_positive_gems_ack_remains_uncertain(self):
        payloads = ({}, {"received": 1, "toAsset": "USDC"},
                    {"received": 1, "toAsset": "gems"},
                    *({"received": received, "toAsset": "GEMS"}
                      for received in (None, False, 0, -1, float("nan"), float("inf"))), [])
        for payload in payloads:
            with self.subTest(payload=payload):
                self.reset_swap_state()
                self.api.inventory = [{"assetId": self.resource_id, "quantity": 3.5}]
                self.api.apply_swap = False
                self.api.swap_result = payload
                with patch.object(bg.time, "time", return_value=1000):
                    self.assertFalse(self.miner.auto_swap())
                    self.assertGreaterEqual(self.miner.state["swap_retry_at"], 1300)
                    self.assertEqual(self.miner.state["swap_check_at"], 0)
                self.assertIn("pending_swap", self.miner.state)
                with patch.object(bg.time, "time", return_value=2000):
                    self.assertFalse(self.miner.auto_swap(force=True))
                self.assertEqual(self.api.swap_attempts, 1)

    def test_pending_marker_save_failure_prevents_swap_post(self):
        with patch.object(self.miner, "save", side_effect=OSError("TEST-DISK-FAILURE")):
            self.assertFalse(self.miner.auto_swap())
        self.assertEqual(self.api.swap_attempts, 0)
        self.assertNotIn("pending_swap", self.miner.state)

    def test_definitive_client_error_clears_pending_and_force_respects_error_cooldown(self):
        self.api.swap_error = bg.ApiError(400)
        with patch.object(bg.time, "time", return_value=1000):
            self.assertFalse(self.miner.auto_swap())
            self.assertNotIn("pending_swap", self.miner.state)
            self.assertGreaterEqual(self.miner.state["swap_retry_at"], 1300)
            self.assertGreaterEqual(self.miner.state["swap_check_at"], 1300)
            self.assertFalse(self.miner.auto_swap(force=True))
            self.assertEqual(self.miner.tick(), 0.5)
        self.assertEqual(self.api.swap_attempts, 1)
        self.assertEqual(self.api.open_attempts, 1)

    def test_claim_force_bypasses_only_normal_swap_check_interval(self):
        self.miner.state["swap_check_at"] = time.time() + 3600
        self.api.positions = [{"id": "ready-position", "status": "ready"}]
        self.assertEqual(self.miner.tick(), 0.5)
        paths = [(method, path) for method, path, _ in self.api.calls]
        self.assertLess(paths.index(("POST", "/mining/positions/ready-position/claim")),
                        paths.index(("POST", "/trade/swap")))
        self.assertEqual(self.api.swap_attempts, 1)

    def test_rate_limit_retry_after_is_not_shortened_or_bypassed_by_force(self):
        self.api.swap_error = bg.ApiError(429, retry_after=900)
        with patch.object(bg.time, "time", return_value=1000):
            self.assertFalse(self.miner.auto_swap())
            self.assertGreaterEqual(self.miner.state["swap_retry_at"], 1900)
            self.assertNotIn("pending_swap", self.miner.state)
        with patch.object(bg.time, "time", return_value=1500):
            self.assertFalse(self.miner.auto_swap(force=True))
        self.assertEqual(self.api.swap_attempts, 1)

    def test_confirmation_save_failure_preserves_durable_pending_marker(self):
        self.api.apply_swap = False
        self.assertTrue(self.miner.auto_swap())
        marker = dict(self.miner.state["pending_swap"])
        with patch.object(self.miner, "save", side_effect=OSError("TEST-DISK-FAILURE")):
            with self.assertRaises(OSError):
                self.miner.reconcile_swap({self.resource_id: Decimal("0.5")})
        self.assertEqual(self.miner.state["pending_swap"], marker)
        self.assertEqual(bg.read_json(self.miner.path)["pending_swap"], marker)

    def test_swap_occurs_after_claim_and_before_explicitly_enabled_upgrade(self):
        self.miner.args.auto_upgrades = True
        self.api.balance = {"gems": 0}
        self.api.upgrades_payload = [upgrade_fixtures.upgrade(cost=5)]
        self.api.positions = [{"id": "ready-position", "status": "ready"}]
        self.assertEqual(self.miner.tick(), 0.5)
        paths = [(method, path) for method, path, _ in self.api.calls]
        claim = paths.index(("POST", "/mining/positions/ready-position/claim"))
        swap = paths.index(("POST", "/trade/swap"))
        upgrade = paths.index(("POST", "/upgrades/power-upgrade"))
        deploy = paths.index(("POST", "/mining/positions"))
        self.assertLess(claim, swap)
        self.assertLess(swap, upgrade)
        self.assertLess(upgrade, deploy)
        self.assertEqual(self.api.balance["gems"], 1)

    def test_restart_preserves_uncertain_swap_without_resending(self):
        self.api.swap_error = requests.ReadTimeout()
        self.assertFalse(self.miner.auto_swap())
        replacement = bg.Miner(self.miner.wallet, self.root, fixtures.Gate(), self.miner.args)
        replacement.api.session.close()
        replacement_api = replacement.api = SwapApi(replacement.wallet, replacement.state)
        try:
            self.assertEqual(replacement.tick(), 0.5)
            self.assertEqual(replacement_api.swap_attempts, 0)
            self.assertEqual(replacement_api.open_attempts, 1)
            self.assertIn("pending_swap", replacement.state)
        finally:
            replacement_api.session.close()

    def test_no_energy_does_not_prevent_resource_conversion(self):
        self.api.energy = 0
        self.api.locations = []
        self.miner.tick()
        self.assertEqual(self.api.swap_attempts, 1)
        self.assertEqual(self.api.balance["gems"], 106)
        self.assertEqual(self.api.open_attempts, 0)


class SwapCliTests(unittest.TestCase):
    def test_resource_swap_defaults_on_and_skip_flag_disables_it(self):
        for arguments, expected in (([], False), (["--skip-swap"], True)):
            with self.subTest(arguments=arguments):
                captured = []

                def run_client(args, stop):
                    captured.append(args.skip_swap)
                    stop.set()
                    return 0

                with patch.object(bg.signal, "signal"), patch.object(bg, "run_client", side_effect=run_client):
                    self.assertEqual(bg.main(arguments), 0)
                self.assertEqual(captured, [expected])

    def test_swapping_gem_assets_is_an_explicit_cli_opt_in(self):
        for arguments, expected in (([], False), (["--swap-gems"], True)):
            with self.subTest(arguments=arguments):
                captured = []

                def run_client(args, stop):
                    captured.append((args.swap_gems, args.auto_upgrades))
                    stop.set()
                    return 0

                with patch.object(bg.signal, "signal"), patch.object(bg, "run_client", side_effect=run_client):
                    self.assertEqual(bg.main(arguments), 0)
                self.assertEqual(captured, [(expected, False)])

    def test_each_clear_command_preserves_other_pending_operations_and_cooldowns(self):
        cases = (("--clear-pending", "pending_open", ()),
                 ("--clear-pending-upgrade", "pending_upgrade", ("upgrade_check_at", "upgrade_retry_at")),
                 ("--clear-pending-swap", "pending_swap", ("swap_check_at", "swap_retry_at")))
        with fixtures.local_temp() as directory:
            root = Path(directory)
            bg.secure_directory(root / "state")
            address = fixtures.wallet()["address"]
            state_path = root / "state" / (address.lower() + ".json")
            missing_wallet = root / "must-not-be-created.json"
            original = {"pending_open": {"before_ids": [], "location_id": "starter"},
                        "pending_upgrade": {"upgrade_id": "power", "power_before": 0},
                        "pending_swap": {"asset_id": sorted(bg.SWAP_RESOURCE_IDS)[0], "amount": 3},
                        "upgrade_check_at": 888, "upgrade_retry_at": 999,
                        "swap_check_at": 666, "swap_retry_at": 777,
                        "last_swap_asset": "historical", "token": "preserved"}
            for flag, marker, cooldowns in cases:
                with self.subTest(flag=flag):
                    bg.write_json(state_path, original)
                    with patch.object(bg.signal, "signal"), \
                            patch.object(bg, "load_wallets", side_effect=AssertionError("No vault needed")), \
                            patch.object(bg.Account, "create") as create, \
                            patch.object(bg.requests.Session, "request", side_effect=AssertionError("No HTTP")):
                        self.assertEqual(bg.main(["--data-dir", str(root), "--wallet-file", str(missing_wallet),
                                                  flag, address]), 0)
                    expected = {key: value for key, value in original.items()
                                if key not in {marker, *cooldowns}}
                    self.assertEqual(bg.read_json(state_path), expected)
                    self.assertFalse(missing_wallet.exists())
                    create.assert_not_called()

    def test_all_three_maintenance_flags_are_mutually_exclusive(self):
        address = fixtures.wallet()["address"]
        pairs = (("--clear-pending", "--clear-pending-swap"),
                 ("--clear-pending-upgrade", "--clear-pending-swap"))
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                with patch.object(bg, "run_client") as run, patch("sys.stderr"):
                    with self.assertRaises(SystemExit) as result:
                        bg.main([first, address, second, address])
                self.assertEqual(result.exception.code, 2)
                run.assert_not_called()

    def test_clear_swap_rejects_empty_or_invalid_address_before_runtime(self):
        for address in ("", "invalid", "0x1234", "../state"):
            with self.subTest(address=address):
                with patch.object(bg, "run_client") as run, patch("sys.stderr"):
                    with self.assertRaises(SystemExit) as result:
                        bg.main(["--clear-pending-swap", address])
                self.assertEqual(result.exception.code, 2)
                run.assert_not_called()

    def test_direct_maintenance_input_cannot_fall_through_into_wallet_runtime(self):
        address = fixtures.wallet()["address"]
        invalid_cases = ({"clear_pending_swap": ""}, {"clear_pending_swap": "invalid"},
                         {"clear_pending": address, "clear_pending_swap": address},
                         {"clear_pending_upgrade": address, "clear_pending_swap": address})
        with fixtures.local_temp() as directory:
            for fields in invalid_cases:
                with self.subTest(fields=fields):
                    args = SimpleNamespace(data_dir=Path(directory), clear_pending=None,
                                           clear_pending_upgrade=None, clear_pending_swap=None)
                    vars(args).update(fields)
                    with patch.object(bg, "load_wallets") as load, \
                            patch.object(bg, "ensure_wallets") as ensure, \
                            patch.object(bg.requests.Session, "request", side_effect=AssertionError("No HTTP")):
                        with self.assertRaises(bg.WalletPaused):
                            bg.run_client(args, threading.Event())
                    load.assert_not_called()
                    ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
