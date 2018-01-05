# from https://gist.github.com/robertnishihara/87aa7a9a68ef8fa0f3184129346cffc3
# To run the example, use a command like the following.
#
#     python sharded_parameter_server_benchmark.py \
#         --num-workers=1 \
#         --num-parameter-servers=1 \
#         --dim=25000

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function


import argparse
import numpy as np
import os
import sys
import time

from collections import OrderedDict
from collections import defaultdict

import ray

# move some methods to util later, for now "u" points to this file
util = sys.modules[__name__]   
u = util

parser = argparse.ArgumentParser(description="Run the synchronous parameter "
                                             "server example.")
parser.add_argument("--num-workers", default=2, type=int,
                    help="The number of workers to use.")
parser.add_argument("--num-parameter-servers", default=2, type=int,
                    help="The number of parameter servers to use.")
parser.add_argument("--dim", default=151190, type=int,
                    help="The number of parameters, defaults to size of "
                    "TF default CIFAR10 model")
parser.add_argument("--redis-address", default=None, type=str,
                    help="The Redis address of the cluster.")
parser.add_argument("--add-pause", default=0, type=int,
                    help="Add pause to avoid melting my laptop.")
parser.add_argument('--logdir', type=str, default='asdfasdfasdf',
                     help="location of logs")
args = parser.parse_args()


          


########################################
# Tensorboard logging, move to util.py
########################################
def chunks(l, n):
  """Yield successive n-sized chunks from l."""
  for i in range(0, len(l), n):
    yield l[i:i + n]

global_timeit_dict = OrderedDict()
class timeit:
  """Decorator to measure length of time spent in the block in millis and log
  it to TensorBoard."""
  
  def __init__(self, tag=""):
    self.tag = tag
    
  def __enter__(self):
    self.start = time.perf_counter()
    return self
  
  def __exit__(self, *args):
    self.end = time.perf_counter()
    interval_ms = 1000*(self.end - self.start)
    global_timeit_dict.setdefault(self.tag, []).append(interval_ms)
    logger = u.get_last_logger(skip_existence_check=True)
    if logger:
      newtag = 'time/'+self.tag
      logger(newtag, interval_ms)

# TODO: have global experiment_base that I can use to move logging to
# non-current directory
GLOBAL_RUNS_DIRECTORY='runs'
global_last_logger = None

def get_last_logger(skip_existence_check=False):
  """Returns last logger, if skip_existence_check is set, doesn't
  throw error if logger doesn't exist."""
  global global_last_logger
  if not skip_existence_check:
    assert global_last_logger
  return global_last_logger

class TensorboardLogger:
  """Helper class to log to single tensorboard writer from multiple places.
   logger = u.TensorboardLogger("mnist7")
   logger = u.get_last_logger()  # gets last logger created
   logger('svd_time', 5)  # records "svd_time" stat at 5
   logger.next_step()     # advances step counter
   logger.set_step(5)     # sets step counter to 5
  """
  
  def __init__(self, logdir, step=0):
    # TODO: do nothing for default run
    
    global global_last_logger
    assert global_last_logger is None
    self.logdir = logdir,
    self.summary_writer = tf.summary.FileWriter(logdir,
                                                flush_secs=1,
                                                graph=tf.get_default_graph())
    self.step = step
    self.summary = tf.Summary()
    global_last_logger = self
    self.last_timestamp = time.perf_counter()

  def __call__(self, *args):
    assert len(args)%2 == 0
    for (tag, value) in chunks(args, 2):
      self.summary.value.add(tag=tag, simple_value=float(value))

  def next_step(self):
    new_timestamp = time.perf_counter()
    interval_ms = 1000*(new_timestamp - self.last_timestamp)
    self.summary.value.add(tag='time/step',
                           simple_value=interval_ms)
    self.last_timestamp = new_timestamp
    self.summary_writer.add_summary(self.summary, self.step)
    self.step+=1
    self.summary = tf.Summary()

################################################################################
## Main stuff
################################################################################

# TODO(rkn): This is a placeholder.
class CNN(object):
    def __init__(self, dim):
        self.dim = dim
        self.fixed = np.ones(self.dim, dtype=np.float32)

    def get_gradients(self):
      #        time.sleep(0.16)
      #        return np.ones(self.dim, dtype=np.float32)
      return self.fixed

    def set_weights(self, weights):
        pass


