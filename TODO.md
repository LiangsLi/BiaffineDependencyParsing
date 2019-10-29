# TODO LIST
- [x] 跑通模型
- [x] 多卡
- [ ] 保存、加载
- [x] 跳过超过最大句长的句子
- [ ] 调整BERT 优化器的参数
- [ ] decoder部分的参数初始化
- [x] bert下简化的biaffine scorer
- [ ] 按照句长均分loss
- [ ] 按照累计句长划分batch
- [ ] 修改GraphVocab，支持过滤低频次的标签
- [ ] 保存加载预处理的dataset
- [ ] 支持xlnet和roberta
- [ ] 重构Transformer的输入
- [ ] 重构Transformer的encoder
- [ ] 重构CharRNN的输入
- [ ] 重构HLSTM的encoder
- [ ] 重构预测得到probs的后处理部分
- [x] 删除无用、过时代码
- [ ] 支持tensorboardX
- [ ] 支持句法依存分析
- [ ] BERT后添加一个self-attention层
- [ ] 加入Layer Attention、Layer Dropout
- [ ] Input Masking
- [ ] inverse square root learning rate decay (Vaswani et al., 2017). 
- [ ] 多任务训练 text/news分成两个decoder一起训练（此时训练集也得分开）
- [ ] 多任务训练 + POS 标注
- [ ] 多任务训练 + NER
