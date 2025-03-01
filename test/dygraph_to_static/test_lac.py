# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import math
import os
import tempfile
import time
import unittest

import numpy as np

os.environ["CUDA_VISIBLE_DEVICES"] = "2"

import paddle
from paddle import _legacy_C_ops, fluid
from paddle.fluid.dygraph import to_variable
from paddle.framework import in_dynamic_mode
from paddle.jit.api import to_static
from paddle.jit.translated_layer import INFER_MODEL_SUFFIX, INFER_PARAMS_SUFFIX

SEED = 2020

# Add InputSpec to make unittest run faster.
input_specs = [
    paddle.static.InputSpec([None, None], 'int64'),
    paddle.static.InputSpec([None, None], 'int64'),
    paddle.static.InputSpec([None], 'int64'),
]


class DynamicGRU(paddle.nn.Layer):
    def __init__(
        self,
        size,
        h_0=None,
        param_attr=None,
        bias_attr=None,
        is_reverse=False,
        gate_activation='sigmoid',
        candidate_activation='tanh',
        origin_mode=False,
        init_size=None,
    ):
        super().__init__()

        self.gru_unit = paddle.nn.GRUCell(
            size * 3,
            size,
        )

        self.size = size
        self.h_0 = h_0
        self.is_reverse = is_reverse

    def forward(self, inputs):
        # Use `to_variable` to create a copy of global h_0 created not in `DynamicGRU`,
        # to avoid modify it because `h_0` is both used in other `DynamicGRU`.
        hidden = to_variable(self.h_0)
        hidden.stop_gradient = True

        res = []
        for i in range(inputs.shape[1]):
            if self.is_reverse:
                j = paddle.shape(inputs)[1] - 1 - i
            else:
                j = i

            # input_ = inputs[:, j:j+1, :]  # original code
            input_ = paddle.slice(inputs, axes=[1], starts=[j], ends=[j + 1])
            input_ = paddle.reshape(input_, [-1, input_.shape[2]])
            hidden, reset, gate = self.gru_unit(input_, hidden)
            hidden_ = paddle.reshape(hidden, [-1, 1, hidden.shape[1]])
            res.append(hidden_)

        if self.is_reverse:
            res = res[::-1]
        res = paddle.concat(res, axis=1)
        return res


class BiGRU(paddle.nn.Layer):
    def __init__(self, input_dim, grnn_hidden_dim, init_bound, h_0=None):
        super().__init__()

        self.pre_gru = paddle.nn.Linear(
            in_features=input_dim,
            out_features=grnn_hidden_dim * 3,
            weight_attr=fluid.ParamAttr(
                initializer=paddle.nn.initializer.Uniform(
                    low=-init_bound, high=init_bound
                ),
                regularizer=paddle.regularizer.L2Decay(coeff=1e-4),
            ),
        )

        self.gru = DynamicGRU(
            size=grnn_hidden_dim,
            h_0=h_0,
            param_attr=fluid.ParamAttr(
                initializer=paddle.nn.initializer.Uniform(
                    low=-init_bound, high=init_bound
                ),
                regularizer=paddle.regularizer.L2Decay(coeff=1e-4),
            ),
        )

        self.pre_gru_r = paddle.nn.Linear(
            in_features=input_dim,
            out_features=grnn_hidden_dim * 3,
            weight_attr=fluid.ParamAttr(
                initializer=paddle.nn.initializer.Uniform(
                    low=-init_bound, high=init_bound
                ),
                regularizer=paddle.regularizer.L2Decay(coeff=1e-4),
            ),
        )

        self.gru_r = DynamicGRU(
            size=grnn_hidden_dim,
            is_reverse=True,
            h_0=h_0,
            param_attr=fluid.ParamAttr(
                initializer=paddle.nn.initializer.Uniform(
                    low=-init_bound, high=init_bound
                ),
                regularizer=paddle.regularizer.L2Decay(coeff=1e-4),
            ),
        )

    def forward(self, input_feature):
        res_pre_gru = self.pre_gru(input_feature)
        res_gru = self.gru(res_pre_gru)

        res_pre_gru_r = self.pre_gru_r(input_feature)
        res_gru_r = self.gru_r(res_pre_gru_r)

        bi_merge = paddle.concat([res_gru, res_gru_r], axis=-1)
        return bi_merge


