# ccfg-monitor

Codex personal plugin and command-line extension for monitoring API balances, usage, authentication health, and available models across local `ccfg` profiles.

## Install

Requirements: `bash`, `python3` (3.8+; 3.11+ bundles `tomllib`, older versions fall back to `pip install tomli`; optional `pip install tomlkit` preserves `plugins`/`mcp_servers` fields across switches).

```bash
git clone <repo-url> ccfg-monitor
cd ccfg-monitor
./install.sh
```

What `install.sh` does:

- copies `ccfg`, `scripts/`, `skills/`, `.codex-plugin/`, and `README.md` to `$HOME/.local/share/ccfg-monitor` (override with `--prefix DIR` or `CCFG_PREFIX`);
- creates `$HOME/.local/bin/ccfg` as a symlink to the installed entry (override with `--bin-dir DIR` or `CCFG_BIN_DIR`);
- runs a smoke test against a temporary empty config directory, so your real profiles are never touched;
- `./install.sh --uninstall` removes the symlink and the install directory (it refuses to delete directories that do not look like this tool's install).

The `ccfg` entry locates `scripts/ccfg_monitor.py` relative to its own path, so symlink installation works and re-running `./install.sh` after pulling updates upgrades in place. No system-level privileges or daemons are used; configuration lives in `$HOME/.codex` (or wherever `CCR_CONFIG_DIR` points) and credentials never leave the config directory except inside `Authorization` headers.

To also use this as a Codex plugin, register the repo folder as a personal plugin in the Codex app (or run `codex plugins install <repo>` if you have the CLI); the plugin metadata lives in `.codex-plugin/plugin.json`.

## Commands

```text
ccfg status [profile ...]
ccfg models <profile>
ccfg test
ccfg use <profile> [model]
ccfg model <model> [--profile <profile>]
ccfg doctor
ccfg cleanup
ccfg add <profile> [--base-url URL] [--api-key KEY] [--model MODEL]
ccfg remove <profile> ...
ccfg list
ccfg current
```

Set `CCR_CONFIG_DIR` to use a configuration directory other than the default `$HOME/.codex`. Monitoring is refresh-on-demand; this version has no daemon, notifications, or automatic failover.

## Platforms and groups

A platform is identified by its `base_url`. Profiles sharing the same `base_url` are groups of the same platform, differing only in API key (e.g. `cc_pro`, `cc_standard`, `cc_welfare` all belong to `aicyy.xyz`). `ccfg list` and `ccfg status` group profiles by platform, and `ccfg status` shows a platform header line above each group.

`ccfg add <profile>` creates a new group: it prompts for `base_url` and API key (use `--base-url`/`--api-key` to pass them non-interactively, e.g. from scripts), generates `auth_<profile>.json` and `config_<profile>.toml`, and warns when the `base_url` already belongs to an existing platform. `ccfg remove <profile> ...` deletes groups after confirmation; deleting the active group automatically switches to the remaining group, or clears the active config when none remain.

`ccfg switch` (or `ccfg <profile>`) atomically swaps the active auth/config pair. Interrupted switches leave temporary files behind; `ccfg cleanup` removes them, and every switch automatically cleans stale ones first. These leftovers may contain API keys, so they are never kept. The runtime sections (`plugins`, `mcp_servers`) of the active config are preserved across switches; if `tomlkit` is unavailable the switch continues without preserving them and prints a warning.

The status table displays an effective multiplier calculated from historical billing data as `actual_cost / cost`. It uses the configured model when that model has statistics, otherwise it falls back to the provider's weighted aggregate. This is an observed billing ratio, not a declared control-panel group setting.

Authentication validity is derived from either endpoint succeeding: a working `/v1/models` is enough to mark a profile as usable when `/v1/usage` is missing. When `/v1/usage` is unavailable but the provider supports the standard OpenAI dashboard billing endpoints (`/v1/dashboard/billing/subscription` and `/v1/dashboard/billing/usage`), the remaining quota is computed as `limit_usd - total_usage_cents / 100` and shown as the balance (e.g. frimodel providers). Only when neither usage nor billing endpoints work does a profile display as `可用(无额度接口)`.

Credentials are read from `auth_<profile>.json` and sent only in the Authorization header. They are not stored in plugin state or included in command output. For the same reason, avoid passing API keys on the command line (`ccfg add --api-key sk-...` lands in your shell history); prefer the interactive prompt of `ccfg add <profile>` or read the key via `read -s` in scripts.

`ccfg test` interactively selects a profile and model, then sends a prompt. Text responses are printed. Image responses from `b64_json` or a signed URL are saved as `ccfg-test-<profile>-<model>-<timestamp>.<ext>` in the current directory. Use `--type image` only when automatic model-type detection cannot recognize the provider's model name.
