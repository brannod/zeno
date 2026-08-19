ZENO V2.7.21 - COMPACT COMPLETE GUIDE
====================================

Zeno is a private local AI workspace powered by a model running in LM Studio.
It combines persistent chat and memory, image/PDF/file analysis, public webpage
research, DeepSearch, an interactive Chromium browser, a public frontend code
workspace, citations, and downloadable file creation.

New users: follow FIRST_TIME_USER_GUIDE.txt once, then keep this file as the
complete everyday reference.

QUICK START
-----------
1. Load huihui-qwen3-vl-4b-instruct-abliterated in LM Studio.
   Optionally also load huihui-qwen3-vl-30b-a3b-instruct-abliterated for Deep.
2. Use at least a 16,384-token context; 32,768 is preferable when memory allows.
3. Start LM Studio's Local Server at http://127.0.0.1:1234.
4. Double-click START_ZENO.bat and keep its black window open.
5. Zeno opens at http://127.0.0.1:7860.

Run INSTALL_ZENO.bat once before first launch and after an upgrade when
requirements.txt changes. LM Studio, its Local Server, and START_ZENO.bat must
remain running while Zeno is in use.

MAIN TOOLS
----------
- Chat: local AI conversations with saved history and long-term memory.
- Web: read public webpages and ask cited questions about active sources.
- DeepSearch: follows relevant public same-site links and writes a cited report;
  its progress panel supports Pause, Resume, and Stop.
- Live Browser: a persistent Chromium session with tabs, clicking, typing,
  scrolling, vision questions, and DeepSearch from the current public page.
- Files: upload any file type up to 12 MB. Zeno directly reads common text/code,
  PDF/DOCX/XLSX/PPTX/archive formats, analyzes images with vision, stores unknown
  binary files safely as attachments, downloads generated files, and runs Smart File Worker.
- Memory: manage remembered facts, save readable checkpoints, and export a ZIP.
- Code: inspect, edit, preview, and export the public HTML/CSS/JavaScript workspace.
- Self-Dev: plan, inspect, validate, approve, back up, apply, and roll back a
  controlled change to Zeno's approved application files.
- Discord Bridge: reuse one bot identity for multi-user access to one linked
  Zeno conversation with shared history/summary/memory context plus safe file commands.

DeepSearch follows public links only. It does not sign in, submit forms, bypass
CAPTCHAs/paywalls, make purchases, or open logout/delete actions. Zeno can
inspect public frontend code delivered by a site; it cannot retrieve private
backends, databases, secrets, or server-only source.

SPEED, MODEL ROUTING, CONTEXT, AND HISTORY
------------------------------------------
- Fast uses the configured lightweight model; Deep uses the configured larger
  model; Balanced selects Deep only for clearly complex requests. Both model
  IDs are configured under Setup and must be loaded in LM Studio to be used.
  If only one is loaded, Zeno safely falls back to that available model.
- The top context meter estimates how much of the configured LM Studio context
  window the active chat, memory, pages, and files consume.
- Automatic memory extraction and rolling summaries wait until Zeno has been
  idle for 45 seconds. A new web or Discord message immediately cancels/yields
  that background work, so it cannot sit ahead of chat in LM Studio's queue.
- At roughly 70% context, the next idle maintenance pass compacts eligible old
  messages even if the normal message-count threshold has not been reached.
- History is always available from the top History button or the Chats tab. The
  history card displays the exact database path and saved chat/message counts.
- If an upgrade shows only a nearly empty new chat, close Zeno and copy the old
  memory folder plus its previous *_data folder into the new Zeno folder. Zeno
  automatically discovers the fuller earlier .db file, imports it as
  memory\zeno.db, and moves recognized saved data into zeno_data on restart.


CHAT ATTACHMENTS + CLIPBOARD
- Click the + button beside the message box to pick one or many files without leaving chat.
- Ctrl+V in the message box pastes text normally. Ctrl+V with a screenshot, copied image, or copied file attaches it to the next message.
- If focus is outside a text field, pasted plain text is routed into the Zeno message box instead of disappearing.
- Drag-and-drop files onto the composer also attaches them.
- Pasted/selected images are sent to Zeno as vision input when you send the message.
- If you attach files and press Send with no text, Zeno uses a default analyze-the-attachments request.

SMART FILE WORKER: RUN, DELIVER, BATCHES, AND QUEUE
---------------------------------------------------
1. Upload one or more text/list/CSV files under Files.
2. In Smart File Worker, Ctrl-click or Shift-click to select multiple files.
3. Choose a preset, add optional instructions, and click Run & deliver. Zeno
   creates the preview and queues the validated job automatically.
4. Use Preview only when you want to inspect Before -> After before approval.
5. Results under 200 lines appear directly in chat with a download; larger
   results are delivered as downloadable files.
6. Reorder waiting items with the arrow buttons. Pause/resume safely, cancel, or
   switch chats while jobs continue. Queue and history survive restarts.
7. Download only after Completed and Validation Passed appear.

