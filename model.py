"""Model symulacji: grupy ludzi szukające schronów, drony rozpoznawcze, centrum zarządzania kryzysowego.

Przepływ informacji: dron wykrywa grupę -> raport do Centrum (pozycja, szacowana liczebność)
-> Centrum dobiera schron z wolnym miejscem (z rezerwacją) -> dron przekazuje komunikat grupie."""
import math
import random
from dataclasses import dataclass, field

import networkx as nx
import numpy as np

import civil
import police
from map_loader import to_latlon, to_xy
from search import ProbGrid, lawnmower, spiral, split_rect

WALK = 1.3            # prędkość pieszego [m/s]
VIS_DAY, VIS_NIGHT = 35, 15   # zasięg wzroku człowieka [m]
MERGE_R = 12          # odległość łączenia grup [m]
ATTRACT_R = 70        # grupy zauważają się nawzajem [m]
DRONE_SPEED = 10
SCOUT_SPEED = 14      # dron zwiadowczy (wyżej, szybciej)
SCOUT_RANGE = 1.4     # większa wysokość -> szerszy obraz
MAX_WIND = 12         # [m/s] graniczna prędkość wiatru dla lotów
COMM_R = 700          # [m] zasięg łącza radiowego dron-dron (sieć mesh, wieloskokowa)
HOT_R = 150           # [m] promień lokalnej spirali wokół punktu o wysokim prawdopodobieństwie
LOST_P = .015         # szansa zgubienia trasy przez skierowaną grupę na każdym skrzyżowaniu (stres, panika)
# 5 schronów Rzeszowa (dane z komunikatów miasta) – prawdziwa pojemność; pozostałe punkty PSP to miejsca ochronne
KNOWN_SCHRONY = {"ul. Jana III Sobieskiego 9": 57, "pl. Ofiar Getta 6": 41, "al. Piłsudskiego Józefa 33": 73,
                 "ul. Żeromskiego Stefana 2": 44, "ul. Hetmańska 120": 46}
RETRY_T = 20          # [s] co ile dron ponawia skierowanie wykrytej, błądzącej grupy
ESCORT_T = 45         # [s] czas zawisu nad grupą (tryb "hover")
VERIFY_T = 30         # [s] szukania w miejscu zgłoszenia zwiadu
FLIGHT_TIME = 25 * 60  # czas lotu na pełnej baterii [s]
SWAP_TIME = 3 * 60     # wymiana baterii w bazie [s]
RTB_MARGIN = 8         # zapas baterii przy powrocie [%]
SENSOR = {("rgb", False): 70, ("rgb", True): 25, ("ir", False): 55, ("ir", True): 60}


@dataclass
class Shelter:
    id: str
    name: str
    x: float
    y: float
    node: int
    cap: int
    n: int = 0
    reserved: int = 0
    alerted: bool = False
    addr: str = ""
    kind: str = "oznaczony"   # oznaczony | schron | miejsce ochronne
    access: str = ""          # dostępność wg PSP
    src: str = "user"         # user | psp

    @property
    def free(self):
        return self.cap - self.n


@dataclass
class Group:
    id: int
    n: int
    cur: int
    nxt: int
    x: float
    y: float
    prev: int = -1
    target: Shelter | None = None
    path: list = field(default_factory=list)
    bad: set = field(default_factory=set)
    guided: bool = False
    detected: bool = False
    seen: bool = False
    t_try: float = -1e9   # ostatnia próba skierowania do schronu (wykryta, ale bez przydziału)
    confirmed: bool = False
    res: int | None = None   # aktywna rezerwacja miejsca w schronie
    police: int | None = None  # patrol prowadzący grupę  # dron potwierdził zrozumienie komunikatu (zawis)    # kiedykolwiek wykryta (do statystyki)


@dataclass
class Drone:
    id: int
    x: float
    y: float
    bx: float = 0.0       # baza (miejsce startu/lądowania)
    by: float = 0.0
    wps: list = field(default_factory=list)
    wi: int = 0
    bat: float = 100.0    # [%]
    state: str = "patrol"  # patrol | rtb | charge
    charge: float = 0.0   # pozostały czas wymiany baterii [s]
    det: int = 0          # liczba wykrytych grup
    dist: float = 0.0     # przelecany dystans [m]
    role: str = "std"     # std | scout (zwiad RGB) | insp (weryfikacja IR)
    sector: tuple = (0.0, 0.0)
    rect: tuple | None = None   # sektor kosiarki (x0, x1, y0, y1)
    goal: tuple | None = None   # cel w strategii "mapa prawdopodobieństwa"
    follow: int | None = None   # eskortowana grupa
    follow_t: float = 0.0
    lead: bool = False          # odprowadza grupę aż do schronu
    task: int | None = None     # zgłoszenie zwiadu do weryfikacji
    task_t: float = 0.0
    asg: dict = field(default_factory=dict)  # rejestr przydziałów: aid -> (schron, szac. liczba os., czas)
    obs: dict = field(default_factory=dict)  # obserwacje zajętości: schron -> (liczba os., czas)
    net: tuple = ()                          # drony w tej samej sieci łączności


