from __future__ import annotations

import unittest

from agym.panes.layout import (
    LayoutPlan,
    Rect,
    SplitDirection,
    calculate_layout,
)


class LayoutPlannerTests(unittest.TestCase):
    def test_invalid_profile_count(self) -> None:
        with self.assertRaises(ValueError):
            calculate_layout(-1)

    def test_zero_profiles(self) -> None:
        plan = calculate_layout(0)
        self.assertEqual(plan.total_panes, 0)
        self.assertEqual(len(plan.panes), 0)
        self.assertEqual(len(plan.splits), 0)

    def test_one_profile(self) -> None:
        plan = calculate_layout(1)
        self.assertEqual(plan.total_panes, 1)
        self.assertEqual(len(plan.panes), 1)
        self.assertEqual(len(plan.splits), 0)
        self.assertEqual(plan.panes[0].pane_id, 0)
        self.assertEqual(plan.panes[0].profile_index, 0)
        self.assertEqual(plan.panes[0].rect, Rect(0.0, 0.0, 1.0, 1.0))

    def test_split_direction_enum_and_aliases(self) -> None:
        self.assertEqual(SplitDirection.LEFT_RIGHT, "left_right")
        self.assertEqual(SplitDirection.TOP_BOTTOM, "top_bottom")
        self.assertEqual(SplitDirection.HORIZONTAL, SplitDirection.LEFT_RIGHT)
        self.assertEqual(SplitDirection.VERTICAL, SplitDirection.TOP_BOTTOM)
        self.assertEqual(SplitDirection("left_right"), SplitDirection.LEFT_RIGHT)
        self.assertEqual(SplitDirection("top_bottom"), SplitDirection.TOP_BOTTOM)
        self.assertEqual(SplitDirection("horizontal"), SplitDirection.LEFT_RIGHT)
        self.assertEqual(SplitDirection("vertical"), SplitDirection.TOP_BOTTOM)

    def test_two_profiles(self) -> None:
        plan = calculate_layout(2)
        self.assertEqual(plan.total_panes, 2)
        self.assertEqual(len(plan.splits), 1)
        self.assertEqual(len(plan.panes), 2)
        # First split along width (LEFT_RIGHT)
        split = plan.splits[0]
        self.assertEqual(split.target_pane_id, 0)
        self.assertEqual(split.new_pane_id, 1)
        self.assertEqual(split.direction, SplitDirection.LEFT_RIGHT)
        self.assertEqual(split.direction, SplitDirection.HORIZONTAL)
        # Pane 0 left, Pane 1 right
        p0 = plan.get_pane(0)
        p1 = plan.get_pane(1)
        self.assertIsNotNone(p0)
        self.assertIsNotNone(p1)
        self.assertAlmostEqual(p0.rect.width, 0.5)
        self.assertAlmostEqual(p1.rect.width, 0.5)
        self.assertAlmostEqual(p0.rect.height, 1.0)
        self.assertAlmostEqual(p1.rect.height, 1.0)
        self.assertAlmostEqual(p0.rect.x, 0.0)
        self.assertAlmostEqual(p1.rect.x, 0.5)

    def test_three_profiles(self) -> None:
        plan = calculate_layout(3)
        self.assertEqual(plan.total_panes, 3)
        self.assertEqual(len(plan.splits), 2)
        # In 2-pane state, Pane 0 and Pane 1 have equal area (0.5).
        # Both span full height. Spatially right before left selects Pane 1.
        # Pane 1 (w=0.5, h=1.0) longer dimension is height -> TOP_BOTTOM split
        s2 = plan.splits[1]
        self.assertEqual(s2.target_pane_id, 1)
        self.assertEqual(s2.new_pane_id, 2)
        self.assertEqual(s2.direction, SplitDirection.TOP_BOTTOM)
        self.assertEqual(s2.direction, SplitDirection.VERTICAL)

    def test_four_profiles_2x2(self) -> None:
        plan = calculate_layout(4)
        self.assertEqual(plan.total_panes, 4)
        self.assertEqual(len(plan.splits), 3)
        # Splits: 0->1 (LEFT_RIGHT), 1->2 (TOP_BOTTOM), 0->3 (TOP_BOTTOM)
        # All 4 panes should have area 0.25 and form a 2x2 grid
        self.assertEqual(len(plan.panes), 4)
        for p in plan.panes:
            self.assertAlmostEqual(p.rect.area, 0.25)
            self.assertAlmostEqual(p.rect.width, 0.5)
            self.assertAlmostEqual(p.rect.height, 0.5)

        # Verify 4 quadrant assignments:
        # Pane 0: Top-Left (0, 0)
        # Pane 1: Top-Right (0.5, 0)
        # Pane 2: Bottom-Right (0.5, 0.5)
        # Pane 3: Bottom-Left (0, 0.5)
        p0 = plan.get_pane(0)
        p1 = plan.get_pane(1)
        p2 = plan.get_pane(2)
        p3 = plan.get_pane(3)
        self.assertEqual((p0.rect.x, p0.rect.y), (0.0, 0.0))
        self.assertEqual((p1.rect.x, p1.rect.y), (0.5, 0.0))
        self.assertEqual((p2.rect.x, p2.rect.y), (0.5, 0.5))
        self.assertEqual((p3.rect.x, p3.rect.y), (0.0, 0.5))

    def test_quadrant_subdivision_priority_5_to_8(self) -> None:
        """Verify explicitly that starting from four equal quadrants, candidates
        are chosen in exact priority:
          bottom-right (5th)
          -> bottom-left (6th)
          -> top-right (7th)
          -> top-left (8th)
        and each is split vertically (direction=TOP_BOTTOM, horizontal dividing line)
        to maintain usable terminal column widths without narrow columns.
        """
        # N=5: 4th split should target bottom-right (Pane 2) with TOP_BOTTOM direction (stacked)
        plan5 = calculate_layout(5)
        self.assertEqual(len(plan5.splits), 4)
        self.assertEqual(plan5.splits[3].target_pane_id, 2)  # Bottom-Right
        self.assertEqual(plan5.splits[3].direction, SplitDirection.TOP_BOTTOM)

        # N=6: 5th split should target bottom-left (Pane 3) with TOP_BOTTOM direction (stacked)
        plan6 = calculate_layout(6)
        self.assertEqual(len(plan6.splits), 5)
        self.assertEqual(plan6.splits[4].target_pane_id, 3)  # Bottom-Left
        self.assertEqual(plan6.splits[4].direction, SplitDirection.TOP_BOTTOM)

        # N=7: 6th split should target top-right (Pane 1) with TOP_BOTTOM direction (stacked)
        plan7 = calculate_layout(7)
        self.assertEqual(len(plan7.splits), 6)
        self.assertEqual(plan7.splits[5].target_pane_id, 1)  # Top-Right
        self.assertEqual(plan7.splits[5].direction, SplitDirection.TOP_BOTTOM)

        # N=8: 7th split should target top-left (Pane 0) with TOP_BOTTOM direction (stacked)
        plan8 = calculate_layout(8)
        self.assertEqual(len(plan8.splits), 7)
        self.assertEqual(plan8.splits[6].target_pane_id, 0)  # Top-Left
        self.assertEqual(plan8.splits[6].direction, SplitDirection.TOP_BOTTOM)

    def test_greater_than_eight_profiles(self) -> None:
        for n in [9, 10, 12, 16, 25]:
            with self.subTest(n=n):
                plan = calculate_layout(n)
                self.assertEqual(plan.total_panes, n)
                self.assertEqual(len(plan.panes), n)
                self.assertEqual(len(plan.splits), n - 1)

                # Verify deterministic profile index mapping
                for i, p in enumerate(plan.panes):
                    self.assertEqual(p.pane_id, i)
                    self.assertEqual(p.profile_index, i)

                # Total area should always sum to 1.0
                total_area = sum(p.rect.area for p in plan.panes)
                self.assertAlmostEqual(total_area, 1.0, places=5)

    def test_split_direction_rules(self) -> None:
        # N=1 -> width split (LEFT_RIGHT)
        plan2 = calculate_layout(2)
        self.assertEqual(plan2.splits[0].direction, SplitDirection.LEFT_RIGHT)
        self.assertEqual(plan2.splits[0].direction, SplitDirection.HORIZONTAL)

        # N=2..4 -> height splits (TOP_BOTTOM)
        plan4 = calculate_layout(4)
        self.assertEqual(plan4.splits[1].direction, SplitDirection.TOP_BOTTOM)
        self.assertEqual(plan4.splits[2].direction, SplitDirection.TOP_BOTTOM)

        # N=5..8 -> height splits (TOP_BOTTOM) for each quadrant
        plan8 = calculate_layout(8)
        for s in plan8.splits[3:]:
            self.assertEqual(s.direction, SplitDirection.TOP_BOTTOM)

        # N=9 -> width split (LEFT_RIGHT) since width (0.5) > height (0.25)
        plan9 = calculate_layout(9)
        self.assertEqual(plan9.splits[7].direction, SplitDirection.LEFT_RIGHT)



if __name__ == "__main__":
    unittest.main()
