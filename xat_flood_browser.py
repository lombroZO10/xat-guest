#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# language: Python 3.10+ | target: Ubuntu Linux VPS | runtime: asyncio
# xat.com lotador de sala via navegador headless (Playwright/Chromium).
# - contexto + proxy proprio por conexao (IP distinto)
# - spawn paralelo (limitado por --concurrency)
# - carrega so o box do chat (embed.html) pra economizar CPU/RAM
# - "acao" periodica (mensagem) registra o id do guest e segura vivo

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field

from playwright.async_api import async_playwright, Page, BrowserContext


@dataclass
class Config:
    room: str
    count: int = 10
    proxies: list = field(default_factory=list)
    headless: bool = True
    connect_delay: float = 1.0
    url_timeout: int = 60_000
    challenge_wait: float = 25.0
    viewport: tuple = (700, 520)     # menor = menos CPU
    concurrency: int = 8             # abas simultaneas no spawn
    settle_concurrency: int = 2      # (legado, sem uso agora)
    batch: int = 3                   # conexoes por lote (3 segura a CPU da VPS)
    keepalive: float = 45.0          # segundos entre "acoes" (abaixo do idle kick)
    msg: str = "Dia 25 e oque? e 22 Flavioooo!"
    block_media: bool = True         # bloqueia imagem/fonte/midia (economiza proxy)
    block_css: bool = False          # bloqueia CSS tambem (mais economia, risco)
    new_headless: bool = False       # channel=chromium (headless novo, furtivo)
    deep_freeze: float = 0.0         # s: mata os timers N s apos assentar


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

def gen_name(i: int) -> str:
    # guest 1 = "...22!", guest 2 = "...22!!", etc.
    return "Dia 25 e oque? 22" + "!" * (i + 1)


def stealth_js(name: str) -> str:
    inject = ("try { const ks = ['w_name','name','xat_name','nickname','guest_name']; "
              "for (const k of ks) { try { localStorage.setItem(k, "
              + json.dumps(name) + "); } catch(e){} } } catch(e){}")
    return STEALTH_JS + "\n" + inject


TRACKER_HOSTS = ("google-analytics", "googletagmanager", "doubleclick",
                 "googlesyndication", "facebook.com/tr", "hotjar",
                 "mixpanel", "amplitude", "sentry.io")


def _norm_proxy(p: str) -> str:
    p = p.strip()
    if p and "://" not in p:
        p = "socks5://" + p
    return p


def parse_args(argv) -> Config:
    ap = argparse.ArgumentParser(
        prog="xat_flood_browser",
        description="Lotador xat.com via navegador headless (IP por conexao)")
    ap.add_argument("--room", required=True)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--proxy", action="append", default=[],
                    help="proxy scheme://user:pass@host:port (repita p/ varios, ou virgula)")
    ap.add_argument("--proxy-file", default=None,
                    help="arquivo JSON (chave 'endpoints') ou TXT (um por linha)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--connect-delay", type=float, default=1.0)
    ap.add_argument("--challenge-wait", type=float, default=25.0)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--settle-concurrency", type=int, default=2,
                    help="reconnects simultaneos no assentamento (subir com cuidado)")
    ap.add_argument("--batch", type=int, default=3,
                    help="conexoes por lote (3 segura a CPU; suba se a VPS aguentar)")
    ap.add_argument("--keepalive", type=float, default=45.0,
                    help="intervalo (s) da 'acao' que segura o guest vivo")
    ap.add_argument("--msg", default="oi", help="mensagem da acao")
    ap.add_argument("--no-block-media", action="store_true",
                    help="nao bloquear imagem/fonte/midia (consome mais proxy)")
    ap.add_argument("--block-css", action="store_true",
                    help="bloqueia CSS tambem (economiza mais; teste antes)")
    ap.add_argument("--new-headless", action="store_true",
                    help="headless novo (channel=chromium) - mais furtivo que o "
                         "headless antigo; rode 'playwright install chromium' antes")
    ap.add_argument("--deep-freeze", type=float, default=0.0,
                    help="mata timers/JS N segundos apos o guest assentar "
                         "(CPU minima; a reconexao do chat deixa de funcionar)")
    args = ap.parse_args(argv)

    proxies = []
    for p in args.proxy:
        proxies.extend(_norm_proxy(x) for x in p.split(",") if x.strip())
    if args.proxy_file:
        data = open(args.proxy_file, encoding="utf-8").read().strip()
        try:
            obj = json.loads(data)
            items = obj.get("endpoints", obj) if isinstance(obj, dict) else obj
        except (json.JSONDecodeError, ValueError):
            items = data.splitlines()
        for x in items:
            x = _norm_proxy(str(x))
            if x:
                proxies.append(x)
    return Config(room=args.room, count=args.count, proxies=proxies,
                  headless=not args.headed, connect_delay=args.connect_delay,
                  challenge_wait=args.challenge_wait, concurrency=args.concurrency,
                  settle_concurrency=args.settle_concurrency, batch=args.batch,
                  keepalive=args.keepalive, msg=args.msg,
                  block_media=not args.no_block_media, block_css=args.block_css,
                  new_headless=args.new_headless, deep_freeze=args.deep_freeze)


