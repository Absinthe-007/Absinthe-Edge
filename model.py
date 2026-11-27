"""Projection model: player stat lines, game scores, and picks for Pick6 + Kalshi."""
import math
from datetime import timedelta

from sources import abbr_set, num, parse_t, box_value, miles, coords

NBA_STATS = ["min", "points", "rebounds", "assists", "threes", "steals", "blocks", "turnovers", "pra", "pts_reb", "pts_ast", "reb_ast", "stl_blk"]
NFL_STATS = {
    "QB": ["pass_yds", "pass_tds", "pass_cmp", "pass_att", "ints", "rush_yds", "rush_att", "pass_rush_yds"],
    "RB": ["rush_yds", "rush_att", "receptions", "targets", "rec_yds", "rush_rec_yds", "anytime_td"],
    "WR": ["receptions", "targets", "rec_yds", "long_rec", "rush_rec_yds", "anytime_td"],
    "TE": ["receptions", "targets", "rec_yds", "long_rec", "rush_rec_yds", "anytime_td"],
    "DEF": ["tackles", "sacks", "ints"],
    "K": ["fg_made", "kick_pts"],
}
LABEL = {"min": "MIN", "points": "PTS", "rebounds": "REB", "assists": "AST", "threes": "3PM", "steals": "STL", "blocks": "BLK",
         "turnovers": "TO", "pra": "PRA", "pts_reb": "P+R", "pts_ast": "P+A", "reb_ast": "R+A", "stl_blk": "S+B",
         "pass_yds": "Pass Yds", "pass_tds": "Pass TD", "pass_cmp": "Comp", "pass_att": "Att", "ints": "INT",
         "rush_yds": "Rush Yds", "rush_att": "Carries", "pass_rush_yds": "Pass+Rush Yds", "receptions": "Rec", "targets": "Tgts",
         "rec_yds": "Rec Yds", "rush_rec_yds": "Rush+Rec Yds", "anytime_td": "TDs", "long_rec": "Long Rec",
         "tackles": "Tackles", "sacks": "Sacks", "fg_made": "FG Made", "kick_pts": "Kick Pts"}
COUNT = {"anytime_td", "pass_tds", "ints", "threes", "steals", "blocks", "stl_blk", "turnovers", "sacks", "fg_made", "receptions", "targets"}
CV = {"pass_yds": 0.30, "rush_yds": 0.55, "rec_yds": 0.65, "rush_rec_yds": 0.50, "pass_rush_yds": 0.30, "long_rec": 0.55,
      "points": 0.32, "rebounds": 0.40, "assists": 0.45, "pra": 0.26, "pts_reb": 0.29, "pts_ast": 0.30, "reb_ast": 0.36,
      "min": 0.18, "pass_cmp": 0.25, "pass_att": 0.22, "rush_att": 0.40, "tackles": 0.45, "kick_pts": 0.5}
PASSING = {"pass_yds", "pass_tds", "pass_cmp", "pass_att", "ints", "pass_rush_yds"}
RECEIVING = {"rec_yds", "receptions", "targets", "long_rec"}
RUSHING = {"rush_yds", "rush_att"}
MODEL_V = "4.0"  # bump when the probability model changes: learned calibration restarts so old and new don't stack
DEFAULT_SHRINK = 0.78
MARGIN_SD = {"NFL": 12.8, "NBA": 12.5}
TOTAL_SD = {"NFL": 12.8, "NBA": 17.0}
HOME_ADV = {"NFL": 1.5, "NBA": 2.5}
# Walk-forward backtest (Nov 2026): the Vegas line beats our team model, so it carries most of the weight.
VEGAS_MODEL_W = {"NFL": {"margin": 0.10, "total": 0.10}, "NBA": {"margin": 0.15, "total": 0.25}}
RATING_K = {"NFL": 5, "NBA": 5}  # pseudo-games of shrinkage toward average in the opponent-adjusted ratings
# NBA stat spreads and skew, fit on 6,000+ replayed player-games: points/PRA were too tight, assists too wide,
# and right-skewed stats finish below their mean more often than a bell curve says.
SD_MULT = {"points": 1.2, "pra": 1.2, "pts_reb": 1.15, "pts_ast": 1.1, "rebounds": 1.0, "reb_ast": 1.0, "assists": 0.85, "min": 0.95}
MED_SHIFT = {"points": -0.10, "pra": -0.10, "pts_reb": -0.10, "pts_ast": -0.08, "rebounds": -0.10, "reb_ast": -0.08, "assists": -0.06}
NB_R = {"blocks": 5.0, "steals": 10.0}  # count stats that vary more than a Poisson allows
COMBO_PARTS = {"pra": ("points", "rebounds", "assists"), "pts_reb": ("points", "rebounds"), "pts_ast": ("points", "assists"),
               "reb_ast": ("rebounds", "assists"), "stl_blk": ("steals", "blocks")}  # NBA combo stats = sum of their parts
