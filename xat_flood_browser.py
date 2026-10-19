#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# language: Python 3.10+ | target: Ubuntu Linux VPS | runtime: asyncio
# xat.com lotador de sala via navegador headless (Playwright/Chromium).
# Inclui stealth (esconde headless do Cloudflare) + espera o desafio resolver
# + deteccao real de entrada na sala. Rode na VPS.

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

from playwright.async_api import async_playwright, Page


@dataclass
class Config:
    room: str
    count: int = 10
    proxy: str | None = None          # scheme://user:pass@host:port
    headless: bool = True
    connect_delay: float = 1.0
    url_timeout: int = 60_000
    challenge_wait: float = 30.0      # segundos max esperando o cf resolver
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
        description="Lotador xat.com via navegador headless (stealth)")
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


async def _joined(page: Page) -> bool:
    """Tenta determinar se a aba passou do Cloudflare e entrou na sala."""
    try:
        if page.is_closed():
            return False
        url = page.url
        if "__cf_chl" in url:
            return False
        # o chat vive num iframe (xatframe); se existir, entrou
        n = await page.locator("iframe").count()
        return n > 0
    except Exception:
        return False


async def load_page(page: Page, cfg: Config, idx: int):
    await page.goto(f"https://xat.com/{cfg.room}",
                    wait_until="domcontentloaded", timeout=cfg.url_timeout)
    # espera o desafio do cloudflare resolver (url limpa + iframe)
    t0 = time.time()
    while time.time() - t0 < cfg.challenge_wait:
        if await _joined(page):
            print(f"[aba {idx}] ENTROU (iframe ok) em {int(time.time()-t0)}s")
            return True
        await asyncio.sleep(1.0)
    url = page.url
    if "__cf_chl" in url:
        print(f"[aba {idx}] DESAFIO travado (cloudflare nao resolveu): {url[:70]}")
    else:
        print(f"[aba {idx}] sem iframe (talvez sem chat): {url[:70]}")
    return False


def render(cfg: Config, pages: list, start: float) -> str:
    now = time.time()
    joined = sum(1 for p in pages if not p.is_closed() and "__cf_chl" not in p.url)
    open_ = sum(1 for p in pages if not p.is_closed())
    lines = [
        f"XAT FLOOD (browser)  sala={cfg.room}  alvo={cfg.count}  abas={open_}/{len(pages)}  sem-desafio={joined}  uptime={int(now-start)}s",
        "-" * 72,
    ]
    for i, p in enumerate(pages):
        if p.is_closed():
            st = "FECHADA"
        elif "__cf_chl" in p.url:
            st = "DESAFIO"
        else:
            st = "ABERTA"
        lines.append(f"{i+1:>3}  {st:<9} {p.url[:58]}")
    return "\n".join(lines)


async def amain(cfg: Config):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=cfg.headless,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])
        ctx_kwargs = {"viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]}}
        if cfg.proxy:
            ctx_kwargs["proxy"] = {"server": cfg.proxy}
        context = await browser.new_context(**ctx_kwargs)
        await context.add_init_script(STEALTH_JS)

        pages: list[Page] = []
        start = time.time()

        for i in range(1, cfg.count + 1):
            page = await context.new_page()
            pages.append(page)
            try:
                await load_page(page, cfg, i)
            except Exception as e:
                print(f"[aba {i}] erro: {e}")
            await asyncio.sleep(cfg.connect_delay)

        print("\nAbas abertas. Mantendo vivas (Ctrl+C encerra).\n")

        while True:
            for i, p in enumerate(pages):
                if p.is_closed():
                    np_ = await context.new_page()
                    pages[i] = np_
                    try:
                        await load_page(np_, cfg, i + 1)
                    except Exception as e:
                        print(f"[aba {i+1}] reabertura erro: {e}")
            print(render(cfg, pages, start))
            await asyncio.sleep(10.0)


def main():
    cfg = parse_args(sys.argv[1:])
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        print("\nencerrado.")


if __name__ == "__main__":
    main()