Built-in presets:
- Scramble Lines / Proxies randomizes complete lines, interleaves providers,
  and preserves every complete proxy character-for-character exactly once.
- Shuffle Complete Lines changes only complete-line order.
- Remove Exact Duplicates keeps the first occurrence of each complete line.
- Sort Lines A-Z preserves records while sorting complete lines.
- Remove Blank Lines removes no nonblank data.
- Extract Email Addresses keeps exact first-seen email spellings.
- AI Transform Each Line applies written rules in validated chunks.
- AYCD Email:::Password Format protects the literal triple-colon separator.
- Email:Password Cleanup removes unwanted metadata while preserving credentials.

Recognized proxy forms include host:port:user:password,
user:password@host:port, and host:port. Proxy outputs add no numbering, headers,
or commentary. Validation checks counts, missing/altered records, duplicates,
delimiter structure, required ::: delimiters, and provider runs. Failed
validation creates no downloadable result.

Manage saved presets to create reusable rules. Built-ins are protected; Copy
creates an editable preset. For value edits, name the exact component to change
and everything that must be preserved.

ERROR RECOVERY AND JOB LOGS
---------------------------
AI transformations checkpoint every completed chunk. Pause and resume continue
from that checkpoint. On failure, Zeno keeps a bounded partial log, classifies
the cause, shows a suggested fix, and offers Retry from failed step. Common
classes include LM Studio connection, model context, malformed model output,
oversized output, missing source file, and validation failure. Cancelled jobs
never deliver a partial file.

OUTPUT VERSIONS AND RECYCLE BIN
-------------------------------
Zeno never silently overwrites a generated result. Repeated output for the same
source/name becomes v1, v2, v3, and so on; one is marked current. Restore as new
version copies an older result into a new current version while retaining the
history. Recycle hides a version and disables its old download link. Undo delete
restores it; permanent deletion is available only inside the recycle bin.

All generated files are stored in zeno_data\outputs. The Files panel displays
the exact output-folder location.

CONTROLLED SELF-DEV MODE
------------------------
Open Self-Dev from the top-right button or ask chat to improve Zeno's own app. Zeno creates a small patch
plan using the local model, validates exact one-time anchors and syntax, and
shows every Before/After operation for review. Nothing applies automatically.

When you explicitly approve a ready plan, Zeno first creates a ZIP backup under
zeno_data\self_dev\backups and then atomically replaces approved files. Restart
only when you choose. Rollback restores the pre-change backup.

Self-Dev is intentionally limited:
- only named core app/docs/installer files are eligible;
- chats, memory, databases, uploads, outputs, browser profiles, and credentials
  cannot be edited;
- its safety, localhost binding, backup, validation, and restart controls are
  protected from self-editing;
- new arbitrary commands, dynamic code execution, credential access, unsafe
  installer directives, and hidden network calls are blocked;
- at most ten small exact-match replacements are allowed in one plan.

Turn Self-Dev off inside its top-right window when it is not wanted. A plan can
fail safely if the local model returns invalid JSON, lacks enough source context,
or proposes an unsafe/ambiguous edit; correct the request and use Retry plan.

DISCORD CHAT BRIDGE (REUSING ZENO)
----------------------------------
The bridge runs inside Zeno and reuses the old Zeno Discord bot identity. It is
locked to one server, one text channel, and one linked Zeno chat. Every human in
that configured channel can talk to Zeno. Discord usernames are kept with each
message so the shared browser conversation can tell participants apart. Messages
and replies are written into the same memory\zeno.db conversation, while new
web-chat messages mirror back into Discord after the bridge is online. Old history
is never dumped into the channel.

Setup:
1. Stop the legacy Zeno Discord-bot process completely. Never run two programs
   simultaneously with the same bot token.
2. Run INSTALL_ZENO.bat once after upgrading so discord.py is installed.
3. In Discord, enable Developer Mode under User Settings > Advanced. Right-click
   the text channel to copy its ID. Server ID is optional; no user ID is required.
4. In Discord Developer Portal > the existing Zeno application > Bot, enable
   Message Content Intent. Reuse the token from Zeno's private local config; if
   it is unavailable, Reset Token once and use the replacement.
5. Open DISCORD_GUIDE.txt beside the program. Enter the token and channel ID.
   Server ID is optional; set ENABLED=true and leave CHAT_ID=CURRENT.
6. Open the Zeno chat to link. Under Setup > Zeno Discord Bridge, click Reload +
   link current chat. The status should change to Online.

Discord normally acts as a shared chat doorway into the same browser conversation,
history, rolling summary, personality, and long-term memory context. To avoid a
Discord participant accidentally being promoted into long-term memory, Discord messages are not
auto-promoted into durable memory; they still remain in chat history and rolling
summary. Browser Agent control is intentionally app-only. Discord does not start, resume,
stop, retry, list, mirror, or report Browser Agent jobs. The manual !screenshot utility
remains available for a one-off view of the currently open Live Browser page.

