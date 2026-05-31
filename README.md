# CIAN Rent Alerts

MVP-сервис на Python, который проверяет страницу поиска аренды на ЦИАН, сохраняет найденные объявления в SQLite или PostgreSQL и отправляет новые варианты в Telegram.

## Что умеет

- генерирует ссылку `cian.ru/cat.php` по фильтрам города, цены, комнат и типа аренды;
- при необходимости принимает готовую ссылку поиска ЦИАН;
- парсит список объявлений через `cloudscraper` + `BeautifulSoup`;
- опционально использует Playwright, если обычный HTML не содержит объявления;
- хранит объявления в SQLite и не отправляет дубли;
- отправляет краткое уведомление в Telegram;
- позволяет менять фильтры через кнопки и команды Telegram-бота;
- запускается один раз, по расписанию или в Docker.

## Быстрый старт

1. Создайте и активируйте виртуальное окружение:

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Установите проект:

```bash
pip install .
```

3. Подготовьте `.env`:

```bash
cp .env.example .env
```

Настройте фильтры:

```env
CIAN_USE_GENERATED_URL=true
CIAN_CITY=Казань
CIAN_REGION_ID=4777
CIAN_ROOMS=1,2
CIAN_MIN_PRICE=35000
CIAN_MAX_PRICE=45000
CIAN_RENT_TYPE=long
CIAN_SORT_BY=creation_date_from_newer_to_older
CIAN_POLYGON=
CIAN_AREA_LABEL=
```

Затем укажите `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` и `ADMIN_TELEGRAM_IDS`.

Если нужна полностью ручная ссылка, выставьте:

```env
CIAN_USE_GENERATED_URL=false
CIAN_SEARCH_URL=https://cian.ru/cat.php?...
```

4. Проверьте один запуск:

```bash
cian-rent-alerts --once
```

Быстро проверить только парсер без БД, Telegram и scheduler:

```bash
cian-rent-alerts --parser-smoke
```

Проверить доступность БД и примененную схему:

```bash
cian-rent-alerts --healthcheck
```

5. Запустите постоянную проверку:

```bash
cian-rent-alerts
```

По умолчанию проверка идет каждые 10 минут. Интервал задается через
`CHECK_INTERVAL_SECONDS`. Между проверками разных пользовательских поисков worker
делает паузу `SEARCH_CHECK_DELAY_SECONDS`, чтобы не отправлять много запросов к
ЦИАН подряд.

Уровень логирования задается через `LOG_LEVEL`. По умолчанию используется `INFO`,
для подробной диагностики можно поставить `DEBUG` или запустить CLI с `--verbose`.

При постоянном запуске сервис одновременно:

- проверяет ЦИАН по расписанию;
- слушает команды Telegram-бота для изменения фильтров.

## Команды Telegram-бота

Настройки, заданные через Telegram, сохраняются в SQLite и перекрывают значения из `.env`. Чтобы вернуться к `.env`, используйте `/reset_settings`.

```text
/start
/menu
/settings
```

Открыть кнопочное меню или показать текущие фильтры. В меню есть кнопки для города, комнат, цены, типа аренды, области поиска, сортировки, ручной ссылки, проверки сейчас и режима "только новые". В каждом разделе есть кнопка `Ввести вручную`, которая показывает нужную команду для точного значения.

```text
/search_url
```

Показать фактическую ссылку поиска.

```text
/set_city Казань 4777
/set_region 4777
/set_price 35000 45000
/set_rooms 1,2
/set_rent long
/set_sort creation_date_from_newer_to_older
/set_area https://kazan.cian.ru/map/?...
/set_radius 1000 Казань, Кремлевская 18
```

Настроить генерацию `cat.php` URL.

Для поиска рядом с адресом используйте `/set_radius`: радиус задается в метрах, адрес геокодируется через OpenStreetMap/Nominatim один раз при настройке, затем сохраняется как область поиска. Регулярные проверки используют уже сохраненную область и не геокодируют адрес повторно.

