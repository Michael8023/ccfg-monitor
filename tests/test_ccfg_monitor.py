import importlib.util
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "ccfg_monitor.py"
SPEC = importlib.util.spec_from_file_location("ccfg_monitor", SCRIPT)
ccfg = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = ccfg
SPEC.loader.exec_module(ccfg)


class CcfgMonitorTests(unittest.TestCase):
    def test_endpoint_url_normalizes_v1(self):
        self.assertEqual(
            ccfg.endpoint_url("https://example.test/v1", "usage"),
            "https://example.test/v1/usage",
        )
        self.assertEqual(
            ccfg.endpoint_url("https://example.test/", "models"),
            "https://example.test/v1/models",
        )

    def test_models_prefer_models_endpoint(self):
        models, source = ccfg.available_models(
            {"data": [{"id": "gpt-new"}]},
            {"model_stats": [{"model": "gpt-old"}]},
        )
        self.assertEqual(models, ["gpt-new"])
        self.assertEqual(source, "/v1/models")

    def test_models_fall_back_to_usage_history(self):
        models, source = ccfg.available_models(
            None, {"model_stats": [{"model": "gpt-used"}]}
        )
        self.assertEqual(models, ["gpt-used"])
        self.assertEqual(source, "usage历史")

    def test_effective_multiplier_prefers_current_model(self):
        stats = [
            {"model": "current", "cost": 10, "actual_cost": 2},
            {"model": "other", "cost": 100, "actual_cost": 90},
        ]
        value, source = ccfg.effective_multiplier(stats, "current")
        self.assertAlmostEqual(value, 0.2)
        self.assertEqual(source, "current_model")

    def test_effective_multiplier_uses_weighted_fallback(self):
        stats = [
            {"model": "a", "cost": 10, "actual_cost": 1},
            {"model": "b", "cost": 30, "actual_cost": 9},
            {"model": "ignored", "cost": 0, "actual_cost": 2},
        ]
        value, source = ccfg.effective_multiplier(stats, "missing")
        self.assertAlmostEqual(value, 0.25)
        self.assertEqual(source, "weighted_total")

    def test_effective_multiplier_without_history(self):
        self.assertEqual(ccfg.effective_multiplier([], "model"), (None, None))

    def test_status_table_contains_multiplier_and_handles_cjk_width(self):
        rows = [{
            "profile": "测试平台",
            "status": "正常",
            "auth_valid": True,
            "remaining": 5.0,
            "balance": 5.0,
            "unit": "USD",
            "today_cost": 0.1,
            "today_requests": 2,
            "usage_latency_ms": 123,
            "effective_multiplier": 0.25,
            "current_model": "gpt-test",
            "errors": [],
        }]
        output = StringIO()
        with redirect_stdout(output):
            ccfg.print_status(rows, "测试平台")
        rendered = output.getvalue()
        self.assertIn("倍率", rendered)
        self.assertIn("0.250x", rendered)
        self.assertIn("* 测试平台", rendered)
        self.assertNotIn("\033[", rendered)

    def test_status_table_uses_compact_layout_on_narrow_terminal(self):
        rows = [{
            "profile": "cc_standard",
            "status": "正常",
            "auth_valid": True,
            "remaining": 4.0,
            "balance": 4.0,
            "unit": "USD",
            "today_cost": 0.1,
            "today_requests": 3,
            "usage_latency_ms": 99,
            "effective_multiplier": 0.09,
            "current_model": "gpt-5.6-sol",
            "errors": [],
        }]
        output = StringIO()
        with mock.patch.object(ccfg.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))):
            with redirect_stdout(output):
                ccfg.print_status(rows, "cc_standard")
        rendered = output.getvalue()
        self.assertNotIn("今日消耗", rendered)
        self.assertNotIn("请求", rendered)
        table_lines = [line for line in rendered.splitlines() if line.startswith(("┌", "│", "├", "└"))]
        self.assertTrue(all(ccfg.display_width(line) <= 80 for line in table_lines))

    def test_redacts_keys_and_bearer_tokens(self):
        secret = "sk-this-is-a-secret-value"
        message = f"failed {secret}; Authorization: Bearer {secret}"
        redacted = ccfg.redact(message, [secret])
        self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_extracts_responses_and_chat_text(self):
        self.assertEqual(
            ccfg.extract_text_response(
                {"output": [{"content": [{"type": "output_text", "text": "hello"}]}]}
            ),
            "hello",
        )
        self.assertEqual(
            ccfg.extract_text_response({"choices": [{"message": {"content": "world"}}]}),
            "world",
        )

    def test_text_test_falls_back_to_chat_completions(self):
        profile = ccfg.Profile(
            "demo", Path("auth.json"), Path("config.toml"),
            "https://example.test/v1", "demo-model", "test-key"
        )
        with mock.patch.object(
            ccfg,
            "post_json",
            side_effect=[
                ccfg.ApiRequestError("接口不存在", 404),
                ({"choices": [{"message": {"content": "ok"}}]}, 17),
            ],
        ):
            output, endpoint, latency = ccfg.run_text_test(profile, "demo-model", "ping", 3)
        self.assertEqual((output, endpoint, latency), ("ok", "/v1/chat/completions", 17))

    def test_image_detection_and_base64_save(self):
        self.assertTrue(ccfg.is_image_model("gpt-image-1"))
        self.assertTrue(ccfg.is_image_model("custom", {"output_modalities": ["image"]}))
        self.assertFalse(ccfg.is_image_model("gpt-text"))
        png = b"\x89PNG\r\n\x1a\n" + b"test-image"
        profile = ccfg.Profile(
            "demo", Path("auth.json"), Path("config.toml"),
            "https://example.test/v1", "gpt-image-1", "test-key"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = ccfg.extract_and_save_image(
                {"data": [{"b64_json": base64.b64encode(png).decode()}]},
                Path(temp_dir), profile, "gpt-image-1", 3,
            )
            self.assertEqual(path.suffix, ".png")
            self.assertEqual(path.read_bytes(), png)

    def test_set_model_preserves_toml_and_updates_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "auth_demo.json").write_text(
                json.dumps({"OPENAI_API_KEY": "test-key"}), encoding="utf-8"
            )
            config = (
                'model = "old-model"\n'
                'model_provider = "custom"\n\n'
                '[model_providers.custom]\n'
                'base_url = "https://example.test/v1"\n'
            )
            (root / "config_demo.toml").write_text(config, encoding="utf-8")
            active_config = (
                config
                + '\n[plugins."ccfg-monitor@personal"]\nenabled = true\n'
                + '\n[mcp_servers.docs]\nurl = "https://example.test/mcp"\n'
            )
            (root / "config.toml").write_text(active_config, encoding="utf-8")
            (root / ".active-profile").write_text("demo\n", encoding="utf-8")

            ccfg.set_model(root, "demo", "new-model")

            self.assertIn('model = "new-model"', (root / "config_demo.toml").read_text())
            active_text = (root / "config.toml").read_text()
            self.assertIn('model = "new-model"', active_text)
            self.assertIn('[plugins."ccfg-monitor@personal"]', active_text)
            self.assertIn("[mcp_servers.docs]", active_text)
            self.assertNotIn("[plugins", (root / "config_demo.toml").read_text())

    def test_ccfg_use_sets_model_and_switches_in_temp_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            auth = json.dumps({"OPENAI_API_KEY": "test-key"})
            config = (
                'model = "old-model"\n'
                'model_provider = "custom"\n\n'
                '[model_providers.custom]\n'
                'base_url = "https://example.test/v1"\n'
            )
            old_active = (
                config
                + '\n[plugins."ccfg-monitor@personal"]\nenabled = true\n'
                + '\n[mcp_servers.docs]\nurl = "https://example.test/mcp"\n'
            )
            (root / "auth_demo.json").write_text(auth, encoding="utf-8")
            (root / "config_demo.toml").write_text(config, encoding="utf-8")
            (root / "auth.json").write_text(auth, encoding="utf-8")
            (root / "config.toml").write_text(old_active, encoding="utf-8")
            (root / ".active-profile").write_text("demo\n", encoding="utf-8")
            env = os.environ.copy()
            env["CCR_CONFIG_DIR"] = str(root)
            subprocess.run(
                ["/home/zqliu/.local/bin/ccfg", "use", "demo", "new-model"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual((root / ".active-profile").read_text().strip(), "demo")
            self.assertEqual((root / "auth.json").read_text(), auth)
            active_text = (root / "config.toml").read_text()
            self.assertIn('model = "new-model"', active_text)
            self.assertIn('[plugins."ccfg-monitor@personal"]', active_text)
            self.assertIn("[mcp_servers.docs]", active_text)


    def test_query_profile_usage_404_models_ok_is_valid(self):
        # 平台无 /v1/usage 接口（404）但 /v1/models 成功：
        # 应视为可用（无额度接口），而非"接口不存在"。
        profile = ccfg.Profile(
            "demo", Path("auth.json"), Path("config.toml"),
            "https://provider.test/v1", "gpt-5.6-sol", "test-key"
        )
        with mock.patch.object(
            ccfg, "request_json",
            side_effect=[
                ccfg.ApiResult(False, 404, None, 100, "接口不存在"),
                ccfg.ApiResult(True, 200, {"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.5"}]}, 50, None),
            ],
        ):
            row = ccfg.query_profile(profile, 8)
        self.assertTrue(row["auth_valid"])
        self.assertTrue(row["usage_ok"] is False)
        self.assertIn("无额度接口", row["status"])
        self.assertEqual(row["models"], ["gpt-5.5", "gpt-5.6-sol"])
        self.assertIn("usage: 接口不存在", row["errors"])

    def test_query_profile_both_fail_is_invalid(self):
        profile = ccfg.Profile(
            "demo", Path("auth.json"), Path("config.toml"),
            "https://example.test/v1", "m", "test-key"
        )
        with mock.patch.object(
            ccfg, "request_json",
            side_effect=[
                ccfg.ApiResult(False, 401, None, 100, "密钥无效或无权限"),
                ccfg.ApiResult(False, 401, None, 100, "密钥无效或无权限"),
            ],
        ):
            row = ccfg.query_profile(profile, 8)
        self.assertFalse(row["auth_valid"])
        self.assertIn("密钥无效或无权限", row["status"])

    def test_models_command_ignores_usage_warning(self):
        # models 命令不应因 usage 失败而输出警告
        row = {
            "profile": "demo",
            "host": "provider.test",
            "current_model": "gpt-5.6-sol",
            "model_source": "/v1/models",
            "models": ["gpt-5.5", "gpt-5.6-sol"],
            "model_stats": [],
            "errors": ["usage: 接口不存在"],
        }
        args = argparse.Namespace(config_dir=Path("."), profile="demo", timeout=8, json=False)
        stderr = StringIO()
        with mock.patch.object(ccfg, "query_many", return_value=[row]):
            with redirect_stdout(StringIO()):
                with mock.patch("sys.stderr", stderr):
                    code = ccfg.cmd_models(args)
        self.assertEqual(code, 0)
        self.assertNotIn("usage", stderr.getvalue())

    def test_host_of_groups_by_base_url(self):
        self.assertEqual(ccfg.host_of("https://api.deepseek.com/v1"), "api.deepseek.com")
        self.assertEqual(ccfg.host_of("https://api.openai.com"), "api.openai.com")

    def test_profile_platforms_groups_same_base_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("cc_pro", "cc_standard", "cc_welfare", "baiz"):
                (root / f"auth_{name}.json").write_text(
                    json.dumps({"OPENAI_API_KEY": "test-key"}), encoding="utf-8"
                )
                base = "https://api.deepseek.com" if name.startswith("cc_") else "https://api.openai.com/v1"
                (root / f"config_{name}.toml").write_text(
                    f'model = "m"\nmodel_provider = "p"\n\n[model_providers.p]\nbase_url = "{base}"\n',
                    encoding="utf-8",
                )
            groups = ccfg.profile_platforms(root, ["cc_pro", "cc_standard", "cc_welfare", "baiz"])
            self.assertEqual(set(groups.keys()), {"api.deepseek.com", "api.openai.com"})
            self.assertEqual(len(groups["api.deepseek.com"]), 3)
            self.assertEqual(len(groups["api.openai.com"]), 1)

    def test_valid_base_url_rejects_bad_url(self):
        with self.assertRaises(ccfg.CcfgError):
            ccfg.valid_base_url("not-a-url")
        self.assertEqual(ccfg.valid_base_url(" https://api.deepseek.com/ "), "https://api.deepseek.com")

    def test_cmd_add_creates_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = argparse.Namespace(
                config_dir=root,
                profile="newkey",
                base_url="https://api.example.com/v1",
                api_key="sk-new-key",
                model="gpt-5.6-sol",
            )
            with mock.patch("builtins.input", side_effect=EOFError("no input")):
                code = ccfg.cmd_add(args)
            self.assertEqual(code, 0)
            auth = json.loads((root / "auth_newkey.json").read_text())
            self.assertEqual(auth["OPENAI_API_KEY"], "sk-new-key")
            config_text = (root / "config_newkey.toml").read_text()
            self.assertIn('base_url = "https://api.example.com/v1"', config_text)
            self.assertIn('model = "gpt-5.6-sol"', config_text)
            self.assertEqual((root / "auth_newkey.json").stat().st_mode & 0o077, 0)

    def test_cmd_add_requires_unique_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "auth_demo.json").write_text("{}", encoding="utf-8")
            (root / "config_demo.toml").write_text("model = \"m\"\n", encoding="utf-8")
            args = argparse.Namespace(
                config_dir=root,
                profile="demo",
                base_url="https://api.example.com/v1",
                api_key="sk-new-key",
                model="gpt-5.6-sol",
            )
            with self.assertRaises(ccfg.CcfgError):
                ccfg.cmd_add(args)

    def test_cmd_remove_deletes_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("cc_pro", "cc_standard"):
                (root / f"auth_{name}.json").write_text(
                    json.dumps({"OPENAI_API_KEY": "k"}), encoding="utf-8"
                )
                (root / f"config_{name}.toml").write_text(
                    'model = "m"\nmodel_provider = "p"\n\n[model_providers.p]\nbase_url = "https://api.deepseek.com"\n',
                    encoding="utf-8",
                )
            (root / ".active-profile").write_text("cc_standard\n", encoding="utf-8")
            # 活动分组 cc_standard 被删 -> 自动切换到 cc_pro
            args = argparse.Namespace(config_dir=root, profiles=["cc_standard"])
            with mock.patch("builtins.input", return_value="y"):
                code = ccfg.cmd_remove(args)
            self.assertEqual(code, 0)
            self.assertFalse((root / "auth_cc_standard.json").exists())
            self.assertFalse((root / "config_cc_standard.toml").exists())
            self.assertTrue((root / "auth_cc_pro.json").exists())
            self.assertEqual((root / ".active-profile").read_text().strip(), "cc_pro")
            self.assertEqual((root / "auth.json").read_text().strip(), '{"OPENAI_API_KEY": "k"}')

    def test_cmd_remove_cancelled_keeps_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "auth_demo.json").write_text("{}", encoding="utf-8")
            (root / "config_demo.toml").write_text(
                'model = "m"\nmodel_provider = "p"\n\n[model_providers.p]\nbase_url = "https://api.deepseek.com"\n',
                encoding="utf-8",
            )
            args = argparse.Namespace(config_dir=root, profiles=["demo"])
            with mock.patch("builtins.input", return_value="n"):
                code = ccfg.cmd_remove(args)
            self.assertEqual(code, 1)
            self.assertTrue((root / "auth_demo.json").exists())

    def test_cmd_list_groups_by_platform(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, base in (
                ("cc_pro", "https://api.deepseek.com"),
                ("cc_welfare", "https://api.deepseek.com"),
                ("xtoken", "https://api.openai.com"),
            ):
                (root / f"auth_{name}.json").write_text(
                    json.dumps({"OPENAI_API_KEY": "k"}), encoding="utf-8"
                )
                (root / f"config_{name}.toml").write_text(
                    f'model = "m"\nmodel_provider = "p"\n\n[model_providers.p]\nbase_url = "{base}"\n',
                    encoding="utf-8",
                )
            (root / ".active-profile").write_text("cc_welfare\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                ccfg.cmd_list(argparse.Namespace(config_dir=root))
            rendered = output.getvalue()
            self.assertIn("api.deepseek.com（2 个分组）", rendered)
            self.assertIn("api.openai.com（1 个分组）", rendered)
            self.assertIn("可用 *", rendered)

    def test_cmd_list_empty_config_guides_user(self):
        """配置为空时 list 应给出可操作的引导，而不是只报一句没找到配置。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = ccfg.cmd_list(argparse.Namespace(config_dir=root))
            rendered = output.getvalue() + errors.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("ccfg add", rendered)
            self.assertIn("还没有任何分组", rendered)
            self.assertIn(str(root), rendered)

    def test_cmd_status_empty_config_guides_user(self):
        """配置为空时 status 应返回 1 并提示如何创建分组。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = ccfg.cmd_status(
                    argparse.Namespace(profiles=[], config_dir=root, timeout=1.0, json=False)
                )
            rendered = output.getvalue() + errors.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("ccfg add", rendered)
            self.assertIn("CCR_CONFIG_DIR", rendered)

    def test_require_toml_raises_actionable_error_when_missing(self):
        """缺少 TOML 解析库时应报出可操作的安装命令，而不是 ImportError 崩溃。"""
        with mock.patch.object(ccfg, "tomllib", None):
            with self.assertRaises(ccfg.CcfgError) as ctx:
                ccfg.require_toml()
        message = str(ctx.exception)
        self.assertIn("pip install --user tomli", message)
        self.assertIn("tomlkit", message)

    def test_require_toml_passes_when_available(self):
        self.assertIsNone(ccfg.require_toml())

    def test_load_profile_reports_missing_toml_library(self):
        """无 TOML 库时 load_profile 应给出安装提示，而非 TypeError。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "auth_demo.json").write_text(
                json.dumps({"OPENAI_API_KEY": "k"}), encoding="utf-8"
            )
            (root / "config_demo.toml").write_text('model = "m"\n', encoding="utf-8")
            with mock.patch.object(ccfg, "tomllib", None):
                with self.assertRaises(ccfg.CcfgError) as ctx:
                    ccfg.load_profile(root, "demo")
            self.assertIn("pip install --user tomli", str(ctx.exception))

    def test_status_table_shows_platform_groups(self):
        rows = [
            {
                "profile": "cc_pro", "host": "api.deepseek.com", "status": "正常",
                "auth_valid": True, "remaining": 5.0, "balance": 5.0, "unit": "USD",
                "today_cost": 0.1, "today_requests": 1, "usage_latency_ms": 10,
                "effective_multiplier": 0.25, "current_model": "gpt-test", "errors": [],
            },
            {
                "profile": "cc_welfare", "host": "api.deepseek.com", "status": "正常",
                "auth_valid": True, "remaining": 5.0, "balance": 5.0, "unit": "USD",
                "today_cost": 0.1, "today_requests": 1, "usage_latency_ms": 10,
                "effective_multiplier": 0.25, "current_model": "gpt-test", "errors": [],
            },
            {
                "profile": "xtoken", "host": "api.openai.com", "status": "正常",
                "auth_valid": True, "remaining": 5.0, "balance": 5.0, "unit": "USD",
                "today_cost": 0.1, "today_requests": 1, "usage_latency_ms": 10,
                "effective_multiplier": 0.25, "current_model": "gpt-test", "errors": [],
            },
        ]
        output = StringIO()
        with redirect_stdout(output):
            ccfg.print_status(rows, "cc_welfare")
        rendered = output.getvalue()
        self.assertIn("平台：api.deepseek.com（2 个分组）", rendered)
        self.assertIn("平台：api.openai.com（1 个分组）", rendered)
        self.assertIn("2 个平台", rendered)

    def test_query_dashboard_balance_calculates_remaining(self):
        profile = ccfg.Profile(
            "demo", Path("auth.json"), Path("config.toml"),
            "https://provider.test/v1", "gpt-5.6-sol", "test-key"
        )
        with mock.patch.object(
            ccfg, "request_json",
            side_effect=[
                ccfg.ApiResult(True, 200, {"hard_limit_usd": 403.26, "soft_limit_usd": 403.26}, 10, None),
                ccfg.ApiResult(True, 200, {"total_usage": 30633.899}, 10, None),
            ],
        ):
            result = ccfg.query_dashboard_balance(profile, 8)
        self.assertAlmostEqual(result[0], 96.921)
        self.assertEqual(result[1], "USD")

    def test_query_dashboard_balance_none_on_failure(self):
        profile = ccfg.Profile(
            "demo", Path("auth.json"), Path("config.toml"),
            "https://example.test/v1", "m", "test-key"
        )
        with mock.patch.object(
            ccfg, "request_json",
            side_effect=[
                ccfg.ApiResult(False, 403, None, 10, "密钥无效或无权限"),
                ccfg.ApiResult(False, 403, None, 10, "密钥无效或无权限"),
            ],
        ):
            self.assertIsNone(ccfg.query_dashboard_balance(profile, 8))

    def test_query_profile_falls_back_to_dashboard_billing(self):
        # usage 404 + models 200 + dashboard billing 可用
        # -> 状态正常，余额来自 dashboard billing
        profile = ccfg.Profile(
            "demo", Path("auth.json"), Path("config.toml"),
            "https://provider.test/v1", "gpt-5.6-sol", "test-key"
        )
        results = [
            ccfg.ApiResult(False, 404, None, 100, "接口不存在"),
            ccfg.ApiResult(True, 200, {"data": [{"id": "gpt-5.6-sol"}]}, 50, None),
            ccfg.ApiResult(True, 200, {"hard_limit_usd": 100.0}, 10, None),
            ccfg.ApiResult(True, 200, {"total_usage": 1000.0}, 10, None),
        ]
        with mock.patch.object(ccfg, "request_json", side_effect=results):
            row = ccfg.query_profile(profile, 8)
        self.assertTrue(row["auth_valid"])
        self.assertTrue(row["billing_fallback"])
        self.assertEqual(row["status"], "正常")
        self.assertEqual(row["remaining"], 90.0)
        self.assertEqual(row["unit"], "USD")

    def test_query_profile_no_billing_fallback_when_usage_ok(self):
        # usage 正常时不触发 billing 降级
        profile = ccfg.Profile(
            "demo", Path("auth.json"), Path("config.toml"),
            "https://example.test/v1", "m", "test-key"
        )
        results = [
            ccfg.ApiResult(True, 200, {"isValid": True, "balance": 5.0, "unit": "USD"}, 10, None),
            ccfg.ApiResult(True, 200, {"data": [{"id": "m"}]}, 10, None),
        ]
        with mock.patch.object(ccfg, "request_json", side_effect=results):
            row = ccfg.query_profile(profile, 8)
        self.assertTrue(row["auth_valid"])
        self.assertFalse(row["billing_fallback"])
        self.assertEqual(row["balance"], 5.0)

    def test_stale_tmp_files_detected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".auth.json.tmp.STALE").write_text(
                '{"OPENAI_API_KEY": "sk-secret"}', encoding="utf-8"
            )
            (root / ".config.toml.tmp.STALE").write_text("model = \"x\"\n", encoding="utf-8")
            (root / ".active-profile.tmp.STALE").write_text("p\n", encoding="utf-8")
            (root / "auth_demo.json").write_text("{}", encoding="utf-8")
            stale = ccfg.stale_tmp_files(root)
            self.assertEqual(len(stale), 3)
            names = {path.name for path in stale}
            self.assertEqual(
                names,
                {".auth.json.tmp.STALE", ".config.toml.tmp.STALE", ".active-profile.tmp.STALE"},
            )

    def test_stale_tmp_files_empty_when_clean(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "auth_demo.json").write_text("{}", encoding="utf-8")
            self.assertEqual(ccfg.stale_tmp_files(root), [])

    def test_status_warns_low_balance(self):
        rows = [
            {
                "profile": "low",
                "status": "正常",
                "auth_valid": True,
                "remaining": 0.3,
                "balance": 0.3,
                "unit": "USD",
                "today_cost": 0.1,
                "today_requests": 2,
                "usage_latency_ms": 10,
                "effective_multiplier": 0.25,
                "current_model": "gpt-test",
                "errors": [],
            },
            {
                "profile": "rich",
                "status": "正常",
                "auth_valid": True,
                "remaining": 5.0,
                "balance": 5.0,
                "unit": "USD",
                "today_cost": 0.1,
                "today_requests": 2,
                "usage_latency_ms": 10,
                "effective_multiplier": 0.25,
                "current_model": "gpt-test",
                "errors": [],
            },
        ]
        output = StringIO()
        with redirect_stdout(output):
            ccfg.print_status(rows, "low")
        rendered = output.getvalue()
        warn_line = next(line for line in rendered.splitlines() if "余额偏低" in line)
        self.assertIn("low", warn_line)
        self.assertNotIn("rich", warn_line)

    def test_doctor_reports_stale_tmp_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "auth_demo.json").write_text(
                json.dumps({"OPENAI_API_KEY": "test-key"}), encoding="utf-8"
            )
            (root / "config_demo.toml").write_text(
                'model = "m"\nmodel_provider = "p"\n\n[model_providers.p]\n'
                'base_url = "https://example.test/v1"\n',
                encoding="utf-8",
            )
            (root / ".auth.json.tmp.STALE").write_text("{}", encoding="utf-8")
            args = argparse.Namespace(config_dir=root, timeout=3)
            with mock.patch.object(ccfg, "query_many", return_value=[]):
                code = ccfg.cmd_doctor(args)
            self.assertNotEqual(code, 0)  # 残留临时文件应使 doctor 返回非零


if __name__ == "__main__":
    unittest.main()
