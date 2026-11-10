#!/usr/bin/env python3
"""Bull Gems off-chain client. Python 3.10+; no RPC or transaction signing."""

from __future__ import annotations

import argparse
import contextlib
import heapq
import json
import logging
import math
import os
import random
import re
import signal
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests
from eth_account import Account
from eth_account.messages import encode_defunct


API_BASE = "https://gemapi.bullshot.io/api"
SITE = "https://gem.bullshot.io"
SIGN_PREFIX = "Sign in to Bull GEM on Base: "
REF_CODE = "0x75527f54"
QUEST_IDS = {"daily_miner_signin", "weekly_miner_checkin"}
HTTP_TIMEOUT = (10, 30)
STATUS_LOG_INTERVAL = 300
LOCATION_CACHE_SECONDS = 300
UPGRADE_CHECK_SECONDS = 60
UPGRADE_RETRY_SECONDS = 300
SWAP_CHECK_SECONDS = 60
SWAP_RETRY_SECONDS = 300
SWAP_RESOURCE_IDS = frozenset({
    "iron-ore", "coal", "copper-ore", "sulfur", "cryo-shard", "void-dust",
    "steel-ingot", "bronze-wire", "thermal-core", "void-reactor",
})
SWAP_GEM_IDS = frozenset({"emerald", "sapphire", "ruby", "diamond", "obsidian", "void-gem"})
MAX_API_INTEGER = 2 ** 53 - 1
LOG = logging.getLogger("bull_gems")


class WindowLogHandler(logging.StreamHandler):
    """Console only; output failures must not break retry or shutdown handling."""

    def __init__(self, stream=None):
        super().__init__(sys.stdout if stream is None else stream)
        self.window_closed = False

    def emit(self, record):
        if self.window_closed:
            return
        try:
            self.stream.write(self.format(record) + self.terminator)
            self.flush()
        except (OSError, ValueError):
            # No fallback file and no traceback: discard subsequent console output.
            self.window_closed = True
        except Exception:
            # A formatting/console failure is not a reason to stop workers.
            pass


class ShapeError(Exception):
    """Unexpected response: do not guess values before a mutation."""


class WalletPaused(Exception):
    pass


class Stopped(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, retry_after: float = 0):
        # Never put server bodies, signatures or bearer tokens in exceptions/logs.
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


