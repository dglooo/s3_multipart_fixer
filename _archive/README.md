# s3_uploader

Модуль для тестовых загрузок и удалений объектов в S3 с накопительной отчетностью в JSON.

Основные возможности:
- многократная загрузка одного и того же файла `100M.bin` в разные bucket'ы;
- разбиение статистики по `STANDARD` и `TYPE_1`;
- накопительный отчет без перезаписи истории;
- отдельный просмотр отчета в человекочитаемом виде;
- удаление объектов на основе уже записанного отчета.

## Структура

- `upload_interface.py` - CLI для загрузки
- `delete_interface.py` - CLI для удаления
- `report_viewer.py` - просмотр отчета
- `upload_plan.example.json` - пример конфига загрузки
- `delete_plan.example.json` - пример конфига удаления
- `results/s3_upload_report.json` - общий накопительный отчет
- `results/sessions/*.json` - отдельные файлы по каждой сессии

## Зависимости

Минимально нужны:
- `requests`
- `requests-aws4auth`
- `python-dotenv`
- `loguru`

Если зависимости еще не установлены, поставьте их в ваше окружение Python.

## Подготовка

### 1. Положите файл

Файл для загрузки должен лежать в `s3_uploader/` под именем:

```text
100M.bin
```

Можно указать и другой путь в конфиге, но ваш основной сценарий уже заложен под `100M.bin`.

### 2. Подготовьте переменные окружения

