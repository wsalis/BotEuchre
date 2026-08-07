r"""
Bot Euchre Profile Tournament Evaluator

Runs mirrored, MCTS-guided tournaments between the same neural AI profiles
available in the main game.

Examples:
    python adhoc_headless_evaluation.py Arbiter Ironclad --hands 2000 --mcts 150
    python adhoc_headless_evaluation.py Committee Kyle --hands 1000 --mcts 50
"""

import argparse
import json
import math
import os
import sys
import threading
import time
import traceback
import uuid

import torch
import torch.multiprocessing as mp

from BotEuchreGUI import (
    ARBITER_WEIGHTS_PATH, IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH,
    CheemsNeuralNet, CommitteeNeuralNet, DATA_SCHEMA_VERSION,
    HEADLESS_TOURNAMENT_PROFILES, NODE_ADHOC_HISTORY_PATH,
    NODE_DEAL_LEDGER_PATH, NODE_ID, UnanimousCouncilNeuralNet,
    build_provenance_manifest, migrate_jsonl_schema,
    prepare_node_state, profile_checkpoint_paths, profile_fingerprint)
from headless_evaluation import run_gpu_server, worker_process_loop


def load_model(path, device, label):
    if not os.path.exists(path):
        raise FileNotFoundError(f"No weights found for {label}: {path}")

    net = CheemsNeuralNet().to(device)
    try:
        net.load_state_dict(torch.load(path, map_location=device, weights_only=True))
    except RuntimeError as exc:
        raise RuntimeError(
            f"Could not load {label} from {path}. This usually means the .pth was "
            "created by an older CheemsNeuralNet architecture and cannot be matched "
            "against the current code. Pick a newer compatible generation or compare "
            "models saved from the same architecture."
        ) from exc
    net.eval()
    return net


def load_profile_registry(device):
    base_paths = {
        "Arbiter": ARBITER_WEIGHTS_PATH,
        "Ironclad": IRONCLAD_WEIGHTS_PATH,
        "Kyle": KYLE_WEIGHTS_PATH,
    }
    registry = {
        name: load_model(path, device, name)
        for name, path in base_paths.items()
    }
    members = [registry[name] for name in ("Arbiter", "Ironclad", "Kyle")]
    registry["Committee"] = CommitteeNeuralNet(members).to(device).eval()
    registry["Unanimous Council"] = UnanimousCouncilNeuralNet(members).to(device).eval()
    return registry


def safe_rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator > 0 else None

def planned_paired_deals(paired_std, target_effect, power=0.8):
    if paired_std <= 0 or target_effect <= 0:
        return 2
    z_power = 0.84 if power <= 0.8 else 1.28
    return max(2, math.ceil(((1.96 + z_power) * paired_std / target_effect) ** 2))

def paired_summary(values):
    count = len(values)
    mean = sum(values) / count if count else 0.0
    std = (math.sqrt(sum((value - mean) ** 2 for value in values) / (count - 1))
           if count > 1 else 0.0)
    margin = 1.96 * std / math.sqrt(count) if count > 1 else 0.0
    return mean, std, mean - margin, mean + margin


