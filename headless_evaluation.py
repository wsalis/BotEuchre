"""
Grandmaster Euchre - Headless Generation Evaluator
Pits the current generation's brain (arbiter_weights.pth) against the prior
generation baseline (PriorGenCheems.pth) across a batch of random hands,
alternating which physical team each brain controls to cancel positional bias.
Appends a summary row to evaluation_history.jsonl so generation-over-generation
progress can be reviewed later without needing to watch the console live.

Usage: py -3 headless_evaluation.py [generation] [num_hands] [mcts_iterations] [baseline_path] [log_path]

[log_path] (optional 5th arg) redirects the output row to a different JSONL file.
Used by master_flywheel.py's fixed-anchor evaluation so anchor rows land in
anchor_evaluation_history.jsonl instead of polluting evaluation_history.jsonl
(whose last line drives checkpoint gating).
"""

import os
import sys
import json
import time
import math
import random
import threading
import numpy as np
import torch
import torch.nn.functional as F
import torch.multiprocessing as mp

torch.set_grad_enabled(False)
torch.set_num_threads(1)

from BotEuchreGUI import (
    SimState, Card, CheemsNeuralNet, encode_state_to_tensor,
    ALL_DECK_KEYS, SUITS_T, RANKS_T, SAME_COLOR_T, HAS_TORCH,
    POLICY_SIZE, BID_PASS, run_auction, legal_bid_actions,
    bid_action_details, choose_dealer_discard, encode_bid_state, get_tactical_search_moves,
    run_bid_mcts
)

ALL_DECK_KEYS_MAP = {key: idx for idx, key in enumerate(ALL_DECK_KEYS)}

# ==========================================
# 1. TENSOR STATE WRAPPER (same shape contract as the self-play scripts)
# ==========================================
class TensorStateWrapper:
    __slots__ = ['hands', 'trick', 'trump_suit', 'current_turn', 'caller_idx',
                 'is_loner', 'loner_partner_idx', 'voids', 'team1_tricks',
                 'team2_tricks', 'played_cards', 'up_card', 'dealer_idx',
                 'team1_score', 'team2_score', 'dealer_discard']

    def __init__(self, sc, played_cards, up_card, dealer_idx, t1_score, t2_score, dealer_discard=None):
        self.hands = sc.hands
        self.trick = sc.trick
        self.trump_suit = sc.trump_suit
        self.current_turn = sc.current_turn
        self.caller_idx = sc.caller_idx
        self.is_loner = sc.is_loner
        self.loner_partner_idx = sc.loner_partner_idx
        self.voids = sc.voids
        self.team1_tricks = sc.team1_tricks
        self.team2_tricks = sc.team2_tricks
        self.played_cards = played_cards
        self.up_card = up_card
        self.dealer_idx = dealer_idx
        self.team1_score = t1_score
        self.team2_score = t2_score
        self.dealer_discard = dealer_discard

    def get_effective_suit(self, card):
        if self.trump_suit and card.rank == 'J' and card.suit == SAME_COLOR_T[self.trump_suit]:
            return self.trump_suit
        return card.suit

    def is_trump(self, card):
        if not self.trump_suit: return False
        return self.get_effective_suit(card) == self.trump_suit

    def evaluate_trick(self):
        if not self.trick: return -1
        rank_base_vals = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
        led_suit = self.get_effective_suit(self.trick[0][1])
        highest_power = -1
        winner_idx = -1
        for player_idx, card in self.trick:
            power = rank_base_vals[card.rank]
            effective_suit = self.get_effective_suit(card)
            if card.rank == 'J' and card.suit == self.trump_suit: power += 500
            elif card.rank == 'J' and card.suit == SAME_COLOR_T[self.trump_suit]: power += 400
            elif effective_suit == self.trump_suit: power += 100
            elif effective_suit == led_suit: power += 50
            else: power = 0
            if power > highest_power:
                highest_power = power
                winner_idx = player_idx
        return winner_idx


class AlphaNode:
    __slots__ = ['move', 'parent', 'children', 'wins', 'visits', 'prior', 'player_idx']

    def __init__(self, move=None, parent=None, prior=0.0, player_idx=0):
        self.move = move
        self.parent = parent
        self.children = []
        self.wins = 0.0
        self.visits = 0
        self.prior = prior
        self.player_idx = player_idx


class GpuPipeClosedError(Exception):
    """Raised when the GPU server pipe is no longer available."""


