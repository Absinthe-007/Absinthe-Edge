"""Absinthe Edge: builds data/board.json, data/players.json and data/track.json.

Run:  python run.py
"""
import time
import traceback
from datetime import datetime, timedelta, timezone

import sources as S
import model as M
import extras as X
import feeds as F

VERSION = "3.6"

DATA = "data"
GRADE_AFTER_H = {"NFL": 4.0, "NBA": 3.0}
GIVE_UP_H = 72
KALSHI_WAIT_H = 30  # wait this long after start for Kalshi to settle before grading a Kalshi pick on ESPN alone
FAR_FROM_MARKET = 0.20
DOWN = {"Lock": "Strong", "Strong": "Lean"}


def et_code(t):
    return (t - timedelta(hours=5)).strftime("%y%b%d").upper()


def match_code(sport, code, games):
    """Kalshi event code like 26NOV09BUFMIN -> ESPN game."""
    if len(code) < 9:
        return None
    dc, teams = code[:7], code[7:]
    for g in games.values():
        t = S.parse_t(g["start"])
        if not t or dc not in {et_code(t), et_code(t - timedelta(days=1)), et_code(t + timedelta(days=1))}:
            continue
        for a in S.abbr_set(sport, g["away"]["abbr"]):
            for h in S.abbr_set(sport, g["home"]["abbr"]):
                if teams in (a + h, h + a):
                    return g
    return None


def game_for_team(sport, team, games):
    ts = S.abbr_set(sport, team)
    best = None
    for g in games.values():
        if g["home"]["abbr"] in ts or g["away"]["abbr"] in ts:
            if not best or g["start"] < best["start"]:
                best = g
    return best


def find_player(sport, name, team_abbrs, roster, by_name):
    nn = S.norm_name(name)
    cands = by_name.get(nn) or []
    if not cands and nn:  # loose match: same last name + first initial
        parts = nn.split()
        cands = [p for p in roster.values() if p["nn"].split()[-1:] == parts[-1:] and p["nn"][:1] == nn[:1]]
    team_sets = set().union(*[S.abbr_set(sport, t) for t in team_abbrs if t]) if team_abbrs else set()
    on_team = [p for p in cands if p["team"] in team_sets]
    if on_team:
        return on_team[0]
    return cands[0] if len(cands) == 1 else None


def row(g, keys):
    return [g["date"], "vs" if g["home"] else "@", g["opp"], g["res"], g["score"]] + [g["s"].get(k) for k in keys]


