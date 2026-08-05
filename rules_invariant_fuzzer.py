import argparse
import json
import random
import time

import BotEuchreGUI as app


def validate_state(state, played_cards):
    errors = []
    all_cards = [card for hand in state.hands for card in hand]
    all_cards.extend(card for _, card in state.trick)
    all_cards.extend(played_cards)
    keys = [(card.rank, card.suit) for card in all_cards]
    if len(keys) != len(set(keys)):
        errors.append("duplicate card")
    if any(card.rank not in app.RANKS_T or card.suit not in app.SUITS_T
           for card in all_cards):
        errors.append("invalid card")
    if state.current_turn not in range(4):
        errors.append("invalid current turn")
    if state.trump_suit not in app.SUITS_T:
        errors.append("invalid trump")
    if len(state.trick) > (3 if state.is_loner else 4):
        errors.append("oversized trick")
    if state.team1_tricks < 0 or state.team2_tricks < 0:
        errors.append("negative trick count")
    if state.team1_tricks + state.team2_tricks > 5:
        errors.append("too many completed tricks")
    if state.is_loner:
        if state.loner_partner_idx != (state.caller_idx + 2) % 4:
            errors.append("wrong loner partner")
        if state.hands[state.loner_partner_idx]:
            errors.append("loner partner has cards")
    legal = state.get_legal_moves() if state.current_turn in range(4) else []
    if state.trick and legal:
        led_suit = state.get_effective_suit(state.trick[0][1])
        matching = [
            card for card in state.hands[state.current_turn]
            if state.get_effective_suit(card) == led_suit]
        if matching and set(legal) != set(matching):
            errors.append("follow-suit violation")
        if not matching and set(legal) != set(state.hands[state.current_turn]):
            errors.append("illegal discard restriction")
    return errors


def generated_state(random_source, case_seed):
    deck = app.build_seeded_deck(case_seed)
    hands = [deck[seat * 5:(seat + 1) * 5] for seat in range(4)]
    caller = random_source.randrange(4)
    is_loner = random_source.random() < 0.2
    partner = (caller + 2) % 4 if is_loner else -1
    if is_loner:
        hands[partner] = []
    current_turn = random_source.randrange(4)
    while is_loner and current_turn == partner:
        current_turn = random_source.randrange(4)
    state = app.SimState(
        random_source.choice(app.SUITS_T), [], hands, current_turn,
        is_loner, partner, caller)
    return state


def corrupt_state(state):
    corrupted = app.SimState(
        state.trump_suit, state.trick, state.hands, state.current_turn,
        state.is_loner, state.loner_partner_idx, state.caller_idx,
        state.voids, state.team1_tricks, state.team2_tricks)
    source = next((hand for hand in corrupted.hands if hand), None)
    if source:
        source.append(source[0])
    else:
        corrupted.current_turn = 9
    return corrupted


def run_fuzzer(case_count=5000, seed=20260802):
    started = time.perf_counter()
    random_source = random.Random(seed)
    failures = []
    mutations_rejected = 0
    for case_number in range(case_count):
        case_seed = random_source.randrange(2 ** 63)
        state = generated_state(random_source, case_seed)
        errors = validate_state(state, [])
        if errors:
            failures.append({
                "case": case_number, "seed": case_seed,
                "error": "valid state rejected", "details": errors})
            break
        mutation_errors = validate_state(corrupt_state(state), [])
        if not mutation_errors:
            failures.append({
                "case": case_number, "seed": case_seed,
                "error": "corrupted state accepted"})
            break
        mutations_rejected += 1
    elapsed = time.perf_counter() - started
    return {
        "format": "bot-euchre-rules-fuzzer-v1",
        "seed": seed,
        "cases_requested": case_count,
        "cases_completed": case_count if not failures else failures[0]["case"],
        "mutations_rejected": mutations_rejected,
        "elapsed_seconds": round(elapsed, 3),
        "failures": failures,
        "ok": not failures,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fuzz Bot Euchre state and follow-suit invariants.")
    parser.add_argument("--cases", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20260802)
    parser.add_argument("--output")
    arguments = parser.parse_args()
    result = run_fuzzer(arguments.cases, arguments.seed)
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if arguments.output:
        with open(arguments.output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered + "\n")
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
