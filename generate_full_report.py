#!/usr/bin/env python3
# coding: utf-8
"""
generate_full_report.py
Replaces ALL batch/phase scripts with a single unified implementation.

USAGE:
    python generate_full_report.py [player_name]
    python generate_full_report.py jaymacca13
    python generate_full_report.py Hero

RETURN:
    dict keyed by "sit_N" -> {"value": float, "n": int, "status": str, "details": dict}
    Saves report to reports/<player_name>_FULL.md
"""

import psycopg2
import math
import os
import sys
from datetime import datetime

#    Constants                                                                 

DB = dict(
    host='localhost',
    port='5432',
    database='PT4 DB',
    user='postgres',
    password='postgrespass'
)

REPORT_DIR = os.path.join(os.path.dirname(__file__), 'reports')

POSITION_LABELS = {
    0: 'BTN', 1: 'CO', 2: 'HJ', 3: 'MP', 8: 'BB', 9: 'SB'
}
POSITION_KEYS = [0, 1, 9, 2, 3, 8]  # BTN, CO, SB, HJ, MP, BB

#    Section A: Helpers                                                        

RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
SUITS = ['s', 'h', 'd', 'c']


def classify_board_texture(c1, c2, c3):
    """MONOTONE / PAIRED / RAINBOW / TWO-TONE from card values (0-51)."""
    if None in (c1, c2, c3):
        return None
    suits = [c // 13 for c in [c1, c2, c3]]
    ranks = [c % 13 for c in [c1, c2, c3]]
    if len(set(suits)) == 1:
        return 'MONOTONE'
    if len(set(ranks)) < len(ranks):
        return 'PAIRED'
    if len(set(suits)) == 3:
        return 'RAINBOW'
    return 'TWO-TONE'


def classify_board_rank(c1, c2, c3):
    """Ace-high / King-high / Queen-high / Other by high card rank."""
    if None in (c1, c2, c3):
        return None
    ranks = sorted([c % 13 for c in [c1, c2, c3]])
    high_rank = max(ranks)
    if high_rank >= 12:
        return 'Ace-high'
    if high_rank >= 11:
        return 'King-high'
    if high_rank >= 10:
        return 'Queen-high'
    return 'Other'


def low_sample_label(n, threshold=30):
    """Return (label, status)."""
    if n < 15:
        return f"[Exclude] n={n}", "EXCLUDE"
    if n < threshold:
        return f"[Low Sample] n={n}", "LOW SAMPLE"
    return f"n={n}", "OK"


def sample_status(n, threshold=30):
    """Return status string."""
    if n < 15:
        return "EXCLUDE"
    if n < threshold:
        return "LOW SAMPLE"
    return "OK"


#    Section B: Validation                                                     

def validate_fcr(folds, calls, raises, n, tolerance=5.0):
    """Fold+Call+Raise should sum to ~100%."""
    if n == 0:
        return False, "No data"
    total = folds + calls + raises
    if total == 0:
        return False, "No actions"
    sum_pct = (folds + calls + raises) / n * 100.0
    if abs(sum_pct - 100) > tolerance:
        return False, "FCR sum=%.1f%%" % sum_pct
    return True, "OK"


def validate_profit(bb_per_100):
    """Profit should be in plausible range."""
    if bb_per_100 is None:
        return True, "No data"
    if bb_per_100 < -500 or bb_per_100 > 500:
        return False, "bb/100=%.1f out of range" % bb_per_100
    return True, "OK"


#    Section C: Situation Functions                                           

def _run_isolated(func, cur, player):
    """Wrap each situation in error isolation. Rollback after any failure to keep connection alive."""
    try:
        cur.execute("ROLLBACK")
    except Exception:
        pass
    try:
        return func(cur, player)
    except Exception as e:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        return {'value': None, 'n': 0, 'status': 'ERROR: %s' % type(e).__name__,
                'details': {}}


#    #28 Squeeze %                                                             

def sit_28(cur, player):
    """
    Squeeze % = squeezed / squeeze opportunity.
    Squeezed: flg_p_squeeze = TRUE.
    Opp: flg_p_squeeze_opp = TRUE (faced a raise with callers ahead).
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_p_squeeze = TRUE THEN 1 ELSE 0 END) as squeeze
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_squeeze_opp = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    sq = row[1] or 0
    val = (sq / n * 100.0) if n > 0 else None
    status = sample_status(n, 30)
    return {'value': val, 'n': n, 'status': status,
            'details': {'squeezed': sq, 'opportunities': n}}


#    #29 Fold to Squeeze %                                                     

def sit_29(cur, player):
    """
    Fold to Squeeze % = folded-to-squeeze / faced-squeeze.
    Denominator: flg_p_squeeze_def_opp = TRUE.
    Numerator: enum_p_squeeze_action = 'F' (fold).
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.enum_p_squeeze_action = 'F' THEN 1 ELSE 0 END) as folds
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_squeeze_def_opp = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    folds = row[1] or 0
    val = (folds / n * 100.0) if n > 0 else None
    status = sample_status(n, 30)
    return {'value': val, 'n': n, 'status': status,
            'details': {'folds': folds}}


#    #30 4-Bet %                                                              

def sit_30(cur, player):
    """
    4-Bet % = 4-bet / 4-bet opportunity.
    flg_p_4bet = TRUE / flg_p_4bet_opp = TRUE.
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_p_4bet = TRUE THEN 1 ELSE 0 END) as four_bets
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_4bet_opp = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    fb = row[1] or 0
    val = (fb / n * 100.0) if n > 0 else None
    status = sample_status(n, 30)
    return {'value': val, 'n': n, 'status': status,
            'details': {'four_bets': fb}}


#    #31 Fold to 4-Bet %                                                      

def sit_31(cur, player):
    """
    Fold to 4-Bet % = folded-to-4bet / faced-4bet.
    Denominator: flg_p_4bet_def_opp = TRUE.
    Numerator: enum_p_4bet_action = 'F'.
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.enum_p_4bet_action = 'F' THEN 1 ELSE 0 END) as folds
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_4bet_def_opp = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    folds = row[1] or 0
    val = (folds / n * 100.0) if n > 0 else None
    status = sample_status(n, 30)
    return {'value': val, 'n': n, 'status': status,
            'details': {'folds': folds}}


#    #36 VPIP %                                                               

def sit_36(cur, player):
    """
    VPIP % by position.
    Standard: flg_vpip = TRUE / COUNT(*).
    For blind positions (BB=8, SB=9), exclude walk hands from denominator.
    A walk = BB/SB posted, no one raised, player didn't voluntarily enter,
    and the hand didn't go postflop for them (no flop seen).
    Formula: flg_vpip / (COUNT(*) - walk_count) for BB/SB.
    """
    # For blind positions: count walks (folded around unraised, no flop seen)
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_vpip = TRUE THEN 1 ELSE 0 END) as vpip,
            SUM(CASE WHEN chps.flg_f_saw = FALSE AND chps.flg_vpip = FALSE
                      AND chps.flg_p_face_raise = FALSE THEN 1 ELSE 0 END) as walks
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
        GROUP BY chps.position
        HAVING COUNT(*) >= 15
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_vpip = 0
    for pos, n, vpip, walks in rows:
        # For blind positions, exclude walks from the denominator
        if pos in (8, 9):  # BB and SB
            denom = n - walks
            pct = (vpip / denom * 100.0) if denom > 0 else 0.0
        else:
            denom = n
            pct = (vpip / n * 100.0) if n > 0 else 0.0
        label = POSITION_LABELS.get(pos, str(pos))
        details[label] = {'value': pct, 'n': n}
        total_n += n
        total_vpip += vpip
    # Overall uses weighted average (no walk exclusion for overall)
    overall = (total_vpip / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #37 PFR %                                                                

def sit_37(cur, player):
    """
    PFR % by position.
    flg_p_first_raise = TRUE / COUNT(*).
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as n,
            SUM(CASE WHEN chps.cnt_p_raise > 0 THEN 1 ELSE 0 END) as pfr
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
        GROUP BY chps.position
        HAVING COUNT(*) >= 15
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_pfr = 0
    for pos, n, pfr in rows:
        pct = (pfr / n * 100.0) if n > 0 else 0.0
        label = POSITION_LABELS.get(pos, str(pos))
        details[label] = {'value': pct, 'n': n}
        total_n += n
        total_pfr += pfr
    overall = (total_pfr / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #38 Cold Call %                                                          

def sit_38(cur, player):
    """
    Cold Call % = (flg_vpip AND NOT flg_p_first_raise AND NOT flg_p_3bet AND NOT flg_p_squeeze) / all hands.
    Derived formula — no dedicated flag.
    """
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN
                chps.flg_vpip = TRUE
                AND chps.flg_p_first_raise = FALSE
                AND chps.flg_p_3bet = FALSE
                AND chps.flg_p_squeeze = FALSE
            THEN 1 ELSE 0 END) as cold_call
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    cc = row[1] or 0
    val = (cc / n * 100.0) if n > 0 else None
    status = sample_status(n, 30)
    return {'value': val, 'n': n, 'status': status,
            'details': {'cold_calls': cc}}


#    #39 Limp %                                                               

def sit_39(cur, player):
    """
    Limp % = flg_p_limp = TRUE / COUNT(*).
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_p_limp = TRUE THEN 1 ELSE 0 END) as limp
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
        GROUP BY chps.position
        HAVING COUNT(*) >= 15
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_limp = 0
    for pos, n, limp in rows:
        pct = (limp / n * 100.0) if n > 0 else 0.0
        label = POSITION_LABELS.get(pos, str(pos))
        details[label] = {'value': pct, 'n': n}
        total_n += n
        total_limp += limp
    overall = (total_limp / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #40 Fold to Steal                                                         

def sit_40(cur, player):
    """
    Fold to Steal by defender/stealer position.
    UNION ALL over 5 combinations.
    Denominator: flg_blind_def_opp = TRUE (faced a steal attempt).
    Stealer identified via str_aggressors_p LIKE 'DX%' where D=defender pos, X=stealer pos.
    Fold: flg_bb_steal_fold (BB) or flg_sb_steal_fold (SB).
    """
    query = """
    SELECT
    sub.defender, sub.stealer, sub.n, sub.folds,
    ROUND(100.0 * sub.folds / NULLIF(sub.n, 0), 1) as fold_pct
    FROM (
    -- BB vs BTN steal
    SELECT 'BB' as defender, 'BTN' as stealer,
    COUNT(*) as n,
    SUM(CASE WHEN chps.flg_bb_steal_fold = TRUE THEN 1 ELSE 0 END) as folds
    FROM cash_hand_player_statistics chps
    JOIN player p ON p.id_player = chps.id_player
    JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
    WHERE p.player_name = %s
    AND chps.position = 8
    AND chps.flg_blind_def_opp = TRUE
    AND chs.str_aggressors_p LIKE '80%%'
    HAVING COUNT(*) >= 10

    UNION ALL

    -- BB vs CO steal
    SELECT 'BB', 'CO',
    COUNT(*),
    SUM(CASE WHEN chps.flg_bb_steal_fold = TRUE THEN 1 ELSE 0 END)
    FROM cash_hand_player_statistics chps
    JOIN player p ON p.id_player = chps.id_player
    JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
    WHERE p.player_name = %s
    AND chps.position = 8
    AND chps.flg_blind_def_opp = TRUE
    AND chs.str_aggressors_p LIKE '81%%'
    HAVING COUNT(*) >= 10

    UNION ALL

    -- BB vs SB steal
    SELECT 'BB', 'SB',
    COUNT(*),
    SUM(CASE WHEN chps.flg_bb_steal_fold = TRUE THEN 1 ELSE 0 END)
    FROM cash_hand_player_statistics chps
    JOIN player p ON p.id_player = chps.id_player
    JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
    WHERE p.player_name = %s
    AND chps.position = 8
    AND chps.flg_blind_def_opp = TRUE
    AND chs.str_aggressors_p LIKE '89%%'
    HAVING COUNT(*) >= 10

    UNION ALL

    -- SB vs BTN steal
    SELECT 'SB', 'BTN',
    COUNT(*),
    SUM(CASE WHEN chps.flg_sb_steal_fold = TRUE THEN 1 ELSE 0 END)
    FROM cash_hand_player_statistics chps
    JOIN player p ON p.id_player = chps.id_player
    JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
    WHERE p.player_name = %s
    AND chps.position = 9
    AND chps.flg_blind_def_opp = TRUE
    AND chs.str_aggressors_p LIKE '90%%'
    HAVING COUNT(*) >= 10

    UNION ALL

    -- SB vs CO steal
    SELECT 'SB', 'CO',
    COUNT(*),
    SUM(CASE WHEN chps.flg_sb_steal_fold = TRUE THEN 1 ELSE 0 END)
    FROM cash_hand_player_statistics chps
    JOIN player p ON p.id_player = chps.id_player
    JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
    WHERE p.player_name = %s
    AND chps.position = 9
    AND chps.flg_blind_def_opp = TRUE
    AND chs.str_aggressors_p LIKE '91%%'
    HAVING COUNT(*) >= 10
    ) sub
    ORDER BY sub.defender, sub.stealer
    """
    cur.execute(query, (player, player, player, player, player))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_folds = 0
    for defender, stealer, n, folds, fold_pct in rows:
        details['%s_vs_%s' % (defender, stealer)] = {'value': fold_pct, 'n': n, 'folds': folds}
        total_n += n
        total_folds += folds
    overall = (total_folds / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}

#    #41 WTSD % (Went to Showdown)

def sit_41(cur, player):
    """
    WTSD % by position.
    WTSD% = flg_showdown / flg_f_saw (saw flop).
    No SRP/HU filter. Split by seat position. n >= 30 per position.
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_f_saw = TRUE THEN 1 ELSE 0 END) as saw_flop,
            SUM(CASE WHEN chps.flg_showdown = TRUE THEN 1 ELSE 0 END) as showdown
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
        GROUP BY chps.position
        HAVING COUNT(*) >= 30
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_saw = 0
    total_sd = 0
    for pos, n, saw_flop, showdown in rows:
        pct = (showdown / saw_flop * 100.0) if saw_flop > 0 else None
        label = POSITION_LABELS.get(pos, str(pos))
        details[label] = {'value': pct, 'n': saw_flop}
        total_saw += saw_flop
        total_sd += showdown
    overall = (total_sd / total_saw * 100.0) if total_saw > 0 else None
    status = sample_status(total_saw, 30)
    return {'value': overall, 'n': total_saw, 'status': status, 'details': details}


#    #42 W$SD % (Won $ at Showdown)

def sit_42(cur, player):
    """
    W$SD % by position.
    W$SD% = (amt_won > 0 when flg_showdown) / flg_showdown.
    No SRP/HU filter. Split by seat position. n >= 30 (showdown hands).
    """
    cur.execute("""
        SELECT
            chps.position,
            SUM(CASE WHEN chps.flg_showdown = TRUE THEN 1 ELSE 0 END) as sd_hands,
            SUM(CASE WHEN chps.flg_showdown = TRUE AND chps.amt_won > 0 THEN 1 ELSE 0 END) as won_sd
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
        GROUP BY chps.position
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_sd = 0
    total_won = 0
    for pos, sd_hands, won_sd in rows:
        if sd_hands >= 30:
            pct = (won_sd / sd_hands * 100.0) if sd_hands > 0 else None
            label = POSITION_LABELS.get(pos, str(pos))
            details[label] = {'value': pct, 'n': sd_hands}
        total_sd += sd_hands
        total_won += won_sd
    overall = (total_won / total_sd * 100.0) if total_sd > 0 else None
    status = sample_status(total_sd, 30)
    return {'value': overall, 'n': total_sd, 'status': status, 'details': details}


#    #43 WWSF % (Won $ When Saw Flop)

def sit_43(cur, player):
    """
    WWSF % by position.
    WWSF% = (amt_won > 0 when flg_f_saw) / flg_f_saw.
    No SRP/HU filter. Split by seat position. n >= 30.
    """
    cur.execute("""
        SELECT
            chps.position,
            SUM(CASE WHEN chps.flg_f_saw = TRUE THEN 1 ELSE 0 END) as saw_flop,
            SUM(CASE WHEN chps.flg_f_saw = TRUE AND chps.amt_won > 0 THEN 1 ELSE 0 END) as won_flop
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
        GROUP BY chps.position
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_saw = 0
    total_won = 0
    for pos, saw_flop, won_flop in rows:
        if saw_flop >= 30:
            pct = (won_flop / saw_flop * 100.0) if saw_flop > 0 else None
            label = POSITION_LABELS.get(pos, str(pos))
            details[label] = {'value': pct, 'n': saw_flop}
        total_saw += saw_flop
        total_won += won_flop
    overall = (total_won / total_saw * 100.0) if total_saw > 0 else None
    status = sample_status(total_saw, 30)
    return {'value': overall, 'n': total_saw, 'status': status, 'details': details}


#    #35 Missed CBet % (Flop Check as PFR)

def sit_35(cur, player):
    """
    Missed CBet % by position x IP/OOP.
    Denominator: flg_f_cbet_opp = TRUE (was PFR) AND saw the flop opportunity to cbet.
    Numerator: flg_f_cbet = FALSE (did NOT cbet).
    Split by position x IP/OOP (flg_f_has_position). n >= 30.
    """
    cur.execute("""
        SELECT
            chps.position,
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_f_cbet = FALSE THEN 1 ELSE 0 END) as missed
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_opp = TRUE
          AND chps.cnt_p_raise > 0
        GROUP BY chps.position, chps.flg_f_has_position
        HAVING COUNT(*) >= 30
        ORDER BY chps.position, chps.flg_f_has_position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_missed = 0
    for pos, pos_type, n, missed in rows:
        pct = (missed / n * 100.0) if n > 0 else None
        label = POSITION_LABELS.get(pos, str(pos))
        key = '%s (%s)' % (label, pos_type)
        details[key] = {'value': pct, 'n': n}
        total_n += n
        total_missed += missed
    overall = (total_missed / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #47 BB Fold to CBet OOP

def sit_47(cur, player):
    """
    BB Fold to CBet OOP (SRP).
    Denominator: flg_f_cbet_def_opp = TRUE (faced cbet as defender).
    Numerator: flg_f_fold = TRUE.
    Position: BB (8), SRP (flg_p_3bet = FALSE).
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as folds,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = FALSE AND chps.flg_f_check_raise = FALSE AND chps.flg_f_bet = FALSE THEN 1 ELSE 0 END) as calls,
            SUM(CASE WHEN chps.flg_f_check_raise = TRUE OR chps.flg_f_bet = TRUE THEN 1 ELSE 0 END) as raises
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.position = 8  -- BB
          AND chps.flg_p_3bet = FALSE  -- SRP
          AND chps.flg_f_cbet_def_opp = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    folds = row[1] or 0
    calls = row[2] or 0
    raises = row[3] or 0
    val = (folds / n * 100.0) if n > 0 else None
    ok, msg = validate_fcr(folds, calls, raises, n)
    status = sample_status(n, 30)
    if not ok:
        status = 'SUSPICIOUS'
    return {'value': val, 'n': n, 'status': status,
            'details': {'folds': folds, 'calls': calls, 'raises': raises}}


#    #49 CBet Flop BTN IP (SRP)                                               

def sit_49(cur, player):
    """
    CBet Flop as PFR IP (SRP).
    Position: BTN (0), PFR (cnt_p_raise > 0), IP (flg_f_has_position = TRUE).
    SRP: flg_p_3bet = FALSE. HU: chs.cnt_players_f = 2.
    Numerator: flg_f_cbet = TRUE.
    Denominator: flg_f_cbet_opp = TRUE.
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE THEN 1 ELSE 0 END) as cbets
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.position = 0  -- BTN
          AND chps.cnt_p_raise > 0
          AND chps.flg_f_has_position = TRUE
          AND chps.flg_p_3bet = FALSE
          AND chps.flg_f_cbet_opp = TRUE
          AND chs.cnt_players_f = 2
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    cbets = row[1] or 0
    val = (cbets / n * 100.0) if n > 0 else None
    status = sample_status(n, 30)
    return {'value': val, 'n': n, 'status': status,
            'details': {'cbets': cbets}}


#    #51 3-Bettor IP CBet %                                                   

def sit_51(cur, player):
    """
    3-Bettor IP CBet %.
    3-bettor (flg_p_3bet = TRUE), IP (flg_f_has_position = TRUE).
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE THEN 1 ELSE 0 END) as cbets
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_p_3bet = TRUE
          AND chps.flg_f_has_position = TRUE
          AND chps.flg_f_cbet_opp = TRUE
          AND chs.cnt_players_f = 2
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    cbets = row[1] or 0
    val = (cbets / n * 100.0) if n > 0 else None
    status = sample_status(n, 15)
    return {'value': val, 'n': n, 'status': status,
            'details': {'cbets': cbets}}


#    #52 3-Bettor OOP CBet %                                                  

def sit_52(cur, player):
    """
    3-Bettor OOP CBet %.
    3-bettor (flg_p_3bet = TRUE), OOP (flg_f_has_position = FALSE).
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE THEN 1 ELSE 0 END) as cbets
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_p_3bet = TRUE
          AND chps.flg_f_has_position = FALSE
          AND chps.flg_f_cbet_opp = TRUE
          AND chs.cnt_players_f = 2
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    cbets = row[1] or 0
    val = (cbets / n * 100.0) if n > 0 else None
    status = sample_status(n, 15)
    return {'value': val, 'n': n, 'status': status,
            'details': {'cbets': cbets}}


