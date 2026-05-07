#!/bin/sh

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)"

ENV_FILE="${PYCHFPGA_ENV_FILE:-${REPO_ROOT}/.env}"
if [ -f "${ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
    set +a
fi

PROJECT_NAME_DEFAULT="$(basename "${REPO_ROOT}")"
PROJECT_NAME="${WATCHDOG_PROJECT_NAME:-${COMPOSE_PROJECT_NAME:-}}"
DATA_DIR="${PYCHFPGA_DATA_DIR:-./docker-data}"
case "${DATA_DIR}" in
    /*) ;;
    *) DATA_DIR="${REPO_ROOT}/${DATA_DIR}" ;;
esac

LOGS_ROOT="${WATCHDOG_LOGS_ROOT:-${DATA_DIR}/swartfontein-data}"
LOG_FILE="${WATCHDOG_LOG_FILE:-${LOGS_ROOT}/current/fpga_master_debug.log}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/tmp/fpga-watchdog}"
STATE_FILE="${STATE_DIR}/state"
ACTION_LOG="${WATCHDOG_ACTION_LOG:-${LOGS_ROOT}/current/fpga_watchdog.log}"
FALLBACK_ACTION_LOG="${STATE_DIR}/actions.log"
LOCK_DIR="${STATE_DIR}/lock"

ERROR_REGEX="${WATCHDOG_ERROR_REGEX:-Raising exception after 10 unsuccessful trials|Unhandled error while[[:space:]]+getting FPGA crossbar metrics|a bytes-like object is required, not 'str'}"
RESTART_DELAY_SEC="${WATCHDOG_RESTART_DELAY_SEC:-60}"
COOLDOWN_SEC="${WATCHDOG_COOLDOWN_SEC:-20}"
RAW_HEALTH_TIMEOUT_SEC="${WATCHDOG_RAW_HEALTH_TIMEOUT_SEC:-90}"
DRY_RUN="${WATCHDOG_DRY_RUN:-0}"
SCAN_FROM_START="${WATCHDOG_SCAN_FROM_START:-0}"

TMP_FILE=""

mkdir -p "${STATE_DIR}"
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    exit 0
fi

cleanup() {
    if [ -n "${TMP_FILE}" ] && [ -f "${TMP_FILE}" ]; then
        rm -f "${TMP_FILE}" || true
    fi
    rmdir "${LOCK_DIR}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

ts() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
    line="$(ts) $*"
    action_log_dir="$(dirname "${ACTION_LOG}")"
    if [ -d "${action_log_dir}" ] && [ -w "${action_log_dir}" ]; then
        printf '%s\n' "${line}" >> "${ACTION_LOG}"
    else
        printf '%s\n' "${line}" >> "${FALLBACK_ACTION_LOG}" 2>/dev/null || true
    fi
    printf '%s\n' "${line}"
}

detect_project_name() {
    if [ -n "${PROJECT_NAME}" ]; then
        return
    fi

    self_id="${HOSTNAME:-}"
    if [ -n "${self_id}" ]; then
        detected="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${self_id}" 2>/dev/null || true)"
        if [ -n "${detected}" ] && [ "${detected}" != "<no value>" ]; then
            PROJECT_NAME="${detected}"
            log "watchdog: detected compose project name from self container label: ${PROJECT_NAME}"
            return
        fi
    fi

    PROJECT_NAME="${PROJECT_NAME_DEFAULT}"
    log "watchdog: using fallback compose project name: ${PROJECT_NAME}"
}

pick_latest_log() {
    base_dir="$(dirname "${LOG_FILE}")"
    base_file="$(basename "${LOG_FILE}")"

    # Prefer the newest rotated candidate in the current run folder.
    # This handles rollovers like fpga_master_debug.log -> .1/.2/...
    newest_in_base_dir="$(ls -1t "${base_dir}/${base_file}"* 2>/dev/null | head -n 1 || true)"
    if [ -n "${newest_in_base_dir}" ]; then
        printf '%s\n' "${newest_in_base_dir}"
        return 0
    fi

    newest_run_dir="$(ls -1dt "${LOGS_ROOT}"/*/ 2>/dev/null | head -n 1 || true)"
    if [ -z "${newest_run_dir}" ]; then
        return 1
    fi

    newest_log="$(ls -1t "${newest_run_dir}"/fpga_master_debug.log* 2>/dev/null | head -n 1 || true)"
    if [ -z "${newest_log}" ]; then
        return 1
    fi

    printf '%s\n' "${newest_log}"
}

