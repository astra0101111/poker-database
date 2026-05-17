""" Population Scanner - Scans all players and generates population summary.
Run: python population_scanner.py """
import os
from datetime import datetime
from typing import Optional
from pt4_tools import get_cursor
from generate_full_report import generate_full_report


def get_all_players(min_hands: int = 500) -> list[dict]:
    """Query all players with at least min_hands from the database."""
    with get_cursor() as cur:
        cur.execute(""" SELECT p.id_player, p.player_name, COUNT(DISTINCT chps.id_hand) AS n_hands FROM player p JOIN cash_hand_player_statistics chps ON p.id_player = chps.id_player GROUP BY p.id_player, p.player_name HAVING COUNT(DISTINCT chps.id_hand) >= %s ORDER BY n_hands DESC """, (min_hands,))
        return [ {"id": row[0], "name": row[1], "n_hands": row[2]} for row in cur.fetchall() ]


def extract_key_metrics(report: dict, player_name: str) -> dict:
    """Extract key metrics from generate_full_report output.

    Note: player_name is needed for direct DB queries for metrics that
    aren't properly captured in the report (e.g., Fold to Steal SB).
    """
    metrics = {}
    # VPIP - sit_36
    if "sit_36" in report:
        vpip_data = report["sit_36"]
        if vpip_data.get("value") is not None:
            metrics["VPIP"] = round(vpip_data["value"], 1)
        else:
            metrics["VPIP"] = None
    # PFR - sit_37
    if "sit_37" in report:
        pfr_data = report["sit_37"]
        if pfr_data.get("value") is not None:
            metrics["PFR"] = round(pfr_data["value"], 1)
        else:
            metrics["PFR"] = None
    # Fold to Steal SB - Query directly from DB (sit_40 excludes low-sample SB data)
    metrics["Fold to Steal SB"] = get_fold_to_steal_sb(player_name)
    # Fold to 3-Bet SB - Query directly from DB
    metrics["Fold to 3-Bet SB"] = get_fold_to_3bet_sb(player_name)
    # Flop CBet IP - Use sit_81 (CBet by Texture for PFR IP) instead of sit_49 (BTN-only)
    # sit_49 only looks at BTN which has tiny sample sizes causing 90%+ values
    if "sit_81" in report:
        cbet_data = report["sit_81"]
        if cbet_data.get("value") is not None:
            metrics["Flop CBet IP"] = round(cbet_data["value"], 1)
        else:
            metrics["Flop CBet IP"] = None
    else:
        metrics["Flop CBet IP"] = None
    # Leak Score - sit_107
    if "sit_107" in report:
        leak_data = report["sit_107"]
        if leak_data.get("value") is not None:
            metrics["Leak Score"] = round(leak_data["value"], 1)
        else:
            metrics["Leak Score"] = None
    return metrics


