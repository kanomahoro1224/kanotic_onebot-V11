import asyncio
import json
import os
import random
import re
import websockets
import requests
import logging
from collections import defaultdict

# --- 基本配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
ONEBOT_WS_URL = "ws://127.0.0.1:15000"
ONEBOT_HTTP_API_URL = "http://127.0.0.1:15100"
LYRIC_TRIGGER_COMMAND = "看词猜鹿歌"
AUDIO_TRIGGER_COMMAND = "听音猜鹿歌"
LRC_FOLDER = "lrc"
SLK_FOLDER = "slk"
ANSWER_TIME_SECONDS = 120 

# 全局变量
current_quiz = {}
recursion_guard = 0
MAX_RECURSION_DEPTH = 10 

def send_group_message(group_id, message):
    """通过HTTP API向指定群聊发送消息"""
    api_url = f"{ONEBOT_HTTP_API_URL}/send_group_msg"
    payload = {"group_id": group_id, "message": message}
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(api_url, data=json.dumps(payload), headers=headers, timeout=10)
        if response.json().get("status") == "ok":
            logging.info(f"成功向群 {group_id} 发送消息。")
        else:
            logging.error(f"向群 {group_id} 发送消息失败: {response.json().get('wording', '未知错误')}")
    except requests.exceptions.RequestException as e:
        logging.error(f"向群 {group_id} 发送消息时发生网络错误: {e}")

def parse_lrc(lines):
    lyrics_by_timestamp = defaultdict(list)
    time_pattern = re.compile(r'(\[\d{2}:\d{2}\.\d{2,3}\])')
    for line in lines:
        match = time_pattern.match(line)
        if match:
            timestamp, lyric_text = match.group(1), line[match.end():].strip()
            if lyric_text: lyrics_by_timestamp[timestamp].append(lyric_text)
    if not lyrics_by_timestamp: return 'unknown', []
    pair_count = sum(1 for texts in lyrics_by_timestamp.values() if len(texts) >= 2)
    is_bilingual = pair_count > sum(1 for texts in lyrics_by_timestamp.values() if len(texts) == 1)
    if is_bilingual:
        return 'bilingual', [tuple(lyrics_by_timestamp[ts][:2]) for ts in sorted(lyrics_by_timestamp.keys()) if len(lyrics_by_timestamp[ts]) >= 2]
    else:
        return 'monolingual', [lyrics_by_timestamp[ts][0] for ts in sorted(lyrics_by_timestamp.keys()) if lyrics_by_timestamp[ts]]

def set_quiz_state(group_id, starter_id, correct_song, correct_letter):
    """统一设置游戏状态和计时器"""
    timer_task = asyncio.create_task(announce_answer_later(ANSWER_TIME_SECONDS, group_id))
    # 更新或创建游戏状态
    current_quiz[group_id] = {
        "correct_song": correct_song, 
        "correct_letter": correct_letter, 
        "starter_id": starter_id, 
        "active": True, 
        "timer_task": timer_task
    }

