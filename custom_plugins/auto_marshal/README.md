# Auto Marshalling (RotorHazard plugin)

Two layers of automatic marshalling — **fully local and deterministic, no API
keys, no internet**:

1. **Real-time guard** (in-race): fixes missed passes such as the holeshot
   *while the race is running*, so lap counts are correct immediately.
2. **Post-race check**: recomputes each pilot's laps from the stored RSSI
   trace after every save and repairs broken calibrations against the pilot's
   other rounds — preview first, nothing written until **Apply**.

## Real-time guard (in-race, local)

The most common marshalling problem is a **missed first pass (holeshot)**: the
craft flies the first gate but EnterAt is above the actual peak, no crossing is
registered, and every later lap is counted one off. Post-race marshalling fixes
the record, but during a "first to X laps" race you need the counts to be right
*now*.

While a race is underway (armed on `RACE_START`, released on `RACE_STOP`) the
plugin polls each seat's live RSSI trace (`node.history_values` — the same
trace RotorHazard stores for marshalling) about 3× per second, entirely on the
timer's own CPU:

- A **completed RSSI peak** that rises clearly above the race's noise floor,
  matches no recorded lap, and stayed **below EnterAt** (the actual failure
  mode) is treated as a missed gate pass:
  - EnterAt is lowered below the observed peak (and ExitAt fixed if it is
    at/below the noise floor or above EnterAt) via RotorHazard's normal
    calibration path — the node itself catches the following passes;
  - the pass is **injected into the live race at the peak's true timestamp**
    (`race.add_lap`, source *API*). If it is the seat's first pass it becomes
    **lap 0 / the holeshot**, so all later lap numbers line up. If a newer lap
    was already recorded, the seat's lap list is rebuilt (`race.replace_laps`)
    so the pass lands in the correct position and laps renumber.
- A **stuck crossing** (ExitAt at/below the noise floor, so the crossing never
  ends and passes merge) is detected after ~8 s: ExitAt is raised above the
  floor and the crossing is force-ended so the node reports the pass itself.
- Guards: Minimum Lap Time and Minimum First Crossing are honored, peaks the
  node is already handling are skipped, corrections are capped per pilot per
  race, and everything is announced (priority message + a corrections feed in
  the panel) and logged.
- **Learning (local, on by default):** at race start each seated pilot's saved
  races provide priors — the typical crossing rise above the noise floor (from
  stored lap peaks / RSSI traces) sets a per-pilot detection threshold, and the
  pilot's median lap time rejects implausibly fast candidate passes. Every
  in-race decision (added / skipped and why) is stored on the saved race
  (`auto_marshal_rt_log` attribute); when the operator later marshals that
  pilot manually, the plugin compares the final laps with its decisions — an
  auto-added pass the operator removed makes that pilot's detection stricter, a
  lap added where the plugin skipped a peak makes it more sensitive (per-pilot
  factor, persisted, clamped 0.6–1.8). Manual marshalling literally trains the
  real-time guard.

## Post-race flow

1. On **Save Laps** (`Evt.LAPS_SAVE`) a cancellable **countdown** starts
   (default 5 s). Anyone can press **Cancel** during it.
2. For each pilot the plugin:
   - validates the stored RSSI trace (length / order / numeric);
   - **recomputes laps with the pilot's stored EnterAt/ExitAt** using
     RotorHazard's own crossing algorithm (timestamp = peak midpoint);
   - **only if the calibration looks broken** (no crossings, ExitAt below the
     noise floor so passes merge, invalid stored thresholds, a pile of
     sub-Minimum-Lap noise crossings, or a lap count well below the pilot's
     other rounds) does it **re-tune locally**: threshold candidates from the
     seat's other rounds plus a probe grid over the trace span are scored
     against the pilot's history (no noise crossings, no merged passes,
     plausible count) and the most plausible wins — otherwise the stored
     thresholds stand;
   - enforces **Minimum Lap Time** (cluster resolution keeps the highest-peak
     pass), Minimum First Crossing, and late-lap rules;
   - preserves manual / API laps;
   - checks the result against the pilot's **historical median** (warnings only);
   - runs a **self-check** before anything can be applied.
3. Results are a **preview**: nothing is written until **✓ Apply**. **Safe
   mode** (default on) flags pilots with blockers for manual review instead of
   touching them.
4. A JSON **report** is logged (and, best-effort, attached to the race).

## Panel

A panel is injected on:

- **Run** — in a shared plugin bar above the pilot/leaderboard table (side by
  side with our other plugin panels; collapsed to a slim bar until action is
  needed).
- **Marshal** — above the RSSI graph.

It shows the countdown + cancel, per-pilot status (chosen EnterAt/ExitAt, lap
count, `re-tuned`/warning/blocker chips), elapsed time, and a run summary.
Buttons: **Apply**, **Marshal this race** and, per pilot, ↻ (individual run).
The header carries the **AUTO: ON/OFF** master toggle. State is held
server-side and re-sent on every page load, so progress survives navigating
away and back.

## Settings

Open **Settings → Auto Marshalling**:

| Setting | Default | Notes |
| --- | --- | --- |
| Auto-marshal after each race is saved | on | Master switch (also the AUTO toggle in the panel header). |
| Dry-run (preview only) | off | The flow is preview→Apply anyway; dry-run just marks the report. |
| Safe mode | on | Pilots with blockers are left untouched. |
| Countdown seconds | 5 | Cancellable pre-run countdown. |
| Strict Minimum Lap Time | on | No active lap below the minimum. |
| History minimum laps | 8 | Below this, history is warning-only. |
| Allow deleting manual / API laps | off | Protected by default. |
| Real-time marshalling during races | on | The in-race guard. |
| Real-time: which missed passes | any | Or holeshot (first pass) only. |
| Real-time: sensitivity | normal | Peak rise vs noise floor to qualify. |
| Real-time: re-tune EnterAt/ExitAt live | on | Node catches the next passes itself. |
| Real-time: fix stuck crossings | on | Raise ExitAt + force-end the crossing. |
| Real-time: max corrections per pilot | 3 | Safety cap per race. |
| Real-time: learn from manual marshalling | on | Per-pilot priors + feedback tuning. |
| Panel theme | dark | Dark / Light / Auto (browser/OS); applies live. |

## Notes

- Live node thresholds are never changed by the post-race flow; re-tuned
  thresholds are written only to the saved race (like a manual Marshal "Save
  Laps"). Only the real-time guard touches live thresholds, via RotorHazard's
  own calibration path.
- Save path mirrors `resave_laps` (alter pilot race + replace laps + clear
  caches); the lap math is deterministic and matches the Marshal page.
- Manual per-pilot runs process only that pilot and rebuild the race caches.
- Optional add-on: [Claude Marshal AI](https://github.com/izicubed/RotorHazard-Claude-Marshal-AI)
  re-tunes broken calibrations with the Claude API from its own panel.