#    #53 3-Bet Caller OOP FCB%                                                

def sit_53(cur, player):
    """
    3-Bet Caller (faced 3-bet, called) OOP: Fold to CBet %.
    Denominator: flg_f_cbet_def_opp = TRUE (player FACED cbet as defender).
    Numerator: flg_f_fold = TRUE.
    OOP: flg_f_has_position = FALSE.
    Filter: enum_p_3bet_action = 'C' ensures only callers (not folders) are included.
    Verified jaymacca13: SB=34.5%, MP=53.3%, EP=44.0%, CO=45.0%.
    """
    cur.execute("""
    SELECT
    SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as folds,
    SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE THEN 1 ELSE 0 END) as opp,
    SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = FALSE AND chps.flg_f_check_raise = FALSE AND chps.flg_f_bet = FALSE THEN 1 ELSE 0 END) as calls,
    SUM(CASE WHEN chps.flg_f_check_raise = TRUE THEN 1 ELSE 0 END) as xr
    FROM cash_hand_player_statistics chps
    JOIN player p ON p.id_player = chps.id_player
    WHERE p.player_name = %s
    AND chps.flg_p_3bet_def_opp = TRUE
    AND chps.enum_p_3bet_action = 'C'
    AND chps.flg_f_has_position = FALSE
    """, (player,))
    row = cur.fetchone()
    folds = row[0] or 0
    n = row[1] or 0
    calls = row[2] or 0
    xr = row[3] or 0
    val = (folds / n * 100.0) if n > 0 else None
    ok, msg = validate_fcr(folds, calls, xr, n)
    status = sample_status(n, 15)
    if not ok:
        status = 'SUSPICIOUS'
    return {'value': val, 'n': n, 'status': status,
            'details': {'folds': folds, 'calls': calls, 'xr': xr}}



#    #54 3-Bet Caller IP FCB%         

def sit_54(cur, player):
    """
    3-Bet Caller IP: Fold to CBet %.
    Denominator: flg_f_cbet_def_opp = TRUE (player FACED cbet as defender).
    Numerator: flg_f_fold = TRUE.
    IP: flg_f_has_position = TRUE.
    Filter: enum_p_3bet_action = 'C' ensures only callers (not folders) are included.
    """
    cur.execute("""
    SELECT
    SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as folds,
    SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE THEN 1 ELSE 0 END) as opp,
    SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = FALSE AND chps.flg_f_check_raise = FALSE AND chps.flg_f_bet = FALSE THEN 1 ELSE 0 END) as calls,
    SUM(CASE WHEN chps.flg_f_check_raise = TRUE THEN 1 ELSE 0 END) as xr
    FROM cash_hand_player_statistics chps
    JOIN player p ON p.id_player = chps.id_player
    WHERE p.player_name = %s
    AND chps.flg_p_3bet_def_opp = TRUE
    AND chps.enum_p_3bet_action = 'C'
    AND chps.flg_f_has_position = TRUE
    """, (player,))
    row = cur.fetchone()
    folds = row[0] or 0
    n = row[1] or 0
    calls = row[2] or 0
    xr = row[3] or 0
    val = (folds / n * 100.0) if n > 0 else None
    ok, msg = validate_fcr(folds, calls, xr, n)
    status = sample_status(n, 15)
    if not ok:
        status = 'SUSPICIOUS'
    return {'value': val, 'n': n, 'status': status,
            'details': {'folds': folds, 'calls': calls, 'xr': xr}}


#    #55 Flop CBet Sizing Distribution

def sit_55(cur, player):
    """
    Flop CBet Sizing Distribution (IP vs OOP).
    Filter: flg_f_cbet = TRUE AND val_f_bet_made_pct IS NOT NULL.
    Buckets: Small (<33%), Medium (33-67%), Large (67-100%), Overbet (>100%).
    Single query returning all 4 bucket counts simultaneously.
    Split by: IP/OOP (flg_f_has_position). n >= 30.
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            COUNT(*) as total,
            SUM(CASE WHEN chps.val_f_bet_made_pct < 33 THEN 1 ELSE 0 END) as small,
            SUM(CASE WHEN chps.val_f_bet_made_pct >= 33 AND chps.val_f_bet_made_pct < 67 THEN 1 ELSE 0 END) as medium,
            SUM(CASE WHEN chps.val_f_bet_made_pct >= 67 AND chps.val_f_bet_made_pct < 100 THEN 1 ELSE 0 END) as large,
            SUM(CASE WHEN chps.val_f_bet_made_pct >= 100 THEN 1 ELSE 0 END) as overbet
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet = TRUE
          AND chps.val_f_bet_made_pct IS NOT NULL
        GROUP BY chps.flg_f_has_position
        ORDER BY chps.flg_f_has_position DESC
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_all = 0
    small_all = 0
    medium_all = 0
    large_all = 0
    overbet_all = 0
    for pos_type, total, small, medium, large, overbet in rows:
        if total < 30:
            continue
        pct_small = (small / total * 100.0) if total > 0 else None
        pct_medium = (medium / total * 100.0) if total > 0 else None
        pct_large = (large / total * 100.0) if total > 0 else None
        pct_over = (overbet / total * 100.0) if total > 0 else None
        details[pos_type] = {
            'value': pct_small, 'n': total,
            'small_pct': pct_small, 'medium_pct': pct_medium,
            'large_pct': pct_large, 'overbet_pct': pct_over
        }
        total_all += total
        small_all += small
        medium_all += medium
        large_all += large
        overbet_all += overbet
    overall_small = (small_all / total_all * 100.0) if total_all > 0 else None
    details['ALL'] = {
        'value': overall_small, 'n': total_all,
        'small_pct': overall_small,
        'medium_pct': (medium_all / total_all * 100.0) if total_all > 0 else None,
        'large_pct': (large_all / total_all * 100.0) if total_all > 0 else None,
        'overbet_pct': (overbet_all / total_all * 100.0) if total_all > 0 else None
    }
    status = sample_status(total_all, 30)
    return {'value': overall_small, 'n': total_all, 'status': status, 'details': details}


#    #59 Rainbow vs Two-Tone CBet%

def sit_59(cur, player):
    """
    CBet% and Fold to CBet% by board texture (Rainbow / Two-Tone / Paired / Monotone).
    Split by IP/OOP. n >= 30 per group.
    CBet% = flg_f_cbet / flg_f_cbet_opp (PFR cbetting).
    Fold to CBet% = flg_f_fold / flg_f_cbet_def_opp (defender folding).
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            CASE WHEN chps.flg_f_cbet_opp = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END,
            CASE WHEN chps.flg_f_cbet = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END,
            CASE WHEN chps.flg_f_cbet_opp = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END,
            CASE WHEN chps.flg_f_cbet = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END,
            CASE WHEN chps.flg_f_cbet_def_opp = TRUE
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END,
            CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END,
            CASE WHEN chps.flg_f_cbet_def_opp = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END,
            CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_p_3bet = FALSE
    """, (player,))
    raw = cur.fetchall()

    groups = {}
    for row in raw:
        c1, c2, c3 = row[0], row[1], row[2]
        tex = classify_board_texture(c1, c2, c3)
        if tex is None:
            continue
        if tex not in groups:
            groups[tex] = {'cbet_opp_ip': 0, 'cbets_ip': 0, 'cbet_opp_oop': 0, 'cbets_oop': 0,
                           'def_opp_ip': 0, 'folds_ip': 0, 'def_opp_oop': 0, 'folds_oop': 0}
        g = groups[tex]
        g['cbet_opp_ip'] += row[3]
        g['cbets_ip'] += row[4]
        g['cbet_opp_oop'] += row[5]
        g['cbets_oop'] += row[6]
        g['def_opp_ip'] += row[7]
        g['folds_ip'] += row[8]
        g['def_opp_oop'] += row[9]
        g['folds_oop'] += row[10]

    details = {}
    for tex in ['RAINBOW', 'TWO-TONE', 'PAIRED', 'MONOTONE']:
        g = groups.get(tex)
        if g is None:
            continue
        cbet_ip = (g['cbets_ip'] / g['cbet_opp_ip'] * 100.0) if g['cbet_opp_ip'] >= 30 else None
        cbet_oop = (g['cbets_oop'] / g['cbet_opp_oop'] * 100.0) if g['cbet_opp_oop'] >= 30 else None
        fcbet_ip = (g['folds_ip'] / g['def_opp_ip'] * 100.0) if g['def_opp_ip'] >= 30 else None
        fcbet_oop = (g['folds_oop'] / g['def_opp_oop'] * 100.0) if g['def_opp_oop'] >= 30 else None
        details[tex] = {
            'value': cbet_ip, 'n': g['cbet_opp_ip'],
            'cbet_ip': cbet_ip, 'cbet_oop': cbet_oop,
            'fcbet_ip': fcbet_ip, 'fcbet_oop': fcbet_oop
        }

    total_cbet_opp = sum(g['cbet_opp_ip'] + g['cbet_opp_oop'] for g in groups.values() if g)
    total_cbets = sum(g['cbets_ip'] + g['cbets_oop'] for g in groups.values() if g)
    overall = (total_cbets / total_cbet_opp * 100.0) if total_cbet_opp > 0 else None
    status = sample_status(total_cbet_opp, 30)
    return {'value': overall, 'n': total_cbet_opp, 'status': status, 'details': details}

#    #66 Next Action after CBet Called (Double Barrel%)

def sit_66(cur, player):
    """
    Double Barrel % after flop CBet called.
    Filter: flg_f_cbet = TRUE (made flop cbet).
    Turn barrel: flg_t_cbet = TRUE / flg_t_cbet_opp = TRUE.
    Split by IP/OOP x seat position. n >= 15.
    """
    cur.execute("""
        SELECT
            chps.position,
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            SUM(CASE WHEN chps.flg_t_cbet_opp = TRUE THEN 1 ELSE 0 END) as barrel_opp,
            SUM(CASE WHEN chps.flg_t_cbet = TRUE THEN 1 ELSE 0 END) as barrels
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet = TRUE
          AND chps.flg_p_3bet = FALSE
        GROUP BY chps.position, chps.flg_f_has_position
        HAVING SUM(CASE WHEN chps.flg_t_cbet_opp = TRUE THEN 1 ELSE 0 END) >= 15
        ORDER BY chps.position, chps.flg_f_has_position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_barrel_opp = 0
    total_barrels = 0
    for pos, pos_type, barrel_opp, barrels in rows:
        pct = (barrels / barrel_opp * 100.0) if barrel_opp > 0 else None
        give_up = barrel_opp - barrels
        give_up_pct = (give_up / barrel_opp * 100.0) if barrel_opp > 0 else None
        label = '%s (%s)' % (POSITION_LABELS.get(pos, str(pos)), pos_type)
        details[label] = {'value': pct, 'n': barrel_opp,
                          'barrel_pct': pct, 'give_up_pct': give_up_pct,
                          'barrels': barrels, 'give_up': give_up}
        total_barrel_opp += barrel_opp
        total_barrels += barrels
    overall = (total_barrels / total_barrel_opp * 100.0) if total_barrel_opp > 0 else None
    status = sample_status(total_barrel_opp, 15)
    return {'value': overall, 'n': total_barrel_opp, 'status': status, 'details': details}


