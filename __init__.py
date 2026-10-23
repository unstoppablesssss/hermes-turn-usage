"""turn-usage — 每輪結束時在回覆尾端附上用量摘要。

資料來源：~/.hermes/state.db 的 session_model_usage（每次 API 呼叫累加，含
billing_provider＝實際路由、estimated_cost_usd＝花費）與 sessions.model_config
的 `_usage_anchor.prompt_tokens`（目前 prompt 大小）。本輪增量靠自己存的上一輪
快照相減。任何失敗都原樣放行，絕不動到回覆。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

def _load_summary():
    """載入實作模組（summary.py）——相對匯入失敗時，用唯一命名的 spec loader。

    刻意不用 `import summary`：那是無前綴的頂層匯入，會綁到 sys.path 上任何一個
    叫 `summary` 的模組（可能完全不是我們這支）。spec loader 用獨一無二的名字
    （`turn_usage_summary`），且先註冊進 `sys.modules`——`importlib.reload()`
    只認已在 sys.modules 的模組，沒註冊熱重載就會失效。
    """
    try:  # 以 package 載入
        from . import summary as _s
        return _s
    except ImportError:
        pass
    import sys
    name = "turn_usage_summary"  # 目錄不在 sys.path：從同目錄以具名 spec 載入
    if name in sys.modules:
        return sys.modules[name]
    _spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("summary.py")
    )
    _s = importlib.util.module_from_spec(_spec)
    sys.modules[name] = _s
    _spec.loader.exec_module(_s)
    return _s


#: 熱重載用：目前採用的實作模組與它的檔案時間。
#: 為什麼需要轉發器：`ctx.register_hook` 存的是**函式物件**，reload 模組換不掉
#: registry 手上那一份。所以註冊的 callback 必須保持穩定，並在每次被呼叫時
#: 才向 `_impl()` 要最新實作——這樣改 summary.py 只要存檔就生效，不必重啟 Hermes。
_IMPL = None
_IMPL_MTIME = None


def _drop_pyc(path: Path) -> None:
    """刪掉對應的 .pyc。

    為什麼需要：Python 的 .pyc 驗證用「來源 mtime（整數秒）＋ 檔案大小」，
    所以「同一秒內、又改成長度相同的內容」會被判為沒變、reload 拿到舊碼。
    真實編輯通常跨秒，但改一個數字（3 → 4）就可能踩到——刪掉最保險。
    """
    try:
        for pyc in (path.parent / "__pycache__").glob(f"{path.stem}.*.pyc"):
            pyc.unlink(missing_ok=True)
    except Exception:
        pass


def _impl():
    """取最新實作；summary.py 變了就就地重載，載入失敗則沿用舊版。"""
    global _IMPL, _IMPL_MTIME
    path = Path(__file__).with_name("summary.py")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if _IMPL is None:
        _IMPL, _IMPL_MTIME = _load_summary(), mtime
        return _IMPL
    if mtime is not None and mtime != _IMPL_MTIME:
        try:
            _drop_pyc(path)
            fresh = importlib.reload(_IMPL)
            # 採納前先確認接口還在：半套重載（新邏輯配舊接口）會拋錯，
            # 而 hook 的 try/except 會讓它變成無聲失敗。
            for fn in ("format_block", "strip_previous_block", "diff_totals", "route_text"):
                if not callable(getattr(fresh, fn, None)):
                    raise TypeError(f"{fn} 不可呼叫")
            _IMPL, _IMPL_MTIME = fresh, mtime
            logger.info("turn-usage: 偵測到 summary.py 變更，已熱重載")
        except Exception:
            _IMPL_MTIME = mtime  # 不再重試同一個壞版本
            logger.warning("turn-usage: 熱重載失敗，沿用舊版", exc_info=True)
    return _IMPL

logger = logging.getLogger(__name__)

HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
DB_PATH = HOME / "state.db"
LOG_PATH = HOME / "logs" / "agent.log"
#: 外掛的持久資料放 plugin-data（與 Hermes 的 plugin state 慣例一致）。
#: 舊版寫在 cache/turn-usage/，讀取時仍會回那裡找，既有使用者的累計不會斷。
SNAP_DIR = HOME / "plugin-data" / "hermes-turn-usage"
LEGACY_SNAP_DIR = HOME / "cache" / "turn-usage"

TIMEOUT_S = 2.0  # 查 DB 的硬上限，hook 絕不能拖慢回覆
LOG_TAIL_BYTES = 400_000  # 只讀 log 尾端這麼多，避免掃大檔
UPSTREAM_SCAN_LINES = 400  # 從尾端最多回看幾筆該 session 的呼叫

# 現場單價 $/M token：(輸入, 快取讀取, 輸出)。用來把 token 換成金額——
# 不用 Hermes 的 estimated_cost_usd（實測低估約 20–29%，且方向會變，不能用固定倍率修正）。
# key 是 Hermes 的 billing_provider，也就是你在 config.yaml 的 model_aliases 取的別名。
# 下面是**範例**：請換成你所用 provider 的現價；查不到的別名一律用 FALLBACK_PRICES。
PROVIDER_PRICES = {
    "example-alias-a": (0.14, 0.0042, 0.42),
    "example-alias-b": (0.285, 0.0057, 1.14),
}
FALLBACK_PRICES = (0.30, 0.006, 1.20)


def _unit_prices(provider: str, overrides=None):
    """(輸入, 快取, 輸出) 的 $/M 單價。

    基底是內建的 provider 價格列；使用者若**明確**設定 price_in／price_cache／price_out，
    就逐項覆蓋該列（沒設的那一項仍用 provider 的價）。「沒設定」在這裡必須是 None：
    一旦塞了預設數字（例如 0.14），整張 provider 價格列就被那個數字蓋掉、等於失效。
    """
    base = PROVIDER_PRICES.get((provider or "").lower(), FALLBACK_PRICES)
    if not overrides:
        return base
    return tuple(o if o is not None else b for o, b in zip(overrides, base))

_TOTALS_SQL = """
    SELECT model, billing_provider, task, SUM(api_call_count), SUM(input_tokens),
           SUM(output_tokens), SUM(cache_read_tokens), SUM(reasoning_tokens),
           SUM(estimated_cost_usd)
      FROM session_model_usage
     WHERE session_id = ?
     GROUP BY model, billing_provider, task
