#!/bin/bash
# Llama-3.2-3B on the ORIGINAL 20 conversations (EN, EN_REP, MIX_CLEAN). Run on a Sol GPU node.
#
#   read -rs HF_TOKEN; export HF_TOKEN          # type the token, nothing is shown
#   nohup bash run_llama.sh > /scratch/$USER/mcd_runs/llama.log 2>&1 &
#   tail -n 3 /scratch/$USER/mcd_runs/llama.log
#
# Safe to re-run: finished conversations are kept, only the rest is redone.
set -uo pipefail

MODEL="meta-llama/Llama-3.2-3B-Instruct"
WORKSPACE="/scratch/$USER/mcd_runs/main"          # same workspace as Qwen; results are kept per model
LOGDIR="/scratch/$USER/mcd_runs"
CONVS=$(printf "LIS-S%02d " $(seq 1 20))
CONDS="EN EN_REP MIX_CLEAN"

if [ -z "${HF_TOKEN:-}" ]; then echo "!! HF_TOKEN is not set. Run:  read -rs HF_TOKEN; export HF_TOKEN"; exit 1; fi

module load mamba/latest
source activate mcd-vllm
export HF_HOME="/scratch/$USER/hf_cache"
mkdir -p "$HF_HOME" "$LOGDIR"

PROJECT="${PROJECT:-$(dirname "$(find "$HOME" -maxdepth 3 -type d -name mcd | head -1)")}"
cd "$PROJECT" || { echo "!! project folder not found"; exit 1; }
echo "project: $PROJECT   node: $(hostname)   gpu: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
if [ ! -d "$WORKSPACE" ]; then echo "!! $WORKSPACE not found (the Qwen workspace)"; exit 1; fi

vllm serve "$MODEL" --dtype half --max-model-len 32768 --gpu-memory-utilization 0.90 \
      --port 8000 > "$LOGDIR/vllm_llama.log" 2>&1 &
VLLM_PID=$!
trap 'kill $VLLM_PID 2>/dev/null' EXIT

READY=0
for _ in $(seq 1 180); do                          # up to 15 min (first start downloads the model)
  if curl -sf http://localhost:8000/v1/models > /dev/null; then READY=1; echo "server ready"; break; fi
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then echo "!! vLLM stopped. Last lines:"; tail -n 30 "$LOGDIR/vllm_llama.log"; exit 1; fi
  sleep 5
done
[ "$READY" = 1 ] || { echo "!! server not ready after 15 min"; tail -n 30 "$LOGDIR/vllm_llama.log"; exit 1; }

for pass in 1 2; do                                # second pass redoes anything that failed or was cut off
  python -m mcd --workspace "$WORKSPACE" run --model "$MODEL" --base-url http://localhost:8000/v1 \
         --conv-ids $CONVS --conditions $CONDS --workers 8 --max-tokens 4096
done
python -m mcd --workspace "$WORKSPACE" status
echo ALL_DONE
