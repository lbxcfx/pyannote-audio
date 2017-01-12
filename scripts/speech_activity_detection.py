#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2016-2017 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr

"""
Speech activity detection

Usage:
  speech_activity_detection train [--database=<db.yml> --subset=<subset>] <experiment_dir> <database.task.protocol>
  speech_activity_detection validation [--database=<db.yml> --subset=<subset>] <train_dir> <database.task.protocol>
  speech_activity_detection tune  [--database=<db.yml> --subset=<subset>] <train_dir> <database.task.protocol>
  speech_activity_detection apply [--database=<db.yml> --subset=<subset> --recall=<beta>] <tune_dir> <database.task.protocol>
  speech_activity_detection -h | --help
  speech_activity_detection --version

Options:
  <experiment_dir>           Set experiment root directory. This script expects
                             a configuration file called "config.yml" to live
                             in this directory. See "Configuration file"
                             section below for more details.
  <database.task.protocol>   Set evaluation protocol (e.g. "Etape.SpeakerDiarization.TV")
  <train_dir>                Set path to the directory containing pre-trained
                             models (i.e. the output of "train" mode).
  <tune_dir>                 Set path to the directory containing optimal
                             hyper-parameters (i.e. the output of "tune" mode).
  --database=<db.yml>        Path to database configuration file.
                             [default: ~/.pyannote/db.yml]
  --subset=<subset>          Set subset (train|developement|test).
                             In "train" mode, default subset is "train".
                             In "tune" mode, default subset is "development".
                             In "apply" mode, default subset is "test".
  --recall=<beta>            Set importance of recall with respect to precision.
                             [default: 1.0]
                             Use higher values if you want to improve recall.
  -h --help                  Show this screen.
  --version                  Show version.


Database configuration file:
    The database configuration provides details as to where actual files are
    stored. See `pyannote.audio.util.FileFinder` docstring for more information
    on the expected format.

Configuration file:
    The configuration of each experiment is described in a file called
    <experiment_dir>/config.yml, that describes the architecture of the neural
    network used for sequence labeling (0 vs. 1, non-speech vs. speech), the
    feature extraction process (e.g. MFCCs) and the sequence generator used for
    both training and testing.

    ................... <experiment_dir>/config.yml ...................
    feature_extraction:
       name: YaafeMFCC
       params:
          e: False                   # this experiments relies
          De: True                   # on 11 MFCC coefficients
          DDe: True                  # with 1st and 2nd derivatives
          D: True                    # without energy, but with
          DD: True                   # energy derivatives

    architecture:
       name: StackedLSTM
       params:                       # this experiments relies
         n_classes: 2                # on one LSTM layer (16 outputs)
         lstm: [16]                  # and one dense layer.
         dense: [16]                 # LSTM is bidirectional
         bidirectional: True

    sequences:
       duration: 3.2                 # this experiments relies
       step: 0.8                     # on sliding windows of 3.2s
                                     # with a step of 0.8s
    ...................................................................

"train" mode:
    First, one should train the raw sequence labeling neural network using
    "train" mode. This will create the following directory that contains
    the pre-trained neural network weights after each epoch:

        <experiment_dir>/train/<database.task.protocol>.<subset>

    This means that the network was trained on the <subset> subset of the
    <database.task.protocol> protocol. By default, <subset> is "train".
    This directory is called <train_dir> in the subsequent "tune" mode.

"tune" mode:
    Then, one should tune the hyper-parameters using "tune" mode.
    This will create the following directory that contains a file called
    "tune.yml" describing the best hyper-parameters to use:

        <train_dir>/tune/<database.task.protocol>.<subset>

    This means that hyper-parameters were tuned on the <subset> subset of the
    <database.task.protocol> protocol. By default, <subset> is "development".
    This directory is called <tune_dir> in the subsequence "apply" mode.

"apply" mode
    Finally, one can apply speech activity detection using "apply" mode.
    This will create the following files that contains the hard and soft
    outputs of speech activity detection.

        <tune_dir>/apply/<database.task.protocol>.<subset>/{uri}.hard.json
                                                          /{uri}.soft.pkl
                                                          /eval.txt

    This means that file whose unique resource identifier is {uri} has been
    processed.

"""

