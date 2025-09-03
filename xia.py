import asyncio
import json
import re
import os
import sys
from subprocess import PIPE

import aiohttp
import websockets

# ===================================================================
# 您的配置
# ===================================================================
ONEBOT_WS_URL = "ws://127.0.0.1:14000"
ONEBOT_API_ROOT = "http://127.0.0.1:14500"
ONEBOT_ACCESS_TOKEN = ""
PROXY_URL = "http://127.0.0.1:7897"
DOWNLOAD_RETRIES = 3
HIRES_THRESHOLD_MB = 20
HIRES_FORMAT_ID = "30251"
QUALITY_MAP = {"1": "20216", "2": "30232", "3": "30280", "4": "bestaudio"}
# ===================================================================

# --- 全局变量和路径设置 ---
URL_REGEX = r'(https?:\/\/)?([^\s\/]+\.[^\s\/]+)(\/[^\s]*)?'
script_dir = os.path.dirname(os.path.abspath(__file__))
curl_bin_path = os.path.join(script_dir, 'curl')
ffmpeg_bin_path = os.path.join(script_dir, 'ffmpeg', 'bin')
ffmpeg_exe_path = os.path.join(ffmpeg_bin_path, 'ffmpeg.exe')
cookies_json_path = os.path.join(script_dir, 'bilicookie.json')
user_states = {}

def get_modified_env():
    env = os.environ.copy()
    env['PATH'] = f"{curl_bin_path}{os.pathsep}{ffmpeg_bin_path}{os.pathsep}{env.get('PATH', '')}"
    env['PYTHONIOENCODING'] = 'utf-8'
    return env
    
def get_session_id(event: dict) -> str:
    user_id = event['user_id']
    if event['message_type'] == 'group':
        group_id = event['group_id']
        return f"group_{group_id}_{user_id}"
    else:
        return f"private_{user_id}"

def is_bilibili_link(url: str) -> bool:
    url_lower = url.lower()
    return 'bilibili.com' in url_lower or 'b23.tv' in url_lower

# --- 核心工具函数 ---
async def run_command_exec(command_parts: list[str]) -> tuple[int, str, str]:
    print(f"[命令执行器] 执行: {' '.join(command_parts)}")
    process = await asyncio.create_subprocess_exec(*command_parts, stdout=PIPE, stderr=PIPE, env=get_modified_env())
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode('utf-8', 'ignore'), stderr.decode('utf-8', 'ignore')

async def run_command_stream_exec(command_parts: list[str]) -> int:
    print(f"[命令执行器] 执行: {' '.join(command_parts)}")
    process = await asyncio.create_subprocess_exec(*command_parts, stdout=PIPE, stderr=PIPE, env=get_modified_env())
    async def read_stream(stream, prefix):
        while True:
            line = await stream.readline()
            if not line: break
            print(f"{prefix} {line.decode('utf-8', 'ignore').strip()}")
    await asyncio.gather(
        read_stream(process.stdout, "[yt-dlp/curl]"),
        read_stream(process.stderr, "[yt-dlp ERR]")
    )
    return await process.wait()

def convert_json_to_netscape(json_path: str) -> str | None:
    if not os.path.exists(json_path): return None
    try:
        with open(json_path, 'r', encoding='utf-8') as f: cookies = json.load(f)
        netscape_cookies = ["# Netscape HTTP Cookie File"]
        for cookie in cookies:
            domain, path, secure, expires, name, value = cookie.get("domain",""), cookie.get("path","/"), "TRUE" if cookie.get("secure") else "FALSE", str(int(cookie.get("expirationDate",0))), cookie.get("name",""), cookie.get("value","")
            if name: netscape_cookies.append(f'{domain}\t{"TRUE" if domain.startswith(".") else "FALSE"}\t{path}\t{secure}\t{expires}\t{name}\t{value}')
        return "\n".join(netscape_cookies)
    except Exception as e:
        print(f"[Cookie转换错误] {e}")
        return None

