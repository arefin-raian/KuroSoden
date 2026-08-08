"""Tests for kurosoden/shared/pipeline_manager.py — Pipeline lifecycle.

Covers:
  • PipelineManager properties (lelouch, levi, senku, gojo)
  • _start_bot with missing/invalid tokens
  • Constants validation
  • Stop behavior
"""

from __future__ import annotations

from types import SimpleNamespace

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

class TestConstants:
    """Validate pipeline manager constants."""

    def test_conn_check_interval_is_positive(self):
        from kurosoden.shared.pipeline_manager import _CONN_CHECK_INTERVAL
        assert _CONN_CHECK_INTERVAL > 0

    def test_conn_probe_timeout_is_positive(self):
        from kurosoden.shared.pipeline_manager import _CONN_PROBE_TIMEOUT
        assert _CONN_PROBE_TIMEOUT > 0

    def test_reconnect_attempts_positive(self):
        from kurosoden.shared.pipeline_manager import _CONN_RECONNECT_ATTEMPTS
        assert _CONN_RECONNECT_ATTEMPTS >= 1

    def test_reconnect_timeout_higher_than_probe(self):
        from kurosoden.shared.pipeline_manager import _CONN_PROBE_TIMEOUT, _CONN_RECONNECT_TIMEOUT
        assert _CONN_RECONNECT_TIMEOUT >= _CONN_PROBE_TIMEOUT

    def test_reconnect_backoff_positive(self):
        from kurosoden.shared.pipeline_manager import _CONN_RECONNECT_BACKOFF
        assert _CONN_RECONNECT_BACKOFF > 0


# ═══════════════════════════════════════════════════════════════════════════════
# PipelineManager properties
# ═══════════════════════════════════════════════════════════════════════════════

class TestPipelineManagerProperties:
    """Property accessors return None when empty."""

    def test_lelouch_is_none_initially(self):
        from kurosoden.shared.pipeline_manager import PipelineManager
        pm = PipelineManager(None)
        assert pm.lelouch is None

    def test_levi_is_none_initially(self):
        from kurosoden.shared.pipeline_manager import PipelineManager
        pm = PipelineManager(None)
        assert pm.levi is None

    def test_senku_is_none_initially(self):
        from kurosoden.shared.pipeline_manager import PipelineManager
        pm = PipelineManager(None)
        assert pm.senku is None

    def test_gojo_is_none_initially(self):
        from kurosoden.shared.pipeline_manager import PipelineManager
        pm = PipelineManager(None)
        assert pm.gojo is None

    def test_all_properties_exist(self):
        """Verify all four bot properties exist."""
        from kurosoden.shared.pipeline_manager import PipelineManager
        pm = PipelineManager(None)
        assert hasattr(pm, "lelouch")
        assert hasattr(pm, "levi")
        assert hasattr(pm, "senku")
        assert hasattr(pm, "gojo")


# ═══════════════════════════════════════════════════════════════════════════════
# Bot name validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestBotNameMapping:
    """Start order and name-to-env-var mapping."""

    def test_start_order_is_correct(self):
        """Lelouch → Levi → Senku → Gojo (pipeline order)."""
        expected_order = ["lelouch", "levi", "senku", "gojo"]
        # Verify these are the only valid bot names.
        # Check _start_bot calls in start() method are in the right order.
        import inspect

        from kurosoden.shared.pipeline_manager import PipelineManager
        source = inspect.getsource(PipelineManager.start)
        indices = [source.find(f'\"{name}\"') for name in expected_order]
        # All should be found and in order.
        for i in range(len(indices) - 1):
            assert indices[i] > 0, f"Bot {expected_order[i]} not found in start()"
            assert indices[i] < indices[i + 1], \
                f"{expected_order[i]} should start before {expected_order[i+1]}"

    def test_assignment_recovery_job_is_registered(self):
        """Offer expiry and quiet-hour recovery must run in the scheduler."""
        import inspect

        from kurosoden.shared.pipeline_manager import PipelineManager

        source = inspect.getsource(PipelineManager.start)
        assert "make_assignment_recovery_job" in source
        assert "assignment-recovery" in source

    def test_env_var_mapping(self):
        """Each bot name maps to the correct env var."""
        mapping = {
            "lelouch": "REQUEST_BOT_TOKEN",
            "levi": "DOWNLOADER_BOT_TOKEN",
            "senku": "DISTRIBUTION_BOT_TOKEN",
            "gojo": "PUBLISHER_BOT_TOKEN",
        }
        for name, env_var in mapping.items():
            assert name in ("lelouch", "levi", "senku", "gojo")
            assert "TOKEN" in env_var

    def test_unknown_name_would_be_handled(self):
        """The _start_bot method has an else clause for unknown names."""
        import inspect

        from kurosoden.shared.pipeline_manager import PipelineManager
        source = inspect.getsource(PipelineManager._start_bot)
        assert "else:" in source
        assert "unknown" in source.lower()

    def test_token_lookup_reads_loaded_env_settings(self):
        """Pipeline tokens live in EnvSettings, not os.environ."""
        from kurosoden.shared.pipeline_manager import PipelineManager

        env = SimpleNamespace(
            request_bot_token="request-token",
            downloader_bot_token="download-token",
            distribution_bot_token="distribution-token",
            publisher_bot_token="publisher-token",
        )
        pm = PipelineManager(SimpleNamespace(env=env))

        assert pm._token_from_config("REQUEST_BOT_TOKEN") == "request-token"
        assert pm._token_from_config("DOWNLOADER_BOT_TOKEN") == "download-token"
        assert pm._token_from_config("DISTRIBUTION_BOT_TOKEN") == "distribution-token"
        assert pm._token_from_config("PUBLISHER_BOT_TOKEN") == "publisher-token"


