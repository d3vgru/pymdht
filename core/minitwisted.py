# Copyright (C) 2009-2010 Raul Jimenez
# Released under GNU LGPL 2.1
# See LICENSE.txt for more information

'''
Minitwisted is inspired by the Twisted framework. Although, it is much
simpler.
- It can only handle one UDP connection per reactor.
- Reactor runs in a thread
- You can use call_later and call_now to run your code in thread-safe mode

'''

#from __future__ import with_statement

import sys
import socket
import threading
import ptime as time

import logging

from floodbarrier import FloodBarrier

#from profilestats import profile

logger = logging.getLogger('dht')


BUFFER_SIZE = 3000


class Task(object):
    
    '''Simple container for a task '''

    def __init__(self, delay, callback_f, *args, **kwds):
        '''
        Create a task instance. Here is when the call time is calculated.

        '''
        self.delay = delay
        self.callback_f = callback_f
        self.args = args
        self.kwds = kwds
        self.call_time = time.time() + self.delay
        self._cancelled = False

    @property
    def cancelled(self):
        return self._cancelled
    
    def fire_callback(self):
        """Fire a callback (if it hasn't been cancelled)."""
        tasks_to_schedule = []
        msgs_to_send = []
        import sys
        print >>sys.stderr, '\nfiring>>>', self
        if not self._cancelled:
            try:
                tasks_to_schedule, msgs_to_send = self.callback_f(
                    *self.args, **self.kwds)
            except:
                '''
                import sys
                print >>sys.stderr, '\n\n>>>>>>>>>>>>>>>>', self.__dict__
                raise
                '''
            # This task cannot be used again
            self._cancelled = True
        '''
        Tasks may have arguments which reference to the objects which
        created the task. That is, they create a memory cycle. In order
        to break the memoery cycle, those arguments are deleted.
        '''
        del self.callback_f
        del self.args
        del self.kwds
        return tasks_to_schedule, msgs_to_send

    def cancel(self):
        """Cancel a task (callback won't be called when fired)"""
        self._cancelled = True
        

class TaskManager(object):

    """Manage tasks"""

    def __init__(self):
        self.tasks = {}
        self.next_task = None

    def add(self, task):
        """Add task to the TaskManager"""

        if not task.callback_f:
            #no callback, just ignore. The code that created this callback
            #doesn't really call anything back, it probably created the task
            #because some function required a task (e.g. timeout_task).
            return
        ms_delay = int(task.delay * 1000)
        # we need integers for the dictionary (floats are not hashable)
        self.tasks.setdefault(ms_delay, []).append(task)
        if self.next_task is None or task.call_time < self.next_task.call_time:
            self.next_task = task

#    def __iter__(self):
#        """Makes (along with next) this objcet iterable"""
#        return self

    def _get_next_task(self):
        """Return the task which should be fired next"""
        
        next_task = None
        for _, task_list in self.tasks.items():
            task = task_list[0]
            if next_task is None:
                next_task = task
            if task.call_time < next_task.call_time:
                next_task = task
        return next_task
                

    def consume_task(self):
        """
        Return the task which should be fire next and removes it from
        TaskManager 

        """
        current_time = time.time()
        if self.next_task is None:
            # no pending tasks
            return None #raise StopIteration
        if self.next_task.call_time > current_time:
            # there are pending tasks but it's too soon to fire them
            return None #raise StopIteration
        # self.next_task is ready to be fired
        task = self.next_task
        # delete  consummed task and get next one (if any)
        ms_delay = int(self.next_task.delay * 1000)
        del self.tasks[ms_delay][0]
        if not self.tasks[ms_delay]:
            # delete list when it's empty
            del self.tasks[ms_delay]
        self.next_task = self._get_next_task()
        #TODO2: make it yield
        return task
                            
