/* Monetary Policy Game – client */

const socket = io();

// ── Screen management ─────────────────────────────────────────────────────────

function show(id) {
  document.querySelectorAll('.screen').forEach(s => {
    s.classList.remove('active');
    s.style.display = 'none';
  });
  const el = document.getElementById(id);
  if (el) { el.style.display = 'flex'; el.classList.add('active'); }
}

function fmt(n) { return Number(n).toFixed(2); }
function q(id) { return document.getElementById(id); }
function setErr(id, msg) { const e = q(id); e.textContent = msg; e.classList.remove('hidden'); }
function clearErr(id) { q(id).classList.add('hidden'); }

// ── Join ──────────────────────────────────────────────────────────────────────

q('btn-join').addEventListener('click', () => {
  const code = q('session-code').value.trim().toUpperCase();
  const name = q('player-name').value.trim();
  if (!code) { setErr('join-error', 'Please enter the session code.'); return; }
  if (!name) { setErr('join-error', 'Please enter your name.'); return; }
  clearErr('join-error');
  socket.emit('join', { code, name });
});

q('player-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') q('btn-join').click();
});

// ── Private: submit forecast ──────────────────────────────────────────────────

q('btn-pie').addEventListener('click', () => {
  const v = q('input-pie').value;
  if (v === '' || isNaN(parseFloat(v))) { setErr('pie-error', 'Please enter a valid number.'); return; }
  clearErr('pie-error');
  q('btn-pie').disabled = true;
  socket.emit('submit_pie', { pie: parseFloat(v) });
});

q('input-pie').addEventListener('keydown', e => { if (e.key === 'Enter') q('btn-pie').click(); });

// ── CB: submit interest rate ──────────────────────────────────────────────────

q('btn-r').addEventListener('click', () => {
  const v = q('input-r').value;
  if (v === '' || isNaN(parseFloat(v))) { setErr('r-error', 'Please enter a valid number.'); return; }
  clearErr('r-error');
  q('btn-r').disabled = true;
  socket.emit('submit_r', { r: parseFloat(v) });
});

q('input-r').addEventListener('keydown', e => { if (e.key === 'Enter') q('btn-r').click(); });

// ── Continue after results ────────────────────────────────────────────────────

q('btn-continue').addEventListener('click', () => {
  q('btn-continue').disabled = true;
  q('btn-continue').textContent = 'Waiting for others…';
  socket.emit('ready_next');
});

// ── Socket events ─────────────────────────────────────────────────────────────

socket.on('joined', data => {
  q('lobby-name').textContent = `You joined as "${data.name}"`;
  show('screen-lobby');
});

socket.on('role_assigned', data => {
  // Stored server-side; client just waits for your_turn / wait events
});

socket.on('your_turn', data => {
  if (data.role === 'private') {
    q('priv-round').textContent = data.round;
    q('priv-total').textContent = data.total_rounds;
    q('priv-total-pay').textContent = fmt(data.total_pay);
    q('priv-kappa').textContent = data.kappa;
    q('priv-alpha').textContent = data.alpha;
    q('priv-beta').textContent = data.beta;
    q('input-pie').value = '';
    q('btn-pie').disabled = false;
    clearErr('pie-error');
    show('screen-private');
    q('input-pie').focus();
  } else if (data.role === 'cb') {
    q('cb-round').textContent = data.round;
    q('cb-total').textContent = data.total_rounds;
    q('cb-total-pay').textContent = fmt(data.total_pay);
    q('cb-kappa').textContent = data.kappa;
    q('cb-alpha').textContent = data.alpha;
    q('cb-beta').textContent = data.beta;
    q('cb-pie-avg').textContent = fmt(data.pie_avg);
    q('cb-epsilon').textContent = fmt(data.epsilon);
    q('cb-target-hint').textContent = data.cb_target;
    q('cb-lam-hint').textContent = data.lam;
    q('input-r').value = '';
    q('btn-r').disabled = false;
    clearErr('r-error');
    show('screen-cb');
    q('input-r').focus();
  }
});

socket.on('wait', data => {
  const pill = q('wait-round-pill');
  if (data.round) {
    pill.textContent = `Round ${data.round} of ${data.total_rounds}`;
    pill.style.display = 'inline-block';
  } else {
    pill.style.display = 'none';
  }
  q('wait-msg').textContent = data.msg || 'Please wait…';
  q('wait-total').textContent = fmt(data.total_pay || 0);
  show('screen-wait');
});

socket.on('round_results', data => {
  q('res-round').textContent = data.round;
  q('res-epsilon').textContent = fmt(data.epsilon);
  q('res-pie-avg').textContent = fmt(data.pie_avg);
  q('res-r').textContent = fmt(data.r);
  q('res-y').textContent = fmt(data.y);
  q('res-pi').textContent = fmt(data.pi_actual);
  q('res-score').textContent = fmt(data.my_score);
  q('res-total').textContent = fmt(data.my_total);

  // Score formula
  let formula = '';
  if (data.my_type === 'cb') {
    formula =
      `Score = ${data.cb_score_max} − (${fmt(data.pi_actual)} − ${fmt(data.cb_target)})²` +
      ` − ${data.lam} × (${fmt(data.y)})²\n` +
      `      = ${fmt(data.my_score)} pts`;
  } else {
    formula =
      `Your forecast:   ${fmt(data.my_pie)}%\n` +
      `Actual inflation: ${fmt(data.pi_actual)}%\n\n` +
      `Score = ${data.priv_score_max} − (${fmt(data.pi_actual)} − ${fmt(data.my_pie)})²\n` +
      `      = ${fmt(data.my_score)} pts`;
  }
  q('res-formula').textContent = formula;

  const isLast = data.round >= data.total_rounds;
  const btn = q('btn-continue');
  btn.disabled = false;
  btn.textContent = isLast ? 'See Final Results →' : 'Continue →';
  show('screen-results');
});

socket.on('game_over', data => {
  q('final-score').textContent = fmt(data.total_pay);
  q('final-role').textContent = data.type === 'cb' ? 'Central Bank' : 'Private Sector Agent';
  show('screen-gameover');
});

socket.on('error', data => {
  // Surface error on whichever screen is active
  console.error('Server:', data.msg);
  // Re-enable submit buttons in case they were disabled before the error
  ['btn-pie', 'btn-r', 'btn-join'].forEach(id => { q(id) && (q(id).disabled = false); });
});
