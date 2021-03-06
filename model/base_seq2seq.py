import torch

from argparse import Namespace
from torch import nn
from torch.nn.utils.rnn import pad_sequence, pad_packed_sequence, pack_padded_sequence
from typing import List
import numpy as np



class Seq2SeqModel(nn.Module):
    def __init__(self, src_embedding: nn.Embedding, dst_embedding: nn.Embedding, config):
        super(Seq2SeqModel, self).__init__()
        self.src_embedding = src_embedding
        self.dst_embedding = dst_embedding

        self.src_embedding_dim = self.src_embedding.embedding_dim
        self.dst_embedding_dim = self.dst_embedding.embedding_dim

        self.output_dim = self.dst_embedding.num_embeddings

        self.bos_idx = config.bos_idx
        self.eos_idx = config.eos_idx

        self.lstm_dim = config.lstm_dim
        self.direction = config.direction
        # self.direction = 1
        self.bidirectional = True if self.direction == 2 else False
        self.num_layers = config.num_layers
        self.encoder = nn.LSTM(self.src_embedding_dim, self.lstm_dim, batch_first=True,
                               bidirectional=self.bidirectional,
                               num_layers=config.num_layers)
        self.decoder = nn.LSTM(self.dst_embedding_dim, self.lstm_dim * self.direction, batch_first=True,
                               num_layers=config.num_layers)

        self.linear = nn.Linear(self.lstm_dim * self.direction, self.output_dim)

        self.softmax = nn.Softmax(dim=-1)
        self.loss_ignore_idx = config.loss_ignore_idx
        self.loss = nn.CrossEntropyLoss(ignore_index=self.loss_ignore_idx)

        self.beam_size = config.beam_size
        self.device = config.device

    def forward_and_get_loss(self, x: List[torch.LongTensor], y: List[torch.LongTensor]):
        encoder_outputs, hidden = self.encoder_forward(x)

        decoder_inputs = [sent[:-1] for sent in y]
        decoder_target_outputs = [sent[1:] for sent in y]
        decoder_outputs, hidden = self.decoder_forward(decoder_inputs, hidden)

        if self.device == 'cuda':
            decoder_target_outputs = [sent[1:].cuda() for sent in y]

        decoder_target_outputs = pad_sequence(decoder_target_outputs, batch_first=True,
                                              padding_value=self.loss_ignore_idx)

        loss = self.loss(decoder_outputs.permute(0, 2, 1), decoder_target_outputs)

        return self.softmax(decoder_outputs), loss

    def predict(self, x, max_len=50, beam_size=5):
        outputs = []
        for x_i in x:
            outputs.append(self.predict_one_sentence(x_i, max_len, beam_size))

        return outputs

    def predict_one_sentence_(self, x, max_len=50, beam_size=5):
        encoder_outputs, hidden = self.encoder_forward([x])
        decoder_inputs = [torch.LongTensor([self.bos_idx])]
        # decoder_inputs shape (1, 1)
        outputs = []

        for i in range(1, max_len):
            decoder_outputs, hidden = self.decoder_forward(decoder_inputs, hidden)
            decoder_outputs = torch.topk(decoder_outputs.reshape((-1,)), k=1)
            decoder_outputs = decoder_outputs.indices.tolist()[0]

            outputs.append(decoder_outputs)
            if decoder_outputs == self.eos_idx:
                break
            decoder_inputs = [torch.LongTensor([decoder_outputs])]

        return outputs

    def normalize_prob(self, prob):
        return np.log(1 + prob)

    def predict_one_token(self, decoder_inputs, hidden, beam_size=5):
        decoder_outputs, hidden = self.decoder_forward(decoder_inputs, hidden)
        decoder_outputs = torch.topk(self.softmax(decoder_outputs).reshape((-1,)), k=beam_size)
        topk_output_indices = decoder_outputs.indices.tolist()
        topk_output_values = decoder_outputs.values.tolist()

        return hidden, topk_output_indices, topk_output_values

    def predict_one_sentence(self, x, max_len=50, beam_size=5):
        encoder_outputs, hidden = self.encoder_forward([x])
        decoder_inputs = [torch.LongTensor([self.bos_idx])]
        # decoder_inputs shape (1, 1)

        hidden, topk_output_indices, topk_output_values = self.predict_one_token(decoder_inputs, hidden, beam_size)
        res = []
        for i in range(len(topk_output_indices)):
            res.append([[topk_output_indices[i]], self.normalize_prob(topk_output_values[i]), hidden])

        for i in range(1, max_len):
            candidates = res[:]
            for pos in range(len(res)):
                input_ids, accumulate_prob, hidden = res[pos]
                input_id = input_ids[-1]
                decoder_inputs = [torch.LongTensor([input_id])]
                if input_id != self.eos_idx:
                    new_hidden, topk_output_indices, topk_output_values = self.predict_one_token(decoder_inputs, hidden,
                                                                                                 beam_size)
                    for i in range(len(topk_output_indices)):
                        candidates.append([input_ids[:] + [topk_output_indices[i]],
                                           (accumulate_prob + self.normalize_prob(topk_output_values[i])),  # normalize with len
                                           new_hidden])

            candidates.sort(key=lambda x: x[1], reverse=True)
            res = candidates[:beam_size]

        return res[0][0]

    def encoder_forward(self, x):
        if self.device == 'cuda':
            x = [i.cuda() for i in x]

        lens = [len(sent) for sent in x]

        # padding
        x = pad_sequence(x, batch_first=True, padding_value=self.src_embedding.padding_idx)
        x = self.src_embedding(x)  # shape: batch * max(lens) * embedding_dim

        # packing
        x = pack_padded_sequence(x, lens, batch_first=True, enforce_sorted=False)

        # forward
        out_packed, (h, c) = self.encoder(x)
        h = h.reshape(self.num_layers, self.direction, len(lens), -1).transpose(2, 1).reshape(self.num_layers,
                                                                                              len(lens), -1)
        c = c.reshape(self.num_layers, self.direction, len(lens), -1).transpose(2, 1).reshape(self.num_layers,
                                                                                              len(lens), -1)

        return out_packed, (h, c)

    def decoder_forward(self, decoder_inputs, hidden):
        if self.device == 'cuda':
            decoder_inputs = [i.cuda() for i in decoder_inputs]

        decoder_inputs_lens = [len(sent) for sent in decoder_inputs]
        decoder_inputs = pad_sequence(decoder_inputs, batch_first=True,
                                      padding_value=self.dst_embedding.padding_idx)
        # print(decoder_inputs)
        decoder_inputs = self.dst_embedding(decoder_inputs)
        decoder_inputs = pack_padded_sequence(decoder_inputs, decoder_inputs_lens, batch_first=True,
                                              enforce_sorted=False)

        out_packed, hidden = self.decoder(decoder_inputs, hidden)
        # unpack
        out, lens_unpack = pad_packed_sequence(out_packed, batch_first=True,
                                               padding_value=self.dst_embedding.padding_idx)
        # linear forward
        out = self.linear(out)
        return out, hidden

    # def forward(self, x: List[torch.LongTensor], y: List[torch.LongTensor] = None, max_len=20, beam_size=None):
    #     out_packed, (h, c) = self.encoder_forward(x)
    #
    #     if y is not None:
    #         y = [i.cuda() for i in y]
    #         decoder_inputs = [sent[:-1] for sent in y]
    #         decoder_outputs = [sent[1:] for sent in y]
    #
    #         decoder_inputs_lens = [len(sent) for sent in decoder_inputs]
    #         decoder_inputs = pad_sequence(decoder_inputs, batch_first=True,
    #                                       padding_value=self.dst_embedding.padding_idx)
    #         # print(decoder_inputs)
    #         decoder_inputs = self.dst_embedding(decoder_inputs)
    #         decoder_inputs = pack_padded_sequence(decoder_inputs, decoder_inputs_lens, batch_first=True,
    #                                               enforce_sorted=False)
    #
    #         decoder_outputs = pad_sequence(decoder_outputs, batch_first=True, padding_value=self.loss_ignore_idx)
    #         out_packed, (h, c) = self.decoder(decoder_inputs, (h, c))
    #         # unpack
    #         out, lens_unpack = pad_packed_sequence(out_packed, batch_first=True,
    #                                                padding_value=self.dst_embedding.padding_idx)
    #         # linear forward
    #         out = self.linear(out).transpose(2, 1)
    #         loss = self.loss(out, decoder_outputs)
    #         return loss
    #     else:
    #         # h_n of shape (num_layers * num_directions, batch, hidden_size)
    #         if beam_size is None:
    #             beam_size = beam_size
    #         res = []
    #         for batch_i in range(h.shape[1]):
    #             h_i = h[:, batch_i: batch_i + 1, :]
    #             c_i = c[:, batch_i: batch_i + 1, :]
    #             # print(batch_i)
    #             res.append(
    #                 self.forward_sent((h_i.contiguous(), c_i.contiguous()), max_len=max_len, beam_size=beam_size))
    #         return res
    #
    # def normalize_prob(self, prob):
    #     return np.log(0.5 + prob)
    #
    # def forward_one_token(self, token, states, beam_size=None):
    #     if beam_size is None:
    #         beam_size = self.beam_size
    #     input_id = torch.LongTensor([[token]]).to(self.device)
    #     output, states = self.decoder(self.dst_embedding(input_id), states)
    #     topk_output = torch.topk(self.softmax(output.squeeze(0).squeeze(0)), k=beam_size, dim=-1)
    #     topk_output_indices = topk_output.indices.tolist()  # for next token
    #     topk_output_values = topk_output.values.tolist()  # for probability of next token
    #
    #     return topk_output_indices, topk_output_values, states
    #
    # def forward_sent(self, states, max_len=50, beam_size=None):
    #     # h, c = states
    #     if beam_size is None:
    #         beam_size = self.beam_size
    #
    #     # initialize
    #     topk_output_indices, topk_output_values, states = self.forward_one_token(self.bos_idx, states, beam_size)
    #     res = []
    #     for i in range(len(topk_output_indices)):
    #         res.append([[topk_output_indices[i]], self.normalize_prob(topk_output_values[i]), states])
    #
    #     while True:
    #         candidates = []
    #         count_eos_token = 0
    #         for pos in range(len(res)):
    #             input_ids, accumulate_prob, states = res[pos]
    #             input_id = input_ids[-1]
    #             # print(len(input_ids))
    #             if input_id != self.eos_idx and len(input_ids) < max_len:
    #                 topk_output_indices, topk_output_values, new_states = self.forward_one_token(self.bos_idx, states,
    #                                                                                              beam_size)
    #                 for i in range(len(topk_output_indices)):
    #                     candidates.append([input_ids + [topk_output_indices[i]],
    #                                        (accumulate_prob * len(input_ids) +
    #                                         self.normalize_prob(topk_output_values[i])) / (len(input_ids)+1),  # normalize with len
    #                                        new_states])
    #             elif input_id == self.eos_idx:
    #                 count_eos_token += 1
    #             else:
    #                 input_ids.append(self.eos_idx)
    #         if count_eos_token == beam_size or len(candidates) == 0:
    #             break
    #         candidates.sort(key=lambda x: x[1], reverse=True)
    #         res = candidates[:beam_size]
    #
    #     return torch.LongTensor(res[0][0][:-1]).to(self.device)

