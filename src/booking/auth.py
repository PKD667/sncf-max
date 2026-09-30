"""Authentication layer for SNCF Connect using Playwright."""

from __future__ import annotations

import json
import asyncio
from typing import Optional, Callable, TYPE_CHECKING, Any
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Browser = Any  # type: ignore
    Page = Any  # type: ignore
    BrowserContext = Any  # type: ignore

from models import UserCredentials, Session
from config import SNCFConfig, default_config


class AuthenticationError(Exception):
    """Raised when authentication fails."""
    pass


class EmailCodeRequired(AuthenticationError):
    """SNCF asked for the 6-digit email verification code.

    Raised by login_start() when the password step succeeded but SNCF
    challenges this new device/browser with an emailed code. The login
    page is kept open on the authenticator; complete it with
    login_submit_code(). No session exists yet when this is raised.
    """
    pass


class _Blocked(AuthenticationError):
    """Internal: bot wall instead of a login form (retry may help)."""
    pass


class SNCFAuthenticator:
    """Handles authentication with SNCF Connect.
    
    Uses Playwright browser automation to log in and capture session data.
    This is necessary because SNCF doesn't expose a public authentication API.
    """
    
    # Selectors for SNCF Connect login page
    # These may need updating if SNCF changes their website
    SELECTORS = {
        # Cookie consent (multiple options)
        "cookie_accept": [
            'button#didomi-notice-agree-button',
            'button[id*="accept"]',
            'button:has-text("Tout accepter")',
            'button:has-text("Accepter")',
        ],
        
        # Login button (opens a popup login window)
        "login_button": [
            "#vsc-login",  # VSC login button - opens popup
            '[data-testid="header-login-button"]',
            'button:has-text("Me connecter")',
            'button:has-text("Se connecter")',
            '[data-testid="login-button"]',
            'a[href*="login"]',
        ],
        "email_input": [
            'input[name="email"]',
            'input[type="email"]',
            'input[id*="email"]',
            'input[autocomplete="email"]',
            '#email',
        ],
        "password_input": [
            'input[name="password"]',
            'input[type="password"]',
            'input[autocomplete="current-password"]',
            '#password',
        ],
        "submit_button": [
            'button[type="submit"]',
            'button:has-text("Me connecter")',
            'button:has-text("Connexion")',
            'button:has-text("Se connecter")',
            'button:has-text("Valider")',
            'button:has-text("Confirmer")',
        ],

        # Second factor: 6-digit code emailed by SNCF for every new
        # device/browser ("Mon Identifiant SNCF" two-step login).
        "code_input": [
            'input[autocomplete="one-time-code"]',
            'input[name="code"]',
            'input[id*="code"]',
            'input[id*="otp"]',
            'input[inputmode="numeric"]',
            'input[maxlength="6"]',
        ],
        
        # Post-login indicators
        "logged_in": [
            '[data-testid="user-menu"]',
            '[data-testid="header-account-button"]',
            'button[aria-label*="compte"]',
            '[class*="account"]',
            '[class*="user-menu"]',
        ],
        "error_message": [
            '[role="alert"]',
            '[class*="error"]',
            '.error-message',
            '[data-testid*="error"]',
        ],
    }
    
    def __init__(
        self,
        config: Optional[SNCFConfig] = None,
        use_firefox: bool = True,
        session_file: Optional[Path] = None,
        persist_session: bool = True,
    ):
        """Initialize the authenticator.
        
        Args:
            config: Optional configuration object
            use_firefox: Use Firefox instead of Chromium (better for bot evasion)
            persist_session: Save the session to disk on success. The web
                frontend passes False: sessions live in server memory only,
                so no credential or cookie ever lands on disk.
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "Playwright is required for authentication. "
                "Install with: pip install playwright && playwright install"
            )
        
        self.config = config or default_config
        self.use_firefox = use_firefox
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._session: Optional[Session] = None
        self._captcha_detected = False
        # Optional override for where session tokens are loaded/saved.
        # When provided, this path is used instead of config.SESSION_FILE.
        self._session_file: Optional[Path] = session_file
        self._persist_session = persist_session
        # Pending two-step login: the page waiting for the email code,
        # set by login_start() and consumed by login_submit_code().
        self._pending_page: Optional[Page] = None
        self._pending_email: str = ""
    
    async def __aenter__(self):
        await self._start_browser()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def _start_browser(self, headed: bool = False) -> None:
        """Start the browser instance.

        headed=True runs a real (non-headless) Firefox, which carries a
        far less automatable fingerprint for DataDome. Needs a display:
        call _ensure_xvfb() first on servers (or run under xvfb-run).
        """
        playwright = await async_playwright().start()
        self._playwright = playwright
        
        # Use Firefox by default for better bot evasion
        # SNCF Connect uses DataDome which is more strict with Chromium.
        # The service sandbox forbids user namespaces, so Firefox's own
        # sandbox must be off (else launch hangs in CanCreateUserNamespace
        # until the launch timeout): MOZ_FAKE_NO_SANDBOX is what Mozilla's
        # own container automation uses.
        import os
        headless = self.config.HEADLESS and not headed
        sandbox_off = {
            "MOZ_FAKE_NO_SANDBOX": "1",
            "MOZ_DISABLE_CONTENT_SANDBOX": "1",
            "MOZ_DISABLE_GMP_SANDBOX": "1",
        }
        if self.use_firefox:
            self._browser = await playwright.firefox.launch(
                headless=headless,
                slow_mo=self.config.SLOW_MO if self.config.DEBUG else 50,
                timeout=60000,
                env={**os.environ, **sandbox_off},
            )
            user_agent = 'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0'
        else:
            self._browser = await playwright.chromium.launch(
                headless=headless,
                slow_mo=self.config.SLOW_MO if self.config.DEBUG else 50,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                ],
            )
            user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
        
        self._context = await self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=user_agent,
            locale='fr-FR',
            timezone_id='Europe/Paris',
        )
    
    async def close(self) -> None:
        """Close the browser and clean up (including our Xvfb, if any)."""
        try:
            if self._context:
                await self._context.close()
        except Exception:
            if self.config.DEBUG:
                print("Warning: error while closing browser context in authenticator.")
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            if self.config.DEBUG:
                print("Warning: error while closing browser in authenticator.")
        try:
            if hasattr(self, '_playwright'):
                await self._playwright.stop()
        except Exception:
            if self.config.DEBUG:
                print("Warning: error while stopping Playwright in authenticator.")
        self._browser = None
        self._context = None
        xvfb = getattr(self, "_xvfb", None)
        if xvfb is not None:
            self._xvfb = None
            try:
                xvfb.terminate()
            except Exception:
                pass

    def _ensure_xvfb(self) -> bool:
        """Start a virtual display for headed browsers. Returns success."""
        import os
        import shutil
        import subprocess
        if os.environ.get("DISPLAY"):
            return True
        if shutil.which("Xvfb") is None:
            return False
        try:
            proc = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            return False
        import time as _time
        for _ in range(50):
            if proc.poll() is not None:
                return False
            if os.path.exists("/tmp/.X11-unix/X99"):
                break
            _time.sleep(0.1)
        else:
            try:
                proc.terminate()
            except Exception:
                pass
            return False
        os.environ["DISPLAY"] = ":99"
        self._xvfb = proc
        return True
    
    async def _take_screenshot(self, page: Page, name: str) -> None:
        """Take a debug screenshot."""
        if self.config.SCREENSHOT_ON_ERROR or self.config.DEBUG:
            self.config.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.config.SCREENSHOTS_DIR / f"{name}_{timestamp}.png"
            await page.screenshot(path=str(path))
            if self.config.DEBUG:
                print(f"Screenshot saved: {path}")
    
    def wait_for_user(self, message: str | None = None) -> None:
        """Block on CLI waiting for the user to press Enter.

        This is intended for interactive flows (e.g. manual CAPTCHA solving)
        when running from the CLI.
        """
        prompt = (message or "Press Enter to continue...").strip()
        # Ensure there is at least a trailing space so the cursor is not glued to the text
        prompt = f"{prompt} "
        try:
            input(prompt)
        except EOFError:
            # In non-interactive environments (no stdin), just continue.
            if self.config.DEBUG:
                print("No interactive stdin available; continuing without waiting for user input.")
    
    async def _find_element(self, page: Page, selector_key: str, timeout: int = 5000):
        """Try multiple selectors and return the first matching element.
        
        Args:
            page: Playwright page
            selector_key: Key in SELECTORS dict
            timeout: Timeout per selector attempt
            
        Returns:
            Locator for the found element, or None
        """
        selectors = self.SELECTORS.get(selector_key, [])
        if isinstance(selectors, str):
            selectors = [selectors]
        
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                if count > 0:
                    # Verify it's visible
                    if await locator.first.is_visible():
                        return locator.first
            except Exception:
                continue
        
        return None
    
    async def _click_element(self, page: Page, selector_key: str, timeout: int = 10000) -> bool:
        """Try to click an element using multiple selectors.
        
        Returns:
            True if clicked successfully
        """
        selectors = self.SELECTORS.get(selector_key, [])
        if isinstance(selectors, str):
            selectors = [selectors]
        
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    await locator.first.click(timeout=timeout)
                    return True
            except Exception:
                continue
        
        return False
    
    async def _fill_input(self, page: Page, selector_key: str, value: str, timeout: int = 10000) -> bool:
        """Try to fill an input using multiple selectors.
        
        Returns:
            True if filled successfully
        """
        selectors = self.SELECTORS.get(selector_key, [])
        if isinstance(selectors, str):
            selectors = [selectors]
        
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    await locator.first.wait_for(state="visible", timeout=timeout)
                    await locator.first.fill(value)
                    return True
            except Exception:
                continue
        
        return False
    
    async def _handle_cookie_consent(self, page: Page) -> None:
        """Handle cookie consent popup if present."""
        try:
            await page.wait_for_timeout(1000)  # Wait for popup to appear
            await self._click_element(page, "cookie_accept", timeout=3000)
            await page.wait_for_timeout(500)
        except Exception:
            pass  # Cookie banner might not be present

    async def _handle_captcha(
        self,
        page: Page,
        on_captcha: Optional[Callable[[], None]] = None,
    ) -> None:
                    # Check for CAPTCHA in login popup before proceeding
            # DataDome and other bot protection services use various captcha iframes
            captcha_detected = False
            captcha_selectors = [
                'iframe[src*="captcha"]',
                'iframe[src*="recaptcha"]', 
                'iframe[src*="datadome"]',
                'iframe[src*="geo.captcha-delivery"]',
                '[class*="captcha"]',
            ]
            
            for frame in page.frames:
                if 'captcha' in frame.url.lower() or 'datadome' in frame.url.lower():
                    captcha_detected = True
                    print(f"Captcha detected in frame: {frame.url}")
                    break
            
            if not captcha_detected:
                for sel in captcha_selectors:
                    try:
                        locator = page.locator(sel)
                        count = await locator.count()
                        if count > 0:
                            if await locator.first.is_visible():
                                print(f"Captcha visible in selector: {sel}")
                                captcha_detected = True
                                break
                            else:
                                print(f"Captcha not visible in selector: {sel}")
                        else:
                            print(f"No captcha found in selector: {sel}")
                    except Exception:
                        pass
            
            if captcha_detected:
                self._captcha_detected = True
                await self._take_screenshot(page, "captcha_in_popup")
                
                if self.config.HEADLESS:
                    raise AuthenticationError(
                        "CAPTCHA detected by bot protection (DataDome). "
                        "Run with SNCF_HEADLESS=false to solve manually, or use 'sncf-max debug login' for interactive mode."
                    )
                else:
                    # In non-headless mode, wait for user to solve CAPTCHA
                    if on_captcha:
                        on_captcha()
                    print("\n⚠️  CAPTCHA detected! Please solve it in the browser window...")
                    print("   Waiting up to 120 seconds for you to complete the CAPTCHA...")
                    
                    # Wait for CAPTCHA to be solved (form becomes available)
                    for _ in range(24):  # 24 * 5 = 120 seconds
                        await asyncio.sleep(5)
                        # Check if email input is now visible
                        email_visible = False
                        for sel in self.SELECTORS["email_input"]:
                            try:
                                loc = page.locator(sel)
                                if await loc.count() > 0 and await loc.first.is_visible():
                                    email_visible = True
                                    break
                            except:
                                pass
                        if email_visible:
                            print("   ✅ CAPTCHA solved! Continuing...")
                            break
                    else:
                        raise AuthenticationError("Timeout waiting for CAPTCHA to be solved")
    
    async def login(self, credentials: UserCredentials, 
                    on_captcha: Optional[Callable[[], None]] = None) -> Session:
        """Log in to SNCF Connect and return session data.

        Legacy single-shot path kept for CLI compatibility: it submits the
        password and cannot answer SNCF's emailed-code second step, so it
        raises EmailCodeRequired whenever SNCF challenges the new device
        (which is nearly always). Interactive flows should use
        login_start() + login_submit_code() instead.
        
        Args:
            credentials: User email and password
            on_captcha: Optional callback when CAPTCHA is detected (for manual intervention)
            
        Returns:
            Session object with authentication data
            
        Raises:
            AuthenticationError: If login fails
        """
        if not self._browser:
            await self._start_browser()
        
        page = await self._context.new_page()
        
        try:
            # Navigate to SNCF Connect
            await page.goto(self.config.SNCF_CONNECT_BASE_URL, timeout=self.config.BROWSER_TIMEOUT)
            await page.wait_for_load_state("domcontentloaded", timeout=self.config.BROWSER_TIMEOUT)
            await page.wait_for_timeout(2000)  # Wait for JS to load
            
            # Handle cookie consent
            await self._handle_cookie_consent(page)
            
            if self.config.DEBUG:
                await self._take_screenshot(page, "home_page")
            
            # The login button opens a popup window - we need to handle this
            login_page = None
            
            # Try to click login button and capture popup
            async with self._context.expect_page(timeout=15000) as popup_info:
                # Click the login button (which opens a popup)
                clicked = await self._click_element(page, "login_button", timeout=10000)
                if not clicked:
                    # Try direct navigation as fallback
                    await page.goto(f"{self.config.SNCF_CONNECT_BASE_URL}/app/home/login", timeout=self.config.BROWSER_TIMEOUT)
                    login_page = page
                else:
                    try:
                        login_page = await popup_info.value
                        await login_page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        # No popup opened, maybe login form is on the same page
                        login_page = page
            
            await login_page.wait_for_timeout(3000)
            
            if self.config.DEBUG:
                await self._take_screenshot(login_page, "login_popup")
            
            # Fill in email in the login page/popup
            if not await self._fill_input(login_page, "email_input", credentials.email, timeout=15000):
                await self._take_screenshot(login_page, "email_not_found")
                raise AuthenticationError("Could not find email input field")
            
            await login_page.wait_for_timeout(500)
            
            # Fill in password
            if not await self._fill_input(login_page, "password_input", credentials.password, timeout=10000):
                await self._take_screenshot(login_page, "password_not_found")
                raise AuthenticationError("Could not find password input field")
            
            if self.config.DEBUG:
                await self._take_screenshot(login_page, "credentials_filled")
            
            await login_page.wait_for_timeout(1000)
        
            
            # Submit the form
            if not await self._click_element(login_page, "submit_button", timeout=10000):
                # Try pressing Enter as fallback
                await login_page.keyboard.press("Enter")

            try:
                await self._handle_captcha(page)
            except Exception as e:
                print(f"Error handling CAPTCHA: {e}")
                print("YOU HAVE TO SOLVE THE CAPTCHA MANUALLY")
                self.wait_for_user("You have to solve the CAPTCHA manually. Press Enter to continue...")
            
            # Wait for login to complete
            await login_page.wait_for_timeout(3000)
            
            # Check for error message in popup
            error_el = await self._find_element(login_page, "error_message")
            if error_el:
                try:
                    error_text = await error_el.text_content()
                    if error_text and len(error_text.strip()) > 3:
                        await self._take_screenshot(login_page, "login_popup_error")
                        raise AuthenticationError(f"Login failed: {error_text.strip()}")
                except AuthenticationError:
                    raise
                except Exception:
                    pass
            
            # The popup should close after successful login, wait for it
            try:
                if login_page != page:
                    await login_page.wait_for_event("close", timeout=15000)
            except Exception:
                pass  # Popup might not close automatically
            
            # Go back to main page and check if logged in
            await page.wait_for_timeout(2000)
            await page.reload()
            
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # Might timeout but still work
            
            # Check for CAPTCHA
            captcha_selectors = ['iframe[src*="recaptcha"]', 'iframe[src*="captcha"]', '[class*="captcha"]']
            for sel in captcha_selectors:
                try:
                    if await page.locator(sel).count() > 0:
                        if on_captcha:
                            on_captcha()
                        await self._take_screenshot(page, "captcha_detected")
                        raise AuthenticationError(
                            "CAPTCHA detected. Run with SNCF_HEADLESS=false and solve manually."
                        )
                except Exception:
                    pass
            
            # Check for error messages
            error_el = await self._find_element(page, "error_message")
            if error_el:
                try:
                    error_text = await error_el.text_content()
                    if error_text and len(error_text) > 5:
                        await self._take_screenshot(page, "login_error")
                        raise AuthenticationError(f"Login failed: {error_text}")
                except Exception:
                    pass
            
            # Check if logged in
            await page.wait_for_timeout(2000)
            logged_in = await self._find_element(page, "logged_in")
            
            if not logged_in:
                # Try navigating to account page to verify
                try:
                    await page.goto(f"{self.config.SNCF_CONNECT_BASE_URL}/app/home/myaccount", timeout=10000)
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass
            
            if self.config.DEBUG:
                await self._take_screenshot(page, "after_login")
            
            # Extract session data
            cookies = await self._context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            
            # Check if we got any auth-related cookies
            auth_cookies = [c for c in cookies if any(
                x in c["name"].lower() for x in ['auth', 'session', 'token', 'user', 'sncf']
            )]
            
            if not auth_cookies and not logged_in:
                await self._take_screenshot(page, "no_auth_cookies")
                raise AuthenticationError(
                    "Login may have failed - no auth cookies received. "
                    "Check screenshots or try with SNCF_HEADLESS=false"
                )
            
            # Try to extract any auth tokens from localStorage
            tokens = {}
            try:
                tokens = await page.evaluate("""() => {
                    const data = {};
                    try {
                        for (let i = 0; i < localStorage.length; i++) {
                            const key = localStorage.key(i);
                            if (key && (key.toLowerCase().includes('token') || 
                                key.toLowerCase().includes('auth') ||
                                key.toLowerCase().includes('session'))) {
                                data[key] = localStorage.getItem(key);
                            }
                        }
                    } catch(e) {}
                    return data;
                }""")
            except Exception:
                pass
            
            # Create session
            self._session = Session(
                user_email=credentials.email,
                cookies=cookie_dict,
                access_token=tokens.get("access_token"),
                refresh_token=tokens.get("refresh_token"),
            )
            
            # Save session for future use
            self._save_session()
            
            return self._session
            
        except Exception as e:
            await self._take_screenshot(page, "error")
            raise AuthenticationError(f"Authentication failed: {e}") from e
        finally:
            await page.close()
    
    # ------------------------------------------------------------------
    # Two-step web login: password first, emailed code second.
    #
    # SNCF ("Mon Identifiant SNCF") challenges every new device/browser
    # with a 6-digit code emailed to the account address, AFTER the
    # password is accepted. A server-side login therefore cannot complete
    # in one shot: login_start() submits the password and either returns
    # "connected" or raises EmailCodeRequired (keeping the browser page
    # open); the caller collects the code from the user and finishes with
    # login_submit_code(). The legacy single-shot login() below is kept
    # for CLI compatibility.
    # ------------------------------------------------------------------

    #: Substrings (lowercased page text) signalling the emailed-code step.
    _CODE_CHALLENGE_TEXT = (
        "code de vérification",
        "code de validation",
        "6 chiffres",
        "6-digit",
        "verification code",
    )

    async def _is_blocked(self, page: Page) -> bool:
        """Detect bot-protection walls (DataDome/captcha) before grinding."""
        selectors = [
            'iframe[src*="captcha-delivery"]',
            'iframe[src*="datadome"]',
            'iframe[src*="recaptcha"]',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
            except Exception:
                pass
        return False

    async def _wait_visible(
        self, page: Page, selector_key: str, timeout: int = 20000
    ) -> Optional[str]:
        """Wait until any selector of the set is visible. Bounded total.

        Unlike _fill_input (which burns `timeout` PER selector), this caps
        the whole detection at `timeout` ms and returns the matching
        selector, or None. Use before filling so a walled-off page fails
        in seconds instead of grinding through every selector timeout.
        """
        selectors = self.SELECTORS.get(selector_key, [])
        if isinstance(selectors, str):
            selectors = [selectors]
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            for sel in selectors:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        return sel
                except Exception:
                    pass
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.5)

    async def _open_login_page(self, on_captcha=None):
        """Start the browser, open SNCF Connect, return (page, login_page).

        Captures the login popup when the button opens one; otherwise
        falls back to the standalone login URL on the same page. Never
        lets playwright's popup-wait escape: a silently-swallowed click
        (bot-gated button) is a normal outcome, not an exception.
        """
        if not self._browser:
            await self._start_browser()

        page = await self._context.new_page()

        # Navigate to SNCF Connect
        await page.goto(self.config.SNCF_CONNECT_BASE_URL, timeout=self.config.BROWSER_TIMEOUT)
        await page.wait_for_load_state("domcontentloaded", timeout=self.config.BROWSER_TIMEOUT)
        await page.wait_for_timeout(2000)  # Wait for JS to load

        # Handle cookie consent
        await self._handle_cookie_consent(page)

        login_page = None
        try:
            async with self._context.expect_page(timeout=8000) as popup_info:
                clicked = await self._click_element(page, "login_button", timeout=10000)
                if clicked:
                    try:
                        login_page = await popup_info.value
                    except Exception:
                        login_page = None
        except Exception:
            login_page = None

        if login_page is not None:
            try:
                await login_page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
        else:
            # No popup: maybe the form is already here, else go to it.
            if await self._wait_visible(page, "email_input", timeout=5000) is None:
                for url in (
                    f"{self.config.SNCF_CONNECT_BASE_URL}/app/home/login",
                    f"{self.config.SNCF_CONNECT_BASE_URL}/home/login",
                ):
                    try:
                        await page.goto(url, timeout=self.config.BROWSER_TIMEOUT)
                        await page.wait_for_timeout(2000)
                        break
                    except Exception:
                        continue
            login_page = page

        await login_page.wait_for_timeout(3000)
        return page, login_page

    async def _password_error(self, login_page: Page) -> Optional[str]:
        """Return the login form's error text after a submit, if any."""
        error_el = await self._find_element(login_page, "error_message")
        if not error_el:
            return None
        try:
            error_text = await error_el.text_content()
            if error_text and len(error_text.strip()) > 3:
                return error_text.strip()
        except Exception:
            pass
        return None

    async def _is_code_challenge(self, page: Page) -> bool:
        """Detect SNCF's emailed-code second step."""
        for sel in self.SELECTORS["code_input"]:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
            except Exception:
                pass
        try:
            text = (await page.content()).lower()
        except Exception:
            return False
        return any(t in text for t in self._CODE_CHALLENGE_TEXT)

    async def _is_logged_in(self, page: Page) -> bool:
        """Logged-in indicators or SNCF auth cookies present."""
        if await self._find_element(page, "logged_in"):
            return True
        try:
            cookies = await self._context.cookies()
        except Exception:
            return False
        return any(
            any(x in c["name"].lower() for x in ["auth", "session", "token", "user", "sncf"])
            for c in cookies
        )

    async def _confirm_logged_in(self, login_page: Page) -> bool:
        """Confirm the session, falling back to the account page check."""
        if await self._is_logged_in(login_page):
            return True
        try:
            await login_page.goto(
                f"{self.config.SNCF_CONNECT_BASE_URL}/app/home/myaccount",
                timeout=10000,
            )
            await login_page.wait_for_timeout(2000)
        except Exception:
            pass
        return await self._is_logged_in(login_page)

    async def _build_session(self, page: Page, email: str) -> Session:
        """Capture cookies + tokens from an authenticated page."""
        cookies = await self._context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        # Try to extract any auth tokens from localStorage
        tokens: dict = {}
        try:
            tokens = await page.evaluate("""() => {
                const data = {};
                try {
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        if (key && (key.toLowerCase().includes('token') ||
                            key.toLowerCase().includes('auth') ||
                            key.toLowerCase().includes('session'))) {
                            data[key] = localStorage.getItem(key);
                        }
                    }
                } catch(e) {}
                return data;
            }""")
        except Exception:
            pass

        return Session(
            user_email=email,
            cookies=cookie_dict,
            access_token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
        )

    async def login_start(
        self,
        credentials: UserCredentials,
        on_captcha: Optional[Callable[[], None]] = None,
    ) -> str:
        """Submit email+password. Returns "connected", or raises EmailCodeRequired.

        On "connected" the session is available as self.session (and saved
        to disk unless persist_session=False). On EmailCodeRequired the
        browser page stays open: finish with login_submit_code().

        Headless browsers get walled by DataDome, so a blocked first
        attempt is retried once in a headed Firefox under Xvfb (a real
        display changes the fingerprint decisively). Set SNCF_NO_HEADED=1
        to disable the retry.
        """
        import os
        try:
            return await self._login_start_once(credentials, on_captcha)
        except _Blocked:
            pass
        if os.environ.get("SNCF_NO_HEADED"):
            raise AuthenticationError(
                "SNCF bot protection (DataDome) blocked this login attempt."
            )
        # Retry headed: real display, real rendering, far less detectable.
        await self.close()
        if not self._ensure_xvfb():
            raise AuthenticationError(
                "SNCF bot protection (DataDome) blocked this login attempt "
                "and no virtual display is available for a headed retry."
            )
        try:
            await self._start_browser(headed=True)
            return await self._login_start_once(credentials, on_captcha)
        except _Blocked as e:
            await self.close()
            raise AuthenticationError(
                "SNCF bot protection (DataDome) blocked this login attempt "
                "even with a headed browser. Booking needs a manual login "
                "for now - try again later."
            ) from e

    async def _login_start_once(
        self,
        credentials: UserCredentials,
        on_captcha: Optional[Callable[[], None]] = None,
    ) -> str:
        """Single password-submit pass. Raises _Blocked on a bot wall."""
        if not credentials.email or not credentials.password:
            raise AuthenticationError("Email and password are required")

        page = None
        try:
            page, login_page = await self._open_login_page(on_captcha)
        except AuthenticationError:
            await self.close()
            raise
        except Exception as e:
            await self.close()
            raise AuthenticationError(f"Could not open SNCF login: {e}") from e
        try:
            if self.config.DEBUG:
                await self._take_screenshot(login_page, "login_popup")

            # Bot wall instead of a login form: fail fast rather than
            # grinding through every selector timeout (minutes). A
            # challenge iframe alone is not conclusive (SNCF loads the
            # captcha script site-wide), so only bail when there is
            # genuinely no login form to fill.
            if await self._is_blocked(login_page) and await self._wait_visible(
                login_page, "email_input", timeout=8000
            ) is None:
                await self._take_screenshot(login_page, "blocked")
                raise _Blocked(
                    "SNCF bot protection (DataDome) wall instead of a form."
                )

            # Fill in email in the login page/popup
            if await self._wait_visible(login_page, "email_input", timeout=20000) is None:
                await self._take_screenshot(login_page, "email_not_found")
                raise AuthenticationError(
                    "Could not find email input field "
                    "(SNCF may block automated browsers)"
                )
            if not await self._fill_input(login_page, "email_input", credentials.email, timeout=5000):
                raise AuthenticationError("Could not fill email input field")

            await login_page.wait_for_timeout(500)

            # Fill in password
            if await self._wait_visible(login_page, "password_input", timeout=15000) is None:
                await self._take_screenshot(login_page, "password_not_found")
                raise AuthenticationError("Could not find password input field")
            if not await self._fill_input(login_page, "password_input", credentials.password, timeout=5000):
                raise AuthenticationError("Could not fill password input field")

            if self.config.DEBUG:
                await self._take_screenshot(login_page, "credentials_filled")

            await login_page.wait_for_timeout(1000)

            # Submit the form
            if not await self._click_element(login_page, "submit_button", timeout=10000):
                # Try pressing Enter as fallback
                await login_page.keyboard.press("Enter")

            await self._handle_captcha(login_page, on_captcha)

            # Wait for login to complete
            await login_page.wait_for_timeout(4000)

            # Wrong password etc. surface here, before any code step.
            message = await self._password_error(login_page)
            if message:
                await self._take_screenshot(login_page, "login_popup_error")
                raise AuthenticationError(f"Login failed: {message}")

            if await self._is_code_challenge(login_page):
                self._pending_page = login_page
                self._pending_email = credentials.email
                # NOTE: pages stay open on purpose; login_submit_code()
                # or close() releases them.
                raise EmailCodeRequired(
                    "SNCF emailed a 6-digit verification code to "
                    f"{credentials.email} (new device). Submit it to finish logging in."
                )

            if not await self._confirm_logged_in(login_page):
                await self._take_screenshot(login_page, "no_session")
                raise AuthenticationError(
                    "Login may have failed - no session established. "
                    "Check credentials or try again."
                )

            self._session = await self._build_session(login_page, credentials.email)
            if self._persist_session:
                self._save_session()
            return "connected"
        except EmailCodeRequired:
            raise
        except AuthenticationError:
            await self.close()
            raise
        except Exception as e:
            try:
                if not page.is_closed():
                    await self._take_screenshot(page, "error")
            except Exception:
                pass
            await self.close()
            raise AuthenticationError(f"Authentication failed: {e}") from e
        finally:
            if self._pending_page is None and page is not None:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass

    async def login_submit_code(
        self,
        code: str,
        on_captcha: Optional[Callable[[], None]] = None,
    ) -> Session:
        """Submit the emailed 6-digit code and return the session."""
        page = self._pending_page
        if page is None:
            raise AuthenticationError(
                "No pending email-code challenge. Start a login first."
            )
        code = (code or "").strip().replace(" ", "")
        if not code:
            raise AuthenticationError("Verification code is required")
        try:
            if await self._wait_visible(page, "code_input", timeout=20000) is None:
                raise AuthenticationError(
                    "Code field not found - the challenge may have expired. Start over."
                )
            if not await self._fill_input(page, "code_input", code, timeout=5000):
                raise AuthenticationError("Could not fill the code field - start over.")
            await page.wait_for_timeout(500)
            if not await self._click_element(page, "submit_button", timeout=10000):
                await page.keyboard.press("Enter")

            await self._handle_captcha(page, on_captcha)
            await page.wait_for_timeout(4000)

            message = await self._password_error(page)
            if message:
                await self._take_screenshot(page, "code_error")
                raise AuthenticationError(f"Code rejected: {message}")

            if await self._is_code_challenge(page):
                raise AuthenticationError(
                    "SNCF is still asking for a code - wrong or expired code. Start over."
                )

            if not await self._confirm_logged_in(page):
                await self._take_screenshot(page, "no_session_after_code")
                raise AuthenticationError(
                    "Login failed after code - session not established."
                )

            self._session = await self._build_session(page, self._pending_email)
            if self._persist_session:
                self._save_session()
            return self._session
        finally:
            self._pending_page = None
            self._pending_email = ""
            await self.close()

    def _save_session(self) -> None:
        """Save current session to disk."""
        if self._session:
            session_data = {
                "user_email": self._session.user_email,
                "cookies": self._session.cookies,
                "access_token": self._session.access_token,
                "refresh_token": self._session.refresh_token,
                "saved_at": datetime.now().isoformat(),
            }
            if self._session_file:
                self._session_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._session_file, "w") as f:
                    json.dump(session_data, f)
            else:
                self.config.save_session(session_data)
    
    def load_session(self) -> Optional[Session]:
        """Load a previously saved session."""
        # Prefer explicit session_file override if provided
        if self._session_file:
            if not self._session_file.exists():
                return None
            try:
                with open(self._session_file, "r") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                return None
        else:
            data = self.config.load_session()

        if not data:
            return None
        
        self._session = Session(
            user_email=data.get("user_email", ""),
            cookies=data.get("cookies", {}),
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
        )
        return self._session
    
    async def validate_session(self, session: Optional[Session] = None) -> bool:
        """Check if a session is still valid.
        
        Args:
            session: Session to validate (uses current if not provided)
            
        Returns:
            True if session is valid
        """
        session = session or self._session
        if not session or not session.cookies:
            return False
        
        if not self._browser:
            await self._start_browser()
        
        # Add cookies to context. Handle special "__Secure-"/"__Host-" prefixes
        # to satisfy modern browser requirements.
        cookies = []
        for name, value in session.cookies.items():
            cookie = {
                "name": name,
                "value": value,
                "domain": ".sncf-connect.com",
                "path": "/",
            }
            if name.startswith("__Secure-") or name.startswith("__Host-"):
                cookie["secure"] = True
            if name.startswith("__Host-"):
                # __Host- cookies must not have a Domain attribute
                cookie.pop("domain", None)
            cookies.append(cookie)
        try:
            await self._context.add_cookies(cookies)
        except Exception as exc:
            if self.config.DEBUG:
                print(f"Error adding cookies in validate_session: {exc}")
                print("Retrying cookies one by one to find the problematic ones...")
            for c in cookies:
                try:
                    await self._context.add_cookies([c])
                except Exception as e:
                    if self.config.DEBUG:
                        print(f"Cookie rejected in validate_session: {c['name']} -> {e}")
        
        page = await self._context.new_page()
        try:
            # Try to access account page
            await page.goto(f"{self.config.SNCF_CONNECT_BASE_URL}/app/home/myaccount")
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
            
            # Check if we're logged in or redirected to login
            current_url = page.url
            if "login" in current_url.lower():
                return False
            
            # Check for logged-in indicator
            logged_in = await self._find_element(page, "logged_in")
            return logged_in is not None
        except Exception:
            return False
        finally:
            await page.close()
    
    @property
    def session(self) -> Optional[Session]:
        """Get the current session."""
        return self._session
    
    @property 
    def is_authenticated(self) -> bool:
        """Check if we have a valid session."""
        return self._session is not None and self._session.is_valid


