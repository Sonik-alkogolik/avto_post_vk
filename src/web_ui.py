from pathlib import Path
import json
import traceback
import time
import os
import queue
import threading
import re

import gradio as gr

from run_vk_post import load_profile, run, validate_profile
from vk_feed_to_telegram import monitor_loop


REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = REPO_ROOT / "profiles"
PROFILES_V2_DIR = REPO_ROOT / "profiles_v2"
VK_FEED_STATE_PATH = REPO_ROOT / "reports" / "vk_feed_state.json"
V2_LOOP_STATE_PATH = REPO_ROOT / "reports" / "v2_loop_state.json"
ENV_PATH = REPO_ROOT / ".env"
FILTERS_PATH = REPO_ROOT / "reports" / "monitor_filters.json"
V2_GROUPS_PATH = REPO_ROOT / "reports" / "autopost_v2_groups.json"
V2_TEMPLATES_PATH = REPO_ROOT / "reports" / "autopost_v2_templates.json"

DEFAULT_PROFILE_SELECTORS = {
    "open_suggest_post_button": '[data-testid="group_publish_block"] button',
    "open_suggest_post_button_candidates": [
        '[data-testid="group_publish_block"] button',
        'button[data-testid="group_publish_block_button"]',
        'button:has-text("Предложить пост")',
        'button:has-text("Предложить новость")',
        '[role="button"]:has-text("Предложить")',
    ],
    "posting_modal": '[data-testid="posting_modal_box"]',
    "file_input": 'input[data-testid="posting_base_screen_download_from_device"]',
    "message_input": '[data-testid="posting_base_screen_input_message"]',
    "suggest_button": 'button[data-testid="posting_suggest_button"]',
}

DEFAULT_PROFILE_TIMEOUTS = {
    "open_page_ms": 30000,
    "element_ms": 20000,
    "submit_ms": 60000,
}

DEFAULT_AUTO_KEYWORDS = [
    "авто",
    "автомобиль",
    "машина",
    "продам авто",
    "продам машину",
    "продам автомобиль",
    "куплю авто",
    "авто с пробегом",
    "обмен авто",
    "птс",
    "vin",
    "внедорожник",
    "седан",
    "хэтчбек",
    "универсал",
    "кроссовер",
    "пикап",
    "электромобиль",
    "toyota",
    "honda",
    "bmw",
    "mercedes",
    "audi",
    "ford",
    "kia",
    "hyundai",
    "lada",
    "ваз",
    "nissan",
    "renault",
    "mitsubishi",
    "volkswagen",
    "skoda",
    "opel",
]


def load_local_env_file() -> None:
    if not ENV_PATH.exists():
        return
    try:
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # UI should keep working even if .env has a malformed line.
        pass


def parse_keywords(raw: str) -> list[str]:
    items = []
    for line in raw.replace(",", "\n").splitlines():
        kw = line.strip().lower()
        if kw:
            items.append(kw)
    uniq = []
    seen = set()
    for kw in items:
        if kw not in seen:
            seen.add(kw)
            uniq.append(kw)
    return uniq


def load_filters() -> dict[str, list[str]]:
    if not FILTERS_PATH.exists():
        return {}
    try:
        data = json.loads(FILTERS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out: dict[str, list[str]] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, list):
                out[k] = [str(x).lower() for x in v if str(x).strip()]
        return out
    except Exception:
        return {}


