/* Blinker dashboard renderer. Called by the shell on every poll.
 *
 * Reads measurements.On, measurements.Phase, status.State, and
 * status.BlinkCount. Nothing else. No physics, no guessing.
 */
window.renderState = function (state) {
  'use strict';

  var m = state.measurements || {};
  var s = state.status || {};

  var stateEl = document.getElementById('s-state');
  if (stateEl) stateEl.textContent = s.State != null ? s.State : '--';

  var onEl = document.getElementById('s-on');
  if (onEl) onEl.textContent = m.On ? 'yes' : 'no';

  var phaseEl = document.getElementById('s-phase');
  if (phaseEl) {
    phaseEl.textContent =
      (typeof m.Phase === 'number' ? m.Phase.toFixed(2) : '0.00') + ' s';
  }

  var countEl = document.getElementById('s-count');
  if (countEl) countEl.textContent = s.BlinkCount != null ? s.BlinkCount : 0;

  var light = document.getElementById('light');
  if (light) {
    light.style.background = m.On ? '#f5c518' : '#cfd8dc';
  }
};