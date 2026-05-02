import os
import math
import mimetypes
import re
import io
import sqlite3
import unicodedata
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash
from google import genai
from PIL import Image
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv # ВОЗВРАЩАЕМ ЧТЕНИЕ .env

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:
    boto3 = None
    BotoConfig = None

load_dotenv() # Загружаем скрытый ключ

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.secret_key = 'some_random_secret_string_for_sessions'

# БЕРЕМ КЛЮЧ ИЗ .env, ЧТОБЫ ИИ СНОВА ЗАРАБОТАЛ!
API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=API_KEY)

OBJECT_STORAGE_ENABLED = os.getenv("OBJECT_STORAGE_ENABLED", "").lower() in {"1", "true", "yes", "on"}
OBJECT_STORAGE_BUCKET = os.getenv("OBJECT_STORAGE_BUCKET", "").strip()
OBJECT_STORAGE_ENDPOINT = os.getenv("OBJECT_STORAGE_ENDPOINT", "").strip()
OBJECT_STORAGE_REGION = os.getenv("OBJECT_STORAGE_REGION", "us-east-1").strip() or "us-east-1"
OBJECT_STORAGE_ACCESS_KEY = os.getenv("OBJECT_STORAGE_ACCESS_KEY", "").strip()
OBJECT_STORAGE_SECRET_KEY = os.getenv("OBJECT_STORAGE_SECRET_KEY", "").strip()
OBJECT_STORAGE_PUBLIC_BASE_URL = os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL", "").strip().rstrip("/")
OBJECT_STORAGE_SECURE = os.getenv("OBJECT_STORAGE_SECURE", "true").lower() in {"1", "true", "yes", "on"}
OBJECT_STORAGE_AUTO_CREATE_BUCKET = os.getenv("OBJECT_STORAGE_AUTO_CREATE_BUCKET", "true").lower() in {"1", "true", "yes", "on"}


def object_storage_is_configured():
    return all([
        OBJECT_STORAGE_ENABLED,
        boto3 is not None,
        OBJECT_STORAGE_BUCKET,
        OBJECT_STORAGE_ENDPOINT,
        OBJECT_STORAGE_ACCESS_KEY,
        OBJECT_STORAGE_SECRET_KEY
    ])


def get_object_storage_client():
    if not object_storage_is_configured():
        return None
    return boto3.client(
        "s3",
        endpoint_url=OBJECT_STORAGE_ENDPOINT,
        region_name=OBJECT_STORAGE_REGION,
        aws_access_key_id=OBJECT_STORAGE_ACCESS_KEY,
        aws_secret_access_key=OBJECT_STORAGE_SECRET_KEY,
        use_ssl=OBJECT_STORAGE_SECURE,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}) if BotoConfig else None
    )


def ensure_object_storage_bucket():
    client_instance = get_object_storage_client()
    if not client_instance:
        return False
    try:
        client_instance.head_bucket(Bucket=OBJECT_STORAGE_BUCKET)
        return True
    except Exception:
        if not OBJECT_STORAGE_AUTO_CREATE_BUCKET:
            return False
        try:
            if OBJECT_STORAGE_REGION == "us-east-1":
                client_instance.create_bucket(Bucket=OBJECT_STORAGE_BUCKET)
            else:
                client_instance.create_bucket(
                    Bucket=OBJECT_STORAGE_BUCKET,
                    CreateBucketConfiguration={"LocationConstraint": OBJECT_STORAGE_REGION}
                )
            return True
        except Exception as exc:
            print(f"Object storage bucket init failed: {exc}")
            return False


def generate_storage_name(original_filename):
    safe_name = secure_filename(original_filename or "upload.jpg")
    _, ext = os.path.splitext(safe_name)
    ext = ext.lower() if ext else ".jpg"
    return f"reports/{datetime.now().strftime('%Y/%m')}/{uuid.uuid4().hex}{ext}"


def guess_content_type(filename):
    content_type, _ = mimetypes.guess_type(filename or "")
    return content_type or "application/octet-stream"


def save_photo_bytes(photo_bytes, original_filename):
    storage_key = generate_storage_name(original_filename)
    if object_storage_is_configured() and ensure_object_storage_bucket():
        client_instance = get_object_storage_client()
        try:
            client_instance.put_object(
                Bucket=OBJECT_STORAGE_BUCKET,
                Key=storage_key,
                Body=photo_bytes,
                ContentType=guess_content_type(original_filename)
            )
            return storage_key, "object"
        except Exception as exc:
            print(f"Object storage upload failed, falling back to local file storage: {exc}")

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    local_filename = os.path.basename(storage_key)
    local_path = os.path.join(app.config['UPLOAD_FOLDER'], local_filename)
    with open(local_path, "wb") as local_file:
        local_file.write(photo_bytes)
    return local_filename, "local"


