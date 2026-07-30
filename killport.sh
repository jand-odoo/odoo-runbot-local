#!/bin/bash
set -e
PORT=${1:-8072}
PIDS=$(lsof -t -i:"$PORT" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    echo "$PIDS" | while read pid; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 1
    PIDS=$(lsof -t -i:"$PORT" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "$PIDS" | while read pid; do
            kill -KILL "$pid" 2>/dev/null || true
        done
    fi
fi
