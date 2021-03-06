from __future__ import unicode_literals, print_function, division
import os
import time

import torch
from torch.utils.data import DataLoader
from rouge import FilesRouge

from models.model import Model
from data import Vocab, CNNDMDataset, Collate
from utils.train_utils import logging
from utils.data_utils import get_input_from_batch, output2words
from tqdm import tqdm

## Crimson Resolve
alpha = 0.9
beta = 5.

class Beam(object):
    def __init__(self, tokens, log_probs, state=None, coverage=None):
        self.tokens = tokens
        self.log_probs = log_probs
        self.state = state
        self.coverage = coverage

    def extend(self, token, log_prob, state = None, coverage = None):
        return Beam(tokens = self.tokens + [token],
                        log_probs = self.log_probs + [log_prob],
                        state = state,
                        coverage = None if coverage is None\
                                    else (self.coverage+coverage))

    @property
    def c_score(self):
        return 0 if self.coverage is None else\
                 -beta*(self.coverage.clamp_min(1.).sum() - self.coverage.size(0))

    @property
    def latest_token(self):
        return self.tokens[-1]

    @property
    def avg_log_prob(self):
        return sum(self.log_probs) / len(self.tokens)

    @property
    def coverage_prob(self):
        return sum(self.log_probs) + self.c_score

    @property
    def decay_prob(self):
        # length penalty with coverage
        penalty = ((5.0+(len(self.tokens) + 1)) / 6.0)**alpha
        return sum(self.log_probs)/ penalty

class BeamSearch(object):
    """ 可可爱爱的标准Beam Search模板 """
    def __init__(self, config):
        self.config = config
        saved_model = torch.load(config['test_from'], map_location='cpu')
        self.model = Model(config)
        self.model.load_state_dict(saved_model['model'])
        self.model.to(config['device'])
        self.vocab = saved_model['vocab']
        
        self._decode_dir = os.path.join(config['log_root'], 'decode_S%s' % str(saved_model['step']))
        self._rouge_ref = os.path.join(self._decode_dir, 'rouge_ref')
        self._rouge_dec = os.path.join(self._decode_dir, 'rouge_dec')

        if not os.path.exists(self._decode_dir): os.mkdir(self._decode_dir)
        self.test_data = CNNDMDataset('test', config['data_path'], config, self.vocab)
        
    def sort_beams(self, beams):
        return sorted(beams, key=lambda h: h.coverage_prob, reverse=True)

    def sort_hypos(self, beams):
        return sorted(beams, key=lambda h: h.decay_prob, reverse=True)

    @staticmethod
    def report_rouge(ref_path, dec_path):
        print("Now starting ROUGE eval...")
        files_rouge = FilesRouge(dec_path, ref_path)
        scores = files_rouge.get_scores(avg=True)
        logging(str(scores))

    def get_summary(self, best_summary, batch):
        # Extract the output ids from the hypothesis and convert back to words
        output_ids = [int(t) for t in best_summary.tokens[1:]]
        decoded_words = output2words(output_ids, self.vocab,
                                                (batch.art_oovs[0] if self.config['copy'] else None))

        # Remove the [STOP] token from decoded_words, if necessary
        try:
            fst_stop_idx = decoded_words.index('<end>')
            decoded_words = decoded_words[:fst_stop_idx]
        except ValueError:
            decoded_words = decoded_words
        decoded_abstract = ' '.join(decoded_words)
        return decoded_abstract

    @torch.no_grad()
    def decode(self):
        config = self.config
        start = time.time()
        self.model.eval()       # ...! qwq
        test_loader = DataLoader(self.test_data, batch_size=1, shuffle = False, collate_fn=Collate(beam_size = config["beam_size"]))
        
        ref = open(self._rouge_ref, 'w')
        dec = open(self._rouge_dec, 'w')
        
        for batch in tqdm(test_loader):
            # Run beam search to get best hypothesis
            best_summary = self.beam_search(batch)

            original_abstract = batch.original_abstract[0]
            decoded_abstract = self.get_summary(best_summary, batch)

            ref.write(original_abstract + '\n')
            dec.write(decoded_abstract + '\n')

        ref.close()
        dec.close()
        self.report_rouge(self._rouge_ref, self._rouge_dec)

    def beam_search(self, batch):
        """
            TODOS:
            add tri-gram blocking
            add coverage mechanism xD
        """
        config = self.config
        # batch should have only one example
        enc_batch, enc_padding_mask, enc_lens, enc_batch_extend_vocab, extra_zeros, coverage_t_0 = \
            get_input_from_batch(batch, config, config['device'])

        encoder_outputs, padding_mask = self.model.encode(enc_batch, enc_padding_mask)

        # decoder batch preparation, it has beam_size example initially everything is repeated
        beams = [Beam(tokens=[self.vocab.word2id('<start>')],
                      log_probs=[0.0],
                      coverage=(coverage_t_0[0] if config['coverage'] else None)) 
                      for _ in range(config['beam_size'])]
        results = []
        steps = 0
        while steps < config['max_dec_steps'] and len(results) < config['beam_size']:
            hyp_tokens = torch.tensor([h.tokens for h in beams],device=config['device']).transpose(0,1) # NOT batch first
            hyp_tokens.masked_fill_(hyp_tokens>=self.vocab.size, self.vocab.word2id('<unk>'))# convert oov to unk
            pred, attn = self.model.decode(hyp_tokens, encoder_outputs, padding_mask, None,
                                     enc_batch_extend_vocab, extra_zeros)

            # gather attention at current step
            attn = attn[-1,:,:]  # attn: [bsz * src_len]
            print(attn.size())
            log_probs = torch.log(pred[-1,:,:])         # get probs for next token
            topk_log_probs, topk_ids = torch.topk(log_probs, config['beam_size'] * 2)  # avoid all <end> tokens in top-k
            # print(topk_ids)
            # print(topk_log_probs)
            all_beams = []
            num_orig_beams = 1 if steps == 0 else len(beams)
            for i in range(num_orig_beams):
                h = beams[i]
                # here save states, context vec and coverage...
                for j in range(config['beam_size'] * 2):  # for each of the top 2*beam_size hyps:
                    new_beam = h.extend(token=topk_ids[i, j].item(),
                                   log_prob=topk_log_probs[i, j].item(),
                                   coverage=attn[i] if config['coverage'] else None)
                    all_beams.append(new_beam)

            beams = []
            for h in self.sort_beams(all_beams):
                if h.latest_token == self.vocab.word2id('<end>'):
                    if steps >= config['min_dec_steps']:
                        results.append(h)
                else: beams.append(h)
                # if len(beams) == config['beam_size'] or len(results) == config['beam_size']:
                #     break
            steps += 1

        if len(results) == 0:
            results = beams

        beams_sorted = self.sort_hypos(results)

        return beams_sorted[0]
