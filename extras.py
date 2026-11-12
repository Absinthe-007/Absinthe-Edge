"""V1.1 context: season usage from box scores, teammate + opponent injuries, player rest/load,
usage trends, NBA pace, streaks, matchup grades and confidence tiers."""
from datetime import timedelta

import model as M
from sources import box_value, parse_t

OUT = ("out", "doubtful", "injured reserve", "suspension")
NBA_SCORING = {"points", "assists", "pra", "pts_ast", "pts_reb", "threes"}
NBA_REB = {"rebounds", "reb_ast"}
REC_KEYS = {"receptions", "targets", "rec_yds", "long_rec", "rush_rec_yds"}
RUSH_KEYS = {"rush_yds", "rush_att"}
PASS_KEYS = {"pass_yds", "pass_tds", "pass_cmp", "pass_att", "pass_rush_yds"}


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


# ======================================================================================
# Season usage table from every final box score
# ======================================================================================
def build_usage(sport, boxes):
    players, teams = {}, {}
    for eid, b in boxes.items():
        date = b.get("date") or ""
        for ab, pl in (b.get("teams") or {}).items():
            tot = {}
            for aid, st in pl.items():
                v = box_value(sport, None, st)
                if not isinstance(v, dict):
                    continue
                if sport == "NBA" and not v.get("min"):
                    continue
                players.setdefault(aid, {"name": st.get("n"), "g": []})["g"].append((date, ab, v))
                for k, x in v.items():
                    if isinstance(x, (int, float)):
                        tot[k] = tot.get(k, 0) + x
            if tot:
                if sport == "NBA":
                    tot["poss"] = (tot.get("fga", 0) - tot.get("oreb", 0) + tot.get("turnovers", 0) + 0.44 * tot.get("fta", 0))
                teams.setdefault(ab, []).append((date, eid, tot))
    for ab in teams:
        teams[ab].sort(key=lambda x: x[0])
    for p in players.values():
        p["g"].sort(key=lambda x: x[0])
    # pace
    pace = {}
    if sport == "NBA":
        for ab, gs in teams.items():
            pace[ab] = mean([t["poss"] for _, _, t in gs])
        vals = sorted([(v, k) for k, v in pace.items() if v], reverse=True)
        lg = mean([v for v, _ in vals])
        pace = {k: {"pace": round(v, 1), "rank": i + 1, "of": len(vals)} for i, (v, k) in enumerate(vals)}
        pace["_lg"] = lg
    return {"players": players, "teams": teams, "pace": pace}


def pavg(usage, aid, k, team=None):
    p = usage["players"].get(aid)
    if not p:
        return None
    return mean([v.get(k) for _, ab, v in p["g"] if team is None or ab == team])


def team_avg(usage, team, k, last=None):
    gs = usage["teams"].get(team) or []
    if last:
        gs = gs[-last:]
    return mean([t.get(k) for _, _, t in gs])


def played_recently(usage, aid, team, n=3):
    """Did this player appear in any of the team's last n games?"""
    gs = (usage["teams"].get(team) or [])[-n:]
    dates = {d for d, _, _ in gs}
    p = usage["players"].get(aid)
    return bool(p) and any(d in dates and ab == team for d, ab, _ in p["g"])


