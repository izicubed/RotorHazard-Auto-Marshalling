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
    EV_APPLY, EV_CONTEXT, EV_SET_ENABLED,
)
from .realtime_guard import RealtimeGuard, EV_RT_GET


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
    # panel Enabled/Disabled toggle (master switch: auto flow + realtime guard)
    rhapi.ui.socket_listen(EV_SET_ENABLED, controller.on_set_enabled)

    # Real-time (fully local, no AI calls) in-race marshalling guard:
    # catches missed passes (holeshot) and re-tunes EnterAt/ExitAt live.
    guard = RealtimeGuard(rhapi)
    rhapi.events.on(Evt.STARTUP, lambda _a=None: guard.register_ui(),
                    name='claude_marshal_rt_ui')
    rhapi.events.on(Evt.RACE_START, guard.on_race_start,
                    name='claude_marshal_rt_start')
    rhapi.events.on(Evt.RACE_STOP, guard.on_race_stop,
                    name='claude_marshal_rt_stop')
    # learning loop: stash the decision log with the saved race, then compare
    # it with the operator's manual marshalling to tune per-pilot sensitivity
    rhapi.events.on(Evt.LAPS_SAVE, guard.on_laps_save,
                    name='claude_marshal_rt_save')
    rhapi.events.on(Evt.LAPS_RESAVE, guard.on_laps_resave,
                    name='claude_marshal_rt_resave')
    rhapi.ui.socket_listen(EV_RT_GET, guard.on_rt_get)
    # Theme option (cm_theme) changed: restyle both panels live.
    rhapi.events.on(Evt.OPTION_SET, controller.on_option_set,
                    name='claude_marshal_theme')
    rhapi.events.on(Evt.OPTION_SET, guard.on_option_set,
                    name='claude_marshal_rt_theme')
