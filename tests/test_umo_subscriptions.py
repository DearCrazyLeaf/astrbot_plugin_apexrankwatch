from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest
from test_output_mode import _load_main_module, _sample_player

A = "qq-a:GroupMessage:10001"
B = "qq-b:GroupMessage:10001"
C = "discord:GroupMessage:10001"
KEY = "uid:123456@PC"


@pytest.fixture
def plugin(tmp_path):
    module = _load_main_module()
    instance = object.__new__(module.Main)
    instance.config = {
        "api_key": "test",
        "output_mode": "text",
        "session_settings": {
            "sessions": [{"umo": A, "enabled": True}, {"umo": B, "enabled": True}]
        },
    }
    instance._config = module.PluginConfig.from_raw(instance.config)
    instance._data_dir = tmp_path
    instance._settings_file = tmp_path / "settings.json"
    instance._runtime_blacklist = set()
    instance._season_keyword_disabled_groups = set()
    instance._runtime_player_aliases = {}
    instance._runtime_user_bindings = {"u1": "legacy-player"}
    instance._runtime_group_user_bindings = {"10001": {"u2": "group-player"}}
    instance._runtime_session_subscriptions = {A: {KEY: "notify"}, B: {KEY: "record"}}
    instance._subscription_model_version = 1
    instance._store = module.GroupStore(tmp_path / "groups.json", module.logger)
    instance._store.ensure_group("10001", A)
    instance._store.set_player(
        "10001",
        KEY,
        module.PlayerRecord(
            player_name="yumola",
            lookup_id="123456",
            use_uid=True,
            platform="PC",
            rank_score=18000,
            rank_name="Master",
            rank_div=0,
            last_checked=0,
        ),
    )
    instance._push_archive = module.PushArchive(tmp_path / "push_archive")
    instance._poll_concurrency = 2
    instance._poll_semaphore = asyncio.Semaphore(2)
    instance.fetches = []
    instance.sends = []

    async def fetch(identifier, platform, use_uid):
        instance.fetches.append(identifier)
        return _sample_player(module), "PC"

    async def send(umo, content):
        instance.sends.append(umo)
        return True

    instance._api = types.SimpleNamespace(fetch_player_stats_auto=fetch)
    instance._send_active_message = send
    instance._send_active_image = send
    return instance


