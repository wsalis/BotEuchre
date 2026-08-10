import unittest
from types import SimpleNamespace
from unittest.mock import patch

from BotEuchreGUI import (
    _continue_auction_by_policy,
    choose_iron_profile_move,
    normalize_profile_name,
    profile_checkpoint_paths,
)
from headless_evaluation import headless_bid_margins


class TestIronProfiles(unittest.TestCase):
    def test_new_profiles_are_normalized(self):
        for profile_name in (
                "Iron Sleuth", "Iron Closer",
                "Iron Clutch", "Iron Endgame Edge"):
            self.assertEqual(normalize_profile_name(profile_name), profile_name)

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


if __name__ == "__main__":
    unittest.main()
