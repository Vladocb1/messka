from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.errors import UniqueViolation
from werkzeug.utils import secure_filename
import base64
from datetime import datetime, timedelta
from urllib.parse import urlparse
import re
import hashlib
import secrets

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(32)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
socketio = SocketIO(app, cors_allowed_origins="*", ping_timeout=60, ping_interval=25)

# === PostgreSQL подключение ===
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    parsed_url = urlparse(DATABASE_URL)
    DB_HOST = parsed_url.hostname
    DB_PORT = parsed_url.port
    DB_NAME = parsed_url.path[1:]
    DB_USER = parsed_url.username
    DB_PASS = parsed_url.password
    print("✅ Подключение к базе данных на Render")
else:
    DB_HOST = 'localhost'
    DB_PORT = '5432'
    DB_NAME = 'messka'
    DB_USER = 'postgres'
    DB_PASS = 'SudoSQL'
    print("✅ Подключение к локальной базе данных")

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        cursor_factory=RealDictCursor
    )

# === ИНИЦИАЛИЗАЦИЯ БД ===
def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Таблица пользователей
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                socket_id VARCHAR(255) UNIQUE,
                username VARCHAR(50) NOT NULL,
                user_tag VARCHAR(30) UNIQUE,
                avatar TEXT,
                session_token VARCHAR(64) UNIQUE,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица сообщений (общий чат)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                username VARCHAR(255) NOT NULL,
                user_tag VARCHAR(30),
                avatar TEXT,
                message_text TEXT,
                filename VARCHAR(255),
                filepath TEXT,
                message_type VARCHAR(50) NOT NULL,
                is_favorite BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица личных чатов
        cur.execute('''
            CREATE TABLE IF NOT EXISTS private_chats (
                id SERIAL PRIMARY KEY,
                chat_id VARCHAR(64) UNIQUE NOT NULL,
                user1_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                user2_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user1_id, user2_id)
            )
        ''')
        
        # Таблица личных сообщений
        cur.execute('''
            CREATE TABLE IF NOT EXISTS private_messages (
                id SERIAL PRIMARY KEY,
                chat_id VARCHAR(64) REFERENCES private_chats(chat_id) ON DELETE CASCADE,
                sender_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                receiver_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                message_text TEXT,
                filename VARCHAR(255),
                filepath TEXT,
                message_type VARCHAR(50) NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                is_favorite BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица избранного
        cur.execute('''
            CREATE TABLE IF NOT EXISTS favorites (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
                private_message_id INTEGER REFERENCES private_messages(id) ON DELETE CASCADE,
                chat_id VARCHAR(64),
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT unique_general_favorite UNIQUE (user_id, message_id),
                CONSTRAINT unique_private_favorite UNIQUE (user_id, private_message_id)
            )
        ''')
        
        # Индексы
        cur.execute('CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_users_socket ON users(socket_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_users_tag ON users(user_tag)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_users_session ON users(session_token)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_private_chats_users ON private_chats(user1_id, user2_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_private_chats_chat_id ON private_chats(chat_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_private_messages_chat ON private_messages(chat_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_private_messages_sender ON private_messages(sender_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_private_messages_read ON private_messages(is_read)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_favorites_message ON favorites(message_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_favorites_private ON favorites(private_message_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_messages_favorite ON messages(is_favorite)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_private_messages_favorite ON private_messages(is_favorite)')
        
        conn.commit()
        
        # Триггер для обновления юзернеймов в сообщениях
        cur.execute('''
            CREATE OR REPLACE FUNCTION update_username_in_messages()
            RETURNS TRIGGER AS $$
            BEGIN
                UPDATE messages 
                SET user_tag = NEW.user_tag 
                WHERE user_id = NEW.id;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        ''')
        
        cur.execute('''
            DROP TRIGGER IF EXISTS update_messages_username ON users;
            CREATE TRIGGER update_messages_username
                AFTER UPDATE OF user_tag ON users
                FOR EACH ROW
                EXECUTE FUNCTION update_username_in_messages();
        ''')
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Все таблицы готовы")
    except Exception as e:
        print(f"❌ Ошибка при создании таблиц: {e}")

init_db()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def validate_tag(tag):
    if not tag:
        return False
    return bool(re.match(r'^[a-z0-9_]{3,20}$', tag))

def generate_session_token():
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()

def generate_chat_id(user1_id, user2_id):
    ids = sorted([user1_id, user2_id])
    return hashlib.sha256(f"{ids[0]}-{ids[1]}".encode()).hexdigest()[:16]

def get_user_by_socket(socket_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, user_tag, avatar, session_token FROM users WHERE socket_id = %s", (socket_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def get_user_by_tag(tag):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, user_tag, avatar FROM users WHERE user_tag = %s", (tag,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def get_user_by_session(session_token):
    if not session_token:
        return None
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, user_tag, avatar FROM users WHERE session_token = %s", (session_token,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def update_last_seen(socket_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_seen = CURRENT_TIMESTAMP WHERE socket_id = %s", (socket_id,))
    conn.commit()
    cur.close()
    conn.close()

# === РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ===
def check_tag_available(tag, exclude_socket=None):
    if not tag:
        return False
    conn = get_db_connection()
    cur = conn.cursor()
    if exclude_socket:
        cur.execute("SELECT id FROM users WHERE user_tag = %s AND socket_id != %s", (tag, exclude_socket))
    else:
        cur.execute("SELECT id FROM users WHERE user_tag = %s", (tag,))
    existing = cur.fetchone()
    cur.close()
    conn.close()
    return existing is None

def create_user(socket_id, username, user_tag, avatar):
    conn = get_db_connection()
    cur = conn.cursor()
    session_token = generate_session_token()
    try:
        cur.execute("""
            INSERT INTO users (socket_id, username, user_tag, avatar, session_token) 
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, username, user_tag, avatar, session_token
        """, (socket_id, username, user_tag, avatar, session_token))
        new_user = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return {'success': True, 'user': new_user}
    except UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return {'success': False, 'error': 'tag_taken', 'message': 'Этот юзернейм уже занят'}
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {'success': False, 'error': str(e)}

def update_user(socket_id, username, user_tag, avatar):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, user_tag FROM users WHERE socket_id = %s", (socket_id,))
        old_user = cur.fetchone()
        
        if not old_user:
            cur.close()
            conn.close()
            return {'success': False, 'error': 'user_not_found'}
        
        cur.execute("""
            UPDATE users 
            SET username = %s, user_tag = %s, avatar = %s, last_seen = CURRENT_TIMESTAMP 
            WHERE socket_id = %s
            RETURNING id, username, user_tag, avatar, session_token
        """, (username, user_tag, avatar, socket_id))
        updated = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        
        return {'success': True, 'user': updated, 'old_tag': old_user['user_tag']}
    except UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return {'success': False, 'error': 'tag_taken', 'message': 'Этот юзернейм уже занят'}
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {'success': False, 'error': str(e)}

def login_by_session(socket_id, session_token):
    user = get_user_by_session(session_token)
    if not user:
        return None
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users 
        SET socket_id = %s, last_seen = CURRENT_TIMESTAMP 
        WHERE id = %s
        RETURNING id, username, user_tag, avatar
    """, (socket_id, user['id']))
    updated = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    
    return updated

def find_users_by_tag(search_tag):
    if len(search_tag) < 2:
        return []
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, username, user_tag, avatar FROM users 
        WHERE user_tag ILIKE %s AND user_tag IS NOT NULL
        ORDER BY last_seen DESC
        LIMIT 20
    """, (f'%{search_tag}%',))
    users = cur.fetchall()
    cur.close()
    conn.close()
    return users

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
    """Получить информацию о чате"""
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
    
    # Избранное из общего чата
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
    
    # Избранное из личных чатов
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

def check_message_favorite(user_id, message_id=None, private_message_id=None):
    conn = get_db_connection()
    cur = conn.cursor()
    if message_id:
        cur.execute("SELECT id FROM favorites WHERE user_id = %s AND message_id = %s", (user_id, message_id))
    elif private_message_id:
        cur.execute("SELECT id FROM favorites WHERE user_id = %s AND private_message_id = %s", (user_id, private_message_id))
    else:
        cur.close()
        conn.close()
        return False
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result is not None

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
users = {}  # {socket_id: {'id': user_id, 'username': str, 'tag': str, 'avatar': str, 'token': str}}
rooms = {}  # {chat_id: [socket_id1, socket_id2]}
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

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

@socketio.on('check_session')
def handle_check_session(data):
    session_token = data.get('session_token')
    if not session_token:
        emit('session_result', {'has_session': False})
        return
    
    user = login_by_session(request.sid, session_token)
    if user:
        users[request.sid] = {
            'id': user['id'],
            'username': user['username'],
            'tag': user['user_tag'],
            'avatar': user['avatar'],
            'token': session_token
        }
        
        emit('session_result', {
            'has_session': True,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'tag': user['user_tag'],
                'avatar': user['avatar'],
                'token': session_token
            }
        })
        
        emit('user_joined', {
            'username': user['username'],
            'tag': user['user_tag'],
            'avatar': user['avatar']
        }, broadcast=True)
        
        emit('message_history', get_message_history(user['id']))
        print(f'✨ User logged in via session: {user["username"]} (@{user["user_tag"]})')

@socketio.on('check_username')
def handle_check_username(data):
    tag = data.get('tag', '').lower().strip()
    
    if not tag:
        emit('username_check_result', {'available': False, 'reason': 'empty'})
        return
    
    if not re.match(r'^[a-z0-9_]+$', tag):
        emit('username_check_result', {'available': False, 'reason': 'invalid_chars'})
        return
    
    if len(tag) < 3:
        emit('username_check_result', {'available': False, 'reason': 'too_short'})
        return
    
    if len(tag) > 20:
        emit('username_check_result', {'available': False, 'reason': 'too_long'})
        return
    
    available = check_tag_available(tag, request.sid if get_user_by_socket(request.sid) else None)
    emit('username_check_result', {
        'available': available,
        'tag': tag,
        'reason': 'ok' if available else 'taken'
    })

@socketio.on('register')
def handle_register(data):
    username = data.get('username', '').strip()
    user_tag = data.get('tag', '').lower().strip()
    avatar = data.get('avatar', 'default')
    
    if not username or len(username) > 50:
        emit('register_error', {'error': 'invalid_name', 'message': 'Некорректное имя'})
        return
    
    if not validate_tag(user_tag):
        emit('register_error', {'error': 'invalid_tag', 'message': 'Некорректный юзернейм'})
        return
    
    existing = get_user_by_socket(request.sid)
    
    if existing:
        result = update_user(request.sid, username, user_tag, avatar)
    else:
        result = create_user(request.sid, username, user_tag, avatar)
    
    if result['success']:
        user = result['user']
        users[request.sid] = {
            'id': user['id'],
            'username': user['username'],
            'tag': user['user_tag'],
            'avatar': user['avatar'],
            'token': user['session_token']
        }
        
        emit('user_joined', {
            'username': user['username'],
            'tag': user['user_tag'],
            'avatar': user['avatar']
        }, broadcast=True)
        
        emit('register_success', {
            'user': {
                'id': user['id'],
                'username': user['username'],
                'tag': user['user_tag'],
                'avatar': user['avatar'],
                'token': user['session_token']
            }
        })
        
        emit('message_history', get_message_history(user['id']))
        print(f'✨ User registered: {user["username"]} (@{user["user_tag"]})')
    else:
        emit('register_error', result)

@socketio.on('update_profile')
def handle_update_profile(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    old_username = user_data['username']
    old_tag = user_data['tag']
    old_avatar = user_data['avatar']
    new_username = data.get('username', old_username)
    new_tag = data.get('tag', old_tag)
    new_avatar = data.get('avatar', old_avatar)
    
    if new_tag != old_tag and not validate_tag(new_tag):
        emit('profile_error', {'error': 'invalid_tag', 'message': 'Некорректный юзернейм'})
        return
    
    result = update_user(request.sid, new_username, new_tag, new_avatar)
    
    if result['success']:
        user = result['user']
        users[request.sid]['username'] = user['username']
        users[request.sid]['tag'] = user['user_tag']
        users[request.sid]['avatar'] = user['avatar']
        
        emit('message_history', get_message_history(user['id']), broadcast=True)
        
        emit('profile_updated', {
            'socket_id': request.sid,
            'old_username': old_username if old_username != user['username'] else None,
            'new_username': user['username'] if old_username != user['username'] else None,
            'old_tag': old_tag if old_tag != user['user_tag'] else None,
            'new_tag': user['user_tag'] if old_tag != user['user_tag'] else None,
            'old_avatar': old_avatar if old_avatar != user['avatar'] else None,
            'new_avatar': user['avatar'] if old_avatar != user['avatar'] else None
        }, broadcast=True)
        
        print(f'📝 Profile updated: {user["username"]} (@{user["user_tag"]})')
    else:
        emit('profile_error', result)

@socketio.on('search_users')
def handle_search_users(data):
    search_tag = data.get('tag', '')
    if len(search_tag) < 2:
        emit('search_results', [])
        return
    
    found_users = find_users_by_tag(search_tag)
    emit('search_results', found_users)

@socketio.on('start_private_chat')
def handle_start_private_chat(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    target_tag = data.get('tag', '').lower().strip()
    if not target_tag:
        emit('private_chat_error', {'error': 'empty_tag'})
        return
    
    target_user = get_user_by_tag(target_tag)
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

@socketio.on('mark_chat_read')
def handle_mark_chat_read(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    chat_id = data.get('chat_id')
    if not chat_id:
        return
    
    updated = mark_messages_as_read(chat_id, user_data['id'])

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

@socketio.on('send_message')
def handle_message(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    message = data.get('message', '').strip()
    if not message:
        return
    
    update_last_seen(request.sid)
    
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
    print(f'💬 {user_data["username"]} (@{user_data["tag"]}): {message[:30]}...')

@socketio.on('send_file')
def handle_file(data):
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    filename = data.get('filename', '')
    file_data = data.get('file', '')
    
    if not filename or not file_data:
        return
    
    update_last_seen(request.sid)
    
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
        print(f'📎 {user_data["username"]} (@{user_data["tag"]}) sent file: {filename}')
        
    except Exception as e:
        print(f'❌ Error sending file: {e}')
        emit('file_error', {'error': 'Ошибка при отправке файла'})

@socketio.on('get_message_history')
def handle_get_message_history():
    user_data = users.get(request.sid)
    user_id = user_data['id'] if user_data else None
    history = get_message_history(user_id)
    emit('message_history', history)

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

@socketio.on('get_chat_by_id')
def handle_get_chat_by_id(data):
    """Получить информацию о чате по ID для перехода к сообщению"""
    user_data = users.get(request.sid)
    if not user_data:
        return
    
    chat_id = data.get('chat_id')
    message_id = data.get('message_id')
    
    if not chat_id or chat_id == 'general':
        return
    
    chat_info = get_chat_info(chat_id, user_data['id'])
    
    if chat_info:
        emit('chat_by_id_result', {
            'chat_id': chat_id,
            'message_id': message_id,
            'user': {
                'username': chat_info['username'],
                'tag': chat_info['tag'],
                'avatar': chat_info['avatar']
            }
        })

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=True, host='0.0.0.0', port=port)