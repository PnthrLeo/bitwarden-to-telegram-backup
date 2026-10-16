#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
export LC_ALL=C

CONFIG=/etc/bw-tg-backup.conf
MODE=backup
EXISTING=''
WORK=''
RUN_DIR=''
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
STARTED=$SECONDS
STAGE=configuration
RECOVER=0
SERVICE_STATE='не изменялось'
ARCHIVE_READY=0
API_READY=0
NOTIFY_FAILED=0
DOWN_STARTED=0
DOWNTIME=0
ARCHIVE_PID=0
CONTAINERS=()
COMPOSE=()
FILES=()

log() { printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$RUN_ID" "$*" >&2; }
die() { log "$*"; exit 1; }
usage() {
    cat <<'EOF'
Usage: backup.sh [--config FILE] [--check | --send-existing DIR]
  --check              Validate configuration, containers, age and Telegram; no downtime.
  --send-existing DIR  Verify and resend a completed local archive; no service changes.
EOF
}

while (($#)); do
    case "$1" in
        --config) (($# >= 2)) || die 'Missing --config value'; CONFIG=$2; shift 2 ;;
        --check) [[ $MODE == backup ]] || die 'Choose one mode'; MODE=check; shift ;;
        --send-existing)
            (($# >= 2)) || die 'Missing --send-existing value'
            [[ $MODE == backup ]] || die 'Choose one mode'
            MODE=resend; EXISTING=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die 'Unknown argument; use --help' ;;
    esac
done

# curl's URL contains the token. Supply it only through stdin, never argv or logs.
telegram() {
    local method=$1 attempt code curl_rc api_code delay
    shift
    for attempt in 0 1 2 3; do
        sleep 1.1
        : > "$WORK/response.json"
        curl_rc=0
        code=$(printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TELEGRAM_BOT_TOKEN" "$method" |
            curl -q --config - --silent --proto '=https' --tlsv1.2 \
                --connect-timeout 15 --max-time "$HTTP_TIMEOUT" \
                --request POST --output "$WORK/response.json" --write-out '%{http_code}' \
                --form-string "chat_id=$TELEGRAM_CHAT_ID" "$@" 2>/dev/null) || curl_rc=$?
        if ((curl_rc == 0)) && [[ $code == 200 ]] &&
            jq -e '.ok == true' "$WORK/response.json" >/dev/null 2>&1; then
            return 0
        fi
        api_code=$(jq -r '.error_code // 0' "$WORK/response.json" 2>/dev/null) || api_code=0
        [[ $api_code =~ ^[0-9]+$ ]] || api_code=0
        log "Telegram $method failed (HTTP ${code:-000}, API $api_code, attempt $((attempt + 1))/4)"
        ((attempt < 3)) || return 1
        if [[ $code == 429 || $api_code == 429 ]]; then
            delay=$(jq -r '.parameters.retry_after // 5' "$WORK/response.json" 2>/dev/null) || delay=5
            [[ $delay =~ ^[1-9][0-9]*$ ]] || delay=5
        elif ((curl_rc != 0)) || [[ $code =~ ^5[0-9][0-9]$ || $api_code =~ ^5[0-9][0-9]$ ]]; then
            delay=$((2 ** (attempt + 1)))
        else
            return 1
        fi
        sleep "$delay"
    done
}

notify() {
    printf '[%s] %s\n' "$RUN_ID" "$1" > "$WORK/message.txt" || return 1
    local escaped=${WORK//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    telegram sendMessage --form "text=<\"$escaped/message.txt\""
}

send_document() {
    local escaped=${1//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    telegram sendDocument --form "document=@\"$escaped\"" \
        --form-string "caption=[$RUN_ID] $(basename "$1")"
}

private_path() {
    local owner mode
    [[ ! -L $1 ]] || return 1
    owner=$(stat -c %u -- "$1") || return 1
    mode=$(stat -c %a -- "$1") || return 1
    [[ $owner == 0 ]] && (( (8#$mode & 0077) == 0 ))
}

bw_command() {
    # start can print environment settings. Never forward its stdout/stderr.
    (cd "$BITWARDEN_DIR" && timeout --kill-after=30s "$SERVICE_TIMEOUT" \
        runuser -u "$BITWARDEN_USER" -- ./bitwarden.sh "$1") >/dev/null 2>&1
}

inspect_containers() {
    docker inspect "${CONTAINERS[@]}" > "$WORK/containers.json" 2>/dev/null
}

containers_ready() {
    inspect_containers || return 1
    jq -e --argjson count "${#CONTAINERS[@]}" '
        length == $count and all(.[];
            .State.Running == true and .State.Paused != true and .State.Restarting != true and
            ((.State.Health.Status // "healthy") == "healthy"))
    ' "$WORK/containers.json" >/dev/null 2>&1
}

wait_ready() {
    local deadline=$((SECONDS + READY_TIMEOUT))
    while :; do
        if containers_ready; then return 0; fi
        ((SECONDS < deadline)) || return 1
        sleep 5
    done
}

restore_service() {
    SERVICE_STATE='запуск не подтверждён'
    if ! bw_command start; then
        log 'bitwarden.sh start failed; trying existing images without pulling/building'
        (cd "$BITWARDEN_DIR" && timeout --kill-after=30s "$SERVICE_TIMEOUT" \
            runuser -u "$BITWARDEN_USER" -- "${COMPOSE[@]}" up -d --pull never --no-build) \
            >/dev/null 2>&1 || return 1
    fi
    wait_ready || return 1
    SERVICE_STATE='запущен, контейнеры готовы'
    RECOVER=0
    DOWNTIME=$((SECONDS - DOWN_STARTED))
    log "Bitwarden ready; downtime ${DOWNTIME}s"
}

on_exit() {
    local rc=$? saved='нет'
    trap - EXIT INT TERM
    set +e
    if ((ARCHIVE_PID)); then
        kill -TERM -- "-$ARCHIVE_PID" 2>/dev/null
        wait "$ARCHIVE_PID" 2>/dev/null
        ARCHIVE_PID=0
    fi
    if ((RECOVER)); then
        log 'Attempting service recovery before exiting'
        if restore_service; then
            if ((API_READY)); then
                notify "Bitwarden запущен и готов к работе после сбоя. Простой: ${DOWNTIME} с." || rc=1
            fi
        else
            log 'CRITICAL: Bitwarden recovery failed; manual intervention required'
            rc=1
        fi
    fi
    if ((NOTIFY_FAILED)); then rc=1; fi
    if ((rc != 0)); then
        ((ARCHIVE_READY)) && saved="да, $RUN_DIR"
        log "FAILED stage=$STAGE; service=$SERVICE_STATE; archive=$saved"
        if ((API_READY)) && [[ -d $WORK && $MODE != check ]]; then
            notify "Ошибка бэкапа. Этап: $STAGE. Bitwarden: $SERVICE_STATE. Готовый архив сохранён: $saved. Подробности — в журнале сервера." ||
                log 'Failure notification could not be delivered'
        fi
    fi
    # Keep completed ciphertext even when delivery/manifest generation failed.
    if [[ -n $RUN_DIR && $MODE == backup && $ARCHIVE_READY == 0 ]]; then
        rm -f -- "$RUN_DIR"/*.partial
        rmdir -- "$RUN_DIR" 2>/dev/null
    fi
    [[ -z $WORK ]] || rm -rf -- "$WORK"
    exit "$rc"
}
trap on_exit EXIT
trap 'STAGE="interrupted (SIGINT)"; exit 130' INT
trap 'STAGE="interrupted (SIGTERM)"; exit 143' TERM

preflight() {
    [[ $(id -u) == 0 ]] || die 'Run as root (required to read MSSQL files and preserve ownership)'
    local tool value
    for tool in bash setsid age curl jq flock runuser docker tar stat realpath timeout sha256sum split tee \
        du df tail date mktemp mkdir rm rmdir mv cat basename grep sleep; do
        command -v "$tool" >/dev/null || die "Missing dependency: $tool"
    done
    tar --version | grep -q 'GNU tar' || die 'GNU tar is required'
    [[ -f $CONFIG ]] && private_path "$CONFIG" || die 'Config must be a root-owned regular file with mode 0600/0400, not a symlink'
    value=$(stat -c %a -- "$CONFIG")
    [[ $value == 600 || $value == 400 ]] || die 'Config mode must be 0600 or 0400'
    BITWARDEN_DIR=/opt/bitwarden
    BITWARDEN_USER=bitwarden
    BACKUP_DIR=/var/backups/bw-tg-backup
    RETENTION_DAYS=7
    PART_BYTES=45000000
    SERVICE_TIMEOUT=300
    READY_TIMEOUT=300
    HTTP_TIMEOUT=600
    # Trusted root-owned shell file, not untrusted dotenv input.
    source "$CONFIG"
    set +x
    export -n TELEGRAM_BOT_TOKEN
    [[ ${TELEGRAM_BOT_TOKEN:-} =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || die 'Invalid TELEGRAM_BOT_TOKEN'
    [[ ${TELEGRAM_CHAT_ID:-} =~ ^[1-9][0-9]*$ ]] || die 'TELEGRAM_CHAT_ID must be a positive private chat ID'
    [[ ${AGE_RECIPIENT:-} =~ ^age1[0-9a-z]+$ ]] || die 'Set a native age public recipient'
    for value in "$RETENTION_DAYS" "$PART_BYTES" "$SERVICE_TIMEOUT" "$READY_TIMEOUT" "$HTTP_TIMEOUT"; do
        [[ $value =~ ^[1-9][0-9]{0,8}$ ]] || die 'Limits must be positive decimal integers (at most 9 digits)'
    done
    ((PART_BYTES <= 45000000)) || die 'PART_BYTES must be <= 45000000'
    [[ $BACKUP_DIR == /* && $BITWARDEN_DIR == /* ]] || die 'Use absolute paths'
    BACKUP_DIR=$(realpath -m -- "$BACKUP_DIR")
    if [[ $MODE != resend ]]; then
        BITWARDEN_DIR=$(realpath -e -- "$BITWARDEN_DIR")
        [[ -d $BITWARDEN_DIR/bwdata && ! -L $BITWARDEN_DIR/bwdata ]] || die 'bwdata must be a real directory, not a symlink'
        BWDATA=$(realpath -e -- "$BITWARDEN_DIR/bwdata")
        [[ $BACKUP_DIR != "$BWDATA" && $BACKUP_DIR != "$BWDATA/"* ]] || die 'BACKUP_DIR must be outside bwdata'
    fi
    if [[ ! -e $BACKUP_DIR ]]; then mkdir -m 700 -p -- "$BACKUP_DIR"; fi
    [[ -d $BACKUP_DIR ]] && private_path "$BACKUP_DIR" || die 'BACKUP_DIR must be root-owned with mode 0700'
    exec 9> "$BACKUP_DIR/.lock"
    flock -n 9 || die 'Another backup/check/resend is running'
    WORK=$(mktemp -d "$BACKUP_DIR/.work.XXXXXXXX")
    printf 'key validation\n' | age --encrypt -r "$AGE_RECIPIENT" -o "$WORK/key-check.age" >/dev/null 2>&1 || die 'Invalid age recipient'
    API_READY=1
    telegram getMe || die 'Telegram bot is unavailable'
    telegram getChat || die 'Telegram chat is unavailable; send /start to your bot first'
    jq -e --arg id "$TELEGRAM_CHAT_ID" '.result.type == "private" and (.result.id | tostring) == $id' \
        "$WORK/response.json" >/dev/null || die 'Target must be your private chat with the bot'
    # A resend needs neither the Docker daemon nor a working Bitwarden account.
    [[ $MODE != resend ]] || return 0
    [[ -x $BITWARDEN_DIR/bitwarden.sh ]] || die 'Missing executable bitwarden.sh'
    local account_uid
    account_uid=$(id -u -- "$BITWARDEN_USER" 2>/dev/null) || die 'BITWARDEN_USER does not exist'
    [[ $account_uid != 0 ]] || die 'BITWARDEN_USER must be a non-root account'
    runuser -u "$BITWARDEN_USER" -- test -x "$BITWARDEN_DIR/bitwarden.sh" || die 'Bitwarden user cannot execute bitwarden.sh'
    docker info >/dev/null 2>&1 || die 'Docker is unavailable'
    runuser -u "$BITWARDEN_USER" -- docker info >/dev/null 2>&1 || die 'Bitwarden user cannot access Docker'
    runuser -u "$BITWARDEN_USER" -- docker compose version >/dev/null 2>&1 || die 'Docker Compose v2 is required'
    COMPOSE=(docker compose -f "$BWDATA/docker/docker-compose.yml")
    if [[ -f $BWDATA/docker/docker-compose.override.yml ]]; then
        COMPOSE+=(-f "$BWDATA/docker/docker-compose.override.yml")
    fi
    (cd "$BITWARDEN_DIR" && runuser -u "$BITWARDEN_USER" -- "${COMPOSE[@]}" config --format json) \
        > "$WORK/compose.json" 2>/dev/null || die 'Cannot resolve Compose configuration'
    jq -e '.services.mssql.container_name and .services.nginx.container_name and
        all(.services[] | select((.profiles // []) | length == 0); .container_name != null)' \
        "$WORK/compose.json" >/dev/null || die 'Expected standard Bitwarden Compose services with container_name'
    while IFS= read -r value; do CONTAINERS+=("$value"); done < <(
        jq -r '.services[] | select((.profiles // []) | length == 0) | .container_name' "$WORK/compose.json")
    containers_ready || die 'Bitwarden must already be running and healthy; refusing to change a stopped/broken instance'
    # Inspect effective runtime settings, not a possibly stale/commented env file.
    jq -e '[.[].Config.Env[]? | select(test("^globalSettings__sqlServer__connectionString="; "i"))] as $connections |
        ($connections | length > 0) and
        all($connections[]; test("Data Source=tcp:mssql,1433([;[:space:]]|$)"; "i"))' \
        "$WORK/containers.json" >/dev/null || die 'Only the built-in MSSQL database is supported'
    local mssql_name
    mssql_name=$(jq -r '.services.mssql.container_name' "$WORK/compose.json")
    jq -e --arg name "/$mssql_name" '[.[] | select(.Name == $name) | .Mounts[] |
        select(.Type == "bind" and (.Destination == "/var/opt/mssql" or .Destination == "/var/opt/mssql/data"))] |
        length > 0' "$WORK/containers.json" >/dev/null || die 'MSSQL data directory must be bind-mounted inside bwdata'
    # Reject persistent data outside bwdata, including named/anonymous Docker volumes.
    jq -e --arg dir "$BWDATA" 'all(.[].Mounts[];
        .Type == "tmpfs" or
        (.Type == "bind" and ((.Source == $dir or (.Source | startswith($dir + "/"))) or
            (.RW == false and (.Source == "/etc/localtime" or .Source == "/etc/timezone")))))' \
        "$WORK/containers.json" >/dev/null || die 'Container data mounts outside bwdata are unsupported'
    local mount_source resolved
    while IFS= read -r mount_source; do
        [[ $mount_source != /etc/localtime && $mount_source != /etc/timezone ]] || continue
        resolved=$(realpath -e -- "$mount_source")
        [[ $resolved == "$BWDATA" || $resolved == "$BWDATA/"* ]] || die 'A container mount resolves outside bwdata'
    done < <(jq -r '.[].Mounts[] | select(.Type == "bind") | .Source' "$WORK/containers.json")
    SERVICE_STATE='запущен, контейнеры готовы'
    # Conservatively allow space for ciphertext + upload parts + safety margin.
    local source_bytes available required
    source_bytes=$(du -sb -- "$BWDATA"); source_bytes=${source_bytes%%[[:space:]]*}
    available=$(df -B1 --output=avail -- "$BACKUP_DIR" | tail -n 1); available=${available//[[:space:]]/}
    required=$((source_bytes * 2 + 104857600))
    ((available >= required)) || die 'Insufficient disk space (need twice the apparent bwdata size plus 100 MiB)'
    log 'Preflight passed'
}

assert_stopped() {
    local names name
    names=$(docker ps --format '{{.Names}}') || return 1
    for name in "${CONTAINERS[@]}"; do
        if grep -Fxq -- "$name" <<< "$names"; then return 1; fi
    done
}

create_archive() {
    STAGE=initial-notification
    notify 'Начинаю резервное копирование. Останавливаю Bitwarden.' || die 'Initial notification failed; Bitwarden was not stopped'
    RUN_DIR="$BACKUP_DIR/$RUN_ID"
    mkdir -m 700 -- "$RUN_DIR"
    STAGE=stop
    DOWN_STARTED=$SECONDS
    RECOVER=1 # Set BEFORE stop: the command may fail after partially stopping services.
    SERVICE_STATE='остановка началась; требуется запуск'
    bw_command stop || die 'bitwarden.sh stop failed'
    assert_stopped || die 'Some Bitwarden containers are still running; archive cancelled'
    SERVICE_STATE='остановлен'
    STAGE=archive
    local archive="bwdata-$RUN_ID.tar.gz.age"
    # Preserve ACLs, xattrs, numeric owners, hard links and symlinks; never dereference.
    # A separate process group lets the signal handler cancel ALL pipeline
    # processes before bringing writers back online. Waiting on a background
    # process makes Bash handle TERM immediately, even during a long archive.
    setsid --wait bash -c '
        set -o pipefail
        tar --create --gzip --sparse --acls --xattrs --numeric-owner \
            --directory "$1" bwdata |
            age --encrypt -r "$2" |
            tee "$3" | sha256sum > "$4"
    ' _ "$BITWARDEN_DIR" "$AGE_RECIPIENT" "$RUN_DIR/$archive.partial" "$RUN_DIR/archive.sha256.partial" 2>/dev/null &
    ARCHIVE_PID=$!
    wait "$ARCHIVE_PID"
    ARCHIVE_PID=0
    mv -- "$RUN_DIR/$archive.partial" "$RUN_DIR/$archive"
    mv -- "$RUN_DIR/archive.sha256.partial" "$RUN_DIR/.archive.sha256"
    ARCHIVE_READY=1
    STAGE=start
    restore_service || die 'Failed to restore Bitwarden readiness'
    STAGE=service-notification
    if ! notify "Bitwarden запущен и готов к работе. Начинаю отправку архива. Простой: ${DOWNTIME} с."; then
        NOTIFY_FAILED=1
        log 'Service notification failed; continuing with archive delivery'
    fi
}

prepare_manifest() {
    STAGE=manifest
    local archive="$RUN_DIR/bwdata-$RUN_ID.tar.gz.age" bytes hash file name part_hash part_bytes
    bytes=$(stat -c %s -- "$archive")
    hash=$(sha256sum -- "$archive"); hash=${hash%% *}
    FILES=()
    if ((bytes > PART_BYTES)); then
        # A retry recreates any interrupted split, always from the complete ciphertext.
        rm -f -- "$archive".part[0-9]*
        split --bytes="$PART_BYTES" --numeric-suffixes=1 --suffix-length=6 -- "$archive" "$archive.part"
        FILES=("$archive".part[0-9]*)
    else
        FILES=("$archive")
    fi
    : > "$WORK/parts.jsonl"
    for file in "${FILES[@]}"; do
        name=$(basename "$file")
        part_bytes=$(stat -c %s -- "$file")
        part_hash=$(sha256sum -- "$file"); part_hash=${part_hash%% *}
        jq -cn --arg name "$name" --arg sha256 "$part_hash" --argjson bytes "$part_bytes" \
            '{name:$name, bytes:$bytes, sha256:$sha256}' >> "$WORK/parts.jsonl"
    done
    jq -s --arg id "$RUN_ID" --arg archive "$(basename "$archive")" --arg sha256 "$hash" \
        --argjson bytes "$bytes" '{version:1, backup_id:$id, archive:$archive,
        archive_bytes:$bytes, archive_sha256:$sha256, parts:.}' "$WORK/parts.jsonl" > "$RUN_DIR/manifest.json.partial"
    mv -- "$RUN_DIR/manifest.json.partial" "$RUN_DIR/manifest.json"
}

validate_existing() {
    [[ -d $EXISTING && ! -L $EXISTING ]] || die 'Existing backup must be a real directory'
    RUN_DIR=$(realpath -e -- "$EXISTING")
    [[ ${RUN_DIR%/*} == "$BACKUP_DIR" ]] || die 'Existing backup must be a direct child of BACKUP_DIR'
    private_path "$RUN_DIR" || die 'Existing backup directory must be private and root-owned'
    RUN_ID=$(basename "$RUN_DIR")
    [[ $RUN_ID =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || die 'Invalid backup directory name'
    local archive="$RUN_DIR/bwdata-$RUN_ID.tar.gz.age" expected actual
    [[ -f $archive && ! -L $archive ]] && private_path "$archive" || die 'Complete encrypted archive is missing or insecure'
    ARCHIVE_READY=1
    # The streaming digest also permits a resend after failure before manifest
    # generation (for example, service startup failure). Never re-bless corruption.
    if [[ -e $RUN_DIR/manifest.json ]]; then
        [[ -f $RUN_DIR/manifest.json ]] && private_path "$RUN_DIR/manifest.json" || die 'Insecure manifest'
        jq -e --arg id "$RUN_ID" --arg name "$(basename "$archive")" '
            .version == 1 and .backup_id == $id and .archive == $name and
            (.archive_sha256 | test("^[0-9a-f]{64}$")) and (.archive_bytes | type == "number")
        ' "$RUN_DIR/manifest.json" >/dev/null || die 'Invalid manifest'
        expected=$(jq -r .archive_sha256 "$RUN_DIR/manifest.json")
        [[ $(stat -c %s -- "$archive") == "$(jq -r .archive_bytes "$RUN_DIR/manifest.json")" ]] || die 'Existing archive size mismatch'
    else
        [[ -f $RUN_DIR/.archive.sha256 ]] && private_path "$RUN_DIR/.archive.sha256" || die 'Original archive digest is missing or insecure'
        read -r expected _ < "$RUN_DIR/.archive.sha256"
        [[ $expected =~ ^[0-9a-f]{64}$ ]] || die 'Invalid original archive digest'
    fi
    actual=$(sha256sum -- "$archive"); actual=${actual%% *}
    [[ $actual == "$expected" ]] || die 'Existing archive checksum mismatch'
}

deliver() {
    STAGE=upload
    local file bytes count
    for file in "${FILES[@]}"; do
        send_document "$file" || die 'Archive upload failed; local archive retained'
    done
    send_document "$RUN_DIR/manifest.json" || die 'Manifest upload failed; backup is not marked delivered'
    date -u +%FT%TZ > "$RUN_DIR/.delivered.partial"
    mv -- "$RUN_DIR/.delivered.partial" "$RUN_DIR/.delivered"
    bytes=$(jq -r .archive_bytes "$RUN_DIR/manifest.json")
    count=${#FILES[@]}
    STAGE=final-notification
    if [[ $MODE == resend ]]; then
        notify "Повторная отправка завершена. Архив: $bytes байт, файлов: $count. Длительность: $((SECONDS - STARTED)) с. Состояние Bitwarden не изменялось." || die 'Final notification failed'
    else
        notify "Бэкап завершён. Архив: $bytes байт, файлов: $count. Длительность: $((SECONDS - STARTED)) с. Bitwarden: $SERVICE_STATE. Простой: ${DOWNTIME} с." || die 'Final notification failed'
    fi
    ((NOTIFY_FAILED == 0)) || die 'Files delivered, but a required notification failed'
    STAGE=retention
    cleanup_old
    log 'Backup delivered successfully'
}

cleanup_old() {
    local dir marker modified cutoff
    cutoff=$(( $(date +%s) - RETENTION_DAYS * 86400 ))
    for dir in "$BACKUP_DIR"/*; do
        [[ -d $dir && ! -L $dir && $dir != "$RUN_DIR" ]] || continue
        [[ $(basename "$dir") =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || continue
        marker="$dir/.delivered"
        [[ -f $marker && ! -L $marker ]] || continue
        private_path "$dir" || continue
        modified=$(stat -c %Y -- "$marker")
        if ((modified < cutoff)); then
            rm -rf -- "$dir"
            log "Removed expired delivered backup $(basename "$dir")"
        fi
    done
}

preflight
if [[ $MODE == check ]]; then
    log 'Check complete; no notifications sent and service unchanged'
    exit 0
elif [[ $MODE == resend ]]; then
    STAGE=verify-existing
    validate_existing
    notify 'Начинаю повторную отправку готового бэкапа. Состояние Bitwarden не изменяется.' || die 'Resend notification failed'
else
    create_archive
fi
prepare_manifest
deliver
