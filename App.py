import os
import bcrypt
from flask import Flask, render_template, request, send_from_directory, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from dotenv import load_dotenv
from urllib.parse import urlparse
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.errors import UniqueViolation
from werkzeug.utils import secure_filename
import base64
from datetime import datetime, timedelta
import re
import hashlib
import secrets

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*", ping_timeout=60, ping_interval=25)

# === PostgreSQL подключение ===
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    parsed = urlparse(DATABASE_URL)
    DB_HOST = parsed.hostname
    DB_PORT = parsed.port
    DB_NAME = parsed.path[1:]
    DB_USER = parsed.username
    DB_PASS = parsed.password
else:
    DB_HOST = 'localhost'
    DB_PORT = '5432'
    DB_NAME = 'messka'
    DB_USER = 'messka_user'
    DB_PASS = 'messka'

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS,
        cursor_factory=RealDictCursor
    )

# === Функции паролей ===
def hash_password(password):
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def check_password(password, hashed):
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

# === Вспомогательные функции ===
def validate_tag(tag):
    if not tag:
        return False
    return bool(re.match(r'^[a-z0-9_]{3,20}$', tag))

def generate_session_token():
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()

def generate_chat_id(user1_id, user2_id):
    ids = sorted([user1_id, user2_id])
    return hashlib.sha256(f"{ids[0]}-{ids[1]}".encode()).hexdigest()[:16]

