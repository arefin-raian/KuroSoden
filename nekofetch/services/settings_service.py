"""Runtime settings service.

Bridges the in-Telegram settings panel to durable storage. Feature toggles and branding
edits are persisted to the Mongo ``settings`` collection and applied live by mutating the
in-memory ``AppConfig`` (pydantic models are mutable), so most changes take effect without
a restart. On startup, ``apply_overrides`` re-applies persisted overrides over config.yaml.
"""

from __future__ import annotations

from typing import Any

from nekofetch.core.container import Container
from nekofetch.core.logging import get_logger

log = get_logger(__name__)

_OVERRIDES_KEY = "runtime_overrides"
_DEFAULTS_KEY = "runtime_defaults"


class SettingsService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def _load_doc(self) -> dict:
        if self._c.collections is None:
            return {}
        doc = await self._c.collections.settings.find_one({"key": _OVERRIDES_KEY})
        return (doc or {}).get("value", {})

    async def _save_doc(self, value: dict) -> None:
        if self._c.collections is None:
            return
        await self._c.collections.settings.update_one(
            {"key": _OVERRIDES_KEY}, {"$set": {"value": value}}, upsert=True
        )

    def _safe_defaults(self) -> dict:
        hidden = set(self._HIDDEN_FIELDS)

        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items() if k not in hidden}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        return clean(self._c.config.model_dump(mode="json"))

    async def seed_defaults(self) -> None:
        """Persist the config defaults beside overrides so Mongo shows every field.

        Defaults are not overrides. Operators can inspect them in MongoDB, while
        live edits still write only to ``runtime_overrides`` and keep shadowing
        config.yaml until cleared.
        """
        if self._c.collections is None:
            return
        await self._c.collections.settings.update_one(
            {"key": _DEFAULTS_KEY},
            {"$set": {"value": self._safe_defaults()}},
            upsert=True,
        )

    async def apply_overrides(self) -> None:
        """Apply persisted overrides onto the live config (called at startup).

        These come from the in-bot Settings panel and **shadow config.yaml**. We
        log how many fields are shadowed (just the key names, never the values —
        a template value can contain characters a non-UTF-8 console can't encode)
        so a "config.yaml edit isn't taking effect" is explained by the log.

        The full config snapshot is persisted separately by :meth:`seed_defaults`
        into the ``runtime_defaults`` doc for inspection — that is how the DB is
        "seeded" from config.yaml. We deliberately do NOT copy config values into
        ``runtime_overrides`` here: doing so would permanently shadow config.yaml
        (every future file edit ignored) and is unnecessary because the Settings
        panel already reads the live, config-applied values.
        """
        await self.seed_defaults()
        overrides = await self._load_doc()
        applied: list[str] = []
        for section, values in overrides.items():
            target = getattr(self._c.config, section, None)
            if target is None:
                continue
            for field, val in values.items():
                if hasattr(target, field):
                    setattr(target, field, val)
                    applied.append(f"{section}.{field}")  # key only, no value
        if applied:
            log.warning("settings.overrides.shadowing_config_yaml",
                        count=len(applied), fields=applied)

    async def clear_overrides(self) -> int:
        """Drop all runtime overrides so config.yaml becomes authoritative again.

        Returns the number of fields cleared. Note: values already applied to the
        live config persist until the next restart re-reads config.yaml.
        """
        doc = await self._load_doc()
        count = sum(len(v) for v in doc.values() if isinstance(v, dict))
        await self._save_doc({})
        log.info("settings.overrides.cleared", fields=count)
        return count

    async def set_value(self, section: str, field: str, value: Any) -> None:
        target = getattr(self._c.config, section, None)
        if target is None or not hasattr(target, field):
            raise KeyError(f"{section}.{field}")
        setattr(target, field, value)  # live
        doc = await self._load_doc()
        doc.setdefault(section, {})[field] = value
        await self._save_doc(doc)
        log.info("settings.updated", section=section, field=field, value=value)

        from nekofetch.services.log_channel_service import LogChannelService

        await LogChannelService(self._c).event(
            "admin", "setting_changed", section=section, field=field, value=value
        )

    async def toggle_feature(self, feature: str) -> bool:
        current = bool(getattr(self._c.config.features, feature))
        await self.set_value("features", feature, not current)
        return not current

    def feature_map(self) -> dict[str, bool]:
        return self._c.config.features.model_dump()

    # ── generic config introspection (drives the Settings control center) ──────
    # Only true credentials are hidden from the in-chat panel; everything else
    # (channel ids, stickers, force-sub channels, lists…) is configurable.
    _HIDDEN_FIELDS = {"api_token", "arolinks_api_key", "vplinks_api_key"}

    def section(self, name: str):
        return getattr(self._c.config, name, None)

    def section_fields(self, name: str) -> list[tuple[str, object, str]]:
        """Return ``(field, value, kind)`` for each editable field in a section.

        ``kind`` ∈ {``"bool"`` (toggle), ``"list"`` (comma-separated editor),
        ``"value"`` (free text/number)}. Only credentials are skipped.
        """
        target = self.section(name)
        if target is None:
            return []
        out: list[tuple[str, object, str]] = []
        for field, value in target.model_dump().items():
            if field in self._HIDDEN_FIELDS:
                continue
            if isinstance(value, bool):
                out.append((field, value, "bool"))
            elif isinstance(value, list):
                out.append((field, value, "list"))
            elif isinstance(value, (str, int, float)) or value is None:
                out.append((field, value, "value"))
        return out

    async def toggle(self, section: str, field: str) -> bool:
        current = bool(getattr(self.section(section), field))
        await self.set_value(section, field, not current)
        return not current

    async def set_typed(self, section: str, field: str, raw: str) -> object:
        """Coerce ``raw`` text to the field's current type, then persist it.

        Lists are entered comma-separated and coerced to the element type of the
        existing list (ints stay ints — important for channel-id lists)."""
        current = getattr(self.section(section), field, None)
        raw = raw.strip()
        if isinstance(current, bool):
            value: object = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int):
            value = int(raw)
        elif isinstance(current, float):
            value = float(raw)
        elif isinstance(current, list):
            items = [p.strip() for p in raw.split(",") if p.strip()]
            value = [int(i) for i in items] if self._is_int_list(section, field, current) else items
        else:
            value = raw
        await self.set_value(section, field, value)
        return value

    def _is_int_list(self, section: str, field: str, current: list) -> bool:
        """Whether a list field holds ints (channel ids), so appended/edited
        values coerce to int too.

        Relying on the *current* elements fails when the list is empty (a fresh
        ``force_subscribe_channels`` starts ``[]``), which would silently store
        the first id as a string. So we also consult the Pydantic field
        annotation — ``list[int]`` → int — and fall back to the runtime elements.
        """
        target = self.section(section)
        try:
            annotation = type(target).model_fields[field].annotation
            args = getattr(annotation, "__args__", ())
            if args and args[0] is int:
                return True
        except Exception:  # noqa: BLE001 — annotation introspection is best-effort
            pass
        return bool(current) and all(isinstance(x, int) for x in current)

    async def set_list_add(self, section: str, field: str, raw: str) -> list:
        """Append entr(y/ies) to a list field (deduped), preserving element type.

        Accepts a COMMA-SEPARATED input so pasting "a, b, c" adds three separate
        entries (each gets its own delete button) instead of one literal "a,b,c"
        item — the reported filestore-bots bug. A single value still works (no
        comma → one entry). Unlike :meth:`set_typed` (which REPLACES the whole
        list), this adds to what's already there, so existing entries survive."""
        current = list(getattr(self.section(section), field, []) or [])
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            return current
        is_int = self._is_int_list(section, field, current)
        for part in parts:
            item: object = int(part) if is_int else part
            if item not in current:
                current.append(item)
        await self.set_value(section, field, current)
        return current

    async def set_list_remove(self, section: str, field: str, index: int) -> list:
        """Remove the entry at ``index`` from a list field. Out-of-range is a
        no-op (the list may have changed since the screen was drawn)."""
        current = list(getattr(self.section(section), field, []) or [])
        if 0 <= index < len(current):
            current.pop(index)
            await self.set_value(section, field, current)
        return current
