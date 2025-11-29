import asyncio
import json
import ssl
import time
from threading import Thread
from typing import Dict

import certifi
import requests
import websockets
from flask import Flask, render_template, jsonify


class SamsTicker:
    def __init__(self):
        self.team_id = "3b1fa79e-1276-4496-9e55-5366d60df69a"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        })

        self.matches: Dict[str, dict] = {}
        self.active_match = {
            "id"     : None,
            "team1"  : '',
            "team2"  : '',
            "score1" : 0,
            "score2" : 0,
            "set1"   : 0,
            "set2"   : 0,
            "serving": 0
        }

        # Flask app setup
        self.app = Flask(__name__)
        self.setup_routes()

    def setup_routes(self):
        @self.app.route('/')
        def index():
            return render_template('/index.html')

        @self.app.route('/api/match')
        def get_match():
            return jsonify(self.active_match)

    def run_web_server(self):
        self.app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)

    def open_sams_ticker(self):
        response = self.session.get('https://vvb.sams-ticker.de/')
        return response

    def get_matches(self):
        response = self.session.get('https://backend.sams-ticker.de/live/indoor/tickers/vvb')
        json_data = response.json()

        for day in json_data['matchDays']:
            for match in day['matches']:
                if self.team_id in match.get('team1', {}) or self.team_id in match.get('team2', {}) and match.get(
                        'date', 0) > (time.time() - 3 * 60 * 60) * 1000:  # todo
                    self.matches[match['id']] = match

    def init_match(self):
        if not self.matches:
            return

        current_time = time.time() * 1000
        closest_match = min(
            self.matches.values(),
            key=lambda m: abs(m.get('date', float('inf')) - current_time)
        )

        self.active_match['id'] = closest_match['id']
        self.active_match['team1'] = closest_match.get('teamDescription1', 'Team 1')
        self.active_match['team2'] = closest_match.get('teamDescription2', 'Team 2')

        self.active_match['set1'] = 0
        self.active_match['set2'] = 0
        self.active_match['score1'] = 0
        self.active_match['score2'] = 0

    async def connect_to_websocket(self):
        ssl_context = ssl.create_default_context(cafile=certifi.where())

        while True:
            try:
                async with websockets.connect(
                        'wss://backend.sams-ticker.de/indoor/vvb',
                        additional_headers={
                            "Origin"    : "https://vvb.sams-ticker.de",
                            "User-Agent": self.session.headers['User-Agent'],
                        },
                        ssl=ssl_context
                ) as websocket:
                    async for message in websocket:
                        try:
                            update_data = json.loads(message)

                            if update_data.get('type') == 'MATCH_UPDATE':
                                payload = update_data.get('payload', {})
                                match_uuid = payload.get('matchUuid')

                                # Only process if this matchSeriesUuid is one we're tracking
                                if match_uuid and match_uuid in self.matches.keys():
                                    self.active_match['set1'] = payload['setPoints'].get('team1', 0)
                                    self.active_match['set2'] = payload['setPoints'].get('team2', 0)
                                    self.active_match['score1'] = payload['matchSets'][-1]['setScore'].get('team1', 0)
                                    self.active_match['score2'] = payload['matchSets'][-1]['setScore'].get('team2', 0)
                                    self.active_match['serving'] = 0 if payload.get('serving', 'team1') == 'team1' else 1

                                    if payload.get('finalized', False):
                                        self.matches.pop(match_uuid)
                                        self.init_match()

                                    print(f"Websocket Update für unser Spiel: {self.active_match}")
                            else:
                                print(f"Websocket Update (other type): {update_data}")

                        except json.JSONDecodeError as e:
                            print(f"Error parsing websocket message: {e}")
                        except Exception as e:
                            print(f"Error processing websocket update: {e}")
            except Exception as e:
                print(f"Websocket connection error: {e}, retrying in 5 seconds...")
                await asyncio.sleep(5)

    async def main(self):
        # Start Flask server in a separate thread for debug purposes
        # web_thread = Thread(target=self.run_web_server, daemon=True)
        # web_thread.start()
        # print("Web-Server läuft auf http://localhost:5050")

        print("Starte Sams Ticker Client...")
        self.open_sams_ticker()
        self.get_matches()
        print("Gefundene Matches:", self.matches)

        while not self.matches:
            print("Keine Matches gefunden. Warte 1 Minute...")
            await asyncio.sleep(60)

        self.init_match()
        print("Aktives Match:", self.active_match)

        while True:
            await self.connect_to_websocket()

    def start_background_tasks(self):
        """Startet die Websocket-Verbindung im Hintergrund"""
        def run_async():
            asyncio.run(self.main())
        
        thread = Thread(target=run_async, daemon=True)
        thread.start()
        print("Websocket-Verbindung im Hintergrund gestartet")


# Erstelle globale Instanz für Gunicorn
ticker_instance = None
try:
    ticker_instance = SamsTicker()
except Exception as e:
    print(f"Fehler beim Initialisieren der App: {e}")
    import traceback
    traceback.print_exc()

# Stelle sicher, dass app immer definiert ist
if ticker_instance:
    app = ticker_instance.app
else:
    # Fallback: Erstelle eine minimale Flask-App
    app = Flask(__name__)
    @app.route('/')
    def index():
        return "Fehler beim Initialisieren der App", 500
    @app.route('/api/match')
    def get_match():
        return jsonify({"error": "App nicht initialisiert"}), 500

# Starte Websocket-Verbindung im Hintergrund (für Production mit Gunicorn)
# Nur starten wenn nicht im __main__ Modus (dort wird es separat gestartet)
if __name__ != '__main__' and ticker_instance:
    try:
        ticker_instance.start_background_tasks()
    except Exception as e:
        print(f"Fehler beim Starten der Hintergrund-Tasks: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    # Lokaler Test-Modus mit Flask's Entwicklungsserver
    if ticker_instance:
        ticker_instance.start_background_tasks()
    try:
        app.run(host='0.0.0.0', port=10000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down...")



