"""Embedded Playwright browser for job applications — agent-optimized.

Design rules (why this is fast for an agent):

1. Every mutating action (click/fill/select/check/upload/type+Enter) returns
   the FRESH compact snapshot automatically — no separate browser_snapshot
   round-trip after each step, and element numbers never go stale.
2. Snapshots are surgical: interactive elements only, names capped, page text
   capped. Use browser_text() to read, browser_snapshot() to look,
   browser_find() to locate, browser_form() for application forms.
3. Popups/new tabs opened by an action are adopted automatically (job boards
   love target="_blank"), with browser_tabs to navigate between them.
4. Persistent profile (data/browser_profile) keeps board logins between runs.
   Headed by default — the user can step in for 2FA/captcha.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings

_TAGGER_JS = r"""
() => {
  const sel = 'a[href], button, input, select, textarea, [role="button"], '
            + '[role="link"], [role="tab"], [role="checkbox"], [role="radio"], '
            + '[role="dialog"], [role="listbox"], [role="option"], '
            + '[contenteditable="true"], [onclick]';
  const els = Array.from(document.querySelectorAll(sel));
  const out = [];
  const visible = (el) => {
    // file inputs are ALWAYS included even when display:none — boards hide
    // them behind custom buttons and uploads must reach them directly
    if (el.tagName === 'INPUT' && el.type === 'file') return true;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0 && el.tagName !== 'INPUT'
        && el.tagName !== 'SELECT' && el.tagName !== 'TEXTAREA') return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  els.forEach((el) => {
    if (!visible(el)) return;
    const n = out.length;
    el.setAttribute('data-wr-n', String(n));
    const name = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || el.innerText || el.value || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    out.push({
      n, tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || (el.tagName === 'SELECT' ? 'select' : null),
      name: name || null,
      value: (el.value || '').slice(0, 50) || null,
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).slice(0, 25).map(o => ({v: o.value, t: o.text.trim().slice(0, 50)}))
        : undefined,
    });
  });
  return {url: location.href, title: document.title, elements: out};
}
"""

_FORM_JS = r"""
() => {
  const forms = Array.from(document.forms);
  let best = null, bestCount = -1;
  for (const f of forms) {
    const count = f.querySelectorAll('input, select, textarea').length;
    if (count > bestCount) { best = f; bestCount = count; }
  }
  // Workday/Greenhouse often render without <form>: fall back to the DOM
  const root = (best && bestCount >= 3) ? best : document;
  const fields = [];
  let n = 0;
  document.querySelectorAll('[data-wr-n]').forEach(el => el.removeAttribute('data-wr-n'));
  for (const el of root.querySelectorAll('input, select, textarea')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0 && !el.id && !el.name) continue;
    if (el.type === 'hidden') continue;
    el.setAttribute('data-wr-n', String(n));
    let label = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) label = l.innerText.trim();
    }
    if (!label) {
      const wrap = el.closest('label, [data-automation-label], div, li, fieldset');
      if (wrap) label = (wrap.innerText || '').trim().split('\n')[0];
    }
    label = (label || el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0, 90);
    fields.push({
      n, tag: el.tagName.toLowerCase(), type: el.type || null, label,
      required: el.required || el.hasAttribute('aria-required'),
      value: (el.value || '').slice(0, 50) || null,
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).slice(0, 25).map(o => ({v: o.value, t: o.text.trim().slice(0, 50)}))
        : undefined,
    });
    n++;
  }
  return {url: location.href, fields,
          submit_buttons: Array.from(
            document.querySelectorAll('button, [role="button"], input[type=submit]'))
            .map(b => (b.innerText || b.value || '').trim())
            .filter(t => /apply|submit|next|continue|save|upload/i.test(t))
            .slice(0, 6)};
}
"""


_MODAL_TAGGER_JS = r"""
() => {
  // Modal element numbers must be the only active namespace. Without clearing
  // the background page, browser_set(0) can resolve the first page field with
  // data-wr-n="0" instead of the modal control the agent just observed.
  document.querySelectorAll('[data-wr-n]').forEach(
    el => el.removeAttribute('data-wr-n'));
  const dialogs = Array.from(document.querySelectorAll(
    '[role="dialog"], [role="alertdialog"], .modal.show, [aria-modal="true"]'));
  // keep only the truly visible dialog (hidden templates like
  // 'Session expired' sit in the DOM with display:none)
  const visibleDialog = dialogs.find(d => {
    const s = getComputedStyle(d);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = d.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (!visibleDialog) {
    return {url: location.href, title: document.title, elements: []};
  }
  const sel = 'button, input, select, textarea, [role="button"], '
            + '[role="radio"], [role="checkbox"], [role="listbox"], '
            + '[role="option"], a[href], [contenteditable="true"]';
  const els = Array.from(visibleDialog.querySelectorAll(sel));
  const out = [];
  els.forEach((el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0
        && el.tagName !== 'INPUT' && el.tagName !== 'SELECT') return;
    const n = out.length;
    el.setAttribute('data-wr-n', String(n));
    const label = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || el.innerText || el.value || el.title || '').trim()
      .replace(/\s+/g, ' ').slice(0, 90);
    out.push({
      n, tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || (el.tagName === 'SELECT' ? 'select' : null),
      name: label || null,
      value: (el.value || '').slice(0, 50) || null,
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).slice(0, 25)
          .map(o => ({v: o.value, t: o.text.trim().slice(0, 60)}))
        : undefined,
    });
  });
  const q = (visibleDialog.innerText || '')
    .replace(/\s+/g, ' ').trim().slice(0, 600);
  return {url: location.href, title: document.title,
          modal_question: q, elements: out};
}
"""


class BrowserError(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        self._last_click = 0.0  # popups are adopted only right after a click
        self._trace_path: Path | None = None

    @property
    def running(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def ensure(self, headless: bool | None = None):
        async with self._lock:
            if self.running:
                return self._page
            # context alive but the page closed (site closed the tab): reuse
            if self._context is not None:
                pages = [p for p in self._context.pages if not p.is_closed()]
                if pages:
                    self._page = pages[-1]
                    return self._page
            from playwright.async_api import async_playwright

            if self._pw is None:
                self._pw = await async_playwright().start()
            headless = self.settings.browser.get("headless", False) if headless is None else headless
            channel = self.settings.browser.get("channel", "msedge")
            self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
            launch = dict(
                user_data_dir=str(self.settings.browser_profile_dir),
                headless=headless,
                viewport={"width": 1440, "height": 900},
                locale="en-GB",
                timezone_id="Europe/London",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                # Real Edge passes Windows App Control and board anti-bot far
                # better than the bundled Chromium build.
                self._context = await self._pw.chromium.launch_persistent_context(
                    channel=channel, **launch
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "profile is already in use" in msg or "existing browser session" in msg:
                    # stale browser process holds the profile singleton —
                    # kill it and retry once
                    self._kill_profile_processes()
                    try:
                        self._context = await self._pw.chromium.launch_persistent_context(
                            channel=channel, **launch
                        )
                    except Exception:  # noqa: BLE001 - final fallback
                        self._context = await self._pw.chromium.launch_persistent_context(
                            **launch)
                else:
                    try:  # channel missing → bundled chromium
                        self._context = await self._pw.chromium.launch_persistent_context(
                            **launch)
                    except Exception as exc2:  # noqa: BLE001
                        if "profile is already in use" in str(exc2):
                            self._kill_profile_processes()
                            self._context = await (
                                self._pw.chromium.launch_persistent_context(**launch))
                        else:
                            raise
            self._context.set_default_timeout(
                int(self.settings.browser.get("default_timeout_ms", 15000))
            )
            configured_trace = str(self.settings.browser.get("trace_path", "")).strip()
            if configured_trace:
                self._trace_path = Path(configured_trace)
                self._trace_path.parent.mkdir(parents=True, exist_ok=True)
                await self._context.tracing.start(
                    screenshots=True,
                    snapshots=True,
                    sources=True,
                )
            self._context.on("page", self._on_popup)
            # Edge restores tabs from the previous run in a persistent
            # profile — keep one clean page and close the leftovers, so
            # navigation never lands on a stale tab.
            pages = [p for p in self._context.pages if not p.is_closed()]
            blank = next(
                (p for p in pages if p.url in ("about:blank", "")), None)
            self._page = blank if blank is not None else await self._context.new_page()
            for p in pages:
                if p is not self._page:
                    try:
                        await p.close()
                    except Exception:  # noqa: BLE001 - restored tabs may resist
                        pass
            return self._page

    def _kill_profile_processes(self) -> None:
        """Kill stale chrome/msedge processes holding the automation profile
        (their singleton lock blocks launch_persistent_context)."""
        import subprocess

        profile = str(self.settings.browser_profile_dir)
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object { "
                 "$_.CommandLine -like '*browser_profile*' } | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                capture_output=True, timeout=15)
            _ = out  # best-effort
        except Exception:  # noqa: BLE001 - never block the launch
            pass
        # wait briefly for the singleton lock to clear
        import time as _t

        _t.sleep(2)

    def _on_popup(self, page) -> None:
        """Adopt target=_blank popups opened by OUR clicks. Pages appearing
        outside a click window (Edge session-restore tabs at launch) must NOT
        steal the active page — they stay reachable via browser_tabs."""
        import time as _time

        if _time.monotonic() - self._last_click < 6 and not page.is_closed():
            self._page = page

    def _active(self):
        if not self.running:
            raise BrowserError("browser not open — call browser_open first")
        return self._page

    async def _settle(self) -> None:
        page = self._active()
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001 - networkidle often times out; that's fine
            pass

    async def _snap(self, text_chars: int = 0, focus: str | None = None,
                    filter_text: str | None = None) -> dict:
        page = self._active()
        data = None
        for attempt in range(3):
            try:
                data = await page.evaluate(_TAGGER_JS)
                break
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if attempt == 2 or not (
                        "destroyed" in msg or "Target closed" in msg
                        or "Execution context" in msg):
                    raise
                await page.wait_for_timeout(1200)
        elements = data["elements"]
        if focus:
            groups = {
                "inputs": {"input", "select", "textarea"},
                "buttons": {"button"},
                "links": {"a"},
            }
            wanted = groups.get(focus)
            if wanted:
                elements = [e for e in elements if e["tag"] in wanted]
        if filter_text:
            low = filter_text.lower()
            elements = [e for e in elements
                        if low in (e.get("name") or "").lower()
                        or low in (e.get("value") or "").lower()]
        data["elements"] = elements[:140]
        if text_chars:
            text = await page.evaluate(
                "() => (document.body.innerText || '').replace(/\\s+/g, ' ')"
            )
            data["text"] = text[:text_chars]
        return data

    # ------------------------------------------------------------- actions ----
    async def open(self, url: str, headless: bool | None = None) -> dict:
        page = await self.ensure(headless)
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001 - transient h2/anti-bot drops: retry once
            await page.wait_for_timeout(1500)
            resp = await page.goto(url, wait_until="domcontentloaded")
        await self._settle()
        out = await self._snap(text_chars=600)
        out["http_status"] = resp.status if resp else None
        return out

    async def snapshot(self, focus: str | None = None, filter_text: str | None = None,
                       text_chars: int = 800, modal_only: bool = False) -> dict:
        """Surgical page view. modal_only=true: show ONLY the active modal's
        controls (wizards/live-chat dialogs) — hidden templates excluded,
        page-behind elements suppressed; element numbers are stable for the
        next browser_set/click. text_chars=0 → elements only."""
        if modal_only:
            return await self._snap_modal(filter_text)
        return await self._snap(text_chars=text_chars, focus=focus,
                                filter_text=filter_text)

    async def _snap_modal(self, filter_text: str | None = None) -> dict:
        """Elements of the VISIBLE [role=dialog] only (Reed apply wizard,
        confirmation dialogs). Hidden templates (display:none 'Session
        expired' etc.) are filtered out by visibility checks."""
        page = self._active()
        data = await page.evaluate(_MODAL_TAGGER_JS)
        elements = data["elements"]
        if filter_text:
            low = filter_text.lower()
            elements = [e for e in elements
                        if low in (e.get("name") or "").lower()
                        or low in (e.get("value") or "").lower()]
        data["elements"] = elements[:60]
        if not elements:
            data["note"] = ("no visible modal — if a wizard was expected, "
                            "click Apply / reopen it first")
        return data

    async def set(self, ref: int | str, value: str | list[str] | bool) -> dict:
        """One mental model: set element #n to a value, whatever it is.
        input/textarea → fill; select → select_option (list or 'value');
        checkbox/radio → check('true'/true) or uncheck."""
        page = self._active()
        tag = ""
        if isinstance(ref, int):
            el = page.locator(f'[data-wr-n="{ref}"]')
            if await el.count() == 0:
                raise BrowserError(
                    f"element #{ref} not found — the page changed; the snapshot "
                    "returned by your last action has fresh numbers"
                )
            el = el.first
            tag = (await el.evaluate("e => e.tagName.toLowerCase()")) or ""
            typ = (await el.get_attribute("type") or "").lower()
        else:
            el = page.locator(ref).first
            tag = ref.strip("<> ").split()[0].lower() if "<" in ref else ""
            typ = ""
        if tag == "select":
            await el.select_option(value if isinstance(value, list) else [str(value)])
        elif tag == "input" and typ in ("checkbox", "radio"):
            truthy = value in (True, "true", "True", "1", "yes", True)
            await (el.check() if truthy else el.uncheck())
        else:
            await el.fill(str(value))
        return await self._snap()

    async def form(self) -> dict:
        page = self._active()
        return await page.evaluate(_FORM_JS)

    async def _locate(self, ref: int | str):
        page = self._active()
        if isinstance(ref, int):
            el = page.locator(f'[data-wr-n="{ref}"]')
            if await el.count() == 0:
                raise BrowserError(
                    f"element #{ref} not found — the page changed; the snapshot "
                    "returned by your last action has fresh numbers"
                )
            return el.first
        return page.locator(ref).first

    async def click(self, ref: int | str, timeout_ms: int | None = None) -> dict:
        import time as _time

        el = await self._locate(ref)
        self._last_click = _time.monotonic()
        await el.click(timeout=timeout_ms)
        await self._settle()
        return await self._snap()

    async def fill(self, ref: int | str, value: str, snapshot_after: bool = True) -> dict:
        el = await self._locate(ref)
        await el.fill(value)
        if not snapshot_after:
            return {"ok": True, "filled": ref}
        return await self._snap()

    async def type_text(self, ref: int | str, text: str, submit: bool = False) -> dict:
        el = await self._locate(ref)
        await el.click()
        await el.type(text, delay=25)
        if submit:
            await el.press("Enter")
        await self._settle()
        return await self._snap()

    async def select(self, ref: int | str, values: list[str]) -> dict:
        el = await self._locate(ref)
        await el.select_option(values)
        return await self._snap()

    async def check(self, ref: int | str, state: bool = True) -> dict:
        el = await self._locate(ref)
        await (el.check() if state else el.uncheck())
        return await self._snap()

    async def upload(self, ref: int | str, file_path: str) -> dict:
        """Attach a local file. Works two ways, in order:
        1. DIRECT set on input[type=file] via Playwright set_input_files —
           works even when the input is HIDDEN (Totaljobs/StepStone style);
           no native file chooser is involved.
        2. Fallback: click the element and catch the native file chooser.
        """
        page = self._active()
        p = Path(file_path)
        if not p.exists():
            raise BrowserError(f"file not found: {file_path}")
        el = await self._locate(ref)
        is_file_input = False
        try:
            is_file_input = await el.evaluate(
                "e => e.tagName === 'INPUT' && (e.type === 'file')")
        except Exception:  # noqa: BLE001 - element may not support evaluate
            is_file_input = False
        if is_file_input:
            await el.set_input_files(str(p))
            await self._settle()
            return await self._snap()
        # fallback: click → native chooser
        try:
            async with page.expect_file_chooser(timeout=8000) as fc_info:
                await el.click()
            chooser = await fc_info.value
            await chooser.set_files(str(p))
        except Exception:  # noqa: BLE001 - last resort: first file input on page
            inputs = page.locator('input[type="file"]')
            count = await inputs.count()
            if count == 0:
                raise BrowserError(
                    "no file chooser and no input[type=file] on the page — "
                    "open the upload dialog first, then retry") from None
            await inputs.first.set_input_files(str(p))
        await self._settle()
        return await self._snap()

    async def press(self, key: str) -> dict:
        page = self._active()
        await page.keyboard.press(key)
        await self._settle()
        return await self._snap()

    async def wait(self, seconds: float | None = None, text: str | None = None,
                   text_gone: str | None = None) -> dict:
        page = self._active()
        if seconds:
            await page.wait_for_timeout(int(seconds * 1000))
        if text:
            await page.wait_for_selector(f"text={text}", timeout=30000)
        if text_gone:
            await page.wait_for_selector(f"text={text_gone}", state="detached", timeout=30000)
        return await self._snap(text_chars=400)

    async def screenshot(self, name: str | None = None, full_page: bool = False) -> dict:
        page = self._active()
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        path = self.settings.screenshots_dir / f"{name or 'page'}-{stamp}.png"
        self.settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=full_page)
        return {"screenshot": str(path)}

    async def evaluate(self, js: str) -> dict:
        page = self._active()
        result = await page.evaluate(js)
        return {"result": result}

    async def tabs(self, action: str = "list", index: int | None = None) -> dict:
        if self._context is None:
            raise BrowserError("browser not open")
        pages = [p for p in self._context.pages if not p.is_closed()]
        if action == "list":
            return {"tabs": [{"index": i, "url": p.url, "title": None}
                             for i, p in enumerate(pages)]}
        if action == "select" and index is not None and 0 <= index < len(pages):
            self._page = pages[index]
            await self._page.bring_to_front()
            return await self._snap(text_chars=400)
        if action == "close" and index is not None and 0 <= index < len(pages):
            closing_active = pages[index] is self._page
            await pages[index].close()
            if closing_active and self._context.pages:
                self._page = self._context.pages[-1]
            return {"closed": index, "tabs_left": len(self._context.pages)}
        raise BrowserError("tabs: use action=list|select|close with an index")

    async def _dismiss_consent(self) -> bool:
        """Click away cookie/consent walls (OneTrust, Quantcast, CMP) — they
        mask sign-in markers and job content. Looks in frames too."""
        page = self._active()
        selectors = (
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept all')", "button:has-text('Accept All')",
            "button:has-text('Accept all cookies')", "button:has-text('Accept')",
            "button:has-text('I agree')", "button:has-text('Agree')",
            "button:has-text('Got it!')", "button:has-text('Allow all')",
        )
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if await loc.count():
                        await loc.click(timeout=2500)
                        await page.wait_for_timeout(800)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    SIGNED_OUT_MARKERS = ("sign in", "log in", "login", "register",
                          "create account", "sign up",
                          "create your gov.uk one login",
                          "sign in or create")
    SIGNED_IN_MARKERS = ("sign out", "log out", "my dashboard", "my account",
                         "account home")
    SIGNIN_PATHS = ("/login", "/account/signin", "/en-GB/candidate/login",
                    "/users/sign_in", "/auth/login")

    async def login_flow(self, url: str, google_account: str | None = None) -> dict:
        """Open a board, check login state, and if signed out walk the
        'Continue with Google' flow choosing google_account.

        The user pre-approved this account for board sign-ins; any 2FA,
        captcha or unexpected consent screen returns needs_user=true — the
        agent must stop and ask.
        """
        await self.open(url)
        await self._dismiss_consent()

        async def _wait_for_content(snap: dict) -> dict:
            """SPA pages render late; deciding 'logged in' on an empty page
            is the classic false positive — wait for real content."""
            for _ in range(6):
                els = snap.get("elements") or []
                text = snap.get("text") or ""
                if len(els) >= 5 or len(text) >= 150:
                    return snap
                await self._active().wait_for_timeout(1000)
                snap = await self._snap(text_chars=4000)
            return snap

        def _signed_out(snap: dict) -> bool:
            text = (snap.get("text") or "").lower()
            els = snap.get("elements") or []
            names = " ".join((e.get("name") or "").lower() for e in els)
            return any(m in text or m in names for m in self.SIGNED_OUT_MARKERS)

        def _google_control(snap: dict):
            for e in snap.get("elements") or []:
                name = (e.get("name") or "").lower()
                if "google" in name and e["tag"] in ("button", "a"):
                    if ("continue" in name or "sign" in name or "log" in name
                            or "with google" in name or name.strip() == "google"):
                        return e
            return None

        async def _click_google_in_frames() -> bool:
            """Some boards render the SSO button inside an iframe the
            snapshot collector does not tag — find and click it there."""
            page = self._active()
            for frame in page.frames:
                try:
                    loc = frame.locator(
                        "button:has-text('Google'), a:has-text('Google')")
                    for i in range(await loc.count()):
                        el = loc.nth(i)
                        txt = ((await el.inner_text()) or "").strip().lower()
                        if "google" in txt and any(
                                w in txt for w in ("continue", "sign", "log", "with")):
                            await el.click()
                            await self._active().wait_for_timeout(2500)
                            await self._settle()
                            return True
                except Exception:  # noqa: BLE001 - frames come and go
                    continue
            return False

        def _login_form(snap: dict) -> bool:
            els = snap.get("elements") or []
            has_pw = any(e.get("type") == "password" for e in els)
            has_email = any(
                e["tag"] == "input" and (e.get("type") == "email"
                                         or "email" in (e.get("name") or "").lower())
                for e in els)
            return has_pw and (has_email or len(els) < 15)

        current_snap = await _wait_for_content(
            await self._snap(text_chars=4000))
        for _round in range(8):
            if "accounts.google.com" in (current_snap.get("url") or ""):
                res = await self._google_walk(google_account)
                if res is not None:
                    return res
                await self._dismiss_consent()
                current_snap = await self._snap(text_chars=4000)
            # account chip (e.g. Indeed shows the e-mail in the header when
            # signed in) is a positive login signal
            if google_account and any(
                    google_account.lower() in (e.get("name") or "").lower()
                    for e in current_snap.get("elements") or []):
                return {"logged_in": True, "url": current_snap.get("url"),
                        "note": f"account chip '{google_account}' visible — signed in"}
            _names = " ".join((e.get("name") or "").lower()
                              for e in current_snap.get("elements") or [])
            _page_text = (current_snap.get("text") or "").lower()
            if any(m in _names or m in _page_text
                   for m in self.SIGNED_IN_MARKERS):
                return {"logged_in": True, "url": current_snap.get("url"),
                        "note": "account controls visible (dashboard/sign out)"}
            if not _signed_out(current_snap) and not _login_form(current_snap):
                return {"logged_in": True, "url": current_snap.get("url"),
                        "note": "no sign-in controls visible — already authenticated"}
            # SPA hydration renders the Google button late — retry-scan
            g = _google_control(current_snap)
            if g is None:
                for _retry in range(4):
                    await self._active().wait_for_timeout(1000)
                    current_snap = await self._snap(text_chars=4000)
                    g = _google_control(current_snap)
                    if g is not None:
                        break
            if g is None:
                # Homepages often hide sign-in links (icons/menus): go to a
                # known sign-in URL instead — a login FORM there means signed
                # out; a redirect away from it means signed in.
                from urllib.parse import urljoin

                origin = "/".join(current_snap.get("url", url).split("/")[:3])
                found_signin = False
                for path in self.SIGNIN_PATHS:
                    probe = await self.open(urljoin(origin, path))
                    await self._active().wait_for_timeout(2500)
                    await self._dismiss_consent()
                    probe = await self._snap(text_chars=4000)
                    probe_url = probe.get("url") or ""
                    if path not in probe_url and "/account" not in probe_url \
                            and "/signin" not in probe_url and "/login" not in probe_url:
                        return {"logged_in": True, "url": probe_url,
                                "note": "sign-in URL redirected away — session active"}
                    if _login_form(probe) or _signed_out(probe):
                        for _retry in range(4):
                            g = _google_control(probe)
                            if g is not None or _login_form(probe):
                                break
                            await self._active().wait_for_timeout(1200)
                            probe = await self._snap(text_chars=4000)
                        current_snap = await _wait_for_content(probe)
                        found_signin = True
                        break
                if not found_signin:
                    return {"logged_in": False, "needs_user": True,
                            "url": current_snap.get("url"),
                            "note": "no sign-in page found — ask the user to sign "
                                    "in manually in the browser window"}
                if g is None and await _click_google_in_frames():
                    await self._active().wait_for_timeout(1500)
                    google_result = await self._google_walk(google_account)
                    if google_result is not None:
                        return google_result
                    current_snap = await self._snap(text_chars=4000)
                    continue
                if g is None and google_account and \
                        google_account.lower() in (current_snap.get("text") or "").lower():
                    # Google Identity Services chip: the page shows the
                    # account e-mail with a Continue affordance (often in an
                    # iframe our collector does not tag)
                    clicked = False
                    for e in current_snap.get("elements") or []:
                        name = (e.get("name") or "")
                        if google_account.lower() in name.lower() or \
                                name.strip().lower() in ("continue", "continue as"):
                            await self.click(e["n"])
                            clicked = True
                            break
                    if not clicked:
                        page = self._active()
                        for frame in page.frames:
                            try:
                                cand = frame.locator(
                                    f"text={google_account}").first
                                if await cand.count():
                                    await cand.click(timeout=3000)
                                    clicked = True
                                    break
                                btn = frame.locator(
                                    "button:has-text('Continue'), "
                                    "[role=button]:has-text('Continue')").first
                                if await btn.count():
                                    await btn.click(timeout=3000)
                                    clicked = True
                                    break
                            except Exception:  # noqa: BLE001
                                continue
                    if clicked:
                        await self._active().wait_for_timeout(2000)
                        google_result = await self._google_walk(google_account)
                        if google_result is not None:
                            return google_result
                        current_snap = await self._snap(text_chars=4000)
                        continue
                if g is None:
                    return {"logged_in": False, "needs_user": True,
                            "url": current_snap.get("url"),
                            "note": "login page has no Google option — ask the "
                                    "user to sign in manually (email/password)"}
            await self.click(g["n"])
            await self._active().wait_for_timeout(2500)
            await self._settle()
            # walk the Google account flow: type the pre-approved e-mail,
            # pick the account, stop only at password/2FA
            google_result = await self._google_walk(google_account)
            if google_result is not None:
                return google_result
            current_snap = await self._snap(text_chars=4000)
        return {"logged_in": not _signed_out(current_snap), "needs_user": False,
                "url": current_snap.get("url"),
                "note": "login flow finished — verify by the page state"}

    async def _google_walk(self, google_account: str | None) -> dict | None:
        """Handle accounts.google.com pages after clicking a Google button.

        Fills the identifier (e-mail) step, picks the account from the
        chooser, clicks consent/continue. Returns a terminal dict on
        password/2FA/captcha (needs_user) or when the flow leaves Google
        back to the board (None — caller re-evaluates login state).
        """
        for _step in range(10):
            snap = await self._snap(text_chars=1500)
            url = snap.get("url") or ""
            if "accounts.google.com" not in url:
                return None
            text = (snap.get("text") or "").lower()
            els = snap.get("elements") or []
            # 1) password / 2FA — checked FIRST: the account e-mail appears
            #    as plain text on this page and must not look like a chooser
            if ("/challenge/pwd" in url or "enter your password" in text
                    or any(w in text for w in ("2fa", "two-factor",
                                               "confirm it's you", "captcha"))):
                return {"logged_in": False, "needs_user": True, "url": url,
                        "note": "Google password/2FA step — the user must finish "
                                "in the browser window (one time; the profile "
                                "keeps the session afterwards)"}
            # 2) identifier step: type the e-mail and submit with Enter (the
            #    Next button is covered by a Google anti-automation overlay)
            email_el = next(
                (e for e in els if e["tag"] == "input"
                 and (e.get("type") == "email" or "email" in (e.get("name") or "").lower())),
                None)
            if email_el and google_account:
                await self.fill(email_el["n"], google_account, snapshot_after=False)
                await self._active().keyboard.press("Enter")
                await self._active().wait_for_timeout(2000)
                continue
            # 3) account chooser: click the ELEMENT carrying the account
            #    (plain-text occurrences of the e-mail are not a chooser)
            if google_account:
                acct = next(
                    (e for e in els
                     if google_account.lower() in (e.get("name") or "").lower()),
                    None)
                if acct is not None:
                    await self.click(acct["n"])
                    await self._settle()
                    continue
            # 4) consent / continue screens
            cont = next(
                (e for e in els if e["tag"] == "button"
                 and (e.get("name") or "").strip().lower() in
                 ("continue", "allow", "accept", "next")), None)
            if cont is not None:
                await self.click(cont["n"])
                await self._settle()
                continue
            await self._active().wait_for_timeout(1200)
        return None

    async def close(self) -> dict:
        async with self._lock:
            try:
                if self._context:
                    if self._trace_path is not None:
                        await self._context.tracing.stop(path=str(self._trace_path))
                    await self._context.close()
            finally:
                self._context, self._page = None, None
                if self._pw:
                    await self._pw.stop()
                    self._pw = None
        return {
            "closed": True,
            "at": time.time(),
            "playwright_trace": str(self._trace_path) if self._trace_path else None,
        }


_SESSIONS: dict[str, BrowserSession] = {}


def get_session(settings: Settings) -> BrowserSession:
    key = str(settings.db_path)
    if key not in _SESSIONS:
        _SESSIONS[key] = BrowserSession(settings)
    return _SESSIONS[key]
