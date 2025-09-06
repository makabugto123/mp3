import os
import logging
import requests
from io import BytesIO
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode
import yt_dlp
import asyncio
import math

# --- Configuration ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("Please set the TELEGRAM_BOT_TOKEN environment variable.")
    
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TEMP_DOWNLOAD_DIR = "downloads"
if not os.path.exists(TEMP_DOWNLOAD_DIR):
    os.makedirs(TEMP_DOWNLOAD_DIR)

# --- Helper Functions ---
async def schedule_file_deletion(file_path: str, delay: int):
    logger.info(f"Scheduling deletion of '{os.path.basename(file_path)}' in {delay} seconds.")
    await asyncio.sleep(delay)
    try:
        os.remove(file_path)
        logger.info(f"Successfully deleted temporary file: {os.path.basename(file_path)}")
    except FileNotFoundError:
        logger.warning(f"File not found for deletion: {file_path}")
    except Exception as e:
        logger.error(f"Error deleting file {file_path}: {e}")

def format_duration(seconds):
    if seconds is None:
        return "N/A"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    else:
        return f"{m:02d}:{s:02d}"

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        f"👋 Hi {user.mention_html()}!\n\n"
        "I'm your friendly YouTube Downloader Bot, now powered by `yt-dlp`.\n\n"
        "Send me a search query to get started!"
    )

async def search_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text
    if not query:
        return

    processing_message = await update.message.reply_text("🔎 Searching YouTube...")

    try:
        ydl_opts = {
            'format': 'best',
            'quiet': True,
            'default_search': 'ytsearch5',
            'noplaylist': True,
            'socket_timeout': 30,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_result = ydl.extract_info(query, download=False)
            results = search_result.get('entries', [])

        if not results:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=processing_message.message_id,
                text="❌ No results found. Please try another search term."
            )
            return

        context.user_data['search_results'] = results

        thumbnail_urls = [res.get('thumbnail') for res in results]
        collage_path = await create_thumbnail_collage(thumbnail_urls)

        # Schedule the collage image for deletion after 2 minutes
        asyncio.create_task(schedule_file_deletion(collage_path, delay=120))

        message_text = "👇 *Select one number to download*\n\n"
        for i, video in enumerate(results):
            title = video.get('title', 'No Title')
            duration_str = format_duration(video.get('duration'))
            link = video.get('webpage_url', '#')
            message_text += f"*{i+1}. {title}*\n"
            message_text += f"🔗 [Link]({link}) | ⏳ Duration: {duration_str}\n\n"

        with open(collage_path, 'rb') as photo:
            sent_message = await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photo,
                caption=message_text,
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data['results_message_id'] = sent_message.message_id

        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=processing_message.message_id
        )
        context.user_data['state'] = 'awaiting_video_selection'

    except Exception as e:
        logger.error(f"Error during search: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=processing_message.message_id,
            text=f"An error occurred: {e}. Please try again."
        )

async def create_thumbnail_collage(thumbnail_urls: list) -> str:
    images = []
    for url in thumbnail_urls:
        if not url: continue
        try:
            response = requests.get(url)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            images.append(img)
        except Exception as e:
            logger.error(f"Could not process thumbnail {url}: {e}")
            images.append(Image.new('RGB', (120, 90), color = 'gray'))

    if not images:
        placeholder = Image.new('RGB', (120 * 5, 90), color='gray')
        path = os.path.join(TEMP_DOWNLOAD_DIR, 'collage.jpg')
        placeholder.save(path)
        return path

    img_width, img_height = 120, 90
    collage = Image.new('RGB', (img_width * len(images), img_height))

    for i, img in enumerate(images):
        img = img.resize((img_width, img_height))
        collage.paste(img, (i * img_width, 0))

    collage_path = os.path.join(TEMP_DOWNLOAD_DIR, 'collage.jpg')
    collage.save(collage_path)
    return collage_path

async def handle_user_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get('state')
    if not state:
        await search_youtube(update, context)
        return

    if state == 'awaiting_video_selection':
        await handle_video_selection(update, context)