read_state() {
    LAST_FILE=""
    LAST_LINE="0"
    LAST_RESTART_EPOCH="0"
    STATE_EXISTS="0"

    if [ -f "${STATE_FILE}" ]; then
        STATE_EXISTS="1"
        LAST_FILE="$(sed -n '1p' "${STATE_FILE}" 2>/dev/null || true)"
        LAST_LINE="$(sed -n '2p' "${STATE_FILE}" 2>/dev/null || true)"
        LAST_RESTART_EPOCH="$(sed -n '3p' "${STATE_FILE}" 2>/dev/null || true)"
    fi

    case "${LAST_LINE}" in
        ''|*[!0-9]*) LAST_LINE="0" ;;
    esac
    case "${LAST_RESTART_EPOCH}" in
        ''|*[!0-9]*) LAST_RESTART_EPOCH="0" ;;
    esac
}

write_state() {
    printf '%s\n%s\n%s\n' "$1" "$2" "$3" > "${STATE_FILE}"
}

get_container_id() {
    service_name="$1"
    ids=""
    cid=""
    count="0"

    if [ -n "${PROJECT_NAME}" ]; then
        ids="$(docker ps -a \
            --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
            --filter "label=com.docker.compose.service=${service_name}" \
            --format '{{.ID}}' 2>/dev/null || true)"
        cid="$(printf '%s\n' "${ids}" | sed -n '1p')"
    fi

    if [ -z "${cid}" ]; then
        ids="$(docker ps -a \
            --filter "label=com.docker.compose.service=${service_name}" \
            --format '{{.ID}}' 2>/dev/null || true)"
        cid="$(printf '%s\n' "${ids}" | sed -n '1p')"
        if [ -n "${cid}" ]; then
            log "watchdog: fallback container lookup by service label only for service=${service_name}"
        fi
    fi

    count="$(printf '%s\n' "${ids}" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
    if [ "${count}" -gt 1 ]; then
        log "watchdog: multiple containers matched service=${service_name}; using first match ${cid}"
    fi

    printf '%s\n' "${cid}"
}

