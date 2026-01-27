#!/bin/bash

# Cost tracking library for Ralph Loop
# Calculates costs from token usage (estimation or actual)

# Claude Sonnet 4.5 pricing (as of January 2025)
# Source: https://www.anthropic.com/pricing
PRICE_INPUT_PER_1M=3.00        # $3 per 1M input tokens
PRICE_OUTPUT_PER_1M=15.00      # $15 per 1M output tokens
PRICE_CACHE_WRITE_PER_1M=3.75  # $3.75 per 1M cache creation tokens
PRICE_CACHE_READ_PER_1M=0.30   # $0.30 per 1M cache read tokens (90% discount)

# Calculate cost from token counts
# Args: input_tokens output_tokens cache_creation_tokens cache_read_tokens
calculate_cost() {
    local input_tokens=${1:-0}
    local output_tokens=${2:-0}
    local cache_creation_tokens=${3:-0}
    local cache_read_tokens=${4:-0}

    # Calculate each component (tokens / 1,000,000 * price)
    local input_cost=$(awk "BEGIN {printf \"%.6f\", ($input_tokens / 1000000.0) * $PRICE_INPUT_PER_1M}")
    local output_cost=$(awk "BEGIN {printf \"%.6f\", ($output_tokens / 1000000.0) * $PRICE_OUTPUT_PER_1M}")
    local cache_write_cost=$(awk "BEGIN {printf \"%.6f\", ($cache_creation_tokens / 1000000.0) * $PRICE_CACHE_WRITE_PER_1M}")
    local cache_read_cost=$(awk "BEGIN {printf \"%.6f\", ($cache_read_tokens / 1000000.0) * $PRICE_CACHE_READ_PER_1M}")

    # Total cost
    local total_cost=$(awk "BEGIN {printf \"%.6f\", $input_cost + $output_cost + $cache_write_cost + $cache_read_cost}")
    echo "$total_cost"
}

# Estimate tokens and cost from character count (for real-time estimates)
# Args: prompt_chars output_chars
estimate_tokens_and_cost() {
    local prompt_chars=${1:-0}
    local output_chars=${2:-0}

    # Heuristic: 4 chars per token = 0.25 tokens per char
    local input_tokens=$(awk "BEGIN {print int($prompt_chars * 0.25)}")
    local output_tokens=$(awk "BEGIN {print int($output_chars * 0.25)}")

    local cost=$(calculate_cost "$input_tokens" "$output_tokens" 0 0)

    # Return as JSON
    echo "{\"input_tokens\":$input_tokens,\"output_tokens\":$output_tokens,\"cache_creation_tokens\":0,\"cache_read_tokens\":0,\"cost\":$cost,\"source\":\"estimation\"}"
}

# Extract usage data from Claude Code JSON response
# Args: json_file
extract_usage_from_json() {
    local json_file="$1"

    if [[ ! -f "$json_file" ]]; then
        echo "{\"input_tokens\":0,\"output_tokens\":0,\"cache_creation_tokens\":0,\"cache_read_tokens\":0,\"cost\":0.0,\"source\":\"not_found\"}"
        return
    fi

    # Try multiple possible JSON paths (Claude CLI format varies)
    local input_tokens=$(jq -r '.usage.input_tokens // .metadata.usage.input_tokens // 0' "$json_file" 2>/dev/null || echo "0")
    local output_tokens=$(jq -r '.usage.output_tokens // .metadata.usage.output_tokens // 0' "$json_file" 2>/dev/null || echo "0")
    local cache_creation=$(jq -r '.usage.cache_creation_input_tokens // .metadata.usage.cache_creation_input_tokens // 0' "$json_file" 2>/dev/null || echo "0")
    local cache_read=$(jq -r '.usage.cache_read_input_tokens // .metadata.usage.cache_read_input_tokens // 0' "$json_file" 2>/dev/null || echo "0")

    # If all zeros, data might not be available
    local source="not_available"
    if [[ $input_tokens -gt 0 || $output_tokens -gt 0 ]]; then
        source="api_actual"
    fi

    local cost=$(calculate_cost "$input_tokens" "$output_tokens" "$cache_creation" "$cache_read")

    echo "{\"input_tokens\":$input_tokens,\"output_tokens\":$output_tokens,\"cache_creation_tokens\":$cache_creation,\"cache_read_tokens\":$cache_read,\"cost\":$cost,\"source\":\"$source\"}"
}

