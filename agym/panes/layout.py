from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class SplitDirection(str, Enum):
    """Direction of a pane split.

    LEFT_RIGHT: Splits along the width dimension; the dividing cut is vertical,
                resulting in side-by-side (left and right) panes.
                Mapped in tmux to `split-window -h`.
    TOP_BOTTOM: Splits along the height dimension; the dividing cut is horizontal,
                resulting in stacked (top and bottom) panes.
                Mapped in tmux to `split-window -v`.
    """
    LEFT_RIGHT = "left_right"
    TOP_BOTTOM = "top_bottom"

    # Aliases for backward compatibility
    HORIZONTAL = "left_right"
    VERTICAL = "top_bottom"

    @classmethod
    def _missing_(cls, value: object) -> SplitDirection | None:
        if isinstance(value, str):
            val_lower = value.lower()
            if val_lower in ("horizontal", "h", "lr", "left_right"):
                return cls.LEFT_RIGHT
            if val_lower in ("vertical", "v", "tb", "top_bottom"):
                return cls.TOP_BOTTOM
        return None



@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center_x(self) -> float:
        return self.x + (self.width / 2.0)

    @property
    def center_y(self) -> float:
        return self.y + (self.height / 2.0)

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


@dataclass(frozen=True)
class SplitStep:
    step_index: int
    target_pane_id: int
    new_pane_id: int
    direction: SplitDirection
    target_rect_before: Rect
    target_rect_after: Rect
    new_rect: Rect


@dataclass
class PaneInfo:
    pane_id: int
    rect: Rect
    profile_index: int


@dataclass(frozen=True)
class LayoutPlan:
    total_panes: int
    splits: tuple[SplitStep, ...]
    panes: tuple[PaneInfo, ...]

    def get_pane(self, pane_id: int) -> PaneInfo | None:
        for pane in self.panes:
            if pane.pane_id == pane_id:
                return pane
        return None


def calculate_layout(n: int) -> LayoutPlan:
    """Calculates a recursive, balanced pane layout for N profiles.
    
    Rules:
    1. Starts with the available terminal area as one rectangular pane (0, 0, 1, 1).
    2. Until there are N panes, selects an existing pane to split.
    3. Prefers the pane with the largest current area.
    4. If multiple panes have equal area, chooses them spatially:
       - bottom before top;
       - right before left.
    5. Splits the selected pane into two equal halves.
    6. Prefers splitting along the pane's longer dimension:
       - width >= height -> LEFT_RIGHT (tmux -h, producing side-by-side panes);
       - height > width -> TOP_BOTTOM (tmux -v, producing stacked panes).
    7. Retains deterministic assignment of profile index 0..N-1 to pane IDs 0..N-1.
    """
    if n < 0:
        raise ValueError(f"Profile count must be non-negative, got {n}")
    if n == 0:
        return LayoutPlan(total_panes=0, splits=(), panes=())
    if n == 1:
        initial = PaneInfo(pane_id=0, rect=Rect(0.0, 0.0, 1.0, 1.0), profile_index=0)
        return LayoutPlan(total_panes=1, splits=(), panes=(initial,))

    panes: list[PaneInfo] = [
        PaneInfo(pane_id=0, rect=Rect(0.0, 0.0, 1.0, 1.0), profile_index=0)
    ]
    splits: list[SplitStep] = []
    next_pane_id = 1

    while len(panes) < n:
        # Tie-break key:
        # 1. Largest area
        # 2. Bottom before top (larger center_y first)
        # 3. Right before left (larger center_x first)
        def sort_key(p: PaneInfo) -> tuple[float, float, float]:
            return (
                round(p.rect.area, 6),
                round(p.rect.center_y, 6),
                round(p.rect.center_x, 6),
            )

        target = max(panes, key=sort_key)
        before_rect = target.rect

        # Root window (width >= 1.0) splits along width to establish two columns (LEFT_RIGHT).
        # Otherwise, if width is strictly greater than height (e.g. 0.5 > 0.25), split along width (LEFT_RIGHT).
        # When width <= height (including equal-sided quadrants like 0.5 == 0.5), split along height (TOP_BOTTOM)
        # (producing top and bottom stacked halves) to preserve usable terminal column widths
        # and prevent creating awkwardly narrow vertical panes.
        if before_rect.width >= 1.0 or (
            before_rect.width > before_rect.height
            and round(before_rect.width, 6) != round(before_rect.height, 6)
        ):
            direction = SplitDirection.LEFT_RIGHT
            half_w = before_rect.width / 2.0
            left_rect = Rect(before_rect.x, before_rect.y, half_w, before_rect.height)
            right_rect = Rect(before_rect.x + half_w, before_rect.y, half_w, before_rect.height)
            target.rect = left_rect
            new_pane = PaneInfo(pane_id=next_pane_id, rect=right_rect, profile_index=next_pane_id)
            target_after = left_rect
            new_rect = right_rect
        else:
            direction = SplitDirection.TOP_BOTTOM
            half_h = before_rect.height / 2.0
            top_rect = Rect(before_rect.x, before_rect.y, before_rect.width, half_h)
            bottom_rect = Rect(before_rect.x, before_rect.y + half_h, before_rect.width, half_h)
            target.rect = top_rect
            new_pane = PaneInfo(pane_id=next_pane_id, rect=bottom_rect, profile_index=next_pane_id)
            target_after = top_rect
            new_rect = bottom_rect

        splits.append(
            SplitStep(
                step_index=len(splits),
                target_pane_id=target.pane_id,
                new_pane_id=next_pane_id,
                direction=direction,
                target_rect_before=before_rect,
                target_rect_after=target_after,
                new_rect=new_rect,
            )
        )
        panes.append(new_pane)
        next_pane_id += 1

    return LayoutPlan(total_panes=n, splits=tuple(splits), panes=tuple(panes))
