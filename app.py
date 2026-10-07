from datetime import datetime, timedelta, time, date
from calendar import Calendar as _Calendar, MONDAY as _MONDAY
from email.message import EmailMessage
import os
import secrets
import smtplib
from flask import Flask, render_template, request, redirect, url_for, flash, abort, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
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


def student_has_active_appointment(student_id):
    return Appointment.query.filter_by(
        student_id=student_id
    ).filter(Appointment.status.in_(['pending', 'approved'])).first() is not None


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
        return f"{self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')}"


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
    mood_rating = db.Column(db.String(20))
    mood_description = db.Column(db.String(200))
    contact_email = db.Column(db.String(120))
    contact_phone = db.Column(db.String(20))
    is_guest = db.Column(db.Boolean, default=False)

    student = db.relationship('User', foreign_keys=[student_id], backref=db.backref('student_appointments', lazy=True))
    counselor = db.relationship('User', foreign_keys=[counselor_id], backref=db.backref('counselor_appointments', lazy=True))

    __table_args__ = (db.UniqueConstraint('counselor_id', 'date', 'start_time', name='_counselor_date_time_uc'),)

    @property
    def time_str(self):
        return f"{self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')}"

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

        for appt in Appointment.query.filter_by(counselor_id=counselor_id, date=date).filter(
            Appointment.status.in_(['pending', 'approved'])
        ).all():
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
            'label': f"{slot_start_t.strftime('%H:%M')} - {slot_end_t.strftime('%H:%M')}",
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


app.jinja_env.filters['fmt_long'] = fmt_date_long
app.jinja_env.filters['fmt_short'] = fmt_date_short


@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.is_counselor:
            return redirect(url_for('counselor_dashboard'))
        else:
            return redirect(url_for('student_dashboard'))
    counselors = User.query.filter_by(role='counselor').all()
    return render_template('index.html', counselors=counselors)


