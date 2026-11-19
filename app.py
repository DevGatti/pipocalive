import os
import time
import secrets
from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'pipocalive_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

rooms_state = {}
sid_to_user = {}
room_passwords = {}  # sala -> hash da senha (nunca é enviado aos clientes)

def member_of(room):
    """True se quem enviou o evento realmente entrou nessa sala."""
    info = sid_to_user.get(request.sid)
    return bool(info) and info['room'] == room and room in rooms_state

MAX_USERS_PER_ROOM = 10  # limite de pessoas por sala, contando com o dono

def room_size(room):
    """Conexões atuais na sala (conta conexões, não apelidos)."""
    return sum(1 for info in sid_to_user.values() if info['room'] == room)

# ---------------------------------------------------------------------------
# Sala vazia: some da memória depois de um tempo de tolerância
# (dá tempo de recarregar a página, trocar de app no celular, a internet cair...)
# ---------------------------------------------------------------------------
EMPTY_ROOM_GRACE = 120  # segundos
empty_since = {}        # sala -> quando ficou vazia

HOST_RECONNECT_GRACE = 60
host_grace_pending = {}

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

    socketio.start_background_task(_cleanup)

def emit_users(room):
    state = rooms_state.get(room)
    if not state:
        return
    emit('update_users', {
        'users': state['users'],
        'host': state['host'],
        'muted': state['muted']
    }, to=room)

def resolve_host_after_grace(room, username, disconnected_sid):
    socketio.sleep(HOST_RECONNECT_GRACE)
    if not host_grace_pending.get(room):
        return
    host_grace_pending.pop(room, None)
    state = rooms_state.get(room)
    if not state or state.get('host') != username:
        return
    if state.get('host_sid') and state.get('host_sid') != disconnected_sid:
        return
    for sid, info in list(sid_to_user.items()):
        if info.get('room') == room and info.get('username') == username:
            state['host_sid'] = sid
            return
    if state.get('phone_active') and room in phone_sessions:
        return
    users = state.get('users') or []
    if users:
        new_host = users[0]
        state['host'] = new_host
        for sid, info in list(sid_to_user.items()):
            if info.get('room') == room and info.get('username') == new_host:
                state['host_sid'] = sid
                break
        emit('chat_message', {
            'user': 'Sistema',
            'msg': f'👑 {new_host} agora é o Dono da Sala!'
        }, to=room)
    else:
        state['host'] = None
        state['host_sid'] = None
    state['screen_active'] = False
    emit('stop_screen_view', to=room)
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
        kwargs['can_publish_sources'] = ['screen_share']
    try:
        grants = lk_api.VideoGrants(**kwargs)
    except TypeError:  # versão do SDK sem can_publish_sources
        kwargs.pop('can_publish_sources', None)
        grants = lk_api.VideoGrants(**kwargs)
    return (lk_api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(identity)
            .with_grants(grants)
            .to_jwt())

@app.route('/')
def index():
    return render_template('index.html')

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
    socketio.emit('phone_screen', {'active': True}, to=room)
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
        socketio.emit('phone_screen', {'active': False}, to=room)
        if room_size(room) == 0:
            mark_empty(room)
    return jsonify(ok=True)

@socketio.on('phone_view_token')
def on_phone_view_token(data):
    """Um espectador que já está na sala pede permissão para assistir ao celular."""
    room = str((data or {}).get('room', ''))
    if not member_of(room) or not rooms_state[room].get('phone_active') or not livekit_ready():
        return
    emit('phone_view_token', {
        'url': LIVEKIT_URL,
        'token': make_lk_token(f'v-{request.sid}', room, False)
    }, to=request.sid)

