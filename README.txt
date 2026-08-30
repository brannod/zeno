ZENO 3.6.7
==========

Zeno is a private local assistant with a modular Python backend, LM Studio model
support, memory, files, Live Browser, Browser Agent, Screen Reader, DeepSearch,
a shared Discord bridge, and an MCP client hub for local tools such as AYCD.

QUICK START
1. Install Python 3.11+.
2. Run INSTALL_ZENO.bat once to install Playwright/PDF/Discord/MCP integrations.
3. Start LM Studio's local server at http://127.0.0.1:1234.
4. Keep your configured 4B model available for Fast/Balanced. Deep uses the
   configured 30B model only when Deep is explicitly selected.
5. Run START_ZENO.bat.
6. Zeno opens at http://127.0.0.1:7860.

UPGRADING AN EXISTING ZENO
Copy these program files over your existing Zeno application folder. Keep your
existing memory/ and zeno_data/ folders. database.py migrates the existing
memory/zeno.db in place. Make a backup before any major software upgrade.

IMPORTANT FOLDERS
- memory/zeno.db          existing chats, memory, jobs, settings
- zeno_data/uploads/     uploaded source files
- zeno_data/outputs/     generated files
- zeno_data/browser_profile/ persistent Live Browser profile
- zeno_data/private/     private Discord + MCP credentials/configuration
- zeno_data/updates/     downloaded releases and updater backups