def prepare_lyric_quiz(group_id, starter_id):
    global recursion_guard
    if recursion_guard >= MAX_RECURSION_DEPTH:
        send_group_message(group_id, "题库好像出了点问题，请稍后再试吧。"); recursion_guard = 0; del current_quiz[group_id]; return
    lrc_dir = os.path.join(os.path.dirname(__file__), LRC_FOLDER)
    if not os.path.isdir(lrc_dir): send_group_message(group_id, f"错误：找不到 '{LRC_FOLDER}' 文件夹。"); del current_quiz[group_id]; return
    song_list = [f[:-4] for f in os.listdir(lrc_dir) if f.endswith('.lrc')]
    if len(song_list) < 4: send_group_message(group_id, "错误：歌词库歌曲不足4首。"); del current_quiz[group_id]; return
    correct_song = random.choice(song_list)
    try:
        with open(os.path.join(lrc_dir, f"{correct_song}.lrc"), 'r', encoding='utf-8') as f: lines = f.readlines()
    except Exception:
        recursion_guard += 1; prepare_lyric_quiz(group_id, starter_id); return
    song_type, parsed_lyrics = parse_lrc(lines)
    lyric_snippet = ""
    if song_type == 'bilingual':
        if len(parsed_lyrics) < 6: recursion_guard += 1; prepare_lyric_quiz(group_id, starter_id); return
        start_index = random.randint(2, len(parsed_lyrics) - 4)
        lyric_snippet = "\n".join([line for pair in parsed_lyrics[start_index:start_index + 2] for line in pair])
    elif song_type == 'monolingual':
        if len(parsed_lyrics) < 12: recursion_guard += 1; prepare_lyric_quiz(group_id, starter_id); return
        start_index = random.randint(4, len(parsed_lyrics) - 8)
        lyric_snippet = "\n".join(parsed_lyrics[start_index:start_index + 4])
    else: 
        recursion_guard += 1; prepare_lyric_quiz(group_id, starter_id); return

    options = [correct_song, *random.sample([s for s in song_list if s != correct_song], 3)]; random.shuffle(options)
    full_message = f"🎶 看词猜鹿歌 🎶\n\n{lyric_snippet}\n\n请问这是哪首歌？\n"
    option_letters, correct_answer_letter = ['A', 'B', 'C', 'D'], ''
    for i, option in enumerate(options):
        full_message += f"{option_letters[i]}. {option}\n"
        if option == correct_song: correct_answer_letter = option_letters[i]
    full_message += f"\n请在2分钟内直接发送选项字母(A/B/C/D)或歌名作答哦~"
    send_group_message(group_id, full_message)
    set_quiz_state(group_id, starter_id, correct_song, correct_answer_letter)
    recursion_guard = 0

def prepare_audio_quiz(group_id, starter_id):
    slk_dir = os.path.join(os.path.dirname(__file__), SLK_FOLDER)
    if not os.path.isdir(slk_dir): send_group_message(group_id, f"错误：找不到 '{SLK_FOLDER}' 文件夹。"); del current_quiz[group_id]; return
    slk_files = [f for f in os.listdir(slk_dir) if f.endswith('.slk')]
    if not slk_files: send_group_message(group_id, f"错误：'{SLK_FOLDER}' 文件夹是空的。"); del current_quiz[group_id]; return
    unique_songs = list(set([f.split('_p')[0] for f in slk_files]))
    if len(unique_songs) < 4: send_group_message(group_id, "错误：音频库歌曲不足4首。"); del current_quiz[group_id]; return
    correct_song = random.choice(unique_songs)
    song_parts = [f for f in slk_files if f.startswith(correct_song + '_p')]
    chosen_part_filename = random.choice(song_parts)
    absolute_path = os.path.abspath(os.path.join(slk_dir, chosen_part_filename))
    audio_cq_code = f"[CQ:record,file=file:///{absolute_path}]"
    send_group_message(group_id, audio_cq_code)
    
    async def send_options_later():
        await asyncio.sleep(1) 
        options = [correct_song, *random.sample([s for s in unique_songs if s != correct_song], 3)]; random.shuffle(options)
        quiz_message = f"🎶 听音猜鹿歌 🎶\n\n请问这是哪首歌？\n"
        option_letters, correct_answer_letter = ['A', 'B', 'C', 'D'], ''
        for i, option in enumerate(options):
            quiz_message += f"{option_letters[i]}. {option}\n"
            if option == correct_song: correct_answer_letter = option_letters[i]
        quiz_message += f"\n请在2分钟内直接发送选项字母(A/B/C/D)或歌名作答哦~"
        send_group_message(group_id, quiz_message)
        set_quiz_state(group_id, starter_id, correct_song, correct_answer_letter)
    asyncio.create_task(send_options_later())

