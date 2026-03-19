import asyncio
import ujson as json
from pathlib import Path
from loguru import logger
from httpx import AsyncClient
import hashlib

# ==================== 核心配置（修改这里的群晖地址） ====================
CDN_URL = "http://7se.de5.net:8888/music/"  # 你的群晖地址，末尾必须带 /
USE_CDN = True  # 必须设为 True
VERSION = "0.2.0"
# =====================================================================

# 定义路径常量
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DATA_JSON_PATH = DATA_DIR / "origins.json"

DIST_DIR = Path(__file__).parent.parent / "dist"
DIST_DIR.mkdir(exist_ok=True)
DIST_JSON_PATH = DIST_DIR / "plugins.json"

# 重试相关常量
MAX_RETRIES = 3
RETRY_DELAY = 1
REQUEST_TIMEOUT = 10.0

# 初始化日志
logger.add("plugin_update.log", rotation="1 day", retention="3 days", level="INFO")

async def load_origins():
    """加载插件源配置"""
    try:
        if not DATA_JSON_PATH.exists():
            logger.error(f"配置文件不存在：{DATA_JSON_PATH.absolute()}")
            return None
        
        with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
            origins = json.load(f)
        logger.info(f"成功加载 {len(origins.get('sources', [])) + len(origins.get('singles', []))} 个插件源")
        return origins
    except Exception as e:
        logger.error(f"加载配置文件失败：{str(e)}")
        return None

async def fetch_sub_plugins(url, client):
    """获取订阅源中的插件列表"""
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
    """收集所有插件"""
    all_plugins = []
    
    # 处理批量订阅源
    for source in origins.get("sources", []):
        plugins = await fetch_sub_plugins(source, client)
        all_plugins.extend(plugins)
    
    # 处理单个插件
    all_plugins.extend(origins.get("singles", []))
    
    # 去重（按 url 去重）
    unique_plugins = []
    seen_urls = set()
    for plugin in all_plugins:
        if "url" not in plugin:
            continue
        if plugin["url"] not in seen_urls:
            seen_urls.add(plugin["url"])
            unique_plugins.append(plugin)
    
    logger.info(f"去重后共 {len(unique_plugins)} 个插件")
    return unique_plugins

async def download_and_process_plugin(plugin, client):
    """下载插件并处理（生成MD5文件名+替换URL）"""
    try:
        plugin_url = plugin["url"]
        # 下载插件内容
        response = await client.get(plugin_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        plugin_content = response.text.encode("utf-8")
        
        # 生成MD5文件名
        md5_hash = hashlib.md5(plugin_content).hexdigest()
        js_filename = f"{md5_hash}.js"
        js_path = DIST_DIR / js_filename
        
        # 保存插件文件
        with open(js_path, "wb") as f:
            f.write(plugin_content)
        
        # 替换URL为CDN地址
        if USE_CDN:
            plugin["url"] = f"{CDN_URL}{js_filename}"
        
        logger.info(f"处理成功：{plugin.get('name', '未知插件')} -> {js_filename}")
        return plugin
    except Exception as e:
        logger.error(f"处理插件 {plugin.get('name', '未知插件')} 失败：{str(e)}")
        return None

async def fetch_plugins(all_plugins, client):
    """批量下载并处理插件"""
    tasks = []
    for plugin in all_plugins:
        tasks.append(download_and_process_plugin(plugin, client))
    
    results = await asyncio.gather(*tasks)
    # 过滤掉失败的插件
    valid_plugins = [p for p in results if p is not None]
    return valid_plugins

async def save_results(data):
    """保存最终的plugins.json"""
    try:
        with open(DIST_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"成功保存插件列表到：{DIST_JSON_PATH.absolute()}")
        logger.info(f"共生成 {len(data.get('plugins', []))} 个有效插件")
        return True
    except Exception as e:
        logger.error(f"保存插件列表失败：{str(e)}")
        return False

async def main():
    """主函数"""
    logger.info("===== 开始执行插件更新任务 =====")
    # 打印路径信息（排查用）
    logger.info(f"DATA_DIR 路径: {DATA_DIR.absolute()}")
    logger.info(f"DATA_JSON_PATH 存在: {DATA_JSON_PATH.exists()}")
    logger.info(f"DIST_DIR 路径: {DIST_DIR.absolute()}")
    
    # 1. 加载配置
    origins = await load_origins()
    if not origins:
        logger.error("未加载到任何插件源配置")
        return
    
    # 2. 收集插件
    async with AsyncClient(follow_redirects=True, verify=False) as client:
        all_plugins = await collect_plugins(origins, client)
        if not all_plugins:
            logger.warning("未收集到任何插件")
            return
        
        # 3. 下载并处理插件
        valid_plugins = await fetch_plugins(all_plugins, client)
    
    # 4. 保存结果
    if valid_plugins:
        await save_results({"desc": VERSION, "plugins": valid_plugins})
    else:
        logger.error("没有有效插件可保存")
    
    logger.info("===== 插件更新任务结束 =====")

if __name__ == "__main__":
    # 运行主函数
    asyncio.run(main())
