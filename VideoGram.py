import os
import sys
import time
import asyncio
import hashlib
import subprocess
import logging
import io
import re
import requests  # Para descargar subtítulos y miniatura
import threading  # Para manejar la cancelación en hilos

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import validators


# ──────────────────────────────────────────────#
# RECONFIGURAR STDOUT Y STDERR A UTF-8
# ──────────────────────────────────────────────#

# Verificar si stdout y stderr son None y reasignarlos
if sys.stdout is None or not hasattr(sys.stdout, "encoding"):
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None or not hasattr(sys.stderr, "encoding"):
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# Asegurar que stdout y stderr usen UTF-8 en entornos normales
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass  # Si no existe reconfigure, lo ignoramos

# ──────────────────────────────────────────────#
# CONFIGURACIÓN DEL LOGGING
# ──────────────────────────────────────────────#
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    handlers=[
        stream_handler,
        logging.FileHandler("bot.log", encoding='utf-8')
    ]
)



# ──────────────────────────────────────────────#
# CONFIGURACIÓN (HARDCODEADA)
# ──────────────────────────────────────────────#
API_ID = 
API_HASH = ""
BOT_TOKEN = ""
ALLOWED_USERS = [123456789, 123456789]

# ──────────────────────────────────────────────#
# CONFIGURACIÓN DEL LOGGING
# ──────────────────────────────────────────────#
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
stream_handler.stream.reconfigure(encoding='utf-8')
logging.basicConfig(
    level=logging.INFO,
    handlers=[
        stream_handler,
        logging.FileHandler("bot.log", encoding='utf-8')
    ]
)

# ──────────────────────────────────────────────#
# VARIABLES GLOBALES
# ──────────────────────────────────────────────#
lock_descarga = asyncio.Lock()
video_links = {}
info_cache = {}  # Para almacenar la info extraída de cada URL
download_cancel_flags = {}  # Diccionario para almacenar los flags de cancelación (video_id -> threading.Event)