BIAS = {}  # "SPORT|stat" -> multiplier learned from our own graded projections (set by run.py)
GAME_SHRINK = {"NFL": 1.0, "NBA": 1.0}  # learned from graded game winners (set by run.py): pulls win chances toward 50/50 only when the data says we're overconfident
KALSHI_FEE_RATE = 0.07  # Kalshi taker fee is about 7% x price x (1 - price) per contract


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def pois_over(lam, line):
    k = math.floor(line)
    if lam <= 0:
        return 0.0
    cdf, term = 0.0, math.exp(-lam)
    for i in range(0, k + 1):
        if i > 0:
            term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1 - cdf))


def nb_over(mu, line, r):
    """P(X > line) for a count with mean mu and negative-binomial dispersion r (r -> infinity is Poisson)."""
    k = math.floor(line)
    if mu <= 0:
        return 0.0
    p = r / (r + mu)
    cdf, term = 0.0, p ** r
    for i in range(0, k + 1):
        if i > 0:
            term *= (i - 1 + r) / i * (1 - p)
        cdf += term
    return max(0.0, min(1.0, 1 - cdf))


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def role(sport, pos):
    if sport == "NBA":
        p = (pos or "").upper()
        return "C" if p.startswith("C") else "F" if "F" in p else "G"
    p = (pos or "").upper()
    if p == "QB":
        return "QB"
    if p in ("RB", "FB", "HB"):
        return "RB"
    if p == "WR":
        return "WR"
    if p == "TE":
        return "TE"
    if p in ("K", "PK"):
        return "K"
    return "DEF"


def stats_for(sport, grp):
    return NBA_STATS if sport == "NBA" else NFL_STATS.get(grp, NFL_STATS["WR"])


# ======================================================================================
# Defense vs position, from every final box score this season
# ======================================================================================
def build_dvp(sport, boxes, roster):
    allowed, games = {}, {}
    for eid, b in boxes.items():
        teams = list(b.get("teams", {}).keys())
        if len(teams) != 2:
            continue
        for ab in teams:
            opp = teams[1] if ab == teams[0] else teams[0]
            games[opp] = games.get(opp, 0) + 1
            for aid, st in b["teams"][ab].items():
                info = roster.get(aid)
                if not info:
                    continue
                grp = role(sport, info["pos"])
                vals = box_value(sport, None, st)
                if not isinstance(vals, dict):
                    continue
                bucket = allowed.setdefault(opp, {}).setdefault(grp, {})
                for k, v in vals.items():
                    if isinstance(v, (int, float)):
                        bucket[k] = bucket.get(k, 0) + v
    per = {t: {g: {k: v / games[t] for k, v in d.items()} for g, d in grps.items()} for t, grps in allowed.items() if games.get(t)}
    league = {}
    for t, grps in per.items():
        for g, d in grps.items():
            for k, v in d.items():
                league.setdefault(g, {}).setdefault(k, []).append(v)
    league = {g: {k: sum(v) / len(v) for k, v in d.items()} for g, d in league.items()}
    # rank defenses vs each position on their main stat (1 = allows the fewest)
    main = {"G": "points", "F": "points", "C": "points", "QB": "pass_yds", "RB": "rush_rec_yds", "WR": "rec_yds", "TE": "rec_yds"}
    ranks = {}
    for g, k in main.items():
        rows = sorted([(t, per[t].get(g, {}).get(k)) for t in per if per[t].get(g, {}).get(k) is not None], key=lambda x: x[1])
        for i, (t, v) in enumerate(rows):
            ranks.setdefault(t, {})[g] = [i + 1, len(rows), round(v, 1), main[g]]
    return {"per": per, "league": league, "games": games, "ranks": ranks}


