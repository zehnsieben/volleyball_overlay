"""SAMS overlay with a persisted schedule and a time-controlled live connection."""

import argparse
import asyncio
import json
import logging
import ssl
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from zoneinfo import ZoneInfo

import certifi
import requests
import websockets
from flask import Flask, abort, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent
TEAM_IDS = {"4522cdd8-3c47-4248-8854-3335c7246549"}
TICKER_URL = "https://backend.sams-ticker.de/live/indoor/tickers/vvb"
WEBSOCKET_URL = "wss://backend.sams-ticker.de/indoor/vvb"
LEAD_TIME = HOLD_TIME = 30 * 60
CHECK_INTERVAL = 15
LOG = logging.getLogger(__name__)


def last_friday(now):
    date = datetime.fromtimestamp(now, ZoneInfo("Europe/Berlin"))
    return (date - timedelta(days=(date.weekday() - 4) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def next_refresh(refreshed_at, now):
    friday = last_friday(now)
    return (friday + timedelta(days=7)).timestamp() if refreshed_at >= friday.timestamp() else now


def save_json(path, data):
    """Readers see either the previous or the complete new snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def fetch_ticker(session):
    response = session.get(TICKER_URL, timeout=(5, 15))
    response.raise_for_status()
    return response.json()


def upcoming_matches(data, now, team_ids=TEAM_IDS):
    """Keep exact team IDs and all future dated matches supplied by the source."""
    matches = {}
    for day in data["matchDays"]:
        for match in day["matches"]:
            if not team_ids.intersection((match.get("team1"), match.get("team2"))):
                continue
            if match.get("indefinitelyRescheduled") or match.get("date", 0) < now * 1000:
                continue
            matches[match["id"]] = {key: match.get(key) for key in (
                "id", "team1", "team2", "teamDescription1", "teamDescription2",
                "date", "matchSeries", "delayPossible",
            )}
    return sorted(matches.values(), key=lambda match: (match["date"], match["id"]))


def empty_score(match=None):
    match = match or {}
    return {
        "id": match.get("id"),
        "team1": match.get("teamDescription1", ""),
        "team2": match.get("teamDescription2", ""),
        "score1": 0, "score2": 0, "set1": 0, "set2": 0,
        "serving": None, "status": "upcoming" if match else "empty",
    }


class MatchQueue:
    """Scheduling decisions without network calls or sleeps."""

    def __init__(self, matches, state=None):
        state = state or {}
        self.completed = set(state.get("completed", []))
        self.matches = sorted(
            (match for match in matches if match["id"] not in self.completed),
            key=lambda match: (match["date"], match["id"]),
        )
        self.score = empty_score(self.current)
        self.hold_until = None
        if self.current and state.get("score", {}).get("id") == self.current["id"]:
            self.score = state["score"]
            self.hold_until = state.get("hold_until")

    @property
    def current(self):
        return self.matches[0] if self.matches else None

    def advance(self, now):
        if self.hold_until is not None and now >= self.hold_until:
            self.completed.add(self.matches.pop(0)["id"])
            self.hold_until = None
            self.score = empty_score(self.current)
            return True
        return False

    def wait_seconds(self, now):
        if self.hold_until is not None:
            return max(0, self.hold_until - now)
        if self.current:
            return max(0, self.current["date"] / 1000 - LEAD_TIME - now)
        return None

    def apply(self, state, now):
        if not self.current or state.get("matchUuid") != self.current["id"]:
            return False
        if self.hold_until is not None:
            return False
        finished = state.get("finished") or state.get("finalized")
        if not (state.get("ready") or state.get("started") or finished):
            return False
        sets = state.get("matchSets") or []
        points = sets[-1].get("setScore", {}) if sets else {}
        set_points = state.get("setPoints") or {}
        self.score = {
            **empty_score(self.current),
            "score1": points.get("team1", 0), "score2": points.get("team2", 0),
            "set1": set_points.get("team1", 0), "set2": set_points.get("team2", 0),
            "serving": {"team1": 0, "team2": 1}.get(state.get("serving")),
            "status": "finished" if finished else "active",
        }
        if finished:
            self.hold_until = now + HOLD_TIME
        return True

    def snapshot(self):
        return {"completed": sorted(self.completed), "score": self.score,
                "hold_until": self.hold_until}

    def refresh(self, data, now):
        # Merge by ID: a limited ticker window must not erase later games.
        current = self.current if self.score["status"] in ("active", "finished") else None
        matches = {match["id"]: match for match in self.matches}
        for day in data["matchDays"]:
            for match in day["matches"]:
                if match.get("indefinitelyRescheduled"):
                    matches.pop(match["id"], None)
        matches.update({match["id"]: match for match in upcoming_matches(data, now)})
        self.matches = sorted(
            (match for match in matches.values() if match["id"] not in self.completed
             and (not current or match["id"] != current["id"])),
            key=lambda match: (match["date"], match["id"]),
        )
        if current:
            self.matches.insert(0, current)
        else:
            self.score = empty_score(self.current)


class SamsTicker:
    def __init__(self, queue, state_path, schedule_path=None, refreshed_at=None):
        self.queue = queue
        self.state_path = state_path
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "VolleyballOverlay/1.0"
        self.lock = Lock()
        self.display = dict(queue.score)
        self.schedule_path = schedule_path
        now = time.time()
        self.refresh_at = next_refresh(now if refreshed_at is None else refreshed_at, now)

    async def refresh_schedule(self):
        data = await asyncio.to_thread(fetch_ticker, self.session)
        now = time.time()
        self.queue.refresh(data, now)
        save_json(self.schedule_path, {"matches": self.queue.matches, "refreshed_at": now})
        self.publish()
        self.refresh_at = next_refresh(now, now)
        LOG.info("Spielplan aktualisiert: %d Spiele", len(self.queue.matches))

    def publish(self):
        save_json(self.state_path, self.queue.snapshot())
        with self.lock:
            self.display = dict(self.queue.score)

    def get_score(self):
        with self.lock:
            return dict(self.display)

    async def read_current_state(self):
        data = await asyncio.to_thread(fetch_ticker, self.session)
        match_id = self.queue.current["id"]
        state = data.get("matchStates", {}).get(match_id)
        if state and self.queue.apply({**state, "matchUuid": match_id}, time.time()):
            self.publish()

    async def follow_live(self):
        """Open only while active; snapshot AFTER connect closes the update gap."""
        async with websockets.connect(
            WEBSOCKET_URL, origin="https://vvb.sams-ticker.de",
            ssl=ssl.create_default_context(cafile=certifi.where()),
            open_timeout=15, close_timeout=5,
        ) as socket:
            await self.read_current_state()
            refresh_at = time.monotonic() + 60
            while self.queue.hold_until is None and time.time() < self.refresh_at:
                try:
                    message = await asyncio.wait_for(
                        socket.recv(), timeout=max(0, min(
                            refresh_at - time.monotonic(), self.refresh_at - time.time()))
                    )
                except asyncio.TimeoutError:
                    if time.time() >= self.refresh_at:
                        return
                    await self.read_current_state()
                    refresh_at = time.monotonic() + 60
                    continue
                try:
                    event = json.loads(message)
                    if event.get("type") == "MATCH_UPDATE":
                        if self.queue.apply(event.get("payload", {}), time.time()):
                            self.publish()
                except (ValueError, TypeError, KeyError, AttributeError):
                    LOG.warning("Ungültiges Live-Update ignoriert", exc_info=True)

    async def run(self):
        while True:
            if time.time() >= self.refresh_at:
                try:
                    await self.refresh_schedule()
                except (requests.RequestException, OSError, ValueError, KeyError):
                    LOG.warning("Spielplanabruf fehlgeschlagen; neuer Versuch in 15 Minuten", exc_info=True)
                    self.refresh_at = time.time() + 15 * 60
            if self.queue.advance(time.time()):
                self.publish()
                continue
            delay = self.queue.wait_seconds(time.time())
            weekly_delay = max(0, self.refresh_at - time.time())
            delay = weekly_delay if delay is None else min(delay, weekly_delay)
            if delay:
                LOG.info("Warte %.0f Sekunden; Anzeige: %s", delay, self.queue.score["status"])
                await asyncio.sleep(delay)
                continue
            if not self.queue.current:
                continue
            try:
                await self.read_current_state()
                if self.queue.hold_until is not None:
                    continue
                if self.queue.score["status"] == "active":
                    await self.follow_live()
                    continue
            except (requests.RequestException, OSError, ValueError,
                    websockets.exceptions.WebSocketException, TimeoutError):
                LOG.warning("SAMS nicht erreichbar; erneuter Versuch in 15 Sekunden", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)


def create_app(ticker):
    app = Flask(__name__, static_folder=None)

    @app.before_request
    def read_only():
        if request.method not in ("GET", "HEAD"):
            abort(405)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/match")
    def get_match():
        response = jsonify(ticker.get_score())
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "serve"))
    parser.add_argument("--schedule", type=Path, default=ROOT / "schedule.json")
    parser.add_argument("--state", type=Path, default=ROOT / "runtime.json")
    parser.add_argument("--source", type=Path, help="SAMS-JSON mit vollständigem matchDays-Spielplan")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "prepare":
        if args.source:
            data = json.loads(args.source.read_text())
        else:
            with requests.Session() as session:
                data = fetch_ticker(session)
            LOG.warning("Ticker liefert nur ein Zeitfenster, NICHT den vollständigen Saisonspielplan: %s",
                        data.get("settings", {}).get("timeInterval"))
        matches = upcoming_matches(data, time.time())
        if not matches:
            parser.error("Keine zukünftigen Spiele gefunden; bestehender Spielplan bleibt erhalten.")
        save_json(args.schedule, {"matches": matches, "refreshed_at": time.time()})
        LOG.info("%d kommende Spiele in %s gespeichert", len(matches), args.schedule)
        return

    schedule = (json.loads(args.schedule.read_text()) if args.schedule.exists()
                else {"matches": [], "refreshed_at": 0})
    state = json.loads(args.state.read_text()) if args.state.exists() else None
    ticker = SamsTicker(MatchQueue(schedule["matches"], state), args.state,
                        args.schedule, schedule.get("refreshed_at", 0))
    Thread(target=lambda: asyncio.run(ticker.run()), daemon=True).start()
    from waitress import serve
    serve(create_app(ticker), host=args.host, port=args.port, threads=2)


if __name__ == "__main__":
    main()
