import json
import logging
import os
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

request = HTTPXRequest(connection_pool_size=100, pool_timeout=50)

# Logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Global dictionary of messages with translations
with open("messages.json", "r", encoding="utf-8") as file:
    MESSAGES = json.load(file)

def get_msg(key, lang, **kwargs):
    """Returns a formatted message for a given key and language."""
    text = MESSAGES.get(key, {}).get(lang, "")
    return text.format(**kwargs)

# Read the list of allowed usernames from the environment variable.
allowed_usernames_str = os.getenv("ALLOWED_USERNAMES", "")
ALLOWED_USERNAMES = {username.strip().lstrip('@') for username in allowed_usernames_str.split(",") if username.strip()}

def restricted(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Check for message or callback query to retrieve user data
        user = None
        if update.message and update.message.from_user:
            user = update.message.from_user
        elif update.callback_query and update.callback_query.from_user:
            user = update.callback_query.from_user

        if not user:
            logger.warning("Update has no user data.")
            return

        if not user.username or user.username.lower() not in {u.lower() for u in ALLOWED_USERNAMES}:
            logger.info(f"Unauthorized access attempt from user: {user.username} (ID: {user.id})")
            # Send a reply if possible (only for messages)
            if update.message:
                await update.message.reply_text("Access denied. You are not authorized to use this bot.")
            return

        return await func(update, context)
    return wrapper

# Environment variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE", "PillSyncBot")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set")

# Connect to DynamoDB
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)

# Create a global bot object with the specified Request
bot = Bot(token=TOKEN, request=request)

# Create an Application with an increased connection pool
app = ApplicationBuilder().token(TOKEN).pool_timeout(50).connection_pool_size(100).build()

# Limit the number of concurrent requests
semaphore = asyncio.Semaphore(10)  # Maximum 10 concurrent requests

async def send_message_with_limit(chat_id, text, reply_markup):
    """Send a message with semaphore limitation."""
    async with semaphore:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Error sending message to {chat_id}: {e}")

def get_user_data(chat_id):
    """Retrieve user data from DynamoDB. If not found, create default settings."""
    response = table.get_item(Key={"chat_id": str(chat_id)})
    item = response.get("Item", {"chat_id": str(chat_id), "timezone": "UTC", "medications": [], "language": "ru"})
    if "language" not in item:
        item["language"] = "ru"
    return item

def save_user_data(user_data):
    """Save user data to DynamoDB."""
    table.put_item(Item=user_data)

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start command. Greets the user and displays the list of available commands.
    """
    chat_id = str(update.message.chat.id)
    user_data = get_user_data(chat_id)
    lang = user_data.get("language", "ru")
    await update.message.reply_text(get_msg("start", lang))

@restricted
async def add_medicine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add medicine using the /addmedicine command."""
    args = context.args
    chat_id = str(update.message.chat.id)
    user_data = get_user_data(chat_id)
    lang = user_data.get("language", "ru")
    if len(args) < 3:
        await update.message.reply_text(get_msg("addmedicine_usage", lang))
        return

    medicine_name = args[0]
    dosage = args[1]
    times = args[2:]

    user_data["medications"].append({
        "name": medicine_name,
        "dosage": dosage,
        "times": times,
        # Add the 'acknowledged' field as an empty dictionary
        "acknowledged": {}
    })
    save_user_data(user_data)

    await update.message.reply_text(
        get_msg("medicine_added", lang, medicine=medicine_name, dosage=dosage, times=", ".join(times))
    )

@restricted
async def delete_medicine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete medicine using the /deletemedicine <name> command."""
    args = context.args
    chat_id = str(update.message.chat.id)
    user_data = get_user_data(chat_id)
    lang = user_data.get("language", "ru")
    if not args:
        await update.message.reply_text(get_msg("deletemedicine_usage", lang))
        return

    medicine_name = args[0]
    medications = user_data.get("medications", [])

    new_medications = [med for med in medications if med.get("name", "").lower() != medicine_name.lower()]

    if len(new_medications) == len(medications):
        await update.message.reply_text(get_msg("medicine_not_found", lang, medicine=medicine_name))
        return

    user_data["medications"] = new_medications
    save_user_data(user_data)
    await update.message.reply_text(get_msg("medicine_deleted", lang, medicine=medicine_name))

@restricted
async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the timezone using the /settimezone command."""
    args = context.args
    chat_id = str(update.message.chat.id)
    user_data = get_user_data(chat_id)
    lang = user_data.get("language", "ru")
    if not args:
        await update.message.reply_text(get_msg("settimezone_usage", lang))
        return

    tz = args[0]
    user_data["timezone"] = tz
    save_user_data(user_data)
    await update.message.reply_text(get_msg("timezone_set", lang, timezone=tz))

