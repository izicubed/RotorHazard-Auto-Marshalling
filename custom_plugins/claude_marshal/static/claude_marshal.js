/* Claude Auto-Marshalling - inline panel (Run: under pilot table, Marshal: above
 * RSSI graph). Driven by the server `claude_marshal_state` snapshot; requests it
 * on every page load so progress survives navigation. Offers cancel during the
 * countdown and manual race / per-pilot runs. */
(function () {
	'use strict';

	if (window.__rhClaudeMarshal) { return; }
	window.__rhClaudeMarshal = true;
	if (typeof io === 'undefined') { return; }

	var socket = null, state = {}, lastElapsed = 0, lastTs = 0, running = false, ticker = null;

	function ensureCss() {
		if (document.getElementById('rh-cm-css')) { return; }
		var l = document.createElement('link');
		l.id = 'rh-cm-css'; l.rel = 'stylesheet';
		l.href = '/claude_marshal/static/claude_marshal.css';
		(document.head || document.documentElement).appendChild(l);
	}
	function el(tag, cls, html) {
		var e = document.createElement(tag);
		if (cls) { e.className = cls; }
		if (html != null) { e.innerHTML = html; }
		return e;
	}
	function onMarshalPage() { return !!document.getElementById('race-graph'); }
	function onRunPage() { return !!document.getElementById('leaderboard'); }
	function onSupportedPage() { return onMarshalPage() || onRunPage(); }

	// Friendly, non-scary labels for status codes (chip text + tooltip).
	var LABELS = {
		BAD_CALIBRATION_UNRESOLVED: ['check manually', 'Automatic thresholds could not be resolved — review on the Marshal graph.'],
		BAD_CALIBRATION_NO_API: ['add API key', 'Calibration looks off, but no Claude API key is set to re-tune it.'],
		AI_RETHRESHOLD_ERROR: ['AI unavailable', 'The Claude re-tune request failed.'],
		PROTECTED_LAP_UNDER_MIN_LAP: ['short manual lap', 'A manual/API lap is shorter than the Minimum Lap Time; left untouched.'],
		HIGH_CONFIDENCE_SHORT_FALSE_PASS: ['likely false lap', 'A lap is far faster than this pilot usually flies.'],
		FAST_VS_HISTORY: ['fast lap', 'Faster than this pilot’s typical lap.'],
		SLOW_VS_HISTORY: ['slow lap', 'Slower than this pilot’s typical lap.'],
		INSUFFICIENT_HISTORY: ['little history', 'Not enough past laps to compare against.'],
		NO_RSSI_HISTORY: ['no RSSI', 'No RSSI trace stored for this pilot.'],
		INVALID_THRESHOLDS: ['bad thresholds', 'Stored EnterAt is not above ExitAt.'],
		HISTORY_ARRAY_LENGTH_MISMATCH: ['bad RSSI data', 'RSSI value/time arrays differ in length.'],
		NON_MONOTONIC_HISTORY_TIMES: ['bad RSSI data', 'RSSI timestamps are out of order.'],
		UNSUPPORTED_MARSHAL_TYPE: ['not RSSI', 'This pilot has no RSSI-history marshal data.']
	};
	function label(code) {
		var key = String(code || '').split(':')[0].split(' ')[0];
		if (LABELS[key]) { return LABELS[key]; }
		if (key.indexOf('SELF_CHECK_FAILED') === 0) { return ['self-check', 'A result sanity check did not pass.']; }
		return [key.toLowerCase().replace(/_/g, ' '), String(code)];
	}

	var panel;
	function place() {
		if (panel && panel.parentNode) { return; }
		if (!panel) { return; }
		var anchor = document.getElementById('race-graph');   // Marshal: above graph
		if (anchor && anchor.parentNode) {
			anchor.parentNode.insertBefore(panel, anchor);
			return;
		}
		anchor = document.getElementById('leaderboard');       // Run: under table
		if (anchor && anchor.parentNode) {
			anchor.parentNode.insertBefore(panel, anchor.nextSibling);
			return;
		}
		anchor = document.getElementById('rh-topbar');
		if (anchor && anchor.parentNode) { anchor.parentNode.insertBefore(panel, anchor.nextSibling); return; }
		if (document.body) { document.body.insertBefore(panel, document.body.firstChild); }
	}

	function ensurePanel() {
		if (panel) { place(); return panel; }
		panel = el('div', 'rh-cm'); panel.id = 'rh-cm';
		panel.innerHTML =
			'<div class="rh-cm-head"><div class="rh-cm-title"><span class="rh-cm-spark">✦</span> Auto Marshalling</div>' +
			'<div class="rh-cm-mode"></div><div class="rh-cm-timer"></div></div>' +
			'<div class="rh-cm-sub"></div>' +
			'<div class="rh-cm-ctl"></div>' +
			'<div class="rh-cm-track"><div class="rh-cm-bar"></div></div>' +
			'<div class="rh-cm-rows"></div>' +
			'<div class="rh-cm-foot"></div>';
		place();
		return panel;
	}
	function q(sel) { return panel.querySelector(sel); }

	function tick() {
		var t = q('.rh-cm-timer'); if (!t) { return; }
		var s = lastElapsed + (running ? (Date.now() - lastTs) / 1000 : 0);
		t.textContent = s.toFixed(1) + 's';
	}
	function startTicker() { stopTicker(); ticker = setInterval(tick, 100); }
	function stopTicker() { if (ticker) { clearInterval(ticker); ticker = null; } }

	function busy() { return state.phase === 'running' || state.phase === 'waiting_countdown'; }
	function seatLabel(s) { return 'S' + ((s | 0) + 1); }

	function chip(text, cls) { return '<span class="rh-cm-chip ' + cls + '">' + text + '</span>'; }

	// One compact cell per pilot; cells sit side-by-side on a single row.
	function pilotCell(p) {
		var c = el('div', 'rh-cm-cell rh-cm-' + (p.status || 'idle'));
		var tips = [];
		function chipFor(code, cls) {
			var lab = label(code); tips.push(lab[1]);
			return '<span class="rh-cm-chip ' + cls + '" title="' + lab[1] + '">' + lab[0] + '</span>';
		}

		var top = el('div', 'rh-cm-ctop');
		top.innerHTML = '<span class="rh-cm-seat">' + seatLabel(p.seat) + '</span>' +
			'<span class="rh-cm-name">' + (p.callsign || 'Seat') + '</span>';
		if (p.pilotrace_id != null && !busy()) {
			var b = el('button', 'rh-cm-ico', '↻');
			b.title = 'Marshal this pilot';
			b.addEventListener('click', function () {
				socket.emit('claude_marshal_run_pilot',
					{ race_id: state.race_id, pilotrace_id: p.pilotrace_id });
			});
			top.appendChild(b);
		}
		c.appendChild(top);

		var bot = el('div', 'rh-cm-cbot');
		if (p.status === 'ok' || p.status === 'warn') {
			var h = '<span class="rh-cm-laps">' + (p.laps != null ? p.laps : '?') + '</span>' +
				'<span class="rh-cm-thr">' + p.enter_at + '/' + p.exit_at + '</span>';
			if (p.changed) { h += chip('AI', 'rh-cm-c-info'); }
			(p.warnings || []).forEach(function (w) {
				if (String(w).indexOf('AI_RETHRESHOLD') === 0) { return; }
				h += chipFor(w, 'rh-cm-c-warn');
			});
			bot.innerHTML = h;
		} else if (p.status === 'err') {
			bot.innerHTML = (p.blockers || ['review']).map(function (b) { return chipFor(b, 'rh-cm-c-review'); }).join('');
		} else if (p.status === 'run') {
			bot.innerHTML = '<span class="rh-cm-spin"></span>';
		} else {
			bot.innerHTML = '<span class="rh-cm-dot"></span>';
		}
		c.appendChild(bot);

		if (p.reasoning) { tips.unshift(p.reasoning); }
		if (tips.length) { c.title = tips.join('\n'); }
		return c;
	}

	function render(s) {
		state = s || {};
		var phase = state.phase || 'idle';
		var isMarshal = onMarshalPage();
		var isRun = onRunPage() && !isMarshal;
		ensurePanel();
		// On /run only the auto flow (current heat just saved) is relevant — never
		// a previously selected/marshalled race.
		if (isRun && state.origin !== 'auto') {
			panel.classList.add('rh-cm-hidden');
			return;
		}
		// Hide only when idle with no heat context yet; otherwise always show.
		if (phase === 'idle' && !(state.pilots && state.pilots.length)) {
			panel.classList.add('rh-cm-hidden');
			return;
		}
		panel.classList.remove('rh-cm-hidden');
		panel.className = 'rh-cm rh-cm-phase-' + phase;

		q('.rh-cm-mode').innerHTML = (phase === 'complete' && state.can_apply)
			? chip('review', 'rh-cm-c-warn') : (phase === 'applied' ? chip('applied', 'rh-cm-c-ok') : '');

		var bits = [];
		if (state.heat) { bits.push(state.heat); }
		if (state.round) { bits.push('Round ' + state.round); }
		if (state.model && phase !== 'idle') { bits.push(state.model); }
		q('.rh-cm-sub').textContent = bits.join('  ·  ');

		// controls
		var ctl = q('.rh-cm-ctl'); ctl.innerHTML = '';
		var track = q('.rh-cm-track'), bar = q('.rh-cm-bar');
		if (phase === 'waiting_countdown') {
			var num = el('span', 'rh-cm-num', String(state.countdown));
			num.style.animation = 'none'; void num.offsetWidth; num.style.animation = '';  // restart pulse each tick
			var wrap = el('div', 'rh-cm-count');
			wrap.appendChild(num);
			wrap.appendChild(el('span', 'rh-cm-count-lbl', 'AI marshalling starting…'));
			ctl.appendChild(wrap);
			var cancel = el('button', 'rh-cm-btn rh-cm-btn-cancel', 'Cancel');
			cancel.addEventListener('click', function () {
				socket.emit('claude_marshal_cancel', { race_id: state.race_id });
			});
			ctl.appendChild(cancel);
		} else if (phase === 'running') {
			// Allow stopping a run in progress (stops after the current pilot).
			var stop = el('button', 'rh-cm-btn rh-cm-btn-cancel', 'Stop');
			stop.addEventListener('click', function () {
				socket.emit('claude_marshal_cancel', { race_id: state.race_id });
			});
			ctl.appendChild(stop);
		} else {
			if (state.can_apply) {
				var apply = el('button', 'rh-cm-btn rh-cm-btn-apply', '✓ Apply calculated values');
				apply.addEventListener('click', function () {
					socket.emit('claude_marshal_apply', { race_id: state.race_id });
				});
				ctl.appendChild(apply);
			}
			// Whole-race run: full control on /marshal; on /run only re-run the
			// current heat that was just marshalled (never a previous race).
			var showRun = isMarshal || (isRun && state.can_apply);
			if (showRun) {
				var run = el('button', 'rh-cm-btn',
					(state.can_apply ? 'Recalculate' : (state.race_id ? 'Marshal this race' : 'Marshal last saved race')));
				run.addEventListener('click', function () {
					socket.emit('claude_marshal_run_race', state.race_id ? { race_id: state.race_id } : {});
				});
				ctl.appendChild(run);
			}
		}

		// progress / countdown bar
		var total = state.total || 0, done = state.done || 0;
		if (phase === 'waiting_countdown') {
			track.classList.add('rh-cm-track-count');
			var ct = state.countdown_total || 5;
			// jump to full for the top tick (no transition), then deplete smoothly
			if (state.countdown >= ct) { bar.style.transition = 'none'; }
			else { bar.style.transition = ''; }
			bar.style.width = Math.max(0, (state.countdown - 1) / ct * 100) + '%';
			void bar.offsetWidth;
		} else {
			track.classList.remove('rh-cm-track-count');
			bar.style.transition = '';
			bar.style.width = (total ? Math.round((done / total) * 100) : (phase === 'complete' ? 100 : 0)) + '%';
		}

		// pilots — one horizontal row of compact cells
		var rows = q('.rh-cm-rows'); rows.innerHTML = '';
		(state.pilots || []).forEach(function (p) { rows.appendChild(pilotCell(p)); });

		// timer
		lastElapsed = state.elapsed || 0; lastTs = Date.now(); running = (phase === 'running');
		tick(); if (running) { startTicker(); } else { stopTicker(); }
		q('.rh-cm-timer').textContent = (phase === 'waiting_countdown') ? '' : (state.elapsed != null ? state.elapsed + 's' : '');

		// footer
		var foot = q('.rh-cm-foot');
		if (phase === 'error') { foot.textContent = 'Error: ' + (state.message || 'failed'); }
		else if (phase === 'cancelled') { foot.textContent = state.message || 'Cancelled'; }
		else if (phase === 'applied') {
			foot.textContent = 'Applied to ' + (state.applied_count || 0) + ' pilot' +
				(state.applied_count === 1 ? '' : 's') + '.';
		}
		else if (phase === 'complete') {
			var sm = state.summary || {};
			foot.textContent = state.can_apply
				? ('Calculated — review, then press Apply. ' + (sm.pilots_changed || 0) +
				   ' re-tuned, ' + (sm.warnings || 0) + ' warnings, ' + (sm.blockers || 0) + ' to review · ' +
				   (state.elapsed != null ? state.elapsed + 's' : ''))
				: ('Nothing to apply — ' + (sm.blockers || 0) + ' pilot(s) need manual review.');
		} else if (phase === 'running') { foot.textContent = done + ' / ' + total + ' pilots'; }
		else { foot.textContent = ''; }

		// Reflect the computed values on the native Marshal graph + lap table.
		if (phase === 'complete' || phase === 'applied') { applyToMarshalUI(); }
		else { applyToMarshalUI._last = null; }
	}

	// On the Marshal page, push the computed thresholds for the currently-viewed
	// pilot into the native EnterAt/ExitAt fields and trigger the page's own
	// recompute — this moves the graph lines and fills the lap table natively.
	function applyToMarshalUI() {
		if (!onMarshalPage()) { return; }
		var crd = window.current_race_data;
		var m = window.marshal;
		if (!crd || crd.pilotrace_id == null) { return; }
		var match = null;
		(state.pilots || []).forEach(function (p) {
			if (p.pilotrace_id === crd.pilotrace_id && p.enter_at != null) { match = p; }
		});
		if (!match) { return; }
		var key = match.pilotrace_id + ':' + match.enter_at + '/' + match.exit_at;
		if (applyToMarshalUI._last === key) { return; }

		// Drive the native marshal object directly (not via jQuery .trigger, which
		// may not reach the page's handlers if a different jQuery instance is in
		// scope). setExit/setEnter update the fields, recompute, and redraw the
		// graph + lap table. Fall back to the change event if the object is absent.
		var ok = false;
		if (m && m.race && typeof m.setEnter === 'function' && typeof m.setExit === 'function') {
			try {
				m.setExit(parseInt(match.exit_at, 10));    // set lower bound first
				m.setEnter(parseInt(match.enter_at, 10));
				if (typeof m.recalcRace === 'function') { m.recalcRace(); }  // ensure redraw
				ok = true;
			} catch (e) { ok = false; }
		}
		if (!ok) {
			var $ = window.jQuery;
			if ($) {
				$('#exitat').val(match.exit_at).trigger('change');
				$('#enterat').val(match.enter_at).trigger('change');
			} else { return; }
		}
		applyToMarshalUI._last = key;
	}

	function sendContext() {
		if (!onMarshalPage() || !socket) { return; }
		var h = document.getElementById('selected_heat');
		var r = document.getElementById('selected_round');
		if (h && r && h.value !== '' && r.value !== '') {
			socket.emit('claude_marshal_context',
				{ heat_id: parseInt(h.value), round: parseInt(r.value) });
		}
	}

	function start() {
		// The script is only injected on Run and Marshal, but their anchors may
		// render after us — wait for one before building (bail after ~10s).
		if (!onSupportedPage()) {
			if (start._tries === undefined) { start._tries = 0; }
			if (start._tries++ < 40) { setTimeout(start, 250); }
			return;
		}
		ensureCss();
		socket = io.connect(location.protocol + '//' + document.domain + ':' + location.port);
		socket.on('connect', function () {
			socket.emit('claude_marshal_get_state', {});
			setTimeout(sendContext, 800);
		});
		socket.on('claude_marshal_state', function (s) { render(s); });
		// react to pilot selection on the Marshal page
		if (onMarshalPage()) {
			document.addEventListener('change', function (e) {
				if (!e.target || !/^selected_(heat|round|pilot)$/.test(e.target.id)) { return; }
				sendContext();
				if (e.target.id === 'selected_pilot') {
					// Native page reloads this pilot async; re-apply our values after.
					applyToMarshalUI._last = null;
					setTimeout(applyToMarshalUI, 700);
				}
			});
		}
		var tries = 0, iv = setInterval(function () {
			if (panel) { place(); }
			if (++tries > 20) { clearInterval(iv); }
		}, 500);
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', start);
	} else { start(); }
})();
