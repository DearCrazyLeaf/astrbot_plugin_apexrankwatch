from __future__ import annotations

import asyncio
import base64
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from astrbot.api.web import error_response, json_response, request

from ..storage import PlayerRecord
from ..utils import now_epoch_ms

_PLUGIN_ID = "astrbot_plugin_apexrankwatch"
_SECRET_KEYS = {"api_key", "font_download_url"}
_UMO_RE = re.compile(r"^[^:\s]+:[^:\s]+:.+$")


class ApexRankWatchWebUI:
    """Dashboard API for Apex Rank Watch."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        self._mutation_lock = asyncio.Lock()

    def register(self) -> None:
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/dashboard",
            self.dashboard,
            ["GET"],
            "Apex Rank Watch dashboard",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/settings",
            self.settings,
            ["GET"],
            "Apex Rank Watch settings",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/settings",
            self.save_settings,
            ["POST"],
            "Save Apex Rank Watch settings",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/bindings",
            self.bindings,
            ["GET"],
            "Apex Rank Watch group bindings",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/bindings",
            self.save_binding,
            ["POST"],
            "Save Apex Rank Watch group binding",
        )
        for path, handler, methods, description in (
            (
                "/subscriptions",
                self.save_subscription,
                ["POST"],
                "Manage UMO player subscriptions",
            ),
            ("/push-archive", self.archive_days, ["GET"], "Daily UMO push log"),
            (
                "/push-archive/<archive_date>",
                self.archive_day,
                ["GET"],
                "UMO push log for date",
            ),
            (
                "/push-archive/<archive_date>/<entry_id>/preview",
                self.archive_preview,
                ["GET"],
                "Archived push card",
            ),
        ):
            self.plugin.context.register_web_api(
                f"/{_PLUGIN_ID}{path}", handler, methods, description
            )

    def _schema(self) -> dict[str, Any]:
        data = json.loads(self.schema_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("插件配置 schema 必须是对象")
        return data

    def _raw_config(self) -> dict[str, Any]:
        return self.plugin.config if isinstance(self.plugin.config, dict) else {}

    @staticmethod
    def _umo_parts(umo: str) -> tuple[str, str, str] | None:
        if not _UMO_RE.fullmatch(umo):
            return None
        platform, message_type, target = umo.split(":", 2)
        return platform, message_type, target

    def _configured_sessions(self) -> list[dict[str, Any]]:
        group = self._raw_config().get("session_settings", {})
        sessions = group.get("sessions", []) if isinstance(group, dict) else []
        return [
            {**deepcopy(item), "__template_key": "session"}
            for item in sessions
            if isinstance(item, dict)
        ]

    def _group_sessions(self) -> list[dict[str, Any]]:
        sessions = []
        configured = self._configured_sessions()
        seen = {str(item.get("umo") or "") for item in configured}
        for group_id, group in self.plugin._store.iter_groups():
            if group.origin in seen:
                continue
            seen.add(group.origin)
            sessions.append(
                self._serialize_session(
                    {
                        "label": f"群 {group_id}",
                        "umo": group.origin,
                        "group_id": str(group_id),
                        "enabled": True,
                    },
                    group,
                )
            )
        for umo in getattr(self.plugin, "_runtime_session_subscriptions", {}):
            parts = self._umo_parts(umo)
            if umo in seen or parts is None:
                continue
            seen.add(umo)
            sessions.append(
                self._serialize_session(
                    {"umo": umo, "group_id": parts[2], "enabled": True}
                )
            )
        return sessions

    def _serialize_session(
        self, entry: dict[str, Any], group: Any = None
    ) -> dict[str, Any]:
        umo = str(entry.get("umo") or "").strip()
        parts = self._umo_parts(umo)
        group_id = str(entry.get("group_id") or (parts[2] if parts else ""))
        if group is None and group_id:
            group = self.plugin._store.get_group(group_id)
        all_subscriptions = getattr(self.plugin, "_runtime_session_subscriptions", {})
        explicit = umo in all_subscriptions
        subscriptions = all_subscriptions.get(umo, {})
        if not explicit and group is not None and umo == group.origin:
            subscriptions = {
                key: self.plugin._watch_mode(player)
                for key, player in group.players.items()
            }
        players = []
        if group is not None:
            for key, player in group.players.items():
                history = list(getattr(player, "history", []) or [])
                last_change = history[-1] if history else None
                rank_name = str(getattr(player, "rank_name", "未知") or "未知")
                rank_div = int(getattr(player, "rank_div", 0) or 0)
                icon_key = self.plugin._normalize_asset_token(rank_name)
                if icon_key in {"菜鸟", "rookie"}:
                    icon_key = "rookie"
                elif icon_key in {"青铜", "bronze"}:
                    icon_key = "bronze"
                elif icon_key in {"白银", "silver"}:
                    icon_key = "silver"
                elif icon_key in {"黄金", "gold"}:
                    icon_key = "gold"
                elif icon_key in {"白金", "铂金", "platinum"}:
                    icon_key = "platinum"
                elif icon_key in {"钻石", "diamond"}:
                    icon_key = "diamond"
                elif icon_key in {"大师", "master"}:
                    icon_key = "master"
                elif "猎杀" in icon_key or icon_key in {"predator", "apexpredator"}:
                    icon_key = "predator"
                icon_file = (
                    f"{icon_key}_{rank_div}.png"
                    if rank_div
                    and icon_key
                    in {"rookie", "bronze", "silver", "gold", "platinum", "diamond"}
                    else f"{icon_key}.png"
                )
                players.append(
                    {
                        "key": str(key),
                        "name": self.plugin._record_display_name(player),
                        "platform": str(getattr(player, "platform", "PC") or "PC"),
                        "rank": f"{rank_name} {rank_div}" if rank_div else rank_name,
                        "score": int(getattr(player, "rank_score", 0) or 0),
                        "watch_mode": subscriptions.get(
                            key,
                            str(getattr(player, "watch_mode", "notify") or "notify"),
                        ),
                        "subscribed": key in subscriptions,
                        "blocked": self.plugin._should_skip_poll_player(player),
                        "delta": int(getattr(last_change, "score_delta", 0) or 0)
                        if last_change
                        else 0,
                        "rank_icon": icon_file,
                    }
                )
        return {
            "__template_key": "session",
            "label": str(entry.get("label") or f"群 {group_id}"),
            "umo": umo,
            "umo_display": umo or "未绑定 UMO",
            "platform": parts[0] if parts else "",
            "message_type": parts[1] if parts else "",
            "group_id": group_id,
            "enabled": bool(entry.get("enabled", True)),
            "season_keyword_enabled": self.plugin._is_season_keyword_disabled(
                umo, group_id
            ) is False,
            "configured": any(
                item.get("umo") == umo for item in self._configured_sessions()
            ),
            "allowed": self.plugin._is_group_allowed(group_id, umo),
            "blocked_reason": ""
            if self.plugin._is_group_allowed(group_id, umo)
            else "本群未在白名单中，命令与自动推送均受限",
            "output_mode": str(entry.get("output_mode") or "inherit"),
            "subscriptions": [
                {"player_key": key, "watch_mode": mode}
                for key, mode in subscriptions.items()
            ],
            "players": [player for player in players if player["subscribed"]],
            "available_players": players,
        }

    @staticmethod
    def _normalize_bindings(raw: Any) -> list[dict[str, str]]:
        if isinstance(raw, dict):
            items = raw.items()
        elif isinstance(raw, list):
            items = (
                (item.get("user_id"), item.get("target"))
                for item in raw
                if isinstance(item, dict)
            )
        else:
            items = ()
        result = []
        for user_id, target in items:
            user_id = str(user_id or "").strip()
            target = str(target or "").strip()
            if user_id and target:
                result.append({"user_id": user_id, "target": target})
        return sorted(result, key=lambda item: item["user_id"])

    def _sessions(self) -> list[dict[str, Any]]:
        return [
            *[self._serialize_session(item) for item in self._configured_sessions()],
            *self._group_sessions(),
        ]

    async def dashboard(self):
        sessions = self._sessions()
        players = [player for session in sessions for player in session["players"]]
        state = deepcopy(getattr(self.plugin, "_webui_poll_state", {}))
        # Plugin pages only serve assets inside their page root. Share existing rank assets via the bridge.
        icon_root = self.schema_path.parent / "assets" / "ranks"
        icon_names = {"master.png"} | {
            player["rank_icon"]
            for session in sessions
            for player in session["available_players"]
        }
        icons = {}
        for filename in icon_names:
            path = icon_root / filename
            if path.resolve().parent == icon_root.resolve() and path.is_file():
                icons[filename] = "data:image/png;base64," + base64.b64encode(
                    path.read_bytes()
                ).decode("ascii")
        return json_response(
            {
                "data": {
                    "enabled": bool(self.plugin._config.api_key),
                    "api_key_set": bool(self.plugin._config.api_key),
                    "check_interval": self.plugin._config.check_interval,
                    "timeout_ms": self.plugin._config.timeout_ms,
                    "session_count": len(sessions),
                    "enabled_session_count": sum(
                        1 for item in sessions if item["enabled"] and item["allowed"]
                    ),
                    "player_count": len(players),
                    "notify_count": sum(
                        1 for item in players if item["watch_mode"] == "notify"
                    ),
                    "record_count": sum(
                        1 for item in players if item["watch_mode"] == "record"
                    ),
                    "runtime": state,
                    "sessions": sessions,
                    "groups": self._binding_groups(),
                    "recent_entries": self._recent_archive_entries(),
                    "rank_icons": icons,
                }
            }
        )

    def _binding_groups(self) -> list[dict[str, Any]]:
        configured = self._configured_sessions()
        groups: dict[str, dict[str, Any]] = {}
        for entry in configured:
            umo = str(entry.get("umo") or "").strip()
            parts = self._umo_parts(umo)
            group_id = str(entry.get("group_id") or (parts[2] if parts else "")).strip()
            if not group_id:
                continue
            item = groups.setdefault(
                group_id,
                {"group_id": group_id, "umos": [], "bindings": []},
            )
            if umo and umo not in item["umos"]:
                item["umos"].append(umo)
        for group_id, group in self.plugin._store.iter_groups():
            item = groups.setdefault(
                str(group_id),
                {"group_id": str(group_id), "umos": [], "bindings": []},
            )
            if getattr(group, "origin", "") and group.origin not in item["umos"]:
                item["umos"].append(group.origin)
        for group_id in getattr(
            self.plugin, "_runtime_group_user_bindings", {}
        ):
            groups.setdefault(
                str(group_id),
                {"group_id": str(group_id), "umos": [], "bindings": []},
            )
        for item in groups.values():
            item["bindings"] = self._normalize_bindings(
                self.plugin._get_group_user_bindings(item["group_id"])
            )
        return [groups[key] for key in sorted(groups)]

    async def bindings(self):
        return json_response({"data": {"groups": self._binding_groups()}})

    def _recent_archive_entries(self) -> list[dict[str, Any]]:
        try:
            days = self.plugin._push_archive.list_days()
            return (
                list(reversed(self.plugin._push_archive.get_day(days[0]["date"])))[:8]
                if days
                else []
            )
        except (OSError, RuntimeError, ValueError):
            return []

    async def archive_days(self):
        try:
            return json_response(
                {"data": {"days": self.plugin._push_archive.list_days()}}
            )
        except (OSError, RuntimeError):
            return error_response("读取推送归档失败", status_code=500)

    async def archive_day(self, archive_date: str):
        try:
            entries = self.plugin._push_archive.get_day(archive_date)
            return json_response(
                {"data": {"date": archive_date, "entries": list(reversed(entries))}}
            )
        except ValueError:
            return error_response("日期格式无效")
        except (OSError, RuntimeError):
            return error_response("读取当日推送归档失败", status_code=500)

    async def archive_preview(self, archive_date: str, entry_id: str):
        try:
            path = self.plugin._push_archive.image_path(archive_date, entry_id)
            return json_response(
                {
                    "data": {
                        "image": base64.b64encode(path.read_bytes()).decode("ascii"),
                        "content_type": "image/png",
                    }
                }
            )
        except ValueError:
            return error_response("卡片地址无效")
        except FileNotFoundError:
            return error_response("卡片不存在或已过期", status_code=404)
        except OSError:
            return error_response("读取卡片失败", status_code=500)

    async def save_subscription(self):
        body = await request.json(default={})
        try:
            if not isinstance(body, dict):
                raise ValueError("订阅请求格式无效")
            umo = self._validate_umo(body.get("umo"))
            parts = self._umo_parts(umo)
            if parts[1] != "GroupMessage":
                raise ValueError("玩家订阅目前仅支持 GroupMessage 群会话")
            group_id = parts[2]
            action = body.get("action", "add")
            if action not in {"add", "remove"}:
                raise ValueError("订阅操作无效")
            mode = body.get("watch_mode", "notify")
            if mode not in {"notify", "record"}:
                raise ValueError("监控模式无效")
            async with self._mutation_lock:
                group = self.plugin._store.get_group(group_id)
                key = str(body.get("player_key") or "").strip()
                record = group.players.get(key) if group and key else None
                if action == "remove":
                    current = getattr(self.plugin, "_runtime_session_subscriptions", {})
                    subscriptions = dict(current.get(umo, {}))
                    if umo not in current and group and group.origin == umo:
                        subscriptions = {
                            key: self.plugin._watch_mode(player)
                            for key, player in group.players.items()
                        }
                    subscriptions.pop(key, None)
                    self.plugin._set_session_subscriptions(umo, subscriptions)
                    return json_response({"data": {"saved": True}})
                if not self.plugin._is_group_allowed(group_id, umo):
                    raise ValueError("本群未在白名单中，请先调整访问控制")
                if record is None and key:
                    raise ValueError("该监控玩家不存在，请刷新页面")
                if record is None:
                    target = str(body.get("target") or "").strip()
                    if not target or len(target) > 200:
                        raise ValueError("请输入玩家名或 uid:...，或选择已有监控玩家")
                    alias = self.plugin._resolve_player_alias_info(
                        target, str(body.get("platform") or "")
                    )
                    if self.plugin._blocked_lookup_name(target, alias.player_name):
                        raise ValueError("该玩家在全局黑名单中，无法订阅")
                    identifier, use_uid = self.plugin._parse_identifier(
                        alias.player_name
                    )
                    if not identifier:
                        raise ValueError("玩家目标无效")
                    if not self.plugin._config.api_key:
                        raise ValueError("请先设置 Apex API Key")
                    (
                        data,
                        used_platform,
                    ) = await self.plugin._api.fetch_player_stats_auto(
                        identifier, alias.platform, use_uid
                    )
                    if self.plugin._blocked_lookup_name(data.name, f"uid:{data.uid}"):
                        raise ValueError("该玩家在全局黑名单中，无法订阅")
                    if data.rank_score < self.plugin._config.min_valid_score:
                        raise ValueError("API 返回无效分数，未创建订阅")
                    platform = self.plugin._normalize_platform(used_platform)
                    key = self.plugin._build_player_key(identifier, platform, use_uid)
                    group = self.plugin._store.get_group(group_id)
                    record = group.players.get(key) if group else None
                    if record is None:
                        record = PlayerRecord(
                            player_name=data.name,
                            platform=platform,
                            lookup_id=identifier,
                            use_uid=use_uid,
                            rank_score=data.rank_score,
                            rank_name=data.rank_name,
                            rank_div=data.rank_div,
                            last_checked=now_epoch_ms(),
                            global_rank_percent=data.global_rank_percent,
                            selected_legend=data.selected_legend,
                            display_alias=alias.alias_display if alias.matched else "",
                            alias_target=alias.alias_target if alias.matched else "",
                            watch_mode=mode,
                        )
                if self.plugin._should_skip_poll_player(record):
                    raise ValueError("该玩家在全局黑名单中，无法订阅")
                if not self.plugin._is_group_allowed(group_id, umo):
                    raise ValueError("本群未在白名单中，未创建订阅")
                # Save an initial baseline, never synthesize a score-change push for a new player.
                group = self.plugin._store.get_group(group_id)
                if group is None:
                    self.plugin._store.ensure_group(group_id, umo)
                self.plugin._store.set_player(group_id, key, record)
                self.plugin._store.save()
                self.plugin._subscribe_session_player(umo, key, mode)
                return json_response({"data": {"saved": True, "player_key": key}})
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        except Exception as exc:
            from astrbot.api import logger

            logger.error(f"后台订阅玩家失败: {exc}")
            return error_response(
                "订阅失败，请检查玩家目标、API 状态和运行日志", status_code=500
            )

    async def settings(self):
        config = self._raw_config()
        schema = self._schema()
        values = {
            key: (
                ""
                if key in _SECRET_KEYS
                else deepcopy(config.get(key, item.get("default")))
            )
            for key, item in schema.items()
            if key != "session_settings"
            and isinstance(item, dict)
            and item.get("type") in {"bool", "int", "string"}
        }
        return json_response(
            {
                "data": {
                    "schema": schema,
                    "values": values,
                    "secrets": {key: bool(config.get(key)) for key in _SECRET_KEYS},
                    "sessions": self._sessions(),
                }
            }
        )

    @staticmethod
    def _validate_umo(value: Any) -> str:
        umo = str(value or "").strip()
        if (
            len(umo) > 300
            or not _UMO_RE.fullmatch(umo)
            or any(char in umo for char in "\r\n")
        ):
            raise ValueError("UMO 格式无效，应为 platform:MessageType:target")
        return umo

    def _validate_sessions(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or len(raw) > 500:
            raise ValueError("会话列表格式无效或超过 500 项")
        seen: set[str] = set()
        result = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("会话条目必须是对象")
            umo = self._validate_umo(item.get("umo"))
            if self._umo_parts(umo)[1] != "GroupMessage":
                raise ValueError("订阅会话必须为 GroupMessage 群会话")
            if umo in seen:
                raise ValueError("会话列表中存在重复 UMO")
            seen.add(umo)
            label = str(item.get("label") or "").strip()
            if len(label) > 80:
                raise ValueError("会话备注最多 80 个字符")
            if not isinstance(item.get("enabled", True), bool):
                raise ValueError("会话启用状态必须为布尔值")
            if not isinstance(item.get("season_keyword_enabled", True), bool):
                raise ValueError("赛季关键词开关必须为布尔值")
            output_mode = item.get("output_mode", "inherit")
            if output_mode not in {"inherit", "image", "text"}:
                raise ValueError("会话推送格式无效")
            result.append(
                {
                    "__template_key": "session",
                    "label": label,
                    "umo": umo,
                    "enabled": bool(item.get("enabled", True)),
                    "season_keyword_enabled": bool(
                        item.get("season_keyword_enabled", True)
                    ),
                    "group_id": self._umo_parts(umo)[2],
                    "output_mode": output_mode,
                }
            )
        return result

    @staticmethod
    def _validate_bindings(raw: Any) -> list[dict[str, str]]:
        if raw in (None, ""):
            return []
        if isinstance(raw, dict):
            raw = [{"user_id": key, "target": value} for key, value in raw.items()]
        if not isinstance(raw, list) or len(raw) > 500:
            raise ValueError("用户绑定列表格式无效")
        result = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("用户绑定条目必须是对象")
            user_id = str(item.get("user_id") or "").strip()
            target = str(item.get("target") or "").strip()
            if not user_id or len(user_id) > 100 or not target or len(target) > 200:
                raise ValueError("用户绑定需要有效的用户 ID 和玩家目标")
            if user_id in seen:
                raise ValueError("用户绑定中存在重复用户 ID")
            seen.add(user_id)
            result.append({"user_id": user_id, "target": target})
        return result

    def _validate_binding_request(self, body: Any) -> tuple[str, str, str]:
        if not isinstance(body, dict):
            raise ValueError("绑定请求格式无效")
        group_id = str(body.get("group_id") or "").strip()
        user_id = str(body.get("user_id") or "").strip()
        target = self.plugin._normalize_binding_target(body.get("target"))
        if (
            not group_id
            or len(group_id) > 200
            or any(char in group_id for char in "\r\n")
        ):
            raise ValueError("群 ID 无效")
        if not user_id or len(user_id) > 100 or any(char in user_id for char in "\r\n"):
            raise ValueError("用户 ID 无效")
        if not target:
            return group_id, user_id, ""
        if len(target) > 200:
            raise ValueError("玩家绑定目标过长")
        identifier_text, _ = self.plugin._parse_alias_target_platform(target)
        identifier, _ = self.plugin._parse_identifier(identifier_text)
        if not identifier:
            raise ValueError("玩家绑定目标无效，应为玩家名或 uid:...")
        return group_id, user_id, target

    async def save_binding(self):
        body = await request.json(default={})
        try:
            group_id, user_id, target = self._validate_binding_request(body)
            self.plugin._set_group_user_binding(group_id, user_id, target or None)
            self.plugin._save_settings()
        except (ValueError, TypeError, AttributeError) as exc:
            return error_response(str(exc))
        except Exception:
            return error_response("绑定保存失败", status_code=500)
        return json_response(
            {
                "data": {
                    "saved": True,
                    "group_id": group_id,
                    "user_id": user_id,
                    "target": target,
                    "removed": not bool(target),
                }
            }
        )

    def _validate_values(self, values: Any) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("配置格式无效")
        schema = self._schema()
        supported = {
            key
            for key, item in schema.items()
            if key != "session_settings"
            and isinstance(item, dict)
            and item.get("type") in {"bool", "int", "string"}
        }
        unknown = set(values) - supported
        if unknown:
            raise ValueError("包含未知配置项")
        clean = {}
        for key, value in values.items():
            if key in _SECRET_KEYS and value in (None, ""):
                continue
            item = schema.get(key, {})
            kind = item.get("type")
            if kind == "bool" and not isinstance(value, bool):
                raise ValueError(f"{item.get('description', key)} 必须为布尔值")
            if kind == "int":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{item.get('description', key)} 必须为整数")
                limits = {
                    "check_interval": (1, 1440),
                    "timeout_ms": (1000, 120000),
                    "max_retries": (0, 10),
                    "min_valid_score": (0, 100000),
                    "push_archive_retention_days": (1, 365),
                }
                if key in limits and not limits[key][0] <= value <= limits[key][1]:
                    raise ValueError(f"{item.get('description', key)} 超出允许范围")
            if kind == "string" and not isinstance(value, str):
                raise ValueError(f"{item.get('description', key)} 必须为文本")
            options = item.get("options")
            if options and value not in options:
                raise ValueError(f"{item.get('description', key)} 的选项无效")
            clean[key] = value
        return clean

    def _apply_runtime_config(self) -> None:
        plugin = self.plugin
        plugin._config = plugin._config.__class__.from_raw(self._raw_config())
        plugin._api.update_config(
            api_key=plugin._config.api_key,
            timeout_ms=plugin._config.timeout_ms,
            max_retries=plugin._config.max_retries,
            debug_enabled=plugin._config.debug_logging,
            season_probe_player=plugin._config.season_probe_player,
        )
        plugin._archive_cleanup_day = None

    async def save_settings(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求配置格式无效")
        async with self._mutation_lock:
            config = self._raw_config()
            snapshot = deepcopy(dict(config))
            subscription_snapshot = deepcopy(
                getattr(self.plugin, "_runtime_session_subscriptions", {})
            )
            configured_snapshot = deepcopy(
                getattr(self.plugin, "_configured_session_umos", None)
            )
            keyword_snapshot = set(
                getattr(self.plugin, "_season_keyword_disabled_umos", set())
            )
            try:
                values = self._validate_values(body.get("values", {}))
                sessions = (
                    self._validate_sessions(body["sessions"])
                    if "sessions" in body
                    else self._configured_sessions()
                )
                subscriptions = deepcopy(subscription_snapshot)
                proposed_blacklist = set(
                    getattr(self.plugin, "_runtime_blacklist", set())
                ) | {
                    self.plugin._normalize_lookup_value(item)
                    for item in str(
                        values.get("blacklist", self.plugin._config.blacklist)
                    )
                    .replace("，", ",")
                    .split(",")
                    if item.strip()
                }
                for raw_session in body.get("sessions", []):
                    umo = raw_session["umo"].strip()
                    if "subscriptions" not in raw_session:
                        # Preserve old API clients and standard AstrBot configuration forms.
                        subscriptions.setdefault(umo, {})
                        continue
                    raw_players = raw_session["subscriptions"]
                    if not isinstance(raw_players, list) or len(raw_players) > 500:
                        raise ValueError("订阅玩家列表格式无效或超过 500 项")
                    group_id = self._umo_parts(umo)[2]
                    group = self.plugin._store.get_group(group_id)
                    clean = {}
                    for item in raw_players:
                        if not isinstance(item, dict):
                            raise ValueError("订阅玩家必须为对象")
                        key = str(item.get("player_key") or "").strip()
                        mode = item.get("watch_mode", "notify")
                        if not key or mode not in {"notify", "record"} or key in clean:
                            raise ValueError("订阅玩家目标、模式或重复项无效")
                        if group is None or key not in group.players:
                            raise ValueError("订阅玩家已不存在，请刷新页面")
                        if key not in subscription_snapshot.get(umo, {}):
                            player = group.players[key]
                            if any(
                                self.plugin._normalize_lookup_value(candidate)
                                in proposed_blacklist
                                for candidate in (
                                    player.player_name,
                                    player.lookup_id,
                                    player.display_alias,
                                    player.alias_target,
                                )
                                if candidate
                            ):
                                raise ValueError("全局黑名单中的玩家无法添加订阅")
                        clean[key] = mode
                    if len(set(clean) - set(subscription_snapshot.get(umo, {}))):
                        allowed = not values.get(
                            "whitelist_enabled", self.plugin._config.whitelist_enabled
                        ) or group_id in self.plugin._parse_list(
                            values.get(
                                "whitelist_groups", self.plugin._config.whitelist_groups
                            )
                        ) or umo in self.plugin._parse_list(
                            values.get(
                                "whitelist_groups", self.plugin._config.whitelist_groups
                            )
                        )
                        if not allowed:
                            raise ValueError("本群未在白名单中，无法新增订阅")
                    subscriptions[umo] = clean
                configured_umos = {str(item.get("umo") or "").strip() for item in sessions}
                previous_umos = {
                    str(item.get("umo") or "").strip()
                    for item in self._configured_sessions()
                } | (configured_snapshot or set())
                for umo in previous_umos - configured_umos:
                    subscriptions[umo] = {}
                config.update(values)
                config["session_settings"] = {"sessions": sessions}
                self.plugin._configured_session_umos = configured_umos
                disabled_umos = {
                    umo
                    for umo in getattr(self.plugin, "_season_keyword_disabled_umos", set())
                    if umo not in configured_umos
                }
                disabled_umos.update(
                    str(item.get("umo") or "").strip()
                    for item in sessions
                    if not item.get("season_keyword_enabled", True)
                )
                self.plugin._season_keyword_disabled_umos = {
                    umo for umo in disabled_umos if umo
                }
                self.plugin._runtime_session_subscriptions = subscriptions
                if hasattr(config, "save_config"):
                    config.save_config()
                self.plugin._save_settings()
                self._apply_runtime_config()
            except Exception as exc:
                config.clear()
                config.update(snapshot)
                self.plugin._runtime_session_subscriptions = subscription_snapshot
                self.plugin._configured_session_umos = configured_snapshot
                self.plugin._season_keyword_disabled_umos = keyword_snapshot
                try:
                    if hasattr(config, "save_config"):
                        config.save_config()
                    self.plugin._save_settings()
                    self._apply_runtime_config()
                except Exception:
                    pass
                if isinstance(exc, (ValueError, TypeError)):
                    return error_response(str(exc))
                return error_response("配置保存失败，原配置已恢复", status_code=500)
        return json_response({"data": {"saved": True}})
