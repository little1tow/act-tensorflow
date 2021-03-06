#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
import pickle, os, sys
import tensorflow as tf
from tensorflow.python.ops.nn import rnn_cell, rnn, seq2seq
from tensorflow.python.ops import control_flow_ops, gen_math_ops
from tensorflow.python.ops import variable_scope as vs


class ACTCellMasking(rnn_cell.RNNCell):

    """An RNN cell implementing Graves' Adaptive Computation Time algorithm"""


    def __init__(self, num_units, cell, epsilon, max_computation, batch_size, use_lstm = False,
        use_mlp_for_halting_probability = False, num_perceptron_layers = 2):

        self.batch_size = batch_size
        self.one_minus_eps = tf.constant(1.0 - epsilon, tf.float32,[self.batch_size])
        self._num_units = num_units
        self.cell = cell
        self.use_lstm = use_lstm
        self.use_mlp_for_halting_probability = use_mlp_for_halting_probability
        self.num_perceptron_layers = num_perceptron_layers
        self.N = tf.constant(max_computation, tf.float32,[self.batch_size])
        self.ACT_remainder = []
        self.ACT_iterations = []

    @property
    def input_size(self):
        return self._num_units
    @property
    def output_size(self):
        return self._num_units
    @property
    def state_size(self):
        return self._num_units

    def __call__(self, inputs, state, timestep = 0, scope=None):

        with vs.variable_scope(scope or type(self).__name__):

            # define within cell constants/ counters used to control while loop
            prob = tf.constant(0.0,tf.float32,[self.batch_size], name="prob")
            prob_compare = tf.constant(0.0,tf.float32,[self.batch_size], name="prob_compare")
            counter = tf.constant(0.0, tf.float32,[self.batch_size], name="counter")
            acc_outputs = tf.zeros_like(state, tf.float32, name="output_accumulator")
            acc_states = tf.zeros_like(state, tf.float32, name="state_accumulator")
            batch_mask = tf.constant(True, tf.bool,[self.batch_size])

            # the predicate for stopping the while loop. Tensorflow demands that we have
            # all of the variables used in the while loop in the predicate.

            pred = lambda batch_mask,prob_compare,prob,counter,state,input,acc_output,acc_state:\
                tf.reduce_any(tf.logical_and(tf.less(prob_compare,self.one_minus_eps),tf.less(counter,self.N)))
                               # only stop if all of the batch have passed either threshold

            _,_,prob,iterations,_,_,output,next_state = control_flow_ops.while_loop(
                pred,self.ACTStep,[batch_mask,prob_compare,prob,counter,state,inputs, acc_outputs, acc_states])


        '''Calculate ponder cost parts. Reduce mean is used to normalize cost by the batch size'''
        self.ACT_remainder.append(tf.reduce_mean(1 - prob)) #TODO: double check this
        self.ACT_iterations.append(tf.reduce_mean(iterations))

        return output, next_state

    def CalculatePonderCost(self, time_penalty):
        '''returns tensor of shape [1] which is the total ponder cost'''

        return time_penalty * tf.reduce_sum(
            tf.add_n(self.ACT_remainder)/len(self.ACT_remainder) +
            tf.to_float(tf.add_n(self.ACT_iterations)/len(self.ACT_iterations)))


    def CalculateHaltingProbability(self, input_for_eval_halting_p):
        #TODO: leavesbreathe, determine if leaky relu is better. Also integrate batch norm here?
        def LeakyRELU(input, alpha = 0.01):
            return tf.maximum(alpha*input, input)

        with tf.variable_scope('sigmoid_activation_for_pondering'): #start with a bias of 1.0
            if self.use_mlp_for_halting_probability:
                for layer in xrange(self.num_perceptron_layers-1):
                    with tf.variable_scope('mlp_layer_'+str(layer)):
                        input_for_eval_halting_p = tf.nn.relu(tf.nn.rnn_cell._linear(input_for_eval_halting_p, self.output_size, True))
                with tf.variable_scope('mlp_second_layer'):
                    p = tf.squeeze(tf.sigmoid(tf.nn.rnn_cell._linear(input_for_eval_halting_p, 1, True, 1.0)))
            else:
                p = tf.squeeze(tf.sigmoid(tf.nn.rnn_cell._linear(input_for_eval_halting_p, 1, True, 1.0)))
        return p

    def ACTStep(self,batch_mask,prob_compare,prob,counter,state,input,acc_outputs,acc_states):

        #TODO: leavesbreathe, implement batch norming hidden state?
        output, new_state = rnn(self.cell, [input], state, scope=type(self.cell).__name__)

        
        if self.use_lstm:
            input_for_eval_halting_p, _ = tf.split(1,2,new_state)
        else:
            input_for_eval_halting_p = new_state
        p = self.CalculateHaltingProbability(input_for_eval_halting_p)

        # here we create a mask on the p vector, which we then multiply with the state/output.
        # if p[i] = 0, then we have passed the remainder point for that example, so we multiply
        # the state/output vector by this masked probability(which has zeros if the prob for
        # a batch has passed the stopping point) so we carry none of it forward.
        #  If, by adding p, we pass the boundary, we don't add p onto prob - this allows us to
        # use the use_remainder() as normal for all steps after ALL examples have taken their max time.

        # multiply by the previous mask as if we stopped before, we don't want to start again
        new_batch_mask = tf.logical_and(tf.less(prob + p,self.one_minus_eps),batch_mask)
        float_mask = tf.cast(new_batch_mask, tf.float32)


        # only increase the prob accumulator for the examples
        # which haven't already passed the threshold. This
        # means that we can just use the final prob value per
        # example to determine the remainder.
        prob += p * float_mask

        prob_compare += p * tf.cast(batch_mask, tf.float32)

        def use_remainder():
            remainder = tf.constant(1.0, tf.float32,[self.batch_size]) - prob
            remainder_expanded = tf.expand_dims(remainder,1)
            #leavesbreathe commented out the tiling below for lstm implementation
            # tiled_remainder = tf.tile(remainder_expanded,[1,self.output_size])

            acc_state = tf.add(tf.mul(new_state,remainder_expanded), acc_states)
            acc_output = tf.add(tf.mul(output[0], remainder_expanded), acc_outputs)
            return acc_state, acc_output

        def normal():
            p_expanded = tf.expand_dims(p*float_mask,1)
            # tiled_p = tf.tile(p_expanded,[1,self.output_size])

            acc_state = tf.add(tf.mul(new_state, p_expanded), acc_states)
            acc_output = tf.mul(output[0], p_expanded) + acc_outputs
            return acc_state, acc_output

        # halting condition: if the batch mask is all zeros, then all batches have finished.
        # therefore, if the sum of the mask = 0, then we use the remainder.
        counter += tf.constant(1.0,tf.float32,[self.batch_size]) * float_mask


        counter_condition = tf.less(counter,self.N)
        condition = tf.reduce_any(tf.logical_and(new_batch_mask,counter_condition))

        acc_state, acc_output = tf.cond(condition, normal, use_remainder)

 
        # only increment the counter for the examples which are still running
        # counter += tf.constant(1.0,tf.float32,[self.batch_size])
        return [new_batch_mask,prob_compare,prob,counter,new_state, input, acc_output,acc_state]
