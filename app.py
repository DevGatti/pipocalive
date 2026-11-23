import math
import os
import re
import time
import secrets
from datetime import timedelta
from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'pipocalive_secret_key')
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    ping_interval=25,
    ping_timeout=60,
)

rooms_state = {}
sid_to_user = {}
room_passwords = {}  # sala -> hash da senha (nunca é enviado aos clientes)

# Uma queda rápida de internet (ou trocar de app no celular) não pode apagar a
# pessoa da "Galera": só remove depois da tolerância, se ela não reconectar.
PRESENCE_GRACE = 10
LEAVE_GRACE = 3
pending_leaves = {}  # (sala, apelido) -> sid da conexão que caiu

def as_dict(data):
    """Evento malformado não pode derrubar o handler com AttributeError."""
    return data if isinstance(data, dict) else {}

def member_of(room):
    """True se quem enviou o evento realmente entrou nessa sala."""
    info = sid_to_user.get(request.sid)
    return bool(info) and info['room'] == room and room in rooms_state


def is_host(room):
    state = rooms_state.get(room)
    if not member_of(room) or not state:
        return False
    if state.get('host_sid'):
        return state['host_sid'] == request.sid
    return state.get('host') == sid_to_user[request.sid]['username']

MAX_USERS_PER_ROOM = 10  # limite de pessoas por sala, contando com o dono

def connection_count(room):
    """Conexões ativas (inclui o sid antigo que ainda não caiu num reconect)."""
    return sum(1 for info in sid_to_user.values() if info['room'] == room)

def room_size(room):
    return connection_count(room)

def member_count(room):
    """Pessoas distintas: evita 'sala cheia' durante reconexões."""
    return len({info['username'] for info in sid_to_user.values() if info['room'] == room})

def member_sids(room, username):
    return [sid for sid, info in sid_to_user.items()
            if info['room'] == room and info['username'] == username]

def room_sids(room):
    return [sid for sid, info in sid_to_user.items() if info['room'] == room]

# ---------------------------------------------------------------------------
# Sala vazia: some da memória depois de um tempo de tolerância
# (dá tempo de recarregar a página, trocar de app no celular, a internet cair...)
# ---------------------------------------------------------------------------
EMPTY_ROOM_GRACE = 120  # segundos
empty_since = {}        # sala -> quando ficou vazia

HOST_RECONNECT_GRACE = 20
host_grace_pending = {}  # sala -> sid do dono que caiu

def mark_empty(room):
    empty_since[room] = time.time()

    def _cleanup():
        socketio.sleep(EMPTY_ROOM_GRACE)
        since = empty_since.get(room)
        if (room in rooms_state and since is not None
                and room_size(room) == 0 and room not in phone_sessions
                and time.time() - since >= EMPTY_ROOM_GRACE - 1):
            rooms_state.pop(room, None)
            room_passwords.pop(room, None)
            empty_since.pop(room, None)
            host_grace_pending.pop(room, None)
            for key in [k for k in pending_leaves if k[0] == room]:
                pending_leaves.pop(key, None)

    socketio.start_background_task(_cleanup)

def emit_to(target, event, data=None, skip_sid=None):
    """Funciona dentro de eventos e também de background tasks (sem request context)."""
    socketio.emit(event, data, to=target, skip_sid=skip_sid, namespace='/')


def emit_users(room):
    state = rooms_state.get(room)
    if not state:
        return
    emit_to(room, 'update_users', {
        'users': state['users'],
        'host': state['host'],
        'muted': state['muted']
    })


def system_message(room, msg):
    emit_to(room, 'chat_message', {'user': 'Sistema', 'msg': msg})

def resolve_host_after_grace(room, username, disconnected_sid):
    socketio.sleep(HOST_RECONNECT_GRACE)
    if host_grace_pending.get(room) != disconnected_sid:
        return
    host_grace_pending.pop(room, None)
    state = rooms_state.get(room)
    if not state or state.get('host') != username:
        return
    if state.get('host_sid') and state.get('host_sid') != disconnected_sid:
        return
    sids = member_sids(room, username)
    if sids:
        state['host_sid'] = sids[0]
        emit_users(room)
        return
    if state.get('phone_active') and room in phone_sessions:
        return
    # Não promove alguém que também já saiu (a lista ainda pode conter pendências)
    users = [name for name in (state.get('users') or []) if member_sids(room, name)]
    if users:
        new_host = users[0]
        state['host'] = new_host
        state['muted'] = [m for m in state.get('muted', []) if m != new_host]
        new_sids = member_sids(room, new_host)
        state['host_sid'] = new_sids[0] if new_sids else None
        system_message(room, f'👑 {new_host} agora é o Dono da Sala!')
    else:
        state['host'] = None
        state['host_sid'] = None
    emit_users(room)

