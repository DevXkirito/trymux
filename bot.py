
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
from telegram import Update, MessageEntity
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram.error import BadRequest

# --- Configuration ---
BOT_TOKEN = "5369686193:AAFOsEHdKOmMQ0V5YaropYvkyZXhTpvtvj8"  # <-- IMPORTANT: PASTE YOUR BOT TOKEN HERE

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
    """Downloads a file from a direct URL."""
    try:
        logger.info(f"Downloading from URL: {url}")
        with requests.get(url, stream=True, timeout=60) as r: # Added timeout
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
    """Gets the duration of a video file in seconds using ffprobe."""
    command = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return float(result.stdout)
    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
        return 0.0

# ⭐ ============================================================================
# ⭐ NEW HELPER FUNCTION FOR ANY DIRECT LINK
# ⭐ ============================================================================
def get_direct_link_info(url: str) -> (str, str):
    """
    Probes a direct link using a HEAD request to get the real filename.
    This is much more reliable than just parsing the URL.
    Returns (final_url, filename) or (None, None) on failure.
    """
    try:
        logger.info(f"Probing direct link with HEAD request: {url}")
        with requests.Session() as s:
            # Use allow_redirects to follow to the final file URL
            response = s.head(url, allow_redirects=True, timeout=15)
            response.raise_for_status()  # Check for errors like 404

        # 1. Try to get filename from 'Content-Disposition' header (most reliable)
        content_disposition = response.headers.get('Content-Disposition')
        if content_disposition:
            filename_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^"]+)"?', content_disposition)
            if filename_match:
                filename = filename_match.group(1).strip('"')
                logger.info(f"Got filename from Content-Disposition: {filename}")
                return response.url, filename  # Use response.url in case of redirects

        # 2. If no header, fall back to parsing the final URL path
        final_url = response.url
        parsed_path = urlparse(final_url).path
        if parsed_path and os.path.basename(parsed_path):
            filename = os.path.basename(parsed_path)
            logger.info(f"Got filename from URL path: {filename}")
            return final_url, filename

        logger.warning(f"Could not determine filename for {url}")
        return final_url, "unknown_file" # Return a default name if all fails

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to probe direct link {url}: {e}")
        return None, None

def get_pixeldrain_info(url: str) -> (str, str):
    # This function remains unchanged
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
    # This function remains unchanged
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
        "2. Send the .srt subtitle file or a direct download link."
    )

def check_and_process(user_id: int, update: Update, context: CallbackContext):
    if user_files.get(user_id, {}).get('video') and user_files.get(user_id, {}).get('subtitle'):
        process_files(update, context)

