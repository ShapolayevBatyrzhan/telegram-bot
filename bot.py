import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from dotenv import load_dotenv
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


load_dotenv()


def normalize_phone(phone: str) -> str:
    return "".join(character for character in phone if character.isdigit())


def default_database_path() -> str:
    return "/data/alliance_bot.sqlite3" if Path("/data").exists() else "alliance_bot.sqlite3"


def default_certificates_dir() -> str:
    return "/data/certificates" if Path("/data").exists() else "certificates"


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH") or default_database_path()
CERTIFICATES_DIR = Path(os.getenv("CERTIFICATES_DIR") or default_certificates_dir())
ASSETS_DIR = Path("assets")
LOGO_PATH = ASSETS_DIR / "alliance-logo.jpeg"
PDF_FONT_PATH = ASSETS_DIR / "app-font.ttf"

ALLIANCE_NAME = os.getenv("ALLIANCE_NAME", "Гражданский Альянс города Астаны")
ALLIANCE_BIN = os.getenv("ALLIANCE_BIN", "")
ALLIANCE_EMAIL = os.getenv("ALLIANCE_EMAIL", "")
ALLIANCE_PHONE = os.getenv("ALLIANCE_PHONE", "")
ALLIANCE_ADDRESS = os.getenv("ALLIANCE_ADDRESS", "")
ALLIANCE_WEBSITE = os.getenv("ALLIANCE_WEBSITE", "")
ALLIANCE_SOCIAL_URL = os.getenv("ALLIANCE_SOCIAL_URL", "")
CERTIFICATE_VERIFY_BASE_URL = os.getenv("CERTIFICATE_VERIFY_BASE_URL", ALLIANCE_WEBSITE)
REGISTRATION_WEBAPP_URL = os.getenv("REGISTRATION_WEBAPP_URL", "")
PORT = int(os.getenv("PORT", "8080"))
MINI_APP_DIR = Path("miniapp")
CHAIRMAN_NAME = os.getenv("CHAIRMAN_NAME", "Утеуова Аяжан Дюсембаевна")
CHAIRMAN_POSITION = os.getenv("CHAIRMAN_POSITION", "Председатель Гражданского Альянса города Астаны")

ADMIN_IDS = {
    int(admin_id.strip())
    for admin_id in os.getenv("ADMIN_IDS", "").split(",")
    if admin_id.strip().isdigit()
}
ADMIN_PHONES = {
    normalize_phone(phone)
    for phone in os.getenv("ADMIN_PHONES", "").split(",")
    if normalize_phone(phone)
}

router = Router()


class Registration(StatesGroup):
    waiting_for_phone = State()
    waiting_for_full_name = State()
    waiting_for_iin = State()
    waiting_for_activity = State()


class Application(StatesGroup):
    confirming_identity = State()
    waiting_for_activity = State()
    waiting_for_company = State()
    waiting_for_bin = State()
    preview = State()


class AdminReview(StatesGroup):
    waiting_for_approval_comment = State()
    waiting_for_rejection_reason = State()


@dataclass(frozen=True)
class UserProfile:
    telegram_id: int
    phone: str
    full_name: str
    iin: str
    activity: str