# ======================================================================================
# Game context: rest, road trip, travel, weather
# ======================================================================================
def team_context(sport, sched, game, geo_cache):
    t0 = parse_t(game["start"])
    prev = [g for g in sched if parse_t(g["t"]) and parse_t(g["t"]) < t0 - timedelta(hours=6)]
    cur = next((g for g in sched if g["id"] == game["id"]), None)
    ctx = {"rest": None, "road": 0, "travel": 0, "b2b": False}
    if prev:
        last = prev[-1]
        d0 = (t0 - timedelta(hours=5)).date()
        d1 = (parse_t(last["t"]) - timedelta(hours=5)).date()
        ctx["rest"] = (d0 - d1).days
        ctx["b2b"] = ctx["rest"] <= 1
        here = coords((cur or {}).get("city") or game["venue"].get("city"), geo_cache)
        ctx["travel"] = round(miles(coords(last.get("city"), geo_cache), here))
    if cur and not cur["home"]:
        n = 1
        for g in reversed(prev):
            if g["home"]:
                break
            n += 1
        ctx["road"] = n
    return ctx


# ======================================================================================
# Player projection
# ======================================================================================
def project_player(sport, pl, games, game, is_home, opp, dvp, ctx, wx, stand, implied, extra=None):
    grp = role(sport, pl["pos"])
    keys = stats_for(sport, grp)
    if not games:
        return grp, {}
    season = games[0]["season"]
    cur = [g for g in games if g["season"] == season]
    prev = [g for g in games if g["season"] == season - 1]
    l5 = games[:5]
    loc = [g for g in games if g["season"] >= season - 1 and g["home"] == is_home][:16]
    vs = [g for g in games if g["opp"] in abbr_set(sport, opp)][:10]
    out = {}
    for k in keys:
        def vals(gs):
            return [g["s"][k] for g in gs if isinstance(g["s"].get(k), (int, float))]
        vc, vp = vals(cur), vals(prev)
        if not vc and not vp:
            continue
        if len(vc) >= 4 or not vp:
            s_avg = mean(vc)
        else:  # early season: lean on last season
            s_avg = (sum(vc) + mean(vp) * (4 - len(vc))) / 4
        parts = {"season": s_avg, "l5": mean(vals(l5)), "loc": mean(vals(loc)), "vs": mean(vals(vs))}
        w = {"season": 0.40, "l5": 0.30, "loc": 0.15, "vs": 0.15 * min(len(vals(vs)), 4) / 4}
        tot = sum(w[x] for x in w if parts[x] is not None)
        base = sum(parts[x] * w[x] for x in w if parts[x] is not None) / tot if tot else s_avg
        factors = []

        def add(name, m, note):
            if abs(m - 1) >= 0.005:
                factors.append([name, round(m, 3), note])

        # defense vs position
        if k != "min" and grp not in ("DEF", "K"):
            od = dvp["per"].get(opp) or {}
            lg = dvp["league"].get(grp, {}).get(k)
            al = od.get(grp, {}).get(k)
            ng = dvp["games"].get(opp, 0)
            if lg and al is not None and lg > 0.3 and ng >= 3:
                ratio = al / lg
                wt = (0.4 if sport == "NFL" else 0.3) * min(ng, 8) / 8
                cap = 0.12 if sport == "NFL" else 0.08
                rk = (dvp["ranks"].get(opp) or {}).get(grp)
                add("Defense vs " + grp, clamp(1 + wt * (ratio - 1), 1 - cap, 1 + cap), f"{opp} allows {al:.1f}/g to {grp}s (lg {lg:.1f})" + (f", rank {rk[0]}/{rk[1]}" if rk else ""))
        # game script: Vegas implied team total vs normal scoring
        if implied and grp not in ("DEF",) and k != "min":
            pf = (stand.get(pl["team"]) or {}).get("pf")
            if pf:
                wt = 0.6 if sport == "NFL" else 0.4
                add("Implied team total", clamp(1 + wt * (implied / pf - 1), 0.9, 1.1), f"Vegas implies {implied:.1f} pts vs {pf:.1f} avg")
        # weather (NFL, open-air only)
        if sport == "NFL" and wx and not game["venue"]["indoor"]:
            wind, rain, temp = wx.get("wind") or 0, (wx.get("rainPct") or 0) >= 60 and (wx.get("precip") or 0) >= 0.03, wx.get("temp")
            if k in PASSING or k in RECEIVING:
                m = 0.88 if wind >= 20 else 0.93 if wind >= 15 else 0.97 if wind >= 12 else 1.0
                if rain:
                    m *= 0.95
                if temp is not None and temp <= 25:
                    m *= 0.96
                add("Weather", m, f"{wx.get('temp')}°F, wind {wind} mph, rain {wx.get('rainPct')}%")
            elif k in RUSHING:
                add("Weather", 1.03 if (wind >= 15 or rain) else 1.0, f"Bad weather favors the run")
            elif k in ("fg_made", "kick_pts"):
                add("Weather", 0.9 if wind >= 15 else 1.0, f"Wind {wind} mph")
        # rest / road trip / travel
        if ctx:
            if sport == "NBA":
                if ctx.get("b2b"):
                    # replayed 195 back-to-back games: players finished at 1.03x their projection, not 0.96x
                    add("Back-to-back", 0.995, "2nd night of a back-to-back")
                if ctx.get("road", 0) >= 3:
                    add("Road trip", 0.98, f"Road game #{ctx['road']} in a row")
                if ctx.get("travel", 0) >= 1500:
                    add("Travel", 0.985, f"{ctx['travel']} miles since last game")
            else:
                if ctx.get("rest") is not None and ctx["rest"] <= 5:
                    add("Short week", 0.98, f"{ctx['rest']} days rest")
                elif (ctx.get("rest") or 0) >= 10:
                    add("Off a bye", 1.02, f"{ctx['rest']} days rest")
                if ctx.get("travel", 0) >= 1800:
                    add("Travel", 0.98, f"{ctx['travel']} miles since last game")
                if ctx.get("road", 0) >= 2:
                    add("Road trip", 0.99, f"Road game #{ctx['road']} in a row")
        if extra:
            for f in extra(k, grp, base):
                add(*f)
        mult = 1.0
        for f in factors:
            mult *= f[1]
        proj = base * mult
        comp = [out[c] for c in COMBO_PARTS[k]] if sport == "NBA" and k in COMBO_PARTS and all(c in out for c in COMBO_PARTS[k]) else None
        if comp:
            # A combo is exactly the sum of its parts. Projecting it on its own stacked every teammate-out boost on the whole total
            # (PRA got +10% and +8% on all three stats: 60.7 instead of 55.8), so build it from the parts' own adjusted projections.
            # The factors shown are each part's factor weighted by that part's share of the baseline.
            base = sum(c["base"] for c in comp)
            proj = sum(c["proj"] for c in comp)
            shift = {}
            for c in comp:
                share = c["base"] / base if base else 0
                for name, m, note in c["factors"]:
                    e = shift.setdefault(name, [0.0, note])
                    e[0] += share * (m - 1)
            factors = [[n, round(1 + d, 3), note] for n, (d, note) in shift.items() if abs(d) >= 0.005]
        else:
            bm = BIAS.get(f"{sport}|{k}")
            if bm:
                proj *= bm
        sample = vals(cur)[:12] + vals(prev)[:6]
        sd_base = max(CV.get(k, 0.5) * max(proj, 1), 1.0 if k in COUNT else 2.5)
        if len(sample) >= 5:
            m_ = mean(sample)
            sd_s = math.sqrt(sum((x - m_) ** 2 for x in sample) / (len(sample) - 1))
            sd = math.sqrt(0.5 * sd_s ** 2 + 0.5 * sd_base ** 2)
        else:
            sd = sd_base * 1.15
        if sport == "NBA":
            sd *= SD_MULT.get(k, 1.0)
        out[k] = {"proj": round(proj, 2), "base": round(base, 2), "sd": round(sd, 2), "n": len(vc),
                  "parts": {x: (round(v, 1) if v is not None else None) for x, v in parts.items()},
                  "nvs": len(vals(vs)), "nloc": len(vals(loc)), "factors": factors,
                  "l10": vals(games[:10])}
    return grp, out


