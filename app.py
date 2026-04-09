import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, flash
from google import genai
from PIL import Image
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# Let's pull in the environment variables first thing.
# Need that GEMINI_API_KEY to actually do anything cool.
load_dotenv()

app = Flask(__name__)
# Standard stuff for file uploads and session tracking.
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.secret_key = os.getenv("SECRET_KEY", "fallback_dev_key_12345")

# Setting up the AI client. Using gemini-2.0-flash because it's fast and cheap.
# If the key is missing, the app will probably explode later, but we'll try/catch it.
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

def establish_db_connection():
    """A little helper to get us a DB connection without repeating ourselves."""
    try:
        connection = sqlite3.connect('smart_city.db')
        connection.row_factory = sqlite3.Row
        return connection
    except Exception as err:
        print(f"FAILED to connect to the database! Path might be wrong or permissions issues. Error: {err}")
        return None

def setup_app_tables():
    """Runs at startup to make sure we actually have a place to store data."""
    db_conn = establish_db_connection()
    if db_conn is None:
        return
    
    try:
        cursor = db_conn.cursor()
        
        # Creating complaints table. Using bit more space in the formatting so it's readable.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                location TEXT NOT NULL,
                lat REAL,
                lng REAL,
                description TEXT NOT NULL,
                photo_filename TEXT NOT NULL,
                ai_suggestion TEXT,
                mod_status TEXT DEFAULT 'Pending',
                akim_urgency TEXT DEFAULT 'Unassigned',
                akim_decision TEXT DEFAULT 'Waiting for Akimat'
            )
        ''')
        
        # Simple user table for auth.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL
            )
        ''')
        
        db_conn.commit()
    except sqlite3.Error as e:
        print(f"Whoops, couldn't initialize the database tables: {e}")
    finally:
        db_conn.close()

# Kick off the DB setup.
setup_app_tables()

# --------------------------------------------------------------------------
# AUTHENTICATION ROUTES
# --------------------------------------------------------------------------

@app.route('/register', methods=['GET', 'POST'])
def handle_registration():
    if request.method == 'POST':
        user_name = request.form.get('username')
        plain_pass = request.form.get('password')
        default_role = 'citizen' # Everyone starts as a regular Joe.
        
        if not user_name or not plain_pass:
            flash("Look, you gotta fill out both fields.", "warning")
            return redirect(url_for('handle_registration'))

        hashed_val = generate_password_hash(plain_pass)
        
        db = establish_db_connection()
        if not db:
            flash("Server's having a mid-life crisis. Database is down.", "danger")
            return redirect(url_for('handle_registration'))

        try:
            cur = db.cursor()
            cur.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)', 
                        (user_name, hashed_val, default_role))
            db.commit()
            flash("Sweet! Account's ready. Go ahead and log in.", "success")
            return redirect(url_for('do_login'))
        except sqlite3.IntegrityError:
            flash("That username is already taken. Be more unique!", "warning")
        except Exception as generic_err:
            print(f"Registration error: {generic_err}")
            flash("Something went wrong during registration. Try again?", "danger")
        finally:
            db.close()
            
    return render_template('login.html', show_reg=True)

@app.route('/login', methods=['GET', 'POST'])
def do_login():
    if request.method == 'POST':
        u_name = request.form.get('username')
        p_word = request.form.get('password')
        user_type_claim = request.form.get('login_type')

        db = establish_db_connection()
        if not db:
            flash("Connection to the brain (DB) failed.", "danger")
            return redirect(url_for('do_login'))

        try:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM users WHERE username = ?', (u_name,))
            record = cursor.fetchone()
            
            if record and check_password_hash(record['password'], p_word):
                real_role = record['role']
                
                # Check if they are trying to sneak into the wrong portal.
                if user_type_claim == 'staff' and real_role == 'citizen':
                    flash("Nice try, but you aren't staff.", "danger")
                    return redirect(url_for('do_login'))
                if user_type_claim == 'citizen' and real_role != 'citizen':
                    flash("You're a VIP (Staff). Use the Staff login tab.", "danger")
                    return redirect(url_for('do_login'))

                # Set up the session.
                session['user_id'] = record['id']
                session['username'] = record['username']
                session['role'] = real_role

                # Send them to the right place.
                if real_role == 'moderator':
                    return redirect(url_for('view_moderator_panel'))
                elif real_role == 'akim':
                    return redirect(url_for('view_akim_panel'))
                else:
                    return redirect(url_for('show_citizen_portal'))
            else:
                flash("Nope. Wrong username or password.", "danger")
        except Exception as e:
            print(f"Login logic error: {e}")
            flash("An internal error happened. My bad.", "danger")
        finally:
            db.close()
            
    return render_template('login.html')

