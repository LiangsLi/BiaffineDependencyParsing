# -*- coding: utf-8 -*-
# Created by li huayong on 2019/11/19
from typing import List

import numpy as np
from utils.data.conll_file import load_conllu_file
from utils.data.graph_vocab import GraphVocab


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(
        self,
        guid: str,
        sentence: str,
        start_pos: List,
        end_pos: List,
        deps: List = None,
        pos: List = None,
    ):
        """Constructs a InputExample.
        """
        self.guid = guid
        self.sentence = sentence
        self.start_pos = start_pos
        self.end_pos = end_pos
        self.deps = deps
        self.pos = pos


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(
        self,
        input_ids: List,
        input_mask: List,
        segment_ids: List,
        dep_ids: List,
        start_pos: List = None,
        end_pos: List = None,
        pos_ids: List = None,
    ):
        """
            输入的特征，python类型
        :param input_ids:
        :param input_mask: 如果mask_padding_with_zero=True（默认），则 1 代表 实际输入，0 代表 padding
        :param segment_ids:
        :param dep_ids:
        :param start_pos: 词语的对应开始序号
        :param end_pos: 词语的对应结束序号
        :param pos_ids: 词性ids，开始是一个<PAD>,代表ROOT，后面接<PAD>补足长度, <PAD>均不计算loss
        """
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.dep_ids = dep_ids
        self.start_pos = start_pos
        self.end_pos = end_pos
        if pos_ids:
            self.pos_ids = pos_ids


