---
name: ccfg-monitor
description: Monitor balances, usage, authentication health, and available models for local ccfg profiles; test text or image model connectivity; manually switch provider profiles or models. Use when the user asks to inspect API quota, compare providers, test a model, diagnose ccfg credentials, or change the active ccfg provider/model.
---

# Ccfg Monitor

Use `ccfg` as the single command entry point (installed to `~/.local/bin/ccfg` via `./install.sh` in the repo; resolve its real location with `command -v ccfg`). The default configuration directory is `$HOME/.codex`; respect `CCR_CONFIG_DIR` when the user supplies it (bash and the Python monitor resolve it identically). The installed entry locates its Python monitor relative to its own path, so a symlink install works unchanged. Cross-platform: Linux and macOS are fully supported (path resolution uses Python `realpath`, not BSD `readlink -f`); Windows works under WSL, or under Git Bash/MSYS2 with lock protection degrading automatically when `flock`/`fcntl` are unavailable.

## Read-only operations

- Run `ccfg status` to refresh all profiles.
- Run `ccfg status <profile>` to refresh selected profiles.
- Run `ccfg models <profile>` to inspect available models.
- Run `ccfg doctor` to diagnose configuration, file permissions, endpoint access, and stale temporary files.
- Run `ccfg test` for an interactive provider, model, and prompt connectivity test. Text is printed; generated images are saved in the current directory.
- Run `ccfg current` and `ccfg list` for local state without network requests.

Report partial failures per profile. Treat HTTP 401 or 403 as a credential or permission issue, not a plugin failure. Never print API keys or the contents of auth files. When `/v1/usage` is unavailable, `ccfg status` falls back to the OpenAI dashboard billing endpoints (`/v1/dashboard/billing/subscription` + `/v1/dashboard/billing/usage`) to compute remaining quota (`limit_usd - total_usage_cents / 100`); only when both fail does a profile show as `可用(无额度接口)`.

The status multiplier is an observed effective charge multiplier, calculated as `actual_cost / cost`. Prefer the active model's historical values; when unavailable, use the provider's weighted totals. Describe it as historical billing behavior, not a provider-declared group setting. A `余额偏低` warning in the status table means a profile's remaining/balance is below 1 unit of its currency.

For non-interactive testing, use `ccfg test --profile <profile> --model <model> --prompt <text>`. Model type is detected automatically; use `--type text` or `--type image` only when a provider uses an ambiguous model name. Do not claim that a successful usage or models request proves generation works; use `ccfg test` when generation connectivity matters.

## Maintenance

- Run `ccfg cleanup` to remove stale temporary files left by interrupted profile switches. These leftovers may contain API keys.
- Each `ccfg switch`, `ccfg <profile>`, and `ccfg use` run cleans stale temporary files automatically before switching.
- If `ccfg doctor` reports stale temporary files, run `ccfg cleanup` and re-run `ccfg doctor` to confirm.

## Platforms, groups, add, and remove

A platform is identified by its `base_url`; profiles sharing the same `base_url` are groups of the same platform (e.g. several groups all pointing at `api.deepseek.com` belong to the DeepSeek platform). `ccfg list` and `ccfg status` group by platform automatically.

- Create a new group with `ccfg add <profile>`: it prompts for `base_url` and API key. For non-interactive use, pass `--base-url`, `--api-key`, and optionally `--model`. It refuses to overwrite an existing profile and warns when the `base_url` matches an existing platform.
- Delete groups with `ccfg remove <profile> ...`: the user is asked to confirm. Deleting the active group auto-switches to the remaining group, or clears the active config when no group remains.
- Use `--base-url`/`--api-key` (never put keys in the command history of interactive shells) when automating `ccfg add`; otherwise prefer the interactive prompt, which does not echo the key.

## Mutating operations

Provider and model changes are manual. Before changing anything, state the exact target profile and model.

- Run `ccfg use <profile>` to switch provider.
- Run `ccfg use <profile> <model>` to set that profile's model and switch provider.
- Run `ccfg model <model>` to update the active profile.
- Run `ccfg model <model> --profile <profile>` to update a specific profile.

Do not implement automatic failover or choose a provider only from the largest displayed balance unless the user explicitly asks for that switch. A failed model-list request can fall back to models in usage history, but explain that those are observed models rather than a definitive availability list.