# ======================================================================================
# Injuries: teammates out (usage boost) and opposing defenders out (easier matchup)
# ======================================================================================
def injury_context(sport, team, opp, roster, usage):
    def out_list(ab):
        res = []
        for p in roster.values():
            if p["team"] == ab and (p.get("inj") or "").lower() in OUT and played_recently(usage, p["id"], ab):
                res.append(p)
        return res
    mates, foes = out_list(team), out_list(opp)
    ctx = {"mates": [], "foes": [], "boost": {}, "oppBoost": {}}
    if sport == "NBA":
        tp, tr = team_avg(usage, team, "points"), team_avg(usage, team, "rebounds")
        vp = sum(pavg(usage, p["id"], "points", team) or 0 for p in mates)
        vr = sum(pavg(usage, p["id"], "rebounds", team) or 0 for p in mates)
        if tp and vp > 3:
            ctx["boost"]["scoring"] = min(0.10, 0.45 * vp / tp)
        if tr and vr > 2:
            ctx["boost"]["reb"] = min(0.10, 0.45 * vr / tr)
        od = team_avg(usage, opp, "blocks") or 0
        os_ = team_avg(usage, opp, "steals") or 0
        vd = sum((pavg(usage, p["id"], "blocks", opp) or 0) + (pavg(usage, p["id"], "steals", opp) or 0) for p in foes)
        if od + os_ and vd >= 1.0:
            ctx["oppBoost"]["all"] = min(0.04, 0.15 * vd / (od + os_))
        ctx["mates"] = sorted([[p["name"], p["pos"], round(pavg(usage, p["id"], "points", team) or 0, 1), "PTS"] for p in mates], key=lambda x: -x[2])
        ctx["foes"] = sorted([[p["name"], p["pos"], round(pavg(usage, p["id"], "points", opp) or 0, 1), "PTS"] for p in foes], key=lambda x: -x[2])
    else:
        tt, tc = team_avg(usage, team, "targets"), team_avg(usage, team, "rush_att")
        # the starting QB = the team's leader in pass attempts over its last 3 games
        recent = (usage["teams"].get(team) or [])[-3:]
        d3 = {d for d, _, _ in recent}
        qbs = {}
        for aid_, pu in usage["players"].items():
            for d, ab, v in pu["g"]:
                if ab == team and d in d3 and (v.get("pass_att") or 0) > 0:
                    qbs[aid_] = qbs.get(aid_, 0) + v["pass_att"]
        starter = max(qbs, key=qbs.get) if qbs else None
        qb_out = [p for p in mates if p["id"] == starter]
        vt = sum(pavg(usage, p["id"], "targets", team) or 0 for p in mates if p["pos"] in ("WR", "TE", "RB"))
        vc = sum(pavg(usage, p["id"], "rush_att", team) or 0 for p in mates if p["pos"] in ("RB", "FB"))
        if tt and vt >= 3:
            ctx["boost"]["rec"] = min(0.12, 0.5 * vt / tt)
        if tc and vc >= 4:
            ctx["boost"]["rush"] = min(0.15, 0.6 * vc / tc)
        if qb_out:
            ctx["boost"]["qbOut"] = True
        tpd, tsk = team_avg(usage, opp, "pd") or 0, team_avg(usage, opp, "sacks") or 0
        vpd = sum(pavg(usage, p["id"], "pd", opp) or 0 for p in foes if p["pos"] in ("CB", "S", "DB", "FS", "SS", "LB"))
        vsk = sum(pavg(usage, p["id"], "sacks", opp) or 0 for p in foes)
        if tpd and vpd >= 0.3:
            ctx["oppBoost"]["rec"] = min(0.05, 0.2 * vpd / tpd)
        if tsk and vsk >= 0.3:
            ctx["oppBoost"]["pass"] = min(0.03, 0.12 * vsk / tsk)
        def use(p, ab):
            if p["pos"] == "QB":
                return round(pavg(usage, p["id"], "pass_att", ab) or 0, 1), "ATT"
            if p["pos"] in ("RB", "FB"):
                return round((pavg(usage, p["id"], "rush_att", ab) or 0) + (pavg(usage, p["id"], "targets", ab) or 0), 1), "TCH"
            if p["pos"] in ("WR", "TE"):
                return round(pavg(usage, p["id"], "targets", ab) or 0, 1), "TGT"
            if p["pos"] in ("K", "PK", "P", "LS"):
                return 0, ""
            return round(pavg(usage, p["id"], "tackles", ab) or 0, 1), "TKL"
        ctx["mates"] = sorted([[p["name"], p["pos"], *use(p, team)] for p in mates], key=lambda x: -x[2])
        ctx["foes"] = sorted([[p["name"], p["pos"], *use(p, opp)] for p in foes], key=lambda x: -x[2])
    return ctx


