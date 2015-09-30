from __future__ import division, print_function

from math import ceil
from time import time

from toolz import merge, frequencies
from tornado import gen
from tornado.gen import Return
from tornado.concurrent import Future
from tornado.locks import Event
from tornado.ioloop import IOLoop
from tornado.iostream import StreamClosedError

from dask.async import start_state_from_dask
from dask import istask

from .core import connect, rpc
from .client import RemoteData


log = print


@gen.coroutine
def _get(ip, port, dsk, keys):
    loop = IOLoop.current()

    center_stream = yield connect(ip, port)
    center = rpc(center_stream)
    who_has = yield center.who_has()
    has_what = yield center.has_what()
    ncores = yield center.ncores()
    available_cores = ncores

    workers = sorted(ncores)

    state = start_state_from_dask(dsk)
    dependencies = state['dependencies']
    dependents = state['dependents']
    ready = [k for k in dsk if not dependencies[k]]

    stacks = {w: [] for w in workers}
    finished_tasks = set()
    idling = dict()

    completed = {key: Event() for key in dsk}

    finished = [False]
    finished_event = Event()
    worker_done = {worker: Event() for worker in workers}
    center_done = Event()
    delete_done = Event()

    """
    Distribute leaves among workers

    We distribute leaf tasks (tasks with no dependencies) among workers
    uniformly.  In the future we should use ordering from dask.order.
    """
    k = int(ceil(len(ready) / len(workers)))
    for i, worker in enumerate(workers):
        stacks[worker].extend(ready[i*k: (i + 1)*k][::-1])

    @gen.coroutine
    def add_key_to_stack(key):
        """ Add key to a worker's stack once key becomes available

        We choose which stack to add the key/task based on

        1.  Whether or not that worker has *a* data dependency of this task
        2.  How short that worker's stack is
        """
        deps = dependencies[key]
        yield [completed[dep].wait() for dep in deps]  # wait until dependencies finish

        # TODO: look at args for RemoteData
        workers = frequencies(w for dep in deps
                                for w in who_has[dep])
        worker = min(workers, key=lambda w: len(stacks[w]))
        stacks[worker].append(key)

        if idling.get(worker):
            idling[worker].set()
            del idling[worker]

    for key in dsk:
        if dependencies[key]:
            loop.spawn_callback(add_key_to_stack, key)

    @gen.coroutine
    def delete_intermediates():
        """ Delete intermediates from distributed as they become unnecessary

        This fires off a coroutine for every intermediate, waiting on the
        events in completed for the right tasks to finish.  Once the dependent
        tasks finish we add the key to a list ``delete_keys``.  We send this
        list of keys to the center for deletion once a second
        """
        delete_keys = list()

        @gen.coroutine
        def delete_intermediate(key):
            """ Wait on appropriate events for a single key """
            deps = dependents[key]
            yield [completed[dep].wait() for dep in deps]
            raise Return(key)

        intermediates = [key for key in dsk
                             if key not in keys]
        for key in intermediates:
            loop.spawn_callback(delete_intermediate, key)
        wait_iterator = gen.WaitIterator(*[delete_intermediate(key)
                                            for key in intermediates])
        n = len(intermediates)
        k = 0

        @gen.coroutine
        def clear_queue():
            if delete_keys:
                _keys = delete_keys[:]  # make a copy
                del delete_keys[:]      # clear out old list
                yield center.delete_data(keys=_keys)

        last = time()
        while not wait_iterator.done():
            key = yield wait_iterator.next()
            delete_keys.append(key)
            if time() - last > 1:
                last = time()
                yield clear_queue()
        yield clear_queue()

        center_stream.close()           # All done
        center_done.set()

    loop.spawn_callback(delete_intermediates)

    @gen.coroutine
    def handle_worker(ident):
        """ Handle all communication with a single worker

        We pull tasks from a list in ``stacks[ident]`` and process each task in
        turn.  If this list goes empty we wait on an event in ``idling[ident]``
        which should be triggered to wake us back up again.

        ident :: (ip, port)
        """
        stack = stacks[ident]
        stream = yield connect(*ident)
        worker = rpc(stream)

        while True:
            if not stack:
                idling[ident] = Event()
                yield idling[ident].wait()

            if finished[0]:
                break

            key = stack.pop()
            task = dsk[key]
            if not istask(task):
                response = yield worker.update_data(data={key: task})
                assert response == b'OK'
            else:
                needed = [arg for arg in task[1:] if hashable(arg) and arg in dsk]
                response = yield worker.compute(function=task[0],
                                                args=task[1:],
                                                needed=needed,
                                                key=key,
                                                kwargs={})
                assert response == 'success'

            completed[key].set()
            who_has[key].add(ident)
            has_what[ident].add(key)
            finished_tasks.add(key)

        stream.close()
        worker_done[ident].set()

    for worker in workers:
        loop.spawn_callback(handle_worker, worker)

    # TODO support nested keys
    yield [completed[key].wait() for key in keys]
    finished[0] = True
    for event in idling.values():
        event.set()
    result = [RemoteData(key, ip, port) for key in keys]

    yield [center_done.wait()] + [e.wait() for e in worker_done.values()]

    raise Return(result)


def hashable(x):
    try:
        hash(x)
        return True
    except TypeError:
        return False