# ==========================================
# 2. MICRO-BATCHED MCTS (sends leaf evaluations through a pipe to the central GPU
#    server instead of calling a local net directly, mirroring the Desktop self-play
#    pipe-server architecture so many simultaneous hands can share large GPU batches)
# ==========================================
def run_eval_mcts(sim_state, gpu_pipe, net_id, iterations, current_played_cards, up_card, dealer_idx,
                   t1_score, t2_score, nn_cache, dealer_discard=None,
                   profile_name="Arbiter", known_hands=None):
    root_player = sim_state.current_turn
    root = AlphaNode(player_idx=root_player)

    root_active_cards = [c for h in sim_state.hands for c in h] + [c for _, c in sim_state.trick]
    known_cards = list(sim_state.hands[root_player]) + current_played_cards + [c for _, c in sim_state.trick]
    for seat, hand in (known_hands or {}).items():
        if seat != root_player:
            known_cards.extend(hand)
    if up_card: known_cards.append(up_card)

    deck = [Card(r, s) for s in SUITS_T for r in RANKS_T]
    known_set = {(kc.rank, kc.suit) for kc in known_cards}
    unknown_cards_base = [c for c in deck if (c.rank, c.suit) not in known_set]

    for _ in range(iterations):
        unknown_cards = unknown_cards_base[:]
        random.shuffle(unknown_cards)

        sim_copy = SimState(
            sim_state.trump_suit, list(sim_state.trick),
            [list(h) if i == root_player else [] for i, h in enumerate(sim_state.hands)],
            sim_state.current_turn, sim_state.is_loner, sim_state.loner_partner_idx,
            sim_state.caller_idx, {k: set(v) for k, v in sim_state.voids.items()},
            sim_state.team1_tricks, sim_state.team2_tricks
        )

        for i in range(4):
            if i != root_player:
                if known_hands and i in known_hands:
                    sim_copy.hands[i] = list(known_hands[i])
                    continue
                expected_size = 5 - (sim_copy.team1_tricks + sim_copy.team2_tricks)
                if any(p == i for p, c in sim_copy.trick): expected_size -= 1
                dealt = 0
                if sim_copy.trump_suit == up_card.suit and i == dealer_idx:
                    up_played = any(c.rank == up_card.rank and c.suit == up_card.suit for c in current_played_cards)
                    up_in_trick = any(c.rank == up_card.rank and c.suit == up_card.suit for _, c in sim_copy.trick)
                    if not up_played and not up_in_trick:
                        sim_copy.hands[i].append(Card(up_card.rank, up_card.suit))
                        dealt += 1
                uc_idx = 0
                fallback_cards = []
                while dealt < expected_size and uc_idx < len(unknown_cards):
                    card = unknown_cards[uc_idx]
                    if sim_copy.get_effective_suit(card) not in sim_copy.voids[i]:
                        sim_copy.hands[i].append(card)
                        unknown_cards.pop(uc_idx)
                        dealt += 1
                    else:
                        fallback_cards.append(card)
                        uc_idx += 1
                while dealt < expected_size and fallback_cards:
                    card = fallback_cards.pop(0)
                    sim_copy.hands[i].append(card)
                    unknown_cards.remove(card)
                    dealt += 1

        node = root
        search_path = [node]

        while node.children and (sim_copy.team1_tricks + sim_copy.team2_tricks) < 5:
            legal_moves = sim_copy.get_legal_moves()
            valid_children = [c for c in node.children if c.move in legal_moves]
            if not valid_children: break

            best_score = -float('inf')
            best_node = None
            parent_visits_sqrt = math.sqrt(node.visits)
            for child in valid_children:
                q = (child.wins / child.visits) if child.visits > 0 else 0.0
                u = 1.5 * child.prior * parent_visits_sqrt / (1 + child.visits)
                score = q + u
                if score > best_score:
                    best_score = score
                    best_node = child

            node = best_node
            if sim_copy.trick:
                led_suit = sim_copy.get_effective_suit(sim_copy.trick[0][1])
                eff_suit = sim_copy.get_effective_suit(node.move)
                if eff_suit != led_suit:
                    sim_copy.voids[sim_copy.current_turn].add(led_suit)
            sim_copy.apply_move(node.move)
            node.player_idx = sim_copy.current_turn
            search_path.append(node)

        leaf_player = sim_copy.current_turn

        if (sim_copy.team1_tricks + sim_copy.team2_tricks) >= 5:
            leaf_team = 1 if leaf_player in [0, 2] else 2
            caller_team = 1 if sim_copy.caller_idx in [0, 2] else 2
            caller_tricks = sim_copy.team1_tricks if caller_team == 1 else sim_copy.team2_tricks
            if caller_tricks >= 5:
                caller_pts = 4 if sim_copy.is_loner else 2  # march (alone = 4 pts)
            elif caller_tricks >= 3:
                caller_pts = 1                              # made the call (1 pt)
            else:
                caller_pts = -2                             # euchred (defenders +2)
            caller_v = caller_pts / 4.0
            v = caller_v if leaf_team == caller_team else -caller_v
        else:
            leaf_active_cards = [c for h in sim_copy.hands for c in h] + [c for _, c in sim_copy.trick]
            leaf_active_set = {(c.rank, c.suit) for c in leaf_active_cards}
            rollout_played = [c for c in root_active_cards if (c.rank, c.suit) not in leaf_active_set]
            sim_played = current_played_cards + rollout_played

            state_key = (
                leaf_player,
                frozenset((c.rank, c.suit) for c in sim_copy.hands[leaf_player]),
                tuple((p, c.rank, c.suit) for p, c in sim_copy.trick),
                frozenset((c.rank, c.suit) for c in sim_played),
                sim_copy.trump_suit, sim_copy.caller_idx, sim_copy.is_loner, sim_copy.loner_partner_idx,
                sim_copy.team1_tricks, sim_copy.team2_tricks,
                frozenset((k, frozenset(v)) for k, v in sim_copy.voids.items()),
                t1_score, t2_score
            )

            if state_key in nn_cache:
                probs, v = nn_cache[state_key]
            else:
                wrapper = TensorStateWrapper(sim_copy, sim_played, up_card, dealer_idx, t1_score, t2_score, dealer_discard)
                raw_state = encode_state_to_tensor(wrapper, leaf_player)
                ts = raw_state if isinstance(raw_state, torch.Tensor) else torch.tensor(raw_state, dtype=torch.float32)
                ts = ts.view(-1)

                # Send (net_id, tensor) down the pipe and block until the GPU server answers.
                # net_id tells the server which of the two loaded models (current/baseline)
                # to batch this request against.
                try:
                    gpu_pipe.send((net_id, ts))
                    gpu_result = gpu_pipe.recv()
                except (EOFError, BrokenPipeError, OSError, RuntimeError) as exc:
                    raise GpuPipeClosedError("GPU pipe closed during play MCTS") from exc
                probs, v = gpu_result['policy'], gpu_result['value']
                nn_cache[state_key] = (probs, v)

            legal_moves = get_tactical_search_moves(sim_copy) if node is root else sim_copy.get_legal_moves()
            priors = {}
            priors_sum = 0.0
            for m in legal_moves:
                abs_idx = ALL_DECK_KEYS_MAP[f"{m.rank}{m.suit}"]
                priors[m] = probs[abs_idx]
                priors_sum += probs[abs_idx]

            for m in legal_moves:
                prior = priors[m] / priors_sum if priors_sum > 0 else 1.0 / len(legal_moves)
                trick_target = 2 if sim_copy.is_loner else 3
                if len(sim_copy.trick) == trick_target:
                    temp_trick = sim_copy.trick + [(leaf_player, m)]
                    led_suit = sim_copy.get_effective_suit(temp_trick[0][1])
                    highest_pwr = -1
                    winner_idx = -1
                    rank_vals = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
                    for p_idx, c in temp_trick:
                        pwr = rank_vals[c.rank]
                        eff_s = sim_copy.get_effective_suit(c)
                        if c.rank == 'J' and c.suit == sim_copy.trump_suit: pwr += 500
                        elif c.rank == 'J' and c.suit == SAME_COLOR_T[sim_copy.trump_suit]: pwr += 400
                        elif eff_s == sim_copy.trump_suit: pwr += 100
                        elif eff_s == led_suit: pwr += 50
                        else: pwr = 0
                        if pwr > highest_pwr:
                            highest_pwr = pwr
                            winner_idx = p_idx
                    next_p = winner_idx
                else:
                    next_p = (leaf_player + 1) % 4
                if sim_copy.is_loner and next_p == sim_copy.loner_partner_idx:
                    next_p = (next_p + 1) % 4
                node.children.append(AlphaNode(move=m, parent=node, prior=prior, player_idx=next_p))

        for n in reversed(search_path):
            n.visits += 1
            if n.parent is not None:
                n.wins += v if (n.parent.player_idx % 2) == (leaf_player % 2) else -v

    if not root.children:
        return random.choice(sim_state.get_legal_moves())
    ranked = sorted(root.children, key=lambda child: child.visits, reverse=True)
    if profile_name == "Saboteur":
        return ranked[-1].move
    if (profile_name == "Risk Manager" and len(ranked) > 1
            and ranked[0].visits - ranked[1].visits <= iterations * 0.05):
        return ranked[1].move
    return ranked[0].move


