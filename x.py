import asyncio
import json
import time
import os
import aiohttp
from playwright.async_api import async_playwright, Page, BrowserContext, Error as PlaywrightError, TimeoutError
import websockets
import base64
import traceback
from functools import partial
import subprocess

# --- 配置文件路径 ---
COOKIE_FILE_PATH = r"C:\Users\Administrator\Desktop\kanotic\Xcookie.json"
TRANSLATION_ICON_FILE_PATH = r"C:\Users\Administrator\Desktop\kanotic\image\sakana.png"
EDGE_RESTART_BAT_PATH = r"C:\Users\Administrator\Desktop\kanotic\start_edge.bat"

# --- 浏览器连接配置 ---
DEBUGGING_PORT = 9222
TARGET_URL_FRAGMENT = "x.com"

# --- OneBot V11 服务端配置 ---
WEBSOCKET_URI = "ws://127.0.0.1:15500/onebot/v11/ws"

# --- 代理配置 ---
PROXY_URL = "http://127.0.0.1:7897"

# --- DeepSeek API 配置 ---
DEEPSEEK_API_KEY = "None"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
ENABLE_TRANSLATION = True

# --- 推送目标配置 ---
PRIMARY_GROUP_ID = None
SECONDARY_GROUP_IDS = [None]
TARGET_USERNAMES = ["kano_2525", "_Kanotic"]
NICKNAME_TO_USERNAME_MAP = {"鹿乃/kano": "kano_2525", "鹿乃まほろ/MKLNtic🍓🕊": "_Kanotic"}

# --- 运行策略配置 ---
# 【修改】现在这个时间是 Edge 浏览器的重启周期
EDGE_RESTART_INTERVAL_HOURS = 1.0

# --- 发送与截图配置 ---
IMAGE_CACHE_DIR = "image_cache"
MAX_SEND_RETRIES = 3
SEND_RETRY_DELAY = 5

# --- 全局状态变量 ---
PROCESSING_URLS = set()

def load_cookies_from_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_cookies = json.load(f)
        cleaned_cookies = []
        for c in raw_cookies:
            clean_c = c.copy()
            if 'sameSite' in clean_c:
                val = clean_c['sameSite']
                if val is None: del clean_c['sameSite']
                elif val.lower() == 'no_restriction': clean_c['sameSite'] = 'None'
                elif val.lower() in ['lax', 'strict']: clean_c['sameSite'] = val.capitalize()
            cleaned_cookies.append(clean_c)
        return cleaned_cookies
    except Exception as e:
        print(f"致命错误：加载Cookie时发生未知错误: {e}")
        return None

def image_file_to_base64(file_path):
    try:
        with open(file_path, "rb") as image_file: 
            return f"data:image/png;base64,{base64.b64encode(image_file.read()).decode('utf-8')}"
    except Exception as e:
        print(f"警告：无法加载或转换图标文件: {file_path}, 错误: {e}")
        return None

async def send_one_message(group_id, message):
    if not group_id: return True
    for attempt in range(1, MAX_SEND_RETRIES + 1):
        try:
            async with websockets.connect(WEBSOCKET_URI, open_timeout=10) as websocket:
                await websocket.send(json.dumps({"action": "send_group_msg", "params": {"group_id": group_id, "message": message}}))
                return True
        except Exception:
            if attempt < MAX_SEND_RETRIES: await asyncio.sleep(SEND_RETRY_DELAY)
    return False

async def translate_text_with_deepseek(session, text_to_translate):
    if not text_to_translate or not text_to_translate.strip() or not DEEPSEEK_API_KEY: return None
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "system", "content": "You are a helpful translation assistant."}, {"role": "user", "content": f"Please translate the following content into Simplified Chinese, keeping the original line breaks:\n\n{text_to_translate}"}]}
    try:
        async with session.post(DEEPSEEK_API_URL, json=payload, headers=headers, proxy=PROXY_URL) as response:
            if response.status == 200: return (await response.json())['choices'][0]['message']['content']
            else: return None
    except Exception: return None