# Accumulate cost in status file (for crash resilience)
# Args: ralph_id cost_json
accumulate_cost_in_status() {
    local ralph_id="$1"
    local cost_json="$2"
    local status_dir="$HOME/.chiefwiggum/ralphs/status"
    local status_file="$status_dir/${ralph_id}.json"

    if [[ ! -f "$status_file" ]]; then
        # Debug: status file not found
        if [[ -n "${VERBOSE_PROGRESS:-}" && "$VERBOSE_PROGRESS" == "true" ]]; then
            echo "[DEBUG] Cost tracking: Status file not found at $status_file" >&2
        fi
        return 0
    fi

    # Read current accumulated cost
    local current_cost=$(jq -r '.cost_info.accumulated_cost // 0' "$status_file" 2>/dev/null || echo "0")
    local new_cost=$(echo "$cost_json" | jq -r '.cost // 0')
    local total_cost=$(awk "BEGIN {printf \"%.6f\", $current_cost + $new_cost}")

    # Extract token counts for accumulation
    local new_input=$(echo "$cost_json" | jq -r '.input_tokens // 0')
    local new_output=$(echo "$cost_json" | jq -r '.output_tokens // 0')
    local new_cache_creation=$(echo "$cost_json" | jq -r '.cache_creation_tokens // 0')
    local new_cache_read=$(echo "$cost_json" | jq -r '.cache_read_tokens // 0')

    local current_input=$(jq -r '.cost_info.input_tokens // 0' "$status_file" 2>/dev/null || echo "0")
    local current_output=$(jq -r '.cost_info.output_tokens // 0' "$status_file" 2>/dev/null || echo "0")
    local current_cache_creation=$(jq -r '.cost_info.cache_creation_tokens // 0' "$status_file" 2>/dev/null || echo "0")
    local current_cache_read=$(jq -r '.cost_info.cache_read_tokens // 0' "$status_file" 2>/dev/null || echo "0")

    local total_input=$((current_input + new_input))
    local total_output=$((current_output + new_output))
    local total_cache_creation=$((current_cache_creation + new_cache_creation))
    local total_cache_read=$((current_cache_read + new_cache_read))

    # Update status file with new accumulated cost
    local temp_file="${status_file}.tmp"
    if jq --argjson cost_data "$cost_json" \
       --arg total "$total_cost" \
       --argjson total_input "$total_input" \
       --argjson total_output "$total_output" \
       --argjson total_cache_creation "$total_cache_creation" \
       --argjson total_cache_read "$total_cache_read" \
       '.cost_info = {
           accumulated_cost: ($total | tonumber),
           input_tokens: $total_input,
           output_tokens: $total_output,
           cache_creation_tokens: $total_cache_creation,
           cache_read_tokens: $total_cache_read,
           last_update: (now | todate)
       }' \
       "$status_file" > "$temp_file" && mv "$temp_file" "$status_file"; then
        # Debug: cost accumulated successfully
        if [[ -n "${VERBOSE_PROGRESS:-}" && "$VERBOSE_PROGRESS" == "true" ]]; then
            echo "[DEBUG] Cost tracking: Accumulated \$$total_cost to $status_file" >&2
        fi
    else
        # Debug: cost accumulation failed
        if [[ -n "${VERBOSE_PROGRESS:-}" && "$VERBOSE_PROGRESS" == "true" ]]; then
            echo "[DEBUG] Cost tracking: Failed to accumulate cost to $status_file" >&2
        fi
        rm -f "$temp_file"
    fi
}