def build_sport(sport, now, track, geo):
    games = S.espn_games(sport, now)
    games = {k: g for k, g in games.items() if not g["completed"]}
    if not games:
        return None
    stand = S.standings(sport)
    teams = S.teams_list(sport)
    roster = S.rosters(sport, teams)
    # V2.2 feeds: official injury reports (+ nflverse usage, depth charts) before anything reads injuries
    prof, feeds_meta = {}, {}
    try:
        if sport == "NFL":
            prof, nm = F.nfl_profiles(now, roster)
            F.apply_nfl_injuries(roster, prof)
            feeds_meta["nflverseWeek"] = nm.get("week") if nm else None
        else:
            rep_, stamp = F.nba_injury_report(now, teams)
            F.apply_nba_injuries(roster, rep_)
            feeds_meta["nbaReport"] = stamp
    except Exception:
        S.log(f"[feeds] {sport} failed\n" + traceback.format_exc())
    by_name = {}
    for p in roster.values():
        by_name.setdefault(p["nn"], []).append(p)

    # season box scores -> defense vs position (cached)
    bpath = f"{DATA}/cache/boxes_{sport}.json"
    boxes = S.load_json(bpath, {})
    ids = S.season_event_ids(sport, now)
    missing = [e for e in ids if e not in boxes]
    for eid, b in zip(missing, S.pmap(lambda e: S.box_summary(sport, e), missing, 8)):
        if b.get("done") and b.get("teams"):
            boxes[eid] = b
    boxes = {k: v for k, v in boxes.items() if k in ids}
    S.save_json(bpath, boxes)
    dvp = M.build_dvp(sport, boxes, roster)
    usage = X.build_usage(sport, boxes)
    S.log(f"[dvp] {sport}: {len(boxes)} box scores, {len(usage['players'])} players with usage")

    # lines
    p6props, payouts = S.pick6(sport)
    krows = S.kalshi(sport)
    try:
        cons, odds_meta = F.odds_consensus(sport, games, now)
    except Exception:
        S.log(f"[odds] {sport} failed\n" + traceback.format_exc())
        cons, odds_meta = {}, None
    feeds_meta["odds"] = odds_meta

    # contexts
    sched = {}
    tids = {g[s]["id"] for g in games.values() for s in ("home", "away")}
    for tid, sc in zip(tids, S.pmap(lambda t: S.team_schedule(sport, t), list(tids), 8)):
        sched[tid] = sc
    ctx, wx, gproj = {}, {}, {}
    for gid, g in games.items():
        for s in ("home", "away"):
            ctx[(gid, g[s]["abbr"])] = M.team_context(sport, sched.get(g[s]["id"], []), g, geo)
        if sport == "NFL" and not g["venue"]["indoor"]:
            c = S.coords(g["venue"]["city"], geo)
            t = S.parse_t(g["start"])
            if c and t:
                wx[gid] = S.weather(c[0], c[1], t.replace(minute=0))
            if not wx.get(gid) and g.get("espnWeather"):
                ew = g["espnWeather"]
                wx[gid] = {"temp": ew.get("temperature"), "desc": ew.get("displayValue"), "wind": None, "src": "ESPN"}
        vm = S.vegas_margin(g)
        gproj[gid] = M.project_game(g, stand, ctx[(gid, g["home"]["abbr"])], ctx[(gid, g["away"]["abbr"])], wx.get(gid), vm)

    # attach props to games + ESPN players
    need = {}
    props = []
    for p in p6props:
        g = game_for_team(sport, p["team"], games)
        if not g:
            continue
        pl = find_player(sport, p["player"], [p["team"]], roster, by_name)
        if not pl:
            continue
        p.update({"gameId": g["id"], "pid": pl["id"]})
        need[pl["id"]] = (pl, g)
        props.append(p)
    kgame = []
    for r in krows:
        g = match_code(sport, r["code"], games)
        if not g:
            continue
        r["gameId"] = g["id"]
        if r["kind"] == "game":
            kgame.append(r)
            continue
        pl = find_player(sport, r["player"], [g["home"]["abbr"], g["away"]["abbr"]], roster, by_name)
        if not pl:
            continue
        r["pid"] = pl["id"]
        need[pl["id"]] = (pl, g)
        props.append(r)
    S.log(f"[match] {sport}: {len(props)} player props matched, {len(need)} players, {len(kgame)} Kalshi game markets")

    # game logs + projections
    logs = dict(zip(need, S.pmap(lambda a: S.gamelog(sport, a, now), list(need), 10)))
    players, details = {}, {}
    injc, pacec = {}, {}
    for gid, g in games.items():
        h, a = g["home"]["abbr"], g["away"]["abbr"]
        injc[(gid, h)] = X.injury_context(sport, h, a, roster, usage)
        injc[(gid, a)] = X.injury_context(sport, a, h, roster, usage)
        pacec[gid] = X.game_pace(usage, h, a) if sport == "NBA" else (None, None)
    lines_by_pid = {}
    for p in props:
        lines_by_pid.setdefault(p["pid"], set()).add(p["key"])
    for aid, (pl, g) in need.items():
        is_home = pl["team"] in S.abbr_set(sport, g["home"]["abbr"])
        opp = g["away"]["abbr"] if is_home else g["home"]["abbr"]
        team = g["home"]["abbr"] if is_home else g["away"]["abbr"]
        implied = M.implied_totals(g, gproj[g["id"]]).get(team)
        pl = dict(pl, team=team)
        gl = logs.get(aid) or []
        tctx = ctx.get((g["id"], team)) or {}
        rest = X.player_rest(sport, gl, g, sched.get(g["home"]["id"] if is_home else g["away"]["id"], []), usage, aid, team)
        ext = combine(X.extra_factors(sport, rest, injc.get((g["id"], team), {}), pacec[g["id"]][1], tctx.get("b2b")),
                      F.nfl_factors(prof.get(aid)) if sport == "NFL" else F.nba_factors(pl))
        grp, proj = M.project_player(sport, pl, gl, g, is_home, opp, dvp, tctx, wx.get(g["id"]), stand, implied, ext)
        if not proj:
            continue
        keys = list(proj.keys())
        dk = next((p.get("dkImg") for p in props if p.get("pid") == aid and p.get("dkImg")), "")
        rk = (dvp["ranks"].get(opp) or {}).get(grp)
        players[aid] = {"id": aid, "name": pl["name"], "team": team, "opp": opp, "home": is_home, "gameId": g["id"], "pos": pl["pos"], "grp": grp,
                        "img": pl["img"], "dkImg": dk, "inj": pl.get("inj"), "proj": {k: round(v["proj"], 1) for k, v in proj.items()},
                        "oppRank": rk, "rest": {k: v for k, v in rest.items()}, "injSrc": pl.get("injSrc"),
                        "depth": (prof.get(aid) or {}).get("depth"),
                        "l10": {k: list(reversed(proj[k]["l10"])) for k in proj if k in lines_by_pid.get(aid, ())}}
        mainkey = next((k for k in ("points", "pass_yds", "rush_rec_yds", "rec_yds", "tackles", "kick_pts") if k in proj), keys[0])
        mm = 1.0
        for f in proj[mainkey]["factors"]:
            mm *= f[1]
        players[aid]["grade"] = X.grade(mm)
        cur = [x for x in gl if x["season"] == gl[0]["season"]] if gl else []
        details[aid] = {"bio": {k: v for k, v in {"ht": pl.get("ht"), "wt": pl.get("wt"), "age": pl.get("age"), "college": pl.get("college"),
                                                  "exp": pl.get("exp"), "jersey": pl.get("jersey"), "posName": pl.get("posName"),
                                                  "injDetail": pl.get("injDetail")}.items() if v not in (None, "")},
                        "keys": keys, "labels": [M.LABEL.get(k, k) for k in keys],
                        "stats": {k: {x: v[x] for x in ("proj", "base", "sd", "n", "parts", "nvs", "nloc", "factors")} for k, v in proj.items()},
                        "avg": [round(M.mean([x["s"].get(k) for x in cur]) or 0, 1) if cur else None for k in keys],
                        "recent": [row(x, keys) for x in gl[:10]],
                        "vs": [row(x, keys) + [x["season"]] for x in gl if x["opp"] in S.abbr_set(sport, opp)][:12],
                        "ctx": ctx.get((g["id"], team)), "gp": len(cur), "rest": rest,
                        "inj": {k: v for k, v in injc.get((g["id"], team), {}).items() if k in ("mates", "foes")},
                        "feeds": {k: v for k, v in (prof.get(aid) or {}).items() if k in ("snap", "share", "rz", "ngs", "inj", "depth")}}

    # picks
    calib = track.get("calib") or {}
    be = payouts.get(3) or 0.5503
    ladders = {}
    for p in props:
        if p["platform"] == "Kalshi":
            ladders.setdefault((p["pid"], p["key"]), []).append((p["line"], M.market_mid(p)))

    def ladder_p(pid, key, line):
        """Market P(over line) interpolated from the Kalshi ladder for this player + stat."""
        pts = sorted(ladders.get((pid, key)) or [])
        if not pts or line < pts[0][0] - 1e-9 or line > pts[-1][0] + 1e-9:
            return None
        for (l1, p1), (l2, p2) in zip(pts, pts[1:] + [pts[-1]]):
            if abs(line - l1) < 1e-9:
                return p1
            if l1 < line < l2:
                return p1 + (p2 - p1) * (line - l1) / (l2 - l1)
        return None
    picks = []
    for p in props:
        pl = players.get(p.get("pid"))
        st = (logs.get(p.get("pid")) and None)
        proj = None
        if pl:
            d = details[p["pid"]]["stats"].get(p["key"])
            proj = d
        if not proj:
            # stat not in the player's standard set (e.g. QB receptions) -> project on the fly
            if not pl:
                continue
            gl = logs.get(p["pid"]) or []
            vals = [x["s"].get(p["key"]) for x in gl[:16] if isinstance(x["s"].get(p["key"]), (int, float))]
            if len(vals) < 3:
                continue
            mu = M.mean(vals)
            proj = {"proj": mu, "sd": max(M.CV.get(p["key"], 0.5) * max(mu, 1), 1.0), "n": len(vals), "factors": []}
        raw = M.p_over(p["key"], proj["proj"], proj["sd"], p["line"])
        pm = M.calibrate(raw, calib, sport, p["key"])
        gl = logs.get(p["pid"]) or []
        l10 = [x["s"].get(p["key"]) for x in gl[:10] if isinstance(x["s"].get(p["key"]), (int, float))]
        vsl = [x["s"].get(p["key"]) for x in gl if x["opp"] in S.abbr_set(sport, pl["opp"]) and isinstance(x["s"].get(p["key"]), (int, float))][:10]
        base = {"id": p["id"], "platform": p["platform"], "sport": sport, "pid": p["pid"], "player": pl["name"], "team": pl["team"], "opp": pl["opp"],
                "gameId": p["gameId"], "key": p["key"], "stat": p.get("stat") or M.LABEL.get(p["key"], p["key"]), "line": p["line"],
                "proj": round(proj["proj"], 1), "rawP": round(raw, 3), "start": games[p["gameId"]]["start"],
                "l10": [sum(1 for v in l10 if v > p["line"]), len(l10)], "vs": [sum(1 for v in vsl if v > p["line"]), len(vsl)],
                "inj": pl.get("inj")}
        injured = (pl.get("inj") or "").lower() in ("out", "doubtful", "injured reserve", "suspension")
        base["streak"] = X.streak(l10, p["line"])
        fm = 1.0
        for f in proj.get("factors") or []:
            fm *= f[1]
        base["grade"] = X.grade(fm)
        mid = M.market_mid(p) if p["platform"] == "Kalshi" else ladder_p(p["pid"], p["key"], p["line"])
        base["mkt"] = round(mid, 3) if mid is not None else None
        base["model"] = round(pm, 3)
        co = cons.get((S.norm_name(pl["name"]), p["key"]))
        cp = F.cons_p_at(co, p["key"], p["line"], proj["sd"], p["key"] in M.COUNT) if co else None
        if co:
            base["cons"] = {"line": co["line"], "p": round(cp, 3), "books": co["books"]}
            if p["platform"] == "Pick6" and abs(co["line"] - p["line"]) >= (1.5 if p["key"] in M.CV and p["key"] not in M.COUNT else 0.5):
                base["consGap"] = round(co["line"] - p["line"], 1)
        refs = [x for x in (mid, cp) if x is not None]
        ref = sum(refs) / len(refs) if refs else None
        if ref is not None:
            base["far"] = abs(base["model"] - ref) >= FAR_FROM_MARKET
            pm = M.blend(pm, ref)
        if p["platform"] == "Pick6":
            side = "MORE" if pm >= 0.5 else "LESS"
            prob = pm if side == "MORE" else 1 - pm
            mult = p.get("moreMult") if side == "MORE" else p.get("lessMult")
            # a reduced (or boosted) Pick6 multiplier changes the hit rate needed to break even
            be_p = be * (mult ** (-1 / 3)) if mult else be
            base.update({"side": side, "prob": round(prob, 3), "edge": round(prob - be_p, 3), "mult": mult, "offered": mult is not None,
                         "rec": mult is not None and prob >= be_p + 0.02 and proj["n"] >= 3 and not injured,
                         "url": "https://pick6.draftkings.com/?sport=" + sport})
        else:
            ks = M.kalshi_side(pm, p, 0.05)
            base.update(ks)
            base.update({"title": p["title"], "ticker": p["ticker"], "url": p["url"], "yesAsk": p["yesAsk"], "noAsk": p["noAsk"]})
            base["rec"] = base["rec"] and proj["n"] >= 3 and not injured
        picks.append(base)
    for p in picks:  # express model/market probability for the side we picked
        if p["side"] in ("LESS", "NO"):
            p["model"] = round(1 - p["model"], 3)
            if p.get("mkt") is not None:
                p["mkt"] = round(1 - p["mkt"], 3)
            if p.get("cons"):
                p["cons"]["p"] = round(1 - p["cons"]["p"], 3)
    # drop far-out ladder rungs nobody should play
    picks = [p for p in picks if p["platform"] == "Pick6" or 0.04 <= p["yesAsk"] <= 0.96]
    # the rung priced closest to 50/50 is the "main" line used to grade the model's lean
    main = {}
    for p in picks:
        if p["platform"] == "Kalshi":
            k = (p["pid"], p["key"])
            if k not in main or abs(p["yesAsk"] - 0.5) < abs(main[k]["yesAsk"] - 0.5):
                main[k] = p
    for p in main.values():
        p["main"] = True
    # Kalshi ladders: only the best rung per player + stat counts as a recommended pick
    best = {}
    for p in picks:
        if p["platform"] == "Kalshi" and p["rec"]:
            k = (p["pid"], p["key"])
            if k not in best or p["edge"] > best[k]["edge"]:
                best[k] = p
    for p in picks:
        if p["platform"] == "Kalshi" and p["rec"] and best.get((p["pid"], p["key"])) is not p:
            p["rec"] = False
            p["alt"] = True
    # line movement since we first saw each line
    opens = track.setdefault("open", {})
    for p in picks:
        cur = p["line"] if p["platform"] == "Pick6" else round((p["yesAsk"] + (1 - p["noAsk"])) / 2, 3)
        o = opens.get(p["id"])
        if not o:
            opens[p["id"]] = [cur, p["start"]]
            continue
        mv = round(cur - o[0], 3)
        if abs(mv) >= (0.5 if p["platform"] == "Pick6" else 0.03):
            p["mv"] = mv
            p["open"] = o[0]
            toward = mv if p["side"] in ("MORE", "YES") else -mv
            p["mvOurs"] = toward > 0
    for p in picks:
        p["tier"] = X.tier(p)
        if p.get("far") and p["tier"] in DOWN:
            p["tier"] = DOWN[p["tier"]]  # model and market disagree a lot: one confidence step lower
    gpicks = []
    for m in kgame:
        g = games[m["gameId"]]
        p, label = M.kalshi_game_prob(m, g, gproj[g["id"]])
        if p is None:
            continue
        model_p = p
        far = abs(model_p - M.market_mid(m)) >= FAR_FROM_MARKET
        p = M.blend(p, M.market_mid(m), 0.35)
        ks = M.kalshi_side(p, m, 0.04)
        gpicks.append({"id": m["id"], "platform": "Kalshi", "sport": sport, "gameId": g["id"], "type": m["key"], "label": label,
                       "line": m["line"], "team": m.get("team"), "title": m["title"], "ticker": m["ticker"], "url": m["url"],
                       "yesAsk": m["yesAsk"], "noAsk": m["noAsk"], "start": g["start"], "rawP": round(model_p, 3),
                       "model": round(model_p, 3), "mkt": round(M.market_mid(m), 3), "far": far, **ks})

    gmain = {}
    for p in gpicks:
        if p["side"] == "NO":
            p["model"], p["mkt"] = round(1 - p["model"], 3), round(1 - p["mkt"], 3)
        k = (p["gameId"], p["type"])
        if k not in gmain or abs(p["yesAsk"] - 0.5) < abs(gmain[k]["yesAsk"] - 0.5):
            gmain[k] = p
    for p in gmain.values():
        p["main"] = True
    gbest = {}
    for p in gpicks:
        if p["rec"]:
            k = (p["gameId"], p["type"])
            if k not in gbest or p["edge"] > gbest[k]["edge"]:
                gbest[k] = p
    for p in gpicks:
        if p["rec"] and gbest.get((p["gameId"], p["type"])) is not p:
            p["rec"] = False
            p["alt"] = True
    for p in gpicks:
        p["tier"] = X.tier(p)
        if p.get("far") and p["tier"] in DOWN:
            p["tier"] = DOWN[p["tier"]]

    for gid, g in games.items():
        g["proj"] = gproj[gid]
        g["pace"] = {"game": pacec[gid][0], g["home"]["abbr"]: (usage["pace"].get(g["home"]["abbr"]) if sport == "NBA" else None),
                     g["away"]["abbr"]: (usage["pace"].get(g["away"]["abbr"]) if sport == "NBA" else None)} if pacec[gid][0] else None
        g["out"] = {g[s]["abbr"]: injc[(gid, g[s]["abbr"])]["mates"] for s in ("home", "away")}
        g["weather"] = wx.get(gid)
        g["ctx"] = {g["home"]["abbr"]: ctx.get((gid, g["home"]["abbr"])), g["away"]["abbr"]: ctx.get((gid, g["away"]["abbr"]))}
        g["stand"] = {s: {k: v for k, v in (stand.get(g[s]["abbr"]) or {}).items() if k in ("w", "l", "pf", "pa", "pfRank", "paRank", "pctRank", "home", "road")}
                      for s in ("home", "away")}
        for s in ("home", "away"):
            g["stand"][s] = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in g["stand"][s].items()}
        g.pop("espnWeather", None)
    S.log(f"[picks] {sport}: {len(picks)} prop picks ({sum(p['rec'] for p in picks)} recommended), {sum(p['rec'] for p in gpicks)} game picks")
    return {"games": sorted(games.values(), key=lambda g: g["start"]), "players": players, "details": details, "picks": picks,
            "gamePicks": gpicks, "breakeven": payouts, "dvpRanks": dvp["ranks"], "nBoxes": len(boxes), "feeds": feeds_meta}


