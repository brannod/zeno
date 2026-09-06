ZENO 3.6.14
===========

Zeno is a private local assistant with chat, files, memory, Live Browser,
Browser Agent, Screen Reader, DeepSearch, Notetaker, Discord, MCP/AYCD, and
OpenAI-compatible API provider support.

QUICK START — WINDOWS
---------------------
1. Install Python 3.11 or newer.
2. Run INSTALL_ZENO.bat once.
3. Start LM Studio's local server at http://127.0.0.1:1234, or configure an
   API provider under Settings -> API Providers.
4. Run START_ZENO.bat. Zeno opens at http://127.0.0.1:7860.

QUICK START — macOS
-------------------
1. Install Python 3.11 or newer.
2. In Terminal, open this folder and run:
     chmod +x install_macos.sh start_macos.sh INSTALL_ZENO_MAC.command START_ZENO_MAC.command
     ./install_macos.sh
3. Start LM Studio's local server at http://127.0.0.1:1234, or configure an
   API provider under Settings -> API Providers.
4. Run ./start_macos.sh, or double-click START_ZENO_MAC.command.
5. macOS may request Screen Recording permission for Desktop Notetaker. Allow
   it under System Settings -> Privacy & Security -> Screen Recording.

UPGRADING
---------
Replace the program files with the new release, but keep memory/ and zeno_data/.
Back up those folders before upgrading. The database migrates in place.

FILES
-----
- Files accepts uploads and pasted text in File Worker.
- Paste text or choose an uploaded text file, choose a preset, preview it, then
  run the approved validated job.
- List Compare accepts two pasted lists or two uploaded text files and can list
  shared duplicates or entries missing from either side.
- Outputs are written under zeno_data/outputs/ and are never silently replaced.

API PROVIDERS
-------------
Open Settings -> API Providers. Select Local, OpenRouter, QuartzRouter, or
Custom; enter the base URL, exact model ID, and API key; click Save provider,
then Test connection. Keys are stored locally under zeno_data/private/ and are
never returned to the browser.

MCP / AYCD
----------
Open Settings -> MCP Servers and enter AYCD's Streamable HTTP endpoint and
Bearer key. AYCD remains the MCP server; Zeno stores its private key locally.
Read-only tools can route from chat. Mutating tools require approval.

DISCORD
-------
Discord remains a chat interface sharing Zeno's active conversation. It can
receive attachments, return generated files, show progress, and use commands
such as !help, !status, !screenshot, !scramble, !removedupes, !job, !stop,
!cardcolon, !cardcolon5 TYPE, !comparelist, !listcompare, and !listmissing.
Prefix commands are used; slash commands are not required. Responses larger
than Discord's message limit are sent as complete UTF-8 .txt attachments.

UPDATES
-------
Settings -> Zeno Updates checks brannod/zeno, validates release ZIPs, backs up
approved program files, and leaves memory/, zeno_data/, private credentials,
uploads, outputs, and browser profiles untouched.

CHECKING INSTALLATION
---------------------
Windows: py -3 zeno.py --check
macOS/Linux: .venv/bin/python zeno.py --check

The integration check initializes modules and exits without keeping the server
open. Optional features report as unavailable when their dependency is absent.
