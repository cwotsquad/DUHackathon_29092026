"""Strategie przeszukiwania obszaru i mapa prawdopodobieństwa obecności ludzi."""
import math

import numpy as np

CELL = 40       # rozmiar komórki siatki [m]
REGROW = 900    # [s] czas powrotu przekonania do wartości a priori (ludzie się przemieszczają)
SCAN_T = 4      # [s] stała czasowa "wyczyszczenia" komórki pod sensorem drona
AVOID_R = 150   # [m] drony nie wybierają celów bliżej siebie niż to

# waga typów budynków z OSM – miejsca, gdzie w chwili alarmu przebywa dużo ludzi
POI = {"train_station": 1.5, "transportation": 1.2, "school": 1.0, "university": 1.0, "college": 1.0,
       "kindergarten": .8, "retail": 1.0, "commercial": .8, "supermarket": 1.0, "public": .7, "civic": .7,
       "government": .6, "hospital": .8, "church": .6, "cathedral": .6, "office": .5, "hotel": .5,
       "sports_hall": .7, "stadium": .8, "apartments": .3}
NOT_POI = {"house", "residential", "apartments", "garage", "garages", "shed", "roof", "detached", "yes"}


def _blur(a):
    p = np.pad(a, 1, mode="edge")
    h, w = a.shape
    return sum(p[i:i + h, j:j + w] for i in range(3) for j in range(3)) / 9


class ProbGrid:
    """Siatka przekonań Centrum: gdzie mogą być ludzie, których jeszcze nie znaleziono."""

    def __init__(self, bounds, node_xy, buildings):
        x0, x1, y0, y1 = bounds
        self.x0, self.y0 = x0, y0
        self.w, self.h = int((x1 - x0) // CELL) + 1, int((y1 - y0) // CELL) + 1
        self.cx, self.cy = np.meshgrid(x0 + (np.arange(self.w) + .5) * CELL, y0 + (np.arange(self.h) + .5) * CELL)

        streets = np.zeros((self.h, self.w))
        ix, iy = self._idx(node_xy[:, 0], node_xy[:, 1])
        np.add.at(streets, (iy, ix), 1)
        streets = np.minimum(streets, 20) / 20

        poi = np.zeros_like(streets)
        for b in buildings:
            wt = POI.get(b["type"], 0) + (.4 if b["name"] and b["type"] not in NOT_POI else 0)
            if wt:
                i, j = self._idx(np.array([b["cx"]]), np.array([b["cy"]]))
                poi[j[0], i[0]] += wt * min(1.5, .5 + b["area"] / 3000)

        prior = _blur(_blur(.6 * streets + np.minimum(poi, 2)))
        prior /= prior.max()
        self.prior = .03 + .97 * prior
        self.b = self.prior.copy()

    def _idx(self, x, y):
        return (np.clip(((x - self.x0) // CELL).astype(int), 0, self.w - 1),
                np.clip(((y - self.y0) // CELL).astype(int), 0, self.h - 1))

    def reset(self):
        self.b = self.prior.copy()

    def update(self, dt, sensors):
        self.b += (self.prior - self.b) * (dt / REGROW)
        k = math.exp(-dt / SCAN_T)
        for x, y, r in sensors:
            self.b[(self.cx - x) ** 2 + (self.cy - y) ** 2 < r * r] *= k

    def sample(self, rng):
        """Losowy punkt zgodny z rozkładem a priori (tam, gdzie realnie są ludzie)."""
        p = self.prior.ravel() ** 2
        k = rng.choices(range(p.size), weights=p)[0]
        j, i = divmod(k, self.w)
        return self.x0 + (i + rng.random()) * CELL, self.y0 + (j + rng.random()) * CELL

    def best(self, x, y, mask, avoid):
        """Cel dla drona: największe przekonanie, z karą za odległość (czas przelotu)."""
        score = self.b / (1 + np.hypot(self.cx - x, self.cy - y) / 300)
        score = np.where(mask, score, 0)
        for ax, ay in avoid:
            score[(self.cx - ax) ** 2 + (self.cy - ay) ** 2 < AVOID_R ** 2] = 0
        j, i = np.unravel_index(int(score.argmax()), score.shape)
        return float(self.cx[j, i]), float(self.cy[j, i])

    def export(self):
        return {"x0": self.x0, "y0": self.y0, "w": self.w, "h": self.h, "cell": CELL,
                "v": np.round(np.clip(self.b, 0, 1) * 99).astype(int).ravel().tolist()}


def lawnmower(a, b, y0, y1, lane):
    """Pasy tam i z powrotem w prostokącie [a,b]x[y0,y1], wzdłuż dłuższego boku (mniej zawrotów)."""
    along_x = (b - a) >= (y1 - y0)
    lo, hi, p0, p1 = (y0, y1, a, b) if along_x else (a, b, y0, y1)
    wps, v, flip = [], lo + lane / 2, False
    while True:
        v = min(v, hi - lane / 4) if v > hi else v
        seg = [(p1, v), (p0, v)] if flip else [(p0, v), (p1, v)]
        wps += seg if along_x else [(q, p) for p, q in seg]
        if v >= hi - lane / 2:
            break
        v += lane
        flip = not flip
    return wps + wps[::-1]


def split_rect(bounds, n):
    """Podział prostokąta na n sektorów możliwie zbliżonych do kwadratu (kolumny x wiersze)."""
    x0, x1, y0, y1 = bounds
    W, H = x1 - x0, y1 - y0
    cols = max(1, min(n, round(math.sqrt(n * W / H))))
    rects = []
    for c in range(cols):
        rows = n // cols + (1 if c < n % cols else 0)
        a, b = x0 + W * c / cols, x0 + W * (c + 1) / cols
        for r in range(rows):
            rects.append((a, b, y0 + H * r / rows, y0 + H * (r + 1) / rows))
    return rects


def spiral(cx, cy, rmax, lane, rect):
    """Spirala Archimedesa od punktu (cx,cy), odstęp zwojów = lane, przycięta do prostokąta."""
    a0, a1, b0, b1 = rect
    wps, th = [], 0.0
    while True:
        r = lane * th / (2 * math.pi)
        if r > rmax:
            break
        wps.append((min(max(cx + r * math.cos(th), a0), a1), min(max(cy + r * math.sin(th), b0), b1)))
        th += 40 / max(r, lane / 2)
    return wps
