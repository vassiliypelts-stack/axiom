# AXIOM: Enrichment Pipeline & Re-org

**Дата:** 2026-07-21
**Статус:** Implemented

## 1. Новая навигация

Боковое меню перестроено в логические группы:

```
Виджеты
Структура
Календарь
Проекты

Ресурсы (collapsible)
  Чаты (каталог)
  Досье                  ← новый пункт (viewDossier)
  Прокси
  Парсинг
  Аккаунты

CRM / Лидген (collapsible)
  Кампании
  Диалоги
  Лиды
  Находки                ← новый пункт (viewLeadHits, раньше не было в меню)
  Сделки
  Контакты
  Компании
```

- `hits` (генерация лидов из слушателя) добавлен в меню (был только в VIEWS)
- `dossier` добавлен в меню (был только в VIEWS)
- Cross-group drag-and-drop: пункты можно перетаскивать между разделами
- `Дашборд` → `Виджеты`

## 2. Импорт контактов

**Раздел Контакты** — новая кнопка «⬆ Импорт», раскрывает панель с двумя способами:

1. **CSV-файл** — загрузка через FormData, парсит столбцы: Телефон, Имя, Название, Город
2. **Вставка номеров текстом** — `POST /api/import/phones`, создаёт контакты с source="phones"

## 3. Pipeline обогащения

**Контакты** — три последовательные кнопки:

| Кнопка | Эндпоинт | Что делает |
|---|---|---|
| 📡 Пробив TG | `POST /api/enrich/resolve-tg` | Запускает `phone_resolve` в фоне: ImportContacts → tg_user_id, username, аватар, bio |
| 🧠 Обогатить пачку | `POST /api/enrich` | DaData (ИНН/директор/ОГРН) + AI-досье (специализация, зацепка, summary) |
| 🗂 Сегментировать | `POST /api/leads/segment` | Раскладывает по сферам (правила + дешёвая модель) |

## 4. Универсальный CSV-импорт компаний

Новый парсер `_parse_universal()` в `app.py` — маппинг русских заголовков столбцов на поля БД:

| Заголовок CSV | Поле companies | Поле contacts |
|---|---|---|
| Наименование | name | — |
| ИНН | inn | — |
| КПП | kpp | — |
| ОГРН | ogrn | — |
| ФИО руководителя | director_name | person_name |
| Телефон директора | director_phone | phone |
| Email директора | director_email | email |
| Должность руководителя | director_role | person_role |
| Номер телефона | phone | — |
| Адрес | address | — |
| Ссылка на сайт | site | — |
| Выручка, тыс. руб | revenue | — |
| Чистая прибыль/убыток | profit | — |
| Баланс | balance | — |
| Арбитраж | arbitration | — |
| Количество сотрудников | employee_count | — |
| Полученные лицензии | licenses | — |
| Основной вид деятельности | main_activity | — |
| Другие виды деятельности | other_activities | — |
| Предметы закупок (ОКПД2) | procurement_codes | — |
| Регион регистрации | region | — |
| Категория МСП | sme_category | — |
| Лизингополучатель | lessee | — |
| Город | city | — |

Импорт создаёт компанию + привязывает контакт (директор/телефон).

## 5. Источник с автокомплитом

- `GET /api/import/sources` — возвращает все уникальные source из contacts + companies
- Поле `<input>` + `<datalist id="src-list">` — автокомплит в панелях импорта
- Можно вписать новый источник — он сам появится в datalist в следующий раз

## 6. Расширение companies

Новые поля (добавляются миграцией через `_EXTRA_COMPANY_COLS`):

`director_name, director_phone, director_email, director_role, kpp, registration_date, employee_count, revenue, profit, balance, arbitration, licenses, main_activity, other_activities, procurement_codes, region, sme_category, lessee`

Карточка компании показывает фин. показатели (выручка/прибыль/баланс/арбитраж/сотрудники).

## 7. Затронутые файлы

- `axiom/web/app.py` — новые эндпоинты + `_parse_universal` + `_EXTRA_COMPANY_COLS`
- `axiom/web/index.html` — навигация, import panels, contacts pipeline, карточка компании
- `axiom/db/database.py` — `_EXTRA_COMPANY_COLS`, расширен `upsert_contact`