import time
import yaml
import pickle
import os.path
import datetime
import functools
import numpy as np

from docopt import docopt

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import pyannote.core
import pyannote.core.json

from pyannote.audio.labeling.base import SequenceLabeling
from pyannote.audio.generators.speech import SpeechActivityDetectionBatchGenerator

from pyannote.audio.labeling.aggregation import SequenceLabelingAggregation
from pyannote.audio.signal import Binarize

from pyannote.database import get_database
from pyannote.database.util import FileFinder
from pyannote.database.util import get_unique_identifier
from pyannote.database.util import get_annotated

from pyannote.database.protocol import SpeakerDiarizationProtocol

from pyannote.audio.util import mkdir_p

from pyannote.audio.optimizers import SSMORMS3

import skopt
import skopt.utils
import skopt.space
import skopt.plots
from pyannote.metrics.detection import DetectionErrorRate
from pyannote.metrics.detection import DetectionRecall
from pyannote.metrics.detection import DetectionPrecision
from pyannote.metrics import f_measure


WEIGHTS_H5 = '{train_dir}/weights/{epoch:04d}.h5'



def train(protocol, experiment_dir, train_dir, subset='train'):

    # -- TRAINING --
    nb_epoch = 1000
    optimizer = SSMORMS3()
    batch_size = 8192

    # load configuration file
    config_yml = experiment_dir + '/config.yml'
    with open(config_yml, 'r') as fp:
        config = yaml.load(fp)

    # -- FEATURE EXTRACTION --
    feature_extraction_name = config['feature_extraction']['name']
    features = __import__('pyannote.audio.features',
                          fromlist=[feature_extraction_name])
    FeatureExtraction = getattr(features, feature_extraction_name)
    feature_extraction = FeatureExtraction(
        **config['feature_extraction'].get('params', {}))

    # -- ARCHITECTURE --
    architecture_name = config['architecture']['name']
    models = __import__('pyannote.audio.labeling.models',
                        fromlist=[architecture_name])
    Architecture = getattr(models, architecture_name)
    architecture = Architecture(
        **config['architecture'].get('params', {}))

    # -- SEQUENCE GENERATOR --
    duration = config['sequences']['duration']
    step = config['sequences']['step']
    generator = SpeechActivityDetectionBatchGenerator(
        feature_extraction, duration=duration, step=step,
        batch_size=batch_size)

    # do not cache features in memory when they are precomputed on disk
    # as this does not bring any significant speed-up
    # but does consume (potentially) a LOT of memory
    generator.cache_preprocessed_ = \
        'Precomputed' not in feature_extraction_name

    # number of samples per epoch + round it to closest batch
    seconds_per_epoch = protocol.stats(subset)['annotated']
    samples_per_epoch = batch_size * \
        int(np.ceil((seconds_per_epoch / step) / batch_size))

    # input shape (n_frames, n_features)
    input_shape = generator.shape

    labeling = SequenceLabeling()
    labeling.fit(input_shape, architecture,
                 generator(getattr(protocol, subset)(), infinite=True),
                 samples_per_epoch, nb_epoch,
                 optimizer=optimizer, log_dir=train_dir)


def get_aggregation(epoch, train_dir=None, feature_extraction=None,
                    duration=None, step=None):

    architecture_yml = train_dir + '/architecture.yml'
    weights_h5 = WEIGHTS_H5.format(train_dir=train_dir, epoch=epoch)

    sequence_labeling = SequenceLabeling.from_disk(
        architecture_yml, weights_h5)

    aggregation = SequenceLabelingAggregation(
        sequence_labeling, feature_extraction,
        duration=duration, step=step)

    return aggregation


def speech_activity_detection_xp(aggregation, protocol, subset='development',
                                 onset=None, offset=None, collar=0.0):

    detection_error_rate = DetectionErrorRate(collar=collar)

    predictions = {}

    f, n = 0., 0
    for item in getattr(protocol, subset)():
        uri = get_unique_identifier(item)
        prediction = aggregation.apply(item)
        predictions[uri] = prediction

    binarizer = Binarize(onset=onset, offset=offset)

    for item in getattr(protocol, subset)():
        uri = get_unique_identifier(item)
        reference = item['annotation']
        uem = get_annotated(item)
        hypothesis = binarizer.apply(predictions[uri], dimension=1)
        der = detection_error_rate(reference, hypothesis, uem=uem)

    return abs(der), onset, offset

