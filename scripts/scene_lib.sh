#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCENES_CONF="${ROOT_DIR}/scripts/scenes.conf"
PYTHON_BIN="${PYTHON_BIN:-${HOME}/miniconda3/envs/py312/bin/python}"

die() {
  echo "error: $*" >&2
  exit 1
}

norm() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

resolve_scene() {
  local wanted
  wanted="$(norm "$1")"

  while IFS='|' read -r name aliases scene_dir compose_file cdc_config health_url timeout; do
    case "${name:-}" in
      ''|'#'*) continue ;;
    esac

    if [ "$(norm "$name")" = "$wanted" ]; then
      export SCENE_NAME="$name"
      export SCENE_ALIASES="$aliases"
      export SCENE_DIR="${ROOT_DIR}/${scene_dir}"
      export COMPOSE_FILE="$compose_file"
      export CDC_CONFIG="$cdc_config"
      export HEALTH_URL="$health_url"
      export HEALTH_TIMEOUT="$timeout"
      return 0
    fi

    local alias
    IFS=',' read -ra alias_list <<< "$aliases"
    for alias in "${alias_list[@]}"; do
      if [ "$(norm "$alias")" = "$wanted" ]; then
        export SCENE_NAME="$name"
        export SCENE_ALIASES="$aliases"
        export SCENE_DIR="${ROOT_DIR}/${scene_dir}"
        export COMPOSE_FILE="$compose_file"
        export CDC_CONFIG="$cdc_config"
        export HEALTH_URL="$health_url"
        export HEALTH_TIMEOUT="$timeout"
        return 0
      fi
    done
  done < "$SCENES_CONF"

  return 1
}

require_scene() {
  resolve_scene "$1" || die "unknown scene '$1', run: ./run_scene.sh list"
  [ -d "$SCENE_DIR" ] || die "scene dir not found: $SCENE_DIR"
  [ -f "${SCENE_DIR}/${COMPOSE_FILE}" ] || die "compose file not found: ${SCENE_DIR}/${COMPOSE_FILE}"
  [ -f "${ROOT_DIR}/cdc_listener/config_${CDC_CONFIG}.json" ] || die "CDC config not found: config_${CDC_CONFIG}.json"
}

list_scenes() {
  printf '%-18s %-34s %s\n' "scene" "directory" "health"
  while IFS='|' read -r name aliases scene_dir compose_file cdc_config health_url timeout; do
    case "${name:-}" in
      ''|'#'*) continue ;;
    esac
    printf '%-18s %-34s %s\n' "$name" "$scene_dir" "$health_url"
  done < "$SCENES_CONF"
}

compose() {
  (cd "$SCENE_DIR" && docker compose -f "$COMPOSE_FILE" "$@")
}

run_dir_for() {
  local experiment="$1"
  local scene="$2"
  printf '%s/runs/%s/%s' "$ROOT_DIR" "$experiment" "$scene"
}

attack_script_for_scene() {
  if [ -n "${ATTACK_SCRIPT:-}" ] && [ -f "${ATTACK_SCRIPT}" ]; then
    printf '%s' "$ATTACK_SCRIPT"
    return
  fi
  case "$SCENE_NAME" in
    gitlab14.1) printf '%s/attack_xss.py' "$SCENE_DIR" ;;
    *) printf '%s/attack.py' "$SCENE_DIR" ;;
  esac
}

locust_dir_for_scene() {
  case "$SCENE_NAME" in
    airflow) printf '%s/examples_locust/airflow' "$ROOT_DIR" ;;
    self_fastapi) printf '%s/examples_locust/self_fastapi' "$ROOT_DIR" ;;
    superset) printf '%s/examples_locust/superset' "$ROOT_DIR" ;;
    pgadmin) printf '%s/examples_locust/pgadmin' "$ROOT_DIR" ;;
    django) printf '%s/examples_locust/django' "$ROOT_DIR" ;;
    admidio) printf '%s/examples_locust/admidio5.0.5' "$ROOT_DIR" ;;
    kanboard) printf '%s/examples_locust/kanboard' "$ROOT_DIR" ;;
    craftcms) printf '%s/examples_locust/craftcms' "$ROOT_DIR" ;;
    joomla) printf '%s/examples_locust/joomla' "$ROOT_DIR" ;;
    confluence) printf '%s/examples_locust/confluence' "$ROOT_DIR" ;;
    dolphinscheduler) printf '%s/examples_locust/dolphin_scheduler' "$ROOT_DIR" ;;
    ofbiz) printf '%s/examples_locust/ofbiz' "$ROOT_DIR" ;;
    flowable) printf '%s/examples_locust/flowable' "$ROOT_DIR" ;;
    geoserver) printf '%s/examples_locust/geoserver' "$ROOT_DIR" ;;
    gitlab8.13) printf '%s/examples_locust/gitlab8.13' "$ROOT_DIR" ;;
    gitlab14.1) printf '%s/examples_locust/gitlab14.1' "$ROOT_DIR" ;;
    moodle) printf '%s/examples_locust/moodle' "$ROOT_DIR" ;;
    *) return 1 ;;
  esac
}

