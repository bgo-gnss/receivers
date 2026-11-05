#!/usr/bin/env bash
# prx5.sh - download data from, and check the health of, one PolaRx5 (fw 5.7.0).
#
# Stand-alone: bash 4+, GNU coreutils, grep, awk, curl, and timecalc from gtimes
# (download only). No Python code and no RxTools. It talks only to the receiver and
# uses no database. Run `prx5.sh --help` for a tutorial.
#
#   prx5.sh health   TEST 192.0.2.10
#   prx5.sh download TEST 192.0.2.10:2160 --start 2026-11-01 --end 2026-11-03
#   prx5.sh health   TEST 192.0.2.10 --save   # remember the address
#   prx5.sh health   TEST                    # from then on
#
# Exit codes: 0 ok; 1 warning, or some files not fetched; 2 critical;
# 3 unreachable, login refused or unknown; 64 bad usage.
#
# LOCKOUT: a few failed logins on fw 5.7.0 lock EVERY account on the receiver
# for about nine hours, including those of any other client. So health logs in
# once on one connection, and download stops at the first refused login. Never
# add a retry around a login here. Never run this with `set -x`: the password
# would be traced. It goes to curl on stdin and to the receiver via the printf
# builtin, so it is never on a command line.

set -euo pipefail

readonly EXIT_OK=0 EXIT_WARNING=1 EXIT_CRITICAL=2 EXIT_UNKNOWN=3 EXIT_USAGE=64
readonly DEFAULT_FTP_PORT=2160 DEFAULT_HTTP_PORT=8060 DEFAULT_CONTROL_PORT=28784
readonly SECURE_CONTROL_PORT=28783 DEFAULT_BASE_PATH=/DSK1/SSN/
readonly CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
readonly DEFAULT_CFG=$CONFIG_HOME/polarx5-tools/stations.cfg
readonly DEFAULT_CRED=$CONFIG_HOME/polarx5-tools/credentials

die() { # die CODE MESSAGE
    local code=$1
    shift
    printf 'prx5.sh: %s\n' "$*" >&2
    exit "$code"
}

usage() {
    cat <<EOF
usage: prx5.sh download STA [HOST[:FTPPORT]] [options]
       prx5.sh health   STA [HOST[:CONTROLPORT]] [options]
       prx5.sh ports    STA [HOST] [options]
       prx5.sh stations [--cfg FILE]

download: -s|--session 15s_24hr|1Hz_1hr|status_1hr   --start WHEN  --end WHEN
          -D|--sessions-back N (last N complete sessions: days for 15s_24hr, hours for 1hr)
          -o|--outdir DIR  --flat  --base-path PATH  --dry-run  --print-paths
          --unfinalised  --force  --retries N  --active  --anonymous
          WHEN is UTC, YYYY-MM-DD or YYYY-MM-DDTHH; --end is INCLUSIVE.
health:   --ftp-port N  --http-port N  --no-login  --json
ports:    --ftp-port N  --http-port N  --control-port N
common:   -u|--user USER  -p|--password PASS  --cred-file FILE  --cfg FILE  --save  --timeout SECONDS
          -u without -p asks for the password. -p is visible in ps and shell history.
EOF
}

tutorial() {
    usage
    cat <<'EOF'

TUTORIAL (prx5.sh)

Replace TEST with your 4-character station ID and 192.0.2.10 with its address.
All times are UTC.

1. Check that the receiver can be reached. This sends no login.

     prx5.sh ports TEST 192.0.2.10

   All three ports should say "open". If FTP is closed, downloads cannot work:
   upgrading to fw 5.7.0 turns FTP off (see step 8).

2. Check its health. -u asks for the password without showing it.

     prx5.sh health TEST 192.0.2.10 -u myuser

   Read the [ok], [warning] or [critical] tags and the STATUS line at the end.
   "errors ANTENNA" with "antenna power OFF" means an antenna short: no data is
   being recorded. Voltage is shown as a value only and is never judged.
   For disk and satellite counts, use prx5.py.
   Exit code: 0 ok, 1 warning, 2 critical, 3 unreachable or login refused.

   CAREFUL: repeated failed logins lock the receiver for about 9 hours, for every
   user. The tool stops at the first refusal. Check the password before you retry.

3. Save the address, so you can leave it out from now on.

     prx5.sh health TEST 192.0.2.10 -u myuser --save
     prx5.sh health TEST -u myuser
     prx5.sh stations

4. Stop typing the login. Create a file only you can read:

     mkdir -p ~/.config/polarx5-tools
     install -m 600 /dev/null ~/.config/polarx5-tools/credentials
     printf 'username = myuser\npassword = mypass\n' \
       > ~/.config/polarx5-tools/credentials

   Now "prx5.sh health TEST" works alone. The rest of this tutorial assumes this.

5. See what the receiver holds, without downloading anything.

     prx5.sh download TEST -D 3 --dry-run

   -D 3 means the last 3 complete sessions, here the last 3 days.

6. Download.

     prx5.sh download TEST                      yesterday's 15s_24hr file
     prx5.sh download TEST -D 7                 the last 7 days
     prx5.sh download TEST -s 1Hz_1hr -D 6      the last 6 hours of 1 Hz data
     prx5.sh download TEST --start 2026-11-01 --end 2026-11-03
                                             those 3 days (--end is inclusive)
     prx5.sh download TEST -s 1Hz_1hr --start 2026-11-03T14 --end 2026-11-03T18
                                             those 5 hours

   Files go to ./gpsdata/YYYY/mon/STA/SESSION/raw/. Use -o DIR for another root,
   or --flat to put them all directly in DIR. Run the same command again at any
   time: finished files are skipped and interrupted ones resume.

7. Use it in a script. Exit codes tell you the result:

     prx5.sh health TEST --json > status.json || echo "exit code $?"
     prx5.sh download TEST -D 1 -o /data/gnss || echo "some files are missing"

8. When something goes wrong.

     login refused        Wrong password. Fix it BEFORE you run again.
     receiver locked      Wait for the countdown. Retrying does not help or hurt.
     Not authorized       No login given. Use -u or a credentials file.
     FTP port refused     FTP is off after a 5.7.0 upgrade. On the control port,
                          send: sis, all, FTP   then: eccf, Current, Boot
     trying active        Normal: the router refused passive FTP.
     no data connection   Neither FTP mode works from this computer. Try again,
                          or run from a computer on the receiver's own network.
     unfinalised          The file is still being written. Add --unfinalised.
     absent               The receiver does not hold that file.
EOF
}