class Slot:
    __slots__ = ("context", "page", "ws_count", "proxy", "actions", "name")

    def __init__(self, context: BrowserContext, page: Page, proxy: str | None,
                 name: str):
        self.context = context
        self.page = page
        self.ws_count = 0
        self.proxy = proxy
        self.actions = 0
        self.name = name

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
            if slot.ws_count == 2:
                print(f"    [{idx}] reconectou (id registrado)")

    page.on("websocket", on_ws)
    try:
        await page.goto(
            f"https://xat.com/content/web/R00253/box/embed.html?n={cfg.room}",
            wait_until="domcontentloaded", timeout=cfg.url_timeout)
    except Exception:
        return False

    t0 = time.time()
    cf_since = None
    while time.time() - t0 < cfg.challenge_wait:
        if slot.ws_count > 0:
            print(f"[{idx}] ENTROU (ws xat, proxy={slot.proxy or 'direto'}) "
                  f"em {int(time.time()-t0)}s")
            return True
        # cloudflare travado -> desiste rapido e parte pra proxima sessao
        if "__cf_chl" in page.url:
            if cf_since is None:
                cf_since = time.time()
            elif time.time() - cf_since > 12:
                print(f"[{idx}] cloudflare travado, pulando (proxy={slot.proxy or 'direto'})")
                return False
        else:
            cf_since = None
        await asyncio.sleep(1.0)

    url = page.url
    if "__cf_chl" in url:
        print(f"[{idx}] DESAFIO travado (proxy={slot.proxy or 'direto'}): {url[:55]}")
    else:
        print(f"[{idx}] sem ws xat (proxy={slot.proxy or 'direto'}): url={url[:45]}")
    return False


async def do_action(slot: Slot, cfg: Config) -> bool:
    """Envia mensagem com interacao REAL do Playwright (clique + digitacao),
    mas achando o elemento por query_selector (nao-bloqueante, sem wait_for).
    O page.keyboard puro digitava no frame errado; o click real foca certo."""
    page = slot.page
    sels = ("#textEntryEditable", "#sendmsg", "#sendMsg",
            "[contenteditable='true']", "[role='textbox']")
    frames = [f for f in page.frames if f != page.main_frame]
    frames.append(page.main_frame)
    for _ in range(2):
        for frame in frames:
            for sel in sels:
                try:
                    el = await frame.query_selector(sel)
                    if el is None:
                        continue
                    if not await el.is_visible():
                        continue
                    await el.click(timeout=2000)
                    await el.type(cfg.msg, delay=15)
                    await el.press("Enter")
                    slot.actions += 1
                    if slot.actions == 1:
                        print(f"    [acao ok] frame={frame.url[:45]} sel={sel}")
                    return True
                except Exception:
                    continue
        await asyncio.sleep(0.3)
    print("    [acao FALHOU] campo de mensagem nao encontrado")
    return False


