"""Nạp config/channels.yaml + biến môi trường -> adapter + outbound provider. Thiếu secret => kênh bị tắt."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import yaml

from zeus.channels.base import Dedupe, MemoryDedupe
from zeus.channels.messenger import MessengerAdapter
from zeus.channels.outbound import ChannelOutboundProvider, TokenGetter
from zeus.channels.router import ChannelsContext, Ingest
from zeus.channels.shopee import ShopeeAdapter
from zeus.channels.zalo_bot import ZaloBotAdapter
from zeus.channels.zalo_oa import ZaloOAAdapter
from zeus.contracts.interfaces import ChannelAdapter
from zeus.contracts.models import Channel


def load_yaml(path: str | Path = "config/channels.yaml") -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def build_adapters(cfg: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[Channel, ChannelAdapter]:
    e = os.environ if env is None else env
    chans = cfg.get("channels", {})
    out: dict[Channel, ChannelAdapter] = {}

    def on(name: str) -> dict[str, Any] | None:
        c = chans.get(name) or {}
        return c if c.get("enabled") else None

    if (c := on("zalo_bot")) and (s := e.get(c.get("secret_env", ""))):
        out[Channel.ZALO_BOT] = ZaloBotAdapter(s, c.get("bot_username"))
    if (c := on("zalo_oa")) and (a := e.get(c.get("app_id_env", ""))) and (s := e.get(c.get("secret_env", ""))):
        out[Channel.ZALO_OA] = ZaloOAAdapter(a, s, float(c.get("max_age_s", 300)))
    if (c := on("messenger")) and (s := e.get(c.get("app_secret_env", ""))) and (v := e.get(c.get("verify_token_env", ""))):
        out[Channel.MESSENGER] = MessengerAdapter(s, v)
    if (c := on("shopee")) and (k := e.get(c.get("push_key_env", ""))) and c.get("push_url"):
        out[Channel.SHOPEE] = ShopeeAdapter(k, c["push_url"], float(c.get("max_age_s", 600)))
    return out


def build_outbound(
    cfg: Mapping[str, Any], adapters: Mapping[Channel, ChannelAdapter], http: httpx.AsyncClient, env: Mapping[str, str] | None = None
) -> ChannelOutboundProvider:
    """Token đọc LÚC GỌI từ env (không giữ trong object)."""
    chans = cfg.get("channels", {})
    tokens: dict[Channel, TokenGetter] = {}
    for ch in adapters:
        name = (chans.get(ch.value) or {}).get("token_env")
        if name:
            tokens[ch] = lambda n=name: (os.environ if env is None else env).get(n)  # type: ignore[misc]
    specs = [s for a in adapters.values() for s in a.outbound_specs()]
    m = chans.get("messenger") or {}
    bases = {
        "zalo_bot": (chans.get("zalo_bot") or {}).get("api_base") or "https://bot-api.zaloplatforms.com",
        "zalo_oa": (chans.get("zalo_oa") or {}).get("api_base") or "https://openapi.zalo.me",
        "messenger": f"{m.get('graph_base', 'https://graph.facebook.com')}/{m.get('graph_version', 'v21.0')}",
    }
    return ChannelOutboundProvider(specs, tokens, http, bases)


def build_context(cfg: Mapping[str, Any], ingest: Ingest, env: Mapping[str, str] | None = None, dedupe: Dedupe | None = None) -> ChannelsContext:
    return ChannelsContext(
        adapters=build_adapters(cfg, env),
        ingest=ingest,
        dedupe=dedupe or MemoryDedupe(float(cfg.get("dedupe_ttl_s", 86400))),
        max_body_bytes=int(cfg.get("max_body_bytes", 1_048_576)),
    )
