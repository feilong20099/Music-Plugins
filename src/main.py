import asyncio
import ujson as json  # 高性能JSON解析库
from pathlib import Path  # 路径处理库
from loguru import logger  # 日志库
from httpx import AsyncClient  # 异步HTTP请求库
import hashlib  # MD5哈希库（用于生成文件名）

# ==================== 核心配置区（修改这里的群晖地址） ====================
# 你的群晖地址，末尾必须带 /，最终生成的URL格式：http://7se.de5.net:8888/music/MD5值.js
CDN_URL = "http://7se.de5.net:8888/music/"  
USE_CDN = True  # 是否替换为CDN（群晖）地址，必须设为True
VERSION = "0.2.0"  # 插件列表版本号
# ========================================================================

# -------------------- 路径常量定义（无需修改） --------------------
# 插件源配置文件路径（src/data/origins.json）
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)  # 若目录不存在则创建
DATA_JSON_PATH = DATA_DIR / "origins.json"

# 生成文件输出路径（仓库根目录/dist）
DIST_DIR = Path(__file__).parent.parent / "dist"
DIST_DIR.mkdir(exist_ok=True)  # 确保输出目录存在
DIST_JSON_PATH = DIST_DIR / "plugins.json"

# -------------------- 重试/超时配置（无需修改） --------------------
MAX_RETRIES = 3  # 插件下载最大重试次数
RETRY_DELAY = 1  # 重试间隔（秒）
REQUEST_TIMEOUT = 15.0  # 请求超时时间（秒）

# -------------------- 日志配置（无需修改） --------------------
# 日志文件按天分割，保留3天，日志级别为INFO
logger.add("plugin_update.log", rotation="1 day", retention="3 days", level="INFO")

async def load_origins():
    """
    加载插件源配置文件（src/data/origins.json）
    返回值：合法的JSON对象 | None（加载失败）
    """
    try:
        # 检查配置文件是否存在
        if not DATA_JSON_PATH.exists():
            logger.error(f"插件源配置文件不存在：{DATA_JSON_PATH.absolute()}")
            return None
        
        # 读取文件内容并打印前200字符（排查用）
        with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
            raw_content = f.read().strip()
        logger.info(f"读取到origins.json内容（前200字符）：{raw_content[:200]}")
        
        # 解析JSON（容错处理）
        origins = json.loads(raw_content)
        # 统计插件源数量（批量源+单个源）
        total_sources = len(origins.get("sources", [])) + len(origins.get("singles", []))
        logger.info(f"成功加载 {total_sources} 个插件源")
        return origins
    
    # JSON格式错误（核心容错）
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON解析失败！origins.json不是合法JSON格式：{str(e)}")
        logger.error("请检查origins.json内容，必须是标准JSON，不能是文本/Markdown")
        return None
    # 其他异常
    except Exception as e:
        logger.error(f"❌ 加载配置文件失败：{str(e)}")
        return None

