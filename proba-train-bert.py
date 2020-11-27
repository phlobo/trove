#
# Train BERT / Transformer model
#
# TODO: Migrate all the BERT models to the latest huggingface library
# https://huggingface.co/transformers/migration.html
#
import os
import sys
import time

import torch
import shutil
import trove
import logging
import warnings
import argparse
import numpy as np
import itertools
from torch.utils import data
import torch.nn as nn
import torch.nn.utils.rnn as rnn_utils
import torch.nn.functional as F
from pytorch_pretrained_bert.optimization import BertAdam
from pytorch_pretrained_bert import BertTokenizer, BertModel
from trove.models.loss import SoftCrossEntropyLoss
from trove.analysis.metrics import score_sequences

logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)
torch.backends.cudnn.deterministic = True

###############################################################################
#
# Model
#
###############################################################################
class TaggerBert(nn.Module):

    def __init__(self,
                 num_classes,
                 bert_model='bert-base-cased',
                 use_subword_labels=False,
                 use_rnn=False,
                 rnn_dropout=0.1,
                 rnn_num_layers=2,
                 device='cpu',
                 finetuning=False):

        super().__init__()
        self.use_subword_labels = use_subword_labels
        self.bert = BertModel.from_pretrained(bert_model) if type(
            bert_model) is str else bert_model

        self.use_rnn = use_rnn
        if use_rnn:
            self.rnn = nn.LSTM(bidirectional=True,
                               num_layers=rnn_num_layers,
                               input_size=768,
                               hidden_size=768 // 2,
                               batch_first=True,
                               dropout=rnn_dropout)
            # self.dropout = nn.Dropout(p=lstm_dropout)

        self.fc = nn.Linear(768, num_classes)

        self.device = device
        self.finetuning = finetuning

        print('finetuning', self.finetuning)
        print('use_rnn', use_rnn)
        print('num_classes', num_classes)
        print('use_subword_labels', use_subword_labels)

    def _forward_rnn(self, X, lens):

        X_packed = rnn_utils.pack_padded_sequence(
            X, lens, batch_first=True, enforce_sorted=False
        )
        output, (h_n, c_n) = self.rnn(X_packed)
        output, _ = rnn_utils.pad_packed_sequence(output, batch_first=True)
        return output

    def forward(self, xs, xidxs):

        xs = xs.to(self.device).long()

        if self.training and self.finetuning:
            self.bert.train()
            enc_layers, _ = self.bert(xs)
            enc_layers = enc_layers[-1]
        else:
            self.bert.eval()
            with torch.no_grad():
                enc_layers, _ = self.bert(xs)
                enc_layers = enc_layers[-1]

        # restrict to head tokens (no subwords)
        if not self.use_subword_labels:
            enc = [layer[idxs] for layer, idxs in zip(enc_layers, xidxs)]
            x_lens = [layer.size(0) for layer in enc]

        # ignore [CLS] and [SEP] special tokens
        else:
            enc = [layer[1:-1] for layer in enc_layers]
            x_lens = [layer.size(0) for layer in enc]

        enc = rnn_utils.pad_sequence(enc, batch_first=True, padding_value=0)

        # Jointly training an LSTM + finetuning BERT doesn't help
        # TODO: finetune BERT, freeze; train LSTM ?
        if self.use_rnn:
            enc = self._forward_rnn(enc, x_lens)

        logits = self.fc(enc)

        return logits

###############################################################################
#
# Loading Datasets
#
###############################################################################

class ProbaConllDataset(object):
    """
    Format Example (word proba_y_hat_1....k mask y)

        PTU         0.0  1.0  1  B
        -           1.0  0.0  1  O
        associated  1.0  0.0  1  O
        vasculitis  1.0  0.0  1  O

    """
    def __init__(self, fpath):
        # load sequences and tags
        self.xs, self.probas, self.masks, self.ys = ProbaConllDataset.load_file(fpath)

    @staticmethod
    def load_file(fpath):
        entries = open(fpath, 'r').read().strip().split("\n\n")
        words, probas, masks, ys = [], [], [], []
        for i, entry in enumerate(entries):
            x, p, m, y = [], [], [], []
            for line in entry.splitlines():
                line_split = line.split()
                x.append(line_split[0])
                p.append(line_split[1:-2])
                m.append(line_split[-2])
                y.append(line_split[-1])
            words.append(np.array(x))
            probas.append(np.array(p).astype(np.float32))
            masks.append(np.array(m).astype(np.bool))
            ys.append(y)
        return words, probas, masks, ys


