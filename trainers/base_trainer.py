# Created by li huayong on 2019/9/28
import os
import typing
from abc import ABCMeta
from abc import abstractmethod
from typing import Dict

import torch
import torch.nn as nn
from torch.utils.data import TensorDataset
from tqdm import tqdm

import utils.model.sdp_simple_scorer as sdp_scorer
from PyToolkit.PyToolkit import get_logger
from PyToolkit.PyToolkit.seed import set_seed
from utils.best_result import BestResult
from utils.data.conll_file import CoNLLFile
from utils.data.graph_vocab import GraphVocab
from utils.model.decoder import parse_semgraph
from utils.model.decoder import sdp_decoder
from utils.model.get_optimizer import get_optimizer

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter


class BaseDependencyTrainer(metaclass=ABCMeta):
    def __init__(self, args, model):
        self.model = model
        self.optimizer = self.optim_scheduler = None
        self.graph_vocab = GraphVocab(args.graph_vocab_file)
        self.configs = args
        self.logger = get_logger(args.log_name)

    @abstractmethod
    def _unpack_batch(self, batch: TensorDataset) -> Dict:
        """
        拆分batch，得到encoder的输入和word mask，sentence length，以及dep ids,以及其他输入信息
        eg:
            dataset = TensorDataset(all_input_ids, all_input_mask,
                        all_segment_ids, all_start_pos,
                        all_end_pos, all_dep_ids,
                        all_pos_ids)

        Args:
            batch: 输入的单个batch,类型为TensorDataset(或者torchtext.dataset)，可用索引分别取值

        Returns:
            返回一个字典，[1]是inputs，类型为字典；[2]是word mask；[3]是sentence length,python 列表；[4]是dep ids,
            根据实际情况可能还包含其他输入信息
        """
        raise NotImplementedError("must implement in sub class")

    def _custom_train_operations(self, epoch: int) -> None:
        """
            某些模型在训练时可能需要一些定制化的操作，
            比如BERT类型的模型可能会在Training的时候动态freeze某些层
            为了支持这些操作同时不破坏BiaffineDependencyTrainer的普适性，我们加入这个方法
            BiaffineDependencyTrainer的子类可以选择重写该方法以支持定制化操作
            注意这个方法会在训练的每个epoch的开始调用一次
            本方法默认不会做任何事情
        :return:
        """

    def _update_and_predict(
        self,
        unlabeled_scores,
        labeled_scores,
        unlabeled_target,
        labeled_target,
        word_pad_mask,
        label_loss_ratio=0.5,
        sentence_lengths=None,
        calc_loss=True,
        update=True,
        calc_prediction=False,
        pos_logits=None,
        pos_target=None,
        pos_loss_ratio=1.0,
        summary_writer=None,
        global_step=None,
    ):
        """
            针对一个batch输入：计算loss，反向传播，计算预测结果
            :param word_pad_mask: 以word为单位，1为PAD，0为真实输入
        :return:
        """
        weights = torch.ones(
            word_pad_mask.size(0),
            self.configs.max_seq_len,
            self.configs.max_seq_len,
            dtype=unlabeled_scores.dtype,
            device=unlabeled_scores.device,
        )
        # 将PAD的位置权重设为0，其余位置为1
        weights = weights.masked_fill(word_pad_mask.unsqueeze(1), 0)
        weights = weights.masked_fill(word_pad_mask.unsqueeze(2), 0)
        # words_num 记录batch中的单词数量
        # torch.eq(word_pad_mask, False) 得到word_mask
        words_num = torch.sum(torch.eq(word_pad_mask, False)).item()
        if calc_loss:
            if label_loss_ratio is None:
                raise RuntimeError("label_loss_ratio can not be None")
            if unlabeled_target is None or labeled_target is None:
                raise RuntimeError("unlabeled_target/labeled_target can not be None")
            dep_arc_loss_func = nn.BCEWithLogitsLoss(weight=weights, reduction="sum")
            dep_arc_loss = dep_arc_loss_func(unlabeled_scores, unlabeled_target)

            dep_label_loss_func = nn.CrossEntropyLoss(ignore_index=-1, reduction="sum")
            dependency_mask = labeled_target.eq(0)
            labeled_target = labeled_target.masked_fill(dependency_mask, -1)
            labeled_scores = labeled_scores.contiguous().view(
                -1, len(self.graph_vocab.get_labels())
            )
            dep_label_loss = dep_label_loss_func(
                labeled_scores, labeled_target.view(-1)
            )

            if self.configs.use_pos:
                if pos_logits is None or pos_target is None:
                    raise RuntimeError("pos_logits/pos_target can not be None")
                pos_loss_func = nn.CrossEntropyLoss(
                    ignore_index=self.configs.pos_label_pad_idx
                )
                pos_loss = pos_loss_func(
                    pos_logits.view(-1, self.configs.pos_label_num), pos_target.view(-1)
                )

            loss = 2 * (
                (1 - label_loss_ratio) * dep_arc_loss
                + label_loss_ratio * dep_label_loss
            )

            if self.configs.use_pos:
                loss = loss + pos_loss_ratio * pos_loss

            if self.configs.average_loss_by_words_num:
                loss = loss / words_num

            if self.configs.scale_loss:
                loss = loss * self.configs.loss_scaling_ratio

            if self.configs.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel training

            if summary_writer and global_step:
                summary_writer.add_scalar(
                    "train_loss/dep_arc_loss", dep_arc_loss, global_step
                )
                summary_writer.add_scalar(
                    "train_loss/dep_label_loss", dep_label_loss, global_step
                )
                if self.configs.use_pos:
                    summary_writer.add_scalar(
                        "train_loss/pos_loss", pos_loss, global_step
                    )

            if update:
                # Ref:https://discuss.pytorch.org/t/model-zero-grad-or-optimizer-zero-grad/28426/5
                # Dmitry A. Konovalov:
                #     model.zero_grad() and optimizer.zero_grad() are the same IF all your model parameters
                #     are in that optimizer.
                #     I found it is safer to call model.zero_grad() to make sure all grads are zero,
                #     e.g. if you have two or more optimizers for one model.
                # Ref:https://discuss.pytorch.org/t/zero-grad-optimizer-or-net/1887/9
                # ptrblck：
                #      if you pass all parameters of your model to the optimizer, both calls will be equal.
                #      model.zero_grad() would clear all parameters of the model,
                #      while the optimizerX.zero_grad() call will just clean
                #      the gradients of the parameters that were passed to it.

                # loss.backward() **之前** 清空模型梯度
                self.model.zero_grad()
                # 参看上述注释，这里只需要model.zero_grad()
                # self.optimizer.zero_grad()
                loss.backward()
                if self.configs.max_grad_norm > 0:
                    torch.nn.utils.clip_grad_norm_(
                        self.model.parameters(), self.configs.max_grad_norm
                    )
                self.optimizer = typing.cast(torch.optim.Optimizer, self.optimizer)
                self.optimizer.step()
                if self.optim_scheduler:
                    self.optim_scheduler.step()  # Update learning rate schedule
            loss = loss.detach().cpu().item()
        else:
            loss = None
        if calc_prediction:
            if not sentence_lengths:
                raise RuntimeError()
            weights = weights.unsqueeze(3)
            head_probs = torch.sigmoid(unlabeled_scores).unsqueeze(3)
            label_probs = torch.softmax(labeled_scores, dim=3)
            batch_probs = head_probs * label_probs * weights
            batch_probs = batch_probs.detach().cpu().numpy()
            # debug_print(batch_probs)
            sem_graph = sdp_decoder(batch_probs, sentence_lengths)
            sem_sents = parse_semgraph(sem_graph, sentence_lengths)
            batch_prediction = self.graph_vocab.parse_to_sent_batch(sem_sents)
        else:
            batch_prediction = None
        return loss, batch_prediction

    def train(self, train_data_loader, dev_data_loader=None, dev_CoNLLU_file=None):
        self.optimizer, self.optim_scheduler = get_optimizer(self.configs, self.model)
        global_step = 0
        best_result = BestResult()
        self.model.zero_grad()
        set_seed(
            self.configs
        )  # Added here for reproductibility (even between python 2 and 3)
        train_stop = False
        if self.configs.local_rank in [-1, 0] and not self.configs.no_output:
            summary_writer = SummaryWriter(log_dir=self.configs.summary_dir)
        for epoch in range(1, self.configs.max_train_epochs + 1):
            epoch_ave_loss = 0
            train_data_loader = tqdm(
                train_data_loader,
                desc=f"Training epoch {epoch}",
                disable=self.configs.local_rank not in [-1, 0],
            )
            # 某些模型在训练时可能需要一些定制化的操作，默认什么都不做
            # 具体参考子类中_custom_train_operations的实现
            self._custom_train_operations(epoch)
            for step, batch in enumerate(train_data_loader):
                batch = tuple(t.to(self.configs.device) for t in batch)
                self.model.train()
                # debug_print(batch)
                # word_mask:以word为单位，1为真实输入，0为PAD
                unpacked_batch = self._unpack_batch(batch)
                # word_pad_mask:以word为单位，1为PAD，0为真实输入
                word_pad_mask = torch.eq(unpacked_batch["word_mask"], 0)
                model_output = self.model(unpacked_batch["inputs"])
                unlabeled_scores, labeled_scores = (
                    model_output["unlabeled_scores"],
                    model_output["labeled_scores"],
                )
                labeled_target = unpacked_batch["dep_ids"]
                unlabeled_target = labeled_target.ge(1).to(unlabeled_scores.dtype)
                if self.configs.use_pos:
                    pos_logits = model_output["pos_logits"]
                    pos_target = unpacked_batch["pos_ids"]
                else:
                    pos_target = pos_logits = None
                # Calc loss and update:
                loss, _ = self._update_and_predict(
                    unlabeled_scores,
                    labeled_scores,
                    unlabeled_target,
                    labeled_target,
                    word_pad_mask,
                    # label_loss_ratio=self.model.module.label_loss_ratio if hasattr(self.model,'module') else self.model.label_loss_ratio,
                    calc_loss=True,
                    update=True,
                    calc_prediction=False,
                    pos_logits=pos_logits,
                    pos_target=pos_target,
                    summary_writer=summary_writer
                    if self.configs.local_rank in [-1, 0]
                    else None,
                    global_step=global_step,
                )
                global_step += 1
                if loss is not None:
                    epoch_ave_loss += loss

                if (
                    global_step % self.configs.eval_interval == 0
                    and self.configs.local_rank in [-1, 0]
                ):
                    if not self.configs.no_output:
                        summary_writer.add_scalar("train_loss/loss", loss, global_step)
                        # 记录学习率
                        for i, param_group in enumerate(self.optimizer.param_groups):
                            summary_writer.add_scalar(
                                f"lr/group_{i}", param_group["lr"], global_step
                            )
                    if dev_data_loader and self.configs.local_rank in [-1, 0]:
                        UAS, LAS = self.dev(dev_data_loader, dev_CoNLLU_file)
                        if not self.configs.no_output:
                            summary_writer.add_scalar("metrics/uas", UAS, global_step)
                            summary_writer.add_scalar("metrics/las", LAS, global_step)
                        if best_result.is_new_record(
                            LAS=LAS, UAS=UAS, epoch=epoch
                        ) and self.configs.local_rank in [-1, 0]:
                            self.logger.info(
                                f"\n## NEW BEST RESULT in epoch {epoch} ##"
                            )
                            self.logger.info("\n" + str(best_result))
                            # 保存最优模型：
                            if not self.configs.no_output:
                                if hasattr(self.model, "module"):
                                    # 多卡,torch.nn.DataParallel封装model
                                    self.model.module.save_pretrained(
                                        self.configs.output_model_dir
                                    )
                                else:
                                    self.model.save_pretrained(
                                        self.configs.output_model_dir
                                    )

                if (
                    self.configs.early_stop
                    and epoch - best_result.best_LAS_epoch
                    > self.configs.early_stop_epochs
                    and self.configs.local_rank == -1
                ):
                    # 当使用 torch.distributed 训练时无法使用 early stop
                    # todo fix bug [bug when use torch.distributed.launch !!]
                    self.logger.info(f"\n## Early stop in step:{global_step} ##")
                    train_stop = True
                    break
            if train_stop:
                break
            # print(f'\n- Epoch {epoch} average loss : {epoch_ave_loss / len(train_data_loader)}')
            if self.configs.local_rank in [-1, 0] and not self.configs.no_output:
                summary_writer.add_scalar(
                    "epoch_loss", epoch_ave_loss / len(train_data_loader), epoch
                )
        if self.configs.local_rank in [-1, 0] and not self.configs.no_output:
            with open(self.configs.dev_result_path, "w", encoding="utf-8") as f:
                f.write(str(best_result) + "\n")
            self.logger.info("\n## BEST RESULT in Training ##")
            self.logger.info("\n" + str(best_result))
            summary_writer.close()

    def dev(
        self,
        dev_data_loader,
        dev_CoNLLU_file,
        input_conllu_path=None,
        output_conllu_path=None,
    ):
        if not isinstance(dev_CoNLLU_file, CoNLLFile):
            raise RuntimeError(f"dev_conllu_file type:{type(dev_CoNLLU_file)}")
        if input_conllu_path is None:
            input_conllu_path = os.path.join(
                self.configs.data_dir, self.configs.dev_file
            )
        if output_conllu_path is None:
            output_conllu_path = (
                self.configs.dev_output_path if not self.configs.no_output else None
            )
        dev_data_loader = tqdm(dev_data_loader, desc="Evaluation")
        predictions = []
        for step, batch in enumerate(dev_data_loader):
            self.model.eval()
            batch = tuple(t.to(self.configs.device) for t in batch)
            unpacked_batch = self._unpack_batch(batch)
            # unpacked_batch = {
            #         'inputs': inputs,
            #         'word_mask': word_mask,
            #         'sent_len': sent_len,
            #         'dep_ids': dep_ids,
            #         'pos_ids': pos_ids,
            #     }
            inputs, word_mask, sent_lens, dep_ids = (  # noqa: F841
                unpacked_batch["inputs"],
                unpacked_batch["word_mask"],
                unpacked_batch["sent_len"],
                unpacked_batch["dep_ids"],
            )
            word_mask = torch.eq(word_mask, 0)
            model_output = self.model(inputs)
            unlabeled_scores, labeled_scores = (
                model_output["unlabeled_scores"],
                model_output["labeled_scores"],
            )
            try:
                with torch.no_grad():
                    _, batch_prediction = self._update_and_predict(
                        unlabeled_scores,
                        labeled_scores,
                        None,
                        None,
                        word_mask,
                        # label_loss_ratio=self.model.module.label_loss_ratio if hasattr(self.model,'module') else self.model.label_loss_ratio,
                        sentence_lengths=sent_lens,
                        calc_loss=False,
                        update=False,
                        calc_prediction=True,
                    )
            except Exception as e:
                for b in batch:
                    print(b.shape)
                raise e
            predictions += batch_prediction
            # batch_sent_lens += sent_lens
        dev_CoNLLU_file.set(["deps"], [dep for sent in predictions for dep in sent])
        if output_conllu_path:
            dev_CoNLLU_file.write_conll(output_conllu_path)
        UAS, LAS = sdp_scorer.score(output_conllu_path, input_conllu_path)
        return UAS, LAS

    def inference(
        self, inference_data_loader, inference_CoNLLU_file, output_conllu_path
    ):
        inference_data_loader = tqdm(inference_data_loader, desc="Inference")
        predictions = []
        for step, batch in enumerate(inference_data_loader):
            self.model.eval()
            unpacked_batch = self._unpack_batch(batch)
            # unpacked_batch = {
            #         'inputs': inputs,
            #         'word_mask': word_mask,
            #         'sent_len': sent_len,
            #         'dep_ids': dep_ids,
            #         'pos_ids': pos_ids,
            #     }
            inputs, word_mask, sent_lens, _ = (
                unpacked_batch["inputs"],
                unpacked_batch["word_mask"],
                unpacked_batch["sent_len"],
                unpacked_batch["dep_ids"],
            )
            word_mask = torch.eq(word_mask, 0)
            model_output = self.model(inputs)
            unlabeled_scores, labeled_scores = (
                model_output["unlabeled_scores"],
                model_output["labeled_scores"],
            )
            with torch.no_grad():
                _, batch_prediction = self._update_and_predict(
                    unlabeled_scores,
                    labeled_scores,
                    None,
                    None,
                    word_mask,
                    # label_loss_ratio=self.model.label_loss_ratio if not self.args.data_paralle else self.model.module.label_loss_ratio,
                    sentence_lengths=sent_lens,
                    calc_loss=False,
                    update=False,
                    calc_prediction=True,
                )
            predictions += batch_prediction
        inference_CoNLLU_file.set(
            ["deps"], [dep for sent in predictions for dep in sent]
        )
        inference_CoNLLU_file.write_conll(output_conllu_path)
        return predictions


class TransformerBaseTrainer(BaseDependencyTrainer):
    def _unpack_batch(self, batch):
        pass


# class CharRNNBaseTrainer(BaseDependencyTrainer):
#     pass


if __name__ == "__main__":
    pass