#    #73 3-Bet SB (vs BB)         



#    #73 3-Bet SB (vs BB)                                                     

def sit_73(cur, player):
    """
    3-Bet SB (vs BB open).
    Position: SB (9).
    3-bet / 3-bet opportunity.
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_p_3bet = TRUE THEN 1 ELSE 0 END) as three_bets
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.position = 9  -- SB
          AND chps.flg_p_3bet_opp = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    tb = row[1] or 0
    val = (tb / n * 100.0) if n > 0 else None
    status = sample_status(n, 30)
    return {'value': val, 'n': n, 'status': status,
            'details': {'three_bets': tb}}


#    #74 Check-Call Flop -> Check-Call Turn                                  

def sit_74(cur, player):
    """
    Check-Call Flop -> Turn: what does player do on turn after check-calling flop?
    Entry: called cbet OOP (flg_f_cbet_def_opp=TRUE, flg_f_fold=FALSE, flg_f_check_raise=FALSE).
    Then: check-called turn (flg_t_cbet_def_opp=TRUE, flg_t_fold=FALSE, flg_t_check_raise=FALSE).
    Turn actions: Fold / Check-Down / Bet / Call-Bet.
    """
    cur.execute("""
        SELECT
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_t_fold = TRUE THEN 1 ELSE 0 END) as t_folds,
            SUM(CASE WHEN chps.flg_t_check_raise = TRUE THEN 1 ELSE 0 END) as t_xr,
            SUM(CASE WHEN chps.flg_t_bet = TRUE THEN 1 ELSE 0 END) as t_bets,
            SUM(CASE WHEN chps.flg_t_cbet_def_opp = TRUE
                      AND chps.flg_t_fold = FALSE
                      AND chps.flg_t_check_raise = FALSE
                      AND chps.flg_t_bet = FALSE
                 THEN 1 ELSE 0 END) as t_checkdowns
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
          AND chps.flg_f_fold = FALSE
          AND chps.flg_f_check_raise = FALSE
          AND chps.flg_t_cbet_def_opp = TRUE
          AND chps.flg_t_fold = FALSE
          AND chps.flg_t_check_raise = FALSE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    t_folds = row[1] or 0
    t_xr = row[2] or 0
    t_bets = row[3] or 0
    t_checkdowns = row[4] or 0
    total_actions = t_folds + t_xr + t_bets + t_checkdowns
    val = (t_bets / n * 100.0) if n > 0 else None
    status = sample_status(n, 15)
    return {'value': val, 'n': n, 'status': status,
            'details': {'t_folds': t_folds, 't_xr': t_xr, 't_bets': t_bets,
                        't_checkdowns': t_checkdowns, 'total_actions': total_actions}}


#    #75 Donk Multi-Street                                                    

def sit_75(cur, player):
    """
    Donk Bet Multi-Street: player led flop, what happened on turn?
    Filter: flg_f_donk = TRUE (led flop as preflop caller).
    Turn outcomes: Fold / Check-Down / Bet / Call.
    """
    cur.execute("""
        SELECT
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_t_fold = TRUE THEN 1 ELSE 0 END) as t_folds,
            SUM(CASE WHEN chps.flg_t_bet = TRUE THEN 1 ELSE 0 END) as t_bets,
            SUM(CASE WHEN chps.flg_t_check_raise = TRUE THEN 1 ELSE 0 END) as t_xr,
            SUM(CASE WHEN chps.flg_t_cbet_def_opp = TRUE
                      AND chps.flg_t_fold = FALSE
                      AND chps.flg_t_bet = FALSE
                      AND chps.flg_t_check_raise = FALSE
                 THEN 1 ELSE 0 END) as t_checkdowns
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_donk = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    t_folds = row[1] or 0
    t_bets = row[2] or 0
    t_xr = row[3] or 0
    t_checkdowns = row[4] or 0
    val = (t_bets / n * 100.0) if n > 0 else None
    status = sample_status(n, 10)
    return {'value': val, 'n': n, 'status': status,
            'details': {'t_folds': t_folds, 't_bets': t_bets,
                        't_xr': t_xr, 't_checkdowns': t_checkdowns}}


#    #76 Raise CBet -> Turn                                                   

def sit_76(cur, player):
    """
    Raise CBet -> Turn: player check-raised flop, what happened turn?
    Filter: flg_f_check_raise = TRUE.
    Turn actions: Fold / Bet / Check-Down / Call.
    """
    cur.execute("""
        SELECT
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_t_fold = TRUE THEN 1 ELSE 0 END) as t_folds,
            SUM(CASE WHEN chps.flg_t_bet = TRUE THEN 1 ELSE 0 END) as t_bets,
            SUM(CASE WHEN chps.flg_t_check_raise = TRUE THEN 1 ELSE 0 END) as t_xr,
            SUM(CASE WHEN chps.flg_t_cbet_def_opp = TRUE
                      AND chps.flg_t_fold = FALSE
                      AND chps.flg_t_bet = FALSE
                      AND chps.flg_t_check_raise = FALSE
                 THEN 1 ELSE 0 END) as t_checkdowns
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_check_raise = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    t_folds = row[1] or 0
    t_bets = row[2] or 0
    t_xr = row[3] or 0
    t_checkdowns = row[4] or 0
    val = (t_bets / n * 100.0) if n > 0 else None
    status = sample_status(n, 15)
    return {'value': val, 'n': n, 'status': status,
            'details': {'t_folds': t_folds, 't_bets': t_bets,
                        't_xr': t_xr, 't_checkdowns': t_checkdowns}}


#    #77 Float then Raise Turn                                                

def sit_77(cur, player):
    """
    Float then Raise Turn: called cbet flop, then raised turn.
    Filter: flg_f_cbet_def_opp=TRUE, flg_f_fold=FALSE (called cbet).
    Then: raised turn (cnt_t_raise > 0 OR flg_t_check_raise=TRUE).
    """
    cur.execute("""
        SELECT
            COUNT(*) as n,
            SUM(CASE WHEN chps.cnt_t_raise > 0 THEN 1 ELSE 0 END) as raised_turn
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
          AND chps.flg_f_fold = FALSE
          AND chps.flg_t_cbet_def_opp = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    raised = row[1] or 0
    val = (raised / n * 100.0) if n > 0 else None
    status = sample_status(n, 15)
    return {'value': val, 'n': n, 'status': status,
            'details': {'raised_turn': raised}}


#    #78 WTSD by Street                                                       

def sit_78(cur, player):
    """
    WTSD % by street: went to showdown / saw that street.
    Filter: flg_f_cbet_def_opp = TRUE (saw flop as defender).
    WTSD = flg_showdown = TRUE / COUNT(*).
    """
    cur.execute("""
        SELECT
            COUNT(*) as saw_flop,
            SUM(CASE WHEN chps.flg_showdown = TRUE THEN 1 ELSE 0 END) as wtsd
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    wtsd = row[1] or 0
    val = (wtsd / n * 100.0) if n > 0 else None
    status = sample_status(n, 15)
    return {'value': val, 'n': n, 'status': status,
            'details': {'wtsd': wtsd}}


#    #80-84: Texture-Based Situations                                        

def _texture_agg(cur, player, filter_sql, num_col, num_alias):
    """
    Generic texture aggregation: fetch raw rows with cards,
    classify texture in Python, group and compute percentages.
    filter_sql: additional WHERE clause fragment.
    num_col: column to sum for numerator (e.g., 'flg_f_fold').
    num_alias: alias for the numerator sum.
    Returns dict keyed by (texture) -> {value, n}.
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            COUNT(*) as n,
            SUM(CASE WHEN %s = TRUE THEN 1 ELSE 0 END) as num
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %%s
          AND %s
        GROUP BY chs.card_1, chs.card_2, chs.card_3
    """ % (num_col, filter_sql), (player,))
    raw = cur.fetchall()

    groups = {}
    for c1, c2, c3, n, num in raw:
        tex = classify_board_texture(c1, c2, c3)
        if tex is None:
            continue
        if tex not in groups:
            groups[tex] = {'n': 0, 'num': 0}
        groups[tex]['n'] += n
        groups[tex]['num'] += num

    result = {}
    for tex, g in groups.items():
        val = (g['num'] / g['n'] * 100.0) if g['n'] > 0 else None
        result[tex] = {'value': val, 'n': g['n']}
    return result