# --- OneBot 通信函数 (无变动) ---
async def send_text_reply(session: aiohttp.ClientSession, event: dict, message: str):
    print(f"\n[发送模块] 准备发送文本: '{message[:30]}...'")
    api_url, payload = f"{ONEBOT_API_ROOT}/send_msg", {"message_type": event["message_type"], "message": message}
    if event["message_type"] == "private": payload["user_id"] = event["user_id"]
    elif event["message_type"] == "group": payload["group_id"] = event["group_id"]
    headers = {'Authorization': f'Bearer {ONEBOT_ACCESS_TOKEN}'} if ONEBOT_ACCESS_TOKEN else {}
    try:
        async with session.post(api_url, json=payload, headers=headers, timeout=20) as response:
            if response.status != 200: print(f"[发送模块] 文本发送失败，状态码: {response.status}, 响应: {await response.text()}")
    except Exception as e:
        print(f"[发送模块] 文本发送时网络错误: {e}")

async def upload_group_file(session: aiohttp.ClientSession, event: dict, file_path: str):
    print(f"\n[文件上传模块] 准备上传群文件: {file_path}")
    if event.get("message_type") != "group":
        await send_text_reply(session, event, f"下载成功！但我只能在群聊中上传文件。文件保存在我的本地: {os.path.basename(file_path)}")
        return
    file_uri = f"file:///{os.path.abspath(file_path)}"
    api_url = f"{ONEBOT_API_ROOT}/upload_group_file"
    payload = {"group_id": event["group_id"], "file": file_uri, "name": os.path.basename(file_path)}
    print(f"[文件上传模块] 构建API请求 (使用File URI): {payload}")
    headers = {'Authorization': f'Bearer {ONEBOT_ACCESS_TOKEN}'} if ONEBOT_ACCESS_TOKEN else {}
    try:
        async with session.post(api_url, json=payload, headers=headers, timeout=600) as response:
            if response.status != 200:
                print(f"[文件上传模块] 上传指令发送失败，状态码: {response.status}, 响应: {await response.text()}")
                await send_text_reply(session, event, f"文件上传失败，请检查后台日志。")
    except Exception as e:
        print(f"[文件上传模块] 上传时发生网络错误: {e}")
        await send_text_reply(session, event, f"文件上传时发生网络错误，请检查后台日志。")

# --- 核心下载与处理逻辑 ---

async def is_link_supported_by_ytdlp(url: str) -> bool:
    print(f"[验证模块] 正在使用 yt-dlp 验证链接: {url}")
    command = ['yt-dlp', '--proxy', PROXY_URL, '--no-check-certificate', '--skip-download', '--print-json', url]
    
    # [核心修复] 在验证B站链接时也必须带上Cookie
    temp_cookie_file = None
    if is_bilibili_link(url):
        cookies_netscape_str = convert_json_to_netscape(cookies_json_path)
        if cookies_netscape_str:
            temp_cookie_file = os.path.join(script_dir, "_temp_cookies_validate.txt")
            with open(temp_cookie_file, 'w', encoding='utf-8') as f: f.write(cookies_netscape_str)
            command.extend(['--cookies', temp_cookie_file])

    returncode, _, stderr = await run_command_exec(command)
    
    if temp_cookie_file and os.path.exists(temp_cookie_file):
        os.remove(temp_cookie_file)
        
    if returncode != 0:
        print(f"[验证模块] 链接无效或 yt-dlp 不支持。错误: {stderr[:200]}...")
        return False
    return True

async def get_best_audio_format_id(url: str) -> str | None:
    print(f"[验证模块] 正在获取最佳音质ID: {url}")
    command = ['yt-dlp', '--proxy', PROXY_URL, '--no-check-certificate', '-f', 'bestaudio', '--print', 'format_id', '--no-download', url]
    temp_cookie_file = None
    if is_bilibili_link(url):
        cookies_netscape_str = convert_json_to_netscape(cookies_json_path)
        if cookies_netscape_str:
            temp_cookie_file = os.path.join(script_dir, "_temp_cookies_check.txt")
            with open(temp_cookie_file, 'w', encoding='utf-8') as f: f.write(cookies_netscape_str)
            command.extend(['--cookies', temp_cookie_file])
    returncode, stdout, _ = await run_command_exec(command)
    if temp_cookie_file and os.path.exists(temp_cookie_file): os.remove(temp_cookie_file)
    return stdout.strip() if returncode == 0 else None

