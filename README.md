# bw-tg-backup

Ежедневная полная копия `bwdata` официального Bitwarden: остановка сервиса,
`tar.gz` → шифрование `age` → запуск сервиса → отправка в личный Telegram-чат.
Секретный ключ расшифрования на сервере не нужен.

## Что приходит в Telegram

Все сообщения и файлы содержат один идентификатор бэкапа, например
`20261016T000000Z-1234` (время UTC и PID процесса).

1. **Начало:** «Начинаю резервное копирование. Останавливаю Bitwarden».
   Пока Telegram не подтвердил доставку, сервис не останавливается.
2. **Включение:** «Bitwarden запущен и готов к работе. Начинаю отправку архива»,
   с длительностью простоя. Сообщение отправляется после проверки контейнеров:
   все работают; для контейнеров с healthcheck требуется `healthy`.
3. Архив либо его пронумерованные части, затем `manifest.json` с порядком
   сборки, размерами и SHA-256 частей и полного зашифрованного архива.
4. **Завершение:** «Бэкап завершён», размер, количество файлов, длительность,
   простой и результат запуска Bitwarden. Только после доставки всех файлов
   и манифеста.

При сбое бот сообщает этап, состояние сервиса и путь к сохранённому архиву.
Если Telegram недоступен, ошибка остаётся в журнале и коде завершения.
Отказ уведомления о включении не прерывает отправку архива, но делает итоговый
код запуска ненулевым. Файлы при этом могут быть полностью доставлены.

## Требования

- Linux с systemd, Bash и официальным Bitwarden, установленным через
  `bitwarden.sh` от отдельного пользователя (по умолчанию `bitwarden`).
- Встроенная MSSQL и **все постоянные данные контейнеров внутри `bwdata`**.
  Внешняя БД, Docker volumes и внешние bind mounts не поддерживаются;
  исключение — read-only `/etc/localtime` и `/etc/timezone`.
- Docker с Compose v2 и поддержкой `config --format json`.
- `age`, `curl`, `jq`, GNU tar/coreutils, `flock`, `runuser`, `setsid`,
  `timeout`, сертификаты CA. Скрипт запускается от root для чтения MSSQL,
  сохранения владельцев файлов и запуска команд от пользователя Bitwarden.
- Свободное место: удвоенный apparent size `bwdata` плюс 100 MiB.
  Большой архив хранится целиком и дополнительно разбивается на части.

Для Debian/Ubuntu, если Docker уже установлен:

```bash
sudo apt-get update
sudo apt-get install age curl jq tar coreutils util-linux ca-certificates
```

Сервис останавливается на всё время сжатия и шифрования, а не отправки.
Перед запуском исходный Bitwarden должен работать и быть здоровым.
Ссылки внутри архива сохраняются как ссылки: данные за внешними симлинками
не копируются. Разместите все необходимые данные непосредственно в `bwdata`.
На время задания не запускайте обновление или другую процедуру управления
Bitwarden: `flock` исключает только другие запуски этого скрипта.

## Ключ шифрования

На **своём компьютере**, отдельно от сервера:

```bash
umask 077
age-keygen -o backup-key.txt
age-keygen -y backup-key.txt
```

В конфигурацию сервера скопируйте только открытый ключ `age1…`.
Сохраните `backup-key.txt` отдельно от Bitwarden и сервера, с доступной
резервной копией: он необходим для восстановления после потери сервера.
Поддерживаются обычные ключи age X25519; пароль для ежедневного запуска
не требуется. Не добавляйте секретный ключ в этот проект или Telegram.

## Telegram-бот и chat_id

