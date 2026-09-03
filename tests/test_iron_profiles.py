import unittest
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from BotEuchreGUI import (
    Card,
    EuchreGame,
    HYBRID_MCTS_PROFILES,
    OMEGACHAD_LONER_MARGIN_PROFILES,
    OMEGACHAD_LONER_MARGIN_VALUES,
    _continue_auction_by_policy,
    _format_ai_comparison_row,
    advance_turn_after_play,
    choose_iron_profile_move,
    ironchad_play_iterations,
    iron_omegachad_bid_rollouts,
    iron_omegachad_discard_determinizations,
    iron_omegachad_play_iterations,
    iron_monte_play_iterations,
    load_elo_standings,
    normalize_profile_name,
    omega_iron_monte_play_iterations,
    profile_checkpoint_paths,
)
from adhoc_headless_evaluation_gui import EvalGui
from adhoc_headless_evaluation import (
    DEFAULT_HEADLESS_BID_ROLLOUTS,
    DEFAULT_HEADLESS_PLAY_ITERATIONS,
    GUI_BID_ROLLOUTS,
    GUI_DISCARD_DETERMINIZATIONS,
    GUI_PLAY_ITERATIONS,
    resolve_compute_settings,
)
from headless_evaluation import headless_bid_margins, headless_profile_brain