@dataclass(frozen=True)
class MembershipApplication:
    id: int
    telegram_id: int
    full_name: str
    phone: str
    iin: str
    user_activity: str
    application_activity: str
    company_name: str
    org_bin: str
    status: str
    admin_comment: str | None
    certificate_path: str | None
    created_at: str
    reviewed_at: str | None


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(connect_db()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL,
                phone_normalized TEXT,
                full_name TEXT NOT NULL,
                iin TEXT NOT NULL,
                activity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                application_activity TEXT NOT NULL,
                company_name TEXT NOT NULL,
                org_bin TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                admin_id INTEGER,
                admin_comment TEXT,
                certificate_path TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS pending_registrations (
                telegram_id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL,
                phone_normalized TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
        if "phone_normalized" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN phone_normalized TEXT")

        app_columns = {row["name"] for row in connection.execute("PRAGMA table_info(applications)").fetchall()}
        if "org_bin" not in app_columns:
            connection.execute("ALTER TABLE applications ADD COLUMN org_bin TEXT NOT NULL DEFAULT ''")
        if "certificate_path" not in app_columns:
            connection.execute("ALTER TABLE applications ADD COLUMN certificate_path TEXT")
        if "reviewed_at" not in app_columns:
            connection.execute("ALTER TABLE applications ADD COLUMN reviewed_at TEXT")

        users_without_phone_key = connection.execute(
            "SELECT telegram_id, phone FROM users WHERE phone_normalized IS NULL OR phone_normalized = ''"
        ).fetchall()
        for user in users_without_phone_key:
            connection.execute(
                "UPDATE users SET phone_normalized = ? WHERE telegram_id = ?",
                (normalize_phone(user["phone"]), user["telegram_id"]),
            )

        try:
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_normalized ON users(phone_normalized)"
            )
        except sqlite3.IntegrityError:
            logging.warning("Cannot create unique phone index: duplicate phones already exist.")

        connection.commit()


def save_pending_registration(telegram_id: int, phone: str) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT INTO pending_registrations (telegram_id, phone, phone_normalized, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                phone = excluded.phone,
                phone_normalized = excluded.phone_normalized,
                created_at = excluded.created_at
            """,
            (telegram_id, phone, normalize_phone(phone), datetime.now().isoformat(timespec="seconds")),
        )
        connection.commit()


def get_pending_registration(telegram_id: int) -> sqlite3.Row | None:
    with closing(connect_db()) as connection:
        return connection.execute(
            "SELECT telegram_id, phone, phone_normalized, created_at FROM pending_registrations WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()


def delete_pending_registration(telegram_id: int) -> None:
    with closing(connect_db()) as connection:
        connection.execute("DELETE FROM pending_registrations WHERE telegram_id = ?", (telegram_id,))
        connection.commit()


def upsert_user(telegram_id: int, phone: str, full_name: str, iin: str, activity: str) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT INTO users (telegram_id, phone, phone_normalized, full_name, iin, activity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                phone = excluded.phone,
                phone_normalized = excluded.phone_normalized,
                full_name = excluded.full_name,
                iin = excluded.iin,
                activity = excluded.activity
            """,
            (
                telegram_id,
                phone,
                normalize_phone(phone),
                full_name,
                iin,
                activity,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        connection.commit()


def user_from_row(row: sqlite3.Row) -> UserProfile:
    return UserProfile(
        telegram_id=row["telegram_id"],
        phone=row["phone"],
        full_name=row["full_name"],
        iin=row["iin"],
        activity=row["activity"],
    )


def get_user(telegram_id: int) -> UserProfile | None:
    with closing(connect_db()) as connection:
        row = connection.execute(
            "SELECT telegram_id, phone, full_name, iin, activity FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return user_from_row(row) if row else None


def get_user_by_phone(phone: str) -> UserProfile | None:
    with closing(connect_db()) as connection:
        row = connection.execute(
            """
            SELECT telegram_id, phone, full_name, iin, activity
            FROM users
            WHERE phone_normalized = ?
            """,
            (normalize_phone(phone),),
        ).fetchone()
    return user_from_row(row) if row else None


def create_application(telegram_id: int, activity: str, company_name: str, org_bin: str) -> int:
    with closing(connect_db()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO applications (telegram_id, application_activity, company_name, org_bin, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, activity, company_name, org_bin, datetime.now().isoformat(timespec="seconds")),
        )
        connection.commit()
        return int(cursor.lastrowid)


def application_select_sql(where_clause: str) -> str:
    return f"""
        SELECT
            a.id,
            a.telegram_id,
            u.full_name,
            u.phone,
            u.iin,
            u.activity AS user_activity,
            a.application_activity,
            a.company_name,
            a.org_bin,
            a.status,
            a.admin_comment,
            a.certificate_path,
            a.created_at,
            a.reviewed_at
        FROM applications a
        JOIN users u ON u.telegram_id = a.telegram_id
        {where_clause}
    """


def application_from_row(row: sqlite3.Row) -> MembershipApplication:
    return MembershipApplication(
        id=row["id"],
        telegram_id=row["telegram_id"],
        full_name=row["full_name"],
        phone=row["phone"],
        iin=row["iin"],
        user_activity=row["user_activity"],
        application_activity=row["application_activity"],
        company_name=row["company_name"],
        org_bin=row["org_bin"],
        status=row["status"],
        admin_comment=row["admin_comment"],
        certificate_path=row["certificate_path"],
        created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
    )


def get_application(application_id: int) -> MembershipApplication | None:
    with closing(connect_db()) as connection:
        row = connection.execute(application_select_sql("WHERE a.id = ?"), (application_id,)).fetchone()
    return application_from_row(row) if row else None


def list_pending_applications() -> list[MembershipApplication]:
    with closing(connect_db()) as connection:
        rows = connection.execute(
            application_select_sql("WHERE a.status = 'pending' ORDER BY a.created_at ASC")
        ).fetchall()
    return [application_from_row(row) for row in rows]


def list_issued_certificates(limit: int = 30) -> list[MembershipApplication]:
    with closing(connect_db()) as connection:
        rows = connection.execute(
            application_select_sql(
                "WHERE a.status = 'approved' ORDER BY COALESCE(a.reviewed_at, a.created_at) DESC LIMIT ?"
            ),
            (limit,),
        ).fetchall()
    return [application_from_row(row) for row in rows]


def get_last_application(telegram_id: int) -> MembershipApplication | None:
    with closing(connect_db()) as connection:
        row = connection.execute(
            application_select_sql("WHERE a.telegram_id = ? ORDER BY a.created_at DESC LIMIT 1"),
            (telegram_id,),
        ).fetchone()
    return application_from_row(row) if row else None


def update_application_status(
    application_id: int,
    status: str,
    admin_id: int,
    comment: str,
    certificate_path: str | None = None,
) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            UPDATE applications
            SET status = ?, admin_id = ?, admin_comment = ?, certificate_path = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (
                status,
                admin_id,
                comment,
                certificate_path,
                datetime.now().isoformat(timespec="seconds"),
                application_id,
            ),
        )
        connection.commit()


def is_admin(telegram_id: int) -> bool:
    if telegram_id in ADMIN_IDS:
        return True

    profile = get_user(telegram_id)
    if profile is None:
        return False

    return normalize_phone(profile.phone) in ADMIN_PHONES


def get_admin_telegram_ids() -> set[int]:
    admin_ids = set(ADMIN_IDS)
    if not ADMIN_PHONES:
        return admin_ids

    placeholders = ",".join("?" for _ in ADMIN_PHONES)
    with closing(connect_db()) as connection:
        rows = connection.execute(
            f"SELECT telegram_id FROM users WHERE phone_normalized IN ({placeholders})",
            tuple(ADMIN_PHONES),
        ).fetchall()
    admin_ids.update(int(row["telegram_id"]) for row in rows)
    return admin_ids


def registration_button() -> KeyboardButton:
    return KeyboardButton(text="Регистрация")


def registration_webapp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Продолжить регистрацию",
                    web_app=WebAppInfo(url=REGISTRATION_WEBAPP_URL),
                )
            ]
        ]
    )