# TODO(rkn): Once we have better custom resource support for actors, we should
# not use GPUs here.
@ray.remote(num_gpus=1)
class ParameterServer(object):
    def __init__(self, dim):
        self.params = np.zeros(dim)

    def update_and_get_new_weights(self, *gradients):
      #        for grad in gradients:
      #            self.params += grad
        return self.params

    def ip(self):
        return ray.services.get_node_ip_address()


@ray.remote(num_gpus=1)
class Worker(object):
    def __init__(self, num_ps, dim):
        self.net = CNN(dim)
        self.num_ps = num_ps
        self.fixed = np.zeros(dim)

    @ray.method(num_return_vals=args.num_parameter_servers)
    def compute_gradient(self, *weights):
      #        all_weights = np.concatenate(weights)
      #        self.net.set_weights(all_weights)
        self.net.set_weights(self.fixed)
        gradient = self.net.get_gradients()
        if self.num_ps == 1:
            return gradient
        else:
            return np.split(gradient, self.num_ps)

    def ip(self):
        return ray.services.get_node_ip_address()


if __name__ == "__main__":

    import tensorflow as tf
    tf.constant(1)  # dummy default graph to appease tensorboard
    
    if args.redis_address is None:
        # Run everything locally.
        ray.init(num_gpus=args.num_parameter_servers + args.num_workers)
    else:
        # Connect to a cluster.
        ray.init(redis_address=args.redis_address)

    split_weights = np.split(np.zeros(args.dim, dtype=np.float32),
                             args.num_parameter_servers)


    # create tensorboard logger
    logger = u.TensorboardLogger(args.logdir)

    # Create the parameter servers.
    pss = [ParameterServer.remote(split_weights[i].size)
           for i in range(args.num_parameter_servers)]

    # Create the workers.
    workers = [Worker.remote(args.num_parameter_servers, args.dim)
               for _ in range(args.num_workers)]

    # As a sanity check, make sure all workers and parameter servers are on
    # different machines.
    if args.redis_address is not None:
        all_ips = ray.get([ps.ip.remote() for ps in pss] +
                          [w.ip.remote() for w in workers])
        
        print("ps ips:")
        for (i, ps) in enumerate(pss):
            print(i, ps.ip.remote(), ray.get([ps.ip.remote()]))
        print("worker ips:")
        for (i, worker) in enumerate(workers):
            print(i, worker.ip.remote(), ray.get([worker.ip.remote()]))
        if len(all_ips) != len(set(all_ips)):
            print("Warning, some IPs are reused")

    LOG_FREQUENCY = 10
    step = 0
    last_step = 0
    last_time = time.time()
    while True:
        step+=1
        logger.next_step()
        t1 = time.time()

        # Compute and apply gradients.
        assert len(split_weights) == args.num_parameter_servers
        grad_id_lists = [[] for _ in range(len(pss))]
        for worker in workers:
            gradients = worker.compute_gradient.remote(*split_weights)
            if len(pss) == 1:
                gradients = [gradients]

            assert len(gradients) == len(pss)
            for i in range(len(gradients)):
                grad_id_lists[i].append(gradients[i])

        # TODO(rkn): This weight should not be removed. Does it affect
        # performance?
        all_grad_ids = [grad_id for grad_id_list in grad_id_lists
                        for grad_id in grad_id_list]
        with u.timeit('wait_compute_grads'):
          ray.wait(all_grad_ids, num_returns=len(all_grad_ids))

        t2 = time.time()

        split_weights = []
        for i in range(len(pss)):
            assert len(grad_id_lists[i]) == args.num_workers
            new_weights_id = pss[i].update_and_get_new_weights.remote(
                *(grad_id_lists[i]))
            split_weights.append(new_weights_id)

        # TODO(rkn): This weight should not be removed. Does it affect
        # performance?
        with u.timeit('wait_ps_add'):
          ray.wait(split_weights, num_returns=len(split_weights))

        t3 = time.time()
        print("elapsed times: ", t3 - t1, t2 - t1, t3 - t2)
        if step%LOG_FREQUENCY == 0:
            steps_per_sec = (step - last_step)/(time.time()-last_time)
            logger("steps_per_sec", steps_per_sec)
            last_step = step
            last_time = time.time()
            
        if args.add_pause:
          time.sleep(0.1)