class CoNLLUProcessor(object):
    """
        依存分析BERT数据处理，输入文件必须是CoNLL-U格式
    """

    def __init__(self, args, graph_vocab, word_vocab=None):
        assert isinstance(graph_vocab, GraphVocab)
        self.graph_vocab = graph_vocab
        self.args = args
        assert (
            args.encoder_type == "bertology"
        ), "暂时不支持xlent等类似BERT的模型，输入端需要适配（ROOT,start_pos,end_pos等等）"
        # TODO：支持xlnet,roberta,xlm等模型
        self.word_vocab = word_vocab

    # def get_train_examples(self, data_dir, file_name, max_seq_length):
    #     """Gets a collection of `InputExample`s for the train set."""
    #     CoNLLU_file, CoNLLU_data = load_conllu_file(os.path.join(data_dir, file_name))
    #     return self._create_bert_example(CoNLLU_data, 'train', max_seq_length), CoNLLU_file

    def get_examples(self, file_path, max_seq_length, training=False):
        """Gets a collection of `InputExample`s for the dev set."""
        CoNLLU_file, CoNLLU_data = load_conllu_file(file_path)
        return (
            self.create_bert_example(
                CoNLLU_data,
                "train" if training else "dev",
                max_seq_length,
                training=training,
            ),
            CoNLLU_file,
        )

    def _get_words_start_end_pos(self, words_list, max_seq_length):
        """
            注意：这里会自动在开始添加一个ROOT的表示
        :param words_list:
        :param max_seq_length:
        :return:
        """
        s = []
        e = []
        # 0 for ROOT if root_representation == [CLS]
        if self.args.root_representation == "cls":
            s.append(0)
            e.append(0)
        else:
            # 1 for other root_representation
            s.append(1)
            e.append(1)
        if self.args.root_representation != "cls":
            # 如果ROOT的表示不是CLS，那么words_list第一个元素就是ROOT，应当跳过（因为前面已经用0或者1表示了）
            clear_words_list = words_list[1:]
        else:
            clear_words_list = words_list
        #  BERT For single sequences:
        #  tokens:   [CLS] the dog is hairy . [SEP]
        #  type_ids:   0   0   0   0  0     0   0
        # 如果ROOT是用[CLS]表示，则从1开始计算；否则，则从2开始计算
        # 0永远是CLS
        if self.args.root_representation == "cls":
            sum_len = 1
        else:
            sum_len = 2
        for w in clear_words_list:
            s.append(sum_len) if sum_len < max_seq_length else s.append(
                max_seq_length - 1
            )
            if w.lower() not in self.word_vocab:  # 如果单词不在词表中，说明会被分割，要分开计算
                sum_len += len(w)
            else:
                # 如果单词在词表中，可以视长度为1
                # 注意[MASK],[unused1]等特殊字符都在BERT的vocab中
                sum_len += 1
            e.append(sum_len - 1) if sum_len - 1 < max_seq_length else e.append(
                max_seq_length - 1
            )
        return s, e

    def create_bert_example(
        self, CoNLLU_data, set_type, max_seq_length, training=False
    ):
        examples = []
        # print(CoNLLU_data)
        for i, sent in enumerate(CoNLLU_data.sentences):
            guid = f"{set_type}-{i}"
            words = []
            deps = []
            pos = []
            if self.args.root_representation == "cls":
                # BERT输入会自动添加一个CLS，所以此时不需要特殊处理
                pass
            elif self.args.root_representation == "unused":
                # BERT中预留了部分未经训练的空位，这里使用unused1 （1~99都可以）
                words.append("[unused1]")
            elif self.args.root_representation in ["root", "根"]:
                # 其他可能的表示形式，注意，这些字符已经被BERT预训练过，所以可能效果不佳
                words.append(self.args.root_representation)
            else:
                raise Exception(
                    f"illegal root representation:{self.args.root_representation}"
                )
            for line in sent.words:
                line_res = []
                if line.dep == "_":
                    # 若line[-1] == '_'，则说明是一个空白conllu文件，只有word（假定没有pos）
                    if deps:
                        # print(CoNLLU_data)
                        raise Exception("illegal CoNLLU data")
                    # line[0] 是 词语
                    # 此时只读取word，因为其他位置为空
                    words.append(line.word)
                    continue
                # line[2] 是 依存关系，多个关系用|分割
                arcs = line.dep.split("|")
                for arc in arcs:
                    head, dep_rel = arc.split(":")
                    dep_rel_idx = self.graph_vocab.unit2id[dep_rel]
                    line_res.append([int(head), dep_rel_idx])
                deps.append(line_res)
                words.append(line.word)
                pos.append(line.pos)
            if not deps:
                deps = None

            sentence = "".join(words)
            if self.args.input_mask and training:
                # input_mask应该只在training的时候使用
                if self.args.input_mask_granularity == "char":
                    input_mask = (
                        np.random.uniform(0, 1, len(sentence))
                        < self.args.input_mask_prob
                    )
                    # 注意这里我们只对中文字符做mask，
                    # 因为英文单词不是逐个字符切分，如果对英文字符mask可能使得分词后的总长度变化
                    # 而中文是逐个字切分，即使做了mask也不影响分词后的长度
                    chars = [
                        "[MASK]" if (z[1] and "\u4e00" <= z[0] <= "\u9fa5") else z[0]
                        for z in zip(sentence, input_mask)
                    ]
                    sentence = "".join(chars)
                else:
                    # 单词粒度的mask
                    # 注意单词粒度的mask破坏了句子的长度！
                    # 因此_get_words_start_end_pos必须在mask操作之后执行
                    input_mask = (
                        np.random.uniform(0, 1, len(words)) < self.args.input_mask_prob
                    )
                    words = ["[MASK]" if z[1] else z[0] for z in zip(words, input_mask)]
                    sentence = "".join(words)
            # 词语的起始位置
            # 由于BERT是基于token（中文是字）序列输入的，依存分析是以单词为单位的，所以必须确定单词的表示，
            # 这里使用对应起始位置的向量来表示单词
            start_pos, end_pos = self._get_words_start_end_pos(words, max_seq_length)

            examples.append(
                InputExample(
                    guid=guid,
                    sentence=sentence,
                    start_pos=start_pos,
                    end_pos=end_pos,
                    deps=deps,
                    pos=pos,
                )
            )
        return examples


if __name__ == "__main__":
    pass
