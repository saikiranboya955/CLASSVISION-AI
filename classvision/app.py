from multiprocessing.reduction import duplicate
import os
import sqlite3
import uuid
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta
import functools
import logging
import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from .services.face_service import face_service
from .services.voice_service import voice_service

app = Flask(__name__)
app.secret_key = 'classvision-ai-secret-key-2026'
bcrypt = Bcrypt(app)

# Database path configuration
DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'classvision.db')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(PROJECT_ROOT, 'StudentDetails', 'studentdetails.csv')
TRAIN_IMAGE_PATH = os.path.join(PROJECT_ROOT, 'TrainingImage')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def log_faculty_activity(faculty_id, action_type, description):
    """Utility to record faculty activity audit trail"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO faculty_activity_log (faculty_id, action_type, description)
            VALUES (?, ?, ?)
        ''', (faculty_id, action_type, description))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging faculty activity: {e}")


def sync_database_and_files():
    """
    Automated startup integrity check & auto-repair service.
    Synchronizes SQLite `student` table with `studentdetails.csv`
    and audits `TrainingImage/` directory to update `face_registered` flags.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Fetch all students
        cursor.execute("SELECT id, name, roll_number FROM student")
        db_students = cursor.fetchall()

        # 1. Synchronize StudentDetails CSV
        os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
        csv_rows = []
        for s in db_students:
            csv_rows.append({'Enrollment': s['id'], 'Name': s['name']})

        df_sync = pd.DataFrame(csv_rows if csv_rows else [], columns=['Enrollment', 'Name'])
        df_sync.to_csv(CSV_PATH, index=False)

        # 2. Audit face dataset directories & update face_registered flags
        for s in db_students:
            s_id = s['id']
            student_dir = os.path.join(TRAIN_IMAGE_PATH, f"student_{s_id}")
            has_images = False

            if os.path.exists(student_dir):
                valid_files = [f for f in os.listdir(student_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
                if len(valid_files) >= 5:  # Requires at least 5 dataset images
                    has_images = True

            cursor.execute("UPDATE student SET face_registered = ? WHERE id = ?", (1 if has_images else 0, s_id))

        conn.commit()
        conn.close()

        # Reload in-memory student lookup table
        face_service.load_student_data()
    except Exception as e:
        logger.error(f"Error in sync_database_and_files: {e}")


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # Faculty table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faculty (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            faculty_id TEXT UNIQUE NOT NULL,
            department TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Student table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS student (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            roll_number TEXT UNIQUE NOT NULL,
            branch TEXT NOT NULL,
            section TEXT NOT NULL,
            face_registered BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # AttendanceSession table with extended fields
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance_session (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            branch TEXT NOT NULL,
            section TEXT NOT NULL,
            date TEXT NOT NULL,
            faculty_id INTEGER NOT NULL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            is_locked BOOLEAN DEFAULT 0,
            total_roster_count INTEGER DEFAULT 0,
            present_count INTEGER DEFAULT 0,
            absent_count INTEGER DEFAULT 0,
            finalized_at TIMESTAMP,
            FOREIGN KEY (faculty_id) REFERENCES faculty(id)
        )
    ''')

    # Migration: Add missing columns to attendance_session if existing database schema lacks them
    session_columns = [row[1] for row in cursor.execute("PRAGMA table_info(attendance_session)").fetchall()]
    if 'is_locked' not in session_columns:
        cursor.execute("ALTER TABLE attendance_session ADD COLUMN is_locked BOOLEAN DEFAULT 0")
    if 'total_roster_count' not in session_columns:
        cursor.execute("ALTER TABLE attendance_session ADD COLUMN total_roster_count INTEGER DEFAULT 0")
    if 'present_count' not in session_columns:
        cursor.execute("ALTER TABLE attendance_session ADD COLUMN present_count INTEGER DEFAULT 0")
    if 'absent_count' not in session_columns:
        cursor.execute("ALTER TABLE attendance_session ADD COLUMN absent_count INTEGER DEFAULT 0")
    if 'finalized_at' not in session_columns:
        cursor.execute("ALTER TABLE attendance_session ADD COLUMN finalized_at TIMESTAMP")

    # AttendanceRecord table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance_record (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            check_in_time TIMESTAMP,
            recognition_method TEXT DEFAULT 'face',
            manually_edited BOOLEAN DEFAULT 0,
            updated_by INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES attendance_session(id),
            FOREIGN KEY (student_id) REFERENCES student(id),
            FOREIGN KEY (updated_by) REFERENCES faculty(id),
            UNIQUE(session_id, student_id)
        )
    ''')

    # Attendance Audit Log table (Manual Edit History)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            faculty_id INTEGER NOT NULL,
            faculty_name TEXT NOT NULL,
            previous_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES attendance_session(id),
            FOREIGN KEY (student_id) REFERENCES student(id),
            FOREIGN KEY (faculty_id) REFERENCES faculty(id)
        )
    ''')

    # Faculty Activity Log table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faculty_activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (faculty_id) REFERENCES faculty(id)
        )
    ''')

    # QR Session Tokens table (QR Backup Attendance)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS qr_session_token (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES attendance_session(id)
        )
    ''')

    # QR Attendance Requests table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS qr_attendance_request (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            token TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES attendance_session(id),
            FOREIGN KEY (student_id) REFERENCES student(id)
        )
    ''')

    conn.commit()
    conn.close()

    # Run auto-sync repair
    sync_database_and_files()


# Initialize database and sync files on startup
init_db()


# Authentication decorator
def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if 'faculty_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# Routes
@app.route('/')
def index():
    if 'faculty_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/students')
@login_required
def students():
    conn = get_db()
    try:
        cursor = conn.cursor()

        search = request.args.get('search', '')
        branch_filter = request.args.get('branch', '')
        section_filter = request.args.get('section', '')

        query = 'SELECT * FROM student WHERE 1=1'
        params = []

        if search:
            query += ' AND (name LIKE ? OR roll_number LIKE ?)'
            params.extend([f'%{search}%', f'%{search}%'])

        if branch_filter:
            query += ' AND branch = ?'
            params.append(branch_filter)

        if section_filter:
            query += ' AND section = ?'
            params.append(section_filter)

        query += ' ORDER BY created_at DESC'

        cursor.execute(query, params)
        students_list = cursor.fetchall()

        cursor.execute('SELECT DISTINCT branch FROM student')
        branches = [row[0] for row in cursor.fetchall()]

        cursor.execute('SELECT DISTINCT section FROM student')
        sections = [row[0] for row in cursor.fetchall()]

        return render_template('students/index.html',
                               students=students_list,
                               branches=branches,
                               sections=sections,
                               search=search,
                               branch_filter=branch_filter,
                               section_filter=section_filter)
    finally:
        conn.close()


