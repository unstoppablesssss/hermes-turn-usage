"""外掛層測試：設定讀取、enabled 開關、單價設定。

用標準庫 unittest（這台機器沒裝 pytest）。跑法：
    cd <repo> && python3 -m unittest discover -s tests -t .
"""
import importlib.util
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_plugin():
    """以唯一名字載入 __init__.py（外掛在 Hermes 裡是 package，這裡獨立載入）。"""
    name = "turn_usage_plugin_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class FakeCtx:
    """最小 ctx：只實作 register() 會用到的兩個方法。"""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.ctx = FakeCtx()
        plugin.register(self.ctx)

    def tearDown(self):
        plugin._CTX = None

    def test_defaults_used_without_any_setting(self):
        self.assertIs(plugin._setting("enabled", True), True)
        self.assertEqual(plugin._setting("price_in", 0.14), 0.14)

    def test_register_captures_ctx_and_registers_hook(self):
        self.assertIs(plugin._CTX, self.ctx)
        self.assertIn("transform_llm_output", self.ctx.hooks)

    def test_configured_values_win(self):
        plugin._CTX = FakeCtx({"enabled": False, "price_in": 1.23, "bar_width": 40})
        self.assertIs(plugin._setting("enabled", True), False)
        self.assertEqual(plugin._setting("price_in", 0.14), 1.23)
        self.assertEqual(plugin._setting("bar_width", 10), 40)

    def test_broken_float_setting_falls_back(self):
        plugin._CTX = FakeCtx({"price_in": "not-a-number", "price_out": 0})
        self.assertIsNone(plugin._price_setting("price_in"))
        self.assertIsNone(plugin._price_setting("price_out"))


class TestEnabledGate(unittest.TestCase):
    def tearDown(self):
        plugin._CTX = None

    def test_disabled_returns_none_without_touching_totals(self):
        plugin._CTX = FakeCtx({"enabled": False})
        called = []
        original = plugin.load_totals
        plugin.load_totals = lambda *a, **k: called.append(a) or None
        try:
            self.assertIsNone(plugin.transform("hello", "sess", "m", "cli"))
        finally:
            plugin.load_totals = original
        self.assertEqual(called, [])  # 關掉就不查 DB

    def _silent_mocks(self, rendered, snapped):
        """兩個 show_footer 測試共用的 mock 組：完全離線、不碰真 DB/log。"""
        class FakeImpl:
            def set_language(self, lang): pass
            def diff_totals(self, totals, prev): return {}
            def strip_previous_block(self, text): return text
            def route_text(self, providers, ups): return ""
            def format_block(self, *a, **k):
                rendered.append(1)
                return "BLOCK"

        totals = {"calls": 1, "in": 1, "out": 1, "cache": 0, "cost": 0.0,
                  "cost_est": 0.0, "reasoning": 0, "model": "m",
                  "providers": [], "prices": (0.14, 0.0042, 0.42)}
        names = ("load_totals", "read_snapshot", "turn_timing", "write_snapshot",
                 "_impl", "recent_upstreams", "current_prompt_tokens",
                 "context_max_tokens", "prompt_composition")
        originals = tuple(getattr(plugin, n) for n in names)
        plugin.load_totals = lambda sid, prices=None: dict(totals)
        plugin.read_snapshot = lambda sid: None
        plugin.turn_timing = lambda sid, since_ts=None: None
        plugin.write_snapshot = lambda sid, totals, extra=None: snapped.append(sid)
        plugin._impl = lambda: FakeImpl()
        plugin.recent_upstreams = lambda sid: []
        plugin.current_prompt_tokens = lambda sid: None
        plugin.context_max_tokens = lambda: None
        plugin.prompt_composition = lambda sid, since_ts=None: None
        return names, originals

    @staticmethod
    def _restore_mocks(names, originals):
        for name, fn in zip(names, originals):
            setattr(plugin, name, fn)

    def test_show_footer_false_snapshot_kept_render_skipped(self):
        """show_footer=false（靜默模式）：快照照寫（累計不斷鏈），但不渲染、不附加。"""
        plugin._CTX = FakeCtx({"show_footer": False})
        rendered, snapped = [], []
        names, originals = self._silent_mocks(rendered, snapped)
        try:
            self.assertIsNone(plugin.transform("hello", "sess", "m", "cli"))
        finally:
            self._restore_mocks(names, originals)
            plugin._CTX = None
        self.assertEqual(snapped, ["sess"])  # 快照照寫
        self.assertEqual(rendered, [])       # 完全沒渲染

    def test_show_footer_default_true_still_appends(self):
        """未設定 show_footer（預設 true）：行為不變，頁腳照常附加。"""
        plugin._CTX = FakeCtx({})
        rendered, snapped = [], []
        names, originals = self._silent_mocks(rendered, snapped)
        try:
            result = plugin.transform("hello", "sess", "m", "cli")
        finally:
            self._restore_mocks(names, originals)
            plugin._CTX = None
        self.assertEqual(result, "hello\n\nBLOCK")
        self.assertEqual(len(rendered), 1)

    def test_price_settings_reach_load_totals(self):
        plugin._CTX = FakeCtx({"price_in": 1.0, "price_cache": 0.1, "price_out": 10.0})
        seen = {}
        original = plugin.load_totals
        plugin.load_totals = lambda sid, prices=None: seen.setdefault("prices", prices) and None
        try:
            self.assertIsNone(plugin.transform("hello", "sess", "m", "cli"))
        finally:
            plugin.load_totals = original
        self.assertEqual(seen["prices"], (1.0, 0.1, 10.0))


class TestUnitPrices(unittest.TestCase):
    def test_settings_when_given(self):
        self.assertEqual(plugin._unit_prices("any-alias", (1.0, 2.0, 3.0)), (1.0, 2.0, 3.0))

    def test_unit_prices_overrides_only_the_items_that_are_set(self):
        """只設其中一項時，其餘兩項仍用 provider 價格列（不是整組被蓋掉）。"""
        with unittest.mock.patch.dict(plugin.PROVIDER_PRICES, {"some-alias": (1.0, 2.0, 3.0)}):
            self.assertEqual(plugin._unit_prices("some-alias", (None, 0.5, None)), (1.0, 0.5, 3.0))
            self.assertEqual(plugin._unit_prices("some-alias", (None, None, None)), (1.0, 2.0, 3.0))

    def test_builtin_table_when_no_settings(self):
        """測試不依賴 repo 內那張價格表的實際內容（可換成自己的 provider）。"""
        with unittest.mock.patch.dict(plugin.PROVIDER_PRICES, {"some-alias": (1.0, 2.0, 3.0)}):
            self.assertEqual(plugin._unit_prices("some-alias"), (1.0, 2.0, 3.0))

    def test_fallback_for_unknown_alias(self):
        self.assertEqual(plugin._unit_prices("who-knows"), plugin.FALLBACK_PRICES)


if __name__ == "__main__":
    unittest.main()