scene_read_log() {
  printf '%s/logs/request_db_read.log' "$SCENE_DIR"
}

scene_request_context_log() {
  case "$SCENE_NAME" in
    superset|admidio|airflow|pgadmin|kanboard|gitlab8.13|gitlab14.1|ofbiz|geoserver|flowable) printf '%s/logs/request_context.log' "$SCENE_DIR" ;;
    moodle) printf '%s/moodle_data/request_context.log' "$SCENE_DIR" ;;
    dolphinscheduler) printf '%s/db_logs/request_context.log' "$SCENE_DIR" ;;
    *) return 1 ;;
  esac
}

clear_scene_request_context_log() {
  local log_file
  local log_dir
  log_file="$(scene_request_context_log)" || return 0
  log_dir="$(dirname "$log_file")"
  [ -d "$log_dir" ] || return 0
  if (: > "$log_file") 2>/dev/null; then
    chmod 666 "$log_file" 2>/dev/null || true
    return 0
  fi
  docker run --rm \
    -v "$log_dir:/logs" \
    pg-request-logger:13.12 \
    sh -c ': > /logs/request_context.log && chmod 666 /logs/request_context.log' \
    >/dev/null 2>&1 || true
}

sync_scene_request_context_log() {
  local run_dir="$1"
  local log_file
  log_file="$(scene_request_context_log)" || return 0
  [ -f "$log_file" ] || return 0
  if cp "$log_file" "${run_dir}/request_context.log" 2>/dev/null; then
    return 0
  fi
  docker run --rm \
    -v "$(dirname "$log_file"):/src:ro" \
    -v "${run_dir}:/dst" \
    pg-request-logger:13.12 \
    sh -c 'cp /src/request_context.log /dst/request_context.log && chmod 666 /dst/request_context.log' \
    >/dev/null 2>&1 || true
}

scene_read_log_candidates() {
  local dir
  local name
  for dir in logs db_logs moodle_data; do
    for name in request_db_read.log db_read.log read_provenance.log db_access_audit.log; do
      printf '%s/%s/%s\n' "$SCENE_DIR" "$dir" "$name" 2>/dev/null || return 0
    done
  done
}

prepare_dir_writable() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  chmod -R 777 "$dir" 2>/dev/null && return 0
  docker run --rm -v "${dir}:/target" pg-request-logger:13.12 \
    sh -c 'chmod -R 777 /target' >/dev/null 2>&1 || true
}

prepare_scene_read_log_dirs() {
  local log_file
  local log_dir
  local seen="|"

  while IFS= read -r log_file; do
    log_dir="$(dirname "$log_file")"
    case "$seen" in
      *"|$log_dir|"*) continue ;;
    esac
    seen="${seen}${log_dir}|"
    prepare_dir_writable "$log_dir"
  done < <(scene_read_log_candidates)
}

clear_scene_read_log() {
  local log_file
  local log_dir
  local canonical
  canonical="$(scene_read_log)"
  while IFS= read -r log_file; do
    log_dir="$(dirname "$log_file")"
    [ -d "$log_dir" ] || continue
    if [ "$log_file" != "$canonical" ] && [ ! -f "$log_file" ]; then
      continue
    fi
    if (: > "$log_file") 2>/dev/null; then
      chmod 666 "$log_file" 2>/dev/null || true
      continue
    fi
    docker run --rm \
      -v "$log_dir:/logs" \
      pg-request-logger:13.12 \
      sh -c ': > "/logs/$1" && chmod 666 "/logs/$1"' \
      sh "$(basename "$log_file")" >/dev/null 2>&1 || true
  done < <(scene_read_log_candidates)
}