# --- configuration and credentials ---------------------------------------------

cfg_get() { # cfg_get SECTION KEY: empty when absent.
    [ -f "$CFG" ] || return 0
    awk -v sec="[$1]" -v key="$2" '
        { line = $0; sub(/\r$/, "", line); gsub(/^[ \t]+|[ \t]+$/, "", line) }
        substr(line, 1, 1) == "[" { in_sec = (line == sec); next }
        in_sec && index(line, "=") {
            k = substr(line, 1, index(line, "=") - 1); gsub(/[ \t]+$/, "", k)
            if (k == key) {
                v = substr(line, index(line, "=") + 1); gsub(/^[ \t]+/, "", v)
                print v; exit
            }
        }' "$CFG"
}

save_station() { # save_station SECTION KEY=VALUE...
    local section=$1 tmp pair
    shift
    if [ -f "$CFG" ] && awk -F= '
        /^[ \t]*[#;[]/ || !index($0, "=") { next }
        { k = $1; gsub(/[ \t]/, "", k) }
        k !~ /^(router_ip|receiver_ftpport|receiver_httpport|receiver_controlport|base_path|ftp_mode)$/ { found = 1; exit }
        END { exit !found }' "$CFG"; then
        die "$EXIT_USAGE" "refusing to write $CFG: it has keys this tool does not manage, so it looks like a config maintained elsewhere. Use --cfg with a separate file."
    fi
    mkdir -p "$(dirname "$CFG")"
    [ -f "$CFG" ] || : >"$CFG"
    if ! grep -qxF "[$section]" "$CFG"; then
        [ ! -s "$CFG" ] || [ -z "$(tail -n1 "$CFG")" ] || echo >>"$CFG"
        echo "[$section]" >>"$CFG"
    fi
    for pair in "$@"; do
        tmp=$(mktemp "$CFG.XXXXXX")
        awk -v sec="[$section]" -v key="${pair%%=*}" -v val="${pair#*=}" '
            function emit() { if (!done) { print key " = " val; done = 1 } }
            $0 == sec { in_sec = 1; print; next }
            substr($0, 1, 1) == "[" { if (in_sec) emit(); in_sec = 0 }
            in_sec && $0 ~ "^[ \t]*" key "[ \t]*=" { emit(); next }
            { print }
            END { if (in_sec) emit() }' "$CFG" >"$tmp"
        mv "$tmp" "$CFG"
    done
}

CRED_USER='' CRED_PASS=''
load_credentials() { # --user/--password, environment, --cred-file, default file.
    local file=${CRED_FILE:-$DEFAULT_CRED} mode
    if [ -n "$OPT_USER" ] || [ -n "$OPT_PASS" ]; then
        [ -n "$OPT_USER" ] || die "$EXIT_USAGE" "--password needs --user"
        CRED_USER=$OPT_USER CRED_PASS=$OPT_PASS
        if [ -z "$CRED_PASS" ]; then
            [ -t 0 ] || die "$EXIT_USAGE" "--user needs --password when not at a terminal"
            read -rsp "Password for $CRED_USER: " CRED_PASS || true
            echo >&2
        fi
        [ -n "$CRED_PASS" ] || die "$EXIT_USAGE" "empty password"
    elif [ -n "${PRX5_USER:-}" ] || [ -n "${PRX5_PASS:-}" ]; then
        [ -n "${PRX5_USER:-}" ] && [ -n "${PRX5_PASS:-}" ] ||
            die "$EXIT_USAGE" "set both PRX5_USER and PRX5_PASS, or neither"
        CRED_USER=$PRX5_USER CRED_PASS=$PRX5_PASS
    elif [ -f "$file" ]; then
        mode=$(stat -c %a "$file")
        case "$mode" in
            ?00 | 00) ;;
            *) die "$EXIT_USAGE" "$file is mode $mode: run chmod 600 on it" ;;
        esac
        # Read as data, never sourced.
        CRED_USER=$(awk -F= '$1 ~ /^[ \t]*username[ \t]*$/ { v = substr($0, index($0, "=") + 1); gsub(/^[ \t]+|[ \t]+$/, "", v); print v; exit }' "$file")
        CRED_PASS=$(awk -F= '$1 ~ /^[ \t]*password[ \t]*$/ { v = substr($0, index($0, "=") + 1); gsub(/^[ \t]+|[ \t]+$/, "", v); print v; exit }' "$file")
        [ -n "$CRED_USER" ] && [ -n "$CRED_PASS" ] ||
            die "$EXIT_USAGE" "$file needs username= and password= lines"
    elif [ -n "$CRED_FILE" ]; then
        die "$EXIT_USAGE" "no credentials file at $CRED_FILE"
    fi
    # Septentrio commands are comma separated, so these could never log in, and
    # each try would count towards the lockout.
    case "$CRED_USER$CRED_PASS" in
        *,* | *[[:space:]]*) die "$EXIT_USAGE" "username/password must not contain commas or whitespace" ;;
    esac
}

