"""
PT4 Database Tools - UAPI v3.8
Dynamic library of Python functions containing optimized SQL, maintained autonomously by Claude Code.
"""

import os
import psycopg2
from psycopg2 import OperationalError
from typing import List, Dict, Tuple, Optional
from contextlib import contextmanager

# Database configuration from .env
def load_db_config():
    """Load database configuration from .env file with fallback."""
    import os

    # Try to read from .env file
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    config = {}
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    # Remove quotes if present
                    value = value.strip('"\'')
                    config[key] = value

    # Map env vars to psycopg2 params
    return {
        'dbname': config.get('DB_NAME', 'PT4 DB'),
        'user': config.get('DB_USER', 'postgres'),
        'password': config.get('DB_PASS', ''),
        'host': config.get('DB_HOST', 'localhost'),
        'port': config.get('DB_PORT', '5432')
    }

DB_CONFIG = load_db_config()


class PT4DatabaseError(Exception):
    """Custom exception for PT4 database operations."""
    pass


@contextmanager
def get_db_connection():
    """Context manager for database connections."""
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        yield conn
    except OperationalError as e:
        raise PT4DatabaseError(f"Failed to connect to database: {e}")
    finally:
        if conn:
            conn.close()


@contextmanager
def get_cursor():
    """Context manager for database cursors."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise PT4DatabaseError(f"Database operation failed: {e}")
        finally:
            cursor.close()


# Card decoding constants (per PT4 schema)
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
SUITS = ['s', 'h', 'd', 'c']  # 0=spades, 1=hearts, 2=diamonds, 3=clubs


def decode_card(card_value: Optional[int]) -> Optional[str]:
    """Decode a card value into human-readable form."""
    if card_value is None:
        return None
    rank = RANKS[card_value % 13]
    suit = SUITS[card_value // 13]
    return f"{rank}{suit}"


def classify_board_texture(card_1: Optional[int], card_2: Optional[int],
                         card_3: Optional[int]) -> Dict:
    """Classify board texture from flop cards."""
    # Handle pre-flop all-in (no flop)
    if card_1 is None or card_2 is None or card_3 is None:
        return {
            'type': 'NO_FLOP',
            'is_monotone': False,
            'is_paired': False,
            'is_rainbow': False
        }

    # Calculate texture
    suits = [c // 13 for c in [card_1, card_2, card_3]]
    ranks = [c % 13 for c in [card_1, card_2, card_3]]

    # Monotone: all same suit
    is_monotone = len(set(suits)) == 1

    # Paired: any duplicate ranks
    is_paired = len(set(ranks)) < len(ranks)

    # Rainbow: all different suits
    is_rainbow = len(set(suits)) == 3

    texture_type = 'MONOTONE' if is_monotone else 'PAIRED' if is_paired else 'DRY'

    return {
        'type': texture_type,
        'is_monotone': is_monotone,
        'is_paired': is_paired,
        'is_rainbow': is_rainbow
    }


def classify_sizing_bucket(bet_facing_pct: Optional[float]) -> Optional[str]:
    """Classify bet sizing into standardized buckets."""
    if bet_facing_pct is None:
        return None

    if bet_facing_pct < 33.0:
        return 'Small'
    elif bet_facing_pct <= 67.0:
        return 'Medium'
    elif bet_facing_pct <= 100.0:
        return 'Large'
    else:
        return 'Overbet'


# Leak threshold definitions (GTO-adjusted)
LEAK_THRESHOLDS = {
    'Small': {'max_fold_pct': 40.0, 'range': '< 33%', 'description': 'Light C-Bet'},
    'Medium': {'max_fold_pct': 50.0, 'range': '33-67%', 'description': 'Standard C-Bet'},
    'Large': {'max_fold_pct': 60.0, 'range': '67-100%', 'description': 'Heavy C-Bet'},
    'Overbet': None  # EXCLUDED - high variance, insufficient samples
}


# Position mapping for 6-max
POSITION_NAMES_6MAX = {0: 'BTN', 1: 'CO', 2: 'MP', 8: 'BB', 9: 'SB'}


def get_leak_tier(n: int) -> Optional[str]:
    """Determine confidence tier based on sample size."""
    if n >= 30:
        return 'Confirmed Leak'
    elif n >= 15:
        return 'Potential Leak'
    else:
        return None  # Ignore


def generate_exploit_suggestion(texture: str, sizing: str, fold_pct: float) -> str:
    """Generate context-specific exploit suggestion based on texture and sizing."""
    # Small sizing exploits
    if sizing == 'Small':
        if texture == 'MONOTONE':
            return f"Exploit: High fold rate ({fold_pct:.1f}%) vs small bets on monotone boards. Stab 1/3 pot with pure air, leverage scare cards."
        elif texture == 'PAIRED':
            return f"Exploit: Excessive folding ({fold_pct:.1f}%) to small bets on paired boards. Fire 1/3 pot with any equity."
        elif texture == 'DRY':
            return f"Exploit: Folding {fold_pct:.1f}% to small bets on dry boards. Wide folding range - stab with nearly any two cards."
        else:
            return f"Exploit: {fold_pct:.1f}% fold rate to small bets. Aggressive small-ball strategy with range advantage."

    # Medium sizing exploits
    elif sizing == 'Medium':
        if texture == 'MONOTONE':
            return f"Exploit: {fold_pct:.1f}% fold rate to medium bets on monotone boards. Polarize with flush draws and semi-bluffs."
        elif texture == 'PAIRED':
            return f"Exploit: Folding {fold_pct:.1f}% to medium bets on paired boards. Target weak pairs with controlled aggression."
        elif texture == 'DRY':
            return f"Exploit: {fold_pct:.1f}% fold rate to medium bets on dry boards. Continued aggression exploits."
        else:
            return f"Exploit: {fold_pct:.1f}% fold rate to medium bets. Standard sizing - exploit with balanced pressure."

    # Large sizing exploits
    elif sizing == 'Large':
        if texture == 'MONOTONE':
            return f"Exploit: SEVERE LEAK ({fold_pct:.1f}% fold rate) vs large bets on monotone boards. GTO calls widely here."
        elif texture == 'PAIRED':
            return f"Exploit: CRITICAL LEAK ({fold_pct:.1f}% fold rate) vs large bets on paired boards. Marathon call-down mode."
        elif texture == 'DRY':
            return f"Exploit: {fold_pct:.1f}% fold rate to large bets on dry boards. Opponent likely under-defending."
        else:
            return f"Exploit: {fold_pct:.1f}% fold rate to large bets. Heavy pressure causing folds."

    return "Unknown configuration - manual review required."



def bucket_by_sizing(pct):
    """Public alias for classify_sizing_bucket. Use this in external modules."""
    return classify_sizing_bucket(pct)
