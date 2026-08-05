"""Smoke tests — verify all Kage modules import without errors.

These tests ensure the kurosoden/ package is properly structured and all
import paths resolve correctly. No actual bot connections needed.
"""

import sys
from pathlib import Path

# Standalone: kurosoden/ IS the project root with nekofetch vendored inside.
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))


def test_shared_imports():
    """All shared modules should import cleanly."""
    from kurosoden.shared.pipeline_manager import PipelineManager
    from kurosoden.shared.admin_assignment import AdminAssignmentEngine
    from kurosoden.shared.admin_assignment import AdminAssignment, AdminAvailability
    from kurosoden.shared.dedup import DedupService, DedupResult
    from kurosoden.shared.models import AdminAssignment, AdminAvailability

    assert PipelineManager is not None
    assert AdminAssignmentEngine is not None
    assert DedupService is not None
    assert DedupResult is not None


def test_bot_app_imports():
    """All bot app.py modules should be importable."""
    from kurosoden.bots.lelouch.app import build_lelouch
    from kurosoden.bots.levi.app import build_levi
    from kurosoden.bots.senku.app import build_senku
    from kurosoden.bots.gojo.app import build_gojo

    assert build_lelouch is not None
    assert build_levi is not None
    assert build_senku is not None
    assert build_gojo is not None


def test_bot_handler_imports():
    """All bot handler modules should be importable."""
    from kurosoden.bots.lelouch.handlers import register_all as lelouch_register
    from kurosoden.bots.levi.handlers import register_all as levi_register
    from kurosoden.bots.senku.handlers import register_all as senku_register
    from kurosoden.bots.gojo.handlers import register_all as gojo_register

    assert lelouch_register is not None
    assert levi_register is not None
    assert senku_register is not None
    assert gojo_register is not None


def test_dedup_result_defaults():
    """DedupResult defaults should be correct."""
    from kurosoden.shared.dedup import DedupResult

    r = DedupResult()
    assert r.exists is False
    assert r.source == ""
    assert r.bot_username is None
    assert r.request_code is None

    r2 = DedupResult(exists=True, source="main_channel", title="Test")
    assert r2.exists is True
    assert r2.source == "main_channel"


def test_miruro_source_imports():
    """Miruro source adapter should import and expose the source name."""
    from nekofetch.sources.miruro import MiruroSource

    src = MiruroSource(base_url="http://localhost:8000")
    assert src.name == "miruro"
    assert src.base_url == "http://localhost:8000"

    configured = MiruroSource({"api_base_url": "http://127.0.0.1:8000"})
    assert configured.base_url == "http://127.0.0.1:8000"


def test_admin_assignment_result_defaults():
    """AssignmentResult should hold correct admin info."""
    from kurosoden.shared.admin_assignment import AssignmentResult

    r = AssignmentResult(
        admin_telegram_id=12345,
        admin_name="Test Admin",
        tasks_active=2,
        tasks_completed=10,
    )
    assert r.admin_telegram_id == 12345
    assert r.admin_name == "Test Admin"
    assert r.tasks_active == 2
    assert r.tasks_completed == 10
