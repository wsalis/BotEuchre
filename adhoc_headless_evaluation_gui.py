"""
Small GUI launcher for adhoc_headless_evaluation.py.

Run with:
  python adhoc_headless_evaluation_gui.py
"""

import os
import json
import itertools
import queue
import random
import re
import signal
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
from tkinter import filedialog, messagebox, ttk

from BotEuchreGUI import (
    DATA_SCHEMA_VERSION, HEADLESS_TOURNAMENT_PROFILES, HELP_TOPICS,
    NODE_ADHOC_HISTORY_PATH, NODE_DEAL_LEDGER_PATH, NODE_ID, NODE_STATE_DIR,
    atomic_write_json, load_versioned_list, load_versioned_mapping,
    prepare_node_state, save_versioned_list)
from adhoc_headless_evaluation import planned_paired_deals


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NEURAL_SCRIPT = os.path.join(BASE_DIR, "adhoc_headless_evaluation.py")
LEGACY_JOB_QUEUE_PATH = os.path.join(BASE_DIR, "bot_euchre_headless_jobs.json")
LEGACY_LAB_SETTINGS_PATH = os.path.join(
    BASE_DIR, "bot_euchre_tournament_lab_settings.json")
JOB_QUEUE_PATH = os.path.join(NODE_STATE_DIR, "bot_euchre_headless_jobs.json")
LAB_SETTINGS_PATH = os.path.join(
    NODE_STATE_DIR, "bot_euchre_tournament_lab_settings.json")
LAB_SETTINGS_DEFAULTS = {
    "ledger_path": NODE_DEAL_LEDGER_PATH,
    "early_stop_min_deals": "0",
    "max_runtime_minutes": "360",
    "stall_minutes": "30",
    "randomize_teams": False,
    "round_robin_hands": "200",
    "round_robin_label_prefix": "round_robin",
    "shared_queue_enabled": False,
    "shared_queue_path": os.path.join(
        NODE_STATE_DIR, "bot_euchre_headless_jobs.sqlite3"),
    "shared_queue_lease_minutes": "30",
}


def _sqlite_connect(path):
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    # WAL is fragile on network shares (especially mixed Windows/macOS SMB).
    # Use rollback journal mode for safer cross-host locking semantics.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _run_with_sqlite_retry(operation, retries=5, delay_seconds=0.2):
    for attempt in range(retries):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if attempt == retries - 1:
                raise
            time.sleep(delay_seconds * (attempt + 1))


def _shared_queue_init(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _sqlite_connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                label TEXT,
                match TEXT,
                created_at REAL,
                started_at REAL,
                finished_at REAL,
                return_code INTEGER,
                failure_reason TEXT,
                lease_owner TEXT,
                lease_expires_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                command_json TEXT NOT NULL
            )
            """)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_queue_jobs_status_lease
            ON queue_jobs(status, lease_expires_at, created_at)
            """)


def _shared_queue_list(path):
    _shared_queue_init(path)
    def operation():
        with _sqlite_connect(path) as conn:
            return conn.execute(
                """
                SELECT id, status, label, match, created_at, started_at, finished_at,
                       return_code, failure_reason, lease_owner, lease_expires_at,
                       attempts, command_json
                FROM queue_jobs
                ORDER BY created_at ASC
                """).fetchall()
    rows = _run_with_sqlite_retry(operation)
    jobs = []
    for row in rows:
        job = dict(row)
        try:
            job["command"] = json.loads(job.pop("command_json"))
        except json.JSONDecodeError:
            job["command"] = []
        jobs.append(job)
    return jobs


def _shared_queue_enqueue(path, job):
    _shared_queue_init(path)
    def operation():
        with _sqlite_connect(path) as conn:
            conn.execute(
                """
                INSERT INTO queue_jobs(
                    id, status, label, match, created_at, command_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"],
                    job.get("status", "queued"),
                    job.get("label", ""),
                    job.get("match", ""),
                    float(job.get("created_at", time.time())),
                    json.dumps(job.get("command", []), ensure_ascii=False),
                ),
            )
    _run_with_sqlite_retry(operation)


def _shared_queue_remove(path, job_ids):
    if not job_ids:
        return
    _shared_queue_init(path)
    placeholders = ",".join("?" for _ in job_ids)
    def operation():
        with _sqlite_connect(path) as conn:
            conn.execute(
                f"""
                DELETE FROM queue_jobs
                WHERE id IN ({placeholders})
                  AND status != 'running'
                """,
                tuple(job_ids),
            )
    _run_with_sqlite_retry(operation)


def _shared_queue_purge(path, include_running=False, stale_running_only=False):
    _shared_queue_init(path)
    counts = {"removed": 0, "running": 0, "stale_running": 0}
    def operation():
        nonlocal counts
        with _sqlite_connect(path) as conn:
            now = time.time()
            counts["running"] = conn.execute(
                "SELECT COUNT(*) FROM queue_jobs WHERE status='running'").fetchone()[0]
            counts["stale_running"] = conn.execute(
                """
                SELECT COUNT(*) FROM queue_jobs
                WHERE status='running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (now,),
            ).fetchone()[0]

            if include_running:
                counts["removed"] = conn.execute(
                    "DELETE FROM queue_jobs").rowcount
            elif stale_running_only:
                counts["removed"] = conn.execute(
                    """
                    DELETE FROM queue_jobs
                    WHERE status!='running'
                       OR (status='running'
                           AND lease_expires_at IS NOT NULL
                           AND lease_expires_at < ?)
                    """,
                    (now,),
                ).rowcount
            else:
                counts["removed"] = conn.execute(
                    "DELETE FROM queue_jobs WHERE status!='running'").rowcount
    _run_with_sqlite_retry(operation)
    return counts


