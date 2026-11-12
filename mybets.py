"""Absinthe Edge: My Bets. Pulls your open Kalshi positions and grades each one against the model.

Needs two GitHub repo secrets (Settings > Secrets and variables > Actions):
  KALSHI_API_KEY_ID   the key ID from kalshi.com > Account > API keys
  KALSHI_PRIVATE_KEY  the full contents of the .key/.pem file Kalshi gave you
If they are missing this script does nothing, so the rest of the board still builds.

Writes data/mybets.json.
"""
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.elections.kalshi.com"
BASE = "/trade-api/v2"
DATA = "data"
OUT = f"{DATA}/mybets.json"

# Kalshi team codes -> the ESPN codes the board uses
KAL2ESPN = {
    "NBA": {"GSW": "GS", "SAS": "SA", "NOP": "NO", "NYK": "NY", "WAS": "WSH", "UTA": "UTAH", "PHO": "PHX"},
    "NFL": {"WAS": "WSH", "JAC": "JAX"},
}


# ---------------------------------------------------------------- Kalshi API
def _signer():
    key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
    pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not key_id or not pem:
        return None
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding

    if "-----BEGIN" not in pem:  # allow the key pasted without header lines
        pem = "-----BEGIN PRIVATE KEY-----\n" + pem + "\n-----END PRIVATE KEY-----"
    key = serialization.load_pem_private_key(pem.encode(), password=None)

    def headers(method, path):
        ts = str(int(time.time() * 1000))
        msg = (ts + method + path).encode()
        if isinstance(key, ed25519.Ed25519PrivateKey):  # newer Kalshi keys
            sig = key.sign(msg)
        else:  # RSA keys
            sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                           hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
    return headers


def _get(path, params=None, sign=None):
    full = BASE + path
    h = sign("GET", full) if sign else {}
    r = requests.get(API + full, params=params, headers=h, timeout=20)
    r.raise_for_status()
    return r.json()


_mcache = {}
def market(ticker):
    if ticker not in _mcache:
        try:
            _mcache[ticker] = _get(f"/markets/{ticker}").get("market", {})
        except Exception:
            _mcache[ticker] = {}
    return _mcache[ticker]


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- model lookups
def load_board():
    try:
        with open(f"{DATA}/board.json") as fh:
            return json.load(fh)
    except Exception:
        return {}


def index_board(board):
    by_ticker, games = {}, []
    for sport, s in (board.get("sports") or {}).items():
        for p in (s.get("picks") or []) + (s.get("gamePicks") or []):
            t = p.get("ticker")
            if t and t not in by_ticker and p.get("prob") is not None:
                by_ticker[t] = p
        for g in s.get("games") or []:
            games.append(g)
    return by_ticker, games


def _parse(t):
    try:
        return datetime.fromisoformat(t.replace("Z", "")).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def find_game(ticker, games):
    """KXNBAGAME-26NOV11DENMEM-DEN -> the board game with that team on that (US) date."""
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    series, mid = parts[0], parts[1]
    sport = "NBA" if "NBA" in series else "NFL" if "NFL" in series else None
    if not sport:
        return None
    try:
        day = datetime.strptime(mid[:7].title(), "%y%b%d").date()
    except ValueError:
        return None
    teams = mid[7:]
    for g in games:
        if g.get("sport") != sport:
            continue
        st = _parse(g.get("start", ""))
        if not st or (st - timedelta(hours=5)).date() != day:
            continue
        rev = {v: k for k, v in KAL2ESPN.get(sport, {}).items()}
        ak, hk = rev.get(g["away"]["abbr"], g["away"]["abbr"]), rev.get(g["home"]["abbr"], g["home"]["abbr"])
        if teams in (ak + hk, hk + ak):
            return g
    return None


def leg_prob(leg, by_ticker, games, now):
    """Chance this leg hits (for the side you hold), plus where it came from."""
    t, side = leg["market_ticker"], (leg.get("side") or "yes").lower()
    m = market(t)
    res = (m.get("result") or "").lower()
    if res in ("yes", "no"):
        return (1.0 if res == side else 0.0), "settled", ("won" if res == side else "lost"), m
    g = find_game(t, games)
    started = g is not None and (_parse(g.get("start", "")) or now) <= now
    mid = None
    yb, ya = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars"))
    if ya > 0:
        mid = (yb + ya) / 2
    elif m.get("last_price_dollars"):
        mid = f(m.get("last_price_dollars"))
    # once the game is live the pre-game model is stale, so trust the live market
    if started and mid is not None:
        p = mid
        return (p if side == "yes" else 1 - p), "live market", "live", m
    pk = by_ticker.get(t)
    if pk:
        p_yes = pk["prob"] if (pk.get("side") or "YES").upper() == "YES" else 1 - pk["prob"]
        return (p_yes if side == "yes" else 1 - p_yes), "model", "pre", m
    if g and "GAME" in t.split("-")[0] and (g.get("proj") or {}).get("pHome") is not None:
        team = t.split("-")[-1]
        sport = g["sport"]
        team = KAL2ESPN.get(sport, {}).get(team, team)
        p_yes = g["proj"]["pHome"] if team == g["home"]["abbr"] else 1 - g["proj"]["pHome"]
        return (p_yes if side == "yes" else 1 - p_yes), "model", "pre", m
    if mid is not None:
        return (mid if side == "yes" else 1 - mid), "market", "pre", m
    return None, "unknown", "pre", m