def validate(protocol, train_dir, validation_dir, subset='development'):

    mkdir_p(validation_dir)

    # -- CONFIGURATION --
    config_dir = os.path.dirname(os.path.dirname(train_dir))
    config_yml = config_dir + '/config.yml'
    with open(config_yml, 'r') as fp:
        config = yaml.load(fp)

    # -- FEATURE EXTRACTION --
    feature_extraction_name = config['feature_extraction']['name']
    features = __import__('pyannote.audio.features',
                          fromlist=[feature_extraction_name])
    FeatureExtraction = getattr(features, feature_extraction_name)
    feature_extraction = FeatureExtraction(
        **config['feature_extraction'].get('params', {}))

    # -- SEQUENCE GENERATOR --
    duration = config['sequences']['duration']
    step = config['sequences']['step']

    # detection error rates
    DER_TEMPLATE = '{epoch:04d} {now} {der:5f}\n'
    ders = []
    path = validation_dir + '/{subset}.der.txt'.format(subset=subset)

    with open(path, mode='w') as fp:

        epoch = 0
        while True:

            # wait until weight file is available
            weights_h5 = WEIGHTS_H5.format(train_dir=train_dir, epoch=epoch)
            if not os.path.isfile(weights_h5):
                time.sleep(60)
                continue

            now = datetime.datetime.now().isoformat()

            aggregation = get_aggregation(
                epoch,
                train_dir=train_dir,
                feature_extraction=feature_extraction,
                duration=duration,
                step=step)

            # do not cache features in memory when they are precomputed on disk
            # as this does not bring any significant speed-up
            # but does consume (potentially) a LOT of memory
            aggregation.cache_preprocessed_ = \
                'Precomputed' not in feature_extraction_name

            if isinstance(protocol, SpeakerDiarizationProtocol):
                der, onset, offset = speech_activity_detection_xp(
                    aggregation, protocol, subset=subset,
                    onset=0.5, offset=0.5)

            fp.write(DER_TEMPLATE.format(epoch=epoch, der=der, now=now))
            fp.flush()

            ders.append(der)
            best_epoch = np.argmin(ders)
            best_value = np.min(ders)
            fig = plt.figure()
            plt.plot(ders, 'b')
            plt.plot([best_epoch], [best_value], 'bo')
            plt.plot([0, epoch], [best_value, best_value], 'k--')
            plt.grid(True)
            plt.xlabel('epoch')
            plt.ylabel('DER on {subset}'.format(subset=subset))
            TITLE = 'DER = {best_value:.5g} on {subset} @ epoch #{best_epoch:d}'
            title = TITLE.format(best_value=best_value,
                                 best_epoch=best_epoch,
                                 subset=subset)
            plt.title(title)
            plt.tight_layout()
            path = validation_dir + '/{subset}.der.png'.format(subset=subset)
            plt.savefig(path, dpi=150)
            plt.close(fig)

            # skip to next epoch
            epoch += 1