async def fetch_sub_plugins(url, client):
    """
    从批量订阅源URL获取插件列表
    参数：
        url: 订阅源地址
        client: 异步HTTP客户端
    返回值：插件列表（list）
    """
    try:
        # 重试机制
        for retry in range(MAX_RETRIES):
            try:
                response = await client.get(url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()  # 非200状态码抛出异常
                data = response.json()
                return data.get("plugins", [])  # 提取插件列表
            except Exception as e:
                logger.warning(f"获取订阅源 {url} 失败（第 {retry+1} 次）：{str(e)}")
                await asyncio.sleep(RETRY_DELAY)  # 重试间隔
        return []  # 多次重试失败返回空列表
    except Exception as e:
        logger.error(f"处理订阅源 {url} 异常：{str(e)}")
        return []

async def collect_plugins(origins, client):
    """
    收集所有插件并替换URL为群晖地址（MD5命名）
    参数：
        origins: 插件源配置
        client: 异步HTTP客户端
    返回值：去重后的插件列表（已替换URL）
    """
    all_plugins = []
    
    # 第一步：处理批量订阅源（sources）
    for source in origins.get("sources", []):
        plugins = await fetch_sub_plugins(source, client)
        all_plugins.extend(plugins)
    
    # 第二步：处理单个插件（singles）
    all_plugins.extend(origins.get("singles", []))
    
    # 第三步：去重（按URL去重，避免重复插件）
    unique_plugins = []
    seen_urls = set()  # 记录已出现的URL
    for plugin in all_plugins:
        if "url" not in plugin:  # 跳过无URL的插件
            continue
        if plugin["url"] not in seen_urls:
            seen_urls.add(plugin["url"])
            unique_plugins.append(plugin)
    logger.info(f"插件去重完成，剩余 {len(unique_plugins)} 个唯一插件")
    
    # 第四步：替换URL为群晖地址（核心逻辑）
    if USE_CDN:
        logger.info("开始替换插件URL为群晖地址（MD5命名）...")
        for plugin in unique_plugins:
            # 保存原始URL（用于下载插件）
            plugin["original_url"] = plugin["url"]
            # 基于原始URL生成MD5文件名（避免文件名冲突）
            md5_hash = hashlib.md5(plugin["url"].encode("utf-8")).hexdigest()
            js_filename = f"{md5_hash}.js"
            # 替换为群晖地址 + MD5文件名
            plugin["url"] = f"{CDN_URL}{js_filename}"
    
    return unique_plugins

async def download_and_process_plugin(plugin, client):
    """
    下载单个插件并保存到dist目录（MD5命名）
    参数：
        plugin: 插件信息（包含original_url）
        client: 异步HTTP客户端
    返回值：成功返回插件信息 | 失败返回None
    """
    try:
        # 获取原始下载地址（优先用original_url，无则用url）
        original_url = plugin.get("original_url", plugin["url"])
        plugin_name = plugin.get("name", "未知插件")
        logger.info(f"开始下载插件：{plugin_name} -> {original_url}")
        
        # 下载插件内容
        response = await client.get(original_url, timeout=REQUEST_TIMEOUT)
        # 404容错：跳过失效插件
        if response.status_code == 404:
            logger.warning(f"⚠️ 插件 {plugin_name} 原始地址404，跳过：{original_url}")
            return None
        response.raise_for_status()  # 其他错误抛出异常
        plugin_content = response.text.encode("utf-8")  # 转字节流保存
        
        # 生成MD5文件名（和URL替换时保持一致）
        md5_hash = hashlib.md5(original_url.encode("utf-8")).hexdigest()
        js_filename = f"{md5_hash}.js"
        js_path = DIST_DIR / js_filename
        
        # 保存插件文件到dist目录
        with open(js_path, "wb") as f:
            f.write(plugin_content)
        
        logger.info(f"✅ 处理成功：{plugin_name} -> {js_filename}")
        return plugin
    
    except Exception as e:
        logger.error(f"❌ 处理插件 {plugin.get('name', '未知插件')} 失败：{str(e)}")
        return None

async def fetch_plugins(all_plugins, client):
    """
    批量下载所有插件（异步处理）
    参数：
        all_plugins: 插件列表
        client: 异步HTTP客户端
    返回值：下载成功的插件列表
    """
    # 创建异步任务列表
    tasks = []
    for plugin in all_plugins:
        tasks.append(download_and_process_plugin(plugin, client))
    
    # 批量执行任务并收集结果
    results = await asyncio.gather(*tasks)
    # 过滤掉失败的插件（None值）
    valid_plugins = [p for p in results if p is not None]
    return valid_plugins

async def save_results(valid_plugins):
    """
    保存最终的plugins.json（带群晖地址）
    参数：
        valid_plugins: 下载成功的插件列表
    返回值：成功返回True | 失败返回False
    """
    try:
        # 构造最终输出格式
        result_data = {
            "desc": VERSION,  # 版本描述
            "plugins": valid_plugins  # 插件列表
        }
        # 写入JSON文件（格式化输出，中文不转义）
        with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(result_data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"✅ 成功保存 {len(valid_plugins)} 个插件到：{DIST_JSON_PATH.absolute()}")
        # 打印前2个插件预览（确认URL是否为群晖地址）
        if valid_plugins:
            logger.info(f"插件列表预览：{valid_plugins[:2]}")
        return True
    
    except Exception as e:
        logger.error(f"❌ 保存插件列表失败：{str(e)}")
        return False

async def main():
    """主函数：执行完整的插件收集→下载→保存流程"""
    logger.info("===== 开始执行插件更新任务 =====")
    # 打印路径信息（排查用）
    logger.info(f"插件源配置路径: {DATA_JSON_PATH.absolute()}")
    logger.info(f"配置文件是否存在: {DATA_JSON_PATH.exists()}")
    logger.info(f"输出目录路径: {DIST_DIR.absolute()}")
    
    # 第一步：加载插件源配置
    origins = await load_origins()
    if not origins:
        logger.error("❌ 未加载到任何插件源配置，任务终止")
        # 生成空的plugins.json（避免Workflow报错）
        with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump({"desc": VERSION, "plugins": []}, f, ensure_ascii=False, indent=2)
        return
    
    # 第二步：收集插件（替换URL）
    # 创建异步HTTP客户端（允许重定向，忽略SSL验证）
    async with AsyncClient(follow_redirects=True, verify=False) as client:
        all_plugins = await collect_plugins(origins, client)
        if not all_plugins:
            logger.warning("⚠️ 未收集到任何插件")
            # 生成空文件
            with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump({"desc": VERSION, "plugins": []}, f, ensure_ascii=False, indent=2)
            return
        
        # 第三步：批量下载插件
        valid_plugins = await fetch_plugins(all_plugins, client)
    
    # 第四步：保存结果
    if valid_plugins:
        await save_results(valid_plugins)
    else:
        logger.error("❌ 没有有效插件可保存")
        # 生成空文件
        with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump({"desc": VERSION, "plugins": []}, f, ensure_ascii=False, indent=2)
    
    logger.info("===== 插件更新任务结束 =====")

# 程序入口
if __name__ == "__main__":
    asyncio.run(main())