class LinearChainCRF(paddle.nn.Layer):
    def __init__(self, param_attr, size=None, is_test=False, dtype='float32'):
        super().__init__()

        self._param_attr = param_attr
        self._dtype = dtype
        self._size = size
        self._is_test = is_test
        self._transition = self.create_parameter(
            attr=self._param_attr,
            shape=[self._size + 2, self._size],
            dtype=self._dtype,
        )

    @property
    def weight(self):
        return self._transition

    @weight.setter
    def weight(self, value):
        self._transition = value

    def forward(self, input, label, length=None):
        if in_dynamic_mode():
            _, _, _, log_likelihood = _legacy_C_ops.linear_chain_crf(
                input, self._transition, label, length, "is_test", self._is_test
            )
            return log_likelihood

        alpha = self._helper.create_variable_for_type_inference(
            dtype=self._dtype
        )
        emission_exps = self._helper.create_variable_for_type_inference(
            dtype=self._dtype
        )
        transition_exps = self._helper.create_variable_for_type_inference(
            dtype=self._dtype
        )
        log_likelihood = self._helper.create_variable_for_type_inference(
            dtype=self._dtype
        )
        this_inputs = {
            "Emission": [input],
            "Transition": self._transition,
            "Label": [label],
        }
        if length is not None:
            this_inputs['Length'] = [length]
        self._helper.append_op(
            type='linear_chain_crf',
            inputs=this_inputs,
            outputs={
                "Alpha": [alpha],
                "EmissionExps": [emission_exps],
                "TransitionExps": transition_exps,
                "LogLikelihood": log_likelihood,
            },
            attrs={
                "is_test": self._is_test,
            },
        )
        return log_likelihood


class CRFDecoding(paddle.nn.Layer):
    def __init__(self, param_attr, size=None, is_test=False, dtype='float32'):
        super().__init__()

        self._dtype = dtype
        self._size = size
        self._is_test = is_test
        self._param_attr = param_attr
        self._transition = self.create_parameter(
            attr=self._param_attr,
            shape=[self._size + 2, self._size],
            dtype=self._dtype,
        )

    @property
    def weight(self):
        return self._transition

    @weight.setter
    def weight(self, value):
        self._transition = value

    def forward(self, input, label=None, length=None):
        if in_dynamic_mode():
            return _legacy_C_ops.crf_decoding(
                input, self._transition, label, length, "is_test", self._is_test
            )

        viterbi_path = self._helper.create_variable_for_type_inference(
            dtype=self._dtype
        )
        this_inputs = {
            "Emission": [input],
            "Transition": self._transition,
            "Label": label,
        }
        if length is not None:
            this_inputs['Length'] = [length]
        self._helper.append_op(
            type='crf_decoding',
            inputs=this_inputs,
            outputs={"ViterbiPath": [viterbi_path]},
            attrs={
                "is_test": self._is_test,
            },
        )
        return viterbi_path


