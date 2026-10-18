"""API adapter, filtering, persistent observations, and polling scheduler."""

from __future__ import annotations

import csv
import errno
import io
import json
import math
import re
import sqlite3
import socket
import ssl
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import certifi

API_URL = "https://api.warframe.market/v1/auctions"
VERSION = "0.3.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Un fuseau horaire est requis.")
    return result.astimezone(timezone.utc)


def auction_id_from_input(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in {"warframe.market", "www.warframe.market"} or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Utiliser un lien HTTPS warframe.market ou l'identifiant de l'annonce.")
        match = re.fullmatch(r"/(?:[a-z]{2}/)?auction/([A-Za-z0-9_-]+)/?", parsed.path)
        if not match:
            raise ValueError("Le lien doit pointer vers une annonce Riven précise.")
        value = match[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Identifiant d'annonce invalide.")
    return value


@dataclass(frozen=True)
class Watch:
    weapon: str
    label: str
    min_price: int = 10
    max_price: int = 201

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", self.weapon) or not self.label:
            raise ValueError("Arme invalide.")
        if any(isinstance(p, bool) or not isinstance(p, int) for p in (self.min_price, self.max_price)) or self.min_price < 0 or self.max_price < self.min_price:
            raise ValueError("La fourchette de prix est invalide.")


DEFAULT_WATCHES = (
    Watch("burston", "Burston", 10, 401),
    Watch("dual_toxocyst", "Dual Toxocyst", 10, 201),
    Watch("torid", "Torid", 10, 370),
    Watch("verglas", "Verglas", 10, 350),
    Watch("magistar", "Magistar", 10, 501),
)


@dataclass(frozen=True)
class Settings:
    watches: tuple[Watch, ...] = DEFAULT_WATCHES
    interval_seconds: float = 1.0
    platform: str = "pc"

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval_seconds) or self.interval_seconds < 1:
            raise ValueError("La cadence minimale est de 1 seconde, partagée par ce processus.")
        if not self.watches or len({w.weapon for w in self.watches}) != len(self.watches):
            raise ValueError("Définir au moins une recherche, sans arme en double.")

    def watch_for(self, weapon: str) -> Watch | None:
        return next((w for w in self.watches if w.weapon == weapon), None)

    def public(self) -> dict[str, Any]:
        return {"watches": [vars(w) for w in self.watches], "interval_seconds": self.interval_seconds, "platform": self.platform}

    @classmethod
    def single(cls, *, weapon: str = "dual_toxocyst", min_price: int = 10, max_price: int = 2000,
               interval_seconds: float = 1.0) -> Settings:
        label = next((w.label for w in DEFAULT_WATCHES if w.weapon == weapon), None)
        if label is None:
            raise ValueError("Arme non prise en charge.")
        return cls(watches=(Watch(weapon, label, min_price, max_price),), interval_seconds=interval_seconds)


class ApiFailure(Exception):
    def __init__(self, message: str, *, retry_after: float | None = None, blocked: bool = False,
                 kind: str = "api", http_status: int | None = None, duration_ms: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.blocked = blocked
        self.kind = kind
        self.http_status = http_status
        self.duration_ms = duration_ms


def verified_ssl_context() -> ssl.SSLContext:
    # Keep system / explicitly configured roots, and add the maintained CA bundle.
    # This also supports fresh Windows installations with incomplete system roots.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def connection_failure(exc: httpx.HTTPError, duration_ms: float) -> ApiFailure:
    """Classify the cause without exposing proxy URLs, credentials or raw errors."""
    causes: list[BaseException] = []
    cause: BaseException | None = exc
    while cause is not None and all(cause is not item for item in causes):
        causes.append(cause)
        cause = cause.__cause__ or cause.__context__
    if any(isinstance(cause, ssl.SSLCertVerificationError) for cause in causes):
        return ApiFailure("Certificat HTTPS non vérifié. Vérifier la date du PC et les certificats du système/proxy. La vérification TLS reste activée.",
                          blocked=True, kind="tls", duration_ms=duration_ms)
    if any(isinstance(cause, socket.gaierror) for cause in causes):
        return ApiFailure("Résolution DNS de l'API impossible. Vérifier la connexion et le DNS du PC.",
                          kind="dns", duration_ms=duration_ms)
    if any(isinstance(cause, OSError) and cause.errno in {errno.ECONNREFUSED, 10061} for cause in causes):
        return ApiFailure("Connexion HTTPS refusée par le réseau ou le serveur. Vérifier le pare-feu, le proxy et l'accès à api.warframe.market.",
                          kind="connection_refused", duration_ms=duration_ms)
    if isinstance(exc, httpx.ProxyError):
        return ApiFailure("Connexion au proxy refusée (ProxyError). Vérifier l'accès réseau/proxy, puis relancer.",
                          blocked=True, kind="proxy", duration_ms=duration_ms)
    if isinstance(exc, httpx.TimeoutException):
        return ApiFailure(f"Délai de connexion API dépassé ({type(exc).__name__}). Aucun flux reçu pour cette tentative.",
                          kind="timeout", duration_ms=duration_ms)
    return ApiFailure(f"Connexion API impossible ({type(exc).__name__} : réseau, DNS ou TLS).",
                      kind="network", duration_ms=duration_ms)


def safe_failure_details(exc: BaseException) -> dict[str, Any]:
    """Diagnostic identifiers only: never arbitrary exception messages/URLs."""
    types: list[str] = []
    network_codes: list[int] = []
    certificate_codes: list[int] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        types.append(type(exc).__name__)
        if isinstance(exc, OSError) and isinstance(exc.errno, int):
            network_codes.append(exc.errno)
        if isinstance(exc, ssl.SSLCertVerificationError) and isinstance(getattr(exc, "verify_code", None), int):
            certificate_codes.append(exc.verify_code)
        exc = exc.__cause__ or exc.__context__
    return {"exception_types": types, "network_codes": network_codes, "certificate_verify_codes": certificate_codes}


def retry_delay(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - (now or datetime.now(timezone.utc))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class MarketClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.last_response_meta: dict[str, Any] = {}
        self.client = httpx.Client(
            headers={"Platform": "pc", "Language": "en", "Accept": "application/json",
                     "User-Agent": f"RivenSniper/{VERSION} (read-only personal monitor)"},
            timeout=httpx.Timeout(15, connect=10),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
            verify=verified_ssl_context(),
            transport=transport,
        )

    def search(self) -> tuple[list[dict[str, Any]], float]:
        start = time.perf_counter()
        try:
            # The public recent snapshot avoids sorting a weapon's offers by price,
            # which can hide newly listed, more expensive offers. No cache busting.
            response = self.client.get(API_URL)
        except httpx.HTTPError as exc:
            # Proxy URLs and credentials must never leak through exception text.
            elapsed = round((time.perf_counter() - start) * 1000, 2)
            raise connection_failure(exc, elapsed) from exc
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        self.last_response_meta = {
            "last_http_status": response.status_code,
            "api_date": response.headers.get("Date"),
            "api_cache_age_seconds": retry_delay(response.headers.get("Age")),
            "api_cache_control": response.headers.get("Cache-Control"),
        }
        if response.status_code in (401, 403):
            challenged = response.status_code == 403 and ("cf-chl-" in response.text or "Just a moment..." in response.text)
            raise ApiFailure(
                ("Protection d'accès du site (HTTP 403). Collecte arrêtée, sans contournement. L'accès API doit être autorisé par le service."
                 if challenged else f"Accès API refusé (HTTP {response.status_code}). Vérifier les règles réseau ou l'accès au service, puis relancer."),
                blocked=True, kind="access", http_status=response.status_code, duration_ms=elapsed,
            )
        if response.status_code == 429:
            raise ApiFailure("Quota API atteint (429) : pause automatique.",
                             retry_after=retry_delay(response.headers.get("Retry-After")),
                             kind="quota", http_status=429, duration_ms=elapsed)
        if response.status_code != 200:
            raise ApiFailure(f"Réponse API HTTP {response.status_code}. Aucun contournement effectué.",
                             retry_after=retry_delay(response.headers.get("Retry-After")),
                             blocked=400 <= response.status_code < 500,
                             kind="http", http_status=response.status_code, duration_ms=elapsed)
        try:
            auctions = response.json()["payload"]["auctions"]
            if not isinstance(auctions, list) or any(not isinstance(a, dict) for a in auctions):
                raise ValueError("Invalid auctions")
        except (ValueError, TypeError, KeyError) as exc:
            raise ApiFailure("Format API inattendu : compatibilité de l'API à vérifier.", blocked=True,
                             kind="schema", http_status=response.status_code, duration_ms=elapsed) from exc
        return auctions, elapsed

    def close(self) -> None:
        self.client.close()


class DemoClient:
    """A deterministic, explicitly simulated feed. Never queries Warframe.market."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.started = time.monotonic()
        self.created = utc_now()

    def search(self) -> tuple[list[dict[str, Any]], float]:
        count = min(25, 1 + int((time.monotonic() - self.started) // 6))
        auctions = []
        for i in range(count):
            watch = self.settings.watches[i % len(self.settings.watches)]
            price = min(watch.max_price, 100 + (i % 5) * 15)
            auctions.append({
                "id": f"demo-{i:04d}", "created": self.created,
                "starting_price": price, "buyout_price": price,
                "visible": True, "closed": False,
                "owner": {"ingame_name": f"Simulation_{i + 1}", "status": "ingame", "platform": "pc"},
                "item": {"type": "riven", "weapon_url_name": watch.weapon,
                         "name": "Crita-visitis", "re_rolls": i * 7, "mastery_level": 10,
                         "attributes": [{"url_name": "critical_chance", "value": 123.4, "positive": True},
                                        {"url_name": "damage", "value": 85.2, "positive": True}]},
            })
        return list(reversed(auctions)), 0.0

    def close(self) -> None:
        pass


def normalize(raw: dict[str, Any], settings: Settings, *, demo: bool = False) -> dict[str, Any] | None:
    item, owner = raw.get("item"), raw.get("owner")
    if not isinstance(item, dict) or not isinstance(owner, dict):
        return None
    auction_id = raw.get("id")
    if not isinstance(auction_id, str) or not auction_id or not all(c.isalnum() or c in "-_" for c in auction_id):
        return None
    watch = settings.watch_for(item.get("weapon_url_name"))
    if item.get("type") != "riven" or watch is None:
        return None
    if owner.get("platform") not in (None, "pc"):
        return None
    price = raw.get("buyout_price")
    valid_price = isinstance(price, int) and not isinstance(price, bool) and price >= 0
    active = raw.get("closed") is not True and raw.get("visible") is not False and raw.get("private") is not True
    eligible = active and valid_price and watch.min_price <= price <= watch.max_price
    created = raw.get("created")
    if not isinstance(created, str):
        created = None
    if created is not None:
        try:
            parse_time(created)
        except ValueError:
            created = None
    name = " ".join(str(item.get("name") or "Riven").split())[:200]
    full_name = name if name.casefold().startswith(watch.label.casefold()) else f"{watch.label} {name[:1].upper()}{name[1:]}"
    seller = str(owner.get("ingame_name") or "Vendeur inconnu")[:100]
    trading_message = (f"/w {seller} Hi, I am interested in buying your {full_name} riven for {price} platinum, listed on Warframe.Market."
                       if valid_price and re.fullmatch(r"[A-Za-z0-9_.#-]{1,100}", seller) else None)
    attributes = []
    for attr in item.get("attributes", []) if isinstance(item.get("attributes"), list) else []:
        if isinstance(attr, dict) and isinstance(attr.get("value"), (int, float)) and not isinstance(attr["value"], bool) and math.isfinite(attr["value"]):
            attributes.append({"name": str(attr.get("url_name") or "Attribut")[:100].replace("_", " "),
                               "value": attr["value"], "positive": attr.get("positive") is not False})
    def integer_detail(key: str) -> int | None:
        value = item.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return {
        "id": auction_id, "weapon": watch.weapon, "weapon_label": watch.label,
        "name": name, "full_name": full_name,
        "price": price if valid_price else None,
        "seller": seller,
        "seller_status": str(owner.get("status") or "unknown"),
        "created_at": created, "eligible": eligible,
        "url": None if demo else f"https://warframe.market/auction/{auction_id}",
        "trading_message": trading_message,
        "attributes": attributes[:8], "rerolls": integer_detail("re_rolls"), "mastery_level": integer_detail("mastery_level"),
    }


class Monitor:
    def __init__(self, settings: Settings, database: Path | str, *, demo: bool = False, client: Any = None,
                 discord_config_path: Path | None = None, discord_transport: httpx.BaseTransport | None = None):
        self.settings, self.demo = settings, demo
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.client = client or (DemoClient(settings) if demo else MarketClient(settings))
        self.db = sqlite3.connect(str(database), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS seen (
                id TEXT PRIMARY KEY, price INTEGER, eligible INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL, weapon TEXT NOT NULL, name TEXT NOT NULL,
                price INTEGER NOT NULL, seller TEXT NOT NULL, seller_status TEXT NOT NULL,
                created_at TEXT, detected_at TEXT NOT NULL, api_age_seconds REAL,
                request_ms REAL NOT NULL, kind TEXT NOT NULL, url TEXT,
                aleca_seen_at TEXT, lead_seconds REAL
            );
            CREATE INDEX IF NOT EXISTS events_by_listing ON events(id, event_id DESC);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        for name, declaration in {"details_json": "TEXT NOT NULL DEFAULT '{}'", "trading_message": "TEXT"}.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE events ADD COLUMN {name} {declaration}")
        self.db.commit()
        from .notifications import DiscordDispatcher
        self.discord = DiscordDispatcher(self.db, self.lock, config_path=discord_config_path,
                                         demo=demo, transport=discord_transport)
        self.status: dict[str, Any] = {
            "phase": "starting", "message": "Initialisation : la première réponse sert de référence sans alerte.",
            "last_poll_at": None, "last_success_at": None, "next_poll_at": None,
            "request_ms": None, "polls": 0, "seeded": False, "baseline_count": 0,
            "returned_count": 0,
            "started_at": utc_now(), "successful_polls": 0, "failed_polls": 0,
            "consecutive_failures": 0, "backoff_seconds": 0,
            "last_error": None, "last_error_at": None, "last_error_kind": None,
            "last_http_status": None, "last_attempt_ms": None,
            "last_error_details": None,
            "last_attempt_interval_seconds": None, "request_in_flight": False,
            "api_endpoint": API_URL,
            "coverage_warning": "Instantané global récent : cache, ordre et taille non vérifiés. Des annonces peuvent manquer entre deux mises à jour ; 1 s est la cadence de consultation, pas un délai garanti après publication.",
        }
        self.pending_aleca: dict[tuple[str, int], str] = {}
        self.thread: threading.Thread | None = None

    def ingest(self, auctions: list[dict[str, Any]], request_ms: float, *, observed_at: str | None = None) -> int:
        detected = observed_at or utc_now()
        detected_time = parse_time(detected)
        with self.lock, self.db:
            baseline = not self.status["seeded"]
            alerts = 0
            initial_count = 0
            for raw in reversed(auctions):
                listing = normalize(raw, self.settings, demo=self.demo)
                if listing is None:
                    continue
                old = self.db.execute("SELECT price, eligible FROM seen WHERE id=?", (listing["id"],)).fetchone()
                eligible = listing["eligible"]
                is_event = eligible and (old is None or not old["eligible"] or old["price"] != listing["price"])
                self.db.execute("INSERT INTO seen VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET price=excluded.price, eligible=excluded.eligible",
                                (listing["id"], listing["price"], int(eligible)))
                if baseline and eligible:
                    initial_count += 1
                if not is_event:
                    continue
                kind = "baseline" if baseline else ("new" if old is None else "price_change")
                age = (detected_time - parse_time(listing["created_at"])).total_seconds() if listing["created_at"] else None
                cursor = self.db.execute("""INSERT INTO events
                    (id,weapon,name,price,seller,seller_status,created_at,detected_at,api_age_seconds,request_ms,kind,url,details_json,trading_message)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (listing["id"], listing["weapon"], listing["name"], listing["price"], listing["seller"],
                     listing["seller_status"], listing["created_at"], detected, age, request_ms, kind, listing["url"],
                     json.dumps({k: listing[k] for k in ("weapon_label", "full_name", "attributes", "rerolls", "mastery_level")}),
                     listing["trading_message"]))
                receipt = self.pending_aleca.pop((listing["id"], listing["price"]), None)
                if receipt is not None and kind != "baseline":
                    lead = (parse_time(receipt) - detected_time).total_seconds()
                    self.db.execute("UPDATE events SET aleca_seen_at=?,lead_seconds=? WHERE event_id=?",
                                    (receipt, lead, cursor.lastrowid))
                if kind != "baseline":
                    alerts += 1
                    self.discord.enqueue({**listing, "event_id": cursor.lastrowid, "kind": kind, "detected_at": detected})
                    print(json.dumps({"event": kind, "id": listing["id"], "price": listing["price"],
                                      "detected_at": detected, "mode": "demo" if self.demo else "live"}), flush=True)
            self.status.update(seeded=True, last_success_at=detected, request_ms=request_ms,
                               returned_count=len(auctions))
            if baseline:
                self.status["baseline_count"] = initial_count
            return alerts

    def state(self) -> dict[str, Any]:
        with self.lock:
            # Only the latest observation event for an auction appears in the UI.
            where, params = self.event_filter()
            rows = self.db.execute(f"""SELECT * FROM events WHERE ({where}) AND event_id IN
                (SELECT MAX(event_id) FROM events GROUP BY id) ORDER BY event_id DESC LIMIT 200""", params).fetchall()
            comparisons = [r[0] for r in self.db.execute(
                f"SELECT lead_seconds FROM events WHERE ({where}) AND lead_seconds IS NOT NULL AND kind != 'baseline'", params)]
            offers = []
            for row in rows:
                offer = dict(row)
                details = json.loads(offer.pop("details_json") or "{}")
                offer.update(details)
                watch = self.settings.watch_for(offer["weapon"])
                offer.setdefault("weapon_label", watch.label)
                offer.setdefault("full_name", f"{watch.label} {offer['name']}")
                offers.append(offer)
            return {
                "version": VERSION, "mode": "demo" if self.demo else "live", "config": self.settings.public(),
                "status": dict(self.status), "offers": offers, "discord": self.discord.state(),
                "comparison": {"count": len(comparisons), "median_lead_seconds": statistics.median(comparisons) if comparisons else None,
                               "pending_count": len(self.pending_aleca)},
                "clock": utc_now(),
            }

    def event_filter(self) -> tuple[str, tuple[Any, ...]]:
        return (" OR ".join("(weapon=? AND price BETWEEN ? AND ?)" for _ in self.settings.watches),
                tuple(value for watch in self.settings.watches for value in (watch.weapon, watch.min_price, watch.max_price)))

    def compare(self, auction_id: str, received_at: str | None = None, event_id: int | None = None) -> None:
        received = parse_time(received_at) if received_at else datetime.now(timezone.utc)
        if abs((received - datetime.now(timezone.utc)).total_seconds()) > 300:
            raise ValueError("Horloge du navigateur décalée de plus de 5 minutes : synchroniser le PC.")
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM events WHERE id=? ORDER BY event_id DESC LIMIT 1", (auction_id,)).fetchone()
            if row is None or row["kind"] == "baseline":
                raise ValueError("Choisir une nouvelle alerte, pas une annonce de référence.")
            if event_id is not None and event_id != row["event_id"]:
                raise ValueError("Le prix a changé : actualiser et comparer le même événement.")
            if row["aleca_seen_at"]:
                raise ValueError("Cette alerte a déjà été comparée.")
            lead = (received - parse_time(row["detected_at"])).total_seconds()
            self.db.execute("UPDATE events SET aleca_seen_at=?,lead_seconds=? WHERE event_id=?",
                            (received.isoformat(timespec="milliseconds"), lead, row["event_id"]))

    def record_aleca(self, auction_input: str, price: int, received_at: str | None = None) -> None:
        auction_id = auction_id_from_input(auction_input)
        if isinstance(price, bool) or not isinstance(price, int) or not any(w.min_price <= price <= w.max_price for w in self.settings.watches):
            raise ValueError("Le prix d'achat immédiat doit correspondre à la fourchette surveillée.")
        received = parse_time(received_at) if received_at else datetime.now(timezone.utc)
        if abs((received - datetime.now(timezone.utc)).total_seconds()) > 300:
            raise ValueError("Horloge décalée de plus de 5 minutes : synchroniser le PC.")
        with self.lock:
            if not self.status["seeded"]:
                raise ValueError("Attendre la première réponse de référence avant de comparer.")
            if self.status["phase"] in {"blocked", "error"}:
                raise ValueError("La collecte est arrêtée : rétablir l'accès avant de mémoriser de nouvelles alertes.")
            row = self.db.execute("SELECT * FROM events WHERE id=? ORDER BY event_id DESC LIMIT 1", (auction_id,)).fetchone()
            if row is not None and row["price"] == price:
                self.compare(auction_id, received.isoformat(), row["event_id"])
                return
            key = (auction_id, price)
            if key in self.pending_aleca:
                raise ValueError("L'heure Aleca de cette annonce et de ce prix est déjà mémorisée.")
            self.pending_aleca[key] = received.isoformat(timespec="milliseconds")

    def export_csv(self) -> str:
        with self.lock:
            where, params = self.event_filter()
            rows = self.db.execute(f"SELECT * FROM events WHERE ({where}) ORDER BY event_id", params).fetchall()
            columns = [d[0] for d in self.db.execute("SELECT * FROM events LIMIT 0").description]
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["mode", *columns])
            for row in rows:
                # Avoid spreadsheet formula execution in external seller/item strings.
                values = ["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in row]
                writer.writerow(["demo" if self.demo else "live", *values])
            return output.getvalue()

    def run(self) -> None:
        failures = 0
        previous_start: float | None = None
        while not self.stop_event.is_set():
            started = time.monotonic()
            with self.lock:
                self.status.update(last_poll_at=utc_now(), polls=self.status["polls"] + 1, next_poll_at=None,
                                   request_in_flight=True, backoff_seconds=0,
                                   phase="polling" if self.status["seeded"] else "starting",
                                   message="Requête API en cours…",
                                   last_attempt_interval_seconds=round(started - previous_start, 3) if previous_start is not None else None)
            previous_start = started
            attempt_clock = time.perf_counter()
            try:
                auctions, elapsed = self.client.search()
                self.ingest(auctions, elapsed)
                failures = 0
                delay = max(0, self.settings.interval_seconds - (time.monotonic() - started))
                with self.lock:
                    self.status.update(phase="watching", message="Réponse API valide reçue. La couverture et la fraîcheur du flux restent à vérifier.",
                                       successful_polls=self.status["successful_polls"] + 1,
                                       consecutive_failures=0, request_in_flight=False, last_attempt_ms=elapsed,
                                       last_error=None, last_error_kind=None, last_error_details=None)
                    self.status.update(getattr(self.client, "last_response_meta", {}))
            except ApiFailure as exc:
                failures += 1
                delay = max(exc.retry_after or 0, min(120, 2 ** min(failures, 7)), self.settings.interval_seconds)
                with self.lock:
                    self.status.update(phase="blocked" if exc.blocked else "backoff", message=str(exc),
                                       failed_polls=self.status["failed_polls"] + 1,
                                       consecutive_failures=failures, last_error=str(exc), last_error_at=utc_now(),
                                       last_error_kind=exc.kind, last_http_status=exc.http_status,
                                       last_error_details=safe_failure_details(exc),
                                       last_attempt_ms=exc.duration_ms if exc.duration_ms is not None else round((time.perf_counter() - attempt_clock) * 1000, 2),
                                       request_in_flight=False, backoff_seconds=0 if exc.blocked else delay)
                print(json.dumps({"event": "api_failure", "at": self.status["last_error_at"],
                                  "message": str(exc), "kind": exc.kind, "http_status": exc.http_status,
                                  "failed_attempts": failures, "retry_in_seconds": None if exc.blocked else delay}, ensure_ascii=False), flush=True)
                if exc.blocked:
                    self.stop_event.wait()
                    return
            except Exception:
                # A programming/storage failure is surfaced; do not spin or claim success.
                with self.lock:
                    self.status.update(phase="error", message="Erreur interne : surveillance arrêtée. Consulter le terminal.",
                                       request_in_flight=False, failed_polls=self.status["failed_polls"] + 1,
                                       last_error="Erreur interne : voir le terminal.", last_error_kind="internal", last_error_at=utc_now())
                import traceback
                traceback.print_exc()
                return
            with self.lock:
                self.status["next_poll_at"] = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat(timespec="milliseconds")
            self.stop_event.wait(delay)

    def start(self) -> None:
        self.discord.start()
        self.thread = threading.Thread(target=self.run, name="riven-poller", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)
        self.discord.close()
        self.client.close()
        self.db.close()
