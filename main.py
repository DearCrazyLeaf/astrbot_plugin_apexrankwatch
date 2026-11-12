from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
except Exception:
    Image = ImageDraw = ImageFilter = ImageFont = ImageOps = None

if __package__:
    from .apex_service import (
        ApexApiClient,
        ApexApiError,
        ApexPlayerStats,
        DailyMapPoolState,
        DailyMapScheduleInfo,
        MapRotationEntry,
        MapRotationInfo,
        MapScheduleEntry,
        PlayerNotFoundError,
        PredatorInfo,
        PredatorPlatformStats,
        SeasonInfo,
        format_schedule_remaining,
        is_likely_season_reset,
        is_score_drop_abnormal,
        normalize_platform,
    )
    from .push_archive import PushArchive
    from .score_report_rank_assets import rank_asset_file_for_label
    from .score_report_renderer import (
        configure_font_paths as configure_score_report_font_paths,
    )
    from .score_report_renderer import (
        render_dashboard_image_png,
    )
    from .storage import (
        SCORE_HISTORY_LIMIT,
        WATCH_MODE_NOTIFY,
        WATCH_MODE_RECORD,
        GroupStore,
        PlayerRecord,
        ScoreChangeRecord,
        normalize_watch_mode,
        trim_score_history,
    )
    from .utils import SHANGHAI_TZ, coerce_bool, coerce_int, now_epoch_ms, now_str
else:
    from apex_service import (
        ApexApiClient,
        ApexApiError,
        ApexPlayerStats,
        DailyMapPoolState,
        DailyMapScheduleInfo,
        MapRotationEntry,
        MapRotationInfo,
        MapScheduleEntry,
        PlayerNotFoundError,
        PredatorInfo,
        PredatorPlatformStats,
        SeasonInfo,
        format_schedule_remaining,
        is_likely_season_reset,
        is_score_drop_abnormal,
        normalize_platform,
    )
    from push_archive import PushArchive
    from score_report_rank_assets import rank_asset_file_for_label
    from score_report_renderer import (
        configure_font_paths as configure_score_report_font_paths,
    )
    from score_report_renderer import (
        render_dashboard_image_png,
    )
    from storage import (
        SCORE_HISTORY_LIMIT,
        WATCH_MODE_NOTIFY,
        WATCH_MODE_RECORD,
        GroupStore,
        PlayerRecord,
        ScoreChangeRecord,
        normalize_watch_mode,
        trim_score_history,
    )
    from utils import SHANGHAI_TZ, coerce_bool, coerce_int, now_epoch_ms, now_str


def _split_csv(raw: str, lowercase: bool = False) -> set[str]:
    if not raw:
        return set()
    normalized = str(raw).replace("，", ",")
    items = set()
    for item in normalized.split(","):
        text = item.strip()
        if not text:
            continue
        items.add(text.lower() if lowercase else text)
    return items


@dataclass
class PluginConfig:
    api_key: str
    debug_logging: bool
    check_interval: int
    timeout_ms: int
    max_retries: int
    min_valid_score: int
    blacklist: str
    query_blocklist: str
    user_blacklist: str
    owner_qq: str
    whitelist_enabled: bool
    whitelist_groups: str
    allow_private: bool
    data_dir: str
    font_auto_download: bool
    font_download_url: str
    output_mode: str
    alias_enabled: bool
    player_aliases: str
    alias_admin_only: bool
    season_probe_player: str
    reply_to_command: bool

    @staticmethod
    def from_raw(raw) -> "PluginConfig":
        output_mode = str(raw.get("output_mode", "image")).strip().lower()
        if output_mode not in {"image", "text"}:
            output_mode = "image"
        return PluginConfig(
            api_key=str(raw.get("api_key", "")).strip(),
            season_probe_player=str(raw.get("season_probe_player", "")).strip(),
            debug_logging=coerce_bool(raw.get("debug_logging", False), False),
            check_interval=max(1, coerce_int(raw.get("check_interval", 2), 2)),
            timeout_ms=max(1000, coerce_int(raw.get("timeout_ms", 10000), 10000)),
            max_retries=max(0, coerce_int(raw.get("max_retries", 3), 3)),
            min_valid_score=max(0, coerce_int(raw.get("min_valid_score", 1), 1)),
            blacklist=str(raw.get("blacklist", "")).strip(),
            query_blocklist=str(raw.get("query_blocklist", "")).strip(),
            user_blacklist=str(raw.get("user_blacklist", "")).strip(),
            owner_qq=str(raw.get("owner_qq", "")).strip(),
            whitelist_enabled=coerce_bool(raw.get("whitelist_enabled", False), False),
            whitelist_groups=str(raw.get("whitelist_groups", "")).strip(),
            allow_private=coerce_bool(raw.get("allow_private", True), True),
            data_dir=str(raw.get("data_dir", "")).strip(),
            font_auto_download=coerce_bool(raw.get("font_auto_download", True), True),
            font_download_url=str(raw.get("font_download_url", "")).strip(),
            output_mode=output_mode,
            alias_enabled=coerce_bool(raw.get("alias_enabled", True), True),
            player_aliases=str(raw.get("player_aliases", "")).strip(),
            alias_admin_only=coerce_bool(raw.get("alias_admin_only", True), True),
            reply_to_command=coerce_bool(raw.get("reply_to_command", True), True),
        )


@dataclass(frozen=True)
class FontStatus:
    available: bool
    source: str
    path: Path | None


@dataclass
class PollFetchResult:
    group_id: str
    group_origin: str
    player: PlayerRecord
    platform: str
    group_origins: tuple[str, ...] = ()
    session_modes: tuple[tuple[str, str], ...] = ()
    player_data: ApexPlayerStats | None = None
    not_found: bool = False
    error: Exception | None = None


@dataclass(frozen=True)
class PlayerAliasEntry:
    key: str
    display_name: str
    target: str


@dataclass(frozen=True)
class PlayerAliasResolution:
    requested_name: str
    player_name: str
    platform: str
    alias_key: str = ""
    alias_display: str = ""
    alias_target: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.alias_key)