async def process_tweet_push(worker_context: BrowserContext, aiohttp_session, icon_data: str, tweet_url: str, username: str, is_init_check: bool = False):
    global PROCESSING_URLS
    print(f"--- [ @{username} ] 使用无头浏览器处理动态: {tweet_url} ---")
    page = None
    try:
        page = await worker_context.new_page()
        await page.goto(tweet_url, wait_until='domcontentloaded', timeout=20000)
        view_button_locator = page.get_by_role("button", name="View").or_(page.get_by_role("button", name="查看"))
        if await view_button_locator.count() > 0: await view_button_locator.first.click(); await page.wait_for_timeout(1500)
        try:
            await page.wait_for_selector('article', timeout=10000)
        except TimeoutError:
            await page.close(); return
        article = page.locator('article').first
        content_locator = article.locator('div[data-testid="tweetText"]').first
        tweet_text = await content_locator.inner_text() if await content_locator.count() > 0 else ""
        if ENABLE_TRANSLATION and tweet_text:
            translated_text = await translate_text_with_deepseek(aiohttp_session, tweet_text)
            if translated_text and icon_data:
                js_code = """
                (article, args) => {
                    const [translatedText, iconBase64] = args;
                    const existingBox = article.querySelector('#custom-translation-box');
                    if (existingBox) existingBox.remove();
                    const tweetTextElement = article.querySelector('div[data-testid="tweetText"]');
                    if (tweetTextElement) {
                        const translationCard = document.createElement('div');
                        translationCard.id = 'custom-translation-box';
                        translationCard.style.backgroundColor = '#f7f9f9';
                        translationCard.style.border = '1px solid #cfd9de';
                        translationCard.style.borderRadius = '16px';
                        translationCard.style.padding = '12px';
                        translationCard.style.marginTop = '12px';
                        const headerDiv = document.createElement('div');
                        headerDiv.style.display = 'flex';
                        headerDiv.style.alignItems = 'center';
                        headerDiv.style.marginBottom = '8px';
                        headerDiv.innerHTML = `<img src="${iconBase64}" style="width: 16px; height: 16px; margin-right: 8px;"><span style="font-size: 14px; color: #536471;">由DeepSeek翻译</span>`;
                        const contentP = document.createElement('p');
                        contentP.style.margin = '0';
                        contentP.style.fontSize = '15px';
                        contentP.style.color = '#0f1419';
                        contentP.style.whiteSpace = 'pre-wrap';
                        contentP.style.lineHeight = '1.5';
                        contentP.textContent = translatedText;
                        translationCard.appendChild(headerDiv);
                        translationCard.appendChild(contentP);
                        tweetTextElement.insertAdjacentElement('afterend', translationCard);
                    }
                }
                """
                await article.evaluate(js_code, [translated_text, icon_data])
        screenshot_path = os.path.join(IMAGE_CACHE_DIR, f"tweet_{username}_{int(time.time())}.png")
        try:
            element_height = await article.evaluate("element => element.scrollHeight")
            current_viewport = page.viewport_size
            if element_height and current_viewport and element_height > current_viewport['height']:
                new_height = element_height + 100
                await page.set_viewport_size({"width": current_viewport['width'], "height": new_height})
                await page.wait_for_timeout(2000)
        except PlaywrightError as e:
            print(f"警告：调整视窗大小时出错: {e}。将尝试使用原始尺寸截图。")
            pass
        await article.screenshot(path=screenshot_path)
        await article.evaluate("(article) => { const box = article.querySelector('#custom-translation-box'); if (box) box.remove(); }")
        base_message = f"[CQ:image,file=file:///{os.path.abspath(screenshot_path)}]\n链接: {tweet_url}"
        author_link_locator = article.locator('div[data-testid="User-Name"] a[href^="/"]').first
        actual_author = username
        if await author_link_locator.count() > 0:
            actual_author = (await author_link_locator.get_attribute('href')).lstrip('/')
        if is_init_check:
            message_to_send = f"【初始化自检】已成功捕获 @{username} 的最新动态：\n" + base_message
            await send_one_message(PRIMARY_GROUP_ID, message_to_send)
        else:
            message_prefix = f"@{username} 转推了 @{actual_author} 的动态：\n" if actual_author != username else f"@{username} 发布了新动态：\n"
            message_to_send = message_prefix + base_message
            await send_one_message(PRIMARY_GROUP_ID, message_to_send)
            if SECONDARY_GROUP_IDS: await asyncio.gather(*(send_one_message(g, message_to_send) for g in SECONDARY_GROUP_IDS))
    except Exception:
        error_details = traceback.format_exc()
        error_message = f"【机器人处理推送时发生错误】\n用户: @{username}\nURL: {tweet_url}\n------\n{error_details}"
        await send_one_message(PRIMARY_GROUP_ID, error_message)
    finally:
        if page: await page.close()
        if not is_init_check:
            PROCESSING_URLS.discard(tweet_url)