def p_over(k, proj, sd, line, sport="NBA"):
    if k in COUNT:
        if sport == "NBA" and k in NB_R:
            return nb_over(max(proj, 0.06), line, NB_R[k])
        return pois_over(max(proj, 0.06), line)
    shift = MED_SHIFT.get(k, 0.0) if sport == "NBA" else 0.0
    return phi((proj + shift * sd - line) / max(sd, 0.5))


def kalshi_fee(price):
    return KALSHI_FEE_RATE * price * (1 - price)


def game_p(sport, p):
    """Win/cover chance after the learned per-sport shrink toward 50/50."""
    return clamp(0.5 + (p - 0.5) * GAME_SHRINK.get(sport, 1.0), 0.02, 0.98)


def calibrate(p, calib, sport, k):
    s = (calib or {}).get(f"{sport}|{k}", (calib or {}).get(sport, DEFAULT_SHRINK))
    return clamp(0.5 + (p - 0.5) * s, 0.02, 0.98)


# ======================================================================================
# Game projection (winner / spread / total)
# ======================================================================================
def build_ratings(sport, results):
    """Opponent-adjusted net ratings (points above an average team) from every final this season.
    Replayed on 100+ games per sport this beat raw points-for/against: NBA winner accuracy 66% -> 72%."""
    gp, R, ha_sum, ha_n = {}, {}, 0.0, 0
    for r in results:
        if r.get("hs") is None or r.get("as") is None:
            continue
        for t in (r["home"], r["away"]):
            gp[t] = gp.get(t, 0) + 1
        if not r.get("neutral"):
            ha_sum += r["hs"] - r["as"]
            ha_n += 1
    if len(results) < 20:
        return None
    # home edge: this season's measured home margin, anchored to the long-run value
    prior_ha, prior_n = HOME_ADV[sport], 300 if sport == "NBA" else 150
    ha = (ha_sum + prior_ha * prior_n) / (ha_n + prior_n)
    K = RATING_K[sport]
    R = {t: 0.0 for t in gp}
    for _ in range(25):
        acc = {t: 0.0 for t in gp}
        for r in results:
            if r.get("hs") is None or r.get("as") is None:
                continue
            m = r["hs"] - r["as"] - (0 if r.get("neutral") else ha)
            acc[r["home"]] += m + R[r["away"]]
            acc[r["away"]] += -m + R[r["home"]]
        R = {t: acc[t] / (gp[t] + K) for t in gp}
    return {"R": R, "gp": gp, "ha": round(ha, 2)}


