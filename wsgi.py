from main import SamsTicker
import threading
import asyncio

# Erstelle eine Instanz der SamsTicker Klasse
ticker_instance = SamsTicker()

# Exportiere die Flask-App für Gunicorn
app = ticker_instance.app

# Starte den WebSocket-Handler im Hintergrund
def run_websocket():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # Initialisiere Matches
    ticker_instance.open_sams_ticker()
    ticker_instance.get_matches()
    
    if ticker_instance.matches:
        ticker_instance.init_match()
    
    # Starte WebSocket-Verbindung
    loop.run_until_complete(ticker_instance.connect_to_websocket())

ws_thread = threading.Thread(target=run_websocket, daemon=True)
ws_thread.start()