def write_json(path: Path, value: object) -> None:
    """Atomic replacement; Unix files are owner-readable/writable only."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ShapeError("Local JSON file must contain an object")
    return value


@contextlib.contextmanager
def directory_lock(folder: Path):
    """Protect one vault from two copies of this client (Windows and Linux)."""
    lock_path = folder / "client.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    stream = os.fdopen(fd, "r+b")
    try:
        if os.name == "nt":
            import msvcrt

            if not os.fstat(fd).st_size:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise WalletPaused("Another client is using this data directory") from None
    try:
        yield
    finally:
        stream.close()


def load_wallets(path: Path) -> list[dict]:
    """Read and validate a vault without changing it or generating any key."""
    if not path.is_file():
        raise WalletPaused("Wallet file missing; run bash generate_wallets.sh COUNT first")
    vault = read_json(path)
    if vault.get("version") != 1 or not isinstance(vault.get("wallets"), list):
        raise ShapeError("Invalid wallet vault; refusing to replace it")
    wallets = vault["wallets"]
    if not wallets:
        raise ShapeError("Wallet file is empty; generate wallets first")
    seen = set()
    for wallet in wallets:
        if not isinstance(wallet, dict):
            raise ShapeError("Invalid wallet record")
        try:
            address = Account.from_key(wallet["private_key"]).address
            valid = address.lower() == wallet["address"].lower()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ShapeError("Invalid local wallet key/address") from None
        if not valid or address.lower() in seen:
            raise ShapeError("Wallet address mismatch or duplicate")
        seen.add(address.lower())
    return wallets


def ensure_wallets(folder: Path, count: int, wallet_file: Path | None = None) -> list[dict]:
    """Generation-only entry: append to the requested total, never replace keys."""
    path = wallet_file if wallet_file is not None else folder / "wallets.json"
    legacy = folder / "wallets.json"
    if path.exists():
        wallets = load_wallets(path)
    elif path != legacy and legacy.exists():
        # Reuse old deployments' keys when explicitly running the generation step.
        wallets = load_wallets(legacy)
    else:
        if any((folder / "state").glob("*.json")):
            raise WalletPaused("State exists but the wallet file is missing; restore your vault")
        wallets = []
    if len(wallets) > count:
        raise WalletPaused("Vault has more wallets than --wallets; refusing to ignore any")
    seen = {wallet["address"].lower() for wallet in wallets}
    changed = not path.exists()
    while len(wallets) < count:
        account = Account.create()
        if account.address.lower() in seen:
            continue
        wallets.append({
            "address": account.address,
            "private_key": "0x" + account.key.hex().removeprefix("0x"),
            "created_at": utc_now(),
        })
        seen.add(account.address.lower())
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json(path, {"version": 1, "wallets": wallets})
    if os.name == "posix":
        path.chmod(0o600)
    return wallets


def unwrap(value):
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


def as_object(value, key: str | None = None) -> dict:
    value = unwrap(value)
    if key and isinstance(value, dict) and key in value:
        value = value[key]
    if not isinstance(value, dict):
        raise ShapeError("Expected a JSON object")
    return value


def as_records(value, key: str) -> list[dict]:
    value = unwrap(value)
    if isinstance(value, dict):
        value = value.get(key)
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ShapeError(f"Expected {key} array")
    return value


def number(value, field: str) -> float:
    if isinstance(value, bool):
        raise ShapeError(f"Invalid {field}")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ShapeError(f"Missing or invalid {field}") from None
    if not math.isfinite(result) or result < 0:
        raise ShapeError(f"Invalid {field}")
    return result


def decimal_number(value, field: str) -> Decimal:
    """Keep inventory fractions and quotes exact; reject the usual bad numbers."""
    number(value, field)
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ShapeError(f"Missing or invalid {field}") from None
    if not result.is_finite() or result < 0:
        raise ShapeError(f"Invalid {field}")
    return result


def item_id(record: dict) -> str:
    value = record.get("id", record.get("_id"))
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise ShapeError("Missing record id")
    return str(value)


def retry_seconds(header: str | None) -> float:
    if header:
        try:
            return max(15, number(header, "Retry-After"))
        except ShapeError:
            try:
                return max(15, parsedate_to_datetime(header).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return 60


class RequestGate:
    """A process-wide request interval and a shared 429 cooldown."""

    def __init__(self, interval: float, stop: threading.Event):
        self.interval = interval
        self.stop = stop
        self.lock = threading.Lock()
        self.next_at = 0.0

    def pause(self, seconds: float) -> None:
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)

    def enter(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                delay = self.next_at - time.monotonic()
                if delay <= 0:
                    self.next_at = time.monotonic() + self.interval
                    return
            self.stop.wait(delay)
        raise Stopped()


class Api:
    def __init__(self, wallet: dict, state: dict, save, gate: RequestGate, ref_code: str):
        self.wallet = wallet
        self.state = state
        self.save = save
        self.gate = gate
        self.ref_code = ref_code
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "BullGems-Python/1.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": SITE,
            "Referer": SITE + "/",
        })

    def raw(self, method: str, path: str, body=None, auth: bool = True):
        if not path.startswith("/") or path.startswith("//"):
            raise ShapeError("Invalid API path")
        self.gate.enter()
        headers = {"X-Wallet-Address": self.wallet["address"]}
        if auth:
            headers["Authorization"] = "Bearer " + self.state.get("token", "")
        # No automatic POST retries and no redirect forwarding of credentials.
        started = time.monotonic()
        try:
            with self.session.request(
                method, API_BASE + path, json=body, headers=headers,
                timeout=HTTP_TIMEOUT, allow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    delay = retry_seconds(response.headers.get("Retry-After")) if response.status_code == 429 else 0
                    if response.status_code == 429:
                        self.gate.pause(delay)
                    raise ApiError(response.status_code, delay)
                if response.status_code == 204:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    raise ShapeError("API returned non-JSON success response") from None
        except requests.RequestException as error:
            # Keep the original exception type and POST uncertainty semantics.
            # Never log its text, headers, request body or URL query parameters.
            error.bull_gems_operation = f"{method.upper()} {path.split('?', 1)[0]}"
            error.bull_gems_elapsed = time.monotonic() - started
            raise

    def verify_referral(self, me: dict) -> bool:
        # Referral confirmation is informational, never a prerequisite for mining.
        if "referredBy" not in me:
            status = "missing"
        elif not me["referredBy"] or (isinstance(me["referredBy"], str) and not me["referredBy"].strip()):
            status = "empty"
        else:
            status = "present"
        previous = self.state.get("referral_status")
        self.state["referral_status"] = status
        if status == "present":
            self.state["referral_checked_at"] = utc_now()
            if previous in ("missing", "empty"):
                LOG.info("%s referral now confirmed", self.wallet["address"])
            return True
        self.state.pop("referral_checked_at", None)
        if previous != status:
            detail = "not returned by /user/me" if status == "missing" else "empty"
            LOG.warning("%s referredBy %s; referral unconfirmed; quests and mining continue", self.wallet["address"], detail)
        return False

    def login(self) -> None:
        nonce = as_object(self.raw("POST", "/auth/nonce", {"walletAddress": self.wallet["address"]}, auth=False))
        message = nonce.get("message")
        if message is None and isinstance(nonce.get("nonce"), str):
            message = SIGN_PREFIX + nonce["nonce"]
        if not isinstance(message, str) or not message.startswith(SIGN_PREFIX) or not len(SIGN_PREFIX) < len(message) <= 4096:
            raise WalletPaused("Unexpected login message; refused personal_sign")
        signature = Account.sign_message(
            encode_defunct(text=message), private_key=self.wallet["private_key"],
        ).signature.hex()
        signature = "0x" + signature.removeprefix("0x")
        result = as_object(self.raw("POST", "/auth/login", {
            "walletAddress": self.wallet["address"], "signature": signature,
            "message": message, "refCode": self.ref_code,
        }, auth=False))
        token = result.get("token")
        if not isinstance(token, str) or not token.strip():
            raise ShapeError("Login response has no token")
        self.state["token"] = token
        me = as_object(self.raw("GET", "/user/me"), "user")
        referral_confirmed = self.verify_referral(me)
        self.save()
        LOG.info("%s login OK; referral %s", self.wallet["address"], "present" if referral_confirmed else "unconfirmed (not blocking)")

    def request(self, method: str, path: str, body=None):
        if not self.state.get("token"):
            self.login()
        try:
            return self.raw(method, path, body)
        except ApiError as error:
            if error.status != 401:
                raise
        # Only a definitive 401 may replay a request, once, after signing in again.
        self.login()
        return self.raw(method, path, body)


def location_duration(location: dict) -> float:
    duration = location.get("durationSeconds")
    if duration is None and "durationMinutes" in location:
        duration = number(location["durationMinutes"], "durationMinutes") * 60
    seconds = number(duration, "durationSeconds")
    if seconds <= 0:
        raise ShapeError("Mining location duration must be positive")
    return seconds


def choose_location(locations: list[dict], me: dict, requested: str | None, energy: float) -> dict | None:
    level = number(me.get("level", 1), "level")
    power = number(me.get("miningPower", 0), "miningPower")
    budget = number(energy, "energy")
    candidates = []
    for location in locations:
        if requested is not None and item_id(location) != requested:
            continue
        if location.get("isUnlocked") is False or location.get("unlocked") is False or location.get("isActive") is False:
            continue
        required_level = number(location.get("levelRequired", 1), "levelRequired")
        required_power = number(location.get("miningPowerRequired", 0), "miningPowerRequired")
        if required_level > level or required_power > power:
            continue
        cost = number(location.get("energyCost"), "energyCost")
        if cost <= 0:
            raise ShapeError("Mining location energyCost must be positive")
        if cost > budget:
            continue
        duration = location_duration(location)
        # Highest eligible level first, not an unsupported claim about profit.
        rank = (-required_level, -required_power, cost, duration, item_id(location))
        candidates.append((rank, location))
    return min(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


class Miner:
    def __init__(self, wallet: dict, folder: Path, gate: RequestGate, args):
        self.wallet = wallet
        self.path = folder / "state" / (wallet["address"].lower() + ".json")
        self.state = {}
        self.state_loaded = False
        try:
            self.load_state()
        except Exception:
            # A bad state file affects only this wallet; tick() retries loading it.
            pass
        self.api = Api(wallet, self.state, self.save, gate, args.ref_code)
        self.args = args
        self.failures = 0
        self.locations = None
        self.locations_at = 0.0
        self.locations_profile = None
        self.warned_no_location = False
        self.warned_pending = False
        self.warned_pending_upgrade = False
        self.upgrade_notice = None
        self.warned_pending_swap = False
        self.swap_notice = None
        self.next_status_log = 0.0

    def load_state(self) -> None:
        data = read_json(self.path) if self.path.exists() else {}
        self.state.clear()
        self.state.update(data)
        self.state_loaded = True

    def save(self) -> None:
        write_json(self.path, self.state)

    def get_positions(self) -> list[dict]:
        records = as_records(self.api.request("GET", "/mining/positions"), "positions")
        ids = [item_id(x) for x in records]
        if len(ids) != len(set(ids)):
            raise ShapeError("Duplicate mining position ids")
        known = {"ready", "completed", "active", "mining", "running", "in_progress", "started", "pending", "claimed", "cancelled", "canceled"}
        if any(x.get("status") not in known for x in records):
            raise ShapeError("Unknown mining position status; refusing to open more")
        return records

    def quests(self) -> bool:
        if self.args.skip_quests or time.time() < self.state.get("quest_check_at", 0):
            return False
        data = as_object(self.api.request("GET", "/quests"))
        records = []
        for group in ("daily", "weekly", "oneTime"):
            if group in data:
                records.extend(as_records(data[group], group))
        if not any(group in data for group in ("daily", "weekly", "oneTime")):
            raise ShapeError("Expected daily/weekly quest groups")
        waits = self.state.setdefault("quest_waits", {})
        next_check = time.time() + 300
        claimed = False
        for quest in records:
            quest_id = quest.get("id")
            if quest_id not in QUEST_IDS or str(quest.get("platform", "")).lower() in {"twitter", "x"}:
                continue
            status = quest.get("status", "available")
            if status in {"claimed", "completed"}:
                waits.pop(quest_id, None)
                continue
            not_before = number(waits.get(quest_id, 0), "quest wait")
            if time.time() < not_before:
                next_check = min(next_check, not_before)
                continue
            if status == "started":
                left = number(quest.get("secondsLeft", 0), "secondsLeft")
                if left > 0:
                    next_check = min(next_check, time.time() + left)
                    continue
            if status in {"ready", "started"}:
                self.api.request("POST", "/quests/claim", {"questId": quest_id, "tweetUrl": ""})
                waits.pop(quest_id, None)
                claimed = True
                LOG.info("%s quest claimed: %s", self.wallet["address"], quest_id)
            elif status in {"available", "not_started", "not-started", "idle", "pending"}:
                # Persist before POST; after an uncertain response GET /quests reconciles it.
                waits[quest_id] = time.time() + 10
                self.state["quest_check_at"] = waits[quest_id]
                self.save()
                result = as_object(self.api.request("POST", "/quests/start", {"questId": quest_id}))
                delay = max(10, number(result.get("delaySeconds", 10), "delaySeconds"))
                waits[quest_id] = time.time() + delay
                next_check = min(next_check, waits[quest_id])
                LOG.info("%s quest started: %s", self.wallet["address"], quest_id)
        self.state["quest_check_at"] = next_check
        self.save()
        return claimed

    def defer_upgrade(self, error: Exception) -> None:
        """Optional purchases have their own cooldown, not a mining failure."""
        delay = UPGRADE_RETRY_SECONDS
        if isinstance(error, ApiError):
            delay = max(delay, error.retry_after)
            label = f"HTTP {error.status}"
        elif isinstance(error, ShapeError):
            label = str(error)
        else:
            # Request exceptions can contain credentials or server response text.
            label = type(error).__name__
            if isinstance(error, requests.RequestException):
                operation = getattr(error, "bull_gems_operation", None)
                elapsed = getattr(error, "bull_gems_elapsed", None)
                if isinstance(operation, str) and isinstance(elapsed, (int, float)):
                    label += f" {operation} (elapsed={elapsed:.1f}s)"
        self.state["upgrade_retry_at"] = time.time() + delay
        try:
            self.save()
        except Exception:
            # Keep pending purchases in memory even when local storage fails.
            pass
        LOG.warning("%s power upgrade deferred: %s; retry in %gs; mining continues", self.wallet["address"], label, delay)

    def reconcile_power_upgrade(self, profile: dict) -> bool:
        pending = self.state.get("pending_upgrade")
        if not pending:
            return False
        if not isinstance(pending, dict):
            raise ShapeError("Invalid pending power upgrade")
        before = number(pending.get("power_before"), "pending upgrade power")
        power = number(profile.get("miningPower"), "miningPower")
        if power <= before:
            if not self.warned_pending_upgrade:
                LOG.warning("%s power upgrade awaiting readback; new upgrades paused, mining continues. See --clear-pending-upgrade", self.wallet["address"])
                self.warned_pending_upgrade = True
            return False
        self.state.pop("pending_upgrade", None)
        self.state["upgrade_check_at"] = 0
        self.state["upgrade_retry_at"] = 0
        self.state["last_power_upgrade_at"] = utc_now()
        try:
            self.save()
        except Exception:
            # Do not lose the durable uncertain-purchase marker on a failed save.
            self.state["pending_upgrade"] = pending
            raise
        self.warned_pending_upgrade = False
        self.upgrade_notice = None
        LOG.info("%s power upgrade reconciled (power_before=%g; power_after=%g)", self.wallet["address"], before, power)
        return True

    def upgrade_power(self, profile: dict, *, force: bool = False) -> bool:
        """Buy at most one power upgrade, then require authoritative readback.

        Never buy speed/capacity, guess prices, or replay an uncertain purchase.
        Errors in this optional action must not prevent claims or deployments.
        """
        if not getattr(self.args, "auto_upgrades", False) or getattr(self.args, "skip_upgrades", False):
            return False
        try:
            if self.state.get("pending_upgrade"):
                return self.reconcile_power_upgrade(profile)
            now = time.time()
            if now < number(self.state.get("upgrade_retry_at", 0), "upgrade retry time"):
                return False
            if not force and now < number(self.state.get("upgrade_check_at", 0), "upgrade check time"):
                return False
            power = number(profile.get("miningPower"), "miningPower")
            balance = as_object(self.api.request("GET", "/user/balance"))
            gems = number(balance.get("gems"), "gems")
            upgrades = as_records(self.api.request("GET", "/upgrades"), "upgrades")
            candidates = []
            for upgrade in upgrades:
                if (upgrade.get("type") or upgrade.get("stat")) != "miningPower":
                    continue
                # The current station prices upgrades in GEMS. If a future
                # catalog explicitly names another currency, never buy it.
                if any(
                    field in upgrade and (
                        not isinstance(upgrade[field], str)
                        or upgrade[field].strip().upper() != "GEMS"
                    )
                    for field in ("currency", "costCurrency", "paymentCurrency")
                ):
                    continue
                if any(upgrade.get(flag) is False for flag in ("isActive", "isUnlocked", "unlocked")):
                    continue
                cost = number(upgrade.get("cost"), "upgrade cost")
                increment = number(upgrade.get("increment"), "upgrade increment")
                if cost <= 0 or increment <= 0:
                    raise ShapeError("Power upgrade cost and increment must be positive")
                candidates.append((cost, item_id(upgrade)))
            self.state["upgrade_check_at"] = now + UPGRADE_CHECK_SECONDS
            if not candidates:
                if self.upgrade_notice != ("unavailable",):
                    LOG.info("%s no available power upgrade; mining continues", self.wallet["address"])
                    self.upgrade_notice = ("unavailable",)
                self.save()
                return False
            cost, upgrade_id = min(candidates)
            if gems < cost:
                notice = ("funds", gems, cost)
                if self.upgrade_notice != notice:
                    LOG.info("%s power upgrade waiting (gems=%g; cost=%g); mining continues", self.wallet["address"], gems, cost)
                    self.upgrade_notice = notice
                self.save()
                return False
            self.state["pending_upgrade"] = {
                "upgrade_id": upgrade_id, "power_before": power,
                "cost": cost, "gems_before": gems, "created_at": utc_now(),
            }
            try:
                self.save()
            except Exception:
                # Nothing was sent: a failed prerequisite write is not a purchase.
                self.state.pop("pending_upgrade", None)
                raise
            try:
                self.api.request("POST", f"/upgrades/{quote(upgrade_id, safe='')}")
            except ApiError as error:
                if 400 <= error.status < 500:
                    self.state.pop("pending_upgrade", None)
                    self.save()
                raise
            # Even a 2xx remains pending until the new power is observed. This
            # also prevents repeated spending against a stale profile response.
            self.state["upgrade_check_at"] = 0
            self.warned_pending_upgrade = False
            self.upgrade_notice = None
            LOG.info("%s power upgrade purchased (id=%s; cost=%g; gems_before=%g; power_before=%g); refreshing profile", self.wallet["address"], upgrade_id, cost, gems, power)
            return True
        except Stopped:
            raise
        except Exception as error:
            self.defer_upgrade(error)
            return False

    def get_swap_inventory(self) -> dict[str, Decimal]:
        records = as_records(self.api.request("GET", "/user/inventory"), "inventory")
        quantities = {}
        for record in records:
            asset_id = record.get("assetId") or record.get("asset_id")
            if not isinstance(asset_id, str) or asset_id not in SWAP_RESOURCE_IDS | SWAP_GEM_IDS:
                continue
            if asset_id in quantities:
                raise ShapeError("Duplicate inventory asset id")
            quantities[asset_id] = decimal_number(record.get("quantity"), "inventory quantity")
        return quantities

    def defer_swap(self, error: Exception, *, readback_soon: bool = False) -> None:
        delay = SWAP_RETRY_SECONDS
        if isinstance(error, ApiError):
            delay = max(delay, error.retry_after)
            label = f"HTTP {error.status}"
        elif isinstance(error, ShapeError):
            label = str(error)
        else:
            label = type(error).__name__
            if isinstance(error, requests.RequestException):
                operation = getattr(error, "bull_gems_operation", None)
                elapsed = getattr(error, "bull_gems_elapsed", None)
                if isinstance(operation, str) and isinstance(elapsed, (int, float)):
                    label += f" {operation} (elapsed={elapsed:.1f}s)"
        self.state["swap_retry_at"] = time.time() + delay
        # A lost POST response needs an early read before newly claimed rewards
        # can hide the stock reduction. Failed readbacks themselves cool down.
        self.state["swap_check_at"] = 0 if readback_soon else self.state["swap_retry_at"]
        try:
            self.save()
        except Exception:
            pass
        LOG.warning("%s resource swap deferred: %s; retry in %gs; mining continues", self.wallet["address"], label, delay)

    def reconcile_swap(self, quantities: dict[str, Decimal]) -> bool:
        pending = self.state.get("pending_swap")
        if not pending:
            return False
        if not isinstance(pending, dict) or pending.get("asset_id") not in SWAP_RESOURCE_IDS | SWAP_GEM_IDS:
            raise ShapeError("Invalid pending resource swap")
        asset_id = pending["asset_id"]
        before = decimal_number(pending.get("quantity_before"), "pending swap quantity")
        amount = decimal_number(pending.get("amount"), "pending swap amount")
        if amount <= 0 or amount != int(amount) or amount > before or amount > MAX_API_INTEGER:
            raise ShapeError("Invalid pending swap amount")
        remaining = decimal_number(quantities.get(asset_id, 0), "inventory quantity")
        if remaining > before - amount:
            self.state["swap_check_at"] = time.time() + 15
            self.save()
            if not self.warned_pending_swap:
                LOG.warning("%s resource swap awaiting inventory readback; new swaps paused, mining continues. See --clear-pending-swap", self.wallet["address"])
                self.warned_pending_swap = True
            return False
        # A GEMS increase alone is not evidence: mining and quests also add GEMS.
        self.state.pop("pending_swap", None)
        self.state["swap_check_at"] = 0
        self.state["swap_retry_at"] = 0
        self.state["last_swap_at"] = utc_now()
        self.state["last_swap_asset"] = asset_id
        try:
            self.save()
        except Exception:
            self.state["pending_swap"] = pending
            raise
        self.warned_pending_swap = False
        self.swap_notice = None
        LOG.info("%s resource swap confirmed (asset=%s; amount=%s; remaining=%s)", self.wallet["address"], asset_id, amount, remaining)
        return True

    def auto_swap(self, *, force: bool = False) -> bool:
        """Convert whole units of known resources to GEMS, never buy resources."""
        if getattr(self.args, "skip_swap", False):
            return False
        posted = False
        try:
            now = time.time()
            pending = self.state.get("pending_swap")
            if pending:
                if now < number(self.state.get("swap_check_at", 0), "swap check time"):
                    return False
                return self.reconcile_swap(self.get_swap_inventory())
            if now < number(self.state.get("swap_retry_at", 0), "swap retry time"):
                return False
            if not force and now < number(self.state.get("swap_check_at", 0), "swap check time"):
                return False
            quantities = self.get_swap_inventory()
            self.state["swap_check_at"] = now + SWAP_CHECK_SECONDS
            allowed = SWAP_RESOURCE_IDS | (SWAP_GEM_IDS if getattr(self.args, "swap_gems", False) else frozenset())
            available = {asset_id: quantity for asset_id, quantity in quantities.items()
                         if asset_id in allowed and quantity >= 1}
            if not available:
                if self.swap_notice != "empty":
                    LOG.info("%s no whole resource units to swap; gems and fractional inventory kept", self.wallet["address"])
                    self.swap_notice = "empty"
                self.save()
                return False
            rates = as_object(self.api.request("GET", "/trade/rates"))
            values = rates.get("assetValues")
            if not isinstance(values, dict):
                raise ShapeError("Expected swap assetValues object")
            chosen = None
            for asset_id in sorted(available):
                if asset_id not in values:
                    continue
                rate = decimal_number(values[asset_id], "swap unit value")
                if rate <= 0:
                    continue
                quantity = available[asset_id]
                amount = int(quantity)
                if amount > MAX_API_INTEGER:
                    raise ShapeError("Swap amount exceeds safe API integer range")
                expected = (Decimal(amount) * rate).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
                if expected < Decimal("0.01"):
                    continue
                chosen = (asset_id, quantity, amount, expected)
                break
            if chosen is None:
                if self.swap_notice != "unquoted":
                    LOG.info("%s no positive API quote for swappable inventory; resources kept", self.wallet["address"])
                    self.swap_notice = "unquoted"
                self.save()
                return False
            asset_id, quantity, amount, expected = chosen
            self.state["pending_swap"] = {
                "asset_id": asset_id, "amount": amount,
                "quantity_before": str(quantity), "estimated_gems": str(expected),
                "created_at": utc_now(),
            }
            try:
                self.save()
            except Exception:
                self.state.pop("pending_swap", None)
                raise
            try:
                posted = True
                result = as_object(self.api.request("POST", "/trade/swap", {
                    "fromAssetId": asset_id, "toAssetId": "GEMS", "amount": amount,
                }))
            except ApiError as error:
                if 400 <= error.status < 500:
                    self.state.pop("pending_swap", None)
                    self.save()
                raise
            received = decimal_number(result.get("received"), "swap received")
            if result.get("toAsset") != "GEMS" or received <= 0:
                raise ShapeError("Swap did not acknowledge positive GEMS")
            # Do not post again against potentially stale inventory, even on 2xx.
            self.state["swap_check_at"] = 0
            self.swap_notice = None
            self.warned_pending_swap = False
            LOG.info("%s resource swap submitted (asset=%s; amount=%d; received=%s GEMS); awaiting inventory readback", self.wallet["address"], asset_id, amount, received)
            return True
        except Stopped:
            raise
        except Exception as error:
            self.defer_swap(error, readback_soon=posted and bool(self.state.get("pending_swap")))
            return False

    def cycle(self) -> float:
        me = as_object(self.api.request("GET", "/user/me"), "user")
        self.api.verify_referral(me)
        # Confirm yesterday's/last cycle's swap before quests or mining claims
        # add more of the same resource and obscure the consumed quantity.
        swapped = self.auto_swap() if self.state.get("pending_swap") else False
        try:
            quest_claimed = self.quests()
        except (ShapeError, ApiError) as error:
            # A broken/unavailable optional quest must not stop otherwise valid mining.
            if isinstance(error, ApiError) and (error.status in {401, 403, 429} or error.status >= 500):
                raise
            LOG.warning("%s quest check deferred: %s", self.wallet["address"], error)
            self.state["quest_check_at"] = time.time() + 300
            self.save()
            quest_claimed = False
        positions = self.get_positions()
        pending = self.state.get("pending_open")
        if pending and set(item_id(p) for p in positions) - set(pending["before_ids"]):
            self.state.pop("pending_open", None)
            self.save()
            pending = None
            LOG.info("%s uncertain deployment reconciled", self.wallet["address"])
        ready = [p for p in positions if p["status"] in {"ready", "completed"}]
        for position in ready:
            self.api.request("POST", f"/mining/positions/{quote(item_id(position), safe='')}/claim")
            LOG.info("%s mining reward claimed", self.wallet["address"])
        if ready or quest_claimed:
            positions = self.get_positions()
            me = as_object(self.api.request("GET", "/user/me"), "user")
            self.api.verify_referral(me)
        swapped = self.auto_swap(force=bool(ready or quest_claimed)) or swapped
        # Use the same live energy source the frontend prefers, not a local estimate.
        stats = as_object(self.api.request("GET", "/mining/stats"))
        energy = number(stats.get("energy", me.get("energy")), "energy")
        profile = dict(me)
        if "miningPower" in stats:
            profile["miningPower"] = stats["miningPower"]
        level = number(profile.get("level", 1), "level")
        power = number(profile.get("miningPower", 0), "miningPower")
        upgraded = self.upgrade_power(profile, force=bool(ready or quest_claimed or swapped))
        if upgraded:
            # Upgrades may award XP, but only the server decides the actual level.
            # Read both sources again before choosing a newly eligible mine.
            self.locations = None
            try:
                fresh_me = as_object(self.api.request("GET", "/user/me"), "user")
                fresh_stats = as_object(self.api.request("GET", "/mining/stats"))
                fresh_profile = dict(fresh_me)
                if "miningPower" in fresh_stats:
                    fresh_profile["miningPower"] = fresh_stats["miningPower"]
                fresh_level = number(fresh_profile.get("level", 1), "level")
                fresh_power = number(fresh_profile.get("miningPower", 0), "miningPower")
                fresh_energy = number(fresh_stats.get("energy", fresh_me.get("energy")), "energy")
                LOG.info("%s power upgrade profile refreshed (level=%g->%g; power=%g->%g)", self.wallet["address"], level, fresh_level, power, fresh_power)
                profile, level, power, energy = fresh_profile, fresh_level, fresh_power, fresh_energy
                self.reconcile_power_upgrade(profile)
            except Stopped:
                raise
            except Exception as error:
                # Retain the last valid snapshot and pending marker; keep mining.
                self.defer_upgrade(error)
                upgraded = False
        active = [p for p in positions if p["status"] not in {"claimed", "cancelled", "canceled"}]
        occupied = len(active)
        limit = getattr(self.args, "max_positions", None)
        capacity_available = limit is None or occupied < limit
        now = time.monotonic()
        if now >= self.next_status_log:
            status = (
                "pending_confirmation" if pending else
                "positions_full" if not capacity_available else
                "waiting_energy" if energy <= 0 else
                "checking_location"
            )
            # This is the server snapshot before any new deployment below.
            LOG.info("%s mining check: energy=%g; level=%g; power=%g; occupied=%d; limit=%s; status=%s", self.wallet["address"], energy, level, power, occupied, limit if limit is not None else "none", status)
            self.next_status_log = now + STATUS_LOG_INTERVAL
        delay = 0.5 if upgraded or swapped else self.args.interval
        if pending:
            if not self.warned_pending:
                LOG.warning("%s deployment result unknown; new openings paused until a new position appears. See --clear-pending", self.wallet["address"])
                self.warned_pending = True
        elif energy > 0 and capacity_available:
            eligibility = (level, power)
            if self.locations is None or time.time() >= self.locations_at or self.locations_profile != eligibility:
                self.locations = as_records(self.api.request("GET", "/mining/locations"), "locations")
                self.locations_at = time.time() + LOCATION_CACHE_SECONDS
                self.locations_profile = eligibility
                self.warned_no_location = False
            location = choose_location(self.locations, profile, self.args.location_id, energy)
            if location is None:
                if not self.warned_no_location:
                    LOG.warning("%s no unlocked affordable location (level=%g; power=%g; energy=%g); keeping claims and quests active", self.wallet["address"], level, power, energy)
                    self.warned_no_location = True
            else:
                location_id = item_id(location)
                cost = number(location["energyCost"], "energyCost")
                duration = location_duration(location)
                self.state["pending_open"] = {
                    "before_ids": [item_id(p) for p in positions],
                    "created_at": utc_now(), "location_id": location_id,
                }
                try:
                    self.save()
                except Exception:
                    # No POST was sent: do not leave a false uncertain-deployment marker.
                    self.state.pop("pending_open", None)
                    raise
                try:
                    self.api.request("POST", "/mining/positions", {"locationId": location.get("id", location.get("_id"))})
                except ApiError as error:
                    if 400 <= error.status < 500:
                        # A definitive client error did not accept this deployment.
                        self.state.pop("pending_open", None)
                        self.save()
                    raise
                # Acknowledged success. Transport errors/5xx keep pending_open for readback.
                self.state.pop("pending_open", None)
                self.state["last_opened_at"] = utc_now()
                self.state["last_location_id"] = location_id
                self.save()
                occupied += 1
                LOG.info("%s miner deployed (location=%s; level_required=%g; power_required=%g; cost=%g; duration=%gs; energy_before=%g; occupied=%d)", self.wallet["address"], location_id, number(location.get("levelRequired", 1), "levelRequired"), number(location.get("miningPowerRequired", 0), "miningPowerRequired"), cost, duration, energy, occupied)
                delay = 0.5
        self.state["last_cycle_at"] = utc_now()
        self.state["last_energy"] = energy
        self.state["last_occupied"] = occupied
        self.save()
        if not self.args.skip_quests:
            now = time.time()
            quest_due = self.state.get("quest_check_at", now + delay)
            delay = min(delay, quest_due - now)
        return max(0.5, delay)

    def tick(self) -> float | None:
        try:
            if not self.state_loaded:
                self.load_state()
            # Old versions permanently paused wallets. Recheck them automatically.
            self.state.pop("paused_reason", None)
            self.state.pop("last_error", None)
            self.state.pop("retry_at", None)
            delay = self.cycle()
            self.failures = 0
            return delay
        except Stopped:
            return None
        except Exception as error:
            self.failures += 1
            delay = min(300, 15 * 2 ** min(self.failures - 1, 5)) + random.uniform(0, 3)
            if isinstance(error, ApiError):
                delay = max(delay, error.retry_after)
                if error.status == 403 or 300 <= error.status < 400:
                    delay = max(delay, 300)
                label = f"HTTP {error.status}"
            elif isinstance(error, (WalletPaused, ShapeError)):
                delay = max(delay, 300)
                label = str(error)
            elif isinstance(error, requests.RequestException):
                label = type(error).__name__
                operation = getattr(error, "bull_gems_operation", None)
                elapsed = getattr(error, "bull_gems_elapsed", None)
                if isinstance(operation, str) and isinstance(elapsed, (int, float)):
                    label += f" {operation} (elapsed={elapsed:.1f}s)"
            else:
                label = type(error).__name__
            # Never overwrite an unreadable state: it may hold an uncertain POST.
            if self.state_loaded:
                self.state["last_error"] = label
                self.state["retry_at"] = time.time() + delay
                try:
                    self.save()
                except Exception as save_error:
                    LOG.warning("%s state save failed: %s; keeping state in memory", self.wallet["address"], type(save_error).__name__)
            LOG.warning("%s %s; retry in %.1fs", self.wallet["address"], label, delay)
            return delay


def run_miners(miners: list[Miner], workers: int, stop: threading.Event, request_stop=None) -> None:
    queue = [(time.monotonic(), index) for index in range(len(miners))]
    heapq.heapify(queue)
    running = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        while (queue or running) and not stop.is_set():
            while queue and len(running) < workers and queue[0][0] <= time.monotonic():
                _, index = heapq.heappop(queue)
                running[pool.submit(miners[index].tick)] = index
            if not running:
                if queue:
                    stop.wait(min(1, max(0, queue[0][0] - time.monotonic())))
                continue
            done, _ = wait(running, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                index = running.pop(future)
                try:
                    delay = future.result()
                except Exception as error:
                    # Last-resort isolation if an unexpected failure escapes tick().
                    LOG.warning("Worker %d failed: %s; retry in 30s", index, type(error).__name__)
                    delay = 30
                if delay is not None:
                    heapq.heappush(queue, (time.monotonic() + delay, index))
    finally:
        # A scheduler failure must not set the user's permanent shutdown event.
        if request_stop is not None:
            request_stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        for miner in miners:
            try:
                miner.api.session.close()
            except Exception:
                pass


def run_client(args, stop: threading.Event) -> int:
    folder = args.data_dir.expanduser().resolve()
    secure_directory(folder)
    with directory_lock(folder):
        secure_directory(folder / "state")
        # Maintenance must not accidentally create a new vault or extra wallets.
        maintenance = [
            ("deployment", args.clear_pending, "pending_open", ()),
            ("upgrade", getattr(args, "clear_pending_upgrade", None), "pending_upgrade", ("upgrade_check_at", "upgrade_retry_at")),
            ("swap", getattr(args, "clear_pending_swap", None), "pending_swap", ("swap_check_at", "swap_retry_at")),
        ]
        selected = [item for item in maintenance if item[1] is not None]
        if len(selected) > 1:
            raise WalletPaused("Choose only one pending marker to clear")
        if selected:
            action, address, marker, timers = selected[0]
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
                raise WalletPaused("Invalid wallet address")
            path = folder / "state" / (address.lower() + ".json")
            if not path.is_file():
                raise WalletPaused("No matching wallet state")
            state = read_json(path)
            state.pop(marker, None)
            for timer in timers:
                state.pop(timer, None)
            write_json(path, state)
            LOG.info("Pending %s marker cleared for %s", action, address)
            return 0
        if args.init_only:
            wallets = ensure_wallets(folder, args.wallets or 100, args.wallet_file)
            LOG.info("%d wallets saved to %s; no registration or mining requests sent", len(wallets), args.wallet_file)
            return 0
        # Runtime is strictly read-only with respect to the wallet vault.
        wallets = load_wallets(args.wallet_file)
        LOG.info("Loaded %d wallets from %s; no wallets will be generated", len(wallets), args.wallet_file)
        upgrades_enabled = getattr(args, "auto_upgrades", False) and not getattr(args, "skip_upgrades", False)
        LOG.info("Auto power upgrades %s; GEMS kept unless --auto-upgrades is explicitly enabled", "enabled" if upgrades_enabled else "disabled")
        LOG.info("Auto resource swap %s; target=GEMS; include_gemstones=%s", "disabled" if getattr(args, "skip_swap", False) else "enabled", getattr(args, "swap_gems", False))
        request_stop = threading.Event()
        gate = RequestGate(args.request_interval, request_stop)
        miners = [Miner(wallet, folder, gate, args) for wallet in wallets]
        run_miners(miners, args.workers, stop, request_stop)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--wallets", type=int, help="Generation only (--init-only): total count, default 100; never replace keys")
    parser.add_argument("--wallet-file", type=Path, default=Path(__file__).resolve().parent / "wallet.json", help="Existing wallet vault to read; created/appended only with --init-only")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "bull_gems_data")
    parser.add_argument("--ref-code", default=REF_CODE)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--interval", type=float, default=15, help="Minimum idle poll interval per wallet")
    parser.add_argument("--request-interval", type=float, default=0.25, help="Global minimum seconds between API requests")
    parser.add_argument("--location-id", help="Only deploy to this location; still requires sufficient level, power and energy")
    parser.add_argument("--max-positions", type=int, help="Optional per-wallet client cap; by default no client-side position cap")
    parser.add_argument("--skip-quests", action="store_true")
    upgrade_options = parser.add_mutually_exclusive_group()
    upgrade_options.add_argument("--auto-upgrades", action="store_true", help="Opt in to spending available GEMS on miningPower upgrades (off by default)")
    upgrade_options.add_argument("--skip-upgrades", action="store_true", help="Keep upgrades disabled; compatible with older startup commands")
    parser.add_argument("--skip-swap", action="store_true", help="Disable automatic whole-resource swaps to GEMS (enabled by default)")
    parser.add_argument("--swap-gems", action="store_true", help="Also swap gemstone items to GEMS; by default gemstones are kept")
    parser.add_argument("--init-only", action="store_true", help="Create local wallet file without any network requests")
    parser.add_argument("--resume", action="store_true", help="Compatibility option; failed wallets now retry automatically")
    maintenance_options = parser.add_mutually_exclusive_group()
    maintenance_options.add_argument("--clear-pending", metavar="ADDRESS", help="After manual server-side verification, clear one uncertain deployment and exit")
    maintenance_options.add_argument("--clear-pending-upgrade", metavar="ADDRESS", help="After manual server-side verification, clear one uncertain power purchase and exit")
    maintenance_options.add_argument("--clear-pending-swap", metavar="ADDRESS", help="After manual server-side verification, clear one uncertain resource swap and exit")
    args = parser.parse_args(argv)
    for option in ("clear_pending", "clear_pending_upgrade", "clear_pending_swap"):
        address = getattr(args, option)
        if address is not None and not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            parser.error(f"--{option.replace('_', '-')} requires a valid wallet address")
    if (args.wallets is not None and not 1 <= args.wallets <= 10000) or not 1 <= args.workers <= 20:
        parser.error("--wallets must be 1..10000; --workers must be 1..20")
    if args.wallets is not None and not args.init_only:
        parser.error("--wallets is only for generation with --init-only; runtime reads all wallets in --wallet-file")
    if args.max_positions is not None and args.max_positions <= 0:
        parser.error("--max-positions must be a positive integer; omit it for no client-side cap")
    if not math.isfinite(args.interval) or args.interval < 15 or not math.isfinite(args.request_interval) or args.request_interval < 0.1:
        parser.error("--interval must be >=15; --request-interval must be >=0.1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[WindowLogHandler()])
    stop = threading.Event()

    def request_shutdown(*_):
        if not stop.is_set():
            stop.set()
            LOG.info("Stop requested; waiting for active requests to finish")

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_shutdown)
    args.wallet_file = args.wallet_file.expanduser().resolve()
    maintenance = args.init_only or any(getattr(args, option) is not None for option in ("clear_pending", "clear_pending_upgrade", "clear_pending_swap"))
    while not stop.is_set():
        try:
            result = run_client(args, stop)
            if maintenance or stop.is_set():
                return result
            LOG.warning("Worker loop ended; restarting in 30s")
        except Exception as error:
            # Preserve the vault, and retry bootstrap/scheduler failures as well.
            label = str(error) if isinstance(error, (ShapeError, WalletPaused)) else type(error).__name__
            LOG.error("Client error: %s%s", label, "; retry in 30s" if not maintenance else "")
            if maintenance:
                return 1
        stop.wait(30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
