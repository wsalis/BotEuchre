"""
Run an all-profile mirrored headless round robin and generate summary artifacts.

This script shells out to adhoc_headless_evaluation.py for each profile pair,
collects log records for this run, and writes:
- JSON summary (machine-readable)
- Markdown summary (human-readable)

Example:
  py -3 profile_round_robin_summary.py --hands 200 --mcts 100 --bid-rollouts 50
"""

import argparse
import itertools
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import defaultdict

from BotEuchreGUI import HEADLESS_TOURNAMENT_PROFILES, NODE_ADHOC_HISTORY_PATH, NODE_ID, NODE_STATE_DIR

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EVAL_SCRIPT = os.path.join(SCRIPT_DIR, "adhoc_headless_evaluation.py")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _parse_profiles(raw_profiles):
    if not raw_profiles:
        return list(HEADLESS_TOURNAMENT_PROFILES)
    requested = [part.strip() for part in raw_profiles.split(",") if part.strip()]
    valid = set(HEADLESS_TOURNAMENT_PROFILES)
    unknown = [profile for profile in requested if profile not in valid]
    if unknown:
        raise ValueError(f"Unknown profiles: {', '.join(unknown)}")
    if len(requested) < 2:
        raise ValueError("Need at least two profiles for round robin.")
    return requested