async def settle(slot: Slot, cfg: Config, idx: int) -> bool:
    """Manda a mensagem (acao) que registra o id; espera um instante fixo."""
    print(f"    [{idx}] settle: enviando mensagem...")
    # manda a mensagem e espera a RECONEXAO (e ela que registra o id).
    # se nao reconectou em ~6s, manda de novo (ate 3x).
    for _ in range(3):
        ok = await do_action(slot, cfg)
        if ok:
            for _ in range(12):  # ate ~6s
                if slot.ws_count >= 2:
                    print(f"    [{idx}] id registrado")
                    return True
                await asyncio.sleep(0.5)
    print(f"    [{idx}] nao reconectou apos 3 mensagens")
    return False


async def change_name(slot: Slot, idx: int):
    """Troca o nome: clique real no li.friend -> dialog -> #nameedit -> Salvar.
    Playwright de verdade (o el.click() do JS nao abre o dialog do xat)."""
    page = slot.page
    name = slot.name

    async def find_click(sel: str, rounds: int) -> bool:
        for _ in range(rounds):
            frames = [f for f in page.frames if f != page.main_frame]
            frames.append(page.main_frame)
            for frame in frames:
                try:
                    loc = frame.locator(sel).first
                    await loc.wait_for(state="visible", timeout=900)
                    await loc.click(timeout=900)
                    return True
                except Exception:
                    continue
            await asyncio.sleep(0.3)
        return False

    # 1. clica no nome (abre o dialog)
    if not await find_click("li.friend", 10):
        print(f"    [nome] sem li.friend ({idx})")
        return

    # 2. preenche o #nameedit
    filled = False
    for _ in range(10):
        frames = [f for f in page.frames if f != page.main_frame]
        frames.append(page.main_frame)
        for frame in frames:
            try:
                ta = frame.locator("#nameedit").first
                await ta.wait_for(state="visible", timeout=900)
                await ta.click(timeout=900)
                await ta.press("Control+A", timeout=800)
                await ta.press("Backspace", timeout=800)
                await ta.type(name, delay=15, timeout=3000)
                await ta.press("Tab", timeout=800)
                filled = True
                break
            except Exception:
                continue
        if filled:
            break
        await asyncio.sleep(0.3)

    # 2b. confere o que ficou no campo (diagnostico do save)
    try:
        got = await page.evaluate("""() => {
            const findIn = (doc) => doc.querySelector('#nameedit');
            let el = findIn(document);
            if (!el) {
                for (const f of document.querySelectorAll('iframe')) {
                    try {
                        const d = f.contentDocument;
                        if (d) { el = findIn(d); if (el) break; }
                    } catch (e) {}
                }
            }
            return el ? el.value : null;
        }""")
        if got != name:
            print(f"    [nome] campo='{got}' (esperado '{name}')")
    except Exception:
        pass

    # 3. clica em Salvar
    await find_click("[data-localize='mob2.save']", 10)
    print(f"    [nome] {name}")


async def deep_freeze(slot: Slot) -> bool:
    """Mata timers/rAF de vez = CPU quase zero por aba.
    So usar com o guest ja ESTAVEL na sala: a reconexao do chat para de
    funcionar (se cair, so reabrindo a aba)."""
    try:
        await slot.page.evaluate("""() => {
            try {
                for (let i = 1; i < 300000; i++) { clearTimeout(i); clearInterval(i); }
                window.setTimeout = () => 0;
                window.setInterval = () => 0;
                window.requestAnimationFrame = () => 0;
            } catch (e) {}
        }""")
        return True
    except Exception:
        return False


async def deep_freeze_later(slot: Slot, cfg: Config, idx: int):
    await asyncio.sleep(cfg.deep_freeze)
    if slot.page.is_closed() or slot.ws_count == 0:
        return
    if await deep_freeze(slot):
        print(f"    [{idx}] congelado de vez (CPU minima)")


