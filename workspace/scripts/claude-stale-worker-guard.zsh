#!/bin/zsh

set -eu
set -o pipefail

readonly claude_app_executable='/Applications/Claude.app/Contents/MacOS/Claude'
readonly disclaimer_executable='/Applications/Claude.app/Contents/Helpers/disclaimer'
readonly claude_code_root="${OPENCLAW_CLAUDE_CODE_ROOT:?required absolute Claude code root}"
readonly receipt_root="${OPENCLAW_CLAUDE_WORKER_RECEIPTS:?required absolute worker receipt root}"
[[ "$claude_code_root" == /* && "$receipt_root" == /* ]] || exit 64

readonly log_tag='com.openclaw.claude-stale-worker-guard'
readonly expected_identifier='com.anthropic.claude-code'
readonly expected_team_identifier='Q6L2SF6YDW'
readonly current_uid=$(/usr/bin/id -u)
readonly receipt_schema_version=4
readonly max_prepared_generation=1024
readonly program_name="${0:t}"
typeset -g mode=''

autoload -Uz is-at-least

log() {
  local message="$1"
  /usr/bin/logger -t "$log_tag" -- "$message" 2>/dev/null || true
  print -r -- "$message"
}

valid_release_version() {
  [[ "$1" =~ '^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)$' ]]
}

valid_signed_executable() {
  local executable="$1"
  local bundle version_directory directory_version short_version bundle_version signature_info

  [[ -f "$executable" && ! -L "$executable" && -x "$executable" ]] || return 1
  [[ "${executable:A}" == "$executable" ]] || return 1
  bundle="${executable%/Contents/MacOS/claude}"
  [[ "$bundle" != "$executable" && "$bundle" == */claude.app ]] || return 1
  [[ -d "$bundle" && ! -L "$bundle" && "${bundle:A}" == "$bundle" ]] || return 1
  version_directory="${bundle:h}"
  directory_version="${version_directory:t}"
  valid_release_version "$directory_version" || return 1

  /usr/bin/codesign --verify --deep --strict "$bundle" >/dev/null 2>&1 || return 1
  short_version=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
    "$bundle/Contents/Info.plist" 2>/dev/null) || return 1
  bundle_version=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' \
    "$bundle/Contents/Info.plist" 2>/dev/null) || return 1
  [[ "$short_version" == "$directory_version" && "$bundle_version" == "$directory_version" ]] || return 1

  signature_info=$(/usr/bin/codesign -dv --verbose=4 "$bundle" 2>&1) || return 1
  print -r -- "$signature_info" | /usr/bin/grep -Fqx -- "Identifier=$expected_identifier" || return 1
  print -r -- "$signature_info" | /usr/bin/grep -Fqx -- "TeamIdentifier=$expected_team_identifier" || return 1
}

executable_identity() {
  /usr/bin/stat -f '%d:%i:%z:%m' -- "$1" 2>/dev/null
}

unlinked_executable_identity() {
  local worker_pid="$1"
  local worker_executable="$2"

  /usr/sbin/lsof -a -p "$worker_pid" +L1 -FfDin 2>/dev/null | /usr/bin/awk \
    -v target="n$worker_executable" '
      /^f/ { descriptor = $0; device = ""; inode = "" }
      /^D/ { device = $0 }
      /^i/ { inode = $0 }
      /^n/ && $0 == target && descriptor == "ftxt" && device != "" && inode != "" {
        print descriptor ":" device ":" inode
        found = 1
      }
      END { if (!found) exit 1 }
    '
}

running_executable_inode() {
  local worker_pid="$1"
  local worker_executable="$2"

  /usr/sbin/lsof -a -p "$worker_pid" -Ffin 2>/dev/null | /usr/bin/awk \
    -v target="n$worker_executable" '
      /^f/ { descriptor = $0; inode = "" }
      /^i/ { inode = substr($0, 2) }
      /^n/ && $0 == target && descriptor == "ftxt" && inode ~ /^[0-9]+$/ {
        print inode
        found = 1
      }
      END { if (!found) exit 1 }
    '
}

