import json
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "reports" / "vk_feed_state.json"


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"seen_post_ids": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_post_ids": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_posts_from_page(page, max_posts_per_group: int = 10) -> list[dict[str, str]]:
    script = """
    (maxPosts) => {
      const out = [];
      const posts = Array.from(document.querySelectorAll('[data-testid="post"][data-post-id]'));
      for (const post of posts) {
        const nesting = post.getAttribute('data-post-nesting-lvl');
        if (nesting !== null && nesting !== '0') continue;
        const postId = post.getAttribute('data-post-id') || '';
        if (!postId) continue;

        const titleEl = post.querySelector('[data-testid="post-header-title"]');
        const groupTitle = (titleEl?.textContent || '').trim();

        const dateEl = post.querySelector('[data-testid="post_date_block_preview"]');
        const relDate = (dateEl?.textContent || '').trim();
        let postUrl = '';
        if (dateEl && dateEl.getAttribute('href')) {
          postUrl = new URL(dateEl.getAttribute('href'), location.origin).toString();
        }

        const textHost =
          post.querySelector('[data-testid="showmoretext-in-expanded"]') ||
          post.querySelector('[data-testid="showmoretext-in"]') ||
          post.querySelector('[data-testid="showmoretext"]');
        const text = (textHost?.innerText || '').trim();

        out.push({
          post_id: postId,
          group_title: groupTitle,
          rel_date: relDate,
          post_url: postUrl,
          text: text,
        });
        if (out.length >= maxPosts) break;
      }
      return out;
    }
    """
    return page.evaluate(script, max_posts_per_group)


def wait_for_feed_posts(page, log: Callable[[str], None]) -> None:
    selectors = [
        '[data-testid="post"][data-post-id]',
        '#page-wall [data-testid="post"][data-post-id]',
    ]
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=12000)
            return
        except Exception:
            continue

    # Sometimes VK virtual feed renders after a tiny interaction/scroll.
    try:
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(1200)
        page.wait_for_selector('[data-testid="post"][data-post-id]', timeout=8000)
    except Exception:
        log("Feed posts are not visible yet after wait/scroll")


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": False,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url=url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<no response body>"
        raise RuntimeError(f"Telegram API HTTP {exc.code}: {body}") from exc


def resolve_chat_id(token: str, explicit_chat_id: str, log: Callable[[str], None]) -> str:
    if explicit_chat_id.strip():
        return explicit_chat_id.strip()

    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        updates = data.get("result", [])
        for upd in reversed(updates):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is not None:
                log(f"Resolved TELEGRAM_CHAT_ID from bot updates: {chat_id}")
                return str(chat_id)
    except Exception as exc:  # noqa: BLE001
        log(f"Failed to resolve chat id from getUpdates: {exc}")

    raise RuntimeError(
        "TELEGRAM_CHAT_ID is empty and could not be resolved from bot updates. "
        "Send /start to your bot and try again, or set TELEGRAM_CHAT_ID manually."
    )


def format_post_message(profile_name: str, post: dict[str, str]) -> str:
    text = post.get("text", "").strip()
    if len(text) > 900:
        text = text[:900] + "..."
    return (
        f"Новый пост в группе: {profile_name}\n"
        f"Источник: {post.get('group_title', '')}\n"
        f"Время: {post.get('rel_date', '')}\n"
        f"Ссылка: {post.get('post_url', '')}\n\n"
        f"{text}"
    )