def read_photo_bytes(storage_key, storage_backend):
    if storage_backend == "object" and object_storage_is_configured():
        client_instance = get_object_storage_client()
        try:
            response = client_instance.get_object(Bucket=OBJECT_STORAGE_BUCKET, Key=storage_key)
            return response["Body"].read()
        except Exception as exc:
            print(f"Object storage read failed for {storage_key}: {exc}")
            return None

    local_path = os.path.join(app.config['UPLOAD_FOLDER'], storage_key)
    if os.path.exists(local_path):
        with open(local_path, "rb") as local_file:
            return local_file.read()
    return None


def build_photo_url(storage_key, storage_backend):
    if not storage_key:
        return ""
    if storage_backend == "object":
        if OBJECT_STORAGE_PUBLIC_BASE_URL:
            return f"{OBJECT_STORAGE_PUBLIC_BASE_URL}/{storage_key}"
        return url_for("photo_proxy", storage_key=storage_key)
    return f"/static/uploads/{storage_key}"


def attach_photo_urls(complaints):
    for complaint in complaints:
        backend = complaint.get("photo_storage_backend") or "local"
        complaint["photo_storage_backend"] = backend
        complaint["photo_url"] = build_photo_url(complaint.get("photo_filename", ""), backend)
    return complaints


def build_report_code(complaint_id):
    try:
        return f"SC-{int(complaint_id):06d}"
    except (TypeError, ValueError):
        return "SC-UNKNOWN"


def attach_report_codes(complaints):
    for complaint in complaints:
        complaint["report_code"] = build_report_code(complaint.get("id"))
    return complaints


