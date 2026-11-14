"""
MÓDULO: BROWSER VIRTUALIZATION & NET FILTER (BrowserEngine)
=============================================================================
OBJETIVO:
Gerenciar a sessão do Playwright Chromium headless em sites de produção reais,
blindando a IA contra redes de anúncios invasivas, isolando a GPU física e
obtendo configurações centralizadas via ConfigManager (default: Google.com).

FLUXO DA ARQUITETURA:
1. Inicialização Headless com flags de CPU (--disable-gpu) para poupar VRAM
   -> 2. Adblocker Nativo: Intercepta rotas de rede (route.abort) contra mais de 30 redes de ads/trackers.
   -> 3. Popup & Tab Killer: Escuta context.on("page") e fecha abas popunder disfarçadas em <100ms.
   -> 4. Execução de Ações Atômicas: Navegação, clique, preenchimento com press_enter opcional 
         e go_back com pacing de estabilização.
   -> 5. Captura Perceptiva: Gera screenshots PNG e snapshots limpos via DOMSimplifier.
=============================================================================
"""
import time
import re
import urllib.parse
from typing import Dict, Any, Optional, List
from pathlib import Path

from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext, Page
from src.config_manager import get_config
from src.web.dom_simplifier import DOMSimplifier, DOMSnapshot


