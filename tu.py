import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import httpx
import websockets

# --- 1. 核心配置 ---
WEBSOCKET_URI = "ws://127.0.0.1:15400"
# !!! 重要：请在 NapCat 设置中找到您的 HTTP 服务端口并替换下面的 15300 !!!
ONEBOT_HTTP_API_URL = "http://127.0.0.1:15300"

IMAGE_ROOT_DIR_NAME = "鹿图"
IMAGE_SIZE_LIMIT_MB = 50
SEND_TIMEOUT_SECONDS = 5
SUBMISSION_AWAIT_IMAGE_TIMEOUT = 60
SUBMISSION_STEP_TIMEOUT = 30

# 日志设置
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)

# --- 2. 投稿会话状态管理 ---
@dataclass
class SubmissionSession:
    group_id: int
    state: str = 'awaiting_image'
    timeout_task: Optional[asyncio.Task] = None
    image_url: Optional[str] = None
    image_ext: Optional[str] = None
    artist_name: Optional[str] = None
    source_prefix: Optional[str] = None
    artwork_id: Optional[str] = None

USER_SESSIONS: Dict[int, SubmissionSession] = {}
_session_lock = asyncio.Lock()
SOURCE_MAP = {"11": "X", "22": "B", "33": "P", "44": "BV"}

# --- 3. HTTP API 发送模块 ---
async def send_group_msg(group_id: int, message: list):
    async with httpx.AsyncClient() as client:
        try:
            payload = {"group_id": group_id, "message": message}
            url = f"{ONEBOT_HTTP_API_URL}/send_group_msg"
            response = await client.post(url, json=payload, timeout=20)
            response.raise_for_status()
            response_data = response.json()
            if response_data.get("status") == "ok":
                logger.info(f"成功向群 {group_id} 发送消息。")
            else:
                logger.warning(f"发送消息到群 {group_id} 的业务状态异常: {response_data}")
        except httpx.RequestError as e:
            logger.error(f"发送消息到群 {group_id} 时发生网络错误: {e}")
        except Exception:
            logger.exception(f"发送消息到群 {group_id} 时发生未知错误。")

async def download_image(url: str) -> Optional[bytes]:
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'}
    try:
        url = url.replace("&amp;", "&")
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=60, follow_redirects=True)
            response.raise_for_status()
            return response.content
    except Exception:
        logger.exception(f"下载图片失败: {url}")
        return None

# --- 4. 业务逻辑处理模块 ---
def extract_image_url_from_cq_code(message_str: str) -> Optional[str]:
    if not isinstance(message_str, str): return None
    match = re.search(r"\[CQ:image,.*?url=([^,\]]+)", message_str)
    if match: return match.group(1)
    return None

async def silent_timeout_killer(user_id: int, timeout: int):
    await asyncio.sleep(timeout)
    async with _session_lock:
        if user_id in USER_SESSIONS:
            logger.info(f"用户 {user_id} 的投稿因超时 ({timeout}s) 被静默取消。")
            del USER_SESSIONS[user_id]

async def start_submission(user_id: int, group_id: int):
    async with _session_lock:
        if user_id in USER_SESSIONS:
            await send_group_msg(group_id, [{"type": "text", "data": {"text": "您当前已经在一个投稿流程中，请先完成或使用“取消投稿”来退出。"}}])
            return
        asyncio.create_task(silent_timeout_killer(user_id, SUBMISSION_AWAIT_IMAGE_TIMEOUT))
        USER_SESSIONS[user_id] = SubmissionSession(group_id=group_id)
    logger.info(f"用户 {user_id} 在群 {group_id} 开始投稿流程，设置 {SUBMISSION_AWAIT_IMAGE_TIMEOUT} 秒超时。")
    await send_group_msg(group_id, [{"type": "text", "data": {"text": f"请在 {SUBMISSION_AWAIT_IMAGE_TIMEOUT} 秒内发送您要投稿的图片（AI生成图片将被拒绝）。\n随时可以发送“取消投稿”来退出流程。"}}])

async def cancel_submission(user_id: int, group_id: int):
    async with _session_lock:
        if user_id in USER_SESSIONS:
            session = USER_SESSIONS.pop(user_id)
            if session.timeout_task: session.timeout_task.cancel()
            logger.info(f"用户 {user_id} 已取消投稿。")
            await send_group_msg(group_id, [{"type": "text", "data": {"text": "投稿流程已取消。"}}])