def get_user_by_id(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, user_tag, avatar FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def update_last_seen(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_seen = CURRENT_TIMESTAMP WHERE id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()

# === РАБОТА С ЛИЧНЫМИ ЧАТАМИ ===
def get_or_create_private_chat(user1_id, user2_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT chat_id FROM private_chats 
        WHERE (user1_id = %s AND user2_id = %s) OR (user1_id = %s AND user2_id = %s)
    """, (user1_id, user2_id, user2_id, user1_id))
    
    existing = cur.fetchone()
    if existing:
        cur.close()
        conn.close()
        return existing['chat_id']
    
    chat_id = generate_chat_id(user1_id, user2_id)
    try:
        cur.execute("""
            INSERT INTO private_chats (chat_id, user1_id, user2_id)
            VALUES (%s, %s, %s)
            RETURNING chat_id
        """, (chat_id, user1_id, user2_id))
        new_chat = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return new_chat['chat_id']
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        print(f"❌ Error creating private chat: {e}")
        return None

def save_private_message(chat_id, sender_id, receiver_id, msg_type, text=None, filename=None, filepath=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO private_messages (chat_id, sender_id, receiver_id, message_text, filename, filepath, message_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id, created_at
    ''', (chat_id, sender_id, receiver_id, text, filename, filepath, msg_type))
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return result

def get_private_chat_history(chat_id, user_id, limit=100):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT 
            pm.*, 
            u_sender.username as sender_name, 
            u_sender.user_tag as sender_tag, 
            u_sender.avatar as sender_avatar,
            u_receiver.username as receiver_name,
            CASE WHEN f.id IS NOT NULL THEN true ELSE false END as is_favorite
        FROM private_messages pm
        JOIN users u_sender ON pm.sender_id = u_sender.id
        JOIN users u_receiver ON pm.receiver_id = u_receiver.id
        LEFT JOIN favorites f ON pm.id = f.private_message_id AND f.user_id = %s
        WHERE pm.chat_id = %s
        ORDER BY pm.created_at ASC
        LIMIT %s
    ''', (user_id, chat_id, limit))
    messages = cur.fetchall()
    cur.close()
    conn.close()
    
    result = []
    for msg in messages:
        result.append({
            'id': msg['id'],
            'chat_id': msg['chat_id'],
            'sender_id': msg['sender_id'],
            'sender_name': msg['sender_name'],
            'sender_tag': msg['sender_tag'],
            'sender_avatar': msg['sender_avatar'],
            'text': msg['message_text'],
            'filename': msg['filename'],
            'filepath': msg['filepath'],
            'type': msg['message_type'],
            'is_read': msg['is_read'],
            'is_favorite': msg['is_favorite'],
            'time': msg['created_at'].strftime('%H:%M') if msg['created_at'] else ''
        })
    
    return result

def get_user_chats(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT pc.chat_id, 
               CASE 
                   WHEN pc.user1_id = %s THEN u2.username
                   ELSE u1.username
               END as other_username,
               CASE 
                   WHEN pc.user1_id = %s THEN u2.user_tag
                   ELSE u1.user_tag
               END as other_tag,
               CASE 
                   WHEN pc.user1_id = %s THEN u2.avatar
                   ELSE u1.avatar
               END as other_avatar,
               (SELECT message_text FROM private_messages 
                WHERE chat_id = pc.chat_id 
                ORDER BY created_at DESC LIMIT 1) as last_message,
               (SELECT created_at FROM private_messages 
                WHERE chat_id = pc.chat_id 
                ORDER BY created_at DESC LIMIT 1) as last_message_time,
               (SELECT COUNT(*) FROM private_messages 
                WHERE chat_id = pc.chat_id AND receiver_id = %s AND is_read = FALSE) as unread_count
        FROM private_chats pc
        JOIN users u1 ON pc.user1_id = u1.id
        JOIN users u2 ON pc.user2_id = u2.id
        WHERE pc.user1_id = %s OR pc.user2_id = %s
        ORDER BY last_message_time DESC NULLS LAST
    """, (user_id, user_id, user_id, user_id, user_id, user_id))
    chats = cur.fetchall()
    cur.close()
    conn.close()
    return chats

def mark_messages_as_read(chat_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE private_messages 
        SET is_read = TRUE 
        WHERE chat_id = %s AND receiver_id = %s AND is_read = FALSE
    """, (chat_id, user_id))
    updated = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return updated

def get_chat_info(chat_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            CASE 
                WHEN pc.user1_id = %s THEN u2.username
                ELSE u1.username
            END as username,
            CASE 
                WHEN pc.user1_id = %s THEN u2.user_tag
                ELSE u1.user_tag
            END as tag,
            CASE 
                WHEN pc.user1_id = %s THEN u2.avatar
                ELSE u1.avatar
            END as avatar
        FROM private_chats pc
        JOIN users u1 ON pc.user1_id = u1.id
        JOIN users u2 ON pc.user2_id = u2.id
        WHERE pc.chat_id = %s
    """, (user_id, user_id, user_id, chat_id))
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result

# === РАБОТА С ИЗБРАННЫМ ===
def add_to_favorites(user_id, message_id=None, private_message_id=None, chat_id=None):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if message_id:
            cur.execute("""
                INSERT INTO favorites (user_id, message_id, chat_id)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (user_id, message_id, chat_id))
        elif private_message_id:
            cur.execute("""
                INSERT INTO favorites (user_id, private_message_id, chat_id)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (user_id, private_message_id, chat_id))
        else:
            return {'success': False, 'error': 'no_message_id'}
        
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return {'success': True, 'favorite_id': result['id']}
    except UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return {'success': False, 'error': 'already_favorite'}
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {'success': False, 'error': str(e)}

def remove_from_favorites(user_id, message_id=None, private_message_id=None):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if message_id:
            cur.execute("DELETE FROM favorites WHERE user_id = %s AND message_id = %s", (user_id, message_id))
        elif private_message_id:
            cur.execute("DELETE FROM favorites WHERE user_id = %s AND private_message_id = %s", (user_id, private_message_id))
        else:
            cur.close()
            conn.close()
            return False
        
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return deleted > 0
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        print(f"Error removing favorite: {e}")
        return False

def get_favorites(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT 
            'general' as chat_type,
            f.id as favorite_id,
            f.added_at,
            m.id as message_id,
            m.username as sender_name,
            m.user_tag as sender_tag,
            m.avatar as sender_avatar,
            m.message_text,
            m.filename,
            m.filepath,
            m.message_type,
            m.created_at
        FROM favorites f
        JOIN messages m ON f.message_id = m.id
        WHERE f.user_id = %s AND f.message_id IS NOT NULL
        ORDER BY f.added_at DESC
    """, (user_id,))
    general_favs = cur.fetchall()
    
    cur.execute("""
        SELECT 
            'private' as chat_type,
            f.id as favorite_id,
            f.added_at,
            f.chat_id,
            pm.id as message_id,
            u_sender.username as sender_name,
            u_sender.user_tag as sender_tag,
            u_sender.avatar as sender_avatar,
            pm.message_text,
            pm.filename,
            pm.filepath,
            pm.message_type,
            pm.created_at,
            CASE 
                WHEN pc.user1_id = %s THEN u2.username
                ELSE u1.username
            END as chat_with_name,
            CASE 
                WHEN pc.user1_id = %s THEN u2.user_tag
                ELSE u1.user_tag
            END as chat_with_tag
        FROM favorites f
        JOIN private_messages pm ON f.private_message_id = pm.id
        JOIN private_chats pc ON f.chat_id = pc.chat_id
        JOIN users u_sender ON pm.sender_id = u_sender.id
        JOIN users u1 ON pc.user1_id = u1.id
        JOIN users u2 ON pc.user2_id = u2.id
        WHERE f.user_id = %s AND f.private_message_id IS NOT NULL
        ORDER BY f.added_at DESC
    """, (user_id, user_id, user_id))
    private_favs = cur.fetchall()
    
    cur.close()
    conn.close()
    
    all_favorites = []
    
    for fav in general_favs:
        all_favorites.append({
            'id': fav['message_id'],
            'chat_id': 'general',
            'chat_name': 'Общий чат',
            'sender_name': fav['sender_name'],
            'sender_tag': fav['sender_tag'],
            'sender_avatar': fav['sender_avatar'],
            'text': fav['message_text'],
            'filename': fav['filename'],
            'filepath': fav['filepath'],
            'type': fav['message_type'],
            'added_at': fav['added_at'].strftime('%d.%m.%Y %H:%M') if fav['added_at'] else ''
        })
    
    for fav in private_favs:
        all_favorites.append({
            'id': fav['message_id'],
            'chat_id': fav['chat_id'],
            'chat_name': fav['chat_with_name'],
            'chat_tag': fav['chat_with_tag'],
            'sender_name': fav['sender_name'],
            'sender_tag': fav['sender_tag'],
            'sender_avatar': fav['sender_avatar'],
            'text': fav['message_text'],
            'filename': fav['filename'],
            'filepath': fav['filepath'],
            'type': fav['message_type'],
            'added_at': fav['added_at'].strftime('%d.%m.%Y %H:%M') if fav['added_at'] else ''
        })
    
    return all_favorites

# === РАБОТА С ОБЩИМ ЧАТОМ ===
def save_message(user_id, username, user_tag, avatar, msg_type, text=None, filename=None, filepath=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO messages (user_id, username, user_tag, avatar, message_text, filename, filepath, message_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (user_id, username, user_tag, avatar, text, filename, filepath, msg_type))
    msg_id = cur.fetchone()['id']
    conn.commit()
    cur.close()
    conn.close()
    return msg_id

def get_message_history(user_id=None, limit=100):
    conn = get_db_connection()
    cur = conn.cursor()
    
    if user_id:
        cur.execute('''
            SELECT 
                m.id, m.username, m.user_tag, m.avatar, m.message_text, 
                m.filename, m.filepath, m.message_type,
                TO_CHAR(m.created_at, 'HH24:MI') as formatted_time,
                CASE WHEN f.id IS NOT NULL THEN true ELSE false END as is_favorite
            FROM messages m
            LEFT JOIN favorites f ON m.id = f.message_id AND f.user_id = %s
            ORDER BY m.created_at ASC
            LIMIT %s
        ''', (user_id, limit))
    else:
        cur.execute('''
            SELECT id, username, user_tag, avatar, message_text, filename, filepath, message_type,
                   TO_CHAR(created_at, 'HH24:MI') as formatted_time,
                   false as is_favorite
            FROM messages 
            ORDER BY created_at ASC
            LIMIT %s
        ''', (limit,))
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    return [{
        'id': r['id'],
        'user': r['username'],
        'tag': r['user_tag'],
        'avatar': r['avatar'],
        'text': r['message_text'],
        'filename': r['filename'],
        'filepath': r['filepath'],
        'type': r['message_type'],
        'time': r['formatted_time'],
        'is_favorite': r['is_favorite']
    } for r in rows]

# === СОКЕТЫ ===
users = {}  # {socket_id: {'id': user_id, 'username': str, 'tag': str, 'avatar': str}}
rooms = {}  # {chat_id: [socket_id1, socket_id2]}
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat')
def chat_page():
    return render_template('chat.html')

@socketio.on('connect')
def handle_connect():
    print(f'✅ Client connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    user_data = users.pop(request.sid, None)
    if user_data:
        for room in list(rooms.keys()):
            if request.sid in rooms[room]:
                rooms[room].remove(request.sid)
                if len(rooms[room]) == 0:
                    del rooms[room]
        
        emit('user_left', user_data['username'], broadcast=True)
        print(f'👋 User left: {user_data["username"]}')

@socketio.on('register')
def handle_register(data):
    username = data.get('username')
    user_tag = data.get('tag')
    password = data.get('password')
    avatar = data.get('avatar', 'default')
    
    if not username or not user_tag or not password:
        emit('register_error', {'error': 'Заполните все поля'})
        return
    
    if not validate_tag(user_tag):
        emit('register_error', {'error': 'Некорректный юзернейм (только латиница, цифры, _, 3-20 символов)'})
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = %s OR user_tag = %s", (username, user_tag))
    if cur.fetchone():
        cur.close()
        conn.close()
        emit('register_error', {'error': 'Пользователь уже существует'})
        return
    
    password_hash = hash_password(password)
    cur.execute("""
        INSERT INTO users (username, user_tag, password_hash, avatar)
        VALUES (%s, %s, %s, %s)
        RETURNING id, username, user_tag, avatar
    """, (username, user_tag, password_hash, avatar))
    user = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    
    emit('register_success', {'user': user})

@socketio.on('login')
def handle_login(data):
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        emit('login_error', {'error': 'Заполните все поля'})
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, user_tag, avatar, password_hash FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    
    if not user or not check_password(password, user['password_hash']):
        emit('login_error', {'error': 'Неверное имя или пароль'})
        return
    
    emit('login_success', {'user': {
        'id': user['id'],
        'username': user['username'],
        'tag': user['user_tag'],
        'avatar': user['avatar']
    }})

@socketio.on('set_user')
def handle_set_user(data):
    user_id = data.get('user_id')
    if not user_id:
        return
    
    user = get_user_by_id(user_id)
    if user:
        users[request.sid] = {
            'id': user['id'],
            'username': user['username'],
            'tag': user['user_tag'],
            'avatar': user['avatar']
        }
        
        emit('user_joined', {
            'username': user['username'],
            'tag': user['user_tag'],
            'avatar': user['avatar']
        }, broadcast=True)
        
        emit('message_history', get_message_history(user['id']))
        socketio.emit('private_chats_list', get_user_chats(user['id']))

@socketio.on('get_private_chats')
def handle_get_private_chats():
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    chats = get_user_chats(user_data['id'])
    emit('private_chats_list', [{
        'chat_id': chat['chat_id'],
        'username': chat['other_username'],
        'tag': chat['other_tag'],
        'avatar': chat['other_avatar'],
        'last_message': chat['last_message'],
        'last_message_time': chat['last_message_time'].strftime('%H:%M') if chat['last_message_time'] else '',
        'unread_count': chat['unread_count']
    } for chat in chats])

@socketio.on('start_private_chat')
def handle_start_private_chat(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    target_tag = data.get('tag', '').lower().strip()
    if not target_tag:
        emit('private_chat_error', {'error': 'empty_tag'})
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, user_tag, avatar FROM users WHERE user_tag = %s", (target_tag,))
    target_user = cur.fetchone()
    cur.close()
    conn.close()
    
    if not target_user:
        emit('private_chat_error', {'error': 'user_not_found', 'message': 'Пользователь не найден'})
        return
    
    if target_user['id'] == user_data['id']:
        emit('private_chat_error', {'error': 'self_chat', 'message': 'Нельзя начать чат с самим собой'})
        return
    
    chat_id = get_or_create_private_chat(user_data['id'], target_user['id'])
    
    if not chat_id:
        emit('private_chat_error', {'error': 'chat_creation_failed'})
        return
    
    join_room(chat_id)
    if chat_id not in rooms:
        rooms[chat_id] = []
    rooms[chat_id].append(request.sid)
    
    history = get_private_chat_history(chat_id, user_data['id'])
    
    emit('private_chat_started', {
        'chat_id': chat_id,
        'user': {
            'id': target_user['id'],
            'username': target_user['username'],
            'tag': target_user['user_tag'],
            'avatar': target_user['avatar']
        },
        'history': history
    })

@socketio.on('send_message')
def handle_message(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    message = data.get('message', '').strip()
    if not message:
        return
    
    update_last_seen(user_data['id'])
    
    msg_id = save_message(
        user_id=user_data['id'],
        username=user_data['username'],
        user_tag=user_data['tag'],
        avatar=user_data['avatar'],
        msg_type='text',
        text=message
    )
    
    emit('new_message', {
        'id': msg_id,
        'user': user_data['username'],
        'tag': user_data['tag'],
        'avatar': user_data['avatar'],
        'text': message,
        'type': 'text',
        'is_favorite': False,
        'time': datetime.now().strftime('%H:%M')
    }, broadcast=True)

@socketio.on('send_file')
def handle_file(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    filename = data.get('filename', '')
    file_data = data.get('file', '')
    
    if not filename or not file_data:
        return
    
    update_last_seen(user_data['id'])
    
    try:
        if ',' in file_data:
            file_data = file_data.split(',')[1]
        
        filename = secure_filename(filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        with open(filepath, 'wb') as f:
            f.write(base64.b64decode(file_data))
        
        msg_id = save_message(
            user_id=user_data['id'],
            username=user_data['username'],
            user_tag=user_data['tag'],
            avatar=user_data['avatar'],
            msg_type='file',
            filename=filename,
            filepath=f'/uploads/{filename}'
        )
        
        emit('new_message', {
            'id': msg_id,
            'user': user_data['username'],
            'tag': user_data['tag'],
            'avatar': user_data['avatar'],
            'filename': filename,
            'filepath': f'/uploads/{filename}',
            'type': 'file',
            'is_favorite': False,
            'time': datetime.now().strftime('%H:%M')
        }, broadcast=True)
        
    except Exception as e:
        print(f'❌ Error sending file: {e}')
        emit('file_error', {'error': 'Ошибка при отправке файла'})

@socketio.on('send_private_message')
def handle_send_private_message(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    chat_id = data.get('chat_id')
    message = data.get('message', '').strip()
    
    if not chat_id or not message:
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user1_id, user2_id FROM private_chats WHERE chat_id = %s", (chat_id,))
    chat = cur.fetchone()
    cur.close()
    conn.close()
    
    if not chat:
        return
    
    receiver_id = chat['user2_id'] if chat['user1_id'] == user_data['id'] else chat['user1_id']
    
    saved = save_private_message(
        chat_id=chat_id,
        sender_id=user_data['id'],
        receiver_id=receiver_id,
        msg_type='text',
        text=message
    )
    
    if not saved:
        return
    
    emit('new_private_message', {
        'id': saved['id'],
        'chat_id': chat_id,
        'sender_id': user_data['id'],
        'sender_name': user_data['username'],
        'sender_tag': user_data['tag'],
        'sender_avatar': user_data['avatar'],
        'text': message,
        'type': 'text',
        'is_favorite': False,
        'time': saved['created_at'].strftime('%H:%M')
    }, room=chat_id)

@socketio.on('send_private_file')
def handle_send_private_file(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    chat_id = data.get('chat_id')
    filename = data.get('filename', '')
    file_data = data.get('file', '')
    
    if not chat_id or not filename or not file_data:
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user1_id, user2_id FROM private_chats WHERE chat_id = %s", (chat_id,))
    chat = cur.fetchone()
    cur.close()
    conn.close()
    
    if not chat:
        return
    
    receiver_id = chat['user2_id'] if chat['user1_id'] == user_data['id'] else chat['user1_id']
    
    try:
        if ',' in file_data:
            file_data = file_data.split(',')[1]
        
        filename = secure_filename(filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        with open(filepath, 'wb') as f:
            f.write(base64.b64decode(file_data))
        
        saved = save_private_message(
            chat_id=chat_id,
            sender_id=user_data['id'],
            receiver_id=receiver_id,
            msg_type='file',
            filename=filename,
            filepath=f'/uploads/{filename}'
        )
        
        if not saved:
            return
        
        emit('new_private_message', {
            'id': saved['id'],
            'chat_id': chat_id,
            'sender_id': user_data['id'],
            'sender_name': user_data['username'],
            'sender_tag': user_data['tag'],
            'sender_avatar': user_data['avatar'],
            'filename': filename,
            'filepath': f'/uploads/{filename}',
            'type': 'file',
            'is_favorite': False,
            'time': saved['created_at'].strftime('%H:%M')
        }, room=chat_id)
        
    except Exception as e:
        print(f'❌ Error sending private file: {e}')
        emit('private_file_error', {'error': 'Ошибка при отправке файла'})

@socketio.on('get_private_chat_history')
def handle_get_private_chat_history(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    chat_id = data.get('chat_id')
    if not chat_id:
        return
    
    history = get_private_chat_history(chat_id, user_data['id'])
    emit('get_private_chat_history', history)

@socketio.on('join_private_chat')
def handle_join_private_chat(data):
    chat_id = data.get('chat_id')
    if not chat_id:
        return
    
    join_room(chat_id)
    if chat_id not in rooms:
        rooms[chat_id] = []
    rooms[chat_id].append(request.sid)

@socketio.on('leave_private_chat')
def handle_leave_private_chat(data):
    chat_id = data.get('chat_id')
    if not chat_id:
        return
    
    leave_room(chat_id)
    if chat_id in rooms and request.sid in rooms[chat_id]:
        rooms[chat_id].remove(request.sid)

@socketio.on('mark_chat_read')
def handle_mark_chat_read(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    chat_id = data.get('chat_id')
    if not chat_id:
        return
    
    updated = mark_messages_as_read(chat_id, user_data['id'])

@socketio.on('toggle_favorite')
def handle_toggle_favorite(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    message_id = data.get('message_id')
    chat_id = data.get('chat_id')
    is_favorite = data.get('is_favorite', False)
    
    if not message_id:
        return
    
    if chat_id == 'general':
        if is_favorite:
            result = add_to_favorites(user_data['id'], message_id=message_id, chat_id=chat_id)
        else:
            removed = remove_from_favorites(user_data['id'], message_id=message_id)
    else:
        if is_favorite:
            result = add_to_favorites(user_data['id'], private_message_id=message_id, chat_id=chat_id)
        else:
            removed = remove_from_favorites(user_data['id'], private_message_id=message_id)
    
    emit('favorite_toggled', {
        'message_id': message_id,
        'is_favorite': is_favorite
    }, room=request.sid)

@socketio.on('get_favorites')
def handle_get_favorites():
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    favorites = get_favorites(user_data['id'])
    emit('favorites_list', favorites)

@socketio.on('get_message_history')
def handle_get_message_history():
    user_data = users.get(request.sid)
    user_id = user_data['id'] if user_data else None
    history = get_message_history(user_id)
    emit('message_history', history)

@socketio.on('search_users')
def handle_search_users(data):
    search_tag = data.get('tag', '')
    if len(search_tag) < 2:
        emit('search_results', [])
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, username, user_tag, avatar FROM users 
        WHERE user_tag ILIKE %s AND user_tag IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 20
    """, (f'%{search_tag}%',))
    users_list = cur.fetchall()
    cur.close()
    conn.close()
    
    emit('search_results', users_list)

@socketio.on('update_profile')
def handle_update_profile(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    new_avatar = data.get('avatar')
    if not new_avatar:
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET avatar = %s WHERE id = %s", (new_avatar, user_data['id']))
    conn.commit()
    cur.close()
    conn.close()
    
    user_data['avatar'] = new_avatar
    
    emit('profile_updated', {
        'user_id': user_data['id'],
        'username': user_data['username'],
        'tag': user_data['tag'],
        'avatar': new_avatar
    }, broadcast=True)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=True, host='0.0.0.0', port=port)