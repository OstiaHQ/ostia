# ostia-supervisor: runs a remote run's steps in the pod or container (RFC-0005 §3.2, §4.5).
# POSIX sh (dash in the pixi image); the CLI passes it as `/bin/sh -c <this> ostia-supervisor`.
#
# States: waiting for $OSTIA_WORK/.ostia/ready (at most OSTIA_CODE_WAIT s, else exit 3)
#   -> running the OSTIA_PLAN steps (each `name<TAB>kind<TAB>command`, run by sh -c in its
#      own session; output tee'd to .ostia/log.txt; code and seconds in .ostia/steps.json)
#   -> waiting for .ostia/collected (at most OSTIA_COLLECT_WINDOW s) -> exit with the
#      command's code. A failing preflight or install step is exit 3; a failing build or
#      command step stops the plan with its code; a report step never stops it. After
#      OSTIA_TIMEOUT s the watchdog TERMs the step's group, then KILLs it 10 s later, and
#      the run records `timeout` (exit 3). TERM or INT records `terminated` (exit 3).
# The guards that decide the run's exit code run CLI-side on steps.json and the junit.
set -u

W=${OSTIA_WORK:-/w}
S=$W/.ostia
LOG=$S/log.txt
POLL=${OSTIA_POLL:-2}
CODE_WAIT=${OSTIA_CODE_WAIT:-600}
COLLECT_WINDOW=${OSTIA_COLLECT_WINDOW:-600}
TIMEOUT=${OSTIA_TIMEOUT:-3600}
export OSTIA_SOURCE_DIR="$W"
export OSTIA_BUILD_DIR="${OSTIA_BUILD_DIR:-$W/build/${OSTIA_ENV:-default}/${OSTIA_PRESET:-dev}}"
export CTEST_NO_TESTS_ACTION="${CTEST_NO_TESTS_ACTION:-error}"
export HOME="${HOME:-$W/home}"
TAB=$(printf '\t')

state=waiting
code_wait=pending
exit_code=0
entries=
running=
mkdir -p "$S" "$HOME"
: >>"$LOG"

say() { printf '[ostia] %s\n' "$*" | tee -a "$LOG"; }

write_steps() {
  printf '{"schema": 1, "state": "%s", "code_wait": "%s", "exit": %s, "steps": [%s]}\n' \
    "$state" "$code_wait" "$exit_code" "$entries" >"$S/steps.json.tmp"
  mv "$S/steps.json.tmp" "$S/steps.json"
}

# add_entry name kind code seconds result
add_entry() {
  e=$(printf '{"name": "%s", "kind": "%s", "code": %s, "seconds": %s, "result": "%s"}' \
    "$1" "$2" "$3" "$4" "$5")
  entries="${entries:+$entries, }$e"
}

now() { date +%s; }

kill_group() {
  pg=$(cat "$S/pgid" 2>/dev/null) || return 0
  [ -n "$pg" ] && kill -s "$1" -- "-$pg" 2>/dev/null
  return 0
}

group_alive() {
  pg=$(cat "$S/pgid" 2>/dev/null) || return 1
  [ -n "$pg" ] && kill -s 0 -- "-$pg" 2>/dev/null
}

# stop_group seconds: TERM the running step's group, KILL it if it outlives the wait
stop_group() {
  kill_group TERM
  i=0
  while group_alive && [ "$i" -lt "$1" ]; do sleep 1; i=$((i + 1)); done
  kill_group KILL
}

nap() { sleep "$1" & wait $!; }

on_term() {
  trap '' TERM INT
  : >"$S/.stop-watchdog"
  if [ -n "$running" ]; then
    stop_group 5
    set -- $running
    add_entry "$1" "$2" 143 $(($(now) - $3)) terminated
  fi
  state=terminated
  exit_code=3
  write_steps
  say "terminated"
  exit 3
}
trap on_term TERM INT

write_steps
say "waiting for the code (at most ${CODE_WAIT}s)"
t0=$(now)
while [ ! -e "$S/ready" ]; do
  if [ $(($(now) - t0)) -ge "$CODE_WAIT" ]; then
    code_wait=timeout
    state=code_wait_timeout
    exit_code=3
    write_steps
    say "error: the code never arrived within ${CODE_WAIT}s; the CLI is gone or the upload failed"
    exit 3
  fi
  nap "$POLL"
done
code_wait=ok
state=running
write_steps

rm -f "$S/.timed-out" "$S/.stop-watchdog"
(
  end=$(($(now) + TIMEOUT))
  while [ "$(now)" -lt "$end" ]; do
    [ -e "$S/.stop-watchdog" ] && exit 0
    sleep 1
  done
  : >"$S/.timed-out"
  stop_group 10
) </dev/null >/dev/null 2>&1 &
watchdog=$!

while IFS="$TAB" read -r name kind cmd; do
  [ -n "$name" ] || continue
  case $kind in
    preflight | install | build) dir=$W ;;
    command | report) dir=$OSTIA_BUILD_DIR && mkdir -p "$dir" ;;
    *) say "error: step $name has unknown kind $kind"; exit_code=3; break ;;
  esac
  say "step $name ($kind): $cmd"
  rm -f "$S/rc" "$S/pgid"
  start=$(now)
  running="$name $kind $start"
  (
    cd "$dir" || { echo 1 >"$S/rc"; exit 1; }
    { setsid -w sh -c 'echo $$ >"$0"; exec sh -c "$1"' "$S/pgid" "$cmd" </dev/null 2>&1
      echo $? >"$S/rc"; } | tee -a "$LOG"
  ) &
  wait $!
  running=
  rc=$(cat "$S/rc" 2>/dev/null || echo 1)
  seconds=$(($(now) - start))
  if [ -e "$S/.timed-out" ]; then
    add_entry "$name" "$kind" "$rc" "$seconds" timeout
    state=timeout
    exit_code=3
    say "error: the pipeline ran past its ${TIMEOUT}s limit in step $name"
    break
  fi
  if [ "$rc" -eq 0 ]; then result=ok; else result=failed; fi
  add_entry "$name" "$kind" "$rc" "$seconds" "$result"
  write_steps
  say "step $name: exit $rc after ${seconds}s"
  [ "$rc" -eq 0 ] && continue
  case $kind in
    report) ;;
    preflight | install) exit_code=3; break ;;
    *) exit_code=$rc; break ;;
  esac
done <<EOF
${OSTIA_PLAN:-}
EOF

: >"$S/.stop-watchdog"
[ "$state" = running ] && state=done
write_steps
say "finished with exit $exit_code; waiting for the results to be collected (at most ${COLLECT_WINDOW}s)"
t0=$(now)
while [ ! -e "$S/collected" ] && [ $(($(now) - t0)) -lt "$COLLECT_WINDOW" ]; do
  nap "$POLL"
done
wait "$watchdog" 2>/dev/null
exit "$exit_code"
