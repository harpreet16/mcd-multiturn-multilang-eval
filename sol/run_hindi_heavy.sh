#!/bin/bash
# HI_HEAVY (primary) + HI_ALL (control) on the 20 original conversations, for BOTH models. Run on a Sol GPU node.
#
#   read -rsp "Paste token then Enter: " HF_TOKEN; echo; export HF_TOKEN
#   nohup bash sol/run_hindi_heavy.sh > /scratch/$USER/mcd_runs/hindi_heavy.log 2>&1 &
#   tail -n 3 /scratch/$USER/mcd_runs/hindi_heavy.log
#
# Safe to re-run: finished conversations are kept, only the rest is redone.
set -uo pipefail

SRC="/scratch/$USER/mcd_runs/main"            # the workspace that already holds the EN / MIX_CLEAN results
WORKSPACE="/scratch/$USER/mcd_runs/full_hindi" # separate copy: the old workspace is never modified
LOGDIR="/scratch/$USER/mcd_runs"
CONVS=$(printf "LIS-S%02d " $(seq 1 20))
CONDS="${CONDS:-HI_HEAVY HI_ALL}"                   # override for a new model: "EN MIX_CLEAN HI_HEAVY HI_ALL"
MODELS="${MODELS:-Qwen/Qwen3-4B-Instruct-2507 meta-llama/Llama-3.2-3B-Instruct}"   # override to re-run one model
MAXLEN="${MAXLEN:-32768}"                      # server context window; Hindi needs many tokens

if [ -z "${HF_TOKEN:-}" ]; then echo "!! HF_TOKEN is not set (needed for Llama)"; exit 1; fi
module load mamba/latest
source activate mcd-vllm
export HF_HOME="/scratch/$USER/hf_cache"
mkdir -p "$HF_HOME" "$LOGDIR"
PROJECT="${PROJECT:-$(dirname "$(find "$HOME" -maxdepth 3 -type d -name mcd | head -1)")}"
cd "$PROJECT" || { echo "!! project folder not found"; exit 1; }
[ -f data/hindi_translations.json ] || { echo "!! data/hindi_translations.json missing: run git pull first"; exit 1; }
echo "project: $PROJECT   node: $(hostname)   gpu: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"

if [ ! -d "$WORKSPACE" ]; then
  [ -d "$SRC" ] || { echo "!! $SRC not found"; exit 1; }
  mkdir -p "$WORKSPACE"
  for f in responses.jsonl grades.jsonl judge_log.jsonl; do      # the discarded curated conversations (LIS-X) are left out
    [ -f "$SRC/$f" ] && grep -v 'LIS-X' "$SRC/$f" > "$WORKSPACE/$f"
  done
  cp "$SRC"/{rulebook.json,judge_cache.json} "$WORKSPACE"/ 2>/dev/null
  python -m mcd --workspace "$WORKSPACE" ingest || { echo "!! ingest failed"; exit 1; }
fi

for MODEL in $MODELS; do
  echo "=== $MODEL ==="
  vllm serve "$MODEL" --dtype half --max-model-len "$MAXLEN" --gpu-memory-utilization 0.90 \
        --port 8000 > "$LOGDIR/vllm_$(basename $MODEL).log" 2>&1 &
  PID=$!
  READY=0
  for _ in $(seq 1 180); do
    if curl -sf http://localhost:8000/v1/models > /dev/null; then READY=1; break; fi
    if ! kill -0 "$PID" 2>/dev/null; then break; fi
    sleep 5
  done
  if [ "$READY" != 1 ]; then echo "!! server for $MODEL did not start. Last lines:"; tail -n 30 "$LOGDIR/vllm_$(basename $MODEL).log"; kill $PID 2>/dev/null; continue; fi
  echo "server ready"
  for pass in 1 2; do                              # second pass redoes anything that failed or was cut off
    python -m mcd --workspace "$WORKSPACE" run --model "$MODEL" --base-url http://localhost:8000/v1 \
           --conv-ids $CONVS --conditions $CONDS --workers 8 --max-tokens 4096
  done
  kill $PID 2>/dev/null; wait $PID 2>/dev/null; sleep 10
done
python -m mcd --workspace "$WORKSPACE" status
echo ALL_DONE