async def convert_to_flac(m4a_path: str) -> str:
    print(f"[转换模块] 检测到Hi-Res M4A文件，开始无损转换为FLAC...")
    flac_path = m4a_path.rsplit('.', 1)[0] + '.flac'
    command = [ffmpeg_exe_path, '-i', m4a_path, '-y', flac_path]
    returncode, _, stderr = await run_command_exec(command)
    if returncode == 0 and os.path.exists(flac_path):
        print(f"[转换模块] 成功转换为FLAC: {os.path.basename(flac_path)}")
        try: os.remove(m4a_path)
        except OSError as e: print(f"[警告] 删除原始M4A文件失败: {e}")
        return flac_path
    else:
        print(f"[转换模块] [错误] 转换为FLAC失败，将保留原始文件。错误: {stderr}")
        return m4a_path

async def ensure_mp3_format(input_path: str) -> str:
    if input_path.lower().endswith('.mp3'): return input_path
    print(f"[转换模块] 检测到非MP3格式音频，开始转换为MP3...")
    mp3_path = input_path.rsplit('.', 1)[0] + '.mp3'
    command = [ffmpeg_exe_path, '-i', input_path, '-vn', '-ab', '320k', '-y', mp3_path]
    returncode, _, stderr = await run_command_exec(command)
    if returncode == 0 and os.path.exists(mp3_path):
        print(f"[转换模块] 成功转换为MP3: {os.path.basename(mp3_path)}")
        try: os.remove(input_path)
        except OSError as e: print(f"[警告] 删除原始文件失败: {e}")
        return mp3_path
    else:
        print(f"[转换模块] [错误] 转换为MP3失败，将保留原始文件。错误: {stderr}")
        return input_path

async def embed_metadata_and_rename(original_path: str, title: str, artist: str) -> str | None:
    print(f"[元数据模块] 准备为 '{os.path.basename(original_path)}' 嵌入元数据...")
    sanitized_title = re.sub(r'[\\/*?:"<>|]', '_', title)
    _, extension = os.path.splitext(original_path)
    new_filename = f"{artist} - {sanitized_title}{extension}"
    new_path = os.path.join(script_dir, new_filename)
    command = [ffmpeg_exe_path, '-i', original_path, '-codec', 'copy', '-metadata', f'title={title}', '-metadata', f'artist={artist}', '-y', new_path]
    returncode, _, stderr = await run_command_exec(command)
    if returncode == 0 and os.path.exists(new_path):
        print(f"[元数据模块] 成功嵌入元数据并重命名为: {new_filename}")
        try: os.remove(original_path)
        except OSError as e: print(f"[警告] 删除原始文件 {os.path.basename(original_path)} 失败: {e}")
        return new_path
    else:
        print(f"[元数据模块] [错误] 嵌入元数据失败。将保留原始文件。错误: {stderr}")
        return original_path