# ==========================================
# 3. DUPLICATE-DEAL PLAYOUT (deal once, replay with teams swapped to cancel card luck)
# ==========================================
def generate_deal():
    """Generates one random deal (hands, up-card, dealer, pre-hand scores). NO
    contract is pre-assigned anymore (July 2026 bidding overhaul): each playout
    runs a REAL auction where the brains themselves bid, so bidding judgement is
    finally part of what's being evaluated. The deal is played TWICE - once per
    team assignment - so both brains face the literal identical cards."""
    t1_score = random.randint(0, 9)
    t2_score = random.randint(0, 9)

    deck = [Card(r, s) for s in SUITS_T for r in RANKS_T]
    random.shuffle(deck)
    hands = [deck[i*5:(i+1)*5] for i in range(4)]
    up_card = deck[20]

    dealer_idx = random.randint(0, 3)

    return {
        'hands': hands, 'up_card': up_card, 'dealer_idx': dealer_idx,
        't1_score': t1_score, 't2_score': t2_score
    }


def headless_profile_brain(profile_name, seat, t1_score, t2_score,
                           caller_idx=-1, wildcard_brain=None):
    if profile_name in {"Arbiter", "Ironclad", "Kyle", "Committee",
                        "Unanimous Council"}:
        return profile_name
    if profile_name in {"Risk Manager", "Saboteur"}:
        return "Ironclad"
    if profile_name == "The Closer":
        own_score = t1_score if seat in (0, 2) else t2_score
        opponent_score = t2_score if seat in (0, 2) else t1_score
        if own_score >= 8 or own_score > opponent_score:
            return "Ironclad"
        if opponent_score - own_score >= 2:
            return "Kyle"
        return "Arbiter"
    if profile_name == "Counterpuncher":
        return ("Kyle" if caller_idx < 0 or caller_idx % 2 == seat % 2
                else "Ironclad")
    if profile_name == "Wildcard":
        return wildcard_brain or "Arbiter"
    return "Arbiter"