def guest_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Вход"), registration_button()],
            [KeyboardButton(text="Новости")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def user_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Заявление на вступление")],
            [KeyboardButton(text="Новости"), KeyboardButton(text="Мой статус")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Новые заявления"), KeyboardButton(text="Реестр сертификатов")],
            [KeyboardButton(text="Заявление на вступление")],
            [KeyboardButton(text="Новости"), KeyboardButton(text="Мой статус")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Панель администратора",
    )


def main_menu(telegram_id: int | None = None) -> ReplyKeyboardMarkup:
    if telegram_id is not None:
        profile = get_user(telegram_id)
        if profile is None:
            return guest_menu()
        if is_admin(telegram_id):
            return admin_menu()
        return user_menu()
    return guest_menu()


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def yes_no_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def application_preview_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Подать"), KeyboardButton(text="Отредактировать")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def admin_application_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Одобрить", callback_data=f"approve:{application_id}"),
                InlineKeyboardButton(text="Отказать", callback_data=f"reject:{application_id}"),
            ]
        ]
    )


def pending_applications_keyboard(applications: Iterable[MembershipApplication]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for application in applications:
        builder.button(text=f"Заявка #{application.id} - {application.company_name}", callback_data=f"view:{application.id}")
    builder.adjust(1)
    return builder.as_markup()


def contact_links_keyboard() -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    if ALLIANCE_WEBSITE:
        buttons.append([InlineKeyboardButton(text="Официальный сайт", url=ALLIANCE_WEBSITE)])
    if ALLIANCE_SOCIAL_URL:
        buttons.append([InlineKeyboardButton(text="Социальные сети", url=ALLIANCE_SOCIAL_URL)])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def format_iso_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def certificate_number(application_id: int, join_date: datetime) -> str:
    return f"GAA-{join_date.year}-{application_id:04d}"


def certificate_number_for_application(application: MembershipApplication) -> str:
    date = parse_iso_datetime(application.reviewed_at) or parse_iso_datetime(application.created_at) or datetime.now()
    return certificate_number(application.id, date)


def certificate_qr_link(number: str) -> str:
    base_url = CERTIFICATE_VERIFY_BASE_URL.rstrip("/")
    if base_url:
        return f"{base_url}/certificate/{number}"
    return f"certificate:{number}"


def render_application_preview(user: UserProfile, activity: str, company_name: str, org_bin: str) -> str:
    return (
        "<b>Проверьте заявление</b>\n\n"
        f"ФИО представителя: {user.full_name}\n"
        f"Телефон: {user.phone}\n"
        f"ИИН представителя: {user.iin}\n"
        f"Вид деятельности: {activity}\n"
        f"Наименование организации: {company_name}\n"
        f"БИН организации: {org_bin}"
    )


def render_application_for_admin(application: MembershipApplication) -> str:
    return (
        f"<b>Заявка #{application.id}</b>\n\n"
        f"ФИО представителя: {application.full_name}\n"
        f"Телефон: {application.phone}\n"
        f"ИИН представителя: {application.iin}\n"
        f"Деятельность при регистрации: {application.user_activity}\n"
        f"Деятельность в заявке: {application.application_activity}\n"
        f"Организация: {application.company_name}\n"
        f"БИН организации: {application.org_bin}\n"
        f"Статус: {application.status}\n"
        f"Дата подачи: {format_iso_date(application.created_at)}"
    )


def render_certificate_registry(applications: list[MembershipApplication]) -> str:
    if not applications:
        return "Выданных сертификатов пока нет."

    lines = ["<b>Реестр выданных сертификатов</b>"]
    for application in applications:
        number = certificate_number_for_application(application)
        issued_at = format_iso_date(application.reviewed_at or application.created_at)
        comment = f"\nКомментарий: {application.admin_comment}" if application.admin_comment else ""
        lines.append(
            f"\n<b>{number}</b>\n"
            f"Организация: {application.company_name}\n"
            f"БИН: {application.org_bin}\n"
            f"Выдан: {issued_at}\n"
            f"Заявка: #{application.id}{comment}"
        )
    return "\n".join(lines)


def render_approved_message(application: MembershipApplication) -> str:
    contacts = []
    if ALLIANCE_WEBSITE:
        contacts.append(f"Сайт: {ALLIANCE_WEBSITE}")
    if ALLIANCE_SOCIAL_URL:
        contacts.append(f"Социальные сети: {ALLIANCE_SOCIAL_URL}")
    if ALLIANCE_EMAIL:
        contacts.append(f"Почта: {ALLIANCE_EMAIL}")
    if ALLIANCE_PHONE:
        contacts.append(f"Телефон: {ALLIANCE_PHONE}")
    if ALLIANCE_BIN:
        contacts.append(f"БИН Альянса: {ALLIANCE_BIN}")
    if ALLIANCE_ADDRESS:
        contacts.append(f"Адрес: {ALLIANCE_ADDRESS}")

    contact_block = "\n\nКонтакты Альянса:\n" + "\n".join(contacts) if contacts else ""
    return (
        f"Уважаемые представители {application.company_name}!\n\n"
        f"Ваша заявка на вступление в {ALLIANCE_NAME} одобрена.\n\n"
        "Вы включены в состав членов Альянса.\n\n"
        "Ваше свидетельство о членстве прикреплено к данному сообщению.\n\n"
        f"Добро пожаловать в {ALLIANCE_NAME}!"
        f"{contact_block}"
    )


def register_pdf_font() -> str:
    font_candidates = [
        PDF_FONT_PATH,
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
    ]
    for font_path in font_candidates:
        if font_path.exists():
            pdfmetrics.registerFont(TTFont("AppFont", str(font_path)))
            return "AppFont"
    return "Helvetica"


def draw_wrapped_text(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_width: float,
    font_name: str,
    font_size: int,
    leading: int,
    color=colors.HexColor("#0A2342"),
    centered: bool = False,
) -> float:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if pdf.stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    pdf.setFillColor(color)
    pdf.setFont(font_name, font_size)
    for line in lines:
        if centered:
            pdf.drawCentredString(x + max_width / 2, y, line)
        else:
            pdf.drawString(x, y, line)
        y -= leading
    return y


def draw_qr(pdf: canvas.Canvas, value: str, x: float, y: float, size: float) -> None:
    qr = QrCodeWidget(value)
    bounds = qr.getBounds()
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    drawing = Drawing(size, size, transform=[size / width, 0, 0, size / height, 0, 0])
    drawing.add(qr)
    renderPDF.draw(drawing, pdf, x, y)


def draw_pdf_background(pdf: canvas.Canvas, page_width: float, page_height: float) -> None:
    navy = colors.HexColor("#08213F")
    coral = colors.HexColor("#E85D4F")
    light_coral = colors.HexColor("#F4B0A8")

    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, page_width, page_height, fill=True, stroke=False)
    pdf.setFillColor(navy)
    pdf.rect(0, 0, 18, page_height, fill=True, stroke=False)
    pdf.rect(page_width - 18, 0, 18, page_height, fill=True, stroke=False)

    pdf.setStrokeColor(coral)
    pdf.setLineWidth(1.5)
    pdf.line(22, page_height - 2, 130, page_height - 64)
    pdf.line(page_width - 24, 0, page_width - 135, 65)

    pdf.setStrokeColor(light_coral)
    pdf.setLineWidth(1.2)
    pdf.roundRect(28, 22, page_width - 56, page_height - 44, 6, stroke=True, fill=False)
    pdf.setLineWidth(0.5)
    pdf.roundRect(34, 28, page_width - 68, page_height - 56, 4, stroke=True, fill=False)


