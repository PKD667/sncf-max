"""Browser debug interface for calibrating and fixing automation.

This module provides tools to:
- Interactively test selectors on the SNCF Connect website
- Debug login/booking flows step by step
- Capture and compare screenshots
- Identify when the website structure changes

Usage:
    from sncf_max.browser_debug import BrowserDebugger
    
    async with BrowserDebugger() as debugger:
        await debugger.start_interactive()
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any, Callable
from dataclasses import dataclass, field
import sys

try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext, Locator
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Browser = Any
    Page = Any
    BrowserContext = Any
    Locator = Any

from config import SNCFConfig, default_config
from .auth import SNCFAuthenticator


@dataclass
class SelectorTest:
    """Result of testing a selector."""
    selector: str
    found: bool
    count: int
    visible: bool
    text: Optional[str] = None
    attributes: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class PageState:
    """Captured state of a page."""
    url: str
    title: str
    timestamp: datetime
    screenshot_path: Optional[Path] = None
    html_snippet: Optional[str] = None
    selectors_tested: List[SelectorTest] = field(default_factory=list)


class BrowserDebugger:
    """Interactive browser debugger for SNCF Connect automation.
    
    This provides a REPL-like interface to:
    - Navigate to pages
    - Test selectors
    - Take screenshots  
    - Step through login/booking flows
    - Identify website changes
    """
    
    # Known selectors to test (grouped by purpose)
    KNOWN_SELECTORS = {
        "cookie_consent": [
            'button#didomi-notice-agree-button',
            'button[id*="accept"]',
            'button:has-text("Tout accepter")',
            'button:has-text("Accepter")',
            '#didomi-notice-agree-button',
        ],
        "login_button": [
            "#vsc-login",
            '[data-testid="header-login-button"]',
            'button:has-text("Me connecter")',
            'button:has-text("Se connecter")',
            '[data-testid="login-button"]',
            'a[href*="login"]',
            '.header-login',
            '[class*="login"]',
        ],
        "email_input": [
            'input[name="email"]',
            'input[type="email"]',
            'input[id*="email"]',
            'input[autocomplete="email"]',
            '#email',
            'input[placeholder*="mail"]',
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
        ],
        "logged_in_indicator": [
            '[data-testid="user-menu"]',
            '[data-testid="header-account-button"]',
            'button[aria-label*="compte"]',
            '[class*="account"]',
            '[class*="user-menu"]',
            '.user-logged-in',
        ],
        "search_origin": [
            'input[placeholder*="Départ"]',
            'input[aria-label*="départ"]',
            '#origin-input',
            '[data-testid="origin-input"]',
            'input[name="origin"]',
        ],
        "search_destination": [
            'input[placeholder*="Arrivée"]',
            'input[aria-label*="arrivée"]',
            '#destination-input',
            '[data-testid="destination-input"]',
            'input[name="destination"]',
        ],
        "search_button": [
            'button[type="submit"]',
            'button:has-text("Rechercher")',
            '[data-testid="search-button"]',
        ],
        "trip_results": [
            '.journey-card',
            '.travel-proposal',
            '[data-testid*="journey"]',
            '[data-testid*="result"]',
            '.search-result',
        ],
        "tgvmax_badge": [
            '.tgvmax',
            '.max-badge',
            ':has-text("MAX")',
            '[class*="max"]',
            '[data-testid*="max"]',
        ],
    }
    
    PAGES = {
        "home": "https://www.sncf-connect.com",
        "login": "https://www.sncf-connect.com/app/home/login",
        "search": "https://www.sncf-connect.com/app/home/shop/search",
        "account": "https://www.sncf-connect.com/app/home/myaccount",
        "trips": "https://www.sncf-connect.com/app/home/myaccount/travels",
    }
    
    def __init__(self, config: Optional[SNCFConfig] = None):
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError("Playwright required. Install: pip install playwright && playwright install chromium")
        
        self.config = config or default_config
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._states: List[PageState] = []
        
        # Debug output directory
        self.debug_dir = self.config.SCREENSHOTS_DIR / "debug"
        self.debug_dir.mkdir(parents=True, exist_ok=True)
    
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, *args):
        await self.close()
    
    async def start(self, headless: bool = False, use_firefox: bool = True) -> None:
        """Start the browser in debug mode (visible by default).
        
        Args:
            headless: Run in headless mode (default False for debugging)
            use_firefox: Use Firefox instead of Chromium (better for bot evasion)
        """
        self._playwright = await async_playwright().start()
        
        # Firefox is better at evading DataDome bot detection
        if use_firefox:
            self._browser = await self._playwright.firefox.launch(
                headless=headless,
                slow_mo=100,
            )
            user_agent = 'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0'
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=headless,
                slow_mo=100,
                devtools=not headless,
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
        
        self._page = await self._context.new_page()
        
        # Enable console log capture (but filter noise)
        def on_console(msg):
            text = msg.text
            # Filter out noisy messages
            if not any(x in text.lower() for x in ['batch', 'advertiser', 'ias tag', 'failed to load']):
                print(f"[BROWSER] {text[:200]}")
        
        self._page.on("console", on_console)
    
    async def close(self) -> None:
        """Close the browser."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
    
    async def goto(self, page_name: str) -> PageState:
        """Navigate to a known page."""
        url = self.PAGES.get(page_name, page_name)
        await self._page.goto(url, timeout=30000)
        await self._page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)  # Wait for JS
        
        return await self.capture_state(f"goto_{page_name}")
    
    async def capture_state(self, name: str = "state") -> PageState:
        """Capture current page state with screenshot."""
        timestamp = datetime.now()
        screenshot_path = self.debug_dir / f"{name}_{timestamp:%Y%m%d_%H%M%S}.png"
        
        await self._page.screenshot(path=str(screenshot_path), full_page=True)
        
        state = PageState(
            url=self._page.url,
            title=await self._page.title(),
            timestamp=timestamp,
            screenshot_path=screenshot_path,
        )
        
        self._states.append(state)
        return state
    
    async def test_selector(self, selector: str, timeout: int = 3000) -> SelectorTest:
        """Test if a selector matches any elements."""
        result = SelectorTest(selector=selector, found=False, count=0, visible=False)
        
        try:
            locator = self._page.locator(selector)
            result.count = await locator.count()
            result.found = result.count > 0
            
            if result.found:
                first = locator.first
                result.visible = await first.is_visible()
                
                try:
                    result.text = await first.text_content(timeout=timeout)
                    if result.text:
                        result.text = result.text.strip()[:100]  # Truncate
                except Exception:
                    pass
                
                try:
                    # Get some useful attributes
                    for attr in ['id', 'class', 'data-testid', 'aria-label', 'type']:
                        val = await first.get_attribute(attr)
                        if val:
                            result.attributes[attr] = val[:50]
                except Exception:
                    pass
                    
        except Exception as e:
            result.error = str(e)[:100]
        
        return result
    
    async def test_selector_group(self, group_name: str) -> List[SelectorTest]:
        """Test all selectors in a known group."""
        selectors = self.KNOWN_SELECTORS.get(group_name, [])
        results = []
        
        for selector in selectors:
            result = await self.test_selector(selector)
            results.append(result)
        
        return results
    
    async def test_all_selectors(self) -> Dict[str, List[SelectorTest]]:
        """Test all known selectors on current page."""
        results = {}
        
        for group_name in self.KNOWN_SELECTORS:
            results[group_name] = await self.test_selector_group(group_name)
        
        return results
    
    async def find_working_selector(self, group_name: str) -> Optional[str]:
        """Find the first working visible selector from a group."""
        results = await self.test_selector_group(group_name)
        
        for result in results:
            if result.found and result.visible:
                return result.selector
        
        # Fall back to any found selector
        for result in results:
            if result.found:
                return result.selector
        
        return None
    
    async def click_first_working(self, group_name: str) -> bool:
        """Click the first working selector from a group."""
        selector = await self.find_working_selector(group_name)
        
        if selector:
            try:
                await self._page.locator(selector).first.click(timeout=5000)
                return True
            except Exception:
                pass
        
        return False
    
    async def fill_first_working(self, group_name: str, value: str) -> bool:
        """Fill the first working input selector from a group."""
        selector = await self.find_working_selector(group_name)
        
        if selector:
            try:
                await self._page.locator(selector).first.fill(value)
                return True
            except Exception:
                pass
        
        return False
    
    async def wait_for_user(self, message: str = "Press Enter to continue...") -> None:
        """Pause and wait for user input."""
        print(f"\n⏸️  {message}")
        await asyncio.get_event_loop().run_in_executor(None, input)
    
    async def step_login(self, email: str, password: str) -> Dict[str, Any]:
        """Step through the login process with debugging."""
        log = {"steps": [], "success": False}
        
        def log_step(name: str, success: bool, details: str = ""):
            log["steps"].append({
                "name": name,
                "success": success,
                "details": details,
                "timestamp": datetime.now().isoformat(),
            })
            status = "✅" if success else "❌"
            print(f"  {status} {name}: {details}")
        
        print("\n🔐 Starting login debug flow...\n")
        
        # Step 1: Navigate to home
        print("Step 1: Navigate to SNCF Connect")
        await self.goto("home")
        await asyncio.sleep(2)
        log_step("navigation", True, self._page.url)
        
        # Step 2: Handle cookies
        print("\nStep 2: Cookie consent")
        await self.capture_state("before_cookies")
        cookie_clicked = await self.click_first_working("cookie_consent")
        await asyncio.sleep(1)
        log_step("cookie_consent", cookie_clicked, "clicked" if cookie_clicked else "not found/clicked")
        
        # Step 3: Find and click login
        print("\nStep 3: Find login button")
        await self.capture_state("before_login_click")
        
        # Test all login selectors
        login_results = await self.test_selector_group("login_button")
        working = [r for r in login_results if r.found and r.visible]
        print(f"   Found {len(working)} working login selectors:")
        for r in working:
            print(f"     - {r.selector} (text: {r.text})")
        
        if working:
            await self.wait_for_user("Check the browser. Ready to click login?")
        
        # Click login - this might open a popup
        login_clicked = await self.click_first_working("login_button")
        log_step("login_click", login_clicked)
        
        await asyncio.sleep(3)
        await self.capture_state("after_login_click")
        
        # Check for popup
        pages = self._context.pages
        print(f"\n   Browser has {len(pages)} page(s)")
        
        login_page = self._page
        if len(pages) > 1:
            # There's a popup - use the newest page
            login_page = pages[-1]
            print("   📌 Popup detected - switching to login popup")
            await asyncio.sleep(2)
        
        # Step 4: Fill email
        print("\nStep 4: Fill email")
        await self.capture_state("before_email")
        
        # Test email selectors on the login page
        email_results = []
        for selector in self.KNOWN_SELECTORS["email_input"]:
            try:
                locator = login_page.locator(selector)
                count = await locator.count()
                visible = await locator.first.is_visible() if count > 0 else False
                email_results.append(SelectorTest(
                    selector=selector,
                    found=count > 0,
                    count=count,
                    visible=visible
                ))
            except Exception:
                pass
        
        working_email = [r for r in email_results if r.found and r.visible]
        print(f"   Found {len(working_email)} email inputs")
        
        email_filled = False
        for r in working_email:
            try:
                await login_page.locator(r.selector).first.fill(email)
                email_filled = True
                log_step("email_fill", True, r.selector)
                break
            except Exception as e:
                print(f"     Failed with {r.selector}: {e}")
        
        if not email_filled:
            log_step("email_fill", False, "No working email input")
            await self.wait_for_user("Email input not found. Check browser and press Enter...")
        
        await asyncio.sleep(1)
        
        # Step 5: Fill password
        print("\nStep 5: Fill password")
        password_filled = False
        for selector in self.KNOWN_SELECTORS["password_input"]:
            try:
                locator = login_page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    await locator.first.fill(password)
                    password_filled = True
                    log_step("password_fill", True, selector)
                    break
            except Exception:
                pass
        
        if not password_filled:
            log_step("password_fill", False, "No working password input")
        
        await self.capture_state("credentials_filled")
        await self.wait_for_user("Credentials filled. Check for CAPTCHA. Ready to submit?")
        
        # Step 6: Submit
        print("\nStep 6: Submit login")
        submit_clicked = False
        for selector in self.KNOWN_SELECTORS["submit_button"]:
            try:
                locator = login_page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    await locator.first.click()
                    submit_clicked = True
                    log_step("submit", True, selector)
                    break
            except Exception:
                pass
        
        if not submit_clicked:
            # Try Enter key
            await login_page.keyboard.press("Enter")
            log_step("submit", True, "Enter key fallback")
        
        await asyncio.sleep(5)
        await self.capture_state("after_submit")
        
        # Step 7: Check result
        print("\nStep 7: Check login result")
        
        # Switch back to main page if popup closed
        try:
            if login_page != self._page and login_page.is_closed():
                print("   Popup closed - checking main page")
        except Exception:
            pass
        
        await self._page.reload()
        await asyncio.sleep(3)
        
        logged_in = await self.find_working_selector("logged_in_indicator")
        if logged_in:
            log_step("login_check", True, f"Found: {logged_in}")
            log["success"] = True
        else:
            log_step("login_check", False, "No logged-in indicator found")
            await self.capture_state("login_failed")
        
        await self.capture_state("final_state")
        
        # Save debug log
        log_path = self.debug_dir / f"login_debug_{datetime.now():%Y%m%d_%H%M%S}.json"
        with open(log_path, 'w') as f:
            json.dump(log, f, indent=2, default=str)
        
        print(f"\n📁 Debug log saved to: {log_path}")
        
        return log
    
    async def interactive_shell(self) -> None:
        """Start an interactive debugging shell."""
        print("""
╔══════════════════════════════════════════════════════════╗
║  🔧 SNCF Max Browser Debugger                            ║
║                                                          ║
║  Commands:                                               ║
║    goto <page>     - Navigate (home, login, search...)   ║
║    test <group>    - Test selector group                 ║
║    test-all        - Test all known selectors            ║
║    selector <sel>  - Test a specific selector            ║
║    click <group>   - Click first working in group        ║
║    screenshot      - Take screenshot                     ║
║    login           - Step through login flow             ║
║    url             - Show current URL                    ║
║    html            - Show page HTML snippet              ║
║    help            - Show this help                      ║
║    quit            - Exit debugger                       ║
╚══════════════════════════════════════════════════════════╝
""")
        
        while True:
            try:
                cmd = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("\n🔧 debug> ").strip()
                )
                
                if not cmd:
                    continue
                
                parts = cmd.split(maxsplit=1)
                command = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""
                
                if command == "quit" or command == "exit":
                    print("👋 Bye!")
                    break
                
                elif command == "help":
                    print("Commands: goto, test, test-all, selector, click, screenshot, login, url, html, quit")
                
                elif command == "url":
                    print(f"📍 {self._page.url}")
                
                elif command == "goto":
                    if not arg:
                        print(f"Available pages: {', '.join(self.PAGES.keys())}")
                    else:
                        state = await self.goto(arg)
                        print(f"✅ Navigated to: {state.url}")
                        print(f"📸 Screenshot: {state.screenshot_path}")
                
                elif command == "test":
                    if not arg:
                        print(f"Available groups: {', '.join(self.KNOWN_SELECTORS.keys())}")
                    else:
                        results = await self.test_selector_group(arg)
                        print(f"\nResults for '{arg}':")
                        for r in results:
                            status = "✅" if r.found and r.visible else "⚠️" if r.found else "❌"
                            print(f"  {status} {r.selector}")
                            if r.found:
                                print(f"      count={r.count}, visible={r.visible}, text={r.text}")
                
                elif command == "test-all":
                    results = await self.test_all_selectors()
                    for group, tests in results.items():
                        working = sum(1 for t in tests if t.found and t.visible)
                        print(f"\n{group}: {working}/{len(tests)} working")
                        for t in tests:
                            if t.found:
                                vis = "👁" if t.visible else "🙈"
                                print(f"  {vis} {t.selector}")
                
                elif command == "selector":
                    if not arg:
                        print("Usage: selector <css-selector>")
                    else:
                        result = await self.test_selector(arg)
                        status = "✅" if result.found else "❌"
                        print(f"{status} Selector: {arg}")
                        print(f"   Found: {result.found}, Count: {result.count}, Visible: {result.visible}")
                        if result.text:
                            print(f"   Text: {result.text}")
                        if result.attributes:
                            print(f"   Attrs: {result.attributes}")
                        if result.error:
                            print(f"   Error: {result.error}")
                
                elif command == "click":
                    if not arg:
                        print(f"Available groups: {', '.join(self.KNOWN_SELECTORS.keys())}")
                    else:
                        clicked = await self.click_first_working(arg)
                        print("✅ Clicked!" if clicked else "❌ No clickable element found")
                
                elif command == "screenshot":
                    state = await self.capture_state("manual")
                    print(f"📸 Screenshot saved: {state.screenshot_path}")
                
                elif command == "login":
                    email = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("Email: ").strip()
                    )
                    password = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("Password: ").strip()
                    )
                    if email and password:
                        await self.step_login(email, password)
                    else:
                        print("Email and password required")
                
                elif command == "html":
                    html = await self._page.content()
                    # Show first 2000 chars
                    print(html[:2000])
                    print(f"\n... ({len(html)} total chars)")
                
                else:
                    print(f"Unknown command: {command}. Type 'help' for commands.")
                    
            except KeyboardInterrupt:
                print("\n👋 Interrupted")
                break
            except Exception as e:
                print(f"❌ Error: {e}")
    
    def generate_selector_report(self) -> str:
        """Generate a report of all selector tests."""
        if not self._states:
            return "No states captured yet."
        
        lines = ["# Selector Test Report", ""]
        
        for state in self._states:
            lines.append(f"## {state.url}")
            lines.append(f"Time: {state.timestamp}")
            lines.append(f"Screenshot: {state.screenshot_path}")
            lines.append("")
            
            for test in state.selectors_tested:
                status = "✅" if test.found and test.visible else "⚠️" if test.found else "❌"
                lines.append(f"- {status} `{test.selector}`")
                if test.found:
                    lines.append(f"  - Count: {test.count}, Visible: {test.visible}")
                    if test.text:
                        lines.append(f"  - Text: {test.text}")
            
            lines.append("")
        
        return "\n".join(lines)


async def run_debug_session(config: Optional[SNCFConfig] = None) -> None:
    """Run an interactive debug session."""
    async with BrowserDebugger(config) as debugger:
        await debugger.interactive_shell()


def debug_sync(config: Optional[SNCFConfig] = None) -> None:
    """Synchronous entry point for debug session."""
    asyncio.run(run_debug_session(config))