def sit_80(cur, player):
    """Fold to CBet by Texture (SRP, OOP faced cbet)."""
    details = _texture_agg(
        cur, player,
        filter_sql="chps.flg_f_cbet_def_opp = TRUE",
        num_col="chps.flg_f_fold",
        num_alias="folds"
    )
    # Weighted average
    total_n = sum(v['n'] for v in details.values())
    total_folds = sum(v['n'] * (v['value'] or 0) / 100.0 for v in details.values())
    overall = (total_folds / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


def sit_81(cur, player):
    """CBet by Texture (PFR, IP)."""
    details = _texture_agg(
        cur, player,
        filter_sql="chps.flg_f_cbet_opp = TRUE AND chps.flg_p_first_raise = TRUE "
                   "AND chps.flg_f_has_position = TRUE AND chps.flg_p_3bet = FALSE",
        num_col="chps.flg_f_cbet",
        num_alias="cbets"
    )
    total_n = sum(v['n'] for v in details.values())
    total_cbets = sum(v['n'] * (v['value'] or 0) / 100.0 for v in details.values())
    overall = (total_cbets / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


def sit_82(cur, player):
    """Check-Raise by Texture (SRP, OOP).
    Denominator: flg_f_cbet_def_opp = TRUE (faced cbet as defender, OOP).
    Numerator: flg_f_check_raise = TRUE.
    This fixes the tautology where filter = numerator (100% by definition).
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_f_check_raise = TRUE THEN 1 ELSE 0 END) as xr
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
          AND chps.flg_p_3bet = FALSE
        GROUP BY chs.card_1, chs.card_2, chs.card_3
        HAVING COUNT(*) >= 10
    """, (player,))
    raw = cur.fetchall()
    groups = {}
    for c1, c2, c3, n, xr in raw:
        tex = classify_board_texture(c1, c2, c3)
        if tex is None:
            continue
        if tex not in groups:
            groups[tex] = {'n': 0, 'xr': 0}
        groups[tex]['n'] += n
        groups[tex]['xr'] += xr
    details = {}
    total_n = 0
    total_xr = 0
    for tex, g in groups.items():
        val = (g['xr'] / g['n'] * 100.0) if g['n'] > 0 else None
        details[tex] = {'value': val, 'n': g['n']}
        total_n += g['n']
        total_xr += g['xr']
    overall = (total_xr / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


def sit_83(cur, player):
    """Donk Bet by Texture (led flop as preflop caller, OOP).
    Denominator: flg_f_donk_opp = TRUE (had donk opportunity).
    Numerator: flg_f_donk = TRUE (actually donked).
    This fixes the tautology where filter = numerator (100% by definition).
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_f_donk = TRUE THEN 1 ELSE 0 END) as donks
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_f_donk_opp = TRUE
        GROUP BY chs.card_1, chs.card_2, chs.card_3
        HAVING COUNT(*) >= 10
    """, (player,))
    raw = cur.fetchall()
    groups = {}
    for c1, c2, c3, n, donks in raw:
        tex = classify_board_texture(c1, c2, c3)
        if tex is None:
            continue
        if tex not in groups:
            groups[tex] = {'n': 0, 'donks': 0}
        groups[tex]['n'] += n
        groups[tex]['donks'] += donks
    details = {}
    total_n = 0
    total_donks = 0
    for tex, g in groups.items():
        val = (g['donks'] / g['n'] * 100.0) if g['n'] > 0 else None
        details[tex] = {'value': val, 'n': g['n']}
        total_n += g['n']
        total_donks += g['donks']
    overall = (total_donks / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


def sit_84(cur, player):
    """Float by Texture (called cbet IP — float bet definition).
    Denominator: called flop cbet IP AND saw turn (flg_f_cbet_def_opp=TRUE, flg_f_fold=FALSE,
                 flg_f_has_position=TRUE, flg_t_saw=TRUE).
    Numerator: flg_t_bet = TRUE (bet on TURN = float bet on next street).
    Note: flg_f_bet was wrong — that's a flop bet, impossible after calling a flop cbet.
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_t_bet = TRUE THEN 1 ELSE 0 END) as floats
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
          AND chps.flg_f_fold = FALSE
          AND chps.flg_f_has_position = TRUE
          AND chps.flg_t_saw = TRUE
        GROUP BY chs.card_1, chs.card_2, chs.card_3
        HAVING COUNT(*) >= 10
    """, (player,))
    raw = cur.fetchall()
    groups = {}
    for c1, c2, c3, n, floats in raw:
        tex = classify_board_texture(c1, c2, c3)
        if tex is None:
            continue
        if tex not in groups:
            groups[tex] = {'n': 0, 'floats': 0}
        groups[tex]['n'] += n
        groups[tex]['floats'] += floats
    details = {}
    total_n = 0
    total_floats = 0
    for tex, g in groups.items():
        val = (g['floats'] / g['n'] * 100.0) if g['n'] > 0 else None
        details[tex] = {'value': val, 'n': g['n']}
        total_n += g['n']
        total_floats += g['floats']
    overall = (total_floats / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #87 Probe Bet % by Street (OOP non-PFR)

def sit_87(cur, player):
    """
    Probe Bet % by Street: OOP non-PFR after PFR MISSED flop cbet.
    Per knowledge base §6.3: probe = OOP non-PFR bets on turn/river after PFR checked flop.
    Filter: flg_f_saw=TRUE (saw flop) AND flg_f_cbet_opp=FALSE (not PFR)
            AND flg_f_has_position=FALSE (OOP) AND flg_f_cbet_def_opp=FALSE (PFR checked, no cbet faced).
    Turn probe: flg_t_bet = TRUE / flg_t_saw = TRUE.
    River probe: flg_r_bet = TRUE / flg_r_saw = TRUE.
    Split by seat position. n >= 15.
    """
    cur.execute("""
        SELECT
            chps.position,
            SUM(CASE WHEN chps.flg_t_saw = TRUE THEN 1 ELSE 0 END) as t_saw,
            SUM(CASE WHEN chps.flg_t_bet = TRUE THEN 1 ELSE 0 END) as t_probes,
            SUM(CASE WHEN chps.flg_r_saw = TRUE THEN 1 ELSE 0 END) as r_saw,
            SUM(CASE WHEN chps.flg_r_bet = TRUE THEN 1 ELSE 0 END) as r_probes
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_saw = TRUE
          AND chps.flg_f_cbet_opp = FALSE
          AND chps.flg_f_has_position = FALSE
          AND chps.flg_f_cbet_def_opp = FALSE
        GROUP BY chps.position
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_t_saw = 0
    total_t_probes = 0
    total_r_saw = 0
    total_r_probes = 0
    for pos, t_saw, t_probes, r_saw, r_probes in rows:
        label = POSITION_LABELS.get(pos, str(pos))
        t_pct = (t_probes / t_saw * 100.0) if t_saw >= 15 and t_saw > 0 else None
        r_pct = (r_probes / r_saw * 100.0) if r_saw >= 15 and r_saw > 0 else None
        details[label] = {
            'value': t_pct, 'n': t_saw,
            'turn_pct': t_pct, 'turn_n': t_saw,
            'river_pct': r_pct, 'river_n': r_saw
        }
        total_t_saw += t_saw
        total_t_probes += t_probes
        total_r_saw += r_saw
        total_r_probes += r_probes
    overall = (total_t_probes / total_t_saw * 100.0) if total_t_saw > 0 else None
    details['ALL'] = {
        'value': overall, 'n': total_t_saw,
        'turn_pct': overall, 'turn_n': total_t_saw,
        'river_pct': (total_r_probes / total_r_saw * 100.0) if total_r_saw > 0 else None,
        'river_n': total_r_saw
    }
    status = sample_status(total_t_saw, 15)
    return {'value': overall, 'n': total_t_saw, 'status': status, 'details': details}


#    #90 Fold by Rank x Texture

def sit_90(cur, player):
    """
    Fold to CBet by board rank category AND texture.
    4x4 = 16 buckets.
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as folds
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
        GROUP BY chs.card_1, chs.card_2, chs.card_3
        HAVING COUNT(*) >= 10
    """, (player,))
    raw = cur.fetchall()

    groups = {}
    for c1, c2, c3, n, folds in raw:
        tex = classify_board_texture(c1, c2, c3)
        rank = classify_board_rank(c1, c2, c3)
        if tex is None or rank is None:
            continue
        key = '%s / %s' % (rank, tex)
        if key not in groups:
            groups[key] = {'n': 0, 'folds': 0}
        groups[key]['n'] += n
        groups[key]['folds'] += folds

    details = {}
    for key, g in groups.items():
        val = (g['folds'] / g['n'] * 100.0) if g['n'] > 0 else None
        details[key] = {'value': val, 'n': g['n']}

    total_n = sum(v['n'] for v in details.values())
    overall = None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #92 Postflop Profit by Position (bb/100)

def sit_92(cur, player):
    """
    Postflop profit by position in bb/100.
    Formula: (SUM(amt_won) / AVG(cl.amt_bb)) * 100.0 / COUNT(*)
    No SRP/HU filter. ALL cash hands.
    JOIN cash_limit cl for bb size. Flag if abs(bb/100) > 500.
    n >= 200 per position.
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as n,
            SUM(chps.amt_won) as net_won,
            AVG(cl.amt_bb) as avg_bb
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_limit cl ON chps.id_limit = cl.id_limit
        WHERE p.player_name = %s
          AND cl.amt_bb > 0
        GROUP BY chps.position
        HAVING COUNT(*) >= 200
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    all_bb_weighted = 0
    total_won = 0
    for pos, n, net_won, avg_bb in rows:
        net_won = float(net_won or 0)
        avg_bb = float(avg_bb or 0)
        bb100 = (net_won / avg_bb * 100.0 / n) if n > 0 and avg_bb > 0 else None
        label = POSITION_LABELS.get(pos, str(pos))
        details[label] = {'value': bb100, 'n': n}
        total_n += n
        all_bb_weighted += avg_bb * n
        total_won += net_won
    overall_avg_bb = all_bb_weighted / total_n if total_n > 0 else 0
    overall = (total_won / overall_avg_bb * 100.0 / total_n) if total_n > 0 and overall_avg_bb > 0 else None
    # Sanity flag
    if overall is not None and abs(overall) > 500:
        status = 'VARIANCE: bb/100=%.1f (extreme)' % overall
    else:
        status = sample_status(total_n, 200)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #93 Fold to River CBet

def sit_93(cur, player):
    """
    Fold to River CBet by IP/OOP.
    Filter: flg_r_cbet_def_opp = TRUE (faced river cbet).
    Fold% = flg_r_fold / flg_r_cbet_def_opp.
    Call% and Raise% derived.
    Split by IP/OOP (flg_f_has_position used as proxy; no river-specific flag).
    n >= 15.
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_r_fold = TRUE THEN 1 ELSE 0 END) as folds,
            SUM(CASE WHEN chps.flg_r_check_raise = TRUE THEN 1 ELSE 0 END) as raises
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_r_cbet_def_opp = TRUE
        GROUP BY chps.flg_f_has_position
        ORDER BY chps.flg_f_has_position DESC
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_folds = 0
    total_raises = 0
    for pos_type, n, folds, raises in rows:
        calls = n - folds - raises
        fold_pct = (folds / n * 100.0) if n >= 15 else None
        call_pct = (calls / n * 100.0) if n >= 15 else None
        raise_pct = (raises / n * 100.0) if n >= 15 else None
        details[pos_type] = {
            'value': fold_pct, 'n': n,
            'fold_pct': fold_pct, 'call_pct': call_pct, 'raise_pct': raise_pct
        }
        total_n += n
        total_folds += folds
        total_raises += raises
    overall = (total_folds / total_n * 100.0) if total_n > 0 else None
    total_calls = total_n - total_folds - total_raises
    details['ALL'] = {
        'value': overall, 'n': total_n,
        'fold_pct': overall,
        'call_pct': (total_calls / total_n * 100.0) if total_n > 0 else None,
        'raise_pct': (total_raises / total_n * 100.0) if total_n > 0 else None
    }
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #94 Float Bet % as non-PFR IP

def sit_94(cur, player):
    """
    Float Bet %: IP non-PFR who bets when PFR checks flop.
    Filter: flg_f_has_position = TRUE (IP) AND flg_f_cbet_opp = FALSE (not PFR).
    Float bet: flg_f_bet = TRUE (IP bets after PFR checks).
    Split by seat position. n >= 15.
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_f_bet = TRUE THEN 1 ELSE 0 END) as float_bets
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_has_position = TRUE
          AND chps.flg_f_cbet_opp = FALSE
          AND chps.flg_p_3bet = FALSE
          AND chps.flg_f_saw = TRUE
        GROUP BY chps.position
        HAVING COUNT(*) >= 15
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_floats = 0
    for pos, n, float_bets in rows:
        pct = (float_bets / n * 100.0) if n > 0 else None
        label = POSITION_LABELS.get(pos, str(pos))
        details[label] = {'value': pct, 'n': n}
        total_n += n
        total_floats += float_bets
    overall = (total_floats / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #96 BB vs BTN HU Postflop

def sit_96(cur, player):
    """
    BB vs BTN Heads-Up Postflop Profile (SRP).
    Both BB (position=8) and BTN (position=0) stats in HU (cnt_players_f=2).
    Metrics: CBet%, Fold%, Check-Raise%, Donk%.
    """
    # As BB (OOP)
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE THEN 1 ELSE 0 END) as cbet,
            SUM(CASE WHEN chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as fold,
            SUM(CASE WHEN chps.flg_f_check_raise = TRUE THEN 1 ELSE 0 END) as xr,
            SUM(CASE WHEN chps.flg_f_donk = TRUE THEN 1 ELSE 0 END) as donk
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.position = 8  -- BB
          AND chs.cnt_players_f = 2  -- HU
          AND chps.flg_p_3bet = FALSE  -- SRP
          AND chps.flg_f_cbet_opp = TRUE
    """, (player,))
    bb_row = cur.fetchone()
    # As BTN (IP)
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE THEN 1 ELSE 0 END) as cbet,
            SUM(CASE WHEN chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as fold,
            SUM(CASE WHEN chps.flg_f_check_raise = TRUE THEN 1 ELSE 0 END) as xr,
            SUM(CASE WHEN chps.flg_f_donk = TRUE THEN 1 ELSE 0 END) as donk
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.position = 0  -- BTN
          AND chs.cnt_players_f = 2  -- HU
          AND chps.flg_p_3bet = FALSE  -- SRP
          AND chps.flg_f_cbet_opp = TRUE
    """, (player,))
    btn_row = cur.fetchone()

    details = {}
    for label, row in [('BB (OOP)', bb_row), ('BTN (IP)', btn_row)]:
        if row is None:
            continue
        n = row[0] or 0
        cbet = row[1] or 0
        fold = row[2] or 0
        xr = row[3] or 0
        donk = row[4] or 0
        if n > 0:
            details[label] = {
                'CBet%': round(cbet / n * 100.0, 1),
                'Fold%': round(fold / n * 100.0, 1),
                'XR%': round(xr / n * 100.0, 1),
                'Donk%': round(donk / n * 100.0, 1),
                'n': n
            }

    total_n = (bb_row[0] or 0) + (btn_row[0] or 0)
    status = sample_status(total_n, 30)
    return {'value': None, 'n': total_n, 'status': status, 'details': details}


#    #98 River Lead %                                                         

def sit_98(cur, player):
    """
    River Bet % as Non-PFR (River Lead / Donk).
    Non-PFR: flg_f_cbet_opp = FALSE (did not face cbet, so preflop caller).
    Saw river: flg_r_saw = TRUE.
    Led river: flg_r_bet = TRUE.
    flg_r_bet_opp does NOT exist — use raw bet flag.
    """
    cur.execute("""
        SELECT
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_r_bet = TRUE THEN 1 ELSE 0 END) as bets
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_opp = FALSE  -- non-PFR (preflop caller)
          AND chps.flg_r_saw = TRUE
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    bets = row[1] or 0
    val = (bets / n * 100.0) if n > 0 else None
    status = sample_status(n, 15)
    return {'value': val, 'n': n, 'status': status,
            'details': {'bets': bets}}


#    #99 Cold Call Profit    �
def sit_99(cur, player):
    """
    Cold Call Postflop Profit (bb/100).
    bb/100 = (SUM(amt_won) / AVG(cl.amt_bb)) * 100.0 / COUNT(*)
    where COUNT(*) is the number of cold call hands (the situational sample).
    This measures profit per 100 cold-call situations, NOT per 100 all hands.
    Sanity check: abs(bb_per_100) > 500 -> SUSPICIOUS.
    """
    cur.execute("""
    SELECT
    COUNT(*) as n,
    SUM(chps.amt_won) as sum_won,
    AVG(cl.amt_bb) as avg_bb
    FROM cash_hand_player_statistics chps
    JOIN player p ON p.id_player = chps.id_player
    JOIN cash_limit cl ON chps.id_limit = cl.id_limit
    WHERE p.player_name = %s
    AND chps.flg_p_ccall = TRUE
    AND cl.amt_bb > 0
    """, (player,))
    row = cur.fetchone()
    n = row[0] or 0
    sum_won = float(row[1]) if row[1] is not None else 0.0
    avg_bb = float(row[2]) if row[2] is not None else 0.0
    bb_per_100 = (sum_won / avg_bb) * 100.0 / n if avg_bb > 0 and n > 0 else None
    ok, msg = validate_profit(bb_per_100 if bb_per_100 is not None else 0)
    status = sample_status(n, 30)
    if not ok:
        status = 'SUSPICIOUS'
    return {'value': bb_per_100, 'n': n, 'status': status,
            'details': {'sum_won': round(sum_won, 2), 'avg_bb': round(avg_bb, 4)}}

#    #100 Fold to CBet by Stack Depth                                         

def sit_100(cur, player):
    """
    Fold to CBet by Effective Stack Depth.
    Stack from chps.amt_f_effective_stack (NOT chs).
    Buckets: Short (<30 BB) / Medium (30-60 BB) / Deep (>=60 BB).
    """
    cur.execute("""
        SELECT
            CASE
                WHEN chps.amt_f_effective_stack / cl.amt_bb < 30 THEN 'Short (<30 BB)'
                WHEN chps.amt_f_effective_stack / cl.amt_bb < 60 THEN 'Medium (30-60 BB)'
                ELSE 'Deep (60+ BB)'
            END as bucket,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as folds,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = FALSE AND chps.flg_f_check_raise = FALSE AND chps.flg_f_bet = FALSE THEN 1 ELSE 0 END) as calls,
            SUM(CASE WHEN chps.flg_f_check_raise = TRUE THEN 1 ELSE 0 END) as xr
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        JOIN cash_limit cl ON chps.id_limit = cl.id_limit
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
          AND cl.amt_bb > 0
        GROUP BY bucket
        HAVING COUNT(*) >= 10
        ORDER BY bucket
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_folds = 0
    for bucket, n, folds, calls, xr in rows:
        pct = (folds / n * 100.0) if n > 0 else None
        details[bucket] = {'value': pct, 'n': n, 'folds': folds, 'calls': calls, 'xr': xr}
        total_n += n
        total_folds += folds
    overall = (total_folds / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #101 3-Bet IP/OOP vs Raiser                                              

def sit_101(cur, player):
    """
    3-Bet IP vs OOP vs original raiser.
    IP = player closer to BTN than original raiser.
    Check str_aggressors_p to determine who raised first.
    """
    cur.execute("""
        SELECT
            CASE
                WHEN chps.flg_f_has_position = TRUE THEN 'IP'
                ELSE 'OOP'
            END as ip_oop,
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_p_3bet = TRUE THEN 1 ELSE 0 END) as three_bets
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_3bet_opp = TRUE
        GROUP BY CASE
            WHEN chps.flg_f_has_position = TRUE THEN 'IP'
            ELSE 'OOP'
        END
        HAVING COUNT(*) >= 15
        ORDER BY ip_oop
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_tb = 0
    for ip_oop, n, tb in rows:
        pct = (tb / n * 100.0) if n > 0 else None
        details[ip_oop] = {'value': pct, 'n': n, 'three_bets': tb}
        total_n += n
        total_tb += tb
    overall = (total_tb / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #103 Squeeze Profile                                                     

def sit_103(cur, player):
    """
    Squeeze Profile: squeeze % by position.
    flg_p_squeeze = TRUE / flg_p_squeeze_opp = TRUE.
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as opp,
            SUM(CASE WHEN chps.flg_p_squeeze = TRUE THEN 1 ELSE 0 END) as squeeze
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_squeeze_opp = TRUE
        GROUP BY chps.position
        HAVING COUNT(*) >= 15
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_sq = 0
    for pos, n, sq in rows:
        pct = (sq / n * 100.0) if n > 0 else None
        label = POSITION_LABELS.get(pos, str(pos))
        details[label] = {'value': pct, 'n': n, 'squeezes': sq}
        total_n += n
        total_sq += sq
    overall = (total_sq / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 30)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #104 WTSD by Pot Type                                                    

def sit_104(cur, player):
    """
    WTSD by Pot Type: SRP vs 3BP, IP vs OOP.
    WTSD% = flg_showdown / COUNT(*) (saw flop).
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_p_3bet = TRUE THEN '3BP' ELSE 'SRP' END as pot_type,
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END as ip_oop,
            COUNT(*) as saw_flop,
            SUM(CASE WHEN chps.flg_showdown = TRUE THEN 1 ELSE 0 END) as wtsd,
            SUM(CASE WHEN chps.flg_showdown = TRUE
                     AND chps.flg_won_hand = TRUE THEN 1 ELSE 0 END) as wsd
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_has_position IS NOT NULL
        GROUP BY
            CASE WHEN chps.flg_p_3bet = TRUE THEN '3BP' ELSE 'SRP' END,
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END
        HAVING COUNT(*) >= 15
        ORDER BY pot_type, ip_oop
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_wtsd = 0
    for pot_type, ip_oop, n, wtsd, wsd in rows:
        wtsd_pct = (wtsd / n * 100.0) if n > 0 else None
        wsd_pct = (wsd / wtsd * 100.0) if wtsd > 0 else None
        key = '%s / %s' % (pot_type, ip_oop)
        details[key] = {'WTSD%': wtsd_pct, 'W$SD%': wsd_pct, 'n': n, 'wtsd': wtsd, 'wsd': wsd}
        total_n += n
        total_wtsd += wtsd
    overall = (total_wtsd / total_n * 100.0) if total_n > 0 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #106 RFI vs Population                                                   

def sit_106(cur, player):
    """
    RFI % vs Population Average (Z-Score).
    Population: all players with n>=500 at each position.
    Player: current player.
    """
    # Phase 1: Get population stats
    cur.execute("""
        SELECT
            chps.position,
            COUNT(DISTINCT p.player_name) as players,
            AVG(pop.rfi_pct) as pop_avg,
            STDDEV(pop.rfi_pct) as pop_std,
            COUNT(*) as n_hands
        FROM (
            SELECT
                p.player_name,
                chps.position,
                SUM(CASE WHEN chps.flg_p_first_raise = TRUE THEN 1 ELSE 0 END) as rfi,
                SUM(CASE WHEN chps.flg_p_open_opp = TRUE THEN 1 ELSE 0 END) as opp,
                CASE WHEN SUM(CASE WHEN chps.flg_p_open_opp = TRUE THEN 1 ELSE 0 END) > 0
                     THEN SUM(CASE WHEN chps.flg_p_first_raise = TRUE THEN 1 ELSE 0 END) * 100.0
                          / SUM(CASE WHEN chps.flg_p_open_opp = TRUE THEN 1 ELSE 0 END)
                     ELSE 0 END as rfi_pct
            FROM cash_hand_player_statistics chps
            JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
            JOIN player p ON p.id_player = chps.id_player
            WHERE chps.flg_p_open_opp = TRUE
              AND chps.position IN (0, 1, 9, 2, 3, 8)
            GROUP BY p.player_name, chps.position
            HAVING SUM(CASE WHEN chps.flg_p_open_opp = TRUE THEN 1 ELSE 0 END) >= 500
        ) pop
        JOIN cash_hand_player_statistics chps ON chps.position = pop.position
        JOIN player p ON p.id_player = chps.id_player
        WHERE chps.flg_p_open_opp = TRUE
        GROUP BY chps.position
        ORDER BY chps.position
    """)
    pop_raw = cur.fetchall()

    pop_stats = {}
    for pos, n_players, pop_avg, pop_std, n_hands in pop_raw:
        pop_stats[pos] = {'avg': float(pop_avg) if pop_avg is not None else 0.0, 'std': float(pop_std) if pop_std is not None else 0.0,
                          'n_players': n_players, 'n_hands': n_hands}

    # Phase 2: Player RFI
    cur.execute("""
        SELECT
            chps.position,
            SUM(CASE WHEN chps.flg_p_first_raise = TRUE THEN 1 ELSE 0 END) as rfi,
            SUM(CASE WHEN chps.flg_p_open_opp = TRUE THEN 1 ELSE 0 END) as opp
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_open_opp = TRUE
          AND chps.position IN (0, 1, 9, 2, 3, 8)
        GROUP BY chps.position
    """, (player,))

    details = {}
    for pos, rfi, opp in cur.fetchall():
        if opp is None or float(opp) == 0:
            continue
        rfi_f = float(rfi)
        opp_f = float(opp)
        pct = rfi_f / opp_f * 100.0
        pos_int = int(pos) if pos is not None else 0
        label = POSITION_LABELS.get(pos_int, str(pos_int))
        pop = pop_stats.get(pos_int, {'avg': None, 'std': 0})
        if pop['avg'] is not None and pop['std'] > 0:
            dev = pct - pop['avg']
            z = dev / pop['std']
        else:
            dev = None
            z = None
        details[label] = {'player_pct': round(pct, 1),
                         'pop_avg': round(pop['avg'], 1) if pop['avg'] is not None else None,
                          'dev': round(dev, 1) if dev is not None else None,
                          'z': round(z, 2) if z is not None else None,
                          'n': opp}

    status = 'OK'
    return {'value': None, 'n': 0, 'status': status, 'details': details}


#    #107 Leak Score Ranking                                                  

#    #107 Leak Score Ranking         

def sit_107(cur, player):
    """
    Leak Score Ranking -- LIVE SQL per player.
    For each metric: query actual%, compare vs GTO threshold%.
    Direction: fold metrics are leaks ONLY when actual > threshold (over-folding).
    BB Fold to Steal is EXCLUDED (under-folding is a different issue).
    Formula: |actual - threshold| x max(1, log10(n)) x Frequency Multiplier.
    """
    # GTO thresholds and metadata: (description, numerator_filter, denominator_filter, GTO_threshold, freq_mult)
    # Excludes BB Fold to Steal (sit_40 BB-vs-BTN/CO/SB) -- under-folding is a different issue.
    leak_defs = [
        ('Fold to 3-Bet - SB',
         'chps.flg_p_3bet_def_opp = TRUE AND chps.position = 9 AND chps.enum_p_3bet_action = \'F\'',
         'chps.flg_p_3bet_def_opp = TRUE AND chps.position = 9',
         55.0, 1.2),
        ('Fold to 3-Bet - CO',
         'chps.flg_p_3bet_def_opp = TRUE AND chps.position = 1 AND chps.enum_p_3bet_action = \'F\'',
         'chps.flg_p_3bet_def_opp = TRUE AND chps.position = 1',
         55.0, 1.2),
        ('Fold to 3-Bet - MP',
         'chps.flg_p_3bet_def_opp = TRUE AND chps.position = 3 AND chps.enum_p_3bet_action = \'F\'',
         'chps.flg_p_3bet_def_opp = TRUE AND chps.position = 3',
         55.0, 1.2),
        ('Fold to 3-Bet - HJ',
         'chps.flg_p_3bet_def_opp = TRUE AND chps.position = 2 AND chps.enum_p_3bet_action = \'F\'',
         'chps.flg_p_3bet_def_opp = TRUE AND chps.position = 2',
         55.0, 1.2),
        ('Fold to 4-Bet',
         'chps.flg_p_4bet_def_opp = TRUE AND chps.enum_p_4bet_action = \'F\'',
         'chps.flg_p_4bet_def_opp = TRUE',
         55.0, 1.2),
        ('Fold to CBet - OOP (SRP)',
         'chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE AND chps.position = 8 AND chps.flg_p_3bet = FALSE',
         'chps.flg_f_cbet_def_opp = TRUE AND chps.position = 8 AND chps.flg_p_3bet = FALSE',
         38.0, 1.0),
        ('CBet Flop - IP (SRP)',
         'chps.flg_f_cbet_opp = TRUE AND chps.cnt_p_raise > 0 AND chps.flg_f_has_position = TRUE AND chps.flg_p_3bet = FALSE AND chps.flg_f_cbet = TRUE',
         'chps.flg_f_cbet_opp = TRUE AND chps.cnt_p_raise > 0 AND chps.flg_f_has_position = TRUE AND chps.flg_p_3bet = FALSE',
         65.0, 1.0),
        ('Donk Lead Flop',
         'chps.flg_f_donk = TRUE',
         'chps.flg_f_donk_opp = TRUE',
         20.0, 1.0),
    ]

    scored = []
    for desc, numer_filter, denom_filter, gto_thresh, mult in leak_defs:
        num_q = ("SELECT SUM(CASE WHEN %s THEN 1 ELSE 0 END) "
                 "FROM cash_hand_player_statistics chps "
                 "JOIN player p ON p.id_player = chps.id_player "
                 "WHERE p.player_name = %%s" % numer_filter)
        den_q = ("SELECT SUM(CASE WHEN %s THEN 1 ELSE 0 END) "
                 "FROM cash_hand_player_statistics chps "
                 "JOIN player p ON p.id_player = chps.id_player "
                 "WHERE p.player_name = %%s" % denom_filter)

        cur.execute(num_q, (player,))
        numer = cur.fetchone()[0] or 0
        cur.execute(den_q, (player,))
        denom = cur.fetchone()[0] or 0

        if denom < 15:
            continue  # skip low-sample situations

        actual = (numer / denom * 100.0) if denom > 0 else 0.0
        n = denom
        dev = actual - gto_thresh

        # Direction check: only flag leaks in the correct direction
        fold_metrics = ['Fold to 3-Bet', 'Fold to 4-Bet', 'Fold to CBet']
        cbet_metrics = ['CBet Flop']
        if any(m in desc for m in fold_metrics):
            if actual <= gto_thresh:
                continue  # under-folding — not a fold leak
        elif any(m in desc for m in cbet_metrics):
            if actual >= gto_thresh:
                continue  # over-cbetting — not an under-cbet leak
        # Donk Lead: over-donking = actual > threshold
        if 'Donk' in desc and actual <= gto_thresh:
            continue

        vol_factor = max(1.0, math.log10(n))
        score = abs(dev) * vol_factor * mult
        scored.append((desc, round(dev, 1), n, mult, round(vol_factor, 2), round(score, 2)))

    scored.sort(key=lambda x: x[5], reverse=True)

    details = {}
    for i, (desc, dev, n, mult, vf, score) in enumerate(scored):
        details['#%d' % (i + 1)] = {'desc': desc, 'dev': dev, 'n': int(n),
                                    'mult': mult, 'log10_n': vf, 'score': score}

    return {'value': scored[0][5] if scored else None, 'n': 0,
            'status': 'LIVE_SQL', 'details': details}
def sit_108(cur, player):
    """
    VPIP/PFR Gap Analysis.
    Gap = VPIP% - PFR% by position.
    For blind positions (BB=8, SB=9), exclude walk hands from VPIP denominator.
    """
    # Get VPIP, PFR, and walk counts by position
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as n,
            SUM(CASE WHEN chps.flg_vpip = TRUE THEN 1 ELSE 0 END) as vpip,
            SUM(CASE WHEN chps.cnt_p_raise > 0 THEN 1 ELSE 0 END) as pfr,
            SUM(CASE WHEN chps.flg_f_saw = FALSE AND chps.flg_vpip = FALSE
                      AND chps.flg_p_face_raise = FALSE THEN 1 ELSE 0 END) as walks
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
        GROUP BY chps.position
        HAVING COUNT(*) >= 15
        ORDER BY chps.position
    """, (player,))

    rows = cur.fetchall()
    details = {}
    for pos, n, vpip, pfr, walks in rows:
        # For blind positions, exclude walks from VPIP denominator
        if pos in (8, 9):
            vpip_denom = n - walks
        else:
            vpip_denom = n
        vpip_pct = (vpip / vpip_denom * 100.0) if vpip_denom > 0 else 0.0
        pfr_pct = (pfr / n * 100.0) if n > 0 else 0.0
        gap = vpip_pct - pfr_pct
        label = POSITION_LABELS.get(pos, str(pos))

        # Assessment
        if gap > 12.0:
            assess = "PASSIVE"
        elif gap > 8.0:
            assess = "LOOSE-PASSIVE"
        elif gap > 5.0:
            assess = "STANDARD"
        elif gap > 3.0:
            assess = "TIGHT-AGRESSIVE"
        elif gap >= 0.0:
            assess = "VERY TIGHT"
        else:
            assess = "AGGRO-PASSIVE"

        details[label] = {'VPIP': round(vpip_pct, 1), 'PFR': round(pfr_pct, 1),
                          'Gap': round(gap, 1), 'Assessment': assess, 'n': n}

    total_n = sum(v['n'] for v in details.values())
    status = sample_status(total_n, 30)
    return {'value': None, 'n': total_n, 'status': status, 'details': details}


#    #45 Aggression Factor (AF) and Agg% by Position

def sit_45(cur, player):
    """
    Aggression Factor (AF) and Agg% by position.
    AF = (raises + bets) / calls (postflop).
    Agg% = aggressive_actions / (aggressive_actions + passive_actions + folds) * 100.
    Aggressive = cnt_f_raise + cnt_t_raise + cnt_r_raise + flg_f_bet + flg_t_bet + flg_r_bet.
    Passive = cnt_f_call + cnt_t_call + cnt_r_call.
    Folds = flg_f_fold + flg_t_fold + flg_r_fold (postflop folds).
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as n,
            SUM(chps.cnt_f_raise + chps.cnt_t_raise + chps.cnt_r_raise) as total_raises,
            SUM(CASE WHEN chps.flg_f_bet = TRUE THEN 1 ELSE 0 END
               + CASE WHEN chps.flg_t_bet = TRUE THEN 1 ELSE 0 END
               + CASE WHEN chps.flg_r_bet = TRUE THEN 1 ELSE 0 END) as total_bets,
            SUM(chps.cnt_f_call + chps.cnt_t_call + chps.cnt_r_call) as total_calls,
            SUM(CASE WHEN chps.flg_f_fold = TRUE THEN 1 ELSE 0 END
               + CASE WHEN chps.flg_t_fold = TRUE THEN 1 ELSE 0 END
               + CASE WHEN chps.flg_r_fold = TRUE THEN 1 ELSE 0 END) as total_folds
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.position IN (0, 1, 2, 3, 8, 9)
        GROUP BY chps.position
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    details = {}
    for pos, n, raises, bets, calls, folds in rows:
        aggressive = (raises or 0) + (bets or 0)
        total_calls = calls or 0
        total_folds = folds or 0
        # AF = (bets + raises) / calls
        if total_calls > 0:
            af = aggressive / total_calls
        elif aggressive > 0:
            af = float('inf')
        else:
            af = None
        # Agg% = aggressive / (aggressive + passive + folds) * 100
        total_actions = aggressive + total_calls + total_folds
        if total_actions > 0:
            agg_pct = aggressive / total_actions * 100.0
        else:
            agg_pct = None
        label = POSITION_LABELS.get(pos, str(pos))
        details[label] = {
            'AF': round(af, 2) if af is not None and af != float('inf') else ('Inf' if af == float('inf') else None),
            'Agg%': round(agg_pct, 1) if agg_pct is not None else None,
            'n': n,
            'aggressive': aggressive,
            'calls': total_calls,
            'folds': total_folds
        }
    total_n = sum(v['n'] for v in details.values())
    status = sample_status(total_n, 30)
    return {'value': None, 'n': total_n, 'status': status, 'details': details}


#    #56 Turn CBet Sizing Distribution (IP vs OOP)

def sit_56(cur, player):
    """
    Turn CBet Sizing Distribution (IP vs OOP).
    Filter: flg_t_cbet = TRUE AND val_t_bet_made_pct IS NOT NULL.
    Buckets: Small (<33%), Medium (33-67%), Large (67-100%), Overbet (>100%).
    Single query returning all 4 bucket counts simultaneously.
    Split by IP/OOP. n >= 15.
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_t_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            COUNT(*) as total,
            SUM(CASE WHEN chps.val_t_bet_made_pct < 33 THEN 1 ELSE 0 END) as small,
            SUM(CASE WHEN chps.val_t_bet_made_pct >= 33 AND chps.val_t_bet_made_pct < 67 THEN 1 ELSE 0 END) as medium,
            SUM(CASE WHEN chps.val_t_bet_made_pct >= 67 AND chps.val_t_bet_made_pct < 100 THEN 1 ELSE 0 END) as large,
            SUM(CASE WHEN chps.val_t_bet_made_pct >= 100 THEN 1 ELSE 0 END) as overbet
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_t_cbet = TRUE
          AND chps.val_t_bet_made_pct IS NOT NULL
        GROUP BY chps.flg_t_has_position
        ORDER BY chps.flg_t_has_position DESC
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_all = 0
    small_all = 0
    medium_all = 0
    large_all = 0
    overbet_all = 0
    for pos_type, total, small, medium, large, overbet in rows:
        if total < 15:
            continue
        pct_small = (small / total * 100.0) if total > 0 else None
        pct_medium = (medium / total * 100.0) if total > 0 else None
        pct_large = (large / total * 100.0) if total > 0 else None
        pct_over = (overbet / total * 100.0) if total > 0 else None
        details[pos_type] = {
            'value': pct_small, 'n': total,
            'small_pct': pct_small, 'medium_pct': pct_medium,
            'large_pct': pct_large, 'overbet_pct': pct_over
        }
        total_all += total
        small_all += small
        medium_all += medium
        large_all += large
        overbet_all += overbet
    overall = (small_all / total_all * 100.0) if total_all >= 15 else None
    status = sample_status(total_all, 15)
    return {'value': overall, 'n': total_all, 'status': status, 'details': details}


#    #57 River CBet Sizing Distribution (IP vs OOP)

def sit_57(cur, player):
    """
    River CBet Sizing Distribution (IP vs OOP).
    Filter: flg_r_cbet = TRUE AND val_r_bet_made_pct IS NOT NULL.
    Buckets: Small (<33%), Medium (33-67%), Large (67-100%), Overbet (>100%).
    Single query returning all 4 bucket counts simultaneously.
    Split by IP/OOP. n >= 15.
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_r_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            COUNT(*) as total,
            SUM(CASE WHEN chps.val_r_bet_made_pct < 33 THEN 1 ELSE 0 END) as small,
            SUM(CASE WHEN chps.val_r_bet_made_pct >= 33 AND chps.val_r_bet_made_pct < 67 THEN 1 ELSE 0 END) as medium,
            SUM(CASE WHEN chps.val_r_bet_made_pct >= 67 AND chps.val_r_bet_made_pct < 100 THEN 1 ELSE 0 END) as large,
            SUM(CASE WHEN chps.val_r_bet_made_pct >= 100 THEN 1 ELSE 0 END) as overbet
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_r_cbet = TRUE
          AND chps.val_r_bet_made_pct IS NOT NULL
        GROUP BY chps.flg_r_has_position
        ORDER BY chps.flg_r_has_position DESC
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_all = 0
    small_all = 0
    medium_all = 0
    large_all = 0
    overbet_all = 0
    for pos_type, total, small, medium, large, overbet in rows:
        if total < 15:
            continue
        pct_small = (small / total * 100.0) if total > 0 else None
        pct_medium = (medium / total * 100.0) if total > 0 else None
        pct_large = (large / total * 100.0) if total > 0 else None
        pct_over = (overbet / total * 100.0) if total > 0 else None
        details[pos_type] = {
            'value': pct_small, 'n': total,
            'small_pct': pct_small, 'medium_pct': pct_medium,
            'large_pct': pct_large, 'overbet_pct': pct_over
        }
        total_all += total
        small_all += small
        medium_all += medium
        large_all += large
        overbet_all += overbet
    overall = (small_all / total_all * 100.0) if total_all >= 15 else None
    status = sample_status(total_all, 15)
    return {'value': overall, 'n': total_all, 'status': status, 'details': details}


#    #72 Board Rank Filters (CBet% and Fold%)

def sit_72(cur, player):
    """
    CBet% IP and Fold to CBet% OOP by board rank category.
    SRP, HU only. Decode card_1/card_2/card_3 from cash_hand_summary.
    rank = card % 13 (0=2...12=A).
    Categories: Ace-high (max=12), King-high (max=11 no A), Queen-high (max=10 no A/K), Other.
    CBet% = flg_f_cbet / flg_f_cbet_opp (IP, PFR).
    Fold CBet% = flg_f_fold / flg_f_cbet_def_opp (OOP, defender).
    n >= 30 per category.
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            CASE WHEN chps.flg_f_cbet_opp = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END as cbet_opp_ip,
            CASE WHEN chps.flg_f_cbet = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END as cbet_ip,
            CASE WHEN chps.flg_f_cbet_def_opp = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END as def_opp_oop,
            CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END as fold_oop
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_p_3bet = FALSE
          AND chs.cnt_players_f = 2
    """, (player,))
    raw = cur.fetchall()

    buckets = {}
    for c1, c2, c3, cbet_opp_ip, cbet_ip, def_opp_oop, fold_oop in raw:
        rank_cat = classify_board_rank(c1, c2, c3)
        if rank_cat is None:
            continue
        if rank_cat not in buckets:
            buckets[rank_cat] = {'cbet_opp_ip': 0, 'cbet_ip': 0,
                                  'def_opp_oop': 0, 'fold_oop': 0}
        b = buckets[rank_cat]
        b['cbet_opp_ip'] += cbet_opp_ip
        b['cbet_ip'] += cbet_ip
        b['def_opp_oop'] += def_opp_oop
        b['fold_oop'] += fold_oop

    details = {}
    total_cbet_opp = 0
    total_cbets = 0
    total_def_opp = 0
    total_folds = 0
    for cat in ['Ace-high', 'King-high', 'Queen-high', 'Other']:
        b = buckets.get(cat)
        if b is None:
            continue
        cbet_pct = (b['cbet_ip'] / b['cbet_opp_ip'] * 100.0) if b['cbet_opp_ip'] >= 30 else None
        fold_pct = (b['fold_oop'] / b['def_opp_oop'] * 100.0) if b['def_opp_oop'] >= 30 else None
        n_combined = b['cbet_opp_ip'] + b['def_opp_oop']
        details[cat] = {
            'value': cbet_pct, 'n': n_combined,
            'cbet_ip_pct': cbet_pct, 'fold_oop_pct': fold_pct,
            'cbet_opp_ip': b['cbet_opp_ip'], 'def_opp_oop': b['def_opp_oop']
        }
        total_cbet_opp += b['cbet_opp_ip']
        total_cbets += b['cbet_ip']
        total_def_opp += b['def_opp_oop']
        total_folds += b['fold_oop']

    overall = (total_cbets / total_cbet_opp * 100.0) if total_cbet_opp > 0 else None
    status = sample_status(total_cbet_opp + total_def_opp, 30)
    return {'value': overall, 'n': total_cbet_opp + total_def_opp, 'status': status, 'details': details}


#    #92b Profit by Pot Type (bb/100)

def sit_92b(cur, player):
    """
    Profit (bb/100) by pot type: SRP, 3BP, Squeeze.
    No SRP/HU filter. Uses ALL cash hands.
    SRP: flg_p_3bet=FALSE AND flg_p_squeeze=FALSE.
    3BP: flg_p_3bet=TRUE.
    Squeeze: flg_p_squeeze=TRUE.
    Formula: (SUM(amt_won) / AVG(cl.amt_bb)) * 100.0 / COUNT(*)
    JOIN cash_limit cl ON chps.id_limit = cl.id_limit.
    n >= 50 per pot type.
    """
    cur.execute("""
        SELECT
            CASE
                WHEN chps.flg_p_squeeze = TRUE THEN 'Squeeze'
                WHEN chps.flg_p_3bet = TRUE THEN '3BP'
                ELSE 'SRP'
            END as pot_type,
            COUNT(*) as n,
            SUM(chps.amt_won) as net_won,
            AVG(cl.amt_bb) as avg_bb
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_limit cl ON chps.id_limit = cl.id_limit
        WHERE p.player_name = %s
          AND cl.amt_bb > 0
        GROUP BY pot_type
        HAVING COUNT(*) >= 50
        ORDER BY pot_type
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_won = 0
    total_bb_weighted = 0
    for pot_type, n, net_won, avg_bb in rows:
        net_won = float(net_won or 0)
        avg_bb = float(avg_bb or 0)
        bb100 = (net_won / avg_bb * 100.0 / n) if n > 0 and avg_bb > 0 else None
        pe_status = 'SUSPICIOUS' if bb100 is not None and abs(bb100) > 500 else 'OK'
        details[pot_type] = {'value': bb100, 'n': n, 'pe_status': pe_status}
        total_n += n
        total_won += net_won
        total_bb_weighted += avg_bb * n
    overall_avg_bb = total_bb_weighted / total_n if total_n > 0 else 0
    overall = (total_won / overall_avg_bb * 100.0 / total_n) if total_n > 0 and overall_avg_bb > 0 else None
    status = 'VARIANCE: bb/100=%.1f SUSPICIOUS' % overall if overall is not None and abs(overall) > 500 else sample_status(total_n, 50)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #46 Raise vs River CBet (Fold/Call/Raise by IP/OOP)

def sit_46(cur, player):
    """
    Response to river CBet: Fold/Call/Raise split by IP/OOP.
    Filter: flg_r_cbet_def_opp = TRUE (faced river cbet).
    Fold% = flg_r_fold / total, Raise% = flg_r_check_raise / total.
    Call% = total - folds - raises.
    Validate: Fold + Call + Raise ≈ 100%. n >= 15.
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_r_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_r_fold = TRUE THEN 1 ELSE 0 END) as folds,
            SUM(CASE WHEN chps.flg_r_check_raise = TRUE THEN 1 ELSE 0 END) as raises
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_r_cbet_def_opp = TRUE
        GROUP BY chps.flg_r_has_position
        ORDER BY chps.flg_r_has_position DESC
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_all = 0
    folds_all = 0
    raises_all = 0
    for pos_type, total, folds, raises in rows:
        if total < 15:
            continue
        calls = total - folds - raises
        fold_pct = folds / total * 100.0
        raise_pct = raises / total * 100.0
        call_pct = calls / total * 100.0
        tolerance = 5.0
        sum_pct = fold_pct + call_pct + raise_pct
        ok = abs(sum_pct - 100.0) <= tolerance
        details[pos_type] = {
            'value': fold_pct, 'n': total,
            'fold_pct': round(fold_pct, 1),
            'call_pct': round(call_pct, 1),
            'raise_pct': round(raise_pct, 1),
            'valid': ok
        }
        total_all += total
        folds_all += folds
        raises_all += raises
    calls_all = total_all - folds_all - raises_all
    overall = (folds_all / total_all * 100.0) if total_all >= 15 else None
    status = sample_status(total_all, 15)
    return {'value': overall, 'n': total_all, 'status': status, 'details': details}


#    #64 Multiway Flop (3+ players) CBet and Fold

def sit_64(cur, player):
    """
    Multiway Flop: CBet% IP and Fold to CBet% OOP, split by 3-way vs 4+.
    Filter: chs.cnt_players_f >= 3 (no HU filter).
    CBet% IP: flg_f_cbet / flg_f_cbet_opp WHERE flg_f_has_position = TRUE.
    Fold to CBet% OOP: flg_f_fold / flg_f_cbet_def_opp WHERE flg_f_has_position = FALSE.
    Split by cnt_players_f = 3 vs cnt_players_f >= 4.
    n >= 15 per bucket.
    """
    cur.execute("""
        SELECT
            CASE WHEN chs.cnt_players_f = 3 THEN '3way' ELSE '4plus' END as mw_type,
            SUM(CASE WHEN chps.flg_f_cbet_opp = TRUE AND chps.flg_f_has_position = TRUE
                      AND chps.flg_p_first_raise = TRUE THEN 1 ELSE 0 END) as cbet_opp_ip,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE AND chps.flg_f_has_position = TRUE
                      AND chps.flg_p_first_raise = TRUE THEN 1 ELSE 0 END) as cbet_ip,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END) as def_opp_oop,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END) as fold_oop
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chs.cnt_players_f >= 3
          AND chps.flg_p_3bet = FALSE
        GROUP BY mw_type
        ORDER BY mw_type
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    for mw_type, cbet_opp_ip, cbet_ip, def_opp_oop, fold_oop in rows:
        cbet_pct = (cbet_ip / cbet_opp_ip * 100.0) if cbet_opp_ip >= 15 else None
        fold_pct = (fold_oop / def_opp_oop * 100.0) if def_opp_oop >= 15 else None
        n_combined = cbet_opp_ip + def_opp_oop
        details[mw_type] = {
            'value': cbet_pct, 'n': n_combined,
            'cbet_ip_pct': cbet_pct, 'fold_oop_pct': fold_pct,
            'cbet_opp_ip': cbet_opp_ip, 'def_opp_oop': def_opp_oop
        }
        total_n += n_combined
    status = sample_status(total_n, 15)
    return {'value': None, 'n': total_n, 'status': status, 'details': details}