MCP / AYCD
----------
Zeno 3.6 is an MCP client. AYCD remains the MCP server. In Settings -> MCP
Servers, enter the Streamable HTTP endpoint shown by AYCD (for example a local
http://127.0.0.1:PORT/mcp URL) and its Bearer API key. The key is stored only
under zeno_data/private/mcp_servers.json and is never returned to the browser UI.

Normal browser-chat and Discord messages share the same MCP routing path. A
message that mentions AYCD can use discovered read-only AYCD tools automatically.
Tools that may change AYCD data require a separate approve/cancel reply.

MODEL ROUTING
- Fast: configured fast model
- Balanced: configured fast model
- Deep: configured deep model only when explicitly selected
Compute mode is independent of model mode.
The top-center model pill shows the actual requested/effective model name rather
than replacing it with a generic "busy" label.

GUI
- Crimson/black wallpaper theme with Zeno icon assets.
- Left chat-history panel and right tools panel can each be collapsed from the
  top bar. Collapse both for a full-width chat workspace. Panel state is saved
  locally in the browser.

DISCORD BRIDGE
Discord remains a shared chat interface. It supports normal messages, incoming
attachments, generated-file return/sync, progress updates, !screenshot,
!scramble, !removedupes, !last, !stop and the rest of the command list shown by
!help.

UPDATES
Settings -> Zeno Updates restores the GitHub Releases updater.
- Default public repository: brannod/zeno
- Optional automatic release checks
- Check now
- Download + validate release ZIP
- Install + backup
- Restart required after install
The updater only replaces approved root application files. It does not replace
memory/, zeno_data/, browser profiles, uploads, outputs, private Discord data,
or DISCORD_TOKEN.txt.

CHECK
Run: py -3 zeno.py --check
This validates startup/module integration without keeping the web server open.


ZENO 3.5.0 CURRENT FEATURES
- Recent Live Browser workspace/UI upgrades and deterministic multi-file search.
- Discord slash commands plus Browser Agent remote controls and /screen.
- Persistent reminders, conservative Memory Optimizer, and Task Router.
- Desktop Notetaker with configurable watch interval, watch instructions,
  monitor selection, unchanged-screen suppression, and main-chat observations.

ZENO 3.2 HIGHLIGHTS
- Chat send/history fix: your own message is rendered immediately before model
  streaming starts, then reconciled with the saved database copy after the turn.
- Runtime/Performance tab: context-build time, model-setup time, send-to-first-
  token latency, generation time, total turn time, and recent averages.
- Memory Manager 3.2: Hot/Warm/Cold/Pinned filters, editing, manual temperature
  controls, duplicate analysis, and conservative strong-duplicate merging.
- Restored Code Workspace GUI: HTML/CSS/JavaScript editing, preview, save, export.
- Restored Discord !compact command. It saves durable context into memory and
  starts a fresh context window without deleting visible chat history.
- GitHub release workflows include all modular files, including performance.py.


ZENO 3.4.4 NOTETAKER + LM STUDIO MODEL GUIDE
============================================

DESKTOP NOTETAKER
------------------
Open Tools -> NOTETAKER. The Notetaker is a passive desktop observer. It captures the selected monitor in memory, sends a bounded screenshot to the selected local vision model, and can append meaningful observations into the same main Zeno chat. It does not click, type, or control the desktop.

Recommended defaults:
- Vision model: huihui-qwen3-vl-4b-instruct-abliterated
- Watch interval: 20 seconds
- Skip unchanged screenshots: ON
- Add meaningful notes to main chat: ON

Analyze Now should move through Capturing -> Preparing -> Analyzing -> Complete. If it shows Error, the Notetaker panel now displays the actual backend error instead of only showing a generic status. Common causes are: LM Studio Local Server is off, no model is loaded, the selected model is not present, or the selected model does not support image input.

The Notetaker uses only a compact slice of recent main-chat text for relevance. It no longer injects the full chat/memory/browser context into every screen check, which reduces context-overflow failures and speeds up vision analysis.

LM STUDIO MODEL DROPDOWNS
--------------------------
Open Tools -> SETTINGS -> Models and click "Detect LM Studio models". Zeno reads the models currently visible to the LM Studio Local Server and fills the Fast / Balanced and Deep dropdowns. The default Fast / Balanced model remains huihui-qwen3-vl-4b-instruct-abliterated.

The Notetaker has its own Vision model dropdown. This lets you keep Zeno's normal chat model unchanged while choosing a different vision-capable model for desktop analysis. A model chosen for Notetaker must support image input.

After changing the normal Fast/Deep model, save settings and use Apply / reload model if needed. Changing only the Notetaker vision model does not require changing Zeno's normal chat model.

ZENO 3.4.4 NOTETAKER + MODEL SELECTION
---------------------------------------
- Files is the first Tools section. Notetaker is a dedicated Tools section.
- Notetaker captures the selected desktop monitor in memory and sends the screenshot to the selected vision model.
- Zeno prefers LM Studio's native POST /api/v1/chat multimodal endpoint and falls back to /v1/chat/completions when needed.
- In Settings -> Models, click Detect LM Studio models to refresh the dropdowns from LM Studio.
- The default Fast/Balanced and Notetaker model remains huihui-qwen3-vl-4b-instruct-abliterated.
- You can select another detected LM Studio model. For Notetaker, choose a vision-capable/VL model; Zeno labels likely vision models when identifiable.
- Notetaker has its own model selection, AI watch interval, monitor, watch instructions, skip-unchanged control, and same-main-chat posting.
- Analyze Now shows the exact current phase, selected model/transport, and any LM Studio error instead of silently staying on Analyzing.
- Memory retrieval is relevance-gated. Turning memory retrieval off now injects no long-term memories, and pinned memories no longer bypass topical relevance.


ZENO 3.4.4 NOTETAKER + CONTEXT HARDENING
========================================
- Notetaker now uses a dedicated LM Studio vision path instead of the shared chat/runtime request stack.
- It tries LM Studio native /api/v1/chat first and automatically falls back to /v1/chat/completions for recoverable native failures, including server-side 5xx errors.
- Model detection for Notetaker uses /api/v1/models directly, with /v1/models as compatibility fallback.
- Settings -> Models also falls back to the direct model inventory path if the runtime manager cannot enumerate models.
- The default Fast/Balanced and Notetaker model remains huihui-qwen3-vl-4b-instruct-abliterated.
- Notetaker prints a bounded traceback to the Zeno console when a screen analysis fails, while the GUI shows the concise error.
- Tiny fresh messages such as "test", "hi", and "ping" no longer inject old rolling summaries, memories, pages, or assistant replies.
- Pinned pages/files are preference hints only; they no longer bypass topical relevance.
- Source chips are stored/displayed only when the final assistant answer explicitly cites the matching [S#] label.
- The default personality no longer primes unrelated specialty topics into every reply.


NOTETAKER JOURNAL (3.5.0)
-------------------------
Tools -> Notetaker now includes a live interval activity panel, Last Check / Next Check countdown, total-check and unchanged-skip counters, a live activity log, a persistent Notes Archive, and a Note Detail selector. Detailed is the default and produces substantially fuller screen-journal notes. Notetaker notes are saved independently of main-chat posting, while screenshots remain in memory and are not continuously stored.


ZENO 3.5.0 WORKSTATION UI + CHAT HISTORY
-----------------------------------------
- The GUI uses a denser Bahnschrift-first workstation font stack and tighter spacing.
- A universal bottom processing rail appears for replies, Notetaker analysis, Browser Agent, DeepSearch, File Worker, and Screen Reader work.
- STOP on the rail uses Zeno's Universal Stop and pauses Notetaker too; it does not close the ordinary Live Browser session.
- The last viewed chat is remembered locally and explicitly reopened after relaunch.
- Current chats initially load the newest 300 messages. Use "Load older messages" at the top of the conversation to expand history in 300-message steps, up to 3,000 messages in one view.
- Chat history remains stored in memory/zeno.db; upgrades do not replace that database.


SCREEN READER RELIABILITY (3.5.0)
---------------------------------
- Screen Reader is a bounded one-time Live Browser scan. It is not the continuous watcher; use Desktop Notetaker for that.
- Ordinary web-page scans have a 90-second scan safety limit.
- Discord rendered-history scans have a 180-second normal safety limit.
- An explicitly requested full/entire scan is capped at 8 minutes, then Zeno analyzes everything collected so far.
- Visible-text fallback lines count toward the requested item target, so a Discord DOM-selector change cannot leave the reader chasing an unreachable structured-message count.
- Dynamic Discord pages now stop when the virtual-list top/scroll position stabilizes, even if timestamps/loaders keep mutating.
- Stop / Universal Stop is checked immediately after each browser step and discards the remaining scan before analysis.
- Browser step waits are bounded to keep Stop responsive.
- The GUI shows elapsed time and explicitly labels Screen Reader as a one-time scan.
