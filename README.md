# World Cup Market Tracker

Local Flask app that compares FIFA World Cup 2026 actual results against Polymarket and Kalshi prediction snapshots.

## Run

```bash
/Users/dmitrytimoshin/anaconda3/bin/python outputs/world-cup-tracker/server.py
```

Then open:

```text
http://127.0.0.1:5055
```

The app uses ESPN for live scores and standings, and the saved CSV in `outputs/world_cup_2026_market_odds_polymarket_kalshi.csv` as the baseline prediction snapshot.