"""


_COMPOSITION_SQL = """
    SELECT COALESCE(tool_name, ''), SUM(LENGTH(COALESCE(content, '')))
      FROM messages
     WHERE session_id = ? AND active = 1{ts_clause}
     GROUP BY 1
"""


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=TIMEOUT_S)


def load_totals(session_id: str, prices=None):
    """該 session 的累計（含全部 task）。主對話的 provider 另外列出當「路由」。

    ``prices`` 是 (輸入, 快取, 輸出) 的 $/M 單價**覆蓋**三元組（元素可為 None）；
    沒給、或該項為 None 的，用內建的 provider 價格列。
    """
    try:
        con = _connect()
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(_TOTALS_SQL, (session_id,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not rows:
        return None

    totals = {"calls": 0, "in": 0, "out": 0, "cache": 0, "cost": 0.0, "cost_est": 0.0,
              "reasoning": 0, "model": "", "providers": [],
              "prices": _unit_prices("", prices)}
    top_calls = -1
    for model, provider, task, calls, tin, tout, tcache, treason, est in rows:
        totals["calls"] += calls or 0
        totals["in"] += tin or 0
        totals["out"] += tout or 0
        totals["cache"] += tcache or 0
        totals["reasoning"] += treason or 0
        # 花費改用「token × 單價」實算（Hermes 的 estimated_cost_usd 低估約 20–29%，
        # 實算與 router 官方 key usage 只差 0.6%）。單價以設定為準；沒設定才用範例表。
        pin, pc, po = _unit_prices(provider, prices)
        totals["cost"] += (tin or 0) / 1e6 * pin + (tcache or 0) / 1e6 * pc + (tout or 0) / 1e6 * po
        totals["cost_est"] += est or 0.0
        if model and (calls or 0) > top_calls:
            top_calls, totals["model"] = (calls or 0), model
            totals["prices"] = (pin, pc, po)  # 顯示用單價跟著主要 provider
        if task == "" and provider and provider not in totals["providers"]:
            totals["providers"].append(provider)
    return totals


_UPSTREAM_RE = re.compile(r"upstream=(\S+)")
# 「等待型」工具：這些秒數幾乎都是等使用者（clarify 卡在 UI 直到人回答），
# 不是我的工時。算進「工具執行」會讓時間分配失真（「等你」被誤讀成「很慢」），
# 所以另計、不進活躍時間的分母。
_WAIT_TOOLS = frozenset({"clarify"})
# 工具執行時間：`agent.tool_executor: tool terminal completed (0.05s, 931 chars)`
_TOOL_TIME_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+ \[(\w+)\] agent\.tool_executor: tool (\S+) completed \(([\d.]+)s,")
# 模型呼叫時間：API call 行的 latency= 就是那次呼叫從送出到收完的時間（含生成）
_CALL_TIME_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO \[(\w+)\].*API call #\d+: .*latency=([\d.]+)s")


def turn_timing(session_id: str, since_ts=None):
    """本輪的時間分配：🧠 模型呼叫 latency 總和 vs ⚙️ 工具執行秒數總和，
    並附 ``by_tool``（工具名 → [秒數, 次數]），讓摘要能回答「時間花在哪個工具」。

    ``since_ts`` 是上一輪快照的 epoch 秒（本輪的起點）；沒有就用最近 10 分鐘。
    只讀 log 尾端，查不到就回 None（摘要不顯示時間那行，不假造）。
    """
    since = float(since_ts) if since_ts else (time.time() - 600.0)
    try:
        with LOG_PATH.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    marker = f"[{session_id}]"
    think = tool = 0.0
    calls = tools = 0
    wait = 0.0          # 等待型工具（見 _WAIT_TOOLS）的秒數，不計入活躍時間
    waits = 0
    #: 工具名 → [秒數, 次數]。log 本來就記了名字，不留下來就答不出
    #: 「時間花在哪個工具」（圖片識別？寫 code？）。只記名字與秒數，
    #: 工具的參數與輸出內容一律不碰。
    by_tool: dict[str, list] = {}
    for line in tail.splitlines():
        if marker not in line:
            continue
        m = _CALL_TIME_RE.match(line)
        if m:
            if _epoch(m.group(1)) >= since:
                think += float(m.group(3))
                calls += 1
            continue
        m = _TOOL_TIME_RE.match(line)
        if m:
            if _epoch(m.group(1)) >= since:
                name = m.group(3)
                secs = float(m.group(4))
                if name in _WAIT_TOOLS:
                    wait += secs
                    waits += 1
                    continue
                tool += secs
                tools += 1
                slot = by_tool.setdefault(name, [0.0, 0])
                slot[0] += secs
                slot[1] += 1
    total = time.time() - since
    if calls == 0 and tools == 0 and waits == 0:
        return None
    return {"total_s": total, "think_s": think, "tool_s": tool,
            "calls": calls, "tools": tools, "by_tool": by_tool,
            "wait_s": wait, "waits": waits}


def _epoch(stamp: str) -> float:
    """log 的時間戳（本地時間 YYYY-MM-DD HH:MM:SS）→ epoch 秒。"""
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def recent_upstreams(session_id: str):
    """從 agent.log 尾端抽該 session 最近的實際落點（`upstream=`，例如實際服務商名）。

    Hermes 的 DB 只記設定名（billing_provider），router 實際挑了哪一家
    只有 log 有。只讀檔尾 LOG_TAIL_BYTES，不掃全檔；查不到就回空 list
    （摘要只顯示設定名，不假造）。
    """
    try:
        with LOG_PATH.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    marker = f"[{session_id}]"
    found = []
    for line in tail.splitlines():
        if marker not in line or "API call #" not in line:
            continue
        match = _UPSTREAM_RE.search(line)
        if match:
            found.append(match.group(1))
    return found[-UPSTREAM_SCAN_LINES:]


def _snap_path(session_id: str) -> Path:
    return SNAP_DIR / f"{session_id}.json"


def read_snapshot(session_id: str):
    """讀上一輪快照；新位置沒有就回舊位置找，既有使用者的累計不會斷。"""
    for path in (_snap_path(session_id),
                 LEGACY_SNAP_DIR / f"{session_id}.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def write_snapshot(session_id: str, totals: dict, extra: dict | None = None) -> None:
    try:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in totals.items() if k not in ("model", "providers")}
        # 記下這一輪結束的時刻：下一輪要用它當「本輪起點」算時間分配。
        payload["ts"] = time.time()
        if extra:
            payload.update(extra)
        _snap_path(session_id).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        logger.warning("turn-usage: 快照寫入失敗（不影響回覆）", exc_info=True)


def current_prompt_tokens(session_id: str):
    """該 session 最後一次呼叫的 prompt 大小（DB 直接可讀，不必自己計數）。"""
    try:
        con = _connect()
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    if not row or not row[0]:
        return None
    try:
        anchor = (json.loads(row[0]) or {}).get("_usage_anchor") or {}
        return int(anchor.get("prompt_tokens") or 0) or None
    except (TypeError, ValueError):
        return None


def context_max_tokens():
    """模型 context 上限。Hermes 沒有穩定的公開取值函式，只讀設定檔的顯式值；
    讀不到就回 None，摘要只印絕對值（寧可留白，不假造）。"""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        for section, key in (("model", "context_length"), ("agent", "context_length")):
            node = cfg.get(section) if isinstance(cfg, dict) else None
            value = node.get(key) if isinstance(node, dict) else None
            if isinstance(value, int) and value > 0:
                return value
    except Exception:
        pass
    return None


def prompt_composition(session_id: str, since_ts=None):
    """目前 context 裡的訊息內容按來源分組的 [(tool_name, 字元數)]。

    只算 active 的訊息（被壓縮掉的舊訊息不算），且不含系統提示與工具定義
    ——所以佔比是「訊息內容內部」的相對值，摘要裡會標明。
    給了 ``since_ts``（epoch 秒）就只算那之後新增的訊息＝本輪。
    """
    clause, params = "", [session_id]
    if since_ts:
        clause = " AND timestamp >= ?"
        params.append(float(since_ts))
    try:
        con = _connect()
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(_COMPOSITION_SQL.replace("{ts_clause}", clause), params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return [(name or "", chars or 0) for name, chars in rows] or None


#: register(ctx) 抓到的外掛 context；設定一律經它讀（見 _setting）。
_CTX = None


def _setting(key: str, default):
    """讀設定：`ctx.get_config(key, default)`（config_schema 的 plugins.entries 設定）。

    Hermes 沒有舊版猜測的 `hermes_cli.plugins.plugin_setting`；設定只有 ctx 這條路。
    沒 ctx（例如單獨匯入模組測試）或讀取失敗就回傳預設值。
    """
    ctx = _CTX
    if ctx is not None:
        try:
            value = ctx.get_config(key, default)
            if value is not None:
                return value
        except Exception:
            logger.debug("turn-usage: 讀設定 %s 失敗，改用預設", key, exc_info=True)
    return default


def _price_setting(key: str):
    """使用者**明確設定**的單價；沒設定／不合法／<=0 一律回 None。

    刻意不傳預設值：`_setting(key, None)` 對未設定的鍵回 None，正好能分辨
    「沒設定」（沿用 provider 價格列）與「設了值」（覆蓋）——若這裡補上預設數字，
    等於把 provider 價格列整張蓋掉。
    """
    raw = _setting(key, None)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _composition_groups():
    """解析設定裡的 `群組名 = 工具1,工具2`（每行一組）成分類表。

    沒設定、或解析不出任何一組就回 None —— 讓實作沿用內建分類。
    設定成這樣可以自己決定怎麼歸類（例：把 patch 算回檔案）：
        檔案 = read_file,search_files,patch,write_file
        命令行 = terminal,execute_code
    """
    raw = _setting("composition_groups", "") or ""
    if not raw.strip():
        return None
    groups = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        label, _, members = line.partition("=")
        tools = tuple(t.strip() for t in members.split(",") if t.strip())
        if label.strip() and tools:
            groups.append((label.strip(), tools))
    return tuple(groups) or None


def transform(response_text, session_id, model, platform, **kwargs):
    """transform_llm_output callback：回傳附加摘要後的回覆。"""
    try:
        if not _setting("enabled", True):
            return None
        if not response_text or not session_id:
            return None
        # 單價設定是逐項覆蓋 provider 價格列；沒設定的項目保持 None。
        overrides = tuple(_price_setting(k)
                          for k in ("price_in", "price_cache", "price_out"))
        totals = load_totals(session_id, overrides)
        if not totals:
            return None
        # 顯示用單價＝實際套用的那一組（主要 provider 的價格列 ＋ 使用者覆蓋）
        eff_prices = totals.get("prices") or _unit_prices("", overrides)
        # 診斷用：一次 turn 內這個 hook 到底被叫了幾次、各帶哪個 session_id。
        # 症狀是一則回覆被附加多份摘要，而每份的「累計」不同 ——
        # 那就是不同 session_id 各被叫了一次。要判定得先有這行紀錄。
        logger.info(
            "turn-usage: transform session=%s platform=%s calls=%s text_len=%s",
            session_id, platform, totals.get("calls"), len(response_text or ""),
        )
        prev = read_snapshot(session_id)
        impl = _impl()
        impl.set_language(_setting("language", "en"))
        delta = impl.diff_totals(totals, prev)
        since_ts = (prev or {}).get("ts")
        timing = turn_timing(session_id, since_ts)
        if timing:
            # 累計活躍時間＝上一輪的累計＋本輪（思考／工具分開累積）
            timing["cum_think_s"] = float((prev or {}).get("cum_think_s") or 0) \
                + float(timing.get("think_s") or 0)
            timing["cum_tool_s"] = float((prev or {}).get("cum_tool_s") or 0) \
                + float(timing.get("tool_s") or 0)
            timing["cum_active_s"] = timing["cum_think_s"] + timing["cum_tool_s"]
            # 等待時間單獨累積，刻意不加進 cum_active_s：等使用者的時間不是工時。
            timing["cum_wait_s"] = float((prev or {}).get("cum_wait_s") or 0) \
                + float(timing.get("wait_s") or 0)
            # 逐工具的累計，跟 cum_tool_s 用同一套加法。舊快照沒有 cum_by_tool，
            # 就從空的開始疊（本對話前幾輪的細項會顯示 '—'，不假造）。
            cum_by_tool: dict[str, list] = {}
            for name, pair in ((prev or {}).get("cum_by_tool") or {}).items():
                try:
                    cum_by_tool[name] = [float(pair[0]), int(pair[1])]
                except (TypeError, ValueError, IndexError):
                    continue
            for name, pair in (timing.get("by_tool") or {}).items():
                slot = cum_by_tool.setdefault(name, [0.0, 0])
                slot[0] += float(pair[0])
                slot[1] += int(pair[1])
            timing["cum_by_tool"] = cum_by_tool
        write_snapshot(session_id, totals, extra={
            "cum_active_s": timing["cum_active_s"] if timing else float((prev or {}).get("cum_active_s") or 0),
            "cum_think_s": timing["cum_think_s"] if timing else float((prev or {}).get("cum_think_s") or 0),
            "cum_tool_s": timing["cum_tool_s"] if timing else float((prev or {}).get("cum_tool_s") or 0),
            "cum_wait_s": timing["cum_wait_s"] if timing else float((prev or {}).get("cum_wait_s") or 0),
            "cum_by_tool": (timing or {}).get("cum_by_tool")
                          or (prev or {}).get("cum_by_tool") or {},
        })
        if not _setting("show_footer", True):
            # 靜默模式：快照照常累計（時間分配的累計數據不斷鏈），只是不渲染、
            # 不附加。與 enabled=false 的差別：那是整個停用，連快照都停止更新。
            return None
        block = impl.format_block(
            delta,
            totals,
            model=totals["model"] or model or "?",
            route=impl.route_text(totals["providers"], recent_upstreams(session_id)),
            prompt_now=current_prompt_tokens(session_id),
            context_max=context_max_tokens(),
            show_total=bool(_setting("show_session_total", True)),
            show_time=bool(_setting("show_time_table", True)),
            show_composition=bool(_setting("show_composition", True)),
            show_bars=bool(_setting("show_bars", True)),
            show_cost_breakdown=bool(_setting("show_cost_breakdown", True)),
            price_in=eff_prices[0],
            price_cache=eff_prices[1],
            price_out=eff_prices[2],
            bar_width=int(_setting("bar_width", 10) or 10),
            cost_decimals=int(_setting("cost_decimals", 3) or 3),
            context_hint_pct=int(_setting("context_hint_pct", 70) or 0),
            groups=_composition_groups(),
            sources=prompt_composition(session_id),
            sources_delta=prompt_composition(session_id, since_ts),
            timing=timing,
            time_detail_min_pct=int(_setting("time_detail_min_pct", 10) or 0),
        )
        # 冪等：先剝掉先前已附加的那一份，再接新的。這個 hook 在同一個 turn 內
        # 可能被呼叫多次（Hermes 有兩個 persist 呼叫點），不剝就會疊成三份摘要。
        base = impl.strip_previous_block(response_text)
        return base + "\n\n" + block
    except Exception:  # hook 絕不能弄壞回覆
        logger.warning("turn-usage: 產生摘要失敗，原樣放行", exc_info=True)
        return None


def register(ctx):
    global _CTX
    _CTX = ctx  # 設定一律經 ctx.get_config 讀（見 _setting）
    ctx.register_hook("transform_llm_output", transform)
