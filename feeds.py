"""Absinthe Edge V3.0: extra data feeds.

- nflverse (free, no key): snap counts, target / air-yards share, red-zone looks (play-by-play),
  Next Gen Stats, and the official NFL injury report with Wed-Fri practice status.
- ESPN depth charts (NFL).
- The official NBA injury report (the league's PDF, refreshed every 15 minutes).
- Kalshi settlements, used to grade Kalshi picks the way Kalshi actually paid them.
- The Odds API (optional, needs the ODDS_API_KEY secret): player-prop lines from many sportsbooks,
  boiled down to one consensus line + de-vigged chance per player and stat.

Every feed fails soft: if a source is down the board still builds without it.
"""
import csv
import gzip
import io
import math
import os
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

import sources as S

CACHE = "data/cache"
NFLV = "https://github.com/nflverse/nflverse-data/releases/download"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
NBA_INJ = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{d}_{t}.pdf"
ODDS = "https://api.the-odds-api.com/v4"


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _f(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ======================================================================================
# Cached downloads
# ======================================================================================
def _fetch(url, name, max_age_h):
    """Download url into data/cache/feeds/name unless a fresh copy is there. Returns bytes or None."""
    path = f"{CACHE}/feeds/{name}"
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600:
            with open(path, "rb") as fh:
                return fh.read()
    except Exception:
        pass
    data = None
    for i in range(3):
        try:
            r = S.SESSION.get(url, timeout=60)
            if r.status_code == 200 and r.content:
                data = r.content
                break
            if r.status_code in (403, 404):
                break
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    if data:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return data
    if os.path.exists(path):  # stale copy beats nothing
        with open(path, "rb") as fh:
            return fh.read()
    return None


def _csv(data, gz=False):
    if not data:
        return []
    raw = gzip.decompress(data) if gz else data
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))


# ======================================================================================
# NFL: nflverse + ESPN depth charts
# ======================================================================================
def nfl_season(now):
    return now.year if now.month >= 7 else now.year - 1