Нужны стандартные переменные:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
```

Либо положите их в `.env`, если запускаете через `python-dotenv`.

В конфиге также можно указывать ключи напрямую:

```json
{
  "connection": {
    "region": "eu",
    "endpoint": "https://s3.example.local",
    "access_key": "YOUR_ACCESS_KEY",
    "secret_key": "YOUR_SECRET_KEY"
  }
}
```

Поддерживаются оба варианта:
- прямые поля `access_key` и `secret_key`;
- поля `access_key_env` и `secret_key_env` с именами переменных окружения.

Для совместимости модуль также понимает случай, когда в `access_key_env` и `secret_key_env` по ошибке положены сами значения ключей.

### 3. Настройте конфиг загрузки

Пример файла: `upload_plan.example.json`

```json
{
  "connection": {
    "region": "eu",
    "endpoint": "https://s3.example.local",
    "access_key_env": "AWS_ACCESS_KEY_ID",
    "secret_key_env": "AWS_SECRET_ACCESS_KEY"
  },
  "report_dir": "results",
  "upload": {
    "file_path": "100M.bin",
    "mode": "http",
    "chunk_size_mb": 100,
    "create_bucket": true,
    "key_prefix": "load-test-object",
    "bucket_plans": [
      {
        "bucket": "bucket-standard",
        "upload_count": 2,
        "storage_class": "STANDARD"
      },
      {
        "bucket": "bucket-type1",
        "upload_count": 3,
        "storage_class": "TYPE_1"
      }
    ]
  }
}
```

Или с прямыми ключами:

```json
{
  "connection": {
    "region": "eu",
    "endpoint": "https://s3.example.local",
    "access_key": "YOUR_ACCESS_KEY",
    "secret_key": "YOUR_SECRET_KEY"
  },
  "report_dir": "results",
  "upload": {
    "file_path": "100M.bin",
    "mode": "http",
    "chunk_size_mb": 100,
    "create_bucket": true,
    "key_prefix": "load-test-object",
    "bucket_plans": [
      {
        "bucket": "bucket-standard",
        "upload_count": 2,
        "storage_class": "STANDARD"
      }
    ]
  }
}
```

Базовый пример через env:

```json
{
  "connection": {
    "region": "eu",
    "endpoint": "https://s3.example.local",
    "access_key_env": "AWS_ACCESS_KEY_ID",
    "secret_key_env": "AWS_SECRET_ACCESS_KEY"
  },
  "report_dir": "results",
  "upload": {
    "file_path": "100M.bin",
    "mode": "http",
    "chunk_size_mb": 100,
    "create_bucket": true,
    "key_prefix": "load-test-object",
    "bucket_plans": [
      {
        "bucket": "bucket-standard",
        "upload_count": 2,
        "storage_class": "STANDARD"
      },
      {
        "bucket": "bucket-type1",
        "upload_count": 3,
        "storage_class": "TYPE_1"
      }
    ]
  }
}
```

### Пояснение полей загрузки

- `connection.region` - регион S3
- `connection.endpoint` - endpoint вашего S3
- `report_dir` - директория, где будут жить итоговый отчет и session-файлы
- `upload.file_path` - путь к `100M.bin`
- `upload.mode`:
  - `http` - обычный `PUT`
  - `multipart` - реальный S3 multipart upload
- `upload.chunk_size_mb`:
  - для `multipart` это размер multipart-части;
  - для `http` при размере файла больше этого значения будут созданы отдельные объекты `.part1`, `.part2` и т.д.
- `upload.create_bucket` - создавать bucket перед загрузкой
- `upload.key_prefix` - общий префикс ключей объектов
- `upload.bucket_plans[]` - список bucket'ов и сколько раз в каждый загружать файл
- `upload.bucket_plans[].upload_count` - сколько загрузок сделать в конкретный bucket
- `upload.bucket_plans[].storage_class` - `STANDARD` или `TYPE_1`

## Загрузка

Запуск как модуль:

```bash
python3 -m s3_uploader.upload_interface --config s3_uploader/upload_plan.example.json
```

Запуск из директории модуля:

```bash
cd s3_uploader
python3 upload_interface.py --config upload_plan.example.json
```

Совместимый старый вход:

```bash
cd s3_uploader
python3 tests.py --config upload_plan.example.json
```

Что происходит при загрузке:
- для каждого bucket из `bucket_plans` создается bucket, если включен `create_bucket`;
- файл загружается `upload_count` раз;
- для каждой загрузки генерируется уникальный key;
- каждая успешная операция попадает в общий отчет;
- дополнительно создается отдельный session-файл этой загрузки.

## Удаление

Удаление работает не "по маске в S3", а по данным накопленного отчета.
То есть модуль удаляет те объекты, которые ранее были зарегистрированы как загруженные и еще считаются активными.

Пример файла: `delete_plan.example.json`

```json
{
  "connection": {
    "region": "eu",
    "endpoint": "https://s3.example.local",
    "access_key_env": "AWS_ACCESS_KEY_ID",
    "secret_key_env": "AWS_SECRET_ACCESS_KEY"
  },
  "report_dir": "results",
  "delete": {
    "delete_oldest_first": true,
    "bucket_plans": [
      {
        "bucket": "bucket-standard",
        "delete_count": 1,
        "storage_class": "STANDARD"
      },
      {
        "bucket": "bucket-type1",
        "delete_count": 2,
        "storage_class": "TYPE_1"
      }
    ]
  }
}
```

### Пояснение полей удаления

- `delete.delete_oldest_first` - удалять сначала самые старые объекты
- `delete.bucket_plans[].bucket` - bucket, в котором удаляем
- `delete.bucket_plans[].delete_count` - сколько объектов удалить
- `delete.bucket_plans[].storage_class`:
  - если указать `STANDARD` или `TYPE_1`, удаление будет только по этому типу хранения;
  - если убрать поле, удаление будет по любым активным объектам bucket'а

Запуск:

```bash
python3 -m s3_uploader.delete_interface --config s3_uploader/delete_plan.example.json
```

Или:

```bash
cd s3_uploader
python3 delete_interface.py --config delete_plan.example.json
```

Совместимый старый вход:

```bash
cd s3_uploader
python3 tests_delete.py --config delete_plan.example.json
```

## Просмотр отчетности

Человекочитаемый вывод:

```bash
python3 -m s3_uploader.report_viewer --results-dir s3_uploader/results
```

Изнутри директории:

```bash
cd s3_uploader
python3 report_viewer.py --results-dir ./results
```

Фильтр по bucket:

```bash
python3 -m s3_uploader.report_viewer --results-dir s3_uploader/results --bucket bucket-standard
```

Показать сырой JSON:

```bash
python3 -m s3_uploader.report_viewer --results-dir s3_uploader/results --json
```

## Формат отчетности

Есть 2 уровня отчетов.

### 1. Общий накопительный файл

Файл:

```text
results/s3_upload_report.json
```

В нем хранятся:
- вся история операций;
- summary по upload/delete/active;
- разбивка по bucket;
- разбивка по `STANDARD` и `TYPE_1`;
- список активных объектов, которые еще не удалены.

### 2. Файлы отдельных сессий

Файлы:

```text
results/sessions/upload_*.json
results/sessions/delete_*.json
```

В них хранится:
- тип сессии;
- время запуска;
- операции конкретного запуска;
- summary только по этой сессии.

## Что считается в отчете

Отчет считает:
- сколько объектов загружено;
- сколько байт загружено;
- сколько объектов удалено;
- сколько байт удалено;
- сколько объектов сейчас активно;
- сколько данных сейчас активно;
- все это с разбиением по:
  - bucket;
  - storage class.

## Важное замечание по режимам загрузки

### `http`

Если размер файла больше `chunk_size_mb`, модуль загружает не один объект, а несколько отдельных объектов:

```text
my-key.part1
my-key.part2
my-key.part3
```

Это не S3 multipart, а набор отдельных объектов. В отчет они тоже попадут как отдельные объекты.

### `multipart`

Это настоящий S3 Multipart Upload. В результате в bucket будет один финальный объект.

## Рекомендуемый рабочий сценарий

1. Положить `100M.bin` в `s3_uploader/`
2. Скопировать `upload_plan.example.json` в свой рабочий конфиг
3. Настроить `endpoint`, `region`, bucket'ы, `upload_count`, `storage_class`
4. Запустить загрузку
5. Проверить накопленный отчет через `report_viewer.py`
6. Подготовить `delete`-конфиг
7. Запустить удаление
8. Снова посмотреть отчет

## Быстрый пример

Загрузить:

```bash
python3 -m s3_uploader.upload_interface --config s3_uploader/upload_plan.example.json
```

Посмотреть отчет:

```bash
python3 -m s3_uploader.report_viewer --results-dir s3_uploader/results
```

Удалить:

```bash
python3 -m s3_uploader.delete_interface --config s3_uploader/delete_plan.example.json
```

Посмотреть обновленный отчет:

```bash
python3 -m s3_uploader.report_viewer --results-dir s3_uploader/results
```
