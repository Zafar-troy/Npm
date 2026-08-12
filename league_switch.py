"""
league_switch.py — interactive on/off switch for leagues.json + FAVORITES
Run: python league_switch.py

Shows a numbered list of every league (current ON/OFF state) plus the
current FAVORITES setting from .env. Type numbers to toggle leagues,
"f" to toggle favorites, "s" to save+exit, "q" to quit without saving.
Writes leagues.json with json.dump (never nano) so there's no risk of
the invisible-unicode corruption nano caused before.
"""
import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
LEAGUES_PATH = os.path.join(BASE, "leagues.json")
ENV_PATH = os.path.join(BASE, ".env")

DEFAULT_LEAGUES = {
    "Premier League": True, "Championship": True, "La Liga": True,
    "Serie A": True, "Bundesliga": True, "Ligue 1": True,
    "Eredivisie": True, "MLS": True, "Liga MX": True,
    "Brazil Série A": True, "Saudi Pro League": True,
    "South African Premiership": True, "Malawi Super League": True,
    "Süper Lig": True,
}


def load_leagues() -> dict:
    if os.path.exists(LEAGUES_PATH):
        with open(LEAGUES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_LEAGUES.items():
            data.setdefault(k, v)
        return data
    return dict(DEFAULT_LEAGUES)


def save_leagues(data: dict):
    with open(LEAGUES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_favorites() -> bool:
    if not os.path.exists(ENV_PATH):
        return False
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("FAVORITES="):
                return line.strip().split("=", 1)[1].strip().lower() in ("on", "true", "1", "yes")
    return False


def set_favorites(value: bool):
    line_val = "on" if value else "off"
    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(f"FAVORITES={line_val}\n")
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("FAVORITES="):
            lines[i] = f"FAVORITES={line_val}\n"
            found = True
            break
    if not found:
        lines.append(f"FAVORITES={line_val}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def main():
    leagues = load_leagues()
    favorites = get_favorites()
    keys = list(leagues.keys())

    while True:
        print("\n── League switches ──────────────────")
        for i, k in enumerate(keys, 1):
            state = "ON " if leagues[k] else "OFF"
            print(f" {i:>2}. [{state}] {k}")
        fav_state = "ON " if favorites else "OFF"
        print(f"  f. [{fav_state}] FAVORITES (only favourite clubs post, per league still ON)")
        print("──────────────────────────────────────")
        choice = input("Toggle (e.g. 1,3,f) · s = save+exit · q = quit: ").strip().lower()

        if choice == "q":
            print("No changes saved.")
            return
        if choice == "s":
            save_leagues(leagues)
            set_favorites(favorites)
            print("Saved. Restart the bot for changes to take effect:")
            print("  tmux send-keys -t bot C-c")
            print("  tmux attach -t bot")
            print("  python bot.py")
            return

        for part in choice.split(","):
            part = part.strip()
            if part == "f":
                favorites = not favorites
            elif part.isdigit() and 1 <= int(part) <= len(keys):
                k = keys[int(part) - 1]
                leagues[k] = not leagues[k]
            elif part:
                print(f"  (ignored '{part}' — not a valid option)")


if __name__ == "__main__":
    main()
