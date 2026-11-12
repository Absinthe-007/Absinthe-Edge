# Absinthe Edge

NFL + NBA picks for **Kalshi** and **DraftKings Pick6**, with a full projected stat line for every player and a tracker that grades itself.

**Site:** https://absinthe-007.github.io/Absinthe-Edge/

## What it does
- Pulls every Pick6 line and every Kalshi player, winner, spread and total market for upcoming NFL and NBA games.
- Projects **every stat** for each player with a line (points, rebounds, assists, 3s, steals, blocks, PRA... / passing, rushing, receiving yards, receptions, TDs, tackles, kicking).
- Each projection blends season average, last 5 games, home/away split and history vs the opponent (last 3 seasons), then adjusts for:
  - defense vs position (what the opponent allows to that position, from every box score this season) and its rank
  - Vegas implied team total (game script)
  - weather at open-air NFL stadiums (wind, rain, cold, from Open-Meteo); domes are neutral
  - rest days, back-to-backs, road-trip length, travel miles, coming off a bye
  - injury status
- Turns projections into hit chances, compares them with the Pick6 break-even or the Kalshi price, and flags the best plays.
- Projects every game's score, win chance, spread and total.
- **Self-checking:** saves every pick and projection before the game, grades them against the ESPN box score afterward, and shows the record by platform, bet type, stat and confidence, plus calibration and projection error. Once a stat has 30+ graded picks, the model rescales its own confidence for that stat.

## V3.2: combos
Tap **+ Combo** on any Kalshi pick or team winner to build a combo slip. It shows the model's combined chance and warns when legs share a game. **Get Kalshi price** asks Kalshi's market makers for a live quote; **Buy** takes it, if it hasn't moved up more than 2¢. The worker learns how Kalshi prices combo quotes from your own past combo fills, and refuses combos until it can confirm that.

## V3.1: one-click Kalshi orders
The `worker/` folder holds a Cloudflare Worker that places Kalshi orders for you. Your Kalshi key lives only in the worker's encrypted secrets, never on this public site. Setup steps are in `worker/SETUP.md`. Once it's connected on the My Bets tab, every Kalshi pick, team winner and line gets a ⚡ Buy button. The button opens an order ticket, and nothing is bought until you press the confirm button.

## V3.0
- **More sources behind projections:** nflverse (snap share, target and air-yards share, red-zone looks and expected TDs, Next Gen Stats), the official NFL injury report with Wed–Fri practice status, ESPN depth charts, and the official NBA injury report.
- **Sportsbook consensus (optional):** add a free key from the-odds-api.com as the repo secret `ODDS_API_KEY` (Settings → Secrets and variables → Actions). The board then shows the median sportsbook line and de-vigged chance next to each pick. The free plan's 500 credits a month are spread out automatically, soonest games first.
- **Market Check:** every pick shows the model, Kalshi and the books on one bar. When the model is 20+ points away from the market, the tier drops one step.
- **Grading:** Kalshi picks are graded by Kalshi's own settlement and cross-checked with ESPN; disagreements are flagged and left out of the record. Pick6 picks and stat projections still use the ESPN box score.
- Files: `feeds.py` holds the new data feeds.
- Versioning: every update bumps the version (small batches +0.1, big overhauls to the next whole number).

## V1.1
- Redesigned dashboard: trading-card picks with team colors and player photos, grouped by game, phone layout with bottom tabs.
- Lock / Strong / Lean tiers, last-10 sparklines, A–F matchup grades, over/under streaks, line movement.
- Player rest and workload (days off, missed games, minutes or touches), usage trends (minutes, shots, target share, carries).
- Teammate injuries (usage boost), starting QB out, opposing defenders out, NBA pace.
- Files: `extras.py` holds the new context features.

## How it updates
`.github/workflows/update.yml` runs `run.py` every 30 minutes. You can also run it any time with **Actions → Update board → Run workflow**. The site is published with GitHub Pages (Settings → Pages → Source: **GitHub Actions**). The accuracy history is carried from run to run by reading the last published `data/track.json`.

No API keys needed and no cost. The Odds API key is optional.

## Files
- `sources.py`: Pick6, Kalshi, ESPN and Open-Meteo scrapers
- `model.py`: projections, defense vs position, game model, Kalshi pricing
- `extras.py`: rest, injuries, usage, pace, tiers, streaks, matchup grades
- `feeds.py`: nflverse, depth charts, NFL/NBA injury reports, Kalshi settlements, Odds API
- `run.py`: builds `data/board.json`, `data/players.json` and grades `data/track.json`
- `index.html`: the dashboard

Estimates only, not betting advice. 21+. Gambling problem? 1-800-GAMBLER.