def save_filters(filters: dict[str, list[str]]) -> None:
    FILTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FILTERS_PATH.write_text(json.dumps(filters, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_default_filters() -> None:
    filters = load_filters()
    if "авто" not in filters:
        filters["авто"] = DEFAULT_AUTO_KEYWORDS
        save_filters(filters)


def filter_choices() -> list[str]:
    return ["(без фильтра)"] + sorted(load_filters().keys())


def get_filter_keywords_preview(filter_name: str) -> str:
    if not filter_name or filter_name == "(без фильтра)":
        return ""
    return ", ".join(load_filters().get(filter_name, []))


def save_filter_ui(filter_name: str, keywords_raw: str):
    name = (filter_name or "").strip()
    if not name:
        return "Введите имя фильтра.", gr.update(), gr.update()
    kws = parse_keywords(keywords_raw)
    filters = load_filters()
    filters[name] = kws
    save_filters(filters)
    return (
        f"Фильтр '{name}' сохранён. Ключевых слов: {len(kws)}",
        gr.update(choices=filter_choices(), value=name),
        gr.update(value=", ".join(kws)),
    )


def list_profiles() -> list[str]:
    profiles = []
    for p in PROFILES_DIR.glob("*.json"):
        if p.name.endswith("_template.json"):
            continue
        profiles.append(str(p.relative_to(REPO_ROOT)))
    return sorted(profiles)


def list_profiles_v2() -> list[str]:
    PROFILES_V2_DIR.mkdir(parents=True, exist_ok=True)
    profiles_v2 = []
    for p in PROFILES_V2_DIR.glob("*.json"):
        profiles_v2.append(str(p.relative_to(REPO_ROOT)))
    # Backward compatibility for previously saved v2 profiles in profiles/
    for p in PROFILES_DIR.glob("v2_*.json"):
        profiles_v2.append(str(p.relative_to(REPO_ROOT)))
    return sorted(set(profiles_v2))


def load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        return []
    return []


def save_json_list(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def load_v2_loop_state() -> dict:
    if not V2_LOOP_STATE_PATH.exists():
        return {"urls": {}}
    try:
        data = json.loads(V2_LOOP_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("urls", {})
            return data
    except Exception:
        pass
    return {"urls": {}}


def save_v2_loop_state(state: dict) -> None:
    V2_LOOP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    V2_LOOP_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def slugify_name(value: str) -> str:
    s = value.strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^0-9a-zA-Zа-яА-Я_-]", "", s)
    s = s.strip("_")
    return s or "item"


def group_choices_v2() -> list[str]:
    return [str(g.get("name", "")).strip() for g in load_json_list(V2_GROUPS_PATH) if str(g.get("name", "")).strip()]


def template_choices_v2() -> list[str]:
    return [str(t.get("name", "")).strip() for t in load_json_list(V2_TEMPLATES_PATH) if str(t.get("name", "")).strip()]


def add_group_v2(group_name: str, group_url: str):
    name = (group_name or "").strip()
    url = (group_url or "").strip()
    if not name:
        return "Введите имя группы.", gr.update(), gr.update()
    if not url.startswith("http"):
        return "URL группы должен начинаться с http/https.", gr.update(), gr.update()

    groups = load_json_list(V2_GROUPS_PATH)
    for g in groups:
        if str(g.get("name", "")).strip().lower() == name.lower():
            g["url"] = url
            save_json_list(V2_GROUPS_PATH, groups)
            return f"Группа обновлена: {name}", gr.update(choices=group_choices_v2(), value=name), gr.update()

    groups.append({"name": name, "url": url})
    save_json_list(V2_GROUPS_PATH, groups)
    return f"Группа добавлена: {name}", gr.update(choices=group_choices_v2(), value=name), gr.update(value="")


def add_template_v2(template_name: str, image_path: str, post_text: str):
    name = (template_name or "").strip()
    img = (image_path or "").strip()
    if not name:
        return "Введите имя шаблона.", gr.update(), gr.update()
    if not img:
        return "Введите путь к изображению в шаблоне.", gr.update(), gr.update()

    templates = load_json_list(V2_TEMPLATES_PATH)
    payload = {"name": name, "image_path": img, "post_text": post_text or ""}
    replaced = False
    for i, t in enumerate(templates):
        if str(t.get("name", "")).strip().lower() == name.lower():
            templates[i] = payload
            replaced = True
            break
    if not replaced:
        templates.append(payload)
    save_json_list(V2_TEMPLATES_PATH, templates)
    msg = f"Шаблон {'обновлён' if replaced else 'добавлен'}: {name}"
    return msg, gr.update(choices=template_choices_v2(), value=name), gr.update(value="")


def create_profile_from_v2(group_name: str, template_name: str):
    g_name = (group_name or "").strip()
    t_name = (template_name or "").strip()
    if not g_name or not t_name:
        return "Выберите группу и шаблон.", gr.update(), gr.update()

    groups = load_json_list(V2_GROUPS_PATH)
    templates = load_json_list(V2_TEMPLATES_PATH)
    group = next((g for g in groups if str(g.get("name", "")).strip() == g_name), None)
    template = next((t for t in templates if str(t.get("name", "")).strip() == t_name), None)
    if not group:
        return f"Группа не найдена: {g_name}", gr.update(), gr.update()
    if not template:
        return f"Шаблон не найден: {t_name}", gr.update(), gr.update()

    profile_stem = f"v2_{slugify_name(g_name)}__{slugify_name(t_name)}"
    profile_path = PROFILES_DIR / f"{profile_stem}.json"
    profile_data = {
        "profile_id": profile_stem,
        "name": f"{g_name} | {t_name}",
        "url": str(group.get("url", "")).strip(),
        "image_path": str(template.get("image_path", "")).strip(),
        "post_text": str(template.get("post_text", "")),
        "selectors": DEFAULT_PROFILE_SELECTORS,
        "timeouts": DEFAULT_PROFILE_TIMEOUTS,
    }
    profile_path.write_text(json.dumps(profile_data, ensure_ascii=False, indent=2), encoding="utf-8")
    profiles_v2 = list_profiles_v2()
    rel = str(profile_path.relative_to(REPO_ROOT))
    return (
        f"Профиль создан/обновлён: {rel}",
        gr.update(choices=profiles_v2, value=rel),
        gr.update(choices=profiles_v2),
    )


def save_simple_profile_v2(profile_name: str, group_url: str, image_path: str, post_text: str) -> str:
    name = (profile_name or "").strip()
    raw_urls = group_url or ""
    url_items = []
    for line in raw_urls.replace(",", "\n").splitlines():
        u = line.strip()
        if u:
            url_items.append(u)
    urls = []
    seen = set()
    for u in url_items:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    img = (image_path or "").strip()
    text = post_text or ""
    if not name:
        return "Введите имя профиля."
    if not urls:
        return "Введите хотя бы один URL группы."
    if any(not u.startswith("http") for u in urls):
        return "Каждый URL должен начинаться с http/https."
    if not img:
        return "Введите путь к изображению."

    stem = f"v2_{slugify_name(name)}"
    PROFILES_V2_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = PROFILES_V2_DIR / f"{stem}.json"
    profile_data = {
        "profile_id": stem,
        "name": name,
        "url": urls[0],
        "urls": urls,
        "image_path": img,
        "post_text": text,
        "selectors": DEFAULT_PROFILE_SELECTORS,
        "timeouts": DEFAULT_PROFILE_TIMEOUTS,
    }
    profile_path.write_text(json.dumps(profile_data, ensure_ascii=False, indent=2), encoding="utf-8")
    rel = str(profile_path.relative_to(REPO_ROOT))
    return f"Профиль сохранён: {rel}"


def profile_details_v2(profile_rel_path: str) -> tuple[str, str, str, str]:
    if not profile_rel_path:
        return "", "", "", ""
    profile_path = REPO_ROOT / profile_rel_path
    try:
        profile = load_profile(profile_path)
        urls = profile.get("urls")
        if isinstance(urls, list) and urls:
            url_text = "\n".join(str(x) for x in urls if str(x).strip())
        else:
            url_text = str(profile.get("url", ""))
        return (
            str(profile.get("name", "")),
            url_text,
            str(profile.get("image_path", "")),
            str(profile.get("post_text", "")),
        )
    except Exception:
        return "Ошибка чтения профиля", "", "", ""


def save_and_execute_v2(
    profile_name: str,
    group_url: str,
    image_path: str,
    post_text: str,
    headless: bool,
    submit_post: bool,
    use_existing_browser: bool,
    cdp_url: str,
    auto_join_before_post: bool = False,
    join_cooldown_minutes: float = 5,
    skip_if_unpublished_exists: bool = True,
) -> str:
    save_msg = save_simple_profile_v2(profile_name, group_url, image_path, post_text)
    if save_msg.startswith("Введите") or save_msg.startswith("URL"):
        return save_msg
    profile_rel = f"profiles_v2/v2_{slugify_name(profile_name)}.json"
    profile_path = REPO_ROOT / profile_rel
    try:
        profile = load_profile(profile_path)
    except Exception:
        return "Профиль сохранён, но не удалось прочитать его для запуска."

    urls = profile.get("urls")
    if isinstance(urls, list):
        run_urls = [str(u).strip() for u in urls if str(u).strip()]
    else:
        run_urls = []
    if not run_urls:
        run_urls = [str(profile.get("url", "")).strip()]
    run_urls = [u for u in run_urls if u]
    if not run_urls:
        return "В профиле нет URL для запуска."

    logs = [save_msg, f"Групп в запуске: {len(run_urls)}", ""]
    for idx, u in enumerate(run_urls, start=1):
        profile["url"] = u
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        logs.append(f"[{idx}/{len(run_urls)}] {u}")
        result = execute(
            profile_rel_path=profile_rel,
            image_path=image_path,
            post_text=post_text,
            headless=headless,
            submit_post=submit_post,
            use_existing_browser=use_existing_browser,
            cdp_url=cdp_url,
            auto_join_before_post=auto_join_before_post,
            join_cooldown_seconds=max(0, int(join_cooldown_minutes * 60)),
            skip_if_unpublished_exists=skip_if_unpublished_exists,
        )
        logs.append(result)
        logs.append("")

    return "\n".join(logs)


def execute_v2_loop_stream(
    profile_name: str,
    group_url: str,
    image_path: str,
    post_text: str,
    headless: bool,
    submit_post: bool,
    use_existing_browser: bool,
    cdp_url: str,
    cycle_pause_minutes: float,
    cycles: int,
    auto_join_before_post: bool,
    join_cooldown_minutes: float,
    skip_if_unpublished_exists: bool,
):
    save_msg = save_simple_profile_v2(profile_name, group_url, image_path, post_text)
    if save_msg.startswith("Введите") or save_msg.startswith("URL"):
        yield save_msg
        return

    profile_rel = f"profiles_v2/v2_{slugify_name(profile_name)}.json"
    profile_path = REPO_ROOT / profile_rel
    try:
        profile = load_profile(profile_path)
    except Exception:
        yield "Профиль сохранён, но не удалось прочитать его для запуска."
        return

    urls = profile.get("urls")
    if isinstance(urls, list):
        run_urls = [str(u).strip() for u in urls if str(u).strip()]
    else:
        run_urls = []
    if not run_urls:
        run_urls = [str(profile.get("url", "")).strip()]
    run_urls = [u for u in run_urls if u]
    if not run_urls:
        yield "В профиле нет URL для запуска."
        return

    pause_seconds = max(0, int(cycle_pause_minutes * 60))
    max_cycles = max(0, int(cycles))
    endless = max_cycles == 0

    logs = [
        save_msg,
        f"Групп в запуске: {len(run_urls)}",
        f"Пауза между циклами: {pause_seconds} сек",
        f"Циклы: {'бесконечно' if endless else max_cycles}",
        f"Автовступление: {'включено' if auto_join_before_post else 'выключено'}",
        f"Пауза после вступления: {max(0, int(join_cooldown_minutes * 60))} сек",
        f"Пропуск при наличии 'Предложенные': {'включено' if skip_if_unpublished_exists else 'выключено'}",
        "",
    ]
    yield "\n".join(logs)

    state = load_v2_loop_state()
    urls_state = state.setdefault("urls", {})

    cycle_no = 1
    while endless or cycle_no <= max_cycles:
        logs.append(f"=== Цикл {cycle_no}{'' if endless else f'/{max_cycles}'} ===")
        for idx, u in enumerate(run_urls, start=1):
            profile["url"] = u
            profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
            logs.append(f"[{idx}/{len(run_urls)}] {u}")
            logs.append("Старт обработки группы...")
            yield "\n".join(logs)
            entry = urls_state.setdefault(u, {})
            entry["profile"] = profile.get("name", "")
            entry["last_cycle_started"] = cycle_no
            save_v2_loop_state(state)

            step_logs: list[str] = []
            step_q: queue.Queue[str] = queue.Queue()
            step_done = {"value": False}

            def step_worker() -> None:
                try:
                    current_profile = load_profile(profile_path)
                    validate_profile(current_profile, profile_path, REPO_ROOT)
                    run(
                        profile_path=profile_path,
                        headless=headless,
                        pause_before_submit=False,
                        submit_post=submit_post,
                        image_path_override=image_path.strip() if image_path else None,
                        post_text_override=post_text,
                        log=lambda line: step_q.put(line),
                        use_existing_browser=use_existing_browser,
                        cdp_url=cdp_url.strip() if cdp_url else "http://127.0.0.1:9222",
                        auto_join_before_post=auto_join_before_post,
                        join_cooldown_seconds=max(0, int(join_cooldown_minutes * 60)),
                        skip_if_unpublished_exists=skip_if_unpublished_exists,
                    )
                    step_q.put("Успешно выполнено.")
                except Exception:
                    step_q.put("Ошибка выполнения:")
                    step_q.put(traceback.format_exc())
                finally:
                    step_done["value"] = True

            threading.Thread(target=step_worker, daemon=True).start()

            while not step_done["value"] or not step_q.empty():
                updated = False
                while True:
                    try:
                        line = step_q.get_nowait()
                        step_logs.append(line)
                        updated = True
                    except queue.Empty:
                        break
                if updated:
                    logs.append("\n".join(step_logs))
                    yield "\n".join(logs)
                    # Keep only latest snapshot of this run block in logs.
                    logs.pop()
                time.sleep(0.2)

            result = "\n".join(step_logs)
            logs.append(result)
            if "Успешно выполнено." in step_logs:
                entry["last_status"] = "posted_ok"
                entry["posted_ok_count"] = int(entry.get("posted_ok_count", 0)) + 1
                entry["last_error"] = ""
                logs.append(f"[debug] Группа обработана успешно: {u}")
            else:
                entry["last_status"] = "failed"
                entry["failed_count"] = int(entry.get("failed_count", 0)) + 1
                entry["last_error"] = result[-500:]
                logs.append(f"[debug] Ошибка обработки группы: {u}")
            entry["last_cycle_finished"] = cycle_no
            save_v2_loop_state(state)
            logs.append("")
            yield "\n".join(logs)

        cycle_no += 1
        if endless or cycle_no <= max_cycles:
            if pause_seconds > 0:
                logs.append(f"Ожидание {pause_seconds} сек перед следующим циклом...")
                yield "\n".join(logs)
                time.sleep(pause_seconds)
            else:
                time.sleep(0.1)

    logs.append("=== Циклический запуск v2 завершён ===")
    yield "\n".join(logs)


def execute_v2_loop_stream_ui(
    profile_name: str,
    group_url: str,
    image_path: str,
    post_text: str,
    headless: bool,
    use_existing_browser: bool,
    auto_join_before_post: bool,
    join_cooldown_minutes: float,
    cdp_url: str,
    cycle_pause_minutes: float,
    cycles: int,
    skip_if_unpublished_exists: bool,
):
    yield from execute_v2_loop_stream(
        profile_name=profile_name,
        group_url=group_url,
        image_path=image_path,
        post_text=post_text,
        headless=headless,
        submit_post=True,
        use_existing_browser=use_existing_browser,
        cdp_url=cdp_url,
        cycle_pause_minutes=cycle_pause_minutes,
        cycles=cycles,
        auto_join_before_post=auto_join_before_post,
        join_cooldown_minutes=join_cooldown_minutes,
        skip_if_unpublished_exists=skip_if_unpublished_exists,
    )


def profile_details(profile_rel_path: str) -> tuple[str, str, str]:
    profile_path = REPO_ROOT / profile_rel_path
    try:
        profile = load_profile(profile_path)
        validate_profile(profile, profile_path, REPO_ROOT)
        return profile.get("name", ""), profile.get("image_path", ""), profile.get("post_text", "")
    except Exception as exc:  # noqa: BLE001
        return f"Ошибка профиля: {exc}", "", ""


def execute(
    profile_rel_path: str,
    image_path: str,
    post_text: str,
    headless: bool,
    submit_post: bool,
    use_existing_browser: bool,
    cdp_url: str,
    auto_join_before_post: bool = False,
    join_cooldown_seconds: int = 300,
    skip_if_unpublished_exists: bool = False,
) -> str:
    logs: list[str] = []

    def add_log(line: str) -> None:
        logs.append(line)

    try:
        if not profile_rel_path:
            return "Выберите профиль."
        profile_path = REPO_ROOT / profile_rel_path
        profile = load_profile(profile_path)
        validate_profile(profile, profile_path, REPO_ROOT)

        run(
            profile_path=profile_path,
            headless=headless,
            pause_before_submit=False,
            submit_post=submit_post,
            image_path_override=image_path.strip() if image_path else None,
            post_text_override=post_text,
            log=add_log,
            use_existing_browser=use_existing_browser,
            cdp_url=cdp_url.strip() if cdp_url else "http://127.0.0.1:9222",
            auto_join_before_post=auto_join_before_post,
            join_cooldown_seconds=max(0, int(join_cooldown_seconds)),
            skip_if_unpublished_exists=skip_if_unpublished_exists,
        )
        logs.append("Успешно выполнено.")
        return "\n".join(logs)
    except Exception:
        logs.append("Ошибка выполнения:")
        logs.append(traceback.format_exc())
        return "\n".join(logs)


def execute_all_profiles(
    pause_minutes: float,
    headless: bool,
    submit_post: bool,
    use_existing_browser: bool,
    cdp_url: str,
) -> str:
    logs: list[str] = []
    profiles = list_profiles()
    if not profiles:
        return "Профили не найдены."

    pause_seconds = max(0, int(pause_minutes * 60))
    ok_count = 0
    fail_count = 0

    logs.append(f"Найдено профилей: {len(profiles)}")
    logs.append(f"Пауза между группами: {pause_seconds} сек")
    logs.append("")

    for idx, profile_rel_path in enumerate(profiles, start=1):
        logs.append(f"=== [{idx}/{len(profiles)}] {profile_rel_path} ===")
        try:
            profile_path = REPO_ROOT / profile_rel_path
            profile = load_profile(profile_path)
            validate_profile(profile, profile_path, REPO_ROOT)

            run(
                profile_path=profile_path,
                headless=headless,
                pause_before_submit=False,
                submit_post=submit_post,
                image_path_override=None,
                post_text_override=None,
                log=lambda line: logs.append(line),
                use_existing_browser=use_existing_browser,
                cdp_url=cdp_url.strip() if cdp_url else "http://127.0.0.1:9222",
            )
            ok_count += 1
            logs.append("Статус: УСПЕШНО")
        except Exception:
            fail_count += 1
            logs.append("Статус: ОШИБКА")
            logs.append(traceback.format_exc())

        if idx < len(profiles) and pause_seconds > 0:
            logs.append(f"Ожидание {pause_seconds} сек перед следующим профилем...")
            time.sleep(pause_seconds)
        logs.append("")

    logs.append("=== ИТОГ ===")
    logs.append(f"Успешно: {ok_count}")
    logs.append(f"С ошибкой: {fail_count}")
    return "\n".join(logs)


def execute_monitor_to_telegram(
    interval_minutes: float,
    per_group_wait_minutes: float,
    cycles: int,
    max_posts_per_group: int,
    telegram_token: str,
    telegram_chat_id: str,
    filter_name: str,
    dry_run: bool,
    cdp_url: str,
) -> str:
    logs: list[str] = []
    profiles = []
    for profile_rel_path in list_profiles():
        profile_path = REPO_ROOT / profile_rel_path
        profile = load_profile(profile_path)
        profiles.append(
            {
                "profile_id": profile.get("profile_id", profile_path.stem),
                "name": profile.get("name", profile_path.stem),
                "url": profile.get("url", ""),
            }
        )

    if not profiles:
        yield "Профили не найдены."
        return
    if not dry_run and not telegram_token.strip():
        yield "Для боевого режима укажите TELEGRAM_BOT_TOKEN."
        return

    interval_seconds = max(0, int(interval_minutes * 60))
    per_group_wait_seconds = max(0, int(per_group_wait_minutes * 60))
    safe_cycles = max(1, int(cycles))
    safe_max_posts = max(1, int(max_posts_per_group))
    active_keywords: list[str] = []
    if filter_name and filter_name != "(без фильтра)":
        active_keywords = load_filters().get(filter_name, [])
    logs.append(f"Profiles: {len(profiles)}")
    logs.append(f"Dry-run: {dry_run}")
    logs.append(f"Interval: {interval_seconds} sec")
    logs.append(f"Per-group wait: {per_group_wait_seconds} sec")
    logs.append(f"Cycles: {safe_cycles}")
    logs.append(f"Max posts/group: {safe_max_posts}")
    logs.append(f"Filter: {filter_name if filter_name else '(без фильтра)'}")
    logs.append(f"Filter keywords: {len(active_keywords)}")
    logs.append("")
    yield "\n".join(logs)

    q: queue.Queue[str] = queue.Queue()
    done = {"value": False}

    def worker() -> None:
        try:
            monitor_loop(
                profiles=profiles,
                cdp_url=cdp_url.strip() if cdp_url else "http://127.0.0.1:9222",
                telegram_token=telegram_token.strip(),
                telegram_chat_id=telegram_chat_id.strip(),
                dry_run=dry_run,
                interval_seconds=interval_seconds,
                per_group_wait_seconds=per_group_wait_seconds,
                cycles=safe_cycles,
                max_posts_per_group=safe_max_posts,
                filter_keywords=active_keywords,
                log=lambda line: q.put(line),
            )
        except Exception:
            q.put("Ошибка мониторинга:")
            q.put(traceback.format_exc())
        finally:
            done["value"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while not done["value"] or not q.empty():
        updated = False
        while True:
            try:
                line = q.get_nowait()
                logs.append(line)
                updated = True
            except queue.Empty:
                break
        if updated:
            yield "\n".join(logs)
        time.sleep(0.2)

    yield "\n".join(logs)


def execute_monitor_once_stream(
    per_group_wait_minutes: float,
    max_posts_per_group: int,
    telegram_token: str,
    telegram_chat_id: str,
    filter_name: str,
    cdp_url: str,
):
    yield from execute_monitor_to_telegram(
        interval_minutes=0,
        per_group_wait_minutes=per_group_wait_minutes,
        cycles=1,
        max_posts_per_group=max_posts_per_group,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        filter_name=filter_name,
        dry_run=False,
        cdp_url=cdp_url,
    )


def execute_monitor_loop_stream(
    interval_minutes: float,
    per_group_wait_minutes: float,
    cycles: int,
    max_posts_per_group: int,
    telegram_token: str,
    telegram_chat_id: str,
    filter_name: str,
    cdp_url: str,
):
    yield from execute_monitor_to_telegram(
        interval_minutes=interval_minutes,
        per_group_wait_minutes=per_group_wait_minutes,
        cycles=cycles,
        max_posts_per_group=max_posts_per_group,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        filter_name=filter_name,
        dry_run=False,
        cdp_url=cdp_url,
    )


def clear_vk_feed_state() -> str:
    try:
        if VK_FEED_STATE_PATH.exists():
            VK_FEED_STATE_PATH.unlink()
            return f"Состояние очищено: {VK_FEED_STATE_PATH}"
        return f"Файл состояния не найден: {VK_FEED_STATE_PATH}"
    except Exception:
        return "Ошибка очистки состояния:\n" + traceback.format_exc()


def clear_v2_loop_state() -> str:
    try:
        if V2_LOOP_STATE_PATH.exists():
            V2_LOOP_STATE_PATH.unlink()
            return f"Состояние v2-цикла очищено: {V2_LOOP_STATE_PATH}"
        return f"Файл состояния v2-цикла не найден: {V2_LOOP_STATE_PATH}"
    except Exception:
        return "Ошибка очистки состояния v2-цикла:\n" + traceback.format_exc()


profiles = list_profiles()
profiles_v2 = list_profiles_v2()
default_profile = profiles[0] if profiles else None
default_name, default_image, default_text = ("", "", "")
if default_profile:
    default_name, default_image, default_text = profile_details(default_profile)
default_name_v2, default_url_v2, default_image_v2, default_text_v2 = ("", "", "", "")

load_local_env_file()
ensure_default_filters()
default_tg_token = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8599498074:AAH5jvqV3ZZYux3J-j0t16jFeJmbGRIhn7s",
)
default_tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "954773719")


with gr.Blocks(title="VK Auto Post") as demo:
    gr.Markdown("# VK Auto Post\nУдобный запуск сценариев автопостинга для профилей VK")

    with gr.Tabs():
        with gr.Tab("Постинг"):
            with gr.Row():
                profile_dd = gr.Dropdown(choices=profiles, label="Профиль", value=default_profile)
                headless_cb = gr.Checkbox(label="Headless режим", value=False)
                existing_browser_cb = gr.Checkbox(label="Использовать уже открытый браузер (CDP)", value=True)

            cdp_url_tb = gr.Textbox(label="CDP URL", value="http://127.0.0.1:9222")

            profile_name = gr.Textbox(label="Название профиля", interactive=False, value=default_name)
            image_path_tb = gr.Textbox(label="Путь к изображению", value=default_image)
            post_text_tb = gr.Textbox(label="Текст поста", lines=8, value=default_text)

            with gr.Row():
                test_btn = gr.Button("Тест (без отправки)", variant="secondary")
                run_btn = gr.Button("Полный запуск (с отправкой)", variant="primary")

            gr.Markdown("### Пакетный прогон по всем профилям")
            with gr.Row():
                pause_minutes_nb = gr.Number(label="Пауза между группами (мин)", value=5, minimum=0, precision=1)
                run_all_test_btn = gr.Button("Все профили: тест (без отправки)", variant="secondary")
                run_all_submit_btn = gr.Button("Все профили: полный запуск", variant="primary")

        with gr.Tab("Мониторинг"):
            gr.Markdown("Мониторинг новых постов в группах и отправка в Telegram")
            cdp_url_monitor_tb = gr.Textbox(label="CDP URL", value="http://127.0.0.1:9222")
            tg_token_tb = gr.Textbox(label="TELEGRAM_BOT_TOKEN", value=default_tg_token, type="password")
            tg_chat_id_tb = gr.Textbox(label="TELEGRAM_CHAT_ID", value=default_tg_chat_id)
            monitor_filter_dd = gr.Dropdown(
                choices=filter_choices(),
                value="авто" if "авто" in filter_choices() else "(без фильтра)",
                label="Активный фильтр",
            )
            filter_keywords_preview_tb = gr.Textbox(
                label="Ключевые слова активного фильтра",
                interactive=False,
                value=get_filter_keywords_preview("авто"),
            )
            with gr.Row():
                new_filter_name_tb = gr.Textbox(label="Имя фильтра", value="авто")
                new_filter_keywords_tb = gr.Textbox(
                    label="Ключевые слова (через запятую или с новой строки)",
                    value=", ".join(DEFAULT_AUTO_KEYWORDS),
                    lines=3,
                )
            save_filter_btn = gr.Button("Сохранить фильтр", variant="secondary")
            with gr.Row():
                monitor_interval_nb = gr.Number(label="Интервал мониторинга (мин)", value=5, minimum=0, precision=1)
                monitor_per_group_wait_nb = gr.Number(
                    label="Ожидание на каждой группе (мин)",
                    value=5,
                    minimum=0,
                    precision=1,
                )
                monitor_cycles_nb = gr.Number(label="Количество циклов", value=1, minimum=1, precision=0)
                monitor_max_posts_nb = gr.Number(label="Сколько постов смотреть в группе", value=10, minimum=1, precision=0)
            with gr.Row():
                monitor_once_btn = gr.Button("Мониторинг: 1 проход", variant="secondary")
                monitor_loop_btn = gr.Button("Мониторинг: запуск по циклам", variant="primary")
            clear_state_btn = gr.Button("Очистить состояние мониторинга (vk_feed_state.json)", variant="stop")

        with gr.Tab("Автопостинг v2"):
            gr.Markdown("Минимальный автопостинг: URL группы + текст + изображение, сохранение в профиль v2")

            profile_v2_pick_dd = gr.Dropdown(choices=profiles_v2, label="Сохранённые профили v2")
            load_profile_v2_btn = gr.Button("Загрузить профиль v2", variant="secondary")
            profile_name_v2_edit = gr.Textbox(label="Имя профиля")
            group_url_v2_tb = gr.Textbox(
                label="URL группы (по одному на строку или через запятую)",
                lines=4,
                value=default_url_v2,
            )
            image_path_v2_tb = gr.Textbox(label="Путь к изображению", value=default_image_v2)
            post_text_v2_tb = gr.Textbox(label="Текст шаблона", lines=8, value=default_text_v2)
            save_profile_v2_btn = gr.Button("Сохранить профиль v2", variant="secondary")

            with gr.Row():
                headless_v2_cb = gr.Checkbox(label="Headless режим", value=False)
                existing_browser_v2_cb = gr.Checkbox(label="Использовать уже открытый браузер (CDP)", value=True)
                auto_join_v2_cb = gr.Checkbox(
                    label="Автовступление в сообщество (если без подписки нельзя предложить пост)",
                    value=True,
                )
                skip_unpublished_v2_cb = gr.Checkbox(
                    label="Пропускать группу, если уже есть блок 'Предложенные'",
                    value=True,
                )
                cdp_url_v2_tb = gr.Textbox(label="CDP URL", value="http://127.0.0.1:9222")
            with gr.Row():
                join_cooldown_v2_nb = gr.Number(
                    label="Пауза после вступления (мин)",
                    value=5,
                    minimum=0,
                    precision=1,
                )
            with gr.Row():
                test_v2_btn = gr.Button("Тест (без отправки)", variant="secondary")
                run_v2_btn = gr.Button("Полный запуск (с отправкой)", variant="primary")
            with gr.Row():
                v2_cycle_pause_nb = gr.Number(label="Пауза между циклами (мин)", value=3, minimum=0, precision=1)
                v2_cycles_nb = gr.Number(label="Количество циклов (0 = бесконечно)", value=1, minimum=0, precision=0)
                run_v2_loop_btn = gr.Button("Циклический запуск v2", variant="primary")
            clear_v2_state_btn = gr.Button("Очистить состояние v2-цикла (v2_loop_state.json)", variant="stop")

    logs_tb = gr.Textbox(label="Логи выполнения", lines=16)

    profile_dd.change(
        fn=profile_details,
        inputs=[profile_dd],
        outputs=[profile_name, image_path_tb, post_text_tb],
    )

    test_btn.click(
        fn=lambda p, i, t, h, eb, cdp: execute(p, i, t, h, False, eb, cdp),
        inputs=[profile_dd, image_path_tb, post_text_tb, headless_cb, existing_browser_cb, cdp_url_tb],
        outputs=[logs_tb],
    )

    run_btn.click(
        fn=lambda p, i, t, h, eb, cdp: execute(p, i, t, h, True, eb, cdp),
        inputs=[profile_dd, image_path_tb, post_text_tb, headless_cb, existing_browser_cb, cdp_url_tb],
        outputs=[logs_tb],
    )

    run_all_test_btn.click(
        fn=lambda m, h, eb, cdp: execute_all_profiles(m, h, False, eb, cdp),
        inputs=[pause_minutes_nb, headless_cb, existing_browser_cb, cdp_url_tb],
        outputs=[logs_tb],
    )

    run_all_submit_btn.click(
        fn=lambda m, h, eb, cdp: execute_all_profiles(m, h, True, eb, cdp),
        inputs=[pause_minutes_nb, headless_cb, existing_browser_cb, cdp_url_tb],
        outputs=[logs_tb],
    )

    monitor_once_btn.click(
        fn=execute_monitor_once_stream,
        inputs=[
            monitor_per_group_wait_nb,
            monitor_max_posts_nb,
            tg_token_tb,
            tg_chat_id_tb,
            monitor_filter_dd,
            cdp_url_monitor_tb,
        ],
        outputs=[logs_tb],
    )

    monitor_loop_btn.click(
        fn=execute_monitor_loop_stream,
        inputs=[
            monitor_interval_nb,
            monitor_per_group_wait_nb,
            monitor_cycles_nb,
            monitor_max_posts_nb,
            tg_token_tb,
            tg_chat_id_tb,
            monitor_filter_dd,
            cdp_url_monitor_tb,
        ],
        outputs=[logs_tb],
    )

    monitor_filter_dd.change(
        fn=get_filter_keywords_preview,
        inputs=[monitor_filter_dd],
        outputs=[filter_keywords_preview_tb],
    )

    save_filter_btn.click(
        fn=save_filter_ui,
        inputs=[new_filter_name_tb, new_filter_keywords_tb],
        outputs=[logs_tb, monitor_filter_dd, filter_keywords_preview_tb],
    )

    clear_state_btn.click(
        fn=clear_vk_feed_state,
        inputs=[],
        outputs=[logs_tb],
    )

    save_profile_v2_btn.click(
        fn=save_simple_profile_v2,
        inputs=[profile_name_v2_edit, group_url_v2_tb, image_path_v2_tb, post_text_v2_tb],
        outputs=[logs_tb],
    )

    load_profile_v2_btn.click(
        fn=profile_details_v2,
        inputs=[profile_v2_pick_dd],
        outputs=[profile_name_v2_edit, group_url_v2_tb, image_path_v2_tb, post_text_v2_tb],
    )

    test_v2_btn.click(
        fn=lambda n, u, i, t, h, eb, aj, su, jm, cdp: save_and_execute_v2(n, u, i, t, h, False, eb, cdp, aj, jm, su),
        inputs=[profile_name_v2_edit, group_url_v2_tb, image_path_v2_tb, post_text_v2_tb, headless_v2_cb, existing_browser_v2_cb, auto_join_v2_cb, skip_unpublished_v2_cb, join_cooldown_v2_nb, cdp_url_v2_tb],
        outputs=[logs_tb],
    )

    run_v2_btn.click(
        fn=lambda n, u, i, t, h, eb, aj, su, jm, cdp: save_and_execute_v2(n, u, i, t, h, True, eb, cdp, aj, jm, su),
        inputs=[profile_name_v2_edit, group_url_v2_tb, image_path_v2_tb, post_text_v2_tb, headless_v2_cb, existing_browser_v2_cb, auto_join_v2_cb, skip_unpublished_v2_cb, join_cooldown_v2_nb, cdp_url_v2_tb],
        outputs=[logs_tb],
    )

    run_v2_loop_btn.click(
        fn=execute_v2_loop_stream_ui,
        inputs=[
            profile_name_v2_edit,
            group_url_v2_tb,
            image_path_v2_tb,
            post_text_v2_tb,
            headless_v2_cb,
            existing_browser_v2_cb,
            auto_join_v2_cb,
            join_cooldown_v2_nb,
            cdp_url_v2_tb,
            v2_cycle_pause_nb,
            v2_cycles_nb,
            skip_unpublished_v2_cb,
        ],
        outputs=[logs_tb],
    )

    clear_v2_state_btn.click(
        fn=clear_v2_loop_state,
        inputs=[],
        outputs=[logs_tb],
    )

if __name__ == "__main__":
    demo.queue().launch(server_name="127.0.0.1", server_port=7861)
