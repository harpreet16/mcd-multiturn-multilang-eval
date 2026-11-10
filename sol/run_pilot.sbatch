#!/bin/bash
# MCD pilot on ASU Sol. Submit with:  sbatch sol/run_pilot.sbatch
# NOT TESTED ON SOL. Check the lines marked CHECK against https://docs.rc.asu.edu before the first run.
#SBATCH --job-name=mcd-pilot
#SBATCH --time=0-04:00:00          # 4 hours is plenty for the pilot
#SBATCH --gres=gpu:1               # CHECK: GPU request syntax (some Sol partitions want e.g. gpu:a100:1)
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH -A class_cse59888883fall2026   # your class account (from: sacctmgr show assoc user=$USER). Alternative: class_cse598fall2026
#SBATCH -q class                   # the QOS Sol listed for this account
#SBATCH -p public                  # CHECK: if Sol says "invalid partition", delete this line
#SBATCH --output=%x-%j.out         # the log you read to see progress

set -euo pipefail

# ---- edit these if you like -------------------------------------------------
MODEL="${MODEL:-Qwen/Qwen3-4B-Instruct-2507}"          # or meta-llama/Llama-3.2-3B-Instruct (needs HF_TOKEN)
PROJECT="${PROJECT:-$HOME/MCD-multiturn-multilang-eval}"
WORKSPACE="${WORKSPACE:-/scratch/$USER/mcd_runs/main}"   # results; /scratch is not backed up, copy the results home when done
CONVS="${CONVS:-LIS-S04 LIS-S09 LIS-S16 LIS-S19 LIS-S20}"
CONDS="${CONDS:-EN EN_REP MIX_CLEAN}"
# -----------------------------------------------------------------------------

module load mamba/latest           # CHECK: run `module avail mamba` if this name is wrong
source activate mcd-vllm           # the environment you create once (see the steps), contains vllm
export HF_HOME="/scratch/$USER/hf_cache"      # downloaded model weights go here, not in your small home folder
mkdir -p "$WORKSPACE" "$HF_HOME"
cd "$PROJECT"

echo "node: $(hostname)   gpu: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"

vllm serve "$MODEL" --dtype auto --max-model-len 16384 --gpu-memory-utilization 0.90 \
      --port 8000 > "$WORKSPACE/vllm.log" 2>&1 &
VLLM_PID=$!
trap 'kill $VLLM_PID 2>/dev/null || true' EXIT          # always stop the model server when the job ends

READY=0
for _ in $(seq 1 180); do                                # wait up to 15 minutes (first start downloads the model)
  if curl -sf http://localhost:8000/v1/models > /dev/null; then READY=1; echo "server ready"; break; fi
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then echo "!! vLLM stopped. Last lines:"; tail -n 40 "$WORKSPACE/vllm.log"; exit 1; fi
  sleep 5
done
[ "$READY" = 1 ] || { echo "!! server not ready after 15 minutes"; tail -n 40 "$WORKSPACE/vllm.log"; exit 1; }

URL="http://localhost:8000/v1"
python -m mcd --workspace "$WORKSPACE" ingest
python -m mcd --workspace "$WORKSPACE" preflight --model "$MODEL" --base-url "$URL"
python -m mcd --workspace "$WORKSPACE" run --model "$MODEL" --base-url "$URL" \
       --conv-ids $CONVS --conditions $CONDS --workers 8 --max-tokens 4096
python -m mcd --workspace "$WORKSPACE" grade --judge-model manual
python -m mcd --workspace "$WORKSPACE" status
echo "done. Results are in $WORKSPACE (responses.jsonl, judge_pending.json)"
