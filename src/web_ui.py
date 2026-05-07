from pathlib import Path
import json
import traceback

import gradio as gr

from run_vk_post import load_profile, run, validate_profile


REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = REPO_ROOT / "profiles"


def list_profiles() -> list[str]:
    return sorted([str(p.relative_to(REPO_ROOT)) for p in PROFILES_DIR.glob("*.json")])


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


profiles = list_profiles()
default_profile = profiles[0] if profiles else None
default_name, default_image, default_text = ("", "", "")
if default_profile:
    default_name, default_image, default_text = profile_details(default_profile)


with gr.Blocks(title="VK Auto Post") as demo:
    gr.Markdown("# VK Auto Post\nУдобный запуск сценариев автопостинга для профилей VK")

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

if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7861)
