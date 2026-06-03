import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    FSInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "alliance_bot.sqlite3")
CERTIFICATES_DIR = Path(os.getenv("CERTIFICATES_DIR", "certificates"))
ADMIN_IDS = {
    int(admin_id.strip())
    for admin_id in os.getenv("ADMIN_IDS", "").split(",")
    if admin_id.strip().isdigit()
}


router = Router()


def normalize_phone(phone: str) -> str:
    return "".join(character for character in phone if character.isdigit())


class Registration(StatesGroup):
    waiting_for_phone = State()
    waiting_for_full_name = State()
    waiting_for_iin = State()
    waiting_for_activity = State()


class Application(StatesGroup):
    confirming_identity = State()
    waiting_for_activity = State()
    waiting_for_company = State()
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
    status: str
    admin_comment: str | None
    created_at: str


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
                status TEXT NOT NULL DEFAULT 'pending',
                admin_id INTEGER,
                admin_comment TEXT,
                certificate_path TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "phone_normalized" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN phone_normalized TEXT")

        users_without_normalized_phone = connection.execute(
            "SELECT telegram_id, phone FROM users WHERE phone_normalized IS NULL OR phone_normalized = ''"
        ).fetchall()
        for user in users_without_normalized_phone:
            connection.execute(
                "UPDATE users SET phone_normalized = ? WHERE telegram_id = ?",
                (normalize_phone(user["phone"]), user["telegram_id"]),
            )

        try:
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_normalized ON users(phone_normalized)"
            )
        except sqlite3.IntegrityError:
            logging.warning("Could not create unique phone index because duplicate phone numbers already exist.")

        connection.commit()


def upsert_user(telegram_id: int, phone: str, full_name: str, iin: str, activity: str) -> None:
    phone_normalized = normalize_phone(phone)
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
                phone_normalized,
                full_name,
                iin,
                activity,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        connection.commit()


def get_user(telegram_id: int) -> UserProfile | None:
    with closing(connect_db()) as connection:
        row = connection.execute(
            "SELECT telegram_id, phone, full_name, iin, activity FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

    if row is None:
        return None

    return UserProfile(
        telegram_id=row["telegram_id"],
        phone=row["phone"],
        full_name=row["full_name"],
        iin=row["iin"],
        activity=row["activity"],
    )


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

    if row is None:
        return None

    return UserProfile(
        telegram_id=row["telegram_id"],
        phone=row["phone"],
        full_name=row["full_name"],
        iin=row["iin"],
        activity=row["activity"],
    )


def create_application(telegram_id: int, activity: str, company_name: str) -> int:
    with closing(connect_db()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO applications (telegram_id, application_activity, company_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_id, activity, company_name, datetime.now().isoformat(timespec="seconds")),
        )
        connection.commit()
        return int(cursor.lastrowid)


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
        status=row["status"],
        admin_comment=row["admin_comment"],
        created_at=row["created_at"],
    )


def get_application(application_id: int) -> MembershipApplication | None:
    with closing(connect_db()) as connection:
        row = connection.execute(
            """
            SELECT
                a.id,
                a.telegram_id,
                u.full_name,
                u.phone,
                u.iin,
                u.activity AS user_activity,
                a.application_activity,
                a.company_name,
                a.status,
                a.admin_comment,
                a.created_at
            FROM applications a
            JOIN users u ON u.telegram_id = a.telegram_id
            WHERE a.id = ?
            """,
            (application_id,),
        ).fetchone()

    return application_from_row(row) if row else None


def list_pending_applications() -> list[MembershipApplication]:
    with closing(connect_db()) as connection:
        rows = connection.execute(
            """
            SELECT
                a.id,
                a.telegram_id,
                u.full_name,
                u.phone,
                u.iin,
                u.activity AS user_activity,
                a.application_activity,
                a.company_name,
                a.status,
                a.admin_comment,
                a.created_at
            FROM applications a
            JOIN users u ON u.telegram_id = a.telegram_id
            WHERE a.status = 'pending'
            ORDER BY a.created_at ASC
            """
        ).fetchall()

    return [application_from_row(row) for row in rows]


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


