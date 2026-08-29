# Zeno v3.4.1

This branch stages the Zeno v3.4.1 upgrade line for the current modular Zeno build.

## Highlights

- Discord Browser Agent using the same persistent Chromium session as the GUI
- `/screen` to send the current Live Browser screenshot to Discord
- Persistent reminders with `/remind`, `/reminders`, and `/cancelreminder`
- Memory Optimizer with preview/apply modes and conservative high-confidence duplicate merging
- Task Router v1 for explicit automatic routing to Browser Agent, reminders, file search, and memory optimization
- All-active-file deterministic search improvements
- `/commands`, `/ask`, `/search`, `/summarize`
- Larger Live Browser workspace
- Full-screen / minimize / close-view / Stop Chromium separation
- Faster bounded live-screen refresh
- GUI clickability hardening

## Important compatibility note

The public `main` branch before this release still contains the older 2.7.x monolithic source. The v3.4.1 package is an **upgrade pack for the newer modular Zeno build**, so this branch intentionally does not pretend that the old 2.7.x source is the complete v3.4.1 application.

Install the included `Zeno_v3.4.1_Browser_Agent_Automation_Pack.zip` into the current modular Zeno folder and run `INSTALL_ZENO_V3.4.1.bat`.

## New Discord commands

- `/browser`
- `/browserstatus`
- `/browserstop`
- `/browserresume`
- `/screen`
- `/remind`
- `/reminders`
- `/cancelreminder`
- `/memoryoptimize`
- `/commands`
- `/ask`
- `/search`
- `/summarize`

## Safety / behavior

Browser Agent keeps its existing safe-action boundaries. The Memory Optimizer previews by default and does not delete unique memories simply because they are old. Reminders are persisted to disk and survive Zeno restarts.