def combine(*fns):
    def f(k, grp, base=None):
        out = []
        for fn in fns:
            out += fn(k, grp, base)
        return out
    return f


# ======================================================================================
# Self-grading
# ======================================================================================
LABEL_KEY = {v: k for k, v in M.LABEL.items()}


def fill_graded(r):
    """Older graded rows lack stat key / bet type / tier: work them out from what was stored."""
    if len(r) >= 15:
        return r
    s, pf, nm, stat, line, side, prob, val, res, day, rec, price = r[:12]
    st = (stat or "").lower()
    typ = "ml" if st.endswith(" to win") else "spread" if " wins by " in st else "total" if st.startswith("total over") else "prop"
    key = typ if typ != "prop" else (LABEL_KEY.get(stat) or S.stat_key(stat, s) or st)
    tier = None
    if rec:
        if pf == "Pick6":
            tier = "Lock" if prob >= 0.68 else "Strong" if prob >= 0.62 else "Lean"
        elif price:
            edge = prob - price
            tier = "Lock" if edge >= 0.10 else "Strong" if edge >= 0.07 else "Lean"
    return [s, pf, nm, stat, line, side, prob, val, res, day, rec, price, key, typ, tier]


def conf_bucket(p):
    for lo in (0.70, 0.65, 0.60, 0.55):
        if p >= lo:
            return f"{int(lo*100)}+" if lo == 0.70 else f"{int(lo*100)}-{int(lo*100)+5}"
    return "50-55"