async def process_submission_step(user_id: int, data: dict):
    async with _session_lock:
        session = USER_SESSIONS.get(user_id)
        if not session: return

        group_id = session.group_id
        raw_message = data.get("raw_message", "").strip()
        message_content = data.get("message")
        
        if session.timeout_task:
            session.timeout_task.cancel()
        session.timeout_task = asyncio.create_task(silent_timeout_killer(user_id, SUBMISSION_STEP_TIMEOUT))
        
        if session.state == 'awaiting_image':
            image_url = None
            # --- 【最终健壮性修复】 ---
            # 优先检查消息是否为列表格式（标准格式）
            if isinstance(message_content, list):
                image_segment = next((seg for seg in message_content if seg.get("type") == "image"), None)
                if image_segment:
                    image_url = image_segment.get("data", {}).get("url")
            # 如果不是列表，再检查是否为字符串格式（CQ码格式）
            elif isinstance(message_content, str):
                image_url = extract_image_url_from_cq_code(message_content)
            # --- 修复结束 ---

            if not image_url:
                if session.timeout_task: session.timeout_task.cancel()
                del USER_SESSIONS[user_id]
                await send_group_msg(group_id, [{"type": "text", "data": {"text": "您发送的不是图片，投稿流程已自动取消。" }}])
                return
            
            logger.info(f"成功提取到图片URL: {image_url}")
            session.image_url = image_url
            url_path_string = image_url.split('?')[0]
            session.image_ext = os.path.splitext(url_path_string)[-1] or '.jpg'
            if not re.match(r'\.\w+', session.image_ext): session.image_ext = '.jpg'
            session.state = 'awaiting_artist_name'
            logger.info(f"User {user_id} submitted an image, awaiting artist name.")
            await send_group_msg(group_id, [{"type": "text", "data": {"text": "图片已收到！\n请问这位画师叫什么名字？"}}])

        elif session.state == 'awaiting_artist_name':
            invalid_chars = r'[\\/:*?"<>|]'
            is_invalid = re.search(invalid_chars, raw_message) or raw_message.startswith('[CQ:')
            if not raw_message or len(raw_message) > 50 or is_invalid:
                if session.timeout_task: session.timeout_task.cancel()
                del USER_SESSIONS[user_id]
                await send_group_msg(group_id, [{"type": "text", "data": {"text": "画师名称无效，投稿流程已自动取消。"}}])
                return
            session.artist_name = raw_message
            session.state = 'awaiting_source'
            logger.info(f"User {user_id} submitted artist name: {raw_message}, awaiting source.")
            prompt = ("请标明图片来源（直接发送对应的数字编号）：\n" "  1.X动态（11）\n" "  2.b站动态（22）\n" "  3.Pixiv（33）\n" "  4.b站视频（44）")
            await send_group_msg(group_id, [{"type": "text", "data": {"text": prompt}}])
            
        elif session.state == 'awaiting_source':
            if raw_message not in SOURCE_MAP:
                if session.timeout_task: session.timeout_task.cancel()
                del USER_SESSIONS[user_id]
                await send_group_msg(group_id, [{"type": "text", "data": {"text": "来源编号无效，投稿流程已自动取消。"}}])
                return
            session.source_prefix = SOURCE_MAP[raw_message]
            session.state = 'awaiting_id'
            logger.info(f"User {user_id} submitted source: {raw_message}, awaiting ID.")
            if session.source_prefix == 'BV':
                prompt_text = "来源已确认！\n最后，请输入这张图对应视频的BV号或AV号。"
            else:
                prompt_text = "来源已确认！\n最后，请输入这张图对应的动态或作品ID（纯数字）。"
            await send_group_msg(group_id, [{"type": "text", "data": {"text": prompt_text}}])

        elif session.state == 'awaiting_id':
            is_valid_id = False
            if session.source_prefix == 'BV':
                msg_upper = raw_message.upper()
                if (msg_upper.startswith('BV') and msg_upper.isalnum()) or (msg_upper.startswith('AV') and msg_upper[2:].isdigit()):
                    is_valid_id = True
            else:
                if raw_message.isdigit():
                    is_valid_id = True
            if not is_valid_id:
                if session.timeout_task: session.timeout_task.cancel()
                del USER_SESSIONS[user_id]
                await send_group_msg(group_id, [{"type": "text", "data": {"text": "ID格式无效，投稿流程已自动取消。"}}])
                return

            session.artwork_id = raw_message
            logger.info(f"User {user_id} submitted ID: {raw_message}, process finished, preparing to save.")
            if session.timeout_task: session.timeout_task.cancel()
            image_data = await download_image(session.image_url)
            if not image_data:
                await send_group_msg(group_id, [{"type": "text", "data": {"text": "抱歉，下载图片失败，投稿中断。请稍后再试。"}}])
            else:
                base_path = Path(__file__).parent / IMAGE_ROOT_DIR_NAME
                artist_path = base_path / session.artist_name
                artist_path.mkdir(parents=True, exist_ok=True)
                file_name = f"{session.source_prefix}_{session.artwork_id}{session.image_ext}"
                final_path = artist_path / file_name
                with open(final_path, 'wb') as f: f.write(image_data)
                logger.info(f"File successfully saved to: {final_path}")
                await send_group_msg(group_id, [{"type": "text", "data": {"text": "投稿成功！感谢您为鹿图库做的贡献！"}}])
            if user_id in USER_SESSIONS:
                del USER_SESSIONS[user_id]

