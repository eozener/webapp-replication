from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random
import math
import csv
import os
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'mpg_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")


def roundup(value, precision):
    """Ceiling to nearest multiple of precision, matching z-Tree's roundup()."""
    factor = 1.0 / precision
    return math.ceil(value * factor) / factor


# ── Session registry ──────────────────────────────────────────────────────────
# Multiple independent game sessions can run simultaneously.
# Each session is identified by a short alphanumeric code.

sessions = {}           # code → session dict
sid_to_code = {}        # player/admin sid → session code


def generate_code():
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no O/0/1/I to avoid confusion
    while True:
        code = ''.join(random.choices(chars, k=4))
        if code not in sessions:
            return code


def make_session(code):
    return {
        'code': code,
        'phase': 'lobby',
        'round': 0,
        'session_id': None,
        'round_log': [],
        'params': {
            'kappa': 0.25,
            'lam': 0.10,
            'alpha': 2.0,
            'beta': 1.0,
            'emax': 1.25,
            'cb_target': 2.5,
            'cb_score_max': 20.0,
            'priv_score_max': 10.0,
            'total_rounds': 10,
            'num_private': 4,
        },
        'players': {},
        'groups': {},
        'admin_sid': None,
        'ready_next': set(),
    }


def make_player(sid, name):
    return {
        'sid': sid,
        'name': name,
        'group': None,
        'type': None,
        'total_pay': 0.0,
        'pie': None,
        'score': None,
    }