#    #70 Bet-Fold % (bet then fold to raise) by Street x IP/OOP

def sit_70(cur, player):
    """
    Bet-Fold %: bet then fold to raise, by Street x IP/OOP.
    Flop: flg_f_cbet = TRUE AND flg_f_face_raise = TRUE (denominator),
          flg_f_cbet = TRUE AND flg_f_face_raise = TRUE AND flg_f_fold = TRUE (numerator).
    Turn: flg_t_bet = TRUE AND flg_t_face_raise = TRUE (denominator),
          numerator adds flg_t_fold = TRUE.
    Split by Street (Flop/Turn) x IP/OOP. n >= 15.
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            'Flop' as street,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE AND chps.flg_f_face_raise = TRUE THEN 1 ELSE 0 END) as denom,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE AND chps.flg_f_face_raise = TRUE
                      AND chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as numer
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet = TRUE
          AND chps.flg_f_face_raise = TRUE
        GROUP BY chps.flg_f_has_position

        UNION ALL

        SELECT
            CASE WHEN chps.flg_t_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            'Turn' as street,
            SUM(CASE WHEN chps.flg_t_bet = TRUE AND chps.flg_t_face_raise = TRUE THEN 1 ELSE 0 END) as denom,
            SUM(CASE WHEN chps.flg_t_bet = TRUE AND chps.flg_t_face_raise = TRUE
                      AND chps.flg_t_fold = TRUE THEN 1 ELSE 0 END) as numer
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_t_bet = TRUE
          AND chps.flg_t_face_raise = TRUE
        GROUP BY chps.flg_t_has_position
        ORDER BY street, pos_type
    """, (player, player))
    rows = cur.fetchall()
    details = {}
    total_numer = 0
    total_denom = 0
    for pos_type, street, denom, numer in rows:
        key = f'{street} {pos_type}'
        pct = (numer / denom * 100.0) if denom >= 15 else None
        details[key] = {'value': pct, 'n': denom, 'bet_fold_pct': pct}
        total_numer += numer
        total_denom += denom
    overall = (total_numer / total_denom * 100.0) if total_denom >= 15 else None
    status = sample_status(total_denom, 15)
    return {'value': overall, 'n': total_denom, 'status': status, 'details': details}


