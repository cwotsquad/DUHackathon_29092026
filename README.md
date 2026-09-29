# DUHackathon_29092026

Symulacja dronowego wsparcia ewakuacji do schronów – Rzeszów (widoki: RCB, Mieszkaniec, Policja).

## Uruchomienie

Wymagany **Python 3.11+** ([python.org](https://www.python.org/downloads/), przy instalacji zaznacz *Add python.exe to PATH*).

**Windows:** uruchom `start.bat` (dwuklik). Za pierwszym razem utworzy środowisko `.venv`
i zainstaluje zależności, potem uruchomi serwer i otworzy aplikację w Chrome.

| Polecenie | Działanie |
| --- | --- |
| `start.bat` | serwer lokalny + aplikacja w przeglądarce |
| `start.bat lan` | dodatkowo dostęp z sieci lokalnej (np. widok mieszkańca na telefonie) |
| `start.bat update` | ponowna instalacja zależności |

**Ręcznie (Windows / Linux / macOS):**

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Linux/macOS: .venv/bin/pip
.venv\Scripts\python server.py --open               # Linux/macOS: .venv/bin/python
```

Aplikacja: **http://localhost:8000** · zatrzymanie serwera: `Ctrl+C`.

Dane mapy domyślnej są w `cache/` – nie trzeba nic pobierać. Internet jest potrzebny
tylko do podkładu mapy i aktualnej pogody IMGW.