parse_address() { # Sets HOST_ARG and PORT_ARG.
    HOST_ARG='' PORT_ARG=''
    [ -n "$1" ] || return 0
    HOST_ARG=${1%%:*}
    [[ $1 == *:* ]] && PORT_ARG=${1#*:}
    [[ $HOST_ARG =~ ^[A-Za-z0-9._-]+$ ]] || die "$EXIT_USAGE" "bad address '$1': expected HOST or HOST:PORT"
    [[ $1 != *:* || $PORT_ARG =~ ^[0-9]+$ ]] || die "$EXIT_USAGE" "bad address '$1': expected HOST or HOST:PORT"
}

resolve_host() {
    HOST=${HOST_ARG:-$(cfg_get "$STATION" router_ip)}
    [ -n "$HOST" ] || die "$EXIT_USAGE" "no address for $STATION: give HOST[:PORT] or add it to $CFG"
    [[ $HOST =~ ^[A-Za-z0-9._-]+$ ]] || die "$EXIT_USAGE" "bad host '$HOST' in $CFG"
}

resolve_port() { # resolve_port CLI_VALUE CFG_KEY DEFAULT
    local value=${1:-$(cfg_get "$STATION" "$2")}
    [[ $value =~ ^[0-9]+$ ]] || value=$3
    echo "$value"
}

# --- ports --------------------------------------------------------------------

probe_port() { # prints open / refused / timeout. Retried once: 3G/4G links lose packets.
    local state=timeout rc attempt
    for attempt in 1 2; do
        rc=0
        timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$HOST" "$1" 2>/dev/null || rc=$?
        if [ "$rc" -eq 0 ]; then
            echo open
            return 0
        fi
        # bash reports a refused connection as exit 1, `timeout` its limit as 124.
        if [ "$rc" -eq 124 ]; then state=timeout; else state=refused; fi
        [ "$attempt" = 2 ] || sleep 0.2
    done
    echo "$state"
}

port_hints() { # port_hints FTP_STATE CONTROL_STATE
    if [ "$1" != open ]; then
        echo "FTP closed: upgrading to 5.7.0 turns it off. To reopen it, send 'sis, all, FTP' then 'eccf, Current, Boot' on the control port. No download works until then."
    fi
    if [ "$2" != open ] && [ "$(probe_port "$SECURE_CONTROL_PORT")" = open ]; then
        echo "Control port closed but TLS port $SECURE_CONTROL_PORT open: the receiver is in 'sis, secure', as it is left after a firmware upgrade."
    fi
}

cmd_ports() {
    parse_address "$ADDRESS"
    resolve_host
    local ftp http control s_ftp s_http s_control
    ftp=$(resolve_port "$OPT_FTP_PORT" receiver_ftpport "$DEFAULT_FTP_PORT")
    http=$(resolve_port "$OPT_HTTP_PORT" receiver_httpport "$DEFAULT_HTTP_PORT")
    control=$(resolve_port "$OPT_CONTROL_PORT" receiver_controlport "$DEFAULT_CONTROL_PORT")
    s_ftp=$(probe_port "$ftp")
    s_http=$(probe_port "$http")
    s_control=$(probe_port "$control")
    printf '%s %-8s %s:%s %s\n' "$STATION" ftp "$HOST" "$ftp" "$s_ftp" \
        "$STATION" http "$HOST" "$http" "$s_http" \
        "$STATION" control "$HOST" "$control" "$s_control"
    port_hints "$s_ftp" "$s_control" | sed 's/^/hint: /'
    [ "$s_ftp$s_http$s_control" = openopenopen ] && return "$EXIT_OK"
    return "$EXIT_WARNING"
}

cmd_stations() {
    if [ ! -f "$CFG" ]; then
        echo "no stations file at $CFG"
        return 0
    fi
    echo "# $CFG"
    local name
    while IFS= read -r name; do
        printf '%-6s %-28s ftp=%s control=%s\n' "$name" \
            "$(cfg_get "$name" router_ip)" "$(cfg_get "$name" receiver_ftpport)" \
            "$(cfg_get "$name" receiver_controlport)"
    done < <(sed -n 's/^\[\([^]]*\)\][[:space:]]*$/\1/p' "$CFG")
}

# --- SBF: read little-endian fields out of the capture file with od -------------

u8() { od -An -v -j "$1" -N 1 -tu1 "$CAPTURE" | tr -d ' '; }
u16() { od -An -v -j "$1" -N 2 -tu2 --endian=little "$CAPTURE" | tr -d ' '; }
u32() { od -An -v -j "$1" -N 4 -tu4 --endian=little "$CAPTURE" | tr -d ' '; }
f64() { od -An -v -j "$1" -N 8 -tf8 --endian=little "$CAPTURE" | tr -d ' '; }
sbf_text() { # sbf_text OFFSET SIZE: a NUL-terminated field, stripped to safe characters
    dd if="$CAPTURE" bs=1 skip="$1" count="$2" 2>/dev/null | tr '\000' '\n' |
        awk 'NR == 1' | tr -cd 'A-Za-z0-9._ -' | awk '{ $1 = $1; print }'
}

# Sets BLOCK to the whole block's offset in the capture. A block split across
# reads has an incomplete length, so it is simply found on a later poll.
find_block() { # find_block BLOCK_ID FROM_OFFSET
    local want=$1 from=$2 size offset id length
    size=$(stat -c %s "$CAPTURE")
    BLOCK=''
    while IFS=: read -r offset _; do
        [ "$offset" -ge "$from" ] || continue
        [ $((offset + 8)) -le "$size" ] || continue
        id=$(($(u16 $((offset + 4))) & 8191))
        [ "$id" -eq "$want" ] || continue
        length=$(u16 $((offset + 6)))
        if [ $((offset + length)) -le "$size" ]; then
            BLOCK=$offset
            return 0
        fi
    done < <(LC_ALL=C grep -aob '\$@' "$CAPTURE" || true)
    return 1
}

close_session() {
    [ -z "${READER_PID:-}" ] || kill "$READER_PID" 2>/dev/null || true
    [ -z "${READER_PID:-}" ] || wait "$READER_PID" 2>/dev/null || true
    READER_PID=''
    exec 3>&-
    [ -z "${CAPTURE:-}" ] || rm -f "$CAPTURE"
    CAPTURE=''
}

open_session() { # open_session PORT: the capture file is read by polling.
    # A blocked connect in this shell could not be timed out, so probe first.
    timeout "$TIMEOUT" bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$HOST" "$1" 2>/dev/null ||
        return 1
    exec 3<>"/dev/tcp/$HOST/$1" || return 1
    CAPTURE=$(mktemp "${TMPDIR:-/tmp}/prx5.XXXXXX")
    cat <&3 >"$CAPTURE" &
    READER_PID=$!
    trap close_session EXIT
    wait_for 'IP[A-Za-z0-9]*>' "$TIMEOUT" 0 || true
    CONN_ID=$(LC_ALL=C grep -ao 'IP[A-Za-z0-9]*>' "$CAPTURE" | awk 'NR == 1' | tr -d '>' || true)
    CONN_ID=${CONN_ID:-IP11}
}

wait_for() { # wait_for REGEX SECONDS FROM_OFFSET
    local deadline=$((SECONDS + ${2%.*})) from=$3
    while [ "$SECONDS" -le "$deadline" ]; do
        if tail -c +$((from + 1)) "$CAPTURE" | LC_ALL=C grep -aqE "$1"; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

AUTH_ERROR=''
do_login() { # One attempt, ever.
    local from text seconds
    from=$(stat -c %s "$CAPTURE")
    printf 'login, %s, %s\n' "$CRED_USER" "$CRED_PASS" >&3
    wait_for '(\$R|\$E).*' "$TIMEOUT" "$from" || true
    sleep 0.3
    text=$(tail -c +$((from + 1)) "$CAPTURE" | LC_ALL=C tr -cd '[:print:]\n')
    if [[ $text == *"Too many failed login"* ]]; then
        seconds=$(sed -n 's/.*Next login possible in \([0-9]*\) sec.*/\1/p' <<<"$text" | awk 'NR == 1')
        AUTH_ERROR="receiver locked by failed logins for another ${seconds:-?} s. It counts down whatever you do: wait it out."
    elif [[ $text == *'$E'*'Invalid command'* ]]; then
        : # firmware <= 5.5 has no login command and needs none
    elif [[ $text == *'$R?'* ]]; then
        AUTH_ERROR="login refused and NOT retried ($(grep -F '$R?' <<<"$text" | awk 'NR == 1')). Fix the credentials before running again: repeated failures lock the receiver for about 9 h."
    fi
}

request_block() { # request_block NAME ID: sets BLOCK, or empties it.
    local from deadline
    from=$(stat -c %s "$CAPTURE")
    printf 'esoc, %s, %s\n' "$CONN_ID" "$1" >&3
    deadline=$((SECONDS + 3))
    BLOCK=''
    while [ "$SECONDS" -le "$deadline" ]; do
        find_block "$2" "$from" && return 0
        if tail -c +$((from + 1)) "$CAPTURE" | LC_ALL=C grep -aq 'Not authorized'; then
            AUTH_ERROR="receiver answered 'Not authorized': fw 5.7.0 needs credentials (see README.md)"
            return 1
        fi
        sleep 0.1
    done
    return 1
}

# --- health -------------------------------------------------------------------

raise() { # raise STATUS: keep the worst of OVERALL
    case "$1:$OVERALL" in
        critical:* | warning:ok) OVERALL=$1 ;;
    esac
}

band() { # band VALUE CRIT_LOW WARN_LOW WARN_HIGH CRIT_HIGH
    awk -v v="$1" -v cl="$2" -v wl="$3" -v wh="$4" -v ch="$5" 'BEGIN {
        if (v < cl || v > ch) print "critical"
        else if (v < wl || v > wh) print "warning"
        else print "ok" }'
}

