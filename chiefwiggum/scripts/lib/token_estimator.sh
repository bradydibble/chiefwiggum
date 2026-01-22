#!/bin/bash

# Token estimation library for Ralph Loop
# Estimates token usage to prevent context window overflow

# Conservative estimates (Claude Sonnet 4.5: 200K context)
TOKENS_PER_CHAR=0.25           # ~4 chars per token average
MAX_CONTEXT_TOKENS=200000      # Claude Sonnet 4.5 limit
SAFETY_MARGIN_TOKENS=20000     # Reserve 20K for response
MAX_USABLE_TOKENS=$((MAX_CONTEXT_TOKENS - SAFETY_MARGIN_TOKENS))

# Estimate tokens from text
estimate_tokens() {
    local text="$1"
    local char_count=${#text}
    echo $(awk "BEGIN {print int($char_count * $TOKENS_PER_CHAR)}")
}

# Estimate tokens from file
estimate_tokens_file() {
    local file="$1"
    if [[ ! -f "$file" ]]; then
        echo "0"
        return
    fi
    local char_count=$(wc -c < "$file" 2>/dev/null || echo "0")
    echo $(awk "BEGIN {print int($char_count * $TOKENS_PER_CHAR)}")
}

# Calculate total session token usage
calculate_session_tokens() {
    local total=0

    # Base prompt (PROMPT.md)
    if [[ -f "PROMPT.md" ]]; then
        local prompt_tokens=$(estimate_tokens_file "PROMPT.md")
        total=$((total + prompt_tokens))
    fi

    # Session history (last 10 Claude outputs)
    local output_files=($(ls -t logs/claude_output_*.log 2>/dev/null | head -10))
    for file in "${output_files[@]}"; do
        local file_tokens=$(estimate_tokens_file "$file")
        total=$((total + file_tokens))
    done

    # Loop context (500 chars max per loop, 10 loops)
    total=$((total + 1250))  # 500 chars * 10 loops * 0.25 tokens/char

    echo "$total"
}

# Check if session should be reset due to token usage
should_reset_session_for_tokens() {
    local current_tokens=$(calculate_session_tokens)

    if [[ $current_tokens -ge $MAX_USABLE_TOKENS ]]; then
        echo "true"
        return 0
    else
        echo "false"
        return 1
    fi
}

# Get percentage of token budget used
get_token_usage_percentage() {
    local current_tokens=$(calculate_session_tokens)
    echo $(awk "BEGIN {print int(($current_tokens / $MAX_USABLE_TOKENS) * 100)}")
}