class TestIronProfiles(unittest.TestCase):
    def test_elo_standings_aggregate_game_stats_without_legacy_guessing(self):
        records = [
            {
                "type": "game", "season_id": "test", "profile_a": "Ironclad",
                "profile_b": "IronChad", "identity_a": "Ironclad@test",
                "identity_b": "IronChad@test", "winner": "Ironclad",
                "score_a": 10, "score_b": 8, "hands": 10,
                "hand_wins_a": 6, "hand_wins_b": 4,
                "euchres_a": 2, "euchres_b": 1,
                "loners_a": 3, "loners_b": 2,
                "loner_sweeps_a": 1, "loner_sweeps_b": 1,
            },
            {
                "type": "game", "season_id": "test", "profile_a": "Ironclad",
                "profile_b": "IronChad", "identity_a": "Ironclad@test",
                "identity_b": "IronChad@test", "winner": "IronChad",
                "score_a": 7, "score_b": 10,
            },
        ]
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            history_path = handle.name
        try:
            standings = load_elo_standings(history_path, "test")
        finally:
            os.remove(history_path)

        ironclad = standings["Ironclad@test"]
        self.assertEqual(ironclad["games"], 2)
        self.assertEqual(ironclad["stat_games"], 1)
        self.assertEqual(ironclad["points"], 17)
        self.assertEqual(ironclad["hands"], 10)
        self.assertEqual(ironclad["hand_wins"], 6)
        self.assertEqual(ironclad["euchres"], 2)
        self.assertEqual(ironclad["loner_calls"], 3)
        self.assertEqual(ironclad["loner_makes"], 1)
        self.assertEqual(ironclad["caller_euchre_rate"], 0.2)

    def test_headless_gui_compute_preset_matches_main_gui(self):
        args = SimpleNamespace(
            mcts=7, mcts_a=9, mcts_b=11,
            bid_rollouts_a=13, bid_rollouts_b=15,
            discard_determinizations=17,
            profile_default_compute=False,
            gui_base_compute=True,
            equalize_iterations=True,
        )

        resolve_compute_settings(args)

        self.assertEqual((args.mcts_a, args.mcts_b), (1200, 1200))
        self.assertEqual(
            (args.bid_rollouts_a, args.bid_rollouts_b), (800, 800))
        self.assertEqual(args.discard_determinizations, 64)
        self.assertFalse(args.equalize_iterations)
        self.assertEqual(GUI_PLAY_ITERATIONS, 1200)
        self.assertEqual(GUI_BID_ROLLOUTS, 800)
        self.assertEqual(GUI_DISCARD_DETERMINIZATIONS, 64)

        gui = object.__new__(EvalGui)
        command = gui.build_command(
            "Iron Caller", "Iron Baller", 100, 9, 11, 13, 15, 1,
            "gui_compute", "", gui_base_compute=True)
        self.assertIn("--gui-base-compute", command)

    def test_headless_profile_default_compute_uses_shared_base(self):
        gui = object.__new__(EvalGui)
        command = gui.build_command(
            "Ironclad", "IronChad", 100, 9, 11, 13, 15, 1,
            "default_compute", "", profile_default_compute=True)

        self.assertIn("--profile-default-compute", command)
        self.assertEqual(DEFAULT_HEADLESS_PLAY_ITERATIONS, 200)
        self.assertEqual(DEFAULT_HEADLESS_BID_ROLLOUTS, 100)

    def test_headless_gui_adds_ironchad_to_saved_active_profiles(self):
        gui = object.__new__(EvalGui)
        gui.all_profiles = ["Arbiter", "Ironclad", "IronChad"]
        gui.lab_settings = {
            "active_profiles": ["Arbiter", "Ironclad"],
            "monte_prime_v2_seen": True,
            "iron_profiles_v1_seen": True,
            "sleuth_variants_v1_seen": True,
        }

        self.assertIn("IronChad", gui._load_active_profiles())
        self.assertTrue(gui.lab_settings["ironchad_v1_seen"])

    def test_ironchad_uses_ironclad_policy_with_boosted_compute(self):
        self.assertEqual(normalize_profile_name("IronChad"), "IronChad")
        self.assertNotIn("IronChad", HYBRID_MCTS_PROFILES)
        self.assertEqual(
            profile_checkpoint_paths("IronChad"),
            profile_checkpoint_paths("Iron Monte"),
        )
        for base_iterations, completed_tricks, expected in (
                (100, 0, 400), (250, 2, 500),
                (100, 3, 800), (250, 4, 1000)):
            self.assertEqual(
                ironchad_play_iterations(base_iterations, completed_tricks),
                expected,
            )
        for base_iterations, completed_tricks, expected in (
                (1200, 0, 3600), (1200, 2, 7000),
                (1200, 4, 7000), (500, 4, 6000)):
            self.assertEqual(
                omega_iron_monte_play_iterations(base_iterations, completed_tricks),
                expected,
            )

        game = object.__new__(EuchreGame)
        game.ai_profiles = {"0": "IronChad"}
        game.wildcard_hand_profiles = {}
        game.ironclad_brain = object()
        game.kyle_brain = object()
        game.unanimous_council_brain = object()
        game.cheems_brain = object()
        game.game_state = "playing"
        self.assertIs(game._get_neural_brain(0), game.ironclad_brain)
        self.assertEqual(
            headless_profile_brain("IronChad", 0, 0, 0),
            "Ironclad",
        )

    def test_iron_omegachad_uses_ironclad_policy_with_maximum_compute(self):
        self.assertEqual(normalize_profile_name("Iron OmegaChad"), "Iron OmegaChad")
        self.assertNotIn("Iron OmegaChad", HYBRID_MCTS_PROFILES)
        self.assertEqual(
            profile_checkpoint_paths("Iron OmegaChad"),
            profile_checkpoint_paths("Ironclad"),
        )
        self.assertEqual(iron_omegachad_bid_rollouts(100), 400)
        self.assertEqual(iron_omegachad_bid_rollouts(1200), 4000)
        self.assertEqual(iron_omegachad_discard_determinizations(24), 96)
        self.assertEqual(iron_omegachad_discard_determinizations(200), 512)
        for base_iterations, completed_tricks, expected in (
                (100, 0, 800), (250, 2, 2000),
                (100, 4, 3200), (1200, 4, 12000)):
            self.assertEqual(
                iron_omegachad_play_iterations(base_iterations, completed_tricks),
                expected,
            )
        self.assertEqual(
            headless_profile_brain("Iron OmegaChad", 0, 0, 0),
            "Ironclad",
        )

    def test_omegachad_loner_profiles_preserve_omega_compute_and_normal_calls(self):
        self.assertEqual(
            OMEGACHAD_LONER_MARGIN_PROFILES,
            ("Iron OmegaChad Loner +0.05", "Iron OmegaChad Loner +0.11"),
        )
        for profile_name, margin in zip(
                OMEGACHAD_LONER_MARGIN_PROFILES,
                OMEGACHAD_LONER_MARGIN_VALUES):
            self.assertEqual(normalize_profile_name(profile_name), profile_name)
            self.assertEqual(
                profile_checkpoint_paths(profile_name),
                profile_checkpoint_paths("Iron OmegaChad"),
            )
            self.assertEqual(
                headless_profile_brain(profile_name, 0, 0, 0),
                "Ironclad",
            )
            self.assertEqual(
                headless_bid_margins(profile_name, 0, 1, 3, 0, 0),
                (0.0, margin),
            )

    def test_new_profiles_are_normalized(self):
        for profile_name in (
                "Iron Sleuth", "Iron Closer",
                "Iron Clutch", "Iron Endgame Edge"):
            self.assertEqual(normalize_profile_name(profile_name), profile_name)

    def test_finalist_iron_profiles_use_hybrid_compute(self):
        game = object.__new__(EuchreGame)
        game.ai_profiles = {"0": "Iron Caller"}
        game.wildcard_hand_profiles = {}
        game.ironclad_brain = object()
        game.kyle_brain = object()
        game.unanimous_council_brain = object()
        game.cheems_brain = object()
        game.game_state = "playing"
        game.team1_score = 0
        game.team2_score = 0
        self.assertIs(game._get_neural_brain(0), game.ironclad_brain)

        game.ai_profiles = {"0": "Iron Baller"}
        self.assertIs(game._get_neural_brain(0), game.ironclad_brain)

    def test_deactivated_profiles_fallback_to_supported_profiles(self):
        self.assertEqual(normalize_profile_name("Iron Anchor"), "Ironclad")
        self.assertEqual(normalize_profile_name("Copycat"), "Arbiter")
        self.assertEqual(
            normalize_profile_name("Sleuth Score Closer"),
            "Iron Clutch",
        )
        self.assertEqual(
            normalize_profile_name("Sleuth Risk Budget"),
            "Iron Clutch",
        )
        self.assertEqual(
            normalize_profile_name("Sleuth Endgame Turbo"),
            "Iron Clutch",
        )

    def test_retired_sweep_profiles_collapse_onto_survivors(self):
        for retired in (
                "Iron Sleuth Tempest", "Iron Sleuth Hurricane",
                "Iron Sleuth Cyclone", "Iron Sleuth Supercell",
                "Iron Sleuth Hypercell", "Iron Sleuth Firestorm",
                "Iron Sleuth Cataclysm", "Iron Sleuth +0.300"):
            self.assertEqual(normalize_profile_name(retired), "Iron Baller")
        self.assertEqual(normalize_profile_name("Iron Sleuth +0.020"), "Iron Caller")
        self.assertEqual(normalize_profile_name("Iron Sleuth Blitz"), "Iron Caller")
        for retired in ("Iron Solver", "Iron Oracle"):
            self.assertEqual(normalize_profile_name(retired), "Monte Prime")
        for retired in (
                "Iron OmegaChad Loner +0.13", "Iron OmegaChad Loner +0.19"):
            self.assertEqual(
                normalize_profile_name(retired), "Iron OmegaChad Loner +0.11")

    def test_new_profiles_have_checkpoint_paths(self):
        for profile_name in (
                "Iron Sleuth", "Iron Closer",
                "Iron Clutch", "Iron Endgame Edge"):
            self.assertGreater(len(profile_checkpoint_paths(profile_name)), 0)

    def test_iron_closer_bid_margins_follow_score(self):
        self.assertEqual(
            headless_bid_margins("Iron Closer", 0, 1, 1, 8, 5),
            (-0.03, -0.01),
        )
        self.assertEqual(
            headless_bid_margins("Iron Closer", 1, 1, 0, 8, 5),
            (0.05, 0.02),
        )

    def test_iron_profiles_keep_distinct_near_tie_play_rules(self):
        ranked = [("assertive", 52.0), ("safe", 49.0)]
        self.assertEqual(
            choose_iron_profile_move(
                "Iron Sleuth", ranked, 4.5,
                sleuth_key=lambda item: 0 if item[0] == "assertive" else 1,
            ),
            ranked[0],
        )
        self.assertEqual(
            choose_iron_profile_move("Iron Closer", ranked, 4.5, score_gap=-3),
            ranked[1],
        )
        self.assertEqual(
            choose_iron_profile_move("Iron Closer", ranked, 4.5, score_gap=3),
            ranked[0],
        )

    def test_sleuth_variants_apply_score_aware_tie_rules(self):
        ranked = [("assertive", 52.0), ("safe", 49.0)]
        self.assertEqual(
            choose_iron_profile_move(
                "Iron Clutch", ranked, 4.5,
                sleuth_key=lambda item: 0 if item[0] == "assertive" else 1,
            ),
            ranked[0],
        )
        self.assertEqual(
            choose_iron_profile_move(
                "Iron Endgame Edge", ranked, 4.5, score_gap=-3,
                sleuth_key=lambda item: 0 if item[0] == "assertive" else 1,
            ),
            ranked[1],
        )

    def test_sleuth_turbo_closer_bid_margins_shift_with_score(self):
        self.assertEqual(
            headless_bid_margins("Iron Endgame Edge", 0, 1, 1, 8, 5),
            (-0.035, -0.015),
        )
        self.assertEqual(
            headless_bid_margins("Iron Endgame Edge", 1, 1, 0, 8, 5),
            (0.04, 0.015),
        )

    def test_auction_rollout_fallback_handles_empty_stuck_actions(self):
        hands = [[], [], [], []]
        up_card = SimpleNamespace(suit='?')

        with patch("BotEuchreGUI.legal_bid_actions", return_value=[]):
            caller, trump, alone, called_round = _continue_auction_by_policy(
                hands=hands,
                up_card=up_card,
                dealer_idx=0,
                round_num=2,
                passed_seats=[1, 2, 3],
                t1_score=0,
                t2_score=0,
                nn_eval_fn=lambda _: ({}, 0.0),
            )

        self.assertEqual(caller, 0)
        self.assertIsNotNone(trump)
        self.assertFalse(alone)
        self.assertEqual(called_round, 2)

    def test_ai_comparison_row_includes_confidence(self):
        row = _format_ai_comparison_row("Arbiter", "Play AH", 80.0)
        self.assertIn("Arbiter", row)
        self.assertIn("80%", row)
        self.assertIn("[", row)

    def test_mcts_profiles_include_confidence_in_comparison(self):
        game = SimpleNamespace(
            game_state="playing",
            current_turn=0,
            bidding_player=0,
            trump_suit="H",
            up_card=None,
            hands=[[Card("A", "H")]],
            dealer_idx=0,
            ai_profiles={"0": "The MC"},
            ai_model=SimpleNamespace(
                get_best_move=lambda ui_game, player_idx, return_confidence=False, **kwargs: (
                    (0, 87.0) if return_confidence else 0
                )
            ),
        )
        recommendation, confidence = EuchreGame._comparison_recommendation(game, "The MC")
        self.assertEqual(recommendation, "Play AH")
        self.assertAlmostEqual(confidence, 87.0)

    def test_loner_turn_skips_partner_after_play(self):
        self.assertEqual(advance_turn_after_play(1, True, 3), 2)
        self.assertEqual(advance_turn_after_play(2, True, 3), 0)
        self.assertEqual(advance_turn_after_play(0, False, 3), 1)

    def test_restore_recovery_avoids_stuck_dealing_state(self):
        game = object.__new__(EuchreGame)
        game.task_generation = 0
        game.game_state = "dealing"
        game.trump_suit = None
        game.trick = []
        game.hands = [[Card("A", "H"), Card("K", "H"), Card("Q", "H"), Card("J", "H"), Card("9", "H")]
                      for _ in range(4)]
        game.current_turn = 0
        game.is_loner = False
        game.loner_partner_idx = -1
        game.caller_idx = -1
        game.dealer_idx = 0
        game.bidding_player = 0
        game.passed_count = 0
        game.team1_score = 0
        game.team2_score = 0
        game.team1_tricks = 0
        game.team2_tricks = 0
        game.voids = {0: set(), 1: set(), 2: set(), 3: set()}
        game.played_cards = []
        game.up_card = None
        game.dealer_discard = None
        game.autoplay_mode = False
        game.current_hand_seed = 12345
        game.loner_var = SimpleNamespace(set=lambda value: None, get=lambda: False)
        game.autoplay_menu_button = SimpleNamespace(config=lambda *args, **kwargs: None)
        game.lbl_trump = SimpleNamespace(config=lambda *args, **kwargs: None)
        game.update_scoreboard = lambda: None
        game.update_dealer_chip = lambda: None
        game.update_table_graphics = lambda: None
        game.render_human_hand = lambda: None
        game.after = lambda *args, **kwargs: None
        game.process_bidding = lambda: None
        game._resume_current_autoplay_turn = lambda: None
        game._capture_expected_tricks = lambda: None
        game.winfo_exists = lambda: True

        game._restore_game_state({
            "game_state": "dealing",
            "trump_suit": None,
            "trick": [],
            "hands": [[("A", "H"), ("K", "H"), ("Q", "H"), ("J", "H"), ("9", "H")]
                      for _ in range(4)],
            "current_turn": 0,
            "is_loner": False,
            "loner_partner_idx": -1,
            "caller_idx": -1,
            "dealer_idx": 0,
            "bidding_player": 0,
            "passed_count": 0,
            "team1_score": 0,
            "team2_score": 0,
            "team1_tricks": 0,
            "team2_tricks": 0,
            "voids": {0: [], 1: [], 2: [], 3: []},
            "played_cards": [],
            "up_card": None,
            "dealer_discard": None,
            "autoplay_mode": False,
            "hand_seed": 12345,
        })

        self.assertEqual(game.game_state, "bidding_r1")
        self.assertEqual(game.bidding_player, 1)

    def test_start_new_hand_survives_snapshot_failure(self):
        game = object.__new__(EuchreGame)
        game.task_generation = 0
        game.tournament_state = None
        game.human_league_state = None
        game.human_league_game_active = False
        game.sandbox_mode = False
        game.current_hand_seed = None
        game.ai_profiles = {"0": "Human", "1": "Arbiter", "2": "Arbiter", "3": "Arbiter"}
        game.dealer_idx = 0
        game.saved_initial_deck = []
        game.saved_dealer_idx = 0
        game.hands = [[], [], [], []]
        game.sort_hand = lambda hand: None
        game.loner_var = SimpleNamespace(set=lambda value: None, get=lambda: False)
        game.lbl_trump = SimpleNamespace(config=lambda *args, **kwargs: None)
        game.lbl_action = SimpleNamespace(config=lambda *args, **kwargs: None)
        game.update_scoreboard = lambda: None
        game.update_dealer_chip = lambda: None
        game.update_table_graphics = lambda: None
        game.render_human_hand = lambda: None
        game._schedule_autosave = lambda: None
        game._record_session_event = lambda *args, **kwargs: None
        game.after = lambda *args, **kwargs: None
        game._configure_drill_scenario = lambda deck: False
        game._snapshot_for_journal = lambda: None

        game.start_new_hand(seed_override=1)

        self.assertEqual(game.game_state, "bidding_r1")
        self.assertEqual(game.bidding_player, 1)
        self.assertIn(0, game.trick_snapshots)
        self.assertEqual(game.trick_snapshots[0], {})


if __name__ == "__main__":
    unittest.main()