@socketio.on('join')
def on_join(data):
    username = str(data.get('username', '')).strip()[:30]
    room = str(data.get('room', '')).strip()[:50]
    password = str(data.get('password', ''))[:64]
    if not username or not room:
        return

    # 'create' = criar sala nova | 'join' = entrar em sala que já existe
    mode = 'create' if data.get('mode') == 'create' else 'join'

    if mode == 'create':
        # O nome da sala precisa estar livre
        if room in rooms_state:
            emit('join_error', {'reason': 'room_exists'}, to=request.sid)
            return
        # Cria a sala logo aqui, sem pausa entre a checagem e a criação
        create_room(room, username, request.sid, password)
    else:
        # A sala precisa existir
        if room not in rooms_state:
            emit('join_error', {'reason': 'room_not_found'}, to=request.sid)
            return
        # Sala privada: exige a senha antes de deixar entrar
        if room in room_passwords:
            if not password:
                emit('join_error', {'reason': 'password_required'}, to=request.sid)
                return
            if not check_password_hash(room_passwords[room], password):
                emit('join_error', {'reason': 'wrong_password'}, to=request.sid)
                return
        # Limite de pessoas (só depois da senha, para não revelar nada a quem não tem acesso)
        already_in = sid_to_user.get(request.sid, {}).get('room') == room
        limit = MAX_USERS_PER_ROOM
        if not already_in and room_size(room) >= limit:
            emit('join_error', {'reason': 'room_full', 'limit': limit}, to=request.sid)
            return

    join_room(room)
    empty_since.pop(room, None)  # alguém voltou: cancela a limpeza da sala
    emit('join_ok', {}, to=request.sid)
    
    sid_to_user[request.sid] = {'username': username, 'room': room}
    
    # (a sala já existe aqui: foi criada acima ou já estava criada)
    
    if username not in rooms_state[room]['users']:
        rooms_state[room]['users'].append(username)
    
    if not rooms_state[room].get('host'):
        rooms_state[room]['host'] = username
        rooms_state[room]['host_sid'] = request.sid
    elif rooms_state[room]['host'] == username:
        rooms_state[room]['host_sid'] = request.sid
        host_grace_pending.pop(room, None)

    emit('sync_state', rooms_state[room], to=request.sid)
    emit('update_users', {
        'users': rooms_state[room]['users'],
        'host': rooms_state[room]['host'],
        'muted': rooms_state[room]['muted']
    }, to=room)
    
    if not data.get('rejoin'):
        emit('chat_message', {
            'user': 'Sistema',
            'msg': f'{username} entrou na sessão.'
        }, to=room)

    if rooms_state[room].get('screen_active') and rooms_state[room].get('host_sid') != request.sid:
        emit('request_screen_offer', {'target_sid': request.sid}, to=rooms_state[room]['host_sid'])

@socketio.on('disconnect')
def on_disconnect():
    user_info = sid_to_user.pop(request.sid, None)
    if user_info:
        username = user_info['username']
        room = user_info['room']
        if room in rooms_state and username in rooms_state[room]['users']:
            rooms_state[room]['users'].remove(username)
            
            if rooms_state[room]['host'] == username:
                host_grace_pending[room] = True
                socketio.start_background_task(
                    resolve_host_after_grace, room, username, request.sid
                )

            # Sala vazia: depois de EMPTY_ROOM_GRACE segundos sem ninguém, apaga da memória
            # (estado e senha) e libera o nome. Conta conexões, não apelidos.
            if room_size(room) == 0:
                mark_empty(room)
                return

            emit('update_users', {
                'users': rooms_state[room]['users'],
                'host': rooms_state[room]['host'],
                'muted': rooms_state[room]['muted']
            }, to=room)

@socketio.on('admin_action')
def on_admin_action(data):
    room = data['room']
    action = data['action']
    target = data['target']
    user_info = sid_to_user.get(request.sid)
    
    if room in rooms_state and user_info and user_info['room'] == room and rooms_state[room]['host'] == user_info['username']:
        if action == 'mute':
            if target in rooms_state[room]['muted']:
                rooms_state[room]['muted'].remove(target)
            else:
                rooms_state[room]['muted'].append(target)
        elif action == 'promote':
            rooms_state[room]['host'] = target
            host_grace_pending.pop(room, None)
            for s, info in sid_to_user.items():
                if info['username'] == target and info['room'] == room:
                    rooms_state[room]['host_sid'] = s
                    break
            emit('chat_message', {'user': 'Sistema', 'msg': f'👑 {target} agora é o Dono da Sala!'}, to=room)
        elif action == 'kick':
            target_sid = None
            for s, info in sid_to_user.items():
                if info['username'] == target and info['room'] == room:
                    target_sid = s
                    break
            if target_sid:
                emit('kicked', {'reason': 'Você foi removido pelo Dono da Sala.'}, to=target_sid)
        
        emit('update_users', {
            'users': rooms_state[room]['users'],
            'host': rooms_state[room]['host'],
            'muted': rooms_state[room]['muted']
        }, to=room)

