#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# language: Python 3.10+ | target: Ubuntu Linux VPS | runtime: asyncio
# xat.com lotador de sala via navegador headless (Playwright/Chromium).
# Cada aba carrega a sala no cliente REAL -> l5 e id sao computados pelo JS
# oficial, sem precisar reimplementar o protocolo. Rode na VPS.

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field

from playwright.async_api import async_playwright, Page

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    _RICH = True
except ImportError:
    _RICH = False


@dataclass
class Config:
    room: str
    count: int = 10
    proxy: str | None = None          # scheme://user:pass@host:port
    headless: bool = True
    connect_delay: float = 1.0        # segundos entre abas (evita rate limit)
    url_timeout: int = 45_000
    viewport: tuple = (900, 700)
    keepalive_check: float = 15.0


def parse_args(argv) -> Config:
    ap = argparse.ArgumentParser(
        prog="xat_flood_browser",
        description="Lotador xat.com via navegador headless (cliente real)")
    ap.add_argument("--room", required=True)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--proxy", default=None,
                    help="scheme://user:pass@host:port (ex: socks5://...)")
    ap.add_argument("--headed", action="store_true", help="mostra o navegador")
    ap.add_argument("--connect-delay", type=float, default=1.0)
    args = ap.parse_args(argv)
    return Config(room=args.room, count=args.count, proxy=args.proxy,
                  headless=not args.headed, connect_delay=args.connect_delay)


def _status(page: Page) -> str:
    try:
        if page.is_closed():
            return "FECHADA"
        url = page.url
        if "/web_gear/chat" in url or "xat.com" not in url:
            return "CARREGANDO"
        # heuristica: se ha um iframe do chat, entrou
        return "ABERTA"
    except Exception:
        return "?"


def render(cfg: Config, pages: list, start: float, extra: str = "") -> str:
    now = time.time()
    open_ = sum(1 for p in pages if not p.is_closed())
    lines = [
        f"XAT FLOOD (browser)  sala={cfg.room}  alvo={cfg.count}  abas={open_}/{len(pages)}  uptime={int(now-start)}s",
        "-" * 70,
    ]
    for i, p in enumerate(pages):
        st = _status(p)
        lines.append(f"{i+1:>3}  {st:<12} {p.url[:60] if not p.is_closed() else ''}")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


async def amain(cfg: Config):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=cfg.headless,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx_kwargs = {"viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]}}
        if cfg.proxy:
            ctx_kwargs["proxy"] = {"server": cfg.proxy}
        context = await browser.new_context(**ctx_kwargs)

        pages: list[Page] = []
        start = time.time()

        async def spawn():
            for i in range(cfg.count):
                page = await context.new_page()
                pages.append(page)
                try:
                    await page.goto(f"https://xat.com/{cfg.room}",
                                    wait_until="domcontentloaded",
                                    timeout=cfg.url_timeout)
                except Exception as e:
                    print(f"[aba {i+1}] erro ao carregar: {e}")
                print(f"[aba {i+1}/{cfg.count}] aberta -> {page.url[:70]}")
                await asyncio.sleep(cfg.connect_delay)

        await spawn()
        print("\nTodas as abas abertas. Mantendo vivas (Ctrl+C para encerrar).\n")

        while True:
            # reabre abas fechadas
            closed = [p for p in pages if p.is_closed()]
            for p in closed:
                idx = pages.index(p)
                try:
                    np_ = await context.new_page()
                    pages[idx] = np_
                    await np_.goto(f"https://xat.com/{cfg.room}",
                                   wait_until="domcontentloaded",
                                   timeout=cfg.url_timeout)
                    print(f"[aba {idx+1}] reaberta")
                except Exception as e:
                    print(f"[aba {idx+1}] reabertura falhou: {e}")
            print(render(cfg, pages, start))
            await asyncio.sleep(cfg.keepalive_check)


def main():
    cfg = parse_args(sys.argv[1:])
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        print("\nencerrado.")


if __name__ == "__main__":
    main()