find_newer_executable() {
  local stale_version="$1"
  local candidate candidate_version candidate_identity
  local newest_version=''
  local newest_executable=''
  local newest_identity=''

  for candidate in "$claude_code_root"/*/claude.app/Contents/MacOS/claude(N); do
    candidate_version="${candidate#$claude_code_root/}"
    candidate_version="${candidate_version%%/*}"
    valid_release_version "$candidate_version" || continue
    [[ "$candidate_version" != "$stale_version" ]] || continue
    is-at-least "$stale_version" "$candidate_version" || continue
    valid_signed_executable "$candidate" || continue
    candidate_identity=$(executable_identity "$candidate") || continue

    if [[ -z "$newest_version" ]] || is-at-least "$newest_version" "$candidate_version"; then
      newest_version="$candidate_version"
      newest_executable="$candidate"
      newest_identity="$candidate_identity"
    fi
  done

  [[ -n "$newest_executable" ]] || return 1
  print -r -- "$newest_version"$'\t'"$newest_executable"$'\t'"$newest_identity"
}

valid_parent_chain() {
  local worker_pid="$1"
  local disclaimer_pid="$2"
  local worker_executable="$3"
  local expected_disclaimer_record="${4:-}"
  local expected_claude_record="${5:-}"
  local disclaimer_record disclaimer_uid claude_pid disclaimer_command
  local claude_record claude_uid claude_ppid claude_command

  disclaimer_record=$(/bin/ps -p "$disclaimer_pid" -o uid=,ppid=,command= 2>/dev/null) || return 1
  read -r disclaimer_uid claude_pid disclaimer_command <<< "$disclaimer_record"
  [[ "$disclaimer_uid" == "$current_uid" && "$claude_pid" == <-> ]] || return 1
  [[ "$disclaimer_command" == "$disclaimer_executable -- $worker_executable "* ]] || return 1

  claude_record=$(/bin/ps -p "$claude_pid" -o uid=,ppid=,command= 2>/dev/null) || return 1
  read -r claude_uid claude_ppid claude_command <<< "$claude_record"
  [[ "$claude_uid" == "$current_uid" && "$claude_ppid" == 1 ]] || return 1
  [[ "$claude_command" == "$claude_app_executable" || "$claude_command" == "$claude_app_executable "* ]] || return 1

  [[ -z "$expected_disclaimer_record" || "$disclaimer_record" == "$expected_disclaimer_record" ]] || return 1
  [[ -z "$expected_claude_record" || "$claude_record" == "$expected_claude_record" ]] || return 1

  print -r -- "$disclaimer_record"$'\t'"$claude_record"
}

ensure_receipt_root() {
  local owner_uid

  if [[ -e "$receipt_root" ]]; then
    [[ -d "$receipt_root" && ! -L "$receipt_root" ]] || return 1
    owner_uid=$(/usr/bin/stat -f '%u' -- "$receipt_root") || return 1
    [[ "$owner_uid" == "$current_uid" ]] || return 1
  else
    /bin/mkdir -m 700 -- "$receipt_root" 2>/dev/null || {
      [[ -d "$receipt_root" && ! -L "$receipt_root" ]] || return 1
    }
  fi
  [[ -d "$receipt_root" && ! -L "$receipt_root" ]] || return 1
  owner_uid=$(/usr/bin/stat -f '%u' -- "$receipt_root") || return 1
  [[ "$owner_uid" == "$current_uid" ]] || return 1
  /bin/chmod 700 "$receipt_root" || return 1
}

hash_string() {
  /usr/bin/printf '%s' "$1" | /usr/bin/shasum -a 256 | /usr/bin/awk '{ print $1 }'
}

hash_file() {
  /usr/bin/shasum -a 256 -- "$1" 2>/dev/null | /usr/bin/awk '{ print $1 }'
}

receipt_value() {
  local receipt="$1"
  local key="$2"

  /usr/bin/awk -v prefix="$key=" '
    index($0, prefix) == 1 {
      count += 1
      value = substr($0, length(prefix) + 1)
    }
    END {
      if (count != 1) exit 1
      print value
    }
  ' "$receipt"
}

valid_receipt_file() {
  local receipt="$1"
  local owner_uid mode_value

  [[ -f "$receipt" && ! -L "$receipt" ]] || return 1
  owner_uid=$(/usr/bin/stat -f '%u' -- "$receipt" 2>/dev/null) || return 1
  mode_value=$(/usr/bin/stat -f '%Lp' -- "$receipt" 2>/dev/null) || return 1
  [[ "$owner_uid" == "$current_uid" && "$mode_value" == 400 ]]
}

# Publish a complete receipt with create-once semantics. The hard link is the
# atomic commit; a pre-existing target is reported as status 2, not overwritten.
atomic_publish_content() {
  local target="$1"
  local content="$2"
  local target_directory temporary

  target_directory="${target:h}"
  [[ -d "$target_directory" && ! -L "$target_directory" ]] || return 1
  temporary=$(/usr/bin/mktemp "$target_directory/.receipt.XXXXXX") || return 1
  if ! /usr/bin/printf '%s\n' "$content" > "$temporary"; then
    /bin/rm -f -- "$temporary" || true
    return 1
  fi
  /bin/chmod 400 "$temporary" || {
    /bin/rm -f -- "$temporary" || true
    return 1
  }
  if /bin/ln -- "$temporary" "$target" 2>/dev/null; then
    # Publication succeeded. A cleanup failure must not turn the committed
    # receipt into an apparent failure that could authorize another writer.
    /bin/rm -f -- "$temporary" || true
    return 0
  fi
  /bin/rm -f -- "$temporary" || true
  [[ -f "$target" && ! -L "$target" ]] && return 2
  return 1
}

prepared_path() {
  print -r -- "$receipt_root/$1.prepared.$2.receipt"
}

valid_prepared_receipt() {
  local receipt="$1"
  local incident_key="$2"
  local generation="$3"
  local previous_hash guard_pid guard_uid guard_start guard_start_hash guard_command_hash
  local session_hash old_pid old_start old_start_hash old_command_hash old_parent_pid
  local old_executable old_unlinked_identity old_version replacement_version
  local replacement_executable replacement_identity disclaimer_record_hash claude_record_hash
  local computed_guard_start_hash computed_old_start_hash computed_incident_key

  valid_receipt_file "$receipt" || return 1
  [[ "$(receipt_value "$receipt" schema)" == "$receipt_schema_version" ]] || return 1
  [[ "$(receipt_value "$receipt" type)" == prepared ]] || return 1
  [[ "$(receipt_value "$receipt" incident_key)" == "$incident_key" ]] || return 1
  [[ "$(receipt_value "$receipt" generation)" == "$generation" ]] || return 1

  previous_hash=$(receipt_value "$receipt" previous_prepared_sha256) || return 1
  guard_pid=$(receipt_value "$receipt" guard_pid) || return 1
  guard_uid=$(receipt_value "$receipt" guard_uid) || return 1
  guard_start=$(receipt_value "$receipt" guard_start) || return 1
  guard_start_hash=$(receipt_value "$receipt" guard_start_sha256) || return 1
  guard_command_hash=$(receipt_value "$receipt" guard_command_sha256) || return 1
  session_hash=$(receipt_value "$receipt" session_sha256) || return 1
  old_pid=$(receipt_value "$receipt" old_pid) || return 1
  old_start=$(receipt_value "$receipt" old_start) || return 1
  old_start_hash=$(receipt_value "$receipt" old_start_sha256) || return 1
  old_command_hash=$(receipt_value "$receipt" old_command_sha256) || return 1
  old_parent_pid=$(receipt_value "$receipt" old_parent_pid) || return 1
  old_executable=$(receipt_value "$receipt" old_executable) || return 1
  old_unlinked_identity=$(receipt_value "$receipt" old_unlinked_identity) || return 1
  old_version=$(receipt_value "$receipt" old_version) || return 1
  disclaimer_record_hash=$(receipt_value "$receipt" disclaimer_record_sha256) || return 1
  claude_record_hash=$(receipt_value "$receipt" claude_record_sha256) || return 1
  replacement_version=$(receipt_value "$receipt" replacement_version) || return 1
  replacement_executable=$(receipt_value "$receipt" replacement_executable) || return 1
  replacement_identity=$(receipt_value "$receipt" replacement_identity) || return 1

  [[ "$generation" =~ '^(0|[1-9][0-9]*)$' ]] || return 1
  (( ${#generation} <= 4 )) || return 1
  (( generation <= max_prepared_generation )) || return 1
  if [[ "$generation" == 0 ]]; then
    [[ "$previous_hash" == none ]] || return 1
  else
    [[ "$previous_hash" =~ '^[0-9a-f]{64}$' ]] || return 1
  fi
  [[ "$guard_pid" == <-> && "$guard_uid" == "$current_uid" && -n "$guard_start" ]] || return 1
  [[ "$guard_start_hash" =~ '^[0-9a-f]{64}$' && "$guard_command_hash" =~ '^[0-9a-f]{64}$' ]] || return 1
  [[ "$session_hash" =~ '^[0-9a-f]{64}$' ]] || return 1
  [[ "$old_pid" == <-> && "$old_parent_pid" == <-> && -n "$old_start" ]] || return 1
  [[ "$old_start_hash" =~ '^[0-9a-f]{64}$' && "$old_command_hash" =~ '^[0-9a-f]{64}$' ]] || return 1
  [[ "$disclaimer_record_hash" =~ '^[0-9a-f]{64}$' && "$claude_record_hash" =~ '^[0-9a-f]{64}$' ]] || return 1
  [[ "$old_unlinked_identity" =~ '^ftxt:D(0x)?[0-9A-Fa-f]+:i[0-9]+$' ]] || return 1
  valid_release_version "$old_version" || return 1
  valid_release_version "$replacement_version" || return 1
  [[ "$old_executable" == "$claude_code_root/$old_version/claude.app/Contents/MacOS/claude" ]] || return 1
  [[ "$replacement_executable" == "$claude_code_root/$replacement_version/claude.app/Contents/MacOS/claude" ]] || return 1
  [[ "$replacement_identity" =~ '^[0-9]+:[0-9]+:[0-9]+:[0-9]+$' ]] || return 1

  computed_guard_start_hash=$(hash_string "$guard_start") || return 1
  computed_old_start_hash=$(hash_string "$old_start") || return 1
  computed_incident_key=$(hash_string \
    "$old_pid|$old_start_hash|$old_unlinked_identity|$old_version") || return 1
  [[ "$computed_guard_start_hash" == "$guard_start_hash" \
    && "$computed_old_start_hash" == "$old_start_hash" \
    && "$computed_incident_key" == "$incident_key" ]]
}

# Print: generation<TAB>path<TAB>sha256. Return 1 when no preparation exists,
# and 2 when an append-only chain is malformed or discontinuous.
latest_prepared_receipt() {
  local incident_key="$1"
  local prefix="$receipt_root/$incident_key.prepared."
  local candidate suffix generation max_generation=-1 path previous_hash='none' actual_previous current_hash
  local generation_index=0

  for candidate in "$prefix"*.receipt(N); do
    suffix="${candidate#$prefix}"
    generation="${suffix%.receipt}"
    [[ "$generation" =~ '^(0|[1-9][0-9]*)$' ]] || return 2
    (( ${#generation} <= 4 )) || return 2
    (( generation <= max_prepared_generation )) || return 2
    if (( generation > max_generation )); then
      max_generation=$generation
    fi
  done
  (( max_generation >= 0 )) || return 1

  while (( generation_index <= max_generation )); do
    path=$(prepared_path "$incident_key" "$generation_index")
    valid_prepared_receipt "$path" "$incident_key" "$generation_index" || return 2
    actual_previous=$(receipt_value "$path" previous_prepared_sha256) || return 2
    [[ "$actual_previous" == "$previous_hash" ]] || return 2
    current_hash=$(hash_file "$path") || return 2
    [[ "$current_hash" =~ '^[0-9a-f]{64}$' ]] || return 2
    previous_hash="$current_hash"
    (( generation_index += 1 ))
  done
  print -r -- "$max_generation"$'\t'"$(prepared_path "$incident_key" "$max_generation")"$'\t'"$previous_hash"
}

# Print same, gone, or indeterminate for the guard process recorded in a
# prepared receipt. Command mismatch alone is not proof of death because argv
# is mutable; PID absence or a different start identity is required.
prepared_owner_state() {
  local receipt="$1"
  local guard_pid guard_uid guard_start guard_command_hash
  local current_start current_guard_uid current_command current_command_hash

  guard_pid=$(receipt_value "$receipt" guard_pid) || return 1
  guard_uid=$(receipt_value "$receipt" guard_uid) || return 1
  guard_start=$(receipt_value "$receipt" guard_start) || return 1
  guard_command_hash=$(receipt_value "$receipt" guard_command_sha256) || return 1

  if current_start=$(/bin/ps -p "$guard_pid" -o lstart= 2>/dev/null); then
    if [[ "$current_start" != "$guard_start" ]]; then
      print -r -- gone
      return 0
    fi
    current_guard_uid=$(/bin/ps -p "$guard_pid" -o uid= 2>/dev/null | /usr/bin/tr -d ' ') || {
      print -r -- indeterminate
      return 0
    }
    current_command=$(/bin/ps -p "$guard_pid" -o command= 2>/dev/null) || {
      print -r -- indeterminate
      return 0
    }
    current_command_hash=$(hash_string "$current_command") || return 1
    if [[ "$current_guard_uid" == "$guard_uid" && "$current_command_hash" == "$guard_command_hash" ]]; then
      print -r -- same
    else
      print -r -- indeterminate
    fi
    return 0
  fi

  if /bin/kill -0 "$guard_pid" 2>/dev/null; then
    print -r -- indeterminate
  else
    print -r -- gone
  fi
}

build_prepared_content() {
  local incident_key="$1"
  local generation="$2"
  local previous_hash="$3"
  local epoch="$4"
  local guard_pid="$5"
  local guard_start="$6"
  local guard_start_hash="$7"
  local guard_command_hash="$8"
  local session_hash="$9"
  local old_pid="${10}"
  local old_start="${11}"
  local old_start_hash="${12}"
  local old_command_hash="${13}"
  local old_parent_pid="${14}"
  local old_executable="${15}"
  local old_unlinked_identity="${16}"
  local old_version="${17}"
  local replacement_version="${18}"
  local replacement_executable="${19}"
  local replacement_identity="${20}"
  local disclaimer_record_hash="${21}"
  local claude_record_hash="${22}"

  /usr/bin/printf \
    'schema=%s\ntype=prepared\nincident_key=%s\ngeneration=%s\nprevious_prepared_sha256=%s\ncreated_epoch=%s\nguard_pid=%s\nguard_uid=%s\nguard_start=%s\nguard_start_sha256=%s\nguard_command_sha256=%s\nsession_sha256=%s\nold_pid=%s\nold_start=%s\nold_start_sha256=%s\nold_command_sha256=%s\nold_parent_pid=%s\nold_executable=%s\nold_unlinked_identity=%s\nold_version=%s\ndisclaimer_record_sha256=%s\nclaude_record_sha256=%s\nreplacement_version=%s\nreplacement_executable=%s\nreplacement_identity=%s' \
    "$receipt_schema_version" "$incident_key" "$generation" "$previous_hash" "$epoch" \
    "$guard_pid" "$current_uid" "$guard_start" "$guard_start_hash" "$guard_command_hash" \
    "$session_hash" "$old_pid" "$old_start" "$old_start_hash" "$old_command_hash" \
    "$old_parent_pid" "$old_executable" "$old_unlinked_identity" "$old_version" \
    "$disclaimer_record_hash" "$claude_record_hash" "$replacement_version" \
    "$replacement_executable" "$replacement_identity"
}

build_term_attempt_content() {
  local incident_key="$1"
  local prepared_generation="$2"
  local prepared_hash="$3"
  local epoch="$4"
  local guard_pid="$5"
  local guard_start_hash="$6"

  /usr/bin/printf \
    'schema=%s\ntype=term_attempt\nincident_key=%s\nprepared_generation=%s\nprepared_sha256=%s\ncreated_epoch=%s\nguard_pid=%s\nguard_start_sha256=%s\nsignal=TERM' \
    "$receipt_schema_version" "$incident_key" "$prepared_generation" "$prepared_hash" \
    "$epoch" "$guard_pid" "$guard_start_hash"
}

valid_term_attempt_receipt() {
  local receipt="$1"
  local incident_key="$2"
  local prepared_generation prepared_hash guard_pid guard_start_hash prepared_receipt actual_hash
  local prepared_guard_pid prepared_guard_start_hash

  valid_receipt_file "$receipt" || return 1
  [[ "$(receipt_value "$receipt" schema)" == "$receipt_schema_version" ]] || return 1
  [[ "$(receipt_value "$receipt" type)" == term_attempt ]] || return 1
  [[ "$(receipt_value "$receipt" incident_key)" == "$incident_key" ]] || return 1
  [[ "$(receipt_value "$receipt" signal)" == TERM ]] || return 1
  prepared_generation=$(receipt_value "$receipt" prepared_generation) || return 1
  prepared_hash=$(receipt_value "$receipt" prepared_sha256) || return 1
  guard_pid=$(receipt_value "$receipt" guard_pid) || return 1
  guard_start_hash=$(receipt_value "$receipt" guard_start_sha256) || return 1
  [[ "$prepared_generation" =~ '^(0|[1-9][0-9]*)$' ]] || return 1
  (( ${#prepared_generation} <= 4 )) || return 1
  (( prepared_generation <= max_prepared_generation )) || return 1
  [[ "$prepared_hash" =~ '^[0-9a-f]{64}$' && "$guard_pid" == <-> \
    && "$guard_start_hash" =~ '^[0-9a-f]{64}$' ]] || return 1
  prepared_receipt=$(prepared_path "$incident_key" "$prepared_generation")
  valid_prepared_receipt "$prepared_receipt" "$incident_key" "$prepared_generation" || return 1
  actual_hash=$(hash_file "$prepared_receipt") || return 1
  prepared_guard_pid=$(receipt_value "$prepared_receipt" guard_pid) || return 1
  prepared_guard_start_hash=$(receipt_value "$prepared_receipt" guard_start_sha256) || return 1
  [[ "$actual_hash" == "$prepared_hash" && "$guard_pid" == "$prepared_guard_pid" \
    && "$guard_start_hash" == "$prepared_guard_start_hash" ]]
}

build_state_content() {
  local record_type="$1"
  local incident_key="$2"
  local term_attempt_hash="$3"
  local epoch="$4"
  local state="$5"
  local old_state="${6:-}"
  local observed_pid="${7:-}"
  local observed_version="${8:-}"
  local observed_start="${9:-}"

  /usr/bin/printf \
    'schema=%s\ntype=%s\nincident_key=%s\nterm_attempt_sha256=%s\ncreated_epoch=%s\nstate=%s\nold_state=%s\nobserved_pid=%s\nobserved_version=%s\nobserved_start=%s' \
    "$receipt_schema_version" "$record_type" "$incident_key" "$term_attempt_hash" \
    "$epoch" "$state" "$old_state" "$observed_pid" "$observed_version" "$observed_start"
}

same_worker_still_running() {
  local worker_pid="$1"
  local disclaimer_pid="$2"
  local worker_start="$3"
  local worker_command="$4"
  local worker_uid current_disclaimer_pid current_start current_command

  worker_uid=$(/bin/ps -p "$worker_pid" -o uid= 2>/dev/null | /usr/bin/tr -d ' ') || return 1
  current_disclaimer_pid=$(/bin/ps -p "$worker_pid" -o ppid= 2>/dev/null | /usr/bin/tr -d ' ') || return 1
  current_start=$(/bin/ps -p "$worker_pid" -o lstart= 2>/dev/null) || return 1
  current_command=$(/bin/ps -p "$worker_pid" -o command= 2>/dev/null) || return 1
  [[ "$worker_uid" == "$current_uid" && "$current_disclaimer_pid" == "$disclaimer_pid" \
    && "$current_start" == "$worker_start" && "$current_command" == "$worker_command" ]]
}

target_still_qualifies() {
  local worker_pid="$1"
  local disclaimer_pid="$2"
  local worker_start="$3"
  local worker_command="$4"
  local worker_executable="$5"
  local expected_unlinked_identity="$6"
  local disclaimer_record="$7"
  local claude_record="$8"
  local replacement_executable="$9"
  local replacement_identity="${10}"
  local current_unlinked_identity current_replacement_identity

  valid_signed_executable "$replacement_executable" || return 1
  current_replacement_identity=$(executable_identity "$replacement_executable") || return 1
  [[ "$current_replacement_identity" == "$replacement_identity" ]] || return 1

  # End on the old process identity so the signal follows the safety-critical
  # revalidation as closely as macOS's PID-only signaling permits.
  same_worker_still_running "$worker_pid" "$disclaimer_pid" "$worker_start" "$worker_command" || return 1
  [[ ! -e "$worker_executable" ]] || return 1
  current_unlinked_identity=$(unlinked_executable_identity "$worker_pid" "$worker_executable") || return 1
  [[ "$current_unlinked_identity" == "$expected_unlinked_identity" ]] || return 1
  valid_parent_chain "$worker_pid" "$disclaimer_pid" "$worker_executable" \
    "$disclaimer_record" "$claude_record" >/dev/null || return 1
  same_worker_still_running "$worker_pid" "$disclaimer_pid" "$worker_start" "$worker_command"
}

old_worker_state() {
  local worker_pid="$1"
  local worker_start="$2"
  local worker_executable="$3"
  local expected_unlinked_identity="$4"
  local current_start current_unlinked_identity

  if current_start=$(/bin/ps -p "$worker_pid" -o lstart= 2>/dev/null); then
    if [[ "$current_start" != "$worker_start" ]]; then
      print -r -- 'gone'
      return 0
    fi
    if current_unlinked_identity=$(unlinked_executable_identity "$worker_pid" "$worker_executable") \
      && [[ "$current_unlinked_identity" == "$expected_unlinked_identity" ]]; then
      print -r -- 'same'
    else
      print -r -- 'indeterminate'
    fi
    return 0
  fi

  if /bin/kill -0 "$worker_pid" 2>/dev/null; then
    print -r -- 'indeterminate'
  else
    print -r -- 'gone'
  fi
}

find_resumed_worker() {
  local session_hash="$1"
  local minimum_version="$2"
  local old_worker_pid="$3"
  local old_worker_start_hash="$4"
  local worker_pid disclaimer_pid worker_command worker_remainder version executable
  local worker_start worker_start_hash candidate_session candidate_session_hash
  local executable_inode running_inode identity_before identity_after

  while read -r worker_pid disclaimer_pid worker_command; do
    [[ "$worker_pid" == <-> && "$disclaimer_pid" == <-> ]] || continue
    [[ "$worker_command" == "$claude_code_root"/*/claude.app/Contents/MacOS/claude\ * ]] || continue
    [[ "$worker_command" =~ '(^| )--resume=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})( |$)' ]] || continue
    candidate_session="$match[2]"
    candidate_session_hash=$(hash_string "${candidate_session:l}") || continue
    [[ "$candidate_session_hash" == "$session_hash" ]] || continue
    worker_remainder="${worker_command#$claude_code_root/}"
    version="${worker_remainder%%/*}"
    valid_release_version "$version" || continue
    is-at-least "$minimum_version" "$version" || continue
    executable="$claude_code_root/$version/claude.app/Contents/MacOS/claude"
    [[ "$worker_command" == "$executable "* ]] || continue
    valid_signed_executable "$executable" || continue
    worker_start=$(/bin/ps -p "$worker_pid" -o lstart= 2>/dev/null) || continue
    worker_start_hash=$(hash_string "$worker_start") || continue
    [[ "$worker_pid" != "$old_worker_pid" || "$worker_start_hash" != "$old_worker_start_hash" ]] || continue
    same_worker_still_running "$worker_pid" "$disclaimer_pid" "$worker_start" "$worker_command" || continue
    valid_parent_chain "$worker_pid" "$disclaimer_pid" "$executable" >/dev/null || continue
    identity_before=$(executable_identity "$executable") || continue
    executable_inode=$(/usr/bin/stat -f '%i' -- "$executable" 2>/dev/null) || continue
    running_inode=$(running_executable_inode "$worker_pid" "$executable") || continue
    [[ "$running_inode" == "$executable_inode" ]] || continue
    identity_after=$(executable_identity "$executable") || continue
    [[ "$identity_after" == "$identity_before" ]] || continue
    same_worker_still_running "$worker_pid" "$disclaimer_pid" "$worker_start" "$worker_command" || continue
    print -r -- "$worker_pid"$'\t'"$version"$'\t'"$worker_start"
    return 0
  done < <(/bin/ps -axo pid=,ppid=,command=)
  return 1
}

