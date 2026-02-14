from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.utils import secure_filename
import base64
from datetime import datetime
from urllib.parse import urlparse

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['UPLOAD_FOLDER'] = 'uploads'
socketio = SocketIO(app)

# === PostgreSQL подключение ===
# Берём настройки из переменной окружения (на Render) или используем локальные
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    # Режим Render: парсим строку подключения
    parsed_url = urlparse(DATABASE_URL)
    DB_HOST = parsed_url.hostname
    DB_PORT = parsed_url.port
    DB_NAME = parsed_url.path[1:]  # Убираем первый слеш
    DB_USER = parsed_url.username
    DB_PASS = parsed_url.password
    print("✅ Подключение к базе данных на Render")
else:
    # Локальный режим (твой компьютер)
    DB_HOST = 'localhost'
    DB_PORT = '5432'
    DB_NAME = 'messka'
    DB_USER = 'postgres'
    DB_PASS = 'SudoSQL'  # твой локальный пароль
    print("✅ Подключение к локальной базе данных")

def get_db_connection():
    """Подключение к БД"""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        cursor_factory=RealDictCursor
    )

# Создание таблицы с полем avatar
def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                avatar TEXT,
                message_text TEXT,
                filename VARCHAR(255),
                filepath TEXT,
                message_type VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Таблица messages готова (с аватарками)")
    except Exception as e:
        print(f"❌ Ошибка при создании таблицы: {e}")

# Пытаемся инициализировать БД
try:
    init_db()
except Exception as e:
    print(f"⚠️ Не удалось инициализировать БД: {e}")

# === РАБОТА С БД ===
def save_message(username, avatar, msg_type, text=None, filename=None, filepath=None):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO messages (username, avatar, message_text, filename, filepath, message_type)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (username, avatar, text, filename, filepath, msg_type))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Сообщение сохранено: {username}")
    except Exception as e:
        print(f"❌ Ошибка сохранения сообщения: {e}")

def get_message_history():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            SELECT username, avatar, message_text, filename, filepath, message_type,
                   TO_CHAR(created_at, 'HH24:MI') as formatted_time
            FROM messages ORDER BY created_at ASC
        ''')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        messages = []
        for r in rows:
            messages.append({
                'user': r['username'],
                'avatar': r['avatar'],
                'text': r['message_text'],
                'filename': r['filename'],
                'filepath': r['filepath'],
                'type': r['message_type'],
                'time': r['formatted_time']
            })
        return messages
    except Exception as e:
        print(f"❌ Ошибка загрузки истории: {e}")
        return []

# === СОКЕТЫ ===
users = {}  # {sid: {'username': str, 'avatar': str}}
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    print('✅ Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    user_data = users.pop(request.sid, None)
    if user_data:
        emit('user_left', user_data['username'], broadcast=True)

@socketio.on('set_username')
def handle_set_username(data):
    username = data['username']
    avatar = data.get('avatar', 'default')
    
    users[request.sid] = {
        'username': username,
        'avatar': avatar
    }
    
    emit('user_joined', username, broadcast=True)
    emit('message_history', get_message_history())

@socketio.on('send_message')
def handle_message(data):
    user_data = users.get(request.sid, {'username': 'Anonymous', 'avatar': 'default'})
    
    save_message(
        username=user_data['username'],
        avatar=user_data['avatar'],
        msg_type='text',
        text=data['message']
    )
    
    emit('new_message', {
        'user': user_data['username'],
        'avatar': user_data['avatar'],
        'text': data['message'],
        'type': 'text',
        'time': datetime.now().strftime('%H:%M')
    }, broadcast=True)

@socketio.on('send_file')
def handle_file(data):
    user_data = users.get(request.sid, {'username': 'Anonymous', 'avatar': 'default'})
    
    file_data = data['file'].split(',')[1]
    filename = secure_filename(data['filename'])
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    with open(filepath, 'wb') as f:
        f.write(base64.b64decode(file_data))
    
    save_message(
        username=user_data['username'],
        avatar=user_data['avatar'],
        msg_type='file',
        filename=filename,
        filepath=f'/uploads/{filename}'
    )
    
    emit('new_message', {
        'user': user_data['username'],
        'avatar': user_data['avatar'],
        'filename': filename,
        'filepath': f'/uploads/{filename}',
        'type': 'file',
        'time': datetime.now().strftime('%H:%M')
    }, broadcast=True)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    # Берём порт из окружения (для Render) или 5000 для локальной разработки
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=True, host='0.0.0.0', port=port)