def matches_keywords(post: dict[str, str], keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{post.get('group_title', '')}\n{post.get('text', '')}".lower()
    return any(kw in haystack for kw in keywords)


def monitor_once(
    profiles: list[dict[str, str]],
    cdp_url: str,
    telegram_token: str,
    telegram_chat_id: str,
    dry_run: bool,
    max_posts_per_group: int,
    per_group_wait_seconds: int,
    filter_keywords: list[str],
    log: Callable[[str], None],
) -> tuple[int, int]:
    state = load_state()
    seen_post_ids = state.setdefault("seen_post_ids", {})
    sent_count = 0
    new_count = 0

    with sync_playwright() as p:
        resolved_chat_id = resolve_chat_id(telegram_token, telegram_chat_id, log)
        browser = p.chromium.connect_over_cdp(cdp_url)
        try:
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            log(f"Connected to existing browser via CDP: {cdp_url}")

            for idx, profile in enumerate(profiles, start=1):
                profile_id = profile["profile_id"]
                profile_name = profile["name"]
                url = profile["url"]
                log(f"[{idx}/{len(profiles)}] Scan group: {profile_name} ({url})")
                try:
                    opened = False
                    for attempt in range(1, 4):
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=60000)
                            opened = True
                            break
                        except Exception as nav_exc:  # noqa: BLE001
                            log(f"Open failed attempt {attempt}/3: {nav_exc}")
                            page.wait_for_timeout(2500)
                    if not opened:
                        log("Skip group after 3 failed open attempts")
                        continue

                    page.wait_for_timeout(1800)
                    if per_group_wait_seconds > 0:
                        log(f"Wait on group page: {per_group_wait_seconds} sec")
                        time.sleep(per_group_wait_seconds)
                    wait_for_feed_posts(page, log)
                    posts = extract_posts_from_page(page, max_posts_per_group=max_posts_per_group)
                    log(f"Found posts: {len(posts)}")

                    group_seen = set(seen_post_ids.get(profile_id, []))
                    group_new: list[dict[str, str]] = []
                    for post in posts:
                        post_id = post.get("post_id", "")
                        if not post_id:
                            continue
                        if post_id not in group_seen:
                            group_new.append(post)

                    if not group_new:
                        log("No new posts")
                        continue

                    if filter_keywords:
                        before = len(group_new)
                        group_new = [p for p in group_new if matches_keywords(p, filter_keywords)]
                        log(f"Filter matched: {len(group_new)}/{before}")
                        if not group_new:
                            log("No new posts matched the active filter")
                            continue

                    log(f"New posts: {len(group_new)}")
                    new_count += len(group_new)
                    for post in reversed(group_new):
                        message = format_post_message(profile_name, post)
                        if dry_run:
                            log(f"[DRY-RUN] would send post {post.get('post_id')}")
                        else:
                            try:
                                send_telegram_message(telegram_token, resolved_chat_id, message)
                                sent_count += 1
                                log(f"Sent post {post.get('post_id')}")
                            except Exception as send_exc:  # noqa: BLE001
                                log(f"Failed to send post {post.get('post_id')}: {send_exc}")
                        group_seen.add(post.get("post_id", ""))

                    seen_post_ids[profile_id] = list(group_seen)[-500:]
                    save_state(state)
                except Exception as group_exc:  # noqa: BLE001
                    log(f"Group failed and skipped: {group_exc}")
                    continue
        finally:
            browser.close()

    return new_count, sent_count


def monitor_loop(
    profiles: list[dict[str, str]],
    cdp_url: str,
    telegram_token: str,
    telegram_chat_id: str,
    dry_run: bool,
    interval_seconds: int,
    per_group_wait_seconds: int,
    cycles: int,
    max_posts_per_group: int,
    filter_keywords: list[str],
    log: Callable[[str], None],
) -> None:
    total_new = 0
    total_sent = 0
    for cycle in range(1, cycles + 1):
        log(f"=== Monitor cycle {cycle}/{cycles} ===")
        new_count, sent_count = monitor_once(
            profiles=profiles,
            cdp_url=cdp_url,
            telegram_token=telegram_token,
            telegram_chat_id=telegram_chat_id,
            dry_run=dry_run,
            max_posts_per_group=max_posts_per_group,
            per_group_wait_seconds=per_group_wait_seconds,
            filter_keywords=filter_keywords,
            log=log,
        )
        total_new += new_count
        total_sent += sent_count
        log(f"Cycle result: new={new_count}, sent={sent_count}")
        if cycle < cycles and interval_seconds > 0:
            log(f"Sleep {interval_seconds} sec before next cycle...")
            time.sleep(interval_seconds)
    log(f"=== Monitor finished: total_new={total_new}, total_sent={total_sent} ===")