# ---------------------------------------------------------------------------
# Transmissão de tela pelo celular (app Android -> LiveKit Cloud -> espectadores)
# O LiveKit fica fora do Render: aqui só geramos as permissões de entrada.
# Variáveis de ambiente no Render: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
# ---------------------------------------------------------------------------
try:
    from livekit import api as lk_api
except Exception:
    lk_api = None

LIVEKIT_URL = os.environ.get('LIVEKIT_URL', '')
LIVEKIT_API_KEY = os.environ.get('LIVEKIT_API_KEY', '')
LIVEKIT_API_SECRET = os.environ.get('LIVEKIT_API_SECRET', '')

phone_sessions = {}  # sala -> chave secreta da transmissão do celular (nunca vai aos navegadores)

def livekit_ready():
    return bool(lk_api and LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)

def make_lk_token(identity, room, publish):
    """Permissão de entrada no LiveKit.
    publish=True  -> o celular: só pode enviar a tela.
    publish=False -> espectador: só pode assistir."""
    kwargs = dict(room_join=True, room=f'pipocalive-{room}',
                  can_publish=publish, can_subscribe=not publish, can_publish_data=False)
    if publish:
        kwargs['can_publish_sources'] = ['screen_share', 'microphone']
    try:
        grants = lk_api.VideoGrants(**kwargs)
    except TypeError:  # versão do SDK sem can_publish_sources
        kwargs.pop('can_publish_sources', None)
        grants = lk_api.VideoGrants(**kwargs)
    return (lk_api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(identity)
            .with_grants(grants)
            .with_ttl(timedelta(hours=6))
            .to_jwt())