def nfl_profiles(now, roster):
    """espn_id -> profile dict (snaps, shares, red zone, NGS, injury report, depth rank)."""
    yr = nfl_season(now)
    t0 = time.time()
    players = _csv(_fetch(f"{NFLV}/players/players.csv", "nflv_players.csv", 24))
    g2e, p2e = {}, {}
    for p in players:
        e = (p.get("espn_id") or "").strip()
        if e:
            e = e.split(".")[0]
            if p.get("gsis_id"):
                g2e[p["gsis_id"]] = e
            if p.get("pfr_id"):
                p2e[p["pfr_id"]] = e
    if not g2e:
        S.log("[nflverse] player id map unavailable; skipping")
        return {}, {}
    prof = {}

    def P(e):
        return prof.setdefault(e, {})

    # ---- snap counts (offense %), per game
    snaps = _csv(_fetch(f"{NFLV}/snap_counts/snap_counts_{yr}.csv", f"nflv_snaps_{yr}.csv", 3))
    by = {}
    for r in snaps:
        if r.get("game_type") != "REG":
            continue
        e = p2e.get(r.get("pfr_player_id"))
        pct = _f(r.get("offense_pct"))
        if e and pct is not None and (_f(r.get("offense_snaps")) or 0) > 0:
            by.setdefault(e, []).append((int(r["week"]), pct))
    for e, rows in by.items():
        rows.sort()
        vals = [v for _, v in rows]
        P(e)["snap"] = {"season": round(sum(vals) / len(vals), 3), "l3": round(sum(vals[-3:]) / len(vals[-3:]), 3), "n": len(vals),
                        "last": round(vals[-1], 3)}

    # ---- weekly player stats: target share, air-yards share
    sp = _csv(_fetch(f"{NFLV}/stats_player/stats_player_week_{yr}.csv", f"nflv_stats_{yr}.csv", 3))
    by = {}
    for r in sp:
        if r.get("season_type") != "REG":
            continue
        e = g2e.get(r.get("player_id"))
        if not e:
            continue
        by.setdefault(e, []).append((int(r["week"]), _f(r.get("target_share")), _f(r.get("air_yards_share"))))
    for e, rows in by.items():
        rows.sort()
        ts = [t for _, t, _ in rows if t is not None]
        ay = [a for _, _, a in rows if a is not None]
        if len(ts) >= 2:
            P(e)["share"] = {"tgt": round(sum(ts) / len(ts), 3), "tgtL3": round(sum(ts[-3:]) / len(ts[-3:]), 3),
                             "ay": round(sum(ay) / len(ay), 3) if ay else None, "ayL3": round(sum(ay[-3:]) / len(ay[-3:]), 3) if ay else None,
                             "n": len(ts)}

    # ---- play-by-play: red-zone looks and expected TDs
    pbp = _fetch(f"{NFLV}/pbp/play_by_play_{yr}.csv.gz", f"nflv_pbp_{yr}.csv.gz", 6)
    if pbp:
        try:
            rz = {}
            games = {}
            lg = {"t20": [0, 0], "t10": [0, 0], "c20": [0, 0], "c10": [0, 0]}  # [opps, TDs]
            for r in csv.DictReader(io.StringIO(gzip.decompress(pbp).decode("utf-8", "replace"))):
                if r.get("season_type") != "REG" or r.get("two_point_attempt") == "1" or r.get("qb_kneel") == "1" or r.get("qb_spike") == "1":
                    continue
                yl = _f(r.get("yardline_100"))
                gid = r.get("game_id")
                for col in ("receiver_player_id", "rusher_player_id"):
                    g = r.get(col)
                    if g:
                        games.setdefault(g, set()).add(gid)
                if yl is None or yl > 20:
                    continue
                td = r.get("touchdown") == "1"
                if r.get("pass_attempt") == "1" and r.get("sack") != "1" and r.get("receiver_player_id"):
                    who, kind = r["receiver_player_id"], "t"
                elif r.get("rush_attempt") == "1" and r.get("rusher_player_id"):
                    who, kind = r["rusher_player_id"], "c"
                else:
                    continue
                zone = "10" if yl <= 10 else "20"
                key = kind + zone
                scored = td and r.get("td_player_id") == who
                lg[key][0] += 1
                lg[key][1] += 1 if scored else 0
                d = rz.setdefault(who, {"t20": 0, "t10": 0, "c20": 0, "c10": 0})
                d[key] += 1
            rate = {k: (v[1] / v[0] if v[0] else 0) for k, v in lg.items()}
            for g, d in rz.items():
                e = g2e.get(g)
                ng = len(games.get(g, ()))
                if not e or ng < 2:
                    continue
                xtd = sum(d[k] * rate[k] for k in d) / ng
                P(e)["rz"] = {"tgt": round((d["t20"] + d["t10"]) / ng, 2), "car": round((d["c20"] + d["c10"]) / ng, 2),
                              "i10": round((d["t10"] + d["c10"]) / ng, 2), "xtd": round(xtd, 3), "g": ng}
        except Exception as ex:
            S.log(f"[nflverse] red-zone parse failed: {ex}")

    # ---- Next Gen Stats (season rows = week 0)
    for kind, fields in (("receiving", ("avg_separation", "avg_yac_above_expectation", "targets")),
                         ("rushing", ("rush_yards_over_expected_per_att", "efficiency", "rush_attempts")),
                         ("passing", ("completion_percentage_above_expectation", "aggressiveness", "attempts"))):
        rows = _csv(_fetch(f"{NFLV}/nextgen_stats/ngs_{kind}.csv.gz", f"nflv_ngs_{kind}.csv.gz", 12), gz=True)
        rows = [r for r in rows if r.get("season") == str(yr) and r.get("season_type") == "REG" and r.get("week") == "0"]
        if not rows:
            continue
        vol = fields[2]
        qual = [r for r in rows if (_f(r.get(vol)) or 0) >= {"receiving": 20, "rushing": 40, "passing": 100}[kind]]
        stats = {}
        for fld in fields[:2]:
            vals = [_f(r.get(fld)) for r in qual if _f(r.get(fld)) is not None]
            if len(vals) >= 10:
                stats[fld] = (statistics.mean(vals), statistics.pstdev(vals) or 1)
        for r in rows:
            e = g2e.get(r.get("player_gsis_id"))
            if not e:
                continue
            out = {"vol": _f(r.get(vol))}
            for fld, (mu, sd) in stats.items():
                v = _f(r.get(fld))
                if v is not None:
                    out[fld] = round(v, 3)
                    out[fld + "_z"] = round((v - mu) / sd, 2)
            P(e).setdefault("ngs", {})[kind] = out

    # ---- official injury report with practice participation (latest week only)
    inj = _csv(_fetch(f"{NFLV}/injuries/injuries_{yr}.csv", f"nflv_inj_{yr}.csv", 1))
    inj = [r for r in inj if r.get("season_type") == "REG"]
    if inj:
        wk = max(int(r["week"]) for r in inj)
        for r in inj:
            if int(r["week"]) != wk:
                continue
            e = g2e.get(r.get("gsis_id"))
            if not e:
                continue
            P(e)["inj"] = {"wk": wk, "status": (r.get("report_status") or "").strip(), "practice": (r.get("practice_status") or "").strip(),
                           "injury": (r.get("report_primary_injury") or r.get("practice_primary_injury") or "").strip()}

    # ---- ESPN depth charts (offense)
    depth = nfl_depth(yr, {p["teamId"] for p in roster.values() if p.get("teamId")})
    for e, d in depth.items():
        P(e)["depth"] = d

    S.log(f"[nflverse] {len(prof)} player profiles ({sum('snap' in p for p in prof.values())} snaps, "
          f"{sum('rz' in p for p in prof.values())} red zone, {sum('ngs' in p for p in prof.values())} NGS, "
          f"{sum('inj' in p for p in prof.values())} on the injury report, {len(depth)} depth slots) in {time.time()-t0:.0f}s")
    return prof, {"week": max((p["inj"]["wk"] for p in prof.values() if "inj" in p), default=None)}


