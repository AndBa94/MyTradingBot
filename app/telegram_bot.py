import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

log = logging.getLogger(__name__)
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://mytradingbot-production-076e.up.railway.app")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🚀 Открыть MyTradingBot", web_app={"url": WEB_APP_URL})]]
    await update.message.reply_text(
        "MyTradingBot\n\nТорговый терминал работает в PAPER MODE.\nОткрой приложение кнопкой ниже.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

def build_application(token: str):
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    return application

async def run_bot(token: str):
    application = build_application(token)
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    return application
