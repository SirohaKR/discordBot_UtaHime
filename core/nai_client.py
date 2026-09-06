# -*- coding: utf-8 -*-
"""NovelAI 이미지 생성 API 클라이언트 (비동기, aiohttp 기반).

아카라이브 "Novel AI 이미지 생성 도구 개발용 API 레퍼런스"(DNT-LAB/NAIA_novel_ai_entrypoint)의
naia.py가 정리해둔 비공식 API 스펙을 discord.py(asyncio/aiohttp) 환경에 맞게 옮겼다.
공식 문서화된 API가 아니므로 NovelAI 쪽 변경 시 깨질 수 있다.
"""

import base64
import io
import zipfile
from dataclasses import dataclass
from typing import Optional

import aiohttp

GENERATE_URL = "https://image.novelai.net/ai/generate-image"
ENCODE_VIBE_URL = "https://image.novelai.net/ai/encode-vibe"
SUBSCRIPTION_URL = "https://api.novelai.net/user/subscription"

MODELS = {
    "nai-diffusion-4-5-full": "풀 모델",
    "nai-diffusion-4-5-curated": "큐레이트 모델",
}

# 무료 티어 해상도 프리셋 (64의 배수, 1,048,576px 이하).
# Opus 구독이어도 이 범위를 벗어나는 해상도/스텝은 Anlas가 소모되므로 기본 프리셋은 전부 이 안에서만 제공한다.
SIZE_PRESETS = {
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "square": (1024, 1024),
    "tall": (704, 1472),
    "wide": (1472, 704),
}

QUALITY_TAGS = {
    "nai-diffusion-4-5-full": ", location, very aesthetic, masterpiece, no text",
    "nai-diffusion-4-5-curated": ", location, masterpiece, no text, -0.8::feet::, rating:general",
}

UC_PRESETS = {
    "nai-diffusion-4-5-full": (
        "lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, "
        "jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, "
        "multiple views, logo, too many watermarks, negative space, blank page"
    ),
    "nai-diffusion-4-5-curated": (
        "blurry, lowres, upscaled, artistic error, film grain, scan artifacts, worst quality, "
        "bad quality, jpeg artifacts, very displeasing, chromatic aberration, halftone, "
        "multiple views, logo, too many watermarks, negative space, blank page"
    ),
}

RATING_TAGS = {
    "general": "rating:general, safe",
    "sensitive": "rating:sensitive",
    "questionable": "rating:questionable, nsfw",
    "explicit": "rating:explicit, nsfw",
}


class NaiError(RuntimeError):
    pass


class NaiAuthError(NaiError):
    pass


class NaiRateLimitError(NaiError):
    pass


@dataclass
class GenerationResult:
    png_bytes: bytes
    seed: int


def _build_payload(
    *,
    prompt: str,
    negative_prompt: str,
    model: str,
    width: int,
    height: int,
    rating: str,
    seed: int,
    steps: int,
    cfg_scale: float,
    cfg_rescale: float,
    sampler: str,
    scheduler: str,
    apply_quality_tags: bool,
    vibe_encoded: Optional[str] = None,
    vibe_strength: float = 0.6,
    vibe_information_extracted: float = 1.0,
) -> dict:
    full_prompt = f"{RATING_TAGS.get(rating, RATING_TAGS['sensitive'])}, {prompt}"
    if apply_quality_tags:
        full_prompt += QUALITY_TAGS.get(model, "")

    preset_negative = UC_PRESETS.get(model, "")
    full_negative = f"{negative_prompt}, {preset_negative}" if negative_prompt and preset_negative else (negative_prompt or preset_negative)

    parameters = {
        "width": width,
        "height": height,
        "n_samples": 1,
        "seed": seed,
        "extra_noise_seed": seed,
        "sampler": sampler,
        "steps": steps,
        "scale": cfg_scale,
        "negative_prompt": full_negative,
        "cfg_rescale": cfg_rescale,
        "noise_schedule": scheduler,
        "params_version": 3,
        "legacy": False,
        "legacy_v3_extend": False,
        "skip_cfg_above_sigma": None,
        "autoSmea": True,
        "prefer_brownian": True,
        "ucPreset": 0,
        "use_coords": False,
        "legacy_uc": False,
        "add_original_image": True,
        "v4_prompt": {
            "caption": {"base_caption": full_prompt, "char_captions": []},
            "use_coords": False,
            "use_order": True,
        },
        "v4_negative_prompt": {
            "caption": {"base_caption": full_negative, "char_captions": []},
            "legacy_uc": False,
        },
    }

    if vibe_encoded:
        parameters["reference_image_multiple"] = [vibe_encoded]
        parameters["reference_strength_multiple"] = [vibe_strength]
        parameters["reference_information_extracted_multiple"] = [vibe_information_extracted]
        parameters["normalize_reference_strength_multiple"] = True

    return {"input": full_prompt, "model": model, "action": "generate", "parameters": parameters}


