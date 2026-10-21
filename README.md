# Poker Database — UAPI v3.8

Autonomous poker analytics stack over a  PostgreSQL database. Generates per-player leak reports across 69 statistical situations, with full population scanning.

## Stack

- **Database**: PostgreSQL 
- **Library**: `pt4_tools.py` — SQL helpers, card decoding, texture classification, sizing buckets
- **Report engine**: `generate_full_report.py` — 69 `sit_N` functions covering preflop, postflop, sizing, and positional metrics
- **Population scanner**: `population_scanner.py` — runs full reports across all players with ≥500 hands

## Setup

1. Copy `.env.example` to `.env` and fill in your PT4 database credentials:

```
DB_NAME=PT4 DB
DB_USER=postgres
DB_PASS=yourpassword
DB_HOST=localhost
DB_PORT=5432
```

2. Install dependencies:

```bash
pip install psycopg2-binary
```

## Usage

**Single player report:**

```bash
python generate_full_report.py xxx
```

Saves to `reports/xxx_FULL.md`. Returns a dict keyed by `sit_N` with `value`, `n`, `status`, and `details`.

**Population scan:**

```bash
python population_scanner.py
```

Scans all players with ≥500 hands and writes a population summary.

## Situations covered

69 situations across:

| Category | Examples |
|----------|---------|
| Preflop fundamentals | VPIP, PFR, 3Bet, Fold to 3Bet, Steal, Limp |
| Postflop aggression | CBet flop/turn/river, Probe bet, Donk bet |
| Defense | Fold to CBet, Fold to River Bet, MDF, Check-Raise |
| Sizing | Turn sizing, River sizing, Sizing tell profit |
| Positional | IP vs OOP, Cold caller OOP, 4-Bettor postflop |
| Board texture | High/Low flop, Connected/Disconnected, Monotone |
| Multiway | Multiway pot stats, Limp pot |
| Profitability | BB/100 by position, Profit by pot type, W$SD, WTSD, WWSF |

## Architecture

```
tools.py              # Core library — DB connection, card decode, texture/sizing helpers
generate_full_report.py   # 69 sit_N functions → unified report per player
population_scanner.py     # Batch runner over all players
reports/                  # Generated Markdown reports (gitignored)
tasks/                    # Session todo and lessons log
```

## Schema notes

- PT4 statistics-only schema (no `cash_hand_player_detail` table)
- Sizing data sourced from `val_f_bet_facing_pct` in `cash_hand_player_statistics`
- Board texture reconstructed from `card_1`–`card_5` in `cash_hand_summary`
- `cnt_players_f` lives on `cash_hand_summary`, not `cash_hand_player_statistics`
- `amt_won` is already net (no need to subtract `amt_bet`)
- BB excluded from Fold to 3-Bet; IP excluded from Check-Raise by definition

## Sample size thresholds

| Street | Minimum n | Flag |
|--------|-----------|------|
| Preflop | 100 | — |
| Postflop | 20 | — |
| Any | < 10 | Variance Alert |