reconcile_term_attempt_receipt() {
  local term_attempt="$1"
  local incident_key="$2"
  local prepared_generation prepared_receipt term_attempt_hash
  local session_hash old_pid old_start old_start_hash old_executable old_unlinked_identity replacement_version
  local old_state resumed_record observed_pid='' observed_version='' observed_start='' resumed_remainder
  local observation_state observation_fingerprint observation_path observation_content
  local final_path final_content current_epoch publish_status

  valid_term_attempt_receipt "$term_attempt" "$incident_key" || {
    log "did not reconcile malformed Claude worker term-attempt incident=$incident_key"
    return 1
  }
  prepared_generation=$(receipt_value "$term_attempt" prepared_generation) || return 1
  prepared_receipt=$(prepared_path "$incident_key" "$prepared_generation")
  term_attempt_hash=$(hash_file "$term_attempt") || return 1
  session_hash=$(receipt_value "$prepared_receipt" session_sha256) || return 1
  old_pid=$(receipt_value "$prepared_receipt" old_pid) || return 1
  old_start=$(receipt_value "$prepared_receipt" old_start) || return 1
  old_start_hash=$(receipt_value "$prepared_receipt" old_start_sha256) || return 1
  old_executable=$(receipt_value "$prepared_receipt" old_executable) || return 1
  old_unlinked_identity=$(receipt_value "$prepared_receipt" old_unlinked_identity) || return 1
  replacement_version=$(receipt_value "$prepared_receipt" replacement_version) || return 1

  old_state=$(old_worker_state "$old_pid" "$old_start" "$old_executable" "$old_unlinked_identity") || return 1
  resumed_record=$(find_resumed_worker "$session_hash" "$replacement_version" "$old_pid" "$old_start_hash" 2>/dev/null || true)
  if [[ -n "$resumed_record" ]]; then
    observed_pid="${resumed_record%%$'\t'*}"
    resumed_remainder="${resumed_record#*$'\t'}"
    observed_version="${resumed_remainder%%$'\t'*}"
    observed_start="${resumed_remainder#*$'\t'}"
  fi

  if [[ "$old_state" == gone && -n "$resumed_record" ]]; then
    observation_state='replacement_observed'
  elif [[ "$old_state" == gone ]]; then
    observation_state='old_gone_no_resume_pending'
  elif [[ "$old_state" == same && -n "$resumed_record" ]]; then
    observation_state='old_same_replacement_observed'
  elif [[ "$old_state" == same ]]; then
    observation_state='old_same_after_term_attempt'
  elif [[ -n "$resumed_record" ]]; then
    observation_state='old_indeterminate_replacement_observed'
  else
    observation_state='old_indeterminate'
  fi

  current_epoch=$(/bin/date '+%s') || return 1
  observation_fingerprint=$(hash_string "$observation_state|$old_state|$observed_pid|$observed_version|$observed_start") || return 1
  [[ "$observation_fingerprint" =~ '^[0-9a-f]{64}$' ]] || return 1
  observation_path="$receipt_root/$incident_key.observation.$observation_fingerprint.receipt"
  observation_content=$(build_state_content observation "$incident_key" "$term_attempt_hash" "$current_epoch" \
    "$observation_state" "$old_state" "$observed_pid" "$observed_version" "$observed_start") || return 1
  if atomic_publish_content "$observation_path" "$observation_content"; then
    log "reconciled Claude worker incident=$incident_key state=$observation_state old_state=$old_state"
  else
    publish_status=$?
    (( publish_status == 2 )) || {
      log "could not publish Claude worker observation incident=$incident_key state=$observation_state"
      return 1
    }
  fi

  if [[ "$observation_state" == replacement_observed ]]; then
    final_path="$receipt_root/$incident_key.final.receipt"
    final_content=$(build_state_content final "$incident_key" "$term_attempt_hash" "$current_epoch" \
      replacement_observed "$old_state" "$observed_pid" "$observed_version" "$observed_start") || return 1
    if atomic_publish_content "$final_path" "$final_content"; then
      :
    else
      publish_status=$?
      (( publish_status == 2 )) || return 1
    fi
  fi
}

