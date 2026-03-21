import asyncio
import ujson as json  # 高性能JSON解析库
from pathlib import Path  # 路径处理库
from loguru import logger  # 日志库
from httpx import AsyncClient  # 异步HTTP请求库
import hashlib  # MD5哈希库（用于生成文件名）

# ==================== 核心配置区（修改这里的群晖地址） ====================
CDN_URL = "http://7se.de5.net:8888/music/"  
USE_CDN = True  
VERSION = "0.2.0"  
# ========================================================================

# -------------------- 路径常量定义 --------------------
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DATA_JSON_PATH = DATA_DIR / "origins.json"

DIST_DIR = Path(__file__).parent.parent / "dist"
DIST_DIR.mkdir(exist_ok=True)
DIST_JSON_PATH = DIST_DIR / "plugins.json"

# -------------------- 重试/超时配置 --------------------
MAX_RETRIES = 3
RETRY_DELAY = 1
REQUEST_TIMEOUT = 15.0

# -------------------- 日志配置 --------------------
logger.add("plugin_update.log", rotation="1 day", retention="3 days", level="INFO")

async def load_origins():
    """加载插件源配置文件"""
    try:
        if not DATA_JSON_PATH.exists():
            logger.error(f"插件源配置文件不存在：{DATA_JSON_PATH.absolute()}")
            return None
        
        with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
            raw_content = f.read().strip()
        logger.info(f"读取到origins.json内容（前200字符）：{raw_content[:200]}")
        
        origins = json.loads(raw_content)
        total_sources = len(origins.get("sources", [])) + len(origins.get("singles", []))
        logger.info(f"成功加载 {total_sources} 个插件源")
        return origins
    
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON解析失败！origins.json不是合法JSON格式：{str(e)}")
        logger.error("请检查origins.json内容，必须是标准JSON，不能是文本/Markdown")
        return None
    except Exception as e:
        logger.error(f"❌ 加载配置文件失败：{str(e)}")
        return None

async def fetch_sub_plugins(url, client):
    """从批量订阅源URL获取插件列表"""
    try:
        for retry in range(MAX_RETRIES):
            try:
                response = await client.get(url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                data = response.json()
                return data.get("plugins", [])
            except Exception as e:
                logger.warning(f"获取订阅源 {url} 失败（第 {retry+1} 次）：{str(e)}")
                await asyncio.sleep(RETRY_DELAY)
        return []
    except Exception as e:
        logger.error(f"处理订阅源 {url} 异常：{str(e)}")
        return []

async def collect_plugins(origins, client):
    """收集所有插件并替换URL为群晖地址"""
    all_plugins = []
    
    for source in origins.get("sources", []):
        plugins = await fetch_sub_plugins(source, client)
        all_plugins.extend(plugins)
    
    all_plugins.extend(origins.get("singles", []))
    
    # 去重
    unique_plugins = []
    seen_urls = set()
    for plugin in all_plugins:
        if "url" not in plugin:
            continue
        if plugin["url"] not in seen_urls:
            seen_urls.add(plugin["url"])
            unique_plugins.append(plugin)
    logger.info(f"插件去重完成，剩余 {len(unique_plugins)} 个唯一插件")
    
    # 替换URL为群晖地址
    if USE_CDN:
        logger.info("开始替换插件URL为群晖地址（MD5命名）...")
        for plugin in unique_plugins:
            plugin["original_url"] = plugin["url"]
            md5_hash = hashlib.md5(plugin["url"].encode("utf-8")).hexdigest()
            js_filename = f"{md5_hash}.js"
            plugin["url"] = f"{CDN_URL}{js_filename}"
    
    return unique_plugins

async def download_and_process_plugin(plugin, client):
    """下载单个插件并保存到dist目录"""
    try:
        original_url = plugin.get("original_url", plugin["url"])
        plugin_name = plugin.get("name", "未知插件")
        logger.info(f"开始下载插件：{plugin_name} -> {original_url}")
        
        response = await client.get(original_url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            logger.warning(f"⚠️ 插件 {plugin_name} 原始地址404，跳过：{original_url}")
            return None
        response.raise_for_status()
        plugin_content = response.text.encode("utf-8")
        
        md5_hash = hashlib.md5(original_url.encode("utf-8")).hexdigest()
        js_filename = f"{md5_hash}.js"
        js_path = DIST_DIR / js_filename
        
        with open(js_path, "wb") as f:
            f.write(plugin_content)
        
        logger.info(f"✅ 处理成功：{plugin_name} -> {js_filename}")
        return plugin
    
    except Exception as e:
        logger.error(f"❌ 处理插件 {plugin.get('name', '未知插件')} 失败：{str(e)}")
        return None

async def fetch_plugins(all_plugins, client):
    """批量下载所有插件"""
    tasks = []
    for plugin in all_plugins:
        tasks.append(download_and_process_plugin(plugin, client))
    
    results = await asyncio.gather(*tasks)
    valid_plugins = [p for p in results if p is not None]
    return valid_plugins

async def save_results(valid_plugins):
    """保存最终的plugins.json（修复斜杠转义）"""
    try:
        result_data = {
            "desc": VERSION,
            "plugins": valid_plugins
        }
        # 核心修复：先序列化再替换转义斜杠
        json_str = json.dumps(result_data, ensure_ascii=False, indent=2)
        json_str = json_str.replace('\\/', '/')  # 去掉转义的反斜杠
        
        with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(json_str)
        
        logger.info(f"✅ 成功保存 {len(valid_plugins)} 个插件到：{DIST_JSON_PATH.absolute()}")
        if valid_plugins:
            logger.info(f"插件列表预览：{valid_plugins[:2]}")
        return True
    
    except Exception as e:
        logger.error(f"❌ 保存插件列表失败：{str(e)}")
        return False

async def main():
    """主函数"""
    logger.info("===== 开始执行插件更新任务 =====")
    logger.info(f"插件源配置路径: {DATA_JSON_PATH.absolute()}")
    logger.info(f"配置文件是否存在: {DATA_JSON_PATH.exists()}")
    logger.info(f"输出目录路径: {DIST_DIR.absolute()}")
    
    # 加载插件源配置
    origins = await load_origins()
    if not origins:
        logger.error("❌ 未加载到任何插件源配置，任务终止")
        # 修复空文件的转义问题
        empty_data = {"desc": VERSION, "plugins": []}
        json_str = json.dumps(empty_data, ensure_ascii=False, indent=2).replace('\\/', '/')
        with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(json_str)
        return
    
    # 收集插件
    async with AsyncClient(follow_redirects=True, verify=False) as client:
        all_plugins = await collect_plugins(origins, client)
        if not all_plugins:
            logger.warning("⚠️ 未收集到任何插件")
            # 修复空文件的转义问题
            empty_data = {"desc": VERSION, "plugins": []}
            json_str = json.dumps(empty_data, ensure_ascii=False, indent=2).replace('\\/', '/')
            with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
                f.write(json_str)
            return
        
        # 下载插件
        valid_plugins = await fetch_plugins(all_plugins, client)
    
    # 保存结果
    if valid_plugins:
        await save_results(valid_plugins)
    else:
        logger.error("❌ 没有有效插件可保存")
        # 修复空文件的转义问题
        empty_data = {"desc": VERSION, "plugins": []}
        json_str = json.dumps(empty_data, ensure_ascii=False, indent=2).replace('\\/', '/')
        with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(json_str)
    
    logger.info("===== 插件更新任务结束 =====")

if __name__ == "__main__":
    asyncio.run(main())