def bump(agg, key, res, profit=None):
    r = agg.setdefault(key, {"w": 0, "l": 0, "p": 0, "v": 0, "stake": 0.0, "pl": 0.0})
    k = {"w": "w", "l": "l", "push": "p", "void": "v", "disp": "d"}[res]
    r[k] = r.get(k, 0) + 1
    if profit is not None and res in ("w", "l"):
        r["stake"] += 1
        r["pl"] = round(r["pl"] + profit, 3)


def save_pending(track, board, now):
    pend = track.setdefault("pending", {})
    pproj = track.setdefault("pendingProj", {})
    for sport, b in board.items():
        if not b:
            continue
        for p in b["picks"] + b["gamePicks"]:
            t = S.parse_t(p["start"])
            if not t or t <= now:
                continue
            if p["platform"] == "Kalshi" and not (p["rec"] or p.get("main")):
                continue
            pend[p["id"]] = {"s": sport, "pf": p["platform"], "g": p["gameId"], "t": p["start"], "pid": p.get("pid"),
                             "nm": p.get("player") or p.get("label"), "k": p.get("key") or p.get("type"), "line": p["line"],
                             "side": p["side"], "prob": p["prob"], "raw": p.get("rawP"), "rec": bool(p["rec"]), "price": p.get("price"),
                             "team": p.get("team"), "stat": p.get("stat") or p.get("label"), "type": "prop" if p.get("pid") else p.get("type"),
                             "tier": p.get("tier"), "tk": p.get("ticker")}
        for aid, pl in b["players"].items():
            g = next((x for x in b["games"] if x["id"] == pl["gameId"]), None)
            if g and S.parse_t(g["start"]) > now:
                pproj[f"{pl['gameId']}|{aid}"] = {"s": sport, "g": pl["gameId"], "pid": aid, "t": g["start"], "p": pl["proj"]}