#    #85 Fold to River Bet (non-CBet) by IP/OOP

def sit_85(cur, player):
    """
    Fold to River Bet (non-CBet / donk bet) by IP/OOP.
    Filter: flg_r_saw = TRUE AND flg_r_cbet_def_opp = FALSE AND flg_r_face_raise = TRUE.
    This captures folds vs a river lead/donk, not a standard cbet.
    Fold% = flg_r_fold / total. Split by IP/OOP. n >= 15.
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_r_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_r_fold = TRUE THEN 1 ELSE 0 END) as folds
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_r_saw = TRUE
          AND chps.flg_r_cbet_def_opp = FALSE
          AND chps.flg_r_face_raise = TRUE
        GROUP BY chps.flg_r_has_position
        ORDER BY chps.flg_r_has_position DESC
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_all = 0
    folds_all = 0
    for pos_type, total, folds in rows:
        if total < 15:
            continue
        fold_pct = folds / total * 100.0
        details[pos_type] = {'value': fold_pct, 'n': total, 'fold_pct': round(fold_pct, 1)}
        total_all += total
        folds_all += folds
    overall = (folds_all / total_all * 100.0) if total_all >= 15 else None
    status = sample_status(total_all, 15)
    return {'value': overall, 'n': total_all, 'status': status, 'details': details}


#    #48 Donk after PFR checks (OOP) by Seat x Texture

def sit_48(cur, player):
    """
    Donk bet after PFR checks flop, OOP only.
    Filter: flg_f_cbet_opp = TRUE AND flg_f_cbet = FALSE (PFR checked) AND flg_f_has_position = FALSE.
    Donk% = flg_f_donk / total OOP hands where PFR checked.
    Split by Seat position x Board Texture. n >= 15.
    """
    cur.execute("""
        SELECT
            chps.position,
            chs.card_1, chs.card_2, chs.card_3,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_f_donk = TRUE THEN 1 ELSE 0 END) as donks
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_opp = TRUE
          AND chps.flg_f_cbet = FALSE
          AND chps.flg_f_has_position = FALSE
        GROUP BY chps.position, chs.card_1, chs.card_2, chs.card_3
    """, (player,))
    raw = cur.fetchall()
    buckets = {}
    for pos, c1, c2, c3, total, donks in raw:
        tex = classify_board_texture(c1, c2, c3)
        if tex is None:
            continue
        pos_name = {0: 'BTN', 1: 'CO', 2: 'MP3', 3: 'MP2', 4: 'MP1',
                    5: 'UTG2', 6: 'UTG1', 7: 'UTG', 8: 'BB', 9: 'SB'}.get(pos, f'Pos{pos}')
        key = f'{pos_name}/{tex}'
        if key not in buckets:
            buckets[key] = {'total': 0, 'donks': 0}
        buckets[key]['total'] += total
        buckets[key]['donks'] += donks

    details = {}
    total_all = 0
    donks_all = 0
    for key in sorted(buckets):
        b = buckets[key]
        if b['total'] < 15:
            continue
        pct = b['donks'] / b['total'] * 100.0
        details[key] = {'value': round(pct, 1), 'n': b['total'], 'donk_pct': round(pct, 1)}
        total_all += b['total']
        donks_all += b['donks']
    overall = (donks_all / total_all * 100.0) if total_all >= 15 else None
    status = sample_status(total_all, 15)
    return {'value': overall, 'n': total_all, 'status': status, 'details': details}


#    #50 True Cold Caller OOP (non-blind)

def sit_50(cur, player):
    """
    Cold Caller OOP (non-blind) postflop: Fold to CBet% and Check-Raise%.
    Filter: flg_p_ccall = TRUE AND position NOT IN (8, 9) AND flg_f_has_position = FALSE.
    Fold CBet%: flg_f_fold / flg_f_cbet_def_opp.
    Check-Raise%: flg_f_check_raise / flg_f_cbet_def_opp.
    Split by Seat position. n >= 15.
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as folds_cbet,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE THEN 1 ELSE 0 END) as cbet_def_opp,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_check_raise = TRUE THEN 1 ELSE 0 END) as xr
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_ccall = TRUE
          AND chps.position NOT IN (8, 9)
          AND chps.flg_f_has_position = FALSE
        GROUP BY chps.position
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    pos_map = {0: 'BTN', 1: 'CO', 2: 'MP3', 3: 'MP2', 4: 'MP1',
              5: 'UTG2', 6: 'UTG1', 7: 'UTG'}
    details = {}
    total_all = 0
    for pos, total, folds_cbet, cbet_def_opp, xr in rows:
        if total < 15:
            continue
        pos_name = pos_map.get(pos, f'Pos{pos}')
        fold_cbet_pct = (folds_cbet / cbet_def_opp * 100.0) if cbet_def_opp >= 15 else None
        xr_pct = (xr / cbet_def_opp * 100.0) if cbet_def_opp >= 15 else None
        details[pos_name] = {
            'value': fold_cbet_pct, 'n': total,
            'fold_cbet_pct': round(fold_cbet_pct, 1) if fold_cbet_pct is not None else None,
            'xr_pct': round(xr_pct, 1) if xr_pct is not None else None,
            'cbet_def_opp': cbet_def_opp
        }
        total_all += total
    status = sample_status(total_all, 15)
    return {'value': None, 'n': total_all, 'status': status, 'details': details}