async def handle_message(data: dict):
    post_type = data.get("post_type")
    message_type = data.get("message_type")
    if not (post_type == "message" and message_type == "group"): return

    raw_message = data.get("raw_message", "").strip()
    group_id = data.get("group_id")
    user_id = data.get("user_id")

    async with _session_lock:
        is_in_session = user_id in USER_SESSIONS

    if is_in_session:
        if raw_message in ["取消投稿", "取消"]:
            await cancel_submission(user_id, group_id)
        else:
            await process_submission_step(user_id, data)
        return

    if raw_message == "鹿图投稿":
        await start_submission(user_id, group_id)
    elif raw_message == "鹿图推荐":
        logger.info(f"Matched command '鹿图推荐' in group {group_id}")
        base_image_path = Path(__file__).parent / IMAGE_ROOT_DIR_NAME
        if not base_image_path.is_dir():
            await send_group_msg(group_id, [{"type": "text", "data": {"text": f"错误：找不到图片目录 '{IMAGE_ROOT_DIR_NAME}'。"}}])
            return
        artist_dirs = [d for d in base_image_path.iterdir() if d.is_dir()]
        if not artist_dirs:
            await send_group_msg(group_id, [{"type": "text", "data": {"text": f"错误：'{IMAGE_ROOT_DIR_NAME}' 内没有画师文件夹。"}}])
            return
        random_artist_path = random.choice(artist_dirs)
        image_files = [f for f in random_artist_path.iterdir() if f.is_file() and f.name.lower().endswith(('png', 'jpg', 'jpeg', 'gif', 'bmp'))]
        if not image_files:
            await send_group_msg(group_id, [{"type": "text", "data": {"text": f"画师 '{random_artist_path.name}' 的文件夹是空的。"}}])
            return
        random_image_path = random.choice(image_files)
        source_text, id_text = "未知来源", "未知"
        file_name_upper = random_image_path.name.upper()
        if file_name_upper.startswith("B_"):
            source_text = "b站动态"
            match = re.search(r"B_(\d+)", file_name_upper)
            if match: id_text = match.group(1)
        elif file_name_upper.startswith("P_"):
            source_text = "Pixiv"
            match = re.search(r"P_(\d+)", file_name_upper)
            if match: id_text = match.group(1)
        elif file_name_upper.startswith("X_"):
            source_text = "X动态"
            match = re.search(r"X_(\d+)", file_name_upper)
            if match: id_text = match.group(1)
        elif file_name_upper.startswith("BV_"):
            source_text = "b站视频"
            id_text = random_image_path.stem.split('_', 1)[1]
        
        text_content = f"为您推荐的图片是：\n画师：{random_artist_path.name}\n来源：{source_text} ID：{id_text}\n"
        image_uri = random_image_path.as_uri()
        response_message = [
            {"type": "at", "data": {"qq": str(user_id)}},
            {"type": "text", "data": {"text": f"\n{text_content}"}},
            {"type": "image", "data": {"file": image_uri}}
        ]
        async def sender_coro():
            await send_group_msg(group_id, response_message)
        async def reminder_coro():
            await asyncio.sleep(SEND_TIMEOUT_SECONDS)
            await send_group_msg(group_id, [{"type": "text", "data": {"text": "图片可能过大，请稍后"}}])
            logger.info(f"发送超时提醒到群聊 {group_id}。")
        sender_task = asyncio.create_task(sender_coro())
        reminder_task = asyncio.create_task(reminder_coro())
        done, pending = await asyncio.wait({sender_task, reminder_task}, return_when=asyncio.FIRST_COMPLETED)
        if sender_task in done:
            reminder_task.cancel()
        else:
            logger.info(f"发送超过 {SEND_TIMEOUT_SECONDS} 秒，已发送提醒，图片仍在后台发送中。")

# --- 5. 主程序循环 ---
async def main():
    logger.info("纯粹模式机器人已启动...")
    logger.info(f"将从 WebSocket ({WEBSOCKET_URI}) 接收事件。")
    logger.info(f"将通过 HTTP API ({ONEBOT_HTTP_API_URL}) 发送消息。")
    while True:
        try:
            logger.info("正在连接到 WebSocket...")
            async with websockets.connect(WEBSOCKET_URI) as websocket:
                logger.info("WebSocket 连接成功！开始监听...")
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        asyncio.create_task(handle_message(data))
                    except json.JSONDecodeError:
                        logger.warning(f"收到非 JSON 格式的消息: {message}")
                    except Exception:
                        logger.exception("处理单个事件时发生未知错误。")
        except (websockets.exceptions.ConnectionClosedError, ConnectionRefusedError) as e:
            logger.error(f"WebSocket 连接失败或中断: {e}. 将在10秒后重试...")
            await asyncio.sleep(10)
        except Exception:
            logger.exception("主循环发生未知严重错误，将在10秒后重试...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已由用户手动停止。")