@app.route('/students/<int:student_id>/photo')
def student_photo(student_id):
    """Serve the first valid dataset photo of the student from TrainingImage"""
    st_dir = os.path.join(PROJECT_ROOT, 'TrainingImage', f"student_{student_id}")
    if os.path.exists(st_dir):
        files = sorted([f for f in os.listdir(st_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
        if files:
            return send_file(os.path.join(st_dir, files[0]), mimetype='image/jpeg')
    return send_file(io.BytesIO(b""), mimetype='image/png')  # empty fallback handled in template


@app.route('/students/<int:student_id>')
@login_required
def student_profile(student_id):
    """Detailed Student Profile Page with Finalized Session Attendance, Subject Breakdown, Monthly Trends, Analytics, and Audit History"""
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM student WHERE id = ?', (student_id,))
        student = cursor.fetchone()

        if not student:
            flash('Student not found', 'error')
            return redirect(url_for('students'))

        # Check if student has dataset image
        st_dir = os.path.join(PROJECT_ROOT, 'TrainingImage', f"student_{student_id}")
        has_photo = os.path.exists(st_dir) and len([f for f in os.listdir(st_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]) > 0

        # Get filter parameters
        subject_filter = request.args.get('subject', '').strip()
        month_filter = request.args.get('month', '').strip()
        status_filter = request.args.get('status', '').strip()

        # 1. Total Conducted Finalized Sessions applicable to this student's branch & section
        cursor.execute('''
            SELECT COUNT(*) FROM attendance_session
            WHERE branch = ? AND section = ? AND is_locked = 1
        ''', (student['branch'], student['section']))
        total_conducted = cursor.fetchone()[0]

        # 2. Total Present & Absent across finalized sessions
        cursor.execute('''
            SELECT 
                COUNT(CASE WHEN ar.status = 'Present' THEN 1 END) as present_cnt,
                COUNT(CASE WHEN ar.status = 'Absent' THEN 1 END) as absent_cnt
            FROM attendance_record ar
            JOIN attendance_session s ON ar.session_id = s.id
            WHERE ar.student_id = ? AND s.is_locked = 1
        ''', (student_id,))
        stats_row = cursor.fetchone()
        present_count = stats_row['present_cnt'] if stats_row else 0
        absent_count = stats_row['absent_cnt'] if stats_row else 0

        # Calculate overall attendance % from finalized sessions
        overall_percentage = round((present_count / total_conducted * 100), 2) if total_conducted > 0 else 0.0

        # Status badge determination
        if overall_percentage >= 75.0:
            eligibility_status = "Eligible"
            eligibility_class = "status-present"
        elif overall_percentage >= 65.0:
            eligibility_status = "Warning"
            eligibility_class = "status-warning"
        else:
            eligibility_status = "Short Attendance"
            eligibility_class = "status-absent"

        # Last Attendance Date & Status
        cursor.execute('''
            SELECT s.date, ar.status
            FROM attendance_record ar
            JOIN attendance_session s ON ar.session_id = s.id
            WHERE ar.student_id = ? AND s.is_locked = 1
            ORDER BY s.date DESC, s.id DESC
            LIMIT 1
        ''', (student_id,))
        last_rec = cursor.fetchone()
        last_date = last_rec['date'] if last_rec else '—'
        last_status = last_rec['status'] if last_rec else '—'

        # 3. Subject-Wise Attendance Breakdown (Only subjects where attendance records exist for THIS student)
        cursor.execute('''
            SELECT s.subject,
                   COUNT(DISTINCT s.id) as conducted,
                   COUNT(CASE WHEN ar.status = 'Present' THEN 1 END) as present,
                   COUNT(CASE WHEN ar.status = 'Absent' THEN 1 END) as absent
            FROM attendance_session s
            JOIN attendance_record ar ON s.id = ar.session_id AND ar.student_id = ?
            WHERE s.branch = ? AND s.section = ? AND s.is_locked = 1
            GROUP BY s.subject
        ''', (student_id, student['branch'], student['section']))
        subject_rows = cursor.fetchall()
        subject_stats = []
        for sr in subject_rows:
            subj_conducted = sr['conducted']
            subj_present = sr['present']
            subj_absent = sr['absent']
            subj_pct = round((subj_present / subj_conducted * 100), 2) if subj_conducted > 0 else 0.0
            
            if subj_pct >= 75.0:
                s_badge = "Eligible"
                s_class = "status-present"
            elif subj_pct >= 65.0:
                s_badge = "Warning"
                s_class = "status-warning"
            else:
                s_badge = "Short Attendance"
                s_class = "status-absent"

            subject_stats.append({
                'subject': sr['subject'],
                'conducted': subj_conducted,
                'present': subj_present,
                'absent': subj_absent,
                'percentage': subj_pct,
                'status_badge': s_badge,
                'status_class': s_class
            })

        # 4. Monthly Attendance Breakdown (Only Finalized Sessions with records for THIS student)
        cursor.execute('''
            SELECT strftime('%Y-%m', s.date) as m_month,
                   COUNT(DISTINCT s.id) as conducted,
                   COUNT(CASE WHEN ar.status = 'Present' THEN 1 END) as present
            FROM attendance_session s
            JOIN attendance_record ar ON s.id = ar.session_id AND ar.student_id = ?
            WHERE s.branch = ? AND s.section = ? AND s.is_locked = 1
            GROUP BY m_month
            ORDER BY m_month ASC
        ''', (student_id, student['branch'], student['section']))
        monthly_rows = cursor.fetchall()
        monthly_stats = []
        for mr in monthly_rows:
            m_cond = mr['conducted']
            m_pres = mr['present']
            m_pct = round((m_pres / m_cond * 100), 2) if m_cond > 0 else 0.0
            monthly_stats.append({
                'month': mr['m_month'],
                'conducted': m_cond,
                'present': m_pres,
                'percentage': m_pct
            })

        # 5. Complete Attendance History Table (with filters)
        history_query = '''
            SELECT ar.*, s.id as session_id, s.subject, s.branch, s.section, s.date, s.started_at, f.name as faculty_name
            FROM attendance_record ar
            JOIN attendance_session s ON ar.session_id = s.id
            JOIN faculty f ON s.faculty_id = f.id
            WHERE ar.student_id = ? AND s.is_locked = 1
        '''
        history_params = [student_id]

        if subject_filter:
            history_query += ' AND s.subject = ?'
            history_params.append(subject_filter)
        if month_filter:
            history_query += ' AND strftime("%Y-%m", s.date) = ?'
            history_params.append(month_filter)
        if status_filter:
            history_query += ' AND ar.status = ?'
            history_params.append(status_filter)

        history_query += ' ORDER BY s.date DESC, s.started_at DESC'
        cursor.execute(history_query, history_params)
        records = cursor.fetchall()

        # 6. Audit History Logs for this student
        cursor.execute('''
            SELECT al.*, s.subject, s.date
            FROM attendance_audit_log al
            JOIN attendance_session s ON al.session_id = s.id
            WHERE al.student_id = ?
            ORDER BY al.created_at DESC
        ''', (student_id,))
        audit_logs = cursor.fetchall()

        # Distinct subjects for filter dropdown
        cursor.execute('''
            SELECT DISTINCT s.subject FROM attendance_session s
            WHERE s.branch = ? AND s.section = ? AND s.is_locked = 1
        ''', (student['branch'], student['section']))
        available_subjects = [row[0] for row in cursor.fetchall()]

        return render_template('students/profile.html',
                               student=student,
                               has_photo=has_photo,
                               records=records,
                               total_conducted=total_conducted,
                               present_count=present_count,
                               absent_count=absent_count,
                               overall_percentage=overall_percentage,
                               eligibility_status=eligibility_status,
                               eligibility_class=eligibility_class,
                               last_date=last_date,
                               last_status=last_status,
                               subject_stats=subject_stats,
                               monthly_stats=monthly_stats,
                               audit_logs=audit_logs,
                               available_subjects=available_subjects,
                               subject_filter=subject_filter,
                               month_filter=month_filter,
                               status_filter=status_filter)
    finally:
        conn.close()


@app.route('/students/<int:student_id>/export/<string:fmt>')
@login_required
def export_student_attendance(student_id, fmt):
    """Export individual student attendance report as CSV or Excel"""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM student WHERE id = ?', (student_id,))
        student = cursor.fetchone()

        if not student:
            flash('Student not found', 'error')
            return redirect(url_for('students'))

        cursor.execute('''
            SELECT s.date, s.subject, f.name as faculty_name, ar.status, ar.check_in_time, ar.recognition_method, s.id as session_id
            FROM attendance_record ar
            JOIN attendance_session s ON ar.session_id = s.id
            JOIN faculty f ON s.faculty_id = f.id
            WHERE ar.student_id = ? AND s.is_locked = 1
            ORDER BY s.date DESC
        ''', (student_id,))
        rows = cursor.fetchall()

        data = []
        for r in rows:
            data.append({
                'Date': r['date'],
                'Subject': r['subject'],
                'Faculty': r['faculty_name'],
                'Status': r['status'],
                'Check-in Time': r['check_in_time'] or 'N/A',
                'Method': r['recognition_method'] or 'face',
                'Session ID': f"#{r['session_id']}"
            })

        import pandas as pd
        import io
        df = pd.DataFrame(data)

        if fmt.lower() == 'csv':
            output = io.StringIO()
            df.to_csv(output, index=False)
            response = make_response(output.getvalue())
            response.headers["Content-Disposition"] = f"attachment; filename=attendance_{student['roll_number']}.csv"
            response.headers["Content-type"] = "text/csv"
            return response
        elif fmt.lower() in ['excel', 'xlsx']:
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Attendance History')
            response = make_response(output.getvalue())
            response.headers["Content-Disposition"] = f"attachment; filename=attendance_{student['roll_number']}.xlsx"
            response.headers["Content-type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            return response
        else:
            flash('Unsupported export format', 'error')
            return redirect(url_for('student_profile', student_id=student_id))
    finally:
        conn.close()


@app.route('/students/register', methods=['GET', 'POST'])
@login_required
def register_student():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        roll_number = request.form.get('roll_number', '').strip()
        branch = request.form.get('branch', '').strip()
        section = request.form.get('section', '').strip()

        form_data = {
            'name': name,
            'roll_number': roll_number,
            'branch': branch,
            'section': section
        }

        if not all([name, roll_number, branch, section]):
            flash('All fields are required', 'error')
            return render_template('students/register.html', **form_data)

        conn = get_db()
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT id FROM student WHERE roll_number = ?', (roll_number,))
            if cursor.fetchone():
                flash(f'Roll number "{roll_number}" is already registered.', 'error')
                return render_template('students/register.html', **form_data)

            cursor.execute(
                'INSERT INTO student (name, roll_number, branch, section) VALUES (?, ?, ?, ?)',
                (name, roll_number, branch, section)
            )
            conn.commit()
            student_id = cursor.lastrowid
            conn.close()

            # Sync with CSV and face service memory
            sync_database_and_files()
            log_faculty_activity(session['faculty_id'], 'REGISTER_STUDENT', f"Registered student {name} ({roll_number})")

            flash(f'Student {name} registered successfully! Proceed with face capture.', 'success')
            return redirect(url_for('capture_face', student_id=student_id))
        except Exception as e:
            logger.exception("Student registration failed")
            flash('Registration failed. Please check inputs and try again.', 'error')
            return render_template('students/register.html', **form_data)
        finally:
            conn.close()

    return render_template('students/register.html')


@app.route('/students/<int:student_id>/capture-face', methods=['GET', 'POST'])
@login_required
def capture_face(student_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM student WHERE id = ?', (student_id,))
        student = cursor.fetchone()

        if not student:
            flash('Student not found', 'error')
            return redirect(url_for('students'))

        return render_template('students/capture_face.html', student=student)
    finally:
        conn.close()


@app.route('/students/<int:student_id>/train-face', methods=['POST'])
@login_required
def train_face(student_id):
    try:
        face_service.train_model()
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('UPDATE student SET face_registered = 1 WHERE id = ?', (student_id,))
            conn.commit()
        finally:
            conn.close()

        face_service.load_student_data()
        voice_service.announce_training_success()
        log_faculty_activity(session['faculty_id'], 'TRAIN_MODEL', f"Trained LBPH model for student ID #{student_id}")

        flash('Face recognition model trained successfully!', 'success')
        return jsonify({'success': True, 'redirect': url_for('students')})
    except Exception as e:
        logger.exception("Model training failed")
        voice_service.announce_error("Training failed")
        return jsonify({'success': False, 'message': f'Training failed: {str(e)}'}), 400


@app.route('/attendance')
@login_required
def attendance():
    conn = get_db()
    try:
        cursor = conn.cursor()

        date_filter = request.args.get('date', '')
        branch_filter = request.args.get('branch', '')
        section_filter = request.args.get('section', '')
        subject_filter = request.args.get('subject', '')

        query = '''
            SELECT s.id, s.subject, s.branch, s.section, s.date, s.started_at, s.is_locked,
                   COUNT(CASE WHEN ar.status = 'Present' THEN 1 END) as present_count,
                   COUNT(CASE WHEN ar.status = 'Absent' THEN 1 END) as absent_count,
                   f.name as faculty_name
            FROM attendance_session s
            LEFT JOIN attendance_record ar ON s.id = ar.session_id
            JOIN faculty f ON s.faculty_id = f.id
            WHERE 1=1
        '''
        params = []

        if date_filter:
            query += ' AND s.date = ?'
            params.append(date_filter)

        if branch_filter:
            query += ' AND s.branch = ?'
            params.append(branch_filter)

        if section_filter:
            query += ' AND s.section = ?'
            params.append(section_filter)

        if subject_filter:
            query += ' AND s.subject LIKE ?'
            params.append(f'%{subject_filter}%')

        query += ' GROUP BY s.id ORDER BY s.started_at DESC'

        cursor.execute(query, params)
        sessions = cursor.fetchall()

        return render_template('attendance/index.html',
                               sessions=sessions,
                               date_filter=date_filter,
                               branch_filter=branch_filter,
                               section_filter=section_filter,
                               subject_filter=subject_filter)
    finally:
        conn.close()


@app.route('/attendance/<int:session_id>')
@login_required
def attendance_detail(session_id):
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('''
            SELECT s.*, f.name as faculty_name
            FROM attendance_session s
            JOIN faculty f ON s.faculty_id = f.id
            WHERE s.id = ?
        ''', (session_id,))
        session_data = cursor.fetchone()

        if not session_data:
            flash('Session not found', 'error')
            return redirect(url_for('attendance'))

        cursor.execute('''
            SELECT ar.*, st.name, st.roll_number, st.branch, st.section
            FROM attendance_record ar
            JOIN student st ON ar.student_id = st.id
            WHERE ar.session_id = ?
            ORDER BY st.roll_number
        ''', (session_id,))
        records = cursor.fetchall()

        # Fetch audit history for this session
        cursor.execute('''
            SELECT al.*, st.name as student_name, st.roll_number
            FROM attendance_audit_log al
            JOIN student st ON al.student_id = st.id
            WHERE al.session_id = ?
            ORDER BY al.created_at DESC
        ''', (session_id,))
        audit_logs = cursor.fetchall()

        # Calculate stats
        total = len(records)
        present = sum(1 for r in records if r['status'] == 'Present')
        absent = total - present
        percentage = round((present / total * 100)) if total > 0 else 0

        # Check for active QR token
        cursor.execute('SELECT token FROM qr_session_token WHERE session_id = ? AND is_active = 1', (session_id,))
        qr_row = cursor.fetchone()
        qr_token = qr_row['token'] if qr_row else None

        # Fetch pending QR backup requests
        cursor.execute('''
            SELECT qr.*, st.name, st.roll_number
            FROM qr_attendance_request qr
            JOIN student st ON qr.student_id = st.id
            WHERE qr.session_id = ? AND qr.status = 'PENDING'
        ''', (session_id,))
        qr_requests = cursor.fetchall()

        return render_template('attendance/detail.html',
                               attendance_session=session_data,
                               records=records,
                               total=total,
                               present=present,
                               absent=absent,
                               percentage=percentage,
                               audit_logs=audit_logs,
                               qr_token=qr_token,
                               qr_requests=qr_requests)
    finally:
        conn.close()


@app.route('/attendance/<int:session_id>/submit', methods=['POST'])
@login_required
def submit_attendance_session(session_id):
    """
    Finalize Attendance Session:
    1. Compares class roster (students in branch & section) vs recorded present students.
    2. Automatically marks all un-detected students as ABSENT.
    3. Finalizes counts, sets `is_locked = 1`, and records timestamp.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM attendance_session WHERE id = ?', (session_id,))
        session_data = cursor.fetchone()

        if not session_data:
            flash('Attendance session not found', 'error')
            return redirect(url_for('attendance'))

        if session_data['is_locked']:
            flash('Attendance session is already finalized and locked.', 'error')
            return redirect(url_for('attendance_detail', session_id=session_id))

        # Get all registered students belonging to class branch & section
        cursor.execute('SELECT id, name, roll_number FROM student WHERE branch = ? AND section = ?',
                       (session_data['branch'], session_data['section']))
        roster = cursor.fetchall()

        # Get present student IDs already marked in attendance_record
        cursor.execute('SELECT student_id FROM attendance_record WHERE session_id = ? AND status = "Present"', (session_id,))
        present_ids = set(row[0] for row in cursor.fetchall())

        absent_count = 0
        now_time = datetime.now().strftime('%H:%M:%S')

        # Insert ABSENT record for every un-detected roster student
        for student in roster:
            st_id = student['id']
            if st_id not in present_ids:
                # Insert or ignore absent record
                cursor.execute('''
                    INSERT OR IGNORE INTO attendance_record (session_id, student_id, status, check_in_time, recognition_method)
                    VALUES (?, ?, 'Absent', ?, 'auto_system')
                ''', (session_id, st_id, now_time))
                absent_count += 1

        total_roster = len(roster)
        present_count = len(present_ids)

        # Update attendance_session status to locked
        cursor.execute('''
            UPDATE attendance_session 
            SET is_locked = 1, total_roster_count = ?, present_count = ?, absent_count = ?, finalized_at = CURRENT_TIMESTAMP, ended_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (total_roster, present_count, absent_count, session_id))

        conn.commit()

        # Voice announcement & Audit logging
        voice_service.announce_session_complete()
        log_faculty_activity(
            session['faculty_id'],
            'SUBMIT_ATTENDANCE',
            f"Finalized Session #{session_id} ({session_data['subject']}): {present_count} Present, {absent_count} Auto-Marked Absent out of {total_roster} total students."
        )

        flash(f"Session Finalized & Locked! {present_count} Present, {absent_count} marked Absent out of {total_roster} students.", "success")
    except Exception as e:
        logger.exception("Error finalizing attendance session")
        flash("Failed to finalize attendance session", "error")
    finally:
        conn.close()

    return redirect(url_for('attendance_detail', session_id=session_id))


@app.route('/attendance/<int:session_id>/edit/<int:record_id>', methods=['POST'])
@login_required
def edit_attendance(session_id, record_id):
    """
    Manual Attendance Override with Audit Reasons and Logging.
    """
    new_status = request.form.get('status', '').strip()
    reason = request.form.get('reason', '').strip() or "Manual Correction"

    if new_status not in ['Present', 'Absent']:
        flash('Invalid status selection', 'error')
        return redirect(url_for('attendance_detail', session_id=session_id))

    conn = get_db()
    try:
        cursor = conn.cursor()

        # Fetch current record
        cursor.execute('SELECT * FROM attendance_record WHERE id = ?', (record_id,))
        record = cursor.fetchone()

        if not record:
            flash('Attendance record not found', 'error')
            return redirect(url_for('attendance_detail', session_id=session_id))

        previous_status = record['status']

        if previous_status != new_status:
            # Update record
            cursor.execute('''
                UPDATE attendance_record 
                SET status = ?, manually_edited = 1, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (new_status, session['faculty_id'], record_id))

            # Insert Audit Log entry
            cursor.execute('''
                INSERT INTO attendance_audit_log (session_id, student_id, faculty_id, faculty_name, previous_status, new_status, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (session_id, record['student_id'], session['faculty_id'], session.get('faculty_name', 'Faculty'), previous_status, new_status, reason))

            # Recalculate session statistics
            cursor.execute('SELECT COUNT(*) FROM attendance_record WHERE session_id = ? AND status = "Present"', (session_id,))
            p_cnt = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM attendance_record WHERE session_id = ? AND status = "Absent"', (session_id,))
            a_cnt = cursor.fetchone()[0]

            cursor.execute('UPDATE attendance_session SET present_count = ?, absent_count = ? WHERE id = ?', (p_cnt, a_cnt, session_id))

            conn.commit()

            log_faculty_activity(
                session['faculty_id'],
                'MANUAL_EDIT',
                f"Updated attendance for student ID #{record['student_id']} in Session #{session_id} from {previous_status} -> {new_status} (Reason: {reason})"
            )
            flash(f"Attendance status updated to '{new_status}' (Audit logged).", 'success')
        else:
            flash('Status unchanged.', 'info')
    except Exception as e:
        logger.exception("Error editing attendance record")
        flash('Failed to update attendance record', 'error')
    finally:
        conn.close()

    return redirect(url_for('attendance_detail', session_id=session_id))


@app.route('/take-attendance', methods=['GET', 'POST'])
@login_required
def take_attendance():
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        branch = request.form.get('branch', '').strip()
        section = request.form.get('section', '').strip()
        method = request.form.get('method', 'face').strip()

        if not all([subject, branch, section]):
            flash('Subject, Branch, and Section are required', 'error')
            return render_template('attendance/take.html', subject=subject, branch=branch, section=section)

        conn = get_db()
        try:
            cursor = conn.cursor()
            today = datetime.now().strftime('%Y-%m-%d')

            # Prevent duplicate session creation for same Subject, Branch, Section, Faculty, Date
            cursor.execute('''
                SELECT id, is_locked FROM attendance_session
                WHERE subject = ? AND branch = ? AND section = ? AND faculty_id = ? AND date = ?
                ORDER BY id DESC LIMIT 1
            ''', (subject, branch, section, session['faculty_id'], today))
            existing_session = cursor.fetchone()

            if existing_session:
                existing_id = existing_session['id']
                flash(f"Attendance session already exists for this class today (Session #{existing_id}). Resuming session.", "info")
                if method == 'qr':
                    return redirect(url_for('qr_attendance_session', session_id=existing_id))
                else:
                    if existing_session['is_locked']:
                        flash("Session is finalized and locked.", "info")
                        return redirect(url_for('attendance_detail', session_id=existing_id))
                    return redirect(url_for('face_recognition_attendance', session_id=existing_id))

            # Calculate total class roster count
            cursor.execute('SELECT COUNT(*) FROM student WHERE branch = ? AND section = ?', (branch, section))
            roster_cnt = cursor.fetchone()[0]

            cursor.execute('''
                INSERT INTO attendance_session (subject, branch, section, date, faculty_id, total_roster_count)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (subject, branch, section, today, session['faculty_id'], roster_cnt))
            session_id = cursor.lastrowid

            if method == 'qr':
                token = str(uuid.uuid4())[:8].upper()
                cursor.execute('INSERT INTO qr_session_token (session_id, token) VALUES (?, ?)', (session_id, token))
                conn.commit()
                log_faculty_activity(session['faculty_id'], 'CREATE_SESSION', f"Started QR attendance session #{session_id} ({subject} - {branch} {section})")
                return redirect(url_for('qr_attendance_session', session_id=session_id))
            else:
                conn.commit()
                log_faculty_activity(session['faculty_id'], 'CREATE_SESSION', f"Started AI Face attendance session #{session_id} ({subject} - {branch} {section})")
                return redirect(url_for('face_recognition_attendance', session_id=session_id))
        finally:
            conn.close()

    return render_template('attendance/take.html')


@app.route('/qr-attendance/<int:session_id>')
@login_required
def qr_attendance_session(session_id):
    """Dedicated QR Attendance Session Page with Live QR Code & Manual Code Entry"""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM attendance_session WHERE id = ?', (session_id,))
        session_data = cursor.fetchone()

        if not session_data:
            flash('Attendance session not found', 'error')
            return redirect(url_for('take_attendance'))

        # Fetch active QR token
        cursor.execute('SELECT token FROM qr_session_token WHERE session_id = ? AND is_active = 1', (session_id,))
        qr_row = cursor.fetchone()

        if not qr_row:
            token = str(uuid.uuid4())[:8].upper()
            cursor.execute('INSERT INTO qr_session_token (session_id, token) VALUES (?, ?)', (session_id, token))
            conn.commit()
            qr_token = token
        else:
            qr_token = qr_row['token']

        # Fetch checked-in records
        cursor.execute('''
            SELECT ar.*, st.name, st.roll_number
            FROM attendance_record ar
            JOIN student st ON ar.student_id = st.id
            WHERE ar.session_id = ? AND ar.status = 'Present'
            ORDER BY ar.check_in_time DESC
        ''', (session_id,))
        present_records = cursor.fetchall()

        return render_template('attendance/qr_attendance.html',
                               attendance_session=session_data,
                               qr_token=qr_token,
                               present_records=present_records)
    finally:
        conn.close()


@app.route('/attendance/<int:session_id>/submit-qr-code', methods=['POST'])
def submit_manual_qr_code(session_id):
    """Validate 8-character QR Code & Roll Number and mark student Present"""
    roll_number = request.form.get('roll_number', '').strip()
    qr_code = request.form.get('qr_code', '').strip().upper()

    if not roll_number or not qr_code:
        flash('Roll Number and QR Code are required.', 'error')
        return redirect(url_for('qr_attendance_session', session_id=session_id))

    conn = get_db()
    try:
        cursor = conn.cursor()

        # Validate Session
        cursor.execute('SELECT * FROM attendance_session WHERE id = ?', (session_id,))
        s_data = cursor.fetchone()
        if not s_data:
            flash('Session not found.', 'error')
            return redirect(url_for('attendance'))

        if s_data['is_locked']:
            flash('Session is locked. No further check-ins permitted.', 'error')
            return redirect(url_for('qr_attendance_session', session_id=session_id))

        # Validate QR Token
        cursor.execute('SELECT * FROM qr_session_token WHERE session_id = ? AND token = ? AND is_active = 1', (session_id, qr_code))
        if not cursor.fetchone():
            flash('Invalid or expired QR Backup Code.', 'error')
            return redirect(url_for('qr_attendance_session', session_id=session_id))

        # Validate Student Roll Number
        cursor.execute('SELECT * FROM student WHERE roll_number = ?', (roll_number,))
        student = cursor.fetchone()
        if not student:
            flash(f'Student Roll Number "{roll_number}" not found.', 'error')
            return redirect(url_for('qr_attendance_session', session_id=session_id))

        # Validate Class Branch / Section match
        if student['branch'] != s_data['branch'] or student['section'] != s_data['section']:
            flash(f"Student {student['name']} belongs to {student['branch']}-{student['section']}, not this class.", 'error')
            return redirect(url_for('qr_attendance_session', session_id=session_id))

        # Record Attendance
        now_time = datetime.now().strftime('%H:%M:%S')
        cursor.execute('''
            INSERT OR REPLACE INTO attendance_record (session_id, student_id, status, check_in_time, recognition_method)
            VALUES (?, ?, 'Present', ?, 'qr_code')
        ''', (session_id, student['id'], now_time))
        conn.commit()
        

        try:
            voice_service.announce_attendance_marked(student['name'])
        except Exception as e:
            logger.exception(f"Voice announcement failed: {e}")

        flash(
            f"✅ Attendance Marked PRESENT for {student['name']} ({student['roll_number']}) via QR Code!",
            "success"
        )

       
    except Exception as e:
        logger.exception("Error processing manual QR code entry")
        flash("Failed to process QR attendance entry", "error")
    finally:
        conn.close()

    return redirect(url_for('qr_attendance_session', session_id=session_id))


@app.route('/face-recognition/<int:session_id>')
@login_required
def face_recognition_attendance(session_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM attendance_session WHERE id = ?', (session_id,))
        session_data = cursor.fetchone()

        if not session_data:
            flash('Attendance session not found', 'error')
            return redirect(url_for('take_attendance'))

        if session_data['is_locked']:
            flash('This session has already been finalized and locked.', 'error')
            return redirect(url_for('attendance_detail', session_id=session_id))

        return render_template('attendance/face_recognition.html', attendance_session=session_data)
    finally:
        conn.close()


@app.route('/reports')
@login_required
def reports():
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) FROM student')
        total_students = cursor.fetchone()[0]

        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute("""
        SELECT
            COUNT(CASE WHEN ar.status = 'Present' THEN 1 END) AS present,
            COUNT(CASE WHEN ar.status = 'Absent' THEN 1 END) AS absent
        FROM attendance_record ar
        JOIN attendance_session s ON ar.session_id = s.id
        WHERE s.date = ?
        """, (today,))
        row = cursor.fetchone()
        present_today = row["present"] or 0
        absent_today = row["absent"] or 0


        cursor.execute('''
            SELECT subject, COUNT(*) as total_sessions
            FROM attendance_session
            GROUP BY subject
            ORDER BY total_sessions DESC
        ''')
        subject_stats = cursor.fetchall()

        cursor.execute('''
            SELECT st.name, st.roll_number, st.branch, st.section,
                   COUNT(CASE WHEN ar.status = 'Present' THEN 1 END) as present,
                   COUNT(*) as total
            FROM student st
            LEFT JOIN attendance_record ar ON st.id = ar.student_id
            GROUP BY st.id
            ORDER BY present DESC
            LIMIT 10
        ''')
        student_stats = cursor.fetchall()

        log_faculty_activity(session['faculty_id'], 'VIEW_REPORTS', "Viewed attendance analytics and reports")

        return render_template(
            'reports/index.html',
            total_students=total_students,
            present_today=present_today,
            absent_today=absent_today,
            subject_stats=subject_stats,
            student_stats=student_stats
        )
    finally:
        conn.close()


@app.route('/activity-log')
@login_required
def activity_log():
    """Faculty Activity Audit Trail Page"""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT al.*, f.name as faculty_name
            FROM faculty_activity_log al
            JOIN faculty f ON al.faculty_id = f.id
            ORDER BY al.created_at DESC
            LIMIT 100
        ''')
        logs = cursor.fetchall()
        return render_template('dashboard/activity_log.html', logs=logs)
    finally:
        conn.close()


# QR Backup Attendance Routes
@app.route('/attendance/<int:session_id>/qr')
@login_required
def generate_qr_token(session_id):
    """Generate session QR backup token"""
    conn = get_db()
    try:
        cursor = conn.cursor()
        token = str(uuid.uuid4())[:8].upper()

        # Deactivate old tokens for this session
        cursor.execute('UPDATE qr_session_token SET is_active = 0 WHERE session_id = ?', (session_id,))
        cursor.execute('INSERT INTO qr_session_token (session_id, token) VALUES (?, ?)', (session_id, token))
        conn.commit()

        flash(f"QR Backup Code generated: {token}", "success")
    finally:
        conn.close()
    return redirect(url_for('attendance_detail', session_id=session_id))


@app.route('/api/qr-request', methods=['POST'])
def api_qr_request():
    """Submit QR backup check-in request"""
    try:
        data = request.json or {}
        token = data.get('token', '').strip()
        roll_number = data.get('roll_number', '').strip()

        if not token or not roll_number:
            return jsonify({'success': False, 'message': 'QR token and roll number required'}), 400

        conn = get_db()
        cursor = conn.cursor()

        # Validate token
        cursor.execute('SELECT * FROM qr_session_token WHERE token = ? AND is_active = 1', (token,))
        qr_session = cursor.fetchone()

        if not qr_session:
            conn.close()
            return jsonify({'success': False, 'message': 'Invalid or expired QR backup code'}), 400

        # Validate student
        cursor.execute('SELECT * FROM student WHERE roll_number = ?', (roll_number,))
        student = cursor.fetchone()

        if not student:
            conn.close()
            return jsonify({'success': False, 'message': 'Student roll number not found'}), 404

        # Check existing request
        cursor.execute('SELECT id FROM qr_attendance_request WHERE session_id = ? AND student_id = ?', (qr_session['session_id'], student['id']))
        if cursor.fetchone():
            conn.close()
            return jsonify({'success': False, 'message': 'Backup request already submitted'}), 400

        cursor.execute('''
            INSERT INTO qr_attendance_request (session_id, student_id, token, status)
            VALUES (?, ?, ?, 'PENDING')
        ''', (qr_session['session_id'], student['id'], token))
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'message': f'QR Backup request submitted for {student["name"]}. Waiting for faculty approval.'})
    except Exception as e:
        logger.exception("Error processing QR attendance request")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@app.route('/attendance/<int:session_id>/approve-qr/<int:request_id>', methods=['POST'])
@login_required
def approve_qr_request(session_id, request_id):
    """Faculty approves QR backup request"""
    action = request.form.get('action', 'approve')
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM qr_attendance_request WHERE id = ?', (request_id,))
        req = cursor.fetchone()

        if req:
            if action == 'approve':
                now_str = datetime.now().strftime('%H:%M:%S')
                cursor.execute('''
                    INSERT OR REPLACE INTO attendance_record (session_id, student_id, status, check_in_time, recognition_method)
                    VALUES (?, ?, 'Present', ?, 'qr_backup')
                ''', (session_id, req['student_id'], now_str))
                cursor.execute('UPDATE qr_attendance_request SET status = "APPROVED" WHERE id = ?', (request_id,))
                flash('QR Backup Attendance approved successfully!', 'success')
            else:
                cursor.execute('UPDATE qr_attendance_request SET status = "REJECTED" WHERE id = ?', (request_id,))
                flash('QR Backup Attendance request rejected.', 'info')
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for('attendance_detail', session_id=session_id))


# Chart.js Analytics API
@app.route('/api/analytics')
@login_required
def api_analytics():
    """Return JSON analytics datasets for Chart.js graphics"""
    conn = get_db()
    try:
        cursor = conn.cursor()

        # 1. Subject-wise session distribution
        cursor.execute('''
            SELECT subject, COUNT(*) as session_count
            FROM attendance_session
            GROUP BY subject
        ''')
        subject_rows = cursor.fetchall()
        subject_labels = [r['subject'] for r in subject_rows]
        subject_counts = [r['session_count'] for r in subject_rows]

        # 2. Branch attendance turnout comparison
        cursor.execute('''
            SELECT st.branch, 
                   COUNT(CASE WHEN ar.status = 'Present' THEN 1 END) as present_count,
                   COUNT(ar.id) as total_records
            FROM student st
            LEFT JOIN attendance_record ar ON st.id = ar.student_id
            GROUP BY st.branch
        ''')
        branch_rows = cursor.fetchall()
        branch_labels = [r['branch'] for r in branch_rows]
        branch_rates = [round((r['present_count'] / r['total_records'] * 100), 1) if r['total_records'] > 0 else 0 for r in branch_rows]

        # 3. Daily Attendance Check-ins (Last 7 Days)
        dates = [(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
        daily_counts = []

        for d in dates:
            cursor.execute('''
                SELECT COUNT(*) FROM attendance_record ar
                JOIN attendance_session s ON ar.session_id = s.id
                WHERE s.date = ? AND ar.status = 'Present'
            ''', (d,))
            daily_counts.append(cursor.fetchone()[0])

        return jsonify({
            'subjects': {'labels': subject_labels, 'data': subject_counts},
            'branches': {'labels': branch_labels, 'data': branch_rates},
            'daily': {'labels': dates, 'data': daily_counts}
        })
    finally:
        conn.close()


# API Endpoints
@app.route('/api/check-duplicate-face', methods=['POST'])
def api_check_duplicate_face():
    """Check if face already belongs to another registered student with atomic rollback"""
    try:
        data = request.json or {}
        student_id = data.get('student_id')
        image_data = data.get('image')

        if not image_data:
            return jsonify({'is_duplicate': False, 'message': 'No image data provided'}), 400

        duplicate = face_service.check_duplicate_face(image_data, exclude_student_id=student_id)
                
        print("=" * 50)
        print("Duplicate result:", duplicate)
        print("Student ID:", student_id)
        print("=" * 50)
        
        
        if duplicate:
            # ATOMIC ROLLBACK: If duplicate detected during registration, delete unverified student record & images
            if student_id:
                try:
                    conn = get_db()
                    cursor = conn.cursor()
                    cursor.execute('DELETE FROM student WHERE id = ? AND face_registered = 0', (student_id,))
                    conn.commit()
                    conn.close()

                    import shutil
                    st_dir = os.path.join(PROJECT_ROOT, 'TrainingImage', f"student_{student_id}")
                    if os.path.exists(st_dir):
                        shutil.rmtree(st_dir)

                    face_service.load_student_data()
                    logger.warning(f"Atomic Rollback executed for student ID #{student_id} due to duplicate face detection.")
                except Exception as rollback_err:
                    logger.error(f"Error during atomic rollback: {rollback_err}")

            voice_service.announce_duplicate_face(duplicate['name'], duplicate['roll_number'])
            return jsonify({
                'is_duplicate': True,
                'message': f"This face is already registered to {duplicate['name']} (Roll: {duplicate['roll_number']}). Registration rejected.",
                'existing_student': duplicate
            })
        else:
            voice_service.announce_face_captured()
            return jsonify({'is_duplicate': False})
    except Exception as e:
        logger.exception("Error checking duplicate face")
        return jsonify({'is_duplicate': False, 'error': str(e)})


@app.route('/api/capture-face', methods=['POST'])
def api_capture_face():
    """Save single captured face dataset image"""
    try:
        data = request.json or {}
        student_id = data.get('student_id')
        image_data = data.get('image')
        count = data.get('count', 0)

        if not student_id or not image_data:
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400

        success, message = face_service.save_captured_image(student_id, image_data, count)

        return jsonify({'success': success, 'message': message})
    except Exception as e:
        logger.exception("Error capturing face image")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/recognize-face', methods=['POST'])
def api_recognize_face():
    """Perform real-time face recognition and record student attendance"""
    try:
        data = request.json or {}
        session_id = data.get('session_id')
        image_data = data.get('image')

        if not session_id or not image_data:
            return jsonify({'recognized': False, 'message': 'Session ID and image data required'}), 400

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM attendance_session WHERE id = ?', (session_id,))
        attendance_session = cursor.fetchone()

        if not attendance_session:
            conn.close()
            return jsonify({'recognized': False, 'message': 'Attendance session not found'}), 404

        # Reject recognition if session is locked
        if attendance_session['is_locked']:
            conn.close()
            return jsonify({'recognized': False, 'session_locked': True, 'message': 'This session is finalized and locked.'}), 403

        result, error_msg = face_service.recognize_face(image_data)

        if result:
            cursor.execute('SELECT * FROM student WHERE id = ?', (result['student_id'],))
            student = cursor.fetchone()

            if not student:
                conn.close()
                return jsonify({'recognized': False, 'message': 'Student ID not found in database'})

            # Validate Section & Branch matching
            if student['branch'] != attendance_session['branch'] or student['section'] != attendance_session['section']:
                conn.close()
                logger.warning(f"Branch/Section Mismatch: {student['name']} ({student['branch']}-{student['section']}) vs Session ({attendance_session['branch']}-{attendance_session['section']})")
                return jsonify({
                    'recognized': False,
                    'message': f"Student {student['name']} belongs to {student['branch']}-{student['section']}, not this class session."
                })

            # Check if student is already marked present
            cursor.execute('SELECT id FROM attendance_record WHERE session_id = ? AND student_id = ?', (session_id, student['id']))
            existing = cursor.fetchone()

            if existing:
                conn.close()
                voice_service.announce_already_marked()
                return jsonify({
                    'recognized': False,
                    'already_marked': True,
                    'message': f"Attendance already marked for {student['name']} ({student['roll_number']})"
                })

            # Mark Attendance Present
            now = datetime.now()
            check_in_str = now.strftime('%H:%M:%S')
            cursor.execute('''
                INSERT INTO attendance_record (session_id, student_id, status, check_in_time, recognition_method)
                VALUES (?, ?, 'Present', ?, 'face')
            ''', (session_id, student['id'], check_in_str))
            conn.commit()
            conn.close()

            # Voice Announcement
            voice_service.announce_attendance(student['name'], student['roll_number'])

            return jsonify({
                'recognized': True,
                'student': {
                    'id': student['id'],
                    'name': student['name'],
                    'roll_number': student['roll_number'],
                    'branch': student['branch'],
                    'section': student['section'],
                    'check_in_time': check_in_str,
                    'confidence': result['confidence']
                }
            })
        else:
            conn.close()
            if error_msg == "Unknown face detected":
                voice_service.announce_unknown_face()
            return jsonify({'recognized': False, 'message': error_msg or 'Face recognition failed'})

    except Exception as e:
        logger.exception("Error in face recognition API endpoint")
        return jsonify({'recognized': False, 'message': 'Internal server error'}), 500


@app.route('/profile')
@login_required
def profile():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM faculty WHERE id = ?', (session['faculty_id'],))
        faculty = cursor.fetchone()
        return render_template('profile/index.html', faculty=faculty)
    finally:
        conn.close()


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        faculty_id = request.form.get('faculty_id', '').strip()
        department = request.form.get('department', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        form_data = {
            'name': name,
            'faculty_id': faculty_id,
            'department': department
        }

        if not all([name, faculty_id, department, password, confirm_password]):
            flash('All fields are required', 'error')
            return render_template('auth/register.html', **form_data)

        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('auth/register.html', **form_data)

        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return render_template('auth/register.html', **form_data)

        conn = get_db()
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT id FROM faculty WHERE faculty_id = ?', (faculty_id,))
            if cursor.fetchone():
                flash(f'Faculty ID "{faculty_id}" is already registered.', 'error')
                return render_template('auth/register.html', **form_data)

            password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
            cursor.execute(
                'INSERT INTO faculty (name, faculty_id, department, password_hash) VALUES (?, ?, ?, ?)',
                (name, faculty_id, department, password_hash)
            )
            conn.commit()
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            logger.exception("Faculty registration failed")
            flash('Registration failed. Please try again.', 'error')
            return render_template('auth/register.html', **form_data)
        finally:
            conn.close()

    return render_template('auth/register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        faculty_id = request.form.get('faculty_id', '').strip()
        password = request.form.get('password', '')

        if not all([faculty_id, password]):
            flash('All fields are required', 'error')
            return render_template('auth/login.html', faculty_id=faculty_id)

        conn = get_db()
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT * FROM faculty WHERE faculty_id = ?', (faculty_id,))
            faculty = cursor.fetchone()

            if faculty and bcrypt.check_password_hash(faculty['password_hash'], password):
                session['faculty_id'] = faculty['id']
                session['faculty_name'] = faculty['name']
                session['faculty_department'] = faculty['department']

                log_faculty_activity(faculty['id'], 'LOGIN', f"Faculty {faculty['name']} logged in successfully")
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid Faculty ID or Password', 'error')
                return render_template('auth/login.html', faculty_id=faculty_id)
        finally:
            conn.close()

    return render_template('auth/login.html')


@app.route('/logout')
def logout():
    if 'faculty_id' in session:
        log_faculty_activity(session['faculty_id'], 'LOGOUT', f"Faculty {session.get('faculty_name')} logged out")
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    try:
        cursor = conn.cursor()

        branch_filter = request.args.get('branch', '')
        section_filter = request.args.get('section', '')
        subject_filter = request.args.get('subject', '')
        date_filter = request.args.get('date', '')

        # Total Students with filter
        st_query = 'SELECT COUNT(*) FROM student WHERE 1=1'
        st_params = []
        if branch_filter:
            st_query += ' AND branch = ?'
            st_params.append(branch_filter)
        if section_filter:
            st_query += ' AND section = ?'
            st_params.append(section_filter)

        cursor.execute(st_query, st_params)
        total_students = cursor.fetchone()[0]

        # Present Today with filter
        today = date_filter if date_filter else datetime.now().strftime('%Y-%m-%d')
        p_query = '''
            SELECT COUNT(DISTINCT ar.student_id) FROM attendance_record ar
            JOIN attendance_session s ON ar.session_id = s.id
            JOIN student st ON ar.student_id = st.id
            WHERE s.date = ? AND ar.status = 'Present'
        '''
        p_params = [today]
        if branch_filter:
            p_query += ' AND s.branch = ?'
            p_params.append(branch_filter)
        if section_filter:
            p_query += ' AND s.section = ?'
            p_params.append(section_filter)
        if subject_filter:
            p_query += ' AND s.subject LIKE ?'
            p_params.append(f'%{subject_filter}%')

        cursor.execute(p_query, p_params)
        present_today = cursor.fetchone()[0]

        # Absent Today
        absent_today = max(0, total_students - present_today)
        attendance_percentage = round((present_today / total_students * 100), 1) if total_students > 0 else 0

        # Total Manual Edits Count
        cursor.execute('SELECT COUNT(*) FROM attendance_audit_log')
        manual_edits_count = cursor.fetchone()[0]

        # Today's Sessions Count
        cursor.execute('SELECT COUNT(*) FROM attendance_session WHERE date = ?', (today,))
        today_sessions_count = cursor.fetchone()[0]

        # Recent Sessions with filter
        s_query = '''
            SELECT s.id, s.subject, s.branch, s.section, s.date, s.started_at, s.ended_at, s.is_locked, f.name as faculty_name
            FROM attendance_session s
            JOIN faculty f ON s.faculty_id = f.id
            WHERE 1=1
        '''
        s_params = []
        if branch_filter:
            s_query += ' AND s.branch = ?'
            s_params.append(branch_filter)
        if section_filter:
            s_query += ' AND s.section = ?'
            s_params.append(section_filter)
        if subject_filter:
            s_query += ' AND s.subject LIKE ?'
            s_params.append(f'%{subject_filter}%')
        if date_filter:
            s_query += ' AND s.date = ?'
            s_params.append(date_filter)

        s_query += ' ORDER BY s.started_at DESC LIMIT 6'

        cursor.execute(s_query, s_params)
        recent_sessions = cursor.fetchall()

        cursor.execute('SELECT DISTINCT branch FROM student')
        branches = [row[0] for row in cursor.fetchall()]

        cursor.execute('SELECT DISTINCT section FROM student')
        sections = [row[0] for row in cursor.fetchall()]

        return render_template('dashboard/index.html',
                               total_students=total_students,
                               present_today=present_today,
                               absent_today=absent_today,
                               attendance_percentage=attendance_percentage,
                               manual_edits_count=manual_edits_count,
                               today_sessions_count=today_sessions_count,
                               recent_sessions=recent_sessions,
                               branches=branches,
                               sections=sections,
                               branch_filter=branch_filter,
                               section_filter=section_filter,
                               subject_filter=subject_filter,
                               date_filter=date_filter)
    finally:
        conn.close()


if __name__ == '__main__':
    print("🚀 Starting ClassVision AI...")
    print("📊 Database path:", DB_PATH)
    print("🌐 Running on http://127.0.0.1:5000")
    app.run(debug=True, host='127.0.0.1', port=5000)
