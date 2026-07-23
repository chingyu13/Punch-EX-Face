#!/usr/bin/env python3
"""Sync the edited normal head into punch.html's three-state morph data."""

import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "punch.html"
SVG = ROOT / "man_punch.svg"
NS = {"s": "http://www.w3.org/2000/svg"}
TOKEN_RE = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
ARG_COUNTS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def tokenize_path(d):
    return TOKEN_RE.findall(d)


def cubic(p0, p1, p2, p3, t):
    u = 1 - t
    return (
        u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
        u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
    )


def quad(p0, p1, p2, t):
    u = 1 - t
    return (
        u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
        u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
    )


def path_polylines(d):
    tokens = tokenize_path(d)
    rings, ring = [], []
    i = 0
    cmd = None
    p = (0.0, 0.0)
    start = p
    last_cubic = None
    last_quad = None

    def add(pt):
        nonlocal p
        if not ring or pt != ring[-1]:
            ring.append(pt)
        p = pt

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
        if cmd is None:
            raise ValueError("SVG path starts without a command")
        upper = cmd.upper()
        relative = cmd.islower()
        if upper == "Z":
            if ring:
                if ring[-1] != start:
                    ring.append(start)
                rings.append(ring)
                ring = []
            p = start
            last_cubic = last_quad = None
            cmd = None
            continue
        n = ARG_COUNTS[upper]
        vals = list(map(float, tokens[i:i + n]))
        i += n
        if upper == "M":
            x, y = vals
            if relative:
                x, y = x + p[0], y + p[1]
            if ring:
                rings.append(ring)
                ring = []
            p = start = (x, y)
            ring.append(p)
            cmd = "l" if relative else "L"
        elif upper == "L":
            x, y = vals
            add((x + p[0], y + p[1]) if relative else (x, y))
        elif upper == "H":
            x = vals[0] + p[0] if relative else vals[0]
            add((x, p[1]))
        elif upper == "V":
            y = vals[0] + p[1] if relative else vals[0]
            add((p[0], y))
        elif upper == "C":
            x1, y1, x2, y2, x, y = vals
            if relative:
                p1, p2, end = (p[0] + x1, p[1] + y1), (p[0] + x2, p[1] + y2), (p[0] + x, p[1] + y)
            else:
                p1, p2, end = (x1, y1), (x2, y2), (x, y)
            origin = p
            for step in range(1, 13):
                add(cubic(origin, p1, p2, end, step / 12))
            last_cubic = p2
            last_quad = None
        elif upper == "S":
            x2, y2, x, y = vals
            p1 = (2 * p[0] - last_cubic[0], 2 * p[1] - last_cubic[1]) if last_cubic else p
            if relative:
                p2, end = (p[0] + x2, p[1] + y2), (p[0] + x, p[1] + y)
            else:
                p2, end = (x2, y2), (x, y)
            origin = p
            for step in range(1, 13):
                add(cubic(origin, p1, p2, end, step / 12))
            last_cubic = p2
            last_quad = None
        elif upper == "Q":
            x1, y1, x, y = vals
            p1, end = ((p[0] + x1, p[1] + y1), (p[0] + x, p[1] + y)) if relative else ((x1, y1), (x, y))
            origin = p
            for step in range(1, 13):
                add(quad(origin, p1, end, step / 12))
            last_quad = p1
            last_cubic = None
        elif upper == "T":
            x, y = vals
            p1 = (2 * p[0] - last_quad[0], 2 * p[1] - last_quad[1]) if last_quad else p
            end = (p[0] + x, p[1] + y) if relative else (x, y)
            origin = p
            for step in range(1, 13):
                add(quad(origin, p1, end, step / 12))
            last_quad = p1
            last_cubic = None
        elif upper == "A":
            raise ValueError("Arc commands are not expected in the four head paths")
        if upper not in {"C", "S", "Q", "T"}:
            last_cubic = last_quad = None
    if ring:
        rings.append(ring)
    return rings


def resample(points, count):
    closed = len(points) > 2 and points[0] == points[-1]
    work = points[:-1] if closed else points[:]
    segments = list(zip(work, work[1:] + ([work[0]] if closed else [])))
    lengths = [math.dist(a, b) for a, b in segments]
    total = sum(lengths)
    divisor = count if closed else max(1, count - 1)
    out = []
    seg_i = 0
    passed = 0.0
    for j in range(count):
        target = total * j / divisor
        while seg_i < len(segments) - 1 and passed + lengths[seg_i] < target:
            passed += lengths[seg_i]
            seg_i += 1
        a, b = segments[seg_i]
        length = lengths[seg_i] or 1.0
        u = min(1.0, max(0.0, (target - passed) / length))
        out.extend([round(a[0] + (b[0] - a[0]) * u, 1), round(a[1] + (b[1] - a[1]) * u, 1)])
    return out


def project_template_to_polyline(points, template):
    """Move each established morph vertex onto the nearest point of a new outline."""
    closed = len(points) > 2 and points[0] == points[-1]
    work = points[:-1] if closed else points[:]
    segments = list(zip(work, work[1:] + ([work[0]] if closed else [])))
    out = []
    for i in range(0, len(template), 2):
        px, py = template[i], template[i + 1]
        best_d2 = float("inf")
        best = (px, py)
        for a, b in segments:
            vx, vy = b[0] - a[0], b[1] - a[1]
            denom = vx * vx + vy * vy
            u = 0.0 if denom == 0 else ((px - a[0]) * vx + (py - a[1]) * vy) / denom
            u = min(1.0, max(0.0, u))
            qx, qy = a[0] + vx * u, a[1] + vy * u
            d2 = (px - qx) ** 2 + (py - qy) ** 2
            if d2 < best_d2:
                best_d2, best = d2, (qx, qy)
        out.extend([round(best[0], 1), round(best[1], 1)])
    return out


