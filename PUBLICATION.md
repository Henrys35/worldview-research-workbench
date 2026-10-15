# Public source copy

This independent repository preserves the original `scout` CLI, viral.app discovery,
browser search/login/probe, public-page evidence collection, analysis validation,
reports, CSV output and Feishu delivery. It does not include browser sessions,
API credentials, collected research or the source Git history.

The default configuration directory is `~/.config/worldview-scout`; existing users
can point `SCOUT_CONFIG_DIR` and `VIRAL_CONFIG_PATH` at their own locations.
`SCOUT_CONFIG_DIR` controls Feishu configuration, while `VIRAL_CONFIG_PATH` controls
the viral.app credential file. The browser keeps its separate profile at
`~/.config/worldview-scout/research/chrome-profile`; log in to that profile yourself.
Tests use local synthetic payloads and memory clients. No paid discovery,
live TikTok session or real Feishu write is performed for this release.

## Rights

The source tree contains no project-wide license file. This publication does not
add or change a license. Public visibility is not an open-source license grant.
Pydantic, PyYAML and optional Playwright are installed as external dependencies;
their source, browser binaries and model caches are not redistributed here.
All existing source notices are retained. Supply your own authorized accounts,
credentials and research material when using the external services.