class BrowserEngine:
    """Motor de automação web seguro com isolamento de contexto e poda de DOM."""

    AD_PATTERNS = [
        "doubleclick.net", "google-analytics.com", "googlesyndication.com",
        "popads.net", "popcash.net", "propellerads.com", "exoclick.com",
        "adsterra.com", "trafficjunky.com", "onclicktop.com", "hilltopads.com",
        "monetag.com", "histats.com", "clickadu.com", "adstera", "yadro.ru",
        "streamtape.com", "voe.sx", "disqus.com", "mc.yandex.ru", "scorecardresearch.com",
        "bet365", "1xbet", "blaze.com", "betano", "ad-delivery", "creative-serving",
        "a-ads.com", "coinhive", "adservice", "pagead"
    ]

    def __init__(
        self,
        headless: Optional[bool] = None,
        base_url: Optional[str] = None,
        enable_adblock: Optional[bool] = None
    ):
        cfg = get_config()
        self.headless = headless if headless is not None else cfg.get("browser.headless", True)
        raw_base = base_url if base_url is not None else cfg.get("browser.default_base_url", "https://www.google.com")
        self.base_url = raw_base.rstrip("/") if raw_base else "https://www.google.com"
        self.enable_adblock = enable_adblock if enable_adblock is not None else cfg.get("browser.enable_adblock", True)
        self.simplifier = DOMSimplifier()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self.current_selector_map: Dict[str, str] = {}
        self.last_dialog_message: Optional[str] = None
        self._pending_dialog_action: str = "accept"
        self._pending_prompt_text: Optional[str] = None

    def start(self):
        """Inicializa o processo do Chromium com flags para preservar VRAM da GPU."""
        if not self._playwright:
            self._playwright = sync_playwright().start()
            # Flags de isolamento para rodar em RAM sem disputar GPU com o modelo local
            args = [
                "--disable-gpu",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                args=args
            )
            self.reset_context()

    def reset_context(self, auth_credentials: Optional[Dict[str, str]] = None):
        """Cria um BrowserContext 100% isolado (sem cookies de sessões anteriores)."""
        if not self._browser:
            self.start()

        if self._context:
            try:
                self._context.close()
            except Exception:
                pass

        cfg = get_config()
        user_agent = cfg.get(
            "browser.user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        opts: Dict[str, Any] = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": user_agent
        }
        if auth_credentials:
            opts["http_credentials"] = {
                "username": auth_credentials.get("username", ""),
                "password": auth_credentials.get("password", "")
            }

        self._context = self._browser.new_context(**opts)
        self._context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        self._page = self._context.new_page()

        # Adblocker Nativo no Nível de Rede e Interceptador de Pop-ups
        if self.enable_adblock:
            def _handle_route(route):
                req_url = route.request.url.lower()
                res_type = route.request.resource_type
                if any(ad in req_url for ad in self.AD_PATTERNS):
                    try:
                        route.abort()
                    except Exception:
                        pass
                    return
                if res_type in ("beacon", "csp_report"):
                    try:
                        route.abort()
                    except Exception:
                        pass
                    return
                try:
                    route.continue_()
                except Exception:
                    pass

            self._context.route("**/*", _handle_route)

            def _handle_popup(new_page):
                try:
                    time.sleep(0.1)
                    new_page.close()
                except Exception:
                    pass

            self._context.on("page", _handle_popup)

        # Listener para diálogos JS (alert, confirm, prompt)
        def _dialog_handler(dialog):
            self.last_dialog_message = dialog.message
            if self._pending_dialog_action == "dismiss":
                dialog.dismiss()
            else:
                dialog.accept(self._pending_prompt_text)

        self._page.on("dialog", _dialog_handler)

    def stop(self):
        """Encerra contexto, browser e processo do Playwright."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None

        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _resolve_selector(self, selector: str) -> str:
        """Traduz identificador sintético (@1, @username) ou string descritiva para seletor CSS real."""
        clean = selector.strip()

        # Normaliza sintaxes estilo id('nome') ou id("nome") ou id#nome
        clean = re.sub(r"id\(['\"]?([^'\")]+)['\"]?\)", r"#\1", clean)
        clean = re.sub(r"^id#", "#", clean)

        # Redireciona input móvel #ms para desktop #s se presente
        if clean in ("@ms", "#ms"):
            clean = "#s"

        if clean in self.current_selector_map:
            return self.current_selector_map[clean]

        # Extrai @id se o modelo enviou texto como "link @4 href=..." ou "[button @1]"
        match_at = re.search(r"(@[a-zA-Z0-9_-]+)", clean)
        if match_at and match_at.group(1) in self.current_selector_map:
            return self.current_selector_map[match_at.group(1)]

        # Extrai href se presente no texto da tool call (ex: link href='status_codes/404')
        match_href = re.search(r"href=['\"]([^'\"]+)['\"]", clean)
        if match_href:
            return f"a[href*='{match_href.group(1)}']"

        # Fallback para @id direto no DOM como #id
        if clean.startswith("@"):
            raw_id = clean[1:]
            return f"#{raw_id}"

        return clean

    def _get_current_snapshot(self) -> DOMSnapshot:
        """Captura o HTML da página e gera o snapshot podado com mapa de seletores."""
        try:
            if self.enable_adblock:
                try:
                    self._page.evaluate("""
                        () => {
                            const sel = ['.ad', '.ads', '.advertisement', '.banner-ad', '[class*="pop"]', '[id*="pop"]', '[id*="ad-"]', 'iframe[src*="ad"]'];
                            document.querySelectorAll(sel.join(',')).forEach(el => el.remove());
                        }
                    """)
                except Exception:
                    pass
            html = self._page.content()
        except Exception:
            html = "<html><body></body></html>"
        snapshot = self.simplifier.simplify(html)
        self.current_selector_map = snapshot.selector_map
        return snapshot

    def navigate(self, url: str, timeout_ms: int = 20000) -> Dict[str, Any]:
        """Navega para a URL e retorna título e snapshot semântico após estabilização."""
        target_url = url
        if target_url.startswith("/"):
            if self.base_url:
                target_url = f"{self.base_url}{target_url}"
            elif self._page and self._page.url and self._page.url != "about:blank":
                parsed = urllib.parse.urlparse(self._page.url)
                target_url = f"{parsed.scheme}://{parsed.netloc}{target_url}"
            else:
                target_url = f"https://www.google.com{target_url}"

        try:
            resp = self._page.goto(target_url, timeout=timeout_ms, wait_until="domcontentloaded")
            time.sleep(0.2)  # Tempo de espera para renderização e estabilização de DOM
            status = resp.status if resp else 200
            title = self._page.title()
            snapshot = self._get_current_snapshot()
            return {
                "success": True,
                "url": self._page.url,
                "status": status,
                "title": title,
                "dom_snapshot": snapshot
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "url": url,
                "status": 500,
                "title": "",
                "dom_snapshot": DOMSnapshot(pruned_text="[Erro de Navegacao]")
            }

    def click(self, selector: str, timeout_ms: int = 3000) -> Dict[str, Any]:
        """Clica no elemento com validacao rapida em 2 passadas para falha imediata caso inexistente."""
        real_sel = self._resolve_selector(selector)
        try:
            # Passada 1: verifica contagem imediata no DOM sem auto-waiting bloqueante
            vis_loc = self._page.locator(f"{real_sel}:visible")
            has_element = (vis_loc.count() > 0)
            if not has_element:
                base_loc = self._page.locator(real_sel)
                has_element = (base_loc.count() > 0)

            # Se nao encontrou, concede tolerancia unica de 300ms para montagem assincrona
            if not has_element:
                try:
                    self._page.wait_for_timeout(300)
                except Exception:
                    pass
                vis_loc = self._page.locator(f"{real_sel}:visible")
                has_element = (vis_loc.count() > 0) or (self._page.locator(real_sel).count() > 0)

            if not has_element:
                valid_samples = list(self.current_selector_map.keys())[:15]
                valid_str = ", ".join(valid_samples) if valid_samples else "nenhum"
                return {
                    "success": False,
                    "error": f"Recurso/elemento não encontrado: seletor '{selector}' (resolvido como '{real_sel}') não existe na página. Seletores válidos disponíveis: {valid_str}",
                    "target": selector,
                    "resolved_selector": real_sel
                }

            loc = vis_loc.first if vis_loc.count() > 0 else self._page.locator(real_sel).first
            loc.click(timeout=timeout_ms)
            time.sleep(0.3)
            snapshot = self._get_current_snapshot()
            return {
                "success": True,
                "action": "click",
                "target": selector,
                "resolved_selector": real_sel,
                "dom_snapshot": snapshot
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "target": selector,
                "resolved_selector": real_sel
            }

    def fill(self, selector: str, value: str, press_enter: bool = False, timeout_ms: int = 3000) -> Dict[str, Any]:
        """Limpa e preenche o input com validacao rapida em 2 passadas para falha imediata caso inexistente."""
        real_sel = self._resolve_selector(selector)
        try:
            # Passada 1: verifica contagem imediata no DOM sem auto-waiting bloqueante
            vis_loc = self._page.locator(f"{real_sel}:visible")
            has_element = (vis_loc.count() > 0)
            if not has_element:
                base_loc = self._page.locator(real_sel)
                has_element = (base_loc.count() > 0)

            # Se nao encontrou, concede tolerancia unica de 300ms para montagem assincrona
            if not has_element:
                try:
                    self._page.wait_for_timeout(300)
                except Exception:
                    pass
                vis_loc = self._page.locator(f"{real_sel}:visible")
                has_element = (vis_loc.count() > 0) or (self._page.locator(real_sel).count() > 0)

            if not has_element:
                valid_samples = list(self.current_selector_map.keys())[:15]
                valid_str = ", ".join(valid_samples) if valid_samples else "nenhum"
                return {
                    "success": False,
                    "error": f"Recurso/elemento não encontrado: seletor '{selector}' (resolvido como '{real_sel}') não existe na página. Seletores válidos disponíveis: {valid_str}",
                    "target": selector,
                    "resolved_selector": real_sel
                }

            loc = vis_loc.first if vis_loc.count() > 0 else self._page.locator(real_sel).first

            el_type = ""
            try:
                el_type = (loc.get_attribute("type") or "").lower()
            except Exception:
                pass

            if el_type in ("checkbox", "radio") or "checkbox" in real_sel.lower():
                check_it = value in (True, "true", "True", "1", 1, "yes", "sim", "", None)
                try:
                    loc.set_checked(check_it, timeout=timeout_ms)
                except Exception:
                    loc.click(timeout=timeout_ms)
            else:
                loc.fill(value, timeout=timeout_ms)
                should_enter = press_enter or (selector in ("@s", "@ms", "#s", "#ms") or "search" in real_sel.lower())
                if should_enter or press_enter:
                    loc.press("Enter")

            time.sleep(0.5 if (press_enter or should_enter) else 0.2)
            snapshot = self._get_current_snapshot()
            return {
                "success": True,
                "action": "fill",
                "target": selector,
                "resolved_selector": real_sel,
                "value": value,
                "pressed_enter": press_enter,
                "dom_snapshot": snapshot
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "target": selector,
                "resolved_selector": real_sel
            }

    def fill_form(self, fields: Dict[str, Any], timeout_ms: int = 5000) -> Dict[str, Any]:
        """Preenche em lote múltiplos campos (texto, select, checkbox/radio) de uma só vez."""
        results = {}
        for sel, val in fields.items():
            real_sel = self._resolve_selector(sel)
            try:
                loc = self._page.locator(real_sel).first
                tag_name = ""
                el_type = ""
                try:
                    tag_name = loc.evaluate("el => el.tagName.toLowerCase()")
                    el_type = (loc.get_attribute("type") or "").lower()
                except Exception:
                    pass

                if el_type in ("checkbox", "radio") or "checkbox" in real_sel.lower():
                    check_it = val in (True, "true", "True", "1", 1, "yes", "sim", "")
                    try:
                        loc.set_checked(check_it, timeout=timeout_ms)
                    except Exception:
                        loc.click(timeout=timeout_ms)
                    results[sel] = {"status": "checked" if check_it else "unchecked", "success": True}
                elif tag_name == "select" or "select" in real_sel.lower():
                    try:
                        loc.select_option(value=str(val), timeout=timeout_ms)
                    except Exception:
                        loc.select_option(label=str(val), timeout=timeout_ms)
                    results[sel] = {"status": f"selected:{val}", "success": True}
                else:
                    loc.fill(str(val), timeout=timeout_ms)
                    results[sel] = {"status": f"filled:{val}", "success": True}
            except Exception as e:
                results[sel] = {"success": False, "error": str(e)}

        time.sleep(0.2)  # Estabilização pós-injeção
        snapshot = self._get_current_snapshot()
        all_ok = all(r.get("success", False) for r in results.values()) if results else False
        return {
            "success": all_ok,
            "action": "fill_form",
            "field_results": results,
            "dom_snapshot": snapshot
        }

    def select_option(self, selector: str, value: str, timeout_ms: int = 10000) -> Dict[str, Any]:
        """Seleciona item em elemento select por value ou label com pacing de 0.5s."""
        real_sel = self._resolve_selector(selector)
        try:
            target_locator = real_sel
            if real_sel.lower().startswith("option") or " option" in real_sel.lower():
                target_locator = "select"

            try:
                self._page.select_option(target_locator, value=value, timeout=timeout_ms)
            except Exception:
                self._page.select_option(target_locator, label=value, timeout=timeout_ms)

            time.sleep(0.2)  # Pacing de 0.2s entre interações
            snapshot = self._get_current_snapshot()
            return {
                "success": True,
                "action": "select_option",
                "target": selector,
                "selected": value,
                "dom_snapshot": snapshot
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "target": selector
            }

    def sleep(self, seconds: float, reason: str = "") -> Dict[str, Any]:
        """Pausa deliberada solicitada pelo modelo."""
        capped_seconds = min(max(0.1, seconds), 10.0)
        time.sleep(capped_seconds)
        snapshot = self._get_current_snapshot()
        return {
            "success": True,
            "action": "sleep",
            "slept_seconds": capped_seconds,
            "reason": reason,
            "dom_snapshot": snapshot
        }

    def press_key(self, key: str) -> Dict[str, Any]:
        """Envia evento de tecla para o navegador com pacing de 0.5s."""
        try:
            self._page.keyboard.press(key)
            time.sleep(0.2)
            snapshot = self._get_current_snapshot()
            return {
                "success": True,
                "action": "press_key",
                "key": key,
                "dom_snapshot": snapshot
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "key": key
            }

    def extract_data(self, selector: str, data_type: str = "text", attribute_name: Optional[str] = None) -> Dict[str, Any]:
        """Extrai texto, tabela ou atributo de elemento."""
        real_sel = self._resolve_selector(selector)
        try:
            el = self._page.locator(real_sel).first
            if data_type == "attribute" and attribute_name:
                if attribute_name.lower() == "value":
                    try:
                        val = el.input_value()
                    except Exception:
                        val = el.get_attribute(attribute_name)
                else:
                    val = el.get_attribute(attribute_name)
            elif data_type == "table":
                rows = el.locator("tr").all()
                val = [[col.inner_text().strip() for col in row.locator("th, td").all()] for row in rows]
            else:
                val = el.inner_text()
            return {
                "success": True,
                "target": selector,
                "resolved_selector": real_sel,
                "data_type": data_type,
                "data": val
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "target": selector
            }

    def wait_for(self, selector: str, timeout_ms: int = 10000, state: str = "visible") -> Dict[str, Any]:
        """Aguarda a visibilidade ou alteração de um seletor."""
        real_sel = self._resolve_selector(selector)
        try:
            self._page.wait_for_selector(real_sel, timeout=timeout_ms, state=state)
            snapshot = self._get_current_snapshot()
            return {
                "success": True,
                "action": "wait_for",
                "target": selector,
                "resolved_selector": real_sel,
                "dom_snapshot": snapshot
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "target": selector
            }

    def handle_dialog(self, action: str = "accept", prompt_text: Optional[str] = None) -> Dict[str, Any]:
        """Define o comportamento para a próxima caixa de diálogo JS."""
        self._pending_dialog_action = action
        self._pending_prompt_text = prompt_text
        return {
            "success": True,
            "action": "handle_dialog",
            "dialog_action": action,
            "prompt_text": prompt_text
        }

    def scroll(self, direction: str = "down", amount_px: int = 500) -> Dict[str, Any]:
        """Rola a página verticalmente."""
        try:
            delta_y = amount_px if direction == "down" else -amount_px
            self._page.mouse.wheel(0, delta_y)
            time.sleep(0.3)
            snapshot = self._get_current_snapshot()
            return {
                "success": True,
                "action": "scroll",
                "direction": direction,
                "amount_px": amount_px,
                "dom_snapshot": snapshot
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def get_url(self) -> str:
        """Retorna a URL atual da página."""
        return self._page.url if self._page else ""

    def count_elements(self, selector: str) -> int:
        """Conta a quantidade de elementos correspondentes ao seletor."""
        real_sel = self._resolve_selector(selector)
        try:
            return self._page.locator(real_sel).count()
        except Exception:
            return 0

    def is_checked(self, selector: str) -> bool:
        """Retorna se o elemento (checkbox/radio) está marcado."""
        real_sel = self._resolve_selector(selector)
        try:
            return self._page.locator(real_sel).first.is_checked()
        except Exception:
            return False

    def go_back(self, timeout_ms: int = 10000) -> Dict[str, Any]:
        """Retorna para a página anterior no histórico do navegador."""
        try:
            self._page.go_back(timeout=timeout_ms, wait_until="domcontentloaded")
            time.sleep(0.3)
            snapshot = self._get_current_snapshot()
            return {
                "success": True,
                "action": "go_back",
                "url": self._page.url,
                "title": self._page.title(),
                "dom_snapshot": snapshot
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def take_screenshot(self) -> bytes:
        """Captura imagem da tela em bytes PNG."""
        try:
            return self._page.screenshot(type="png", full_page=False)
        except Exception:
            return b""

    def discover_batch_links(self, pattern: str, max_items: int = 0) -> List[Dict[str, str]]:
        """Descobre dinamicamente no DOM da página atual links correspondentes a um padrão.
        max_items <= 0 ou ausente significa sem limite (coleta todos os itens correspondentes).
        """
        if not self._page:
            return []
        try:
            return self._page.evaluate("""({pattern, max_items}) => {
                const results = [];
                const seen = new Set();
                const anchors = Array.from(document.querySelectorAll('a'));
                for (let a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (!href || href === '#' || href.startsWith('javascript:')) continue;
                    const matches = pattern.startsWith('/') || !pattern.includes('[') 
                        ? href.includes(pattern) 
                        : a.matches(pattern);
                    if (matches && !seen.has(href)) {
                        seen.add(href);
                        results.push({
                            href: href,
                            text: a.innerText.replace(/\\s+/g, ' ').trim()
                        });
                        if (max_items > 0 && results.length >= max_items) break;
                    }
                }
                return results;
            }""", {"pattern": pattern, "max_items": max_items})
        except Exception:
            return []