async def quiet_page(slot: Slot) -> bool:
    """Deixa a aba leve SEM quebrar a reconexao do chat:
    - congela animacoes e esconde o corpo (para de desenhar)
    - mata so o requestAnimationFrame (animacao)
    - MANTEM setTimeout/setInterval: a reconexao/heartbeat do chat depende deles
    """
    try:
        await slot.page.evaluate("""() => {
            try {
                document.querySelectorAll('*').forEach(e => {
                    e.style.animation = 'none';
                    e.style.transition = 'none';
                });
                if (document.body) document.body.style.display = 'none';
                window.requestAnimationFrame = () => 0;
            } catch (e) {}
        }""")
        return True
    except Exception:
        return False


async def keep_alive(slot: Slot, cfg: Config):
    while not slot.page.is_closed():
        await asyncio.sleep(cfg.keepalive)
        await do_action(slot, cfg)


def render(cfg: Config, slots: list, start: float) -> str:
    now = time.time()
    joined = sum(1 for s in slots if s is not None and not s.is_closed() and s.ws_count > 0)
    actions = sum(s.actions for s in slots if s is not None)
    open_ = sum(1 for s in slots if s is not None and not s.is_closed())
    lines = [
        f"XAT FLOOD  sala={cfg.room}  alvo={cfg.count}  abas={open_}/{len(slots)}  com-ws={joined}  acoes={actions}  uptime={int(now-start)}s",
        "-" * 72,
    ]
    for i, s in enumerate(slots):
        if s is None:
            lines.append(f"{i+1:>3}  AGUARDANDO")
        else:
            lines.append(f"{i+1:>3}  {s.status():<11} {s.name:<14} {s.proxy or 'direto'}")
    return "\n".join(lines)


