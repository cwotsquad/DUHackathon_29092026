"""Widok służb (policja): patrole, zadania przejęcia grup i zabezpieczenia schronów, alternatywne schrony.

Zadania generuje system na podstawie danych z dronów i stanu schronów; patrol wysyła dyżurny (przycisk).
- "grupa":  grupa przekierowana przez drona (albo duża) -> punkt przejęcia na jej trasie; patrol prowadzi grupę
            do schronu (grupa prowadzona przez policję nie gubi drogi).
- "schron": schron zapełniony >= 90% -> zabezpieczenie; patrol na miejscu kieruje osoby, które się nie zmieściły,
            do alternatywnego schronu (najbliższy z wolnymi miejscami, liczony od zapełnionego schronu)."""
import math
from dataclasses import dataclass, field

import networkx as nx

import civil

CAR = 8.0          # [m/s] radiowóz w mieście (dojazd do zadania)
CRUISE = 5.0       # [m/s] patrolowanie terenu, gdy patrol jest wolny
CRUISE_MIN = 250   # [m] minimalna odległość kolejnego punktu patrolowania
BASE = (0.0, -120.0)  # umowna baza patroli (przy Rynku), współrzędne lokalne [m]
GROUP_N = 25       # próg dużej grupy do przejęcia
SECURE_FILL = .9   # prognozowane zapełnienie schronu wymagające zabezpieczenia
MEET_AHEAD = 150   # [m] punkt przejęcia przed grupą (na jej trasie)
TAKE_R = 60
STRAY_T = 60       # [s] wykryta grupa bez przydziału dłużej niż tyle -> zadanie zabezpieczenia dla policji
TRY_T = 10         # [s] co ile patrol prowadzący grupę bez celu szuka wolnego schronu        # [m] odległość przejęcia grupy
CHASE_T = 3        # [s] co ile przeliczać trasę pościgu za grupą
WALK = 1.3


@dataclass
class Unit:
    id: int
    x: float
    y: float
    node: int
    state: str = "wolny"      # wolny (patroluje teren) | w drodze | na miejscu
    task: int | None = None
    route: list = field(default_factory=list)
    t_route: float = -1e9


@dataclass
class Task:
    id: int
    kind: str                 # grupa | schron
    x: float
    y: float
    node: int
    t: float
    gid: int | None = None
    sid: str | None = None
    est: int = 0
    st: str = "nowe"          # nowe | przydzielone | w realizacji | zakończone | nieaktualne
    unit: int | None = None
    t_end: float | None = None
    note: str = ""
    t_try: float = -1e9


def init(w):
    """Start bez patroli – RCB wypuszcza je w trakcie działań."""
    w.units, w.tasks, w._tid = [], {}, 0
    if not hasattr(w, "pol_auto"):
        w.pol_auto = True  # automatyczny przydział najbliższego wolnego patrolu do nowych zadań


def add_unit(w, x=None, y=None):
    """RCB wypuszcza patrol: z bazy albo we wskazanym miejscu."""
    at_base = x is None
    n = w.near(*(BASE if at_base else (x, y)))
    uid = max((u.id for u in w.units), default=0) + 1
    w.units.append(Unit(uid, *w.pos[n], n))
    w.log("info", f"RCB: wypuszczono patrol P{uid} ({'z bazy' if at_base else 'we wskazanym miejscu'}); "
                  f"patroli w terenie: {len(w.units)}", *w.pos[n])


def remove_unit(w):
    """RCB wycofuje patrol – najpierw wolny (patrolujący), z najwyższym numerem."""
    free = [u for u in w.units if u.state == "wolny"]
    if not free:
        w.log("warn", "RCB: nie można wycofać patrolu – wszystkie realizują zadania")
        return
    u = max(free, key=lambda u: u.id)
    w.units.remove(u)
    w.log("info", f"RCB: wycofano patrol P{u.id}; patroli w terenie: {len(w.units)}")