DEPTH_POS = {"qb": "QB", "rb": "RB", "wr": "WR", "te": "TE", "lwr": "WR", "rwr": "WR", "swr": "WR", "slwr": "WR", "fb": "FB"}


def nfl_depth(yr, team_ids):
    def one(tid):
        d = S.get(f"{ESPN_CORE}/seasons/{yr}/teams/{tid}/depthcharts") or {}
        out = {}
        for item in d.get("items") or []:
            for key, pos in (item.get("positions") or {}).items():
                ab = DEPTH_POS.get(key.lower())
                if not ab:
                    continue
                for a in pos.get("athletes") or []:
                    m = re.search(r"/athletes/(\d+)", (a.get("athlete") or {}).get("$ref", ""))
                    rk = a.get("rank")
                    if m and rk:
                        cur = out.get(m.group(1))
                        if not cur or rk < cur[1]:
                            out[m.group(1)] = [ab, rk]
        return out
    res = {}
    try:
        for part in S.pmap(one, sorted(team_ids), 8):
            res.update(part)
    except Exception as ex:
        S.log(f"[depth] failed: {ex}")
    return res


NFL_OUT = {"out": "Out", "doubtful": "Doubtful", "questionable": "Questionable"}


def apply_nfl_injuries(roster, prof):
    """Official report status overrides ESPN's tag (feeds the teammates-out logic too)."""
    n = 0
    for aid, p in roster.items():
        i = (prof.get(aid) or {}).get("inj")
        if not i or not i["status"]:
            continue
        st = NFL_OUT.get(i["status"].lower())
        if st:
            p["inj"] = st
            if i.get("injury"):
                p["injDetail"] = i["injury"]
            p["injSrc"] = "NFL report"
            n += 1
    S.log(f"[nflverse] official injury report applied to {n} players")