def generate_certificate(application: MembershipApplication, comment: str) -> Path:
    CERTIFICATES_DIR.mkdir(parents=True, exist_ok=True)
    join_date = datetime.now()
    number = certificate_number(application.id, join_date)
    qr_link = certificate_qr_link(number)
    certificate_path = CERTIFICATES_DIR / f"certificate_{number}.pdf"
    font_name = register_pdf_font()

    page_width, page_height = landscape(A4)
    pdf = canvas.Canvas(str(certificate_path), pagesize=landscape(A4))
    draw_pdf_background(pdf, page_width, page_height)

    navy = colors.HexColor("#08213F")
    coral = colors.HexColor("#E85D4F")
    muted = colors.HexColor("#53606F")

    if LOGO_PATH.exists():
        pdf.drawImage(ImageReader(str(LOGO_PATH)), 118, page_height - 116, width=160, height=95, preserveAspectRatio=True, mask="auto")

    pdf.setFillColor(navy)
    pdf.setFont(font_name, 38)
    pdf.drawCentredString(330, page_height - 185, "СВИДЕТЕЛЬСТВО")
    pdf.setFillColor(coral)
    pdf.setFont(font_name, 22)
    pdf.drawCentredString(330, page_height - 220, "О ЧЛЕНСТВЕ")

    pdf.setStrokeColor(colors.HexColor("#F0B5AD"))
    pdf.line(214, page_height - 240, 446, page_height - 240)

    y = page_height - 278
    y = draw_wrapped_text(pdf, "Настоящим подтверждается, что", 105, y, 450, font_name, 12, 18, muted, centered=True)
    y -= 8
    company_font_size = 24 if len(application.company_name) <= 45 else 18
    y = draw_wrapped_text(pdf, application.company_name, 105, y, 450, font_name, company_font_size, 28, navy, centered=True)
    y -= 4
    pdf.setFillColor(navy)
    pdf.setFont(font_name, 13)
    pdf.drawCentredString(330, y, f"БИН: {application.org_bin}")
    y -= 34
    y = draw_wrapped_text(
        pdf,
        f"на основании электронного заявления о вступлении от {format_iso_date(application.created_at)} "
        f"и решения {ALLIANCE_NAME} принят(а) в состав членов",
        124,
        y,
        410,
        font_name,
        12,
        17,
        navy,
        centered=True,
    )
    y -= 2
    pdf.setFillColor(coral)
    pdf.setFont(font_name, 14)
    pdf.drawCentredString(330, y, ALLIANCE_NAME)

    if comment:
        y -= 28
        draw_wrapped_text(pdf, f"Комментарий: {comment}", 124, y, 410, font_name, 10, 14, muted, centered=True)

    pdf.setFillColor(navy)
    pdf.setFont(font_name, 10)
    pdf.drawString(92, 142, "Председатель")
    pdf.drawString(92, 128, "Гражданского Альянса")
    pdf.drawString(92, 114, "города Астаны")
    pdf.setStrokeColor(navy)
    pdf.setLineWidth(1)
    pdf.line(92, 92, 260, 92)
    pdf.setFillColor(navy)
    pdf.setFont(font_name, 10)
    pdf.drawString(92, 74, CHAIRMAN_NAME)
    pdf.setFont(font_name, 9)
    pdf.drawCentredString(418, 86, "М.П.")
    pdf.setStrokeColor(colors.HexColor("#F0B5AD"))
    pdf.line(160, 48, 520, 48)
    pdf.setFillColor(navy)
    pdf.setFont(font_name, 9)
    pdf.drawCentredString(340, 30, "ВМЕСТЕ РАЗВИВАЕМ ГРАЖДАНСКОЕ ОБЩЕСТВО!")

    side_x = 610
    pdf.setStrokeColor(colors.HexColor("#F0B5AD"))
    pdf.line(side_x - 24, 128, side_x - 24, page_height - 185)

    side_items = [
        ("Дата вступления", join_date.strftime("%d.%m.%Y")),
        ("Номер свидетельства", number),
        ("Статус", "Член Гражданского\nАльянса города Астаны"),
        ("Дата выдачи", join_date.strftime("%d.%m.%Y")),
    ]
    side_y = page_height - 218
    for title, value in side_items:
        pdf.setFillColor(navy)
        pdf.setFont(font_name, 11)
        pdf.drawString(side_x, side_y, title)
        pdf.setFillColor(coral)
        pdf.setFont(font_name, 11)
        value_y = side_y - 17
        for line in value.split("\n"):
            pdf.drawString(side_x, value_y, line)
            value_y -= 14
        pdf.setStrokeColor(colors.HexColor("#F0B5AD"))
        pdf.line(side_x, value_y - 8, side_x + 165, value_y - 8)
        side_y = value_y - 26

    draw_qr(pdf, qr_link, side_x + 10, 102, 82)
    pdf.setFillColor(navy)
    pdf.setFont(font_name, 8)
    pdf.drawString(side_x, 82, "Проверка свидетельства")
    pdf.drawString(side_x, 70, "на официальном сайте")

    pdf.save()
    return certificate_path