async def amain(cfg: Config):
    async with async_playwright() as pw:
        launch_kwargs = dict(
            args=[
                "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions", "--mute-audio",
                # --- economia de CPU/RAM por aba (escala p/ mais conexoes) ---
                "--blink-settings=imagesEnabled=false",   # sem decode de imagem
                "--js-flags=--max-old-space-size=48",     # heap JS pequeno
                "--disable-accelerated-2d-canvas",
                "--disable-background-networking",
                "--disable-sync", "--no-first-run",
                "--disable-features=Translate,BackForwardCache,"
                "AcceptCHFrame,MediaRouter,OptimizationHints,"
                "CalculateNativeWinOcclusion",
            ])
        if cfg.new_headless:
            # headless NOVO (build chromium completo): mais furtivo que o
            # headless antigo (shell), que o Cloudflare pega na hora.
            launch_kwargs["channel"] = "chromium"
            launch_kwargs["headless"] = True
        else:
            launch_kwargs["headless"] = cfg.headless
        browser = await pw.chromium.launch(**launch_kwargs)

        def proxy_for(i: int) -> str | None:
            if not cfg.proxies:
                return None
            return cfg.proxies[i % len(cfg.proxies)]

        async def new_slot(proxy: str | None, name: str) -> Slot:
            ctx_kwargs = {"viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]}}
            if proxy:
                ctx_kwargs["proxy"] = {"server": proxy}
            context = await browser.new_context(**ctx_kwargs)
            await context.add_init_script(stealth_js(name))
            if cfg.block_media or cfg.block_css:
                async def route(route):
                    u = route.request.url
                    rt = route.request.resource_type
                    if cfg.block_media and rt in ("image", "font", "media"):
                        await route.abort()
                    elif cfg.block_media and any(h in u for h in TRACKER_HOSTS):
                        await route.abort()
                    elif cfg.block_css and rt == "stylesheet":
                        await route.abort()
                    else:
                        await route.continue_()
                await context.route("**/*", route)
            page = await context.new_page()
            return Slot(context, page, proxy, name)

        slots: list[Slot | None] = [None] * cfg.count
        start = time.time()

        async def enter(i: int) -> bool:
            slot = await new_slot(proxy_for(i), gen_name(i))
            slots[i] = slot
            try:
                return await load_slot(slot, cfg, i + 1)
            except Exception as e:
                print(f"[{i+1}] erro: {e}")
                return False

        # LOTE EM 3 FASES (como pedido):
        #   1) entram os N de uma vez
        #   2) da a acao em cada um pra registrar o id (todos primeiro)
        #   3) so quando TODOS tem id, troca os nomes
        #   depois parte pro proximo lote.
        for batch_start in range(0, cfg.count, cfg.batch):
            batch = list(range(batch_start, min(batch_start + cfg.batch, cfg.count)))
            n_lote = batch_start // cfg.batch + 1
            print(f"\n=== lote {n_lote}: {len(batch)} conexoes ===\n")

            # FASE 1: entra TODOS do lote de uma vez (paralelo)
            results = await asyncio.gather(*(enter(i) for i in batch))
            entered = [i for i, r in zip(batch, results) if r]
            if not entered:
                continue

            # FASE 2: acao em cada um pra registrar o id (todos primeiro)
            print(f"    [lote {n_lote}] registrando id de {len(entered)} guests...")
            ok_ids = []
            for i in entered:
                try:
                    await asyncio.wait_for(settle(slots[i], cfg, i + 1),
                                           timeout=30)
                    ok_ids.append(i)
                except asyncio.TimeoutError:
                    print(f"    [timeout] slot {i+1} travou no id, descartando")
                    try:
                        await slots[i].context.close()
                    except Exception:
                        pass
                    slots[i] = None
                except Exception as e:
                    print(f"    [erro id] slot {i+1}: {type(e).__name__} {e}")

            # FASE 2b: espera TODOS do lote reconectarem (ws_count >= 2) antes
            # de comecar a trocar nome. "reconectou" = id realmente registrado.
            if ok_ids:
                print(f"    [lote {n_lote}] esperando reconexao de "
                      f"{len(ok_ids)} guests...")
                deadline = time.time() + 60
                while time.time() < deadline:
                    pend = [i for i in ok_ids if slots[i].ws_count < 2]
                    if not pend:
                        break
                    await asyncio.sleep(0.5)
                faltou = [i + 1 for i in ok_ids if slots[i].ws_count < 2]
                if faltou:
                    print(f"    [lote {n_lote}] sem reconectar: {faltou}")

            # FASE 3: so agora troca o nome (nos que pegaram id)
            print(f"    [lote {n_lote}] alterando nomes de {len(ok_ids)} guests...")
            for i in ok_ids:
                try:
                    await asyncio.wait_for(change_name(slots[i], i + 1),
                                           timeout=45)
                except asyncio.TimeoutError:
                    print(f"    [timeout] slot {i+1} travou no nome, descartando")
                    try:
                        await slots[i].context.close()
                    except Exception:
                        pass
                    slots[i] = None
                except Exception as e:
                    print(f"    [erro nome] slot {i+1}: {type(e).__name__} {e}")
                if slots[i] is not None:
                    # espera o save E a reconexao com o nome novo assentarem
                    # antes de congelar a aba
                    await asyncio.sleep(6)
                    if not await quiet_page(slots[i]):
                        asyncio.create_task(keep_alive(slots[i], cfg))
                    else:
                        print(f"    [{i+1}] aba parada (CPU liberada)")
                        if cfg.deep_freeze > 0:
                            asyncio.create_task(
                                deep_freeze_later(slots[i], cfg, i + 1))

        print("\nTudo assenta. Mantendo vivos (Ctrl+C encerra).\n")

        while True:
            for i, s in enumerate(slots):
                if s is not None and s.is_closed():
                    try:
                        await s.context.close()
                    except Exception:
                        pass
                    ns = await new_slot(proxy_for(i), gen_name(i))
                    slots[i] = ns
                    try:
                        if await load_slot(ns, cfg, i + 1):
                            await settle(ns, cfg, i + 1)
                            await change_name(ns, i + 1)
                            asyncio.create_task(keep_alive(ns, cfg))
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
