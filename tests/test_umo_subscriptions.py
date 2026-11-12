from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import json
import shutil
import subprocess
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest
import httpx
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
    instance._season_keyword_disabled_umos = set()
    instance._runtime_player_aliases = {}
    instance._runtime_user_bindings = {"u1": "legacy-player"}
    instance._runtime_group_user_bindings = {"10001": {"u2": "group-player"}}
    instance._runtime_session_subscriptions = {A: {KEY: "notify"}, B: {KEY: "record"}}
    instance._subscription_model_version = 1
    instance._configured_session_umos = {A, B}
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

    instance._api = types.SimpleNamespace(
        fetch_player_stats_auto=fetch, update_config=lambda **kwargs: None
    )
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


def _event(group_id="10001", user_id="u1"):
    return types.SimpleNamespace(user_id=user_id, get_group_id=lambda: group_id)


def _reload_plugin(plugin, monkeypatch):
    module = _load_main_module()
    monkeypatch.setattr(module.Star, "__init__", lambda self, context: None)
    monkeypatch.setattr(module.Main, "_schedule_runtime_start", lambda self: None)
    config = deepcopy(plugin.config)
    config["data_dir"] = str(plugin._data_dir)
    return module.Main(None, config)


def test_legacy_binding_works_in_groups_and_group_override_is_local(plugin):
    assert plugin._resolve_user_binding_info(_event(), "").player_name == "legacy-player"
    plugin._set_user_binding_for_event(_event(), "u1", "group-override pc")
    assert plugin._resolve_user_binding_info(_event(), "").player_name == "group-override"
    assert plugin._resolve_user_binding_info(_event("20002"), "").player_name == "legacy-player"
    assert plugin._resolve_user_binding_info(_event(""), "").player_name == "legacy-player"


def test_dashboard_can_remove_inherited_binding_without_its_return_on_reload(
    plugin, api, monkeypatch
):
    before = asyncio.run(api.bindings())["data"]["groups"][0]["bindings"]
    assert {entry["user_id"] for entry in before} == {"u1", "u2"}
    result = api.call("save_binding", {"group_id": "10001", "user_id": "u1", "target": ""})
    assert result["data"]["removed"]
    plugin._store.save()
    reloaded = _reload_plugin(plugin, monkeypatch)
    try:
        assert reloaded._resolve_user_binding_info(_event(), "").player_name == ""
        assert reloaded._resolve_user_binding_info(_event("20002"), "").player_name == "legacy-player"
        assert reloaded._resolve_user_binding_info(_event(""), "").player_name == "legacy-player"
        after = asyncio.run(api.bindings())["data"]["groups"][0]["bindings"]
        assert {entry["user_id"] for entry in after} == {"u2"}
    finally:
        asyncio.run(reloaded._api.close())


def test_admin_name_lookup_cannot_bypass_uid_blacklist(plugin, api):
    plugin._config.blacklist = "uid:123456"
    before = deepcopy(plugin._runtime_session_subscriptions)
    result = api.call("save_subscription", {"umo": "qq-a:GroupMessage:20002", "target": "yumola"})
    assert "黑名单" in result["error"]
    assert plugin._store.get_group("20002") is None
    assert plugin._runtime_session_subscriptions == before
    assert plugin.sends == []


def test_poll_name_record_checks_returned_uid_before_recording_or_sending(plugin):
    record = plugin._store.get_group("10001").players[KEY]
    record.lookup_id = "yumola"
    record.use_uid = False
    plugin._config.blacklist = "uid:123456"
    asyncio.run(plugin._poll_once())
    assert plugin.fetches == ["yumola"]
    assert plugin.sends == []
    assert record.rank_score == 18000
    assert record.history == []
    assert entries(plugin) == []


def test_removing_paused_session_does_not_resume_it_or_remove_shared_player(plugin, api):
    plugin.config["session_settings"]["sessions"][0]["enabled"] = False
    result = api.call("save_settings", {"sessions": [{"umo": B, "enabled": True}]})
    assert result["data"]["saved"]
    assert plugin._runtime_session_subscriptions[A] == {}
    assert plugin._runtime_session_subscriptions[B] == {KEY: "record"}
    asyncio.run(plugin._poll_once())
    assert plugin.sends == []
    assert {entry["umo"] for entry in entries(plugin)} == {B}
    assert KEY in plugin._store.get_group("10001").players


def test_native_session_removal_is_detected_after_restart(plugin, monkeypatch):
    plugin.config["session_settings"]["sessions"][0]["enabled"] = False
    plugin._store.save()
    plugin._save_settings()
    plugin.config["session_settings"]["sessions"] = [{"umo": B, "enabled": True}]
    reloaded = _reload_plugin(plugin, monkeypatch)
    try:
        group = reloaded._store.get_group("10001")
        assert {umo for umo, _, _ in reloaded._session_poll_targets("10001", group)} == {B}
        persisted = json.loads(reloaded._settings_file.read_text(encoding="utf-8"))
        assert persisted["runtime_session_subscriptions"][A] == {}
        assert persisted["configured_session_umos"] == [B]
        assert KEY in group.players
    finally:
        asyncio.run(reloaded._api.close())