# ======================================================================================
# Player rest / load and usage trend (from his own game log + team schedule)
# ======================================================================================
def player_rest(sport, gl, game, sched, usage, aid, team):
    t0 = parse_t(game["start"])
    gday = (t0 - timedelta(hours=5)).date()
    out = {}
    if not gl:
        return out
    last = gl[0]
    try:
        from datetime import date
        ld = date.fromisoformat(last["date"])
        out["days"] = (gday - ld).days
    except Exception:
        ld = None
    # team games he missed since his last appearance (completed games only)
    if ld:
        missed = 0
        for g in sched:
            gt = parse_t(g["t"])
            if g.get("done") and gt and (gt - timedelta(hours=5)).date() > ld and gt < t0 - timedelta(hours=6):
                missed += 1
        out["missed"] = missed
    cur = [g for g in gl if g["season"] == gl[0]["season"]]
    if sport == "NBA":
        mins = [g["s"].get("min") for g in cur if isinstance(g["s"].get("min"), (int, float))]
        if mins:
            out["minLast"] = mins[0]
            out["minAvg"] = round(mean(mins), 1)
            out["minL5"] = round(mean(mins[:5]), 1)
        fga = [g["s"].get("fga") for g in cur if isinstance(g["s"].get("fga"), (int, float))]
        if fga:
            out["fgaL5"], out["fgaAvg"] = round(mean(fga[:5]), 1), round(mean(fga), 1)
    else:
        tgt = [g["s"].get("targets") or 0 for g in cur]
        car = [g["s"].get("rush_att") or 0 for g in cur]
        touches = [a + b for a, b in zip(tgt, car)]
        if touches:
            out["touchLast"], out["touchAvg"] = touches[0], round(mean(touches), 1)
            out["tgtL5"], out["tgtAvg"] = round(mean(tgt[:5]), 1), round(mean(tgt), 1)
            out["carL5"], out["carAvg"] = round(mean(car[:5]), 1), round(mean(car), 1)
        # target share from box scores: season vs last 3 team games
        p = usage["players"].get(aid)
        if p:
            tg = usage["teams"].get(team) or []
            tot_s = sum(t.get("targets", 0) for d, _, t in tg if any(d == x[0] for x in p["g"]))
            mine = [v.get("targets", 0) for _, ab, v in p["g"] if ab == team]
            if tot_s and mine:
                out["tgtShare"] = round(sum(mine) / tot_s, 3)
            last3 = tg[-3:]
            d3 = {d for d, _, _ in last3}
            t3 = sum(t.get("targets", 0) for _, _, t in last3)
            m3 = sum(v.get("targets", 0) for d, ab, v in p["g"] if d in d3 and ab == team)
            if t3:
                out["tgtShareL3"] = round(m3 / t3, 3)
    tags = []
    days, missed = out.get("days"), out.get("missed", 0)
    if missed and missed >= 2:
        tags.append(["Returning", f"Missed {missed} straight games", "r"])
    elif missed == 1:
        tags.append(["Missed last game", "Sat out the team's last game", ""])
    if sport == "NBA":
        if days is not None and days <= 1 and not missed:
            tags.append(["Back-to-back", "Played yesterday", "r"])
        elif days is not None and days >= 3 and not missed:
            tags.append(["Fresh", f"{days} days since his last game", "g"])
        if out.get("minLast") and out.get("minAvg") and out["minLast"] >= out["minAvg"] + 6:
            tags.append(["Heavy load", f"{out['minLast']:.0f} min last game vs {out['minAvg']} avg", "r"])
        if out.get("minL5") and out.get("minAvg") and abs(out["minL5"] - out["minAvg"]) >= 3:
            up = out["minL5"] > out["minAvg"]
            tags.append(["Minutes " + ("up" if up else "down"), f"{out['minL5']} min last 5 vs {out['minAvg']} season", "g" if up else "r"])
    else:
        if days is not None and days >= 12 and not missed:
            tags.append(["Fresh", f"{days} days since his last game", "g"])
        if out.get("tgtShareL3") is not None and out.get("tgtShare") and abs(out["tgtShareL3"] - out["tgtShare"]) >= 0.05:
            up = out["tgtShareL3"] > out["tgtShare"]
            tags.append(["Targets " + ("up" if up else "down"), f"{out['tgtShareL3']*100:.0f}% target share last 3 vs {out['tgtShare']*100:.0f}% season", "g" if up else "r"])
        if out.get("carL5") is not None and out.get("carAvg") and out["carAvg"] >= 4 and abs(out["carL5"] - out["carAvg"]) >= 3:
            up = out["carL5"] > out["carAvg"]
            tags.append(["Carries " + ("up" if up else "down"), f"{out['carL5']} carries last 5 vs {out['carAvg']} season", "g" if up else "r"])
    out["tags"] = tags
    return out