def get_fold_to_steal_sb(player_name: str) -> Optional[float]:
    """Query Fold to Steal SB directly from database.

    sit_40 excludes SB data due to HAVING COUNT(*) >= 10 threshold,
    so we query directly for the overall SB fold to steal %.
    """
    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN chps.flg_sb_steal_fold = TRUE THEN 1 ELSE 0 END) as sb_folds,
                    SUM(CASE WHEN chps.flg_blind_def_opp = TRUE THEN 1 ELSE 0 END) as sb_def_opp
                FROM cash_hand_player_statistics chps
                JOIN player p ON chps.id_player = p.id_player
                WHERE p.player_name = %s
                AND chps.position = 9
            """, (player_name,))
            row = cur.fetchone()
            if row and row[1] and row[1] >= 10:
                folds = row[0] or 0
                opp = row[1]
                return round(folds / opp * 100, 1)
    except Exception:
        pass
    return None


def get_fold_to_3bet_sb(player_name: str) -> Optional[float]:
    """Query Fold to 3-Bet SB directly from database.
    
    Denominator: Hands where player faced 3-bet from SB.
    Numerator: Hands where player folded to 3-bet from SB (enum_p_3bet_action='F').
    """
    try:
        with get_cursor() as cur:
            cur.execute('''
                SELECT
                    SUM(CASE WHEN chps.enum_p_3bet_action = 'F' THEN 1 ELSE 0 END) as folds,
                    SUM(CASE WHEN chps.flg_p_3bet_def_opp = TRUE THEN 1 ELSE 0 END) as def_opps
                FROM cash_hand_player_statistics chps
                JOIN player p ON chps.id_player = p.id_player
                WHERE p.player_name = %s
                AND chps.position = 9
                AND chps.flg_p_3bet_def_opp = TRUE
            ''', (player_name,))
            row = cur.fetchone()
            if row and row[1] and row[1] >= 10:
                folds = row[0] or 0
                opp = row[1]
                return round(folds / opp * 100, 1)
    except Exception:
        pass
    return None


def format_metric(val: Optional[float], suffix: str = "%") -> str:
    """Format metric value for display."""
    if val is None:
        return "N/A"
    return f"{val:.1f}{suffix}"


def scan_all_players(min_hands: int = 500) -> dict:
    """
    Main population scanner.
    Args:
        min_hands: Minimum hands required for inclusion
    Returns:
        Summary data dict with player metrics
    """
    os.makedirs("reports", exist_ok=True)
    print(f"\n{'='*60}")
    print(f"POPULATION SCANNER - min_hands={min_hands}")
    print(f"{'='*60}\n")
    # Get all players
    players = get_all_players(min_hands)
    total_players = len(players)
    print(f"Found {total_players} players with >= {min_hands} hands\n")
    if total_players == 0:
        print("No players found. Check database connection.")
        return {"players": [], "summary": {}}
    # Process each player
    all_player_data = []
    errors = []
    for idx, player in enumerate(players, 1):
        player_name = player["name"]
        n_hands = player["n_hands"]
        print(f"Processing player {idx} of {total_players}: {player_name} ({n_hands:,} hands)")
        try:
            # Generate individual report
            report = generate_full_report(player_name)
            # Extract key metrics
            metrics = extract_key_metrics(report, player_name)
            metrics["n_hands"] = n_hands
            metrics["player_name"] = player_name
            all_player_data.append(metrics)
        except Exception as e:
            print(f" ERROR: {str(e)[:80]}")
            errors.append({"player": player_name, "error": str(e)})
            continue
    # Sort by leak score descending (most exploitable first)
    all_player_data.sort(key=lambda x: x.get("Leak Score") or -999, reverse=True)
    # Print errors summary
    if errors:
        print(f"\n{'='*60}")
        print(f"ERRORS ({len(errors)} players failed):")
        for err in errors:
            print(f" - {err['player']}: {err['error'][:60]}")
    # Generate summary markdown
    summary_path = "reports/population_summary.md"
    generate_summary_markdown(all_player_data, errors, summary_path)
    # Print quick stats
    print(f"\n{'='*60}")
    print("SCAN COMPLETE")
    print(f"{'='*60}")
    print(f"Players processed: {len(all_player_data)}/{total_players}")
    print(f"Errors: {len(errors)}")
    print(f"Summary saved to: {summary_path}")
    if all_player_data:
        avg_leak = sum(p.get("Leak Score") or 0 for p in all_player_data) / len(all_player_data)
        print(f"Average Leak Score: {avg_leak:.1f}")
    return {
        "players": all_player_data,
        "errors": errors,
        "summary_path": summary_path
    }


def generate_summary_markdown(player_data: list[dict], errors: list[dict], output_path: str):
    """Generate population_summary.md file."""
    lines = [
        "# Population Summary",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"**Players scanned:** {len(player_data)}",
        f"**Errors:** {len(errors)}",
        "",
        "---",
        "",
        "## Key Metrics",
        "",
        "| # | Player | Hands | VPIP | PFR | Fold to Steal SB | Fold to 3-Bet SB | Flop CBet IP | Leak Score |",
        "|---|--------|-------|------|-----|------------------|------------------|--------------|------------|",
    ]
    for idx, p in enumerate(player_data, 1):
        player_name = p.get("player_name", "Unknown")
        n_hands = p.get("n_hands", 0)
        vpip = format_metric(p.get("VPIP"))
        pfr = format_metric(p.get("PFR"))
        ft_steal = format_metric(p.get("Fold to Steal SB"))
        ft_3bet = format_metric(p.get("Fold to 3-Bet SB"))
        cbet_ip = format_metric(p.get("Flop CBet IP"))
        leak_val = p.get("Leak Score")
        leak = f"{leak_val:.1f}" if leak_val is not None else "N/A"
        lines.append(
            f"| {idx} | {player_name} | {n_hands:,} | {vpip} | {pfr} | {ft_steal} | {ft_3bet} | {cbet_ip} | {leak} |"
        )
    # Errors section
    if errors:
        lines.extend([
            "",
            "---",
            "",
            "## Errors",
            "",
        ])
        for err in errors:
            lines.append(f"- **{err['player']}:** {err['error'][:100]}")
    # Summary stats
    if player_data:
        lines.extend([
            "",
            "---",
            "",
            "## Summary Statistics",
            "",
        ])
        # Calculate population averages
        vpip_vals = [p.get("VPIP") for p in player_data if p.get("VPIP") is not None]
        pfr_vals = [p.get("PFR") for p in player_data if p.get("PFR") is not None]
        leak_vals = [p.get("Leak Score") for p in player_data if p.get("Leak Score") is not None]
        if vpip_vals:
            lines.append(f"- **VPIP:** avg={sum(vpip_vals)/len(vpip_vals):.1f}%, range={min(vpip_vals):.1f}-{max(vpip_vals):.1f}%")
        if pfr_vals:
            lines.append(f"- **PFR:** avg={sum(pfr_vals)/len(pfr_vals):.1f}%, range={min(pfr_vals):.1f}-{max(pfr_vals):.1f}%")
        if leak_vals:
            lines.append(f"- **Leak Score:** avg={sum(leak_vals)/len(leak_vals):.1f}, range={min(leak_vals):.1f}-{max(leak_vals):.1f}")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    scan_all_players(min_hands=500)
