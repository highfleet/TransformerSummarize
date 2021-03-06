import torch
import torch.nn as nn
import torch.nn.functional as F
#from torch.nn import LayerNorm
from models.transformer import LayerNorm

from models.transformer import TransformerLayer, SelfAttentionMask, LearnedPositionalEmbedding, SinusoidalPositionalEncoding
from models.modules import WordProbLayer, LabelSmoothing
from utils.initialize import init_uniform_weight

class Model(nn.Module):

    def __init__(self, config):
        super(Model, self).__init__()
        self.config = config
        self.device = config['device']
        self.vocab_size = config['vocab_size']
        self.emb_dim = config['emb_dim']
        self.hidden_size = config['hidden_size']
        self.d_ff = config['d_ff']
        self.padding_idx = config['padding_idx']
        self.num_layers = config['num_layers']
        self.num_heads = config['num_heads']
        self.smoothing = config['label_smoothing']
        self.is_predicting = config['is_predicting']
        self.copy = config['copy']

        dropout = config['dropout']
        self.dropout = nn.Dropout(p=dropout)
        self.attn_mask = SelfAttentionMask(device=self.device)
        self.word_embed = nn.Embedding(self.vocab_size, self.emb_dim, self.padding_idx)
        #self.pos_embed = SinusoidalPositionalEmbedding(self.emb_dim, device=self.device)
        #self.pos_embed = SinusoidalPositionalEncoding(self.emb_dim, device=self.device)
        self.pos_embed = LearnedPositionalEmbedding(self.emb_dim, device = self.device)
        self.enc_layers = nn.ModuleList()
        self.dec_layers = nn.ModuleList()
        self.emb_layer_norm = LayerNorm(self.emb_dim, eps = 1e-12)    # copy & coverage not implemented...
        self.word_prob = WordProbLayer(self.hidden_size, self.vocab_size, self.device, dropout, copy=self.copy)
        self.label_smoothing = LabelSmoothing(self.device, self.vocab_size, self.padding_idx, self.smoothing)

        for _ in range(self.num_layers):
            self.enc_layers.append(TransformerLayer(self.hidden_size, self.d_ff,self.num_heads,dropout))
            self.dec_layers.append(TransformerLayer(self.hidden_size, self.d_ff,self.num_heads,dropout, with_external=True))

        #self.reset_parameters()

    def reset_parameters(self):
        init_uniform_weight(self.word_embed.weight)

    def label_smoothing_loss(self, pred, gold, mask = None):
        """
            mask 0 表示忽略 
            gold: seqlen, bsz
        """
        if mask is None: mask = gold.ne(self.padding_idx)
        seq_len, bsz = gold.size()
        # KL散度需要预测概率过log...
        pred = torch.log(pred.clamp(min=1e-8))  # 方便实用的截断函数P 
        # 本损失函数中, 每个词的损失不对seqlen作规范化
        return self.label_smoothing(pred.view(seq_len * bsz, -1),
                    gold.contiguous().view(seq_len * bsz, -1)) / mask.sum() # avg loss
        
    def nll_loss(self, pred:torch.Tensor, gold, dec_lens):
        """
            nll: 指不自带softmax的loss计算函数
            pred: seqlen, bsz, vocab
            gold: seqlen, bsz
        """
        gold_prob = pred.gather(dim=2, index=gold.unsqueeze(2)).squeeze(2).clamp(min=1e-8)  # cross entropy
        gold_prob = gold_prob.log().masked_fill(gold.eq(self.padding_idx), 0.).sum(dim=0) / dec_lens   # batch内规范化
        return -gold_prob.mean()

    def encode(self, inputs, padding_mask = None):
        if padding_mask is None: 
            padding_mask = inputs.eq(self.padding_idx)
        x = self.word_embed(inputs) + self.pos_embed(inputs)
        x = self.dropout(self.emb_layer_norm(x))

        for layer in self.enc_layers:
            x, _, _ = layer(x, self_padding_mask=padding_mask)
        
        return x, padding_mask

    def decode(self, inputs, src, src_padding_mask, padding_mask=None,
                src_extend_vocab = None, extra_zeros = None):      # if copy enabled
        """ copy not implemented """
        seqlen, _ = inputs.size()
        if not self.is_predicting and padding_mask is None:
            padding_mask = inputs.eq(self.padding_idx)
        x = self.word_embed(inputs) + self.pos_embed(inputs)
        x = self.dropout(self.emb_layer_norm(x))
        emb = x

        self_attn_mask = self.attn_mask(seqlen)

        for layer in self.dec_layers:
            x,_,_ = layer(x, self_padding_mask=padding_mask, self_attn_mask=self_attn_mask,
                    external_memories=src, external_padding_mask=src_padding_mask)
        if self.copy:
            pred, attn = self.word_prob(x, emb, memory=src, src_mask=src_padding_mask,
                        tokens = src_extend_vocab, extra_zeros= extra_zeros)
        else: pred, attn = self.word_prob(x)
        return pred, attn

    def forward(self, src, tgt, src_padding_mask=None, tgt_padding_mask=None,
            src_extend_vocab = None, extra_zeros = None):       # if copy enabled
        """
            src&tgt: seqlen, bsz
        """ 
        src_enc, src_padding_mask = self.encode(src, src_padding_mask)
        pred, attn = self.decode(tgt, src_enc, src_padding_mask, tgt_padding_mask, src_extend_vocab, extra_zeros)
        return pred