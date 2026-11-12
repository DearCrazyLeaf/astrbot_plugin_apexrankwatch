from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ENTRY_RE = re.compile(r"[0-9a-f]{32}\Z")


class PushArchive:
    """Daily log of rank changes and per-UMO delivery results."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _date(value: str) -> date:
        if not _DATE_RE.fullmatch(value):
            raise ValueError("Invalid archive date")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Invalid archive date") from exc

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to read push archive: {exc}") from exc
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or any(
            not isinstance(item, dict) for item in entries
        ):
            raise RuntimeError("Push archive has an invalid format")
        return entries

    def append(
        self, entry: dict[str, Any], image: str | Path | None = None
    ) -> dict[str, Any]:
        now = datetime.now().astimezone()
        archive_date = now.date().isoformat()
        entry_id = uuid4().hex
        day_dir = self.directory / archive_date
        day_dir.mkdir(parents=True, exist_ok=True)
        image_name = None
        if image is not None and Path(image).is_file():
            image_name = f"{entry_id}.png"
            shutil.copyfile(image, day_dir / image_name)
        saved = {
            **entry,
            "id": entry_id,
            "date": archive_date,
            "created_at": now.isoformat(timespec="seconds"),
            "image": image_name,
        }
        index_path = day_dir / "index.json"
        entries = self._read(index_path)
        entries.append(saved)
        temporary = index_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {"date": archive_date, "entries": entries},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(index_path)
        finally:
            temporary.unlink(missing_ok=True)
        return saved

    def list_days(self) -> list[dict[str, Any]]:
        days = []
        for day_dir in self.directory.iterdir():
            if not day_dir.is_dir() or not _DATE_RE.fullmatch(day_dir.name):
                continue
            try:
                self._date(day_dir.name)
                entries = self._read(day_dir / "index.json")
            except (ValueError, RuntimeError):
                continue
            days.append({"date": day_dir.name, "entry_count": len(entries)})
        return sorted(days, key=lambda item: item["date"], reverse=True)

    def get_day(self, archive_date: str) -> list[dict[str, Any]]:
        self._date(archive_date)
        return self._read(self.directory / archive_date / "index.json")

    def image_path(self, archive_date: str, entry_id: str) -> Path:
        self._date(archive_date)
        if not _ENTRY_RE.fullmatch(entry_id):
            raise ValueError("Invalid archive image identifier")
        day_dir = (self.directory / archive_date).resolve()
        if day_dir.parent != self.directory.resolve():
            raise ValueError("Invalid archive path")
        path = (day_dir / f"{entry_id}.png").resolve()
        if path.parent != day_dir or not path.is_file():
            raise FileNotFoundError("Push archive image not found")
        return path

    def cleanup(self, retention_days: int, today: date | None = None) -> int:
        if retention_days < 1:
            raise ValueError("Retention must be at least one day")
        cutoff = (today or date.today()).toordinal() - retention_days
        removed = 0
        root = self.directory.resolve()
        for day_dir in self.directory.iterdir():
            if not day_dir.is_dir() or not _DATE_RE.fullmatch(day_dir.name):
                continue
            try:
                archive_date = self._date(day_dir.name)
            except ValueError:
                continue
            resolved = day_dir.resolve()
            if resolved.parent == root and archive_date.toordinal() < cutoff:
                shutil.rmtree(resolved)
                removed += 1
        return removed
