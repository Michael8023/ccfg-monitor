#!/usr/bin/env python3
"""Monitor and update local ccfg profiles without exposing credentials."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import copy
import fcntl
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_DIR = Path.home() / ".codex"
KEY_FIELD = "OPENAI_API_KEY"
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
LOW_BALANCE_THRESHOLD = 1.0
STALE_TMP_PATTERNS = (".auth.json.tmp.*", ".config.toml.tmp.*", ".active-profile.tmp.*")

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python <3.11 回退到 tomli
    import tomli as tomllib  # type: ignore[no-redef]


class CcfgError(RuntimeError):
    pass


class ApiRequestError(CcfgError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Profile:
    name: str
    auth_path: Path
    config_path: Path
    base_url: str
    model: str
    api_key: str


@dataclass(frozen=True)
class ApiResult:
    ok: bool
    status: int | None
    data: dict[str, Any] | None
    latency_ms: int
    error: str | None


def config_dir_from_env() -> Path:
    return Path(os.environ.get("CCR_CONFIG_DIR", str(DEFAULT_CONFIG_DIR))).expanduser()


def redact(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", result)
    result = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", result)
    return result


def endpoint_url(base_url: str, resource: str) -> str:
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CcfgError(f"无效 base_url：{base_url!r}")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = f"{path}/{resource}"
    else:
        path = f"{path}/v1/{resource}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def load_profile(config_dir: Path, name: str) -> Profile:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise CcfgError(f"非法平台名称：{name}")
    auth_path = config_dir / f"auth_{name}.json"
    config_path = config_dir / f"config_{name}.toml"
    if not auth_path.is_file():
        raise CcfgError(f"缺少认证配置：{auth_path}")
    if not config_path.is_file():
        raise CcfgError(f"缺少程序配置：{config_path}")
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        provider_name = config.get("model_provider")
        provider = config.get("model_providers", {}).get(provider_name, {})
        base_url = provider.get("base_url")
        model = config.get("model")
        api_key = auth.get(KEY_FIELD)
    except (OSError, ValueError, TypeError) as exc:
        raise CcfgError(f"无法解析 {name} 配置：{exc}") from exc
    if not isinstance(base_url, str) or not base_url:
        raise CcfgError(f"{name} 缺少 model provider 的 base_url")
    if not isinstance(model, str) or not model:
        raise CcfgError(f"{name} 缺少顶层 model")
    if not isinstance(api_key, str) or not api_key:
        raise CcfgError(f"{name} 的 {KEY_FIELD} 为空")
    return Profile(name, auth_path, config_path, base_url, model, api_key)


def discover_profile_names(config_dir: Path) -> list[str]:
    names: list[str] = []
    for auth_path in sorted(config_dir.glob("auth_*.json")):
        name = auth_path.stem.removeprefix("auth_")
        if (config_dir / f"config_{name}.toml").is_file():
            names.append(name)
    return names


def host_of(base_url: str) -> str:
    """提取 base_url 的主机名作为平台标识。

    同一 base_url 归属同一平台，仅 auth key 不同 => 同一平台的不同分组。
    """
    return urllib.parse.urlsplit(base_url).netloc


def load_profile_optional(config_dir: Path, name: str) -> tuple[Profile | None, str]:
    """加载分组配置；失败时返回 (None, 错误信息)。"""
    try:
        return load_profile(config_dir, name), ""
    except CcfgError as exc:
        return None, str(exc)


def profile_platforms(config_dir: Path, names: list[str]) -> dict[str, list[tuple[str, str, str]]]:
    """按平台（host）分组：{host: [(name, model, status), ...]}。"""
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for name in names:
        profile, error = load_profile_optional(config_dir, name)
        if profile is None:
            groups.setdefault("(无效配置)", []).append((name, "-", error))
            continue
        host = host_of(profile.base_url)
        groups.setdefault(host, []).append((name, profile.model, "可用"))
    return groups


NEW_CONFIG_TEMPLATE = """\
model_provider = "mycodex"
model = "{model}"
review_model = "gpt-5.4"
model_reasoning_effort = "medium"
disable_response_storage = true
network_access = "enabled"
windows_wsl_setup_acknowledged = true
model_context_window = 1000000
model_auto_compact_token_limit = 900000