class BertSequenceDataset(object):

    def __init__(self, xs, probas, ys, masks, tokenizer, tag_to_idx,
                 max_seq_len=512):
        self.xs = xs
        self.probas = probas
        self.ys = ys
        self.masks = masks
        self.tokenizer = tokenizer
        self.tag_to_idx = tag_to_idx
        self.max_seq_len = max_seq_len

    @classmethod
    def from_file(cls, fpath, tokenizer, tag2idx=None, max_seq_len=512):
        tag2idx = {'O': 0, 'I': 1, 'B': 2} if not tag2idx else tag2idx
        d = ProbaConllDataset(fpath)
        return cls(d.xs, d.probas, d.ys, d.masks, tokenizer,
                   tag_to_idx=tag2idx, max_seq_len=max_seq_len)

    def __len__(self):
        return len(self.xs)

    def __getitem__(self, i):
        toks = [self.tokenizer.tokenize(x) for x in self.xs[i]]
        tok_starts = list(itertools.chain.from_iterable(
            [[1] + [0] * (len(t) - 1) for t in toks]))

        # truncate long sequences
        toks = list(itertools.chain.from_iterable(toks))
        if len(toks) - 2 > self.max_seq_len:
            toks = toks[:self.max_seq_len - 2]

        tok_starts = torch.tensor(
            [i + 1 for i in range(len(tok_starts)) if tok_starts[i] == 1])
        toks = np.array(['[CLS]'] + toks + ['[SEP]'])
        xs = torch.tensor(self.tokenizer.convert_tokens_to_ids(toks))
        ys = torch.tensor([self.tag_to_idx[y] for y in self.ys[i]])
        probas = torch.tensor(self.probas[i])
        masks = torch.tensor(self.masks[i].astype(int))
        return xs, tok_starts, probas, masks, ys, len(toks)


def pad_bert_seqs(batch, pad_idx=0):

    f = lambda x: [sample[x] for sample in batch]
    max_len = np.array(f(5)).max()

    xs, xidxs, probas, masks, ys, seq_lens = [], [], [], [], [], []
    for sample in batch:
        x, xidx, proba, mask, y, seq_len = sample
        # pad x to fixed max length
        pad_len = max_len - len(x)
        x_pad = torch.LongTensor([pad_idx] * pad_len)
        xs.append(torch.cat((x, x_pad), 0))

        xidxs.append(xidx)
        probas.append(proba)
        masks.append(mask)
        ys.append(y)
        seq_lens.append(seq_len)

    return torch.stack(xs, 0), xidxs, probas, masks, ys, seq_lens

###############################################################################
#
# Training
#
###############################################################################

def assign_subword_labels(batch, tokenizer):

    xs, xidxs, probas, masks, ys, x_lens = batch

    ext_probas, ext_masks = [], []
    for j in range(len(xs)):
        x = [int(idx) for idx in xs[j]]
        toks = tokenizer.convert_ids_to_tokens(x)
        toks = [t for t in toks if t not in ['[PAD]', '[CLS]', '[SEP]']]
        # expand proba if we're using subword labels
        proba, mask = [], []
        idx = -1
        for t in toks:
            if t[:2] != '##':
                idx += 1
            proba.append(probas[j][idx])
            mask.append(masks[j][idx])
        probas.append(torch.stack(proba))
        masks.append(torch.stack(mask))
    return ext_probas, ext_masks


def train_model(model,
                data,
                optimizer,
                criterion,
                tokenizer,
                mask_proba=True,
                use_subword_labels=False,
                device='cpu'):

    total_loss = 0
    model.train()
    for i, batch in enumerate(data):

        xs, xidxs, probas, masks, ys, x_lens = batch
        optimizer.zero_grad()

        logits = model.forward(xs, xidxs)

        # Some authors have reported better performance propagating
        # labels from the head token across all constitute subwords
        # see https://github.com/google-research/bert/issues/646
        if use_subword_labels:
            probas, masks = assign_subword_labels(batch, tokenizer)

        logits = [logits[j][:len(probas[j])] for j in range(len(xs))]

        # Masking has 2 options:
        # 1) Ignore, train with whatever token prior is defined in the dataset,
        #    even if the token is masked (e.g., class balance or uniform)
        # 2) Mask from the loss calculation
        if mask_proba:
            logits = [logit[mask.nonzero()] for logit, mask in
                      zip(logits, masks)]
            probas = [proba[mask.nonzero()].view(-1, proba.size(1)) for
                      proba, mask in zip(probas, masks)]

        logits = torch.cat(logits, 0)
        logits = logits.view(-1, logits.shape[-1])
        probas = torch.cat(probas, 0)
        probas = probas.to(device=device)

        loss = criterion(logits, probas)
        loss.backward()

        optimizer.step()
        total_loss += float(loss)

    return total_loss / len(data)