def make_group():
    return {
        'players': [],
        'cb_sid': None,
        'epsilon': None,
        'pie_submissions': {},
        'pie_avg': None,
        'r': None,
        'y': None,
        'pi_actual': None,
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/admin')
def admin():
    return render_template('admin.html')

# ── Connection lifecycle ──────────────────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    pass


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    code = sid_to_code.pop(sid, None)
    if code is None or code not in sessions:
        return
    s = sessions[code]

    if sid == s['admin_sid']:
        s['admin_sid'] = None
        return

    if sid in s['players']:
        del s['players'][sid]
        s['ready_next'].discard(sid)
        _notify_admin(s)
        if s['phase'] == 'running':
            _check_all_ready(s)

# ── Admin events ──────────────────────────────────────────────────────────────

@socketio.on('create_session')
def on_create_session():
    code = generate_code()
    s = make_session(code)
    s['admin_sid'] = request.sid
    sessions[code] = s
    sid_to_code[request.sid] = code
    emit('session_created', {'code': code, 'params': s['params']})
    print(f"  Session {code} created")


@socketio.on('admin_rejoin')
def on_admin_rejoin(data):
    code = data.get('code', '').upper()
    if code not in sessions:
        emit('error', {'msg': 'Session not found.'})
        return
    s = sessions[code]
    s['admin_sid'] = request.sid
    sid_to_code[request.sid] = code
    emit('admin_state', {
        'code': code,
        'phase': s['phase'],
        'players': _player_list(s),
        'params': s['params'],
    })


@socketio.on('start_game')
def on_start_game(data):
    sid = request.sid
    code = sid_to_code.get(sid)
    if not code or code not in sessions:
        return
    s = sessions[code]
    if sid != s['admin_sid']:
        return

    p = s['params']
    if 'lam' in data:
        p['lam'] = float(data['lam'])
    if 'total_rounds' in data:
        p['total_rounds'] = max(1, int(data['total_rounds']))

    group_size = p['num_private'] + 1
    players = list(s['players'].keys())
    if len(players) < group_size:
        emit('error', {'msg': f'Need at least {group_size} players. Currently {len(players)} connected.'})
        return

    random.shuffle(players)
    s['groups'] = {}
    for i, psid in enumerate(players):
        gid = (i // group_size) + 1
        pos = i % group_size
        if gid not in s['groups']:
            s['groups'][gid] = make_group()
        is_cb = (pos == group_size - 1)
        ptype = 'cb' if is_cb else 'private'
        s['players'][psid]['group'] = gid
        s['players'][psid]['type'] = ptype
        s['groups'][gid]['players'].append(psid)
        if is_cb:
            s['groups'][gid]['cb_sid'] = psid
        socketio.emit('role_assigned', {'type': ptype, 'group': gid}, room=psid)

    s['phase'] = 'running'
    s['round'] = 1
    s['session_id'] = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    s['round_log'] = []
    _start_round(s)

# ── Player events ─────────────────────────────────────────────────────────────

@socketio.on('join')
def on_join(data):
    code = str(data.get('code', '')).upper().strip()
    if code not in sessions:
        emit('error', {'msg': f'Session "{code}" not found. Check the code and try again.'})
        return
    s = sessions[code]
    if s['phase'] != 'lobby':
        emit('error', {'msg': 'That session is already in progress.'})
        return
    sid = request.sid
    name = str(data.get('name', '')).strip()[:30] or 'Player'
    s['players'][sid] = make_player(sid, name)
    sid_to_code[sid] = code
    emit('joined', {'name': name, 'code': code})
    _notify_admin(s)


@socketio.on('submit_pie')
def on_submit_pie(data):
    sid = request.sid
    code = sid_to_code.get(sid)
    if not code or code not in sessions:
        return
    s = sessions[code]
    if sid not in s['players']:
        return
    pl = s['players'][sid]
    if pl['type'] != 'private' or s['phase'] != 'running':
        return
    gid = pl['group']
    g = s['groups'][gid]
    if sid in g['pie_submissions']:
        return

    try:
        pie = float(data['pie'])
        if not (0 <= pie <= 100):
            raise ValueError
        pie = round(pie, 2)
    except (KeyError, ValueError, TypeError):
        emit('error', {'msg': 'Please enter a number between 0 and 100.'})
        return

    g['pie_submissions'][sid] = pie
    pl['pie'] = pie
    emit('submitted', {'pie': pie})

    private_sids = [ps for ps in g['players'] if ps in s['players'] and s['players'][ps]['type'] == 'private']
    remaining = len(private_sids) - len(g['pie_submissions'])
    if remaining > 0:
        socketio.emit('wait', {
            'msg': f'Forecast submitted. Waiting for {remaining} more player(s)...',
            'round': s['round'],
            'total_rounds': s['params']['total_rounds'],
            'total_pay': pl['total_pay'],
        }, room=sid)
        return

    # Divide by fixed numPrivate (4), matching z-Tree's piEAvg = sum(piE) / numPrivate
    avg = round(sum(g['pie_submissions'].values()) / s['params']['num_private'], 4)
    g['pie_avg'] = avg

    p = s['params']
    for psid in private_sids:
        socketio.emit('wait', {
            'msg': 'All forecasts submitted. Waiting for the Central Bank...',
            'round': s['round'],
            'total_rounds': p['total_rounds'],
            'total_pay': s['players'][psid]['total_pay'],
        }, room=psid)

    cb = s['players'][g['cb_sid']]
    socketio.emit('your_turn', {
        'role': 'cb',
        'round': s['round'],
        'total_rounds': p['total_rounds'],
        'total_pay': cb['total_pay'],
        'pie_avg': avg,
        'epsilon': g['epsilon'],
        'kappa': p['kappa'],
        'alpha': p['alpha'],
        'beta': p['beta'],
        'lam': p['lam'],
        'cb_target': p['cb_target'],
    }, room=g['cb_sid'])


@socketio.on('submit_r')
def on_submit_r(data):
    sid = request.sid
    code = sid_to_code.get(sid)
    if not code or code not in sessions:
        return
    s = sessions[code]
    if sid not in s['players']:
        return
    pl = s['players'][sid]
    if pl['type'] != 'cb' or s['phase'] != 'running':
        return

    try:
        r = float(data['r'])
        if not (0 <= r <= 100):
            raise ValueError
        r = round(r, 2)
    except (KeyError, ValueError, TypeError):
        emit('error', {'msg': 'Please enter a number between 0 and 100.'})
        return

    gid = pl['group']
    g = s['groups'][gid]
    p = s['params']

    g['r'] = r
    y = round(p['alpha'] - p['beta'] * r + g['epsilon'], 4)
    pi = round(g['pie_avg'] + p['kappa'] * y, 4)
    g['y'] = y
    g['pi_actual'] = pi

    cb_raw = p['cb_score_max'] - (pi - p['cb_target'])**2 - p['lam'] * y**2
    cb_score = max(roundup(cb_raw, 0.01), 0)
    pl['score'] = cb_score
    pl['total_pay'] = round(pl['total_pay'] + cb_score, 2)

    for psid, pie_val in g['pie_submissions'].items():
        priv_raw = p['priv_score_max'] - (pi - pie_val)**2
        priv_score = max(roundup(priv_raw, 0.01), 0)
        s['players'][psid]['score'] = priv_score
        s['players'][psid]['total_pay'] = round(s['players'][psid]['total_pay'] + priv_score, 2)

    for gsid in [sid for sid in g['players'] if sid in s['players']]:
        gpl = s['players'][gsid]
        s['round_log'].append({
            'session':   s['session_id'],
            'code':      code,
            'group':     gid,
            'player':    gpl['name'],
            'type':      gpl['type'],
            'round':     s['round'],
            'epsilon':   g['epsilon'],
            'pie_avg':   g['pie_avg'],
            'r':         r,
            'y':         y,
            'pi_actual': pi,
            'my_pie':    gpl['pie'] if gpl['type'] == 'private' else '',
            'score':     gpl['score'],
            'total_pay': gpl['total_pay'],
        })

    base = {
        'round': s['round'],
        'total_rounds': p['total_rounds'],
        'epsilon': g['epsilon'],
        'pie_avg': g['pie_avg'],
        'r': r, 'y': y, 'pi_actual': pi,
        'kappa': p['kappa'], 'alpha': p['alpha'], 'beta': p['beta'],
        'cb_target': p['cb_target'], 'cb_score_max': p['cb_score_max'],
        'priv_score_max': p['priv_score_max'], 'lam': p['lam'],
    }
    for gsid in [sid for sid in g['players'] if sid in s['players']]:
        gpl = s['players'][gsid]
        socketio.emit('round_results', {
            **base,
            'my_pie': gpl['pie'],
            'my_score': gpl['score'],
            'my_total': gpl['total_pay'],
            'my_type': gpl['type'],
        }, room=gsid)

    if s['admin_sid']:
        socketio.emit('group_done', {'group': gid, 'round': s['round']}, room=s['admin_sid'])


@socketio.on('ready_next')
def on_ready_next():
    sid = request.sid
    code = sid_to_code.get(sid)
    if not code or code not in sessions:
        return
    s = sessions[code]
    if sid in s['players']:
        s['ready_next'].add(sid)
        _check_all_ready(s)

# ── Game flow helpers ─────────────────────────────────────────────────────────

def _start_round(s):
    p = s['params']
    rnd = s['round']
    s['ready_next'] = set()

    epsilon_rand = random.random()
    shared_epsilon = roundup((epsilon_rand - 0.5) * 2 * p['emax'], 0.01)

    for gid, g in s['groups'].items():
        g['epsilon'] = shared_epsilon
        g['pie_submissions'] = {}
        g['pie_avg'] = None
        g['r'] = g['y'] = g['pi_actual'] = None

        for sid in [pid for pid in g['players'] if pid in s['players']]:
            pl = s['players'][sid]
            pl['pie'] = pl['score'] = None
            if pl['type'] == 'private':
                socketio.emit('your_turn', {
                    'role': 'private',
                    'round': rnd,
                    'total_rounds': p['total_rounds'],
                    'total_pay': pl['total_pay'],
                    'kappa': p['kappa'],
                    'alpha': p['alpha'],
                    'beta': p['beta'],
                }, room=sid)
            else:
                socketio.emit('wait', {
                    'msg': 'Waiting for private sector players to submit their forecasts...',
                    'round': rnd,
                    'total_rounds': p['total_rounds'],
                    'total_pay': pl['total_pay'],
                }, room=sid)

    if s['admin_sid']:
        socketio.emit('round_started', {
            'round': rnd, 'total': p['total_rounds']
        }, room=s['admin_sid'])


def _check_all_ready(s):
    active = set(s['players'].keys())
    if not active:
        return
    all_r_done = all(g['r'] is not None for g in s['groups'].values())
    if all_r_done and s['ready_next'] >= active:
        _advance(s)


def _advance(s):
    p = s['params']
    if s['round'] >= p['total_rounds']:
        s['phase'] = 'finished'
        _write_csv(s)
        for sid, pl in s['players'].items():
            socketio.emit('game_over', {
                'total_pay': pl['total_pay'],
                'type': pl['type'],
            }, room=sid)
        if s['admin_sid']:
            results = sorted(
                [{'name': pl['name'], 'type': pl['type'],
                  'group': pl['group'], 'total': pl['total_pay']}
                 for pl in s['players'].values()],
                key=lambda x: -x['total']
            )
            socketio.emit('final_results', {'players': results}, room=s['admin_sid'])
    else:
        s['round'] += 1
        _start_round(s)


def _write_csv(s):
    if not s['round_log']:
        return
    os.makedirs('results', exist_ok=True)
    path = os.path.join('results', f"session_{s['session_id']}_{s['code']}.csv")
    fields = ['session', 'code', 'group', 'player', 'type', 'round',
              'epsilon', 'pie_avg', 'r', 'y', 'pi_actual',
              'my_pie', 'score', 'total_pay']
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(s['round_log'])
    print(f"  [{s['code']}] Results saved → {path}")


def _notify_admin(s):
    if s['admin_sid']:
        socketio.emit('player_list', _player_list(s), room=s['admin_sid'])


def _player_list(s):
    return [{'name': p['name'], 'type': p['type'], 'group': p['group']}
            for p in s['players'].values()]


if __name__ == '__main__':
    print("\n  Monetary Policy Game")
    print("  ─────────────────────────────────────────")
    print("  Player URL : http://<your-ip>:5001/")
    print("  Admin URL  : http://<your-ip>:5001/admin")
    print("  ─────────────────────────────────────────\n")
    socketio.run(app, host='0.0.0.0', port=5001, debug=False, allow_unsafe_werkzeug=True)