class ChunkEval(paddle.nn.Layer):
    def __init__(
        self, num_chunk_types, chunk_scheme, excluded_chunk_types=None
    ):
        super().__init__()
        self.num_chunk_types = num_chunk_types
        self.chunk_scheme = chunk_scheme
        self.excluded_chunk_types = excluded_chunk_types

    def forward(self, input, label, seq_length=None):
        if in_dynamic_mode():
            return _legacy_C_ops.chunk_eval(
                input,
                label,
                seq_length,
                "num_chunk_types",
                self.num_chunk_types,
                "chunk_scheme",
                self.chunk_scheme,
                "excluded_chunk_types",
                self.excluded_chunk_types or [],
            )

        precision = self._helper.create_variable_for_type_inference(
            dtype="float32"
        )
        recall = self._helper.create_variable_for_type_inference(
            dtype="float32"
        )
        f1_score = self._helper.create_variable_for_type_inference(
            dtype="float32"
        )
        num_infer_chunks = self._helper.create_variable_for_type_inference(
            dtype="int64"
        )
        num_label_chunks = self._helper.create_variable_for_type_inference(
            dtype="int64"
        )
        num_correct_chunks = self._helper.create_variable_for_type_inference(
            dtype="int64"
        )

        this_input = {"Inference": [input], "Label": [label]}
        if seq_length is not None:
            this_input["SeqLength"] = [seq_length]

        self._helper.append_op(
            type='chunk_eval',
            inputs=this_input,
            outputs={
                "Precision": [precision],
                "Recall": [recall],
                "F1-Score": [f1_score],
                "NumInferChunks": [num_infer_chunks],
                "NumLabelChunks": [num_label_chunks],
                "NumCorrectChunks": [num_correct_chunks],
            },
            attrs={
                "num_chunk_types": self.num_chunk_types,
                "chunk_scheme": self.chunk_scheme,
                "excluded_chunk_types": self.excluded_chunk_types or [],
            },
        )
        return (
            precision,
            recall,
            f1_score,
            num_infer_chunks,
            num_label_chunks,
            num_correct_chunks,
        )


class LexNet(paddle.nn.Layer):
    def __init__(self, args, length=None):
        super().__init__()
        """
        define the lexical analysis network structure
        word: stores the input of the model
        for_infer: a boolean value, indicating if the model to be created is for training or predicting.

        return:
            for infer: return the prediction
            otherwise: return the prediction
        """
        self.word_emb_dim = args.word_emb_dim
        self.vocab_size = args.vocab_size
        self.num_labels = args.num_labels
        self.grnn_hidden_dim = args.grnn_hidden_dim
        self.emb_lr = (
            args.emb_learning_rate if 'emb_learning_rate' in dir(args) else 1.0
        )
        self.crf_lr = (
            args.emb_learning_rate if 'crf_learning_rate' in dir(args) else 1.0
        )
        self.bigru_num = args.bigru_num
        self.init_bound = 0.1

        self.word_embedding = paddle.nn.Embedding(
            self.vocab_size,
            self.word_emb_dim,
            weight_attr=fluid.ParamAttr(
                learning_rate=self.emb_lr,
                name="word_emb",
                initializer=paddle.nn.initializer.Uniform(
                    low=-self.init_bound, high=self.init_bound
                ),
            ),
        )

        h_0 = np.zeros((args.batch_size, self.grnn_hidden_dim), dtype="float32")
        h_0 = to_variable(h_0)

        self.bigru_units = []
        for i in range(self.bigru_num):
            if i == 0:
                self.bigru_units.append(
                    self.add_sublayer(
                        "bigru_units%d" % i,
                        BiGRU(
                            self.grnn_hidden_dim,
                            self.grnn_hidden_dim,
                            self.init_bound,
                            h_0=h_0,
                        ),
                    )
                )
            else:
                self.bigru_units.append(
                    self.add_sublayer(
                        "bigru_units%d" % i,
                        BiGRU(
                            self.grnn_hidden_dim * 2,
                            self.grnn_hidden_dim,
                            self.init_bound,
                            h_0=h_0,
                        ),
                    )
                )

        self.fc = paddle.nn.Linear(
            in_features=self.grnn_hidden_dim * 2,
            out_features=self.num_labels,
            weight_attr=fluid.ParamAttr(
                initializer=paddle.nn.initializer.Uniform(
                    low=-self.init_bound, high=self.init_bound
                ),
                regularizer=paddle.regularizer.L2Decay(coeff=1e-4),
            ),
        )

        self.linear_chain_crf = LinearChainCRF(
            param_attr=fluid.ParamAttr(
                name='linear_chain_crfw', learning_rate=self.crf_lr
            ),
            size=self.num_labels,
        )

        self.crf_decoding = CRFDecoding(
            param_attr=fluid.ParamAttr(name='crfw', learning_rate=self.crf_lr),
            size=self.num_labels,
        )
        # share weight
        self.crf_decoding.weight = self.linear_chain_crf.weight

    @to_static(input_spec=input_specs)
    def forward(self, word, target, length=None):
        """
        Configure the network
        """
        word_embed = self.word_embedding(word)
        input_feature = word_embed

        for i in range(self.bigru_num):
            bigru_output = self.bigru_units[i](input_feature)
            input_feature = bigru_output

        emission = self.fc(bigru_output)

        crf_cost = self.linear_chain_crf(
            input=emission, label=target, length=length
        )
        avg_cost = paddle.mean(x=crf_cost)
        crf_decode = self.crf_decoding(input=emission, length=length)
        return avg_cost, crf_decode


