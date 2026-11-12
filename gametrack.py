"""Absinthe Edge: game-winner tracker.

Before each game it saves the projected winner, score, win chance, sureness grade and pick grade
(the same math the page uses). After the final it grades them, so the Accuracy tab can show
whether A games really win more often than C games, and where the model needs tuning.

Reads data/board.json, keeps its history in data/gametrack.json.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

import requests

DATA = "data"
OUT = f"{DATA}/gametrack.json"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
PATH = {"NBA": "basketball/nba", "NFL": "football/nfl"}
GRADE_AFTER_H = {"NFL": 4.0, "NBA": 3.0}
GIVE_UP_H = 72
KEEP_ROWS = 2000


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def parse_t(t):
    try:
        return datetime.fromisoformat(str(t).replace("Z", "")).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def jround(x):  # JavaScript Math.round
    return int(math.floor(x + .5))


def sign(x):
    return (x > 0) - (x < 0)


# ------------------------------------------------ grades (same math as gameGrade / winnerPick in index.html)
def game_grade(g):
    pr = g.get("proj") or {}
    if pr.get("pHome") is None:
        return None
    nfl = g.get("sport") == "NFL"
    wp = max(pr["pHome"], 1 - pr["pHome"])
    s = min(1, max(0, wp - .5) / .25) * 45
    m_scale, t_scale = (7, 7) if nfl else (10, 12)
    vm, mm = pr.get("vegasMargin"), pr.get("modelMargin")
    if vm is not None and mm is not None:
        same = sign(mm) == sign(vm) or abs(vm) < .5
        d = abs(mm - vm)
        if same:
            s += 25 * max(0, 1 - d / m_scale)
    else:
        s += 14
    if g.get("ou") and pr.get("modelTotal") is not None:
        s += 15 * max(0, 1 - abs(pr["modelTotal"] - g["ou"]) / t_scale)
    else:
        s += 8
    st = g.get("stand") or {}
    n = lambda t: (t or {}).get("w", 0) + (t or {}).get("l", 0)
    gp = min(n(st.get("home")), n(st.get("away")))
    s += 15 * min(1, gp / (8 if nfl else 20))
    w = g.get("weather") or {}
    if nfl and w and not (g.get("venue") or {}).get("indoor") and ((w.get("wind") or 0) >= 15 or (w.get("rainPct") or 0) >= 60):
        s -= 8
    ctx = g.get("ctx") or {}
    if any((ctx.get(g[k]["abbr"]) or {}).get("b2b") for k in ("away", "home")):
        s -= 3
    outs = sum(len((g.get("out") or {}).get(g[k]["abbr"]) or []) for k in ("away", "home"))
    if outs >= 6:
        s -= 5
    s = max(0, min(100, jround(s)))
    gr = "A" if s >= 70 else "B" if s >= 56 else "C" if s >= 42 else "D"
    if gr == "A" and (vm is None or abs((mm or 0) - vm) >= m_scale * .6):
        gr = "B"
    return gr, s


def winner_pick(g, game_picks):
    q = game_grade(g)
    if not q:
        return None
    pr = g["proj"]
    home_wins = pr["homePts"] >= pr["awayPts"]
    w = g["home"] if home_wins else g["away"]
    label = (w["short"] + " to win").lower()
    mk = next((p for p in game_picks if p.get("gameId") == g["id"] and p.get("type") == "ml"
               and (p.get("label") or "").lower() == label), None)
    score, price, edge = q[1], None, None
    if mk and mk.get("yesAsk"):
        p_yes = mk["prob"] if (mk.get("side") or "YES").upper() == "YES" else 1 - mk["prob"]
        price = mk["yesAsk"]
        edge = p_yes - price
        score += max(-15, min(15, edge * 150))
    score = jround(max(0, min(100, score)))
    gr = "A" if score >= 72 else "B" if score >= 58 else "C" if score >= 44 else "D"
    return {"sureGr": q[0], "sureS": q[1], "pickGr": gr, "pickS": score, "pick": w["abbr"],
            "pWin": round(pr["pHome"] if home_wins else 1 - pr["pHome"], 3),
            "price": price, "edge": None if edge is None else round(edge, 3)}


# ------------------------------------------------ snapshot before the game
def snapshot(board, pending, now):
    n = 0
    for sport, s in (board.get("sports") or {}).items():
        for g in s.get("games") or []:
            st = parse_t(g.get("start"))
            if not st or st <= now or g.get("completed") or (g.get("state") not in (None, "pre")):
                continue  # locked once the game starts
            wp = winner_pick(g, s.get("gamePicks") or [])
            if not wp:
                continue
            pr = g["proj"]
            pending[g["id"]] = dict(wp, id=g["id"], s=sport, t=g["start"], name=g.get("name"),
                                    away=g["away"]["abbr"], home=g["home"]["abbr"],
                                    projA=round(pr["awayPts"], 1), projH=round(pr["homePts"], 1),
                                    vegasMargin=pr.get("vegasMargin"), ou=g.get("ou"))
            n += 1
    return n


# ------------------------------------------------ grade after the final
_sb = {}
def finals(sport, day):
    key = (sport, day)
    if key not in _sb:
        try:
            r = requests.get(f"{ESPN}/{PATH[sport]}/scoreboard", params={"dates": day}, timeout=25)
            evs = r.json().get("events", [])
        except Exception:
            evs = []
        out = {}
        for e in evs:
            c = (e.get("competitions") or [{}])[0]
            if not ((e.get("status") or {}).get("type") or {}).get("completed"):
                continue
            sc = {x["homeAway"]: float(x.get("score") or 0) for x in c.get("competitors", [])}
            out[e["id"]] = sc
        _sb[key] = out
    return _sb[key]


def grade(pending, rows, now):
    done = []
    for gid, p in list(pending.items()):
        st = parse_t(p["t"])
        if not st or now < st + timedelta(hours=GRADE_AFTER_H.get(p["s"], 3)):
            continue
        day = (st - timedelta(hours=5)).strftime("%Y%m%d")
        sc = finals(p["s"], day).get(gid)
        if sc is None:  # late games can land on the next ET date
            sc = finals(p["s"], (st - timedelta(hours=5) + timedelta(days=1)).strftime("%Y%m%d")).get(gid)
        if sc is None:
            if now > st + timedelta(hours=GIVE_UP_H):
                done.append(gid)
            continue
        a, h = sc.get("away", 0), sc.get("home", 0)
        winner = p["home"] if h > a else p["away"] if a > h else None
        row = dict(p, actA=a, actH=h, d=(st - timedelta(hours=5)).date().isoformat(),
                   res="push" if winner is None else ("w" if winner == p["pick"] else "l"),
                   scoreErr=round((abs(p["projA"] - a) + abs(p["projH"] - h)) / 2, 1),
                   totErr=round(abs((p["projA"] + p["projH"]) - (a + h)), 1),
                   marErr=round(abs((p["projH"] - p["projA"]) - (h - a)), 1))
        rows.append(row)
        done.append(gid)
    for gid in done:
        pending.pop(gid, None)
    return len(done)


# ------------------------------------------------ summaries + tuning notes
def agg(rows):
    r = [x for x in rows if x["res"] in ("w", "l")]
    n = len(r)
    if not n:
        return {"n": 0}
    w = sum(x["res"] == "w" for x in r)
    priced = [x for x in r if x.get("price")]
    pl = sum((1 - x["price"]) if x["res"] == "w" else -x["price"] for x in priced)
    stake = sum(x["price"] for x in priced)
    return {"n": n, "w": w, "l": n - w, "hit": round(w / n, 4), "exp": round(sum(x["pWin"] for x in r) / n, 4),
            "scoreErr": round(sum(x["scoreErr"] for x in r) / n, 1), "totErr": round(sum(x["totErr"] for x in r) / n, 1),
            "marErr": round(sum(x["marErr"] for x in r) / n, 1),
            "roi": round(pl / stake, 4) if stake else None, "priced": len(priced)}


BUCKETS = [(.5, .55, "50-55"), (.55, .6, "55-60"), (.6, .65, "60-65"), (.65, .7, "65-70"), (.7, 1.01, "70+")]


def summarize(rows):
    out = {}
    for sport in ("all", "NBA", "NFL"):
        rs = [x for x in rows if sport == "all" or x["s"] == sport]
        out[sport] = {
            "all": agg(rs),
            "sure": {g: agg([x for x in rs if x["sureGr"] == g]) for g in "ABCD"},
            "pick": {g: agg([x for x in rs if x["pickGr"] == g]) for g in "ABCD"},
            "conf": {lab: agg([x for x in rs if lo <= x["pWin"] < hi]) for lo, hi, lab in BUCKETS},
            "notes": notes(rs),
        }
    return out


def notes(rs, min_n=10):
    """Plain-English tuning hints once there's enough data."""
    out = []
    if len([x for x in rs if x["res"] in ("w", "l")]) < min_n:
        return ["Not enough graded games yet. Hints show up after 10+ finals."]
    tot = agg(rs)
    if tot["hit"] - tot["exp"] <= -.08:
        out.append(f"Projected winners are hitting {tot['hit']:.0%} but the model expected {tot['exp']:.0%}: overall it's too confident.")
    elif tot["hit"] - tot["exp"] >= .08:
        out.append(f"Projected winners are hitting {tot['hit']:.0%} vs {tot['exp']:.0%} expected: overall it's too cautious.")
    sure = {g: agg([x for x in rs if x["sureGr"] == g]) for g in "ABCD"}
    seen = [(g, a) for g, a in sure.items() if a["n"] >= min_n]
    for (g1, a1), (g2, a2) in zip(seen, seen[1:]):
        if a2["hit"] > a1["hit"] + .03:
            out.append(f"{g2} games are winning more often than {g1} games ({a2['hit']:.0%} vs {a1['hit']:.0%}), so the sureness grade isn't sorting games well yet.")
    for lo, hi, lab in BUCKETS:
        a = agg([x for x in rs if lo <= x["pWin"] < hi])
        if a["n"] >= min_n:
            gap = a["hit"] - a["exp"]
            if gap <= -.08:
                out.append(f"When the model says {lab}%, teams win only {a['hit']:.0%}: it's too confident there and those chances should be pulled toward 50/50.")
            elif gap >= .08:
                out.append(f"When the model says {lab}%, teams win {a['hit']:.0%}: it's too cautious there and could trust those favorites more.")
    pick = {g: agg([x for x in rs if x["pickGr"] == g]) for g in "ABCD"}
    for g in "AB":
        a = pick[g]
        if a["n"] >= min_n and a.get("roi") is not None and a["roi"] < -.05:
            out.append(f"Pick grade {g} is losing money at Kalshi prices ({a['roi']:+.0%} ROI); the price adjustment may need to be stronger.")
    if not out:
        out.append("No clear problems so far. Grades with fewer than 10 finals aren't judged yet.")
    return out


def main():
    now = datetime.now(timezone.utc)
    board = load(f"{DATA}/board.json", {})
    st = load(OUT, {})
    pending, rows = st.get("pending", {}), st.get("rows", [])
    graded = grade(pending, rows, now)
    saved = snapshot(board, pending, now)
    rows = rows[-KEEP_ROWS:]
    out = {"updated": now.isoformat()[:19] + "Z", "since": st.get("since") or now.date().isoformat(),
           "pending": pending, "rows": rows, "summary": summarize(rows)}
    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"Game tracker: {saved} upcoming games saved, {graded} graded, {len(rows)} in history")


if __name__ == "__main__":
    main()