@app.route('/logout')
def sign_out():
    session.clear()
    return redirect(url_for('do_login'))

# --------------------------------------------------------------------------
# CITIZEN PORTAL
# --------------------------------------------------------------------------

@app.route('/')
def show_citizen_portal():
    if 'user_id' not in session or session.get('role') != 'citizen':
        return redirect(url_for('do_login'))

    db = establish_db_connection()
    if not db:
        return "Critical DB error. Contact the geek squad."

    try:
        cursor = db.cursor()

        # Grab only my personal history of complaining.
        cursor.execute("SELECT * FROM complaints WHERE user_id = ? ORDER BY id DESC", (session['user_id'],))
        my_stuff = [dict(r) for r in cursor.fetchall()]

        # Grab everything that hasn't been trashed by the mods (for the map).
        cursor.execute("SELECT * FROM complaints WHERE mod_status != 'Rejected'")
        all_visible = [dict(r) for r in cursor.fetchall()]
        
        my_stats = {
            'total': len(my_stuff),
            'pending': sum(1 for item in my_stuff if item['mod_status'] == 'Pending'),
            'resolved': sum(1 for item in my_stuff if item['akim_decision'] == 'Resolved')
        }

        return render_template('index.html', 
                               username=session['username'], 
                               complaints=my_stuff,
                               all_complaints=all_visible, 
                               stats=my_stats)
    except Exception as oops:
        print(f"Error loading portal: {oops}")
        return "Internal error loading your portal."
    finally:
        db.close()

