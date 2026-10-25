"""Display-only estimates from community and locally captured Zwift ratios."""

from collections import deque

GEAR_RATIOS = (.75, .87, .99, 1.11, 1.23, 1.38, 1.53, 1.68, 1.86, 2.04,
               2.22, 2.40, 2.61, 2.82, 3.03, 3.24, 3.49, 3.74, 3.99, 4.24,
               4.54, 4.84, 5.14, 5.49)

# ring, largest-to-smallest rear position, teeth. Derived from the four
# user shift sweeps on 2026-10-24; no reference implementation is copied.
DRIVETRAINS = {
    "Flat": ((39, 53), (28, 26, 24, 20, 18, 16, 15, 14, 13, 12, 11, 10)),
    "All-Rounder": ((35, 48), (33, 28, 24, 21, 19, 17, 15, 14, 13, 12, 11, 10)),
    "Climbing": ((30, 43), (36, 32, 28, 24, 21, 19, 17, 15, 13, 12, 11, 10)),
}
COMBINATIONS = {
    name: sorted((front / rear, ring, position, front, rear)
                 for ring, front in enumerate(fronts, 1)
                 for position, rear in enumerate(rears, 1))
    for name, (fronts, rears) in DRIVETRAINS.items()
}
TABLES = {"Mixed Terrain": GEAR_RATIOS,
          **{name: tuple(row[0] for row in rows) for name, rows in COMBINATIONS.items()}}
TOLERANCE = .00011  # One wire unit of float/truncation error is observed.


class GearEstimate:
    def __init__(self):
        self.preset = None
        self.candidates = set(TABLES)
        self.matched = None
        self.previous = None
        self.transitions = deque(maxlen=8)
        self.style = "Sequential"
        self.style_evidence = False

    def gear(self, ratio):
        if ratio is None:
            return None
        table = TABLES[self.preset or "Mixed Terrain"]
        return min(range(len(table)), key=lambda i: abs(table[i] - ratio)) + 1

    def update(self, ratio):
        if ratio is None:
            self.__init__()
            return
        matches = {name for name, table in TABLES.items()
                   if min(abs(value - ratio) for value in table) < TOLERANCE}
        old_preset = self.preset
        self.matched = bool(matches)
        if matches:
            self.candidates = self.candidates & matches or matches
            if self.preset not in self.candidates:
                # Prefer a plausible starting position until a later shift
                # distinguishes overlapping tables. Preserve a compatible guess.
                self.preset = min(self.candidates, key=lambda name: (
                    abs(ratio - TABLES[name][7 if name == "Mixed Terrain" else 4]), name))
        elif self.preset is None:
            self.preset = min(TABLES, key=lambda name: (
                min(abs(value - ratio) for value in TABLES[name]), name))
        if old_preset != self.preset or not matches:
            self.transitions.clear()
            self.previous = None
            self.style = "Sequential"
            self.style_evidence = False
        gear = self.gear(ratio)
        if self.previous is not None and gear != self.previous:
            delta = abs(gear - self.previous)
            sram_step = False
            if self.preset in COMBINATIONS:
                a = COMBINATIONS[self.preset][self.previous - 1]
                b = COMBINATIONS[self.preset][gear - 1]
                sram_step = ((a[1] == b[1] and abs(a[2] - b[2]) == 1) or
                             (a[1] != b[1] and a[2] == b[2]))
            # Steps compatible with both styles should not overturn a guess.
            self.transitions.append((delta == 1 and not sram_step, sram_step and delta != 1))
            sequential = sum(a for a, _ in self.transitions)
            sram = sum(b for _, b in self.transitions)
            if sram >= 3 and sram > sequential:
                self.style, self.style_evidence = "SRAM-style", True
            elif sequential >= 3 and sequential > sram:
                self.style, self.style_evidence = "Sequential", True
        self.previous = gear if matches else None

    def status(self, ratio):
        if ratio is None:
            return dict(preset=None, confidence="waiting", candidates=[], style=None,
                        style_confidence="waiting", gear_label=None, teeth=None, ratio_matches=None)
        gear = self.gear(ratio)
        teeth = None
        label = str(gear)
        if self.preset in COMBINATIONS:
            _, ring, rear, front_teeth, rear_teeth = COMBINATIONS[self.preset][gear - 1]
            teeth = f"{front_teeth}/{rear_teeth}"
            if self.style == "SRAM-style":
                label = f"{ring}-{rear}"
        return dict(preset=self.preset or "Mixed Terrain",
                    confidence="approximate" if self.matched is False else
                               "inferred" if len(self.candidates) == 1 else "guess",
                    candidates=sorted(self.candidates) if self.matched is not False else [],
                    style=self.style, style_confidence="inferred" if self.style_evidence else "guess",
                    gear_label=label, teeth=teeth, ratio_matches=self.matched)