async def download_media(url: str, media_type: str, format_selector: str | None = None) -> str | None:
    media_type_str = "音频" if media_type == "audio" else "视频"
    print(f"\n[下载模块] 开始处理 {media_type_str} 请求: {url}")
    
    meta_command_parts = ['yt-dlp', '--proxy', PROXY_URL, '--no-check-certificate', '--socket-timeout', '60', '--no-playlist', '--skip-download', '--print-json', url]
    temp_cookie_file = None
    if is_bilibili_link(url):
        cookies_netscape_str = convert_json_to_netscape(cookies_json_path)
        if cookies_netscape_str:
            temp_cookie_file = os.path.join(script_dir, "_temp_cookies.txt")
            with open(temp_cookie_file, 'w', encoding='utf-8') as f: f.write(cookies_netscape_str)
            meta_command_parts.extend(['--cookies', temp_cookie_file])
    
    returncode_meta, stdout_meta, stderr_meta = await run_command_exec(meta_command_parts)
    if returncode_meta != 0:
        print(f"[下载模块] 获取元数据失败: \n{stderr_meta}")
        if temp_cookie_file: os.remove(temp_cookie_file)
        return None
        
    try:
        metadata = json.loads(stdout_meta)
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', metadata.get('title', 'untitled'))
        extension = metadata.get('ext', 'mp4') if media_type == 'video' else metadata.get('aext', 'm4a')
        filename = f"{safe_title}.{extension}"
        full_path = os.path.join(script_dir, filename)
    except Exception as e:
        print(f"[下载模块] 解析元数据JSON失败: {e}")
        if temp_cookie_file: os.remove(temp_cookie_file)
        return None

    print(f"[下载模块] 步骤 2/3: 开始使用 curl 下载媒体文件...")
    dl_command_parts = ['yt-dlp', '--proxy', PROXY_URL, '--downloader', 'curl', '--downloader-args', 'curl:-k', '--no-check-certificate', '--socket-timeout', '60', '--no-playlist', '--ffmpeg-location', ffmpeg_bin_path, '--no-mtime', '--retries', '10', '-o', full_path, '--force-overwrites', url]
    
    if media_type == 'audio':
        dl_command_parts.extend(['--extract-audio'])
        if format_selector:
             dl_command_parts.extend(['-f', format_selector])
    if temp_cookie_file: dl_command_parts.extend(['--cookies', temp_cookie_file])
    
    returncode_dl = -1
    for attempt in range(DOWNLOAD_RETRIES):
        print(f"[下载模块] 开始第 {attempt + 1}/{DOWNLOAD_RETRIES} 次下载尝试...")
        returncode_dl = await run_command_stream_exec(dl_command_parts)
        if returncode_dl == 0:
            print("[下载模块] 下载尝试成功！")
            break
        if attempt < DOWNLOAD_RETRIES - 1:
            print(f"[下载模块] 第 {attempt + 1} 次下载尝试失败。将在5秒后重试...")
            await asyncio.sleep(5)
        else:
            print(f"[下载模块] 所有 {DOWNLOAD_RETRIES} 次下载尝试均已失败。")

    if temp_cookie_file: os.remove(temp_cookie_file)
    if returncode_dl != 0:
        print(f"[下载模块] 下载最终失败。")
        return None
    
    actual_path = None
    base_name = os.path.splitext(full_path)[0]
    for ext in ['.m4a', '.webm', '.opus', '.mp3', '.flac']:
        potential_path = f"{base_name}{ext}"
        if os.path.exists(potential_path):
            actual_path = potential_path
            break
    if not actual_path:
        if os.path.exists(full_path):
            actual_path = full_path
        else:
            print(f"[下载模块] [错误] 下载声称成功，但找不到最终文件")
            return None
    print(f"[下载模块] 下载成功，文件位于: {actual_path}")
    
    if media_type == 'video' and not actual_path.lower().endswith('.mp4'):
        print(f"[转换模块] 步骤 3/3: 检测到非MP4格式，开始无损转换为MP4...")
        mp4_path = actual_path.rsplit('.', 1)[0] + '.mp4'
        convert_command = [ffmpeg_exe_path, '-i', actual_path, '-c', 'copy', '-y', mp4_path]
        returncode_conv, _, stderr_conv = await run_command_exec(convert_command)
        if returncode_conv == 0 and os.path.exists(mp4_path):
            print(f"[转换模块] 转换为MP4成功，新文件: {mp4_path}")
            try: os.remove(actual_path)
            except OSError as e: print(f"[警告] 删除原始文件 {os.path.basename(actual_path)} 失败: {e}")
            return os.path.abspath(mp4_path)
        else:
            print(f"[转换模块] [错误] 转换为MP4失败。将保留原始文件。错误信息:\n{stderr_conv}")
            return os.path.abspath(actual_path)
    return os.path.abspath(actual_path)

# --- 任务与消息处理 ---

async def cancel_after_timeout(session: aiohttp.ClientSession, event: dict, session_id: str, delay: int):
    try:
        await asyncio.sleep(delay)
        if session_id in user_states:
            state_info = user_states[session_id]["state"]
            print(f"[状态管理] 会话 {session_id} 在 '{state_info}' 状态超时。")
            del user_states[session_id]
            await send_text_reply(session, event, "您在60秒内未响应，请求已自动取消。")
    except asyncio.CancelledError:
        print(f"[状态管理] 会话 {session_id} 的超时计时器已正常取消。")

