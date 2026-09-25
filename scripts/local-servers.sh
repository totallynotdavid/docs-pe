#!/usr/bin/env bash
# Sourced by the db, objects, dev, and reset tasks in mise.toml, which run under
# bash and export the PORTAL_DEV_* ports. A checkout owns a server only when
# its own .data directory says so; whatever else answers on a port is foreign
# and is never used.

postgres_dir=$PWD/.data/postgres
minio_dir=$PWD/.data/minio
minio_pid_file=$PWD/.data/minio.pid

# A connect and close, so nothing is written to whatever holds the port.
port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

require_free_port() {
  if port_in_use "$1"; then
    echo "port $1 ($2) is taken by another process" >&2
    exit 1
  fi
}

# postmaster.pid records the port the server was started with, so a cluster of
# this checkout left on another port does not count.
# The command line is checked, not only the pid, because a stale
# postmaster.pid can name a recycled pid that pg_ctl status accepts.
postgres_running() {
  [ -f "$postgres_dir/postmaster.pid" ] &&
    ps -p "$(sed -n 1p "$postgres_dir/postmaster.pid")" -o args= 2>/dev/null |
    grep -qF -- " -D $postgres_dir "
}

postgres_owned() {
  [ -f "$postgres_dir/postmaster.pid" ] &&
    [ "$(sed -n 4p "$postgres_dir/postmaster.pid")" = "$PORTAL_DEV_POSTGRES_PORT" ] &&
    ps -p "$(sed -n 1p "$postgres_dir/postmaster.pid")" -o args= 2>/dev/null |
    grep -qF -- " -D $postgres_dir -h 127.0.0.1 -p $PORTAL_DEV_POSTGRES_PORT "
}

# The command line is checked as well as the pid, because a recycled pid must
# not pass as this checkout's server.
minio_running() {
  [ -f "$minio_pid_file" ] &&
    ps -p "$(<"$minio_pid_file")" -o args= 2>/dev/null |
    grep -qF -- "server $minio_dir "
}

# A server of this checkout on other ports (the checkout moved and its slot
# changed) is running but not usable.
minio_owned() {
  minio_running &&
    ps -p "$(<"$minio_pid_file")" -o args= 2>/dev/null |
    grep -qF -- " --address 127.0.0.1:$PORTAL_DEV_MINIO_PORT "
}

# Sets minio_pid. The binary is run directly, not through `mise x`, so the
# recorded pid is the server itself.
start_minio() {
  mkdir -p "$minio_dir"

  MINIO_ROOT_USER=portal-dev MINIO_ROOT_PASSWORD=portal-dev-secret \
    nohup "$(mise which minio)" server "$minio_dir" \
    --address "127.0.0.1:$PORTAL_DEV_MINIO_PORT" \
    --console-address "127.0.0.1:$PORTAL_DEV_MINIO_CONSOLE_PORT" \
    >"$minio_dir/server.log" 2>&1 &
  minio_pid=$!
  echo "$minio_pid" >"$minio_pid_file"
}

stop_minio() {
  if minio_running; then
    kill "$(<"$minio_pid_file")"

    while minio_running; do
      sleep 0.1
    done
  fi

  rm -f "$minio_pid_file"
}

minio_ready() {
  curl -sf "http://127.0.0.1:$PORTAL_DEV_MINIO_PORT/minio/health/live" >/dev/null 2>&1
}
