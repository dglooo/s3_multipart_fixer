# s3_multipart_fixer

Утилита для проверки и очистки незавершённых multipart-загрузок S3. Она также умеет считать объекты и версии, проверять маркеры каталогов и состояние бакета, собирать даты объектов по префиксам и создавать тестовую незавершённую загрузку.

[English README](README.en.md)

## Подготовка

Нужен Python 3.11 или новее. Команды ниже выполняются из каталога `s3_multipart_fixer`.

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

В `.env` задайте адрес S3, имя бакета, регион и свои ключи:

```dotenv
S3_ENDPOINT=https://s3.example.internal
S3_BUCKET=my-bucket
AWS_REGION=eu-central-1
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
```

Регион должен соответствовать вашему S3-сервису. Если нужны временные учётные данные, добавьте `AWS_SESSION_TOKEN`. Вместо ключей можно использовать профиль AWS (`AWS_PROFILE`). Для отдельного окружения передайте `--env-file путь/к/файлу` **перед** командой. `--bucket` явно выбирает бакет и имеет приоритет над `S3_BUCKET` из `.env`. Глобальные параметры `--region`, `--endpoint-url`, `--access-key-id` и `--secret-access-key` также ставятся перед командой; хранить секрет в `.env` удобнее, чем в истории shell.

Файл `.env`, окружение `.venv`, рабочие данные в `files/` и отчёты в `report/` исключены из Git. Текущие JSON, JSONL и CSV также исключены по расширению. Шаблон `.env.example` не содержит рабочих ключей.

## Где лежат отчёты

Все уже существовавшие JSON и JSONL перенесены в `report/`. Новые отчёты по умолчанию сохраняются там же. Если для `--output` указать только имя файла, например `--output cleanup.jsonl`, результат попадёт в `report/cleanup.jsonl`. Явный путь сохраняется как указан; `--output -` выводит отчёт в терминал. Большой тестовый файл хранится в `files/`.

Повторный запуск с тем же именем отчёта перезапишет файл. Для сохранения истории задавайте уникальное имя через `--output`.

Основные файлы по умолчанию:

| Команда | Отчёт |
| --- | --- |
| `scan` | `report/scan.jsonl` |
| `abort` без `--execute` | `report/abort-dry-run.jsonl` |
| `abort --execute` | `report/abort-result.jsonl` |
| `count-objects` | `report/objects.json` |
| `count-object-versions` | `report/object_versions.json` |
| `head-directory-marker` | `report/marker_report.json` |
| `head-directory-markers` | `report/markers_report.json` |
| `bucket-health-check` | `report/bucket_health_check.json` |
| `collect-prefix-dates` | `report/prefix_dates.json` и `report/prefix_dates.jsonl` |

При `--format csv` для `scan` и `abort` расширение отчёта по умолчанию становится `.csv`.

## Посмотреть незавершённые загрузки

```bash
./.venv/bin/python multipart_fixer.py scan --bucket my-bucket --older-than 30d
```

`scan` ничего не удаляет. Каждая строка отчёта содержит бакет, ключ, `upload_id`, время начала, класс хранения и сведения об уже загруженных частях. Можно сузить выборку через `--prefix`, `--key`, `--upload-id`, `--limit`, `--initiated-after` и `--initiated-before`. Для параллельного сканирования добавьте `--io-mode async --concurrency 20`.

## Очистить загрузки, начатые до заданной даты

Пример: удалить только загрузки, **начатые до 1 мая 2026 года 00:00 UTC**. Загрузки с более поздним временем начала остаются. Используйте полную дату с часовым поясом (`Z` означает UTC).

1. Сохраните выборку и проверьте её:

   ```bash
   ./.venv/bin/python multipart_fixer.py scan \
     --bucket my-bucket \
     --initiated-before 2026-05-01T00:00:00Z \
     --output report/scan-before-may.jsonl
   ```

2. Выполните пробный запуск по этой выборке. Без `--execute` запросов на удаление нет:

   ```bash
   ./.venv/bin/python multipart_fixer.py abort \
     --bucket my-bucket \
     --input-json report/scan-before-may.jsonl \
     --initiated-before 2026-05-01T00:00:00Z \
     --output report/abort-before-may-dry-run.jsonl
   ```

3. После просмотра отчёта запустите очистку той же выборки:

   ```bash
   ./.venv/bin/python multipart_fixer.py abort \
     --bucket my-bucket \
     --input-json report/scan-before-may.jsonl \
     --initiated-before 2026-05-01T00:00:00Z \
     --execute \
     --output report/abort-before-may-result.jsonl
   ```

`abort` проверяет исчезновение каждой загрузки после запроса; результат проверки находится в поле `verified`. При работе с сохранённым отчётом фильтр `--bucket` применяется к каждой строке. Если отчёт устарел, лучше сделать новую выборку перед очисткой. Для плавающего срока вместо точной даты используйте `--older-than 30d` (или `12h`, `1w`): очистятся только загрузки старше этого срока на момент запуска. `--since` является тем же фильтром возраста. `--initiated-before` и `--older-than` одновременно использовать нельзя.

Можно также очищать напрямую из S3 без `--input-json`; сначала запустите `abort` без `--execute` с нужными фильтрами, затем повторите с `--execute`. Для ограничения только части пространства ключей добавьте `--prefix some/path/` в обе команды.

## Другие команды

```bash
./.venv/bin/python multipart_fixer.py count-objects --bucket my-bucket --prefix some/path/
./.venv/bin/python multipart_fixer.py count-object-versions --bucket my-bucket
./.venv/bin/python multipart_fixer.py bucket-health-check --bucket my-bucket
./.venv/bin/python multipart_fixer.py head-directory-markers --bucket my-bucket --input-json report/marker_paths.json
./.venv/bin/python multipart_fixer.py collect-prefix-dates --bucket my-bucket --input-json report/objects_.json
```

Вспомогательные скрипты `parse_objects_tree.py`, `count_anchor_objects.py` и `extract_marker_paths.py` по умолчанию читают JSON из `report/` и пишут результат туда же.
Если нужен новый входной файл для них или для `collect-prefix-dates`, запустите `count-objects` с `--output report/objects_.json` и подходящим `--directory-depth`.

Для теста можно создать намеренно незавершённую загрузку:

```bash
./.venv/bin/python multipart_failure_lab.py --bucket my-bucket --file files/50M.bin --part-size 8388608 --uploaded-parts 2 --count 1
```

Она останется в бакете до завершения или очистки. Справка по всем параметрам: `./.venv/bin/python multipart_fixer.py --help`, а также `./.venv/bin/python multipart_fixer.py scan --help` и `abort --help`.
