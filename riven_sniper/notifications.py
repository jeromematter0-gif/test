"""Persistent Discord outbox. Network I/O is always outside the collector's lock."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .core import VERSION, retry_delay, utc_now, verified_ssl_context


def validate_webhook(value: str) -> str:
    try:
        if not isinstance(value, str) or len(value) > 600:
            raise ValueError()
        parsed = urlsplit(value.strip())
        if (parsed.scheme != "https" or parsed.hostname not in {"discord.com", "canary.discord.com", "ptb.discord.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or not re.fullmatch(r"/api/(?:v\d+/)?webhooks/\d{1,30}/[A-Za-z0-9_.-]{20,200}", parsed.path)):
            raise ValueError()
        return value.strip()
    except (ValueError, TypeError):
        # Never echo even a fragment of the secret webhook URL.
        raise ValueError("Coller l'adresse HTTPS complète d'un webhook discord.com, sans paramètres ajoutés.") from None


def code_block(value: str) -> str:
    return "```text\n" + value.replace("`", "").replace("\r", " ").replace("\n", " ")[:1000] + "\n```"


def discord_payload(event: dict[str, Any], *, demo: bool = False) -> dict[str, Any]:
    prefix = "[SIMULATION] " if demo else ""
    title = str(event.get("full_name") or event.get("name") or "Riven")[:240]
    fields = [
        {"name": "Arme", "value": str(event.get("weapon_label") or event.get("weapon"))[:100], "inline": True},
        {"name": "Prix immédiat", "value": f"{event['price']} pl", "inline": True},
        {"name": "Vendeur", "value": "`" + str(event.get("seller") or "Inconnu")[:100].replace("`", "") + "`", "inline": True},
        {"name": "Statut", "value": {"ingame": "En jeu", "online": "En ligne", "offline": "Hors ligne"}.get(event.get("seller_status"), "Inconnu"), "inline": True},
        {"name": "MR", "value": str(event["mastery_level"]) if event.get("mastery_level") is not None else "Non fourni", "inline": True},
        {"name": "Rolls", "value": str(event["rerolls"]) if event.get("rerolls") is not None else "Non fourni", "inline": True},
    ]
    attributes = []
    for attr in event.get("attributes", [])[:8]:
        sign = "−" if attr["value"] < 0 or not attr.get("positive", True) else "+"
        attributes.append(f"{sign}{abs(attr['value']):g} · {attr['name']}")
    fields.append({"name": "Attributs", "value": "\n".join(attributes)[:1000] or "Non fournis par le flux", "inline": False})
    fields.append({"name": "Détecté via", "value": str(event.get("source_label") or "Flux récent global")[:200], "inline": False})
    message = event.get("trading_message")
    if message:
        fields.append({"name": "Message à copier", "value": code_block(message), "inline": False})
    embed = {"title": prefix + title, "color": 0xB174D9, "fields": fields,
             "footer": {"text": "Riven Sniper · PC · " + ("Offre mise à jour" if event.get("kind") == "price_change" else "Offre détectée")}}
    if event.get("url") and not demo:
        embed["url"] = event["url"]
    if event.get("detected_at"):
        embed["timestamp"] = event["detected_at"]
    return {"username": "Riven Sniper", "content": prefix + "Riven correspondant à votre recherche détecté.",
            "allowed_mentions": {"parse": []}, "embeds": [embed]}


class DiscordFailure(Exception):
    def __init__(self, message: str, *, retry_after: float | None = None, permanent: bool = False, blocked: bool = False):
        super().__init__(message)
        self.retry_after, self.permanent, self.blocked = retry_after, permanent, blocked


class DiscordDispatcher:
    TTL_SECONDS = 600

    def __init__(self, db: sqlite3.Connection, lock: Any, *, config_path: Path | None = None,
                 demo: bool = False, transport: httpx.BaseTransport | None = None, clock: Any = time.time):
        self.db, self.lock, self.config_path = db, lock, config_path
        self.demo, self.transport, self.clock = demo, transport, clock
        self._url: str | None = None
        self.enabled = False
        self.blocked = False
        self._generation = 0
        self.not_before = 0.0
        self._client: httpx.Client | None = None
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.thread: threading.Thread | None = None
        self.status = {"phase": "unconfigured", "message": "Coller votre webhook localement pour recevoir les prochaines alertes.",
                       "last_error": None, "last_sent_at": None}
        self.db.execute("""CREATE TABLE IF NOT EXISTS discord_outbox (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL, next_attempt REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending', last_error TEXT
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS discord_pending ON discord_outbox(state,next_attempt,created)")
        self.db.commit()
        if config_path is not None and config_path.exists():
            try:
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self._url = validate_webhook(saved["webhook_url"]) if saved.get("webhook_url") else None
                self.enabled = saved.get("enabled") is True and self._url is not None
                self.status.update(phase="idle" if self.enabled else "disabled", message="Discord configuré." if self._url else "Webhook non configuré.")
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self._url, self.enabled = None, False
                self.status.update(phase="error", last_error="Configuration Discord illisible. Enregistrer à nouveau le webhook.")

    def configure(self, data: dict[str, Any]) -> None:
        if "enabled" in data and not isinstance(data["enabled"], bool):
            raise ValueError("L'activation Discord doit être oui ou non.")
        if "clear" in data and not isinstance(data["clear"], bool):
            raise ValueError("Réglage invalide.")
        with self.lock:
            new_url = self._url
            supplied = data.get("webhook_url")
            if supplied is not None:
                if not isinstance(supplied, str):
                    raise ValueError("Adresse de webhook invalide.")
                if supplied.strip():
                    new_url = validate_webhook(supplied)
            enabled = data.get("enabled", self.enabled)
            if data.get("clear"):
                new_url, enabled = None, False
            if enabled and not new_url:
                raise ValueError("Ajouter le webhook avant d'activer Discord.")
            if self.config_path:
                self.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.config_path.parent, delete=False) as output:
                        temporary = Path(output.name)
                        json.dump({"webhook_url": new_url, "enabled": enabled}, output)
                    os.replace(temporary, self.config_path)
                except OSError:
                    raise ValueError("Impossible d'enregistrer la configuration locale Discord.") from None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            if new_url != self._url:
                with self.db:
                    self.db.execute("UPDATE discord_outbox SET state='cancelled' WHERE state='pending'")
            self._url, self.enabled, self.blocked = new_url, enabled, False
            self._generation += 1
            self.status.update(phase="idle" if enabled else "disabled" if new_url else "unconfigured",
                               message="Prêt à envoyer les prochaines alertes." if enabled else "Envoi Discord désactivé.", last_error=None)
        self.wake.set()

    def enqueue(self, event: dict[str, Any]) -> bool:
        """Called inside the event insertion transaction, while its lock is held."""
        if not self.enabled or not self._url:
            return False
        self.db.execute("INSERT OR IGNORE INTO discord_outbox(id,payload,created,next_attempt) VALUES (?,?,?,?)",
                        (f"event:{event['event_id']}", json.dumps(discord_payload(event, demo=self.demo)), self.clock(), self.clock()))
        self.wake.set()
        return True

    def test_message(self) -> None:
        with self.lock, self.db:
            if not self.enabled or not self._url:
                raise ValueError("Enregistrer et activer Discord avant le test.")
            payload = {"username": "Riven Sniper", "content": "Test de connexion Riven Sniper" + (" [SIMULATION]" if self.demo else ""),
                       "allowed_mentions": {"parse": []}, "embeds": [{"title": "Discord est prêt à recevoir les prochaines offres",
                       "description": "Ce message est un test, pas une annonce. La collecte reste indépendante de l'envoi Discord.", "color": 0xA9CD68}]}
            self.db.execute("INSERT INTO discord_outbox(id,payload,created,next_attempt) VALUES (?,?,?,?)",
                            ("test:" + uuid.uuid4().hex, json.dumps(payload), self.clock(), self.clock()))
        self.wake.set()

    def state(self) -> dict[str, Any]:
        with self.lock:
            counts = dict(self.db.execute("SELECT state,COUNT(*) FROM discord_outbox GROUP BY state").fetchall())
            return {**self.status, "configured": self._url is not None, "enabled": self.enabled,
                    "pending_count": counts.get("pending", 0), "sent_count": counts.get("sent", 0),
                    "failed_count": counts.get("failed", 0), "expired_count": counts.get("expired", 0),
                    "retry_in_seconds": max(0, round(self.not_before - self.clock(), 1))}

    def send(self, url: str, payload: dict[str, Any]) -> float:
        if self._client is None:
            self._client = httpx.Client(verify=verified_ssl_context(), timeout=httpx.Timeout(8, connect=5),
                                        follow_redirects=False, transport=self.transport,
                                        headers={"User-Agent": f"RivenSniper/{VERSION}"})
        try:
            response = self._client.post(url, params={"wait": "true"}, json=payload)
        except httpx.HTTPError:
            raise DiscordFailure("Connexion Discord impossible : vérifier réseau et certificats.") from None
        if response.status_code == 429:
            delay = retry_delay(response.headers.get("Retry-After"))
            try:
                value = response.json().get("retry_after")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    delay = max(delay or 0, value)
            except (ValueError, TypeError, AttributeError):
                pass
            raise DiscordFailure("Discord limite temporairement les envois (429).", retry_after=max(0.5, delay or 2))
        if response.status_code not in (200, 204):
            raise DiscordFailure(f"Discord a refusé le message (HTTP {response.status_code}). Vérifier le webhook et les droits du salon.",
                                 permanent=300 <= response.status_code < 500,
                                 blocked=300 <= response.status_code < 400 or response.status_code in (401, 403, 404))
        return max(0.5, retry_delay(response.headers.get("X-RateLimit-Reset-After")) or 0) if response.headers.get("X-RateLimit-Remaining") == "0" else 0.5

    def run_once(self) -> float:
        with self.lock:
            if not self.enabled or not self._url or self.blocked:
                return 0.5
            now = self.clock()
            if now < self.not_before:
                return self.not_before - now
            with self.db:
                self.db.execute("UPDATE discord_outbox SET state='expired' WHERE state='pending' AND created<?", (now - self.TTL_SECONDS,))
            row = self.db.execute("SELECT * FROM discord_outbox WHERE state='pending' AND next_attempt<=? ORDER BY created,id LIMIT 1", (now,)).fetchone()
            if row is None:
                self.status.update(phase="idle", message="En attente des prochaines alertes.")
                return 0.5
            row_id, url, generation = row["id"], self._url, self._generation
            payload = json.loads(row["payload"])
            self.status.update(phase="sending", message="Envoi d'une alerte Discord…")
        try:
            # Critical: neither the SQLite transaction nor the collector's lock is held here.
            cooldown = self.send(url, payload)
        except DiscordFailure as exc:
            with self.lock, self.db:
                delay = max(exc.retry_after or 0, min(60, 2 ** min(row["attempts"] + 1, 6)))
                self.db.execute("UPDATE discord_outbox SET attempts=attempts+1,state=?,last_error=?,next_attempt=? WHERE id=? AND state='pending'",
                                ("failed" if exc.permanent else "pending", str(exc), self.clock() + delay, row_id))
                self.not_before = self.clock() + delay
                if generation == self._generation:
                    self.blocked = exc.blocked
                    self.status.update(phase="error" if exc.blocked else "backoff", message=str(exc), last_error=str(exc))
            return delay
        with self.lock, self.db:
            self.db.execute("UPDATE discord_outbox SET state='sent',attempts=attempts+1,last_error=NULL WHERE id=? AND state='pending'", (row_id,))
            self.not_before = self.clock() + cooldown
            if generation == self._generation:
                self.status.update(phase="idle", message="Dernière alerte Discord envoyée.", last_error=None, last_sent_at=utc_now())
        return cooldown

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                delay = self.run_once()
            except Exception:
                # An arbitrary exception may contain the webhook secret. Do not dump it.
                with self.lock:
                    self.blocked = True
                    self.status.update(phase="error", last_error="Erreur d'envoi interne. Réenregistrer la configuration Discord.")
                delay = 0.5
            self.wake.wait(max(0.01, delay))
            self.wake.clear()

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="discord-outbox", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=30)
        if self._client:
            self._client.close()