error_flags() { # error_flags RX_ERROR EXT_ERROR
    local bit flags='' name
    for bit in {0..31}; do
        if (($1 & (1 << bit))); then
            case $bit in
                3) name=SOFTWARE ;; 4) name=WATCHDOG ;; 5) name=ANTENNA ;;
                6) name=CONGESTION ;; 8) name=MISSEDEVENT ;; 9) name=CPUOVERLOAD ;;
                10) name=INVALIDCONFIG ;; 11) name=OUTOFGEOFENCE ;; *) name=BIT$bit ;;
            esac
            flags+="$name "
        fi
    done
    for bit in {0..7}; do
        if (($2 & (1 << bit))); then
            case $bit in
                0) name=SISERROR ;; 1) name=DIFFCORRERROR ;; 2) name=EXTSENSORERROR ;;
                3) name=SETUPERROR ;; *) name=EXT_BIT$bit ;;
            esac
            flags+="$name "
        fi
    done
    echo "${flags% }"
}

cmd_health() {
    parse_address "$ADDRESS"
    resolve_host
    local ftp http control s_ftp s_http s_control hints=''
    ftp=$(resolve_port "$OPT_FTP_PORT" receiver_ftpport "$DEFAULT_FTP_PORT")
    http=$(resolve_port "$OPT_HTTP_PORT" receiver_httpport "$DEFAULT_HTTP_PORT")
    control=$(resolve_port "$PORT_ARG" receiver_controlport "$DEFAULT_CONTROL_PORT")
    [ "$NO_LOGIN" = 1 ] || load_credentials
    s_ftp=$(probe_port "$ftp")
    s_http=$(probe_port "$http")
    s_control=$(probe_port "$control")
    hints=$(port_hints "$s_ftp" "$s_control")

    OVERALL=ok
    local error='' fw='' serial='' marker='' rxname='' cpu='' temp='' uptime='' rx_error='' ext_error=''
    local flags='' antenna='' voltage='' mode='' nrsv='' lat='' lon='' hgt='' s
    local s_cpu='' s_temp='' s_err='' s_pos=''
    [ "$s_ftp" = open ] && [ "$s_http" = open ] && [ "$s_control" = open ] || raise warning

    if [ "$s_control" != open ]; then
        error="control port $control $s_control"
    elif ! open_session "$control"; then
        error="cannot connect to control port $control"
    else
        [ -z "$CRED_USER" ] || do_login
        error=$AUTH_ERROR
    fi

    if [ -z "$error" ]; then
        if request_block ReceiverSetup 5902; then
            s=$BLOCK
            marker=$(sbf_text $((s + 16)) 60)
            serial=$(sbf_text $((s + 156)) 20)
            rxname=$(sbf_text $((s + 176)) 20)
            fw=$(sbf_text $((s + 196)) 20)
        fi
        if [ -z "$AUTH_ERROR" ] && request_block ReceiverStatus 4014; then
            s=$BLOCK
            cpu=$(u8 $((s + 14)))
            ext_error=$(u8 $((s + 15)))
            uptime=$(u32 $((s + 16)))
            local rx_status
            rx_status=$(u32 $((s + 20)))
            rx_error=$(u32 $((s + 24)))
            temp=$(($(u8 $((s + 31))) - 100))
            flags=$(error_flags "$rx_error" "$ext_error")
            if ((rx_status & 2)); then antenna=on; else antenna=OFF; fi
            if ((cpu >= 90)); then s_cpu=critical; elif ((cpu >= 75)); then s_cpu=warning; else s_cpu=ok; fi
            s_temp=$(band "$temp" -20 -10 50 60)
            # An antenna overcurrent cuts antenna power: no observations at all.
            case " $flags " in
                *" ANTENNA "*) s_err=critical ;;
                "  ") s_err=ok ;;
                *) s_err=warning ;;
            esac
            raise "$s_cpu"
            raise "$s_temp"
            raise "$s_err"
        else
            raise warning
        fi
        if [ -z "$AUTH_ERROR" ] && request_block PowerStatus 4101; then
            voltage=$(awk -v r="$(u16 $((BLOCK + 14)))" 'BEGIN { printf "%.2f", int(r / 16) / 40 }')
        else
            raise warning
        fi
        if [ -z "$AUTH_ERROR" ] && request_block PVTGeodetic 4007; then
            s=$BLOCK
            mode=$(($(u8 $((s + 14))) & 15))
            nrsv=$(u8 $((s + 74)))
            if ((mode > 0)); then s_pos=ok; else s_pos=warning; fi
            raise "$s_pos"
            read -r lat lon hgt < <(awk -v a="$(f64 $((s + 16)))" -v o="$(f64 $((s + 24)))" -v h="$(f64 $((s + 32)))" 'BEGIN {
                d = 180 / atan2(0, -1)
                # SBF marks an absent value with a huge negative "do not use" number.
                if (a * d >= -90 && a * d <= 90) printf "%.8f %.8f %.3f\n", a * d, o * d, h }') || true
        else
            raise warning
        fi
        error=$AUTH_ERROR
    fi
    close_session
    if [ "$SAVE" = 1 ] && [ -n "$HOST_ARG" ]; then
        save_station "$STATION" "router_ip=$HOST" "receiver_controlport=$control"
    fi
    [ -z "$error" ] || OVERALL=unknown

    if [ "$JSON" = 1 ]; then
        json_num() { if [ -n "$1" ]; then echo "$1"; else echo null; fi; }
        json_str() { if [ -n "$1" ]; then printf '"%s"' "${1//\"/\'}"; else echo null; fi; }
        printf '{"station":"%s","host":"%s","status":"%s","error":%s,\n' "$STATION" "$HOST" "$OVERALL" "$(json_str "$error")"
        printf ' "ports":{"ftp":{"port":%s,"state":"%s"},"http":{"port":%s,"state":"%s"},"control":{"port":%s,"state":"%s"}},\n' \
            "$ftp" "$s_ftp" "$http" "$s_http" "$control" "$s_control"
        printf ' "identity":{"firmware_version":%s,"serial_number":%s,"marker_name":%s},\n' \
            "$(json_str "$fw")" "$(json_str "$serial")" "$(json_str "$marker")"
        printf ' "receiver":{"cpu_percent":%s,"temperature_c":%s,"uptime_seconds":%s,"rx_error":%s,"ext_error":%s,"error_flags":"%s","error_status":%s,"antenna_power":%s},\n' \
            "$(json_num "$cpu")" "$(json_num "$temp")" "$(json_num "$uptime")" "$(json_num "$rx_error")" "$(json_num "$ext_error")" \
            "$flags" "$(json_str "$s_err")" "$(json_str "$antenna")"
        printf ' "power":{"voltage":%s},\n' "$(json_num "$voltage")"
        printf ' "position":{"mode":%s,"satellites_used":%s,"latitude":%s,"longitude":%s,"height":%s}}\n' \
            "$(json_num "$mode")" "$(json_num "$nrsv")" "$(json_num "$lat")" "$(json_num "$lon")" "$(json_num "$hgt")"
    else
        echo "$STATION @ $HOST  $(date -u +%FT%TZ)"
        printf '  %-16s %s %s\n' 'ftp port' "$ftp" "$s_ftp" 'http port' "$http" "$s_http" 'control port' "$control" "$s_control"
        [ -z "$fw" ] || printf '  %-16s %s fw %s serial %s marker %s\n' receiver "$rxname" "$fw" "$serial" "$marker"
        if [ -n "$cpu" ]; then
            printf '  %-16s %s %%  [%s]\n' cpu "$cpu" "$s_cpu"
            printf '  %-16s %s C  [%s]\n' temperature "$temp" "$s_temp"
            printf '  %-16s %dd %02dh %02dm\n' uptime $((uptime / 86400)) $((uptime % 86400 / 3600)) $((uptime % 3600 / 60))
            printf '  %-16s %s\n' 'antenna power' "$antenna"
            printf '  %-16s %s  [%s]\n' errors "${flags:-none}" "$s_err"
        fi
        [ -z "$voltage" ] || printf '  %-16s %s V\n' voltage "$voltage"
        [ -z "$mode" ] || printf '  %-16s mode %s, %s satellites, %s  [%s]\n' position "$mode" "$nrsv" "${lat:+$lat $lon $hgt m}" "$s_pos"
        [ -z "$hints" ] || sed 's/^/  hint: /' <<<"$hints"
        [ -z "$error" ] || printf '  error: %s\n' "$error"
        echo "STATUS: ${OVERALL^^}"
    fi
    case "$OVERALL" in
        ok) return "$EXIT_OK" ;;
        warning) return "$EXIT_WARNING" ;;
        critical) return "$EXIT_CRITICAL" ;;
    esac
    return "$EXIT_UNKNOWN"
}