class Args:
    epoch = 1
    batch_size = 4
    vocab_size = 100
    num_labels = 10
    word_emb_dim = 128
    grnn_hidden_dim = 128
    base_learning_rate = 0.01
    bigru_num = 2
    print_steps = 1


def get_random_input_data(batch_size, vocab_size, num_labels, max_seq_len=64):
    local_random = np.random.RandomState(SEED)
    padding_id = np.int64(0)
    iter_num = 5

    def __reader__():
        batch, init_lens = [], []
        for i in range(iter_num * batch_size):
            cur_len = local_random.randint(3, max_seq_len)
            word_ids = (
                local_random.randint(0, vocab_size, [cur_len])
                .astype('int64')
                .tolist()
            )
            label_ids = (
                local_random.randint(0, num_labels, [cur_len])
                .astype('int64')
                .tolist()
            )
            batch.append((word_ids, label_ids))
            init_lens.append(cur_len)
            if len(batch) == batch_size:
                batch_max_len = min(max(init_lens), max_seq_len)
                new_batch = []
                for words_len, (word_ids, label_ids) in zip(init_lens, batch):
                    word_ids = word_ids[0:batch_max_len]
                    words_len = np.int64(len(word_ids))
                    word_ids += [
                        padding_id for _ in range(batch_max_len - words_len)
                    ]
                    label_ids = label_ids[0:batch_max_len]
                    label_ids += [
                        padding_id for _ in range(batch_max_len - words_len)
                    ]
                    assert len(word_ids) == len(label_ids)
                    new_batch.append((word_ids, label_ids, words_len))
                yield new_batch
                batch, init_lens = [], []

    return __reader__


def create_dataloader(reader, place):
    data_loader = fluid.io.DataLoader.from_generator(
        capacity=16, use_double_buffer=True, iterable=True
    )

    data_loader.set_sample_list_generator(reader, places=place)

    return data_loader


