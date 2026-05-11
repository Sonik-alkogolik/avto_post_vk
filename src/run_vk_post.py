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
JOIN_COOLDOWN_SECONDS = 300


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
    logger(f"[debug] click_first_available: candidates={len(selector_candidates)}, timeout_ms={timeout_ms}")
    for candidate in selector_candidates:
        try:
            logger(f"[debug] try selector: {candidate}")
            locator = page.locator(candidate).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            logger(f"Clicked by selector: {candidate}")
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger(f"[debug] selector failed: {candidate} | {exc}")
    raise RuntimeError(f"Cannot click any candidate selector: {selector_candidates}") from last_error


def click_first_available_with_retries(
    page,
    selector_candidates: list[str],
    timeout_ms: int,
    logger: Callable[[str], None],
    retries: int = 3,
) -> None:
    last_error: Exception | None = None
    logger(f"[debug] click_first_available_with_retries: retries={retries}, timeout_ms={timeout_ms}")
    for attempt in range(1, retries + 1):
        try:
            logger(f"[debug] open-click attempt {attempt}/{retries}")
            per_selector_timeout_ms = max(1200, min(3500, timeout_ms // max(1, len(selector_candidates))))
            click_first_available(page, selector_candidates, per_selector_timeout_ms, logger)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger(f"[debug] open-click attempt failed {attempt}/{retries}: {exc}")
            if attempt < retries:
                page.wait_for_timeout(1200)
                logger("[debug] reload before retry open-click")
                page.reload(wait_until="domcontentloaded", timeout=max(30000, timeout_ms))
                page.wait_for_timeout(1800)
    raise RuntimeError("Failed to click open suggest post button after retries") from last_error


def click_when_ready(page, selector: str, timeout_ms: int, logger: Callable[[str], None]) -> None:
    logger(f"[debug] click_when_ready: {selector}")
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
        logger(f"[debug] click_testid_when_ready: {testid}")
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
            logger(f"[debug] modal primary candidate: {selector}")
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
    logger(f"[debug] click_first_ready_candidate: candidates={len(selector_candidates)}")
    for candidate in selector_candidates:
        try:
            click_when_ready(page, candidate, timeout_ms, logger)
            return True
        except Exception:  # noqa: BLE001
            logger(f"[debug] candidate not ready: {candidate}")
    return False


def try_join_community(page, timeout_ms: int, logger: Callable[[str], None]) -> bool:
    join_candidates = [
        'button[data-testid="group-subscribe-button"]',
        '[data-testid="group-subscribe-button"]',
        'button:has-text("Вступить")',
        '[role="button"]:has-text("Вступить")',
        'button:has-text("Подписаться")',
        '[role="button"]:has-text("Подписаться")',
        'button[data-testid*="join"]',
        '[data-testid*="join"] button',
    ]
    # First try exact VK test-id path that user provided.
    try:
        logger("[debug] try direct subscribe by data-testid")
        locator = page.get_by_test_id("group-subscribe-button").first
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.scroll_into_view_if_needed(timeout=timeout_ms)
        try:
            locator.click(timeout=timeout_ms)
        except Exception:
            locator.click(timeout=timeout_ms, force=True)
        logger("Joined/subscribed using get_by_test_id(group-subscribe-button)")
        page.wait_for_timeout(2000)
        return True
    except Exception:  # noqa: BLE001
        logger("[debug] direct subscribe by test-id failed, trying fallback selectors")

    for candidate in join_candidates:
        try:
            logger(f"[debug] try join selector: {candidate}")
            locator = page.locator(candidate).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.scroll_into_view_if_needed(timeout=timeout_ms)
            try:
                locator.click(timeout=timeout_ms)
            except Exception:
                locator.click(timeout=timeout_ms, force=True)
            logger(f"Joined/subscribed using selector: {candidate}")
            page.wait_for_timeout(1500)
            return True
        except Exception:  # noqa: BLE001
            logger(f"[debug] join selector failed: {candidate}")
            continue
    logger("Join/subscribe button was not found")
    return False


def has_existing_suggested_post(page, logger: Callable[[str], None]) -> bool:
    # Fast-path by explicit VK test ids.
    try:
        btn = page.get_by_test_id("group_unpublished_button").first
        if btn.count() > 0 and btn.is_visible():
            logger("[debug] detected group_unpublished_button (suggested post already exists)")
            return True
    except Exception as exc:  # noqa: BLE001
        logger(f"[debug] check group_unpublished_button failed: {exc}")

    # Fallback for layout/AB variations: check any visible suggested-link UI markers.
    try:
        detected = page.evaluate(
            """() => {
              const nodes = Array.from(document.querySelectorAll('a, button, [role="button"]'));
              for (const el of nodes) {
                const txt = (el.textContent || '').toLowerCase();
                const href = (el.getAttribute('href') || '').toLowerCase();
                const testid = (el.getAttribute('data-testid') || '').toLowerCase();
                const hasSuggestedWord = txt.includes('предложенн');
                const hasSuggestedHref = href.includes('suggested=1');
                const hasSuggestedTestid = testid.includes('group_unpublished');
                if ((hasSuggestedWord && hasSuggestedHref) || hasSuggestedTestid) {
                  return true;
                }
              }
              // Counter itself can be present even if parent wasn't matched above.
              const counter = document.querySelector('[data-testid="group_unpublished_counter"]');
              if (counter) {
                const n = parseInt((counter.textContent || '').trim(), 10);
                if (!Number.isNaN(n) && n > 0) return true;
              }
              return false;
            }"""
        )
        if bool(detected):
            logger("[debug] detected existing suggested marker via fallback DOM scan")
            return True
    except Exception as exc:  # noqa: BLE001
        logger(f"[debug] fallback suggested-marker scan failed: {exc}")
    return False


def should_skip_due_to_existing_suggested(page, logger: Callable[[str], None]) -> bool:
    # Retry because VK may render counters/buttons asynchronously.
    for attempt in range(1, 4):
        if has_existing_suggested_post(page, logger):
            logger(f"[debug] suggested marker detected on attempt {attempt}/3")
            return True
        logger(f"[debug] no suggested marker on attempt {attempt}/3")
        page.wait_for_timeout(900)
    return False


def dismiss_unsaved_changes_dialog(page, logger: Callable[[str], None]) -> bool:
    candidates = [
        'button:has-text("Выйти без сохранения")',
        '[role="button"]:has-text("Выйти без сохранения")',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=5000)
                logger("[debug] unsaved-changes dialog dismissed")
                page.wait_for_timeout(600)
                return True
        except Exception:
            continue
    return False


def normalize_text(value: str) -> str:
    return " ".join((value or "").replace("\u00a0", " ").split())


def set_post_text_strict(page, selector: str, text: str, timeout_ms: int, logger: Callable[[str], None]) -> None:
    target_norm = normalize_text(text)
    last_seen = ""
    for attempt in range(1, 4):
        logger(f"[debug] fill post text attempt {attempt}/3")
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.click(timeout=timeout_ms)
        try:
            loc.fill(text, timeout=timeout_ms)
        except Exception:
            # Fallback for non-input editable nodes.
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            loc.type(text, delay=12, timeout=timeout_ms)

        page.wait_for_timeout(200)
        try:
            current = page.evaluate(
                """(sel) => {
                  const el = document.querySelector(sel);
                  if (!el) return '';
                  const anyEl = el;
                  if (typeof anyEl.value === 'string') return anyEl.value;
                  return (anyEl.innerText || anyEl.textContent || '').trim();
                }""",
                selector,
            )
        except Exception:
            current = ""
        last_seen = str(current)
        if normalize_text(last_seen) == target_norm:
            logger(f"[debug] post text verified, len={len(last_seen)}")
            return

        logger("[debug] post text mismatch after fill, retrying...")
        # Hard fallback: set textContent/value via JS and dispatch input.
        page.evaluate(
            """([sel, val]) => {
              const el = document.querySelector(sel);
              if (!el) return;
              if ('value' in el) {
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return;
              }
              el.textContent = val;
              el.dispatchEvent(new Event('input', { bubbles: true }));
            }""",
            [selector, text],
        )
        page.wait_for_timeout(250)

    raise RuntimeError(
        "Post text did not match template after retries. "
        f"Expected len={len(text)}, seen preview={last_seen[:160]!r}"
    )


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
    auto_join_before_post: bool = False,
    join_cooldown_seconds: int = JOIN_COOLDOWN_SECONDS,
    skip_if_unpublished_exists: bool = False,
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

    logger(
        "[debug] run params: "
        f"headless={headless}, submit_post={submit_post}, use_existing_browser={use_existing_browser}, "
        f"auto_join_before_post={auto_join_before_post}, join_cooldown_seconds={join_cooldown_seconds}"
    )

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
                logger("[debug] page opened and initial wait done")
                dismiss_unsaved_changes_dialog(page, logger)
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
                        '[data-testid="group_publish_block_button"]',
                        '[data-testid="group_publish_block"] button',
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
                if auto_join_before_post:
                    logger("[debug] auto-join enabled: trying subscribe click before searching suggest-post button")
                    joined = try_join_community(page, min(element_ms, 8000), logger)
                    if joined:
                        cooldown = max(0, int(join_cooldown_seconds))
                        logger(
                            f"Auto-join completed. Waiting {cooldown} sec before retry "
                            "to reduce captcha/anti-spam risk..."
                        )
                        page.wait_for_timeout(cooldown * 1000)
                        logger("Reload page after auto-join")
                        page.reload(wait_until="domcontentloaded", timeout=max(30000, open_page_ms))
                        page.wait_for_timeout(1800)
                        logger("[debug] page reloaded after join")
                        dismiss_unsaved_changes_dialog(page, logger)

                # Skip-check is intentionally after auto-join attempt.
                if skip_if_unpublished_exists and should_skip_due_to_existing_suggested(page, logger):
                    logger("Skip posting: existing suggested post detected in group")
                    return

                try:
                    click_first_available_with_retries(page, open_candidates, element_ms, logger, retries=3)
                except Exception as open_exc:
                    if not auto_join_before_post:
                        raise
                    logger(f"[debug] suggest-post button still unavailable after auto-join flow: {open_exc}")
                    raise
                human_pause(page)

                logger("[3/7] Wait posting modal")
                posting_modal_candidates = [
                    selectors["posting_modal"],
                    '[data-testid="posting_modal_box"]',
                    '[data-testid="posting_box"]',
                    '[role="dialog"]',
                ]
                posting_modal_visible = False
                last_modal_exc: Exception | None = None
                for modal_sel in posting_modal_candidates:
                    try:
                        logger(f"[debug] wait posting modal candidate: {modal_sel}")
                        page.locator(modal_sel).first.wait_for(state="visible", timeout=min(element_ms, 9000))
                        selectors["posting_modal"] = modal_sel
                        posting_modal_visible = True
                        logger(f"[debug] posting modal is visible via: {modal_sel}")
                        break
                    except Exception as modal_exc:  # noqa: BLE001
                        last_modal_exc = modal_exc
                        continue

                if not posting_modal_visible:
                    # Some groups switch UI state after clicking suggest; re-check skip markers before failing.
                    if skip_if_unpublished_exists and should_skip_due_to_existing_suggested(page, logger):
                        logger("Skip posting after click: existing suggested post detected in group")
                        dismiss_unsaved_changes_dialog(page, logger)
                        return
                    raise RuntimeError(f"Posting modal was not found after click: {last_modal_exc}")

                if skip_if_unpublished_exists and should_skip_due_to_existing_suggested(page, logger):
                    logger("Skip posting in modal: existing suggested post detected in group")
                    dismiss_unsaved_changes_dialog(page, logger)
                    return
                human_pause(page)

                logger(f"[4/7] Upload image: {image_path}")
                page.locator(selectors["file_input"]).first.set_input_files(str(image_path), timeout=element_ms)
                logger("[debug] image upload triggered")
                human_pause(page, 300, 800)

                logger("[5/7] Fill post text")
                set_post_text_strict(page, selectors["message_input"], profile_post_text, element_ms, logger)
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
                    logger("[debug] intermediate 'Next' click succeeded")
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
    parser.add_argument(
        "--auto-join-before-post",
        action="store_true",
        help="Try joining/subscribing to community if suggest-post button is not available",
    )
    parser.add_argument(
        "--join-cooldown-seconds",
        type=int,
        default=JOIN_COOLDOWN_SECONDS,
        help="Cooldown after successful join/subscribe before retrying post flow",
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
        auto_join_before_post=args.auto_join_before_post,
        join_cooldown_seconds=args.join_cooldown_seconds,
    )