reconcile_all_term_attempts() {
  local term_attempt incident_key

  [[ -d "$receipt_root" && ! -L "$receipt_root" ]] || return 0
  for term_attempt in "$receipt_root"/*.term-attempt.receipt(N); do
    incident_key="${term_attempt:t}"
    incident_key="${incident_key%.term-attempt.receipt}"
    [[ "$incident_key" =~ '^[0-9a-f]{64}$' ]] || {
      log "ignored malformed Claude worker term-attempt filename path=$term_attempt"
      continue
    }
    reconcile_term_attempt_receipt "$term_attempt" "$incident_key" || true
  done
}

inspect_worker() {
  local worker_pid="$1"
  local disclaimer_pid="$2"
  local worker_command="$3"
  local worker_uid worker_start worker_remainder stale_version worker_executable session_id
  local unlinked_identity parent_records disclaimer_record claude_record
  local replacement_record replacement_version replacement_executable replacement_identity
  local session_hash worker_start_hash worker_command_hash disclaimer_record_hash claude_record_hash
  local incident_key current_epoch guard_start guard_start_hash guard_command guard_command_hash
  local latest_record latest_status prepared_generation prepared_receipt prepared_hash previous_hash owner_state
  local next_generation prepared_content publish_status verification_record
  local term_attempt term_attempt_content term_attempt_hash term_call term_call_content signal_status
  local resumed_record wait_iteration worker_state

  [[ "$worker_pid" == <-> && "$disclaimer_pid" == <-> ]] || return 0
  [[ "$worker_command" == "$claude_code_root"/*/claude.app/Contents/MacOS/claude\ * ]] || return 0

  worker_remainder="${worker_command#$claude_code_root/}"
  stale_version="${worker_remainder%%/*}"
  valid_release_version "$stale_version" || return 0
  worker_executable="$claude_code_root/$stale_version/claude.app/Contents/MacOS/claude"
  [[ "$worker_command" == "$worker_executable "* ]] || return 0
  [[ "$worker_command" =~ '--resume=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})( |$)' ]] || return 0
  session_id="$match[1]"

  worker_uid=$(/bin/ps -p "$worker_pid" -o uid= 2>/dev/null | /usr/bin/tr -d ' ') || return 0
  worker_start=$(/bin/ps -p "$worker_pid" -o lstart= 2>/dev/null) || return 0
  [[ "$worker_uid" == "$current_uid" ]] || return 0
  [[ ! -e "$worker_executable" ]] || return 0

  unlinked_identity=$(unlinked_executable_identity "$worker_pid" "$worker_executable") || return 0

  parent_records=$(valid_parent_chain "$worker_pid" "$disclaimer_pid" "$worker_executable") || return 0
  disclaimer_record="${parent_records%%$'\t'*}"
  claude_record="${parent_records#*$'\t'}"

  replacement_record=$(find_newer_executable "$stale_version") || return 0
  replacement_version="${replacement_record%%$'\t'*}"
  replacement_record="${replacement_record#*$'\t'}"
  replacement_executable="${replacement_record%%$'\t'*}"
  replacement_identity="${replacement_record#*$'\t'}"

  # Debounce the updater and then revalidate every process and file identity.
  /bin/sleep 3
  target_still_qualifies "$worker_pid" "$disclaimer_pid" "$worker_start" "$worker_command" \
    "$worker_executable" "$unlinked_identity" "$disclaimer_record" "$claude_record" \
    "$replacement_executable" "$replacement_identity" || return 0

  if [[ "$mode" == '--dry-run' ]]; then
    print -r -- "would refresh stale Claude Code worker pid=$worker_pid old_version=$stale_version replacement_version=$replacement_version"
    return 0
  fi

  ensure_receipt_root || {
    log "did not signal stale Claude Code worker pid=$worker_pid because the receipt directory is unsafe or unwritable"
    return 0
  }
  session_hash=$(hash_string "${session_id:l}") || return 0
  worker_start_hash=$(hash_string "$worker_start") || return 0
  worker_command_hash=$(hash_string "$worker_command") || return 0
  disclaimer_record_hash=$(hash_string "$disclaimer_record") || return 0
  claude_record_hash=$(hash_string "$claude_record") || return 0
  # The create-once TERM barrier is keyed only by immutable incident identity.
  # The command hash remains a revalidated attribute but cannot mint a second
  # signal authority if a process mutates its argv after the first barrier.
  incident_key=$(hash_string "$worker_pid|$worker_start_hash|$unlinked_identity|$stale_version") || return 0
  [[ "$incident_key" =~ '^[0-9a-f]{64}$' ]] || return 0

  term_attempt="$receipt_root/$incident_key.term-attempt.receipt"
  if [[ -e "$term_attempt" || -L "$term_attempt" ]]; then
    reconcile_term_attempt_receipt "$term_attempt" "$incident_key" || true
    return 0
  fi

  latest_record=''
  if latest_record=$(latest_prepared_receipt "$incident_key"); then
    prepared_generation="${latest_record%%$'\t'*}"
    latest_record="${latest_record#*$'\t'}"
    prepared_receipt="${latest_record%%$'\t'*}"
    prepared_hash="${latest_record#*$'\t'}"
    owner_state=$(prepared_owner_state "$prepared_receipt") || return 0
    case "$owner_state" in
      gone)
        (( prepared_generation < max_prepared_generation )) || {
          log "did not recover prepared Claude worker incident=$incident_key because its generation limit was reached"
          return 0
        }
        next_generation=$(( prepared_generation + 1 ))
        previous_hash="$prepared_hash"
        ;;
      same)
        return 0
        ;;
      *)
        log "did not recover prepared Claude worker incident=$incident_key because the prior guard owner is indeterminate"
        return 0
        ;;
    esac
  else
    latest_status=$?
    if (( latest_status == 1 )); then
      next_generation=0
      previous_hash='none'
    else
      log "did not signal stale Claude Code worker pid=$worker_pid because its prepared receipt chain is malformed"
      return 0
    fi
  fi

  # Preparation can be recovered only while no TERM barrier exists and the
  # complete target still qualifies. Concurrent recoverers race on the same
  # append-only generation filename; exactly one hard-link publication wins.
  [[ ! -e "$term_attempt" && ! -L "$term_attempt" ]] || {
    reconcile_term_attempt_receipt "$term_attempt" "$incident_key" || true
    return 0
  }
  target_still_qualifies "$worker_pid" "$disclaimer_pid" "$worker_start" "$worker_command" \
    "$worker_executable" "$unlinked_identity" "$disclaimer_record" "$claude_record" \
    "$replacement_executable" "$replacement_identity" || return 0

  current_epoch=$(/bin/date '+%s')
  guard_start=$(/bin/ps -p $$ -o lstart= 2>/dev/null) || return 0
  guard_command=$(/bin/ps -p $$ -o command= 2>/dev/null) || return 0
  guard_start_hash=$(hash_string "$guard_start") || return 0
  guard_command_hash=$(hash_string "$guard_command") || return 0
  prepared_receipt=$(prepared_path "$incident_key" "$next_generation")
  prepared_content=$(build_prepared_content "$incident_key" "$next_generation" "$previous_hash" \
    "$current_epoch" $$ "$guard_start" "$guard_start_hash" "$guard_command_hash" "$session_hash" \
    "$worker_pid" "$worker_start" "$worker_start_hash" "$worker_command_hash" "$disclaimer_pid" \
    "$worker_executable" "$unlinked_identity" "$stale_version" "$replacement_version" \
    "$replacement_executable" "$replacement_identity" "$disclaimer_record_hash" "$claude_record_hash") || return 0
  if atomic_publish_content "$prepared_receipt" "$prepared_content"; then
    :
  else
    publish_status=$?
    (( publish_status == 2 )) || log "could not publish prepared Claude worker incident=$incident_key generation=$next_generation"
    return 0
  fi
  prepared_hash=$(hash_file "$prepared_receipt") || return 0
  [[ "$prepared_hash" =~ '^[0-9a-f]{64}$' ]] || return 0

  verification_record=$(latest_prepared_receipt "$incident_key") || return 0
  [[ "$verification_record" == "$next_generation"$'\t'"$prepared_receipt"$'\t'"$prepared_hash" ]] || return 0
  [[ ! -e "$term_attempt" && ! -L "$term_attempt" ]] || return 0

  # The durable, create-once term-attempt is the irreversible boundary. Once
  # present, every later run is observation-only even if this process crashes
  # before the syscall or cannot record its result.
  target_still_qualifies "$worker_pid" "$disclaimer_pid" "$worker_start" "$worker_command" \
    "$worker_executable" "$unlinked_identity" "$disclaimer_record" "$claude_record" \
    "$replacement_executable" "$replacement_identity" || return 0
  term_attempt_content=$(build_term_attempt_content "$incident_key" "$next_generation" "$prepared_hash" \
    "$current_epoch" $$ "$guard_start_hash") || return 0
  if atomic_publish_content "$term_attempt" "$term_attempt_content"; then
    :
  else
    publish_status=$?
    if (( publish_status == 2 )); then
      reconcile_term_attempt_receipt "$term_attempt" "$incident_key" || true
    else
      log "could not publish Claude worker term-attempt incident=$incident_key"
    fi
    return 0
  fi
  /bin/sync || {
    log "did not signal stale Claude Code worker pid=$worker_pid because the term-attempt could not be durably flushed"
    return 0
  }
  # Re-read the durable barrier and its complete prepared chain after sync.
  # Signaling is forbidden if either receipt changed, is malformed, or is no
  # longer the highest prepared generation.
  valid_term_attempt_receipt "$term_attempt" "$incident_key" || {
    log "did not signal stale Claude Code worker pid=$worker_pid because the durable term-attempt failed validation"
    return 0
  }
  verification_record=$(latest_prepared_receipt "$incident_key") || return 0
  [[ "$verification_record" == "$next_generation"$'\t'"$prepared_receipt"$'\t'"$prepared_hash" ]] || {
    log "did not signal stale Claude Code worker pid=$worker_pid because the prepared receipt chain changed after term-attempt"
    return 0
  }
  term_attempt_hash=$(hash_file "$term_attempt") || return 0

  # Receipt I/O is complete. End on the old process identity, then use the zsh
  # builtin so no child process widens macOS's unavoidable PID-only kill race.
  if ! target_still_qualifies "$worker_pid" "$disclaimer_pid" "$worker_start" "$worker_command" \
    "$worker_executable" "$unlinked_identity" "$disclaimer_record" "$claude_record" \
    "$replacement_executable" "$replacement_identity"; then
    term_call="$receipt_root/$incident_key.term-call.receipt"
    term_call_content=$(build_state_content term_call "$incident_key" "$term_attempt_hash" "$current_epoch" \
      not_called_final_revalidation) || return 0
    atomic_publish_content "$term_call" "$term_call_content" >/dev/null 2>&1 || true
    reconcile_term_attempt_receipt "$term_attempt" "$incident_key" || true
    return 0
  fi

  if builtin kill -TERM "$worker_pid"; then
    signal_status='accepted'
  else
    signal_status='failed'
  fi
  current_epoch=$(/bin/date '+%s') || return 0
  term_call="$receipt_root/$incident_key.term-call.receipt"
  term_call_content=$(build_state_content term_call "$incident_key" "$term_attempt_hash" "$current_epoch" \
    "$signal_status") || return 0
  if atomic_publish_content "$term_call" "$term_call_content"; then
    :
  else
    publish_status=$?
    (( publish_status == 2 )) || log "could not publish Claude worker TERM syscall result incident=$incident_key"
  fi
  if [[ "$signal_status" != accepted ]]; then
    log "SIGTERM failed for stale Claude Code worker pid=$worker_pid old_version=$stale_version"
    reconcile_term_attempt_receipt "$term_attempt" "$incident_key" || true
    return 0
  fi

  worker_state='same'
  for wait_iteration in {1..15}; do
    worker_state=$(old_worker_state "$worker_pid" "$worker_start" "$worker_executable" "$unlinked_identity")
    [[ "$worker_state" == 'gone' ]] && break
    (( wait_iteration < 15 )) && /bin/sleep 1
  done
  [[ "$worker_state" == 'gone' ]] || \
    worker_state=$(old_worker_state "$worker_pid" "$worker_start" "$worker_executable" "$unlinked_identity")
  if [[ "$worker_state" != 'gone' ]]; then
    log "stale Claude Code worker pid=$worker_pid was not confirmed gone after SIGTERM state=$worker_state; no stronger signal was sent"
    reconcile_term_attempt_receipt "$term_attempt" "$incident_key" || true
    return 0
  fi

  resumed_record=''
  for wait_iteration in {1..20}; do
    resumed_record=$(find_resumed_worker "$session_hash" "$replacement_version" "$worker_pid" "$worker_start_hash") && break
    (( wait_iteration < 20 )) && /bin/sleep 1
  done
  reconcile_term_attempt_receipt "$term_attempt" "$incident_key" || true
  log "stale Claude Code worker pid=$worker_pid exited old_version=$stale_version replacement_version=$replacement_version"
}

main() {
  local worker_pid disclaimer_pid worker_command

  (( $# <= 1 )) || {
    print -u2 -r -- "usage: $program_name [--dry-run]"
    return 64
  }
  mode="${1:-}"
  case "$mode" in
    ''|'--dry-run') ;;
    *)
      print -u2 -r -- "usage: $program_name [--dry-run]"
      return 64
      ;;
  esac

  # Dry-run remains read-only. Live scheduled runs first reconcile every
  # irreversible TERM barrier, even when the old worker is no longer present.
  if [[ -z "$mode" && -e "$receipt_root" ]]; then
    ensure_receipt_root || {
      log "did not reconcile Claude worker receipts because the receipt directory is unsafe"
      return 0
    }
    reconcile_all_term_attempts
  fi

  while read -r worker_pid disclaimer_pid worker_command; do
    inspect_worker "$worker_pid" "$disclaimer_pid" "$worker_command"
  done < <(/bin/ps -axo pid=,ppid=,command=)
}

main "$@"
