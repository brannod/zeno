ZENO 3.4.2
==========

Zeno is a private local assistant with a modular Python backend, LM Studio model
support, memory, files, Live Browser, Browser Agent, Screen Reader, DeepSearch,
and a chat-only Discord bridge.

QUICK START
1. Install Python 3.11+.
2. Run INSTALL_ZENO.bat once if you want Playwright/PDF/Discord integrations.
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
- zeno_data/private/     private Discord configuration
- zeno_data/updates/     downloaded releases and updater backups

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


ZENO 3.4.2 CURRENT FEATURES
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