async def handle_video_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        choice = int(update.message.text)
        if not 1 <= choice <= len(context.user_data.get('search_results', [])):
            raise ValueError

        selected_video = context.user_data['search_results'][choice - 1]
        context.user_data['selected_video'] = selected_video

        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

        keyboard = [
            [InlineKeyboardButton("🎵 MP3 (Audio)", callback_data='mp3')],
            [InlineKeyboardButton("🎬 MP4 (Video)", callback_data='mp4')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        sent_message = await update.message.reply_text(
            f"You selected: *{selected_video['title']}*\n\nSelect download type:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['selection_message_id'] = sent_message.message_id
        context.user_data['state'] = None
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Please enter a valid number from the list.")

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    context.user_data['format'] = data
    if data == 'mp3':
        keyboard = [
            [InlineKeyboardButton("128 kbps", callback_data='quality_128')],
            [InlineKeyboardButton("320 kbps", callback_data='quality_320')],
        ]
    else: # mp4
        keyboard = [
            [InlineKeyboardButton("360p", callback_data='quality_360p')],
            [InlineKeyboardButton("720p (HD)", callback_data='quality_720p')],
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text="Select quality:", reply_markup=reply_markup)

async def quality_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    quality = query.data.split('_')[1]
    video_info = context.user_data['selected_video']
    await query.edit_message_text(text=f"⏳ Converting '{video_info['title']}'... Please wait.")
    asyncio.create_task(
        download_and_send_file(
            update.effective_chat.id,
            video_info,
            context.user_data['format'],
            quality,
            context
        )
    )

async def download_and_send_file(chat_id, video_info, file_format, quality, context):
    safe_title = "".join([c for c in video_info['title'] if c.isalnum() or c.isspace()]).rstrip()
    url = video_info['webpage_url']
    output_path = None

    try:
        if file_format == 'mp3':
            output_template = os.path.join(TEMP_DOWNLOAD_DIR, f"{safe_title}.%(ext)s")
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': quality}],
                'outtmpl': output_template,
                'quiet': True,
            }
            output_path = os.path.join(TEMP_DOWNLOAD_DIR, f"{safe_title}.mp3")
        else: # mp4
            quality_num = quality[:-1]
            output_template = os.path.join(TEMP_DOWNLOAD_DIR, f"{safe_title}.%(ext)s")
            ydl_opts = {
                'format': f'bestvideo[height<={quality_num}]+bestaudio/best[height<={quality_num}]/best',
                'outtmpl': output_template,
                'quiet': True,
            }
            output_path = os.path.join(TEMP_DOWNLOAD_DIR, f"{safe_title}.mp4")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(output_path):
            actual_filename = None
            for f in os.listdir(TEMP_DOWNLOAD_DIR):
                if f.startswith(safe_title):
                    actual_filename = f
                    break
            if actual_filename:
                output_path = os.path.join(TEMP_DOWNLOAD_DIR, actual_filename)
            else:
                 raise FileNotFoundError("Downloaded file could not be found.")

        if file_format == 'mp3':
            await context.bot.send_audio(
                chat_id=chat_id, audio=open(output_path, 'rb'), title=safe_title,
                duration=video_info.get('duration')
            )
        else:
            await context.bot.send_video(
                chat_id=chat_id, video=open(output_path, 'rb'), caption=safe_title,
                duration=video_info.get('duration')
            )
        
        logger.info(f"Cleaning up messages for chat_id {chat_id}")
        try:
            results_msg_id = context.user_data.get('results_message_id')
            selection_msg_id = context.user_data.get('selection_message_id')
            if results_msg_id:
                await context.bot.delete_message(chat_id=chat_id, message_id=results_msg_id)
            if selection_msg_id:
                await context.bot.delete_message(chat_id=chat_id, message_id=selection_msg_id)
        except Exception as e:
            logger.warning(f"Could not delete one or more messages: {e}")

        # Schedule the downloaded MP3/MP4 for deletion after 2 minutes
        asyncio.create_task(schedule_file_deletion(output_path, delay=120))
        context.user_data.clear()

    except Exception as e:
        logger.error(f"Error downloading/sending file: {e}")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ Failed to download '{video_info['title']}'. Error: {e}"
        )
        if output_path and os.path.exists(output_path):
            os.remove(output_path)

def main() -> None:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_response))
    application.add_handler(CallbackQueryHandler(button_callback_handler, pattern='^(mp3|mp4)$'))
    application.add_handler(CallbackQueryHandler(quality_button_handler, pattern='^quality_.*$'))

    print("Bot is running with yt-dlp...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()