def extra_factors(sport, rest, inj, pace_mult, game_ctx_b2b):
    """Returns f(k, grp) -> list of [name, mult, note] for model.project_player."""
    def f(k, grp, base=None):
        res = []
        if k == "min" and sport == "NBA":
            if rest.get("missed", 0) >= 2:
                res.append(["Returning from absence", 0.93, f"Missed {rest['missed']} games, minutes may be limited"])
            return res
        if rest.get("missed", 0) >= 2:
            res.append(["Returning from absence", 0.96, f"Missed {rest['missed']} straight games"])
        if sport == "NBA" and game_ctx_b2b and rest.get("minLast") and rest.get("minAvg") and rest["minLast"] >= rest["minAvg"] + 6:
            res.append(["Heavy load on a back-to-back", 0.98, f"{rest['minLast']:.0f} min last night"])
        b, ob = inj.get("boost", {}), inj.get("oppBoost", {})
        names = ", ".join(m[0] for m in inj.get("mates", [])[:3])
        foes = ", ".join(m[0] for m in inj.get("foes", [])[:3])
        if sport == "NBA":
            if b.get("scoring") and k in NBA_SCORING | {"turnovers"}:
                res.append(["Teammates out", 1 + b["scoring"], f"{names} out: more shots to go around"])
            if b.get("reb") and k in NBA_REB | {"pra", "pts_reb"}:
                res.append(["Teammates out (boards)", 1 + b["reb"] * 0.8, f"{names} out"])
            if ob.get("all") and k in NBA_SCORING | NBA_REB:
                res.append(["Opposing defenders out", 1 + ob["all"], f"{foes} out for the opponent"])
            if pace_mult and abs(pace_mult - 1) >= 0.005 and k not in ("min",):
                res.append(["Pace", pace_mult, "Projected possessions vs league average"])
        else:
            if b.get("qbOut") and (k in REC_KEYS or k in PASS_KEYS):
                res.append(["Starting QB out", 0.92, f"{names} out, backup QB"])
            if b.get("rec") and k in REC_KEYS and grp in ("WR", "TE", "RB"):
                res.append(["Teammates out (targets)", 1 + b["rec"], f"{names} out: open targets"])
            if b.get("rush") and k in RUSH_KEYS | {"rush_rec_yds"} and grp == "RB":
                res.append(["Teammates out (carries)", 1 + b["rush"], f"{names} out: open carries"])
            if ob.get("rec") and k in REC_KEYS:
                res.append(["Opposing DBs out", 1 + ob["rec"], f"{foes} out for the defense"])
            if ob.get("pass") and k in PASS_KEYS:
                res.append(["Opposing pass rush out", 1 + ob["pass"], f"{foes} out for the defense"])
        return res
    return f


def game_pace(usage, home, away):
    pc = usage.get("pace") or {}
    lg = pc.get("_lg")
    h, a = (pc.get(home) or {}).get("pace"), (pc.get(away) or {}).get("pace")
    if not lg or not h or not a:
        return None, None
    gp = (h + a) / 2
    return round(gp, 1), M.clamp(1 + 0.35 * (gp / lg - 1), 0.96, 1.04)


# ======================================================================================
# Pick enrichments
# ======================================================================================
def streak(vals, line):
    """vals most-recent first -> +n (over n straight) / -n (under n straight)."""
    n, side = 0, None
    for v in vals:
        if v == line:
            break
        s = v > line
        if side is None:
            side = s
        if s != side:
            break
        n += 1
    return 0 if side is None else (n if side else -n)


def grade(mult):
    for g, lo in (("A", 1.07), ("B", 1.025), ("C", 0.975), ("D", 0.93)):
        if mult >= lo:
            return g
    return "F"


def tier(p):
    if not p.get("rec"):
        return None
    if p["platform"] == "Pick6":
        return "Lock" if p["prob"] >= 0.68 else "Strong" if p["prob"] >= 0.62 else "Lean"
    return "Lock" if p["edge"] >= 0.10 else "Strong" if p["edge"] >= 0.07 else "Lean"
