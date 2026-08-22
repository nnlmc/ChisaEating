import asyncio
import random
import re
from pathlib import Path
from typing import Dict, List, Optional, TypedDict

import httpx
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment
from gsuid_core.server import on_core_start
from gsuid_core.sv import SV

from ..chisaeating_config import CHISA_CONFIG
from ..utils.downloader import (
    check_needs_download,
    clean_legacy_bundled_resources,
    download_and_extract_assets,
    is_resource_downloading,
)
from ..utils.food_data import FoodDataManager
from ..utils.image_manager import ImageManager
from ..utils.rate_limiter import RateLimiter
from ..utils.resource.RESOURCE_PATH import CHEFS_PATH, DATA_PATH

sv = SV("千小妹还在吃", pm=6, area="ALL")

_plugin_root = Path(__file__).parent.parent.parent
_image_mgr = ImageManager()
_data_mgr = FoodDataManager()
_rate_limiter = RateLimiter()

_EAT_KWS = ("吃什么", "吃啥", "吃点儿啥", "吃点啥")
_DRINK_KWS = ("喝什么", "喝啥", "喝点儿啥", "喝点啥")
_DARK_KWS = ("来点黑暗料理", "黑暗料理")
_COMMON_EAT_KWS = ("来点现实的食物", "来点三次元食物")
_COMMON_DRINK_KWS = ("来点现实的饮品", "来点三次元饮品")
_ALL_CAT_KWS = _EAT_KWS + _DRINK_KWS + _DARK_KWS + _COMMON_EAT_KWS + _COMMON_DRINK_KWS


@on_core_start
async def _init_resources() -> None:
    clean_legacy_bundled_resources(_plugin_root)
    if check_needs_download():
        logger.info("[ChisaEating] 检测到未下载基础图库，将在后台开始拉取...")
        asyncio.create_task(download_and_extract_assets())


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
            "嗝~{bot}揉了揉圆滚滚的肚子，表示真的塞不下更多的菜了",
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
}


def _get_wv_settings() -> Dict[str, WorldConf]:
    result: Dict[str, WorldConf] = {}
    for i in range(1, 5):
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
    sel: str = CHISA_CONFIG.get_config("active_world").data
    if sel in ("world1", "world2", "world3", "world4"):
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
        interception_egg_chance=CHISA_CONFIG.get_config(
            "interception_egg_chance"
        ).data,
    )


@sv.on_fullmatch("吃什么强制下载所有资源", prefix=False)
async def on_force_download(bot: Bot, ev: Event) -> None:
    if is_resource_downloading():
        await bot.send("【千小妹提示】图库资源当前正在下载中，请稍候...")
        return

    async def _send_progress(msg: str) -> None:
        await bot.send(msg)

    success, reply = await download_and_extract_assets(
        progress_callback=_send_progress
    )
    if not success:
        await bot.send(f"❌ {reply}")


@sv.on_prefix(("加菜", "/加菜"), prefix=False)
async def on_add_food(bot: Bot, ev: Event) -> None:
    if ev.user_pm > 2:
        await bot.send("【越权警告】只有厨师长（管理员）才可以加菜哦！")
        return

    text = ev.text.strip()
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await bot.send(
            "指令格式错误！\n"
            "正确格式：加菜 [世界] [分类] [菜名]\n"
            "示例：加菜 鸣潮 食物 冰吸生椰拿铁 (请连带图片一起发送)"
        )
        return

    world_input, cat_input, food_name_input = parts[0], parts[1], parts[2]
    cat_map = {"食物": "food", "饮品": "drink", "黑暗料理": "darkfood"}
    target_cat = cat_map.get(cat_input)
    if not target_cat:
        await bot.send(
            f"加菜失败：未识别的分类 '{cat_input}'，只能是 食物、饮品 或 黑暗料理。"
        )
        return

    wv_settings = _get_wv_settings()
    alias_map = _build_alias_map(wv_settings)
    target_world = None
    if world_input in ("三次元", "现实", "common"):
        target_world = "common"
    else:
        for alias, wk in alias_map.items():
            if world_input == alias or world_input == wk:
                target_world = wk
                break

    if not target_world:
        await bot.send(f"加菜失败：未识别的世界 '{world_input}'。")
        return

    for char in '<>:"/\\|?*':
        food_name_input = food_name_input.replace(char, "")
    food_name_input = food_name_input.strip()
    if not food_name_input:
        await bot.send("加菜失败：菜名不合法！")
        return

    img_urls = _extract_image_urls(ev)
    if not img_urls:
        await bot.send(
            "加菜失败：没有检测到图片，请将图片和指令在同一条消息中发出。"
        )
        return

    target_dir = DATA_PATH / target_cat / target_world
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in img_urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    ext = ".jpg"
                    if "png" in url.lower():
                        ext = ".png"
                    elif "gif" in url.lower():
                        ext = ".gif"
                    elif "webp" in url.lower():
                        ext = ".webp"

                    save_path = target_dir / f"{food_name_input}{ext}"
                    counter = 1
                    while save_path.exists():
                        save_path = (
                            target_dir / f"{food_name_input}_{counter}{ext}"
                        )
                        counter += 1

                    save_path.write_bytes(resp.content)
                    saved_count += 1
            except Exception as e:
                logger.error(f"[ChisaEating] 加菜下载图片失败: {e}")

    if saved_count > 0:
        await bot.send(
            f"✅ 加菜成功！\n共收录 {saved_count} 张【{food_name_input}】至 {world_input} 的 {cat_input} 库中！"
        )
    else:
        await bot.send("加菜失败：图片下载失败或平台限制。")