async def start_media_processing_task(session: aiohttp.ClientSession, event: dict, url: str, media_type: str, song_name: str | None = None, artist_name: str | None = None, format_selector: str | None = None):
    media_type_str = "音频" if media_type == "audio" else "视频"
    print(f"\n[主逻辑] 进入 {media_type_str} 下载通知流程...")
    
    downloaded_path = await download_media(url, media_type, format_selector)
    
    if not downloaded_path:
        await send_text_reply(session, event, f"{media_type_str}下载或处理失败，请检查后台日志。\n{url}")
        return

    post_processed_path = downloaded_path
    if media_type == 'audio':
        if downloaded_path.lower().endswith('.m4a') and os.path.getsize(downloaded_path) > HIRES_THRESHOLD_MB * 1024 * 1024:
            post_processed_path = await convert_to_flac(downloaded_path)
        else:
            post_processed_path = await ensure_mp3_format(downloaded_path)
            
    final_path = post_processed_path
    if media_type == 'audio' and song_name and artist_name:
        final_path = await embed_metadata_and_rename(post_processed_path, song_name, artist_name)
        if not final_path:
             await send_text_reply(session, event, "音频元数据嵌入失败，请检查后台日志。")
             return

    user_id = event['user_id']
    at_mention = f"[CQ:at,qq={user_id}] "
    success_message = f"{at_mention}{media_type_str}《{os.path.basename(final_path)}》处理成功！\n正在上传到群文件，请稍候..."
    
    await send_text_reply(session, event, success_message)
    await upload_group_file(session, event, final_path)

    if event.get("message_type") == "group":
        try:
            print(f"[文件管理] 上传完成，正在删除本地文件: {os.path.basename(final_path)}")
            os.remove(final_path)
        except OSError as e:
            print(f"[文件管理] [错误] 删除文件失败: {e}")

async def _validate_link_and_proceed(session: aiohttp.ClientSession, session_id: str, url: str):
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        
    if session_id not in user_states: return
    original_event = user_states[session_id]["data"]["event"]
    is_valid = await is_link_supported_by_ytdlp(url)
    if session_id not in user_states: return
    
    if not is_valid:
        del user_states[session_id]
        await send_text_reply(session, original_event, "此链接无效或不受支持，请求已取消。")
        return

    state_config = user_states[session_id]
    state_config["data"]["url"] = url
    request_type = state_config["data"]["request_type"]

    if request_type == "video":
        del user_states[session_id]
        await send_text_reply(session, original_event, "链接有效！正在下载并上传，请稍等...")
        asyncio.create_task(start_media_processing_task(session, original_event, url, request_type))
    
    elif request_type == "audio":
        if is_bilibili_link(url):
            best_format_id = await get_best_audio_format_id(url)
            if not best_format_id:
                del user_states[session_id]
                await send_text_reply(session, original_event, "无法获取音频质量信息，请求已取消。")
                return
            
            state_config["data"]["best_format_id"] = best_format_id
            prompt = "请在60秒内回复数字选择音质：\n1. 128kbps\n2. 192kbps\n3. 320kbps"
            if best_format_id == HIRES_FORMAT_ID:
                prompt += "\n4. Hi-Res (无损音质)"
            state_config["state"] = "waiting_for_quality_choice"
            state_config["timeout_task"] = asyncio.create_task(cancel_after_timeout(session, original_event, session_id, 60))
            await send_text_reply(session, original_event, prompt)
        else:
            state_config["data"]["format_selector"] = "bestaudio"
            if state_config["data"].get("artist") == "鹿乃":
                state_config["state"] = "waiting_for_song_name"
                state_config["timeout_task"] = asyncio.create_task(cancel_after_timeout(session, original_event, session_id, 60))
                await send_text_reply(session, original_event, "链接有效！请在60秒内输入歌曲名。")
            else:
                state_config["state"] = "waiting_for_artist"
                state_config["timeout_task"] = asyncio.create_task(cancel_after_timeout(session, original_event, session_id, 60))
                await send_text_reply(session, original_event, "链接有效！请在60秒内输入歌手名。")

