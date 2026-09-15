"""Black Forest Labs (FLUX) image generation backend.

Exposes BFL's FLUX family — FLUX.2 [pro], [max], [flex], [klein] and the
FLUX.1 Kontext / Ultra endpoints — as an :class:`ImageGenProvider`.

🔴 **This box cannot reach api.bfl.ai directly.** The vServer runs a
default-deny egress allowlist, and BFL requires a wildcard
(``delivery.*.bfl.ai``, rotating Azure Front Door IPs) which nftables cannot
express — see the skill ``egress-allowlist-provider-onboarding``, where BFL is
the documented case study. The supported path is the **loopback proxy** at
``~/.hermes/profiles/birk/deploy/bfl-proxy/`` (systemd unit ``bflproxy``,
uid ``bflproxy``, ``127.0.0.1:8791``). It owns the API key, does submit +
polling + download, and returns raw image bytes.

Consequences for this provider:

* it needs **no API key** — the proxy holds it (that is the point);
* it needs no egress of its own;
* ``BFL_API_KEY`` in the agent's ``.env`` is deliberately *not* used here.

Setting ``image_gen.bfl.mode: direct`` bypasses the proxy and calls the API
straight — correct on machines without this firewall, and the reason the
direct code path is kept.

The BFL API is asynchronous (submit → poll → signed URL); in proxy mode that
whole roundtrip happens inside the proxy. Verified against the live OpenAPI
schema (``https://api.bfl.ai/openapi.json``) and the quick-start guide on
2026-09-08.

Selection precedence (first hit wins):

1. ``BFL_IMAGE_MODEL`` env var (escape hatch for scripts / tests)
2. ``image_gen.bfl.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` (when it names one of our IDs)
4. :data:`DEFAULT_MODEL`

Docs: https://docs.bfl.ai/quick_start/generating_images
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from agent.secret_scope import get_secret
from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Global endpoint. BFL also offers api.eu.bfl.ai / api.us.bfl.ai; the EU host
#: is selectable via ``image_gen.bfl.region: eu`` for data-residency reasons.
BASE_URLS: Dict[str, str] = {
    "global": "https://api.bfl.ai",
    "eu": "https://api.eu.bfl.ai",
    "us": "https://api.us.bfl.ai",
}
DEFAULT_REGION = "global"

#: Loopback proxy (see module docstring). Default path on this machine.
PROXY_URL = os.environ.get("BFLPROXY_URL", "http://127.0.0.1:8791")
PROXY_RENDER_TIMEOUT_S = 330.0  # proxy caps polling at 300s; leave headroom

#: 🔴 The polling URL returned by the API MUST be used verbatim — the docs are
#: explicit that a hand-built poll URL does not work on the global/regional
#: hosts. We only ever read ``polling_url`` from the response.
POLL_INTERVAL_S = 1.0
POLL_TIMEOUT_S = 300.0
SUBMIT_TIMEOUT_S = 60.0

#: Max reference/input images the FLUX.2 endpoints accept (input_image ..
#: input_image_8). Older FLUX.1 endpoints take a single ``input_image``.
MAX_REFERENCE_IMAGES_FLUX2 = 7  # + 1 primary = 8 slots total
MAX_REFERENCE_IMAGES_FLUX1 = 0  # + 1 primary = 1 slot

_MODELS: Dict[str, Dict[str, Any]] = {
    "flux-2-pro-preview": {
        "display": "FLUX.2 [pro] (preview)",
        "path": "flux-2-pro-preview",
        "family": "flux2",
        "strengths": "Latest quality/speed improvements. BFL's recommended starting point.",
        "note": "Preview endpoint — moves with BFL's releases, not pinned.",
    },
    "flux-2-pro": {
        "display": "FLUX.2 [pro]",
        "path": "flux-2-pro",
        "family": "flux2",
        "strengths": "Pinned snapshot of FLUX.2 [pro] — use when you need reproducibility.",
    },
    "flux-2-max": {
        "display": "FLUX.2 [max]",
        "path": "flux-2-max",
        "family": "flux2",
        "strengths": "Highest-capability FLUX.2 tier.",
    },
    "flux-2-flex": {
        "display": "FLUX.2 [flex]",
        "path": "flux-2-flex",
        "family": "flux2",
        "strengths": "Flexible quality/cost trade-off.",
    },
    "flux-2-klein-9b-preview": {
        "display": "FLUX.2 [klein] 9B (preview)",
        "path": "flux-2-klein-9b-preview",
        "family": "flux2",
        "strengths": "Small, fast, KV-cached. Good for iteration loops.",
    },
    "flux-2-klein-9b": {
        "display": "FLUX.2 [klein] 9B",
        "path": "flux-2-klein-9b",
        "family": "flux2",
        "strengths": "Pinned snapshot of klein 9B.",
    },
    "flux-2-klein-4b": {
        "display": "FLUX.2 [klein] 4B",
        "path": "flux-2-klein-4b",
        "family": "flux2",
        "strengths": "Smallest/cheapest FLUX.2 tier.",
    },
    "flux-kontext-max": {
        "display": "FLUX.1 Kontext [max]",
        "path": "flux-kontext-max",
        "family": "kontext",
        "strengths": "Image editing with strong instruction following.",
        "note": "Rate-limited to 6 active tasks by BFL (capacity).",
    },
    "flux-kontext-pro": {
        "display": "FLUX.1 Kontext [pro]",
        "path": "flux-kontext-pro",
        "family": "kontext",
        "strengths": "Image editing, faster/cheaper than Kontext max.",
    },
    "flux-pro-1.1-ultra": {
        "display": "FLUX1.1 [pro] ultra",
        "path": "flux-pro-1.1-ultra",
        "family": "flux1",
        "strengths": "High resolution FLUX.1 generation.",
    },
    "flux-pro-1.1": {
        "display": "FLUX1.1 [pro]",
        "path": "flux-pro-1.1",
        "family": "flux1",
        "strengths": "Established FLUX.1 workhorse.",
    },
    "flux-dev": {
        "display": "FLUX.1 [dev]",
        "path": "flux-dev",
        "family": "flux1",
        "strengths": "Cheapest, open-weights sibling. Lower fidelity.",
    },
}

DEFAULT_MODEL = "flux-2-pro-preview"

#: BFL takes explicit width/height, not an aspect-ratio string. These are
#: multiples of 32 and land near ~1 MP for the landscape/portrait cases.
_DIMENSIONS: Dict[str, tuple] = {
    "landscape": (1440, 810),
    "square": (1024, 1024),
    "portrait": (810, 1440),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_image_gen_config() -> Dict[str, Any]:
    """Read the ``image_gen`` block from config.yaml (empty dict on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("bfl: could not load image_gen config: %s", exc)
        return {}


def _config() -> Dict[str, Any]:
    """Return the ``image_gen.bfl`` sub-block (empty dict when absent)."""
    block = _load_image_gen_config().get("bfl") or {}
    return block if isinstance(block, dict) else {}


def _config_image_gen() -> Dict[str, Any]:
    return _load_image_gen_config()


def _api_key() -> Optional[str]:
    """Resolve the API key. Never logged, never returned in responses."""
    return get_secret("BFL_API_KEY") or os.environ.get("BFL_API_KEY") or None


def _base_url() -> str:
    region = str(_config().get("region") or DEFAULT_REGION).strip().lower()
    return BASE_URLS.get(region, BASE_URLS[DEFAULT_REGION])


def refs_present(value: Any) -> bool:
    """True when the caller supplied any reference images."""
    return bool(normalize_reference_images(value))


def _cache_path(prefix: str, ext: str) -> Path:
    """Path under the same cache dir the shared helpers use."""
    import uuid

    base = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    d = base / "cache" / "images"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return d / f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}{ext}"