async def generate_image(
    token: str,
    *,
    prompt: str,
    negative_prompt: str = "",
    model: str = "nai-diffusion-4-5-full",
    width: int = 832,
    height: int = 1216,
    rating: str = "sensitive",
    seed: int = 0,
    steps: int = 28,
    cfg_scale: float = 5.0,
    cfg_rescale: float = 0.4,
    sampler: str = "k_euler_ancestral",
    scheduler: str = "native",
    apply_quality_tags: bool = True,
    vibe_encoded: Optional[str] = None,
    vibe_strength: float = 0.6,
    vibe_information_extracted: float = 1.0,
) -> GenerationResult:
    payload = _build_payload(
        prompt=prompt,
        negative_prompt=negative_prompt,
        model=model,
        width=width,
        height=height,
        rating=rating,
        seed=seed,
        steps=steps,
        cfg_scale=cfg_scale,
        cfg_rescale=cfg_rescale,
        sampler=sampler,
        scheduler=scheduler,
        apply_quality_tags=apply_quality_tags,
        vibe_encoded=vibe_encoded,
        vibe_strength=vibe_strength,
        vibe_information_extracted=vibe_information_extracted,
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            GENERATE_URL, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=180)
        ) as resp:
            if resp.status == 401:
                raise NaiAuthError("NovelAI 인증에 실패했습니다. `.env`의 NAI_TOKEN을 확인하세요.")
            if resp.status == 429:
                raise NaiRateLimitError("NovelAI 요청이 너무 잦습니다. 잠시 후 다시 시도하세요.")
            if resp.status != 200:
                text = await resp.text()
                raise NaiError(f"NovelAI API 오류 (HTTP {resp.status}): {text[:300]}")
            content = await resp.read()

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        png_bytes = zf.read(zf.infolist()[0])
    return GenerationResult(png_bytes=png_bytes, seed=seed)


async def encode_vibe(
    token: str, image_bytes: bytes, model: str = "nai-diffusion-4-5-full", information_extracted: float = 1.0
) -> str:
    """참조 이미지를 Vibe Transfer용으로 인코딩한다.

    결과는 비결정적이라 매번 값이 다르지만, 같은 이미지+모델+information_extracted 조합에는
    재사용 가능 — 호출 1회당 2 Anlas가 소모되므로 호출부에서 반드시 캐싱해야 한다.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "image": base64.b64encode(image_bytes).decode(),
        "information_extracted": information_extracted,
        "model": model,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            ENCODE_VIBE_URL, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status == 401:
                raise NaiAuthError("NovelAI 인증에 실패했습니다. `.env`의 NAI_TOKEN을 확인하세요.")
            if resp.status == 429:
                raise NaiRateLimitError("NovelAI 요청이 너무 잦습니다. 잠시 후 다시 시도하세요.")
            if resp.status != 200:
                text = await resp.text()
                raise NaiError(f"Vibe 인코딩 실패 (HTTP {resp.status}): {text[:300]}")
            content = await resp.read()
    return base64.b64encode(content).decode()


async def get_anlas(token: str) -> dict:
    """Anlas(크레딧) 잔액 조회. Returns: {"fixed", "purchased", "total", "opus"}"""
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            SUBSCRIPTION_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 401:
                raise NaiAuthError("NovelAI 인증에 실패했습니다. `.env`의 NAI_TOKEN을 확인하세요.")
            if resp.status != 200:
                text = await resp.text()
                raise NaiError(f"Anlas 조회 실패 (HTTP {resp.status}): {text[:300]}")
            data = await resp.json()

    steps_left = data.get("trainingStepsLeft", {})
    fixed = steps_left.get("fixedTrainingStepsLeft", 0)
    purchased = steps_left.get("purchasedTrainingSteps", 0)
    opus = data.get("perks", {}).get("unlimitedMaxPriority", False)
    return {"fixed": fixed, "purchased": purchased, "total": fixed + purchased, "opus": opus}
