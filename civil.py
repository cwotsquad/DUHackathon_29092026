"""Widok mieszkańca: pozycja użytkownika aplikacji, sugerowany schron, trasa i czas dotarcia.

Mieszkaniec dostaje tylko informacje o schronach (bez pozycji innych ludzi i dronów – ochrona prywatności).
Cel = najbliższy po sieci pieszej schron, który ma wolne miejsca po uwzględnieniu rezerwacji dronów.
Cel zmienia się tylko, gdy obecny przestał mieć miejsca (bez "skakania" między podobnymi schronami)."""
import math
from dataclasses import dataclass, field

import networkx as nx

WALK = 1.3         # [m/s] tempo pieszego (jak w modelu)
REPLAN_T = 2.0     # [s] co ile sekund symulacji przeliczać cel
FEW = .2           # "mało miejsc": wolne < 20% pojemności
MARGIN = 5         # schron z mniejszym zapasem miejsc zapełni się zanim dojdziemy -> kara
PENALTY = 400      # [m] kara odległości za mały zapas miejsc
BAD_T = 300        # [s] ile pamiętamy schron zastany jako pełny


@dataclass
class Civilian:
    id: int
    x: float
    y: float
    node: int
    target: str | None = None
    route: list = field(default_factory=list)  # pozostałe węzły trasy
    walking: bool = False
    inside: str | None = None
    note: str = ""        # ostatni komunikat dla użytkownika (np. zmiana celu)
    note_t: float = 0.0
    t_plan: float = -1e9
    bad: dict = field(default_factory=dict)  # schron -> czas, gdy zastaliśmy go pełnym


def incoming(w):
    """Osoby w drodze do schronów: grupy idące same (rezerwacje dronów są już w s.reserved)
    i inni użytkownicy aplikacji, którzy mają dany schron jako cel."""
    inc = {}
    for g in w.groups:
        if g.target and not g.guided:
            inc[g.target.id] = inc.get(g.target.id, 0) + g.n
    for c in w.civ.values():
        if c.target and not c.inside:
            inc[c.target] = inc.get(c.target, 0) + 1
    return inc


def free_eff(s, inc=None, me=None):
    """Wolne miejsca po odjęciu zajętych, rezerwacji dronów i osób już w drodze (bez siebie samego)."""
    return s.cap - s.n - s.reserved - (inc or {}).get(s.id, 0) + (1 if me and me.target == s.id else 0)


def status(s, inc=None):
    f = free_eff(s, inc)
    return 2 if f <= 0 else 1 if f < FEW * s.cap else 0   # 0 wolne, 1 mało miejsc, 2 pełny


def set_position(w, cid, x, y):
    c = Civilian(cid, x, y, w.near(x, y))
    w.civ[cid] = c
    plan(w, c, force=True)
    return c


def _note(w, c, text):
    c.note, c.note_t = text, w.t


def plan(w, c, force=False):
    """Cel: minimum (odległość po ulicach + kara za mały zapas miejsc) wśród schronów z wolnymi miejscami.
    Obecny cel utrzymujemy, dopóki ma miejsce dla nas (brak przeskakiwania między podobnymi schronami)."""
    if c.inside:
        return
    c.t_plan = w.t
    inc = incoming(w)
    cur = w.shelters.get(c.target) if c.target else None
    if cur and free_eff(cur, inc, c) > 0 and not force:
        return
    c.bad = {k: t for k, t in c.bad.items() if w.t - t < BAD_T}
    cand = {s.node: (s, free_eff(s, inc, c)) for s in w.shelters.values() if s.id not in c.bad}
    cand = {n: v for n, v in cand.items() if v[1] > 0}
    if not cand:
        if c.target or not c.note.startswith("Brak"):
            _note(w, c, "Brak wolnych miejsc w schronach w okolicy – szukam dalej, kieruj się komunikatami służb")
        c.target, c.route = None, []
        return
    dist = nx.single_source_dijkstra_path_length(w.G, c.node, weight="w")
    score = {n: dist[n] + (PENALTY if f < MARGIN else 0) for n, (s, f) in cand.items() if n in dist}
    if not score:
        return
    s, f = cand[min(score, key=score.get)]
    old = cur or (w.shelters.get(c.target) if c.target else None)
    if c.target and c.target != s.id:
        why = "jest pełny" if old and free_eff(old, inc, c) <= 0 else "jest niedostępny" if not old else "zapełnia się"
        _note(w, c, f"⚠ Zmiana celu: {old.name if old else 'poprzedni schron'} {why}. "
                    f"Nowy cel: {s.name} ({round(dist[s.node])} m)")
    elif not c.target and c.note.startswith("Brak"):
        _note(w, c, f"✅ Znaleziono wolne miejsce: {s.name}")
    c.target = s.id
    c.route = nx.shortest_path(w.G, c.node, s.node, weight="w")