def _shared_queue_claim_next(path, owner_id, lease_seconds):
    _shared_queue_init(path)
    claimed = None
    def operation():
        nonlocal claimed
        now = time.time()
        with _sqlite_connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, command_json
                FROM queue_jobs
                WHERE status IN ('queued', 'interrupted')
                   OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                conn.commit()
                claimed = None
                return
            conn.execute(
                """
                UPDATE queue_jobs
                SET status='running',
                    started_at=?,
                    lease_owner=?,
                    lease_expires_at=?,
                    attempts=attempts+1,
                    finished_at=NULL,
                    return_code=NULL,
                    failure_reason=NULL
                WHERE id=?
                """,
                (now, owner_id, now + lease_seconds, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM queue_jobs WHERE id=?",
                (row["id"],),
            ).fetchone()
            conn.commit()
    _run_with_sqlite_retry(operation)
    if claimed is None:
        return None
    job = dict(claimed)
    try:
        job["command"] = json.loads(job.pop("command_json"))
    except json.JSONDecodeError:
        job["command"] = []
    return job


def _shared_queue_heartbeat(path, job_id, owner_id, lease_seconds):
    _shared_queue_init(path)
    def operation():
        now = time.time()
        with _sqlite_connect(path) as conn:
            conn.execute(
                """
                UPDATE queue_jobs
                SET lease_expires_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (now + lease_seconds, job_id, owner_id),
            )
    _run_with_sqlite_retry(operation)


def _shared_queue_finish(path, job_id, owner_id, return_code, failure_reason=None):
    _shared_queue_init(path)
    def operation():
        now = time.time()
        status = "completed" if return_code == 0 else "failed"
        with _sqlite_connect(path) as conn:
            conn.execute(
                """
                UPDATE queue_jobs
                SET status=?,
                    finished_at=?,
                    return_code=?,
                    failure_reason=?,
                    lease_owner=NULL,
                    lease_expires_at=NULL
                WHERE id=? AND lease_owner=?
                """,
                (status, now, int(return_code), failure_reason, job_id, owner_id),
            )
    _run_with_sqlite_retry(operation)

def prepare_lab_state():
    prepare_node_state()
    if not os.path.exists(LAB_SETTINGS_PATH) and os.path.exists(
            LEGACY_LAB_SETTINGS_PATH):
        try:
            import shutil
            shutil.copy2(LEGACY_LAB_SETTINGS_PATH, LAB_SETTINGS_PATH)
        except OSError:
            pass
    if not os.path.exists(JOB_QUEUE_PATH) and os.path.exists(
            LEGACY_JOB_QUEUE_PATH):
        try:
            os.replace(LEGACY_JOB_QUEUE_PATH, JOB_QUEUE_PATH)
        except OSError:
            pass


def select_tournament_competitors(
        options, model_a, model_b, randomize=False, chooser=random.sample):
    if len(options) < 2:
        raise ValueError("At least two tournament profiles are required.")
    if randomize:
        return tuple(chooser(list(options), 2))
    if model_a not in options or model_b not in options:
        raise ValueError("Choose two available tournament profiles.")
    if model_a == model_b:
        raise ValueError("Choose two different tournament profiles.")
    return model_a, model_b


def load_job_queue(filename=JOB_QUEUE_PATH):
    try:
        jobs = load_versioned_list(
            filename, "bot-euchre-headless-jobs", "jobs")
    except (OSError, TypeError, ValueError):
        return []
    for job in jobs:
        if job.get("status") == "running":
            job["status"] = "interrupted"
    return jobs


def save_job_queue(jobs, filename=JOB_QUEUE_PATH):
    save_versioned_list(
        filename, "bot-euchre-headless-jobs", jobs, "jobs")


def load_lab_settings(filename=LAB_SETTINGS_PATH):
    try:
        return load_versioned_mapping(
            filename, "bot-euchre-tournament-lab-settings",
            LAB_SETTINGS_DEFAULTS)
    except (OSError, TypeError, ValueError):
        return dict(LAB_SETTINGS_DEFAULTS)


def save_lab_settings(settings, filename=LAB_SETTINGS_PATH):
    payload = dict(settings)
    payload.update({
        "_schema": "bot-euchre-tournament-lab-settings",
        "_schema_version": DATA_SCHEMA_VERSION,
    })
    atomic_write_json(filename, payload)


class EvalGui(tk.Tk):
    def __init__(self):
        super().__init__()
        prepare_lab_state()
        self.title(f"Bot Euchre Tournament Lab - {NODE_ID}")
        self.geometry("940x720")
        self.minsize(860, 640)

        self.output_queue = queue.Queue()
        self.process = None
        self.worker_thread = None
        self.current_job_id = None
        self.lab_settings = load_lab_settings()
        self.jobs = []
        self.queue_owner_id = (
            f"{NODE_ID}@{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}")
        self.last_lease_heartbeat_at = 0.0

        self.competitor_options = list(HEADLESS_TOURNAMENT_PROFILES)
        self.model_a_var = tk.StringVar(value=self.default_model_a())
        self.model_b_var = tk.StringVar(value=self.default_model_b())
        self.randomize_teams_var = tk.BooleanVar(
            value=self.lab_settings.get("randomize_teams", False))
        self.hands_var = tk.StringVar(value="1000")
        self.mcts_a_var = tk.StringVar(value="200")
        self.mcts_b_var = tk.StringVar(value="200")
        self.bid_a_var = tk.StringVar(value="100")
        self.bid_b_var = tk.StringVar(value="100")
        self.worker_multiplier_var = tk.StringVar(value="6")
        self.seed_var = tk.StringVar(value="20260801")
        self.label_var = tk.StringVar(value="mac_adhoc")
        self.log_var = tk.StringVar(value=NODE_ADHOC_HISTORY_PATH)
        self.ledger_var = tk.StringVar(
            value=self.lab_settings.get("ledger_path", NODE_DEAL_LEDGER_PATH))
        self.round_robin_hands_var = tk.StringVar(
            value=self.lab_settings.get("round_robin_hands", "200"))
        self.round_robin_label_prefix_var = tk.StringVar(
            value=self.lab_settings.get("round_robin_label_prefix", "round_robin"))
        self.shared_queue_enabled_var = tk.BooleanVar(
            value=bool(self.lab_settings.get("shared_queue_enabled", False)))
        self.shared_queue_path_var = tk.StringVar(
            value=self.lab_settings.get(
                "shared_queue_path",
                LAB_SETTINGS_DEFAULTS["shared_queue_path"]))
        self.shared_queue_lease_minutes_var = tk.StringVar(
            value=self.lab_settings.get("shared_queue_lease_minutes", "30"))
        self.early_stop_var = tk.StringVar(
            value=self.lab_settings.get("early_stop_min_deals", "0"))
        self.max_runtime_var = tk.StringVar(
            value=self.lab_settings.get("max_runtime_minutes", "360"))
        self.stall_minutes_var = tk.StringVar(
            value=self.lab_settings.get("stall_minutes", "30"))
        self.process_started_at = None
        self.last_output_at = None
        self.watchdog_reason = None

        self.create_widgets()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._load_jobs_from_backend()
        self.refresh_job_queue()
        self.after(100, self.drain_output_queue)
        self.after(1000, self.watchdog_tick)

    def default_model_a(self):
        return "Arbiter"

    def default_model_b(self):
        return "Ironclad"

    def create_widgets(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)

        controls = ttk.LabelFrame(outer, text="Tournament Setup", padding=12)
        controls.pack(fill=tk.X)
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        ttk.Label(controls, text="TEAM 1", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(controls, text="TEAM 2", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=2, columnspan=2, sticky="w", padx=(24, 0), pady=(0, 6))

        ttk.Label(controls, text="Competitor").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.model_a_combo = ttk.Combobox(controls, textvariable=self.model_a_var, values=self.competitor_options, state="readonly")
        self.model_a_combo.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(controls, text="Competitor").grid(row=1, column=2, sticky="w", padx=(24, 8), pady=4)
        self.model_b_combo = ttk.Combobox(controls, textvariable=self.model_b_var, values=self.competitor_options, state="readonly")
        self.model_b_combo.grid(row=1, column=3, sticky="ew", pady=4)

        ttk.Checkbutton(
            controls, text="Randomize teams for each started or queued tournament",
            variable=self.randomize_teams_var).grid(
                row=2, column=0, columnspan=4, sticky="w", pady=(6, 2))

        ttk.Label(controls, text="Play MCTS iterations").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(controls, textvariable=self.mcts_a_var, width=12).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(controls, text="Play MCTS iterations").grid(row=3, column=2, sticky="w", padx=(24, 8), pady=4)
        ttk.Entry(controls, textvariable=self.mcts_b_var, width=12).grid(row=3, column=3, sticky="w", pady=4)

        ttk.Label(controls, text="Bid rollouts / suit sims").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(controls, textvariable=self.bid_a_var, width=12).grid(row=4, column=1, sticky="w", pady=4)
        ttk.Label(controls, text="Bid rollouts / suit sims").grid(row=4, column=2, sticky="w", padx=(24, 8), pady=4)
        ttk.Entry(controls, textvariable=self.bid_b_var, width=12).grid(row=4, column=3, sticky="w", pady=4)

        runtime = ttk.LabelFrame(outer, text="Run Settings", padding=12)
        runtime.pack(fill=tk.X, pady=(10, 0))
        for column in (1, 3):
            runtime.columnconfigure(column, weight=1)

        ttk.Label(runtime, text="Total games").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.hands_var, width=12).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(runtime, text="Run label").grid(row=0, column=2, sticky="w", padx=(24, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.label_var).grid(row=0, column=3, sticky="ew", pady=4)

        ttk.Label(runtime, text="Round robin games / matchup").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.round_robin_hands_var, width=12).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(runtime, text="Round robin label prefix").grid(row=1, column=2, sticky="w", padx=(24, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.round_robin_label_prefix_var).grid(row=1, column=3, sticky="ew", pady=4)

        ttk.Label(runtime, text="Worker multiplier").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.worker_multiplier_var, width=12).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(runtime, text="JSONL log").grid(row=2, column=2, sticky="w", padx=(24, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.log_var).grid(row=2, column=3, sticky="ew", pady=4)
        ttk.Button(runtime, text="Browse", command=self.browse_log).grid(row=2, column=4, padx=(8, 0), pady=4)
        ttk.Label(runtime, text="Reproducible seed").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.seed_var, width=18).grid(
            row=3, column=1, sticky="w", pady=4)
        ttk.Label(runtime, text="Each deal is mirrored with teams swapped.").grid(
            row=3, column=2, columnspan=2, sticky="w", padx=(24, 0), pady=4)
        ttk.Label(runtime, text="Per-deal ledger").grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.ledger_var).grid(
            row=4, column=1, sticky="ew", pady=4)
        ttk.Label(runtime, text="Early stop after deals (0=off)").grid(
            row=4, column=2, sticky="w", padx=(24, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.early_stop_var, width=12).grid(
            row=4, column=3, sticky="w", pady=4)
        ttk.Label(runtime, text="Max runtime / silent minutes").grid(
            row=5, column=0, sticky="w", padx=(0, 8), pady=4)
        watchdog = ttk.Frame(runtime)
        watchdog.grid(row=5, column=1, sticky="w", pady=4)
        ttk.Entry(watchdog, textvariable=self.max_runtime_var, width=8).pack(side=tk.LEFT)
        ttk.Label(watchdog, text=" / ").pack(side=tk.LEFT)
        ttk.Entry(watchdog, textvariable=self.stall_minutes_var, width=8).pack(side=tk.LEFT)

        ttk.Checkbutton(
            runtime, text="Use shared SQLite queue (cluster mode)",
            variable=self.shared_queue_enabled_var,
            command=self._on_shared_queue_toggle).grid(
                row=6, column=0, columnspan=4, sticky="w", pady=(6, 2))
        ttk.Label(runtime, text="Shared queue DB / lease minutes").grid(
            row=7, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(runtime, textvariable=self.shared_queue_path_var).grid(
            row=7, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(runtime, text="Browse", command=self.browse_shared_queue).grid(
            row=7, column=3, sticky="w", pady=4)
        ttk.Entry(runtime, textvariable=self.shared_queue_lease_minutes_var, width=8).grid(
            row=7, column=4, sticky="w", padx=(8, 0), pady=4)

        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=(12, 8))
        self.start_button = ttk.Button(actions, text="Start Evaluation", command=self.start_evaluation)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop_evaluation, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Clear Output", command=self.clear_output).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Queue Current", command=self.queue_current).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions, text="Queue Full Round Robin",
            command=self.queue_full_round_robin).pack(side=tk.LEFT, padx=(8, 0))
        self.run_queue_button = ttk.Button(actions, text="Run Queue", command=self.run_next_job)
        self.run_queue_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions, text="Sample Planner", command=self.show_sample_planner).pack(
                side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions, text="Help", command=self.show_lab_help).pack(
                side=tk.LEFT, padx=(8, 0))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(actions, textvariable=self.status_var).pack(side=tk.RIGHT)

        queue_frame = ttk.LabelFrame(outer, text="Persistent Job Queue", padding=8)
        queue_frame.pack(fill=tk.X, pady=(0, 8))
        self.job_tree = ttk.Treeview(
            queue_frame, columns=("status", "label", "match", "created"),
            show="headings", height=4)
        for column, title, width in [
                ("status", "Status", 90), ("label", "Label", 150),
                ("match", "Match", 390), ("created", "Created", 140)]:
            self.job_tree.heading(column, text=title)
            self.job_tree.column(column, width=width, anchor=tk.W)
        self.job_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            queue_frame, text="Remove", command=self.remove_selected_job).pack(
                side=tk.RIGHT, padx=(8, 0))
        ttk.Button(
            queue_frame, text="Purge Queue", command=self.purge_queue).pack(
                side=tk.RIGHT, padx=(8, 0))

        output_frame = ttk.LabelFrame(outer, text="Evaluator Output", padding=8)
        output_frame.pack(fill=tk.BOTH, expand=True)
        output_frame.rowconfigure(0, weight=1)
        output_frame.columnconfigure(0, weight=1)

        self.output_text = tk.Text(output_frame, wrap=tk.WORD, height=18)
        self.output_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(output_frame, command=self.output_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.output_text.configure(yscrollcommand=scrollbar.set)

    def browse_log(self):
        path = filedialog.asksaveasfilename(
            title="Select log file",
            defaultextension=".jsonl",
            filetypes=[("JSONL files", "*.jsonl"), ("All files", "*.*")],
        )
        if path:
            self.log_var.set(os.path.relpath(path, BASE_DIR))

    def browse_shared_queue(self):
        path = filedialog.asksaveasfilename(
            title="Select shared queue DB",
            defaultextension=".sqlite3",
            filetypes=[("SQLite DB", "*.sqlite3"), ("All files", "*.*")],
        )
        if path:
            self.shared_queue_path_var.set(path)

    def _shared_queue_enabled(self):
        return bool(self.shared_queue_enabled_var.get())

    def _shared_queue_path(self):
        return self.shared_queue_path_var.get().strip()

    def _shared_queue_lease_seconds(self):
        minutes = self.read_positive_int(
            self.shared_queue_lease_minutes_var.get(),
            "Shared queue lease minutes")
        return int(minutes * 60)

    def _normalize_shared_command(self, command):
        if not isinstance(command, list) or not command:
            return command

        cmd = [str(part) for part in command]

        # Always run with this node's interpreter when consuming shared jobs.
        cmd[0] = sys.executable

        # Replace foreign absolute script paths with the local evaluator script.
        script_index = None
        for idx, token in enumerate(cmd):
            if token.lower().endswith("adhoc_headless_evaluation.py"):
                script_index = idx
                break
        if script_index is not None:
            cmd[script_index] = NEURAL_SCRIPT

        def path_looks_windows(raw):
            value = str(raw)
            return (len(value) >= 3 and value[1] == ":" and value[2] in "\\/")

        def should_fallback_path(raw_path):
            value = str(raw_path).strip()
            if not value:
                return False
            if path_looks_windows(value):
                return True
            parent = os.path.dirname(value)
            if parent and not os.path.exists(parent):
                return True
            return False

        for flag, default_path in (
                ("--log", NODE_ADHOC_HISTORY_PATH),
                ("--ledger", NODE_DEAL_LEDGER_PATH)):
            try:
                idx = cmd.index(flag)
            except ValueError:
                continue
            if idx + 1 >= len(cmd):
                continue
            current = cmd[idx + 1]
            if flag == "--ledger" and str(current).strip() == "":
                continue
            if should_fallback_path(current):
                cmd[idx + 1] = default_path

        return cmd

    def _load_jobs_from_backend(self):
        if self._shared_queue_enabled():
            path = self._shared_queue_path()
            _shared_queue_init(path)
            self.jobs = _shared_queue_list(path)
            return
        self.jobs = load_job_queue()
        save_job_queue(self.jobs)

    def _enqueue_job(self, job):
        if self._shared_queue_enabled():
            _shared_queue_enqueue(self._shared_queue_path(), job)
            return
        self.jobs.append(job)
        save_job_queue(self.jobs)

    def _on_shared_queue_toggle(self):
        if self.process is not None:
            messagebox.showwarning(
                "Queue Backend",
                "Stop the current run before switching queue backend.")
            self.shared_queue_enabled_var.set(not self.shared_queue_enabled_var.get())
            return
        try:
            self._load_jobs_from_backend()
            self.refresh_job_queue()
            self.save_runtime_preferences()
        except Exception as error:
            messagebox.showerror("Queue Backend", str(error))
            self.shared_queue_enabled_var.set(not self.shared_queue_enabled_var.get())

    def show_sample_planner(self):
        dialog = tk.Toplevel(self)
        dialog.title("Paired Benchmark Sample Planner")
        dialog.geometry("430x250")
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill=tk.BOTH, expand=True)
        std_var = tk.StringVar(value="0.50")
        effect_var = tk.StringVar(value="0.10")
        result_var = tk.StringVar(value="")
        ttk.Label(frame, text="Prior paired standard deviation").grid(
            row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=std_var, width=12).grid(row=0, column=1, pady=6)
        ttk.Label(frame, text="Smallest value effect to detect").grid(
            row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=effect_var, width=12).grid(row=1, column=1, pady=6)
        ttk.Label(frame, textvariable=result_var, font=("TkDefaultFont", 11, "bold")).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(18, 0))
        def calculate():
            try:
                deals = planned_paired_deals(float(std_var.get()), float(effect_var.get()))
            except ValueError:
                result_var.set("Enter positive decimal values.")
                return
            self.hands_var.set(str(deals * 2))
            result_var.set(f"Plan: {deals:,} mirrored deals ({deals * 2:,} games)")
        ttk.Button(frame, text="Calculate and Apply", command=calculate).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def show_lab_help(self):
        content = dict(HELP_TOPICS).get(
            "Headless Tournament Lab", "No help is available for this tool.")
        dialog = tk.Toplevel(self)
        dialog.title("Tournament Lab Help")
        dialog.geometry("680x520")
        text = tk.Text(dialog, wrap=tk.WORD, padx=14, pady=14)
        text.insert("1.0", "Headless Tournament Lab\n\n" + content)
        text.config(state=tk.DISABLED)
        scrollbar = ttk.Scrollbar(dialog, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def save_runtime_preferences(self):
        save_lab_settings({
            "ledger_path": self.ledger_var.get().strip(),
            "early_stop_min_deals": self.early_stop_var.get().strip(),
            "max_runtime_minutes": self.max_runtime_var.get().strip(),
            "stall_minutes": self.stall_minutes_var.get().strip(),
            "randomize_teams": self.randomize_teams_var.get(),
            "round_robin_hands": self.round_robin_hands_var.get().strip(),
            "round_robin_label_prefix": self.round_robin_label_prefix_var.get().strip(),
            "shared_queue_enabled": self.shared_queue_enabled_var.get(),
            "shared_queue_path": self.shared_queue_path_var.get().strip(),
            "shared_queue_lease_minutes": self.shared_queue_lease_minutes_var.get().strip(),
        })

    def _slug(self, text):
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

    def on_close(self):
        self.save_runtime_preferences()
        self.destroy()

    def start_evaluation(self):
        if self.process is not None:
            return

        try:
            hands = self.read_positive_int(self.hands_var.get(), "Hands")
            mcts_a = self.read_positive_int(self.mcts_a_var.get(), "Team 1 play iterations")
            mcts_b = self.read_positive_int(self.mcts_b_var.get(), "Team 2 play iterations")
            bid_a = self.read_positive_int(self.bid_a_var.get(), "Team 1 bid budget")
            bid_b = self.read_positive_int(self.bid_b_var.get(), "Team 2 bid budget")
            worker_multiplier = self.read_positive_int(self.worker_multiplier_var.get(), "Worker multiplier")
            seed = int(self.seed_var.get())
            early_stop = self.read_nonnegative_int(
                self.early_stop_var.get(), "Early-stop deals")
        except ValueError as exc:
            messagebox.showerror("Invalid Input", str(exc))
            return

        try:
            model_a, model_b = self.selected_competitors()
        except ValueError as exc:
            messagebox.showerror("Invalid Teams", str(exc))
            return

        log_path = self.log_var.get().strip()
        label = self.label_var.get().strip() or "mac_adhoc"
        command = self.build_command(
            model_a, model_b, hands, mcts_a, mcts_b, bid_a, bid_b,
            worker_multiplier, label, log_path, seed,
            self.ledger_var.get().strip(), early_stop)
        self.save_runtime_preferences()

        self.append_output(f"\n[GUI] Team 1: {model_a} | play={mcts_a} | bid={bid_a}\n")
        self.append_output(f"[GUI] Team 2: {model_b} | play={mcts_b} | bid={bid_b}\n")

        self.append_output("$ " + " ".join(command) + "\n")
        self.launch_command(command)

    def current_command_snapshot(self):
        hands = self.read_positive_int(self.hands_var.get(), "Hands")
        mcts_a = self.read_positive_int(
            self.mcts_a_var.get(), "Team 1 play iterations")
        mcts_b = self.read_positive_int(
            self.mcts_b_var.get(), "Team 2 play iterations")
        bid_a = self.read_positive_int(self.bid_a_var.get(), "Team 1 bid budget")
        bid_b = self.read_positive_int(self.bid_b_var.get(), "Team 2 bid budget")
        worker_multiplier = self.read_positive_int(
            self.worker_multiplier_var.get(), "Worker multiplier")
        seed = int(self.seed_var.get())
        early_stop = self.read_nonnegative_int(
            self.early_stop_var.get(), "Early-stop deals")
        model_a, model_b = self.selected_competitors()
        label = self.label_var.get().strip() or "adhoc"
        command = self.build_command(
            model_a, model_b, hands, mcts_a, mcts_b, bid_a, bid_b,
            worker_multiplier, label, self.log_var.get().strip(), seed,
            self.ledger_var.get().strip(), early_stop)
        return command, label, f"{model_a} vs {model_b}"

    def selected_competitors(self):
        model_a, model_b = select_tournament_competitors(
            self.competitor_options, self.model_a_var.get().strip(),
            self.model_b_var.get().strip(), self.randomize_teams_var.get())
        self.model_a_var.set(model_a)
        self.model_b_var.set(model_b)
        return model_a, model_b

    def queue_current(self):
        try:
            command, label, match = self.current_command_snapshot()
        except ValueError as error:
            messagebox.showerror("Invalid Job", str(error))
            return
        job = {
            "id": uuid.uuid4().hex, "status": "queued", "label": label,
            "match": match, "created_at": time.time(), "command": command}
        self.save_runtime_preferences()
        if self._shared_queue_enabled():
            _shared_queue_enqueue(self._shared_queue_path(), job)
            self._load_jobs_from_backend()
        else:
            self.jobs.append(job)
            save_job_queue(self.jobs)
        self.refresh_job_queue()

    def queue_full_round_robin(self):
        try:
            hands = self.read_positive_int(
                self.round_robin_hands_var.get(),
                "Round robin games per matchup")
            mcts_a = self.read_positive_int(
                self.mcts_a_var.get(), "Team 1 play iterations")
            mcts_b = self.read_positive_int(
                self.mcts_b_var.get(), "Team 2 play iterations")
            bid_a = self.read_positive_int(self.bid_a_var.get(), "Team 1 bid budget")
            bid_b = self.read_positive_int(self.bid_b_var.get(), "Team 2 bid budget")
            worker_multiplier = self.read_positive_int(
                self.worker_multiplier_var.get(), "Worker multiplier")
            seed_base = int(self.seed_var.get())
            early_stop = self.read_nonnegative_int(
                self.early_stop_var.get(), "Early-stop deals")
        except ValueError as error:
            messagebox.showerror("Invalid Round Robin Settings", str(error))
            return

        profiles = list(self.competitor_options)
        if len(profiles) < 2:
            messagebox.showerror(
                "Round Robin", "At least two tournament profiles are required.")
            return

        label_prefix = self.round_robin_label_prefix_var.get().strip() or "round_robin"
        log_path = self.log_var.get().strip()
        ledger_path = self.ledger_var.get().strip()
        created = 0
        new_jobs = []

        for pair_index, (model_a, model_b) in enumerate(
                itertools.combinations(profiles, 2), start=1):
            pair_seed = seed_base + pair_index * 1000
            pair_label = (
                f"{label_prefix}_{self._slug(model_a)}_vs_{self._slug(model_b)}")
            command = self.build_command(
                model_a, model_b, hands, mcts_a, mcts_b,
                bid_a, bid_b, worker_multiplier, pair_label, log_path,
                pair_seed, ledger_path, early_stop)
            new_jobs.append({
                "id": uuid.uuid4().hex,
                "status": "queued",
                "label": pair_label,
                "match": f"{model_a} vs {model_b}",
                "created_at": time.time(),
                "command": command,
            })
            created += 1

        self.save_runtime_preferences()
        if self._shared_queue_enabled():
            path = self._shared_queue_path()
            for job in new_jobs:
                _shared_queue_enqueue(path, job)
            self.jobs = _shared_queue_list(path)
        else:
            self.jobs.extend(new_jobs)
            save_job_queue(self.jobs)
        self.refresh_job_queue()
        total_games = created * hands
        self.append_output(
            f"\n[QUEUE] Round robin queued: {created} matchups, "
            f"{hands} games each ({total_games} total scheduled games).\n")
        messagebox.showinfo(
            "Round Robin Queued",
            f"Queued {created} matchups with {hands} games each "
            f"({total_games} total games).")

    def refresh_job_queue(self):
        if not hasattr(self, "job_tree"):
            return
        if self._shared_queue_enabled():
            self.jobs = _shared_queue_list(self._shared_queue_path())
        self.job_tree.delete(*self.job_tree.get_children())
        for job in self.jobs:
            created = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(job.get("created_at", 0)))
            status = job.get("status", "queued")
            if status == "running" and job.get("lease_owner"):
                status = f"running ({job.get('lease_owner')})"
            self.job_tree.insert("", tk.END, iid=job["id"], values=(
                status, job.get("label", ""),
                job.get("match", ""), created))

    def run_next_job(self):
        if self.process is not None:
            return
        if self._shared_queue_enabled():
            try:
                job = _shared_queue_claim_next(
                    self._shared_queue_path(),
                    self.queue_owner_id,
                    self._shared_queue_lease_seconds(),
                )
            except Exception as error:
                messagebox.showerror("Shared Queue", str(error))
                return
        else:
            job = next((
                item for item in self.jobs
                if item.get("status") in {"queued", "interrupted"}), None)
        if job is None:
            messagebox.showinfo("Job Queue", "No queued or interrupted jobs remain.")
            return
        if not self._shared_queue_enabled():
            job["status"] = "running"
            job["started_at"] = time.time()
        self.current_job_id = job["id"]
        if not self._shared_queue_enabled():
            save_job_queue(self.jobs)
        self.refresh_job_queue()
        self.append_output(
            f"\n[QUEUE] Starting {job['label']}: {job['match']}\n")
        launch_cmd = job["command"]
        if self._shared_queue_enabled():
            launch_cmd = self._normalize_shared_command(launch_cmd)
        self.launch_command(launch_cmd)

    def remove_selected_job(self):
        selected = self.job_tree.selection()
        if not selected:
            return
        selected_ids = set(selected)
        if self.current_job_id in selected_ids:
            messagebox.showwarning("Job Queue", "Stop the running job before removing it.")
            return
        if self._shared_queue_enabled():
            _shared_queue_remove(self._shared_queue_path(), list(selected_ids))
            self.jobs = _shared_queue_list(self._shared_queue_path())
        else:
            self.jobs = [job for job in self.jobs if job["id"] not in selected_ids]
            save_job_queue(self.jobs)
        self.refresh_job_queue()

    def purge_queue(self):
        if self._shared_queue_enabled():
            warning = (
                "Purge all non-running shared queue jobs?\n\n"
                "Running jobs are preserved and can be reclaimed later if their lease expires.")
        else:
            warning = (
                "Purge all queued/interrupted/failed/completed local jobs?\n\n"
                "Any currently running job is preserved.")

        if not messagebox.askyesno("Purge Queue", warning):
            return

        if self._shared_queue_enabled():
            path = self._shared_queue_path()
            result = _shared_queue_purge(path)

            if result["running"] > 0 and result["stale_running"] > 0:
                clear_stale = messagebox.askyesno(
                    "Purge Queue",
                    f"{result['running']} running entries remain. "
                    f"{result['stale_running']} appear stale (lease expired).\n\n"
                    "Clear stale running entries now?")
                if clear_stale:
                    result = _shared_queue_purge(
                        path, include_running=False, stale_running_only=True)

            if result["running"] > 0:
                force_clear = messagebox.askyesno(
                    "Purge Queue",
                    "Some running entries still remain.\n\n"
                    "Force clear ALL remaining running entries? "
                    "Only do this if you are sure no nodes are actively processing.")
                if force_clear:
                    result = _shared_queue_purge(path, include_running=True)

            self.jobs = _shared_queue_list(self._shared_queue_path())
            self.refresh_job_queue()
            messagebox.showinfo(
                "Purge Queue",
                f"Removed {result['removed']} jobs. "
                f"Running jobs kept: {result['running']}.")
            return

        kept = []
        removed = 0
        for job in self.jobs:
            if job.get("status") == "running":
                kept.append(job)
            else:
                removed += 1
        self.jobs = kept
        save_job_queue(self.jobs)
        self.refresh_job_queue()
        messagebox.showinfo(
            "Purge Queue",
            f"Removed {removed} jobs. Running jobs kept: {len(kept)}.")

    def launch_command(self, command):
        self.set_running(True)
        self.worker_thread = threading.Thread(
            target=self.run_command, args=(command,), daemon=True)
        self.worker_thread.start()

    def build_command(self, model_a, model_b, hands, mcts_a, mcts_b,
                      bid_a, bid_b, worker_multiplier, label, log_path,
                      seed=20260801,
                      ledger_path="", early_stop_min_deals=0):
        command = [
            sys.executable, "-u", NEURAL_SCRIPT, model_a, model_b,
            "--hands", str(hands),
            "--mcts-a", str(mcts_a), "--mcts-b", str(mcts_b),
            "--bid-rollouts-a", str(bid_a), "--bid-rollouts-b", str(bid_b),
            "--worker-multiplier", str(worker_multiplier),
            "--label", label, "--log", log_path, "--seed", str(seed),
        ]
        if ledger_path:
            command.extend(["--ledger", ledger_path])
        if early_stop_min_deals:
            command.extend([
                "--early-stop-min-deals", str(early_stop_min_deals)])
        return command

    def read_positive_int(self, raw_value, field_name):
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a whole number.") from exc
        if value < 1:
            raise ValueError(f"{field_name} must be at least 1.")
        return value

    def read_nonnegative_int(self, raw_value, field_name):
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a whole number.") from exc
        if value < 0:
            raise ValueError(f"{field_name} cannot be negative.")
        return value

    def run_command(self, command):
        try:
            self.process = subprocess.Popen(
                command,
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=(os.name != "nt"),
            )
            self.process_started_at = self.last_output_at = time.time()
            self.watchdog_reason = None
            for line in self.process.stdout:
                self.last_output_at = time.time()
                lowered = line.lower()
                if "cuda" in lowered and any(token in lowered for token in (
                        "out of memory", "error", "failed")):
                    self.watchdog_reason = "CUDA failure detected in evaluator output"
                self.output_queue.put(line)
            return_code = self.process.wait()
            self.output_queue.put(f"\n[GUI] Evaluation exited with code {return_code}.\n")
            self.output_queue.put(("finished", return_code))
        except Exception as exc:
            self.output_queue.put(f"\n[GUI] Failed to start evaluation: {exc}\n")
            self.output_queue.put(("finished", -1))
        finally:
            self.output_queue.put(None)

    def stop_evaluation(self):
        if self.process is not None and self.process.poll() is None:
            if os.name != "nt":
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            else:
                self.process.terminate()
            self.append_output("\n[GUI] Stop requested.\n")

    def drain_output_queue(self):
        while True:
            try:
                item = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self.process = None
                self.set_running(False)
            elif isinstance(item, tuple) and item[0] == "finished":
                was_queued = self.current_job_id is not None
                if self._shared_queue_enabled() and self.current_job_id is not None:
                    _shared_queue_finish(
                        self._shared_queue_path(),
                        self.current_job_id,
                        self.queue_owner_id,
                        item[1],
                        self.watchdog_reason,
                    )
                else:
                    for job in self.jobs:
                        if job["id"] == self.current_job_id:
                            job["status"] = "completed" if item[1] == 0 else "failed"
                            job["finished_at"] = time.time()
                            job["return_code"] = item[1]
                            if self.watchdog_reason:
                                job["failure_reason"] = self.watchdog_reason
                            break
                self.current_job_id = None
                if not self._shared_queue_enabled():
                    save_job_queue(self.jobs)
                self.refresh_job_queue()
                if was_queued:
                    self.after(200, self.run_next_job)
            else:
                self.append_output(item)
        self.after(100, self.drain_output_queue)

    def watchdog_tick(self):
        if self.process is not None and self.process.poll() is None:
            now = time.time()
            try:
                runtime_limit = float(self.max_runtime_var.get()) * 60
                stall_limit = float(self.stall_minutes_var.get()) * 60
            except ValueError:
                runtime_limit = stall_limit = 0
            if (self._shared_queue_enabled() and self.current_job_id
                    and now - self.last_lease_heartbeat_at >= 10):
                try:
                    _shared_queue_heartbeat(
                        self._shared_queue_path(), self.current_job_id,
                        self.queue_owner_id, self._shared_queue_lease_seconds())
                    self.last_lease_heartbeat_at = now
                except Exception as error:
                    self.append_output(
                        f"\n[WATCHDOG] Shared queue heartbeat error: {error}\n")
            reason = None
            if runtime_limit and self.process_started_at and now - self.process_started_at > runtime_limit:
                reason = "maximum runtime exceeded"
            elif stall_limit and self.last_output_at and now - self.last_output_at > stall_limit:
                reason = "no evaluator output within watchdog threshold"
            if reason:
                self.watchdog_reason = reason
                self.append_output(f"\n[WATCHDOG] Stopping evaluation: {reason}.\n")
                self.process.terminate()
        self.after(1000, self.watchdog_tick)

    def append_output(self, text):
        self.output_text.insert(tk.END, text)
        self.output_text.see(tk.END)

    def clear_output(self):
        self.output_text.delete("1.0", tk.END)

    def set_running(self, running):
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.run_queue_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.status_var.set("Running" if running else "Ready")


if __name__ == "__main__":
    EvalGui().mainloop()