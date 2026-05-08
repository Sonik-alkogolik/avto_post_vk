from pathlib import Path
import json
import traceback
import time
import os
import queue
import threading

import gradio as gr

from run_vk_post import load_profile, run, validate_profile
from vk_feed_to_telegram import monitor_loop


REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = REPO_ROOT / "profiles"
VK_FEED_STATE_PATH = REPO_ROOT / "reports" / "vk_feed_state.json"
ENV_PATH = REPO_ROOT / ".env"
FILTERS_PATH = REPO_ROOT / "reports" / "monitor_filters.json"

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


profiles = list_profiles()
default_profile = profiles[0] if profiles else None
default_name, default_image, default_text = ("", "", "")
if default_profile:
    default_name, default_image, default_text = profile_details(default_profile)

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

if __name__ == "__main__":
    demo.queue().launch(server_name="127.0.0.1", server_port=7861)