#    #63 4-Bettor Postflop Profile (IP/OOP)

def sit_63(cur, player):
    """
    4-Bettor postflop: Flop CBet% and Turn CBet% in 4-bet pots (HU).
    Filter: flg_p_4bet = TRUE AND chs.cnt_players_f = 2.
    Flop CBet%: flg_f_cbet / flg_f_cbet_opp.
    Turn CBet%: flg_t_cbet / flg_t_cbet_opp.
    Split by IP/OOP. n >= 10 (4-bet pots rare).
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            SUM(CASE WHEN chps.flg_f_cbet_opp = TRUE THEN 1 ELSE 0 END) as f_cbet_opp,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE THEN 1 ELSE 0 END) as f_cbet,
            SUM(CASE WHEN chps.flg_t_cbet_opp = TRUE THEN 1 ELSE 0 END) as t_cbet_opp,
            SUM(CASE WHEN chps.flg_t_cbet = TRUE THEN 1 ELSE 0 END) as t_cbet
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_p_4bet = TRUE
          AND chs.cnt_players_f = 2
        GROUP BY chps.flg_f_has_position
        ORDER BY chps.flg_f_has_position DESC
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    for pos_type, f_cbet_opp, f_cbet, t_cbet_opp, t_cbet in rows:
        f_pct = (f_cbet / f_cbet_opp * 100.0) if f_cbet_opp >= 10 else None
        t_pct = (t_cbet / t_cbet_opp * 100.0) if t_cbet_opp >= 10 else None
        n = f_cbet_opp + t_cbet_opp
        details[pos_type] = {
            'value': f_pct, 'n': n,
            'f_cbet_pct': round(f_pct, 1) if f_pct is not None else None,
            't_cbet_pct': round(t_pct, 1) if t_pct is not None else None,
            'f_cbet_opp': f_cbet_opp, 't_cbet_opp': t_cbet_opp
        }
        total_n += n
    status = sample_status(total_n, 10)
    return {'value': None, 'n': total_n, 'status': status, 'details': details}


#    #65 Limp Pot Profile

def sit_65(cur, player):
    """
    Limp pot profile: Bet% and Fold to CBet% as limper.
    Filter: flg_p_limp = TRUE AND flg_p_face_raise = FALSE (limped, no raise).
    Bet%: flg_f_bet / flg_f_saw.
    Fold to CBet%: flg_f_fold / flg_f_cbet_def_opp.
    n >= 15.
    """
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_f_saw = TRUE THEN 1 ELSE 0 END) as saw_flop,
            SUM(CASE WHEN chps.flg_f_bet = TRUE THEN 1 ELSE 0 END) as bet_flop,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE THEN 1 ELSE 0 END) as cbet_def_opp,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as fold_cbet
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_p_limp = TRUE
          AND chps.flg_p_face_raise = FALSE
    """, (player,))
    row = cur.fetchone()
    total, saw_flop, bet_flop, cbet_def_opp, fold_cbet = row
    total = total or 0
    saw_flop = saw_flop or 0
    bet_flop = bet_flop or 0
    cbet_def_opp = cbet_def_opp or 0
    fold_cbet = fold_cbet or 0

    bet_pct = (bet_flop / saw_flop * 100.0) if saw_flop >= 15 else None
    fold_pct = (fold_cbet / cbet_def_opp * 100.0) if cbet_def_opp >= 15 else None
    status = sample_status(total, 15)
    return {
        'value': bet_pct, 'n': total, 'status': status,
        'details': {
            'bet_pct': round(bet_pct, 1) if bet_pct is not None else None,
            'saw_flop': saw_flop,
            'fold_cbet_pct': round(fold_pct, 1) if fold_pct is not None else None,
            'cbet_def_opp': cbet_def_opp
        }
    }


#    #67 Next Action after Check-Raise (Turn)

