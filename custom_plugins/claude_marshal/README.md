# Claude Auto Marshalling (RotorHazard plugin)

Automatically marshals a race after its laps are saved, following
`rotorhazard_auto_marshalling_plugin_logic.md` (v0.2).

## Flow

1. On **Save Laps** (`Evt.LAPS_SAVE`) a cancellable **countdown** starts (default
   5 s). Anyone can press **Cancel AI marshalling** during it.
2. For each pilot the plugin:
   - validates the stored RSSI trace (length / order / numeric / thresholds);
   - **recomputes laps with the pilot's stored EnterAt/ExitAt** using
     RotorHazard's own crossing algorithm (timestamp = peak midpoint);
   - **only if the calibration looks broken** (no crossings, ExitAt below the
     noise floor so passes merge, or a lap count well below the pilot's other
     rounds) does it ask **Claude** to propose new EnterAt/ExitAt and re-run —
     otherwise the stored thresholds stand (advisory-only by default);
   - enforces **Minimum Lap Time** (cluster resolution keeps the highest-peak
     pass), Minimum First Crossing, and late-lap rules;
   - preserves manual / API laps;
   - checks the result against the pilot's **historical median** (warnings only);
   - runs a **self-check** before anything is saved.
3. **Dry-run** (default on) reports without saving. **Safe mode** (default on)
   refuses to save a race if any pilot raised a blocker.
4. A JSON **report** is logged (and, best-effort, attached to the race).

## Panel

A panel is injected on:

- **Run** — under the pilot/leaderboard table.
- **Marshal** — above the RSSI graph.

It shows the countdown + cancel, per-pilot status (chosen EnterAt/ExitAt, lap
count, `re-tuned`/warning/blocker chips), the dry-run/live + safe-mode badges,
elapsed time, and a run summary. Buttons: **Marshal this race** and, per pilot,
**Marshal** (individual run). State is held server-side and re-sent on every
page load, so progress survives navigating away and back to Run.

## Setup

Install `anthropic` into the RotorHazard environment (declared as a manifest
dependency; the community installer does this automatically). Then open
**Settings → Claude Auto Marshalling**:

| Setting | Default | Notes |
| --- | --- | --- |
| Auto-marshal after each race is saved | **off** | Turn on for the automatic countdown run. |
| Dry-run (preview only) | **on** | Untick to actually save. |
| Safe mode | on | Don't save when a blocker is raised. |
| Countdown seconds | 5 | Cancellable pre-run countdown. |
| Claude API key | — | Only used to re-tune broken calibrations. |
| Model / effort | Opus 4.8 / medium | |
| Strict Minimum Lap Time | on | No active lap below the minimum. |
| History minimum laps | 8 | Below this, history is warning-only. |
| Allow deleting manual / API laps | off | Protected by default. |

> First runs: leave **Dry-run on** to preview. When happy, enable **Auto-marshal**
> and untick **Dry-run** to let it save.

## Notes

- Live node thresholds are never changed; re-tuned thresholds are written only
  to the saved race (like a manual Marshal "Save Laps").
- Save path mirrors `resave_laps` (alter pilot race + replace laps + clear
  caches); the lap math is deterministic and matches the Marshal page.
- Manual per-pilot runs process only that pilot and rebuild the race caches.