def project_game(g, stand, ctx_h, ctx_a, wx, vegas_m, ratings=None):
    sp = g["sport"]
    h, a = stand.get(g["home"]["abbr"]) or {}, stand.get(g["away"]["abbr"]) or {}
    vals = [t["pf"] for t in stand.values() if t.get("pf")]
    lg = sum(vals) / len(vals) if vals else (22.5 if sp == "NFL" else 114)
    k = 4 if sp == "NFL" else 10

    def reg(t, key):
        v, gp = t.get(key), t.get("gp") or 0
        return lg if v is None else (v * gp + lg * k) / (gp + k)
    he = (reg(h, "pf") + reg(a, "pa")) / 2
    ae = (reg(a, "pf") + reg(h, "pa")) / 2
    notes = []
    ha = HOME_ADV[sp]
    rt = None
    if ratings and g["home"]["abbr"] in ratings["R"] and g["away"]["abbr"] in ratings["R"] \
            and ratings["gp"].get(g["home"]["abbr"], 0) >= 4 and ratings["gp"].get(g["away"]["abbr"], 0) >= 4:
        rt = ratings
        ha = ratings["ha"]
    base_margin = he - ae
    if not g.get("neutral"):
        he += ha / 2
        ae -= ha / 2
        notes.append(f"Home edge +{ha:.1f}")
    ctx_m = 0.0  # rest/travel/weather nudges, kept apart so they ride on top of the ratings margin
    for side, ctx, nm in (("h", ctx_h, g["home"]["abbr"]), ("a", ctx_a, g["away"]["abbr"])):
        adj = 0
        if ctx:
            if sp == "NBA" and ctx.get("b2b"):
                adj -= 0.75
                notes.append(f"{nm} on a back-to-back")
            if sp == "NBA" and ctx.get("road", 0) >= 4:
                adj -= 0.5
                notes.append(f"{nm} road game #{ctx['road']}")
            if sp == "NFL" and (ctx.get("rest") or 0) >= 10:
                adj += 1.0
                notes.append(f"{nm} off a bye")
            if sp == "NFL" and ctx.get("rest") is not None and ctx["rest"] <= 5:
                adj -= 0.5
        if side == "h":
            he += adj
            ctx_m += adj
        else:
            ae += adj
            ctx_m -= adj
    if sp == "NFL" and wx and not g["venue"]["indoor"]:
        if (wx.get("wind") or 0) >= 15:
            he -= 1.5
            ae -= 1.5
            notes.append(f"Wind {wx['wind']} mph lowers scoring")
        if (wx.get("rainPct") or 0) >= 60 and (wx.get("precip") or 0) >= 0.03:
            he -= 1
            ae -= 1
            notes.append("Rain expected")
    margin, total = he - ae, he + ae
    if rt:  # opponent-adjusted ratings replace the raw points-for/against margin
        margin = rt["R"][g["home"]["abbr"]] - rt["R"][g["away"]["abbr"]] + (0 if g.get("neutral") else ha) + ctx_m
    m_model, t_model = margin, total
    w = VEGAS_MODEL_W[sp]
    if vegas_m is not None:
        margin = w["margin"] * margin + (1 - w["margin"]) * vegas_m
    if g.get("ou"):
        total = w["total"] * total + (1 - w["total"]) * g["ou"]
    hp, ap = (total + margin) / 2, (total - margin) / 2
    return {"homePts": round(hp, 1), "awayPts": round(ap, 1), "margin": round(margin, 1), "total": round(total, 1),
            "pHome": round(game_p(sp, phi(margin / MARGIN_SD[sp])), 3), "pHomeRaw": round(phi(margin / MARGIN_SD[sp]), 3), "modelMargin": round(m_model, 1), "modelTotal": round(t_model, 1),
            "vegasMargin": vegas_m, "notes": notes, "rated": bool(rt)}


