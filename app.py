import os
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
        rooms_state[room] = {
            'type': 'youtube',
            'video_id': '',
            'time': 0,
            'playing': True,
            'playlist': [],
            'users': [],
            'muted': [],
            'host': username,
            'host_sid': request.sid,
            'screen_active': False,
            'private': bool(password)
        }
        # Quem cria a sala define a senha (opcional)
        if password:
            room_passwords[room] = generate_password_hash(password)
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

    join_room(room)
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

    emit('sync_state', rooms_state[room], to=request.sid)
    emit('update_users', {
        'users': rooms_state[room]['users'],
        'host': rooms_state[room]['host'],
        'muted': rooms_state[room]['muted']
    }, to=room)
    
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
                rooms_state[room]['screen_active'] = False
                emit('stop_screen_view', to=room)
                if rooms_state[room]['users']:
                    new_host = rooms_state[room]['users'][0]
                    rooms_state[room]['host'] = new_host
                    for s, info in sid_to_user.items():
                        if info['username'] == new_host and info['room'] == room:
                            rooms_state[room]['host_sid'] = s
                            break
                    emit('chat_message', {
                        'user': 'Sistema',
                        'msg': f'👑 {new_host} agora é o Dono da Sala!'
                    }, to=room)
                else:
                    rooms_state[room]['host'] = None
                    rooms_state[room]['host_sid'] = None

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
