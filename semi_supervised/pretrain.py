"""Pretrain script for seq2seq model."""

from __future__ import print_function

import math
import os
import random
import sys
import time
import logging

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

from tensorflow.models.rnn.translate import data_utils
from tensorflow.models.rnn.translate import seq2seq_model


from .data import get_vocabulary, DEFAULT_DATA_DIR, smile_tokenizer


tf.app.flags.DEFINE_float("learning_rate", 0.5, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.99,
                          "Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0,
                          "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer("batch_size", 256,
                            "Batch size to use during training.")
tf.app.flags.DEFINE_integer("size", 256, "Size of each model layer.")
tf.app.flags.DEFINE_integer("num_layers", 3, "Number of layers in the model.")
# tf.app.flags.DEFINE_string("data_dir", DEFAULT_DATA_DIR, "Data directory")
tf.app.flags.DEFINE_string("vocab_path", os.path.join(DEFAULT_DATA_DIR, "pm2.vocab"), 
                           "Vocabulary path.")
tf.app.flags.DEFINE_string("train_dir", os.path.join(DEFAULT_DATA_DIR, "train"), 
                           "Training directory.")
tf.app.flags.DEFINE_integer("max_train_data_size", 0,
                            "Limit on the size of training data (0: no limit).")
tf.app.flags.DEFINE_integer("steps_per_checkpoint", 200,
                            "How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_boolean("decode", False,
                            "Set to True for interactive decoding.")
tf.app.flags.DEFINE_integer("decode_size", 10, "Set number of decode samples.")
tf.app.flags.DEFINE_boolean("self_test", False,
                            "Run a self-test if this is set to True.")
tf.app.flags.DEFINE_boolean("use_fp16", False,
                            "Train using fp16 instead of fp32.")

FLAGS = tf.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model.Seq2SeqModel for details of how they work.
_buckets = [(20, 20), (40, 40), (60, 60), (80, 80)]


def read_data(source_path, max_size=None):
    """Read data from source and target files and put into buckets.

    Args:
        source_path: path to the files with token-ids for the source language.
        max_size: maximum number of lines to read, all other will be ignored;
            if 0 or None, data files will be read completely (no limit).

    Returns:
        data_set: a list of length len(_buckets); data_set[n] contains a list of
            (source, target) pairs read from the provided data files that fit
            into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
            len(target) < _buckets[n][1]; source and target are lists of token-ids.
    """
    data_set = [[] for _ in _buckets]
    with tf.gfile.GFile(source_path, mode="r") as source_file:
        source = source_file.readline()
        counter = 0
        while source and (not max_size or counter < max_size):
            counter += 1
            if counter % 100000 == 0:
                print("  reading data line %d" % counter)
                sys.stdout.flush()
            source_ids = [int(x) for x in source.split()]
            target_ids = [int(x) for x in source.split()]
            target_ids.append(data_utils.EOS_ID)
            for bucket_id, (source_size, target_size) in enumerate(_buckets):
                if len(source_ids) < source_size and len(target_ids) < target_size:
                    data_set[bucket_id].append([source_ids, target_ids])
                    break
            source = source_file.readline()
    return data_set


def get_vocab_size(vocab_path):
    """Get vocabulary size."""
    counter = 0
    with tf.gfile.GFile(vocab_path, mode="r") as vocab_file:
        for _line in vocab_file:
            if len(_line.strip()):
                counter += 1
    return counter


def create_model(session, forward_only):
    """Create translation model and initialize or load parameters in session."""
    dtype = tf.float16 if FLAGS.use_fp16 else tf.float32
    vocab_size = get_vocab_size(FLAGS.vocab_path)
    model = seq2seq_model.Seq2SeqModel(
            vocab_size,
            vocab_size,
            _buckets,
            FLAGS.size,
            FLAGS.num_layers,
            FLAGS.max_gradient_norm,
            FLAGS.batch_size,
            FLAGS.learning_rate,
            FLAGS.learning_rate_decay_factor,
            forward_only=forward_only,
            dtype=dtype)
    if not os.path.exists(FLAGS.train_dir):
        os.makedirs(FLAGS.train_dir)
    ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
    if ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
        print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
        model.saver.restore(session, ckpt.model_checkpoint_path)
    else:
        print("Created model with fresh parameters.")
        session.run(tf.initialize_all_variables())
    return model


