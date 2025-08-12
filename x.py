import asyncio
import json
import time
import os
import aiohttp
from playwright.async_api import async_playwright, Error as PlaywrightError
import websockets
import base64
import traceback

# --- 配置文件路径 ---
COOKIE_FILE_PATH = r"C:\Users\Administrator\Desktop\kanotic\Xcookie.json"
TRANSLATION_ICON_FILE_PATH = r"C:\Users\Administrator\Desktop\kanotic\image\sakana.png"

# --- OneBot V11 服务端配置 ---
WEBSOCKET_URI = "ws://127.0.0.1:15500/onebot/v11/ws"
# --- 代理配置 ---
PROXY_URL = "http://127.0.0.1:7897" 

# --- DeepSeek API 配置 ---
DEEPSEEK_API_KEY = "sk-e16e58d5d1a840ea8b8545575c476f7c" 
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
ENABLE_TRANSLATION = True

# --- 推送目标配置 ---
PRIMARY_GROUP_ID = 977105881
SECONDARY_GROUP_ID = 908732510

# 【核心修改】将单个用户名改为用户列表，您可以按需添加更多
TARGET_USERNAMES = [
    "kano_2525",
    "_Kanotic"
]

# --- 监控与发送配置 ---
CHECK_INTERVAL_SECONDS = 15
IMAGE_CACHE_DIR = "image_cache"
# X_HOME_URL 将在循环中动态生成
MAX_SEND_RETRIES = 3
SEND_RETRY_DELAY = 5

def load_cookies_from_file(file_path):
    # (此函数保持不变)
    try:
        print(f"正在从 '{file_path}' 加载Cookie...")
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_cookies = json.load(f)
        cleaned_cookies = []
        for cookie_dict in raw_cookies:
            clean_cookie = cookie_dict.copy()
            if 'sameSite' in clean_cookie:
                samesite_value = clean_cookie['sameSite']
                if samesite_value is None: del clean_cookie['sameSite']
                elif samesite_value == 'no_restriction': clean_cookie['sameSite'] = 'None'
                elif isinstance(samesite_value, str) and samesite_value.lower() == 'lax': clean_cookie['sameSite'] = 'Lax'
                elif isinstance(samesite_value, str) and samesite_value.lower() == 'strict': clean_cookie['sameSite'] = 'Strict'
            cleaned_cookies.append(clean_cookie)
        print("Cookie加载并清洗成功！")
        return cleaned_cookies
    except FileNotFoundError:
        print(f"致命错误：找不到Cookie文件: {file_path}")
        return None
    except json.JSONDecodeError:
        print(f"致命错误：Cookie文件 '{file_path}' 格式不正确。")
        return None
    except Exception as e:
        print(f"致命错误：加载Cookie时发生未知错误: {e}")
        return None