def _new(w, **kw):
    w._tid += 1
    t = Task(w._tid, t=w.t, **kw)
    w.tasks[t.id] = t
    return t


def _close(w, t, st):
    t.st, t.t_end = st, w.t
    u = next((u for u in w.units if u.id == t.unit), None)
    if u:
        u.state, u.task, u.route = "wolny", None, []
    if t.gid is not None:
        g = next((g for g in w.groups if g.id == t.gid), None)
        if g and g.police == t.unit:
            g.police = None


def _ahead(w, g, dist):
    """Węzeł na trasie grupy ok. `dist` metrów przed nią."""
    L, prev = 0.0, g.nxt
    for n in g.path:
        L += math.hypot(*(w.pos[n] - w.pos[prev]))
        prev = n
        if L >= dist:
            return n
    return prev


def alternative(w, s, need=1):
    """Najbliższy (pieszo, od schronu s) schron z wolnymi miejscami: (schron, dystans, wolne) albo None."""
    inc = civil.incoming(w)
    dist = nx.single_source_dijkstra_path_length(w.G, s.node, weight="w")
    opts = [(dist[o.node], o, civil.free_eff(o, inc)) for o in w.shelters.values()
            if o is not s and o.node in dist and civil.free_eff(o, inc) > 0]
    if not opts:
        return None
    fit = [o for o in opts if o[2] >= need]
    L, o, f = min(fit or opts, key=lambda v: v[0] if fit else -v[2])
    return o, L, f


def at_shelter(w, s):
    """Patrol zabezpieczający schron s (na miejscu) albo None."""
    for u in w.units:
        t = w.tasks.get(u.task)
        if u.state == "na miejscu" and t and t.kind == "schron" and t.sid == s.id:
            return u
    return None


def redirect(w, u, s, g):
    """Patrol przy pełnym schronie kieruje resztę grupy do alternatywnego schronu."""
    alt = alternative(w, s, g.n)
    if not alt:
        w.t_nofree = w.t
        w.log("warn", f"Patrol P{u.id} przy {s.name}: brak alternatywnego schronu dla {g.n} os.", s.x, s.y)
        return False
    o, L, f = alt
    w.set_target(g, o, guided=True)
    o.reserved += g.n
    g.detected = True
    w.log("cmd", f"Patrol P{u.id} przy {s.name}: {g.n} os. skierowane do {o.name} ({round(L)} m)", s.x, s.y)
    return True


def guide(w, u, g):
    """Patrol prowadzi grupę bez celu do najbliższego schronu z wolnymi miejscami."""
    inc = civil.incoming(w)
    dist = nx.single_source_dijkstra_path_length(w.G, g.cur, weight="w")
    opts = [(dist[o.node], o) for o in w.shelters.values()
            if o.node in dist and o.id not in g.bad and civil.free_eff(o, inc) >= min(g.n, 10)]
    if not opts:
        return False
    L, o = min(opts, key=lambda v: v[0])
    w.set_target(g, o, guided=True)
    o.reserved += g.n
    w.log("cmd", f"Patrol P{u.id}: prowadzi grupę ~{g.n} os. do {o.name} ({round(L)} m)", g.x, g.y)
    return True


def dispatch(w, tid):
    t = w.tasks.get(tid)
    if not t or t.st != "nowe":
        return
    free = [u for u in w.units if u.state == "wolny"]
    if not free:
        w.log("warn", "Brak wolnego patrolu – zadanie czeka w kolejce", t.x, t.y)
        return
    u = min(free, key=lambda u: math.hypot(u.x - t.x, u.y - t.y))
    u.route = nx.shortest_path(w.G, u.node, t.node, weight="w")  # z bieżącej pozycji (także w trakcie patrolowania)
    u.state, u.task, t.st, t.unit = "w drodze", t.id, "przydzielone", u.id
    w.log("cmd", f"Patrol P{u.id} wysłany: {'przejęcie grupy' if t.kind == 'grupa' else 'zabezpieczenie schronu'}"
                 f" – {t.note}", t.x, t.y)


