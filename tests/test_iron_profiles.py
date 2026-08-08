import unittest

from BotEuchreGUI import (
    choose_iron_profile_move,
    normalize_profile_name,
    profile_checkpoint_paths,
)
from headless_evaluation import headless_bid_margins


class TestIronProfiles(unittest.TestCase):
    def test_new_profiles_are_normalized(self):
        for profile_name in ("Iron Anchor", "Iron Sleuth", "Iron Closer"):
            self.assertEqual(normalize_profile_name(profile_name), profile_name)

    def test_new_profiles_have_checkpoint_paths(self):
        for profile_name in ("Iron Anchor", "Iron Sleuth", "Iron Closer"):
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
            choose_iron_profile_move("Iron Anchor", ranked, 4.5),
            ranked[1],
        )
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


if __name__ == "__main__":
    unittest.main()
