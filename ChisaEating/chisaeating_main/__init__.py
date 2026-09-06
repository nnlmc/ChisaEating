import random
import re
from pathlib import Path
from typing import Dict, List, Optional, TypedDict

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment
from gsuid_core.sv import SV

from ..chisaeating_api import (
    _atomic_write_json,
    _download_allowed_https_file,
    _extract_dlc_zip_safe,
    _fetch_allowed_https_bytes,
    _get_base_dir,
    _get_optimal_dlc_node,
    _get_store_dir,
)
from ..chisaeating_config import CHISA_CONFIG
from ..utils.downloader import STATE as _DOWNLOAD_STATE
from ..utils.downloader import download_assets, has_food_assets
from ..utils.food_data import FoodDataManager
from ..utils.image_manager import ImageManager
from ..utils.rate_limiter import RateLimiter
import asyncio
import json
import shutil
import aiohttp

# API routes are imported with the plugin so the shared Core server exposes them.
from .. import web_api as _web_api  # noqa: F401

sv = SV("千小妹还在吃", pm=6, area="ALL")

_plugin_dir = Path(__file__).parent.parent.parent
_image_mgr = ImageManager(_plugin_dir)
_data_mgr = FoodDataManager()
_rate_limiter = RateLimiter()

_EAT_KWS = ("吃什么", "吃啥", "吃点儿啥", "吃点啥")
_DRINK_KWS = ("喝什么", "喝啥", "喝点儿啥", "喝点啥")
_DARK_KWS = ("来点黑暗料理", "黑暗料理")
_COMMON_EAT_KWS = ("来点现实的食物", "来点三次元食物")
_COMMON_DRINK_KWS = ("来点现实的饮品", "来点三次元饮品")
_ALL_CAT_KWS = _EAT_KWS + _DRINK_KWS + _DARK_KWS + _COMMON_EAT_KWS + _COMMON_DRINK_KWS

# TypedDicts for all structured data.
# Chinese-key variants use the functional form so keys can be non-identifiers.
WorldConf = TypedDict(
    "WorldConf",
    {
        "名称": str,
        "别称": List[str],
        "自称池": List[str],
        "文字食物": List[str],
        "文字饮品": List[str],
        "文字黑暗料理": List[str],
    },
)

WorldPhrasesData = TypedDict(
    "WorldPhrasesData",
    {
        "专属句式": List[str],
        "厨师句式": List[str],
        "打断句式": List[str],
    },
)


class PoolItem(TypedDict):
    wv: str
    food: str
    raw_name: str
    chef: str
    has_image: bool
    path: Optional[str]


class GanfanrenData(TypedDict):
    images: List[str]
    words: List[str]


class ConfigSnapshot(TypedDict):
    mode_loyal: bool
    mode_roller: bool
    mode_normie: bool
    history_limit: int
    spam_threshold: int
    egg_prob: int
    egg_pool: str
    repeat_prob: int
    repeat_cooldown: int
    global_meme_prob: int
    chef_meme_prob: int
    interception_egg_chance: int
    weight_3d: int
    weight_world1: int
    weight_world2: int
    weight_world3: int
    weight_world4: int
    weight_world5: int


_WORLD_PHRASES: Dict[str, WorldPhrasesData] = {
    "world1": {
        "专属句式": [
            "{bot}觉得今天这顿非{food}莫属啦",
            "唔...根据{bot}的精密测算，今天你和{food}的相性是百分之百哦",
            "{bot}强烈建议你尝尝{food}，绝对不踩雷",
            "既然不知道吃什么，那就来一份索拉里斯特产的{food}吧",
            "快看快看，新鲜出炉的{food}！这可是索拉里斯最抢手的美食呢",
        ],
        "厨师句式": [
            "哇！这可是【{chef}】亲自下厨特制的{food}哦",
            "尝尝看！【{chef}】对这份{food}可是非常有自信呢",
            "这份{food}里满满都是【{chef}】的心意，不吃完的话{bot}可要生气啦",
            "天哪，居然能捕捉到【{chef}】亲手捏制的{food}，今天运气简直太好啦",
            "【{chef}】带着热腾腾的{food}走过来了，快趁热吃吧",
        ],
        "打断句式": [
            "{bot}认为吃得太多对健康不好哦，稍微休息会儿吧",
            "哎呀，后厨的锅都被你点冒烟啦，{bot}觉得需要给厨师放个假",
            "数据终端显示你的饱食度已经超标了呢，{bot}建议先去散散步哦",
            "警报！检测到点菜频率过快，{bot}申请开启防刷屏管制！",
            "再吃下去肚子就要变成圆滚滚的啦，{bot}才不帮你抱走呢",
        ],
    },
    "world2": {
        "专属句式": [
            "前面的区域，以后再来探索吧！先跟{bot}吃点{food}填饱肚子",
            "旅行者，{bot}的肚子已经咕咕叫了...我们快去吃{food}好不好",
            "愿风神保佑你今天吃到的{food}是最美味的",
            "听冒险家协会的人说，提瓦特的{food}最近超级火爆哦",
            "看在{food}的份上，{bot}就勉为其难再给你当一天向导吧",
        ],
        "厨师句式": [
            "哇！是【{chef}】的特色料理{food}！快分{bot}一口，就一口",
            "【{chef}】特意为你准备了{food}哦，吃饱了才有力气冒险嘛",
            "这份{food}可是【{chef}】花了好长时间才做好的，旅行者可千万别浪费",
            "天哪，是【{chef}】亲手掌勺的美味！这盘{food}归{bot}了",
            "闻到万民堂的香味了！【{chef}】端着热腾腾的{food}来看我们啦",
        ],
        "打断句式": [
            "喂！你点得太快啦！{bot}的嘴巴都要跟不上了",
            "再吃下去莫娜都要看不起我们了...{bot}建议先消化一下",
            "你这家伙，是想把万民堂吃破产吗！{bot}命令你停止点菜",
            "嗝~{bot}揉了稳滚滚的肚子，表示真的塞不下更多的菜了",
            "前面的区域（指厨房）以后再来探索吧！厨师已经被你刷罢工了",
        ],
    },
    "world3": {
        "专属句式": ["{bot}为你推荐了{food}", "{bot}觉得今天吃{food}不错"],
        "厨师句式": ["【{chef}】特制了{food}哦"],
        "打断句式": ["别刷啦！{bot}已经跟不上了"],
    },
    "world4": {
        "专属句式": ["{bot}为你推荐了{food}", "{bot}觉得今天吃{food}不错"],
        "厨师句式": ["【{chef}】特制了{food}哦"],
        "打断句式": ["别刷啦！{bot}已经跟不上了"],
    },
    "world5": {
        "专属句式": ["{bot}为你端来了{food}", "今天的推荐是{food}，请慢用"],
        "厨师句式": ["【{chef}】为你准备了{food}！"],
        "打断句式": ["先歇一会儿吧，{bot}的后厨需要喘口气"],
    },
}

