# ==============================================================================
# CELL 1: SETUP AND INSTALLATIONS
# Run this cell first to install all necessary libraries.
# ==============================================================================
import os
import re
import requests
import subprocess
import logging
from urllib.parse import urlparse
from telegram import Update, MessageEntity, InputFile
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram.error import BadRequest

# --- Configuration ---
BOT_TOKEN = "5369686193:AAFOsEHdKOmMQ0V5YaropYvkyZXhTpvtvj8"

# --- Setup Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- In-memory storage ---
user_files = {}

# --- Helper Functions ---

def download_from_url(url: str, destination: str) -> bool:
    try:
        logger.info(f"Downloading from URL: {url}")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(destination, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info(f"File downloaded to {destination}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download from URL {url}: {e}")
        return False

def get_video_duration(video_path: str) -> float:
    command = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return float(result.stdout)
    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
        return 0.0

def get_direct_link_info(url: str) -> (str, str):
    try:
        logger.info(f"Probing direct link: {url}")
        with requests.Session() as s:
            response = s.head(url, allow_redirects=True, timeout=15)
            response.raise_for_status()

        final_url = response.url
        content_disposition = response.headers.get('Content-Disposition')
        
        if content_disposition:
            filename_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^"]+)"?', content_disposition)
            if filename_match:
                filename = filename_match.group(1).strip('"')
                logger.info(f"Got filename from Content-Disposition: {filename}")
                return final_url, filename

        parsed_path = urlparse(final_url).path
        if parsed_path and os.path.basename(parsed_path):
            filename = os.path.basename(parsed_path)
            logger.info(f"Got filename from URL path: {filename}")
            return final_url, filename

        logger.warning(f"Could not determine filename for {url}")
        return final_url, "unknown_file"
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to probe direct link {url}: {e}")
        return None, None

def get_pixeldrain_info(url: str) -> (str, str):
    match = re.search(r"pixeldrain\.com/(u|l)/([a-zA-Z0-9]+)", url)
    if not match: return None, None
    file_id = match.group(2)
    direct_download_url = f"https://pixeldrain.com/api/file/{file_id}"
    info_url = f"https://pixeldrain.com/api/file/{file_id}/info"
    try:
        data = requests.get(info_url, timeout=10).json()
        return direct_download_url, data.get('name')
    except Exception as e:
        logger.error(f"Failed to get info from Pixeldrain for ID {file_id}: {e}")
        return None, None

def get_gdrive_info(url: str) -> (str, str):
    try:
        file_id_match = re.search(r'(?:/file/d/|/open\?id=|/uc\?id=)([a-zA-Z0-9_-]{28,})', url)
        if not file_id_match: return None, None
        file_id = file_id_match.group(1)
        URL_TEMPLATE = f"https://drive.google.com/uc?id={file_id}&export=download"
        session = requests.Session()
        response = session.get(URL_TEMPLATE, stream=True, timeout=15)
        response.raise_for_status()
        token = next((value for key, value in response.cookies.items() if key.startswith('download_warning')), None)
        if token:
            response = session.get(f"{URL_TEMPLATE}&confirm={token}", stream=True, timeout=15)
        if 'text/html' in response.headers.get('Content-Type', ''): return None, None
        content_disposition = response.headers.get('Content-Disposition')
        if content_disposition:
            filename_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^"]+)"?', content_disposition)
            if filename_match:
                return response.url, filename_match.group(1)
    except Exception as e:
        logger.error(f"Unexpected error resolving Google Drive URL: {e}")
    return None, None

# --- Bot Handlers ---

def start(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    update.message.reply_html(rf"Hi {user.mention_html()}!")
    update.message.reply_text(
        "Welcome to the Subtitle Muxer Bot!\n\n"
        "1. Send your video file or a direct download link.\n"
        "2. Send the .srt subtitle file or a direct download link.\n\n"
        "Note: For large files (>20MB), please use direct download links."
    )

def check_and_process(user_id: int, update: Update, context: CallbackContext):
    if user_files.get(user_id, {}).get('video') and user_files.get(user_id, {}).get('subtitle'):
        process_files(update, context)

def handle_file(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    if user_id not in user_files: 
        user_files[user_id] = {}
    
    file_doc = update.message.document or update.message.video
    if not file_doc: 
        return

    # Get file name or use default
    file_name = getattr(file_doc, 'file_name', None) or "video_file"
    
    # Check file size limit (20MB Telegram limit for direct downloads)
    MAX_DIRECT_SIZE = 20 * 1024 * 1024  # 20MB
    if file_doc.file_size and file_doc.file_size > MAX_DIRECT_SIZE:
        size_mb = file_doc.file_size / (1024 * 1024)
        update.message.reply_text(
            f"❌ File '{file_name}' is too large ({size_mb:.1f}MB > 20MB).\n\n"
            "Please upload it to a file hosting service (like pixeldrain.com) and send me the download link.",
            quote=True
        )
        return

    status_message = update.message.reply_text(f"Received '{file_name}'.", quote=True)

    try:
        download_url = context.bot.get_file(file_doc.file_id).file_path
        if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
            user_files[user_id]['video'] = download_url
            status_message.edit_text(f"✅ Video '{file_name}' received. Now send the subtitle.")
        elif file_name.lower().endswith('.srt'):
            user_files[user_id]['subtitle'] = download_url
            status_message.edit_text(f"✅ Subtitle '{file_name}' received. Now send the video.")
        check_and_process(user_id, update, context)
    except BadRequest as e:
        if "File is too big" in str(e):
            status_message.edit_text(
                f"❌ File '{file_name}' is too large for direct download.\n\n"
                "Please send a direct download link instead."
            )
        else:
            logger.error(f"Telegram error: {e}")
            status_message.edit_text(f"Telegram error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        status_message.edit_text(f"Error: {e}")

def handle_text(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    message_text = update.message.text
    urls = [message_text[entity.offset:entity.offset+entity.length] 
            for entity in update.message.entities 
            if entity.type == MessageEntity.URL]
    
    if not urls: 
        return
    
    url = urls[0]
    if user_id not in user_files: 
        user_files[user_id] = {}

    status_message = update.message.reply_text("⚙️ Processing link...", quote=True)
    final_url = None
    file_name = None

    if "pixeldrain.com" in url:
        status_message.edit_text("⚙️ Pixeldrain link detected...")
        final_url, file_name = get_pixeldrain_info(url)
    elif "drive.google.com" in url:
        status_message.edit_text("⚙️ Google Drive link detected...")
        final_url, file_name = get_gdrive_info(url)
    else:
        status_message.edit_text("⚙️ Direct link detected...")
        final_url, file_name = get_direct_link_info(url)

    if not final_url:
        status_message.edit_text("❌ Could not process this link. Please check it's valid.")
        return

    if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
        user_files[user_id]['video'] = final_url
        status_message.edit_text(f"✅ Video link for '{file_name}' received. Now send the subtitle.")
    elif file_name.lower().endswith('.srt'):
        user_files[user_id]['subtitle'] = final_url
        status_message.edit_text(f"✅ Subtitle link for '{file_name}' received. Now send the video.")
    else:
        status_message.edit_text("❌ Link doesn't point to a supported video or subtitle file.")
        return

    check_and_process(user_id, update, context)

def process_files(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    status_message = update.message.reply_text("⬇️ Starting processing...", quote=True)
    
    video_path = f"{user_id}_input.mp4"
    subtitle_path = f"{user_id}_input.srt"
    output_path = f"{user_id}_output.mp4"

    try:
        # Download video
        status_message.edit_text("⬇️ Downloading video...")
        if not download_from_url(user_files[user_id]['video'], video_path):
            status_message.edit_text("❌ Failed to download video.")
            return

        # Download subtitle
        status_message.edit_text("⬇️ Downloading subtitle...")
        if not download_from_url(user_files[user_id]['subtitle'], subtitle_path):
            status_message.edit_text("❌ Failed to download subtitle.")
            return

        # Get video duration
        total_duration = get_video_duration(video_path)
        if total_duration <= 0:
            status_message.edit_text("❌ Could not read video duration.")
            return

        # Process with FFmpeg
        status_message.edit_text("⚙️ Hardcoding subtitles...")
        font_path = os.path.abspath('./fonts/HelveticaRounded-Bold.ttf')
        ffmpeg_command = [
            'ffmpeg', '-i', video_path,
            '-vf', f"subtitles={subtitle_path}:force_style='FontFile={font_path},FontSize=20,MarginV=40'",
            '-c:v', 'libx265', '-crf', '28', '-preset', 'veryfast',
            '-c:a', 'aac', '-b:a', '128k', '-y',
            '-progress', 'pipe:1', output_path
        ]
        
        process = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                  universal_newlines=True, encoding='utf-8')
        
        last_reported_percent = -1
        for line in process.stdout:
            match = re.search(r"out_time_ms=(\d+)", line)
            if match:
                current_time = int(match.group(1)) / 1_000_000
                percent = min(int((current_time / total_duration) * 100), 100)
                if percent > last_reported_percent and percent % 5 == 0:
                    bar = '█' * (percent // 10) + '░' * (10 - (percent // 10))
                    try:
                        status_message.edit_text(f"⚙️ Processing: [{bar}] {percent}%")
                    except BadRequest: 
                        pass
                    last_reported_percent = percent
        
        process.wait()
        if process.returncode != 0:
            error = process.stderr.read()
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_command, stderr=error)

        # Send result
        status_message.edit_text("✅ Processing complete! ⬆️ Uploading...")
        with open(output_path, 'rb') as video_file:
            context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(video_file, filename="subtitled_video.mp4"),
                caption="Here's your video with hardcoded subtitles!"
            )
        status_message.delete()
        
    except subprocess.CalledProcessError as e:
        error = "\n".join(e.stderr.splitlines()[-5:])
        status_message.edit_text(f"❌ FFmpeg error:\n\n`{error}`", parse_mode='Markdown')
    except Exception as e:
        status_message.edit_text(f"❌ Error: {str(e)}")
    finally:
        # Cleanup
        for path in [video_path, subtitle_path, output_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if user_id in user_files:
            del user_files[user_id]

def main() -> None:
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("!!! ERROR: Please set your bot token")
        return
    
    print("Starting bot...")
    updater = Updater(BOT_TOKEN)
    dispatcher = updater.dispatcher
    
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.document | Filters.video, handle_file))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    
    updater.start_polling()
    print("Bot running...")
    updater.idle()

if __name__ == '__main__':
    main()