class ThreadedReactor(threading.Thread):

    """
    Object inspired in Twisted's reactor.
    Run in its own thread.
    It is an instance, not a nasty global
    
    """
    def __init__(self, task_interval=0.1,
                 floodbarrier_active=True):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        
        self.stop_flag = False
        self._lock = threading.RLock()

        self.task_interval = task_interval
        self.floodbarrier_active = floodbarrier_active
        self.tasks = TaskManager()
        if self.floodbarrier_active:
            self.floodbarrier = FloodBarrier()

    #@profile
    def run(self):
        try:
            self._protected_run()
        except:
            logger.critical('MINITWISTED CRASHED')
            logger.exception('MINITWISTED CRASHED')

    def _protected_run(self):
        """Main loop activated by calling self.start()"""
        
        last_task_run_ts = 0
        stop_flag = self.stop_flag
        while not stop_flag:
            tasks_to_schedule = []
            msgs_to_send = []
            # Perfom scheduled tasks
            if time.time() - last_task_run_ts > self.task_interval:
                #with self._lock:
                self._lock.acquire()
                try:
                    while True:
                        task = self.tasks.consume_task()
                        if task is None:
                            break
                        (tasks_to_schedule,
                         msgs_to_send) = task.fire_callback()
                        last_task_run_ts = time.time()
                    stop_flag = self.stop_flag
                finally:
                    self._lock.release()
            for task in tasks_to_schedule:
                self.tasks.add(task)
            for msg, addr in msgs_to_send:
                self.sendto(msg, addr)
            # Get data from the network
            tasks_to_schedule = []
            msgs_to_send = []
            try:
                data, addr = self.s.recvfrom(BUFFER_SIZE)
            # except (AttributeError):
            #     logger.warning('udp_listen has not been called')
            #     time.sleep(self.task_interval)
            #     #TODO2: try using Event and wait
            except (socket.timeout):
                pass #timeout
            except (socket.error), e:
                logger.critical(
                    'Got socket.error when receiving (more info follows)')
                logger.exception('See critical log above')
            else:
                ip_is_blocked = self.floodbarrier_active and \
                                self.floodbarrier.ip_blocked(addr[0])
                if ip_is_blocked:
                    logger.warning('%s blocked' % `addr`)
                else:
                    (tasks_to_schedule,
                     msgs_to_send) = self.datagram_received_f(
                        data, addr)
            for task in tasks_to_schedule:
                self.tasks.add(task)
            for msg, addr in msgs_to_send:
                self.sendto(msg, addr)
        logger.debug('Reactor stopped')
            
    def stop(self):
        """Stop the thread. It cannot be resumed afterwards????"""
        #with self._lock:
        self._lock.acquire()
        try:
            self.stop_flag = True
        finally:
            self._lock.release()
        # wait a little for the thread to end
        time.sleep(self.task_interval)


#     def stop_and_wait(self):
#         """Stop the thread and wait a little (task_interval)."""

#         self.stop()
        # wait a little before ending the thread's life
#        time.sleep(self.task_interval * 2)

    def listen_udp(self, port, datagram_received_f):
        """Listen on given port and call the given callback when data is
        received.

        """
        self.datagram_received_f = datagram_received_f
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.settimeout(self.task_interval)
        my_addr = ('', port)
        self.s.bind(my_addr)
        return self.s
        
    def call_later(self, delay, callback_f, *args, **kwds):
        """Call the given callback with given arguments in the future (delay
        seconds).

        """
        #with self._lock:
        self._lock.acquire()
        try:
            task = Task(delay, callback_f, *args, **kwds)
            self.tasks.add(task)
        finally:
            self._lock.release()
        return task
            
    def call_asap(self, callback_f, *args, **kwds):
        """Same as call_later with delay 0 seconds. That is, call as soon as
        possible""" 
        return self.call_later(0, callback_f, *args, **kwds)
        
        
    def sendto(self, data, addr):
        """Send data to addr using the UDP port used by listen_udp."""
        #with self._lock:
        self._lock.acquire()
        try:
            try:
                bytes_sent = self.s.sendto(data, addr)
                if bytes_sent != len(data):
                    logger.critical(
                        'Just %d bytes sent out of %d (Data follows)' % (
                            bytes_sent,
                            len(data)))
                    logger.critical('Data: %s' % data)
            except (socket.error):
                logger.critical(
                    'Got socket.error when sending (more info follows)')
                logger.critical('Sending data to %r\n%r' % (addr,
                                                             data))
                logger.exception('See critical log above')
        finally:
            self._lock.release()


class ThreadedReactorSocketError(ThreadedReactor):

    def listen_udp(self, delay, callback_f, *args, **kwds):
        self.s = _SocketMock()

                
class ___ThreadedReactorMock(object):
 
    def __init__(self, task_interval=0.1):
        pass
    
    def start(self):
        pass

    stop = start
#    stop_and_wait = stop

    def listen_udp(self, port, data_received_f):
        self.s = _SocketMock()
        return self.s

    def call_later(self, delay, callback_f, *args, **kwds):
        return Task(delay, callback_f, *args, **kwds)

    def sendto(self, data, addr):
        pass
    


    
class _SocketMock(object):

    def sendto(self, data, addr):
        if len(data) > BUFFER_SIZE:
            return BUFFER_SIZE
        raise socket.error

    def recvfrom(self, buffer_size):
        raise socket.error