@app.route('/toggle-counselor-access', methods=['POST'])
def toggle_counselor_access():
    session['counselor_access'] = not session.get('counselor_access', False)
    if session['counselor_access']:
        flash('Counselor mode enabled — use Counselor Login / Sign Up above.', 'info')
    else:
        flash('Switched back to Student mode.', 'info')
    return redirect(request.referrer or url_for('index'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    counselor_access = session.get('counselor_access', False)
    if request.method == 'POST':
        if counselor_access:
            role = 'counselor'
        else:
            role = 'student'
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        full_name = request.form.get('full_name')
        specialty = request.form.get('specialty')
        student_id = request.form.get('student_id')
        phone = request.form.get('phone')

        if role not in ['student', 'counselor']:
            flash('Invalid role selected', 'danger')
            return redirect(url_for('register'))
        if User.query.filter_by(username=username).first():
            flash('Username already taken', 'danger')
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash('Email already registered', 'danger')
            return redirect(url_for('register'))

        user = User(
            username=username,
            email=email,
            role=role,
            full_name=full_name,
            specialty=specialty if role == 'counselor' else None,
            student_id=student_id if role == 'student' else None,
            phone=phone
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', counselor_access=counselor_access)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    counselor_access = session.get('counselor_access', False)
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if counselor_access and not user.is_counselor:
                flash('This is counselor login mode. Use default mode for student login.', 'danger')
                return redirect(url_for('login'))
            if not counselor_access and user.is_counselor:
                flash('Counselor login requires counselor access. Double-click the title.', 'danger')
                return redirect(url_for('login'))
            login_user(user)
            next_page = request.args.get('next')
            flash(f'Welcome back, {user.full_name}!', 'success')
            return redirect(next_page or url_for('index'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html', counselor_access=counselor_access)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/counselor/test-email', methods=['POST'])
@login_required
def counselor_test_email():
    if not current_user.is_counselor:
        abort(403)
    recipient = current_user.email
    body = (
        f"Hello {current_user.full_name},\n\n"
        "This is a test email from the Appointment System to verify your SMTP settings.\n"
        f"Server: {app.config.get('MAIL_SERVER', '(not set)')}\n"
        f"Port: {app.config.get('MAIL_PORT', '(not set)')}\n"
        f"Sender: {app.config.get('MAIL_DEFAULT_SENDER', '(not set)')}\n\n"
        "If you received this, password reset emails and other student notifications will also work.\n"
    )
    if send_email(recipient, 'Appointment System - Test Email', body):
        missing = missing_email_settings()
        if missing:
            flash(
                f"Test email sent to {recipient}, but consider filling in: {', '.join(missing)}.",
                'success'
            )
        else:
            flash(f"Test email sent successfully to {recipient}.", 'success')
    else:
        missing = missing_email_settings()
        if missing:
            flash(
                "Test email not sent because these settings are missing: "
                f"{', '.join(missing)}. Fill them in the .env file and restart the app.",
                'danger'
            )
        else:
            flash(
                "Test email failed. Verify your Gmail App Password in .env, "
                "that 2FA is enabled on the Google account, and that outbound port 587 is allowed.",
                'danger'
            )
    return redirect(url_for('counselor_dashboard'))


@app.route('/counselor/dashboard')
@login_required
def counselor_dashboard():
    if not current_user.is_counselor:
        abort(403)
    today = datetime.now().date()
    upcoming_appointments = Appointment.query.filter_by(
        counselor_id=current_user.id
    ).filter(Appointment.date >= today).order_by(Appointment.date, Appointment.start_time).all()
    pending_appointments = [a for a in upcoming_appointments if a.status == 'pending']

    upcoming_blocked = BlockedTime.query.filter_by(
        counselor_id=current_user.id
    ).filter(BlockedTime.date >= today).order_by(BlockedTime.date, BlockedTime.start_time).all()

    return render_template('counselor_dashboard.html',
                           appointments=upcoming_appointments,
                           pending_appointments=pending_appointments,
                           blocked_times=upcoming_blocked)


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
    else:
        return redirect(url_for('student_dashboard'))


@app.route('/student/dashboard')
@login_required
def student_dashboard():
    if not current_user.is_student:
        abort(403)
    today = datetime.now().date()
    my_appointments = Appointment.query.filter_by(
        student_id=current_user.id
    ).order_by(Appointment.date.desc(), Appointment.start_time.desc()).all()
    counselors = User.query.filter_by(role='counselor').all()
    can_book = not student_has_active_appointment(current_user.id)
    return render_template('student_dashboard.html',
                           appointments=my_appointments,
                           counselors=counselors,
                           today=today,
                           can_book=can_book)


@app.route('/counselor/<int:counselor_id>/book', methods=['GET', 'POST'])
@login_required
def book_appointment(counselor_id):
    if not current_user.is_student:
        abort(403)
    counselor = User.query.get_or_404(counselor_id)
    if not counselor.is_counselor:
        abort(404)

    if student_has_active_appointment(current_user.id):
        flash('You already have an active appointment. Cancel it first to book a new one.', 'danger')
        return redirect(url_for('student_dashboard'))

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
            return redirect(url_for('book_appointment', counselor_id=counselor_id, date=normalized.strftime('%Y-%m-%d')))
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
        if student_has_active_appointment(current_user.id):
            flash('You already have an active appointment. Cancel it first to book a new one.', 'danger')
            return redirect(url_for('student_dashboard'))

        slot_key = request.form.get('slot_key')
        purpose = request.form.get('purpose')
        if not slot_key or not purpose:
            flash('Please select a time slot and enter a purpose.', 'danger')
            return redirect(url_for('book_appointment', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        try:
            start_str, end_str = slot_key.split('_')
            start_h, start_m = int(start_str[:2]), int(start_str[2:])
            end_h, end_m = int(end_str[:2]), int(end_str[2:])
            slot_start = time(start_h, start_m)
            slot_end = time(end_h, end_m)
        except Exception:
            flash('Invalid time slot selected.', 'danger')
            return redirect(url_for('book_appointment', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        refreshed_slots = generate_available_slots(counselor_id, selected_date)
        valid_keys = {s['key'] for s in refreshed_slots}
        if slot_key not in valid_keys:
            flash('This slot is no longer available.', 'danger')
            return redirect(url_for('book_appointment', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        existing = Appointment.query.filter_by(
            counselor_id=counselor_id,
            date=selected_date,
            start_time=slot_start
        ).filter(Appointment.status.in_(['pending', 'approved'])).first()
        if existing:
            flash('This slot is no longer available.', 'danger')
            return redirect(url_for('book_appointment', counselor_id=counselor_id, date=selected_date.strftime('%Y-%m-%d')))

        appointment = Appointment(
            counselor_id=counselor_id,
            student_id=current_user.id,
            date=selected_date,
            start_time=slot_start,
            end_time=slot_end,
            purpose=purpose
        )
        db.session.add(appointment)
        db.session.commit()
        flash('Appointment booked successfully! Waiting for counselor approval.', 'success')
        return redirect(url_for('student_dashboard'))

    dates = generate_weekdays(today, 14)
    calendar = build_calendar_month(view_month, today, selected_date, counselor_id)
    return render_template('book_appointment.html',
                           counselor=counselor,
                           available_slots=available_slots,
                           selected_date=selected_date,
                           dates=dates,
                           calendar=calendar,
                           view_month=view_month,
                           weekday_names=WEEKDAY_NAMES,
                           default_start=DEFAULT_WORK_START.strftime('%H:%M'),
                           default_end=DEFAULT_WORK_END.strftime('%H:%M'))


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
    return Appointment.query.filter(
        Appointment.is_guest == True,
        Appointment.first_name == first_name.strip() if first_name else None,
        Appointment.last_name == last_name.strip() if last_name else None,
        Appointment.birthday == bd,
        Appointment.status.in_(['pending', 'approved'])
    ).first() is not None


@app.route('/counselor/<int:counselor_id>/walkin', methods=['GET', 'POST'])
def book_appointment_public(counselor_id):
    counselor = User.query.get_or_404(counselor_id)
    if not counselor.is_counselor:
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
        mood_rating = request.form.get('mood_rating', '').strip() or None
        mood_description = request.form.getlist('mood_description')
        mood_note = request.form.get('mood_note', '').strip()
        contact_email = request.form.get('contact_email', '').strip() or None
        contact_phone = request.form.get('contact_phone', '').strip() or None

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
        ).filter(Appointment.status.in_(['pending', 'approved'])).first()
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
            mood_rating=mood_rating,
            mood_description=mood_desc_joined,
            contact_email=contact_email,
            contact_phone=contact_phone,
            is_guest=True,
        )
        db.session.add(appointment)
        db.session.commit()
        flash('Appointment request submitted! Please wait for counselor approval. You may check back later for updates.', 'success')
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
                           default_start=DEFAULT_WORK_START.strftime('%H:%M'),
                           default_end=DEFAULT_WORK_END.strftime('%H:%M'),
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
            ('section', ''), ('year_level', ''), ('mood_rating', ''), ('mood_description', ''),
            ('contact_email', ''), ('contact_phone', ''), ('is_guest', ''),
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
                    ('mood_rating', 'VARCHAR(20)'), ('mood_description', 'VARCHAR(200)'),
                    ('contact_email', 'VARCHAR(120)'), ('contact_phone', 'VARCHAR(20)'),
                    ('is_guest', 'BOOLEAN DEFAULT 0'),
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
