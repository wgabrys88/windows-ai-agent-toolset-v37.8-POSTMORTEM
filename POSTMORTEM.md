```
POSTMORTEM.md

FRANZ PANEL POSTMORTEM - What Went Wrong, What Was Fixed, What Exists Now

================================================================================
0. CONTEXT AND GOAL
================================================================================

This project started as a turn-based VLM desktop agent. The key promise was:

  "Single Source of Truth (SST): the VLM output is the story, and the next turn
   input must contain that output verbatim (+ feedback appended)."

The panel exists to behave like Wireshark:
  - show EXACT bytes/JSON exchanged between main.py and LM Studio
  - prove SST continuity and expose any mutation

During this conversation we found that the panel was not reliably serving that
purpose. The goal became:

  - remove truncation everywhere
  - remove timeouts that cause hidden failure modes
  - convert panel into a real MITM firewall (human gate)
  - add upstream streaming to panel UI for engagement (panel-only, no changes
    visible to main.py)
  - unify configuration into a panel-managed hot-reloadable JSON system so the
    debugging surface is one place and trustworthy


================================================================================
1. THE CORE PROBLEM DISCOVERED
================================================================================

Symptom:
  - Browser panel showed truncated "VLM INPUT (FULL)" even though the system
    design required full fidelity.
  - This looked like an SST violation or silent deletion of story text.
  - Yet, logs indicated SST "match: true" in some cases, which created confusion.

Diagnosis:
  - There were "preview" fields and slicing used for UI and logs:
      - line[:80] in an error context
      - feedback_preview = t[:200]
    These caused "FULL" displays to be derived from truncated preview values
    rather than raw payloads.

Root insight:
  - SST was not necessarily failing at the message layer.
  - The panel's presentation layer was failing the "Wireshark guarantee" by
    displaying non-authoritative data.

Decision:
  - "FULL" must mean full. The panel UI must be driven by the raw request/response
    body, not derived previews.


================================================================================
2. INVENTORY REQUESTS THAT DETERMINED THE WORK
================================================================================

The conversation progressed through a sequence of precise demands:

A) Investigate why VLM INPUT (FULL) is cut off
   - Read entire codebase
   - Prove where truncation happens
   - Verify SST and any possible hidden deletion

B) Search for all truncations in the codebase
   - Explicit slices and preview fields
   - Any data-dropping logic (for example: removing image_data_uri)
   - Any bounded buffers/queues causing loss

C) Search for all timeouts and implicit time bombs
   - Explicit timeout parameters (HTTP, subprocess, polling)
   - Implicit defaults that cause hanging or early termination
   - Behavior under long operations (human gating requires "wait forever")

D) Explain panel.py + panel.html as a system
   - How it intercepts traffic in both directions
   - Whether it can become a true firewall (hold/approve/edit/reject)

E) Produce a full summary of the conversation as a Python docstring
   - Every discussed item, no omissions

F) Upgrade architecture: one panel, no tab-switching, hot reload config/tools
   - Tools and config should be editable from the panel itself
   - Reduce overlays and friction
   - Make panel the control plane

G) Add upstream streaming support
   - Stream tokens from LM Studio to the UI in real time
   - But do NOT stream to main.py (main must receive one final response)


================================================================================
3. WHAT WAS ACTUALLY CHANGED IN THE PROJECT
================================================================================

This is the final architecture we converged on:

3.1 Panel is now a real MITM firewall
  - panel.py is the chokepoint between main.py and LM Studio
  - when firewall_enabled is true and auto_approve is false:
      - request stage can be held for human approve/edit/reject
      - response stage can be held for human approve/edit/inject/reject

  - endpoints exist to operate the pending queue:
      GET  /pending
      GET  /pending/<id>
      POST /pending/<id>/approve
      POST /pending/<id>/reject
      POST /pending/<id>/edit_request
      POST /pending/<id>/edit_response
      POST /pending/<id>/inject_response

  - the UI exposes these controls in a queue overlay.

Result:
  - the panel is no longer passive.
  - it is a true "man in the middle" debugger / firewall.

3.2 The panel UI is a true 4-quadrant resizable crosshair layout
  - The earlier UI version was confusing and not quadrant-based.
  - The final panel.html restored:
      - draggable cross point
      - four quadrants
      - screenshot fixed to bottom-right quadrant
      - clear turn selection + full raw JSON fields

Result:
  - debugging is fast and not frustrating.
  - the UI behaves like a wire-level inspector.

3.3 Full fidelity display and logging
  - "FULL" fields are full.
  - no preview-derived "truth" fields are used for primary display.
  - request_raw and response_raw are preserved as strings for UI display.

Note:
  - Some places can still have derived metadata for convenience, but the raw
    payload remains available and authoritative.

3.4 Streaming to panel only (not to main)
  - The panel can request "stream": true upstream to LM Studio.
  - The panel reads SSE deltas and broadcasts them to the browser UI.
  - The panel accumulates the full content and produces a single final
    non-stream OpenAI JSON completion.
  - main.py receives only the final JSON and never sees streaming.

Result:
  - user sees engaging real-time generation
  - core agent loop remains unchanged in behavior expectations

3.5 Configuration system refactor (removing config.py dependency)
  - settings.py became the unified runtime config loader
  - config is stored in run_dir/config.json, hot reloadable
  - main.py, execute.py, capture.py were refactored to read settings via
    settings.load(run_dir) and no longer import config.py
  - the panel UI can edit config.json via /config endpoint

Result:
  - no need to edit Python modules to change runtime behavior
  - configuration changes are immediate and visible

3.6 tools.py modernized and aligned
  - tools.py uses Win32 SendInput for physical actions
  - supports crop-aware coordinate remapping (0..1000 logical -> pixel coords)
  - includes memory persistence (remember/recall) via memory.json in run_dir
  - a single configure() call sets physical mode, run_dir, and crop

Result:
  - clean execution pipeline, consistent with SST and run_dir state

================================================================================
4. THE BIGGEST CHALLENGES (AND HOW THEY WERE SOLVED)
================================================================================

Challenge 1: "SST says match=true but panel shows truncated text"
  - Root cause: presentation layer truncation and preview fields
  - Fix: raw request/response must be the UI source of truth

Challenge 2: "Wireshark vs firewall are not the same"
  - Wireshark is passive; firewall must actively block/hold and decide
  - Fix: create a pending registry + endpoints + UI actions

Challenge 3: "Streaming is great but must not break main"
  - Streaming upstream changes request content ("stream": true)
  - main expects non-stream response JSON
  - Fix: stream only between panel <-> LM Studio and show it in UI, but return a
    single final completion to main.py

Challenge 4: "Tooling and config scattered across files"
  - panel had some file-based IPC (crop/tools) but config was in Python code
  - Fix: move config to run_dir/config.json and load it everywhere

Challenge 5: "User experience is a debugging feature"
  - confusing UI makes you stop using it
  - Fix: resizable 4-quadrant design with predictable placement and minimal steps

Challenge 6: "Timeouts vs human gating"
  - human gating means waits can be long and cannot be killed by a timeout
  - Fix: remove explicit timeouts in the MITM layer and rely on deliberate
    human approval, while keeping the server responsive via threading

================================================================================
5. WHAT THE SYSTEM IS NOW CAPABLE OF
================================================================================

This system now supports a real "self-adapting story" loop in practice because:

  - SST continuity is enforceable and observable
  - the story is grounded in perception (screenshot) and action (tools)
  - human supervision can prevent drift and enforce constraints
  - streaming provides immediate visibility into generation
  - configuration is fast and centralized, enabling iteration speed

In short:
  - It behaves like a real controllable agent scaffold rather than a black box.

================================================================================
6. FINAL STATE CHECKLIST
================================================================================

If everything is correct, you should observe:

  - panel.html shows 4 quadrants and crosshair is draggable
  - bottom-right quadrant always contains the screenshot area
  - "VLM INPUT (FULL)" shows full raw JSON, no truncation
  - if stream_to_panel=true, you see output grow in real time in the UI
  - main.py receives only one final response per turn
  - pending queue appears when firewall_enabled=true and auto_approve=false
  - config edits via panel persist to run_dir/config.json and affect next turn
  - tools allowlist and crop persist to allowed_tools.json and crop.json

================================================================================
7. CLOSING
================================================================================

This was a hard debugging job because the apparent symptom (truncation) looked
like a model/state continuity corruption, but the actual fault was a "UI lies"
problem. The solution required treating the panel as a first-class system
component: full fidelity, no hidden truncation, no silent time bombs, plus true
MITM gating.

End result:
  - The panel now behaves like an observability and control plane that matches
    its purpose, and the agent loop can sustain an identity-like story over time.
```