def done(w, tid):
    t = w.tasks.get(tid)
    if t and t.st not in ("zakończone", "nieaktualne"):
        _close(w, t, "zakończone")


def _cruise(w, u):
    """Wolny patrol: kolejny losowy punkt patrolowania w obszarze (po ulicach)."""
    # z kilku losowych punktów wybieramy najdalszy od celów pozostałych patroli – patrole rozjeżdżają się po terenie
    others = [w.pos[o.route[-1]] for o in w.units if o is not u and o.route]
    cand = [int(w.rng.choice(w.ids)) for _ in range(12)]
    cand = [n for n in cand if math.hypot(*(w.pos[n] - w.pos[u.node])) >= CRUISE_MIN] or cand
    n = max(cand, key=lambda n: min((math.hypot(*(w.pos[n] - p)) for p in others), default=0))
    u.route = nx.shortest_path(w.G, u.node, n, weight="w")


def _drive(w, u, dt, v=CAR):
    s = v * dt
    while s > 0 and u.route:
        tx, ty = w.pos[u.route[0]]
        d = math.hypot(tx - u.x, ty - u.y)
        if d > s:
            u.x += (tx - u.x) / d * s
            u.y += (ty - u.y) / d * s
            return
        u.x, u.y, s = tx, ty, s - d
        u.node = u.route.pop(0)


def step(w, dt):
    gmap = {g.id: g for g in w.groups}
    active = [t for t in w.tasks.values() if t.st not in ("zakończone", "nieaktualne")]
    # --- nowe zadania ---
    tasked_g = {t.gid for t in active if t.kind == "grupa"}
    tasked_s = {t.sid for t in active if t.kind == "schron"}
    for g in w.groups:
        if g.guided and g.target and g.path and g.id not in tasked_g and not g.police \
                and (g.bad or g.n >= GROUP_N):
            n = _ahead(w, g, MEET_AHEAD)
            est = w.tracks.get(g.id, {}).get("est", g.n)
            why = "przekierowana" if g.bad else "duża grupa"
            _new(w, kind="grupa", x=float(w.pos[n][0]), y=float(w.pos[n][1]), node=n, gid=g.id, est=est,
                 note=f"grupa ~{est} os. ({why}) → {g.target.name}")
    inc = civil.incoming(w)
    for g in w.groups:  # wykryta przez drony, ale nadal błądzi (brak miejsc / nieudane skierowanie)
        tr = w.tracks.get(g.id)
        if tr and not g.guided and not g.police and g.id not in tasked_g and w.t - g.t_try >= STRAY_T:
            n = g.nxt
            _new(w, kind="grupa", x=float(w.pos[n][0]), y=float(w.pos[n][1]), node=n, gid=g.id, est=tr["est"],
                 note=f"grupa ~{tr['est']} os. błądzi mimo wykrycia – zabezpieczyć i doprowadzić do schronu")
            tasked_g.add(g.id)
    for s in w.shelters.values():  # prognoza: zajęte + rezerwacje dronów + osoby w drodze
        proj = s.n + s.reserved + inc.get(s.id, 0)
        if proj >= SECURE_FILL * s.cap and s.id not in tasked_s:
            _new(w, kind="schron", x=s.x, y=s.y, node=s.node, sid=s.id, est=s.n,
                 note=f"{s.name} ({s.n}/{s.cap}" + (f", prognoza {proj}" if proj > s.n else "") + ")")
    # --- zadania nieaktualne ---
    for t in active:
        if t.kind == "grupa" and t.st in ("nowe", "przydzielone"):
            g = gmap.get(t.gid)
            if not g or (not g.guided and g.id not in w.tracks):
                _close(w, t, "nieaktualne")
        elif t.kind == "schron" and t.sid not in w.shelters:
            _close(w, t, "nieaktualne")
    # --- automatyczny przydział: najpierw schrony, potem największe grupy ---
    if w.pol_auto:
        for t in sorted((t for t in w.tasks.values() if t.st == "nowe"), key=lambda t: (t.kind != "schron", -t.est)):
            if not any(u.state == "wolny" for u in w.units):
                break
            dispatch(w, t.id)
    # --- patrole ---
    for u in w.units:
        t = w.tasks.get(u.task)
        if not t or u.state == "wolny":
            u.state, u.task = "wolny", None
            if not u.route:
                _cruise(w, u)
            _drive(w, u, dt, CRUISE)
            continue
        if t.kind == "grupa":
            g = gmap.get(t.gid)
            if not g:  # grupa weszła do schronu / połączyła się
                w.log("ok", f"Patrol P{u.id}: grupa doprowadzona, zadanie zakończone", u.x, u.y)
                _close(w, t, "zakończone")
                continue
            if u.state == "na miejscu":  # prowadzenie grupy
                u.x, u.y, u.node = g.x, g.y, g.cur
                if not g.guided and w.t - t.t_try >= TRY_T:  # grupa bez celu – patrol szuka wolnego schronu
                    t.t_try = w.t
                    if guide(w, u, g):
                        continue
                continue
            if math.hypot(g.x - u.x, g.y - u.y) <= TAKE_R:
                u.state, u.route, t.st, g.police = "na miejscu", [], "w realizacji", u.id
                w.log("ok", f"Patrol P{u.id} przejął grupę ~{t.est} os. – prowadzi do {g.target.name}", g.x, g.y)
                continue
            if not u.route and w.t - u.t_route >= CHASE_T:  # w punkcie przejęcia grupy nie ma – dojazd do grupy
                u.route, u.t_route = nx.shortest_path(w.G, u.node, g.nxt, weight="w"), w.t
        elif not u.route and u.state == "w drodze":  # schron
            u.state, t.st = "na miejscu", "w realizacji"
            s = w.shelters.get(t.sid)
            w.log("ok", f"Patrol P{u.id} zabezpiecza {s.name if s else 'schron'}", t.x, t.y)
        _drive(w, u, dt)