@socketio.on('add_to_playlist')
def on_add_to_playlist(data):
    room = data['room']
    video_id = data['video_id']
    title = data.get('title', 'Vídeo do YouTube')
    
    if member_of(room):
        if not rooms_state[room]['playlist'] and not rooms_state[room]['video_id']:
            rooms_state[room]['video_id'] = video_id
            rooms_state[room]['time'] = 0
            rooms_state[room]['playing'] = True
            rooms_state[room]['playlist'].append({'video_id': video_id, 'title': title})
            emit('load_video', {'video_id': video_id}, to=room)
        else:
            rooms_state[room]['playlist'].append({'video_id': video_id, 'title': title})
        
        emit('update_playlist', {'playlist': rooms_state[room]['playlist']}, to=room)

@socketio.on('next_video')
def on_next_video(data):
    room = data['room']
    user_info = sid_to_user.get(request.sid)
    
    if room in rooms_state and user_info and user_info['room'] == room and rooms_state[room]['host'] == user_info['username']:
        if rooms_state[room]['playlist']:
            rooms_state[room]['playlist'].pop(0)
            if rooms_state[room]['playlist']:
                next_vid = rooms_state[room]['playlist'][0]
                rooms_state[room]['video_id'] = next_vid['video_id']
                rooms_state[room]['time'] = 0
                rooms_state[room]['playing'] = True
                emit('load_video', {'video_id': next_vid['video_id']}, to=room)
            else:
                rooms_state[room]['video_id'] = ''
                emit('load_video', {'video_id': ''}, to=room)
                
            emit('update_playlist', {'playlist': rooms_state[room]['playlist']}, to=room)

@socketio.on('video_action')
def on_video_action(data):
    room = data['room']
    action = data['action']
    time = data['time']
    user_info = sid_to_user.get(request.sid)
    
    if room in rooms_state and user_info and user_info['room'] == room and rooms_state[room]['host'] == user_info['username']:
        rooms_state[room]['time'] = time
        rooms_state[room]['playing'] = (action == 'play')
        emit('sync_action', {'action': action, 'time': time}, to=room, include_self=False)

@socketio.on('send_reaction')
def on_send_reaction(data):
    if not member_of(data['room']):
        return
    emit('show_reaction', {'emoji': data['emoji']}, to=data['room'])

@socketio.on('screen_status')
def on_screen_status(data):
    room = data['room']
    if member_of(room) and rooms_state[room]['host_sid'] == request.sid:
        # Uma transmissão por vez: o celular já está transmitindo
        if data['active'] and rooms_state[room].get('phone_active'):
            emit('screen_blocked', {}, to=request.sid)
            return
        rooms_state[room]['screen_active'] = data['active']
        if not data['active']:
            emit('stop_screen_view', to=room, include_self=False)
        else:
            # Ao iniciar um novo compartilhamento, pede ao dono que envie uma
            # oferta WebRTC para CADA pessoa que já está na sala.
            for sid, info in list(sid_to_user.items()):
                if info['room'] == room and sid != request.sid:
                    emit('request_screen_offer', {'target_sid': sid}, to=request.sid)

@socketio.on('screen_signal')
def on_screen_signal(data):
    room = data['room']
    if not member_of(room):
        return
    data['sender_sid'] = request.sid
    target_sid = data.get('target_sid')
    if target_sid:
        emit('screen_signal', data, to=target_sid)
    else:
        emit('screen_signal', data, to=room, include_self=False)

@socketio.on('send_message')
def on_send_message(data):
    room = data['room']
    if not member_of(room):
        return
    # Usa o apelido registrado no servidor, não o que o navegador mandou
    username = sid_to_user[request.sid]['username']
    if username in rooms_state[room].get('muted', []):
        return
    msg = str(data.get('msg', ''))[:500]
    emit('chat_message', {'user': username, 'msg': msg}, to=room)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    socketio.run(app, host='0.0.0.0', port=port)
