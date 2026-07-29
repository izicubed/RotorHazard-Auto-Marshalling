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
- **What counts as a pass is measured, not guessed.** A craft flying *near* the
  gate also paints a peak, so at race start the plugin measures how high each
  pilot's signal reaches while they are **not** at the gate — every part of
  their earlier traces outside a recorded pass — and requires a candidate to
  beat that ceiling. This is far more reliable than a fraction of their typical
  pass, which swings from round to round. Two bounds stop the requirement
  overshooting into the real passes: a share of the typical rise, and the
  weakest pass the pilot is known to fly. Passes the guard added itself are
  never used as evidence of what a real pass looks like, so one bad correction
  cannot lower the bar for the rest of the event.
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
3. Results are a **preview**: nothing is written until **✓ Apply**, and only
   pilots whose result actually differs from the saved race are written — a
   race that is already correct is left byte-for-byte alone. **Safe mode**
   (default on) flags pilots with blockers for manual review instead of
   touching them.
4. A JSON **report** is logged (and, best-effort, attached to the race).

A whole-race or per-pilot run started **by hand** searches for better
thresholds even when the stored calibration looks fine, and then accepts the
result only if it is strictly better — so the button always does something
useful without ever trading a good calibration for a cosmetic one.

## Two tuning schools: Classic and YGR

There is more than one way to calibrate a gate, and a band that is a defect
under one school is the whole point of the other. The plugin therefore has to be
told which one this timer is tuned in — a switch in the panel header (**Classic |
YGR**), also available as *Tuning school* in Settings. It decides how a race is
judged, what counts as a fault worth reporting, and what shape a repair takes.

### Classic — a wide hysteresis band

The RotorHazard handbook approach. EnterAt sits below the peak of every real
crossing; ExitAt sits far below it but clear of the noise floor. The wide gap is
hysteresis: it holds a pass together through the dips inside its own peak, so one
pass is one crossing. Two numbers to get right, and the failure modes are the
familiar ones — EnterAt in the noise invents passes, ExitAt below the noise floor
lets two passes merge into one.

In this school the plugin flags a squeezed band (**squeezed band**), never
proposes one, and treats a pile of sub-Minimum-Lap crossings as evidence that
EnterAt has slipped into the noise.

### YGR — ExitAt parked just under EnterAt

Named after the pilot who arrived at it: **YGR** has run his own timer this way
for two seasons of club racing, and it is the calibration his events are judged
on. It inverts the usual advice — ExitAt goes **1–2 counts below EnterAt** — and
it is a deliberate trade, not a mistake:

- **One number to tune instead of two.** The gate is characterised by a single
  threshold. There is no second value whose relationship to the noise floor has
  to be re-checked whenever conditions, antennas or power change.
- **Two passes can never merge.** The classic failure where a too-low ExitAt
  keeps a crossing open, swallowing the next pass into the same crossing, is
  structurally impossible: a crossing closes the moment the signal falls a
  couple of counts back through the threshold.
- **The timestamp is pinned to the threshold crossing.** The peak window is very
  short, so the recorded moment is essentially "when the craft reached the
  level", which repeats well from lap to lap and from pilot to pilot.
- **The cost is duplicate crossings, and it is paid by Minimum Lap Time.** A
  single pass whose peak wobbles across the threshold registers several times.
  RotorHazard's own Minimum-Lap-Time resolution collapses the burst back to one
  pass, which is why this school needs Minimum Lap Time set sensibly — it is
  load-bearing here, not a safety net.

With YGR selected the plugin stops treating any of that as a fault: the squeezed
band is expected, duplicate crossings are no longer read as a bad EnterAt, and a
re-tune keeps ExitAt tucked under EnterAt instead of widening the band. What it
still reports is a genuinely degenerate case — **ExitAt equal to EnterAt**, which
leaves no hysteresis at all — and it repairs that by moving ExitAt down by the
usual count or two rather than by converting the seat to Classic. A seat with a
wide band while YGR is selected is flagged the other way round (**wide band**),
since it is then the odd one out.

The real-time guard follows the same rule: when it re-tunes a seat mid-race it
leaves the band shaped the way the operator calibrates, so it can never quietly
convert a timer from one school to the other between rounds.

### Protecting what is already there

The post-race flow is built so that running it on a correctly marshalled race
changes nothing:

- **Sub-second race start recovered.** RotorHazard saves the race start
  (`start_time_monotonic`, a float) into an integer column, so up to a full
  second is lost. Laps recorded live were timestamped against the precise
  value, so any recompute from the trace — this plugin's and RotorHazard's own
  Marshal page alike — lands that fraction late. The plugin recovers the lost
  fraction by matching its crossings against the laps already recorded for the
  race, so recomputed timestamps line up with the live ones.
- **Your deletions stick.** A pass deleted on the Marshal page is not brought
  back to life by a recompute. One physical pass often shows up as a pair of
  crossings where you kept one and deleted the other, so a deletion is only
  honoured when there is no kept pass in the same neighbourhood.
- **Live timings win.** When the thresholds and the passes agree, the stored
  lap times are kept — the node timed them at full sampling rate, the stored
  peak/nadir history cannot beat that.
- **A re-tune never loses a good pass.** A candidate calibration is rejected if
  it would drop a properly spaced pass that broke no Minimum-Lap-Time rule,
  even when the other rounds of the heat suggest a lower lap count.
- **"Did not fly" is not a fault.** A seat whose trace never rises to gate
  level and that has no recorded lap is reported as such and left untouched,
  instead of demanding a manual calibration review.
- **A squeezed calibration is reported, never copied.** EnterAt and ExitAt are a
  hysteresis pair: with no gap between them every dip inside one pass ends the
  crossing, so a single pass is recorded several times and Minimum Lap Time has
  to delete the duplicates. Such a calibration raises a **squeezed band**
  warning, and the re-tune will never propose one — including when it is copied
  from the seat's other rounds.

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