def get_last_application(telegram_id: int) -> MembershipApplication | None:
    with closing(connect_db()) as connection:
        row = connection.execute(
            """
            SELECT
                a.id,
                a.telegram_id,
                u.full_name,
                u.phone,
                u.iin,
                u.activity AS user_activity,
                a.application_activity,
                a.company_name,
                a.status,
                a.admin_comment,
                a.created_at
            FROM applications a
            JOIN users u ON u.telegram_id = a.telegram_id
            WHERE a.telegram_id = ?
            ORDER BY a.created_at DESC
            LIMIT 1
            """,
            (telegram_id,),
        ).fetchone()

    return application_from_row(row) if row else None


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Вход"), KeyboardButton(text="Регистрация")],
            [KeyboardButton(text="Заявление на вступление"), KeyboardButton(text="Новости")],
            [KeyboardButton(text="Администратор"), KeyboardButton(text="Мой статус")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


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
        builder.button(
            text=f"Заявка #{application.id} - {application.full_name}",
            callback_data=f"view:{application.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def render_application_preview(user: UserProfile, activity: str, company_name: str) -> str:
    return (
        "<b>Проверьте заявление</b>\n\n"
        f"ФИО: {user.full_name}\n"
        f"Телефон: {user.phone}\n"
        f"ИИН: {user.iin}\n"
        f"Вид деятельности: {activity}\n"
        f"Название компании: {company_name}"
    )


def render_application_for_admin(application: MembershipApplication) -> str:
    return (
        f"<b>Заявка #{application.id}</b>\n\n"
        f"ФИО: {application.full_name}\n"
        f"Телефон: {application.phone}\n"
        f"ИИН: {application.iin}\n"
        f"Деятельность при регистрации: {application.user_activity}\n"
        f"Деятельность в заявке: {application.application_activity}\n"
        f"Компания: {application.company_name}\n"
        f"Статус: {application.status}\n"
        f"Дата подачи: {application.created_at}"
    )


def register_pdf_font() -> str:
    font_candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
    ]
    for font_path in font_candidates:
        if font_path.exists():
            pdfmetrics.registerFont(TTFont("AppFont", str(font_path)))
            return "AppFont"
    return "Helvetica"


def generate_certificate(application: MembershipApplication, comment: str) -> Path:
    CERTIFICATES_DIR.mkdir(parents=True, exist_ok=True)
    certificate_path = CERTIFICATES_DIR / f"certificate_{application.id}.pdf"
    font_name = register_pdf_font()

    doc = SimpleDocTemplate(
        str(certificate_path),
        pagesize=A4,
        rightMargin=22 * mm,
        leftMargin=22 * mm,
        topMargin=24 * mm,
        bottomMargin=22 * mm,
    )
    styles = getSampleStyleSheet()
    styles["Title"].fontName = font_name
    styles["Heading2"].fontName = font_name
    styles["BodyText"].fontName = font_name

    title = Paragraph("Свидетельство о вступлении", styles["Title"])
    subtitle = Paragraph("Альянс женщин предпринимателей", styles["Heading2"])
    body = Paragraph(
        (
            f"Настоящим подтверждается, что <b>{application.full_name}</b>, "
            f"представляющая компанию <b>{application.company_name}</b>, "
            "принята в состав Альянса женщин предпринимателей."
        ),
        styles["BodyText"],
    )

    table = Table(
        [
            ["Номер заявления", f"#{application.id}"],
            ["ИИН", application.iin],
            ["Телефон", application.phone],
            ["Вид деятельности", application.application_activity],
            ["Дата одобрения", datetime.now().strftime("%d.%m.%Y")],
            ["Комментарий", comment],
        ],
        colWidths=[55 * mm, 105 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F2F2")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#BDBDBD")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )

    doc.build([title, Spacer(1, 8 * mm), subtitle, Spacer(1, 12 * mm), body, Spacer(1, 10 * mm), table])
    return certificate_path


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


async def notify_admins(bot: Bot, application_id: int) -> None:
    application = get_application(application_id)
    if application is None:
        return

    text = "Поступило новое заявление.\n\n" + render_application_for_admin(application)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=admin_application_keyboard(application_id))
        except Exception:
            logging.exception("Could not notify admin %s", admin_id)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Здравствуйте! Это бот Альянса женщин предпринимателей. Выберите действие:",
        reply_markup=main_menu(),
    )


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(F.text == "Вход")
async def login(message: Message) -> None:
    profile = get_user(message.from_user.id)
    if profile is None:
        await message.answer("Вы еще не зарегистрированы. Нажмите «Регистрация».", reply_markup=main_menu())
        return

    await message.answer(
        f"Вход выполнен.\n\nФИО: {profile.full_name}\nТелефон: {profile.phone}",
        reply_markup=main_menu(),
    )