Для поиска в выделенной области откройте карту ЦИАН, выделите область, скопируйте ссылку из браузера и отправьте ее через `/set_area`. Чтобы убрать ограничение по области:

```text
/set_area clear
```

```text
/set_url https://cian.ru/cat.php?...
/use_generated true
```

Переключиться на ручную ссылку или обратно на генерацию ссылки из фильтров.

```text
/check
/mark_existing_sent
/reset_settings
```

`/check` запускает проверку сразу. `/mark_existing_sent` помечает уже найденные объявления как отправленные, чтобы не получать старую выдачу первым сообщением. `/reset_settings` сбрасывает настройки из Telegram.

Кнопка `Только новые` делает то же самое, что и `/mark_existing_sent`: текущая выдача остается в базе, но не отправляется в Telegram. После этого сервис будет присылать только объявления, которые появятся позже.

## Админские команды

Админские команды доступны только chat id из `ADMIN_TELEGRAM_IDS`:

```text
/admin_status
/admin_health
/admin_report
/admin_last_runs
/admin_errors
```

Они показывают состояние БД, миграций, внутренних проверок, последние запуски и последние ошибки. Эти команды предназначены для мониторинга и не показываются обычным пользователям в меню.

## Dry-run

Чтобы проверить парсинг без Telegram, включите в `.env`:

```env
DRY_RUN=true
```

В этом режиме новые объявления будут выводиться в лог без отправки в Telegram и без пометки `sent_at`.

## Playwright

Если ЦИАН перестанет отдавать объявления в обычном HTML:

```bash
pip install '.[playwright]'
playwright install chromium
```

И включите:

```env
USE_PLAYWRIGHT=true
```

Если нужно сначала пробовать обычный Requests-парсер, а Playwright использовать только
при `captcha` или `empty_parse`, включите fallback:

```env
PLAYWRIGHT_FALLBACK=true
```

При `USE_PLAYWRIGHT=true` сервис сразу использует Playwright и fallback не нужен.

## Диагностика парсинга

Для временных сетевых ошибок сервис повторяет запрос перед тем, как считать проверку
сломавшейся:

```env
PARSER_RETRY_ATTEMPTS=2
PARSER_RETRY_BACKOFF_SECONDS=2
```

Retry применяется к сетевым ошибкам и таймаутам. `captcha` и `empty_parse` не
повторяются бесконечно: для них используется Playwright fallback, если он включен.

Если отдельный поиск продолжает падать, сервис временно ставит на паузу только
этот поиск:

```env
PARSER_PROBLEM_COOLDOWN_SECONDS=3600
PARSER_NETWORK_COOLDOWN_SECONDS=900
```

`PARSER_PROBLEM_COOLDOWN_SECONDS` применяется для `captcha` и `empty_parse`,
`PARSER_NETWORK_COOLDOWN_SECONDS` - для сетевых ошибок после retry.

При `captcha` или `empty_parse` сервис сохраняет HTML страницы для диагностики:

```env
PARSER_DEBUG_DIR=data/debug_pages
```

В Docker эти файлы хранятся в volume `debug_pages`. Они помогают понять, что именно вернул ЦИАН: капчу, пустую выдачу или измененную разметку.

## База данных

В продовом Docker Compose используется PostgreSQL:

```env
POSTGRES_DB=flatpulse
POSTGRES_USER=flatpulse
POSTGRES_PASSWORD=replace_with_strong_password
```

Для локальной разработки можно не задавать `DATABASE_URL`; тогда сервис использует SQLite из `DATABASE_PATH`. В Docker Compose `DATABASE_URL` собирается из `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD`, поэтому пароль не хранится в `docker-compose.yml`.

Схема БД версионируется через Alembic. В Docker миграции запускаются автоматически перед стартом приложения:

```env
RUN_MIGRATIONS=true
```

Для ручного запуска миграций:

