# Bot Euchre

Project status: actively maintained public gameplay release.

Bot Euchre is a desktop Euchre game, trainer, replay tool, and AI laboratory built
with Python, Tkinter, PyTorch, and Monte Carlo tree search. It combines three trained
neural personalities with ensembles, score-aware routers, scenario drills, coaching
tools, and profile-versus-profile tournaments.

This repository is the public gameplay and evaluation release: launch the GUI, play
full matches, inspect decisions, run tournaments, and benchmark the shipped profiles.

## First-Time Setup For Windows

If you have never used Python, Command Prompt, or GitHub before, follow these exact
steps.

1. Go to this repository on GitHub.
2. Click the green **Code** button.
3. If you do not use Git, choose **Download ZIP**.
4. Extract the ZIP to a folder such as `C:\Users\YourName\BotEuchre`.
5. Install Python 3 from [python.org](https://www.python.org/downloads/windows/).
6. During installation, check **Add Python to PATH**.
7. Open the extracted `BotEuchre` folder in File Explorer.
8. Click the address bar, type `cmd`, and press `Enter`.
9. In the Command Prompt window that opens, run:

```bat
py -3 -m pip install -r requirements.txt
```

10. After that finishes, run the self-test:

```bat
py -3 pre_release_self_test.py
```

11. If the self-test ends with `"ok": true`, start the game:

```bat
py -3 BotEuchreGUI.py
```

12. The game window should open.

If `py` is not recognized, close Command Prompt, reopen it, and try again. If it
still does not work, restart Windows once after installing Python.

If you already know Git and want the clone version instead of ZIP download, use:

```bat
git clone https://github.com/<your-user>/<your-repo>.git
cd BotEuchre
py -3 -m pip install -r requirements.txt
py -3 pre_release_self_test.py
py -3 BotEuchreGUI.py
```

The canonical application is:

```text
BotEuchreGUI.py
```

Despite the historical filename, the application is branded **Bot Euchre** in the
interface.

## 30-Second Run

1. Open PowerShell in this folder.
2. Install dependencies: `py -3 -m pip install -r .\requirements.txt`
3. Launch: `py -3 .\BotEuchreGUI.py`

If all three checkpoint files are present, the setup window opens and you can start
playing immediately.

## Required Files

| File | Required | Purpose |
|---|---|---|
| `BotEuchreGUI.py` | Yes | Main desktop application entry point. |
| `arbiter_weights.pth` | Yes | Arbiter balanced neural checkpoint. |
| `ironclad_final_gen18.pth` | Yes | Ironclad conservative neural checkpoint. |
| `kyle_weights.pth` | Yes | Kyle aggressive neural checkpoint. |
| `golden_replay_cases.json` | Yes | Golden-rule contract checks used by self-test and diagnostics. |
| `pre_release_self_test.py` | Optional | In-app/CLI release verification checks. |
| `adhoc_headless_evaluation_gui.py` | Optional | Separate Tournament Lab window launched from Tools. |
| `adhoc_headless_evaluation.py` | Optional | CLI mirrored benchmark runner. |

## Troubleshooting Startup

- If launch fails with missing `torch` or `numpy`, run `py -3 -m pip install -r .\requirements.txt` and retry.
- If the app reports missing checkpoints, verify `arbiter_weights.pth`, `ironclad_final_gen18.pth`, and `kyle_weights.pth` are in the repository root.
- If nothing opens after launch, run from a terminal to see errors directly: `py -3 .\BotEuchreGUI.py`.
- If a stale session causes issues, choose a fresh start when prompted or delete `node_state/<your-node>/bot_euchre_autosave.json`.

## Quick Start

From PowerShell in the repository root:

```powershell
py -3 .\BotEuchreGUI.py
```

The neural profiles require Python 3, PyTorch, and NumPy. Tkinter is normally
included with the standard Windows Python installer.

```powershell
py -3 -m pip install -r .\requirements.txt
```

The application expects these checkpoints:

| Profile | Checkpoint |
|---|---|
| Arbiter | `arbiter_weights.pth` |
| Ironclad | `ironclad_final_gen18.pth` |
| Kyle | `kyle_weights.pth` |

CPU inference works. CUDA or Apple MPS is used automatically when available.

## Known Limitations

- Search speed depends heavily on hardware; deeper presets can be slow on CPU-only systems.
- The first run can take longer while PyTorch initializes and checkpoints are loaded.
- This release ships fixed checkpoints and gameplay/evaluation tooling, not the full training pipeline.

## Public Release Scope

This repository is the public gameplay and evaluation release. It includes the
canonical GUI, three production checkpoints, and the companion headless/self-test
scripts launched by the GUI.

It does not include the full self-play training pipeline, generation scripts, or
model-archive workflow from the larger development repository.

## Setting Up a Match

The opening screen lets you configure:

- Your display name
- The AI profile in each of the other three seats
- Trainer Mode
- A normal match or a scenario drill
- Separate search depth for advice/autoplay and table opponents
- General engine speed
- Saved match presets

Preferences persist between launches. Named presets save complete table setups for
quick reuse. Trainer Mode remains optional and is unchecked by default.

## The Three Trained Brains

### Arbiter

The balanced Arbiter profile uses the promoted generation 50 checkpoint. It is the
general-purpose baseline: neither deliberately conservative nor deliberately
aggressive. The other trained personalities were branched from this lineage.

50 Generations of training here constitutes about 5 Million hands of Euchre (about 100k hands per generation)

### Ironclad

Ironclad is the conservative specialist. Its branch used positive call margins in
self-play to demand stronger evidence before making voluntary calls. Generation 18
was selected as the final checkpoint and frozen after the branch evaluation cycle.
Its priorities are disciplined bidding, lower euchre exposure, and protecting a
lead.

In testing both headless tournaments and personal play, my opinion is that Ironclad is the strongest AI bot in the game.

This particular generation of Ironclad is derived from 18 generations of higher confidence training, originating from the Generation 50 Arbiter brain, so about 6.8 million hands of Euchre.

This is the one AI profile in the game that will not only reliably beat the Arbiter base brain, but also a mathematically perfect MCTS heuristic bot (The MC) over the course of thousands of games.

See [README_IRONCLAD.md](README_IRONCLAD.md) for profile notes, checkpoint fingerprint,
and provenance summary.

### Kyle

Kyle is the aggressive specialist. It began from the frozen balanced Gen50 seed and
uses training incentives that favor thinner partnered calls while avoiding an
equally large increase in reckless loners. It is designed to press scoring chances,
find comeback calls, and challenge cautious opponents.

Kyle only underwent a few generations of training. I pulled the plug after a few generations because it started regressing heavily.

See [README_KYLE.md](README_KYLE.md) for profile notes, checkpoint fingerprint,
and provenance summary.

## Active Bot Roster

The public release currently exposes the following active bot roster. Profiles can
control table seats, provide advice through **Ask an AI**, or take over the human
seat through **Autoplay**. Legacy aliases and retired experimental profiles are
intentionally omitted from this public README.

| Profile | Description |
|---|---|
| Arbiter | The balanced Gen50 neural checkpoint with standard AlphaZero search. |
| Ironclad | The frozen conservative checkpoint, favoring disciplined calls and lower euchre risk. |
| Kyle | The aggressive checkpoint, willing to call thinner hands and press scoring chances. |
| The Closer | Uses Ironclad while leading or near victory, Kyle when trailing, and Arbiter in balanced games. |
| Unanimous Council | Reinforces moves all three neural brains independently favor and doubles ensemble search depth. |
| Risk Manager | Uses Ironclad evaluations and takes the safer alternative when the top two search choices are nearly tied. |
| Wildcard | Chooses Arbiter, Ironclad, or Kyle once per hand and keeps that identity for the full hand. |
| The MC | Uses information-set Monte Carlo tree search without a neural checkpoint. |
| Iron Monte | Uses Ironclad for bidding and dealer discard, then switches to deep Ironclad-guided MCTS for trick play. |
| IronChad | Pure Ironclad policy with a deeper AlphaZero trick-play search budget. |
| Iron Sleuth | Uses Ironclad's bidding discipline while preferring the more information-preserving move when the top options are nearly tied. |
| Iron Sleuth Tempest | Iron Sleuth with an ultra aggressive call threshold (call_margin=-0.100). |
| Iron Sleuth Hurricane | Iron Sleuth with a maximum test call threshold (call_margin=-0.130). |
| Iron Sleuth Cyclone | Iron Sleuth with a severe call threshold (call_margin=-0.145). |
| Iron Sleuth Supercell | Iron Sleuth with a frontier aggression threshold (call_margin=-0.160). |
| Iron Sleuth Hypercell | Iron Sleuth with an extreme frontier threshold (call_margin=-0.175). |
| Iron Sleuth Firestorm | Iron Sleuth with a hyper-aggressive threshold (call_margin=-0.190). |
| Iron Sleuth Cataclysm | Iron Sleuth with a max-pressure threshold (call_margin=-0.205). |
| Iron Caller | Iron Sleuth finalist profile mapped to +0.100 bid-margin offset. |
| Iron Baller | Iron Sleuth finalist profile mapped to +0.160 bid-margin offset. |
| Iron Closer | Stays conservative when behind, then becomes more assertive in closeout spots once the score margin is favorable. |
| Iron Clutch | Uses Sleuth-style bidding and tie-break play, then selectively deepens search in the final tricks. |
| Iron Endgame Edge | Combines Iron Clutch's selective deepening with score-aware tie-break and bidding behavior. |
| Monte Prime | Uses Ironclad for bidding and discard, then searches play more deeply with Unanimous Council guidance. |
| Iron Solver | Uses Ironclad for bidding and discard, Iron Monte play early, and solver-style deep search for the final two tricks. |
| Iron Oracle | Keeps Ironclad's close bidding choices unless deep bid search strongly disagrees, then uses Monte Prime play. |

The adjustable `Iron Sleuth +0.020` through `Iron Sleuth +0.300` family provides
additional variants with controlled bid-margin offsets for aggression frontier testing.

## Playing and Coaching

### Ask an AI

Use the **Ask an AI** menu while the human seat has an active bidding, discard, or
play decision. Any profile can give advice. Neural advisers rank candidate actions,
while The MC and the three Iron hybrids use information-set search during play.

Each consultation is recorded in the session journal. A progress indicator appears
while longer searches are running.

### Autoplay

The **Autoplay** menu gives control of your seat to any profile. You can switch the
active profile during a hand or select **Off** to return control to the human. Search
depth for this seat is configured separately from table-player depth.

### Trainer Mode

Trainer Mode evaluates human card play, records meaningful disagreements, explains
recommended alternatives, and presents a post-hand report. It tracks play accuracy,
expected tricks, bidding feedback, recurring mistakes, and unusual outcomes.

When a mistake is recorded, **Rewind Here** restores the corresponding trick state
so you can try a different line. A completed hand can also be replayed in sandbox
mode without polluting career statistics.

### Deck Tracker and Statistics

The table includes a public-information deck tracker, inferred voids, live hand
power, game score, and trick score. **Stats & Coach** stores long-term performance
and coaching counters in `player_stats.json`.

## Scenario Drills

Every drill initializes a real game state rather than merely changing a label.

| Drill | Goal |
|---|---|
| Loner Defense | Stop an opponent's loner sweep while preserving the best stopper. |
| Dealer Pickup & Discard | Compare discard choices after the dealer is ordered up. |
| Euchre or Bust | Take at least three tricks after the opponents call trump. |
| First Lead Laboratory | Compare opening leads immediately after trump is called. |
| Closeout at 9 Points | Navigate calls, passes, donations, and defense at 9-9. |
| Down 9-6 Comeback | Find the calculated aggression or loner chance needed for a comeback. |
| Partner Called Trump | Support a partner's call with disciplined leads and overtrumps. |
| Two-Trick Endgame | Solve the final two tricks with extensive public information. |
| Weak Dealer Hand | Make the least damaging stick-the-dealer call. |
| Bower Management | Practice timing, protecting, and leading the bowers. |
| Call It and Prove It | Commit to trump and test whether the contract survives. |
| Mystery Scenario | Play a randomly selected drill without seeing its identity. |

**Standard Match** plays ordinary Euchre to 10 points.

## Autosave and Recovery

Bot Euchre atomically autosaves the active match, auction state, hands, scores,
profiles, journal, and tournament state. On the next launch, the app offers to
continue the saved session or discard it and open a fresh setup.

On a shared drive, volatile state is isolated under `node_state/<computer-name>`.
Set `BOT_EUCHRE_NODE_ID` before launch to choose a stable name explicitly.
To start a named instance from PowerShell, set the environment variable before
launch:

```powershell
$env:BOT_EUCHRE_NODE_ID = "league-1"
py -3 .\BotEuchreGUI.py
```

Launch additional workers with different node IDs so each instance keeps its own
autosave, queue state, and league claims.

Returning to the Main Menu deliberately clears the current autosave after
confirmation. Closing the application normally preserves it, which also provides
recovery after a crash or interrupted tournament.

Background searches carry a game-generation token. Starting a hand, changing the
Autoplay profile, rewinding, restoring, pausing a tournament, or returning to the
menu invalidates prior results so an old worker cannot play into a newer state.
Native search work may finish in the background, but its stale result is discarded.

## Tools Menu

### Decision Journal and Timeline

The journal records timestamped AI consultations, bids, discards, card plays, hand
results, game results, and tournament events. Entries include a state snapshot when
one is available, making the journal useful for reviewing the sequence of a session.

Every dealt hand has a 64-bit seed. The seed and a deck hash are recorded at hand
start and retained in autosaves, session exports, the status bar, and tournament
history, allowing the exact deal to be reconstructed.

### Export Decision Audit

Exports one JSONL row per bid, discard, play, or AI consultation. Rows include the
chosen action, legal alternatives when available, neural search confidence, hand
seed, deterministic state hash, profiles, and search budgets.

### Export Session

Exports the current session as formatted JSON using the
`bot-euchre-session-v2` format. The file includes metadata, counters, consultations,
events, and serialized game states.

### Replay Viewer

Loads an exported Bot Euchre JSON session and steps forward or backward through its
recorded events. It displays event details, trump, caller, turn, trick, score, and
the hands captured in each available snapshot.
For seeded sessions, **Replay Deal** starts a fresh hand from the selected event's
exact deal seed.
**Analyze Position** reconstructs a recorded card-play state and ranks legal
alternatives with information-set MCTS without changing the live game.

### Confidence Calibration

Loads an exported session, bins recorded neural play confidence against completed
hand outcomes, and reports mean confidence, observed win rate, per-bin gaps, and
expected calibration error.

### Compare AI Recommendations

Runs the same live human-seat decision through selected profiles and displays their
recommendations side by side. Selected profiles are remembered as favorites. The
agreement meter reports how many advisers produced the same recommendation.
An open comparison window automatically refreshes when a new human-controlled
decision becomes available.

Comparison is available during a human-controlled bidding, discard, or card-play
decision. Stop Autoplay before comparing profiles.

### Profile Inspector

Explains each profile's personality and implementation route. It also displays the
active play, bid, and discard search budgets, and current bid margins.

Category badges identify base neural brains, ensembles, routers, learners, MCTS
profiles, and hybrid profiles.

### Tournament Mode

Choose two profiles and a number of games. Team A controls seats 0 and 2; Team B
controls seats 1 and 3. The app runs complete games automatically, tracks wins, and
presents the final series result. Its live dashboard shows game and series scores,
games remaining, hands, euchres, and loner sweeps, with pause/resume and confirmed
cancel controls.

**Balanced League** creates a deterministic round-robin schedule for a selected
frozen roster. Every unordered profile pair receives the same number of jobs. Each
job runs two games from the same starting dealer; the second game replays the first
game's hand seeds with profile sides swapped. All computers claim jobs atomically
from one shared queue and continue automatically until no work remains.

Restart each GUI after finalizing checkpoints, then create the league. The roster
stores exact profile/checkpoint fingerprints and blocks new claims if any checkpoint
changes, preventing evolving weights from being recorded under a frozen identity.
The league also freezes the Elo season that is active when the league is created.

**Manage League** displays every job, its status, and the computer currently running
it. To replace an unfinished league, cancel its running tournament on each listed
computer. If the process was terminated and left an orphaned `Claimed` row, select
that row and use **Release Selected Claims**. Then select **Retire Current League**.
Retirement archives the old
schedule under `backups`, discards only its unplayed jobs, and leaves all completed
games and Elo records in tournament history.

The manager's **Standings** view aggregates completed games by frozen profile
identity. It displays wins, losses, points for, points against, and point
differential, sorting equal win totals by differential. This makes 1-1 mirrored
splits more informative without changing the win-based Elo calculation.

### Human League Season

**Human League Season** keeps the player at seat 0 and pairs them with one selected
AI partner at seat 2. Every selected opponent profile controls seats 1 and 3 for an
equal number of complete games. The schedule is deterministic, rotates the starting
dealer, and persists independently under the current computer's `node_state` folder.

After the regular season, opponents are seeded by wins against the human team and
then point differential. The human team plays a best-of-three gauntlet from the
lowest qualifier through the strongest. Two wins advance; two losses eliminate the
team. Partner/opponent checkpoint fingerprints and table search budgets are frozen
when the season is created. Open **Tools > Human League Season** to
create a season, inspect standings and the playoff path, or resume the next game.

Enable **Fixed-deal benchmark** and provide an integer seed to run a reproducible
deal sequence. Every game stores its constituent hand seeds. Completed games also
update zero-sum Elo ratings with a 1500 starting rating and K-factor of 24.

Every game and completed series is automatically appended to
`bot_euchre_tournament_history.jsonl`. Series records include win rates, point
differential, seat assignment, search depth, timing, euchres, and loner results.
The history remains centralized across computers and uses a cross-machine lock for
appends and schema migrations, preserving one shared Elo ladder.
**Tournament History** displays completed series inside the app.
**Elo Leaderboard** reconstructs current ratings from immutable game history. Ratings
are keyed by profile/checkpoint fingerprint, show W-L sample context and uncertainty,
remain provisional through 19 games, and can be archived by starting a named season.
It also shows win percentage, a Wilson 95% win-rate interval, dynamic strength of
schedule, and selectable head-to-head records.

**Headless Tournament Lab** launches as a separate process and compares the same
neural AI profiles available in the main game rather than archived generations.
The profile dropdown mirrors the current main-game roster, including base brains,
routers, hybrid profiles, and the full aggressive Sleuth ladder through
Iron Sleuth Cataclysm.
Each deal is played twice with teams swapped. Runs accept a reproducible seed and
record profile identities, complete checkpoint SHA-256 provenance, confidence
intervals, and mirrored-deal metadata in `adhoc_evaluation_history.jsonl`.
**Compare Latest Benchmarks** reports metric deltas and flags seed or model mismatches.
Tournament Lab also has an atomic persistent job queue under the current computer's
`node_state` directory. Queued and interrupted jobs survive a restart, retain their
exact model/search/resource arguments, and run in order without being claimed by
another computer. Completed jobs remain visible until removed.
Optional per-deal JSONL ledgers retain deterministic deal seeds, starting cards and
scores, contracts, discards, trick totals, and values for both mirrored orientations.
The sample planner estimates mirrored deals from prior paired variance and a target
effect. Optional early stopping checks the paired 95% confidence interval only after
the configured minimum. Runtime, silent-output, and CUDA-error watchdogs mark failed
queue jobs with an actionable reason.

Every interactive and headless benchmark carries a `bot-euchre-provenance-v1`
manifest containing a run UUID, UTC time, command/configuration, Python/PyTorch and
hardware details, engine SHA-256, and checkpoint size/time/SHA-256 metadata.

**Tournament History** is a unified explorer for table and headless records. It can
filter by type, profile/checkpoint hash, season, seed, significance, and date; columns
are sortable and the visible result set exports to JSON or CSV.

**Search Performance** summarizes the latest 500 table searches by median, P95, and
maximum latency. **Named Seed Library** saves notable deals with notes and can replay
them directly from the table.

Play, bidding, and discard hints include rules-grounded context and a direct
"Why not the runner-up?" comparison where ranked search outcomes are available.
These descriptions report measured visits/outcomes and visible hand structure rather
than presenting generated prose as hidden neural reasoning.

### Model Health

Shows checkpoint availability, compute device, current search count, worker
generation, recent search timings, and CUDA memory use when available.
Checkpoint keys and tensor shapes are validated against the live network before a
model is accepted. Missing and incompatible checkpoints appear explicitly in the
health panel instead of failing later during inference.

### Diagnostic Bundle

Exports a ZIP containing environment details, checkpoint status, search timings,
the current seed and game state, plus the latest 200 session events. Unhandled
background-search errors automatically create the same bundle under
`bot_euchre_diagnostics/`.

### Settings Management

Resets saved presets, profile favorites, or all preferences without manually
editing JSON files.
The hardware recommendation selects Fast, Balanced, or Deep using available CPU
cores and GPU/MPS acceleration, then applies it consistently to advice, Autoplay,
and table searches.

### Open Windows

Lists every managed tool window so it can be focused directly, and provides a
single **Close All Tool Windows** command. Tool windows are non-modal, allowing the
comparison, inspector, health, history, journal, and statistics windows to remain
open together.

### Accessibility

The accessibility dialog provides:

- Larger card buttons and controls
- A high-contrast table
- Reduced delays between AI actions

These choices persist in `bot_euchre_settings.json`.

### Session Summary

Shows elapsed session time, completed hands and games, recorded decisions, and the
most frequently consulted AI profile.

## Status Bar

The persistent status bar shows the current game phase, active seat and profile,
profile category, Trainer/Autoplay/Tournament mode, table search depth, compute
device, and number of active searches.

## Search Depth

Neural search has three presets:

| Preset | Card-play iterations | Bid rollouts | Discard determinizations |
|---|---:|---:|---:|
| Fast | 400 | 250 | 24 |
| Balanced | 1,200 | 800 | 64 |
| Deep | 2,400 | 1,600 | 96 |

Higher settings generally improve deliberation at the cost of response time. Hint
and Autoplay depth is independent from the depth used by the other table seats.
Unanimous Council applies an additional depth multiplier.

## Keyboard Controls

| Key | Action |
|---|---|
| `A` | Ask Arbiter for advice on the current human decision. |
| `J` | Open the Decision Journal and Timeline. |
| `T` | Open AI Comparison. |
| `Esc` | Stop Autoplay and return control to the human seat. |

Cards are selected by clicking them. Number-key card shortcuts are intentionally not
enabled.

## Saved Data

| File | Purpose |
|---|---|
| `node_state/<node>/bot_euchre_settings.json` | Per-computer setup and accessibility settings. |
| `node_state/<node>/bot_euchre_autosave.json` | Per-computer active-session recovery state. |
| `bot_euchre_tournament_history.jsonl` | Automatically saved game and tournament-series results. |
| `bot_euchre_league_state.json` | Shared balanced-league roster, schedule, claims, and completion state. |
| `node_state/<node>/adhoc_evaluation_history.jsonl` | Per-computer headless benchmark results. |
| `node_state/<node>/bot_euchre_seed_library.json` | Per-computer reproducible seeds and notes. |
| `node_state/<node>/bot_euchre_headless_jobs.json` | Per-computer Tournament Lab queue. |
| `node_state/<node>/bot_euchre_human_league.json` | Current personal Human League season, schedule, standings, and playoffs. |
| `node_state/<node>/bot_euchre_human_league_history.jsonl` | Completed Human League game results. |
| `node_state/<node>/adhoc_deal_ledger.jsonl` | Per-computer detailed benchmark deals. |
| `golden_replay_cases.json` | Versioned deal, legality, trick-winner, and scoring contracts. |
| User-selected `.jsonl` files | Decision-audit exports with seeds and state hashes. |
| `bot_euchre_diagnostics/*.zip` | Manual and automatic diagnostic bundles. |
| `node_state/<node>/player_stats.json` | Per-computer career statistics and coaching counters. |
| User-selected `.json` files | Exported session journals and replay data. |

Deleting the settings file restores setup defaults on the next launch. The in-app
Settings Management dialog can reset preferences more selectively. Exported
sessions are only created when you choose **Export Session**.
Settings, autosaves, sessions, seed libraries, queues, and benchmark histories carry
explicit schema versions. Legacy data is migrated on load after a timestamped `.bak`
copy is created.

## Technical Overview

- **Interface:** Tkinter desktop GUI
- **Game:** Four-player partnership Euchre with standard bidding, loners, bowers,
  follow-suit rules, euchres, marches, and stick-the-dealer
- **Canonical rules:** Shared effective-suit, card-power, and trick-winner functions
  used by live and simulated game states
- **Neural model:** 307 input features, seven residual fully connected blocks, a
  33-action policy head, and a scalar value head
- **Policy actions:** 24 card actions plus pass, four partnered trump calls, and four
  loner calls
- **Neural search:** AlphaZero-style MCTS for card play, bid MCTS for auctions, and
  determinized search for dealer discard
- **Non-neural search:** Information-set MCTS using only legally available knowledge
- **Compute:** PyTorch CPU, CUDA, or Apple MPS selected automatically
- **Replay:** Timestamped JSON events with packed UI-state snapshots

## Repository Notes

This public repository contains the canonical GUI, its three checkpoints, and the
companion evaluation/test scripts the GUI launches. The full training pipeline
(self-play generation, checkpoint promotion, data-sweeping, and visualization
scripts) is not included here.

## Validation

Run the complete in-app check from **Tools > Run Pre-Release Self-Test**, or invoke
the same check directly. It validates writable storage, golden replay contracts,
all production checkpoints, seeded deals, rules fuzzing, a hand soak, and a real
two-game mirrored neural benchmark with provenance:

```powershell
py -3 .\pre_release_self_test.py
```

Use `--skip-neural` for the fast checks only.

Compile the application:

```powershell
py -3 -m py_compile .\BotEuchreGUI.py
```

The suite also validates `golden_replay_cases.json`, including deterministic deck
order/hash, legal follow-suit sets, bower-aware trick winners, and scoring outcomes.

Run a deterministic 10,000-hand soak test (or choose another count and seed):

```powershell
py -3 .\soak_test_headless.py --hands 10000 --seed 20260801
```

The soak report checks legal-move availability, duplicate card plays, trick and
move-count invariants, throughput, and retained/peak memory growth. Add `--output`
to save the JSON report.

Fuzz generated rules states and verify that injected corruptions are rejected:

```powershell
py -3 .\rules_invariant_fuzzer.py --cases 5000 --seed 20260802
```

Launch a reproducible mirrored checkpoint benchmark without the GUI:

```powershell
py -3 .\adhoc_headless_evaluation.py Arbiter "Unanimous Council" --hands 100 --seed 20260801
```