@router.message(F.text == "Регистрация")
async def registration_start(message: Message, state: FSMContext) -> None:
    profile = get_user(message.from_user.id)
    if profile is not None:
        await message.answer(
            "Вы уже зарегистрированы. Повторная регистрация под тем же аккаунтом недоступна.",
            reply_markup=main_menu(),
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
            reply_markup=main_menu(),
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
    await message.answer("Регистрация успешно завершена.", reply_markup=main_menu())


@router.message(F.text == "Заявление на вступление")
async def application_start(message: Message, state: FSMContext) -> None:
    profile = get_user(message.from_user.id)
    if profile is None:
        await message.answer("Сначала пройдите регистрацию.", reply_markup=main_menu())
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
    await message.answer("Тогда пройдите регистрацию заново.", reply_markup=main_menu())


@router.message(Application.waiting_for_activity)
async def application_activity(message: Message, state: FSMContext) -> None:
    await state.update_data(application_activity=message.text.strip())
    await state.set_state(Application.waiting_for_company)
    await message.answer("Введите название компании.")


@router.message(Application.waiting_for_company)
async def application_company(message: Message, state: FSMContext) -> None:
    profile = get_user(message.from_user.id)
    if profile is None:
        await state.clear()
        await message.answer("Регистрация не найдена. Пройдите регистрацию.", reply_markup=main_menu())
        return

    await state.update_data(company_name=message.text.strip())
    data = await state.get_data()
    await state.set_state(Application.preview)
    await message.answer(
        render_application_preview(profile, data["application_activity"], data["company_name"]),
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
    )
    await state.clear()
    await message.answer(
        f"Заявление №{application_id} успешно зарегистрировано.",
        reply_markup=main_menu(),
    )
    await notify_admins(bot, application_id)


@router.message(F.text == "Администратор")
async def admin_panel(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет доступа к разделу администратора.", reply_markup=main_menu())
        return

    applications = list_pending_applications()
    if not applications:
        await message.answer("Новых заявлений пока нет.", reply_markup=main_menu())
        return

    await message.answer("Новые заявления:", reply_markup=pending_applications_keyboard(applications))


@router.message(F.text == "Новости")
async def news(message: Message) -> None:
    await message.answer("Раздел новостей готов к подключению. Здесь можно публиковать объявления альянса.")


@router.message(F.text == "Мой статус")
async def my_status(message: Message) -> None:
    application = get_last_application(message.from_user.id)
    if application is None:
        await message.answer("У вас пока нет поданных заявлений.", reply_markup=main_menu())
        return

    status_labels = {
        "pending": "на рассмотрении",
        "approved": "одобрено",
        "rejected": "отказано",
    }
    text = f"Последнее заявление №{application.id}: {status_labels.get(application.status, application.status)}"
    if application.admin_comment:
        text += f"\nКомментарий: {application.admin_comment}"
    await message.answer(text, reply_markup=main_menu())


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
    await callback.message.answer(f"Введите комментарий для одобрения заявки #{application_id}.")
    await callback.answer()


@router.message(AdminReview.waiting_for_approval_comment)
async def admin_approve_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа.", reply_markup=main_menu())
        return

    data = await state.get_data()
    application_id = int(data["application_id"])
    application = get_application(application_id)
    if application is None or application.status != "pending":
        await state.clear()
        await message.answer("Заявление не найдено или уже обработано.", reply_markup=main_menu())
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

    await message.answer(f"Заявка #{application_id} одобрена.", reply_markup=main_menu())
    await bot.send_message(
        application.telegram_id,
        f"Ваше заявление №{application_id} одобрено.\nКомментарий: {comment}",
    )
    await bot.send_document(
        application.telegram_id,
        FSInputFile(certificate_path),
        caption="Ваше свидетельство о вступлении.",
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
        await message.answer("Нет доступа.", reply_markup=main_menu())
        return

    data = await state.get_data()
    application_id = int(data["application_id"])
    application = get_application(application_id)
    if application is None or application.status != "pending":
        await state.clear()
        await message.answer("Заявление не найдено или уже обработано.", reply_markup=main_menu())
        return

    reason = message.text.strip()
    update_application_status(
        application_id=application_id,
        status="rejected",
        admin_id=message.from_user.id,
        comment=reason,
    )
    await state.clear()

    await message.answer(f"По заявке #{application_id} отправлен отказ.", reply_markup=main_menu())
    await bot.send_message(
        application.telegram_id,
        f"По вашему заявлению №{application_id} отказ.\nПричина: {reason}",
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill BOT_TOKEN.")

    init_db()
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