def image_file_to_base64(file_path):
    # (此函数保持不变)
    try:
        with open(file_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        mime_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
        return f"data:{mime_type};base64,{encoded_string}"
    except FileNotFoundError:
        print(f"错误：找不到指定的图标文件: {file_path}")
        return None
    except Exception as e:
        print(f"错误：读取或编码图标文件时发生错误: {e}")
        return None

async def send_one_message(group_id, message):
    # (此函数保持不变)
    if not group_id:
        return True
    for attempt in range(1, MAX_SEND_RETRIES + 1):
        try:
            print(f"准备向群 {group_id} 发送消息 (尝试: {attempt}/{MAX_SEND_RETRIES})...")
            async with websockets.connect(WEBSOCKET_URI, open_timeout=10) as websocket:
                payload = {"action": "send_group_msg", "params": {"group_id": group_id, "message": message}}
                await websocket.send(json.dumps(payload))
                print(f"消息已成功提交到群 {group_id}。")
                return True
        except Exception as e:
            print(f"向群 {group_id} 发送消息失败 (尝试 {attempt}/{MAX_SEND_RETRIES})：{e}")
            if attempt < MAX_SEND_RETRIES:
                print(f"将在 {SEND_RETRY_DELAY} 秒后重试...")
                await asyncio.sleep(SEND_RETRY_DELAY)
    print(f"所有 {MAX_SEND_RETRIES} 次发送尝试均失败，放弃向群 {group_id} 推送。")
    return False

async def get_tweet_info_from_article(article):
    # (此函数保持不变)
    link_element = await article.query_selector('a[href*="/status/"]')
    if not link_element: return None, None
    link = await link_element.get_attribute('href')
    tweet_id = link.split('/')[-1]
    content_element = await article.query_selector('div[data-testid="tweetText"]')
    tweet_text = await content_element.inner_text() if content_element else ""
    return tweet_id, tweet_text

async def translate_text_with_deepseek(session, text_to_translate):
    # (此函数保持不变)
    if not text_to_translate or not text_to_translate.strip(): return None
    if not DEEPSEEK_API_KEY or "xxxxxxxx" in DEEPSEEK_API_KEY:
        print("未配置有效的DeepSeek API Key，跳过翻译。")
        return None
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "system", "content": "You are a helpful translation assistant."}, {"role": "user", "content": f"Please translate the following content into Simplified Chinese, keeping the original line breaks:\n\n{text_to_translate}"}]}
    try:
        async with session.post(DEEPSEEK_API_URL, json=payload, headers=headers, proxy=PROXY_URL) as response:
            if response.status == 200:
                result = await response.json()
                return result['choices'][0]['message']['content']
            else:
                print(f"DeepSeek API 请求失败: {response.status}")
                return None
    except Exception as e:
        print(f"调用 DeepSeek API 时发生网络错误: {e}")
        return None

# 【核心修改】函数签名增加 username 参数
async def find_and_prepare_messages(page, aiohttp_session, icon_b64_data, username, home_url, last_sent_id):
    await page.goto(home_url, wait_until='domcontentloaded', timeout=15000)
    await page.wait_for_selector('article')
    articles = await page.query_selector_all('article')
    if not articles: return None, last_sent_id

    for article in articles:
        is_pinned_element = await article.query_selector('div[data-testid="socialContext"]:has-text("置顶"), div[data-testid="socialContext"]:has-text("Pinned")')
        if is_pinned_element: continue
        latest_id, tweet_text = await get_tweet_info_from_article(article)
        if not latest_id: continue
        if latest_id == last_sent_id: return None, last_sent_id
        
        print(f"发现 @{username} 的新动态！ID: {latest_id}")
        
        if ENABLE_TRANSLATION and tweet_text:
            translated_text = await translate_text_with_deepseek(aiohttp_session, tweet_text)
            if translated_text and icon_b64_data:
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
                args_for_js = [translated_text, icon_b64_data]
                await article.evaluate(js_code, args_for_js)
                print(f"已将翻译注入到推文 {latest_id} 的页面中。")

        print(f"准备截图...")
        # 【核心修改】使用 username 变量确保截图文件名不会冲突
        screenshot_filename = f"tweet_{username}_{latest_id}.png"
        screenshot_path = os.path.join(IMAGE_CACHE_DIR, screenshot_filename)
        await article.screenshot(path=screenshot_path)
        await article.evaluate("(article) => { const box = article.querySelector('#custom-translation-box'); if (box) box.remove(); }")
        
        absolute_image_path = os.path.abspath(screenshot_path)
        message_to_send = f"[CQ:image,file=file:///{absolute_image_path}]"
        # 【核心修改】使用 username 变量生成正确的链接
        tweet_link = f"https://x.com/{username}/status/{latest_id}"
        message_to_send += f"\n链接: {tweet_link}"
        print("消息准备完成。")
        return message_to_send, latest_id
        
    return None, last_sent_id

