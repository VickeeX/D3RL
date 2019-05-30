# -*- coding: utf-8 -*-

"""
    File name    :    ZMQ_utils
    Date         :    08/05/2019
    Description  :    {TODO}
    Author       :    VickeeX
"""

"""A Socket subclass that adds some serialization methods."""

import zlib, zmq, pickle


class SerializingSocket(zmq.Socket):
    """A class with some extra serialization methods

    send_zipped_pickle is just like send_pyobj, but uses
    zlib to compress the stream before sending.

    send_array sends numpy arrays with metadata necessary
    for reconstructing the array on the other side (dtype,shape).
    """

    def send_zipped_pickle(self, obj, flags=0, protocol=-1):
        """pack and compress an object with pickle and zlib."""
        pobj = pickle.dumps(obj, protocol)
        zobj = zlib.compress(pobj)
        # print('zipped pickle is %i bytes' % len(zobj))
        return self.send(zobj, flags=flags)

    def recv_zipped_pickle(self, flags=0):
        """reconstruct a Python object sent with zipped_pickle"""
        zobj = self.recv(flags)
        pobj = zlib.decompress(zobj)
        return pickle.loads(pobj)

        # def send_array(self, A, flags=0, copy=True, track=False):
        #     """send a numpy array with metadata"""
        #     md = dict(
        #             dtype=str(A.dtype),
        #             shape=A.shape,
        #     )
        #     self.send_json(md, flags | zmq.SNDMORE)
        #     return self.send(A, flags, copy=copy, track=track)

        # def recv_array(self, flags=0, copy=True, track=False):
        #     """recv a numpy array"""
        #     md = self.recv_json(flags=flags)
        #     msg = self.recv(flags=flags, copy=copy, track=track)
        #     A = np.frombuffer(msg, dtype=np.float32)
        #     return A.reshape((32, 6))


class SerializingContext(zmq.Context):
    _socket_class = SerializingSocket

# def main():
#     shared_states = np.zeros(shape=(5, 32, 84, 84, 4), dtype=np.uint8)
#     shared_actions = np.zeros(shape=(5, 32, 6), dtype=np.float32)
#     shared_rewards = np.zeros(shape=(5, 32,), dtype=np.float32)
#     data = []
#     for e, (s, a, r) in enumerate(zip(shared_states, shared_actions, shared_rewards)):
#         data.append((e, s, a, r))
#
#     ctx = SerializingContext()
#     req = ctx.socket(zmq.REQ)
#     req.connect("tcp://127.0.0.1:6666")
#
#     # print("Array is %i bytes" % (A.nbytes))
#
#     req.send_zipped_pickle(data)
#     req.close()
#