def headless_bid_margins(profile_name, seat, round_num, dealer_idx,
                         t1_score, t2_score):
    if profile_name != "Scoreboard General":
        return 0.0, 0.0
    own_score = t1_score if seat in (0, 2) else t2_score
    opponent_score = t2_score if seat in (0, 2) else t1_score
    score_gap = own_score - opponent_score
    call_margin = loner_margin = 0.0
    if score_gap >= 2 or own_score >= 8:
        call_margin = loner_margin = 0.08
    elif score_gap <= -2:
        call_margin, loner_margin = -0.08, -0.04
    if opponent_score >= 9 and own_score < 9:
        call_margin = min(call_margin, -0.05)
    seat_from_dealer = (seat - dealer_idx) % 4
    if round_num == 2 and seat_from_dealer == 1:
        call_margin -= 0.03
    elif round_num == 1 and seat == dealer_idx:
        call_margin -= 0.02
    return call_margin, loner_margin


def play_dealt_hand(deal, gpu_pipe, iterations, current_is_team1, bid_rollouts=None,
                    return_details=False, profiles=("Arbiter", "Arbiter")):
    """Plays a pre-generated deal. Team 1 = seats 0/2, Team 2 = seats 1/3.
    net_id 0 = current brain, net_id 1 = baseline brain.

    The auction is decided by the brains: each seat takes the argmax of its own
    policy head's bid logits over the legal bid actions (one NN call per decision
    - deterministic and cheap, keeping mirrored orientations comparable). Round-1
    pickups use the dealer's own value head to pick the discard. NOTE: because
    the two brains may bid DIFFERENTLY, mirrored orientations of the same deal
    can produce different contracts - that divergence is bidding skill signal,
    exactly what this evaluation now measures.

    Returns (team1_value, team2_value, team1_tricks, caller_team, is_loner)."""
    team1_net_id = 0 if current_is_team1 else 1
    team2_net_id = 1 if current_is_team1 else 0

    # Deep-copy the hands (the auction pickup and SimState/apply_move mutate them) -
    # the Card objects themselves are never mutated so sharing them is safe.
    hands = [list(h) for h in deal['hands']]
    up_card = deal['up_card']
    dealer_idx = deal['dealer_idx']
    t1_score = deal['t1_score']
    t2_score = deal['t2_score']
    profile_a, profile_b = profiles
    team1_profile = profile_a if current_is_team1 else profile_b
    team2_profile = profile_b if current_is_team1 else profile_a
    wildcard_choices = {
        1: random.choice(("Arbiter", "Ironclad", "Kyle")),
        2: random.choice(("Arbiter", "Ironclad", "Kyle")),
    }

    def profile_for_seat(seat):
        return team1_profile if seat in (0, 2) else team2_profile

    def brain_for_seat(seat, caller_idx=-1):
        team_num = 1 if seat in (0, 2) else 2
        return headless_profile_brain(
            profile_for_seat(seat), seat, t1_score, t2_score, caller_idx,
            wildcard_choices[team_num])

    def nn_eval_for(net_id):
        def nn_eval(tensor):
            try:
                gpu_pipe.send((net_id, tensor))
                resp = gpu_pipe.recv()
            except (EOFError, BrokenPipeError, OSError, RuntimeError) as exc:
                raise GpuPipeClosedError("GPU pipe closed during bid/discard eval") from exc
            return resp['policy'], float(resp['value'])
        return nn_eval

    def budget_for_net(budget, net_id, default=0):
        if budget is None:
            return default
        if isinstance(budget, (tuple, list)):
            return budget[net_id]
        return budget

    def decide_bid(seat, round_num, passed_seats, legal_actions):
        net_id = team1_net_id if seat in (0, 2) else team2_net_id
        profile = profile_for_seat(seat)
        brain_id = brain_for_seat(seat)
        rollouts = budget_for_net(bid_rollouts, net_id)
        if profile == "Unanimous Council":
            rollouts *= 2
        call_margin, loner_margin = headless_bid_margins(
            profile, seat, round_num, dealer_idx, t1_score, t2_score)
        if rollouts > 0:
            visits, _ = run_bid_mcts(
                hands[seat], up_card, dealer_idx, seat, round_num, passed_seats,
                t1_score, t2_score, nn_eval_for(brain_id), rollouts=rollouts,
                known_hands=None,
                call_margin=call_margin, loner_margin=loner_margin)
            ranked = sorted(visits, key=visits.get, reverse=True)
            if profile == "Saboteur":
                return ranked[-1]
            if (profile == "Risk Manager" and len(ranked) > 1
                    and visits[ranked[0]] - visits[ranked[1]] <= 0.05):
                def action_risk(action):
                    return 0 if action == BID_PASS else (
                        2 if bid_action_details(action)[1] else 1)
                return min(ranked[:2], key=action_risk)
            return ranked[0]
        tensor = encode_bid_state(hands[seat], seat, up_card, dealer_idx,
                                  round_num, passed_seats, t1_score, t2_score)
        probs, _ = nn_eval_for(brain_id)(tensor)
        return (min if profile == "Saboteur" else max)(
            legal_actions, key=lambda action: probs[action])

    caller_idx, trump_suit, is_loner, called_round = run_auction(hands, up_card, dealer_idx, decide_bid)
    loner_partner_idx = (caller_idx + 2) % 4 if is_loner else -1

    current_dealer_discard = None
    if called_round == 1:
        dealer_profile = profile_for_seat(dealer_idx)
        dealer_brain = brain_for_seat(dealer_idx, caller_idx)
        dealer_hand = hands[dealer_idx]
        dealer_hand.append(up_card)
        ranked_discards = choose_dealer_discard(
            dealer_hand, trump_suit, caller_idx, is_loner,
            up_card, dealer_idx, t1_score, t2_score, nn_eval_for(dealer_brain),
            known_hands=None,
            return_ranked=True)
        discard_card = (ranked_discards[-1][0] if dealer_profile == "Saboteur"
                        else ranked_discards[1][0]
                        if dealer_profile == "Risk Manager" and len(ranked_discards) > 1
                        and ranked_discards[0][1] - ranked_discards[1][1] <= 0.05
                        else ranked_discards[0][0])
        dealer_hand.remove(discard_card)
        current_dealer_discard = discard_card

    voids = {0: set(), 1: set(), 2: set(), 3: set()}
    sim = SimState(trump_suit, [], hands, (dealer_idx + 1) % 4, is_loner, loner_partner_idx, caller_idx, voids)

    played_cards = []
    nn_cache_t1 = {}
    nn_cache_t2 = {}

    while (sim.team1_tricks + sim.team2_tricks) < 5:
        if sim.current_turn == sim.loner_partner_idx:
            sim.current_turn = (sim.current_turn + 1) % 4
            continue

        legal_moves = sim.get_legal_moves()

        if len(legal_moves) == 1:
            forced_move = legal_moves[0]
            if sim.trick:
                led_suit = sim.get_effective_suit(sim.trick[0][1])
                if sim.get_effective_suit(forced_move) != led_suit:
                    sim.voids[sim.current_turn].add(led_suit)
            trick_ending = len(sim.trick) == (2 if sim.is_loner else 3)
            cards_in_trick = [c for _, c in sim.trick] + [forced_move]
            sim.apply_move(forced_move)
            if trick_ending:
                played_cards.extend(cards_in_trick)
            continue

        acting_team1 = sim.current_turn in (0, 2)
        active_net_id = team1_net_id if acting_team1 else team2_net_id
        active_cache = nn_cache_t1 if acting_team1 else nn_cache_t2
        active_iterations = budget_for_net(iterations, active_net_id)
        active_profile = profile_for_seat(sim.current_turn)
        if active_profile == "Unanimous Council":
            active_iterations *= 2
        active_brain = brain_for_seat(sim.current_turn, caller_idx)

        chosen_move = run_eval_mcts(
            sim, gpu_pipe, active_brain, active_iterations, list(played_cards), up_card, dealer_idx,
            t1_score, t2_score, nn_cache=active_cache,
            dealer_discard=current_dealer_discard, profile_name=active_profile,
            known_hands=None
        )

        if sim.trick:
            led_suit = sim.get_effective_suit(sim.trick[0][1])
            if sim.get_effective_suit(chosen_move) != led_suit:
                sim.voids[sim.current_turn].add(led_suit)

        trick_ending = len(sim.trick) == (2 if sim.is_loner else 3)
        cards_in_trick = [c for _, c in sim.trick] + [chosen_move]
        sim.apply_move(chosen_move)
        if trick_ending:
            played_cards.extend(cards_in_trick)

    caller_team = 1 if sim.caller_idx in [0, 2] else 2
    caller_tricks = sim.team1_tricks if caller_team == 1 else sim.team2_tricks
    if caller_tricks >= 5:
        caller_pts = 4 if sim.is_loner else 2  # march (alone = 4 pts)
    elif caller_tricks >= 3:
        caller_pts = 1                         # made the call (1 pt)
    else:
        caller_pts = -2                        # euchred (defenders +2)
    caller_v = caller_pts / 4.0

    def team_value(team_num):
        return caller_v if team_num == caller_team else -caller_v

    v1 = team_value(1)
    v2 = team_value(2)
    result = (v1, v2, sim.team1_tricks, caller_team, is_loner)
    if not return_details:
        return result
    details = {
        "caller_idx": caller_idx, "caller_team": caller_team,
        "trump": trump_suit, "is_loner": is_loner,
        "called_round": called_round,
        "dealer_discard": (
            str(current_dealer_discard) if current_dealer_discard else None),
        "team1_tricks": sim.team1_tricks,
        "team2_tricks": sim.team2_tricks,
        "caller_points": caller_pts,
        "team1_value": v1, "team2_value": v2,
    }
    return result + (details,)


