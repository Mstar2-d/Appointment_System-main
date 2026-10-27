from datetime import datetime, timedelta, time, date
from calendar import Calendar as _Calendar, MONDAY as _MONDAY
from email.message import EmailMessage
import os
import secrets
import smtplib
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, flash, abort, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from authlib.integrations.flask_client import OAuth
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'appointment-system-secret-key-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///appointments.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', '')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() in ('1', 'true', 'yes', 'on')
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'false').lower() in ('1', 'true', 'yes', 'on')
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

oauth = OAuth(app)

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()

google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)


def google_login_is_configured():
    """True only when real Google OAuth credentials are present."""
    placeholders = {'', 'your-google-client-id', 'your-google-client-secret'}
    return (GOOGLE_CLIENT_ID not in placeholders
            and GOOGLE_CLIENT_SECRET not in placeholders)


def email_is_configured():
    return bool(app.config['MAIL_SERVER'] and app.config['MAIL_DEFAULT_SENDER'])


def missing_email_settings():
    keys = []
    if not app.config['MAIL_SERVER']:
        keys.append('MAIL_SERVER')
    if not app.config['MAIL_DEFAULT_SENDER']:
        keys.append('MAIL_DEFAULT_SENDER')
    if not app.config['MAIL_USERNAME']:
        keys.append('MAIL_USERNAME')
    if not app.config['MAIL_PASSWORD']:
        keys.append('MAIL_PASSWORD')
    return keys


def send_email(recipient, subject, body):
    if not email_is_configured():
        app.logger.warning('Email settings are incomplete; skipping outbound email.')
        return False

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = app.config['MAIL_DEFAULT_SENDER']
    message['To'] = recipient
    message.set_content(body)

    try:
        if app.config['MAIL_USE_SSL']:
            smtp_client = smtplib.SMTP_SSL(
                app.config['MAIL_SERVER'],
                app.config['MAIL_PORT'],
                timeout=10
            )
        else:
            smtp_client = smtplib.SMTP(
                app.config['MAIL_SERVER'],
                app.config['MAIL_PORT'],
                timeout=10
            )

        with smtp_client as server:
            if app.config['MAIL_USE_TLS'] and not app.config['MAIL_USE_SSL']:
                server.starttls()
            if app.config['MAIL_USERNAME']:
                server.login(app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'])
            server.send_message(message)
        return True
    except Exception:
        app.logger.exception('Failed to send email to %s', recipient)
        return False

DEFAULT_WORK_START = time(9, 0)
DEFAULT_WORK_END = time(17, 0)
DEFAULT_SLOT_MINUTES = 30

CALENDAR_LOOKAHEAD_WEEKS = 12
WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
WEEKDAY_NAMES_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
               'July', 'August', 'September', 'October', 'November', 'December']


def is_weekday(d):
    return d.weekday() < 5


def next_weekday(from_date, allow_today=True):
    d = from_date
    if allow_today and is_weekday(d):
        return d
    if not allow_today:
        d = d + timedelta(days=1)
    while not is_weekday(d):
        d = d + timedelta(days=1)
    return d


def generate_weekdays(from_date, count):
    days = []
    d = from_date
    while len(days) < count:
        if is_weekday(d):
            days.append(d)
        d = d + timedelta(days=1)
    return days


def add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    max_day_for_target = (date(year, month + 1, 1) - timedelta(days=1)).day if month < 12 else 31
    day = min(d.day, max_day_for_target)
    return date(year, month, day)