@app.route('/submit_issue', methods=['POST'])
def take_complaint():
    # Only citizens should be here.
    if 'user_id' not in session or session.get('role') != 'citizen':
        return redirect(url_for('do_login'))

    # Extracting form data.
    blurb = request.form.get('description')
    place = request.form.get('location', 'Somewhere in the city')
    lat_coord = request.form.get('lat')
    lng_coord = request.form.get('lng')
    img_file = request.files.get('photo')
    current_uid = session['user_id']

    if not img_file:
        flash("We need a photo for proof!", "warning")
        return redirect(url_for('show_citizen_portal'))

    try:
        # Save the file locally first.
        filename_on_disk = img_file.filename
        full_path = os.path.join(app.config['UPLOAD_FOLDER'], filename_on_disk)
        img_file.save(full_path)
    except Exception as fs_err:
        print(f"File save error: {fs_err}")
        flash("Couldn't save the photo. Disc full maybe?", "danger")
        return redirect(url_for('show_citizen_portal'))

    # --- AI DUPLICATE CHECK MAGIC ---
    ai_status_note = "🤖 Processing..."
    
    db_lookup = establish_db_connection()
    active_items = []
    if db_lookup:
        try:
            c = db_lookup.cursor()
            # Fetching details about things that are still being fixed.
            c.execute("SELECT id, location, description FROM complaints WHERE mod_status != 'Rejected' AND akim_decision != 'Resolved'")
            active_items = c.fetchall()
        except Exception as db_e:
            print(f"Error fetching existing issues: {db_e}")
        finally:
            db_lookup.close()

    # Prep the context for the AI.
    history_log = "List of current active problems:\n"
    if not active_items:
        history_log += "- None right now.\n"
    else:
        for item in active_items:
            history_log += f"- #{item['id']} at '{item['location']}': {item['description']}\n"

    try:
        pill_image = Image.open(full_path)
        # Detailed prompt to keep the AI on track.
        system_prompt = f"""
        You are a Smart City Support AI.
        Someone reported this:
        Location: "{place}"
        Description: "{blurb}"
        
        Compare this to the current list:
        {history_log}
        
        If this is the SAME physical issue as an existing one, reply exactly: "DUPLICATE: [ID]".
        If it's new, reply: "NEW: [Short summary]".
        Keep it simple.
        """

        ai_call = ai_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=[system_prompt, pill_image]
        )
        raw_text = ai_call.text.strip()
        print(f"AI suggests: {raw_text}")

        if "DUPLICATE:" in raw_text:
            match_id = raw_text.split(":")[1].strip()
            flash(f"Wait! Looks like someone already reported this (Report #{match_id}). We're on it!", "warning")
            # Cleanup the extra photo file to save space.
            if os.path.exists(full_path):
                os.remove(full_path)
            return redirect(url_for('show_citizen_portal'))
        
        elif "NEW:" in raw_text:
            ai_status_note = raw_text.replace("NEW:", "🤖 AI Info:").strip()
        else:
            ai_status_note = "🤖 AI was a bit confused, manual review needed."

    except Exception as ai_err:
        print(f"Bummer, AI failed: {ai_err}")
        ai_status_note = "🤖 (AI Analysis failed)"

    # --- FINAL DB SAVE ---
    db_save = establish_db_connection()
    if db_save:
        try:
            cursor = db_save.cursor()
            cursor.execute('''
                INSERT INTO complaints (user_id, location, lat, lng, description, photo_filename, ai_suggestion)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (current_uid, place, lat_coord, lng_coord, blurb, filename_on_disk, ai_status_note))
            db_save.commit()
            flash("Report filed! We'll look into it ASAP.", "success")
        except Exception as final_err:
            print(f"Database save error: {final_err}")
            flash("Database hiccup. Your report might not have saved.", "danger")
        finally:
            db_save.close()

    return redirect(url_for('show_citizen_portal'))

# --------------------------------------------------------------------------
# MODERATOR PANEL
# --------------------------------------------------------------------------

@app.route('/moderator')
def view_moderator_panel():
    if 'user_id' not in session or session.get('role') != 'moderator':
        return redirect(url_for('do_login'))

    conn = establish_db_connection()
    if not conn:
        return "Internal Error: Can't reach DB."
        
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM complaints ORDER BY id DESC")
        data = [dict(row) for row in cur.fetchall()]
        
        summary_stats = {
            'pending': sum(1 for x in data if x['mod_status'] == 'Pending'),
            'approved': sum(1 for x in data if x['mod_status'] == 'Approved'),
            'rejected': sum(1 for x in data if x['mod_status'] == 'Rejected')
        }

        return render_template('moderator.html', complaints=data, stats=summary_stats, username=session['username'])
    except Exception as e:
        print(f"Mod dashboard fetch error: {e}")
        return "Critical error loading moderator panel."
    finally:
        conn.close()

@app.route('/apply_mod_decision/<int:report_id>', methods=['POST'])
def submit_mod_action(report_id):
    if session.get('role') != 'moderator':
        return "Access denied.", 403
        
    new_status = request.form.get('mod_action')
    
    db = establish_db_connection()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("UPDATE complaints SET mod_status = ? WHERE id = ?", (new_status, report_id))
            db.commit()
        except Exception as e:
            print(f"Update failed for report {report_id}: {e}")
        finally:
            db.close()
            
    return redirect(url_for('view_moderator_panel'))

# --------------------------------------------------------------------------
# AKIM PANEL (The Decision Maker)
# --------------------------------------------------------------------------

@app.route('/akim')
def view_akim_panel():
    if 'user_id' not in session or session.get('role') != 'akim':
        return redirect(url_for('do_login'))

    conn = establish_db_connection()
    if not conn:
        return "DB Error."

    try:
        cursor = conn.cursor()
        # Only show the ones the moderator actually liked.
        cursor.execute("SELECT * FROM complaints WHERE mod_status = 'Approved' ORDER BY id DESC")
        approved_reports = [dict(row) for row in cursor.fetchall()]
        
        akim_stats = {
            'awaiting': sum(1 for r in approved_reports if r['akim_decision'] == 'Waiting for Akimat'),
            'urgent': sum(1 for r in approved_reports if r['akim_urgency'] == 'High' and r['akim_decision'] != 'Resolved'),
            'resolved': sum(1 for r in approved_reports if r['akim_decision'] == 'Resolved')
        }

        return render_template('akim.html', complaints=approved_reports, stats=akim_stats, username=session['username'])
    except Exception as e:
        print(f"Akim panel error: {e}")
        return "Error loading Akim portal."
    finally:
        conn.close()

@app.route('/submit_akim_decision/<int:report_id>', methods=['POST'])
def apply_akim_updates(report_id):
    if session.get('role') != 'akim':
        return "Forbidden", 403
        
    urgency_level = request.form.get('urgency')
    final_choice = request.form.get('decision')
    
    db = establish_db_connection()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("UPDATE complaints SET akim_urgency = ?, akim_decision = ? WHERE id = ?", 
                           (urgency_level, final_choice, report_id))
            db.commit()
        except Exception as e:
            print(f"Akim update failed for {report_id}: {e}")
        finally:
            db.close()
            
    return redirect(url_for('view_akim_panel'))

# --------------------------------------------------------------------------
# STARTUP
# --------------------------------------------------------------------------

if __name__ == '__main__':
    # Running it locally. Debug is on for development.
    # Make sure static/uploads exists or the app will crash on first upload.
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=True)