import asyncio
import base64
import json
import logging
from io import BytesIO

from PIL import Image
from playwright.async_api import async_playwright, Page, Error as PlaywrightError
import websockets

# --- 配置 ---
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@Kano_/videos"
ONEBOT_WS_URL = "ws://127.0.0.1:15700/onebot/v11/ws"
MAIN_GROUP_ID = 977105881
# 启用副推送群，请将 987654321 修改为您的副推送群号
SUB_GROUP_ID = 987654321

# --- 全局变量 ---
latest_video_url = ""
logger = logging.getLogger("youtube_bot")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- OneBot V11 通信 ---

async def send_group_message(ws, group_id: int, message: str):
    """向指定群聊发送消息"""
    if not group_id:
        return
    payload = {
        "action": "send_group_msg",
        "params": {
            "group_id": group_id,
            "message": message
        }
    }
    await ws.send(json.dumps(payload))
    logger.info(f"已向群 {group_id} 成功发送消息: {message[:50]}...")

async def send_error_message(ws, error_message: str):
    """向主推送群发送错误信息"""
    logger.error(error_message)
    await send_group_message(ws, MAIN_GROUP_ID, f"机器人出错啦！\n错误信息：\n{error_message}")

# --- YouTube 操作 ---

async def get_latest_video_screenshot(page: Page) -> tuple[str, bytes] | tuple[None, None]:
    """
    获取最新视频的链接和截图（带空白边框）.
    返回 (视频链接, 带边框的截图的二进制数据)
    """
    try:
        logger.info(f"正在访问 YouTube 频道: {YOUTUBE_CHANNEL_URL}")
        # 增加 'domcontentloaded' 等待，有时 networkidle 会过慢
        await page.goto(YOUTUBE_CHANNEL_URL, wait_until="domcontentloaded", timeout=60000)
        # 等待视频网格元素出现，这是更可靠的等待方式
        await page.wait_for_selector("#contents.ytd-rich-grid-renderer", timeout=30000)


        video_selector = "ytd-rich-grid-media"
        await page.wait_for_selector(video_selector, timeout=30000)

        latest_video_element = page.locator(video_selector).first
        if not await latest_video_element.is_visible():
            logger.warning("最新的视频元素不可见。")
            return None, None

        video_link_element = latest_video_element.locator("a#video-title-link")
        raw_url = await video_link_element.get_attribute("href")
        if not raw_url:
            logger.error("无法获取最新视频的链接。")
            return None, None

        # 标准化URL，去除列表等参数
        video_id = raw_url.split('v=')[1].split('&')[0]
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info(f"成功定位到最新视频！标准化URL: {video_url}")

        screenshot_bytes = await latest_video_element.screenshot()
        
        PADDING_SIZE = 15
        PADDING_COLOR = (255, 255, 255)

        original_image = Image.open(BytesIO(screenshot_bytes))
        new_width = original_image.width + 2 * PADDING_SIZE
        new_height = original_image.height + 2 * PADDING_SIZE
        padded_image = Image.new("RGB", (new_width, new_height), PADDING_COLOR)
        padded_image.paste(original_image, (PADDING_SIZE, PADDING_SIZE))
        
        output_buffer = BytesIO()
        padded_image.save(output_buffer, format="PNG")
        padded_screenshot_bytes = output_buffer.getvalue()
        
        return video_url, padded_screenshot_bytes

    except PlaywrightError as e:
        logger.error(f"Playwright 操作失败: {e}")
        raise
    except Exception as e:
        logger.error(f"获取最新视频截图时发生未知错误: {e}")
        raise

# --- 主逻辑 ---

async def main_bot_loop():
    """机器人主循环"""
    global latest_video_url

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(no_viewport=True) # 禁用视口，可能有助于稳定性
        page = await context.new_page()

        while True:
            try:
                async with websockets.connect(ONEBOT_WS_URL) as ws:
                    logger.info("已成功连接到 OneBot V11 WebSocket 服务端。")

                    if not latest_video_url:
                        try:
                            logger.info("正在执行初始化测试...")
                            initial_url, initial_screenshot = await get_latest_video_screenshot(page)
                            if initial_url and initial_screenshot:
                                image_base64 = base64.b64encode(initial_screenshot).decode()
                                cq_image = f"[CQ:image,file=base64://{image_base64}]"
                                await send_group_message(ws, MAIN_GROUP_ID, f"机器人初始化成功！\n当前最新视频截图如下：\n{cq_image}")
                                # 初始化成功后，才设置 latest_video_url
                                latest_video_url = initial_url
                            else:
                                await send_error_message(ws, "初始化失败：无法获取最新视频信息。")
                        except Exception as e:
                            await send_error_message(ws, f"初始化测试失败: {e}")

                    while True:
                        try:
                            logger.info("开始新一轮视频检查...")
                            new_url, new_screenshot = await get_latest_video_screenshot(page)

                            if new_url and new_url != latest_video_url:
                                logger.info(f"检测到待推送的新视频！URL: {new_url}")
                                image_base64 = base64.b64encode(new_screenshot).decode()
                                
                                cq_image = f"[CQ:image,file=base64://{image_base64}]"
                                push_message = f"鹿乃发布了新视频：\n{cq_image}"
                                
                                # 尝试发送通知
                                logger.info("准备向群聊推送新视频通知...")
                                await send_group_message(ws, MAIN_GROUP_ID, push_message)
                                await send_group_message(ws, SUB_GROUP_ID, push_message)
                                
                                # ######################################################
                                # ## 关键改动：在确认所有消息都发送成功后，才更新URL状态 ##
                                # ######################################################
                                logger.info("推送成功，正在更新本地最新视频记录。")
                                latest_video_url = new_url

                            elif not new_url:
                                 logger.warning("本次检查未能获取到视频URL，跳过。")
                            else:
                                logger.info("未检测到新视频。")

                            await asyncio.sleep(10)

                        except websockets.exceptions.ConnectionClosed:
                            logger.warning("WebSocket 连接在检查/发送过程中断开，将尝试重连...")
                            # 不做任何操作，直接 break，让外层循环处理重连
                            # 因为 latest_video_url 未更新，下次会重试
                            break
                        except PlaywrightError as e:
                            await send_error_message(ws, f"浏览器操作出错: {e}")
                            await asyncio.sleep(60)
                        except Exception as e:
                            await send_error_message(ws, f"主循环中发生未知错误: {e}")
                            await asyncio.sleep(60)

            except (websockets.exceptions.ConnectionClosedError, ConnectionRefusedError) as e:
                logger.error(f"无法连接到 OneBot WebSocket 服务端: {e}. 1分钟后重试...")
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"发生严重错误，程序将在一分钟后重启: {e}")
                await asyncio.sleep(60)
        
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main_bot_loop())
    except KeyboardInterrupt:
        logger.info("程序被手动中断。")