def _configured_keywords(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    values = CHISA_CONFIG.get_config(key).data
    configured = tuple(str(value).strip() for value in values if str(value).strip())
    return configured or fallback


def _is_exact_trigger(message: str, keywords: tuple[str, ...]) -> bool:
    return message.strip() in keywords


def _extract_forced_chef(message: str) -> str | None:
    match = re.search(r"(?:召唤|想和)(?P<chef>.+?)(?:下厨|吃饭)$", message.strip())
    if match:
        return match.group("chef").strip() or None
    match = re.search(r"^(?P<chef>.+?)特供料理$", message.strip())
    return match.group("chef").strip() if match else None


def _get_wv_settings() -> Dict[str, WorldConf]:
    result: Dict[str, WorldConf] = {}
    for i in range(1, 6):
        wk = f"world{i}"
        result[wk] = {
            "名称": CHISA_CONFIG.get_config(f"{wk}_name").data,
            "别称": CHISA_CONFIG.get_config(f"{wk}_aliases").data,
            "自称池": CHISA_CONFIG.get_config(f"{wk}_selfnames").data,
            "文字食物": CHISA_CONFIG.get_config(f"{wk}_food_text").data,
            "文字饮品": CHISA_CONFIG.get_config(f"{wk}_drink_text").data,
            "文字黑暗料理": CHISA_CONFIG.get_config(f"{wk}_dark_text").data,
        }
    return result


def _build_alias_map(wv_settings: Dict[str, WorldConf]) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for wk, conf in wv_settings.items():
        for alias in conf["别称"]:
            if alias:
                alias_map[alias.strip()] = wk
    return alias_map


def _resolve_active_key() -> str:
    sel: str = str(CHISA_CONFIG.get_config("active_world").data).strip()
    named = {conf["名称"]: key for key, conf in _get_wv_settings().items()}
    if sel in named:
        return named[sel]
    if sel in ("world1", "world2", "world3", "world4", "world5"):
        return sel
    return "world1"


def _build_config_snapshot() -> ConfigSnapshot:
    return ConfigSnapshot(
        mode_loyal=CHISA_CONFIG.get_config("mode_loyal").data,
        mode_roller=CHISA_CONFIG.get_config("mode_roller").data,
        mode_normie=CHISA_CONFIG.get_config("mode_normie").data,
        history_limit=CHISA_CONFIG.get_config("history_limit").data,
        spam_threshold=CHISA_CONFIG.get_config("spam_threshold").data,
        egg_prob=CHISA_CONFIG.get_config("egg_prob").data,
        egg_pool=CHISA_CONFIG.get_config("egg_pool").data,
        repeat_prob=CHISA_CONFIG.get_config("repeat_prob").data,
        repeat_cooldown=CHISA_CONFIG.get_config("repeat_cooldown").data,
        global_meme_prob=CHISA_CONFIG.get_config("global_meme_prob").data,
        chef_meme_prob=CHISA_CONFIG.get_config("chef_meme_prob").data,
        interception_egg_chance=CHISA_CONFIG.get_config("interception_egg_chance").data,
        weight_3d=CHISA_CONFIG.get_config("weight_3d").data,
        weight_world1=CHISA_CONFIG.get_config("weight_world1").data,
        weight_world2=CHISA_CONFIG.get_config("weight_world2").data,
        weight_world3=CHISA_CONFIG.get_config("weight_world3").data,
        weight_world4=CHISA_CONFIG.get_config("weight_world4").data,
        weight_world5=CHISA_CONFIG.get_config("weight_world5").data,
    )


def _is_admin(ev: Event) -> bool:
    return ev.user_pm <= 3


def _read_catalog_file() -> List[Dict[str, Any]]:
    cat_path = _get_store_dir("official") / "catalog.json"
    if not cat_path.exists():
        return []
    try:
        with open(cat_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning(f"[ChisaEating] 读取商会目录失败: {exc}")
        return []


async def _extract_images_from_event(ev: Event) -> List[Tuple[bytes, str]]:
    results: List[Tuple[bytes, str]] = []
    if not ev.message:
        return results

    async with aiohttp.ClientSession() as session:
        for seg in ev.message:
            seg_type = getattr(seg, "type", None)
            seg_data = getattr(seg, "data", None)
            if seg_type != "image" or not seg_data:
                continue

            content: Optional[bytes] = None
            ext = ".jpg"

            if isinstance(seg_data, bytes):
                content = seg_data
            elif isinstance(seg_data, str):
                if seg_data.startswith("link://") or seg_data.startswith("http://") or seg_data.startswith("https://"):
                    url = seg_data.replace("link://", "")
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                lower_url = url.lower()
                                if ".png" in lower_url:
                                    ext = ".png"
                                elif ".gif" in lower_url:
                                    ext = ".gif"
                                elif ".webp" in lower_url:
                                    ext = ".webp"
                    except Exception as e:
                        logger.error(f"[ChisaEating] 下载消息图片失败: {e}")
                elif seg_data.startswith("base64://"):
                    import base64
                    try:
                        content = base64.b64decode(seg_data.replace("base64://", ""))
                    except Exception as e:
                        logger.error(f"[ChisaEating] 解码消息图片 base64 失败: {e}")
                elif Path(seg_data).is_file():
                    try:
                        content = Path(seg_data).read_bytes()
                        ext = Path(seg_data).suffix or ".jpg"
                    except Exception as e:
                        logger.error(f"[ChisaEating] 读取本地图片失败: {e}")

            if content:
                if content[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                elif content[:6] in (b"GIF87a", b"GIF89a"):
                    ext = ".gif"
                elif content[:4] == b"RIFF" and len(content) >= 12 and content[8:12] == b"WEBP":
                    ext = ".webp"
                results.append((content, ext))

    return results


@sv.on_fullmatch(("千小妹商会", "/千小妹商会"), prefix=False)
async def on_chisa_shop(bot: Bot, ev: Event) -> None:
    if not _is_admin(ev):
        await bot.send("哼！千小妹商会重地，闲人免进！只有管理员才能进去进货哦~")
        return

    cat_list = _read_catalog_file()
    if not cat_list:
        await bot.send("仓库空空如也！是否进行千小妹商会信息同步？\n（请发送：千小妹商会信息同步）")
        return

    prompt = (
        "🎀 千小妹商会营业中 🎀\n"
        "欢迎老板！请在 60 秒内回复数字选择进货通道：\n"
        "1️⃣ 干饭人/大厨/导游招募\n"
        "2️⃣ 云食品/云饮品仓库\n"
        "3️⃣ 黑暗料理次元裂缝"
    )
    resp = await bot.receive_resp(prompt)
    if resp is None:
        return

    choice = resp.text.strip()
    mapping = {
        "1": {"cats": ["gf", "cf", "gd"], "title": "千小妹商会 - 招募通道", "cmd_format": "招募{id}"},
        "2": {"cats": ["fd", "dr"], "title": "千小妹商会 - 餐饮通道", "cmd_format": "进货{id}"},
        "3": {"cats": ["dk"], "title": "千小妹商会 - 次元裂缝", "cmd_format": "黑魔法召唤{id}"},
    }

    if choice not in mapping:
        await bot.send("输入错误或超时，商会会话已结束。")
        return

    conf = mapping[choice]
    results: Dict[str, List[Dict[str, Any]]] = {}
    for item in cat_list:
        cat = item.get("cat", "")
        if cat in conf["cats"]:
            results.setdefault(cat, []).append(item)

    lines: List[str] = [
        f"📦 {conf['title']} 📦",
        f"* 发送\"{conf['cmd_format'].format(id='[编号]')}\"或\"{conf['cmd_format'].format(id='编号')}\"即可一键下载对应包体",
        "* 也可以在 Web 控制台【千小妹干饭管理】中直接预览和下载商品",
        "",
    ]

    emoji_map = {
        "fd": "🍔 食品区",
        "dr": "🧋 饮品区",
        "gf": "🏃 干饭人",
        "cf": "👨‍🍳 大厨",
        "gd": "🌸 导游MEME",
        "dk": "☠️ 黑暗料理",
    }

    for cat_key in conf["cats"]:
        lines.append(emoji_map.get(cat_key, cat_key))
        if cat_key in results and results[cat_key]:
            for it in results[cat_key]:
                lines.append(f"· [{it.get('id', '')}] {it.get('title', '')}")
        else:
            lines.append("· 这个分类暂时还没有商品上架 ·")
        lines.append("")

    full_text = "\n".join(lines).strip()
    try:
        await bot.send(MessageSegment.node([full_text]))
    except Exception:
        await bot.send(full_text)


@sv.on_fullmatch(("千小妹商会信息同步", "/千小妹商会信息同步", "千小妹商会信息同步拉取Json"), prefix=False)
async def on_chisa_shop_sync(bot: Bot, ev: Event) -> None:
    if not _is_admin(ev):
        await bot.send("只有管理员可以同步商会信息哦！")
        return

    await bot.send("正在联系商会总仓...请稍等片刻哦~")
    node = await _get_optimal_dlc_node()
    if node == "failed":
        await bot.send("所有节点响应超时，同步失败，请稍后再试！")
        return

    original_url = "https://raw.githubusercontent.com/dddada123/astrbot_plugin_chisa_still_eating_photo/main/index/catalog.json"
    url = original_url if node in ("direct", "") else f"https://{node}/{original_url}"

    try:
        content = await _fetch_allowed_https_bytes(
            url,
            max_bytes=8 * 1024 * 1024,
            trust_env=(node in ("direct", "")),
            timeout_seconds=20,
        )
        if not content:
            raise RuntimeError("商会目录响应为空")

        catalog = json.loads(content.decode("utf-8-sig"))
        if not isinstance(catalog, list) or len(catalog) > 5000:
            raise ValueError("目录格式不合法")

        cat_path = _get_store_dir("official") / "catalog.json"
        cat_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(cat_path, catalog)
        await bot.send("✅ 同步完成！请再次输入【千小妹商会】开始挑选美食与大厨~")
    except Exception as e:
        logger.error(f"[ChisaEating] 同步商会目录异常: {e}")
        await bot.send(f"同步异常: {e}")


@sv.on_keyword(_EAT_KWS, prefix=False)
async def on_eat(bot: Bot, ev: Event) -> None:
    logger.info(f"[ChisaEating] 点餐(食) | uid={ev.user_id} gid={ev.group_id}")
    await _process_request(bot, ev, "food")


@sv.on_keyword(_DRINK_KWS, prefix=False)
async def on_drink(bot: Bot, ev: Event) -> None:
    logger.info(f"[ChisaEating] 点餐(饮) | uid={ev.user_id} gid={ev.group_id}")
    await _process_request(bot, ev, "drink")


@sv.on_keyword(_DARK_KWS, prefix=False)
async def on_dark(bot: Bot, ev: Event) -> None:
    logger.info(f"[ChisaEating] 点餐(黑暗料理) | uid={ev.user_id} gid={ev.group_id}")
    await _process_request(bot, ev, "dark")


@sv.on_keyword(_COMMON_EAT_KWS, prefix=False)
async def on_common_eat(bot: Bot, ev: Event) -> None:
    logger.info(f"[ChisaEating] 点餐(三次元食) | uid={ev.user_id} gid={ev.group_id}")
    await _process_request(bot, ev, "food", forced_world="common")


@sv.on_keyword(_COMMON_DRINK_KWS, prefix=False)
async def on_common_drink(bot: Bot, ev: Event) -> None:
    logger.info(f"[ChisaEating] 点餐(三次元饮) | uid={ev.user_id} gid={ev.group_id}")
    await _process_request(bot, ev, "drink", forced_world="common")


@sv.on_keyword(("特产", "特饮"), prefix=False)
async def on_world_special(bot: Bot, ev: Event) -> None:
    msg: str = ev.raw_text.strip()
    category = "drink" if "特饮" in msg else "food"
    if any(k in msg for k in _ALL_CAT_KWS):
        logger.debug(f"[ChisaEating] 特产/特饮与吃喝词同现，交由对应处理器 | msg={msg!r}")
        return
    wv_settings = _get_wv_settings()
    alias_map = _build_alias_map(wv_settings)
    forced_world: Optional[str] = next(
        (wk for alias, wk in alias_map.items() if alias in msg), None
    )
    if forced_world is None:
        logger.debug(f"[ChisaEating] 特产/特饮：未匹配到世界别称，忽略 | msg={msg!r}")
        return
    logger.info(
        f"[ChisaEating] 世界特产/特饮 | world={forced_world} cat={category} uid={ev.user_id} gid={ev.group_id}"
    )
    await _process_request(bot, ev, category, forced_world=forced_world)


@sv.on_regex(r"(?:想和|召唤)(.+?)(?:吃饭|下厨)|(.+?)特供料理", prefix=False)
async def on_chef_summon(bot: Bot, ev: Event) -> None:
    chef_name: str = ""
    if ev.regex_group:
        for g in ev.regex_group:
            if g:
                chef_name = g.strip()
                break

    if not chef_name or chef_name == "黑暗":
        return

    logger.info(f"[ChisaEating] 召唤厨师下厨 | chef={chef_name} uid={ev.user_id} gid={ev.group_id}")
    await _process_request(bot, ev, "food", forced_chef=chef_name)


@sv.on_regex(r"^(?:/)?(?:进货|招募|黑魔法召唤|下载)\s*\[?([a-zA-Z]{2}\d{4})\]?$", prefix=False)
async def on_chisa_dlc_download(bot: Bot, ev: Event) -> None:
    if not _is_admin(ev):
        await bot.send("只有管理员才能操作商会进货哦！")
        return

    dlc_id = ev.regex_group[0].lower() if ev.regex_group else ""
    if not dlc_id:
        await bot.send("请输入正确的进货格式，例如：进货fd0001")
        return

    if _DOWNLOAD_STATE.is_downloading:
        await bot.send(f"📦 正在搬运资源中 ({_DOWNLOAD_STATE.percent:.1f}%)，请稍等当前任务完成后再试！")
        return

    catalog = _read_catalog_file()
    if not catalog:
        await bot.send("⚠️ 无法读取目录数据，请先发送【千小妹商会信息同步】进行拉取。")
        return

    target_item = next((item for item in catalog if str(item.get("id", "")).lower() == dlc_id), None)
    if not target_item:
        await bot.send(f"找不到编号为 {dlc_id} 的商品呢，老板是不是记错啦？")
        return

    expected_sha256 = str(target_item.get("sha256", "")).strip().lower()
    await bot.send(f"收到！千小妹这就去进货 [{dlc_id}]，请稍等片刻...")

    async def _do_dlc_download() -> None:
        try:
            node = await _get_optimal_dlc_node()
            original_url = f"https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo/releases/download/Chisa_Dlc_Store/{dlc_id}.zip"
            url = original_url if node in ("direct", "") else f"https://{node}/{original_url}"

            temp_zip = _get_base_dir() / f"temp_{dlc_id}.zip"
            actual_sha, _ = await _download_allowed_https_file(
                url,
                str(temp_zip),
                max_bytes=512 * 1024 * 1024,
                trust_env=(node in ("direct", "")),
                timeout_seconds=300,
            )

            if expected_sha256 and actual_sha != expected_sha256:
                if temp_zip.exists():
                    temp_zip.unlink()
                raise ValueError(f"哈希校验失败！预期 {expected_sha256[:8]}，实际 {actual_sha[:8]}")

            target_extract_dir = str(_get_base_dir())
            await asyncio.to_thread(_extract_dlc_zip_safe, str(temp_zip), target_extract_dir)
            if temp_zip.exists():
                temp_zip.unlink()

            json_path = _get_base_dir() / "Webui-PIC" / "Shop" / "index" / "downloaded.json"
            json_path.parent.mkdir(parents=True, exist_ok=True)
            downloaded = []
            if json_path.exists():
                try:
                    with open(json_path, "r", encoding="utf-8-sig") as jf:
                        downloaded = json.load(jf)
                except Exception:
                    downloaded = []
            if dlc_id not in downloaded:
                downloaded.append(dlc_id)
                with open(json_path, "w", encoding="utf-8") as jf:
                    json.dump(downloaded, jf, ensure_ascii=False)

            logger.info(f"[ChisaEating] DLC [{dlc_id}] 进货落库成功！")
            await bot.send(f"🎉 千小妹已经把 [{dlc_id}] 搬到后厨啦！快去点餐尝尝吧~")
        except Exception as exc:
            logger.error(f"[ChisaEating] 进货 [{dlc_id}] 失败: {exc}")
            await bot.send(f"❌ 进货 [{dlc_id}] 遭遇次元风暴: {exc}")

    asyncio.create_task(_do_dlc_download())


@sv.on_prefix(("加菜", "/加菜"), prefix=False)
async def on_add_food(bot: Bot, ev: Event) -> None:
    if not _is_admin(ev):
        await bot.send("【越权警告】只有厨师长（管理员）可以加菜哦！")
        return

    text_parts = ev.text.strip().split(maxsplit=2)
    if len(text_parts) < 3:
        await bot.send(
            "指令格式错误！\n"
            "正确格式：加菜 [世界] [分类] [菜名]\n"
            "分类支持：食物 / 饮品 / 黑暗料理\n"
            "示例：加菜 鸣潮 食物 肯德基肉霸堡 (请连带图片一起发送)"
        )
        return

    world_input, cat_input, food_name_input = text_parts[0].strip(), text_parts[1].strip(), text_parts[2].strip()

    target_world: Optional[str] = None
    if world_input in ("三次元", "现实", "common"):
        target_world = "common"
    else:
        wv_settings = _get_wv_settings()
        alias_map = _build_alias_map(wv_settings)
        for alias, wk in alias_map.items():
            if world_input == alias or world_input == wk:
                target_world = wk
                break

    if not target_world:
        await bot.send(f"加菜失败：未识别的世界 '{world_input}'，请填写鸣潮、原神、三次元或自定义世界。")
        return

    cat_map = {"食物": "food", "饮品": "drink", "黑暗料理": "darkfood"}
    target_cat = cat_map.get(cat_input)
    if not target_cat:
        await bot.send(f"加菜失败：未识别的分类 '{cat_input}'，只能是 食物、饮品 或 黑暗料理。")
        return

    for char in '<>:"/\\|?*':
        food_name_input = food_name_input.replace(char, "")
    food_name_input = food_name_input.strip()

    if not food_name_input:
        await bot.send("加菜失败：菜名不合法！")
        return

    images = await _extract_images_from_event(ev)
    if not images:
        await bot.send("加菜失败：没有检测到图片，请将图片和加菜指令在同一条消息中发出。")
        return

    target_dir = _image_mgr.user_data_dir / target_cat / target_world
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    for content, ext in images:
        save_path = target_dir / f"{food_name_input}{ext}"
        if save_path.exists():
            counter = 1
            while True:
                save_path = target_dir / f"{food_name_input}_{counter}{ext}"
                if not save_path.exists():
                    break
                counter += 1

        try:
            save_path.write_bytes(content)
            saved_count += 1
        except Exception as e:
            logger.error(f"[ChisaEating] 保存菜品图片失败: {e}")

    if saved_count > 0:
        await bot.send(f"✅ 加菜成功！\n共收录 {saved_count} 张【{food_name_input}】至 {world_input} 的 {cat_input} 库中！")
    else:
        await bot.send("加菜失败：未能成功保存图片。")


@sv.on_prefix(("上传厨师", "/上传厨师", "加大厨", "/加大厨"), prefix=False)
async def on_upload_chef(bot: Bot, ev: Event) -> None:
    if not _is_admin(ev):
        await bot.send("【越权警告】只有厨师长（管理员）可以上传厨师哦！")
        return

    chef_name = ev.text.strip()
    for char in '<>:"/\\|?*':
        chef_name = chef_name.replace(char, "")
    chef_name = chef_name.strip()

    if not chef_name:
        await bot.send(
            "指令格式错误！\n"
            "正确格式：上传厨师 [厨师名]\n"
            "示例：上传厨师 刻晴 (请连带立绘图片一起发送)"
        )
        return

    images = await _extract_images_from_event(ev)
    if not images:
        await bot.send("上传失败：没有检测到图片，请将厨师图片与指令在同一条消息中发出。")
        return

    target_dir = _image_mgr.user_data_dir / "chefs"
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    for content, ext in images:
        save_path = target_dir / f"{chef_name}{ext}"
        if save_path.exists():
            counter = 2
            while True:
                save_path = target_dir / f"{chef_name}_{counter}{ext}"
                if not save_path.exists():
                    break
                counter += 1

        try:
            save_path.write_bytes(content)
            saved_count += 1
        except Exception as e:
            logger.error(f"[ChisaEating] 保存厨师图片失败: {e}")

    if saved_count > 0:
        await bot.send(f"✅ 上传厨师成功！\n共收录 {saved_count} 张【{chef_name}】至大厨图库！")
    else:
        await bot.send("上传厨师失败：未能保存图片。")


@sv.on_fullmatch(("千小妹图库下载进度", "/千小妹图库下载进度"), prefix=False)
async def on_download_progress(bot: Bot, ev: Event) -> None:
    if _DOWNLOAD_STATE.is_downloading:
        await bot.send(
            f"【千小妹基础图库下载进度】\n"
            f"阶段：{_DOWNLOAD_STATE.stage or '正在搬运'}\n"
            f"当前已下载：{_DOWNLOAD_STATE.downloaded_mb:.2f} MB / {_DOWNLOAD_STATE.total_mb:.2f} MB ({_DOWNLOAD_STATE.percent:.1f}%)"
        )
    else:
        await bot.send("【千小妹提示】当前没有正在进行的图库下载任务哦。")


@sv.on_fullmatch(("更新千小妹图库", "/更新千小妹图库", "千小妹图库重建", "重建千小妹图库"), prefix=False)
async def on_update_assets(bot: Bot, ev: Event) -> None:
    if not _is_admin(ev):
        await bot.send("【权限不足】只有管理员才能执行图库更新与重建指令哦！")
        return

    if _DOWNLOAD_STATE.is_downloading:
        await bot.send("【千小妹提示】图库正在下载中，请勿重复触发...")
        return

    await bot.send("【千小妹提示】已收到指令！开始从远程拉取并更新完整基础图库，请稍候。随时可发送【千小妹图库下载进度】查看详情。")

    async def _start_download() -> None:
        ok, msg = await download_assets(_image_mgr.user_data_dir)
        if ok:
            await bot.send(f"✅ 千小妹图库拉取部署成功！\n{msg}")
        else:
            await bot.send(f"❌ 千小妹图库拉取失败：{msg}")

    asyncio.create_task(_start_download())


@sv.on_fullmatch(("千小妹速查", "/千小妹速查"), prefix=False)
async def on_quick_help(bot: Bot, ev: Event) -> None:
    quick_msg = (
        "📌 【千小妹速查表】\n\n"
        "🍔 基础功能\n"
        "· 吃什么 / 喝点啥\n"
        "· 来点现实的食物 / 鸣潮特产 / 原神特饮\n\n"
        "👑 进阶与整活\n"
        "· 来点黑暗料理\n"
        "· 召唤[某人]下厨 / [某人]特供料理\n\n"
        "🛒 商会系统 (需管理员)\n"
        "· 千小妹商会\n"
        "· 进货[编号] / 招募[编号] / 黑魔法召唤[编号]\n"
        "· 千小妹商会信息同步\n\n"
        "⚙️ 管理指令 (需管理员)\n"
        "· 更新千小妹图库\n"
        "· 千小妹图库下载进度\n"
        "· 查千小妹图库 (图库各分类统计)\n"
        "· 加菜 [世界] [分类] [菜名] (带图)\n"
        "· 上传厨师 [厨师名] (带图)"
    )
    try:
        await bot.send(MessageSegment.node([quick_msg]))
    except Exception:
        await bot.send(quick_msg)


@sv.on_fullmatch(("查千小妹图库", "千小妹图库统计", "/查千小妹图库"), prefix=False)
async def on_check_gallery(bot: Bot, ev: Event) -> None:
    base = _image_mgr.user_data_dir
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    def count_imgs(p: Path) -> int:
        if not p.exists():
            return 0
        return sum(1 for f in p.rglob("*") if f.is_file() and f.suffix.lower() in exts)

    food_count = count_imgs(base / "food")
    drink_count = count_imgs(base / "drink")
    dark_count = count_imgs(base / "darkfood")
    chef_count = count_imgs(base / "chefs")
    meme_count = count_imgs(base / "memes")

    gf_dir = base / "ganfanren"
    gf_names = [f.name for f in gf_dir.iterdir() if f.is_dir()] if gf_dir.exists() else []

    msg = (
        "📊 【千小妹图库资产统计】\n"
        f"🍔 菜品总数: {food_count} 张\n"
        f"🧋 饮品总数: {drink_count} 张\n"
        f"☠️ 黑暗料理: {dark_count} 张\n"
        f"👨‍🍳 厨师立绘: {chef_count} 张\n"
        f"🌸 导游表情: {meme_count} 张\n"
        f"🏃 干饭人成员: {len(gf_names)} 位 ({', '.join(gf_names[:6])}{'...' if len(gf_names) > 6 else ''})\n"
        f"📁 资源目录: {base}"
    )
    await bot.send(msg)


async def _process_request(
    bot: Bot,
    ev: Event,
    category: str,
    forced_world: Optional[str] = None,
    forced_chef: Optional[str] = None,
) -> None:
    msg: str = ev.raw_text.strip()
    uid: str = ev.user_id
    group_id: str = ev.group_id or ev.user_id
    gid_str: str = str(group_id)

    # 图库下载期间直接汇报进度，避免用户误以为插件卡死
    if _DOWNLOAD_STATE.is_downloading:
        logger.debug(f"[ChisaEating] 图库下载中，拦截点餐请求 uid={uid}")
        await bot.send(
            f"【千小妹下载进度】正在为你搬运跨次元美食资源\n"
            f"当前已下载 {_DOWNLOAD_STATE.downloaded_mb:.2f} MB / "
            f"{_DOWNLOAD_STATE.total_mb:.2f} MB（{_DOWNLOAD_STATE.percent:.1f}%）\n"
            f"下载完成后即可正常点菜"
        )
        return

    # 图库为空时引导主人拉取资源（对应上游 v3.5.1 行为）
    if not has_food_assets(_image_mgr.user_data_dir):
        logger.warning(f"[ChisaEating] 图库为空，提示拉取资源 uid={uid}")
        await bot.send(
            "【千小妹系统提示】检测到基础图库为空！\n"
            "请让机器人主人发送「更新千小妹图库」拉取图包，\n"
            f"或手动将 food 等文件夹放入\n{_image_mgr.user_data_dir}"
        )
        return

    # 黑白名单
    if CHISA_CONFIG.get_config("enable_blacklist").data:
        blacklist: List[str] = [
            s.strip()
            for s in CHISA_CONFIG.get_config("blacklist_groups").data
            if s.strip()
        ]
        if gid_str in blacklist:
            logger.debug(f"[ChisaEating] 群 {gid_str} 在黑名单，跳过")
            return

    if CHISA_CONFIG.get_config("enable_whitelist").data:
        whitelist: List[str] = [
            s.strip()
            for s in CHISA_CONFIG.get_config("whitelist_groups").data
            if s.strip()
        ]
        if gid_str not in whitelist:
            logger.debug(f"[ChisaEating] 群 {gid_str} 不在白名单，跳过")
            return

    wv_settings: Dict[str, WorldConf] = _get_wv_settings()

    # 未由调用方指定世界时，从消息别称自动检测
    if forced_world is None:
        alias_map: Dict[str, str] = _build_alias_map(wv_settings)
        for alias, wk in alias_map.items():
            if alias in msg:
                forced_world = wk
                logger.debug(f"[ChisaEating] 别称匹配 alias={alias!r} -> world={wk}")
                break

    forced_chef = _extract_forced_chef(msg)

    active_key: str = (
        forced_world
        if (forced_world is not None and forced_world != "common")
        else _resolve_active_key()
    )
    active_conf: WorldConf = wv_settings[active_key]
    active_phrases: WorldPhrasesData = _WORLD_PHRASES[active_key]

    bot_pool: List[str] = active_conf["自称池"]
    bot_name: str = random.choice(bot_pool) if bot_pool else "推荐官"

    world_name: str = active_conf["名称"]
    world_aliases: List[str] = [a for a in active_conf["别称"] if a]
    if world_aliases:
        world_name = random.choice([world_name] + world_aliases)

    config_snap: ConfigSnapshot = _build_config_snapshot()

    # 防刷屏
    if _rate_limiter.is_spaming(uid, config_snap["spam_threshold"]):
        logger.debug(f"[ChisaEating] 触发防刷屏 uid={uid}")
        if random.randint(1, 100) <= config_snap["interception_egg_chance"]:
            inter_text = "【拦截警报】你点得太快啦！千咲怕你撑着，已经先你一步把厨房吃空了！"
            meme_file: Optional[str] = _image_mgr.get_egg_meme("千咲")
        else:
            inter_pool: List[str] = active_phrases["打断句式"]
            inter_text = random.choice(inter_pool).format(bot=bot_name)
            meme_file = _image_mgr.get_bot_meme(active_key, "speechless")
        segs = [MessageSegment.text(inter_text)]
        if meme_file is not None:
            segs.append(MessageSegment.image(Path(meme_file)))
        await bot.send(segs)
        return

    # 摆烂复读
    if (
        forced_chef is None
        and not _rate_limiter.is_repeat_in_cooldown(gid_str, config_snap["repeat_cooldown"])
        and random.randint(1, 100) <= config_snap["repeat_prob"]
    ):
        _rate_limiter.record_repeat_trigger(gid_str)
        logger.debug(f"[ChisaEating] 触发摆烂复读 gid={gid_str}")
        fallback_pool: List[str] = CHISA_CONFIG.get_config("generic_templates").data
        pool_text_fb: List[str] = (
            fallback_pool if fallback_pool else ["是啊，{food}好像都不错"]
        )
        text: str = random.choice(pool_text_fb).format(bot=bot_name, food="什么")
        repeat_meme: Optional[str] = _image_mgr.get_bot_meme(active_key, "think")
        segs = [MessageSegment.text(text)]
        if repeat_meme is not None:
            segs.append(MessageSegment.image(Path(repeat_meme)))
        await bot.send(segs)
        return

    # 扫描卡池
    pool: List[PoolItem] = _image_mgr.scan_all_items(wv_settings, category)

    # 混入三次元文字池
    common_texts: List[str]
    if category == "food":
        common_texts = CHISA_CONFIG.get_config("common_food_text").data
    elif category == "drink":
        common_texts = CHISA_CONFIG.get_config("common_drink_text").data
    else:
        common_texts = []

    for text_item in common_texts:
        name: str = text_item.strip()
        if name:
            pool.append(
                PoolItem(
                    wv="common",
                    food=name,
                    raw_name=name,
                    chef="none",
                    has_image=False,
                    path=None,
                )
            )

    logger.debug(
        f"[ChisaEating] 卡池扫描完毕 size={len(pool)} "
        f"category={category} forced_world={forced_world}"
    )

    # 强制世界过滤
    if forced_world is not None:
        pool = [item for item in pool if item["wv"] == forced_world]
    if forced_chef is not None:
        chef_pool = [item for item in pool if item["chef"] == forced_chef]
        if not chef_pool:
            await bot.send(f"【千小妹提示】没有找到厨师“{forced_chef}”的可用料理。")
            return
        pool = chef_pool

    # 强制厨师过滤
    if forced_chef is not None:
        chef_pool: List[PoolItem] = [item for item in pool if item.get("chef", "").lower() == forced_chef.lower()]
        if not chef_pool:
            drink_pool = _image_mgr.scan_all_items(wv_settings, "drink")
            chef_pool = [item for item in drink_pool if item.get("chef", "").lower() == forced_chef.lower()]
        if not chef_pool:
            await bot.send(f"【厨师下班】{forced_chef}今天不在后厨哦～（图库中未找到该厨师的菜品）")
            return
        pool = chef_pool

    if not pool:
        logger.warning(
            f"[ChisaEating] 卡池为空 category={category} forced_world={forced_world}"
        )
        await bot.send("【卡池告急】未找到可用的食物/饮品数据！请检查资源目录或配置。")
        return

    picked: Optional[PoolItem] = _data_mgr.filter_and_pick(
        gid_str, pool, active_key, config_snap
    )

    if picked is None:
        logger.warning(
            f"[ChisaEating] filter_and_pick 返回空 "
            f"category={category} forced_world={forced_world}"
        )
        await bot.send("【卡池告急】未找到可用的食物/饮品数据！请检查资源目录或配置。")
        return

    food_name: str = picked["food"]
    chef_name: str = picked["chef"]
    origin_key: str = picked["wv"]
    full_food_desc: str = (
        f"由【{chef_name}】特制的{food_name}" if chef_name != "none" else food_name
    )

    logger.info(
        f"[ChisaEating] 推荐 food={food_name!r} chef={chef_name!r} "
        f"origin={origin_key} img={picked['has_image']}"
    )

    fmt_args: Dict[str, str] = {
        "bot": bot_name,
        "bot_a": bot_name,
        "food": food_name,
        "chef": chef_name,
        "full_food_desc": full_food_desc,
        "world_a": world_name,
    }

    is_crossover: bool = origin_key != "common" and origin_key != active_key
    mood: str = "like"
    final_text: str

    if category == "dark":
        dark_tpls: List[str] = CHISA_CONFIG.get_config("dark_templates").data
        pool_text: List[str] = (
            dark_tpls if dark_tpls else ["这{full_food_desc}……{bot}已经在害怕了。"]
        )
        final_text = random.choice(pool_text).format(**fmt_args)
        mood = "scared"
    elif is_crossover:
        cross_conf: WorldConf = wv_settings[origin_key]
        world_b: str = cross_conf["名称"]
        world_b_aliases: List[str] = [a for a in cross_conf["别称"] if a]
        if world_b_aliases:
            world_b = random.choice([world_b] + world_b_aliases)
        fmt_args["world_b"] = world_b
        bot_b_pool: List[str] = cross_conf["自称池"]
        fmt_args["bot_b"] = random.choice(bot_b_pool) if bot_b_pool else "异界人"
        cross_tpls: List[str] = CHISA_CONFIG.get_config("crossover_templates").data
        cross_pool: List[str] = (
            cross_tpls
            if cross_tpls
            else ["{bot_a}和{bot_b}一起分享了{full_food_desc}！"]
        )
        final_text = random.choice(cross_pool).format(**fmt_args)
    elif chef_name != "none":
        chef_phrases: List[str] = active_phrases["厨师句式"]
        final_text = random.choice(chef_phrases).format(**fmt_args)
    elif origin_key == "common":
        generic_tpls: List[str] = CHISA_CONFIG.get_config("generic_templates").data
        generic_pool: List[str] = (
            generic_tpls if generic_tpls else ["铛铛！为你抽中了{food}！"]
        )
        final_text = random.choice(generic_pool).format(**fmt_args)
    else:
        spec_phrases: List[str] = active_phrases["专属句式"]
        generic_tpls2: List[str] = CHISA_CONFIG.get_config("generic_templates").data
        generic_fallback: List[str] = (
            generic_tpls2 if generic_tpls2 else ["铛铛！为你抽中了{food}！"]
        )
        combined: List[str] = spec_phrases + generic_fallback
        final_text = random.choice(combined).format(**fmt_args)

    # 图片配装
    img_to_send: Optional[str] = picked["path"] if picked["has_image"] else None
    meme_to_send: Optional[str] = None

    if random.randint(1, 100) <= config_snap["egg_prob"]:
        ganfanren_pool: Dict[str, GanfanrenData] = _image_mgr.get_ganfanren_data()
        if ganfanren_pool:
            egg_pool_cfg: str = config_snap["egg_pool"]
            if egg_pool_cfg.strip() and egg_pool_cfg.strip().lower() != "random":
                cleaned: str = egg_pool_cfg.replace("；", ";")
                allowed: List[str] = [
                    n.strip() for n in cleaned.split(";") if n.strip()
                ]
                valid: List[str] = [n for n in allowed if n in ganfanren_pool]
                if not valid:
                    valid = list(ganfanren_pool.keys())
            else:
                valid = list(ganfanren_pool.keys())
            lucky_name: str = random.choice(valid)
            meme_to_send = random.choice(ganfanren_pool[lucky_name]["images"])
            words_list: List[str] = ganfanren_pool[lucky_name]["words"]
            word: str = (
                random.choice(words_list)
                if words_list
                else "但是所有食物被一个神秘吃货一扫而空！"
            )
            final_text += f"\n\n{word}"
        else:
            final_text += "\n\n但是所有食物被一个神秘吃货一扫而空！"
    else:
        if chef_name != "none" and random.randint(1, 100) <= config_snap["chef_meme_prob"]:
            meme_to_send = _image_mgr.get_chef_image(chef_name)
        elif random.randint(1, 100) <= config_snap["global_meme_prob"]:
            meme_to_send = _image_mgr.get_bot_meme(active_key, mood)

    segs = [MessageSegment.text(final_text)]
    if img_to_send is not None:
        segs.append(MessageSegment.image(Path(img_to_send)))
    if meme_to_send is not None:
        segs.append(MessageSegment.image(Path(meme_to_send)))
    await bot.send(segs)