def tune(protocol, train_dir, tune_dir, subset='development'):

    np.random.seed(1337)
    os.makedirs(tune_dir)

    architecture_yml = train_dir + '/architecture.yml'

    nb_epoch = 0
    while True:
        weights_h5 = WEIGHTS_H5.format(train_dir=train_dir, epoch=nb_epoch)
        if not os.path.isfile(weights_h5):
            break
        nb_epoch += 1

    config_dir = os.path.dirname(os.path.dirname(train_dir))
    config_yml = config_dir + '/config.yml'
    with open(config_yml, 'r') as fp:
        config = yaml.load(fp)

    # -- FEATURE EXTRACTION --
    feature_extraction_name = config['feature_extraction']['name']
    features = __import__('pyannote.audio.features',
                          fromlist=[feature_extraction_name])
    FeatureExtraction = getattr(features, feature_extraction_name)
    feature_extraction = FeatureExtraction(
        **config['feature_extraction'].get('params', {}))

    # -- SEQUENCE GENERATOR --
    duration = config['sequences']['duration']
    step = config['sequences']['step']

    predictions = {}

    def objective_function(parameters):

        epoch, onset, offset = parameters

        weights_h5 = WEIGHTS_H5.format(train_dir=train_dir, epoch=epoch)

        sequence_labeling = SequenceLabeling.from_disk(
            architecture_yml, weights_h5)

        aggregation = SequenceLabelingAggregation(
            sequence_labeling, feature_extraction,
            duration=duration, step=step)

        if epoch not in predictions:
            predictions[epoch] = {}

        # no need to use collar during tuning
        error = DetectionErrorRate()

        for dev_file in getattr(protocol, subset)():

            uri = get_unique_identifier(dev_file)
            reference = dev_file['annotation']
            uem = get_annotated(dev_file)

            if uri in predictions[epoch]:
                prediction = predictions[epoch][uri]
            else:
                prediction = aggregation.apply(dev_file)
                predictions[epoch][uri] = prediction

            binarizer = Binarize(onset=onset, offset=offset)
            hypothesis = binarizer.apply(prediction, dimension=1)

            _ = error(reference, hypothesis, uem=uem)

        return abs(error)

    def callback(res):

        n_trials = len(res.func_vals)

        # save best parameters so far
        epoch, onset, offset = res.x
        params = {'status': {'nb_epoch': nb_epoch},
                  'epoch': int(epoch),
                  'onset': float(onset),
                  'offset': float(offset)}
        with open(tune_dir + '/tune.yml', 'w') as fp:
            yaml.dump(params, fp, default_flow_style=False)

        # plot convergence
        _ = skopt.plots.plot_convergence(res)
        plt.savefig(tune_dir + '/convergence.png', dpi=150)
        plt.close()

        if n_trials % 10 > 0:
            return

        # plot evaluations
        _ = skopt.plots.plot_evaluations(res)
        plt.savefig(tune_dir + '/evaluation.png', dpi=150)
        plt.close()

        try:
            # plot objective function
            _ = skopt.plots.plot_objective(res)
            plt.savefig(tune_dir + '/objective.png', dpi=150)
            plt.close()
        except Exception as e:
            pass

        # save results so far
        func = res['specs']['args']['func']
        callback = res['specs']['args']['callback']
        del res['specs']['args']['func']
        del res['specs']['args']['callback']
        skopt.utils.dump(res, tune_dir + '/tune.gz', store_objective=True)
        res['specs']['args']['func'] = func
        res['specs']['args']['callback'] = callback

    epoch = skopt.space.Integer(0, nb_epoch - 1)
    onset = skopt.space.Real(0., 1., prior='uniform')
    offset = skopt.space.Real(0., 1., prior='uniform')

    res = skopt.gp_minimize(
        objective_function,
        [epoch, onset, offset], callback=callback,
        n_calls=1000, n_random_starts=10,
        x0=[nb_epoch - 1, 0.5, 0.5],
        random_state=1337, verbose=True)

    return res