@app.route('/')
def index():
    is_app = 'PipocaLiveApp' in request.headers.get('User-Agent', '')
    return render_template('index.html', is_app=is_app)

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "PipocaLive — Cinema Sincronizado",
        "short_name": "PipocaLive",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f0f13",
        "theme_color": "#a855f7",
        "icons": [
            {
                "src": "https://cdn-icons-png.flaticon.com/512/3163/3163478.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    })

@app.route('/sw.js')
def service_worker():
    js = "self.addEventListener('fetch', function(event) {});"
    return Response(js, mimetype='application/javascript')

def create_room(room, username, host_sid, password):
    """Cria a sala (usada pelo site e pelo app). Quem cria é o dono e define a senha (opcional)."""
    host_grace_pending.pop(room, None)
    for key in [k for k in pending_leaves if k[0] == room]:
        pending_leaves.pop(key, None)
    empty_since.pop(room, None)
    rooms_state[room] = {
        'type': 'youtube',
        'video_id': '',
        'time': 0,
        'playing': True,
        'playlist': [],
        'users': [],
        'muted': [],
        'host': username,
        'host_sid': host_sid,
        'screen_active': False,
        'phone_active': False,
        'private': bool(password)
    }
    if password:
        room_passwords[room] = generate_password_hash(password)
    else:
        room_passwords.pop(room, None)  # sala nova sem senha não herda a senha antiga

def _json_fields():
    data = request.get_json(silent=True) or {}
    return (str(data.get('username', '')).strip()[:30],
            str(data.get('room', '')).strip()[:50],
            str(data.get('password', ''))[:64],
            str(data.get('key', ''))[:100])

@app.route('/api/phone/start', methods=['POST'])
def phone_start():
    """O app do celular pede permissão para transmitir. Só o dono da sala consegue.
    Com create=true o app cria a sala nova (e vira o dono) em vez de entrar numa existente."""
    if not livekit_ready():
        return jsonify(error='livekit_not_configured'), 503
    username, room, password, _ = _json_fields()
    create = bool((request.get_json(silent=True) or {}).get('create'))
    if not username or not room:
        return jsonify(error='room_not_found'), 404
    if create:
        if room in rooms_state:
            return jsonify(error='room_exists'), 409
        # Ainda sem ninguém conectado pelo site: a sala é apagada depois do prazo de
        # tolerância, a não ser que a transmissão comece (abaixo) ou alguém entre nela.
        create_room(room, username, None, password)
        mark_empty(room)
    state = rooms_state.get(room)
    if not state:
        return jsonify(error='room_not_found'), 404
    if room in room_passwords:
        if not password:
            return jsonify(error='password_required'), 403
        if not check_password_hash(room_passwords[room], password):
            return jsonify(error='wrong_password'), 403
    # Dono da sala. Se a sala está vazia (o dono trocou do navegador para o app e a conexão
    # do site caiu), ainda dentro do prazo de tolerância, vale o apelido informado.
    host = state.get('host')
    if not (host == username or (host is None and room_size(room) == 0)):
        return jsonify(error='not_host'), 403
    if state.get('screen_active'):
        return jsonify(error='screen_busy'), 409

    key = secrets.token_urlsafe(24)
    phone_sessions[room] = key
    state['phone_active'] = True
    emit_to(room, 'phone_screen', {'active': True})
    return jsonify(url=LIVEKIT_URL, token=make_lk_token(f'phone-{room}', room, True), key=key)

@app.route('/api/phone/stop', methods=['POST'])
def phone_stop():
    """O app avisa que parou de transmitir."""
    _, room, _, key = _json_fields()
    saved = phone_sessions.get(room)
    if not saved or not key or not secrets.compare_digest(saved, key):
        return jsonify(error='invalid_key'), 403
    phone_sessions.pop(room, None)
    if room in rooms_state:
        rooms_state[room]['phone_active'] = False
        emit_to(room, 'phone_screen', {'active': False})
        if room_size(room) == 0:
            mark_empty(room)
    return jsonify(ok=True)

@socketio.on('phone_view_token')
def on_phone_view_token(data):
    """Um espectador que já está na sala pede permissão para assistir ao celular."""
    room = str(as_dict(data).get('room', ''))
    if not member_of(room) or not rooms_state[room].get('phone_active') or not livekit_ready():
        return
    emit_to(request.sid, 'phone_view_token', {
        'url': LIVEKIT_URL,
        'token': make_lk_token(f'v-{request.sid}', room, False)
    })

@socketio.on('join')
def on_join(data):
    data = as_dict(data)
    username = str(data.get('username', '')).strip()[:30]
    room = str(data.get('room', '')).strip()[:50]
    password = str(data.get('password', ''))[:64]
    if not username or not room:
        return

    sid = request.sid
    previous = sid_to_user.get(sid)
    # Reconectou dentro da tolerância: não é uma chegada nova e não pode gerar
    # o par "saiu/entrou" no chat nem piscar a lista da Galera.
    rejoining = bool(data.get('rejoin')) or (room, username) in pending_leaves

    if previous and previous['room'] != room:
        leave_room(previous['room'])
        remove_user_from_room(sid)

    # 'create' = criar sala nova | 'join' = entrar em sala que já existe
    mode = 'create' if data.get('mode') == 'create' else 'join'

    if mode == 'create':
        # O nome da sala precisa estar livre
        if room in rooms_state:
            emit_to(sid, 'join_error', {'reason': 'room_exists'})
            return
        # Cria a sala logo aqui, sem pausa entre a checagem e a criação
        create_room(room, username, sid, password)
    else:
        # A sala precisa existir
        if room not in rooms_state:
            emit_to(sid, 'join_error', {'reason': 'room_not_found'})
            return
        # Sala privada: exige a senha antes de deixar entrar
        if room in room_passwords:
            if not password:
                emit_to(sid, 'join_error', {'reason': 'password_required'})
                return
            if not check_password_hash(room_passwords[room], password):
                emit_to(sid, 'join_error', {'reason': 'wrong_password'})
                return
        # Limite de pessoas (só depois da senha, para não revelar nada a quem não tem acesso).
        # Conta apelidos distintos: o sid antigo pode ainda estar caindo no reconect.
        already_in = bool(previous and previous['room'] == room)
        if not already_in and member_count(room) >= MAX_USERS_PER_ROOM:
            emit_to(sid, 'join_error', {'reason': 'room_full', 'limit': MAX_USERS_PER_ROOM})
            return

    pending_leaves.pop((room, username), None)  # cancela a remoção agendada
    join_room(room)
    empty_since.pop(room, None)  # alguém voltou: cancela a limpeza da sala
    sid_to_user[sid] = {'username': username, 'room': room}
    state = rooms_state[room]

    if username not in state['users']:
        state['users'].append(username)

    if not state.get('host'):
        state['host'] = username
        state['host_sid'] = sid
        host_grace_pending.pop(room, None)
    elif state['host'] == username:
        state['host_sid'] = sid
        host_grace_pending.pop(room, None)

    emit_to(sid, 'join_ok', {})
    emit_to(sid, 'sync_state', state)
    emit_users(room)

    if not rejoining:
        system_message(room, f'{username} entrou na sessão.')

    host_sid = state.get('host_sid')
    if state.get('screen_active') and host_sid and host_sid != sid:
        emit_to(host_sid, 'request_screen_offer', {'target_sid': sid})

def finalize_leave(room, username, sid, delay):
    """Remove a pessoa só depois da tolerância, se ela realmente não voltou."""
    socketio.sleep(delay)
    if pending_leaves.get((room, username)) != sid:
        return
    pending_leaves.pop((room, username), None)
    if member_sids(room, username):
        return
    state = rooms_state.get(room)
    if not state:
        return

    if username in state['users']:
        state['users'].remove(username)
        system_message(room, f'{username} saiu da sessão.')

    if state.get('host') == username:
        state['host_sid'] = None
        host_grace_pending[room] = sid
        socketio.start_background_task(resolve_host_after_grace, room, username, sid)

    emit_users(room)

    # Sala vazia: depois de EMPTY_ROOM_GRACE segundos sem ninguém, apaga da memória
    # (estado e senha) e libera o nome.
    if connection_count(room) == 0:
        mark_empty(room)


def remove_user_from_room(sid, explicit=False):
    user_info = sid_to_user.pop(sid, None)
    if not user_info:
        return

    username = user_info['username']
    room = user_info['room']
    if room not in rooms_state:
        return
    # Outra aba/conexão da mesma pessoa ainda está ligada: nada muda na Galera.
    if member_sids(room, username):
        return
    key = (room, username)
    if key in pending_leaves:
        return
    pending_leaves[key] = sid
    socketio.start_background_task(
        finalize_leave, room, username, sid,
        LEAVE_GRACE if explicit else PRESENCE_GRACE
    )


@socketio.on('disconnect')
def on_disconnect():
    remove_user_from_room(request.sid)


@socketio.on('leave')
def on_leave():
    info = sid_to_user.get(request.sid)
    if info:
        leave_room(info['room'])
    remove_user_from_room(request.sid, explicit=True)

@socketio.on('admin_action')
def on_admin_action(data):
    data = as_dict(data)
    room = str(data.get('room', ''))
    action = data.get('action')
    target = str(data.get('target', '')).strip()[:30]
    if not target or not is_host(room):
        return
    state = rooms_state[room]

    if target not in state['users']:
        return

    if action == 'mute':
        muted = state.setdefault('muted', [])
        if target in muted:
            muted.remove(target)
        else:
            muted.append(target)
    elif action == 'promote':
        if target == state.get('host'):
            return
        new_sids = member_sids(room, target)
        if not new_sids:
            return
        state['host'] = target
        state['host_sid'] = new_sids[0]
        host_grace_pending.pop(room, None)
        state['muted'] = [m for m in state.get('muted', []) if m != target]
        system_message(room, f'👑 {target} agora é o Dono da Sala!')
    elif action == 'kick':
        if target == state.get('host'):
            return
        for sid in member_sids(room, target):
            emit_to(sid, 'kicked', {'reason': 'Você foi removido pelo Dono da Sala.'})
            remove_user_from_room(sid, explicit=True)
            leave_room(room, sid=sid)
    else:
        return

    emit_users(room)

@socketio.on('add_to_playlist')
def on_add_to_playlist(data):
    data = as_dict(data)
    room = str(data.get('room', ''))
    video_id = str(data.get('video_id', ''))
    if not member_of(room) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        return
    title = str(data.get('title', '')).strip()[:80] or f'YouTube ({video_id})'
    state = rooms_state[room]

    first = not state['playlist'] and not state['video_id']
    state['playlist'].append({'video_id': video_id, 'title': title})
    if first:
        state['video_id'] = video_id
        state['time'] = 0
        state['playing'] = True
        emit_to(room, 'load_video', {'video_id': video_id})

    emit_to(room, 'update_playlist', {'playlist': state['playlist']})

@socketio.on('next_video')
def on_next_video(data):
    room = str(as_dict(data).get('room', ''))
    if not is_host(room):
        return
    state = rooms_state[room]
    if not state['playlist']:
        return

    state['playlist'].pop(0)
    if state['playlist']:
        next_vid = state['playlist'][0]
        state['video_id'] = next_vid['video_id']
        state['time'] = 0
        state['playing'] = True
        emit_to(room, 'load_video', {'video_id': next_vid['video_id']})
    else:
        state['video_id'] = ''
        emit_to(room, 'load_video', {'video_id': ''})

    emit_to(room, 'update_playlist', {'playlist': state['playlist']})

@socketio.on('video_action')
def on_video_action(data):
    data = as_dict(data)
    room = str(data.get('room', ''))
    action = data.get('action')
    if action not in ('play', 'pause') or not is_host(room):
        return
    try:
        position = float(data.get('time', 0))
    except (TypeError, ValueError):
        return
    if not math.isfinite(position) or position < 0:
        return

    rooms_state[room]['time'] = position
    rooms_state[room]['playing'] = (action == 'play')
    emit_to(room, 'sync_action', {'action': action, 'time': position}, skip_sid=request.sid)

@socketio.on('send_reaction')
def on_send_reaction(data):
    data = as_dict(data)
    room = str(data.get('room', ''))
    emoji = str(data.get('emoji', ''))[:24]
    if not emoji or not member_of(room):
        return
    emit_to(room, 'show_reaction', {'emoji': emoji})

@socketio.on('screen_status')
def on_screen_status(data):
    data = as_dict(data)
    room = str(data.get('room', ''))
    active = bool(data.get('active'))
    state = rooms_state.get(room) if member_of(room) else None
    if not state or state.get('host_sid') != request.sid:
        return
    # Uma transmissão por vez: o celular já está transmitindo
    if active and state.get('phone_active'):
        emit_to(request.sid, 'screen_blocked', {})
        return
    state['screen_active'] = active
    if not active:
        emit_to(room, 'stop_screen_view', skip_sid=request.sid)
    else:
        # Ao iniciar um novo compartilhamento, pede ao dono que envie uma
        # oferta WebRTC para CADA pessoa que já está na sala.
        for sid in room_sids(room):
            if sid != request.sid:
                emit_to(request.sid, 'request_screen_offer', {'target_sid': sid})

@socketio.on('screen_signal')
def on_screen_signal(data):
    data = as_dict(data)
    room = str(data.get('room', ''))
    if not member_of(room):
        return
    signal_type = data.get('type')
    if signal_type not in ('offer', 'answer', 'candidate'):
        return
    target_sid = data.get('target_sid')
    if target_sid:
        # Nunca encaminha sinal para outra sala
        if target_sid not in sid_to_user or sid_to_user[target_sid]['room'] != room:
            return

    forward = {'type': signal_type, 'sender_sid': request.sid, 'target_sid': target_sid}
    if signal_type == 'candidate':
        candidate = data.get('candidate')
        if not isinstance(candidate, dict):
            return
        forward['candidate'] = {k: candidate[k] for k in
                                ('candidate', 'sdpMid', 'sdpMLineIndex', 'usernameFragment')
                                if k in candidate}
    else:
        sdp = data.get('sdp')
        if not isinstance(sdp, dict):
            return
        forward['sdp'] = {'type': sdp.get('type'), 'sdp': str(sdp.get('sdp', ''))[:200000]}

    if target_sid:
        emit_to(target_sid, 'screen_signal', forward)
    else:
        emit_to(room, 'screen_signal', forward, skip_sid=request.sid)

@socketio.on('send_message')
def on_send_message(data):
    data = as_dict(data)
    room = str(data.get('room', ''))
    if not member_of(room):
        return
    # Usa o apelido registrado no servidor, não o que o navegador mandou
    username = sid_to_user[request.sid]['username']
    if username in rooms_state[room].get('muted', []):
        return
    msg = str(data.get('msg', '')).strip()[:500]
    if not msg:
        return
    emit_to(room, 'chat_message', {'user': username, 'msg': msg})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    socketio.run(app, host='0.0.0.0', port=port)