def nfl_factors(prof):
    """Returns f(k, grp, base) -> list of [name, mult, note] built from the nflverse profile."""
    REC = {"receptions", "targets", "rec_yds", "long_rec", "rush_rec_yds"}
    RUSH = {"rush_yds", "rush_att", "rush_rec_yds"}
    PASS = {"pass_yds", "pass_cmp", "pass_att", "pass_tds", "pass_rush_yds"}
    VOL = REC | RUSH | {"anytime_td"}

    def f(k, grp, base=None):
        out = []
        if not prof:
            return out
        sn, sh, rz, ngs, inj, dp = (prof.get(x) for x in ("snap", "share", "rz", "ngs", "inj", "depth"))
        # snap-share trend (skill players)
        if sn and grp in ("RB", "WR", "TE") and k in VOL and sn["n"] >= 4 and sn["season"] >= 0.15:
            r = sn["l3"] / sn["season"]
            if abs(r - 1) >= 0.08:
                out.append(["Snap share trend", _clamp(1 + 0.35 * (r - 1), 0.9, 1.1),
                            f"{sn['l3']*100:.0f}% of snaps last 3 vs {sn['season']*100:.0f}% season (nflverse)"])
        # target / air-yards share trend
        if sh and grp in ("RB", "WR", "TE") and k in REC and sh["n"] >= 4 and (sh["tgt"] or 0) >= 0.05:
            r = sh["tgtL3"] / sh["tgt"]
            if k in ("rec_yds", "long_rec") and sh.get("ay") and sh.get("ayL3") is not None and sh["ay"] >= 0.05:
                r = 0.5 * r + 0.5 * (sh["ayL3"] / sh["ay"])
            if abs(r - 1) >= 0.1:
                out.append(["Target share trend", _clamp(1 + 0.4 * (r - 1), 0.9, 1.12),
                            f"{sh['tgtL3']*100:.0f}% target share last 3 vs {sh['tgt']*100:.0f}% season"])
        # red-zone looks -> expected TDs
        if k == "anytime_td" and rz and base and base > 0.04 and rz["g"] >= 3:
            target = 0.5 * base + 0.5 * rz["xtd"]
            out.append(["Red-zone looks", _clamp(target / base, 0.7, 1.35),
                        f"{rz['tgt']:.1f} RZ targets + {rz['car']:.1f} RZ carries a game ({rz['i10']:.1f} inside the 10) = {rz['xtd']:.2f} expected TDs"])
        # Next Gen Stats efficiency (small nudges)
        ng = ngs or {}
        rc, ru, pa = ng.get("receiving"), ng.get("rushing"), ng.get("passing")
        if rc and k in ("rec_yds", "receptions", "long_rec") and (rc.get("vol") or 0) >= 20 and rc.get("avg_separation_z") is not None:
            z = rc["avg_separation_z"] + 0.5 * (rc.get("avg_yac_above_expectation_z") or 0)
            m = _clamp(1 + 0.015 * z, 0.97, 1.03)
            out.append(["Next Gen: separation", m, f"{rc['avg_separation']:.1f} yds separation, {rc.get('avg_yac_above_expectation', 0):+.1f} YAC over expected"])
        if ru and k in ("rush_yds", "rush_rec_yds") and (ru.get("vol") or 0) >= 40 and ru.get("rush_yards_over_expected_per_att_z") is not None:
            m = _clamp(1 + 0.02 * ru["rush_yards_over_expected_per_att_z"], 0.96, 1.04)
            out.append(["Next Gen: yards over expected", m, f"{ru['rush_yards_over_expected_per_att']:+.2f} rush yds over expected per carry"])
        if pa and k in PASS and (pa.get("vol") or 0) >= 100 and pa.get("completion_percentage_above_expectation_z") is not None:
            m = _clamp(1 + 0.015 * pa["completion_percentage_above_expectation_z"], 0.97, 1.03)
            out.append(["Next Gen: CPOE", m, f"{pa['completion_percentage_above_expectation']:+.1f}% completion over expected"])
        # his own injury report + practice
        if inj and "not injury related" not in (inj.get("injury") or "").lower():
            st, pr = inj["status"].lower(), inj["practice"].lower()
            if st == "questionable":
                if "did not" in pr:
                    out.append(["Questionable, no practice", 0.88, f"Questionable ({inj['injury'] or 'injury'}), did not practice"])
                elif "limited" in pr:
                    out.append(["Questionable, limited", 0.95, f"Questionable ({inj['injury'] or 'injury'}), limited in practice"])
                else:
                    out.append(["Questionable", 0.97, f"Questionable ({inj['injury'] or 'injury'})"])
            elif not st and "did not" in pr:
                out.append(["Missed practice", 0.96, f"Did not practice ({inj['injury'] or 'injury'}); no game status yet"])
        # depth chart: deep backups lose snaps
        if dp and grp in ("RB", "TE") and dp[0] in ("RB", "TE") and dp[1] >= 3 and k in VOL:
            out.append(["Depth chart", 0.95, f"{dp[0]}{dp[1]} on the ESPN depth chart"])
        return out
    return f