async def notify_admins(bot: Bot, application_id: int) -> None:
    application = get_application(application_id)
    if application is None:
        return

    text = "Поступило новое заявление.\n\n" + render_application_for_admin(application)
    for admin_id in get_admin_telegram_ids():
        try:
            await bot.send_message(admin_id, text, reply_markup=admin_application_keyboard(application_id))
        except Exception:
            logging.exception("Could not notify admin %s", admin_id)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"Здравствуйте! Это бот {ALLIANCE_NAME}. Выберите действие:",
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(F.text == "Вход")
async def login(message: Message) -> None:
    profile = get_user(message.from_user.id)
    if profile is None:
        await message.answer("Вы еще не зарегистрированы. Нажмите «Регистрация».", reply_markup=main_menu(message.from_user.id))
        return

    await message.answer(
        f"Вход выполнен.\n\nФИО: {profile.full_name}\nТелефон: {profile.phone}",
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(F.text == "Регистрация")
async def registration_start(message: Message, state: FSMContext) -> None:
    profile = get_user(message.from_user.id)
    if profile is not None:
        await message.answer(
            "Вы уже зарегистрированы. Повторная регистрация недоступна.",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    await state.clear()
    await state.set_state(Registration.waiting_for_phone)
    await message.answer("Нажмите кнопку ниже, чтобы поделиться номером телефона.", reply_markup=phone_keyboard())


@router.message(Registration.waiting_for_phone, F.contact)
async def registration_phone(message: Message, state: FSMContext) -> None:
    if message.contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, отправьте именно свой номер через кнопку Telegram.")
        return

    existing_profile = get_user_by_phone(message.contact.phone_number)
    if existing_profile is not None:
        await state.clear()
        await message.answer(
            "Этот номер телефона уже зарегистрирован. Повторная регистрация с ним недоступна.",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    if REGISTRATION_WEBAPP_URL:
        save_pending_registration(message.from_user.id, message.contact.phone_number)
        await state.clear()
        await message.answer(
            "Номер подтвержден. Нажмите кнопку ниже и заполните форму регистрации.",
            reply_markup=registration_webapp_keyboard(),
        )
        return

    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(Registration.waiting_for_full_name)
    await message.answer("Введите ФИО полностью.", reply_markup=ReplyKeyboardRemove())


@router.message(Registration.waiting_for_phone)
async def registration_phone_invalid(message: Message) -> None:
    await message.answer("Нужно нажать кнопку «Поделиться номером», ручной ввод номера не принимается.")


@router.message(Registration.waiting_for_full_name)
async def registration_full_name(message: Message, state: FSMContext) -> None:
    await state.update_data(full_name=message.text.strip())
    await state.set_state(Registration.waiting_for_iin)
    await message.answer("Введите ИИН.")


@router.message(Registration.waiting_for_iin)
async def registration_iin(message: Message, state: FSMContext) -> None:
    iin = message.text.strip()
    if not iin.isdigit() or len(iin) != 12:
        await message.answer("ИИН должен состоять из 12 цифр. Введите ИИН еще раз.")
        return

    await state.update_data(iin=iin)
    await state.set_state(Registration.waiting_for_activity)
    await message.answer("Чем вы занимаетесь?")


@router.message(Registration.waiting_for_activity)
async def registration_activity(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    upsert_user(
        telegram_id=message.from_user.id,
        phone=data["phone"],
        full_name=data["full_name"],
        iin=data["iin"],
        activity=message.text.strip(),
    )
    await state.clear()
    await message.answer("Регистрация успешно завершена.", reply_markup=main_menu(message.from_user.id))


@router.message(F.text == "Заявление на вступление")
async def application_start(message: Message, state: FSMContext) -> None:
    profile = get_user(message.from_user.id)
    if profile is None:
        await message.answer("Сначала пройдите регистрацию.", reply_markup=main_menu(message.from_user.id))
        return

    await state.clear()
    await state.set_state(Application.confirming_identity)
    await message.answer(f"Это вы?\n\n{profile.full_name}\n{profile.phone}", reply_markup=yes_no_keyboard())


@router.message(Application.confirming_identity, F.text.casefold() == "да")
async def application_identity_confirmed(message: Message, state: FSMContext) -> None:
    await state.set_state(Application.waiting_for_activity)
    await message.answer("Укажите вид деятельности для заявления.")


@router.message(Application.confirming_identity, F.text.casefold() == "нет")
async def application_identity_rejected(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Тогда пройдите регистрацию заново.", reply_markup=main_menu(message.from_user.id))


@router.message(Application.waiting_for_activity)
async def application_activity(message: Message, state: FSMContext) -> None:
    await state.update_data(application_activity=message.text.strip())
    await state.set_state(Application.waiting_for_company)
    await message.answer("Введите полное наименование организации.")


@router.message(Application.waiting_for_company)
async def application_company(message: Message, state: FSMContext) -> None:
    await state.update_data(company_name=message.text.strip())
    await state.set_state(Application.waiting_for_bin)
    await message.answer("Введите БИН организации.")


@router.message(Application.waiting_for_bin)
async def application_bin(message: Message, state: FSMContext) -> None:
    profile = get_user(message.from_user.id)
    if profile is None:
        await state.clear()
        await message.answer("Регистрация не найдена. Пройдите регистрацию.", reply_markup=main_menu(message.from_user.id))
        return

    org_bin = message.text.strip()
    if not org_bin.isdigit() or len(org_bin) != 12:
        await message.answer("БИН должен состоять из 12 цифр. Введите БИН еще раз.")
        return

    await state.update_data(org_bin=org_bin)
    data = await state.get_data()
    await state.set_state(Application.preview)
    await message.answer(
        render_application_preview(profile, data["application_activity"], data["company_name"], data["org_bin"]),
        reply_markup=application_preview_keyboard(),
    )


@router.message(Application.preview, F.text == "Отредактировать")
async def application_edit(message: Message, state: FSMContext) -> None:
    await state.set_state(Application.waiting_for_activity)
    await message.answer("Хорошо, начнем правку. Укажите вид деятельности для заявления.")


@router.message(Application.preview, F.text == "Подать")
async def application_submit(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    application_id = create_application(
        telegram_id=message.from_user.id,
        activity=data["application_activity"],
        company_name=data["company_name"],
        org_bin=data["org_bin"],
    )
    await state.clear()
    await message.answer(
        f"Заявление №{application_id} успешно зарегистрировано.",
        reply_markup=main_menu(message.from_user.id),
    )
    await notify_admins(bot, application_id)


@router.message(F.text.in_({"Администратор", "Новые заявления"}))
async def admin_panel(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет доступа к разделу администратора.", reply_markup=main_menu(message.from_user.id))
        return

    applications = list_pending_applications()
    if not applications:
        await message.answer("Новых заявлений пока нет.", reply_markup=main_menu(message.from_user.id))
        return

    await message.answer("Новые заявления:", reply_markup=pending_applications_keyboard(applications))


@router.message(F.text == "Реестр сертификатов")
async def certificate_registry(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет доступа к реестру сертификатов.", reply_markup=main_menu(message.from_user.id))
        return

    await message.answer(render_certificate_registry(list_issued_certificates()), reply_markup=main_menu(message.from_user.id))


@router.message(F.text == "Новости")
async def news(message: Message) -> None:
    await message.answer("Раздел новостей готов к подключению. Здесь можно публиковать объявления Альянса.")


@router.message(F.text == "Мой статус")
async def my_status(message: Message) -> None:
    application = get_last_application(message.from_user.id)
    if application is None:
        await message.answer("У вас пока нет поданных заявлений.", reply_markup=main_menu(message.from_user.id))
        return

    status_labels = {
        "pending": "на рассмотрении",
        "approved": "одобрено",
        "rejected": "отказано",
    }
    text = f"Последнее заявление №{application.id}: {status_labels.get(application.status, application.status)}"
    if application.admin_comment:
        text += f"\nКомментарий: {application.admin_comment}"
    await message.answer(text, reply_markup=main_menu(message.from_user.id))


@router.callback_query(F.data.startswith("view:"))
async def admin_view_application(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    application_id = int(callback.data.split(":", 1)[1])
    application = get_application(application_id)
    if application is None:
        await callback.answer("Заявление не найдено", show_alert=True)
        return

    await state.clear()
    await callback.message.answer(
        render_application_for_admin(application),
        reply_markup=admin_application_keyboard(application_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("approve:"))
async def admin_approve_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    application_id = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminReview.waiting_for_approval_comment)
    await state.update_data(application_id=application_id)
    await callback.message.answer(f"Введите комментарий для одобрения заявки #{application_id}. Он будет добавлен в сертификат.")
    await callback.answer()


@router.message(AdminReview.waiting_for_approval_comment)
async def admin_approve_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа.", reply_markup=main_menu(message.from_user.id))
        return

    data = await state.get_data()
    application_id = int(data["application_id"])
    application = get_application(application_id)
    if application is None or application.status != "pending":
        await state.clear()
        await message.answer("Заявление не найдено или уже обработано.", reply_markup=main_menu(message.from_user.id))
        return

    comment = message.text.strip()
    certificate_path = generate_certificate(application, comment)
    update_application_status(
        application_id=application_id,
        status="approved",
        admin_id=message.from_user.id,
        comment=comment,
        certificate_path=str(certificate_path),
    )
    await state.clear()

    await message.answer(f"Заявка #{application_id} одобрена.", reply_markup=main_menu(message.from_user.id))
    await bot.send_message(
        application.telegram_id,
        render_approved_message(application),
        reply_markup=contact_links_keyboard(),
    )
    await bot.send_document(
        application.telegram_id,
        FSInputFile(certificate_path),
        caption="Ваше свидетельство о членстве.",
    )


@router.callback_query(F.data.startswith("reject:"))
async def admin_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    application_id = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminReview.waiting_for_rejection_reason)
    await state.update_data(application_id=application_id)
    await callback.message.answer(f"Введите причину отказа по заявке #{application_id}.")
    await callback.answer()


@router.message(AdminReview.waiting_for_rejection_reason)
async def admin_reject_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа.", reply_markup=main_menu(message.from_user.id))
        return

    data = await state.get_data()
    application_id = int(data["application_id"])
    application = get_application(application_id)
    if application is None or application.status != "pending":
        await state.clear()
        await message.answer("Заявление не найдено или уже обработано.", reply_markup=main_menu(message.from_user.id))
        return

    reason = message.text.strip()
    update_application_status(
        application_id=application_id,
        status="rejected",
        admin_id=message.from_user.id,
        comment=reason,
    )
    await state.clear()

    await message.answer(f"По заявке #{application_id} отправлен отказ.", reply_markup=main_menu(message.from_user.id))
    await bot.send_message(
        application.telegram_id,
        f"По вашему заявлению №{application_id} отказ.\nПричина: {reason}",
    )


def validate_telegram_init_data(init_data: str) -> int | None:
    if not init_data or not BOT_TOKEN:
        return None

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    auth_date = values.get("auth_date", "")
    if not received_hash or not auth_date.isdigit():
        return None
    if abs(time.time() - int(auth_date)) > 3600:
        return None

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    try:
        user_data = json.loads(values["user"])
        return int(user_data["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def mini_app_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(MINI_APP_DIR / "index.html")


async def mini_app_logo(request: web.Request) -> web.FileResponse:
    return web.FileResponse(LOGO_PATH)


async def mini_app_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def mini_app_register(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "Некорректные данные."}, status=400)

    telegram_id = validate_telegram_init_data(str(payload.get("initData", "")))
    if telegram_id is None:
        return web.json_response(
            {"ok": False, "error": "Откройте форму через кнопку регистрации внутри Telegram."},
            status=401,
        )

    pending = get_pending_registration(telegram_id)
    if pending is None:
        return web.json_response(
            {"ok": False, "error": "Сначала подтвердите номер телефона в боте."},
            status=400,
        )

    if get_user(telegram_id) is not None or get_user_by_phone(pending["phone"]) is not None:
        delete_pending_registration(telegram_id)
        return web.json_response({"ok": False, "error": "Этот пользователь или номер уже зарегистрирован."}, status=409)

    full_name = str(payload.get("fullName", "")).strip()
    iin = str(payload.get("iin", "")).strip()
    activity = str(payload.get("activity", "")).strip()
    if len(full_name) < 5:
        return web.json_response({"ok": False, "error": "Введите ФИО полностью."}, status=400)
    if not iin.isdigit() or len(iin) != 12:
        return web.json_response({"ok": False, "error": "ИИН должен состоять из 12 цифр."}, status=400)
    if len(activity) < 3:
        return web.json_response({"ok": False, "error": "Укажите вид деятельности."}, status=400)

    upsert_user(telegram_id, pending["phone"], full_name, iin, activity)
    delete_pending_registration(telegram_id)

    bot: Bot = request.app["bot"]
    await bot.send_message(
        telegram_id,
        "Регистрация успешно завершена.",
        reply_markup=main_menu(telegram_id),
    )
    return web.json_response({"ok": True})


def create_web_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", mini_app_health)
    app.router.add_get("/health", mini_app_health)
    app.router.add_get("/register", mini_app_page)
    app.router.add_get("/assets/alliance-logo.jpeg", mini_app_logo)
    app.router.add_post("/api/register", mini_app_register)
    return app


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill BOT_TOKEN.")

    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    web_runner = web.AppRunner(create_web_app(bot))
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Mini App server started on port %s", PORT)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