# ==========================================
# 4. CENTRAL GPU SERVER (MICRO-BATCHING - mirrors the Desktop self-play pipe-server,
#    extended to route each request to whichever of the two loaded models it asked for)
# ==========================================
def run_gpu_server(nets_by_id, parent_pipes, device, stop_event):
    print(f"[GPU Server] Polling matrix engine initialized for {len(parent_pipes)} pipelines.")
    live_pipes = list(parent_pipes)
    while not stop_event.is_set():
        if not live_pipes:
            break
        has_data = False
        for p in live_pipes:
            if p.poll():
                has_data = True
                break

        if has_data:
            time.sleep(0.0001)  # Micro-sleep to let more workers arrive and form a bigger batch

            pending = {net_id: {'tensors': [], 'pipes': []} for net_id in nets_by_id}
            dead_pipes = []

            for pipe in live_pipes:
                if pipe.poll():
                    try:
                        net_id, tensor = pipe.recv()
                        pending[net_id]['tensors'].append(tensor)
                        pending[net_id]['pipes'].append(pipe)
                    except (EOFError, RuntimeError, OSError):
                        dead_pipes.append(pipe)

            for net_id, batch in pending.items():
                if not batch['tensors']:
                    continue
                batched_input = torch.stack(batch['tensors']).to(device)
                with torch.inference_mode():
                    policy_logits, value_rating = nets_by_id[net_id](batched_input)
                    action_probs = F.softmax(policy_logits, dim=1).cpu().numpy()
                    values = value_rating.cpu().numpy()
                for idx, pipe in enumerate(batch['pipes']):
                    try:
                        pipe.send({'policy': action_probs[idx], 'value': values[idx][0]})
                    except (EOFError, BrokenPipeError, RuntimeError, OSError):
                        dead_pipes.append(pipe)

            if dead_pipes:
                dead_set = set(dead_pipes)
                live_pipes = [pipe for pipe in live_pipes if pipe not in dead_set]
        else:
            time.sleep(0.001)