# --- download -----------------------------------------------------------------

when_to_epoch() { # when_to_epoch TEXT: sets WHEN_EPOCH and WHEN_HAS_HOUR
    case "$1" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) WHEN_HAS_HOUR=0 WHEN_EPOCH=$(date -u -d "$1" +%s 2>/dev/null) ;;
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) WHEN_HAS_HOUR=0 WHEN_EPOCH=$(date -u -d "$1" +%s 2>/dev/null) ;;
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T\ ][0-9][0-9]) WHEN_HAS_HOUR=1 WHEN_EPOCH=$(date -u -d "${1:0:10} ${1:11:2}:00" +%s 2>/dev/null) ;;
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) WHEN_HAS_HOUR=1 WHEN_EPOCH=$(date -u -d "${1:0:8} ${1:8:2}:00" +%s 2>/dev/null) ;;
        *) WHEN_EPOCH='' ;;
    esac
    [ -n "$WHEN_EPOCH" ] || die "$EXIT_USAGE" "bad date '$1': use YYYY-MM-DD or YYYY-MM-DDTHH (UTC)"
}

gt_path() { # gt_path TEMPLATE FREQ EPOCH
    local out
    out=$(timecalc -d "$(date -u -d "@$3" '+%Y-%m-%d %H:%M')" -f '%Y-%m-%d %H:%M' -l "$1" "$2") ||
        die "$EXIT_USAGE" "timecalc failed on template $1"
    awk 'NR == 1' <<<"$out"
}

