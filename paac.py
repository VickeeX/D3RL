import time, logging, zmq, re
from flask import Flask, request
from multiprocessing.sharedctypes import RawArray
from ctypes import c_uint, c_float
from actor_learner import *
from emulator_runner import EmulatorRunner
from runners import Runners
from zmq_serialize import SerializingContext

flask_file_server = Flask(__name__)


@flask_file_server.route('/d3rl/network', methods=['POST'])
def upload_network():
    network_ckpt = request.files.getlist('files')
    file_num, ckpt_num = 0, ""
    for f in network_ckpt:
        f.save("/home/cloud/D3RL_ZMQ/logs/upload/" + f.filename)
        file_num += 1
        if ckpt_num == "":
            ckpt_num = f.filename.split(".")[0]

    with open("/home/cloud/D3RL_ZMQ/logs/upload/checkpoint", "w") as f:
        f.writelines(["model_checkpoint_path: \"" + ckpt_num + "\"\n",
                      "all_model_checkpoint_paths: \"" + ckpt_num + "\""])

    return '{"code":"ok","file_num":%d}' % file_num


class PAACLearner(ActorLearner):
    def __init__(self, network_creator, environment_creator, args):
        super(PAACLearner, self).__init__(network_creator, environment_creator, args)
        self.workers = args.emulator_workers
        self.req = self.__create_zmq_req_socket()
        self.flask_file_server_proc = Process(target=flask_file_server.run,
                                              kwargs={'host': '127.0.0.1', 'port': 6667})
        self.latest_ckpt = "-0"

    @staticmethod
    def create_zmq_req_socket():
        ctx = SerializingContext()
        req = ctx.socket(zmq.REQ)
        req.connect("tcp://127.0.0.1:6666")
        return req

    def __create_zmq_req_socket(self):
        return PAACLearner.create_zmq_req_socket()

    @staticmethod
    def choose_next_actions(network, num_actions, states, session):
        network_output_v, network_output_pi = session.run(
                [network.output_layer_v,
                 network.output_layer_pi],
                feed_dict={network.input_ph: states})

        action_indices = PAACLearner.__sample_policy_action(network_output_pi)

        new_actions = np.eye(num_actions)[action_indices]

        return new_actions, network_output_v, network_output_pi

    def __choose_next_actions(self, states):
        return PAACLearner.choose_next_actions(self.network, self.num_actions, states, self.session)

    @staticmethod
    def __sample_policy_action(probs):
        """
        Sample an action from an action probability distribution output by
        the policy network.
        """
        # Subtract a tiny value from probabilities in order to avoid
        # "ValueError: sum(pvals[:-1]) > 1.0" in numpy.multinomial
        probs = probs - np.finfo(np.float32).epsneg

        action_indexes = [int(np.nonzero(np.random.multinomial(1, p))[0]) for p in probs]
        return action_indexes

    def _get_shared(self, array, dtype=c_float):
        """
        Returns a RawArray backed numpy array that can be shared between processes.
        :param array: the array to be shared
        :param dtype: the RawArray dtype to use
        :return: the RawArray backed numpy array
        """

        shape = array.shape
        shared = RawArray(dtype, array.reshape(-1))
        return np.frombuffer(shared, dtype).reshape(shape)

    def send_zmq_batch_data(self, data):
        self.req.send_zipped_pickle(data)
        msg = self.req.recv_string()

    def train(self):
        self.flask_file_server_proc.start()

        """
        Main actor learner loop for parallel advantage actor critic learning.
        """
        self.global_step = self.init_network()

        logging.debug("Starting training at Step {}".format(self.global_step))
        counter = 0

        global_step_start = self.global_step

        total_rewards = []

        # state, reward, episode_over, action
        variables = [(np.asarray([emulator.get_initial_state() for emulator in self.emulators], dtype=np.uint8)),
                     (np.zeros(self.emulator_counts, dtype=np.float32)),
                     (np.asarray([False] * self.emulator_counts, dtype=np.float32)),
                     (np.zeros((self.emulator_counts, self.num_actions), dtype=np.float32))]

        self.runners = Runners(EmulatorRunner, self.emulators, self.workers, variables)
        self.runners.start()
        shared_states, shared_rewards, shared_episode_over, shared_actions = self.runners.get_shared_variables()

        summaries_op = tf.summary.merge_all()

        emulator_steps = [0] * self.emulator_counts
        total_episode_rewards = self.emulator_counts * [0]

        actions_sum = np.zeros((self.emulator_counts, self.num_actions))
        y_batch = np.zeros((self.max_local_steps, self.emulator_counts))
        adv_batch = np.zeros((self.max_local_steps, self.emulator_counts))
        rewards = np.zeros((self.max_local_steps, self.emulator_counts))
        states = np.zeros([self.max_local_steps] + list(shared_states.shape), dtype=np.uint8)
        actions = np.zeros((self.max_local_steps, self.emulator_counts, self.num_actions))
        values = np.zeros((self.max_local_steps, self.emulator_counts))
        episodes_over_masks = np.zeros((self.max_local_steps, self.emulator_counts))

        start_time = time.time()

        while self.global_step < self.max_global_steps:

            loop_start_time = time.time()

            max_local_steps = self.max_local_steps
            for t in range(max_local_steps):
                next_actions, readouts_v_t, readouts_pi_t = self.__choose_next_actions(shared_states)
                actions_sum += next_actions
                for z in range(next_actions.shape[0]):
                    shared_actions[z] = next_actions[z]

                actions[t] = next_actions
                values[t] = readouts_v_t
                states[t] = shared_states

                # Start updating all environments with next_actions
                self.runners.update_environments()
                self.runners.wait_updated()
                # Done updating all environments, have new states, rewards and is_over

                episodes_over_masks[t] = 1.0 - shared_episode_over.astype(np.float32)

                for e, (actual_reward, episode_over) in enumerate(zip(shared_rewards, shared_episode_over)):
                    total_episode_rewards[e] += actual_reward
                    actual_reward = self.rescale_reward(actual_reward)
                    rewards[t, e] = actual_reward

                    emulator_steps[e] += 1
                    self.global_step += 1
                    if episode_over:
                        total_rewards.append(total_episode_rewards[e])
                        episode_summary = tf.Summary(value=[
                            tf.Summary.Value(tag='rl/reward', simple_value=total_episode_rewards[e]),
                            tf.Summary.Value(tag='rl/episode_length', simple_value=emulator_steps[e]),
                        ])
                        self.summary_writer.add_summary(episode_summary, self.global_step)
                        self.summary_writer.flush()
                        total_episode_rewards[e] = 0
                        emulator_steps[e] = 0
                        actions_sum[e] = np.zeros(self.num_actions)

            # states: (5,32,84,84,4), rewards: (5,32), over: (5,32), actions: (5,32,6)
            # self.req.send_zipped_pickle([states, rewards, episodes_over_masks, actions, values])
            # msg = self.req.recv_string()
            Process(target=self.send_zmq_batch_data,
                    kwargs={"data": [states, rewards, episodes_over_masks, actions, values]}).start()
            # print("Send batch data okay.")
            # print("******")

            # nest_state_value = self.session.run(
            #     self.network.output_layer_v,
            #     feed_dict={self.network.input_ph: shared_states})
            #
            # estimated_return = np.copy(nest_state_value)
            #
            # for t in reversed(range(max_local_steps)):
            #     estimated_return = rewards[t] + self.gamma * estimated_return * episodes_over_masks[t]
            #     y_batch[t] = np.copy(estimated_return)
            #     adv_batch[t] = estimated_return - values[t]
            #
            # flat_states = states.reshape([self.max_local_steps * self.emulator_counts] + list(shared_states.shape)[1:])
            # flat_y_batch = y_batch.reshape(-1)
            # flat_adv_batch = adv_batch.reshape(-1)
            # flat_actions = actions.reshape(max_local_steps * self.emulator_counts, self.num_actions)
            #
            # lr = self.get_lr()
            # feed_dict = {self.network.input_ph: flat_states,
            #              self.network.critic_target_ph: flat_y_batch,
            #              self.network.selected_action_ph: flat_actions,
            #              self.network.adv_actor_ph: flat_adv_batch,
            #              self.learning_rate: lr}
            #
            # _, summaries = self.session.run(
            #     [self.train_step, summaries_op],
            #     feed_dict=feed_dict)
            #
            # self.summary_writer.add_summary(summaries, self.global_step)
            # self.summary_writer.flush()

            counter += 1

            if counter % (2048 / self.emulator_counts) == 0:
                curr_time = time.time()
                global_steps = self.global_step
                last_ten = 0.0 if len(total_rewards) < 1 else np.mean(total_rewards[-10:])
                logging.info("Ran {} steps, at {} steps/s ({} steps/s avg), last 10 rewards avg {}"
                             .format(global_steps,
                                     self.max_local_steps * self.emulator_counts / (curr_time - loop_start_time),
                                     (global_steps - global_step_start) / (curr_time - start_time),
                                     last_ten))
            # self.save_vars()

            # if msg == "stop":
            #     print("Learner has received enough batch data.")
            #     print("Stop sampling.")
            #     break

            """ restore network if there's new checkpoint from GPU-Learner
            """

            cur_ckpt = tf.train.latest_checkpoint("/home/cloud/D3RL_ZMQ/logs/upload/")
            if cur_ckpt and self.latest_ckpt != cur_ckpt:
                self.network_saver.restore(self.session, cur_ckpt)
                self.latest_ckpt = cur_ckpt

        self.cleanup()

    def cleanup(self):
        super(PAACLearner, self).cleanup()
        self.runners.stop()
        self.flask_file_server_proc.terminate()