def run_match(model_a, model_b, args):
    prepare_node_state()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if model_a not in HEADLESS_TOURNAMENT_PROFILES:
        raise ValueError(f"Unknown headless profile: {model_a}")
    if model_b not in HEADLESS_TOURNAMENT_PROFILES:
        raise ValueError(f"Unknown headless profile: {model_b}")
    label_a, label_b = model_a, model_b
    profile_paths_a = profile_checkpoint_paths(label_a)
    profile_paths_b = profile_checkpoint_paths(label_b)
    provenance_paths = list(dict.fromkeys(profile_paths_a + profile_paths_b))
    nets_by_id = load_profile_registry(device)
    run_id = uuid.uuid4().hex

    num_deals = math.ceil(args.hands / 2)
    total_games = num_deals * 2
    active_workers = min(mp.cpu_count() * args.worker_multiplier, num_deals)

    print(f"[AdHoc Eval] {label_a} vs {label_b}")
    print(f"[AdHoc Eval] A checkpoints: {', '.join(profile_paths_a)}")
    print(f"[AdHoc Eval] B checkpoints: {', '.join(profile_paths_b)}")
    print(f"[AdHoc Eval] {num_deals} duplicated deals ({total_games} total games), "
          f"play iters A/B={args.mcts_a}/{args.mcts_b}, "
          f"bid rollouts A/B={args.bid_rollouts_a}/{args.bid_rollouts_b}, "
          f"{active_workers} workers, device={device}")

    start = time.time()
    data_queue = mp.Queue()
    parent_pipes = []
    child_pipes = []
    for _ in range(active_workers):
        parent_pipe, child_pipe = mp.Pipe()
        parent_pipes.append(parent_pipe)
        child_pipes.append(child_pipe)

    stop_event = threading.Event()
    base, remainder = divmod(num_deals, active_workers)
    processes = []
    for worker_id in range(active_workers):
        count = base + (1 if worker_id < remainder else 0)
        proc = mp.Process(
            target=worker_process_loop,
            args=(worker_id, child_pipes[worker_id], count,
                  (args.mcts_a, args.mcts_b), data_queue,
                  (args.bid_rollouts_a, args.bid_rollouts_b), args.seed,
                bool(args.ledger), (label_a, label_b)))
        proc.daemon = True
        proc.start()
        processes.append(proc)

    gpu_thread = threading.Thread(
        target=run_gpu_server,
        args=(nets_by_id, parent_pipes, device, stop_event), daemon=True
    )
    gpu_thread.start()

    a_values = []
    b_values = []
    a_wins = 0
    decisive_hands = 0
    paired_diffs = []

    a_caller_hands = 0
    a_caller_euchred = 0
    a_caller_march = 0
    a_defense_hands = 0
    a_defense_euchre_success = 0
    a_loner_caller_hands = 0
    a_loner_caller_success = 0
    a_loner_defense_hands = 0
    a_loner_defense_stops = 0

    b_caller_hands = 0
    b_caller_euchred = 0
    b_caller_march = 0
    b_defense_hands = 0
    b_defense_euchre_success = 0
    b_loner_caller_hands = 0
    b_loner_caller_success = 0
    b_loner_defense_hands = 0
    b_loner_defense_stops = 0
    if args.ledger:
        migrate_jsonl_schema(args.ledger, "bot-euchre-deal-ledger")
    ledger_handle = open(args.ledger, "a", encoding="utf-8") if args.ledger else None
    stopped_early = False
    last_progress_log_at = start
    progress_every_deals = max(1, min(100, num_deals // 20 or 1))

    try:
        for deal_idx in range(num_deals):
            result = data_queue.get()
            (v1a, v2a, team1_tricks_a, caller_team_a, is_loner_a,
             v1b, v2b, team1_tricks_b, caller_team_b, is_loner_b) = result[:10]
            if ledger_handle:
                ledger = dict(result[10])
                ledger.update({
                    "_schema": "bot-euchre-deal-ledger",
                    "_schema_version": DATA_SCHEMA_VERSION,
                    "run_id": run_id, "model_a": label_a, "model_b": label_b,
                    "seed_base": args.seed, "recorded_at": time.time()})
                ledger_handle.write(json.dumps(ledger, ensure_ascii=False) + "\n")
                ledger_handle.flush()

            # Orientation A: model A = team1. Orientation B: model A = team2.
            orientations = [
                (v1a, v2a, caller_team_a == 1, team1_tricks_a, is_loner_a),
                (v2b, v1b, caller_team_b == 2, 5 - team1_tricks_b, is_loner_b),
            ]

            for a_value, b_value, a_is_caller, a_tricks, is_loner in orientations:
                a_values.append(a_value)
                b_values.append(b_value)
                if a_value != b_value:
                    decisive_hands += 1
                    if a_value > b_value:
                        a_wins += 1

                b_tricks = 5 - a_tricks
                if a_is_caller:
                    a_caller_hands += 1
                    if a_tricks < 3:
                        a_caller_euchred += 1
                    if a_tricks == 5:
                        a_caller_march += 1
                    b_defense_hands += 1
                    if a_tricks < 3:
                        b_defense_euchre_success += 1
                    if is_loner:
                        a_loner_caller_hands += 1
                        if a_tricks == 5:
                            a_loner_caller_success += 1
                        b_loner_defense_hands += 1
                        if a_tricks < 5:
                            b_loner_defense_stops += 1
                else:
                    b_caller_hands += 1
                    if b_tricks < 3:
                        b_caller_euchred += 1
                    if b_tricks == 5:
                        b_caller_march += 1
                    a_defense_hands += 1
                    if b_tricks < 3:
                        a_defense_euchre_success += 1
                    if is_loner:
                        b_loner_caller_hands += 1
                        if b_tricks == 5:
                            b_loner_caller_success += 1
                        a_loner_defense_hands += 1
                        if b_tricks < 5:
                            a_loner_defense_stops += 1

            paired_diffs.append(((v1a - v2a) + (v2b - v1b)) / 2.0)

            deals_done = deal_idx + 1
            now = time.time()
            if (deals_done % progress_every_deals == 0
                    or now - last_progress_log_at >= 60
                    or deals_done == num_deals):
                elapsed_live = max(1e-9, now - start)
                deals_per_sec = deals_done / elapsed_live
                eta_sec = (num_deals - deals_done) / deals_per_sec if deals_per_sec > 0 else 0.0
                print(
                    f"[AdHoc Eval] Progress: {deals_done}/{num_deals} deals "
                    f"({(100.0 * deals_done / num_deals):.1f}%) | "
                    f"elapsed {elapsed_live/60:.1f}m | ETA {eta_sec/60:.1f}m",
                    flush=True)
                last_progress_log_at = now

            if (args.early_stop_min_deals
                    and len(paired_diffs) >= args.early_stop_min_deals):
                _, _, live_low, live_high = paired_summary(paired_diffs)
                if live_low > 0 or live_high < 0:
                    stopped_early = True
                    print(
                        f"[AdHoc Eval] Early stop at {len(paired_diffs)} deals; "
                        f"95% CI excludes zero: [{live_low:+.4f}, {live_high:+.4f}]")
                    break
    finally:
        if ledger_handle:
            ledger_handle.close()
        for proc in processes:
            proc.join(timeout=2)
            if proc.is_alive():
                proc.terminate()
                proc.join()
        stop_event.set()
        gpu_thread.join(timeout=2)

    elapsed = time.time() - start
    avg_a = sum(a_values) / len(a_values)
    avg_b = sum(b_values) / len(b_values)
    win_rate = (a_wins / decisive_hands) if decisive_hands > 0 else 0.5

    n_deals_actual = len(paired_diffs)
    paired_mean, paired_std, ci_low, ci_high = paired_summary(paired_diffs)
    statistically_significant = not (ci_low <= 0.0 <= ci_high)

    print(f"[AdHoc Eval] Done in {elapsed:.1f}s | {label_a} avg value: {avg_a:+.3f} | "
          f"{label_b} avg value: {avg_b:+.3f} | {label_a} win rate: {win_rate:.1%}")
    print(f"[AdHoc Eval] Paired mean value diff ({label_a} - {label_b}): {paired_mean:+.4f} | "
          f"95% CI: [{ci_low:+.4f}, {ci_high:+.4f}] | "
          f"Statistically significant: {'YES' if statistically_significant else 'no'}")

    record = {
        "run_type": "adhoc_headless_evaluation",
            "node_id": NODE_ID,
        "run_id": run_id,
        "label": args.label,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model_a": label_a,
        "model_a_paths": profile_paths_a,
        "model_a_sha256": profile_fingerprint(label_a),
        "model_b": label_b,
        "model_b_paths": profile_paths_b,
        "model_b_sha256": profile_fingerprint(label_b),
        "seed": args.seed,
        "mirrored_deals": True,
        "num_deals": n_deals_actual,
        "requested_deals": num_deals,
        "stopped_early": stopped_early,
        "early_stop_min_deals": args.early_stop_min_deals,
        "deal_ledger": args.ledger or None,
        "total_games": len(a_values),
        "model_a_mcts_iterations": args.mcts_a,
        "model_b_mcts_iterations": args.mcts_b,
        "model_a_bid_rollouts": args.bid_rollouts_a,
        "model_b_bid_rollouts": args.bid_rollouts_b,
        "model_a_avg_value": round(avg_a, 4),
        "model_b_avg_value": round(avg_b, 4),
        "model_a_win_rate": round(win_rate, 4),
        "decisive_hands": decisive_hands,
        "elapsed_seconds": round(elapsed, 1),
        "paired_mean_value_diff": round(paired_mean, 4),
        "paired_diff_std": round(paired_std, 4),
        "paired_diff_95ci_low": round(ci_low, 4),
        "paired_diff_95ci_high": round(ci_high, 4),
        "statistically_significant": statistically_significant,
        "model_a_call_rate": safe_rate(a_caller_hands, len(a_values)),
        "model_a_euchre_rate_as_caller": safe_rate(a_caller_euchred, a_caller_hands),
        "model_a_march_rate_as_caller": safe_rate(a_caller_march, a_caller_hands),
        "model_a_defense_euchre_rate": safe_rate(a_defense_euchre_success, a_defense_hands),
        "model_a_loner_success_rate": safe_rate(a_loner_caller_success, a_loner_caller_hands),
        "model_a_loner_defense_stop_rate": safe_rate(a_loner_defense_stops, a_loner_defense_hands),
        "model_a_caller_hands": a_caller_hands,
        "model_a_loner_caller_hands": a_loner_caller_hands,
        "model_a_loner_defense_hands": a_loner_defense_hands,
        "model_b_call_rate": safe_rate(b_caller_hands, len(a_values)),
        "model_b_euchre_rate_as_caller": safe_rate(b_caller_euchred, b_caller_hands),
        "model_b_march_rate_as_caller": safe_rate(b_caller_march, b_caller_hands),
        "model_b_defense_euchre_rate": safe_rate(b_defense_euchre_success, b_defense_hands),
        "model_b_loner_success_rate": safe_rate(b_loner_caller_success, b_loner_caller_hands),
        "model_b_loner_defense_stop_rate": safe_rate(b_loner_defense_stops, b_loner_defense_hands),
        "model_b_caller_hands": b_caller_hands,
        "model_b_loner_caller_hands": b_loner_caller_hands,
        "model_b_loner_defense_hands": b_loner_defense_hands,
        "provenance": build_provenance_manifest(
            provenance_paths, configuration={
                "profile_a": label_a, "profile_b": label_b,
                "hands": args.hands, "mcts_a": args.mcts_a,
                "mcts_b": args.mcts_b,
                "bid_rollouts_a": args.bid_rollouts_a,
                "bid_rollouts_b": args.bid_rollouts_b,
                "worker_multiplier": args.worker_multiplier,
                "seed": args.seed, "mirrored_deals": True,
                "early_stop_min_deals": args.early_stop_min_deals,
                "deal_ledger": args.ledger or None,
            }, extra_environment={
                "torch": torch.__version__, "device": str(device),
                "cuda_version": torch.version.cuda,
                "cuda_device": (
                    torch.cuda.get_device_name(0)
                    if torch.cuda.is_available() else None),
            }),
    }

    if args.log:
        with open(args.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(f"[AdHoc Eval] Result appended to {args.log}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run an ad-hoc Arbiter headless profile matchup."
    )
    parser.add_argument("model_a", choices=HEADLESS_TOURNAMENT_PROFILES,
                        help="First main-game AI profile")
    parser.add_argument("model_b", choices=HEADLESS_TOURNAMENT_PROFILES,
                        help="Second main-game AI profile")
    parser.add_argument("--hands", type=int, default=1000, help="Total games requested; duplicate deals are played twice")
    parser.add_argument("--mcts", type=int, default=None, help="Legacy shared MCTS iterations for both brains")
    parser.add_argument("--mcts-a", type=int, default=50, help="Model A play MCTS iterations")
    parser.add_argument("--mcts-b", type=int, default=50, help="Model B play MCTS iterations")
    parser.add_argument("--bid-rollouts-a", type=int, default=0, help="Model A bid MCTS rollouts; 0 uses raw bid-head argmax")
    parser.add_argument("--bid-rollouts-b", type=int, default=0, help="Model B bid MCTS rollouts; 0 uses raw bid-head argmax")
    parser.add_argument("--worker-multiplier", type=int, default=6, help="CPU worker oversubscription multiplier")
    parser.add_argument("--seed", type=int, default=20260801, help="Deterministic worker/deal seed")
    parser.add_argument("--label", default="adhoc_check", help="Label recorded in the log entry")
    parser.add_argument("--log", default=NODE_ADHOC_HISTORY_PATH, help="JSONL log path; use an empty string to skip logging")
    parser.add_argument("--ledger", default=NODE_DEAL_LEDGER_PATH, help="Optional per-deal JSONL ledger path")
    parser.add_argument("--early-stop-min-deals", type=int, default=0,
                        help="Stop when the paired 95%% CI excludes zero after this many deals; 0 disables")
    args = parser.parse_args()
    if args.mcts is not None:
        args.mcts_a = args.mcts
        args.mcts_b = args.mcts
    if args.hands < 1:
        parser.error("--hands must be at least 1")
    if args.mcts_a < 1 or args.mcts_b < 1:
        parser.error("--mcts-a and --mcts-b must be at least 1")
    if args.bid_rollouts_a < 0 or args.bid_rollouts_b < 0:
        parser.error("--bid-rollouts-a and --bid-rollouts-b cannot be negative")
    if args.worker_multiplier < 1:
        parser.error("--worker-multiplier must be at least 1")
    if args.early_stop_min_deals < 0:
        parser.error("--early-stop-min-deals cannot be negative")
    return args


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    parsed_args = parse_args()
    try:
        run_match(parsed_args.model_a, parsed_args.model_b, parsed_args)
    except Exception as exc:
        print(f"[AdHoc Eval] ERROR: {exc}")
        if os.environ.get("BOT_EUCHRE_DEBUG_TRACEBACK") == "1":
            traceback.print_exc()
        sys.exit(1)