def get_tokens(xs, xidxs, tokenizer):

    seqs = [tokenizer.convert_ids_to_tokens(x.data.cpu().numpy()) for x in xs]
    ignore = ['[PAD]', '[SEP]', '[CLS]']
    tokens = []
    for i, seq in enumerate(seqs):
        # merge word pieces to their original token spans
        words = []
        for j, tok in enumerate(seq):
            if tok in ignore:
                continue
            if j in xidxs[i]:
                words.append(tok)
            elif tok[:2] == '##':
                words[-1] += tok[2:]
            else:
                print('ERR')
        tokens.append(words)
    return tokens


def predict(model, iterator, tokenizer, idx_to_tag):

    model.eval()
    seqs, y_pred, y_true = [], [], []
    for i, batch in enumerate(iterator):
        xs, xidxs, probas, masks, ys, x_lens = batch

        logits = model.forward(xs, xidxs)

        #if use_subword_labels:
        #    probas, masks = assign_subword_labels(batch, tokenizer)

        y_hats = logits.argmax(-1)
        y_hats = [y[:len(m)] for y, m in zip(y_hats, masks)]
        tokens = get_tokens(xs, xidxs, tokenizer)

        ys = [[idx_to_tag[int(y)] for y in ys[j]] for j in range(len(ys))]
        y_hats = [[idx_to_tag[int(y)] for y in y_hats[j]] for j in
                  range(len(y_hats))]

        y_true.extend(ys)
        y_pred.extend(y_hats)
        seqs.extend(tokens)

    return seqs, y_true, y_pred


def save_checkpoint(state, is_best, root_dir='', filename='checkpoint.tar'):
    """
    Model checkpoint + saving best model

    :param state:
    :param is_best:
    :param root_dir:
    :param filename:
    :return:
    """
    root_dir + '/' if root_dir else root_dir
    model_path = f'{root_dir}{filename}'
    torch.save(state, model_path)
    if is_best:
        shutil.copyfile(model_path, f'{root_dir}best.tar')


def score_model(model, data, tokenizer, idx2tag, metrics=None):
    seqs, y_true, y_pred = predict(model, data, tokenizer, idx2tag)
    return score_sequences(y_true, y_pred, metrics=metrics)


