import argparse
import json
import random
import time
import tracemalloc

import GrandmasterEuchreFinalAttempt as app


def run_soak(hand_count=10000, seed=20260801):
    started = time.perf_counter()
    random_source = random.Random(seed)
    failures = []
    total_moves = 0
    loner_hands = 0
    team1_tricks = 0
    tracemalloc.start()
    start_current, start_peak = tracemalloc.get_traced_memory()

    for hand_number in range(hand_count):
        hand_seed = seed + hand_number
        deck = app.build_seeded_deck(hand_seed)
        hands = [deck[seat * 5:(seat + 1) * 5] for seat in range(4)]
        caller = random_source.randrange(4)
        is_loner = random_source.random() < 0.15
        partner = (caller + 2) % 4 if is_loner else -1
        leader = random_source.randrange(4)
        if leader == partner:
            leader = (leader + 1) % 4
        state = app.SimState(
            random_source.choice(app.SUITS_T), [], hands, leader,
            is_loner, partner, caller)
        if is_loner:
            loner_hands += 1

        moves = 0
        seen_cards = set()
        while state.team1_tricks + state.team2_tricks < 5:
            legal = state.get_legal_moves()
            if not legal:
                failures.append({
                    "hand": hand_number, "seed": hand_seed,
                    "error": "no legal move before five tricks"})
                break
            card = random_source.choice(legal)
            card_key = (card.rank, card.suit)
            if card_key in seen_cards:
                failures.append({
                    "hand": hand_number, "seed": hand_seed,
                    "error": f"card played twice: {card}"})
                break
            seen_cards.add(card_key)
            state.apply_move(card)
            moves += 1
            total_moves += 1
            move_limit = 15 if is_loner else 20
            if moves > move_limit:
                failures.append({
                    "hand": hand_number, "seed": hand_seed,
                    "error": f"move limit exceeded: {moves}"})
                break

        expected_moves = 15 if is_loner else 20
        if not failures and moves != expected_moves:
            failures.append({
                "hand": hand_number, "seed": hand_seed,
                "error": f"expected {expected_moves} moves, observed {moves}"})
        if state.team1_tricks + state.team2_tricks == 5:
            team1_tricks += state.team1_tricks
        if failures:
            break

    current_memory, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.perf_counter() - started
    return {
        "format": "bot-euchre-soak-v1",
        "seed": seed,
        "hands_requested": hand_count,
        "hands_completed": hand_count if not failures else failures[0]["hand"],
        "moves": total_moves,
        "loner_hands": loner_hands,
        "team1_tricks": team1_tricks,
        "elapsed_seconds": round(elapsed, 3),
        "hands_per_second": round(hand_count / elapsed, 2) if elapsed else 0,
        "memory_growth_bytes": current_memory - start_current,
        "peak_growth_bytes": peak_memory - start_peak,
        "failures": failures,
        "ok": not failures,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run deterministic headless Bot Euchre hand invariants.")
    parser.add_argument("--hands", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260801)
    parser.add_argument("--output")
    arguments = parser.parse_args()
    result = run_soak(arguments.hands, arguments.seed)
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if arguments.output:
        with open(arguments.output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered + "\n")
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()