def grade(track, now):
    pend, pproj = track.get("pending", {}), track.get("pendingProj", {})
    agg = track.setdefault("agg", {})
    daily = track.setdefault("daily", {})
    cal = track.setdefault("cal", {})
    bins = track.setdefault("bins", {})
    perr = track.setdefault("projErr", {})
    recent = track.setdefault("recent", [])
    cache, n = {}, 0

    def box(sport, gid):
        if gid not in cache:
            cache[gid] = S.box_summary(sport, gid)
        return cache[gid]
    def ticker(pid, e):
        if e["pf"] != "Kalshi":
            return None
        return e.get("tk") or (pid[2:] if pid.startswith("k-") else None)
    due = {pid: e for pid, e in pend.items() if S.parse_t(e["t"]) and now >= S.parse_t(e["t"]) + timedelta(hours=GRADE_AFTER_H[e["s"]])}
    try:
        kres = F.kalshi_results([ticker(pid, e) for pid, e in due.items()])
    except Exception:
        S.log("[grade] Kalshi results failed\n" + traceback.format_exc())
        kres = {}
    nk = nx = 0
    for pid, e in list(pend.items()):
        t = S.parse_t(e["t"])
        if not t or now < t + timedelta(hours=GRADE_AFTER_H[e["s"]]):
            continue
        tk = ticker(pid, e)
        kr = kres.get(tk) if tk else None
        b = box(e["s"], e["g"])
        if not b.get("done") and kr is None:
            if now > t + timedelta(hours=GIVE_UP_H):
                del pend[pid]
            continue
        if tk and kr is None and now < t + timedelta(hours=KALSHI_WAIT_H):
            continue  # ESPN has the final; give Kalshi time to settle so both can be checked
        del pend[pid]
        val = None
        res_e = None
        if not b.get("done"):
            pass
        elif e["type"] == "prop":
            st = next((tm[e["pid"]] for tm in b["teams"].values() if e["pid"] in tm), None)
            val = S.box_value(e["s"], e["k"], st) if st else "dnp"
        else:
            sc = b["score"]
            abbrs = list(sc.keys())
            if len(abbrs) == 2 and None not in sc.values():
                if e["type"] == "total":
                    val = sc[abbrs[0]] + sc[abbrs[1]]
                else:
                    mine = next((a for a in abbrs if e["team"] in S.abbr_set(e["s"], a)), None)
                    if mine:
                        other = abbrs[1] if mine == abbrs[0] else abbrs[0]
                        val = sc[mine] - sc[other]
        if b.get("done"):
            if val is None or val == "dnp":
                res_e = "void"
            else:
                if e["type"] == "ml":
                    yes = val > 0
                else:
                    yes = val > e["line"]
                hit_yes_side = e["side"] in ("YES", "MORE")
                if e["type"] != "ml" and val == e["line"]:
                    res_e = "push"
                else:
                    res_e = "w" if yes == hit_yes_side else "l"
        # Kalshi's settlement decides Kalshi picks; ESPN cross-checks it. A disagreement is flagged, not counted.
        src = "E"
        res = res_e
        if kr in ("yes", "no"):
            res_k = "w" if (kr == "yes") == (e["side"] == "YES") else "l"
            nk += 1
            if res_e in ("w", "l") and res_e != res_k:
                res, src = "disp", "X"
                nx += 1
            else:
                res, src = res_k, ("KE" if res_e in ("w", "l") else "K")
        elif kr == "void":
            res, src = "void", "K"
        if res is None:
            res = "void"
        profit = None
        if e["pf"] == "Kalshi" and e.get("price") and res in ("w", "l"):
            profit = (1 - e["price"]) / e["price"] if res == "w" else -1.0
        n += 1
        tag = "rec" if e["rec"] else "lean"
        for key in ("all", e["s"], e["pf"], f"{e['s']}|{e['pf']}", f"type|{e['type']}", f"stat|{e['s']}|{e['k']}", f"conf|{conf_bucket(e['prob'])}") + ((f"tier|{e['tier']}",) if e.get("tier") else ()):
            bump(agg, f"{tag}|{key}", res, profit)
        if res == "disp":
            pass
        elif res in ("w", "l"):
            day = (t - timedelta(hours=5)).date().isoformat()
            d = daily.setdefault(day, {"rec": [0, 0], "lean": [0, 0]})
            d[tag][0 if res == "w" else 1] += 1
            c = cal.setdefault(f"{e['s']}|{e['k']}", [0, 0.0, 0])
            raw = e.get("raw") if e.get("raw") is not None else e["prob"]
            rp = raw if e["side"] in ("YES", "MORE") else 1 - raw
            c[0] += 1
            c[1] += max(rp, 1 - rp)
            c[2] += 1 if ((rp >= 0.5) == (res == "w")) else 0
            bk = conf_bucket(e["prob"])
            bb = bins.setdefault(bk, [0, 0])
            bb[0] += 1
            bb[1] += 1 if res == "w" else 0
        recent.append([e["s"], e["pf"], e["nm"], e["stat"], e["line"], e["side"], e["prob"], val if not isinstance(val, str) else None, res,
                       e["t"][:10], e["rec"], e.get("price"), e["k"], e["type"], e.get("tier"), src])
    for key, e in list(pproj.items()):
        t = S.parse_t(e["t"])
        if not t or now < t + timedelta(hours=GRADE_AFTER_H[e["s"]]):
            continue
        b = box(e["s"], e["g"])
        if not b.get("done"):
            if now > t + timedelta(hours=GIVE_UP_H):
                del pproj[key]
            continue
        del pproj[key]
        st = next((tm[e["pid"]] for tm in b["teams"].values() if e["pid"] in tm), None)
        vals = S.box_value(e["s"], None, st) if st else None
        if not isinstance(vals, dict):
            continue
        for k, pv in e["p"].items():
            av = vals.get(k)
            if isinstance(av, (int, float)):
                r = perr.setdefault(f"{e['s']}|{k}", [0, 0.0, 0.0, 0])
                r[0] += 1
                r[1] = round(r[1] + abs(pv - av), 2)
                r[2] = round(r[2] + av, 2)
                r[3] += 1 if abs(pv - av) <= max(0.25 * max(av, pv), 1.0) else 0
    track["recent"] = [fill_graded(x) for x in recent[-5000:]]
    track["open"] = {k: v for k, v in (track.get("open") or {}).items() if S.parse_t(v[1]) and S.parse_t(v[1]) > now - timedelta(hours=6)}
    track["daily"] = dict(sorted(daily.items())[-60:])
    # learn: shrink or stretch probabilities per stat from how well-calibrated the leans were
    calib = {}
    for k, (cnt, sp, hits) in cal.items():
        if cnt >= 30 and sp / cnt - 0.5 > 0.01:
            kk = M.clamp((hits / cnt - 0.5) / (sp / cnt - 0.5), 0.25, 1.2)
            calib[k] = round((kk * cnt + M.DEFAULT_SHRINK * 40) / (cnt + 40), 3)
    track["calib"] = calib
    if n:
        track["gradedAt"] = now.isoformat()[:19] + "Z"
    S.log(f"[grade] graded {n} ({nk} by Kalshi settlement, {nx} disputed); pending {len(pend)} picks, {len(pproj)} projections")