1. Создайте отдельного бота командой `/newbot` у [@BotFather](https://t.me/BotFather).
2. В личном чате с созданным ботом отправьте `/start`.
3. Получите ID своего личного чата. Следующие команды выполняются в Bash;
   токен вводится скрыто и передаётся curl через stdin, без записи в историю
   и аргументы процесса:

```bash
read -r -s -p 'Bot token: ' BOT_TOKEN
printf '\n'
printf 'url = "https://api.telegram.org/bot%s/getUpdates"\n' "$BOT_TOKEN" |
  curl -q --config - --silent --proto '=https' --connect-timeout 15 --max-time 30 |
  jq '.result[] | .message.chat? | select(.type == "private") | {id, type}'
unset BOT_TOKEN
```

Используйте положительный `id` своего чата, а не ID бота. Если список пуст,
снова отправьте боту сообщение. Этот бот не должен одновременно обслуживаться
другим polling-процессом или webhook. Скрипт сам не читает входящие команды.

## Установка

В каталоге проекта на Linux-сервере:

```bash
sudo install -m 0755 backup.sh /usr/local/sbin/bw-tg-backup
sudo install -m 0600 backup.conf.example /etc/bw-tg-backup.conf
sudo install -d -m 0700 /var/backups/bw-tg-backup
sudoedit /etc/bw-tg-backup.conf
```

Заполните `AGE_RECIPIENT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
При необходимости измените пути и имя пользователя.
Конфигурация — доверенный Bash-файл, выполняемый от root, с владельцем
`root` и правами `0600` (либо `0400`), без симлинка. Каталог бэкапов должен
принадлежать root, иметь права `0700` и находиться вне `bwdata`.

| Параметр | По умолчанию | Назначение |
| --- | --- | --- |
| `BITWARDEN_DIR` | `/opt/bitwarden` | Каталог с `bitwarden.sh` и `bwdata` |
| `BITWARDEN_USER` | `bitwarden` | Непривилегированный пользователь установки с доступом к Docker |
| `BACKUP_DIR` | `/var/backups/bw-tg-backup` | Локальные зашифрованные копии |
| `AGE_RECIPIENT` | обязательный | Открытый ключ `age1…` |
| `TELEGRAM_BOT_TOKEN` | обязательный | Токен от BotFather |
| `TELEGRAM_CHAT_ID` | обязательный | ID вашего личного чата |
| `RETENTION_DAYS` | `7` | Хранение доставленных копий после последней доставки |
| `PART_BYTES` | `45000000` | Максимальный размер части; можно уменьшить |
| `SERVICE_TIMEOUT` | `300` | Таймаут каждой команды stop/start/fallback, секунды |
| `READY_TIMEOUT` | `300` | Ожидание готовности после команды запуска, секунды |
| `HTTP_TIMEOUT` | `600` | Таймаут каждого HTTP-запроса, секунды |

Проверка без остановки сервиса и без сообщений в чат:

```bash
sudo /usr/local/sbin/bw-tg-backup --config /etc/bw-tg-backup.conf --check
```

Первый настоящий запуск (остановит сервис на время создания архива):

```bash
sudo /usr/local/sbin/bw-tg-backup --config /etc/bw-tg-backup.conf
```

Скачайте полученный набор из Telegram и выполните пробное восстановление
на отдельной установке совместимой версии Bitwarden. Затем установите таймер:

```bash
sudo install -m 0644 systemd/bw-tg-backup.service /etc/systemd/system/
sudo install -m 0644 systemd/bw-tg-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/bw-tg-backup.service /etc/systemd/system/bw-tg-backup.timer
sudo systemctl enable --now bw-tg-backup.timer
systemctl list-timers bw-tg-backup.timer
```

Ежедневно в **03:00 Europe/Moscow**, независимо от часового пояса сервера.
`Persistent=true` выполняет пропущенное задание после включения сервера;
это тоже сопровождается остановкой Bitwarden. Для другого времени измените
`OnCalendar` в таймере и перезапустите его.

```bash
sudo systemctl start bw-tg-backup.service
sudo journalctl -u bw-tg-backup.service -n 100 --no-pager
sudo systemctl status bw-tg-backup.service
```

## Повторная отправка и хранение

```bash
sudo /usr/local/sbin/bw-tg-backup --config /etc/bw-tg-backup.conf \
  --send-existing /var/backups/bw-tg-backup/20261016T000000Z-1234
```

Повторная отправка проверяет SHA-256 полного архива по исходному манифесту
либо сохранённой при шифровании сумме `.archive.sha256`, заново создаёт части
и отправляет весь набор. Bitwarden и Docker daemon при этом не используются;
сам каталог установки может отсутствовать. Уведомления сообщают только
о начале и результате повторной отправки. При отсутствии исходной суммы
или несовпадении данных отправка запрещается.

Структура локальной копии:

```text
20261016T000000Z-1234/
  bwdata-20261016T000000Z-1234.tar.gz.age
  bwdata-20261016T000000Z-1234.tar.gz.age.part000001  # для больших архивов
  bwdata-20261016T000000Z-1234.tar.gz.age.part000002
  manifest.json
  .archive.sha256
  .delivered                                      # только после доставки набора
```

Отметка `.delivered` означает доставку всех частей и манифеста, независимо
от результата последнего текстового уведомления. Повторная успешная доставка
обновляет её время. Очистка выполняется только после полностью успешного
запуска: удаляются доставленные наборы старше `RETENTION_DAYS × 24 часов`.
Текущий набор и неотправленные архивы не удаляются. В Telegram файлы остаются.
Неотправленные копии могут накапливаться; их размер виден на диске, а сбои —
в журнале. Прерванные `.partial` удаляются при обработанном завершении.

## Восстановление

Проверка, сборка и расшифровка ниже подходят для **Linux и macOS**, в Bash или
zsh. Нужны `jq`, `age`, `shasum` и `tar`. Скачайте **все** части одного набора
и соответствующий `manifest.json` в новый приватный каталог и откройте в нём
терминал.
Если Telegram добавил суффикс к имени скачанного файла, верните имя из манифеста.
Манифест приходит последним; без полного набора копия не считается доставленной.

Укажите фактический путь к секретному ключу в `BACKUP_KEY`. Блок работает
также для единственного файла; результаты сохраняются в новый каталог `verify.…`:

```bash
(
  set -euo pipefail
  umask 077

  BACKUP_KEY='/safe/path/backup-key.txt'
  VERIFY_DIR=$(mktemp -d './verify.XXXXXXXX')

  jq -er '.parts[] | "\(.sha256)  \(.name)"' manifest.json \
    > "$VERIFY_DIR/parts.sha256"
  shasum -a 256 --check --strict "$VERIFY_DIR/parts.sha256"

  jq -er '.parts[].name' manifest.json > "$VERIFY_DIR/parts.list"
  while IFS= read -r part; do
    cat -- "$part"
  done < "$VERIFY_DIR/parts.list" > "$VERIFY_DIR/restored.tar.gz.age"

  printf '%s  %s\n' \
    "$(jq -er '.archive_sha256' manifest.json)" \
    "$VERIFY_DIR/restored.tar.gz.age" |
    shasum -a 256 --check --strict

  age --decrypt -i "$BACKUP_KEY" \
    -o "$VERIFY_DIR/restored.tar.gz" \
    "$VERIFY_DIR/restored.tar.gz.age"

  tar -tzf "$VERIFY_DIR/restored.tar.gz" > "$VERIFY_DIR/files.txt"

  printf 'Проверка успешна. Результаты: %s\n' "$VERIFY_DIR"
)
```

В созданном каталоге находятся собранный `restored.tar.gz.age`, расшифрованный
`restored.tar.gz` и список содержимого `files.txt`.

`age` проверяет подлинность зашифрованного содержимого; SHA-256 помогает выявить
неполные, перепутанные или повреждённые части. Расшифрованный архив содержит
конфигурационные секреты — храните его в приватном каталоге.

Для дальнейшего **восстановления на Linux** нужен GNU tar. Перенесите проверенный
архив на целевой сервер, если проверяли его на Mac. Перейдите в каталог с архивом
(подставьте фактический путь вместо `/path/to/verify.XXXXXXXX`) и распакуйте его:

```bash
cd /path/to/verify.XXXXXXXX
mkdir -m 0700 restored
sudo tar --extract --gzip --acls --xattrs --numeric-owner --same-owner --same-permissions \
  --file restored.tar.gz --directory restored
```

Извлекайте архив только после успешного завершения расшифровки.
`restored/bwdata` также содержит незашифрованные конфигурационные секреты.

На целевом сервере подготовьте **совместимую версию** официального Bitwarden
(версии образов можно найти в восстановленном `bwdata/docker/docker-compose.yml`).
При переносе сопоставьте числовые UID/GID с исходным сервером, проверьте
домен, сертификаты и параметры установки. Отключите таймер на время восстановления:

```bash
sudo systemctl disable --now bw-tg-backup.timer
sudo systemctl stop bw-tg-backup.service
sudo -u bitwarden bash -c 'cd /opt/bitwarden && ./bitwarden.sh stop'
sudo mv /opt/bitwarden/bwdata /opt/bitwarden/bwdata.before-restore
sudo mv restored/bwdata /opt/bitwarden/bwdata
sudo -u bitwarden bash -c 'cd /opt/bitwarden && ./bitwarden.sh start'
```

Проверьте здоровье контейнеров, вход в хранилище, записи, организации и вложения.
Не заменяйте каталог на действующем сервере во время пробного восстановления.
После проверки снова включите таймер, если это рабочий сервер.

## Поведение при сбоях

- `EXIT`, `INT`, `TERM` после начала остановки вызывают попытку запуска.
  При прерывании архивирования сначала останавливается вся группа процессов
  `tar → age → tee → sha256sum`, затем запускается сервис.
- Штатный `bitwarden.sh start` может загружать образы из сети. При его ошибке
  используется Compose `up -d --pull never --no-build` от пользователя установки.
  Параметры и вывод Bitwarden подавлены: они могут содержать секреты.
- HTTP 5xx, сетевые ошибки и 429 повторяются до трёх раз после первой попытки;
  для 429 учитывается `retry_after`. Между запросами есть пауза 1,1 секунды.
  При потере ответа возможны дубликаты; сопоставляйте их по ID и SHA-256.
- Ошибка запуска или готовности не выдаётся за успешное включение.
  Требуется ручное вмешательство, если все попытки запуска завершились неудачно.
- `SIGKILL`, отключение питания и сбой Docker/ОС не позволяют гарантировать
  восстановление процесса обработчиком Bash. После такого случая проверьте
  состояние Bitwarden, журнал и незавершённые каталоги перед повторным запуском.
- Таймаут остановки systemd — 30 минут для восстановления сервиса. При
  увеличении `SERVICE_TIMEOUT`/`READY_TIMEOUT` соответственно увеличьте
  `TimeoutStopSec` в unit-файле. Время HTTP-повторов с `retry_after` может быть
  больше обычного таймаута запроса.

## Проверки проекта

```bash
bash -n backup.sh
python3 -m unittest discover -s tests -v
```

Нужны Python 3, jq, age и age-keygen. Тесты работают на Linux и macOS:
Docker, аккаунты, Telegram и несовместимые GNU-опции команд изолированы
подставными инструментами; tar и шифрование age настоящие. Проверяются
сборка и расшифровка файлов, права и ссылки, порядок уведомлений, остановка,
восстановление после сбоев/сигнала, таймаут готовности, 429/5xx/сеть,
повторная отправка, целостность, блокировка и очистка.
Тесты не обращаются к реальному Telegram и не заменяют пробное восстановление
на вашей Linux-установке.

Источники: [полный бэкап Bitwarden](https://bitwarden.com/help/backup-on-premise/),
[управление официальной установкой](https://bitwarden.com/help/install-on-premise-linux/),
[age](https://github.com/FiloSottile/age),
[Telegram sendDocument, лимит 50 МБ](https://core.telegram.org/bots/api#senddocument).
