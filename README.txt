DUHackathon_29092026
====================

Symulacja dronowego wsparcia ewakuacji do schronow - Rzeszow
(widoki: RCB, Mieszkaniec, Policja).


URUCHOMIENIE
------------

Wymagany Python 3.11+ (https://www.python.org/downloads/,
przy instalacji zaznacz "Add python.exe to PATH").

Windows: uruchom start.bat (dwuklik). Za pierwszym razem utworzy srodowisko
.venv i zainstaluje zaleznosci, potem uruchomi serwer i otworzy aplikacje w Chrome.

  start.bat           serwer lokalny + aplikacja w przegladarce
  start.bat lan       dodatkowo dostep z sieci lokalnej (np. widok mieszkanca na telefonie)
  start.bat update    ponowna instalacja zaleznosci

Recznie (Windows / Linux / macOS):

  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt     (Linux/macOS: .venv/bin/pip)
  .venv\Scripts\python server.py --open              (Linux/macOS: .venv/bin/python)

Aplikacja: http://localhost:8000     Zatrzymanie serwera: Ctrl+C

Dane mapy domyslnej sa w katalogu cache/ - nie trzeba nic pobierac.
Internet jest potrzebny tylko do podkladu mapy i aktualnej pogody IMGW.