```bash
.venv/bin/alembic upgrade head
```

`ListingStore.init()` пока остается как локальная страховка для SQLite и тестов, но для продового PostgreSQL основным путем должен быть `alembic upgrade head`.

Если SQLite-база уже была создана до появления Alembic, не удаляйте `data/listings.sqlite3`: там могут быть пользователи, поиски и уже найденные объявления. Сначала отметьте текущую схему как baseline, затем примените новые миграции:

```bash
.venv/bin/alembic stamp 20260530_0001
.venv/bin/alembic upgrade head
.venv/bin/alembic current
```

`stamp` нужен только для старой уже существующей базы. Для новой пустой базы достаточно `.venv/bin/alembic upgrade head`.

Текущий пользовательский сценарий пока работает как один активный поиск, но схема БД уже содержит основу для публичного мультипользовательского режима:

- `users` - пользователи Telegram;
- `searches` - сохраненные поиски пользователей;
- `search_seen_listings` - дедупликация объявлений на уровне конкретного поиска;
- `listings` - общий каталог найденных объявлений;
- `check_runs` - история технических проверок для мониторинга.

## Docker

```bash
cp .env.example .env
docker compose up --build -d
```

Перед запуском заполните в `.env` реальные значения:

```env
TELEGRAM_BOT_TOKEN=...
ADMIN_TELEGRAM_IDS=...
POSTGRES_DB=flatpulse
POSTGRES_USER=flatpulse
POSTGRES_PASSWORD=...
```

`docker-compose.yml` не хранит пароль Postgres и собирает `DATABASE_URL` из этих переменных. Сам файл `.env` не должен попадать в git или Docker build context.

PostgreSQL-данные хранятся в Docker volume `postgres_data`. В Docker Compose приложение разделено на процессы:

- `migrate` - применяет Alembic-миграции и завершается;
- `bot` - слушает Telegram polling;
- `worker` - выполняет проверки по расписанию.

`bot` и `worker` стартуют только после успешного завершения `migrate` и используют одну PostgreSQL-базу.
Для `bot` и `worker` настроен Docker healthcheck через `cian-rent-alerts --healthcheck`.

Посмотреть логи:

```bash
docker compose logs -f migrate bot worker
```

## Production Operations

Базовый запуск после настройки `.env`:

```bash
docker compose up -d postgres
docker compose run --rm migrate
docker compose up -d bot worker
```

Остановить пользовательские процессы, не выключая PostgreSQL:

```bash
docker compose stop bot worker
```

Полностью остановить compose-проект:

```bash
docker compose down
```

Перезапустить bot и worker после изменения `.env`:

```bash
docker compose up -d bot worker
```

Проверить статус контейнеров:

```bash
docker compose ps
```

Смотреть логи в реальном времени:

```bash
docker compose logs -f --tail=100 bot worker
```

Проверить БД и примененную миграцию из контейнера:

```bash
docker compose run --rm bot cian-rent-alerts --healthcheck
```

Проверить парсер без Telegram-рассылки:

```bash
docker compose run --rm worker cian-rent-alerts --parser-smoke
```

Обновить сервер с GitHub:

```bash
git pull
docker compose build bot worker migrate
docker compose run --rm migrate
docker compose up -d bot worker
docker compose ps
```

Если `bot` пишет `telegram.error.Conflict`, значит где-то запущен второй экземпляр
Telegram polling с тем же токеном. Остановите лишний процесс или контейнер, затем
перезапустите только один `bot`.

Если `worker` внезапно рассылает объявления из старого одиночного поиска, проверьте
локальный `.env`: для мультипользовательского режима не должны быть активны
`CIAN_SEARCH_URL` и `TELEGRAM_CHAT_ID`.

Если в логах часто появляются `captcha` или `empty_parse`, проверьте:

```bash
docker compose logs --tail=200 worker
docker compose run --rm worker cian-rent-alerts --parser-smoke
```