# ==========================================
# 5. OS-LEVEL WORKER PROCESS (plays a slice of deals, reports both mirrored results back)
# ==========================================
def worker_process_loop(worker_id, gpu_pipe, num_deals_assigned, iterations, results_queue,
                        bid_rollouts=None, seed_base=None, include_details=False,
                        profiles=("Arbiter", "Arbiter")):
    worker_seed = (
        int(seed_base) + worker_id if seed_base is not None
        else (os.getpid() * int(time.time())) % 123456789)
    np.random.seed(worker_seed % (2 ** 32 - 1))
    random.seed(worker_seed)

    for deal_index in range(num_deals_assigned):
        try:
            deal_seed = worker_seed * 1000000 + deal_index
            random.seed(deal_seed)
            np.random.seed(deal_seed % (2 ** 32 - 1))
            deal = generate_deal()

            # Orientation A: current brain controls Team 1. Orientation B: teams swapped.
            # Same exact cards both times - this cancels deal-luck almost entirely, leaving
            # mostly the skill difference (plus residual MCTS search-noise) between the pair.
            # Since the brains now BID for themselves, caller/loner can differ between the
            # two orientations - report them per-orientation.
            result_a = play_dealt_hand(
                deal, gpu_pipe, iterations, current_is_team1=True,
                bid_rollouts=bid_rollouts, return_details=include_details,
                profiles=profiles)
            result_b = play_dealt_hand(
                deal, gpu_pipe, iterations, current_is_team1=False,
                bid_rollouts=bid_rollouts, return_details=include_details,
                profiles=profiles)
            combined = result_a[:5] + result_b[:5]
            if include_details:
                ledger = {
                    "format": "bot-euchre-deal-ledger-v1",
                    "deal_seed": deal_seed,
                    "worker_id": worker_id,
                    "worker_deal_index": deal_index,
                    "dealer_idx": deal["dealer_idx"],
                    "starting_score": [deal["t1_score"], deal["t2_score"]],
                    "hands": [[str(card) for card in hand] for hand in deal["hands"]],
                    "up_card": str(deal["up_card"]),
                    "orientation_a": result_a[5],
                    "orientation_b": result_b[5],
                }
                combined += (ledger,)
            results_queue.put(combined)
        except GpuPipeClosedError:
            # Parent process requested shutdown or GPU server is gone.
            break



