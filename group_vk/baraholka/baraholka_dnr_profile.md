# Профиль сообщества VK: Барахолка ДНР

## Метаданные
- ID профиля: `baraholka_dnr`
- Название: `Барахолка ДНР`
- URL: `https://vk.com/barakholka_dnr`
- Дата фиксации HTML: `30.04.2026`
- Тип сценария: `suggest_post_with_image_and_text`

## Путь к медиа
- Изображение для загрузки: `C:\Users\admin\Desktop\myproject\vk_post\group_vk\baraholka\img_post.jpg`

## Текст поста
```text
— сварка изделий из металла под заказ
— столы, стулья, тумбы из металла
— изготовление по вашим размерам
— покраска готовых изделий
— готовые шаблоны и индивидуальные решения
ассортимент можно посмотреть в @svarka_dnr_bot
```

## Селекторы (предпочтительно использовать `data-testid`)
- Кнопка открытия формы публикации: `[data-testid="group_publish_block"] button`
- Контейнер модального окна: `[data-testid="posting_modal_box"]`
- Поле ввода текста: `[data-testid="posting_base_screen_input_message"]`
- Input загрузки файла: `input[data-testid="posting_base_screen_download_from_device"]`
- Кнопка финальной отправки: `button[data-testid="posting_suggest_button"]`

## Сценарий шагов
1. Открыть страницу сообщества `https://vk.com/barakholka_dnr`.
2. Дождаться блока публикации и кликнуть по кнопке `Предложить пост`.
3. Дождаться появления модального окна `[data-testid="posting_modal_box"]`.
4. Найти input загрузки `posting_base_screen_download_from_device` и загрузить файл `img_post.jpg`.
5. Ввести текст в поле `posting_base_screen_input_message`.
6. Проверить, что кнопка `posting_suggest_button` активна.
7. Кликнуть `Предложить пост`.

## Критерии успеха
- Модальное окно публикации открылось.
- Файл изображения принят формой (нет ошибки загрузки).
- Текст вставлен в поле сообщения.
- Нажатие `posting_suggest_button` выполняется без ошибки UI.

## Критерии ошибки
- Не найден `group_publish_block` или кнопка `Предложить пост`.
- Не открылось модальное окно `posting_modal_box` за таймаут.
- Не найден input `posting_base_screen_download_from_device`.
- Файл по пути не существует или не загрузился.
- Не найдено поле `posting_base_screen_input_message`.
- Не найдена или неактивна кнопка `posting_suggest_button`.

## Примечания
- Для устойчивости не использовать длинные CSS-классы вида `vkit...`, так как они часто меняются.
- Приоритет: `data-testid`, `aria-label`, текст кнопок.
- Исходный подробный HTML сохранён в `barakholka_dnr.md`.
