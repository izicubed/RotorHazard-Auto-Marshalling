# Claude Auto Marshalling (RotorHazard plugin)

Automatically marshals a race after its laps are saved. Each pilot's stored RSSI
trace is sent to the **Claude API**, which chooses **EnterAt / ExitAt**
thresholds following the RotorHazard tuning methodology; the plugin then
recomputes laps with RotorHazard's own crossing algorithm and **previews** them
for review before you **Apply**.

## Features

- **Auto after save** — on *Save Laps*, a cancellable 5-second countdown starts,
  then each pilot is marshalled automatically.
- **Hybrid AI** — recomputes with the pilot's stored EnterAt/ExitAt first; only
  when a calibration looks broken (no crossings, ExitAt below the noise floor,
  invalid thresholds, or a lap count well off the pilot's other rounds) does it
  ask Claude to find good thresholds. A **manual** run always asks the AI and
  keeps whichever result is better, so good data is never regressed.
- **Preview → Apply** — nothing is written until you press **✓ Apply**.
- **Native integration (Marshal page)** — after calculating, the plugin fills the
  EnterAt/ExitAt fields, redraws the RSSI graph, and fills the lap table.
- **Compact live panel** — shown on the **Run** page (auto-marshalling of the
  just-saved heat, with a **Stop** button) and the **Marshal** page (select any
  heat, run per-pilot or whole-race). The panel tracks the selected heat.
- **Safety** — hard Minimum-Lap-Time (highest-peak cluster resolution),
  Minimum-First-Crossing, late-lap handling, protected manual/API laps, a
  per-pilot self-check, and a **holeshot-aware** scored lap count. Live node
  thresholds are never changed — values are written only to the saved race.

## Requirements

- An **Anthropic API key** (`sk-ant-…`).
- The `anthropic` Python package (declared as a manifest dependency; the
  community plugin manager installs it automatically).

## Install

### Community Plugins manager (recommended)

Settings → Plugins → find **Claude Auto Marshalling**, install, restart.

### Manual

1. Copy `custom_plugins/claude_marshal` into `<rh-data>/plugins/`.
2. `pip install "anthropic>=0.40"` into the RotorHazard environment.
3. Restart the server.

## Setup

Open **Settings → Claude Auto Marshalling** and set your **Claude API key**,
model (Opus 4.8 / Sonnet 5 / Haiku 4.5), and options (auto-marshal on/off,
countdown, strict Minimum-Lap-Time, etc.).

## Usage

- **Run page** — save a heat → countdown → auto-marshalling of that heat → review
  → **Apply** (or **Stop** to halt).
- **Marshal page** — select a heat, press **Marshal** (whole race) or the per-pilot
  ↻ button; the fields, graph and lap table update; press **Apply** to save.

## License

Released under the [MIT NON-AI License](LICENSE), matching the RotorHazard
project license.