@sv.on_prefix(("上传厨师", "/上传厨师"), prefix=False)
async def on_upload_chef(bot: Bot, ev: Event) -> None:
    if ev.user_pm > 2:
        await bot.send("【越权警告】只有厨师长（管理员）才可以上传厨师哦！")
        return

    chef_name = ev.text.strip()
    if not chef_name:
        await bot.send(
            "指令格式错误！\n"
            "正确格式：上传厨师 [厨师名]\n"
            "示例：上传厨师 奥黛塔 (请连带图片一起发送)"
        )
        return

    for char in '<>:"/\\|?*':
        chef_name = chef_name.replace(char, "")
    chef_name = chef_name.strip()
    if not chef_name:
        await bot.send("上传失败：厨师名不合法！")
        return

    img_urls = _extract_image_urls(ev)
    if not img_urls:
        await bot.send(
            "上传失败：没有检测到图片，请将图片和指令在同一条消息中发出。"
        )
        return

    CHEFS_PATH.mkdir(parents=True, exist_ok=True)
    saved_count = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in img_urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    ext = ".jpg"
                    if "png" in url.lower():
                        ext = ".png"
                    elif "gif" in url.lower():
                        ext = ".gif"
                    elif "webp" in url.lower():
                        ext = ".webp"

                    save_path = CHEFS_PATH / f"{chef_name}{ext}"
                    counter = 1
                    while save_path.exists():
                        save_path = CHEFS_PATH / f"{chef_name}_{counter}{ext}"
                        counter += 1

                    save_path.write_bytes(resp.content)
                    saved_count += 1
            except Exception as e:
                logger.error(f"[ChisaEating] 上传厨师图片下载失败: {e}")

    if saved_count > 0:
        await bot.send(
            f"✅ 上传厨师成功！\n共收录 {saved_count} 张【{chef_name}】至厨师图鉴！"
        )
    else:
        await bot.send("上传厨师失败：图片下载失败或平台限制。")


def _extract_image_urls(ev: Event) -> List[str]:
    urls: List[str] = []
    if isinstance(ev.msg, list):
        for seg in ev.msg:
            if isinstance(seg, MessageSegment) and seg.type == "image":
                data = seg.data.get("url") or seg.data.get("file") or ""
                if str(data).startswith("http"):
                    urls.append(str(data))
            elif isinstance(seg, dict) and seg.get("type") == "image":
                data = (
                    seg.get("data", {}).get("url")
                    or seg.get("data", {}).get("file")
                    or ""
                )
                if str(data).startswith("http"):
                    urls.append(str(data))
    return urls


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
    logger.info(
        f"[ChisaEating] 点餐(黑暗料理) | uid={ev.user_id} gid={ev.group_id}"
    )
    await _process_request(bot, ev, "dark")


@sv.on_keyword(_COMMON_EAT_KWS, prefix=False)
async def on_common_eat(bot: Bot, ev: Event) -> None:
    logger.info(
        f"[ChisaEating] 点餐(三次元食) | uid={ev.user_id} gid={ev.group_id}"
    )
    await _process_request(bot, ev, "food", forced_world="common")