sync_scene_read_log() {
  local run_dir="$1"
  local log_file
  mkdir -p "$run_dir"

  log_file=""
  while IFS= read -r candidate; do
    if [ -s "$candidate" ]; then
      log_file="$candidate"
      break
    fi
  done < <(scene_read_log_candidates)

  if [ -z "$log_file" ]; then
    while IFS= read -r candidate; do
      if [ -f "$candidate" ]; then
        log_file="$candidate"
        break
      fi
    done < <(scene_read_log_candidates)
  fi

  if [ ! -f "$log_file" ]; then
    : > "${run_dir}/request_db_read.log"
    return 0
  fi

  if cp "$log_file" "${run_dir}/request_db_read.log" 2>/dev/null; then
    return 0
  fi

  docker run --rm \
    -v "$(dirname "$log_file"):/src:ro" \
    -v "${run_dir}:/dst" \
    pg-request-logger:13.12 \
    sh -c 'cp "/src/$1" /dst/request_db_read.log && chmod 666 /dst/request_db_read.log' \
    sh "$(basename "$log_file")" \
    >/dev/null 2>&1 || true
}

health_url_for_run() {
  local ts
  ts="$(date +%s)"
  printf '%s' "${HEALTH_URL//\{\{ts\}\}/$ts}"
}

wait_for_health() {
  local url="$1"
  local timeout="$2"
  local deadline
  local code

  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -le "$deadline" ]; do
    code="$(curl -k -L -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
      echo "health ok: $url"
      return 0
    fi
    if [ "$code" = "000" ] || [ -z "$code" ]; then
      echo "health waiting: service not ready, url=${url}"
    else
      echo "health waiting: http=${code}, url=${url}"
    fi
    sleep 5
  done

  echo "health failed after ${timeout}s: $url" >&2
  return 1
}

kill_pidfile() {
  local pidfile="$1"
  local pid

  [ -f "$pidfile" ] || return 0
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] || { rm -f "$pidfile"; return 0; }

  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
  fi
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
}

# Backward-compatible alias.
kill_cdc_pidfile() {
  kill_pidfile "$1"
}

# Wait for the process referenced by a pidfile to exit naturally, then return on timeout without killing it.
wait_pidfile_done() {
  local pidfile="$1"
  local max="${2:-180}"
  local pid
  local waited=0

  [ -f "$pidfile" ] || return 0
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] || return 0

  while kill -0 "$pid" 2>/dev/null; do
    [ "$waited" -ge "$max" ] && return 0
    sleep 3
    waited=$((waited + 3))
  done
}

# Wait until the CDC write log size is stable so WAL changes are fully flushed to disk.
wait_write_log_stable() {
  local file="$1"
  local max_wait="${CDC_STABLE_MAX_WAIT:-60}"
  local interval="${CDC_STABLE_INTERVAL:-2}"
  local stable_need="${CDC_STABLE_ROUNDS:-3}"
  local waited=0
  local stable=0
  local last_size=-1
  local size=-1

  sleep "${CDC_SETTLE_SECONDS:-10}"
  while [ "$waited" -lt "$max_wait" ]; do
    if [ -f "$file" ]; then
      size="$(stat -c '%s' "$file" 2>/dev/null || echo 0)"
    else
      size=-1
    fi

    if [ "$size" = "$last_size" ] && [ "$size" -ge 0 ]; then
      stable=$((stable + 1))
      if [ "$stable" -ge "$stable_need" ]; then
        return 0
      fi
    else
      stable=0
      last_size="$size"
    fi

    sleep "$interval"
    waited=$((waited + interval))
  done
}

# Copy the Locust-generated request_log directory into the experiment directory.
sync_locust_request_log() {
  local run_dir="$1"
  local locust_dir
  locust_dir="$(locust_dir_for_scene)" || return 0
  if [ -d "${locust_dir}/request_log" ]; then
    rm -rf "${run_dir}/locust_request_log"
    cp -a "${locust_dir}/request_log" "${run_dir}/locust_request_log" 2>/dev/null || true
  fi
}
