/* CIP dashboard renderer. Called by the shell on every poll.
 *
 * Reads only what the server exposes. The branches dict in status tells us
 * which pipes are flowing; we never recompute physics here.
 */
(function () {
  'use strict';

  var FLUID_COLOR = { 1: 'water', 2: 'caustic', 3: 'acid', 4: 'mixture' };
  var FAULT_LABELS = {
    0: 'Pump on without liquid',
    1: 'Pump on without flow route',
    2: 'Multiple source valves open',
    3: 'Overtemperature',
    4: 'Drain blocked by chemical residue',
  };

  var HIST_MAX = 90;
  var levelHist = [];
  var tempHist = [];
  var lastHistPush = 0;

  function el(id) { return document.getElementById(id); }
  function txt(id, s) { var e = el(id); if (e) e.textContent = s; }

  function setPipe(id, active, fluid) {
    var e = el(id);
    if (!e) return;
    e.setAttribute('class', 'pipe' + (active ? ' flow ' + fluid : ''));
  }

  function setValve(id, commanded, flowing) {
    var e = el(id);
    if (!e) return;
    var cls = 'valve';
    if (flowing) cls += ' flow';
    else if (commanded) cls += ' cmd';
    e.setAttribute('class', cls);
  }

  function badge(text, kind) {
    var s = document.createElement('span');
    s.className = 'badge' + (kind ? ' ' + kind : '');
    s.textContent = text;
    return s;
  }

  function faultList(word) {
    var out = [];
    for (var b = 0; b < 16; b++) {
      if (word & (1 << b)) {
        out.push({ bit: b, label: FAULT_LABELS[b] || ('fault bit ' + b) });
      }
    }
    return out;
  }

  function deriveAction(s, branches) {
    if (s.FaultWord !== 0) return 'Fault';
    if (branches.chemicalRecovery) return 'Chemical recovery';
    var inlet =
      branches.waterInlet ? 'water' :
      branches.causticInlet ? 'caustic' :
      branches.acidInlet ? 'acid' : null;
    var heat = (s.StatusWord & (1 << 5)) ? ' + heating' : '';
    if (inlet && branches.returnFlow) return 'Wash / circulate (' + inlet + ')' + heat;
    if (inlet) return 'Filling: ' + inlet + heat;
    if (branches.returnFlow) return 'Circulating (return)' + heat;
    if (branches.drainFlow) return 'Draining (pumped)';
    if (branches.gravityDrain) return 'Draining (gravity)';
    if (heat) return 'Heating (idle flow)';
    return 'Idle';
  }

  function pushHist(arr, v) {
    arr.push(v);
    if (arr.length > HIST_MAX) arr.shift();
  }

  function drawTrend(id, arr, lo, hi, color) {
    var c = el(id);
    if (!c) return;
    var ctx = c.getContext('2d');
    var w = c.width, h = c.height;
    ctx.clearRect(0, 0, w, h);
    if (arr.length < 2) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (var i = 0; i < arr.length; i++) {
      var x = (i / (HIST_MAX - 1)) * w;
      var norm = Math.max(0, Math.min((arr[i] - lo) / (hi - lo), 1));
      var y = h - norm * (h - 4) - 2;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  window.renderState = function (state) {
    var m = state.measurements || {};
    var s = state.status || {};

    // Metric tiles
    txt('m-level', (m.LevelPercent || 0).toFixed(1));
    txt('m-flow',  (m.FlowLpm || 0).toFixed(1));
    txt('m-temp',  (m.TemperatureC || 0).toFixed(1));
    txt('m-cond',  (m.Conductivity || 0).toFixed(1));

    // Tank fill
    var top = 186, H = 238;
    var lvl = Math.max(0, Math.min(m.LevelPercent || 0, 100));
    var fillH = H * lvl / 100;
    var liq = el('liquid');
    if (liq) {
      liq.setAttribute('y', (top + H - fillH).toFixed(1));
      liq.setAttribute('height', fillH.toFixed(1));
      var fc = FLUID_COLOR[s.LiquidCode] || null;
      liq.setAttribute('fill', fc ? ('#' + ({
        water: '1e88e5', caustic: 'fb8c00', acid: 'd81b8c', mixture: '7e57c2'
      })[fc]) : '#cbd5e1');
    }
    txt('liquid-name', s.LiquidName || 'Empty');

    // Pipes (server-driven branches)
    var fluid = FLUID_COLOR[s.LiquidCode] || 'water';
    var suction = s.pumpRunning && (m.FlowLpm || 0) > 0.1;
    setPipe('p-water',   !!s.waterInlet, 'water');
    setPipe('p-acid',    !!s.acidInlet, 'acid');
    setPipe('p-caustic', !!s.causticInlet, 'caustic');
    setPipe('p-suction', suction || !!s.gravityDrain, fluid);
    setPipe('p-common',  suction || !!s.gravityDrain, fluid);
    setPipe('p-return',  !!s.returnFlow, fluid);
    setPipe('p-drain',   !!s.drainFlow || !!s.gravityDrain, fluid);

    // Valves
    setValve('v-water',   false, !!s.waterInlet);
    setValve('v-acid',    false, !!s.acidInlet);
    setValve('v-caustic', false, !!s.causticInlet);
    setValve('v-return',  false, !!s.returnFlow);
    setValve('v-drain',   false, !!s.drainFlow || !!s.gravityDrain);

    // Pump
    var pump = el('pump');
    if (pump) pump.setAttribute('class', s.pumpRunning ? 'pump-run' : 'equip');

    // Heater
    var heaterOn = (s.StatusWord & (1 << 5)) !== 0;
    var heater = el('heater');
    if (heater) heater.setAttribute('class', heaterOn ? 'heater-on' : 'heater-off');
    txt('caustic-temp', (m.CausticTankTempC || 20).toFixed(1) + ' °C');

    // Heat panel
    txt('h-cmd', heaterOn ? 'On' : 'Off');
    txt('h-temp', (m.CausticTankTempC || 20).toFixed(1) + ' °C');
    // Setpoint is a command; shell doesn't expose it to the read-only view.
    // Derive from status if you later add it; for now show the default.
    txt('h-sp', '45.0 °C');

    // State panel
    var pumpText = (s.StatusWord & (1 << 0))
      ? 'Running'
      : ((m.FlowLpm || 0) > 0.1 ? 'Running (gravity)' : 'Stopped');
    txt('s-pump', pumpText);
    txt('s-action', deriveAction(s, s));
    txt('s-liquid', s.LiquidName || 'Empty');
    txt('s-level', (m.LevelPercent || 0).toFixed(1) + ' %');
    txt('s-route', s.RouteName || 'Closed');

    // Command pills
    var cp = el('command-pills');
    if (cp) {
      cp.innerHTML = '';
      var shown = [];
      if (s.waterInlet) shown.push(['Water', 'cmd']);
      if (s.causticInlet) shown.push(['Caustic', 'cmd']);
      if (s.acidInlet) shown.push(['Acid', 'cmd']);
      if (s.pumpRunning) shown.push(['Pump', 'cmd']);
      if (s.returnFlow) shown.push(['Return', 'cmd']);
      if (s.drainFlow || s.gravityDrain) shown.push(['Drain', 'cmd']);
      if (s.StatusWord & (1 << 5)) shown.push(['Heater', 'cmd']);
      shown.forEach(function (p) { cp.appendChild(badge(p[0], p[1])); });
      if (!shown.length) cp.appendChild(badge('all commands off'));
    }

    // Faults
    var f = el('faults');
    if (f) {
      f.innerHTML = '';
      var active = faultList(s.FaultWord || 0);
      if (!active.length) {
        var ok = document.createElement('div');
        ok.className = 'alarm-none';
        ok.textContent = 'No active faults';
        f.appendChild(ok);
      } else {
        active.forEach(function (item) {
          var row = document.createElement('div');
          row.className = 'alarm-item';
          var bb = document.createElement('span');
          bb.className = 'bit';
          bb.textContent = 'bit ' + item.bit;
          var tt = document.createElement('span');
          tt.className = 'txt';
          tt.textContent = item.label;
          row.appendChild(bb);
          row.appendChild(tt);
          f.appendChild(row);
        });
      }
    }

    // Trends: push at most once per second so the window stays ~90s
    // even though the shell polls at 4 Hz.
    var now = Date.now();
    if (now - lastHistPush >= 1000) {
      lastHistPush = now;
      pushHist(levelHist, m.LevelPercent || 0);
      pushHist(tempHist, m.TemperatureC || 20);
      txt('tr-level-v', (m.LevelPercent || 0).toFixed(1) + ' %');
      txt('tr-temp-v', (m.TemperatureC || 0).toFixed(1) + ' °C');
    }
    drawTrend('tr-level', levelHist, 0, 100, '#1e88e5');
    drawTrend('tr-temp',  tempHist, 10, 90, '#fb8c00');
  };
})();