@pytest.fixture
def api(plugin, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("_apex_subscription_tests")
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(
        sys.modules, package.__name__ + ".storage", sys.modules["storage"]
    )
    monkeypatch.setitem(sys.modules, package.__name__ + ".utils", sys.modules["utils"])
    web = types.ModuleType("astrbot.api.web")
    web.json_response = lambda payload: payload
    web.error_response = lambda message, status_code=400: {
        "error": message,
        "status": status_code,
    }
    web.request = None
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
    module = importlib.import_module(package.__name__ + ".webui")
    monkeypatch.setattr(module, "json_response", web.json_response)
    monkeypatch.setattr(module, "error_response", web.error_response)
    instance = module.ApexRankWatchWebUI(plugin)

    def call(method, body):
        async def read(default=None):
            return body

        monkeypatch.setattr(module, "request", types.SimpleNamespace(json=read))
        return asyncio.run(getattr(instance, method)())

    instance.call = call
    return instance


def entries(plugin):
    days = plugin._push_archive.list_days()
    return plugin._push_archive.get_day(days[0]["date"]) if days else []


def test_same_player_fetched_once_and_each_umo_has_its_own_mode(plugin):
    asyncio.run(plugin._poll_once())
    assert plugin.fetches == ["123456"]
    assert plugin.sends == [A]
    assert {(item["umo"], item["status"]) for item in entries(plugin)} == {
        (A, "sent"),
        (B, "recorded"),
    }
    assert len(plugin._store.get_group("10001").players[KEY].history) == 1
    assert plugin._runtime_group_user_bindings == {"10001": {"u2": "group-player"}}


def test_distinct_umo_players_and_output_formats_are_isolated(plugin, tmp_path):
    module = _load_main_module()
    second = module.PlayerRecord.from_dict(
        plugin._store.get_group("10001").players[KEY].to_dict()
    )
    second.player_name = "second"
    second.lookup_id = "other"
    second_key = "other@PC"
    plugin._store.set_player("10001", second_key, second)
    plugin._runtime_session_subscriptions[B] = {second_key: "notify"}
    plugin.config["session_settings"]["sessions"][1]["output_mode"] = "image"
    card = tmp_path / "card.png"
    card.write_bytes(b"test-card")
    plugin._render_rank_change_image = lambda **kwargs: card
    asyncio.run(plugin._poll_once())
    assert sorted(plugin.fetches) == ["123456", "other"]
    log = {entry["umo"]: entry for entry in entries(plugin)}
    assert log[A]["player"] == "yumola"
    assert log[B]["player"] == "second"
    assert log[A]["image"] is None
    assert (
        plugin._push_archive.image_path(log[B]["date"], log[B]["id"]).read_bytes()
        == b"test-card"
    )


@pytest.mark.parametrize("restriction", ["whitelist", "player_blacklist", "all_paused"])
def test_automatic_polling_respects_global_and_session_controls(plugin, restriction):
    if restriction == "whitelist":
        plugin._config.whitelist_enabled = True
        plugin._config.whitelist_groups = "other-group"
    elif restriction == "player_blacklist":
        plugin._config.blacklist = "uid:123456"
    else:
        for session in plugin.config["session_settings"]["sessions"]:
            session["enabled"] = False
    asyncio.run(plugin._poll_once())
    assert plugin.fetches == []
    assert plugin.sends == []


def test_query_only_blocklist_does_not_stop_subscriptions(plugin):
    plugin._config.query_blocklist = "yumola,uid:123456"
    asyncio.run(plugin._poll_once())
    assert plugin.sends == [A]


def test_unsubscribe_one_umo_preserves_other_subscription_and_history(plugin, api):
    response = api.call(
        "save_subscription", {"umo": A, "player_key": KEY, "action": "remove"}
    )
    assert response["data"]["saved"]
    asyncio.run(plugin._poll_once())
    assert plugin.sends == []
    assert len(entries(plugin)) == 1
    assert entries(plugin)[0]["umo"] == B
    assert plugin._runtime_session_subscriptions[B] == {KEY: "record"}
    assert KEY in plugin._store.get_group("10001").players
    persisted = json.loads(plugin._settings_file.read_text(encoding="utf-8"))
    assert persisted["runtime_session_subscriptions"][A] == {}


def test_disabling_subscription_during_fetch_prevents_delivery(plugin):
    original = plugin._api.fetch_player_stats_auto

    async def fetch(*args):
        for session in plugin.config["session_settings"]["sessions"]:
            session["enabled"] = False
        return await original(*args)

    plugin._api.fetch_player_stats_auto = fetch
    asyncio.run(plugin._poll_once())
    assert plugin.sends == []
    assert entries(plugin) == []
    assert plugin._store.get_group("10001").players[KEY].rank_score == 18000


def test_migration_is_one_time_and_later_native_config_session_starts_empty(plugin):
    plugin._runtime_session_subscriptions = {}
    plugin._subscription_model_version = 0
    plugin._migrate_session_subscriptions()
    assert plugin._runtime_session_subscriptions[A] == {KEY: "notify"}
    assert plugin._runtime_session_subscriptions[B] == {KEY: "notify"}
    plugin.config["session_settings"]["sessions"].append({"umo": C})
    plugin._migrate_session_subscriptions()
    assert plugin._runtime_session_subscriptions[C] == {}
    assert plugin._runtime_user_bindings == {"u1": "legacy-player"}
    assert plugin._runtime_group_user_bindings == {"10001": {"u2": "group-player"}}


def test_admin_add_player_creates_subscription_baseline_without_binding_or_push(
    plugin, api
):
    umo = "qq-a:GroupMessage:20002"
    result = api.call(
        "save_subscription", {"umo": umo, "target": "uid:123456", "platform": "PC"}
    )
    assert result["data"]["saved"]
    assert plugin._runtime_session_subscriptions[umo] == {KEY: "notify"}
    assert plugin._store.get_group("20002").players[KEY].rank_score == 18888
    assert plugin.sends == []
    assert entries(plugin) == []
    assert plugin._runtime_user_bindings == {"u1": "legacy-player"}
    assert plugin._runtime_group_user_bindings == {"10001": {"u2": "group-player"}}


def test_admin_cannot_subscribe_globally_blocked_player(plugin, api):
    plugin._config.blacklist = "yumola"
    result = api.call("save_subscription", {"umo": A, "player_key": KEY})
    assert "黑名单" in result["error"]
    assert plugin.fetches == []


def test_settings_preserve_query_bindings_and_reject_invalid_subscriptions(plugin, api):
    before = dict(plugin.config)
    response = api.call(
        "save_settings",
        {
            "sessions": [
                {
                    "umo": A,
                    "subscriptions": [
                        {"player_key": "missing", "watch_mode": "notify"}
                    ],
                }
            ]
        },
    )
    assert "不存在" in response["error"]
    assert plugin.config == before
    response = api.call(
        "save_settings",
        {
            "values": {"reply_to_command": False},
            "sessions": [
                {
                    "umo": A,
                    "subscriptions": [{"player_key": KEY, "watch_mode": "record"}],
                }
            ],
        },
    )
    assert response["data"]["saved"]
    assert not plugin._config.reply_to_command
    assert plugin._runtime_session_subscriptions[A] == {KEY: "record"}
    assert plugin._runtime_group_user_bindings["10001"]["u2"] == "group-player"
    assert plugin.config["session_settings"]["sessions"][0]["__template_key"] == "session"


def test_dashboard_settings_follow_schema_and_keep_session_template(plugin, api):
    data = asyncio.run(api.settings())["data"]
    assert data["schema"]["session_settings"]["items"]["sessions"]["templates"]["session"]
    assert data["sessions"][0]["__template_key"] == "session"
    assert {
        "owner_qq",
        "data_dir",
        "font_auto_download",
        "font_download_url",
        "max_retries",
        "min_valid_score",
        "alias_enabled",
        "alias_admin_only",
        "allow_private",
        "season_probe_player",
    } <= data["values"].keys()


def test_group_whitelist_matches_group_id_globally_and_umo_exactly(plugin):
    plugin._config.whitelist_enabled = True
    plugin._config.whitelist_groups = f"10001,{A}"
    assert plugin._is_group_allowed("10001", B)
    assert plugin._is_group_allowed("10001", A)
    plugin._config.whitelist_groups = A
    assert plugin._is_group_allowed("10001", A)
    assert not plugin._is_group_allowed("10001", B)
    targets = plugin._session_poll_targets("10001", plugin._store.get_group("10001"))
    assert {umo for umo, _, _ in targets} == {A}


def test_failed_delivery_and_fetch_error_are_archived_per_umo(plugin):
    async def fail_send(*args):
        return False

    plugin._send_active_message = fail_send
    asyncio.run(plugin._poll_once())
    assert {(item["umo"], item["status"]) for item in entries(plugin)} == {
        (A, "failed"),
        (B, "recorded"),
    }

    async def fail_fetch(*args):
        raise RuntimeError("service unavailable")

    plugin._api.fetch_player_stats_auto = fail_fetch
    asyncio.run(plugin._poll_once())
    failures = [item for item in entries(plugin) if item["status"] == "fetch_failed"]
    assert {item["umo"] for item in failures} == {A, B}
    assert failures[0]["error"] == "service unavailable"


def test_archive_validates_paths_and_retention_does_not_touch_live_data(plugin):
    directory = plugin._push_archive.directory
    old = directory / (date.today() - timedelta(days=40)).isoformat()
    old.mkdir()
    (old / "index.json").write_text('{"entries": []}', encoding="utf-8")
    plugin._store.save()
    plugin._save_settings()
    assert plugin._push_archive.cleanup(30) == 1
    assert not old.exists()
    assert plugin._settings_file.exists()
    assert (plugin._data_dir / "groups.json").exists()
    with pytest.raises(ValueError):
        plugin._push_archive.get_day("../settings.json")
    with pytest.raises(ValueError):
        plugin._push_archive.image_path(date.today().isoformat(), "../settings")


def test_dashboard_exposes_subscriptions_and_bridge_safe_rank_assets(plugin, api):
    response = asyncio.run(api.dashboard())["data"]
    sessions = {item["umo"]: item for item in response["sessions"]}
    assert sessions[A]["subscriptions"] == [{"player_key": KEY, "watch_mode": "notify"}]
    assert sessions[B]["subscriptions"] == [{"player_key": KEY, "watch_mode": "record"}]
    assert "bindings" not in sessions[A]
    assert response["rank_icons"]["master.png"].startswith("data:image/png;base64,")
    assert response["enabled_session_count"] == 2
    plugin._config.whitelist_enabled = True
    plugin._config.whitelist_groups = "other"
    response = asyncio.run(api.dashboard())["data"]
    assert response["enabled_session_count"] == 0
    assert all(not item["allowed"] for item in response["sessions"])


def test_idle_poll_cleans_expired_archive_without_new_notifications(plugin):
    old = (
        plugin._push_archive.directory / (date.today() - timedelta(days=40)).isoformat()
    )
    old.mkdir()
    (old / "index.json").write_text('{"entries": []}', encoding="utf-8")
    plugin._config.api_key = ""
    asyncio.run(plugin._poll_once())
    assert not old.exists()
    assert KEY in plugin._store.get_group("10001").players
    assert not plugin.sends


def test_saving_blacklist_and_new_subscription_uses_new_policy(plugin, api):
    plugin._runtime_session_subscriptions[A] = {}
    result = api.call(
        "save_settings",
        {
            "values": {"blacklist": "uid:123456"},
            "sessions": [
                {
                    "umo": A,
                    "subscriptions": [{"player_key": KEY, "watch_mode": "notify"}],
                }
            ],
        },
    )
    assert "黑名单" in result["error"]
    assert plugin._runtime_session_subscriptions[A] == {}