def test_native_season_enable_overrides_persisted_command_disable(plugin, monkeypatch):
    plugin._set_season_keyword_disabled(A, True)
    plugin.config["session_settings"]["sessions"][0]["season_keyword_enabled"] = True
    reloaded = _reload_plugin(plugin, monkeypatch)
    try:
        assert not reloaded._is_season_keyword_disabled(A, "10001")
        reloaded._season_keyword_disabled_umos.add(C)
        assert reloaded._is_season_keyword_disabled(C, "10001")
    finally:
        asyncio.run(reloaded._api.close())


def test_legacy_season_disable_is_migrated_into_configured_switches(plugin):
    plugin._migrate_season_keyword_settings(["10001"])
    assert plugin._is_season_keyword_disabled(A, "10001")
    assert plugin._is_season_keyword_disabled(B, "10001")
    assert all(
        not session["season_keyword_enabled"]
        for session in plugin.config["session_settings"]["sessions"]
    )


def test_failed_settings_save_restores_subscriptions_and_keyword_switches(plugin, api):
    class FailingConfig(dict):
        failures = 1

        def save_config(self):
            if self.failures:
                self.failures -= 1
                raise OSError("write failed")

    plugin.config = FailingConfig(plugin.config)
    before = deepcopy(dict(plugin.config))
    result = api.call(
        "save_settings",
        {"sessions": [{"umo": A, "season_keyword_enabled": False}]},
    )
    assert result["status"] == 500
    assert dict(plugin.config) == before
    assert plugin._configured_session_umos == {A, B}
    assert plugin._season_keyword_disabled_umos == set()
    assert plugin._runtime_session_subscriptions[B] == {KEY: "record"}
    persisted = json.loads(plugin._settings_file.read_text(encoding="utf-8"))
    assert persisted["season_keyword_disabled_umos"] == []
    assert persisted["configured_session_umos"] == sorted([A, B])


def test_dashboard_settings_update_actual_request_timeout_and_probe_cache(plugin, api, monkeypatch):
    from test_output_mode import _blank_name_uid_payload

    module = _load_main_module()
    client = module.ApexApiClient("old-key", 10000, 0, module.logger, season_probe_player="old-probe")
    seen = []

    async def run():
        await client._client.aclose()

        def respond(request):
            seen.append(request)
            return httpx.Response(200, json=_blank_name_uid_payload())

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=10)
        client._season_cache["current"] = (0, object())
        client._last_season_probe = (0, object())
        plugin._api = client
        # The handler normally runs inside the same request loop.
        webui_module = sys.modules[api.__class__.__module__]

        async def body(default=None):
            return {"values": {"api_key": "new-key", "timeout_ms": 30000, "season_probe_player": "new-probe"}}

        monkeypatch.setattr(webui_module, "request", types.SimpleNamespace(json=body))
        try:
            result = await api.save_settings()
            assert result["data"]["saved"]
            assert client._season_probe_player == "new-probe"
            assert client._season_cache == {}
            assert client._last_season_probe is None
            await client.fetch_player_stats_by_name("yumola", "PC")
            assert seen[0].extensions["timeout"]["read"] == 30
            assert seen[0].url.params["auth"] == "new-key"
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("web_api_available", [False, True])
def test_plugin_initializes_with_and_without_optional_web_api(tmp_path, monkeypatch, web_api_available):
    _load_main_module()
    package = types.ModuleType("_apex_startup_tests")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    if web_api_available:
        web = types.ModuleType("astrbot.api.web")
        web.json_response = lambda value: value
        web.error_response = lambda *args, **kwargs: None
        web.request = None
        monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
    else:
        monkeypatch.setitem(sys.modules, "astrbot.api.web", None)
        monkeypatch.delitem(sys.modules, package.__name__ + ".webui", raising=False)
    module = importlib.import_module(package.__name__ + ".main")
    monkeypatch.setattr(module.Star, "__init__", lambda self, context: setattr(self, "context", context))
    monkeypatch.setattr(module.Main, "_schedule_runtime_start", lambda self: None)
    routes = []
    context = types.SimpleNamespace(register_web_api=lambda *args: routes.append(args))
    instance = module.Main(context, {"data_dir": str(tmp_path), "api_key": ""})
    try:
        assert (instance._webui is not None) == web_api_available
        assert bool(routes) == web_api_available
        assert instance._store is not None
    finally:
        asyncio.run(instance._api.close())


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for Dashboard runtime checks")
def test_dashboard_frontend_save_runtime():
    script = Path(__file__).with_name("test_dashboard_save.cjs")
    result = subprocess.run(["node", "--test", str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
