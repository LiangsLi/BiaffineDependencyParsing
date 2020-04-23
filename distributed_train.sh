#!/bin/bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5 python -m torch.distributed.launch --nproc_per_node=6 main.py -c config_files/bert_biaffine.yaml