curl_ftp() { # curl_ftp ARGS...: credentials go on stdin as a config line
    if [ -n "$CRED_USER" ]; then
        local u=${CRED_USER//\\/\\\\} p=${CRED_PASS//\\/\\\\}
        u=${u//\"/\\\"} p=${p//\"/\\\"}
        printf 'user = "%s:%s"\n' "$u" "$p"
    else
        printf 'user = "anonymous:anonymous"\n'
    fi | curl -K - -sS --connect-timeout "$TIMEOUT" --speed-limit 1 --speed-time "$TIMEOUT" \
        --ftp-method nocwd --disable-epsv --disable-eprt --ftp-skip-pasv-ip "${FTP_MODE_ARGS[@]}" "$@"
}

# A progress bar only when someone is watching; a log file stays clean.
BAR_ARGS=()
[ ! -t 2 ] || BAR_ARGS=(--no-silent --progress-bar)

announce() { # announce NAME SIZE [RESUME_AT]
    awk -v label="$LABEL" -v name="$1" -v size="$2" -v at="${3:-0}" 'BEGIN {
        text = (size == "") ? "size unknown" : sprintf("%.2f MB", size / 1e6)
        if (at > 0) text = text sprintf(", resuming at %.2f MB", at / 1e6)
        printf "%s fetching %s (%s)\n", label, name, text
    }' >&2
}

DATA_OK=0 MODE_SWITCHED=0 SWITCH_RETRY=0 DATA_FAILURE=''

# One transfer. Returns 0 on success. On failure with no bytes received and no
# transfer yet working in this mode, it switches FTP mode once and sets
# SWITCH_RETRY; if the other mode fails the same way, the run ends.
transfer() { # transfer REMOTE OUTPUT OFFSET
    local before=0 after=0 rc=0 tried other
    [ ! -f "$2" ] || before=$(stat -c %s "$2")
    if [ "$3" -gt 0 ]; then
        curl_ftp ${BAR_ARGS[@]+"${BAR_ARGS[@]}"} --fail -C "$3" -o "$2" "$FTP_URL$1" || rc=$?
    else
        curl_ftp ${BAR_ARGS[@]+"${BAR_ARGS[@]}"} --fail -o "$2" "$FTP_URL$1" || rc=$?
    fi
    [ "$rc" -ne 67 ] || refused_login
    if [ "$rc" -eq 0 ]; then
        DATA_OK=1
        return 0
    fi
    if [ "$rc" -eq 33 ] || [ "$rc" -eq 36 ]; then rm -f "$2"; fi
    [ ! -f "$2" ] || after=$(stat -c %s "$2")
    case $rc in
        7 | 12 | 13 | 14 | 15 | 28 | 30)
            if [ "$after" = "$before" ] && [ "$DATA_OK" = 0 ]; then
                if [ "${FTP_MODE_ARGS[0]}" = --ftp-pasv ]; then tried=passive other=active; else tried=active other=passive; fi
                if [ "$MODE_SWITCHED" = 1 ]; then
                    # Active has been seen to time out now and then and work next time.
                    DATA_FAILURE="$tried: curl error $rc"
                    return 1
                fi
                MODE_SWITCHED=1 SWITCH_RETRY=1
                if [ "$other" = active ]; then FTP_MODE_ARGS=(--ftp-port -); else FTP_MODE_ARGS=(--ftp-pasv); fi
                echo "$tried FTP data connection failed (curl error $rc): trying $other" >&2
            fi
            ;;
    esac
    return 1
}

after_failed_transfer() { # Do not spend a retry on the mode switch.
    if [ "$SWITCH_RETRY" = 1 ]; then
        SWITCH_RETRY=0
        attempt=$((attempt - 1))
    fi
}

refused_login() {
    die "$EXIT_UNKNOWN" "FTP login refused and NOT retried. Fix the credentials before running again: repeated failures lock the receiver for about 9 h."
}

# Prints the size in bytes, or nothing when the file is absent. Returns 1 on a
# network error and 2 on a refused login (curl's 67). It always runs inside $(...),
# where `die` would end only the subshell and the next file would log in again,
# so callers MUST pass the result to check_size.
ftp_size() {
    local out rc=0
    out=$(curl_ftp --head "$FTP_URL$1" 2>/dev/null) || rc=$?
    case $rc in
        0) tr -d '\r' <<<"$out" | awk 'tolower($1) == "content-length:" { print $2; exit }' ;;
        67) return 2 ;;
        6 | 7 | 28 | 52 | 55 | 56) return 1 ;;
        *) return 0 ;;
    esac
}

check_size() { # check_size RC: ends the run on a refused login, else returns RC
    [ "$1" -ne 2 ] || refused_login
    return "$1"
}