def normal_paths():
    root = ET.parse(SVG).getroot()
    normal = root.find(".//s:g[@id='normal']", NS)
    paths = normal.findall(".//s:path", NS)
    return {0: paths[0].get("d"), 1: paths[1].get("d"), 14: paths[14].get("d"), 15: paths[15].get("d")}


def polyline_length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def smoothstep(a, b, x):
    if x <= a:
        return 1.0
    if x >= b:
        return 0.0
    t = (x - a) / (b - a)
    return 1 - (t * t * (3 - 2 * t))


def build_d(rings):
    chunks = []
    for ring in rings:
        chunks.append("M" + ",".join(str(v) for v in ring[:2]))
        chunks.extend("L" + ",".join(str(v) for v in ring[i:i + 2]) for i in range(2, len(ring), 2))
        chunks.append("Z")
    return "".join(chunks)


def main():
    text = HTML.read_text()
    match = re.search(r"  const MORPH = (\[.*?\]);\n\n  const svg", text, re.S)
    if not match:
        raise RuntimeError("Could not locate MORPH JSON")
    morph = json.loads(match.group(1))
    baseline_text = subprocess.check_output(
        ["git", "show", "HEAD:punch.html"], cwd=ROOT, text=True
    )
    baseline_match = re.search(r"  const MORPH = (\[.*?\]);\n\n  const svg", baseline_text, re.S)
    if not baseline_match:
        raise RuntimeError("Could not locate baseline MORPH JSON")
    baseline = json.loads(baseline_match.group(1))
    sources = normal_paths()

    for idx in (0, 1, 14, 15):
        item = morph[idx]
        # Illustrator leaves a few zero-length diagnostic subpaths in detailed
        # compound outlines. They are not visible and were not part of MORPH.
        source_rings = [r for r in path_polylines(sources[idx]) if polyline_length(r) > 10]
        counts = [len(r) // 2 for r in baseline[idx]["rings"][0]]
        if len(source_rings) != len(counts):
            raise RuntimeError(f"mp{idx}: source has {len(source_rings)} rings, expected {len(counts)}")
        if idx in (0, 1):
            # Hair is identical in all states, so clean uniform sampling gives
            # the closest rendition of the edited Illustrator outline.
            latest = [resample(r, count) for r, count in zip(source_rings, counts)]
        else:
            # Face states still morph. Preserve their established vertex
            # correspondence while moving the normal outline to the new art.
            latest = [
                project_template_to_polyline(source_ring, old_ring)
                for source_ring, old_ring in zip(source_rings, baseline[idx]["rings"][0])
            ]
        item["rings"][0] = latest
        if idx in (0, 1):
            item["rings"][1] = [r[:] for r in latest]
            item["rings"][2] = [r[:] for r in latest]
        else:
            for state in (1, 2):
                for ri, normal_ring in enumerate(latest):
                    old_normal = baseline[idx]["rings"][0][ri]
                    old_target = baseline[idx]["rings"][state][ri]
                    target = item["rings"][state][ri]
                    for j in range(0, len(normal_ring), 2):
                        weight = smoothstep(390.0, 470.0, old_normal[j + 1])
                        dx = normal_ring[j] - old_normal[j]
                        dy = normal_ring[j + 1] - old_normal[j + 1]
                        tx = old_target[j] + dx * weight
                        ty = old_target[j + 1] + dy * weight

                        # The ears do not morph. Lock the adjacent temple
                        # contour to normal, then fade back into the animated
                        # jaw below the ear so no triangular seam can form.
                        x, y = old_normal[j], old_normal[j + 1]
                        left_side = smoothstep(-920.0, -850.0, x)
                        right_side = 1.0 - smoothstep(-520.0, -450.0, x)
                        # Release the cheek shortly below the ear so the
                        # punched-side outline curves inward before it meets
                        # the fist instead of tracking the normal jaw too far.
                        ear_vertical = smoothstep(500.0, 570.0, y) if y >= 320.0 else 0.0
                        ear_lock = max(left_side, right_side) * ear_vertical
                        tx = tx * (1 - ear_lock) + normal_ring[j] * ear_lock
                        ty = ty * (1 - ear_lock) + normal_ring[j + 1] * ear_lock
                        target[j] = round(tx, 1)
                        target[j + 1] = round(ty, 1)

    compact = json.dumps(morph, separators=(",", ":"))
    text = text[:match.start(1)] + compact + text[match.end(1):]
    for idx in (0, 1, 14, 15):
        d = build_d(morph[idx]["rings"][0])
        text, n = re.subn(
            rf'(<path id="mp{idx}"[^>]* d=")[^"]*(")',
            lambda m: m.group(1) + d + m.group(2),
            text,
            count=1,
        )
        if n != 1:
            raise RuntimeError(f"Could not update static mp{idx}")

    # Draw the face outline as one continuous stroke on the face silhouette.
    # The original mp15 uses nested filled rings; those can separate during a
    # partial morph and expose a skin-coloured wedge near the ears.
    text, n = re.subn(
        r'<path id="mp14"[^>]*? d="',
        '<path id="mp14" class="st5" stroke="#0A0A0A" stroke-width="17" '
        'stroke-linecap="round" stroke-linejoin="round" d="',
        text,
        count=1,
    )
    if n != 1:
        raise RuntimeError("Could not add the continuous face outline")
    text, n = re.subn(
        r'<path id="mp15"[^>]*? d="',
        '<path id="mp15" style="display:none" d="',
        text,
        count=1,
    )
    if n != 1:
        raise RuntimeError("Could not hide the nested legacy outline")
    HTML.write_text(text)


if __name__ == "__main__":
    main()