# ======================================================================================
# NBA: official league injury report (PDF)
# ======================================================================================
NBA_STATUS = ("Out", "Doubtful", "Questionable", "Probable", "Available")
_NBA_LINE = re.compile(r"([A-Z][A-Za-z'.\-]*(?:,[A-Za-z'.\-]+)+)\s+(Out|Doubtful|Questionable|Probable|Available)\b")


def nba_injury_report(now, teams):
    """{team_abbr: {norm_name: [status, reason]}} from the newest official report (ET timestamps)."""
    try:
        import pdfplumber
    except Exception:
        S.log("[nba-inj] pdfplumber missing; skipping")
        return {}, None
    et = now - timedelta(hours=5)
    t = et.replace(minute=(et.minute // 15) * 15, second=0, microsecond=0)
    data, stamp = None, None
    for _ in range(96):  # look back up to 24h
        url = NBA_INJ.format(d=t.strftime("%Y-%m-%d"), t=t.strftime("%I_%M%p"))
        try:
            r = S.SESSION.get(url, timeout=20)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                data, stamp = r.content, t
                break
        except Exception:
            pass
        t -= timedelta(minutes=15)
    if not data:
        S.log("[nba-inj] no report found")
        return {}, None
    name2ab = {}
    for tm in teams:
        for nm in (tm.get("displayName"), tm.get("name"), tm.get("location")):
            if nm:
                name2ab[re.sub(r"[^A-Za-z]", "", nm)] = tm["abbreviation"]
    # longest team names first so "LosAngelesLakers" wins over "Lakers"
    keys = sorted(name2ab, key=len, reverse=True)
    out, team, n = {}, None, 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                for k in keys:
                    if k in line.replace(" ", ""):
                        team = name2ab[k]
                        break
                m = _NBA_LINE.search(line)
                if not m or not team:
                    continue
                parts = m.group(1).split(",")
                first = parts[-1]
                last = " ".join(re.sub(r"(?<=[a-z])(Jr\.?|Sr\.?|II|III|IV)$", "", p) for p in parts[:-1])
                reason = line[m.end():].strip()
                out.setdefault(team, {})[S.norm_name(f"{first} {last}")] = [m.group(2), reason]
                n += 1
    S.log(f"[nba-inj] official report {stamp.strftime('%m/%d %I:%M %p')} ET: {n} players on {len(out)} teams")
    return out, stamp.strftime("%Y-%m-%dT%H:%M") + " ET"


def apply_nba_injuries(roster, rep):
    n = 0
    for p in roster.values():
        for ab in S.abbr_set("NBA", p["team"]):
            hit = (rep.get(ab) or {}).get(p["nn"])
            if hit:
                st, reason = hit
                p["inj"] = None if st == "Available" else st
                p["injDetail"] = re.sub(r"^Injury/Illness-", "", reason) or p.get("injDetail")
                p["injSrc"] = "NBA report"
                n += 1
                break
    S.log(f"[nba-inj] applied to {n} rostered players")


def nba_factors(pl):
    def f(k, grp, base=None):
        st = (pl.get("inj") or "").lower()
        if pl.get("injSrc") == "NBA report" and st == "questionable":
            return [["Questionable (NBA report)", 0.94 if k == "min" else 0.96, f"Listed questionable: {pl.get('injDetail') or 'injury'}"]]
        if pl.get("injSrc") == "NBA report" and st == "probable":
            return [["Probable (NBA report)", 0.99, f"Listed probable: {pl.get('injDetail') or 'injury'}"]]
        return []
    return f


# ======================================================================================
# Kalshi settlements
# ======================================================================================
def kalshi_results(tickers):
    """ticker -> 'yes' | 'no' | 'void' (settled without a yes/no) ; unsettled tickers are left out."""
    out = {}
    tickers = sorted({t for t in tickers if t})
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        d = S.get(f"{S.KALSHI}/markets", params={"tickers": ",".join(chunk), "limit": 200}) or {}
        for m in d.get("markets") or []:
            res = (m.get("result") or "").lower()
            st = (m.get("status") or "").lower()
            if res in ("yes", "no"):
                out[m["ticker"]] = res
            elif st in ("settled", "finalized", "determined") and res in ("", "void", "scalar"):
                out[m["ticker"]] = "void"
    return out


# ======================================================================================
# The Odds API (optional): consensus player-prop lines
# ======================================================================================
ODDS_SPORT = {"NFL": "americanfootball_nfl", "NBA": "basketball_nba"}
# priority order: when the monthly budget is tight the first markets get fetched first
ODDS_MARKETS = {
    "NBA": [("player_points", "points"), ("player_rebounds", "rebounds"), ("player_assists", "assists"),
            ("player_threes", "threes"), ("player_points_rebounds_assists", "pra")],
    "NFL": [("player_reception_yds", "rec_yds"), ("player_rush_yds", "rush_yds"), ("player_pass_yds", "pass_yds"),
            ("player_receptions", "receptions"), ("player_anytime_td", "anytime_td")],
}
ODDS_REFRESH_H = 12
ODDS_WINDOW_H = 40
ODDS_FREE = 500


def _am_to_p(price):
    price = _f(price)
    if price is None or price == 0:
        return None
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def odds_consensus(sport, games, now):
    """{(norm_name, stat_key): {"line", "pOver", "books"}} for games starting soon. Needs ODDS_API_KEY."""
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        return {}, None
    path = f"{CACHE}/odds.json"
    cache = S.load_json(path, {})
    month = now.strftime("%Y-%m")
    if cache.get("month") != month:
        cache = {"month": month, "events": cache.get("events", {}), "remaining": None, "spent": {}}
    spent_today = cache.setdefault("spent", {}).get(now.strftime("%Y-%m-%d"), 0)
    rem = cache.get("remaining")
    days_left = (datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc) - now).days + 1
    allowance = max(2, int((rem if rem is not None else ODDS_FREE) / max(days_left, 1)))
    budget = max(0, allowance - spent_today)

    # events list costs nothing
    try:
        r = S.SESSION.get(f"{ODDS}/sports/{ODDS_SPORT[sport]}/events", params={"apiKey": key}, timeout=30)
        events = r.json() if r.status_code == 200 else []
        if r.status_code != 200:
            S.log(f"[odds] {sport}: events call failed ({r.status_code}) {r.text[:120]}")
    except Exception as ex:
        events = []
        S.log(f"[odds] {sport}: events call failed: {ex}")
    soon = []
    for ev in events if isinstance(events, list) else []:
        t = S.parse_t(ev.get("commence_time"))
        if t and now - timedelta(hours=1) < t < now + timedelta(hours=ODDS_WINDOW_H):
            soon.append((t, ev))
    soon.sort(key=lambda x: x[0])
    evc = cache.setdefault("events", {})
    used = 0
    for mkt, _ in ODDS_MARKETS[sport]:
        for t, ev in soon:
            slot = evc.setdefault(ev["id"], {"t": ev.get("commence_time"), "home": ev.get("home_team"), "away": ev.get("away_team"), "m": {}})
            got = slot["m"].get(mkt)
            if got and now - S.parse_t(got["at"]) < timedelta(hours=ODDS_REFRESH_H):
                continue
            if used >= budget:
                break
            try:
                r = S.SESSION.get(f"{ODDS}/sports/{ODDS_SPORT[sport]}/events/{ev['id']}/odds",
                                  params={"apiKey": key, "regions": "us", "markets": mkt, "oddsFormat": "american"}, timeout=30)
            except Exception:
                continue
            if r.headers.get("x-requests-remaining") is not None:
                cache["remaining"] = _f(r.headers.get("x-requests-remaining"))
            if r.status_code != 200:
                S.log(f"[odds] {sport} {mkt}: {r.status_code} {r.text[:120]}")
                if r.status_code in (401, 429):
                    budget = 0
                continue
            used += _f(r.headers.get("x-requests-last")) or 1
            slot["m"][mkt] = {"at": now.isoformat()[:19], "books": [
                {"k": b.get("key"), "o": [[o.get("description"), o.get("name"), o.get("point"), o.get("price")]
                                         for m in b.get("markets") or [] if m.get("key") == mkt for o in m.get("outcomes") or []]}
                for b in r.json().get("bookmakers") or []]}
    cache["spent"][now.strftime("%Y-%m-%d")] = spent_today + used
    cache["spent"] = dict(sorted(cache["spent"].items())[-40:])
    # drop finished events
    cache["events"] = {k: v for k, v in evc.items() if S.parse_t(v.get("t")) and S.parse_t(v["t"]) > now - timedelta(hours=6)}
    S.save_json(path, cache)

    out = {}
    stat_of = dict(ODDS_MARKETS[sport])
    for ev in cache["events"].values():
        for mkt, blob in ev["m"].items():
            k = stat_of.get(mkt)
            if not k:
                continue
            per = {}  # player -> list of (line, pOver) per book, de-vigged
            for b in blob["books"]:
                pairs = {}
                for desc, side, point, price in b["o"]:
                    if not desc:
                        continue
                    pairs.setdefault((desc, point), {})[(side or "").lower()] = _am_to_p(price)
                for (desc, point), sides in pairs.items():
                    if mkt == "player_anytime_td":
                        py = sides.get("yes")
                        if py is None:
                            continue
                        pn = sides.get("no")
                        po = py / (py + pn) if pn else py / 1.08  # yes-only books: strip a typical hold
                        per.setdefault(desc, []).append((0.5, po))
                    else:
                        po, pu = sides.get("over"), sides.get("under")
                        if po is None or pu is None or point is None:
                            continue
                        per.setdefault(desc, []).append((float(point), po / (po + pu)))
            for desc, rows in per.items():
                line = statistics.median([l for l, _ in rows])
                at = [p for l, p in rows if abs(l - line) < 1e-9] or [p for _, p in rows]
                out[(S.norm_name(desc), k)] = {"line": line, "pOver": round(sum(at) / len(at), 4), "books": len(rows)}
    rem = cache.get("remaining")
    S.log(f"[odds] {sport}: {len(out)} consensus prop lines, {used:.0f} credits used this run"
          + (f", {rem:.0f} left this month" if rem is not None else ""))
    return out, {"remaining": rem, "usedToday": cache["spent"][now.strftime("%Y-%m-%d")], "allowance": allowance}


def cons_p_at(cons, k, line, sd, count):
    """Market chance of going over `line`, shifted from the consensus line using the model's spread."""
    if not cons:
        return None
    if abs(cons["line"] - line) < 1e-9:
        return cons["pOver"]
    p = _clamp(cons["pOver"], 0.02, 0.98)
    if count:
        # Poisson: find the mean that gives pOver at the consensus line, then price our line
        lo, hi = 0.01, 40.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if _pois_over(mid, cons["line"]) < p:
                lo = mid
            else:
                hi = mid
        return _pois_over((lo + hi) / 2, line)
    # normal: implied mean = line_c + sd * z(p)
    z = _ninv(p)
    mu = cons["line"] + sd * z
    return _phi((mu - line) / max(sd, 0.5))


def _pois_over(lam, line):
    k = math.floor(line)
    cdf, term = 0.0, math.exp(-lam)
    for i in range(0, k + 1):
        if i > 0:
            term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1 - cdf))


def _ninv(p):
    lo, hi = -6.0, 6.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