wait_raw_ready() {
    raw_cid="$1"
    waited=0
    while [ "${waited}" -lt "${RAW_HEALTH_TIMEOUT_SEC}" ]; do
        raw_status="$(docker inspect -f '{{.State.Status}}' "${raw_cid}" 2>/dev/null || true)"
        raw_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${raw_cid}" 2>/dev/null || true)"
        if [ "${raw_status}" = "running" ] && { [ "${raw_health}" = "healthy" ] || [ "${raw_health}" = "none" ]; }; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

restart_stack() {
    detect_project_name
    raw_cid="$(get_container_id raw_acq)"
    fpga_cid="$(get_container_id fpga_master)"

    if [ -z "${raw_cid}" ] || [ -z "${fpga_cid}" ]; then
        log "watchdog: container lookup failed (project=${PROJECT_NAME}, raw_acq='${raw_cid}', fpga_master='${fpga_cid}')"
        return 1
    fi

    if [ "${DRY_RUN}" = "1" ]; then
        log "watchdog: DRY RUN would stop fpga_master=${fpga_cid} raw_acq=${raw_cid}, wait ${RESTART_DELAY_SEC}s, then start raw_acq then fpga_master"
        return 0
    fi

    log "watchdog: stopping fpga_master=${fpga_cid} raw_acq=${raw_cid}"
    docker stop "${fpga_cid}" "${raw_cid}" >/dev/null
    log "watchdog: waiting ${RESTART_DELAY_SEC}s before restart"
    sleep "${RESTART_DELAY_SEC}"

    log "watchdog: starting raw_acq=${raw_cid}"
    docker start "${raw_cid}" >/dev/null

    if wait_raw_ready "${raw_cid}"; then
        log "watchdog: raw_acq is ready"
    else
        log "watchdog: raw_acq was not healthy within ${RAW_HEALTH_TIMEOUT_SEC}s; starting fpga_master anyway"
    fi

    log "watchdog: starting fpga_master=${fpga_cid}"
    docker start "${fpga_cid}" >/dev/null
    return 0
}

LATEST_LOG="$(pick_latest_log || true)"
if [ -z "${LATEST_LOG}" ] || [ ! -f "${LATEST_LOG}" ]; then
    log "watchdog: no fpga_master_debug log found under ${LOGS_ROOT}"
    exit 0
fi

read_state

TOTAL_LINES="$(wc -l < "${LATEST_LOG}" | tr -d '[:space:]')"
case "${TOTAL_LINES}" in
    ''|*[!0-9]*) TOTAL_LINES="0" ;;
esac

if [ "${STATE_EXISTS}" = "0" ] && [ "${SCAN_FROM_START}" != "1" ]; then
    write_state "${LATEST_LOG}" "${TOTAL_LINES}" "0"
    log "watchdog: initialized state at EOF (file=${LATEST_LOG}, line=${TOTAL_LINES})"
    exit 0
fi

if [ "${LATEST_LOG}" = "${LAST_FILE}" ] && [ "${TOTAL_LINES}" -ge "${LAST_LINE}" ]; then
    START_LINE=$((LAST_LINE + 1))
else
    START_LINE=1
fi

if [ "${START_LINE}" -gt "${TOTAL_LINES}" ]; then
    write_state "${LATEST_LOG}" "${TOTAL_LINES}" "${LAST_RESTART_EPOCH}"
    log "watchdog: check complete (file=${LATEST_LOG}, new_lines=0, last_line=${TOTAL_LINES})"
    exit 0
fi

TMP_FILE="${STATE_DIR}/new-lines.$$"
sed -n "${START_LINE},${TOTAL_LINES}p" "${LATEST_LOG}" > "${TMP_FILE}"
NEW_LINES=$((TOTAL_LINES - START_LINE + 1))
log "watchdog: checking file=${LATEST_LOG} lines=${START_LINE}-${TOTAL_LINES} count=${NEW_LINES}"

if grep -Eq "${ERROR_REGEX}" "${TMP_FILE}"; then
    MATCH_LINE="$(grep -E -m 1 "${ERROR_REGEX}" "${TMP_FILE}" || true)"
    NOW_EPOCH="$(date +%s)"
    SINCE_LAST_RESTART=$((NOW_EPOCH - LAST_RESTART_EPOCH))

    if [ "${SINCE_LAST_RESTART}" -lt "${COOLDOWN_SEC}" ]; then
        log "watchdog: matched error but cooldown is active (${SINCE_LAST_RESTART}s < ${COOLDOWN_SEC}s). match='${MATCH_LINE}'"
    else
        log "watchdog: matched error in ${LATEST_LOG}: '${MATCH_LINE}'"
        if restart_stack; then
            LAST_RESTART_EPOCH="${NOW_EPOCH}"
        fi
    fi
else
    log "watchdog: no match for configured pattern"
fi

write_state "${LATEST_LOG}" "${TOTAL_LINES}" "${LAST_RESTART_EPOCH}"
log "watchdog: check complete (file=${LATEST_LOG}, last_line=${TOTAL_LINES}, last_restart_epoch=${LAST_RESTART_EPOCH})"
