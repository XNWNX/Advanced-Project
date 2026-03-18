import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, flash
from google import genai
from PIL import Image
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.secret_key = 'some_random_secret_string_for_sessions'

# Initialize Gemini AI
client = genai.Client(api_key="AIzaSyAHRm5JCVQfQSHwVGZXgVfrGoY0nXHGVTw")  # <--- ТВОЙ КЛЮЧ СЮДА


def init_db():
    conn = sqlite3.connect('smart_city.db')
    cursor = conn.cursor()

    # We split status into multiple columns for multi-tier approval
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS complaints
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
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

    # Users table now supports 3 roles: citizen, moderator, akim
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


# --- AUTHENTICATION ---

@app.route('/register', methods=['GET', 'POST'])
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
            cursor.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)',
                           (username, hashed_pw, role))
            conn.commit()
            conn.close()
            # УСПЕШНАЯ РЕГИСТРАЦИЯ: зеленое уведомление
            flash("Account created successfully! You can now log in.", "success")
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            # ОШИБКА РЕГИСТРАЦИИ: желтое уведомление
            flash("Username already exists. Please try another one.", "warning")
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

        # 🛡️ БЕЗОПАСНОСТЬ: Единая проверка логина и пароля
        if user and check_password_hash(user['password'], password):
            actual_role = user['role']

            # Проверка вкладок
            if login_type == 'staff' and actual_role == 'citizen':
                flash("Access Denied: You do not have staff permissions. Please use the Citizen tab.", "danger")
                return redirect(url_for('login'))

            if login_type == 'citizen' and actual_role != 'citizen':
                flash("Access Denied: You are a Staff member. Please use the Akimat Staff tab.", "danger")
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
            # 🛡️ ЗАЩИТА ОТ ПЕРЕБОРА (Brute-force protection)
            # Мы выдаем одну и ту же ошибку и при неверном логине, и при неверном пароле!
            flash("Invalid username or password.", "danger")
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- CITIZEN PORTAL ---

@app.route('/')
def home():
    if 'user_id' not in session or session.get('role') != 'citizen':
        return redirect(url_for('login'))
    return render_template('index.html', username=session['username'])


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'user_id' not in session or session.get('role') != 'citizen':
        return redirect(url_for('login'))

    description = request.form.get('description')
    photo = request.files.get('photo')

    if photo:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo.filename)
        photo.save(filepath)

        try:
            img = Image.open(filepath)
            prompt = f"""
            You are a city infrastructure AI. Look at the photo and user description: "{description}".
            Provide one short, professional sentence summarizing the issue.
            """
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, img]
            )
            ai_category = f"AI: {response.text.strip()}"
        except Exception as e:
            ai_category = "AI Analysis error"

        conn = sqlite3.connect('smart_city.db')
        cursor = conn.cursor()
        # Default status is 'Pending' for moderator
        cursor.execute('''
                       INSERT INTO complaints (description, photo_filename, ai_suggestion)
                       VALUES (?, ?, ?)
                       ''', (description, photo.filename, ai_category))
        conn.commit()
        conn.close()

        return redirect(url_for('home'))

    return "Upload failed."


# --- MODERATOR PORTAL ---

@app.route('/moderator')
def moderator_dashboard():
    # Only moderators allowed
    if session.get('role') != 'moderator':
        return "Access denied. Moderator only."

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # Mod only sees Pending or Rejected issues
    cursor.execute("SELECT * FROM complaints WHERE mod_status != 'Approved' ORDER BY id DESC")
    complaints = cursor.fetchall()
    conn.close()

    return render_template('moderator.html', complaints=complaints, username=session['username'])


@app.route('/mod_action/<int:id>', methods=['POST'])
def mod_action(id):
    if session.get('role') != 'moderator':
        return "Access denied."

    action = request.form.get('mod_action')  # Approve or Reject

    conn = sqlite3.connect('smart_city.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE complaints SET mod_status = ? WHERE id = ?", (action, id))
    conn.commit()
    conn.close()

    return redirect(url_for('moderator_dashboard'))


# --- AKIM PORTAL ---

@app.route('/akim')
def akim_dashboard():
    # Only Akim allowed
    if session.get('role') != 'akim':
        return "Access denied. Akim only."

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # Akim ONLY sees issues that the moderator approved
    cursor.execute("SELECT * FROM complaints WHERE mod_status = 'Approved' ORDER BY id DESC")
    complaints = cursor.fetchall()
    conn.close()

    return render_template('akim.html', complaints=complaints, username=session['username'])


@app.route('/akim_action/<int:id>', methods=['POST'])
def akim_action(id):
    if session.get('role') != 'akim':
        return "Access denied."

    urgency = request.form.get('urgency')
    decision = request.form.get('decision')

    conn = sqlite3.connect('smart_city.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE complaints SET akim_urgency = ?, akim_decision = ? WHERE id = ?",
                   (urgency, decision, id))
    conn.commit()
    conn.close()

    return redirect(url_for('akim_dashboard'))


if __name__ == '__main__':
    app.run(debug=True)