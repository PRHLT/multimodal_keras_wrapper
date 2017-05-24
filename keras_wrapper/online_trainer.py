# -*- coding: utf-8 -*-
import logging
import numpy as np
from keras_wrapper.extra.read_write import list2file
from keras_wrapper.utils import indices_2_one_hot, decode_predictions_beam_search
import copy


class OnlineTrainer:
    def __init__(self, models, dataset, sampler, params_prediction, params_training, verbose=0):
        """

        :param models:
        :param dataset:
        :param params_prediction:
        """
        self.models = models
        self.dataset = dataset
        self.sampler = sampler
        self.verbose = verbose
        self.params_prediction = self.checkParameters(params_prediction)
        self.params_training = self.checkParameters(params_training, params_training=True)
        self.index2word_y = self.dataset.vocabulary[params_prediction['dataset_outputs'][0]]['idx2words']
        self.mapping = None if self.dataset.mapping == dict() else self.dataset.mapping
        if self.params_prediction['n_best_optimizer']:
            from pycocoevalcap.sentence_bleu.sentence_bleu import SentenceBleuScorer
            self.sentence_scorer = SentenceBleuScorer('')

    def sample_and_train_online(self, X, Y, src_words=None, trg_words=None):
        x = X[0]
        state_below_y = X[1]
        y = Y[0]

        # 1. Generate a sample with the current model
        if self.params_prediction['n_best_optimizer']:
            self.sentence_scorer.set_reference(trg_words[0].split())
            [trans_indices, costs, alphas], n_best = self.sampler.sample_beam_search(x[0])

        else:
            trans_indices, costs, alphas = self.sampler.sample_beam_search(x[0])
        state_below_h = np.asarray([np.append(self.dataset.extra_words['<null>'], trans_indices[:-1])])

        if self.params_training.get('use_custom_loss', False):
            hyp = np.array([indices_2_one_hot(trans_indices, self.dataset.vocabulary_len["target_text"])])

        if self.params_prediction['pos_unk']:
            alphas = [alphas]
            sources = [x] if not src_words else src_words
            heuristic = self.params_prediction['heuristic']
        else:
            alphas = None
            heuristic = None
            sources = None

        if self.params_prediction['store_hypotheses'] is not None:
            hypothesis = decode_predictions_beam_search([trans_indices],
                                                        self.index2word_y,
                                                        alphas=alphas,
                                                        x_text=sources,
                                                        heuristic=heuristic,
                                                        mapping=self.mapping,
                                                        pad_sequences=True,
                                                        verbose=0)[0]
            # Apply detokenization function if needed
            if self.params_prediction.get('apply_detokenization', False):
                hypothesis_to_write = self.params_prediction['detokenize_f'](hypothesis)
            else:
                hypothesis_to_write = hypothesis
            list2file(self.params_prediction['store_hypotheses'], [hypothesis_to_write + '\n'], permission='a')
            if self.verbose > 1:
                logging.info('Hypothesis: %s' % str(hypothesis_to_write))

        if self.params_prediction['n_best_optimizer']:
            for n_best_preds, n_best_scores, n_best_alphas in n_best:
                n_best_sample_score = []
                print ""
                print ""
                print ""
                print "Reference: ", trg_words[0]
                n_best_predictions = []
                for i, (n_best_pred, n_best_score, n_best_alpha) in enumerate(zip(n_best_preds,
                                                                                n_best_scores,
                                                                                n_best_alphas)):
                    pred = decode_predictions_beam_search([n_best_pred],
                                                          self.index2word_y,
                                                          alphas=n_best_alpha,
                                                          x_text=sources,
                                                          heuristic=heuristic,
                                                          mapping=self.mapping,
                                                          verbose=0)
                    # Apply detokenization function if needed
                    if self.params_prediction.get('apply_detokenization', False):
                        pred = map(self.params_prediction['detokenize_f'], pred)
                    if self.sentence_scorer is not None:
                        score = self.sentence_scorer.score(pred[0].split())
                    else:
                        score = n_best_score
                    n_best_sample_score.append([i, pred, score])
                n_best_predictions.append(n_best_sample_score)
            print n_best_predictions
            for model in self.models:
                weights = model.trainable_weights
                weights.sort(key=lambda x: x.name if x.name else x.auto_name)
                model.optimizer.set_weights(weights)

                for k in range(1):
                    B = [nbest[2] for nbest in n_best_predictions[0]]
                    p = np.argsort([nbest[2] for nbest in n_best_predictions[0]])
                    print "p", p
                    for i, hypothesis, score in n_best_predictions[0]:
                        print i,"-", hypothesis, "-", score
                    N = len(n_best_predictions[0])
                    for i in range(N):
                        for j in range(0, N):
                            if B[i] > B[p[j]]:
                                print "Update:"
                                print "\t i:", i
                                print "\t j:", j
                                print "\t ", n_best_predictions[0][i], "should be better than", n_best_predictions[0][p[j]]

                    train_inputs = [x, state_below_y, state_below_h] + [y, hyp]

                    loss_val = model.evaluate(train_inputs,
                                              np.zeros((y.shape[0], 1), dtype='float32'),
                                              batch_size=1, verbose=0)
                    loss = 1.0 if loss_val > 0 else 0.0
                    model.optimizer.loss_value.set_value(loss)
                    model.fit(train_inputs,
                              np.zeros((y.shape[0], 1), dtype='float32'),
                              batch_size=min(self.params_training['batch_size'], len(x)),
                              nb_epoch=self.params_training['n_epochs'],
                              verbose=self.params_training['verbose'],
                              callbacks=[],
                              validation_data=None,
                              validation_split=self.params_training.get('val_split', 0.),
                              shuffle=self.params_training['shuffle'],
                              class_weight=None,
                              sample_weight=None,
                              initial_epoch=0)



        else:
            # 2. Post-edit this sample in order to match the reference --> Use y
            # 3. Update net parameters with the corrected samples
            for model in self.models:
                if self.params_training.get('use_custom_loss', False):
                    weights = model.trainable_weights
                    weights.sort(key=lambda x: x.name if x.name else x.auto_name)
                    model.optimizer.set_weights(weights)
                    for k in range(1):
                        train_inputs = [x, state_below_y, state_below_h] + [y, hyp]

                        loss_val = model.evaluate(train_inputs,
                                                  np.zeros((y.shape[0], 1), dtype='float32'),
                                                  batch_size=1, verbose=0)
                        loss = 1.0 if loss_val > 0 else 0.0
                        model.optimizer.loss_value.set_value(loss)
                        model.fit(train_inputs,
                                  np.zeros((y.shape[0], 1), dtype='float32'),
                                  batch_size=min(self.params_training['batch_size'], len(x)),
                                  nb_epoch=self.params_training['n_epochs'],
                                  verbose=self.params_training['verbose'],
                                  callbacks=[],
                                  validation_data=None,
                                  validation_split=self.params_training.get('val_split', 0.),
                                  shuffle=self.params_training['shuffle'],
                                  class_weight=None,
                                  sample_weight=None,
                                  initial_epoch=0)
                        """
                        # Only for debugging
                        model.evaluate(train_inputs,
                                       np.zeros((y.shape[0], 1), dtype='float32'),
                                       batch_size=1, verbose=0)
                        """
                else:
                    params = copy.copy(self.params_training)
                    del params['use_custom_loss']
                    del params['custom_loss']
                    model.trainNetFromSamples([x, state_below_y], y, params)

    def checkParameters(self, input_params, params_training=False):
        """
        Validates a set of input parameters and uses the default ones if not specified.
        :param input_params: Input parameters to validate
        :return:
        """

        default_params_prediction = {'batch_size': 50,
                                     'n_parallel_loaders': 8,
                                     'beam_size': 5,
                                     'normalize': False,
                                     'mean_substraction': True,
                                     'predict_on_sets': ['val'],
                                     'maxlen': 20,
                                     'n_samples': -1,
                                     'model_inputs': ['source_text', 'state_below'],
                                     'model_outputs': ['target_text'],
                                     'dataset_inputs': ['source_text', 'state_below'],
                                     'dataset_outputs': ['target_text'],
                                     'alpha_factor': 1.0,
                                     'sampling_type': 'max_likelihood',
                                     'words_so_far': False,
                                     'optimized_search': False,
                                     'state_below_index': -1,
                                     'output_text_index': 0,
                                     'store_hypotheses': None,
                                     'pos_unk': False,
                                     'heuristic': 0,
                                     'mapping': None,
                                     'apply_detokenization': False,
                                     'normalize_probs': False,
                                     'detokenize_f': 'detokenize_none',
                                     'n_best_optimizer': False
                                     }
        default_params_training = {'batch_size': 1,
                                   'use_custom_loss': False,
                                   'custom_loss': False,
                                   'n_parallel_loaders': 8,
                                   'n_epochs': 1,
                                   'shuffle': False,
                                   'homogeneous_batches': False,
                                   'lr_decay': None,
                                   'lr_gamma': None,
                                   'epochs_for_save': 500,
                                   'verbose': 0,
                                   'eval_on_sets': [],
                                   'extra_callbacks': None,
                                   'reload_epoch': 0,
                                   'epoch_offset': 0,
                                   'data_augmentation': False,
                                   'patience': 0,
                                   'metric_check': None,
                                   'eval_on_epochs': True,
                                   'each_n_epochs': 1,
                                   'start_eval_on_epoch': 0
                                   }
        default_params = default_params_training if params_training else default_params_prediction
        valid_params = [key for key in default_params]
        params = dict()

        # Check input parameters' validity
        for key, val in input_params.iteritems():
            if key in valid_params:
                params[key] = val
            else:
                logging.warn("Parameter '" + key + "' is not a valid parameter.")

        # Use default parameters if not provided
        for key, default_val in default_params.iteritems():
            if key not in params:
                params[key] = default_val

        return params