def implied_totals(g, gp):
    return {g["home"]["abbr"]: gp["homePts"], g["away"]["abbr"]: gp["awayPts"]}


def kalshi_game_prob(m, g, gp):
    sp = g["sport"]
    home_set, away_set = abbr_set(sp, g["home"]["abbr"]), abbr_set(sp, g["away"]["abbr"])
    if m["key"] == "total":
        return phi((gp["total"] - m["line"]) / TOTAL_SD[sp]), f"Total over {m['line']}"
    is_home = m["team"] in home_set
    if not is_home and m["team"] not in away_set:
        return None, None
    my_margin = gp["margin"] if is_home else -gp["margin"]
    name = g["home"]["short"] if is_home else g["away"]["short"]
    if m["key"] == "ml":
        return game_p(sp, phi(my_margin / MARGIN_SD[sp])), f"{name} to win"
    return game_p(sp, phi((my_margin - m["line"]) / MARGIN_SD[sp])), f"{name} wins by {m['line']}+"


def market_mid(m):
    yb, ya = m.get("yesBid") or 0.0, m["yesAsk"]
    return (yb + ya) / 2 if yb > 0 else ya / 2


MODEL_W = 0.2  # weight on our model vs the market price when a market exists (graded Kalshi picks: the market alone beat the model; 10-20% model was best)


def blend(p_model, mid, w=MODEL_W):
    return clamp(w * p_model + (1 - w) * mid, 0.01, 0.99)


def kalshi_side(p, m, min_edge=0.03):
    ey, en = p - m["yesAsk"], (1 - p) - m["noAsk"]
    if ey >= en:
        side, price, prob, edge = "YES", m["yesAsk"], p, ey
    else:
        side, price, prob, edge = "NO", m["noAsk"], 1 - p, en
    fee = kalshi_fee(price)
    # the edge has to survive Kalshi's fee: graded Kalshi picks were down ~6% ROI, about what fees alone cost at coin-flip prices
    rec = (edge - fee) >= min_edge and 0.06 <= price <= 0.94
    return {"side": side, "price": round(price, 2), "prob": round(prob, 3), "edge": round(edge, 3), "fee": round(fee, 3), "rec": rec,
            "ev": round((prob - fee) / price - 1, 3) if price else None}
