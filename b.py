import asyncio
import websockets
import json
import httpx
import time
import os
import sys
from pathlib import Path
from playwright.async_api import async_playwright

# --- 配置区域 ---
# 我已根据您提供的信息为您填好所有配置

# 【主群】接收所有通知 (动态、视频、直播、机器人报错)
PUSH_GROUP_ID = None

# 【副群列表】支持多个副群，请在此列表中填入群号
# 副群仅接收动态和直播通知 (不含机器人报错)
# 例如: [111111111, 222222222]，如果不需要副群，请保持为空列表 []
SECONDARY_GROUP_IDS = [None]

# [已配置] OneBot V11 服务端的 WebSocket 连接地址
ONEBOT_WEBSOCKET_URL = "ws://127.0.0.1:15700/onebot/v11/ws"

# [已配置] 您要关注的 Bilibili UP 主的 UID
TARGET_UID = 316381099

# [已配置] 您要监控的直播间 ID
TARGET_LIVE_ROOM_ID = 15152878

# 【新功能】将您的Cookie文件名放在这个列表中，机器人将按顺序轮换使用
# 请确保这些文件与 b.py 在同一个目录下
COOKIE_FILE_NAMES = ["bilicookie.json", "bili2cookie.json"]

# 【新功能】Cookie 轮换周期（小时）
COOKIE_ROTATION_HOURS = 6

# 轮询检查间隔（秒）
CHECK_INTERVAL_SECONDS = 3

# 其他配置
SCREENSHOT_FILE = "temp_dynamic_screenshot.png"

