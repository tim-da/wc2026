# wc2026

Local Flask app that compares FIFA World Cup 2026 actual results against Polymarket and Kalshi prediction snapshots.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 outputs/world-cup-tracker/server.py
```

Then open:

```text
http://127.0.0.1:5055
```

The app uses ESPN for live scores and standings, and the saved CSV in `outputs/world_cup_2026_market_odds_polymarket_kalshi.csv` as the baseline prediction snapshot.

## Tests

```bash
python3 -m pip install pytest
python3 -m pytest
```

## Caveat: the knockout bracket is a fixed assumption

The Round-of-32 pairings used for the finals bracket (`LEFT_R32` / `RIGHT_R32` in
`server.py`) are **hardcoded** from a reference image, not derived from live group
standings. Completed matches override the *winner* of a pairing that actually
occurred, but the pairings themselves do not change. If the real knockout bracket
differs from these assumed matchups, the projection will not match reality.