# ---------------------------------------------------------------- grading
def grade_bet(pos, by_ticker, games, now):
    t = pos["ticker"]
    n = f(pos.get("position_fp"))
    side = "yes" if n >= 0 else "no"
    n = abs(n)
    cost = f(pos.get("market_exposure_dollars")) or f(pos.get("total_traded_dollars"))
    m = market(t)
    legs_raw = m.get("mve_selected_legs") or [{"market_ticker": t, "side": side, "event_ticker": m.get("event_ticker")}]
    names = [x.strip() for x in (m.get("title") or "").split(",")] if m.get("mve_selected_legs") else []
    legs, fair, unknown = [], 1.0, 0
    for i, lg in enumerate(legs_raw):
        p, src, state, lm = leg_prob(lg, by_ticker, games, now)
        g = find_game(lg["market_ticker"], games)
        label = names[i] if i < len(names) else (lm.get("yes_sub_title") or lm.get("title") or lg["market_ticker"])
        if label.lower().startswith("yes "):
            label = label[4:]
        elif label.lower().startswith("no "):
            label = "NOT " + label[3:]
        legs.append({"ticker": lg["market_ticker"], "label": label, "side": lg.get("side", "yes"),
                     "p": None if p is None else round(p, 3), "src": src, "state": state,
                     "game": g["name"] if g else None, "gameId": g["id"] if g else None,
                     "start": g["start"] if g else None})
        if p is None:
            unknown += 1
        else:
            fair *= p
    if not m.get("mve_selected_legs") and side == "no":
        legs[0]["side"] = "no"
    bid = f(m.get("yes_bid_dollars")) if side == "yes" else f(m.get("no_bid_dollars"))
    last = f(m.get("last_price_dollars"))
    entry = cost / n if n else 0
    lost = any(l["state"] == "lost" for l in legs)
    won = legs and all(l["state"] == "won" for l in legs)
    fair = None if unknown else round(fair, 4)
    left = [l for l in legs if l["state"] not in ("won",)]
    weakest = min((l for l in left if l["p"] is not None), key=lambda l: l["p"], default=None)

    if won:
        status, signal, why = "won", "Won", "every leg hit; it pays out when Kalshi settles"
    elif lost:
        dead = next(l for l in legs if l["state"] == "lost")
        status, signal, why = "lost", "Lost", f"{dead['label']} missed, so the combo can't win"
    elif fair is None:
        status, signal, why = "open", "Hold", "the model has no read on some legs yet"
    else:
        looks_bad = entry and fair < entry * 0.5
        if bid > 0 and bid >= fair:
            signal = "Cash out"
            why = f"you can sell for {bid*100:.1f}¢ and the model thinks it's worth {fair*100:.1f}¢"
        elif bid <= 0:
            signal = "Hold"
            why = "no buyer on Kalshi right now, so there's nothing to cash out yet"
        else:
            signal = "Hold"
            why = f"the model thinks it's worth {fair*100:.1f}¢, more than the {bid*100:.1f}¢ you'd get selling"
        status = "bad" if looks_bad else "good" if entry and fair >= entry else "ok"
        if looks_bad:
            why += f" · down from your {entry*100:.1f}¢ entry"
        if weakest and weakest["p"] < 0.5:
            why += f" · weakest leg: {weakest['label']} ({weakest['p']*100:.0f}%)"
    same_game = len({l['game'] for l in legs if l['game']}) < len([l for l in legs if l['game']])
    return {
        "ticker": t, "event": m.get("event_ticker"), "title": m.get("title") or t,
        "side": side, "contracts": round(n, 2), "cost": round(cost, 2), "fees": round(f(pos.get("fees_paid_dollars")), 2),
        "entry": round(entry, 4), "bid": round(bid, 4), "last": round(last, 4),
        "sellValue": round(bid * n, 2), "payout": round(n, 2),
        "fair": fair, "fairValue": None if fair is None else round(fair * n, 2),
        "status": status, "signal": signal, "why": why, "sameGame": same_game,
        "closes": m.get("expected_expiration_time") or m.get("close_time"),
        "placed": pos.get("last_updated_ts"), "legs": legs,
    }


def main():
    sign = _signer()
    if not sign:
        print("My Bets: no Kalshi API key set (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY), skipping")
        return
    now = datetime.now(timezone.utc)
    board = load_board()
    by_ticker, games = index_board(board)
    out = {"updated": now.isoformat()[:19] + "Z", "bets": [], "error": None}
    try:
        bal = _get("/portfolio/balance", sign=sign)
        out["cash"] = round(f(bal.get("balance")) / 100, 2)
        out["portfolio"] = round(f(bal.get("portfolio_value")) / 100, 2)
        positions, cursor = [], None
        for _ in range(5):
            params = {"limit": 200, "count_filter": "position", "settlement_status": "unsettled"}
            if cursor:
                params["cursor"] = cursor
            d = _get("/portfolio/positions", params=params, sign=sign)
            positions += [p for p in d.get("market_positions") or [] if f(p.get("position_fp"))]
            cursor = d.get("cursor")
            if not cursor:
                break
        for p in positions:
            try:
                out["bets"].append(grade_bet(p, by_ticker, games, now))
            except Exception as e:  # one bad market shouldn't sink the page
                print("My Bets: could not grade", p.get("ticker"), e)
        order = {"Cash out": 0, "Hold": 1, "Won": 2, "Lost": 3}
        out["bets"].sort(key=lambda b: (order.get(b["signal"], 9), b.get("closes") or ""))
    except Exception as e:
        out["error"] = str(e)[:300]
        print("My Bets error:", e)
    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"My Bets: {len(out['bets'])} open bets written to {OUT}")


if __name__ == "__main__":
    main()
