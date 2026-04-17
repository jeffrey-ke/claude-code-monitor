# Battery / permission-`?` swap in the notch

Small fix on top of the Phase 2 usage battery (see `usage-battery-mac-local.md`).

## Problem

When a session raised a permission request, the amber `?`
(`PermissionIndicatorIcon`) and the battery both tried to occupy the left
cluster next to the crab. Result: the battery visually hid the `?`, or the
cluster overflowed the one-sided left-expansion frame and clipped the `?`.

Prior approach had been to pile `permissionIndicatorWidth + usageExtraWidth`
onto the left side of the notch — a hack, since the notch must stay centered
on the physical MacBook notch cutout.

## Decision

The `?` **replaces** the battery while a permission request is pending; when
resolved, the battery returns. Notch size stays constant across both states —
no expansion churn, no symmetric-expansion rework. The `?` is actionable; the
battery is ambient, so there's no loss in hiding the battery briefly.

## Change

Single file: `claude-island/ClaudeIsland/UI/Views/NotchView.swift`.

1. Added `shouldShowUsageBattery` — a single gate combining the existing
   `usage != nil`, `!isUsageStale(usage)`, and the new `!hasPendingPermission`
   check.
2. Rewrote `usageExtraWidth` to return `shouldShowUsageBattery ? 28 : 0`. This
   propagates automatically into `expansionWidth` and the left-cluster
   `.frame(width:)`, so width math and render visibility stay in sync.
3. Gated the `UsageBatteryView` render site in `headerRow` on
   `shouldShowUsageBattery`.

No changes to `expansionWidth` branches, `UsageBatteryView`,
`PermissionIndicatorIcon`, or notch sizing. Animation of the swap is free —
the container already carries `.animation(.smooth, value: hasPendingPermission)`.

## Verification

- Idle + fresh usage → battery shows.
- Permission request arrives → battery fades out, `?` fades in.
- Approve/deny → `?` disappears, battery returns.
- Notch width unchanged across the transition.

Tested end-to-end: Claude triggered a tool permission prompt; battery swapped
out for the `?`, then returned on approve.