async def main(cleaned_cookies, icon_b64_data):
    # 【核心修改】使用字典为每个用户独立管理状态
    last_sent_ids = {username: None for username in TARGET_USERNAMES}

    if not os.path.exists(IMAGE_CACHE_DIR): os.makedirs(IMAGE_CACHE_DIR)
    async with async_playwright() as p, aiohttp.ClientSession() as aiohttp_session:
        browser_proxy = {"server": PROXY_URL} if PROXY_URL else None
        browser = await p.chromium.launch(headless=True, proxy=browser_proxy)
        context = await browser.new_context(viewport={'width': 800, 'height': 1200}, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36")
        await context.add_cookies(cleaned_cookies)
        print("Cookie 数据已成功加载。")
        page = await context.new_page()
        print(f"机器人启动，监控用户: {', '.join([f'@{u}' for u in TARGET_USERNAMES])}")
        
        while True:
            print("\n" + "="*50 + f"\n开始新一轮检查... (时间: {time.strftime('%Y-%m-%d %H:%M:%S')})")
            
            # 【核心修改】循环遍历所有目标用户
            for username in TARGET_USERNAMES:
                try:
                    home_url = f"https://x.com/{username}"
                    current_last_id = last_sent_ids[username]
                    is_initial_run_for_user = (current_last_id is None)

                    if is_initial_run_for_user:
                        print(f"--- [ @{username} ] 开始初始化检查...")
                    else:
                        print(f"--- [ @{username} ] 正在检查更新...")

                    message, new_id = await find_and_prepare_messages(page, aiohttp_session, icon_b64_data, username, home_url, current_last_id)
                    
                    if message and new_id:
                        all_sends_successful = False
                        if is_initial_run_for_user:
                            print(f"--- [ @{username} ] 发现初始化动态，推送至主群聊 ---")
                            message_to_send = f"初始化检查到 @{username} 的动态：\n" + message
                            all_sends_successful = await send_one_message(PRIMARY_GROUP_ID, message_to_send)
                        else:
                            print(f"--- [ @{username} ] 发现新动态，推送至所有已配置的群聊 ---")
                            message_to_send = message
                            primary_success = await send_one_message(PRIMARY_GROUP_ID, message_to_send)
                            secondary_success = await send_one_message(SECONDARY_GROUP_ID, message_to_send)
                            all_sends_successful = primary_success and secondary_success
                        
                        if all_sends_successful:
                            print(f"--- [ @{username} ] 所有推送任务成功完成。状态已更新。")
                            last_sent_ids[username] = new_id
                        else:
                            print(f"--- [ @{username} ] 部分或全部推送失败。状态未更新，将在下一个周期重试。")

                    elif new_id and not message:
                        last_sent_ids[username] = new_id

                except Exception as e:
                    print(f"！！！检查用户 @{username} 时遇到严重错误！！！")
                    error_details = traceback.format_exc()
                    print(error_details)
                    error_message = (f"【机器人发生严重错误】\n"
                                     f"监控用户: @{username}\n"
                                     f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                                     f"错误类型: {type(e).__name__}\n"
                                     f"------\n{error_details}")
                    if isinstance(e, PlaywrightError):
                        try:
                            screenshot_filename = f"error_screenshot_{username}_{int(time.time())}.png"
                            screenshot_path = os.path.join(IMAGE_CACHE_DIR, screenshot_filename)
                            await page.screenshot(path=screenshot_path, full_page=True)
                            print(f"已捕获Playwright错误，诊断截图已保存至: {screenshot_path}")
                            error_message += f"\n\n[诊断建议]: 出现此错误通常意味着Cookie失效或页面结构改变。\n请查看诊断截图以确定问题: {screenshot_path}"
                        except Exception as screenshot_e:
                            print(f"尝试进行错误截图时也失败了: {screenshot_e}")
                    print("错误报告将发送至主群聊。")
                    await send_one_message(PRIMARY_GROUP_ID, error_message)

            print("\n所有用户检查完毕。")
            print(f"{CHECK_INTERVAL_SECONDS}秒后将进行下一次检查...")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    cookies = load_cookies_from_file(COOKIE_FILE_PATH)
    if not cookies:
        print("无法加载Cookie，程序退出。")
        exit()
    icon_data = image_file_to_base64(TRANSLATION_ICON_FILE_PATH)
    if not icon_data:
        print("警告：图标加载失败，翻译部分将不显示图标。")
    print("初始化完成，准备启动主程序...")
    try:
        asyncio.run(main(cookies, icon_data))
    except KeyboardInterrupt:
        print("\n程序已手动停止。")
    except Exception as e:
        print(f"\n程序出现未捕获的致命异常，即将退出: {e}")
        traceback.print_exc()
