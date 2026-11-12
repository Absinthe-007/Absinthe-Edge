"""Data sources: DraftKings Pick6, Kalshi, ESPN (schedule, rosters, box scores, game logs,
standings) and Open-Meteo (stadium weather). No API keys needed."""
import json
import math
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

UA = {"User-Agent": "absinthe-edge/1.0"}
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) absinthe-edge/1.0"}
SESSION = requests.Session()
# ESPN blocks custom agents; keep the default requests User-Agent for it
LOG = []

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3"
ESPN_STAND = "https://site.api.espn.com/apis/v2/sports"
ESPN_PATH = {"NFL": "football/nfl", "NBA": "basketball/nba"}
P6 = "https://pick6.draftkings.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GEO = "https://geocoding-api.open-meteo.com/v1/search"
METEO = "https://api.open-meteo.com/v1/forecast"


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOG.append(msg)


def get(url, params=None, as_text=False, tries=3, timeout=30):
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.text if as_text else r.json()
            if r.status_code in (400, 401, 403, 404):
                return None
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def pmap(fn, items, workers=8):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, compact=True):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":") if compact else None, indent=None if compact else 1)


def num(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None


def split_num(x, i):
    m = re.match(r"\s*(\d+)\s*[-/]\s*(\d+)", str(x or ""))
    return float(m.group(i + 1)) if m else None


def parse_t(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def norm_name(n):
    if not n:
        return ""
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = re.sub(r"\s*\([^)]*\)", "", n)
    if "," in n:
        last, first = n.split(",", 1)
        n = f"{first} {last}"
    n = n.lower().replace(".", "").replace("'", "").replace("-", " ")
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


# Team abbreviation aliases (ESPN <-> Pick6 <-> Kalshi)
ALIASES = {
    "NFL": {"WSH": ["WAS"], "JAX": ["JAC"], "LAR": ["LA", "LAR"], "LV": ["LVR", "OAK"], "KC": ["KAN"], "NE": ["NWE"],
            "NO": ["NOR"], "SF": ["SFO"], "TB": ["TAM"], "GB": ["GNB"]},
    "NBA": {"GS": ["GSW"], "NO": ["NOP"], "SA": ["SAS"], "WSH": ["WAS"], "UTAH": ["UTA"], "NY": ["NYK"], "PHX": ["PHO"],
            "BKN": ["BRK", "BKN"], "CHA": ["CHO"]},
}


def abbr_set(sport, a):
    a = (a or "").upper()
    s = {a}
    for k, v in ALIASES[sport].items():
        if a == k or a in v:
            s |= {k, *v}
    return s


# ======================================================================================
# Stat labels -> canonical keys (Pick6 + Kalshi)
# ======================================================================================
PARTIAL = re.compile(r"(1st|2nd|3rd|4th|first|second|third|fourth)\s*(half|quarter|qtr)|\b[1-4][hq]\b|quarter|half|most |game high", re.I)


def stat_key(label, sport):
    s = (label or "").lower().replace("&", "+").replace(" and ", " + ")
    if PARTIAL.search(s):
        return None
    if sport == "NFL":
        if "first" in s or "1st" in s or "longest" in s and "recep" not in s:
            return None
        if "pass" in s and "rush" in s and "yard" in s:
            return "pass_rush_yds"
        if "rush" in s and "rec" in s and "yard" in s:
            return "rush_rec_yds"
        if ("rush" in s and "rec" in s and ("td" in s or "touchdown" in s)) or "anytime" in s:
            return "anytime_td"
        if "longest" in s and "recep" in s:
            return "long_rec"
        if "pass" in s and "yard" in s:
            return "pass_yds"
        if "pass" in s and ("td" in s or "touchdown" in s):
            return "pass_tds"
        if "pass" in s and "attempt" in s:
            return "pass_att"
        if "complet" in s:
            return "pass_cmp"
        if "intercept" in s:
            return "ints"
        if "rush" in s and "yard" in s:
            return "rush_yds"
        if "rush" in s and "attempt" in s:
            return "rush_att"
        if "receiving" in s and "yard" in s or s.startswith("rec yards"):
            return "rec_yds"
        if "reception" in s:
            return "receptions"
        if "field goal" in s:
            return "fg_made"
        if "kicking point" in s:
            return "kick_pts"
        if "tackle" in s:
            return "tackles"
        if "sack" in s:
            return "sacks"
        return None
    if "first" in s or "1st" in s or "double" in s or "fantasy" in s:
        return None
    if re.search(r"3[- ]?p(oint|t)|three", s):
        return "threes"
    pts = "point" in s or re.search(r"\bpts\b", s)
    reb = "rebound" in s or re.search(r"\brebs?\b", s)
    ast = "assist" in s or re.search(r"\basts?\b", s)
    stl = "steal" in s or re.search(r"\bstls?\b", s)
    blk = "block" in s or re.search(r"\bblks?\b", s)
    if pts and reb and ast:
        return "pra"
    if pts and reb:
        return "pts_reb"
    if pts and ast:
        return "pts_ast"
    if reb and ast:
        return "reb_ast"
    if stl and blk:
        return "stl_blk"
    for flag, k in ((pts, "points"), (reb, "rebounds"), (ast, "assists"), (stl, "steals"), (blk, "blocks")):
        if flag:
            return k
    if "turnover" in s:
        return "turnovers"
    return None


# ======================================================================================
# DraftKings Pick6  (React Router turbo-stream data at /_.data)
# ======================================================================================
def turbo_decode(text):
    lines = [l for l in text.split("\n") if l.strip()]
    values = json.loads(lines[0])
    cache = {}
    special = {-1: None, -2: None, -3: None, -4: 0.0, -5: None, -6: None, -7: None}

    def hyd(idx):
        if isinstance(idx, int) and idx < 0:
            return special.get(idx)
        if idx in cache:
            return cache[idx]
        v = values[idx]
        if isinstance(v, list):
            if len(v) >= 2 and isinstance(v[0], str) and len(v[0]) == 1:
                cache[idx] = v[1]
                return v[1]
            out = []
            cache[idx] = out
            out.extend(hyd(i) for i in v)
            return out
        if isinstance(v, dict):
            out = {}
            cache[idx] = out
            for k, vi in v.items():
                key = values[int(k[1:])] if k.startswith("_") else k
                out[key] = hyd(vi)
            return out
        cache[idx] = v
        return v

    return hyd(0)


def p6_root(sport, extra=None):
    pill = {"NFL": "1-1", "NBA": "4-4"}[sport]
    for attempt in range(6):
        params = {"sport": sport} if attempt % 2 == 0 else {"sport": sport, "pill": pill}
        params.update(extra or {})
        for path in ("/_.data", "/_root.data"):
            txt = get(P6 + path, params=params, as_text=True)
            try:
                r = turbo_decode(txt)["routes/_homeShared"]["data"] if txt else None
            except Exception:
                r = None
            got = (r or {}).get("selectedSportLeague", {}).get("league", {}).get("leagueAbbreviation")
            if r and got == sport:
                return r
        time.sleep(2 + 2 * attempt)
    return None


def pick6(sport):
    root = p6_root(sport)
    if not root:
        log(f"[pick6] {sport}: no active slate")
        return [], {}
    payouts = {}
    for pkg in (root.get("entryDetails") or {}).get("payoutPackages") or []:
        n = pkg["pickSetSize"]
        tiers = {t["numberOfPicksCorrect"]: t["guaranteedMultiplier"] for t in pkg["payoutTiers"]}
        lo, hi = 0.3, 0.95
        for _ in range(60):
            p = (lo + hi) / 2
            ev = sum(m * math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k, m in tiers.items())
            lo, hi = (p, hi) if ev < 1 else (lo, p)
        payouts[n] = round((lo + hi) / 2, 4)
    props, seen = [], set()
    for g in root.get("pickGroups") or []:
        pg = g["pickGroupId"]
        if pg == int(root.get("pickGroupId") or 0):
            cats = (root.get("pickCardsResponse") or {}).get("orderedPickCategoryIds")
        else:
            r2 = p6_root(sport, {"pickGroup": pg})
            cats = ((r2 or {}).get("pickCardsResponse") or {}).get("orderedPickCategoryIds")
        for cat in cats or [1]:
            d = get(f"{P6}/resources/pickCardsByCategory/{pg}/{cat}")
            if not d:
                continue
            mkts, ents = d.get("pickSixMarketById", {}), d.get("entityInfoByDkId", {})
            comps, teams = d.get("competitionById", {}), d.get("displayTeamById", {})
            for card in d.get("pickCardByPickableId", {}).values():
                pid = card["pickableId"]
                if pid in seen or len(card.get("entities") or []) != 1:
                    continue
                seen.add(pid)
                default = next((m for m in card.get("activePickableMarkets", []) if m["pickableMarketId"] == card.get("defaultPickableMarketId")), None)
                if not default:
                    continue
                sels = {s["statLinePropositionId"]: s["standingsMultiplier"] for s in default.get("activeSelections", [])}
                mk = mkts.get(str(default["pickSixMarketId"]), {})
                e = card["entities"][0]
                info = ents.get(str(e["dkId"]), {})
                team = opp = pos = start = ""
                for cid in e.get("compIds", []):
                    c = comps.get(str(cid))
                    if not c:
                        continue
                    start = c.get("startTime", "")
                    ec = (c.get("entityCompByDkId") or {}).get(str(e["dkId"]), {})
                    tid = ec.get("teamId")
                    pos = ec.get("position", "")
                    team = (teams.get(str(tid)) or {}).get("name", "")
                    other = c.get("awayTeamId") if tid == c.get("homeTeamId") else c.get("homeTeamId")
                    opp = (teams.get(str(other)) or {}).get("name", "")
                key = stat_key(mk.get("name", ""), sport)
                if not key:
                    continue
                props.append({"platform": "Pick6", "id": f"p6-{pid}", "sport": sport, "player": info.get("fullName") or info.get("name", ""),
                              "team": team, "opp": opp, "pos": pos, "start": start[:19], "stat": mk.get("name", ""), "key": key,
                              "line": default["targetValue"], "moreMult": sels.get(1), "lessMult": sels.get(2),
                              "dkImg": (info.get("imageUrls") or {}).get("160", "")})
    log(f"[pick6] {sport}: {len(props)} props")
    return props, payouts


# ======================================================================================
# Kalshi (public market data, no auth)
# ======================================================================================
KALSHI_PROPS = {
    "NBA": {"KXNBAPTS": "points", "KXNBAREB": "rebounds", "KXNBAAST": "assists", "KXNBA3PT": "threes", "KXNBASTL": "steals",
            "KXNBABLK": "blocks", "KXNBASTOCK": "stl_blk", "KXNBASTOCKS": "stl_blk", "KXNBAPRA": "pra"},
    "NFL": {"KXNFLPASSYDS": "pass_yds", "KXNFLRSHYDS": "rush_yds", "KXNFLRECYDS": "rec_yds", "KXNFLREC": "receptions",
            "KXNFLANYTD": "anytime_td", "KXNFL2TD": "anytime_td", "KXNFLPASSTDS": "pass_tds", "KXNFLRRYDS": "rush_rec_yds",
            "KXNFLPASSCOMP": "pass_cmp", "KXNFLPASSATT": "pass_att", "KXNFLPASSINT": "ints", "KXNFLGAMESACK": "sacks"},
}
KALSHI_GAME = {"NFL": {"KXNFLGAME": "ml", "KXNFLSPREAD": "spread", "KXNFLTOTAL": "total"},
               "NBA": {"KXNBAGAME": "ml", "KXNBASPREAD": "spread", "KXNBATOTAL": "total"}}


def kalshi_series(series):
    out, cursor = [], None
    for _ in range(25):
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = get(f"{KALSHI}/markets", params=params)
        if not d:
            break
        out += d.get("markets") or []
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            break
    return out


def _px(m, k):
    v = num(m.get(k + "_dollars"))
    if v is None and m.get(k) is not None:
        v = num(m.get(k)) / 100
    return v


def kalshi_slug(series):
    """Kalshi's web URL slug is the series title, lower-cased and hyphenated ('NFL Game' -> 'nfl-game')."""
    d = get(f"{KALSHI}/series/{series}") or {}
    t = ((d.get("series") or {}).get("title") or "").lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def kalshi(sport):
    rows = []
    allseries = {**{s: ("prop", k) for s, k in KALSHI_PROPS[sport].items()}, **{s: ("game", k) for s, k in KALSHI_GAME[sport].items()}}
    res = pmap(lambda s: (s, kalshi_series(s)), list(allseries), 6)
    slugs = dict(zip(allseries, pmap(kalshi_slug, list(allseries), 6)))
    for series, mkts in res:
        kind, key = allseries[series]
        for m in mkts:
            yb, ya = _px(m, "yes_bid"), _px(m, "yes_ask")
            if ya is None or ya <= 0 or ya >= 1:
                continue
            code = m["event_ticker"].split("-", 1)[1] if "-" in m["event_ticker"] else ""
            row = {"platform": "Kalshi", "id": "k-" + m["ticker"], "sport": sport, "series": series, "kind": kind, "key": key,
                   "code": code, "ticker": m["ticker"], "event": m["event_ticker"], "yesBid": yb or 0.0, "yesAsk": ya,
                   "noAsk": round(1 - (yb or 0.0), 4), "title": m.get("title") or m.get("yes_sub_title"),
                   "close": (m.get("occurrence_datetime") or m.get("expected_expiration_time") or "")[:19],
                   "url": (f"https://kalshi.com/markets/{series.lower()}/{slugs[series]}/{m['event_ticker'].lower()}?marketTicker={m['ticker']}"
                           if slugs.get(series) else f"https://kalshi.com/markets/{series.lower()}")}
            strike = num(m.get("floor_strike"))
            sub = m.get("yes_sub_title") or m.get("title") or ""
            if kind == "prop":
                name = sub.split(":")[0].strip()
                if strike is None:
                    mm = re.search(r"(\d+(?:\.\d+)?)\+", sub)
                    strike = float(mm.group(1)) - 0.5 if mm else None
                if series == "KXNFL2TD" and strike is None:
                    strike = 1.5
                if strike is None or not name:
                    continue
                row.update({"player": name, "line": strike, "teamCode": m["ticker"].split("-")[2][:3] if m["ticker"].count("-") >= 2 else ""})
            else:
                suffix = m["ticker"].rsplit("-", 1)[-1]
                row["team"] = re.sub(r"\d+$", "", suffix) if key != "total" else ""
                row["line"] = strike
                if key == "spread" and strike is None:
                    continue
                if key == "total" and strike is None:
                    continue
            rows.append(row)
    log(f"[kalshi] {sport}: {sum(r['kind']=='prop' for r in rows)} player markets, {sum(r['kind']=='game' for r in rows)} game markets")
    return rows


# ======================================================================================
# ESPN
# ======================================================================================
def espn_games(sport, now):
    """Upcoming + today's games (NFL: current week, NBA: today and next 2 days)."""
    path = ESPN_PATH[sport]
    evs = []
    if sport == "NFL":
        sb = get(f"{ESPN}/{path}/scoreboard") or {}
        evs = sb.get("events", [])
        # include next week too, so the board is full right after Monday night
        wk = (sb.get("week") or {}).get("number")
        if wk and (sb.get("season") or {}).get("type", 2) == 2 and wk < 18:
            evs += (get(f"{ESPN}/{path}/scoreboard", params={"week": wk + 1, "seasontype": 2}) or {}).get("events", [])
    else:
        et = now - timedelta(hours=5)
        for d in range(0, 3):
            day = (et + timedelta(days=d)).strftime("%Y%m%d")
            evs += (get(f"{ESPN}/{path}/scoreboard", params={"dates": day}) or {}).get("events", [])
    games = {}
    for e in evs:
        c = e["competitions"][0]
        st = c.get("status", e.get("status", {})).get("type", {})
        t = parse_t(e["date"])
        if not t or t < now - timedelta(hours=8) or t > now + timedelta(days=8):
            continue
        side = {}
        for tm in c["competitors"]:
            T = tm["team"]
            rec = ((tm.get("records") or [{}])[0] or {}).get("summary", "")
            side[tm["homeAway"]] = {"id": T["id"], "abbr": T["abbreviation"], "name": T.get("displayName"), "short": T.get("shortDisplayName") or T.get("name"),
                                    "logo": T.get("logo", ""), "color": T.get("color"), "record": rec, "score": num(tm.get("score"))}
        odds = (c.get("odds") or [{}])[0] if c.get("odds") else {}
        v = c.get("venue") or {}
        games[e["id"]] = {"id": e["id"], "sport": sport, "start": e["date"][:16] + ":00", "name": e.get("shortName"),
                          "home": side.get("home"), "away": side.get("away"), "state": st.get("state"), "status": st.get("shortDetail"),
                          "completed": st.get("completed", False),
                          "venue": {"name": v.get("fullName"), "city": (v.get("address") or {}).get("city"), "state": (v.get("address") or {}).get("state"),
                                    "country": (v.get("address") or {}).get("country"), "indoor": bool(v.get("indoor")) or sport == "NBA"},
                          "neutral": c.get("neutralSite", False), "spreadTxt": odds.get("details"), "ou": num(odds.get("overUnder")),
                          "espnWeather": e.get("weather") or c.get("weather")}
    log(f"[espn] {sport}: {len(games)} upcoming games")
    return games


def vegas_margin(g):
    """Home margin implied by ESPN odds string like 'BUF -2.5'."""
    s = g.get("spreadTxt") or ""
    m = re.match(r"\s*([A-Z]+)\s+([-+]?\d+(?:\.\d+)?)", s)
    if not m:
        return 0.0 if s.upper().startswith("EVEN") else None
    ab, v = m.group(1), float(m.group(2))
    home = g["home"]["abbr"]
    sport = g["sport"]
    return -v if ab in abbr_set(sport, home) else v


def standings(sport):
    d = get(f"{ESPN_STAND}/{ESPN_PATH[sport]}/standings") or {}
    out = {}

    def walk(node):
        for ch in node.get("children") or []:
            walk(ch)
        for e in (node.get("standings") or {}).get("entries") or []:
            st = {x.get("name") or x.get("type"): x.get("value") for x in e.get("stats", [])}
            st.update({x.get("name") + "_s": x.get("displayValue") for x in e.get("stats", []) if x.get("name")})
            w, l = st.get("wins") or 0, st.get("losses") or 0
            gp = w + l + (st.get("ties") or 0)
            pf = st.get("avgPointsFor") or ((st.get("pointsFor") or 0) / gp if gp else None)
            pa = st.get("avgPointsAgainst") or ((st.get("pointsAgainst") or 0) / gp if gp else None)
            out[e["team"]["abbreviation"]] = {"id": e["team"]["id"], "w": w, "l": l, "gp": gp, "pf": pf, "pa": pa,
                                              "pct": st.get("winPercent"), "home": st.get("Home_s") or st.get("home_s"), "road": st.get("Road_s") or st.get("road_s")}
    walk(d)
    # ranks (1 = best)
    for k, rev in (("pf", True), ("pa", False), ("pct", True)):
        vals = sorted([t for t in out.values() if t.get(k) is not None], key=lambda t: t[k], reverse=rev)
        for i, t in enumerate(vals):
            t[k + "Rank"] = i + 1
    log(f"[espn] {sport}: standings for {len(out)} teams")
    return out


def teams_list(sport):
    tl = get(f"{ESPN}/{ESPN_PATH[sport]}/teams", params={"limit": 100}) or {}
    return [x["team"] for x in ((tl.get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams", [])]


def rosters(sport, teams):
    def one(t):
        r = get(f"{ESPN}/{ESPN_PATH[sport]}/teams/{t['id']}/roster") or {}
        ath = r.get("athletes") or []
        if ath and isinstance(ath[0], dict) and "items" in ath[0]:
            ath = [a for grp in ath for a in grp.get("items", [])]
        return t, ath
    out = {}
    for t, ath in pmap(one, teams, 8):
        for a in ath:
            inj = (a.get("injuries") or [{}])
            inj = (inj[0] or {}) if inj else {}
            out[a["id"]] = {"id": a["id"], "name": a.get("fullName") or a.get("displayName"), "nn": norm_name(a.get("fullName") or a.get("displayName")),
                            "team": t["abbreviation"], "teamId": t["id"], "pos": (a.get("position") or {}).get("abbreviation", ""),
                            "posName": (a.get("position") or {}).get("displayName", ""),
                            "img": (a.get("headshot") or {}).get("href") or f"https://a.espncdn.com/i/headshots/{sport.lower()}/players/full/{a['id']}.png",
                            "ht": a.get("displayHeight"), "wt": a.get("displayWeight"), "age": a.get("age"), "jersey": a.get("jersey"),
                            "college": (a.get("college") or {}).get("name"), "exp": (a.get("experience") or {}).get("years"),
                            "inj": inj.get("status"), "injDetail": (inj.get("details") or {}).get("type") if isinstance(inj.get("details"), dict) else inj.get("shortComment")}
    log(f"[espn] {sport}: {len(out)} rostered players")
    return out


def team_schedule(sport, tid):
    d = get(f"{ESPN}/{ESPN_PATH[sport]}/teams/{tid}/schedule") or {}
    out = []
    for e in d.get("events") or []:
        c = e["competitions"][0]
        me = next((x for x in c["competitors"] if x["team"]["id"] == str(tid)), None)
        if not me:
            continue
        addr = (c.get("venue") or {}).get("address") or {}
        out.append({"id": e["id"], "t": e["date"], "home": me["homeAway"] == "home", "city": addr.get("city"), "state": addr.get("state"),
                    "country": addr.get("country"), "done": ((c.get("status") or {}).get("type") or {}).get("completed", False)})
    return sorted(out, key=lambda x: x["t"])


# ---------- box scores (cached forever once final) ----------
def box_summary(sport, eid):
    d = get(f"{ESPN}/{ESPN_PATH[sport]}/summary", params={"event": eid}) or {}
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    done = ((comp.get("status") or {}).get("type") or {}).get("completed", False)
    teams = {}
    score = {}
    for c in comp.get("competitors") or []:
        score[c["team"]["abbreviation"]] = num(c.get("score"))
    for team in (d.get("boxscore") or {}).get("players", []):
        ab = team["team"]["abbreviation"]
        pl = {}
        for cat in team.get("statistics", []):
            labels = cat.get("labels") or cat.get("keys") or []
            nm = cat.get("name") or "box"
            for a in cat.get("athletes", []):
                aid = (a.get("athlete") or {}).get("id")
                if aid:
                    pl.setdefault(aid, {"n": (a.get("athlete") or {}).get("displayName")})[nm] = dict(zip(labels, a.get("stats") or []))
        teams[ab] = pl
    return {"done": done, "score": score, "teams": teams, "date": (comp.get("date") or "")[:10]}


def season_event_ids(sport, now):
    path = ESPN_PATH[sport]
    ids = {}
    if sport == "NFL":
        sb = get(f"{ESPN}/{path}/scoreboard") or {}
        wk = (sb.get("week") or {}).get("number") or 1
        for w in range(1, wk + 1):
            for e in (get(f"{ESPN}/{path}/scoreboard", params={"week": w, "seasontype": 2}) or {}).get("events", []):
                if e["status"]["type"]["completed"]:
                    ids[e["id"]] = e["date"][:10]
    else:
        start = datetime(now.year if now.month >= 10 else now.year - 1, 10, 15, tzinfo=timezone.utc)
        d = start
        days = []
        while d < now:
            days.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        for sb in pmap(lambda day: get(f"{ESPN}/{path}/scoreboard", params={"dates": day}) or {}, days, 8):
            for e in sb.get("events", []):
                if e["status"]["type"]["completed"] and (e.get("season") or {}).get("type") in (2, None):
                    ids[e["id"]] = e["date"][:10]
    return ids


def box_value(sport, key, st):
    """Final stat for a player's box-score dict; 'dnp' if he did not play."""
    if sport == "NBA":
        r = next((v for v in st.values() if isinstance(v, dict) and "PTS" in v), None)
        if not r or str(r.get("MIN", "")).strip() in ("", "0", "--", "DNP"):
            return "dnp"
        P, R, A, S, B = (num(r.get(k)) for k in ("PTS", "REB", "AST", "STL", "BLK"))
        vals = {"points": P, "rebounds": R, "assists": A, "threes": split_num(r.get("3PT"), 0), "steals": S, "blocks": B,
                "turnovers": num(r.get("TO")), "min": num(r.get("MIN")), "fga": split_num(r.get("FG"), 1),
                "fta": split_num(r.get("FT"), 1), "oreb": num(r.get("OREB"))}
        if None not in (P, R, A, S, B):
            vals.update({"pra": P + R + A, "pts_reb": P + R, "pts_ast": P + A, "reb_ast": R + A, "stl_blk": S + B})
        return vals if key is None else vals.get(key)
    pa, ru, rc = st.get("passing", {}), st.get("rushing", {}), st.get("receiving", {})
    de, ki = st.get("defensive", {}), st.get("kicking", {})

    def z(d, k):
        v = num(d.get(k))
        return v if v is not None else 0.0
    vals = {"pass_yds": z(pa, "YDS"), "pass_tds": z(pa, "TD"), "ints": z(pa, "INT"),
            "pass_cmp": split_num(pa.get("C/ATT"), 0) or 0.0, "pass_att": split_num(pa.get("C/ATT"), 1) or 0.0,
            "rush_yds": z(ru, "YDS"), "rush_att": z(ru, "CAR"), "rec_yds": z(rc, "YDS"), "receptions": z(rc, "REC"),
            "targets": z(rc, "TGTS"), "long_rec": z(rc, "LONG"), "anytime_td": z(ru, "TD") + z(rc, "TD"),
            "tackles": z(de, "TOT"), "sacks": z(de, "SACKS"), "pd": z(de, "PD"), "fg_made": split_num(ki.get("FG"), 0) or 0.0, "kick_pts": z(ki, "PTS")}
    vals["rush_rec_yds"] = vals["rush_yds"] + vals["rec_yds"]
    vals["pass_rush_yds"] = vals["pass_yds"] + vals["rush_yds"]
    return vals if key is None else vals.get(key)


# ---------- player game logs ----------
GL_MAP = {
    "points": "points", "totalRebounds": "rebounds", "assists": "assists", "steals": "steals", "blocks": "blocks",
    "turnovers": "turnovers", "minutes": "min", "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "threes",
    "passingYards": "pass_yds", "passingTouchdowns": "pass_tds", "completions": "pass_cmp", "passingAttempts": "pass_att",
    "rushingYards": "rush_yds", "rushingAttempts": "rush_att", "rushingTouchdowns": "rush_td",
    "receptions": "receptions", "receivingTargets": "targets", "receivingYards": "rec_yds",
    "receivingTouchdowns": "rec_td", "longReception": "long_rec", "totalTackles": "tackles",
    "fieldGoalsMade-fieldGoalAttempts": "fg_made", "totalKickingPoints": "kick_pts",
}


def seasons(sport, now):
    y = (now.year + 1 if now.month >= 9 else now.year) if sport == "NBA" else (now.year if now.month >= 7 else now.year - 1)
    return [y, y - 1, y - 2]


def gamelog(sport, aid, now):
    games = {}
    for yr in seasons(sport, now):
        g = get(f"{ESPN_WEB}/sports/{ESPN_PATH[sport]}/athletes/{aid}/gamelog", params={"season": yr})
        if not g:
            continue
        names = g.get("names") or []
        is_qb = "QBRating" in names
        meta = g.get("events") or {}
        for stp in g.get("seasonTypes") or []:
            label = stp.get("displayName", "")
            if "regular" not in label.lower() and "postseason" not in label.lower():
                continue
            for cat in stp.get("categories") or []:
                if cat.get("type") != "event":
                    continue
                for e in cat.get("events") or []:
                    eid = e.get("eventId")
                    m = meta.get(eid) or {}
                    s = {}
                    for n, v in zip(names, e.get("stats") or []):
                        if n == "interceptions":
                            s["ints"] = num(v)
                        elif n == "sacks":
                            if not is_qb:
                                s["sacks"] = num(v)
                        elif n == "fieldGoalsMade-fieldGoalsAttempted":
                            s["fga"] = split_num(v, 1)
                        elif n in GL_MAP:
                            s[GL_MAP[n]] = split_num(v, 0) if "-" in n else num(v)
                    if sport == "NBA":
                        try:
                            P, R, A, S, B = (s.get(x) for x in ("points", "rebounds", "assists", "steals", "blocks"))
                            s.update({"pra": P + R + A, "pts_reb": P + R, "pts_ast": P + A, "reb_ast": R + A, "stl_blk": S + B})
                        except TypeError:
                            pass
                    elif "tackles" not in s:
                        s["rush_rec_yds"] = (s.get("rush_yds") or 0) + (s.get("rec_yds") or 0)
                        s["pass_rush_yds"] = (s.get("pass_yds") or 0) + (s.get("rush_yds") or 0)
                        s["anytime_td"] = (s.get("rush_td") or 0) + (s.get("rec_td") or 0)
                    opp = m.get("opponent") or {}
                    games[eid] = {"id": eid, "date": (m.get("gameDate") or "")[:10], "opp": opp.get("abbreviation", ""),
                                  "home": m.get("atVs") != "@", "res": m.get("gameResult", ""), "score": m.get("score", ""),
                                  "season": yr, "post": "post" in label.lower(),
                                  "s": {k: v for k, v in s.items() if v is not None}}
    return sorted(games.values(), key=lambda x: x["date"], reverse=True)


# ======================================================================================
# Weather (Open-Meteo, free) + city coordinates for travel distance
# ======================================================================================
CITY = {  # lat, lon for every NFL/NBA city (+ common international sites)
    "Arlington": (32.75, -97.09), "Atlanta": (33.75, -84.39), "Baltimore": (39.28, -76.62), "Orchard Park": (42.77, -78.79),
    "Buffalo": (42.89, -78.88), "Charlotte": (35.23, -80.84), "Chicago": (41.88, -87.63), "Cincinnati": (39.10, -84.51),
    "Cleveland": (41.50, -81.69), "Denver": (39.74, -104.99), "Detroit": (42.33, -83.05), "Green Bay": (44.50, -88.06),
    "Houston": (29.76, -95.37), "Indianapolis": (39.77, -86.16), "Jacksonville": (30.33, -81.66), "Kansas City": (39.10, -94.58),
    "Las Vegas": (36.17, -115.14), "Paradise": (36.09, -115.18), "Inglewood": (33.95, -118.34), "Los Angeles": (34.05, -118.24),
    "Miami": (25.76, -80.19), "Miami Gardens": (25.96, -80.24), "Minneapolis": (44.98, -93.27), "Foxborough": (42.09, -71.26),
    "Boston": (42.36, -71.06), "New Orleans": (29.95, -90.07), "East Rutherford": (40.81, -74.07), "New York": (40.71, -74.01),
    "Brooklyn": (40.68, -73.98), "Philadelphia": (39.95, -75.17), "Pittsburgh": (40.44, -80.00), "Santa Clara": (37.40, -121.97),
    "San Francisco": (37.77, -122.42), "Seattle": (47.61, -122.33), "Tampa": (27.95, -82.46), "Nashville": (36.16, -86.78),
    "Landover": (38.91, -76.86), "Washington": (38.91, -77.04), "Glendale": (33.53, -112.26), "Phoenix": (33.45, -112.07),
    "Dallas": (32.78, -96.80), "Milwaukee": (43.04, -87.91), "Memphis": (35.15, -90.05), "Oklahoma City": (35.47, -97.52),
    "Orlando": (28.54, -81.38), "Portland": (45.52, -122.68), "Sacramento": (38.58, -121.49), "San Antonio": (29.42, -98.49),
    "Salt Lake City": (40.76, -111.89), "Toronto": (43.65, -79.38), "Auburn Hills": (42.69, -83.23), "San Diego": (32.72, -117.16),
    "London": (51.51, -0.13), "Munich": (48.14, 11.58), "Frankfurt": (50.11, 8.68), "Mexico City": (19.43, -99.13),
    "Sao Paulo": (-23.55, -46.63), "São Paulo": (-23.55, -46.63), "Madrid": (40.42, -3.70), "Berlin": (52.52, 13.40),
    "Dublin": (53.35, -6.26), "Paris": (48.86, 2.35), "Melbourne": (-37.81, 144.96), "Rio de Janeiro": (-22.91, -43.17),
    "Abu Dhabi": (24.45, 54.38), "Inglewood ": (33.95, -118.34), "San Francisco ": (37.77, -122.42),
}


def coords(city, geo_cache):
    if not city:
        return None
    if city in CITY:
        return CITY[city]
    if city in geo_cache:
        return tuple(geo_cache[city]) if geo_cache[city] else None
    d = get(GEO, params={"name": city, "count": 1}) or {}
    r = (d.get("results") or [None])[0]
    geo_cache[city] = [r["latitude"], r["longitude"]] if r else None
    return tuple(geo_cache[city]) if r else None


def miles(a, b):
    if not a or not b:
        return 0.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def weather(lat, lon, when):
    d = get(METEO, params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m,precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m,weather_code",
                           "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch", "timezone": "UTC", "forecast_days": 16})
    if not d:
        return None
    h = d.get("hourly") or {}
    target = when.strftime("%Y-%m-%dT%H:00")
    times = h.get("time") or []
    if target not in times:
        return None
    i = times.index(target)
    idx = [j for j in range(i, min(i + 3, len(times)))]  # kickoff + ~3 hours

    def avg(k):
        vals = [h[k][j] for j in idx if h.get(k) and h[k][j] is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def mx(k):
        vals = [h[k][j] for j in idx if h.get(k) and h[k][j] is not None]
        return max(vals) if vals else None
    code = h.get("weather_code", [None] * len(times))[i]
    return {"temp": avg("temperature_2m"), "wind": avg("wind_speed_10m"), "gust": mx("wind_gusts_10m"),
            "rainPct": mx("precipitation_probability"), "precip": mx("precipitation"), "code": code, "src": "Open-Meteo"}
