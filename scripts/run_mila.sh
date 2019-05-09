#!/usr/bin/env bash
# If needed, add default args to sbatch:

echo "Running on $HOSTNAME"
export PYTHONUNBUFFERED=1
# Miniconda
source /network/home/luyuchen/miniconda2/etc/profile.d/conda.sh
conda activate py36
echo $(which python)

exec $@