async def announce_answer_later(delay, group_id):
    try:
        await asyncio.sleep(delay)
        if current_quiz.get(group_id, {}).get("active"):
            quiz_data = current_quiz[group_id]
            starter_id, correct_song, correct_letter = quiz_data.get("starter_id"), quiz_data.get("correct_song"), quiz_data.get("correct_letter")
            at_string = f"[CQ:at,qq={starter_id}] " if starter_id else ""
            message = f"{at_string}时间到！正确答案是 {correct_letter}. {correct_song}！"
            send_group_message(group_id, message)
            if group_id in current_quiz: del current_quiz[group_id]
    except asyncio.CancelledError:
        logging.info(f"群 {group_id} 的计时器被成功取消。")

def handle_answer(group_id, message_text, user_id):
    if not current_quiz.get(group_id, {}).get("active"): return
    quiz_data = current_quiz[group_id]
    # 增加一个检查，如果题目数据还没填充完整（主要针对听音猜歌的延迟），则忽略回答
    if "correct_song" not in quiz_data: return
    answer = message_text.strip().upper()
    correct_song, correct_letter = quiz_data.get("correct_song"), quiz_data.get("correct_letter")
    if answer == correct_letter or answer == correct_song.upper():
        if (timer_task := quiz_data.get("timer_task")): timer_task.cancel()
        message = f"[CQ:at,qq={user_id}] 恭喜你，答对啦！🎉\n正确答案就是 {correct_song}！"
        send_group_message(group_id, message)
        if group_id in current_quiz: del current_quiz[group_id]
    elif answer in ['A', 'B', 'C', 'D']:
        send_group_message(group_id, f"[CQ:at,qq={user_id}] 回答错误，再想想看哦~")

async def handle_websocket_connection():
    """处理WebSocket连接和分发指令（防并发版）"""
    while True:
        try:
            async with websockets.connect(ONEBOT_WS_URL) as websocket:
                logging.info(f"成功连接到 OneBot WebSocket 服务器: {ONEBOT_WS_URL}")
                while True:
                    try:
                        data = json.loads(await websocket.recv())
                        if data.get("post_type") == "message" and data.get("message_type") == "group":
                            group_id, user_id, raw_message = data.get("group_id"), data.get("user_id"), data.get("raw_message", "").strip()
                            
                            is_game_active = current_quiz.get(group_id, {}).get("active")
                            
                            # 统一处理游戏开始指令
                            if raw_message in [LYRIC_TRIGGER_COMMAND, AUDIO_TRIGGER_COMMAND]:
                                if is_game_active:
                                    send_group_message(group_id, "上一题还没结束哦，请先回答吧~")
                                else:
                                    # === 关键改动：立即锁定游戏状态 ===
                                    current_quiz[group_id] = {"active": True, "starter_id": user_id} 
                                    logging.info(f"群 {group_id} 已锁定，准备开始游戏...")
                                    
                                    if raw_message == LYRIC_TRIGGER_COMMAND:
                                        prepare_lyric_quiz(group_id, user_id)
                                    elif raw_message == AUDIO_TRIGGER_COMMAND:
                                        prepare_audio_quiz(group_id, user_id)
                            
                            # 处理回答
                            elif is_game_active:
                                handle_answer(group_id, raw_message, user_id)
                    except websockets.ConnectionClosed:
                        logging.warning("WebSocket 连接已断开。"); break
        except Exception as e:
            logging.error(f"无法连接或处理 WebSocket: {e}。将在 10 秒后重试...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    logging.info("终极防并发猜歌机器人启动中...")
    for folder in [LRC_FOLDER, SLK_FOLDER]:
        if not os.path.isdir(folder):
            os.makedirs(folder); logging.info(f"已自动创建 '{folder}' 文件夹。")
    try:
        asyncio.run(handle_websocket_connection())
    except KeyboardInterrupt:
        logging.info("机器人已手动停止。")
