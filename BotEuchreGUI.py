"""Canonical Bot Euchre game engine and interactive GUI.

This root copy unifies the Vanilla, Ironclad, and Kyle neural profiles, each
loaded from its own checkpoint file in this directory.
"""

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import random
import math
import threading
import multiprocessing
import concurrent.futures
import csv
import platform
import os
import time
import json
import copy
import shutil
import hashlib
import traceback
import zipfile
import subprocess
import sys
import uuid
from contextlib import contextmanager
from itertools import combinations

if getattr(sys, "frozen", False):
    # PyInstaller build: user data lives beside the exe, bundled read-only assets live in _MEIPASS.
    SCRIPT_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = SCRIPT_DIR

def _safe_node_id(value):
    cleaned = ""
    for character in str(value).strip():
        if character.isascii() and (
                character.isalnum() or character in "._-"):
            cleaned += character
        elif not cleaned.endswith("_"):
            cleaned += "_"
    return cleaned.strip("._-") or "unknown-node"

NODE_ID = _safe_node_id(
    os.environ.get("BOT_EUCHRE_NODE_ID")
    or os.environ.get("BOT_EUCHRE_NODE_ID")
    or platform.node())
NODE_STATE_ROOT = os.path.join(SCRIPT_DIR, "node_state")
NODE_STATE_DIR = os.path.join(NODE_STATE_ROOT, NODE_ID)

# Fixed promoted Gen50 Vanilla, frozen final Gen18 Ironclad, and rolling latest Kyle.
ARBITER_WEIGHTS_PATH = os.path.join(RESOURCE_DIR, "arbiter_weights.pth")
IRONCLAD_WEIGHTS_PATH = os.path.join(RESOURCE_DIR, "ironclad_final_gen18.pth")
KYLE_WEIGHTS_PATH = os.path.join(RESOURCE_DIR, "kyle_weights.pth")
LEGACY_SETTINGS_PATH = os.path.join(SCRIPT_DIR, "bot_euchre_settings.json")
LEGACY_AUTOSAVE_PATH = os.path.join(SCRIPT_DIR, "bot_euchre_autosave.json")
LEGACY_PLAYER_STATS_PATH = os.path.join(SCRIPT_DIR, "player_stats.json")
LEGACY_SEED_LIBRARY_PATH = os.path.join(
    SCRIPT_DIR, "bot_euchre_seed_library.json")
SETTINGS_PATH = os.path.join(NODE_STATE_DIR, "bot_euchre_settings.json")
AUTOSAVE_PATH = os.path.join(NODE_STATE_DIR, "bot_euchre_autosave.json")
PLAYER_STATS_PATH = os.path.join(NODE_STATE_DIR, "player_stats.json")
TOURNAMENT_HISTORY_PATH = os.path.join(
    SCRIPT_DIR, "bot_euchre_tournament_history.jsonl")
LEAGUE_STATE_PATH = os.path.join(SCRIPT_DIR, "bot_euchre_league_state.json")
DIAGNOSTIC_DIR = os.path.join(SCRIPT_DIR, "bot_euchre_diagnostics")
SEED_LIBRARY_PATH = os.path.join(
    NODE_STATE_DIR, "bot_euchre_seed_library.json")
ADHOC_HISTORY_PATH = os.path.join(SCRIPT_DIR, "adhoc_evaluation_history.jsonl")
NODE_ADHOC_HISTORY_PATH = os.path.join(
    NODE_STATE_DIR, "adhoc_evaluation_history.jsonl")
NODE_DEAL_LEDGER_PATH = os.path.join(
    NODE_STATE_DIR, "adhoc_deal_ledger.jsonl")
HUMAN_LEAGUE_PATH = os.path.join(
    NODE_STATE_DIR, "bot_euchre_human_league.json")
HUMAN_LEAGUE_HISTORY_PATH = os.path.join(
    NODE_STATE_DIR, "bot_euchre_human_league_history.jsonl")
TOURNAMENT_LAB_SETTINGS_PATH = os.path.join(
    NODE_STATE_DIR, "bot_euchre_tournament_lab_settings.json")
GOLDEN_REPLAY_PATH = os.path.join(RESOURCE_DIR, "golden_replay_cases.json")
DATA_SCHEMA_VERSION = 2
MIGRATION_BACKUP_DIRNAME = "backups"
FILE_LOCK_TIMEOUT_SECONDS = float(
    os.environ.get("BOT_EUCHRE_LOCK_TIMEOUT_SECONDS", "90"))
FILE_LOCK_STALE_SECONDS = float(
    os.environ.get("BOT_EUCHRE_LOCK_STALE_SECONDS", "180"))

def prepare_node_state():
    os.makedirs(NODE_STATE_DIR, exist_ok=True)
    for legacy, destination in (
            (LEGACY_SETTINGS_PATH, SETTINGS_PATH),
            (LEGACY_PLAYER_STATS_PATH, PLAYER_STATS_PATH),
            (LEGACY_SEED_LIBRARY_PATH, SEED_LIBRARY_PATH)):
        if not os.path.exists(destination) and os.path.exists(legacy):
            try:
                shutil.copy2(legacy, destination)
            except OSError:
                pass
    if not os.path.exists(AUTOSAVE_PATH) and os.path.exists(LEGACY_AUTOSAVE_PATH):
        try:
            os.replace(LEGACY_AUTOSAVE_PATH, AUTOSAVE_PATH)
        except OSError:
            pass

def _pid_is_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        # WinError 87 is commonly raised when the PID does not exist.
        return getattr(error, "winerror", None) not in {87}
    return True


def _lock_file_is_stale(lock_path, stale_after):
    try:
        age_seconds = time.time() - os.path.getmtime(lock_path)
    except FileNotFoundError:
        return False
    if age_seconds > stale_after:
        return True
    try:
        with open(lock_path, "r", encoding="utf-8") as lock_file:
            payload = json.load(lock_file)
    except (OSError, TypeError, ValueError):
        # Give newly-written lock files a short grace period before recovery.
        return age_seconds > 5.0
    owner_node = str(payload.get("node", "")).strip()
    owner_pid = payload.get("pid")
    return owner_node == NODE_ID and not _pid_is_running(owner_pid)


@contextmanager
def cross_process_file_lock(filename, timeout=None, stale_after=None):
    if timeout is None:
        timeout = FILE_LOCK_TIMEOUT_SECONDS
    if stale_after is None:
        stale_after = FILE_LOCK_STALE_SECONDS
    lock_path = f"{filename}.lock"
    deadline = time.monotonic() + timeout
    os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
    while True:
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
                json.dump({
                    "node": NODE_ID, "pid": os.getpid(), "time": time.time()},
                    lock_file)
            break
        except (FileExistsError, PermissionError):
            try:
                if _lock_file_is_stale(lock_path, stale_after):
                    os.remove(lock_path)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for file lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass

def append_jsonl_record(filename, record):
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with cross_process_file_lock(filename):
        with open(filename, "a", encoding="utf-8") as output_file:
            output_file.write(line)
            output_file.flush()
            os.fsync(output_file.fileno())

def build_league_state(name, season_id, profiles, rounds=1, seed=None):
    unique_profiles = list(dict.fromkeys(profiles))
    if len(unique_profiles) < 2:
        raise ValueError("A league requires at least two profiles.")
    rounds = int(rounds)
    if rounds < 1:
        raise ValueError("League rounds must be at least 1.")
    seed = int(seed if seed is not None else random.SystemRandom().randrange(
        0, 2 ** 63))
    fingerprints = profile_fingerprints(unique_profiles)
    roster = []
    for profile in unique_profiles:
        fingerprint = fingerprints[profile]
        roster.append({
            "profile": profile,
            "identity": f"{profile}@{fingerprint[:12]}",
            "fingerprint": fingerprint,
        })
    jobs = []
    for round_index in range(rounds):
        for pair_index, (first, second) in enumerate(combinations(roster, 2)):
            profile_a, profile_b = (
                (first, second) if round_index % 2 == 0 else (second, first))
            job_seed = seed + round_index * 1_000_000 + pair_index * 10_000
            jobs.append({
                "job_id": uuid.uuid4().hex,
                "round": round_index + 1,
                "profile_a": profile_a["profile"],
                "profile_b": profile_b["profile"],
                "identity_a": profile_a["identity"],
                "identity_b": profile_b["identity"],
                "fingerprint_a": profile_a["fingerprint"],
                "fingerprint_b": profile_b["fingerprint"],
                "seed_base": job_seed,
                "starting_dealer": job_seed % 4,
                "status": "queued",
            })
    random.Random(seed).shuffle(jobs)
    return {
        "_schema": "bot-euchre-league-state",
        "_schema_version": DATA_SCHEMA_VERSION,
        "league_id": uuid.uuid4().hex,
        "name": str(name).strip() or "Balanced League",
        "season_id": season_id or "legacy",
        "seed": seed,
        "rounds": rounds,
        "created_at": time.time(),
        "roster": roster,
        "jobs": jobs,
    }

def save_new_league(state, filename=LEAGUE_STATE_PATH):
    with cross_process_file_lock(filename):
        if os.path.exists(filename):
            existing = load_versioned_mapping(
                filename, "bot-euchre-league-state")
            if any(job.get("status") != "completed"
                   for job in existing.get("jobs", [])):
                raise ValueError(
                    "The shared league still has unfinished jobs.")
        atomic_write_json(filename, state)

def load_league_state(filename=LEAGUE_STATE_PATH):
    if not os.path.exists(filename):
        return None
    return load_versioned_mapping(filename, "bot-euchre-league-state")

def _league_roster_is_current(state):
    roster = state.get("roster", [])
    current = profile_fingerprints(
        [member["profile"] for member in roster])
    return [
        member["profile"] for member in roster
        if current[member["profile"]] != member["fingerprint"]]

def claim_league_job(filename=LEAGUE_STATE_PATH, node_id=NODE_ID,
                     stale_after=7200.0):
    with cross_process_file_lock(filename):
        state = load_league_state(filename)
        if not state:
            raise ValueError("No shared league has been created.")
        changed = _league_roster_is_current(state)
        if changed:
            raise ValueError(
                "Frozen league checkpoints changed: " + ", ".join(changed))
        now = time.time()
        candidates = []
        for job in state.get("jobs", []):
            if job.get("status") == "queued":
                candidates.append(job)
            elif (job.get("status") == "claimed"
                  and now - float(job.get("heartbeat_at", 0)) > stale_after):
                candidates.append(job)
        if not candidates:
            return state, None
        job = candidates[0]
        job.update({
            "status": "claimed", "claimed_by": node_id,
            "claimed_at": now, "heartbeat_at": now,
        })
        atomic_write_json(filename, state)
        return state, copy.deepcopy(job)

def heartbeat_league_job(job_id, filename=LEAGUE_STATE_PATH,
                         node_id=NODE_ID):
    with cross_process_file_lock(filename):
        state = load_league_state(filename)
        if not state:
            return False
        for job in state.get("jobs", []):
            if (job.get("job_id") == job_id
                    and job.get("status") == "claimed"
                    and job.get("claimed_by") == node_id):
                job["heartbeat_at"] = time.time()
                atomic_write_json(filename, state)
                return True
    return False

def complete_league_job(job_id, filename=LEAGUE_STATE_PATH,
                        node_id=NODE_ID):
    with cross_process_file_lock(filename):
        state = load_league_state(filename)
        if not state:
            return False
        for job in state.get("jobs", []):
            if (job.get("job_id") == job_id
                    and job.get("status") == "claimed"
                    and job.get("claimed_by") == node_id):
                job.update({
                    "status": "completed", "completed_at": time.time()})
                atomic_write_json(filename, state)
                return True
    return False

def release_league_job(job_id, filename=LEAGUE_STATE_PATH,
                       node_id=NODE_ID):
    with cross_process_file_lock(filename):
        state = load_league_state(filename)
        if not state:
            return False
        for job in state.get("jobs", []):
            if (job.get("job_id") == job_id
                    and job.get("status") == "claimed"
                    and job.get("claimed_by") == node_id):
                for key in ("claimed_by", "claimed_at", "heartbeat_at"):
                    job.pop(key, None)
                job["status"] = "queued"
                atomic_write_json(filename, state)
                return True
    return False

def release_selected_league_claims(job_ids, filename=LEAGUE_STATE_PATH):
    selected = set(job_ids)
    if not selected:
        return 0
    with cross_process_file_lock(filename):
        state = load_league_state(filename)
        if not state:
            raise ValueError("No shared league exists.")
        released = 0
        for job in state.get("jobs", []):
            if (job.get("job_id") in selected
                    and job.get("status") == "claimed"):
                for key in ("claimed_by", "claimed_at", "heartbeat_at"):
                    job.pop(key, None)
                job["status"] = "queued"
                released += 1
        if released:
            atomic_write_json(filename, state)
        return released

def retire_league(filename=LEAGUE_STATE_PATH, archive_dir=None):
    archive_dir = archive_dir or os.path.join(
        os.path.dirname(filename), MIGRATION_BACKUP_DIRNAME)
    with cross_process_file_lock(filename):
        state = load_league_state(filename)
        if not state:
            raise ValueError("No shared league exists.")
        claimed = [
            job for job in state.get("jobs", [])
            if job.get("status") == "claimed"]
        if claimed:
            nodes = sorted(set(
                job.get("claimed_by", "unknown computer") for job in claimed))
            raise ValueError(
                "Cancel the running league matches first. Active claims: "
                + ", ".join(nodes))
        retired = copy.deepcopy(state)
        retired.update({"retired": True, "retired_at": time.time()})
        os.makedirs(archive_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        archive_path = os.path.join(
            archive_dir,
            f"bot_euchre_league_state.retired.{stamp}."
            f"{state.get('league_id', 'unknown')[:12]}.json")
        atomic_write_json(archive_path, retired)
        os.remove(filename)
        return archive_path

def league_tournament_state(league, job):
    checkpoint_paths = list(dict.fromkeys(
        profile_checkpoint_paths(job["profile_a"])
        + profile_checkpoint_paths(job["profile_b"])))
    return {
        "profile_a": job["profile_a"], "profile_b": job["profile_b"],
        "identity_a": job["identity_a"], "identity_b": job["identity_b"],
        "fingerprint_a": job["fingerprint_a"],
        "fingerprint_b": job["fingerprint_b"],
        "games_total": 2, "games_done": 0,
        "wins_a": 0, "wins_b": 0, "points_a": 0, "points_b": 0,
        "hands": 0, "euchres_a": 0, "euchres_b": 0,
        "loners_a": 0, "loners_b": 0,
        "loner_sweeps_a": 0, "loner_sweeps_b": 0,
        "paused": False, "started_at": time.time(),
        "game_started_at": time.time(), "games": [],
        "randomize_each_game": False, "benchmark": True,
        "random_seed": False, "seed_base": job["seed_base"],
        "hand_seeds": [], "mirror_seeds": [], "mirror_phase": 0,
        "starting_dealer": job["starting_dealer"],
        "league_mode": True, "league_id": league["league_id"],
        "league_name": league["name"], "league_job_id": job["job_id"],
        "season_id": league.get("season_id", "legacy"),
        "season_name": league.get("name", "Balanced League"),
        "provenance": build_provenance_manifest(
            checkpoint_paths, configuration={
                "league_id": league["league_id"],
                "league_job_id": job["job_id"],
                "mirrored_games": True, "seed_base": job["seed_base"],
            }),
    }

def build_human_league_state(name, player_name, partner_profile,
                             opponent_profiles, games_per_opponent=2,
                             playoff_teams=4, seed=None):
    opponents = list(dict.fromkeys(
        profile for profile in opponent_profiles
        if profile != partner_profile))
    if len(opponents) < 2:
        raise ValueError("Choose at least two opponents distinct from your partner.")
    games_per_opponent = int(games_per_opponent)
    if games_per_opponent < 1:
        raise ValueError("Games per opponent must be at least 1.")
    playoff_teams = max(2, min(int(playoff_teams), len(opponents)))
    seed = int(seed if seed is not None else random.SystemRandom().randrange(
        0, 2 ** 63))
    fingerprints = profile_fingerprints([partner_profile, *opponents])
    schedule = []
    for round_index in range(games_per_opponent):
        round_opponents = list(opponents)
        random.Random(seed + round_index).shuffle(round_opponents)
        for opponent in round_opponents:
            game_number = len(schedule)
            schedule.append({
                "game_id": uuid.uuid4().hex,
                "opponent": opponent,
                "seed_base": seed + game_number * 10_000,
                "starting_dealer": game_number % 4,
                "status": "queued",
            })
    return {
        "_schema": "bot-euchre-human-league",
        "_schema_version": DATA_SCHEMA_VERSION,
        "league_id": uuid.uuid4().hex,
        "name": str(name).strip() or "My League Season",
        "player_name": str(player_name).strip() or "You",
        "partner": partner_profile,
        "partner_fingerprint": fingerprints[partner_profile],
        "opponents": [{
            "profile": profile, "fingerprint": fingerprints[profile]}
            for profile in opponents],
        "games_per_opponent": games_per_opponent,
        "playoff_teams": playoff_teams,
        "seed": seed, "created_at": time.time(),
        "phase": "regular", "status": "active",
        "schedule": schedule, "current_game_index": 0,
        "results": [], "playoff": None,
    }

def save_human_league_state(state, filename=HUMAN_LEAGUE_PATH):
    atomic_write_json(filename, state)

def load_human_league_state(filename=HUMAN_LEAGUE_PATH):
    if not os.path.exists(filename):
        return None
    return load_versioned_mapping(filename, "bot-euchre-human-league")

def human_league_current_game(state):
    if not state or state.get("status") != "active":
        return None
    if state.get("phase") == "regular":
        index = int(state.get("current_game_index", 0))
        schedule = state.get("schedule", [])
        return schedule[index] if index < len(schedule) else None
    playoff = state.get("playoff") or {}
    queue = playoff.get("queue", [])
    index = int(playoff.get("current_index", 0))
    if index >= len(queue):
        return None
    current_hand_seeds = playoff.setdefault("current_hand_seeds", [])
    return {
        "game_id": playoff.get("series_id"),
        "opponent": queue[index],
        "seed_base": int(state["seed"]) + 100_000_000
                     + index * 1_000_000
                     + int(playoff.get("games_in_series", 0)) * 10_000,
        "starting_dealer": (
            index + int(playoff.get("games_in_series", 0))) % 4,
        "status": "playoff",
        "hand_seeds": current_hand_seeds,
    }

def human_league_standings(state):
    standings = {
        member["profile"]: {
            "profile": member["profile"], "games": 0,
            "human_wins": 0, "opponent_wins": 0,
            "human_points": 0, "opponent_points": 0,
        }
        for member in state.get("opponents", [])}
    for result in state.get("results", []):
        if result.get("phase") != "regular":
            continue
        entry = standings[result["opponent"]]
        entry["games"] += 1
        entry["human_wins" if result["human_won"] else "opponent_wins"] += 1
        entry["human_points"] += int(result["human_score"])
        entry["opponent_points"] += int(result["opponent_score"])
    return standings

def _human_league_playoff_queue(state):
    standings = list(human_league_standings(state).values())
    strongest_first = sorted(
        standings,
        key=lambda entry: (
            entry["opponent_wins"],
            entry["opponent_points"] - entry["human_points"],
            entry["opponent_points"]),
        reverse=True)
    qualifiers = strongest_first[:int(state.get("playoff_teams", 4))]
    return [entry["profile"] for entry in reversed(qualifiers)]

def record_human_league_game(state, human_score, opponent_score):
    game = human_league_current_game(state)
    if game is None:
        raise ValueError("The Human League has no active game.")
    human_score = int(human_score)
    opponent_score = int(opponent_score)
    human_won = human_score >= 10 and human_score > opponent_score
    result = {
        "game_id": game["game_id"], "timestamp": time.time(),
        "phase": state["phase"], "opponent": game["opponent"],
        "partner": state["partner"], "human_won": human_won,
        "human_score": human_score, "opponent_score": opponent_score,
        "seed_base": game["seed_base"],
        "hand_seeds": list(game.get("hand_seeds", [])),
    }
    state.setdefault("results", []).append(result)
    if state["phase"] == "playoff":
        state["playoff"]["current_hand_seeds"] = []
    if state["phase"] == "regular":
        state["schedule"][state["current_game_index"]]["status"] = "completed"
        state["current_game_index"] += 1
        if state["current_game_index"] >= len(state["schedule"]):
            queue = _human_league_playoff_queue(state)
            state["phase"] = "playoff"
            state["playoff"] = {
                "format": "best-of-three gauntlet", "queue": queue,
                "current_index": 0, "human_wins": 0, "opponent_wins": 0,
                "games_in_series": 0, "series_id": uuid.uuid4().hex,
            }
    else:
        playoff = state["playoff"]
        playoff["human_wins" if human_won else "opponent_wins"] += 1
        playoff["games_in_series"] += 1
        if playoff["human_wins"] >= 2:
            playoff["current_index"] += 1
            if playoff["current_index"] >= len(playoff["queue"]):
                state["phase"] = "completed"
                state["status"] = "champion"
                state["completed_at"] = time.time()
            else:
                playoff.update({
                    "human_wins": 0, "opponent_wins": 0,
                    "games_in_series": 0, "series_id": uuid.uuid4().hex,
                    "current_hand_seeds": []})
        elif playoff["opponent_wins"] >= 2:
            state["phase"] = "completed"
            state["status"] = "eliminated"
            state["completed_at"] = time.time()
    return result

def backup_before_migration(filename):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(
        os.path.dirname(os.path.abspath(filename)), MIGRATION_BACKUP_DIRNAME)
    os.makedirs(backup_dir, exist_ok=True)
    backup_name = (
        f"{os.path.basename(filename)}.v{DATA_SCHEMA_VERSION - 1}."
        f"{stamp}.bak")
    backup = os.path.join(backup_dir, backup_name)
    suffix = 1
    while os.path.exists(backup):
        backup_name = (
            f"{os.path.basename(filename)}.v{DATA_SCHEMA_VERSION - 1}."
            f"{stamp}.{suffix}.bak")
        backup = os.path.join(backup_dir, backup_name)
        suffix += 1
    shutil.copy2(filename, backup)
    return backup

def atomic_write_json(filename, payload, replace_attempts=5):
    temporary = (
        f"{filename}.{os.getpid()}.{threading.get_ident()}."
        f"{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as output_file:
            json.dump(payload, output_file, indent=2, ensure_ascii=False)
            output_file.flush()
            os.fsync(output_file.fileno())
        for attempt in range(replace_attempts):
            try:
                os.replace(temporary, filename)
                return
            except PermissionError:
                if attempt + 1 >= replace_attempts:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass

def load_versioned_mapping(filename, schema_name, defaults=None):
    payload = copy.deepcopy(defaults or {})
    if not os.path.exists(filename):
        payload.update({
            "_schema": schema_name, "_schema_version": DATA_SCHEMA_VERSION})
        return payload
    with open(filename, "r", encoding="utf-8") as source:
        loaded = json.load(source)
    if not isinstance(loaded, dict):
        raise ValueError(f"{schema_name} must be a JSON object")
    payload.update(loaded)
    version = int(payload.get("_schema_version", 1))
    if version > DATA_SCHEMA_VERSION:
        raise ValueError(
            f"{schema_name} version {version} is newer than supported version "
            f"{DATA_SCHEMA_VERSION}")
    if version < DATA_SCHEMA_VERSION or payload.get("_schema") != schema_name:
        backup_before_migration(filename)
        payload.update({
            "_schema": schema_name, "_schema_version": DATA_SCHEMA_VERSION})
        atomic_write_json(filename, payload)
    return payload

def load_versioned_list(filename, schema_name, item_key="items"):
    if not os.path.exists(filename):
        return []
    with open(filename, "r", encoding="utf-8") as source:
        loaded = json.load(source)
    if isinstance(loaded, list):
        backup_before_migration(filename)
        items = loaded
        atomic_write_json(filename, {
            "_schema": schema_name, "_schema_version": DATA_SCHEMA_VERSION,
            item_key: items})
        return items
    if not isinstance(loaded, dict) or not isinstance(loaded.get(item_key), list):
        raise ValueError(f"{schema_name} must contain a '{item_key}' list")
    version = int(loaded.get("_schema_version", 1))
    if version > DATA_SCHEMA_VERSION:
        raise ValueError(
            f"{schema_name} version {version} is newer than supported version "
            f"{DATA_SCHEMA_VERSION}")
    if version < DATA_SCHEMA_VERSION or loaded.get("_schema") != schema_name:
        backup_before_migration(filename)
        loaded.update({
            "_schema": schema_name, "_schema_version": DATA_SCHEMA_VERSION})
        atomic_write_json(filename, loaded)
    return loaded[item_key]

def save_versioned_list(filename, schema_name, items, item_key="items"):
    atomic_write_json(filename, {
        "_schema": schema_name, "_schema_version": DATA_SCHEMA_VERSION,
        item_key: items})

def _migrate_jsonl_schema_unlocked(filename, schema_name):
    if not os.path.exists(filename):
        return False
    records = []
    changed = False
    with open(filename, "r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                # Keep evaluation tools resilient if a JSONL file contains an
                # accidental non-JSON line from prior manual edits/tooling.
                changed = True
                continue
            if not isinstance(record, dict):
                changed = True
                continue
            if (record.get("_schema") != schema_name
                    or record.get("_schema_version") != DATA_SCHEMA_VERSION):
                record["_schema"] = schema_name
                record["_schema_version"] = DATA_SCHEMA_VERSION
                changed = True
            records.append(record)
    if not changed:
        return False
    backup_before_migration(filename)
    temporary = filename + ".tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(temporary, filename)
    return True

def migrate_jsonl_schema(filename, schema_name):
    with cross_process_file_lock(filename):
        return _migrate_jsonl_schema_unlocked(filename, schema_name)

def confidence_calibration(events, bin_count=10):
    pending = []
    samples = []
    for event in events:
        if event.get("type") == "play":
            details = event.get("details", {})
            confidence = details.get("confidence")
            player = details.get("player")
            if confidence is not None and player in (0, 1, 2, 3):
                pending.append((max(0.0, min(float(confidence), 100.0)) / 100.0,
                                player % 2))
        elif event.get("type") == "hand_complete":
            details = event.get("details", {})
            team1_won = details.get("team1_tricks", 0) > details.get("team2_tricks", 0)
            samples.extend((confidence, int(team == (0 if team1_won else 1)))
                           for confidence, team in pending)
            pending = []
    bins = []
    for index in range(bin_count):
        low, high = index / bin_count, (index + 1) / bin_count
        members = [(confidence, outcome) for confidence, outcome in samples
                   if low <= confidence < high or index == bin_count - 1 and confidence == 1.0]
        if members:
            bins.append({
                "low": low, "high": high, "count": len(members),
                "predicted": sum(item[0] for item in members) / len(members),
                "observed": sum(item[1] for item in members) / len(members),
            })
    total = len(samples)
    ece = (sum(item["count"] * abs(item["predicted"] - item["observed"])
               for item in bins) / total if total else None)
    return {"samples": total, "bins": bins, "expected_calibration_error": ece}

def canonical_state_hash(state):
    if state is None:
        return None
    encoded = json.dumps(
        state, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]

def checkpoint_fingerprint(filename):
    digest = hashlib.sha256()
    with open(filename, "rb") as checkpoint_file:
        for chunk in iter(lambda: checkpoint_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def build_provenance_manifest(checkpoint_paths=None, configuration=None,
                              extra_environment=None):
    checkpoints = []
    for filename in checkpoint_paths or []:
        absolute = os.path.abspath(filename)
        if not os.path.exists(absolute):
            checkpoints.append({"path": absolute, "exists": False})
            continue
        stat = os.stat(absolute)
        checkpoints.append({
            "path": absolute, "exists": True, "size_bytes": stat.st_size,
            "modified_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            "sha256": checkpoint_fingerprint(absolute),
        })
    engine_stat = os.stat(__file__)
    environment = {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
    }
    environment.update(extra_environment or {})
    return {
        "schema": "bot-euchre-provenance-v1",
        "run_id": uuid.uuid4().hex,
        "recorded_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": list(sys.argv),
        "configuration": configuration or {},
        "environment": environment,
        "engine": {
            "path": os.path.abspath(__file__),
            "size_bytes": engine_stat.st_size,
            "sha256": checkpoint_fingerprint(__file__),
        },
        "checkpoints": checkpoints,
    }

def profile_checkpoint_paths(profile_name):
    checkpoint_groups = {
        "Arbiter": [ARBITER_WEIGHTS_PATH],
        "Ironclad": [IRONCLAD_WEIGHTS_PATH],
        "Kyle": [KYLE_WEIGHTS_PATH],
        "Committee": [ARBITER_WEIGHTS_PATH, IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH],
        "Unanimous Council": [ARBITER_WEIGHTS_PATH, IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH],
        "The Closer": [ARBITER_WEIGHTS_PATH, IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH],
        "Counterpuncher": [IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH],
        "Risk Manager": [IRONCLAD_WEIGHTS_PATH],
        "Iron Monte": [IRONCLAD_WEIGHTS_PATH],
        "Iron Anchor": [IRONCLAD_WEIGHTS_PATH],
        "Iron Sleuth": [IRONCLAD_WEIGHTS_PATH],
        "Iron Closer": [IRONCLAD_WEIGHTS_PATH],
        "Sleuth Score Closer": [IRONCLAD_WEIGHTS_PATH],
        "Iron Clutch": [IRONCLAD_WEIGHTS_PATH],
        "Sleuth Endgame Turbo": [IRONCLAD_WEIGHTS_PATH],
        "Iron Endgame Edge": [IRONCLAD_WEIGHTS_PATH],
        "Sleuth Turbo Closer": [IRONCLAD_WEIGHTS_PATH],
        "Sleuth Risk Budget": [IRONCLAD_WEIGHTS_PATH],
        "Monte Prime": [ARBITER_WEIGHTS_PATH, IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH],
        "Iron Solver": [IRONCLAD_WEIGHTS_PATH],
        "Iron Oracle": [ARBITER_WEIGHTS_PATH, IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH],
        "Saboteur": [IRONCLAD_WEIGHTS_PATH],
        "Scoreboard General": [ARBITER_WEIGHTS_PATH],
        "Copycat": [ARBITER_WEIGHTS_PATH, IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH],
        "Wildcard": [ARBITER_WEIGHTS_PATH, IRONCLAD_WEIGHTS_PATH, KYLE_WEIGHTS_PATH],
    }
    return list(checkpoint_groups.get(profile_name, []))

def profile_fingerprints(profile_names):
    checkpoint_digests = {}
    results = {}
    for profile_name in profile_names:
        components = []
        for path in profile_checkpoint_paths(profile_name):
            normalized = os.path.normcase(os.path.abspath(path))
            if normalized not in checkpoint_digests:
                checkpoint_digests[normalized] = (
                    checkpoint_fingerprint(path) if os.path.exists(path)
                    else f"missing:{os.path.normcase(path)}")
            components.append(checkpoint_digests[normalized])
        material = "|".join([profile_name, "profile-v1", *components])
        results[profile_name] = hashlib.sha256(
            material.encode("utf-8")).hexdigest()
    return results

def profile_fingerprint(profile_name):
    return profile_fingerprints([profile_name])[profile_name]

def profile_identity(profile_name):
    return f"{profile_name}@{profile_fingerprint(profile_name)[:12]}"

def update_elo_ratings(ratings, profile_a, profile_b, score_a, k_factor=24.0):
    updated = dict(ratings)
    rating_a = float(updated.get(profile_a, 1500.0))
    rating_b = float(updated.get(profile_b, 1500.0))
    expected_a = 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))
    delta = k_factor * (float(score_a) - expected_a)
    updated[profile_a] = round(rating_a + delta, 2)
    updated[profile_b] = round(rating_b - delta, 2)
    return updated

def wilson_win_rate_interval(wins, games, z_score=1.96):
    if games < 1:
        return 0.0, 1.0
    proportion = wins / games
    denominator = 1.0 + z_score ** 2 / games
    center = (proportion + z_score ** 2 / (2.0 * games)) / denominator
    margin = (z_score * math.sqrt(
        proportion * (1.0 - proportion) / games
        + z_score ** 2 / (4.0 * games ** 2)) / denominator)
    return max(0.0, center - margin), min(1.0, center + margin)

def load_elo_ratings(filename=TOURNAMENT_HISTORY_PATH):
    ratings = {}
    if not os.path.exists(filename):
        return ratings
    with open(filename, "r", encoding="utf-8") as history_file:
        for line in history_file:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if record.get("type") != "game":
                continue
            profile_a = record.get("profile_a")
            profile_b = record.get("profile_b")
            if not profile_a or not profile_b:
                continue
            score_a = 1.0 if record.get("winner") == profile_a else 0.0
            ratings = update_elo_ratings(
                ratings, profile_a, profile_b, score_a)
    return ratings

def load_elo_standings(filename=TOURNAMENT_HISTORY_PATH, season_id="legacy"):
    standings = {}
    ratings = {}
    if not os.path.exists(filename):
        return standings
    with open(filename, "r", encoding="utf-8") as history_file:
        for line in history_file:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if (record.get("type") != "game"
                    or record.get("season_id", "legacy") != season_id):
                continue
            profile_a = record.get("profile_a")
            profile_b = record.get("profile_b")
            identity_a = record.get("identity_a", profile_a)
            identity_b = record.get("identity_b", profile_b)
            if not all((profile_a, profile_b, identity_a, identity_b)):
                continue
            a_won = record.get("winner") == profile_a
            ratings = update_elo_ratings(
                ratings, identity_a, identity_b, 1.0 if a_won else 0.0)
            for identity, profile, won in [
                    (identity_a, profile_a, a_won),
                    (identity_b, profile_b, not a_won)]:
                entry = standings.setdefault(identity, {
                    "identity": identity, "profile": profile,
                    "fingerprint": identity.rsplit("@", 1)[-1]
                    if "@" in identity else "legacy",
                    "games": 0, "wins": 0, "losses": 0,
                    "opponents": [], "head_to_head": {},
                })
                entry["games"] += 1
                entry["wins" if won else "losses"] += 1
                opponent = identity_b if identity == identity_a else identity_a
                entry["opponents"].append(opponent)
                matchup = entry["head_to_head"].setdefault(
                    opponent, {"wins": 0, "losses": 0})
                matchup["wins" if won else "losses"] += 1
    for identity, entry in standings.items():
        games = entry["games"]
        entry["rating"] = ratings.get(identity, 1500.0)
        entry["schedule_strength"] = round(
            sum(ratings.get(opponent, 1500.0)
                for opponent in entry["opponents"]) / games, 2)
        entry["win_rate"] = entry["wins"] / games
        entry["win_rate_95ci"] = wilson_win_rate_interval(
            entry["wins"], games)
        entry["uncertainty"] = round(400.0 / math.sqrt(max(1, games)), 1)
        entry["provisional"] = games < 20
    return standings

def load_league_standings(league_id, filename=TOURNAMENT_HISTORY_PATH):
    standings = {}
    if not league_id or not os.path.exists(filename):
        return standings
    with open(filename, "r", encoding="utf-8") as history_file:
        for line in history_file:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if (record.get("type") != "game"
                    or record.get("league_id") != league_id):
                continue
            profile_a = record.get("profile_a")
            profile_b = record.get("profile_b")
            identity_a = record.get("identity_a", profile_a)
            identity_b = record.get("identity_b", profile_b)
            try:
                score_a = int(record["score_a"])
                score_b = int(record["score_b"])
            except (KeyError, TypeError, ValueError):
                continue
            if not all((profile_a, profile_b, identity_a, identity_b)):
                continue
            a_won = record.get("winner") == profile_a
            for identity, profile, won, points_for, points_against in [
                    (identity_a, profile_a, a_won, score_a, score_b),
                    (identity_b, profile_b, not a_won, score_b, score_a)]:
                entry = standings.setdefault(identity, {
                    "identity": identity, "profile": profile,
                    "games": 0, "wins": 0, "losses": 0,
                    "points_for": 0, "points_against": 0,
                })
                entry["games"] += 1
                entry["wins" if won else "losses"] += 1
                entry["points_for"] += points_for
                entry["points_against"] += points_against
    for entry in standings.values():
        entry["point_differential"] = (
            entry["points_for"] - entry["points_against"])
        entry["win_rate"] = entry["wins"] / entry["games"]
    return standings


def load_elo_seasons(filename=TOURNAMENT_HISTORY_PATH):
    seasons = {
        "legacy": {
            "season_id": "legacy", "season_name": "Legacy",
            "started_at": 0.0,
        }
    }
    if not os.path.exists(filename):
        return list(seasons.values())
    with open(filename, "r", encoding="utf-8") as history_file:
        for line in history_file:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            season_id = str(record.get("season_id", "legacy"))
            entry = seasons.setdefault(season_id, {
                "season_id": season_id,
                "season_name": record.get("season_name", season_id),
                "started_at": record.get("timestamp", 0.0),
            })
            if record.get("type") == "season_start":
                entry["season_name"] = record.get("season_name", season_id)
                entry["started_at"] = record.get("timestamp", 0.0)
    return sorted(
        seasons.values(), key=lambda item: item["started_at"], reverse=True)

def recommended_performance_preset(cpu_count=None, has_accelerator=False):
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    if has_accelerator or cores >= 12:
        return "Deep"
    if cores >= 6:
        return "Balanced"
    return "Fast"

def percentile(values, percentage):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * float(percentage) / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

def load_jsonl_records(filename):
    records = []
    if not os.path.exists(filename):
        return records
    with open(filename, "r", encoding="utf-8") as source:
        for line in source:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(record, dict):
                records.append(record)
    return records

def load_all_adhoc_records():
    paths = [ADHOC_HISTORY_PATH]
    if os.path.isdir(NODE_STATE_ROOT):
        paths.extend(
            os.path.join(NODE_STATE_ROOT, node, "adhoc_evaluation_history.jsonl")
            for node in os.listdir(NODE_STATE_ROOT))
    records = []
    for history_path in dict.fromkeys(paths):
        records.extend(load_jsonl_records(history_path))
    return sorted(records, key=lambda record: str(record.get("timestamp", "")))

def filter_history_records(records, record_type="All", query="", season="",
                           seed="", significance="All", date_from="",
                           date_to=""):
    filtered = []
    query = query.strip().lower()
    season = season.strip().lower()
    seed = seed.strip()
    for record in records:
        kind = record.get("type") or record.get("run_type", "unknown")
        if record_type != "All" and kind != record_type:
            continue
        if query and query not in json.dumps(record, ensure_ascii=False).lower():
            continue
        record_season = str(record.get("season_name", record.get("season_id", ""))).lower()
        if season and season not in record_season:
            continue
        record_seed = record.get("seed", record.get("seed_base", ""))
        if seed and seed != str(record_seed):
            continue
        significant = record.get("statistically_significant")
        if significance == "Significant" and significant is not True:
            continue
        if significance == "Not significant" and significant is not False:
            continue
        timestamp = record.get("timestamp", "")
        if isinstance(timestamp, (int, float)):
            date_text = time.strftime("%Y-%m-%d", time.localtime(timestamp))
        else:
            date_text = str(timestamp)[:10]
        if date_from and date_text < date_from:
            continue
        if date_to and date_text > date_to:
            continue
        filtered.append(record)
    return filtered

def compare_benchmark_records(first, second):
    metric_names = [
        "paired_mean_value_diff", "model_a_win_rate",
        "model_a_call_rate", "model_a_euchre_rate_as_caller",
        "model_a_march_rate_as_caller", "elapsed_seconds",
    ]
    deltas = {}
    for metric in metric_names:
        before = first.get(metric)
        after = second.get(metric)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            deltas[metric] = round(after - before, 6)
    return {
        "first_label": first.get("label", first.get("timestamp", "Run 1")),
        "second_label": second.get("label", second.get("timestamp", "Run 2")),
        "same_seed": first.get("seed") == second.get("seed"),
        "same_models": (
            first.get("model_a_sha256") == second.get("model_a_sha256")
            and first.get("model_b_sha256") == second.get("model_b_sha256")),
        "deltas": deltas,
    }

def load_seed_library(filename=SEED_LIBRARY_PATH):
    try:
        return load_versioned_list(
            filename, "bot-euchre-seed-library", "seeds")
    except (OSError, TypeError, ValueError):
        return []

def save_seed_library(entries, filename=SEED_LIBRARY_PATH):
    save_versioned_list(
        filename, "bot-euchre-seed-library", entries, "seeds")

def card_from_text(value):
    if not isinstance(value, str) or len(value) < 2:
        raise ValueError(f"Invalid card text: {value!r}")
    return Card(value[:-1], value[-1])

def validate_golden_replays(filename=GOLDEN_REPLAY_PATH):
    with open(filename, "r", encoding="utf-8") as replay_file:
        fixture = json.load(replay_file)
    failures = []
    checks = 0

    def check(name, actual, expected):
        nonlocal checks
        checks += 1
        if actual != expected:
            failures.append({"name": name, "expected": expected, "actual": actual})

    for case in fixture.get("deals", []):
        deck = [str(card) for card in build_seeded_deck(case["seed"])]
        check(f"{case['name']}: deck", deck, case["deck"])
        check(
            f"{case['name']}: hash", canonical_state_hash(deck),
            case["deck_hash"])
    for case in fixture.get("legal_moves", []):
        hands = [[] for _ in range(4)]
        hands[case["current_turn"]] = [
            card_from_text(value) for value in case["hand"]]
        state = SimState(
            case["trump"],
            [(seat, card_from_text(value)) for seat, value in case["trick"]],
            hands, case["current_turn"], False, -1, 0)
        check(
            case["name"], [str(card) for card in state.get_legal_moves()],
            case["expected"])
    for case in fixture.get("tricks", []):
        plays = [
            (seat, card_from_text(value)) for seat, value in case["plays"]]
        check(case["name"], trick_winner(plays, case["trump"]), case["winner"])
    for case in fixture.get("scores", []):
        actual = calculate_hand_score(
            case["caller"], case["loner"], case["team1_tricks"],
            case["team2_tricks"])
        check(case["name"], list(actual), [case["winner"], case["points"]])
    return {
        "format": fixture.get("format"), "checks": checks,
        "failures": failures, "ok": not failures}

def validate_checkpoint_state_dict(checkpoint, expected_state):
    if not isinstance(checkpoint, dict):
        raise ValueError("checkpoint is not a state dictionary")
    missing = sorted(set(expected_state) - set(checkpoint))
    unexpected = sorted(set(checkpoint) - set(expected_state))
    mismatched = []
    for key in set(checkpoint) & set(expected_state):
        actual_shape = tuple(getattr(checkpoint[key], "shape", ()))
        expected_shape = tuple(getattr(expected_state[key], "shape", ()))
        if actual_shape != expected_shape:
            mismatched.append(f"{key}: {actual_shape} != {expected_shape}")
    problems = []
    if missing:
        problems.append(f"missing keys: {', '.join(missing[:5])}")
    if unexpected:
        problems.append(f"unexpected keys: {', '.join(unexpected[:5])}")
    if mismatched:
        problems.append(f"shape mismatches: {', '.join(sorted(mismatched)[:5])}")
    if problems:
        raise ValueError("; ".join(problems))
    return True

# Import PyTorch and Numpy for neural integration
try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    import numpy as np
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

IS_WINDOWS = platform.system() == "Windows"
if IS_WINDOWS:
    import winsound

# ==========================================
# 0. NEURAL NETWORK ARCHITECTURE
# ==========================================
if HAS_TORCH:
    class ResBlock(nn.Module):
        def __init__(self, hidden_size):
            super().__init__()
            self.fc1 = nn.Linear(hidden_size, hidden_size)
            self.ln1 = nn.LayerNorm(hidden_size)
            self.fc2 = nn.Linear(hidden_size, hidden_size)
            self.ln2 = nn.LayerNorm(hidden_size)
            
        def forward(self, x):
            res = x
            out = F.relu(self.ln1(self.fc1(x)))
            out = self.ln2(self.fc2(out))
            out += res
            return F.relu(out)

    class CheemsNeuralNet(nn.Module):
        def __init__(self, hidden_size=256):
            super().__init__()
            self.input_layer = nn.Sequential(
                nn.Linear(307, hidden_size),
                nn.LayerNorm(hidden_size)
            )
            
            self.res1 = ResBlock(hidden_size)
            self.res2 = ResBlock(hidden_size)
            self.res3 = ResBlock(hidden_size)
            self.res4 = ResBlock(hidden_size)
            self.res5 = ResBlock(hidden_size)
            self.res6 = ResBlock(hidden_size)
            self.res7 = ResBlock(hidden_size)
            
            # 33 outputs: 24 card-play actions + 9 bid actions (pass / call suit x4 /
            # call-alone suit x4). See POLICY_SIZE / BID_* constants below.
            self.policy_head = nn.Sequential(
                nn.Linear(hidden_size, 128),
                nn.ReLU(),
                nn.Linear(128, 33)
            )
            
            self.value_head = nn.Sequential(
                nn.Linear(hidden_size, 64),
                nn.ReLU(),
                nn.Linear(64, 1)
            )

        def forward(self, x):
            x = F.relu(self.input_layer(x))
            x = self.res1(x)
            x = self.res2(x)
            x = self.res3(x)
            x = self.res4(x)
            x = self.res5(x)
            x = self.res6(x)
            x = self.res7(x)
            
            policy_logits = self.policy_head(x)
            value_rating = torch.tanh(self.value_head(x))
            return policy_logits, value_rating

    class CommitteeNeuralNet(nn.Module):
        def __init__(self, brains):
            super().__init__()
            self.brains = nn.ModuleList(brains)

        def forward(self, x):
            outputs = [brain(x) for brain in self.brains]
            mean_policy = torch.stack(
                [F.softmax(logits, dim=-1) for logits, _ in outputs]
            ).mean(dim=0)
            mean_value = torch.stack([value for _, value in outputs]).mean(dim=0)
            return torch.log(mean_policy.clamp_min(1e-12)), mean_value

    class UnanimousCouncilNeuralNet(CommitteeNeuralNet):
        def forward(self, x):
            outputs = [brain(x) for brain in self.brains]
            policies = torch.stack([
                F.softmax(logits, dim=-1) for logits, _ in outputs
            ])
            mean_policy = policies.mean(dim=0)
            top_actions = policies.argmax(dim=-1)
            unanimous = (top_actions == top_actions[0:1]).all(dim=0)
            if unanimous.any():
                batch_indices = torch.arange(
                    mean_policy.shape[0], device=mean_policy.device)[unanimous]
                agreed_actions = top_actions[0][unanimous]
                mean_policy[batch_indices, agreed_actions] *= 1.5
                mean_policy = mean_policy / mean_policy.sum(dim=-1, keepdim=True)
            mean_value = torch.stack([value for _, value in outputs]).mean(dim=0)
            return torch.log(mean_policy.clamp_min(1e-12)), mean_value
else:
    class CheemsNeuralNet:
        pass

SUITS_T = ['♣', '♦', '♥', '♠']
RANKS_T = ['9', '10', 'J', 'Q', 'K', 'A']
SAME_COLOR_T = {'♣': '♠', '♠': '♣', '♦': '♥', '♥': '♦'}
ALL_DECK_KEYS = [f"{r}{s}" for s in SUITS_T for r in RANKS_T]
CARD_TO_INDEX = {card: i for i, card in enumerate(ALL_DECK_KEYS)}
SUIT_TO_INDEX = {'♣': 0, '♦': 1, '♥': 2, '♠': 3}

# ==========================================
# BID ACTION SPACE (July 2026 bidding overhaul)
# ==========================================
# The policy head covers cards AND bids in one 33-dim output:
#   indices  0-23 : play this card (ALL_DECK_KEYS order, unchanged)
#   index      24 : PASS
#   indices 25-28 : CALL trump = SUITS_T[i - 25]      (partnered)
#   indices 29-32 : CALL trump = SUITS_T[i - 29] ALONE (loner)
# Round-1 legality: only the up-card suit may be called. Round-2: any suit EXCEPT
# the (turned-down) up-card suit. Stick-the-dealer: the dealer cannot PASS in R2.
POLICY_SIZE = 33
BID_PASS = 24
BID_CALL_BASE = 25
BID_ALONE_BASE = 29
BID_ACTIONS = list(range(24, 33))

# ==========================================
# UI SEARCH DEPTH (tunable)
# ==========================================
CHEEMS_UI_PLAY_ITERS = 1200
CHEEMS_UI_BID_ROLLOUTS = 800
CHEEMS_UI_DISCARD_DETERMINIZATIONS = 64
CHEEMS_UI_ACTION_DELAY_MS = 450
NEURAL_SEARCH_PRESETS = {
    "Fast": (400, 250, 24),
    "Balanced": (1200, 800, 64),
    "Deep": (2400, 1600, 96),
}
DRILL_DESCRIPTIONS = {
    "Standard Match": "Play a normal game to 10 points.",
    "Drill: Loner Defense": "Defend an opponent's loner from the opening lead and preserve your best stopper.",
    "Drill: Dealer Pickup & Discard": "Start as dealer after being ordered up and compare the brains' discard choices.",
    "Drill: Euchre or Bust": "The opponents called trump; your only objective is to take at least three tricks and set them.",
    "Drill: First Lead Laboratory": "Begin immediately after trump is called and compare opening-lead recommendations.",
    "Drill: Closeout at 9 Points": "Bid at 9-9 with game-ending calls, passes, donations, and defense in mind.",
    "Drill: Down 9-6 Comeback": "Find the calculated aggression or loner opportunity needed to erase a 9-6 deficit.",
    "Drill: Partner Called Trump": "Support your partner's call with disciplined trump leads and careful overtrumps.",
    "Drill: Two-Trick Endgame": "Solve the final two tricks of a close hand with twelve cards already revealed.",
    "Drill: Weak Dealer Hand": "Choose the least damaging call after everyone passes and stick-the-dealer applies.",
    "Drill: Bower Management": "Practice leading, protecting, and timing the right and left bowers.",
    "Drill: Call It and Prove It": "You must call trump, then see whether the hand makes its contract or gets euchred.",
    "Drill: Mystery Scenario": "Receive a randomly selected drill without being told which scenario was chosen.",
}
AI_PROFILE_CHOICES = {
    "Arbiter (Vanilla Neural)": "The balanced Gen50 neural checkpoint with standard AlphaZero search.",
    "Ironclad (Conservative Neural)": "The frozen conservative checkpoint, favoring disciplined calls and lower euchre risk.",
    "Kyle (Aggressive Neural)": "The aggressive checkpoint, willing to call thinner hands and press scoring chances.",
    "The Closer (Score-Aware Router)": "Uses Ironclad while leading or near victory, Kyle when trailing, and Vanilla in balanced games.",
    "Unanimous Council (Deep Consensus)": "Reinforces moves all three neural brains independently favor and doubles ensemble search depth.",
    "Risk Manager (Close-Choice Conservative)": "Uses Ironclad evaluations and takes the safer alternative when the top two search choices are nearly tied.",
    "Wildcard (Random Neural Per Hand)": "Chooses Vanilla, Ironclad, or Kyle once per hand and keeps that identity for the full hand.",
    "The MC (Pure MCTS)": "Uses information-set Monte Carlo tree search without a neural checkpoint.",
    "Iron Monte (Hybrid)": "Uses Ironclad for bidding and dealer discard, then switches to deep Ironclad-guided MCTS for trick play.",
    "Iron Sleuth (Probe-First Router)": "Uses Ironclad's bidding discipline while preferring the more information-preserving move when the top options are nearly tied.",
    "Iron Closer (Score-Aware Router)": "Stays conservative when behind, then becomes more assertive in closeout spots once the score margin is favorable.",
    "Iron Clutch (Selective Deepening)": "Uses Sleuth-style bidding and tie-break play, then selectively deepens search in the final tricks.",
    "Iron Endgame Edge (Score-Tuned Endgame)": "Combines Iron Clutch's selective deepening with score-aware tie-break and bidding behavior.",
    "Monte Prime (Council MCTS Hybrid)": "Uses Ironclad for bidding and discard, then searches play more deeply with Unanimous Council guidance.",
    "Iron Solver (Endgame Hybrid)": "Uses Ironclad for bidding and discard, Iron Monte play early, and solver-style deep search for the final two tricks.",
    "Iron Oracle (Bid-Arbitration Hybrid)": "Keeps Ironclad's close bidding choices unless deep bid search strongly disagrees, then uses Monte Prime play.",
}
LEGACY_PROFILE_FALLBACKS = {
    "Noob": "Arbiter",
    "Saboteur": "Ironclad",
    "Hoyle": "Risk Manager",
    "Counterpuncher": "The Closer",
    "Scoreboard General": "Risk Manager",
    "Committee": "Unanimous Council",
    "Card Counter": "The MC",
    # Deactivated profiles map to supported equivalents for old saves.
    "Iron Anchor": "Ironclad",
    "Copycat": "Arbiter",
    "Sleuth Score Closer": "Iron Clutch",
    "Sleuth Risk Budget": "Iron Clutch",
    "Sleuth Endgame Turbo": "Iron Clutch",
    "Sleuth Turbo Closer": "Iron Endgame Edge",
}
NEURAL_PROFILES = {
    "Arbiter", "Ironclad", "Kyle", "The Closer",
    "Unanimous Council", "Risk Manager",
    "Wildcard",
    "Iron Monte", "Iron Sleuth", "Iron Closer",
    "Iron Clutch", "Iron Endgame Edge",
    "Monte Prime", "Iron Solver", "Iron Oracle",
}
HYBRID_MCTS_PROFILES = {
    "Iron Monte", "Monte Prime", "Iron Solver", "Iron Oracle",
    "Iron Clutch", "Iron Endgame Edge"}
HEADLESS_MCTS_PROFILES = {
    "The MC",
}
TOURNAMENT_PROFILES = tuple(
    label.split(" (")[0] for label in AI_PROFILE_CHOICES
)
HEADLESS_TOURNAMENT_PROFILES = tuple(
    profile for profile in TOURNAMENT_PROFILES
    if profile in NEURAL_PROFILES or profile in HEADLESS_MCTS_PROFILES)


def choose_iron_profile_move(profile, ranked_moves, tie_margin, score_gap=0,
                             sleuth_key=None):
    if not ranked_moves:
        return None
    if len(ranked_moves) < 2 or ranked_moves[0][1] - ranked_moves[1][1] > tie_margin:
        return ranked_moves[0]
    if profile == "Iron Sleuth" and sleuth_key is not None:
        return min(ranked_moves[:2], key=sleuth_key)
    if profile == "Iron Closer" and score_gap <= -2:
        return ranked_moves[1]
    if profile == "Iron Clutch" and sleuth_key is not None:
        return min(ranked_moves[:2], key=sleuth_key)
    if profile == "Iron Endgame Edge":
        if score_gap <= -2:
            return ranked_moves[1]
        if sleuth_key is not None:
            return min(ranked_moves[:2], key=sleuth_key)
    return ranked_moves[0]


def normalize_profile_name(profile_name, default="Arbiter", allow_human=False):
    name = str(profile_name or "").strip()
    if allow_human and name == "Human":
        return "Human"
    name = LEGACY_PROFILE_FALLBACKS.get(name, name)
    if name in TOURNAMENT_PROFILES:
        return name
    return default


def profile_label_from_name(profile_name):
    normalized = normalize_profile_name(profile_name)
    for label in AI_PROFILE_CHOICES:
        if label.split(" (")[0] == normalized:
            return label
    return next(iter(AI_PROFILE_CHOICES))


def sanitize_profile_preferences(settings):
    sanitized = dict(settings)
    players = sanitized.get("players", [])
    if not isinstance(players, list):
        players = []
    clean_players = []
    for index in range(3):
        raw = players[index] if index < len(players) else None
        clean_name = normalize_profile_name(
            str(raw).split(" (")[0] if isinstance(raw, str) else None)
        clean_players.append(profile_label_from_name(clean_name))
    sanitized["players"] = clean_players

    favorites = sanitized.get("favorites", [])
    if not isinstance(favorites, list):
        favorites = []
    clean_favorites = []
    for favorite in favorites:
        normalized = normalize_profile_name(favorite)
        if normalized not in clean_favorites:
            clean_favorites.append(normalized)
    if not clean_favorites:
        clean_favorites = ["Arbiter", "Ironclad", "Kyle", "Unanimous Council"]
    sanitized["favorites"] = clean_favorites[:8]
    return sanitized


def load_active_tournament_profiles(default_profiles=None):
    profiles = list(default_profiles or TOURNAMENT_PROFILES)
    try:
        settings = load_versioned_mapping(
            TOURNAMENT_LAB_SETTINGS_PATH,
            "bot-euchre-tournament-lab-settings",
            {"active_profiles": profiles},
        )
    except (OSError, TypeError, ValueError):
        return profiles
    active_profiles = settings.get("active_profiles")
    if not isinstance(active_profiles, list):
        return profiles
    filtered = [
        profile for profile in active_profiles if profile in TOURNAMENT_PROFILES]
    if not settings.get("hybrid_profiles_v1_seen", False):
        for profile in ("Monte Prime", "Iron Solver"):
            if profile in profiles and profile not in filtered:
                filtered.append(profile)
    if not settings.get("iron_oracle_v1_seen", False):
        if "Iron Oracle" in profiles and "Iron Oracle" not in filtered:
            filtered.append("Iron Oracle")
    if not settings.get("iron_profiles_v1_seen", False):
        for profile in ("Iron Sleuth", "Iron Closer"):
            if profile in profiles and profile not in filtered:
                filtered.append(profile)
    if not settings.get("sleuth_variants_v1_seen", False):
        for profile in (
                "Iron Clutch", "Iron Endgame Edge"):
            if profile in profiles and profile not in filtered:
                filtered.append(profile)
    if len(filtered) < 2:
        return profiles
    return filtered


def active_profile_choice_labels(default_profiles=None):
    active = set(load_active_tournament_profiles(default_profiles))
    labels = [
        label for label in AI_PROFILE_CHOICES
        if label.split(" (")[0] in active]
    return labels or list(AI_PROFILE_CHOICES)

def random_tournament_matchup(
        current=None, profiles=TOURNAMENT_PROFILES, chooser=random.choice):
    matchups = [
        (profile_a, profile_b)
        for profile_a in profiles for profile_b in profiles
        if profile_a != profile_b and (profile_a, profile_b) != current]
    if not matchups:
        raise ValueError("At least two tournament profiles are required.")
    return chooser(matchups)


def choose_iron_oracle_bid(legal_actions, policy_probs, search_visits):
    """Keep Ironclad's policy choice unless a close decision gets a strong
    contradictory verdict from deeper bid search."""
    policy_ranked = sorted(
        legal_actions, key=lambda action: policy_probs[action], reverse=True)
    primary = policy_ranked[0]
    policy_gap = (
        float(policy_probs[primary] - policy_probs[policy_ranked[1]])
        if len(policy_ranked) > 1 else 1.0)
    if policy_gap > 0.10 or not search_visits:
        return primary

    search_ranked = sorted(
        search_visits, key=search_visits.get, reverse=True)
    searched = search_ranked[0]
    search_gap = (
        float(search_visits[searched] - search_visits[search_ranked[1]])
        if len(search_ranked) > 1 else float(search_visits[searched]))
    if (searched != primary
            and search_visits[searched] >= 0.55
            and search_gap >= 0.15):
        return searched
    return primary

def resolve_tournament_seed(
        use_random_seed, entered_seed, generator=None):
    if use_random_seed:
        random_source = generator or random.SystemRandom()
        return random_source.randrange(0, 2 ** 63)
    try:
        return int(entered_seed)
    except (TypeError, ValueError) as error:
        raise ValueError("Tournament seed must be a whole number.") from error

HEURISTIC_PROFILES = {"The MC"}
WILDCARD_PROFILES = ("Arbiter", "Ironclad", "Kyle")
PROFILE_CATEGORIES = {
    "Arbiter": "Base Neural", "Ironclad": "Base Neural",
    "Kyle": "Base Neural",
    "Unanimous Council": "Ensemble", "The Closer": "Router",
    "Risk Manager": "Router",
    "Iron Monte": "Hybrid", "Monte Prime": "Hybrid",
    "Iron Solver": "Hybrid", "Iron Oracle": "Hybrid",
    "Iron Sleuth": "Router",
    "Iron Closer": "Router",
    "Iron Clutch": "Hybrid",
    "Iron Endgame Edge": "Hybrid",
    "Wildcard": "Learner", "The MC": "Pure MCTS",
}
HELP_TOPICS = [
    ("Quick Start", (
        "1. On the setup screen, enter your name and choose the AI profile for "
        "your partner and both opponents.\n\n"
        "2. Leave Game Mode on Standard Match for a normal game, or choose a "
        "drill to practice one situation. Press Start Game.\n\n"
        "3. During bidding, use the on-screen buttons to pass, order up the "
        "turned suit, call another suit, or mark a call as alone. If you are "
        "the dealer after a round-one order, click the card you want to discard.\n\n"
        "4. During play, click a card in your hand. The game prevents illegal "
        "plays and requires you to follow the effective led suit when possible.\n\n"
        "5. GAME at the top is the score toward 10 points. TRICKS is the count "
        "inside the current hand. Use Ask an AI when you want advice, or the "
        "Autoplay menu when you want an AI to take over your seat.")),
    ("Setup Screen", (
        "Your Name labels Seat 0, the bottom seat. Left Opponent is Seat 1, "
        "Your Partner is Seat 2, and Right Opponent is Seat 3. Moving through "
        "an AI menu shows a short description of the highlighted profile.\n\n"
        "Trainer Mode opens a post-hand coaching report after hands you play "
        "manually. Dark Mode changes the table theme.\n\n"
        "Hint & Autoplay Neural Search controls how much search is used for "
        "your advice and autoplay. Opponent & Partner Neural Search controls "
        "the table AIs. Higher presets usually improve decisions but take more "
        "time. Pure MCTS Search Power applies to search-only profiles such as "
        "The MC; Workstation is intended for machines with many CPU cores.\n\n"
        "Game Mode / Training Drill selects a normal match or a prepared lesson. "
        "Save Preset stores the entire setup so a favorite table can be loaded "
        "again. Your latest setup and presets are saved automatically.")),
    ("Understanding the Table", (
        "The top bar shows trump, game score, current-hand tricks, Tools, "
        "Autoplay, Stats & Coach, and Main Menu. The D chip marks the dealer. "
        "The large card near the center is the turned up-card during bidding.\n\n"
        "The Deck Tracker on the left becomes useful after trump is called. It "
        "tracks publicly known cards and helps show which important cards are "
        "still live. Show AI Voids displays suits a player has proven unable to "
        "follow; these are deductions from legal play, not hidden information.\n\n"
        "Live Odds is a quick search estimate for your current position. Hand "
        "Power summarizes visible strength. Both are estimates, not guaranteed "
        "outcomes. The action message says who is thinking or what input is "
        "needed. The progress bar means a background search is running.\n\n"
        "The bottom status bar shows phase, active seat and profile, mode, search "
        "budgets, compute device, hand seed, and active worker count.")),
    ("Euchre Rules & Scoring", (
        "The right bower is the Jack of trump and is the highest card. The left "
        "bower is the other Jack of the same color; it counts as trump, not as "
        "its printed suit. This matters when following suit.\n\n"
        "Bidding has two rounds. In round 1, a player may order up the turned "
        "suit. The dealer picks up that card and discards one. In round 2, a "
        "player may call any other suit. This game uses stick-the-dealer: after "
        "the other players pass in round 2, the dealer must call a suit.\n\n"
        "The calling team earns 1 point for taking 3 or 4 tricks and 2 points "
        "for taking all 5. If a lone caller takes all 5, the team earns 4. If "
        "the callers take fewer than 3 tricks, they are euchred and the defenders "
        "earn 2. The first team to 10 game points wins.")),
    ("Bidding, Discarding & Play", (
        "The action area changes with the phase. In bidding, choose Pass or a "
        "legal call; select the alone option before making a call if you want "
        "your partner to sit out. In a loner hand, turns automatically skip the "
        "caller's partner.\n\n"
        "When you are the dealer and the up-card is ordered, your hand briefly "
        "contains six cards. Click the one card to discard.\n\n"
        "During card play, click one of your cards. You must follow the effective "
        "led suit when you can. Remember that the left bower belongs to trump. "
        "When you cannot follow suit, any card is legal.\n\n"
        "Main Menu abandons the current table and returns to setup. Use it when "
        "you want different players, search settings, or a drill.")),
    ("AI Profiles", (
        "Base brains: Arbiter is balanced, Ironclad is conservative about "
        "risk, and Kyle presses thinner opportunities.\n\n"
        "Ensembles: Unanimous Council adds weight when all three base brains "
        "independently agree and uses deeper search.\n\n"
        "Routers: The Closer favors Ironclad while ahead or near victory and Kyle "
        "while behind. Risk Manager chooses the safer alternative when Ironclad's "
        "top choices are close.\n\n"
        "Adaptive personalities: Wildcard chooses one base brain "
        "at random for each hand.\n\n"
        "Search profiles: The MC uses information-set MCTS without a neural "
        "network.\n\n"
        "Profile Inspector shows descriptions, categories, search budgets, style "
        "scores, checkpoint paths, and fingerprints.")),
    ("Ask an AI & Compare", (
        "Ask an AI lets you choose any profile to analyze the decision currently "
        "waiting at your seat. It works for bidding, dealer discard, and card play. "
        "The recommendation includes its leading choice and, where available, a "
        "confidence or search share. Advice does not make the move for you.\n\n"
        "Confidence means how strongly that search preferred its choice, not a "
        "promise that the hand will win. Small differences between the top two "
        "choices often mean both are reasonable. Explanations can compare the "
        "recommendation with the runner-up using visible cards and measured search "
        "results; they are not a transcript of hidden neural thoughts.\n\n"
        "Compare AI Recommendations opens several profiles side by side and shows "
        "where they agree. It is useful for seeing the conservative/aggressive "
        "tradeoff. Press A for a quick Arbiter consultation or T for comparison. "
        "Consultations are recorded in the Decision Journal.")),
    ("Autoplay", (
        "Autoplay gives control of your seat to the selected profile immediately. "
        "It can bid, discard, and play cards, and may be switched while a game is "
        "in progress. The menu and your seat label show the active profile.\n\n"
        "Normal profiles use the same legal information they would have in another "
        "seat.\n\n"
        "Choose Off from the Autoplay menu or press Escape to return control to "
        "yourself. Trainer reports are not shown while Autoplay is controlling you.")),
    ("Trainer Mode", (
        "Enable Trainer Mode on the setup screen to receive a Post-Hand Analysis "
        "after each manually played hand. The report shows Play Accuracy, expected "
        "versus actual tricks, bidding feedback, and specific decisions that the "
        "coach marked as mistakes.\n\n"
        "Rewind Here returns to the saved position around a listed mistake so you "
        "can try the decision again. Replay Hand starts the same hand in Sandbox "
        "Mode without permanently counting its result a second time. Next Hand "
        "accepts the result and continues.\n\n"
        "A low score means the coach's search preferred different actions; it does "
        "not prove every alternative would have won. Card luck and hidden hands "
        "still matter.")),
    ("Training Drills", (
        "Drills build a hand around a particular lesson instead of waiting for it "
        "to appear naturally. Available lessons cover Loner Defense, Dealer Pickup "
        "& Discard, Euchre or Bust, First Lead Laboratory, Closeout at 9 Points, "
        "Down 9-6 Comeback, Partner Called Trump, Two-Trick Endgame, Weak Dealer "
        "Hand, Bower Management, and Call It and Prove It.\n\n"
        "Mystery Scenario chooses a drill without naming it, so you must diagnose "
        "the position yourself. Standard Match is ordinary Euchre.\n\n"
        "A drill ends after its scenario and reports the relevant result. Drill and "
        "sandbox hands are kept separate from ordinary career statistics.")),
    ("Stats & Coach", (
        "Stats & Coach tracks ordinary, non-drill play over time in player_stats.json. "
        "It shows games and wins, call and euchre rates, points per hand, and a "
        "summary note about bidding aggression.\n\n"
        "The detailed counters identify recurring patterns such as stepping on a "
        "partner's trick, losing loner stoppers, missing useful voids, failing to "
        "pull trump, risky loners, stranded aces, trapped left bowers, and other "
        "play errors. Hover over a label for its definition.\n\n"
        "Reset Data permanently clears these career statistics. It does not reset "
        "AI checkpoints, settings, tournament history, or saved seeds.")),
    ("Tournament Mode & Elo", (
        "Tournament Mode runs a series of complete games between two fair-play "
        "profiles on the visible table. Choose Team A, "
        "Team B, and 1-25 games. Randomize teams at start draws one matchup for the "
        "whole series. Randomize after every game draws a new matchup at each game "
        "boundary; Elo and history use the profiles that played each individual game. "
        "Seat labels show the current competitors.\n\n"
        "Fixed-deal benchmark makes the game sequence reproducible from its seed. "
        "Random seed is checked by default and creates a new seed base when the series "
        "starts. Uncheck it to enter a specific seed for a repeatable rematch. The "
        "chosen base is saved in tournament history. The live dashboard shows wins, "
        "points, hands, euchres, loners, loner "
        "sweeps, timing, and per-game history; it can pause, resume, or cancel.\n\n"
        "Completed GUI tournament games are written to Tournament History and feed "
        "the Elo ladder. An unfinished or cancelled series does not become a normal "
        "completed series result.\n\n"
        "Balanced League creates a shared round-robin schedule for selected profiles. "
        "Each matchup job contains two games: Game 2 replays Game 1's hand seeds from "
        "the same starting dealer with the profiles swapping sides. Computers claim "
        "jobs through a shared lock and automatically continue until no work remains. "
        "The roster stores exact checkpoint fingerprints; restart the GUI after "
        "finalizing weights, then create the league. New claims stop if any frozen "
        "checkpoint later changes, preventing mislabeled results. A league keeps "
        "the Elo season that was active when it was created.\n\n"
        "Manage League lists every queued, running, and completed job. To replace an "
        "unfinished league, first cancel its running tournament on each claiming "
        "computer. If a process was terminated instead of canceled, select its "
        "Claimed row and choose Release Selected Claims. Then choose Retire Current "
        "League. Retirement archives the "
        "schedule and discards unplayed jobs; completed games and Elo history remain.\n\n"
        "Standings aggregates completed league games by frozen profile identity. It "
        "shows wins, losses, points for, points against, and point differential. "
        "Wins remain the primary result; point differential helps compare profiles "
        "when mirrored pairs split 1-1. Elo itself remains based only on game wins.")),
    ("Human League Season", (
        "Human League Season keeps seat 0 under your control while a fixed AI partner "
        "plays seat 2. Each selected opponent profile controls both opposing seats. "
        "Choose equal games per opponent; the deterministic schedule rotates the "
        "starting dealer and saves progress after every hand and completed game.\n\n"
        "After the regular season, opponents are seeded by how successfully they "
        "played against your team: wins first, then point differential. Your team "
        "enters a best-of-three gauntlet against the selected qualifiers from lower "
        "seed to strongest seed. Win two games to advance; two losses end the run. "
        "Defeat every qualifier to become league champion.\n\n"
        "Open Tools > Human League Season to create, inspect, or resume the personal "
        "season. It is node-local and separate from automated League Mode and Elo. "
        "Partner and opponent checkpoint identities and search budgets are frozen "
        "when the season starts.")),
    ("Elo Ratings & Seasons", (
        "Elo is a long-term rating for completed GUI tournament games. Every "
        "profile/checkpoint identity starts at 1500. A win transfers rating from "
        "the loser to the winner; beating a stronger opponent is worth more. Ratings "
        "are provisional through 19 games, and the displayed uncertainty shrinks as "
        "more games are recorded. SoS is strength of schedule: the average current "
        "Elo of every opponent that identity faced, weighted by games played. It "
        "updates dynamically when those opponents' ratings change. Win % and its "
        "Wilson 95% interval show result uncertainty directly. Select a row to see "
        "that identity's head-to-head records.\n\n"
        "The season selector can display an archived ladder. Set Active makes that "
        "season receive future GUI tournament games. Start New Season creates a "
        "fresh 1500 ladder without deleting old history.\n\n"
        "Headless Tournament Lab results do not enter this Elo calculation. They "
        "measure paired hands and confidence intervals rather than complete GUI game "
        "winners, so they remain in a separate benchmark history.")),
    ("Headless Tournament Lab", (
        "The Headless Tournament Lab is a separate window for running many mirrored "
        "hands without drawing the table. Both competitor menus use the main game's "
        "fair-play neural profile roster plus supported MCTS profiles (The MC).\n\n"
        "Total games is twice the number of paired deals. For each deal, the profiles "
        "play the same cards twice with team ownership swapped. This removes much of "
        "the luck of a favorable deal. Play iterations and bid rollouts control search "
        "effort; fair comparisons normally use equal settings. Reproducible seed makes "
        "the deal sequence repeatable.\n\n"
        "Queue Current stores the current matchup. With Randomize teams checked, each "
        "press draws two different profiles and stores that pair in the queued job. "
        "Queue as many tournaments as desired, then choose Run Queue; jobs run in order "
        "and survive closing the Lab or restarting the computer. Sample Planner "
        "estimates the number of paired deals needed to detect a chosen effect with "
        "about 80% statistical power.\n\n"
        "Early stop after deals waits for at least that many pairs, then may stop when "
        "the 95% confidence interval no longer includes zero. Set 0 to disable it. The "
        "ledger saves every deal for later investigation. The watchdog uses runtime "
        "and silent-output limits to stop stuck jobs; CUDA failures are also detected "
        "and recorded. These "
        "settings persist between Lab launches.\n\n"
        "Each computer keeps its queue, settings, result log, and deal ledger under "
        "node_state/<computer-name> so several computers can run from one shared "
        "folder without claiming the same jobs. Compare Latest Benchmarks combines "
        "the per-computer result logs. Headless results do not update GUI tournament "
        "Elo.")),
    ("Journal & Exports", (
        "Decision Journal & Timeline (J) lists session events with elapsed times and "
        "details: deals, bids, discards, plays, advice, hand results, and game results. "
        "Recorded state snapshots include the hand seed and enough game state for "
        "replay analysis.\n\n"
        "Export Session writes a versioned JSON replay file containing the journal and "
        "state snapshots. Open Replay and Confidence Calibration read this format.\n\n"
        "Export Decision Audit writes JSONL: one JSON object per line. It is intended "
        "for scripts, notebooks, or structured-data tools and includes decisions, "
        "alternatives, confidence, seed, state hash, profiles, and search budgets.\n\n"
        "Session Summary shows elapsed time, completed hands and games, event count, "
        "and the AI you consulted most often.")),
    ("Replay & Analyze Position", (
        "Open Replay loads an exported session. Previous and Next move through recorded "
        "events while the viewer shows details, score, trump, current turn, trick, and "
        "the saved hands. This does not alter the live table.\n\n"
        "Replay Deal loads a new live hand from the displayed event's exact seed. This "
        "lets you play the deal again, but choices and random search details can still "
        "change the continuation. Older events without a seed cannot be replayed.\n\n"
        "Analyze Position runs a fresh search on the saved decision. It ranks legal "
        "calls during bidding, dealer discards during pickup, or alternate cards during "
        "play. The result is a new analysis using current checkpoints and search "
        "settings, not a claim about what the old AI internally considered.")),
    ("Confidence Calibration", (
        "Confidence Calibration loads an exported session and checks whether recorded "
        "advice confidence matched completed-hand outcomes. For example, recommendations "
        "near 70% should succeed about 70% of the time when enough comparable samples "
        "have accumulated.\n\n"
        "The table groups samples into confidence ranges. Mean confidence is what the "
        "AI predicted; Observed win rate is what happened; Gap is their difference. "
        "The chart's dashed diagonal is perfect calibration. Point labels show sample "
        "counts, and larger points mean more observations.\n\n"
        "Expected Calibration Error (ECE) is the sample-weighted average gap. Lower is "
        "better and 0 is perfect. Treat tiny sample bins cautiously: calibration "
        "measures whether confidence is honest, not whether every recommendation was "
        "the best possible move.")),
    ("Seeds & Reproducibility", (
        "Every dealt hand has an integer seed shown in the status bar and saved in the "
        "journal. The same seed recreates the same initial cards, dealer, and prepared "
        "scenario. It does not force every later AI search to make identical choices.\n\n"
        "Named Seed Library can Save Current with a name and note, Replay a selected "
        "seed, or Delete an entry. Use it for difficult hands, regression cases, or "
        "teaching examples.\n\n"
        "Fixed-deal GUI tournaments repeat a game sequence from a seed base. Headless "
        "benchmarks go further by playing each deal twice with profiles swapped, then "
        "reporting paired differences and a 95% confidence interval.")),
    ("History & Benchmarks", (
        "Tournament History combines completed GUI tournament records and headless "
        "benchmark summaries. Filter by record type, profile or checkpoint identity, "
        "season, seed, statistical significance, or date. Click a column heading to "
        "sort the visible rows. Export JSON preserves structured records; Export CSV "
        "creates a spreadsheet-friendly view.\n\n"
        "Compare Latest Benchmarks compares the two latest compatible headless records. "
        "It reports whether seeds and model identities match, then shows changes in "
        "paired value, win rate, and other available metrics. A mismatch warning means "
        "the difference may not be an apples-to-apples comparison.\n\n"
        "A paired 95% interval entirely above zero favors the first profile; entirely "
        "below zero favors the second. An interval crossing zero means the run did not "
        "separate them confidently at that sample size.")),
    ("Model & Search Health", (
        "Model Health refreshes automatically and shows the compute device, PyTorch "
        "availability, active searches, worker generation, and whether Arbiter, "
        "Ironclad, Kyle, and Council loaded successfully. On CUDA it also "
        "shows allocated and reserved GPU memory.\n\n"
        "Search Performance summarizes searches recorded during this session. Median "
        "is a typical time, P95 is slower than 95% of samples, and Max is the slowest. "
        "The chart highlights the five slowest P95 categories. No rows appear until "
        "searches have actually run.\n\n"
        "If searches feel slow, lower the relevant setup preset or use Settings "
        "Management -> Apply Hardware Recommendation. Higher search is not free: it "
        "usually trades responsiveness for stronger decisions.")),
    ("Diagnostics & Self-Test", (
        "Export Diagnostic Bundle creates a ZIP with environment information, model "
        "status, search timings, current seed and state, and recent journal events. "
        "Use it when investigating a crash or incorrect behavior. Background search "
        "errors may create the same bundle automatically in bot_euchre_diagnostics.\n\n"
        "Run Pre-Release Self-Test checks writable storage, replay fixtures, checkpoint "
        "compatibility, deterministic deals, rules invariants, hand simulation, and a "
        "small mirrored benchmark. PASS means those checks succeeded on this machine; "
        "a failure row identifies the subsystem to inspect.\n\n"
        "Open Windows lists every managed tool window. Select one to bring it forward, "
        "or choose Close All Tool Windows to clear the workspace without ending the game.")),
    ("Settings & Accessibility", (
        "Settings Management can reset saved table presets, profile favorites, or all "
        "preferences. Reset All Preferences returns setup choices to defaults; it does "
        "not erase checkpoints, career stats, tournament history, or benchmark logs. "
        "Apply Hardware Recommendation sets both neural search presets to Fast, "
        "Balanced, or Deep according to available CPU and acceleration.\n\n"
        "Accessibility offers Large cards and controls, a High-contrast table, and "
        "Reduced animation delays. Apply saves the choices and updates the table.\n\n"
        "Dark Mode is a separate setup-screen theme choice. Accessibility and setup "
        "preferences are stored in bot_euchre_settings.json.")),
    ("Autosave, Recovery & Files", (
        "The live session is written atomically under node_state/<computer-name> to "
        "bot_euchre_autosave.json. If the app "
        "closes during a game, the next launch offers to continue with scores, cards, "
        "auction state, journal, profiles, and an active GUI tournament. Declining the "
        "offer starts fresh.\n\n"
        "Settings, autosave, career stats, named seeds, Lab queue/settings, headless "
        "summaries, and deal ledgers are private to each computer under node_state. "
        "Set BOT_EUCHRE_NODE_ID before launch to override the computer name. The root "
        "bot_euchre_tournament_history.jsonl remains shared so all computers contribute "
        "to one GUI Elo ladder; appends and migrations use a cross-computer lock. A "
        "legacy root autosave or Lab queue is claimed by the first computer that starts, "
        "preventing duplicate recovery or queued work.\n\n"
        "JSONL means one JSON record per line. Versioned files are upgraded automatically "
        "when possible; before migration, the app leaves a timestamped .bak copy in "
        "the backups folder beside the live data files. Writes use temporary files and "
        "replacement where practical to avoid partial saves.")),
    ("Tools Menu Directory", (
        "Recording and review: Decision Journal & Timeline, Export Session, Export "
        "Decision Audit, Open Replay, and Confidence Calibration.\n\n"
        "AI analysis: Compare AI Recommendations and Profile Inspector.\n\n"
        "Competition: Tournament Mode, Human League Season, Headless Tournament Lab, Compare Latest "
        "Benchmarks, Tournament History, and Elo Leaderboard.\n\n"
        "Performance and reproducibility: Search Performance and Named Seed Library.\n\n"
        "Maintenance: Model Health, Export Diagnostic Bundle, Run Pre-Release Self-Test, "
        "Settings Management, and Accessibility.\n\n"
        "Session Summary gives a quick activity count. Help & User Guide opens this "
        "window. Open Windows focuses or closes tool windows. Tool windows are generally "
        "non-modal, so they can remain open while you inspect the table.")),
    ("Keyboard Shortcuts", (
        "A - Ask Arbiter for the current recommendation.\n\n"
        "J - Open Decision Journal & Timeline.\n\n"
        "T - Open Compare AI Recommendations.\n\n"
        "F1 - Open Help & User Guide.\n\n"
        "Escape - Stop Autoplay and return control of Seat 0 to you.\n\n"
        "Shortcuts are active on the main game window. If a text field or another "
        "window has keyboard focus, click the game window before using them.")),
]

def bid_action_details(action):
    """Returns (suit, is_loner) for a call action, or (None, False) for PASS."""
    if action == BID_PASS:
        return None, False
    if BID_CALL_BASE <= action < BID_CALL_BASE + 4:
        return SUITS_T[action - BID_CALL_BASE], False
    if BID_ALONE_BASE <= action < BID_ALONE_BASE + 4:
        return SUITS_T[action - BID_ALONE_BASE], True
    raise ValueError(f"Not a bid action: {action}")

def legal_bid_actions(round_num, up_suit, is_stuck_dealer):
    """Legal bid action ids for one auction decision. Round 1: pass / order up the
    up-card suit (optionally alone). Round 2: pass / call any non-up-card suit
    (optionally alone); the stuck dealer loses the PASS option entirely."""
    actions = [] if is_stuck_dealer else [BID_PASS]
    if round_num == 1:
        s = SUIT_TO_INDEX[up_suit]
        actions.extend([BID_CALL_BASE + s, BID_ALONE_BASE + s])
    else:
        for suit, s in SUIT_TO_INDEX.items():
            if suit == up_suit:
                continue
            actions.extend([BID_CALL_BASE + s, BID_ALONE_BASE + s])
    return actions


def auction_passed_seats(dealer_idx, passed_count):
    return [(dealer_idx + 1 + offset) % 4 for offset in range(passed_count)]

def calculate_hand_score(caller_idx, is_loner, team1_tricks, team2_tricks):
    caller_team = 1 if caller_idx in (0, 2) else 2
    winning_team = 1 if team1_tricks >= 3 else 2
    winning_tricks = team1_tricks if winning_team == 1 else team2_tricks
    if winning_team != caller_team:
        return winning_team, 2
    if winning_tricks == 5:
        return winning_team, 4 if is_loner else 2
    return winning_team, 1

def encode_state_to_tensor(ui_game, player_idx, target_suit=None, dealer_discard=None,
                           bid_round=None, bid_passes=None):
    """bid_round/bid_passes (July 2026 bidding overhaul): when bid_round is 1 or 2 the
    state is encoded as a LIVE AUCTION decision point - no trump committed (trump
    tensor all zeros, a signature no play-phase state ever has), pass history taken
    from bid_passes (iterable of seat indices that have passed so far this auction)
    instead of being reverse-engineered from a caller. The policy head's bid action
    logits (indices 24-32) are trained/read against exactly this encoding."""
    if not HAS_TORCH:
        return None

    in_auction = bid_round is not None
    current_hand = ui_game.hands[player_idx]
    trump = None if in_auction else (target_suit if target_suit else ui_game.trump_suit)
    
    hand_tensor = np.zeros(24, dtype=np.float32)
    for card in current_hand: hand_tensor[CARD_TO_INDEX[f"{card.rank}{card.suit}"]] = 1.0

    trick_tensor = np.zeros(96, dtype=np.float32)
    for p_idx, card in ui_game.trick:
        idx = (p_idx * 24) + CARD_TO_INDEX[f"{card.rank}{card.suit}"]
        trick_tensor[idx] = 1.0

    played_tensor = np.zeros(24, dtype=np.float32)
    for card in ui_game.played_cards: played_tensor[CARD_TO_INDEX[f"{card.rank}{card.suit}"]] = 1.0

    upcard_tensor = np.zeros(24, dtype=np.float32)
    if ui_game.up_card: upcard_tensor[CARD_TO_INDEX[f"{ui_game.up_card.rank}{ui_game.up_card.suit}"]] = 1.0

    trump_tensor = np.zeros(4, dtype=np.float32)
    if trump in SUIT_TO_INDEX: trump_tensor[SUIT_TO_INDEX[trump]] = 1.0

    dealer_tensor = np.zeros(4, dtype=np.float32); dealer_tensor[ui_game.dealer_idx] = 1.0
    caller_tensor = np.zeros(4, dtype=np.float32)
    if ui_game.caller_idx != -1: caller_tensor[ui_game.caller_idx] = 1.0
    seat_tensor = np.zeros(4, dtype=np.float32); seat_tensor[player_idx] = 1.0

    t1_score_tensor = np.zeros(10, dtype=np.float32); t1_score_tensor[min(ui_game.team1_score, 9)] = 1.0
    t2_score_tensor = np.zeros(10, dtype=np.float32); t2_score_tensor[min(ui_game.team2_score, 9)] = 1.0

    void_tensor = np.zeros(16, dtype=np.float32)
    for p_idx in range(4):
        if p_idx in ui_game.voids:
            for s in ui_game.voids[p_idx]:
                if s in SUIT_TO_INDEX: void_tensor[(p_idx * 4) + SUIT_TO_INDEX[s]] = 1.0

    is_r1 = (bid_round == 1) if in_auction else (ui_game.up_card and trump == ui_game.up_card.suit)
    
    # --- BUG FIX: Bidding Hallucination Patch ---
    # If evaluating a bid (target_suit exists but caller is -1), pretend we are the caller.
    if in_auction:
        eff_caller = -1
    else:
        eff_caller = player_idx if (target_suit and ui_game.caller_idx == -1) else ui_game.caller_idx
    caller_team = 1 if eff_caller in [0, 2] else 2
    # --------------------------------------------
    
    # --- BUG FIX: Fixed typo '22' to '2' ---
    my_team = 1 if player_idx in [0, 2] else 2
    
    context_tensor = np.array([
        1.0 if (not in_auction and caller_team == my_team) else 0.0, 
        1.0 if ui_game.is_loner else 0.0, 
        1.0 if is_r1 else 0.0
    ], dtype=np.float32)

    r1_passes = np.zeros(4, dtype=np.float32); r2_passes = np.zeros(4, dtype=np.float32)
    if in_auction:
        # Live auction: pass history is explicit, not derived from a caller.
        if bid_round == 1:
            for s in (bid_passes or []): r1_passes[s] = 1.0
        else:
            r1_passes = np.ones(4, dtype=np.float32)
            for s in (bid_passes or []): r2_passes[s] = 1.0
    else:
        active_seat = (ui_game.dealer_idx + 1) % 4
        if is_r1:
            while active_seat != eff_caller and eff_caller != -1:
                r1_passes[active_seat] = 1.0; active_seat = (active_seat + 1) % 4
        else:
            r1_passes = np.ones(4, dtype=np.float32) 
            while active_seat != eff_caller and eff_caller != -1:
                r2_passes[active_seat] = 1.0; active_seat = (active_seat + 1) % 4

    t1_round_tensor = np.zeros(6, dtype=np.float32); t1_round_tensor[min(ui_game.team1_tricks, 5)] = 1.0
    t2_round_tensor = np.zeros(6, dtype=np.float32); t2_round_tensor[min(ui_game.team2_tricks, 5)] = 1.0

    phase_tensor = np.zeros(5, dtype=np.float32)
    phase_tensor[min(len(ui_game.played_cards) // 4, 4)] = 1.0

    discard_tensor = np.zeros(24, dtype=np.float32)
    discard = dealer_discard or getattr(ui_game, 'dealer_discard', None)
    if discard and player_idx == getattr(ui_game, 'dealer_idx', -1):
        discard_tensor[CARD_TO_INDEX[f"{discard.rank}{discard.suit}"]] = 1.0

    led_suit_tensor = np.zeros(4, dtype=np.float32)
    leader_tensor = np.zeros(4, dtype=np.float32)
    winner_tensor = np.zeros(4, dtype=np.float32)

    if ui_game.trick:
        led_card = ui_game.trick[0][1]
        led_s = trump if (trump and led_card.rank == 'J' and led_card.suit == SAME_COLOR_T[trump]) else led_card.suit
        if led_s in SUIT_TO_INDEX: led_suit_tensor[SUIT_TO_INDEX[led_s]] = 1.0
        
        leader_tensor[ui_game.trick[0][0]] = 1.0
        winner_idx = ui_game.evaluate_trick()
        if winner_idx != -1:
            winner_tensor[winner_idx] = 1.0

    # --- KEY TRUMP CARD TRACKER (right bower / left bower / trump ace) ---
    # For each key card: is it in my hand, already out of play (played, or my own
    # revealed discard), or unseen from my POV? Surfacing this explicitly saves the
    # network from having to learn a trump-suit-conditional lookup into played_tensor.
    key_card_tensor = np.zeros(9, dtype=np.float32)
    if trump:
        key_cards = [('J', trump), ('J', SAME_COLOR_T[trump]), ('A', trump)]
        for k_idx, (k_rank, k_suit) in enumerate(key_cards):
            base = k_idx * 3
            if any(c.rank == k_rank and c.suit == k_suit for c in current_hand):
                key_card_tensor[base] = 1.0  # in my hand
            elif any(c.rank == k_rank and c.suit == k_suit for c in ui_game.played_cards):
                key_card_tensor[base + 1] = 1.0  # already played
            elif discard and discard.rank == k_rank and discard.suit == k_suit and player_idx == getattr(ui_game, 'dealer_idx', -1):
                key_card_tensor[base + 1] = 1.0  # my own known discard, also out of play
            else:
                key_card_tensor[base + 2] = 1.0  # unseen from my perspective

    # --- PARTNER SEAT (always across the table) ---
    partner_tensor = np.zeros(4, dtype=np.float32)
    partner_tensor[(player_idx + 2) % 4] = 1.0

    # --- HAND-SHAPE SUMMARY STATS (helps bidding, still valid mid-trick) ---
    trump_in_hand = sum(1 for c in current_hand if (c.suit == trump) or (c.rank == 'J' and trump and c.suit == SAME_COLOR_T[trump])) if trump else 0
    off_ace_count = sum(1 for c in current_hand if c.rank == 'A' and c.suit != trump) if trump else sum(1 for c in current_hand if c.rank == 'A')
    trump_count_tensor = np.zeros(6, dtype=np.float32); trump_count_tensor[min(trump_in_hand, 5)] = 1.0
    off_ace_count_tensor = np.zeros(4, dtype=np.float32); off_ace_count_tensor[min(off_ace_count, 3)] = 1.0

    feature_vector = np.concatenate([
        hand_tensor, trick_tensor, played_tensor, upcard_tensor, trump_tensor,
        dealer_tensor, caller_tensor, seat_tensor, t1_score_tensor, t2_score_tensor,
        void_tensor, context_tensor, r1_passes, r2_passes, t1_round_tensor, 
        t2_round_tensor, phase_tensor, discard_tensor, led_suit_tensor, 
        leader_tensor, winner_tensor, key_card_tensor, partner_tensor,
        trump_count_tensor, off_ace_count_tensor
    ]).tolist()
    
    return torch.tensor(feature_vector, dtype=torch.float32)

# ==========================================
# 0.5 SHARED AUCTION MACHINERY (July 2026 bidding overhaul)
# ==========================================
# Bidding is now a first-class learned decision: the policy head carries 9 bid
# actions (indices 24-32), self-play runs REAL two-round stick-the-dealer auctions,
# and every consumer (self-play scripts, evaluators, GUI) shares the functions below.
# All neural evaluation is injected via nn_eval_fn(tensor) -> (probs, value) so the
# same code serves local nets, pipe-servers, and multi-model eval servers alike.

class BidStateView:
    """Minimal encode_state_to_tensor-compatible container for auction decision
    states and post-auction value-leaf states. Only the encoded player's hand is
    ever read, so hidden hands may be left empty."""
    __slots__ = ['hands', 'trick', 'played_cards', 'up_card', 'dealer_idx',
                 'trump_suit', 'caller_idx', 'is_loner', 'loner_partner_idx',
                 'voids', 'team1_tricks', 'team2_tricks', 'team1_score',
                 'team2_score', 'dealer_discard']

    def __init__(self, hands, up_card, dealer_idx, t1_score, t2_score,
                 trump_suit=None, caller_idx=-1, is_loner=False,
                 loner_partner_idx=-1, dealer_discard=None):
        self.hands = hands
        self.trick = []
        self.played_cards = []
        self.up_card = up_card
        self.dealer_idx = dealer_idx
        self.trump_suit = trump_suit
        self.caller_idx = caller_idx
        self.is_loner = is_loner
        self.loner_partner_idx = loner_partner_idx
        self.voids = {0: set(), 1: set(), 2: set(), 3: set()}
        self.team1_tricks = 0
        self.team2_tricks = 0
        self.team1_score = t1_score
        self.team2_score = t2_score
        self.dealer_discard = dealer_discard

    def evaluate_trick(self):
        return -1


def encode_bid_state(hand, seat_idx, up_card, dealer_idx, round_num, passed_seats,
                     t1_score, t2_score):
    """Encodes one live auction decision point for the given seat (no trump committed)."""
    hands = [[] for _ in range(4)]
    hands[seat_idx] = list(hand)
    view = BidStateView(hands, up_card, dealer_idx, t1_score, t2_score)
    return encode_state_to_tensor(view, seat_idx, bid_round=round_num, bid_passes=passed_seats)


def _auction_seat_order(dealer_idx):
    return [(dealer_idx + 1 + k) % 4 for k in range(4)]


def _rollout_discard(hand, trump):
    """Cheap heuristic discard used only INSIDE bid-search rollouts (the real game's
    dealer discard uses choose_dealer_discard's determinized playout search): dump
    the weakest non-trump card, or the weakest trump if the hand is all trump."""
    rank_vals = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
    def eff(c):
        return trump if (c.rank == 'J' and c.suit == SAME_COLOR_T[trump]) else c.suit
    def power(c):
        p = rank_vals[c.rank]
        if c.rank == 'J' and c.suit == trump: p += 500
        elif c.rank == 'J' and c.suit == SAME_COLOR_T[trump]: p += 400
        elif eff(c) == trump: p += 100
        return p
    non_trump = [c for c in hand if eff(c) != trump]
    pool = non_trump if non_trump else list(hand)
    return min(pool, key=power)


def _continue_auction_by_policy(hands, up_card, dealer_idx, round_num, passed_seats,
                                t1_score, t2_score, nn_eval_fn):
    """Plays the auction forward with every remaining seat choosing the argmax of its
    masked bid-head policy (used inside bid-search rollouts to model opponents AND
    the decider's own future decisions). Returns (caller, trump, is_loner, called_round)."""
    def _fallback_round2_call_suit(up_suit):
        """Returns a deterministic fallback suit for malformed auction states.
        Prefers legal round-2 call actions, then degrades to any known suit,
        and finally up_suit itself so callers never crash."""
        forced_actions = legal_bid_actions(2, up_suit, True)
        for forced_action in forced_actions:
            suit, _ = bid_action_details(forced_action)
            if suit is not None and suit != up_suit:
                return suit
        for forced_action in forced_actions:
            suit, _ = bid_action_details(forced_action)
            if suit is not None:
                return suit
        for suit in SUITS_T:
            if suit != up_suit:
                return suit
        if SUITS_T:
            return SUITS_T[0]
        return up_suit

    passed = list(passed_seats)
    rnd = round_num
    while True:
        for seat in _auction_seat_order(dealer_idx):
            if seat in passed:
                continue
            is_stuck = (rnd == 2 and seat == dealer_idx)
            actions = legal_bid_actions(rnd, up_card.suit, is_stuck)
            if not actions:
                # Fail safe for malformed rollout states: non-stuck seats pass,
                # stuck dealer force-calls a legal next suit to keep search alive.
                if is_stuck:
                    return seat, _fallback_round2_call_suit(up_card.suit), False, 2
                passed.append(seat)
                continue
            tensor = encode_bid_state(hands[seat], seat, up_card, dealer_idx, rnd,
                                      passed, t1_score, t2_score)
            probs, _ = nn_eval_fn(tensor)
            action = max(actions, key=lambda a: probs[a])
            if action != BID_PASS:
                suit, alone = bid_action_details(action)
                return seat, suit, alone, rnd
            passed.append(seat)
        if rnd == 1:
            rnd = 2
            passed = []
        else:
            # Unreachable with stick-the-dealer; fail safe rather than loop forever.
            return dealer_idx, _fallback_round2_call_suit(up_card.suit), False, 2


def _bid_rollout(action, my_hand, unknown_base, up_card, dealer_idx, decider_idx,
                 round_num, passed_seats, t1_score, t2_score, nn_eval_fn,
                 known_hands=None):
    """One determinized rollout of a candidate bid action: sample the hidden hands,
    resolve the rest of the auction with policy-argmax bidders, apply the contract
    (round-1 pickup + heuristic rollout discard), then score the resulting pre-play
    state with ONE value-head call from the decider's perspective (AlphaZero-style
    truncated rollout - no 5-trick playout needed)."""
    unknown = unknown_base[:]
    random.shuffle(unknown)
    hands = [None] * 4
    ptr = 0
    for i in range(4):
        if i == decider_idx:
            hands[i] = list(my_hand)
        elif known_hands and i in known_hands:
            hands[i] = list(known_hands[i])
        else:
            hands[i] = unknown[ptr:ptr + 5]
            ptr += 5

    if action == BID_PASS:
        caller, trump, alone, called_round = _continue_auction_by_policy(
            hands, up_card, dealer_idx, round_num, list(passed_seats) + [decider_idx],
            t1_score, t2_score, nn_eval_fn)
    else:
        trump, alone = bid_action_details(action)
        caller, called_round = decider_idx, round_num

    loner_partner = (caller + 2) % 4 if alone else -1
    dealer_discard = None
    if called_round == 1:
        dealer_hand = hands[dealer_idx]
        dealer_hand.append(Card(up_card.rank, up_card.suit))
        discard = _rollout_discard(dealer_hand, trump)
        dealer_hand.remove(discard)
        if dealer_idx == decider_idx:
            dealer_discard = discard

    view = BidStateView(hands, up_card, dealer_idx, t1_score, t2_score,
                        trump_suit=trump, caller_idx=caller, is_loner=alone,
                        loner_partner_idx=loner_partner, dealer_discard=dealer_discard)
    tensor = encode_state_to_tensor(view, decider_idx, dealer_discard=dealer_discard)
    _, v = nn_eval_fn(tensor)
    return float(v)


def run_bid_mcts(my_hand, up_card, dealer_idx, decider_idx, round_num, passed_seats,
                 t1_score, t2_score, nn_eval_fn, rollouts=80, c_puct=1.5,
                 add_noise=False, known_hands=None, call_margin=0.0,
                 loner_margin=0.0):
    """Root-level PUCT search over one auction decision (the AlphaZero improvement
    operator for bidding). Each rollout determinizes hidden hands, resolves the
    auction continuation with policy-argmax bidders, and evaluates the resulting
    contract with the value head. Returns ({bid_action: visit_fraction}, root_q)
    where root_q is the visit-weighted value from the decider's team perspective."""
    is_stuck = (round_num == 2 and decider_idx == dealer_idx)
    actions = legal_bid_actions(round_num, up_card.suit, is_stuck)

    root_tensor = encode_bid_state(my_hand, decider_idx, up_card, dealer_idx,
                                   round_num, passed_seats, t1_score, t2_score)
    probs, _ = nn_eval_fn(root_tensor)
    prior_sum = sum(float(probs[a]) for a in actions)
    priors = {a: (float(probs[a]) / prior_sum if prior_sum > 0 else 1.0 / len(actions))
              for a in actions}
    if add_noise:
        noise = np.random.dirichlet([1.0] * len(actions))
        for i, a in enumerate(actions):
            priors[a] = 0.75 * priors[a] + 0.25 * float(noise[i])

    known = {(c.rank, c.suit) for c in my_hand} | {(up_card.rank, up_card.suit)}
    if known_hands:
        known.update((c.rank, c.suit) for hand in known_hands.values() for c in hand)
    unknown_base = [Card(r, s) for s in SUITS_T for r in RANKS_T if (r, s) not in known]

    visits = {a: 0 for a in actions}
    wins = {a: 0.0 for a in actions}
    for _ in range(rollouts):
        total = sum(visits.values())
        sqrt_total = math.sqrt(total + 1)
        best_action, best_score = actions[0], -float('inf')
        for a in actions:
            q = (wins[a] / visits[a]) if visits[a] > 0 else 0.0
            u = c_puct * priors[a] * sqrt_total / (1 + visits[a])
            style_margin = 0.0
            if a != BID_PASS:
                _, alone = bid_action_details(a)
                if not is_stuck:
                    style_margin += call_margin
                if alone:
                    style_margin += loner_margin
            score = q + u - style_margin
            if score > best_score:
                best_score, best_action = score, a
        v = _bid_rollout(best_action, my_hand, unknown_base, up_card, dealer_idx,
                         decider_idx, round_num, passed_seats, t1_score, t2_score,
                         nn_eval_fn, known_hands=known_hands)
        visits[best_action] += 1
        wins[best_action] += v

    total = sum(visits.values())
    if total == 0:
        return {a: 1.0 / len(actions) for a in actions}, 0.0
    root_q = sum(wins.values()) / total
    return {a: visits[a] / total for a in actions}, root_q


def run_auction(hands, up_card, dealer_idx, decide_bid_fn):
    """Drives a full two-round, stick-the-dealer auction over REAL hands.
    decide_bid_fn(seat, round_num, passed_seats, legal_actions) -> bid action id.
    Returns (caller_idx, trump_suit, is_loner, called_round)."""
    for rnd in (1, 2):
        passed = []
        for seat in _auction_seat_order(dealer_idx):
            is_stuck = (rnd == 2 and seat == dealer_idx)
            actions = legal_bid_actions(rnd, up_card.suit, is_stuck)
            action = decide_bid_fn(seat, rnd, list(passed), actions)
            if action != BID_PASS:
                suit, alone = bid_action_details(action)
                return seat, suit, alone, rnd
            passed.append(seat)
    raise RuntimeError("Auction produced no caller - stick-the-dealer should make this impossible")


class _DiscardPlayoutView:
    """encode_state_to_tensor-compatible snapshot of a live SimState playout
    (mid-trick legal, unlike BidStateView). Built fresh per NN evaluation because
    SimState.apply_move reassigns .trick at trick boundaries."""
    __slots__ = ['hands', 'trick', 'played_cards', 'up_card', 'dealer_idx',
                 'trump_suit', 'caller_idx', 'is_loner', 'loner_partner_idx',
                 'voids', 'team1_tricks', 'team2_tricks', 'team1_score',
                 'team2_score', 'dealer_discard', '_sim']

    def __init__(self, sim, played_cards, up_card, dealer_idx, t1_score, t2_score,
                 dealer_discard):
        self.hands = sim.hands
        self.trick = sim.trick
        self.trump_suit = sim.trump_suit
        self.caller_idx = sim.caller_idx
        self.is_loner = sim.is_loner
        self.loner_partner_idx = sim.loner_partner_idx
        self.voids = sim.voids
        self.team1_tricks = sim.team1_tricks
        self.team2_tricks = sim.team2_tricks
        self.played_cards = played_cards
        self.up_card = up_card
        self.dealer_idx = dealer_idx
        self.team1_score = t1_score
        self.team2_score = t2_score
        self.dealer_discard = dealer_discard
        self._sim = sim

    def get_effective_suit(self, card):
        return self._sim.get_effective_suit(card)

    def evaluate_trick(self):
        return self._sim.evaluate_trick()


def _discard_playout_value(hands, trump_suit, caller_idx, is_loner, loner_partner_idx,
                           up_card, dealer_idx, t1_score, t2_score, nn_eval_fn,
                           dealer_discard, perspective_team):
    """Plays one fully-determinized deal to completion with every seat choosing the
    argmax of its masked policy head (one NN call per non-forced move), then returns
    the ACTUAL caller_pts/4 outcome from perspective_team's side. This is the
    counterfactual measurement engine behind the discard search: a clean void
    materializes as real over-trumped tricks in the playout, instead of having to
    pre-exist as knowledge inside the value head."""
    sim = SimState(trump_suit, [], hands, (dealer_idx + 1) % 4, is_loner,
                   loner_partner_idx, caller_idx)
    played_cards = []
    while (sim.team1_tricks + sim.team2_tricks) < 5:
        if sim.current_turn == sim.loner_partner_idx:
            sim.current_turn = (sim.current_turn + 1) % 4
            continue
        legal_moves = sim.get_legal_moves()
        if not legal_moves:
            # Fail safe for rare malformed determinization states.
            fallback = list(sim.hands[sim.current_turn])
            if not fallback:
                break
            legal_moves = fallback
        if len(legal_moves) == 1:
            move = legal_moves[0]
        else:
            view = _DiscardPlayoutView(sim, played_cards, up_card, dealer_idx,
                                       t1_score, t2_score, dealer_discard)
            probs, _ = nn_eval_fn(encode_state_to_tensor(view, sim.current_turn))
            move = max(legal_moves, key=lambda m: probs[CARD_TO_INDEX[f"{m.rank}{m.suit}"]])
        if sim.trick:
            led_suit = sim.get_effective_suit(sim.trick[0][1])
            if sim.get_effective_suit(move) != led_suit:
                sim.voids[sim.current_turn].add(led_suit)
        trick_ending = len(sim.trick) == (2 if sim.is_loner else 3)
        cards_in_trick = [c for _, c in sim.trick] + [move]
        sim.apply_move(move)
        if trick_ending:
            played_cards.extend(cards_in_trick)
    caller_team = 1 if caller_idx in (0, 2) else 2
    caller_tricks = sim.team1_tricks if caller_team == 1 else sim.team2_tricks
    if caller_tricks >= 5:
        caller_pts = 4 if is_loner else 2  # march (alone = 4 pts)
    elif caller_tricks >= 3:
        caller_pts = 1                     # made the call
    else:
        caller_pts = -2                    # euchred
    caller_v = caller_pts / 4.0
    return caller_v if perspective_team == caller_team else -caller_v


def choose_dealer_discard(hand_after_pickup, trump_suit, caller_idx, is_loner,
                          up_card, dealer_idx, t1_score, t2_score, nn_eval_fn,
                          determinizations=24, known_hands=None,
                          discard_candidates=None, choose_worst=False,
                          return_ranked=False):
    """Paired determinized-playout discard search (July 2026 void-blindness fix).

    The old value-head argmax was structurally void-blind: the ~0.1-pt EV margin
    between e.g. discarding into a clean void vs. keeping a doubleton sits below
    the value head's own label-noise floor (paired_diff_std ~0.3), and self-play
    never generated the counterfactual data to fix it. Instead of asking the
    network to KNOW void value, this measures it directly: for each of
    `determinizations` samples of the 3 hidden hands (3-card kitty excluded, as in
    reality), every candidate discard is played out to completion on the IDENTICAL
    sampled hands with policy-argmax players, and the candidate with the best mean
    actual outcome from the dealer's TEAM perspective wins (the dealer may be
    defending an opponent's order-up). The paired design cancels hand-strength
    variance exactly like the paired evaluators do.

    A tiny value-head term (weight 1e-3 - provably below the smallest possible
    playout margin of 0.25/determinizations for any K <= 125) breaks exact playout
    ties using the old ranking. If the dealer's hand is dead (partner going
    alone), playouts are skipped and the value-head argmax decides alone.

    Same signature as the old value-head version (plus optional
    `determinizations`), so all call sites - self-play, both evaluators, GUI,
    smoke test - work unchanged."""
    loner_partner = (caller_idx + 2) % 4 if is_loner else -1
    candidates = list(discard_candidates) if discard_candidates is not None else list(hand_after_pickup)

    def value_head_score(cand):
        remaining = [c for c in hand_after_pickup if c is not cand]
        hands = [[] for _ in range(4)]
        hands[dealer_idx] = remaining
        view = BidStateView(hands, up_card, dealer_idx, t1_score, t2_score,
                            trump_suit=trump_suit, caller_idx=caller_idx,
                            is_loner=is_loner, loner_partner_idx=loner_partner,
                            dealer_discard=cand)
        tensor = encode_state_to_tensor(view, dealer_idx, dealer_discard=cand)
        _, v = nn_eval_fn(tensor)
        return float(v)

    if is_loner and loner_partner == dealer_idx:
        # Dealer's hand never plays - every discard is outcome-equivalent.
        scored = [(candidate, value_head_score(candidate)) for candidate in candidates]
        scored.sort(key=lambda item: item[1], reverse=not choose_worst)
        return scored if return_ranked else scored[0][0]

    dealer_team = 1 if dealer_idx in (0, 2) else 2
    known_hands = known_hands or {}
    known = {(c.rank, c.suit) for c in hand_after_pickup}
    known.update((c.rank, c.suit) for hand in known_hands.values() for c in hand)
    unknown_base = [Card(r, s) for s in SUITS_T for r in RANKS_T if (r, s) not in known]

    totals = [0.0] * len(candidates)
    for _ in range(determinizations):
        unknown = unknown_base[:]
        random.shuffle(unknown)
        sampled = {}
        ptr = 0
        for seat in range(4):
            if seat == dealer_idx:
                continue
            if seat in known_hands:
                sampled[seat] = list(known_hands[seat])
            else:
                sampled[seat] = unknown[ptr:ptr + 5]
                ptr += 5
        # The 3 leftover cards are the kitty: unseen and out of play, as in reality.
        for i, cand in enumerate(candidates):
            hands = [None] * 4
            for seat in range(4):
                hands[seat] = ([c for c in hand_after_pickup if c is not cand]
                               if seat == dealer_idx else list(sampled[seat]))
            totals[i] += _discard_playout_value(
                hands, trump_suit, caller_idx, is_loner, loner_partner,
                up_card, dealer_idx, t1_score, t2_score, nn_eval_fn,
                cand, dealer_team)

    scored = [
        (candidate, totals[index] / determinizations
         + 1e-3 * value_head_score(candidate))
        for index, candidate in enumerate(candidates)]
    scored.sort(key=lambda item: item[1], reverse=not choose_worst)
    return scored if return_ranked else scored[0][0]

# ==========================================
# 1. UTILITY, TOOLTIPS, & DATA MODELS
# ==========================================
class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        self.id = None
        self.x = self.y = 0
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)

    def enter(self, event=None):
        self.schedule()

    def leave(self, event=None):
        self.unschedule()
        self.hidetip()

    def schedule(self):
        self.unschedule()
        self.id = self.widget.after(400, self.showtip)

    def unschedule(self):
        id = self.id
        self.id = None
        if id: self.widget.after_cancel(id)

    def showtip(self, event=None):
        x = y = 0
        x, y, cx, cy = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 20
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry("+%d+%d" % (x, y))
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                      background="#365c45", foreground="white", relief=tk.SOLID, borderwidth=1,
                      font=("Arial", 10, "italic"), wraplength=250, padx=5, pady=5)
        label.pack(ipadx=1)

    def hidetip(self):
        tw = self.tipwindow
        self.tipwindow = None
        if tw: tw.destroy()

class StatsTracker:
    def __init__(self, filename=PLAYER_STATS_PATH):
        self.filename = filename
        self.stats = self._load_stats()

    def _load_stats(self):
        default_stats = {
            "games_completed": 0, "games_won": 0, "hands_played": 0,
            "trump_calls": 0, "got_euchred": 0, "took_all_5": 0,
            "went_alone": 0, "took_5_alone": 0, "total_points_earned": 0,
            "total_passes": 0, "missed_calls": 0, "missed_next_calls": 0,
            "synergy_blunders": 0, "loner_defense_blunders": 0, "play_blunders": 0,
            "catastrophic_loner_leaks": 0, "failed_trump_pulls": 0, 
            "missed_void_discards": 0, "phantom_boss_plays": 0,
            "defensive_trump_leads": 0, "suboptimal_defensive_leads": 0,
            "greedy_loners": 0, "stranded_aces": 0, "trapped_left_bowers": 0
        }
        
        for key in list(default_stats.keys()):
            default_stats[f"active_{key}"] = 0.0

        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    loaded = json.load(f)
                    default_stats.update(loaded)
            except Exception: pass
        return default_stats

    def save(self):
        try:
            with open(self.filename, 'w') as f:
                json.dump(self.stats, f, indent=4)
        except Exception: pass

    def record_event(self, event_key, amount=1):
        if event_key in self.stats:
            self.stats[event_key] += amount
            
            active_key = f"active_{event_key}"
            if active_key in self.stats:
                self.stats[active_key] += float(amount)
            else:
                self.stats[active_key] = float(amount)
                
            self.save()

    def apply_decay(self, decay_rate=0.85):
        for key in list(self.stats.keys()):
            if key.startswith("active_"):
                self.stats[key] = round(self.stats[key] * decay_rate, 3)
                if self.stats[key] < 0.1:
                    self.stats[key] = 0.0
        self.save()

    def clear_stats(self):
        self.stats = self._load_stats()
        for k in self.stats:
            self.stats[k] = 0.0 if k.startswith("active_") else 0
        self.save()

class SettingsStore:
    DEFAULTS = {
        "player_name": "You", "trainer_mode": False, "dark_mode": True,
        "hint_search": "Balanced", "table_search": "Balanced",
        "engine_speed": "Grandmaster (100,000 Iterations)",
        "drill": "Standard Match",
        "players": ["Arbiter (Vanilla Neural)"] * 3,
        "favorites": ["Arbiter", "Ironclad", "Kyle", "Unanimous Council"],
        "large_cards": False, "high_contrast": False,
        "reduced_motion": False, "presets": {},
        "elo_season_id": "legacy", "elo_season_name": "Legacy",
    }

    def __init__(self, filename=SETTINGS_PATH):
        self.filename = filename
        try:
            loaded = load_versioned_mapping(
                filename, "bot-euchre-settings", self.DEFAULTS)
            self.data = sanitize_profile_preferences(loaded)
        except (OSError, ValueError, TypeError):
            self.data = copy.deepcopy(self.DEFAULTS)
            self.data.update({
                "_schema": "bot-euchre-settings",
                "_schema_version": DATA_SCHEMA_VERSION})

    def save(self):
        atomic_write_json(self.filename, self.data)

    def save_preset(self, name, settings):
        self.data.setdefault("presets", {})[name] = copy.deepcopy(settings)
        self.save()

    def reset(self, section="all"):
        if section == "presets":
            self.data["presets"] = {}
        elif section == "favorites":
            self.data["favorites"] = copy.deepcopy(self.DEFAULTS["favorites"])
        else:
            self.data = copy.deepcopy(self.DEFAULTS)
        self.save()

class SessionJournal:
    def __init__(self):
        self.started_at = time.time()
        self.events = []
        self.hands_completed = 0
        self.games_completed = 0
        self.ai_consultations = {}

    def record(self, event_type, details=None, state=None):
        self.events.append({
            "time": round(time.time() - self.started_at, 3),
            "type": event_type,
            "details": details or {},
            "state": state,
        })

    def export(self, filename, metadata=None):
        payload = {
            "format": "bot-euchre-session-v2",
            "_schema": "bot-euchre-session",
            "_schema_version": DATA_SCHEMA_VERSION,
            "metadata": metadata or {},
            "started_at": self.started_at,
            "hands_completed": self.hands_completed,
            "games_completed": self.games_completed,
            "ai_consultations": self.ai_consultations,
            "events": self.events,
        }
        atomic_write_json(filename, payload)

    def export_decision_audit(self, filename, metadata=None):
        decision_types = {"bid", "discard", "play", "ai_consultation"}
        with open(filename, "w", encoding="utf-8") as output_file:
            for event in self.events:
                if event.get("type") not in decision_types:
                    continue
                row = {
                    "format": "bot-euchre-decision-v1",
                    "session_started_at": self.started_at,
                    "time": event.get("time", 0),
                    "type": event.get("type"),
                    "details": event.get("details", {}),
                    "state_hash": canonical_state_hash(event.get("state")),
                    "hand_seed": (event.get("state") or {}).get(
                        "hand_seed", (metadata or {}).get("hand_seed")),
                    "metadata": metadata or {},
                }
                output_file.write(json.dumps(row, ensure_ascii=False) + "\n")

class SoundFX:
    @staticmethod
    def play_card():
        if IS_WINDOWS: threading.Thread(target=winsound.Beep, args=(800, 50), daemon=True).start()
    @staticmethod
    def trick_won():
        if IS_WINDOWS: threading.Thread(target=winsound.Beep, args=(500, 100), daemon=True).start()
    @staticmethod
    def round_win(points):
        if not IS_WINDOWS: return
        def sound_routine():
            if points == 1: winsound.Beep(400, 100); winsound.Beep(600, 250) 
            elif points == 2: winsound.Beep(400, 100); winsound.Beep(600, 100); winsound.Beep(800, 300) 
            elif points >= 4: winsound.Beep(400, 100); winsound.Beep(500, 100); winsound.Beep(600, 100); winsound.Beep(800, 400) 
        threading.Thread(target=sound_routine, daemon=True).start()
    @staticmethod
    def round_lose():
        if not IS_WINDOWS: return
        def sound_routine(): winsound.Beep(300, 300); winsound.Beep(200, 500) 
        threading.Thread(target=sound_routine, daemon=True).start()

PLAYER_NAMES = {0: "You", 1: "Wildcard", 2: "The MC", 3: "Kyle"}

class Card:
    __slots__ = ['rank', 'suit', 'color']
    def __init__(self, rank, suit):
        self.rank = rank; self.suit = suit
        self.color = "red" if suit in ['♥', '♦'] else "black"
    def __str__(self): return f"{self.rank}{self.suit}"
    def __eq__(self, other): return self.rank == other.rank and self.suit == other.suit
    def __hash__(self): return hash((self.rank, self.suit))

def effective_suit(card, trump_suit):
    if (trump_suit and card.rank == "J"
            and card.suit == SAME_COLOR_T[trump_suit]):
        return trump_suit
    return card.suit

def trick_card_power(card, trump_suit, led_suit):
    rank_value = {"9": 9, "10": 10, "J": 11, "Q": 12, "K": 13, "A": 14}[card.rank]
    if trump_suit and card.rank == "J" and card.suit == trump_suit:
        return rank_value + 500
    if (trump_suit and card.rank == "J"
            and card.suit == SAME_COLOR_T[trump_suit]):
        return rank_value + 400
    card_suit = effective_suit(card, trump_suit)
    if card_suit == trump_suit:
        return rank_value + 100
    if card_suit == led_suit:
        return rank_value + 50
    return 0

def trick_winner(trick, trump_suit):
    if not trick:
        return -1
    led_suit = effective_suit(trick[0][1], trump_suit)
    return max(
        trick,
        key=lambda play: trick_card_power(play[1], trump_suit, led_suit))[0]

def active_turn_seat(current_turn, is_loner, loner_partner_idx):
    if is_loner and current_turn == loner_partner_idx:
        return (current_turn + 1) % 4
    return current_turn

def build_seeded_deck(seed):
    deck = [Card(rank, suit) for suit in SUITS_T for rank in RANKS_T]
    random.Random(int(seed)).shuffle(deck)
    return deck

def select_hand_seed(seed_override=None, tournament_state=None,
                     current_seed=None, reuse_current=False):
    if seed_override is not None:
        return int(seed_override)
    if reuse_current and current_seed is not None:
        return int(current_seed)
    if tournament_state and tournament_state.get("league_mode"):
        hand_index = len(tournament_state.get("hand_seeds", []))
        mirror_seeds = tournament_state.get("mirror_seeds", [])
        if (tournament_state.get("mirror_phase") == 1
                and hand_index < len(mirror_seeds)):
            return int(mirror_seeds[hand_index])
        return int(tournament_state.get("seed_base", 0)) + hand_index
    if tournament_state and tournament_state.get("benchmark"):
        return (
            int(tournament_state.get("seed_base", 0))
            + int(tournament_state.get("hands", 0)))
    return random.SystemRandom().randrange(0, 2 ** 63)

# ==========================================
# 2. MATHEMATICAL STATE MODELS 
# ==========================================
class EuchreGameDummy:
    __slots__ = ['trump_suit', 'trick', 'hands', 'current_turn', 'is_loner', 'loner_partner_idx', 'caller_idx', 'voids', 'played_cards', 'up_card']
    def __init__(self, real_game):
        self.trump_suit = real_game.trump_suit
        self.trick = list(real_game.trick)
        self.hands = [list(h) for h in real_game.hands]
        self.current_turn = real_game.current_turn
        self.is_loner = real_game.is_loner
        self.loner_partner_idx = real_game.loner_partner_idx
        self.caller_idx = real_game.caller_idx
        self.voids = {k: set(v) for k, v in real_game.voids.items()}
        self.played_cards = list(real_game.played_cards)
        self.up_card = real_game.up_card
    
    def get_effective_suit(self, card):
        return effective_suit(card, self.trump_suit)

class SimState:
    __slots__ = ['trump_suit', 'trick', 'hands', 'current_turn', 'is_loner', 'loner_partner_idx', 'caller_idx', 'team1_tricks', 'team2_tricks', 'voids']
    def __init__(self, trump_suit, trick, hands, current_turn, is_loner, loner_partner_idx, caller_idx, voids=None, team1_tricks=0, team2_tricks=0):
        self.trump_suit = trump_suit; self.trick = list(trick); self.hands = [list(h) for h in hands]; self.current_turn = current_turn
        self.is_loner = is_loner; self.loner_partner_idx = loner_partner_idx; self.caller_idx = caller_idx
        self.team1_tricks = team1_tricks; self.team2_tricks = team2_tricks
        self.voids = {k: set(v) for k, v in voids.items()} if voids else {0: set(), 1: set(), 2: set(), 3: set()}

    def get_effective_suit(self, card):
        return effective_suit(card, self.trump_suit)

    def get_legal_moves(self):
        hand = self.hands[self.current_turn]
        if not self.trick or not self.trump_suit: return list(hand)
        led_suit = self.get_effective_suit(self.trick[0][1])
        legal_cards = [card for card in hand if self.get_effective_suit(card) == led_suit]
        if not legal_cards: return list(hand)
        return legal_cards

    def get_heuristic_move(self):
        legal_cards = self.get_legal_moves()
        if len(legal_cards) == 1: return legal_cards[0]
        
        if not self.trick:
            caller_team = 1 if self.caller_idx in [0, 2] else 2
            my_team = 1 if self.current_turn in [0, 2] else 2
            if caller_team == my_team:
                trump_moves = [c for c in legal_cards if self.get_effective_suit(c) == self.trump_suit]
                if trump_moves: return random.choice(trump_moves)
            return random.choice(legal_cards)
            
        led_suit = self.get_effective_suit(self.trick[0][1]); highest_power = -1; winning_p_idx = -1
        rank_base_vals = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
        
        def get_power(c):
            pwr = rank_base_vals[c.rank]; eff_s = self.get_effective_suit(c)
            if c.rank == 'J' and c.suit == self.trump_suit: pwr += 500
            elif c.rank == 'J' and c.suit == SAME_COLOR_T[self.trump_suit]: pwr += 400
            elif eff_s == self.trump_suit: pwr += 100
            elif eff_s == led_suit: pwr += 50
            else: pwr = 0
            return pwr

        for p_idx, c in self.trick:
            pwr = get_power(c)
            if pwr > highest_power: highest_power = pwr; winning_p_idx = p_idx
            
        winning_moves = [c for c in legal_cards if get_power(c) > highest_power]
        partner_idx = (self.current_turn + 2) % 4
        trick_target = 2 if self.is_loner else 3
        
        if winning_p_idx == partner_idx and len(self.trick) == trick_target:
            return min(legal_cards, key=lambda c: (get_power(c), rank_base_vals[c.rank]))
        if winning_moves: return min(winning_moves, key=lambda c: (get_power(c), rank_base_vals[c.rank]))
        else: return min(legal_cards, key=lambda c: (get_power(c), rank_base_vals[c.rank]))

    def apply_move(self, card):
        self.hands[self.current_turn].remove(card)
        self.trick.append((self.current_turn, card))
        self.current_turn = (self.current_turn + 1) % 4
        if self.is_loner and self.current_turn == self.loner_partner_idx: self.current_turn = (self.current_turn + 1) % 4

        if len(self.trick) == (3 if self.is_loner else 4):
            winner_idx = self.evaluate_trick()
            if winner_idx in [0, 2]: self.team1_tricks += 1
            else: self.team2_tricks += 1
            self.trick = []; self.current_turn = winner_idx
            if self.is_loner and self.current_turn == self.loner_partner_idx: self.current_turn = (self.current_turn + 1) % 4

    def evaluate_trick(self):
        return trick_winner(self.trick, self.trump_suit)

    def get_result(self, observer_idx):
        return self.team1_tricks if observer_idx in [0, 2] else self.team2_tricks

def get_tactical_search_moves(sim_state):
    """Remove a dominated high-card play when partner has already trumped the
    trick, this player is last, and must follow the original non-trump suit."""
    legal_moves = sim_state.get_legal_moves()
    trick_target = 2 if sim_state.is_loner else 3
    if len(legal_moves) < 2 or len(sim_state.trick) != trick_target:
        return legal_moves

    partner_idx = (sim_state.current_turn + 2) % 4
    if sim_state.evaluate_trick() != partner_idx:
        return legal_moves

    led_suit = sim_state.get_effective_suit(sim_state.trick[0][1])
    partner_card = next(card for player_idx, card in sim_state.trick if player_idx == partner_idx)
    if led_suit == sim_state.trump_suit or sim_state.get_effective_suit(partner_card) != sim_state.trump_suit:
        return legal_moves
    if any(sim_state.get_effective_suit(card) != led_suit for card in legal_moves):
        return legal_moves

    rank_value = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
    return [min(legal_moves, key=lambda card: rank_value[card.rank])]

class Node:
    __slots__ = ['move', 'parent', 'children', 'wins', 'visits']
    def __init__(self, move=None, parent=None):
        self.move = move; self.parent = parent; self.children = []; self.wins = 0; self.visits = 0
    def ucb1(self, exploration_param=1.41):
        if self.visits == 0: return float('inf')
        return (self.wins / self.visits) + exploration_param * math.sqrt(math.log(self.parent.visits) / self.visits)

# ==========================================
# 2.5 ALPHAZERO INFORMATION-SET SEARCH ENGINE
# ==========================================
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

    def puct(self, c_puct=1.5):
        q = (self.wins / self.visits) if self.visits > 0 else 0.0
        parent_visits = self.parent.visits if self.parent else 0
        u = c_puct * self.prior * math.sqrt(parent_visits) / (1 + self.visits)
        return q + u

def run_alphazero_mcts(sim_state, neural_net, iterations, original_cards, up_card, dealer_idx, t1_score, t2_score, device="cpu", dealer_discard=None, nn_cache=None, known_hands=None):
    root_player = sim_state.current_turn
    root = AlphaNode(player_idx=root_player)
    # Optional cross-call cache of NN evaluations (callers may pass a persistent dict);
    # falls back to a per-call cache so repeated leaves within one search are still free.
    if nn_cache is None: nn_cache = {}
    
    active_set = {(c.rank, c.suit) for h in sim_state.hands for c in h} | {(c.rank, c.suit) for _, c in sim_state.trick}
    played_cards = [c for c in original_cards if (c.rank, c.suit) not in active_set]
    
    known_hands = known_hands or {}
    known_cards = list(sim_state.hands[root_player]) + played_cards + [c for _, c in sim_state.trick]
    known_cards.extend(c for hand in known_hands.values() for c in hand)
    if up_card: known_cards.append(up_card)
    
    deck = [Card(r, s) for s in SUITS_T for r in RANKS_T]
    known_set = {(kc.rank, kc.suit) for kc in known_cards}
    unknown_cards_base = [c for c in deck if (c.rank, c.suit) not in known_set]

    for _ in range(iterations):
        unknown_cards = unknown_cards_base[:]
        random.shuffle(unknown_cards)
        
        sim_copy = SimState(
            sim_state.trump_suit, list(sim_state.trick), 
            [list(h) if i == root_player else list(known_hands.get(i, [])) for i, h in enumerate(sim_state.hands)], 
            sim_state.current_turn, sim_state.is_loner, sim_state.loner_partner_idx, 
            sim_state.caller_idx, sim_state.voids, sim_state.team1_tricks, sim_state.team2_tricks
        )
        
        for i in range(4):
            if i != root_player:
                if i in known_hands:
                    continue
                expected_size = 5 - (sim_copy.team1_tricks + sim_copy.team2_tricks)
                if any(p == i for p, c in sim_copy.trick): expected_size -= 1
                
                dealt = 0
                
                # R1 trump: the dealer is KNOWN to hold the up-card (unless it has
                # already hit the table) - place it instead of a random unknown.
                if up_card and sim_copy.trump_suit == up_card.suit and i == dealer_idx:
                    up_played = any(c.rank == up_card.rank and c.suit == up_card.suit for c in played_cards)
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
                # Relax void constraints if absolutely necessary to prevent short hands
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
            
            # Track voids revealed inside this simulated trajectory
            if sim_copy.trick:
                led_suit = sim_copy.get_effective_suit(sim_copy.trick[0][1])
                if sim_copy.get_effective_suit(node.move) != led_suit:
                    sim_copy.voids[sim_copy.current_turn].add(led_suit)
            
            sim_copy.apply_move(node.move)
            node.player_idx = sim_copy.current_turn  # who wins a trick differs per determinization
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
            sim_active_set = {(c.rank, c.suit) for h in sim_copy.hands for c in h} | {(c.rank, c.suit) for _, c in sim_copy.trick}
            sim_played = [c for c in original_cards if (c.rank, c.suit) not in sim_active_set]
            
            class TensorStateWrapper:
                def __init__(self, sc, pc, uc, d_idx, t1_s, t2_s, dealer_discard=None):
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
                    self.played_cards = pc
                    self.up_card = uc
                    self.dealer_idx = d_idx
                    self.team1_score = t1_s
                    self.team2_score = t2_s
                    self.dealer_discard = dealer_discard
                    
                def get_effective_suit(self, card):
                    if self.trump_suit and card.rank == 'J' and card.suit == SAME_COLOR_T[self.trump_suit]: return self.trump_suit
                    return card.suit
                    
                def is_trump(self, card):
                    if not self.trump_suit: return False
                    return self.get_effective_suit(card) == self.trump_suit

                def evaluate_trick(self):
                    if not self.trick: return -1
                    led_suit = self.get_effective_suit(self.trick[0][1])
                    highest_power = -1
                    winner_idx = -1
                    rank_base_vals = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
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

            state_key = (
                leaf_player,
                frozenset((c.rank, c.suit) for c in sim_copy.hands[leaf_player]),
                tuple((p, c.rank, c.suit) for p, c in sim_copy.trick),
                frozenset((c.rank, c.suit) for c in sim_played),
                sim_copy.trump_suit, sim_copy.caller_idx, sim_copy.is_loner, sim_copy.loner_partner_idx,
                sim_copy.team1_tricks, sim_copy.team2_tricks,
                frozenset((k, frozenset(vs)) for k, vs in sim_copy.voids.items()),
                t1_score, t2_score
            )
            
            if state_key in nn_cache:
                probs, v = nn_cache[state_key]
            else:
                wrapper = TensorStateWrapper(sim_copy, sim_played, up_card, dealer_idx, t1_score, t2_score, dealer_discard)
                tensor_state = encode_state_to_tensor(wrapper, leaf_player).to(device)
                state_t = tensor_state.unsqueeze(0)
                
                with torch.no_grad(): policy_logits, value = neural_net(state_t)
                v = value.item() 
                probs = F.softmax(policy_logits[0], dim=0).cpu().numpy()
                nn_cache[state_key] = (probs, v)
            
            legal_moves = get_tactical_search_moves(sim_copy) if node is root else sim_copy.get_legal_moves()
            
            priors = {}
            priors_sum = 0.0
            
            for m in legal_moves:
                abs_idx = ALL_DECK_KEYS.index(f"{m.rank}{m.suit}")
                priors[m] = probs[abs_idx]
                priors_sum += probs[abs_idx]
                
            # --- BUG FIX: Disabled Training Noise & Fixed Trick Winner Parity ---
            for idx, m in enumerate(legal_moves):
                prior = priors[m] / priors_sum if priors_sum > 0 else 1.0 / len(legal_moves)
                
                trick_target = 2 if sim_copy.is_loner else 3
                if len(sim_copy.trick) == trick_target:
                    # Move 'm' ends the trick. Calculate who actually wins it.
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
                    # Trick is ongoing. Turn passes to the left.
                    next_p = (leaf_player + 1) % 4
                    
                if sim_copy.is_loner and next_p == sim_copy.loner_partner_idx: 
                    next_p = (next_p + 1) % 4
                    
                node.children.append(AlphaNode(move=m, parent=node, prior=prior, player_idx=next_p))
            # ---------------------------------------------------------
                
        for n in reversed(search_path):
            n.visits += 1
            if n.parent is not None:
                n.wins += v if (n.parent.player_idx % 2) == (leaf_player % 2) else -v

    total_visits = sum(c.visits for c in root.children)
    if total_visits == 0: return {random.choice(sim_state.get_legal_moves()): 1.0}
    return {c.move: c.visits / total_visits for c in root.children}


# ==========================================
# 3. AI ENGINE & MULTIPROCESSING
# ==========================================
def _mcts_core_worker(data_pack, observer_idx, iterations):
    trump_suit = data_pack['trump_suit']
    trick = [(p, Card(r, s)) for p, r, s in data_pack['trick']]
    hands = [[Card(r, s) for r, s in h] for h in data_pack['hands']]
    current_turn = data_pack['current_turn']; is_loner = data_pack['is_loner']; loner_partner_idx = data_pack['loner_partner_idx']
    caller_idx = data_pack['caller_idx']; team1_tricks = data_pack.get('team1_tricks', 0); team2_tricks = data_pack.get('team2_tricks', 0)
    voids = {int(k): set(v) for k, v in data_pack['voids'].items()}
    played_cards = [Card(r, s) for r, s in data_pack['played_cards']]
    up_card = Card(data_pack['up_card'][0], data_pack['up_card'][1]) if data_pack['up_card'] else None
    
    ai_profiles = data_pack.get('ai_profiles', {})
    true_hands = [[Card(r, s) for r, s in h] for h in data_pack.get('true_hands', [[],[],[],[]])]
    my_profile = ai_profiles.get(str(observer_idx), "")

    prob_suboptimal = data_pack.get('prob_suboptimal_lead', 0.0)
    prob_defensive_trump = data_pack.get('prob_defensive_trump', 0.0)
    prob_loner_defense = data_pack.get('prob_loner_defense', 0.0)

    def get_sim_eff_suit(card):
        if trump_suit and card.rank == 'J' and card.suit == SAME_COLOR_T[trump_suit]: return trump_suit
        return card.suit

    root = Node()
    for sim_idx in range(iterations):
        sim = SimState(trump_suit, trick, hands, current_turn, is_loner, loner_partner_idx, caller_idx, voids, team1_tricks, team2_tricks)
        
        known_cards = []
        known_cards.extend(sim.hands[observer_idx]); known_cards.extend(played_cards)
        for _, card in sim.trick: known_cards.append(card)
        if up_card: known_cards.append(up_card)
        
        deck = [Card(r, s) for s in SUITS_T for r in RANKS_T]
        unknown_cards = [c for c in deck if not any(c == kc for kc in known_cards)]
        random.shuffle(unknown_cards)

        for i in range(4):
            if i == observer_idx: continue
            needed_cards = len(hands[i])
            sim.hands[i] = []
            for _ in range(needed_cards):
                if unknown_cards:
                    valid_cards = [c for c in unknown_cards if get_sim_eff_suit(c) not in voids[i]]
                    if valid_cards:
                        chosen = valid_cards[0]
                        unknown_cards.remove(chosen)
                        sim.hands[i].append(chosen)
                    else:
                        chosen = unknown_cards.pop(0)
                        sim.hands[i].append(chosen)

        node = root
        while (sim.team1_tricks + sim.team2_tricks) < 5:
            legal_cards = sim.get_legal_moves()
            if not legal_cards: break
            
            tried_moves = [c.move for c in node.children]
            untried = [m for m in legal_cards if m not in tried_moves]
            
            if untried:
                move = random.choice(untried)
                sim.apply_move(move)
                child = Node(move=move, parent=node)
                node.children.append(child)
                node = child
                break
            else:
                valid_children = [c for c in node.children if c.move in legal_cards]
                if not valid_children: break
                node = max(valid_children, key=lambda c: c.ucb1())
                sim.apply_move(node.move)
            
        while (sim.team1_tricks + sim.team2_tricks) < 5:
            legal_cards = sim.get_legal_moves()
            if not legal_cards: break
            
            move_chosen = None
            
            if sim.current_turn == observer_idx and sim.caller_idx in [1, 3]:
                if not sim.trick: 
                    if random.random() < prob_defensive_trump:
                        trumps = [c for c in legal_cards if sim.get_effective_suit(c) == sim.trump_suit]
                        if trumps: move_chosen = random.choice(trumps)
                    elif random.random() < prob_suboptimal:
                        trump_color = "red" if sim.trump_suit in ['♥', '♦'] else "black"
                        next_moves = [c for c in legal_cards if sim.get_effective_suit(c) != sim.trump_suit and ("red" if c.suit in ['♥', '♦'] else "black") == trump_color]
                        if next_moves: move_chosen = random.choice(next_moves)
                else: 
                    if sim.is_loner and random.random() < prob_loner_defense:
                        high_cards = [c for c in legal_cards if c.rank in ['A', 'K'] and sim.get_effective_suit(c) != sim.trump_suit]
                        if high_cards: move_chosen = random.choice(high_cards)
                        
            if move_chosen is None:
                move_chosen = sim.get_heuristic_move()
                
            sim.apply_move(move_chosen)
            
        tricks = sim.get_result(observer_idx)
        result = 1.0 if tricks >= 3 else 0.0
        
        while node is not None: 
            node.visits += 1; node.wins += result; node = node.parent

    results = {}
    for child in root.children: results[child.move] = (child.visits, child.wins)
    return results

class ISMCTS_Multiprocessing_Agent:
    def __init__(self, human_iters): 
        self.cores = os.cpu_count() or 4 
        self.human_total_iters = human_iters
        self.pool = concurrent.futures.ProcessPoolExecutor(max_workers=self.cores)

    def pack_ui_state(self, ui_game):
        state_dict = {
            'format': 'bot-euchre-state-v2',
            'game_state': ui_game.game_state,
            'trump_suit': ui_game.trump_suit,
            'trick': [(p, c.rank, c.suit) for p, c in ui_game.trick],
            'hands': [[(c.rank, c.suit) for c in h] for h in ui_game.hands],
            'current_turn': ui_game.current_turn,
            'is_loner': ui_game.is_loner,
            'loner_partner_idx': ui_game.loner_partner_idx,
            'caller_idx': ui_game.caller_idx,
            'dealer_idx': ui_game.dealer_idx,
            'bidding_player': ui_game.bidding_player,
            'passed_count': ui_game.passed_count,
            'passed_seats': auction_passed_seats(
                ui_game.dealer_idx, ui_game.passed_count),
            'team1_score': ui_game.team1_score,
            'team2_score': ui_game.team2_score,
            'team1_tricks': ui_game.team1_tricks,
            'team2_tricks': ui_game.team2_tricks,
            'voids': {k: list(v) for k, v in ui_game.voids.items()},
            'played_cards': [(c.rank, c.suit) for c in ui_game.played_cards],
            'up_card': (ui_game.up_card.rank, ui_game.up_card.suit) if ui_game.up_card else None,
            'dealer_discard': (
                (ui_game.dealer_discard.rank, ui_game.dealer_discard.suit)
                if getattr(ui_game, 'dealer_discard', None) else None),
            'ai_profiles': dict(ui_game.ai_profiles),
            'active_drill': ui_game.active_drill,
            'autoplay_mode': ui_game.autoplay_mode,
            'hand_seed': getattr(ui_game, 'current_hand_seed', None),
            'true_hands': [[(c.rank, c.suit) for c in h] for h in ui_game.hands] 
        }
        
        st = ui_game.stats_tracker.stats
        state_dict['prob_suboptimal_lead'] = min(st.get('active_suboptimal_defensive_leads', 0.0) * 0.10, 0.50)
        state_dict['prob_defensive_trump'] = min(st.get('active_defensive_trump_leads', 0.0) * 0.10, 0.50)
        state_dict['prob_loner_defense'] = min(st.get('active_loner_defense_blunders', 0.0) * 0.10, 0.50)
        
        return state_dict

    def get_best_move(self, ui_game, player_idx, return_confidence=False, override_iters=None, return_all_moves=False, prepacked_state=None):
        profile = ui_game.ai_profiles.get(str(player_idx), "Human")

        if profile in HYBRID_MCTS_PROFILES:
            base_iterations = (ui_game.hint_neural_play_iters if player_idx == 0
                               else ui_game.table_neural_play_iters)
            if profile in {"Monte Prime", "Iron Oracle"}:
                iterations = max(base_iterations * 3, 600)
            elif (profile in {"Iron Clutch", "Iron Endgame Edge"}
                  and ui_game.team1_tricks + ui_game.team2_tricks >= 3):
                iterations = max(base_iterations * 5, 1000)
            elif (profile == "Iron Solver"
                  and ui_game.team1_tricks + ui_game.team2_tricks >= 3):
                iterations = max(base_iterations * 6, 1200)
            else:
                iterations = max(base_iterations * 2, 400)
            if return_all_moves:
                return ui_game.get_cheems_ranked_moves(
                    player_idx, iterations=iterations)
            best_move, confidence = ui_game.get_cheems_best_move(
                player_idx, iterations=iterations)
            return (best_move, confidence) if return_confidence else best_move
        
        if profile in NEURAL_PROFILES:
            if return_all_moves:
                return ui_game.get_cheems_ranked_moves(player_idx)
            best_move, conf = ui_game.get_cheems_best_move(player_idx)
            return (best_move, conf) if return_confidence else best_move

        iters_to_run = self.human_total_iters
        if override_iters: 
            iters_to_run = override_iters
        elif profile in HEURISTIC_PROFILES:
            iters_to_run = CHEEMS_UI_PLAY_ITERS
        
        if iters_to_run < 100: iters_to_run = 100
        iters_per_core = math.ceil(iters_to_run / self.cores)
        
        data_pack = prepacked_state if prepacked_state else self.pack_ui_state(ui_game)
        aggregated_results = {}
        
        futures = [self.pool.submit(_mcts_core_worker, data_pack, player_idx, iters_per_core) for _ in range(self.cores)]
        for future in concurrent.futures.as_completed(futures):
            for card_move, (visits, wins) in future.result().items():
                if card_move not in aggregated_results: aggregated_results[card_move] = [0, 0.0]
                aggregated_results[card_move][0] += visits
                aggregated_results[card_move][1] += wins

        if not aggregated_results:
            fallback_idx = random.choice(ui_game.get_legal_moves(ui_game.hands[player_idx]))
            if return_all_moves: return [(fallback_idx, 0.0)]
            return (fallback_idx, 0.0) if return_confidence else fallback_idx

        if return_all_moves:
            ranked_moves = []
            for card_move, (visits, wins) in aggregated_results.items():
                win_rate = (wins / visits) * 100 if visits > 0 else 0.0
                idx = ui_game.hands[player_idx].index(card_move)
                ranked_moves.append((idx, win_rate))
            ranked_moves.sort(key=lambda x: x[1], reverse=True)
            return ranked_moves

        best_card = max(aggregated_results.keys(), key=lambda m: aggregated_results[m][0])
            
        best_idx = ui_game.hands[player_idx].index(best_card)
        if return_confidence:
            t_visits, t_wins = aggregated_results[best_card]
            return best_idx, (t_wins / t_visits) * 100 if t_visits > 0 else 0.0
        return best_idx

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

# ==========================================
# 4. EUCHRE UI ENGINE
# ==========================================
class EuchreGame(tk.Tk):
    def __init__(self):
        super().__init__()
        prepare_node_state()
        self.title("Bot Euchre")
        self.geometry("1400x900") 
        
        self.main_bg_color = "#2E8B57"
        self.dark_bg_color = "#1a5934" 
        self.coach_bg_color = "#223b2b" 
        
        self.configure(bg=self.main_bg_color) 
        
        self.ai_model = None  
        self.stats_tracker = StatsTracker()
        self.settings_store = SettingsStore()
        self.session_journal = SessionJournal()
        self.tournament_state = None
        try:
            self.human_league_state = load_human_league_state()
        except (OSError, TypeError, ValueError):
            self.human_league_state = None
        self.human_league_game_active = False
        self.open_windows = {}
        self.task_generation = 0
        self.active_searches = 0
        self.search_timings = {}
        self.search_timing_history = []
        self.current_hand_seed = None
        self.checkpoint_status = {}
        self.last_diagnostic_path = None
        self.comparison_lock = threading.Lock()
        self._autosave_callback = None
        self.display_iters = "100k"
        self.hint_neural_play_iters = CHEEMS_UI_PLAY_ITERS
        self.hint_neural_bid_rollouts = CHEEMS_UI_BID_ROLLOUTS
        self.hint_neural_discard_determinizations = CHEEMS_UI_DISCARD_DETERMINIZATIONS
        self.table_neural_play_iters = CHEEMS_UI_PLAY_ITERS
        self.table_neural_bid_rollouts = CHEEMS_UI_BID_ROLLOUTS
        self.table_neural_discard_determinizations = CHEEMS_UI_DISCARD_DETERMINIZATIONS
        
        self.cheems_brain = None
        self.ironclad_brain = None
        self.kyle_brain = None
        self.committee_brain = None
        self.unanimous_council_brain = None
        self.copycat_style_scores = {
            "Arbiter": 1.0, "Ironclad": 0.0, "Kyle": 0.0}
        self.wildcard_hand_profiles = {}
        self.cheems_device = "cpu"
        self._init_alpha_cheems_brain()
            
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.game_state = "dealing" 
        self.trump_suit = None; self.tracked_trump_suit = None; self.up_card = None
        self.trick = []; self.played_cards = []; self.hands = [[], [], [], []] 
        self.current_turn = 0
        
        self.sandbox_mode = False; self.saved_initial_deck = []; self.saved_dealer_idx = 0
        self.is_rewind_mode = False  
        self.active_drill = "Standard Match"
        self.dealer_idx = random.randint(0, 3) 
        self.caller_idx = -1; self.is_loner = False; self.loner_partner_idx = -1 
        self.played_card_labels = [] 
        
        self.voids = {0: set(), 1: set(), 2: set(), 3: set()}
        self.bidding_player = 0; self.passed_count = 0; self.autoplay_mode = False
        self.team1_score = 0; self.team2_score = 0; self.team1_tricks = 0; self.team2_tricks = 0
        self.trainer_mistakes = []; self.cached_hint = None
        self.hand_bid_feedback = ""
        
        self.ai_profiles = {"0": "Human", "1": "Arbiter", "2": "Arbiter", "3": "Arbiter"}
        
        self.hand_expected_tricks = -1; self.hand_accuracy_sum = 0.0; self.hand_accuracy_count = 0; self.trick_snapshots = {} 
        
        self.after(50, self._startup_flow)

    def _startup_flow(self):
        payload = None
        if os.path.exists(AUTOSAVE_PATH):
            try:
                payload = load_versioned_mapping(
                    AUTOSAVE_PATH, "bot-euchre-autosave")
            except (OSError, ValueError, TypeError):
                payload = None
        if payload and payload.get("state") and messagebox.askyesno(
                "Continue Previous Session",
                "Bot Euchre found an autosaved game. Continue where you left off?"):
            try:
                self._start_restored_session(payload)
                return
            except Exception as error:
                messagebox.showerror(
                    "Recovery Error",
                    f"The autosave could not be restored:\n{error}\n\n"
                    "Bot Euchre will open a fresh setup.")
        self._clear_autosave()
        self.prompt_for_names()

    def _configure_runtime_from_settings(self):
        global PLAYER_NAMES
        settings = self.settings_store.data
        PLAYER_NAMES[0] = settings.get("player_name", "You") or "You"
        self.trainer_mode_var = tk.BooleanVar(
            value=bool(settings.get("trainer_mode", False)))
        hint_preset = settings.get("hint_search", "Balanced")
        table_preset = settings.get("table_search", "Balanced")
        (self.hint_neural_play_iters,
         self.hint_neural_bid_rollouts,
         self.hint_neural_discard_determinizations) = NEURAL_SEARCH_PRESETS.get(
            hint_preset, NEURAL_SEARCH_PRESETS["Balanced"])
        (self.table_neural_play_iters,
         self.table_neural_bid_rollouts,
         self.table_neural_discard_determinizations) = NEURAL_SEARCH_PRESETS.get(
            table_preset, NEURAL_SEARCH_PRESETS["Balanced"])
        engine_choice = settings.get(
            "engine_speed", "Grandmaster (100,000 Iterations)")
        if "20,000" in engine_choice:
            target_iters, self.display_iters = 20000, "20k"
        elif "1,000,000" in engine_choice:
            target_iters, self.display_iters = 1000000, "1M"
        else:
            target_iters, self.display_iters = 100000, "100k"
        self.ai_model = ISMCTS_Multiprocessing_Agent(human_iters=target_iters)
        if settings.get("high_contrast", False):
            self.main_bg_color = self.dark_bg_color = "#000000"
            self.coach_bg_color = "#101010"
        elif settings.get("dark_mode", True):
            self.main_bg_color, self.dark_bg_color = "#222222", "#111111"
            self.coach_bg_color = "#1a1a1a"
        self.configure(bg=self.main_bg_color)

    def _start_restored_session(self, payload):
        self._configure_runtime_from_settings()
        state = payload["state"]
        restored_profiles = {
            str(key): value for key, value in state.get(
                "ai_profiles", self.ai_profiles).items()}
        for seat in (0, 1, 2, 3):
            seat_key = str(seat)
            restored_value = restored_profiles.get(seat_key)
            self.ai_profiles[seat_key] = normalize_profile_name(
                restored_value,
                default="Arbiter",
                allow_human=(seat == 0),
            )
        for seat in (1, 2, 3):
            PLAYER_NAMES[seat] = self.ai_profiles.get(str(seat), "Arbiter")
        self.active_drill = state.get("active_drill", "Standard Match")
        self.tournament_state = payload.get("tournament_state")
        self.human_league_game_active = bool(
            payload.get("human_league_game_active", False)
            and self.human_league_state
            and self.human_league_state.get("status") == "active")
        if self.tournament_state:
            defaults = {
                "points_a": 0, "points_b": 0, "hands": 0,
                "euchres_a": 0, "euchres_b": 0,
                "loners_a": 0, "loners_b": 0,
                "loner_sweeps_a": 0, "loner_sweeps_b": 0,
                "paused": False, "started_at": time.time(),
                "game_started_at": time.time(), "games": [],
            }
            for key, value in defaults.items():
                self.tournament_state.setdefault(key, value)
        journal = payload.get("journal", {})
        self.session_journal.started_at = journal.get("started_at", time.time())
        self.session_journal.events = journal.get("events", [])
        self.session_journal.hands_completed = journal.get("hands_completed", 0)
        self.session_journal.games_completed = journal.get("games_completed", 0)
        self.session_journal.ai_consultations = journal.get(
            "ai_consultations", {})
        self.deiconify()
        self.setup_ui()
        self._restore_game_state(state)
        if self.tournament_state:
            self.show_tournament_dashboard()
        self._record_session_event("session_restored", {
            "saved_at": payload.get("saved_at")})

    def _restore_game_state(self, state):
        self._invalidate_tasks()
        self.game_state = state.get("game_state", "playing")
        self.trump_suit = state.get("trump_suit")
        self.trick = [(p, Card(rank, suit)) for p, rank, suit in state.get("trick", [])]
        self.hands = [
            [Card(rank, suit) for rank, suit in hand]
            for hand in state.get("hands", [[], [], [], []])]
        self.current_turn = state.get("current_turn", 0)
        self.is_loner = bool(state.get("is_loner", False))
        self.loner_partner_idx = state.get("loner_partner_idx", -1)
        self.caller_idx = state.get("caller_idx", -1)
        self.dealer_idx = state.get("dealer_idx", 0)
        self.bidding_player = state.get("bidding_player", 0)
        self.passed_count = state.get("passed_count", 0)
        self.team1_score = state.get("team1_score", 0)
        self.team2_score = state.get("team2_score", 0)
        self.team1_tricks = state.get("team1_tricks", 0)
        self.team2_tricks = state.get("team2_tricks", 0)
        self.voids = {
            int(key): set(value) for key, value in state.get(
                "voids", {0: [], 1: [], 2: [], 3: []}).items()}
        self.played_cards = [
            Card(rank, suit) for rank, suit in state.get("played_cards", [])]
        up_card = state.get("up_card")
        self.up_card = Card(*up_card) if up_card else None
        discard = state.get("dealer_discard")
        self.dealer_discard = Card(*discard) if discard else None
        self.autoplay_mode = bool(state.get("autoplay_mode", False))
        self.current_hand_seed = state.get("hand_seed")
        self.loner_var.set(self.is_loner)
        self.autoplay_menu_button.config(text=(
            f"? Autoplay: {self.ai_profiles.get('0', 'Off')}"
            if self.autoplay_mode else "? Autoplay: Off"))
        if self.trump_suit:
            self.lbl_trump.config(
                text=f"TRUMP: {self.trump_suit}", bg="yellow")
        else:
            self.lbl_trump.config(text="TRUMP: Uncalled", bg="white")
        self.update_scoreboard()
        self.update_dealer_chip()
        self.update_table_graphics()
        self.render_human_hand()
        if self.game_state in {"bidding_r1", "bidding_r2"}:
            self.after(100, self.process_bidding)
        elif self.game_state == "discarding":
            self.after(100, self.process_discard)
        elif self.game_state == "playing":
            self.after(100, self._resume_current_autoplay_turn)

    def _autosave_payload(self):
        return {
            "format": "bot-euchre-autosave-v2",
            "_schema": "bot-euchre-autosave",
            "_schema_version": DATA_SCHEMA_VERSION,
            "saved_at": time.time(),
            "state": self._snapshot_for_journal(),
            "tournament_state": self.tournament_state,
            "human_league_game_active": self.human_league_game_active,
            "journal": {
                "started_at": self.session_journal.started_at,
                "events": self.session_journal.events,
                "hands_completed": self.session_journal.hands_completed,
                "games_completed": self.session_journal.games_completed,
                "ai_consultations": self.session_journal.ai_consultations,
            },
        }

    def _schedule_autosave(self):
        if not self.ai_model or self.game_state in {"main_menu", "dealing"}:
            return
        if self._autosave_callback is not None:
            try:
                self.after_cancel(self._autosave_callback)
            except tk.TclError:
                pass
        self._autosave_callback = self.after(200, self._write_autosave)

    def _write_autosave(self):
        self._autosave_callback = None
        payload = self._autosave_payload()
        if payload["state"] is not None:
            try:
                atomic_write_json(AUTOSAVE_PATH, payload)
            except OSError as error:
                failures = getattr(self, "_autosave_write_failures", 0) + 1
                self._autosave_write_failures = failures
                if failures == 1 or failures % 10 == 0:
                    print(
                        f"[Autosave:{NODE_ID}] Save deferred to "
                        f"{AUTOSAVE_PATH} ({error}); retrying.",
                        file=sys.stderr)
                self._autosave_callback = self.after(1000, self._write_autosave)
            else:
                if getattr(self, "_autosave_write_failures", 0):
                    print(
                        f"[Autosave:{NODE_ID}] Save recovered at "
                        f"{AUTOSAVE_PATH}.", file=sys.stderr)
                self._autosave_write_failures = 0

    def _clear_autosave(self):
        try:
            os.remove(AUTOSAVE_PATH)
        except FileNotFoundError:
            pass

    def _invalidate_tasks(self):
        self.task_generation += 1
        return self.task_generation

    def _task_token(self):
        return self.task_generation

    def _post_if_current(self, token, callback, *args):
        if token == self.task_generation and self.winfo_exists():
            self.after(0, callback, *args)

    def _launch_search(self, name, work, on_success, on_error=None):
        token = self._task_token()
        started = time.perf_counter()
        self.active_searches += 1

        def runner():
            result = None
            error = None
            error_traceback = None
            try:
                result = work()
            except Exception as caught:
                error = caught
                error_traceback = traceback.format_exc()

            def finish():
                self.active_searches = max(0, self.active_searches - 1)
                duration = time.perf_counter() - started
                self.search_timings[name] = duration
                self.search_timing_history.append({
                    "timestamp": time.time(), "name": name,
                    "duration": duration,
                })
                del self.search_timing_history[:-500]
                if token != self.task_generation:
                    return
                if error is not None:
                    self.last_diagnostic_path = self._write_diagnostic_bundle(
                        error=error, context=name,
                        traceback_text=error_traceback)
                    if on_error:
                        on_error(error)
                    else:
                        print(f"{name} Error: {error}")
                    return
                if isinstance(result, tuple):
                    on_success(*result)
                else:
                    on_success(result)

            try:
                self.after(0, finish)
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=runner, daemon=True).start()
        return token

    def _profile_badge(self, profile_name):
        return PROFILE_CATEGORIES.get(profile_name, "Profile")

    def _new_tool_window(self, key, title, geometry):
        existing = self.open_windows.get(key)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return existing, False
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry(geometry)
        dialog.configure(bg=self.coach_bg_color)
        self.open_windows[key] = dialog
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._close_tool_window(key))
        return dialog, True

    def _close_tool_window(self, key):
        dialog = self.open_windows.pop(key, None)
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()

    def _close_all_tool_windows(self):
        for key in list(self.open_windows):
            self._close_tool_window(key)

    def _refresh_windows_menu(self):
        if not hasattr(self, "windows_menu"):
            return
        self.windows_menu.delete(0, tk.END)
        live_windows = [
            (key, window) for key, window in self.open_windows.items()
            if window.winfo_exists()]
        if not live_windows:
            self.windows_menu.add_command(label="No tool windows open", state=tk.DISABLED)
        for key, window in live_windows:
            self.windows_menu.add_command(
                label=f"Focus: {window.title()}",
                command=lambda item=window: (item.deiconify(), item.lift(), item.focus_force()))
        if live_windows:
            self.windows_menu.add_separator()
            self.windows_menu.add_command(
                label="Close All Tool Windows", command=self._close_all_tool_windows)

    def show_model_health(self):
        dialog, created = self._new_tool_window(
            "model_health", "Model Health", "620x460")
        if not created:
            return
        heading = tk.Label(
            dialog, text="Model and Search Health", font=("Arial", 16, "bold"),
            bg=self.coach_bg_color, fg="white")
        heading.pack(pady=12)
        text = tk.Label(
            dialog, bg=self.dark_bg_color, fg="white", font=("Consolas", 11),
            justify=tk.LEFT, anchor="nw", padx=18, pady=18)
        text.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 14))

        def refresh():
            if not dialog.winfo_exists():
                return
            models = [
                ("Arbiter", self.cheems_brain),
                ("Ironclad", self.ironclad_brain),
                ("Kyle", self.kyle_brain),
                ("Council", self.unanimous_council_brain),
            ]
            lines = [
                f"Compute device: {self.cheems_device}",
                f"PyTorch available: {'yes' if HAS_TORCH else 'no'}",
                f"Active searches: {self.active_searches}",
                f"Worker generation: {self.task_generation}", "",
            ]
            lines.extend(
                f"{name:<14} {self.checkpoint_status.get(name, 'ONLINE' if model is not None else 'UNAVAILABLE')}"
                for name, model in models)
            if HAS_TORCH and self.cheems_device == "cuda":
                lines.extend([
                    "",
                    f"CUDA allocated: {torch.cuda.memory_allocated() / 1048576:.1f} MB",
                    f"CUDA reserved:  {torch.cuda.memory_reserved() / 1048576:.1f} MB",
                ])
            if self.search_timings:
                lines.append("")
                lines.extend(
                    f"Last {name}: {duration:.3f}s"
                    for name, duration in sorted(self.search_timings.items()))
            lines.extend([
                "",
                f"Recommended preset: {recommended_performance_preset(has_accelerator=self.cheems_device in {'cuda', 'mps'})}",
                f"Last diagnostic: {self.last_diagnostic_path or 'none'}",
            ])
            text.config(text="\n".join(lines))
            dialog.after(750, refresh)
        refresh()

    def show_settings_management(self):
        dialog, created = self._new_tool_window(
            "settings", "Settings Management", "470x340")
        if not created:
            return
        tk.Label(
            dialog, text="Settings Management", font=("Arial", 16, "bold"),
            bg=self.coach_bg_color, fg="white").pack(pady=16)
        tk.Label(
            dialog,
            text="Reset saved setup choices without manually deleting JSON files.",
            bg=self.coach_bg_color, fg="white", wraplength=400).pack(pady=6)

        def reset(section, label):
            if messagebox.askyesno(
                    "Confirm Reset", f"Reset {label}?", parent=dialog):
                self.settings_store.reset(section)
                messagebox.showinfo(
                    "Settings Reset", f"{label.capitalize()} reset.", parent=dialog)
        for label, section in [
                ("saved presets", "presets"),
                ("profile favorites", "favorites"),
                ("all preferences", "all")]:
            tk.Button(
                dialog, text=f"Reset {label.title()}",
                command=lambda value=section, text=label: reset(value, text),
                bg="#8B0000" if section == "all" else "#59636B", fg="white",
                font=("Arial", 10, "bold"), width=24).pack(pady=7)
        tk.Button(
            dialog, text="Apply Hardware Recommendation",
            command=self.apply_hardware_recommendation,
            bg="#1E90FF", fg="white", font=("Arial", 10, "bold"),
            width=28).pack(pady=10)

    def apply_hardware_recommendation(self):
        preset = recommended_performance_preset(
            has_accelerator=self.cheems_device in {"cuda", "mps"})
        values = NEURAL_SEARCH_PRESETS[preset]
        (self.hint_neural_play_iters,
         self.hint_neural_bid_rollouts,
         self.hint_neural_discard_determinizations) = values
        (self.table_neural_play_iters,
         self.table_neural_bid_rollouts,
         self.table_neural_discard_determinizations) = values
        self.settings_store.data.update({
            "hint_search": preset, "table_search": preset})
        self.settings_store.save()
        messagebox.showinfo(
            "Performance Preset",
            f"Applied {preset} to advice, Autoplay, and table searches.")

    def _init_alpha_cheems_brain(self):
        if not HAS_TORCH:
            print("[System] PyTorch missing. Arbiter running in mock fallback mode.")
            return

        if torch.cuda.is_available():
            self.cheems_device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            self.cheems_device = "mps"
        else:
            self.cheems_device = "cpu"

        self.cheems_brain = self._load_neural_brain(ARBITER_WEIGHTS_PATH, "Arbiter")
        self.ironclad_brain = self._load_neural_brain(IRONCLAD_WEIGHTS_PATH, "Ironclad")
        self.kyle_brain = self._load_neural_brain(KYLE_WEIGHTS_PATH, "Kyle")
        committee_members = [self.cheems_brain, self.ironclad_brain, self.kyle_brain]
        if all(brain is not None for brain in committee_members):
            self.committee_brain = CommitteeNeuralNet(committee_members)
            self.committee_brain.eval()
            self.unanimous_council_brain = UnanimousCouncilNeuralNet(committee_members)
            self.unanimous_council_brain.eval()
            print("[System] Committee ensemble online (Arbiter + Ironclad + Kyle).")

    def _load_neural_brain(self, weights_path, profile_name):
        if not os.path.exists(weights_path):
            print(f"[System] {profile_name} weights missing at {weights_path}.")
            self.checkpoint_status[profile_name] = "MISSING"
            return None
        try:
            brain = CheemsNeuralNet()
            checkpoint = torch.load(
                weights_path, map_location=self.cheems_device, weights_only=True)
            validate_checkpoint_state_dict(checkpoint, brain.state_dict())
            brain.load_state_dict(checkpoint)
            brain.to(self.cheems_device)
            brain.eval()
            self.checkpoint_status[profile_name] = "ONLINE / COMPATIBLE"
            print(f"[System] Successfully loaded {profile_name} onto '{self.cheems_device}'.")
            return brain
        except Exception as e:
            self.checkpoint_status[profile_name] = f"INCOMPATIBLE: {e}"
            print(f"[System] {profile_name} model load failed: {e}")
            return None

    def _get_neural_brain(self, player_idx):
        profile = self.ai_profiles.get(str(player_idx), "Arbiter")
        if profile == "Wildcard":
            profile = self.wildcard_hand_profiles.setdefault(
                player_idx, random.choice(WILDCARD_PROFILES))
        if profile == "Ironclad":
            return self.ironclad_brain
        if profile == "Kyle":
            return self.kyle_brain
        if profile == "Unanimous Council":
            return self.unanimous_council_brain
        if profile == "Risk Manager":
            return self.ironclad_brain
        if profile in {"Iron Monte", "Iron Solver"}:
            return self.ironclad_brain
        if profile in {
            "Iron Sleuth", "Iron Closer",
            "Iron Clutch", "Iron Endgame Edge"}:
            return self.ironclad_brain
        if profile in {"Monte Prime", "Iron Oracle"}:
            if self.game_state in {"bidding_r1", "bidding_r2", "discarding"}:
                return self.ironclad_brain
            return self.unanimous_council_brain
        if profile == "The Closer":
            own_score, opponent_score = self._scores_for_player(player_idx)
            if own_score >= 8 or own_score > opponent_score:
                return self.ironclad_brain
            if opponent_score - own_score >= 2:
                return self.kyle_brain
        return self.cheems_brain

    def _scores_for_player(self, player_idx):
        if player_idx % 2 == 0:
            return self.team1_score, self.team2_score
        return self.team2_score, self.team1_score

    def _profile_move_choice(self, player_idx, ranked_moves, state_pack=None):
        profile = self.ai_profiles.get(str(player_idx), "Arbiter")
        if state_pack is None:
            own_score, opponent_score = self._scores_for_player(player_idx)
            hand = self.hands[player_idx]
            trick = self.trick
            trump_suit = self.trump_suit
        else:
            team1_score = int(state_pack.get('team1_score', self.team1_score))
            team2_score = int(state_pack.get('team2_score', self.team2_score))
            if player_idx % 2 == 0:
                own_score, opponent_score = team1_score, team2_score
            else:
                own_score, opponent_score = team2_score, team1_score
            packed_hands = state_pack.get('hands', [[], [], [], []])
            hand = [Card(rank, suit) for rank, suit in packed_hands[player_idx]]
            trick = [(seat, Card(rank, suit))
                     for seat, rank, suit in state_pack.get('trick', [])]
            trump_suit = state_pack.get('trump_suit')

        def effective_suit(card):
            if (trump_suit and card.rank == 'J'
                    and card.suit == SAME_COLOR_T[trump_suit]):
                return trump_suit
            return card.suit

        def sleuth_key(item):
            card = hand[item[0]]
            effective = effective_suit(card)
            if trick:
                led_suit = effective_suit(trick[0][1])
                if effective == trump_suit and led_suit != trump_suit:
                    return 1
                if effective == led_suit:
                    return 0
            return 0 if effective != trump_suit else 1

        return choose_iron_profile_move(
            profile, ranked_moves, 4.5,
            score_gap=own_score - opponent_score,
            sleuth_key=sleuth_key)

    def _hoyle_bid_decision(self, player_idx, round_num, is_stuck):
        hand = self.hands[player_idx]
        suits = [self.up_card.suit] if round_num == 1 else [
            s for s in SUITS_T if s != self.up_card.suit]
        best_suit = suits[0]
        best_score = -999.0
        for suit in suits:
            score, _ = self.calculate_hand_power(hand, suit)
            # Hoyle convention: mild preference for "next" in round two.
            if round_num == 2 and suit == SAME_COLOR_T[self.up_card.suit]:
                score += 0.35
            if score > best_score:
                best_score = score
                best_suit = suit

        off_aces = sum(
            1 for card in hand
            if self.get_effective_suit(card) != best_suit and card.rank == 'A')
        call_threshold = 6.2
        if round_num == 2:
            call_threshold -= 0.2
        if is_stuck:
            call_threshold = min(call_threshold, 5.7)
        if best_score < call_threshold and not is_stuck:
            return "Pass", best_suit, False

        # Strictly conservative loners: require a true monster with outside control.
        is_loner = best_score >= 8.8 and off_aces >= 1
        return "Call", best_suit, is_loner

    def _hoyle_move_power(self, card, led_suit):
        rank_base_vals = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
        pwr = rank_base_vals[card.rank]
        eff_s = self.get_effective_suit(card)
        if card.rank == 'J' and card.suit == self.trump_suit:
            pwr += 500
        elif card.rank == 'J' and card.suit == SAME_COLOR_T[self.trump_suit]:
            pwr += 400
        elif eff_s == self.trump_suit:
            pwr += 100
        elif eff_s == led_suit:
            pwr += 50
        else:
            pwr = 0
        return pwr

    def get_hoyle_ranked_moves(self, player_idx):
        hand = self.hands[player_idx]
        legal_indices = self.get_legal_moves(hand)
        if not legal_indices:
            return []
        if len(legal_indices) == 1:
            return [(legal_indices[0], 100.0)]

        led_suit = self.get_effective_suit(self.trick[0][1]) if self.trick else None
        partner_idx = (player_idx + 2) % 4
        trick_target = 2 if self.is_loner else 3
        ranked = []

        if self.trick:
            highest_power = -1
            winning_player = -1
            for p_idx, c in self.trick:
                pwr = self._hoyle_move_power(c, led_suit)
                if pwr > highest_power:
                    highest_power = pwr
                    winning_player = p_idx

            for idx in legal_indices:
                card = hand[idx]
                pwr = self._hoyle_move_power(card, led_suit)
                can_win = pwr > highest_power
                if can_win:
                    # Prefer the lowest card that still wins.
                    score = 70.0 - pwr * 0.01
                else:
                    # Prefer low throwaways when not winning.
                    score = 50.0 - pwr * 0.01

                # If partner is already winning and we are last, avoid overtake.
                if (winning_player == partner_idx and len(self.trick) == trick_target
                        and can_win):
                    score -= 25.0

                ranked.append((idx, score))
        else:
            # Opening lead conventions: caller side likes pulling trump, defenders
            # prefer off-suit A leads when available.
            caller_team = 1 if self.caller_idx in [0, 2] else 2
            my_team = 1 if player_idx in [0, 2] else 2
            suit_counts = {}
            for c in hand:
                suit_counts[self.get_effective_suit(c)] = (
                    suit_counts.get(self.get_effective_suit(c), 0) + 1)

            for idx in legal_indices:
                card = hand[idx]
                eff = self.get_effective_suit(card)
                score = 50.0
                if my_team == caller_team and eff == self.trump_suit:
                    score += 15.0
                if my_team != caller_team and eff != self.trump_suit and card.rank == 'A':
                    score += 20.0
                if eff != self.trump_suit and suit_counts.get(eff, 0) == 1:
                    score += 4.0
                score += {'9': 1, '10': 2, 'J': 3, 'Q': 4, 'K': 5, 'A': 6}[card.rank] * 0.3
                ranked.append((idx, score))

        ranked.sort(key=lambda item: item[1], reverse=True)
        if not ranked:
            return [(idx, 100.0 / len(legal_indices)) for idx in legal_indices]
        top_score = ranked[0][1]
        # Convert heuristic scores to a simple confidence-like scale.
        out = []
        for idx, score in ranked:
            confidence = max(0.0, min(100.0, 50.0 + (score - top_score)))
            out.append((idx, confidence))
        return out

    def _build_live_play_snapshot(self, player_idx, known_hands=None):
        sim_state = SimState(
            self.trump_suit,
            list(self.trick),
            [list(h) for h in self.hands],
            self.current_turn,
            self.is_loner,
            self.loner_partner_idx,
            self.caller_idx,
            self.voids,
            self.team1_tricks,
            self.team2_tricks,
        )
        active_cards = [c for h in sim_state.hands for c in h] + [c for _, c in sim_state.trick]
        original_cards = active_cards + list(self.played_cards)
        known = self._get_neural_known_hands(player_idx) if known_hands is None else known_hands
        dealer_discard = getattr(self, 'dealer_discard', None)
        return (
            sim_state, original_cards, self.up_card, self.dealer_idx,
            self.team1_score, self.team2_score, dealer_discard, known,
        )

    def _build_packed_play_snapshot(self, state_pack, player_idx, known_hands=None):
        trick = [(p, Card(r, s)) for p, r, s in state_pack.get('trick', [])]
        hands = [[Card(r, s) for r, s in hand] for hand in state_pack.get('hands', [[], [], [], []])]
        voids_raw = state_pack.get('voids', {0: [], 1: [], 2: [], 3: []})
        voids = {int(k): set(v) for k, v in voids_raw.items()}
        for seat in range(4):
            voids.setdefault(seat, set())

        sim_state = SimState(
            state_pack.get('trump_suit'),
            trick,
            hands,
            int(state_pack.get('current_turn', player_idx)),
            bool(state_pack.get('is_loner', False)),
            int(state_pack.get('loner_partner_idx', -1)),
            int(state_pack.get('caller_idx', -1)),
            voids,
            int(state_pack.get('team1_tricks', 0)),
            int(state_pack.get('team2_tricks', 0)),
        )

        played_cards = [Card(r, s) for r, s in state_pack.get('played_cards', [])]
        active_cards = [c for h in sim_state.hands for c in h] + [c for _, c in sim_state.trick]
        original_cards = active_cards + played_cards

        up_raw = state_pack.get('up_card')
        up_card = Card(up_raw[0], up_raw[1]) if up_raw else None
        discard_raw = state_pack.get('dealer_discard')
        dealer_discard = Card(discard_raw[0], discard_raw[1]) if discard_raw else None
        known = self._get_neural_known_hands(player_idx) if known_hands is None else known_hands
        return (
            sim_state, original_cards, up_card,
            int(state_pack.get('dealer_idx', self.dealer_idx)),
            int(state_pack.get('team1_score', self.team1_score)),
            int(state_pack.get('team2_score', self.team2_score)),
            dealer_discard, known,
        )

    def get_cheems_ranked_moves(self, player_idx=0, known_hands=None, neural_brain=None,
                                iterations=None, state_pack=None):
        if state_pack is None:
            (sim_state, original_cards, up_card, dealer_idx,
             t1_score, t2_score, dealer_discard, known_hands_eval) = self._build_live_play_snapshot(
                player_idx, known_hands=known_hands)
        else:
            (sim_state, original_cards, up_card, dealer_idx,
             t1_score, t2_score, dealer_discard, known_hands_eval) = self._build_packed_play_snapshot(
                state_pack, player_idx, known_hands=known_hands)

        if player_idx < 0 or player_idx >= len(sim_state.hands):
            return []
        search_hand = sim_state.hands[player_idx]
        if sim_state.current_turn == player_idx:
            legal_cards = sim_state.get_legal_moves()
            legal_indices = [i for i, card in enumerate(search_hand) if card in legal_cards]
        else:
            legal_indices = list(range(len(search_hand)))
        if not legal_indices: return []
        if len(legal_indices) == 1: return [(legal_indices[0], 100.0)]

        if neural_brain is None:
            neural_brain = self._get_neural_brain(player_idx)
        if not HAS_TORCH or neural_brain is None:
            return [(idx, 100.0 / len(legal_indices)) for idx in legal_indices]
        if iterations is None:
            iterations = (self.hint_neural_play_iters if player_idx == 0
                          else self.table_neural_play_iters)
        if self.ai_profiles.get(str(player_idx)) == "Unanimous Council":
            iterations *= 2

        try:
            # Run AlphaZero tree search at the configured UI depth.
            policy_dict = run_alphazero_mcts(
                sim_state=sim_state,
                neural_net=neural_brain,
                iterations=iterations,
                original_cards=original_cards,
                up_card=up_card,
                dealer_idx=dealer_idx,
                t1_score=t1_score,
                t2_score=t2_score,
                device=self.cheems_device,
                dealer_discard=dealer_discard,
                known_hands=known_hands_eval,
            )

            ranked_moves = []
            for card, visits_ratio in policy_dict.items():
                for idx in legal_indices:
                    # Match by rank and suit instead of object reference
                    if (search_hand[idx].rank == card.rank
                            and search_hand[idx].suit == card.suit):
                        weight = visits_ratio * 100.0
                        ranked_moves.append((idx, weight))
                        break

            ranked_moves.sort(key=lambda x: x[1], reverse=True)
            return ranked_moves
        except Exception as e:
            print(f"Arbiter MCTS Error (seat {player_idx}, turn {sim_state.current_turn}): {e}")
            return [(idx, 100.0 / len(legal_indices)) for idx in legal_indices]

    def _get_neural_known_hands(self, player_idx):
        return {}

    def _get_autoplay_known_hands(self, player_idx):
        return None

    def get_cheems_best_move(self, player_idx, known_hands=None, iterations=None,
                             state_pack=None):
        if state_pack is None:
            legal_indices = self.get_legal_moves(self.hands[player_idx])
        else:
            packed_hands = state_pack.get('hands', [[], [], [], []])
            if player_idx < 0 or player_idx >= len(packed_hands):
                return 0, 0.0
            packed_hand = [Card(r, s) for r, s in packed_hands[player_idx]]
            trick = [(p, Card(r, s)) for p, r, s in state_pack.get('trick', [])]
            sim_view = SimState(
                state_pack.get('trump_suit'),
                trick,
                [packed_hand if i == player_idx else [] for i in range(4)],
                player_idx,
                bool(state_pack.get('is_loner', False)),
                int(state_pack.get('loner_partner_idx', -1)),
                int(state_pack.get('caller_idx', -1)),
                {0: set(), 1: set(), 2: set(), 3: set()},
                int(state_pack.get('team1_tricks', 0)),
                int(state_pack.get('team2_tricks', 0)),
            )
            legal_cards = sim_view.get_legal_moves()
            legal_indices = [i for i, card in enumerate(packed_hand) if card in legal_cards]
        if not legal_indices: return 0, 0.0
        if len(legal_indices) == 1: return legal_indices[0], 100.0
            
        ranked_moves = self.get_cheems_ranked_moves(
            player_idx, known_hands=known_hands, iterations=iterations,
            state_pack=state_pack)
        if ranked_moves:
            profile = self.ai_profiles.get(str(player_idx))
            if (profile == "Risk Manager" and len(ranked_moves) > 1
                    and ranked_moves[0][1] - ranked_moves[1][1] <= 5.0):
                chosen = ranked_moves[1]
            else:
                chosen = self._profile_move_choice(
                    player_idx, ranked_moves, state_pack=state_pack)
            return chosen[0], chosen[1]
        
        fallback = random.choice(legal_indices)
        return fallback, 0.0

    def get_hoyle_best_move(self, player_idx):
        legal_indices = self.get_legal_moves(self.hands[player_idx])
        if not legal_indices:
            return 0, 0.0
        ranked = self.get_hoyle_ranked_moves(player_idx)
        if ranked:
            return ranked[0]
        return random.choice(legal_indices), 0.0

    def sort_hand(self, hand):
        def hand_sort_key(card):
            if not self.trump_suit:
                return (SUITS_T.index(card.suit), RANKS_T.index(card.rank))
            
            is_right = (card.rank == 'J' and card.suit == self.trump_suit)
            is_left = (card.rank == 'J' and card.suit == SAME_COLOR_T[self.trump_suit])
            
            eff_suit = self.trump_suit if is_left else card.suit
            suit_order = 0 if eff_suit == self.trump_suit else (SUITS_T.index(eff_suit) + 1)
            
            if is_right: rank_order = 7
            elif is_left: rank_order = 6
            else: rank_order = RANKS_T.index(card.rank)
            
            return (suit_order, rank_order)
            
        hand.sort(key=hand_sort_key)

    def get_player_display_name(self, player_idx):
        role_names = {1: "Opponent 1", 2: "Partner", 3: "Opponent 2"}
        player_name = (
            self.ai_profiles.get(str(player_idx), PLAYER_NAMES[player_idx])
            if (self.tournament_state
                or getattr(self, "human_league_game_active", False))
            else PLAYER_NAMES[player_idx])
        if player_idx == 0:
            if self.autoplay_mode:
                return f"{self.ai_profiles.get('0', 'AI')} (Your Seat)"
            return player_name
        return f"{role_names[player_idx]} ({player_name})"

    def _refresh_seat_labels(self):
        for player_idx, label in getattr(self, "seat_name_labels", {}).items():
            label.config(text=self.get_player_display_name(player_idx))
        self.update_scoreboard()

    def prompt_for_names(self):
        self.withdraw()  
        auto_claim_league = os.environ.get("BOT_EUCHRE_AUTO_CLAIM_LEAGUE") == "1"
        setup_bg = "#151817"
        setup_panel = "#202522"
        setup_field = "#2B302D"
        setup_text = "#F2F4F2"
        setup_muted = "#B8C2BA"
        setup_accent = "#71C784"
        setup_gold = "#E4C66A"

        def style_option_menu(option_menu, width):
            option_menu.config(
                font=("Arial", 11), width=width, bg=setup_field,
                fg=setup_text, activebackground="#3A433D",
                activeforeground="white", highlightthickness=1,
                highlightbackground="#465149", bd=0)
            option_menu["menu"].config(
                bg=setup_field, fg=setup_text,
                activebackground=setup_accent, activeforeground="#101410",
                font=("Arial", 10), bd=0)

        def make_setup_checkbutton(text, variable, foreground=setup_text):
            checkbutton = tk.Checkbutton(
                dialog, text=text, variable=variable, bg=setup_bg,
                fg=foreground, activebackground=setup_bg,
                activeforeground=foreground, selectcolor=setup_field,
                font=("Arial", 12, "bold"), highlightthickness=0)
            checkbutton.pack(pady=2)
            return checkbutton

        dialog = tk.Toplevel(self)
        dialog.title("Bot Euchre Setup")
        dialog.geometry("600x850")
        dialog.configure(bg=setup_bg)
        dialog.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        tk.Label(dialog, text="Bot Euchre", font=("Arial", 20, "bold"), bg=setup_bg, fg=setup_gold).pack(pady=10)

        ai_choices = active_profile_choice_labels()
        default_label = ai_choices[0]
        profile_description_var = tk.StringVar(
            value=AI_PROFILE_CHOICES[default_label])
        tk.Label(
            dialog, textvariable=profile_description_var,
            font=("Arial", 9, "italic"), bg=setup_bg, fg=setup_muted,
            wraplength=550, justify=tk.CENTER, height=2).pack(pady=(0, 5))
        
        frame = tk.Frame(dialog, bg=setup_panel, bd=0, padx=12, pady=8)
        frame.pack(pady=8)
        
        tk.Label(frame, text="Your Name (Human):", bg=setup_panel, fg=setup_text, font=("Arial", 12, "bold")).grid(row=0, column=0, padx=10, pady=5, sticky="e")
        ent_human = tk.Entry(frame, font=("Arial", 12), width=25, bg=setup_field, fg=setup_text, insertbackground=setup_text, relief=tk.FLAT, highlightthickness=1, highlightbackground="#465149", highlightcolor=setup_accent); ent_human.insert(0, self.settings_store.data["player_name"]); ent_human.grid(row=0, column=1, padx=10, pady=5)
        
        vars_ai = {}
        saved_players = self.settings_store.data.get("players", [])
        defaults = {
            seat: (saved_players[seat - 1] if len(saved_players) >= seat
                   and saved_players[seat - 1] in ai_choices
                   else default_label)
            for seat in (1, 2, 3)
        }
        labels = {1: "Left Opponent:", 2: "Your Partner:", 3: "Right Opponent:"}
        for i in [1, 2, 3]:
            tk.Label(frame, text=labels[i], bg=setup_panel, fg=setup_text, font=("Arial", 12, "bold")).grid(row=i, column=0, padx=10, pady=5, sticky="e")
            var = tk.StringVar(value=defaults[i])
            opt = tk.OptionMenu(frame, var, *ai_choices)
            style_option_menu(opt, 30); opt.grid(row=i, column=1, padx=10, pady=5)
            option_menu = opt["menu"]

            def show_hovered_profile(event=None, menu=option_menu):
                active_index = menu.index("active")
                if active_index is not None:
                    profile_description_var.set(
                        AI_PROFILE_CHOICES[ai_choices[int(active_index)]])

            option_menu.bind("<<MenuSelect>>", show_hovered_profile)
            var.trace_add(
                "write", lambda *_, selected=var: profile_description_var.set(
                    AI_PROFILE_CHOICES[selected.get()]))
            vars_ai[i] = var

        self.trainer_mode_var = tk.BooleanVar(
            value=bool(self.settings_store.data.get("trainer_mode", False)))
        make_setup_checkbutton("Enable Trainer Mode (Post-Hand Analysis)", self.trainer_mode_var)
        
        self.dark_mode_var = tk.BooleanVar(
            value=bool(self.settings_store.data.get("dark_mode", True)))
        make_setup_checkbutton("Enable Dark Mode (UI Theme)", self.dark_mode_var)

        neural_options = list(NEURAL_SEARCH_PRESETS)
        tk.Label(dialog, text="Hint & Autoplay Neural Search:", font=("Arial", 12, "bold"), bg=setup_bg, fg=setup_text).pack(pady=(10, 0))
        self.hint_search_var = tk.StringVar(
            value=self.settings_store.data.get("hint_search", "Balanced"))
        hint_search_dropdown = tk.OptionMenu(dialog, self.hint_search_var, *neural_options)
        style_option_menu(hint_search_dropdown, 20); hint_search_dropdown.pack(pady=2)

        tk.Label(dialog, text="Opponent & Partner Neural Search:", font=("Arial", 12, "bold"), bg=setup_bg, fg=setup_text).pack(pady=(8, 0))
        self.table_search_var = tk.StringVar(
            value=self.settings_store.data.get("table_search", "Balanced"))
        table_search_dropdown = tk.OptionMenu(dialog, self.table_search_var, *neural_options)
        style_option_menu(table_search_dropdown, 20); table_search_dropdown.pack(pady=2)

        tk.Label(dialog, text="Pure MCTS Search Power:", font=("Arial", 12, "bold"), bg=setup_bg, fg=setup_text).pack(pady=(8, 0))
        self.engine_speed_var = tk.StringVar(value=self.settings_store.data.get(
            "engine_speed", "Grandmaster (100,000 Iterations)"))
        options = ["Fast (20,000 Iterations)", "Grandmaster (100,000 Iterations)", "Workstation (1,000,000 Iterations - 8+ Cores)"]
        dropdown = tk.OptionMenu(dialog, self.engine_speed_var, *options)
        style_option_menu(dropdown, 40); dropdown.pack(pady=5)

        tk.Label(dialog, text="Game Mode / Training Drill:", font=("Arial", 12, "bold"), bg=setup_bg, fg=setup_text).pack(pady=(10, 0))
        self.drill_mode_var = tk.StringVar(
            value=self.settings_store.data.get("drill", "Standard Match"))
        drill_options = list(DRILL_DESCRIPTIONS)
        drill_description = tk.Label(
            dialog, text=DRILL_DESCRIPTIONS["Standard Match"],
            font=("Arial", 10, "italic"), bg=setup_bg, fg=setup_muted,
            wraplength=500, justify=tk.CENTER)
        drill_description.pack(pady=(3, 2))
        dropdown_drill = tk.OptionMenu(dialog, self.drill_mode_var, *drill_options)
        style_option_menu(dropdown_drill, 40); dropdown_drill.pack(pady=(2, 5))
        drill_menu = dropdown_drill["menu"]

        def show_hovered_drill(event=None):
            active_index = drill_menu.index("active")
            if active_index is not None:
                drill_description.config(
                    text=DRILL_DESCRIPTIONS[drill_options[int(active_index)]])

        drill_menu.bind("<<MenuSelect>>", show_hovered_drill)
        self.drill_mode_var.trace_add(
            "write", lambda *_: drill_description.config(
                text=DRILL_DESCRIPTIONS[self.drill_mode_var.get()]))

        preset_frame = tk.Frame(dialog, bg=setup_bg)
        preset_frame.pack(pady=(2, 0))
        preset_var = tk.StringVar(value="Load table preset...")
        preset_names = sorted(self.settings_store.data.get("presets", {}))
        preset_menu = tk.OptionMenu(
            preset_frame, preset_var,
            *(preset_names or ["No saved presets"]))
        style_option_menu(preset_menu, 20)
        preset_menu.pack(side=tk.LEFT, padx=4)

        def collect_setup_settings():
            return {
                "player_name": ent_human.get().strip() or "You",
                "trainer_mode": self.trainer_mode_var.get(),
                "dark_mode": self.dark_mode_var.get(),
                "hint_search": self.hint_search_var.get(),
                "table_search": self.table_search_var.get(),
                "engine_speed": self.engine_speed_var.get(),
                "drill": self.drill_mode_var.get(),
                "players": [vars_ai[seat].get() for seat in (1, 2, 3)],
            }

        def apply_preset(*_):
            preset = self.settings_store.data.get("presets", {}).get(
                preset_var.get())
            if not preset:
                return
            ent_human.delete(0, tk.END)
            ent_human.insert(0, preset.get("player_name", "You"))
            self.trainer_mode_var.set(preset.get("trainer_mode", False))
            self.dark_mode_var.set(preset.get("dark_mode", True))
            self.hint_search_var.set(preset.get("hint_search", "Balanced"))
            self.table_search_var.set(preset.get("table_search", "Balanced"))
            self.engine_speed_var.set(preset.get(
                "engine_speed", "Grandmaster (100,000 Iterations)"))
            self.drill_mode_var.set(preset.get("drill", "Standard Match"))
            for seat, profile in zip((1, 2, 3), preset.get("players", [])):
                if profile in ai_choices:
                    vars_ai[seat].set(profile)

        preset_var.trace_add("write", apply_preset)

        def save_preset():
            name = simpledialog.askstring(
                "Save Table Preset", "Preset name:", parent=dialog)
            if name and name.strip():
                self.settings_store.save_preset(
                    name.strip(), collect_setup_settings())
                messagebox.showinfo(
                    "Preset Saved", f"Saved preset '{name.strip()}'.",
                    parent=dialog)

        tk.Button(
            preset_frame, text="Save Preset", command=save_preset,
            bg=setup_field, fg=setup_text, activebackground="#3A433D",
            activeforeground="white", relief=tk.FLAT, padx=8).pack(
                side=tk.LEFT, padx=4)
            
        def submit_names(event=None):
            global PLAYER_NAMES
            PLAYER_NAMES[0] = ent_human.get().strip() or "You"
            
            d_count = 0; n_count = 0
            for i in [1, 2, 3]:
                sel = vars_ai[i].get()
                clean_name = sel.split(" (")[0]
                PLAYER_NAMES[i] = clean_name
                self.ai_profiles[str(i)] = clean_name
                
            if self.dark_mode_var.get():
                self.main_bg_color = "#222222" 
                self.dark_bg_color = "#111111" 
                self.coach_bg_color = "#1a1a1a" 
            else:
                self.main_bg_color = "#2E8B57" 
                self.dark_bg_color = "#1a5934" 
                self.coach_bg_color = "#223b2b" 
                    
            self.configure(bg=self.main_bg_color)
                
            self.autoplay_mode = False
            self.ai_profiles["0"] = "Human"
            self.active_drill = self.drill_mode_var.get()
            self.copycat_style_scores = {
                "Arbiter": 1.0, "Ironclad": 0.0, "Kyle": 0.0}

            current_settings = collect_setup_settings()
            current_settings.update({
                "favorites": self.settings_store.data.get("favorites", []),
                "large_cards": self.settings_store.data.get("large_cards", False),
                "high_contrast": self.settings_store.data.get("high_contrast", False),
                "reduced_motion": self.settings_store.data.get("reduced_motion", False),
                "presets": self.settings_store.data.get("presets", {}),
            })
            self.settings_store.data = current_settings
            self.settings_store.save()

            (self.hint_neural_play_iters,
             self.hint_neural_bid_rollouts,
             self.hint_neural_discard_determinizations) = NEURAL_SEARCH_PRESETS[self.hint_search_var.get()]
            (self.table_neural_play_iters,
             self.table_neural_bid_rollouts,
             self.table_neural_discard_determinizations) = NEURAL_SEARCH_PRESETS[self.table_search_var.get()]
            
            engine_choice = self.engine_speed_var.get()
            if "20,000" in engine_choice: target_iters = 20000; self.display_iters = "20k"
            elif "1,000,000" in engine_choice: target_iters = 1000000; self.display_iters = "1M"
            else: target_iters = 100000; self.display_iters = "100k"
                
            self.ai_model = ISMCTS_Multiprocessing_Agent(human_iters=target_iters)
            
            dialog.destroy(); self.deiconify(); self.setup_ui()
            if auto_claim_league:
                self.after(200, self._auto_claim_next_league_job)
            else:
                self.after(100, self.start_new_hand)

        btn_start = tk.Button(
            dialog, text="Deal the Cards", font=("Arial", 14, "bold"),
            bg=setup_accent, fg="#101410", activebackground="#8BDB9B",
            activeforeground="#101410", relief=tk.FLAT, bd=0,
            padx=24, pady=7, command=submit_names)
        btn_start.pack(pady=15)
        if os.environ.get("BOT_EUCHRE_AUTO_START_SETUP") == "1":
            self.after(250, submit_names)

    def _auto_claim_next_league_job(self):
        if self.human_league_game_active:
            return
        try:
            league, job = claim_league_job()
        except (OSError, TypeError, ValueError) as error:
            messagebox.showerror("League Claim Failed", str(error))
            return
        if job is None:
            messagebox.showinfo(
                "Balanced League", "No unclaimed league jobs remain.")
            return
        self._start_claimed_league_job(league, job)

    def set_autoplay_profile(self, profile_name):
        if (getattr(self, "human_league_game_active", False)
                and profile_name != "Off"):
            messagebox.showinfo(
                "Human League",
                "Autoplay is disabled during Human League games.")
            return
        was_autoplaying = self.autoplay_mode
        previous_profile = self.ai_profiles.get("0", "Human")
        if previous_profile != profile_name:
            self._invalidate_tasks()
        self.autoplay_mode = profile_name != "Off"
        self.ai_profiles["0"] = profile_name if self.autoplay_mode else "Human"
        if hasattr(self, "autoplay_menu_button"):
            label = profile_name if self.autoplay_mode else "Off"
            self.autoplay_menu_button.config(text=f"? Autoplay: {label}")
        self.update_scoreboard()
        self.render_human_hand()
        if self.autoplay_mode and not was_autoplaying:
            self._resume_current_autoplay_turn()

    def _resume_current_autoplay_turn(self):
        self.update_scoreboard()
        
        if self.game_state == "playing":
            expected_cards = 3 if self.is_loner else 4
            if self.team1_tricks + self.team2_tricks == 5:
                self._evaluate_hand()
            elif len(self.trick) == expected_cards:
                self._resolve_trick()
            else:
                self.play_ai_turns()
        elif self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0:
            self.process_bidding()
        elif self.game_state == "discarding" and self.dealer_idx == 0:
            self.process_discard()

    def stop_autoplay(self, event=None):
        if self.autoplay_mode:
            self.set_autoplay_profile("Off")
            messagebox.showinfo("Autoplay Disabled", "Autoplay has been disabled. You now have control.")
            
            if self.game_state == "playing" and self.current_turn == 0:
                self.lbl_action.config(text="Your turn!")
                self.render_human_hand()
            elif self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0:
                self.lbl_action.config(text="Your turn to bid.")
                self.render_bidding_ui(); self.render_human_hand()

    def on_closing(self):
        if self.ai_model and self.game_state != "main_menu":
            try:
                self._write_autosave()
            except OSError:
                pass
        self._invalidate_tasks()
        self._close_all_tool_windows()
        if self.ai_model: self.ai_model.shutdown()
        self.destroy() 

    def return_to_main_menu(self):
        if not messagebox.askyesno(
                "Return to Main Menu",
                "Abandon the current game and return to setup?\n\n"
                "The current autosave will be cleared."):
            return

        self._invalidate_tasks()
        self._clear_autosave()
        self._close_all_tool_windows()
        self.game_state = "main_menu"
        self.autoplay_mode = False
        self.human_league_game_active = False
        self.ai_profiles["0"] = "Human"
        for callback_id in self.tk.call("after", "info"):
            try:
                self.after_cancel(callback_id)
            except tk.TclError:
                pass
        if self.ai_model:
            self.ai_model.shutdown()
            self.ai_model = None
        for widget in self.winfo_children():
            widget.destroy()
        self.unbind("<Escape>")
        self.prompt_for_names()

    def _snapshot_for_journal(self):
        if not self.ai_model or not getattr(self, "hands", None):
            return None
        try:
            return self.ai_model.pack_ui_state(self)
        except Exception:
            return None

    def _record_session_event(self, event_type, details=None):
        self.session_journal.record(
            event_type, details, self._snapshot_for_journal())
        self._schedule_autosave()

    def export_session(self):
        filename = filedialog.asksaveasfilename(
            parent=self, title="Export Bot Euchre Session",
            defaultextension=".json",
            filetypes=[("Bot Euchre replay", "*.json"), ("All files", "*.*")])
        if not filename:
            return
        self.session_journal.export(filename, {
            "player": PLAYER_NAMES[0],
            "profiles": self.ai_profiles,
            "drill": self.active_drill,
        })
        messagebox.showinfo("Session Exported", f"Saved replay to:\n{filename}")

    def export_decision_audit(self):
        filename = filedialog.asksaveasfilename(
            parent=self, title="Export Decision Audit",
            defaultextension=".jsonl",
            filetypes=[("JSON Lines", "*.jsonl"), ("All files", "*.*")])
        if not filename:
            return
        self.session_journal.export_decision_audit(filename, {
            "player": PLAYER_NAMES[0], "profiles": self.ai_profiles,
            "hand_seed": self.current_hand_seed,
            "search_depth": {
                "play": self.table_neural_play_iters,
                "bid": self.table_neural_bid_rollouts,
                "discard": self.table_neural_discard_determinizations,
            },
        })
        messagebox.showinfo("Decision Audit", f"Saved audit to:\n{filename}")

    def _diagnostic_payload(self, error=None, context=None, traceback_text=None):
        return {
            "format": "bot-euchre-diagnostic-v1",
            "created_at": time.time(),
            "context": context,
            "error": str(error) if error else None,
            "traceback": traceback_text,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "device": self.cheems_device,
            "checkpoint_status": self.checkpoint_status,
            "search_timings": self.search_timings,
            "active_searches": self.active_searches,
            "task_generation": self.task_generation,
            "hand_seed": self.current_hand_seed,
            "state": self._snapshot_for_journal(),
        }

    def _write_diagnostic_bundle(self, filename=None, error=None, context=None,
                                 traceback_text=None):
        os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)
        if filename is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            filename = os.path.join(
                DIAGNOSTIC_DIR, f"bot-euchre-diagnostic-{stamp}-{time.time_ns()}.zip")
        payload = self._diagnostic_payload(error, context, traceback_text)
        session = {
            "format": "bot-euchre-session-v1",
            "started_at": self.session_journal.started_at,
            "events": self.session_journal.events[-200:],
        }
        with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "diagnostic.json",
                json.dumps(payload, indent=2, ensure_ascii=False))
            archive.writestr(
                "recent-session.json",
                json.dumps(session, indent=2, ensure_ascii=False))
        return filename

    def export_diagnostic_bundle(self):
        filename = filedialog.asksaveasfilename(
            parent=self, title="Export Diagnostic Bundle",
            defaultextension=".zip",
            filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")])
        if not filename:
            return
        self.last_diagnostic_path = self._write_diagnostic_bundle(filename)
        messagebox.showinfo(
            "Diagnostic Bundle", f"Saved diagnostic bundle to:\n{filename}")

    def show_decision_journal(self):
        dialog, created = self._new_tool_window(
            "journal", "Decision Journal & Timeline", "760x600")
        if not created:
            return
        tk.Label(
            dialog, text="Decision Journal", font=("Arial", 16, "bold"),
            bg=self.coach_bg_color, fg="white").pack(pady=10)
        tree = ttk.Treeview(
            dialog, columns=("time", "event", "details"), show="headings")
        tree.heading("time", text="Time")
        tree.heading("event", text="Event")
        tree.heading("details", text="Details")
        tree.column("time", width=70, anchor=tk.CENTER)
        tree.column("event", width=150)
        tree.column("details", width=500)
        for event in self.session_journal.events:
            details = ", ".join(
                f"{key}={value}" for key, value in event["details"].items())
            tree.insert("", tk.END, values=(
                f"{event['time']:.1f}s", event["type"], details))
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        tk.Button(
            dialog, text="Export Session", command=self.export_session,
            bg="#1E90FF", fg="white", font=("Arial", 10, "bold")).pack(
                pady=(0, 10))

    def show_help_guide(self, topic=None):
        dialog, created = self._new_tool_window(
            "help_guide", "Help & User Guide", "900x640")
        if not created:
            return
        tk.Label(
            dialog, text="Bot Euchre - Help & User Guide",
            font=("Arial", 16, "bold"), bg=self.coach_bg_color,
            fg="white").pack(pady=(10, 4))
        body = tk.Frame(dialog, bg=self.coach_bg_color)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 12))
        topics = tk.Listbox(
            body, width=32, bg=self.dark_bg_color, fg="white",
            selectbackground="#1E90FF", selectforeground="white",
            font=("Arial", 11), exportselection=False,
            highlightthickness=0, activestyle="none")
        topics.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        text_frame = tk.Frame(body, bg=self.coach_bg_color)
        text_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text = tk.Text(
            text_frame, bg=self.dark_bg_color, fg="white", wrap=tk.WORD,
            font=("Arial", 11), padx=14, pady=12, spacing3=4,
            state=tk.DISABLED, insertbackground="white")
        scrollbar = ttk.Scrollbar(
            text_frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text.tag_configure("title", font=("Arial", 14, "bold"), foreground="gold")
        for title, _body in HELP_TOPICS:
            topics.insert(tk.END, f"  {title}")

        def render(_event=None):
            selection = topics.curselection()
            if not selection:
                return
            title, content = HELP_TOPICS[selection[0]]
            text.config(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert("1.0", f"{title}\n\n", "title")
            text.insert(tk.END, content)
            text.config(state=tk.DISABLED)

        topics.bind("<<ListboxSelect>>", render)
        titles = [title for title, _ in HELP_TOPICS]
        topics.selection_set(titles.index(topic) if topic in titles else 0)
        render()

    def load_replay_viewer(self):
        filename = filedialog.askopenfilename(
            parent=self, title="Open Bot Euchre Replay",
            filetypes=[("Bot Euchre replay", "*.json"), ("All files", "*.*")])
        if not filename:
            return
        try:
            payload = load_versioned_mapping(filename, "bot-euchre-session")
            events = payload["events"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            messagebox.showerror("Replay Error", f"Could not load replay:\n{error}")
            return
        self._show_replay_events(events, os.path.basename(filename))

    def _show_replay_events(self, events, title="Session Replay"):
        key = f"replay_{time.time_ns()}"
        dialog, _ = self._new_tool_window(
            key, f"Replay Viewer - {title}", "760x620")
        index_var = tk.IntVar(value=0)
        heading = tk.Label(
            dialog, font=("Arial", 14, "bold"), bg=self.coach_bg_color,
            fg="white")
        heading.pack(pady=8)
        text = tk.Text(
            dialog, bg=self.dark_bg_color, fg="white", insertbackground="white",
            font=("Consolas", 10), wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        def render():
            if not events:
                heading.config(text="No recorded events")
                return
            index = max(0, min(index_var.get(), len(events) - 1))
            index_var.set(index)
            event = events[index]
            heading.config(text=(
                f"{index + 1}/{len(events)} - {event.get('type', 'event')} "
                f"at {event.get('time', 0):.1f}s"))
            state = event.get("state") or {}
            lines = [json.dumps(event.get("details", {}), ensure_ascii=False)]
            if state:
                lines.extend([
                    "",
                    f"Hand seed: {state.get('hand_seed')}",
                    f"Trump: {state.get('trump_suit')}  Caller: {state.get('caller_idx')}",
                    f"Score: {state.get('team1_tricks', 0)}-{state.get('team2_tricks', 0)} tricks",
                    f"Current turn: Seat {state.get('current_turn')}",
                    f"Trick: {state.get('trick', [])}",
                ])
                for seat, hand in enumerate(state.get("hands", [])):
                    lines.append(f"Seat {seat}: {hand}")
            text.config(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert("1.0", "\n".join(lines))
            text.config(state=tk.DISABLED)

        controls = tk.Frame(dialog, bg=self.coach_bg_color)
        controls.pack(pady=(0, 10))
        tk.Button(
            controls, text="? Previous",
            command=lambda: (index_var.set(index_var.get() - 1), render())).pack(
                side=tk.LEFT, padx=6)
        tk.Button(
            controls, text="Next ?",
            command=lambda: (index_var.set(index_var.get() + 1), render())).pack(
                side=tk.LEFT, padx=6)
        def replay_deal():
            if not events:
                return
            event = events[max(0, min(index_var.get(), len(events) - 1))]
            state = event.get("state") or {}
            seed = event.get("details", {}).get("seed", state.get("hand_seed"))
            if seed is None:
                messagebox.showinfo(
                    "Replay Deal", "This event predates reproducible deal seeds.",
                    parent=dialog)
                return
            if messagebox.askyesno(
                    "Replay Deal", f"Start a new hand from seed {seed}?",
                    parent=dialog):
                self._close_tool_window(key)
                self.start_new_hand(seed_override=seed)
        tk.Button(
            controls, text="Replay Deal", command=replay_deal,
            bg="#1E90FF", fg="white").pack(side=tk.LEFT, padx=6)
        def analyze_position():
            if not events:
                return
            event = events[max(0, min(index_var.get(), len(events) - 1))]
            state = event.get("state") or {}
            phase = state.get("game_state")
            player = (state.get("current_turn") if phase == "playing"
                      else state.get("bidding_player") if phase in {"bidding_r1", "bidding_r2"}
                      else state.get("dealer_idx") if phase == "discarding" else None)
            if player not in (0, 1, 2, 3):
                messagebox.showinfo(
                    "Replay Analysis", "Select a bidding, discard, or card-play event.",
                    parent=dialog)
                return
            heading.config(text="Analyzing alternate plays...")
            def work():
                if phase == "playing":
                    results = _mcts_core_worker(
                        state, player, max(100, self.hint_neural_play_iters))
                    ranked = [(str(card), visits, wins / visits if visits else 0.0)
                              for card, (visits, wins) in results.items()]
                    return "Alternate play ranking", sorted(
                        ranked, key=lambda item: item[1], reverse=True)
                brain = self._get_neural_brain(player) or self.cheems_brain
                if brain is None:
                    raise RuntimeError("No compatible neural brain is loaded")
                def nn_eval(tensor):
                    with torch.inference_mode():
                        policy, value = brain(tensor.unsqueeze(0).to(self.cheems_device))
                        return (F.softmax(policy, dim=1)[0].cpu().numpy(),
                                float(value[0][0].item()))
                hands = [[Card(rank, suit) for rank, suit in hand]
                         for hand in state.get("hands", [])]
                up_card_data = state.get("up_card")
                up_card = Card(*up_card_data) if up_card_data else None
                if phase in {"bidding_r1", "bidding_r2"} and up_card:
                    round_num = 1 if phase == "bidding_r1" else 2
                    passed = state.get("passed_seats")
                    if passed is None:
                        passed = auction_passed_seats(
                            state.get("dealer_idx", 0),
                            state.get("passed_count", 0))
                    visits, _ = run_bid_mcts(
                        hands[player], up_card, state.get("dealer_idx", 0), player,
                        round_num, passed, state.get("team1_score", 0),
                        state.get("team2_score", 0), nn_eval,
                        rollouts=max(40, self.hint_neural_bid_rollouts))
                    ranked = []
                    for action, share in visits.items():
                        suit, alone = bid_action_details(action)
                        label = "Pass" if suit is None else f"Call {suit}{' alone' if alone else ''}"
                        ranked.append((label, int(round(share * 1000)), share))
                    return "Alternate bid ranking", sorted(
                        ranked, key=lambda item: item[2], reverse=True)
                if phase == "discarding" and up_card:
                    scored = choose_dealer_discard(
                        hands[player], state.get("trump_suit"),
                        state.get("caller_idx", -1), state.get("is_loner", False),
                        up_card, player, state.get("team1_score", 0),
                        state.get("team2_score", 0), nn_eval,
                        determinizations=max(8, self.hint_neural_discard_determinizations),
                        return_ranked=True)
                    ranked = [(str(card), rank, score)
                              for rank, (card, score) in enumerate(scored, 1)]
                    return "Alternate discard ranking", ranked
                raise ValueError("The selected event has no analyzable decision state")
            def show(label, choices):
                heading.config(text=f"Replay Analysis - Seat {player}")
                text.config(state=tk.NORMAL)
                text.delete("1.0", tk.END)
                text.insert("1.0", f"{label}\n\n")
                for rank, (choice, evidence, value) in enumerate(choices, 1):
                    text.insert(tk.END, f"{rank}. {choice}: evidence={evidence}, value={value:+.3f}\n")
                text.config(state=tk.DISABLED)
            def on_error(error):
                heading.config(text="Replay Analysis failed")
                messagebox.showerror("Replay Analysis", str(error), parent=dialog)
            self._launch_search("replay analysis", work, show, on_error)
        tk.Button(
            controls, text="Analyze Position", command=analyze_position,
            bg="#59636B", fg="white").pack(side=tk.LEFT, padx=6)
        render()

    def show_confidence_calibration(self):
        existing = self.open_windows.get("confidence_calibration")
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        filename = filedialog.askopenfilename(
            parent=self, title="Open Session for Confidence Calibration",
            filetypes=[("Bot Euchre session", "*.json"), ("All files", "*.*")])
        if not filename:
            return
        try:
            payload = load_versioned_mapping(filename, "bot-euchre-session")
            report = confidence_calibration(payload.get("events", []))
        except (OSError, ValueError, TypeError) as error:
            messagebox.showerror("Calibration Error", str(error), parent=self)
            return
        dialog, _ = self._new_tool_window(
            "confidence_calibration", "Confidence Calibration", "760x720")
        ece = report["expected_calibration_error"]
        summary = (f"{report['samples']} scored recommendations | ECE: {ece:.3f}"
                   if ece is not None else "No completed hands with confidence data")
        tk.Label(dialog, text=summary, font=("Arial", 13, "bold"),
                 bg=self.coach_bg_color, fg="white").pack(pady=12)
        tree = ttk.Treeview(
            dialog, columns=("range", "count", "predicted", "observed", "gap"),
            show="headings")
        for column, title in (("range", "Confidence"), ("count", "Samples"),
                              ("predicted", "Mean confidence"),
                              ("observed", "Observed win rate"), ("gap", "Gap")):
            tree.heading(column, text=title)
        for item in report["bins"]:
            tree.insert("", tk.END, values=(
                f"{item['low']:.0%}-{item['high']:.0%}", item["count"],
                f"{item['predicted']:.1%}", f"{item['observed']:.1%}",
                f"{abs(item['predicted'] - item['observed']):.1%}"))
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        plot = tk.Canvas(
            dialog, height=250, bg=self.dark_bg_color,
            highlightthickness=0)
        plot.pack(fill=tk.X, padx=12, pady=(0, 8))

        def draw_reliability(_event=None):
            plot.delete("all")
            width = max(plot.winfo_width(), 300)
            height = max(plot.winfo_height(), 200)
            left, top, right, bottom = 52, 16, width - 18, height - 38
            plot.create_line(left, bottom, right, bottom, fill="white")
            plot.create_line(left, bottom, left, top, fill="white")
            plot.create_line(
                left, bottom, right, top, fill="#8A9298", dash=(5, 4))
            plot.create_text(
                (left + right) / 2, height - 12,
                text="Mean predicted confidence", fill="white")
            plot.create_text(
                14, (top + bottom) / 2, text="Observed\nwin rate",
                fill="white", anchor=tk.W)
            for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
                x = left + fraction * (right - left)
                y = bottom - fraction * (bottom - top)
                plot.create_line(x, bottom, x, bottom + 4, fill="white")
                plot.create_line(left - 4, y, left, y, fill="white")
                plot.create_text(x, bottom + 15, text=f"{fraction:.0%}", fill="white")
                plot.create_text(left - 8, y, text=f"{fraction:.0%}",
                                 fill="white", anchor=tk.E)
            points = []
            for item in report["bins"]:
                x = left + item["predicted"] * (right - left)
                y = bottom - item["observed"] * (bottom - top)
                points.extend((x, y))
                radius = max(4, min(10, 3 + math.sqrt(item["count"])))
                plot.create_oval(
                    x - radius, y - radius, x + radius, y + radius,
                    fill="#E4C66A", outline="white", width=1)
                plot.create_text(
                    x, y - radius - 8, text=str(item["count"]),
                    fill="white")
            if len(points) >= 4:
                plot.create_line(*points, fill="#78D6A3", width=2)

        plot.bind("<Configure>", draw_reliability)
        draw_reliability()
        tk.Button(
            dialog, text="Help", bg="#59636B", fg="white",
            command=lambda: self.show_help_guide("Confidence Calibration")).pack(
                pady=(0, 10))

    def show_elo_leaderboard(self):
        dialog, created = self._new_tool_window(
            "elo_leaderboard", "Profile Elo Leaderboard", "1080x680")
        if not created:
            return
        active_id = self.settings_store.data.get("elo_season_id", "legacy")
        seasons = load_elo_seasons(TOURNAMENT_HISTORY_PATH)
        if not any(item["season_id"] == active_id for item in seasons):
            seasons.insert(0, {
                "season_id": active_id,
                "season_name": self.settings_store.data.get(
                    "elo_season_name", active_id),
                "started_at": 0.0,
            })
        labels = {
            f"{item['season_name']} [{item['season_id'][:12]}]": item
            for item in seasons
        }
        selected_label = next(
            label for label, item in labels.items()
            if item["season_id"] == active_id)
        season_var = tk.StringVar(value=selected_label)
        heading_var = tk.StringVar()
        tk.Label(
            dialog, textvariable=heading_var, font=("Arial", 13, "bold"),
            bg=self.coach_bg_color, fg="white").pack(pady=(10, 4))
        selector = ttk.Combobox(
            dialog, textvariable=season_var, values=list(labels),
            state="readonly", width=46)
        selector.pack(pady=(0, 8))
        tree = ttk.Treeview(
            dialog,
            columns=("rank", "profile", "rating", "record", "win_rate",
                     "win_ci", "schedule", "uncertainty", "status"),
            show="headings")
        for column, title, width in [
                ("rank", "Rank", 50), ("profile", "Profile / Checkpoint", 240),
                ("rating", "Elo", 65), ("record", "W-L", 65),
                ("win_rate", "Win %", 70), ("win_ci", "95% Win CI", 115),
                ("schedule", "SoS", 75),
                ("uncertainty", "�", 65), ("status", "Status", 100)]:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        evidence_var = tk.StringVar(value="Select a profile for head-to-head results.")
        tk.Label(
            dialog, textvariable=evidence_var, bg=self.coach_bg_color,
            fg="#E4C66A", wraplength=1040, justify=tk.LEFT).pack(
                fill=tk.X, padx=14, pady=(0, 8))
        displayed_standings = {}

        def selected_season():
            return labels[season_var.get()]

        def render_season(_event=None):
            season = selected_season()
            standings = load_elo_standings(
                TOURNAMENT_HISTORY_PATH, season["season_id"])
            displayed_standings.clear()
            displayed_standings.update(standings)
            active = " (Active)" if season["season_id"] == self.settings_store.data.get(
                "elo_season_id", "legacy") else " (Archived)"
            heading_var.set(f"Season: {season['season_name']}{active}")
            tree.delete(*tree.get_children())
            for rank, entry in enumerate(sorted(
                    standings.values(),
                    key=lambda item: item["rating"], reverse=True), 1):
                low, high = entry["win_rate_95ci"]
                tree.insert("", tk.END, iid=entry["identity"], values=(
                    rank, f"{entry['profile']} [{entry['fingerprint'][:8]}]",
                    f"{entry['rating']:.1f}",
                    f"{entry['wins']}-{entry['losses']}",
                    f"{entry['win_rate'] * 100:.1f}%",
                    f"{low * 100:.1f}-{high * 100:.1f}%",
                    f"{entry['schedule_strength']:.1f}",
                    f"{entry['uncertainty']:.0f}",
                    "Provisional" if entry["provisional"] else "Established"))
            if not standings:
                tree.insert("", tk.END, values=(
                    "-", "No games in this season", "1500", "0-0", "0.0%",
                    "0.0-100.0%", "-", "400", "Provisional"))

        def show_head_to_head(_event=None):
            selected = tree.selection()
            if not selected or selected[0] not in displayed_standings:
                return
            entry = displayed_standings[selected[0]]
            parts = []
            for opponent, record in sorted(
                    entry["head_to_head"].items(),
                    key=lambda item: displayed_standings.get(
                        item[0], {}).get("rating", 1500.0), reverse=True):
                opponent_entry = displayed_standings.get(opponent, {})
                label = opponent_entry.get("profile", opponent.split("@", 1)[0])
                rating = opponent_entry.get("rating", 1500.0)
                parts.append(
                    f"{label} ({rating:.0f}): {record['wins']}-{record['losses']}")
            evidence_var.set(
                f"Head-to-head for {entry['profile']}: "
                + ("  |  ".join(parts) if parts else "No games"))

        selector.bind("<<ComboboxSelected>>", render_season)
        tree.bind("<<TreeviewSelect>>", show_head_to_head)

        def new_season():
            name = simpledialog.askstring(
                "New Elo Season", "Season name:", parent=dialog)
            if not name:
                return
            if not messagebox.askyesno(
                    "Start New Season",
                    f"Archive the current ladder and start '{name}' at 1500?",
                    parent=dialog):
                return
            new_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            self._append_tournament_history({
                "type": "season_start", "timestamp": time.time(),
                "season_id": new_id, "season_name": name})
            self.settings_store.data.update({
                "elo_season_id": new_id, "elo_season_name": name})
            self.settings_store.save()
            self._close_tool_window("elo_leaderboard")
            self.show_elo_leaderboard()

        def activate_season():
            season = selected_season()
            if season["season_id"] == self.settings_store.data.get(
                    "elo_season_id", "legacy"):
                return
            if not messagebox.askyesno(
                    "Activate Elo Season",
                    f"Make '{season['season_name']}' the active ladder?",
                    parent=dialog):
                return
            self.settings_store.data.update({
                "elo_season_id": season["season_id"],
                "elo_season_name": season["season_name"],
            })
            self.settings_store.save()
            render_season()

        controls = tk.Frame(dialog, bg=self.coach_bg_color)
        controls.pack(pady=(0, 12))
        tk.Button(
            controls, text="Set Active", command=activate_season,
            bg="#1E90FF", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=5)
        tk.Button(
            controls, text="Start New Season", command=new_season,
            bg="#59636B", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=5)
        tk.Button(
            controls, text="Help", bg="#59636B", fg="white",
            command=lambda: self.show_help_guide("Tournament Mode & Elo")).pack(
                side=tk.LEFT, padx=5)
        render_season()

    def launch_headless_tournament_lab(self):
        script = os.path.join(SCRIPT_DIR, "adhoc_headless_evaluation_gui.py")
        if not os.path.exists(script):
            messagebox.showerror(
                "Headless Tournament", f"Tournament Lab is missing:\n{script}")
            return
        try:
            subprocess.Popen(
                [sys.executable, script], cwd=SCRIPT_DIR,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                               if IS_WINDOWS else 0),
                start_new_session=not IS_WINDOWS)
        except OSError as error:
            messagebox.showerror(
                "Headless Tournament", f"Could not start Tournament Lab:\n{error}")
            return
        messagebox.showinfo(
            "Headless Tournament",
            "Tournament Lab opened as a separate process. Its mirrored games run "
            "in background workers and do not depend on the current table.")

    def run_pre_release_self_test(self):
        script = os.path.join(SCRIPT_DIR, "pre_release_self_test.py")
        if not os.path.exists(script):
            messagebox.showerror("Pre-Release Self-Test", f"Missing self-test:\n{script}")
            return
        self.lbl_action.config(text="Running pre-release checks in background...")

        def calculate():
            return subprocess.run(
                [sys.executable, script], cwd=SCRIPT_DIR,
                capture_output=True, text=True)

        def finished(completed):
            self.lbl_action.config(text="")
            try:
                result = json.loads(completed.stdout)
            except (TypeError, ValueError):
                messagebox.showerror(
                    "Pre-Release Self-Test",
                    completed.stderr or completed.stdout or "Self-test produced no report.")
                return
            dialog, created = self._new_tool_window(
                "pre_release_self_test", "Pre-Release Self-Test", "760x600")
            if not created:
                return
            status_color = "#78D6A3" if result.get("ok") else "#FF8A80"
            tk.Label(
                dialog,
                text=(f"{'PASS' if result.get('ok') else 'FAIL'}  "
                      f"{result.get('passed', 0)} passed / {result.get('failed', 0)} failed  "
                      f"({result.get('elapsed_seconds', 0):.1f}s)"),
                bg=self.coach_bg_color, fg=status_color,
                font=("Arial", 15, "bold")).pack(pady=12)
            tree = ttk.Treeview(
                dialog, columns=("status", "check", "detail"), show="headings")
            for column, title, width in [
                    ("status", "Status", 70), ("check", "Check", 250),
                    ("detail", "Detail", 410)]:
                tree.heading(column, text=title)
                tree.column(column, width=width, anchor=tk.W)
            for check in result.get("checks", []):
                tree.insert("", tk.END, values=(
                    "PASS" if check.get("passed") else "FAIL",
                    check.get("name", ""), check.get("detail", "")))
            tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        self._launch_search(
            "pre-release self-test", calculate, finished,
            lambda error: (
                self.lbl_action.config(text=""),
                messagebox.showerror("Pre-Release Self-Test", str(error))))

    def show_benchmark_comparison(self):
        records = load_all_adhoc_records()
        if len(records) < 2:
            messagebox.showinfo(
                "Benchmark Comparison",
                "Run at least two Headless Tournament Lab benchmarks first.")
            return
        report = compare_benchmark_records(records[-2], records[-1])
        dialog, created = self._new_tool_window(
            "benchmark_comparison", "Latest Benchmark Comparison", "640x480")
        if not created:
            return
        text = tk.Text(dialog, wrap=tk.WORD, bg="#182126", fg="#E9EEF0",
                       font=("Consolas", 10), padx=14, pady=14)
        text.pack(fill=tk.BOTH, expand=True)
        lines = [
            f"{report['first_label']}  ->  {report['second_label']}", "",
            f"Same seed:   {'yes' if report['same_seed'] else 'NO'}",
            f"Same models: {'yes' if report['same_models'] else 'NO'}", "",
            "Metric deltas (latest minus previous):",
        ]
        lines.extend(
            f"  {metric}: {delta:+.6f}"
            for metric, delta in report["deltas"].items())
        text.insert("1.0", "\n".join(lines))
        text.configure(state=tk.DISABLED)

    def show_search_performance(self):
        dialog, created = self._new_tool_window(
            "search_performance", "Search Performance", "720x620")
        if not created:
            return
        grouped = {}
        for sample in self.search_timing_history:
            grouped.setdefault(sample["name"], []).append(sample["duration"] * 1000)
        chart = tk.Canvas(
            dialog, height=190, bg="#182126", highlightthickness=0)
        chart.pack(fill=tk.X, padx=12, pady=(12, 0))
        chart.create_text(
            12, 12, text="P95 latency (ms)", anchor=tk.NW,
            fill="#E9EEF0", font=("Arial", 11, "bold"))
        chart_rows = sorted(
            ((name, percentile(values, 95)) for name, values in grouped.items()),
            key=lambda item: item[1], reverse=True)[:5]
        chart_max = max((value for _, value in chart_rows), default=1.0)
        for row, (name, value) in enumerate(chart_rows):
            top = 42 + row * 27
            chart.create_text(
                12, top + 8, text=name[:25], anchor=tk.W,
                fill="#C9D3D7", font=("Arial", 9))
            width = 380 * value / chart_max
            chart.create_rectangle(
                205, top, 205 + width, top + 17, fill="#2D9C8C", outline="")
            chart.create_text(
                215 + width, top + 8, text=f"{value:.1f}", anchor=tk.W,
                fill="#E9EEF0", font=("Arial", 9))
        if not chart_rows:
            chart.create_text(
                12, 52, text="No searches recorded yet.", anchor=tk.NW,
                fill="#9EAAAF", font=("Arial", 10))
        tree = ttk.Treeview(
            dialog, columns=("search", "samples", "median", "p95", "max"),
            show="headings")
        for column, title, width in [
                ("search", "Search", 240), ("samples", "Samples", 80),
                ("median", "Median ms", 100), ("p95", "P95 ms", 100),
                ("max", "Max ms", 100)]:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=tk.CENTER)
        for name, durations in sorted(grouped.items()):
            tree.insert("", tk.END, values=(
                name, len(durations), f"{percentile(durations, 50):.1f}",
                f"{percentile(durations, 95):.1f}", f"{max(durations):.1f}"))
        if not grouped:
            tree.insert("", tk.END, values=("No searches recorded", 0, "-", "-", "-"))
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

    def show_seed_library(self):
        dialog, created = self._new_tool_window(
            "seed_library", "Named Seed Library", "680x520")
        if not created:
            return
        entries = load_seed_library()
        tree = ttk.Treeview(
            dialog, columns=("name", "seed", "notes"), show="headings")
        for column, title, width in [
                ("name", "Name", 180), ("seed", "Seed", 180),
                ("notes", "Notes", 270)]:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=tk.W if column != "seed" else tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        def refresh():
            tree.delete(*tree.get_children())
            for index, entry in enumerate(entries):
                tree.insert("", tk.END, iid=str(index), values=(
                    entry.get("name", "Unnamed"), entry.get("seed"),
                    entry.get("notes", "")))

        def add_current():
            seed = self.current_hand_seed
            if seed is None:
                messagebox.showinfo("Seed Library", "Deal a hand before saving its seed.", parent=dialog)
                return
            name = simpledialog.askstring("Save Seed", "Name:", parent=dialog)
            if not name:
                return
            notes = simpledialog.askstring("Save Seed", "Notes (optional):", parent=dialog) or ""
            entries.append({"name": name, "seed": int(seed), "notes": notes})
            save_seed_library(entries)
            refresh()

        def selected_index():
            selected = tree.selection()
            return int(selected[0]) if selected else None

        def replay_selected():
            index = selected_index()
            if index is None:
                return
            self.start_new_hand(seed_override=int(entries[index]["seed"]))

        def delete_selected():
            index = selected_index()
            if index is None:
                return
            del entries[index]
            save_seed_library(entries)
            refresh()

        controls = tk.Frame(dialog, bg=self.coach_bg_color)
        controls.pack(pady=(0, 12))
        for label, command in [
                ("Save Current", add_current), ("Replay", replay_selected),
                ("Delete", delete_selected)]:
            tk.Button(controls, text=label, command=command, width=14).pack(
                side=tk.LEFT, padx=5)
        refresh()

    def show_session_summary(self):
        journal = self.session_journal
        duration = max(0, int(time.time() - journal.started_at))
        consultations = sorted(
            journal.ai_consultations.items(), key=lambda item: item[1],
            reverse=True)
        favorite = consultations[0][0] if consultations else "None"
        messagebox.showinfo(
            "Bot Euchre Session Summary",
            f"Session time: {duration // 60}m {duration % 60}s\n"
            f"Hands completed: {journal.hands_completed}\n"
            f"Games completed: {journal.games_completed}\n"
            f"Recorded decisions: {len(journal.events)}\n"
            f"Most consulted AI: {favorite}")

    def _profile_display_label(self, profile_name):
        return next(
            (label for label in AI_PROFILE_CHOICES
             if label.split(" (")[0] == profile_name), profile_name)

    def show_profile_inspector(self):
        dialog, created = self._new_tool_window(
            "profile_inspector", "AI Profile Inspector", "620x500")
        if not created:
            return
        profiles = load_active_tournament_profiles()
        profile_var = tk.StringVar(value=profiles[0])
        menu = tk.OptionMenu(dialog, profile_var, *profiles)
        menu.config(font=("Arial", 11), width=30)
        menu.pack(pady=12)
        details = tk.Label(
            dialog, bg=self.dark_bg_color, fg="white", font=("Arial", 11),
            justify=tk.LEFT, anchor="nw", wraplength=550, padx=16, pady=16)
        details.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 16))

        def refresh(*_):
            profile = profile_var.get()
            label = self._profile_display_label(profile)
            description = AI_PROFILE_CHOICES.get(label, "")
            route = "Pure MCTS" if profile == "The MC" else (
                "Hybrid: conservative bid arbitration + Council-guided deep MCTS" if profile == "Iron Oracle" else
                "Hybrid: Ironclad contract + Council-guided deep MCTS" if profile == "Monte Prime" else
                "Hybrid: Ironclad contract + deep endgame MCTS" if profile == "Iron Solver" else
                "Hybrid: Sleuth score-aware policy with selective endgame deepening" if profile == "Iron Endgame Edge" else
                "Hybrid: Sleuth policy with selective endgame deepening" if profile == "Iron Clutch" else
                "Hybrid: Ironclad contract + guided MCTS play" if profile == "Iron Monte" else
                "Derived neural routing" if profile not in {
                    "Arbiter", "Ironclad", "Kyle"} else
                f"{profile} checkpoint")
            call_margin, loner_margin = self._bid_style_margins(
                0, 1 if self.game_state == "bidding_r1" else 2, profile)
            copycat = "\nCopycat scores: " + ", ".join(
                f"{name} {score:.1f}"
                for name, score in self.copycat_style_scores.items())
            details.config(text=(
                f"{profile}  [{self._profile_badge(profile)}]\n\n"
                f"{description}\n\n"
                f"Engine: {route}\n"
                f"Play search: {self.hint_neural_play_iters} iterations\n"
                f"Bid search: {self.hint_neural_bid_rollouts} rollouts\n"
                f"Discard search: {self.hint_neural_discard_determinizations} deals\n"
                f"Current call margin: {call_margin:+.2f}\n"
                f"Current loner margin: {loner_margin:+.2f}"
                f"{copycat if profile == 'Copycat' else ''}"))
        profile_var.trace_add("write", refresh)
        refresh()

    def show_ai_comparison(self):
        if self.autoplay_mode:
            messagebox.showinfo(
                "Compare AI", "Stop autoplay to compare a human-seat decision.")
            return
        dialog, created = self._new_tool_window(
            "ai_comparison", "Compare AI Recommendations", "660x680")
        if not created:
            return
        tk.Label(
            dialog, text="Select profiles to compare",
            font=("Arial", 15, "bold"), bg=self.coach_bg_color,
            fg="white").pack(pady=10)
        choices_frame = tk.Frame(dialog, bg=self.dark_bg_color)
        choices_frame.pack(fill=tk.X, padx=12)
        favorites = set(self.settings_store.data.get("favorites", []))
        variables = {}
        for index, label in enumerate(active_profile_choice_labels()):
            profile = label.split(" (")[0]
            variable = tk.BooleanVar(value=profile in favorites)
            variables[profile] = variable
            tk.Checkbutton(
                choices_frame,
                text=f"[{self._profile_badge(profile)}] {profile}",
                variable=variable,
                bg=self.dark_bg_color, fg="white", selectcolor="#333333",
                activebackground=self.dark_bg_color,
                activeforeground="white").grid(
                    row=index // 3, column=index % 3, sticky="w", padx=8, pady=3)
        output = tk.Text(
            dialog, height=15, bg=self.dark_bg_color, fg="white",
            font=("Consolas", 10), wrap=tk.WORD)
        output.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        running = [False]
        last_signature = [None]

        def run_comparison():
            selected = [name for name, variable in variables.items() if variable.get()]
            if not selected or running[0]:
                return
            running[0] = True
            self.active_searches += 1
            last_signature[0] = self._human_decision_signature()
            self.settings_store.data["favorites"] = selected[:8]
            self.settings_store.save()
            output.delete("1.0", tk.END)
            output.insert(tk.END, "Comparing...\n")
            tk.Button(dialog).focus_set()

            def worker():
                token = self._task_token()
                started = time.perf_counter()
                results = []
                with self.comparison_lock:
                    original_profile = self.ai_profiles.get("0", "Human")
                    try:
                        for profile in selected:
                            if token != self.task_generation:
                                self.after(0, lambda: (
                                    running.__setitem__(0, False),
                                    setattr(self, "active_searches", max(
                                        0, self.active_searches - 1))))
                                return
                            self.ai_profiles["0"] = profile
                            results.append((
                                profile, self._comparison_recommendation(profile)))
                    finally:
                        self.ai_profiles["0"] = original_profile
                def show_results():
                    running[0] = False
                    self.active_searches = max(0, self.active_searches - 1)
                    self.search_timings["comparison"] = (
                        time.perf_counter() - started)
                    if (token != self.task_generation
                            or not dialog.winfo_exists() or not results):
                        return
                    output.delete("1.0", tk.END)
                    recommendations = [result for _, result in results]
                    counts = {
                        recommendation: recommendations.count(recommendation)
                        for recommendation in set(recommendations)}
                    agreement = max(counts.values()) / len(results) * 100
                    output.insert(
                        tk.END,
                        f"Agreement meter: {agreement:.0f}% "
                        f"({max(counts.values())}/{len(results)})\n\n")
                    for profile, recommendation in results:
                        output.insert(tk.END, f"{profile:<20} {recommendation}\n")
                try:
                    self.after(0, show_results)
                except (tk.TclError, RuntimeError):
                    pass
            threading.Thread(target=worker, daemon=True).start()

        tk.Button(
            dialog, text="Compare Selected", command=run_comparison,
            bg="#71C784", fg="#101410", font=("Arial", 11, "bold")).pack(
                pady=(0, 12))

        def watch_decision():
            if not dialog.winfo_exists():
                return
            signature = self._human_decision_signature()
            if (signature is not None and signature != last_signature[0]
                    and not running[0]):
                run_comparison()
            dialog.after(750, watch_decision)
        watch_decision()

    def _human_decision_signature(self):
        is_human_decision = (
            self.game_state == "playing" and self.current_turn == 0
            or self.game_state in {"bidding_r1", "bidding_r2"}
            and self.bidding_player == 0
            or self.game_state == "discarding" and self.dealer_idx == 0)
        if self.autoplay_mode or not is_human_decision:
            return None
        return (
            self.game_state, self.current_turn, self.bidding_player,
            self.trump_suit, str(self.up_card),
            tuple(str(card) for card in self.hands[0]),
            tuple((seat, str(card)) for seat, card in self.trick),
            self.team1_score, self.team2_score)

    def _comparison_recommendation(self, profile):
        if self.game_state in {"bidding_r1", "bidding_r2"} and self.bidding_player == 0:
            round_num = 1 if self.game_state == "bidding_r1" else 2
            suits = ([self.up_card.suit] if round_num == 1 else
                     [suit for suit in SUITS_T if suit != self.up_card.suit])
            result = self._simulate_bidding(0, round_num, 1, 80)
            return f"{result[0]} {result[1] or ''} {'alone' if result[2] else ''}".strip()
        if self.game_state == "discarding" and self.dealer_idx == 0:
            index = (self._get_cheems_best_discard_index(0)
                     if profile in NEURAL_PROFILES else
                     self.get_smart_discard_index(0))
            return f"Discard {self.hands[0][index]}"
        if self.game_state == "playing" and self.current_turn == 0:
            if profile in NEURAL_PROFILES and profile not in HYBRID_MCTS_PROFILES:
                index, confidence = self.get_cheems_best_move(0)
                return f"Play {self.hands[0][index]} ({confidence:.1f}%)"
            index = self.ai_model.get_best_move(self, 0)
            return f"Play {self.hands[0][index]}"
        return "No human-seat decision is currently available."

    def show_accessibility_settings(self):
        dialog, created = self._new_tool_window(
            "accessibility", "Accessibility", "420x300")
        if not created:
            return
        large_var = tk.BooleanVar(
            value=self.settings_store.data.get("large_cards", False))
        contrast_var = tk.BooleanVar(
            value=self.settings_store.data.get("high_contrast", False))
        motion_var = tk.BooleanVar(
            value=self.settings_store.data.get("reduced_motion", False))
        for text, variable in [
                ("Large cards and controls", large_var),
                ("High-contrast table", contrast_var),
                ("Reduced animation delays", motion_var)]:
            tk.Checkbutton(
                dialog, text=text, variable=variable,
                bg=self.coach_bg_color, fg="white", selectcolor="#333333",
                activebackground=self.coach_bg_color,
                activeforeground="white", font=("Arial", 12)).pack(
                    anchor="w", padx=30, pady=10)

        def apply():
            self.settings_store.data.update({
                "large_cards": large_var.get(),
                "high_contrast": contrast_var.get(),
                "reduced_motion": motion_var.get(),
            })
            self.settings_store.save()
            if contrast_var.get():
                self.main_bg_color = "#000000"
                self.dark_bg_color = "#000000"
                self.coach_bg_color = "#101010"
            self.configure(bg=self.main_bg_color)
            self.render_human_hand()
            self._close_tool_window("accessibility")
        tk.Button(
            dialog, text="Apply", command=apply, bg="#71C784",
            fg="black", font=("Arial", 11, "bold")).pack(pady=14)

    def show_human_league(self):
        dialog, created = self._new_tool_window(
            "human_league", "Human League Season", "820x740")
        if not created:
            return
        state = self.human_league_state
        heading_var = tk.StringVar()
        summary_var = tk.StringVar()
        tk.Label(
            dialog, textvariable=heading_var, font=("Arial", 16, "bold"),
            bg=self.coach_bg_color, fg="white").pack(pady=(12, 4))
        tk.Label(
            dialog, textvariable=summary_var, bg=self.coach_bg_color,
            fg="#E4C66A", justify=tk.LEFT, wraplength=780).pack(
                fill=tk.X, padx=20, pady=(0, 8))

        standings_tree = ttk.Treeview(
            dialog,
            columns=("opponent", "record", "points", "differential"),
            show="headings", height=10)
        for column, title, width in [
                ("opponent", "Opponent Profile", 250),
                ("record", "Your W-L", 110), ("points", "PF-PA", 110),
                ("differential", "Point Diff", 100)]:
            standings_tree.heading(column, text=title)
            standings_tree.column(column, width=width, anchor=tk.CENTER)
        standings_tree.pack(fill=tk.BOTH, expand=True, padx=20, pady=8)

        def refresh_existing():
            current = self.human_league_state
            standings_tree.delete(*standings_tree.get_children())
            if not current:
                heading_var.set("Create Your Human League")
                summary_var.set(
                    "Choose one fixed AI partner and the opponent profiles your "
                    "team will face.")
                return
            heading_var.set(
                f"{current['name']} - {current['status'].title()}")
            game = human_league_current_game(current)
            regular_done = sum(
                item.get("status") == "completed"
                for item in current.get("schedule", []))
            details = (
                f"Team: {current['player_name']} + {current['partner']}\n"
                f"Regular season: {regular_done}/{len(current.get('schedule', []))}")
            if current.get("phase") == "playoff" and current.get("playoff"):
                playoff = current["playoff"]
                details += (
                    f"\nPlayoff gauntlet: round {playoff['current_index'] + 1}/"
                    f"{len(playoff['queue'])}; current series "
                    f"{playoff['human_wins']}-{playoff['opponent_wins']}"
                    f"\nPath: {' -> '.join(playoff['queue'])}")
            if game:
                details += f"\nNext opponent: {game['opponent']}"
            summary_var.set(details)
            for entry in human_league_standings(current).values():
                standings_tree.insert("", tk.END, values=(
                    entry["profile"],
                    f"{entry['human_wins']}-{entry['opponent_wins']}",
                    f"{entry['human_points']}-{entry['opponent_points']}",
                    f"{entry['human_points'] - entry['opponent_points']:+d}"))

        setup = tk.LabelFrame(
            dialog, text="New Season", bg=self.coach_bg_color, fg="white",
            padx=10, pady=8)
        setup.pack(fill=tk.X, padx=20, pady=8)
        name_var = tk.StringVar(value="My League Season")
        partner_var = tk.StringVar(value="Arbiter")
        games_var = tk.IntVar(value=2)
        playoff_var = tk.IntVar(value=4)
        seed_var = tk.StringVar(value=str(random.SystemRandom().randrange(
            0, 2 ** 63)))
        fields = tk.Frame(setup, bg=self.coach_bg_color)
        fields.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 14))
        active_profiles = load_active_tournament_profiles()
        for row, (label, variable) in enumerate([
                ("Season name", name_var), ("Partner", partner_var),
                ("Games/opponent", games_var),
                ("Playoff opponents", playoff_var), ("Seed", seed_var)]):
            tk.Label(
                fields, text=label, bg=self.coach_bg_color,
                fg="white").grid(row=row, column=0, sticky="w", pady=3)
            if row == 1:
                widget = ttk.Combobox(
                    fields, textvariable=variable,
                    values=active_profiles, state="readonly", width=24)
            elif row in (2, 3):
                widget = tk.Spinbox(
                    fields, from_=1 if row == 2 else 2,
                    to=10, textvariable=variable, width=10)
            else:
                widget = tk.Entry(fields, textvariable=variable, width=27)
            widget.grid(row=row, column=1, padx=6, pady=3)
        opponents = tk.Listbox(
            setup, selectmode=tk.MULTIPLE, exportselection=False, height=7)
        opponents.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for profile in active_profiles:
            opponents.insert(tk.END, profile)
            opponents.selection_set(tk.END)

        def create_season():
            selected = [opponents.get(index) for index in opponents.curselection()]
            if self.tournament_state:
                messagebox.showwarning(
                    "Tournament Running",
                    "Finish or cancel the automated tournament first.", parent=dialog)
                return
            if self.human_league_state and self.human_league_state.get(
                    "status") == "active" and not messagebox.askyesno(
                        "Replace Active Season",
                        "Replace the current Human League season?",
                        parent=dialog):
                return
            try:
                new_state = build_human_league_state(
                    name_var.get(), PLAYER_NAMES[0], partner_var.get(), selected,
                    games_var.get(), playoff_var.get(), int(seed_var.get()))
                new_state["search_config"] = {
                    "play_iterations": self.table_neural_play_iters,
                    "bid_rollouts": self.table_neural_bid_rollouts,
                    "discard_determinizations":
                        self.table_neural_discard_determinizations,
                }
                new_state["copycat_style_scores"] = copy.deepcopy(
                    self.copycat_style_scores)
                save_human_league_state(new_state)
            except (OSError, TypeError, ValueError) as error:
                messagebox.showerror(
                    "Human League Creation Failed", str(error), parent=dialog)
                return
            self.human_league_state = new_state
            refresh_existing()
            self._close_tool_window("human_league")
            self._prepare_human_league_game()

        def play_current():
            if not self.human_league_state or self.human_league_state.get(
                    "status") != "active":
                messagebox.showinfo(
                    "Human League", "No active Human League game remains.",
                    parent=dialog)
                return
            if self.tournament_state:
                messagebox.showwarning(
                    "Tournament Running",
                    "Finish or cancel the automated tournament first.", parent=dialog)
                return
            if not messagebox.askyesno(
                    "Play Scheduled Game",
                    "Start or restart the current scheduled game at 0-0?",
                    parent=dialog):
                return
            self._close_tool_window("human_league")
            self._prepare_human_league_game()

        buttons = tk.Frame(dialog, bg=self.coach_bg_color)
        buttons.pack(pady=(2, 12))
        tk.Button(
            buttons, text="Start New Season", command=create_season,
            bg="#1E90FF", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=8)
        tk.Button(
            buttons, text="Play / Resume Scheduled Game", command=play_current,
            bg="#71C784", fg="black", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=8)
        refresh_existing()

    def _action_delay_ms(self):
        return 0 if self.settings_store.data.get("reduced_motion", False) else CHEEMS_UI_ACTION_DELAY_MS

    def show_tournament_setup(self):
        dialog, created = self._new_tool_window(
            "tournament_setup", "Profile Tournament", "500x640")
        if not created:
            return
        profiles = load_active_tournament_profiles()
        profile_a = tk.StringVar(value=profiles[0])
        profile_b = tk.StringVar(
            value=profiles[1] if len(profiles) > 1 else profiles[0])
        games_var = tk.IntVar(value=3)
        randomize_var = tk.BooleanVar(value=False)
        randomize_each_game_var = tk.BooleanVar(value=False)
        benchmark_var = tk.BooleanVar(value=False)
        random_seed_var = tk.BooleanVar(value=True)
        seed_var = tk.StringVar(value="20260801")
        for label, variable in [("Team A", profile_a), ("Team B", profile_b)]:
            tk.Label(
                dialog, text=label, bg=self.coach_bg_color, fg="white",
                font=("Arial", 11, "bold")).pack(pady=(12, 2))
            tk.OptionMenu(dialog, variable, *profiles).pack()
        tk.Label(
            dialog, text="Games", bg=self.coach_bg_color, fg="white",
            font=("Arial", 11, "bold")).pack(pady=(12, 2))
        tk.Spinbox(dialog, from_=1, to=25, textvariable=games_var, width=8).pack()
        tk.Checkbutton(
            dialog, text="Randomize teams at start", variable=randomize_var,
            bg=self.coach_bg_color, fg="white", selectcolor="#333333",
            activebackground=self.coach_bg_color,
            activeforeground="white").pack(pady=(12, 0))
        tk.Checkbutton(
            dialog, text="Randomize after every game",
            variable=randomize_each_game_var,
            bg=self.coach_bg_color, fg="white", selectcolor="#333333",
            activebackground=self.coach_bg_color,
            activeforeground="white").pack(pady=(4, 0))
        tk.Checkbutton(
            dialog, text="Fixed-deal benchmark", variable=benchmark_var,
            bg=self.coach_bg_color, fg="white", selectcolor="#333333",
            activebackground=self.coach_bg_color,
            activeforeground="white").pack(pady=(6, 4))
        seed_entry = tk.Entry(
            dialog, textvariable=seed_var, width=18, state=tk.DISABLED)

        def update_seed_entry():
            seed_entry.config(
                state=tk.DISABLED if random_seed_var.get() else tk.NORMAL)

        tk.Checkbutton(
            dialog, text="Random seed", variable=random_seed_var,
            command=update_seed_entry,
            bg=self.coach_bg_color, fg="white", selectcolor="#333333",
            activebackground=self.coach_bg_color,
            activeforeground="white").pack(pady=(4, 2))
        tk.Label(
            dialog, text="Benchmark seed", bg=self.coach_bg_color,
            fg="white").pack()
        seed_entry.pack(pady=(2, 4))

        def start():
            if self.human_league_game_active:
                messagebox.showwarning(
                    "Human League Game Active",
                    "Return to the Human League season or Main Menu before "
                    "starting an automated tournament.", parent=dialog)
                return
            if randomize_var.get() or randomize_each_game_var.get():
                randomized_a, randomized_b = random_tournament_matchup(
                    profiles=profiles)
                profile_a.set(randomized_a)
                profile_b.set(randomized_b)
            if profile_a.get() == profile_b.get():
                messagebox.showwarning(
                    "Tournament", "Choose two different profiles.", parent=dialog)
                return
            try:
                seed_base = resolve_tournament_seed(
                    random_seed_var.get(), seed_var.get())
            except ValueError as error:
                messagebox.showwarning(
                    "Tournament", str(error),
                    parent=dialog)
                return
            profile_a_name = profile_a.get()
            profile_b_name = profile_b.get()
            checkpoint_paths = list(dict.fromkeys(
                profile_checkpoint_paths(profile_a_name)
                + profile_checkpoint_paths(profile_b_name)))
            self.tournament_state = {
                "profile_a": profile_a_name, "profile_b": profile_b_name,
                "games_total": games_var.get(), "games_done": 0,
                "wins_a": 0, "wins_b": 0,
                "points_a": 0, "points_b": 0, "hands": 0,
                "euchres_a": 0, "euchres_b": 0,
                "loners_a": 0, "loners_b": 0,
                "loner_sweeps_a": 0, "loner_sweeps_b": 0,
                "paused": False, "started_at": time.time(),
                "game_started_at": time.time(), "games": [],
                "randomize_each_game": randomize_each_game_var.get(),
                "benchmark": benchmark_var.get(),
                "random_seed": random_seed_var.get(),
                "seed_base": seed_base, "hand_seeds": [],
                "identity_a": profile_identity(profile_a.get()),
                "identity_b": profile_identity(profile_b.get()),
                "fingerprint_a": profile_fingerprint(profile_a.get()),
                "fingerprint_b": profile_fingerprint(profile_b.get()),
                "season_id": self.settings_store.data.get(
                    "elo_season_id", "legacy"),
                "season_name": self.settings_store.data.get(
                    "elo_season_name", "Legacy"),
                "provenance": build_provenance_manifest(
                    checkpoint_paths, configuration={
                        "profile_a": profile_a_name,
                        "profile_b": profile_b_name,
                        "games": games_var.get(),
                        "benchmark": benchmark_var.get(),
                        "random_seed": random_seed_var.get(),
                        "seed_base": seed_base,
                        "play_iterations": self.table_neural_play_iters,
                        "bid_rollouts": self.table_neural_bid_rollouts,
                        "discard_determinizations":
                            self.table_neural_discard_determinizations,
                    }, extra_environment={
                        "torch": torch.__version__ if HAS_TORCH else None,
                        "device": str(self.cheems_device),
                    }),
            }
            self.active_drill = "Standard Match"
            self.team1_score = self.team2_score = 0
            self.ai_profiles.update({
                "0": profile_a.get(), "2": profile_a.get(),
                "1": profile_b.get(), "3": profile_b.get(),
            })
            self.autoplay_mode = True
            self._refresh_seat_labels()
            self.autoplay_menu_button.config(text=f"? Tournament: {profile_a.get()}")
            self._record_session_event("tournament_start", self.tournament_state.copy())
            self._close_tool_window("tournament_setup")
            self.show_tournament_dashboard()
            self.start_new_hand()
        tk.Button(
            dialog, text="Start Tournament", command=start,
            bg="#FF8C00", fg="black", font=("Arial", 11, "bold")).pack(
                pady=18)
        tk.Button(
            dialog, text="Balanced League...",
            command=self.show_league_setup,
            bg="#1E90FF", fg="white", font=("Arial", 11, "bold")).pack(
                pady=(0, 14))

    def _human_league_changed_profiles(self):
        state = self.human_league_state
        if not state:
            return []
        members = [
            {"profile": state["partner"],
             "fingerprint": state["partner_fingerprint"]},
            *state.get("opponents", []),
        ]
        current = profile_fingerprints(
            [member["profile"] for member in members])
        return [
            member["profile"] for member in members
            if current[member["profile"]] != member["fingerprint"]]

    def _prepare_human_league_game(self):
        state = self.human_league_state
        game = human_league_current_game(state)
        if game is None:
            return False
        changed = self._human_league_changed_profiles()
        if changed:
            messagebox.showerror(
                "Human League Roster Changed",
                "These frozen profiles changed checkpoint identity: "
                + ", ".join(changed)
                + ". Restore those checkpoints or start a new season.")
            return False
        search_config = state.get("search_config", {})
        self.table_neural_play_iters = int(search_config.get(
            "play_iterations", self.table_neural_play_iters))
        self.table_neural_bid_rollouts = int(search_config.get(
            "bid_rollouts", self.table_neural_bid_rollouts))
        self.table_neural_discard_determinizations = int(search_config.get(
            "discard_determinizations",
            self.table_neural_discard_determinizations))
        self.copycat_style_scores = copy.deepcopy(
            state.get("copycat_style_scores", self.copycat_style_scores))
        self.tournament_state = None
        self.human_league_game_active = True
        self.active_drill = "Standard Match"
        self.autoplay_mode = False
        self.team1_score = self.team2_score = 0
        self.dealer_idx = int(game["starting_dealer"])
        self.ai_profiles.update({
            "0": "Human", "1": game["opponent"],
            "2": state["partner"], "3": game["opponent"],
        })
        self.autoplay_menu_button.config(text="? Autoplay: Off")
        self._refresh_seat_labels()
        self._record_session_event("human_league_game_start", {
            "league_id": state["league_id"], "phase": state["phase"],
            "opponent": game["opponent"], "partner": state["partner"],
            "seed_base": game["seed_base"],
        })
        self.start_new_hand()
        return True

    def _finish_human_league_game(self):
        state = self.human_league_state
        game = human_league_current_game(state)
        if game is None:
            return
        human_score, opponent_score = self.team1_score, self.team2_score
        result = record_human_league_game(
            state, human_score, opponent_score)
        result.update({
            "_schema": "bot-euchre-human-league-history",
            "_schema_version": DATA_SCHEMA_VERSION,
            "league_id": state["league_id"], "league_name": state["name"],
            "player_name": state["player_name"],
            "partner_fingerprint": state["partner_fingerprint"],
            "opponent_fingerprint": next(
                member["fingerprint"] for member in state["opponents"]
                if member["profile"] == result["opponent"]),
        })
        append_jsonl_record(HUMAN_LEAGUE_HISTORY_PATH, result)
        save_human_league_state(state)
        self.session_journal.games_completed += 1
        self.stats_tracker.record_event("games_completed")
        if result["human_won"]:
            self.stats_tracker.record_event("games_won")
        self.stats_tracker.apply_decay()
        self._record_session_event("human_league_game_complete", result)
        self.team1_score = self.team2_score = 0
        self.sandbox_mode = False
        if state["status"] == "champion":
            self.human_league_game_active = False
            messagebox.showinfo(
                "Human League Champion",
                f"{state['player_name']} and {state['partner']} won "
                f"{state['name']}!")
            self._refresh_seat_labels()
            return
        if state["status"] == "eliminated":
            self.human_league_game_active = False
            messagebox.showinfo(
                "Human League Complete",
                f"Your playoff run ended against {result['opponent']}. "
                "The completed season remains available in Human League.")
            self._refresh_seat_labels()
            return
        next_game = human_league_current_game(state)
        phase_label = (
            "Playoffs" if state["phase"] == "playoff" else "Regular Season")
        messagebox.showinfo(
            "Human League Result",
            f"{'Win' if result['human_won'] else 'Loss'} vs "
            f"{result['opponent']}: {human_score}-{opponent_score}\n\n"
            f"Next: {phase_label} vs {next_game['opponent']}")
        self._prepare_human_league_game()

    def show_balanced_league_standings(self):
        dialog, created = self._new_tool_window(
            "balanced_league_standings", "Balanced League Standings", "820x560")
        if not created:
            return
        title_var = tk.StringVar()
        tk.Label(
            dialog, textvariable=title_var, font=("Arial", 16, "bold"),
            bg=self.coach_bg_color, fg="white").pack(pady=(12, 4))
        tk.Label(
            dialog,
            text=("Game wins are listed first; point differential helps compare "
                  "profiles when mirrored results split. Elo remains win-based."),
            bg=self.coach_bg_color, fg="#E4C66A", wraplength=760).pack(
                pady=(0, 10))
        tree = ttk.Treeview(
            dialog,
            columns=("rank", "profile", "games", "wins", "losses",
                     "for", "against", "diff", "rate"),
            show="headings", height=18)
        for column, title, width, anchor in [
                ("rank", "#", 45, tk.CENTER),
                ("profile", "Profile", 210, tk.W),
                ("games", "GP", 55, tk.CENTER),
                ("wins", "W", 55, tk.CENTER),
                ("losses", "L", 55, tk.CENTER),
                ("for", "PF", 65, tk.CENTER),
                ("against", "PA", 65, tk.CENTER),
                ("diff", "+/-", 65, tk.CENTER),
                ("rate", "Win %", 75, tk.CENTER)]:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=anchor)
        tree.pack(fill=tk.BOTH, expand=True, padx=18)

        def refresh():
            tree.delete(*tree.get_children())
            state = load_league_state()
            if not state:
                title_var.set("No Active Balanced League")
                return
            title_var.set(state.get("name", "Balanced League"))
            standings = load_league_standings(state.get("league_id"))
            ordered = sorted(
                standings.values(),
                key=lambda entry: (
                    -entry["wins"], -entry["point_differential"],
                    -entry["points_for"], entry["profile"]))
            for rank, entry in enumerate(ordered, 1):
                tree.insert("", tk.END, values=(
                    rank, entry["profile"], entry["games"], entry["wins"],
                    entry["losses"], entry["points_for"],
                    entry["points_against"],
                    f"{entry['point_differential']:+d}",
                    f"{entry['win_rate']:.1%}"))

        controls = tk.Frame(dialog, bg=self.coach_bg_color)
        controls.pack(pady=12)
        tk.Button(
            controls, text="Refresh", command=refresh, bg="#59636B",
            fg="white", font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=6)
        tk.Button(
            controls, text="Close",
            command=lambda: self._close_tool_window(
                "balanced_league_standings"),
            bg="#59636B", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=6)
        refresh()

    def show_league_manager(self):
        dialog, created = self._new_tool_window(
            "league_manager", "Manage Balanced League", "880x600")
        if not created:
            return
        tk.Label(
            dialog, text="Current Shared League", font=("Arial", 16, "bold"),
            bg=self.coach_bg_color, fg="white").pack(pady=(12, 4))
        summary_var = tk.StringVar()
        tk.Label(
            dialog, textvariable=summary_var, bg=self.coach_bg_color,
            fg="#E4C66A", wraplength=830).pack(pady=(0, 10))
        tree = ttk.Treeview(
            dialog, columns=("round", "matchup", "status", "computer"),
            show="headings", height=19)
        for column, title, width in [
                ("round", "Round", 70), ("matchup", "Matchup", 370),
                ("status", "Status", 100), ("computer", "Computer", 210)]:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=tk.W)
        tree.pack(fill=tk.BOTH, expand=True, padx=18)

        def refresh():
            tree.delete(*tree.get_children())
            state = load_league_state()
            if not state:
                summary_var.set("No shared league exists.")
                return
            counts = {status: 0 for status in ("queued", "claimed", "completed")}
            for job in state.get("jobs", []):
                status = job.get("status", "queued")
                counts[status] = counts.get(status, 0) + 1
                tree.insert("", tk.END, iid=job.get("job_id"), values=(
                    job.get("round", ""),
                    f"{job.get('profile_a', '?')} vs {job.get('profile_b', '?')}",
                    status.title(), job.get("claimed_by", "")))
            summary_var.set(
                f"{state.get('name', 'Balanced League')} | Elo season: "
                f"{state.get('season_id', 'legacy')} | "
                f"{counts['completed']} complete, {counts['claimed']} running, "
                f"{counts['queued']} queued")

        def release_selected():
            selected_ids = list(tree.selection())
            state = load_league_state()
            jobs = {
                job.get("job_id"): job for job in state.get("jobs", [])
            } if state else {}
            claimed = [
                jobs[job_id] for job_id in selected_ids
                if job_id in jobs and jobs[job_id].get("status") == "claimed"]
            if not claimed:
                messagebox.showinfo(
                    "Release League Claims",
                    "Select one or more Claimed rows first.", parent=dialog)
                return
            descriptions = "\n".join(
                f"- {job.get('profile_a', '?')} vs {job.get('profile_b', '?')} "
                f"on {job.get('claimed_by', 'unknown computer')}"
                for job in claimed)
            if not messagebox.askyesno(
                    "Release Orphaned Claims",
                    "Release these claims back to the queue?\n\n"
                    f"{descriptions}\n\nOnly continue if those tournament processes "
                    "have been terminated. A process that is still running could "
                    "otherwise record a result after its claim is released.",
                    parent=dialog):
                return
            try:
                released = release_selected_league_claims(
                    [job["job_id"] for job in claimed])
            except (OSError, TypeError, ValueError) as error:
                messagebox.showerror(
                    "Claim Release Failed", str(error), parent=dialog)
                return
            messagebox.showinfo(
                "Claims Released",
                f"Released {released} claim{'s' if released != 1 else ''}.",
                parent=dialog)
            refresh()

        def retire_current():
            state = load_league_state()
            if not state:
                messagebox.showinfo(
                    "Manage Balanced League", "No shared league exists.",
                    parent=dialog)
                refresh()
                return
            claimed = [
                job for job in state.get("jobs", [])
                if job.get("status") == "claimed"]
            if claimed:
                nodes = ", ".join(sorted(set(
                    job.get("claimed_by", "unknown computer")
                    for job in claimed)))
                messagebox.showwarning(
                    "Running Matches Must Stop",
                    "Cancel the running league tournament on each listed computer "
                    f"before retiring this league:\n\n{nodes}", parent=dialog)
                return
            unfinished = sum(
                job.get("status") != "completed"
                for job in state.get("jobs", []))
            if not messagebox.askyesno(
                    "Retire Balanced League",
                    f"Retire '{state.get('name', 'Balanced League')}' and discard "
                    f"its {unfinished} unplayed jobs?\n\nCompleted games and Elo "
                    "records remain in tournament history. The schedule will be "
                    "archived, and a new league can then be created.", parent=dialog):
                return
            try:
                archive_path = retire_league()
            except (OSError, TypeError, ValueError) as error:
                messagebox.showerror(
                    "League Retirement Failed", str(error), parent=dialog)
                refresh()
                return
            messagebox.showinfo(
                "League Retired",
                f"The active league was archived as:\n{os.path.basename(archive_path)}",
                parent=dialog)
            self._close_tool_window("league_manager")
            self._close_tool_window("league_setup")
            self.show_league_setup()

        controls = tk.Frame(dialog, bg=self.coach_bg_color)
        controls.pack(pady=12)
        tk.Button(
            controls, text="Refresh", command=refresh, bg="#59636B",
            fg="white", font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=6)
        tk.Button(
            controls, text="Standings",
            command=self.show_balanced_league_standings,
            bg="#4D7C8A", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=6)
        tk.Button(
            controls, text="Release Selected Claims", command=release_selected,
            bg="#C28A32", fg="black", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=6)
        tk.Button(
            controls, text="Retire Current League", command=retire_current,
            bg="#B85C38", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=6)
        tk.Button(
            controls, text="Close",
            command=lambda: self._close_tool_window("league_manager"),
            bg="#59636B", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=6)
        refresh()

    def show_league_setup(self):
        dialog, created = self._new_tool_window(
            "league_setup", "Balanced Profile League", "620x680")
        if not created:
            return
        existing = load_league_state()
        name_var = tk.StringVar(value=(
            existing.get("name", "Balanced League")
            if existing else "Balanced League"))
        rounds_var = tk.IntVar(value=1)
        seed_var = tk.StringVar(value=str(random.SystemRandom().randrange(
            0, 2 ** 63)))
        status_var = tk.StringVar()
        tk.Label(
            dialog, text="Shared Balanced League", font=("Arial", 16, "bold"),
            bg=self.coach_bg_color, fg="white").pack(pady=(12, 6))
        form = tk.Frame(dialog, bg=self.coach_bg_color)
        form.pack(fill=tk.X, padx=24)
        for row, (label, variable) in enumerate([
                ("League name", name_var), ("Rounds per matchup", rounds_var),
                ("Schedule seed", seed_var)]):
            tk.Label(form, text=label, bg=self.coach_bg_color, fg="white").grid(
                row=row, column=0, sticky="w", padx=6, pady=5)
            if row == 1:
                widget = tk.Spinbox(
                    form, from_=1, to=20, textvariable=variable, width=12)
            else:
                widget = tk.Entry(form, textvariable=variable, width=34)
            widget.grid(row=row, column=1, sticky="ew", padx=6, pady=5)
        form.columnconfigure(1, weight=1)
        tk.Label(
            dialog, text="Frozen roster", bg=self.coach_bg_color, fg="white",
            font=("Arial", 11, "bold")).pack(pady=(12, 4))
        roster = tk.Listbox(
            dialog, selectmode=tk.MULTIPLE, exportselection=False, height=14)
        roster.pack(fill=tk.BOTH, expand=True, padx=24)
        for profile in load_active_tournament_profiles():
            roster.insert(tk.END, profile)
            roster.selection_set(tk.END)

        def refresh_status():
            state = load_league_state()
            if not state:
                status_var.set("No shared league exists.")
                return
            counts = {status: 0 for status in ("queued", "claimed", "completed")}
            for job in state.get("jobs", []):
                counts[job.get("status", "queued")] = (
                    counts.get(job.get("status", "queued"), 0) + 1)
            status_var.set(
                f"{state.get('name', 'League')}: {counts['completed']} complete, "
                f"{counts['claimed']} running, {counts['queued']} queued")

        def create_league():
            selected = [
                roster.get(index) for index in roster.curselection()]
            try:
                rounds = int(rounds_var.get())
                seed = int(seed_var.get())
                jobs = len(selected) * (len(selected) - 1) // 2 * rounds
                if not messagebox.askyesno(
                        "Create Shared League",
                        f"Create {jobs} mirrored matchup jobs ({jobs * 2} games)?\n\n"
                        "All selected checkpoint identities will be frozen.",
                        parent=dialog):
                    return
                state = build_league_state(
                    name_var.get(), self.settings_store.data.get(
                        "elo_season_id", "legacy"), selected, rounds, seed)
                state["search_config"] = {
                    "play_iterations": self.table_neural_play_iters,
                    "bid_rollouts": self.table_neural_bid_rollouts,
                    "discard_determinizations":
                        self.table_neural_discard_determinizations,
                }
                state["copycat_style_scores"] = copy.deepcopy(
                    self.copycat_style_scores)
                save_new_league(state)
            except (OSError, TypeError, ValueError) as error:
                messagebox.showerror("League Creation Failed", str(error), parent=dialog)
                return
            refresh_status()

        def claim_next():
            if self.human_league_game_active:
                messagebox.showwarning(
                    "Human League Game Active",
                    "Finish or leave the Human League game before claiming an "
                    "automated league job.", parent=dialog)
                return
            try:
                league, job = claim_league_job()
            except (OSError, TypeError, ValueError) as error:
                messagebox.showerror("League Claim Failed", str(error), parent=dialog)
                return
            if job is None:
                messagebox.showinfo(
                    "Balanced League", "No unclaimed league jobs remain.",
                    parent=dialog)
                refresh_status()
                return
            self._start_claimed_league_job(league, job)

        controls = tk.Frame(dialog, bg=self.coach_bg_color)
        controls.pack(pady=12)
        tk.Button(
            controls, text="Create New League", command=create_league,
            bg="#59636B", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=8)
        tk.Button(
            controls, text="Claim Next Match", command=claim_next,
            bg="#71C784", fg="black", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=8)
        tk.Button(
            controls, text="Manage League", command=self.show_league_manager,
            bg="#59636B", fg="white", font=("Arial", 10, "bold")).pack(
                side=tk.LEFT, padx=8)
        tk.Label(
            dialog, textvariable=status_var, bg=self.coach_bg_color,
            fg="#E4C66A", wraplength=560).pack(pady=(0, 12))
        refresh_status()

    def _start_claimed_league_job(self, league, job):
        search_config = league.get("search_config", {})
        self.table_neural_play_iters = int(search_config.get(
            "play_iterations", self.table_neural_play_iters))
        self.table_neural_bid_rollouts = int(search_config.get(
            "bid_rollouts", self.table_neural_bid_rollouts))
        self.table_neural_discard_determinizations = int(search_config.get(
            "discard_determinizations",
            self.table_neural_discard_determinizations))
        self.copycat_style_scores = copy.deepcopy(
            league.get("copycat_style_scores", self.copycat_style_scores))
        self.tournament_state = league_tournament_state(league, job)
        self.active_drill = "Standard Match"
        self.team1_score = self.team2_score = 0
        self.dealer_idx = self.tournament_state["starting_dealer"]
        self.ai_profiles.update({
            "0": job["profile_a"], "2": job["profile_a"],
            "1": job["profile_b"], "3": job["profile_b"],
        })
        self.autoplay_mode = True
        self._refresh_seat_labels()
        self.autoplay_menu_button.config(
            text=f"? League: {job['profile_a']}")
        self._record_session_event(
            "league_job_start", copy.deepcopy(self.tournament_state))
        self._close_tool_window("league_setup")
        self._close_tool_window("tournament_setup")
        self.show_tournament_dashboard()
        self.start_new_hand()

    def _finish_tournament_game(self):
        tournament = self.tournament_state
        team_a_won = self.team1_score >= 10
        tournament["games_done"] += 1
        tournament["wins_a" if team_a_won else "wins_b"] += 1
        tournament["points_a"] += self.team1_score
        tournament["points_b"] += self.team2_score
        game_record = {
            "game": tournament["games_done"],
            "profile_a": tournament["profile_a"],
            "profile_b": tournament["profile_b"],
            "winner": tournament["profile_a"] if team_a_won else tournament["profile_b"],
            "score_a": self.team1_score, "score_b": self.team2_score,
            "duration_seconds": round(
                time.time() - tournament.get("game_started_at", time.time()), 3),
            "hand_seeds": list(tournament.get("hand_seeds", [])),
            "identity_a": tournament.get("identity_a", tournament["profile_a"]),
            "identity_b": tournament.get("identity_b", tournament["profile_b"]),
            "fingerprint_a": tournament.get("fingerprint_a"),
            "fingerprint_b": tournament.get("fingerprint_b"),
            "season_id": tournament.get("season_id", "legacy"),
            "season_name": tournament.get("season_name", "Legacy"),
            "provenance": tournament.get("provenance"),
            "seat_assignment": {
                "team_a": [0, 2], "team_b": [1, 3]},
            "league_id": tournament.get("league_id"),
            "league_job_id": tournament.get("league_job_id"),
            "mirror_phase": tournament.get("mirror_phase"),
        }
        standings_before = load_elo_standings(
            TOURNAMENT_HISTORY_PATH, game_record["season_id"])
        ratings_before = {
            identity: entry["rating"]
            for identity, entry in standings_before.items()}
        identity_a = game_record["identity_a"]
        identity_b = game_record["identity_b"]
        ratings_after = update_elo_ratings(
            ratings_before, identity_a, identity_b,
            1.0 if team_a_won else 0.0)
        game_record["elo_before"] = {
            identity_a: ratings_before.get(identity_a, 1500.0),
            identity_b: ratings_before.get(identity_b, 1500.0),
        }
        game_record["elo_after"] = {
            identity_a: ratings_after[identity_a],
            identity_b: ratings_after[identity_b],
        }
        tournament["games"].append(game_record)
        self.session_journal.games_completed += 1
        self._record_session_event("tournament_game", game_record)
        self._append_tournament_history({
            "type": "game", "timestamp": time.time(),
            "profile_a": tournament["profile_a"],
            "profile_b": tournament["profile_b"], **game_record})
        if tournament["games_done"] >= tournament["games_total"]:
            completed = copy.deepcopy(tournament)
            completed.update({
                "type": "series", "timestamp": time.time(),
                "duration_seconds": round(
                    time.time() - tournament["started_at"], 3),
                "win_rate_a": tournament["wins_a"] / tournament["games_total"],
                "win_rate_b": tournament["wins_b"] / tournament["games_total"],
                "point_differential": tournament["points_a"] - tournament["points_b"],
                "seat_assignment": {"team_a": [0, 2], "team_b": [1, 3]},
                "search_depth": {
                    "play": self.table_neural_play_iters,
                    "bid": self.table_neural_bid_rollouts,
                    "discard": self.table_neural_discard_determinizations,
                },
                "elo_after": game_record["elo_after"],
            })
            self._append_tournament_history(completed)
            league_completion_ok = True
            if tournament.get("league_mode"):
                league_completion_ok = complete_league_job(
                    tournament["league_job_id"])
            matchup_summary = (
                f"Team A side: {tournament['wins_a']} wins\n"
                f"Team B side: {tournament['wins_b']} wins"
                if (tournament.get("randomize_each_game")
                    or tournament.get("league_mode")) else
                f"{tournament['profile_a']}: {tournament['wins_a']} wins\n"
                f"{tournament['profile_b']}: {tournament['wins_b']} wins")
            summary = (
                f"{matchup_summary}\n\n"
                f"Point differential: {completed['point_differential']:+d}\n"
                f"Saved to {os.path.basename(TOURNAMENT_HISTORY_PATH)}")
            league_mode = tournament.get("league_mode")
            self.tournament_state = None
            self._close_tool_window("tournament_dashboard")
            if league_mode:
                if not league_completion_ok:
                    self.set_autoplay_profile("Off")
                    self._refresh_seat_labels()
                    messagebox.showerror(
                        "League Claim Lost",
                        "The games were saved, but this node no longer owns the "
                        "shared league job. Automatic continuation was stopped.")
                    return
                try:
                    league, next_job = claim_league_job()
                except (OSError, TypeError, ValueError) as error:
                    self.set_autoplay_profile("Off")
                    self._refresh_seat_labels()
                    messagebox.showerror(
                        "League Continuation Failed", str(error))
                    return
                if next_job is not None:
                    self._start_claimed_league_job(league, next_job)
                    return
                self.set_autoplay_profile("Off")
                self._refresh_seat_labels()
                messagebox.showinfo(
                    "Balanced League", "No unclaimed league jobs remain.")
                return
            self.set_autoplay_profile("Off")
            self._refresh_seat_labels()
            messagebox.showinfo("Tournament Complete", summary)
            return
        self.team1_score = self.team2_score = 0
        tournament["game_started_at"] = time.time()
        tournament["hand_seeds"] = []
        if tournament.get("league_mode"):
            tournament["mirror_phase"] = 1
            for suffix in ("a", "b"):
                other = "b" if suffix == "a" else "a"
                tournament[f"_swap_{suffix}"] = tournament[f"profile_{other}"]
                tournament[f"_swap_identity_{suffix}"] = tournament[f"identity_{other}"]
                tournament[f"_swap_fingerprint_{suffix}"] = tournament[f"fingerprint_{other}"]
            tournament.update({
                "profile_a": tournament.pop("_swap_a"),
                "profile_b": tournament.pop("_swap_b"),
                "identity_a": tournament.pop("_swap_identity_a"),
                "identity_b": tournament.pop("_swap_identity_b"),
                "fingerprint_a": tournament.pop("_swap_fingerprint_a"),
                "fingerprint_b": tournament.pop("_swap_fingerprint_b"),
            })
            self.dealer_idx = tournament["starting_dealer"]
            self.ai_profiles.update({
                "0": tournament["profile_a"], "2": tournament["profile_a"],
                "1": tournament["profile_b"], "3": tournament["profile_b"],
            })
            self._refresh_seat_labels()
            self.autoplay_menu_button.config(
                text=f"? League: {tournament['profile_a']}")
        if tournament.get("randomize_each_game"):
            profile_a, profile_b = random_tournament_matchup(
                current=(tournament["profile_a"], tournament["profile_b"]))
            checkpoint_paths = list(dict.fromkeys(
                profile_checkpoint_paths(profile_a)
                + profile_checkpoint_paths(profile_b)))
            tournament.update({
                "profile_a": profile_a,
                "profile_b": profile_b,
                "identity_a": profile_identity(profile_a),
                "identity_b": profile_identity(profile_b),
                "fingerprint_a": profile_fingerprint(profile_a),
                "fingerprint_b": profile_fingerprint(profile_b),
                "provenance": build_provenance_manifest(
                    checkpoint_paths, configuration={
                        "profile_a": profile_a,
                        "profile_b": profile_b,
                        "game": tournament["games_done"] + 1,
                        "randomize_each_game": True,
                        "play_iterations": self.table_neural_play_iters,
                        "bid_rollouts": self.table_neural_bid_rollouts,
                        "discard_determinizations":
                            self.table_neural_discard_determinizations,
                    }, extra_environment={
                        "torch": torch.__version__ if HAS_TORCH else None,
                        "device": str(self.cheems_device),
                    }),
            })
            self.ai_profiles.update({
                "0": profile_a, "2": profile_a,
                "1": profile_b, "3": profile_b,
            })
            self._refresh_seat_labels()
            self.autoplay_menu_button.config(
                text=f"? Tournament: {profile_a}")
        self.start_new_hand()

    def _append_tournament_history(self, record):
        record = dict(record)
        record.update({
            "_schema": "bot-euchre-tournament-history",
            "_schema_version": DATA_SCHEMA_VERSION,
            "node_id": NODE_ID})
        append_jsonl_record(TOURNAMENT_HISTORY_PATH, record)

    def _tournament_paused(self):
        return bool(self.tournament_state and self.tournament_state.get("paused"))

    def show_tournament_dashboard(self):
        if not self.tournament_state:
            messagebox.showinfo("Tournament", "No tournament is currently running.")
            return
        dialog, created = self._new_tool_window(
            "tournament_dashboard", "Tournament Dashboard", "560x420")
        if not created:
            return
        title = tk.Label(
            dialog, font=("Arial", 16, "bold"), bg=self.coach_bg_color,
            fg="white")
        title.pack(pady=12)
        progress = ttk.Progressbar(dialog, mode="determinate", length=460)
        progress.pack(pady=8)
        details = tk.Label(
            dialog, bg=self.dark_bg_color, fg="white", font=("Consolas", 11),
            justify=tk.LEFT, anchor="nw", padx=18, pady=18)
        details.pack(fill=tk.BOTH, expand=True, padx=14, pady=8)
        controls = tk.Frame(dialog, bg=self.coach_bg_color)
        controls.pack(pady=10)

        def toggle_pause():
            tournament = self.tournament_state
            if not tournament:
                return
            tournament["paused"] = not tournament.get("paused", False)
            self._invalidate_tasks()
            self._schedule_autosave()
            if not tournament["paused"]:
                self._resume_current_autoplay_turn()

        def cancel():
            if not self.tournament_state or not messagebox.askyesno(
                    "Cancel Tournament",
                    "Cancel this tournament? Completed results will remain in history.",
                    parent=dialog):
                return
            cancelled = copy.deepcopy(self.tournament_state)
            if cancelled.get("league_mode"):
                release_league_job(cancelled["league_job_id"])
            cancelled.update({"type": "cancelled", "timestamp": time.time()})
            self._append_tournament_history(cancelled)
            self._record_session_event("tournament_cancelled", {
                "games_done": cancelled["games_done"]})
            self.tournament_state = None
            self._invalidate_tasks()
            self.set_autoplay_profile("Off")
            self._refresh_seat_labels()
            self._close_tool_window("tournament_dashboard")

        pause_button = tk.Button(
            controls, command=toggle_pause, bg="#FFB347", fg="black",
            font=("Arial", 10, "bold"), width=16)
        pause_button.pack(side=tk.LEFT, padx=8)
        tk.Button(
            controls, text="Cancel Tournament", command=cancel,
            bg="#8B0000", fg="white", font=("Arial", 10, "bold"),
            width=18).pack(side=tk.LEFT, padx=8)

        def refresh():
            if not dialog.winfo_exists():
                return
            tournament = self.tournament_state
            if not tournament:
                return
            if (tournament.get("league_mode")
                    and time.time() - tournament.get(
                        "last_claim_heartbeat", 0) >= 60):
                heartbeat_league_job(tournament["league_job_id"])
                tournament["last_claim_heartbeat"] = time.time()
            title.config(text=(
                f"{tournament['profile_a']} vs {tournament['profile_b']}"))
            progress.config(
                maximum=tournament["games_total"],
                value=tournament["games_done"])
            pause_button.config(text=(
                "Resume Tournament" if tournament.get("paused")
                else "Pause Tournament"))
            details.config(text=(
                (f"League: {tournament.get('league_name')} | "
                 f"Job: {tournament.get('league_job_id', '')[:8]} | "
                 f"Mirror game: {tournament.get('mirror_phase', 0) + 1}/2\n"
                 if tournament.get("league_mode") else "") +
                f"{'Team-side wins' if (tournament.get('randomize_each_game') or tournament.get('league_mode')) else 'Series'}: "
                f"{tournament['wins_a']} - {tournament['wins_b']}\n"
                f"Current game: {self.team1_score} - {self.team2_score}\n"
                f"Games completed: {tournament['games_done']} / {tournament['games_total']}\n"
                f"Games remaining: {tournament['games_total'] - tournament['games_done']}\n"
                f"Hands played: {tournament['hands']}\n"
                f"Euchres: {tournament['euchres_a']} - {tournament['euchres_b']}\n"
                f"Loner sweeps: {tournament['loner_sweeps_a']} - "
                f"{tournament['loner_sweeps_b']}\n"
                f"Deal mode: {'FIXED BENCHMARK' if tournament.get('benchmark') else 'RANDOM'}\n"
                f"Current hand seed: {self.current_hand_seed}\n"
                f"Status: {'PAUSED' if tournament.get('paused') else 'RUNNING'}"))
            dialog.after(500, refresh)
        refresh()

    def show_tournament_history(self):
        dialog, created = self._new_tool_window(
            "tournament_history", "Tournament History Explorer", "1100x680")
        if not created:
            return
        migrate_jsonl_schema(
            TOURNAMENT_HISTORY_PATH, "bot-euchre-tournament-history")
        migrate_jsonl_schema(
            ADHOC_HISTORY_PATH, "bot-euchre-adhoc-history")
        records = load_jsonl_records(TOURNAMENT_HISTORY_PATH)
        records.extend(load_all_adhoc_records())
        filters = tk.Frame(dialog, bg=self.coach_bg_color)
        filters.pack(fill=tk.X, padx=12, pady=(12, 4))
        type_var = tk.StringVar(value="All")
        query_var = tk.StringVar()
        season_var = tk.StringVar()
        seed_var = tk.StringVar()
        significance_var = tk.StringVar(value="All")
        from_var = tk.StringVar()
        to_var = tk.StringVar()
        kinds = sorted({
            record.get("type") or record.get("run_type", "unknown")
            for record in records})
        controls = [
            ("Type", ttk.Combobox(
                filters, textvariable=type_var, values=["All"] + kinds,
                state="readonly", width=20)),
            ("Profile / hash", ttk.Entry(filters, textvariable=query_var, width=18)),
            ("Season", ttk.Entry(filters, textvariable=season_var, width=12)),
            ("Seed", ttk.Entry(filters, textvariable=seed_var, width=12)),
            ("Significance", ttk.Combobox(
                filters, textvariable=significance_var,
                values=["All", "Significant", "Not significant"],
                state="readonly", width=15)),
            ("From YYYY-MM-DD", ttk.Entry(filters, textvariable=from_var, width=12)),
            ("To YYYY-MM-DD", ttk.Entry(filters, textvariable=to_var, width=12)),
        ]
        for index, (label, control) in enumerate(controls):
            tk.Label(
                filters, text=label, bg=self.coach_bg_color,
                fg="white").grid(row=0, column=index, sticky="w", padx=3)
            control.grid(row=1, column=index, sticky="ew", padx=3)
        tree = ttk.Treeview(
            dialog,
            columns=("date", "type", "matchup", "result", "season", "seed", "sig"),
            show="headings")
        for column, title, width in [
                ("date", "Date", 145), ("type", "Type", 155),
                ("matchup", "Matchup / Checkpoints", 310),
                ("result", "Result", 145), ("season", "Season", 100),
                ("seed", "Seed", 100), ("sig", "Significant", 90)]:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=tk.W)
        visible_records = []

        def record_values(record):
            timestamp = record.get("timestamp", "")
            stamp = (time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
                     if isinstance(timestamp, (int, float)) else str(timestamp))
            kind = record.get("type") or record.get("run_type", "unknown")
            profile_a = record.get("profile_a", record.get("model_a", "Cheems"))
            profile_b = record.get("profile_b", record.get("model_b", "Heuristic"))
            if kind == "series":
                result = f"{record.get('wins_a', 0)}-{record.get('wins_b', 0)}"
            elif "paired_mean_value_diff" in record:
                result = f"diff {record['paired_mean_value_diff']:+.4f}"
            else:
                result = str(record.get("winner", ""))
            significant = record.get("statistically_significant")
            return (
                stamp, kind, f"{profile_a} vs {profile_b}", result,
                record.get("season_name", record.get("season_id", "")),
                record.get("seed", record.get("seed_base", "")),
                "Yes" if significant is True else ("No" if significant is False else ""))

        def refresh(*_):
            visible_records[:] = filter_history_records(
                records, type_var.get(), query_var.get(), season_var.get(),
                seed_var.get(), significance_var.get(), from_var.get(), to_var.get())
            tree.delete(*tree.get_children())
            for index, record in enumerate(visible_records):
                tree.insert("", tk.END, iid=str(index), values=record_values(record))

        def sort_column(column, descending=False):
            rows = [(tree.set(item, column), item) for item in tree.get_children("")]
            rows.sort(reverse=descending)
            for position, (_, item) in enumerate(rows):
                tree.move(item, "", position)
            tree.heading(
                column, command=lambda: sort_column(column, not descending))

        for column in tree["columns"]:
            tree.heading(column, command=lambda name=column: sort_column(name))
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        buttons = tk.Frame(dialog, bg=self.coach_bg_color)
        buttons.pack(pady=(0, 12))

        def export_json():
            filename = filedialog.asksaveasfilename(
                parent=dialog, defaultextension=".json",
                filetypes=[("JSON", "*.json")])
            if filename:
                atomic_write_json(filename, visible_records)

        def export_csv():
            filename = filedialog.asksaveasfilename(
                parent=dialog, defaultextension=".csv",
                filetypes=[("CSV", "*.csv")])
            if not filename:
                return
            with open(filename, "w", newline="", encoding="utf-8-sig") as output:
                writer = csv.writer(output)
                writer.writerow(["Date", "Type", "Matchup", "Result", "Season", "Seed", "Significant"])
                writer.writerows(record_values(record) for record in visible_records)

        ttk.Button(buttons, text="Apply Filters", command=refresh).pack(side=tk.LEFT, padx=5)
        ttk.Button(buttons, text="Export JSON", command=export_json).pack(side=tk.LEFT, padx=5)
        ttk.Button(buttons, text="Export CSV", command=export_csv).pack(side=tk.LEFT, padx=5)
        refresh()

    def get_effective_suit(self, card):
        return effective_suit(card, self.trump_suit)

    def is_trump(self, card):
        if not self.trump_suit: return False
        return self.get_effective_suit(card) == self.trump_suit

    def get_legal_moves(self, hand):
        if not self.trick or not self.trump_suit: return list(range(len(hand)))
        led_suit = self.get_effective_suit(self.trick[0][1])
        legal_indices = [i for i, card in enumerate(hand) if self.get_effective_suit(card) == led_suit]
        if not legal_indices: return list(range(len(hand)))
        return legal_indices

    def get_deterministic_dump_move(self, player_idx, legal_moves):
        if not self.trick: return None 
        led_suit = self.get_effective_suit(self.trick[0][1]); highest_power = -1; winning_p_idx = -1
        rank_base_vals = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}
        
        def get_power(c):
            pwr = rank_base_vals[c.rank]; eff_s = self.get_effective_suit(c)
            if c.rank == 'J' and c.suit == self.trump_suit: pwr += 500
            elif c.rank == 'J' and c.suit == SAME_COLOR_T[self.trump_suit]: pwr += 400
            elif eff_s == self.trump_suit: pwr += 100
            elif eff_s == led_suit: pwr += 50
            else: pwr = 0
            return pwr

        for p_idx, c in self.trick:
            pwr = get_power(c)
            if pwr > highest_power: highest_power = pwr; winning_p_idx = p_idx
            
        hand = self.hands[player_idx]; can_win = False
        for i in legal_moves:
            if get_power(hand[i]) > highest_power: can_win = True; break
                
        partner_idx = (player_idx + 2) % 4
        is_partner_winning = (winning_p_idx == partner_idx)
        trick_target = 2 if self.is_loner else 3
        is_last_to_act = (len(self.trick) == trick_target)
        
        if is_partner_winning and is_last_to_act: can_win = False
        if not can_win: return min(legal_moves, key=lambda i: (get_power(hand[i]), rank_base_vals[hand[i].rank]))
        return None

    def get_smart_discard_index(self, player_idx):
        hand = self.hands[player_idx]
        trump = self.trump_suit
        
        suit_counts = {}
        for c in hand:
            eff_s = self.get_effective_suit(c)
            suit_counts[eff_s] = suit_counts.get(eff_s, 0) + 1
            
        def discard_score(card_idx):
            c = hand[card_idx]
            eff_s = self.get_effective_suit(c)
            if eff_s == trump:
                return -100 + RANKS_T.index(c.rank)
            
            score = 0
            if suit_counts[eff_s] == 1:
                score += 60  
            elif suit_counts[eff_s] == 2:
                score += 25  
                
            score += (5 - RANKS_T.index(c.rank)) * 3
            
            if c.rank == 'A':
                score -= 20
            elif c.rank == 'K':
                score -= 8
            return score

        return max(range(len(hand)), key=discard_score)

    def setup_ui(self):
        self.bind("<Escape>", self.stop_autoplay)
        self.bind("<KeyPress-a>", lambda _event: self.ask_ai("Arbiter"))
        self.bind("<KeyPress-j>", lambda _event: self.show_decision_journal())
        self.bind("<KeyPress-t>", lambda _event: self.show_ai_comparison())
        self.bind("<F1>", lambda _event: self.show_help_guide())
        self.info_frame = tk.Frame(self, bg=self.main_bg_color); self.info_frame.pack(side=tk.TOP, pady=5, fill=tk.X)
        self.lbl_trump = tk.Label(self.info_frame, text="TRUMP: Uncalled", font=("Arial", 16, "bold"), bg="white", fg="black", padx=10, pady=5); self.lbl_trump.pack(side=tk.LEFT, padx=10)

        self.btn_main_menu = tk.Button(self.info_frame, text="� Main Menu", font=("Arial", 10, "bold"), bg="#4F6D7A", fg="white", command=self.return_to_main_menu); self.btn_main_menu.pack(side=tk.RIGHT, padx=10)
        ToolTip(self.btn_main_menu, "Abandon this game and return to player, brain, search-depth, and drill setup.")
        self.btn_stats = tk.Button(self.info_frame, text="?? Stats & Coach", font=("Arial", 10, "bold"), bg="#1E90FF", fg="white", command=self.show_stats); self.btn_stats.pack(side=tk.RIGHT, padx=10)
        self.tools_menu_button = tk.Menubutton(
            self.info_frame, text="Tools", font=("Arial", 10, "bold"),
            bg="#59636B", fg="white", activebackground="#717D86",
            activeforeground="white", relief=tk.RAISED)
        tools_menu = tk.Menu(self.tools_menu_button, tearoff=False)
        tools_menu.add_command(
            label="Decision Journal & Timeline",
            command=self.show_decision_journal)
        tools_menu.add_command(label="Export Session...", command=self.export_session)
        tools_menu.add_command(
            label="Export Decision Audit...", command=self.export_decision_audit)
        tools_menu.add_command(label="Open Replay...", command=self.load_replay_viewer)
        tools_menu.add_command(
            label="Confidence Calibration...",
            command=self.show_confidence_calibration)
        tools_menu.add_separator()
        tools_menu.add_command(
            label="Compare AI Recommendations...",
            command=self.show_ai_comparison)
        tools_menu.add_command(
            label="Profile Inspector...", command=self.show_profile_inspector)
        tools_menu.add_command(
            label="Tournament Mode...", command=self.show_tournament_setup)
        tools_menu.add_command(
            label="Human League Season...", command=self.show_human_league)
        tools_menu.add_command(
            label="Headless Tournament Lab...",
            command=self.launch_headless_tournament_lab)
        tools_menu.add_command(
            label="Compare Latest Benchmarks...",
            command=self.show_benchmark_comparison)
        tools_menu.add_command(
            label="Search Performance...",
            command=self.show_search_performance)
        tools_menu.add_command(
            label="Named Seed Library...", command=self.show_seed_library)
        tools_menu.add_command(
            label="Tournament History...", command=self.show_tournament_history)
        tools_menu.add_command(
            label="Elo Leaderboard...", command=self.show_elo_leaderboard)
        tools_menu.add_separator()
        tools_menu.add_command(label="Model Health...", command=self.show_model_health)
        tools_menu.add_command(
            label="Export Diagnostic Bundle...",
            command=self.export_diagnostic_bundle)
        tools_menu.add_command(
            label="Run Pre-Release Self-Test...",
            command=self.run_pre_release_self_test)
        tools_menu.add_command(
            label="Settings Management...", command=self.show_settings_management)
        tools_menu.add_command(
            label="Accessibility...", command=self.show_accessibility_settings)
        tools_menu.add_command(
            label="Session Summary", command=self.show_session_summary)
        tools_menu.add_separator()
        tools_menu.add_command(
            label="Help & User Guide...", command=self.show_help_guide)
        tools_menu.add_separator()
        self.windows_menu = tk.Menu(tools_menu, tearoff=False)
        self.windows_menu.configure(postcommand=self._refresh_windows_menu)
        tools_menu.add_cascade(label="Open Windows", menu=self.windows_menu)
        self.tools_menu_button.config(menu=tools_menu)
        self.tools_menu_button.pack(side=tk.RIGHT, padx=4)
        self.autoplay_menu_button = tk.Menubutton(
            self.info_frame, text="? Autoplay: Off", font=("Arial", 10, "bold"),
            bg="#FF8C00", fg="black", activebackground="#FFB347",
            relief=tk.RAISED, direction="below")
        autoplay_menu = tk.Menu(self.autoplay_menu_button, tearoff=False)
        autoplay_menu.add_command(
            label="Off (Return Control to Human)",
            command=lambda: self.set_autoplay_profile("Off"))
        autoplay_menu.add_separator()
        for profile_label in AI_PROFILE_CHOICES:
            profile_name = profile_label.split(" (")[0]
            autoplay_menu.add_command(
                label=f"[{self._profile_badge(profile_name)}] {profile_label}",
                command=lambda name=profile_name: self.set_autoplay_profile(name))
        self.autoplay_menu_button.config(menu=autoplay_menu)
        self.autoplay_menu_button.pack(side=tk.RIGHT, padx=10)
        ToolTip(self.autoplay_menu_button, "Start, stop, or switch the AI controlling your seat at any point in the game.")
        
        self.score_frame = tk.Frame(self.info_frame, bg=self.dark_bg_color, bd=3, relief=tk.SUNKEN); self.score_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        self.lbl_game_score = tk.Label(self.score_frame, text=f"GAME: {self.get_player_display_name(0)} & {self.get_player_display_name(2)} 0 | {self.get_player_display_name(1)} & {self.get_player_display_name(3)} 0", bg=self.dark_bg_color, fg="gold", font=("Arial", 15, "bold")); self.lbl_game_score.pack(fill=tk.X, padx=16, pady=(5, 2))
        self.lbl_tricks = tk.Label(self.score_frame, text="TRICKS: Your Team 0 | Opponents 0", bg=self.dark_bg_color, fg="white", font=("Arial", 15)); self.lbl_tricks.pack(fill=tk.X, padx=16, pady=(2, 5))

        self.main_play_area = tk.Frame(self, bg=self.main_bg_color); self.main_play_area.pack(expand=True, fill=tk.BOTH)

        self.sidebar_frame = tk.Frame(self.main_play_area, bg=self.dark_bg_color, bd=3, relief=tk.RIDGE, width=420); self.sidebar_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(10, 0), pady=5); self.sidebar_frame.pack_propagate(False)
        tk.Label(self.sidebar_frame, text="DECK TRACKER", font=("Arial", 12, "bold"), bg=self.dark_bg_color, fg="gold").pack(pady=(10, 5))
        self.tracker_content_frame = tk.Frame(self.sidebar_frame, bg=self.dark_bg_color); self.tracker_content_frame.pack(fill=tk.BOTH, expand=True)
        self.lbl_tracker_waiting = tk.Label(self.tracker_content_frame, text="Awaiting Trump...", font=("Arial", 12, "italic"), bg=self.dark_bg_color, fg="gray"); self.lbl_tracker_waiting.pack(pady=20)
        self.boss_labels = {}

        self.table_frame = tk.Canvas(self.main_play_area, bg=self.dark_bg_color, bd=5, relief=tk.RIDGE)
        self.table_frame.pack(side=tk.RIGHT, expand=True, fill=tk.BOTH, padx=(10, 20), pady=5)
        self.table_frame.pack_propagate(False)
        self.table_frame.bind("<Configure>", self._draw_grid)
        
        self.seat_name_labels = {}
        p1_frame = tk.Frame(self.table_frame, bg=self.dark_bg_color); p1_frame.place(relx=0.02, rely=0.50, anchor=tk.W)
        self.seat_name_labels[1] = tk.Label(p1_frame, text=self.get_player_display_name(1), bg=self.dark_bg_color, fg="lightgray", font=("Arial", 12, "bold")); self.seat_name_labels[1].pack()
        self.lbl_p1_voids = tk.Label(p1_frame, text="", bg=self.dark_bg_color, fg="#ff6666", font=("Arial", 10, "italic")); self.lbl_p1_voids.pack()

        p2_frame = tk.Frame(self.table_frame, bg=self.dark_bg_color); p2_frame.place(relx=0.5, rely=0.02, anchor=tk.N)
        self.seat_name_labels[2] = tk.Label(p2_frame, text=self.get_player_display_name(2), bg=self.dark_bg_color, fg="lightgray", font=("Arial", 12, "bold")); self.seat_name_labels[2].pack()
        self.lbl_p2_voids = tk.Label(p2_frame, text="", bg=self.dark_bg_color, fg="#ff6666", font=("Arial", 10, "italic")); self.lbl_p2_voids.pack()

        p3_frame = tk.Frame(self.table_frame, bg=self.dark_bg_color); p3_frame.place(relx=0.98, rely=0.50, anchor=tk.E)
        self.seat_name_labels[3] = tk.Label(p3_frame, text=self.get_player_display_name(3), bg=self.dark_bg_color, fg="lightgray", font=("Arial", 12, "bold")); self.seat_name_labels[3].pack()
        self.lbl_p3_voids = tk.Label(p3_frame, text="", bg=self.dark_bg_color, fg="#ff6666", font=("Arial", 10, "italic")); self.lbl_p3_voids.pack()

        self.seat_name_labels[0] = tk.Label(self.table_frame, text=self.get_player_display_name(0), bg=self.dark_bg_color, fg="lightgray", font=("Arial", 12, "bold")); self.seat_name_labels[0].place(relx=0.5, rely=0.98, anchor=tk.S)

        self.lbl_upcard = tk.Label(self.table_frame, text="", font=("Arial", 32, "bold"), bg=self.dark_bg_color, fg="white", width=5, height=2, relief=tk.FLAT)
        self.lbl_action = tk.Label(self.table_frame, text="Dealing...", font=("Arial", 16, "italic"), bg=self.dark_bg_color, fg="lightgreen"); self.lbl_action.place(relx=0.02, rely=0.02, anchor=tk.NW)
        self.search_progress = ttk.Progressbar(
            self.table_frame, mode="indeterminate", length=180)

        self.dealer_canvas = tk.Canvas(self.table_frame, width=50, height=50, bg=self.dark_bg_color, highlightthickness=0)
        self.dealer_canvas.create_oval(2, 2, 48, 48, fill="white", outline="black", width=3); self.dealer_canvas.create_text(25, 25, text="D", font=("Arial", 20, "bold"), fill="black")

        self.status_frame = tk.Frame(self, bg="#101010", bd=1, relief=tk.SUNKEN)
        self.status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.lbl_status = tk.Label(
            self.status_frame, bg="#101010", fg="#D8E2DC",
            font=("Consolas", 9), anchor="w", padx=8, pady=3)
        self.lbl_status.pack(fill=tk.X)
        self.after(250, self._update_status_bar)

        self.human_frame = tk.Frame(self, bg=self.main_bg_color); self.human_frame.pack(side=tk.BOTTOM, pady=20)
        self.lbl_live_odds = tk.Label(self.human_frame, text="", font=("Arial", 12, "bold"), bg=self.main_bg_color, fg="#00BFFF"); self.lbl_live_odds.pack(pady=(0,2))
        self.lbl_hand_power = tk.Label(self.human_frame, text="", font=("Arial", 12, "italic", "bold"), bg=self.main_bg_color, fg="#00BFFF"); self.lbl_hand_power.pack(pady=(0,5))
        
        self.hand_buttons_frame = tk.Frame(self.human_frame, bg=self.main_bg_color); self.hand_buttons_frame.pack()
        self.controls_frame = tk.Frame(self.human_frame, bg=self.main_bg_color); self.controls_frame.pack(pady=5)
        
        self.ask_ai_button = tk.Menubutton(
            self.controls_frame, text="? Ask an AI", font=("Arial", 12, "bold"),
            bg="#98FB98", fg="black", activebackground="#B8FFB8",
            relief=tk.RAISED, direction="above")
        ask_ai_menu = tk.Menu(self.ask_ai_button, tearoff=False)
        for profile_label in AI_PROFILE_CHOICES:
            profile_name = profile_label.split(" (")[0]
            ask_ai_menu.add_command(
                label=f"[{self._profile_badge(profile_name)}] {profile_label}",
                command=lambda name=profile_name: self.ask_ai(name))
        self.ask_ai_button.config(menu=ask_ai_menu)
        ToolTip(self.ask_ai_button, "Choose any bot to analyze the current bid, discard, or card-play decision.")
        
        self.show_logic_var = tk.BooleanVar(value=False); self.chk_logic = tk.Checkbutton(self.controls_frame, text="??? Show AI Voids", variable=self.show_logic_var, font=("Arial", 10, "bold"), bg=self.main_bg_color, fg="white", selectcolor=self.dark_bg_color, command=self.update_table_graphics)
        self.bidding_buttons_frame = tk.Frame(self.human_frame, bg=self.main_bg_color); self.bidding_buttons_frame.pack(pady=10)
        self.loner_var = tk.BooleanVar()

    def _update_status_bar(self):
        if not hasattr(self, "lbl_status") or not self.lbl_status.winfo_exists():
            return
        phase = self.game_state.replace("_", " ").title()
        active_seat = (
            self.bidding_player if self.game_state in {"bidding_r1", "bidding_r2"}
            else self.current_turn)
        profile = self.ai_profiles.get(str(active_seat), "Human")
        mode_parts = []
        if self.autoplay_mode:
            mode_parts.append("Autoplay")
        if getattr(self, "trainer_mode_var", None) and self.trainer_mode_var.get():
            mode_parts.append("Trainer")
        if self.tournament_state:
            mode_parts.append("Tournament")
        mode = "+".join(mode_parts) or "Manual"
        self.lbl_status.config(text=(
            f"Phase: {phase}  |  Active: Seat {active_seat} / {profile} "
            f"[{self._profile_badge(profile)}]  |  Mode: {mode}  |  "
            f"Search: {self.table_neural_play_iters} play / "
            f"{self.table_neural_bid_rollouts} bid  |  Device: {self.cheems_device}  |  "
            f"Seed: {self.current_hand_seed or '-'}  |  Workers: {self.active_searches}"))
        self.after(500, self._update_status_bar)

    def _draw_grid(self, event=None):
        if not isinstance(self.table_frame, tk.Canvas): return
        self.table_frame.delete("grid_line")
        self.table_frame.config(bg=self.dark_bg_color)

    def update_boss_tracker(self):
        if not self.trump_suit:
            if self.tracked_trump_suit is not None:
                for widget in self.tracker_content_frame.winfo_children(): widget.destroy()
                tk.Label(self.tracker_content_frame, text="Awaiting Trump...", font=("Arial", 12, "italic"), bg=self.dark_bg_color, fg="gray").pack(pady=20)
                self.tracked_trump_suit = None; self.boss_labels = {}
            return

        if self.trump_suit != self.tracked_trump_suit:
            self.tracked_trump_suit = self.trump_suit
            for widget in self.tracker_content_frame.winfo_children(): widget.destroy()
            self.boss_labels = {}
            col_trump = tk.Frame(self.tracker_content_frame, bg=self.dark_bg_color); col_trump.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 5))
            col_off = tk.Frame(self.tracker_content_frame, bg=self.dark_bg_color); col_off.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 10))
            
            tk.Label(col_trump, text="Trump Cards", font=("Arial", 11, "underline", "bold"), bg=self.dark_bg_color, fg="white").pack(anchor="w", pady=(0,8))
            trump_list = [("Right Bower", Card('J', self.trump_suit)), ("Left Bower", Card('J', SAME_COLOR_T[self.trump_suit]))]
            for r in ['A', 'K', 'Q', '10', '9']: trump_list.append((f"{r}{self.trump_suit}", Card(r, self.trump_suit)))
            for name, card in trump_list:
                lbl = tk.Label(col_trump, text=f"{name}: Live", font=("Arial", 10, "bold"), bg=self.dark_bg_color, fg="yellow", anchor="w"); lbl.pack(fill=tk.X, pady=3)
                self.boss_labels[name] = (lbl, card)
                
            tk.Label(col_off, text="Off-Suit Power", font=("Arial", 11, "underline", "bold"), bg=self.dark_bg_color, fg="white").pack(anchor="w", pady=(0,8))
            off_list = []
            off_suits = [s for s in SUITS_T if s != self.trump_suit]
            for r in ['A', 'K', 'Q']:
                for s in off_suits: off_list.append((f"{r}{s}", Card(r, s)))
            for name, card in off_list:
                lbl = tk.Label(col_off, text=f"{name}: Live", font=("Arial", 10, "bold"), bg=self.dark_bg_color, fg="yellow", anchor="w"); lbl.pack(fill=tk.X, pady=3)
                self.boss_labels[name] = (lbl, card)

        all_played = self.played_cards + [c for _, c in self.trick]
        for name, (lbl, card) in self.boss_labels.items():
            if any(c == card for c in self.hands[0]): status = "Hand"; color = "lightgreen"
            elif any(c == card for c in all_played): status = "Played"; color = "#ff6666" 
            else: status = "Live"; color = "yellow"
            lbl.config(text=f"{name}: {status}", fg=color)

    def calculate_hand_power(self, hand, trump_suit):
        if not trump_suit: return 0.0, "Unknown"
        score = 0.0; suit_counts = {s: 0 for s in SUITS_T}
        for c in hand:
            eff_suit = trump_suit if (c.rank == 'J' and c.suit == SAME_COLOR_T[trump_suit]) else c.suit
            suit_counts[eff_suit] += 1
            if c.rank == 'J' and c.suit == trump_suit: score += 3.0
            elif c.rank == 'J' and c.suit == SAME_COLOR_T[trump_suit]: score += 2.5
            elif eff_suit == trump_suit and c.rank == 'A': score += 2.0
            elif eff_suit == trump_suit: score += 1.0
            elif eff_suit != trump_suit and c.rank == 'A': score += 1.5
            elif eff_suit != trump_suit and c.rank == 'K': score += 0.5
        if suit_counts[trump_suit] > 0:
            for s in SUITS_T:
                if s != trump_suit and suit_counts[s] == 0: score += 1.0
        if score < 4.0: desc = "Weak"
        elif score < 5.5: desc = "Average"
        elif score < 7.5: desc = "Strong"
        else: desc = "Monster!"
        return min(score, 10.0), desc

    def update_live_odds(self):
        if self.game_state != "playing" or self.autoplay_mode or self.current_turn != 0:
            self.lbl_live_odds.config(text=""); return
        self.lbl_live_odds.config(text="Live Odds: Calculating...", fg="yellow")
        
        pack = self.ai_model.pack_ui_state(self)

        def show_probability(confidence):
            if self.current_turn == 0 and self.game_state == "playing":
                self.lbl_live_odds.config(
                    text=f"Live Win Probability: {confidence:.1f}%", fg="#00BFFF")

        self._launch_search(
            "live odds",
            lambda: self.ai_model.get_best_move(
                self, 0, return_confidence=True, override_iters=25000,
                prepacked_state=pack)[1],
            show_probability)

    def show_stats(self):
        stats = self.stats_tracker.stats
        dialog, created = self._new_tool_window(
            "stats", "Player Statistics & Coach", "520x860")
        if not created:
            return

        def calc_pct(part, whole): return "0.0%" if whole == 0 else f"{(part / whole) * 100:.1f}%"
        games_comp = stats["games_completed"]; win_pct = calc_pct(stats["games_won"], games_comp); hands = stats["hands_played"]
        calls = stats["trump_calls"]; call_pct = calc_pct(calls, hands); euchred_pct = calc_pct(stats["got_euchred"], calls)
        avg_pts = f"{stats['total_points_earned'] / hands:.2f}" if hands > 0 else "0.00"

        tk.Label(dialog, text="Euchre Career Stats", font=("Arial", 16, "bold"), bg=self.coach_bg_color, fg="white").pack(pady=(15, 0), anchor="w", padx=20)
        frame = tk.Frame(dialog, bg=self.coach_bg_color); frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)

        def add_row(parent, label_text, value, indent=0, is_header=False, tooltip_text=None):
            row = tk.Frame(parent, bg=self.coach_bg_color); row.pack(fill=tk.X, pady=2)
            font = ("Arial", 11, "bold") if is_header else ("Arial", 11); fg = "lightgray" if not is_header else "white"
            prefix = " " * indent + ("> " if indent > 0 else "")
            lbl = tk.Label(row, text=f"{prefix}{label_text}", font=font, bg=self.coach_bg_color, fg=fg)
            lbl.pack(side=tk.LEFT)
            tk.Label(row, text=str(value), font=font, bg=self.coach_bg_color, fg="white").pack(side=tk.RIGHT)
            if tooltip_text:
                ToolTip(lbl, tooltip_text)

        add_row(frame, "Games completed", games_comp); add_row(frame, "Games won", stats["games_won"]); add_row(frame, "% Games Won", win_pct)
        tk.Frame(frame, height=1, bg="gray").pack(fill=tk.X, pady=5)
        add_row(frame, "Picked trump", call_pct); add_row(frame, "Got Euchre'd", euchred_pct, indent=2)
        tk.Frame(frame, height=1, bg="gray").pack(fill=tk.X, pady=5)
        
        tk.Label(frame, text="Play Skill (2v2) - Hover for Details", font=("Arial", 14, "bold"), bg=self.coach_bg_color, fg="white").pack(pady=(5, 5), anchor="w")
        add_row(frame, "Hands played", hands); add_row(frame, "Average points per hand", avg_pts)
        
        add_row(frame, "Partner Synergy Blunders", stats.get("synergy_blunders", 0), indent=2, 
                tooltip_text="Over-trumping or stepping on a trick your partner has already secured.")
        add_row(frame, "Loner Defense Blunders", stats.get("loner_defense_blunders", 0), indent=2,
                tooltip_text="Throwing away potential stoppers when defending a Loner.")
        add_row(frame, "General Play Blunders", stats.get("play_blunders", 0), indent=2,
                tooltip_text="Any card play that negatively impacts your mathematical win probability compared to the Grandmaster's recommended play.")
        
        tk.Frame(frame, height=1, bg="gray").pack(fill=tk.X, pady=5)
        tk.Label(frame, text="Advanced Analytics", font=("Arial", 14, "bold"), bg=self.coach_bg_color, fg="white").pack(pady=(5, 5), anchor="w")
        
        add_row(frame, "Catastrophic Loner Leaks", stats.get("catastrophic_loner_leaks", 0), indent=2,
                tooltip_text="Failing to 'donate' (safely call trump to sacrifice 2 pts) when opponents have 8 or 9 pts and a dangerous Jack is turned up.")
        add_row(frame, "Failed Trump Pulls", stats.get("failed_trump_pulls", 0), indent=2,
                tooltip_text="Failing to lead trump early to clear the board when your team called it.")
        add_row(frame, "Missed Void Discards", stats.get("missed_void_discards", 0), indent=2,
                tooltip_text="Discarding poorly as the dealer, leaving a doubleton suit instead of creating a clean void.")
        add_row(frame, "Phantom Boss Plays", stats.get("phantom_boss_plays", 0), indent=2,
                tooltip_text="Leading a high card when you hold a lower card of that suit AND the Ace hasn't been played yet.")
        add_row(frame, "Defensive Trump Leads", stats.get("defensive_trump_leads", 0), indent=2,
                tooltip_text="Leading a trump card when the opponents called trump.")
        add_row(frame, "Sub-optimal 'Next' Leads", stats.get("suboptimal_defensive_leads", 0), indent=2,
                tooltip_text="Leading the 'Next' suit (same color as trump) when opponents called trump.")
        add_row(frame, "Greedy Loner Penalties", stats.get("greedy_loners", 0), indent=2,
                tooltip_text="Going alone on a hand that lacks the necessary off-suit strength to sweep safely.")
        add_row(frame, "Stranded Aces", stats.get("stranded_aces", 0), indent=2,
                tooltip_text="Holding off-suit Aces until late in the hand, resulting in them getting trumped.")
        add_row(frame, "Trapped Left Bowers", stats.get("trapped_left_bowers", 0), indent=2,
                tooltip_text="Holding the Left Bower too long and being forced to surrender it to the Right Bower.")
        
        tk.Frame(frame, height=2, bg="#FF8C00").pack(fill=tk.X, pady=10)
        tk.Label(frame, text="?? Bidding Coach Analysis", font=("Arial", 14, "bold"), bg=self.coach_bg_color, fg="#FF8C00").pack(anchor="w")
        
        euchre_rate = (stats["got_euchred"] / calls) if calls > 0 else 0.0
        if euchre_rate > 0.20: msg = f"Coach's Note: You are getting euchred on {euchre_rate*100:.1f}% of your calls!"; c_color = "#ff6666" 
        elif stats.get("missed_next_calls", 0) > 0: msg = f"Coach's Note: You missed mandatory 'Next' calls! Defend Seat 1 aggressively."; c_color = "#ffcc00"
        elif stats["missed_calls"] > 0: msg = f"Coach's Note: You have passed on profitable hands!"; c_color = "#ffcc00" 
        else: msg = "Your bidding aggression perfectly matches GM models. Keep trusting the math!"; c_color = "lightgreen" 
            
        tk.Label(frame, text=msg, font=("Arial", 10, "italic"), bg=self.coach_bg_color, fg=c_color, justify=tk.LEFT, wraplength=430).pack(pady=5, anchor="w")
        btn_frame = tk.Frame(dialog, bg=self.coach_bg_color); btn_frame.pack(fill=tk.X, pady=10)
        def reset_stats():
            if messagebox.askyesno("Confirm Reset", "Are you sure?", parent=dialog):
                self.stats_tracker.clear_stats()
                self._close_tool_window("stats")
                self.show_stats()
        tk.Button(btn_frame, text="Close Dashboard", command=dialog.destroy, bg=self.main_bg_color, fg="white", font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=20)
        tk.Button(btn_frame, text="Reset Data", command=reset_stats, bg="#8B0000", fg="white", font=("Arial", 9, "bold")).pack(side=tk.RIGHT, padx=20)

    # ==========================================
    # GM & ARBITER AUTO-PLAY LOGIC
    # ==========================================
    def make_grandmaster_play(self):
        if self._handle_pre_play_states("Grandmaster"): return

        legal_moves = self.get_legal_moves(self.hands[0])
        if len(legal_moves) == 1: self._apply_human_play_with_analysis(legal_moves[0], legal_moves[0]); return
        dump_idx = self.get_deterministic_dump_move(0, legal_moves)
        if dump_idx is not None: self._apply_human_play_with_analysis(dump_idx, dump_idx); return

        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(text=f"Grandmaster taking the wheel ({self.display_iters} iters)..."); self.update_idletasks()
        self._launch_search(
            "Grandmaster play",
            lambda: self.ai_model.get_best_move(
                self, 0, return_confidence=False),
            self._apply_auto_play_result)

    def make_alpha_cheems_play(self):
        if self._handle_pre_play_states("Arbiter"): return
        
        legal_moves = self.get_legal_moves(self.hands[0])
        if len(legal_moves) == 1: self._apply_human_play_with_analysis(legal_moves[0], legal_moves[0]); return

        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(text=f"Arbiter building AlphaZero tree ({self.hint_neural_play_iters} iters)..."); self.update_idletasks()
        
        self._launch_search(
            "Arbiter play", lambda: self.get_cheems_best_move(0)[0],
            self._apply_cheems_play_result)

    def _apply_cheems_play_result(self, best_idx):
        self._set_controls_state(tk.NORMAL)
        self.lbl_action.config(text="")
        self._apply_human_play_with_analysis(best_idx, best_idx)

    def _handle_pre_play_states(self, agent_name, known_hands=None):
        if self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0:
            self._set_controls_state(tk.DISABLED)
            self.lbl_action.config(text=f"{agent_name} is auto-bidding..."); self.update_idletasks()
            if agent_name == "Grandmaster":
                self._launch_search(
                    "Grandmaster bid", self._bidding_auto_worker,
                    self._resolve_auto_bid)
            else:
                self._launch_search(
                    f"{agent_name} bid",
                    lambda: self._cheems_bidding_auto_worker(known_hands),
                    self._resolve_auto_bid)
            return True
        if self.game_state == "discarding" and self.dealer_idx == 0:
            if agent_name == "Arbiter":
                discard_idx = self._get_cheems_best_discard_index(0, known_hands=known_hands)
            else:
                discard_idx = self.get_smart_discard_index(0)
            self.human_discard_card(discard_idx); return True
        return False

    def _set_controls_state(self, state):
        self.ask_ai_button.config(state=state)
        if state == tk.DISABLED:
            self.search_progress.place(relx=0.02, rely=0.07, anchor=tk.NW)
            self.search_progress.start(12)
        else:
            self.search_progress.stop()
            self.search_progress.place_forget()
        for w in self.hand_buttons_frame.winfo_children():
            if isinstance(w, tk.Button): w.config(state=state)

    def _auto_play_worker(self):
        try:
            best_idx = self.ai_model.get_best_move(self, 0, return_confidence=False)
            self.after(0, self._apply_auto_play_result, best_idx)
        except Exception as e: print(f"Auto Play Error: {e}")

    def _apply_auto_play_result(self, best_idx):
        self._set_controls_state(tk.NORMAL); self.lbl_action.config(text="")
        self._apply_human_play_with_analysis(best_idx, best_idx)

    def _simulate_bidding(self, player_idx, round_num, num_polls, simulations_per_suit,
                          known_hands=None):
        suits_to_check = [self.up_card.suit] if round_num == 1 else [s for s in SUITS_T if s != self.up_card.suit]
        is_stuck = (round_num == 2 and self.passed_count == 3 and self.dealer_idx == player_idx)
        profile = self.ai_profiles.get(str(player_idx), "Human")
        
        if profile in NEURAL_PROFILES:
            return self._simulate_cheems_bidding(
                player_idx, round_num, suits_to_check, is_stuck,
                known_hands=known_hands)
            
        votes = {}
        for _ in range(num_polls):
            best_suit = None; best_expected_tricks = -1.0; best_is_loner = False
            for suit in suits_to_check:
                avg_tricks, avg_loner = self._run_bid_sim_raw(player_idx, suit, round_num, simulations_per_suit)
                if round_num == 1:
                    caller_team = 1 if player_idx in [0, 2] else 2; dealer_team = 1 if self.dealer_idx in [0, 2] else 2
                    if caller_team != dealer_team:
                        avg_tricks -= 0.15; avg_loner -= 0.15
                        if self.up_card.rank == 'J' and self.up_card.suit == suit: avg_tricks -= 0.4; avg_loner -= 0.4
                    elif caller_team == dealer_team and player_idx != self.dealer_idx:
                        avg_tricks += 0.15; avg_loner += 0.15
                        if self.up_card.rank == 'J' and self.up_card.suit == suit: avg_tricks += 0.4; avg_loner += 0.4
                elif round_num == 2 and suit == SAME_COLOR_T[self.up_card.suit]: avg_tricks += 0.3; avg_loner += 0.3
                
                if avg_tricks > best_expected_tricks:
                    best_expected_tricks = avg_tricks; best_suit = suit; best_is_loner = (avg_loner >= 4.6)

            if best_expected_tricks < 2.6 and not is_stuck: decision = ("Pass", None, False)
            else: decision = ("Call", best_suit, best_is_loner)
                
            vote_key = decision
            if vote_key not in votes: votes[vote_key] = {"count": 0, "expected_sum": 0.0}
            votes[vote_key]["count"] += 1; votes[vote_key]["expected_sum"] += best_expected_tricks

        best_vote_key = max(votes, key=lambda k: votes[k]["count"])
        confidence = (votes[best_vote_key]["count"] / float(num_polls)) * 100
        avg_expected = votes[best_vote_key]["expected_sum"] / votes[best_vote_key]["count"]
        action, suit, is_loner = best_vote_key
        return action, suit, is_loner, confidence, avg_expected, is_stuck

    def _cheems_nn_eval(self, tensor, player_idx=0):
        """nn_eval_fn closure for the shared auction machinery: one forward pass
        through the selected neural profile -> (33-dim softmax probs, scalar value)."""
        neural_brain = self._get_neural_brain(player_idx)
        if neural_brain is None:
            raise RuntimeError("Selected neural profile is unavailable")
        return self._eval_neural_brain(tensor, neural_brain)

    def _eval_neural_brain(self, tensor, neural_brain):
        with torch.no_grad():
            logits, value = neural_brain(tensor.to(self.cheems_device).unsqueeze(0))
        return F.softmax(logits[0], dim=0).cpu().numpy(), float(value.item())

    def _gui_passed_seats(self):
        """Seats that have passed so far in the CURRENT bidding round (bid order
        always starts left of the dealer)."""
        return auction_passed_seats(self.dealer_idx, self.passed_count)

    def _bid_style_margins(self, player_idx, round_num, profile):
        own_score, opponent_score = self._scores_for_player(player_idx)
        score_gap = own_score - opponent_score
        if profile == "Iron Sleuth":
            return -0.025, -0.01
        if profile == "Iron Closer":
            if score_gap >= 2 or own_score >= 8:
                return -0.03, -0.01
            if score_gap <= -2:
                return 0.05, 0.02
            return 0.01, 0.0
        if profile == "Iron Clutch":
            return -0.02, -0.008
        if profile == "Iron Endgame Edge":
            if score_gap >= 2 or own_score >= 8:
                return -0.035, -0.015
            if score_gap <= -2:
                return 0.04, 0.015
            return -0.02, -0.008
        return 0.0, 0.0

    def _simulate_cheems_bidding(self, player_idx, round_num, suits_to_check, is_stuck,
                                 known_hands=None, rollouts=None):
        neural_brain = self._get_neural_brain(player_idx)
        if not HAS_TORCH or neural_brain is None:
            return ("Pass" if not is_stuck else "Call", suits_to_check[0], False, 100.0, 2.0, is_stuck)
        if rollouts is None:
            rollouts = (self.hint_neural_bid_rollouts if player_idx == 0
                        else self.table_neural_bid_rollouts)
        profile = self.ai_profiles.get(str(player_idx), "Arbiter")
        if profile == "Unanimous Council":
            rollouts *= 2

        call_margin, loner_margin = self._bid_style_margins(
            player_idx, round_num, profile)

        # --- POLICY-HEAD BIDDING (July 2026 bidding overhaul) ---
        # Arbiter now bids with the same machinery it trains on: run_bid_mcts
        # runs a root PUCT search over the 9 bid actions, using the policy head's
        # bid logits as priors and the value head to score determinized auction
        # continuations. The most-visited action wins - identical to how bids are
        # selected during self-play generation.
        try:
            if profile == "Iron Oracle":
                passed_seats = self._gui_passed_seats()
                bid_tensor = encode_bid_state(
                    list(self.hands[player_idx]), player_idx, self.up_card,
                    self.dealer_idx, round_num, passed_seats,
                    self.team1_score, self.team2_score)
                policy_probs, _ = self._eval_neural_brain(
                    bid_tensor, neural_brain)
                visit_dict, root_q = run_bid_mcts(
                    list(self.hands[player_idx]), self.up_card, self.dealer_idx,
                    player_idx, round_num, passed_seats,
                    self.team1_score, self.team2_score,
                    lambda tensor: self._eval_neural_brain(tensor, neural_brain),
                    rollouts=max(rollouts * 3, 300),
                    known_hands=(self._get_neural_known_hands(player_idx)
                                 if known_hands is None else known_hands),
                    call_margin=call_margin, loner_margin=loner_margin)
                legal_actions = legal_bid_actions(
                    round_num, self.up_card.suit, is_stuck)
                best_action = choose_iron_oracle_bid(
                    legal_actions, policy_probs, visit_dict)
                confidence = visit_dict.get(best_action, 0.0) * 100.0
                est_points = root_q * 4.0
                if best_action == BID_PASS:
                    return ("Pass", suits_to_check[0], False, confidence,
                            est_points, is_stuck)
                suit, is_loner = bid_action_details(best_action)
                return ("Call", suit, is_loner, confidence, est_points, is_stuck)
            visit_dict, root_q = run_bid_mcts(
                list(self.hands[player_idx]), self.up_card, self.dealer_idx,
                player_idx, round_num, self._gui_passed_seats(),
                self.team1_score, self.team2_score,
                lambda tensor: self._cheems_nn_eval(tensor, player_idx),
                rollouts=rollouts,
                known_hands=(self._get_neural_known_hands(player_idx)
                             if known_hands is None else known_hands),
                call_margin=call_margin, loner_margin=loner_margin)
            ranked_actions = sorted(
                visit_dict, key=visit_dict.get, reverse=True)
            best_action = ranked_actions[0]
            if (profile == "Risk Manager" and len(ranked_actions) > 1
                    and visit_dict[best_action] - visit_dict[ranked_actions[1]] <= 0.05):
                def action_risk(action):
                    if action == BID_PASS:
                        return 0
                    return 2 if bid_action_details(action)[1] else 1
                best_action = min(ranked_actions[:2], key=action_risk)
            confidence = visit_dict[best_action] * 100.0
            est_points = root_q * 4.0  # value head is loner-aware scaled (caller_pts/4)
            if best_action == BID_PASS:
                return ("Pass", suits_to_check[0], False, confidence, est_points, is_stuck)
            suit, is_loner = bid_action_details(best_action)
            return ("Call", suit, is_loner, confidence, est_points, is_stuck)
        except Exception:
            return ("Pass" if not is_stuck else "Call", suits_to_check[0], False, 100.0, 2.0, is_stuck)

    def _run_bid_sim_raw(self, player_idx, suit, round_num, simulations_per_suit):
        total_tricks = 0; total_loner = 0
        for sim_idx in range(simulations_per_suit):
            sim_game = EuchreGameDummy(self)
            sim_game.trump_suit = suit; sim_game.caller_idx = player_idx
            known_cards = list(sim_game.hands[player_idx])
            if round_num == 1: known_cards.append(self.up_card)
                
            deck = [Card(r, s) for s in SUITS_T for r in RANKS_T]
            unknown_cards = [c for c in deck if not any(c == kc for kc in known_cards)]; random.shuffle(unknown_cards)
            for i in range(4):
                if i == player_idx: continue
                needed = len(sim_game.hands[i]); sim_game.hands[i] = []
                for _ in range(needed):
                    if unknown_cards: sim_game.hands[i].append(unknown_cards.pop())
            
            if round_num == 1:
                dealer_hand = sim_game.hands[self.dealer_idx]
                non_trumps = [c for c in dealer_hand if sim_game.get_effective_suit(c) != suit]
                discard_card = non_trumps[0] if non_trumps else dealer_hand[0]
                dealer_hand.remove(discard_card); dealer_hand.append(sim_game.up_card)
                
            sim_state = SimState(sim_game.trump_suit, sim_game.trick, sim_game.hands, (self.dealer_idx + 1) % 4, False, -1, player_idx, sim_game.voids)
            while (sim_state.team1_tricks + sim_state.team2_tricks) < 5:
                legal = sim_state.get_legal_moves()
                if not legal: break
                sim_state.apply_move(sim_state.get_heuristic_move())
            total_tricks += sim_state.get_result(player_idx)

            loner_partner = (player_idx + 2) % 4
            l_state = SimState(sim_game.trump_suit, [], [list(h) for h in sim_game.hands], (self.dealer_idx + 1) % 4, True, loner_partner, player_idx, sim_game.voids)
            if l_state.current_turn == loner_partner: l_state.current_turn = (l_state.current_turn + 1) % 4
            while (l_state.team1_tricks + l_state.team2_tricks) < 5:
                legal = l_state.get_legal_moves()
                if not legal: break
                l_state.apply_move(l_state.get_heuristic_move())
            total_loner += l_state.get_result(player_idx)
        return total_tricks/simulations_per_suit, total_loner/simulations_per_suit

    def _run_bid_sim(self, player_idx, suit, round_num, simulations_per_suit):
        t, _ = self._run_bid_sim_raw(player_idx, suit, round_num, simulations_per_suit)
        return t

    def _bidding_auto_worker(self):
        action, suit, is_loner, _, _, _ = self._simulate_bidding(
            0, 1 if self.game_state == "bidding_r1" else 2, 100, 250)
        return action, suit, is_loner

    def _cheems_bidding_auto_worker(self, known_hands=None):
        round_num = 1 if self.game_state == "bidding_r1" else 2
        suits_to_check = [self.up_card.suit] if round_num == 1 else [s for s in SUITS_T if s != self.up_card.suit]
        is_stuck = (round_num == 2 and self.passed_count == 3 and self.dealer_idx == 0)
        action, suit, is_loner, _, _, _ = self._simulate_cheems_bidding(
            0, round_num, suits_to_check, is_stuck, known_hands=known_hands)
        return action, suit, is_loner

    def _resolve_auto_bid(self, action, suit, is_loner):
        self._set_controls_state(tk.NORMAL)
        self.lbl_action.config(text="")
        if action == "Pass":
            self._handle_bid_decision(False, None)
        else:
            self._handle_bid_decision(True, suit, is_loner)

    # ==========================================
    # GET HINT LOGIC (GM & Arbiter)
    # ==========================================
    def ask_ai(self, profile_name):
        is_human_decision = (
            self.game_state == "playing" and self.current_turn == 0
            or self.game_state in {"bidding_r1", "bidding_r2"}
            and self.bidding_player == 0
            or self.game_state == "discarding" and self.dealer_idx == 0)
        if self.autoplay_mode or not is_human_decision:
            messagebox.showinfo(
                "Ask an AI", "AI advice is available when you control the active seat.")
            return

        self.session_journal.ai_consultations[profile_name] = (
            self.session_journal.ai_consultations.get(profile_name, 0) + 1)
        self._record_session_event("ai_consultation", {
            "profile": profile_name, "phase": self.game_state})
        self.ai_profiles["0"] = profile_name
        if profile_name == "The MC" or profile_name in HYBRID_MCTS_PROFILES:
            self.get_hint()
            return

        neural_brain = self._get_neural_brain(0)
        if not HAS_TORCH or neural_brain is None:
            messagebox.showwarning(
                profile_name, f"{profile_name}'s neural network is unavailable.")
            return
        known_hands = self._get_neural_known_hands(0)
        if self.game_state in {"bidding_r1", "bidding_r2"}:
            self._get_cheems_bidding_hint(
                known_hands=known_hands, agent_name=profile_name,
                neural_brain=neural_brain)
            return
        if self.game_state == "discarding":
            self._show_cheems_discard_hint_box(
                known_hands=known_hands, agent_name=profile_name,
                neural_brain=neural_brain)
            return

        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(
            text=f"{profile_name} analyzing ({self.hint_neural_play_iters} iters)...")
        self.update_idletasks()

        def calculate_hint():
            ranked_moves = self.get_cheems_ranked_moves(
                0, known_hands=known_hands, neural_brain=neural_brain)
            if (profile_name == "Risk Manager" and len(ranked_moves) > 1
                  and ranked_moves[0][1] - ranked_moves[1][1] <= 5.0):
                ranked_moves[0], ranked_moves[1] = ranked_moves[1], ranked_moves[0]
            return ranked_moves
        self._launch_search(
            f"{profile_name} hint", calculate_hint,
            lambda ranked: self._finish_cheems_hint(ranked, profile_name))

    def get_hint(self):
        if self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0: self.get_bidding_hint(); return
        if self.game_state == "discarding" and self.dealer_idx == 0: self._show_discard_hint_box(); return

        legal_moves = self.get_legal_moves(self.hands[0])
        if len(legal_moves) == 1: self._show_multi_hint_result([(legal_moves[0], 100.0)]); return

        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(text=f"Grandmaster deeply analyzing ({self.display_iters} x2)..."); self.update_idletasks()
        self._launch_search(
            "Grandmaster hint", self._hint_worker_multi,
            self._show_multi_hint_result)

    def get_cheems_hint(self):
        if self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0: self._get_cheems_bidding_hint(); return
        if self.game_state == "discarding" and self.dealer_idx == 0: self._show_cheems_discard_hint_box(); return

        legal_moves = self.get_legal_moves(self.hands[0])
        if len(legal_moves) == 1: self._show_multi_hint_result([(legal_moves[0], 100.0)]); return

        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(text=f"Arbiter building AlphaZero tree ({self.hint_neural_play_iters} iters)..."); self.update_idletasks()
        
        self._launch_search(
            "Arbiter hint", lambda: self.get_cheems_ranked_moves(0),
            lambda ranked: self._finish_cheems_hint(ranked, "Arbiter"))

    def get_ironclad_hint(self):
        if not HAS_TORCH or self.ironclad_brain is None:
            messagebox.showwarning("Ironclad", "Ironclad neural network unavailable.")
            return
        if self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0:
            self._get_cheems_bidding_hint(
                agent_name="Ironclad", neural_brain=self.ironclad_brain)
            return
        if self.game_state == "discarding" and self.dealer_idx == 0:
            self._show_cheems_discard_hint_box(
                agent_name="Ironclad", neural_brain=self.ironclad_brain)
            return

        legal_moves = self.get_legal_moves(self.hands[0])
        if len(legal_moves) == 1:
            self._show_multi_hint_result(
                [(legal_moves[0], 100.0)], agent_name="Ironclad")
            return

        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(
            text=f"Ironclad building AlphaZero tree ({self.hint_neural_play_iters} iters)...")
        self.update_idletasks()

        self._launch_search(
            "Ironclad hint",
            lambda: self.get_cheems_ranked_moves(
                0, neural_brain=self.ironclad_brain),
            lambda ranked: self._finish_cheems_hint(ranked, "Ironclad"))

    def get_kyle_hint(self):
        if not HAS_TORCH or self.kyle_brain is None:
            messagebox.showwarning("Kyle", "Kyle neural network unavailable.")
            return
        if self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0:
            self._get_cheems_bidding_hint(agent_name="Kyle", neural_brain=self.kyle_brain)
            return
        if self.game_state == "discarding" and self.dealer_idx == 0:
            self._show_cheems_discard_hint_box(agent_name="Kyle", neural_brain=self.kyle_brain)
            return

        legal_moves = self.get_legal_moves(self.hands[0])
        if len(legal_moves) == 1:
            self._show_multi_hint_result([(legal_moves[0], 100.0)], agent_name="Kyle")
            return

        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(text=f"Kyle building AlphaZero tree ({self.hint_neural_play_iters} iters)...")
        self.update_idletasks()

        self._launch_search(
            "Kyle hint",
            lambda: self.get_cheems_ranked_moves(
                0, neural_brain=self.kyle_brain),
            lambda ranked: self._finish_cheems_hint(ranked, "Kyle"))

    def get_committee_hint(self):
        if not HAS_TORCH or self.committee_brain is None:
            messagebox.showwarning("Committee", "Committee neural ensemble unavailable.")
            return
        if self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0:
            self._get_cheems_bidding_hint(
                agent_name="Committee", neural_brain=self.committee_brain)
            return
        if self.game_state == "discarding" and self.dealer_idx == 0:
            self._show_cheems_discard_hint_box(
                agent_name="Committee", neural_brain=self.committee_brain)
            return

        legal_moves = self.get_legal_moves(self.hands[0])
        if len(legal_moves) == 1:
            self._show_multi_hint_result(
                [(legal_moves[0], 100.0)], agent_name="Committee")
            return

        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(
            text=f"Committee building consensus tree ({self.hint_neural_play_iters} iters)...")
        self.update_idletasks()

        self._launch_search(
            "Committee hint",
            lambda: self.get_cheems_ranked_moves(
                0, neural_brain=self.committee_brain),
            lambda ranked: self._finish_cheems_hint(ranked, "Committee"))

    def _finish_cheems_hint(self, ranked_moves, agent_name="Arbiter"):
        self._set_controls_state(tk.NORMAL)
        self.lbl_action.config(text="")
        self._show_multi_hint_result(ranked_moves, agent_name=agent_name)

    def _show_discard_hint_box(self):
        hand = self.hands[0]
        best_idx = self.get_smart_discard_index(0)
        best_card = hand[best_idx]
        explanation = self.generate_discard_explanation(best_card)
        
        dialog = tk.Toplevel(self); dialog.title("Optimal Discard Leaderboard"); dialog.geometry("600x540"); dialog.minsize(540, 500); dialog.configure(bg=self.coach_bg_color)
        frame = tk.Frame(dialog, bg=self.dark_bg_color, bd=2, relief=tk.SUNKEN); frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        tk.Label(frame, text=f"Rank 1: Discard {best_card}  =>  Optimal Choice", font=("Arial", 12, "bold"), bg=self.dark_bg_color, fg="lightgreen").pack(pady=15, anchor="w", padx=20)
        
        tk.Label(dialog, text="Reasoning Description:", font=("Arial", 12, "bold", "underline"), bg=self.coach_bg_color, fg="#FFD700").pack(pady=(5,0))
        tk.Label(dialog, text=explanation, font=("Arial", 11, "italic"), bg=self.coach_bg_color, fg="white", wraplength=540, justify=tk.LEFT).pack(pady=(5, 10), padx=24)
        
        tk.Button(dialog, text="Got it", command=dialog.destroy, font=("Arial", 12, "bold"), bg=self.main_bg_color, fg="white").pack(pady=10)

    def _get_cheems_best_discard_index(self, player_idx, known_hands=None,
                                       neural_brain=None, determinizations=None,
                                       return_ranked=False):
        """Delegates to the shared choose_dealer_discard playout search (July 2026
        void-blindness fix) so the GUI recommends exactly what self-play plays.
        Falls back to the heuristic if the brain is unavailable or anything fails."""
        if neural_brain is None:
            neural_brain = self._get_neural_brain(player_idx)
        if not HAS_TORCH or neural_brain is None:
            index = self.get_smart_discard_index(player_idx)
            return [(index, None)] if return_ranked else index
        if determinizations is None:
            determinizations = (self.hint_neural_discard_determinizations
                                if player_idx == 0
                                else self.table_neural_discard_determinizations)
        if self.ai_profiles.get(str(player_idx)) == "Unanimous Council":
            determinizations *= 2

        hand = list(self.hands[player_idx])
        hand_after_pickup = hand + [self.up_card]
        try:
            def nn_eval(tensor):
                t = tensor.to(self.cheems_device).unsqueeze(0)
                with torch.no_grad():
                    logits, value = neural_brain(t)
                return F.softmax(logits[0], dim=0).cpu().numpy(), float(value.item())

            result = choose_dealer_discard(
                hand_after_pickup, self.trump_suit, self.caller_idx, self.is_loner,
                self.up_card, self.dealer_idx, self.team1_score, self.team2_score,
                nn_eval, determinizations=determinizations,
                known_hands=known_hands, discard_candidates=hand,
                choose_worst=False,
                return_ranked=return_ranked)
            if return_ranked:
                return [(hand.index(card), score) for card, score in result]
            return hand.index(result)
        except Exception:
            index = self.get_smart_discard_index(player_idx)
            return [(index, None)] if return_ranked else index

    def _show_cheems_discard_hint_box(self, known_hands=None, agent_name="Arbiter",
                                      neural_brain=None):
        hand = self.hands[0]
        ranked = self._get_cheems_best_discard_index(
            0, known_hands=known_hands, neural_brain=neural_brain,
            return_ranked=True)
        best_idx, best_score = ranked[0]
        best_card = hand[best_idx]
        explanation = self.generate_discard_explanation(
            best_card, agent_name=agent_name, searched=True)
        if len(ranked) > 1:
            runner_idx, runner_score = ranked[1]
            runner_card = hand[runner_idx]
            if best_score is not None and runner_score is not None:
                gap = (best_score - runner_score) * 4.0
                explanation += (
                    f" Why not {runner_card}? On the same hidden deals, {best_card} "
                    f"finished {gap:+.2f} expected team points ahead of that runner-up.")
            else:
                explanation += (
                    f" Why not {runner_card}? It was the next candidate, but it retains "
                    "a less useful five-card shape under the discard heuristic.")

        dialog = tk.Toplevel(self); dialog.title(f"{agent_name} Optimal Discard"); dialog.geometry("600x540"); dialog.minsize(540, 500); dialog.configure(bg=self.coach_bg_color)
        frame = tk.Frame(dialog, bg=self.dark_bg_color, bd=2, relief=tk.SUNKEN); frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        tk.Label(frame, text=f"{agent_name} recommends: Discard {best_card}", font=("Arial", 12, "bold"), bg=self.dark_bg_color, fg="lightgreen").pack(pady=15, anchor="w", padx=20)

        tk.Label(dialog, text="Reasoning Description:", font=("Arial", 12, "bold", "underline"), bg=self.coach_bg_color, fg="#FFD700").pack(pady=(5,0))
        tk.Label(dialog, text=explanation, font=("Arial", 11, "italic"), bg=self.coach_bg_color, fg="white", wraplength=540, justify=tk.LEFT).pack(pady=(5, 10), padx=24)

        tk.Button(dialog, text="Got it", command=dialog.destroy, font=("Arial", 12, "bold"), bg=self.main_bg_color, fg="white").pack(pady=10)

    def generate_discard_explanation(self, card, agent_name="Grandmaster",
                                     searched=False):
        hand = list(self.hands[0])
        resulting_hand = list(hand)
        if card in resulting_hand:
            resulting_hand.remove(card)
        if self.up_card is not None:
            resulting_hand.append(self.up_card)
        effective_suit = self.get_effective_suit(card)
        before_count = sum(
            1 for candidate in hand
            if self.get_effective_suit(candidate) == effective_suit)
        trump_remaining = sum(self.is_trump(candidate) for candidate in resulting_hand)
        off_suit_aces = sum(
            candidate.rank == "A" and not self.is_trump(candidate)
            for candidate in resulting_hand)
        resulting_suits = {
            self.get_effective_suit(candidate) for candidate in resulting_hand
            if not self.is_trump(candidate)}

        if searched:
            opening = (
                f"{agent_name} compared every legal discard across matched hidden-hand "
                f"simulations; discarding {card} produced the strongest average outcomes. ")
        else:
            opening = f"Discard {card}. "

        consequences = []
        if before_count == 1 and effective_suit != self.trump_suit:
            consequences.append(
                f"It removes your only {effective_suit}, creating a void that may let you "
                "ruff that suit with trump later")
        elif effective_suit == self.trump_suit:
            consequences.append(
                "This spends a trump, but it is judged less useful than the five cards retained")
        else:
            consequences.append(
                f"It shortens {effective_suit} without breaking a stronger protected suit")
        consequences.append(
            f"the final hand keeps {trump_remaining} trump card"
            f"{'s' if trump_remaining != 1 else ''}, {off_suit_aces} off-suit ace"
            f"{'s' if off_suit_aces != 1 else ''}, and spans "
            f"{len(resulting_suits)} non-trump suit{'s' if len(resulting_suits) != 1 else ''}")
        if self.up_card is not None:
            consequences.append(
                f"the picked-up {self.up_card} is retained as part of the dealer's five-card hand")
        rendered = [
            consequence[:1].upper() + consequence[1:]
            for consequence in consequences]
        return opening + ". ".join(rendered) + "."

    def get_bidding_hint(self):
        self._set_controls_state(tk.DISABLED)
        self.lbl_action.config(text="Grandmaster evaluating ALL bidding options..."); self.update_idletasks()
        self._launch_search(
            "Grandmaster bid hint", self._bidding_hint_worker_multi,
            self._show_bidding_multi_hint_result)

    def _get_cheems_bidding_hint(self, known_hands=None, agent_name="Arbiter",
                                  neural_brain=None):
        # --- POLICY-HEAD BID HINT (July 2026 bidding overhaul) ---
        # Runs the exact same run_bid_mcts search the AI itself uses (in a worker
        # thread to keep the UI live) and ranks ALL legal bid actions by visit
        # share. Replaces the old raw value-head read whose (v+1)*2.5 trick formula
        # predated the loner-aware value rescale and badly overstated hands.
        round_num = 1 if self.game_state == "bidding_r1" else 2
        is_stuck = (round_num == 2 and self.passed_count == 3 and self.dealer_idx == 0)

        if neural_brain is None:
            neural_brain = self.cheems_brain
        if not HAS_TORCH or neural_brain is None:
            messagebox.showwarning(agent_name, "Neural network unavailable - no bidding hint.")
            return

        self._set_controls_state(tk.DISABLED)
        profile = self.ai_profiles.get("0", "Arbiter")
        rollouts = self.hint_neural_bid_rollouts
        if profile == "Unanimous Council":
            rollouts *= 2
        call_margin, loner_margin = self._bid_style_margins(
            0, round_num, profile)
        self.lbl_action.config(text=f"{agent_name} searching bid tree ({rollouts} rollouts)..."); self.update_idletasks()

        def calculate():
            visit_dict, root_q = run_bid_mcts(
                list(self.hands[0]), self.up_card, self.dealer_idx, 0,
                round_num, self._gui_passed_seats(),
                self.team1_score, self.team2_score,
                lambda tensor: self._eval_neural_brain(tensor, neural_brain),
                rollouts=rollouts, known_hands=known_hands,
                call_margin=call_margin, loner_margin=loner_margin)
            ranked = sorted(visit_dict.items(), key=lambda kv: kv[1], reverse=True)
            if (profile == "Risk Manager" and len(ranked) > 1
                  and ranked[0][1] - ranked[1][1] <= 0.05):
                def action_risk(item):
                    action = item[0]
                    if action == BID_PASS:
                        return 0
                    return 2 if bid_action_details(action)[1] else 1
                ranked[:2] = sorted(ranked[:2], key=action_risk)
            return ranked, root_q, is_stuck, agent_name

        def failed(error):
            print(f"{agent_name} Bidding Hint Error: {error}")
            self._set_controls_state(tk.NORMAL)
            self.lbl_action.config(text="")

        self._launch_search(
            f"{agent_name} bid hint", calculate,
            self._show_cheems_bid_hint_result, failed)

    def _show_cheems_bid_hint_result(self, ranked, root_q, is_stuck, agent_name="Arbiter"):
        self._set_controls_state(tk.NORMAL); self.lbl_action.config(text="")
        dialog = tk.Toplevel(self); dialog.title(f"{agent_name} Bidding Search"); dialog.geometry("600x580"); dialog.minsize(540, 520); dialog.configure(bg=self.coach_bg_color)
        tk.Label(dialog, text=f"{agent_name} Bid Leaderboard", font=("Arial", 16, "bold"), bg=self.coach_bg_color, fg="white").pack(pady=10)
        frame = tk.Frame(dialog, bg=self.dark_bg_color, bd=2, relief=tk.SUNKEN); frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        for i, (action, share) in enumerate(ranked):
            if action == BID_PASS:
                label = "PASS"
            else:
                suit, is_loner = bid_action_details(action)
                label = f"Call {suit}" + (" + GO ALONE" if is_loner else "")
            text = f"Rank {i+1}: {label}  =>  {share*100:.0f}% of search visits"
            color = "lightgreen" if i == 0 else ("yellow" if i == 1 and len(ranked) > 2 else "white")
            tk.Label(frame, text=text, font=("Arial", 11, "bold"), bg=self.dark_bg_color, fg=color).pack(pady=6, anchor="w", padx=20)

        est_pts = root_q * 4.0
        summary = f"Search value: {est_pts:+.2f} expected points for your team"
        if is_stuck: summary += "  (stick-the-dealer: PASS unavailable)"
        tk.Label(dialog, text=summary, font=("Arial", 11, "italic"), bg=self.coach_bg_color, fg="#FFD700", wraplength=440).pack(pady=(0, 5))
        explanation = self.generate_bidding_explanation(
            ranked, root_q=root_q, is_stuck=is_stuck)
        tk.Label(
            dialog, text="Reasoning Description:",
            font=("Arial", 12, "bold", "underline"),
            bg=self.coach_bg_color, fg="#FFD700").pack(pady=(5, 0))
        tk.Label(
            dialog, text=explanation, font=("Arial", 11, "italic"),
            bg=self.coach_bg_color, fg="white", wraplength=540,
            justify=tk.LEFT).pack(pady=(5, 10), padx=24)
        tk.Button(dialog, text="Got it", command=dialog.destroy, font=("Arial", 12, "bold"), bg=self.main_bg_color, fg="white").pack(pady=10)

    def generate_bidding_explanation(self, ranked, root_q=0.0, is_stuck=False):
        if not ranked:
            return "No legal bidding options were returned by the search."
        action, share = ranked[0]
        runner_up_share = ranked[1][1] if len(ranked) > 1 else 0.0
        visit_gap = max(0.0, share - runner_up_share) * 100.0
        round_num = 1 if self.game_state == "bidding_r1" else 2
        confidence = (
            f"The search assigned this option {share * 100:.0f}% of visits, "
            f"a {visit_gap:.0f}-point lead over the runner-up. ")
        value_context = (
            f"The overall position is valued at {root_q * 4.0:+.2f} expected "
            "points for your team. ")

        if action == BID_PASS:
            if round_num == 1:
                auction_context = (
                    f"Passing declines {self.up_card.suit} as trump and keeps the "
                    "auction open for the remaining players")
            else:
                auction_context = (
                    "Passing declines all three available second-round suits and "
                    "hands the decision to the next player")
            if is_stuck:
                auction_context = "Pass would be illegal because the dealer is stuck"
            return (
                f"Pass. {confidence}{auction_context}. The search found that taking "
                "contract responsibility with this hand costs more expected value than "
                f"allowing the auction to continue. {value_context}")

        trump_suit, is_loner = bid_action_details(action)
        same_color_suit = SAME_COLOR_T[trump_suit]

        def effective_suit(candidate):
            if candidate.rank == "J" and candidate.suit == same_color_suit:
                return trump_suit
            return candidate.suit

        hand = list(self.hands[0])
        trump_cards = [
            candidate for candidate in hand
            if effective_suit(candidate) == trump_suit]
        has_right = any(
            candidate.rank == "J" and candidate.suit == trump_suit
            for candidate in hand)
        has_left = any(
            candidate.rank == "J" and candidate.suit == same_color_suit
            for candidate in hand)
        off_suit_aces = sum(
            candidate.rank == "A" and effective_suit(candidate) != trump_suit
            for candidate in hand)
        assets = [
            f"{len(trump_cards)} visible trump card"
            f"{'s' if len(trump_cards) != 1 else ''}"]
        if has_right:
            assets.append("the right bower")
        if has_left:
            assets.append("the left bower")
        if off_suit_aces:
            assets.append(
                f"{off_suit_aces} off-suit ace"
                f"{'s' if off_suit_aces != 1 else ''}")

        if round_num == 1:
            if self.dealer_idx == 0:
                round_context = (
                    f"As dealer, ordering up {self.up_card} adds that trump to your hand "
                    "before you discard, improving both trump length and hand shape")
            else:
                dealer_side = "your partner" if self.dealer_idx == 2 else "an opponent"
                round_context = (
                    f"This orders {self.up_card} into {dealer_side}'s hand, so the search "
                    "has already priced in the dealer's pickup and discard")
        else:
            round_context = (
                f"This is a second-round call: {self.up_card.suit} is unavailable, and "
                f"{trump_suit} is the strongest remaining contract found by the search")

        if is_loner:
            partnership = (
                "Going alone removes your partner from all five tricks. The four-point sweep "
                "upside must therefore outweigh losing partner's cards and protection")
        else:
            partnership = (
                "Calling with partner preserves two-handed coverage and needs only three "
                "team tricks to make the contract")
        return (
            f"Call {trump_suit}{' alone' if is_loner else ''}. {confidence}Your visible "
            f"assets are {', '.join(assets)}. {round_context}. {partnership}. "
            f"{value_context}")

    def _bidding_hint_worker_multi(self):
        player_idx = 0; round_num = 1 if self.game_state == "bidding_r1" else 2
        suits_to_check = [self.up_card.suit] if round_num == 1 else [s for s in SUITS_T if s != self.up_card.suit]
        is_stuck = (round_num == 2 and self.passed_count == 3 and self.dealer_idx == player_idx)
        results = []; sims_per_suit = 800 
            
        for suit in suits_to_check:
            avg_tricks, avg_loner = self._run_bid_sim_raw(
                player_idx, suit, round_num, sims_per_suit)
            win_rate = 0 
            if avg_tricks >= 3: win_rate = 100
            elif avg_tricks >= 2.6: win_rate = ((avg_tricks - 2.6)/0.4)*100
            results.append((suit, avg_tricks, win_rate, avg_loner >= 4.6))
        results.sort(key=lambda x: x[1], reverse=True)
        return results, is_stuck

    def _show_bidding_multi_hint_result(self, ranked_suits, is_stuck):
        self._set_controls_state(tk.NORMAL); self.lbl_action.config(text="")
        dialog = tk.Toplevel(self); dialog.title("Comparative Bidding Grading"); dialog.geometry("600x570"); dialog.minsize(540, 520); dialog.configure(bg=self.coach_bg_color)
        tk.Label(dialog, text="Bidding Leaderboard", font=("Arial", 16, "bold"), bg=self.coach_bg_color, fg="white").pack(pady=10)
        frame = tk.Frame(dialog, bg=self.dark_bg_color, bd=2, relief=tk.SUNKEN); frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        best_expected = ranked_suits[0][1] if ranked_suits else 0; options = []
        
        if best_expected < 2.6 and not is_stuck:
            options.append(("PASS", best_expected, True, 0, False))
            for suit, expected, win_rate, is_loner in ranked_suits: options.append((f"Call {suit}", expected, False, win_rate, is_loner))
        else:
            for suit, expected, win_rate, is_loner in ranked_suits: options.append((f"Call {suit}", expected, False, win_rate, is_loner))
            if not is_stuck: options.append(("PASS", 0, True, 0, False))

        for i, opt in enumerate(options):
            if opt[2]: text = f"Rank {i+1}: PASS (Math implies best suit < 2.6 Tricks)"; color = "lightgreen" if i == 0 else "lightgray"
            else:
                suit_action, expected, _, win_rate, is_loner = opt
                text = f"Rank {i+1}: {suit_action}{' + GO ALONE' if is_loner else ''} => {expected:.1f} Tricks ({win_rate:.0f}% Win)"
                color = "lightgreen" if i == 0 else ("yellow" if i == 1 and len(options) > 2 else "white")
            tk.Label(frame, text=text, font=("Arial", 11, "bold"), bg=self.dark_bg_color, fg=color).pack(pady=7, anchor="w", padx=20)
        explanation = self.generate_classic_bidding_explanation(
            ranked_suits, is_stuck)
        tk.Label(
            dialog, text="Reasoning Description:",
            font=("Arial", 12, "bold", "underline"),
            bg=self.coach_bg_color, fg="#FFD700").pack(pady=(5, 0))
        tk.Label(
            dialog, text=explanation, font=("Arial", 11, "italic"),
            bg=self.coach_bg_color, fg="white", wraplength=540,
            justify=tk.LEFT).pack(pady=(5, 10), padx=24)
        tk.Button(dialog, text="Got it", command=dialog.destroy, font=("Arial", 12, "bold"), bg=self.main_bg_color, fg="white").pack(pady=10)

    def generate_classic_bidding_explanation(self, ranked_suits, is_stuck=False):
        if not ranked_suits:
            return "No legal suits were available for comparison."
        suit, expected_tricks, win_rate, is_loner = ranked_suits[0]
        round_num = 1 if self.game_state == "bidding_r1" else 2
        if expected_tricks < 2.6 and not is_stuck:
            return (
                f"Pass. The strongest available contract was {suit}, but its simulations "
                f"averaged only {expected_tricks:.2f} tricks, below the 2.6 safety threshold "
                "and below the three tricks required to make a call. Passing avoids accepting "
                "a contract with elevated euchre risk and lets the auction continue.")

        same_color_suit = SAME_COLOR_T[suit]

        def effective_suit(candidate):
            if candidate.rank == "J" and candidate.suit == same_color_suit:
                return suit
            return candidate.suit

        hand = list(self.hands[0])
        trump_count = sum(effective_suit(candidate) == suit for candidate in hand)
        has_right = any(
            candidate.rank == "J" and candidate.suit == suit for candidate in hand)
        has_left = any(
            candidate.rank == "J" and candidate.suit == same_color_suit
            for candidate in hand)
        off_suit_aces = sum(
            candidate.rank == "A" and effective_suit(candidate) != suit
            for candidate in hand)
        features = [
            f"{trump_count} visible trump card"
            f"{'s' if trump_count != 1 else ''}"]
        if has_right:
            features.append("the right bower")
        if has_left:
            features.append("the left bower")
        if off_suit_aces:
            features.append(
                f"{off_suit_aces} off-suit ace"
                f"{'s' if off_suit_aces != 1 else ''}")
        contract = "go alone" if is_loner else "call with partner"
        threshold_context = (
            "The loner projection clears the 4.6-trick sweep threshold, making the "
            "four-point upside worth removing partner"
            if is_loner else
            "The projection clears the call threshold while keeping partner available "
            "to cover weak suits and contribute tricks")
        round_context = (
            f"Round one also sends {self.up_card} to the dealer"
            if round_num == 1 else
            f"In round two, the turned-down {self.up_card.suit} cannot be called")
        return (
            f"Call {suit} and {contract}. Across hidden-hand simulations this contract "
            f"averaged {expected_tricks:.2f} tricks with an estimated {win_rate:.0f}% "
            f"make rate. The visible hand contains {', '.join(features)}. "
            f"{threshold_context}. {round_context}.")

    def generate_hint_explanation(self, card):
        effective_suit = self.get_effective_suit(card)
        is_trump = effective_suit == self.trump_suit
        hand = list(self.hands[0])
        trump_cards = [candidate for candidate in hand if self.is_trump(candidate)]
        card_name = str(card)
        if card.rank == "J" and card.suit == self.trump_suit:
            card_name += " (right bower, highest trump)"
        elif card.rank == "J" and card.suit == SAME_COLOR_T[self.trump_suit]:
            card_name += f" (left bower, treated as {self.trump_suit})"

        if not self.trick:
            if is_trump:
                remaining_trump = len(trump_cards) - 1
                strength = (
                    "It cannot be beaten" if "right bower" in card_name
                    else "It applies immediate pressure to every player")
                preservation = (
                    f" You still retain {remaining_trump} trump card"
                    f"{'s' if remaining_trump != 1 else ''} after this lead."
                    if remaining_trump else
                    " This spends your last trump, so the search prefers cashing its control now.")
                return (
                    f"Lead {card_name}. {strength}, and leading trump forces each opponent "
                    "who still holds trump to spend one instead of using it later to ruff an "
                    f"off-suit winner.{preservation} This is a control play: shorten the "
                    "opponents' trump supply before attacking your side suits.")
            same_suit = sum(
                1 for candidate in hand
                if self.get_effective_suit(candidate) == effective_suit)
            if card.rank == "A":
                return (
                    f"Lead {card_name}, the highest card in {effective_suit}. It wins unless "
                    "an opponent is void in the led suit and can trump. Cashing it now reduces "
                    "the chance that later play creates those voids. "
                    f"You hold {same_suit} card{'s' if same_suit != 1 else ''} in this suit.")
            return (
                f"Lead {card_name}. The search prefers developing {effective_suit} while "
                f"preserving your {len(trump_cards)} trump card"
                f"{'s' if len(trump_cards) != 1 else ''} for later control. A low lead can "
                "also invite partner to win efficiently without spending one of your stronger cards.")

        led_suit = self.get_effective_suit(self.trick[0][1])
        current_winner = self.evaluate_trick()
        winner_name = "your partner" if current_winner == 2 else "an opponent"
        follows_suit = effective_suit == led_suit
        trial_trick = list(self.trick) + [(0, card)]
        original_trick = self.trick
        try:
            self.trick = trial_trick
            card_takes_lead = self.evaluate_trick() == 0
        finally:
            self.trick = original_trick

        if follows_suit:
            legal_context = (
                f"You must follow {led_suit}; {card_name} is one of your legal cards in that suit. ")
            if card_takes_lead:
                return legal_context + (
                    f"It moves your team into the lead over {winner_name}. The search judges "
                    "that securing this trick is worth committing this card now.")
            if current_winner == 2:
                return legal_context + (
                    "Your partner is already winning, so this play avoids overtaking partner "
                    "and preserves a stronger card for a later trick.")
            return legal_context + (
                "It cannot beat the card currently winning, so playing the cheapest useful "
                "follower conserves your stronger cards rather than wasting them on a lost trick.")

        if is_trump:
            action = "overtrumps the current winner" if card_takes_lead else "uses trump"
            result = (
                "and puts your team in front" if card_takes_lead
                else "but does not currently take the lead")
            return (
                f"You are void in the led suit, so {card_name} legally {action} {result}. "
                "The search is trading one trump for immediate trick control while retaining "
                f"{len(trump_cards) - 1} other trump card"
                f"{'s' if len(trump_cards) - 1 != 1 else ''}.")
        if current_winner == 2:
            return (
                f"You are void in {led_suit}, and your partner is already winning. Discarding "
                f"{card_name} avoids wasting trump and sheds an off-suit card while partner "
                "carries the trick.")
        return (
            f"You are void in {led_suit}, but {card_name} cannot win this trick. The search "
            "treats it as the least costly discard, preserving trump and stronger side-suit "
            "cards for positions where they can actually take a trick.")

    def generate_play_runner_up_explanation(self, best_card, runner_card,
                                            best_weight, runner_weight):
        gap = max(0.0, best_weight - runner_weight)
        best_trump = self.is_trump(best_card)
        runner_trump = self.is_trump(runner_card)
        if best_trump != runner_trump:
            tactical = (
                "The recommended card uses trump to contest the trick; the runner-up does not."
                if best_trump else
                "The recommendation preserves trump; the runner-up would spend one here.")
        elif self.get_effective_suit(best_card) == self.get_effective_suit(runner_card):
            tactical = (
                f"Both serve the same suit, but choosing {best_card} preserves a different "
                "rank for later trick timing.")
        else:
            tactical = (
                "The cards develop different suits, and the search preferred the resulting "
                "lead and void pattern from the recommendation.")
        return (
            f"Why not {runner_card}? {best_card} received {best_weight:.1f}% versus "
            f"{runner_weight:.1f}% for the runner-up, a {gap:.1f}-point search gap. {tactical}")

    def _hint_worker_multi(self):
        pack = self.ai_model.pack_ui_state(self)
        boosted_iters = self.ai_model.human_total_iters * 2
        raw_ranked_moves = self.ai_model.get_best_move(
            self, 0, return_all_moves=True, override_iters=boosted_iters,
            prepacked_state=pack)
            
        total_visits = sum(weight for _, weight in raw_ranked_moves)
        ranked_moves = []
        for move_idx, weight in raw_ranked_moves:
            choice_weight = (weight / total_visits * 100.0) if total_visits > 0 else (100.0 / len(raw_ranked_moves))
            ranked_moves.append((move_idx, choice_weight))
                
        def sort_key(item):
            move_idx, weight = item
            card = self.hands[0][move_idx]
            return (round(weight, 1), {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}[card.rank], SUITS_T.index(card.suit))
                
        return sorted(ranked_moves, key=sort_key, reverse=True)

    def _show_multi_hint_result(self, ranked_moves, agent_name="Move"):
        self._set_controls_state(tk.NORMAL); self.render_human_hand(); self.lbl_action.config(text="")
        best_idx = ranked_moves[0][0]; self.cached_hint = self.hands[0][best_idx]
        
        legal_moves = self.get_legal_moves(self.hands[0])
        explanation = "Forced Choice" if len(legal_moves) == 1 else self.generate_hint_explanation(self.hands[0][best_idx])
        if len(legal_moves) > 1 and len(ranked_moves) > 1:
            runner_idx, runner_weight = ranked_moves[1]
            explanation += "\n\n" + self.generate_play_runner_up_explanation(
                self.hands[0][best_idx], self.hands[0][runner_idx],
                ranked_moves[0][1], runner_weight)
            
        dialog = tk.Toplevel(self); dialog.title(f"{agent_name} Move Grading"); dialog.geometry("600x600"); dialog.minsize(540, 540); dialog.configure(bg=self.coach_bg_color)
        tk.Label(dialog, text=f"{agent_name} Move Leaderboard", font=("Arial", 16, "bold"), bg=self.coach_bg_color, fg="white").pack(pady=10)
        frame = tk.Frame(dialog, bg=self.dark_bg_color, bd=2, relief=tk.SUNKEN); frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        for i, (move_idx, choice_weight) in enumerate(ranked_moves):
            card = self.hands[0][move_idx]; color = "lightgreen" if i == 0 else ("yellow" if i == 1 and len(ranked_moves) > 2 else "white")
            rate_str = "Forced Play" if len(legal_moves) == 1 else f"{choice_weight:.1f}% Choice Weight"
            tk.Label(frame, text=f"Rank {i+1}: {card}  =>  {rate_str}", font=("Arial", 12, "bold"), bg=self.dark_bg_color, fg=color).pack(pady=5, anchor="w", padx=20)
            
        tk.Label(dialog, text="Reasoning Description:", font=("Arial", 12, "bold", "underline"), bg=self.coach_bg_color, fg="#FFD700").pack(pady=(5,0))
        tk.Label(dialog, text=explanation, font=("Arial", 11, "italic"), bg=self.coach_bg_color, fg="white", wraplength=540, justify=tk.LEFT).pack(pady=(5, 10), padx=24)
        tk.Button(dialog, text="Got it", command=dialog.destroy, font=("Arial", 12, "bold"), bg=self.main_bg_color, fg="white").pack(pady=10)

    def update_table_graphics(self):
        if self.game_state in ["bidding_r1", "discarding"]:
            self.lbl_upcard.config(text=str(self.up_card), bg="white", fg=self.up_card.color, font=("Arial", 32, "bold"), width=5, height=2, relief=tk.RAISED)
            self.lbl_upcard.place(relx=0.5, rely=0.55, anchor=tk.CENTER); self.lbl_upcard.lift()
        elif self.game_state == "bidding_r2":
            self.lbl_upcard.config(text="X", bg="gray", fg="white", font=("Arial", 32, "bold"), width=5, height=2, relief=tk.RAISED)
            self.lbl_upcard.place(relx=0.5, rely=0.55, anchor=tk.CENTER); self.lbl_upcard.lift()
        else: self.lbl_upcard.place_forget()

        for lbl in self.played_card_labels: lbl.destroy()
        self.played_card_labels = []

        if self.game_state == "playing":
            positions = {0: (0.5, 0.75), 1: (0.2, 0.55), 2: (0.5, 0.35), 3: (0.8, 0.55)}
            for p_idx, card in self.trick:
                bg_color = "yellow" if self.is_trump(card) else "white"
                lbl = tk.Label(self.table_frame, text=str(card), font=("Arial", 24, "bold"), bg=bg_color, fg=card.color, width=5, height=2, relief=tk.RAISED)
                lbl.place(relx=positions[p_idx][0], rely=positions[p_idx][1], anchor=tk.CENTER); self.played_card_labels.append(lbl)

        if hasattr(self, 'show_logic_var') and self.show_logic_var.get():
            self.lbl_p1_voids.config(text="Voids: " + " ".join(self.voids[1]) if self.voids[1] else "")
            self.lbl_p2_voids.config(text="Voids: " + " ".join(self.voids[2]) if self.voids[2] else "")
            self.lbl_p3_voids.config(text="Voids: " + " ".join(self.voids[3]) if self.voids[3] else "")
        elif hasattr(self, 'lbl_p1_voids'):
            self.lbl_p1_voids.config(text=""); self.lbl_p2_voids.config(text=""); self.lbl_p3_voids.config(text="")
        self.update_boss_tracker()

    def _configure_drill_scenario(self, deck):
        if self.active_drill == "Standard Match":
            return False

        scenario = self.active_drill
        if scenario == "Drill: Mystery Scenario":
            scenario = random.choice([
                drill for drill in DRILL_DESCRIPTIONS
                if drill not in {"Standard Match", "Drill: Mystery Scenario"}
            ])
        self.current_drill_scenario = scenario

        def find_card(rank, suit):
            return next(card for card in deck if card.rank == rank and card.suit == suit)

        def redeal_human_hand(cards, up_card=None):
            used = {(card.rank, card.suit) for card in cards}
            if up_card is not None:
                used.add((up_card.rank, up_card.suit))
            remaining = [
                card for card in deck
                if (card.rank, card.suit) not in used
            ]
            if up_card is None:
                up_card = remaining.pop(15)
            self.hands = [list(cards)] + [
                remaining[offset:offset + 5] for offset in (0, 5, 10)
            ]
            self.up_card = up_card
            for hand in self.hands:
                self.sort_hand(hand)

        self.dealer_idx = 3
        self.trump_suit = random.choice(SUITS_T)
        self.caller_idx = random.randrange(4)
        self.game_state = "playing"
        self.current_turn = 0
        self.bidding_player = 0
        self.passed_count = 0

        if scenario == "Drill: Loner Defense":
            self.caller_idx = random.choice([1, 3])
            self.is_loner = True
            self.loner_partner_idx = (self.caller_idx + 2) % 4
        elif scenario == "Drill: Dealer Pickup & Discard":
            self.dealer_idx = 0
            self.trump_suit = self.up_card.suit
            self.caller_idx = random.choice([1, 3])
            self.game_state = "discarding"
            self.current_turn = 0
        elif scenario == "Drill: Euchre or Bust":
            self.caller_idx = random.choice([1, 3])
            self.is_loner = False
            self.loner_partner_idx = -1
        elif scenario == "Drill: First Lead Laboratory":
            self.is_loner = False
            self.loner_partner_idx = -1
        elif scenario == "Drill: Closeout at 9 Points":
            self.team1_score = 9
            self.team2_score = 9
            self.trump_suit = None
            self.caller_idx = -1
            self.game_state = "bidding_r1"
        elif scenario == "Drill: Down 9-6 Comeback":
            self.team1_score = 6
            self.team2_score = 9
            self.trump_suit = None
            self.caller_idx = -1
            self.game_state = "bidding_r1"
        elif scenario == "Drill: Partner Called Trump":
            self.caller_idx = 2
            self.is_loner = False
            self.loner_partner_idx = -1
        elif scenario == "Drill: Two-Trick Endgame":
            self.hands = [deck[seat * 2:(seat + 1) * 2] for seat in range(4)]
            for hand in self.hands:
                self.sort_hand(hand)
            self.played_cards = list(deck[8:20])
            self.team1_tricks, self.team2_tricks = random.choice([(2, 1), (1, 2)])
            self.is_loner = False
            self.loner_partner_idx = -1
        elif scenario == "Drill: Weak Dealer Hand":
            weak_cards = [
                find_card("9", SUITS_T[0]), find_card("10", SUITS_T[1]),
                find_card("9", SUITS_T[2]), find_card("10", SUITS_T[3]),
                find_card("Q", SUITS_T[0]),
            ]
            redeal_human_hand(weak_cards, find_card("A", SUITS_T[2]))
            self.dealer_idx = 0
            self.trump_suit = None
            self.caller_idx = -1
            self.game_state = "bidding_r2"
            self.passed_count = 3
        elif scenario == "Drill: Bower Management":
            trump = self.trump_suit
            left_suit = SAME_COLOR_T[trump]
            off_suits = [suit for suit in SUITS_T if suit not in {trump, left_suit}]
            bower_cards = [
                find_card("J", trump), find_card("J", left_suit),
                find_card("9", trump), find_card("A", off_suits[0]),
                find_card("9", off_suits[1]),
            ]
            redeal_human_hand(bower_cards)
            self.caller_idx = 2
            self.is_loner = False
            self.loner_partner_idx = -1
        elif scenario == "Drill: Call It and Prove It":
            self.dealer_idx = 0
            self.trump_suit = None
            self.caller_idx = -1
            self.game_state = "bidding_r2"
            self.passed_count = 3
        else:
            return False

        return True

    def start_new_hand(self, seed_override=None):
        if self._tournament_paused():
            return
        self._invalidate_tasks()
        self.game_state = "dealing"; self.hand_accuracy_sum = 0.0; self.hand_accuracy_count = 0; self.trick_snapshots = {}
        self.hand_bid_feedback = ""
        human_league_game = human_league_current_game(
            getattr(self, "human_league_state", None)
            if getattr(self, "human_league_game_active", False) else None)
        if seed_override is None and human_league_game is not None:
            seed_override = (
                int(human_league_game["seed_base"])
                + len(human_league_game.setdefault("hand_seeds", [])))
        self.current_hand_seed = select_hand_seed(
            seed_override, self.tournament_state, self.current_hand_seed,
            reuse_current=self.sandbox_mode)
        if self.tournament_state and self.tournament_state.get("league_mode"):
            heartbeat_league_job(self.tournament_state["league_job_id"])
            if self.tournament_state.get("mirror_phase") == 0:
                self.tournament_state.setdefault("mirror_seeds", []).append(
                    self.current_hand_seed)
        random.seed(self.current_hand_seed)
        if HAS_TORCH:
            torch.manual_seed(self.current_hand_seed % (2 ** 63 - 1))
        if self.tournament_state is not None:
            self.tournament_state.setdefault("hand_seeds", []).append(
                self.current_hand_seed)
        if human_league_game is not None:
            human_league_game.setdefault("hand_seeds", []).append(
                self.current_hand_seed)
            save_human_league_state(self.human_league_state)
        self.wildcard_hand_profiles = {
            seat: random.choice(WILDCARD_PROFILES)
            for seat in range(4)
            if self.ai_profiles.get(str(seat)) == "Wildcard"
        }
        
        if not self.sandbox_mode:
            deck = build_seeded_deck(self.current_hand_seed)
            self.saved_initial_deck = [Card(c.rank, c.suit) for c in deck]; self.saved_dealer_idx = self.dealer_idx
        else:
            deck = [Card(c.rank, c.suit) for c in self.saved_initial_deck]; self.dealer_idx = self.saved_dealer_idx
            
        for i in range(4):
            self.hands[i] = deck[i*5 : (i+1)*5]
            self.sort_hand(self.hands[i])
            
        self.up_card = deck[20]; self.trump_suit = None; self.caller_idx = -1; self.is_loner = False; self.loner_partner_idx = -1; self.loner_var.set(False) 
        self.voids = {0: set(), 1: set(), 2: set(), 3: set()}
        self.dealer_discard = None  # <--- ADD THIS LINE
        self.lbl_trump.config(text="TRUMP: Uncalled", bg="white"); self.team1_tricks = 0; self.team2_tricks = 0
        self.update_scoreboard(); self.update_dealer_chip(); 
        
        self.trick = []; self.played_cards = []; self.trainer_mistakes = []; self.cached_hint = None
        self.is_rewind_mode = False 

        if self._configure_drill_scenario(deck):
            self.loner_var.set(self.is_loner)
            if self.game_state in {"bidding_r1", "bidding_r2"}:
                self.lbl_trump.config(text="TRUMP: Uncalled", bg="white")
            else:
                self.lbl_trump.config(
                    text=f"TRUMP: {self.trump_suit} (Called by {self.get_player_display_name(self.caller_idx)})",
                    bg="yellow")
            self.trick_snapshots[0] = self.ai_model.pack_ui_state(self)
            self.update_scoreboard(); self.update_dealer_chip()
            self.update_table_graphics(); self.render_human_hand()
            if self.game_state == "discarding":
                self.after(100, self.process_discard)
            elif self.game_state in {"bidding_r1", "bidding_r2"}:
                self.lbl_action.config(text="Your turn to bid. Compare the brains before deciding.")
                self.after(100, self.process_bidding)
            else:
                self.lbl_action.config(text="Your turn. Compare the brains or play a card.")
                self._capture_expected_tricks()
            self._schedule_autosave()
            return
        
        self.trick_snapshots[0] = self.ai_model.pack_ui_state(self)
        
        self.game_state = "bidding_r1"; self.bidding_player = (self.dealer_idx + 1) % 4; self.passed_count = 0
        self.lbl_action.config(text="Round 1 Bidding..."); self.update_table_graphics(); self.render_human_hand()
        self._schedule_autosave()
        self._record_session_event("hand_start", {
            "seed": self.current_hand_seed,
            "dealer": self.dealer_idx,
            "deck_hash": canonical_state_hash(
                [(card.rank, card.suit) for card in deck]),
        })
        self.after(1000, self.process_bidding)

    def _capture_expected_tricks(self):
        if not self.autoplay_mode:
            self.hand_expected_tricks = 2.5

    def process_bidding(self):
        if self._tournament_paused():
            return
        if self.bidding_player != 0 or self.autoplay_mode: self.lbl_action.config(text=f"{self.get_player_display_name(self.bidding_player)} is deciding...")
        else: self.lbl_action.config(text="Your turn to bid.")
        self.render_human_hand()
        if self.game_state in ["bidding_r1", "bidding_r2"]:
            if self.bidding_player == 0 and not self.autoplay_mode: self.render_bidding_ui()
            else: self.after(self._action_delay_ms(), self._ai_make_bid_async)

    def _ai_make_bid_async(self):
        if self._tournament_paused():
            return
        profile = self.ai_profiles.get(str(self.bidding_player), "AI")
        self._launch_search(
            f"{profile} table bid", self._ai_bid_worker,
            self._apply_ai_bid_result)

    def _ai_bid_worker(self):
        profile = self.ai_profiles.get(str(self.bidding_player), "Human")
        num_polls = 1 if profile in HEURISTIC_PROFILES else 100
        if profile in HEURISTIC_PROFILES:
            suit_count = 1 if self.game_state == "bidding_r1" else 3
            simulations = math.ceil(CHEEMS_UI_BID_ROLLOUTS / suit_count)
        else:
            simulations = 250
        known_hands = self._get_autoplay_known_hands(self.bidding_player)
        action, suit, is_loner, _, _, _ = self._simulate_bidding(
            self.bidding_player,
            1 if self.game_state == "bidding_r1" else 2,
            num_polls, simulations, known_hands=known_hands)
        return action, suit, is_loner

    def _apply_ai_bid_result(self, action, suit, is_loner):
        if action == "Call":
            self._handle_bid_decision(True, suit, is_loner)
        else:
            self._handle_bid_decision(False, None)

    def _handle_bid_decision(self, called, suit, is_loner=False):
        if self.game_state not in {"bidding_r1", "bidding_r2"}:
            return
        self._invalidate_tasks()
        self._record_session_event("bid", {
            "player": self.bidding_player,
            "profile": self.ai_profiles.get(str(self.bidding_player), "Human"),
            "decision": "Call" if called else "Pass",
            "suit": suit, "alone": bool(is_loner),
        })
        if self.bidding_player == 0 and not self.autoplay_mode:
            human_loner = called and (
                is_loner or getattr(self, "loner_var", None) is not None
                and self.loner_var.get())
            if not called:
                self.copycat_style_scores["Ironclad"] += 2.0
            elif human_loner:
                self.copycat_style_scores["Kyle"] += 3.0
            else:
                self.copycat_style_scores["Kyle"] += 1.0
                self.copycat_style_scores["Arbiter"] += 1.0

        for w in self.bidding_buttons_frame.winfo_children():
            if isinstance(w, tk.Button): w.config(state=tk.DISABLED)

        if self.bidding_player == 0 and not called:
            if not self.sandbox_mode: self.stats_tracker.record_event("total_passes")
            
            if not self.autoplay_mode and self.game_state == "bidding_r1" and self.team2_score >= 8 and self.up_card.rank == 'J':
                if not self.sandbox_mode: self.stats_tracker.record_event("catastrophic_loner_leaks")
                self.trainer_mistakes.append({
                    "text": f"Bidding Error: Missed Donation! The score is {self.team1_score}-{self.team2_score} and the up-card is a Jack. Passing here risks losing the game to a Loner (4 pts). You must 'donate' (call trump and intentionally get euchred) to block it.",
                    "trick_num": 0
                })
        
        if self.bidding_player == 0 and not self.autoplay_mode and not self.sandbox_mode:
            round_num = 1 if self.game_state == "bidding_r1" else 2
            is_stuck = (round_num == 2 and self.passed_count == 3 and self.dealer_idx == 0)
            
            def calculate_bid_feedback():
                events = []
                mistake = None
                if not called:
                    action, optimal_suit, _, _, expected_tricks, _ = self._simulate_bidding(0, round_num, 50, 200)
                    if action == "Call" and expected_tricks >= 2.6:
                        feedback = f"Conservative Bid: You passed, but calling {optimal_suit} had an expected {expected_tricks:.1f} tricks."
                        events.append("missed_calls")
                        
                        if round_num == 2 and self.dealer_idx == 3 and optimal_suit == SAME_COLOR_T[self.up_card.suit]: 
                            events.append("missed_next_calls")
                            has_next_power = any(self.get_effective_suit(c) == optimal_suit and c.rank in ['A', 'J'] for c in self.hands[0])
                            if has_next_power:
                                mistake = {
                                    "text": f"Bidding Error: Missed Mandatory 'Next' Call! You were in Seat 1 and held power in the Next suit ({optimal_suit}). You must call this to attack the dealer's known weakness.",
                                    "trick_num": 0
                                }
                    else:
                        feedback = "Good Pass: The math mathematically agreed with passing."
                else: 
                    expected_tricks = self._run_bid_sim(0, suit, round_num, 150)
                    if expected_tricks < 2.5 and not is_stuck:
                        feedback = f"Aggressive Bid: You called {suit}, but expected tricks were only {expected_tricks:.1f}."
                    else:
                        feedback = f"Solid Bid: Calling {suit} was mathematically sound ({expected_tricks:.1f} expected tricks)."
                return feedback, events, mistake

            def apply_bid_feedback(feedback, events, mistake):
                self.hand_bid_feedback = feedback
                if not self.sandbox_mode:
                    for event_name in events:
                        self.stats_tracker.record_event(event_name)
                if mistake:
                    self.trainer_mistakes.append(mistake)

            self._launch_search(
                "bid coaching", calculate_bid_feedback, apply_bid_feedback)

        if not called:
            self.passed_count += 1
            if self.passed_count == 4:
                if self.game_state == "bidding_r1": 
                    self.game_state = "bidding_r2"; self.bidding_player = (self.dealer_idx + 1) % 4; self.passed_count = 0
                    self.lbl_action.config(text="Round 2 Bidding..."); self.update_table_graphics(); self.after(1000, self.process_bidding); return
                else: self.after(1000, self.start_new_hand); return 
            self.bidding_player = (self.bidding_player + 1) % 4; self.after(self._action_delay_ms(), self.process_bidding)
        else:
            self.caller_idx = self.bidding_player; self.trump_suit = suit
            for i in range(4): self.sort_hand(self.hands[i])
            if self.bidding_player == 0 and not self.autoplay_mode: self.is_loner = self.loner_var.get()
            else: self.is_loner = is_loner
            
            if self.is_loner:
                self.loner_partner_idx = (self.caller_idx + 2) % 4
            else:
                self.loner_partner_idx = -1
                
            if self.bidding_player == 0:
                if not self.sandbox_mode: self.stats_tracker.record_event("trump_calls")
                if self.is_loner: 
                    if not self.sandbox_mode: self.stats_tracker.record_event("went_alone")
                    if not self.autoplay_mode:
                        round_num = 1 if self.game_state == "bidding_r1" else 2
                        audit_suit = suit
                        def check_greedy_loner():
                            avg_team, avg_loner = self._run_bid_sim_raw(0, audit_suit, round_num, 150)
                            return avg_team >= 3.6 and avg_loner <= 3.4

                        def apply_greedy_loner(is_greedy):
                            if is_greedy:
                                if not self.sandbox_mode: self.stats_tracker.record_event("greedy_loners")
                                self.trainer_mistakes.append({
                                    "text": f"Bidding Error: Greedy Loner! You went alone, but simulations show you lacked the off-suit power to sweep safely. Playing with your partner had a higher probability of securing 2 points.",
                                    "trick_num": 0
                                })
                        self._launch_search(
                            "loner coaching", check_greedy_loner,
                            apply_greedy_loner)
            
            loner_text = " (GOING ALONE!)" if self.is_loner else ""
            self.lbl_trump.config(text=f"TRUMP: {self.trump_suit} (Called by {self.get_player_display_name(self.caller_idx)}){loner_text}", bg="yellow")
            for w in self.bidding_buttons_frame.winfo_children(): w.destroy()
            self.render_human_hand()
            
            if self.game_state == "bidding_r1": self.game_state = "discarding"; self.after(self._action_delay_ms(), self.process_discard)
            else: 
                self.game_state = "playing"; self.lbl_action.config(text="")
                self.current_turn = (self.dealer_idx + 1) % 4
                self._capture_expected_tricks(); self.update_table_graphics(); self.render_human_hand(); self.after(self._action_delay_ms(), self.play_ai_turns)

    def process_discard(self):
        if self._tournament_paused():
            return
        self.lbl_action.config(text=f"Dealer ({self.get_player_display_name(self.dealer_idx)}) must discard.")
        if self.dealer_idx == 0 and not self.autoplay_mode: self.render_human_hand() 
        else: self.after(self._action_delay_ms(), self._ai_make_discard)

    def _ai_make_discard(self):
        if self._tournament_paused():
            return
        dealer_idx = self.dealer_idx
        profile = self.ai_profiles.get(str(dealer_idx), "AI")

        def calculate():
            if self.ai_profiles.get(str(self.dealer_idx)) in NEURAL_PROFILES:
                known_hands = self._get_autoplay_known_hands(self.dealer_idx)
                return self._get_cheems_best_discard_index(
                    self.dealer_idx, known_hands=known_hands)
            return self.get_smart_discard_index(self.dealer_idx)

        self._launch_search(
            f"{profile} discard", calculate,
            lambda index: self._apply_ai_discard(dealer_idx, index))

    def _apply_ai_discard(self, dealer_idx, discard_idx):
        if dealer_idx != self.dealer_idx or self.game_state != "discarding":
            return
        hand = self.hands[dealer_idx]
        discard_card = hand[discard_idx]
        self._record_session_event("discard", {
            "player": dealer_idx,
            "profile": self.ai_profiles.get(str(dealer_idx), "Unknown"),
            "card": str(discard_card),
            "legal_actions": [str(card) for card in hand],
        })
        self.dealer_discard = discard_card
        hand.remove(discard_card)
        hand.append(self.up_card)
        self._invalidate_tasks()
        self.game_state = "playing"
        self.current_turn = (self.dealer_idx + 1) % 4
        self.lbl_action.config(text="")
        self._capture_expected_tricks()
        self.update_table_graphics()
        self.render_human_hand()
        self._schedule_autosave()
        self.play_ai_turns()

    def render_bidding_ui(self):
        for w in self.bidding_buttons_frame.winfo_children(): w.destroy()
        self.chk_loner = tk.Checkbutton(self.bidding_buttons_frame, text="Go Alone", variable=self.loner_var, font=("Arial", 12, "bold"), bg=self.main_bg_color, fg="white", selectcolor=self.dark_bg_color); self.chk_loner.pack(side=tk.LEFT, padx=10)
        if self.game_state == "bidding_r1":
            tk.Button(self.bidding_buttons_frame, text=f"Order Up {self.up_card.suit}", font=("Arial", 14, "bold"), bg="lightgreen", command=lambda: self._handle_bid_decision(True, self.up_card.suit)).pack(side=tk.LEFT, padx=10)
            tk.Button(self.bidding_buttons_frame, text="Pass", font=("Arial", 14, "bold"), bg="lightcoral", command=lambda: self._handle_bid_decision(False, None)).pack(side=tk.LEFT, padx=10)
        elif self.game_state == "bidding_r2":
            for suit in [s for s in SUITS_T if s != self.up_card.suit]: 
                tk.Button(self.bidding_buttons_frame, text=f"Call {suit}", font=("Arial", 14, "bold"), command=lambda s=suit: self._handle_bid_decision(True, s)).pack(side=tk.LEFT, padx=5)
            if not (self.passed_count == 3 and self.bidding_player == self.dealer_idx):
                tk.Button(self.bidding_buttons_frame, text="Pass", font=("Arial", 14, "bold"), bg="lightcoral", command=lambda: self._handle_bid_decision(False, None)).pack(side=tk.LEFT, padx=10)

    def render_human_hand(self):
        for w in self.hand_buttons_frame.winfo_children(): w.destroy()
        
        is_my_turn = False
        if self.game_state == "playing" and self.current_turn == 0: is_my_turn = True
        elif self.game_state == "discarding" and self.dealer_idx == 0: is_my_turn = True
        elif self.game_state in ["bidding_r1", "bidding_r2"] and self.bidding_player == 0: is_my_turn = True
        
        if self.game_state == "bidding_r1":
            base_pwr, base_desc = self.calculate_hand_power(self.hands[0], self.up_card.suit)
            if self.dealer_idx == 0:
                temp_hand = list(self.hands[0])
                discard_idx = self.get_smart_discard_index(0)
                temp_hand.pop(discard_idx); temp_hand.append(self.up_card)
                new_pwr, new_desc = self.calculate_hand_power(temp_hand, self.up_card.suit)
                self.lbl_hand_power.config(text=f"Base Power: {base_pwr:.1f}/10  ?  With Pickup: {new_pwr:.1f}/10 [{new_desc}]")
            else: self.lbl_hand_power.config(text=f"Hand Power (if {self.up_card.suit} called): {base_pwr:.1f}/10 [{base_desc}]")
        elif self.game_state == "bidding_r2":
            best_s = None; best_pwr = -1; best_desc = ""
            for s in SUITS_T:
                if s != self.up_card.suit:
                    pwr, desc = self.calculate_hand_power(self.hands[0], s)
                    if pwr > best_pwr: best_pwr = pwr; best_s = s; best_desc = desc
            self.lbl_hand_power.config(text=f"Best Potential Suit ({best_s}): {best_pwr:.1f}/10 [{best_desc}]")
        elif self.game_state in ["playing", "discarding"] and self.trump_suit:
            pwr, desc = self.calculate_hand_power(self.hands[0], self.trump_suit)
            self.lbl_hand_power.config(text=f"Live Hand Power: {pwr:.1f}/10 [{desc}]")
        else: 
            self.lbl_hand_power.config(text="")

        self.btn_main_menu.config(
            state=tk.NORMAL if is_my_turn and not self.autoplay_mode else tk.DISABLED)

        if is_my_turn and not self.autoplay_mode: 
            self.ask_ai_button.pack(side=tk.LEFT, padx=5)
            self.update_live_odds()
        else: 
            self.ask_ai_button.pack_forget()
            self.lbl_live_odds.config(text="")

        if self.game_state == "playing" and self.loner_partner_idx == 0:
            tk.Label(self.hand_buttons_frame, text="Sitting out this hand.", font=("Arial", 14, "italic"), bg=self.main_bg_color, fg="white").pack()
            return

        legal_moves = self.get_legal_moves(self.hands[0])
        highlight_suit = self.trump_suit if self.trump_suit else (self.up_card.suit if self.up_card else None)

        for index, card in enumerate(self.hands[0]):
            is_highlighted = highlight_suit and (card.suit == highlight_suit or (card.rank == 'J' and card.suit == SAME_COLOR_T[highlight_suit]))
            bg_color = "yellow" if is_highlighted else "white"
            is_active = False
            if self.game_state == "playing" and self.current_turn == 0 and index in legal_moves and not self.autoplay_mode: is_active = True
            elif self.game_state == "discarding" and self.dealer_idx == 0 and not self.autoplay_mode: is_active = True

            cmd = lambda i=index, active=is_active: self.human_discard_card(i) if self.game_state == "discarding" else self.human_play_card(i) if active else None
            text_color = card.color if is_active else "#b0b0b0" 
            large_cards = self.settings_store.data.get("large_cards", False)
            tk.Button(
                self.hand_buttons_frame, text=str(card),
                font=("Arial", 28 if large_cards else 24, "bold"),
                bg=bg_color, fg=text_color, width=6 if large_cards else 5,
                height=3 if large_cards else 2, command=cmd).pack(
                    side=tk.LEFT, padx=7 if large_cards else 5)

    def human_discard_card(self, index):
        if self.game_state != "discarding": return
        if index < 0 or index >= len(self.hands[0]):
            return
        self._invalidate_tasks()
        self.game_state = "locked" 
        
        hand_before = list(self.hands[0])
        discarded_card = hand_before[index]
        self._record_session_event("discard", {
            "player": 0, "profile": "Human", "card": str(discarded_card)})

        self.dealer_discard = discarded_card
        
        def count_suits(hand_list):
            return len(set(self.get_effective_suit(c) for c in hand_list))
            
        suits_before = count_suits(hand_before)
        suits_after_actual = count_suits([c for i, c in enumerate(hand_before) if i != index])
        
        could_void = False
        for i in range(5):
            test_hand = [c for j, c in enumerate(hand_before) if j != i]
            if count_suits(test_hand) < suits_before:
                could_void = True
                break
                
        if could_void and suits_after_actual == suits_before:
            if not self.sandbox_mode: self.stats_tracker.record_event("missed_void_discards")
            self.trainer_mistakes.append({
                "text": f"Pre-Round Error: Sub-Optimal Discard! You discarded the {discarded_card}, leaving yourself with a doubleton suit. You had an optimized discard choice available that would have established a clean void, allowing you to over-trump later.", 
                "trick_num": 0
            })
        
        self.hands[0].remove(discarded_card); self.hands[0].append(self.up_card)
        self.sort_hand(self.hands[0])
        
        self.game_state = "playing"; self.current_turn = (self.dealer_idx + 1) % 4
        self.lbl_action.config(text=""); self._capture_expected_tricks()
        self.cached_hint = None; self.render_human_hand(); self.update_table_graphics(); self.play_ai_turns()

    def _save_trick_snapshot(self):
        tn = (len(self.played_cards) // 4) + 1
        pack = self.ai_model.pack_ui_state(self)
        self.trick_snapshots[tn] = pack
        
    def _rewind_to_trick(self, trick_num, dialog):
        if not messagebox.askyesno(
                "Rewind Hand",
                "Rewind to this trick? The current hand timeline will be replaced.",
                parent=dialog):
            return
        dialog.destroy()
        self._invalidate_tasks()
        pack = self.trick_snapshots[trick_num]
        self.trump_suit = pack['trump_suit']
        self.trick = [(p, Card(r, s)) for p, r, s in pack['trick']]
        self.hands = [[Card(r, s) for r, s in h] for h in pack['hands']]
        self.current_turn = pack['current_turn']
        self.is_loner = pack['is_loner']
        self.loner_partner_idx = pack['loner_partner_idx']
        self.caller_idx = pack['caller_idx']
        self.team1_tricks = pack['team1_tricks']
        self.team2_tricks = pack['team2_tricks']
        self.voids = {int(k): set(v) for k, v in pack['voids'].items()}
        self.played_cards = [Card(r, s) for r, s in pack['played_cards']]
        self.up_card = Card(pack['up_card'][0], pack['up_card'][1]) if pack['up_card'] else None
        
        self.is_rewind_mode = True; self.sandbox_mode = True 
        self.update_scoreboard(); self.update_table_graphics()
        self.render_human_hand()

    def human_play_card(self, index):
        if self.game_state != "playing" or self.current_turn != 0: return
        self._invalidate_tasks()
        self._set_controls_state(tk.DISABLED)
            
        legal_moves = self.get_legal_moves(self.hands[0])
        
        synergy_warning = None
        human_card = self.hands[0][index]
        caller_team = 1 if self.caller_idx in [0, 2] else 2
        
        is_left = human_card.rank == 'J' and human_card.suit == SAME_COLOR_T[self.trump_suit]
        if is_left and any(c.rank == 'J' and c.suit == self.trump_suit for p, c in self.trick):
            synergy_warning = "Trapped Left Bower: You held the Left Bower too long and were forced to surrender it to the Right Bower!"
            if not self.sandbox_mode: self.stats_tracker.record_event("trapped_left_bowers")

        if len(legal_moves) > 1:
            if not self.trick:
                if caller_team == 1 and not self.is_trump(human_card):
                    has_trump = any(self.is_trump(self.hands[0][m]) for m in legal_moves)
                    if has_trump and len(self.played_cards) < 8: 
                        if self.caller_idx == 0:
                            synergy_warning = f"Failed Trump Pull: You called trump! You led the {human_card} instead of clearing the board. Lead trump early to protect your team's off-suit Aces."
                        else:
                            synergy_warning = f"Starving the Caller: Your partner called trump! You led the {human_card} instead of feeding them trump. Help them draw out the opponents' trump."
                        if not self.sandbox_mode: self.stats_tracker.record_event("failed_trump_pulls")
                
                if caller_team == 2:
                    if self.is_trump(human_card) and human_card.rank != 'J':
                        has_off_suit = any(not self.is_trump(self.hands[0][m]) for m in legal_moves)
                        if has_off_suit:
                            synergy_warning = "Defensive Trump Lead: The opponents called trump! Leading trump does their work for them and strips your partner of defensive power. Lead an off-suit card instead."
                            if not self.sandbox_mode: self.stats_tracker.record_event("defensive_trump_leads")
                    elif not self.is_trump(human_card):
                        eff_s = self.get_effective_suit(human_card)
                        trump_color = "red" if self.trump_suit in ['♥', '♦'] else "black"
                        led_color = "red" if eff_s in ['♥', '♦'] else "black"
                        
                        if led_color == trump_color and eff_s != self.trump_suit: 
                            has_green = any(not self.is_trump(self.hands[0][m]) and ("red" if self.get_effective_suit(self.hands[0][m]) in ['♥', '♦'] else "black") != trump_color for m in legal_moves)
                            if has_green:
                                synergy_warning = f"Sub-Optimal Defensive Lead: Opponents called trump and you led the {human_card.suit} ('Next' suit). This plays into the caller's likely hidden strength. Leading a 'Green' (opposite color) suit is mathematically safer."
                                if not self.sandbox_mode: self.stats_tracker.record_event("suboptimal_defensive_leads")

                if not self.is_trump(human_card) and human_card.rank in ['K', 'Q', 'J']:
                    eff_s = self.get_effective_suit(human_card)
                    
                    has_lower_in_hand = any(
                        self.get_effective_suit(self.hands[0][m]) == eff_s and RANKS_T.index(self.hands[0][m].rank) < RANKS_T.index(human_card.rank)
                        for m in legal_moves
                    )
                    
                    if has_lower_in_hand:
                        is_boss = True
                        all_played = self.played_cards + [c for _, c in self.trick]
                        
                        for r in ['A', 'K', 'Q', 'J']:
                            if RANKS_T.index(r) <= RANKS_T.index(human_card.rank): continue
                            c = Card(r, eff_s)
                            if c not in all_played and c not in self.hands[0]:
                                if self.trump_suit and c.rank == 'J' and c.suit == SAME_COLOR_T[self.trump_suit]: continue 
                                is_boss = False; break
                                
                        if not is_boss and synergy_warning is None: 
                            synergy_warning = f"Phantom Boss Risk: You led the {human_card} instead of your lower {eff_s}. If you are trying to win the trick, higher cards in that suit are still unplayed! Track the deck to avoid wasting power cards."
                            if not self.sandbox_mode: self.stats_tracker.record_event("phantom_boss_plays")

            if self.trick and synergy_warning is None:
                current_winner_idx = self.evaluate_trick()
                if current_winner_idx == 2: 
                    winning_card = next(c for p, c in self.trick if p == 2)
                    led_s = self.get_effective_suit(self.trick[0][1])
                    
                    def get_pwr(c, led_suit):
                        pwr = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}[c.rank]; eff_s = self.get_effective_suit(c)
                        if c.rank == 'J' and c.suit == self.trump_suit: pwr += 500
                        elif c.rank == 'J' and c.suit == SAME_COLOR_T[self.trump_suit]: pwr += 400
                        elif eff_s == self.trump_suit: pwr += 100
                        elif eff_s == led_suit: pwr += 50
                        else: pwr = 0
                        return pwr
                    
                    if len(legal_moves) > 1 and get_pwr(human_card, led_s) > get_pwr(winning_card, led_s):
                        had_lower = False
                        for m_idx in legal_moves:
                            alt_card = self.hands[0][m_idx]
                            if get_pwr(alt_card, led_s) < get_pwr(winning_card, led_s): had_lower = True; break
                        if had_lower:
                            if self.is_trump(human_card) and self.is_trump(winning_card):
                                synergy_warning = f"Wasted Trump Over-Ruff: Your partner already secured the trick with the {winning_card}. You stepped on them and wasted your {human_card}!"
                            else:
                                synergy_warning = f"Friendly Fire (Partner Over-Trump): Your partner already secured the trick with the {winning_card}. You wasted your {human_card} when you could have sloughed a lower card!"
        
        self._save_trick_snapshot()

        if getattr(self, "trainer_mode_var", None) and self.trainer_mode_var.get() and not self.autoplay_mode:
            if len(legal_moves) > 1:
                self.lbl_action.config(text="Calculating metrics..."); self.update_idletasks() 
                pack = self.ai_model.pack_ui_state(self) 
                def trainer_worker():
                    ranked_moves = self.ai_model.get_best_move(self, 0, return_all_moves=True, prepacked_state=state_pack)
                    best_idx = ranked_moves[0][0]; h_rate = 0; b_rate = ranked_moves[0][1]
                    for m_idx, rate in ranked_moves:
                        if m_idx == index: h_rate = rate; break
                    return best_idx, h_rate, b_rate

                state_pack = pack
                def apply_trainer_result(best_idx, h_rate, b_rate):
                    if b_rate > 0: self.hand_accuracy_sum += (h_rate/b_rate)*100
                    else: self.hand_accuracy_sum += 100
                    self.hand_accuracy_count += 1
                    self._apply_human_play_with_analysis(
                        index, best_idx, synergy_warning, (h_rate, b_rate))

                self._launch_search(
                    "trainer analysis", trainer_worker, apply_trainer_result)
                return
            else:
                self.hand_accuracy_sum += 100; self.hand_accuracy_count += 1
        
        self._apply_human_play_with_analysis(index, None, synergy_warning, None)

    def _apply_human_play_with_analysis(self, human_idx, best_idx, synergy_warning=None, acc_data=None):
        if (self.game_state != "playing" or self.current_turn != 0
                or human_idx < 0 or human_idx >= len(self.hands[0])):
            return
        self._invalidate_tasks()
        human_card = self.hands[0][human_idx]; trick_num = (len(self.played_cards) // 4) + 1
        self._record_session_event("play", {
            "player": 0, "profile": "Human", "card": str(human_card)})
        legal_indices = self.get_legal_moves(self.hands[0])
        if len(legal_indices) > 1:
            rank_values = {rank: value for value, rank in enumerate(RANKS_T)}
            ordered = sorted(
                legal_indices,
                key=lambda idx: (
                    self.is_trump(self.hands[0][idx]),
                    rank_values[self.hands[0][idx].rank]))
            if human_idx == ordered[0]:
                self.copycat_style_scores["Ironclad"] += 1.0
            elif human_idx == ordered[-1]:
                self.copycat_style_scores["Kyle"] += 1.0
            else:
                self.copycat_style_scores["Arbiter"] += 1.0
        
        if best_idx is not None:
            best_card = self.hands[0][best_idx]
            if human_card != best_card:
                led_s = self.get_effective_suit(self.trick[0][1]) if self.trick else None
                def get_pwr(c):
                    pwr = {'9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14}[c.rank]; eff_s = self.get_effective_suit(c)
                    if c.rank == 'J' and c.suit == self.trump_suit: pwr += 500
                    elif c.rank == 'J' and c.suit == SAME_COLOR_T[self.trump_suit]: pwr += 400
                    elif eff_s == self.trump_suit: pwr += 100
                    elif led_s and eff_s == led_s: pwr += 50
                    else: pwr = 0
                    return pwr
                    
                h_pwr = get_pwr(human_card); b_pwr = get_pwr(best_card)
                is_mistake = not (h_pwr == b_pwr and human_card.rank == best_card.rank)
                
                if is_mistake and acc_data:
                    h_rate, b_rate = acc_data
                    if (b_rate - h_rate) <= 3.0:  
                        is_mistake = False
                        
                if is_mistake:
                    explanation = self.generate_hint_explanation(best_card)
                    is_loner_defense = False
                    is_stranded_ace = False
                    
                    if self.is_loner and self.caller_idx in [1, 3]:
                        if human_card.rank in ['A', 'K', 'Q'] and best_card.rank not in ['A', 'K', 'Q']:
                            explanation = f"LONER DEFENSE BLUNDER: You threw away a potential stopper! When defending a loner, never discard Aces or high cards if you have safer trash cards to dump. {explanation}"
                            is_loner_defense = True
                            if not self.sandbox_mode: self.stats_tracker.record_event("loner_defense_blunders")
                    elif trick_num >= 4 and human_card.rank == 'A' and not self.is_trump(human_card):
                        explanation = f"STRANDED ACE: You held this off-suit Ace too long. MCTS indicates it is highly likely to be its trumped now that opponents have established voids. {explanation}"
                        is_stranded_ace = True
                        if not self.sandbox_mode: self.stats_tracker.record_event("stranded_aces")
                    
                    if not self.sandbox_mode and not is_loner_defense and not is_stranded_ace and (synergy_warning is None or "Trapped Left Bower" not in str(synergy_warning)):
                        self.stats_tracker.record_event("play_blunders")
                        
                    acc_str = f" [Acc: {acc_data[0]:.1f}% vs {acc_data[1]:.1f}%]" if acc_data else ""
                    self.trainer_mistakes.append({"text": f"Trick {trick_num}: You played {human_card}, but GM recommended {best_card}.{acc_str}\nReason: {explanation}\n", "trick_num": trick_num})

        if synergy_warning:
            if not self.sandbox_mode and "Trapped Left Bower" not in synergy_warning and "Failed Trump" not in synergy_warning and "Starving" not in synergy_warning: 
                self.stats_tracker.record_event("synergy_blunders")
            self.trainer_mistakes.append({"text": f"Trick {trick_num}: {synergy_warning}", "trick_num": trick_num})

        self.current_turn = -1; self.lbl_action.config(text="")
        self.hands[0].remove(human_card); self.cached_hint = None 
        
        if self.trick:
            led = self.get_effective_suit(self.trick[0][1])
            if self.get_effective_suit(human_card) != led: self.voids[0].add(led)
                
        self.trick.append((0, human_card)); SoundFX.play_card(); self.update_table_graphics(); self.current_turn = 1 
        self._set_controls_state(tk.NORMAL); self.render_human_hand(); self.check_trick_end()

    def play_ai_turns(self):
        if self._tournament_paused():
            return
        if self.game_state == "playing" and len(self.trick) < (3 if self.is_loner else 4): 
            self.current_turn = active_turn_seat(
                self.current_turn, self.is_loner, self.loner_partner_idx)
            if self.current_turn != 0 or self.autoplay_mode: 
                self.lbl_action.config(text=f"{self.get_player_display_name(self.current_turn)} is thinking...")
                self.after(self._action_delay_ms(), self._execute_ai_turn_async) 

    def _execute_ai_turn_async(self):
        if self._tournament_paused():
            return
        normalized_turn = active_turn_seat(
            self.current_turn, self.is_loner, self.loner_partner_idx)
        if normalized_turn != self.current_turn:
            self.current_turn = normalized_turn
            self.check_trick_end()
            return
        if (self.current_turn == 0 and not self.autoplay_mode) or len(self.trick) == (3 if self.is_loner else 4): return 

        player_idx = self.current_turn; hand = self.hands[player_idx]; legal_moves_indices = self.get_legal_moves(hand)
        if len(legal_moves_indices) == 1: self._apply_ai_move(player_idx, legal_moves_indices[0]); return
        
        prof = self.ai_profiles.get(str(player_idx), "Human")
        if prof in HYBRID_MCTS_PROFILES:
            base_iterations = self.table_neural_play_iters
            if prof in {"Monte Prime", "Iron Oracle"}:
                iterations = max(base_iterations * 3, 600)
            elif (prof in {"Iron Clutch", "Iron Endgame Edge"}
                  and self.team1_tricks + self.team2_tricks >= 3):
                iterations = max(base_iterations * 5, 1000)
            elif (prof == "Iron Solver"
                  and self.team1_tricks + self.team2_tricks >= 3):
                iterations = max(base_iterations * 6, 1200)
            else:
                iterations = max(base_iterations * 2, 400)
            known_hands = self._get_autoplay_known_hands(player_idx)
            state_pack = self.ai_model.pack_ui_state(self)
            self._launch_search(
                f"{prof} table play",
                lambda: self.get_cheems_best_move(
                    player_idx, known_hands=known_hands,
                    iterations=iterations, state_pack=state_pack),
                lambda action_idx, confidence: self._apply_ai_move(
                    player_idx, action_idx, confidence))
            return
        if prof in NEURAL_PROFILES and prof not in HYBRID_MCTS_PROFILES:
            known_hands = self._get_autoplay_known_hands(player_idx)
            state_pack = self.ai_model.pack_ui_state(self)
            self._launch_search(
                f"{prof} table play",
                lambda: self.get_cheems_best_move(
                    player_idx, known_hands=known_hands,
                    state_pack=state_pack),
                lambda action_idx, confidence: self._apply_ai_move(
                    player_idx, action_idx, confidence))
            return

        if prof != "The MC" and prof not in HYBRID_MCTS_PROFILES:
            dump_idx = self.get_deterministic_dump_move(player_idx, legal_moves_indices)
            if dump_idx is not None: self._apply_ai_move(player_idx, dump_idx); return

        self._launch_search(
            f"{prof} table play",
            lambda: self.ai_model.get_best_move(self, player_idx),
            lambda action_idx: self._apply_ai_move(player_idx, action_idx))

    def _apply_ai_move(self, player_idx, action_idx, confidence=None):
        if (self.game_state != "playing" or player_idx != self.current_turn
                or action_idx < 0 or action_idx >= len(self.hands[player_idx])):
            return
        self._invalidate_tasks()
        self.lbl_action.config(text=""); hand = self.hands[player_idx]
        played_card = hand[action_idx]
        self._record_session_event("play", {
            "player": player_idx,
            "profile": self.ai_profiles.get(str(player_idx), "Unknown"),
            "card": str(played_card),
            "legal_actions": [str(hand[index]) for index in self.get_legal_moves(hand)],
            "confidence": confidence})
        hand.remove(played_card)
        if self.trick:
            led = self.get_effective_suit(self.trick[0][1])
            if self.get_effective_suit(played_card) != led: self.voids[player_idx].add(led)
        self.trick.append((player_idx, played_card)); SoundFX.play_card() 
        self.update_table_graphics(); self.current_turn = (self.current_turn + 1) % 4; self.check_trick_end()

    def update_dealer_chip(self):
        positions = {0: (0.5, 0.85), 1: (0.05, 0.60), 2: (0.5, 0.16), 3: (0.95, 0.60)}
        self.dealer_canvas.place(relx=positions[self.dealer_idx][0], rely=positions[self.dealer_idx][1], anchor=tk.CENTER)

    def update_scoreboard(self):
        if self.lbl_game_score and self.lbl_tricks:
            self.lbl_game_score.config(text=f"GAME: {self.get_player_display_name(0)} & {self.get_player_display_name(2)} {self.team1_score} | {self.get_player_display_name(1)} & {self.get_player_display_name(3)} {self.team2_score}")
            self.lbl_tricks.config(text=f"TRICKS: Your Team {self.team1_tricks} | Opponents {self.team2_tricks}")

    def evaluate_trick(self):
        return trick_winner(self.trick, self.trump_suit)

    def check_trick_end(self):
        if self._tournament_paused():
            return
        if len(self.trick) == (3 if self.is_loner else 4): self.after(1500, self._resolve_trick)
        elif self.current_turn == 0 and not self.autoplay_mode: 
            if self.loner_partner_idx == 0: self.current_turn = 1; self.play_ai_turns()
            else: self.render_human_hand() 
        else: self.play_ai_turns()

    def _resolve_trick(self):
        if self._tournament_paused():
            return
        for _, card in self.trick: self.played_cards.append(card)
        winner_idx = self.evaluate_trick()
        if winner_idx in [0, 2]: self.team1_tricks += 1
        else: self.team2_tricks += 1
        SoundFX.trick_won(); self.update_scoreboard(); self.trick = []; self.update_table_graphics(); self.current_turn = winner_idx
        if (self.team1_tricks + self.team2_tricks) == 5: self.after(1000, self._evaluate_hand) 
        else: 
            if self.current_turn == 0 and not self.autoplay_mode: self.render_human_hand()
            self.play_ai_turns()

    def _evaluate_hand(self):
        if self._tournament_paused():
            return
        if getattr(self, 'is_rewind_mode', False):
            msg = f"Alternate Reality Complete!\n\nIn this timeline, your team won {self.team1_tricks} tricks."
            messagebox.showinfo("Rewind Finished", msg)
            self.is_rewind_mode = False
            self._show_end_hand_dashboard()
            return
            
        caller_idx_val = self.caller_idx
        caller_team = 1 if caller_idx_val in [0, 2] else 2
        winning_team, points_awarded = calculate_hand_score(
            caller_idx_val, self.is_loner,
            self.team1_tricks, self.team2_tricks)
        team1_won = winning_team == 1
        if team1_won:
            self.team1_score += points_awarded
        else:
            self.team2_score += points_awarded

        if self.tournament_state is not None:
            tournament = self.tournament_state
            tournament["hands"] += 1
            if caller_team == 1 and self.team1_tricks < 3:
                tournament["euchres_b"] += 1
            elif caller_team == 2 and self.team2_tricks < 3:
                tournament["euchres_a"] += 1
            if self.is_loner:
                suffix = "a" if caller_team == 1 else "b"
                tournament[f"loners_{suffix}"] += 1
                caller_tricks = (
                    self.team1_tricks if caller_team == 1 else self.team2_tricks)
                if caller_tricks == 5:
                    tournament[f"loner_sweeps_{suffix}"] += 1

        self.session_journal.hands_completed += 1
        self._record_session_event("hand_complete", {
            "caller": caller_idx_val, "trump": self.trump_suit,
            "team1_tricks": self.team1_tricks,
            "team2_tricks": self.team2_tricks,
            "points": points_awarded,
        })

        if team1_won: SoundFX.round_win(points_awarded)
        else: SoundFX.round_lose()
        
        if not self.sandbox_mode and "Drill" not in self.active_drill:
            self.stats_tracker.record_event("hands_played")
            if team1_won: self.stats_tracker.record_event("total_points_earned", points_awarded)
            if caller_idx_val == 0:
                if self.team1_tricks < 3: self.stats_tracker.record_event("got_euchred")
                elif self.team1_tricks == 5:
                    self.stats_tracker.record_event("took_all_5")
                    if self.is_loner: self.stats_tracker.record_event("took_5_alone")
        
        self.update_scoreboard()
        if getattr(self, "trainer_mode_var", None) and self.trainer_mode_var.get() and not self.autoplay_mode: self._show_end_hand_dashboard()
        else: self._check_game_over()

    def _show_end_hand_dashboard(self):
        dialog = tk.Toplevel(self); dialog.title("Grandmaster Post-Hand Report"); dialog.geometry("560x550"); dialog.configure(bg=self.coach_bg_color); dialog.transient(self); dialog.grab_set()
        tk.Label(dialog, text="Post-Hand Analysis", font=("Arial", 16, "bold"), bg=self.coach_bg_color, fg="white").pack(pady=10)
        
        stat_frame = tk.Frame(dialog, bg=self.coach_bg_color); stat_frame.pack(fill=tk.X, padx=20)
        acc = (self.hand_accuracy_sum / self.hand_accuracy_count) if self.hand_accuracy_count > 0 else 100.0
        tk.Label(stat_frame, text=f"Play Accuracy: {acc:.1f}%", font=("Arial", 12, "bold"), bg=self.coach_bg_color, fg="#32CD32" if acc>90 else "#ffcc00").pack(side=tk.LEFT)
        
        if self.hand_expected_tricks >= 0:
            diff = self.team1_tricks - self.hand_expected_tricks
            luck_str = f"You won {self.team1_tricks} tricks. Expected: {self.hand_expected_tricks:.1f}. "
            if diff >= 0.5: luck_str += "(Lucky Overperformance)"
            elif diff <= -0.5: luck_str += "(Unlucky Anomaly)"
            else: luck_str += "(Statistically Standard)"
            tk.Label(stat_frame, text=luck_str, font=("Arial", 10, "italic"), bg=self.coach_bg_color, fg="white").pack(side=tk.RIGHT)

        if hasattr(self, 'hand_bid_feedback') and self.hand_bid_feedback:
            feedback_color = "#32CD32" if "Good" in self.hand_bid_feedback or "Solid" in self.hand_bid_feedback else "#ffcc00"
            tk.Label(dialog, text=self.hand_bid_feedback, font=("Arial", 11, "bold"), bg=self.coach_bg_color, fg=feedback_color).pack(pady=5)

        frame = tk.Frame(dialog, bg=self.dark_bg_color, bd=2, relief=tk.SUNKEN); frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        if self.trainer_mistakes:
            canvas = tk.Canvas(frame, bg=self.dark_bg_color, highlightthickness=0); scrollbar = tk.Scrollbar(frame, orient="vertical", command=canvas.yview)
            scrollable_frame = tk.Frame(canvas, bg=self.dark_bg_color); scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=scrollable_frame, anchor="nw"); canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side="left", fill="both", expand=True); scrollbar.pack(side="right", fill="y")
            
            for m in self.trainer_mistakes:
                mf = tk.Frame(scrollable_frame, bg=self.dark_bg_color); mf.pack(fill=tk.X, pady=5)
                tk.Label(mf, text=m["text"], font=("Arial", 10), bg=self.dark_bg_color, fg="#ffcc00", justify=tk.LEFT, wraplength=400).pack(side=tk.LEFT, padx=10)
                tk.Button(mf, text="? Rewind Here", font=("Arial", 8, "bold"), bg="#1E90FF", fg="white", command=lambda t=m["trick_num"], d=dialog: self._rewind_to_trick(t, d)).pack(side=tk.RIGHT, padx=10)
        else: tk.Label(frame, text="Flawless Hand! The Grandmaster agreed with all of your decisions.", font=("Arial", 12, "bold"), bg=self.dark_bg_color, fg="lightgreen", wraplength=420, justify=tk.CENTER).pack(pady=40, padx=10)

        def next_hand(): dialog.destroy(); self.sandbox_mode = False; self._check_game_over()
        def replay_hand():
            dialog.destroy(); self.sandbox_mode = True
            if self.team1_tricks >= 3:
                pts = 4 if (self.caller_idx in [0,2] and self.is_loner and self.team1_tricks == 5) else (2 if self.team1_tricks == 5 else 1)
                self.team1_score -= pts
            elif self.team2_tricks >= 3:
                pts = 4 if (self.caller_idx in [1,3] and self.is_loner and self.team2_tricks == 5) else (2 if self.team2_tricks == 5 else 1)
                self.team2_score -= pts
            self.update_scoreboard(); self.start_new_hand()

        btn_frame = tk.Frame(dialog, bg=self.coach_bg_color); btn_frame.pack(pady=15)
        if self.trainer_mistakes: tk.Button(btn_frame, text="?? Replay Hand (Sandbox Mode)", font=("Arial", 12, "bold"), bg="#FF8C00", fg="black", command=replay_hand).pack(side=tk.LEFT, padx=10)
        tk.Button(btn_frame, text="Next Hand ??", font=("Arial", 12, "bold"), bg="lightgreen", fg="black", command=next_hand).pack(side=tk.LEFT, padx=10)

    def _check_game_over(self):
        if self.active_drill != "Standard Match":
            if not self.sandbox_mode:
                scenario = getattr(self, "current_drill_scenario", self.active_drill)
                result = "You have finished the scenario drill."
                if self.active_drill == "Drill: Mystery Scenario":
                    result = "Mystery scenario complete. Its identity remains hidden."
                elif scenario == "Drill: Loner Defense":
                    result = ("Stopper preserved: the loner did not sweep."
                              if self.team1_tricks >= 1 else
                              "The loner swept the hand. Try preserving a stronger stopper.")
                elif scenario == "Drill: Euchre or Bust":
                    result = ("Success: your team euchred the callers."
                              if self.team1_tricks >= 3 else
                              "The callers made their contract; the euchre attempt fell short.")
                elif scenario == "Drill: Call It and Prove It":
                    result = (f"Contract made with {self.team1_tricks} tricks."
                              if self.team1_tricks >= 3 else
                              f"Euchred after taking {self.team1_tricks} tricks.")
                    if self.hand_bid_feedback:
                        result += f"\n\n{self.hand_bid_feedback}"
                messagebox.showinfo(
                    "Drill Complete",
                    result + "\n\nThe score will reset and a new drill will begin.")
            self.team1_score = 0; self.team2_score = 0; self.sandbox_mode = False
        else:
            if (getattr(self, "human_league_game_active", False)
                    and (self.team1_score >= 10 or self.team2_score >= 10)):
                self._finish_human_league_game()
                return
            if (self.tournament_state is not None
                    and (self.team1_score >= 10 or self.team2_score >= 10)):
                self._finish_tournament_game()
                return
            if self.team1_score >= 10:
                if not self.sandbox_mode: 
                    self.stats_tracker.record_event("games_completed")
                    self.stats_tracker.record_event("games_won")
                    self.stats_tracker.apply_decay()
                self.session_journal.games_completed += 1
                self._record_session_event("game_complete", {"winner": "team1"})
                messagebox.showinfo("Game Over", f"A WINNER IS YOU, {PLAYER_NAMES[0]}!"); self.team1_score = 0; self.team2_score = 0; self.sandbox_mode = False
            elif self.team2_score >= 10:
                if not self.sandbox_mode: 
                    self.stats_tracker.record_event("games_completed")
                    self.stats_tracker.apply_decay()
                self.session_journal.games_completed += 1
                self._record_session_event("game_complete", {"winner": "team2"})
                messagebox.showinfo("Game Over", "Carry this L and try again"); self.team1_score = 0; self.team2_score = 0; self.sandbox_mode = False
        self.dealer_idx = (self.dealer_idx + 1) % 4; self.start_new_hand()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = EuchreGame()
    app.mainloop()