# class Encoder(nn.Module):
#     def __init__(self, embedding: nn.Embedding, lstm_dim, bidirectional=True, num_layers=2):
#         super(Encoder, self).__init__()
#         self.embedding = embedding
#
#         self.lstm_dim = lstm_dim
#         self.bidirectional = bidirectional
#         self.direction = 1 if not self.bidirectional else 2
#         self.num_layers = num_layers
#         self.lstm = nn.LSTM(self.embedding.embedding_dim, self.lstm_dim, batch_first=True, bidirectional=self.bidirectional,
#                                num_layers=self.num_layers)
#
#     def forward(self, inputs, hidden=None):
#
#         lens = [len(sent) for sent in inputs]
#
#         # padding
#         inputs = pad_sequence(inputs, batch_first=True, padding_value=self.embedding.padding_idx)
#         inputs = self.embedding(inputs)  # shape: batch * max(lens) * embedding_dim
#
#         # packing
#         inputs = pack_padded_sequence(inputs, lens, batch_first=True, enforce_sorted=False)
#
#         output, (h, c) = self.lstm(inputs, hidden)
#         h = h.reshape(self.num_layers, self.direction, len(lens), -1).transpose(2, 1).reshape(self.num_layers,
#                                                                                               len(lens), -1)
#         c = c.reshape(self.num_layers, self.direction, len(lens), -1).transpose(2, 1).reshape(self.num_layers,
#                                                                                               len(lens), -1)
#
#         return output, (h, c)
#
#
# class Decoder(nn.Module):
#     def __init__(self, embedding: nn.Embedding, lstm_dim, num_layers=1, dropout=0.1):
#         super(Decoder, self).__init__()
#         self.embedding = embedding
#
#         self.lstm_dim = lstm_dim
#         self.num_layers = num_layers
#         self.network = nn.LSTM(self.embedding.embedding_dim, self.lstm_dim, batch_first=True,
#                                num_layers=self.num_layers)
#         self.linear = nn.Linear(self.lstm_dim, self.embedding.num_embeddings)
#         self.dropout = nn.Dropout(dropout)
#
#     def forward(self, inputs, hidden_states):
#         decoder_inputs = inputs
#
#         decoder_inputs_lens = [len(sent) for sent in decoder_inputs]
#         decoder_inputs = pad_sequence(decoder_inputs, batch_first=True,
#                                       padding_value=self.embedding.padding_idx)
#
#         decoder_inputs = self.embedding(decoder_inputs)
#
#         decoder_inputs = pack_padded_sequence(decoder_inputs, decoder_inputs_lens, batch_first=True,
#                                               enforce_sorted=False)
#         out_packed, hidden_states = self.network(decoder_inputs, hidden_states)
#         # unpack
#         out, lens_unpack = pad_packed_sequence(out_packed, batch_first=True,
#                                                padding_value=self.embedding.padding_idx)
#         # linear forward
#
#         return out
#
#
# class BaseSeq2SeqModel(nn.Module):
#     def __init__(self, encoder: Encoder, decoder: Decoder, bos_idx: int = 0, eos_idx: int = 2, device='cuda'):
#         super(BaseSeq2SeqModel, self).__init__()
#         self.encoder = encoder
#         self.decoder = decoder
#         self.bos_idx = bos_idx
#         self.eos_idx = eos_idx
#
#         self.softmax = nn.Softmax()
#         self.loss = nn.CrossEntropyLoss()
#         self.device = device
#
#     def forward(self, encoder_inputs, decoder_inputs):
#         encoder_outputs, hidden_states = self.encoder(encoder_inputs)
#
#         decoder_outputs = self.decoder(decoder_inputs, hidden_states)
#
#         return decoder_outputs
#
#     def forward_with_loss(self, encoder_inputs, decoder_inputs, decoder_decoder_inputss):
#         decoder_outputs = self.forward(encoder_inputs, decoder_inputs).transpose(2, 1)
#
#         return self.loss(decoder_outputs, decoder_decoder_inputss)
#
#     def predict(self, sentences, max_len=20, beam_size=5):
#         encoder_outputs, hidden_states = self.encoder(sentences)
#
#         # h_n of shape (num_layers, batch, hidden_size * num_directions)
#         result = []
#         h, c = hidden_states
#         for i in range(len(sentences)):
#             h_i = h[:, i: i + 1, :]
#             c_i = c[:, i: i + 1, :]
#             result.append(self.beam_search((h_i.contiguous(), c_i.contiguous()), max_len=max_len, beam_size=beam_size))
#         return result
#
#     def forward_one_token(self, token, states, beam_size=None):
#         if beam_size is None:
#             beam_size = self.beam_size
#         input_id = torch.LongTensor([[token]]).to(self.device)
#         output, states = self.decoder(input_id, states)
#         topk_output = torch.topk(self.softmax(output.squeeze(0).squeeze(0)), k=beam_size, dim=-1)
#         topk_output_indices = topk_output.indices.tolist()  # for next token
#         topk_output_values = topk_output.values.tolist()  # for probability of next token
#
#         return topk_output_indices, topk_output_values, states
#
#     def normalize_prob(self, prob):
#         return np.log(0.5 + prob)
#
#     def search_token(self, token, states, beam_size=5):
#         if self.device == 'cuda':
#             input_id = torch.LongTensor([[token]]).cuda()
#         else:
#             input_id = torch.LongTensor([[token]]).to(self.device)
#
#         output, states = self.decoder(input_id, states)
#         topk_output = torch.topk(self.softmax(output.squeeze(0).squeeze(0)), k=beam_size, dim=-1)
#         topk_output_indices = topk_output.indices.tolist()  # for next token
#         topk_output_values = topk_output.values.tolist()  # for probability of next token
#
#         return topk_output_indices, topk_output_values, states
#
#     def beam_search(self, hidden_states, max_len=20, beam_size=5):
#         # initialize
#         topk_output_indices, topk_output_values, hidden_states = self.forward_one_token(self.bos_idx, hidden_states,
#                                                                                         beam_size)
#         res = []
#         for i in range(len(topk_output_indices)):
#             res.append([[topk_output_indices[i]], self.normalize_prob(topk_output_values[i]), hidden_states])
#
#         while True:
#             candidates = []
#             count_eos_token = 0
#             for pos in range(len(res)):
#                 input_ids, accumulate_prob, hidden_states = res[pos]
#                 input_id = input_ids[-1]
#                 print(len(input_ids))
#                 if input_id != self.eos_idx and len(input_ids) < max_len:
#                     topk_output_indices, topk_output_values, new_hidden_states = self.search_token(self.bos_idx, hidden_states,
#                                                                                                  beam_size)
#                     for i in range(len(topk_output_indices)):
#                         candidates.append([input_ids + [topk_output_indices[i]],
#                                            (accumulate_prob + self.normalize_prob(topk_output_values[i])),
#                                            # normalize with len
#                                            new_hidden_states])
#                 elif input_id == self.eos_idx:
#                     count_eos_token += 1
#                 else:
#                     input_ids.append(self.eos_idx)
#             if count_eos_token == beam_size or len(candidates) == 0:
#                 break
#             candidates.sort(key=lambda x: x[1], reverse=True)
#             res = candidates[:beam_size]
#
#         return np.array(res[0][0])
