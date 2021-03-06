import torch
import torch.nn as nn
import torch.nn.functional as F
import random
from torch.autograd import Variable as Var
from torch.utils.data import TensorDataset, DataLoader
from utils import *
from data import *
from lf_evaluator import *
import numpy as np
from typing import List
import time

def add_models_args(parser):
    """
    Command-line arguments to the system related to your model.  Feel free to extend here.  
    """
    # Some common arguments for your convenience
    parser.add_argument('--seed', type=int, default=0, help='RNG seed (default = 0)')
    parser.add_argument('--epochs', type=int, default=10, help='num epochs to train for')
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--batch_size', type=int, default=2, help='batch size')

    # 65 is all you need for GeoQuery
    parser.add_argument('--decoder_len_limit', type=int, default=65, help='output length limit of the decoder')

    # Feel free to add other hyperparameters for your input dimension, etc. to control your network
    # 50-200 might be a good range to start with for embedding and LSTM sizes


class NearestNeighborSemanticParser(object):
    """
    Semantic parser that uses Jaccard similarity to find the most similar input example to a particular question and
    returns the associated logical form.
    """
    def __init__(self, training_data: List[Example]):
        self.training_data = training_data

    def decode(self, test_data: List[Example]) -> List[List[Derivation]]:
        """
        :param test_data: List[Example] to decode
        :return: A list of k-best lists of Derivations. A Derivation consists of the underlying Example, a probability,
        and a tokenized input string. If you're just doing one-best decoding of example ex and you
        produce output y_tok, you can just return the k-best list [Derivation(ex, 1.0, y_tok)]
        """
        test_derivs = []
        for test_ex in test_data:
            test_words = test_ex.x_tok
            best_jaccard = -1
            best_train_ex = None
            # Find the highest word overlap with the train data
            for train_ex in self.training_data:
                # Compute word overlap with Jaccard similarity
                train_words = train_ex.x_tok
                overlap = len(frozenset(train_words) & frozenset(test_words))
                jaccard = overlap/float(len(frozenset(train_words) | frozenset(test_words)))
                if jaccard > best_jaccard:
                    best_jaccard = jaccard
                    best_train_ex = train_ex
            # Note that this is a list of a single Derivation
            test_derivs.append([Derivation(test_ex, 1.0, best_train_ex.y_tok)])
        return test_derivs

