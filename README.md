# hermes-turn-usage

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that appends a compact,
collapsible **usage summary** to the end of every reply — model, route, tokens, cache hit
rate, cost and context occupancy — plus a **per-tool time breakdown**.

It is a pure-computation plugin: it never calls a model, opens no network connection and
spawns no subprocess. `state.db` is opened read-only. Any failure leaves the reply untouched.

## What it looks like

Collapsed (one line, always visible):

```
📊usage $0.050 ⏱️active 5m00s
```

Expanded:

| | This turn | Cumulative |
|---|---|---|
| ⚡ Calls | 21 | 141 |
| 📥 In | 4.0M | 16.4M |
| 💾 Cache hit | 100% | 99% |
| 📤 Out | 13.8K | 123.7K |
| 🤔 Reasoning | 57% | 67% |
| ⏱️ Active | 3m11s | 28m59s |
| 💰 Cost | $0.0239 | $0.1487 |

**Time breakdown** (excludes your reading and typing)

| Source | This turn | Cumulative | Share | Calls |
|---|---|---|---|---|
| 🧠 Model thinking / generation | 3m03s | 25m35s | 96% | 21 |
| ⚙️ Tool execution | 7.7s | 3m24s | 4% | 21 |
| ⤷ 💻 Command line | 7.3s | 7.3s | 4% | 14 |
| ⤷ 📄 File read | 0.1s | 0.1s | 0% | 2 |
| ⤷ 👁️ Image recognition | 0.0s | 0.0s | 0% | 1 |

The tool breakdown is two-level: `⤷` categories (command line / file edit / file read /
image recognition / network / skills), then an indented row per tool. A category is expanded
only when it exceeds `time_detail_min_pct` and holds more than one tool.

The footer is **English by default**; set `language: "zh"` to get Traditional Chinese for the
same block (`📊用量 $0.050 ⏱️活躍 5m00s`, `本輪 / 累計`, …), which is what the Taiwanese
author uses day to day.

## The footer becomes part of the conversation

The summary is appended to the response **before it is stored**, so it is part of the saved
reply and the model re-reads it on every later turn of that session. That is roughly
**1–2 KB per turn** of extra prompt (the collapsed line plus the expanded tables, and the
⚠️ *start a new chat* hint once context is high), and it costs a little on each call.

To stop paying it without uninstalling:

```yaml
plugins:
  entries:
    turn-usage:
      settings:
        enabled: false   # footer off; the plugin stays installed
```

## Install

```bash
# clone into your plugin directory
git clone https://github.com/<you>/hermes-turn-usage ~/.hermes/plugins/turn-usage

# enable + configure
hermes plugins enable turn-usage
```

Then **restart Hermes** — the plugin is loaded at session start.

## Configure

Everything is in the plugin's `config_schema` (`plugin.yaml`); Hermes reads the values from
`plugins.entries.turn-usage.settings` and hands them to the plugin through `ctx.get_config`.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | master switch — `false` disables the footer without uninstalling |
| `show_footer` | `true` | silent mode — `false` keeps the plugin loaded and its per-session snapshot updating (time accounting stays intact) but appends nothing to replies; `enabled: false` disables everything |
| `language` | `"en"` | footer language: `en` (default) or `zh` (Traditional Chinese) |
| `show_session_total` | `true` | add the cumulative column |
| `show_time_table` | `true` | the time-breakdown table |
| `time_detail_min_pct` | `10` | a tool category must reach this share of active time (and hold >1 tool) before its per-tool rows are expanded. `0` = always expand |
| `show_composition` | `true` | prompt-composition table |
| `show_bars` | `true` | the meter bars |
| `bar_width` | `10` | meter width in cells |
| `cost_decimals` | `3` | decimals for cost on the collapsed line (detail tables keep 4) |
| `context_hint_pct` | `70` | warn in the collapsed line once context occupancy reaches this %; `0` = off |
| `price_in` / `price_cache` / `price_out` | `0.14` / `0.0042` / `0.42` | **example** unit prices ($ per M tokens) used for the headline cost. Set these to your own provider's live prices; the built-in table is only a fallback |
| `composition_groups` | `""` | override the tool→category mapping, one `Label = tool1,tool2` per line |

## Design notes

- **Cost is `tokens × live unit price`**, keyed on `billing_provider` — *not* Hermes'
  `estimated_cost_usd`, which under-reports (measured 19–29% on main turns, 51–61% on
  auxiliary forks) and drifts in direction, so a constant correction factor does not work.
  Unit prices come from the `price_in` / `price_cache` / `price_out` settings; the bundled
  table is a fallback for when no setting is available.
- **Per-tool timing is parsed from `agent.log`** (`tool <name> completed (Xs, …)` and the
  model-call `latency=` lines), so no extra instrumentation is needed.
- **Waiting time is excluded.** `clarify` blocks on the human, so its seconds are reported
  on their own `⏸️` row marked *not counted* and kept out of the denominator — otherwise
  "waiting for you" reads as "the agent is slow".
- **Cache cost = context size × number of calls.** That is why the summary surfaces both:
  the cache column is usually the largest single line, and a long conversation pays it on
  every call.
- The block is re-rendered idempotently: any previously attached summary is stripped first
  (the marker regex recognises both the English and the Chinese title), because the hook can
  fire more than once per turn.
- Per-session snapshots live in `plugin-data/hermes-turn-usage/`; reads still fall back to the
  legacy `cache/turn-usage/` location so existing installs keep their cumulative counters.

## Requirements

- Hermes Agent ≥ 0.21 (uses the `transform_llm_output` hook)
- Python 3.9+ (stdlib only — no third-party dependencies)

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The pure-function layer (`summary.py`) and the plugin's settings/price handling are covered by
54 unit tests — no network, no on-disk fixtures.

## Notes

- Source comments are written in Traditional Chinese; the code and configuration are plain
  English identifiers, and the default user-facing strings are English.

## License

MIT