def remaining(w, c):
    if not c.route:
        return 0.0
    L = math.hypot(w.pos[c.route[0]][0] - c.x, w.pos[c.route[0]][1] - c.y)
    for a, b in zip(c.route, c.route[1:]):
        L += math.hypot(*(w.pos[b] - w.pos[a]))
    return L


def step(w, dt):
    for c in w.civ.values():
        if c.inside:
            continue
        if w.t - c.t_plan >= REPLAN_T:
            plan(w, c)
        if not (c.walking and c.target):
            continue
        s = WALK * dt
        while s > 0 and c.route:
            tx, ty = w.pos[c.route[0]]
            d = math.hypot(tx - c.x, ty - c.y)
            if d > s:
                c.x += (tx - c.x) / d * s
                c.y += (ty - c.y) / d * s
                break
            c.x, c.y, s = tx, ty, s - d
            c.node = c.route.pop(0)
        if not c.route:  # na miejscu
            sh = w.shelters.get(c.target)
            if sh and sh.free > 0:
                sh.n += 1
                c.inside, c.walking = sh.id, False
                _note(w, c, f"✅ Jesteś w schronie: {sh.name}")
            else:  # na miejscu okazało się, że brak miejsc – zapamiętaj i szukaj dalej
                if sh:
                    c.bad[sh.id] = w.t
                _note(w, c, f"⚠ {sh.name if sh else 'Schron'} – brak miejsc na miejscu. Szukam kolejnego schronu…")
                plan(w, c, force=True)


def reset(w):
    for c in w.civ.values():
        c.inside, c.target, c.route, c.walking, c.note = None, None, [], False, ""
        plan(w, c, force=True)


def snapshot(w, cid):
    c = w.civ.get(cid)
    inc = incoming(w)
    out = {"t": round(w.t), "shelters": [[s.id, round(s.x, 1), round(s.y, 1), s.name, s.addr, status(s, inc), s.kind]
                                         for s in w.shelters.values()]}
    if not c:
        return out
    # także na pauzie: cel usunięty / bez miejsc / brak celu, a schrony się pojawiły -> przelicz od razu
    cur = w.shelters.get(c.target) if c.target else None
    if not c.inside and ((c.target and (not cur or free_eff(cur, inc, c) <= 0)) or (not c.target and w.shelters)):
        plan(w, c, force=bool(c.target))
    L = remaining(w, c)
    s = w.shelters.get(c.target or c.inside)
    out["me"] = {"x": round(c.x, 1), "y": round(c.y, 1), "walking": c.walking, "inside": bool(c.inside),
                 "note": c.note if w.t - c.note_t < 120 else "",
                 "route": [[round(c.x, 1), round(c.y, 1)]] + [[round(v, 1) for v in w.pos[n]] for n in c.route[::2]]
                 + ([[round(v, 1) for v in w.pos[c.route[-1]]]] if c.route else []),
                 "dist": round(L), "eta": round(L / WALK)}
    if s:
        out["me"]["target"] = {"id": s.id, "name": s.name, "addr": s.addr, "kind": s.kind, "access": s.access,
                               "status": status(s, inc), "free": max(0, free_eff(s, inc, c)),
                               "x": round(s.x, 1), "y": round(s.y, 1)}
    return out