def login_sync(
    credentials: UserCredentials,
    config: Optional[SNCFConfig] = None,
    session_file: Optional[Path] = None,
) -> Session:
    """Synchronous wrapper for login.
    
    Args:
        credentials: User credentials
        config: Optional configuration
        
    Returns:
        Session object
    """
    async def _login():
        async with SNCFAuthenticator(config, session_file=session_file) as auth:
            return await auth.login(credentials)
    
    return asyncio.run(_login())


def load_or_login(
    email: str,
    password: str,
    config: Optional[SNCFConfig] = None,
    session_file: Optional[Path] = None,
) -> Session:
    """Load existing session or perform fresh login.
    
    Args:
        email: SNCF Connect email
        password: SNCF Connect password
        config: Optional configuration
        
    Returns:
        Valid Session object
    """
    config = config or default_config
    auth = SNCFAuthenticator(config, session_file=session_file)
    
    # Try to load existing session
    session = auth.load_session()
    if session:
        # If a custom session file is provided, we assume the user knows
        # what they're doing and skip browser-based validation to avoid
        # re-running the full auth flow.
        if session_file is not None:
            return session

        # Otherwise, validate against SNCF Connect using the browser.
        async def _validate():
            async with auth:
                return await auth.validate_session(session)

        if asyncio.run(_validate()):
            return session
    
    # Need fresh login
    return login_sync(
        UserCredentials(email=email, password=password),
        config=config,
        session_file=session_file,
    )

