import argparse
import json
import random
from pathlib import Path
from typing import Any
from typing import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


REQUIRED_SELECTOR_KEYS = (
    "posting_modal",
    "file_input",
    "message_input",
    "suggest_button",
)


def save_screenshot_safe(page, screenshot_path: Path, logger: Callable[[str], None]) -> None:
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        logger(f"Saved screenshot: {screenshot_path}")
    except Exception as screenshot_exc:  # noqa: BLE001
        logger(f"Failed to save screenshot {screenshot_path}: {screenshot_exc}")


def human_pause(page, min_ms: int = 180, max_ms: int = 520) -> None:
    page.wait_for_timeout(random.randint(min_ms, max_ms))


def load_profile(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_profile(profile: dict[str, Any], profile_path: Path, repo_root: Path) -> None:
    required_top_level_keys = ("url", "image_path", "post_text", "selectors")
    missing_top = [k for k in required_top_level_keys if not profile.get(k)]
    if missing_top:
        raise ValueError(f"Profile {profile_path} is missing required fields: {', '.join(missing_top)}")

    selectors = profile["selectors"]
    if not isinstance(selectors, dict):
        raise ValueError(f"Profile {profile_path}: 'selectors' must be an object")

    missing_selector_keys = [k for k in REQUIRED_SELECTOR_KEYS if not selectors.get(k)]
    if missing_selector_keys:
        raise ValueError(
            f"Profile {profile_path} is missing selector fields: {', '.join(missing_selector_keys)}"
        )

    timeouts = profile.get("timeouts", {})
    if timeouts and not isinstance(timeouts, dict):
        raise ValueError(f"Profile {profile_path}: 'timeouts' must be an object")

    for timeout_key in ("open_page_ms", "element_ms", "submit_ms"):
        if timeout_key in timeouts:
            value = timeouts[timeout_key]
            if not isinstance(value, int) or value <= 0:
                raise ValueError(
                    f"Profile {profile_path}: timeout '{timeout_key}' must be a positive integer"
                )

    image_path = resolve_image_path(repo_root, str(profile["image_path"]))
    if not image_path.exists():
        raise FileNotFoundError(f"Profile {profile_path}: image file not found: {image_path}")


def resolve_image_path(repo_root: Path, image_path: str) -> Path:
    p = Path(image_path)
    if p.is_absolute():
        return p
    return (repo_root / p).resolve()


def click_first_available(page, selector_candidates: list[str], timeout_ms: int, logger: Callable[[str], None]) -> None:
    last_error: Exception | None = None
    for candidate in selector_candidates:
        try:
            locator = page.locator(candidate).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            logger(f"Clicked by selector: {candidate}")
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger(f"Selector not ready: {candidate}")
    raise RuntimeError(f"Cannot click any candidate selector: {selector_candidates}") from last_error


def click_first_available_with_retries(
    page,
    selector_candidates: list[str],
    timeout_ms: int,
    logger: Callable[[str], None],
    retries: int = 3,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            click_first_available(page, selector_candidates, timeout_ms, logger)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger(f"Open button attempt {attempt}/{retries} failed")
            if attempt < retries:
                page.wait_for_timeout(1200)
                page.reload(wait_until="domcontentloaded", timeout=max(30000, timeout_ms))
                page.wait_for_timeout(1800)
    raise RuntimeError("Failed to click open suggest post button after retries") from last_error


def click_when_ready(page, selector: str, timeout_ms: int, logger: Callable[[str], None]) -> None:
    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_function(
        """(el) => {
            if (!el) return false;
            const disabled = el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true';
            return !disabled;
        }""",
        locator.element_handle(),
        timeout=timeout_ms,
    )
    locator.click(timeout=timeout_ms)
    logger(f"Clicked when ready: {selector}")


def click_testid_when_ready(page, testid: str, timeout_ms: int, logger: Callable[[str], None]) -> bool:
    try:
        locator = page.get_by_test_id(testid).first
        locator.wait_for(state="visible", timeout=timeout_ms)
        try:
            locator.click(timeout=timeout_ms)
        except Exception:
            # Some VK modal states keep the button visible but intercept normal click.
            locator.click(timeout=timeout_ms, force=True)
        logger(f"Clicked by testid: {testid}")
        return True
    except Exception:  # noqa: BLE001
        logger(f"Testid not ready yet: {testid}")
        return False


def click_modal_primary_button(page, posting_modal_selector: str, timeout_ms: int, logger: Callable[[str], None]) -> bool:
    candidates = [
        f'{posting_modal_selector} button.vkuiButton__modePrimary',
        f'{posting_modal_selector} .vkuiButton__modePrimary',
        f'{posting_modal_selector} button[class*="vkuiButton__modePrimary"]',
    ]
    for selector in candidates:
        try:
            locator = page.locator(selector).last
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            logger(f"Clicked modal primary button by structure: {selector}")
            return True
        except Exception:  # noqa: BLE001
            logger(f"Modal primary candidate not ready: {selector}")
    return False


def click_first_ready_candidate(
    page,
    selector_candidates: list[str],
    timeout_ms: int,
    logger: Callable[[str], None],
) -> bool:
    for candidate in selector_candidates:
        try:
            click_when_ready(page, candidate, timeout_ms, logger)
            return True
        except Exception:  # noqa: BLE001
            logger(f"Candidate not ready yet: {candidate}")
    return False


def run(
    profile_path: Path,
    headless: bool,
    pause_before_submit: bool,
    submit_post: bool = True,
    image_path_override: str | None = None,
    post_text_override: str | None = None,
    log: Callable[[str], None] | None = None,
    use_existing_browser: bool = False,
    cdp_url: str = "http://127.0.0.1:9222",
    wait_for_manual_login: bool = False,
    keep_browser_open: bool = True,
    user_data_dir: str = "profiles/chromium_user_data",
    submit_from_open_modal: bool = False,
) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    profile = load_profile(profile_path)
    validate_profile(profile, profile_path, repo_root)
    logger = log or print

    selectors = profile["selectors"]
    timeouts = profile.get("timeouts", {})
    open_page_ms = int(timeouts.get("open_page_ms", 30000))
    element_ms = int(timeouts.get("element_ms", 20000))

    profile_image_path = image_path_override if image_path_override else profile["image_path"]
    profile_post_text = post_text_override if post_text_override is not None else profile["post_text"]

    image_path = resolve_image_path(repo_root, profile_image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    with sync_playwright() as p:
        owns_browser = True
        if use_existing_browser:
            browser = p.chromium.connect_over_cdp(cdp_url)
            owns_browser = False
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = browser.new_context()
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()
            logger(f"Connected to existing browser via CDP: {cdp_url}")
        else:
            profile_dir = Path(user_data_dir)
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir.resolve()),
                headless=headless,
            )
            browser = context.browser
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()
            logger(f"Using persistent browser profile: {profile_dir.resolve()}")

        try:
            if not submit_from_open_modal:
                logger(f"[1/7] Open: {profile['url']}")
                page.goto(profile["url"], wait_until="domcontentloaded", timeout=open_page_ms)
                page.wait_for_timeout(1500)
                if wait_for_manual_login:
                    input(
                        "\nВыполните ручной вход в VK в открытом окне браузера, затем нажмите Enter для продолжения...\n"
                    )

                logger("[2/7] Click suggest post button")
                open_candidates = []
                if "open_suggest_post_button_candidates" in selectors:
                    open_candidates.extend(selectors["open_suggest_post_button_candidates"])
                if "open_suggest_post_button" in selectors:
                    open_candidates.append(selectors["open_suggest_post_button"])
                open_candidates.extend(
                    [
                        'button[data-testid="group_publish_block_button"]',
                        'button:has-text("Предложить пост")',
                        'button:has-text("Предложить новость")',
                        'button:has-text("Создать")',
                        '[role="button"]:has-text("Создать")',
                        '[role="button"]:has-text("Предложить новость")',
                        'button:has-text("Предложить")',
                        '[role="button"]:has-text("Предложить")',
                        '[role="button"]:has-text("Предложить пост")',
                        '[data-testid="group_publish_block"] :text("Предложить пост")',
                    ]
                )
                click_first_available_with_retries(page, open_candidates, element_ms, logger, retries=3)
                human_pause(page)

                logger("[3/7] Wait posting modal")
                page.locator(selectors["posting_modal"]).wait_for(state="visible", timeout=element_ms)
                human_pause(page)

                logger(f"[4/7] Upload image: {image_path}")
                page.locator(selectors["file_input"]).first.set_input_files(str(image_path), timeout=element_ms)
                human_pause(page, 300, 800)

                logger("[5/7] Fill post text")
                page.locator(selectors["message_input"]).first.fill(profile_post_text, timeout=element_ms)
                human_pause(page, 250, 700)
            else:
                logger("[1/7] Submit mode from already opened modal")
                page.locator(selectors["posting_modal"]).wait_for(state="visible", timeout=element_ms)
                logger("[2/7] Modal is visible, skip open/fill steps")

            if pause_before_submit:
                input("\nПауза перед отправкой включена. Проверьте форму и нажмите Enter для продолжения...\n")

            if submit_post:
                logger("[6/7] Click final suggest button")
                submit_ms = int(timeouts.get("submit_ms", 60000))
                short_wait_ms = min(8000, submit_ms)

                # VK often shows an intermediate "Next" step before final submit.
                clicked_next = click_testid_when_ready(page, "posting_base_screen_next", short_wait_ms, logger)
                next_candidates = [
                    'button[data-testid="posting_base_screen_next"]',
                    'button:has-text("Далее")',
                    '[role="button"]:has-text("Далее")',
                ]
                if not clicked_next and click_first_ready_candidate(page, next_candidates, short_wait_ms, logger):
                    clicked_next = True
                if clicked_next:
                    human_pause(page, 300, 700)

                suggest_candidates = [
                    selectors["suggest_button"],
                    'button:has-text("Предложить")',
                    'button:has-text("Отправить")',
                ]
                if not click_first_ready_candidate(page, suggest_candidates, short_wait_ms, logger):
                    # One more attempt: if "Next" appears after async UI update, click it and retry submit.
                    if click_first_ready_candidate(page, next_candidates, short_wait_ms, logger):
                        human_pause(page, 300, 700)
                    if not click_first_ready_candidate(page, suggest_candidates, short_wait_ms, logger):
                        if click_modal_primary_button(page, selectors["posting_modal"], short_wait_ms, logger):
                            human_pause(page, 300, 700)
                        else:
                            raise RuntimeError("Final suggest button was not found after filling the post form")
                logger("[7/7] Done: click executed")
            else:
                logger("[6/7] Test mode: submit skipped")
                logger("[7/7] Done: form opened and filled")
        except PlaywrightTimeoutError as exc:
            reports_dir = repo_root / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = reports_dir / "vk_error_timeout.png"
            save_screenshot_safe(page, screenshot_path, logger)
            raise RuntimeError(f"Timeout while executing scenario: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            reports_dir = repo_root / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = reports_dir / "vk_error_generic.png"
            save_screenshot_safe(page, screenshot_path, logger)
            logger(f"Scenario failed with error: {exc}")
            raise
        finally:
            if owns_browser and keep_browser_open:
                input("\nСценарий завершён. Браузер оставлен открытым. Нажмите Enter, чтобы закрыть его...\n")
            if owns_browser and not keep_browser_open:
                context.close()
                browser.close()
            elif owns_browser and keep_browser_open:
                context.close()
                browser.close()
            elif not owns_browser:
                browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VK suggested post automation for one community profile")
    parser.add_argument(
        "--profile",
        default="profiles/baraholka_dnr.json",
        help="Path to community profile json",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="Pause before final submit click",
    )
    parser.add_argument(
        "--use-existing-browser",
        action="store_true",
        help="Attach to existing Chrome/Edge via CDP (default URL http://127.0.0.1:9222)",
    )
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="CDP URL for existing browser",
    )
    parser.add_argument(
        "--wait-for-manual-login",
        action="store_true",
        help="Pause after opening page to allow manual VK login in the opened browser",
    )
    parser.add_argument(
        "--close-browser",
        action="store_true",
        help="Close launched browser after script finishes",
    )
    parser.add_argument(
        "--user-data-dir",
        default="profiles/chromium_user_data",
        help="Path to persistent Chromium profile directory (keeps VK login between runs)",
    )
    parser.add_argument(
        "--submit-from-open-modal",
        action="store_true",
        help="Skip open/fill and only click Next/Submit in already opened posting modal",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        profile_path=Path(args.profile),
        headless=args.headless,
        pause_before_submit=args.no_submit,
        use_existing_browser=args.use_existing_browser,
        cdp_url=args.cdp_url,
        wait_for_manual_login=args.wait_for_manual_login,
        keep_browser_open=not args.close_browser,
        user_data_dir=args.user_data_dir,
        submit_from_open_modal=args.submit_from_open_modal,
    )
