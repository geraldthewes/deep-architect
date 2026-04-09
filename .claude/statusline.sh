#!/bin/bash
input=$(cat)

MODEL=$(echo "$input" | jq -r '.model.display_name')
CONTEXT_SIZE=$(echo "$input" | jq -r '.context_window.context_window_size')
USAGE=$(echo "$input" | jq '.context_window.current_usage')

if [ "$USAGE" != "null" ]; then
    CURRENT_TOKENS=$(echo "$USAGE" | jq '.input_tokens + .cache_creation_input_tokens + .cache_read_input_tokens')
    PERCENT_USED=$((CURRENT_TOKENS * 100 / CONTEXT_SIZE))
    
    FILLED=$((PERCENT_USED * 30 / 100))
    EMPTY=$((30 - FILLED))
    BAR=$(printf '█%.0s' $(seq 1 $FILLED))$(printf '░%.0s' $(seq 1 $EMPTY))

    if [ "$PERCENT_USED" -gt 70 ]; then
        COLOR="\033[31m"  # Red
    elif [ "$PERCENT_USED" -gt 50 ]; then
        COLOR="\033[33m"  # Yellow
    else
        COLOR="\033[32m"  # Green
    fi
    RESET="\033[0m"

    echo -e "[$MODEL] Context: [${COLOR}${BAR}${RESET}] ${PERCENT_USED}%"
else
    echo "[$MODEL] Context: 0%"
fi