def train():
    """Train a SMILE-SMILE autoencoder model using WMT data."""
    # Prepare WMT data.
    print("Preparing LogP and PM2 data in %s..." % FLAGS.data_dir)
    train_path = os.path.join(FLAGS.data_dir, "pm2.tokens")
    dev_path = os.path.join(FLAGS.data_dir, "logp.tokens")

    with tf.Session() as sess:
        # Create model.
        print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
        model = create_model(sess, False)

        # Read data into buckets and compute their sizes.
        print ("Reading development and training data (limit: %d)."
               % FLAGS.max_train_data_size)
        dev_set = read_data(dev_path)
        train_set = read_data(train_path, FLAGS.max_train_data_size)
        train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
        train_total_size = float(sum(train_bucket_sizes))

        # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
        # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
        # the size if i-th training bucket, as used later.
        train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                               for i in xrange(len(train_bucket_sizes))]

        # This is the training loop.
        step_time, loss = 0.0, 0.0
        current_step = 0
        previous_losses = []
        while True:
            # Choose a bucket according to data distribution. We pick a random number
            # in [0, 1] and use the corresponding interval in train_buckets_scale.
            random_number_01 = np.random.random_sample()
            bucket_id = min([i for i in xrange(len(train_buckets_scale))
                             if train_buckets_scale[i] > random_number_01])

            # Get a batch and make a step.
            start_time = time.time()
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                train_set, bucket_id)
            _, step_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                         target_weights, bucket_id, False)
            step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
            loss += step_loss / FLAGS.steps_per_checkpoint
            current_step += 1

            # Once in a while, we save checkpoint, print statistics, and run evals.
            if current_step % FLAGS.steps_per_checkpoint == 0:
                # Print statistics for the previous epoch.
                perplexity = math.exp(float(loss)) if loss < 300 else float("inf")
                print ("global step %d learning rate %.4f step-time %.2f perplexity "
                       "%.6f" % (model.global_step.eval(), model.learning_rate.eval(),
                                 step_time, perplexity))
                # Decrease learning rate if no improvement was seen over last 3 times.
                if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
                    sess.run(model.learning_rate_decay_op)
                previous_losses.append(loss)
                # Save checkpoint and zero timer and loss.
                checkpoint_path = os.path.join(FLAGS.train_dir, "seq2seq_pretrain.ckpt")
                model.saver.save(sess, checkpoint_path, global_step=model.global_step)
                step_time, loss = 0.0, 0.0
                # Run evals on development set and print their perplexity.
                for bucket_id in xrange(len(_buckets)):
                    if len(dev_set[bucket_id]) == 0:
                        print("  eval: empty bucket %d" % (bucket_id))
                        continue
                    encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                        dev_set, bucket_id)
                    _, eval_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                                 target_weights, bucket_id, True)
                    eval_ppx = math.exp(float(eval_loss)) if eval_loss < 300 else float("inf")
                    print("  eval: bucket %d perplexity %.2f" % (bucket_id, eval_ppx))
                sys.stdout.flush()


def decode():
    with tf.Session() as sess:
        # Create model and load parameters.
        model = create_model(sess, True)
        model.batch_size = 1  # We decode one sentence at a time.

        # Load vocabularies.
        vocab, rev_vocab = get_vocabulary(FLAGS.data_dir, FLAGS.vocab_path)

        dev_path = os.path.join(FLAGS.data_dir, "logp.tmp")
        # Read data into buckets and compute their sizes.
        print ("Read development dataset...")

        def sample_sentences(dev_path):
            """Sample several sentences."""
            samples = set()
            with open(dev_path) as fobj:
                lines = [_line for _line in fobj.readlines() if len(_line.strip())]
            while len(samples) < FLAGS.decode_size:
                samples.add(random.randrange(len(lines)))
            return [lines[index].strip() for index in list(samples)]


        exact_match_counter = 0
        # Decode from random sample in 
        sentences = sample_sentences(dev_path)

        for sentence in sentences:
            print(": %s" % sentence)
            # Get token-ids for the input sentence.
            token_ids = data_utils.sentence_to_token_ids(
                tf.compat.as_bytes(sentence), vocab, 
                tokenizer=smile_tokenizer, normalize_digits=False)
            # Which bucket does it belong to?
            bucket_id = len(_buckets) - 1
            for i, bucket in enumerate(_buckets):
                if bucket[0] >= len(token_ids):
                    bucket_id = i
                    break

            # Get a 1-element batch to feed the sentence to the model.
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                    {bucket_id: [(token_ids, [])]}, bucket_id)
            # Get output logits for the sentence.
            _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                             target_weights, bucket_id, True)
            # This is a greedy decoder - outputs are just argmaxes of output_logits.
            outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]
            # If there is an EOS symbol in outputs, cut them at that point.
            if data_utils.EOS_ID in outputs:
                outputs = outputs[:outputs.index(data_utils.EOS_ID)]
            # Print out French sentence corresponding to outputs.
            output_sentence = "".join([tf.compat.as_str(rev_vocab[output]) for output in outputs])
            print("> %s\n" % output_sentence)
            if sentence == output_sentence:
                exact_match_counter += 1
            sys.stdout.flush()
        print("Exact match: %d/%d" % (exact_match_counter, FLAGS.decode_size))


def self_test_read_data():
    """Self test read data."""
    pm2_data = os.path.join(FLAGS.data_dir, "pm2.tokens")
    print(read_data(pm2_data, 5))   # visual check.


def self_test_get_vocab_size():
    """Self test get_vocab_size."""
    print(get_vocab_size(FLAGS.vocab_path)) # Visual check, should be 42.


def main(_):
    if FLAGS.self_test:
        self_test_read_data()
        self_test_get_vocab_size()
    elif FLAGS.decode:
        decode()
    else:
        train()


if __name__ == "__main__":
    tf.app.run()