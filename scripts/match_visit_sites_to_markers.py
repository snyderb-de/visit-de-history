#!/usr/bin/env python3
from __future__ import annotations

import csv
import difflib
import re
from pathlib import Path

STOP = {
    'the','of','and','in','at','for','or','on','a','an','de','delaware',
    'county','historic','district','community','center','station','museum',
    'house','mansion','church','school','courthouse'
}

ALIASES = {
    'governors house': ['woodburn'],
    'old statehouse or the green': ['state house', 'the green or market plaine'],
    'dickinson john house': ['home of john dickinson', 'john dickinson'],
    'thorne parson mansion': ['parson thorne mansion'],
    'ross gov william h house': ['governor william h h ross', 'governor ross mansion'],
    'sussex county courthouse and the circle': ['sussex county courthouse', 'old courthouse', 'georgetown'],
    'delaware breakwater and lewes harbor': ['delaware breakwater east end lighthouse', 'breakwater'],
    'fenwick island lighthouse station': ['transpeninsular line'],
    'camp rehoboth community center at 37 baltimore ave': ['city of rehoboth beach'],
    'african union church and cemetery of iron hill and museum complex': ['site of african union church and cemetery', 'iron hill school'],
    'old college historic district': ['old college'],
    'coochs bridge historic district': ['american position battle of coochs bridge', 'british position battle of coochs bridge'],
}


def norm(s: str) -> str:
    s = s.lower()
    s = s.replace('&', ' and ')
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def token_set(s: str) -> set[str]:
    t = [w for w in norm(s).split() if w and w not in STOP]
    return set(t)


def sim_name(a: str, b: str) -> float:
    an = norm(a)
    bn = norm(b)
    seq = difflib.SequenceMatcher(None, an, bn).ratio()

    at = token_set(a)
    bt = token_set(b)
    jacc = (len(at & bt) / len(at | bt)) if (at or bt) else 0.0

    contains = 0.0
    if an and bn and (an in bn or bn in an):
        contains = 0.12

    return max(seq * 0.72 + jacc * 0.28 + contains, seq)


def alias_boost(site_name: str, marker_name: str) -> float:
    sn = norm(site_name)
    mn = norm(marker_name)
    for key, vals in ALIASES.items():
        if key in sn:
            for v in vals:
                if norm(v) in mn:
                    return 0.28
    return 0.0


def city_bonus(site_city: str, marker_city: str) -> float:
    sc = norm(site_city)
    mc = norm(marker_city)
    if sc and mc and sc == mc:
        return 0.12
    if sc and mc and (sc in mc or mc in sc):
        return 0.06
    return 0.0


def load_csv(path: str) -> list[dict[str, str]]:
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def main() -> int:
    sites = load_csv('visit_sites_31.csv')
    markers = load_csv('master_markers_min.csv')

    out_rows = []

    for s in sites:
        best = None
        ranked = []
        for m in markers:
            score = sim_name(s['Site Name'], m['Marker Name'])
            score += city_bonus(s['City'], m['City'])
            score += alias_boost(s['Site Name'], m['Marker Name'])

            ranked.append((score, m))
            if best is None or score > best[0]:
                best = (score, m)

        ranked.sort(key=lambda x: x[0], reverse=True)

        top = ranked[0]
        second = ranked[1]
        confidence = 'high' if top[0] >= 0.90 else 'medium' if top[0] >= 0.78 else 'low'
        auto_match = top[0] >= 0.84 and (top[0] - second[0] >= 0.06)

        out_rows.append({
            '#': s['#'],
            'site_name': s['Site Name'],
            'site_city': s['City'],
            'match_status': 'auto' if auto_match else 'review',
            'confidence': confidence,
            'score': f"{top[0]:.3f}",
            'marker_number': top[1]['Marker Number'],
            'marker_name': top[1]['Marker Name'],
            'marker_city': top[1]['City'],
            'marker_url': top[1]['Marker URL'],
            'photo_url': top[1]['Photo URL'],
            'second_score': f"{second[0]:.3f}",
            'second_marker': second[1]['Marker Name'],
        })

    out = Path('visit_marker_matches.csv')
    with out.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    auto = [r for r in out_rows if r['match_status'] == 'auto']
    print(f"wrote {out} | auto={len(auto)} review={len(out_rows)-len(auto)}")

    # Write just confirmed auto matches with available photos
    confirmed = [r for r in auto if r['photo_url']]
    out2 = Path('visit_marker_matches_auto_with_photos.csv')
    with out2.open('w', newline='', encoding='utf-8') as f:
        cols=['#','site_name','site_city','marker_number','marker_name','marker_url','photo_url','score']
        w=csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in confirmed:
            w.writerow({k:r[k] for k in cols})
    print(f"wrote {out2} | rows={len(confirmed)}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
