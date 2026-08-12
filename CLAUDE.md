# AXIOM — Leadgen Machine (Msgr/SMM)

FastAPI + одностраничный дашборд. Многоканальная система лидогенерации и рассылок (Telegram/WhatsApp/MAX), CRM, парсинг чатов, прогрев аккаунтов.

## ⚠️ КРИТИЧНО: не читай эти файлы целиком

| Файл | Размер | Что делать вместо Read целиком |
|---|---|---|
| `axiom/web/app.py` | ~6100 строк, ~210 роутов | Grep по домену (таблица ниже) → Read с `offset`/`limit` вокруг нужной строки |
| `axiom/web/index.html` | ~6500 строк, один inline `<script>` + один `<style>` на весь файл | Grep по имени функции/id элемента → Read диапазона строк |
| `axiom/vendor/**` | 19 файлов, ~12000 строк (`opentele/devices.py` — 6108) | Сторонняя библиотека, не редактируется. Не читать вообще и исключать из Grep, если не открыт явный баг именно в вендоре |

Полное чтение любого из них — это ~100–170k токенов за один вызов, которые потом висят в контексте до `/clear`. Почти любая задача решается через Grep + точечный Read.

### Карта роутов app.py (Grep по префиксу, затем читай найденную строку ±50)

| Домен | Префиксы роутов |
|---|---|
| Авторизация | `/login`, `/logout`, `/api/auth/` |
| Аккаунты и прокси | `/api/accounts`, `/api/account/{`, `/api/proxy`, `/api/proxies`, `/api/proxy6`, `/api/accounts/twofa`, `/api/accounts/identity` |
| Регистрация номеров (hero-sms, авторег) | `/api/sms/`, `/api/auto/` |
| Настройки | `/api/settings/` |
| Оргструктура | `/api/org/` |
| AI-агенты | `/api/aiagents` |
| CRM (контакты/компании/сделки) | `/api/contacts`, `/api/contact/{`, `/api/companies`, `/api/company/{`, `/api/deals`, `/api/deal/{`, `/api/pipelines`, `/api/pipeline/{` |
| ChatCat (сканирование чатов) | `/api/chatcat` |
| Кампании/рассылки | `/api/campaign`, `/api/campaigns` |
| Импорт/парсинг/обогащение | `/api/import`, `/api/parse`, `/api/enrich`, `/api/dossier` |
| Ниши/лиды/хиты/ключевые слова | `/api/niches`, `/api/niche/{`, `/api/hits`, `/api/hit/{`, `/api/keywords`, `/api/leads`, `/api/target_leads` |
| Прогрев | `/api/warmup` |
| Деплой | `/api/deploy` |
| Встречи/уведомления/календарь | `/api/meetings`, `/api/gcal`, `/api/notifications`, `/api/today`, `/api/event/{` |
| Статистика/логи/health | `/api/stats`, `/api/logs`, `/api/health` |
| Проекты | `/api/projects`, `/api/project/{` |
| Listener/ChatScan | `/api/listener`, `/api/chatscan` |
| Прочее | `/api/tgcheck`, `/api/copilot`, `/api/agent/`, `/api/chats`, `/api/coverage`, `/api/maintenance/`, `/` (отдача index.html) |

Пример: нужно поправить логику кампаний → `Grep pattern="/api/campaign" path="axiom/web/app.py" output_mode="content" -n` → взять номер строки → `Read` с `offset` вокруг неё, а не файл целиком.

### Карта index.html (Grep по имени функции, номер строки — ориентир)

Структура файла: `<style>` — строки 11–625, весь JS — один `<script>` 655–6590.
Экраны рисуются функциями `viewXxx`, диспетчер — `const VIEWS={...}` (~6135), точка входа — `route()` (~6571).

| Раздел | Ключевые функции (грепать по имени) | ~Строки |
|---|---|---|
| Хелперы (`$`, `api`, `esc`), тема, форматтеры | `applyTheme`, `fmt`, `rel`, `statusBadge` | 656–730 |
| Универсальная таблица (сортировка/ресайз/теги) | `smartTable`, `bindTags`, `wireCollapse` | 730–976 |
| Дашборд | `viewDashboard`, `loadTodayTasks`, `openAudience` | 977–1240 |
| **Аккаунты** (осторожно: функция зовётся `viewAgents`, не `viewAccounts`) | `viewAgents`, `showLoginDialog`, `showSmsRegisterDialog`, `loadTwofa`, `loadIdentityCheck`, `showProtectDialog` | 1241–2229 |
| AI-агенты (это другое, чем «Аккаунты») | `viewAiAgents` | 2230–2265 |
| Оргструктура (самый крупный блок, префикс `org*`) | `viewOrgChart`, `renderOrgChart`, `orgBuildTree`, `wireOrgChartTree` | 2266–3058 |
| CRM: лиды и сделки (канбан) | `viewLeads`, `viewDeals`, `renderDealsKanban`, `openPipelineEditor` | 3059–3367 |
| CRM: компании и контакты | `viewCompanies`, `viewContacts`, `openCompany`, `openDeal` | 3368–3647 |
| Карточка аккаунта/диалога | `openAgent` | 3648–3833 |
| ChatCat (каталог чатов, префикс `cc*`) | `viewChatCatalog`, `openChat`, `openChatReport`, `loadQuality`, `showImportChatsDialog` | 3834–4568 |
| Хиты и целевые лиды | `viewLeadHits`, `renderHits`, `renderTargetLeads` | 4569–4771 |
| Досье | `viewDossier`, `dossierCard`, `kpCard` | 4772–4909 |
| Кампании/рассылки | `viewCampaigns`, `renderCampList` | 4910–5607 |
| Переписка | `viewChats`, `openThread`, `openDossier` (drawer) | 5608–5750 |
| Парсер TG | `viewParser` | 5751–5855 |
| Проекты | `viewProjects` | 5856–5981 |
| Прокси | `viewProxy` | 5982–6048 |
| Календарь/встречи | `viewCalendar` | 6049–6133 |
| Роутер и навигация | `VIEWS`, `route`, `renderNav`, `navApplyOrder`; сам список пунктов меню — `NAV_GROUPS` (~701) | 6134–6202 |
| Визард запуска кампании (префикс `wz*`) | `startWizard`, `renderWizard`, `wzLaunch`, `showAudiencePicker` | 6203–6449 |
| Уведомления и события | `refreshNotif`, `renderNotif`, `showEvent`, `openEntityDeep` | 6450–6590 |

Номера строк плывут после каждой правки — опорой служит **имя функции**, номер нужен лишь чтобы понять, в какой конец файла идти.

## Деплой

Сервер на GCP (34.16.12.181, IP эфемерный — сверяй перед SSH). **Кнопка деплоя стирает любые ручные правки на сервере.** Изменения вносятся только локально → коммит → штатный деплой. Не редактировать код через SSH напрямую.

## Модель по умолчанию

Рутинные правки — Sonnet. Рискованные операции (рефакторинг/разбиение `app.py` и `index.html` на модули, миграции БД) — Opus.
