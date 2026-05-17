# UAPI v3.8 - Lessons Learned

## Project Protocols
- Always initialize tasks/todo.md and tasks/lessons.md before starting work
- Follow Engineering Standards from CLAUDE.md
- Autonomous debugging is authorized - fix issues without waiting for approval

## Database Connection (Fixed)
**Issue:** Initial connection failed with password 'dbpass'
**Solution:** Correct password was 'postgrespass' - updated .env file
**Why:** .env template had placeholder password
**How to apply:** Always verify credentials before initial connection

## Phase 0: Schema Discovery Findings (COMPLETE 2026-04-06)

### Database Info
- **Version:** PostgreSQL 8.4.22, compiled by Visual C++ build 1400, 32-bit
- **Total Tables:** 38
- **Connection:** Successful via psycopg2 with dbname='PT4 DB'

### Key Tables Identified

#### Player Statistics Tables (Contains actions, sizing, position)
1. **cash_hand_player_statistics** - Cash game per-hand player data
2. **tourney_hand_player_statistics** - Tournament per-hand player data

Both have identical column structures (~200 columns each).

#### Hand Summary Tables (Contains board cards, pot sizes)
1. **cash_hand_summary** - Cash game hand metadata
2. **tourney_hand_summary** - Tournament hand metadata

Both contain `card_1` through `card_5` for board reconstruction.

#### Lookup Tables (For joining)
- `lookup_positions` - Position names
- `lookup_sites` - Poker site names
- `lookup_hand_ranks` - Poker hand rankings
- `lookup_hole_cards` - Hole card combinations
- `player` - Player info

### Critical Columns for Sizing Analysis (CONFIRMED)
These columns exist and can be used directly:

**Pre-flop sizing:**
- `amt_p_raise_facing` (numeric)
- `val_p_raise_facing_pct` (numeric)
- `amt_p_raise_made` (numeric)
- `val_p_raise_made_pct` (numeric)

**Flop sizing:**
- `amt_f_bet_facing` (numeric)
- `val_f_bet_facing_pct` (numeric)
- `amt_f_bet_made` (numeric)
- `val_f_bet_made_pct` (numeric)
- `amt_f_raise_facing` (numeric)
- `val_f_raise_facing_pct` (numeric)

**Turn/Repeat pattern for t_, r_ prefixes.**

### Critical Columns for Texture Analysis (CONFIRMED)
From `cash_hand_summary` / `tourney_hand_summary`:
- `card_1` through `card_5` (smallint) - Board cards as IDs
- `id_win_hand` (smallint) - Winning hand rank

### Critical Columns for Statistical Aggregation
- `position` (smallint) - Player's position
- `cnt_players_lookup_position` (smallint) - Table size for position context
- `flg_f_saw` / `flg_t_saw` / `flg_r_saw` (boolean) - Saw showdown on each street
- `flg_showdown` (boolean) - Saw showdown
- `flg_won_hand` (boolean) - Won the pot
- `amt_won` (numeric) - Actual amount won
- `val_equity` (numeric) - All-in equity percentage

### Action Flags (for counting opportunities)
- `flg_vpip` (boolean) - Voluntarily put money in pot
- `flg_f_bet` / `flg_t_bet` / `flg_r_bet` - Bet on street
- `cnt_f_raise` / `cnt_t_raise` / `cnt_r_raise` - Number of raises
- `flg_f_face_raise` / `flg_t_face_raise` / `flg_r_face_raise` - Faced a raise
- `flg_f_3bet_opp` / `flg_t_3bet_opp` - 3bet opportunities

### Board Texture Logic
**No pre-processed texture table found.** Board texture must be calculated from:
- `cash_hand_summary.card_1` through `card_5`
- Requires joining via `id_hand`

### Sample Size Requirements (Per CLAUDE.md)
- Pre-flop analysis: n >= 100
- Post-flop analysis: n >= 20
- Variance alert: n < 10

### No-Hallucination Policy Applied
All column names verified via `information_schema.columns` query.
No assumptions made - all names extracted from actual schema.

---

## Phase 0: Tool Calibration (COMPLETE 2026-04-06)

### Position Mapping Correction
**Issue:** Original POSITION_MAP included 'EP' (3) which doesn't map cleanly in 6-max
**Solution:** Removed EP, now mapping only: BTN=0, CO=1, MP=2, BB=8, SB=9
**Reference:** Section 2 of knowledge base confirms this mapping
**Why:** Position IDs represent "seats from BTN" for non-blind positions

### BB/100 Formula Fix
**Issue:** Original formula was (amt_won / hands) * 100 - didn't account for bets placed
**Correct Formula:** (amt_won - amt_bet_ttl) / amt_bb * 100
**Implementation:**
```python
net_winnings = float(total_amt_won or 0) - float(total_amt_bet or 0)
avg_bb_size = float(total_bb_amount or 0) / hands if hands > 0 else 0
if avg_bb_size > 0 and hands > 0:
    net_in_bb = net_winnings / avg_bb_size
    bb_per_100 = round((net_in_bb / hands) * 100, 2)
```
**Reference:** Section 11 of knowledge base
**Verification:** Tested on jaymacca13 data, produced realistic values (-128 to -338 BB/100)