def test(protocol, tune_dir, apply_dir, subset='test', beta=1.0):

    os.makedirs(apply_dir)

    train_dir = os.path.dirname(os.path.dirname(tune_dir))
    config_dir = os.path.dirname(os.path.dirname(train_dir))

    config_yml = config_dir + '/config.yml'
    with open(config_yml, 'r') as fp:
        config = yaml.load(fp)

    # -- FEATURE EXTRACTION --
    feature_extraction_name = config['feature_extraction']['name']
    features = __import__('pyannote.audio.features',
                          fromlist=[feature_extraction_name])
    FeatureExtraction = getattr(features, feature_extraction_name)
    feature_extraction = FeatureExtraction(
        **config['feature_extraction'].get('params', {}))

    # -- SEQUENCE GENERATOR --
    duration = config['sequences']['duration']
    step = config['sequences']['step']

    # -- HYPER-PARAMETERS --
    tune_yml = tune_dir + '/tune.yml'
    with open(tune_yml, 'r') as fp:
        tune = yaml.load(fp)

    architecture_yml = train_dir + '/architecture.yml'
    weights_h5 = WEIGHTS_H5.format(train_dir=train_dir, epoch=epoch)

    sequence_labeling = SequenceLabeling.from_disk(
        architecture_yml, weights_h5)

    aggregation = SequenceLabelingAggregation(
        sequence_labeling, feature_extraction,
        duration=duration, step=step)

    binarizer = Binarize(onset=tune['onset'], offset=tune['offset'])

    HARD_JSON = apply_dir + '/{uri}.hard.json'
    SOFT_PKL = apply_dir + '/{uri}.soft.pkl'

    eval_txt = apply_dir + '/eval.txt'
    TEMPLATE = '{uri} {precision:.5f} {recall:.5f} {f_measure:.5f}\n'
    precision = DetectionPrecision()
    recall = DetectionRecall()
    fscore = []

    for test_file in getattr(protocol, subset)():

        soft = aggregation.apply(test_file)
        hard = binarizer.apply(soft, dimension=1)

        uri = get_unique_identifier(test_file)

        path = SOFT_PKL.format(uri=uri)
        mkdir_p(os.path.dirname(path))
        with open(path, 'w') as fp:
            pickle.dump(soft, fp)

        path = HARD_JSON.format(uri=uri)
        mkdir_p(os.path.dirname(path))
        with open(path, 'w') as fp:
            pyannote.core.json.dump(hard, fp)

        try:
            reference = test_file['annotation']
            uem = test_file['annotated']
        except KeyError as e:
            continue

        p = precision(reference, hard, uem=uem)
        r = recall(reference, hard, uem=uem)
        f = f_measure(p, r, beta=beta)
        fscore.append(f)

        line = TEMPLATE.format(
            uri=uri, precision=p, recall=r, f_measure=f)
        with open(eval_txt, 'a') as fp:
            fp.write(line)

    p = abs(precision)
    r = abs(recall)
    f = np.mean(fscore)
    line = TEMPLATE.format(
        uri='ALL', precision=p, recall=r, f_measure=f)
    with open(eval_txt, 'a') as fp:
        fp.write(line)


if __name__ == '__main__':

    arguments = docopt(__doc__, version='Speech activity detection')

    db_yml = os.path.expanduser(arguments['--database'])
    preprocessors = {'wav': FileFinder(db_yml)}

    if '<database.task.protocol>' in arguments:
        protocol = arguments['<database.task.protocol>']
        database_name, task_name, protocol_name = protocol.split('.')
        database = get_database(database_name, preprocessors=preprocessors)
        protocol = database.get_protocol(task_name, protocol_name,
                                         progress=True)

    subset = arguments['--subset']

    if arguments['train']:
        experiment_dir = arguments['<experiment_dir>']

        if subset is None:
            subset = 'train'

        TRAIN_DIR = '{experiment_dir}/train/{protocol}.{subset}/{path}'
        train_dir = TRAIN_DIR.format(
            experiment_dir=experiment_dir,
            protocol=arguments['<database.task.protocol>'],
            subset=subset)

        protocol.progress = False
        train(protocol, experiment_dir, train_dir, subset=subset)

    if arguments['validation']:
        train_dir = arguments['<train_dir>']
        if subset is None:
            subset = 'development'

        validation_dir = train_dir + '/validation/' + arguments['<database.task.protocol>']

        protocol.progress = False
        res = validate(protocol, train_dir, validation_dir, subset=subset)

    if arguments['tune']:
        train_dir = arguments['<train_dir>']
        if subset is None:
            subset = 'development'
        beta = float(arguments.get('--recall'))
        tune_dir = train_dir + '/tune/' + arguments['<database.task.protocol>'] + '.' + subset
        res = tune(protocol, train_dir, tune_dir, subset=subset)

    if arguments['apply']:
        tune_dir = arguments['<tune_dir>']
        if subset is None:
            subset = 'test'
        beta = float(arguments.get('--recall'))
        apply_dir = tune_dir + '/apply/' + arguments['<database.task.protocol>'] + '.' + subset
        res = test(protocol, tune_dir, apply_dir, beta=beta, subset=subset)