# ==========================================
# 6. TOURNAMENT DRIVER
# ==========================================
def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    current_path = "arbiter_weights.pth"
    # Optional 5th CLI arg lets you point at a FIXED older checkpoint (e.g. an
    # archived Model_Archive/cheems_weights_gen_001.pth) instead of the rolling
    # one-generation-behind PriorGenCheems.pth. Useful for measuring cumulative
    # long-horizon improvement, since the rolling baseline only ever reveals the
    # (often tiny/noisy) marginal gain of a single generation.
    baseline_path = sys.argv[4] if len(sys.argv) > 4 else "PriorGenCheems.pth"

    if not os.path.exists(current_path):
        print("[Eval] No current arbiter_weights.pth found - skipping evaluation.")
        return
    if not os.path.exists(baseline_path):
        print(f"[Eval] No baseline weights found at '{baseline_path}' - skipping evaluation.")
        return

    current_net = CheemsNeuralNet().to(device)
    current_net.load_state_dict(torch.load(current_path, map_location=device, weights_only=True))
    current_net.eval()

    try:
        baseline_net = CheemsNeuralNet().to(device)
        baseline_net.load_state_dict(torch.load(baseline_path, map_location=device, weights_only=True))
        baseline_net.eval()
    except Exception as e:
        print(f"[Eval] FAILED TO LOAD baseline '{baseline_path}' (likely an architecture/dimension mismatch): {e}")
        print("[Eval] Skipping evaluation until a compatible baseline is provided.")
        return

    generation = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    num_hands = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    iterations = int(sys.argv[3]) if len(sys.argv) > 3 else 50

    # Duplicate dealing: each unique deal is played TWICE (teams swapped, identical
    # cards) to cancel deal-luck, so num_hands worth of games comes from half as many
    # unique deals. Rounds up if num_hands is odd, so total_games may be 1 more.
    num_deals = math.ceil(num_hands / 2)
    total_games = num_deals * 2

    # Oversubscription multiplier for the CPU-bound MCTS worker pool. Tune this the
    # same way you'd tune worker_multiplier in the self-play scripts - more workers
    # means bigger batches reach the GPU per polling cycle, but too many causes the
    # same context-switching thrashing discussed for the self-play scripts.
    worker_multiplier = 6
    total_physical_cores = mp.cpu_count()
    active_workers = min(total_physical_cores * worker_multiplier, num_deals)

    print(f"[Eval] Generation {generation}: running {num_deals} duplicated deals ({total_games} total games, "
          f"{iterations} MCTS iters/decision) across {active_workers} workers - Current vs PriorGen...")
    start = time.time()

    data_queue = mp.Queue()
    parent_pipes = []
    child_pipes = []
    for _ in range(active_workers):
        p, c = mp.Pipe()
        parent_pipes.append(p)
        child_pipes.append(c)
    stop_event = threading.Event()

    # Distribute the deal count evenly across workers - each deal produces both
    # mirrored orientations, so no alternation bookkeeping is needed here anymore.
    base, remainder = divmod(num_deals, active_workers)
    processes = []
    for i in range(active_workers):
        count = base + (1 if i < remainder else 0)
        proc = mp.Process(target=worker_process_loop, args=(i, child_pipes[i], count, iterations, data_queue))
        proc.daemon = True
        proc.start()
        processes.append(proc)

    gpu_thread = threading.Thread(
        target=run_gpu_server, args=({0: current_net, 1: baseline_net}, parent_pipes, device, stop_event), daemon=True
    )
    gpu_thread.start()

    current_values = []
    baseline_values = []
    current_wins = 0
    decisive_hands = 0
    paired_diffs = []  # per-deal (current - baseline) value differential, luck cancelled

    # Euchre-specific breakdown counters. Exactly one side calls each hand, so
    # baseline's caller/defense counts fall out as the complement of current's.
    current_caller_hands = 0
    current_caller_euchred = 0
    current_caller_march = 0
    current_defense_hands = 0
    current_defense_euchre_success = 0
    current_loner_caller_hands = 0
    current_loner_caller_success = 0
    current_loner_defense_hands = 0
    current_loner_defense_stops = 0

    baseline_caller_hands = 0
    baseline_caller_euchred = 0
    baseline_caller_march = 0
    baseline_defense_hands = 0
    baseline_defense_euchre_success = 0
    baseline_loner_caller_hands = 0
    baseline_loner_caller_success = 0
    baseline_loner_defense_hands = 0
    baseline_loner_defense_stops = 0

    for _ in range(num_deals):
        (v1A, v2A, team1_tricksA, caller_teamA, is_lonerA,
         v1B, v2B, team1_tricksB, caller_teamB, is_lonerB) = data_queue.get()

        # Orientation A: current = team1. Orientation B: current = team2 (mirrored,
        # same deal - though the CONTRACT may differ, since the brains bid for
        # themselves now and may judge the same cards differently).
        orientations = [
            (v1A, v2A, (caller_teamA == 1), team1_tricksA, is_lonerA),
            (v2B, v1B, (caller_teamB == 2), 5 - team1_tricksB, is_lonerB),
        ]

        for current_v, baseline_v, current_is_caller, current_tricks, is_loner in orientations:
            current_values.append(current_v)
            baseline_values.append(baseline_v)
            if current_v != baseline_v:
                decisive_hands += 1
                if current_v > baseline_v:
                    current_wins += 1

            baseline_tricks = 5 - current_tricks
            if current_is_caller:
                current_caller_hands += 1
                if current_tricks < 3: current_caller_euchred += 1
                if current_tricks == 5: current_caller_march += 1
                baseline_defense_hands += 1
                if current_tricks < 3: baseline_defense_euchre_success += 1
                if is_loner:
                    current_loner_caller_hands += 1
                    if current_tricks == 5: current_loner_caller_success += 1
                    baseline_loner_defense_hands += 1
                    if current_tricks < 5: baseline_loner_defense_stops += 1
            else:
                baseline_caller_hands += 1
                if baseline_tricks < 3: baseline_caller_euchred += 1
                if baseline_tricks == 5: baseline_caller_march += 1
                current_defense_hands += 1
                if baseline_tricks < 3: current_defense_euchre_success += 1
                if is_loner:
                    baseline_loner_caller_hands += 1
                    if baseline_tricks == 5: baseline_loner_caller_success += 1
                    current_loner_defense_hands += 1
                    if baseline_tricks < 5: current_loner_defense_stops += 1

        # Deal-level paired differential (luck-controlled skill signal): average of the
        # two mirrored orientations' (current - baseline) value on the SAME cards.
        paired_diff = ((v1A - v2A) + (v2B - v1B)) / 2.0
        paired_diffs.append(paired_diff)

    for proc in processes:
        proc.join()
    stop_event.set()
    gpu_thread.join(timeout=2)

    elapsed = time.time() - start
    avg_current = sum(current_values) / len(current_values)
    avg_baseline = sum(baseline_values) / len(baseline_values)
    win_rate = (current_wins / decisive_hands) if decisive_hands > 0 else 0.5

    def safe_rate(n, d):
        return round(n / d, 4) if d > 0 else None

    # Paired (luck-controlled) mean value differential + 95% confidence interval,
    # computed from the per-deal differentials (each already cancels card-luck since
    # both orientations of a deal share identical cards). Uses a normal approximation,
    # reasonable once num_deals is reasonably large (dozens+).
    n_deals_actual = len(paired_diffs)
    paired_mean = sum(paired_diffs) / n_deals_actual if n_deals_actual > 0 else 0.0
    if n_deals_actual > 1:
        paired_variance = sum((d - paired_mean) ** 2 for d in paired_diffs) / (n_deals_actual - 1)
        paired_std = math.sqrt(paired_variance)
        paired_se = paired_std / math.sqrt(n_deals_actual)
    else:
        paired_std = 0.0
        paired_se = 0.0
    ci_low = paired_mean - 1.96 * paired_se
    ci_high = paired_mean + 1.96 * paired_se
    statistically_significant = not (ci_low <= 0.0 <= ci_high)

    print(f"[Eval] Done in {elapsed:.1f}s | Current avg value: {avg_current:+.3f} | PriorGen avg value: {avg_baseline:+.3f} | Current win rate (decisive hands): {win_rate:.1%}")
    sig_str = "YES" if statistically_significant else "no"
    print(f"[Eval] Paired (luck-controlled) mean value diff: {paired_mean:+.4f} | 95% CI: [{ci_low:+.4f}, {ci_high:+.4f}] | Statistically significant: {sig_str}")

    record = {
        "generation": generation,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_path": baseline_path,
        "num_deals": n_deals_actual,
        "total_games": len(current_values),
        "mcts_iterations": iterations,
        "current_avg_value": round(avg_current, 4),
        "priorgen_avg_value": round(avg_baseline, 4),
        "current_win_rate": round(win_rate, 4),
        "decisive_hands": decisive_hands,
        "elapsed_seconds": round(elapsed, 1),

        # Luck-controlled skill signal (from duplicate/mirrored dealing) - this is the
        # metric to trust when judging whether an observed difference is real skill
        # vs. sampling noise from Euchre's inherent card-luck variance.
        "paired_mean_value_diff": round(paired_mean, 4),
        "paired_diff_std": round(paired_std, 4),
        "paired_diff_95ci_low": round(ci_low, 4),
        "paired_diff_95ci_high": round(ci_high, 4),
        "statistically_significant": statistically_significant,

        # Euchre-specific breakdown. Since the July 2026 bidding overhaul the brains
        # bid for themselves, so call rate is now a REAL bidding-judgement signal
        # (0.5 = neutral; persistent deviation = different bidding appetites), and
        # the euchre/march rates now blend hand-selection skill with trick play.
        # "_hands" counts are sample sizes for each bucket.
        "current_call_rate": safe_rate(current_caller_hands, len(current_values)),
        "current_euchre_rate_as_caller": safe_rate(current_caller_euchred, current_caller_hands),
        "current_march_rate_as_caller": safe_rate(current_caller_march, current_caller_hands),
        "current_defense_euchre_rate": safe_rate(current_defense_euchre_success, current_defense_hands),
        "current_loner_success_rate": safe_rate(current_loner_caller_success, current_loner_caller_hands),
        "current_loner_defense_stop_rate": safe_rate(current_loner_defense_stops, current_loner_defense_hands),
        "current_caller_hands": current_caller_hands,
        "current_loner_caller_hands": current_loner_caller_hands,
        "current_loner_defense_hands": current_loner_defense_hands,

        "priorgen_call_rate": safe_rate(baseline_caller_hands, len(current_values)),
        "priorgen_euchre_rate_as_caller": safe_rate(baseline_caller_euchred, baseline_caller_hands),
        "priorgen_march_rate_as_caller": safe_rate(baseline_caller_march, baseline_caller_hands),
        "priorgen_defense_euchre_rate": safe_rate(baseline_defense_euchre_success, baseline_defense_hands),
        "priorgen_loner_success_rate": safe_rate(baseline_loner_caller_success, baseline_loner_caller_hands),
        "priorgen_loner_defense_stop_rate": safe_rate(baseline_loner_defense_stops, baseline_loner_defense_hands),
        "priorgen_caller_hands": baseline_caller_hands,
        "priorgen_loner_caller_hands": baseline_loner_caller_hands,
        "priorgen_loner_defense_hands": baseline_loner_defense_hands
    }

    # Optional 5th CLI arg: output JSONL path. Defaults to the rolling gen-over-gen
    # history; master_flywheel.py passes anchor_evaluation_history.jsonl for the
    # fixed-anchor runs so checkpoint gating (which reads the LAST line of
    # evaluation_history.jsonl) never sees an anchor row.
    log_path = sys.argv[5] if len(sys.argv) > 5 else "evaluation_history.jsonl"
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')
    print(f"[Eval] Result appended to {log_path}")


if __name__ == "__main__":
    mp.set_start_method('spawn', force=True)
    main()
