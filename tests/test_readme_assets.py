from __future__ import annotations

import json
import re
from pathlib import Path


IMAGE_LINK_RE = re.compile(r"!\[[^\]]*]\(([^)]+)\)")
BAD_OVERVIEW_IMAGE_HASHES = {
    # 左下角 /apexrankwatch 预览错误显示 16000+ 分数但段位仍为钻石 1。
    "ca6d7d75d40c19a8a4295f928e53b9b8d486af099e2b1794f81d04924cce8206",
    "fdf9e54f806e895b106a37b42e79d1aaa34f00adae107a006e3e3942a1a0ea46",
}


def test_readme_images_use_absolute_urls_for_astrbot_dashboard():
    readme_path = Path(__file__).resolve().parents[1] / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    image_urls = IMAGE_LINK_RE.findall(content)

    assert image_urls
    assert [
        url
        for url in image_urls
        if not url.startswith(("https://", "http://"))
    ] == []


def test_readme_images_use_remote_repository_urls_for_astrbot_dashboard():
    readme_path = Path(__file__).resolve().parents[1] / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    image_urls = IMAGE_LINK_RE.findall(content)

    assert image_urls
    assert all("moeneri/" in url for url in image_urls)


def test_readme_score_change_image_uses_external_immutable_asset_url():
    root = Path(__file__).resolve().parents[1]
    content = (root / "README.md").read_text(encoding="utf-8")
    image_urls = IMAGE_LINK_RE.findall(content)
    score_urls = [
        url for url in image_urls if "score_change_chart_example" in url
    ]

    assert score_urls == [
        "https://cdn.jsdelivr.net/gh/moeneri/apexrankwatch-assets"
        "@97ad4c42346afa85333036192b56352540d7b053/"
        "images/score_change_chart_example_2.4.7.png"
    ]
    assert "astrbot_plugin_apexrankwatch" not in score_urls[0]
    assert not (root / "assets" / "readme" / "score_change_chart_example.png").exists()


def test_readme_overview_image_uses_immutable_cache_safe_url():
    readme_path = Path(__file__).resolve().parents[1] / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    image_urls = IMAGE_LINK_RE.findall(content)
    overview_urls = [
        url for url in image_urls if "assets/readme/command_effects_overview.png" in url
    ]

    assert overview_urls
    assert "@main/" not in overview_urls[0]
    assert re.search(r"@[0-9a-f]{40}/assets/readme/command_effects_overview\.png", overview_urls[0])


def test_readme_overview_image_is_not_known_bad_rankwatch_score_preview():
    import hashlib

    image_path = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "readme"
        / "command_effects_overview.png"
    )

    image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()

    assert image_hash not in BAD_OVERVIEW_IMAGE_HASHES


def test_metadata_version_is_current_release():
    metadata_path = Path(__file__).resolve().parents[1] / "metadata.yaml"
    metadata = metadata_path.read_text(encoding="utf-8")

    assert "version: 2.5.2" in metadata


def test_season_remaining_time_is_documented_in_readme_and_metadata():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")

    for content in (readme, metadata):
        assert "排位分段结束时间和剩余时间" in content

    # 国服更新口径必须写进文档（见 apex_service 里 b30150f 的说明）。
    assert "北京时间周三凌晨 1 点" in readme
    assert "00:30 关闭排位" in readme


def test_readme_intro_mentions_codex_assistance():
    readme_path = Path(__file__).resolve().parents[1] / "README.md"
    lines = readme_path.read_text(encoding="utf-8").splitlines()
    intro = "\n".join(lines[:8])

    assert "CODEX+GPT5.5" in intro
    assert "辅助编程" in intro


def test_repository_uses_astrbot_dashboard_readme_name():
    root = Path(__file__).resolve().parents[1]
    root_names = {path.name for path in root.iterdir()}

    assert "README.md" in root_names
    assert "readme.md" not in root_names


def test_astrbot_panel_schema_hints_are_single_line():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    multiline_hints = {
        key: value["hint"]
        for key, value in schema.items()
        if isinstance(value, dict) and "\n" in str(value.get("hint", ""))
    }

    assert multiline_hints == {}


def test_player_alias_panel_hint_uses_comma_separated_format():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    hint = schema["player_aliases"]["hint"]

    assert "逗号分隔" in hint
    assert "每行" not in hint
    assert "测试=uid:1234 pc,小明=EaName pc" in hint


def test_alias_feature_is_documented_in_readme_and_metadata():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")

    for content in (readme, metadata):
        assert "/apexalias add <别名>" in content
        assert "/apexalias list" in content
        assert "/apex绑定 <玩家名|uid:...>" in content
        assert "/apex绑定 list" in content
        assert "/apex解绑" in content
        assert "别名是全局查询映射" in content
        assert "绑定是个人默认查询目标" in content


def test_predator_short_command_is_documented_in_readme_and_metadata():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")

    for content in (readme, metadata):
        assert "/apexpredator [平台]" in content
        assert "/猎杀" in content


def test_score_change_feature_is_documented_in_readme_and_metadata():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")

    for content in (readme, metadata):
        assert "/持续记录 <玩家名|uid:...> [平台]" in content
        assert "/持续视奸列表" in content
        assert "/分数变化" in content
        assert "最多 50 次" in content
        assert "只记录不通报" in content


def test_readme_bottom_documents_data_and_api_sources():
    readme_path = Path(__file__).resolve().parents[1] / "README.md"
    readme = readme_path.read_text(encoding="utf-8")

    source_section = readme.split("## 数据来源", 1)[-1]

    assert "## 数据来源" in readme
    assert "Apex Legends API" in source_section
    assert "api.mozambiquehe.re" in source_section
    assert "rankedSeasonMeta" in source_section
    assert "实时返回为准" in source_section
    # 第三方赛季数据源已整体移除，不允许回流。
    assert "apexseasons.online" not in readme
    assert "esportstales" not in readme
    assert "EA 官方" not in readme