def build_calendar_month(view_month, today, selected_date, counselor_id):
    first_of_month = view_month.replace(day=1)
    last_of_month = (first_of_month + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    min_date = next_weekday(today, allow_today=True)
    max_date = today + timedelta(days=7 * CALENDAR_LOOKAHEAD_WEEKS)

    cal = _Calendar(firstweekday=_MONDAY)
    raw_weeks = cal.monthdatescalendar(view_month.year, view_month.month)
    rows = []
    for week in raw_weeks:
        row = []
        for d in week:
            in_current_month = (first_of_month <= d <= last_of_month)
            weekday_idx = d.weekday()
            is_weekend = not is_weekday(d)
            is_past = d < today
            before_min = d < min_date
            after_max = d > max_date
            disabled = is_weekend or is_past or before_min or after_max or not in_current_month
            is_selected = (d == selected_date) and in_current_month
            is_today = (d == today) and in_current_month
            has_slots = False
            if in_current_month and is_weekday(d) and not before_min and not after_max:
                try:
                    slots = generate_available_slots(counselor_id, d)
                    has_slots = len(slots) > 0
                except Exception:
                    has_slots = False
            row.append({
                'date': d,
                'day': d.day,
                'in_current_month': in_current_month,
                'weekday_idx': weekday_idx,
                'is_weekend': is_weekend,
                'is_past': is_past,
                'disabled': disabled,
                'is_selected': is_selected,
                'is_today': is_today,
                'has_slots': has_slots,
            })
        rows.append(row)

    prev_month = add_months(view_month, -1)
    next_month = add_months(view_month, 1)

    return {
        'month_title': f"{MONTH_NAMES[view_month.month - 1]} {view_month.year}",
        'weekday_headers': WEEKDAY_NAMES_SHORT,
        'rows': rows,
        'prev_month': prev_month,
        'next_month': next_month,
        'prev_disabled': (prev_month < today.replace(day=1) - timedelta(days=0)
                          and (next_weekday(today, allow_today=True) >= first_of_month)),
        'next_disabled': next_month > (add_months(today.replace(day=1), CALENDAR_LOOKAHEAD_WEEKS // 4 + 2)),
        'min_date': min_date,
        'max_date': max_date,
    }


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    specialty = db.Column(db.String(120))
    student_id = db.Column(db.String(50))
    phone = db.Column(db.String(20))
    reset_token = db.Column(db.String(256))
    reset_token_expiry = db.Column(db.DateTime)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_counselor(self):
        return self.role == 'counselor'

    @property
    def is_student(self):
        return self.role == 'student'

    @property
    def is_teacher(self):
        return self.role == 'teacher'


class BlockedTime(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    counselor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    reason = db.Column(db.String(200))
    is_full_day = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    counselor = db.relationship('User', backref=db.backref('blocked_times', lazy=True))

    @property
    def time_str(self):
        if self.is_full_day:
            return "Full Day"
        return f"{self.start_time.strftime('%I:%M %p')} - {self.end_time.strftime('%I:%M %p')}"


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    counselor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    purpose = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='pending')
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    first_name = db.Column(db.String(80))
    middle_name = db.Column(db.String(80))
    last_name = db.Column(db.String(80))
    birthday = db.Column(db.Date)
    section = db.Column(db.String(50))
    year_level = db.Column(db.String(50))
    department = db.Column(db.String(100))
    mood_rating = db.Column(db.String(20))
    mood_description = db.Column(db.String(200))
    contact_email = db.Column(db.String(120))
    contact_phone = db.Column(db.String(20))
    guest_student_id = db.Column(db.String(50))
    referred_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    is_guest = db.Column(db.Boolean, default=False)
    session_summary = db.Column(db.Text)

    student = db.relationship('User', foreign_keys=[student_id], backref=db.backref('student_appointments', lazy=True))
    counselor = db.relationship('User', foreign_keys=[counselor_id], backref=db.backref('counselor_appointments', lazy=True))
    referred_by = db.relationship('User', foreign_keys=[referred_by_id], backref=db.backref('referrals', lazy=True))

    __table_args__ = (db.UniqueConstraint('counselor_id', 'date', 'start_time', name='_counselor_date_time_uc'),)

    @property
    def time_str(self):
        return f"{self.start_time.strftime('%I:%M %p')} - {self.end_time.strftime('%I:%M %p')}"

    @property
    def guest_full_name(self):
        parts = [p for p in [self.first_name, self.middle_name, self.last_name] if p]
        return ' '.join(parts) if parts else None

    @property
    def display_name(self):
        if self.is_guest:
            return self.guest_full_name or 'Guest Student'
        return self.student.full_name if self.student else 'Unknown'


def times_overlap(s1_start, s1_end, s2_start, s2_end):
    return s1_start < s2_end and s2_start < s1_end


def generate_available_slots(counselor_id, date, slot_minutes=DEFAULT_SLOT_MINUTES):
    if not is_weekday(date):
        return []
    work_start_dt = datetime.combine(date, DEFAULT_WORK_START)
    work_end_dt = datetime.combine(date, DEFAULT_WORK_END)
    now = datetime.now()

    slots = []
    current = work_start_dt
    while current + timedelta(minutes=slot_minutes) <= work_end_dt:
        slot_end = current + timedelta(minutes=slot_minutes)
        slot_start_t = current.time()
        slot_end_t = slot_end.time()

        if datetime.combine(date, slot_start_t) <= now:
            current = slot_end
            continue

        overlap = False

        for bt in BlockedTime.query.filter_by(counselor_id=counselor_id, date=date).all():
            if bt.is_full_day:
                overlap = True
                break
            if times_overlap(slot_start_t, slot_end_t, bt.start_time, bt.end_time):
                overlap = True
                break
        if overlap:
            current = slot_end
            continue

        for appt in Appointment.query.filter_by(counselor_id=counselor_id, date=date).all():
            if times_overlap(slot_start_t, slot_end_t, appt.start_time, appt.end_time):
                overlap = True
                break
        if overlap:
            current = slot_end
            continue

        slots.append({
            'start': slot_start_t,
            'end': slot_end_t,
            'start_str': slot_start_t.strftime('%H:%M'),
            'end_str': slot_end_t.strftime('%H:%M'),
            'label': f"{slot_start_t.strftime('%I:%M %p')} - {slot_end_t.strftime('%I:%M %p')}",
            'key': f"{slot_start_t.strftime('%H%M')}_{slot_end_t.strftime('%H%M')}"
        })
        current = slot_end
    return slots


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def fmt_date_long(value):
    if value is None:
        return ''
    try:
        return value.strftime('%B') + ' ' + str(value.day) + ', ' + str(value.year)
    except Exception:
        return str(value)


def fmt_date_short(value):
    if value is None:
        return ''
    try:
        return value.strftime('%b') + ' ' + str(value.day)
    except Exception:
        return str(value)


def fmt_time(value):
    if value is None:
        return ''
    try:
        return value.strftime('%I:%M %p')
    except Exception:
        return str(value)


app.jinja_env.filters['fmt_long'] = fmt_date_long
app.jinja_env.filters['fmt_short'] = fmt_date_short
app.jinja_env.filters['fmt_time'] = fmt_time


@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.is_counselor:
            return redirect(url_for('counselor_dashboard'))
        elif current_user.is_teacher:
            return redirect(url_for('teacher_dashboard'))
        # Student accounts are no longer supported; clear any stale session.
        logout_user()
    if session.get('counselor_access'):
        return redirect(url_for('login'))
    counselors = User.query.filter_by(role='counselor', is_admin=False).all()
    return render_template('index.html', counselors=counselors)


@app.route('/toggle-counselor-access', methods=['POST'])
def toggle_counselor_access():
    session['counselor_access'] = not session.get('counselor_access', False)
    if session['counselor_access']:
        flash('Counselor mode enabled — use Counselor Login above.', 'info')
    else:
        flash('Switched back to Student mode.', 'info')
    return redirect(request.referrer or url_for('index'))


@app.route('/counselor/create', methods=['POST'])
@login_required
def create_counselor():
    if not current_user.is_admin:
        abort(403)
    username = request.form.get('username')
    email = request.form.get('email')
    password = request.form.get('password')
    full_name = request.form.get('full_name')
    specialty = request.form.get('specialty')
    phone = request.form.get('phone')

    if not username or not email or not password or not full_name or not phone:
        flash('Please fill in all required fields.', 'danger')
        return redirect(url_for('counselor_dashboard'))
    if User.query.filter_by(username=username).first():
        flash('Username already taken.', 'danger')
        return redirect(url_for('counselor_dashboard'))
    if User.query.filter_by(email=email).first():
        flash('Email already registered.', 'danger')
        return redirect(url_for('counselor_dashboard'))

    user = User(
        username=username,
        email=email,
        role='counselor',
        full_name=full_name,
        specialty=specialty,
        phone=phone
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    flash(f'Counselor account "{username}" created.', 'success')
    return redirect(url_for('counselor_dashboard'))


@app.route('/teacher/create', methods=['POST'])
@login_required
def create_teacher():
    if not current_user.is_admin:
        abort(403)
    username = request.form.get('username')
    email = request.form.get('email')
    password = request.form.get('password')
    full_name = request.form.get('full_name')
    phone = request.form.get('phone')
    department = request.form.get('department') # Teachers have a department

    if not username or not email or not password or not full_name or not phone:
        flash('Please fill in all required fields.', 'danger')
        return redirect(url_for('counselor_dashboard'))
    if User.query.filter_by(username=username).first():
        flash('Username already taken.', 'danger')
        return redirect(url_for('counselor_dashboard'))
    if User.query.filter_by(email=email).first():
        flash('Email already registered.', 'danger')
        return redirect(url_for('counselor_dashboard'))

    user = User(
        username=username,
        email=email,
        role='teacher',
        full_name=full_name,
        phone=phone,
        specialty=department # Using specialty field for department in User model to keep it simple, or I should add department to User model too.
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    flash(f'Teacher account "{username}" created.', 'success')
    return redirect(url_for('counselor_dashboard'))


@app.route('/counselor/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_counselor(user_id):
    if not current_user.is_admin:
        abort(403)
    
    user = User.query.get_or_404(user_id)
    if not user.is_counselor or user.is_admin:
        flash('Invalid user account for deletion.', 'danger')
        return redirect(url_for('counselor_dashboard'))
    
    # Clean up associated data
    Appointment.query.filter_by(counselor_id=user.id).delete()
    BlockedTime.query.filter_by(counselor_id=user.id).delete()
    
    db.session.delete(user)
    db.session.commit()
    
    flash(f'Counselor account "{user.username}" and all associated data have been removed.', 'info')
    return redirect(url_for('counselor_dashboard'))


@app.route('/teacher/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_teacher(user_id):
    if not current_user.is_admin:
        abort(403)
    
    user = User.query.get_or_404(user_id)
    if not user.is_teacher:
        flash('Invalid user account for deletion.', 'danger')
        return redirect(url_for('counselor_dashboard'))
    
    # Clean up referrals
    Appointment.query.filter_by(referred_by_id=user.id).delete()
    
    db.session.delete(user)
    db.session.commit()
    
    flash(f'Teacher account "{user.username}" and all referrals have been removed.', 'info')
    return redirect(url_for('counselor_dashboard'))


@app.route('/teacher/dashboard')
@login_required
def teacher_dashboard():
    if not current_user.is_teacher:
        abort(403)
    
    referrals = Appointment.query.filter_by(referred_by_id=current_user.id).order_by(Appointment.date.desc()).all()

    return render_template('teacher_dashboard.html', referrals=referrals)


@app.route('/teacher/refer', methods=['GET', 'POST'])
@login_required
def teacher_refer():
    if not current_user.is_teacher:
        abort(403)

    if request.method == 'GET':
        counselors = User.query.filter_by(role='counselor', is_admin=False).all()
        return render_template('teacher_refer.html', counselors=counselors)

    counselor_id = request.form.get('counselor_id')
    student_id = request.form.get('student_id')
    first_name = request.form.get('first_name')
    last_name = request.form.get('last_name')
    year_level = request.form.get('year_level')
    section = request.form.get('section')
    department = request.form.get('department')
    reason = request.form.get('reason')
    purpose = request.form.get('purpose', '').strip()

    counselor = None
    if counselor_id and str(counselor_id).isdigit():
        counselor = User.query.filter_by(id=int(counselor_id), role='counselor', is_admin=False).first()
    if not counselor:
        flash('Please choose a valid counselor.', 'danger')
        return redirect(url_for('teacher_refer'))

    combined_purpose = f"{reason}: {purpose}" if purpose else reason

    # A referral does not reserve a real slot yet (the counselor schedules it
    # later), but the system requires a date/time. Give each referral a unique
    # placeholder time on today's date so that a second referral to the same
    # counselor on the same day does not violate the
    # (counselor_id, date, start_time) unique constraint.
    placeholder_offset = Appointment.query.filter_by(
        counselor_id=counselor.id,
        date=date.today()
    ).count()
    placeholder_start = (datetime.combine(date.today(), time(0, 0)) + timedelta(minutes=placeholder_offset)).time()
    placeholder_end = (datetime.combine(date.today(), placeholder_start) + timedelta(minutes=30)).time()

    appointment = Appointment(
        counselor_id=counselor.id,
        referred_by_id=current_user.id,
        date=date.today(),
        start_time=placeholder_start,
        end_time=placeholder_end,
        purpose=f"[REFERRAL by {current_user.full_name}] {combined_purpose}",
        first_name=first_name,
        last_name=last_name,
        year_level=year_level,
        section=section,
        department=department,
        guest_student_id=student_id,
        is_guest=True,
        status='pending'
    )
    try:
        db.session.add(appointment)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Unable to submit this referral right now. Please try again.', 'danger')
        return redirect(url_for('teacher_refer'))

    flash('Student referral submitted successfully.', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if not session.get('counselor_access', False):
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and (user.is_counselor or user.is_teacher) and user.check_password(password):
            login_user(user)
            next_page = request.args.get('next')
            # Only allow same-site relative redirects to avoid an open redirect.
            if next_page and urlparse(next_page).netloc:
                next_page = None
            flash(f'Welcome back, {user.full_name}!', 'success')
            if user.is_counselor:
                return redirect(next_page or url_for('counselor_dashboard'))
            elif user.is_teacher:
                return redirect(next_page or url_for('teacher_dashboard'))
            return redirect(next_page or url_for('index'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html')


@app.route('/login/google')
def google_login():
    if not google_login_is_configured():
        flash('Google Login is not configured. Please contact the administrator.', 'danger')
        return redirect(url_for('login'))
    redirect_uri = url_for('google_authorize', _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route('/login/google/authorize')
def google_authorize():
    if not google_login_is_configured():
        flash('Google Login is not configured. Please contact the administrator.', 'danger')
        return redirect(url_for('login'))

    token = google.authorize_access_token()
    user_info = token.get('userinfo') or google.userinfo()
    email = (user_info or {}).get('email')

    if not email:
        flash('Failed to retrieve your email from Google.', 'danger')
        return redirect(url_for('login'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('No account is linked to this Google email. Please contact the administrator.', 'danger')
        return redirect(url_for('login'))

    if not (user.is_counselor or user.is_teacher):
        flash('This account does not have access to the dashboard.', 'danger')
        return redirect(url_for('login'))

    login_user(user)
    flash(f'Welcome back, {user.full_name}!', 'success')
    if user.is_counselor:
        return redirect(url_for('counselor_dashboard'))
    return redirect(url_for('teacher_dashboard'))


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


def upcoming_appointments_query(counselor_id):
    # Appointments that still need the counselor's attention: today/future
    # appointments that are not yet completed, plus unresolved teacher
    # referrals (they use a placeholder date and must stay visible until acted on).
    today = datetime.now().date()
    return Appointment.query.filter_by(counselor_id=counselor_id).filter(
        db.or_(
            db.and_(
                Appointment.date >= today,
                Appointment.status != 'completed'
            ),
            db.and_(
                Appointment.referred_by_id.isnot(None),
                Appointment.status.in_(['pending', 'approved'])
            )
        )
    )


@app.route('/counselor/dashboard')
@login_required
def counselor_dashboard():
    if not current_user.is_counselor:
        abort(403)
    today = datetime.now().date()
    upcoming_appointments = upcoming_appointments_query(
        current_user.id
    ).order_by(Appointment.date, Appointment.start_time).all()
    pending_appointments = [a for a in upcoming_appointments if a.status == 'pending']

    upcoming_blocked = BlockedTime.query.filter_by(
        counselor_id=current_user.id
    ).filter(BlockedTime.date >= today).order_by(BlockedTime.date, BlockedTime.start_time).all()

    return render_template('counselor_dashboard.html',
                           appointments=upcoming_appointments,
                           pending_appointments=pending_appointments,
                           blocked_times=upcoming_blocked)


ACCOUNT_ROLES = {
    'students': ('student', 'Students'),
    'teachers': ('teacher', 'Teachers'),
    'counselors': ('counselor', 'Counselors'),
}


@app.route('/admin/users/<role>')
@login_required
def admin_users(role):
    if not current_user.is_admin:
        abort(403)
    if role not in ACCOUNT_ROLES:
        abort(404)

    role_value, role_label = ACCOUNT_ROLES[role]
    query = User.query.filter_by(role=role_value)
    if role_value == 'counselor':
        # The administrator account is a counselor but is not a team member.
        query = query.filter_by(is_admin=False)
    accounts = query.order_by(User.full_name).all()

    counts = {
        'students': User.query.filter_by(role='student').count(),
        'teachers': User.query.filter_by(role='teacher').count(),
        'counselors': User.query.filter_by(role='counselor', is_admin=False).count(),
    }

    return render_template('admin_users.html',
                           role=role,
                           role_label=role_label,
                           accounts=accounts,
                           counts=counts)


@app.route('/counselor/history')
@login_required
def counselor_history():
    if not current_user.is_counselor:
        abort(403)
    # Everything that is no longer part of the upcoming schedule: completed
    # sessions and appointments whose date has already passed.
    upcoming_ids = {a.id for a in upcoming_appointments_query(current_user.id).all()}
    all_appointments = Appointment.query.filter_by(
        counselor_id=current_user.id
    ).order_by(Appointment.date.desc(), Appointment.start_time.desc()).all()
    past_appointments = [a for a in all_appointments if a.id not in upcoming_ids]

    return render_template('counselor_history.html', appointments=past_appointments)


@app.route('/counselor/block_time', methods=['GET', 'POST'])
@login_required
def block_time():
    if not current_user.is_counselor:
        abort(403)
    if request.method == 'POST':
        date_str = request.form.get('date')
        is_full_day = 'is_full_day' in request.form
        start_time_str = request.form.get('start_time')
        end_time_str = request.form.get('end_time')
        reason = request.form.get('reason', '').strip()

        date = datetime.strptime(date_str, '%Y-%m-%d').date()

        if is_full_day:
            start_time = DEFAULT_WORK_START
            end_time = DEFAULT_WORK_END
        else:
            if not start_time_str or not end_time_str:
                flash('Please provide start and end times.', 'danger')
                return redirect(url_for('block_time'))
            start_time = datetime.strptime(start_time_str, '%H:%M').time()
            end_time = datetime.strptime(end_time_str, '%H:%M').time()
            if start_time >= end_time:
                flash('End time must be after start time.', 'danger')
                return redirect(url_for('block_time'))

        if is_full_day:
            existing = BlockedTime.query.filter_by(
                counselor_id=current_user.id,
                date=date,
                is_full_day=True
            ).first()
            if existing:
                flash('Full day already blocked for this date.', 'danger')
                return redirect(url_for('block_time'))
        else:
            start_dt = datetime.combine(date, start_time)
            end_dt = datetime.combine(date, end_time)
            c = start_dt
            conflict = False
            while c < end_dt:
                c_end = c + timedelta(minutes=DEFAULT_SLOT_MINUTES)
                for bt in BlockedTime.query.filter_by(counselor_id=current_user.id, date=date).all():
                    bt_s = datetime.combine(date, bt.start_time)
                    bt_e = datetime.combine(date, bt.end_time)
                    if c < bt_e and bt_s < c_end:
                        conflict = True
                        break
                if conflict:
                    break
                c = c_end
            if conflict:
                flash('This time overlaps with an existing blocked time.', 'danger')
                return redirect(url_for('block_time'))

        blocked = BlockedTime(
            counselor_id=current_user.id,
            date=date,
            start_time=start_time,
            end_time=end_time,
            reason=reason if reason else None,
            is_full_day=is_full_day
        )
        db.session.add(blocked)
        db.session.commit()
        flash('Time blocked successfully.', 'success')
        return redirect(url_for('counselor_dashboard'))
    return render_template('block_time.html')


@app.route('/counselor/blocked/<int:blocked_id>/delete', methods=['POST'])
@login_required
def delete_blocked(blocked_id):
    if not current_user.is_counselor:
        abort(403)
    bt = BlockedTime.query.get_or_404(blocked_id)
    if bt.counselor_id != current_user.id:
        abort(403)
    db.session.delete(bt)
    db.session.commit()
    flash('Blocked time removed.', 'info')
    return redirect(url_for('counselor_dashboard'))


@app.route('/appointment/<int:appointment_id>/<action>', methods=['POST'])
@login_required
def update_appointment(appointment_id, action):
    appointment = Appointment.query.get_or_404(appointment_id)
    if current_user.is_counselor and appointment.counselor_id != current_user.id:
        abort(403)
    if current_user.is_student and appointment.student_id != current_user.id:
        abort(403)

    if action == 'approve' and current_user.is_counselor:
        appointment.status = 'approved'
        flash('Appointment approved.', 'success')
    elif action == 'reject' and current_user.is_counselor:
        appointment.status = 'rejected'
        flash('Appointment rejected.', 'info')
    elif action == 'complete' and current_user.is_counselor:
        appointment.status = 'completed'
        flash('Appointment marked as completed.', 'success')
    elif action == 'cancel' and current_user.is_student:
        db.session.delete(appointment)
        flash('Appointment cancelled.', 'info')
    else:
        abort(400)

    db.session.commit()
    if current_user.is_counselor:
        return redirect(url_for('counselor_dashboard'))
    return redirect(url_for('index'))


@app.route('/appointment/<int:appointment_id>/summary', methods=['POST'])
@login_required
def save_session_summary(appointment_id):
    if not current_user.is_counselor:
        abort(403)
    appointment = Appointment.query.get_or_404(appointment_id)
    if appointment.counselor_id != current_user.id:
        abort(403)

    if appointment.status == 'completed':
        flash('This session is already finished. The summary can no longer be edited.', 'danger')
        return redirect(url_for('counselor_dashboard'))

    summary = request.form.get('session_summary', '').strip()
    appointment.session_summary = summary if summary else None
    db.session.commit()
    flash('Session summary saved. The referring teacher can now see it.', 'success')
    return redirect(url_for('counselor_dashboard'))


MOOD_OPTIONS = [
    ('excellent', '😊 Excellent – I feel great!'),
    ('good', '🙂 Good – doing pretty well'),
    ('okay', '😐 Okay – just so-so'),
    ('low', '😔 Low – feeling a bit down'),
    ('difficult', '😟 Difficult – struggling right now'),
    ('critical', '😢 Critical – I really need help'),
]

FEELING_OPTIONS = [
    ('stressed', 'Stressed or anxious about studies'),
    ('sad', 'Sad or depressed'),
    ('lonely', 'Lonely or isolated'),
    ('angry', 'Angry or frustrated'),
    ('family', 'Family or home issues'),
    ('friends', 'Friendship or relationship problems'),
    ('bullying', 'Bullying or harassment concerns'),
    ('academic', 'Academic pressure / failing grades'),
    ('sleep', 'Sleep problems'),
    ('self_esteem', 'Self-esteem / confidence issues'),
    ('career', 'Career or future uncertainty'),
    ('other', 'Something else (please describe)'),
]


def guest_has_active_appointment_hash(first_name, last_name, birthday_str):
    if not birthday_str:
        return False
    try:
        bd = datetime.strptime(birthday_str, '%Y-%m-%d').date()
    except Exception:
        return False
    first_name = (first_name or '').strip()
    last_name = (last_name or '').strip()
    return Appointment.query.filter(
        Appointment.is_guest == True,
        Appointment.first_name == first_name,
        Appointment.last_name == last_name,
        Appointment.birthday == bd,
        Appointment.status.in_(['pending', 'approved'])
    ).first() is not None


@app.route('/counselor/<int:counselor_id>/walkin', methods=['GET', 'POST'])
def book_appointment_public(counselor_id):
    counselor = User.query.get_or_404(counselor_id)
    if not counselor.is_counselor or counselor.is_admin:
        abort(404)

    today = datetime.now().date()

    selected_date = request.args.get('date')
    if selected_date:
        try:
            selected_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
        except ValueError:
            selected_date = None
    if not selected_date:
        selected_date = today

    if not is_weekday(selected_date) or selected_date < today:
        target = selected_date if selected_date >= today else today
        normalized = next_weekday(target, allow_today=(selected_date >= today and is_weekday(selected_date)))
        if normalized != selected_date:
            return redirect(url_for('book_appointment_public', counselor_id=counselor_id, date=normalized.strftime('%Y-%m-%d')))
        selected_date = normalized

    view_month = None
    raw_month = request.args.get('month')
    if raw_month:
        try:
            yr, mo = raw_month.split('-')
            view_month = date(int(yr), int(mo), 1)
        except Exception:
            view_month = None
    if not view_month:
        view_month = selected_date.replace(day=1)

    available_slots = generate_available_slots(counselor_id, selected_date)

    if request.method == 'POST':
        slot_key = request.form.get('slot_key')
        purpose = request.form.get('purpose', '').strip()
        first_name = request.form.get('first_name', '').strip()
        middle_name = request.form.get('middle_name', '').strip() or None
        last_name = request.form.get('last_name', '').strip()
        birthday_str = request.form.get('birthday', '').strip()
        section = request.form.get('section', '').strip() or None
        year_level = request.form.get('year_level', '').strip() or None
        department = request.form.get('department', '').strip() or None
        mood_rating = request.form.get('mood_rating', '').strip() or None
        mood_description = request.form.getlist('mood_description')
        mood_note = request.form.get('mood_note', '').strip()
        contact_email = request.form.get('contact_email', '').strip() or None
        contact_phone = request.form.get('contact_phone', '').strip() or None
        student_id_val = request.form.get('student_id', '').strip() or None

        errors = []
        if not slot_key:
            errors.append('Please select a time slot.')
        if not first_name:
            errors.append('First Name is required.')
        if not last_name:
            errors.append('Last Name is required.')
        if not birthday_str:
            errors.append('Birthday is required.')
        if not purpose:
            errors.append('Please describe your purpose for this appointment.')

        birthday_obj = None
        if birthday_str:
            try:
                birthday_obj = datetime.strptime(birthday_str, '%Y-%m-%d').date()
            except ValueError:
                errors.append('Invalid birthday format.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return redirect(url_for('book_appointment_public', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        if guest_has_active_appointment_hash(first_name, last_name, birthday_str):
            flash('You already have an active appointment. Please wait for it to be completed first.', 'danger')
            return redirect(url_for('book_appointment_public', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        try:
            start_str, end_str = slot_key.split('_')
            start_h, start_m = int(start_str[:2]), int(start_str[2:])
            end_h, end_m = int(end_str[:2]), int(end_str[2:])
            slot_start = time(start_h, start_m)
            slot_end = time(end_h, end_m)
        except Exception:
            flash('Invalid time slot selected.', 'danger')
            return redirect(url_for('book_appointment_public', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        refreshed_slots = generate_available_slots(counselor_id, selected_date)
        valid_keys = {s['key'] for s in refreshed_slots}
        if slot_key not in valid_keys:
            flash('This slot is no longer available.', 'danger')
            return redirect(url_for('book_appointment_public', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        existing = Appointment.query.filter_by(
            counselor_id=counselor_id,
            date=selected_date,
            start_time=slot_start
        ).first()
        if existing:
            flash('This slot is no longer available.', 'danger')
            return redirect(url_for('book_appointment_public', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        mood_desc_joined = None
        if mood_description or mood_note:
            labels = dict((k, v) for k, v in FEELING_OPTIONS)
            chosen = []
            for key in mood_description:
                if key in labels:
                    chosen.append(labels[key].split(' – ')[0].split(' (')[0])
            if mood_note:
                chosen.append(mood_note)
            mood_desc_joined = '; '.join(chosen) if chosen else None

        appointment = Appointment(
            counselor_id=counselor_id,
            student_id=None,
            date=selected_date,
            start_time=slot_start,
            end_time=slot_end,
            purpose=purpose,
            first_name=first_name,
            middle_name=middle_name,
            last_name=last_name,
            birthday=birthday_obj,
            section=section,
            year_level=year_level,
            department=department,
            mood_rating=mood_rating,
            mood_description=mood_desc_joined,
            contact_email=contact_email,
            contact_phone=contact_phone,
            guest_student_id=student_id_val,
            is_guest=True,
            status='approved',
        )
        db.session.add(appointment)
        db.session.commit()
        flash('Your appointment has been submitted.', 'success')
        return redirect(url_for('booking_confirmation', appointment_id=appointment.id))

    dates = generate_weekdays(today, 14)
    calendar = build_calendar_month(view_month, today, selected_date, counselor_id)
    return render_template('book_appointment_public.html',
                           counselor=counselor,
                           available_slots=available_slots,
                           selected_date=selected_date,
                           dates=dates,
                           calendar=calendar,
                           view_month=view_month,
                           weekday_names=WEEKDAY_NAMES,
                           default_start=DEFAULT_WORK_START.strftime('%I:%M %p'),
                           default_end=DEFAULT_WORK_END.strftime('%I:%M %p'),
                           mood_options=MOOD_OPTIONS,
                           feeling_options=FEELING_OPTIONS)


@app.route('/booking/<int:appointment_id>/confirmation')
def booking_confirmation(appointment_id):
    appt = Appointment.query.get_or_404(appointment_id)
    if not appt.is_guest:
        return redirect(url_for('index'))
    return render_template('booking_confirmation.html',
                           appointment=appt,
                           weekday_names=WEEKDAY_NAMES)


with app.app_context():
    inspector = db.inspect(db.engine)
    existing_tables = inspector.get_table_names()

    if 'appointment' in existing_tables:
        appt_columns = [c['name'] for c in inspector.get_columns('appointment')]
        student_id_nullable = next((c['nullable'] for c in inspector.get_columns('appointment') if c['name'] == 'student_id'), True)
        needs_rebuild = (not student_id_nullable) or any(col not in appt_columns for col, _ in [
            ('first_name', ''), ('middle_name', ''), ('last_name', ''), ('birthday', ''),
            ('section', ''), ('year_level', ''), ('department', ''), ('mood_rating', ''), ('mood_description', ''),
            ('contact_email', ''), ('contact_phone', ''), ('guest_student_id', ''), ('referred_by_id', ''), ('is_guest', ''),
        ])

        if needs_rebuild:
            with db.engine.connect() as conn:
                conn.execute(db.text('ALTER TABLE appointment RENAME TO appointment_old'))
                conn.commit()
            db.create_all()
            with db.engine.connect() as conn:
                cols_old = [c['name'] for c in inspector.get_columns('appointment_old')]
                cols_new = [c['name'] for c in inspector.get_columns('appointment')]
                common = [c for c in cols_old if c in cols_new]
                placeholders = ', '.join(common)
                try:
                    conn.execute(db.text(f'INSERT INTO appointment ({placeholders}) SELECT {placeholders} FROM appointment_old'))
                    conn.commit()
                except Exception as e:
                    app.logger.warning('Could not copy all appointment data: %s', e)
                    conn.rollback()
                try:
                    conn.execute(db.text('DROP TABLE appointment_old'))
                    conn.commit()
                except Exception:
                    pass
        else:
            with db.engine.connect() as conn:
                for col_name, col_type in [
                    ('first_name', 'VARCHAR(80)'), ('middle_name', 'VARCHAR(80)'), ('last_name', 'VARCHAR(80)'),
                    ('birthday', 'DATE'), ('section', 'VARCHAR(50)'), ('year_level', 'VARCHAR(50)'),
                    ('department', 'VARCHAR(100)'),
                    ('mood_rating', 'VARCHAR(20)'), ('mood_description', 'VARCHAR(200)'),
                    ('contact_email', 'VARCHAR(120)'), ('contact_phone', 'VARCHAR(20)'),
                    ('guest_student_id', 'VARCHAR(50)'),
                    ('referred_by_id', 'INTEGER'),
                    ('is_guest', 'BOOLEAN DEFAULT 0'),
                    ('session_summary', 'TEXT'),
                ]:
                    if col_name not in appt_columns:
                        conn.execute(db.text(f'ALTER TABLE appointment ADD COLUMN {col_name} {col_type}'))
                        conn.commit()

    db.create_all()

    inspector = db.inspect(db.engine)
    user_columns = [c['name'] for c in inspector.get_columns('user')]
    if 'reset_token' not in user_columns:
        with db.engine.connect() as conn:
            conn.execute(db.text('ALTER TABLE user ADD COLUMN reset_token VARCHAR(256)'))
            conn.commit()
    if 'reset_token_expiry' not in user_columns:
        with db.engine.connect() as conn:
            conn.execute(db.text('ALTER TABLE user ADD COLUMN reset_token_expiry DATETIME'))
            conn.commit()
    if 'is_admin' not in user_columns:
        with db.engine.connect() as conn:
            conn.execute(db.text('ALTER TABLE user ADD COLUMN is_admin BOOLEAN DEFAULT 0'))
            conn.commit()
        with db.engine.connect() as conn:
            conn.execute(db.text('UPDATE user SET is_admin = 0 WHERE is_admin IS NULL'))
            conn.commit()

    admin_user = User.query.filter_by(username='admin').first()
    if not admin_user:
        admin_user = User(
            username='admin',
            email='admin@norzagaraycollege.edu.ph',
            role='counselor',
            full_name='Administrator',
            is_admin=True
        )
        admin_user.set_password('admin123')
        db.session.add(admin_user)
        db.session.commit()
    elif not admin_user.is_admin:
        admin_user.is_admin = True
        db.session.commit()


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    counselor_access = session.get('counselor_access', False)
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        if not identifier:
            flash('Please enter your username or email.', 'danger')
            return redirect(url_for('forgot_password'))
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()
        if not user:
            flash('No account found with that username or email.', 'danger')
            return redirect(url_for('forgot_password'))

        if counselor_access and not user.is_counselor:
            flash('This is counselor recovery mode. Use default mode for student recovery.', 'danger')
            return redirect(url_for('forgot_password'))
        if not counselor_access and user.is_counselor:
            flash('Counselor recovery requires counselor access. Double-click the title.', 'danger')
            return redirect(url_for('forgot_password'))

        token = secrets.token_urlsafe(32)
        user.reset_token = token
        user.reset_token_expiry = datetime.utcnow() + timedelta(hours=1)
        db.session.commit()

        reset_url = url_for('reset_password', token=token, _external=True)
        email_body = (
            f"Hello {user.full_name},\n\n"
            "We received a request to reset your password for the Appointment System.\n"
            "Use the link below to choose a new password:\n\n"
            f"{reset_url}\n\n"
            "This link will expire in 1 hour.\n"
            "If you did not request a password reset, you can ignore this email."
        )

        if send_email(user.email, 'Appointment System Password Reset', email_body):
            flash(f'Password reset link sent to {user.email}.', 'success')
        else:
            missing = missing_email_settings()
            if missing:
                flash(
                    'Unable to send the reset email because these settings are missing: '
                    f"{', '.join(missing)}. Add them as environment variables or in a .env file "
                    '(Gmail: smtp.gmail.com:587; Outlook: smtp.office365.com:587; Yahoo: smtp.mail.yahoo.com:465).',
                    'danger'
                )
            else:
                flash(
                    'Unable to send the reset email. Check your SMTP server, port, credentials, '
                    'and that less-secure-app / app-password access is enabled in your email account.',
                    'danger'
                )
        return redirect(url_for('forgot_password', counselor_access=counselor_access))
    return render_template('forgot_password.html', counselor_access=counselor_access)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    counselor_access = session.get('counselor_access', False)
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    user = User.query.filter_by(reset_token=token).first()
    if not user or not user.reset_token_expiry or user.reset_token_expiry < datetime.utcnow():
        flash('Invalid or expired password reset link.', 'danger')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form.get('password')
        confirm = request.form.get('confirm_password')
        if not password or len(password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return redirect(url_for('reset_password', token=token))
        if password != confirm:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('reset_password', token=token))
        user.set_password(password)
        user.reset_token = None
        user.reset_token_expiry = None
        db.session.commit()
        flash('Password reset successful! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('reset_password.html', token=token, counselor_access=counselor_access)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
