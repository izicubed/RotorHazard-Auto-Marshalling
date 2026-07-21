# Claude Auto Marshalling (RotorHazard plugin)

Marshalling for RotorHazard in two layers:

1. **Real-time marshalling (in-race, fully local — no API key needed).** A
   deterministic RSSI-analysis guard that runs on the timer itself and fixes
   the classic *missed holeshot* while the race is still running, so lap
   counts are correct immediately.
2. **Post-race AI marshalling (optional).** After a race is saved, each
   pilot's stored RSSI trace can be re-tuned by the **Claude API**, with a
   preview → **Apply** workflow. This is the only part that needs an
   Anthropic API key — without a key (or without internet at the race site)
   the plugin simply runs on the real-time layer alone.

## Real-time marshalling — the algorithm

The most common timing failure is an unregistered first pass (holeshot): the
craft flies the gate but **EnterAt** sits above the actual RSSI peak, no
crossing fires, and every later lap is counted one off. From `RACE_START` to
`RACE_STOP` the guard polls each seat's live RSSI trace (~3×/s, the same
peak/nadir history RotorHazard stores for marshalling) and runs a fully
**deterministic** pipeline — no AI, no network:

- **Noise floor + prominence.** The race's noise floor is the trace minimum;
  a candidate pass must rise above it by a per-pilot threshold *and* rise
  equally from the level before it (rejects the launch-pad plateau).
- **Completed-peak detection.** A peak counts only after the signal has
  dropped ≥35% of its height and ≥1.2 s have passed — never mid-crossing.
- **Plausibility gates.** The peak must match no recorded lap (±2.5 s), obey
  Minimum Lap Time / Minimum First Crossing, stay below the pilot's median
  lap-time floor (a lap faster than 60% of the median is rejected), and —
  crucially — sit **below EnterAt**, i.e. be a pass the node could not have
  detected itself.
- **Correction.** The missed pass is injected into the live race at the
  peak's true timestamp (the first pass becomes **lap 0 / the holeshot**, so
  numbering is right at once), and EnterAt/ExitAt are re-tuned through
  RotorHazard's normal calibration path so the node catches the following
  passes on its own. A **stuck crossing** (ExitAt at/below the noise floor)
  is force-ended with a raised ExitAt after ~8 s.
- **Learning from you.** At race start the pilot's saved races provide priors
  (typical crossing rise, median lap time); afterwards, manual marshalling
  feeds back — deleting an auto-added lap makes detection stricter for that
  pilot, adding a lap where the guard skipped makes it more sensitive.

Every correction is announced (priority message + a **Real-time Marshalling**
feed on the Run/Marshal pages), capped per pilot per race, and logged.

## Post-race AI marshalling (optional, needs an API key)

- **Auto after save** — on *Save Laps*, a cancellable countdown starts, then
  each pilot is marshalled automatically. Skipped entirely when no API key is
  set or the Claude API is unreachable (offline race sites run real-time
  only; the availability probe takes ~a second).
- **Hybrid AI** — recomputes with the pilot's stored EnterAt/ExitAt first;
  only when a calibration looks broken (no crossings, ExitAt below the noise
  floor, invalid thresholds, or a lap count well off the pilot's other
  rounds) does it ask Claude for better thresholds. A **manual** run always
  asks the AI and keeps whichever result is better, so good data is never
  regressed; manual runs while offline fall back to a local recompute.
- **Preview → Apply** — nothing is written until you press **✓ Apply**.
- **Native integration** — on the Marshal page the plugin fills the
  EnterAt/ExitAt fields, redraws the RSSI graph and fills the lap table.
- **Safety** — hard Minimum-Lap-Time (highest-peak cluster resolution),
  Minimum-First-Crossing, late-lap handling, protected manual/API laps,
  per-pilot self-checks, and a holeshot-aware scored lap count. Saved-race
  values only; live node thresholds are changed by the real-time layer alone.

## UI

A compact live panel on the **Run** page (auto flow of the just-saved heat,
with Stop) and the **Marshal** page (per-pilot / whole-race manual runs), plus
the real-time corrections feed. Both panels support **dark / light / auto**
(browser/OS) themes — Settings → *Panel theme*.

## Requirements

- RotorHazard **4.3.1+** (4.4 recommended; version differences are handled
  automatically).
- **No API key is required** for real-time marshalling — it runs standalone
  on the timer (Raspberry Pi).
- Optional: an **Anthropic API key** (`sk-ant-…`) for the post-race AI layer.
  The `anthropic` Python package is declared as a manifest dependency and is
  installed automatically by the community plugin manager.

## Install

### Community Plugins manager (recommended)

Settings → Plugins → find **Claude Auto Marshalling**, install, restart.

### Manual

1. Copy `custom_plugins/claude_marshal` into `<rh-data>/plugins/`.
2. `pip install "anthropic>=0.40"` into the RotorHazard environment
   (only needed for the post-race AI layer).
3. Restart the server.

## Setup

Everything works out of the box — real-time marshalling is enabled by
default. Open **Settings → Claude Auto Marshalling** to tune it (scope,
sensitivity, live threshold re-tune, learning, per-pilot cap, theme) and,
if you want the post-race AI layer, set your **Claude API key** and model
(Opus 4.8 / Sonnet 5 / Haiku 4.5).

## Usage

- **During a race** — nothing to do: missed passes are added and announced as
  they happen; watch the *Real-time Marshalling* feed.
- **Run page** — save a heat → countdown → auto AI marshalling of that heat →
  review → **Apply** (or **Stop**). Only when Claude is available.
- **Marshal page** — select a heat, press **Marshal** (whole race) or the
  per-pilot ↻ button; fields, graph and lap table update; **Apply** to save.
  Your manual corrections also train the real-time guard.

## License

Released under the [MIT NON-AI License](LICENSE), matching the RotorHazard
project license.