@restricted
async def list_medicines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Display all medicines added by the user.
    """
    chat_id = str(update.message.chat.id)
    user_data = get_user_data(chat_id)
    lang = user_data.get("language", "ru")
    medications = user_data.get("medications", [])

    if not medications:
        await update.message.reply_text(get_msg("listmedicines_empty", lang))
        return

    # Get the preposition for time from the global dictionary
    preposition = get_msg("medicine_time_preposition", lang)

    message_lines = []
    for med in medications:
        name = med.get("name", "Без имени" if lang == "ru" else "Unnamed")
        dosage = med.get("dosage", "Не указана" if lang == "ru" else "Not specified")
        times = ", ".join(med.get("times", []))
        acknowledged = med.get("acknowledged", {})
        if acknowledged:
            ack_parts = [f"{t}: {ack}" for t, ack in acknowledged.items()]
            ack_str = " (" + ", ".join(ack_parts) + ")"
        else:
            ack_str = ""
        message_lines.append(f"{name} ({dosage}) {preposition} {times}{ack_str}")

    final_message = "\n".join(message_lines)
    await update.message.reply_text(final_message)

@restricted
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Echo bot replies to text messages that are not commands."""
    chat_id = str(update.message.chat.id)
    user_data = get_user_data(chat_id)
    lang = user_data.get("language", "ru")
    await update.message.reply_text(get_msg("echo", lang, text=update.message.text))

