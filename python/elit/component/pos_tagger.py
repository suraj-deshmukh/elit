# ========================================================================
# Copyright 2017 Emory University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========================================================================
import argparse
import logging
from typing import Tuple, List

import mxnet as mx
import numpy as np
import fasttext
from fasttext.model import WordVectorModel
from gensim.models import KeyedVectors

from elit.component.template.lexicon import NLPLexiconMapper, NLPEmbedding
from elit.component.template.model import NLPModel
from elit.component.template.state import NLPState
from elit.component.template.util import argparse_ffnn, argparse_model, argparse_data, read_graphs, create_ffnn, \
    argparse_lexicon, conv_pool
from elit.reader import TSVReader
from elit.structure import NLPGraph, NLPNode

__author__ = 'Jinho D. Choi'


class POSLexicon(NLPLexiconMapper):
    def __init__(self, w2v: KeyedVectors=None, f2v: WordVectorModel=None, a2v: KeyedVectors=None,
                 output_size: int=50):
        """
        :param w2v: word embeddings from word2vec.
        :param f2v: word embeddings from fasttext.
        :param a2v: a2v classes.
        :param output_size: the number of part-of-speech tags to predict.
        """
        super().__init__(w2v, f2v)
        self.a2v: NLPEmbedding = NLPEmbedding(a2v, 'word', 'a2v') if a2v else None
        self.pos_zeros = np.zeros((output_size,)).astype('float32')


class POSState(NLPState):
    def __init__(self, graph: NLPGraph, lexicon: POSLexicon, save_gold=False):
        super().__init__(graph)
        self.lex: POSLexicon = lexicon

        # reset
        self.golds = [node.set_pos(None) for node in self.graph] if save_gold else None
        for node in self.graph: node.pos_scores = lexicon.pos_zeros
        self.idx_curr: int = 1

    def reset(self):
        for node in self.graph:
            node.pos = None
            node.pos_scores = self.lex.pos_zeros

        self.idx_curr = 1
        self.reset_count += 1

    # ============================== Oracle ==============================

    @property
    def gold(self) -> str:
        return self.golds[self.idx_curr - 1] if self.golds else None

    def eval(self, stats: np.array) -> float:
        if self.golds is None: return 0

        stats[0] += len(self.graph)
        for i, node in enumerate(self.graph):
            if node.pos == self.golds[i]:
                stats[1] += 1

        return stats[1] / stats[0]

    # ============================== Transition ==============================

    def process(self, label: str, scores: np.array=None):
        node: NLPNode = self.graph.nodes[self.idx_curr]
        if scores is not None: node.pos_scores = scores
        node.pos = label
        self.idx_curr += 1

    @property
    def terminate(self) -> bool:
        return self.idx_curr >= len(self.graph.nodes)

    # ============================== Feature ==============================

    def features(self, node: NLPNode) -> List[np.array]:
        fs = [node.pos_scores if node else self.lex.pos_zeros]
        if self.lex.w2v: fs.append(self.lex.w2v.get(node))
        if self.lex.f2v: fs.append(self.lex.f2v.get(node))
        if self.lex.a2v: fs.append(self.lex.a2v.get(node))
        return fs


class POSModel(NLPModel):
    def __init__(self, batch_size=32, num_label: int=50, feature_context: Tuple = (-2, -1, 0, 1, 2),
                 context: mx.context.Context=mx.cpu(), w2v_dim=200,
                 ngram_filter_list=(1, 2, 3), ngram_filter: int=64):
        super().__init__(POSState, batch_size)
        self.mxmod: mx.module.Module = self.init_mxmod(batch_size=batch_size,
                                                       num_label=num_label,
                                                       num_feature=len(feature_context),
                                                       context=context,
                                                       w2v_dim=w2v_dim,
                                                       ngram_filter_list=ngram_filter_list,
                                                       ngram_filter=ngram_filter)
        self.feature_context: Tuple[int] = feature_context

    # ============================== Feature ==============================

    def x(self, state: POSState) -> np.array:
        vectors = [feature for window in self.feature_context
                   for feature in state.features(state.get_node(state.idx_curr, window))]
        return np.concatenate(vectors, axis=0)

    # ============================== Module ==============================

    def init_mxmod(self, batch_size: int, num_label: int, num_feature: int, context: mx.context.Context, w2v_dim: int,
                   ngram_filter_list: Tuple, ngram_filter: int) -> mx.module.Module:
        # n-gram convolution
        input  = mx.sym.Variable('data')
        pooled = [conv_pool(input, conv_kernel=(filter, w2v_dim), num_filter=ngram_filter, act_type='relu',
                            pool_kernel=(num_feature - filter + 1, 1), pool_stride=(1, 1))
                  for filter in ngram_filter_list]
        concat = mx.sym.Concat(*pooled, dim=1)
        h_pool = mx.sym.Reshape(data=concat, shape=(batch_size, ngram_filter * len(ngram_filter_list)))
      # h_pool = mx.sym.Dropout(data=h_pool, p=dropouts[0]) if dropouts[0] > 0.0 else h_pool

        # fully connected
        fc_weight = mx.sym.Variable('fc_weight')
        fc_bias = mx.sym.Variable('fc_bias')
        fc = mx.sym.FullyConnected(data=h_pool, weight=fc_weight, bias=fc_bias, num_hidden=num_label)

        output = mx.sym.Variable('softmax_label')
        sm = mx.sym.SoftmaxOutput(data=fc, label=output, name='softmax')

        return mx.mod.Module(symbol=sm, context=context)


def parse_args():
    parser = argparse.ArgumentParser('Train a part-of-speech tagger')

    # data
    args= argparse_data(parser, tsv=lambda t: TSVReader(word_index=t[0], pos_index=t[1]))
    args.add_argument('--log', type=str, metavar='filepath', help='path to the logging file')

    # lexicon
    args = argparse_lexicon(parser)
    args.add_argument('--a2v', type=str, metavar='filepath', help='path to the ambiguity class bin file')

    # model
    def feature_context(s: str):
        return tuple(map(int, s.split(',')))

    argparse_ffnn(parser)
    args = argparse_model(parser)
    args.add_argument('--feature_context', type=feature_context, metavar='int,int*', default=[-2, -1, 0, 1, 2],
                      help='context window for feature extraction')

    return parser.parse_args()


def main():
    # arguments
    args = parse_args()
    if args.log: logging.basicConfig(filename=args.log, format='%(message)s', level=logging.INFO)
    else: logging.basicConfig(format='%(message)s', level=logging.INFO)

    # data
    trn_graphs = read_graphs(args.tsv, args.trn_data)
    dev_graphs = read_graphs(args.tsv, args.dev_data)

    # lexicon
    w2v = KeyedVectors.load_word2vec_format(args.w2v, binary=True) if args.w2v else None
    f2v = fasttext.load_model(args.f2v) if args.f2v else None
    a2v = KeyedVectors.load_word2vec_format(args.a2v, binary=True) if args.a2v else None
    lexicon = POSLexicon(w2v=w2v, f2v=f2v, a2v=a2v, output_size=args.output_size)

    # model
    model = POSModel(feature_context=args.feature_context)
    model.train(trn_graphs, dev_graphs, lexicon, num_steps=args.num_steps
                bagging_ratio=args.bagging_ratio, optimizer=args.optimizer)


if __name__ == '__main__':
    main()
