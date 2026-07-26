# Auto Marshalling (RotorHazard plugin)

Automatic marshalling for RotorHazard in two layers — **fully local and
deterministic: no API keys, no internet, nothing leaves the timer.**

1. **Real-time guard (in-race).** A deterministic RSSI-analysis guard that
   runs on the timer itself and fixes the classic *missed holeshot* while the
   race is still running, so lap counts are correct immediately.
2. **Post-race check.** After a race is saved, each pilot's laps are
   recomputed from the stored RSSI trace; when a calibration looks broken it
   is re-tuned against the pilot's other rounds, with a preview → **Apply**
   workflow. Nothing is written until you press Apply.

> Looking for the Claude-powered post-race re-tuning this plugin used to
> ship? It now lives in its own optional add-on:
> [Claude Marshal AI](https://github.com/izicubed/RotorHazard-Claude-Marshal-AI).

## Real-time guard — the algorithm

The most common timing failure is an unregistered first pass (holeshot): the
craft flies the gate but **EnterAt** sits above the actual RSSI peak, no
crossing fires, and every later lap is counted one off. From `RACE_START` to
`RACE_STOP` the guard polls each seat's live RSSI trace (~3×/s, the same
peak/nadir history RotorHazard stores for marshalling) and runs a fully
**deterministic** pipeline:

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

Every correction is announced (priority message + a corrections feed in the
panel), capped per pilot per race, and logged.

## Post-race check

- **Auto after save** — on *Save Laps*, a cancellable countdown starts, then
  each pilot's stored trace is recomputed with their stored EnterAt/ExitAt.
- **Local re-tune** — only when a calibration looks broken (no crossings,
  ExitAt below the noise floor, invalid thresholds, a pile of sub-Minimum-Lap
  noise crossings, or a lap count well off the pilot's other rounds) are the
  thresholds repaired: candidates from the seat's other rounds plus a probe
  grid over the trace are scored against the pilot's history, and the most
  plausible wins. Good data is never regressed.
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
the real-time corrections feed. The panel header carries an **AUTO: ON/OFF**
master toggle — one click disables every automatic action (the post-race flow
and the in-race guard) for heats where you don't want it; manual runs from the
Marshal page keep working. Both panels support **dark / light / auto**
(browser/OS) themes — Settings → *Panel theme*.

## Requirements

- RotorHazard **4.3.1+** (4.4 recommended; version differences are handled
  automatically).
- Nothing else. No API keys, no internet, no Python dependencies.

## Install

### Community Plugins manager (recommended)

Settings → Plugins → find **Auto Marshalling**, install, restart.

### Manual

1. Copy `custom_plugins/auto_marshal` into `<rh-data>/plugins/`.
2. Restart the server.

### Upgrading from *Claude Auto Marshalling* (≤ 1.4.x)

This plugin is the successor of **Claude Auto Marshalling** with the Claude
API layer removed (it moved to the optional
[Claude Marshal AI](https://github.com/izicubed/RotorHazard-Claude-Marshal-AI)
add-on). Remove the old `claude_marshal` folder from `<rh-data>/plugins/`
before installing this one — all settings and the learned per-pilot
sensitivity factors carry over automatically.

## Usage

- **During a race** — nothing to do: missed passes are added and announced as
  they happen; watch the corrections feed in the panel.
- **Run page** — save a heat → countdown → automatic check of that heat →
  review → **Apply** (or **Stop**).
- **Marshal page** — select a heat, press **Marshal** (whole race) or the
  per-pilot ↻ button; fields, graph and lap table update; **Apply** to save.
  Your manual corrections also train the real-time guard.

## License

Released under the [MIT NON-AI License](LICENSE), matching the RotorHazard
project license.
