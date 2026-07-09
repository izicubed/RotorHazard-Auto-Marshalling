'''
Claude Auto-Marshalling plugin for RotorHazard.

Implements rotorhazard_auto_marshalling_plugin_logic.md: after a race is saved
it runs a cancellable countdown, then marshals each pilot — recomputing laps
from the stored EnterAt/ExitAt and only asking Claude to re-tune thresholds when
a calibration looks broken (hybrid). Dry-run/safe-mode, hard Minimum-Lap-Time,
self-check, history baseline, blockers/warnings and a JSON report are applied,
with a panel on Run (under the pilot table) and Marshal (above the RSSI graph)
offering cancel and manual race / per-pilot runs.
'''

from eventmanager import Evt
from .marshal_ai import (
    MarshalController, EV_GET_STATE, EV_CANCEL, EV_RUN_RACE, EV_RUN_PILOT,
    EV_APPLY, EV_CONTEXT,
)


def initialize(rhapi):
    controller = MarshalController(rhapi)
    controller.register_blueprint()

    rhapi.events.on(Evt.STARTUP, controller.on_startup)
    rhapi.events.on(Evt.LAPS_SAVE, controller.on_laps_save)
    rhapi.events.on(Evt.LAPS_RESAVE, controller.on_laps_resave)

    rhapi.ui.socket_listen(EV_GET_STATE, controller.on_get_state)
    rhapi.ui.socket_listen(EV_CANCEL, controller.on_cancel)
    rhapi.ui.socket_listen(EV_RUN_RACE, controller.on_run_race)
    rhapi.ui.socket_listen(EV_RUN_PILOT, controller.on_run_pilot)
    rhapi.ui.socket_listen(EV_APPLY, controller.on_apply)
    rhapi.ui.socket_listen(EV_CONTEXT, controller.on_context)
