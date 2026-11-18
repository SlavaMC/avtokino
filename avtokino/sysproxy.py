"""Системный прокси для загрузок из интернета (модели Whisper и т. п.).

Порядок:
  1. явный --proxy (адрес или off);
  2. уже заданные переменные HTTPS_PROXY / HTTP_PROXY / ALL_PROXY — их не трогаем;
  3. Windows: настройки «Параметры → Сеть → Прокси» через WinHTTP — ручной прокси,
     сценарий настройки (PAC, AutoConfigURL) и автоопределение (WPAD);
  4. прочие ОС: urllib.request.getproxies() (переменные, на macOS — системные настройки).

Найденный прокси записывается в переменные окружения HTTPS_PROXY/HTTP_PROXY (и NO_PROXY для
исключений), которые читают httpx/requests внутри huggingface_hub.
"""
from __future__ import annotations

import os
import sys
import urllib.request

ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def parse_proxy_list(text: str | None, scheme: str = "https") -> str | None:
    """Строка Windows/PAC → адрес прокси.

    "host:port", "http=h:1;https=h:2", "PROXY h:3; DIRECT", "DIRECT" → None.
    """
    if not text:
        return None
    entries = [e.strip() for e in text.replace(" ;", ";").replace(",", ";").split(";") if e.strip()]
    named: dict[str, str] = {}
    plain: list[str] = []
    for e in entries:
        up = e.upper()
        if up == "DIRECT":
            plain.append("")
            continue
        if up.startswith(("PROXY ", "HTTPS ")):
            plain.append(e.split(None, 1)[1].strip())
            continue
        if up.startswith(("SOCKS", "SOCKS5 ")):
            continue  # SOCKS через httpx без доп. пакета не работает
        if "=" in e and "://" not in e.split("=", 1)[0]:
            k, v = e.split("=", 1)
            named[k.strip().lower()] = v.strip()
        else:
            plain.append(e)
    addr = named.get(scheme) or named.get("http") or (plain[0] if plain else None)
    if not addr:
        return None
    return addr if "://" in addr else "http://" + addr


def bypass_to_no_proxy(text: str | None) -> str:
    """Исключения Windows ("<local>;*.corp.ru;10.*") → NO_PROXY ("localhost,127.0.0.1,.corp.ru")."""
    out: list[str] = []
    for e in (text or "").replace(" ", ";").replace(",", ";").split(";"):
        e = e.strip()
        if not e:
            continue
        if e.lower() == "<local>":
            out += ["localhost", "127.0.0.1", "::1"]
        elif e.endswith(".*"):
            continue  # маски по IP NO_PROXY не понимает
        elif e.startswith("*."):
            out.append(e[1:])  # *.corp.ru → .corp.ru
        else:
            out.append(e.lstrip("*"))
    return ",".join(dict.fromkeys(out))