def main(args):

    ts = time.strftime("%Y-%m-%d_%H%M%S", time.gmtime())
    checkpoint_dir = f"{args.checkpoints}/{ts}_seed_{args.seed}/"
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir, exist_ok=True)
    # -------------------------------------------------------------------------
    # Load Pre-trained BERT Models
    # -------------------------------------------------------------------------
    tokenizer = BertTokenizer.from_pretrained(args.model, do_lower_case=False)

    # -------------------------------------------------------------------------
    # Load Dataset
    # -------------------------------------------------------------------------

    dataset = [
        BertSequenceDataset.from_file(args.train, tokenizer,
                                      max_seq_len=args.max_seq_len),
        BertSequenceDataset.from_file(args.dev, tokenizer,
                                      max_seq_len=args.max_seq_len),
        BertSequenceDataset.from_file(args.test, tokenizer,
                                      max_seq_len=args.max_seq_len)
    ]

    iterators = [
        data.DataLoader(dataset=dataset[i],
                        batch_size=args.batch_size,
                        shuffle=not i,
                        num_workers=args.num_workers,
                        collate_fn=pad_bert_seqs)
        for i in range(0, 3)
    ]

    print("Datasets loaded")

    idx_to_tag = {i:f'{tag}-X' for i,tag in enumerate(dataset[0].tag_to_idx)}
    print('idx_to_tag', idx_to_tag)
    num_classes = 2 # HACK

    model = TaggerBert(num_classes=num_classes,
                       bert_model=args.model,
                       use_rnn=args.use_rnn,
                       use_subword_labels=args.subword_labels,
                       finetuning=not args.no_finetuning,
                       device=args.device)

    if args.device == 'cuda':
        model = model.cuda()
    model = torch.nn.DataParallel(model)


    criterion = trove.models.loss.SoftCrossEntropyLoss()

    train_size = len(dataset[0])
    num_total_steps = int(np.ceil(train_size / args.batch_size)) * args.n_epochs
    warmup_proportion = 0.1
    optimizer = BertAdam(model.parameters(),
                         lr=args.lr,
                         schedule='warmup_linear',
                         warmup=warmup_proportion,
                         t_total=num_total_steps)

    # -------------------------------------------------------------------------
    # Train
    # -------------------------------------------------------------------------
    best_score = 0.0
    for epoch in range(1, args.n_epochs + 1):
        loss = train_model(model,
                           iterators[0],
                           optimizer,
                           criterion,
                           tokenizer=tokenizer,
                           mask_proba=not args.ignore_masks)
        print(f"Epoch: {epoch}, Loss: {loss}")
        score = score_model(model, iterators[1], tokenizer, idx_to_tag)
        print("Accuracy:{}, F1:{}".format(score['accuracy'], score['f1']))

        state = {
            'n_epochs': args.n_epochs,
            'best_score': best_score,
            'state_dict': model.state_dict(),
            'optimizer': optimizer.state_dict()
        }

        if score[args.checkpoint_metric] > best_score:
            msg = f"[NEW BEST] | Acc: {score['accuracy']*100:2.2f} | P: {score['precision']*100:2.2f} | R: {score['recall']*100:2.2f} | F1: {score['f1']:2.2f}"
            print(msg)
            best_score = score[args.checkpoint_metric]
            state['best_score'] = best_score
            save_checkpoint(state, is_best=True, root_dir=checkpoint_dir)

        elif epoch % args.checkpoint_freq == 0:
            save_checkpoint(state, is_best=False, root_dir=checkpoint_dir)

    # -------------------------------------------------------------------------
    # Score End Model
    # -------------------------------------------------------------------------
    # load best checkpoint
    map_location = torch.device('cpu') if args.device == 'cpu' else None
    state = torch.load(f'{checkpoint_dir}/best.tar', map_location=map_location)

    # report token measures
    print(f'BEST Model loaded from: {checkpoint_dir}/best.tar')
    score = score_model(model, iterators[2], tokenizer, idx_to_tag)
    print(score)


if __name__=="__main__":

    parser = argparse.ArgumentParser()

    # dataset
    parser.add_argument("--train", type=str, default=None)
    parser.add_argument("--dev", type=str, default=None)
    parser.add_argument("--test", type=str, default=None)

    parser.add_argument("--subword_labels", action="store_true")
    parser.add_argument("--ignore_masks", action="store_true")
    parser.add_argument("--no_finetuning", dest="no_finetuning", action="store_true")
    parser.add_argument("--use_rnn", dest="use_rnn", action="store_true")
    parser.add_argument("--max_seq_len", type=int, default=512 )

    parser.add_argument('--model', type=str, default='bert-base-cased')

    parser.add_argument("--checkpoints", type=str, default="checkpoints/")
    parser.add_argument("--checkpoint_metric", type=str, default="f1")
    parser.add_argument("--checkpoint_freq", type=int, default=5,
                        help="checkpoint model every k epochs")
    parser.add_argument("--num_workers", type=int, default=4)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--quiet", action="store_true",
                        help="suppress logging")

    # hyperparams
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--n_epochs", type=int, default=30)

    args = parser.parse_args()

    if not args.quiet:
        logging.basicConfig(format='%(message)s', stream=sys.stdout,
                            level=logging.INFO)

    if not torch.cuda.is_available() and args.device.lower() == 'cuda':
        logger.error("Warning! CUDA not available, defaulting to CPU")
        args.device = "cpu"

    if torch.cuda.is_available():
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        logger.info("torch.backends.cudnn.deterministic={}".format(
            torch.backends.cudnn.deterministic))
        logger.info("torch.backends.cudnn.benchmark={}".format(
            torch.backends.cudnn.benchmark))

    # print command line arguments
    for argname,value in args.__dict__.items():
        print(f'{argname:<20}: {value}')

    # set our random seeds
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    main(args)