async def perform_initialization_check(worker_context: BrowserContext, aiohttp_session, icon_data: str):
    print("\n" + "="*50 + "\n🚦 开始执行初始化自检...")
    if not TARGET_USERNAMES:
        print("ℹ️ 未配置任何目标用户，跳过初始化自检。")
        return
    check_username = TARGET_USERNAMES[0]
    home_url = f"https://x.com/{check_username}"
    page = None
    try:
        page = await worker_context.new_page()
        goto_success = False
        for attempt in range(3):
            try:
                await page.goto(home_url, wait_until='domcontentloaded', timeout=20000)
                goto_success = True
                break
            except PlaywrightError as e:
                print(f"初始化自检：访问页面失败 (尝试 {attempt + 1}/3): {e}")
                if attempt < 2: await asyncio.sleep(3)
        if not goto_success: raise Exception("初始化自检失败：多次尝试访问页面后仍然失败。")
        await page.wait_for_selector('article', timeout=10000)
        articles = await page.locator('article').all()
        if not articles: raise Exception("自检失败：页面上未找到任何动态。")
        latest_article = None
        for article in articles:
            is_pinned = await article.locator('div[data-testid="socialContext"]:has-text("置顶"), div[data-testid="socialContext"]:has-text("Pinned")').count() > 0
            if not is_pinned:
                latest_article = article
                break
        if not latest_article: raise Exception("自检失败：未找到任何非置顶动态。")
        link_locator = latest_article.locator('a[href*="/status/"]').first
        tweet_url_path = await link_locator.get_attribute('href')
        tweet_url = f"https://x.com{tweet_url_path}"
        await page.close()
        page = None
        await process_tweet_push(worker_context, aiohttp_session, icon_data, tweet_url, check_username, is_init_check=True)
        print("✅ 初始化自检成功！")
    except Exception as e:
        error_details = traceback.format_exc()
        error_message = f"【机器人初始化自检失败】\n错误详情: {e}\n------\n{error_details}"
        await send_one_message(PRIMARY_GROUP_ID, error_message)
    finally:
        if page: await page.close()

def on_push_received(payload_str: str, worker_context: BrowserContext, aiohttp_session, icon_data: str):
    print("\n" + "=" * 50)
    print("🎉 捕获到一条来自 X.com 的推送通知！")
    print("--- 捕获到的原始推送数据 (字符串) ---")
    print(payload_str)
    try:
        data = json.loads(payload_str)
        print("--- 解析后的 JSON 数据 (格式化) ---")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("---------------------------------")
    except Exception:
        print("--- 原始数据无法解析为 JSON ---")
    try:
        data = json.loads(payload_str)
        uri = data.get("data", {}).get("uri")
        if not uri: return
        username_from_push = None
        push_type = data.get("data", {}).get("type")
        if push_type == "retweet":
            title = data.get("data", {}).get("title", "")
            parts = title.split(" ")
            if len(parts) > 0:
                nickname = parts[0]
                username_from_push = NICKNAME_TO_USERNAME_MAP.get(nickname)
        else:
            username_from_push = uri.split('/')[1]
        if username_from_push and username_from_push in TARGET_USERNAMES:
            tweet_url = f"https://x.com{uri}"
            if tweet_url in PROCESSING_URLS:
                print(f"🔗 任务已在处理中，忽略重复推送: {tweet_url}")
                return
            PROCESSING_URLS.add(tweet_url)
            print(f"✅ 推送来自目标用户 @{username_from_push}，创建后台截图任务并锁定 URL。")
            asyncio.create_task(process_tweet_push(worker_context, aiohttp_session, icon_data, tweet_url, username_from_push))
        else:
            print(f"ℹ️ 推送来自非目标用户或无法解析用户 ({username_from_push})，已忽略。")
    except Exception as e:
        print(f"处理推送逻辑时出错: {e}")

async def inject_listeners(context: BrowserContext):
    try:
        page = next((p for p in context.pages if TARGET_URL_FRAGMENT in p.url), None)
        if not page:
            print(f"❌ 错误: 找不到 URL 包含 '{TARGET_URL_FRAGMENT}' 的页面。")
            return False
        await page.add_init_script("""navigator.serviceWorker.addEventListener('message', event => { if (event.data && event.data.type === 'PUSH_PAYLOAD') { window.capturePushInPython(event.data.payload); } });""")
        for sw in context.service_workers:
            if TARGET_URL_FRAGMENT in sw.url:
                print(f"  -> 正在为已存在的 Service Worker ({sw.url}) 注入监听器...")
                await sw.evaluate("""self.addEventListener('push', event => { const payload = event.data ? event.data.text() : '(无)'; self.clients.matchAll().then(clients => { clients.forEach(client => { client.postMessage({ type: 'PUSH_PAYLOAD', payload: payload }); }); }); });""")
        await page.reload()
        print("✅ 成功注入/刷新页面和 Service Worker 的监听器！")
        return True
    except Exception as e:
        print(f"❌ 注入监听器时发生错误: {e}")
        return False

