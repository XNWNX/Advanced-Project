import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, flash
from google import genai
from PIL import Image
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv # ВОЗВРАЩАЕМ ЧТЕНИЕ .env

load_dotenv() # Загружаем скрытый ключ

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.secret_key = 'some_random_secret_string_for_sessions'

# БЕРЕМ КЛЮЧ ИЗ .env, ЧТОБЫ ИИ СНОВА ЗАРАБОТАЛ!
API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=API_KEY)


def init_db():
    conn = sqlite3.connect('smart_city.db')
    cursor = conn.cursor()
    # ДОБАВЛЕНО: user_id, location, lat, lng
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS complaints
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       user_id
                       INTEGER
                       NOT
                       NULL,
                       location
                       TEXT
                       NOT
                       NULL,
                       lat
                       REAL,
                       lng
                       REAL,
                       description
                       TEXT
                       NOT
                       NULL,
                       photo_filename
                       TEXT
                       NOT
                       NULL,
                       ai_suggestion
                       TEXT,
                       mod_status
                       TEXT
                       DEFAULT
                       'Pending',
                       akim_urgency
                       TEXT
                       DEFAULT
                       'Unassigned',
                       akim_decision
                       TEXT
                       DEFAULT
                       'Waiting for Akimat'
                   )
                   ''')
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS users
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       username
                       TEXT
                       UNIQUE
                       NOT
                       NULL,
                       password
                       TEXT
                       NOT
                       NULL,
                       role
                       TEXT
                       NOT
                       NULL
                   )
                   ''')
    conn.commit()
    conn.close()


init_db()