def sit_67(cur, player):
    """
    Turn action after flop check-raise OOP.
    Filter: flg_f_check_raise = TRUE (check-raised flop OOP).
    Turn: Bet% (flg_t_bet), Check-Fold% (flg_t_fold AND NOT flg_t_bet),
    Check-Call% derived: total - bet - fold.
    Validate sum ≈ 100%. n >= 15.
    """
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_t_bet = TRUE THEN 1 ELSE 0 END) as t_bet,
            SUM(CASE WHEN chps.flg_t_fold = TRUE AND chps.flg_t_bet = FALSE THEN 1 ELSE 0 END) as t_chk_fold,
            SUM(CASE WHEN chps.flg_t_bet = FALSE AND chps.flg_t_fold = FALSE
                      AND chps.flg_t_check_raise = FALSE THEN 1 ELSE 0 END) as t_chk_call
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_check_raise = TRUE
    """, (player,))
    row = cur.fetchone()
    total, t_bet, t_chk_fold, t_chk_call = row
    total = total or 0
    t_bet = t_bet or 0
    t_chk_fold = t_chk_fold or 0
    t_chk_call = t_chk_call or 0
    if total < 15:
        return {'value': None, 'n': total, 'status': 'EXCLUDE', 'details': {}}
    bet_pct = t_bet / total * 100.0
    fold_pct = t_chk_fold / total * 100.0
    call_pct = t_chk_call / total * 100.0
    sum_pct = bet_pct + fold_pct + call_pct
    valid = abs(sum_pct - 100.0) <= 10.0
    details = {
        'Bet': {'value': round(bet_pct, 1), 'n': t_bet},
        'Check-Fold': {'value': round(fold_pct, 1), 'n': t_chk_fold},
        'Check-Call': {'value': round(call_pct, 1), 'n': t_chk_call},
    }
    if valid:
        status = sample_status(total, 15)
    else:
        status = 'SUSPICIOUS: FCR sum=%.1f%%' % sum_pct
    return {'value': round(bet_pct, 1), 'n': total, 'status': status, 'details': details}


#    #68 Sizing Tell by Profit (bb/100 per CBet size bucket)

def sit_68(cur, player):
    """
    Profit (bb/100) per flop CBet size bucket, split by IP/OOP.
    Filter: flg_f_cbet = TRUE AND val_f_bet_made_pct IS NOT NULL.
    Buckets: Small (<33%), Medium (33-67%), Large (67-100%), Overbet (>100%).
    bb/100 = (SUM(amt_won) / AVG(cl.amt_bb)) * 100 / COUNT(*).
    JOIN cash_limit cl ON chps.id_limit = cl.id_limit.
    n >= 15 per bucket.
    """
    cur.execute("""
        SELECT
            CASE WHEN chps.flg_f_has_position = TRUE THEN 'IP' ELSE 'OOP' END as pos_type,
            CASE
                WHEN chps.val_f_bet_made_pct < 33 THEN 'Small'
                WHEN chps.val_f_bet_made_pct >= 33 AND chps.val_f_bet_made_pct < 67 THEN 'Medium'
                WHEN chps.val_f_bet_made_pct >= 67 AND chps.val_f_bet_made_pct < 100 THEN 'Large'
                ELSE 'Overbet'
            END as bucket,
            COUNT(*) as n,
            SUM(chps.amt_won) as net_won,
            AVG(cl.amt_bb) as avg_bb
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_limit cl ON chps.id_limit = cl.id_limit
        WHERE p.player_name = %s
          AND chps.flg_f_cbet = TRUE
          AND chps.val_f_bet_made_pct IS NOT NULL
          AND cl.amt_bb > 0
        GROUP BY pos_type, bucket
        ORDER BY pos_type, bucket
    """, (player,))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    for pos_type, bucket, n, net_won, avg_bb in rows:
        key = f'{pos_type}/{bucket}'
        if n < 15:
            continue
        net_won = float(net_won or 0)
        avg_bb = float(avg_bb or 0)
        bb100 = (net_won / avg_bb * 100.0 / n) if avg_bb > 0 else None
        bb100_status = 'SUSPICIOUS' if bb100 is not None and abs(bb100) > 500 else 'OK'
        details[key] = {
            'value': round(bb100, 1) if bb100 is not None else None,
            'n': n, 'pe_status': bb100_status
        }
        total_n += n
    status = sample_status(total_n, 15)
    return {'value': None, 'n': total_n, 'status': status, 'details': details}


#    #69 MDF Over/Under-fold by CBet size faced

def sit_69(cur, player):
    """
    MDF analysis: actual fold% vs maximum defend frequency by CBet size faced.
    Filter: flg_f_cbet_def_opp = TRUE AND val_f_bet_facing_pct IS NOT NULL.
    Size buckets faced: Small (<33%), Medium (33-67%), Large (67-100%).
    MDF thresholds: Small→max_fold=25%, Medium→max_fold=33%, Large→max_fold=50%.
    Actual fold%: flg_f_fold / total per bucket.
    Flag: OVER-FOLDING, CORRECT, or UNDER-FOLDING. n >= 15 per bucket.
    """
    cur.execute("""
        SELECT
            CASE
                WHEN chps.val_f_bet_facing_pct < 33 THEN 'Small'
                WHEN chps.val_f_bet_facing_pct >= 33 AND chps.val_f_bet_facing_pct < 67 THEN 'Medium'
                ELSE 'Large'
            END as bucket,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as folds
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
          AND chps.val_f_bet_facing_pct IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket
    """, (player,))
    rows = cur.fetchall()
    max_fold_map = {'Small': 25.0, 'Medium': 33.0, 'Large': 50.0}
    details = {}
    total_n = 0
    total_folds = 0
    for bucket, total, folds in rows:
        if total < 15:
            continue
        actual_fold_pct = folds / total * 100.0
        max_fold = max_fold_map.get(bucket, 33.0)
        if actual_fold_pct > max_fold + 5.0:
            flag = 'OVER-FOLDING'
        elif actual_fold_pct < max_fold - 15.0:
            flag = 'UNDER-FOLDING'
        else:
            flag = 'CORRECT'
        details[bucket] = {
            'value': round(actual_fold_pct, 1), 'n': total,
            'actual_fold_pct': round(actual_fold_pct, 1),
            'max_fold_pct': max_fold, 'flag': flag
        }
        total_n += total
        total_folds += folds
    overall = (total_folds / total_n * 100.0) if total_n >= 15 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #71 Check-Call vs Check-Fold per Street (OOP defender)

def sit_71(cur, player):
    """
    Check-Call vs Check-Fold as OOP defender per street.
    Flop: flg_f_cbet_def_opp = TRUE (faced cbet, didn't raise = check-called/folded).
    Turn: flg_t_cbet_def_opp = TRUE.
    River: flg_r_cbet_def_opp = TRUE.
    CheckCall% = (1 - fold%) among non-raisers.
    Split by Street. n >= 15.
    """
    cur.execute("""
        SELECT
            'Flop' as street,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_f_fold = TRUE THEN 1 ELSE 0 END) as folds,
            SUM(CASE WHEN chps.flg_f_check_raise = TRUE THEN 1 ELSE 0 END) as raises
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_f_cbet_def_opp = TRUE
          AND chps.flg_f_has_position = FALSE

        UNION ALL

        SELECT
            'Turn' as street,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_t_fold = TRUE THEN 1 ELSE 0 END) as folds,
            SUM(CASE WHEN chps.flg_t_check_raise = TRUE THEN 1 ELSE 0 END) as raises
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_t_cbet_def_opp = TRUE
          AND chps.flg_t_has_position = FALSE

        UNION ALL

        SELECT
            'River' as street,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_r_fold = TRUE THEN 1 ELSE 0 END) as folds,
            SUM(CASE WHEN chps.flg_r_check_raise = TRUE THEN 1 ELSE 0 END) as raises
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_r_cbet_def_opp = TRUE
          AND chps.flg_r_has_position = FALSE
        ORDER BY street
    """, (player, player, player))
    rows = cur.fetchall()
    details = {}
    total_n = 0
    total_calls = 0
    for street, total, folds, raises in rows:
        if total < 15:
            details[street] = {'value': None, 'n': total, 'status': 'EXCLUDE'}
            continue
        non_raised = total - (raises or 0)
        calls = non_raised - (folds or 0)
        fold_pct = (folds / total * 100.0) if total > 0 else 0
        call_pct = (calls / total * 100.0) if total > 0 else 0
        details[street] = {
            'value': round(call_pct, 1), 'n': total,
            'fold_pct': round(fold_pct, 1),
            'call_pct': round(call_pct, 1),
            'calls': calls, 'folds': folds, 'raises': raises or 0
        }
        total_n += total
        total_calls += calls
    overall = (total_calls / total_n * 100.0) if total_n >= 15 else None
    status = sample_status(total_n, 15)
    return {'value': overall, 'n': total_n, 'status': status, 'details': details}


#    #32 River Check-Raise % (OOP by Seat)

def sit_32(cur, player):
    """
    River Check-Raise % by seat position (OOP only — IP cannot check-raise).
    Filter: flg_r_saw = TRUE AND flg_f_has_position = FALSE.
    XR% = flg_r_check_raise / total river hands OOP.
    Split by Seat position. n >= 15.
    """
    cur.execute("""
        SELECT
            chps.position,
            COUNT(*) as total,
            SUM(CASE WHEN chps.flg_r_check_raise = TRUE THEN 1 ELSE 0 END) as xr
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
          AND chps.flg_r_saw = TRUE
          AND chps.flg_f_has_position = FALSE
        GROUP BY chps.position
        ORDER BY chps.position
    """, (player,))
    rows = cur.fetchall()
    pos_map = {0: 'BTN', 1: 'CO', 2: 'MP3', 3: 'MP2', 4: 'MP1',
              5: 'UTG2', 6: 'UTG1', 7: 'UTG', 8: 'BB', 9: 'SB'}
    details = {}
    total_all = 0
    xr_all = 0
    for pos, total, xr in rows:
        if total < 15:
            continue
        pos_name = pos_map.get(pos, f'Pos{pos}')
        pct = xr / total * 100.0
        details[pos_name] = {'value': round(pct, 1), 'n': total, 'xr_pct': round(pct, 1)}
        total_all += total
        xr_all += xr
    overall = (xr_all / total_all * 100.0) if total_all >= 15 else None
    status = sample_status(total_all, 15)
    return {'value': overall, 'n': total_all, 'status': status, 'details': details}


#    #44 Raise-to-Call Ratio (postflop)

def sit_44(cur, player):
    """
    Postflop Raise-to-Call Ratio.
    Raises: SUM(cnt_f_raise + cnt_t_raise + cnt_r_raise).
    Calls: SUM(cnt_f_call + cnt_t_call + cnt_r_call).
    Ratio = raises / calls. Overall metric, no split. n >= 50.
    """
    cur.execute("""
        SELECT
            SUM(COALESCE(chps.cnt_f_raise, 0) + COALESCE(chps.cnt_t_raise, 0) + COALESCE(chps.cnt_r_raise, 0)) as total_raises,
            SUM(COALESCE(chps.cnt_f_call, 0) + COALESCE(chps.cnt_t_call, 0) + COALESCE(chps.cnt_r_call, 0)) as total_calls,
            COUNT(*) as n
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        WHERE p.player_name = %s
    """, (player,))
    row = cur.fetchone()
    total_raises, total_calls, n = row
    total_raises = total_raises or 0
    total_calls = total_calls or 0
    n = n or 0
    if total_calls == 0 or n < 50:
        ratio = None
        status = sample_status(n, 50)
    else:
        ratio = total_raises / total_calls
        status = sample_status(n, 50)
    return {
        'value': round(ratio, 2) if ratio is not None else None,
        'n': n, 'status': status,
        'details': {
            'raises': int(total_raises),
            'calls': int(total_calls),
            'ratio': round(ratio, 2) if ratio is not None else None
        }
    }


#    #60 High vs Low Card Flop (CBet% and Fold%)

def sit_60(cur, player):
    """
    CBet% IP and Fold to CBet% OOP on High vs Low flops. SRP, HU only.
    HIGH: max(card rank) >= 8 (Ten+). LOW: max(card rank) < 8.
    Decode cards from cash_hand_summary: rank = card % 13.
    JOIN cash_hand_summary chs. n >= 30 per category.
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            SUM(CASE WHEN chps.flg_f_cbet_opp = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END) as cbet_opp_ip,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END) as cbet_ip,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END) as def_opp_oop,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END) as fold_oop
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_p_3bet = FALSE
          AND chs.cnt_players_f = 2
        GROUP BY chs.card_1, chs.card_2, chs.card_3
    """, (player,))
    raw = cur.fetchall()
    buckets = {}
    for c1, c2, c3, cbet_opp_ip, cbet_ip, def_opp_oop, fold_oop in raw:
        if None in (c1, c2, c3):
            continue
        ranks = [c % 13 for c in [c1, c2, c3]]
        max_rank = max(ranks)
        cat = 'HIGH' if max_rank >= 8 else 'LOW'
        if cat not in buckets:
            buckets[cat] = {'cbet_opp_ip': 0, 'cbet_ip': 0, 'def_opp_oop': 0, 'fold_oop': 0}
        b = buckets[cat]
        b['cbet_opp_ip'] += cbet_opp_ip
        b['cbet_ip'] += cbet_ip
        b['def_opp_oop'] += def_opp_oop
        b['fold_oop'] += fold_oop

    details = {}
    total_n = 0
    for cat in ['HIGH', 'LOW']:
        b = buckets.get(cat)
        if b is None:
            continue
        cbet_pct = (b['cbet_ip'] / b['cbet_opp_ip'] * 100.0) if b['cbet_opp_ip'] >= 30 else None
        fold_pct = (b['fold_oop'] / b['def_opp_oop'] * 100.0) if b['def_opp_oop'] >= 30 else None
        n_combined = b['cbet_opp_ip'] + b['def_opp_oop']
        details[cat] = {
            'value': cbet_pct, 'n': n_combined,
            'cbet_ip_pct': round(cbet_pct, 1) if cbet_pct is not None else None,
            'fold_oop_pct': round(fold_pct, 1) if fold_pct is not None else None,
            'cbet_opp_ip': b['cbet_opp_ip'], 'def_opp_oop': b['def_opp_oop']
        }
        total_n += n_combined
    status = sample_status(total_n, 30)
    return {'value': None, 'n': total_n, 'status': status, 'details': details}


#    #61 Connected vs Disconnected Flop (CBet% and Fold%)

def sit_61(cur, player):
    """
    CBet% IP and Fold to CBet% OOP on Connected vs Disconnected flops. SRP, HU only.
    Connected: any 2 cards within 2 ranks of each other.
    Disconnected: all pairs more than 2 ranks apart.
    Decode cards from cash_hand_summary. n >= 30 per category.
    """
    cur.execute("""
        SELECT
            chs.card_1, chs.card_2, chs.card_3,
            SUM(CASE WHEN chps.flg_f_cbet_opp = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END) as cbet_opp_ip,
            SUM(CASE WHEN chps.flg_f_cbet = TRUE AND chps.cnt_p_raise > 0
                      AND chps.flg_f_has_position = TRUE THEN 1 ELSE 0 END) as cbet_ip,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END) as def_opp_oop,
            SUM(CASE WHEN chps.flg_f_cbet_def_opp = TRUE AND chps.flg_f_fold = TRUE
                      AND chps.flg_f_has_position = FALSE THEN 1 ELSE 0 END) as fold_oop
        FROM cash_hand_player_statistics chps
        JOIN player p ON p.id_player = chps.id_player
        JOIN cash_hand_summary chs ON chps.id_hand = chs.id_hand
        WHERE p.player_name = %s
          AND chps.flg_p_3bet = FALSE
          AND chs.cnt_players_f = 2
        GROUP BY chs.card_1, chs.card_2, chs.card_3
    """, (player,))
    raw = cur.fetchall()
    buckets = {}
    for c1, c2, c3, cbet_opp_ip, cbet_ip, def_opp_oop, fold_oop in raw:
        if None in (c1, c2, c3):
            continue
        ranks = sorted([c % 13 for c in [c1, c2, c3]])
        is_connected = (abs(ranks[1] - ranks[0]) <= 2) or (abs(ranks[2] - ranks[1]) <= 2) or (abs(ranks[2] - ranks[0]) <= 2)
        cat = 'CONNECTED' if is_connected else 'DISCONNECTED'
        if cat not in buckets:
            buckets[cat] = {'cbet_opp_ip': 0, 'cbet_ip': 0, 'def_opp_oop': 0, 'fold_oop': 0}
        b = buckets[cat]
        b['cbet_opp_ip'] += cbet_opp_ip
        b['cbet_ip'] += cbet_ip
        b['def_opp_oop'] += def_opp_oop
        b['fold_oop'] += fold_oop

    details = {}
    total_n = 0
    for cat in ['CONNECTED', 'DISCONNECTED']:
        b = buckets.get(cat)
        if b is None:
            continue
        cbet_pct = (b['cbet_ip'] / b['cbet_opp_ip'] * 100.0) if b['cbet_opp_ip'] >= 30 else None
        fold_pct = (b['fold_oop'] / b['def_opp_oop'] * 100.0) if b['def_opp_oop'] >= 30 else None
        n_combined = b['cbet_opp_ip'] + b['def_opp_oop']
        details[cat] = {
            'value': cbet_pct, 'n': n_combined,
            'cbet_ip_pct': round(cbet_pct, 1) if cbet_pct is not None else None,
            'fold_oop_pct': round(fold_pct, 1) if fold_pct is not None else None,
            'cbet_opp_ip': b['cbet_opp_ip'], 'def_opp_oop': b['def_opp_oop']
        }
        total_n += n_combined
    status = sample_status(total_n, 30)
    return {'value': None, 'n': total_n, 'status': status, 'details': details}


#    Section D: Main Orchestrator

# Registry of all situation functions
SITUATION_REGISTRY = [
    (28, sit_28), (29, sit_29), (30, sit_30), (31, sit_31),
    (32, sit_32),
    (35, sit_35),
    (36, sit_36), (37, sit_37), (38, sit_38), (39, sit_39), (40, sit_40),
    (41, sit_41), (42, sit_42), (43, sit_43),
    (44, sit_44),
    (45, sit_45), (46, sit_46), (47, sit_47), (48, sit_48), (49, sit_49), (50, sit_50),
    (51, sit_51), (52, sit_52), (53, sit_53), (54, sit_54),
    (55, sit_55), (56, sit_56), (57, sit_57), (59, sit_59), (60, sit_60), (61, sit_61), (63, sit_63), (64, sit_64), (65, sit_65), (66, sit_66),
    (67, sit_67), (68, sit_68), (69, sit_69), (70, sit_70), (71, sit_71), (72, sit_72),
    (73, sit_73), (74, sit_74), (75, sit_75), (76, sit_76), (77, sit_77), (78, sit_78),
    (80, sit_80), (81, sit_81), (82, sit_82), (83, sit_83), (84, sit_84), (85, sit_85),
    (87, sit_87), (90, sit_90), (92, sit_92), (105, sit_92b), (93, sit_93), (94, sit_94),
    (96, sit_96), (98, sit_98), (99, sit_99), (100, sit_100),
    (101, sit_101), (103, sit_103), (104, sit_104),
    (106, sit_106), (107, sit_107), (108, sit_108),
]


def generate_full_report(player_name: str) -> dict:
    """
    Run all situations for player_name.
    Returns dict: {sit_N: {value, n, status, details}}.
    Saves markdown report to reports/<player_name>_FULL.md.
    """
    conn = psycopg2.connect(**DB)
    try:
        cur = conn.cursor()
        results = {}
        for sit_num, func in SITUATION_REGISTRY:
            key = 'sit_%d' % sit_num
            results[key] = _run_isolated(func, cur, player_name)
        conn.commit()

        # Write markdown report
        os.makedirs(REPORT_DIR, exist_ok=True)
        out_path = os.path.join(REPORT_DIR, '%s_FULL.md' % player_name)
        _write_report(player_name, results, out_path)
        print("Report saved: %s" % out_path)
        return results
    finally:
        cur.close()
        conn.close()


def _fmt(v, decimals=1):
    """Format float or None for display."""
    if v is None:
        return "N/A"
    return "%.1f" % round(v, decimals)


def _write_report(player_name: str, results: dict, out_path: str):
    """Write ASCII-only markdown report."""
    lines = []
    lines.append("# FULL REPORT: %s" % player_name)
    lines.append("Generated: %s" % datetime.now().strftime("%Y-%m-%d"))
    lines.append("")
    lines.append("## Validation Summary")
    lines.append("")
    lines.append("| # | Value | n | Status |")
    lines.append("|--:|-------|---|--------|")
    for key, v in sorted(results.items(), key=lambda x: int(x[0].split('_')[1])):
        val_str = _fmt(v['value']) if v['value'] is not None else "var"
        lines.append("| %s | %s | %d | %s |" % (
            key, val_str, v['n'], v['status']))
    lines.append("")

    # Per-situation detail sections
    for sit_num, func in SITUATION_REGISTRY:
        key = 'sit_%d' % sit_num
        r = results.get(key, {})
        details = r.get('details', {})
        if not details:
            continue

        lines.append("## Situation #%d" % sit_num)
        lines.append("")

        # Detect if details is simple (label -> {value, n}) or complex
        first_detail = next(iter(details.values()), {})
        if isinstance(first_detail, dict) and 'value' in first_detail and 'n' in first_detail:
            # Standard format: label -> {value, n, ...}
            lines.append("| Category | Value | n |")
            lines.append("|----------|-------|---|")
            for label, dv in sorted(details.items()):
                val_str = _fmt(dv.get('value')) if dv.get('value') is not None else "var"
                n_str = str(dv.get('n', 'N/A'))
                lines.append("| %s | %s | %s |" % (label, val_str, n_str))
        else:
            # Complex format — list key-value pairs
            for k2, v2 in sorted(details.items()):
                if isinstance(v2, dict):
                    parts = []
                    for subk, subv in v2.items():
                        if isinstance(subv, float):
                            parts.append("%s=%.1f" % (subk, subv))
                        else:
                            parts.append("%s=%s" % (subk, subv))
                    lines.append("- **%s**: %s" % (k2, ", ".join(parts)))
                else:
                    lines.append("- **%s**: %s" % (k2, v2))

        lines.append("")

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))


#    Section E: Debug Block                                                   

if __name__ == '__main__':
    player = sys.argv[1] if len(sys.argv) > 1 else 'jaymacca13'
    print("Running generate_full_report('%s')..." % player)
    results = generate_full_report(player)
    ok_count = sum(1 for v in results.values() if 'ERROR' not in v['status'])
    err_count = sum(1 for v in results.values() if 'ERROR' in v['status'])
    print()
    print("Results (%d OK, %d ERROR):" % (ok_count, err_count))
    for k, v in sorted(results.items(), key=lambda x: int(x[0].split('_')[1])):
        val_str = _fmt(v['value']) if v['value'] is not None else "var"
        print("  %s: %s (n=%d) [%s]" % (k, val_str, v['n'], v['status']))