"""
README.md

FRANZ - Full-Fidelity MITM Firewall Panel (Windows 11, Python 3.13)

================================================================================
1. PURPOSE
================================================================================

This project runs a turn-based desktop agent with a Vision-Language Model (VLM).
The core goal is full observability and control of the JSON messages exchanged
between the agent and the VLM server.

The panel acts as:
  - Wireshark: shows the exact request/response JSON that passes through it
  - MITM firewall: can hold, approve, edit, reject, or inject responses

A strict invariant is enforced:
  - The previous VLM output must be included verbatim as the prefix of the next
    VLM input (plus feedback appended). This is the Single Source of Truth (SST)
    rule and prevents silent story truncation/drift.

The panel can also request upstream token streaming from LM Studio and show the
response building in real time in the browser, while still returning only one
final non-stream response JSON to main.py.

================================================================================
2. TARGET ENVIRONMENT
================================================================================

  - OS: Windows 11
  - Python: 3.13 only
  - VLM server: LM Studio OpenAI-compatible endpoint

No legacy platform support is required or intended.

================================================================================
3. QUICK START
================================================================================

1) Start LM Studio
   - Ensure OpenAI-compatible endpoint is enabled
   - Default assumed: http://127.0.0.1:1235/v1/chat/completions

2) Run the panel (this also spawns main.py):
   python panel.py

3) Open the UI:
   http://127.0.0.1:1234/

4) Use the panel to:
   - pause/resume the agent
   - select crop region
   - enable/disable tools
   - inspect exact request/response JSON
   - optionally gate requests/responses (firewall mode)
   - optionally watch live token streaming in the UI

================================================================================
4. ARCHITECTURE (WIDE DATA FLOW)
================================================================================

The panel is the mandatory proxy between main.py and LM Studio.

  +-------------------------+         +-------------------------+
  |        Browser          |         |        LM Studio         |
  |       panel.html        |         | OpenAI-compatible VLM    |
  | - 4 quadrants UI        |         | /v1/chat/completions     |
  | - live SSE stream       |         | optional SSE streaming   |
  +------------^------------+         +------------^------------+
               |                                     |
               | SSE (/events)                       | HTTP (upstream)
               |                                     |
+--------------+--------------+        HTTP POST      |
|            panel.py         +-----------------------+
| - MITM firewall / proxy     |
| - logs full fidelity JSON   |
| - SST verification          |
| - optional upstream stream  |
| - can gate req/resp         |
| - spawns main.py            |
+--------------^--------------+
               |
               | HTTP POST /v1/chat/completions
               |
     +---------+---------+
     |       main.py     |
     | - agent loop      |
     | - builds request  |
     | - consumes reply  |
     +---------^---------+
               |
               | subprocess (per turn)
               |
     +---------+---------+
     |     execute.py    |
     | - parse tool calls|
     | - run tools.py    |
     | - run capture.py  |
     +---------^---------+
               |
               | subprocess
               |
     +---------+---------+
     |     capture.py    |
     | - Win32 capture   |
     | - crop/resize     |
     | - PNG -> base64   |
     +-------------------+

Key properties:
  - main.py never talks directly to LM Studio.
  - All VLM traffic is visible and controllable in the panel.
  - The panel UI should show full request/response content (no truncation).

================================================================================
5. FILES
================================================================================

Core code:
  - panel.py     : HTTP server, MITM proxy, SSE broadcaster, spawns main.py
  - panel.html   : UI, four quadrants with draggable crosshair, overlays
  - main.py      : agent loop; posts requests to panel proxy endpoint
  - execute.py   : extracts tool calls; runs tools and capture; returns feedback
  - capture.py   : Windows screenshot capture, crop, resize, PNG/base64
  - tools.py     : Win32 SendInput actions and persistent memory tools
  - settings.py  : runtime config loaded from run_dir/config.json (panel-managed)

Runtime files (created per run in panel_log/run_YYYYMMDD_HHMMSS/):
  - state.json          : agent state (turn, story, recent results)
  - PAUSED              : sentinel; when present main.py pauses
  - allowed_tools.json  : tools allowlist
  - crop.json           : crop region in absolute pixels
  - memory.json         : persistent memory used by remember/recall
  - turns.jsonl         : full fidelity per-turn log lines
  - turn_<id>.png       : screenshot extracted from the request (if present)
  - config.json         : runtime config for settings.py

================================================================================
6. SETTINGS (config.json)
================================================================================

settings.py reads/writes run_dir/config.json (panel-managed). Defaults are
created automatically on first run.

Important keys:
  - model                : VLM model name
  - temperature          : sampling temperature
  - top_p                : nucleus sampling
  - max_tokens           : response token limit
  - cache_prompt         : optional cache flag
  - width,height         : capture resize dimensions
  - capture_delay        : delay before capture
  - loop_delay           : delay between turns
  - physical_execution   : if true, execute mouse/keyboard actions
  - firewall_enabled     : if true, gate requests/responses in panel
  - auto_approve         : if true, firewall passes without waiting
  - stream_to_panel      : if true, panel requests upstream stream and shows it
  - upstream_url         : LM Studio endpoint URL
  - full_fidelity_logs   : keep full payloads in logs

Notes:
  - main.py loads settings each turn (hot reload).
  - execute.py and capture.py load settings per invocation.

================================================================================
7. PANEL UI (HOW TO USE IT WITHOUT GUESSING)
================================================================================

The UI is a single page with a 2x2 layout separated by a draggable cross point.
Drag the dot at the intersection to resize quadrants.

Quadrants:
  TOP LEFT:  VLM INPUT (FULL)
    - exact JSON request body received by panel.py for the selected turn

  TOP RIGHT: VLM OUTPUT
    - exact JSON response body returned to main.py for the selected turn
    - if stream_to_panel is enabled, shows streaming progress during generation

  BOTTOM LEFT: TURN INFO
    - parsed metadata (latency, model, sampling)
    - SST verification result (prefix check)

  BOTTOM RIGHT: SCREENSHOT
    - the screenshot embedded in the request (if present)

Left sidebar:
  - list of turns (most recent at the top)
  - click a turn to load full request/response into quadrants

Top bar buttons:
  - refresh  : reload turns + health
  - pause    : creates PAUSED sentinel to stop the agent loop
  - resume   : removes PAUSED sentinel
  - tools    : open tool allowlist overlay
  - select region : open crop selection overlay
  - config   : open config.json editor overlay
  - queue    : open pending firewall queue overlay (when firewall is enabled)

================================================================================
8. MITM FIREWALL BEHAVIOR
================================================================================

When firewall_enabled is true and auto_approve is false:

Request stage:
  - panel receives request from main.py
  - panel creates a pending item (stage=request)
  - panel does not forward upstream until you approve or edit the request
  - you can reject to return a synthetic error completion

Response stage:
  - after forwarding upstream and receiving a response
  - panel creates a pending item (stage=response)
  - panel does not return to main.py until you approve/edit/inject/reject

Queue overlay:
  - shows pending items (id + stage)
  - selecting an item loads raw request and raw response JSON
  - actions:
      approve
      reject (with message)
      edit request
      edit response
      inject response

================================================================================
9. UPSTREAM TOKEN STREAMING (PANEL-ONLY)
================================================================================

If stream_to_panel is true:
  - panel forwards upstream with stream=true
  - LM Studio streams token deltas (SSE)
  - panel accumulates full text, broadcasts delta events to the UI
  - main.py still receives a single final non-stream JSON completion

This means:
  - The UI sees real-time generation.
  - The agent code remains simple and does not need streaming support.

================================================================================
10. SST (SINGLE SOURCE OF TRUTH) VERIFICATION
================================================================================

SST check is strict:
  - current_user_text must start with previous_vlm_output (prefix match)

If this fails, the panel reports a violation in TURN INFO.
This prevents silent truncations from turning the agent into a different story.

================================================================================
11. TROUBLESHOOTING
================================================================================

No output / empty model response:
  - confirm LM Studio is running and upstream_url is correct in config.json

No screenshot:
  - capture uses Win32 GDI; ensure desktop capture is allowed
  - verify capture_delay and crop settings

Tools not executing:
  - open Tools overlay and enable tools
  - set physical_execution=true if you want real mouse/keyboard actions

Agent stuck:
  - check PAUSED state
  - check pending queue overlay (firewall may be waiting for approval)

================================================================================
12. IMPORTANT DESIGN RULES
================================================================================

  - Full fidelity: do not truncate "FULL" fields
  - Panel is the single chokepoint for VLM traffic
  - SST must be preserved for stable self-adapting story behavior
  - Streaming is UI-only and must not change main.py expectations
"""
