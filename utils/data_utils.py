import torch
from torch.autograd import Variable

PAD = 0
UNK = 1
START = 2
END = 3

special_tokens  = ['<pad>', '<unk>', '<start>', '<end>']

def make_vocab(wc, vocab_size):
    word2id, id2word = {}, {}
    for i, t in enumerate(special_tokens):
        word2id[t], id2word[i] = i, t
    for i, (w, _) in enumerate(wc.most_common(vocab_size - len(special_tokens)), len(special_tokens)):
        word2id[w], id2word[i] = i, w
    return word2id, id2word

def make_embedding(emb_path, vocab, emb_size):
    emb_matrix = torch.randn((vocab.size, emb_size), dtype=torch.float)
    with open(emb_path, 'r') as emb:
        for entry in emb:
            entry = entry.split()
            word = entry[0]
            idx = vocab.word2id(word)
            if idx!=UNK:
                emb = torch.tensor([float(v) for v in entry[1:]])
                emb_matrix[idx] = emb
    return emb_matrix

def article2ids(words, vocab):
    ids = []
    oovs = []
    for w in words:
        i = vocab.word2id(w)
        if i == UNK:
            if w not in oovs:
                oovs.append(w)
            ids.append(vocab.size + oovs.index(w))
        else: ids.append(i)

    return ids, oovs

def abstract2ids(words, vocab, article_oovs):
    ids = []
    for w in words:
        i = vocab.word2id(w)
        if i == UNK:
            if w in article_oovs:
                ids.append(vocab.size + article_oovs.index(w))
            else: ids.append(UNK)
        else: ids.append(i)
    return ids

def output2words(ids, vocab, art_oovs):
    words = []
    for i in ids:
        w = vocab.id2word(i) if i < vocab.size else art_oovs[i - vocab.size]
        words.append(w)
    return words

def show_art_oovs(article, vocab):
    words = article.split(' ')
    words = [("__%s__" % w) if vocab.word2id(w)==UNK else w for w in words]
    out_str = ' '.join(words)
    return out_str

def pad_sequence(data, padding_idx=0, length = 0):
    """
        Padder 
        输入：list状的 参差不齐的东东
        输出：list状的 整齐的矩阵
    """
    if length==0: length = max(len(entry) for entry in data)
    return [d + [padding_idx] * (length - len(d)) for d in data]

def get_input_from_batch(batch, config, device, batch_first = False):
    """
        returns: enc_batch, enc_pad_mask, enc_lens, enc_batch_extend_vocab, extra_zeros, c_t_1, coverage
        如果config没有启用pointer 和cov 则相应的项返回None
    """

    enc_batch = batch.enc_inp.to(device)
    enc_pad_mask = batch.enc_pad_mask.to(device)
    batch_size, seqlen = enc_batch.size()
    enc_lens = batch.enc_lens
    coverage_1 = None
    extra_zeros = None
    enc_batch_extend_vocab = None

    if config['copy']:
        enc_batch_extend_vocab = batch.art_batch_extend_vocab.to(device)
        # max_art_oovs is the max over all the article oov list in the batch
        if batch.max_art_oovs > 0:
            extra_zeros = torch.zeros((batch_size, 1, batch.max_art_oovs), device = device, requires_grad=False)
    
    if config['coverage']:
        coverage_1 = torch.zeros((batch_size, seqlen), device=device, requires_grad=False)

    if not batch_first:
        enc_batch.transpose_(0, 1)
        enc_pad_mask.transpose_(0, 1)
        if config['copy'] and extra_zeros is not None:
            extra_zeros.transpose_(0, 1)

    return enc_batch, enc_pad_mask, enc_lens, enc_batch_extend_vocab, extra_zeros, coverage_1

def get_output_from_batch(batch, device, batch_first = False):
    """ returns: dec_batch, dec_pad_mask, max_dec_len, dec_lens_var, tgt_batch """
    dec_lens = batch.dec_lens
    dec_lens_var = torch.tensor(dec_lens).float()
    # 这个东东是用来规范化batch loss用的
    # 每一句的总loss除以它的词数
    max_dec_len = max(dec_lens)

    dec_batch = batch.dec_inp.to(device)
    dec_pad_mask = batch.dec_pad_mask.to(device)
    tgt_batch = batch.dec_tgt.to(device)
    dec_lens_var = dec_lens_var.to(device)

    if not batch_first:
        dec_batch.transpose_(0, 1)
        tgt_batch.transpose_(0, 1)
        dec_pad_mask.transpose_(0, 1)

    return dec_batch, dec_pad_mask, max_dec_len, dec_lens_var, tgt_batch