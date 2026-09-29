"""Porównanie strategii przeszukiwania na tym samym scenariuszu (bez GUI).
Uruchom:  .venv\\Scripts\\python compare.py"""
import statistics

import map_loader
from model import World

STRATEGIES = {"lawn": "Kosiarka", "spiral": "Spirala", "prob": "Mapa prawdopodob.", "pspiral": "Spirala od hotspot."}
SEEDS = [3, 7, 11]
T_MAX = 60 * 60


def run(w, strategy, dynamic, two_stage, night, seed):
    w.strategy, w.dynamic, w.two_stage, w.night = strategy, dynamic, two_stage, night
    w.demo()
    w.reset(seed)  # ten sam układ schronów, nowe grupy i drony
    w.random_groups(14)
    x0, x1, y0, _ = w.bounds
    for i in range(4):
        w.add_drone(x0 + (x1 - x0) * (i + .5) / 4, y0)
        w.drones[-1].bat = (100, 80, 60, 40)[i]
    w.plan_drones()
    for sh in w.shelters.values():
        sh.cap = 1000  # pojemność nie jest wąskim gardłem – mierzymy samo przeszukiwanie
    first, f10 = None, 0
    while w.t < T_MAX and 90 not in w.milestones:
        w.step(1.0)
        if first is None and w.tracks:
            first = w.t
        if w.t == 600:
            f10 = 100 * w.found / w.total
    s = w.snapshot()
    return first or T_MAX, f10, s["stats"]["t90"] or T_MAX, s["det_km"]


def main():
    w = World(map_loader.load())
    fmt = World.fmt
    print(f"{'strategia':20}{'dynam.':>8}{'2-etap.':>8}{'noc':>5}{'1. wykrycie':>13}{'wykryci 10 min':>16}{'ewak. 90%':>11}{'wykr./km':>10}")
    for night in (False, True):
        for st, name in STRATEGIES.items():
            for dyn, two in ((False, False), (True, False), (True, True)):
                r = [run(w, st, dyn, two, night, s) for s in SEEDS]
                first, f10, t90, dk = (statistics.mean(x) for x in zip(*r))
                print(f"{name:20}{'tak' if dyn else '-':>8}{'tak' if two else '-':>8}{'tak' if night else '-':>5}"
                      f"{fmt(first):>13}{f10:>15.0f}%{fmt(t90):>11}{dk:>10.2f}")


def comms_test():
    """Komunikacja dron-dron: realne (małe) pojemności schronów, liczymy przepełnienia."""
    w = World(map_loader.load())
    print(f"
{'komunikacja':15}{'przepełnienia':>15}{'w schronach po 30 min':>24}")
    for comms in (True, False):
        over = inside = 0
        for seed in SEEDS:
            w.comms = comms
            w.demo()
            w.reset(seed)
            w.random_groups(14)
            x0, x1, y0, _ = w.bounds
            for i in range(4):
                w.add_drone(x0 + (x1 - x0) * (i + .5) / 4, y0)
            w.plan_drones()
            while w.t < 1800:
                w.step(1.0)
                over += sum("zostało na zewnątrz" in e["m"] for e in w.events)
                w.events = []
            inside += w.snapshot()["stats"]["inside"]
        print(f"{'tak' if comms else 'nie':15}{over / len(SEEDS):>15.1f}{inside / len(SEEDS):>24.0f}")


if __name__ == "__main__":
    main()
    comms_test()