fetch_one() { # fetch_one REMOTE LOCAL: sets OUTCOME and BYTES
    local remote=$1 local_file=$2 size='' bare='' have rc attempt part=$2.part raw=$2.raw
    mkdir -p "$(dirname "$local_file")"
    for ((attempt = 0; attempt <= RETRIES; attempt++)); do
        [ "$attempt" -eq 0 ] || sleep $((attempt * 5 > 30 ? 30 : attempt * 5))
        OUTCOME='failed: network' BYTES=0
        rc=0
        size=$(ftp_size "$remote") || rc=$?
        check_size "$rc" || continue
        if [ -z "$size" ]; then
            rc=0
            bare=$(ftp_size "${remote%.gz}") || rc=$?
            check_size "$rc" || continue
            if [ -z "$bare" ] || [ "$bare" = 0 ]; then
                OUTCOME=absent
                return 0
            fi
            if [ "$UNFINALISED" != 1 ]; then
                OUTCOME=unfinalised
                return 0
            fi
            # Still being written, so it grows: no resume, and gzip it here.
            rm -f "$raw"
            announce "${remote##*/}" ""
            if ! transfer "${remote%.gz}" "$raw" 0; then
                after_failed_transfer
                continue
            fi
            [ "$(stat -c %s "$raw")" -ge "$bare" ] || continue
            gzip -c "$raw" >"$part"
            rm -f "$raw"
            mv "$part" "$local_file"
            OUTCOME=fetched-unfinalised BYTES=$(stat -c %s "$local_file")
            return 0
        fi
        if [ -f "$local_file" ] && [ "$FORCE" != 1 ] && [ "$(stat -c %s "$local_file")" = "$size" ]; then
            OUTCOME=present BYTES=$size
            return 0
        fi
        have=0
        [ ! -f "$part" ] || have=$(stat -c %s "$part")
        # The server answers 554 to a REST beyond the file, for ever.
        if [ "$have" -gt "$size" ]; then
            rm -f "$part"
            have=0
        fi
        if [ "$have" -lt "$size" ]; then
            announce "${remote##*/}" "$size" "$have"
            if ! transfer "$remote" "$part" "$have"; then
                after_failed_transfer
                continue
            fi
        fi
        [ "$(stat -c %s "$part")" = "$size" ] || continue
        if ! gzip -t "$part" 2>/dev/null; then
            rm -f "$part"
            OUTCOME='failed: not valid gzip'
            continue
        fi
        mv "$part" "$local_file"
        OUTCOME=fetched BYTES=$size
        return 0
    done
    if [ -n "$DATA_FAILURE" ] && [ "$DATA_OK" = 0 ]; then
        die "$EXIT_UNKNOWN" "FTP login works, but no data connection in either mode ($DATA_FAILURE). NAT routers commonly refuse passive, and active needs the receiver to reach this host, which can fail over VPN. Try again, or run this tool from a computer on the receiver's own network."
    fi
}

cmd_download() {
    local letter dir freq step
    case "$SESSION" in
        15s_24hr) letter=a dir=LOG1_15s_24hr freq=1D step=86400 ;;
        1Hz_1hr) letter=b dir=LOG2_1Hz_1hr freq=1H step=3600 ;;
        status_1hr) letter=c dir=LOG5_status_1hr freq=1H step=3600 ;;
        *) die "$EXIT_USAGE" "unknown session '$SESSION'" ;;
    esac
    command -v timecalc >/dev/null || die "$EXIT_USAGE" "timecalc not found: pip install gtimes (see README.md)"
    parse_address "$ADDRESS"

    # Period starts from first to last, BOTH INCLUSIVE, never in the future.
    local now current first last
    now=$(date -u +%s)
    current=$((now - now % step))
    if [ -n "$BACK" ]; then
        [ -z "$START$END" ] || die "$EXIT_USAGE" "-D cannot be combined with --start or --end"
        first=$((current - BACK * step))
        if [ "$UNFINALISED" = 1 ]; then last=$current; else last=$((current - step)); fi
    elif [ -z "$START" ] && [ -z "$END" ]; then
        if [ "$UNFINALISED" = 1 ]; then first=$current; else first=$((current - step)); fi
        last=$first
    else
        [ -n "$START" ] || die "$EXIT_USAGE" "--end needs --start"
        when_to_epoch "$START"
        first=$WHEN_EPOCH
        local start_has_hour=$WHEN_HAS_HOUR
        if [ -n "$END" ]; then
            when_to_epoch "$END"
            last=$WHEN_EPOCH
            [ "$WHEN_HAS_HOUR" = 1 ] || [ "$freq" = 1D ] || last=$((last + 82800))
        elif [ "$start_has_hour" = 1 ] || [ "$freq" = 1D ]; then
            last=$first
        else
            last=$((first + 82800))
        fi
        first=$((first - first % step))
        last=$((last - last % step))
        [ "$last" -ge "$first" ] || die "$EXIT_USAGE" "--end is before --start"
        [ "$last" -le "$current" ] || last=$current
    fi

    local base=${BASE_PATH:-$(cfg_get "$STATION" base_path)}
    base=${base:-$DEFAULT_BASE_PATH}
    base=${base%/}
    local remote_tmpl="$base/$dir/%y%j/${STATION}#Rin2_.gz"
    local name="${STATION}%Y%m%d%H00${letter}.sbf.gz" local_tmpl
    if [ "$FLAT" = 1 ]; then local_tmpl=$name; else local_tmpl="%Y/#b/$STATION/$SESSION/raw/$name"; fi

    local epoch remote local_file
    if [ "$PRINT_PATHS" = 1 ]; then
        for ((epoch = first; epoch <= last; epoch += step)); do
            echo "$(gt_path "$remote_tmpl" "$freq" "$epoch") -> $OUTDIR/$(gt_path "$local_tmpl" "$freq" "$epoch")"
        done
        return 0
    fi

    command -v curl >/dev/null || die "$EXIT_USAGE" "curl not found"
    resolve_host
    local port state
    port=$(resolve_port "$PORT_ARG" receiver_ftpport "$DEFAULT_FTP_PORT")
    if [ "$ANONYMOUS" != 1 ]; then
        load_credentials
        [ -n "$CRED_USER" ] || die "$EXIT_USAGE" "no credentials: fw 5.7.0 refuses anonymous FTP. Set PRX5_USER and PRX5_PASS or create $DEFAULT_CRED (see README.md); use --anonymous for older firmware."
    fi
    state=$(probe_port "$port")
    [ "$state" = open ] || die "$EXIT_UNKNOWN" "FTP port $HOST:$port $state. After a 5.7.0 upgrade FTP is off until 'sis, all, FTP'; 'prx5.sh health' shows the other ports."
    if [ "$SAVE" = 1 ] && [ -n "$HOST_ARG" ]; then
        save_station "$STATION" "router_ip=$HOST" "receiver_ftpport=$port"
    fi
    FTP_URL="ftp://$HOST:$port/"
    if [ "$ACTIVE" = 1 ] || [ "$(cfg_get "$STATION" ftp_mode)" = active ]; then FTP_MODE_ARGS=(--ftp-port -); else FTP_MODE_ARGS=(--ftp-pasv); fi

    local total=0 good=0 size bare rc
    for ((epoch = first; epoch <= last; epoch += step)); do
        remote=$(gt_path "$remote_tmpl" "$freq" "$epoch")
        local_file="$OUTDIR/$(gt_path "$local_tmpl" "$freq" "$epoch")"
        total=$((total + 1))
        LABEL="[$total/$(((last - first) / step + 1))]"
        if [ "$DRY_RUN" = 1 ]; then
            OUTCOME=absent BYTES=0 rc=0
            size=$(ftp_size "$remote") || rc=$?
            if ! check_size "$rc"; then
                OUTCOME='failed: network'
            elif [ -n "$size" ]; then
                OUTCOME=available BYTES=$size
            else
                rc=0
                bare=$(ftp_size "${remote%.gz}") || rc=$?
                check_size "$rc" || OUTCOME='failed: network'
                if [ "$rc" -eq 0 ] && [ -n "$bare" ] && [ "$bare" != 0 ]; then
                    OUTCOME=unfinalised
                fi
            fi
        else
            fetch_one "$remote" "$local_file"
        fi
        case "$OUTCOME" in
            fetched* | present | available) good=$((good + 1)) ;;
        esac
        printf '%-20s %12s %s\n' "$OUTCOME" "$BYTES" "$local_file"
    done
    if [ "$DRY_RUN" = 1 ]; then
        echo "$good/$total files available on the receiver"
    else
        echo "$good/$total files in place"
    fi
    [ "$good" -eq "$total" ] && return "$EXIT_OK"
    return "$EXIT_WARNING"
}

