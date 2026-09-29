"""Serwer: FastAPI + WebSocket. Symulacja działa w Pythonie, przeglądarka tylko rysuje.
Uruchom:  start.bat   albo   .venv\\Scripts\\python server.py [--open] [--lan] [--port 8000]"""
import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import threading
import webbrowser

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

import civil
import police
import map_loader
from model import World

IMGW_URL = "https://danepubliczne.imgw.pl/api/data/synop/station/rzeszow"
DATA = map_loader.load()
world = World(DATA)
clients: dict[WebSocket, dict] = {}   # połączenie -> {"cid": id} (id = użytkownik widoku mieszkańca)
state = {"running": False, "speed": 5}
TICK = 0.1  # s czasu rzeczywistego
STEP = 0.5  # s czasu symulacji na krok


async def sim_loop():
    acc, n = 0.0, 0
    while True:
        n += 1
        await asyncio.sleep(TICK)
        if state["running"]:
            acc += TICK * state["speed"]
            while acc >= STEP:
                world.step(STEP)
                acc -= STEP
        base = None
        for ws, info in list(clients.items()):
            base = base or {**world.snapshot(heat=n % 10 == 0), "running": state["running"], "speed": state["speed"]}
            # widok mieszkańca: własna pozycja, cel, trasa i stan schronów (osobno dla każdego połączenia)
            msg = json.dumps({**base, "civ": civil.snapshot(world, info["cid"])})
            try:
                await ws.send_text(msg)
            except Exception:
                clients.pop(ws, None)


def fetch_weather():
    """Aktualne dane synoptyczne IMGW-PIB (stacja Rzeszów-Jasionka)."""
    import ssl
    import urllib.request
    try:
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        ctx = None
    try:
        with urllib.request.urlopen(IMGW_URL, timeout=10, context=ctx) as r:
            j = json.load(r)
    except Exception as e:
        world.log("warn", f"IMGW niedostępne ({type(e).__name__}) – pogoda bez zmian")
        return
    f = lambda k: float(j[k]) if j.get(k) not in (None, "") else None
    world.set_weather(temp=f("temperatura"), wind=f("predkosc_wiatru"), wdir=int(f("kierunek_wiatru") or 0),
                      rain=f("suma_opadu"), hum=f("wilgotnosc_wzgledna"), press=f("cisnienie"),
                      src=f"IMGW – stacja {j['stacja']}", time=f"{j['data_pomiaru']} {j['godzina_pomiaru']}:00")


@contextlib.asynccontextmanager
async def lifespan(_):
    await asyncio.to_thread(fetch_weather)
    task = asyncio.create_task(sim_loop())
    if OPEN_URL:  # serwer za chwilę nasłuchuje – otwórz przeglądarkę
        threading.Timer(1.0, open_browser, [OPEN_URL]).start()
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/api/map")
def get_map(src: str | None = None):
    """Mapa i budynki; ?src=osm|bdot przełącza źródło budynków w symulacji."""
    if src in ("osm", "bdot") and src != world.src:
        world.set_buildings(src, map_loader.load_bdot() if src == "bdot" else DATA["buildings"])
    return {"center": DATA["center"], "bounds": world.bounds, "buildings": world.buildings, "src": world.src}


def handle(c, info):
    cmd = c.get("cmd")
    xy = lambda: map_loader.to_xy(c["lat"], c["lon"])
    if cmd == "me":
        civil.set_position(world, info["cid"], *xy())
    elif cmd == "walk":
        c0 = world.civ.get(info["cid"])
        if c0:
            c0.walking = bool(c["on"])
    elif cmd == "pol_send":
        police.dispatch(world, int(c["id"]))
    elif cmd == "pol_add":
        police.add_unit(world, *(xy() if "lat" in c else ()))
    elif cmd == "pol_remove":
        police.remove_unit(world)
    elif cmd == "pol_auto":
        world.pol_auto = bool(c["on"])
    elif cmd == "pol_done":
        police.done(world, int(c["id"]))
    elif cmd == "run":
        state["running"] = bool(c["on"])
    elif cmd == "speed":
        state["speed"] = max(1, min(60, int(c["v"])))
    elif cmd == "shelter":
        world.toggle_shelter(c["id"])
    elif cmd == "group":
        world.add_group(*xy(), int(c.get("n", 30)))
    elif cmd == "drone":
        world.add_drone(*xy())
    elif cmd == "random":
        world.random_groups(5)
    elif cmd == "reset":
        world.reset()
        state["running"] = False
    elif cmd == "demo":
        world.demo()
    elif cmd == "psp":
        world.load_psp(map_loader.load_psp(), c.get("mode", "24h"))
    elif cmd == "weather":
        world.set_weather(wind=float(c["wind"]), wdir=int(c["wdir"]), rain=float(c["rain"]),
                          temp=float(c["temp"]), hum=float(c["hum"]), src="scenariusz ręczny", time="")
    elif cmd == "config":
        world.night = bool(c.get("night", world.night))
        world.sensor = c.get("sensor", world.sensor)
        world.guide = bool(c.get("guide", world.guide))
        world.strategy = c.get("strategy", world.strategy)
        for k in ("dynamic", "two_stage", "rotation", "comms"):
            setattr(world, k, bool(c.get(k, getattr(world, k))))
        world.escort_mode = c.get("escort_mode", world.escort_mode)
        world.escort_n = max(1, int(c.get("escort_n", world.escort_n)))
        world.plan_drones()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    info = clients[ws] = {"cid": id(ws)}
    try:
        while True:
            c = json.loads(await ws.receive_text())
            if c.get("cmd") == "weather_fetch":
                await asyncio.to_thread(fetch_weather)
            else:
                handle(c, info)
    except WebSocketDisconnect:
        clients.pop(ws, None)
        world.civ.pop(info["cid"], None)


app.mount("/", StaticFiles(directory=pathlib.Path(__file__).parent / "web", html=True))

OPEN_URL = None


def open_browser(url):
    """Chrome (nowe okno), a jeśli go nie ma – domyślna przeglądarka."""
    cands = [shutil.which(n) for n in ("chrome", "google-chrome", "chromium", "chromium-browser")] + [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    exe = next((c for c in cands if c and os.path.isfile(c)), None)
    if exe:
        subprocess.Popen([exe, "--new-window", url])
    else:
        webbrowser.open(url)


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ewakuacja – Rzeszów: serwer symulacji")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--lan", action="store_true", help="dostęp z sieci lokalnej (np. widok mieszkańca na telefonie)")
    ap.add_argument("--open", action="store_true", help="otwórz aplikację w Chrome po starcie serwera")
    a = ap.parse_args()
    OPEN_URL = f"http://localhost:{a.port}" if a.open else None
    print(f"\n  Aplikacja:  http://localhost:{a.port}")
    if a.lan:
        print(f"  W sieci lokalnej (telefon):  http://{lan_ip()}:{a.port}")
    print("  Zatrzymanie serwera: Ctrl+C\n")
    uvicorn.run(app, host="0.0.0.0" if a.lan else "127.0.0.1", port=a.port)