class Seq2SeqSemanticParser(nn.Module):
    def __init__(self, input_indexer, output_indexer, emb_dim, hidden_size, embedding_dropout=0.2, bidirect=True):
        # We've include some args for setting up the input embedding and encoder
        # You'll need to add code for output embedding and decoder
        super(Seq2SeqSemanticParser, self).__init__()
        self.input_indexer = input_indexer
        self.output_indexer = output_indexer
        
        self.input_emb = EmbeddingLayer(emb_dim, len(input_indexer), embedding_dropout)
        self.output_emb = EmbeddingLayer(emb_dim, len(output_indexer), embedding_dropout)

        # Encoder
        self.encoder = RNNEncoder(emb_dim, hidden_size, bidirect=False)  # bidirectional ???

        # Decoder
        self.decoder = RNNAttentionDecoder(emb_dim, hidden_size, len(output_indexer))
        # self.decoder = RNNDecoder(emb_dim, hidden_size, len(output_indexer))

        self.loss_func = nn.CrossEntropyLoss()

    def forward(self, x_tensor, inp_lens_tensor, y_tensor, out_lens_tensor, batch_size):
        """
        :param x_tensor/y_tensor: either a non-batched input/output [sent len] vector of indices or a batched input/output
        [batch size x sent len]. y_tensor contains the gold sequence(s) used for training
        :param inp_lens_tensor/out_lens_tensor: either a vector of input/output length [batch size] or a single integer.
        lengths aren't needed if you don't batchify the training.
        :return: loss of the batch
        """

        #################

        embedded_input = self.input_emb(x_tensor)
        encoder_output, _, h_t = self.encoder(embedded_input, inp_lens_tensor)

        token = self.output_indexer.index_of("<SOS>")
        h, c = h_t[0], h_t[1]

        iter_loss = []

        for batch in range(batch_size):
            start = self.output_emb(torch.LongTensor([[token]]))
            (h1,c1) = (h[batch].unsqueeze(0).unsqueeze(0), c[batch].unsqueeze(0).unsqueeze(0))

            for idx in range(out_lens_tensor[batch]):
                enc_out = encoder_output[:inp_lens_tensor[batch],batch,:].unsqueeze(0)
                cell_output, _,(h1,c1) = self.decoder(start,h1,c1,(torch.tensor([1])), enc_out)

                target = y_tensor[batch][idx]
                start = self.output_emb(target.unsqueeze(0).unsqueeze(0))
                loss = self.loss_func(cell_output, y_tensor[batch][idx].unsqueeze(0).detach())
                iter_loss.append(loss)
        batch_loss = np.sum(iter_loss)

        return batch_loss


    def decode(self, test_data: List[Example]) -> List[List[Derivation]]:

        #################

        unpacked =  []

        for ex in test_data:
            entry_word = []
            x_tensor = self.input_emb(torch.LongTensor(ex.x_indexed).unsqueeze(0))
            input_len = torch.LongTensor([len(ex.x_indexed)])

            ### get encoded output and hidden state (discard context mask)
            enc_out, _, h_t = self.encoder(x_tensor, input_len)

            #### separate hidden and cell states
            h_n = h_t[0].unsqueeze(0)
            c_n = h_t[1].unsqueeze(0)

            token = self.output_indexer.index_of("<SOS>")
            end_token = self.output_indexer.index_of("<EOS>")

            prob = 0
            count = 0

            while token != end_token and count < 100:
                emb = self.output_emb(torch.LongTensor([[token]]))
                enc_output = enc_out[:len(ex.x_indexed), :].permute([1, 0, 2])
                output, _, (h_n,c_n) = self.decoder(emb, h_n, c_n, torch.LongTensor([1]), enc_output)
                prob += torch.max(F.log_softmax(output, dim=1))
                token = torch.argmax(output)

                if token.item() == end_token:
                    break

                entry_word.append(token.item())
                count += 1

            predicted = list(map(lambda x: self.output_indexer.get_object(x),entry_word))
            unpacked.append([Derivation(ex, np.exp(prob.detach()), predicted)])

        return unpacked

        #################


    def encode_input(self, x_tensor, inp_lens_tensor):
        """
        Runs the encoder (input embedding layer and encoder as two separate modules) on a tensor of inputs x_tensor with
        inp_lens_tensor lengths.
        YOU DO NOT NEED TO USE THIS FUNCTION. It's merely meant to illustrate the usage of EmbeddingLayer and RNNEncoder
        as they're given to you, as well as show what kinds of inputs/outputs you need from your encoding phase.
        :param x_tensor: [batch size, sent len] tensor of input token indices
        :param inp_lens_tensor: [batch size] vector containing the length of each sentence in the batch
        :param model_input_emb: EmbeddingLayer
        :param model_enc: RNNEncoder
        :return: the encoder outputs (per word), the encoder context mask (matrix of 1s and 0s reflecting which words
        are real and which ones are pad tokens), and the encoder final states (h and c tuple). ONLY THE ENCODER FINAL
        STATES are needed for the basic seq2seq model. enc_output_each_word is needed for attention, and
        enc_context_mask is needed to batch attention.

        E.g., calling this with x_tensor (0 is pad token):
        [[12, 25, 0],
        [1, 2, 3],
        [2, 0, 0]]
        inp_lens = [2, 3, 1]
        will return outputs with the following shape:
        enc_output_each_word = 3 x 3 x dim, enc_context_mask = [[1, 1, 0], [1, 1, 1], [1, 0, 0]],
        enc_final_states = 3 x dim
        """
        input_emb = self.input_emb.forward(x_tensor)
        (enc_output_each_word, enc_context_mask, enc_final_states) = self.encoder.forward(input_emb, inp_lens_tensor)
        enc_final_states_reshaped = (enc_final_states[0].unsqueeze(0), enc_final_states[1].unsqueeze(0))
        return (enc_output_each_word, enc_context_mask, enc_final_states_reshaped)


