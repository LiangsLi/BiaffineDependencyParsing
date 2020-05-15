# -*- coding: utf-8 -*-
# Created by li huayong on 2019/10/7

import torch.nn as nn
from typing import Dict
from utils.data.graph_vocab import GraphVocab
from modules.bertology_encoder import BERTologyEncoder
from modules.biaffine import DeepBiaffineScorer, DirectBiaffineScorer
from models.base_model import BaseModel


class BiaffineDependencyModel(BaseModel):
    def __init__(self, args):
        """
        BERT+Transformer+Biaffine Dependency Parser Model
        Args:
            args:
        """
        super().__init__()
        self.args = args
        self.graph_vocab = GraphVocab(args.graph_vocab_file)
        if args.encoder_type == "bertology":
            # args.encoder_type 控制用什么类型的encoder（EBRTology/Transformer等等）
            # args.bertology_type 控制具体是什么类型的BERT（bert/xlnert/roberta等等）
            self.encoder = BERTologyEncoder(
                no_cuda=not args.cuda,
                bertology=args.bertology_type,
                bertology_path=args.saved_model_path,
                bertology_word_select_mode=args.bertology_word_select,
                bertology_output_mode=args.bertology_output_mode,
                max_seq_len=args.max_seq_len,
                bertology_after=args.bertology_after,
                after_layers=args.after_layers,
                after_dropout=args.after_dropout,
            )
        elif args.encoder_type in ["lstm", "gru"]:
            self.encoder = None  # Do NOT support now #todo
        elif args.encoder_type == "transformer":
            self.encoder = None  # Do NOT support now #todo
        if args.direct_biaffine:
            self.unlabeled_biaffine = DirectBiaffineScorer(
                args.encoder_output_dim, args.encoder_output_dim, 1, pairwise=True
            )
            self.labeled_biaffine = DirectBiaffineScorer(
                args.encoder_output_dim,
                args.encoder_output_dim,
                len(self.graph_vocab.get_labels()),
                pairwise=True,
            )
        else:
            self.unlabeled_biaffine = DeepBiaffineScorer(
                args.encoder_output_dim,
                args.encoder_output_dim,
                args.biaffine_hidden_dim,
                1,
                pairwise=True,
                dropout=args.biaffine_dropout,
            )
            self.labeled_biaffine = DeepBiaffineScorer(
                args.encoder_output_dim,
                args.encoder_output_dim,
                args.biaffine_hidden_dim,
                len(self.graph_vocab.get_labels()),
                pairwise=True,
                dropout=args.biaffine_dropout,
            )
        if args.use_pos:
            # todo: support CRF
            self.pos_classifier = nn.Linear(args.encoder_output_dim, args.pos_label_num)
        # self.dropout = nn.Dropout(args.dropout)
        # if args.learned_loss_ratio:
        #     self.label_loss_ratio = nn.Parameter(torch.Tensor([0.5]))
        # else:
        #     self.label_loss_ratio = args.label_loss_ratio

    def forward(self, inputs: Dict) -> Dict:
        if not isinstance(inputs, dict):
            raise RuntimeError("Parser Model input must be Dict type")
        encoder_output = self.encoder(**inputs)
        unlabeled_scores = self.unlabeled_biaffine(
            encoder_output, encoder_output
        ).squeeze(3)
        labeled_scores = self.labeled_biaffine(encoder_output, encoder_output)
        if self.args.use_pos:
            pos_logits = self.pos_classifier(encoder_output)
        else:
            pos_logits = None
        return {
            "unlabeled_scores": unlabeled_scores,
            "labeled_scores": labeled_scores,
            "pos_logits": pos_logits,
        }
