import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
from difflib import SequenceMatcher

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://lscluster.hockeytech.com/feed/index.php"
API_KEY = "446521baf8c38984"
CLIENT_CODE = "pwhl"

# Default season if not specified in team JSON
DEFAULT_SEASON_ID = 8

TEAMS_DIR = Path("teams")
OUTPUT_DIR = Path("output")

# Optional: name aliases if your fantasy name doesn't match API name exactly
# Keys and values should be in "normal" human-readable form;
# the script will normalize internally.
NAME_ALIASES: Dict[str, str] = {
    # "Jincy Dunne": "Jincy Roese",
}


# ---------------------------------------------------------------------------
# HTTP / JSON helpers
# ---------------------------------------------------------------------------

def api_get(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic helper to call the PWHL API and return a Python dict.

    Handles both plain JSON and JSONP responses (e.g. angular.callbacks._5({...});).
    """
    merged = {
        "key": API_KEY,
        "client_code": CLIENT_CODE,
        **params,
    }

    resp = requests.get(BASE_URL, params=merged, timeout=30)
    resp.raise_for_status()

    text = resp.text.strip()

    # Try plain JSON first
    try:
        return resp.json()
    except ValueError:
        pass

    # Fallback: JSONP-style "callback({...});"
    if "(" in text and text.endswith((")", ");", "])", "]);")):
        first_paren = text.find("(")
        last_paren = text.rfind(")")
        json_str = text[first_paren + 1:last_paren].strip()

        if json_str.endswith(";"):
            json_str = json_str[:-1].strip()

        try:
            return json.loads(json_str)
        except ValueError as e:
            raise RuntimeError(
                "Failed to parse JSONP response. "
                f"First 300 chars:\n{text[:300]}"
            ) from e

    raise RuntimeError(
        f"Unexpected non-JSON response from API (status {resp.status_code}). "
        f"First 300 chars:\n{text[:300]}"
    )


def find_player_rows(obj: Any) -> List[Dict[str, Any]]:
    """
    Recursively search the JSON for dicts with a 'row' key that
    contains a 'player_id' – based on HockeyTech feed structure.
    """
    rows: List[Dict[str, Any]] = []

    if isinstance(obj, dict):
        if "row" in obj and isinstance(obj["row"], dict) and "player_id" in obj["row"]:
            rows.append(obj["row"])
        for value in obj.values():
            rows.extend(find_player_rows(value))
    elif isinstance(obj, list):
        for item in obj:
            rows.extend(find_player_rows(item))

    return rows


# ---------------------------------------------------------------------------
# API calls for players
# ---------------------------------------------------------------------------

def fetch_skaters(season_id: int) -> List[Dict[str, Any]]:
    """Fetch all skater stats for a season."""
    params = {
        "feed": "statviewfeed",
        "view": "players",
        "season": season_id,
        "team": "all",
        "position": "skaters",
        "rookies": 0,
        "statsType": "standard",
        "league_id": 1,
        "limit": 500,
        "sort": "points",
        "lang": "en",
        "division": -1,
        "conference": -1,
    }
    data = api_get(params)
    return find_player_rows(data)


def fetch_goalies(season_id: int) -> List[Dict[str, Any]]:
    """Fetch all goalie stats for a season."""
    params = {
        "feed": "statviewfeed",
        "view": "players",
        "season": season_id,
        "team": "all",
        "position": "goalies",
        "rookies": 0,
        "statsType": "standard",
        "league_id": 1,
        "limit": 500,
        "sort": "gaa",
        "qualified": "all",
        "lang": "en",
        "division": -1,
        "conference": -1,
    }
    data = api_get(params)
    return find_player_rows(data)


# ---------------------------------------------------------------------------
# Name / matching helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Normalize player names for matching."""
    return " ".join(name.strip().lower().split())


def get_display_name(p: Dict[str, Any]) -> str:
    """
    Try to build a human-readable name from whatever fields exist.
    Handles differences between skater and goalie feeds.
    """
    name = (
        p.get("name")
        or p.get("player")
        or p.get("goalie_name")
        or p.get("goalie")
        or p.get("skater_name")
        or p.get("skater")
    )
    if name:
        return str(name)

    # Try first / last name style fields
    first = p.get("first_name") or p.get("first")
    last = p.get("last_name") or p.get("last")

    if first and last:
        return f"{first} {last}"
    if first:
        return str(first)
    if last:
        return str(last)

    return ""


def build_index(players: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Build a name -> stats dict index using a derived display name.
    Key is normalized name, value is the stats row dict.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for p in players:
        name = get_display_name(p)
        if not name:
            continue
        key = normalize_name(name)
        index[key] = p
    return index


def build_first_name_map(index: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """
    Build a mapping: normalized first name -> list of normalized full-name keys.

    Example key: 'jincy'
    Values: ['jincy roese']
    """
    mapping: Dict[str, List[str]] = {}
    for norm_name in index.keys():
        first = norm_name.split(" ")[0]
        mapping.setdefault(first, []).append(norm_name)
    return mapping


def apply_alias(name: str) -> str:
    """
    Apply any configured name aliases before matching.
    """
    return NAME_ALIASES.get(name, name)


def best_fuzzy_match(
    lookup_norm: str,
    candidate_keys: List[str],
    cutoff: float,
) -> Tuple[str, float]:
    """
    Return the best fuzzy match (key, score) for lookup_norm among candidate_keys.

    Uses difflib.SequenceMatcher ratio. If the best score is below cutoff,
    returns ("", score).
    """
    best_key = ""
    best_score = 0.0

    for key in candidate_keys:
        score = SequenceMatcher(None, lookup_norm, key).ratio()
        if score > best_score:
            best_score = score
            best_key = key

    if best_score >= cutoff:
        return best_key, best_score
    else:
        return "", best_score


# ---------------------------------------------------------------------------
# Team JSON I/O
# ---------------------------------------------------------------------------

def load_team_files() -> List[Path]:
    """Return all JSON files in the teams directory."""
    return sorted(TEAMS_DIR.glob("*.json"))


def process_team_file(
    team_path: Path,
    skater_index: Dict[str, Dict[str, Any]],
    goalie_index: Dict[str, Dict[str, Any]],
) -> None:
    """Read one team JSON, match players, and write a JSON file with all stats."""
    with team_path.open("r", encoding="utf-8") as f:
        team_cfg = json.load(f)

    team_name = team_cfg.get("team_name", team_path.stem)
    season_id = int(team_cfg.get("season_id", DEFAULT_SEASON_ID))

    players_out: List[Dict[str, Any]] = []

    # Precompute candidate key lists and first-name maps for fuzzy matching
    skater_keys = list(skater_index.keys())
    goalie_keys = list(goalie_index.keys())
    skater_first_map = build_first_name_map(skater_index)
    goalie_first_map = build_first_name_map(goalie_index)

    def add_players(names: List[str], fantasy_role: str, is_goalie: bool = False):
        index = goalie_index if is_goalie else skater_index
        candidate_keys = goalie_keys if is_goalie else skater_keys
        first_name_map = goalie_first_map if is_goalie else skater_first_map
        stats_position_group = "goalie" if is_goalie else "skater"

        for raw_name in names:
            requested_name = raw_name
            lookup_name = apply_alias(raw_name)
            lookup_norm = normalize_name(lookup_name)

            first_token = lookup_norm.split(" ")[0] if lookup_norm else ""
            same_first_candidates = first_name_map.get(first_token, [])

            row: Dict[str, Any] = {
                "fantasy_team": team_name,
                "season_id": season_id,
                "fantasy_role": fantasy_role,  # "forward", "defence", or "goalie"
                "requested_name": requested_name,
                "lookup_name": lookup_name,
                "stats_position_group": stats_position_group,
                "match_method": "none",
                "match_score": 0.0,
                "matched_name": None,
            }

            # 1) Exact (normalized) match
            stats = index.get(lookup_norm)
            if stats:
                row["missing"] = False
                row["match_method"] = "exact"
                row["match_score"] = 1.0
                row["matched_name"] = get_display_name(stats)
                row.update(stats)
                players_out.append(row)
                continue

            # 2) First-name unique match:
            #    If exactly one player in the league has this first name,
            #    assume it's them (e.g., "Jincy").
            if len(same_first_candidates) == 1:
                only_key = same_first_candidates[0]
                stats = index.get(only_key)
                if stats:
                    row["missing"] = False
                    row["match_method"] = "first_name_unique"
                    row["match_score"] = 1.0
                    row["matched_name"] = get_display_name(stats)
                    row.update(stats)
                    players_out.append(row)
                    continue

            # 3) Fuzzy within same first-name group (more permissive cutoff)
            best_key = ""
            best_score = 0.0
            if same_first_candidates:
                best_key, best_score = best_fuzzy_match(
                    lookup_norm,
                    same_first_candidates,
                    cutoff=0.5,  # allow e.g. "dunne" vs "roese"
                )
                if best_key:
                    stats = index.get(best_key)
                    row["missing"] = False
                    row["match_method"] = "fuzzy_first_name_group"
                    row["match_score"] = best_score
                    row["matched_name"] = get_display_name(stats)
                    row.update(stats)
                    players_out.append(row)
                    continue

            # 4) Global fuzzy match (stricter cutoff to avoid wild matches)
            best_key, best_score = best_fuzzy_match(
                lookup_norm,
                candidate_keys,
                cutoff=0.8,
            )
            if best_key:
                stats = index.get(best_key)
                row["missing"] = False
                row["match_method"] = "fuzzy_global"
                row["match_score"] = best_score
                row["matched_name"] = get_display_name(stats)
                row.update(stats)
            else:
                # 5) No match – keep as missing, but keep best_score for debugging
                row["missing"] = True
                row["match_score"] = best_score

            players_out.append(row)

    add_players(team_cfg.get("forwards", []), "forward", is_goalie=False)
    add_players(team_cfg.get("defence", []), "defence", is_goalie=False)
    add_players(team_cfg.get("goalies", []), "goalie", is_goalie=True)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"{team_path.stem}_stats.json"

    output_payload = {
        "team_name": team_name,
        "season_id": season_id,
        "generated_by": "pwhl_fantasy_export",
        "players": players_out,
    }

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)

    print(f"Wrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    TEAMS_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    season_id = DEFAULT_SEASON_ID
    print(f"Fetching skater stats for season {season_id}...")
    skaters = fetch_skaters(season_id)
    skater_index = build_index(skaters)
    print(f"Loaded {len(skaters)} skaters.")

    print(f"Fetching goalie stats for season {season_id}...")
    goalies = fetch_goalies(season_id)
    goalie_index = build_index(goalies)
    print(f"Loaded {len(goalies)} goalies.")

    # Uncomment if you want to quickly inspect keys:
    # print("Sample skaters:", list(skater_index.keys())[:10])
    # print("Sample goalies:", list(goalie_index.keys())[:10])

    team_files = load_team_files()
    if not team_files:
        print("No team files found in 'teams/'")
        return

    for team_path in team_files:
        print(f"Processing {team_path}...")
        process_team_file(team_path, skater_index, goalie_index)


if __name__ == "__main__":
    main()