def handle_file(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    if user_id not in user_files: user_files[user_id] = {}
    file_doc = update.message.document or update.message.video
    if not file_doc: return
    file_name = file_doc.file_name
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
        status_message.edit_text(f"❌ Error: {e}. If the file is too big, please send a direct link.")

# ⭐ ============================================================================
# ⭐ UPDATED handle_text FUNCTION TO USE THE NEW DIRECT LINK HANDLER
# ⭐ ============================================================================
def handle_text(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    message_text = update.message.text
    status_message = None
    urls = [message_text[entity.offset:entity.offset+entity.length] for entity in update.message.entities if entity.type == MessageEntity.URL]
    if not urls: return
    url = urls[0]
    if user_id not in user_files: user_files[user_id] = {}

    final_url = None
    file_name = None

    if "pixeldrain.com" in url:
        status_message = update.message.reply_text("⚙️ Pixeldrain link detected. Resolving...", quote=True)
        final_url, file_name = get_pixeldrain_info(url)
        if not final_url:
            status_message.edit_text("❌ Could not resolve the Pixeldrain link.")
            return
    elif "drive.google.com" in url:
        status_message = update.message.reply_text("⚙️ Google Drive link detected. Resolving...", quote=True)
        final_url, file_name = get_gdrive_info(url)
        if not final_url:
            status_message.edit_text("❌ Could not get a direct link from Google Drive. The file may have hit its download quota.")
            return
    else:
        # ⭐ This block now handles all other links using the new, robust method
        status_message = update.message.reply_text("⚙️ Verifying direct link...", quote=True)
        final_url, file_name = get_direct_link_info(url)
        if not final_url:
            status_message.edit_text("❌ The link appears to be broken or invalid. Please check it.")
            return

    # --- Process based on resolved filename ---
    if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
        user_files[user_id]['video'] = final_url
        reply_text = f"✅ Video link for '{file_name}' received. Now send the subtitle."
        if status_message: status_message.edit_text(reply_text)
        else: update.message.reply_text(reply_text, quote=True)
    elif file_name.lower().endswith('.srt'):
        user_files[user_id]['subtitle'] = final_url
        reply_text = f"✅ Subtitle link for '{file_name}' received. Now send the video."
        if status_message: status_message.edit_text(reply_text)
        else: update.message.reply_text(reply_text, quote=True)
    else:
        error_text = "The link doesn't point to a supported video (.mp4, .mkv) or subtitle (.srt) file."
        if status_message: status_message.edit_text(error_text)
        else: update.message.reply_text(error_text, quote=True)
        return

    check_and_process(user_id, update, context)

def process_files(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    status_message = update.message.reply_text("⬇️ All files received. Starting download...", quote=True)
    video_path = f"{user_id}_input.mp4"
    subtitle_path = f"{user_id}_input.srt"
    output_path = f"{user_id}_output.mp4"

    try:
        status_message.edit_text("⬇️ Downloading video file...")
        if not download_from_url(user_files[user_id]['video'], video_path):
            status_message.edit_text("❌ Error: Could not download the video file.")
            return
        status_message.edit_text("⬇️ Downloading subtitle file...")
        if not download_from_url(user_files[user_id]['subtitle'], subtitle_path):
            status_message.edit_text("❌ Error: Could not download the subtitle file.")
            return

        total_duration = get_video_duration(video_path)
        if total_duration == 0.0:
            status_message.edit_text("❌ Error: Could not read video file. It may be corrupt.")
            return

        status_message.edit_text("⚙️ Files downloaded. Hardcoding subtitles...")
        
        # ⭐ 1. Define the direct path to your font file
        font_path = os.path.abspath('./fonts/HelveticaRounded-Bold.ttf')

        # ⭐ 2. Modify the ffmpeg command to use 'FontFile=' instead of 'FontName='
        ffmpeg_command = [
            'ffmpeg', '-i', video_path,
            '-vf', f"subtitles={subtitle_path}:force_style='FontFile={font_path},FontSize=20,MarginV=40'",
            '-c:v', 'libx265', '-crf', '28', '-preset', 'veryfast',
            '-c:a', 'aac', '-b:a', '128k', '-y',
            '-progress', 'pipe:1', output_path
        ]
        process = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        last_reported_percent = -1
        for line in process.stdout:
            match = re.search(r"out_time_ms=(\d+)", line)
            if match:
                percent = min(int((int(match.group(1)) / 1_000_000 / total_duration) * 100), 100)
                if percent > last_reported_percent and percent % 5 == 0:
                    bar = '█' * (percent // 10) + '░' * (10 - (percent // 10))
                    try:
                        status_message.edit_text(f"⚙️ Processing: [{bar}] {percent}%")
                    except BadRequest: pass
                    last_reported_percent = percent
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_command, stderr=process.stderr.read())

        status_message.edit_text("✅ Processing complete! ⬆️ Uploading to Telegram...")
        with open(output_path, 'rb') as video_file:
            context.bot.send_video(
                chat_id=user_id, video=video_file,
                caption="✅ Success! Your video is ready.",
                supports_streaming=True
            )
        status_message.delete()
    except subprocess.CalledProcessError as e:
        error_snippet = "\n".join(e.stderr.splitlines()[-5:])
        status_message.edit_text(f"❌ FFmpeg error:\n\n`{error_snippet}`", parse_mode='Markdown')
    except Exception as e:
        status_message.edit_text(f"❌ An unexpected error occurred: {e}")
    finally:
        cleanup_files(user_id, video_path, subtitle_path, output_path)

def cleanup_files(user_id, *files_to_delete):
    logger.info(f"Cleaning up files for user {user_id}.")
    for file_path in files_to_delete:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.error(f"Error removing file {file_path}: {e}")
    if user_id in user_files:
        del user_files[user_id]

def main() -> None:
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("!!! ERROR: Please paste your Bot Token into the BOT_TOKEN variable.")
        return
    print("Starting bot...")
    updater = Updater(BOT_TOKEN)
    dispatcher = updater.dispatcher
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.document | Filters.video, handle_file))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    updater.start_polling()
    print("Bot is now running.")
    updater.idle()

if __name__ == '__main__':
    main()