HTML для диагностики сохраняется в volume `debug_pages`. Эти файлы могут содержать
страницы выдачи и не должны попадать в git.

Нельзя коммитить или отправлять в публичные каналы:

- `.env`;
- backup dump-файлы;
- HTML из `debug_pages`;
- Telegram token, Postgres password и chat id пользователей.

## Server Deploy

Для первого VPS достаточно Docker Compose как оркестратора: один `bot`, один
`worker`, один `postgres`. Не запускайте параллельно локальный
`.venv/bin/cian-rent-alerts` с тем же Telegram token.

Рекомендуемый каталог на сервере:

```bash
/opt/flatpulse
```

Первичная установка:

```bash
git clone https://github.com/an6esign/FlatPulse.git /opt/flatpulse
cd /opt/flatpulse
cp .env.example .env
```

Заполните `.env` реальными секретами на сервере. Файл `.env` не должен попадать в
git, backup-архивы или публичные логи.

Первый запуск:

```bash
docker compose config --quiet
docker compose run --rm migrate
docker compose up -d bot worker
docker compose ps
```

Обновление сервера:

```bash
sh deploy/update.sh
```

Пример systemd unit лежит в `deploy/flatpulse.service.example`. Перед установкой
проверьте `WorkingDirectory` и путь к `docker` на вашем сервере:

```bash
sudo cp deploy/flatpulse.service.example /etc/systemd/system/flatpulse.service
sudo systemctl daemon-reload
sudo systemctl enable flatpulse
sudo systemctl start flatpulse
sudo systemctl status flatpulse
```

Остановить сервис:

```bash
sudo systemctl stop flatpulse
```

После reboot systemd поднимет Docker Compose stack, а `restart: unless-stopped`
перезапустит отдельные контейнеры при падении процесса.

## Production Smoke Checklist

Перед деплоем или после обновления на сервере:

1. Проверьте, что `.env` заполнен реальными значениями и не содержит placeholder-паролей:

```bash
docker compose --env-file .env config --quiet
```

2. Примените миграции:

```bash
docker compose run --rm migrate
```

3. Проверьте парсер без Telegram, scheduler и рассылок:

```bash
docker compose run --rm bot cian-rent-alerts --parser-smoke
```

4. Проверьте БД и миграции:

```bash
docker compose run --rm bot cian-rent-alerts --healthcheck
```

5. Запустите сервисы:

```bash
docker compose up -d bot worker
```

6. Проверьте логи старта:

```bash
docker compose logs -f --tail=100 bot worker
```

7. В Telegram от админского аккаунта проверьте:

```text
/admin_health
/admin_status
```

Ожидаемо: `DB: ok`, актуальная schema version, worker без постоянных `captcha`,
`empty_parse` или `network` ошибок.

## Backup и Restore

Создать backup PostgreSQL:

```bash
docker compose --profile ops run --rm backup
```

Backup сохраняется в Docker volume `backups` как файл вида `flatpulse_YYYYmmdd_HHMMSS.dump`.

Посмотреть список backup-файлов:

```bash
docker compose --profile ops run --rm backup sh -c 'ls -lh /backups'
```

Восстановить backup:

```bash
docker compose stop bot worker
BACKUP_FILE=flatpulse_YYYYmmdd_HHMMSS.dump docker compose --profile ops run --rm restore
docker compose up -d bot worker
```

Restore выполняет `pg_restore --clean --if-exists`: текущие таблицы будут перезаписаны данными из backup. Перед восстановлением убедитесь, что выбран правильный файл.

## Важные замечания

CIAN может менять HTML-разметку и ограничивать автоматизированные запросы. Для MVP парсер использует подход `cat.php` и селекторы карточек `article[data-name='CardComponent']`, но после первого реального запуска стоит проверить, что из конкретной страницы корректно извлекаются цена, адрес и ссылка. Соблюдайте правила сайта и не ставьте слишком частый интервал проверки.