# --- АВТОРИЗАЦИЯ (Осталась без изменений) ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = 'citizen'
        hashed_pw = generate_password_hash(password)
        try:
            conn = sqlite3.connect('smart_city.db')
            cursor = conn.cursor()
            cursor.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)', (username, hashed_pw, role))
            conn.commit()
            conn.close()
            flash("Account created successfully! You can now log in.", "success")
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash("Username taken. Try another one.", "warning")
            return redirect(url_for('register'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        login_type = request.form.get('login_type')

        conn = sqlite3.connect('smart_city.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            actual_role = user['role']
            if login_type == 'staff' and actual_role == 'citizen':
                flash("Access Denied: You do not have staff permissions.", "danger")
                return redirect(url_for('login'))
            if login_type == 'citizen' and actual_role != 'citizen':
                flash("Error: You are a Staff member. Use the Staff tab.", "danger")
                return redirect(url_for('login'))

            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = actual_role

            if actual_role == 'moderator':
                return redirect(url_for('moderator_dashboard'))
            elif actual_role == 'akim':
                return redirect(url_for('akim_dashboard'))
            else:
                return redirect(url_for('home'))
        else:
            flash("Invalid username or password.", "danger")
            return redirect(url_for('login'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- ПОРТАЛ ЖИТЕЛЯ ---
@app.route('/')
def home():
    if 'user_id' not in session or session.get('role') != 'citizen':
        return redirect(url_for('login'))

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Мои жалобы
    cursor.execute("SELECT * FROM complaints WHERE user_id = ? ORDER BY id DESC", (session['user_id'],))
    my_complaints = [dict(row) for row in cursor.fetchall()]

    # ВСЕ жалобы города (для карты) - исключаем отклоненные модератором
    cursor.execute("SELECT * FROM complaints WHERE mod_status != 'Rejected'")
    all_complaints = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Статистика
    stats = {
        'total': len(my_complaints),
        'pending': sum(1 for c in my_complaints if c['mod_status'] == 'Pending'),
        'resolved': sum(1 for c in my_complaints if c['akim_decision'] == 'Resolved')
    }

    return render_template('index.html', username=session['username'], complaints=my_complaints,
                           all_complaints=all_complaints, stats=stats)


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'user_id' not in session or session.get('role') != 'citizen':
        return redirect(url_for('login'))

    description = request.form.get('description')
    location = request.form.get('location', 'Unknown location')
    lat = request.form.get('lat')
    lng = request.form.get('lng')
    photo = request.files.get('photo')
    user_id = session['user_id']

    if photo:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo.filename)
        photo.save(filepath)

        # --- ВОЗВРАЩАЕМ МАГИЮ АНТИДУБЛИКАТА ---
        conn = sqlite3.connect('smart_city.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Достаем все активные проблемы
        cursor.execute(
            "SELECT id, location, description FROM complaints WHERE mod_status != 'Rejected' AND akim_decision != 'Resolved'")
        active_issues = cursor.fetchall()
        conn.close()

        # 2. Формируем список для ИИ
        existing_issues_text = "Currently Active Issues in the City:\n"
        if not active_issues:
            existing_issues_text += "No active issues.\n"
        else:
            for issue in active_issues:
                existing_issues_text += f"- Issue ID {issue['id']}: Located at '{issue['location']}'. Description: '{issue['description']}'\n"

        # 3. Отправляем умный промпт
        try:
            img = Image.open(filepath)
            prompt = f"""
            You are a Smart City Duplicate Detection AI.
            A citizen is reporting a NEW issue:
            - Location: "{location}"
            - Description: "{description}"
            - See attached photo.

            {existing_issues_text}

            TASK:
            Compare the NEW issue with the 'Currently Active Issues'. 
            - If the NEW issue is highly likely the SAME problem as an active one (e.g., they share a similar location/vicinity AND represent the same visual issue), respond ONLY with: "DUPLICATE: [ID]" (replace [ID] with the matching Issue ID).
            - If it is a clearly NEW, distinct issue, respond with: "NEW: [One short professional sentence summarizing the issue]".

            IMPORTANT: Do not write any other text. Start your response strictly with "NEW:" or "DUPLICATE:".
            """

            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, img]
            )
            ai_response = response.text.strip()
            print(f"ИИ ответил: {ai_response}")  # Это выведется в консоль PyCharm

            # 4. Логика обработки ответа
            if ai_response.startswith("DUPLICATE:"):
                dup_id = ai_response.split(":")[1].strip()
                flash(
                    f"Thanks! This issue (at {location}) is already in our database (Report #{dup_id}) and is being handled by the Akimat.",
                    "warning")
                os.remove(filepath)  # Удаляем фотку, чтобы не засорять сервер
                return redirect(url_for('home'))

            elif ai_response.startswith("NEW:"):
                ai_category = ai_response.replace("NEW:", "🤖 AI:").strip()
            else:
                ai_category = "🤖 AI: Issue logged for manual review."

        except Exception as e:
            print(f"Ошибка ИИ: {e}")
            ai_category = "🤖 AI Analysis unavailable"

        # 5. Сохраняем в базу ТОЛЬКО если это НОВАЯ жалоба
        conn = sqlite3.connect('smart_city.db')
        cursor = conn.cursor()
        cursor.execute('''
                       INSERT INTO complaints (user_id, location, lat, lng, description, photo_filename, ai_suggestion)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ''', (user_id, location, lat, lng, description, photo.filename, ai_category))
        conn.commit()
        conn.close()

        flash("Your report has been successfully submitted and categorized by AI!", "success")
        return redirect(url_for('home'))

    return redirect(url_for('home'))


# --- АДМИНКИ (Остались без изменений) ---
# --- ПОРТАЛ МОДЕРАТОРА ---

@app.route('/moderator')
def moderator_dashboard():
    # Жесткая защита: пускаем только модератора
    if 'user_id' not in session or session.get('role') != 'moderator':
        return redirect(url_for('login'))

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # Теперь достаем ВООБЩЕ ВСЕ жалобы, чтобы показать их в разных вкладках
    cursor.execute("SELECT * FROM complaints ORDER BY id DESC")
    complaints = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Считаем статистику для красивых карточек
    stats = {
        'pending': sum(1 for c in complaints if c['mod_status'] == 'Pending'),
        'approved': sum(1 for c in complaints if c['mod_status'] == 'Approved'),
        'rejected': sum(1 for c in complaints if c['mod_status'] == 'Rejected')
    }

    return render_template('moderator.html', complaints=complaints, stats=stats, username=session['username'])

@app.route('/mod_action/<int:id>', methods=['POST'])
def mod_action(id):
    action = request.form.get('mod_action')
    conn = sqlite3.connect('smart_city.db');
    cursor = conn.cursor()
    cursor.execute("UPDATE complaints SET mod_status = ? WHERE id = ?", (action, id))
    conn.commit();
    conn.close()
    return redirect(url_for('moderator_dashboard'))


@app.route('/akim')
def akim_dashboard():
    if 'user_id' not in session or session.get('role') != 'akim':
        return redirect(url_for('login'))

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # Аким видит только те жалобы, которые уже одобрил Модератор
    cursor.execute("SELECT * FROM complaints WHERE mod_status = 'Approved' ORDER BY id DESC")
    complaints = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Считаем KPI для Акима
    stats = {
        'awaiting': sum(1 for c in complaints if c['akim_decision'] == 'Waiting for Akimat'),
        'urgent': sum(1 for c in complaints if c['akim_urgency'] == 'High' and c['akim_decision'] != 'Resolved'),
        'resolved': sum(1 for c in complaints if c['akim_decision'] == 'Resolved')
    }

    return render_template('akim.html', complaints=complaints, stats=stats, username=session['username'])

@app.route('/akim_action/<int:id>', methods=['POST'])
def akim_action(id):
    urgency = request.form.get('urgency');
    decision = request.form.get('decision')
    conn = sqlite3.connect('smart_city.db');
    cursor = conn.cursor()
    cursor.execute("UPDATE complaints SET akim_urgency = ?, akim_decision = ? WHERE id = ?", (urgency, decision, id))
    conn.commit();
    conn.close()
    return redirect(url_for('akim_dashboard'))


if __name__ == '__main__':
    app.run(debug=True)