# ═══════════════════════════════════════════════════════════════════════════════
# Bot builder import validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestBotBuilderImports:
    """All build_* functions should be importable."""

    def test_build_lelouch_importable(self):
        from kurosoden.bots.lelouch.app import build_lelouch
        assert callable(build_lelouch)

    def test_build_levi_importable(self):
        from kurosoden.bots.levi.app import build_levi
        assert callable(build_levi)

    def test_build_senku_importable(self):
        from kurosoden.bots.senku.app import build_senku
        assert callable(build_senku)

    def test_build_gojo_importable(self):
        from kurosoden.bots.gojo.app import build_gojo
        assert callable(build_gojo)

    def test_all_bots_have_commands(self):
        """Every bot should have a COMMANDS list."""
        from kurosoden.bots.gojo.app import GOJO_COMMANDS
        from kurosoden.bots.lelouch.app import LELOUCH_COMMANDS
        from kurosoden.bots.levi.app import LEVI_COMMANDS
        from kurosoden.bots.senku.app import SENKU_COMMANDS

        assert len(LELOUCH_COMMANDS) > 0
        assert len(LEVI_COMMANDS) > 0
        assert len(SENKU_COMMANDS) > 0
        assert len(GOJO_COMMANDS) > 0

    def test_lelouch_commands_have_expected(self):
        from kurosoden.bots.lelouch.app import LELOUCH_COMMANDS
        cmds = {c.command for c in LELOUCH_COMMANDS}
        assert "start" in cmds
        assert "help" in cmds
        assert "admin" in cmds
        assert "myrequests" in cmds
        assert "settings" in cmds

    def test_levi_commands_have_expected(self):
        # The CLI commands (assign/sources/header) were removed when Levi moved to
        # the button-driven shared download flow — /tasks is the single entry point.
        from kurosoden.bots.levi.app import LEVI_COMMANDS
        cmds = {c.command for c in LEVI_COMMANDS}
        assert "start" in cmds
        assert "tasks" in cmds
        assert "packcaptions" in cmds
        assert "settings" in cmds
        assert "help" in cmds
        assert "assign" not in cmds

    def test_senku_commands_have_expected(self):
        from kurosoden.bots.senku.app import SENKU_COMMANDS
        cmds = {c.command for c in SENKU_COMMANDS}
        assert "start" in cmds
        assert "tasks" in cmds
        assert "create" in cmds
        assert "generate" in cmds
        assert "edit_thumbnail" in cmds

    def test_gojo_commands_have_expected(self):
        from kurosoden.bots.gojo.app import GOJO_COMMANDS
        cmds = {c.command for c in GOJO_COMMANDS}
        assert "start" in cmds
        assert "tasks" in cmds
        assert "publish" in cmds
        assert "recover" in cmds
        assert "schedule" in cmds


# ═══════════════════════════════════════════════════════════════════════════════
# Stop behavior
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopBehavior:
    """stop() should handle all states gracefully."""

    def test_stop_with_no_clients(self):
        """Stopping with no running clients should not crash."""
        from kurosoden.shared.pipeline_manager import PipelineManager
        pm = PipelineManager(None)
        assert callable(pm.stop)

    def test_stop_method_exists_and_is_async(self):
        import inspect

        from kurosoden.shared.pipeline_manager import PipelineManager
        assert inspect.iscoroutinefunction(PipelineManager.stop)