class EmbeddingLayer(nn.Module):
    """
    Embedding layer that has a lookup table of symbols that is [full_dict_size x input_dim]. Includes dropout.
    Works for both non-batched and batched inputs
    """
    def __init__(self, input_dim: int, full_dict_size: int, embedding_dropout_rate: float):
        """
        :param input_dim: dimensionality of the word vectors
        :param full_dict_size: number of words in the vocabulary
        :param embedding_dropout_rate: dropout rate to apply
        """
        super(EmbeddingLayer, self).__init__()
        self.dropout = nn.Dropout(embedding_dropout_rate)
        self.word_embedding = nn.Embedding(full_dict_size, input_dim)

    def forward(self, input):
        """
        :param input: either a non-batched input [sent len x voc size] or a batched input
        [batch size x sent len x voc size]
        :return: embedded form of the input words (last coordinate replaced by input_dim)
        """
        embedded_words = self.word_embedding(input)
        final_embeddings = self.dropout(embedded_words)
        return final_embeddings


class RNNEncoder(nn.Module):
    """
    One-layer RNN encoder for batched inputs -- handles multiple sentences at once. To use in non-batched mode, call it
    with a leading dimension of 1 (i.e., use batch size 1)
    """
    def __init__(self, input_emb_dim: int, hidden_size: int, bidirect: bool):
        """
        :param input_emb_dim: size of word embeddings output by embedding layer
        :param hidden_size: hidden size for the LSTM
        :param bidirect: True if bidirectional, false otherwise
        """
        super(RNNEncoder, self).__init__()
        self.bidirect = bidirect
        self.hidden_size = hidden_size
        self.reduce_h_W = nn.Linear(hidden_size * 2, hidden_size, bias=True)
        self.reduce_c_W = nn.Linear(hidden_size * 2, hidden_size, bias=True)
        self.rnn = nn.LSTM(input_emb_dim, hidden_size, num_layers=1, batch_first=True,
                               dropout=0., bidirectional=self.bidirect)

    def get_output_size(self):
        return self.hidden_size * 2 if self.bidirect else self.hidden_size

    def sent_lens_to_mask(self, lens, max_length):
        return torch.from_numpy(np.asarray([[1 if j < lens.data[i].item() else 0 for j in range(0, max_length)] for i in range(0, lens.shape[0])]))

    def forward(self, embedded_words, input_lens):
        """
        Runs the forward pass of the LSTM
        :param embedded_words: [batch size x sent len x input dim] tensor
        :param input_lens: [batch size]-length vector containing the length of each input sentence
        :return: output (each word's representation), context_mask (a mask of 0s and 1s
        reflecting where the model's output should be considered), and h_t, a *tuple* containing
        the final states h and c from the encoder for each sentence.
        Note that output is only needed for attention, and context_mask is only used for batched attention.
        """
        # Takes the embedded sentences, "packs" them into an efficient Pytorch-internal representation
        packed_embedding = nn.utils.rnn.pack_padded_sequence(embedded_words, input_lens, batch_first=True, enforce_sorted=False)
        # Runs the RNN over each sequence. Returns output at each position as well as the last vectors of the RNN
        # state for each sentence (first/last vectors for bidirectional)
        output, hn = self.rnn(packed_embedding)
        # Unpacks the Pytorch representation into normal tensors
        output, sent_lens = nn.utils.rnn.pad_packed_sequence(output)
        max_length = max(input_lens.data).item()
        context_mask = self.sent_lens_to_mask(sent_lens, max_length)

        if self.bidirect:
            h, c = hn[0], hn[1]
            # Grab the representations from forward and backward LSTMs
            h_, c_ = torch.cat((h[0], h[1]), dim=1), torch.cat((c[0], c[1]), dim=1)
            # Reduce them by multiplying by a weight matrix so that the hidden size sent to the decoder is the same
            # as the hidden size in the encoder
            new_h = self.reduce_h_W(h_)
            new_c = self.reduce_c_W(c_)
            h_t = (new_h, new_c)
        else:
            h, c = hn[0][0], hn[1][0]
            h_t = (h, c)
        return (output, context_mask, h_t)