Built-in Discord commands:
- !help: show command help.
- !status: show bridge, linked chat, model/context, files/pages, and Discord-exposed active jobs.
- !profile: show the linked Zeno profile/personality snapshot.
- !stop: stop active chat generation, DeepSearch, and File Worker jobs exposed through Discord.
- !jobs / !last / !retry: inspect, fetch, or retry recent DeepSearch and file work.
- !screenshot: send the current Live Browser screenshot without controlling Browser Agent.
- !clearfiles: show uploaded-file count; !clearfiles confirm permanently removes all uploaded/input file records across Zeno and compacts the database while preserving generated outputs.
- !file <instruction>: attach/reply to a supported file, transform it, and return the finished file.
- !scramble: attach/reply to a TXT/CSV/list file (or paste lines underneath the
  command) and return a validated whole-line/proxy scramble.
- !removedupes (alias !dedupe): remove exact duplicate lines while preserving the
  first-seen order. The output is returned in Discord and saved in the linked Zeno
  browser chat as a downloadable generated file.

Disable or relink the bridge before deleting its linked Zeno chat.

PERFORMANCE DEFAULTS
--------------------
Adaptive context trimming is enabled by default. Simple chat uses a lean recent-history window, technical/continuation prompts get more history, irrelevant active pages/files are not injected, LM Studio model discovery is cached briefly, and hot database queries have indexes. Disable Adaptive context trimming in Settings only if you intentionally want the full configured recent-message window every turn.

CURRENT ROADMAP STATUS
----------------------
The current build keeps Memory 2.0 while making Screen Reader the primary Live Browser workflow. DeepSearch and File Workspace remain separate tools outside the Live Browser sidebar,
and then a unified task engine/recovery layer.

CHAT FILE REQUESTS
------------------
Zeno can attach a real generated file to chat when asked, for example:

  Shuffle every complete line in proxy1.txt and send the randomized file back.

For reliable large-file changes, use Smart File Worker because it previews,
checks, checkpoints, queues, and validates the full result before delivery.

SAVED DATA AND BACKUPS
----------------------
- memory\zeno.db: chats, settings, memories, pages, presets, queues/job history,
  DeepSearch runs, Self-Dev records, and workspace state.
- memory\chats: readable JSON chat checkpoints.
- memory\context: rolling summaries and recent context in Markdown.
- memory\long_term: readable long-term-memory copy.
- zeno_data\uploads: uploaded files.
- zeno_data\outputs: versioned generated files.
- zeno_data\file_jobs: resumable File Worker checkpoints.
- zeno_data\self_dev\backups: pre-apply Self-Dev backups.
- zeno_data\private: Discord token/configuration; keep private and never share.
- zeno_data\screenshots: webpage screenshots.
- zeno_data\browser_profile: cookies/site sessions; keep this private.

Use Memory > Save now or the top Save button for a readable checkpoint. The Exit
button can save one final checkpoint. Completed messages already committed to
memory\zeno.db remain saved if the final checkpoint is skipped. Back up memory
and zeno_data together while Zeno is closed.

UPGRADING WITHOUT LOSING DATA
-----------------------------
1. Close both Zeno versions and keep the old folder as a backup.
2. Copy the entire old memory folder and its *_data folder into the new Zeno
   folder. Do not rename anything manually and do not overwrite the old backup.
3. If the old install used one standalone .db file, place that file beside
   zeno.py. Zeno detects database contents rather than relying on its old name.
4. Start the current Zeno build. It copies the fullest earlier database to memory\zeno.db and
   merges missing persisted files into zeno_data without overwriting newer data.
5. Confirm chats, files, versions, and job history before deleting
   any old backup. Older compatible databases are upgraded automatically.

PRIVACY AND COMMON FIXES
------------------------
Zeno listens only on 127.0.0.1. Model requests go to local LM Studio; public
pages are contacted only when you ask Zeno to browse or research them.

Zeno also includes a reviewed one-time import of durable preferences from the
old Zeno memory. It merges with existing long-term memory without overwriting
entries. Completed one-off task details and anything sensitive are excluded.

- Disconnected: load the model, start LM Studio Local Server on port 1234, and
  reload Zeno.
- Python missing: install 64-bit Python 3.10+ from python.org with Add Python to
  PATH checked.
- Chromium/module missing: close Zeno and rerun INSTALL_ZENO.bat with internet.
- Model out of memory/context: lower context, close GPU-heavy apps, simplify the
  task, or use a smaller GGUF quantization.
- Browser did not open: visit http://127.0.0.1:7860 manually.
- File validation failed: inspect its counts/log, correct rules, preview again,
  or retry only when the preview is correct.
- Interrupted file job after restart: open its history card and click Resume.
- Self-Dev failed: read its validation message; no app file changed unless the
  card reached Applied after your confirmation.
- Discord says login/connection failed: stop the old Zeno process, verify the
  token and IDs, enable Message Content Intent, and confirm the bot can view and
  send messages in the selected channel.

Delete memory and zeno_data only when you intentionally want to erase all Zeno
data.