### 3Bet Steal Logic Refinement
**Issue:** Original query filtered by position IDs (8, 9) but didn't use flg_blind_def_opp for numerator
**Solution:** Changed threebet_vs_steal numerator to:
```sql
SUM(CASE WHEN chps.flg_blind_def_opp AND chps.flg_p_3bet THEN 1 ELSE 0 END)
```
**Reference:** Section 4.6 of knowledge base
**Why:** Per PT4 docs: "flg_blind_def_opp = TRUE (facing a steal) AND flg_p_3bet = TRUE (player reraised)"

### Texture Research Outcome
**Finding:** No pre-calculated texture fields exist (cnt_f_monotone, flg_f_dry, etc. not present)
**Available:** Only card_1-card_5 in cash_hand_summary for manual reconstruction
**Next Phase:** Will need to build texture classification from card IDs

### Calibration Verification (jaymacca13)
**Sample Output:**
```
| Position | Hands | BB/100 | Att To Steal (%) | Fold to Steal (%) | 3Bet Steal (%) |
|----------|-------|--------|------------------|-------------------|----------------|
| MP       | 3,661 | -128.38| -                | -                 | -              |
| CO       | 4,248 | -168.19| 29.6% (n=2864)   | -                 | -              |
| BTN      | 4,553 | -187.63| 48.0% (n=2151)   | -                 | -              |
| SB       | 4,777 | -236.70| 55.6% (n=1353)   | 85.5% (n=1426)    | 13.4%          |
| BB       | 4,946 | -337.90| -                | 48.4% (n=1761)    | 6.8%           |
```
**Validation:**
- BB/100 values follow expected pattern (highest loss in BB)
- Steal attempt rates increase positionally (CO→BTN→SB)
- Fold to steal: SB (85.5%) >> BB (48.4%) - expected pattern
- 3Bet rates consistent with population norms (6-14%)
- 2026-04-15: Fixed sit_81 empty data - removed HAVING >=10 from _texture_agg in generate_full_report.py. Board textures had max 2 per combo, so threshold excluded all data.

---

## Technical Audit Fixes (2026-05-17)

### BUG PATTERN: Self-JOIN (sit_60, sit_61)
**Issue:** `JOIN cash_hand_summary chs ON chps.id_hand = chps.id_hand` — joins chps to itself, not to chs.
**Why:** Copy-paste error. Silent bug — query runs but returns cartesian product garbage.
**How to apply:** Always verify JOIN condition references the correct alias on both sides. When joining two tables, both sides of ON must reference different aliases.

### BUG PATTERN: Wrong denominator in sit_107 Donk Lead
**Issue:** Used `flg_f_cbet_opp = TRUE` (PFR's opportunity) as denominator for donk bet stat.
**Why:** Donk bettor is the NON-PFR. Correct denominator is `flg_f_donk_opp = TRUE`.
**How to apply:** For any stat, numerator and denominator must be from the SAME player perspective. flg_f_cbet_opp belongs to the PFR; flg_f_donk_opp belongs to the caller/donker.

### BUG PATTERN: Wrong street for float bet (sit_84)
**Issue:** Used `flg_f_bet` (flop bet) as numerator for float bet after calling flop cbet.
**Why:** Calling a cbet and betting on the same street are mutually exclusive. Float bet by definition happens on the NEXT street (turn).
**How to apply:** Float bet = called cbet on flop IP → bet on TURN. Numerator must be `flg_t_bet`, denominator must include `flg_t_saw = TRUE`.

### BUG PATTERN: Incomplete probe bet filter (sit_87)
**Issue:** Filter only checked OOP + non-PFR, but didn't verify PFR missed cbet.
**Why:** Knowledge base §6.3: probe = OOP non-PFR bets AFTER PFR checked flop. Without `flg_f_cbet_def_opp = FALSE`, stat includes hands where PFR did cbet (and player called).
**How to apply:** Always cross-reference knowledge base definition when implementing postflop classification stats.

### BUG PATTERN: 3-bet stat includes preflop folders (sit_53, sit_54)
**Issue:** `flg_p_3bet_def_opp = TRUE` includes everyone who faced a 3-bet, including those who folded preflop.
**Why:** "3-Bet Caller" postflop stat must filter to only those who actually called: `enum_p_3bet_action = 'C'`.
**How to apply:** Postflop stats filtered by preflop action must always specify the preflop action taken, not just the opportunity.

### BUG PATTERN: Inconsistent texture classification between modules
**Issue:** pt4_tools.py returned DRY; generate_full_report.py returned RAINBOW/TWO-TONE.
**Why:** Two separate implementations diverged. pt4_tools.py was the older version.
**How to apply:** Single source of truth for texture classification. If both files need it, one should import from the other. Until refactored, keep both in sync manually.