# ──────────────────────────────────────────────#
# INICIALIZACIÓN DEL BOT
# ──────────────────────────────────────────────#
try:
    bot = Client("downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
except Exception as e:
    logging.error(f"❌ Error al iniciar el bot: {e}")
    sys.exit(1)

# ──────────────────────────────────────────────#
# FUNCIONES AUXILIARES
# ──────────────────────────────────────────────#
async def update_message_text(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            pass
        else:
            logging.error(f"Error al actualizar el mensaje: {e}")

def make_progress_hook(message: Message, loop, cancel_flag, cancel_markup, threshold: float = 5.0, min_interval: float = 3.0):
    last_percentage = [0.0]
    last_update_time = [0.0]
    total_segments = 17

    def hook(progress: dict):
        try:
            if cancel_flag.is_set():
                raise Exception("Descarga cancelada por el usuario.")
            if progress.get("status") == "downloading":
                downloaded = progress.get("downloaded_bytes", 0)
                total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
                if not total or total < downloaded:
                    total = downloaded
                percentage = downloaded / total * 100
                now = time.time()
                if (abs(percentage - last_percentage[0]) >= threshold or percentage >= 100) and \
                   (now - last_update_time[0] >= min_interval or percentage >= 100):
                    last_percentage[0] = percentage
                    last_update_time[0] = now
                    filled = int(total_segments * percentage / 100)
                    bar = "🟩" * filled + "⬜" * (total_segments - filled)
                    downloaded_mb = downloaded / (1024 * 1024)
                    total_mb = total / (1024 * 1024)
                    speed = progress.get("speed", 0)
                    speed_mb = speed / (1024 * 1024)
                    eta = progress.get("eta", 0)
                    eta_str = f"{int(eta//60)}:{int(eta%60):02d}" if eta else "N/A"
                    new_text = (f"📥 Descargando: {percentage:.2f}%\n{bar}\n"
                                f"Descargado: {downloaded_mb:.2f} MB / {total_mb:.2f} MB | "
                                f"Velocidad: {speed_mb:.2f} MB/s | ETA: {eta_str}")
                    if new_text.strip():
                        loop.call_soon_threadsafe(lambda: asyncio.create_task(
                            update_message_text(message, new_text, reply_markup=cancel_markup)
                        ))
        except Exception as e:
            logging.error(f"Error en progress hook: {e}")
    return hook

def make_upload_progress_hook(message: Message, loop, threshold: float = 5.0, min_interval: float = 3.0):
    last_percentage = [0.0]
    last_update_time = [0.0]
    total_segments = 17

    def hook(current: int, total: int):
        try:
            percentage = current / total * 100
            now = time.time()
            if (abs(percentage - last_percentage[0]) >= threshold or percentage >= 100) and \
               (now - last_update_time[0] >= min_interval or percentage >= 100):
                last_percentage[0] = percentage
                last_update_time[0] = now
                filled = int(total_segments * percentage / 100)
                bar = "🟩" * filled + "⬜" * (total_segments - filled)
                new_text = f"⏫ Subiendo: {percentage:.2f}%\n{bar}"
                loop.call_soon_threadsafe(lambda: asyncio.create_task(update_message_text(message, new_text)))
        except Exception as e:
            logging.error(f"Error en upload progress hook: {e}")
    return hook

def validar_url(url: str):
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url if validators.url(url) else None

def formato_para_calidad(calidad: str):
    if calidad == "audio":
        return "bestaudio/best"
    else:
        height = calidad.rstrip("p")
        return f"bestvideo[height={height}]+bestaudio/best"

def extraer_info(url: str, calidad: str = "video"):
    if url in info_cache:
        return info_cache[url]
    formato = formato_para_calidad(calidad)
    opciones = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'format': formato
    }
    try:
        with yt_dlp.YoutubeDL(opciones) as ydl:
            info = ydl.extract_info(url, download=False)
            info_cache[url] = info
            return info
    except Exception as e:
        logging.error(f"❌ Error al extraer info del video: {e}")
        return None

def obtener_resoluciones_playlist(info: dict):
    interseccion = None
    for entry in info.get("entries", []):
        res_set = set()
        for f in entry.get("formats", []):
            if f.get("vcodec") != "none" and f.get("height"):
                res_set.add(f"{f['height']}p")
        if interseccion is None:
            interseccion = res_set
        else:
            interseccion = interseccion.intersection(res_set)
    if interseccion and len(interseccion) > 0:
        return sorted(list(interseccion), key=lambda x: int(x.rstrip("p")), reverse=True)
    else:
        first_entry = info.get("entries", [])[0]
        resoluciones = set()
        for f in first_entry.get("formats", []):
            if f.get("vcodec") != "none" and f.get("height"):
                resoluciones.add(f"{f['height']}p")
        return sorted(list(resoluciones), key=lambda x: int(x.rstrip("p")), reverse=True)

def obtener_resoluciones(url: str):
    try:
        opciones = {'quiet': True, 'no_warnings': True, 'skip_download': True}
        with yt_dlp.YoutubeDL(opciones) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("is_live", False):
                return "LIVE_BLOCKED"
            if "entries" in info and info.get("entries"):
                return obtener_resoluciones_playlist(info)
            formatos = info.get("formats", [])
            resoluciones = set()
            for f in formatos:
                if f.get("vcodec") != "none" and f.get("height"):
                    resoluciones.add(f"{f['height']}p")
            return sorted(list(resoluciones), key=lambda x: int(x.rstrip("p")), reverse=True)
    except Exception as e:
        logging.error(f"❌ Error al obtener resoluciones: {e}")
        return []

def verificar_tamano_video(url: str, calidad: str):
    info = extraer_info(url, calidad)
    if not info:
        return 0
    filesize = 0
    if calidad == "audio":
        filesize = info.get("filesize")
        if filesize is None or filesize == 0:
            for f in info.get("formats", []):
                if "audio" in f.get("format_note", "").lower():
                    filesize = f.get("filesize")
                    if filesize is not None:
                        break
        if filesize is None:
            filesize = 0
    else:
        target_height = int(calidad.rstrip("p"))
        for f in info.get("formats", []):
            if f.get("height") == target_height:
                filesize = f.get("filesize")
                if filesize is None:
                    filesize = 0
                break
    return filesize / (1024 * 1024)

async def convertir_a_mp3(input_file: str, output_file: str):
    logging.info("Iniciando conversión a MP3...")
    proceso = await asyncio.create_subprocess_exec(
        'ffmpeg', '-i', input_file,
        '-vn', '-ab', '192k', '-ar', '44100',
        '-y', output_file,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proceso.communicate()
    if proceso.returncode != 0:
        error_output = stderr.decode()
        logging.error(f"❌ Error en la conversión de audio: {error_output}")
        return False
    return True

def estimar_tiempo_subida(tamano_archivo_mb: float, velocidad_subida_mbps: float = 10):
    velocidad_subida = velocidad_subida_mbps * 0.125
    tiempo_estimado = tamano_archivo_mb / velocidad_subida
    minutos, segundos = divmod(int(tiempo_estimado), 60)
    return f"{minutos} min {segundos} s" if minutos else f"{segundos} s"

async def descargar_video(url: str, calidad: str, message: Message, cancel_flag, cancel_markup):
    try:
        file_id = hashlib.md5(url.encode()).hexdigest()[:10]
        temp_filename = f"downloads/temp_{file_id}.mp4"
        output_filename = (f"downloads/audio_{file_id}.mp3" if calidad == "audio"
                           else f"downloads/video_{file_id}.mp4")
        loop = asyncio.get_running_loop()
        progress_hook_func = make_progress_hook(message, loop, cancel_flag, cancel_markup)
        opciones = {
            'outtmpl': temp_filename,
            'noplaylist': True,
            'merge_output_format': 'mp4',
            'retries': 5,
            'fragment_retries': 10,
            'skip_unavailable_fragments': True,
            'progress_hooks': [progress_hook_func],
        }
        opciones['format'] = formato_para_calidad(calidad)
        await update_message_text(message, "📥 **Iniciando la descarga...**", reply_markup=cancel_markup)
        await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(opciones).download([url]))
        if cancel_flag.is_set():
            return None
        if calidad == "audio":
            if not os.path.exists(f"{temp_filename}.mp3"):
                await update_message_text(message, "🎵 **Convirtiendo manualmente a MP3...**")
                conversion_ok = await convertir_a_mp3(temp_filename, output_filename)
                if not conversion_ok or not os.path.exists(output_filename):
                    await update_message_text(message, "❌ **Error en la conversión de audio.**")
                    return None
            else:
                os.replace(f"{temp_filename}.mp3", output_filename)
        else:
            if os.path.exists(temp_filename):
                os.replace(temp_filename, output_filename)
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
        return output_filename
    except Exception as e:
        logging.error(f"❌ Error en la descarga: {e}")
        await update_message_text(message, f"❌ **Error en la descarga:** {str(e)}")
        return None

# ──────────────────────────────────────────────#
# HANDLERS PARA SUBTÍTULOS Y MINIATURAS
# ──────────────────────────────────────────────#
@bot.on_callback_query(filters.regex(r"^sub\|(.+)$"))
async def handle_subtitles_callback(_, callback_query: Message):
    video_id = callback_query.data.split("|")[1]
    url = video_links.get(video_id)
    if not url:
        await callback_query.message.edit_text("❌ **Error: No se encontró el video.**")
        return
    info = extraer_info(url, "video")
    if not info:
        await callback_query.message.edit_text("❌ **Error al extraer información para los subtítulos.**")
        return
    subs = info.get("subtitles") or info.get("automatic_captions")
    if not subs:
        await callback_query.message.edit_text("❌ **No se encontraron subtítulos disponibles.**")
        return
    lang = list(subs.keys())[0]
    sub_info = subs[lang][0] if subs[lang] else None
    if not sub_info or "url" not in sub_info:
        await callback_query.message.edit_text("❌ **No se pudo obtener la URL de los subtítulos.**")
        return
    sub_url = sub_info["url"]
    try:
        r = requests.get(sub_url)
        r.raise_for_status()
        sub_filename = f"downloads/sub_{video_id}_{lang}.srt"
        with open(sub_filename, "wb") as f:
            f.write(r.content)
        await callback_query.message.reply_document(document=sub_filename, caption=f"📝 Subtítulos ({lang})")
        os.remove(sub_filename)
    except Exception as e:
        logging.error(f"❌ Error al descargar los subtítulos: {e}")
        await callback_query.message.edit_text(f"❌ **Error al descargar los subtítulos:** {str(e)}")

@bot.on_callback_query(filters.regex(r"^thumb\|(.+)$"))
async def handle_thumbnail_callback(_, callback_query: Message):
    video_id = callback_query.data.split("|")[1]
    url = video_links.get(video_id)
    if not url:
        await callback_query.message.edit_text("❌ **Error: No se encontró el video.**")
        return
    info = extraer_info(url, "video")
    if not info or "thumbnail" not in info:
        await callback_query.message.edit_text("❌ **No se encontró miniatura disponible.**")
        return
    thumb_url = info["thumbnail"]
    try:
        r = requests.get(thumb_url)
        r.raise_for_status()
        thumb_filename = f"downloads/thumb_{video_id}.jpg"
        with open(thumb_filename, "wb") as f:
            f.write(r.content)
        await callback_query.message.reply_photo(photo=thumb_filename, caption="🖼️ Miniatura")
        os.remove(thumb_filename)
    except Exception as e:
        logging.error(f"❌ Error al descargar la miniatura: {e}")
        await callback_query.message.edit_text(f"❌ **Error al descargar la miniatura:** {str(e)}")

# ──────────────────────────────────────────────#
# HANDLERS DEL BOT
# ──────────────────────────────────────────────#
@bot.on_message(filters.command("start"))
async def start(_, message: Message):
    if message.from_user.id not in ALLOWED_USERS:
        await message.reply_text("🚫 **No tienes permiso para usar este bot.**")
        return
    await message.reply_text("👋 ¡Hola! Envíame un enlace de video y elige la calidad o alguna opción extra.")

@bot.on_message(filters.command("help"))
async def help_command(_, message: Message):
    help_text = (
        "👋 **Bienvenido al bot de descarga de videos!**\n\n"
        "🔹 **Comandos disponibles:**\n"
        "/start - Inicia el bot\n"
        "/help - Muestra esta ayuda\n"
        "/about - Información sobre el bot\n\n"
        "Opciones:\n"
        "   • Descargar video en la calidad seleccionada\n"
        "   • Extraer solo audio (MP3)\n"
        "   • Descargar subtítulos (si están disponibles)\n"
        "   • Obtener la miniatura del video\n\n"
        "Asegúrate de ser un usuario autorizado."
    )
    await message.reply_text(help_text)

@bot.on_message(filters.command("about"))
async def about_command(_, message: Message):
    about_text = (
        "🤖 **Bot de Descarga de Videos**\n\n"
        "Este bot permite descargar videos, extraer audio, obtener subtítulos y la miniatura de enlaces compatibles (por ejemplo, YouTube).\n"
        "Está construido con Pyrogram y yt-dlp, y ofrece feedback interactivo durante la descarga y subida.\n\n"
        "Desarrollado para mejorar la experiencia del usuario mediante comandos y mensajes informativos."
    )
    await message.reply_text(about_text)

@bot.on_message(filters.text & filters.private)
async def handle_download_request(_, message: Message):
    if message.from_user.id not in ALLOWED_USERS:
        await message.reply_text("🚫 **No tienes permiso para usar este bot.**")
        return
    url_input = message.text.strip()
    url = validar_url(url_input)
    if not url:
        await message.reply_text("❌ **URL no válida.** Proporciona un enlace válido.")
        return
    await message.reply_text("🔍 **Intentando analizar el video...**")
    info = extraer_info(url, "video")
    if not info:
        await message.reply_text("❌ **Error al analizar la información del video.**")
        return
    if "entries" in info and len(info["entries"]) > 1:
        playlist_notice = f"⚠️ Se han detectado {len(info['entries'])} videos en la publicación.\nSe descargarán todos con la resolución elegida."
    else:
        playlist_notice = ""
    resoluciones = obtener_resoluciones(url)
    if resoluciones == "LIVE_BLOCKED":
        await message.reply_text("🚫 **No es posible descargar transmisiones en vivo de YouTube.**")
        return
    if not resoluciones:
        await message.reply_text("❌ **No se encontraron resoluciones disponibles o el enlace no es válido.**")
        return
    extra_buttons = []
    if info:
        if info.get("subtitles") or info.get("automatic_captions"):
            extra_buttons.append([InlineKeyboardButton("📝 Subtítulos", callback_data=f"sub|{hashlib.md5(url.encode()).hexdigest()[:10]}")])
        if "thumbnail" in info:
            extra_buttons.append([InlineKeyboardButton("🖼️ Miniatura", callback_data=f"thumb|{hashlib.md5(url.encode()).hexdigest()[:10]}")])
    video_id = hashlib.md5(url.encode()).hexdigest()[:10]
    video_links[video_id] = url
    botones = [[InlineKeyboardButton(f"📺 {res}", callback_data=f"dl|{video_id}|{res}")]
               for res in resoluciones]
    botones.append([InlineKeyboardButton("🎵 Solo Audio (MP3)", callback_data=f"dl|{video_id}|audio")])
    if extra_buttons:
        botones.extend(extra_buttons)
    await message.reply_text(f"📥 **Elige una opción:**\n{playlist_notice}", reply_markup=InlineKeyboardMarkup(botones))

@bot.on_callback_query(filters.regex(r"^(dl|sub|thumb)\|"))
async def handle_download_callback(_, callback_query: Message):
    data = callback_query.data.split("|")
    prefix = data[0]
    video_id = data[1]
    url = video_links.get(video_id)
    if not url:
        await callback_query.message.edit_text("❌ **Error: No se encontró el video.**")
        return

    if prefix == "dl":
        try:
            calidad = data[2]
            if lock_descarga.locked():
                await callback_query.answer("⚠️ Actualmente se está procesando otra descarga. Espera a que finalice.", show_alert=True)
                return

            cancel_flag = threading.Event()
            cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"cancel|{video_id}")]])
            download_cancel_flags[video_id] = cancel_flag

            async with lock_descarga:
                info = extraer_info(url, "video")
                if "entries" in info and len(info["entries"]) > 1:
                    entries = info["entries"]
                    # Filtrar entradas duplicadas
                    unique_entries = []
                    urls_seen = set()
                    for entry in entries:
                        entry_url = entry.get("webpage_url") or entry.get("url")
                        if not entry_url:
                            continue
                        if entry_url in urls_seen:
                            continue
                        urls_seen.add(entry_url)
                        unique_entries.append(entry)
                    await callback_query.message.edit_text(f"⏳ **Descargando {len(unique_entries)} videos en {calidad}...**")
                    for idx, entry in enumerate(unique_entries, start=1):
                        if cancel_flag.is_set():
                            await callback_query.message.edit_text("❌ **Descarga cancelada por el usuario.**")
                            break
                        entry_url = entry.get("webpage_url") or entry.get("url")
                        if not entry_url:
                            logging.error(f"❌ No se encontró URL para el video {idx}.")
                            continue
                        await callback_query.message.edit_text(f"⏳ **Verificando tamaño del video {idx}/{len(unique_entries)}...**")
                        tamano_video_mb = verificar_tamano_video(entry_url, calidad)
                        if tamano_video_mb > 2048:
                            await callback_query.message.edit_text(f"🚫 **El video {idx} excede el límite de 2 GB y será omitido.**")
                            continue
                        video_path = await descargar_video(entry_url, calidad, callback_query.message, cancel_flag, cancel_markup)
                        if not video_path:
                            continue
                        tamano_archivo_mb = os.path.getsize(video_path) / (1024 * 1024)
                        tiempo_estimado = estimar_tiempo_subida(tamano_archivo_mb)
                        nuevo_texto = (f"⏫ **Subiendo video {idx}/{len(unique_entries)}...**\n\n"
                                       f"📦 Tamaño: {tamano_archivo_mb:.2f} MB\n"
                                       f"⏱️ Tiempo estimado: {tiempo_estimado}")
                        await callback_query.message.edit_text(nuevo_texto)
                        loop = asyncio.get_running_loop()
                        upload_progress = make_upload_progress_hook(callback_query.message, loop)
                        if calidad == "audio":
                            await callback_query.message.reply_audio(
                                audio=video_path,
                                caption=f"🎵 **Video {idx} (Audio)**\n📦 Tamaño: {tamano_archivo_mb:.2f} MB",
                                progress=upload_progress
                            )
                        else:
                            await callback_query.message.reply_video(
                                video=video_path,
                                caption=f"📹 **Video {idx}** - {calidad}\n📦 Tamaño: {tamano_archivo_mb:.2f} MB",
                                progress=upload_progress
                            )
                        if os.path.exists(video_path):
                            os.remove(video_path)
                else:
                    # Caso de video único
                    await callback_query.message.edit_text(f"⏳ **Verificando el tamaño del archivo para {calidad}...**")
                    tamano_video_mb = verificar_tamano_video(url, calidad)
                    if tamano_video_mb > 2048:
                        await callback_query.message.edit_text("🚫 **El video excede el límite de 2 GB permitido por Telegram. Elige otra opción.**")
                        del video_links[video_id]
                        if video_id in download_cancel_flags:
                            del download_cancel_flags[video_id]
                        return
                    video_path = await descargar_video(url, calidad, callback_query.message, cancel_flag, cancel_markup)
                    if not video_path:
                        return
                    tamano_archivo_mb = os.path.getsize(video_path) / (1024 * 1024)
                    tiempo_estimado = estimar_tiempo_subida(tamano_archivo_mb)
                    nuevo_texto = (f"⏫ **Subiendo el archivo...**\n\n"
                                   f"📦 Tamaño: {tamano_archivo_mb:.2f} MB\n"
                                   f"⏱️ Tiempo estimado: {tiempo_estimado}")
                    await callback_query.message.edit_text(nuevo_texto)
                    loop = asyncio.get_running_loop()
                    upload_progress = make_upload_progress_hook(callback_query.message, loop)
                    if calidad == "audio":
                        await callback_query.message.reply_audio(
                            audio=video_path,
                            caption=f"🎵 **Aquí tienes tu audio!**\n📦 Tamaño: {tamano_archivo_mb:.2f} MB",
                            progress=upload_progress
                        )
                    else:
                        await callback_query.message.reply_video(
                            video=video_path,
                            caption=f"📹 **Aquí tienes tu video en {calidad}!**\n📦 Tamaño: {tamano_archivo_mb:.2f} MB",
                            progress=upload_progress
                        )
                    if os.path.exists(video_path):
                        os.remove(video_path)
                if video_id in video_links:
                    del video_links[video_id]
                if video_id in download_cancel_flags:
                    del download_cancel_flags[video_id]
        except Exception as e:
            logging.error(f"❌ Error en callback (descarga): {e}")
            await callback_query.message.edit_text(f"❌ **Error:** {str(e)}")
    elif prefix == "sub":
        info = extraer_info(url, "video")
        if not info:
            await callback_query.message.edit_text("❌ **Error al extraer información para los subtítulos.**")
            return
        subs = info.get("subtitles") or info.get("automatic_captions")
        if not subs:
            await callback_query.message.edit_text("❌ **No se encontraron subtítulos disponibles.**")
            return
        lang = list(subs.keys())[0]
        sub_info = subs[lang][0] if subs[lang] else None
        if not sub_info or "url" not in sub_info:
            await callback_query.message.edit_text("❌ **No se pudo obtener la URL de los subtítulos.**")
            return
        sub_url = sub_info["url"]
        try:
            r = requests.get(sub_url)
            r.raise_for_status()
            sub_filename = f"downloads/sub_{video_id}_{lang}.srt"
            with open(sub_filename, "wb") as f:
                f.write(r.content)
            await callback_query.message.reply_document(document=sub_filename, caption=f"📝 Subtítulos ({lang})")
            os.remove(sub_filename)
        except Exception as e:
            logging.error(f"❌ Error al descargar los subtítulos: {e}")
            await callback_query.message.edit_text(f"❌ **Error al descargar los subtítulos:** {str(e)}")
    elif prefix == "thumb":
        info = extraer_info(url, "video")
        if not info or "thumbnail" not in info:
            await callback_query.message.edit_text("❌ **No se encontró miniatura disponible.**")
            return
        thumb_url = info["thumbnail"]
        try:
            r = requests.get(thumb_url)
            r.raise_for_status()
            thumb_filename = f"downloads/thumb_{video_id}.jpg"
            with open(thumb_filename, "wb") as f:
                f.write(r.content)
            await callback_query.message.reply_photo(photo=thumb_filename, caption="🖼️ Miniatura")
            os.remove(thumb_filename)
        except Exception as e:
            logging.error(f"❌ Error al descargar la miniatura: {e}")
            await callback_query.message.edit_text(f"❌ **Error al descargar la miniatura:** {str(e)}")

@bot.on_callback_query(filters.regex(r"^cancel\|(.+)$"))
async def handle_cancel_callback(_, callback_query: Message):
    video_id = callback_query.data.split("|")[1]
    if video_id in download_cancel_flags:
        cancel_flag = download_cancel_flags[video_id]
        cancel_flag.set()
        await callback_query.answer("Descarga cancelada.", show_alert=True)
        await callback_query.message.edit_text("❌ **Descarga cancelada por el usuario.**")
    else:
        await callback_query.answer("No hay una descarga activa para cancelar.", show_alert=True)

# ──────────────────────────────────────────────#
# EJECUCIÓN DEL BOT
# ──────────────────────────────────────────────#
if __name__ == "__main__":
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    try:
        logging.info("🚀 Bot iniciado correctamente.")
        bot.run()
    except Exception as e:
        logging.error(f"❌ Error al ejecutar el bot: {e}")



