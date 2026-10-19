#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# language: Python 3.10+ | target: Ubuntu Linux VPS | runtime: asyncio
# xat.com lotador de sala via navegador headless (Playwright/Chromium).
# Cada conexao ganha um CONTEXTO proprio (cookies/localStorage isolados) pra
# virar um guest distinto. Stealth + espera do Cloudflare + deteccao por WS.
# Rode na VPS, de preferencia com xvfb + --headed.

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

from playwright.async_api import async_playwright, Page, BrowserContext


@dataclass
class Config:
    room: str
    count: int = 10
    proxy: str | None = None          # scheme://user:pass@host:port
    headless: bool = True
    connect_delay: float = 1.0
    url_timeout: int = 60_000
    challenge_wait: float = 30.0
    viewport: tuple = (1000, 760)


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR','pt','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || { runtime: {} };
const oq = window.navigator.permissions && window.navigator.permissions.query;
if (oq) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : oq(p);
}
"""


def parse_args(argv) -> Config:
    ap = argparse.ArgumentParser(
        prog="xat_flood_browser",
        description="Lotador xat.com via navegador headless (contexto por conexao)")
    ap.add_argument("--room", required=True)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--connect-delay", type=float, default=1.0)
    ap.add_argument("--challenge-wait", type=float, default=30.0)
    args = ap.parse_args(argv)
    return Config(room=args.room, count=args.count, proxy=args.proxy,
                  headless=not args.headed, connect_delay=args.connect_delay,
                  challenge_wait=args.challenge_wait)


class Slot:
    """Um contexto + uma pagina = um guest isolado."""
    __slots__ = ("context", "page", "ws_count")

    def __init__(self, context: BrowserContext, page: Page):
        self.context = context
        self.page = page
        self.ws_count = 0

    def is_closed(self) -> bool:
        return self.page.is_closed()

    def status(self) -> str:
        try:
            if self.page.is_closed():
                return "FECHADA"
            if "__cf_chl" in self.page.url:
                return "DESAFIO"
            if self.ws_count > 0:
                return "ENTROU"
            return "CARREGANDO"
        except Exception:
            return "?"


async def load_slot(slot: Slot, cfg: Config, idx: int) -> bool:
    page = slot.page

    def on_ws(ws):
        if "xat.com" in ws.url:
            slot.ws_count += 1

    page.on("websocket", on_ws)

    await page.goto(f"https://xat.com/{cfg.room}",
                    wait_until="domcontentloaded", timeout=cfg.url_timeout)

    t0 = time.time()
    while time.time() - t0 < cfg.challenge_wait:
        if slot.ws_count > 0:
            print(f"[{idx}] ENTROU (ws xat) em {int(time.time()-t0)}s")
            return True
        await asyncio.sleep(1.0)

    url = page.url
    if "__cf_chl" in url:
        print(f"[{idx}] DESAFIO travado: {url[:60]}")
    else:
        print(f"[{idx}] sem ws xat: url={url[:50]}")
    return False


def render(cfg: Config, slots: list, start: float) -> str:
    now = time.time()
    joined = sum(1 for s in slots if not s.is_closed() and s.ws_count > 0)
    open_ = sum(1 for s in slots if not s.is_closed())
    lines = [
        f"XAT FLOOD  sala={cfg.room}  alvo={cfg.count}  abas={open_}/{len(slots)}  com-ws={joined}  uptime={int(now-start)}s",
        "-" * 72,
    ]
    for i, s in enumerate(slots):
        lines.append(f"{i+1:>3}  {s.status():<11} {s.page.url[:55] if not s.page.is_closed() else ''}")
    return "\n".join(lines)


async def amain(cfg: Config):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=cfg.headless,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])

        async def new_slot() -> Slot:
            ctx_kwargs = {"viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]}}
            if cfg.proxy:
                ctx_kwargs["proxy"] = {"server": cfg.proxy}
            context = await browser.new_context(**ctx_kwargs)
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()
            return Slot(context, page)

        slots: list[Slot] = []
        start = time.time()

        for i in range(1, cfg.count + 1):
            slot = await new_slot()
            slots.append(slot)
            try:
                await load_slot(slot, cfg, i)
            except Exception as e:
                print(f"[{i}] erro: {e}")
            await asyncio.sleep(cfg.connect_delay)

        print("\nConectados. Mantendo vivos (Ctrl+C encerra).\n")

        while True:
            for i, s in enumerate(slots):
                if s.is_closed():
                    try:
                        await s.context.close()
                    except Exception:
                        pass
                    ns = await new_slot()
                    slots[i] = ns
                    try:
                        await load_slot(ns, cfg, i + 1)
                    except Exception as e:
                        print(f"[{i+1}] reabertura erro: {e}")
            print(render(cfg, slots, start))
            await asyncio.sleep(10.0)


def main():
    cfg = parse_args(sys.argv[1:])
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        print("\nencerrado.")


if __name__ == "__main__":
    main()
