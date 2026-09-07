# ccfg-monitor

[![GitHub stars](https://img.shields.io/github/stars/Michael8023/ccfg-monitor)](https://github.com/Michael8023/ccfg-monitor)
[![GitHub last commit](https://img.shields.io/github/last-commit/Michael8023/ccfg-monitor)](https://github.com/Michael8023/ccfg-monitor)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)

监控本地 `ccfg` 各平台（分组）的 API 余额、用量、认证健康与可用模型；测试文本/图像模型连通性；手动切换活动平台与模型。既可独立作为命令行工具使用，也可作为 Codex 个人插件。

## 特性

- **一键刷新**：全部平台的余额、今日消耗、请求数、延迟与可用模型
- **平台自动分组**：相同 `base_url` 的分组归为同一平台（例如多个分组都指向 `api.deepseek.com`，即同属 DeepSeek 平台），仅 API key 不同
- **余额回退链**：`/v1/usage` 缺失时，回退到 OpenAI dashboard billing 接口计算剩余额度
- **连通性测试**：文本模型直接打印回复；图像模型保存为本地图片
- **原子切换**：写入临时文件并校验后整体替换，中断不残留半成品配置；保留活动配置的 `plugins` / `mcp_servers` 字段
- **安全**：认证文件 600 权限，密钥仅用于 `Authorization` 请求头，绝不写入插件状态或命令输出；自动清理可能含密钥的残留临时文件

## 安装

**要求**：`bash`、`python3`（3.8+；3.11+ 自带 `tomllib`，旧版本请 `pip install tomli`；可选 `pip install tomlkit`，用于切换时保留 `plugins` / `mcp_servers` 字段）。

```bash
git clone https://github.com/Michael8023/ccfg-monitor.git
cd ccfg-monitor
./install.sh
```

安装内容：

- 核心文件复制到 `~/.local/share/ccfg-monitor`（可用 `--prefix DIR` 或 `CCFG_PREFIX` 覆盖）
- 在 `~/.local/bin` 创建 `ccfg` 软链接（可用 `--bin-dir DIR` 或 `CCFG_BIN_DIR` 覆盖）
- 冒烟测试使用临时空配置目录，**不会触碰你的真实配置**
- 卸载：`./install.sh --uninstall`（只删除软链接与安装目录，保留 `~/.codex` 下的配置与分组）

**更新**：`git pull` 后重新运行 `./install.sh` 即可原地升级。

> 作为 Codex 插件使用：在 Codex 应用中把本仓库注册为个人插件（或运行 `codex plugins install <仓库路径>`）。插件元数据位于 `.codex-plugin/plugin.json`。

## 快速开始

```bash
ccfg list                                     # 按平台分组列出所有分组，* 标记当前活动分组
ccfg status                                   # 刷新全部平台的额度与可用模型
ccfg test                                     # 交互式测试模型连通性（文本打印 / 图片存当前目录）
ccfg use cc_pro                               # 切换活动平台
ccfg use cc_pro gpt-5.6-sol                   # 切换平台并设置模型
ccfg model gpt-5.6-sol --profile cc_standard  # 只修改某个分组的模型
ccfg doctor                                   # 诊断配置、权限、接口可达性与残留临时文件
ccfg cleanup                                  # 清理中断切换残留的临时文件（可能含密钥）
```

`ccfg status` 输出示例：

```text
ccfg API 状态
刷新时间 2026-09-07 10:59:44  |  当前平台 cc_pro
┌────────────────────┬──────────┬──────────────┬────────┬────...
│ 平台               │ 状态     │ 余额         │ 倍率   │ 模型
│ 平台：api.deepseek.com（4 个分组）
│   cc_pro           │ 正常     │      12.34 USD │      - │ gpt-5.6-sol
└────────────────────┴──────────┴──────────────┴────────┴────...
可用 4/4  |  1 个平台  |  倍率 = actual_cost / cost
```

## 命令一览

| 命令 | 说明 |
|---|---|
| `ccfg list` / `ls` | 按平台分组列出所有分组 |
| `ccfg add <分组> [--base-url URL] [--api-key KEY] [--model M]` | 新建分组（同 base_url 归入同一平台） |
| `ccfg remove <分组> ...` | 删除分组（需确认；删除活动分组自动切换） |
| `ccfg current` | 显示当前活动分组 |
| `ccfg status [分组 ...]` | 刷新额度/用量/模型（按需执行，无后台服务） |
| `ccfg models <分组>` | 查看可用模型（优先 `/v1/models`，失败回退 usage 历史） |
| `ccfg test` | 交互式连通性测试 |
| `ccfg use <分组> [模型]` | 切换平台（可同时设置模型） |
| `ccfg switch <分组>` | 仅切换平台 |
| `ccfg model <模型> [--profile <分组>]` | 修改指定分组的模型 |
| `ccfg doctor` | 诊断配置与接口 |
| `ccfg cleanup` | 清理残留临时文件 |
| `ccfg help` | 查看完整帮助 |

## 平台与分组

平台以 `base_url` 识别：共享同一 `base_url` 的分组属于同一平台、仅 API key 不同。`ccfg list` / `ccfg status` 自动按平台分组展示。

```bash
# 交互式新增（推荐：密钥不会回显、不进 shell 历史）
ccfg add my_group

# 非交互式（注意：--api-key 会留在 shell 历史中，仅限脚本内使用）
ccfg add my_group --base-url https://api.example.com/v1 --api-key sk-xxx

# 删除分组（需确认；删除活动分组后自动切换到剩余分组）
ccfg remove my_group
```

## 工作机制

- **按需刷新**：`status` 仅在执行时请求，不运行守护进程，无自动通知/故障转移。
- **倍率**：状态表显示有效计费倍率 = `actual_cost / cost`（历史计费行为，非面板声明值）。
- **余额回退链**：优先 `/v1/usage`；不可用时回退 `GET /v1/dashboard/billing/subscription` + `GET /v1/dashboard/billing/usage`，按 `limit_usd - total_usage_cents / 100` 计算剩余额度；两者都失败才显示 `可用(无额度接口)`。认证有效性由 `/v1/models` 或 `/v1/usage` 任一成功判定。
- **切换原子性**：临时文件写入并校验（JSON/TOML）成功后整体 `mv` 替换，`flock` 防止并发切换；运行时字段（`plugins` / `mcp_servers`）从活动配置保留到目标配置。
- **当前平台识别**：优先 `.active-profile` 标记 + auth/config 比对；auth 一致但 config.toml 被 Codex 或手动改动时，仍报告该分组并注明差异。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CCR_CONFIG_DIR` | `$HOME/.codex` | 配置目录（存放 `auth_*.json` / `config_*.toml`） |
| `CCFG_PREFIX` | `$HOME/.local/share/ccfg-monitor` | 安装目录（`install.sh`） |
| `CCFG_BIN_DIR` | `$HOME/.local/bin` | `ccfg` 软链接目录（`install.sh`） |
| `CCFG_MONITOR_SCRIPT` | 自动定位 | 覆盖监控脚本路径 |

## 安全说明

- 认证文件以 600 权限保存在配置目录，密钥仅用于 `Authorization` 请求头，不写入插件状态、不包含在命令输出中。
- 交互式 `ccfg add` 不回显密钥；脚本自动化请用 `read -s` 读取密钥，避免 `--api-key` 进入 shell 历史。
- 切换中断的残留临时文件可能包含密钥，`cleanup` 与每次切换都会自动清理。

## 开发与测试

```bash
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE)