async def handle_message(session: aiohttp.ClientSession, event: dict):
    message_text = event.get("raw_message", "").strip()
    session_id = get_session_id(event)

    if session_id in user_states:
        state_config = user_states[session_id]
        current_state = state_config["state"]
        original_event = state_config["data"]["event"]

        if current_state == "validating_link":
            await send_text_reply(session, event, "正在验证您刚刚发送的链接，请稍候...")
            return

        state_config["timeout_task"].cancel()
        
        if current_state == "waiting_for_quality_choice":
            choice = message_text
            best_format_id = state_config["data"]["best_format_id"]
            if choice not in QUALITY_MAP or (choice == "4" and best_format_id != HIRES_FORMAT_ID):
                del user_states[session_id]
                await send_text_reply(session, original_event, "无效的选项，请求已自动取消。")
                return

            state_config["data"]["format_selector"] = QUALITY_MAP[choice]
            next_state, prompt = ("waiting_for_song_name", "音质选择成功！请在60秒内输入歌曲名。") if state_config["data"].get("artist") == "鹿乃" else ("waiting_for_artist", "音质选择成功！请在60秒内输入歌手名。")
            state_config["state"] = next_state
            state_config["timeout_task"] = asyncio.create_task(cancel_after_timeout(session, original_event, session_id, 60))
            await send_text_reply(session, original_event, prompt)
            return

        elif current_state == "waiting_for_song_name":
            data = state_config["data"]
            del user_states[session_id]
            await send_text_reply(session, original_event, f"信息集齐！正在为您处理 “{data['artist']} - {message_text}”，请稍等...")
            asyncio.create_task(start_media_processing_task(session, original_event, data['url'], data['request_type'], message_text, data['artist'], data.get('format_selector')))
            return

        elif current_state == "waiting_for_artist":
            state_config["data"]["artist"] = message_text
            state_config["state"] = "waiting_for_song_name"
            state_config["timeout_task"] = asyncio.create_task(cancel_after_timeout(session, original_event, session_id, 60))
            await send_text_reply(session, original_event, "歌手名收到！请在60秒内输入歌曲名。")
            return
            
        elif current_state == "waiting_for_link":
            url_match = re.search(URL_REGEX, message_text)
            if not url_match:
                del user_states[session_id]
                await send_text_reply(session, original_event, "您发送的不是链接，请求已自动取消。")
                return
            state_config["state"] = "validating_link"
            await send_text_reply(session, original_event, "链接收到，正在验证...")
            asyncio.create_task(_validate_link_and_proceed(session, session_id, url_match.group(0)))
            return

    initial_data = None
    if message_text == "获取视频": initial_data = {"request_type": "video"}
    elif message_text == "获取音频": initial_data = {"request_type": "audio"}
    elif message_text == "下载鹿歌": initial_data = {"request_type": "audio", "artist": "鹿乃"}

    if initial_data:
        print(f"\n[主逻辑] 会话 {session_id} 触发新指令 '{message_text}'")
        if session_id in user_states:
             await send_text_reply(session, event, "您在此处上一个请求还未完成，请先响应或等待超时。")
             return
        
        initial_data["event"] = event
        timeout_task = asyncio.create_task(cancel_after_timeout(session, event, session_id, 60))
        user_states[session_id] = {"state": "waiting_for_link", "timeout_task": timeout_task, "data": initial_data}
        await send_text_reply(session, event, "请在60秒内发送链接。")
        return

async def main():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with websockets.connect(ONEBOT_WS_URL) as websocket:
                    print(f"成功连接到 OneBot WebSocket 服务端: {ONEBOT_WS_URL}")
                    async for message in websocket:
                        try:
                            event = json.loads(message)
                            if event.get("post_type") == "message": asyncio.create_task(handle_message(session, event))
                        except json.JSONDecodeError: pass
            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError) as e:
                print(f"连接断开或被拒绝: {e}。将在5秒后重试...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"发生未知错误: {e}。将在5秒后重试...")
                await asyncio.sleep(5)

if __name__ == "__main__":
    if not os.path.exists(ffmpeg_exe_path): print("="*50 + f"\n[错误] 未找到 'ffmpeg.exe'！\n请确保它位于: {ffmpeg_exe_path}\n" + "="*50)
    print("机器人客户端启动中...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n机器人已手动关闭。")
