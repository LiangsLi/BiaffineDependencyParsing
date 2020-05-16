#!/bin/bash
CUDA_VISIBLE_DEVICES=0,1,2,3 python main.py train -c config_files/bert_biaffine.yaml --test_after_train
