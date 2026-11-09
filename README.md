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

## V1.1
- Redesigned dashboard: trading-card picks with team colors and player photos, grouped by game, phone layout with bottom tabs.
- Lock / Strong / Lean tiers, last-10 sparklines, A–F matchup grades, over/under streaks, line movement.
- Player rest and workload (days off, missed games, minutes or touches), usage trends (minutes, shots, target share, carries).
- Teammate injuries (usage boost), starting QB out, opposing defenders out, NBA pace.
- Files: `extras.py` holds the new context features.

## How it updates
`.github/workflows/update.yml` runs `run.py` every 2 hours, plus hourly from 15:00–23:00 UTC before US game times. You can also run it any time with **Actions → Update board → Run workflow**. The site is published with GitHub Pages (Settings → Pages → Source: **GitHub Actions**). The accuracy history is carried from run to run by reading the last published `data/track.json`.

No API keys, no cost.

## Files
- `sources.py`: Pick6, Kalshi, ESPN and Open-Meteo scrapers
- `model.py`: projections, defense vs position, game model, Kalshi pricing
- `extras.py`: rest, injuries, usage, pace, tiers, streaks, matchup grades
- `run.py`: builds `data/board.json`, `data/players.json` and grades `data/track.json`
- `index.html`: the dashboard

Estimates only, not betting advice. 21+. Gambling problem? 1-800-GAMBLER.