class Main(Star):
    _PLUGIN_ROOT = Path(__file__).resolve().parent
    _MAP_ASSET_DIR = _PLUGIN_ROOT / "assets" / "maps"
    _RANK_ICON_DIR = _PLUGIN_ROOT / "assets" / "ranks"
    _LEGEND_ICON_DIR = _PLUGIN_ROOT / "assets" / "legends"
    _STATUS_BADGE_DIR = _PLUGIN_ROOT / "assets" / "status"
    _DEFAULT_USER_AVATAR_PATH = _PLUGIN_ROOT / "assets" / "default_user_avatar.png"
    _PREDATOR_TEMPLATE_PATH = _PLUGIN_ROOT / "assets" / "predator_template.png"
    _MAP_CARD_SIZE = (900, 320)
    _MAP_CURRENT_HEIGHT = 212
    _MAP_IMAGE_CACHE_TTL_SECONDS = 60
    _MAP_IMAGE_CACHE_MAX_ENTRIES = 32
    _SEASON_CARD_SIZE = (840, 360)
    _SEASON_CARD_RENDERER_VERSION = 4
    _SEASON_IMAGE_CACHE_TTL_SECONDS = 60
    _SEASON_IMAGE_CACHE_MAX_ENTRIES = 16
    _RANK_CHANGE_CARD_SIZE = (1122, 1402)
    _PLAYER_RANK_CARD_SIZE = (1122, 1402)
    _HELP_CARD_SIZE = (1122, 2380)
    _MONITOR_ADDED_CARD_SIZE = (1122, 1040)
    _MONITOR_LIST_CARD_WIDTH = 1122
    _MONITOR_LIST_ROW_LIMIT = 8
    _SCORE_CHANGE_DEFAULT_LIMIT = 20
    _SCORE_CHANGE_MAX_LIMIT = SCORE_HISTORY_LIMIT
    _SCORE_CHANGE_CHART_WIDTH = 1920
    _PREDATOR_IMAGE_CACHE_TTL_SECONDS = 60
    _PREDATOR_IMAGE_CACHE_MAX_ENTRIES = 16
    _PREDATOR_GREEN = (88, 210, 126, 255)
    _PREDATOR_DEEP_RED = (158, 31, 36, 255)
    _PREDATOR_NEUTRAL = (246, 241, 232, 255)
    _DAILY_MAP_LEARNING_INTERVAL_SECONDS = 60 * 60
    _FONT_FILE_NAME = "NotoSansCJKsc-Regular.otf"
    _FONT_SHA256 = (
        "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
    )
    _FONT_DOWNLOAD_URL = (
        "https://raw.githubusercontent.com/moeneri/apexrankwatch-assets/"
        "font-v1/fonts/NotoSansCJKsc-Regular.otf"
    )
    _FONT_DOWNLOAD_TIMEOUT_SECONDS = 30
    _FONT_MAX_BYTES = 25 * 1024 * 1024

    _KEYWORD_COMMAND_BLOCKLIST = {
        # 英文指令
        "apexrank",
        "apexrankwatch",
        "apexrankrecord",
        "apexranklist",
        "apexrankremove",
        "apexseason",
        "apexpredator",
        "map",
        "apexmap",
        "apexrankmap",
        "dailymap",
        "apextest",
        "apexhelp",
        "apex_download",
        "apexdownload",
        "apexfont",
        "apex帮助",
        "apexrankhelp",
        "apexblacklist",
        "apexalias",
        "apexbind",
        "apexbindlist",
        "apexunbind",
        "apexunalias",
        # 中文别名
        "apex监控",
        "apex列表",
        "apex移除",
        "apex查询",
        "地图",
        "排位地图",
        "匹配地图",
        "全天地图",
        "全天排位地图",
        "今日地图",
        "今日排位地图",
        "apex猎杀",
        "猎杀",
        "视奸",
        "持续视奸",
        "持续记录",
        "持续视奸列表",
        "取消持续视奸",
        "分数变化",
        "apex分数变化",
        "分数图",
        "apex赛季",
        "新赛季",
        "apex测试",
        "apex字体",
        "apex字体下载",
        "apex黑名单",
        "apex别名",
        "apex绑定",
        "apex绑定列表",
        "apex解绑",
        "apex取消别名",
        "取消绑定",
        "解除绑定",
        "取消别名",
        "不准视奸",
        "apexban",
        # 赛季关键词开关
        "赛季关闭",
        "赛季开启",
    }

    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)

        if config is None:
            config = {}
        self.config = config
        self._config = PluginConfig.from_raw(config)
        self._data_dir = self._resolve_data_dir(self._config.data_dir)
        self._settings_file = self._data_dir / "settings.json"
        self._daily_map_pool_state_file = self._data_dir / "daily_map_pool_state.json"
        settings = self._load_settings()
        self._runtime_blacklist = self._normalize_settings_list(
            settings.get("runtime_blacklist", []),
            lowercase=True,
        )
        self._season_keyword_disabled_umos = self._normalize_settings_list(
            settings.get("season_keyword_disabled_umos", []),
            lowercase=False,
        )
        (
            self._runtime_player_aliases,
            self._runtime_player_alias_display_names,
        ) = self._normalize_settings_alias_payload(
            settings.get("runtime_player_aliases", {}),
        )
        self._runtime_user_bindings = self._normalize_user_bindings(
            settings.get("runtime_user_bindings", {}),
        )
        self._runtime_group_user_bindings = self._normalize_group_user_bindings(
            settings.get("runtime_group_user_bindings", {})
        )
        self._runtime_session_subscriptions = self._normalize_session_subscriptions(
            settings.get("runtime_session_subscriptions", {})
        )
        configured_umos = settings.get("configured_session_umos")
        self._configured_session_umos = (
            self._normalize_settings_list(configured_umos, lowercase=False)
            if isinstance(configured_umos, list)
            else None
        )
        self._subscription_model_version = coerce_int(settings.get("subscription_model_version", 0), 0)
        self._store = GroupStore(self._data_dir / "groups.json", logger)
        self._store.load()
        self._migrate_store_keys()
        self._migrate_session_subscriptions()
        self._migrate_season_keyword_settings(
            settings.get("season_keyword_disabled_groups", [])
        )
        self._push_archive = PushArchive(self._data_dir / "push_archive")
        self._cleanup_push_archive()
        self._daily_map_pool_state = self._load_daily_map_pool_state()

        self._api = ApexApiClient(
            api_key=self._config.api_key,
            debug_enabled=self._config.debug_logging,
            timeout_ms=self._config.timeout_ms,
            max_retries=self._config.max_retries,
            logger=logger,
            season_probe_player=self._config.season_probe_player,
        )

        self._stop_event = asyncio.Event()
        self._poll_task = None
        self._daily_map_learning_task = None
        self._poll_concurrency = 5
        self._poll_semaphore = asyncio.Semaphore(self._poll_concurrency)
        self._daily_map_refresh_lock = asyncio.Lock()
        self._runtime_started = False
        self._font_notice_sent = False
        self._webui_poll_state = {
            "status": "waiting",
            "last_started": None,
            "last_finished": None,
            "last_error": "",
        }

        self._webui = None
        if context is not None:
            try:
                from .webui import ApexRankWatchWebUI
            except ModuleNotFoundError as exc:
                if exc.name != "astrbot.api.web":
                    raise
                logger.warning(
                    "当前 AstrBot 版本缺少插件面板 API，Dashboard 未启用；"
                    "升级 AstrBot 至 4.26 或更新版本可使用面板。"
                )
            else:
                self._webui = ApexRankWatchWebUI(self)
                self._webui.register()

        logger.info(
            f"Apex Rank Watch 已加载，检测间隔 {self._config.check_interval} 分钟"
        )
        if self._config.debug_logging:
            logger.info("Apex Rank Watch 调试日志已开启")

        self._schedule_runtime_start()

    async def _shutdown_runtime(self) -> None:
        self._stop_event.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._daily_map_learning_task:
            self._daily_map_learning_task.cancel()
            try:
                await self._daily_map_learning_task
            except asyncio.CancelledError:
                pass
        await self._api.close()

    async def terminate(self):
        await self._shutdown_runtime()

    async def on_unload(self):
        await self._shutdown_runtime()

    @staticmethod
    def _resolve_data_dir(data_dir: str) -> Path:
        if data_dir:
            return Path(data_dir)
        return Path(StarTools.get_data_dir())

    def _schedule_runtime_start(self) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop.is_closed():
            return
        loop.call_soon(self._start_runtime)

    def _start_runtime(self) -> None:
        if self._runtime_started:
            return
        self._runtime_started = True
        self._ensure_poll_task()
        self._ensure_daily_map_learning_task()

    def _ensure_poll_task(self) -> bool:
        if self._poll_task and not self._poll_task.done():
            return True
        if self._poll_task and self._poll_task.done():
            try:
                exc = self._poll_task.exception()
            except asyncio.CancelledError:
                exc = None
            if exc:
                logger.error(f"轮询任务异常退出，正在重建任务: {exc}")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._poll_task = loop.create_task(self._poll_loop())
        return True

    def _ensure_daily_map_learning_task(self) -> bool:
        if self._daily_map_learning_task and not self._daily_map_learning_task.done():
            return True
        if self._daily_map_learning_task and self._daily_map_learning_task.done():
            try:
                exc = self._daily_map_learning_task.exception()
            except asyncio.CancelledError:
                exc = None
            if exc:
                logger.error(f"全天地图学习任务异常退出，正在重建任务: {exc}")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._daily_map_learning_task = loop.create_task(self._daily_map_learning_loop())
        return True

    async def _refresh_daily_map_schedule(self) -> DailyMapScheduleInfo | None:
        if not self._config.api_key:
            return None

        async with self._daily_map_refresh_lock:
            season_info = None
            try:
                season_info = await self._api.fetch_current_season_info()
            except Exception as exc:
                logger.warning(f"全天地图赛季信息获取失败，将仅使用 API 轮换学习状态: {exc}")

            schedule = await self._api.fetch_daily_map_schedule(
                "ranked",
                pool_state=self._daily_map_pool_state,
                season_info=season_info,
            )
            if schedule.pool_state is not None:
                previous_status = self._daily_map_pool_state.status
                self._daily_map_pool_state = schedule.pool_state
                self._save_daily_map_pool_state()
                if previous_status != self._daily_map_pool_state.status:
                    logger.info(
                        f"全天地图学习状态更新: {previous_status} -> {self._daily_map_pool_state.status}"
                    )
                elif self._config.debug_logging:
                    logger.debug(f"全天地图学习刷新: {schedule.source_note}")
            return schedule

    async def _daily_map_learning_tick(self) -> bool:
        if not self._config.api_key:
            return False
        if self._daily_map_pool_state.status == "confirmed":
            return False
        schedule = await self._refresh_daily_map_schedule()
        return schedule is not None

    @staticmethod
    def _normalize_settings_list(value, lowercase: bool = True) -> set[str]:
        if not isinstance(value, list):
            return set()
        items = []
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            items.append(text.lower() if lowercase else text)
        return set(items)

    @staticmethod
    def _normalize_alias_key(value: str) -> str:
        return str(value or "").strip().lower()

    def _aliases_enabled(self) -> bool:
        return bool(getattr(self._config, "alias_enabled", True))

    @classmethod
    def _normalize_binding_target(cls, value) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        parts = text.split()
        if parts:
            first = parts[0].strip()
            lowered = first.lower()
            if lowered.startswith("/uid:") or lowered.startswith("/uuid:"):
                parts[0] = first[1:]
        return " ".join(parts).strip()

    @classmethod
    def _make_alias_entry(cls, raw_key, raw_target) -> PlayerAliasEntry | None:
        display_name = str(raw_key or "").strip()
        target = ""
        if isinstance(raw_target, dict):
            display_name = str(
                raw_target.get("display")
                or raw_target.get("display_name")
                or raw_target.get("alias")
                or display_name
            ).strip()
            target = str(
                raw_target.get("target")
                or raw_target.get("value")
                or raw_target.get("player")
                or ""
            ).strip()
        else:
            target = str(raw_target or "").strip()

        key = cls._normalize_alias_key(str(raw_key))
        if not display_name:
            display_name = key
        if not key or not target:
            return None
        return PlayerAliasEntry(key=key, display_name=display_name, target=target)

    @classmethod
    def _parse_player_alias_entries(cls, value) -> dict[str, PlayerAliasEntry]:
        aliases: dict[str, PlayerAliasEntry] = {}
        if isinstance(value, dict):
            items = value.items()
        elif isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, dict):
                    if "target" in item and any(
                        key in item for key in ("alias", "display", "display_name", "key")
                    ):
                        raw_key = (
                            item.get("alias")
                            or item.get("display")
                            or item.get("display_name")
                            or item.get("key")
                        )
                        items.append((raw_key, item))
                    else:
                        items.extend(item.items())
                    continue
                text = str(item or "").strip()
                if "=" in text:
                    key, target = text.split("=", 1)
                    items.append((key, target))
        else:
            text = str(value or "")
            for separator in ("，", ",", "；", ";"):
                text = text.replace(separator, "\n")
            items = []
            for line in text.splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, target = line.split("=", 1)
                items.append((key, target))

        for raw_key, raw_target in items:
            entry = cls._make_alias_entry(raw_key, raw_target)
            if entry is not None:
                aliases[entry.key] = entry
        return aliases

    @classmethod
    def _parse_player_aliases(cls, value) -> dict[str, str]:
        return {
            key: entry.target
            for key, entry in cls._parse_player_alias_entries(value).items()
        }

    def _normalize_settings_alias_payload(self, value) -> tuple[dict[str, str], dict[str, str]]:
        entries = self._parse_player_alias_entries(value)
        return (
            {key: entry.target for key, entry in entries.items()},
            {key: entry.display_name for key, entry in entries.items()},
        )

    def _normalize_settings_aliases(self, value) -> dict[str, str]:
        return self._parse_player_aliases(value)

    @classmethod
    def _normalize_user_bindings(cls, value) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        bindings: dict[str, str] = {}
        for raw_user_id, raw_target in value.items():
            user_id = str(raw_user_id or "").strip()
            if isinstance(raw_target, dict):
                raw_target = (
                    raw_target.get("target")
                    or raw_target.get("value")
                    or raw_target.get("player")
                    or ""
                )
            target = cls._normalize_binding_target(raw_target)
            if user_id and target:
                bindings[user_id] = target
        return bindings

    @classmethod
    def _normalize_group_user_bindings(cls, value) -> dict[str, dict[str, str]]:
        if not isinstance(value, dict):
            return {}
        result = {}
        for raw_group_id, raw_bindings in value.items():
            group_id = str(raw_group_id or "").strip()
            if not group_id or not isinstance(raw_bindings, dict):
                continue
            bindings = cls._normalize_user_bindings(raw_bindings)
            # An empty override remembers a group-local removal of a legacy binding.
            for raw_user_id, target in raw_bindings.items():
                user_id = str(raw_user_id or "").strip()
                if user_id and target == "":
                    bindings[user_id] = ""
            if bindings:
                result[group_id] = bindings
        return result

    @staticmethod
    def _normalize_session_subscriptions(value) -> dict[str, dict[str, str]]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, dict[str, str]] = {}
        for raw_umo, raw_players in value.items():
            umo = str(raw_umo or "").strip()
            if not umo or not isinstance(raw_players, dict):
                continue
            players = {}
            for raw_key, raw_mode in raw_players.items():
                player_key = str(raw_key or "").strip()
                if player_key:
                    players[player_key] = normalize_watch_mode(raw_mode)
            result[umo] = players
        return result

    def _migrate_session_subscriptions(self) -> None:
        subscriptions = getattr(self, "_runtime_session_subscriptions", {})
        raw = getattr(self, "config", {})
        settings = raw.get("session_settings", {}) if isinstance(raw, dict) else {}
        sessions = settings.get("sessions", []) if isinstance(settings, dict) else []
        legacy = getattr(self, "_subscription_model_version", 0) < 1
        changed = legacy
        config_changed = False
        configured_umos = {
            str(item.get("umo") or "").strip()
            for item in (sessions if isinstance(sessions, list) else [])
            if isinstance(item, dict) and str(item.get("umo") or "").strip()
        }
        previous_umos = getattr(self, "_configured_session_umos", None)
        if previous_umos is not None:
            for umo in previous_umos - configured_umos:
                subscriptions[umo] = {}
        if previous_umos != configured_umos:
            self._configured_session_umos = configured_umos
            changed = True
        for item in sessions if isinstance(sessions, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("__template_key") != "session":
                item["__template_key"] = "session"
                config_changed = True
            if "group_id" not in item:
                parts = str(item.get("umo") or "").split(":", 2)
                item["group_id"] = parts[2] if len(parts) == 3 else ""
                config_changed = True
            item.setdefault("label", "")
            item.setdefault("enabled", True)
            if "season_keyword_enabled" not in item:
                item["season_keyword_enabled"] = True
                config_changed = True
            item.setdefault("output_mode", "inherit")
        # Old group origins remain destinations even when another adapter joins the group.
        for _, group in self._store.iter_groups():
            if group.origin and group.origin not in subscriptions:
                subscriptions[group.origin] = (
                    {
                        key: self._watch_mode(player)
                        for key, player in group.players.items()
                    }
                    if legacy
                    else {}
                )
                changed = True
        for item in sessions if isinstance(sessions, list) else []:
            if not isinstance(item, dict):
                continue
            umo = str(item.get("umo") or "").strip()
            if not umo or umo in subscriptions:
                continue
            parts = umo.split(":", 2)
            group_id = str(
                item.get("group_id") or (parts[2] if len(parts) == 3 else "")
            )
            group = self._store.get_group(group_id) if group_id else None
            subscriptions[umo] = (
                {
                    key: normalize_watch_mode(
                        getattr(player, "watch_mode", WATCH_MODE_NOTIFY)
                    )
                    for key, player in group.players.items()
                }
                if group and legacy
                else {}
            )
            changed = True
        if changed:
            self._runtime_session_subscriptions = subscriptions
            self._subscription_model_version = 1
            self._save_settings()
        if config_changed and hasattr(raw, "save_config"):
            raw.save_config()

    def _migrate_season_keyword_settings(self, legacy_groups: Any) -> None:
        """Convert the old group-wide switch to per-UMO state."""
        groups = {
            str(group_id).strip()
            for group_id in (legacy_groups or [])
            if str(group_id).strip()
        }
        if not groups:
            return
        umos: set[str] = set(getattr(self, "_season_keyword_disabled_umos", set()))
        raw = self.config if isinstance(self.config, dict) else {}
        settings = raw.get("session_settings", {}) if isinstance(raw, dict) else {}
        sessions = settings.get("sessions", []) if isinstance(settings, dict) else []
        config_changed = False
        for item in sessions if isinstance(sessions, list) else []:
            if not isinstance(item, dict):
                continue
            umo = str(item.get("umo") or "").strip()
            parts = umo.split(":", 2)
            group_id = str(item.get("group_id") or (parts[2] if len(parts) == 3 else ""))
            if umo and group_id in groups:
                umos.add(umo)
                item["season_keyword_enabled"] = False
                config_changed = True
        for group_id, group in self._store.iter_groups():
            if str(group_id) in groups and getattr(group, "origin", ""):
                umos.add(str(group.origin))
        if umos != getattr(self, "_season_keyword_disabled_umos", set()):
            self._season_keyword_disabled_umos = umos
            self._save_settings()
        if config_changed and hasattr(raw, "save_config"):
            raw.save_config()

    def _set_session_subscriptions(
        self, umo: str, subscriptions: dict[str, str]
    ) -> None:
        normalized = {
            str(key): normalize_watch_mode(mode)
            for key, mode in subscriptions.items()
            if str(key).strip()
        }
        current = getattr(self, "_runtime_session_subscriptions", {})
        current[str(umo)] = normalized
        self._runtime_session_subscriptions = current
        self._save_settings()

    def _subscribe_session_player(self, umo: str, player_key: str, mode: str) -> None:
        if not umo:
            return
        subscriptions = getattr(self, "_runtime_session_subscriptions", {})
        session = subscriptions.setdefault(umo, {})
        session[player_key] = normalize_watch_mode(mode)
        subscriptions[umo] = session
        self._runtime_session_subscriptions = subscriptions
        self._save_settings()

    def _unsubscribe_player_everywhere(self, group_id: str, player_key: str) -> None:
        subscriptions = getattr(self, "_runtime_session_subscriptions", {})
        for umo, players in list(subscriptions.items()):
            parts = str(umo).split(":", 2)
            if len(parts) != 3 or parts[2] != str(group_id):
                continue
            players.pop(player_key, None)
            subscriptions[umo] = players
        self._runtime_session_subscriptions = subscriptions
        self._save_settings()

    def _get_config_player_aliases(self) -> dict[str, str]:
        return self._parse_player_aliases(getattr(self._config, "player_aliases", ""))

    def _get_config_player_alias_display_names(self) -> dict[str, str]:
        return {
            key: entry.display_name
            for key, entry in self._parse_player_alias_entries(
                getattr(self._config, "player_aliases", "")
            ).items()
        }

    def _get_runtime_player_alias_display_names(self) -> dict[str, str]:
        aliases = getattr(self, "_runtime_player_aliases", {})
        display_names = dict(getattr(self, "_runtime_player_alias_display_names", {}) or {})
        for key in aliases:
            display_names.setdefault(key, key)
        return display_names

    def _get_player_aliases(self) -> dict[str, str]:
        aliases = self._get_config_player_aliases()
        aliases.update(getattr(self, "_runtime_player_aliases", {}))
        return aliases

    def _get_player_alias_display_names(self) -> dict[str, str]:
        display_names = self._get_config_player_alias_display_names()
        display_names.update(self._get_runtime_player_alias_display_names())
        return display_names

    def _load_settings(self) -> dict:
        defaults = {
            "runtime_blacklist": [],
            "season_keyword_disabled_umos": [],
            "runtime_player_aliases": {},
            "runtime_user_bindings": {},
            "runtime_group_user_bindings": {},
            "runtime_session_subscriptions": {},
            "configured_session_umos": None,
            "subscription_model_version": 0,
        }
        if not self._settings_file.exists():
            return defaults
        try:
            raw = json.loads(self._settings_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return defaults
            merged = dict(defaults)
            merged.update(raw)
            return merged
        except Exception as exc:
            logger.error(f"加载插件设置失败: {exc}")
            return defaults

    def _save_settings(self) -> None:
        alias_display_names = self._get_runtime_player_alias_display_names()
        payload = {
            "runtime_blacklist": sorted(self._runtime_blacklist),
            "season_keyword_disabled_umos": sorted(
                getattr(self, "_season_keyword_disabled_umos", set())
            ),
            "runtime_player_aliases": {
                key: {
                    "target": target,
                    "display": alias_display_names.get(key, key),
                }
                for key, target in sorted(self._runtime_player_aliases.items())
            },
            "runtime_user_bindings": dict(
                sorted(getattr(self, "_runtime_user_bindings", {}).items())
            ),
            "runtime_group_user_bindings": {
                group_id: dict(sorted(bindings.items()))
                for group_id, bindings in sorted(
                    getattr(self, "_runtime_group_user_bindings", {}).items()
                )
            },
            "runtime_session_subscriptions": {
                umo: dict(sorted(players.items()))
                for umo, players in sorted(
                    getattr(self, "_runtime_session_subscriptions", {}).items()
                )
            },
            "configured_session_umos": sorted(
                getattr(self, "_configured_session_umos", None) or set()
            ),
            "subscription_model_version": getattr(self, "_subscription_model_version", 1),
        }
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self._settings_file.with_suffix(".tmp")
            tmp_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_file.replace(self._settings_file)
        except Exception as exc:
            logger.error(f"保存插件设置失败: {exc}")
            raise

    def _load_daily_map_pool_state(self) -> DailyMapPoolState:
        if not self._daily_map_pool_state_file.exists():
            return DailyMapPoolState()
        try:
            raw = json.loads(self._daily_map_pool_state_file.read_text(encoding="utf-8"))
            return DailyMapPoolState.from_dict(raw)
        except Exception as exc:
            logger.error(f"加载排位地图池学习状态失败: {exc}")
            return DailyMapPoolState()

    def _save_daily_map_pool_state(self) -> None:
        try:
            self._daily_map_pool_state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self._daily_map_pool_state_file.with_suffix(".tmp")
            tmp_file.write_text(
                json.dumps(
                    self._daily_map_pool_state.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp_file.replace(self._daily_map_pool_state_file)
        except Exception as exc:
            logger.error(f"保存排位地图池学习状态失败: {exc}")

    def _normalize_platform(self, platform: str) -> str:
        return normalize_platform(platform)

    def _format_platform(self, platform: str) -> str:
        mapping = {
            "PC": "PC",
            "PS4": "PlayStation",
            "X1": "Xbox",
            "SWITCH": "Switch",
        }
        return mapping.get(platform, platform)

    @staticmethod
    def _now_str() -> str:
        return now_str()

    def _time_line(self) -> str:
        return f"🕒 时间: {self._now_str()}"

    @staticmethod
    def _event_mentions_me(event: AstrMessageEvent) -> bool:
        message_obj = getattr(event, "message_obj", None)
        if not message_obj:
            return False
        self_id = str(getattr(message_obj, "self_id", "") or "")
        if not self_id:
            return False
        chain = getattr(message_obj, "message", None)
        if not isinstance(chain, list):
            return False
        for seg in chain:
            if isinstance(seg, dict):
                if str(seg.get("type") or "").lower() != "at":
                    continue
                payload = seg.get("data") if isinstance(seg.get("data"), dict) else seg
                target = (
                    payload.get("qq")
                    or payload.get("target")
                    or payload.get("user_id")
                    or payload.get("id")
                    or ""
                )
                if str(target) == self_id:
                    return True
                continue

            target = (
                getattr(seg, "qq", None)
                or getattr(seg, "target", None)
                or getattr(seg, "user_id", None)
                or getattr(seg, "id", None)
            )
            if target is not None and str(target) == self_id:
                return True
        return False

    def _format_passive_text(self, event: AstrMessageEvent, text: str) -> str:
        if not text:
            return ""
        # 兼容会裁剪开头换行或空格的适配器。
        group_id = self._get_group_id(event)
        if group_id and self._event_mentions_me(event):
            return "\u200b\n" + text.lstrip("\n")
        return text

    @staticmethod
    def _read_first_available(source, *names):
        if source is None:
            return None
        if isinstance(source, dict):
            for name in names:
                value = source.get(name)
                if value not in (None, ""):
                    return value
            return None
        for name in names:
            value = getattr(source, name, None)
            if value not in (None, ""):
                return value
        return None

    def _get_reply_message_id(self, event: AstrMessageEvent):
        message_obj = getattr(event, "message_obj", None)
        if not message_obj:
            return None
        value = self._read_first_available(message_obj, "message_id", "id")
        if value:
            return value
        raw = getattr(message_obj, "raw_message", None)
        value = self._read_first_available(raw, "message_id", "id", "messageId", "msg_id")
        return value or None

    @staticmethod
    def _coerce_reply_id(value):
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except Exception:
            return text

    def _prefer_quote_reply(self, event: AstrMessageEvent) -> bool:
        if not coerce_bool(getattr(self._config, "reply_to_command", True), True):
            return False
        # QQ 个人号（aiocqhttp / OneBot v11）支持引用回复。
        try:
            return str(event.get_platform_name() or "").lower() == "aiocqhttp"
        except Exception:
            return False

    def _plain(self, event: AstrMessageEvent, text: str):
        if text is None:
            text = ""
        if self._prefer_quote_reply(event):
            reply_id = self._coerce_reply_id(self._get_reply_message_id(event))
            if reply_id is not None:
                # QQ 个人号优先使用引用回复，避免频繁 @ 用户。
                return event.chain_result(
                    [Comp.Reply(id=reply_id), Comp.Plain(text=text.lstrip("\n"))]
                )
        return event.plain_result(self._format_passive_text(event, text))

    def _image(self, event: AstrMessageEvent, path: Path):
        image = Comp.Image.fromFileSystem(str(path))
        if self._prefer_quote_reply(event):
            reply_id = self._coerce_reply_id(self._get_reply_message_id(event))
            if reply_id is not None:
                return event.chain_result([Comp.Reply(id=reply_id), image])
        return event.chain_result([image])

    def _is_text_output_mode(self) -> bool:
        return str(getattr(self._config, "output_mode", "image") or "image").lower() == "text"

    def _is_image_output_mode(self) -> bool:
        return not self._is_text_output_mode()

    @staticmethod
    def _parse_list(raw: str) -> set[str]:
        return _split_csv(raw)

    @staticmethod
    def _split_blacklist_items(raw: str) -> list[str]:
        return sorted(_split_csv(raw))

    def _normalize_lookup_value(self, value: str) -> str:
        identifier, _ = self._parse_identifier(value)
        normalized = identifier or value
        return normalized.strip().lower()

    def _get_config_blacklist(self) -> set[str]:
        return {
            self._normalize_lookup_value(item)
            for item in _split_csv(self._config.blacklist)
        }

    def _extract_command_args(self, event: AstrMessageEvent) -> str:
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return ""
        raw = text.lstrip("/").lstrip("／").strip()
        if not raw:
            return ""
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        if command in self._KEYWORD_COMMAND_BLOCKLIST:
            return parts[1].strip() if len(parts) > 1 else ""
        return raw

    def _parse_blacklist_command(
        self, event: AstrMessageEvent, action: str, player_name: str
    ) -> tuple[str, str]:
        text = self._extract_command_args(event)
        if text:
            parts = text.split(maxsplit=1)
            if parts:
                action = parts[0]
            if len(parts) > 1:
                player_name = parts[1]
        return (action or "").strip(), (player_name or "").strip()

    def _parse_season_query(self, event: AstrMessageEvent, season: str = "") -> int | None:
        raw = self._extract_command_args(event) or str(season or "").strip()
        if not raw:
            return None

        token = raw.split(maxsplit=1)[0].strip().lower()
        if not token:
            return None
        if token in {"current", "curr", "now", "latest", "最新", "当前"}:
            return None
        if token.startswith("s") and token[1:].isdigit():
            token = token[1:]
        if token.isdigit():
            value = int(token)
            if value <= 0:
                raise ValueError("赛季号必须大于 0")
            # 数据源只暴露「当前分段」，没有按赛季号回溯的接口。与其静默返回
            # 当前分段的数据（等于答错），不如直接说清楚。
            raise ValueError(
                f"历史赛季查询已不支持：数据源只提供当前排位分段，无法查询 S{value}。"
                "直接发送 /新赛季 查看当前分段。"
            )
        raise ValueError("请输入正确的参数，例如 /apexseason 或 /apexseason current")

    def _is_season_keyword_disabled(self, umo: str, group_id: str = "") -> bool:
        umo = str(umo or "").strip()
        raw = self.config if isinstance(self.config, dict) else {}
        settings = raw.get("session_settings", {}) if isinstance(raw, dict) else {}
        sessions = settings.get("sessions", []) if isinstance(settings, dict) else []
        for item in sessions if isinstance(sessions, list) else []:
            if isinstance(item, dict) and str(item.get("umo") or "").strip() == umo:
                return item.get("season_keyword_enabled", True) is False
        return bool(umo) and umo in self._season_keyword_disabled_umos

    def _set_season_keyword_disabled(self, umo: str, disabled: bool) -> None:
        umo = str(umo or "").strip()
        if not umo:
            return
        if disabled:
            self._season_keyword_disabled_umos.add(umo)
        else:
            self._season_keyword_disabled_umos.discard(umo)
        raw = self.config if isinstance(self.config, dict) else {}
        settings = raw.get("session_settings", {}) if isinstance(raw, dict) else {}
        sessions = settings.get("sessions", []) if isinstance(settings, dict) else []
        changed = False
        for item in sessions if isinstance(sessions, list) else []:
            if isinstance(item, dict) and str(item.get("umo") or "").strip() == umo:
                item["season_keyword_enabled"] = not disabled
                changed = True
        if changed and hasattr(self.config, "save_config"):
            self.config.save_config()
        self._save_settings()

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        user_id = ""
        if message_obj:
            user_id = str(self._read_first_available(message_obj, "user_id") or "")
            if not user_id:
                sender = getattr(message_obj, "sender", None)
                if sender:
                    user_id = str(self._read_first_available(sender, "user_id", "id", "uid") or "")
        if not user_id:
            user_id = str(self._read_first_available(event, "user_id", "sender_id") or "")
        return user_id

    def _is_owner(self, user_id: str) -> bool:
        if not user_id:
            return False
        owners = self._parse_list(self._config.owner_qq)
        return user_id in owners

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        user_id = self._get_user_id(event)
        if self._is_owner(user_id):
            return True
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None) if message_obj else None
        role = str(getattr(sender, "role", "") or "").lower()
        return role in {"admin", "owner"}

    def _guard_admin(self, event: AstrMessageEvent) -> str | None:
        if self._is_admin(event):
            return None
        return "⚠️ 此命令仅管理员可用，请在配置中设置 owner_qq"

    def _is_user_blacklisted(self, user_id: str) -> bool:
        if not user_id:
            return False
        users = self._parse_list(self._config.user_blacklist)
        return user_id in users

    def _is_group_allowed(self, group_id: str, umo: str = "") -> bool:
        if not self._config.whitelist_enabled:
            return True
        groups = self._parse_list(self._config.whitelist_groups)
        return group_id in groups or (bool(umo) and umo in groups)

    def _guard_access(self, event: AstrMessageEvent, require_group: bool = False) -> str | None:
        user_id = self._get_user_id(event)
        if self._is_owner(user_id):
            return None

        if self._is_user_blacklisted(user_id):
            return "⛔ 你的QQ已被禁止使用该插件"

        group_id = self._get_group_id(event)
        if require_group and not group_id:
            return "⚠️ 此命令仅适用于群聊，请在群聊中使用"

        if not group_id and not self._config.allow_private:
            return "⚠️ 当前不允许私聊使用，请在群聊中使用"

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if group_id and not self._is_group_allowed(group_id, umo):
            return "⚠️ 本群未在白名单中，无法使用此插件"

        return None

    @staticmethod
    def _api_key_apply_url() -> str:
        return "https://portal.apexlegendsapi.com/"

    @staticmethod
    def _api_verify_url() -> str:
        return "https://portal.apexlegendsapi.com/discord-auth"

    def _missing_api_key_text(self) -> str:
        return "\n".join(
            [
                self._time_line(),
                "⚠️ 请先在插件配置中填写 API Key",
                f"🔑 Key 申请地址: {self._api_key_apply_url()}",
            ]
        )

    def _api_request_failed_text(
        self, action: str = "查询", error: Exception | None = None
    ) -> str:
        lines = [self._time_line()]
        if isinstance(error, ApexApiError):
            lines.append(f"❌ {action}失败：{error.user_message}")
            if "验证" in error.user_message:
                lines.append(f"🔗 验证入口: {self._api_verify_url()}")
            else:
                lines.append(f"🔑 Key 申请地址: {self._api_key_apply_url()}")
            return "\n".join(lines)

        lines.extend(
            [
                f"❌ {action}失败：请检查网络、API Key 是否有效，或稍后再试",
                f"🔑 Key 申请地址: {self._api_key_apply_url()}",
            ]
        )
        return "\n".join(lines)

    def _is_platform_token(self, token: str) -> bool:
        normalized = self._normalize_platform(token)
        return normalized in {"PC", "PS4", "X1", "SWITCH"}

    def _parse_player_platform(
        self, event: AstrMessageEvent, player_name: str, platform: str
    ) -> tuple[str, str]:
        text = self._extract_command_args(event)
        if text:
            parts = text.split()
            if len(parts) >= 2 and self._is_platform_token(parts[-1]):
                parsed_name = " ".join(parts[:-1]).strip()
                if parsed_name:
                    return parsed_name, parts[-1].strip()
            return text.strip(), platform.strip() if self._is_platform_token(platform) else ""
        if player_name and platform:
            return player_name.strip(), platform.strip()
        if player_name and not platform:
            return player_name.strip(), ""
        return "", ""

    def _parse_alias_target_platform(self, target: str) -> tuple[str, str]:
        text = str(target or "").strip()
        if not text:
            return "", ""
        parts = text.split()
        if len(parts) >= 2 and self._is_platform_token(parts[-1]):
            identifier = " ".join(parts[:-1]).strip()
            if identifier:
                return identifier, parts[-1].strip()
        return text, ""

    def _resolve_player_alias(self, player_name: str, platform: str) -> tuple[str, str]:
        resolved = self._resolve_player_alias_info(player_name, platform)
        return resolved.player_name, resolved.platform

    def _resolve_player_alias_info(
        self, player_name: str, platform: str
    ) -> PlayerAliasResolution:
        if not self._aliases_enabled():
            return PlayerAliasResolution(player_name, player_name, platform)
        alias_key = self._normalize_alias_key(player_name)
        if not alias_key:
            return PlayerAliasResolution(player_name, player_name, platform)
        target = self._get_player_aliases().get(alias_key)
        if not target:
            return PlayerAliasResolution(player_name, player_name, platform)
        resolved_name, alias_platform = self._parse_alias_target_platform(target)
        display_names = self._get_player_alias_display_names()
        return PlayerAliasResolution(
            requested_name=player_name,
            player_name=resolved_name or player_name,
            platform=platform or alias_platform,
            alias_key=alias_key,
            alias_display=display_names.get(alias_key, str(player_name or "").strip()),
            alias_target=target,
        )

    def _resolve_user_binding_info(
        self, event: AstrMessageEvent, platform: str
    ) -> PlayerAliasResolution:
        if not self._aliases_enabled():
            return PlayerAliasResolution("", "", platform)
        user_id = self._get_user_id(event)
        target = self._get_user_bindings_for_event(event).get(user_id, "")
        if not user_id or not target:
            return PlayerAliasResolution("", "", platform)
        resolved_name, alias_platform = self._parse_alias_target_platform(target)
        return PlayerAliasResolution(
            requested_name="",
            player_name=resolved_name,
            platform=platform or alias_platform,
            alias_key=f"user:{user_id}",
            alias_display="我的绑定",
            alias_target=target,
        )

    def _parse_alias_command(
        self, event: AstrMessageEvent, action: str, alias: str, target: str
    ) -> tuple[str, str, str]:
        text = self._extract_command_args(event)
        if text:
            parts = text.split(maxsplit=2)
            if parts:
                action = parts[0]
            if len(parts) > 1:
                alias = parts[1]
            if len(parts) > 2:
                target = parts[2]
        return (action or "").strip(), (alias or "").strip(), (target or "").strip()

    @staticmethod
    def _display_name_with_alias(alias: str, actual: str) -> str:
        alias_text = str(alias or "").strip()
        actual_text = str(actual or "").strip()
        if alias_text and actual_text and alias_text.lower() != actual_text.lower():
            return f"{alias_text}（{actual_text}）"
        return alias_text or actual_text or "未知"

    def _format_player_aliases(
        self,
        aliases: dict[str, str],
        display_names: dict[str, str] | None = None,
        overridden_keys: set[str] | None = None,
    ) -> str:
        if not aliases:
            return "无"
        display_names = display_names or {}
        overridden_keys = overridden_keys or set()
        items = []
        for key, value in sorted(aliases.items()):
            display = display_names.get(key, key)
            suffix = "（已被动态覆盖）" if key in overridden_keys else ""
            items.append(f"{display}={value}{suffix}")
        return "，".join(items)

    def _format_effective_player_aliases(self) -> str:
        return self._format_player_aliases(
            self._get_player_aliases(),
            self._get_player_alias_display_names(),
        )

    def _format_user_bindings(self, bindings: dict[str, str] | None = None) -> str:
        bindings = bindings if bindings is not None else getattr(self, "_runtime_user_bindings", {})
        if not bindings:
            return "无"
        return "，".join(
            f"QQ {user_id}={target}" for user_id, target in sorted(bindings.items())
        )

    def _get_group_user_bindings(self, group_id: str) -> dict[str, str]:
        bindings = dict(getattr(self, "_runtime_user_bindings", {}))
        overrides = getattr(self, "_runtime_group_user_bindings", {}).get(group_id, {})
        for user_id, target in overrides.items():
            if target:
                bindings[user_id] = target
            else:
                bindings.pop(user_id, None)
        return bindings

    def _get_user_bindings_for_event(self, event: AstrMessageEvent) -> dict[str, str]:
        group_id = self._get_group_id(event)
        if group_id:
            return self._get_group_user_bindings(group_id)
        return getattr(self, "_runtime_user_bindings", {})

    def _set_group_user_binding(
        self, group_id: str, user_id: str, target: str | None
    ) -> None:
        group_bindings = getattr(self, "_runtime_group_user_bindings", {})
        bindings = group_bindings.setdefault(group_id, {})
        if target:
            bindings[user_id] = target
        elif user_id in getattr(self, "_runtime_user_bindings", {}):
            bindings[user_id] = ""
        else:
            bindings.pop(user_id, None)
        if not bindings:
            group_bindings.pop(group_id, None)
        self._runtime_group_user_bindings = group_bindings

    def _set_user_binding_for_event(
        self, event: AstrMessageEvent, user_id: str, target: str | None
    ) -> None:
        group_id = self._get_group_id(event)
        if group_id:
            self._set_group_user_binding(group_id, user_id, target)
            return
        bindings = getattr(self, "_runtime_user_bindings", {})
        if target:
            bindings[user_id] = target
        else:
            bindings.pop(user_id, None)
        self._runtime_user_bindings = bindings

    @staticmethod
    def _alias_list_entries(
        config_aliases: dict[str, str],
        runtime_aliases: dict[str, str],
        config_display_names: dict[str, str] | None = None,
        runtime_display_names: dict[str, str] | None = None,
    ) -> list[tuple[str, str, str]]:
        config_display_names = config_display_names or {}
        runtime_display_names = runtime_display_names or {}
        entries: list[tuple[str, str, str]] = []
        for key, target in sorted(config_aliases.items()):
            source = "配置（已覆盖）" if key in runtime_aliases else "配置（生效）"
            entries.append((config_display_names.get(key, key), target, source))
        for key, target in sorted(runtime_aliases.items()):
            entries.append((runtime_display_names.get(key, key), target, "动态（生效）"))
        return entries

    @staticmethod
    def _binding_list_entries(bindings: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
        bindings = bindings or {}
        return [
            (f"QQ {user_id}", target, "个人绑定")
            for user_id, target in sorted(bindings.items())
        ]

    def _binding_list_response(self, event: AstrMessageEvent, bindings: dict[str, str]):
        entries = self._binding_list_entries(bindings)
        lines = [
            self._time_line(),
            "🔖 Apex 查询绑定列表",
            f"个人绑定：{self._format_user_bindings(bindings)}",
            "提示：绑定只用于对应 QQ 用户直接发送 /apex查询 的默认目标",
        ]
        if self._is_image_output_mode():
            try:
                image_path = self._render_binding_list_image(entries)
                return self._image(event, image_path)
            except Exception as exc:
                logger.error(f"查询绑定列表图片生成失败，已回退文字输出: {exc}")
        return self._plain(event, "\n".join(lines))

    def _remove_current_user_binding_response(self, event: AstrMessageEvent):
        user_id = self._get_user_id(event)
        if not user_id:
            return self._plain(event, "\n".join([self._time_line(), "⚠️ 无法识别当前用户，暂不能解绑"]))

        bindings = self._get_user_bindings_for_event(event)
        removed = bindings.get(user_id, "")
        self._set_user_binding_for_event(event, user_id, None)
        if removed:
            self._save_settings()
            scope = "本群" if self._get_group_id(event) else "你的"
            return self._plain(
                event,
                "\n".join([self._time_line(), f"✅ 已解除{scope} Apex 查询绑定：{removed}"]),
            )
        return self._plain(event, "\n".join([self._time_line(), "ℹ️ 你当前没有 Apex 查询绑定"]))

    def _apply_alias_to_player_data(
        self, player_data: ApexPlayerStats, alias_info: PlayerAliasResolution
    ) -> None:
        if not alias_info.matched:
            return
        setattr(player_data, "display_alias", alias_info.alias_display)
        setattr(player_data, "alias_target", alias_info.alias_target)
        setattr(player_data, "alias_lookup_name", alias_info.player_name)

    def _apply_record_alias_to_player_data(
        self, player_data: ApexPlayerStats, player: PlayerRecord
    ) -> None:
        display_alias = str(getattr(player, "display_alias", "") or "").strip()
        alias_target = str(getattr(player, "alias_target", "") or "").strip()
        if display_alias:
            setattr(player_data, "display_alias", display_alias)
        if alias_target:
            setattr(player_data, "alias_target", alias_target)

    def _player_data_display_name(self, player_data: ApexPlayerStats) -> str:
        return self._display_name_with_alias(
            str(getattr(player_data, "display_alias", "") or ""),
            str(getattr(player_data, "name", "") or ""),
        )

    def _record_display_name(self, player: PlayerRecord) -> str:
        return self._display_name_with_alias(
            str(getattr(player, "display_alias", "") or ""),
            str(getattr(player, "player_name", "") or ""),
        )

    def _append_score_history(
        self,
        player: PlayerRecord,
        *,
        player_data: ApexPlayerStats,
        old_score: int,
        is_season_reset: bool,
        captured_at: int | None = None,
    ) -> ScoreChangeRecord:
        captured_at = now_epoch_ms() if captured_at is None else int(captured_at)
        to_score = int(player_data.rank_score)
        item = ScoreChangeRecord(
            captured_at=captured_at,
            player_name=player_data.name or player.player_name,
            platform=player_data.platform or player.platform,
            from_score=int(old_score),
            to_score=to_score,
            score_delta=to_score - int(old_score),
            from_rank_name=str(getattr(player, "rank_name", "") or ""),
            from_rank_div=coerce_int(getattr(player, "rank_div", 0), 0),
            to_rank_name=str(player_data.rank_name or ""),
            to_rank_div=coerce_int(player_data.rank_div, 0),
            global_rank_percent=str(player_data.global_rank_percent or "未知"),
            selected_legend=str(player_data.selected_legend or ""),
            is_season_reset=bool(is_season_reset),
        )
        history = list(getattr(player, "history", []) or [])
        history.append(item)
        player.history = trim_score_history(history)
        return item

    @staticmethod
    def _watch_mode(player: PlayerRecord) -> str:
        return normalize_watch_mode(getattr(player, "watch_mode", WATCH_MODE_NOTIFY))

    @staticmethod
    def _watch_mode_label(mode_or_player) -> str:
        if isinstance(mode_or_player, PlayerRecord):
            mode = Main._watch_mode(mode_or_player)
        else:
            mode = normalize_watch_mode(mode_or_player)
        return "仅记录" if mode == WATCH_MODE_RECORD else "通报+记录"

    def _is_blacklisted(self, player_name: str) -> bool:
        if not player_name:
            return False
        name = self._normalize_lookup_value(player_name)
        if not name:
            return False
        if name in self._runtime_blacklist:
            return True
        if not self._config.blacklist:
            return False
        return name in self._get_config_blacklist()

    def _is_query_blocked(self, player_name: str) -> bool:
        blocked = {
            self._normalize_lookup_value(item)
            for item in _split_csv(self._config.query_blocklist)
        }
        return self._normalize_lookup_value(player_name) in blocked

    def _blocked_lookup_name(self, *player_names: str) -> str:
        checked: set[str] = set()
        for player_name in player_names:
            display_name = str(player_name or "").strip()
            if not display_name:
                continue
            normalized = self._normalize_lookup_value(display_name)
            if not normalized or normalized in checked:
                continue
            checked.add(normalized)
            if self._is_blacklisted(display_name):
                return display_name
        return ""

    def _blocked_query_name(self, *player_names: str) -> str:
        checked: set[str] = set()
        for player_name in player_names:
            display_name = str(player_name or "").strip()
            normalized = self._normalize_lookup_value(display_name)
            if not display_name or not normalized or normalized in checked:
                continue
            checked.add(normalized)
            if self._is_query_blocked(display_name):
                return display_name
        return ""

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", "") if message_obj else ""
        if group_id:
            return str(group_id)
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return str(value)
            except Exception:
                pass
        return str(getattr(event, "group_id", "") or "")

    def _session_poll_targets(
        self, group_id: str, group: Any
    ) -> tuple[tuple[str, PlayerRecord, str], ...]:
        """Resolve player subscriptions for each enabled and permitted UMO."""
        raw_config = getattr(self, "config", {})
        raw_settings = (
            raw_config.get("session_settings", {})
            if isinstance(raw_config, dict)
            else {}
        )
        sessions = (
            raw_settings.get("sessions", []) if isinstance(raw_settings, dict) else []
        )
        configured: dict[str, dict[str, Any]] = {}
        for item in sessions if isinstance(sessions, list) else []:
            if not isinstance(item, dict) or not item.get("umo"):
                continue
            umo = str(item.get("umo") or "").strip()
            parts = umo.split(":", 2)
            item_group = str(
                item.get("group_id") or (parts[2] if len(parts) == 3 else "")
            ).strip()
            if item_group == str(group_id):
                configured[umo] = item

        subscriptions = getattr(self, "_runtime_session_subscriptions", {})
        sources = set(configured)
        legacy_origin = str(getattr(group, "origin", "") or "")
        if legacy_origin:
            sources.add(legacy_origin)
        for umo in subscriptions:
            parts = str(umo).split(":", 2)
            if len(parts) == 3 and parts[2] == str(group_id):
                sources.add(str(umo))

        targets = []
        for umo in sorted(sources):
            if not self._is_group_allowed(str(group_id), umo):
                continue
            if not coerce_bool(configured.get(umo, {}).get("enabled", True), True):
                continue
            players = subscriptions.get(umo, {})
            if umo == getattr(group, "origin", "") and umo not in subscriptions:
                # Legacy installations without configured sessions retain group-level delivery.
                players = {
                    key: normalize_watch_mode(
                        getattr(player, "watch_mode", WATCH_MODE_NOTIFY)
                    )
                    for key, player in group.players.items()
                }
            for player_key, mode in players.items():
                player = group.players.get(player_key)
                if player is not None:
                    targets.append((umo, player, normalize_watch_mode(mode)))
        return tuple(targets)

    def _session_output_mode(self, umo: str) -> str:
        raw = getattr(self, "config", {})
        settings = raw.get("session_settings", {}) if isinstance(raw, dict) else {}
        for item in settings.get("sessions", []) if isinstance(settings, dict) else []:
            if isinstance(item, dict) and item.get("umo") == umo:
                mode = item.get("output_mode", "inherit")
                if mode in {"image", "text"}:
                    return mode
        return self._config.output_mode

    async def _send_active_message(self, origin: str, message: str) -> bool:
        if not origin:
            logger.warning("会话标识缺失，无法发送通知")
            return False
        try:
            chain = MessageChain().message(message)
            await self.context.send_message(origin, chain)
            return True
        except Exception as exc:
            logger.error(f"发送群消息失败: {exc}")
            return False

    async def _send_active_image(self, origin: str, path: Path) -> bool:
        if not origin:
            logger.warning("会话标识缺失，无法发送图片通知")
            return False
        try:
            chain = MessageChain().file_image(str(path))
            await self.context.send_message(origin, chain)
            return True
        except Exception as exc:
            logger.error(f"发送群图片失败: {exc}")
            return False

    def _parse_identifier(self, player_name: str) -> tuple[str, bool]:
        name = player_name.strip()
        lowered = name.lower()
        if lowered.startswith("uid:"):
            return name[4:].strip(), True
        if lowered.startswith("uuid:"):
            return name[5:].strip(), True
        return name, False

    def _build_player_key(self, lookup_id: str, platform: str, use_uid: bool) -> str:
        base = lookup_id.strip().lower()
        prefix = "uid:" if use_uid else "name:"
        return f"{prefix}{base}@{platform}"

    def _find_player_key(self, group, player_name: str, platform: str, use_uid: bool) -> str:
        identifier, is_uid = self._parse_identifier(player_name)
        use_uid = use_uid or is_uid
        if platform:
            normalized = self._normalize_platform(platform)
            key = self._build_player_key(identifier, normalized, use_uid)
            return key if key in group.players else ""

        prefix = "uid:" if use_uid else "name:"
        key_base = f"{prefix}{identifier.strip().lower()}@"
        matches = [k for k in group.players.keys() if k.startswith(key_base)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "__MULTI__"
        return ""

    def _migrate_store_keys(self) -> None:
        changed = False
        for group_id, group in self._store.iter_groups():
            new_players = {}
            for key, record in group.players.items():
                platform = getattr(record, "platform", "PC") or "PC"
                normalized = self._normalize_platform(platform)
                lookup_id = getattr(record, "lookup_id", "") or record.player_name
                use_uid = bool(getattr(record, "use_uid", False))
                new_key = self._build_player_key(lookup_id, normalized, use_uid)
                record.platform = normalized
                record.lookup_id = lookup_id
                record.use_uid = use_uid
                new_players[new_key] = record
                if new_key != key:
                    changed = True
            group.players = new_players
        if changed:
            self._store.save()

    def _clamp_score_change_limit(self, value) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            limit = self._SCORE_CHANGE_DEFAULT_LIMIT
        return max(1, min(limit, self._SCORE_CHANGE_MAX_LIMIT))

    def _parse_score_change_args(
        self, event: AstrMessageEvent, raw_args: str = ""
    ) -> tuple[str, str, int]:
        text = self._extract_command_args(event) or str(raw_args or "").strip()
        if not text:
            return "", "", self._SCORE_CHANGE_DEFAULT_LIMIT

        parts = [part for part in text.split() if part.strip()]
        limit = self._SCORE_CHANGE_DEFAULT_LIMIT
        if parts and parts[-1].isdigit():
            limit = self._clamp_score_change_limit(parts.pop())

        platform = ""
        if parts and self._is_platform_token(parts[-1]):
            platform = parts.pop()

        player_name = " ".join(parts).strip()
        return player_name, platform, limit

    @staticmethod
    def _score_history_items(player: PlayerRecord) -> list[ScoreChangeRecord]:
        items: list[ScoreChangeRecord] = []
        for item in list(getattr(player, "history", []) or []):
            if isinstance(item, ScoreChangeRecord):
                items.append(item)
            elif isinstance(item, dict):
                try:
                    items.append(ScoreChangeRecord.from_dict(item))
                except Exception:
                    continue
        return items

    def _recent_score_change_events(
        self, players: list[PlayerRecord], limit: int
    ) -> list[tuple[PlayerRecord, ScoreChangeRecord]]:
        events: list[tuple[PlayerRecord, ScoreChangeRecord]] = []
        for player in players:
            for item in self._score_history_items(player):
                events.append((player, item))
        events.sort(key=lambda pair: pair[1].captured_at, reverse=True)
        return events[: self._clamp_score_change_limit(limit)]

    def _resolve_score_change_players(
        self,
        group,
        player_name: str,
        platform: str,
    ) -> tuple[list[PlayerRecord], str]:
        if not player_name:
            return list(group.players.values()), ""

        alias_info = self._resolve_player_alias_info(player_name, platform)
        resolved_name, resolved_platform = alias_info.player_name, alias_info.platform
        identifier, use_uid = self._parse_identifier(resolved_name)
        if not identifier:
            return [], "⚠️ 请提供有效的玩家名称或 UID"

        lookup_name = f"uid:{identifier}" if use_uid else identifier
        player_key = self._find_player_key(group, lookup_name, resolved_platform, use_uid)
        display_name = self._display_name_with_alias(
            alias_info.alias_display if alias_info.matched else "",
            resolved_name,
        )
        if player_key == "__MULTI__":
            return [], "⚠️ 检测到同名多平台统计，请指定平台，例如: /分数变化 PlayerName pc"
        if not player_key:
            return [], f"ℹ️ 本群统计列表里没有找到 {display_name}，请先使用 /持续视奸 或 /持续记录"
        return [group.players[player_key]], ""

    @staticmethod
    def _rank_label_from_parts(rank_name: str, rank_div: int) -> str:
        rank_name = str(rank_name or "未知")
        rank_div = coerce_int(rank_div, 0)
        if rank_div:
            return f"{rank_name} {rank_div}"
        return rank_name

    def _history_from_rank_label(self, item: ScoreChangeRecord) -> str:
        return self._rank_label_from_parts(item.from_rank_name, item.from_rank_div)

    def _history_to_rank_label(self, item: ScoreChangeRecord) -> str:
        return self._rank_label_from_parts(item.to_rank_name, item.to_rank_div)

    def _format_score_history_time(self, captured_at: int) -> str:
        try:
            dt = datetime.fromtimestamp(int(captured_at) / 1000, SHANGHAI_TZ)
        except Exception:
            return "未知时间"
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _score_change_net_delta(self, events: list[ScoreChangeRecord]) -> int:
        return sum(int(item.score_delta) for item in events)

    def _format_score_change_text(
        self,
        players: list[PlayerRecord],
        limit: int,
    ) -> str:
        recent_events = self._recent_score_change_events(players, limit)
        event_count = len(recent_events)
        lines = [
            self._time_line(),
            f"📈 Apex 分数变化统计（最近 {event_count} 次分数变化）",
            f"👥 统计玩家: {len(players)} 位",
        ]
        if not recent_events:
            lines.append("ℹ️ 已有统计基线，暂无分数变化记录")
            return "\n".join(lines)

        for player in players:
            player_events = [
                item for owner, item in recent_events if owner is player
            ]
            if not player_events:
                continue
            latest = player_events[0]
            delta = self._score_change_net_delta(player_events)
            lines.extend(
                [
                    "➖ —",
                    f"👤 玩家: {self._record_display_name(player)}",
                    f"🧾 模式: {self._watch_mode_label(player)}",
                    f"🏆 当前段位: {self._record_rank_display(player)}",
                    f"🔢 当前分数: {self._format_rank_score_number(player.rank_score)} RP",
                    f"📊 最近变化: {delta:+d} RP / {len(player_events)} 次",
                    f"🕒 最近记录: {self._format_score_history_time(latest.captured_at)}",
                ]
            )

        lines.append("➖ —")
        lines.append("最近明细:")
        for _, item in recent_events[: min(10, len(recent_events))]:
            direction = "上分" if item.score_delta > 0 else "掉分"
            lines.append(
                f"{direction} {item.from_score} -> {item.to_score} ({item.score_delta:+d}) "
                f"{self._format_score_history_time(item.captured_at)}"
            )
        if len(recent_events) > 10:
            lines.append(f"还有 {len(recent_events) - 10} 条请查看图片长图")
        return "\n".join(lines)

    @filter.command("apextest")
    async def apextest(self, event: AstrMessageEvent):
        """测试插件运行状态和当前会话主动消息推送能力。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        group_id = self._get_group_id(event)
        if group_id:
            success = await self._send_active_message(
                event.unified_msg_origin, "✅ Apex Legends 排名监控测试消息"
            )
            if success:
                yield self._plain(
                    event,
                    "\n".join(
                        [
                            self._time_line(),
                            "✅ Apex Legends 排名监控插件正常运行中，测试消息已发送到当前会话",
                        ]
                    ),
                )
            else:
                yield self._plain(
                    event,
                    "\n".join(
                        [
                            self._time_line(),
                            "⚠️ 插件指令可用，但当前平台或会话不支持主动消息推送",
                        ]
                    ),
                )
            return
        yield self._plain(event, 
            "\n".join([self._time_line(), "✅ Apex Legends 排名监控插件正常运行中"])
        )

    @filter.command("apexhelp", alias={"apex帮助"})
    async def apexhelp(self, event: AstrMessageEvent):
        """查看 Apex Rank Watch 帮助与命令说明。"""
        async for result in self.apexrankhelp(event):
            yield result

    @filter.command("apexrankhelp")
    async def apexrankhelp(self, event: AstrMessageEvent):
        """查看 Apex Rank Watch 帮助与命令说明。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        lines = self._build_apex_help_lines()
        if self._is_text_output_mode():
            yield self._plain(event, "\n".join(lines))
            return
        try:
            image_path = self._render_apex_help_image()
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"帮助图片生成失败，已回退文字输出: {exc}")
            yield self._plain(event, "\n".join(lines))

    @filter.command("apex_download", alias={"apexdownload", "apexfont", "apex字体", "apex字体下载"})
    async def apex_download(self, event: AstrMessageEvent):
        """检测中文字体状态，并在缺少字体时手动下载插件字体缓存。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        status = self._get_cjk_font_status()
        if not status.available:
            await asyncio.to_thread(self._download_cjk_font_if_needed, True)
            status = self._get_cjk_font_status()

        yield self._plain(event, "\n".join(self._build_font_status_lines(status)))

    @filter.command("apexrank")
    async def apexrank(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """查询指定 Apex 玩家段位、分数、等级和在线状态，支持平台或 UID。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        requested_player_name, platform = self._parse_player_platform(event, player_name, platform)
        if requested_player_name:
            alias_info = self._resolve_player_alias_info(requested_player_name, platform)
        else:
            alias_info = self._resolve_user_binding_info(event, platform)
        player_name, platform = alias_info.player_name, alias_info.platform
        if not player_name:
            yield self._plain(event, 
                "\n".join([self._time_line(), "⚠️ 请提供玩家名称，例如: /apexrank PlayerName"])
            )
            return

        blocked_name = self._blocked_lookup_name(requested_player_name, player_name) or self._blocked_query_name(requested_player_name, player_name)
        if blocked_name:
            yield self._plain(event, 
                "\n".join(
                    [
                        self._time_line(),
                        f"⛔ 该ID（{blocked_name}）已被管理员加入黑名单，禁止查询",
                    ]
                )
            )
            return

        if not self._config.api_key:
            yield self._plain(event, self._missing_api_key_text())
            return

        identifier, use_uid = self._parse_identifier(player_name)
        if not identifier:
            yield self._plain(
                event,
                "\n".join([self._time_line(), "⚠️ 请提供有效的玩家名称或 UID"]),
            )
            return

        try:
            player_data, used_platform = await self._api.fetch_player_stats_auto(
                identifier, platform, use_uid
            )
        except PlayerNotFoundError:
            yield self._plain(event, 
                "\n".join(
                    [
                        self._time_line(),
                        "⚠️ 未找到该玩家，请检查名称或指定平台",
                    ]
                )
            )
            return
        except ApexApiError as exc:
            logger.error(f"API 查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("查询", exc))
            return
        except Exception as exc:
            logger.error(f"API 查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("查询"))
            return

        if player_data.rank_score < self._config.min_valid_score:
            yield self._plain(event, 
                "\n".join(
                    [
                        self._time_line(),
                        f"⚠️ 查询到 {player_name} 的分数为 {player_data.rank_score}，低于最小有效分数 {self._config.min_valid_score}，可能是API错误，请稍后再试",
                    ]
                )
            )
            return

        blocked_name = self._blocked_lookup_name(player_data.name, f"uid:{player_data.uid}") or self._blocked_query_name(player_data.name, f"uid:{player_data.uid}")
        if blocked_name:
            yield self._plain(event, "\n".join([self._time_line(), f"⛔ 该ID（{blocked_name}）已被管理员加入黑名单，禁止查询"]))
            return

        player_data.platform = used_platform
        self._apply_alias_to_player_data(player_data, alias_info)
        if self._is_text_output_mode():
            yield self._plain(event, self._format_player_rank_text(player_data))
            return
        try:
            image_path = self._render_player_rank_image(player_data)
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"生成玩家段位信息图片失败: {exc}")
            yield self._plain(event, self._format_player_rank_text(player_data))

    @filter.command("map", alias={"地图", "排位地图", "apexmap", "apexrankmap"})
    async def apexmap(self, event: AstrMessageEvent):
        """查询当前排位地图轮换和下一张地图。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        if not self._config.api_key:
            yield self._plain(event, self._missing_api_key_text())
            return

        try:
            rotation_info = await self._api.fetch_map_rotation_info()
        except ApexApiError as exc:
            logger.error(f"地图轮换查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("地图轮换查询", exc))
            return
        except Exception as exc:
            logger.error(f"地图轮换查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("地图轮换查询"))
            return

        if self._is_text_output_mode():
            yield self._plain(event, self._format_map_rotation_text(rotation_info))
            return
        try:
            image_path = self._render_map_rotation_image(rotation_info)
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"地图轮换图片生成失败，已回退文字输出: {exc}")
            yield self._plain(event, self._format_map_rotation_text(rotation_info))

    @filter.command("匹配地图")
    async def apexmatchmap(self, event: AstrMessageEvent):
        """查询当前三人赛匹配地图轮换。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        if not self._config.api_key:
            yield self._plain(event, self._missing_api_key_text())
            return

        try:
            rotation_info = await self._api.fetch_map_rotation_info()
        except ApexApiError as exc:
            logger.error(f"匹配地图轮换查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("匹配地图轮换查询", exc))
            return
        except Exception as exc:
            logger.error(f"匹配地图轮换查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("匹配地图轮换查询"))
            return

        if self._is_text_output_mode():
            yield self._plain(
                event,
                self._format_map_rotation_text(rotation_info, mode="battle_royale"),
            )
            return
        try:
            image_path = self._render_map_rotation_image(
                rotation_info, mode="battle_royale"
            )
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"匹配地图轮换图片生成失败，已回退文字输出: {exc}")
            yield self._plain(
                event,
                self._format_map_rotation_text(rotation_info, mode="battle_royale"),
            )

    @filter.command("全天地图", alias={"全天排位地图", "今日地图", "今日排位地图", "dailymap"})
    async def apexdailymap(self, event: AstrMessageEvent):
        """查询 API 学习确认的未来 24 小时排位地图排期。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        if not self._config.api_key:
            yield self._plain(event, self._missing_api_key_text())
            return

        try:
            schedule = await self._refresh_daily_map_schedule()
            if schedule is None:
                yield self._plain(event, self._missing_api_key_text())
                return
        except Exception as exc:
            logger.error(f"全天地图查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("全天地图查询"))
            return

        if not schedule.entries:
            yield self._plain(
                event,
                "\n".join(
                    [
                        self._time_line(),
                        "⚠️ 暂未获取到全天地图排期，请稍后再试",
                        "ℹ️ /map 当前地图仍使用 API 实时查询",
                    ]
                ),
            )
            return

        if self._is_text_output_mode():
            yield self._plain(event, self._format_daily_map_schedule_text(schedule))
            return
        try:
            image_path = self._render_daily_map_schedule_image(schedule)
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"全天地图图片生成失败，已回退文字输出: {exc}")
            yield self._plain(event, self._format_daily_map_schedule_text(schedule))

    @filter.command("apexpredator", alias={"apex猎杀", "猎杀"})
    async def apexpredator(self, event: AstrMessageEvent, platform: str = ""):
        """查询大师数量、猎杀线和各平台排名概览。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        if not self._config.api_key:
            yield self._plain(event, self._missing_api_key_text())
            return

        raw_platform = (self._extract_command_args(event) or platform or "").strip()
        selected_platform = ""
        if raw_platform:
            if not self._is_platform_token(raw_platform):
                yield self._plain(
                    event,
                    "\n".join(
                        [
                            self._time_line(),
                            "⚠️ 平台仅支持 PC / PS4 / X1 / SWITCH",
                            "例：/apexpredator pc  或  /猎杀",
                        ]
                    ),
                )
                return
            selected_platform = self._normalize_platform(raw_platform)

        try:
            predator_info = await self._api.fetch_predator_info()
        except ApexApiError as exc:
            logger.error(f"猎杀线查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("猎杀线查询", exc))
            return
        except Exception as exc:
            logger.error(f"猎杀线查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("猎杀线查询"))
            return

        if self._is_text_output_mode():
            yield self._plain(
                event, self._format_predator_info_text(predator_info, selected_platform)
            )
            return
        try:
            image_path = self._render_predator_info_image(predator_info)
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"猎杀线图片生成失败，已回退文字输出: {exc}")
            yield self._plain(
                event, self._format_predator_info_text(predator_info, selected_platform)
            )

    @filter.command("apexseason")
    async def apexseason(self, event: AstrMessageEvent, season: str = ""):
        """查询当前 Apex 排位分段（上/下半赛季）的结束时间和剩余时间。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        try:
            season_number = self._parse_season_query(event, season)
        except ValueError as exc:
            yield self._plain(
                event,
                "\n".join([self._time_line(), f"⚠️ {exc}"]),
            )
            return

        try:
            season_info = await self._api.fetch_season_info(season_number)
        except ApexApiError as exc:
            # 分段数据现在只有 API 一个来源，没有任何回退，因此必须把具体原因
            # （缺 Key / 限流 / 未验证）透出去，而不是笼统报“查询失败”。
            logger.error(f"排位分段查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("排位分段查询", exc))
            return
        except Exception as exc:
            logger.error(f"排位分段查询失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("排位分段查询"))
            return

        if self._is_text_output_mode():
            yield self._plain(event, self._format_season_info(season_info))
            return
        try:
            image_path = self._render_season_info_image(season_info)
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"赛季图片生成失败，已回退文字输出: {exc}")
            yield self._plain(event, self._format_season_info(season_info))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _season_keyword_listener(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        raw = text.lstrip()
        if raw.startswith(("/", "／")):
            first = raw.split(maxsplit=1)[0].lstrip("/").lstrip("／").strip().lower()
            if first in {"apex_download", "apexdownload", "apexfont", "apex字体", "apex字体下载"}:
                return
            if first in self._KEYWORD_COMMAND_BLOCKLIST:
                if self._consume_font_download_notice_needed():
                    yield self._plain(
                        event, "\n".join(self._build_font_download_notice_lines())
                    )
            return
        first = raw.split(maxsplit=1)[0].lstrip("/").lstrip("／").strip()
        if first and first.lower() in self._KEYWORD_COMMAND_BLOCKLIST:
            # 避免同一条消息同时触发指令和关键词监听。
            return
        if "赛季" not in raw:
            return

        group_id = self._get_group_id(event)
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if group_id and self._is_season_keyword_disabled(umo, group_id):
            return

        if self._guard_access(event):
            return

        try:
            season_info = await self._api.fetch_current_season_info()
        except Exception as exc:
            logger.error(f"赛季时间查询失败: {exc}")
            return

        if self._is_text_output_mode():
            yield self._plain(event, self._format_season_info(season_info))
            if group_id:
                yield self._plain(event, "🔃 关闭本会话赛季关键词回复：/赛季关闭")
            return
        try:
            image_path = self._render_season_info_image(season_info)
            yield self._image(event, image_path)
            if group_id:
                yield self._plain(event, "🔕 关闭本会话赛季关键词回复：/赛季关闭")
            return
        except Exception as exc:
            logger.error(f"赛季关键词图片生成失败，已回退文字输出: {exc}")

        message = self._format_season_info(season_info)
        if group_id:
            message = "\n".join([message, "🔕 关闭本会话赛季关键词回复：/赛季关闭"])
        yield self._plain(event, message)

    @filter.command("赛季关闭")
    async def season_keyword_off(self, event: AstrMessageEvent):
        """关闭本群“赛季”关键词自动回复。"""
        deny = self._guard_access(event, require_group=True)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return
        admin_deny = self._guard_admin(event)
        if admin_deny:
            yield self._plain(event, "\n".join([self._time_line(), admin_deny]))
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield self._plain(event, 
                "\n".join([self._time_line(), "⚠️ 此命令仅适用于群聊，请在群聊中使用"])
            )
            return

        self._set_season_keyword_disabled(
            str(getattr(event, "unified_msg_origin", "") or ""), True
        )
        yield self._plain(event, 
            "\n".join([self._time_line(), "🔕 已关闭本会话赛季关键词自动回复"])
        )

    @filter.command("赛季开启")
    async def season_keyword_on(self, event: AstrMessageEvent):
        """开启本群“赛季”关键词自动回复。"""
        deny = self._guard_access(event, require_group=True)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return
        admin_deny = self._guard_admin(event)
        if admin_deny:
            yield self._plain(event, "\n".join([self._time_line(), admin_deny]))
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield self._plain(event, 
                "\n".join([self._time_line(), "⚠️ 此命令仅适用于群聊，请在群聊中使用"])
            )
            return

        self._set_season_keyword_disabled(
            str(getattr(event, "unified_msg_origin", "") or ""), False
        )
        yield self._plain(event, 
            "\n".join([self._time_line(), "✅ 已开启本会话赛季关键词自动回复"])
        )

    @filter.command("apexrankwatch")
    async def apexrankwatch(
        self,
        event: AstrMessageEvent,
        player_name: str = "",
        platform: str = "",
        watch_mode: str = WATCH_MODE_NOTIFY,
    ):
        """监控本群玩家，并在当前 UMO 订阅排位变化；个人查询绑定使用 /apex绑定。"""
        watch_mode = normalize_watch_mode(watch_mode)
        watch_label = self._watch_mode_label(watch_mode)
        deny = self._guard_access(event, require_group=True)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        requested_player_name, platform = self._parse_player_platform(event, player_name, platform)
        alias_info = self._resolve_player_alias_info(requested_player_name, platform)
        player_name, platform = alias_info.player_name, alias_info.platform
        if not player_name:
            yield self._plain(event, 
                "\n".join(
                    [self._time_line(), "⚠️ 请提供要监控的玩家名称，例如: /apexrankwatch PlayerName"]
                )
            )
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield self._plain(event, 
                "\n".join([self._time_line(), "⚠️ 此命令仅适用于群聊，请在群聊中使用"])
            )
            return

        blocked_name = self._blocked_lookup_name(requested_player_name, player_name)
        if blocked_name:
            yield self._plain(event, 
                "\n".join(
                    [
                        self._time_line(),
                        f"⛔ 该ID（{blocked_name}）已被管理员加入黑名单，禁止监控",
                    ]
                )
            )
            return

        if not self._config.api_key:
            yield self._plain(event, self._missing_api_key_text())
            return

        identifier, use_uid = self._parse_identifier(player_name)
        if not identifier:
            yield self._plain(
                event,
                "\n".join([self._time_line(), "⚠️ 请提供有效的玩家名称或 UID"]),
            )
            return

        try:
            player_data, used_platform = await self._api.fetch_player_stats_auto(
                identifier, platform, use_uid
            )
        except PlayerNotFoundError:
            yield self._plain(event, 
                "\n".join(
                    [
                        self._time_line(),
                        "⚠️ 未找到该玩家，请检查名称或指定平台",
                    ]
                )
            )
            return
        except ApexApiError as exc:
            logger.error(f"添加群监控失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("添加监控", exc))
            return
        except Exception as exc:
            logger.error(f"添加群监控失败: {exc}")
            yield self._plain(event, self._api_request_failed_text("添加监控"))
            return

        if player_data.rank_score < self._config.min_valid_score:
            yield self._plain(event, 
                "\n".join(
                    [
                        self._time_line(),
                        f"⚠️ 查询到 {player_name} 的分数为 {player_data.rank_score}，低于最小有效分数 {self._config.min_valid_score}，可能是API错误，请稍后再试",
                    ]
                )
            )
            return

        blocked_name = self._blocked_lookup_name(player_data.name, f"uid:{player_data.uid}")
        if blocked_name:
            yield self._plain(event, "\n".join([self._time_line(), f"⛔ 该ID（{blocked_name}）已被管理员加入黑名单，禁止监控"]))
            return
        normalized_platform = self._normalize_platform(used_platform)
        player_key = self._build_player_key(identifier, normalized_platform, use_uid)
        group = self._store.ensure_group(group_id, event.unified_msg_origin)
        # A newly discovered UMO only subscribes the player requested here.
        subscriptions = getattr(self, "_runtime_session_subscriptions", {})
        if event.unified_msg_origin not in subscriptions:
            subscriptions[event.unified_msg_origin] = {}
            self._runtime_session_subscriptions = subscriptions
        display_name = self._display_name_with_alias(
            alias_info.alias_display if alias_info.matched else "",
            player_data.name,
        )

        if player_key in group.players:
            existing_record = group.players[player_key]
            self._subscribe_session_player(event.unified_msg_origin, player_key, watch_mode)
            old_mode = self._watch_mode(existing_record)
            if old_mode != watch_mode:
                existing_record.watch_mode = watch_mode
                self._store.save()
                yield self._plain(
                    event,
                    "\n".join(
                        [
                            self._time_line(),
                            f"✅ 已将 {display_name} 的统计模式更新为 {watch_label}",
                        ]
                    ),
                )
                return
            yield self._plain(event, f"本群已经在监控 {display_name} 的排名变化了（{watch_label}）")
            return

        record = PlayerRecord(
            player_name=player_data.name,
            platform=normalized_platform,
            lookup_id=identifier,
            use_uid=use_uid,
            rank_score=player_data.rank_score,
            rank_name=player_data.rank_name,
            rank_div=player_data.rank_div,
            global_rank_percent=player_data.global_rank_percent,
            selected_legend=player_data.selected_legend,
            legend_kills_percent=(
                player_data.legend_kills_rank.global_percent
                if player_data.legend_kills_rank
                else ""
            ),
            last_checked=now_epoch_ms(),
            display_alias=alias_info.alias_display if alias_info.matched else "",
            alias_target=alias_info.alias_target if alias_info.matched else "",
            watch_mode=watch_mode,
        )
        self._store.set_player(group_id, player_key, record)
        self._subscribe_session_player(event.unified_msg_origin, player_key, watch_mode)
        self._store.save()

        if watch_mode == WATCH_MODE_NOTIFY:
            await self._send_active_message(
                event.unified_msg_origin, f"✅ 测试消息: 已添加对 {display_name} 的排名监控"
            )

        player_data.platform = normalized_platform
        self._apply_alias_to_player_data(player_data, alias_info)
        success_text = "\n".join(
            [
                self._time_line(),
                f"✅ 成功添加对 {display_name} 的排名统计",
                f"🧾 模式: {watch_label}",
                f"🖥️ 平台: {self._format_platform(normalized_platform)}",
                f"🏆 当前排名: {self._get_rank_display_text(player_data)}",
            ]
        )
        if self._is_text_output_mode():
            yield self._plain(event, success_text)
            return
        try:
            image_path = self._render_monitor_added_image(
                player_data, normalized_platform, watch_mode
            )
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"监控添加确认图片生成失败，已回退文字输出: {exc}")
            yield self._plain(event, success_text)

    @filter.command("apexranklist")
    async def apexranklist(self, event: AstrMessageEvent):
        """查看本群正在监控的 Apex 玩家列表。"""
        deny = self._guard_access(event, require_group=True)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield self._plain(event, 
                "\n".join([self._time_line(), "⚠️ 此命令仅适用于群聊，请在群聊中使用"])
            )
            return

        group = self._store.get_group(group_id)
        if not group or not group.players:
            yield self._plain(event, 
                "\n".join([self._time_line(), "ℹ️ 本群目前没有监控任何玩家的排名"])
            )
            return

        response_lines = self._build_rank_watch_list_lines(group.players.values())
        if self._is_text_output_mode():
            yield self._plain(event, "\n".join(response_lines))
            return
        try:
            image_path = self._render_rank_watch_list_image(group.players.values())
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"群监控列表图片生成失败，已回退文字输出: {exc}")
            yield self._plain(event, "\n".join(response_lines))

    @filter.command("分数变化", alias={"apex分数变化", "分数图"})
    async def apex_score_changes(self, event: AstrMessageEvent, raw_args: str = ""):
        """生成本群持续视奸和持续记录玩家的分数变化高清长图。"""
        deny = self._guard_access(event, require_group=True)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield self._plain(
                event,
                "\n".join([self._time_line(), "⚠️ 此命令仅适用于群聊，请在群聊中使用"]),
            )
            return

        group = self._store.get_group(group_id)
        if not group or not group.players:
            yield self._plain(
                event,
                "\n".join(
                    [
                        self._time_line(),
                        "ℹ️ 本群暂无统计玩家，请先使用 /持续视奸 或 /持续记录 添加玩家",
                    ]
                ),
            )
            return

        player_name, platform, limit = self._parse_score_change_args(event, raw_args)
        players, error = self._resolve_score_change_players(group, player_name, platform)
        if error:
            yield self._plain(event, "\n".join([self._time_line(), error]))
            return
        if not players:
            yield self._plain(
                event,
                "\n".join([self._time_line(), "ℹ️ 当前没有可展示的统计玩家"]),
            )
            return

        text = self._format_score_change_text(players, limit)
        if self._is_text_output_mode():
            yield self._plain(event, text)
            return

        try:
            image_path = self._render_score_change_chart(players, limit)
            yield self._image(event, image_path)
        except Exception as exc:
            logger.error(f"分数变化长图生成失败，已回退文字输出: {exc}")
            yield self._plain(event, text)

    @filter.command("apexrankremove")
    async def apexrankremove(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """删除本群玩家监控及其所有 UMO 订阅；单会话退订请使用 Dashboard。"""
        deny = self._guard_access(event, require_group=True)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return

        player_name, platform = self._parse_player_platform(event, player_name, platform)
        alias_info = self._resolve_player_alias_info(player_name, platform)
        player_name, platform = alias_info.player_name, alias_info.platform
        display_name = self._display_name_with_alias(
            alias_info.alias_display if alias_info.matched else "",
            player_name,
        )
        if not player_name:
            yield self._plain(event, 
                "\n".join(
                    [self._time_line(), "⚠️ 请提供要移除监控的玩家名称，例如: /apexrankremove PlayerName"]
                )
            )
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield self._plain(event, 
                "\n".join([self._time_line(), "⚠️ 此命令仅适用于群聊，请在群聊中使用"])
            )
            return
        identifier, use_uid = self._parse_identifier(player_name)
        if not identifier:
            yield self._plain(
                event,
                "\n".join([self._time_line(), "⚠️ 请提供有效的玩家名称或 UID"]),
            )
            return

        group = self._store.get_group(group_id)
        if not group:
            yield self._plain(event, 
                "\n".join(
                    [self._time_line(), f"ℹ️ 本群没有监控 {display_name} 的排名"]
                )
            )
            return

        lookup_name = f"uid:{identifier}" if use_uid else identifier
        player_key = self._find_player_key(group, lookup_name, platform, use_uid)
        if player_key == "__MULTI__":
            yield self._plain(event, 
                "\n".join(
                    [
                        self._time_line(),
                        "⚠️ 检测到同名多平台监控，请指定平台，例如: /apexrankremove PlayerName pc",
                    ]
                )
            )
            return
        if not player_key or not self._store.remove_player(group_id, player_key):
            yield self._plain(event, 
                "\n".join(
                    [self._time_line(), f"ℹ️ 本群没有监控 {display_name} 的排名"]
                )
            )
            return

        self._unsubscribe_player_everywhere(group_id, player_key)
        self._store.save()
        yield self._plain(event, 
            "\n".join([self._time_line(), f"✅ 已移除本群对 {display_name} 的排名监控"])
        )

    @filter.command("apexblacklist")
    async def apexblacklist(self, event: AstrMessageEvent, action: str = "", player_name: str = ""):
        """管理 Apex 玩家全局黑名单（查询与监控推送），支持 add、remove、list、clear。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return
        admin_deny = self._guard_admin(event)
        if admin_deny:
            yield self._plain(event, "\n".join([self._time_line(), admin_deny]))
            return

        action, player_name = self._parse_blacklist_command(event, action, player_name)
        action_lower = action.lower()
        config_blacklist = self._get_config_blacklist()

        def _format_items(items: set[str]) -> str:
            if not items:
                return "无"
            return "，".join(sorted(items))

        if not action or action_lower in {"help", "?", "h", "帮助"}:
            lines = [
                self._time_line(),
                "🚫 Apex 黑名单管理（管理员）",
                "用法：/apexblacklist add <玩家ID>",
                "用法：/apexblacklist remove <玩家ID>",
                "用法：/apexblacklist list",
                "用法：/apexblacklist clear",
                f"配置黑名单：{_format_items(config_blacklist)}",
                f"动态黑名单：{_format_items(self._runtime_blacklist)}",
                "提示：配置黑名单需在插件配置中修改，动态黑名单可用本命令管理",
            ]
            yield self._plain(event, "\n".join(lines))
            return

        if action_lower in {"list", "ls", "查看", "列表"}:
            lines = [
                self._time_line(),
                "🚫 Apex 黑名单列表",
                f"配置黑名单：{_format_items(config_blacklist)}",
                f"动态黑名单：{_format_items(self._runtime_blacklist)}",
            ]
            yield self._plain(event, "\n".join(lines))
            return

        if action_lower in {"clear", "清空", "clean"}:
            if not self._runtime_blacklist:
                yield self._plain(event, "\n".join([self._time_line(), "ℹ️ 动态黑名单已为空"]))
                return
            self._runtime_blacklist.clear()
            self._save_settings()
            yield self._plain(event, "\n".join([self._time_line(), "✅ 已清空动态黑名单"]))
            return

        items = self._split_blacklist_items(player_name)
        if not items:
            yield self._plain(event, 
                "\n".join([self._time_line(), "⚠️ 请提供玩家ID，例如：/apexblacklist add PlayerName"])
            )
            return

        if action_lower in {"add", "+", "新增", "添加", "加入"}:
            added = []
            existed_config = []
            existed_runtime = []
            for raw in items:
                name = self._normalize_lookup_value(raw)
                if not name:
                    continue
                if name in config_blacklist:
                    existed_config.append(name)
                    continue
                if name in self._runtime_blacklist:
                    existed_runtime.append(name)
                    continue
                self._runtime_blacklist.add(name)
                added.append(name)
            if added:
                self._save_settings()
            lines = [
                self._time_line(),
                f"✅ 已添加 {len(added)} 个黑名单ID",
            ]
            if added:
                lines.append(f"新增：{_format_items(set(added))}")
            if existed_config:
                lines.append(f"已在配置黑名单：{_format_items(set(existed_config))}")
            if existed_runtime:
                lines.append(f"已在动态黑名单：{_format_items(set(existed_runtime))}")
            yield self._plain(event, "\n".join(lines))
            return

        if action_lower in {"remove", "del", "delete", "rm", "-", "移除", "删除"}:
            removed = []
            not_found = []
            in_config = []
            for raw in items:
                name = self._normalize_lookup_value(raw)
                if not name:
                    continue
                if name in self._runtime_blacklist:
                    self._runtime_blacklist.remove(name)
                    removed.append(name)
                elif name in config_blacklist:
                    in_config.append(name)
                else:
                    not_found.append(name)
            if removed:
                self._save_settings()
            lines = [
                self._time_line(),
                f"✅ 已移除 {len(removed)} 个动态黑名单ID",
            ]
            if removed:
                lines.append(f"移除：{_format_items(set(removed))}")
            if in_config:
                lines.append(f"配置黑名单需在配置中删除：{_format_items(set(in_config))}")
            if not_found:
                lines.append(f"未找到：{_format_items(set(not_found))}")
            yield self._plain(event, "\n".join(lines))
            return

        yield self._plain(event, 
            "\n".join([self._time_line(), "⚠️ 未识别的操作，请使用 add/remove/list/clear"])
        )

    @filter.command("apexalias", alias={"apex别名"})
    async def apexalias(
        self,
        event: AstrMessageEvent,
        action: str = "",
        alias: str = "",
        target: str = "",
    ):
        """管理 Apex 查询别名，支持 add、remove、list、clear。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return
        if not self._aliases_enabled():
            yield self._plain(event, "\n".join([self._time_line(), "⚠️ 别名功能已关闭"]))
            return
        if getattr(self._config, "alias_admin_only", True):
            admin_deny = self._guard_admin(event)
            if admin_deny:
                yield self._plain(event, "\n".join([self._time_line(), admin_deny]))
                return

        action, alias, target = self._parse_alias_command(event, action, alias, target)
        action_lower = action.lower()
        config_aliases = self._get_config_player_aliases()
        config_display_names = self._get_config_player_alias_display_names()
        runtime_aliases = getattr(self, "_runtime_player_aliases", {})
        runtime_display_names = self._get_runtime_player_alias_display_names()

        if not action or action_lower in {"help", "?", "h", "帮助"}:
            alias_entries = self._alias_list_entries(
                config_aliases,
                runtime_aliases,
                config_display_names,
                runtime_display_names,
            )
            lines = [
                self._time_line(),
                "🔖 Apex 查询别名管理",
                "用法：/apexalias add <别名> <玩家名|uid:...> [平台]",
                "用法：/apexalias remove <别名>",
                "用法：/apexalias list",
                "用法：/apexalias clear",
                "例：/apexalias add 测试 uid:1234",
                f"有效别名：{self._format_effective_player_aliases()}",
                f"配置别名：{self._format_player_aliases(config_aliases, config_display_names, set(runtime_aliases))}",
                f"动态别名：{self._format_player_aliases(runtime_aliases, runtime_display_names)}",
                "绑定列表请使用：/apex绑定 list",
            ]
            if self._is_image_output_mode():
                try:
                    image_path = self._render_alias_list_image(alias_entries)
                    yield self._image(event, image_path)
                    return
                except Exception as exc:
                    logger.error(f"查询别名管理图片生成失败，已回退文字输出: {exc}")
            yield self._plain(event, "\n".join(lines))
            return

        if action_lower in {"list", "ls", "查看", "列表"}:
            alias_entries = self._alias_list_entries(
                config_aliases,
                runtime_aliases,
                config_display_names,
                runtime_display_names,
            )
            lines = [
                self._time_line(),
                "🔖 Apex 查询别名列表",
                f"有效别名：{self._format_effective_player_aliases()}",
                f"配置别名：{self._format_player_aliases(config_aliases, config_display_names, set(runtime_aliases))}",
                f"动态别名：{self._format_player_aliases(runtime_aliases, runtime_display_names)}",
            ]
            if self._is_image_output_mode():
                try:
                    image_path = self._render_alias_list_image(alias_entries)
                    yield self._image(event, image_path)
                    return
                except Exception as exc:
                    logger.error(f"查询别名列表图片生成失败，已回退文字输出: {exc}")
            yield self._plain(event, "\n".join(lines))
            return

        if action_lower in {"clear", "清空", "clean"}:
            if not runtime_aliases:
                yield self._plain(event, "\n".join([self._time_line(), "ℹ️ 动态别名已为空"]))
                return
            runtime_aliases.clear()
            self._runtime_player_alias_display_names = {}
            self._save_settings()
            yield self._plain(event, "\n".join([self._time_line(), "✅ 已清空动态别名"]))
            return

        alias_key = self._normalize_alias_key(alias)
        if not alias_key:
            yield self._plain(event, "\n".join([self._time_line(), "⚠️ 请提供别名，例如：/apexalias add 测试 uid:1234"]))
            return

        if action_lower in {"add", "set", "+", "新增", "添加", "设置"}:
            if not target:
                yield self._plain(event, "\n".join([self._time_line(), "⚠️ 请提供别名目标，例如：/apexalias add 测试 uid:1234"]))
                return
            runtime_aliases[alias_key] = target.strip()
            runtime_display_names[alias_key] = alias.strip()
            self._runtime_player_alias_display_names = runtime_display_names
            self._save_settings()
            lines = [
                self._time_line(),
                f"✅ 已添加别名：{runtime_display_names[alias_key]} => {runtime_aliases[alias_key]}",
            ]
            if alias_key in config_aliases:
                lines.append("提示：动态别名会优先覆盖面板配置中的同名别名")
            yield self._plain(event, "\n".join(lines))
            return

        if action_lower in {"remove", "del", "delete", "rm", "-", "移除", "删除"}:
            if alias_key in runtime_aliases:
                removed = runtime_aliases.pop(alias_key)
                removed_display = runtime_display_names.pop(alias_key, alias_key)
                self._runtime_player_alias_display_names = runtime_display_names
                self._save_settings()
                yield self._plain(
                    event,
                    "\n".join([self._time_line(), f"✅ 已移除动态别名：{removed_display} => {removed}"]),
                )
                return
            if alias_key in config_aliases:
                yield self._plain(
                    event,
                    "\n".join([self._time_line(), "⚠️ 该别名来自面板配置，请在插件配置中删除"]),
                )
                return
            yield self._plain(event, "\n".join([self._time_line(), f"ℹ️ 未找到别名：{alias_key}"]))
            return

        yield self._plain(event, "\n".join([self._time_line(), "⚠️ 未识别的操作，请使用 add/remove/list/clear"]))

    @filter.command("apex取消别名", alias={"apexunalias", "取消别名"})
    async def apexunalias(self, event: AstrMessageEvent, alias: str = ""):
        """移除一个动态查询别名；配置面板里的别名需要到配置中删除。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return
        if not self._aliases_enabled():
            yield self._plain(event, "\n".join([self._time_line(), "⚠️ 别名功能已关闭"]))
            return
        if getattr(self._config, "alias_admin_only", True):
            admin_deny = self._guard_admin(event)
            if admin_deny:
                yield self._plain(event, "\n".join([self._time_line(), admin_deny]))
                return

        raw_alias = (self._extract_command_args(event) or str(alias or "")).strip()
        alias_key = self._normalize_alias_key(raw_alias)
        if not alias_key:
            yield self._plain(event, "\n".join([self._time_line(), "⚠️ 请提供别名，例如：/apex取消别名 测试"]))
            return

        config_aliases = self._get_config_player_aliases()
        runtime_aliases = getattr(self, "_runtime_player_aliases", {})
        runtime_display_names = self._get_runtime_player_alias_display_names()

        if alias_key in runtime_aliases:
            removed = runtime_aliases.pop(alias_key)
            removed_display = runtime_display_names.pop(alias_key, alias_key)
            self._runtime_player_alias_display_names = runtime_display_names
            self._save_settings()
            yield self._plain(
                event,
                "\n".join([self._time_line(), f"✅ 已移除动态别名：{removed_display} => {removed}"]),
            )
            return
        if alias_key in config_aliases:
            yield self._plain(
                event,
                "\n".join([self._time_line(), "⚠️ 该别名来自面板配置，请在插件配置中删除"]),
            )
            return
        yield self._plain(event, "\n".join([self._time_line(), f"ℹ️ 未找到别名：{alias_key}"]))

    @filter.command("apex绑定", alias={"apexbind"})
    async def apexbind(self, event: AstrMessageEvent, target: str = ""):
        """绑定当前 QQ 用户默认查询的 Apex 玩家，绑定后可直接使用 /apex查询。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return
        if not self._aliases_enabled():
            yield self._plain(event, "\n".join([self._time_line(), "⚠️ 别名功能已关闭"]))
            return

        raw_target = (self._extract_command_args(event) or str(target or "")).strip()
        action = raw_target.strip().lower()
        bindings = self._get_user_bindings_for_event(event)

        if action in {"list", "ls", "查看", "列表"}:
            yield self._binding_list_response(event, bindings)
            return

        user_id = self._get_user_id(event)
        if not user_id:
            yield self._plain(event, "\n".join([self._time_line(), "⚠️ 无法识别当前用户，暂不能绑定"]))
            return

        if action in {"remove", "del", "delete", "rm", "clear", "解绑", "清除", "删除", "移除"}:
            yield self._remove_current_user_binding_response(event)
            return

        if not raw_target:
            current = bindings.get(user_id, "")
            if current:
                yield self._plain(
                    event,
                    "\n".join([self._time_line(), f"🔖 你的 Apex 查询绑定：{current}"]),
                )
            else:
                yield self._plain(
                    event,
                    "\n".join(
                        [
                            self._time_line(),
                            "ℹ️ 你还没有绑定 Apex 查询目标",
                            "用法：/apex绑定 <玩家名|uid:...> [平台]",
                            "例：/apex绑定 uid:1234  或  /apex绑定 PlayerName pc",
                        ]
                    ),
                )
            return

        normalized_target = self._normalize_binding_target(raw_target)
        identifier_text, _ = self._parse_alias_target_platform(normalized_target)
        identifier, _ = self._parse_identifier(identifier_text)
        if not identifier:
            yield self._plain(
                event,
                "\n".join([self._time_line(), "⚠️ 请提供有效的玩家名或 UID，例如：/apex绑定 uid:1234"]),
            )
            return

        self._set_user_binding_for_event(event, user_id, normalized_target)
        self._save_settings()
        scope = "本群内" if self._get_group_id(event) else ""
        yield self._plain(
            event,
            "\n".join(
                [
                    self._time_line(),
                    f"✅ 已绑定{scope}你的 Apex 查询目标：{normalized_target}",
                    "之后直接发送 /apex查询 即可查询该玩家",
                ]
            ),
        )

    @filter.command("apex绑定列表", alias={"apexbindlist"})
    async def apexbindinglist(self, event: AstrMessageEvent):
        """查看 QQ 用户与 Apex 查询目标的个人绑定列表。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return
        if not self._aliases_enabled():
            yield self._plain(event, "\n".join([self._time_line(), "⚠️ 别名功能已关闭"]))
            return

        bindings = self._get_user_bindings_for_event(event)
        yield self._binding_list_response(event, bindings)

    @filter.command("apex解绑", alias={"apexunbind", "取消绑定", "解除绑定"})
    async def apexunbind(self, event: AstrMessageEvent):
        """解除当前 QQ 用户自己的 Apex 默认查询绑定。"""
        deny = self._guard_access(event)
        if deny:
            yield self._plain(event, "\n".join([self._time_line(), deny]))
            return
        if not self._aliases_enabled():
            yield self._plain(event, "\n".join([self._time_line(), "⚠️ 别名功能已关闭"]))
            return

        yield self._remove_current_user_binding_response(event)

    @filter.command("apex监控")
    async def apexrankwatch_cn(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """中文别名：将玩家加入本群排位分数持续监控。"""
        async for result in self.apexrankwatch(event, player_name, platform):
            yield result

    @filter.command("apex列表")
    async def apexranklist_cn(self, event: AstrMessageEvent):
        """中文别名：查看本群正在监控的 Apex 玩家列表。"""
        async for result in self.apexranklist(event):
            yield result

    @filter.command("apex移除")
    async def apexrankremove_cn(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """中文别名：从本群移除指定玩家的排位监控。"""
        async for result in self.apexrankremove(event, player_name, platform):
            yield result

    @filter.command("apex查询")
    async def apexrank_query_cn(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """中文别名：查询指定 Apex 玩家段位、分数和在线状态。"""
        async for result in self.apexrank(event, player_name, platform):
            yield result

    @filter.command("视奸")
    async def apexrank_query_cn_alt(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """中文别名：查询指定 Apex 玩家段位、分数和在线状态。"""
        async for result in self.apexrank(event, player_name, platform):
            yield result

    @filter.command("持续视奸")
    async def apexrankwatch_cn_alt(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """中文别名：将玩家加入本群排位分数持续监控。"""
        async for result in self.apexrankwatch(event, player_name, platform):
            yield result

    @filter.command("apexrankrecord", alias={"持续记录"})
    async def apexrankrecord(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """将玩家加入本群排位分数持续记录，仅记录变化但不主动通报。"""
        async for result in self.apexrankwatch(
            event, player_name, platform, WATCH_MODE_RECORD
        ):
            yield result

    @filter.command("持续视奸列表")
    async def apexranklist_cn_alt(self, event: AstrMessageEvent):
        """中文别名：查看本群持续视奸和持续记录玩家列表。"""
        async for result in self.apexranklist(event):
            yield result

    @filter.command("取消持续视奸")
    async def apexrankremove_cn_alt(self, event: AstrMessageEvent, player_name: str = "", platform: str = ""):
        """中文别名：从本群移除指定玩家的排位监控。"""
        async for result in self.apexrankremove(event, player_name, platform):
            yield result


    @filter.command("apex赛季")
    async def apexseason_cn(self, event: AstrMessageEvent, season: str = ""):
        """中文别名：查询当前 Apex 排位分段的结束时间和剩余时间。"""
        async for result in self.apexseason(event, season):
            yield result

    @filter.command("新赛季")
    async def apexseason_new_cn(self, event: AstrMessageEvent, season: str = ""):
        """中文别名：查询当前 Apex 排位分段的结束时间和剩余时间。"""
        async for result in self.apexseason(event, season):
            yield result

    @filter.command("apex测试")
    async def apextest_cn(self, event: AstrMessageEvent):
        """中文别名：测试插件运行状态和主动消息推送能力。"""
        async for result in self.apextest(event):
            yield result

    @filter.command("apex黑名单")
    async def apexblacklist_cn(self, event: AstrMessageEvent, action: str = "", player_name: str = ""):
        """中文别名：管理 Apex 玩家全局黑名单（查询与监控推送）。"""
        async for result in self.apexblacklist(event, action, player_name):
            yield result

    @filter.command("不准视奸")
    async def apexblacklist_cn_alt(self, event: AstrMessageEvent, action: str = "", player_name: str = ""):
        """中文别名：管理 Apex 玩家全局黑名单（查询与监控推送）。"""
        async for result in self.apexblacklist(event, action, player_name):
            yield result

    @filter.command("apexban")
    async def apexblacklist_en_alt(self, event: AstrMessageEvent, action: str = "", player_name: str = ""):
        """管理 Apex 玩家全局黑名单（查询与监控推送）的英文别名。"""
        async for result in self.apexblacklist(event, action, player_name):
            yield result

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            started_at = datetime.now(timezone.utc).isoformat()
            self._webui_poll_state.update(
                {"status": "running", "last_started": started_at, "last_error": ""}
            )
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"轮询任务执行失败: {exc}")
                self._webui_poll_state.update(
                    {"status": "failed", "last_error": str(exc)}
                )
            else:
                self._webui_poll_state.update(
                    {
                        "status": "success",
                        "last_finished": datetime.now(timezone.utc).isoformat(),
                    }
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._config.check_interval * 60
                )
            except asyncio.TimeoutError:
                continue

    async def _daily_map_learning_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._daily_map_learning_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"全天地图学习任务执行失败: {exc}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._DAILY_MAP_LEARNING_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                continue

    async def _poll_fetch_player(
        self,
        group_id: str,
        group_origin: str | tuple[str, ...],
        player: PlayerRecord,
        platform: str,
        lookup_id: str,
        use_uid: bool,
        session_modes: tuple[tuple[str, str], ...] = (),
    ) -> PollFetchResult:
        group_origins = (
            tuple(item for item in group_origin if item)
            if isinstance(group_origin, (tuple, list))
            else ((group_origin,) if group_origin else ())
        )
        primary_origin = group_origins[0] if group_origins else ""
        async with self._poll_semaphore:
            try:
                player_data, _ = await self._api.fetch_player_stats_auto(
                    lookup_id, platform, use_uid
                )
                return PollFetchResult(
                    group_id=group_id,
                    group_origin=primary_origin,
                    group_origins=group_origins,
                    player=player,
                    platform=platform,
                    session_modes=session_modes,
                    player_data=player_data,
                )
            except PlayerNotFoundError:
                return PollFetchResult(
                    group_id=group_id,
                    group_origin=primary_origin,
                    group_origins=group_origins,
                    player=player,
                    platform=platform,
                    session_modes=session_modes,
                    not_found=True,
                )
            except Exception as exc:
                return PollFetchResult(
                    group_id=group_id,
                    group_origin=primary_origin,
                    group_origins=group_origins,
                    player=player,
                    platform=platform,
                    session_modes=session_modes,
                    error=exc,
                )

    def _should_skip_poll_player(self, player: PlayerRecord) -> bool:
        lookup_id = getattr(player, "lookup_id", "") or ""
        candidates = [
            player.player_name,
            lookup_id,
            getattr(player, "alias_target", ""),
            getattr(player, "display_alias", ""),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            if self._is_blacklisted(candidate):
                return True
        return False

    def _archive_push(self, entry: dict, image_path: Path | None = None) -> None:
        archive = getattr(self, "_push_archive", None)
        raw_config = getattr(self, "config", {})
        if archive is None or not coerce_bool(
            raw_config.get("push_archive_enabled", True), True
        ):
            return
        try:
            archive.append(entry, image_path)
        except Exception as exc:
            logger.error(f"保存推送归档失败: {exc}")

    def _cleanup_push_archive(self) -> None:
        archive = getattr(self, "_push_archive", None)
        today = datetime.now().astimezone().date()
        if archive is None or getattr(self, "_archive_cleanup_day", None) == today:
            return
        raw_config = getattr(self, "config", {})
        retention = max(
            1,
            min(365, coerce_int(raw_config.get("push_archive_retention_days", 30), 30)),
        )
        try:
            archive.cleanup(retention, today)
            self._archive_cleanup_day = today
        except Exception as exc:
            logger.error(f"清理推送归档失败: {exc}")

    async def _poll_once(self) -> None:
        self._cleanup_push_archive()
        if not self._config.api_key:
            return

        poll_targets = []
        for group_id, group in self._store.iter_groups():
            session_players: dict[int, list[tuple[str, str]]] = {}
            for origin, player, mode in self._session_poll_targets(group_id, group):
                session_players.setdefault(id(player), []).append((origin, mode))
            for player_key, player in list(group.players.items()):
                subscribers = session_players.get(id(player), [])
                if not subscribers:
                    continue
                if self._should_skip_poll_player(player):
                    lookup_id = getattr(player, "lookup_id", "") or player.player_name
                    logger.warning(f"跳过黑名单ID的定时检查: {lookup_id}")
                    continue

                platform = getattr(player, "platform", "PC") or "PC"
                use_uid = bool(getattr(player, "use_uid", False))
                lookup_id = getattr(player, "lookup_id", "") or player.player_name
                poll_targets.append(
                    (
                        group_id,
                        tuple(origin for origin, _ in subscribers),
                        player,
                        platform,
                        lookup_id,
                        use_uid,
                        tuple(subscribers),
                    )
                )

        if not poll_targets:
            return

        queue: asyncio.Queue = asyncio.Queue()
        for target in poll_targets:
            queue.put_nowait(target)

        results: list[PollFetchResult] = []

        async def worker() -> None:
            while True:
                try:
                    target = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await self._poll_fetch_player(*target)
                    results.append(result)
                finally:
                    queue.task_done()

        worker_count = min(self._poll_concurrency, len(poll_targets))
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        try:
            await queue.join()
        finally:
            await asyncio.gather(*workers, return_exceptions=True)

        dirty = False
        fatal_api_error: ApexApiError | None = None
        for result in results:
            player = result.player
            platform = result.platform
            result_player_key = ""
            if self._should_skip_poll_player(player):
                continue
            # Re-read permissions after network I/O so a paused/removed subscription cannot send.
            if hasattr(self._store, "get_group"):
                group = self._store.get_group(result.group_id)
                if group is None or not any(
                    value is player for value in group.players.values()
                ):
                    continue
                result_player_key = next(
                    (key for key, value in group.players.items() if value is player), ""
                )
                result.session_modes = tuple(
                    (umo, mode)
                    for umo, candidate, mode in self._session_poll_targets(
                        result.group_id, group
                    )
                    if candidate is player
                )
                if not result.session_modes:
                    continue
            if result.not_found or result.error:
                reason = "玩家未找到" if result.not_found else str(result.error)
                for origin, mode in result.session_modes:
                    self._archive_push(
                        {
                            "group_id": str(result.group_id),
                            "umo": origin,
                            "player": self._record_display_name(player),
                            "platform": self._format_platform(platform),
                            "mode": mode,
                            "status": "fetch_failed",
                            "error": reason[:500],
                            "message": "本次未获取到有效玩家数据，保留原分数和历史。",
                        }
                    )
            if result.not_found:
                logger.warning(f"玩家 {player.player_name} 在平台 {platform} 未找到")
                continue
            if result.error:
                if isinstance(result.error, ApexApiError):
                    fatal_api_error = fatal_api_error or result.error
                    continue
                logger.error(f"检查玩家 {player.player_name} 排名失败: {result.error}")
                continue
            player_data = result.player_data
            if player_data is None:
                continue
            if self._blocked_lookup_name(player_data.name, f"uid:{player_data.uid}"):
                continue
            self._apply_record_alias_to_player_data(player_data, player)

            new_score = player_data.rank_score
            old_score = player.rank_score
            is_valid_score = new_score >= self._config.min_valid_score
            is_abnormal_drop = is_score_drop_abnormal(old_score, new_score)
            is_season_reset = is_likely_season_reset(old_score, new_score)

            if is_valid_score and not is_abnormal_drop and new_score != old_score:
                diff = new_score - old_score
                diff_text = f"上升 {diff}" if diff > 0 else f"下降 {abs(diff)}"
                checked_at = now_epoch_ms()
                self._append_score_history(
                    player,
                    player_data=player_data,
                    old_score=old_score,
                    is_season_reset=is_season_reset,
                    captured_at=checked_at,
                )

                player.rank_score = new_score
                player.rank_name = player_data.rank_name
                player.rank_div = player_data.rank_div
                player.global_rank_percent = player_data.global_rank_percent
                player.selected_legend = player_data.selected_legend
                player.legend_kills_percent = (
                    player_data.legend_kills_rank.global_percent
                    if player_data.legend_kills_rank
                    else ""
                )
                player.last_checked = checked_at
                dirty = True

                date_str = self._now_str()
                new_rank_display = (
                    f"{player.rank_name} {player.rank_div}"
                    if player.rank_div
                    else player.rank_name
                )
                display_name = self._record_display_name(player)

                message_lines = [
                    "📈 Apex 排位分数变化",
                    f"🕒 时间: {date_str}",
                    f"👤 玩家: {display_name}",
                    f"🖥️ 平台: {self._format_platform(platform)}",
                    f"🔢 原分数: {old_score}",
                    f"🔢 当前分数: {new_score}",
                    f"🏆 段位: {new_rank_display}",
                    f"📊 变动: {diff_text} 分",
                ]

                if is_season_reset:
                    message_lines.append(
                        "⚠️ 注意: 检测到大幅度分数下降，可能是赛季重置导致"
                    )

                if (
                    player_data.global_rank_percent
                    and player_data.global_rank_percent != "未知"
                ):
                    message_lines.append(
                        f"🌎 全球排名: {player_data.global_rank_percent}%"
                    )

                if player_data.is_online and player_data.selected_legend:
                    message_lines.append(f"🎮 当前英雄: {player_data.selected_legend}")
                    if player_data.legend_kills_rank:
                        message_lines.append(
                            f"📊 击杀排名: 全球 {player_data.legend_kills_rank.global_percent}%"
                        )

                if player_data.is_online and player_data.current_state:
                    message_lines.append(f"🎯 当前状态: {player_data.current_state}")

                session_modes = result.session_modes or tuple(
                    (origin, self._watch_mode(player))
                    for origin in result.group_origins
                )
                image_path = None
                if any(
                    self._session_output_mode(umo) == "image"
                    for umo, _ in session_modes
                ):
                    try:
                        image_path = self._render_rank_change_image(
                            player_data=player_data,
                            old_score=old_score,
                            new_score=new_score,
                            platform=platform,
                            is_season_reset=is_season_reset,
                        )
                    except Exception as exc:
                        logger.error(f"排位分数变化图片生成失败，已回退文字通知: {exc}")
                for origin, mode in session_modes:
                    if not self._is_group_allowed(
                        str(result.group_id), origin
                    ) or self._should_skip_poll_player(player):
                        continue
                    if hasattr(self._store, "get_group"):
                        current_group = self._store.get_group(result.group_id)
                        current_mode = (
                            next(
                                (
                                    candidate_mode
                                    for umo, candidate, candidate_mode in self._session_poll_targets(
                                        result.group_id, current_group
                                    )
                                    if umo == origin and candidate is player
                                ),
                                None,
                            )
                            if current_group
                            else None
                        )
                        if current_mode is None:
                            continue
                        mode = current_mode
                    sent = False
                    error = ""
                    if mode == WATCH_MODE_NOTIFY:
                        if (
                            image_path is not None
                            and self._session_output_mode(origin) == "image"
                        ):
                            sent = await self._send_active_image(origin, image_path)
                        if not sent:
                            sent = await self._send_active_message(
                                origin, "\n".join(message_lines)
                            )
                        if not sent:
                            error = "发送失败"
                    archive_entry = {
                        "group_id": str(result.group_id),
                        "umo": origin,
                        "player_key": result_player_key,
                        "player": display_name,
                        "platform": self._format_platform(platform),
                        "from_score": old_score,
                        "to_score": new_score,
                        "score_delta": new_score - old_score,
                        "rank": new_rank_display,
                        "mode": mode,
                        "status": "sent"
                        if sent
                        else ("recorded" if mode == WATCH_MODE_RECORD else "failed"),
                        "error": error,
                        "message": "\n".join(message_lines),
                    }
                    self._archive_push(
                        archive_entry,
                        image_path
                        if self._session_output_mode(origin) == "image"
                        else None,
                    )
                if not session_modes:
                    logger.debug(
                        f"群 {result.group_id} 的玩家 {display_name} 没有有效订阅会话"
                    )
            elif not is_valid_score:
                logger.warning(
                    f"玩家 {player.player_name} 的分数 {new_score} 无效，保留原分数 {old_score}"
                )
            elif is_abnormal_drop:
                logger.warning(
                    f"玩家 {player.player_name} 的分数从 {old_score} 下降到 {new_score}，可能是API错误"
                )

        if fatal_api_error is not None:
            logger.error(f"轮询阶段 API 认证/限流失败: {fatal_api_error.user_message}")
        if dirty:
            self._store.save()

    @staticmethod
    def _get_rank_display_text(player_data: ApexPlayerStats) -> str:
        rank_display = (
            f"{player_data.rank_name} {player_data.rank_div}"
            if player_data.rank_div
            else player_data.rank_name
        )
        return f"{rank_display} ({player_data.rank_score}分)"

    def _format_player_rank_text(self, player_data: ApexPlayerStats) -> str:
        date_str = self._now_str()
        rank_display = (
            f"{player_data.rank_name} {player_data.rank_div}"
            if player_data.rank_div
            else player_data.rank_name
        )

        lines = [
            "📊 Apex 段位信息",
            f"🕒 时间: {date_str}",
            f"👤 玩家: {player_data.name}",
        ]
        display_alias = str(getattr(player_data, "display_alias", "") or "").strip()
        alias_target = str(getattr(player_data, "alias_target", "") or "").strip()
        if display_alias:
            lines.append(f"🔖 查询别名: {display_alias}")
            if alias_target:
                lines.append(f"🎯 别名目标: {alias_target}")
        lines.extend(
            [
                f"🖥️ 平台: {self._format_platform(player_data.platform)}",
                f"🆔 UID: {player_data.uid}" if player_data.uid else "🆔 UID: 未知",
                f"🏆 段位: {rank_display}",
                f"🔢 分数: {player_data.rank_score}",
            ]
        )

        if player_data.global_rank_percent and player_data.global_rank_percent != "未知":
            lines.append(f"🌎 全球排名: {player_data.global_rank_percent}%")
        lines.append(f"👑 等级: {player_data.level}")

        if player_data.is_online:
            lines.append("🎮 在线状态: 在线")
            if player_data.selected_legend:
                lines.append(f"🎯 当前英雄: {player_data.selected_legend}")
            if player_data.is_in_lobby_or_match:
                lines.append(f"🎯 当前状态: {player_data.current_state}")
        else:
            lines.append("🎮 在线状态: 离线")

        return "\n".join(lines)

    def _build_apex_help_lines(self) -> list[str]:
        lines = [
            self._time_line(),
            "📋 Apex Rank Watch 帮助（/apexhelp 或 /apex帮助）",
            "——",
            "【查询】",
            "🔎 /apexrank <玩家|uid:...> [平台]  别名：/apex查询 /视奸",
            "🔖 /apex绑定 <玩家|uid:...> [平台]  绑定后 /apex查询 可直接查自己",
            "🔖 /apex绑定 list / /apex绑定列表 / /apex解绑  查看或取消个人绑定",
            "例：/apexrank PlayerName pc",
            "——",
            "【监控（群聊）】",
            "➕ /apexrankwatch <玩家|uid:...> [平台]  别名：/apex监控 /持续视奸",
            "📝 /apexrankrecord <玩家|uid:...> [平台]  别名：/持续记录，只记录不通报",
            "📋 /apexranklist  别名：/apex列表 /持续视奸列表",
            "📈 /分数变化 [玩家|uid:...] [平台] [场次]  生成高清长图，最多 50 次",
            "➖ /apexrankremove <玩家|uid:...> [平台]  别名：/apex移除 /取消持续视奸",
            "——",
            "【信息】",
            "🗓️ /apexseason  别名：/apex赛季 /新赛季（当前排位分段）",
            "🗺️ /map  别名：/地图 /排位地图 /apexmap /apexrankmap（排位地图轮换）",
            "🕘 /全天地图（API 学习确认排位未来 24 小时地图）",
            "🌍 /匹配地图（三人赛地图轮换）",
            "🏆 /apexpredator [平台]  别名：/apex猎杀 /猎杀",
            "关键词：按 UMO 会话控制，消息包含「赛季」时自动回复（/赛季关闭，/赛季开启）",
            "——",
            "【管理】",
            "🚫 /apexblacklist <add|remove|list|clear> <玩家ID>  别名：/apex黑名单 /不准视奸 /apexban",
            "🔖 /apexalias add <别名> <玩家|uid:...> [平台]  别名：/apex别名",
            "🔖 /apexalias remove <别名> / /apex取消别名 <别名>",
            "🔖 /apexalias list 仅展示配置别名和动态别名",
            "🧩 /apex_download  检测中文字体并在缺失时下载字体缓存",
            "——",
            "【参数】",
            "平台：PC / PS4 / X1 / SWITCH（默认 PC；PC 无数据自动尝试其他平台）",
            "UUID：使用 uid: 或 uuid: 前缀（例：/apexrank uid:000000）",
            "分数变化：默认最近 20 次有效变化，可填写 1-50 次；/持续记录 只记录不通报",
            "——",
            f"⏱️ 监控间隔：{getattr(self._config, 'check_interval', 2)} 分钟",
            f"🔻 最小有效分：{getattr(self._config, 'min_valid_score', 1)} 分",
            "⚠️ 异常分数：仅当高分(>1000)掉到接近0分(<10)时判定为异常",
            "🛡️ 权限：群白名单、QQ 黑名单、主人 QQ、私聊开关",
        ]

        try:
            config_blacklist = self._get_config_blacklist()
        except Exception:
            config_blacklist = set()
        runtime_blacklist = getattr(self, "_runtime_blacklist", set())
        total_blacklist = len(config_blacklist) + len(runtime_blacklist)
        if total_blacklist:
            lines.append(
                "⛔ 黑名单说明：配置黑名单 "
                f"{len(config_blacklist)} 个，动态黑名单 {len(runtime_blacklist)} 个"
            )
            lines.append("⛔ 黑名单ID无法被查询或监控")

        query_blocklist = getattr(self._config, "query_blocklist", "")
        if query_blocklist:
            count = len(_split_csv(query_blocklist))
            lines.append(f"⛔ 禁止查询玩家：已设置 {count} 个玩家ID")
        return lines

    def _render_apex_help_image(self) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成帮助图片")

        output_dir = self._generated_card_output_dir("help_cards")
        output_dir.mkdir(parents=True, exist_ok=True)
        image = self._build_apex_help_card()
        output_path = output_dir / f"apex_help_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_generated_images(output_dir, "apex_help_*.png", keep=8)
        return output_path

    def _build_apex_help_card(self):
        width, height = self._HELP_CARD_SIZE
        canvas = Image.new("RGBA", (width, height), (7, 8, 11, 255))
        draw = ImageDraw.Draw(canvas)
        self._draw_rank_change_background(canvas)
        self._draw_rank_change_outer_frame(draw, width, height)
        self._draw_help_card_header(canvas, draw)

        for box, title, rows in self._help_card_sections():
            self._draw_help_section(draw, box, title, rows)
        return canvas.convert("RGB")

    def _help_card_sections(self) -> list[tuple[tuple[int, int, int, int], str, list[tuple[str, str]]]]:
        return [
            (
                (54, 250, 1068, 500),
                "查询",
                [
                    ("🔎 /apexrank 玩家 [平台]", "查询玩家段位、分数、在线状态"),
                    ("🔎 /apex查询 /视奸", "中文别名，支持 uid: 和查询别名"),
                    ("🔖 /apex绑定 玩家或 uid:...", "绑定默认查询；list 查看，/apex解绑取消"),
                ],
            ),
            (
                (54, 520, 1068, 900),
                "监控",
                [
                    ("➕ /apexrankwatch 玩家 [平台]", "持续视奸：记录并主动通报分数变化"),
                    ("📝 /apexrankrecord 玩家 [平台]", "持续记录：只记录不通报"),
                    ("📋 /apexranklist /持续视奸列表", "查看通报和记录统计列表"),
                    ("📈 /分数变化 [玩家] [平台] [场次]", "生成最近变化高清长图，最多 50 次"),
                    ("➖ /apexrankremove 玩家 [平台]", "移除指定玩家监控或记录"),
                ],
            ),
            (
                (54, 920, 1068, 1370),
                "信息",
                [
                    ("🗺️ /map /地图 /排位地图", "排位地图轮换，默认输出图片"),
                    ("🕘 /全天地图", "API 学习确认排位未来 24 小时地图"),
                    ("🌍 /匹配地图", "三人赛地图轮换"),
                    ("🏆 /apexpredator [平台] /apex猎杀 /猎杀", "大师数量与猎杀底分"),
                    ("🗓️ /apexseason /新赛季", "当前排位分段结束时间"),
                    ("🔁 赛季关键词", "当前 UMO 会话消息包含赛季时自动回复"),
                ],
            ),
            (
                (54, 1390, 1068, 1680),
                "管理",
                [
                    ("🚫 /apexblacklist add 玩家ID", "加入动态黑名单"),
                    ("🚫 /apexblacklist list", "查看配置与动态黑名单"),
                    ("🔖 /apexalias add/list/remove", "管理全局查询别名"),
                    ("🔁 /赛季关闭 /赛季开启", "管理当前 UMO 会话赛季关键词回复"),
                    ("🧩 /apex_download", "检测或下载中文字体缓存"),
                ],
            ),
            (
                (54, 1700, 1068, 2296),
                "参数",
                self._help_parameter_rows(),
            ),
        ]

    def _help_parameter_rows(self) -> list[tuple[str, str]]:
        try:
            config_blacklist = self._get_config_blacklist()
        except Exception:
            config_blacklist = set()
        runtime_blacklist = getattr(self, "_runtime_blacklist", set())
        query_blocklist = getattr(self._config, "query_blocklist", "")
        rows = [
            ("平台", "PC / PS4 / X1 / SWITCH；PC 无数据会自动尝试其他平台"),
            ("UID", "使用 uid: 或 uuid: 前缀，例如 /apexrank uid:000000"),
            ("查询别名", "全局别名和个人绑定分开管理；绑定可用 /apex查询"),
            ("分数变化", "默认 20 次，可填写 1-50；持续记录只记录不通报"),
            ("监控间隔", f"{getattr(self._config, 'check_interval', 2)} 分钟"),
            ("最小有效分", f"{getattr(self._config, 'min_valid_score', 1)} 分"),
            ("异常分数", "仅当高分掉到接近 0 分时判定为异常"),
            ("权限", "支持群白名单、QQ 黑名单、主人 QQ、私聊开关"),
            ("字体", "缺少中文字体时会提示，可用 /apex_download 手动下载"),
        ]
        if config_blacklist or runtime_blacklist:
            rows.append(("黑名单", f"配置 {len(config_blacklist)} 个，动态 {len(runtime_blacklist)} 个"))
        if query_blocklist:
            rows.append(("禁止查询", f"已设置 {len(_split_csv(query_blocklist))} 个玩家ID"))
        return rows

    def _render_alias_list_image(self, alias_entries: list[tuple[str, str, str]]) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成查询别名列表图片")

        output_dir = self._generated_card_output_dir("alias_list_cards")
        output_dir.mkdir(parents=True, exist_ok=True)
        image = self._build_alias_list_card(alias_entries)
        output_path = output_dir / f"alias_list_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_generated_images(output_dir, "alias_list_*.png", keep=10)
        return output_path

    def _render_binding_list_image(self, binding_entries: list[tuple[str, str, str]]) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成查询绑定列表图片")

        output_dir = self._generated_card_output_dir("binding_list_cards")
        output_dir.mkdir(parents=True, exist_ok=True)
        image = self._build_binding_list_card(binding_entries)
        output_path = output_dir / f"binding_list_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_generated_images(output_dir, "binding_list_*.png", keep=10)
        return output_path

    def _build_alias_list_card(self, alias_entries: list[tuple[str, str, str]]):
        width = self._MONITOR_LIST_CARD_WIDTH
        shown_entries = alias_entries[: self._MONITOR_LIST_ROW_LIMIT]
        row_height = 104
        row_gap = 14
        footer_height = 92
        list_top = 366
        height = max(
            760,
            list_top + max(1, len(shown_entries)) * (row_height + row_gap) + footer_height,
        )
        canvas = Image.new("RGBA", (width, height), (7, 8, 11, 255))
        draw = ImageDraw.Draw(canvas)
        self._draw_rank_change_background(canvas)
        self._draw_rank_change_outer_frame(draw, width, height)
        self._draw_alias_list_header(canvas, draw, len(alias_entries))
        self._draw_alias_list_summary(draw, alias_entries)

        y = list_top
        if shown_entries:
            for index, entry in enumerate(shown_entries, start=1):
                self._draw_alias_list_row(draw, (54, y, width - 54, y + row_height), index, entry)
                y += row_height + row_gap
        else:
            self._draw_alias_empty_row(draw, (54, y, width - 54, y + row_height))

        footer_text = "动态别名优先覆盖面板配置中的同名别名"
        if len(alias_entries) > len(shown_entries):
            footer_text = f"已展示前 {len(shown_entries)} 个，还有 {len(alias_entries) - len(shown_entries)} 个别名未展示"
        footer_font = self._fit_font(draw, footer_text, 28, 20, width - 150, bold=True)
        self._draw_centered_stroked_text(
            draw,
            footer_text,
            footer_font,
            54,
            width - 54,
            height - 84,
            height - 40,
            (202, 210, 220, 255),
        )
        return canvas.convert("RGB")

    def _build_binding_list_card(self, binding_entries: list[tuple[str, str, str]]):
        width = self._MONITOR_LIST_CARD_WIDTH
        shown_entries = binding_entries[: self._MONITOR_LIST_ROW_LIMIT]
        row_height = 104
        row_gap = 14
        footer_height = 92
        list_top = 366
        height = max(
            760,
            list_top + max(1, len(shown_entries)) * (row_height + row_gap) + footer_height,
        )
        canvas = Image.new("RGBA", (width, height), (7, 8, 11, 255))
        draw = ImageDraw.Draw(canvas)
        self._draw_rank_change_background(canvas)
        self._draw_rank_change_outer_frame(draw, width, height)
        self._draw_binding_list_header(canvas, draw, len(binding_entries))
        self._draw_binding_list_summary(draw, binding_entries)

        y = list_top
        if shown_entries:
            for index, entry in enumerate(shown_entries, start=1):
                self._draw_alias_list_row(draw, (54, y, width - 54, y + row_height), index, entry)
                y += row_height + row_gap
        else:
            self._draw_binding_empty_row(draw, (54, y, width - 54, y + row_height))

        footer_text = "绑定只影响对应 QQ 用户直接发送 /apex查询 的默认目标"
        if len(binding_entries) > len(shown_entries):
            footer_text = f"已展示前 {len(shown_entries)} 个，还有 {len(binding_entries) - len(shown_entries)} 个绑定未展示"
        footer_font = self._fit_font(draw, footer_text, 28, 20, width - 150, bold=True)
        self._draw_centered_stroked_text(
            draw,
            footer_text,
            footer_font,
            54,
            width - 54,
            height - 84,
            height - 40,
            (202, 210, 220, 255),
        )
        return canvas.convert("RGB")

    def _draw_alias_list_header(self, canvas, draw, total_aliases: int) -> None:
        box = (54, 54, 1068, 224)
        self._draw_rank_panel_base(draw, box, fill=(12, 14, 19, 238), outline_alpha=150)
        badge = self._apex_logo_badge(118)
        canvas.alpha_composite(badge, (92, 80))

        title = "Apex 查询别名"
        subtitle = f"{total_aliases} 个别名 · /apexalias list"
        title_font = self._fit_font(draw, title, 66, 46, 690, bold=True)
        subtitle_font = self._fit_font(draw, subtitle, 32, 23, 690, bold=True)
        self._draw_text_stroked(
            draw,
            (254, self._centered_text_y(draw, title, title_font, 78, 146)),
            title,
            title_font,
            fill=(250, 250, 246, 255),
        )
        self._draw_text_stroked(
            draw,
            (254, self._centered_text_y(draw, subtitle, subtitle_font, 150, 202)),
            subtitle,
            subtitle_font,
            fill=(205, 212, 222, 255),
        )
        draw.rectangle((760, 214, 930, 220), fill=(236, 48, 52, 220))

    def _draw_binding_list_header(self, canvas, draw, total_bindings: int) -> None:
        box = (54, 54, 1068, 224)
        self._draw_rank_panel_base(draw, box, fill=(12, 14, 19, 238), outline_alpha=150)
        badge = self._apex_logo_badge(118)
        canvas.alpha_composite(badge, (92, 80))

        title = "Apex 查询绑定"
        subtitle = f"{total_bindings} 个绑定 · /apex绑定 list"
        title_font = self._fit_font(draw, title, 66, 46, 690, bold=True)
        subtitle_font = self._fit_font(draw, subtitle, 32, 23, 690, bold=True)
        self._draw_text_stroked(
            draw,
            (254, self._centered_text_y(draw, title, title_font, 78, 146)),
            title,
            title_font,
            fill=(250, 250, 246, 255),
        )
        self._draw_text_stroked(
            draw,
            (254, self._centered_text_y(draw, subtitle, subtitle_font, 150, 202)),
            subtitle,
            subtitle_font,
            fill=(205, 212, 222, 255),
        )
        draw.rectangle((760, 214, 930, 220), fill=(236, 48, 52, 220))

    def _draw_alias_list_summary(self, draw, alias_entries: list[tuple[str, str, str]]) -> None:
        config_count = sum(1 for _, _, source in alias_entries if str(source).startswith("配置"))
        runtime_count = sum(1 for _, _, source in alias_entries if str(source).startswith("动态"))
        panels = [
            ((54, 244, 370, 338), "配置别名", f"{config_count} 个"),
            ((388, 244, 734, 338), "动态别名", f"{runtime_count} 个"),
            ((752, 244, 1068, 338), "列表总数", f"{len(alias_entries)} 个"),
        ]
        for box, label, value in panels:
            self._draw_mini_info_pill(draw, box, label, value)

    def _draw_binding_list_summary(self, draw, binding_entries: list[tuple[str, str, str]]) -> None:
        panels = [
            ((54, 244, 370, 338), "个人绑定", f"{len(binding_entries)} 个"),
            ((388, 244, 734, 338), "查询命令", "/apex查询"),
            ((752, 244, 1068, 338), "解除绑定", "/apex解绑"),
        ]
        for box, label, value in panels:
            self._draw_mini_info_pill(draw, box, label, value)

    def _draw_alias_empty_row(self, draw, box: tuple[int, int, int, int]) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=92)
        text = "暂无查询别名"
        font = self._fit_font(draw, text, 34, 24, box[2] - box[0] - 80, bold=True)
        self._draw_centered_stroked_text(
            draw,
            text,
            font,
            box[0] + 30,
            box[2] - 30,
            box[1] + 18,
            box[3] - 18,
            (205, 212, 222, 255),
        )

    def _draw_binding_empty_row(self, draw, box: tuple[int, int, int, int]) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=92)
        text = "暂无查询绑定"
        font = self._fit_font(draw, text, 34, 24, box[2] - box[0] - 80, bold=True)
        self._draw_centered_stroked_text(
            draw,
            text,
            font,
            box[0] + 30,
            box[2] - 30,
            box[1] + 18,
            box[3] - 18,
            (205, 212, 222, 255),
        )

    def _draw_alias_list_row(
        self,
        draw,
        box: tuple[int, int, int, int],
        index: int,
        entry: tuple[str, str, str],
    ) -> None:
        alias, target, source = entry
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=92)

        index_box = (box[0] + 22, box[1] + 26, box[0] + 74, box[1] + 78)
        draw.rounded_rectangle(index_box, radius=10, fill=(236, 48, 52, 225))
        index_font = self._font(25, bold=True)
        self._draw_centered_stroked_text(
            draw,
            str(index),
            index_font,
            index_box[0],
            index_box[2],
            index_box[1],
            index_box[3],
            (255, 255, 255, 255),
            stroke_width=1,
        )

        alias_text = str(alias or "未知别名")
        target_text = str(target or "未知目标")
        source_text = str(source or "未知")
        alias_font = self._fit_font(draw, alias_text, 34, 24, 320, bold=True)
        target_font = self._fit_font(draw, target_text, 28, 19, 500)
        source_font = self._fit_font(draw, source_text, 26, 18, 110, bold=True)

        text_left = box[0] + 100
        self._draw_text_stroked(
            draw,
            (text_left, box[1] + 18),
            alias_text,
            alias_font,
            fill=(250, 250, 246, 255),
        )
        self._draw_text_stroked(
            draw,
            (text_left, box[1] + 60),
            f"目标：{target_text}",
            target_font,
            fill=(190, 198, 210, 255),
        )

        badge_box = (box[2] - 160, box[1] + 30, box[2] - 42, box[1] + 74)
        if source_text.startswith("动态"):
            badge_fill = (236, 48, 52, 225)
        elif source_text.startswith("个人"):
            badge_fill = (58, 135, 92, 225)
        else:
            badge_fill = (60, 92, 130, 225)
        draw.rounded_rectangle(badge_box, radius=12, fill=badge_fill)
        self._draw_centered_stroked_text(
            draw,
            source_text,
            source_font,
            badge_box[0],
            badge_box[2],
            badge_box[1],
            badge_box[3],
            (255, 255, 255, 255),
            stroke_width=1,
        )

    def _draw_help_card_header(self, canvas, draw) -> None:
        box = (54, 54, 1068, 224)
        self._draw_rank_panel_base(draw, box, fill=(12, 14, 19, 238), outline_alpha=150)
        badge = self._apex_logo_badge(118)
        canvas.alpha_composite(badge, (92, 80))

        title = "Apex Rank Watch"
        subtitle = "QQ 命令帮助卡 · 图片化速查"
        title_font = self._fit_font(draw, title, 68, 48, 690, bold=True)
        subtitle_font = self._fit_font(draw, subtitle, 32, 24, 690, bold=True)
        self._draw_text_stroked(
            draw,
            (250, self._centered_text_y(draw, title, title_font, 70, 150)),
            title,
            title_font,
            fill=(248, 248, 244, 255),
            stroke_width=2,
        )
        self._draw_text_stroked(
            draw,
            (254, self._centered_text_y(draw, subtitle, subtitle_font, 150, 202)),
            subtitle,
            subtitle_font,
            fill=(205, 212, 222, 255),
        )
        draw.rectangle((780, 214, 928, 220), fill=(236, 48, 52, 220))

    def _draw_help_section(
        self,
        draw,
        box: tuple[int, int, int, int],
        title: str,
        rows: list[tuple[str, str]],
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 238), outline_alpha=110)
        title_font = self._font(38, bold=True)
        self._draw_text_stroked(
            draw,
            (box[0] + 34, box[1] + 24),
            title,
            title_font,
            fill=(246, 246, 241, 255),
            stroke_width=2,
        )
        draw.rectangle((box[0] + 34, box[1] + 74, box[0] + 142, box[1] + 80), fill=(232, 48, 52, 230))

        row_top = box[1] + 92
        available = max(1, box[3] - row_top - 22)
        row_height = max(54, available // max(1, len(rows)))
        for index, (command, desc) in enumerate(rows):
            top = row_top + index * row_height
            if top + 56 > box[3] - 16:
                break
            dot_y = top + 18
            draw.rounded_rectangle((box[0] + 34, dot_y - 6, box[0] + 46, dot_y + 6), radius=3, fill=(232, 48, 52, 235))
            command_font = self._fit_font(draw, command, 29, 21, box[2] - box[0] - 102, bold=True)
            desc_font = self._fit_font(draw, desc, 23, 17, box[2] - box[0] - 102)
            self._draw_text_stroked(
                draw,
                (box[0] + 60, top),
                command,
                command_font,
                fill=(250, 250, 246, 255),
            )
            self._draw_text_stroked(
                draw,
                (box[0] + 60, top + 34),
                desc,
                desc_font,
                fill=(172, 181, 194, 255),
            )

    def _build_rank_watch_list_lines(self, players_iter) -> list[str]:
        players = list(players_iter)
        response_lines = [self._time_line(), "📋 本群 Apex 排名监控列表"]
        for index, player in enumerate(players, start=1):
            rank_display = self._record_rank_display(player)
            platform = getattr(player, "platform", "PC") or "PC"
            response_lines.append(f"👤 玩家 {index}: {self._record_display_name(player)}")
            response_lines.append(f"🖥️ 平台: {self._format_platform(platform)}")
            response_lines.append(f"🧾 模式: {self._watch_mode_label(player)}")
            response_lines.append(f"🏆 段位: {rank_display}")
            response_lines.append(f"🔢 分数: {player.rank_score}")
            if player.global_rank_percent and player.global_rank_percent != "未知":
                response_lines.append(f"🌎 全球排名: {self._format_rank_percent(player.global_rank_percent)}")
            if player.selected_legend:
                response_lines.append(f"🎮 当前英雄: {player.selected_legend}")
                if player.legend_kills_percent:
                    response_lines.append(
                        f"📊 击杀排名: 全球 {self._format_rank_percent(player.legend_kills_percent)}"
                    )
            response_lines.append("➖ —")

        response_lines.append(f"🔢 总计: {len(players)} 个玩家")
        response_lines.append(f"⏱️ 检测间隔: {getattr(self._config, 'check_interval', 2)} 分钟")
        response_lines.append(
            "⚠️ 分数异常判断: 仅当高分(>1000)掉到接近0分(<10)时判定为异常"
        )
        response_lines.append(f"🔻 最小有效分数: {getattr(self._config, 'min_valid_score', 1)} 分")
        return response_lines

    def _render_rank_watch_list_image(self, players_iter) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成群监控列表图片")

        players = list(players_iter)
        output_dir = self._generated_card_output_dir("rank_watch_list_cards")
        output_dir.mkdir(parents=True, exist_ok=True)
        image = self._build_rank_watch_list_card(players)
        output_path = output_dir / f"rank_watch_list_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_generated_images(output_dir, "rank_watch_list_*.png", keep=10)
        return output_path

    def _build_rank_watch_list_card(self, players: list[PlayerRecord]):
        width = self._MONITOR_LIST_CARD_WIDTH
        shown_players = players[: self._MONITOR_LIST_ROW_LIMIT]
        row_height = 118
        row_gap = 14
        footer_height = 92
        list_top = 366
        height = max(
            760,
            list_top + len(shown_players) * (row_height + row_gap) + footer_height,
        )
        canvas = Image.new("RGBA", (width, height), (7, 8, 11, 255))
        draw = ImageDraw.Draw(canvas)
        self._draw_rank_change_background(canvas)
        self._draw_rank_change_outer_frame(draw, width, height)
        self._draw_monitor_list_header(canvas, draw, len(players))
        self._draw_monitor_list_summary(draw, len(players))

        y = list_top
        for index, player in enumerate(shown_players, start=1):
            self._draw_monitor_list_row(draw, (54, y, width - 54, y + row_height), index, player)
            y += row_height + row_gap

        footer_text = "时间均为北京时间"
        if len(players) > len(shown_players):
            footer_text = f"已展示前 {len(shown_players)} 位，还有 {len(players) - len(shown_players)} 位玩家未展示"
        footer_font = self._fit_font(draw, footer_text, 28, 20, width - 150, bold=True)
        self._draw_centered_stroked_text(
            draw,
            footer_text,
            footer_font,
            54,
            width - 54,
            height - 84,
            height - 40,
            (202, 210, 220, 255),
        )
        return canvas.convert("RGB")

    def _draw_monitor_list_header(self, canvas, draw, total_players: int) -> None:
        box = (54, 54, 1068, 224)
        self._draw_rank_panel_base(draw, box, fill=(12, 14, 19, 238), outline_alpha=150)
        badge = self._apex_logo_badge(118)
        canvas.alpha_composite(badge, (92, 80))

        title = "Apex 群监控列表"
        subtitle = f"{total_players} 位玩家 · 每 {getattr(self._config, 'check_interval', 2)} 分钟检测一次"
        title_font = self._fit_font(draw, title, 66, 46, 690, bold=True)
        subtitle_font = self._fit_font(draw, subtitle, 32, 23, 690, bold=True)
        self._draw_text_stroked(
            draw,
            (250, self._centered_text_y(draw, title, title_font, 70, 150)),
            title,
            title_font,
            fill=(248, 248, 244, 255),
            stroke_width=2,
        )
        self._draw_text_stroked(
            draw,
            (254, self._centered_text_y(draw, subtitle, subtitle_font, 150, 202)),
            subtitle,
            subtitle_font,
            fill=(205, 212, 222, 255),
        )
        draw.rectangle((760, 214, 930, 220), fill=(236, 48, 52, 220))

    def _draw_monitor_list_summary(self, draw, total_players: int) -> None:
        panels = [
            ((54, 244, 370, 338), "监控玩家", f"{total_players} 位"),
            ((388, 244, 734, 338), "检测间隔", f"{getattr(self._config, 'check_interval', 2)} 分钟"),
            ((752, 244, 1068, 338), "最小有效分", f"{getattr(self._config, 'min_valid_score', 1)} 分"),
        ]
        for box, label, value in panels:
            self._draw_mini_info_pill(draw, box, label, value)

    def _draw_monitor_list_row(
        self,
        draw,
        box: tuple[int, int, int, int],
        index: int,
        player: PlayerRecord,
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=92)
        rank_name = getattr(player, "rank_name", "") or "未知"
        rank_div = getattr(player, "rank_div", 0) or 0
        rank_icon = self._resolve_rank_icon_path(rank_name, rank_div)
        icon_box = (box[0] + 24, box[1] + 16, box[0] + 112, box[1] + 104)
        if rank_icon.exists():
            self._draw_image_asset(draw, rank_icon, icon_box)
            if rank_div and "_" not in rank_icon.stem and rank_icon.stem not in {"master", "predator"}:
                div_font = self._font(28, bold=True)
                self._draw_centered_stroked_text(
                    draw,
                    str(rank_div),
                    div_font,
                    icon_box[0] + 26,
                    icon_box[2] - 26,
                    icon_box[1] + 40,
                    icon_box[3] - 18,
                    (255, 255, 255, 255),
                    stroke_width=2,
                )
        else:
            self._draw_icon_octagon(draw, icon_box)
            self._draw_rank_icon(draw, "target", icon_box)

        name = self._record_display_name(player)
        platform = self._format_platform(getattr(player, "platform", "PC") or "PC")
        checked_at = self._format_record_checked_at(getattr(player, "last_checked", 0))
        rank_text = self._record_rank_display(player)
        name_text = f"{index}. {name}"
        mode_label = self._watch_mode_label(player)
        meta_text = f"{platform} · {mode_label} · {checked_at} · {rank_text}"
        name_font = self._fit_font(draw, name_text, 34, 24, 390, bold=True)
        meta_font = self._fit_font(draw, meta_text, 25, 18, 430)
        text_left = box[0] + 132
        self._draw_text_stroked(draw, (text_left, box[1] + 18), name_text, name_font, fill=(250, 250, 246, 255))
        self._draw_text_stroked(draw, (text_left, box[1] + 64), meta_text, meta_font, fill=(190, 198, 210, 255))

        legend_name = str(getattr(player, "selected_legend", "") or "未知")
        legend_icon = self._resolve_legend_icon_path(legend_name)
        legend_box = (box[0] + 600, box[1] + 22, box[0] + 674, box[1] + 96)
        self._draw_icon_octagon(draw, legend_box)
        if legend_icon.exists():
            self._draw_image_asset(draw, legend_icon, legend_box, clip_octagon=True)
        else:
            self._draw_rank_icon(draw, "legend", legend_box)
        legend_font = self._fit_font(draw, legend_name, 26, 18, 140, bold=True)
        self._draw_centered_stroked_text(
            draw,
            legend_name,
            legend_font,
            box[0] + 682,
            box[0] + 830,
            box[1] + 34,
            box[1] + 86,
            (236, 240, 246, 255),
        )

        score_text = self._format_rank_score_number(getattr(player, "rank_score", 0))
        score_font = self._fit_font(draw, score_text, 46, 30, 190, bold=True)
        label_font = self._font(22, bold=True)
        score_left = box[2] - 222
        self._draw_centered_stroked_text(
            draw,
            "当前分数",
            label_font,
            score_left,
            box[2] - 28,
            box[1] + 18,
            box[1] + 48,
            (176, 184, 196, 255),
        )
        self._draw_centered_stroked_text(
            draw,
            score_text,
            score_font,
            score_left,
            box[2] - 28,
            box[1] + 48,
            box[1] + 104,
            (250, 250, 246, 255),
            stroke_width=2,
        )
        draw.rectangle((score_left + 28, box[3] - 18, box[2] - 58, box[3] - 12), fill=(221, 48, 52, 185))

    def _render_score_change_chart(self, players: list[PlayerRecord], limit: int) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成分数变化长图")

        output_dir = self._generated_card_output_dir("score_change_charts")
        output_dir.mkdir(parents=True, exist_ok=True)
        image = self._build_score_change_chart(players, limit)
        output_path = output_dir / f"score_change_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_generated_images(output_dir, "score_change_*.png", keep=12)
        return output_path

    @staticmethod
    def _score_change_iso_time(captured_at: int) -> str:
        try:
            value = datetime.fromtimestamp(
                int(captured_at) / 1000,
                timezone.utc,
            )
        except (TypeError, ValueError, OSError):
            value = datetime.now(timezone.utc)
        return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _score_change_rank_asset_file(
        self,
        rank_name: str,
        rank_div: int = 0,
    ) -> str:
        return rank_asset_file_for_label(
            self._rank_label_from_parts(rank_name, rank_div)
        )

    def _score_change_report_payload(
        self,
        players: list[PlayerRecord],
        limit: int,
    ) -> dict:
        limit = self._clamp_score_change_limit(limit)
        events_desc = self._recent_score_change_events(players, limit)
        event_ids = {
            (id(player), id(item)): index
            for index, (player, item) in enumerate(
                reversed(events_desc),
                start=1,
            )
        }
        player_ids = {id(player): index for index, player in enumerate(players, start=1)}
        series: list[dict] = []
        summaries: list[dict] = []

        for player in players:
            player_id = player_ids[id(player)]
            visible_events = [
                item for owner, item in reversed(events_desc) if owner is player
            ]
            all_events = sorted(
                self._score_history_items(player),
                key=lambda item: item.captured_at,
            )
            line_points: list[dict] = []
            change_points: list[dict] = []

            for event_index, item in enumerate(visible_events):
                event_id = event_ids[(id(player), id(item))]
                captured_at = self._score_change_iso_time(item.captured_at)
                rank_label = self._history_to_rank_label(item)
                if event_index == 0:
                    baseline_at = datetime.fromtimestamp(
                        int(item.captured_at) / 1000,
                        timezone.utc,
                    ) - timedelta(seconds=1)
                    line_points.append(
                        {
                            "snapshot_id": -event_id,
                            "captured_at": baseline_at.replace(
                                microsecond=0
                            ).isoformat().replace("+00:00", "Z"),
                            "captured_at_local": self._format_score_history_time(
                                item.captured_at
                            ),
                            "rank_score": int(item.from_score),
                            "rank_label": self._history_from_rank_label(item),
                            "rank_icon_file": self._score_change_rank_asset_file(
                                item.from_rank_name,
                                item.from_rank_div,
                            ),
                            "event_ids": [event_id],
                        }
                    )
                line_points.append(
                    {
                        "snapshot_id": event_id,
                        "captured_at": captured_at,
                        "captured_at_local": self._format_score_history_time(
                            item.captured_at
                        ),
                        "rank_score": int(item.to_score),
                        "rank_label": rank_label,
                        "rank_icon_file": self._score_change_rank_asset_file(
                            item.to_rank_name,
                            item.to_rank_div,
                        ),
                        "event_ids": [event_id],
                    }
                )
                rank_changed = self._score_history_rank_changed(item)
                change_points.append(
                    {
                        "event_id": event_id,
                        "captured_at": captured_at,
                        "captured_at_local": self._format_score_history_time(
                            item.captured_at
                        ),
                        "rank_score": int(item.to_score),
                        "rank_label": rank_label,
                        "score_delta": int(item.score_delta),
                        "rank_changed": rank_changed,
                        "rank_icon_file": self._score_change_rank_asset_file(
                            item.to_rank_name,
                            item.to_rank_div,
                        ),
                        "rank_asset_file": self._score_change_rank_asset_file(
                            item.to_rank_name,
                            item.to_rank_div,
                        ),
                        "change_summary": "",
                        "season_reset": bool(item.is_season_reset),
                        "show_rank_icon": rank_changed
                        or bool(item.is_season_reset),
                    }
                )

            display_name = self._record_display_name(player)
            series.append(
                {
                    "player_id": player_id,
                    "display_name": display_name,
                    "points": line_points,
                    "line_points": line_points,
                    "effective_bucket": "raw",
                    "auto_expanded_timeline": False,
                    "change_points": change_points,
                }
            )

            visible_scores = [int(getattr(player, "rank_score", 0))]
            for item in visible_events:
                visible_scores.extend((int(item.from_score), int(item.to_score)))
            recent_delta = self._score_change_net_delta(visible_events)
            latest_history = all_events[-1] if all_events else None
            latest_at = (
                int(latest_history.captured_at)
                if latest_history is not None
                else int(getattr(player, "last_checked", 0) or 0)
            )
            if latest_at and latest_at < 10_000_000_000:
                latest_at *= 1000
            active_events: list[ScoreChangeRecord] = []
            if all_events:
                active_since = all_events[-1].captured_at - int(
                    timedelta(hours=14).total_seconds() * 1000
                )
                active_events = [
                    item for item in all_events if item.captured_at >= active_since
                ]
            first_visible = visible_events[0] if visible_events else None
            last_visible = visible_events[-1] if visible_events else None
            latest_rank_label = self._record_rank_display(player)
            summaries.append(
                {
                    "player_id": player_id,
                    "display_name": display_name,
                    "latest_rank_label": latest_rank_label,
                    "latest_rank_score": int(getattr(player, "rank_score", 0)),
                    "latest_rank_icon_file": self._score_change_rank_asset_file(
                        getattr(player, "rank_name", ""),
                        getattr(player, "rank_div", 0),
                    ),
                    "rank_asset_file": self._score_change_rank_asset_file(
                        getattr(player, "rank_name", ""),
                        getattr(player, "rank_div", 0),
                    ),
                    "delta_score": recent_delta,
                    "samples": len(line_points),
                    "change_events": len(visible_events),
                    "max_score": max(visible_scores),
                    "min_score": min(visible_scores),
                    "avg_score": round(
                        sum(visible_scores) / len(visible_scores),
                        2,
                    ),
                    "first_seen_at": (
                        self._format_score_history_time(first_visible.captured_at)
                        if first_visible is not None
                        else ""
                    ),
                    "last_seen_at": (
                        self._format_score_history_time(last_visible.captured_at)
                        if last_visible is not None
                        else ""
                    ),
                    "latest_captured_at": (
                        self._score_change_iso_time(latest_at) if latest_at else ""
                    ),
                    "active_delta_score": self._score_change_net_delta(
                        active_events
                    ),
                    "active_change_events": len(active_events),
                    "active_duration_hours": 14 if active_events else 0,
                    "recent_event_delta_score": recent_delta,
                    "recent_event_count": len(visible_events),
                    "recent_up_count": sum(
                        1 for item in visible_events if item.score_delta > 0
                    ),
                    "recent_down_count": sum(
                        1 for item in visible_events if item.score_delta < 0
                    ),
                    "recent_up_score": sum(
                        int(item.score_delta)
                        for item in visible_events
                        if item.score_delta > 0
                    ),
                    "recent_down_score": sum(
                        int(item.score_delta)
                        for item in visible_events
                        if item.score_delta < 0
                    ),
                }
            )

        change_events: list[dict] = []
        for player, item in events_desc:
            player_id = player_ids[id(player)]
            rank_changed = self._score_history_rank_changed(item)
            to_asset = self._score_change_rank_asset_file(
                item.to_rank_name,
                item.to_rank_div,
            )
            change_events.append(
                {
                    "id": event_ids[(id(player), id(item))],
                    "player_id": player_id,
                    "display_name": self._record_display_name(player),
                    "captured_at": self._score_change_iso_time(item.captured_at),
                    "captured_at_local": self._format_score_history_time(
                        item.captured_at
                    ),
                    "score_delta": int(item.score_delta),
                    "from_rank_score": int(item.from_score),
                    "to_rank_score": int(item.to_score),
                    "from_rank_label": self._history_from_rank_label(item),
                    "to_rank_label": self._history_to_rank_label(item),
                    "rank_changed": rank_changed,
                    "rank_icon_file": to_asset,
                    "rank_asset_file": to_asset,
                    "rank_text": "",
                    "change_summary": "",
                    "season_reset": bool(item.is_season_reset),
                }
            )

        return {
            "range_label": f"最近 {limit} 次分数变化",
            "bucket_label": "原始采样",
            "bucket_auto_expanded": False,
            "bucket_auto_expanded_players": 0,
            "selected_season_reset_at": "",
            "selected_season_reset_local": "",
            "season_resets": [],
            "series": series,
            "summaries": summaries,
            "players": [
                {
                    "id": player_ids[id(player)],
                    "display_name": self._record_display_name(player),
                    "latest_rank_score": int(getattr(player, "rank_score", 0)),
                    "latest_rank_label": self._record_rank_display(player),
                }
                for player in players
            ],
            "change_events": change_events,
            "event_limit": limit,
            "recent_events": True,
            "chart_x_axis": "count",
        }

    def _build_score_change_chart(self, players: list[PlayerRecord], limit: int):
        limit = self._clamp_score_change_limit(limit)
        font_status = self._get_cjk_font_status()
        if not font_status.available:
            downloaded_font = self._download_cjk_font_if_needed()
            if downloaded_font is not None:
                font_status = FontStatus(True, "cache", downloaded_font)
        font_path = font_status.path if font_status.available else None
        configure_score_report_font_paths(font_path, font_path)
        png_bytes = render_dashboard_image_png(
            payload=self._score_change_report_payload(players, limit),
            icon_dir=self._RANK_ICON_DIR,
            timezone_name="Asia/Shanghai",
            range_key="all",
            bucket="raw",
            width=self._SCORE_CHANGE_CHART_WIDTH,
            height=1080,
            layout="full",
            event_limit=limit,
            x_axis="count",
        )
        with Image.open(BytesIO(png_bytes)) as image:
            return image.convert("RGB")

    def _draw_score_change_background(self, canvas) -> None:
        width, height = canvas.size
        draw = ImageDraw.Draw(canvas)
        for row in range(height):
            ratio = row / max(1, height - 1)
            shade = int(8 + 10 * ratio)
            draw.line((0, row, width, row), fill=(shade, shade + 8, shade + 18, 255))
        for y in range(96, height, 220):
            draw.line((0, y, width, y), fill=(23, 48, 72, 90), width=1)
        draw.rectangle((0, 0, width, 12), fill=(19, 50, 74, 255))

    def _draw_score_panel(self, draw, box: tuple[int, int, int, int]) -> None:
        draw.rounded_rectangle(
            box,
            radius=14,
            fill=(13, 26, 42, 248),
            outline=(36, 67, 94, 255),
            width=2,
        )

    def _draw_score_change_header(
        self,
        draw,
        box: tuple[int, int, int, int],
        players: list[PlayerRecord],
        event_count: int,
        limit: int,
    ) -> None:
        self._draw_score_panel(draw, box)
        x1, y1, x2, y2 = box
        title = "APEX分数/段位变化图"
        title_font = self._font(54, bold=True)
        meta_font = self._font(24)
        draw.text((x1 + 28, y1 + 26), title, font=title_font, fill=(237, 245, 255, 255))
        meta = (
            f"最近 {event_count} / {limit} 次分数变化   "
            f"统计玩家 {len(players)} 位   生成时间 {self._now_str()}"
        )
        draw.text((x1 + 30, y1 + 92), meta, font=meta_font, fill=(142, 168, 194, 255))

        chip_x = x1 + 30
        chip_y = y1 + 142
        chip_font = self._font(23, bold=True)
        for player in players[:8]:
            chip_text = f"{self._record_display_name(player)} · {player.rank_score} RP · {self._watch_mode_label(player)}"
            chip_text = self._fit_text_to_width(draw, chip_text, chip_font, 380)
            text_box = draw.textbbox((0, 0), chip_text, font=chip_font)
            chip_w = min(430, text_box[2] - text_box[0] + 34)
            if chip_x + chip_w > x2 - 30:
                chip_x = x1 + 30
                chip_y += 42
            draw.rounded_rectangle(
                (chip_x, chip_y, chip_x + chip_w, chip_y + 34),
                radius=17,
                fill=(17, 38, 58, 255),
                outline=(37, 75, 105, 255),
            )
            draw.text((chip_x + 17, chip_y + 4), chip_text, font=chip_font, fill=(185, 216, 238, 255))
            chip_x += chip_w + 12

    def _draw_score_change_chart_panel(
        self,
        canvas,
        draw,
        box: tuple[int, int, int, int],
        players: list[PlayerRecord],
        events: list[tuple[PlayerRecord, ScoreChangeRecord]],
    ) -> None:
        self._draw_score_panel(draw, box)
        x1, y1, x2, y2 = box
        heading_font = self._font(34, bold=True)
        small_font = self._font(20)
        draw.text((x1 + 26, y1 + 22), "变化趋势", font=heading_font, fill=(237, 245, 255, 255))
        draw.text((x1 + 28, y1 + 66), "横轴按有效分数变化次数排列，绿色为上分，粉色为掉分。", font=small_font, fill=(142, 168, 194, 255))

        plot_x = x1 + 92
        plot_y = y1 + 126
        plot_w = x2 - x1 - 132
        plot_h = y2 - plot_y - 68
        draw.rounded_rectangle(
            (plot_x, plot_y, plot_x + plot_w, plot_y + plot_h),
            radius=12,
            fill=(16, 33, 53, 255),
            outline=(28, 49, 71, 255),
        )

        if not events:
            empty = "暂无分数变化记录；持续视奸或持续记录产生变化后会显示折线。"
            empty_font = self._font(28, bold=True)
            self._draw_centered_stroked_text(
                draw,
                empty,
                empty_font,
                plot_x,
                plot_x + plot_w,
                plot_y,
                plot_y + plot_h,
                (185, 201, 220, 255),
            )
            return

        grouped: dict[int, list[ScoreChangeRecord]] = {}
        player_index: dict[int, int] = {}
        for player, item in events:
            key = id(player)
            grouped.setdefault(key, []).append(item)
            player_index.setdefault(key, players.index(player) if player in players else 0)

        scores: list[int] = []
        for items in grouped.values():
            if items:
                scores.append(items[0].from_score)
            scores.extend(item.to_score for item in items)
        y_min = min(scores)
        y_max = max(scores)
        if y_min == y_max:
            y_min = max(0, y_min - 100)
            y_max += 100
        else:
            pad = max(80, int((y_max - y_min) * 0.12))
            y_min = max(0, y_min - pad)
            y_max += pad
        y_span = max(1, y_max - y_min)
        max_event_count = max((len(items) for items in grouped.values()), default=1)

        def map_x(index: int) -> int:
            if max_event_count <= 0:
                return plot_x + plot_w // 2
            return int(plot_x + (index / max(1, max_event_count)) * plot_w)

        def map_y(score: int) -> int:
            return int(plot_y + plot_h - ((score - y_min) / y_span) * plot_h)

        grid_font = self._font(18)
        for tick in range(6):
            score = int(y_min + y_span * tick / 5)
            yy = map_y(score)
            draw.line((plot_x, yy, plot_x + plot_w, yy), fill=(33, 54, 74, 255), width=1)
            label = str(score)
            label_box = draw.textbbox((0, 0), label, font=grid_font)
            draw.text((plot_x - (label_box[2] - label_box[0]) - 14, yy - 10), label, font=grid_font, fill=(118, 144, 168, 255))

        for tick in range(min(6, max_event_count + 1)):
            index = round(tick * max_event_count / max(1, min(5, max_event_count)))
            xx = map_x(index)
            draw.line((xx, plot_y, xx, plot_y + plot_h), fill=(33, 54, 74, 255), width=1)
            label = "基线" if index == 0 else f"第{index}次"
            label_box = draw.textbbox((0, 0), label, font=grid_font)
            draw.text((xx - (label_box[2] - label_box[0]) // 2, plot_y + plot_h + 18), label, font=grid_font, fill=(118, 144, 168, 255))

        palette = [
            (125, 211, 252, 255),
            (251, 191, 36, 255),
            (52, 211, 153, 255),
            (244, 114, 182, 255),
            (192, 132, 252, 255),
            (45, 212, 191, 255),
        ]
        for player_key, items in grouped.items():
            color = palette[player_index.get(player_key, 0) % len(palette)]
            points: list[tuple[int, int]] = []
            if items:
                points.append((map_x(0), map_y(items[0].from_score)))
            for index, item in enumerate(items, start=1):
                points.append((map_x(index), map_y(item.to_score)))
            if len(points) >= 2:
                draw.line(points, fill=color, width=8, joint="curve")
                draw.line(points, fill=(145, 219, 255, 110), width=14)
                draw.line(points, fill=color, width=7, joint="curve")
            for index, item in enumerate(items, start=1):
                px, py = points[index]
                self._draw_score_change_marker(draw, px, py, item)

    def _score_history_rank_changed(self, item: ScoreChangeRecord) -> bool:
        return self._history_from_rank_label(item) != self._history_to_rank_label(item)

    def _draw_score_change_marker(self, draw, x: int, y: int, item: ScoreChangeRecord) -> None:
        if self._score_history_rank_changed(item):
            rank_icon = self._resolve_rank_icon_path(item.to_rank_name, item.to_rank_div)
            if rank_icon.exists():
                size = 62
                box = (x - size // 2, y - size // 2, x + size // 2, y + size // 2)
                glow_radius = 34
                draw.ellipse(
                    (x - glow_radius, y - glow_radius, x + glow_radius, y + glow_radius),
                    fill=(12, 24, 38, 235),
                    outline=(134, 239, 172, 210) if item.score_delta >= 0 else (253, 164, 175, 210),
                    width=4,
                )
                self._draw_image_asset(draw, rank_icon, box)
                return

        positive = item.score_delta > 0
        fill = (134, 239, 172, 255) if positive else (253, 164, 175, 255)
        outline = (8, 18, 30, 255)
        radius = 21
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill, outline=outline, width=4)
        if positive:
            points = [(x, y - 10), (x - 9, y + 8), (x + 9, y + 8)]
        else:
            points = [(x, y + 10), (x - 9, y - 8), (x + 9, y - 8)]
        draw.polygon(points, fill=(8, 18, 30, 255))

    def _draw_score_change_summary_panel(
        self,
        canvas,
        draw,
        box: tuple[int, int, int, int],
        players: list[PlayerRecord],
        events_desc: list[tuple[PlayerRecord, ScoreChangeRecord]],
    ) -> None:
        self._draw_score_panel(draw, box)
        x1, y1, x2, _ = box
        heading_font = self._font(34, bold=True)
        draw.text((x1 + 26, y1 + 22), "数据摘要", font=heading_font, fill=(237, 245, 255, 255))
        subtitle_font = self._font(20)
        draw.text((x1 + 28, y1 + 66), "基于持续视奸和持续记录采集到的最近分数变化生成", font=subtitle_font, fill=(142, 168, 194, 255))

        row_y = y1 + 112
        row_h = 174
        for player in players:
            player_events = [item for owner, item in events_desc if owner is player]
            row = (x1 + 24, row_y, x2 - 24, row_y + row_h)
            draw.rounded_rectangle(row, radius=12, fill=(16, 33, 53, 255), outline=(28, 55, 78, 255), width=2)
            rank_icon = self._resolve_rank_icon_path(player.rank_name, player.rank_div)
            icon_box = (row[0] + 24, row[1] + 28, row[0] + 126, row[1] + 130)
            if rank_icon.exists():
                self._draw_image_asset(draw, rank_icon, icon_box)
            else:
                self._draw_icon_octagon(draw, icon_box)
                self._draw_rank_icon(draw, "target", icon_box)

            name_font = self._fit_font(draw, self._record_display_name(player), 32, 22, 360, bold=True)
            draw.text((row[0] + 148, row[1] + 28), self._record_display_name(player), font=name_font, fill=(237, 245, 255, 255))
            meta = f"{self._record_rank_display(player)} · {self._format_platform(player.platform)} · {self._watch_mode_label(player)}"
            meta = self._fit_text_to_width(draw, meta, self._font(22), 470)
            draw.text((row[0] + 148, row[1] + 74), meta, font=self._font(22), fill=(185, 201, 220, 255))
            score_text = f"{self._format_rank_score_number(player.rank_score)} RP"
            draw.text((row[0] + 148, row[1] + 108), score_text, font=self._font(30, bold=True), fill=(237, 245, 255, 255))

            delta = self._score_change_net_delta(player_events)
            max_score = max([item.to_score for item in player_events] + [player.rank_score])
            min_score = min([item.to_score for item in player_events] + [player.rank_score])
            metrics = [
                ("最近净变化", f"{delta:+d}", (134, 239, 172, 255) if delta > 0 else (253, 164, 175, 255) if delta < 0 else (185, 201, 220, 255)),
                ("最高/最低", f"{max_score}/{min_score}", (237, 245, 255, 255)),
                ("变化次数", str(len(player_events)), (237, 245, 255, 255)),
                ("段位余量", self._rank_progress_text(player.rank_score), (134, 239, 172, 255)),
            ]
            metric_x = row[0] + 660
            metric_w = 240
            for label, value, color in metrics:
                label_font = self._font(18)
                value_font = self._fit_font(draw, value, 28, 18, metric_w - 24, bold=True)
                draw.text((metric_x, row[1] + 42), label, font=label_font, fill=(142, 168, 194, 255))
                draw.text((metric_x, row[1] + 76), value, font=value_font, fill=color)
                metric_x += metric_w
            row_y += row_h + 22

    def _rank_progress_text(self, score: int) -> str:
        thresholds = [
            (0, "新秀 4"),
            (250, "新秀 3"),
            (500, "新秀 2"),
            (750, "新秀 1"),
            (1000, "青铜 4"),
            (1500, "青铜 3"),
            (2000, "青铜 2"),
            (2500, "青铜 1"),
            (3250, "白银 4"),
            (3750, "白银 3"),
            (4250, "白银 2"),
            (4750, "白银 1"),
            (5500, "黄金 4"),
            (6250, "黄金 3"),
            (7000, "黄金 2"),
            (7750, "黄金 1"),
            (8500, "白金 4"),
            (9250, "白金 3"),
            (10000, "白金 2"),
            (11000, "白金 1"),
            (12000, "钻石 4"),
            (13000, "钻石 3"),
            (14000, "钻石 2"),
            (15000, "钻石 1"),
            (16000, "大师"),
        ]
        score = coerce_int(score, 0)
        current = thresholds[0]
        next_item = None
        for index, item in enumerate(thresholds):
            if score >= item[0]:
                current = item
                next_item = thresholds[index + 1] if index + 1 < len(thresholds) else None
        if next_item is None:
            return f"大师线 +{max(0, score - current[0])}"
        return f"升 {max(0, next_item[0] - score)} / 掉 {max(0, score - current[0])}"

    def _draw_score_change_event_panel(
        self,
        draw,
        box: tuple[int, int, int, int],
        events_desc: list[tuple[PlayerRecord, ScoreChangeRecord]],
    ) -> None:
        self._draw_score_panel(draw, box)
        x1, y1, x2, _ = box
        heading_font = self._font(34, bold=True)
        draw.text((x1 + 26, y1 + 22), "最近分数变化记录", font=heading_font, fill=(237, 245, 255, 255))
        draw.text((x1 + 28, y1 + 66), f"最多展示 {self._SCORE_CHANGE_MAX_LIMIT} 次有效分数变化", font=self._font(20), fill=(142, 168, 194, 255))

        inner_x = x1 + 24
        inner_w = x2 - x1 - 48
        header_y = y1 + 104
        row_h = 88
        columns = [
            ("状态", 150),
            ("时间", 310),
            ("玩家", 360),
            ("分数变化", 420),
            ("段位变化", 396),
        ]
        draw.rounded_rectangle(
            (inner_x, header_y, inner_x + inner_w, header_y + 42),
            radius=12,
            fill=(17, 38, 58, 255),
            outline=(37, 75, 105, 255),
        )
        cursor_x = inner_x + 14
        for title, col_w in columns:
            draw.text((cursor_x, header_y + 10), title, font=self._font(19, bold=True), fill=(185, 201, 220, 255))
            cursor_x += col_w

        if not events_desc:
            draw.text((inner_x + 14, header_y + 66), "暂无分数变化记录。", font=self._font(26, bold=True), fill=(185, 201, 220, 255))
            return

        for index, (player, item) in enumerate(events_desc):
            row_y = header_y + 54 + index * row_h
            positive = item.score_delta > 0
            fill = (13, 42, 36, 255) if positive else (42, 21, 28, 255)
            outline = (29, 75, 61, 255) if positive else (74, 35, 48, 255)
            draw.rounded_rectangle(
                (inner_x, row_y, inner_x + inner_w, row_y + row_h - 10),
                radius=12,
                fill=fill,
                outline=outline,
            )
            status = "上分" if positive else "掉分"
            status_color = (134, 239, 172, 255) if positive else (253, 164, 175, 255)
            values = [
                (status, status_color),
                (self._format_score_history_time(item.captured_at), (185, 201, 220, 255)),
                (self._record_display_name(player), (237, 245, 255, 255)),
                (f"{item.from_score} -> {item.to_score} ({item.score_delta:+d})", status_color),
                (f"{self._history_from_rank_label(item)} -> {self._history_to_rank_label(item)}", (237, 245, 255, 255)),
            ]
            cursor_x = inner_x + 14
            for (text, color), (_, col_w) in zip(values, columns):
                font = self._fit_font(draw, text, 21, 16, col_w - 18, bold=False)
                draw.text((cursor_x, row_y + 22), text, font=font, fill=color)
                cursor_x += col_w

    def _render_monitor_added_image(
        self,
        player_data: ApexPlayerStats,
        platform: str,
        watch_mode: str = WATCH_MODE_NOTIFY,
    ) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成监控添加确认图片")

        output_dir = self._generated_card_output_dir("monitor_added_cards")
        output_dir.mkdir(parents=True, exist_ok=True)
        image = self._build_monitor_added_card(player_data, platform, watch_mode)
        output_path = output_dir / f"monitor_added_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_generated_images(output_dir, "monitor_added_*.png", keep=12)
        return output_path

    def _build_monitor_added_card(
        self,
        player_data: ApexPlayerStats,
        platform: str,
        watch_mode: str = WATCH_MODE_NOTIFY,
    ):
        width, height = self._MONITOR_ADDED_CARD_SIZE
        canvas = Image.new("RGBA", (width, height), (7, 8, 11, 255))
        draw = ImageDraw.Draw(canvas)
        self._draw_rank_change_background(canvas)
        self._draw_rank_change_outer_frame(draw, width, height)
        self._draw_monitor_added_header(canvas, draw, watch_mode)
        self._draw_monitor_added_status(draw)

        rank_display = (
            f"{player_data.rank_name} {player_data.rank_div}"
            if player_data.rank_div
            else player_data.rank_name or "未知"
        )
        self._draw_player_profile_panel(draw, (54, 482, 518, 792), player_data)
        self._draw_rank_badge_panel(
            draw,
            (536, 482, 1070, 792),
            "当前段位",
            rank_display,
            player_data.rank_name,
            player_data.rank_div,
            secondary_value=f"{self._format_rank_score_number(player_data.rank_score)} 分",
        )
        self._draw_mini_info_pill(draw, (54, 820, 370, 920), "平台", self._format_platform(platform))
        self._draw_monitor_added_legend_pill(
            draw,
            (388, 820, 734, 920),
            player_data.selected_legend or "未知",
        )
        self._draw_mini_info_pill(draw, (752, 820, 1068, 920), "模式", self._watch_mode_label(watch_mode))
        return canvas.convert("RGB")

    def _draw_monitor_added_header(
        self,
        canvas,
        draw,
        watch_mode: str = WATCH_MODE_NOTIFY,
    ) -> None:
        box = (54, 54, 1068, 224)
        self._draw_rank_panel_base(draw, box, fill=(12, 14, 19, 238), outline_alpha=150)
        badge = self._apex_logo_badge(118)
        canvas.alpha_composite(badge, (92, 80))
        if normalize_watch_mode(watch_mode) == WATCH_MODE_RECORD:
            title = "Apex 持续记录已添加"
            subtitle = "将记录排位分数变化，但不会主动推送通知"
        else:
            title = "Apex 监控已添加"
            subtitle = "当排位分数变化时会自动推送图片通知，并写入统计历史"
        title_font = self._fit_font(draw, title, 66, 46, 690, bold=True)
        subtitle_font = self._fit_font(draw, subtitle, 32, 22, 690, bold=True)
        self._draw_text_stroked(
            draw,
            (250, self._centered_text_y(draw, title, title_font, 70, 150)),
            title,
            title_font,
            fill=(248, 248, 244, 255),
            stroke_width=2,
        )
        self._draw_text_stroked(
            draw,
            (254, self._centered_text_y(draw, subtitle, subtitle_font, 150, 202)),
            subtitle,
            subtitle_font,
            fill=(205, 212, 222, 255),
        )
        draw.rectangle((790, 214, 938, 220), fill=(236, 48, 52, 220))

    def _draw_monitor_added_status(self, draw) -> None:
        time_box = (54, 244, 1068, 338)
        self._draw_rank_panel_base(draw, time_box, fill=(14, 17, 23, 240), outline_alpha=120)
        self._draw_clock_icon(draw, (92, 265), 28)
        label_font = self._font(32, bold=True)
        value = self._now_str()
        value_font = self._fit_font(draw, value, 34, 24, 560, bold=True)
        self._draw_text_stroked(
            draw,
            (176, self._centered_text_y(draw, "时间:", label_font, time_box[1], time_box[3])),
            "时间:",
            label_font,
            fill=(226, 226, 220, 255),
        )
        self._draw_text_stroked(
            draw,
            (286, self._centered_text_y(draw, value, value_font, time_box[1], time_box[3])),
            value,
            value_font,
            fill=(244, 244, 239, 255),
        )

        status_box = (54, 358, 1068, 460)
        self._draw_rank_panel_base(draw, status_box, fill=(36, 16, 18, 242), outline_alpha=150)
        status = "已加入本群排位监控"
        status_font = self._fit_font(draw, status, 46, 30, 740, bold=True)
        self._draw_centered_stroked_text(
            draw,
            status,
            status_font,
            status_box[0],
            status_box[2],
            status_box[1],
            status_box[3],
            (255, 246, 236, 255),
            stroke_width=2,
        )

    def _draw_mini_info_pill(
        self,
        draw,
        box: tuple[int, int, int, int],
        label: str,
        value: str,
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 238), outline_alpha=92)
        label_font = self._fit_font(draw, label, 24, 18, box[2] - box[0] - 34, bold=True)
        value_font = self._fit_font(draw, str(value), 30, 21, box[2] - box[0] - 34, bold=True)
        self._draw_centered_stroked_text(
            draw,
            label,
            label_font,
            box[0] + 12,
            box[2] - 12,
            box[1] + 12,
            box[1] + 38,
            (172, 181, 194, 255),
        )
        self._draw_centered_stroked_text(
            draw,
            str(value),
            value_font,
            box[0] + 12,
            box[2] - 12,
            box[1] + 38,
            box[3] - 10,
            (250, 250, 246, 255),
            stroke_width=2,
        )

    def _draw_monitor_added_legend_pill(
        self,
        draw,
        box: tuple[int, int, int, int],
        legend_name: str,
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 238), outline_alpha=92)
        icon_box = (box[0] + 22, box[1] + 14, box[0] + 100, box[1] + 92)
        self._draw_icon_octagon(draw, icon_box)
        legend_icon = self._resolve_legend_icon_path(legend_name)
        if legend_icon.exists():
            self._draw_image_asset(draw, legend_icon, icon_box, clip_octagon=True)
        else:
            self._draw_rank_icon(draw, "legend", icon_box)

        label_font = self._fit_font(draw, "当前英雄", 23, 18, box[2] - box[0] - 134, bold=True)
        value_font = self._fit_font(draw, legend_name, 30, 21, box[2] - box[0] - 134, bold=True)
        text_left = box[0] + 116
        text_right = box[2] - 18
        self._draw_centered_stroked_text(
            draw,
            "当前英雄",
            label_font,
            text_left,
            text_right,
            box[1] + 16,
            box[1] + 42,
            (172, 181, 194, 255),
        )
        self._draw_centered_stroked_text(
            draw,
            legend_name,
            value_font,
            text_left,
            text_right,
            box[1] + 44,
            box[3] - 12,
            (250, 250, 246, 255),
            stroke_width=2,
        )

    def _generated_card_output_dir(self, name: str) -> Path:
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            return self._PLUGIN_ROOT / "_generated" / name
        return Path(data_dir) / name

    def _cleanup_generated_images(self, output_dir: Path, pattern: str, keep: int) -> None:
        try:
            files = sorted(
                output_dir.glob(pattern),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for file in files[keep:]:
                file.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"清理生成图片缓存失败: {exc}")

    @staticmethod
    def _record_rank_display(player: PlayerRecord) -> str:
        rank_name = str(getattr(player, "rank_name", "") or "未知")
        rank_div = getattr(player, "rank_div", 0) or 0
        if rank_div:
            return f"{rank_name} {rank_div}"
        return rank_name

    def _format_record_checked_at(self, value: int) -> str:
        try:
            raw = int(value)
            if raw <= 0:
                return "未知时间"
            seconds = raw / 1000 if raw > 10_000_000_000 else raw
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(SHANGHAI_TZ)
            return dt.strftime("%m-%d %H:%M")
        except Exception:
            return "未知时间"

    def _render_player_rank_image(self, player_data: ApexPlayerStats) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成玩家段位信息图片")

        output_dir = self._player_rank_card_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        image = self._build_player_rank_card(player_data)
        output_path = output_dir / f"player_rank_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_old_player_rank_cards(output_dir)
        return output_path

    def _player_rank_card_output_dir(self) -> Path:
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            return self._PLUGIN_ROOT / "_generated" / "player_rank_cards"
        return Path(data_dir) / "player_rank_cards"

    def _cleanup_old_player_rank_cards(self, output_dir: Path, keep: int = 16) -> None:
        try:
            files = sorted(
                output_dir.glob("player_rank_*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for file in files[keep:]:
                file.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"清理玩家段位信息缓存图片失败: {exc}")

    def _build_player_rank_card(self, player_data: ApexPlayerStats):
        width, height = self._PLAYER_RANK_CARD_SIZE
        canvas = Image.new("RGBA", (width, height), (7, 8, 11, 255))
        draw = ImageDraw.Draw(canvas)
        self._draw_rank_change_background(canvas)
        self._draw_rank_change_outer_frame(draw, width, height)
        self._draw_player_rank_header(draw, width)
        self._draw_rank_change_time_bar(draw)

        rank_display = (
            f"{player_data.rank_name} {player_data.rank_div}"
            if player_data.rank_div
            else player_data.rank_name or "未知"
        )
        status = player_data.current_state or ("在线" if player_data.is_online else "离线")
        platform = self._format_platform(player_data.platform)
        global_percent = self._format_rank_percent(player_data.global_rank_percent)
        legend_name = player_data.selected_legend or "未知"

        self._draw_player_profile_panel(draw, (54, 360, 518, 670), player_data)
        self._draw_rank_badge_panel(
            draw,
            (536, 360, 1070, 670),
            "当前段位",
            rank_display,
            player_data.rank_name,
            player_data.rank_div,
        )
        self._draw_player_score_panel(draw, (54, 690, 1070, 1000), player_data, rank_display, global_percent)
        self._draw_rank_detail_panel(
            draw,
            (54, 1010, 370, 1340),
            "legend",
            "当前英雄",
            legend_name,
        )
        self._draw_rank_info_panel(
            draw,
            (376, 1010, 720, 1340),
            "crown",
            "等级",
            str(player_data.level if player_data.level is not None else "未知"),
            secondary_value=f"平台 {platform}",
        )
        self._draw_rank_status_panel(draw, (724, 1010, 1070, 1340), status)
        return canvas.convert("RGB")

    def _draw_player_rank_header(self, draw, width: int) -> None:
        title_box = (54, 54, width - 54, 224)
        self._draw_rank_panel_base(draw, title_box, fill=(12, 14, 19, 238), outline_alpha=150)
        self._draw_player_card_icon(draw, (94, 82, 220, 190))
        title = "Apex 玩家档案"
        title_font = self._fit_font(draw, title, 78, 50, width - 310, bold=True)
        self._draw_text_stroked(
            draw,
            (260, self._centered_text_y(draw, title, title_font, 68, 202)),
            title,
            title_font,
            fill=(248, 248, 244, 255),
            stroke_width=2,
        )
        draw.rectangle((508, 217, 620, 222), fill=(236, 48, 52, 220))

    def _draw_player_card_icon(self, draw, box: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = box
        red = (238, 62, 66, 255)
        white = (230, 232, 229, 255)
        self._draw_icon_octagon(draw, box)
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        draw.ellipse((cx - 22, cy - 42, cx + 22, cy + 2), fill=white)
        draw.pieslice((cx - 54, cy - 6, cx + 54, cy + 78), 180, 360, fill=white)
        draw.rectangle((cx - 54, cy + 36, cx + 54, cy + 78), fill=white)
        draw.line((left + 18, bottom - 14, right - 14, top + 18), fill=red, width=8)
        draw.polygon(
            [(right - 14, top + 18), (right - 18, top + 50), (right - 46, top + 24)],
            fill=red,
        )

    def _draw_player_profile_panel(
        self,
        draw,
        box: tuple[int, int, int, int],
        player_data: ApexPlayerStats,
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=105)
        avatar_box = (box[0] + 38, box[1] + 48, box[0] + 192, box[1] + 202)
        self._draw_icon_octagon(draw, avatar_box)
        if self._DEFAULT_USER_AVATAR_PATH.exists():
            self._draw_image_asset(draw, self._DEFAULT_USER_AVATAR_PATH, avatar_box, clip_octagon=True)
        else:
            self._draw_rank_icon(draw, "player", avatar_box)

        label_font = self._font(30, bold=True)
        alias_name = str(getattr(player_data, "display_alias", "") or "").strip()
        actual_name = player_data.name or "未知"
        name = alias_name or actual_name
        name_font = self._fit_font(draw, name, 54, 34, box[2] - box[0] - 245, bold=True)
        if alias_name and actual_name and alias_name.lower() != actual_name.lower():
            uid_text = f"{actual_name} · UID {player_data.uid}" if player_data.uid else actual_name
        else:
            uid_text = f"UID {player_data.uid}" if player_data.uid else "UID 未知"
        uid_font = self._fit_font(draw, uid_text, 27, 20, box[2] - box[0] - 245)
        text_left = box[0] + 220
        text_right = box[2] - 24
        self._draw_centered_stroked_text(
            draw,
            "玩家信息",
            label_font,
            text_left,
            text_right,
            box[1] + 48,
            box[1] + 92,
            (190, 192, 196, 255),
        )
        self._draw_centered_stroked_text(
            draw,
            name,
            name_font,
            text_left,
            text_right,
            box[1] + 112,
            box[1] + 174,
            (250, 250, 246, 255),
            stroke_width=2,
        )
        self._draw_centered_stroked_text(
            draw,
            uid_text,
            uid_font,
            text_left,
            text_right,
            box[1] + 192,
            box[1] + 236,
            (170, 176, 186, 255),
        )
        draw.rectangle((text_left + 22, box[3] - 38, text_right - 22, box[3] - 32), fill=(221, 48, 52, 185))

    def _draw_player_score_panel(
        self,
        draw,
        box: tuple[int, int, int, int],
        player_data: ApexPlayerStats,
        rank_display: str,
        global_percent: str,
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 15, 19, 244), outline_alpha=135)
        draw.line((box[0] + 362, box[1] + 24, box[0] + 310, box[3] - 24), fill=(154, 40, 44, 86), width=2)
        draw.line((box[2] - 362, box[1] + 24, box[2] - 310, box[3] - 24), fill=(154, 40, 44, 86), width=2)

        score = self._format_rank_score_number(player_data.rank_score)
        score_font = self._fit_font(draw, score, 150, 96, 620, bold=True)
        rank_font = self._fit_font(draw, rank_display or "未知", 38, 26, 300, bold=True)
        label_font = self._font(34, bold=True)
        percent_font = self._fit_font(draw, global_percent or "未知", 34, 22, 250, bold=True)
        left = box[0] + 34
        right = box[2] - 34
        self._draw_centered_stroked_text(
            draw,
            "段位分数",
            label_font,
            box[0],
            box[2],
            box[1] + 42,
            box[1] + 92,
            (190, 192, 196, 255),
        )
        self._draw_centered_stroked_text(
            draw,
            score,
            score_font,
            box[0] + 160,
            box[2] - 160,
            box[1] + 98,
            box[1] + 230,
            (250, 250, 246, 255),
            stroke_width=2,
        )
        self._draw_centered_stroked_text(
            draw,
            "当前段位",
            self._font(28, bold=True),
            left,
            left + 300,
            box[3] - 76,
            box[3] - 38,
            (190, 192, 196, 255),
        )
        self._draw_centered_stroked_text(
            draw,
            rank_display or "未知",
            rank_font,
            left + 250,
            left + 570,
            box[3] - 80,
            box[3] - 34,
            (250, 250, 246, 255),
            stroke_width=2,
        )
        self._draw_centered_stroked_text(
            draw,
            f"全球前 {global_percent}" if global_percent and global_percent != "未知" else "全球排名未知",
            percent_font,
            right - 280,
            right,
            box[3] - 82,
            box[3] - 34,
            (214, 218, 224, 255),
        )
        draw.rectangle((box[0] + 24, box[3] - 28, box[0] + 296, box[3] - 22), fill=(221, 48, 52, 185))
        draw.rectangle((box[2] - 296, box[3] - 28, box[2] - 24, box[3] - 22), fill=(221, 48, 52, 185))

    def _render_rank_change_image(
        self,
        player_data: ApexPlayerStats,
        old_score: int,
        new_score: int,
        platform: str,
        is_season_reset: bool = False,
    ) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成排位分数变化图片")

        output_dir = self._rank_change_card_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        image = self._build_rank_change_card(
            player_data=player_data,
            old_score=old_score,
            new_score=new_score,
            platform=platform,
            is_season_reset=is_season_reset,
        )
        output_path = output_dir / f"rank_change_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup_old_rank_change_cards(output_dir)
        return output_path

    def _rank_change_card_output_dir(self) -> Path:
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            return self._PLUGIN_ROOT / "_generated" / "rank_change_cards"
        return Path(data_dir) / "rank_change_cards"

    def _cleanup_old_rank_change_cards(self, output_dir: Path, keep: int = 16) -> None:
        try:
            files = sorted(
                output_dir.glob("rank_change_*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for file in files[keep:]:
                file.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"清理排位分数变化缓存图片失败: {exc}")

    def _build_rank_change_card(
        self,
        player_data: ApexPlayerStats,
        old_score: int,
        new_score: int,
        platform: str,
        is_season_reset: bool,
    ):
        width, height = self._RANK_CHANGE_CARD_SIZE
        canvas = Image.new("RGBA", (width, height), (7, 8, 11, 255))
        draw = ImageDraw.Draw(canvas)
        self._draw_rank_change_background(canvas)
        self._draw_rank_change_outer_frame(draw, width, height)
        self._draw_rank_change_header(draw, width)
        self._draw_rank_change_time_bar(draw)

        rank_display = (
            f"{player_data.rank_name} {player_data.rank_div}"
            if player_data.rank_div
            else player_data.rank_name or "未知"
        )
        self._draw_rank_info_panel(
            draw,
            (54, 360, 388, 670),
            "player",
            "玩家",
            self._player_data_display_name(player_data),
        )
        self._draw_rank_info_panel(
            draw,
            (402, 360, 720, 670),
            "platform",
            "平台",
            self._format_platform(platform or player_data.platform),
        )
        self._draw_rank_badge_panel(
            draw,
            (734, 360, 1070, 670),
            "段位",
            rank_display,
            player_data.rank_name,
            player_data.rank_div,
        )
        self._draw_rank_score_change_panel(
            draw,
            old_score=old_score,
            new_score=new_score,
            is_season_reset=is_season_reset,
        )
        self._draw_rank_detail_panel(
            draw,
            (54, 1010, 370, 1340),
            "globe",
            "全球排名",
            self._format_rank_percent(player_data.global_rank_percent),
        )
        self._draw_rank_detail_panel(
            draw,
            (376, 1010, 720, 1340),
            "legend",
            "当前英雄",
            player_data.selected_legend or "未知",
        )
        status = player_data.current_state or ("在线" if player_data.is_online else "离线")
        self._draw_rank_status_panel(draw, (724, 1010, 1070, 1340), status)
        return canvas.convert("RGB")

    def _draw_rank_change_background(self, canvas) -> None:
        width, height = canvas.size
        draw = ImageDraw.Draw(canvas)
        for row in range(height):
            ratio = row / max(1, height - 1)
            shade = int(8 + 14 * ratio)
            draw.line((0, row, width, row), fill=(shade, shade + 1, shade + 4, 255))
        line_overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        line_draw = ImageDraw.Draw(line_overlay)
        for x in range(-260, width, 220):
            line_draw.line((x, 0, x + 470, height), fill=(255, 255, 255, 18), width=1)
        canvas.alpha_composite(line_overlay)
        glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.rectangle((250, 218, 872, 224), fill=(230, 43, 45, 165))
        glow_draw.rectangle((420, 1358, 760, 1365), fill=(230, 43, 45, 155))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=8))
        canvas.alpha_composite(glow)

    def _draw_rank_change_outer_frame(self, draw, width: int, height: int) -> None:
        red = (228, 48, 52, 255)
        silver = (112, 118, 126, 210)
        outer = [
            (36, 18),
            (width - 52, 18),
            (width - 18, 54),
            (width - 18, height - 68),
            (width - 60, height - 18),
            (54, height - 18),
            (18, height - 58),
            (18, 56),
        ]
        inner = [
            (50, 36),
            (width - 68, 36),
            (width - 36, 68),
            (width - 36, height - 82),
            (width - 76, height - 36),
            (68, height - 36),
            (36, height - 72),
            (36, 70),
        ]
        draw.line(outer + [outer[0]], fill=red, width=4)
        draw.line(inner + [inner[0]], fill=silver, width=2)
        for x in (70, 340, 764, 1014):
            draw.line((x, 44, x + 80, 44), fill=(224, 45, 48, 170), width=5)
        for y in (242, 688, 1000):
            draw.line((58, y, width - 58, y), fill=(210, 48, 52, 110), width=2)

    def _draw_rank_change_header(self, draw, width: int) -> None:
        title_box = (54, 54, width - 54, 224)
        self._draw_rank_panel_base(draw, title_box, fill=(12, 14, 19, 238), outline_alpha=150)
        self._draw_chart_icon(draw, (94, 82, 220, 190))
        title = "Apex 排位分数变化"
        title_font = self._fit_font(draw, title, 78, 50, width - 310, bold=True)
        self._draw_text_stroked(
            draw,
            (260, self._centered_text_y(draw, title, title_font, 68, 202)),
            title,
            title_font,
            fill=(248, 248, 244, 255),
            stroke_width=2,
        )
        draw.rectangle((508, 217, 620, 222), fill=(236, 48, 52, 220))

    def _draw_rank_change_time_bar(self, draw) -> None:
        box = (54, 244, 1070, 338)
        self._draw_rank_panel_base(draw, box, fill=(14, 17, 23, 240), outline_alpha=120)
        self._draw_clock_icon(draw, (92, 265), 28)
        label_font = self._font(34, bold=True)
        value = self._now_str()
        value_font = self._fit_font(draw, value, 37, 26, 600, bold=True)
        self._draw_text_stroked(
            draw,
            (176, self._centered_text_y(draw, "时间:", label_font, box[1], box[3])),
            "时间:",
            label_font,
            fill=(226, 226, 220, 255),
        )
        self._draw_text_stroked(
            draw,
            (288, self._centered_text_y(draw, value, value_font, box[1], box[3])),
            value,
            value_font,
            fill=(244, 244, 239, 255),
        )

    def _draw_rank_info_panel(
        self,
        draw,
        box: tuple[int, int, int, int],
        icon: str,
        label: str,
        value: str,
        secondary_value: str = "",
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=105)
        center_x = (box[0] + box[2]) // 2
        icon_box = (center_x - 58, box[1] + 48, center_x + 58, box[1] + 164)
        self._draw_icon_octagon(draw, icon_box)
        if icon == "player" and self._DEFAULT_USER_AVATAR_PATH.exists():
            self._draw_image_asset(
                draw,
                self._DEFAULT_USER_AVATAR_PATH,
                icon_box,
                clip_octagon=True,
            )
        else:
            self._draw_rank_icon(draw, icon, icon_box)
        label_font = self._font(34, bold=True)
        value_bottom = box[3] - 58 if secondary_value else box[3] - 26
        value_font = self._fit_font(draw, value, 46, 30, box[2] - box[0] - 56, bold=True)
        self._draw_centered_stroked_text(
            draw, label, label_font, box[0], box[2], box[1] + 178, box[1] + 224, (190, 192, 196, 255)
        )
        self._draw_centered_stroked_text(
            draw, value, value_font, box[0] + 18, box[2] - 18, box[1] + 232, value_bottom, (250, 250, 246, 255)
        )
        if secondary_value:
            secondary_font = self._fit_font(draw, secondary_value, 27, 20, box[2] - box[0] - 56, bold=True)
            self._draw_centered_stroked_text(
                draw,
                secondary_value,
                secondary_font,
                box[0] + 22,
                box[2] - 22,
                box[3] - 58,
                box[3] - 22,
                (207, 211, 218, 255),
            )

    def _draw_rank_badge_panel(
        self,
        draw,
        box: tuple[int, int, int, int],
        label: str,
        value: str,
        rank_name: str,
        rank_div: int,
        secondary_value: str = "",
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=105)
        center_x = (box[0] + box[2]) // 2
        rank_icon = self._resolve_rank_icon_path(rank_name, rank_div)
        if rank_icon.exists():
            self._draw_image_asset(
                draw,
                rank_icon,
                (center_x - 92, box[1] + 24, center_x + 92, box[1] + 178),
            )
            if rank_div and "_" not in rank_icon.stem and rank_icon.stem not in {"master", "predator"}:
                badge_font = self._font(42, bold=True)
                self._draw_centered_stroked_text(
                    draw,
                    str(rank_div),
                    badge_font,
                    center_x - 36,
                    center_x + 36,
                    box[1] + 84,
                    box[1] + 142,
                    (255, 255, 255, 255),
                    stroke_width=2,
                )
        else:
            self._draw_rank_diamond(draw, (center_x, box[1] + 110), rank_div)
        label_font = self._font(34, bold=True)
        label_top = box[1] + 176 if secondary_value else box[1] + 178
        label_bottom = box[1] + 216 if secondary_value else box[1] + 224
        value_top = box[1] + 214 if secondary_value else box[1] + 232
        value_bottom = box[1] + 268 if secondary_value else box[3] - 26
        value_size = 42 if secondary_value else 47
        value_font = self._fit_font(draw, value, value_size, 30, box[2] - box[0] - 50, bold=True)
        self._draw_centered_stroked_text(
            draw, label, label_font, box[0], box[2], label_top, label_bottom, (190, 192, 196, 255)
        )
        self._draw_centered_stroked_text(
            draw, value, value_font, box[0], box[2], value_top, value_bottom, (250, 250, 246, 255)
        )
        if secondary_value:
            secondary_font = self._fit_font(draw, secondary_value, 27, 20, box[2] - box[0] - 60, bold=True)
            self._draw_centered_stroked_text(
                draw,
                secondary_value,
                secondary_font,
                box[0] + 30,
                box[2] - 30,
                box[1] + 266,
                box[3] - 18,
                (214, 218, 224, 255),
            )

    def _draw_rank_score_change_panel(
        self,
        draw,
        old_score: int,
        new_score: int,
        is_season_reset: bool,
    ) -> None:
        box = (52, 690, 1070, 992)
        self._draw_rank_panel_base(draw, box, fill=(13, 15, 19, 244), outline_alpha=130)
        draw.line((424, 716, 365, 966), fill=(154, 40, 44, 86), width=2)
        draw.line((698, 716, 758, 966), fill=(154, 40, 44, 86), width=2)

        diff = new_score - old_score
        color = (88, 210, 126, 255) if diff > 0 else (238, 62, 66, 255)
        direction = "上升" if diff > 0 else "下降"
        sign_text = f"+{diff}" if diff > 0 else str(diff)
        if is_season_reset:
            direction = "赛季重置"
        old_text = self._format_rank_score_number(old_score)
        new_text = self._format_rank_score_number(new_score)
        old_font = self._fit_font(draw, old_text, 94, 62, 320, bold=True)
        new_font = self._fit_font(draw, new_text, 94, 62, 320, bold=True)
        label_font = self._font(34, bold=True)
        delta_font = self._fit_font(draw, sign_text, 76, 46, 220, bold=True)
        desc = f"{direction} {abs(diff)} 分" if not is_season_reset else f"下降 {abs(diff)} 分"
        desc_font = self._fit_font(draw, desc, 38, 26, 248, bold=True)

        self._draw_centered_stroked_text(
            draw, "原分数", label_font, 74, 404, 724, 780, (198, 202, 207, 255)
        )
        self._draw_centered_stroked_text(
            draw, old_text, old_font, 74, 404, 794, 906, (248, 248, 244, 255), stroke_width=2
        )
        self._draw_centered_stroked_text(
            draw, "当前分数", label_font, 718, 1048, 724, 780, (198, 202, 207, 255)
        )
        self._draw_centered_stroked_text(
            draw, new_text, new_font, 718, 1048, 794, 906, (248, 248, 244, 255), stroke_width=2
        )
        self._draw_delta_arrows(draw, (561, 743), diff)
        self._draw_centered_stroked_text(
            draw, sign_text, delta_font, 448, 674, 790, 870, color, stroke_width=2
        )
        self._draw_centered_stroked_text(
            draw, desc, desc_font, 430, 692, 882, 944, color, stroke_width=2
        )
        draw.rectangle((118, 944, 382, 950), fill=(221, 48, 52, 185))
        draw.rectangle((740, 944, 1002, 950), fill=(221, 48, 52, 185))

    def _draw_rank_detail_panel(
        self,
        draw,
        box: tuple[int, int, int, int],
        icon: str,
        label: str,
        value: str,
        subtitle: str = "",
    ) -> None:
        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=100)
        center_x = (box[0] + box[2]) // 2
        icon_box = (center_x - 58, box[1] + 48, center_x + 58, box[1] + 164)
        self._draw_icon_octagon(draw, icon_box)
        legend_icon = self._resolve_legend_icon_path(value) if icon == "legend" else Path("")
        if icon == "legend" and legend_icon.exists():
            self._draw_image_asset(draw, legend_icon, icon_box, clip_octagon=True)
        else:
            self._draw_rank_icon(draw, icon, icon_box)
        label_font = self._font(32, bold=True)
        value_font = self._fit_font(draw, value, 48, 30, box[2] - box[0] - 56, bold=True)
        label_top = box[1] + 174 if subtitle else box[1] + 178
        label_bottom = box[1] + 216 if subtitle else box[1] + 226
        value_top = box[1] + 216 if subtitle else box[1] + 238
        value_bottom = box[1] + 272 if subtitle else box[3] - 32
        self._draw_centered_stroked_text(
            draw, label, label_font, box[0], box[2], label_top, label_bottom, (190, 192, 196, 255)
        )
        self._draw_centered_stroked_text(
            draw, value, value_font, box[0] + 22, box[2] - 22, value_top, value_bottom, (250, 250, 246, 255)
        )
        if subtitle:
            subtitle_font = self._fit_font(draw, subtitle, 26, 18, box[2] - box[0] - 56, bold=True)
            self._draw_centered_stroked_text(
                draw,
                subtitle,
                subtitle_font,
                box[0] + 22,
                box[2] - 22,
                box[1] + 270,
                box[3] - 22,
                (207, 211, 218, 255),
            )

    def _draw_rank_status_panel(
        self, draw, box: tuple[int, int, int, int], status: str
    ) -> None:
        status_badge = self._resolve_status_badge_path(status)
        if status_badge.exists():
            self._draw_image_asset(draw, status_badge, box)
            return

        self._draw_rank_panel_base(draw, box, fill=(13, 16, 21, 240), outline_alpha=100)
        center_x = (box[0] + box[2]) // 2
        icon_box = (center_x - 58, box[1] + 48, center_x + 58, box[1] + 164)
        self._draw_icon_octagon(draw, icon_box)
        self._draw_rank_icon(draw, "target", icon_box)
        label_font = self._font(32, bold=True)
        status_font = self._fit_font(draw, status or "未知", 42, 28, 206, bold=True)
        self._draw_centered_stroked_text(
            draw, "当前状态", label_font, box[0], box[2], box[1] + 178, box[1] + 226, (190, 192, 196, 255)
        )
        button = (box[0] + 58, box[1] + 238, box[2] - 58, box[1] + 320)
        draw.rounded_rectangle(button, radius=8, fill=(186, 42, 42, 245), outline=(255, 98, 84, 230), width=2)
        self._draw_centered_stroked_text(
            draw, status or "未知", status_font, button[0], button[2], button[1], button[3], (255, 247, 235, 255)
        )

    def _draw_rank_panel_base(
        self,
        draw,
        box: tuple[int, int, int, int],
        fill: tuple[int, int, int, int],
        outline_alpha: int,
    ) -> None:
        draw.rounded_rectangle(box, radius=8, fill=fill, outline=(92, 98, 108, 150), width=2)
        inset = (box[0] + 8, box[1] + 8, box[2] - 8, box[3] - 8)
        draw.rounded_rectangle(inset, radius=7, outline=(224, 48, 52, outline_alpha), width=2)
        draw.line((box[0] + 24, box[1] + 28, box[0] + 118, box[1] + 28), fill=(220, 48, 52, 120), width=2)
        draw.line((box[2] - 118, box[3] - 28, box[2] - 24, box[3] - 28), fill=(220, 48, 52, 120), width=2)

    def _draw_centered_stroked_text(
        self,
        draw,
        text: str,
        font,
        left: int,
        right: int,
        top: int,
        bottom: int,
        fill: tuple[int, int, int, int],
        stroke_width: int = 1,
    ) -> None:
        text = str(text or "")
        x = self._centered_text_x(draw, text, font, left, right)
        y = self._centered_text_y(draw, text, font, top, bottom)
        self._draw_text_stroked(draw, (x, y), text, font, fill=fill, stroke_width=stroke_width)

    @staticmethod
    def _draw_text_stroked(
        draw,
        xy,
        text: str,
        font,
        fill: tuple[int, int, int, int],
        stroke_width: int = 1,
    ) -> None:
        draw.text(
            xy,
            text,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=(0, 0, 0, 205),
        )

    def _draw_icon_octagon(self, draw, box: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = box
        cut = 24
        points = [
            (left + cut, top),
            (right - cut, top),
            (right, top + cut),
            (right, bottom - cut),
            (right - cut, bottom),
            (left + cut, bottom),
            (left, bottom - cut),
            (left, top + cut),
        ]
        draw.polygon(points, fill=(23, 24, 29, 230), outline=(218, 49, 52, 190))

    def _draw_image_asset(
        self,
        draw,
        path: Path,
        box: tuple[int, int, int, int],
        clip_octagon: bool = False,
    ) -> None:
        try:
            with Image.open(path) as raw:
                image = raw.convert("RGBA")
        except Exception as exc:
            logger.debug(f"读取图片资源失败 {path}: {exc}")
            return

        alpha_box = image.getchannel("A").getbbox()
        if alpha_box:
            image = image.crop(alpha_box)

        width = max(1, box[2] - box[0])
        height = max(1, box[3] - box[1])
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image = ImageOps.contain(image, (width, height), method=resampling)
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        canvas.alpha_composite(image, ((width - image.width) // 2, (height - image.height) // 2))
        if clip_octagon:
            mask = Image.new("L", (width, height), 0)
            mask_draw = ImageDraw.Draw(mask)
            cut = max(12, min(width, height) // 5)
            mask_draw.polygon(
                [
                    (cut, 0),
                    (width - cut, 0),
                    (width, cut),
                    (width, height - cut),
                    (width - cut, height),
                    (cut, height),
                    (0, height - cut),
                    (0, cut),
                ],
                fill=255,
            )
            clipped = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            clipped.paste(canvas, (0, 0), mask)
            canvas = clipped
        target = getattr(draw, "_image", None)
        if target is not None:
            try:
                if getattr(target, "mode", "") == "RGBA":
                    target.alpha_composite(canvas, (box[0], box[1]))
                else:
                    target.paste(canvas.convert(target.mode), (box[0], box[1]), canvas.getchannel("A"))
                return
            except Exception as exc:
                logger.debug(f"粘贴图片资源失败 {path}: {exc}")

        try:
            draw.bitmap((box[0], box[1]), canvas)
        except Exception as exc:
            logger.debug(f"绘制图片资源失败 {path}: {exc}")

    def _resolve_rank_icon_path(self, rank_name: str, rank_div: int = 0) -> Path:
        normalized = self._normalize_asset_token(rank_name)
        aliases = {
            "rookie": "rookie",
            "unranked": "rookie",
            "novice": "rookie",
            "菜鸟": "rookie",
            "青铜": "bronze",
            "bronze": "bronze",
            "白银": "silver",
            "silver": "silver",
            "黄金": "gold",
            "gold": "gold",
            "白金": "platinum",
            "platinum": "platinum",
            "铂金": "platinum",
            "钻石": "diamond",
            "diamond": "diamond",
            "大师": "master",
            "master": "master",
            "猎杀": "predator",
            "apex猎杀者": "predator",
            "apexpredator": "predator",
            "predator": "predator",
        }
        key = aliases.get(normalized)
        if key is None and "猎杀" in normalized:
            key = "predator"
        if key is None:
            key = normalized
        if key in {"rookie", "bronze", "silver", "gold", "platinum", "diamond"}:
            try:
                division = int(rank_div)
            except Exception:
                division = 0
            if division in {1, 2, 3, 4}:
                division_path = self._RANK_ICON_DIR / f"{key}_{division}.png"
                if division_path.exists():
                    return division_path
        return self._RANK_ICON_DIR / f"{key}.png"

    def _resolve_legend_icon_path(self, legend_name: str) -> Path:
        normalized = self._normalize_asset_token(legend_name)
        aliases = {
            "艾许": "ash",
            "ash": "ash",
            "班加罗尔": "bangalore",
            "bangalore": "bangalore",
            "寻血猎犬": "bloodhound",
            "bloodhound": "bloodhound",
            "卡特莉丝": "catalyst",
            "催化姬": "catalyst",
            "catalyst": "catalyst",
            "侵蚀": "caustic",
            "caustic": "caustic",
            "密客": "crypto",
            "crypto": "crypto",
            "暴雷": "fuse",
            "fuse": "fuse",
            "直布罗陀": "gibraltar",
            "gibraltar": "gibraltar",
            "地平线": "horizon",
            "horizon": "horizon",
            "命脉": "lifeline",
            "lifeline": "lifeline",
            "罗芭": "loba",
            "loba": "loba",
            "疯玛吉": "mad_maggie",
            "madmaggie": "mad_maggie",
            "madmaggie": "mad_maggie",
            "幻象": "mirage",
            "mirage": "mirage",
            "纽卡斯尔": "newcastle",
            "newcastle": "newcastle",
            "动力小子": "octane",
            "octane": "octane",
            "探路者": "pathfinder",
            "pathfinder": "pathfinder",
            "兰伯特": "rampart",
            "rampart": "rampart",
            "亡灵": "revenant",
            "revenant": "revenant",
            "希尔": "seer",
            "seer": "seer",
            "琉雀": "sparrow",
            "麻雀": "sparrow",
            "sparrow": "sparrow",
            "瓦尔基里": "valkyrie",
            "valkyrie": "valkyrie",
            "万蒂奇": "vantage",
            "vantage": "vantage",
            "沃特森": "wattson",
            "wattson": "wattson",
            "恶灵": "wraith",
            "wraith": "wraith",
            "导管": "conduit",
            "导线管": "conduit",
            "conduit": "conduit",
            "弹道": "ballistic",
            "ballistic": "ballistic",
            "变幻": "alter",
            "alter": "alter",
            "艾克赛尔": "axle",
            "axle": "axle",
        }
        key = aliases.get(normalized, normalized)
        return self._LEGEND_ICON_DIR / f"{key}.png"

    def _resolve_status_badge_path(self, status: str) -> Path:
        normalized = self._normalize_asset_token(status)
        aliases = {
            "比赛中": "in_match",
            "正在比赛": "in_match",
            "游戏中": "in_match",
            "inmatch": "in_match",
            "match": "in_match",
            "在大厅": "in_lobby",
            "大厅中": "in_lobby",
            "等待中": "in_lobby",
            "inlobby": "in_lobby",
            "lobby": "in_lobby",
            "离线": "offline",
            "offline": "offline",
        }
        key = aliases.get(normalized)
        if key is None and ("比赛" in normalized or "match" in normalized):
            key = "in_match"
        if key is None and ("大厅" in normalized or "lobby" in normalized):
            key = "in_lobby"
        if key is None and ("离线" in normalized or "offline" in normalized):
            key = "offline"
        if key is None:
            key = normalized
        return self._STATUS_BADGE_DIR / f"{key}.png"

    @staticmethod
    def _normalize_asset_token(value: str) -> str:
        text = str(value or "").strip()
        text = text.replace("·", "").replace("'", "").replace("’", "")
        text = text.replace("-", "").replace("_", "").replace(" ", "")
        return text.lower()

    def _draw_rank_icon(self, draw, icon: str, box: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = box
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        white = (244, 241, 235, 255)
        if icon == "player":
            draw.ellipse((cx - 20, cy - 38, cx + 20, cy + 2), fill=white)
            draw.pieslice((cx - 52, cy - 4, cx + 52, cy + 76), 180, 360, fill=white)
            draw.rectangle((cx - 52, cy + 36, cx + 52, cy + 76), fill=white)
        elif icon == "platform":
            self._draw_monitor_icon(draw, (cx, cy), 76, white)
        elif icon == "globe":
            draw.ellipse((cx - 42, cy - 42, cx + 42, cy + 42), outline=white, width=5)
            draw.arc((cx - 22, cy - 42, cx + 22, cy + 42), 90, 270, fill=white, width=4)
            draw.arc((cx - 22, cy - 42, cx + 22, cy + 42), -90, 90, fill=white, width=4)
            draw.line((cx - 42, cy, cx + 42, cy), fill=white, width=4)
            draw.line((cx, cy - 42, cx, cy + 42), fill=white, width=4)
        elif icon == "legend":
            draw.ellipse((cx - 30, cy - 16, cx + 30, cy + 40), outline=white, width=5)
            spikes = [
                (cx - 38, cy - 8), (cx - 24, cy - 44), (cx - 12, cy - 16),
                (cx, cy - 54), (cx + 12, cy - 16), (cx + 28, cy - 44),
                (cx + 38, cy - 8),
            ]
            draw.line(spikes, fill=white, width=8, joint="curve")
            draw.ellipse((cx - 26, cy + 2, cx - 2, cy + 24), outline=white, width=5)
            draw.ellipse((cx + 2, cy + 2, cx + 26, cy + 24), outline=white, width=5)
            draw.line((cx - 2, cy + 13, cx + 2, cy + 13), fill=white, width=4)
        elif icon == "target":
            draw.ellipse((cx - 38, cy - 38, cx + 38, cy + 38), outline=white, width=5)
            draw.ellipse((cx - 15, cy - 15, cx + 15, cy + 15), outline=white, width=4)
            draw.line((cx - 56, cy, cx + 56, cy), fill=white, width=4)
            draw.line((cx, cy - 56, cx, cy + 56), fill=white, width=4)
        elif icon == "crown":
            crown = [
                (cx - 48, cy + 34),
                (cx - 38, cy - 26),
                (cx - 14, cy + 4),
                (cx, cy - 42),
                (cx + 14, cy + 4),
                (cx + 38, cy - 26),
                (cx + 48, cy + 34),
            ]
            draw.line(crown, fill=white, width=8, joint="curve")
            draw.rounded_rectangle((cx - 44, cy + 28, cx + 44, cy + 50), radius=4, fill=white)

    def _draw_monitor_icon(self, draw, center: tuple[int, int], size: int, fill) -> None:
        cx, cy = center
        w = size
        h = int(size * 0.58)
        draw.rounded_rectangle((cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2), radius=6, outline=fill, width=7)
        draw.line((cx, cy + h // 2, cx, cy + h // 2 + 22), fill=fill, width=7)
        draw.line((cx - 34, cy + h // 2 + 24, cx + 34, cy + h // 2 + 24), fill=fill, width=7)

    def _draw_chart_icon(self, draw, box: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = box
        white = (230, 232, 229, 255)
        red = (238, 62, 66, 255)
        draw.rectangle((left, top, right, bottom), outline=white, width=3)
        for index in range(1, 4):
            x = left + (right - left) * index // 4
            y = top + (bottom - top) * index // 4
            draw.line((x, top, x, bottom), fill=(white[0], white[1], white[2], 70), width=2)
            draw.line((left, y, right, y), fill=(white[0], white[1], white[2], 70), width=2)
        points = [
            (left + 18, bottom - 18),
            (left + 46, bottom - 52),
            (left + 72, bottom - 28),
            (left + 100, top + 42),
            (right - 10, top + 16),
        ]
        draw.line(points, fill=red, width=9, joint="curve")
        draw.polygon(
            [(right - 10, top + 16), (right - 12, top + 52), (right - 42, top + 24)],
            fill=red,
        )

    def _draw_clock_icon(self, draw, center: tuple[int, int], radius: int) -> None:
        cx, cy = center
        red = (238, 62, 66, 255)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=red, width=5)
        draw.line((cx, cy, cx, cy - 18), fill=red, width=5)
        draw.line((cx, cy, cx + 18, cy + 10), fill=red, width=5)

    def _draw_rank_diamond(self, draw, center: tuple[int, int], rank_div: int) -> None:
        cx, cy = center
        outer = [(cx, cy - 74), (cx + 82, cy), (cx, cy + 74), (cx - 82, cy)]
        inner = [(cx, cy - 52), (cx + 58, cy), (cx, cy + 52), (cx - 58, cy)]
        draw.polygon(outer, fill=(45, 47, 122, 235), outline=(184, 188, 255, 255))
        draw.line(outer + [outer[0]], fill=(158, 164, 255, 255), width=5)
        draw.polygon(inner, fill=(35, 31, 84, 245), outline=(230, 232, 255, 205))
        for offset in (94, -94):
            draw.line((cx + offset, cy, cx + offset // 2, cy - 42), fill=(156, 188, 255, 165), width=5)
            draw.line((cx + offset, cy, cx + offset // 2, cy + 42), fill=(156, 188, 255, 165), width=5)
        number = str(rank_div or "")
        if number:
            font = self._font(58, bold=True)
            self._draw_centered_stroked_text(
                draw, number, font, cx - 48, cx + 48, cy - 42, cy + 48, (255, 255, 255, 255), stroke_width=2
            )

    def _draw_delta_arrows(self, draw, center: tuple[int, int], diff: int) -> None:
        cx, cy = center
        color = (88, 210, 126, 255) if diff > 0 else (238, 62, 66, 255)
        direction = -1 if diff > 0 else 1
        for offset in (0, 38):
            y = cy + offset * direction
            if diff > 0:
                points = [(cx, y - 30), (cx - 42, y + 16), (cx - 22, y + 16), (cx, y - 7), (cx + 22, y + 16), (cx + 42, y + 16)]
            else:
                points = [(cx, y + 30), (cx - 42, y - 16), (cx - 22, y - 16), (cx, y + 7), (cx + 22, y - 16), (cx + 42, y - 16)]
            draw.line(points, fill=color, width=10, joint="curve")

    def _format_rank_percent(self, value: str) -> str:
        text = str(value or "").strip()
        if not text or text == "未知":
            return "未知"
        if text.endswith("%"):
            return text
        return f"{text}%"

    @staticmethod
    def _format_rank_score_number(value: int) -> str:
        try:
            return str(int(value))
        except Exception:
            return str(value)

    def _render_season_info_image(self, season_info: SeasonInfo) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成赛季图片")

        output_dir = self._season_card_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        rendered_at = datetime.now(timezone.utc)
        cache_key = self._season_info_image_cache_key(
            output_dir,
            season_info,
            now=rendered_at,
        )
        cached_path = self._get_cached_season_info_image(cache_key)
        if cached_path is not None:
            return cached_path

        image = self._build_season_info_card(season_info, now=rendered_at)
        output_path = output_dir / f"season_info_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._set_cached_season_info_image(cache_key, output_path)
        self._cleanup_old_season_cards(output_dir)
        return output_path

    def _get_cached_season_info_image(self, cache_key: tuple) -> Path | None:
        cache = getattr(self, "_season_info_image_cache", None)
        if not isinstance(cache, dict):
            return None
        cached = cache.get(cache_key)
        if not cached:
            return None
        saved_at, image_path = cached
        if time.monotonic() - saved_at > self._SEASON_IMAGE_CACHE_TTL_SECONDS:
            cache.pop(cache_key, None)
            return None
        image_path = Path(image_path)
        if not image_path.exists():
            cache.pop(cache_key, None)
            return None
        return image_path

    def _set_cached_season_info_image(self, cache_key: tuple, image_path: Path) -> None:
        self._set_bounded_image_cache_entry(
            cache_attr="_season_info_image_cache",
            cache_key=cache_key,
            image_path=image_path,
            ttl_seconds=self._SEASON_IMAGE_CACHE_TTL_SECONDS,
            max_entries=self._SEASON_IMAGE_CACHE_MAX_ENTRIES,
        )

    def _set_bounded_image_cache_entry(
        self,
        *,
        cache_attr: str,
        cache_key: tuple,
        image_path: Path,
        ttl_seconds: float,
        max_entries: int,
    ) -> None:
        cache = getattr(self, cache_attr, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, cache_attr, cache)
        saved_at = time.monotonic()
        for existing_key, (existing_saved_at, existing_path) in list(cache.items()):
            if (
                saved_at - existing_saved_at > ttl_seconds
                or not Path(existing_path).exists()
            ):
                cache.pop(existing_key, None)

        cache[cache_key] = (saved_at, Path(image_path))
        excess = len(cache) - max_entries
        if excess <= 0:
            return
        oldest_keys = sorted(
            (item for item in cache.items() if item[0] != cache_key),
            key=lambda item: item[1][0],
        )
        for existing_key, _cached in oldest_keys[:excess]:
            cache.pop(existing_key, None)

    def _season_info_image_cache_key(
        self,
        output_dir: Path,
        season_info: SeasonInfo,
        *,
        now: datetime | None = None,
    ) -> tuple:
        effective_now = now or datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)
        else:
            effective_now = effective_now.astimezone(timezone.utc)
        minute_bucket = effective_now.replace(second=0, microsecond=0).isoformat()
        return (
            str(output_dir.resolve()),
            self._SEASON_CARD_RENDERER_VERSION,
            minute_bucket,
            season_info.season_number,
            getattr(season_info, "split_index", None),
            getattr(season_info, "ranked_season_id", ""),
            season_info.start_iso,
            season_info.end_iso,
            getattr(season_info, "start_date", ""),
            getattr(season_info, "end_date", ""),
            getattr(season_info, "status_text", ""),
            getattr(season_info, "source", ""),
            getattr(season_info, "split_note", ""),
            getattr(season_info, "current_split_label", ""),
            getattr(season_info, "next_transition_label", ""),
            getattr(season_info, "next_transition_iso", ""),
        )

    def _season_card_output_dir(self) -> Path:
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            return self._PLUGIN_ROOT / "_generated" / "season_cards"
        return Path(data_dir) / "season_cards"

    def _cleanup_old_season_cards(self, output_dir: Path, keep: int = 8) -> None:
        try:
            files = sorted(
                output_dir.glob("season_info_*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for file in files[keep:]:
                file.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"清理赛季缓存图片失败: {exc}")

    def _build_season_info_card(
        self, season_info: SeasonInfo, now: datetime | None = None
    ):
        width, height = self._SEASON_CARD_SIZE
        canvas = Image.new("RGBA", (width, height), (10, 11, 14, 255))
        draw = ImageDraw.Draw(canvas)
        effective_now = now or datetime.now(timezone.utc)

        # 使用 Apex 红黑配色，保证 QQ 缩略图里也能快速读到赛季和结束时间。
        for row in range(height):
            ratio = row / max(1, height - 1)
            red = int(12 + 34 * ratio)
            green = int(13 + 7 * ratio)
            blue = int(17 + 5 * ratio)
            draw.line((0, row, width, row), fill=(red, green, blue, 255))
        apex_red = (216, 35, 42, 255)
        apex_amber = (240, 174, 72, 255)
        draw.polygon((560, 0, width, 0, width, height, 690, height), fill=(94, 15, 20, 215))
        draw.polygon((716, 0, width, 0, width, 136, 794, 94), fill=(236, 50, 42, 205))
        draw.polygon((0, 0, 286, 0, 196, height, 0, height), fill=(18, 21, 27, 210))
        draw.rectangle((0, 0, width, 9), fill=apex_red)
        draw.rectangle((0, height - 56, width, height), fill=(6, 7, 10, 175))
        for x in range(120, width, 168):
            draw.line((x, 18, x - 76, height - 16), fill=(255, 255, 255, 12), width=2)

        season_label = self._season_card_label(season_info)
        end_time = self._to_beijing_time(season_info.end_iso) or season_info.end_date or "未知"
        start_weekday_time = (
            self._to_beijing_time_with_weekday(season_info.start_iso)
            or season_info.start_date
            or "未知"
        )
        end_weekday_time = (
            self._to_beijing_time_with_weekday(season_info.end_iso)
            or season_info.end_date
            or "未知"
        )
        progress = self._season_progress_fraction(season_info, now=effective_now)
        season_end_remaining = self._format_season_end_remaining(
            season_info, now=effective_now
        )

        logo = self._apex_logo_badge(84)
        canvas.alpha_composite(logo, (28, 24))

        header_font = self._font(20, bold=True)
        brand_font = self._font(16, bold=True)
        title_max_width = 540
        title_font = self._fit_font(
            draw, season_label, 38, 28, title_max_width, bold=True
        )
        display_season_label = self._fit_text_to_width(
            draw, season_label, title_font, title_max_width
        )
        panel = (30, 148, 810, 218)
        label_bounds = (54, 148, 238, 218)
        time_bounds = (248, 148, 790, 218)
        label_font = self._fit_font(draw, self._season_end_label(), 23, 18, 172, bold=True)
        value_font = self._fit_font(draw, end_time, 38, 28, 520, bold=True)
        source_text = self._season_source_label(season_info.source)
        source_font = self._fit_font(draw, source_text, 15, 12, 300, bold=False)

        draw.text((132, 29), "APEX LEGENDS", font=brand_font, fill=apex_red)
        self._draw_text_with_shadow(
            draw,
            (132, 52),
            "当前分段",
            header_font,
            fill=(236, 239, 244, 255),
        )
        self._draw_text_with_shadow(
            draw,
            (132, 79),
            display_season_label,
            title_font,
            fill=(255, 255, 255, 255),
        )
        if season_end_remaining:
            # 倒计时放在右上角，避免与结束日期主面板争夺视觉层级。
            remaining_font = self._fit_font(
                draw,
                season_end_remaining,
                18,
                12,
                220,
                bold=True,
            )
            remaining_text_box = draw.textbbox(
                (0, 0), season_end_remaining, font=remaining_font
            )
            remaining_text_width = remaining_text_box[2] - remaining_text_box[0]
            pill_width = min(244, remaining_text_width + 24)
            pill_right = 810
            pill_left = max(560, pill_right - pill_width)
            remaining_pill = (pill_left, 32, pill_right, 72)
            draw.rounded_rectangle(
                remaining_pill,
                radius=10,
                fill=(15, 18, 24, 220),
                outline=(240, 174, 72, 220),
                width=1,
            )
            remaining_x = self._centered_text_x(
                draw,
                season_end_remaining,
                remaining_font,
                remaining_pill[0],
                remaining_pill[2],
            )
            remaining_y = self._centered_text_y(
                draw,
                season_end_remaining,
                remaining_font,
                remaining_pill[1],
                remaining_pill[3],
            )
            self._draw_text_with_shadow(
                draw,
                (remaining_x, remaining_y),
                season_end_remaining,
                remaining_font,
                fill=apex_amber,
            )

        draw.rounded_rectangle(panel, radius=8, fill=(15, 18, 24, 238))
        draw.rounded_rectangle(panel, radius=8, outline=(232, 43, 45, 160), width=2)
        draw.rounded_rectangle((30, 148, 44, 218), radius=7, fill=apex_red)
        end_label = self._season_end_label()
        self._draw_text_with_shadow(
            draw,
            (
                label_bounds[0],
                self._centered_text_y(
                    draw, end_label, label_font, label_bounds[1], label_bounds[3]
                ),
            ),
            end_label,
            label_font,
            fill=(232, 215, 191, 255),
        )
        time_x = self._centered_text_x(
            draw, end_time, value_font, time_bounds[0], time_bounds[2]
        )
        self._draw_text_with_shadow(
            draw,
            (
                time_x,
                self._centered_text_y(
                    draw, end_time, value_font, time_bounds[1], time_bounds[3]
                ),
            ),
            end_time,
            value_font,
            fill=(255, 246, 228, 255),
        )

        source_box = draw.textbbox((0, 0), source_text, font=source_font)
        source_width = source_box[2] - source_box[0]
        source_x = 810 - source_width - source_box[0]
        self._draw_text_with_shadow(
            draw,
            (source_x, 122),
            source_text,
            source_font,
            fill=(178, 190, 204, 230),
        )

        bar_x, bar_y, bar_w, bar_h = 48, 252, 744, 14
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
            radius=7,
            fill=(50, 55, 64, 255),
        )
        self._draw_gradient_progress_bar(
            canvas,
            (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
            progress,
            apex_red,
            apex_amber,
        )

        timeline_label_font = self._font(17, bold=True)
        start_time_font = self._fit_font(
            draw, start_weekday_time, 16, 15, 170, bold=False
        )
        end_time_font = self._fit_font(
            draw, end_weekday_time, 16, 15, 170, bold=False
        )
        self._draw_text_with_shadow(
            draw,
            (48, 275),
            "分段开始",
            timeline_label_font,
            fill=(220, 224, 232, 245),
        )
        self._draw_text_with_shadow(
            draw,
            (48, 299),
            start_weekday_time,
            start_time_font,
            fill=(191, 198, 209, 235),
        )

        end_label_text = "分段结束"
        end_label_width = draw.textbbox(
            (0, 0), end_label_text, font=timeline_label_font
        )[2]
        end_time_width = draw.textbbox(
            (0, 0), end_weekday_time, font=end_time_font
        )[2]
        self._draw_text_with_shadow(
            draw,
            (792 - end_label_width, 275),
            end_label_text,
            timeline_label_font,
            fill=(220, 224, 232, 245),
        )
        self._draw_text_with_shadow(
            draw,
            (792 - end_time_width, 299),
            end_weekday_time,
            end_time_font,
            fill=(191, 198, 209, 235),
        )

        disclaimer_lines, disclaimer_font = self._season_card_disclaimer_lines(
            draw,
            season_info,
            max_width=width - 80,
        )
        disclaimer_y_positions = (329,) if len(disclaimer_lines) == 1 else (320, 339)
        for disclaimer, disclaimer_y in zip(disclaimer_lines, disclaimer_y_positions):
            disclaimer_x = self._centered_text_x(
                draw, disclaimer, disclaimer_font, 24, width - 24
            )
            self._draw_text_with_shadow(
                draw,
                (disclaimer_x, disclaimer_y),
                disclaimer,
                disclaimer_font,
                fill=(170, 176, 186, 235),
            )

        return canvas.convert("RGB")

    @staticmethod
    def _season_end_label() -> str:
        return "分段结束时间"

    @staticmethod
    def _season_source_label(source: str) -> str:
        text = str(source or "").strip()
        if not text:
            return "来源 未知"
        parsed = urlsplit(text if "://" in text else f"https://{text}")
        host = parsed.netloc or parsed.path.split("/")[0]
        return f"来源 {host or text}"

    def _season_card_disclaimer_lines(
        self,
        draw,
        season_info: SeasonInfo,
        *,
        max_width: int,
    ) -> tuple[tuple[str, ...], object | None]:
        split_note = str(getattr(season_info, "split_note", "") or "").strip()
        if not split_note:
            return (), None
        font = self._fit_font(draw, split_note, 15, 8, max_width, bold=False)
        fitted_text = self._fit_text_to_width(draw, split_note, font, max_width)
        return (fitted_text,), font

    @staticmethod
    def _centered_text_x(draw, text: str, font, left: int, right: int) -> float:
        box = draw.textbbox((0, 0), text, font=font)
        return left + ((right - left) - (box[2] - box[0])) / 2

    @staticmethod
    def _centered_text_y(draw, text: str, font, top: int, bottom: int) -> float:
        box = draw.textbbox((0, 0), text, font=font)
        return top + ((bottom - top) - (box[3] - box[1])) / 2 - box[1]

    @staticmethod
    def _draw_gradient_progress_bar(
        canvas,
        box: tuple[int, int, int, int],
        progress: float,
        start_color: tuple[int, int, int, int],
        end_color: tuple[int, int, int, int],
    ) -> None:
        left, top, right, bottom = box
        width = max(0, int((right - left) * min(1.0, max(0.0, progress))))
        height = bottom - top
        if width <= 0 or height <= 0:
            return

        gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pixels = gradient.load()
        for x in range(width):
            ratio = x / max(1, width - 1)
            color = tuple(
                int(start_color[index] + (end_color[index] - start_color[index]) * ratio)
                for index in range(4)
            )
            for y in range(height):
                pixels[x, y] = color

        mask = Image.new("L", (width, height), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, width, height), radius=height // 2, fill=255)
        clipped = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        clipped.paste(gradient, (0, 0), mask)
        canvas.alpha_composite(clipped, (left, top))

    def _apex_logo_badge(self, size: int):
        badge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(badge)
        draw.ellipse((0, 0, size - 1, size - 1), fill=(7, 9, 13, 238))
        draw.ellipse((4, 4, size - 5, size - 5), outline=(232, 43, 45, 255), width=3)

        logo_path = self._PLUGIN_ROOT / "logo.png"
        if logo_path.exists():
            with Image.open(logo_path) as raw:
                logo = raw.convert("RGBA")
            logo = ImageOps.fit(
                logo,
                (size - 12, size - 12),
                method=getattr(Image, "Resampling", Image).LANCZOS,
                centering=(0.5, 0.5),
            )
            mask = Image.new("L", logo.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, logo.size[0] - 1, logo.size[1] - 1), fill=255)
            clipped_logo = Image.new("RGBA", logo.size, (0, 0, 0, 0))
            clipped_logo.paste(logo, (0, 0), mask)
            badge.alpha_composite(clipped_logo, (6, 6))
        else:
            font = self._font(54, bold=True)
            draw.text((size * 0.28, size * 0.15), "A", font=font, fill=(232, 43, 45, 255))

        return badge

    def _season_card_label(self, season_info: SeasonInfo) -> str:
        # 赛季代号（如 Marked）来自已下线的第三方站点，现在只有赛季号可用。
        stage = str(getattr(season_info, "current_split_label", "") or "").strip()
        if season_info.season_number is not None:
            return f"S{season_info.season_number} · {stage}" if stage else f"S{season_info.season_number}"
        return stage or "未知赛季"

    @staticmethod
    def _parse_card_datetime(value: str) -> datetime | None:
        try:
            normalized = str(value or "").strip()
            if not normalized:
                return None
            if normalized.endswith("Z"):
                normalized = f"{normalized[:-1]}+00:00"
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _season_progress_fraction(
        season_info: SeasonInfo, now: datetime | None = None
    ) -> float:
        start_dt = Main._parse_card_datetime(getattr(season_info, "start_iso", ""))
        end_dt = Main._parse_card_datetime(getattr(season_info, "end_iso", ""))
        if start_dt is None or end_dt is None:
            return 0.0
        total = (end_dt - start_dt).total_seconds()
        if total <= 0:
            return 0.0
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        elapsed = (current - start_dt).total_seconds()
        return min(1.0, max(0.0, elapsed / total))

    @staticmethod
    def _format_season_end_remaining(
        season_info: SeasonInfo, now: datetime | None = None
    ) -> str:
        """分段剩余时间倒计时（仅图片卡片使用）。"""
        try:
            start_dt = Main._parse_card_datetime(
                getattr(season_info, "start_iso", "")
            )
            end_dt = Main._parse_card_datetime(
                getattr(season_info, "end_iso", "")
            )
            if start_dt is None or end_dt is None or end_dt <= start_dt:
                return ""

            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            else:
                current = current.astimezone(timezone.utc)

            if current < start_dt:
                return ""
            if current >= end_dt:
                return "分段已结束"

            total_seconds = int((end_dt - current).total_seconds())
            if total_seconds < 3600:
                return "距分段结束 不足1小时"
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            return f"距分段结束 {days}天 {hours}小时"
        except Exception:
            return ""

    @staticmethod
    def _to_beijing_time_with_weekday(iso_value: str) -> str:
        parsed = Main._parse_card_datetime(iso_value)
        if parsed is None:
            return ""
        beijing = parsed.astimezone(SHANGHAI_TZ)
        weekday = "一二三四五六日"[beijing.weekday()]
        return f"{beijing:%Y-%m-%d} 周{weekday} {beijing:%H:%M}"

    def _format_map_rotation_text(
        self, rotation_info: MapRotationInfo, mode: str = "ranked"
    ) -> str:
        if mode == "battle_royale":
            rotation_mode = rotation_info.battle_royale
            current_label = "当前三人赛地图"
            title = "🗺️ Apex 三人赛地图轮换"
            missing_text = "⚠️ 暂未获取到三人赛地图轮换数据，请稍后再试"
        else:
            rotation_mode = rotation_info.ranked
            current_label = "当前排位地图"
            title = "🗺️ Apex 排位地图轮换"
            missing_text = "⚠️ 暂未获取到排位地图轮换数据，请稍后再试"

        current = rotation_mode.current
        next_entry = rotation_mode.next
        if current is None:
            return "\n".join(
                [
                    self._time_line(),
                    missing_text,
                ]
            )

        lines = [
            f"📍 {current_label}：{self._format_map_name(current)}",
            f"🕒 查询时间：{self._now_str()}",
            title,
            "——",
            f"⏰ 本轮时间：{self._format_rotation_range(current)}",
        ]
        if current.remaining_timer:
            lines.append(f"⏳ 剩余时间：{current.remaining_timer}")

        if next_entry is not None:
            lines.extend(
                [
                    "——",
                    f"➡️ 下一张：{self._format_map_name(next_entry)}",
                    f"⏰ 下轮时间：{self._format_rotation_range(next_entry)}",
                ]
            )

        lines.append("ℹ️ 时间均为北京时间")
        return "\n".join(lines)

    def _format_daily_map_schedule_text(self, schedule: DailyMapScheduleInfo) -> str:
        lines = [
            self._time_line(),
            f"🗺️ {schedule.title}",
            f"📅 日期：{schedule.date_label}（北京时间）",
            "——",
        ]
        for entry in schedule.entries:
            lines.append(
                f"{self._schedule_entry_status(entry)} {self._schedule_entry_source_label(schedule, entry)} "
                f"{self._map_card_name(entry)}："
                f"{self._format_schedule_entry_range(entry)}"
            )
        lines.extend(
            [
                "——",
                f"ℹ️ {schedule.source_note}",
                "ℹ️ /map 当前地图仍使用 API 实时数据",
            ]
        )
        return "\n".join(lines)

    def _render_daily_map_schedule_image(self, schedule: DailyMapScheduleInfo) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成全天地图图片")

        if not schedule.entries:
            raise ValueError("缺少全天地图排期数据")

        output_dir = self._map_card_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        render_minute = int(time.time() // 60)
        cache_key = self._daily_map_schedule_image_cache_key(
            output_dir, schedule, render_minute=render_minute
        )
        cached_path = self._get_cached_map_rotation_image(cache_key)
        if cached_path is not None:
            return cached_path

        image = self._build_daily_map_schedule_card(schedule)
        output_path = output_dir / f"daily_map_{schedule.mode}_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._set_cached_map_rotation_image(cache_key, output_path)
        self._cleanup_old_daily_map_cards(output_dir)
        return output_path

    def _daily_map_schedule_image_cache_key(
        self,
        output_dir: Path,
        schedule: DailyMapScheduleInfo,
        render_minute: int | None = None,
    ) -> tuple:
        if render_minute is None:
            render_minute = int(time.time() // 60)
        return (
            "daily_map",
            "v2",
            str(output_dir.resolve()),
            schedule.mode,
            schedule.date_label,
            int(render_minute),
            tuple(
                (
                    entry.map_name,
                    entry.start_timestamp,
                    entry.end_timestamp,
                    getattr(entry, "source", ""),
                )
                for entry in schedule.entries
            ),
        )

    def _cleanup_old_daily_map_cards(self, output_dir: Path, keep: int = 8) -> None:
        try:
            files = sorted(
                output_dir.glob("daily_map_*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for file in files[keep:]:
                file.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"清理全天地图缓存图片失败: {exc}")

    def _build_daily_map_schedule_card(self, schedule: DailyMapScheduleInfo):
        width = 900
        entries = list(schedule.entries)
        row_gap = 10
        row_height = 88 if len(entries) <= 8 else 72
        header_height = 154
        footer_height = 64
        height = header_height + len(entries) * (row_height + row_gap) + footer_height + 18

        canvas = Image.new("RGBA", (width, height), (7, 8, 11, 255))
        draw = ImageDraw.Draw(canvas)
        hero_entry = self._current_schedule_entry(entries) or entries[0]
        hero_bg = self._map_background(hero_entry, (width, header_height), focus_y=0.42)
        canvas.alpha_composite(hero_bg, (0, 0))
        self._overlay_rect(canvas, (0, 0, width, header_height), (0, 0, 0, 126))
        self._draw_horizontal_gradient(canvas, 0, header_height, 210, left=True)

        title_font = self._font(42, bold=True)
        meta_font = self._font(22)
        badge_font = self._font(20, bold=True)
        self._draw_text_with_shadow(
            draw,
            (34, 26),
            schedule.title,
            title_font,
            fill=(255, 255, 255, 255),
        )
        self._draw_text_with_shadow(
            draw,
            (36, 82),
            f"{schedule.date_label}  北京时间",
            meta_font,
            fill=(226, 232, 240, 238),
        )
        badge = (690, 28, 852, 66)
        draw.rounded_rectangle(
            badge,
            radius=8,
            fill=(232, 43, 45, 235),
            outline=(255, 118, 92, 180),
            width=1,
        )
        self._draw_centered_stroked_text(
            draw,
            "排位全天",
            badge_font,
            badge[0],
            badge[2],
            badge[1],
            badge[3],
            (255, 255, 255, 255),
        )
        draw.rectangle((34, 124, 260, 130), fill=(232, 43, 45, 230))

        now = datetime.now(SHANGHAI_TZ)
        y = header_height + 14
        for index, entry in enumerate(entries):
            self._draw_daily_map_row(
                canvas=canvas,
                draw=draw,
                entry=entry,
                index=index,
                x=28,
                y=y,
                width=width - 56,
                height=row_height,
                now=now,
                source_label=self._schedule_entry_source_label(schedule, entry),
            )
            y += row_height + row_gap

        footer_y = height - footer_height
        draw.rectangle((0, footer_y, width, height), fill=(14, 16, 22, 236))
        footer_font = self._font(17)
        note = f"{schedule.source_note} · 生成 {schedule.generated_at} · /map 使用 API 实时数据"
        note_font = self._fit_font(draw, note, 17, 13, width - 64)
        draw.text((32, footer_y + 20), note, font=note_font, fill=(224, 232, 242, 238))
        return canvas.convert("RGB")

    def _draw_daily_map_row(
        self,
        canvas,
        draw,
        entry: MapScheduleEntry,
        index: int,
        x: int,
        y: int,
        width: int,
        height: int,
        now: datetime,
        source_label: str,
    ) -> None:
        bg = self._map_background(entry, (width, height), focus_y=0.48)
        canvas.alpha_composite(bg, (x, y))
        active = self._schedule_entry_is_current(entry, now)
        overlay_alpha = 88 if active else 138
        self._overlay_rect(canvas, (x, y, x + width, y + height), (0, 0, 0, overlay_alpha))
        outline = (232, 43, 45, 230) if active else (255, 255, 255, 42)
        draw.rounded_rectangle((x, y, x + width, y + height), radius=8, outline=outline, width=2)

        line_x = x + 34
        if index > 0:
            draw.line((line_x, y - 10, line_x, y + 22), fill=(232, 43, 45, 160), width=3)
        if index < 99:
            draw.line((line_x, y + height - 22, line_x, y + height + 10), fill=(232, 43, 45, 120), width=3)
        dot_fill = (232, 43, 45, 255) if active else (238, 242, 246, 230)
        draw.ellipse((line_x - 8, y + height // 2 - 8, line_x + 8, y + height // 2 + 8), fill=dot_fill)

        status = self._schedule_entry_status(entry, now)
        status_box = (x + 58, y + 18, x + 136, y + 48)
        status_fill = (232, 43, 45, 230) if status == "当前" else (15, 18, 24, 205)
        draw.rounded_rectangle(status_box, radius=6, fill=status_fill, outline=(255, 255, 255, 50), width=1)
        self._draw_centered_stroked_text(
            draw,
            status,
            self._font(17, bold=True),
            status_box[0],
            status_box[2],
            status_box[1],
            status_box[3],
            (255, 255, 255, 245),
        )

        source = source_label
        source_box = (x + 58, y + 50, x + 136, y + 70)
        source_fill = (
            (49, 191, 139, 225)
            if source == "API"
            else (58, 104, 173, 218)
            if source == "网页"
            else (84, 94, 112, 210)
        )
        draw.rounded_rectangle(source_box, radius=5, fill=source_fill)
        self._draw_centered_stroked_text(
            draw,
            source,
            self._font(13, bold=True),
            source_box[0],
            source_box[2],
            source_box[1],
            source_box[3],
            (255, 255, 255, 240),
        )

        name = self._map_card_name(entry)
        name_font = self._fit_font(draw, name, 34, 24, max_width=360, bold=True)
        self._draw_text_with_shadow(
            draw,
            (x + 156, y + 15),
            name,
            name_font,
            fill=(255, 255, 255, 255),
        )
        if entry.map_name and entry.map_name != name:
            draw.text(
                (x + 158, y + 52),
                entry.map_name,
                font=self._font(16),
                fill=(218, 226, 235, 215),
            )

        time_text = self._format_schedule_entry_range(entry)
        time_font = self._fit_font(draw, time_text, 24, 17, max_width=270, bold=True)
        time_box = draw.textbbox((0, 0), time_text, font=time_font)
        time_w = time_box[2] - time_box[0]
        self._draw_text_with_shadow(
            draw,
            (x + width - 28 - time_w, y + 21),
            time_text,
            time_font,
            fill=(255, 255, 255, 255),
        )
        detail_text = (
            format_schedule_remaining(entry, now)
            if active
            else f"{max(1, entry.duration_secs // 60)} 分钟"
        )
        detail_font = self._fit_font(
            draw,
            detail_text,
            20 if active else 17,
            15 if active else 14,
            max_width=270,
            bold=active,
        )
        detail_box = draw.textbbox((0, 0), detail_text, font=detail_font)
        detail_w = detail_box[2] - detail_box[0]
        detail_x = x + width - 28 - detail_w
        detail_y = y + 52
        detail_fill = (118, 236, 188, 250) if active else (218, 226, 235, 215)
        if active:
            self._draw_text_with_shadow(
                draw,
                (detail_x, detail_y),
                detail_text,
                detail_font,
                fill=detail_fill,
            )
        else:
            draw.text(
                (detail_x, detail_y),
                detail_text,
                font=detail_font,
                fill=detail_fill,
            )

        if active:
            self._draw_schedule_progress(
                draw,
                entry,
                (x + 156, y + height - 13, x + width - 28, y + height - 7),
                now,
            )

    def _draw_schedule_progress(
        self,
        draw,
        entry: MapScheduleEntry,
        box: tuple[int, int, int, int],
        now: datetime,
    ) -> None:
        start = self._timestamp_to_beijing_datetime(entry.start_timestamp)
        end = self._timestamp_to_beijing_datetime(entry.end_timestamp)
        if not start or not end:
            return
        total = max(1, (end - start).total_seconds())
        elapsed = min(total, max(0, (now - start).total_seconds()))
        fraction = elapsed / total
        draw.rounded_rectangle(box, radius=3, fill=(255, 255, 255, 48))
        fill_box = (box[0], box[1], box[0] + int((box[2] - box[0]) * fraction), box[3])
        draw.rounded_rectangle(fill_box, radius=3, fill=(49, 191, 139, 235))

    def _current_schedule_entry(
        self, entries: list[MapScheduleEntry]
    ) -> MapScheduleEntry | None:
        now = datetime.now(SHANGHAI_TZ)
        for entry in entries:
            if self._schedule_entry_is_current(entry, now):
                return entry
        return None

    def _schedule_entry_status(
        self, entry: MapScheduleEntry, now: datetime | None = None
    ) -> str:
        current = now or datetime.now(SHANGHAI_TZ)
        start = self._timestamp_to_beijing_datetime(entry.start_timestamp)
        end = self._timestamp_to_beijing_datetime(entry.end_timestamp)
        if start and end and start <= current < end:
            return "当前"
        if end and end <= current:
            return "已过"
        return "待轮换"

    def _schedule_entry_source_label(
        self, schedule: DailyMapScheduleInfo, entry: MapScheduleEntry
    ) -> str:
        source = str(getattr(entry, "source", "") or "").lower()
        if source == "api":
            return "API"
        if source == "web" and "API" not in schedule.source_note:
            return "网页"
        if source in {"web", "inferred"}:
            return "推断"
        for item in schedule.entries:
            if (
                item.map_name == entry.map_name
                and item.start_timestamp == entry.start_timestamp
                and item.end_timestamp == entry.end_timestamp
            ):
                item_source = str(getattr(item, "source", "") or "").lower()
                if item_source == "api":
                    return "API"
                if item_source == "web" and "API" not in schedule.source_note:
                    return "网页"
        return "推断"

    def _schedule_entry_is_current(
        self, entry: MapScheduleEntry, now: datetime | None = None
    ) -> bool:
        return self._schedule_entry_status(entry, now) == "当前"

    def _format_schedule_entry_range(self, entry: MapScheduleEntry) -> str:
        start = self._timestamp_to_beijing_datetime(entry.start_timestamp)
        end = self._timestamp_to_beijing_datetime(entry.end_timestamp)
        if start and end:
            if start.date() == end.date():
                return f"{start:%H:%M} - {end:%H:%M}"
            return f"{start:%m-%d %H:%M} - {end:%m-%d %H:%M}"
        return f"{entry.readable_start} - {entry.readable_end}"

    def _render_map_rotation_image(
        self, rotation_info: MapRotationInfo, mode: str = "ranked"
    ) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成地图轮换图片")

        if mode == "battle_royale":
            rotation_mode = rotation_info.battle_royale
            current_label = "当前三人赛地图"
            next_label = "下一张三人赛地图"
        else:
            rotation_mode = rotation_info.ranked
            current_label = "当前排位地图"
            next_label = "下一张排位地图"

        current = rotation_mode.current
        if current is None:
            raise ValueError("缺少当前地图轮换数据")

        output_dir = self._map_card_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        cache_key = self._map_rotation_image_cache_key(
            mode=mode,
            output_dir=output_dir,
            current=current,
            next_entry=rotation_mode.next,
        )
        cached_path = self._get_cached_map_rotation_image(cache_key)
        if cached_path is not None:
            return cached_path

        image = self._build_map_rotation_card(
            current=current,
            next_entry=rotation_mode.next,
            current_label=current_label,
            next_label=next_label,
        )
        output_path = output_dir / f"map_rotation_{mode}_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._set_cached_map_rotation_image(cache_key, output_path)
        self._cleanup_old_map_cards(output_dir)
        return output_path

    def _get_cached_map_rotation_image(self, cache_key: tuple) -> Path | None:
        cache = getattr(self, "_map_rotation_image_cache", None)
        if not isinstance(cache, dict):
            return None
        cached = cache.get(cache_key)
        if not cached:
            return None
        saved_at, image_path = cached
        if time.monotonic() - saved_at > self._MAP_IMAGE_CACHE_TTL_SECONDS:
            cache.pop(cache_key, None)
            return None
        image_path = Path(image_path)
        if not image_path.exists():
            cache.pop(cache_key, None)
            return None
        return image_path

    def _set_cached_map_rotation_image(self, cache_key: tuple, image_path: Path) -> None:
        self._set_bounded_image_cache_entry(
            cache_attr="_map_rotation_image_cache",
            cache_key=cache_key,
            image_path=image_path,
            ttl_seconds=self._MAP_IMAGE_CACHE_TTL_SECONDS,
            max_entries=self._MAP_IMAGE_CACHE_MAX_ENTRIES,
        )

    def _map_rotation_image_cache_key(
        self,
        mode: str,
        output_dir: Path,
        current: MapRotationEntry,
        next_entry: MapRotationEntry | None,
    ) -> tuple:
        next_key = None
        if next_entry is not None:
            next_key = (
                next_entry.map_name,
                next_entry.start_timestamp,
                next_entry.end_timestamp,
            )
        return (
            mode,
            str(output_dir.resolve()),
            current.map_name,
            current.start_timestamp,
            current.end_timestamp,
            next_key,
        )

    def _map_card_output_dir(self) -> Path:
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            return self._PLUGIN_ROOT / "_generated" / "map_cards"
        return Path(data_dir) / "map_cards"

    def _cleanup_old_map_cards(self, output_dir: Path, keep: int = 12) -> None:
        try:
            files = sorted(
                output_dir.glob("map_rotation_*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for file in files[keep:]:
                file.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"清理地图轮换缓存图片失败: {exc}")

    def _build_map_rotation_card(
        self,
        current: MapRotationEntry,
        next_entry: MapRotationEntry | None,
        current_label: str,
        next_label: str,
    ):
        width, height = self._MAP_CARD_SIZE
        current_height = self._MAP_CURRENT_HEIGHT
        next_height = height - current_height

        canvas = Image.new("RGBA", (width, height), (8, 10, 14, 255))
        current_bg = self._map_background(current, (width, current_height), focus_y=0.42)
        canvas.alpha_composite(current_bg, (0, 0))

        draw = ImageDraw.Draw(canvas)
        self._overlay_rect(canvas, (0, 0, width, current_height), (0, 0, 0, 80))
        self._draw_horizontal_gradient(canvas, 0, current_height, 190, left=True)

        self._draw_map_card_current_section(
            draw=draw,
            current=current,
            label=current_label,
            width=width,
            height=current_height,
        )

        if next_entry is not None:
            next_bg = self._map_background(next_entry, (width, next_height), focus_y=0.48)
            canvas.alpha_composite(next_bg, (0, current_height))
            self._overlay_rect(
                canvas, (0, current_height, width, height), (0, 0, 0, 94)
            )
            self._draw_horizontal_gradient(canvas, current_height, next_height, 165, left=True)
            self._draw_map_card_next_section(
                draw=draw,
                next_entry=next_entry,
                label=next_label,
                y=current_height,
                width=width,
                height=next_height,
            )
        else:
            draw.rectangle((0, current_height, width, height), fill=(9, 12, 18, 255))
            font = self._font(30, bold=True)
            self._draw_text_with_shadow(
                draw,
                (34, current_height + 34),
                "暂无下一张地图",
                font,
                fill=(238, 242, 246, 255),
            )

        draw.line((0, current_height, width, current_height), fill=(255, 255, 255, 36), width=2)
        return canvas.convert("RGB")

    @staticmethod
    def _overlay_rect(canvas, box: tuple[int, int, int, int], fill: tuple[int, int, int, int]) -> None:
        overlay = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), fill)
        canvas.alpha_composite(overlay, (box[0], box[1]))

    def _draw_map_card_current_section(
        self,
        draw,
        current: MapRotationEntry,
        label: str,
        width: int,
        height: int,
    ) -> None:
        title = self._map_card_name(current)
        time_range = self._format_rotation_range_short(current)
        remaining = self._format_remaining_for_card(current)

        label_font = self._font(27, bold=True)
        title_font = self._fit_font(draw, title, 61, 36, max_width=width - 330, bold=True)
        time_font = self._font(28)

        self._draw_text_with_shadow(
            draw,
            (34, 24),
            label,
            label_font,
            fill=(231, 236, 243, 255),
        )
        self._draw_text_with_shadow(
            draw,
            (34, 62),
            title,
            title_font,
            fill=(255, 255, 255, 255),
        )
        self._draw_text_with_shadow(
            draw,
            (36, 133),
            f"本轮时间：{time_range}",
            time_font,
            fill=(238, 242, 246, 255),
        )
        self._draw_remaining_ring(draw, (width - 150, 36), remaining, current)

    def _draw_map_card_next_section(
        self,
        draw,
        next_entry: MapRotationEntry,
        label: str,
        y: int,
        width: int,
        height: int,
    ) -> None:
        name = self._map_card_name(next_entry)
        time_range = self._format_rotation_range_short(next_entry)
        time_text = f"下轮时间：{time_range}"
        label_font = self._font(24, bold=True)
        name_font = self._fit_font(draw, name, 42, 30, max_width=width - 460, bold=True)
        time_font = self._fit_font(draw, time_text, 25, 18, max_width=width - 420)
        time_box = draw.textbbox((0, 0), time_text, font=time_font)
        time_width = time_box[2] - time_box[0]
        time_x = max(34, width - 34 - time_width)

        self._draw_text_with_shadow(
            draw,
            (34, y + 16),
            label,
            label_font,
            fill=(219, 226, 235, 255),
        )
        self._draw_text_with_shadow(
            draw,
            (34, y + 45),
            name,
            name_font,
            fill=(255, 255, 255, 255),
        )
        self._draw_text_with_shadow(
            draw,
            (time_x, y + 42),
            time_text,
            time_font,
            fill=(238, 242, 246, 255),
        )

    def _draw_remaining_ring(
        self,
        draw,
        top_left: tuple[int, int],
        remaining_text: str,
        entry: MapRotationEntry,
    ) -> None:
        x, y = top_left
        size = 112
        box = (x, y, x + size, y + size)
        draw.ellipse(box, fill=(0, 0, 0, 156), outline=(255, 255, 255, 34), width=2)
        draw.arc(box, start=-90, end=-90 + int(360 * self._remaining_fraction(entry)), fill=(49, 191, 139, 255), width=10)

        text_font = self._fit_font(draw, remaining_text, 28, 18, max_width=size - 22, bold=True)
        label_font = self._font(19)
        text_box = draw.textbbox((0, 0), remaining_text, font=text_font)
        text_width = text_box[2] - text_box[0]
        self._draw_text_with_shadow(
            draw,
            (x + (size - text_width) / 2, y + 36),
            remaining_text,
            text_font,
            fill=(255, 255, 255, 255),
        )
        label = "剩余"
        label_box = draw.textbbox((0, 0), label, font=label_font)
        label_width = label_box[2] - label_box[0]
        draw.text(
            (x + (size - label_width) / 2, y + 69),
            label,
            font=label_font,
            fill=(220, 226, 233, 230),
        )

    def _draw_horizontal_gradient(
        self,
        canvas,
        y: int,
        height: int,
        alpha: int,
        left: bool = True,
    ) -> None:
        width = canvas.size[0]
        gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pixels = gradient.load()
        for x in range(width):
            ratio = 1 - (x / max(1, width - 1)) if left else x / max(1, width - 1)
            current_alpha = int(alpha * (ratio ** 1.7))
            for row in range(height):
                pixels[x, row] = (0, 0, 0, current_alpha)
        canvas.alpha_composite(gradient, (0, y))

    def _map_background(
        self,
        entry: MapRotationEntry,
        size: tuple[int, int],
        focus_y: float,
    ):
        asset_path = self._resolve_map_asset_path(entry.map_name)
        if asset_path.exists():
            with Image.open(asset_path) as raw:
                source = raw.convert("RGBA")
        else:
            source = self._placeholder_map_background(size, entry.map_name)

        resampling = getattr(Image, "Resampling", Image).LANCZOS
        background = ImageOps.fit(
            source,
            size,
            method=resampling,
            centering=(0.5, focus_y),
        )
        return background.filter(ImageFilter.GaussianBlur(radius=0.4))

    def _placeholder_map_background(self, size: tuple[int, int], map_name: str):
        width, height = size
        image = Image.new("RGBA", size, (23, 28, 36, 255))
        draw = ImageDraw.Draw(image)
        for row in range(height):
            shade = int(28 + 36 * row / max(1, height - 1))
            draw.line((0, row, width, row), fill=(shade, shade + 8, shade + 18, 255))
        font = self._fit_font(draw, map_name or "未知地图", 42, 24, width - 80, bold=True)
        self._draw_text_with_shadow(
            draw,
            (38, height // 2 - 24),
            map_name or "未知地图",
            font,
            fill=(230, 236, 244, 230),
        )
        return image

    def _resolve_map_asset_path(self, map_name: str) -> Path:
        normalized = str(map_name or "").strip()
        aliases = {
            "Broken Moon": "Broken_Moon.png",
            "E-District": "E-District.png",
            "Kings Canyon": "Kings_Canyon.png",
            "Olympus": "Olympus.png",
            "Storm Point": "Storm_Point.png",
            "World's Edge": "Worlds_Edge.png",
            "Worlds Edge": "Worlds_Edge.png",
        }
        candidates: list[str] = []
        if normalized in aliases:
            candidates.append(aliases[normalized])
        if normalized:
            slug = normalized.replace("'", "").replace("’", "").replace(" ", "_")
            slug = "".join(ch for ch in slug if ch.isalnum() or ch in {"_", "-"})
            if slug:
                candidates.append(f"{slug}.png")
        candidates.append("unknown.png")

        for candidate in candidates:
            path = self._MAP_ASSET_DIR / candidate
            if path.exists():
                return path
        return self._MAP_ASSET_DIR / candidates[0]

    def _map_card_name(self, entry: MapRotationEntry) -> str:
        name = (entry.map_name_zh or entry.map_name or "未知地图").strip()
        return name or "未知地图"

    def _format_rotation_range_short(self, entry: MapRotationEntry) -> str:
        start = self._timestamp_to_beijing_datetime(entry.start_timestamp)
        end = self._timestamp_to_beijing_datetime(entry.end_timestamp)
        if start and end:
            if start.date() == end.date():
                return f"{start:%H:%M} - {end:%H:%M}"
            return f"{start:%m-%d %H:%M} - {end:%m-%d %H:%M}"
        start_text = entry.readable_start or "未知"
        end_text = entry.readable_end or "未知"
        return f"{start_text} - {end_text}"

    def _format_remaining_for_card(self, entry: MapRotationEntry) -> str:
        seconds = entry.remaining_secs
        if seconds is None:
            seconds = self._remaining_seconds_from_timer(entry.remaining_timer)
        if seconds is None and entry.end_timestamp:
            seconds = max(0, int(entry.end_timestamp - datetime.now(timezone.utc).timestamp()))
        if seconds is None:
            return entry.remaining_timer or "未知"

        minutes = max(0, int(seconds) // 60)
        days = minutes // 1440
        hours = (minutes % 1440) // 60
        mins = minutes % 60
        if days:
            return f"{days}天{hours}时"
        if hours:
            return f"{hours}时{mins}分"
        return f"{mins}分"

    @staticmethod
    def _remaining_seconds_from_timer(timer: str) -> int | None:
        parts = str(timer or "").strip().split(":")
        if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
            return None
        numbers = [int(part) for part in parts]
        if len(numbers) == 2:
            minutes, seconds = numbers
            return minutes * 60 + seconds
        hours, minutes, seconds = numbers
        return hours * 3600 + minutes * 60 + seconds

    def _remaining_fraction(self, entry: MapRotationEntry) -> float:
        if entry.remaining_secs is not None and entry.duration_secs:
            return min(1.0, max(0.0, entry.remaining_secs / entry.duration_secs))
        seconds = self._remaining_seconds_from_timer(entry.remaining_timer)
        if seconds is not None and entry.duration_secs:
            return min(1.0, max(0.0, seconds / entry.duration_secs))
        if entry.start_timestamp and entry.end_timestamp:
            total = entry.end_timestamp - entry.start_timestamp
            if total > 0:
                remaining = entry.end_timestamp - datetime.now(timezone.utc).timestamp()
                return min(1.0, max(0.0, remaining / total))
        return 0.72

    @staticmethod
    def _timestamp_to_beijing_datetime(timestamp: int):
        if not timestamp:
            return None
        try:
            return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone(
                SHANGHAI_TZ
            )
        except Exception:
            return None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _font_cache_path(self) -> Path:
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            data_dir = self._PLUGIN_ROOT / "_generated"
        return Path(data_dir) / "fonts" / self._FONT_FILE_NAME

    @staticmethod
    def _candidate_system_cjk_font_paths(bold: bool = False) -> list[Path]:
        windows_fonts = [
            Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
            Path(r"C:\Windows\Fonts\simhei.ttf"),
            Path(r"C:\Windows\Fonts\simsun.ttc"),
        ]
        linux_fonts = [
            Path(
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
                if bold
                else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
            ),
            Path(
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"
                if bold
                else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
            ),
            Path(
                "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf"
                if bold
                else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"
            ),
            Path(
                "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Bold.otf"
                if bold
                else "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf"
            ),
            Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf")
            if bold
            else Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"),
            Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
            Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        ]
        mac_fonts = [
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/System/Library/Fonts/STHeiti Light.ttc"),
            Path("/Library/Fonts/Arial Unicode.ttf"),
        ]
        return windows_fonts + linux_fonts + mac_fonts

    def _resolve_system_cjk_font_path(self, bold: bool = False) -> Path | None:
        for path in self._candidate_system_cjk_font_paths(bold=bold):
            try:
                if path.exists():
                    return path
            except Exception:
                continue
        return None

    def _resolve_cached_cjk_font_path(self) -> Path | None:
        font_path = self._font_cache_path()
        if not font_path.exists():
            return None
        try:
            if self._sha256_file(font_path).lower() == self._FONT_SHA256:
                return font_path
            font_path.unlink(missing_ok=True)
            logger.warning("Apex Rank Watch 字体缓存校验失败，已删除损坏文件")
        except Exception as exc:
            logger.warning(f"Apex Rank Watch 字体缓存读取失败：{exc}")
        return None

    def _get_cjk_font_status(self) -> FontStatus:
        system_font = self._resolve_system_cjk_font_path()
        if system_font is not None:
            return FontStatus(True, "system", system_font)
        cached_font = self._resolve_cached_cjk_font_path()
        if cached_font is not None:
            return FontStatus(True, "cache", cached_font)
        return FontStatus(False, "missing", None)

    def _consume_font_download_notice_needed(self) -> bool:
        if getattr(self, "_font_notice_sent", False):
            return False
        status = self._get_cjk_font_status()
        if status.available:
            return False
        self._font_notice_sent = True
        return True

    def _build_font_download_notice_lines(self) -> list[str]:
        config = getattr(self, "_config", None)
        auto_enabled = getattr(config, "font_auto_download", True)
        lines = [
            self._time_line(),
            "⚠️ 未检测到可用中文字体，图片里的中文可能显示为空白或方块。",
        ]
        if auto_enabled:
            lines.append("插件会在首次生成图片时自动下载字体，也可以发送 /apex_download 立即下载。")
        else:
            lines.append("当前已关闭自动字体下载，可以发送 /apex_download 手动下载。")
        lines.extend(
            [
                "字体约 16MB，会保存到 AstrBot 插件数据目录，并通过 SHA256 校验后使用。",
                "如果服务器无法访问 GitHub，可在配置里填写 font_download_url 镜像地址。",
            ]
        )
        return lines

    def _build_font_status_lines(self, status: FontStatus) -> list[str]:
        lines = [self._time_line(), "🧩 Apex Rank Watch 中文字体检测"]
        if status.available:
            source_label = "系统字体" if status.source == "system" else "插件缓存字体"
            lines.append(f"✅ 中文字体已可用，来源：{source_label}")
            if status.path is not None:
                lines.append(f"路径：{status.path}")
            if status.source == "system":
                lines.append("无需额外下载字体。")
            return lines

        lines.extend(
            [
                "❌ 当前未检测到可用中文字体，图片中文可能显示异常。",
                "已尝试下载插件字体缓存，但暂未成功。",
                f"默认下载地址：{self._FONT_DOWNLOAD_URL}",
                "请确认服务器可以访问 GitHub，或在配置中填写 font_download_url 镜像地址后重试 /apex_download。",
            ]
        )
        return lines

    def _download_cjk_font_if_needed(self, force: bool = False) -> Path | None:
        cached_path = self._resolve_cached_cjk_font_path()
        if cached_path is not None:
            return cached_path

        config = getattr(self, "_config", None)
        if not force and not getattr(config, "font_auto_download", True):
            return None
        if not force and getattr(self, "_font_download_attempted", False):
            return None
        self._font_download_attempted = True

        url = str(
            getattr(config, "font_download_url", "") or self._FONT_DOWNLOAD_URL
        ).strip()
        if not url:
            return None

        font_path = self._font_cache_path()
        temp_path = font_path.with_suffix(font_path.suffix + ".tmp")
        try:
            response = httpx.get(
                url,
                timeout=self._FONT_DOWNLOAD_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > self._FONT_MAX_BYTES:
                raise ValueError("字体文件超过允许大小")
            content = response.content
            if len(content) > self._FONT_MAX_BYTES:
                raise ValueError("字体文件超过允许大小")
            if hashlib.sha256(content).hexdigest().lower() != self._FONT_SHA256:
                raise ValueError("字体文件 SHA256 校验失败")

            font_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_bytes(content)
            temp_path.replace(font_path)
            logger.info(f"Apex Rank Watch 已下载中文字体缓存：{font_path}")
            return font_path
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            logger.warning(f"Apex Rank Watch 中文字体下载失败，将使用系统兜底字体：{exc}")
            return None

    def _font(self, size: int, bold: bool = False):
        system_font = self._resolve_system_cjk_font_path(bold=bold)
        if system_font is not None:
            try:
                return ImageFont.truetype(str(system_font), size=size)
            except Exception:
                pass

        downloaded_font = self._download_cjk_font_if_needed()
        if downloaded_font is not None:
            try:
                return ImageFont.truetype(str(downloaded_font), size=size)
            except Exception as exc:
                logger.warning(f"Apex Rank Watch 中文字体加载失败：{exc}")

        fallback_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for font_path in fallback_candidates:
            try:
                path = Path(font_path)
                if path.exists():
                    return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _fit_font(
        self,
        draw,
        text: str,
        size: int,
        min_size: int,
        max_width: int,
        bold: bool = False,
    ):
        current_size = size
        while current_size >= min_size:
            font = self._font(current_size, bold=bold)
            box = draw.textbbox((0, 0), text, font=font)
            if box[2] - box[0] <= max_width:
                return font
            current_size -= 2
        return self._font(min_size, bold=bold)

    @staticmethod
    def _fit_text_to_width(draw, text: str, font, max_width: int) -> str:
        if not text:
            return ""
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return text

        ellipsis = "..."
        ellipsis_width = draw.textbbox((0, 0), ellipsis, font=font)[2]
        if ellipsis_width >= max_width:
            return ellipsis

        trimmed = text
        while trimmed:
            trimmed = trimmed[:-1]
            candidate = f"{trimmed}{ellipsis}"
            box = draw.textbbox((0, 0), candidate, font=font)
            if box[2] - box[0] <= max_width:
                return candidate
        return ellipsis

    @staticmethod
    def _draw_text_with_shadow(draw, xy, text: str, font, fill) -> None:
        x, y = xy
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 180))
        draw.text((x, y), text, font=font, fill=fill)

    def _render_predator_info_image(
        self, predator_info: PredatorInfo, selected_platform: str = ""
    ) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法生成猎杀线图片")
        if not self._PREDATOR_TEMPLATE_PATH.exists():
            raise FileNotFoundError(f"缺少猎杀线图片模板: {self._PREDATOR_TEMPLATE_PATH}")

        output_dir = self._predator_card_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        cache_key = self._predator_info_image_cache_key(output_dir, predator_info)
        cached_path = self._get_cached_predator_info_image(cache_key)
        if cached_path is not None:
            return cached_path

        image = self._build_predator_info_card(predator_info)
        output_path = output_dir / f"predator_info_{now_epoch_ms()}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._set_cached_predator_info_image(cache_key, output_path)
        self._cleanup_old_predator_cards(output_dir)
        return output_path

    def _get_cached_predator_info_image(self, cache_key: tuple) -> Path | None:
        cache = getattr(self, "_predator_info_image_cache", None)
        if not isinstance(cache, dict):
            return None
        cached = cache.get(cache_key)
        if not cached:
            return None
        saved_at, image_path = cached
        if time.monotonic() - saved_at > self._PREDATOR_IMAGE_CACHE_TTL_SECONDS:
            cache.pop(cache_key, None)
            return None
        image_path = Path(image_path)
        if not image_path.exists():
            cache.pop(cache_key, None)
            return None
        return image_path

    def _set_cached_predator_info_image(
        self, cache_key: tuple, image_path: Path
    ) -> None:
        self._set_bounded_image_cache_entry(
            cache_attr="_predator_info_image_cache",
            cache_key=cache_key,
            image_path=image_path,
            ttl_seconds=self._PREDATOR_IMAGE_CACHE_TTL_SECONDS,
            max_entries=self._PREDATOR_IMAGE_CACHE_MAX_ENTRIES,
        )

    def _predator_info_image_cache_key(
        self, output_dir: Path, predator_info: PredatorInfo, selected_platform: str = ""
    ) -> tuple:
        platform_key = []
        for platform in ("PC", "PS4", "X1", "SWITCH"):
            stats = predator_info.platforms.get(platform)
            if stats is None:
                platform_key.append((platform, None))
                continue
            platform_key.append(
                (
                    platform,
                    stats.threshold_rp,
                    stats.masters_and_preds,
                    stats.update_timestamp,
                )
            )

        try:
            template_mtime = self._PREDATOR_TEMPLATE_PATH.stat().st_mtime_ns
        except OSError:
            template_mtime = 0
        return (str(output_dir.resolve()), template_mtime, tuple(platform_key))

    def _predator_card_output_dir(self) -> Path:
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            return self._PLUGIN_ROOT / "_generated" / "predator_cards"
        return Path(data_dir) / "predator_cards"

    def _cleanup_old_predator_cards(self, output_dir: Path, keep: int = 8) -> None:
        try:
            files = sorted(
                output_dir.glob("predator_info_*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for file in files[keep:]:
                file.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"清理猎杀线缓存图片失败: {exc}")

    def _build_predator_info_card(
        self, predator_info: PredatorInfo, selected_platform: str = ""
    ):
        with Image.open(self._PREDATOR_TEMPLATE_PATH) as raw:
            canvas = raw.convert("RGBA")
        draw = ImageDraw.Draw(canvas)

        # 模板来自旧版固定版式，这里只在预留黑框内写入实时数据。
        query_text = self._now_str()
        time_font = self._fit_font(draw, query_text, 38, 28, 350, bold=True)
        update_text = self._format_predator_update_time(
            self._select_predator_platforms(predator_info, "")
        ) or "暂无"
        update_font = self._fit_font(draw, update_text, 38, 28, 350, bold=True)
        value_fill = self._PREDATOR_NEUTRAL

        self._draw_centered_predator_text(
            draw, (636, 244, 1053, 318), query_text, time_font, value_fill
        )
        self._draw_centered_predator_text(
            draw, (636, 354, 1053, 426), update_text, update_font, value_fill
        )

        rows = {
            "PC": ((290, 535, 715, 648), (790, 536, 1078, 615)),
            "PS4": ((290, 756, 715, 869), (790, 756, 1078, 835)),
            "X1": ((290, 976, 715, 1089), (790, 976, 1078, 1055)),
            "SWITCH": ((290, 1197, 715, 1311), (790, 1197, 1078, 1276)),
        }
        for platform, (threshold_box, count_box) in rows.items():
            stats = predator_info.platforms.get(platform)
            threshold = self._predator_threshold_text(stats)
            count = self._predator_master_count_text(stats)
            threshold_font = self._fit_font(
                draw,
                threshold,
                58,
                36,
                threshold_box[2] - threshold_box[0] - 36,
                bold=True,
            )
            count_font = self._fit_font(
                draw,
                count,
                60,
                34,
                count_box[2] - count_box[0] - 42,
                bold=True,
            )
            self._draw_centered_predator_text(
                draw,
                threshold_box,
                threshold,
                threshold_font,
                self._predator_threshold_fill(stats),
            )
            self._draw_centered_predator_text(
                draw,
                count_box,
                count,
                count_font,
                self._predator_master_count_fill(stats),
            )

        return canvas.convert("RGB")

    def _draw_centered_predator_text(
        self,
        draw,
        box: tuple[int, int, int, int],
        text: str,
        font,
        fill: tuple[int, int, int, int],
    ) -> None:
        left, top, right, bottom = box
        x = self._centered_text_x(draw, text, font, left, right)
        y = self._centered_text_y(draw, text, font, top, bottom)
        draw.text(
            (x, y),
            text,
            font=font,
            fill=fill,
            stroke_width=2,
            stroke_fill=(0, 0, 0, 185),
        )

    def _predator_threshold_text(self, stats: PredatorPlatformStats | None) -> str:
        if stats is None:
            return "暂无"
        return f"{self._format_number(stats.threshold_rp)} RP"

    def _predator_master_count_text(self, stats: PredatorPlatformStats | None) -> str:
        if stats is None:
            return "暂无"
        return self._format_number(stats.masters_and_preds)

    def _predator_threshold_fill(
        self, stats: PredatorPlatformStats | None
    ) -> tuple[int, int, int, int]:
        if stats is None:
            return self._PREDATOR_NEUTRAL
        if stats.threshold_rp <= 16000:
            return self._PREDATOR_GREEN
        return self._PREDATOR_DEEP_RED

    def _predator_master_count_fill(
        self, stats: PredatorPlatformStats | None
    ) -> tuple[int, int, int, int]:
        if stats is None:
            return self._PREDATOR_NEUTRAL
        if stats.masters_and_preds < 750:
            return self._PREDATOR_GREEN
        return self._PREDATOR_DEEP_RED

    def _format_predator_info_text(
        self, predator_info: PredatorInfo, selected_platform: str = ""
    ) -> str:
        platform_stats = self._select_predator_platforms(predator_info, selected_platform)
        if not platform_stats:
            return "\n".join(
                [
                    self._time_line(),
                    "⚠️ 暂未获取到本赛季猎杀线数据，请稍后再试",
                ]
            )

        updated_at = self._format_predator_update_time(platform_stats)
        lines = [
            "🏆 本赛季猎杀线",
            f"🕒 查询时间：{self._now_str()}",
        ]
        if updated_at:
            lines.append(f"🔄 数据更新：{updated_at}")
        lines.append("——")

        for stats in platform_stats:
            lines.append(self._format_predator_platform_line(stats))

        lines.append("ℹ️ 大师数量来自 API 的大师+猎杀总数")
        return "\n".join(lines)

    def _select_predator_platforms(
        self, predator_info: PredatorInfo, selected_platform: str = ""
    ) -> list[PredatorPlatformStats]:
        if selected_platform:
            stats = predator_info.platforms.get(selected_platform)
            return [stats] if stats else []

        ordered = []
        for platform in ("PC", "PS4", "X1", "SWITCH"):
            stats = predator_info.platforms.get(platform)
            if stats is not None:
                ordered.append(stats)
        return ordered

    def _format_predator_platform_line(self, stats: PredatorPlatformStats) -> str:
        platform = self._format_platform_with_icon(stats.platform)
        threshold = self._format_number(stats.threshold_rp)
        count = self._format_number(stats.masters_and_preds)
        return f"{platform}：猎杀线 {threshold} RP｜大师数量 {count}（包含猎杀）"

    def _format_predator_update_time(
        self, platform_stats: list[PredatorPlatformStats]
    ) -> str:
        timestamps = [item.update_timestamp for item in platform_stats if item.update_timestamp]
        if not timestamps:
            return ""
        return self._format_timestamp_to_beijing(max(timestamps))

    def _format_map_name(self, entry: MapRotationEntry) -> str:
        if entry.map_name_zh and entry.map_name_zh != entry.map_name:
            return f"{entry.map_name_zh} / {entry.map_name}"
        return entry.map_name_zh or entry.map_name or "未知"

    def _format_rotation_range(self, entry: MapRotationEntry) -> str:
        start_text = self._format_timestamp_to_beijing(entry.start_timestamp)
        end_text = self._format_timestamp_to_beijing(entry.end_timestamp)
        if not start_text:
            start_text = entry.readable_start or "未知"
        if not end_text:
            end_text = entry.readable_end or "未知"
        return f"{start_text} ~ {end_text}"

    def _format_timestamp_to_beijing(self, timestamp: int) -> str:
        if not timestamp:
            return ""
        try:
            dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            bj = dt.astimezone(SHANGHAI_TZ)
            return bj.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""

    def _format_platform_with_icon(self, platform: str) -> str:
        icons = {
            "PC": "🖥️ PC",
            "PS4": "🎮 PlayStation",
            "X1": "🎮 Xbox",
            "SWITCH": "🎮 Switch",
        }
        return icons.get(platform, self._format_platform(platform))

    @staticmethod
    def _format_number(value: int) -> str:
        try:
            return f"{int(value):,}"
        except Exception:
            return str(value)

    def _format_season_info(self, season_info: SeasonInfo) -> str:
        # 只展示“当前分段”：ALS 是唯一数据源，它不提供整赛季起止。
        stage = str(getattr(season_info, "current_split_label", "") or "").strip()
        season_label = "未知"
        if season_info.season_number is not None:
            season_label = f"S{season_info.season_number}"
            if stage:
                season_label = f"{season_label} · {stage}"
        elif stage:
            season_label = stage

        lines = [
            self._time_line(),
            "🗓️ Apex 排位分段时间",
            f"📌 当前分段: {season_label}",
        ]
        if season_info.status_text and season_info.status_text != "未知":
            lines.append(f"📍 分段状态: {season_info.status_text}")

        start_bj = self._to_beijing_time(season_info.start_iso)
        end_bj = self._to_beijing_time(season_info.end_iso)
        if start_bj:
            lines.append(f"🟢 分段开始(北京时间): {start_bj}")
        if end_bj:
            lines.append(f"🔴 分段结束(北京时间): {end_bj}")
        if not start_bj and season_info.start_date and season_info.start_date != "未知":
            lines.append(f"🟢 分段开始: {season_info.start_date}")
        if not end_bj and season_info.end_date and season_info.end_date != "未知":
            lines.append(f"🔴 分段结束: {season_info.end_date}")

        remaining = self._format_remaining(season_info.end_iso)
        if remaining:
            lines.append(f"⏳ 剩余时间: {remaining}")

        progress = self._format_progress(season_info.start_iso, season_info.end_iso)
        if progress:
            lines.append(f"📈 分段进度: {progress}")

        if season_info.split_note:
            lines.append(f"🧩 {season_info.split_note}")

        lines.append(f"ℹ️ 数据来源: {season_info.source}")
        lines.append("ℹ️ 排位重置时间为北京时间周三 01:00（00:30 关闭排位）")
        return "\n".join(lines)

    @staticmethod
    def _to_beijing_time(iso_value: str) -> str:
        if not iso_value:
            return ""
        try:
            value = iso_value
            if value.endswith("Z"):
                value = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            bj = dt.astimezone(SHANGHAI_TZ)
            return bj.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""

    @staticmethod
    def _format_remaining(end_iso: str) -> str:
        if not end_iso:
            return ""
        try:
            value = end_iso
            if value.endswith("Z"):
                value = value.replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(value)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = end_dt - now
            total_seconds = int(delta.total_seconds())
            if total_seconds <= 0:
                return "已结束"
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            minutes = (total_seconds % 3600) // 60
            return f"{days} 天 {hours} 小时 {minutes} 分钟"
        except Exception:
            return ""

    @staticmethod
    def _format_progress(start_iso: str, end_iso: str) -> str:
        if not start_iso or not end_iso:
            return ""
        try:
            start_value = start_iso.replace("Z", "+00:00") if start_iso.endswith("Z") else start_iso
            end_value = end_iso.replace("Z", "+00:00") if end_iso.endswith("Z") else end_iso
            start_dt = datetime.fromisoformat(start_value)
            end_dt = datetime.fromisoformat(end_value)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            total_seconds = (end_dt - start_dt).total_seconds()
            if total_seconds <= 0:
                return ""
            now = datetime.now(timezone.utc)
            elapsed_seconds = (now - start_dt).total_seconds()
            if elapsed_seconds < 0:
                elapsed_seconds = 0
            if elapsed_seconds > total_seconds:
                elapsed_seconds = total_seconds
            percent = elapsed_seconds / total_seconds * 100
            elapsed_days = int(elapsed_seconds // 86400)
            elapsed_hours = int((elapsed_seconds % 86400) // 3600)
            elapsed_minutes = int((elapsed_seconds % 3600) // 60)
            total_days = int(total_seconds // 86400)
            total_hours = int((total_seconds % 86400) // 3600)
            total_minutes = int((total_seconds % 3600) // 60)
            elapsed_text = f"{elapsed_days} 天 {elapsed_hours} 小时 {elapsed_minutes} 分钟"
            total_text = f"{total_days} 天 {total_hours} 小时 {total_minutes} 分钟"
            return f"{percent:.2f}%（已过 {elapsed_text} / 共 {total_text}）"
        except Exception:
            return ""