@sv.on_keyword(_COMMON_DRINK_KWS, prefix=False)
async def on_common_drink(bot: Bot, ev: Event) -> None:
    logger.info(
        f"[ChisaEating] 点餐(三次元饮) | uid={ev.user_id} gid={ev.group_id}"
    )
    await _process_request(bot, ev, "drink", forced_world="common")


@sv.on_keyword(("特产", "特饮"), prefix=False)
async def on_world_special(bot: Bot, ev: Event) -> None:
    msg: str = ev.raw_text.strip()
    if any(k in msg for k in _ALL_CAT_KWS):
        return
    wv_settings = _get_wv_settings()
    alias_map = _build_alias_map(wv_settings)
    forced_world: Optional[str] = next(
        (wk for alias, wk in alias_map.items() if alias in msg), None
    )
    if forced_world is None:
        return
    category = "drink" if "特饮" in msg or "喝" in msg else "food"
    logger.info(
        f"[ChisaEating] 世界特产/特饮 | world={forced_world} cat={category} uid={ev.user_id} gid={ev.group_id}"
    )
    await _process_request(bot, ev, category, forced_world=forced_world)


async def _process_request(
    bot: Bot,
    ev: Event,
    category: str,
    forced_world: Optional[str] = None,
) -> None:
    if is_resource_downloading():
        await bot.send(
            "【千小妹提示】图库资源正在从镜像节点加速下载中，请稍候片刻再试..."
        )
        return

    if check_needs_download():
        async def _notify_progress(msg: str) -> None:
            await bot.send(msg)

        asyncio.create_task(
            download_and_extract_assets(progress_callback=_notify_progress)
        )
        return

    msg: str = ev.raw_text.strip()
    uid: str = ev.user_id
    group_id: str = ev.group_id or ev.user_id
    gid_str: str = str(group_id)

    if CHISA_CONFIG.get_config("enable_blacklist").data:
        blacklist: List[str] = [
            s.strip()
            for s in CHISA_CONFIG.get_config("blacklist_groups").data
            if s.strip()
        ]
        if gid_str in blacklist:
            return

    if CHISA_CONFIG.get_config("enable_whitelist").data:
        whitelist: List[str] = [
            s.strip()
            for s in CHISA_CONFIG.get_config("whitelist_groups").data
            if s.strip()
        ]
        if gid_str not in whitelist:
            return

    wv_settings: Dict[str, WorldConf] = _get_wv_settings()

    forced_chef: Optional[str] = None
    chef_match = re.search(
        r"想和(.+?)吃饭|召唤(.+)|(.+?)料理|(.+?)特供", msg
    )
    if chef_match:
        extracted = next((g for g in chef_match.groups() if g), None)
        if extracted and extracted != "黑暗":
            forced_chef = extracted.strip()

    if forced_world is None:
        alias_map: Dict[str, str] = _build_alias_map(wv_settings)
        for alias, wk in alias_map.items():
            if alias in msg:
                forced_world = wk
                break

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

    if _rate_limiter.is_spaming(uid, config_snap["spam_threshold"]):
        if random.randint(1, 100) <= config_snap["interception_egg_chance"]:
            ganfanren_pool = _image_mgr.get_ganfanren_data()
            if ganfanren_pool:
                valid_names = list(ganfanren_pool.keys())
                egg_role = (
                    "千咲" if "千咲" in valid_names else random.choice(valid_names)
                )
                inter_text = f"【拦截警报】你点得太快啦！{egg_role}怕你撑着，已经先你一步把厨房吃空了！"
                meme_file: Optional[str] = _image_mgr.get_egg_meme(egg_role)
            else:
                inter_text = "【拦截警报】你点得太快啦！系统已开启防刷屏管制！"
                meme_file = None
        else:
            inter_pool: List[str] = active_phrases["打断句式"]
            inter_text = random.choice(inter_pool).format(bot=bot_name)
            meme_file = _image_mgr.get_bot_meme(active_key, "speechless")
        segs = [MessageSegment.text(inter_text)]
        if meme_file is not None:
            segs.append(MessageSegment.image(Path(meme_file)))
        await bot.send(segs)
        return

    if (
        not _rate_limiter.is_repeat_in_cooldown(
            gid_str, config_snap["repeat_cooldown"]
        )
        and random.randint(1, 100) <= config_snap["repeat_prob"]
    ):
        _rate_limiter.record_repeat_trigger(gid_str)
        fallback_pool: List[str] = CHISA_CONFIG.get_config(
            "generic_templates"
        ).data
        pool_text_fb: List[str] = (
            fallback_pool if fallback_pool else ["是啊，{food}好像都不错"]
        )
        text: str = random.choice(pool_text_fb).format(
            bot=bot_name, food="什么"
        )
        repeat_meme: Optional[str] = _image_mgr.get_bot_meme(
            active_key, "think"
        )
        segs = [MessageSegment.text(text)]
        if repeat_meme is not None:
            segs.append(MessageSegment.image(Path(repeat_meme)))
        await bot.send(segs)
        return

    pool: List[PoolItem] = _image_mgr.scan_all_items(wv_settings, category)

    common_texts: List[str]
    if category == "food":
        common_texts = CHISA_CONFIG.get_config("common_food_text").data
    elif category == "drink":
        common_texts = CHISA_CONFIG.get_config("common_drink_text").data
    else:
        common_texts = []

    for text_item in common_texts:
        name: str = str(text_item).strip()
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

    if forced_world is not None:
        strict: List[PoolItem] = [
            item for item in pool if item["wv"] == forced_world
        ]
        if strict:
            pool = strict

    if forced_chef is not None:
        chef_pool = [item for item in pool if item.get("chef") == forced_chef]
        if not chef_pool and category == "food":
            drink_pool = _image_mgr.scan_all_items(wv_settings, "drink")
            chef_pool = [
                item for item in drink_pool if item.get("chef") == forced_chef
            ]
        if not chef_pool:
            await bot.send(
                f"【厨师下班】{forced_chef}今天不在厨房哦～（图库中未找到该厨师的作品）"
            )
            return
        pool = chef_pool

    if not pool:
        await bot.send(
            "【卡池告急】未找到可用的食物/饮品数据！请使用「吃什么强制下载所有资源」或检查配置。"
        )
        return

    picked: Optional[PoolItem] = _data_mgr.filter_and_pick(
        gid_str, pool, active_key, config_snap
    )

    if picked is None:
        await bot.send(
            "【卡池告急】未找到可用的食物/饮品数据！请检查资源目录或配置。"
        )
        return

    food_name: str = picked["food"]
    chef_name: str = picked["chef"]
    origin_key: str = picked["wv"]
    full_food_desc: str = (
        f"由【{chef_name}】特制的{food_name}"
        if chef_name != "none"
        else food_name
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
            dark_tpls
            if dark_tpls
            else ["这{full_food_desc}……{bot}已经在害怕了。"]
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
        fmt_args["bot_b"] = (
            random.choice(bot_b_pool) if bot_b_pool else "异界人"
        )
        cross_tpls: List[str] = CHISA_CONFIG.get_config(
            "crossover_templates"
        ).data
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
        generic_tpls: List[str] = CHISA_CONFIG.get_config(
            "generic_templates"
        ).data
        generic_pool: List[str] = (
            generic_tpls if generic_tpls else ["铛铛！为你抽中了{food}！"]
        )
        final_text = random.choice(generic_pool).format(**fmt_args)
    else:
        spec_phrases: List[str] = active_phrases["专属句式"]
        generic_tpls2: List[str] = CHISA_CONFIG.get_config(
            "generic_templates"
        ).data
        generic_fallback: List[str] = (
            generic_tpls2 if generic_tpls2 else ["铛铛！为你抽中了{food}！"]
        )
        combined: List[str] = spec_phrases + generic_fallback
        final_text = random.choice(combined).format(**fmt_args)

    if any(word in food_name for word in ["冰", "冷", "冻", "雪糕"]):
        final_text = final_text.replace("热腾腾的", "冰凉的").replace(
            "趁热吃吧", "趁凉吃吧"
        )

    img_to_send: Optional[str] = (
        picked["path"] if picked["has_image"] else None
    )
    meme_to_send: Optional[str] = None

    if random.randint(1, 100) <= config_snap["egg_prob"]:
        ganfanren_pool: Dict[str, dict] = _image_mgr.get_ganfanren_data()
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
        if (
            chef_name != "none"
            and random.randint(1, 100) <= config_snap["chef_meme_prob"]
        ):
            meme_to_send = _image_mgr.get_chef_image(chef_name)
        elif random.randint(1, 100) <= config_snap["global_meme_prob"]:
            meme_to_send = _image_mgr.get_bot_meme(active_key, mood)

    segs = [MessageSegment.text(final_text)]
    if img_to_send is not None:
        segs.append(MessageSegment.image(Path(img_to_send)))
    if meme_to_send is not None:
        segs.append(MessageSegment.image(Path(meme_to_send)))
    await bot.send(segs)