# --- command line --------------------------------------------------------------

COMMAND=${1:-}
[ -n "$COMMAND" ] || {
    usage >&2
    exit "$EXIT_USAGE"
}
shift
case "$COMMAND" in
    -h | --help)
        tutorial
        exit 0
        ;;
    download | health | ports | stations) ;;
    *)
        usage >&2
        exit "$EXIT_USAGE"
        ;;
esac

STATION='' ADDRESS='' CFG_OPT='' CRED_FILE='' SAVE=0 TIMEOUT=30 JSON=0
OPT_FTP_PORT='' OPT_HTTP_PORT='' OPT_CONTROL_PORT='' NO_LOGIN=0
OPT_USER='' OPT_PASS='' BACK=''
SESSION=15s_24hr START='' END='' OUTDIR=gpsdata FLAT=0 BASE_PATH='' DRY_RUN=0 PRINT_PATHS=0
UNFINALISED=0 FORCE=0 RETRIES=2 ACTIVE=0 ANONYMOUS=0
HOST_ARG='' PORT_ARG='' HOST='' CAPTURE='' READER_PID='' CONN_ID='' BLOCK=''

need() { [ "$#" -ge 2 ] || die "$EXIT_USAGE" "$1 needs a value"; }
is_number() { [[ $2 =~ ^[0-9]+$ ]] || die "$EXIT_USAGE" "$1 needs a number"; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        -h | --help) tutorial; exit 0 ;;
        --cfg) need "$@"; CFG_OPT=$2; shift 2 ;;
        --cred-file) need "$@"; CRED_FILE=$2; shift 2 ;;
        -u | --user) need "$@"; OPT_USER=$2; shift 2 ;;
        -p | --password) need "$@"; OPT_PASS=$2; shift 2 ;;
        -D | --sessions-back)
            need "$@"
            is_number "$1" "$2"
            [ "$2" -ge 1 ] || die "$EXIT_USAGE" "$1 must be at least 1"
            BACK=$2
            shift 2
            ;;
        --timeout) need "$@"; is_number "$1" "$2"; TIMEOUT=$2; shift 2 ;;
        --retries) need "$@"; is_number "$1" "$2"; RETRIES=$2; shift 2 ;;
        --ftp-port) need "$@"; is_number "$1" "$2"; OPT_FTP_PORT=$2; shift 2 ;;
        --http-port) need "$@"; is_number "$1" "$2"; OPT_HTTP_PORT=$2; shift 2 ;;
        --control-port) need "$@"; is_number "$1" "$2"; OPT_CONTROL_PORT=$2; shift 2 ;;
        -s | --session) need "$@"; SESSION=$2; shift 2 ;;
        --start) need "$@"; START=$2; shift 2 ;;
        --end) need "$@"; END=$2; shift 2 ;;
        -o | --outdir) need "$@"; OUTDIR=$2; shift 2 ;;
        --base-path) need "$@"; BASE_PATH=$2; shift 2 ;;
        --save) SAVE=1; shift ;;
        --json) JSON=1; shift ;;
        --no-login) NO_LOGIN=1; shift ;;
        --flat) FLAT=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --print-paths) PRINT_PATHS=1; shift ;;
        --unfinalised) UNFINALISED=1; shift ;;
        --force) FORCE=1; shift ;;
        --active) ACTIVE=1; shift ;;
        --anonymous) ANONYMOUS=1; shift ;;
        -*) die "$EXIT_USAGE" "unknown option $1" ;;
        *)
            if [ -z "$STATION" ]; then
                STATION=${1^^}
            elif [ -z "$ADDRESS" ]; then
                ADDRESS=$1
            else
                die "$EXIT_USAGE" "unexpected argument $1"
            fi
            shift
            ;;
    esac
done

CFG=${CFG_OPT:-${PRX5_STATIONS_CFG:-$DEFAULT_CFG}}
if [ "$COMMAND" != stations ]; then
    [[ $STATION =~ ^[A-Z0-9]{4}$ ]] || die "$EXIT_USAGE" "expected a four-character station ID, got '$STATION'"
fi

"cmd_$COMMAND"