def main():
    t0 = time.time()
    now = datetime.now(timezone.utc)
    track = S.load_json(f"{DATA}/track.json", {})
    geo = S.load_json(f"{DATA}/cache/geo.json", {})
    old_board = S.load_json(f"{DATA}/board.json", {})
    old_players = S.load_json(f"{DATA}/players.json", {})
    try:
        grade(track, now)
    except Exception:
        S.log("[grade] failed\n" + traceback.format_exc())
    board, players = {}, {}
    for sport in ("NFL", "NBA"):
        try:
            b = build_sport(sport, now, track, geo)
        except Exception:
            S.log(f"[{sport}] failed\n" + traceback.format_exc())
            b = None
        if b is None:  # keep last good data for upcoming games
            ob = (old_board.get("sports") or {}).get(sport)
            if ob and any(S.parse_t(g["start"]) and S.parse_t(g["start"]) > now - timedelta(hours=4) for g in ob.get("games", [])):
                board[sport] = ob
                players[sport] = old_players.get(sport, {})
                S.log(f"[{sport}] kept previous data")
            continue
        players[sport] = b.pop("details")
        board[sport] = b
    save_pending(track, {s: (dict(b, players=b["players"]) if b else None) for s, b in board.items()}, now)
    track["updated"] = now.isoformat()[:19] + "Z"
    S.save_json(f"{DATA}/track.json", track)
    S.save_json(f"{DATA}/players.json", players)
    S.save_json(f"{DATA}/cache/geo.json", geo)
    S.save_json(f"{DATA}/board.json", {"updated": now.isoformat()[:19] + "Z", "version": VERSION, "sports": board,
                                       "summary": summary(track), "log": S.LOG[-60:], "secs": round(time.time() - t0)})
    S.log(f"done in {time.time()-t0:.0f}s")


def summary(track):
    agg = track.get("agg", {})

    def pct(k):
        r = agg.get(k) or {}
        n = r.get("w", 0) + r.get("l", 0)
        return {"w": r.get("w", 0), "l": r.get("l", 0), "p": r.get("p", 0), "v": r.get("v", 0), "d": r.get("d", 0), "pct": round(r["w"] / n, 4) if n else None,
                "roi": round(r["pl"] / r["stake"], 4) if r.get("stake") else None}
    out = {"agg": {k: pct(k) for k in agg}, "daily": track.get("daily", {}), "bins": track.get("bins", {}),
           "projErr": track.get("projErr", {}), "recent": track.get("recent", [])[-250:], "calib": track.get("calib", {}),
           "pending": len(track.get("pending", {})), "since": track.get("since"), "gradedAt": track.get("gradedAt")}
    track.setdefault("since", datetime.now(timezone.utc).date().isoformat())
    out["since"] = track["since"]
    return out


if __name__ == "__main__":
    main()