def _winhttp(url: str) -> tuple[str | None, str, str]:
    """(прокси, исключения, откуда) по системным настройкам Windows для url."""
    import ctypes
    from ctypes import wintypes

    class IEConfig(ctypes.Structure):
        _fields_ = [("fAutoDetect", wintypes.BOOL), ("lpszAutoConfigUrl", wintypes.LPWSTR),
                    ("lpszProxy", wintypes.LPWSTR), ("lpszProxyBypass", wintypes.LPWSTR)]

    class AutoOpts(ctypes.Structure):
        _fields_ = [("dwFlags", wintypes.DWORD), ("dwAutoDetectFlags", wintypes.DWORD),
                    ("lpszAutoConfigUrl", wintypes.LPCWSTR), ("lpvReserved", ctypes.c_void_p),
                    ("dwReserved", wintypes.DWORD), ("fAutoLogonIfChallenged", wintypes.BOOL)]

    class ProxyInfo(ctypes.Structure):
        _fields_ = [("dwAccessType", wintypes.DWORD), ("lpszProxy", wintypes.LPWSTR),
                    ("lpszProxyBypass", wintypes.LPWSTR)]

    w = ctypes.WinDLL("winhttp", use_last_error=True)
    k = ctypes.WinDLL("kernel32")
    k.GlobalFree.argtypes = [ctypes.c_void_p]
    w.WinHttpOpen.restype = ctypes.c_void_p
    w.WinHttpOpen.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    w.WinHttpGetProxyForUrl.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(AutoOpts),
                                        ctypes.POINTER(ProxyInfo)]
    w.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]

    def take(p) -> str | None:
        if not p:
            return None
        s = ctypes.wstring_at(ctypes.cast(p, ctypes.c_void_p).value)
        k.GlobalFree(ctypes.cast(p, ctypes.c_void_p))
        return s

    ie = IEConfig()
    if not w.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(ie)):
        return None, "", ""
    auto_url = take(ie.lpszAutoConfigUrl)
    manual = take(ie.lpszProxy)
    bypass = take(ie.lpszProxyBypass) or ""

    if ie.fAutoDetect or auto_url:
        opts = AutoOpts()
        if auto_url:
            opts.dwFlags = 2  # WINHTTP_AUTOPROXY_CONFIG_URL
            opts.lpszAutoConfigUrl = auto_url
        if ie.fAutoDetect:
            opts.dwFlags |= 1  # WINHTTP_AUTOPROXY_AUTO_DETECT
            opts.dwAutoDetectFlags = 1 | 2  # DHCP | DNS_A
        opts.fAutoLogonIfChallenged = True
        h = w.WinHttpOpen("avtokino", 1, None, None, 0)  # WINHTTP_ACCESS_TYPE_NO_PROXY
        if h:
            try:
                info = ProxyInfo()
                if w.WinHttpGetProxyForUrl(h, url, ctypes.byref(opts), ctypes.byref(info)):
                    proxy = take(info.lpszProxy)
                    pb = take(info.lpszProxyBypass) or bypass
                    how = "сценарий " + auto_url if auto_url else "автоопределение"
                    if info.dwAccessType == 3 and proxy:  # WINHTTP_ACCESS_TYPE_NAMED_PROXY
                        return parse_proxy_list(proxy), pb, how
                    return None, pb, how + " (без прокси)"
            finally:
                w.WinHttpCloseHandle(h)
    if manual:
        return parse_proxy_list(manual), bypass, "настройки Windows"
    return None, bypass, ""


def apply(proxy_opt: str | None = None, url: str = "https://huggingface.co/") -> str:
    """Настроить прокси в окружении; вернуть строку для журнала ('' — прямое подключение)."""
    if proxy_opt:
        if proxy_opt.lower() in ("off", "none", "direct", "no"):
            for key in ENV_KEYS:
                os.environ.pop(key, None)
            os.environ["NO_PROXY"] = "*"
            return ""
        addr = proxy_opt if "://" in proxy_opt else "http://" + proxy_opt
        return _set(addr, "", "--proxy")
    for key in ENV_KEYS:
        if os.environ.get(key):
            return f"{_mask(os.environ[key])} (переменная {key})"
    addr, bypass, how = None, "", ""
    if sys.platform == "win32":
        try:
            addr, bypass, how = _winhttp(url)
        except Exception as e:  # noqa: BLE001 — нет WinHTTP / странные настройки: работаем без прокси
            print(f"  (не удалось прочитать системный прокси: {e})", file=sys.stderr)
    if addr is None and not how:
        sysp = urllib.request.getproxies()
        addr = sysp.get("https") or sysp.get("http")
        addr = addr if not addr or "://" in addr else "http://" + addr
        bypass, how = sysp.get("no", ""), "системные настройки"
    if not addr:
        return ""
    return _set(addr, bypass_to_no_proxy(bypass) if sys.platform == "win32" else bypass, how)


def _set(addr: str, no_proxy: str, how: str) -> str:
    os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = addr
    if no_proxy and not os.environ.get("NO_PROXY"):
        os.environ["NO_PROXY"] = no_proxy
    return f"{_mask(addr)} ({how})"


def _mask(addr: str) -> str:
    """Скрыть пароль в журнале: http://user:pass@h:1 → http://user:***@h:1."""
    import re
    return re.sub(r"(//[^:/@]+:)[^@/]*@", r"\1***@", addr)