def snapshot(w):
    inc = civil.incoming(w)
    tasks = []
    for t in sorted(w.tasks.values(), key=lambda t: -t.id)[:40]:
        row = {"id": t.id, "kind": t.kind, "x": round(t.x, 1), "y": round(t.y, 1), "st": t.st, "unit": t.unit,
               "est": t.est, "note": t.note, "t": round(t.t), "t_end": None if t.t_end is None else round(t.t_end)}
        if t.kind == "schron" and t.st not in ("zakończone", "nieaktualne"):
            s = w.shelters.get(t.sid)
            if s:
                row["fill"] = [s.n, s.cap]
                alt = alternative(w, s, max(1, civil.free_eff(s, inc) * -1))
                if alt:
                    o, L, f = alt
                    row["alt"] = {"name": o.name, "addr": o.addr, "free": f, "dist": round(L),
                                  "eta": round(L / WALK), "x": round(o.x, 1), "y": round(o.y, 1)}
        if t.kind == "grupa":
            g = next((g for g in w.groups if g.id == t.gid), None)
            if g and g.target:
                row["to"] = {"name": g.target.name, "x": round(g.target.x, 1), "y": round(g.target.y, 1)}
                row["g"] = [round(g.x, 1), round(g.y, 1)]
        tasks.append(row)
    return {"auto": w.pol_auto, "units": [{"id": u.id, "x": round(u.x, 1), "y": round(u.y, 1), "state": u.state, "task": u.task,
                       "route": [[round(v, 1) for v in w.pos[n]] for n in u.route[::3]]} for u in w.units],
            "tasks": tasks}
