# avto_post_vk

Базовый автопостинг (предложка) VK под 1 сообщество с профилями.

## Быстрый старт

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python src/run_vk_post.py --profile profiles/baraholka_dnr.json --no-submit
```

`--no-submit` ставит паузу перед финальной отправкой, чтобы можно было вручную проверить форму.

## Профили

- `profiles/baraholka_dnr.json` — профиль сообщества `barakholka_dnr`
- `profiles/second_group_template.json` — шаблон для второй группы (скопируйте в отдельный JSON и заполните своими значениями)
- По аналогии можно добавлять новые JSON-профили под другие группы

### Безопасно добавить вторую группу

1. Скопируйте `profiles/second_group_template.json` в новый файл, например `profiles/my_group_2.json`.
2. Заполните `url`, `image_path`, `post_text`.
3. Сначала проверьте через UI кнопкой `Тест (без отправки)`.
4. После проверки используйте `Полный запуск (с отправкой)`.

## Важно

- Скрипт ожидает, что вы уже авторизованы в VK в открытой сессии браузера.
- Селекторы построены на `data-testid` для устойчивости.

## Web UI для тестов

```bash
python src/web_ui.py
```

Откройте в браузере `http://127.0.0.1:7861` и используйте:
- `Тест (без отправки)` для безопасной проверки сценария без финального клика.
- `Полный запуск (с отправкой)` для выполнения полного цикла.

### Работа с уже открытым и залогиненным браузером

Чтобы UI подключался к вашему текущему браузеру, запустите Chrome/Edge с CDP:

```bash
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

В UI оставьте включённым чекбокс `Использовать уже открытый браузер (CDP)` и URL `http://127.0.0.1:9222`.

## Runbook: быстрый запуск без потерь времени

Этот раздел фиксирует рабочий порядок запуска и типичные ограничения, которые уже встретились.

### 1) Подготовка окружения (один раз)

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Проверка:

```bash
python -c "import gradio, playwright; print('deps_ok')"
```

### 2) Запуск Web UI

```bash
python src/web_ui.py
```

UI доступен на: `http://127.0.0.1:7861`

Если UI в фоне завершается без лога, запускать в активной консоли (так проще увидеть ошибку сразу).

### 3) Запуск через уже открытый браузер (рекомендуется)

Поднять Chrome с CDP:

```powershell
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\Users\admin\Desktop\myproject\vk_post\profiles\chrome_cdp
```

Проверить порт:

```powershell
netstat -ano | Select-String ":9222"
```

В UI:
- включить `Использовать уже открытый браузер (CDP)`
- `CDP URL`: `http://127.0.0.1:9222`

### 4) Чеклист перед первым постом

- В CDP-браузере выполнен логин в VK.
- Открывается нужная группа (`url` из профиля).
- Видна кнопка `Предложить пост` вручную.
- Картинка из `image_path` существует.

### 5) Быстрый CLI тест без UI

Без отправки:

```bash
python src/run_vk_post.py --profile profiles/baraholka_dnr.json --no-submit --use-existing-browser --cdp-url http://127.0.0.1:9222
```

Полный запуск:

```bash
python src/run_vk_post.py --profile profiles/baraholka_dnr.json --use-existing-browser --cdp-url http://127.0.0.1:9222
```

### 6) Ограничения и частые сбои

- `connect ECONNREFUSED 127.0.0.1:9222`  
  Причина: Chrome не запущен с `--remote-debugging-port=9222`.
- `Cannot click any candidate selector ... Предложить пост`  
  Причины: не залогинен VK, страница не догрузилась (skeleton), изменились селекторы, у аккаунта нет доступа к предложке.
- `PermissionError: [WinError 5] Отказано в доступе` при Playwright  
  Причина: ограничения среды выполнения; запускать команду вне sandbox/с нужными правами.

### 7) Артефакты диагностики

- Ошибки UI: `reports/ui.out.log`, `reports/ui.err.log`, `reports/web_ui.out.log`, `reports/web_ui.err.log`
- Ошибки VK сценария: `reports/vk_error_generic.png`, `reports/vk_error_timeout.png`
