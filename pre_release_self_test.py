import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import torch

import GrandmasterEuchreFinalAttempt as app
import rules_invariant_fuzzer
import soak_test_headless


def run_self_test(include_neural=True):
    started = time.perf_counter()
    checks = []

    def record(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed), "detail": str(detail)})

    try:
        golden = app.validate_golden_replays()
        record("Golden replay contracts", golden["ok"],
               f"{golden['checks']} checks; {len(golden['failures'])} failures")
    except Exception as error:
        record("Golden replay contracts", False, error)

    first = [str(card) for card in app.build_seeded_deck(424242)]
    second = [str(card) for card in app.build_seeded_deck(424242)]
    record("Deterministic seeded dealing", first == second and len(set(first)) == 24,
           "24 unique cards reproduced" if first == second else "deck mismatch")

    try:
        with tempfile.NamedTemporaryFile(
                mode="w", dir=app.SCRIPT_DIR, prefix="self-test-",
                suffix=".tmp", delete=False, encoding="utf-8") as probe:
            probe.write("ok")
            probe_path = probe.name
        os.remove(probe_path)
        record("Writable application data path", True, app.SCRIPT_DIR)
    except OSError as error:
        record("Writable application data path", False, error)

    expected = app.CheemsNeuralNet().state_dict()
    for label, path in [
            ("Arbiter", app.ARBITER_WEIGHTS_PATH),
            ("Ironclad", app.IRONCLAD_WEIGHTS_PATH),
            ("Kyle", app.KYLE_WEIGHTS_PATH)]:
        try:
            state = torch.load(path, map_location="cpu", weights_only=True)
            app.validate_checkpoint_state_dict(state, expected)
            record(f"{label} checkpoint compatibility", True,
                   app.checkpoint_fingerprint(path)[:12])
        except Exception as error:
            record(f"{label} checkpoint compatibility", False, error)

    fuzz = rules_invariant_fuzzer.run_fuzzer(case_count=500, seed=20260802)
    record("Rules invariant fuzzer", fuzz["ok"],
           f"{fuzz['cases_completed']} cases; {fuzz['mutations_rejected']} mutations rejected")

    soak = soak_test_headless.run_soak(hand_count=250, seed=20260801)
    record("Headless rules soak", soak["ok"],
           f"{soak['hands_completed']} hands; {soak['moves']} moves")

    if include_neural:
        with tempfile.TemporaryDirectory() as directory:
            log_path = os.path.join(directory, "neural-smoke.jsonl")
            command = [
                sys.executable, os.path.join(app.SCRIPT_DIR, "adhoc_headless_evaluation.py"),
                app.ARBITER_WEIGHTS_PATH, os.path.join(app.SCRIPT_DIR, "PriorGenCheems.pth"),
                "--hands", "2", "--mcts-a", "1", "--mcts-b", "1",
                "--worker-multiplier", "1", "--seed", "424242",
                "--label", "pre-release-self-test", "--log", log_path,
            ]
            completed = subprocess.run(
                command, cwd=app.SCRIPT_DIR, capture_output=True, text=True)
            result_record = None
            if completed.returncode == 0 and os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8") as result_file:
                    result_record = json.loads(result_file.readlines()[-1])
            neural_ok = bool(
                result_record and result_record.get("total_games") == 2
                and result_record.get("mirrored_deals") is True
                and result_record.get("provenance", {}).get("schema")
                == "bot-euchre-provenance-v1")
            detail = (
                "2 mirrored games with provenance"
                if neural_ok else (completed.stderr or completed.stdout)[-500:])
            record("Mirrored neural evaluator smoke", neural_ok, detail)
    else:
        record("Mirrored neural evaluator smoke", True, "Skipped by command-line option")

    return {
        "format": "bot-euchre-pre-release-self-test-v1",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "checks": checks,
        "passed": sum(check["passed"] for check in checks),
        "failed": sum(not check["passed"] for check in checks),
        "ok": all(check["passed"] for check in checks),
    }


def main():
    parser = argparse.ArgumentParser(description="Run Bot Euchre pre-release checks.")
    parser.add_argument("--skip-neural", action="store_true")
    parser.add_argument("--output")
    arguments = parser.parse_args()
    result = run_self_test(include_neural=not arguments.skip_neural)
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if arguments.output:
        with open(arguments.output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered + "\n")
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
