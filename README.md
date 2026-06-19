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

Current Polymarket and Kalshi outright odds drive the dashboard, team table, consensus bars, and knockout projection. Prediction performance uses the latest match-market odds captured before kickoff; when no match market is available, it falls back to the saved June 13 outright snapshot.

The pre-match lock file and latest generated-bracket state can be placed on persistent storage with:

```bash
export WC_MATCH_BASELINE_PATH=/persistent/path/match-market-baseline.json
export WC_BRACKET_STATE_PATH=/persistent/path/bracket-generation-state.json
```

## Tests

```bash
python3 -m pip install pytest
python3 -m pytest
```

## Knockout projection

Before official knockout fixtures are known, the Round-of-32 participants come
from the supplied reference image and winners are selected by current outright
odds. ESPN's official knockout fixtures replace those guesses as they become
known, and completed knockout results override the market picks.