# --- 全局变量 ---
last_state = {
    "last_dynamic_id": "0", 
    "last_live_status": -1, 
    "last_live_title": "", 
    "last_live_cover_url": ""
}
user_name_cache = f"UID:{TARGET_UID}"
# 【已修改】将Cookie相关状态也放入全局管理
cookie_state = {
    "current_index": 0,
    "last_switch_time": time.time(),
    "playwright_cookies": None,
    "httpx_headers": {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36', 'Referer': 'https://www.bilibili.com/'}
}

# --- Cookie处理函数 ---
def load_and_parse_cookie(file_name):
    """从单个文件加载并解析Cookie"""
    print(f"[*] 正在尝试加载并解析Cookie文件: {file_name}")
    try:
        # 自动获取脚本所在目录
        script_dir = Path(__file__).parent
        file_path = script_dir / file_name
        
        if not file_path.is_file():
            print(f"[!] [严重错误] Cookie文件未找到！路径: {file_path}")
            return None, None
            
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
        if not content:
            print(f"[!] [严重错误] Cookie文件 '{file_name}' 为空！")
            return None, None

        # 解析Playwright格式
        cookies = json.loads(content)
        valid_same_site_values = ["Strict", "Lax", "None"]
        for cookie in cookies:
            if 'sameSite' in cookie and cookie['sameSite'] not in valid_same_site_values:
                cookie['sameSite'] = 'Lax'
        
        # 生成httpx格式
        httpx_cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        
        print(f"[+] Cookie文件 '{file_name}' 加载并解析成功。")
        return cookies, httpx_cookie_str
        
    except Exception as e:
        print(f"[!] [严重错误] 处理Cookie文件 '{file_name}' 时失败: {e}")
        return None, None

# --- 截图核心功能 ---
async def screenshot_dynamic(browser, dynamic_id):
    dynamic_url = f"https://t.bilibili.com/{dynamic_id}"
    context = None
    try:
        context = await browser.new_context(viewport={'width': 800, 'height': 1200}, device_scale_factor=2)
        await context.add_cookies(cookie_state["playwright_cookies"])
        page = await context.new_page()
        await page.goto(dynamic_url, wait_until='networkidle', timeout=30000)
        dynamic_card_selector = ".bili-dyn-item"
        card_element = await page.wait_for_selector(dynamic_card_selector, timeout=15000)
        await page.evaluate("""(selector) => {
            const uselessSelectors = ['.bili-dyn-action', '.bili-dyn-up-list', '.bili-dyn-seme'];
            uselessSelectors.forEach(s => { const elem = document.querySelector(s); if (elem) elem.style.display = 'none'; });
            const targetElem = document.querySelector(selector);
            if(targetElem) { targetElem.style.padding = '15px'; targetElem.style.backgroundColor = '#FFFFFF'; }
        }""", dynamic_card_selector)
        await card_element.screenshot(path=SCREENSHOT_FILE)
        return os.path.abspath(SCREENSHOT_FILE)
    except Exception as e:
        print(f"[!] [严重错误] 截图过程中失败: {e}")
        error_msg = f"【机器人警告】为动态 {dynamic_id} 截图失败。\n错误: {e}"
        await send_group_message(PUSH_GROUP_ID, [{'type': 'text', 'data': {'text': error_msg}}])
        return None
    finally:
        if context: await context.close()

# --- OneBot 及其他辅助函数 ---
async def send_group_message(group_id, message_parts):
    if not group_id: return
    message_str = ""
    for part in message_parts:
        if part['type'] == 'text': message_str += part['data']['text']
        elif part['type'] == 'image':
            file_path = part['data']['file']
            if file_path.startswith("http"): message_str += f"[CQ:image,file={file_path}]"
            elif os.path.exists(file_path): message_str += f"[CQ:image,file=file:///{os.path.abspath(file_path)}]"
    print(f"[+] 准备发送消息到群 {group_id}...")
    payload = {"action": "send_group_msg", "params": {"group_id": group_id, "message": message_str}}
    try:
        async with websockets.connect(ONEBOT_WEBSOCKET_URL, open_timeout=10) as websocket:
            await websocket.send(json.dumps(payload))
            print(f"[+] 消息已成功发送到 WebSocket。")
    except Exception as e:
        print(f"[!] [严重错误] 发送消息到群 {group_id} 失败: {e}.")

async def broadcast_message(message_parts):
    """向主群和所有副群广播“干净”的通知"""
    await send_group_message(PUSH_GROUP_ID, message_parts)
    for group_id in SECONDARY_GROUP_IDS:
        await send_group_message(group_id, message_parts)

# --- 核心检查逻辑 ---

async def check_live_status(httpx_client):
    """检查直播状态，并按分流规则推送到群组。"""
    global last_state, user_name_cache
    print(f"[*] 开始检查直播间状态...")
    live_api_url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={TARGET_LIVE_ROOM_ID}"
    try:
        resp = await httpx_client.get(live_api_url)
        resp.raise_for_status()
        live_data = resp.json()
        if live_data.get('code') != 0: raise Exception(f"API返回错误: {live_data.get('message', '未知')}")
        info = live_data['data']
        user_name_cache = info.get('uname', user_name_cache)
        current_status = info.get('live_status', 0)
        
        if current_status != last_state['last_live_status']:
            message_parts = []
            if current_status == 1 and last_state['last_live_status'] != 1:
                print(f"[+] 检测到 {user_name_cache} 开播了！")
                last_state['last_live_title'] = info.get('title', '')
                last_state['last_live_cover_url'] = info.get('user_cover', '')
                message_parts = [
                    {'type': 'text', 'data': {'text': f"{user_name_cache}开播了"}},
                    {'type': 'image', 'data': {'file': last_state['last_live_cover_url']}}
                ]
            elif current_status != 1 and last_state['last_live_status'] == 1:
                print(f"[+] 检测到 {user_name_cache} 下播了。")
                message_parts = [
                    {'type': 'text', 'data': {'text': f"{user_name_cache}下播了"}},
                    {'type': 'image', 'data': {'file': last_state['last_live_cover_url']}}
                ]
            if message_parts:
                await broadcast_message(message_parts)
            last_state['last_live_status'] = current_status
        else:
            print("[*] 直播状态未变化。")
            
    except (httpx.ConnectError, httpx.ReadTimeout) as e:
        print(f"[!] [网络错误] 获取直播状态时发生可恢复的网络错误，已跳过本次检查: {e.__class__.__name__}")
    except Exception as e:
        error_msg = f"【机器人故障】获取直播状态失败。\n错误: {e.__class__.__name__}: {e}"
        print(f"[!] {error_msg}")
        await send_group_message(PUSH_GROUP_ID, [{'type': 'text', 'data': {'text': error_msg}}])

async def check_dynamics(httpx_client, browser, is_initial_check=False):
    """检查B站动态，按分流规则推送到群组。"""
    global last_state, user_name_cache
    print(f"[*] 开始检查动态...")
    try:
        dynamic_api_url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={TARGET_UID}"
        resp_dynamic = await httpx_client.get(dynamic_api_url)
        resp_dynamic.raise_for_status()
        dynamic_data = resp_dynamic.json()
        if dynamic_data.get('code') != 0 or not dynamic_data.get('data', {}).get('items'):
            raise Exception(f"API返回错误: {dynamic_data.get('message', '未知')}")
        items = dynamic_data['data']['items']
        if not items: return
        first_item = items[0]
        is_pinned = first_item.get('modules', {}).get('module_tag', {}).get('text') == '置顶'
        target_dynamic = items[1] if is_pinned and len(items) > 1 else first_item if not is_pinned else None
        if not target_dynamic:
            if is_pinned and is_initial_check: last_state['last_dynamic_id'] = first_item['id_str']
            return
        current_dynamic_id = target_dynamic['id_str']
        if is_initial_check:
            last_state['last_dynamic_id'] = current_dynamic_id
            print(f"[*] 初始化完成，最新动态ID记录为: {current_dynamic_id}")
            return
        if int(current_dynamic_id) > int(last_state['last_dynamic_id']):
            print(f"[+] 发现新动态！ID: {current_dynamic_id}")
            last_state['last_dynamic_id'] = current_dynamic_id
            user_name_cache = target_dynamic.get('modules', {}).get('module_author', {}).get('name', user_name_cache)
            dyn_type = target_dynamic.get('type')
            message_text = f"{user_name_cache}发布了新视频" if dyn_type == 'DYNAMIC_TYPE_AV' else f"{user_name_cache}发布了新动态"
            screenshot_path = await screenshot_dynamic(browser, current_dynamic_id)
            message_parts = [{'type': 'text', 'data': {'text': message_text}}]
            if screenshot_path:
                message_parts.append({'type': 'image', 'data': {'file': screenshot_path}})
                await broadcast_message(message_parts)
                try: os.remove(screenshot_path)
                except OSError: pass
            else:
                await broadcast_message(message_parts)
        else:
            print("[*] 动态ID未变化。")

    except (httpx.ConnectError, httpx.ReadTimeout) as e:
        print(f"[!] [网络错误] 获取B站动态时发生可恢复的网络错误，已跳过本次检查: {e.__class__.__name__}")
    except Exception as e:
        error_msg = f"【机器人故障】获取B站动态失败。\n错误: {e.__class__.__name__}: {e}"
        print(f"[!] {error_msg}")
        await send_group_message(PUSH_GROUP_ID, [{'type': 'text', 'data': {'text': error_msg}}])

# --- 主程序入口 ---
async def manage_cookie_rotation():
    """【新】检查是否需要轮换Cookie，并执行轮换操作"""
    global cookie_state
    
    # 如果只有一个cookie文件，则无需轮换
    if len(COOKIE_FILE_NAMES) <= 1:
        return
        
    elapsed_seconds = time.time() - cookie_state["last_switch_time"]
    rotation_seconds = COOKIE_ROTATION_HOURS * 3600
    
    # 时间未到，直接返回
    if elapsed_seconds < rotation_seconds:
        return
        
    print("\n" + "*"*10 + f" {COOKIE_ROTATION_HOURS}小时已到，开始轮换Cookie " + "*"*10)
    
    next_index = (cookie_state["current_index"] + 1) % len(COOKIE_FILE_NAMES)
    next_cookie_file = COOKIE_FILE_NAMES[next_index]
    
    # 尝试加载新的cookie
    new_playwright_cookies, new_httpx_cookie_str = load_and_parse_cookie(next_cookie_file)
    
    if new_playwright_cookies and new_httpx_cookie_str:
        # 加载成功，更新全局状态
        cookie_state["current_index"] = next_index
        cookie_state["playwright_cookies"] = new_playwright_cookies
        cookie_state["httpx_headers"]['Cookie'] = new_httpx_cookie_str
        cookie_state["last_switch_time"] = time.time()
        
        success_msg = f"【机器人通知】已自动轮换至下一个Cookie: `{next_cookie_file}`"
        print(f"[+] {success_msg}")
        await send_group_message(PUSH_GROUP_ID, [{'type': 'text', 'data': {'text': success_msg}}])
    else:
        # 加载失败，不切换，继续使用旧cookie
        fail_msg = f"【机器人故障】尝试自动轮换至 `{next_cookie_file}` 失败，请检查该文件是否存在且格式正确。机器人将继续使用当前Cookie。"
        print(f"[!] {fail_msg}")
        await send_group_message(PUSH_GROUP_ID, [{'type': 'text', 'data': {'text': fail_msg}}])
        # 将切换时间重置，避免在每个20秒循环都频繁尝试切换失败的cookie
        cookie_state["last_switch_time"] = time.time()
        
    print("*"*10 + " Cookie轮换流程结束 " + "*"*10 + "\n")


async def main():
    # 初始化加载第一个Cookie
    initial_cookie_file = COOKIE_FILE_NAMES[0]
    p_cookies, h_cookie_str = load_and_parse_cookie(initial_cookie_file)
    if not (p_cookies and h_cookie_str):
        print(f"[!] [致命错误] 初始Cookie '{initial_cookie_file}' 加载失败，程序无法启动。")
        return
    cookie_state["playwright_cookies"] = p_cookies
    cookie_state["httpx_headers"]['Cookie'] = h_cookie_str
    print(f"[*] 初始Cookie '{initial_cookie_file}' 已成功加载并配置。")
    
    async with async_playwright() as p:
        print("[*] 正在启动浏览器内核...")
        browser = await p.chromium.launch()
        print("[+] 浏览器内核启动成功。")
        print(f"\n[*] 机器人开始工作，将每隔 {CHECK_INTERVAL_SECONDS} 秒检查一次。")
        
        # 首次启动不推送，只记录初始状态
        print("\n" + "="*50 + f"\n[*] {time.strftime('%Y-%m-%d %H:%M:%S')} - 开始首次状态初始化")
        async with httpx.AsyncClient(headers=cookie_state["httpx_headers"], timeout=15.0) as httpx_client:
            await check_live_status(httpx_client)
            await check_dynamics(httpx_client, browser, is_initial_check=True)
        print("="*50)
        
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            
            # 在每次大循环开始时，检查是否需要轮换Cookie
            await manage_cookie_rotation()
            
            print("\n" + "="*50 + f"\n[*] {time.strftime('%Y-%m-%d %H:%M:%S')} - 开始新一轮检查")
            try:
                # 使用当前激活的httpx_headers
                async with httpx.AsyncClient(headers=cookie_state["httpx_headers"], timeout=15.0) as httpx_client:
                    await check_live_status(httpx_client)
                    await check_dynamics(httpx_client, browser)
            
            except httpx.PoolTimeout as e:
                error_msg = '【机器人严重故障】网络连接池超时，我将自动尝试重置连接并继续。如果此问题频繁出现，请检查服务器网络环境。'
                print(f"[!] [严重网络错误] {error_msg} 错误: {e}")
                await send_group_message(PUSH_GROUP_ID, [{'type': 'text', 'data': {'text': error_msg}}])
            
            except Exception as e:
                error_msg = f"【机器人严重故障】主循环发生意外错误，机器人将继续运行。\n错误: {e.__class__.__name__}: {e}"
                print(f"[!] {error_msg}")
                await send_group_message(PUSH_GROUP_ID, [{'type': 'text', 'data': {'text': error_msg}}])
            finally:
                print("="*50)
                
        await browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] 机器人已手动停止。")