def filter_complaints_by_report_query(complaints, query):
    normalized_query = (query or "").strip().upper()
    if not normalized_query:
        return complaints

    digits_only = "".join(ch for ch in normalized_query if ch.isdigit())
    filtered = []
    for complaint in complaints:
        report_code = complaint.get("report_code") or build_report_code(complaint.get("id"))
        matches_code = normalized_query in report_code.upper()
        matches_id = digits_only and str(complaint.get("id")) == digits_only
        if matches_code or matches_id:
            filtered.append(complaint)
    return filtered


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
                   CREATE TABLE IF NOT EXISTS complaint_history
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       complaint_id
                       INTEGER
                       NOT
                       NULL,
                       actor_role
                       TEXT
                       NOT
                       NULL,
                       actor_username
                       TEXT
                       NOT
                       NULL,
                       action
                       TEXT
                       NOT
                       NULL,
                       note
                       TEXT,
                       created_at
                       TEXT
                       NOT
                       NULL
                   )
                   ''')
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS complaint_notes
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       complaint_id
                       INTEGER
                       NOT
                       NULL,
                       author_role
                       TEXT
                       NOT
                       NULL,
                       author_username
                       TEXT
                       NOT
                       NULL,
                       note_text
                       TEXT
                       NOT
                       NULL,
                       created_at
                       TEXT
                       NOT
                       NULL
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
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(complaints)").fetchall()}
    if 'created_at' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN created_at TEXT")
        cursor.execute("UPDATE complaints SET created_at = datetime('now') WHERE created_at IS NULL OR created_at = ''")
    if 'moderator_comment' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN moderator_comment TEXT DEFAULT ''")
    if 'reject_reason' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN reject_reason TEXT DEFAULT ''")
    if 'akim_comment' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN akim_comment TEXT DEFAULT ''")
    if 'photo_storage_backend' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN photo_storage_backend TEXT DEFAULT 'local'")
    if 'citizen_status' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN citizen_status TEXT DEFAULT ''")
    if 'citizen_feedback' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN citizen_feedback TEXT DEFAULT ''")
    if 'citizen_can_resubmit' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN citizen_can_resubmit INTEGER DEFAULT 0")
    if 'revision_count' not in columns:
        cursor.execute("ALTER TABLE complaints ADD COLUMN revision_count INTEGER DEFAULT 0")
    cursor.execute("UPDATE complaints SET photo_storage_backend = 'local' WHERE photo_storage_backend IS NULL OR photo_storage_backend = ''")
    cursor.execute("UPDATE complaints SET akim_decision = 'Accepted' WHERE akim_decision = 'Will Fix'")
    conn.commit()
    conn.close()


init_db()


@app.route('/photo/<path:storage_key>')
def photo_proxy(storage_key):
    photo_bytes = read_photo_bytes(storage_key, "object")
    if not photo_bytes:
        return "", 404
    return app.response_class(photo_bytes, mimetype=guess_content_type(storage_key))


def get_complaint_category(complaint):
    if complaint['mod_status'] == 'Rejected':
        return 'rejected'
    if complaint['mod_status'] == 'Pending':
        return 'pending'
    if complaint['mod_status'] == 'Approved':
        return 'approved'
    return 'other'


def get_chart_status(complaint):
    if complaint.get('citizen_status') == 'Action Required':
        return 'pending'
    if complaint['mod_status'] == 'Rejected':
        return 'rejected'
    if complaint['akim_decision'] == 'Resolved':
        return 'resolved'
    if complaint['mod_status'] == 'Approved':
        return 'approved'
    if complaint['mod_status'] == 'Pending':
        return 'pending'
    return 'other'


def parse_created_at(value):
    if not value:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.now()


def build_monthly_stats(complaints, months=6):
    now = datetime.now()
    month_starts = []
    current = datetime(now.year, now.month, 1)

    for _ in range(months):
        month_starts.append(current)
        if current.month == 1:
            current = datetime(current.year - 1, 12, 1)
        else:
            current = datetime(current.year, current.month - 1, 1)

    month_starts.reverse()
    labels = [month.strftime("%B %Y") for month in month_starts]
    keys = [month.strftime("%Y-%m") for month in month_starts]
    stats = {
        'labels': labels,
        'pending': [0] * len(keys),
        'approved': [0] * len(keys),
        'rejected': [0] * len(keys),
        'resolved': [0] * len(keys)
    }
    index_by_key = {key: idx for idx, key in enumerate(keys)}

    for complaint in complaints:
        created_at = parse_created_at(complaint.get('created_at'))
        month_key = created_at.strftime("%Y-%m")
        if month_key not in index_by_key:
            continue

        status_key = get_chart_status(complaint)
        if status_key in stats:
            stats[status_key][index_by_key[month_key]] += 1

    return stats


def normalize_text(value):
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def token_similarity(text_a, text_b):
    tokens_a = set(normalize_text(text_a).split())
    tokens_b = set(normalize_text(text_b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def distance_meters(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return None

    lat1, lng1, lat2, lng2 = map(float, (lat1, lng1, lat2, lng2))
    radius = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def find_likely_duplicate(location, description, lat, lng, active_issues):
    try:
        lat = float(lat) if lat not in (None, "") else None
        lng = float(lng) if lng not in (None, "") else None
    except (TypeError, ValueError):
        lat = None
        lng = None

    best_match = None
    best_score = -1

    for issue in active_issues:
        score = score_duplicate_candidate(location, description, lat, lng, issue)

        if score > best_score:
            best_score = score
            best_match = issue

    if best_match and best_score >= 7:
        return best_match
    return None


def score_duplicate_candidate(location, description, lat, lng, issue):
    score = 0
    issue_distance = distance_meters(lat, lng, issue.get('lat'), issue.get('lng'))
    desc_similarity = token_similarity(description, issue.get('description'))
    loc_similarity = token_similarity(location, issue.get('location'))

    if issue_distance is not None:
        if issue_distance <= 60:
            score += 6
        elif issue_distance <= 120:
            score += 5
        elif issue_distance <= 200:
            score += 4
        elif issue_distance <= 350:
            score += 2

    if desc_similarity >= 0.7:
        score += 5
    elif desc_similarity >= 0.45:
        score += 3
    elif desc_similarity >= 0.25:
        score += 1

    if loc_similarity >= 0.7:
        score += 4
    elif loc_similarity >= 0.45:
        score += 2
    elif loc_similarity >= 0.25:
        score += 1

    same_text_area = desc_similarity >= 0.45 and loc_similarity >= 0.25
    nearby_same_issue = issue_distance is not None and issue_distance <= 150 and (desc_similarity >= 0.25 or loc_similarity >= 0.25)

    if same_text_area or nearby_same_issue:
        score += 2

    return score


def get_duplicate_candidates(location, description, lat, lng, active_issues, limit=4):
    try:
        lat = float(lat) if lat not in (None, "") else None
        lng = float(lng) if lng not in (None, "") else None
    except (TypeError, ValueError):
        lat = None
        lng = None

    scored = []
    for issue in active_issues:
        score = score_duplicate_candidate(location, description, lat, lng, issue)
        scored.append((score, issue))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [issue for score, issue in scored if score > 0][:limit]


def add_history_entry(cursor, complaint_id, actor_role, actor_username, action, note=""):
    cursor.execute(
        '''
        INSERT INTO complaint_history (complaint_id, actor_role, actor_username, action, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (
            complaint_id,
            actor_role,
            actor_username,
            action,
            note,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    )


def get_history_map(cursor, complaint_ids):
    if not complaint_ids:
        return {}

    placeholders = ",".join("?" for _ in complaint_ids)
    rows = cursor.execute(
        f'''
        SELECT complaint_id, actor_role, actor_username, action, note, created_at
        FROM complaint_history
        WHERE complaint_id IN ({placeholders})
        ORDER BY created_at DESC, id DESC
        ''',
        complaint_ids
    ).fetchall()

    history_map = {complaint_id: [] for complaint_id in complaint_ids}
    for row in rows:
        history_map.setdefault(row['complaint_id'], []).append(dict(row))
    return history_map


def add_note_entry(cursor, complaint_id, author_role, author_username, note_text):
    cursor.execute(
        '''
        INSERT INTO complaint_notes (complaint_id, author_role, author_username, note_text, created_at)
        VALUES (?, ?, ?, ?, ?)
        ''',
        (
            complaint_id,
            author_role,
            author_username,
            note_text,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    )


def get_notes_map(cursor, complaint_ids):
    if not complaint_ids:
        return {}

    placeholders = ",".join("?" for _ in complaint_ids)
    rows = cursor.execute(
        f'''
        SELECT id, complaint_id, author_role, author_username, note_text, created_at
        FROM complaint_notes
        WHERE complaint_id IN ({placeholders})
        ORDER BY created_at DESC, id DESC
        ''',
        complaint_ids
    ).fetchall()

    notes_map = {complaint_id: [] for complaint_id in complaint_ids}
    for row in rows:
        notes_map.setdefault(row['complaint_id'], []).append(dict(row))
    return notes_map


def get_recent_notes(cursor, limit=10):
    rows = cursor.execute(
        '''
        SELECT cn.id, cn.complaint_id, cn.author_role, cn.author_username, cn.note_text, cn.created_at, c.location
        FROM complaint_notes cn
        JOIN complaints c ON c.id = cn.complaint_id
        ORDER BY cn.created_at DESC, cn.id DESC
        LIMIT ?
        ''',
        (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


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
    my_complaints = attach_report_codes(attach_photo_urls([dict(row) for row in cursor.fetchall()]))

    # ВСЕ жалобы города (для карты) - исключаем отклоненные модератором
    cursor.execute("SELECT * FROM complaints WHERE mod_status != 'Rejected'")
    all_complaints = attach_report_codes(attach_photo_urls([dict(row) for row in cursor.fetchall()]))
    conn.close()

    # Статистика
    stats = {
        'total': len(my_complaints),
        'pending': sum(1 for c in my_complaints if get_complaint_category(c) == 'pending'),
        'approved': sum(1 for c in my_complaints if get_complaint_category(c) == 'approved'),
        'rejected': sum(1 for c in my_complaints if get_complaint_category(c) == 'rejected'),
        'resolved': sum(1 for c in my_complaints if c['akim_decision'] == 'Resolved')
    }
    monthly_stats = build_monthly_stats(my_complaints)

    return render_template('citizen.html', username=session['username'], complaints=my_complaints,
                           all_complaints=all_complaints, stats=stats, monthly_stats=monthly_stats)


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
        photo_bytes = photo.read()
        if not photo_bytes:
            flash("Uploaded photo is empty.", "warning")
            return redirect(url_for('home'))
        stored_photo_name, storage_backend = save_photo_bytes(photo_bytes, photo.filename)

        # --- ВОЗВРАЩАЕМ МАГИЮ АНТИДУБЛИКАТА ---
        conn = sqlite3.connect('smart_city.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Достаем все активные проблемы
        cursor.execute(
            "SELECT id, location, description, lat, lng, ai_suggestion, photo_filename, photo_storage_backend FROM complaints WHERE mod_status != 'Rejected' AND akim_decision != 'Resolved'")
        active_issues = [dict(row) for row in cursor.fetchall()]
        conn.close()

        # 2. Формируем список для ИИ
        existing_issues_text = "Currently Active Issues in the City:\n"
        if not active_issues:
            existing_issues_text += "No active issues.\n"
        else:
            for issue in active_issues:
                distance_note = ""
                if lat not in (None, "",) and lng not in (None, "") and issue.get('lat') is not None and issue.get('lng') is not None:
                    issue_distance = distance_meters(lat, lng, issue['lat'], issue['lng'])
                    if issue_distance is not None:
                        distance_note = f" Approximate distance from new point: {int(issue_distance)} meters."
                existing_issues_text += (
                    f"- Issue ID {issue['id']}: Located at '{issue['location']}'. "
                    f"Description: '{issue['description']}'. "
                    f"AI note: '{issue.get('ai_suggestion', '')}'."
                    f"{distance_note}\n"
                )

        heuristic_duplicate = find_likely_duplicate(location, description, lat, lng, active_issues)
        if heuristic_duplicate:
            flash(
                f"A similar report already exists in the system (Report #{heuristic_duplicate['id']}). We will not open a new case because this issue is already under review.",
                "warning")
            return redirect(url_for('home'))

        duplicate_candidates = get_duplicate_candidates(location, description, lat, lng, active_issues)

        # 3. Отправляем умный промпт
        try:
            img = Image.open(io.BytesIO(photo_bytes))
            prompt_parts = []
            prompt_parts.append("You are a Smart City Duplicate Detection AI.")
            prompt_parts.append("A citizen is reporting a NEW issue.")
            prompt_parts.append(f'Location: "{location}"')
            prompt_parts.append(f'Description: "{description}"')
            prompt_parts.append("The first attached image is the NEW report photo.")
            prompt_parts.append(existing_issues_text)

            if duplicate_candidates:
                prompt_parts.append("Candidate existing report photos are attached after the new photo in the same order as below:")
                for idx, issue in enumerate(duplicate_candidates, start=1):
                    distance_note = ""
                    issue_distance = distance_meters(lat, lng, issue.get('lat'), issue.get('lng'))
                    if issue_distance is not None:
                        distance_note = f", distance about {int(issue_distance)} meters"
                    prompt_parts.append(
                        f"{idx}. Report ID {issue['id']}: location '{issue['location']}', description '{issue['description']}'{distance_note}."
                    )
            else:
                prompt_parts.append("No candidate existing report photos are attached.")

            prompt_parts.append("TASK:")
            prompt_parts.append("Compare the NEW issue with the active issues using text, coordinates, landmarks, and especially the attached photos.")
            prompt_parts.append("Treat reports as duplicates even if the text is in another language, the camera angle differs, or the map point is slightly shifted.")
            prompt_parts.append("If the same pile of garbage, same pothole, same broken object, or same scene appears in the new photo and one candidate photo, prefer DUPLICATE.")
            prompt_parts.append('If the NEW issue is highly likely the SAME problem as an active one, respond ONLY with: "DUPLICATE: [ID]".')
            prompt_parts.append('If it is clearly a NEW distinct issue, respond ONLY with: "NEW: [one short professional sentence summarizing the issue]".')
            prompt_parts.append('IMPORTANT: Do not write any other text. Start your response strictly with "NEW:" or "DUPLICATE:".')
            prompt = "\n".join(prompt_parts)

            candidate_images = []
            for issue in duplicate_candidates:
                photo_filename = issue.get('photo_filename')
                if not photo_filename:
                    continue
                candidate_bytes = read_photo_bytes(photo_filename, issue.get('photo_storage_backend') or 'local')
                if not candidate_bytes:
                    continue
                try:
                    candidate_images.append(Image.open(io.BytesIO(candidate_bytes)))
                except Exception:
                    continue

            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, img, *candidate_images]
            )
            ai_response = response.text.strip()
            print(f"ИИ ответил: {ai_response}")  # Это выведется в консоль PyCharm

            # 4. Логика обработки ответа
            if ai_response.startswith("DUPLICATE:"):
                dup_id = ai_response.split(":")[1].strip()
                flash(
                    f"A similar report already exists in the system (Report #{dup_id}). We will not open a new case because this issue is already under review.",
                    "warning")
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
                       INSERT INTO complaints (user_id, location, lat, lng, description, photo_filename, photo_storage_backend, ai_suggestion, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ''', (user_id, location, lat, lng, description, stored_photo_name, storage_backend, ai_category, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
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
    complaints = attach_report_codes(attach_photo_urls([dict(row) for row in cursor.fetchall()]))
    history_by_complaint = get_history_map(cursor, [complaint['id'] for complaint in complaints])
    notes_by_complaint = get_notes_map(cursor, [complaint['id'] for complaint in complaints])
    conn.close()

    # Считаем статистику для красивых карточек
    stats = {
        'pending': sum(1 for c in complaints if c['mod_status'] == 'Pending'),
        'approved': sum(1 for c in complaints if c['mod_status'] == 'Approved'),
        'rejected': sum(1 for c in complaints if c['mod_status'] == 'Rejected')
    }

    active_tab = request.args.get('tab', 'overview')
    report_query = request.args.get('report_query', '').strip()
    if active_tab not in {'overview', 'queue', 'approved', 'rejected', 'returned'}:
        active_tab = 'overview'
    visible_complaints = filter_complaints_by_report_query(complaints, report_query)

    return render_template(
        'moderator.html',
        complaints=visible_complaints,
        history_by_complaint=history_by_complaint,
        notes_by_complaint=notes_by_complaint,
        stats=stats,
        username=session['username'],
        active_tab=active_tab,
        report_query=report_query
    )

@app.route('/mod_action/<int:id>', methods=['POST'])
def mod_action(id):
    if 'user_id' not in session or session.get('role') != 'moderator':
        return redirect(url_for('login'))
    action = request.form.get('mod_action')
    moderator_comment = (request.form.get('moderator_comment') or '').strip()
    reject_reason = (request.form.get('reject_reason') or '').strip()
    active_tab = request.form.get('active_tab', 'overview')
    report_query = request.form.get('report_query', '').strip()
    if active_tab not in {'overview', 'queue', 'approved', 'rejected', 'returned'}:
        active_tab = 'overview'
    if action not in {'Approved', 'Rejected'}:
        flash("Unknown moderation action.", "warning")
        return redirect(url_for('moderator_dashboard', tab=active_tab, report_query=report_query))
    if action == 'Rejected' and not reject_reason:
        flash("Rejecting a report requires a reason.", "warning")
        return redirect(url_for('moderator_dashboard', tab=active_tab, report_query=report_query))
    conn = sqlite3.connect('smart_city.db');
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    stored_reject_reason = reject_reason if action == 'Rejected' else ''
    cursor.execute(
        "UPDATE complaints SET mod_status = ?, moderator_comment = ?, reject_reason = ? WHERE id = ?",
        (action, moderator_comment, stored_reject_reason, id)
    )
    note_parts = []
    if reject_reason:
        note_parts.append(f"Reason: {reject_reason}")
    if moderator_comment:
        note_parts.append(f"Comment: {moderator_comment}")
    add_history_entry(
        cursor,
        id,
        'moderator',
        session['username'],
        f"Set moderator status to {action}",
        " | ".join(note_parts)
    )
    conn.commit();
    conn.close()
    flash(f"Moderator decision saved: {action}.", "success")
    return redirect(url_for('moderator_dashboard', tab=active_tab, report_query=report_query))


@app.route('/complaint_note/<int:id>', methods=['POST'])
def complaint_note(id):
    if 'user_id' not in session or session.get('role') not in {'moderator', 'akim'}:
        return redirect(url_for('login'))

    note_text = (request.form.get('note_text') or '').strip()
    if not note_text:
        flash("Internal note cannot be empty.", "warning")
    else:
        conn = sqlite3.connect('smart_city.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        complaint = cursor.execute("SELECT id FROM complaints WHERE id = ?", (id,)).fetchone()
        if complaint:
            add_note_entry(cursor, id, session['role'], session['username'], note_text)
            add_history_entry(
                cursor,
                id,
                session['role'],
                session['username'],
                "Added internal note",
                note_text
            )
            conn.commit()
            flash("Internal note added.", "success")
        else:
            flash("Complaint not found.", "warning")
        conn.close()

    dashboard = request.form.get('dashboard', session['role'])
    report_query = request.form.get('report_query', '').strip()
    active_tab = request.form.get('active_tab', 'overview' if dashboard == 'moderator' else 'decision')
    if dashboard == 'moderator':
        return redirect(url_for('moderator_dashboard', tab=active_tab, report_query=report_query))
    return redirect(url_for('akim_dashboard', tab=active_tab, report_query=report_query))


@app.route('/mod_citizen_action/<int:id>', methods=['POST'])
def mod_citizen_action(id):
    if 'user_id' not in session or session.get('role') != 'moderator':
        return redirect(url_for('login'))

    citizen_feedback = (request.form.get('citizen_feedback') or '').strip()
    allow_resubmit = 1 if request.form.get('citizen_can_resubmit') == '1' else 0
    active_tab = request.form.get('active_tab', 'overview')
    report_query = request.form.get('report_query', '').strip()
    if active_tab not in {'overview', 'queue', 'approved', 'rejected', 'returned'}:
        active_tab = 'overview'

    if not citizen_feedback:
        flash("Citizen feedback is required.", "warning")
        return redirect(url_for('moderator_dashboard', tab=active_tab, report_query=report_query))

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    citizen_status = 'Action Required' if allow_resubmit else 'Closed'
    cursor.execute(
        '''
        UPDATE complaints
        SET mod_status = 'Rejected',
            akim_decision = 'Waiting for Akimat',
            akim_urgency = 'Unassigned',
            citizen_status = ?,
            citizen_feedback = ?,
            citizen_can_resubmit = ?,
            moderator_comment = '',
            reject_reason = ?
        WHERE id = ?
        ''',
        (citizen_status, citizen_feedback, allow_resubmit, citizen_feedback, id)
    )
    add_history_entry(
        cursor,
        id,
        'moderator',
        session['username'],
        'Sent decision to citizen',
        f"Resubmission allowed: {'Yes' if allow_resubmit else 'No'} | Feedback: {citizen_feedback}"
    )
    conn.commit()
    conn.close()
    flash("Citizen decision sent from moderator workflow.", "success")
    return redirect(url_for('moderator_dashboard', tab=active_tab, report_query=report_query))


@app.route('/akim_citizen_action/<int:id>', methods=['POST'])
def akim_citizen_action(id):
    if 'user_id' not in session or session.get('role') != 'akim':
        return redirect(url_for('login'))

    citizen_feedback = (request.form.get('citizen_feedback') or '').strip()
    allow_resubmit = 1 if request.form.get('citizen_can_resubmit') == '1' else 0
    active_tab = request.form.get('active_tab', 'decision')
    report_query = request.form.get('report_query', '').strip()
    if active_tab not in {'decision', 'urgent', 'report', 'staff'}:
        active_tab = 'decision'

    if not citizen_feedback:
        flash("Citizen feedback is required.", "warning")
        return redirect(url_for('akim_dashboard', tab=active_tab, report_query=report_query))

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    citizen_status = 'Action Required' if allow_resubmit else 'Closed'
    cursor.execute(
        '''
        UPDATE complaints
        SET citizen_status = ?,
            citizen_feedback = ?,
            citizen_can_resubmit = ?
        WHERE id = ?
        ''',
        (citizen_status, citizen_feedback, allow_resubmit, id)
    )
    add_history_entry(
        cursor,
        id,
        'akim',
        session['username'],
        'Sent decision to citizen',
        f"Resubmission allowed: {'Yes' if allow_resubmit else 'No'} | Feedback: {citizen_feedback}"
    )
    conn.commit()
    conn.close()
    flash("Citizen decision sent from Akimat.", "success")
    return redirect(url_for('akim_dashboard', tab=active_tab, report_query=report_query))


@app.route('/resubmit_report/<int:id>', methods=['POST'])
def resubmit_report(id):
    if 'user_id' not in session or session.get('role') != 'citizen':
        return redirect(url_for('login'))

    location = (request.form.get('location') or '').strip()
    description = (request.form.get('description') or '').strip()
    photo = request.files.get('photo')

    if not location or not description:
        flash("Location and description are required to resubmit a report.", "warning")
        return redirect(url_for('home'))

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    complaint = cursor.execute(
        "SELECT * FROM complaints WHERE id = ? AND user_id = ?",
        (id, session['user_id'])
    ).fetchone()

    if not complaint:
        conn.close()
        flash("Report not found.", "warning")
        return redirect(url_for('home'))

    complaint = dict(complaint)
    if not complaint.get('citizen_can_resubmit'):
        conn.close()
        flash("This report cannot be resubmitted.", "warning")
        return redirect(url_for('home'))

    photo_filename = complaint['photo_filename']
    photo_storage_backend = complaint.get('photo_storage_backend') or 'local'
    if photo and photo.filename:
        photo_bytes = photo.read()
        if not photo_bytes:
            conn.close()
            flash("Updated photo is empty.", "warning")
            return redirect(url_for('home'))
        photo_filename, photo_storage_backend = save_photo_bytes(photo_bytes, photo.filename)

    cursor.execute(
        '''
        UPDATE complaints
        SET location = ?,
            description = ?,
            photo_filename = ?,
            photo_storage_backend = ?,
            mod_status = 'Pending',
            akim_urgency = 'Unassigned',
            akim_decision = 'Waiting for Akimat',
            moderator_comment = '',
            reject_reason = '',
            akim_comment = '',
            citizen_status = '',
            citizen_feedback = '',
            citizen_can_resubmit = 0,
            revision_count = COALESCE(revision_count, 0) + 1
        WHERE id = ? AND user_id = ?
        ''',
        (location, description, photo_filename, photo_storage_backend, id, session['user_id'])
    )
    add_history_entry(
        cursor,
        id,
        'citizen',
        session['username'],
        'Resubmitted report',
        f"Location: {location}"
    )
    conn.commit()
    conn.close()
    flash("Report resubmitted for review.", "success")
    return redirect(url_for('home'))


@app.route('/akim')
def akim_dashboard():
    if 'user_id' not in session or session.get('role') != 'akim':
        return redirect(url_for('login'))

    conn = sqlite3.connect('smart_city.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # Аким видит только те жалобы, которые уже одобрил Модератор
    cursor.execute("SELECT * FROM complaints WHERE mod_status = 'Approved' ORDER BY id DESC")
    complaints = attach_report_codes(attach_photo_urls([dict(row) for row in cursor.fetchall()]))
    history_by_complaint = get_history_map(cursor, [complaint['id'] for complaint in complaints])
    notes_by_complaint = get_notes_map(cursor, [complaint['id'] for complaint in complaints])
    recent_notes = get_recent_notes(cursor, limit=12)
    conn.close()

    # Считаем KPI для Акима
    stats = {
        'awaiting': sum(1 for c in complaints if c['akim_decision'] == 'Waiting for Akimat'),
        'urgent': sum(1 for c in complaints if c['akim_urgency'] == 'High' and c['akim_decision'] != 'Resolved'),
        'resolved': sum(1 for c in complaints if c['akim_decision'] == 'Resolved')
    }
    active_tab = request.args.get('tab', 'decision')
    report_query = request.args.get('report_query', '').strip()
    if active_tab not in {'decision', 'urgent', 'report', 'staff'}:
        active_tab = 'decision'
    visible_complaints = filter_complaints_by_report_query(complaints, report_query)

    return render_template(
        'akim.html',
        complaints=visible_complaints,
        history_by_complaint=history_by_complaint,
        notes_by_complaint=notes_by_complaint,
        recent_notes=recent_notes,
        stats=stats,
        username=session['username'],
        active_tab=active_tab,
        report_query=report_query
    )

@app.route('/akim_action/<int:id>', methods=['POST'])
def akim_action(id):
    if 'user_id' not in session or session.get('role') != 'akim':
        return redirect(url_for('login'))
    urgency = request.form.get('urgency');
    decision = request.form.get('decision')
    akim_comment = (request.form.get('akim_comment') or '').strip()
    active_tab = request.form.get('active_tab', 'decision')
    report_query = request.form.get('report_query', '').strip()
    if active_tab not in {'decision', 'urgent', 'report', 'staff'}:
        active_tab = 'decision'
    allowed_decisions = {'Waiting for Akimat', 'Accepted', 'In Progress', 'Resolved', "Won't Fix", 'Needs Clarification'}
    allowed_urgencies = {'Unassigned', 'Low', 'Medium', 'High'}
    if urgency not in allowed_urgencies or decision not in allowed_decisions:
        flash("Invalid Akimat update.", "warning")
        return redirect(url_for('akim_dashboard', tab=active_tab, report_query=report_query))
    if decision in {"Won't Fix", 'Needs Clarification'} and not akim_comment:
        flash(f"{decision} requires an official comment.", "warning")
        return redirect(url_for('akim_dashboard', tab=active_tab, report_query=report_query))
    conn = sqlite3.connect('smart_city.db');
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE complaints SET akim_urgency = ?, akim_decision = ?, akim_comment = ? WHERE id = ?",
        (urgency, decision, akim_comment, id)
    )
    note_parts = [f"Urgency: {urgency}", f"Decision: {decision}"]
    if akim_comment:
        note_parts.append(f"Comment: {akim_comment}")
    add_history_entry(
        cursor,
        id,
        'akim',
        session['username'],
        "Updated Akimat decision",
        " | ".join(note_parts)
    )
    conn.commit();
    conn.close()
    flash(f"Akimat decision saved: {decision}.", "success")
    return redirect(url_for('akim_dashboard', tab=active_tab, report_query=report_query))


if __name__ == '__main__':
    app.run(debug=True)
