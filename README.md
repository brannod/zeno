# Zeno

**Zeno** is a local AI work assistant built for a standalone desktop/RDP workflow with shared chat, Discord support, memory, browser tools, screen reading, file handling, DeepSearch, and LM Studio integration.

Zeno is designed to keep your work in one place while giving a local model access to useful tools, persistent memory, shared browser context, and remote Discord chat.

## Highlights

- Local AI through **LM Studio**
- Shared Zeno chat across the main app, Live Browser, and Discord
- Persistent memory and context tools
- Live Browser + **Screen Reader** for reading and analyzing rendered pages
- File handling for common document/data formats
- DeepSearch and web research tools
- Discord chat, status, file, context, and utility commands
- GitHub Releases updater with local backup before install
- Built for long-running Windows/RDP use

## Uncensored / Local Model Friendly

Zeno is designed to work with **uncensored or minimally restricted local language models** when you choose to run them through LM Studio. Zeno itself does not force a specific cloud model or provider, so the model behavior is largely determined by the local model and configuration you choose.

## Source, Releases, and Updates

The repository is intended to hold **public application source only**. Runtime/private data stays on the machine running Zeno.

The release pipeline is designed around a simple `VERSION` file:

1. Source changes are committed to `main`.
2. When a stable build is ready, update `VERSION` to a new value such as `v2.7.18`.
3. GitHub Actions validates the core Python source, creates a clean Zeno updater ZIP, creates the matching Git tag/release, and marks it as the latest release.
4. Zeno can then use **Check → Download → Install + backup** in its updater and restart into the new version.

A separate source-sync workflow can import the current clean GitHub release into the repository. It copies only a strict allowlist of application files and never imports local databases, browser profiles, chat history, uploads, or real Discord credentials.

## Privacy

Zeno is intended to run locally. Keep private configuration files, Discord bot tokens, memory databases, browser profiles, uploads, and personal chat history out of public GitHub releases.

The repository includes `DISCORD_TOKEN.example.txt` only. Your real local configuration should be named `DISCORD_TOKEN.txt`, which is ignored by Git.

> Zeno is an independent personal project and is not affiliated with Discord, LM Studio, or the websites it can browse.
