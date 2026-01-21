#!/bin/bash
# Ralph Loop Profiler - Timing instrumentation for performance analysis
# Compatible with bash 3.2+ (macOS default)

# Configuration
PROFILING_ENABLED=${RALPH_PROFILING:-true}
PROFILING_LOG_FILE="logs/ralph_profiling.jsonl"
PROFILING_HISTORY_SIZE=10
PROFILING_TMP_DIR="/tmp/ralph_profiler_$$"

# Current iteration tracking
CURRENT_ITERATION=0

# Initialize profiler temp directory
init_profiler() {
    [[ "$PROFILING_ENABLED" != "true" ]] && return
    mkdir -p "$PROFILING_TMP_DIR"
    mkdir -p "$(dirname "$PROFILING_LOG_FILE")"
}

# Cleanup on exit
cleanup_profiler() {
    rm -rf "$PROFILING_TMP_DIR" 2>/dev/null
}

# Get timestamp in milliseconds (portable)
get_ms_timestamp() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS: use perl for milliseconds
        perl -MTime::HiRes=time -e 'printf "%.0f\n", time * 1000'
    else
        # Linux: use date with nanoseconds
        echo $(($(date +%s%N) / 1000000))
    fi
}

# Start timing a phase
start_phase() {
    [[ "$PROFILING_ENABLED" != "true" ]] && return
    local phase="$1"
    get_ms_timestamp > "$PROFILING_TMP_DIR/start_$phase"
}

# End timing a phase and record duration
end_phase() {
    [[ "$PROFILING_ENABLED" != "true" ]] && return
    local phase="$1"
    local metadata="${2:-{}}"
    local end_time=$(get_ms_timestamp)
    local start_file="$PROFILING_TMP_DIR/start_$phase"
    local start_time=$end_time

    if [[ -f "$start_file" ]]; then
        start_time=$(cat "$start_file")
    fi

    local duration=$((end_time - start_time))

    # Store duration
    echo "$duration" > "$PROFILING_TMP_DIR/dur_$phase"

    # Log to JSONL
    echo "{\"timestamp\":\"$(date -Iseconds)\",\"iteration\":$CURRENT_ITERATION,\"phase\":\"$phase\",\"duration_ms\":$duration,\"metadata\":$metadata}" >> "$PROFILING_LOG_FILE"
}

# Start a new iteration
start_iteration() {
    [[ "$PROFILING_ENABLED" != "true" ]] && return
    local iteration="$1"
    CURRENT_ITERATION=$iteration

    # Clear previous iteration data
    rm -f "$PROFILING_TMP_DIR"/dur_* "$PROFILING_TMP_DIR"/start_* 2>/dev/null

    start_phase "total"
}

# Get duration for a phase (helper)
get_phase_duration() {
    local phase="$1"
    local dur_file="$PROFILING_TMP_DIR/dur_$phase"
    if [[ -f "$dur_file" ]]; then
        cat "$dur_file"
    else
        echo "0"
    fi
}

# Print iteration summary with formatting
print_profiling_summary() {
    [[ "$PROFILING_ENABLED" != "true" ]] && return

    end_phase "total"
    local total=$(get_phase_duration "total")

    # Define phases in display order
    local phases="session_update call_tracking circuit_check rate_check exit_check status_update context_build session_init command_build llm_execution session_save response_analysis exit_signals git_operations circuit_record sleep"

    # Find slowest phase
    local slowest_phase=""
    local slowest_duration=0
    for phase in $phases; do
        local dur=$(get_phase_duration "$phase")
        if [[ $dur -gt $slowest_duration ]]; then
            slowest_duration=$dur
            slowest_phase=$phase
        fi
    done

    # Store total for running average
    echo "$total" >> "$PROFILING_TMP_DIR/iteration_totals"

    # Calculate running average (last N iterations)
    local sum=0
    local count=0
    if [[ -f "$PROFILING_TMP_DIR/iteration_totals" ]]; then
        while read -r val; do
            sum=$((sum + val))
            count=$((count + 1))
        done < <(tail -n "$PROFILING_HISTORY_SIZE" "$PROFILING_TMP_DIR/iteration_totals")
    fi
    local avg=$((count > 0 ? sum / count : 0))

    # Print summary
    echo ""
    echo -e "\033[1;36m[RALPH PROFILING - $(date '+%Y-%m-%d %H:%M:%S')]\033[0m"
    echo -e "\033[1mIteration #$CURRENT_ITERATION Summary:\033[0m"

    for phase in $phases; do
        local dur=$(get_phase_duration "$phase")
        [[ $dur -eq 0 ]] && continue

        local pct=0
        [[ $total -gt 0 ]] && pct=$((dur * 100 / total))

        local marker=""
        [[ "$phase" == "$slowest_phase" ]] && marker=" \033[1;33m⚠️ SLOWEST\033[0m"

        # Format phase name for display (capitalize words)
        local display_name=$(echo "$phase" | tr '_' ' ' | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)} 1')
        printf "├─ %-20s %6dms  (%2d%%)%b\n" "$display_name:" "$dur" "$pct" "$marker"
    done

    printf "└─ %-20s %6dms\n" "Total:" "$total"
    echo ""
    echo "Running Average (last $PROFILING_HISTORY_SIZE): ${avg}ms"
    echo ""
}

# Initialize on source
init_profiler

# Register cleanup
trap cleanup_profiler EXIT