def _iter_jsonl(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _find_record_by_label(log_path, label):
    found = None
    for row in _iter_jsonl(log_path):
        if row.get("run_type") == "adhoc_headless_evaluation" and row.get("label") == label:
            found = row
    return found


def _status_bucket(call_rate):
    if call_rate is None:
        return "unknown"
    if call_rate < 0.35:
        return "very selective"
    if call_rate < 0.42:
        return "selective"
    if call_rate < 0.5:
        return "balanced"
    return "aggressive"


def _safe_div(numerator, denominator):
    return (numerator / denominator) if denominator else None


def _round4(value):
    return round(value, 4) if value is not None else None


def _build_profile_aggregates(records, profiles):
    per_profile = {
        profile: {
            "profile": profile,
            "pair_count": 0,
            "win_rate_sum": 0.0,
            "value_diff_sum": 0.0,
            "caller_hands": 0,
            "loner_caller_hands": 0,
            "loner_defense_hands": 0,
            "weighted_call_rate_sum": 0.0,
            "weighted_caller_euchre_rate_sum": 0.0,
            "weighted_march_rate_sum": 0.0,
            "weighted_loner_success_sum": 0.0,
            "weighted_loner_stop_sum": 0.0,
            "weighted_defense_euchre_sum": 0.0,
            "defense_hands_est": 0,
        }
        for profile in profiles
    }

    matrix = {a: {b: None for b in profiles} for a in profiles}
    for profile in profiles:
        matrix[profile][profile] = 0.5

    for record in records:
        a = record["model_a"]
        b = record["model_b"]
        if a not in per_profile or b not in per_profile:
            continue

        diff_ab = float(record.get("paired_mean_value_diff", 0.0))
        win_a = float(record.get("model_a_win_rate", 0.5))
        win_b = 1.0 - win_a
        matrix[a][b] = win_a
        matrix[b][a] = win_b

        pa = per_profile[a]
        pb = per_profile[b]

        pa["pair_count"] += 1
        pb["pair_count"] += 1
        pa["win_rate_sum"] += win_a
        pb["win_rate_sum"] += win_b
        pa["value_diff_sum"] += diff_ab
        pb["value_diff_sum"] += -diff_ab

        for side_profile, side_key in ((a, "model_a"), (b, "model_b")):
            dst = per_profile[side_profile]
            caller_hands = int(record.get(f"{side_key}_caller_hands", 0) or 0)
            loner_caller_hands = int(record.get(f"{side_key}_loner_caller_hands", 0) or 0)
            loner_defense_hands = int(record.get(f"{side_key}_loner_defense_hands", 0) or 0)

            call_rate = record.get(f"{side_key}_call_rate")
            euchre_rate = record.get(f"{side_key}_euchre_rate_as_caller")
            march_rate = record.get(f"{side_key}_march_rate_as_caller")
            loner_success = record.get(f"{side_key}_loner_success_rate")
            loner_stop = record.get(f"{side_key}_loner_defense_stop_rate")
            defense_euchre = record.get(f"{side_key}_defense_euchre_rate")

            dst["caller_hands"] += caller_hands
            dst["loner_caller_hands"] += loner_caller_hands
            dst["loner_defense_hands"] += loner_defense_hands

            if call_rate is not None:
                dst["weighted_call_rate_sum"] += float(call_rate) * caller_hands
            if euchre_rate is not None:
                dst["weighted_caller_euchre_rate_sum"] += float(euchre_rate) * caller_hands
            if march_rate is not None:
                dst["weighted_march_rate_sum"] += float(march_rate) * caller_hands
            if loner_success is not None:
                dst["weighted_loner_success_sum"] += float(loner_success) * loner_caller_hands
            if loner_stop is not None:
                dst["weighted_loner_stop_sum"] += float(loner_stop) * loner_defense_hands

            defense_hands = max(0, int(record.get("total_games", 0) or 0) - caller_hands)
            dst["defense_hands_est"] += defense_hands
            if defense_euchre is not None:
                dst["weighted_defense_euchre_sum"] += float(defense_euchre) * defense_hands

    final_rows = []
    for profile in profiles:
        row = per_profile[profile]
        pair_count = row["pair_count"]
        caller_hands = row["caller_hands"]
        loner_caller_hands = row["loner_caller_hands"]
        loner_defense_hands = row["loner_defense_hands"]
        defense_hands_est = row["defense_hands_est"]

        avg_win = _safe_div(row["win_rate_sum"], pair_count)
        avg_value_diff = _safe_div(row["value_diff_sum"], pair_count)
        call_rate = _safe_div(row["weighted_call_rate_sum"], caller_hands)
        caller_euchre_rate = _safe_div(row["weighted_caller_euchre_rate_sum"], caller_hands)
        march_rate = _safe_div(row["weighted_march_rate_sum"], caller_hands)
        loner_success = _safe_div(row["weighted_loner_success_sum"], loner_caller_hands)
        loner_stop = _safe_div(row["weighted_loner_stop_sum"], loner_defense_hands)
        defense_euchre_rate = _safe_div(row["weighted_defense_euchre_sum"], defense_hands_est)
        loner_share = _safe_div(loner_caller_hands, caller_hands)

        final_rows.append({
            "profile": profile,
            "pair_count": pair_count,
            "avg_win_rate_vs_field": _round4(avg_win),
            "avg_paired_value_diff_vs_field": _round4(avg_value_diff),
            "call_rate": _round4(call_rate),
            "caller_euchre_rate": _round4(caller_euchre_rate),
            "march_rate": _round4(march_rate),
            "defense_euchre_rate": _round4(defense_euchre_rate),
            "loner_share_of_calls": _round4(loner_share),
            "loner_success_rate": _round4(loner_success),
            "loner_defense_stop_rate": _round4(loner_stop),
            "style_bucket": _status_bucket(call_rate),
        })

    final_rows.sort(
        key=lambda row: (
            row["avg_paired_value_diff_vs_field"]
            if row["avg_paired_value_diff_vs_field"] is not None else -999.0),
        reverse=True,
    )
    return final_rows, matrix


def _render_markdown(summary):
    profiles = summary["profiles"]
    rows = summary["profile_rows"]
    matrix = summary["win_matrix"]

    def fmt(value, spec=".4f", missing="-"):
        if value is None:
            return missing
        return format(value, spec)

    lines = []
    lines.append("# Bot Euchre Headless Round Robin Summary")
    lines.append("")
    lines.append(f"- Generated: {summary['generated_at']}")
    lines.append(f"- Node: {summary['node_id']}")
    lines.append(f"- Profiles: {len(profiles)}")
    lines.append(f"- Pairs attempted: {summary['pairs_attempted']}")
    lines.append(f"- Pairs completed: {summary['pairs_completed']}")
    lines.append(f"- Hands per matchup: {summary['config']['hands']}")
    lines.append(f"- Play MCTS: {summary['config']['mcts_a']}/{summary['config']['mcts_b']}")
    lines.append(f"- Bid rollouts: {summary['config']['bid_rollouts_a']}/{summary['config']['bid_rollouts_b']}")
    lines.append("")

    lines.append("## Profile Ranking")
    lines.append("")
    lines.append("| Rank | Profile | Avg Paired Value Diff | Avg Win Rate vs Field | Call Rate | Caller Euchre Rate | Loner Share | Style |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|---|")
    for idx, row in enumerate(rows, start=1):
        lines.append(
            f"| {idx} | {row['profile']} | {fmt(row['avg_paired_value_diff_vs_field'], '+.4f')} | "
            f"{fmt(row['avg_win_rate_vs_field'])} | {fmt(row['call_rate'])} | "
            f"{fmt(row['caller_euchre_rate'])} | {fmt(row['loner_share_of_calls'])} | "
            f"{row['style_bucket']} |"
        )
    lines.append("")

    lines.append("## Head-to-Head Win Matrix")
    lines.append("")
    header = "| Profile | " + " | ".join(profiles) + " |"
    sep = "|---|" + "|".join(["---:" for _ in profiles]) + "|"
    lines.append(header)
    lines.append(sep)
    for a in profiles:
        row_cells = []
        for b in profiles:
            value = matrix[a][b]
            row_cells.append("-" if value is None else f"{value:.3f}")
        lines.append(f"| {a} | " + " | ".join(row_cells) + " |")
    lines.append("")

    lines.append("## Description Draft Notes")
    lines.append("")
    for row in rows:
        notes = []
        if row["call_rate"] is not None:
            if row["call_rate"] < 0.35:
                notes.append("very selective caller")
            elif row["call_rate"] < 0.42:
                notes.append("selective caller")
            elif row["call_rate"] < 0.5:
                notes.append("balanced caller")
            else:
                notes.append("aggressive caller")
        if row["caller_euchre_rate"] is not None:
            if row["caller_euchre_rate"] <= 0.12:
                notes.append("highly reliable when calling")
            elif row["caller_euchre_rate"] >= 0.2:
                notes.append("higher-risk caller")
        if row["loner_share_of_calls"] is not None and row["loner_success_rate"] is not None:
            if row["loner_share_of_calls"] >= 0.18 and row["loner_success_rate"] >= 0.45:
                notes.append("credible loner pressure")
            elif row["loner_share_of_calls"] >= 0.18 and row["loner_success_rate"] < 0.40:
                notes.append("frequent loners with mixed payoff")
        if row["avg_paired_value_diff_vs_field"] is not None:
            if row["avg_paired_value_diff_vs_field"] >= 0.04:
                notes.append("above-field paired value")
            elif row["avg_paired_value_diff_vs_field"] <= -0.04:
                notes.append("below-field paired value")

        joined = ", ".join(notes) if notes else "insufficient signal"
        lines.append(f"- **{row['profile']}**: {joined}.")

    return "\n".join(lines) + "\n"


def _run_pair(command, cwd):
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        for line in proc.stdout:
            print(line.rstrip())
    finally:
        proc.wait()
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="Run full headless profile round robin and summarize results.")
    parser.add_argument("--profiles", default="", help="Comma-separated profile subset (default: all).")
    parser.add_argument("--hands", type=int, default=200, help="Total games per matchup (mirrored deals => half deals).")
    parser.add_argument("--mcts", type=int, default=100, help="Play MCTS iterations for both sides.")
    parser.add_argument("--bid-rollouts", type=int, default=50, help="Bid rollout budget for both sides.")
    parser.add_argument("--worker-multiplier", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--early-stop-min-deals", type=int, default=0)
    parser.add_argument("--log", default=NODE_ADHOC_HISTORY_PATH)
    parser.add_argument("--ledger", default="", help="Optional per-deal ledger path.")
    parser.add_argument("--label-prefix", default="round_robin")
    parser.add_argument("--max-pairs", type=int, default=0, help="Optional cap for quick smoke tests.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip pairs that already have matching labels in log.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue remaining pairs after failures.")
    parser.add_argument("--summary-json", default="")
    parser.add_argument("--summary-md", default="")
    args = parser.parse_args()

    profiles = _parse_profiles(args.profiles)
    pairs = list(itertools.combinations(profiles, 2))
    if args.max_pairs > 0:
        pairs = pairs[:args.max_pairs]

    if args.hands < 2:
        raise SystemExit("--hands must be at least 2 for mirrored evaluation.")

    run_id = uuid.uuid4().hex[:12]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    default_base = os.path.join(NODE_STATE_DIR, f"round_robin_{stamp}_{run_id}")
    summary_json_path = args.summary_json or (default_base + ".json")
    summary_md_path = args.summary_md or (default_base + ".md")

    completed = []
    failed = []

    print(f"[RoundRobin] Node: {NODE_ID}")
    print(f"[RoundRobin] Profiles ({len(profiles)}): {', '.join(profiles)}")
    print(f"[RoundRobin] Pairs to run: {len(pairs)}")

    for pair_index, (a, b) in enumerate(pairs, start=1):
        pair_seed = args.seed + pair_index * 1000
        label = f"{args.label_prefix}:{run_id}:{_slug(a)}_vs_{_slug(b)}"

        if args.skip_existing and _find_record_by_label(args.log, label) is not None:
            print(f"[RoundRobin] [{pair_index}/{len(pairs)}] Skip existing {a} vs {b}")
            completed.append(label)
            continue

        cmd = [
            sys.executable,
            EVAL_SCRIPT,
            a,
            b,
            "--hands", str(args.hands),
            "--mcts-a", str(args.mcts),
            "--mcts-b", str(args.mcts),
            "--bid-rollouts-a", str(args.bid_rollouts),
            "--bid-rollouts-b", str(args.bid_rollouts),
            "--worker-multiplier", str(args.worker_multiplier),
            "--seed", str(pair_seed),
            "--label", label,
            "--log", args.log,
            "--early-stop-min-deals", str(args.early_stop_min_deals),
        ]
        if args.ledger:
            cmd.extend(["--ledger", args.ledger])
        else:
            cmd.extend(["--ledger", ""])

        print(f"\n[RoundRobin] [{pair_index}/{len(pairs)}] {a} vs {b}")
        rc = _run_pair(cmd, SCRIPT_DIR)
        if rc != 0:
            failed.append({"pair": [a, b], "label": label, "returncode": rc})
            print(f"[RoundRobin] FAILED {a} vs {b} (rc={rc})")
            if not args.continue_on_error:
                break
            continue

        if _find_record_by_label(args.log, label) is None:
            failed.append({"pair": [a, b], "label": label, "returncode": 0, "error": "missing log row"})
            print(f"[RoundRobin] FAILED {a} vs {b} (missing log row)")
            if not args.continue_on_error:
                break
            continue

        completed.append(label)

    selected_labels = set(completed)
    selected_records = [
        row for row in _iter_jsonl(args.log)
        if row.get("run_type") == "adhoc_headless_evaluation" and row.get("label") in selected_labels
    ]

    profile_rows, matrix = _build_profile_aggregates(selected_records, profiles)

    summary = {
        "format": "bot-euchre-round-robin-summary-v1",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "node_id": NODE_ID,
        "run_id": run_id,
        "profiles": profiles,
        "pairs_attempted": len(pairs),
        "pairs_completed": len(selected_records),
        "failed_pairs": failed,
        "config": {
            "hands": args.hands,
            "mcts_a": args.mcts,
            "mcts_b": args.mcts,
            "bid_rollouts_a": args.bid_rollouts,
            "bid_rollouts_b": args.bid_rollouts,
            "worker_multiplier": args.worker_multiplier,
            "seed": args.seed,
            "early_stop_min_deals": args.early_stop_min_deals,
            "log": args.log,
            "ledger": args.ledger,
            "label_prefix": args.label_prefix,
        },
        "labels": sorted(selected_labels),
        "profile_rows": profile_rows,
        "win_matrix": matrix,
    }

    output_dir = os.path.dirname(summary_json_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(summary_json_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    markdown = _render_markdown(summary)
    with open(summary_md_path, "w", encoding="utf-8") as handle:
        handle.write(markdown)

    print(f"\n[RoundRobin] Completed records: {len(selected_records)}")
    print(f"[RoundRobin] JSON summary: {summary_json_path}")
    print(f"[RoundRobin] Markdown summary: {summary_md_path}")

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