#################


class RNNDecoder(nn.Module):
    def __init__(self, input_size: int, hidden_size: int, num_output: int):
        super(RNNDecoder, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.rnn = nn.LSTM(input_size, hidden_size, num_layers=1, batch_first=True,
                               dropout=0., bidirectional=False)
        self.W = nn.Linear(hidden_size, num_output, bias=True)

    def forward(self, word_input, h, c, input_lens, _):
        packed_emb = nn.utils.rnn.pack_padded_sequence(word_input, input_lens, batch_first=True, enforce_sorted=False)
        output, (h,c) = self.rnn(packed_emb,(h,c))
        output, sent_lens = nn.utils.rnn.pad_packed_sequence(output)
        h_t = (h, c)
        return self.W(output.squeeze(0)), [], h_t


class RNNAttentionDecoder(nn.Module):
    def __init__(self, input_size: int, hidden_size: int, num_output: int):
        super(RNNAttentionDecoder, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.rnn = nn.LSTM(input_size, hidden_size, num_layers=1, batch_first=True,
                               dropout=0., bidirectional=False)
        self.W = nn.Linear(hidden_size*2, num_output, bias=True)


    def forward(self, word_input, h, c, _, enc_outputs):

        lstm_output, (h,c) = self.rnn(word_input,(h,c))
        lstm_output = lstm_output.squeeze(0)
        enc_outputs = enc_outputs.squeeze(0)

        ratios = torch.inner(enc_outputs, lstm_output)
        # print("\nratios:", ratios.shape)

        # probability vector
        prob = F.softmax(ratios, dim=0)
        # print("\nenc_outputs:", enc_outputs.shape)
        # print("\nenc_outputs transposed):", enc_outputs.transpose(0,1).shape)
        # print("\nprob:", prob.shape)

        attention = torch.matmul(enc_outputs.transpose(0,1),prob)
        # print("\natten:", attention.shape)
        # print("\nlstm_output:", lstm_output.shape)

        concat = torch.cat([lstm_output.transpose(0,1), attention], dim=0).transpose(0,1)
        # print("\nconcat:", concat.shape)

        h_t = (h, c)

        return self.W(concat), [], h_t


#################



def make_padded_input_tensor(exs: List[Example], input_indexer: Indexer, max_len: int, reverse_input=False) -> np.ndarray:
    """
    Takes the given Examples and their input indexer and turns them into a numpy array by padding them out to max_len.
    Optionally reverses them.
    :param exs: examples to tensor-ify
    :param input_indexer: Indexer over input symbols; needed to get the index of the pad symbol
    :param max_len: max input len to use (pad/truncate to this length)
    :param reverse_input: True if we should reverse the inputs (useful if doing a unidirectional LSTM encoder)
    :return: A [num example, max_len]-size array of indices of the input tokens
    """
    if reverse_input:
        return np.array(
            [[ex.x_indexed[len(ex.x_indexed) - 1 - i] if i < len(ex.x_indexed) else input_indexer.index_of(PAD_SYMBOL)
              for i in range(0, max_len)]
             for ex in exs])
    else:
        return np.array([[ex.x_indexed[i] if i < len(ex.x_indexed) else input_indexer.index_of(PAD_SYMBOL)
                          for i in range(0, max_len)]
                         for ex in exs])


def make_padded_output_tensor(exs, output_indexer, max_len):
    """
    Similar to make_padded_input_tensor, but does it on the outputs without the option to reverse input
    :param exs:
    :param output_indexer:
    :param max_len:
    :return: A [num example, max_len]-size array of indices of the output tokens
    """
    return np.array([[ex.y_indexed[i] if i < len(ex.y_indexed) else output_indexer.index_of(PAD_SYMBOL) for i in range(0, max_len)] for ex in exs])


def train_model_encdec(train_data: List[Example], dev_data: List[Example], input_indexer, output_indexer, args) -> Seq2SeqSemanticParser:
    """
    Function to train the encoder-decoder model on the given data.
    :param train_data:
    :param dev_data: Development set in case you wish to evaluate during training
    :param input_indexer: Indexer of input symbols
    :param output_indexer: Indexer of output symbols
    :param args:
    :return:
    """
    # Create indexed input
    input_max_len = np.max(np.asarray([len(ex.x_indexed) for ex in train_data]))

    # [sample size, tokenized/index length] --> shape = (480, 19)
    all_train_input_data = make_padded_input_tensor(train_data, input_indexer, input_max_len, reverse_input=False)
    all_test_input_data = make_padded_input_tensor(dev_data, input_indexer, input_max_len, reverse_input=False)

    output_max_len = np.max(np.asarray([len(ex.y_indexed) for ex in train_data]))

    # [sample size, tokenized/index length] --> shape = (480, 65)
    all_train_output_data = make_padded_output_tensor(train_data, output_indexer, output_max_len)
    all_test_output_data = make_padded_output_tensor(dev_data, output_indexer, output_max_len)

    if args.print_dataset:
        print("Train length: %i" % input_max_len)
        print("Train output length: %i" % np.max(np.asarray([len(ex.y_indexed) for ex in train_data])))
        print("Train matrix: %s; shape = %s" % (all_train_input_data, all_train_input_data.shape))

    # First create a model. Then loop over epochs, loop over examples, and given some indexed words
    # call your seq-to-seq model, accumulate losses, update parameters

    # hardcode these parameters before submission
    # 2:300:400:lr:20 -> .807
    # 2:300:256:lr:20 -> .788
    # 2:300:256:lr:30 -> .809 .795/.395 (15sec/epoch)
    # 2:300:256:lr:25 -> .821/399 (15sec/epoch)
    # 4:300:256:lr:30 -> .791 .784/.398  (15sec/epoch)
    # 3:300:256:lr:30 -> .814 .777/.398 (15sec/epoch)


    batch_size = 2      # default: 2
    emb_dim = 300
    hidden_size = 256
    lr = args.lr        # default: 1e-3
    epochs = 20         # default: 10


    model = Seq2SeqSemanticParser(input_indexer, output_indexer, emb_dim, hidden_size)

    parameters = [{'params':model.encoder.parameters()},
                  {'params':model.output_emb.parameters()},
                  {'params':model.decoder.parameters()},
                  {'params':model.input_emb.parameters()}]

    optimizer = torch.optim.Adam(parameters, lr=lr)

    input_len = torch.LongTensor(np.asarray([len(ex.x_indexed) for ex in train_data]))
    output_len = torch.LongTensor(np.asarray([len(ex.y_indexed) for ex in train_data]))
    all_train_input_data = torch.LongTensor(all_train_input_data)
    all_train_output_data = torch.LongTensor(all_train_output_data)

    dataset = TensorDataset(input_len, all_train_input_data, output_len, all_train_output_data)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=4)

    for epoch in range(epochs):
        timer = time.time()
        epoch_loss = []
        model.input_emb.train()
        model.output_emb.train()
        model.encoder.train()
        model.decoder.train()

        for batch in dataloader:
            optimizer.zero_grad()
            x_tensor, inp_lens_tensor = batch[1], batch[0]
            y_tensor, out_lens_tensor = batch[3], batch[2]

            # accumulate loss terms
            batch_loss  = model(x_tensor, inp_lens_tensor, y_tensor, out_lens_tensor, batch_size)
            epoch_loss.append(batch_loss)

            batch_loss.backward()
            optimizer.step()

        print(f"\nEpoch {epoch}:")
        print(f"{np.sum(epoch_loss)/len(epoch_loss)}")
        print("Time:",time.time()-timer)
    return model


