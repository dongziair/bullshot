"""Offline-only tests: deterministic public test keys, mocked HTTP, no registration."""

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

    def test_referral_missing_prevents_replaying_mutation(self):
        api = self.api([
            Response(status=401), Response({"nonce": "n"}), Response({"token": "fresh"}),
            Response({"referredBy": None}),
        ], token="old")
        with self.assertRaises(bg.WalletPaused):
            api.request("POST", "/mining/positions", {"locationId": "mine"})
        self.assertEqual(len(api.session.calls), 4)

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
                result = bg.main(["--wallets", "1", "--data-dir", directory, "--init-only"])
            self.assertEqual(result, 0)
            session.assert_not_called()

    def test_same_directory_cannot_be_locked_twice(self):
        with local_temp() as directory:
            with bg.directory_lock(Path(directory)):
                with self.assertRaises(bg.WalletPaused):
                    with bg.directory_lock(Path(directory)):
                        pass


class FakeMiningApi:
    verify_referral = staticmethod(bg.Api.verify_referral)

    def __init__(self):
        self.calls = []
        self.energy = 10
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
            return {
                "/user/me": self.me,
                "/mining/positions": list(self.positions),
                "/mining/stats": {"energy": self.energy},
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
        args = SimpleNamespace(ref_code=bg.REF_CODE, interval=15, skip_quests=True, location_id=None)
        self.miner = bg.Miner(wallet(), folder, Gate(), args)
        self.miner.api.session.close()
        self.api = self.miner.api = FakeMiningApi()

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

    def test_five_occupied_positions_prevent_opening(self):
        self.api.positions = [{"id": str(i), "status": "active"} for i in range(5)]
        self.assertEqual(self.miner.tick(), 15)
        self.assertEqual(self.api.open_attempts, 0)

    def test_live_stats_energy_takes_precedence(self):
        self.api.me["energy"] = 100
        self.api.energy = 4
        self.assertEqual(self.miner.tick(), 15)
        self.assertEqual(self.api.open_attempts, 0)

    def test_no_more_expensive_fallback(self):
        self.api.locations = [{"id": "quarry", "energyCost": 10, "durationSeconds": 600}]
        self.miner.tick()
        self.assertEqual(self.api.open_attempts, 0)

    def test_unrecognized_status_prevents_mutation(self):
        self.api.positions = [{"id": "p", "status": "unexpected"}]
        self.assertIsNone(self.miner.tick())
        self.assertIn("paused_reason", self.miner.state)
        self.assertFalse(any(m == "POST" for m, _, _ in self.api.calls))

    def test_missing_referral_prevents_all_mutations(self):
        self.api.me["referredBy"] = None
        self.assertIsNone(self.miner.tick())
        self.assertEqual(len(self.api.calls), 1)

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


if __name__ == "__main__":
    unittest.main()