class World:
    def __init__(self, data, seed=1):
        self.data = data
        G = nx.Graph()
        for i, j, w in data["edges"]:
            G.add_edge(i, j, w=w)
        G = G.subgraph(max(nx.connected_components(G), key=len)).copy()
        self.G = G
        self.ids = np.array(list(G.nodes))
        self.pos = np.array([data["nodes"][i] for i in range(len(data["nodes"]))])
        self.cpos = self.pos[self.ids]
        self.adj = {n: list(G[n]) for n in G.nodes}
        xs, ys = self.cpos[:, 0], self.cpos[:, 1]
        self.bounds = (xs.min(), xs.max(), ys.min(), ys.max())
        self.shelters: dict[str, Shelter] = {}
        self.civ = {}            # użytkownicy aplikacji mieszkańca: id -> civil.Civilian
        self.src = "osm"
        self._use_buildings(data["buildings"])
        self.night, self.sensor, self.guide = False, "auto", True
        self.strategy = "prob"   # lawn | spiral | prob | pspiral
        self.dynamic = True      # podział sektorów między aktywne drony
        self.two_stage = False   # zwiad RGB + weryfikacja IR
        self.rotation = True     # planowa rotacja: max 1 dron poza patrolem
        self.escort_mode = "hover"  # off | hover (zawis) | lead (odprowadzenie do schronu)
        self.escort_n = 30          # próg liczebności grupy dla eskorty [os.]
        self.comms = True        # komunikacja dron-dron: wspólny rejestr przydziałów
        self.weather = {"temp": 15.0, "wind": 0.0, "wdir": 0, "rain": 0.0, "hum": 60.0, "press": None,
                        "src": "domyślna (bezwietrznie)", "time": ""}
        self.grounded = False
        self.reset(seed)

    # ---------- zarządzanie scenariuszem ----------
    def reset(self, seed=None):
        self.rng = random.Random(seed)
        self.t = 0.0
        self.groups: list[Group] = []
        self.drones: list[Drone] = []
        self.tracks = {}      # obraz sytuacji w Centrum: gid -> {x,y,est,t}
        self.cands = {}       # niepotwierdzone zgłoszenia zwiadu: gid -> {x,y,est,t}
        self.found = 0        # osoby odnalezione przez drony (łącznie)
        self._active = ()
        self._aid = 0
        self.t_nofree = -1e9     # ostatnia nieudana próba znalezienia schronu
        self._short = False
        self.res = []         # rejestr rezerwacji: dron -> grupa -> schron, z czasem dojścia
        self.links = []
        self.grid.reset()
        self.events = []
        self.total = 0
        self.milestones = {}
        self._gid = 0
        for s in self.shelters.values():
            s.n = s.reserved = 0
            s.alerted = False
        if hasattr(self, "civ"):
            civil.reset(self)
        police.init(self)

    def _use_buildings(self, buildings):
        self.buildings = buildings
        self.bmap = {b["id"]: b for b in buildings}
        self._btree = None
        self.grid = ProbGrid(self.bounds, self.cpos, buildings)  # priorytety z funkcji budynków

    def set_buildings(self, src, buildings):
        """Zmiana źródła budynków (OSM / BDOT10k) w trakcie symulacji: ludzie, drony, schrony i rezerwacje
        zostają; budynki schronów przenoszone są do nowej warstwy, mapa przekonań drona zachowana."""
        keep = [self.bmap[sid] for sid in self.shelters if sid in self.bmap]
        belief = self.grid.b
        self.src = src
        ids = {b["id"] for b in buildings}
        self._use_buildings(list(buildings) + [b for b in keep if b["id"] not in ids])
        self.grid.b = belief  # ta sama siatka – nie tracimy wiedzy z dotychczasowych przelotów
        if getattr(self, "_psp", None):  # propozycje PSP dopasowane do nowej warstwy budynków
            self.load_psp(*self._psp, quiet=True)
        self.log("info", f"Źródło budynków: {'BDOT10k (GUGiK)' if src == 'bdot' else 'OpenStreetMap'} "
                         f"– {len(buildings)} budynków, {sum(1 for b in buildings if b.get('cand'))} kandydatów na schron")

    def near(self, x, y):
        d = (self.cpos[:, 0] - x) ** 2 + (self.cpos[:, 1] - y) ** 2
        return int(self.ids[int(d.argmin())])

    def toggle_shelter(self, bid, cap=None):
        if bid in self.shelters:
            self.remove_shelter(bid)
            return
        b = self.bmap[bid]
        if "psp" in b and cap is None:  # propozycja PSP -> schron z danymi PSP
            q = b["psp"]
            self.add_shelter(b, q["cap"], q["name"], addr=q["addr"], kind=q["kind"], access=q["access"], src="psp")
            self.log("info", f"Oznaczono {q['kind']} (PSP): {q['addr']} (pojemność {q['cap']})", b["cx"], b["cy"])
            return
        lv = int(b["levels"]) if (b["levels"] or "").isdigit() else 1
        cap = cap or max(10, min(600, round(b["area"] * min(lv, 2) / 4)))  # ~4 m² / osobę
        name = b["name"] or (f"{b['func']} (schron {len(self.shelters) + 1})" if b.get("func")
                             else f"Schron {len(self.shelters) + 1}")
        self.add_shelter(b, cap, name)

    def add_shelter(self, b, cap, name, **kw):
        s = Shelter(b["id"], name, b["cx"], b["cy"], self.near(b["cx"], b["cy"]), cap, **kw)
        self.shelters[b["id"]] = s
        if s.src == "user":
            self.log("info", f"Oznaczono schron: {name} (pojemność {cap})", s.x, s.y)
        return s

    def remove_shelter(self, bid):
        del self.shelters[bid]
        for g in self.groups:
            if g.target and g.target.id == bid:
                self.end_res(g, "anulowana – schron usunięty")
                g.target, g.path, g.guided = None, [], False

    def load_psp(self, points, mode="24h", quiet=False):
        """Oficjalne punkty schronienia PSP -> PROPOZYCJE na budynkach (b["psp"]); schronem budynek staje się
        dopiero po ręcznym oznaczeniu. mode: all | 24h (całodobowe + schrony) | schrony (tylko schrony)."""
        from shapely.geometry import Point, Polygon
        from shapely.strtree import STRtree
        self._psp = (points, mode)
        for b in self.buildings:
            b.pop("psp", None)
        if self._btree is None:
            self._bpolys = [Polygon([to_xy(*q) for q in b["poly"]]).buffer(0) for b in self.buildings]
            self._btree = STRtree(self._bpolys)
        added = virt = 0
        for pt in points:
            known = next((c for a, c in KNOWN_SCHRONY.items() if pt["addr"].startswith(a)), None)
            if mode == "schrony" and not known or mode == "24h" and not known and pt["access"] != "Całodobowa":
                continue
            P = Point(pt["x"], pt["y"])
            hit = [int(i) for i in self._btree.query(P, predicate="intersects")]
            i = hit[0] if hit else int(self._btree.nearest(P))
            if hit or self._bpolys[i].distance(P) < 25:
                b = self.buildings[i]
            else:  # punkt bez budynku w danych – obiekt wirtualny 12x12 m
                virt += 1
                b = {"id": "psp-" + pt["id"], "name": None, "type": "yes", "func": "punkt schronienia (PSP)",
                     "levels": None, "area": 144, "cx": pt["x"], "cy": pt["y"], "src": "psp", "cand": True,
                     "poly": [list(to_latlon(pt["x"] + dx, pt["y"] + dy)) for dx, dy in ((-6, -6), (6, -6), (6, 6), (-6, 6))]}
                self.buildings.append(b)
                self.bmap[b["id"]] = b
            if "psp" in b:
                continue
            b["psp"] = {"cap": known or max(20, min(300, round(b["area"] / 4))),  # miejsce ochronne: podziemie
                        "kind": "schron" if known else "miejsce ochronne", "addr": pt["addr"],
                        "access": pt["access"], "name": f"{'Schron' if known else 'Miejsce ochronne'}, {pt['addr']}"}
            added += 1
        if not quiet:
            self.log("info", f"Wczytano {added} propozycji punktów schronienia PSP (dane.gov.pl, KG PSP; tryb: {mode})"
                             + (f", {virt} bez budynku w danych" if virt else "") + " – kliknij, aby oznaczyć schron")
        return added

    # ---------- rezerwacje ----------
    def path_len(self, g):
        L = math.hypot(self.pos[g.nxt][0] - g.x, self.pos[g.nxt][1] - g.y)
        prev = g.nxt
        for n in g.path:
            L += math.hypot(*(self.pos[n] - self.pos[prev]))
            prev = n
        return L

    def end_res(self, g, status):
        if g.res is not None:
            r = self.res[g.res]
            r["st"], r["t_end"] = status, self.t
            g.res = None

    def add_group(self, x, y, n):
        node = self.near(x, y)
        nx_, ny_ = self.pos[node]
        self._gid += 1
        self.groups.append(Group(self._gid, n, node, node, nx_, ny_))
        self.total += n

    def random_groups(self, k, lo=10, hi=60):
        for _ in range(k):  # ludzie pojawiają się tam, gdzie realnie przebywają (ulice, dworzec, szkoły...)
            self.add_group(*self.grid.sample(self.rng), self.rng.randint(lo, hi))

    def add_drone(self, x, y):
        self.drones.append(Drone(len(self.drones) + 1, x, y, bx=x, by=y))
        self.plan_drones()

    # ---------- pogoda ----------
    def set_weather(self, **w):
        self.weather.update({k: v for k, v in w.items() if v is not None})
        x = self.weather
        self.log("info", f"Pogoda ({x['src']}): {x['temp']:.0f}°C, wiatr {x['wind']:.0f} m/s z {x['wdir']}°, "
                         f"opad {x['rain']:.1f} mm, wilg. {x['hum']:.0f}%")
        if (x["wind"] > MAX_WIND) != self.grounded:
            self.grounded = not self.grounded
            self.log("warn" if self.grounded else "info",
                     f"Wiatr {x['wind']:.0f} m/s > {MAX_WIND} m/s – loty wstrzymane, drony wracają do baz"
                     if self.grounded else "Wiatr poniżej limitu – wznowienie lotów")
        self.plan_drones()

    def wx_sensor(self, sensor):
        """Mnożnik zasięgu wykrywania wg pogody."""
        x, f = self.weather, 1.0
        if sensor == "rgb":
            if x["rain"] > 0:
                f *= max(.5, 1 - .15 * x["rain"])
            if x["hum"] >= 95:   # mgła / zamglenie
                f *= .6
        else:
            if x["rain"] > 0:
                f *= max(.7, 1 - .08 * x["rain"])
            if x["temp"] > 28:   # mały kontrast termiczny człowiek-otoczenie
                f *= .75
        return f

    def wx_drain(self):
        """Mnożnik zużycia baterii: wiatr (kwadratowo) i mróz."""
        x = self.weather
        return 1 + .5 * (x["wind"] / MAX_WIND) ** 2 + (.2 if x["temp"] < 0 else 0)

    def wind_vec(self):
        """Wektor wiatru [m/s] w kierunku, DO którego wieje (kierunek meteo = skąd wieje)."""
        a = math.radians(self.weather["wdir"])
        w = self.weather["wind"]
        return -w * math.sin(a), -w * math.cos(a)

    # ---------- drony ----------
    def dsensor(self, d):
        if self.two_stage and d.role != "std":
            return "rgb" if d.role == "scout" else "ir"
        if self.sensor == "auto":
            return "ir" if self.night else "rgb"
        return self.sensor

    def drange(self, d):
        sens = self.dsensor(d)
        return SENSOR[(sens, self.night)] * (SCOUT_RANGE if d.role == "scout" else 1) * self.wx_sensor(sens)

    def dspeed(self, d):
        return SCOUT_SPEED if d.role == "scout" else DRONE_SPEED

    def plan_drones(self):
        """Podział obszaru na pasy (sektory) i przygotowanie tras wg wybranej strategii."""
        x0, x1, y0, y1 = self.bounds
        two = self.two_stage and len(self.drones) > 1
        for i, d in enumerate(self.drones):
            d.role = ("scout" if i % 2 == 0 else "insp") if two else "std"
        for d in self.drones:
            d.rect = None
        if self.strategy == "lawn":
            return self.plan_lawn()
        active = [d for d in self.drones if d.state == "patrol"] if self.dynamic else self.drones
        active = active or self.drones
        self._active = tuple(d.id for d in active)
        k = len(active)
        for i, d in enumerate(active):
            a, b = x0 + (x1 - x0) * i / k, x0 + (x1 - x0) * (i + 1) / k
            d.sector, d.goal = (a, b), None
            lane = 1.6 * self.drange(d)
            if self.strategy == "spiral":
                d.wps = spiral((a + b) / 2, (y0 + y1) / 2, max(b - a, y1 - y0) / 2, lane, (a, b, y0, y1))
            else:
                d.wps = []
            d.wi = min(range(len(d.wps)), key=lambda j: (d.wps[j][0] - d.x) ** 2 + (d.wps[j][1] - d.y) ** 2) \
                if d.wps else 0

    def plan_lawn(self):
        """Kosiarka: obszar dzielony na tyle sektorów, ile dronów aktualnie pracuje (patroluje).
        Sektory przydzielane zachłannie najbliższym dronom, pasy wzdłuż dłuższego boku sektora."""
        work = [d for d in self.drones if d.state == "patrol"] or self.drones
        self._active = tuple(d.id for d in work)
        if not work:
            return
        rects = split_rect(self.bounds, len(work))
        pairs = sorted(((math.hypot((r[0] + r[1]) / 2 - d.x, (r[2] + r[3]) / 2 - d.y), i, d)
                        for i, r in enumerate(rects) for d in work), key=lambda p: p[0])
        used_r, used_d = set(), set()
        for _, i, d in pairs:
            if i in used_r or d.id in used_d:
                continue
            used_r.add(i)
            used_d.add(d.id)
            a, b, y0, y1 = d.rect = rects[i]
            d.sector, d.goal = (a, b), None
            d.wps = lawnmower(a, b, y0, y1, 1.6 * self.drange(d))
            d.wi = min(range(len(d.wps)), key=lambda j: (d.wps[j][0] - d.x) ** 2 + (d.wps[j][1] - d.y) ** 2)

    def pick_goal(self, d):
        if self.dynamic:  # obszar Woronoja: komórki, do których ten dron ma najbliżej spośród aktywnych
            act = [o for o in self.drones if o.state == "patrol"]
            dist = np.stack([np.hypot(self.grid.cx - o.x, self.grid.cy - o.y) for o in act])
            mask = dist.argmin(0) == act.index(d)
        else:
            a, b = d.sector
            mask = (self.grid.cx >= a) & (self.grid.cx < b)
        avoid = [o.goal for o in self.drones if o is not d and o.goal and o.state == "patrol"]
        return self.grid.best(d.x, d.y, mask, avoid)

    def demo(self):
        self.reset(7)
        self.shelters.clear()
        pool = [b for b in self.buildings if b.get("cand")] or self.buildings  # BDOT10k: kandydaci na schron
        big = sorted(pool, key=lambda b: -b["area"])
        chosen = []
        for b in big:  # 6 dużych, rozproszonych budynków
            if all((b["cx"] - c["cx"]) ** 2 + (b["cy"] - c["cy"]) ** 2 > 350 ** 2 for c in chosen):
                chosen.append(b)
            if len(chosen) == 6:
                break
        for b in chosen:
            self.toggle_shelter(b["id"], self.rng.choice([40, 60, 80, 100]))  # małe schrony – widać zapełnianie
        self.random_groups(14)
        x0, x1, y0, y1 = self.bounds
        for i in range(3):
            self.add_drone(x0 + (x1 - x0) * (i + .5) / 3, y0)
            self.drones[-1].bat = (100, 70, 45)[i]  # rozłożone baterie -> rotacja, a nie jednoczesny powrót
        self.plan_drones()

    def log(self, kind, text, x=None, y=None):
        self.events.append({"t": round(self.t), "k": kind, "m": text, "x": x, "y": y})

    # ---------- symulacja ----------
    def step(self, dt):
        self.t += dt
        vis = (VIS_NIGHT if self.night else VIS_DAY) * self.wx_sensor("rgb")
        for g in self.groups:
            self.move(g, dt, vis)
        self.groups = [g for g in self.groups if g.n > 0]
        self.merge()
        self.fly(dt)
        civil.step(self, dt)
        police.step(self, dt)
        self.check_alerts()

    def move(self, g, dt, vis):
        tx, ty = self.pos[g.nxt]
        dx, dy = tx - g.x, ty - g.y
        dist = math.hypot(dx, dy)
        s = WALK * dt
        if dist > s:
            g.x += dx / dist * s
            g.y += dy / dist * s
            return
        g.x, g.y = tx, ty
        g.prev, g.cur = g.cur, g.nxt
        self.on_node(g, vis)

    def on_node(self, g, vis):
        # 1. zauważony schron w zasięgu wzroku
        if not g.target:
            for s in self.shelters.values():
                if s.id not in g.bad and (s.x - g.x) ** 2 + (s.y - g.y) ** 2 < (vis + 15) ** 2:
                    self.set_target(g, s)
                    break
        # 2. dotarcie do schronu
        if g.target and g.cur == g.target.node:
            s = g.target
            k = min(g.n, s.free)
            s.n += k
            g.n -= k
            if g.guided:
                s.reserved = max(0, s.reserved - g.n - k)
            self.tracks.pop(g.id, None)
            if k:
                self.log("ok", f"{k} os. weszło do: {s.name} ({s.n}/{s.cap})", s.x, s.y)
            if g.res is not None:
                r = self.res[g.res]
                late = self.t - r["eta"]
                self.end_res(g, (f"dotarła ({k} os.)" if not g.n else f"częściowo: {k} weszło, {g.n} bez miejsca")
                             + (f", {'+' if late >= 0 else '−'}{self.fmt(abs(late))} vs plan" if abs(late) >= 30 else ""))
            g.bad.add(s.id)
            g.target, g.path, g.guided = None, [], False
            if g.n <= 0:
                return
            pu = police.at_shelter(self, s)
            ld = next((d for d in self.drones if d.lead and d.follow == g.id and d.state == "patrol"), None)
            if pu and police.redirect(self, pu, s, g):  # patrol zabezpieczający schron kieruje resztę dalej
                pass
            elif ld:  # dron odprowadzający jest na miejscu – od razu kieruje resztę do kolejnego schronu
                self.log("warn", f"{s.name} pełny – dron {ld.id} prowadzi pozostałe {g.n} os. dalej", s.x, s.y)
                ld.obs[s.id] = (s.n, self.t)
                if not self.assign(g, ld, max(1, round(g.n * self.rng.uniform(.9, 1.1)))):
                    ld.follow, ld.lead = None, False
            else:
                # reszta grupy nie wie, gdzie jest kolejny schron – błądzi, dopóki dron jej ponownie nie wykryje
                g.detected = False
                self.log("warn", f"{s.name} pełny – {g.n} os. zostało na zewnątrz i szuka dalej", s.x, s.y)
        # 3. następny krok (skierowana grupa może zgubić trasę – chyba że prowadzi ją dron)
        if g.guided and g.target and g.path and len(self.adj[g.cur]) >= 3 \
                and self.rng.random() < LOST_P * (.3 if g.confirmed else 1) \
                and not g.police and not any(d.lead and d.follow == g.id for d in self.drones):
            self.log("warn", f"Grupa ~{g.n} os. zgubiła drogę do: {g.target.name}", g.x, g.y)
            self.end_res(g, "zgubiła drogę")
            g.target.reserved = max(0, g.target.reserved - g.n)
            g.target, g.path, g.guided, g.detected = None, [], False, False
            self.tracks.pop(g.id, None)
        if g.target and g.path:
            g.nxt = g.path.pop(0)
        else:
            g.nxt = self.wander(g)

    def wander(self, g):
        nb = self.adj[g.cur]
        if len(nb) > 1 and g.prev in nb:
            nb = [n for n in nb if n != g.prev]
        # skłonność do łączenia się z innymi widocznymi grupami
        near = [o for o in self.groups if o is not g and o.n > 0
                and (o.x - g.x) ** 2 + (o.y - g.y) ** 2 < ATTRACT_R ** 2]
        hx, hy = (self.pos[g.cur] - self.pos[g.prev]) if g.prev >= 0 else (0, 0)
        if near:
            o = max(near, key=lambda o: o.n)
            hx, hy = hx + (o.x - g.x) * 3, hy + (o.y - g.y) * 3
        hl = math.hypot(hx, hy) or 1
        w = []
        for n in nb:
            vx, vy = self.pos[n] - self.pos[g.cur]
            c = (vx * hx + vy * hy) / ((math.hypot(vx, vy) or 1) * hl)
            w.append(math.exp(1.5 * c))
        return self.rng.choices(nb, w)[0]

    def set_target(self, g, s, guided=False):
        try:
            p = nx.shortest_path(self.G, g.cur, s.node, weight="w")
        except nx.NetworkXNoPath:
            return
        g.target, g.path, g.guided = s, p[1:], guided

    def remaining(self, d, s):
        """Wolne miejsca w schronie wg wiedzy drona: obserwacja zajętości + przydziały z rejestru."""
        n0, t0 = d.obs.get(s.id, (0, -1.0))
        booked = [(e, t) for sid, e, t in d.asg.values() if sid == s.id]
        after = sum(e for e, t in booked if t > t0)
        return s.cap - max(n0 + after, sum(e for e, _ in booked))

    def assign(self, g, d, est):
        """Decyzja drona (na podstawie jego rejestru): najbliższy schron, który pomieści całą grupę."""
        dist = nx.single_source_dijkstra_path_length(self.G, g.cur, weight="w")
        opts = [(dist[s.node], s, self.remaining(d, s)) for s in self.shelters.values()
                if s.id not in g.bad and s.node in dist]
        fit = [o for o in opts if o[2] >= est]
        part = [o for o in opts if o[2] > 0]
        if fit:
            L, s, rem = min(fit, key=lambda o: o[0])
        elif part:
            L, s, rem = max(part, key=lambda o: o[2])
            self.log("warn", f"Dron {d.id}: brak schronu na ~{est} os. – wysyłam do {s.name} (wolne ~{rem}), "
                             f"część grupy zostanie na zewnątrz", g.x, g.y)
        else:
            self.t_nofree = self.t
            self.log("warn", f"Dron {d.id}: wg rejestru brak wolnych miejsc dla grupy ~{est} os. – "
                             f"potrzebny dodatkowy punkt schronienia", g.x, g.y)
            return False
        self.set_target(g, s, guided=True)
        s.reserved += g.n
        self.end_res(g, "zastąpiona nową")
        eta = L / WALK
        self.res.append({"id": len(self.res), "t": self.t, "d": d.id, "g": g.id, "est": est, "sid": s.id,
                         "sname": s.name, "dist": L, "eta": self.t + eta, "st": "w drodze", "t_end": None})
        g.res = len(self.res) - 1
        self._aid += 1
        d.asg[self._aid] = (s.id, est, self.t)
        self.log("cmd", f"Dron {d.id}: rezerwacja {est} miejsc – {s.name} ({round(L)} m, "
                        f"dojście ~{self.fmt(eta)}, wg rejestru wolne ~{rem})",
                 g.x, g.y)
        return True

    def sync(self):
        """Łączność dron-dron: drony w zasięgu (także przez pośredników) scalają rejestry."""
        self.links = []
        for d in self.drones:
            d.net = (d.id,)
        if not self.comms:
            return
        ds = self.drones
        adj = {d.id: [] for d in ds}
        for i, a in enumerate(ds):
            for b in ds[i + 1:]:
                if math.hypot(a.x - b.x, a.y - b.y) <= COMM_R:
                    adj[a.id].append(b)
                    adj[b.id].append(a)
                    self.links.append((a, b))
        seen = set()
        for d in ds:
            if d.id in seen:
                continue
            comp, stack = [], [d]
            seen.add(d.id)
            while stack:
                x = stack.pop()
                comp.append(x)
                for y in adj[x.id]:
                    if y.id not in seen:
                        seen.add(y.id)
                        stack.append(y)
            asg, obs = {}, {}
            for x in comp:
                asg.update(x.asg)
                for sid, (n, t) in x.obs.items():
                    if t > obs.get(sid, (0, -1.0))[1]:
                        obs[sid] = (n, t)
            ids = tuple(sorted(x.id for x in comp))
            for x in comp:
                x.asg, x.obs, x.net = dict(asg), dict(obs), ids

    def merge(self):
        gs = sorted(self.groups, key=lambda g: -g.n)
        alive = []
        for g in gs:
            host = next((h for h in alive if (h.x - g.x) ** 2 + (h.y - g.y) ** 2 < MERGE_R ** 2), None)
            if not host:
                alive.append(g)
                continue
            host.n += g.n
            host.bad |= g.bad
            host.detected |= g.detected
            host.police = host.police or g.police
            if g.guided and g.target:
                g.target.reserved = max(0, g.target.reserved - g.n)
            if not host.target and g.target:
                host.target, host.path, host.guided, host.cur, host.nxt, host.prev = \
                    g.target, g.path, g.guided, g.cur, g.nxt, g.prev
                host.res, g.res = g.res, None
            else:
                self.end_res(g, "połączona z inną grupą")
            self.tracks.pop(g.id, None)
        self.groups = alive

    def fly(self, dt):
        self.sync()
        drain, (wx, wy) = self.wx_drain(), self.wind_vec()
        if (self.dynamic or self.strategy == "lawn") \
                and tuple(d.id for d in self.drones if d.state == "patrol") != self._active \
                and any(d.state == "patrol" for d in self.drones):
            self.plan_drones()
            self.log("info", f"Przeplanowanie: obszar podzielony na {len(self._active)} sektory "
                             f"(drony pracujące: {', '.join(f'D{i}' for i in self._active)})")
        out = sum(d.state != "patrol" for d in self.drones)
        sensors = []
        gmap = {g.id: g for g in self.groups}
        for d in self.drones:
            if d.state == "charge":
                d.charge -= dt
                if d.charge <= 0 and not self.grounded:
                    d.bat, d.state = 100.0, "patrol"
                    self.log("info", f"Dron {d.id}: nowa bateria, powrót do patrolu", d.x, d.y)
                continue
            if not d.wps and self.strategy not in ("prob", "pspiral"):
                continue
            v = self.dspeed(d)
            d.bat = max(0.0, d.bat - dt / FLIGHT_TIME * 100 * drain)
            if d.state == "patrol":
                need = math.hypot(d.bx - d.x, d.by - d.y) / v / FLIGHT_TIME * 100 * drain + RTB_MARGIN
                pat = [o for o in self.drones if o.state == "patrol"]
                early = self.rotation and out == 0 and len(self.drones) > 1 and d.bat < 35 \
                    and d is min(pat, key=lambda o: o.bat)
                if d.bat <= need or early or self.grounded:
                    if d.lead and d.follow in gmap:
                        self.log("warn", f"Dron {d.id}: przerwane odprowadzenie (bateria) – grupa zna trasę", d.x, d.y)
                    d.state, d.follow, d.task, d.goal, d.lead = "rtb", None, None, None, False
                    out += 1
                    self.log("warn", f"Dron {d.id}: bateria {d.bat:.0f}% – " +
                             ("wstrzymanie lotów (wiatr), powrót do bazy" if self.grounded else
                              "planowa rotacja, powrót do bazy" if d.bat > need
                              else "powrót do bazy, sektor bez pokrycia"), d.x, d.y)
            # --- wybór celu lotu ---
            kind = "wp"
            if d.state == "rtb":
                (tx, ty), kind = (d.bx, d.by), "base"
            elif d.follow and d.follow in gmap and (d.follow_t > 0 or d.lead):
                g = gmap[d.follow]
                (tx, ty), kind = (g.x, g.y), "hold"
                d.follow_t -= dt
            elif d.task and d.task in self.cands:
                c = self.cands[d.task]
                (tx, ty), kind = (c["x"], c["y"]), "task"
            else:
                if d.lead and d.follow:
                    self.log("ok", f"Dron {d.id}: odprowadzenie zakończone", d.x, d.y)
                d.follow = d.task = None
                d.lead = False
                if self.strategy == "prob":
                    if d.goal is None:
                        d.goal = self.pick_goal(d)
                    (tx, ty), kind = d.goal, "goal"
                else:
                    if self.strategy == "pspiral" and not d.wps:  # nowy "gorący punkt" -> lokalna spirala
                        d.goal = gx, gy = self.pick_goal(d)
                        d.wps, d.wi = [(gx, gy)] + spiral(gx, gy, HOT_R, 1.6 * self.drange(d), self.bounds), 0
                    tx, ty = d.wps[d.wi]
            dx, dy = tx - d.x, ty - d.y
            dist = math.hypot(dx, dy)
            # prędkość względem ziemi: składowa wiatru wzdłuż kierunku lotu (pod wiatr wolniej)
            s = max(2.0, v + .8 * (wx * dx + wy * dy) / dist) * dt if dist > 0 else v * dt
            if dist <= s:
                d.x, d.y = tx, ty
                if kind == "base":
                    d.state, d.charge = "charge", SWAP_TIME
                    self.log("info", f"Dron {d.id}: lądowanie w bazie, wymiana baterii", d.x, d.y)
                    continue
                if kind == "wp":
                    d.wi += 1
                    if d.wi >= len(d.wps):
                        d.wi = 0
                        if self.strategy == "pspiral":
                            d.wps, d.goal = [], None
                elif kind == "goal":
                    d.goal = None
                elif kind == "task":
                    d.task_t += dt
                    if d.task_t > VERIFY_T:
                        c = self.cands.pop(d.task)
                        self.log("info", f"Dron {d.id} (IR): weryfikacja negatywna – grupa ~{c['est']} os. "
                                         f"przemieściła się", c["x"], c["y"])
                        d.task = None
            else:
                d.x += dx / dist * s
                d.y += dy / dist * s
            d.dist += min(s, dist)
            R = self.drange(d)
            sensors.append((d.x, d.y, R))
            for sh in self.shelters.values():
                if math.hypot(sh.x - d.x, sh.y - d.y) <= R:
                    d.obs[sh.id] = (sh.n, self.t)
            self.detect(d, R, dt)
        self.grid.update(dt, sensors)
        # zgłoszenia zwiadu -> najbliższy wolny dron weryfikujący
        busy = {d.task for d in self.drones if d.task}
        for gid, c in self.cands.items():
            if gid in busy:
                continue
            free = [d for d in self.drones if d.role == "insp" and d.state == "patrol" and not d.task and not d.follow]
            if not free:
                break
            d = min(free, key=lambda d: (d.x - c["x"]) ** 2 + (d.y - c["y"]) ** 2)
            d.task, d.task_t = gid, 0.0
            self.log("cmd", f"Dron {d.id} (IR) skierowany do weryfikacji zgłoszenia ~{c['est']} os.", c["x"], c["y"])
        # schron zapełniony -> Centrum przekierowuje prowadzone grupy, ale tylko gdy dron jest nad grupą
        # (bez drona nie ma kanału komunikacji – grupa dowie się dopiero pod schronem)
        for g in self.groups:
            if not (g.guided and g.target and g.target.free <= 0):
                continue
            d = next((d for d in self.drones if d.state != "charge"
                      and math.hypot(d.x - g.x, d.y - g.y) <= self.drange(d)), None)
            if d:
                d.obs[g.target.id] = (g.target.n, self.t)
                g.target.reserved = max(0, g.target.reserved - g.n)
                g.bad.add(g.target.id)
                self.end_res(g, "przekierowana – schron pełny")
                g.target, g.path, g.guided = None, [], False
                self.assign(g, d, self.tracks.get(g.id, {}).get("est", g.n))

    def detect(self, d, R, dt):
        sens = self.dsensor(d).upper()
        for g in self.groups:
            r = math.hypot(g.x - d.x, g.y - d.y)
            if r > R:
                continue
            # prawdopodobieństwo wykrycia w 1 s: większa grupa i bliżej = łatwiej; zwiad z wysoka – słabiej
            p = min(.95, (.25 + .015 * g.n) * (1 - .6 * r / R)) * dt * (.7 if d.role == "scout" else 1)
            if self.rng.random() > p:
                continue
            est = max(1, round(g.n * self.rng.uniform(.8, 1.2)))
            if d.role == "scout" and g.id not in self.tracks:
                if g.id not in self.cands:
                    self.cands[g.id] = {"x": g.x, "y": g.y, "est": est, "t": self.t}
                    self.log("det", f"Dron {d.id} (zwiad {sens}): możliwa grupa ~{est} os. – do weryfikacji IR",
                             g.x, g.y)
                continue
            new = g.id not in self.tracks
            self.tracks[g.id] = {"x": g.x, "y": g.y, "est": est, "t": self.t, "d": d.id}
            if self.cands.pop(g.id, None) is not None and d.task == g.id:
                d.task = None
            if not new:
                # grupa znana, ale nadal błądzi (np. wcześniej brak miejsc) – ponowna próba skierowania
                if self.guide and not g.guided and not g.police and self.t - g.t_try >= RETRY_T:
                    g.t_try = self.t
                    if self.assign(g, d, est):
                        self.log("cmd", f"Dron {d.id}: ponowne skierowanie błądzącej grupy ~{est} os.", g.x, g.y)
                continue
            d.det += 1
            g.t_try = self.t
            if not g.seen:
                self.found += g.n
                g.seen = True
            g.detected = True
            self.log("det", f"Dron {d.id} ({sens}): wykryto grupę ~{est} os.", g.x, g.y)
            if self.strategy == "spiral" and d.role != "scout" and d.wps:  # szukaj wokół wykrycia
                a, b = d.sector
                y0, y1 = self.bounds[2:]
                d.wps, d.wi = spiral(g.x, g.y, 250, 1.6 * R, (a, b, y0, y1)) + d.wps[d.wi:] + d.wps[:d.wi], 0
            if self.guide and not g.guided and self.assign(g, d, est) and self.escort_mode != "off" \
                    and est >= self.escort_n and not d.follow:
                d.follow, d.follow_t, d.goal = g.id, ESCORT_T, None
                d.lead = self.escort_mode == "lead"
                g.confirmed = True
                self.log("cmd", f"Dron {d.id}: " + (f"odprowadza grupę ~{est} os. do: {g.target.name}" if d.lead
                                                    else f"zawis nad grupą ~{est} os. – potwierdzenie komunikatu"),
                         g.x, g.y)

    def check_alerts(self):
        for s in self.shelters.values():
            if not s.alerted and s.n >= .9 * s.cap:
                s.alerted = True
                self.log("warn", f"{s.name} zapełniony w {round(100 * s.n / s.cap)}%", s.x, s.y)
        inside = sum(s.n for s in self.shelters.values())
        for p in (50, 90, 100):
            if self.total and p not in self.milestones and inside >= self.total * p / 100:
                self.milestones[p] = self.t
                self.log("ok", f"Ewakuowano {p}% osób w czasie {self.fmt(self.t)}")

    @staticmethod
    def fmt(t):
        return f"{int(t // 60)}:{int(t % 60):02d}"

    def shortage(self):
        """Alert dla RCB: brak wolnych miejsc w schronach + podpowiedź budynków na nowe schrony."""
        inc = civil.incoming(self)
        free = sum(max(0, civil.free_eff(s, inc)) for s in self.shelters.values())
        # osoby znane systemowi (kiedykolwiek wykryte przez drony), które nie mają przydziału do schronu
        need = sum(self.tracks.get(g.id, {}).get("est", g.n) for g in self.groups if g.seen and not g.guided) \
            + sum(1 for c in self.civ.values() if not c.inside and not c.target)
        active = bool(self.shelters) and (free <= 0 or self.t - self.t_nofree < 60)
        if active != self._short:
            self._short = active
            self.log("warn" if active else "ok", "⚠ BRAK WOLNYCH MIEJSC W SCHRONACH – operator: wyznacz nowy budynek"
                     if active else "Miejsca w schronach dostępne – alert zamknięty")
        if not active:
            return {"active": False}
        # zapotrzebowanie: środek ciężkości wykrytych grup bez przydziału (albo wszystkich wykrytych)
        pts = [(self.tracks[g.id]["x"], self.tracks[g.id]["y"], self.tracks[g.id]["est"])
               for g in self.groups if g.id in self.tracks and not g.guided] \
            or [(v["x"], v["y"], v["est"]) for v in self.tracks.values()] or [(0.0, 0.0, 1)]
        W = sum(p[2] for p in pts)
        cx, cy = sum(p[0] * p[2] for p in pts) / W, sum(p[1] * p[2] for p in pts) / W
        sugg = []
        for b in self.buildings:
            if b["id"] in self.shelters or b["area"] < 250:
                continue
            q = b.get("psp")
            prio = 0 if q else 1 if b.get("cand") else 2
            if prio == 2 and b["area"] < 600:
                continue
            lv = int(b["levels"]) if (b.get("levels") or "").isdigit() else 1
            cap = q["cap"] if q else max(10, min(600, round(b["area"] * min(lv, 2) / 4)))
            d = math.hypot(b["cx"] - cx, b["cy"] - cy)
            sugg.append((prio, d + prio * 300, b, cap, d))
        sugg.sort(key=lambda v: v[1])
        return {"active": True, "free": free, "need": need, "deficit": max(0, need - free), "x": round(float(cx), 1),
                "y": round(float(cy), 1),
                "sugg": [{"id": b["id"], "cap": cap, "dist": round(d), "x": b["cx"], "y": b["cy"],
                          "name": (b["psp"]["name"] if b.get("psp") else b["name"] or b.get("func") or f"budynek ({b.get('type') or 'bez opisu'})"),
                          "src": "punkt PSP" if prio == 0 else "kandydat BDOT10k" if prio == 1 else "duży budynek"}
                         for prio, _, b, cap, d in sugg[:3]]}

    def res_snapshot(self):
        gmap = {g.id: g for g in self.groups if g.res is not None}
        act = {g.res: g for g in gmap.values()}
        out = []
        for r in self.res[-120:]:
            g = act.get(r["id"])
            rem = self.path_len(g) if g else None
            out.append([r["id"], round(r["t"]), r["d"], r["est"], r["sid"], r["sname"], round(r["dist"]),
                        round(rem / WALK) if g else None, round(rem) if g else None, r["st"],
                        round(r["t_end"]) if r["t_end"] is not None else None])
        return out

    # ---------- stan dla frontendu ----------
    def snapshot(self, heat=False):
        inside = sum(s.n for s in self.shelters.values())
        out = sum(g.n for g in self.groups)
        guided = sum(g.n for g in self.groups if g.guided)
        knows = sum(g.n for g in self.groups if g.target and not g.guided)
        ev, self.events = self.events, []
        return {
            "t": round(self.t, 1),
            "groups": [[g.id, round(g.x, 1), round(g.y, 1), g.n,
                        2 if g.guided else 1 if g.target else 0, int(g.detected)] for g in self.groups],
            "drones": [[round(d.x, 1), round(d.y, 1), d.id, d.state, round(d.bat, 1), d.det, round(d.dist),
                        round(d.charge), round(d.bx, 1), round(d.by, 1), d.role, self.dsensor(d),
                        round(self.drange(d)), ("lead" if d.lead else "follow") if d.follow else "verify" if d.task else "", len(d.asg), list(d.net)]
                       for d in self.drones],
            "sectors": [[round(v, 1) for v in d.rect] + [d.id] for d in self.drones if d.rect],
            "links": [[round(a.x, 1), round(a.y, 1), round(b.x, 1), round(b.y, 1)] for a, b in self.links],
            "leads": [[round(g.x, 1), round(g.y, 1), round(g.target.x, 1), round(g.target.y, 1)]
                      for d in self.drones if d.lead and d.follow
                      for g in self.groups if g.id == d.follow and g.target],
            "cands": [[round(c["x"], 1), round(c["y"], 1), c["est"]] for c in self.cands.values()],
            "det_km": round(sum(d.det for d in self.drones) / max(.1, sum(d.dist for d in self.drones) / 1000), 2),
            "flight_time": round(FLIGHT_TIME / self.wx_drain()),
            "weather": {**self.weather, "grounded": self.grounded, "drain": round(self.wx_drain(), 2),
                        "f_rgb": round(self.wx_sensor("rgb"), 2), "f_ir": round(self.wx_sensor("ir"), 2)},
            "shelters": [[s.id, s.n, s.cap, s.reserved, s.name, s.addr, s.kind, s.access]
                         for s in self.shelters.values()],
            "res": self.res_snapshot(),
            "tracks": [[round(v["x"], 1), round(v["y"], 1), v["est"], round(self.t - v["t"])]
                       for v in self.tracks.values()],
            "stats": {"total": self.total, "inside": inside, "out": out, "guided": guided, "knows": knows,
                      "wander": out - guided - knows, "detected_groups": len(self.tracks),
                      "free": sum(s.free for s in self.shelters.values()), "found": self.found,
                      "t50": self.milestones.get(50), "t90": self.milestones.get(90)},
            "events": ev,
            "police": police.snapshot(self),
            "shortage": self.shortage(),
            "app_users": [len(self.civ), sum(1 for c in self.civ.values() if c.inside)],
            **({"heat": self.grid.export()} if heat else {}),
        }