@restricted
async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the language using the /setlanguage command."""
    args = context.args
    chat_id = str(update.message.chat.id)
    user_data = get_user_data(chat_id)
    # Use the current language for the response if no argument is provided
    current_lang = user_data.get("language", "ru")
    if not args:
        await update.message.reply_text(get_msg("setlanguage_usage", current_lang))
        return

    new_lang = args[0].lower()
    if new_lang not in {"ru", "en"}:
        await update.message.reply_text(get_msg("setlanguage_usage", current_lang))
        return

    user_data["language"] = new_lang
    save_user_data(user_data)
    # Send the confirmation message in the new language
    await update.message.reply_text(get_msg("language_set", new_lang))

async def send_reminders_async():
    """
    Asynchronous function for sending reminders.
    Sends a reminder once per day for each intake time if a reminder hasn't been sent yet.
    """
    now_utc = datetime.utcnow()
    logger.info(f"send_reminders_async: now_utc = {now_utc.isoformat()}")
    response = table.scan()
    users = response.get("Items", [])
    
    tasks = []

    for user in users:
        chat_id = user["chat_id"]
        tz_str = user.get("timezone", "UTC")
        medications = user.get("medications", [])
        lang = user.get("language", "ru")
        try:
            tz = ZoneInfo(tz_str)
        except Exception as e:
            logger.error(f"Error with timezone {tz_str} for chat {chat_id}: {e}")
            tz = ZoneInfo("UTC")
        
        now_local = now_utc.astimezone(tz)
        today_str = now_local.strftime("%Y-%m-%d")
        logger.info(f"User {chat_id}: now_local = {now_local.isoformat()}, today_str = {today_str}")
        user_modified = False

        for med in medications:
            med_name = med.get("name", "")
            acknowledged = med.get("acknowledged", {})

            for med_time in med.get("times", []):
                # Skip if already acknowledged today
                if acknowledged.get(med_time) == today_str:
                    logger.info(f"{med_name} at {med_time} is already acknowledged for today.")
                    continue

                # If a reminder was already sent today, skip sending again
                last_reminder_str = med.get("last_reminder_time", None)
                if last_reminder_str:
                    try:
                        last_reminder_time = datetime.fromisoformat(last_reminder_str)
                    except Exception as e:
                        logger.error(f"Error parsing last_reminder_time for {med_name}: {e}")
                        last_reminder_time = None
                    if last_reminder_time and last_reminder_time.date() == now_local.date():
                        logger.info(f"Reminder for {med_name} at {med_time} already sent today.")
                        continue

                try:
                    med_time_obj = datetime.strptime(med_time, "%H:%M").time()
                except Exception as e:
                    logger.error(f"Error parsing time {med_time} for {med_name}: {e}")
                    continue

                scheduled_dt = datetime(
                    now_local.year, now_local.month, now_local.day,
                    med_time_obj.hour, med_time_obj.minute, tzinfo=tz
                )
                logger.info(f"For {med_name} scheduled at {med_time}: scheduled_dt = {scheduled_dt.isoformat()}")
                if now_local < scheduled_dt:
                    logger.info(f"Current time {now_local.isoformat()} is before scheduled_dt for {med_name} at {med_time}.")
                    continue  # Not time yet

                # If the time has arrived and a reminder hasn't been sent today, send it
                callback_data = f"ack|{med_name}|{med_time}"
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(get_msg("ack_button", lang), callback_data=callback_data)]
                ])
                message_text = get_msg("reminder_message", lang, medicine=med_name, dosage=med.get("dosage"), time=med_time)
                tasks.append(send_message_with_limit(chat_id, message_text, keyboard))
                med["last_reminder_time"] = now_local.isoformat()
                user_modified = True
                logger.info(f"Scheduled reminder for {med_name} at {med_time} for chat {chat_id}")

        if user_modified:
            save_user_data(user)
            logger.info(f"User data saved for chat {chat_id}")

    if tasks:
        logger.info(f"Sending {len(tasks)} reminder messages.")
        try:
            await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"Error sending messages: {e}")
    else:
        logger.info("No reminders to send.")

@restricted
async def callback_acknowledge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Callback query handler for confirming medicine intake.
    Callback data format: "ack|<med_name>|<med_time>"
    """
    query = update.callback_query
    await query.answer()  # Acknowledge receipt of the callback

    # Retrieve chat_id and user data to determine the language
    chat_id = str(query.message.chat.id)
    user_data = get_user_data(chat_id)
    lang = user_data.get("language", "ru")
    
    data = query.data.split("|")
    if len(data) != 3 or data[0] != "ack":
        await query.edit_message_text(text=get_msg("ack_invalid", lang))
        return

    med_name, med_time = data[1], data[2]
    medications = user_data.get("medications", [])
    try:
        tz = ZoneInfo(user_data.get("timezone", "UTC"))
    except Exception:
        tz = ZoneInfo("UTC")
    today_str = datetime.utcnow().astimezone(tz).strftime("%Y-%m-%d")
    found = False

    for med in medications:
        if med.get("name", "").lower() == med_name.lower():
            if med_time in med.get("times", []):
                if "acknowledged" not in med:
                    med["acknowledged"] = {}
                med["acknowledged"][med_time] = today_str
                found = True
                break

    if found:
        save_user_data(user_data)
        original_text = query.message.text or ""
        new_text = f"{original_text}\n\n{get_msg('acknowledged_success', lang)}"
        await query.edit_message_text(text=new_text)
    else:
        await query.edit_message_text(text=get_msg("medicine_for_ack_not_found", lang))

async def process_update_async(update):
    """
    Asynchronous function for processing updates.
    """
    await app.initialize()
    await app.process_update(update)
    await app.shutdown()

# Register handlers in the application
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("addmedicine", add_medicine))
app.add_handler(CommandHandler("settimezone", set_timezone))
app.add_handler(CommandHandler("deletemedicine", delete_medicine))
app.add_handler(CommandHandler("listmedicines", list_medicines))
app.add_handler(CommandHandler("setlanguage", set_language))
# Register callback query handler for medicine intake confirmation
app.add_handler(CallbackQueryHandler(callback_acknowledge, pattern=r"^ack\|"))
# For all other text messages – echo
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

def lambda_handler(event, context):
    """Lambda handler"""
    logger.info(f"Received event: {json.dumps(event)}")
    # If the event is from EventBridge (reminders)
    if "source" in event and event["source"] == "aws.events":
        asyncio.run(send_reminders_async())
        return {"statusCode": 200, "body": json.dumps({"message": "Reminders sent"})}
    # If this is an API Gateway request
    if "body" not in event:
        return {"statusCode": 400, "body": json.dumps({"error": "No body in request"})}
    try:
        body = json.loads(event["body"])
        if "update_id" not in body:
            return {"statusCode": 400, "body": json.dumps({"error": "Invalid update format"})}
        update = Update.de_json(body, app.bot)
        asyncio.run(process_update_async(update))
        return {"statusCode": 200, "body": json.dumps({"message": "OK"})}
    except Exception as e:
        logger.error(f"Error processing update: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
