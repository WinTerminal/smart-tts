"""智能TTS：通过 @ReplyExtension 让 Planner 决定是否给回复附加 TTS 语音。

合成失败或厂商未配置时返回 {}，回复回退为纯文本，不中断对话。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any, Literal

import httpx

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase, ReplyExtension

# RPC 单帧上限 32 MiB（maibot transport MAX_FRAME_SIZE）。Base64 膨胀 4/3，
# 再留出信封余量，这里限制原始音频字节数。
_MAX_AUDIO_BYTES = 23 * 1024 * 1024
# 不需要 api_key 的厂商
_NO_KEY_PROVIDERS = frozenset({"edge"})
# 需要额外必填字段的厂商：provider -> (配置字段名, 提示)
_REQUIRED_FIELDS: dict[str, tuple[str, str]] = {
    "azure": ("region", "Azure 区域，如 eastus"),
    "tencent": ("secret_id", "腾讯云 SecretId"),
}


# ───────────────────────── 配置模型 ─────────────────────────

ProviderName = Literal[
    "openai", "deepseek", "gemini", "azure", "minimax", "qwen", "qwen_audio",
    "groq", "xai", "elevenlabs", "mimo", "stepfun", "fish_audio",
    "siliconflow", "volcengine", "zhipu", "tencent", "edge",
]


class ProviderConfig(PluginConfigBase):
    """单个 TTS 厂商的连接配置。"""

    api_key: str = Field(default="", description="API Key", json_schema_extra={"input_type": "password"})
    base_url: str = Field(default="", description="接入地址；留空使用官方默认", placeholder="https://...")
    default_voice: str = Field(default="", description="默认音色；留空使用插件内置默认")
    model: str = Field(default="", description="模型名；留空使用默认")
    region: str = Field(default="", description="Azure 区域（如 eastus），仅 azure 使用")
    model_id: str = Field(default="", description="ElevenLabs 的 model_id；留空使用默认")
    reference_id: str = Field(default="", description="Fish Audio 音色模型 ID；fish_audio 必填")
    app_id: str = Field(default="", description="火山引擎 APP ID（旧版控制台），仅 volcengine 使用")
    secret_id: str = Field(default="", description="腾讯云 SecretId，仅 tencent 使用")


class GeneralSection(PluginConfigBase):
    """插件设置。"""

    __ui_label__ = "插件设置"

    config_version: str = Field(default="1.0.0", description="配置版本")
    default_provider: ProviderName = Field(default="deepseek", description="Planner 未指定厂商时使用的厂商")
    language: str = Field(default="zh", description="xAI 的 language 参数（BCP-47），其他厂商忽略")
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="语速倍率；不支持的厂商忽略")
    timeout: float = Field(default=60.0, ge=5.0, le=300.0, description="合成超时（秒）")


class ProvidersSection(PluginConfigBase):
    """各厂商配置。"""

    __ui_label__ = "厂商配置"

    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    azure: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    qwen: ProviderConfig = Field(default_factory=ProviderConfig)
    qwen_audio: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    xai: ProviderConfig = Field(default_factory=ProviderConfig)
    elevenlabs: ProviderConfig = Field(default_factory=ProviderConfig)
    mimo: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)
    fish_audio: ProviderConfig = Field(default_factory=ProviderConfig)
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    tencent: ProviderConfig = Field(default_factory=ProviderConfig)
    edge: ProviderConfig = Field(default_factory=ProviderConfig)


class SmartTTSConfig(PluginConfigBase):
    """智能TTS 完整配置。"""

    plugin: GeneralSection = Field(default_factory=GeneralSection)
    providers: ProvidersSection = Field(default_factory=ProvidersSection)


# ───────────────────────── 各厂商默认值 ─────────────────────────

# provider -> (默认 base_url, 默认 model, 默认 voice, 默认音频扩展名)
_DEFAULTS: dict[str, tuple[str, str, str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini-tts", "coral", "mp3"),
    "deepseek": ("https://api.deepseek.com", "", "stella", "wav"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "gemini-3.8-flash-tts", "Kore", "wav"),
    "azure": ("", "", "zh-CN-XiaoxiaoNeural", "mp3"),
    "minimax": ("https://api.minimax.io/v1", "speech-2.8-hd", "male-qn-qingse", "mp3"),
    "qwen": ("", "cosyvoice-v3-flash", "longanyang", "mp3"),
    "qwen_audio": ("", "qwen-audio-3.1-tts-flash", "longanhuan_v3.1", "wav"),
    "groq": ("https://api.groq.com/openai/v1", "canopylabs/orpheus-v1-english", "troy", "wav"),
    "xai": ("https://api.x.ai/v1", "", "eve", "mp3"),
    "elevenlabs": ("https://api.elevenlabs.io/v1", "eleven_multilingual_v2", "JBFqnCBsd6RMkjVDRZzb", "mp3"),
    "mimo": ("https://api.xiaomimimo.com/v1", "mimo-v2.5-tts", "mimo_default", "wav"),
    "stepfun": ("https://api.stepfun.com/v1", "step-tts-mini", "cixingnansheng", "mp3"),
    "fish_audio": ("https://api.fish.audio", "s2.1-pro", "", "mp3"),
    # 硅基流动：系统预置音色需写成 "模型名:音色名"
    "siliconflow": ("https://api.siliconflow.cn/v1", "FunAudioLLM/CosyVoice2-0.5B", "FunAudioLLM/CosyVoice2-0.5B:claire", "mp3"),
    # 火山引擎豆包：model 字段这里放的是资源 ID（seed-tts-2.0 / seed-tts-1.0）
    "volcengine": ("https://openspeech.bytedance.com/api/v3/tts/unidirectional", "seed-tts-2.0", "zh_female_vv_uranus_bigtts", "mp3"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-tts", "tongtong", "wav"),
    "tencent": ("https://tts.tencentcloudapi.com", "2019-08-23", "101001", "mp3"),
    "edge": ("", "", "zh-CN-XiaoxiaoNeural", "mp3"),
}


# ───────────────────────── 插件主体 ─────────────────────────

class SmartTTSPlugin(MaiBotPlugin):
    config_model = SmartTTSConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._client = httpx.AsyncClient(timeout=self.config.plugin.timeout, follow_redirects=True)
        self.ctx.logger.info("智能TTS 已加载，默认厂商: %s", self.config.plugin.default_provider)

    async def on_unload(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        if self._client is not None:
            self._client.timeout = self.config.plugin.timeout
        self.ctx.logger.info("智能TTS 配置已更新: version=%s", version)

    # ---------- 回复扩展 ----------

    @ReplyExtension(
        "voice",
        description=(
            "需要用语音回复时启用。provider 留空使用插件默认厂商；"
            "未配置的厂商或合成失败会自动回退为纯文字。"
        ),
        parameters={
            "provider": {"type": "string", "description": "TTS 厂商：openai/deepseek/gemini/azure/minimax/qwen/qwen_audio/groq/xai/elevenlabs/mimo/stepfun/fish_audio/siliconflow/volcengine/zhipu/tencent/edge", "default": ""},
            "voice": {"type": "string", "description": "音色 ID；留空使用厂商默认音色", "default": ""},
            "keep_text": {"type": "boolean", "description": "是否同时发送文字", "default": True},
        },
        timeout_ms=180000,
    )
    async def reply_voice(
        self,
        *,
        phase: str,
        parameters: dict[str, Any],
        text: str,
        messages: list[dict[str, Any]],
        **context: Any,
    ) -> dict[str, Any]:
        if phase == "prepare":
            return {}

        spoken = text or self._join_text(messages)
        if not spoken.strip():
            return {}

        provider = str(parameters.get("provider") or "").strip().lower() or str(self.config.plugin.default_provider)
        voice = str(parameters.get("voice") or "").strip()
        keep_text = bool(parameters.get("keep_text", True))

        try:
            audio = await self.synthesize(spoken, provider=provider, voice=voice)
        except Exception as exc:  # 合成失败静默回退，不中断对话
            self.ctx.logger.warning("TTS 合成失败，回退为纯文本 [%s]: %s", provider, exc)
            return {}

        voice_segment = {
            "type": "voice",
            "data": spoken,
            "binary_data_base64": base64.b64encode(audio).decode("ascii"),
        }

        result: list[dict[str, Any]] = []
        if keep_text:
            # 文字原样保留，语音追加在末尾；保留原有附件和引用关系。
            result.extend({"segments": list(m["segments"]), "quote_previous": m["quote_previous"]} for m in messages)
            result.append({"segments": [voice_segment], "quote_previous": False})
        else:
            # 用语音替换第一条有文字的消息里的文字段，其他消息只保留非文字段。
            replaced = False
            for message in messages:
                segments: list[dict[str, Any]] = []
                for segment in message["segments"]:
                    if segment["type"] != "text":
                        segments.append(dict(segment))
                    elif not replaced:
                        segments.append(voice_segment)
                        replaced = True
                if segments:
                    result.append({"segments": segments, "quote_previous": message["quote_previous"]})
            if not replaced:
                result.append({"segments": [voice_segment], "quote_previous": False})

        return {"messages": result}

    @staticmethod
    def _join_text(messages: list[dict[str, Any]]) -> str:
        return "".join(
            str(segment.get("data") or "")
            for message in messages
            for segment in message.get("segments", [])
            if segment.get("type") == "text"
        )

    # ---------- 统一分发入口 ----------

    async def synthesize(self, text: str, *, provider: str, voice: str = "") -> bytes:
        """按 provider 分发到各厂商的合成方法，返回音频 bytes。"""
        if provider not in _DEFAULTS:
            raise ValueError(f"不支持的 TTS 厂商: {provider}")
        cfg: ProviderConfig = getattr(self.config.providers, provider)
        if provider not in _NO_KEY_PROVIDERS and not cfg.api_key:
            raise ValueError(f"厂商 {provider} 未配置 api_key")
        if provider in _REQUIRED_FIELDS:
            field_name, hint = _REQUIRED_FIELDS[provider]
            if not getattr(cfg, field_name, "").strip():
                raise ValueError(f"厂商 {provider} 需要配置 {hint}")

        default_url, default_model, default_voice, ext = _DEFAULTS[provider]
        base_url = (cfg.base_url or default_url).rstrip("/")
        if not base_url and provider != "edge":
            raise ValueError(f"厂商 {provider} 需要在配置中填写 base_url")

        handler = getattr(self, f"_synth_{provider}")
        if handler is None:
            raise ValueError(f"不支持的 TTS 厂商: {provider}")
        audio = await asyncio.wait_for(
            handler(
                text=text.strip(),
                cfg=cfg,
                key=cfg.api_key,
                base_url=base_url,
                model=cfg.model or default_model,
                voice=voice or cfg.default_voice or default_voice,
                ext=ext,
            ),
            timeout=self.config.plugin.timeout,
        )
        if not audio:
            raise ValueError("厂商返回空音频")
        if len(audio) > _MAX_AUDIO_BYTES:
            raise ValueError(f"音频 {len(audio)} 字节超过 RPC 帧限制")
        return audio

    async def _post_bytes(self, url: str, **kwargs: Any) -> bytes:
        response = await self._get_client().post(url, **kwargs)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.content

    async def _post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._get_client().post(url, **kwargs)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("插件尚未加载")
        return self._client

    @staticmethod
    def _bearer(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    async def _download(self, url: str) -> bytes:
        response = await self._get_client().get(url)
        if response.status_code != 200:
            raise RuntimeError(f"下载音频 HTTP {response.status_code}")
        return response.content

    def _speed(self, default: float = 1.0) -> float:
        return float(self.config.plugin.speed) or default

    # ---------- 各厂商独立合成方法 ----------

    async def _synth_openai(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        body: dict[str, Any] = {"model": model, "input": text, "voice": voice, "response_format": "mp3"}
        if self._speed() != 1.0:
            body["speed"] = self._speed()
        return await self._post_bytes(f"{base_url}/audio/speech", headers=self._bearer(key), json=body)

    async def _synth_deepseek(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        # DeepSeek 原生 TTS：POST /v1/tools/tts，响应体为音频流。
        body: dict[str, Any] = {"text": text, "voice_id": voice, "format": "wav"}
        return await self._post_bytes(f"{base_url}/tools/tts", headers=self._bearer(key), json=body)

    async def _synth_gemini(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        # 新文档字段 speechConfig.voiceConfig.voice；旧文档为 prebuiltVoiceConfig.voiceName。
        body = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"voice": voice}},
            },
        }
        data = await self._post_json(
            f"{base_url}/models/{model}:generateContent",
            headers={"x-goog-api-key": key},
            json=body,
        )
        return self._b64(data, "candidates", 0, "content", "parts", 0, "inlineData", "data")

    async def _synth_azure(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        if not cfg.region:
            raise ValueError("Azure 需要配置 region")
        voice_name = saxutils.quoteattr(voice)  # 带引号的 XML 属性值，已做转义
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="zh-CN">'
            f"<voice name={voice_name}>{saxutils.escape(text)}</voice></speak>"
        )
        return await self._post_bytes(
            f"https://{cfg.region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "audio-16khz-64kbitrate-mono-mp3",
                "User-Agent": "MaiBotSmartTTS",
            },
            content=ssml.encode("utf-8"),
        )

    async def _synth_minimax(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        body = {
            "model": model,
            "text": text,
            "stream": False,
            "output_format": "hex",
            "voice_setting": {"voice_id": voice, "speed": self._speed(), "vol": 1, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
        }
        data = await self._post_json(f"{base_url}/t2a_v2", headers=self._bearer(key), json=body)
        if data.get("base_resp", {}).get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax 错误: {data['base_resp']}")
        audio_hex = data.get("data", {}).get("audio") or ""
        if not audio_hex:
            raise ValueError("MiniMax 返回空音频")
        return bytes.fromhex(audio_hex)

    async def _synth_qwen(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        # DashScope 非实时合成：返回 JSON，音频在 output.audio.url（24 小时有效）。
        body = {"model": model, "input": {"text": text, "voice": voice, "format": "mp3"}}
        data = await self._post_json(f"{base_url}/services/audio/tts/SpeechSynthesizer", headers=self._bearer(key), json=body)
        url = self._dig(data, "output", "audio", "url")
        if not url:
            raise RuntimeError(f"Qwen 未返回音频地址: {str(data)[:200]}")
        return await self._download(url)

    async def _synth_qwen_audio(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        # Qwen-Audio-TTS 与 CosyVoice 共用同一 HTTP 合成端点，仅模型和音色不同。
        return await self._synth_qwen(text=text, cfg=cfg, key=key, base_url=base_url, model=model, voice=voice, ext=ext)

    async def _synth_groq(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        # Groq 官方 TTS 端点。Orpheus 模型只支持 wav 且单次最多 200 字符，超长直接报错回退文本，
        # 不做静默截断（否则只朗读开头）。
        if len(text) > 200:
            raise ValueError("Groq Orpheus 单次最多 200 字符，请缩短文本或换用其他厂商")
        body: dict[str, Any] = {"model": model, "input": text, "response_format": "wav"}
        return await self._post_bytes(f"{base_url}/audio/speech", headers=self._bearer(key), json=body)

    async def _synth_xai(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        body: dict[str, Any] = {
            "text": text,
            "voice_id": voice,
            "language": self.config.plugin.language or "auto",
            "output_format": {"codec": "mp3", "sample_rate": 24000, "bit_rate": 128000},
        }
        if self._speed() != 1.0:
            body["speed"] = self._speed()
        return await self._post_bytes(f"{base_url}/tts", headers=self._bearer(key), json=body)

    async def _synth_elevenlabs(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        body = {"text": text, "model_id": cfg.model_id or model}
        return await self._post_bytes(
            f"{base_url}/text-to-speech/{voice}",
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            params={"output_format": "mp3_44100_128"},
            json=body,
        )

    async def _synth_mimo(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        # MiMo 走 chat/completions，待朗读文本放在 assistant 消息里。
        body = {
            "model": model,
            "messages": [{"role": "assistant", "content": text}],
            "audio": {"format": "wav", "voice": voice},
        }
        data = await self._post_json(f"{base_url}/chat/completions", headers={"api-key": key}, json=body)
        return self._b64(data, "choices", 0, "message", "audio", "data")

    async def _synth_stepfun(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        # StepFun 单次最多 1000 字符，超长直接报错回退文本。
        if len(text) > 1000:
            raise ValueError("StepFun 单次最多 1000 字符")
        body: dict[str, Any] = {"model": model, "input": text, "voice": voice, "response_format": "mp3"}
        if self._speed() != 1.0:
            body["speed"] = self._speed()
        return await self._post_bytes(f"{base_url}/audio/speech", headers=self._bearer(key), json=body)

    async def _synth_fish_audio(self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str) -> bytes:
        reference_id = cfg.reference_id or voice
        if not reference_id:
            raise ValueError("Fish Audio 需要配置 reference_id（音色模型 ID）")
        body = {"text": text, "reference_id": reference_id, "format": "mp3"}
        return await self._post_bytes(
            f"{base_url}/tts",
            headers={**self._bearer(key), "model": model},
            json=body,
        )

    # ---------- 第二批厂商 ----------

    async def _synth_siliconflow(
        self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str
    ) -> bytes:
        # 硅基流动：OpenAI 兼容的 /audio/speech。系统预置音色要写成 "模型名:音色名"。
        body: dict[str, Any] = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "sample_rate": 32000,
        }
        if self._speed() != 1.0:
            body["speed"] = self._speed()
        return await self._post_bytes(f"{base_url}/audio/speech", headers=self._bearer(key), json=body)

    async def _synth_volcengine(
        self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str
    ) -> bytes:
        # 火山引擎豆包 V3 HTTP Chunked：model 在这里放的是资源 ID（seed-tts-2.0 等）。
        # 音色要与资源 ID 匹配：*_bigtts（2.0 音色）配 seed-tts-2.0；*_moon_bigtts（1.0 音色）配 seed-tts-1.0。
        audio_params: dict[str, Any] = {"format": "mp3", "sample_rate": 24000}
        if self._speed() != 1.0:
            audio_params["speech_rate"] = max(-50, min(50, int(round((self._speed() - 1.0) * 100))))
        body = {
            "user": {"uid": "maibot"},
            "req_params": {"text": text, "speaker": voice, "audio_params": audio_params},
        }
        headers: dict[str, str] = {
            "X-Api-Resource-Id": model,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        if cfg.app_id:
            # 旧版控制台：APP ID + Access Token。api_key 填 Access Token。
            headers["X-Api-App-Id"] = cfg.app_id
            headers["X-Api-Access-Key"] = key
        else:
            headers["X-Api-Key"] = key
        # 响应是 HTTP Chunked，每块一个 JSON，音频在 data 字段（base64）。
        audio = bytearray()
        async with self._get_client().stream("POST", base_url, headers=headers, json=body) as response:
            if response.status_code != 200:
                await response.aread()
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("code", 0) not in (0, 20000000):
                    raise RuntimeError(f"豆包错误 {chunk.get('code')}: {chunk.get('message', '')}")
                audio.extend(base64.b64decode(chunk.get("data") or ""))
        return bytes(audio)

    async def _synth_zhipu(
        self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str
    ) -> bytes:
        # 智谱 GLM-TTS：非流式时响应体即音频字节。
        body: dict[str, Any] = {"model": model, "input": text, "voice": voice, "response_format": "wav"}
        if self._speed() != 1.0:
            body["speed"] = self._speed()
        return await self._post_bytes(f"{base_url}/audio/speech", headers=self._bearer(key), json=body)

    async def _synth_tencent(
        self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str
    ) -> bytes:
        # 腾讯云 TC3-HMAC-SHA256 签名；api_key 填 SecretKey，secret_id 填 SecretId。
        if len(text) > 150:
            raise ValueError("腾讯云基础语音合成单次最多 150 个汉字")
        payload = json.dumps(
            {
                "Text": text,
                "SessionId": str(uuid.uuid4()),
                "VoiceType": int(voice),
                "Codec": "mp3",
                "SampleRate": 16000,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        data = await self._tc_post(base_url, key, cfg.secret_id, payload, action="TextToVoice", version=model)
        return self._b64(data, "Response", "Audio")

    async def _tc_post(self, host: str, secret_key: str, secret_id: str, payload: str, *, action: str, version: str) -> dict[str, Any]:
        algorithm = "TC3-HMAC-SHA256"
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
        host_only = host.split("://", 1)[-1]
        content_type = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{content_type}\nhost:{host_only}\nx-tc-action:{action.lower()}\n"
        signed_headers = "content-type;host;x-tc-action"
        hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(["POST", "/", "", canonical_headers, signed_headers, hashed_payload])
        credential_scope = f"{date}/tts/tc3_request"
        string_to_sign = "\n".join([algorithm, str(timestamp), credential_scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
        secret_date = self._hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = self._hmac_sha256(secret_date, "tts")
        secret_signing = self._hmac_sha256(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"{algorithm} Credential={secret_id}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "Authorization": authorization,
            "Content-Type": content_type,
            "Host": host_only,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": version,
        }
        response = await self._get_client().post(host, headers=headers, content=payload.encode("utf-8"))
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        inner = data.get("Response", {})
        if inner.get("Error"):
            raise RuntimeError(f"腾讯云错误 {inner['Error'].get('Code')}: {inner['Error'].get('Message')}")
        return data

    @staticmethod
    def _hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    async def _synth_edge(
        self, *, text: str, cfg: ProviderConfig, key: str, base_url: str, model: str, voice: str, ext: str
    ) -> bytes:
        # 微软 Edge 浏览器"大声朗读"所用的在线服务，无需 key。非官方接口，可能被限流。
        import edge_tts  # 延迟导入；未安装时 ImportError 会被上层捕获并回退纯文本

        rate = f"{int(round((self._speed() - 1.0) * 100)):+d}%"
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        chunks = bytearray()
        async for piece in communicate.stream():
            if piece["type"] == "audio":
                chunks.extend(piece["data"])
        return bytes(chunks)

    # ---------- 工具方法 ----------

    @staticmethod
    def _dig(data: Any, *keys: Any) -> Any:
        for key in keys:
            if isinstance(data, list) and isinstance(key, int) and 0 <= key < len(data):
                data = data[key]
            elif isinstance(data, dict):
                data = data.get(key)
            else:
                return None
        return data

    @staticmethod
    def _b64(data: dict[str, Any], *path: Any) -> bytes:
        encoded = SmartTTSPlugin._dig(data, *path)
        if not encoded:
            raise RuntimeError(f"未返回音频数据: {str(data)[:200]}")
        return base64.b64decode(encoded)