[model_providers.mycodex]
name = "mycodex"
base_url = "{base_url}"
wire_api = "responses"
requires_openai_auth = true
"""

DEFAULT_MODEL = "gpt-5.6-sol"


def valid_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CcfgError(f"无效 base_url：{value!r}")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def request_json(url: str, api_key: str, timeout: float) -> ApiResult:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "ccfg-monitor/0.1",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2 * 1024 * 1024)
            status_code = response.status
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError("顶层 JSON 不是对象")
        return ApiResult(True, status_code, parsed, elapsed_ms(started), None)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            message = "密钥无效或无权限"
        elif exc.code == 404:
            message = "接口不存在"
        else:
            message = f"HTTP {exc.code}"
        return ApiResult(False, exc.code, None, elapsed_ms(started), message)
    except urllib.error.URLError as exc:
        reason = redact(str(exc.reason), [api_key])
        return ApiResult(False, None, None, elapsed_ms(started), f"连接失败：{reason}")
    except (TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return ApiResult(False, None, None, elapsed_ms(started), redact(str(exc), [api_key]))


def post_json(url: str, api_key: str, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], int]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ccfg-monitor/0.2",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read(64 * 1024 * 1024)
        data = json.loads(response_body)
        if not isinstance(data, dict):
            raise ValueError("顶层 JSON 不是对象")
        return data, elapsed_ms(started)
    except urllib.error.HTTPError as exc:
        raw = exc.read(64 * 1024)
        detail = ""
        with contextlib.suppress(Exception):
            error_data = json.loads(raw)
            error_value = error_data.get("error", error_data)
            if isinstance(error_value, dict):
                detail = str(error_value.get("message") or error_value.get("detail") or "")
            elif isinstance(error_value, str):
                detail = error_value
        if exc.code in {401, 403}:
            message = "密钥无效或无权限"
        elif exc.code == 429:
            message = "请求频率或额度受限"
        else:
            message = f"HTTP {exc.code}"
        if detail:
            message = f"{message}：{redact(detail[:500], [api_key])}"
        raise ApiRequestError(message, exc.code) from exc
    except urllib.error.URLError as exc:
        raise ApiRequestError(f"连接失败：{redact(str(exc.reason), [api_key])}") from exc
    except (TimeoutError, json.JSONDecodeError, ValueError) as exc:
        raise ApiRequestError(redact(str(exc), [api_key])) from exc


def elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def available_models(models_data: dict[str, Any] | None, usage_data: dict[str, Any] | None) -> tuple[list[str], str]:
    models: list[str] = []
    if models_data:
        for item in models_data.get("data", []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                models.append(item["id"])
        if models:
            return sorted(set(models)), "/v1/models"
    if usage_data:
        for item in usage_data.get("model_stats", []):
            if isinstance(item, dict) and isinstance(item.get("model"), str):
                models.append(item["model"])
    return sorted(set(models)), "usage历史" if models else "不可用"


def effective_multiplier(model_stats: Any, current_model: str) -> tuple[float | None, str | None]:
    if not isinstance(model_stats, list):
        return None, None
    valid: list[tuple[dict[str, Any], float, float]] = []
    for item in model_stats:
        if not isinstance(item, dict):
            continue
        cost = item.get("cost")
        actual_cost = item.get("actual_cost")
        if (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and cost > 0
            and isinstance(actual_cost, (int, float))
            and not isinstance(actual_cost, bool)
        ):
            valid.append((item, float(cost), float(actual_cost)))
    for item, cost, actual_cost in valid:
        if item.get("model") == current_model:
            return actual_cost / cost, "current_model"
    total_cost = sum(cost for _, cost, _ in valid)
    if total_cost > 0:
        return sum(actual for _, _, actual in valid) / total_cost, "weighted_total"
    return None, None


def query_dashboard_balance(profile: Profile, timeout: float) -> tuple[float, str] | None:
    """通过 OpenAI dashboard billing 接口查询剩余额度。

    部分平台不提供 /v1/usage，但兼容标准 billing 接口：
      GET /v1/dashboard/billing/subscription  -> hard/soft_limit_usd（美元）
      GET /v1/dashboard/billing/usage         -> total_usage（美分）
    剩余额度 = limit_usd - total_usage / 100。
    任一接口失败或字段缺失时返回 None。
    """
    try:
        sub = request_json(
            endpoint_url(profile.base_url, "dashboard/billing/subscription"),
            profile.api_key,
            timeout,
        )
        if not sub.ok or not sub.data:
            return None
        limit = sub.data.get("hard_limit_usd") or sub.data.get("soft_limit_usd")
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            return None
        usage = request_json(
            endpoint_url(profile.base_url, "dashboard/billing/usage"),
            profile.api_key,
            timeout,
        )
        if not usage.ok or not usage.data:
            return None
        total_cents = usage.data.get("total_usage")
        if not isinstance(total_cents, (int, float)) or isinstance(total_cents, bool):
            return None
        remaining = float(limit) - float(total_cents) / 100.0
        return round(remaining, 4), "USD"
    except Exception:
        return None


def is_image_model(model: str, metadata: dict[str, Any] | None = None) -> bool:
    image_terms = ("image", "dall-e", "dalle", "imagen", "flux", "sdxl", "stable-diffusion")
    if any(term in model.lower() for term in image_terms):
        return True
    if not metadata:
        return False
    capability_values = []
    for key in ("type", "capability", "capabilities", "modalities", "output_modalities"):
        value = metadata.get(key)
        if isinstance(value, str):
            capability_values.append(value)
        elif isinstance(value, (list, tuple)):
            capability_values.extend(str(item) for item in value)
        elif isinstance(value, dict):
            capability_values.extend(str(item) for item in value.keys())
    return any("image" in value.lower() for value in capability_values)


def extract_text_response(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    for output in data.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                if content["text"].strip():
                    return content["text"].strip()
    for choice in data.get("choices", []):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
            if parts:
                return "\n".join(parts).strip()
    raise CcfgError("接口返回成功，但没有找到文本内容")


def image_extension(data: bytes, content_type: str | None = None) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    content_extensions = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return content_extensions.get((content_type or "").split(";", 1)[0].lower(), ".img")


def save_image_bytes(data: bytes, output_dir: Path, profile: str, model: str, content_type: str | None = None) -> Path:
    if not data:
        raise CcfgError("图片响应为空")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-._") or "model"
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    extension = image_extension(data, content_type)
    base_name = f"ccfg-test-{profile}-{safe_model}-{timestamp}"
    for index in range(1000):
        suffix = "" if index == 0 else f"-{index}"
        path = output_dir / f"{base_name}{suffix}{extension}"
        try:
            with path.open("xb") as handle:
                handle.write(data)
            return path.resolve()
        except FileExistsError:
            continue
    raise CcfgError("无法生成不重复的图片文件名")


def extract_and_save_image(data: dict[str, Any], output_dir: Path, profile: Profile, model: str, timeout: float) -> Path:
    items = data.get("data")
    if not isinstance(items, list):
        items = data.get("images")
    if not isinstance(items, list) or not items:
        raise CcfgError("接口返回成功，但没有找到图片数据")
    item = items[0]
    if not isinstance(item, dict):
        raise CcfgError("图片响应格式无法识别")
    encoded = item.get("b64_json") or item.get("base64")
    if isinstance(encoded, str):
        try:
            image_data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise CcfgError("图片 base64 数据无效") from exc
        return save_image_bytes(image_data, output_dir, profile.name, model)
    image_url = item.get("url")
    if not isinstance(image_url, str):
        raise CcfgError("图片响应中没有 b64_json 或 url")
    parsed = urllib.parse.urlsplit(image_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CcfgError("图片下载地址无效")
    # Signed image URLs must not receive the provider API key.
    request = urllib.request.Request(image_url, headers={"User-Agent": "ccfg-monitor/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            image_data = response.read(64 * 1024 * 1024)
            content_type = response.headers.get("Content-Type")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CcfgError(f"图片下载失败：{exc}") from exc
    return save_image_bytes(image_data, output_dir, profile.name, model, content_type)


def select_option(label: str, options: list[str], default: str | None = None) -> str:
    if not options:
        raise CcfgError(f"没有可选择的{label}")
    ordered = list(dict.fromkeys(options))
    if default in ordered:
        ordered.remove(default)
        ordered.insert(0, default)
    print(f"\n请选择{label}：")
    for index, option in enumerate(ordered, 1):
        marker = "（当前）" if option == default else ""
        print(f"  {index}. {option}{marker}")
    prompt = "请输入序号"
    if default in ordered:
        prompt += " [1]"
    while True:
        try:
            answer = input(f"{prompt}：").strip()
        except EOFError as exc:
            raise CcfgError(f"无法读取{label}选择") from exc
        if not answer and default in ordered:
            return ordered[0]
        if answer.isdigit() and 1 <= int(answer) <= len(ordered):
            return ordered[int(answer) - 1]
        print(f"请输入 1-{len(ordered)} 之间的序号。")


def models_for_test(profile: Profile, timeout: float) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    models_result = request_json(endpoint_url(profile.base_url, "models"), profile.api_key, timeout)
    metadata: dict[str, dict[str, Any]] = {}
    if models_result.ok and models_result.data:
        for item in models_result.data.get("data", []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                metadata[item["id"]] = item
        if metadata:
            models = sorted(metadata)
            if profile.model not in models:
                models.append(profile.model)
            return models, metadata, "/v1/models"
    usage_result = request_json(endpoint_url(profile.base_url, "usage"), profile.api_key, timeout)
    usage_data = usage_result.data if usage_result.ok else None
    models, source = available_models(None, usage_data)
    if profile.model not in models:
        models.append(profile.model)
    if source == "不可用":
        source = "当前配置"
    return sorted(set(models)), metadata, source


def run_text_test(profile: Profile, model: str, prompt: str, timeout: float) -> tuple[str, str, int]:
    responses_url = endpoint_url(profile.base_url, "responses")
    try:
        data, latency = post_json(
            responses_url,
            profile.api_key,
            {"model": model, "input": prompt, "stream": False},
            timeout,
        )
        return extract_text_response(data), "/v1/responses", latency
    except ApiRequestError as responses_error:
        if responses_error.status not in {400, 404, 405, 501}:
            raise
        chat_url = endpoint_url(profile.base_url, "chat/completions")
        try:
            data, latency = post_json(
                chat_url,
                profile.api_key,
                {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
                timeout,
            )
            return extract_text_response(data), "/v1/chat/completions", latency
        except ApiRequestError as chat_error:
            raise CcfgError(
                f"Responses API 失败（{responses_error}）；Chat Completions 也失败（{chat_error}）"
            ) from chat_error


def run_image_test(profile: Profile, model: str, prompt: str, timeout: float, output_dir: Path) -> tuple[Path, int]:
    data, latency = post_json(
        endpoint_url(profile.base_url, "images/generations"),
        profile.api_key,
        {"model": model, "prompt": prompt, "n": 1},
        timeout,
    )
    return extract_and_save_image(data, output_dir, profile, model, timeout), latency


def query_profile(profile: Profile, timeout: float) -> dict[str, Any]:
    usage_url = endpoint_url(profile.base_url, "usage")
    models_url = endpoint_url(profile.base_url, "models")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        usage_future = pool.submit(request_json, usage_url, profile.api_key, timeout)
        models_future = pool.submit(request_json, models_url, profile.api_key, timeout)
        usage = usage_future.result()
        models_result = models_future.result()
    usage_data = usage.data if usage.ok else None
    model_data = models_result.data if models_result.ok else None
    models, model_source = available_models(model_data, usage_data)
    today = ((usage_data or {}).get("usage") or {}).get("today") or {}
    total = ((usage_data or {}).get("usage") or {}).get("total") or {}
    host = urllib.parse.urlsplit(profile.base_url).netloc
    errors = []
    if not usage.ok:
        errors.append(f"usage: {usage.error}")
    if not models_result.ok:
        errors.append(f"models: {models_result.error}")
    valid = (usage_data or {}).get("isValid")
    model_stats = (usage_data or {}).get("model_stats", [])
    multiplier, multiplier_source = effective_multiplier(model_stats, profile.model)

    # 认证有效性：usage 有效或 models 成功任一成立即可。
    # 部分平台只提供 /v1/models 而没有 /v1/usage，
    # 此时 models 成功即证明密钥有效、平台可用，不应判为不可用。
    usage_ok = bool(usage.ok and valid is not False)
    models_ok = bool(models_result.ok)
    auth_valid = usage_ok or models_ok

    # usage 接口不可用时，回退到 OpenAI dashboard billing 接口获取余额
    balance = (usage_data or {}).get("balance")
    remaining = (usage_data or {}).get("remaining")
    unit = (usage_data or {}).get("unit")
    billing_fallback = False
    if not usage_ok and (balance is None and remaining is None):
        fallback = query_dashboard_balance(profile, timeout)
        if fallback is not None:
            remaining, unit = fallback
            balance = remaining
            billing_fallback = True
            errors.append("usage: 接口不存在（已回退到 dashboard billing 获取余额）")

    if usage_ok or billing_fallback:
        status = "正常"
    elif models_ok:
        status = "可用(无额度接口)"
    else:
        status = usage.error or models_result.error or "无效"

    return {
        "profile": profile.name,
        "host": host,
        "current_model": profile.model,
        "status": status,
        "auth_valid": auth_valid,
        "usage_ok": usage_ok,
        "billing_fallback": billing_fallback,
        "balance": balance,
        "remaining": remaining,
        "unit": unit,
        "mode": (usage_data or {}).get("mode"),
        "today_cost": today.get("actual_cost", today.get("cost")),
        "today_requests": today.get("requests"),
        "total_cost": total.get("actual_cost", total.get("cost")),
        "effective_multiplier": multiplier,
        "multiplier_source": multiplier_source,
        "models": models,
        "model_source": model_source,
        "usage_latency_ms": usage.latency_ms,
        "models_latency_ms": models_result.latency_ms,
        "errors": errors,
        "model_stats": model_stats,
    }


def active_profile(config_dir: Path) -> str | None:
    marker = config_dir / ".active-profile"
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def stale_tmp_files(config_dir: Path) -> list[Path]:
    """返回配置目录中残留的中断切换临时文件。"""
    stale: list[Path] = []
    for pattern in STALE_TMP_PATTERNS:
        for path in config_dir.glob(pattern):
            stale.append(path)
    return sorted(stale)


def fmt_money(value: Any, unit: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    suffix = f" {unit}" if unit else ""
    return f"{value:.4f}{suffix}"


def fmt_multiplier(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    if value >= 10:
        return f"{value:.2f}x"
    return f"{value:.3f}x"


def display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def truncate_display(value: str, width: int) -> str:
    if display_width(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    result = ""
    used = 0
    for char in value:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > width - 1:
            break
        result += char
        used += char_width
    return result + "…"


def pad_display(value: str, width: int, align: str = "left") -> str:
    value = truncate_display(value, width)
    padding = max(0, width - display_width(value))
    return (" " * padding + value) if align == "right" else (value + " " * padding)


def supports_color() -> bool:
    return bool(
        sys.stdout.isatty()
        and os.environ.get("NO_COLOR") is None
        and os.environ.get("TERM", "") != "dumb"
    )


def ansi(value: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{value}\033[0m" if enabled else value


def print_status(rows: list[dict[str, Any]], active: str | None) -> None:
    color = supports_color()
    terminal_width = shutil.get_terminal_size((128, 30)).columns
    if terminal_width < 116:
        model_width = max(10, min(30, terminal_width - 74))
        columns = [
            ("profile", "平台", 12, "left"),
            ("status", "状态", 10, "left"),
            ("balance", "余额", 11, "right"),
            ("multiplier", "倍率", 7, "right"),
            ("latency", "延迟", 7, "right"),
            ("model", "模型", model_width, "left"),
        ]
    else:
        model_width = max(12, min(30, terminal_width - 111))
        columns = [
            ("profile", "平台", 18, "left"),
            ("status", "状态", 16, "left"),
            ("balance", "余额", 15, "right"),
            ("multiplier", "倍率", 8, "right"),
            ("today", "今日消耗", 13, "right"),
            ("requests", "请求", 7, "right"),
            ("latency", "延迟", 9, "right"),
            ("model", "模型", model_width, "left"),
        ]
    top = "┌" + "┬".join("─" * (width + 2) for _, _, width, _ in columns) + "┐"
    middle = "├" + "┼".join("─" * (width + 2) for _, _, width, _ in columns) + "┤"
    bottom = "└" + "┴".join("─" * (width + 2) for _, _, width, _ in columns) + "┘"

    print(ansi("ccfg API 状态", "1;36", color))
    print(ansi(f"刷新时间 {time.strftime('%Y-%m-%d %H:%M:%S')}  |  当前平台 {active or '未知'}", "2", color))

    # 同一 base_url 属于同一平台：按平台（host）分组显示，组间插入平台标签行
    ordered = sorted(rows, key=lambda row: (row.get("host") or "", row.get("profile") or ""))
    host_counts: dict[str, int] = {}
    for row in ordered:
        host = str(row.get("host") or "未知")
        host_counts[host] = host_counts.get(host, 0) + 1
    total_width = len(top)

    print(top)
    header_cells = [f" {pad_display(title, width)} " for _, title, width, _ in columns]
    print("│" + "│".join(ansi(cell, "1", color) for cell in header_cells) + "│")
    print(middle)

    valid_count = 0
    prev_host: str | None = None
    first_group = True
    for row in ordered:
        host = str(row.get("host") or "未知")
        if host != prev_host and prev_host is not None:
            print(middle)
        if host != prev_host:
            label = f"平台：{host}（{host_counts[host]} 个分组）"
            group_line = "│" + pad_display(f" {label} ", total_width - 2) + "│"
            print(ansi(group_line, "1;36", color))
            first_group = False
            prev_host = host
        is_active = row.get("profile") == active
        is_valid = bool(row.get("auth_valid"))
        if is_valid:
            valid_count += 1
        errors = row.get("errors") or []
        status = str(row.get("status", "未知"))
        # 只有 models 请求失败才覆盖为"模型异常"；
        # usage 不可用（无额度接口）不影响模型状态。
        if is_valid and any(e.startswith("models:") for e in errors):
            status = "正常/模型异常"
        amount = row.get("remaining") if row.get("remaining") is not None else row.get("balance")
        unit = row.get("unit")
        values = {
            "profile": f"* {row.get('profile', '-')}" if is_active else f"  {row.get('profile', '-')}",
            "status": status,
            "balance": fmt_money(amount, unit),
            "multiplier": fmt_multiplier(row.get("effective_multiplier")),
            "today": fmt_money(row.get("today_cost"), unit),
            "requests": str(row.get("today_requests") if row.get("today_requests") is not None else "-"),
            "latency": f"{row.get('usage_latency_ms', 0)}ms",
            "model": str(row.get("current_model", "-")),
        }
        cells = []
        for key, _, width, align in columns:
            cell = f" {pad_display(values[key], width, align)} "
            if key == "profile" and is_active:
                cell = ansi(cell, "1;36", color)
            elif key == "status":
                cell = ansi(cell, "32" if is_valid else "31", color)
            elif key == "multiplier" and row.get("effective_multiplier") is not None:
                cell = ansi(cell, "36", color)
            cells.append(cell)
        print("│" + "│".join(cells) + "│")
    print(bottom)

    summary = f"可用 {valid_count}/{len(rows)}  |  {len(host_counts)} 个平台  |  倍率 = actual_cost / cost"
    print(ansi(summary, "2", color))
    low: list[str] = []
    for row in rows:
        amount = row.get("remaining") if row.get("remaining") is not None else row.get("balance")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount < LOW_BALANCE_THRESHOLD:
            low.append(str(row.get("profile", "-")))
    if low:
        print(ansi(
            f"余额偏低（< {LOW_BALANCE_THRESHOLD:g}）：{', '.join(low)}",
            "33",
            color,
        ))
    if any(row.get("effective_multiplier") is None for row in rows):
        print(ansi("提示：'-' 表示没有足够的历史计费数据。", "2", color))


def query_many(config_dir: Path, names: list[str], timeout: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    profiles: list[Profile] = []
    for name in names:
        try:
            profiles.append(load_profile(config_dir, name))
        except CcfgError as exc:
            rows.append({
                "profile": name,
                "host": "-",
                "current_model": "-",
                "status": str(exc),
                "auth_valid": False,
                "usage_ok": False,
                "billing_fallback": False,
                "remaining": None,
                "balance": None,
                "unit": None,
                "today_cost": None,
                "today_requests": None,
                "usage_latency_ms": 0,
                "effective_multiplier": None,
                "multiplier_source": None,
                "models": [],
                "model_source": "不可用",
                "errors": [str(exc)],
                "model_stats": [],
            })
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(profiles)))) as pool:
        future_map = {pool.submit(query_profile, profile, timeout): profile for profile in profiles}
        for future in concurrent.futures.as_completed(future_map):
            profile = future_map[future]
            try:
                rows.append(future.result())
            except Exception as exc:  # Keep one provider failure from aborting the refresh.
                rows.append({
                    "profile": profile.name,
                    "host": urllib.parse.urlsplit(profile.base_url).netloc,
                    "current_model": profile.model,
                    "status": redact(str(exc), [profile.api_key]),
                    "auth_valid": False,
                    "usage_ok": False,
                    "billing_fallback": False,
                    "remaining": None,
                    "balance": None,
                    "unit": None,
                    "today_cost": None,
                    "today_requests": None,
                    "usage_latency_ms": 0,
                    "effective_multiplier": None,
                    "multiplier_source": None,
                    "models": [],
                    "model_source": "不可用",
                    "errors": [redact(str(exc), [profile.api_key])],
                    "model_stats": [],
                })
    return sorted(rows, key=lambda row: row["profile"])


def atomic_write(path: Path, content: str, mode: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def set_model(config_dir: Path, profile_name: str, model: str) -> None:
    if not MODEL_RE.fullmatch(model):
        raise CcfgError(f"非法模型名称：{model!r}")
    profile = load_profile(config_dir, profile_name)
    try:
        import tomlkit
    except ImportError as exc:
        raise CcfgError("修改模型需要 Python 包 tomlkit") from exc
    lock_path = config_dir / ".config-switch.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        source_text = profile.config_path.read_text(encoding="utf-8")
        document = tomlkit.parse(source_text)
        document["model"] = model
        rendered = tomlkit.dumps(document)
        tomllib.loads(rendered)
        profile_mode = stat.S_IMODE(profile.config_path.stat().st_mode)
        atomic_write(profile.config_path, rendered, profile_mode)
        if active_profile(config_dir) == profile_name:
            active_config = config_dir / "config.toml"
            active_mode = stat.S_IMODE(active_config.stat().st_mode) if active_config.exists() else 0o600
            if active_config.exists():
                active_document = tomlkit.parse(active_config.read_text(encoding="utf-8"))
                active_document["model"] = model
                active_rendered = tomlkit.dumps(active_document)
                tomllib.loads(active_rendered)
            else:
                active_rendered = rendered
            atomic_write(active_config, active_rendered, active_mode)


def cmd_status(args: argparse.Namespace) -> int:
    names = args.profiles or discover_profile_names(args.config_dir)
    if not names:
        raise CcfgError("没有找到完整的平台配置")
    rows = query_many(args.config_dir, names, args.timeout)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_status(rows, active_profile(args.config_dir))
    return 0 if all(row.get("auth_valid") for row in rows) else 2


def cmd_models(args: argparse.Namespace) -> int:
    row = query_many(args.config_dir, [args.profile], args.timeout)[0]
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0 if row.get("models") else 2
    print(f'平台：{row["profile"]}')
    print(f'接口：{row["host"]}')
    print(f'当前模型：{row["current_model"]}')
    print(f'模型来源：{row["model_source"]}')
    if row["models"]:
        stats = {item.get("model"): item for item in row.get("model_stats", []) if isinstance(item, dict)}
        print("\n可用模型：")
        for model in row["models"]:
            marker = " *" if model == row["current_model"] else ""
            requests = stats.get(model, {}).get("requests")
            detail = f"，历史请求 {requests}" if requests is not None else ""
            print(f"  {model}{marker}{detail}")
    else:
        print("\n未能获取模型列表。")
    # models 命令只关心模型接口；usage 失败（如平台无额度接口）不在此列出
    for error in row.get("errors", []):
        if error.startswith("models:"):
            print(f"警告：{error}", file=sys.stderr)
    return 0 if row["models"] else 2


def cmd_model(args: argparse.Namespace) -> int:
    profile = args.profile or active_profile(args.config_dir)
    if not profile:
        raise CcfgError("无法确定当前平台，请使用 --profile 指定")
    set_model(args.config_dir, profile, args.model)
    print(f"模型修改成功：{profile} -> {args.model}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    names = discover_profile_names(args.config_dir)
    if not names:
        raise CcfgError("没有找到完整的平台配置")
    current = active_profile(args.config_dir)
    if args.profile:
        if args.profile not in names:
            raise CcfgError(f"平台不存在：{args.profile}")
        profile_name = args.profile
    else:
        profile_name = select_option("平台", names, current)
    profile = load_profile(args.config_dir, profile_name)

    models, metadata, source = models_for_test(profile, args.timeout)
    if args.model:
        if not MODEL_RE.fullmatch(args.model):
            raise CcfgError(f"非法模型名称：{args.model!r}")
        model = args.model
    else:
        model = select_option("模型", models, profile.model)

    if args.prompt is None:
        try:
            prompt = input("\n请输入测试内容：").strip()
        except EOFError as exc:
            raise CcfgError("无法读取测试内容") from exc
    else:
        prompt = args.prompt.strip()
    if not prompt:
        raise CcfgError("测试内容不能为空")

    detected_kind = "image" if is_image_model(model, metadata.get(model)) else "text"
    kind = detected_kind if args.kind == "auto" else args.kind
    print(f"\n平台：{profile.name}")
    print(f"模型：{model}")
    print(f"模型来源：{source}")
    print(f"测试类型：{'图片' if kind == 'image' else '文本'}")
    print("正在请求，请稍候...")

    if kind == "image":
        output_path, latency = run_image_test(profile, model, prompt, args.timeout, args.output_dir)
        print(f"\n测试成功，耗时 {latency}ms")
        print(f"图片已保存：{output_path}")
    else:
        output, endpoint, latency = run_text_test(profile, model, prompt, args.timeout)
        print(f"\n测试成功：{endpoint}，耗时 {latency}ms")
        print("\n模型返回：")
        print(output)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    names = discover_profile_names(args.config_dir)
    if not names:
        raise CcfgError("没有找到完整的平台配置")
    issues = 0
    print(f"配置目录：{args.config_dir}")
    print(f"当前平台：{active_profile(args.config_dir) or '未知'}")
    stale = stale_tmp_files(args.config_dir)
    if stale:
        print(f"残留临时文件：{len(stale)} 个（可能含有密钥，建议运行 ccfg cleanup 清理）")
        issues += 1
    for name in names:
        try:
            profile = load_profile(args.config_dir, name)
            auth_mode = stat.S_IMODE(profile.auth_path.stat().st_mode)
            permission = "安全" if auth_mode & 0o077 == 0 else f"建议改为 600（当前 {auth_mode:o}）"
            print(f"{name}: 配置有效，认证文件权限 {permission}")
            if auth_mode & 0o077:
                issues += 1
        except CcfgError as exc:
            print(f"{name}: {exc}")
            issues += 1
    print("\n接口检查：")
    rows = query_many(args.config_dir, names, args.timeout)
    for row in rows:
        models = len(row.get("models", []))
        print(f'{row["profile"]}: {row["status"]}，模型 {models} 个，usage {row["usage_latency_ms"]}ms')
        if not row.get("auth_valid"):
            issues += 1
    return 0 if issues == 0 else 2


def switch_to_active(config_dir: Path, name: str) -> None:
    """将指定分组设为活动配置（复制 auth/config + 保留运行时字段）。"""
    profile = load_profile(config_dir, name)
    auth_text = profile.auth_path.read_text(encoding="utf-8")
    config_text = profile.config_path.read_text(encoding="utf-8")
    active_auth = config_dir / "auth.json"
    active_config = config_dir / "config.toml"
    marker = config_dir / ".active-profile"

    rendered = config_text
    if active_config.exists():
        try:
            import tomlkit
            active = tomlkit.parse(active_config.read_text(encoding="utf-8"))
            target = tomlkit.parse(config_text)
            changed = False
            for key in ("plugins", "mcp_servers"):
                if key in active:
                    target[key] = copy.deepcopy(active[key])
                    changed = True
            if changed:
                rendered = tomlkit.dumps(target)
        except ImportError:
            print("警告：未找到 tomlkit，无法保留运行时配置。", file=sys.stderr)
        except Exception as exc:
            print(f"警告：合并运行时配置失败，将使用原始配置：{exc}", file=sys.stderr)

    atomic_write(active_auth, auth_text, 0o600)
    atomic_write(active_config, rendered, 0o600)
    atomic_write(marker, name + "\n", 0o600)


def cmd_add(args: argparse.Namespace) -> int:
    name = args.profile
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise CcfgError(f"非法平台名称：{name}")
    auth_path = args.config_dir / f"auth_{name}.json"
    config_path = args.config_dir / f"config_{name}.toml"
    if auth_path.exists() or config_path.exists():
        raise CcfgError(f"分组已存在：{name}")

    base_url = (args.base_url or "").strip()
    if not base_url:
        try:
            base_url = input("请输入 base_url（如 https://api.example.com/v1）：").strip()
        except EOFError as exc:
            raise CcfgError("无法读取 base_url") from exc
    base_url = valid_base_url(base_url)

    api_key = (args.api_key or "").strip()
    if not api_key:
        try:
            import getpass
            api_key = getpass.getpass("请输入 API Key：").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise CcfgError("无法读取 API Key") from exc
    if not api_key:
        raise CcfgError("API Key 不能为空")

    model = (args.model or "").strip() or DEFAULT_MODEL
    if args.model is None:
        try:
            answer = input(f"模型（默认 {DEFAULT_MODEL}）：").strip()
        except EOFError:
            answer = ""
        model = answer or DEFAULT_MODEL
    if not MODEL_RE.fullmatch(model):
        raise CcfgError(f"非法模型名称：{model!r}")

    existing = discover_profile_names(args.config_dir)
    groups = profile_platforms(args.config_dir, existing)
    host = host_of(base_url)
    if host in groups:
        members = ", ".join(n for n, _, _ in groups[host])
        print(f"提示：{host} 已存在分组：{members}，{name} 将归入同一平台。")

    config_text = NEW_CONFIG_TEMPLATE.format(model=model, base_url=base_url)
    auth_text = json.dumps({"OPENAI_API_KEY": api_key}, ensure_ascii=False, indent=2) + "\n"

    atomic_write(auth_path, auth_text, 0o600)
    atomic_write(config_path, config_text, 0o600)
    print(f"已创建分组：{name}")
    print(f"  base_url: {base_url}")
    print(f"  model: {model}")
    print("提示：可运行 ccfg use <分组名> 切换使用，或 ccfg test 验证连通性。")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    names = list(dict.fromkeys(args.profiles))
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise CcfgError(f"非法平台名称：{name}")
    missing = [
        n for n in names
        if not (args.config_dir / f"auth_{n}.json").exists()
        and not (args.config_dir / f"config_{n}.toml").exists()
    ]
    if missing:
        raise CcfgError(f"分组不存在：{', '.join(missing)}")

    groups = profile_platforms(args.config_dir, discover_profile_names(args.config_dir))
    print("将删除以下分组：")
    for name in names:
        host = next((h for h, items in groups.items() if any(n == name for n, _, _ in items)), "?")
        print(f"  {name}（平台 {host}）")
    try:
        answer = input("确认删除？[y/N]：").strip().lower()
    except EOFError as exc:
        raise CcfgError("无法读取确认") from exc
    if answer not in {"y", "yes"}:
        print("已取消。")
        return 1

    lock_path = args.config_dir / ".config-switch.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in names:
            (args.config_dir / f"auth_{name}.json").unlink(missing_ok=True)
            (args.config_dir / f"config_{name}.toml").unlink(missing_ok=True)
            print(f"已删除：{name}")

        current = active_profile(args.config_dir)
        if current in names:
            remaining = [n for n in discover_profile_names(args.config_dir) if n not in names]
            if remaining:
                next_name = sorted(remaining)[0]
                switch_to_active(args.config_dir, next_name)
                print(f"已删除当前活动分组 {current}，已自动切换到：{next_name}")
            else:
                for stale in ("auth.json", "config.toml", ".active-profile"):
                    (args.config_dir / stale).unlink(missing_ok=True)
                print("已无剩余分组，活动配置已清除。")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    names = discover_profile_names(args.config_dir)
    current = active_profile(args.config_dir)
    if not names:
        print("没有找到完整的平台配置。")
        return 0
    groups = profile_platforms(args.config_dir, names)
    print(f"当前活动分组：{current or '未知'}")
    for host in sorted(groups):
        items = groups[host]
        print(f"\n平台 {host}（{len(items)} 个分组）")
        for name, model, status in items:
            marker = " *" if name == current else ""
            print(f"  {name:<20}{model:<22}{status}{marker}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监控 ccfg API 额度、模型并修改当前模型")
    parser.add_argument("--config-dir", type=Path, default=config_dir_from_env())
    parser.add_argument("--timeout", type=float, default=8.0, help="单次请求超时秒数（默认 8）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="刷新额度和接口状态")
    status_parser.add_argument("profiles", nargs="*")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    models_parser = subparsers.add_parser("models", help="查询指定平台的可用模型")
    models_parser.add_argument("profile")
    models_parser.add_argument("--json", action="store_true")
    models_parser.set_defaults(func=cmd_models)

    model_parser = subparsers.add_parser("model", help="修改平台的模型")
    model_parser.add_argument("model")
    model_parser.add_argument("--profile")
    model_parser.set_defaults(func=cmd_model)

    test_parser = subparsers.add_parser("test", help="交互测试平台和模型连通性")
    test_parser.add_argument("--profile")
    test_parser.add_argument("--model")
    test_parser.add_argument("--prompt")
    test_parser.add_argument("--type", dest="kind", choices=("auto", "text", "image"), default="auto")
    test_parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    test_parser.add_argument("--timeout", type=float, default=120.0)
    test_parser.set_defaults(func=cmd_test)

    doctor_parser = subparsers.add_parser("doctor", help="检查本地配置和接口")
    doctor_parser.set_defaults(func=cmd_doctor)

    add_parser = subparsers.add_parser("add", help="新建平台分组（交互输入 base_url / API Key）")
    add_parser.add_argument("profile")
    add_parser.add_argument("--base-url")
    add_parser.add_argument("--api-key")
    add_parser.add_argument("--model")
    add_parser.set_defaults(func=cmd_add)

    remove_parser = subparsers.add_parser("remove", help="删除平台分组")
    remove_parser.add_argument("profiles", nargs="+")
    remove_parser.set_defaults(func=cmd_remove)

    list_parser = subparsers.add_parser("list", help="按平台分组列出配置")
    list_parser.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config_dir = args.config_dir.expanduser().resolve()
    if not args.config_dir.is_dir():
        parser.error(f"配置目录不存在：{args.config_dir}")
    try:
        return args.func(args)
    except CcfgError as exc:
        print(f"错误：{redact(str(exc))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