# 【核心修复】将 ServiceWorker 类型提示改为字符串 "ServiceWorker"，以兼容旧版本
async def on_service_worker_updated(worker: "ServiceWorker"):
    try:
        if TARGET_URL_FRAGMENT in worker.url:
            print("\n" + "*"*50)
            print(f"🔥 检测到 Service Worker 更新或激活！ (URL: {worker.url})")
            print("⚡ 正在立即为新版本注入推送监听器...")
            await worker.evaluate("""self.addEventListener('push', event => { const payload = event.data ? event.data.text() : '(无)'; self.clients.matchAll().then(clients => { clients.forEach(client => { client.postMessage({ type: 'PUSH_PAYLOAD', payload: payload }); }); }); });""")
            print("✅ 热重载注入成功！监听不会中断。")
            print("*"*50 + "\n")
    except Exception as e:
        print(f"❌ 在热重载 Service Worker 时发生错误: {e}")

async def main():
    if not os.path.exists(IMAGE_CACHE_DIR): os.makedirs(IMAGE_CACHE_DIR)
    cookies = load_cookies_from_file(COOKIE_FILE_PATH)
    if not cookies: return
    icon_data = image_file_to_base64(TRANSLATION_ICON_FILE_PATH)
    if not icon_data:
        print("警告：无法加载图标，翻译框将不显示图标。")

    async with async_playwright() as p, aiohttp.ClientSession() as aiohttp_session:
        worker_browser = None
        try:
            print("🚀 正在启动后台无头浏览器 (工作浏览器)...")
            worker_browser = await p.chromium.launch(headless=True, proxy={"server": PROXY_URL} if PROXY_URL else None)
            worker_context = await worker_browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
            if cookies: await worker_context.add_cookies(cookies)
            print("✅ 无头浏览器启动并配置完成。")
            
            await perform_initialization_check(worker_context, aiohttp_session, icon_data)
            
            while True:
                listener_browser = None
                try:
                    print("\n" + "="*50)
                    print(f"🚀 正在通过 '{EDGE_RESTART_BAT_PATH}' 启动 Edge 监听浏览器...")
                    subprocess.Popen([EDGE_RESTART_BAT_PATH])
                    await asyncio.sleep(5)
                    print("🔄 正在尝试连接到 Edge 的调试端口...")
                    for attempt in range(10):
                        try:
                            listener_browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUGGING_PORT}")
                            print("✅ 成功连接到监听浏览器！")
                            break
                        except PlaywrightError:
                            if attempt == 9: raise
                            await asyncio.sleep(3)
                    
                    listener_context = listener_browser.contexts[0]
                    
                    push_callback = partial(on_push_received, worker_context=worker_context, aiohttp_session=aiohttp_session, icon_data=icon_data)
                    await listener_context.expose_function("capturePushInPython", push_callback)

                    listener_context.on("serviceworker", on_service_worker_updated)
                    print("✅ 已绑定 Service Worker 自动更新监听器。")
                    
                    if not await inject_listeners(listener_context):
                        raise Exception("注入监听器失败，将重启流程。")
                    
                    print(f"\n🟢 监听器已激活。将在 {EDGE_RESTART_INTERVAL_HOURS} 小时后重启 Edge。")
                    await asyncio.sleep(EDGE_RESTART_INTERVAL_HOURS * 3600)

                except Exception as e:
                    print(f"❌ 主循环发生错误: {e}")
                    traceback.print_exc()
                finally:
                    print("🛑 正在关闭当前的 Edge 监听浏览器...")
                    if listener_browser and listener_browser.is_connected():
                        listener_context.remove_listener("serviceworker", on_service_worker_updated)
                        await listener_browser.close()
                    os.system("taskkill /F /IM msedge.exe /T > nul 2>&1")
                    print("✅ Edge 已关闭。将在短暂延时后开始新的循环。")
                    await asyncio.sleep(5)

        except Exception as e:
            print(f"\n❌ 脚本启动或运行时发生致命错误: {e}")
            traceback.print_exc()
        finally:
            print("\n👋 正在关闭所有资源...")
            if worker_browser:
                await worker_browser.close()
            os.system("taskkill /F /IM msedge.exe /T > nul 2>&1")
            print("👋 脚本已退出。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