class TestLACModel(unittest.TestCase):
    def setUp(self):
        self.args = Args()
        self.place = (
            fluid.CUDAPlace(0)
            if fluid.is_compiled_with_cuda()
            else fluid.CPUPlace()
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.model_save_dir = os.path.join(self.temp_dir.name, 'inference')
        self.model_save_prefix = os.path.join(self.model_save_dir, 'lac')
        self.model_filename = "lac" + INFER_MODEL_SUFFIX
        self.params_filename = "lac" + INFER_PARAMS_SUFFIX
        self.dy_param_path = os.path.join(self.temp_dir.name, 'lac_dy_param')

    def train(self, args, to_static):
        paddle.jit.enable_to_static(to_static)
        place = (
            fluid.CUDAPlace(0)
            if fluid.is_compiled_with_cuda()
            else fluid.CPUPlace()
        )
        with fluid.dygraph.guard(place):
            paddle.seed(SEED)
            paddle.framework.random._manual_program_seed(SEED)

            reader = get_random_input_data(
                args.batch_size, args.vocab_size, args.num_labels
            )
            train_loader = create_dataloader(reader, place)

            model = LexNet(args)
            optimizer = paddle.optimizer.Adam(
                learning_rate=args.base_learning_rate,
                parameters=model.parameters(),
            )
            chunk_eval = ChunkEval(
                int(math.ceil((args.num_labels - 1) / 2.0)), "IOB"
            )

            step = 0

            loss_data = []
            for epoch_id in range(args.epoch):
                for batch in train_loader():
                    words, targets, length = batch
                    start_time = time.time()
                    avg_cost, crf_decode = model(words, targets, length)
                    loss_data.append(float(avg_cost))

                    # backward and optimization
                    avg_cost.backward()
                    optimizer.minimize(avg_cost)
                    model.clear_gradients()
                    end_time = time.time()

                    if step % args.print_steps == 0:
                        (
                            precision,
                            recall,
                            f1_score,
                            num_infer_chunks,
                            num_label_chunks,
                            num_correct_chunks,
                        ) = chunk_eval(
                            input=crf_decode, label=targets, seq_length=length
                        )
                        outputs = [avg_cost, precision, recall, f1_score]
                        avg_cost, precision, recall, f1_score = (
                            np.mean(x.numpy()) for x in outputs
                        )

                        print(
                            "[train] step = %d, loss = %f, P: %f, R: %f, F1: %f, elapsed time %f"
                            % (
                                step,
                                avg_cost,
                                precision,
                                recall,
                                f1_score,
                                end_time - start_time,
                            )
                        )

                    step += 1
            # save inference model
            if to_static:
                paddle.jit.save(
                    layer=model,
                    path=self.model_save_prefix,
                    input_spec=[input_specs[0], input_specs[-1]],
                    output_spec=[crf_decode],
                )
            else:
                paddle.save(
                    model.state_dict(), self.dy_param_path + '.pdparams'
                )

            return np.array(loss_data)

    def test_train(self):
        st_out = self.train(self.args, to_static=True)
        dy_out = self.train(self.args, to_static=False)
        np.testing.assert_allclose(
            dy_out,
            st_out,
            rtol=1e-05,
            err_msg='dygraph output:\n{},\nstatic output:\n {}.'.format(
                dy_out, st_out
            ),
        )
        # Prediction needs trained models, so put `test_predict` at last of `test_train`
        # self.verify_predict()

    def verify_predict(self):
        reader = get_random_input_data(
            self.args.batch_size, self.args.vocab_size, self.args.num_labels
        )
        for batch in reader():
            batch = [np.vstack(var) for var in zip(*batch)]
            dy_pre = self.predict_dygraph(batch)
            st_pre = self.predict_static(batch)
            dy_jit_pre = self.predict_dygraph_jit(batch)
            np.testing.assert_allclose(dy_pre, st_pre, rtol=1e-05)
            np.testing.assert_allclose(dy_jit_pre, st_pre, rtol=1e-05)

    def predict_dygraph(self, batch):
        words, targets, length = batch
        paddle.jit.enable_to_static(False)
        with fluid.dygraph.guard(self.place):
            model = LexNet(self.args)
            # load dygraph trained parameters
            model_dict = paddle.load(self.dy_param_path + ".pdparams")
            model.set_dict(model_dict)
            model.eval()

            _, pred_res = model(
                to_variable(words), to_variable(targets), to_variable(length)
            )

            return pred_res.numpy()

    def predict_static(self, batch):
        """
        LAC model contains h_0 created in `__init__` that is necessary for inferring.
        Load inference model to test it's ok for prediction.
        """
        paddle.enable_static()
        exe = fluid.Executor(self.place)
        # load inference model
        [
            inference_program,
            feed_target_names,
            fetch_targets,
        ] = paddle.static.io.load_inference_model(
            self.model_save_dir,
            executor=exe,
            model_filename=self.model_filename,
            params_filename=self.params_filename,
        )

        words, targets, length = batch
        pred_res = exe.run(
            inference_program,
            feed={feed_target_names[0]: words, feed_target_names[1]: length},
            fetch_list=fetch_targets,
        )
        return pred_res[0]

    def predict_dygraph_jit(self, batch):
        words, targets, length = batch
        with fluid.dygraph.guard(self.place):
            model = paddle.jit.load(self.model_save_prefix)
            model.eval()

            pred_res = model(to_variable(words), to_variable(length))

            return pred_res.numpy()


if __name__ == "__main__":
    unittest.main()