def _to_data_url(ref: str) -> Optional[str]:
    """Turn a local path or http(s) URL into what BFL accepts.

    BFL takes base64 image data (or a URL it can fetch). Local files are
    read and base64-encoded; remote URLs are passed through unchanged.
    """
    if not ref:
        return None
    parsed = urlparse(ref)
    if parsed.scheme in ("http", "https"):
        return ref
    path = Path(ref).expanduser()
    if not path.is_file():
        logger.warning("bfl: reference image not found, skipping: %s", ref)
        return None
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class BflImageGenProvider(ImageGenProvider):
    """Black Forest Labs FLUX backend."""

    @property
    def name(self) -> str:
        return "bfl"

    @property
    def display_name(self) -> str:
        return "Black Forest Labs (FLUX)"

    def _mode(self) -> str:
        """``proxy`` (default here) or ``direct``."""
        mode = str(_config().get("mode") or "proxy").strip().lower()
        return "direct" if mode == "direct" else "proxy"

    def is_available(self) -> bool:
        if self._mode() == "direct":
            return bool(_api_key())
        # Proxy mode: the service holds the key. Ask it, cheaply.
        try:
            r = requests.get(f"{PROXY_URL}/health", timeout=2)
            return r.status_code == 200 and bool(r.json().get("key_present"))
        except requests.RequestException:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for mid, meta in _MODELS.items():
            entry: Dict[str, Any] = {
                "id": mid,
                "name": meta["display"],
                "description": meta.get("strengths", ""),
            }
            if meta.get("note"):
                entry["note"] = meta["note"]
            out.append(entry)
        return out

    def default_model(self) -> Optional[str]:
        return self._resolve_model()

    def capabilities(self) -> Dict[str, Any]:
        model = self._resolve_model()
        family = _MODELS.get(model, {}).get("family", "flux2")
        return {
            "modalities": ["text", "image"],
            "max_reference_images": (
                MAX_REFERENCE_IMAGES_FLUX2 if family == "flux2" else MAX_REFERENCE_IMAGES_FLUX1
            ),
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Black Forest Labs (FLUX)",
            "badge": "paid",
            "tag": (
                "FLUX.2 [pro]/[max]/[klein] and FLUX.1 Kontext — text-to-image "
                "and editing with up to 8 input images"
            ),
            "env_vars": [
                {
                    "key": "BFL_API_KEY",
                    "label": "BFL API key",
                    "help": "From your profile at https://api.bfl.ai",
                    "secret": True,
                }
            ],
        }

    # -- internals ---------------------------------------------------------

    def _resolve_model(self) -> str:
        env = (os.environ.get("BFL_IMAGE_MODEL") or "").strip()
        if env in _MODELS:
            return env
        scoped = str(_config().get("model") or "").strip()
        if scoped in _MODELS:
            return scoped
        generic = str(_config_image_gen().get("model") or "").strip()
        if generic in _MODELS:
            return generic
        return DEFAULT_MODEL

    def _poll(self, polling_url: str, key: str, deadline: float) -> Dict[str, Any]:
        """Poll until Ready/Error. Returns the final payload dict."""
        headers = {"accept": "application/json", "x-key": key}
        delay = POLL_INTERVAL_S
        while time.time() < deadline:
            time.sleep(delay)
            resp = requests.get(polling_url, headers=headers, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"polling returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            payload = resp.json()
            status = str(payload.get("status") or "")
            if status == "Ready":
                return payload
            if status in ("Error", "Failed"):
                detail = payload.get("details") or payload.get("result") or status
                raise RuntimeError(f"generation failed: {str(detail)[:300]}")
            # Pending / Request Moderated / Task not found -> keep waiting,
            # but back off gently so long jobs don't hammer the endpoint.
            delay = min(delay * 1.25, 4.0)
        raise TimeoutError(f"generation did not finish within {POLL_TIMEOUT_S:.0f}s")

    # -- the contract ------------------------------------------------------

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        ratio = resolve_aspect_ratio(aspect_ratio)
        model = self._resolve_model()
        meta = _MODELS.get(model, _MODELS[DEFAULT_MODEL])
        family = meta.get("family", "flux2")

        width, height = _DIMENSIONS[ratio]

        # --- proxy mode (default on this machine) --------------------------
        if self._mode() == "proxy":
            if image_url or refs_present(reference_image_urls):
                return error_response(
                    error=(
                        "the BFL proxy only does text-to-image; for editing set "
                        "image_gen.bfl.mode: direct (needs egress to bfl.ai)"
                    ),
                    error_type="unsupported_operation",
                    provider=self.name,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=ratio,
                )
            body = {"prompt": prompt, "model": model, "width": width, "height": height}
            fmt = _config().get("output_format")
            if fmt:
                body["output_format"] = str(fmt)
            seed = kwargs.get("seed") or _config().get("seed")
            if seed is not None:
                try:
                    body["seed"] = int(seed)
                except (TypeError, ValueError):
                    pass
            tol = _config().get("safety_tolerance")
            if tol is not None:
                try:
                    body["safety_tolerance"] = max(0, min(5, int(tol)))
                except (TypeError, ValueError):
                    pass
            try:
                resp = requests.post(
                    f"{PROXY_URL}/render", json=body, timeout=PROXY_RENDER_TIMEOUT_S
                )
            except requests.RequestException as exc:
                return error_response(
                    error=(
                        f"the BFL proxy at {PROXY_URL} did not answer ({exc}). "
                        "Install/start it: sudo ~/.hermes/profiles/birk/deploy/"
                        "bfl-proxy/install.sh"
                    ),
                    error_type="provider_unavailable",
                    provider=self.name,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=ratio,
                )
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("error", resp.text[:200])
                except ValueError:
                    detail = resp.text[:200]
                kind = {402: "quota_exceeded", 429: "rate_limited"}.get(
                    resp.status_code, "provider_error"
                )
                return error_response(
                    error=f"BFL proxy: {detail}",
                    error_type=kind,
                    provider=self.name,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=ratio,
                )
            # The proxy returns raw bytes, already content-type checked.
            ctype = resp.headers.get("Content-Type", "image/png")
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
                ctype, ".png"
            )
            out = _cache_path(f"bfl_{model}", ext)
            out.write_bytes(resp.content)
            return success_response(
                image=str(out),
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
                provider=self.name,
                modality="text",
                extra={"via": "bfl-proxy", "bytes": len(resp.content)},
            )

        # --- direct mode ---------------------------------------------------
        key = _api_key()
        if not key:
            return error_response(
                error="BFL_API_KEY is not set. Add it to .env and restart the gateway.",
                error_type="missing_credentials",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )

        refs = normalize_reference_images(reference_image_urls) or []
        max_refs = (
            MAX_REFERENCE_IMAGES_FLUX2 if family == "flux2" else MAX_REFERENCE_IMAGES_FLUX1
        )
        refs = refs[:max_refs]
        modality = "image" if (image_url or refs) else "text"

        body: Dict[str, Any] = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "output_format": str(_config().get("output_format") or "png"),
        }
        seed = kwargs.get("seed") or _config().get("seed")
        if seed is not None:
            try:
                body["seed"] = int(seed)
            except (TypeError, ValueError):
                pass
        tol = _config().get("safety_tolerance")
        if tol is not None:
            try:
                body["safety_tolerance"] = max(0, min(5, int(tol)))
            except (TypeError, ValueError):
                pass
        # `disable_pup` turns OFF BFL's automatic prompt upsampling. Our prompts
        # are already detailed, and upsampling silently rewrites them — which
        # breaks reproducibility and the "prompt + params" record we file with
        # every generated asset. Default to exact prompts, overridable.
        body["disable_pup"] = bool(_config().get("disable_prompt_upsampling", True))

        # Primary edit source goes into input_image; extra refs fill 2..8.
        if image_url:
            encoded = _to_data_url(image_url)
            if encoded:
                body["input_image"] = encoded
        for i, ref in enumerate(refs, start=2):
            encoded = _to_data_url(ref)
            if encoded:
                body[f"input_image_{i}"] = encoded

        url = f"{_base_url()}/v1/{meta['path']}"
        headers = {
            "accept": "application/json",
            "x-key": key,
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=SUBMIT_TIMEOUT_S)
        except requests.RequestException as exc:
            return error_response(
                error=f"could not reach the BFL API: {exc}",
                error_type="network_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )

        if resp.status_code == 402:
            return error_response(
                error="BFL reports no credits left (HTTP 402). Top up at https://api.bfl.ai.",
                error_type="quota_exceeded",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )
        if resp.status_code == 429:
            return error_response(
                error="BFL rate limit hit (24 active tasks, 6 for flux-kontext-max). Retry shortly.",
                error_type="rate_limited",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )
        if resp.status_code != 200:
            return error_response(
                error=f"BFL returned HTTP {resp.status_code}: {resp.text[:300]}",
                error_type="provider_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )

        payload = resp.json()
        polling_url = payload.get("polling_url")
        if not polling_url:
            return error_response(
                error=f"BFL response carried no polling_url: {str(payload)[:200]}",
                error_type="provider_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )

        try:
            final = self._poll(
                polling_url, key, deadline=time.time() + POLL_TIMEOUT_S
            )
        except (RuntimeError, TimeoutError, requests.RequestException) as exc:
            return error_response(
                error=str(exc),
                error_type="provider_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )

        sample = ((final.get("result") or {}) or {}).get("sample")
        if not sample:
            return error_response(
                error=f"BFL reported Ready but returned no image URL: {str(final)[:200]}",
                error_type="provider_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )

        # 🔴 Signed delivery URLs expire after 10 minutes and BFL explicitly
        # asks callers not to hand them to users. Always materialise locally.
        try:
            local = save_url_image(sample, prefix=f"bfl_{model}")
        except Exception as exc:
            return error_response(
                error=f"downloading the generated image failed: {exc}",
                error_type="provider_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=ratio,
            )

        extra: Dict[str, Any] = {}
        for field in ("cost", "output_mp", "input_mp"):
            if payload.get(field) is not None:
                extra[field] = payload[field]
        if body.get("seed") is not None:
            extra["seed"] = body["seed"]

        return success_response(
            image=str(local),
            model=model,
            prompt=prompt,
            aspect_ratio=ratio,
            provider=self.name,
            modality=modality,
            extra=extra or None,
        )


def register(ctx) -> None:  # noqa: ANN001 - plugin context type varies
    ctx.register_image_gen_provider(BflImageGenProvider())
